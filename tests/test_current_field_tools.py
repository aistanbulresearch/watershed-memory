"""Same observation, different remembered field outcome, explicit staged follow-up."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_context_v3 import load
from test_current_field_store import (
    CANARY,
    NOW,
    SITE,
    SPEC,
    attached,
    contents,
    verified,
)
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.field_tools import CurrentToolsV3, validate_assessment_v3


def source_ready(tools):
    source = tools.source
    source.get_case_context()
    source.inspect_current_series()
    if source.context.source_health is not None:
        source.inspect_source_health()
    source.find_relevant_reviews("COVERAGE_REVIEW")
    source.stage_assessment(
        "CONTINUE_EXISTING_REVIEW",
        "COVERAGE_REVIEW",
        source.context.current.event_id,
        source.context.reviews[0].task_id,
        None,
        "Keep the existing human source review in view.",
        None,
        [],
    )


def arguments(context, disposition, basis=None, proposal=False):
    report = basis.result if basis is not None else None
    values = dict(
        disposition=disposition,
        basis_plan_id=basis.plan.plan_id if basis else None,
        basis_report_id=report.report.report_id if report else None,
        basis_report_revision=report.report.revision if report else None,
        basis_verification_level=report.verification_level if report else None,
        reason="Use the saved field outcome to choose the next assignment.",
        task_id=None,
        review_revision=None,
        location_id=None,
        location_revision=None,
        activity=None,
        purpose=None,
        assignee_role=None,
        window_start=None,
        window_end=None,
        required_evidence=None,
    )
    if proposal:
        spec = replace(
            SPEC,
            window_start=context.base.evaluated_at,
            window_end=context.base.evaluated_at + timedelta(hours=1),
        )
        values.update(
            task_id=context.base.reviews[0].task_id,
            review_revision=context.base.reviews[0].revision,
            location_id=SITE.location_id,
            location_revision=SITE.revision,
            activity=spec.activity,
            purpose=spec.purpose,
            assignee_role=spec.assignee_role,
            window_start=spec.window_start.isoformat(),
            window_end=spec.window_end.isoformat(),
            required_evidence=list(spec.required_evidence),
        )
    return values


@pytest.mark.parametrize(
    "outcome,level,disposition",
    [
        ("COMPLETE", "VERIFIED", "NO_NEW_FIELD_PLAN"),
        ("COMPLETE", "EVIDENCE_ATTACHED", "AWAIT_VERIFICATION"),
        ("PARTIAL", "VERIFIED", "PROPOSE_FIELD_PLAN"),
    ],
)
def test_saved_field_result_supports_three_distinct_explicit_decisions(
    field, outcome, level, disposition
):
    (verified if level == "VERIFIED" else attached)(field, outcome)
    context = load(field)
    before = contents(field[0])
    tools = CurrentToolsV3(context)
    source_ready(tools)
    index = tools.get_field_context()
    result = context.field_work.latest_results[0]
    assert index["latest_results"][0]["plan_id"] == result.plan.plan_id
    detail = tools.inspect_field_work(result.plan.plan_id)
    assert detail["result"]["verification_level"] == level
    if disposition == "PROPOSE_FIELD_PLAN":
        sites = tools.list_approved_field_locations("VISUAL_INSPECTION")
        assert sites["locations"][0]["location_id"] == SITE.location_id
    staged = tools.stage_field_decision(
        **arguments(context, disposition, result, disposition == "PROPOSE_FIELD_PLAN")
    )
    assert staged["status"] == "STAGED" and staged["committed"] is False
    assessment = tools.finish()
    assert assessment.field.disposition == disposition
    assert assessment.field.basis_report_id == result.result.report.report_id
    assert assessment.field.basis_report_revision == result.result.report.revision
    assert assessment.field.basis_verification_level == level
    assert validate_assessment_v3(context, assessment) == assessment
    assert CANARY not in str(assessment)
    assert contents(field[0]) == before


def test_first_field_proposal_needs_a_real_context_site_and_human_review(field):
    context = load(field)
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    tools.list_approved_field_locations("VISUAL_INSPECTION")
    tools.stage_field_decision(**arguments(context, "PROPOSE_FIELD_PLAN", proposal=True))
    value = tools.finish()
    assert value.field.proposal.task_id == field[3].task_id
    assert value.field.basis_plan_id is None
    assert value.field.proposal.spec.window_start == context.base.evaluated_at


def test_read_outputs_are_copies_and_unknown_field_work_is_unavailable(field):
    verified(field)
    context = load(field)
    tools = CurrentToolsV3(context)
    source_ready(tools)
    index = tools.get_field_context()
    index["latest_results"].clear()
    assert len(context.field_work.latest_results) == 1
    with pytest.raises(ValueError):
        tools.inspect_field_work("not-in-this-context")
    assert len(tools.field_trace) == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"basis_report_revision": 99},
        {"basis_verification_level": "VERIFIED"},
        {"basis_report_id": "wrong-report"},
        {"basis_plan_id": "wrong-plan"},
    ],
)
def test_stage_cannot_invent_result_identity_or_verification(field, changed):
    attached(field)
    context = load(field)
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    basis = context.field_work.latest_results[0]
    tools.inspect_field_work(basis.plan.plan_id)
    values = arguments(context, "AWAIT_VERIFICATION", basis)
    values.update(changed)
    before = tools.field_trace
    with pytest.raises(ValueError):
        tools.stage_field_decision(**values)
    assert tools.field_trace == before
    with pytest.raises(ValueError):
        tools.finish()


def test_partial_result_cannot_be_described_as_awaiting_completion_verification(field):
    attached(field, "PARTIAL")
    context = load(field)
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    basis = context.field_work.latest_results[0]
    tools.inspect_field_work(basis.plan.plan_id)
    with pytest.raises(ValueError):
        tools.stage_field_decision(**arguments(context, "AWAIT_VERIFICATION", basis))


@pytest.mark.parametrize("missing", ["source", "index", "detail", "location"])
def test_field_decision_requires_source_context_and_each_relevant_read(field, missing):
    verified(field, "PARTIAL")
    context = load(field)
    tools = CurrentToolsV3(context)
    if missing == "source":
        with pytest.raises(ValueError):
            tools.get_field_context()
        return
    source_ready(tools)
    if missing != "index":
        tools.get_field_context()
    basis = context.field_work.latest_results[0]
    if missing == "index":
        with pytest.raises(ValueError):
            tools.inspect_field_work(basis.plan.plan_id)
        return
    if missing != "detail":
        tools.inspect_field_work(basis.plan.plan_id)
    if missing != "location":
        tools.list_approved_field_locations("VISUAL_INSPECTION")
    with pytest.raises(ValueError):
        tools.stage_field_decision(**arguments(context, "PROPOSE_FIELD_PLAN", basis, True))


@pytest.mark.parametrize(
    "change",
    [
        {"task_id": "wrong-parent"},
        {"review_revision": 99},
        {"location_id": "USGS-08380500"},
        {"location_revision": 2},
        {"window_start": NOW.isoformat()},
        {"required_evidence": ("PHOTO_REFERENCE",)},
        {"window_end": (NOW + timedelta(days=31)).isoformat()},
    ],
)
def test_proposal_uses_exact_review_location_and_bounded_future_window(field, change):
    context = load(field)
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    tools.list_approved_field_locations("VISUAL_INSPECTION")
    values = arguments(context, "PROPOSE_FIELD_PLAN", proposal=True)
    values.update(change)
    with pytest.raises(ValueError):
        tools.stage_field_decision(**values)


def test_replay_rejects_a_changed_field_receipt(field):
    verified(field)
    context = load(field)
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    basis = context.field_work.latest_results[0]
    tools.inspect_field_work(basis.plan.plan_id)
    tools.stage_field_decision(**arguments(context, "NO_NEW_FIELD_PLAN", basis))
    result = tools.finish()
    forged = replace(
        result,
        field_trace=(
            result.field_trace[0],
            replace(result.field_trace[1], output_json="{}"),
            result.field_trace[2],
        ),
    )
    with pytest.raises(ValueError):
        validate_assessment_v3(context, forged)
