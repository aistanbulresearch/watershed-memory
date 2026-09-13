"""Field responses return original receipts and current work after lost confirmation."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_field_desk import desk
from test_current_field_store import CASE, COORDINATOR, NOW, SITE, SPEC, contents, proposed
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.desk import DeskUnavailable
from watershed_memory.current.field_types import (
    AttachFieldEvidence,
    CorrectFieldResult,
    DecideFieldPlan,
    FieldPrincipal,
    ModifyFieldPlan,
    ProposeFieldPlan,
    ReportFieldResult,
    VerifyFieldReport,
)

ALL = FieldPrincipal("local-field-operator", ("COORDINATOR", "FIELD_OPERATOR", "VERIFIER"), (CASE,))


def send(page, command, number, *, expected=None):
    revision = page.cases.context(CASE)["revision"] if expected is None else expected
    return page.respond_field(
        command,
        request_id=f"field-response-{number}",
        expected_case_revision=revision,
        now=NOW + timedelta(minutes=number),
    )


def test_full_human_field_cycle_preserves_verified_receipt_after_correction(field):
    page = desk(field, ALL)
    review = field[3]
    result = send(
        page,
        ProposeFieldPlan(
            review.task_id, review.revision, SITE.location_id, SITE.revision, SPEC, True
        ),
        0,
    )
    plan = result["selected_field_work"]["plan"]
    assert plan["status"] == "PROPOSED"
    result = send(
        page,
        ModifyFieldPlan(
            plan["plan_id"],
            plan["revision"],
            SITE.location_id,
            SITE.revision,
            replace(
                SPEC,
                purpose="Inspect and record the revised sediment marker.",
                window_start=NOW + timedelta(minutes=1),
            ),
            True,
        ),
        1,
    )
    plan = result["selected_field_work"]["plan"]
    result = send(
        page,
        DecideFieldPlan(plan["plan_id"], plan["revision"], "DEFER", NOW + timedelta(hours=1)),
        2,
    )
    plan = result["selected_field_work"]["plan"]
    assert plan["status"] == "DEFERRED"
    result = send(page, DecideFieldPlan(plan["plan_id"], plan["revision"], "APPROVE"), 3)
    plan = result["selected_field_work"]["plan"]
    result = send(
        page,
        ReportFieldResult(
            plan["plan_id"],
            plan["revision"],
            "COMPLETE",
            "The agreed inspection has been completed.",
            NOW + timedelta(minutes=4),
            NOW + timedelta(minutes=5),
            True,
        ),
        6,
    )
    report = result["selected_field_work"]["result"]["report"]
    result = send(
        page,
        AttachFieldEvidence(
            report["report_id"],
            report["revision"],
            "OPERATOR_RECORD_REFERENCE",
            "INSPECTION_RECORD_REFERENCE",
            "inspection-record-17",
            "Operator-maintained inspection register.",
            NOW + timedelta(minutes=5),
            None,
            True,
        ),
        7,
    )
    assert result["selected_field_work"]["result"]["verification_level"] == "EVIDENCE_ATTACHED"
    evidence_ids = tuple(
        item["evidence_id"] for item in result["selected_field_work"]["result"]["evidence"]
    )
    command = VerifyFieldReport(
        report["report_id"],
        report["revision"],
        evidence_ids,
        "Reviewed the exact attached inspection record.",
        True,
    )
    revision = page.cases.context(CASE)["revision"]
    confirmed = send(page, command, 8, expected=revision)
    assert confirmed["selected_field_work"]["field_dimension"] == "VERIFIED_COMPLETE"
    corrected = send(
        page,
        CorrectFieldResult(
            report["report_id"],
            report["revision"],
            "PARTIAL",
            "The second agreed inspection point remains unvisited.",
            NOW + timedelta(minutes=4),
            NOW + timedelta(minutes=5),
            True,
        ),
        9,
    )
    assert corrected["selected_field_work"]["result"]["verification_level"] == "REPORTED"
    before = contents(field[0])
    repeated = page.respond_field(
        command,
        request_id="field-response-8",
        expected_case_revision=revision,
        now=NOW + timedelta(minutes=10),
    )
    assert repeated["receipt"] == confirmed["receipt"]
    assert repeated["receipt"]["result"]["verification_level"] == "VERIFIED"
    assert repeated["selected_field_work"]["result"]["report"]["outcome"] == "PARTIAL"
    assert repeated["selected_field_work"]["result"]["report"]["revision"] == 2
    assert contents(field[0]) == before
    assert "private_note" not in json.dumps([confirmed, corrected, repeated])


def test_lost_confirmation_keeps_original_request_identity(field, monkeypatch):
    plan = proposed(field).plan
    page = desk(field)
    revision = page.cases.context(CASE)["revision"]
    command = DecideFieldPlan(plan.plan_id, plan.revision, "APPROVE")
    read = page._read

    def unavailable(**kwargs):
        raise RuntimeError("PRIVATE-REFRESH-ERROR")

    monkeypatch.setattr(page, "_read", unavailable)
    with pytest.raises(DeskUnavailable) as error:
        send(page, command, 1, expected=revision)
    assert "PRIVATE" not in str(error.value)
    assert field[2].get_plan(CASE, plan.plan_id).status == "APPROVED"
    monkeypatch.setattr(page, "_read", read)
    before = contents(field[0])
    retried = send(page, command, 1, expected=revision)
    assert retried["receipt"]["plan"]["status"] == "APPROVED"
    assert contents(field[0]) == before


def test_changed_retry_and_stale_case_revision_cannot_overwrite(field):
    plan = proposed(field).plan
    page = desk(field)
    revision = page.cases.context(CASE)["revision"]
    send(page, DecideFieldPlan(plan.plan_id, plan.revision, "APPROVE"), 1, expected=revision)
    before = contents(field[0])
    with pytest.raises(WorkflowConflict):
        send(page, DecideFieldPlan(plan.plan_id, plan.revision, "CANCEL"), 1, expected=revision)
    with pytest.raises(WorkflowConflict):
        send(page, DecideFieldPlan(plan.plan_id, plan.revision + 1, "CANCEL"), 2, expected=revision)
    assert contents(field[0]) == before


@pytest.mark.parametrize(
    "principal", [None, FieldPrincipal("verifier-only", ("VERIFIER",), (CASE,))]
)
def test_server_roles_reject_unauthorized_mutations_before_changes(field, principal):
    plan = proposed(field).plan
    page = desk(field, principal)
    before = contents(field[0])
    with pytest.raises(ValueError):
        send(page, DecideFieldPlan(plan.plan_id, plan.revision, "APPROVE"), 1)
    assert contents(field[0]) == before


def test_field_desk_rejects_private_note_and_untyped_command_without_changes(field):
    plan = proposed(field).plan
    page = desk(field, COORDINATOR)
    before = contents(field[0])
    for command in (
        DecideFieldPlan(plan.plan_id, plan.revision, "APPROVE", private_note="PRIVATE-NEW-NOTE"),
        {},
        None,
    ):
        with pytest.raises(ValueError):
            send(page, command, 1)
    assert contents(field[0]) == before
