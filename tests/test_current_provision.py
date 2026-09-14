"""Current provisioning is an explicit closed choice; historical remains default."""

import hashlib
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from deployment import provision
from deployment.agentcore_spec import RuntimeSpec, build_current_plan, build_plan


def inputs(tmp_path, *, current=True, name="watershed_memory_current_20260914"):
    artifact = tmp_path / "artifact.zip"
    with zipfile.ZipFile(artifact, "x") as archive:
        names = ("runtime/current_launch.py", "runtime/current_entrypoint.py") if current else (
            "runtime/launch.py", "runtime/entrypoint.py")
        for target in names:
            archive.writestr(target, "# selected fixture\n")
    spec = RuntimeSpec("123456789012", "us-east-1", name, "amazon.nova-pro-v1:0",
                       hashlib.sha256(artifact.read_bytes()).hexdigest())
    return spec, artifact, tmp_path / "journal.jsonl", datetime.now(timezone.utc) + timedelta(hours=1)


def test_default_constructor_preserves_historical_plan(tmp_path):
    args = inputs(tmp_path, current=False, name="watershed_memory_test")
    runner = provision.Provisioner(*args, session=object())
    assert runner.plan == build_plan(args[0])


def test_current_constructor_uses_exact_closed_plan_without_creating_clients(tmp_path):
    args = inputs(tmp_path)
    runner = provision.Provisioner(*args, session=object(), runtime_mode="current-v3")
    assert runner.plan == build_current_plan(args[0])
    assert not args[2].exists()


@pytest.mark.parametrize("mode", [None, True, 3, "", "current", "current-v2", "arbitrary.py"])
def test_invalid_mode_fails_before_session_or_journal(tmp_path, monkeypatch, mode):
    args = inputs(tmp_path)
    monkeypatch.setattr(provision.boto3, "Session", lambda **_: pytest.fail("No session admitted"))
    with pytest.raises(ValueError):
        provision.Provisioner(*args, runtime_mode=mode)
    assert not args[2].exists()


@pytest.mark.parametrize("current,name", [
    (False, "watershed_memory_current_20260914"), (True, "watershed_memory_test"),
])
def test_current_mode_refuses_historical_artifact_or_resource_namespace(
    tmp_path, monkeypatch, current, name
):
    args = inputs(tmp_path, current=current, name=name)
    monkeypatch.setattr(provision.boto3, "Session", lambda **_: pytest.fail("No session admitted"))
    with pytest.raises(ValueError):
        provision.Provisioner(*args, runtime_mode="current-v3")


def test_current_endpoint_uses_fixed_current_qualifier(tmp_path, monkeypatch):
    args = inputs(tmp_path)
    runner = provision.Provisioner(*args, session=object(), runtime_mode="current-v3")
    runtime_id = args[0].name + "-Abc123"
    arn = f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{runtime_id}"
    monkeypatch.setattr(runner, "inspect", lambda _: {
        "status": "READY", "agentRuntimeVersion": "2", "agentRuntimeArn": arn,
    })
    monkeypatch.setattr(runner, "verify_role", lambda: None)
    monkeypatch.setattr(runner, "verify_bucket", lambda: None)
    calls = []

    def call(service, operation, **kwargs):
        calls.append((service, operation, kwargs))
        return {"agentRuntimeId": runtime_id, "agentRuntimeArn": arn,
                "agentRuntimeEndpointArn": arn + "/runtime-endpoint/current_v3",
                "endpointName": "current_v3", "targetVersion": "2"}

    monkeypatch.setattr(runner, "call", call)
    assert runner.endpoint(runtime_id, "2")["endpointName"] == "current_v3"
    assert len(calls) == 1 and calls[0][2]["name"] == "current_v3"


def test_cli_can_render_current_plan_without_authentication(tmp_path, monkeypatch, capsys):
    args = inputs(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(args[0].__dict__), encoding="utf-8")
    monkeypatch.setattr(provision.boto3, "Session", lambda **_: pytest.fail("Render is offline"))
    monkeypatch.setattr(sys, "argv", ["provision", "render", "--runtime-mode", "current-v3",
        "--spec", str(spec_path), "--artifact", str(args[1]), "--deadline", args[3].isoformat()])
    provision.main()
    assert json.loads(capsys.readouterr().out) == build_current_plan(args[0])


def test_current_render_refuses_historical_namespace_without_output(tmp_path, monkeypatch, capsys):
    args = inputs(tmp_path, name="watershed_memory_test")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(args[0].__dict__), encoding="utf-8")
    monkeypatch.setattr(provision.boto3, "Session", lambda **_: pytest.fail("Render is offline"))
    monkeypatch.setattr(sys, "argv", ["provision", "render", "--runtime-mode", "current-v3",
        "--spec", str(spec_path), "--artifact", str(args[1]), "--deadline", args[3].isoformat()])
    with pytest.raises(ValueError, match="separate resource namespace"):
        provision.main()
    assert not capsys.readouterr().out
    assert not args[2].exists()
