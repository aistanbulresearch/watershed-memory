"""Model-staged field decisions have a closed shape and no human authority."""

from dataclasses import replace

import pytest
from test_current_field_store import SPEC

from watershed_memory.current.assessment_types import CurrentAssessment, ReviewDecision, ToolReceipt
from watershed_memory.current.field_assessment_types import (
    AgentFieldDecision,
    AgentFieldProposal,
    CurrentAssessmentV3,
    FieldToolReceipt,
)

HASH = "a" * 64


def decision(**changes):
    value = AgentFieldDecision(
        "NO_NEW_FIELD_PLAN",
        None,
        None,
        None,
        None,
        "No field follow-up is needed for this case.",
        None,
    )
    return replace(value, **changes)


def assessment(**changes):
    base = CurrentAssessment(
        "CASE",
        1,
        HASH,
        HASH,
        (
            ReviewDecision(
                "NO_FOLLOW_UP", None, HASH, None, None, "No new source work is needed.", None, ()
            ),
        ),
        (ToolReceipt("get_case_context", "{}", "{}"),),
    )
    value = CurrentAssessmentV3(
        3,
        base,
        decision(),
        (
            FieldToolReceipt("get_field_context", "{}", "{}"),
            FieldToolReceipt("stage_field_decision", "{}", "{}"),
        ),
        HASH,
    )
    return replace(value, **changes)


def test_three_supported_field_decisions_are_frozen_without_principal_or_private_note():
    no_new = decision()
    awaiting = decision(
        disposition="AWAIT_VERIFICATION",
        basis_plan_id="plan-1",
        basis_report_id="report-1",
        basis_report_revision=1,
        basis_verification_level="REPORTED",
    )
    proposal = AgentFieldProposal("task-1", 1, "site-1", 1, SPEC)
    staged = decision(disposition="PROPOSE_FIELD_PLAN", proposal=proposal)
    for value in (no_new, awaiting, proposal, staged, assessment()):
        assert not hasattr(value, "__dict__")
        assert not hasattr(value, "principal") and not hasattr(value, "private_note")
        with pytest.raises((AttributeError, TypeError)):
            value.extra = "mutable"


def test_no_new_plan_can_name_the_exact_verified_result_it_remembers():
    value = decision(
        basis_plan_id="plan-1",
        basis_report_id="report-1",
        basis_report_revision=1,
        basis_verification_level="VERIFIED",
    )
    assert value.basis_report_revision == 1 and value.proposal is None


def test_report_basis_requires_its_exact_revision():
    with pytest.raises(ValueError):
        decision(
            disposition="AWAIT_VERIFICATION",
            basis_plan_id="plan-1",
            basis_report_id="report-1",
            basis_verification_level="REPORTED",
        )


def test_field_decision_can_be_staged_only_once_in_saved_trace():
    original = assessment()
    with pytest.raises(ValueError):
        replace(
            original,
            field_trace=(original.field_trace[0], original.field_trace[1], original.field_trace[1]),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"disposition": "APPROVE"},
        {"disposition": "VERIFY"},
        {"disposition": "PROPOSE_FIELD_PLAN"},
        {"disposition": "AWAIT_VERIFICATION"},
        {"basis_report_id": "report-1"},
        {"basis_report_revision": 1},
        {"basis_verification_level": "VERIFIED"},
        {"basis_plan_id": True},
        {"reason": ""},
        {"reason": "x" * 701},
        {"reason": "Wrong\ncontrol text"},
        {"proposal": AgentFieldProposal("task-1", 1, "site-1", 1, SPEC)},
    ],
)
def test_field_decision_rejects_incoherent_or_unauthorized_shapes(changes):
    with pytest.raises(ValueError):
        decision(**changes)


@pytest.mark.parametrize("revision", [0, True, -1, 2**63, 1.5, "1"])
def test_proposal_revision_is_exact_positive(revision):
    with pytest.raises(ValueError):
        AgentFieldProposal("task-1", revision, "site-1", 1, SPEC)
    with pytest.raises(ValueError):
        AgentFieldProposal("task-1", 1, "site-1", revision, SPEC)


@pytest.mark.parametrize("level", ["VERIFIED", "UNKNOWN", None])
def test_awaiting_verification_requires_an_existing_unverified_report(level):
    with pytest.raises(ValueError):
        decision(
            disposition="AWAIT_VERIFICATION",
            basis_plan_id="plan-1",
            basis_report_id="report-1",
            basis_report_revision=1,
            basis_verification_level=level,
        )


@pytest.mark.parametrize("name", ["verify_result", "approve_plan", "stage_assessment", None])
def test_field_receipt_names_cannot_grant_human_authority(name):
    with pytest.raises(ValueError):
        FieldToolReceipt(name, "{}", "{}")


@pytest.mark.parametrize(
    "raw", ['{"a":1,"a":1}', '{"x":NaN}', "[]", " {}", '{"x":' + "1" * 4096 + "}"]
)
def test_field_receipt_payload_is_a_bounded_canonical_object(raw):
    with pytest.raises(ValueError):
        FieldToolReceipt("get_field_context", raw, "{}")


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"field": {}},
        {"base": {}},
        {"context_digest": "X" * 64},
        {"field_trace": []},
        {"field_trace": ()},
        {"field_trace": (FieldToolReceipt("stage_field_decision", "{}", "{}"),)},
    ],
)
def test_assessment_has_exact_types_version_and_trace_shape(changes):
    with pytest.raises(ValueError):
        assessment(**changes)
