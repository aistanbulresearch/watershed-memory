"""Protected AgentCore planner transport with external validation and bounded cleanup."""

import json
import re
import uuid
from importlib.metadata import version
from typing import Any

from .agentcore_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    AgentCoreMetadata,
    AgentCoreResponse,
    ObservedModelOutput,
    make_request,
    validate_response,
    validate_wire_size,
)
from .planning import Plan
from .strands_agent import INSTRUCTION_VERSION


class AgentCoreTurnError(RuntimeError):
    """Transport outcome and cleanup evidence without arbitrary remote exception text."""

    def __init__(self, evidence: dict):
        super().__init__(f"AgentCore turn failed ({evidence['error_code']}).")
        self.evidence = evidence


def error_code(error: Exception) -> str:
    response = getattr(error, "response", {})
    code = response.get("Error", {}).get("Code", type(error).__name__)
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", code) else "RemoteError"


class AgentCoreClient:
    def __init__(self, runtime_arn: str, qualifier: str, region: str, *,
                 expected_account: str, expected_version: str, model_id: str,
                 profile: str | None = None, boto_client: Any = None,
                 control_client: Any = None, cleanup_client: Any = None,
                 scripted_test: bool = False):
        match = re.fullmatch(r"arn:aws:bedrock-agentcore:([a-z0-9-]+):([0-9]{12}):runtime/"
                             r"([A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]+)", runtime_arn)
        if not match or match[1] != region or match[2] != expected_account:
            raise ValueError("Runtime ARN must match the configured account and region.")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", qualifier) or qualifier == "DEFAULT":
            raise ValueError("Use an explicitly named and version-pinned Runtime endpoint.")
        if not re.fullmatch(r"[1-9][0-9]*", expected_version) or not model_id:
            raise ValueError("Expected Runtime version and model are required.")
        self.runtime_arn, self.runtime_id = runtime_arn, match[3]
        self.qualifier, self.expected_version = qualifier, expected_version
        self.model_id, self.scripted_test = model_id, scripted_test
        self.mode = {
            "id": "scripted_agentcore_test" if scripted_test else "agentcore_historical_replay",
            "label": "Scripted AgentCore transport test" if scripted_test else
                     "Historical replay · Strands on AgentCore",
            "agent_enabled": not scripted_test,
        }
        if boto_client is None:
            import boto3
            from botocore.config import Config
            session = boto3.Session(profile_name=profile, region_name=region)
            self.boto = session.client("bedrock-agentcore", config=Config(
                connect_timeout=5, read_timeout=145, retries={"max_attempts": 0}))
            short = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 0})
            self.control = session.client("bedrock-agentcore-control", config=short)
            self.cleanup = session.client("bedrock-agentcore", config=short)
        else:
            if control_client is None:
                raise ValueError("An injected transport requires an endpoint-check client.")
            self.boto, self.control = boto_client, control_client
            self.cleanup = cleanup_client or boto_client

    def plan(self, state: dict, released: list[dict]) -> Plan:
        turn = state["_turn"]
        request = make_request(state, released, turn["request_id"], turn["revision"])
        body = validate_wire_size(request, MAX_REQUEST_BYTES)
        runtime_session = "wm-" + uuid.uuid4().hex
        metadata = {"runtime_arn": self.runtime_arn, "qualifier": self.qualifier,
                    "runtime_session_id": runtime_session, "request_id": request.request_id,
                    "state_hash": request.state_hash, "released_hash": request.released_hash,
                    "invocation_attempted": False, "stop_status": "NOT_STARTED"}
        plan, failure = None, None
        try:
            endpoint = self.control.get_agent_runtime_endpoint(
                agentRuntimeId=self.runtime_id, endpointName=self.qualifier)
            if (endpoint.get("status") != "READY" or endpoint.get("agentRuntimeArn") != self.runtime_arn
                    or endpoint.get("liveVersion") != self.expected_version
                    or ("targetVersion" in endpoint
                        and endpoint["targetVersion"] != self.expected_version)):
                raise ValueError("Runtime endpoint is not ready at the expected version.")
            if endpoint.get("agentRuntimeEndpointArn") != (
                    f"{self.runtime_arn}/runtime-endpoint/{self.qualifier}"):
                raise ValueError("Runtime endpoint ARN does not match the configured endpoint.")
            metadata["endpoint_version_verified_before_call"] = self.expected_version
            metadata["endpoint_arn"] = endpoint.get("agentRuntimeEndpointArn")
            metadata["invocation_attempted"] = True
            result = self.boto.invoke_agent_runtime(
                agentRuntimeArn=self.runtime_arn, runtimeSessionId=runtime_session,
                qualifier=self.qualifier, payload=body, contentType="application/json",
                accept="application/json")
            response_meta = result.get("ResponseMetadata", {})
            stream = result.get("response")
            try:
                checked_metadata = AgentCoreMetadata.model_validate({**metadata,
                    "aws_request_id": response_meta.get("RequestId"), "trace_id": result.get("traceId")})
                metadata = checked_metadata.model_dump(exclude_none=True)
                if result.get("statusCode", response_meta.get("HTTPStatusCode")) != 200:
                    raise ValueError("Runtime did not return a successful response.")
                if result.get("contentType", "").split(";")[0] != "application/json":
                    raise ValueError("Runtime response was not JSON.")
                if result.get("runtimeSessionId") != runtime_session:
                    raise ValueError("Runtime returned a different session identity.")
                if not hasattr(stream, "read"):
                    raise ValueError("Runtime response body is missing.")
                raw = stream.read(MAX_RESPONSE_BYTES + 1)
            finally:
                if hasattr(stream, "close"):
                    stream.close()
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Runtime response exceeds bounded size.")
            response = AgentCoreResponse.model_validate(json.loads(raw))
            plan = validate_response(response, request, state, released)
            marker = plan.trace[-1]["input"]
            if (marker["scripted_test"] is not self.scripted_test
                    or marker["model_id"] != self.model_id
                    or marker["instruction_version"] != INSTRUCTION_VERSION
                    or marker["sdk_version"] != version("strands-agents")):
                raise ValueError("Runtime model execution does not match configured expectations.")
        except Exception as error:
            failure = error
            metadata["error_code"] = error_code(error)
        finally:
            if metadata["invocation_attempted"]:
                try:
                    self.cleanup.stop_runtime_session(agentRuntimeArn=self.runtime_arn,
                        runtimeSessionId=runtime_session, qualifier=self.qualifier)
                    metadata["stop_status"] = "STOP_REQUEST_ACCEPTED"
                except Exception as error:
                    metadata["stop_status"] = "STOP_FAILED"
                    metadata["stop_error_code"] = error_code(error)
        if failure is not None:
            checked = AgentCoreMetadata.model_validate(metadata).model_dump(exclude_none=True)
            raise AgentCoreTurnError({"status": "FAILED", **checked}) from failure
        # This is client-observed transport metadata, attached to the existing SDK marker.
        checked = AgentCoreMetadata.model_validate(metadata).model_dump(exclude_none=True)
        output = {**plan.trace[-1]["output"], "agentcore": checked}
        plan.trace[-1]["output"] = ObservedModelOutput.model_validate(output).model_dump(exclude_none=True)
        augmented = response.model_copy(update={"plan": {"proposals": plan.proposals, "trace": plan.trace}})
        validate_wire_size(augmented, MAX_RESPONSE_BYTES)
        return plan
