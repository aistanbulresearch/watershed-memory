"""Field-aware dispatch preserves v2 attention while sealing v3 delivery."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_current_case_store import CASE, NOW, query, stage
from test_current_case_store import ready as ready
from test_current_delivery_store import failure as legacy_failure
from test_current_dispatch_store import POLICY, next_interval, outcome
from test_current_field_store import SITE, SPEC
from test_current_field_tools import arguments, source_ready
from test_current_health_integration import HEALTH_PROFILE

from watershed_memory.current.case_records import digest
from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import InvocationAllowance
from watershed_memory.current.dispatch_store import DispatchStore
from watershed_memory.current.field_delivery_store import FieldDeliveryStore
from watershed_memory.current.field_delivery_types import CurrentExecutionV3, CurrentFailureV3
from watershed_memory.current.field_dispatch_store import FieldDispatchStore
from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.field_tools import CurrentToolsV3
from watershed_memory.current.field_types import (
    DecideFieldPlan,
    FieldPrincipal,
    ProposeFieldPlan,
    ReportFieldResult,
)
from watershed_memory.current.locations import LocationRegistry
from watershed_memory.current.tools import _json

V3_PROFILE = replace(HEALTH_PROFILE, instruction_version="watershed-current-v3")
COORDINATOR = FieldPrincipal("dispatch-coordinator", ("COORDINATOR",), (CASE,))
OPERATOR = FieldPrincipal("dispatch-operator", ("FIELD_OPERATOR",), (CASE,))


@pytest.fixture
def field_dispatch(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    dispatch = DispatchStore(journal)
    dispatch.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    fields = FieldStore(ready[1], LocationRegistry((replace(SITE, case_id=CASE),)))
    delivery = FieldDeliveryStore(journal, fields)
    return FieldDispatchStore(dispatch, delivery)


def snapshot(ready):
    with ready[1]._connect() as db:
        names = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        return {
            name: [tuple(row) for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
            for name in names
        }


def execution(ticket):
    tools = CurrentToolsV3(ticket.context)
    source = tools.source
    source.get_case_context()
    source.inspect_current_series()
    source.inspect_source_health()
    source.stage_assessment(
        "NO_FOLLOW_UP",
        None,
        ticket.context.base.current.event_id,
        None,
        None,
        "No new source review is needed for this synthetic dispatch turn.",
        None,
        [],
    )
    tools.get_field_context()
    tools.stage_field_decision(
        "NO_NEW_FIELD_PLAN",
        None,
        None,
        None,
        None,
        "No selected field history requires another plan.",
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


def proposal_execution(ticket):
    tools = CurrentToolsV3(ticket.context)
    source_ready(tools)
    tools.get_field_context()
    snapshots = (
        *ticket.context.field_work.current_plans,
        *ticket.context.field_work.stranded_plans,
    )
    for item in snapshots:
        tools.inspect_field_work(item.plan.plan_id)
    basis = ticket.context.field_work.latest_results[0]
    tools.inspect_field_work(basis.plan.plan_id)
    tools.list_approved_field_locations("VISUAL_INSPECTION")
    tools.stage_field_decision(**arguments(ticket.context, "PROPOSE_FIELD_PLAN", basis, True))
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


def failed(ticket):
    base = ticket.context.base
    return CurrentFailureV3(
        base.case_id,
        base.case_revision,
        base.policy_digest,
        base.current.event_id,
        digest(_json(ticket.context)),
        V3_PROFILE.mode,
        V3_PROFILE.model_id,
        V3_PROFILE.instruction_version,
        V3_PROFILE.sdk_version,
        0,
        0,
        (),
        (),
        "{}",
    )


def prepare_partial_field_history(ready):
    _, cases, _, events = ready
    location = replace(
        SITE,
        case_id=CASE,
        simulated=False,
        latitude=Decimal("35.6000"),
        longitude=Decimal("-105.2000"),
        coordinate_system="WGS84",
        coordinate_accuracy="Synthetic test coordinate with fixed precision.",
    )
    fields = FieldStore(cases, LocationRegistry((location,)))
    review = stage(cases, events[0])
    spec = replace(SPEC, window_start=NOW, window_end=NOW + timedelta(hours=2))
    proposed = fields.propose_plan(
        CASE,
        ProposeFieldPlan(review.task_id, review.revision, location.location_id, 1, spec, False),
        principal=COORDINATOR,
        request_id="human-field-proposal",
        expected_case_revision=cases.context(CASE)["revision"],
        now=NOW,
    )
    approved = fields.decide_plan(
        CASE,
        DecideFieldPlan(proposed.plan.plan_id, proposed.plan.revision, "APPROVE"),
        principal=COORDINATOR,
        request_id="human-field-approval",
        expected_case_revision=cases.context(CASE)["revision"],
        now=NOW,
    )
    fields.report_result(
        CASE,
        ReportFieldResult(
            approved.plan.plan_id,
            approved.plan.revision,
            "PARTIAL",
            "The first inspection was only partially completed.",
            NOW,
            NOW + timedelta(minutes=5),
            False,
        ),
        principal=OPERATOR,
        request_id="human-field-report",
        expected_case_revision=cases.context(CASE)["revision"],
        now=NOW + timedelta(minutes=10),
    )
    return fields, review


def test_upgrade_pins_profiles_and_preserves_every_other_dispatch_value(ready, field_dispatch):
    before = snapshot(ready)
    field_dispatch.upgrade_to_v3(CASE, V3_PROFILE, now=NOW + timedelta(seconds=1))
    after = snapshot(ready)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"context3_dispatch_settings", "dispatch_settings"}
    assert set(after) - set(before) == set()
    row = query(
        ready[0],
        "SELECT previous_profile_json,v3_profile_json,context_version "
        "FROM context3_dispatch_settings",
    )[0]
    assert "watershed-current-v2" in row[0]
    assert "watershed-current-v3" in row[1]
    assert row[2] == 3
    before_repeat = snapshot(ready)
    field_dispatch.upgrade_to_v3(CASE, V3_PROFILE, now=NOW + timedelta(seconds=2))
    assert snapshot(ready) == before_repeat
    with pytest.raises((ValueError, WorkflowConflict)):
        field_dispatch.upgrade_to_v3(
            CASE, replace(V3_PROFILE, model_id="different"), now=NOW + timedelta(seconds=2)
        )
    assert snapshot(ready) == before_repeat


def test_initial_v3_prepare_and_finish_use_one_allowance_and_clear_pointer(
    ready, field_dispatch
):
    field_dispatch.upgrade_to_v3(CASE, V3_PROFILE, now=NOW)
    step = field_dispatch.prepare(CASE, now=NOW)
    assert step.status == "RESERVED" and step.attention.reasons == ("INITIAL_REVIEW",)
    receipt = field_dispatch.finish(
        step.reservation.attempt_id, execution(step.reservation), now=NOW
    )
    assert receipt.delivery.status == "COMMITTED" and receipt.field_plan is None
    assert field_dispatch.status(CASE).assessment_count == 1
    assert field_dispatch.status(CASE).active_attempt_id is None
    assert field_dispatch.delivery.journal.allowance().provider_used == 1
    before_duplicate = snapshot(ready)
    assert field_dispatch.finish(
        step.reservation.attempt_id, execution(step.reservation), now=NOW
    ) == receipt
    assert field_dispatch.delivery.journal.allowance().provider_used == 1
    assert snapshot(ready) == before_duplicate


def test_failed_v3_attempt_remains_held_across_restart(ready, field_dispatch):
    field_dispatch.upgrade_to_v3(CASE, V3_PROFILE, now=NOW)
    step = field_dispatch.prepare(CASE, now=NOW)
    receipt = field_dispatch.fail(step.reservation.attempt_id, failed(step.reservation), now=NOW)
    assert receipt.delivery.status == "FAILED"
    journal = DeliveryStore(ready[1])
    reopened = FieldDispatchStore(
        DispatchStore(journal),
        FieldDeliveryStore(journal, field_dispatch.delivery.fields),
    )
    before = snapshot(ready)
    assert reopened.prepare(CASE, now=NOW).status == "HELD"
    assert reopened.status(CASE).active_status == "FAILED"
    assert snapshot(ready) == before


def test_quiet_clock_is_read_only_and_does_not_charge(ready, field_dispatch):
    field_dispatch.upgrade_to_v3(CASE, V3_PROFILE, now=NOW)
    step = field_dispatch.prepare(CASE, now=NOW)
    field_dispatch.finish(step.reservation.attempt_id, execution(step.reservation), now=NOW)
    before = snapshot(ready)
    allowance = field_dispatch.journal.allowance()
    quiet = field_dispatch.prepare(CASE, now=NOW + timedelta(seconds=1))
    assert quiet.status == "QUIET" and not quiet.attention.eligible
    assert snapshot(ready) == before
    assert field_dispatch.journal.allowance() == allowance


def test_human_revision_change_stales_and_keeps_source_and_pointer(ready, field_dispatch):
    field_dispatch.upgrade_to_v3(CASE, V3_PROFILE, now=NOW)
    step = field_dispatch.prepare(CASE, now=NOW)
    run = execution(step.reservation)
    stage(ready[1], ready[3][1], request="human-during-v3")
    receipt = field_dispatch.finish(step.reservation.attempt_id, run, now=NOW)
    assert receipt.delivery.status == "STALE" and receipt.field_plan is None
    assert field_dispatch.status(CASE).active_status == "STALE"
    assert query(
        ready[0], "SELECT status FROM watch_outbox WHERE event_id=?", (step.event_id,)
    ) == [("PENDING",)]
    assert query(
        ready[0], "SELECT COUNT(*) FROM dispatch_source_outcomes WHERE attempt_id=?",
        (step.reservation.attempt_id,),
    ) == [(0,)]
    assert field_dispatch.journal.allowance().provider_used == 1


def test_field_proposal_commits_with_source_outcome_in_one_dispatch_transaction(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    dispatch = DispatchStore(journal)
    dispatch.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    fields, review = prepare_partial_field_history(ready)
    store = FieldDispatchStore(dispatch, FieldDeliveryStore(journal, fields))
    at = NOW + timedelta(minutes=20)
    store.upgrade_to_v3(CASE, V3_PROFILE, now=at)
    step = store.prepare(CASE, now=at)
    run = proposal_execution(step.reservation)
    receipt = store.finish(step.reservation.attempt_id, run, now=at)
    assert receipt.delivery.status == "COMMITTED"
    assert receipt.field_plan.plan.author_kind == "AGENT"
    assert receipt.field_plan.plan.status == "PROPOSED"
    assert receipt.field_plan.plan.task_id == review.task_id
    assert receipt.field_plan.request_id == step.reservation.attempt_id + "-field-0"
    assert query(
        ready[0],
        "SELECT kind,attempt_id FROM dispatch_source_outcomes WHERE case_id=? AND kind='ASSESSED'",
        (CASE,),
    ) == [("ASSESSED", step.reservation.attempt_id)]
    assert query(
        ready[0], "SELECT status,COUNT(*) FROM watch_outbox GROUP BY status ORDER BY status"
    ) == [("DONE", 1), ("HISTORY", 1)]
    assert store.status(CASE).active_attempt_id is None


@pytest.mark.parametrize("stale_boundary", ["site", "window"])
def test_field_proposal_stales_when_site_or_window_loses_authority(ready, stale_boundary):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    dispatch = DispatchStore(journal)
    dispatch.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    fields, _ = prepare_partial_field_history(ready)
    store = FieldDispatchStore(dispatch, FieldDeliveryStore(journal, fields))
    at = NOW + timedelta(minutes=20)
    store.upgrade_to_v3(CASE, V3_PROFILE, now=at)
    step = store.prepare(CASE, now=at)
    run = proposal_execution(step.reservation)
    finish_at = at
    if stale_boundary == "site":
        approved_site = fields.locations.get(SITE.location_id, 1)
        withdrawn = replace(approved_site, revision=2, status="WITHDRAWN", recorded_at=at)
        changed_fields = FieldStore(
            ready[1], LocationRegistry((approved_site, withdrawn))
        )
        store = FieldDispatchStore(
            DispatchStore(journal), FieldDeliveryStore(journal, changed_fields)
        )
    else:
        finish_at = at + timedelta(hours=1)
    receipt = store.finish(step.reservation.attempt_id, run, now=finish_at)
    assert receipt.delivery.status == "STALE" and receipt.field_plan is None
    assert store.status(CASE).active_status == "STALE"
    assert query(
        ready[0], "SELECT status FROM watch_outbox WHERE event_id=?", (step.event_id,)
    ) == [("PENDING",)]
    assert query(ready[0], "SELECT COUNT(*) FROM context3_agent_proposals") == [(0,)]


def test_dispatch_outcome_failure_rolls_back_field_and_source_delivery(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    dispatch = DispatchStore(journal)
    dispatch.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    fields, _ = prepare_partial_field_history(ready)
    store = FieldDispatchStore(dispatch, FieldDeliveryStore(journal, fields))
    at = NOW + timedelta(minutes=20)
    store.upgrade_to_v3(CASE, V3_PROFILE, now=at)
    step = store.prepare(CASE, now=at)
    run = proposal_execution(step.reservation)
    before = snapshot(ready)
    with sqlite3.connect(ready[0]) as db:
        db.execute(
            "CREATE TRIGGER fail_field_dispatch_outcome BEFORE INSERT ON dispatch_source_outcomes "
            "BEGIN SELECT RAISE(ABORT, 'forced dispatcher outcome failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="forced dispatcher outcome failure"):
        store.finish(step.reservation.attempt_id, run, now=at)
    after = snapshot(ready)
    for table in before:
        assert after[table] == before[table], table
    assert journal.allowance().provider_used == 1
    assert store.status(CASE).active_status == "RESERVED"


def test_completed_v2_history_remains_readable_after_upgrade(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    legacy = DispatchStore(journal)
    legacy.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    step = legacy.prepare(CASE, now=NOW)
    receipt = legacy.finish(step.reservation.attempt_id, outcome(step.reservation), now=NOW)
    fields = FieldStore(ready[1], LocationRegistry((replace(SITE, case_id=CASE),)))
    delivery = FieldDeliveryStore(journal, fields)
    store = FieldDispatchStore(legacy, delivery)
    store.upgrade_to_v3(CASE, V3_PROFILE, now=NOW + timedelta(seconds=1))
    before = snapshot(ready)
    assert store.get(CASE, step.reservation.request_id) == receipt
    assert snapshot(ready) == before
    assert query(ready[0], "SELECT COUNT(*) FROM context3_attempts") == [(0,)]


def test_mixed_v2_and_v3_history_survives_restart_with_exact_receipts(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    legacy = DispatchStore(journal)
    legacy.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    old_step = legacy.prepare(CASE, now=NOW)
    old_receipt = legacy.finish(
        old_step.reservation.attempt_id, outcome(old_step.reservation), now=NOW
    )
    fields = FieldStore(ready[1], LocationRegistry((replace(SITE, case_id=CASE),)))
    store = FieldDispatchStore(legacy, FieldDeliveryStore(journal, fields))
    store.upgrade_to_v3(CASE, V3_PROFILE, now=NOW + timedelta(seconds=1))
    at = next_interval(ready, 45, 20)
    new_step = store.prepare(CASE, now=at)
    assert new_step.status == "RESERVED"
    new_receipt = store.finish(
        new_step.reservation.attempt_id, execution(new_step.reservation), now=at
    )
    reopened_journal = DeliveryStore(ready[1])
    reopened = FieldDispatchStore(
        DispatchStore(reopened_journal),
        FieldDeliveryStore(reopened_journal, fields),
    )
    before = snapshot(ready)
    assert reopened.get(CASE, old_step.reservation.request_id) == old_receipt
    assert reopened.get(CASE, new_step.reservation.request_id) == new_receipt
    assert snapshot(ready) == before


def test_upgrade_rejects_active_v2_attempt_without_mutation(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    legacy = DispatchStore(journal)
    legacy.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    step = legacy.prepare(CASE, now=NOW)
    fields = FieldStore(ready[1], LocationRegistry((replace(SITE, case_id=CASE),)))
    store = FieldDispatchStore(legacy, FieldDeliveryStore(journal, fields))
    before = snapshot(ready)
    with pytest.raises(WorkflowConflict):
        store.upgrade_to_v3(CASE, V3_PROFILE, now=NOW)
    assert snapshot(ready) == before
    legacy.fail(step.reservation.attempt_id, legacy_failure(step.reservation), now=NOW)


def test_upgrade_rejects_policy_compatible_but_profile_drift(ready, field_dispatch):
    with pytest.raises(ValueError):
        field_dispatch.upgrade_to_v3(
            CASE,
            replace(V3_PROFILE, sdk_version="different"),
            now=NOW + timedelta(seconds=1),
        )


def test_v3_admission_replay_is_byte_identical_to_v2_for_same_source_state(ready):
    legacy_journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    legacy = DispatchStore(legacy_journal)
    legacy.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    clone_path = ready[0].with_name("field-v3-clone.sqlite")
    with sqlite3.connect(ready[0]) as source, sqlite3.connect(clone_path) as target:
        source.backup(target)
    legacy_step = legacy.prepare(CASE, now=NOW)

    clone_cases = CaseStore(clone_path)
    clone_journal = DeliveryStore(clone_cases)
    clone_dispatch = DispatchStore(clone_journal)
    clone_fields = FieldStore(
        clone_cases, LocationRegistry((replace(SITE, case_id=CASE),))
    )
    field_dispatch = FieldDispatchStore(
        clone_dispatch, FieldDeliveryStore(clone_journal, clone_fields)
    )
    field_dispatch.upgrade_to_v3(CASE, V3_PROFILE, now=NOW)
    field_step = field_dispatch.prepare(CASE, now=NOW)
    assert field_step.attention == legacy_step.attention
    assert query(
        ready[0],
        "SELECT replay_json,attention_json,move_numeric FROM dispatch_attempts",
    ) == query(
        clone_path,
        "SELECT replay_json,attention_json,move_numeric FROM dispatch_attempts",
    )
