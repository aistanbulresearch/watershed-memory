"""Field-aware delivery makes one unapproved proposal with its exact source turn."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_context_v3 import event_id
from test_current_field_store import (
    CASE,
    COORDINATOR,
    NOW,
    SITE,
    attached,
    contents,
    mutate,
    verified,
)
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401
from test_current_field_tools import arguments, source_ready

from watershed_memory.current.case_records import digest
from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.case_types import HumanAction, WorkflowConflict
from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import (
    DeliveryHeld,
    InvocationAllowance,
    InvocationProfile,
)
from watershed_memory.current.field_delivery_store import FieldDeliveryStore
from watershed_memory.current.field_delivery_types import CurrentExecutionV3, CurrentFailureV3
from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.field_tools import CurrentToolsV3
from watershed_memory.current.field_types import DecideFieldPlan
from watershed_memory.current.locations import LocationRegistry
from watershed_memory.current.tools import _json

PROFILE = InvocationProfile("SCRIPTED_SDK", "synthetic-envelope", "watershed-current-v3", "fixture")
AT = NOW + timedelta(minutes=20)


def store(field):
    journal = DeliveryStore(field[1], allowance=InvocationAllowance(4, 0))
    return FieldDeliveryStore(journal, field[2])


def reserve(delivery, field, **changes):
    kwargs = dict(request_id="v3-delivery", profile=PROFILE, source_delivery=False, now=AT)
    kwargs.update(changes)
    return delivery.reserve(CASE, event_id(field), **kwargs)


def execution(ticket, disposition="PROPOSE_FIELD_PLAN"):
    context = ticket.context
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    snapshots = (*context.field_work.current_plans, *context.field_work.stranded_plans)
    for item in snapshots:
        tools.inspect_field_work(item.plan.plan_id)
    basis = context.field_work.latest_results[0] if context.field_work.latest_results else None
    if basis:
        tools.inspect_field_work(basis.plan.plan_id)
    if disposition == "PROPOSE_FIELD_PLAN":
        tools.list_approved_field_locations("VISUAL_INSPECTION")
    tools.stage_field_decision(
        **arguments(context, disposition, basis, disposition == "PROPOSE_FIELD_PLAN")
    )
    return CurrentExecutionV3(
        tools.finish(),
        PROFILE.mode,
        PROFILE.model_id,
        PROFILE.instruction_version,
        PROFILE.sdk_version,
        1,
        tools.attempts,
        0.01,
        "{}",
        "end_turn",
    )


@pytest.mark.parametrize(
    "outcome,level,disposition,new_plan",
    [
        ("COMPLETE", "VERIFIED", "NO_NEW_FIELD_PLAN", False),
        ("COMPLETE", "EVIDENCE_ATTACHED", "AWAIT_VERIFICATION", False),
        ("PARTIAL", "VERIFIED", "PROPOSE_FIELD_PLAN", True),
    ],
)
def test_saved_result_controls_persisted_field_outcome_and_original_retry(
    field, outcome, level, disposition, new_plan
):
    (verified if level == "VERIFIED" else attached)(field, outcome)
    delivery = store(field)
    before = contents(field[0])
    ticket = reserve(delivery, field)
    run = execution(ticket, disposition)
    receipt = delivery.commit(ticket.attempt_id, run, now=AT)
    assert receipt.delivery.status == "COMMITTED"
    assert (receipt.field_plan is not None) is new_plan
    assert receipt.delivery.work[0].task_id == field[3].task_id
    assert field[1].context(CASE)["revision"] == ticket.context.base.case_revision + 2 + new_plan
    if new_plan:
        proposal = receipt.field_plan
        assert proposal.plan.status == "PROPOSED" and proposal.plan.author_kind == "AGENT"
        assert proposal.plan.recorded_by == ticket.attempt_id
        assert proposal.plan.task_id == field[3].task_id
        assert proposal.request_id == ticket.attempt_id + "-field-0"
        assert proposal.result is proposal.evidence is proposal.verification is None
    assert delivery.commit(ticket.attempt_id, run, now=AT) == receipt
    assert reserve(delivery, field) == receipt
    cases = CaseStore(field[0])
    reopened = FieldDeliveryStore(
        DeliveryStore(cases), FieldStore(cases, LocationRegistry((SITE,)))
    )
    assert reopened.get(CASE, ticket.request_id) == receipt
    assert reopened.journal.allowance().scripted_used == 1
    after = contents(field[0])
    for name in ("observations_current", "observation_versions", "watch_events", "watch_outbox"):
        assert after[name] == before[name]
    with pytest.raises(WorkflowConflict):
        delivery.commit(ticket.attempt_id, replace(run, elapsed_seconds=0.02), now=AT)


def test_repeated_reservation_is_held_and_never_charges_twice(field):
    delivery = store(field)
    ticket = reserve(delivery, field)
    assert delivery.get(CASE, ticket.request_id).delivery.status == "RESERVED"
    for request in (ticket.request_id, "different-request"):
        with pytest.raises(DeliveryHeld):
            reserve(delivery, field, request_id=request)
    assert delivery.journal.allowance().scripted_used == 1


def test_human_change_between_reservation_and_commit_holds_stale_result(field):
    delivery = store(field)
    ticket = reserve(delivery, field)
    run = execution(ticket)
    field[1].act(
        CASE,
        HumanAction(
            field[3].task_id,
            1,
            "MODIFY",
            title="Human changed this review",
            next_check_at=AT + timedelta(hours=1),
        ),
        request_id="human-during-turn",
        now=AT,
    )
    before = contents(field[0])
    receipt = delivery.commit(ticket.attempt_id, run, now=AT)
    assert receipt.delivery.status == "STALE" and receipt.field_plan is None
    after = contents(field[0])
    for name in before:
        if name != "delivery_attempts":
            assert before[name] == after[name], name
    assert delivery.get(CASE, ticket.request_id) == receipt
    with pytest.raises(DeliveryHeld):
        delivery.commit(ticket.attempt_id, run, now=AT)


def test_site_withdrawal_does_not_rewrite_reserved_context_but_refuses_proposal(field):
    delivery = store(field)
    ticket = reserve(delivery, field)
    run = execution(ticket)
    withdrawn = replace(SITE, revision=2, status="WITHDRAWN", recorded_at=AT)
    changed = FieldDeliveryStore(
        delivery.journal, FieldStore(field[1], LocationRegistry((SITE, withdrawn)))
    )
    before = contents(field[0])
    receipt = changed.commit(ticket.attempt_id, run, now=AT)
    assert receipt.delivery.status == "STALE" and receipt.field_plan is None
    assert contents(field[0])["field_plans"] == before["field_plans"]


def test_committed_agent_proposal_requires_a_separate_human_approval(field):
    delivery = store(field)
    ticket = reserve(delivery, field)
    run = execution(ticket)
    receipt = delivery.commit(ticket.attempt_id, run, now=AT)
    plan = receipt.field_plan.plan
    assert plan.status == "PROPOSED"
    approved = mutate(
        field, "decide_plan", DecideFieldPlan(plan.plan_id, 1, "APPROVE"), who=COORDINATOR, now=AT
    )
    assert approved.plan.status == "APPROVED" and approved.plan.author_kind == "HUMAN"
    assert delivery.commit(ticket.attempt_id, run, now=AT) == receipt
    assert delivery.get(CASE, ticket.request_id).field_plan.plan.status == "PROPOSED"


def test_failure_preserves_context_and_charged_allowance_without_field_work(field):
    delivery = store(field)
    ticket = reserve(delivery, field)
    base = ticket.context.base
    failure = CurrentFailureV3(
        base.case_id,
        base.case_revision,
        base.policy_digest,
        base.current.event_id,
        digest(_json(ticket.context)),
        PROFILE.mode,
        PROFILE.model_id,
        PROFILE.instruction_version,
        PROFILE.sdk_version,
        1,
        0,
        (),
        (),
        "{}",
    )
    before = contents(field[0])
    receipt = delivery.fail(ticket.attempt_id, failure, now=AT)
    assert receipt.delivery.status == "FAILED" and receipt.field_plan is None
    assert delivery.fail(ticket.attempt_id, failure, now=AT) == receipt
    after = contents(field[0])
    for name in before:
        if name != "delivery_attempts":
            assert before[name] == after[name], name
    with pytest.raises(DeliveryHeld):
        reserve(delivery, field)
