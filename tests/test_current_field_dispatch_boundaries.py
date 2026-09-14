"""Boundary regressions for v3 dispatch ordering, due checks, and ownership."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_case_store import CASE, MONITOR, NOW, START, action, query
from test_current_case_store import ready as ready
from test_current_dispatch_store import POLICY
from test_current_field_dispatch_store import V3_PROFILE, failed, snapshot
from test_current_field_store import SITE
from test_current_health_integration import HEALTH_PROFILE
from test_current_source_health import publish

from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import InvocationAllowance
from watershed_memory.current.dispatch_store import DispatchStore
from watershed_memory.current.field_delivery_store import FieldDeliveryStore
from watershed_memory.current.field_delivery_types import CurrentExecutionV3
from watershed_memory.current.field_dispatch_store import FieldDispatchStore
from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.field_tools import CurrentToolsV3
from watershed_memory.current.locations import LocationRegistry


@pytest.fixture
def dispatch(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    source = DispatchStore(journal)
    source.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    fields = FieldStore(ready[1], LocationRegistry((replace(SITE, case_id=CASE),)))
    store = FieldDispatchStore(source, FieldDeliveryStore(journal, fields))
    store.upgrade_to_v3(CASE, V3_PROFILE, now=NOW)
    return store


def plan_execution(ticket, *, due=None):
    tools = CurrentToolsV3(ticket.context)
    source = tools.source
    source.get_case_context()
    source.inspect_current_series()
    source.inspect_source_health()
    reviews = source.find_relevant_reviews("COVERAGE_REVIEW")["reviews"]
    source.stage_assessment(
        "CONTINUE_EXISTING_REVIEW" if reviews else "PROPOSE_REVIEW",
        "COVERAGE_REVIEW",
        ticket.context.base.current.event_id,
        reviews[0]["task_id"] if reviews else None,
        None if reviews else "Verify station coverage",
        "Synthetic v3 dispatch test: verify the missing station series.",
        None if reviews or due is None else due.isoformat(),
        [],
    )
    tools.get_field_context()
    tools.stage_field_decision(
        "NO_NEW_FIELD_PLAN",
        None,
        None,
        None,
        None,
        "No field-ledger state requires a new assignment.",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    return CurrentExecutionV3(
        tools.finish(),
        V3_PROFILE.mode,
        V3_PROFILE.model_id,
        V3_PROFILE.instruction_version,
        V3_PROFILE.sdk_version,
        1,
        tools.attempts,
        0.0,
        "{}",
        "end_turn",
    )


def test_due_check_and_equal_interval_revisions_are_handled_in_exact_order(ready, dispatch):
    due = NOW + timedelta(minutes=15)
    initial = dispatch.prepare(CASE, now=NOW)
    first = dispatch.finish(
        initial.reservation.attempt_id,
        plan_execution(initial.reservation, due=due),
        now=NOW,
    )
    review = first.delivery.work[0]
    assert dispatch.journal.allowance().provider_used == 1

    before_quiet = snapshot(ready)
    assert dispatch.prepare(CASE, now=NOW + timedelta(minutes=1)).status == "QUIET"
    assert snapshot(ready) == before_quiet
    assert dispatch.journal.allowance().provider_used == 1

    observed_at = START + timedelta(minutes=20)
    publish(
        ready[2],
        NOW + timedelta(minutes=5),
        observed_at=observed_at,
        value="5",
        provider="equal-interval-revision-2",
    )
    publish(
        ready[2],
        NOW + timedelta(minutes=10),
        observed_at=observed_at,
        value="6",
        provider="equal-interval-revision-3",
    )
    pending = ready[2].pending_events(MONITOR)
    assert tuple(item.revision for item in pending) == (2, 3)
    assert pending[0].start == pending[1].start

    due_step = dispatch.prepare(CASE, now=due)
    assert due_step.event_id == pending[0].event_id
    assert "CHECK_DUE" in due_step.attention.reasons
    due_check = due_step.attention.due_checks[0]
    assert (due_check.task_id, due_check.revision, due_check.next_check_at) == (
        review.task_id,
        review.revision,
        due,
    )
    dispatch.finish(
        due_step.reservation.attempt_id,
        plan_execution(due_step.reservation),
        now=due,
    )
    assert query(
        ready[0],
        "SELECT due_key,task_id,revision,next_check_at,handled_at,attempt_id FROM dispatch_due",
    ) == [
        (
            due_check.key,
            review.task_id,
            review.revision,
            due.isoformat(),
            due.isoformat(),
            due_step.reservation.attempt_id,
        )
    ]

    same_time = dispatch.prepare(CASE, now=due)
    assert same_time.event_id == pending[1].event_id
    assert "CHECK_DUE" not in same_time.attention.reasons
    assert same_time.attention.due_checks == ()
    dispatch.finish(
        same_time.reservation.attempt_id,
        plan_execution(same_time.reservation),
        now=due,
    )
    assert query(ready[0], "SELECT COUNT(*) FROM dispatch_due") == [(1,)]
    allowance = dispatch.journal.allowance()
    before_quiet = snapshot(ready)
    assert dispatch.prepare(CASE, now=due).status == "QUIET"
    assert snapshot(ready) == before_quiet
    assert dispatch.journal.allowance() == allowance


@pytest.mark.parametrize("terminal", ["STALE", "FAILED"])
def test_stale_and_failed_due_attempts_do_not_advance_baselines_or_due(ready, dispatch, terminal):
    due = NOW + timedelta(minutes=5)
    initial = dispatch.prepare(CASE, now=NOW)
    first = dispatch.finish(
        initial.reservation.attempt_id,
        plan_execution(initial.reservation, due=due),
        now=NOW,
    )
    before = dispatch.status(CASE)
    due_step = dispatch.prepare(CASE, now=due)
    assert due_step.attention.reasons == ("CHECK_DUE",)
    run = plan_execution(due_step.reservation)
    if terminal == "STALE":
        action(
            ready[1],
            first.delivery.work[0],
            "MODIFY",
            request="human-reschedules-due-v3",
            now=due,
            title="Operator rescheduled this source review",
            next_check_at=due + timedelta(minutes=5),
        )
        receipt = dispatch.finish(due_step.reservation.attempt_id, run, now=due)
    else:
        receipt = dispatch.fail(
            due_step.reservation.attempt_id, failed(due_step.reservation), now=due
        )
    assert receipt.delivery.status == terminal
    after = dispatch.status(CASE)
    assert (
        after.numeric_event_id,
        after.health_evaluated_at,
        after.assessment_count,
        after.suppressed_count,
    ) == (
        before.numeric_event_id,
        before.health_evaluated_at,
        before.assessment_count,
        before.suppressed_count,
    )
    assert after.active_attempt_id == due_step.reservation.attempt_id
    assert after.active_status == terminal
    assert query(ready[0], "SELECT COUNT(*) FROM dispatch_due") == [(0,)]


def test_upgrade_rejects_lower_level_v3_delivery_without_adopting_it(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    source = DispatchStore(journal)
    source.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    fields = FieldStore(ready[1], LocationRegistry((replace(SITE, case_id=CASE),)))
    delivery = FieldDeliveryStore(journal, fields)
    store = FieldDispatchStore(source, delivery)
    external = delivery.reserve(
        CASE,
        ready[3][-1].event_id,
        request_id="lower-level-v3-delivery",
        profile=V3_PROFILE,
        source_delivery=False,
        now=NOW,
    )
    before = snapshot(ready)
    with pytest.raises(WorkflowConflict, match="outside dispatch"):
        store.upgrade_to_v3(CASE, V3_PROFILE, now=NOW)
    assert snapshot(ready) == before
    assert delivery.get(CASE, external.request_id).delivery.status == "RESERVED"
    assert journal.allowance().provider_used == 1


def test_malformed_pinned_v3_profile_fails_closed_without_writes(ready, dispatch):
    with ready[1]._connect() as db:
        db.execute("UPDATE dispatch_settings SET profile_json='{}' WHERE case_id=?", (CASE,))
        db.execute(
            "UPDATE context3_dispatch_settings SET v3_profile_json='{}' WHERE case_id=?",
            (CASE,),
        )
    before = snapshot(ready)
    for operation in (
        lambda: dispatch.status(CASE),
        lambda: dispatch.prepare(CASE, now=NOW),
    ):
        with pytest.raises(ValueError, match="invalid dispatch profile fields"):
            operation()
        assert snapshot(ready) == before
