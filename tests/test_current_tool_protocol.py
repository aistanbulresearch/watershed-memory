"""Production current-field prompt and installed SDK protocol checks."""

from __future__ import annotations

from test_current_context_v3 import load
from test_current_field_sdk_fixture import FieldResultScriptedModel
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401
from test_current_field_store import verified

from watershed_memory.current.field_strands import SYSTEM_PROMPT, CurrentStrandsPlannerV3


def _planner(model):
    return CurrentStrandsPlannerV3(
        model,
        model_id="tool-protocol-controlled-fixture",
        scripted_test=True,
    )


def test_prompt_names_complete_ordered_protocol_and_closed_values():
    ordered = (
        "1. Call get_case_context first",
        "2. Compare only allowlisted prior event IDs",
        "3. Stage the source decision",
        "4. Call get_field_context",
        "5. Call inspect_field_work",
        "6. The field dispositions",
        "7. Before PROPOSE_FIELD_PLAN, call list_approved_field_locations",
        "8. Stage exactly one field decision",
    )
    positions = [SYSTEM_PROMPT.index(marker) for marker in ordered]
    assert positions == sorted(positions)
    for value in (
        "OBSERVATION_REVIEW",
        "COVERAGE_REVIEW",
        "NO_FOLLOW_UP",
        "PROPOSE_REVIEW",
        "CONTINUE_EXISTING_REVIEW",
        "NO_NEW_FIELD_PLAN",
        "AWAIT_VERIFICATION",
        "PROPOSE_FIELD_PLAN",
        "REPORTED",
        "EVIDENCE_ATTACHED",
        "VERIFIED",
        "VISUAL_INSPECTION",
        "SAMPLING",
        "MAINTENANCE_REVIEW",
        "PHOTO_REFERENCE",
        "SAMPLE_RECORD_REFERENCE",
        "INSPECTION_RECORD_REFERENCE",
        "MAINTENANCE_RECORD_REFERENCE",
    ):
        assert value in SYSTEM_PROMPT
    assert "all eight stage_assessment arguments" in SYSTEM_PROMPT
    assert "all sixteen stage_field_decision arguments" in SYSTEM_PROMPT
    assert "executor runs a batch sequentially" in SYSTEM_PROMPT
    assert "explicit JSON nulls" in SYSTEM_PROMPT


def test_actual_sdk_follows_partial_result_to_bounded_field_followup(field):
    verified(field, "PARTIAL")
    context = load(field)
    model = FieldResultScriptedModel()

    result = _planner(model).plan(context)

    source_names = [item.name for item in result.assessment.base.trace]
    field_names = [item.name for item in result.assessment.field_trace]
    assert source_names == [
        "get_case_context",
        "inspect_current_series",
        "inspect_source_health",
        "find_relevant_reviews",
        "stage_assessment",
    ]
    assert field_names == [
        "get_field_context",
        "inspect_field_work",
        "list_approved_field_locations",
        "stage_field_decision",
    ]
    assert result.model_calls == 6
    assert result.tool_attempts == 9
    assert result.model_calls <= 8 and result.tool_attempts <= 16
    assert result.assessment.field.disposition == "PROPOSE_FIELD_PLAN"

    basis = context.field_work.latest_results[0]
    proposal = result.assessment.field.proposal
    assert result.assessment.field.basis_plan_id == basis.plan.plan_id
    assert result.assessment.field.basis_report_id == basis.result.report.report_id
    assert result.assessment.field.basis_report_revision == basis.result.report.revision
    assert result.assessment.field.basis_verification_level == "VERIFIED"
    assert proposal is not None
    assert proposal.task_id == context.base.reviews[0].task_id
    assert proposal.review_revision == context.base.reviews[0].revision

    schemas = {item["name"]: item["inputSchema"]["json"] for item in model.tool_specs[0]}
    assert set(schemas["stage_assessment"]["required"]) == set(
        schemas["stage_assessment"]["properties"]
    )
    assert set(schemas["stage_field_decision"]["required"]) == set(
        schemas["stage_field_decision"]["properties"]
    )
    assert len(schemas["stage_assessment"]["required"]) == 8
    assert len(schemas["stage_field_decision"]["required"]) == 16
