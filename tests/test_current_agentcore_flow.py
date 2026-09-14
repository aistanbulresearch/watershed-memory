"""Actual local SDK routes connect remote transport to durable field delivery."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Lock

from starlette.testclient import TestClient
from test_current_agentcore_transport import Body
from test_current_field_agentcore import Cleanup, build
from test_current_field_dispatch_runner import AT
from test_current_field_dispatch_runner import sdk_field as sdk_field  # noqa: F401
from test_current_field_sdk_budget import BatchModel
from test_current_field_store import CASE, NOW, OPERATOR, contents, mutate
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401
from test_current_remote_types import executed as executed  # noqa: F401
from test_current_remote_types import reserved as reserved  # noqa: F401

from watershed_memory.current.agentcore_wrapper import decode_sdk_wrapper
from watershed_memory.current.field_delivery_codec import decode_execution
from watershed_memory.current.field_dispatch_runner import FieldDispatchRunner
from watershed_memory.current.field_types import CorrectFieldResult
from watershed_memory.current.remote_protocol import (
    decode_request,
    encode_response,
    success_response,
)
from watershed_memory.current.runtime_handler import create_current_app


class AppInvoke:
    """In-process ASGI test transport, never an AWS service invocation."""

    def __init__(self, local, *, after_response=None, truncate=False):
        self.app = create_current_app(expected_profile=local.profile, planner_factory=lambda: local)
        self.after_response, self.truncate = after_response, truncate
        self.calls, self.bodies, self.http_statuses = [], [], []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        with TestClient(self.app) as client:
            response = client.post("/invocations", content=kwargs["payload"],
                headers={"content-type": kwargs["contentType"]})
        self.http_statuses.append(response.status_code)
        if self.after_response:
            self.after_response()
        raw = response.content[:-1] if self.truncate else response.content
        body = Body([raw])
        self.bodies.append(body)
        return {"statusCode": response.status_code, "contentType": response.headers["content-type"],
            "runtimeSessionId": kwargs["runtimeSessionId"], "response": body,
            "ResponseMetadata": {"HTTPStatusCode": response.status_code, "RequestId": "local-asgi-fixture"}}


def test_actual_sdk_route_commits_once_then_replays_without_inference(sdk_field):
    case, store, local = sdk_field
    invoke, events, observed_statuses = AppInvoke(local), [], []
    def sink(event):
        events.append(event)
        observed_statuses.append(store.status(CASE).active_status)
    remote, _, control, cleanup = build(local.profile, invoke=invoke, sink=sink)
    active = FieldDispatchRunner(store, remote, clock=lambda: AT)
    result = active.tick(CASE)
    assert result.outcome == "COMMITTED" and result.receipt.field_plan.plan.status == "PROPOSED"
    assert invoke.http_statuses == [200] and observed_statuses == ["RESERVED"]
    assert len(events) == len(invoke.calls) == len(control.calls) == len(cleanup.calls) == 1
    assert events[0].identity.attempt_id == result.receipt.delivery.attempt_id
    assert events[0].outcome == "VALIDATED_EXECUTION" and local.model.response_count == 6
    before = contents(case[0])
    saved = decode_execution(result.receipt.delivery.execution_json)
    assert store.finish(result.attempt_id, saved, now=AT) == result.receipt
    assert store.get(CASE, result.receipt.delivery.request_id) == result.receipt
    assert active.tick(CASE).outcome == "QUIET"
    assert contents(case[0]) == before and len(invoke.calls) == 1 and local.model.response_count == 6
    assert store.journal.allowance().scripted_used == 1 and store.journal.allowance().provider_used == 0


def test_actual_sdk_failure_is_saved_and_held_without_reinvocation(sdk_field):
    case, store, local = sdk_field
    local.model = BatchModel([])
    invoke, events = AppInvoke(local), []
    remote, _, _, cleanup = build(local.profile, invoke=invoke, sink=events.append)
    active = FieldDispatchRunner(store, remote, clock=lambda: AT)
    result = active.tick(CASE)
    assert result.outcome == "FAILED" and result.receipt.delivery.failure_json is not None
    assert invoke.http_statuses == [200] and events[0].outcome == "VALIDATED_FAILURE"
    before = contents(case[0])
    assert active.tick(CASE).outcome == "HELD"
    assert contents(case[0]) == before and len(invoke.calls) == len(cleanup.calls) == 1
    assert store.journal.allowance().scripted_used == 1


def test_lost_response_after_sdk_execution_preserves_reserved_identity(sdk_field):
    case, store, local = sdk_field
    invoke, events = AppInvoke(local, truncate=True), []
    remote, _, _, cleanup = build(local.profile, invoke=invoke, sink=events.append)
    active = FieldDispatchRunner(store, remote, clock=lambda: AT)
    result = active.tick(CASE)
    assert result.outcome == "OUTCOME_UNKNOWN" and result.receipt is None
    assert store.status(CASE).active_status == "RESERVED"
    assert events[0].identity.attempt_id == result.attempt_id
    assert events[0].outcome == "OUTCOME_UNKNOWN" and local.model.response_count == 6
    before = contents(case[0])
    assert active.tick(CASE).outcome == "HELD"
    assert contents(case[0]) == before and len(invoke.calls) == len(cleanup.calls) == 1
    assert store.journal.allowance().scripted_used == 1


def test_human_correction_during_remote_turn_is_preserved_and_proposal_is_stale(sdk_field):
    case, store, local = sdk_field
    original = case[2].recent(CASE)[0].result.report
    corrections, events = [], []
    def correct():
        corrections.append(mutate(case, "correct_result", CorrectFieldResult(
            original.report_id, original.revision, "COMPLETE", "Corrected completed field inspection.",
            NOW, NOW + timedelta(minutes=10), True), who=OPERATOR, now=AT))
    invoke = AppInvoke(local, after_response=correct)
    remote, *_ = build(local.profile, invoke=invoke, sink=events.append)
    result = FieldDispatchRunner(store, remote, clock=lambda: AT).tick(CASE)
    assert result.outcome == "STALE" and result.receipt.field_plan is None
    assert events[0].outcome == "VALIDATED_EXECUTION"
    current = case[2].get_result(CASE, original.report_id)
    assert current == corrections[0].result and current.report.outcome == "COMPLETE"
    assert current.report.revision == original.revision + 1
    assert len(invoke.calls) == 1 and local.model.response_count == 6


def test_concurrent_attempts_on_one_client_keep_distinct_request_evidence(executed):
    case, _, local, reservation, execution = executed
    barrier, lock, events = Barrier(2), Lock(), []
    reservations = [reservation, replace(reservation, attempt_id="attempt-" + "f" * 32,
        request_id="concurrent-request-2")]

    class ConcurrentInvoke:
        def __init__(self):
            self.calls = []
        def invoke_agent_runtime(self, **kwargs):
            with lock:
                self.calls.append(kwargs)
            request = decode_request(decode_sdk_wrapper(kwargs["payload"]))
            raw = encode_response(success_response(request, execution), request)
            barrier.wait(timeout=15)
            return {"statusCode": 200, "contentType": "application/json", "response": Body([raw]),
                "runtimeSessionId": kwargs["runtimeSessionId"], "ResponseMetadata": {}}

    def sink(event):
        with lock:
            events.append(event)
    invoke, cleanup = ConcurrentInvoke(), Cleanup()
    remote, *_ = build(local.profile, invoke=invoke, cleanup=cleanup, sink=sink)
    before = contents(case[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(remote.plan_reserved, reservations))
    assert results == [execution, execution] and contents(case[0]) == before
    assert len(invoke.calls) == len(cleanup.calls) == len(events) == 2
    assert {e.identity.attempt_id for e in events} == {r.attempt_id for r in reservations}
    assert len({e.runtime_session_id for e in events}) == len({e.request_sha256 for e in events}) == 2
    calls = {c["runtimeSessionId"]: decode_request(decode_sdk_wrapper(c["payload"])) for c in invoke.calls}
    assert all(calls[e.runtime_session_id].identity == e.identity for e in events)
    assert not hasattr(remote, "last_result") and not hasattr(remote, "current_attempt")
