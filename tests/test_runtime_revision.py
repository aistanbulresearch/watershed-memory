"""Require exact previous state before a versioned Runtime source update."""

import base64
import hashlib
import json
from dataclasses import replace

import pytest
from test_provision import complete_runner

from deployment.provision import Provisioner
from deployment.revise_runtime import RuntimeRevision


def setup_revision(tmp_path):
    previous, aws = complete_runner(tmp_path, "existing_safe")
    artifact = tmp_path / "new.zip"
    artifact.write_bytes(b"new artifact fixture")
    spec = replace(previous.spec, artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    updated = Provisioner(spec, artifact, previous.journal, previous.deadline, session=aws)
    revision = RuntimeRevision(previous, updated, previous.spec.name + "-abc123", "1")
    aws.role_policy = previous.plan["execution_policy"]
    aws.get_role_policy = lambda **kwargs: {"PolicyDocument": aws.role_policy}

    def put_policy(**kwargs):
        aws._call("put_role_policy", kwargs)
        aws.role_policy = json.loads(kwargs["PolicyDocument"])

    aws.put_role_policy = put_policy
    aws.head_object = lambda **kwargs: {"ChecksumSHA256": base64.b64encode(bytes.fromhex(
        spec.artifact_sha256 if kwargs["Key"] == updated.plan["object_key"]
        else previous.spec.artifact_sha256)).decode()}
    original_update = aws.update_agent_runtime

    def update(**kwargs):
        response = original_update(**kwargs)
        aws.plan = updated.plan
        return response

    aws.update_agent_runtime = update

    def endpoint(**kwargs):
        aws._call("create_agent_runtime_endpoint", kwargs)
        arn = f"arn:aws:bedrock-agentcore:{spec.region}:{spec.account}:runtime/{revision.runtime_id}"
        return {"agentRuntimeId": revision.runtime_id, "agentRuntimeArn": arn,
                "agentRuntimeEndpointArn": arn + "/runtime-endpoint/" + kwargs["name"],
                "endpointName": kwargs["name"], "targetVersion": kwargs["agentRuntimeVersion"]}

    aws.create_agent_runtime_endpoint = endpoint
    updated.preflight()
    return revision, aws


def test_revision_keeps_old_artifact_and_endpoint_while_creating_new_version(tmp_path):
    revision, aws = setup_revision(tmp_path)
    revision.policy()
    statement = next(row for row in aws.role_policy["Statement"] if row["Sid"] == "ReadArtifact")
    assert len(statement["Resource"]) == 2
    assert all("*" not in resource for resource in statement["Resource"])
    result = revision.runtime()
    assert result["agentRuntimeVersion"] == "2"
    result = revision.endpoint("2")
    assert result["endpointName"] == "proof_v2"
    assert all(name not in ("delete_agent_runtime_endpoint", "update_agent_runtime_endpoint")
               for name, _ in aws.calls)
    assert sum(name == "update_agent_runtime" for name, _ in aws.calls) == 1


def test_revision_does_not_update_runtime_before_exact_combined_policy(tmp_path):
    revision, aws = setup_revision(tmp_path)
    with pytest.raises(RuntimeError, match="policy"):
        revision.runtime()
    assert not any(name == "update_agent_runtime" for name, _ in aws.calls)


@pytest.mark.parametrize("drift", ["version", "runtime_code", "role_trust"])
def test_previous_drift_blocks_policy_mutation(tmp_path, drift):
    revision, aws = setup_revision(tmp_path)
    if drift == "version":
        aws.runtime_version = "2"
    else:
        aws.fault = drift
    with pytest.raises(RuntimeError):
        revision.policy()
    assert not any(name == "put_role_policy" for name, _ in aws.calls)


def test_endpoint_cannot_target_an_unverified_or_old_version(tmp_path):
    revision, aws = setup_revision(tmp_path)
    revision.policy()
    revision.runtime()
    with pytest.raises(ValueError):
        revision.endpoint("1")
    with pytest.raises(ValueError):
        revision.endpoint("3")
    assert not any(name == "create_agent_runtime_endpoint" for name, _ in aws.calls)


def test_unexpected_returned_version_stops_before_endpoint_creation(tmp_path):
    revision, aws = setup_revision(tmp_path)
    revision.policy()
    actual_update = aws.update_agent_runtime

    def unexpected(**kwargs):
        return {**actual_update(**kwargs), "agentRuntimeVersion": "3"}

    aws.update_agent_runtime = unexpected
    with pytest.raises(RuntimeError, match="unexpected"):
        revision.runtime()
    assert not any(name == "create_agent_runtime_endpoint" for name, _ in aws.calls)
