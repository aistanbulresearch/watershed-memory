"""The operator sees exact work/results and role-bound controls without any writes."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_context_v3 import load
from test_current_field_store import (
    CANARY,
    CASE,
    COORDINATOR,
    NOW,
    OPERATOR,
    VERIFIER,
    approved,
    attached,
    contents,
    mutate,
    proposed,
    reported,
    verified,
)
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current import field_records
from watershed_memory.current.case_types import HumanAction
from watershed_memory.current.field_desk_records import project_field, project_work
from watershed_memory.current.field_types import DecideFieldPlan, FieldPrincipal

PANEL_KEYS = {
    "case_id",
    "case_revision",
    "simulated",
    "evaluated_at",
    "state",
    "current_plans",
    "stranded_plans",
    "latest_results",
    "approved_locations",
    "has_more_stranded_plans",
    "has_more_results",
}
WORK_KEYS = {"plan", "result", "parent_binding", "field_dimension", "available_actions"}


def test_empty_initialized_ledger_has_an_honest_read_only_projection(field):
    context = load(field).field_work
    before = contents(field[0])
    value = project_field(context)
    assert set(value) == PANEL_KEYS
    assert value["state"] == "EMPTY"
    assert value["case_id"] == CASE and value["case_revision"] == context.case_revision
    assert value["current_plans"] == value["stranded_plans"] == value["latest_results"] == []
    assert value["simulated"] is True and value["evaluated_at"] == context.evaluated_at.isoformat()
    assert value["approved_locations"][0]["latitude"] is None
    assert not value["has_more_results"] and not value["has_more_stranded_plans"]
    assert contents(field[0]) == before
    json.dumps(value, allow_nan=False)


@pytest.mark.parametrize("principal", [None, OPERATOR, VERIFIER])
def test_proposed_work_does_not_give_unrelated_roles_approval(field, principal):
    proposed(field)
    context = load(field).field_work
    row = project_field(context, principal)["current_plans"][0]
    assert row["available_actions"] == []
    assert row["plan"]["status"] == "PROPOSED" and row["result"] is None
    assert set(row) == WORK_KEYS and row["parent_binding"] == "CURRENT"


def test_proposal_controls_and_detached_public_data(field):
    proposed(field)
    context = load(field).field_work
    before = contents(field[0])
    value = project_field(context, COORDINATOR)
    row = value["current_plans"][0]
    assert row["available_actions"] == ["APPROVE", "MODIFY", "DEFER", "CANCEL"]
    assert CANARY not in json.dumps(value)
    old_purpose = context.current_plans[0].plan.spec.purpose
    row["plan"]["spec"]["purpose"] = "client-side change"
    assert context.current_plans[0].plan.spec.purpose == old_purpose
    assert contents(field[0]) == before


@pytest.mark.parametrize(
    "principal,expected",
    [(COORDINATOR, ["MODIFY", "DEFER", "CANCEL"]), (OPERATOR, ["REPORT"]), (VERIFIER, [])],
)
def test_approved_plan_controls_match_actual_roles(field, principal, expected):
    approved(field)
    context = load(field).field_work
    assert project_field(context, principal)["current_plans"][0]["available_actions"] == expected


def test_expired_proposal_can_be_rescheduled_or_cancelled(field):
    proposed(field)
    context = load(field, at=NOW + timedelta(hours=3)).field_work
    assert project_field(context, COORDINATOR)["current_plans"][0]["available_actions"] == [
        "MODIFY",
        "CANCEL",
    ]


def test_withdrawn_registry_approval_is_not_inferred_from_saved_site(field):
    proposed(field)
    context = replace(load(field).field_work, approved_locations=())
    actions = project_field(context, COORDINATOR)["current_plans"][0]["available_actions"]
    assert actions == ["DEFER", "CANCEL"]


@pytest.mark.parametrize("terminal", [False, True])
def test_old_parent_plan_stays_visible_and_cancellable(field, terminal):
    proposed(field)
    parent = field[3]
    action = (
        HumanAction(parent.task_id, parent.revision, "CANCEL")
        if terminal
        else HumanAction(
            parent.task_id,
            parent.revision,
            "MODIFY",
            "",
            "Updated human review",
            NOW + timedelta(hours=1),
        )
    )
    field[1].act(CASE, action, request_id="parent-change", now=NOW + timedelta(minutes=1))
    context = load(field).field_work
    value = project_field(context, COORDINATOR)
    assert not value["current_plans"] and len(value["stranded_plans"]) == 1
    row = value["stranded_plans"][0]
    assert row["parent_binding"] == ("TERMINAL" if terminal else "SUPERSEDED")
    assert row["available_actions"] == ["CANCEL"]
    assert project_field(context, OPERATOR)["stranded_plans"][0]["available_actions"] == []


@pytest.mark.parametrize(
    "prepare,level,verifier_actions",
    [
        (reported, "REPORTED", ["ATTACH"]),
        (attached, "EVIDENCE_ATTACHED", ["ATTACH", "VERIFY"]),
        (verified, "VERIFIED", ["ATTACH"]),
    ],
)
def test_reported_attached_and_verified_are_distinct(field, prepare, level, verifier_actions):
    receipt = prepare(field)
    context = load(field).field_work
    before = contents(field[0])
    value = project_field(context, VERIFIER)
    work = value["latest_results"][0]
    result = work["result"]
    assert result["report"]["report_id"] == receipt.result.report.report_id
    assert result["report"]["revision"] == receipt.result.report.revision
    assert result["verification_level"] == level
    assert result["evidence_count"] == len(receipt.result.evidence)
    assert "evidence" not in result
    assert work["available_actions"] == verifier_actions
    assert CANARY not in json.dumps(value)
    assert project_field(context, OPERATOR)["latest_results"][0]["available_actions"] == [
        "CORRECT",
        "ATTACH",
    ]
    assert contents(field[0]) == before


def test_detail_preserves_exact_evidence_and_human_verification(field):
    receipt = verified(field)
    context = load(field).field_work
    row = project_work(context.latest_results[0], context, VERIFIER, detail=True)
    result = row["result"]
    assert [item["evidence_id"] for item in result["evidence"]] == list(
        receipt.result.verification.evidence_ids
    )
    assert result["verification"]["verified_by"] == VERIFIER.principal_id
    assert result["verification"]["scope"] == receipt.result.verification.scope
    assert CANARY not in json.dumps(row)


@pytest.mark.parametrize(
    "principal", [{}, True, "operator", FieldPrincipal("elsewhere", ("COORDINATOR",), ("OTHER",))]
)
def test_invalid_or_other_case_principal_is_rejected(field, principal):
    with pytest.raises(ValueError):
        project_field(load(field).field_work, principal)


@pytest.mark.parametrize("bad", [None, {}, True, "context"])
def test_untyped_context_is_rejected(bad):
    with pytest.raises(ValueError):
        project_field(bad)


@pytest.mark.parametrize("bad", [None, {}, True, "plan"])
def test_untyped_work_is_rejected(field, bad):
    with pytest.raises(ValueError):
        project_work(bad, load(field).field_work)


@pytest.mark.parametrize("bad", [None, 0, 1, "true"])
def test_detail_flag_is_exact_bool(field, bad):
    proposed(field)
    context = load(field).field_work
    with pytest.raises(ValueError):
        project_work(context.current_plans[0], context, detail=bad)


@pytest.mark.parametrize("terminal", [False, True])
def test_result_actions_survive_a_later_parent_change(field, terminal):
    attached(field)
    parent = field[3]
    action = (
        HumanAction(parent.task_id, parent.revision, "CANCEL")
        if terminal
        else HumanAction(
            parent.task_id,
            parent.revision,
            "MODIFY",
            "",
            "Updated later review",
            NOW + timedelta(hours=1),
        )
    )
    field[1].act(CASE, action, request_id="reported-parent-change", now=NOW + timedelta(minutes=18))
    context = load(field).field_work
    row = context.latest_results[0]
    assert row.parent_binding == ("TERMINAL" if terminal else "SUPERSEDED")
    assert project_work(row, context, COORDINATOR)["available_actions"] == []
    assert project_work(row, context, OPERATOR)["available_actions"] == ["CORRECT", "ATTACH"]
    assert project_work(row, context, VERIFIER)["available_actions"] == ["ATTACH", "VERIFY"]


def test_another_approved_site_still_allows_rescheduling(field):
    proposed(field)
    context = load(field).field_work
    alternative = replace(context.approved_locations[0], location_id="another-approved-site")
    context = replace(context, approved_locations=(alternative,))
    assert project_field(context, COORDINATOR)["current_plans"][0]["available_actions"] == [
        "MODIFY",
        "DEFER",
        "CANCEL",
    ]


def test_combined_roles_keep_both_plan_management_and_reporting(field):
    approved(field)
    principal = FieldPrincipal(
        "local-operator", ("COORDINATOR", "FIELD_OPERATOR", "VERIFIER"), (CASE,)
    )
    context = load(field).field_work
    assert project_field(context, principal)["current_plans"][0]["available_actions"] == [
        "MODIFY",
        "DEFER",
        "CANCEL",
        "REPORT",
    ]


def test_cancelled_work_outside_the_current_index_is_still_inspectable(field):
    item = proposed(field).plan
    mutate(
        field,
        "decide_plan",
        DecideFieldPlan(item.plan_id, item.revision, "CANCEL"),
        now=NOW + timedelta(minutes=1),
    )
    context = load(field).field_work
    assert not context.current_plans and not context.stranded_plans and not context.latest_results
    with field[1]._connect() as db:
        db.execute("BEGIN")
        saved = field_records.snapshot(db, CASE, item.plan_id)
    value = project_work(saved, context, COORDINATOR, detail=True)
    assert value["plan"]["status"] == "CANCELLED" and value["available_actions"] == []


def test_modification_can_switch_to_another_approved_activity(field):
    proposed(field)
    context = load(field).field_work
    alternative = replace(
        context.approved_locations[0], location_id="sampling-site", activities=("SAMPLING",)
    )
    context = replace(context, approved_locations=(alternative,))
    assert project_field(context, COORDINATOR)["current_plans"][0]["available_actions"] == [
        "MODIFY",
        "DEFER",
        "CANCEL",
    ]


@pytest.mark.parametrize("terminal", [False, True])
def test_cancelled_plan_with_later_parent_change_has_no_actions(field, terminal):
    item = proposed(field).plan
    mutate(
        field,
        "decide_plan",
        DecideFieldPlan(item.plan_id, item.revision, "CANCEL"),
        now=NOW + timedelta(minutes=1),
    )
    parent = field[3]
    action = (
        HumanAction(parent.task_id, parent.revision, "CANCEL")
        if terminal
        else HumanAction(
            parent.task_id,
            parent.revision,
            "MODIFY",
            "",
            "Changed after plan cancellation",
            NOW + timedelta(hours=1),
        )
    )
    field[1].act(CASE, action, request_id="cancelled-parent-change", now=NOW + timedelta(minutes=2))
    with field[1]._connect() as db:
        db.execute("BEGIN")
        saved = field_records.snapshot(db, CASE, item.plan_id)
    assert saved.parent_binding == ("TERMINAL" if terminal else "SUPERSEDED")
    assert (
        project_work(saved, load(field).field_work, COORDINATOR, detail=True)["available_actions"]
        == []
    )


@pytest.mark.parametrize("part", ["plan", "report", "evidence", "verification", "location"])
def test_detail_rejects_future_records_even_outside_the_bounded_index(field, part):
    prepare = verified if part == "verification" else attached if part == "evidence" else reported
    prepare(field)
    context = load(field).field_work
    saved = context.latest_results[0]
    future = context.evaluated_at + timedelta(hours=1)
    if part == "plan":
        saved = replace(saved, plan=replace(saved.plan, updated_at=future))
    elif part == "report":
        saved = replace(
            saved,
            result=replace(saved.result, report=replace(saved.result.report, updated_at=future)),
        )
    elif part == "evidence":
        saved = replace(
            saved,
            result=replace(
                saved.result, evidence=(replace(saved.result.evidence[0], attached_at=future),)
            ),
        )
    elif part == "verification":
        saved = replace(
            saved,
            result=replace(
                saved.result, verification=replace(saved.result.verification, verified_at=future)
            ),
        )
    else:
        saved = replace(
            saved,
            plan=replace(
                saved.plan,
                updated_at=future,
                location=replace(saved.plan.location, recorded_at=future, approved_at=future),
            ),
        )
    context = replace(context, latest_results=())
    with pytest.raises(ValueError):
        project_work(saved, context, COORDINATOR, detail=True)
