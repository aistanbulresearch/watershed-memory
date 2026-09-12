"""Publish a reviewed code revision without repointing an earlier proof endpoint."""

import argparse
import base64
import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .agentcore_spec import RuntimeSpec
from .provision import Provisioner, encoded


class RuntimeRevision:
    """Explicit upload, policy, Runtime and endpoint phases with previous-state checks."""

    def __init__(self, previous: Provisioner, updated: Provisioner,
                 runtime_id: str, previous_version: str):
        old_spec, new_spec = asdict(previous.spec), asdict(updated.spec)
        if (any(old_spec[key] != new_spec[key] for key in old_spec if key != "artifact_sha256")
                or old_spec["artifact_sha256"] == new_spec["artifact_sha256"]
                or not re.fullmatch(r"[1-9][0-9]*", previous_version)
                or not runtime_id.startswith(previous.spec.name + "-")):
            raise ValueError("A revision must change only the code artifact of this named Runtime.")
        self.previous, self.updated = previous, updated
        self.runtime_id, self.previous_version = runtime_id, previous_version
        # Preserve the earlier version's exact object permission for cold starts.
        old_read = next(row for row in previous.plan["execution_policy"]["Statement"]
                        if row["Sid"] == "ReadArtifact")
        new_read = next(row for row in updated.plan["execution_policy"]["Statement"]
                        if row["Sid"] == "ReadArtifact")
        new_read["Resource"] = sorted(set(old_read["Resource"] + new_read["Resource"]))

    def _previous_ready(self) -> dict:
        result = self.previous.inspect(self.runtime_id)
        if result["status"] != "READY" or result["agentRuntimeVersion"] != self.previous_version:
            raise RuntimeError("The previous Runtime version changed; reconcile before mutation.")
        return result

    def _artifact_ready(self) -> None:
        self.updated.verify_bucket()
        response = self.updated.call("s3", "head_object", Bucket=self.updated.plan["bucket"],
            Key=self.updated.plan["object_key"], ExpectedBucketOwner=self.updated.spec.account,
            ChecksumMode="ENABLED")
        if response.get("ChecksumSHA256") != base64.b64encode(
                bytes.fromhex(self.updated.spec.artifact_sha256)).decode():
            raise RuntimeError("The new uploaded artifact differs from the reviewed revision.")

    def policy(self) -> dict:
        self._previous_ready()
        self.previous.verify_role()
        self._artifact_ready()
        return self.updated.call("iam", "put_role_policy", mutation=True,
            RoleName=self.updated.plan["role_name"], PolicyName="WatershedRuntimeProof",
            PolicyDocument=encoded(self.updated.plan["execution_policy"]))

    def runtime(self) -> dict:
        previous = self._previous_ready()
        self.updated.verify_role()
        self._artifact_ready()
        config = {key: value for key, value in self.updated.plan["create_runtime"].items()
                  if key not in ("agentRuntimeName", "tags", "clientToken")}
        config.update(agentRuntimeId=self.runtime_id,
            metadataConfiguration=self.updated.plan["runtime_metadata"],
            clientToken=str(uuid.uuid5(uuid.NAMESPACE_URL,
                self.updated.plan["create_runtime"]["clientToken"] + ":source-revision")))
        result = self.updated.call("bedrock-agentcore-control", "update_agent_runtime", mutation=True,
                                   **config)
        version = result.get("agentRuntimeVersion", "")
        if (result.get("agentRuntimeId") != self.runtime_id
                or result.get("agentRuntimeArn") != previous["agentRuntimeArn"]
                or not re.fullmatch(r"[1-9][0-9]*", version)
                or int(version) != int(self.previous_version) + 1):
            raise RuntimeError("Runtime revision response is unexpected; reconcile before continuing.")
        return result

    def endpoint(self, target_version: str) -> dict:
        if (not re.fullmatch(r"[1-9][0-9]*", target_version)
                or int(target_version) != int(self.previous_version) + 1):
            raise ValueError("Select the newly returned Runtime version explicitly.")
        runtime = self.updated.inspect(self.runtime_id)
        if runtime["status"] != "READY" or runtime["agentRuntimeVersion"] != target_version:
            raise RuntimeError("The updated Runtime is not ready at the requested version.")
        self.updated.verify_role()
        self._artifact_ready()
        name = f"proof_v{target_version}"
        result = self.updated.call("bedrock-agentcore-control", "create_agent_runtime_endpoint",
            mutation=True, agentRuntimeId=self.runtime_id, name=name,
            agentRuntimeVersion=target_version, tags=self.updated.plan["tags"],
            clientToken=str(uuid.uuid5(uuid.NAMESPACE_URL,
                self.updated.plan["create_runtime"]["clientToken"] + ":" + name)))
        expected = {"agentRuntimeId": self.runtime_id, "agentRuntimeArn": runtime["agentRuntimeArn"],
                    "agentRuntimeEndpointArn": runtime["agentRuntimeArn"] + "/runtime-endpoint/" + name,
                    "endpointName": name, "targetVersion": target_version}
        if any(result.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Endpoint revision response is unexpected; reconcile before continuing.")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("upload", "policy", "runtime", "endpoint", "inspect", "render"))
    for label in ("previous-spec", "previous-artifact", "updated-spec", "updated-artifact"):
        parser.add_argument(f"--{label}", type=Path, required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--previous-version", required=True)
    parser.add_argument("--target-version")
    parser.add_argument("--deadline", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "endpoint" and not args.target_version:
        parser.error("Endpoint creation requires an explicit target version.")
    deadline = datetime.fromisoformat(args.deadline)
    previous = Provisioner(RuntimeSpec(**json.loads(args.previous_spec.read_text(encoding="utf-8-sig"))),
        args.previous_artifact, args.journal, deadline, args.profile)
    updated = Provisioner(RuntimeSpec(**json.loads(args.updated_spec.read_text(encoding="utf-8-sig"))),
        args.updated_artifact, args.journal, deadline, session=previous.session)
    revision = RuntimeRevision(previous, updated, args.runtime_id, args.previous_version)
    if args.action == "render":
        print(json.dumps(updated.plan, indent=2))
        return
    updated.preflight()
    if args.action == "endpoint":
        result = revision.endpoint(args.target_version)
    elif args.action == "inspect":
        result = updated.inspect(args.runtime_id)
    elif args.action == "upload":
        result = updated.upload()
    else:
        result = getattr(revision, args.action)()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
