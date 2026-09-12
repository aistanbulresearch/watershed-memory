"""Synthetic v3 delivery tests pin the atomic field/source authority boundary."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_current_case_store import CASE, MONITOR, NOW, query, stage
from test_current_case_store import ready as ready
from test_current_field_store import SITE, contents
from test_current_field_tools import arguments, source_ready

from watershed_memory.current.assessment_types import ToolReceipt
from watershed_memory.current.case_records import digest
from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import InvocationAllowance, InvocationProfile
from watershed_memory.current.field_agent import proposal_command
from watershed_memory.current.field_delivery_codec import encode_execution
from watershed_memory.current.field_delivery_store import FieldDeliveryStore
from watershed_memory.current.field_delivery_types import CurrentExecutionV3, CurrentFailureV3
from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.field_tools import CurrentToolsV3
from watershed_memory.current.field_types import DecideFieldPlan
from watershed_memory.current.locations import LocationRegistry
from watershed_memory.current.tools import _json

PROFILE = InvocationProfile(
    "STRANDS_CURRENT", "synthetic-envelope", "watershed-current-v3", "fixture"
)
AT = NOW + timedelta(minutes=20)


@pytest.fixture
def canonical(ready):
    path, cases, watch, events = ready
    review = stage(cases, events[0])
    site = replace(
        SITE,
        location_id="gallinas-inspection-site",
        case_id=CASE,
        simulated=False,
        latitude=Decimal("35.6500"),
        longitude=Decimal("-105.3200"),
        coordinate_system="WGS84",
        coordinate_accuracy="Synthetic fixture coordinates",
    )
    fields = FieldStore(cases, LocationRegistry((site,)))
    journal = DeliveryStore(cases, allowance=InvocationAllowance(0, 8))
    delivery = FieldDeliveryStore(journal, fields)
    return path, cases, watch, events, review, site, fields, delivery


def reserve(canonical):
    _, _, _, events, _, _, _, delivery = canonical
    return delivery.reserve(
        CASE,
        events[-1].event_id,
        request_id="canonical-v3",
        profile=PROFILE,
        source_delivery=True,
        now=AT,
    )


def execution(ticket, site):
    context = ticket.context
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    for item in (*context.field_work.current_plans, *context.field_work.stranded_plans):
        tools.inspect_field_work(item.plan.plan_id)
    basis = context.field_work.latest_results[0] if context.field_work.latest_results else None
    if basis is not None:
        tools.inspect_field_work(basis.plan.plan_id)
    tools.list_approved_field_locations("VISUAL_INSPECTION")
    values = arguments(context, "PROPOSE_FIELD_PLAN", basis, True)
    values.update(location_id=site.location_id, location_revision=site.revision)
    tools.stage_field_decision(**values)
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


def invalid_failure(ticket):
    context = ticket.context
    base = context.base
    forged = ToolReceipt("get_case_context", "{}", "{}")
    return CurrentFailureV3(
        base.case_id,
        base.case_revision,
        base.policy_digest,
        base.current.event_id,
        digest(_json(context)),
        PROFILE.mode,
        PROFILE.model_id,
        PROFILE.instruction_version,
        PROFILE.sdk_version,
        1,
        1,
        (forged,),
        (),
        "{}",
    )


def test_canonical_source_and_agent_proposal_commit_as_one_synthetic_envelope(canonical):
    path, cases, watch, _, review, _, _, delivery = canonical
    before_pending = watch.status(MONITOR)["pending_count"]
    ticket = reserve(canonical)
    result = delivery.commit(ticket.attempt_id, execution(ticket, canonical[5]), now=AT)

    assert result.delivery.status == "COMMITTED"
    assert result.delivery.source_delivery is True
    assert result.delivery.profile.mode == "STRANDS_CURRENT"
    assert len(result.delivery.work) == 1
    assert result.delivery.work[0].task_id == review.task_id
    assert result.field_plan.plan.status == "PROPOSED"
    assert result.field_plan.plan.author_kind == "AGENT"
    assert result.field_plan.plan.recorded_by == ticket.attempt_id
    assert result.field_plan.plan.simulated is False
    assert query(
        path,
        "SELECT status FROM watch_outbox WHERE event_id=?",
        (ticket.context.base.current.event_id,),
    ) == [("DONE",)]
    assert watch.status(MONITOR)["pending_count"] == before_pending - 1
    assert cases.evidence(CASE, review.task_id)[0] == ticket.context.base.current.event_id
    assert query(path, "SELECT COUNT(*) FROM context3_agent_proposals") == [(1,)]
    assert query(path, "PRAGMA foreign_key_check") == []


@pytest.mark.parametrize("boundary", ["field", "source"])
def test_exception_after_field_or_source_mutation_rolls_back_the_whole_finish(
    canonical, monkeypatch, boundary
):
    path, cases, watch, _, _, site, fields, delivery = canonical
    ticket = reserve(canonical)
    run = execution(ticket, site)
    before = contents(path)
    before_case = cases.context(CASE)
    before_source = watch.status(MONITOR)

    if boundary == "field":
        original = FieldStore._stage_agent_plan

        def fail_after_field(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise RuntimeError("injected after field save")

        monkeypatch.setattr(FieldStore, "_stage_agent_plan", fail_after_field)
    else:
        original = DeliveryStore._acknowledge

        def fail_after_source(db, attempt):
            original(db, attempt)
            raise RuntimeError("injected after source acknowledgement")

        monkeypatch.setattr(DeliveryStore, "_acknowledge", staticmethod(fail_after_source))

    with pytest.raises(RuntimeError, match="injected"):
        delivery.commit(ticket.attempt_id, run, now=AT)

    assert contents(path) == before
    assert cases.context(CASE) == before_case
    assert watch.status(MONITOR) == before_source
    held = delivery.get(CASE, ticket.request_id)
    assert held.delivery.status == "RESERVED"
    assert held.delivery.execution_json is None and held.field_plan is None
    assert delivery.journal.allowance().provider_used == 1


def test_proposal_expired_at_commit_is_stale_without_source_or_field_write(canonical):
    path, cases, watch, _, _, site, _, delivery = canonical
    before_pending = watch.status(MONITOR)["pending_count"]
    ticket = reserve(canonical)
    run = execution(ticket, site)
    result = delivery.commit(ticket.attempt_id, run, now=AT + timedelta(hours=2))

    assert result.delivery.status == "STALE" and result.field_plan is None
    assert cases.context(CASE)["revision"] == ticket.context.base.case_revision
    assert query(path, "SELECT COUNT(*) FROM field_plans") == [(0,)]
    assert query(
        path,
        "SELECT status FROM watch_outbox WHERE event_id=?",
        (ticket.context.base.current.event_id,),
    ) == [("PENDING",)]
    assert watch.status(MONITOR)["pending_count"] == before_pending
    assert delivery.journal.allowance().provider_used == 1


@pytest.mark.parametrize("forgery", ["profile", "context_digest", "failure_trace"])
def test_identity_or_failure_trace_forgery_cannot_write(canonical, forgery):
    path, _, _, _, _, site, _, delivery = canonical
    ticket = reserve(canonical)
    before = contents(path)
    if forgery == "failure_trace":
        def action():
            return delivery.fail(ticket.attempt_id, invalid_failure(ticket), now=AT)
    else:
        run = execution(ticket, site)
        if forgery == "profile":
            run = replace(run, model_id="different-envelope")
        else:
            run = replace(
                run,
                assessment=replace(run.assessment, context_digest="f" * 64),
            )
        def action():
            return delivery.commit(ticket.attempt_id, run, now=AT)
    with pytest.raises(ValueError):
        action()
    assert contents(path) == before
    assert delivery.get(CASE, ticket.request_id).delivery.status == "RESERVED"
    assert delivery.journal.allowance().provider_used == 1


def test_concurrent_identical_finish_returns_one_original_plan_and_receipt(canonical):
    path, _, _, _, _, site, _, delivery = canonical
    ticket = reserve(canonical)
    run = execution(ticket, site)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: delivery.commit(ticket.attempt_id, run, now=AT), range(2)))

    assert results[0] == results[1]
    assert results[0].field_plan.plan.plan_id == results[1].field_plan.plan.plan_id
    assert query(path, "SELECT COUNT(*) FROM field_plans") == [(1,)]
    assert query(path, "SELECT COUNT(*) FROM field_receipts") == [(1,)]
    assert query(path, "SELECT COUNT(*) FROM context3_agent_proposals") == [(1,)]
    assert delivery.journal.allowance().provider_used == 1


@pytest.mark.parametrize("violation", ["wrong_command", "private_note", "no_execution"])
def test_private_agent_primitive_rejects_unbound_calls_without_writes(canonical, violation):
    path, cases, _, _, _, site, fields, delivery = canonical
    ticket = reserve(canonical)
    run = execution(ticket, site)
    before = contents(path)
    command = proposal_command(ticket.context, run.assessment.field.proposal)
    if violation == "wrong_command":
        command = DecideFieldPlan("unbound-plan", 1, "APPROVE")
    elif violation == "private_note":
        command = replace(command, private_note="Caller cannot smuggle private authority.")

    with cases._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if violation != "no_execution":
            db.execute(
                "UPDATE delivery_attempts SET execution_json=? WHERE attempt_id=?",
                (encode_execution(run), ticket.attempt_id),
            )
        with pytest.raises(ValueError):
            fields._stage_agent_plan(
                db,
                CASE,
                command,
                attempt_id=ticket.attempt_id,
                request_id=ticket.attempt_id + "-field-0",
                expected_case_revision=ticket.context.base.case_revision,
                reserved_context_digest=run.assessment.context_digest,
                now=AT,
            )
        db.rollback()

    assert contents(path) == before
    assert delivery.get(CASE, ticket.request_id).delivery.status == "RESERVED"
    assert query(path, "SELECT COUNT(*) FROM field_plans") == [(0,)]
