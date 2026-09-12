"""Regressions for field proposal basis and bounded parent visibility."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest
from test_current_context_v3 import event_id, load
from test_current_field_store import CASE, NOW, SITE, SPEC, mutate, proposed, verified
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.case_records import digest
from watershed_memory.current.case_types import HumanAction, ReviewDraft
from watershed_memory.current.context_v3_types import CurrentContextV3
from watershed_memory.current.field_codec import encode
from watershed_memory.current.field_models import FieldWorkSnapshot, field_dimension
from watershed_memory.current.field_tools import CurrentToolsV3, validate_assessment_v3
from watershed_memory.current.field_types import ProposeFieldPlan


def _reviews(base):
    seed = base.reviews[0]
    coverage = replace(
        seed,
        task_id="regression-coverage-parent",
        kind="COVERAGE_REVIEW",
        title="Review source coverage",
    )
    observation = replace(
        seed,
        task_id="regression-observation-parent",
        kind="OBSERVATION_REVIEW",
        title="Review current observations",
    )
    return coverage, observation


def _reported(template, parent, label, outcome):
    plan_id = f"regression-{label}-plan"
    report_id = f"regression-{label}-report"
    plan = replace(
        template.plan,
        plan_id=plan_id,
        task_id=parent.task_id,
        review_revision=parent.revision,
    )
    report = replace(template.result.report, report_id=report_id, plan_id=plan_id, outcome=outcome)
    evidence = tuple(
        replace(item, evidence_id=f"regression-{label}-evidence-{index}", report_id=report_id)
        for index, item in enumerate(template.result.evidence)
    )
    verification = replace(
        template.result.verification,
        verification_id=f"regression-{label}-verification",
        report_id=report_id,
        evidence_ids=tuple(item.evidence_id for item in evidence),
    )
    result = replace(
        template.result,
        report=report,
        evidence=evidence,
        verification=verification,
        verification_level="VERIFIED",
    )
    return replace(
        template,
        plan=plan,
        result=result,
        parent_binding="CURRENT",
        field_dimension=field_dimension(plan, result),
    )


def _active(template, parent, label, *, binding="CURRENT"):
    plan = replace(
        template.plan,
        plan_id=f"regression-{label}-plan",
        task_id=parent.task_id,
        review_revision=parent.revision,
        revision=1,
        status="PROPOSED",
        change_kind="PROPOSE",
        recorded_by="regression-fixture",
        deferred_until=None,
    )
    return FieldWorkSnapshot(plan, None, binding, field_dimension(plan, None))


def _context(
    field,
    *,
    result_specs=(),
    current_parent=None,
    has_more_results=False,
    evaluated_at=None,
):
    verified(field, "PARTIAL")
    original = load(field)
    coverage, observation = _reviews(original.base)
    base = replace(
        original.base,
        reviews=(coverage, observation),
        review_evidence=((coverage.task_id, ()), (observation.task_id, ())),
    )
    template = original.field_work.latest_results[0]
    parents = {"coverage": coverage, "observation": observation}
    results = tuple(
        _reported(template, parents[parent], label, outcome)
        for label, parent, outcome in result_specs
    )
    current = ()
    if current_parent is not None:
        current = (_active(template, parents[current_parent], f"{current_parent}-current"),)
    when = evaluated_at or original.base.evaluated_at
    if evaluated_at is not None:
        base = replace(base, evaluated_at=when, source_health=None)
    field_work = replace(
        original.field_work,
        evaluated_at=when,
        current_plans=current,
        stranded_plans=(),
        latest_results=results,
        has_more_stranded_plans=False,
        has_more_results=has_more_results,
    )
    return CurrentContextV3(base, field_work, digest(encode(asdict(field_work))))


def _stage_source(tools):
    source = tools.source
    source.get_case_context()
    source.inspect_current_series()
    if source.context.source_health is not None:
        source.inspect_source_health()
    for review in source.context.reviews:
        source.find_relevant_reviews(review.kind)
        source.stage_assessment(
            disposition="CONTINUE_EXISTING_REVIEW",
            kind=review.kind,
            event_id=source.context.current.event_id,
            target_task_id=review.task_id,
            title=None,
            reason="Continue this exact active source review for the regression fixture.",
            next_check_at=None,
            reference_ids=[],
        )


def _proposal_arguments(context, parent, basis=None, *, hours=1):
    result = basis.result if basis is not None else None
    spec = replace(
        SPEC,
        window_start=context.base.evaluated_at,
        window_end=context.base.evaluated_at + timedelta(hours=hours),
    )
    return {
        "disposition": "PROPOSE_FIELD_PLAN",
        "basis_plan_id": basis.plan.plan_id if basis else None,
        "basis_report_id": result.report.report_id if result else None,
        "basis_report_revision": result.report.revision if result else None,
        "basis_verification_level": result.verification_level if result else None,
        "reason": "Use only relevant prior field work for this unapproved proposal.",
        "task_id": parent.task_id,
        "review_revision": parent.revision,
        "location_id": SITE.location_id,
        "location_revision": SITE.revision,
        "activity": spec.activity,
        "purpose": spec.purpose,
        "assignee_role": spec.assignee_role,
        "window_start": spec.window_start.isoformat(),
        "window_end": spec.window_end.isoformat(),
        "required_evidence": list(spec.required_evidence),
    }


def _prepare_proposal(context, *, inspect=()):
    tools = CurrentToolsV3(context)
    _stage_source(tools)
    index = tools.get_field_context()
    assert index["case_id"] == CASE and index["simulated"] is True
    for snapshot in inspect:
        detail = tools.inspect_field_work(snapshot.plan.plan_id)
        assert detail["plan"]["case_id"] == CASE
    tools.list_approved_field_locations("VISUAL_INSPECTION")
    return tools


def test_null_basis_allows_unblocked_parent_with_unrelated_truncated_history(field):
    context = _context(
        field,
        current_parent="observation",
        result_specs=(
            ("other-newest", "observation", "COMPLETE"),
            ("other-middle", "observation", "PARTIAL"),
            ("other-oldest", "observation", "NOT_DONE"),
        ),
        has_more_results=True,
    )
    target = context.base.reviews[0]
    other_plan = context.field_work.current_plans[0]
    assert target.kind == "COVERAGE_REVIEW" and other_plan.plan.task_id != target.task_id
    assert all(item.plan.task_id != target.task_id for item in context.field_work.latest_results)
    tools = _prepare_proposal(context, inspect=(other_plan,))
    tools.stage_field_decision(**_proposal_arguments(context, target))
    assessment = tools.finish()
    assert assessment.field.basis_plan_id is None
    assert validate_assessment_v3(context, assessment) == assessment


@pytest.mark.parametrize("outcome", ["PARTIAL", "NOT_DONE"])
def test_proposal_accepts_newest_selected_incomplete_result_for_same_task(field, outcome):
    context = _context(field, result_specs=(("target", "coverage", outcome),))
    target = context.base.reviews[0]
    basis = context.field_work.latest_results[0]
    tools = _prepare_proposal(context, inspect=(basis,))
    tools.stage_field_decision(**_proposal_arguments(context, target, basis))
    assert tools.finish().field.basis_report_id == basis.result.report.report_id


def test_proposal_can_use_target_result_at_position_two_without_first_global_inspection(field):
    context = _context(
        field,
        result_specs=(
            ("other-selected-first", "observation", "COMPLETE"),
            ("target-newest", "coverage", "PARTIAL"),
        ),
    )
    target = context.base.reviews[0]
    unrelated, basis = context.field_work.latest_results
    tools = _prepare_proposal(context, inspect=(basis,))
    inspection_inputs = [
        item.input_json for item in tools.field_trace if item.name == "inspect_field_work"
    ]
    assert all(unrelated.plan.plan_id not in item for item in inspection_inputs)
    tools.stage_field_decision(**_proposal_arguments(context, target, basis))
    assessment = tools.finish()
    assert assessment.field.basis_plan_id == basis.plan.plan_id


@pytest.mark.parametrize("case", ["unrelated", "complete", "no-result", "older"])
def test_proposal_rejects_irrelevant_or_non_incomplete_basis(field, case):
    if case == "unrelated":
        context = _context(field, result_specs=(("other", "observation", "PARTIAL"),))
        basis = context.field_work.latest_results[0]
        inspected = (basis,)
    elif case == "complete":
        context = _context(field, result_specs=(("target", "coverage", "COMPLETE"),))
        basis = context.field_work.latest_results[0]
        inspected = (basis,)
    elif case == "no-result":
        context = _context(field, current_parent="observation")
        basis = context.field_work.current_plans[0]
        inspected = (basis,)
    else:
        context = _context(
            field,
            result_specs=(
                ("target-selected-first", "coverage", "COMPLETE"),
                ("target-selected-second", "coverage", "PARTIAL"),
            ),
        )
        basis = context.field_work.latest_results[1]
        inspected = context.field_work.latest_results
    target = context.base.reviews[0]
    tools = _prepare_proposal(context, inspect=inspected)
    with pytest.raises(ValueError):
        tools.stage_field_decision(**_proposal_arguments(context, target, basis))


def test_null_basis_is_rejected_when_target_has_a_selected_result(field):
    context = _context(field, result_specs=(("target", "coverage", "PARTIAL"),))
    target = context.base.reviews[0]
    basis = context.field_work.latest_results[0]
    tools = _prepare_proposal(context, inspect=(basis,))
    with pytest.raises(ValueError):
        tools.stage_field_decision(**_proposal_arguments(context, target))


@pytest.mark.parametrize("superseded", [False, True])
def test_unsupported_active_parent_kind_fails_closed_for_any_revision_binding(field, superseded):
    proposed(field)
    if superseded:
        parent = field[1].get_review(CASE, field[3].task_id)
        field[1].act(
            CASE,
            HumanAction(
                parent.task_id,
                parent.revision,
                "MODIFY",
                title="Modified active parent before corruption",
                next_check_at=NOW + timedelta(hours=1),
            ),
            request_id="supersede-parent-before-corruption",
            now=NOW,
        )
    with field[1]._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE current_reviews SET kind='RESULT_VERIFICATION' WHERE case_id=? AND task_id=?",
            (CASE, field[3].task_id),
        )
    with pytest.raises(ValueError, match="unsupported parent kind"):
        load(field, NOW)


def _stage_observation_parent(field, label):
    return field[1].stage_review(
        CASE,
        ReviewDraft(
            "OBSERVATION_REVIEW",
            f"Observation review {label}",
            "Inspect the exact current observation before field work.",
            event_id(field),
            NOW + timedelta(hours=1),
        ),
        request_id=f"stage-observation-{label}",
        expected_case_revision=field[1].context(CASE)["revision"],
        now=NOW,
    )


def _supersede(field, parent, label):
    return field[1].act(
        CASE,
        HumanAction(
            parent.task_id,
            parent.revision,
            "MODIFY",
            title=f"Modified active parent {label}",
            next_check_at=NOW + timedelta(hours=1),
        ),
        request_id=f"modify-parent-{label}",
        now=NOW,
    )


def test_stranded_selection_keeps_both_blockers_then_newest_terminal_with_truncation(field):
    coverage_blocker = proposed(field).plan
    _supersede(field, field[3], "coverage")

    terminal = []
    for index in range(3):
        parent = _stage_observation_parent(field, f"terminal-{index}")
        plan = mutate(
            field,
            "propose_plan",
            ProposeFieldPlan(parent.task_id, parent.revision, SITE.location_id, 1, SPEC, True),
        ).plan
        terminal.append(plan.plan_id)
        field[1].act(
            CASE,
            HumanAction(parent.task_id, parent.revision, "CANCEL"),
            request_id=f"cancel-observation-{index}",
            now=NOW,
        )

    observation_parent = _stage_observation_parent(field, "blocker")
    observation_blocker = mutate(
        field,
        "propose_plan",
        ProposeFieldPlan(
            observation_parent.task_id,
            observation_parent.revision,
            SITE.location_id,
            1,
            SPEC,
            True,
        ),
    ).plan
    _supersede(field, observation_parent, "observation")

    context = load(field, NOW)
    selected = context.field_work.stranded_plans
    assert [item.plan.plan_id for item in selected] == [
        observation_blocker.plan_id,
        coverage_blocker.plan_id,
        terminal[-1],
    ]
    assert [item.parent_binding for item in selected] == ["SUPERSEDED", "SUPERSEDED", "TERMINAL"]
    assert context.field_work.current_plans == ()
    assert context.field_work.has_more_stranded_plans is True


def test_one_hour_proposal_near_datetime_limit_is_valid(field):
    evaluated_at = datetime(9999, 12, 15, tzinfo=timezone.utc)
    context = _context(
        field,
        result_specs=(("target", "coverage", "PARTIAL"),),
        evaluated_at=evaluated_at,
    )
    target = context.base.reviews[0]
    basis = context.field_work.latest_results[0]
    tools = _prepare_proposal(context, inspect=(basis,))
    tools.stage_field_decision(**_proposal_arguments(context, target, basis, hours=1))
    assert tools.finish().field.proposal.spec.window_end == evaluated_at + timedelta(hours=1)


def test_executed_field_selection_uses_indexes_without_a_history_sort(field):
    from test_current_field_store import CASE, NOW, proposed

    from watershed_memory.current.case_records import case_row
    from watershed_memory.current.field_context import _load_field_context

    proposed(field)
    statements = []
    with field[1]._connect() as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        db.set_trace_callback(statements.append)
        _load_field_context(db, case_row(db, CASE), field[2].locations, evaluated_at=NOW)
        db.set_trace_callback(None)
        selectors = (
            ("FROM current_reviews r INDEXED BY", ("current_active_kind", "field_active_review")),
            ("SELECT p.plan_id FROM field_plans p JOIN", ("field_plan_case_recent",)),
            ("SELECT plan_id FROM field_plans WHERE", ("field_plan_case_recent",)),
        )
        for marker, indexes in selectors:
            queries = [query for query in statements if marker in query]
            assert len(queries) == 1
            details = "\n".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + queries[0]))
            assert "TEMP B-TREE" not in details
            for index in indexes:
                assert index in details


def test_real_ledger_orders_task_results_and_capture_rejects_reversed_history(field):
    from test_current_context_v3 import capture, restore
    from test_current_field_store import OPERATOR, contents
    from test_current_field_tools import arguments, source_ready

    from watershed_memory.current.field_types import DecideFieldPlan, ReportFieldResult

    old = verified(field, "PARTIAL")
    later = NOW + timedelta(minutes=20)
    plan = mutate(
        field,
        "propose_plan",
        ProposeFieldPlan(
            field[3].task_id,
            field[3].revision,
            SITE.location_id,
            SITE.revision,
            replace(SPEC, window_start=later),
            True,
        ),
        now=later,
    ).plan
    plan = mutate(
        field,
        "decide_plan",
        DecideFieldPlan(plan.plan_id, plan.revision, "APPROVE"),
        now=later,
    ).plan
    newest = mutate(
        field,
        "report_result",
        ReportFieldResult(
            plan.plan_id,
            plan.revision,
            "COMPLETE",
            "The follow-up inspection covered the remaining marker.",
            later,
            later + timedelta(minutes=2),
            True,
        ),
        who=OPERATOR,
        now=later + timedelta(minutes=3),
    )
    context = load(field, later + timedelta(minutes=4))
    assert [item.result.report.report_id for item in context.field_work.latest_results] == [
        newest.result.report.report_id,
        old.result.report.report_id,
    ]
    with field[1]._connect() as db:
        revisions = dict(db.execute("SELECT plan_id,last_activity_revision FROM field_plans"))
    assert revisions[newest.plan.plan_id] > revisions[old.plan.plan_id]
    before = contents(field[0])
    assert restore(field, capture(field, context)) == context
    reversed_field = replace(
        context.field_work, latest_results=tuple(reversed(context.field_work.latest_results))
    )
    forged = replace(
        context, field_work=reversed_field, field_digest=digest(encode(asdict(reversed_field)))
    )
    with pytest.raises(ValueError):
        capture(field, forged)
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    for item in context.field_work.latest_results:
        tools.inspect_field_work(item.plan.plan_id)
    tools.list_approved_field_locations(SPEC.activity)
    with pytest.raises(ValueError):
        tools.stage_field_decision(
            **arguments(context, "PROPOSE_FIELD_PLAN", context.field_work.latest_results[1], True)
        )
    assert contents(field[0]) == before
