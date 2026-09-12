"""Check cloud permissions and build boundaries without using an AWS account."""

import struct
import zipfile

import pytest

from deployment.agentcore_spec import RuntimeSpec, build_plan, transaction_search_policy
from deployment.build_runtime import SOURCE_FILES, build_package, validate_native


def spec(**changes):
    return RuntimeSpec(**{
        "account": "123456789012", "region": "us-east-1", "name": "watershed_memory_test",
        "model_id": "amazon.nova-pro-v1:0", "artifact_sha256": "a" * 64, **changes,
    })


def test_runtime_plan_has_exact_model_and_package_permissions():
    plan = build_plan(spec())
    statements = plan["execution_policy"]["Statement"]
    model = next(s for s in statements if s["Sid"] == "BedrockModel")
    assert model["Resource"] == ["arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"]
    artifact = next(s for s in statements if s["Sid"] == "ReadArtifact")
    assert artifact["Action"] == ["s3:GetObject"]
    assert artifact["Resource"] == [f"arn:aws:s3:::{plan['bucket']}/{plan['object_key']}"]
    assert all(not any(a in str(s["Action"]) for a in ["s3:Put", "dynamodb:", "iam:"])
               for s in statements)
    trust = plan["trust_policy"]["Statement"][0]["Condition"]
    assert trust["StringEquals"]["aws:SourceAccount"] == "123456789012"
    assert trust["ArnLike"]["aws:SourceArn"].endswith("runtime/watershed_memory_test-*")


def test_runtime_request_is_bounded_and_no_public_authorizer_is_added():
    request = build_plan(spec())["create_runtime"]
    assert "authorizerConfiguration" not in request
    assert build_plan(spec())["runtime_metadata"] == {"requireMMDSV2": True}
    assert request["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 60, "maxLifetime": 300}
    code = request["agentRuntimeArtifact"]["codeConfiguration"]
    assert code["runtime"] == "PYTHON_3_12"
    assert code["entryPoint"] == ["runtime/launch.py"]
    assert build_plan(spec())["create_runtime"]["clientToken"] == request["clientToken"]
    assert build_plan(spec(artifact_sha256="b" * 64))["create_runtime"]["clientToken"] != request["clientToken"]
    from boto3 import Session
    from botocore.validate import validate_parameters
    model = Session()._session.get_service_model("bedrock-agentcore-control")
    validate_parameters(request, model.operation_model("CreateAgentRuntime").input_shape)
    policy = next(s for s in build_plan(spec())["execution_policy"]["Statement"]
                  if s["Sid"] == "RuntimeTracePolicy")
    assert policy["Action"] == ["logs:PutResourcePolicy"]
    assert "watershed_memory_test-*" in policy["Resource"][0]


def test_tracing_configuration_rejects_unbounded_account_or_region():
    with pytest.raises(ValueError):
        transaction_search_policy("*", "us-east-1")
    with pytest.raises(ValueError):
        transaction_search_policy("123456789012", "*")


@pytest.mark.parametrize("changes", [{"account": "*"}, {"name": "../../private"},
                                     {"region": "*"}, {"model_id": "*"},
                                     {"artifact_sha256": "bad"}])
def test_invalid_resource_parameters_fail_before_cloud_work(changes):
    with pytest.raises(ValueError):
        build_plan(spec(**changes))


def test_native_architecture_is_checked_instead_of_trusting_the_filename():
    elf = bytearray(64)
    elf[:6] = b"\x7fELF\x02\x01"
    elf[18:20] = struct.pack("<H", 183)
    validate_native("extension.so", bytes(elf))
    elf[18:20] = struct.pack("<H", 62)
    with pytest.raises(ValueError, match="Non-ARM64"):
        validate_native("extension.so", bytes(elf))
    with pytest.raises(ValueError, match="Windows"):
        validate_native("launcher", b"MZdisguised-host-binary")


def test_archive_uses_explicit_application_files_and_omits_host_launchers(tmp_path):
    source = tmp_path / "source"
    dependencies = tmp_path / "dependencies"
    for name in SOURCE_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public fixture", encoding="utf-8")
    (source / "AGENTS.md").write_text("private instructions", encoding="utf-8")
    metadata = dependencies / "aws_opentelemetry_distro-0.19.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("package metadata", encoding="utf-8")
    launcher = dependencies / "bin" / "opentelemetry-instrument.exe"
    launcher.parent.mkdir()
    launcher.write_bytes(b"MZhost launcher")
    output = tmp_path / "runtime.zip"
    result = build_package(source, dependencies, output)
    with zipfile.ZipFile(output) as archive:
        assert "runtime/launch.py" in archive.namelist()
        assert "AGENTS.md" not in archive.namelist()
        assert not any(n.endswith(".exe") for n in archive.namelist())
        assert all((i.external_attr >> 16) & 0o777 == 0o644 for i in archive.infolist())
    assert result["platform"] == "linux-aarch64"
    with pytest.raises(ValueError, match="fresh artifact"):
        build_package(source, dependencies, output)
