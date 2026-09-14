"""Exact v3 shared-budget coverage with typed synthetic context expansion."""

from dataclasses import asdict, replace
from hashlib import sha256

import pytest
from test_current_context_v3 import load
from test_current_field_store import CASE, SITE, SPEC, verified
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.case_records import digest
from watershed_memory.current.context_v3_types import CurrentContextV3
from watershed_memory.current.field_codec import encode
from watershed_memory.current.field_models import FieldWorkSnapshot, field_dimension
from watershed_memory.current.field_tools import CurrentToolsV3, validate_assessment_v3
from watershed_memory.current.tools import CurrentTools


def _identity(label: str) -> str:
    return sha256(label.encode("ascii")).hexdigest()


def _active_snapshot(template, review, label, *, binding="CURRENT"):
    plan = replace(
        template.plan,
        plan_id=f"budget-{label}-plan",
        task_id=review.task_id,
        review_revision=review.revision,
        revision=1,
        status="PROPOSED",
        change_kind="PROPOSE",
        author_kind="HUMAN",
        recorded_by="budget-fixture",
        deferred_until=None,
    )
    return FieldWorkSnapshot(plan, None, binding, field_dimension(plan, None))


def _reported_copy(template, review, label):
    plan_id = f"budget-{label}-plan"
    report_id = f"budget-{label}-report"
    plan = replace(
        template.plan,
        plan_id=plan_id,
        task_id=review.task_id,
        review_revision=review.revision,
    )
    report = replace(template.result.report, report_id=report_id, plan_id=plan_id)
    evidence = tuple(
        replace(item, evidence_id=f"budget-{label}-evidence-{index}", report_id=report_id)
        for index, item in enumerate(template.result.evidence)
    )
    verification = replace(
        template.result.verification,
        verification_id=f"budget-{label}-verification",
        report_id=report_id,
        evidence_ids=tuple(item.evidence_id for item in evidence),
    )
    result = replace(
        template.result,
        report=report,
        evidence=evidence,
        verification=verification,
    )
    return replace(
        template,
        plan=plan,
        result=result,
        field_dimension=field_dimension(plan, result),
    )


def _max_context(field, *, disposition, await_position_two=False):
    verified(field, "PARTIAL" if disposition == "PROPOSE_FIELD_PLAN" else "COMPLETE")
    original = load(field)
    base = original.base
    prior_seed = base.prior[0]
    priors = tuple(
        replace(
            prior_seed,
            event_id=_identity(f"budget-prior-{index}"),
            revision=1,
            supersedes_event_id=None,
        )
        for index in range(3)
    )

    review_seed = base.reviews[0]
    coverage = replace(
        review_seed,
        task_id="budget-coverage-parent",
        kind="COVERAGE_REVIEW",
        title="Review source coverage",
    )
    observation = replace(
        review_seed,
        task_id="budget-observation-parent",
        kind="OBSERVATION_REVIEW",
        title="Review current observations",
    )
    base = replace(
        base,
        prior=priors,
        reviews=(coverage, observation),
        review_evidence=((coverage.task_id, ()), (observation.task_id, ())),
    )

    latest = original.field_work.latest_results[0]
    unrelated_latest = _reported_copy(latest, observation, "unrelated-latest")
    latest_plan = replace(
        latest.plan,
        task_id=coverage.task_id,
        review_revision=coverage.revision,
    )
    latest_result = latest.result
    if disposition == "AWAIT_VERIFICATION":
        latest_result = replace(
            latest_result,
            verification=None,
            verification_level="EVIDENCE_ATTACHED",
        )
    latest = replace(
        latest,
        plan=latest_plan,
        result=latest_result,
        parent_binding="CURRENT",
        field_dimension=field_dimension(latest_plan, latest_result),
    )

    current = (
        _active_snapshot(latest, coverage, "coverage"),
        _active_snapshot(latest, observation, "observation"),
    )
    if disposition == "PROPOSE_FIELD_PLAN":
        current = (current[1],)
    terminal_review = replace(
        coverage,
        task_id="budget-terminal-parent",
        status="CANCELLED",
        title="Closed historical field parent",
    )
    terminal = _active_snapshot(latest, terminal_review, "terminal", binding="TERMINAL")
    latest_results = (unrelated_latest, latest) if await_position_two else (latest,)
    field_work = replace(
        original.field_work,
        current_plans=current,
        stranded_plans=(terminal,),
        latest_results=latest_results,
        has_more_stranded_plans=False,
        has_more_results=False,
    )
    context = CurrentContextV3(base, field_work, digest(encode(asdict(field_work))))
    assert context.base.case_id == context.field_work.case_id == CASE
    assert context.base.simulated is context.field_work.simulated is True
    return context


def _stage_max_source(tools):
    source = tools.source
    source.get_case_context()
    source.inspect_current_series()
    assert source.context.source_health is not None
    source.inspect_source_health()
    references = [item.event_id for item in source.context.prior]
    for event_id in references:
        source.compare_prior_event(event_id)
    for kind in ("COVERAGE_REVIEW", "OBSERVATION_REVIEW"):
        found = source.find_relevant_reviews(kind)
        assert len(found["reviews"]) == 1
    for review in source.context.reviews:
        source.stage_assessment(
            disposition="CONTINUE_EXISTING_REVIEW",
            kind=review.kind,
            event_id=source.context.current.event_id,
            target_task_id=review.task_id,
            title=None,
            reason="Continue the exact active source review after inspecting prior evidence.",
            next_check_at=None,
            reference_ids=references,
        )
    assert len(source.trace) == tools.attempts == 10


def _decision_arguments(context, disposition, basis, *, proposal=False):
    result = basis.result
    values = {
        "disposition": disposition,
        "basis_plan_id": basis.plan.plan_id,
        "basis_report_id": result.report.report_id,
        "basis_report_revision": result.report.revision,
        "basis_verification_level": result.verification_level,
        "reason": "Use the exact saved field result and verification level for this decision.",
        "task_id": None,
        "review_revision": None,
        "location_id": None,
        "location_revision": None,
        "activity": None,
        "purpose": None,
        "assignee_role": None,
        "window_start": None,
        "window_end": None,
        "required_evidence": None,
    }
    if proposal:
        parent = context.base.reviews[0]
        spec = replace(
            SPEC,
            window_start=context.base.evaluated_at,
            window_end=context.base.evaluated_at + (SPEC.window_end - SPEC.window_start),
        )
        values.update(
            task_id=parent.task_id,
            review_revision=parent.revision,
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


def _inspect_required_field_work(tools, context):
    index = tools.get_field_context()
    assert tools.source.closed is True
    assert index["case_id"] == CASE and index["simulated"] is True
    for name in ("current_plans", "stranded_plans", "latest_results"):
        expected = [item.plan.plan_id for item in getattr(context.field_work, name)]
        assert [item["plan_id"] for item in index[name]] == expected
    required = (
        *context.field_work.current_plans,
        *context.field_work.stranded_plans,
        context.field_work.latest_results[0],
    )
    for snapshot in required:
        detail = tools.inspect_field_work(snapshot.plan.plan_id)
        assert detail["plan"]["plan_id"] == snapshot.plan.plan_id
        assert detail["plan"]["case_id"] == CASE
        assert detail["plan"]["simulated"] is True
    return context.field_work.latest_results[0]


@pytest.mark.parametrize(
    ("disposition", "level"),
    [
        ("NO_NEW_FIELD_PLAN", "VERIFIED"),
        ("AWAIT_VERIFICATION", "EVIDENCE_ATTACHED"),
    ],
)
def test_no_new_and_await_exactly_fill_required_sixteen_calls(field, disposition, level):
    context = _max_context(field, disposition=disposition)
    tools = CurrentToolsV3(context)
    _stage_max_source(tools)
    basis = _inspect_required_field_work(tools, context)
    assert basis.result.verification_level == level
    assert tools.attempts == 15 and len(tools.field_trace) == 5
    tools.stage_field_decision(**_decision_arguments(context, disposition, basis))
    assert tools.attempts == 16 and len(tools.field_trace) == 6
    assessment = tools.finish()
    assert len(assessment.base.trace) == 10
    assert assessment.field.disposition == disposition
    assert validate_assessment_v3(context, assessment) == assessment


def test_await_position_two_uses_exact_basis_without_inspecting_unrelated_global_latest(field):
    context = _max_context(
        field,
        disposition="AWAIT_VERIFICATION",
        await_position_two=True,
    )
    unrelated, basis = context.field_work.latest_results
    assert unrelated.result.verification_level == "VERIFIED"
    assert basis.result.verification_level == "EVIDENCE_ATTACHED"
    assert basis.result.report.outcome == "COMPLETE"

    tools = CurrentToolsV3(context)
    _stage_max_source(tools)
    index = tools.get_field_context()
    assert [item["plan_id"] for item in index["latest_results"]] == [
        unrelated.plan.plan_id,
        basis.plan.plan_id,
    ]
    for snapshot in (
        *context.field_work.current_plans,
        *context.field_work.stranded_plans,
        basis,
    ):
        tools.inspect_field_work(snapshot.plan.plan_id)
    inspected = [item.input_json for item in tools.field_trace if item.name == "inspect_field_work"]
    assert all(unrelated.plan.plan_id not in item for item in inspected)
    assert tools.attempts == 15 and len(tools.field_trace) == 5

    tools.stage_field_decision(
        **_decision_arguments(context, "AWAIT_VERIFICATION", basis)
    )
    assert tools.attempts == 16 and len(tools.field_trace) == 6
    assessment = tools.finish()
    assert assessment.field.basis_plan_id == basis.plan.plan_id
    assert validate_assessment_v3(context, assessment) == assessment


def test_proposal_exactly_fills_sixteen_calls_with_only_other_parent_active(field):
    context = _max_context(field, disposition="PROPOSE_FIELD_PLAN")
    tools = CurrentToolsV3(context)
    _stage_max_source(tools)
    basis = _inspect_required_field_work(tools, context)
    assert basis.result.report.outcome == "PARTIAL"
    assert len(context.field_work.current_plans) == 1
    target = context.base.reviews[0]
    assert all(item.plan.task_id != target.task_id for item in context.field_work.current_plans)
    locations = tools.list_approved_field_locations("VISUAL_INSPECTION")
    assert locations["locations"][0]["location_id"] == SITE.location_id
    assert locations["locations"][0]["simulated"] is True
    tools.stage_field_decision(
        **_decision_arguments(context, "PROPOSE_FIELD_PLAN", basis, proposal=True)
    )
    assert tools.attempts == 16 and len(tools.field_trace) == 6
    assessment = tools.finish()
    assert assessment.field.proposal.task_id == target.task_id
    assert validate_assessment_v3(context, assessment) == assessment


def test_seventeenth_attempt_permanently_exhausts_v3_and_blocks_finish(field):
    context = _max_context(field, disposition="NO_NEW_FIELD_PLAN")
    tools = CurrentToolsV3(context)
    _stage_max_source(tools)
    basis = _inspect_required_field_work(tools, context)
    tools.stage_field_decision(
        **_decision_arguments(context, "NO_NEW_FIELD_PLAN", basis)
    )
    assert tools.attempts == 16
    with pytest.raises(RuntimeError, match="tool attempt budget exhausted"):
        tools.source.get_case_context()
    assert tools.attempts == 16
    for _ in range(2):
        with pytest.raises(RuntimeError, match="tool attempt budget exhausted"):
            tools.finish()


def test_legacy_current_tools_still_exhausts_on_thirteenth_attempt(field):
    verified(field)
    context = load(field).base
    tools = CurrentTools(context)
    tools.get_case_context()
    tools.inspect_current_series()
    tools.inspect_source_health()
    found = tools.find_relevant_reviews("COVERAGE_REVIEW")["reviews"][0]
    for _ in range(7):
        tools.get_case_context()
    tools.stage_assessment(
        disposition="CONTINUE_EXISTING_REVIEW",
        kind="COVERAGE_REVIEW",
        event_id=context.current.event_id,
        target_task_id=found["task_id"],
        title=None,
        reason="Continue the exact active source review after reading the current evidence.",
        next_check_at=None,
        reference_ids=[],
    )
    assert tools.attempts == len(tools.trace) == 12
    assert tools.finish().decisions[0].target_task_id == found["task_id"]
    with pytest.raises(RuntimeError, match="tool attempt budget exhausted"):
        tools.get_case_context()
    with pytest.raises(RuntimeError, match="tool attempt budget exhausted"):
        tools.finish()
