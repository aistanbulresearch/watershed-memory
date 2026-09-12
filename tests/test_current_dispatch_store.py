"""Selective dispatch has one durable admission and one atomic completion."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_current_case_store import CASE, MONITOR, NOW, START, action, query, stage
from test_current_case_store import ready as ready
from test_current_delivery_store import failure
from test_current_health_integration import HEALTH_PROFILE, outcome
from test_current_source_health import publish

from watershed_memory.current.attention import AttentionPolicy, AttentionRule
from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import InvocationAllowance
from watershed_memory.current.dispatch_store import DispatchStore

POLICY = AttentionPolicy(
    "dispatch-fixture", (AttentionRule("00060", "ft^3/s", "LATEST", Decimal("10"), Decimal("0")),)
)


@pytest.fixture
def dispatch(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    store = DispatchStore(journal)
    store.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    return store


def snapshot(ready):
    with ready[1]._connect() as db:
        names = [
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        return {
            name: [tuple(r) for r in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
            for name in names
        }


def initial(dispatch):
    step = dispatch.prepare(CASE, now=NOW)
    assert step.status == "RESERVED"
    receipt = dispatch.finish(step.reservation.attempt_id, outcome(step.reservation), now=NOW)
    assert receipt.status == "COMMITTED"
    return step


def next_interval(ready, minute, value):
    at = START + timedelta(minutes=minute)
    publish(ready[2], at, observed_at=at - timedelta(minutes=10), value=str(value))
    return at


def test_bootstrap_latest_once_keeps_history_and_global_allowance(ready, dispatch):
    before = ready[2].status(MONITOR)
    ticket = dispatch.prepare(CASE, now=NOW)
    assert ticket.event_id == ready[3][-1].event_id
    assert ticket.attention.reasons == ("INITIAL_REVIEW",)
    assert query(ready[0], "SELECT status FROM watch_outbox ORDER BY interval_start") == [
        ("HISTORY",),
        ("PENDING",),
    ]
    state = dispatch.status(CASE)
    assert state.history_count == 1 and state.assessment_count == 0
    assert state.active_attempt_id == ticket.reservation.attempt_id
    assert dispatch.journal.allowance().provider_used == 1
    assert ready[2].status(MONITOR)["observation_count"] == before["observation_count"]
    reopened = DispatchStore(DeliveryStore(ready[1]))
    assert reopened.prepare(CASE, now=NOW).status == "HELD"
    assert reopened.journal.allowance().provider_used == 1


def test_activation_exact_idempotence_and_no_existing_work_import(ready, dispatch):
    before = snapshot(ready)
    dispatch.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW + timedelta(seconds=1))
    assert snapshot(ready) == before
    for policy, profile in (
        (replace(POLICY, policy_id="other"), HEALTH_PROFILE),
        (POLICY, replace(HEALTH_PROFILE, model_id="other")),
    ):
        with pytest.raises(WorkflowConflict):
            dispatch.activate(CASE, policy, profile, now=NOW)
    assert snapshot(ready) == before


def test_activation_refuses_case_with_existing_human_work(ready):
    stage(ready[1], ready[3][0])
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 1))
    store = DispatchStore(journal)
    before = snapshot(ready)
    with pytest.raises(WorkflowConflict):
        store.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    assert snapshot(ready) == before


@pytest.mark.parametrize(
    "profile",
    [
        replace(HEALTH_PROFILE, instruction_version="watershed-current-v1"),
        replace(HEALTH_PROFILE, mode="SCRIPTED_SDK"),
    ],
)
def test_activation_requires_health_and_truthful_execution_mode(ready, profile):
    store = DispatchStore(DeliveryStore(ready[1], allowance=InvocationAllowance(1, 1)))
    before = snapshot(ready)
    with pytest.raises(ValueError):
        store.activate(CASE, POLICY, profile, now=NOW)
    assert snapshot(ready) == before


def test_exhausted_first_admission_rolls_back_entire_bootstrap(ready):
    from watershed_memory.current.delivery_types import AllowanceExceeded

    store = DispatchStore(DeliveryStore(ready[1], allowance=InvocationAllowance(0, 0)))
    store.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    before = snapshot(ready)
    with pytest.raises(AllowanceExceeded):
        store.prepare(CASE, now=NOW)
    assert snapshot(ready) == before


def test_committed_baseline_survives_quiet_source_and_accumulates_small_changes(ready, dispatch):
    first = initial(dispatch)
    for minute, value in ((45, 7), (60, 11)):
        at = next_interval(ready, minute, value)
        step = dispatch.prepare(CASE, now=at)
        assert step.status == "SUPPRESSED" and not step.attention.eligible
        state = dispatch.status(CASE)
        assert state.numeric_event_id == first.event_id and state.assessment_count == 1
    assert dispatch.status(CASE).suppressed_count == 2
    at = next_interval(ready, 75, 14)
    step = dispatch.prepare(CASE, now=at)
    assert step.status == "RESERVED"
    assert step.attention.reasons == ("MATERIAL_CHANGE",)
    assert step.attention.basis_event_id == first.event_id
    assert dispatch.journal.allowance().provider_used == 2


def test_quiet_clock_has_no_writes_and_quiet_source_is_consumed_once(ready, dispatch):
    initial(dispatch)
    before = snapshot(ready)
    step = dispatch.prepare(CASE, now=NOW + timedelta(seconds=1))
    assert step.status == "QUIET" and snapshot(ready) == before
    at = next_interval(ready, 45, 6)
    quiet = dispatch.prepare(CASE, now=at)
    assert quiet.status == "SUPPRESSED"
    before = snapshot(ready)
    assert dispatch.prepare(CASE, now=at).status == "QUIET"
    assert snapshot(ready) == before


def test_clock_source_age_transition_is_assessed_once(ready, dispatch):
    initial(dispatch)
    at = NOW + timedelta(minutes=51)
    step = dispatch.prepare(CASE, now=at)
    assert step.status == "RESERVED" and not step.reservation.source_delivery
    assert step.attention.reasons == ("COVERAGE_CHANGED",)
    dispatch.finish(step.reservation.attempt_id, outcome(step.reservation), now=at)
    assert dispatch.prepare(CASE, now=at + timedelta(minutes=1)).status == "QUIET"
    assert dispatch.status(CASE).assessment_count == 2


def test_concurrent_prepare_has_one_reservation_and_one_held(ready, dispatch):
    with ThreadPoolExecutor(max_workers=2) as pool:
        steps = list(pool.map(lambda _: dispatch.prepare(CASE, now=NOW), range(2)))
    assert sorted(s.status for s in steps) == ["HELD", "RESERVED"]
    assert dispatch.journal.allowance().provider_used == 1


def test_failure_is_held_across_restart_without_retry_or_refund(ready, dispatch):
    step = dispatch.prepare(CASE, now=NOW)
    dispatch.fail(step.reservation.attempt_id, failure(step.reservation), now=NOW)
    reopened = DispatchStore(DeliveryStore(ready[1]))
    before = snapshot(ready)
    assert reopened.prepare(CASE, now=NOW).status == "HELD"
    assert reopened.status(CASE).active_status == "FAILED"
    assert snapshot(ready) == before and reopened.journal.allowance().provider_used == 1


def test_human_change_during_inference_stales_without_losing_plan(ready, dispatch):
    step = dispatch.prepare(CASE, now=NOW)
    plan = stage(ready[1], ready[3][1])
    approved = action(ready[1], plan)
    receipt = dispatch.finish(step.reservation.attempt_id, outcome(step.reservation), now=NOW)
    assert receipt.status == "STALE" and receipt.work == ()
    assert ready[1].get_review(CASE, plan.task_id) == approved
    assert dispatch.status(CASE).assessment_count == 0
    assert dispatch.prepare(CASE, now=NOW).status == "HELD"
    assert query(
        ready[0], "SELECT status FROM watch_outbox WHERE event_id=?", (step.event_id,)
    ) == [("PENDING",)]


def test_downstream_failure_rolls_back_journal_work_ack_and_dispatch_state(ready, dispatch):
    step = dispatch.prepare(CASE, now=NOW)
    with ready[1]._connect() as db:
        db.execute(
            "CREATE TRIGGER fixture_abort BEFORE INSERT ON dispatch_source_outcomes "
            "WHEN NEW.kind='ASSESSED' BEGIN SELECT RAISE(ABORT,'fixture downstream failure'); END"
        )
    before = snapshot(ready)
    with pytest.raises(Exception, match="fixture downstream failure"):
        dispatch.finish(step.reservation.attempt_id, outcome(step.reservation), now=NOW)
    assert snapshot(ready) == before
    assert dispatch.journal.get(CASE, step.reservation.request_id).status == "RESERVED"


def test_exact_duplicate_finish_does_not_rewind_newer_baseline(ready, dispatch):
    first = initial(dispatch)
    first_receipt = dispatch.journal.get(CASE, first.reservation.request_id)
    at = next_interval(ready, 45, 20)
    second = dispatch.prepare(CASE, now=at)
    dispatch.finish(second.reservation.attempt_id, outcome(second.reservation), now=at)
    before = snapshot(ready)
    duplicate = dispatch.finish(first.reservation.attempt_id, outcome(first.reservation), now=at)
    assert duplicate == first_receipt and snapshot(ready) == before
    assert dispatch.status(CASE).numeric_event_id == second.event_id


@pytest.mark.parametrize("field", ["attention_json", "replay_json", "policy_json"])
def test_corrupted_admission_recipe_or_policy_fails_before_work(ready, dispatch, field):
    step = dispatch.prepare(CASE, now=NOW)
    table = "dispatch_settings" if field == "policy_json" else "dispatch_attempts"
    with ready[1]._connect() as db:
        db.execute(f"UPDATE {table} SET {field}='{{}}'")
    before = snapshot(ready)
    with pytest.raises((ValueError, WorkflowConflict)):
        dispatch.finish(step.reservation.attempt_id, outcome(step.reservation), now=NOW)
    assert snapshot(ready) == before


def test_quiet_receipt_retains_exact_current_baselines_and_handled_keys(ready, dispatch):
    from watershed_memory.current.dispatch_replay import restore_admission

    first = initial(dispatch)
    at = next_interval(ready, 45, 6)
    quiet = dispatch.prepare(CASE, now=at)
    saved = query(
        ready[0],
        "SELECT replay_json,attention_json FROM dispatch_source_outcomes "
        "WHERE case_id=? AND event_id=?",
        (CASE, quiet.event_id),
    )[0]
    data = json.loads(saved[0])
    assert data["numeric"]["event_id"] == first.event_id
    assert data["health"]["event_id"] == first.event_id
    assert data["handled_due_keys"] == []
    next_interval(ready, 60, 30)
    with ready[1]._connect() as db:
        db.execute("BEGIN")
        restored, attention, move_numeric = restore_admission(db, saved[0], saved[1], POLICY)
    assert restored.current.event_id == quiet.event_id
    assert attention == quiet.attention and move_numeric


def plan_outcome(ticket, *, due=None):
    from watershed_memory.current.strands import CurrentExecution
    from watershed_memory.current.tools import CurrentTools

    tools = CurrentTools(ticket.context)
    tools.get_case_context()
    tools.inspect_current_series()
    tools.inspect_source_health()
    reviews = tools.find_relevant_reviews("COVERAGE_REVIEW")["reviews"]
    tools.stage_assessment(
        "CONTINUE_EXISTING_REVIEW" if reviews else "PROPOSE_REVIEW",
        "COVERAGE_REVIEW",
        ticket.context.current.event_id,
        reviews[0]["task_id"] if reviews else None,
        None if reviews else "Verify station coverage",
        "Synthetic dispatch test: verify the missing station series.",
        None if reviews or due is None else due.isoformat(),
        [],
    )
    p = ticket.profile
    return CurrentExecution(
        tools.finish(),
        p.mode,
        p.model_id,
        p.instruction_version,
        p.sdk_version,
        1,
        tools.attempts,
        0.1,
        "{}",
        "end_turn",
    )


def test_due_check_is_handled_once_per_human_plan_revision(ready, dispatch):
    step = dispatch.prepare(CASE, now=NOW)
    due = NOW + timedelta(minutes=5)
    result = dispatch.finish(
        step.reservation.attempt_id, plan_outcome(step.reservation, due=due), now=NOW
    )
    original = result.work[0]
    next_step = dispatch.prepare(CASE, now=due)
    assert next_step.attention.reasons == ("CHECK_DUE",)
    dispatch.finish(next_step.reservation.attempt_id, plan_outcome(next_step.reservation), now=due)
    assert dispatch.prepare(CASE, now=due).status == "QUIET"
    modified = action(
        ready[1],
        original,
        "MODIFY",
        now=due,
        title="Operator plan: verify at the next check",
        next_check_at=due + timedelta(minutes=1),
    )
    third = dispatch.prepare(CASE, now=due + timedelta(minutes=1))
    assert third.attention.reasons == ("CHECK_DUE",)
    assert third.attention.due_checks[0].revision == modified.revision
    assert query(ready[0], "SELECT COUNT(*) FROM dispatch_due") == [(1,)]


@pytest.mark.parametrize("active_link", [False, True])
def test_later_old_correction_is_reviewed_without_rewinding_numeric_baseline(
    ready, dispatch, active_link
):
    step = dispatch.prepare(CASE, now=NOW)
    result = plan_outcome(step.reservation) if active_link else outcome(step.reservation)
    dispatch.finish(step.reservation.attempt_id, result, now=NOW)
    at = next_interval(ready, 45, 20)
    second = dispatch.prepare(CASE, now=at)
    result = plan_outcome(second.reservation) if active_link else outcome(second.reservation)
    dispatch.finish(second.reservation.attempt_id, result, now=at)
    later = START + timedelta(minutes=50)
    publish(
        ready[2],
        later,
        observed_at=START + timedelta(minutes=20),
        value="40",
        provider="correction",
    )
    corrected = dispatch.prepare(CASE, now=later)
    assert corrected.status == "RESERVED"
    assert ("SOURCE_CORRECTION" if active_link else "LATE_EVIDENCE") in corrected.attention.reasons
    assert corrected.attention.basis_event_id == second.event_id
    result = plan_outcome(corrected.reservation) if active_link else outcome(corrected.reservation)
    dispatch.finish(corrected.reservation.attempt_id, result, now=later)
    assert dispatch.status(CASE).numeric_event_id == second.event_id
    assert dispatch.status(CASE).health_evaluated_at == later
    assert dispatch.prepare(CASE, now=later).status == "QUIET"


def test_same_interval_correction_does_not_interrupt_again_after_assessment(ready, dispatch):
    first = dispatch.prepare(CASE, now=NOW)
    dispatch.finish(first.reservation.attempt_id, plan_outcome(first.reservation), now=NOW)
    at = NOW + timedelta(minutes=5)
    publish(
        ready[2], at, observed_at=START + timedelta(minutes=20), value="5", provider="correction"
    )
    corrected = dispatch.prepare(CASE, now=at)
    assert corrected.attention.reasons == ("SOURCE_CORRECTION",)
    dispatch.finish(corrected.reservation.attempt_id, plan_outcome(corrected.reservation), now=at)
    assert dispatch.status(CASE).numeric_event_id == corrected.event_id
    assert dispatch.prepare(CASE, now=at).status == "QUIET"


def test_no_source_waits_without_a_baseline_or_charge(tmp_path):
    from watershed_memory.current.case_store import CaseStore
    from watershed_memory.current.case_types import CaseConfig
    from watershed_memory.current.fact_types import CoveragePolicy
    from watershed_memory.watch.store import MonitorConfig, WatchStore

    path = tmp_path / "empty.sqlite"
    watch = WatchStore(path)
    watch.register(MonitorConfig(MONITOR, "USGS-08380500", CASE, START), now=START)
    cases = CaseStore(path)
    cases.register(CaseConfig(CASE, MONITOR, CoveragePolicy("empty", ("00060",)), False), now=NOW)
    store = DispatchStore(DeliveryStore(cases, allowance=InvocationAllowance(0, 1)))
    store.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    before = snapshot((path, cases, watch, ()))
    assert store.prepare(CASE, now=NOW).status == "WAITING_FOR_SOURCE"
    assert snapshot((path, cases, watch, ())) == before


def test_changed_dispatch_baseline_cannot_be_used_to_complete_original_admission(ready, dispatch):
    initial(dispatch)
    at = next_interval(ready, 45, 20)
    step = dispatch.prepare(CASE, now=at)
    from watershed_memory.current.context_reference import capture_context, encode_reference

    wrong = encode_reference(capture_context(step.reservation.context))
    with ready[1]._connect() as db:
        db.execute(
            "UPDATE dispatch_settings SET numeric_context_json=?,health_context_json=?",
            (wrong, wrong),
        )
    before = snapshot(ready)
    with pytest.raises(WorkflowConflict):
        dispatch.finish(step.reservation.attempt_id, outcome(step.reservation), now=at)
    assert snapshot(ready) == before
