"""Actual local AgentCore SDK routes reason over an immutable admitted case."""

import json
from dataclasses import replace

import pytest
from bedrock_agentcore.runtime import RequestContext
from starlette.requests import Request
from starlette.testclient import TestClient
from test_current_field_dispatch_runner import sdk_field as sdk_field  # noqa: F401
from test_current_field_store import contents
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401
from test_current_remote_types import executed as executed  # noqa: F401
from test_current_remote_types import failure_for
from test_current_remote_types import reserved as reserved  # noqa: F401

from watershed_memory.current.agentcore_wrapper import encode_sdk_wrapper
from watershed_memory.current.field_planner import FieldPlannerErrorV3, FieldPlannerV3
from watershed_memory.current.remote_protocol import decode_response, encode_request, make_request
from watershed_memory.current.runtime_handler import create_current_app, handle_current


class SpyPlanner(FieldPlannerV3):
    def __init__(self, profile, result=None, error=None, local=None):
        self._profile, self.result, self.error, self.local = profile, result, error, local
        self.calls = []

    @property
    def profile(self):
        return self._profile

    def plan(self, context):
        raise AssertionError("identity-less inference is forbidden")

    def plan_reserved(self, reservation):
        self.calls.append(reservation)
        if self.error is not None:
            raise self.error
        return self.local.plan_reserved(reservation) if self.local else self.result


def request_body(reservation):
    request = make_request(reservation)
    return request, encode_sdk_wrapper(encode_request(request))


def post(app, body):
    with TestClient(app) as client:
        return client.post("/invocations", content=body, headers={"content-type": "application/json"})


def test_actual_sdk_route_preserves_reservation_and_success_bytes_without_writes(reserved):
    case, _, local, reservation = reserved
    before = contents(case[0])
    request, body = request_body(reservation)
    planner = SpyPlanner(local.profile, local=local)
    creations = []

    def factory():
        creations.append(True)
        return planner

    app = create_current_app(expected_profile=local.profile, planner_factory=factory)
    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
        assert creations == [] and local.model.response_count == 0
        response = client.post("/invocations", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    restored = decode_response(response.content, request)
    assert restored.execution.assessment.field.disposition == "PROPOSE_FIELD_PLAN"
    assert restored.identity == request.identity
    assert planner.calls == [reservation] and creations == [True]
    assert 1 <= local.model.response_count <= 8
    assert contents(case[0]) == before


@pytest.mark.parametrize("change", [{"model_id": "wrong-model"}, {"sdk_version": "wrong-sdk"},
    {"mode": "STRANDS_CURRENT"}])
def test_full_profile_refused_before_factory_or_model(reserved, change):
    case, _, local, reservation = reserved
    before = contents(case[0])
    _, body = request_body(replace(reservation, profile=replace(local.profile, **change)))
    creations = []
    app = create_current_app(expected_profile=local.profile,
        planner_factory=lambda: creations.append(True))
    response = post(app, body)
    assert response.status_code == 400
    assert response.json() == {"error": "CURRENT_REQUEST_REJECTED"}
    assert creations == [] and local.model.response_count == 0
    assert contents(case[0]) == before


@pytest.mark.parametrize("bad", [b"{}", b'"PRIVATE-CANARY\\ud800"', b"\xff", b'"e30="'])
def test_actual_sdk_never_sees_bad_raw_wrapper(reserved, bad, caplog):
    _, _, local, _ = reserved
    creations = []
    app = create_current_app(expected_profile=local.profile,
        planner_factory=lambda: creations.append(True))
    response = post(app, bad)
    assert response.status_code == 400
    assert response.json() == {"error": "CURRENT_REQUEST_REJECTED"}
    assert creations == [] and local.model.response_count == 0
    assert "PRIVATE-CANARY" not in caplog.text


def test_wrong_factory_profile_is_runtime_failure_without_model(reserved):
    _, _, local, reservation = reserved
    wrong = SpyPlanner(replace(local.profile, sdk_version="different"), local=local)
    app = create_current_app(expected_profile=local.profile, planner_factory=lambda: wrong)
    response = post(app, request_body(reservation)[1])
    assert response.status_code == 500
    assert response.json() == {"error": "CURRENT_RUNTIME_FAILED"}
    assert wrong.calls == [] and local.model.response_count == 0


def test_valid_typed_failure_returns_exact_canonical_failure_without_writes(executed):
    case, _, local, reservation, execution = executed
    before = contents(case[0])
    failure = failure_for(reservation, execution)
    planner = SpyPlanner(local.profile, error=FieldPlannerErrorV3(failure))
    app = create_current_app(expected_profile=local.profile, planner_factory=lambda: planner)
    request, body = request_body(reservation)
    response = post(app, body)
    assert response.status_code == 200
    assert decode_response(response.content, request).failure == failure
    assert planner.calls == [reservation] and contents(case[0]) == before


@pytest.mark.parametrize("kind", ["factory", "unexpected", "forged_failure", "forged_execution"])
def test_unexpected_or_invalid_result_is_never_promoted_to_typed_failure(executed, kind, caplog):
    case, _, local, reservation, execution = executed
    before = contents(case[0])
    failure = failure_for(reservation, execution)
    failure = replace(failure, source_trace=(replace(failure.source_trace[0], output_json="{}"),))
    trace = execution.assessment.field_trace
    forged = replace(execution, assessment=replace(execution.assessment,
        field_trace=(replace(trace[0], output_json="{}"), *trace[1:])))
    planner = SpyPlanner(local.profile, result=forged,
        error=FieldPlannerErrorV3(failure) if kind == "forged_failure" else
        RuntimeError("PRIVATE-CANARY") if kind == "unexpected" else None)

    def factory():
        if kind == "factory":
            raise ValueError("PRIVATE-CANARY")
        return planner

    app = create_current_app(expected_profile=local.profile, planner_factory=factory)
    response = post(app, request_body(reservation)[1])
    assert response.status_code == 500
    assert response.json() == {"error": "CURRENT_RUNTIME_FAILED"}
    assert "PRIVATE-CANARY" not in caplog.text
    assert contents(case[0]) == before


@pytest.mark.parametrize("state", ["absent_request", "absent_pin", "wrong_pin_type", "wrong_pin", "wrong_payload"])
def test_handler_requires_middleware_scope_binding_before_factory(reserved, state):
    _, _, local, reservation = reserved
    request, body = request_body(reservation)
    raw = encode_request(request)
    scope = {"type": "http", "watershed.current_request_bytes": raw}
    if state == "absent_pin":
        del scope["watershed.current_request_bytes"]
    if state == "wrong_pin":
        scope["watershed.current_request_bytes"] = b"{}"
    if state == "wrong_pin_type":
        scope["watershed.current_request_bytes"] = "not-bytes"
    context = RequestContext(request=None if state == "absent_request" else Request(scope))
    payload = "" if state == "wrong_payload" else json.loads(body)
    creations = []
    response = handle_current(payload, context, expected_profile=local.profile,
        planner_factory=lambda: creations.append(True))
    server_fault = state in {"absent_request", "absent_pin", "wrong_pin_type"}
    assert response.status_code == (500 if server_fault else 400)
    assert json.loads(response.body) == {
        "error": "CURRENT_RUNTIME_FAILED" if server_fault else "CURRENT_REQUEST_REJECTED",
    }
    assert creations == [] and local.model.response_count == 0
