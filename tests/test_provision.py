"""Exercise account, artifact, deadline and uncertain-outcome guards without cloud access."""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from deployment.agentcore_spec import RuntimeSpec
from deployment.provision import Provisioner


class Session:
    def __init__(self, account="123456789012", failure=None):
        self.account, self.failure, self.calls = account, failure, []

    def client(self, name, **_kwargs):
        self.calls.append(("client", name))
        return self

    def get_caller_identity(self):
        self.calls.append(("get_caller_identity", {}))
        return {"Account": self.account}

    def get_account_plan_state(self):
        self.calls.append(("get_account_plan_state", {}))
        return {"accountId": self.account, "accountPlanType": "FREE", "accountPlanStatus": "ACTIVE"}

    def create_role(self, **kwargs):
        self.calls.append(("create_role", kwargs))
        raise self.failure or TimeoutError("Simulated response was lost after request submission.")

    def put_role_policy(self, **kwargs):
        self.calls.append(("put_role_policy", kwargs))
        raise AssertionError("A failed role creation must not continue to policy mutation.")


def provisioner(tmp_path, session, expired=False):
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"artifact fixture")
    spec = RuntimeSpec(
        "123456789012",
        "us-east-1",
        "watershed_memory_test",
        "amazon.nova-pro-v1:0",
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
    )
    deadline = datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)
    return Provisioner(spec, artifact, tmp_path / "journal.jsonl", deadline, session=session)


def test_wrong_account_never_performs_any_mutation(tmp_path):
    session = Session(account="999999999999")
    runner = provisioner(tmp_path, session)
    with pytest.raises(RuntimeError, match="identity"):
        runner.preflight()
    with pytest.raises(RuntimeError, match="verified"):
        runner.role()
    assert all(name not in ("create_role", "put_role_policy") for name, _ in session.calls)


def test_artifact_tampering_is_rejected_before_aws_clients_are_created(tmp_path):
    session = Session()
    runner = provisioner(tmp_path, session)
    runner.artifact.write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256"):
        Provisioner(runner.spec, runner.artifact, runner.journal, runner.deadline, session=session)
    assert session.calls == []


def test_expired_work_window_blocks_mutation_after_successful_preflight(tmp_path):
    session = Session()
    runner = provisioner(tmp_path, session, expired=True)
    runner.preflight()
    with pytest.raises(RuntimeError, match="work window"):
        runner.role()
    assert all(name != "create_role" for name, _ in session.calls)


@pytest.mark.parametrize("status", [0, 403, 503])
def test_failed_mutation_is_journaled_without_blind_retry(tmp_path, status):
    error = ClientError(
        {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "CreateRole",
    )
    session = Session(failure=error if status else TimeoutError())
    runner = provisioner(tmp_path, session)
    runner.preflight()
    with pytest.raises((ClientError, TimeoutError)):
        runner.role()
    rows = [json.loads(line) for line in runner.journal.read_text().splitlines()]
    mutations = [row for row in rows if row["mutation"]]
    assert mutations[0]["status"] == "STARTED"
    assert mutations[-1]["status"] == ("FAILED" if status == 403 else "OUTCOME_UNKNOWN")
    assert len([name for name, _ in session.calls if name == "create_role"]) == 1
    assert all(name != "put_role_policy" for name, _ in session.calls)


class CompleteAWS:
    """SDK-shaped offline account with a newly created, locked-down bucket."""

    def __init__(self, spec, plan, fault=None):
        self.spec, self.plan, self.fault = spec, plan, fault
        self.calls = []
        self.created_bucket = False
        self.runtime_version = "1"

    def client(self, name, **_kwargs):
        return self

    def _call(self, name, kwargs):
        self.calls.append((name, kwargs))

    def get_caller_identity(self):
        self._call("get_caller_identity", {})
        return {"Account": self.spec.account}

    def get_account_plan_state(self):
        self._call("get_account_plan_state", {})
        return {
            "accountId": self.spec.account,
            "accountPlanType": "FREE",
            "accountPlanStatus": "ACTIVE",
        }

    def create_role(self, **kwargs):
        self._call("create_role", kwargs)
        return {"Role": {"Arn": self.plan["create_runtime"]["roleArn"]}}

    def put_role_policy(self, **kwargs):
        self._call("put_role_policy", kwargs)
        return {}

    def get_role(self, **kwargs):
        self._call("get_role", kwargs)
        role = {
            "Arn": self.plan["create_runtime"]["roleArn"],
            "AssumeRolePolicyDocument": self.plan["trust_policy"],
            "Tags": [{"Key": k, "Value": v} for k, v in self.plan["tags"].items()],
        }
        if self.fault == "role_tags":
            role["Tags"] = []
        if self.fault == "role_trust":
            role["AssumeRolePolicyDocument"] = {}
        if self.fault == "role_boundary":
            role["PermissionsBoundary"] = {"PermissionsBoundaryArn": "bad"}
        return {"Role": role}

    def list_role_policies(self, **kwargs):
        self._call("list_role_policies", kwargs)
        return {
            "IsTruncated": self.fault == "inline_truncated",
            "PolicyNames": ["WatershedRuntimeProof", "extra"]
            if self.fault == "inline_extra"
            else ["WatershedRuntimeProof"],
        }

    def list_attached_role_policies(self, **kwargs):
        self._call("list_attached_role_policies", kwargs)
        return {
            "IsTruncated": False,
            "AttachedPolicies": (
                [{"PolicyName": "extra"}] if self.fault == "managed_policy" else []
            ),
        }

    def get_role_policy(self, **kwargs):
        self._call("get_role_policy", kwargs)
        return {"PolicyDocument": self.plan["execution_policy"]}

    def head_bucket(self, **kwargs):
        self._call("head_bucket", kwargs)
        if self.fault in {
            "bucket_acl",
            "bucket_policy",
            "bucket_ownership",
            "bucket_publicblock",
            "bucket_encryption",
            "existing_safe",
        }:
            return {}
        raise ClientError(
            {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadBucket"
        )

    def create_bucket(self, **kwargs):
        self._call("create_bucket", kwargs)
        self.created_bucket = True
        return {}

    def put_bucket_tagging(self, **kwargs):
        self._call("put_bucket_tagging", kwargs)
        return {}

    def get_bucket_tagging(self, **kwargs):
        self._call("get_bucket_tagging", kwargs)
        return {"TagSet": [{"Key": k, "Value": v} for k, v in self.plan["tags"].items()]}

    def put_public_access_block(self, **kwargs):
        self._call("put_public_access_block", kwargs)
        return {}

    def put_bucket_encryption(self, **kwargs):
        self._call("put_bucket_encryption", kwargs)
        return {}

    def get_bucket_ownership_controls(self, **kwargs):
        self._call("get_bucket_ownership_controls", kwargs)
        return {
            "OwnershipControls": {
                "Rules": [
                    {
                        "ObjectOwnership": "BucketOwnerPreferred"
                        if self.fault == "bucket_ownership"
                        else "BucketOwnerEnforced"
                    }
                ]
            }
        }

    def get_public_access_block(self, **kwargs):
        self._call("get_public_access_block", kwargs)
        return {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": False if self.fault == "bucket_publicblock" else True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        }

    def get_bucket_encryption(self, **kwargs):
        self._call("get_bucket_encryption", kwargs)
        return {
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms"
                            if self.fault == "bucket_encryption"
                            else "AES256"
                        },
                        "BlockedEncryptionTypes": {"EncryptionType": ["SSE-C"]},
                        "BucketKeyEnabled": False,
                    }
                ]
            }
        }

    def get_bucket_acl(self, **kwargs):
        self._call("get_bucket_acl", kwargs)
        owner = "canonical-owner"
        grants = (
            []
            if self.fault == "bucket_acl"
            else [{"Permission": "FULL_CONTROL", "Grantee": {"Type": "CanonicalUser", "ID": owner}}]
        )
        return {"Owner": {"ID": owner}, "Grants": grants}

    def get_bucket_policy(self, **kwargs):
        self._call("get_bucket_policy", kwargs)
        if self.fault == "bucket_policy":
            return {"Policy": "bad"}
        raise ClientError({"Error": {"Code": "NoSuchBucketPolicy"}}, "GetBucketPolicy")

    def put_object(self, **kwargs):
        self._call("put_object", kwargs)
        return {"ETag": "fixture"}

    def head_object(self, **kwargs):
        self._call("head_object", kwargs)
        return {
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(self.spec.artifact_sha256)).decode()
        }

    def create_agent_runtime(self, **kwargs):
        self._call("create_agent_runtime", kwargs)
        return {"agentRuntimeId": self.spec.name + "-abc123", "agentRuntimeVersion": "1"}

    def get_agent_runtime(self, **kwargs):
        self._call("get_agent_runtime", kwargs)
        rid = kwargs["agentRuntimeId"]
        result = {
            "agentRuntimeId": rid,
            "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{self.spec.region}:{self.spec.account}:runtime/{rid}",
            "agentRuntimeName": self.spec.name,
            "status": "READY",
            "agentRuntimeVersion": self.runtime_version,
        }
        result.update(self.plan["create_runtime"])
        result["metadataConfiguration"] = self.plan["runtime_metadata"]
        if self.fault == "runtime_code":
            result["agentRuntimeArtifact"] = {}
        if self.fault == "runtime_role":
            result["roleArn"] = "bad"
        if self.fault == "runtime_network":
            result["networkConfiguration"] = {"networkMode": "PRIVATE"}
        if self.fault == "runtime_lifecycle":
            result["lifecycleConfiguration"] = {}
        if self.fault == "runtime_protocol":
            result["protocolConfiguration"] = {}
        if self.fault == "runtime_env":
            result["environmentVariables"] = {}
        if self.fault == "runtime_authorizer":
            result["authorizerConfiguration"] = {"custom": "bad"}
        if self.fault == "runtime_filesystem":
            result["filesystemConfigurations"] = [{"bad": True}]
        if self.fault == "runtime_metadata":
            result["metadataConfiguration"] = {"requireMMDSV2": False}
        return result

    def create_agent_runtime_endpoint(self, **kwargs):
        self._call("create_agent_runtime_endpoint", kwargs)
        arn = f"arn:aws:bedrock-agentcore:{self.spec.region}:{self.spec.account}:runtime/{kwargs['agentRuntimeId']}"
        if self.fault == "endpoint_identity":
            arn += "-wrong"
        version = "2" if self.fault == "endpoint_version" else kwargs["agentRuntimeVersion"]
        return {
            "agentRuntimeId": kwargs["agentRuntimeId"],
            "agentRuntimeArn": arn,
            "agentRuntimeEndpointArn": arn + "/runtime-endpoint/proof_v1",
            "endpointName": "proof_v1",
            "targetVersion": version,
        }

    def update_agent_runtime(self, **kwargs):
        self._call("update_agent_runtime", kwargs)
        from boto3 import Session
        from botocore.validate import validate_parameters
        model = Session()._session.get_service_model("bedrock-agentcore-control")
        validate_parameters(kwargs, model.operation_model("UpdateAgentRuntime").input_shape)
        self.fault = None
        self.runtime_version = "2"
        return {"agentRuntimeId": kwargs["agentRuntimeId"], "agentRuntimeVersion": "2",
                "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{self.spec.region}:{self.spec.account}:runtime/{kwargs['agentRuntimeId']}"}


def complete_runner(tmp_path, fault=None):
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"artifact fixture")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    spec = RuntimeSpec(
        "123456789012", "us-east-1", "watershed_memory_test", "amazon.nova-pro-v1:0", digest
    )
    from deployment.agentcore_spec import build_plan

    plan = build_plan(spec)
    aws = CompleteAWS(spec, plan, fault)
    runner = Provisioner(
        spec,
        artifact,
        tmp_path / "journal.jsonl",
        datetime.now(timezone.utc) + timedelta(hours=1),
        session=aws,
    )
    return runner, aws


def test_complete_offline_provision_path(tmp_path):
    runner, aws = complete_runner(tmp_path)
    runner.preflight()
    assert runner.role()["role_arn"].endswith("watershed_memory_test_execution")
    runner.upload()
    runtime = runner.runtime()
    endpoint = runner.endpoint(runtime["agentRuntimeId"], "1")
    assert endpoint["endpointName"] == "proof_v1"
    assert any(name == "create_agent_runtime" for name, _ in aws.calls)


def test_metadata_phase_is_explicit_sdk_valid_and_skips_an_already_enabled_runtime(tmp_path):
    runner, aws = complete_runner(tmp_path, "runtime_metadata")
    runner.preflight()
    runtime_id = runner.spec.name + "-abc123"
    with pytest.raises(RuntimeError, match="MMDSv2"):
        runner.inspect(runtime_id)
    result = runner.metadata(runtime_id)
    assert result["agentRuntimeVersion"] == "2"
    updates = [arguments for name, arguments in aws.calls if name == "update_agent_runtime"]
    assert len(updates) == 1
    assert updates[0]["metadataConfiguration"] == {"requireMMDSV2": True}
    assert updates[0]["agentRuntimeArtifact"] == runner.plan["create_runtime"]["agentRuntimeArtifact"]
    with pytest.raises(RuntimeError, match="intended version"):
        runner.endpoint(runtime_id, "1")
    assert not any(name == "create_agent_runtime_endpoint" for name, _ in aws.calls)
    assert runner.endpoint(runtime_id, result["agentRuntimeVersion"])["targetVersion"] == "2"
    assert runner.metadata(runtime_id)["status"] == "ALREADY_ENABLED"
    assert len([name for name, _ in aws.calls if name == "update_agent_runtime"]) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "managed_policy",
        "inline_extra",
        "inline_truncated",
        "role_tags",
        "role_trust",
        "role_boundary",
        "bucket_acl",
        "bucket_policy",
        "bucket_ownership",
        "bucket_publicblock",
        "bucket_encryption",
        "runtime_code",
        "runtime_role",
        "runtime_network",
        "runtime_lifecycle",
        "runtime_protocol",
        "runtime_env",
        "runtime_authorizer",
        "runtime_filesystem",
        "runtime_metadata",
        "endpoint_identity",
        "endpoint_version",
    ],
)
def test_offline_drift_is_rejected_before_next_mutation(tmp_path, fault):
    runner, aws = complete_runner(tmp_path, fault)
    runner.preflight()
    if fault.startswith("role_") or fault in {"managed_policy", "inline_extra", "inline_truncated"}:
        with pytest.raises(RuntimeError):
            runner.runtime()
        assert not any(name == "create_agent_runtime" for name, _ in aws.calls)
    elif fault.startswith("bucket_"):
        with pytest.raises(RuntimeError):
            runner.upload()
        assert not any(
            name
            in {
                "put_object",
                "put_bucket_tagging",
                "put_public_access_block",
                "put_bucket_encryption",
            }
            for name, _ in aws.calls
        )
    elif fault.startswith("runtime_"):
        runner.role()
        runner.upload()
        runtime = runner.runtime()
        with pytest.raises(RuntimeError):
            runner.inspect(runtime["agentRuntimeId"])
    else:
        runner.role()
        runner.upload()
        runtime = runner.runtime()
        with pytest.raises(RuntimeError):
            runner.endpoint(runtime["agentRuntimeId"], "1")
