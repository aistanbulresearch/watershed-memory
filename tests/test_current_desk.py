"""One operator view preserves source facts, private notes and human revisions."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_case_store import CASE, CONFIG, LATER, NOW, query, stage
from test_current_case_store import ready as ready
from test_current_dispatch_runner import sdk_case as sdk_case
from test_current_dispatch_store import snapshot

from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.case_types import HumanAction, WorkflowConflict
from watershed_memory.current.desk import CurrentDesk
from watershed_memory.current.dispatch_runner import DispatchRunner


def test_reading_times_coverage_and_plan_are_separate_and_reads_write_nothing(ready, monkeypatch):
    path, cases, _, events = ready
    review = stage(cases, events[0])
    before = snapshot(ready)
    opened = []
    connect = cases._connect

    def read_only():
        db = connect()
        db.execute("PRAGMA query_only=ON")
        opened.append(db)
        return db

    monkeypatch.setattr(cases, "_connect", read_only)
    desk = CurrentDesk(cases, CASE)
    result = desk.snapshot(now=NOW)
    assert len(opened) == 1
    assert result["source"]["readings"][0]["value"] == "4"
    assert result["source"]["readings"][0]["observed_at"] != result["source"]["collected_through"]
    assert result["source"]["readings"][0]["retrieved_at"] == NOW.isoformat()
    assert result["source"]["missing_parameters"] == ["00045", "63680"]
    assert result["source"]["readings"][1]["flags"] == ["MISSING"]
    assert result["interval"]["event_id"] == events[-1].event_id
    assert result["work"][0]["evidence_event_ids"] == [events[0].event_id]
    assert result["work"][0]["task_id"] == review.task_id
    assert result["dispatch"] is None and result["assessments"] == []
    assert snapshot(ready) == before
    assert not query(path, "SELECT name FROM sqlite_master WHERE name LIKE 'dispatch_%'")
    result["work"][0]["title"] = "Changed detached browser value"
    assert cases.get_review(CASE, review.task_id).title == review.title


def test_empty_watch_remains_empty_without_made_up_measurements(ready):
    _, cases, watch, _ = ready
    from watershed_memory.watch.store import MonitorConfig

    watch.register(MonitorConfig("empty-monitor", "USGS-08380500", "EMPTY", NOW), now=NOW)
    cases.register(replace(CONFIG, case_id="EMPTY", monitor_id="empty-monitor"), now=NOW)
    result = CurrentDesk(cases, "EMPTY").snapshot(now=NOW)
    assert result["interval"] is None and result["work"] == []
    assert result["source"]["observation_count"] == 0
    assert result["source"]["collected_through"] is None
    assert all(r["value"] is None and r["flags"] == ["MISSING"] for r in result["source"]["readings"])
    from test_watch_store import batch

    fetched_at = NOW + timedelta(minutes=10)
    lease = watch.acquire_poll("empty-monitor", now=fetched_at)
    watch.commit_poll(lease, batch(lease), now=fetched_at)
    collected = CurrentDesk(cases, "EMPTY").snapshot(now=fetched_at)
    assert collected["source"]["collected_through"] == fetched_at.isoformat()
    assert collected["source"]["observation_count"] == 0


def test_private_note_absent_and_duplicate_action_returns_original_after_later_change(ready):
    path, cases, _, events = ready
    review = stage(cases, events[-1])
    desk = CurrentDesk(cases, CASE)
    private = "PRIVATE-DESK-NOTE-CANARY"
    action = HumanAction(review.task_id, 1, "MODIFY", private, "Verify tomorrow's reading", LATER)
    first = desk.respond(action, request_id="operator-1", now=NOW)
    assert first["receipt"]["revision"] == 2
    desk.respond(HumanAction(review.task_id, 2, "CANCEL"), request_id="operator-2", now=NOW)
    repeated = desk.respond(action, request_id="operator-1", now=NOW + timedelta(hours=2))
    assert repeated["receipt"] == first["receipt"]
    assert repeated["snapshot"]["work"] == []
    assert repeated["current_work"]["revision"] == 3
    assert repeated["current_work"]["status"] == "CANCELLED"
    assert private not in json.dumps([first, repeated, desk.snapshot(now=NOW)])
    assert private in query(path, "SELECT input_json FROM current_receipts WHERE request_id='operator-1'")[0][0]
    assert len(query(path, "SELECT * FROM current_review_revisions")) == 3


def test_stale_human_action_does_not_overwrite_saved_plan(ready):
    _, cases, _, events = ready
    review = stage(cases, events[0])
    desk = CurrentDesk(cases, CASE)
    desk.respond(HumanAction(review.task_id, 1, "APPROVE"), request_id="approve", now=NOW)
    before = snapshot(ready)
    with pytest.raises(WorkflowConflict):
        desk.respond(HumanAction(review.task_id, 1, "CANCEL"), request_id="stale", now=NOW)
    assert snapshot(ready) == before


def test_expired_approval_is_not_offered_but_plan_can_be_rescheduled(ready):
    _, cases, _, events = ready
    stage(cases, events[-1])
    result = CurrentDesk(cases, CASE).snapshot(now=LATER)
    assert result["work"][0]["available_actions"] == ["MODIFY", "DEFER", "DISMISS", "CANCEL"]
    assert "STALE" in result["source"]["readings"][0]["flags"]


def test_committed_sdk_receipt_is_bounded_safe_and_bound_to_the_case(sdk_case):
    source, dispatch, planner, _ = sdk_case
    tick = DispatchRunner(dispatch, planner, clock=lambda: NOW).tick(CASE)
    desk = CurrentDesk(source[1], CASE)
    before = snapshot(source)
    result = desk.snapshot(now=NOW)
    detail = desk.assessment(tick.attempt_id)
    assert result["dispatch"]["remaining_attempts"] == 1
    assert result["dispatch"]["assessment_count"] == 1
    assert result["assessments"][0]["status"] == "COMMITTED"
    assert detail["attention_reasons"] == ["INITIAL_REVIEW"]
    assert detail["tool_names"] == ["get_case_context", "inspect_current_series", "inspect_source_health", "stage_assessment"]
    assert detail["decisions"][0]["disposition"] == "NO_FOLLOW_UP"
    assert detail["profile"]["mode"] == "SCRIPTED_SDK"
    assert not any(key in json.dumps(detail) for key in ("input_json", "output_json", "usage_json", str(source[0])))
    assert snapshot(source) == before
    source[1].register(replace(CONFIG, case_id="ISOLATED", simulated=True), now=NOW)
    with pytest.raises(KeyError):
        CurrentDesk(source[1], "ISOLATED").assessment(tick.attempt_id)


def test_reserved_attempt_is_visible_without_inference(sdk_case):
    source, dispatch, _, _ = sdk_case
    reservation = dispatch.prepare(CASE, now=NOW).reservation
    result = CurrentDesk(source[1], CASE).snapshot(now=NOW)
    assert result["dispatch"]["active_status"] == "RESERVED"
    detail = CurrentDesk(source[1], CASE).assessment(reservation.attempt_id)
    assert detail["tool_names"] == [] and detail["decisions"] == []
    assert detail["finished_at"] is None


@pytest.mark.parametrize("mutation", ["bad_trace", "bad_decision", "wrong_event", "wrong_profile"])
def test_malformed_execution_fails_closed_instead_of_becoming_browser_data(sdk_case, mutation):
    source, dispatch, planner, _ = sdk_case
    tick = DispatchRunner(dispatch, planner, clock=lambda: NOW).tick(CASE)
    with source[1]._connect() as db:
        raw = json.loads(db.execute("SELECT execution_json FROM delivery_attempts").fetchone()[0])
        if mutation == "bad_trace":
            raw["assessment"]["trace"][0]["name"] = "PRIVATE-ARBITRARY-TOOL"
        elif mutation == "bad_decision":
            raw["assessment"]["decisions"][0]["reference_ids"] = "PRIVATE-NOT-ARRAY"
        elif mutation == "wrong_event":
            raw["assessment"]["event_id"] = "a" * 64
        else:
            raw["model_id"] = "different-model"
        from watershed_memory.current.tools import _json
        db.execute("UPDATE delivery_attempts SET execution_json=?", (_json(raw),))
    with pytest.raises((ValueError, WorkflowConflict)):
        CurrentDesk(source[1], CASE).assessment(tick.attempt_id)


def test_reopen_keeps_saved_human_plan_and_does_not_enable_models(ready):
    path, cases, _, events = ready
    review = stage(cases, events[0])
    CurrentDesk(cases, CASE).respond(HumanAction(review.task_id, 1, "DEFER", next_check_at=LATER), request_id="defer", now=NOW)
    result = CurrentDesk(CaseStore(path), CASE).snapshot(now=NOW)
    assert result["work"][0]["status"] == "DEFERRED"
    assert result["work"][0]["next_check_at"] == LATER.isoformat()
    assert result["dispatch"] is None


@pytest.mark.parametrize("value", ["PRIVATE-ROW-CANARY", 1.5, -1, 1, 2**63-1])
def test_malformed_reserved_revision_cannot_reach_browser(sdk_case, value):
    source, dispatch, _, _ = sdk_case
    reservation = dispatch.prepare(CASE, now=NOW).reservation
    with source[1]._connect() as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE delivery_attempts SET case_revision=?", (value,))
    with pytest.raises((ValueError, WorkflowConflict)):
        CurrentDesk(source[1], CASE).assessment(reservation.attempt_id)
    with pytest.raises((ValueError, WorkflowConflict)):
        CurrentDesk(source[1], CASE).snapshot(now=NOW)


def test_dimension_states_distinguish_unassessed_baseline_and_completed_review(sdk_case):
    source, dispatch, planner, _ = sdk_case
    desk = CurrentDesk(source[1], CASE)
    states = {item["id"]: item["status"] for item in desk.snapshot(now=NOW)["dimensions"]}
    assert states == {"physical": "NOT_ASSESSED", "observation": "BASELINE_PENDING", "field": "NOT_PLANNED", "coverage": "DEGRADED"}
    DispatchRunner(dispatch, planner, clock=lambda: NOW).tick(CASE)
    states = {item["id"]: item["status"] for item in desk.snapshot(now=NOW)["dimensions"]}
    assert states["observation"] == "MONITORING"
    assert states["physical"] == "NOT_ASSESSED" and states["field"] == "NOT_PLANNED"
