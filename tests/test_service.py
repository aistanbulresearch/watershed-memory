"""Acceptance tests fixed before implementing the operator service."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from watershed_memory.planning import ReplayPlanner
from watershed_memory.service import Conflict, Service


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path / "cases.sqlite")


def test_three_windows_keep_work_connected(service):
    session = service.create_session()["session_id"]
    july = service.advance(session, "july-request")
    review = july["tasks"][0]["id"]
    august = service.advance(session, "august-request")
    assert len(august["tasks"]) == 1
    assert august["tasks"][0]["id"] == review
    assert len(august["tasks"][0]["evidence"]) == 2
    september = service.advance(session, "september-request")
    assert len(september["tasks"]) == 2
    assert len(september["tasks"][0]["evidence"]) == 3
    assert september["tasks"][1]["kind"] == "EVIDENCE_GAP_REVIEW"
    assert september["case"]["coverage"] == "DEGRADED"
    assert september["case"]["status"] == "OPEN"
    assert september["case"]["water_safety"] == "NOT_ASSESSED"


def test_operator_completion_persists_and_new_work_does_not_reopen_it(service):
    session = service.create_session()["session_id"]
    task = service.advance(session, "july")["tasks"][0]["id"]
    service.respond(session, "ack", task, "acknowledge", "Review assigned to duty team.")
    result = service.respond(session, "done", task, "complete_review", "Reviewed available evidence.")
    assert result["tasks"][0]["status"] == "COMPLETED"
    result = service.advance(session, "august")
    assert len(result["tasks"]) == 2
    assert result["tasks"][0]["status"] == "COMPLETED"
    assert result["tasks"][1]["status"] == "OPEN"
    assert Service(service.path).snapshot(session) == result


def test_retry_does_not_advance_twice_and_conflicting_key_fails(service):
    session = service.create_session()["session_id"]
    first = service.advance(session, "one")
    assert service.advance(session, "one") == first
    task = first["tasks"][0]["id"]
    response = service.respond(session, "reply", task, "acknowledge", "Assigned.")
    assert service.respond(session, "reply", task, "acknowledge", "Assigned.") == response
    with pytest.raises(Conflict):
        service.respond(session, "reply", task, "complete_review", "Different payload.")
    with pytest.raises(Conflict):
        service.respond(session, "one", task, "acknowledge", "Shared id.")


def test_sessions_and_unreleased_evidence_are_isolated(service):
    first = service.create_session()["session_id"]
    second = service.create_session()["session_id"]
    task = service.advance(first, "one")["tasks"][0]["id"]
    untouched = service.snapshot(second)
    assert untouched["progress"]["processed"] == 0
    assert not untouched["tasks"]
    assert all(e["p1_count"] is None for e in untouched["events"])
    with pytest.raises(Conflict):
        service.respond(second, "attack", task, "complete_review", "Wrong session.")
    assert service.snapshot(second) == untouched


def test_invalid_transition_has_no_effect(service):
    session = service.create_session()["session_id"]
    start = service.advance(session, "one")
    task = start["tasks"][0]["id"]
    with pytest.raises(ValueError):
        service.respond(session, "empty", task, "acknowledge", " ")
    with pytest.raises(ValueError):
        service.respond(session, "unsafe", task, "close_watershed", "Close it.")
    assert service.snapshot(session) == start
    completed = service.respond(session, "done", task, "complete_review", "Reviewed.")
    with pytest.raises(Conflict):
        service.respond(session, "again", task, "acknowledge", "Too late.")
    assert service.snapshot(session) == completed


def test_concurrent_duplicate_advance_has_one_effect(service):
    session = service.create_session()["session_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(lambda _: service.advance(session, "same"), range(4)))
    assert all(out == outputs[0] for out in outputs)
    assert service.snapshot(session)["progress"]["processed"] == 1


def test_end_of_replay_and_missing_session_fail(service):
    with pytest.raises(KeyError):
        service.snapshot("unknown")
    session = service.create_session()["session_id"]
    for request in ("one", "two", "three"):
        service.advance(session, request)
    before = service.snapshot(session)
    with pytest.raises(Conflict):
        service.advance(session, "four")
    assert service.snapshot(session) == before


def test_duplicate_requests_claim_model_execution_once_across_service_instances(tmp_path):
    class CountingPlanner(ReplayPlanner):
        calls = 0
        mutex = threading.Lock()

        def plan(self, state, released):
            with self.mutex:
                self.calls += 1
            time.sleep(0.25)
            return super().plan(state, released)

    planner = CountingPlanner()
    path = tmp_path / "shared.sqlite"
    services = [Service(path, planner) for _ in range(4)]
    session = services[0].create_session()["session_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda instance: instance.advance(session, "same-call"), services))
    assert planner.calls == 1
    assert all(state == results[0] for state in results)
