"""Current runtime artifacts remain explicit, private-free and historically separate."""

import hashlib
import runpy
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

from deployment import agentcore_spec, build_runtime

PUBLIC = (
    "watershed_memory/__init__.py",
    "watershed_memory/current/runtime_config.py",
    "watershed_memory/data/locations.json",
    "watershed_memory/static/current.js",
    "runtime/current_entrypoint.py",
    "runtime/current_launch.py",
    "LICENSE",
)
METADATA = "aws_opentelemetry_distro-0.19.0.dist-info/METADATA"


@pytest.fixture
def artifact_tree(tmp_path):
    source, deps = tmp_path / "source", tmp_path / "dependencies"
    for name in set(PUBLIC) | set(build_runtime.SOURCE_FILES):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"public content: {name}\n", encoding="utf-8")
    for name in ("AGENTS.md", ".local/director/REMEMBER.md", "temp/key.json"):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("PRIVATE-CANARY", encoding="utf-8")
    metadata = deps / METADATA
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: aws-opentelemetry-distro\nVersion: 0.19.0\n", encoding="utf-8")
    return source, deps, tmp_path / "artifact.zip"


def test_explicit_current_archive_contains_exact_selected_bytes(artifact_tree):
    source, deps, output = artifact_tree
    result = build_runtime.build_package(source, deps, output, source_files=PUBLIC)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {*PUBLIC, METADATA}
        assert archive.testzip() is None
        for name in PUBLIC:
            assert archive.read(name) == (source / name).read_bytes()
        assert all(b"PRIVATE-CANARY" not in archive.read(n) for n in archive.namelist())
    assert result["artifact_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["platform"] == "linux-aarch64"
    assert {e["path"] for e in result["entries"]} == {*PUBLIC, METADATA}


def test_default_archive_still_selects_only_historical_files(artifact_tree):
    source, deps, output = artifact_tree
    build_runtime.build_package(source, deps, output)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {*build_runtime.SOURCE_FILES, METADATA}
        assert "runtime/current_launch.py" not in archive.namelist()


@pytest.mark.parametrize("selection", [
    (), [], list(PUBLIC), "watershed_memory/__init__.py", (None,), (1,),
    ("",), ("/LICENSE",), ("C:/LICENSE",), ("../LICENSE",),
    ("watershed_memory//__init__.py",), ("./LICENSE",),
    ("watershed_memory/../LICENSE",), ("watershed_memory\\__init__.py",),
    ("watershed_memory/\x00key.py",), ("watershed_memory/.env",),
    ("AGENTS.md",), ("watershed_memory/AGENTS.md",), ("PROJECT_STATE.md",),
    (".local/director/REMEMBER.md",), ("temp/key.json",), ("tests/test_secret.py",),
    ("runtime/arbitrary.py",), ("README.md",), ("watershed_memory/key.pem",),
    ("watershed_memory/türkçe.py",), ("watershed_memory/data/",),
    ("LICENSE", "LICENSE"), ("LICENSE", "license"),
])
def test_invalid_selection_fails_before_artifact_creation(artifact_tree, selection):
    source, deps, output = artifact_tree
    with pytest.raises(ValueError):
        build_runtime.build_package(source, deps, output, source_files=selection)
    assert not output.exists()
    assert not output.with_suffix(".manifest.json").exists()


@pytest.mark.parametrize("name", [
    "watershed_memory/current/runtime_config.py",
    "watershed_memory/CURRENT/runtime_config.py",
    "watershed_memory/unselected.py",
    "watershed_memory.py",
    "runtime/current_launch.py",
])
def test_dependency_cannot_shadow_application_namespace(artifact_tree, name):
    source, deps, output = artifact_tree
    shadow = deps / name
    shadow.parent.mkdir(parents=True, exist_ok=True)
    shadow.write_text("shadowed application", encoding="utf-8")
    with pytest.raises(ValueError):
        build_runtime.build_package(source, deps, output, source_files=PUBLIC)
    assert not output.exists()


def test_sibling_manifest_is_never_overwritten(artifact_tree):
    source, deps, output = artifact_tree
    prior = output.with_suffix(".manifest.json")
    prior.write_text("prior inspected artifact", encoding="utf-8")
    with pytest.raises(ValueError):
        build_runtime.build_package(source, deps, output, source_files=PUBLIC)
    assert prior.read_text() == "prior inspected artifact"
    assert not output.exists()


@pytest.mark.parametrize("case_alias", [False, True])
def test_duplicate_dependency_archive_names_are_refused(artifact_tree, monkeypatch, case_alias):
    source, deps, output = artifact_tree
    first = deps / "dependency" / "module.py"
    first.parent.mkdir()
    first.write_text("dependency", encoding="utf-8")
    second = deps / "dependency" / ("MODULE.PY" if case_alias else "module.py")
    real_rglob = Path.rglob
    monkeypatch.setattr(Path, "rglob", lambda p, pattern: iter([deps / METADATA, first, second])
                        if p == deps else real_rglob(p, pattern))
    with pytest.raises(ValueError):
        build_runtime.build_package(source, deps, output, source_files=PUBLIC)
    assert not output.exists()


def test_selected_source_must_resolve_inside_source_root(artifact_tree, monkeypatch):
    source, deps, output = artifact_tree
    selected = source / PUBLIC[1]
    real_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == selected:
            return source.parent / "outside.py"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError):
        build_runtime.build_package(source, deps, output, source_files=PUBLIC)
    assert not output.exists()


def test_selected_source_parent_link_is_rejected(artifact_tree, monkeypatch):
    source, deps, output = artifact_tree
    real_link = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == source / "watershed_memory"
                        or real_link(p))
    with pytest.raises(ValueError):
        build_runtime.build_package(source, deps, output, source_files=PUBLIC)
    assert not output.exists()


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("link_kind", ["is_symlink", "is_junction"])
def test_dependency_links_are_rejected_before_nonfile_skip(
    artifact_tree, monkeypatch, directory, link_kind
):
    source, deps, output = artifact_tree
    linked = deps / "linked_dependency"
    if directory:
        linked.mkdir()
    else:
        linked.write_text("dependency", encoding="utf-8")
    real_check = getattr(Path, link_kind)
    monkeypatch.setattr(Path, link_kind, lambda p: p == linked or real_check(p))
    with pytest.raises(ValueError):
        build_runtime.build_package(source, deps, output, source_files=PUBLIC)
    assert not output.exists()


def specification(**changes):
    return agentcore_spec.RuntimeSpec(**{
        "account": "123456789012", "region": "us-east-1",
        "name": "watershed_memory_current", "model_id": "amazon.nova-pro-v1:0",
        "artifact_sha256": "a" * 64, **changes,
    })


def nova_2_specification(**changes):
    return specification(**{
        "model_id": "us.amazon.nova-2-lite-v1:0",
        "name": "watershed_memory_current_20260914",
        **changes,
    })


def test_nova_2_lite_current_plan_has_exact_cross_region_inference_permissions():
    spec = nova_2_specification()
    plan = agentcore_spec.build_current_plan(spec)
    statements = plan["execution_policy"]["Statement"]
    profile = next(item for item in statements if item["Sid"] == "BedrockInferenceProfile")
    targets = next(item for item in statements if item["Sid"] == "BedrockInferenceTargets")
    profile_arn = (
        "arn:aws:bedrock:us-east-1:123456789012:"
        "inference-profile/us.amazon.nova-2-lite-v1:0"
    )
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    assert profile == {
        "Sid": "BedrockInferenceProfile", "Effect": "Allow", "Action": actions,
        "Resource": [profile_arn],
    }
    assert targets == {
        "Sid": "BedrockInferenceTargets", "Effect": "Allow", "Action": actions,
        "Resource": [
            "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-lite-v1:0",
            "arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-2-lite-v1:0",
            "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-2-lite-v1:0",
        ],
        "Condition": {"StringEquals": {"bedrock:InferenceProfileArn": profile_arn}},
    }
    assert not any(item["Sid"] == "BedrockModel" for item in statements)
    request = plan["create_runtime"]
    assert request["environmentVariables"]["WATERSHED_MODEL_ID"] == spec.model_id


@pytest.mark.parametrize("changes", [
    {"model_id": "us.amazon.nova-premier-v1:0"},
    {"model_id": "amazon.nova-2-lite-v1:0"},
    {"model_id": "us.amazon.nova-2-lite-v1:0", "region": "us-west-2"},
    {"model_id": "us.amazon.nova-2-lite-v1:0", "name": "watershed_memory_historical"},
    {"model_id": "us.amazon.nova-2-lite-v1:0", "name": "watershed_memory_current"},
    {"model_id": "us.amazon.nova-2-lite-v1:0", "name": "watershed_memory_currently"},
])
def test_unapproved_model_or_nova_2_scope_is_refused(changes):
    with pytest.raises(ValueError):
        specification(**changes)


def test_current_plan_changes_only_fixed_target_description_and_token():
    spec = specification()
    old = agentcore_spec.build_plan(spec)
    current = agentcore_spec.build_current_plan(spec)
    request = current["create_runtime"]
    code = request["agentRuntimeArtifact"]["codeConfiguration"]
    assert code["entryPoint"] == ["runtime/current_launch.py"]
    assert request["clientToken"] != old["create_runtime"]["clientToken"]
    expected_token = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        "watershed-memory:current-v3:123456789012:us-east-1:"
        f"watershed_memory_current:{'a' * 64}",
    ))
    assert request["clientToken"] == expected_token
    assert request["clientToken"] == agentcore_spec.build_current_plan(spec)["create_runtime"]["clientToken"]
    assert request["description"] != old["create_runtime"]["description"]
    assert set(request["environmentVariables"]) == {
        "WATERSHED_MODEL_ID", "WATERSHED_AWS_REGION", "OTEL_SERVICE_NAME",
        "AGENT_OBSERVABILITY_ENABLED", "OTEL_PYTHON_DISTRO", "OTEL_PYTHON_CONFIGURATOR",
    }
    code["entryPoint"] = old["create_runtime"]["agentRuntimeArtifact"]["codeConfiguration"]["entryPoint"]
    request["clientToken"] = old["create_runtime"]["clientToken"]
    request["description"] = old["create_runtime"]["description"]
    assert current == old == agentcore_spec.build_plan(spec)
    assert agentcore_spec.build_current_plan(spec)["create_runtime"]["agentRuntimeArtifact"]["codeConfiguration"]["entryPoint"] == ["runtime/current_launch.py"]


def test_current_plan_is_bound_to_artifact_and_matches_installed_aws_shape():
    from botocore.session import Session
    from botocore.validate import validate_parameters

    first = agentcore_spec.build_current_plan(specification())
    second = agentcore_spec.build_current_plan(specification(artifact_sha256="b" * 64))
    assert first["create_runtime"]["clientToken"] != second["create_runtime"]["clientToken"]
    model = Session().get_service_model("bedrock-agentcore-control")
    validate_parameters(first["create_runtime"], model.operation_model("CreateAgentRuntime").input_shape)


@pytest.mark.parametrize("as_main", [False, True])
def test_current_launcher_is_inert_on_import_and_has_fixed_target(monkeypatch, as_main):
    from opentelemetry.instrumentation import auto_instrumentation

    path = Path(__file__).resolve().parents[1] / "runtime/current_launch.py"
    calls = []
    monkeypatch.setattr(auto_instrumentation, "run", lambda: calls.append(list(sys.argv)))
    monkeypatch.setattr(sys, "argv", ["launcher", "--entrypoint", "arbitrary.py"])
    monkeypatch.setenv("WATERSHED_ENTRYPOINT", "arbitrary.py")
    scope = runpy.run_path(str(path), run_name="__main__" if as_main else "current_launcher_test")
    if not as_main:
        assert calls == []
        scope["main"]()
    assert calls == [["opentelemetry-instrument", sys.executable,
                      str(path.with_name("current_entrypoint.py"))]]
