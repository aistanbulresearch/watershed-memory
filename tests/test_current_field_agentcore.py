"""One reserved case produces at most one pinned AgentCore invocation."""

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from test_current_agentcore_transport import Body, StringSubclass
from test_current_field_dispatch_runner import sdk_field as sdk_field  # noqa: F401
from test_current_field_store import contents
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401
from test_current_remote_types import executed as executed  # noqa: F401
from test_current_remote_types import failure_for
from test_current_remote_types import reserved as reserved  # noqa: F401

from watershed_memory.current import field_agentcore as client_module
from watershed_memory.current.agentcore_evidence import CurrentAgentCoreTransportError
from watershed_memory.current.agentcore_wrapper import encode_sdk_wrapper
from watershed_memory.current.delivery_types import InvocationProfile
from watershed_memory.current.field_planner import FieldPlannerErrorV3, FieldPlannerV3
from watershed_memory.current.remote_protocol import (
    encode_request,
    encode_response,
    failure_response,
    make_request,
    success_response,
)

ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/watershedcurrent-abc123"
ENDPOINT = ARN + "/runtime-endpoint/proof_v3"


class Control:
    def __init__(self, change=None, error=None):
        self.calls = []
        self.error = error
        self.value = {"status": "READY", "agentRuntimeArn": ARN,
            "agentRuntimeEndpointArn": ENDPOINT, "liveVersion": "1", "targetVersion": "1"}
        self.value.update(change or {})

    def get_agent_runtime_endpoint(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.value


class Invoke:
    def __init__(self, raw=b"{}", *, error=None, change=None, body=None):
        self.calls = []
        self.error, self.change = error, change or {}
        self.body = body if body is not None else Body([raw])

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"statusCode": 200, "contentType": "application/json",
            "runtimeSessionId": kwargs["runtimeSessionId"], "response": self.body,
            "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "request-1"},
            "traceId": "trace-1", **self.change}


class Cleanup:
    def __init__(self, *, error=None, change=None):
        self.calls = []
        self.error, self.change = error, change or {}

    def stop_runtime_session(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"runtimeSessionId": kwargs["runtimeSessionId"], "statusCode": 200, **self.change}


def build(base_profile, *, invoke=None, control=None, cleanup=None, sink=None, **changes):
    invoke = invoke if invoke is not None else Invoke()
    control = control if control is not None else Control()
    cleanup = cleanup if cleanup is not None else Cleanup()
    arguments = dict(runtime_arn=ARN, qualifier="proof_v3", region="us-east-1",
        expected_account="123456789012", expected_version="1", profile=base_profile,
        boto_client=invoke, control_client=control, cleanup_client=cleanup, evidence_sink=sink)
    arguments.update(changes)
    return client_module.CurrentAgentCorePlannerV3(**arguments), invoke, control, cleanup


@pytest.fixture
def profile():
    return InvocationProfile("SCRIPTED_SDK", "fixture", "watershed-current-v3", "1.55.1")


@pytest.mark.parametrize("change", [
    {"runtime_arn": None}, {"qualifier": "DEFAULT"}, {"expected_version": 1},
    {"expected_account": "000000000000"}, {"profile": {}},
    {"evidence_sink": "not-callable"}, {"aws_profile": True}, {"aws_profile": " bad "},
    {"boto_client": None}, {"control_client": None}, {"cleanup_client": None},
    {"boto_client": object()}, {"control_client": object()}, {"cleanup_client": object()},
])
def test_bad_configuration_is_refused_before_any_real_sdk_construction(profile, monkeypatch, change):
    constructions = []
    monkeypatch.setattr(client_module.boto3, "Session", lambda **kw: constructions.append(kw))
    with pytest.raises(ValueError):
        build(profile, **change)
    assert constructions == []


def test_scripted_profile_cannot_construct_real_clients(profile, monkeypatch):
    constructions = []
    monkeypatch.setattr(client_module.boto3, "Session", lambda **kw: constructions.append(kw))
    with pytest.raises(ValueError):
        build(profile, boto_client=None, control_client=None, cleanup_client=None)
    assert constructions == []


@pytest.mark.parametrize("attribute,value", [("model_id", None), ("sdk_version", True)])
def test_forged_native_profile_is_revalidated_before_client_construction(profile, monkeypatch, attribute, value):
    forged = replace(profile, mode="STRANDS_CURRENT")
    object.__setattr__(forged, attribute, value)
    constructions = []
    monkeypatch.setattr(client_module.boto3, "Session", lambda **kw: constructions.append(kw))
    with pytest.raises(ValueError):
        build(forged, boto_client=None, control_client=None, cleanup_client=None)
    assert constructions == []


def test_real_constructor_has_explicit_region_profile_and_zero_retries(profile, monkeypatch):
    constructions, clients = [], []

    class Session:
        def __init__(self, **kw):
            constructions.append(kw)

        def client(self, name, *, config):
            clients.append((name, config))
            return Control() if name.endswith("-control") else SimpleNamespace(
                invoke_agent_runtime=lambda **kw: None, stop_runtime_session=lambda **kw: None)

    monkeypatch.setattr(client_module.boto3, "Session", Session)
    real = replace(profile, mode="STRANDS_CURRENT")
    planner, *_ = build(real, aws_profile="research team", boto_client=None,
        control_client=None, cleanup_client=None)
    assert isinstance(planner, FieldPlannerV3)
    assert planner.profile == real and planner.profile is not real
    assert planner.model_id == real.model_id and planner.scripted_test is False
    assert constructions == [{"profile_name": "research team", "region_name": "us-east-1"}]
    assert [name for name, _ in clients] == ["bedrock-agentcore", "bedrock-agentcore-control", "bedrock-agentcore"]
    assert [c.read_timeout for _, c in clients] == [145, 10, 10]
    assert all(c.connect_timeout == 5 and c.retries == {"max_attempts": 0} for _, c in clients)


def test_sdk_construction_error_is_redacted(profile, monkeypatch):
    def fail(**kw):
        raise RuntimeError("PRIVATE-CANARY")
    monkeypatch.setattr(client_module.boto3, "Session", fail)
    with pytest.raises(ValueError) as caught:
        build(replace(profile, mode="STRANDS_CURRENT"), boto_client=None,
            control_client=None, cleanup_client=None)
    assert "PRIVATE-CANARY" not in str(caught.value) and caught.value.__suppress_context__


def test_wrong_full_profile_and_identityless_call_never_reach_transport(reserved):
    _, _, local, reservation = reserved
    events = []
    planner, invoke, control, cleanup = build(local.profile, sink=events.append)
    invalids = [None, {}, replace(reservation, profile=replace(local.profile, sdk_version="other"))]
    forged = deepcopy(reservation)
    object.__setattr__(forged, "source_delivery", 1)
    invalids.append(forged)
    for value in invalids:
        with pytest.raises(ValueError):
            planner.plan_reserved(value)
    with pytest.raises(ValueError):
        planner.plan(reservation.context)
    assert not invoke.calls and not control.calls and not cleanup.calls and not events


@pytest.mark.parametrize("change", [
    {"status": "CREATING"}, {"liveVersion": "2"}, {"liveVersion": 1},
    {"targetVersion": "2"}, {"agentRuntimeArn": ARN + "x"},
    {"agentRuntimeEndpointArn": ENDPOINT + "x"},
    {"status": StringSubclass("READY")}, {"liveVersion": StringSubclass("1")},
    {"agentRuntimeArn": StringSubclass(ARN)},
])
def test_endpoint_mismatch_emits_unknown_without_invoke_or_stop(reserved, change):
    _, _, local, reservation = reserved
    events = []
    planner, invoke, control, cleanup = build(local.profile, control=Control(change), sink=events.append)
    with pytest.raises(CurrentAgentCoreTransportError) as caught:
        planner.plan_reserved(reservation)
    assert len(control.calls) == 1 and not invoke.calls and not cleanup.calls
    assert len(events) == 1 and events[0] == caught.value.evidence
    assert events[0].outcome == "OUTCOME_UNKNOWN" and not events[0].invocation_attempted
    assert events[0].stop_status == "NOT_STARTED"
    assert caught.value.__suppress_context__


def test_endpoint_error_is_redacted_and_not_retried(reserved):
    _, _, local, reservation = reserved
    planner, invoke, control, cleanup = build(local.profile,
        control=Control(error=RuntimeError("PRIVATE-CANARY")))
    with pytest.raises(CurrentAgentCoreTransportError) as caught:
        planner.plan_reserved(reservation)
    assert "PRIVATE-CANARY" not in str(caught.value) + repr(caught.value.evidence)
    assert len(control.calls) == 1 and not invoke.calls and not cleanup.calls


def test_preflight_exception_text_is_never_evaluated(reserved):
    class UnprintableError(Exception):
        def __str__(self):
            raise AssertionError("PRIVATE-CANARY")
    _, _, local, reservation = reserved
    events = []
    planner, invoke, _, cleanup = build(local.profile,
        control=Control(error=UnprintableError()), sink=events.append)
    with pytest.raises(CurrentAgentCoreTransportError):
        planner.plan_reserved(reservation)
    assert len(events) == 1 and not invoke.calls and not cleanup.calls


def test_valid_success_uses_exact_wrapped_reservation_and_leaves_commit_to_runner(executed):
    case, _, local, reservation, execution = executed
    request = make_request(reservation)
    raw = encode_response(success_response(request, execution), request)
    events = []
    planner, invoke, control, cleanup = build(local.profile, invoke=Invoke(raw), sink=events.append)
    before = contents(case[0])
    assert planner.plan_reserved(reservation) == execution
    assert contents(case[0]) == before and len(events) == 1
    call, stop = invoke.calls[0], cleanup.calls[0]
    assert len(invoke.calls) == len(control.calls) == len(cleanup.calls) == 1
    assert control.calls == [{"agentRuntimeId": "watershedcurrent-abc123", "endpointName": "proof_v3"}]
    assert call == dict(agentRuntimeArn=ARN, qualifier="proof_v3",
        runtimeSessionId=events[0].runtime_session_id,
        payload=encode_sdk_wrapper(encode_request(request)), contentType="application/json", accept="application/json")
    assert stop == {k: call[k] for k in ("agentRuntimeArn", "runtimeSessionId", "qualifier")}
    event = events[0]
    assert event.identity == request.identity and event.outcome == "VALIDATED_EXECUTION"
    assert event.request_sha256 == sha256(encode_request(request)).hexdigest()
    assert event.wrapper_sha256 == sha256(call["payload"]).hexdigest()
    assert event.response_sha256 == sha256(raw).hexdigest()
    assert event.result_wire_sha256 == success_response(request, execution).result_wire_sha256
    assert invoke.body.closes == 1 and len(invoke.body.reads) == 2


@pytest.mark.parametrize("kind", ["success", "failure", "unknown"])
@pytest.mark.parametrize("cleanup_problem", ["exception", "wrong_session", "wrong_status"])
def test_cleanup_failure_preserves_the_primary_outcome(executed, kind, cleanup_problem):
    _, _, local, reservation, execution = executed
    request = make_request(reservation)
    failure = failure_for(reservation, execution)
    response = success_response(request, execution) if kind == "success" else failure_response(request, failure)
    invoke = Invoke(encode_response(response, request),
        error=TimeoutError("PRIVATE-CANARY") if kind == "unknown" else None)
    cleanup = Cleanup(error=RuntimeError("PRIVATE-CANARY") if cleanup_problem == "exception" else None,
        change={"runtimeSessionId": "wrong"} if cleanup_problem == "wrong_session" else
        {"statusCode": 500} if cleanup_problem == "wrong_status" else None)
    events = []
    planner, *_ = build(local.profile, invoke=invoke, cleanup=cleanup, sink=events.append)
    if kind == "success":
        assert planner.plan_reserved(reservation) == execution
    else:
        error = FieldPlannerErrorV3 if kind == "failure" else CurrentAgentCoreTransportError
        with pytest.raises(error) as caught:
            planner.plan_reserved(reservation)
        if kind == "failure":
            assert caught.value.failure == failure
    assert len(invoke.calls) == len(cleanup.calls) == len(events) == 1
    event = events[0]
    assert event.outcome == {"success": "VALIDATED_EXECUTION", "failure": "VALIDATED_FAILURE",
        "unknown": "OUTCOME_UNKNOWN"}[kind]
    assert event.stop_status == "STOP_FAILED" and event.stop_error_code
    assert "PRIVATE-CANARY" not in repr(event)


@pytest.mark.parametrize("kind", ["wrong_session", "wrong_attempt", "wrong_hash", "truncated", "suffix", "close_error"])
def test_malformed_response_stays_unknown_with_one_cleanup(executed, kind):
    _, _, local, reservation, execution = executed
    request = make_request(reservation)
    raw = encode_response(success_response(request, execution), request)
    if kind in {"wrong_attempt", "wrong_hash"}:
        data = json.loads(raw)
        if kind == "wrong_attempt":
            data["identity"]["attempt_id"] = "attempt-" + "f" * 32
        else:
            data["result_wire_sha256"] = "0" * 64
        raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    body = Body([raw[:-1] if kind == "truncated" else raw] + ([b"suffix"] if kind == "suffix" else []),
        close_failure=kind == "close_error")
    invoke = Invoke(body=body, change={"runtimeSessionId": "wrong"} if kind == "wrong_session" else None)
    events = []
    planner, _, _, cleanup = build(local.profile, invoke=invoke, sink=events.append)
    with pytest.raises(CurrentAgentCoreTransportError) as caught:
        planner.plan_reserved(reservation)
    assert len(invoke.calls) == len(cleanup.calls) == len(events) == 1 and body.closes == 1
    assert events[0].outcome == "OUTCOME_UNKNOWN" and events[0].result_wire_sha256 is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("kind", ["success", "failure", "unknown"])
@pytest.mark.parametrize("sink_problem", ["exception", "non_none"])
def test_evidence_sink_cannot_replace_outcome_or_retry(executed, kind, sink_problem, caplog):
    _, _, local, reservation, execution = executed
    request = make_request(reservation)
    failure = failure_for(reservation, execution)
    response = success_response(request, execution) if kind == "success" else failure_response(request, failure)
    invoke = Invoke(encode_response(response, request),
        error=TimeoutError("PRIVATE-CANARY") if kind == "unknown" else None)
    events = []
    def sink(event):
        events.append(event)
        if sink_problem == "exception":
            raise RuntimeError("PRIVATE-CANARY")
        return "PRIVATE-CANARY"
    planner, _, _, cleanup = build(local.profile, invoke=invoke, sink=sink)
    if kind == "success":
        assert planner.plan_reserved(reservation) == execution
    else:
        with pytest.raises(FieldPlannerErrorV3 if kind == "failure" else CurrentAgentCoreTransportError):
            planner.plan_reserved(reservation)
    assert len(events) == len(invoke.calls) == len(cleanup.calls) == 1
    assert "PRIVATE-CANARY" not in caplog.text and len(caplog.records) == 1


def test_cleanup_session_requires_exact_native_string(executed):
    class SubclassCleanup(Cleanup):
        def stop_runtime_session(self, **kwargs):
            result = super().stop_runtime_session(**kwargs)
            result["runtimeSessionId"] = StringSubclass(result["runtimeSessionId"])
            return result
    _, _, local, reservation, execution = executed
    request, events = make_request(reservation), []
    invoke = Invoke(encode_response(success_response(request, execution), request))
    planner, *_ = build(local.profile, invoke=invoke, cleanup=SubclassCleanup(), sink=events.append)
    assert planner.plan_reserved(reservation) == execution
    assert events[0].outcome == "VALIDATED_EXECUTION" and events[0].stop_status == "STOP_FAILED"


def test_broken_logging_after_sink_failure_cannot_erase_validated_result(executed, monkeypatch):
    _, _, local, reservation, execution = executed
    request = make_request(reservation)
    def broken(*a, **kw):
        raise RuntimeError("PRIVATE-CANARY")
    monkeypatch.setattr(client_module._LOGGER, "warning", broken)
    invoke = Invoke(encode_response(success_response(request, execution), request))
    planner, _, _, cleanup = build(local.profile, invoke=invoke, sink=broken)
    assert planner.plan_reserved(reservation) == execution
    assert len(invoke.calls) == len(cleanup.calls) == 1
