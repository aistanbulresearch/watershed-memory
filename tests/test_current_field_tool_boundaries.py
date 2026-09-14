"""A proposed follow-up cannot duplicate stranded work or bypass required reads."""

from datetime import timedelta

import pytest
from test_current_context_v3 import event_id, load
from test_current_field_store import CASE, NOW, SITE, SPEC, contents, mutate, proposed
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401
from test_current_field_tools import arguments, source_ready

from watershed_memory.current.case_types import HumanAction, ReviewDraft
from watershed_memory.current.field_tools import CurrentToolsV3
from watershed_memory.current.field_types import ProposeFieldPlan


def test_old_active_parent_blocker_stays_visible_ahead_of_newer_terminal_history(field):
    blocker = proposed(field).plan
    cases = field[1]
    cases.act(
        CASE,
        HumanAction(
            field[3].task_id,
            1,
            "MODIFY",
            title="Check the revised station plan",
            next_check_at=NOW + timedelta(hours=1),
        ),
        request_id="revise-blocked-parent",
        now=NOW,
    )
    for index in range(3):
        parent = cases.stage_review(
            CASE,
            ReviewDraft(
                "OBSERVATION_REVIEW",
                "Review the flow update",
                "Inspect the new station measurements.",
                event_id(field),
                NOW + timedelta(hours=1),
            ),
            request_id=f"other-parent-{index}",
            expected_case_revision=cases.context(CASE)["revision"],
            now=NOW,
        )
        mutate(
            field,
            "propose_plan",
            ProposeFieldPlan(parent.task_id, 1, SITE.location_id, 1, SPEC, True),
        )
        cases.act(
            CASE,
            HumanAction(parent.task_id, 1, "CANCEL"),
            request_id=f"close-other-{index}",
            now=NOW,
        )
    before = contents(field[0])
    context = load(field)
    assert context.field_work.current_plans == ()
    assert context.field_work.stranded_plans[0].plan.plan_id == blocker.plan_id
    assert context.field_work.stranded_plans[0].parent_binding == "SUPERSEDED"
    assert context.field_work.has_more_stranded_plans
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    for value in context.field_work.stranded_plans[:2]:
        tools.inspect_field_work(value.plan.plan_id)
    tools.list_approved_field_locations("VISUAL_INSPECTION")
    with pytest.raises(ValueError):
        tools.stage_field_decision(
            **arguments(context, "PROPOSE_FIELD_PLAN", context.field_work.stranded_plans[0], True)
        )
    assert contents(field[0]) == before


def test_every_actionable_plan_must_be_inspected_before_any_field_decision(field):
    proposed(field)
    context = load(field)
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    with pytest.raises(ValueError):
        tools.stage_field_decision(
            **arguments(context, "NO_NEW_FIELD_PLAN", context.field_work.current_plans[0])
        )
    tools.inspect_field_work(context.field_work.current_plans[0].plan.plan_id)
    tools.stage_field_decision(
        **arguments(context, "NO_NEW_FIELD_PLAN", context.field_work.current_plans[0])
    )
    assert tools.finish().field.basis_plan_id == context.field_work.current_plans[0].plan.plan_id


def test_source_phase_cannot_change_after_field_index_is_opened(field):
    context = load(field)
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    trace, attempts = tools.source.trace, tools.attempts
    with pytest.raises(ValueError):
        tools.source.inspect_current_series()
    assert tools.source.trace == trace and tools.attempts == attempts + 1
    tools.stage_field_decision(**arguments(context, "NO_NEW_FIELD_PLAN"))
    assert tools.finish().field.disposition == "NO_NEW_FIELD_PLAN"


@pytest.mark.parametrize("change", ["missing", "principal", "simulation"])
def test_field_stage_accepts_all_explicit_arguments_and_no_authority_override(field, change):
    context = load(field)
    tools = CurrentToolsV3(context)
    source_ready(tools)
    tools.get_field_context()
    values = arguments(context, "NO_NEW_FIELD_PLAN")
    if change == "missing":
        values.pop("basis_plan_id")
    elif change == "principal":
        values["principal"] = {"roles": ["VERIFIER"]}
    else:
        values["expected_simulated"] = False
    before = tools.field_trace
    with pytest.raises(ValueError):
        tools.stage_field_decision(**values)
    assert tools.field_trace == before
