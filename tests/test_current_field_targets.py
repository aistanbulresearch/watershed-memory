"""Proposal discovery follows stored parent authority, including hidden old work."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_field_desk import desk
from test_current_field_store import (
    CASE,
    COORDINATOR,
    NOW,
    OPERATOR,
    contents,
    mutate,
    proposed,
    reported,
)
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current import case_records
from watershed_memory.current.case_types import HumanAction
from watershed_memory.current.field_context import _load_field_context
from watershed_memory.current.field_desk_targets import proposal_targets
from watershed_memory.current.field_types import DecideFieldPlan, FieldPrincipal


def targets(field, *, principal=COORDINATOR, alter=lambda context: context, now=NOW):
    with field[1]._connect() as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        context = _load_field_context(
            db, case_records.case_row(db, CASE), field[2].locations, evaluated_at=now
        )
        return proposal_targets(db, alter(context), principal)


def test_eligible_parent_is_case_visible_without_private_or_role_metadata(field):
    before = contents(field[0])
    parent = field[3]
    assert targets(field) == [
        {"task_id": parent.task_id, "revision": parent.revision, "title": parent.title}
    ]
    assert contents(field[0]) == before


@pytest.mark.parametrize("principal", [None, OPERATOR])
def test_proposal_discovery_needs_trusted_coordinator(field, principal):
    assert targets(field, principal=principal) == []


def test_wrong_case_principal_is_rejected(field):
    with pytest.raises(ValueError):
        targets(field, principal=FieldPrincipal("elsewhere", ("COORDINATOR",), ("OTHER",)))


def test_no_approved_site_has_no_proposal_target(field):
    assert targets(field, alter=lambda context: replace(context, approved_locations=())) == []


@pytest.mark.parametrize("status", ["PROPOSED", "APPROVED", "DEFERRED"])
def test_every_active_field_plan_blocks_the_same_parent(field, status):
    plan = proposed(field).plan
    if status != "PROPOSED":
        mutate(
            field,
            "decide_plan",
            DecideFieldPlan(
                plan.plan_id,
                plan.revision,
                "APPROVE" if status == "APPROVED" else "DEFER",
                NOW + timedelta(hours=1) if status == "DEFERRED" else None,
            ),
        )
    assert targets(field) == []


def test_superseded_active_plan_blocks_even_when_absent_from_projection(field):
    proposed(field)
    parent = field[3]
    field[1].act(
        CASE,
        HumanAction(parent.task_id, parent.revision, "APPROVE"),
        request_id="new-parent-revision",
        now=NOW,
    )
    assert (
        targets(
            field,
            alter=lambda context: replace(
                context,
                stranded_plans=(),
                state="EMPTY",
                has_more_stranded_plans=False,
            ),
        )
        == []
    )


@pytest.mark.parametrize("terminal", ["CANCELLED", "REPORTED"])
def test_terminal_field_work_does_not_block_new_proposal(field, terminal):
    if terminal == "REPORTED":
        reported(field)
    else:
        plan = proposed(field).plan
        mutate(field, "decide_plan", DecideFieldPlan(plan.plan_id, plan.revision, "CANCEL"))
    assert len(targets(field, now=NOW + timedelta(hours=1))) == 1


@pytest.mark.parametrize("action", ["DISMISS", "CANCEL"])
def test_closed_source_review_is_not_a_proposal_target(field, action):
    parent = field[3]
    field[1].act(
        CASE,
        HumanAction(parent.task_id, parent.revision, action),
        request_id="closed-parent",
        now=NOW,
    )
    assert targets(field) == []


def test_context_revision_must_equal_the_saved_case(field):
    with pytest.raises(ValueError):
        targets(
            field, alter=lambda context: replace(context, case_revision=context.case_revision + 1)
        )


@pytest.mark.parametrize("query_only,transaction", [(False, False), (False, True), (True, False)])
def test_discovery_requires_existing_query_only_transaction(field, query_only, transaction):
    with field[1]._connect() as db:
        db.execute("BEGIN")
        context = _load_field_context(
            db, case_records.case_row(db, CASE), field[2].locations, evaluated_at=NOW
        )
        db.rollback()
        if query_only:
            db.execute("PRAGMA query_only=ON")
        if transaction:
            db.execute("BEGIN")
        with pytest.raises(ValueError):
            proposal_targets(db, context, COORDINATOR)


def test_desk_advertises_targets_without_changing_historical_context_shape(field):
    before = contents(field[0])
    view = desk(field).snapshot(now=NOW)
    assert view["current_field_work"]["proposal_targets"] == targets(field)
    assert contents(field[0]) == before
