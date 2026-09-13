"""Pinned AgentCore planner for one reserved current-case attempt."""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from hashlib import sha256
from typing import Callable

import boto3
from botocore.config import Config

from .agentcore_evidence import (
    AgentCoreTarget,
    CurrentAgentCoreEvidence,
    CurrentAgentCoreTransportError,
)
from .agentcore_transport import read_response_body
from .agentcore_wrapper import encode_sdk_wrapper
from .delivery_types import InvocationProfile
from .field_delivery_types import CurrentExecutionV3, FieldDeliveryReservation
from .field_planner import FieldPlannerErrorV3, FieldPlannerV3
from .remote_protocol import (
    decode_response,
    encode_request,
    make_request,
)
from .remote_types import CurrentRemoteRequest

_LOGGER = logging.getLogger(__name__)


class CurrentAgentCorePlannerV3(FieldPlannerV3):
    """Perform one exact, non-retried AgentCore invocation."""

    def __init__(
        self,
        runtime_arn: str,
        qualifier: str,
        region: str,
        *,
        expected_account: str,
        expected_version: str,
        profile: InvocationProfile,
        aws_profile: str | None = None,
        boto_client: object | None = None,
        control_client: object | None = None,
        cleanup_client: object | None = None,
        evidence_sink: Callable[[CurrentAgentCoreEvidence], None] | None = None,
    ) -> None:
        try:
            target = AgentCoreTarget(runtime_arn, qualifier, region, expected_account, expected_version)
            if type(profile) is not InvocationProfile:
                raise ValueError
            profile = replace(profile)
            if profile.instruction_version != "watershed-current-v3":
                raise ValueError
            if profile.mode not in {"STRANDS_CURRENT", "SCRIPTED_SDK"}:
                raise ValueError
            if evidence_sink is not None and not callable(evidence_sink):
                raise ValueError
            if aws_profile is not None and (
                type(aws_profile) is not str or not aws_profile or not aws_profile.isprintable()
                or aws_profile != aws_profile.strip() or len(aws_profile) > 200
            ):
                raise ValueError
            supplied = (boto_client, control_client, cleanup_client)
            if any(item is not None for item in supplied) and not all(item is not None for item in supplied):
                raise ValueError
            if all(item is None for item in supplied):
                if profile.mode != "STRANDS_CURRENT":
                    raise ValueError
                session = boto3.Session(profile_name=aws_profile, region_name=region)
                long = Config(connect_timeout=5, read_timeout=145, retries={"max_attempts": 0})
                short = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 0})
                boto_client = session.client("bedrock-agentcore", config=long)
                control_client = session.client("bedrock-agentcore-control", config=short)
                cleanup_client = session.client("bedrock-agentcore", config=short)
            for client, method in (
                (boto_client, "invoke_agent_runtime"),
                (control_client, "get_agent_runtime_endpoint"),
                (cleanup_client, "stop_runtime_session"),
            ):
                if not callable(getattr(client, method, None)):
                    raise ValueError
            self._target = target
            self._profile = replace(profile)
            self.model_id = profile.model_id
            self.scripted_test = profile.mode == "SCRIPTED_SDK"
            self._boto = boto_client
            self._control = control_client
            self._cleanup = cleanup_client
            self._sink = evidence_sink
        except Exception:
            raise ValueError("invalid current AgentCore configuration") from None

    @property
    def profile(self) -> InvocationProfile:
        return replace(self._profile)

    def plan(self, context) -> CurrentExecutionV3:
        raise ValueError("current AgentCore inference requires a reserved attempt")

    def _emit(self, evidence: CurrentAgentCoreEvidence) -> None:
        if self._sink is None:
            return
        try:
            result = self._sink(replace(evidence))
            if result is not None:
                raise ValueError
        except Exception:
            try:
                _LOGGER.warning("current AgentCore evidence sink failed")
            except Exception:
                # An observation failure cannot replace an already validated result.
                pass

    def _evidence(
        self, identity, *, session: str, request_hash: str, wrapper_hash: str,
        attempted: bool, verified: str | None, endpoint: str | None,
        request_id: str | None, trace_id: str | None, response_hash: str | None,
        result_hash: str | None, outcome: str, stop: str, error: str | None,
        stop_error: str | None,
    ) -> CurrentAgentCoreEvidence:
        return CurrentAgentCoreEvidence(
            1, identity, self._target, session, request_hash, wrapper_hash, attempted,
            verified, endpoint, request_id, trace_id, response_hash, result_hash,
            outcome, stop, error, stop_error,
        )

    def plan_reserved(self, reservation: FieldDeliveryReservation) -> CurrentExecutionV3:
        if type(reservation) is not FieldDeliveryReservation or reservation.profile != self.profile:
            raise ValueError("reserved planner profile differs")
        request: CurrentRemoteRequest = make_request(reservation)
        request_bytes = encode_request(request)
        wrapper = encode_sdk_wrapper(request_bytes)
        request_hash = sha256(request_bytes).hexdigest()
        wrapper_hash = sha256(wrapper).hexdigest()
        session = "wm-" + uuid.uuid4().hex
        endpoint = None
        preflight_code = "EndpointCheckFailed"
        try:
            checked = self._control.get_agent_runtime_endpoint(
                agentRuntimeId=self._target.runtime_id, endpointName=self._target.qualifier,
            )
            preflight_code = "EndpointMismatch"
            expected = {"status": "READY", "agentRuntimeArn": self._target.runtime_arn,
                "agentRuntimeEndpointArn": self._target.endpoint_arn,
                "liveVersion": self._target.expected_version}
            if type(checked) is not dict:
                raise ValueError
            if "targetVersion" in checked:
                expected["targetVersion"] = self._target.expected_version
            if any(type(checked.get(key)) is not str or checked[key] != value
                for key, value in expected.items()):
                raise ValueError
            endpoint = self._target.endpoint_arn
        except Exception:
            evidence = self._evidence(
                request.identity, session=session, request_hash=request_hash, wrapper_hash=wrapper_hash,
                attempted=False, verified=None, endpoint=None, request_id=None, trace_id=None,
                response_hash=None, result_hash=None, outcome="OUTCOME_UNKNOWN", stop="NOT_STARTED",
                error=preflight_code, stop_error=None,
            )
            self._emit(evidence)
            raise CurrentAgentCoreTransportError(evidence) from None

        attempted = True
        response_hash = result_hash = None
        service_request = trace_id = None
        outcome = "OUTCOME_UNKNOWN"
        primary_error = "InvokeFailed"
        known: CurrentExecutionV3 | None = None
        known_failure = None
        try:
            try:
                result = self._boto.invoke_agent_runtime(
                    agentRuntimeArn=self._target.runtime_arn, qualifier=self._target.qualifier,
                    runtimeSessionId=session, payload=wrapper,
                    contentType="application/json", accept="application/json",
                )
            except Exception:
                primary_error = "InvokeFailed"
            else:
                try:
                    raw, service_request, trace_id = read_response_body(result, session)
                    response_hash = sha256(raw).hexdigest()
                except Exception:
                    primary_error = "InvalidResponse"
                else:
                    try:
                        response = decode_response(raw, request)
                    except Exception:
                        primary_error = "InvalidPayload"
                    else:
                        result_value = response.execution if response.result_kind == "EXECUTION" else response.failure
                        result_hash = response.result_wire_sha256
                        if response.result_kind == "EXECUTION":
                            known = result_value
                            outcome = "VALIDATED_EXECUTION"
                        else:
                            known_failure = result_value
                            outcome = "VALIDATED_FAILURE"
        except Exception:
            primary_error = "InvalidPayload" if response_hash is not None else "InvokeFailed"

        stop_status = "STOP_REQUEST_ACCEPTED"
        stop_error = None
        try:
            stopped = self._cleanup.stop_runtime_session(
                agentRuntimeArn=self._target.runtime_arn,
                runtimeSessionId=session,
                qualifier=self._target.qualifier,
            )
            if (type(stopped) is not dict or type(stopped.get("runtimeSessionId")) is not str
                or stopped["runtimeSessionId"] != session
                or type(stopped.get("statusCode")) is not int or stopped["statusCode"] != 200):
                raise ValueError
            metadata = stopped.get("ResponseMetadata")
            if metadata is not None and (
                type(metadata) is not dict or (
                    "HTTPStatusCode" in metadata and (
                        type(metadata["HTTPStatusCode"]) is not int or metadata["HTTPStatusCode"] != 200
                    )
                )
            ):
                raise ValueError
        except Exception:
            stop_status, stop_error = "STOP_FAILED", "StopFailed"

        evidence = self._evidence(
            request.identity, session=session, request_hash=request_hash, wrapper_hash=wrapper_hash,
            attempted=attempted, verified=self._target.expected_version, endpoint=endpoint,
            request_id=service_request, trace_id=trace_id, response_hash=response_hash,
            result_hash=result_hash if outcome != "OUTCOME_UNKNOWN" else None,
            outcome=outcome, stop=stop_status, error=None if outcome != "OUTCOME_UNKNOWN" else primary_error,
            stop_error=stop_error,
        )
        self._emit(evidence)
        if outcome == "VALIDATED_EXECUTION":
            return known
        if outcome == "VALIDATED_FAILURE":
            raise FieldPlannerErrorV3(known_failure) from None
        raise CurrentAgentCoreTransportError(evidence) from None
