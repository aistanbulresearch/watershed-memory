"""Synthetic executions exercise the journal, never a paid model or cloud service."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_case_store import CASE, CONFIG, LATER, MONITOR, NOW, action, query, stage
from test_current_case_store import ready as ready
from test_current_context import add_interval

from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import (
    AllowanceExceeded,
    DeliveryHeld,
    DeliveryReceipt,
    DeliveryReservation,
    InvocationAllowance,
    InvocationProfile,
)
from watershed_memory.current.strands import CurrentExecution, CurrentFailure
from watershed_memory.current.tools import CurrentTools

PROFILE = InvocationProfile(
    "STRANDS_CURRENT", "synthetic-envelope", "watershed-current-v1", "fixture"
)
SCRIPTED = replace(PROFILE, mode="SCRIPTED_SDK")


@pytest.fixture
def journal(ready):
    return DeliveryStore(ready[1], allowance=InvocationAllowance(4, 4))


def reserve(journal, ready, **changes):
    fields = dict(request_id="source-1", profile=PROFILE, source_delivery=True, now=NOW)
    fields.update(changes)
    return journal.reserve(CASE, ready[3][1].event_id, **fields)


def execution(reservation, kinds=("COVERAGE_REVIEW",)):
    tools = CurrentTools(reservation.context)
    tools.get_case_context()
    tools.inspect_current_series()
    for kind in kinds or (None,):
        reviews = tools.find_relevant_reviews(kind)["reviews"] if kind else []
        tools.stage_assessment(
            disposition="CONTINUE_EXISTING_REVIEW"
            if reviews
            else ("PROPOSE_REVIEW" if kind else "NO_FOLLOW_UP"),
            kind=kind,
            event_id=reservation.context.current.event_id,
            target_task_id=reviews[0]["task_id"] if reviews else None,
            title="Review the station evidence" if kind and not reviews else None,
            reason="Synthetic fixture: inspect the available measurements and preserve the human plan.",
            next_check_at=None,
            reference_ids=[],
        )
    profile = reservation.profile
    return CurrentExecution(
        tools.finish(),
        profile.mode,
        profile.model_id,
        profile.instruction_version,
        profile.sdk_version,
        1,
        tools.attempts,
        0.1,
        "{}",
        "end_turn",
    )


def failure(reservation):
    ctx, profile = reservation.context, reservation.profile
    return CurrentFailure(
        ctx.case_id,
        ctx.case_revision,
        ctx.policy_digest,
        ctx.current.event_id,
        profile.mode,
        profile.model_id,
        profile.instruction_version,
        profile.sdk_version,
        1,
        0,
        (),
        "{}",
    )


def test_reservation_is_durable_before_invocation_and_restart_cannot_reset_budget(ready, journal):
    path, cases, watch, _ = ready
    original = watch.status(MONITOR)
    ticket = reserve(journal, ready)
    assert type(ticket) is DeliveryReservation
    assert query(path, "SELECT status,case_revision FROM delivery_attempts") == [("RESERVED", 0)]
    restored = DeliveryStore(CaseStore(path))
    assert restored.get(CASE, "source-1").status == "RESERVED"
    assert restored.allowance().provider_used == 1
    with pytest.raises(DeliveryHeld):
        reserve(restored, ready, now=LATER)
    with pytest.raises(DeliveryHeld):
        reserve(restored, ready, request_id="source-other")
    assert cases.context(CASE)["revision"] == 0 and watch.status(MONITOR) == original


def test_two_kind_work_receipt_and_source_ack_are_committed_together(ready, journal):
    path, cases, watch, _ = ready
    before = watch.status(MONITOR)["pending_count"]
    ticket = reserve(journal, ready)
    result = journal.commit(
        ticket.attempt_id, execution(ticket, ("COVERAGE_REVIEW", "OBSERVATION_REVIEW")), now=NOW
    )
    assert result.status == "COMMITTED" and len(result.work) == 2
    assert {item.kind for item in result.work} == {"COVERAGE_REVIEW", "OBSERVATION_REVIEW"}
    assert len(cases.context(CASE)["active_reviews"]) == 2
    assert cases.context(CASE)["revision"] == 3
    assert watch.status(MONITOR)["pending_count"] == before - 1
    assert query(
        path, "SELECT status FROM watch_outbox WHERE event_id=?", (ticket.context.current.event_id,)
    ) == [("DONE",)]
    assert query(path, "SELECT COUNT(*) FROM current_receipts") == [(2,)]
    assert query(path, "PRAGMA foreign_key_check") == []
    assert query(path, "PRAGMA integrity_check") == [("ok",)]
    assert DeliveryStore(CaseStore(path)).get(CASE, "source-1") == result


def test_duplicate_returns_original_receipt_after_human_change_without_another_call(ready, journal):
    _, cases, _, _ = ready
    ticket = reserve(journal, ready)
    outcome = execution(ticket)
    original = journal.commit(ticket.attempt_id, outcome, now=NOW)
    changed = action(
        cases, original.work[0], "MODIFY", title="The operator's changed plan", next_check_at=LATER
    )
    assert changed.title != original.work[0].title
    assert journal.commit(ticket.attempt_id, outcome, now=LATER + timedelta(days=40)) == original
    assert reserve(journal, ready, now=LATER + timedelta(days=40)) == original
    assert journal.allowance().provider_used == 1
    with pytest.raises(WorkflowConflict):
        journal.commit(ticket.attempt_id, replace(outcome, elapsed_seconds=0.2), now=NOW)


@pytest.mark.parametrize("boundary", ["review", "source", "receipt"])
def test_error_at_any_commit_boundary_rolls_back_work_ack_and_case_revision(
    ready, journal, boundary
):
    path, cases, watch, _ = ready
    ticket = reserve(journal, ready)
    before = watch.status(MONITOR), cases.context(CASE), journal.get(CASE, "source-1")
    triggers = {
        "review": "AFTER INSERT ON current_reviews WHEN NEW.kind='OBSERVATION_REVIEW'",
        "source": "AFTER UPDATE OF status ON watch_outbox WHEN NEW.status='DONE'",
        "receipt": "AFTER UPDATE OF status ON delivery_attempts WHEN NEW.status='COMMITTED'",
    }
    with cases._connect() as db:
        db.execute(
            "CREATE TRIGGER test_abort_delivery "
            + triggers[boundary]
            + " BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    with pytest.raises(Exception, match="injected failure"):
        journal.commit(
            ticket.attempt_id, execution(ticket, ("COVERAGE_REVIEW", "OBSERVATION_REVIEW")), now=NOW
        )
    assert before == (watch.status(MONITOR), cases.context(CASE), journal.get(CASE, "source-1"))
    assert query(path, "SELECT COUNT(*) FROM current_receipts") == [(0,)]
    assert query(path, "SELECT COUNT(*) FROM current_reviews") == [(0,)]
    assert journal.allowance().provider_used == 1


def test_human_change_during_invocation_wins_and_holds_the_unapplied_execution(ready, journal):
    _, cases, watch, events = ready
    ticket = reserve(journal, ready)
    outcome = execution(ticket)
    first = stage(cases, events[0])
    human = action(cases, first, "DEFER", next_check_at=LATER)
    before = watch.status(MONITOR), cases.context(CASE)
    result = journal.commit(ticket.attempt_id, outcome, now=NOW)
    assert result.status == "STALE" and result.work == () and result.execution_json
    assert before == (watch.status(MONITOR), cases.context(CASE))
    assert cases.get_review(CASE, human.task_id) == human
    with pytest.raises(DeliveryHeld):
        reserve(journal, ready, request_id="automatic-retry")


def test_human_change_cannot_bypass_reserved_tool_trace_validation(ready, journal):
    _, cases, _, events = ready
    ticket = reserve(journal, ready)
    outcome = execution(ticket)
    stage(cases, events[0])
    forged = (replace(outcome.assessment.trace[0], output_json="{}"), *outcome.assessment.trace[1:])
    outcome = replace(outcome, assessment=replace(outcome.assessment, trace=forged))
    with pytest.raises(ValueError):
        journal.commit(ticket.attempt_id, outcome, now=NOW)
    assert journal.get(CASE, "source-1").status == "RESERVED"


def test_stale_execution_is_replayed_against_the_actual_earlier_human_plan(ready, journal):
    _, cases, watch, events = ready
    first = stage(cases, events[0])
    ticket = reserve(journal, ready)
    outcome = execution(ticket)
    human = action(
        cases,
        first,
        "MODIFY",
        title="A later operator revision",
        next_check_at=LATER + timedelta(hours=1),
        now=LATER,
    )
    result = journal.commit(ticket.attempt_id, outcome, now=LATER)
    assert result.status == "STALE" and result.work == ()
    assert cases.get_review(CASE, human.task_id) == human
    assert watch.status(MONITOR)["pending_count"] == 2


def test_corrupted_reserved_evidence_is_rejected_instead_of_silently_filtered(ready, journal):
    _, cases, _, events = ready
    stage(cases, events[0])
    ticket = reserve(journal, ready)
    outcome = execution(ticket)
    with cases._connect() as db:
        raw = db.execute(
            "SELECT review_refs_json FROM delivery_attempts WHERE attempt_id=?",
            (ticket.attempt_id,),
        ).fetchone()[0]
        refs = json.loads(raw)
        refs[0][2].append("f" * 64)
        db.execute(
            "UPDATE delivery_attempts SET review_refs_json=? WHERE attempt_id=?",
            (json.dumps(refs), ticket.attempt_id),
        )
    with pytest.raises(ValueError):
        journal.commit(ticket.attempt_id, outcome, now=NOW)
    assert journal.get(CASE, "source-1").status == "RESERVED"


def test_continuation_keeps_the_specific_human_plan_and_original_task_revision(ready, journal):
    _, cases, _, events = ready
    first = stage(cases, events[0])
    human = action(
        cases,
        first,
        "MODIFY",
        title="Check station publication at the next shift",
        next_check_at=LATER,
    )
    ticket = reserve(journal, ready)
    result = journal.commit(ticket.attempt_id, execution(ticket), now=NOW)
    assert result.work == (human,)
    assert cases.get_review(CASE, human.task_id) == human
    assert set(cases.evidence(CASE, human.task_id)) == {e.event_id for e in events}


def test_no_follow_up_is_a_saved_decision_without_creating_work(ready, journal):
    _, cases, watch, _ = ready
    ticket = reserve(journal, ready)
    result = journal.commit(ticket.attempt_id, execution(ticket, ()), now=NOW)
    assert result.status == "COMMITTED" and result.work == ()
    assert cases.context(CASE)["revision"] == 1
    assert cases.context(CASE)["active_reviews"] == ()
    assert watch.status(MONITOR)["pending_count"] == 1


def test_failure_is_held_and_explicit_abandonment_does_not_refund_allowance(ready, journal):
    _, cases, watch, _ = ready
    ticket = reserve(journal, ready)
    failed = journal.fail(ticket.attempt_id, failure(ticket), now=NOW)
    assert failed.status == "FAILED" and failed.failure_json
    assert journal.fail(ticket.attempt_id, failure(ticket), now=LATER) == failed
    with pytest.raises(DeliveryHeld):
        reserve(journal, ready, request_id="silent-retry")
    reason = "Explicit fixture reconciliation permits a new bounded attempt."
    abandoned = journal.abandon(CASE, "source-1", reason=reason, now=NOW)
    assert abandoned.status == "ABANDONED"
    assert journal.abandon(CASE, "source-1", reason=reason, now=LATER) == abandoned
    assert journal.allowance().provider_used == 1 and watch.status(MONITOR)["pending_count"] == 2
    with pytest.raises(WorkflowConflict):
        journal.commit(ticket.attempt_id, execution(ticket), now=NOW)
    with pytest.raises(DeliveryHeld):
        reserve(journal, ready)
    assert type(reserve(journal, ready, request_id="explicit-next")) is DeliveryReservation
    assert journal.allowance().provider_used == 2 and cases.context(CASE)["revision"] == 0


def test_process_death_reservation_requires_explicit_reconciliation(ready, journal):
    path, _, _, _ = ready
    ticket = reserve(journal, ready)
    reopened = DeliveryStore(CaseStore(path))
    assert reopened.get(CASE, "source-1").attempt_id == ticket.attempt_id
    with pytest.raises(DeliveryHeld):
        reserve(reopened, ready, now=LATER)
    reopened.abandon(
        CASE, "source-1", reason="Original process ended; no result is recoverable.", now=LATER
    )
    assert reopened.allowance().provider_used == 1


@pytest.mark.parametrize("which", ["mode", "model", "event", "trace"])
def test_mismatched_or_forged_execution_cannot_commit(ready, journal, which):
    ticket = reserve(journal, ready)
    outcome = execution(ticket)
    if which == "mode":
        outcome = replace(outcome, mode="SCRIPTED_SDK")
    elif which == "model":
        outcome = replace(outcome, model_id="unreserved-model")
    elif which == "event":
        decisions = tuple(replace(d, event_id="f" * 64) for d in outcome.assessment.decisions)
        outcome = replace(
            outcome, assessment=replace(outcome.assessment, event_id="f" * 64, decisions=decisions)
        )
    else:
        trace = (
            replace(outcome.assessment.trace[0], output_json="{}"),
            *outcome.assessment.trace[1:],
        )
        outcome = replace(outcome, assessment=replace(outcome.assessment, trace=trace))
    with pytest.raises(ValueError):
        journal.commit(ticket.attempt_id, outcome, now=NOW)
    assert journal.get(CASE, "source-1").status == "RESERVED"


def test_isolated_scripted_case_cannot_acknowledge_canonical_source(ready, journal):
    _, cases, watch, events = ready
    cases.register(replace(CONFIG, case_id="isolated", simulated=True), now=NOW)
    with pytest.raises(ValueError):
        reserve(journal, ready, profile=SCRIPTED)
    with pytest.raises(ValueError):
        journal.reserve(
            "isolated",
            events[1].event_id,
            request_id="bad-ack",
            profile=SCRIPTED,
            source_delivery=True,
            now=NOW,
        )
    ticket = journal.reserve(
        "isolated",
        events[1].event_id,
        request_id="demo",
        profile=SCRIPTED,
        source_delivery=False,
        now=NOW,
    )
    result = journal.commit(ticket.attempt_id, execution(ticket), now=NOW)
    assert result.work[0].simulated and result.work[0].case_id == "isolated"
    assert watch.status(MONITOR)["pending_count"] == 2
    assert cases.context(CASE)["active_reviews"] == ()
    assert journal.allowance().scripted_used == 1 and journal.allowance().provider_used == 0


def test_invalid_requests_leave_allowance_and_attempt_table_unchanged(ready, journal):
    path, _, _, _ = ready
    for changes in (
        {"source_delivery": 1},
        {"request_id": "../bad"},
        {"now": NOW.replace(tzinfo=None)},
    ):
        with pytest.raises(ValueError):
            reserve(journal, ready, **changes)
    with pytest.raises(KeyError):
        journal.reserve(
            CASE, "f" * 64, request_id="unknown", profile=PROFILE, source_delivery=True, now=NOW
        )
    assert query(path, "SELECT COUNT(*) FROM delivery_attempts") == [(0,)]
    assert journal.allowance().provider_used == 0


def test_same_request_concurrency_emits_exactly_one_invocation_capability(ready, journal):
    def attempt(_):
        try:
            return reserve(journal, ready)
        except DeliveryHeld:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(type(item) is DeliveryReservation for item in results) == 1
    assert journal.allowance().provider_used == 1


def test_global_allowance_survives_new_cases_and_concurrent_requests(ready):
    _, cases, _, events = ready
    journal = DeliveryStore(cases, allowance=InvocationAllowance(0, 1))
    for case in ("case-a", "case-b"):
        cases.register(replace(CONFIG, case_id=case, simulated=True), now=NOW)

    def attempt(case):
        try:
            return journal.reserve(
                case,
                events[1].event_id,
                request_id="r",
                profile=PROFILE,
                source_delivery=False,
                now=NOW,
            )
        except AllowanceExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("case-a", "case-b")))
    assert sum(type(item) is DeliveryReservation for item in results) == 1
    assert journal.allowance().provider_used == 1


def test_late_prior_arrival_cannot_replace_the_reserved_tool_context(ready, journal):
    path, _, watch, _ = ready
    current = add_interval(path, 45)
    evaluated = NOW + timedelta(hours=2)
    ticket = journal.reserve(
        CASE,
        current.event_id,
        request_id="explicit-saved",
        profile=PROFILE,
        source_delivery=False,
        now=evaluated,
    )
    add_interval(path, 30)
    result = journal.commit(ticket.attempt_id, execution(ticket), now=evaluated)
    assert type(result) is DeliveryReceipt and result.status == "COMMITTED"
    assert watch.status(MONITOR)["pending_count"] == 2
