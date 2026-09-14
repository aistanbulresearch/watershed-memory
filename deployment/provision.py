"""Apply one reviewed AgentCore proof phase and journal every AWS operation.

Each phase is explicit. Failures are recorded; this command never blindly retries,
automatically upgrades the AWS plan, deploys unrelated services or deletes resources.
"""

import argparse
import base64
import hashlib
import json
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .agentcore_spec import RuntimeSpec, build_current_plan, build_plan


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def selected_plan(spec: RuntimeSpec, runtime_mode: str) -> dict:
    if type(runtime_mode) is not str or runtime_mode not in ("historical", "current-v3"):
        raise ValueError("Select the historical or current-v3 runtime mode explicitly.")
    if runtime_mode == "current-v3":
        if not spec.name.startswith("watershed_memory_current_"):
            raise ValueError("Current runtime requires its separate resource namespace.")
        return build_current_plan(spec)
    return build_plan(spec)


class Provisioner:
    def __init__(self, spec: RuntimeSpec, artifact: Path, journal: Path, deadline: datetime,
                 profile: str | None = None, session=None, *, runtime_mode: str = "historical"):
        plan = selected_plan(spec, runtime_mode)
        if runtime_mode == "current-v3":
            try:
                with zipfile.ZipFile(artifact) as archive:
                    names = archive.namelist()
                required = {"runtime/current_launch.py", "runtime/current_entrypoint.py"}
                if (not required <= set(names) or len(names) != len(set(names))
                        or {"runtime/launch.py", "runtime/entrypoint.py"} & set(names)):
                    raise ValueError("Current artifact must contain the fixed current launch pair.")
            except zipfile.BadZipFile as error:
                raise ValueError("Current runtime requires the reviewed ZIP artifact.") from error
        self.runtime_mode = runtime_mode
        self.spec = spec
        self.plan = plan
        self.artifact, self.journal, self.deadline = artifact, journal, deadline
        if deadline.tzinfo is None:
            raise ValueError("Use an explicit timezone for the provisioning deadline.")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != spec.artifact_sha256:
            raise ValueError("Artifact does not match the reviewed SHA-256.")
        self.session = session or boto3.Session(profile_name=profile, region_name=spec.region)
        self.clients = {}
        self.verified = False

    def call(self, service: str, operation: str, *, mutation: bool = False, **kwargs):
        if mutation and (not self.verified or datetime.now(timezone.utc) >= self.deadline):
            raise RuntimeError("Identity/plan must be verified and the work window must be open.")
        if service not in self.clients:
            self.clients[service] = self.session.client(service, config=Config(
                connect_timeout=5, read_timeout=120 if service == "s3" else 30,
                retries={"max_attempts": 0}))
        entry = {"at": datetime.now(timezone.utc).isoformat(), "service": service,
                 "operation": operation, "mutation": mutation,
                 "arguments_sha256": hashlib.sha256(encoded({k: v for k, v in kwargs.items()
                                                             if k != "Body"}).encode()).hexdigest()}
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.record({**entry, "status": "STARTED"})
        try:
            response = getattr(self.clients[service], operation)(**kwargs)
        except Exception as error:
            details = error.response if isinstance(error, ClientError) else {}
            status = details.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            self.record({**entry, "status": "FAILED" if 400 <= status < 500 else "OUTCOME_UNKNOWN",
                         "error_code": details.get("Error", {}).get("Code", type(error).__name__),
                         "aws_request_id": details.get("ResponseMetadata", {}).get("RequestId")})
            raise
        self.record({**entry, "status": "RETURNED", "response": response})
        return response

    def record(self, entry: dict) -> None:
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(encoded(entry) + "\n")
            handle.flush()

    def preflight(self) -> dict:
        self.verified = False
        identity = self.call("sts", "get_caller_identity")
        if identity["Account"] != self.spec.account:
            raise RuntimeError("AWS identity does not match the reviewed account.")
        account = self.call("freetier", "get_account_plan_state")
        if (account.get("accountId") != self.spec.account or account.get("accountPlanType") != "FREE"
                or account.get("accountPlanStatus") != "ACTIVE"):
            raise RuntimeError("This bounded proof expects the active Free account plan.")
        self.verified = True
        return account

    def role(self) -> dict:
        created = self.call("iam", "create_role", mutation=True, RoleName=self.plan["role_name"],
            AssumeRolePolicyDocument=encoded(self.plan["trust_policy"]),
            Description="Watershed Memory bounded Runtime proof",
            Tags=[{"Key": k, "Value": v} for k, v in self.plan["tags"].items()])
        self.call("iam", "put_role_policy", mutation=True, RoleName=self.plan["role_name"],
            PolicyName="WatershedRuntimeProof", PolicyDocument=encoded(self.plan["execution_policy"]))
        return {"role_arn": created["Role"]["Arn"]}

    def upload(self) -> dict:
        bucket, account = self.plan["bucket"], self.spec.account
        created = False
        try:
            self.call("s3", "head_bucket", Bucket=bucket, ExpectedBucketOwner=account)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            options = {} if self.spec.region == "us-east-1" else {
                "CreateBucketConfiguration": {"LocationConstraint": self.spec.region}}
            self.call("s3", "create_bucket", mutation=True, Bucket=bucket,
                      ObjectOwnership="BucketOwnerEnforced", **options)
            created = True
            self.call("s3", "put_bucket_tagging", mutation=True, Bucket=bucket,
                ExpectedBucketOwner=account,
                Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in self.plan["tags"].items()]})
        tags = self.call("s3", "get_bucket_tagging", Bucket=bucket, ExpectedBucketOwner=account)
        if not self.plan["tags"].items() <= {t["Key"]: t["Value"] for t in tags["TagSet"]}.items():
            raise RuntimeError("Existing bucket is not tagged for this proof; do not adopt it.")
        if created:
            self.call("s3", "put_public_access_block", mutation=True, Bucket=bucket,
                ExpectedBucketOwner=account, PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True, "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
            self.call("s3", "put_bucket_encryption", mutation=True, Bucket=bucket,
                ExpectedBucketOwner=account, ServerSideEncryptionConfiguration={"Rules": [{
                    "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                    "BlockedEncryptionTypes": {"EncryptionType": ["SSE-C"]}}]})
        self.verify_bucket()
        content = self.artifact.read_bytes()
        if hashlib.sha256(content).hexdigest() != self.spec.artifact_sha256:
            raise ValueError("Artifact changed after preflight.")
        return self.call("s3", "put_object", mutation=True, Bucket=bucket,
            Key=self.plan["object_key"], Body=content, ExpectedBucketOwner=account,
            ServerSideEncryption="AES256", ContentType="application/zip",
            ChecksumSHA256=base64.b64encode(bytes.fromhex(self.spec.artifact_sha256)).decode(),
            Metadata={"sha256": self.spec.artifact_sha256})

    def verify_bucket(self) -> None:
        options = {"Bucket": self.plan["bucket"], "ExpectedBucketOwner": self.spec.account}
        ownership = self.call("s3", "get_bucket_ownership_controls", **options)
        block = self.call("s3", "get_public_access_block", **options)
        encryption = self.call("s3", "get_bucket_encryption", **options)
        encryption_rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        blocked_types_ok = all(rule.get("BlockedEncryptionTypes") in (
            None, {"EncryptionType": ["SSE-C"]}) for rule in encryption_rules)
        normalized_rules = [{key: value for key, value in rule.items()
                             if key != "BlockedEncryptionTypes"} for rule in encryption_rules]
        encryption_ok = blocked_types_ok and len(normalized_rules) == 1 and normalized_rules[0] in (
            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}},
            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}, "BucketKeyEnabled": False},
        )
        acl = self.call("s3", "get_bucket_acl", **options)
        owner = acl.get("Owner", {}).get("ID")
        if (ownership.get("OwnershipControls", {}).get("Rules") != [
                {"ObjectOwnership": "BucketOwnerEnforced"}]
                or block.get("PublicAccessBlockConfiguration") != {
                    "BlockPublicAcls": True, "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True, "RestrictPublicBuckets": True}
                or not encryption_ok
                or not owner or len(acl.get("Grants", [])) != 1
                or acl["Grants"][0].get("Permission") != "FULL_CONTROL"
                or acl["Grants"][0].get("Grantee", {}).get("Type") != "CanonicalUser"
                or acl["Grants"][0]["Grantee"].get("ID") != owner):
            raise RuntimeError("Bucket privacy controls differ from this proof; do not adopt it.")
        try:
            self.call("s3", "get_bucket_policy", **options)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "NoSuchBucketPolicy":
                raise
        else:
            raise RuntimeError("Bucket has an unexpected access policy; do not adopt it.")

    def verify_role(self) -> None:
        role = self.call("iam", "get_role", RoleName=self.plan["role_name"])["Role"]
        inline = self.call("iam", "list_role_policies", RoleName=self.plan["role_name"])
        attached = self.call("iam", "list_attached_role_policies", RoleName=self.plan["role_name"])
        policy = self.call("iam", "get_role_policy", RoleName=self.plan["role_name"],
                           PolicyName="WatershedRuntimeProof")["PolicyDocument"]
        if (role["Arn"] != self.plan["create_runtime"]["roleArn"]
                or role["AssumeRolePolicyDocument"] != self.plan["trust_policy"]
                or {t["Key"]: t["Value"] for t in role.get("Tags", [])} != self.plan["tags"]
                or "PermissionsBoundary" in role
                or inline.get("IsTruncated") or attached.get("IsTruncated")
                or inline.get("PolicyNames") != ["WatershedRuntimeProof"]
                or attached.get("AttachedPolicies") != []
                or policy != self.plan["execution_policy"]):
            raise RuntimeError("Execution role differs from the reviewed policy.")

    def runtime(self) -> dict:
        self.verify_role()
        self.verify_bucket()
        uploaded = self.call("s3", "head_object", Bucket=self.plan["bucket"],
            Key=self.plan["object_key"], ExpectedBucketOwner=self.spec.account, ChecksumMode="ENABLED")
        checksum = base64.b64encode(bytes.fromhex(self.spec.artifact_sha256)).decode()
        if uploaded.get("ChecksumSHA256") != checksum:
            raise RuntimeError("Uploaded artifact checksum differs from the reviewed build.")
        return self.call("bedrock-agentcore-control", "create_agent_runtime", mutation=True,
                         **self.plan["create_runtime"])

    def endpoint(self, runtime_id: str, runtime_version: str) -> dict:
        runtime = self.inspect(runtime_id)
        if runtime["status"] != "READY" or runtime["agentRuntimeVersion"] != runtime_version:
            raise RuntimeError("Runtime is not ready at the intended version.")
        self.verify_role()
        self.verify_bucket()
        endpoint_name = "current_v3" if self.runtime_mode == "current-v3" else "proof_v1"
        result = self.call("bedrock-agentcore-control", "create_agent_runtime_endpoint", mutation=True,
            agentRuntimeId=runtime_id, name=endpoint_name, agentRuntimeVersion=runtime_version,
            clientToken=self.plan["create_runtime"]["clientToken"], tags=self.plan["tags"])
        expected = {"agentRuntimeId": runtime_id, "agentRuntimeArn": runtime["agentRuntimeArn"],
                    "agentRuntimeEndpointArn": runtime["agentRuntimeArn"] + "/runtime-endpoint/" + endpoint_name,
                    "endpointName": endpoint_name, "targetVersion": runtime_version}
        if any(result.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Endpoint response differs from this proof; reconcile before continuing.")
        return result

    def metadata(self, runtime_id: str) -> dict:
        runtime = self.inspect(runtime_id, require_mmdsv2=False)
        if runtime.get("metadataConfiguration") == self.plan["runtime_metadata"]:
            return {"status": "ALREADY_ENABLED", "agentRuntimeVersion": runtime["agentRuntimeVersion"]}
        if runtime["status"] != "READY":
            raise RuntimeError("Runtime must be ready before the explicit metadata update.")
        self.verify_role()
        self.verify_bucket()
        config = {key: value for key, value in self.plan["create_runtime"].items()
                  if key not in ("agentRuntimeName", "tags", "clientToken")}
        config.update(agentRuntimeId=runtime_id, metadataConfiguration=self.plan["runtime_metadata"],
            clientToken=str(uuid.uuid5(uuid.NAMESPACE_URL,
                self.plan["create_runtime"]["clientToken"] + ":mmdsv2")))
        result = self.call("bedrock-agentcore-control", "update_agent_runtime", mutation=True, **config)
        version = result.get("agentRuntimeVersion", "")
        if (result.get("agentRuntimeId") != runtime_id
                or result.get("agentRuntimeArn") != runtime["agentRuntimeArn"]
                or not re.fullmatch(r"[1-9][0-9]*", version)
                or int(version) <= int(runtime["agentRuntimeVersion"])):
            raise RuntimeError("Metadata update response differs from this proof; reconcile before continuing.")
        return result

    def inspect(self, runtime_id: str, *, require_mmdsv2: bool = True) -> dict:
        if not runtime_id.startswith(self.spec.name + "-"):
            raise ValueError("Runtime identity is outside this named proof.")
        result = self.call("bedrock-agentcore-control", "get_agent_runtime", agentRuntimeId=runtime_id)
        expected = f"arn:aws:bedrock-agentcore:{self.spec.region}:{self.spec.account}:runtime/{runtime_id}"
        if (result.get("agentRuntimeArn") != expected or result.get("agentRuntimeId") != runtime_id
                or result.get("agentRuntimeName") != self.spec.name):
            raise RuntimeError("Returned Runtime identity differs from this proof.")
        fields = ("roleArn", "networkConfiguration", "lifecycleConfiguration", "agentRuntimeArtifact",
                  "protocolConfiguration", "environmentVariables", "description")
        if (any(result.get(key) != self.plan["create_runtime"][key] for key in fields)
                or any(result.get(key) for key in ("authorizerConfiguration", "requestHeaderConfiguration",
                    "filesystemConfigurations", "capacityProviderConfiguration"))):
            raise RuntimeError("Runtime configuration differs from the reviewed deployment.")
        metadata = result.get("metadataConfiguration")
        accepted = (self.plan["runtime_metadata"],) if require_mmdsv2 else (
            self.plan["runtime_metadata"], {"requireMMDSV2": False}, {}, None)
        if metadata not in accepted:
            raise RuntimeError("Runtime MMDSv2 configuration differs from the reviewed deployment.")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("render", "role", "upload", "runtime", "metadata", "endpoint", "inspect"))
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--journal", type=Path, default=Path(".local/runtime/provision.jsonl"))
    parser.add_argument("--deadline", required=True, help="ISO8601 UTC work-window deadline")
    parser.add_argument("--profile")
    parser.add_argument("--runtime-mode", choices=("historical", "current-v3"), default="historical")
    parser.add_argument("--runtime-id", default="")
    parser.add_argument("--runtime-version", help="Explicit version returned by creation or metadata update")
    args = parser.parse_args()
    if args.action == "endpoint" and not args.runtime_version:
        parser.error("Endpoint creation requires the explicit verified --runtime-version.")
    spec = RuntimeSpec(**json.loads(args.spec.read_text(encoding="utf-8-sig")))
    if args.action == "render":
        plan = selected_plan(spec, args.runtime_mode)
        print(json.dumps(plan, indent=2))
        return
    provisioner = Provisioner(spec, args.artifact, args.journal,
        datetime.fromisoformat(args.deadline), args.profile, runtime_mode=args.runtime_mode)
    provisioner.preflight()
    if args.action == "endpoint":
        result = provisioner.endpoint(args.runtime_id, args.runtime_version)
    elif args.action in ("inspect", "metadata"):
        result = getattr(provisioner, args.action)(args.runtime_id)
    else:
        result = getattr(provisioner, args.action)()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
