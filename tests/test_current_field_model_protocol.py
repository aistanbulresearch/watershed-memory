"""Actual SDK checks for the state-driven current-field model interface."""

from __future__ import annotations

import asyncio
import copy
import inspect
from dataclasses import asdict, replace
from typing import Any

import jsonschema
import pytest
from strands import Agent, tool
from strands.models.model import Model
from strands.tools.executors import SequentialToolExecutor
from test_current_context_v3 import load
from test_current_field_review_regressions import _active, _context, _stage_source
from test_current_field_sdk_budget import BatchModel
from test_current_field_sdk_fixture import FieldResultScriptedModel
from test_current_field_store import attached, contents, verified
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401
from test_current_field_tool_budget import _active_snapshot, _max_context, _reported_copy

from watershed_memory.current.case_records import digest
from watershed_memory.current.context_v3_types import CurrentContextV3
from watershed_memory.current.field_codec import encode
from watershed_memory.current.field_model_protocol import (
    FieldModelProtocol,
    FieldProtocolRequestGuard,
)
from watershed_memory.current.field_models import field_dimension
from watershed_memory.current.field_sdk_budget import FieldSDKToolBudget
from watershed_memory.current.field_sdk_requests import FieldSDKRequestGuard
from watershed_memory.current.field_strands import CurrentStrandsPlannerV3, CurrentTurnErrorV3
from watershed_memory.current.field_tools import CurrentToolsV3


def _enum(spec: dict[str, Any], key: str) -> list[Any]:
    value = spec["inputSchema"]["json"]["properties"][key]
    if "enum" in value:
        return value["enum"]
    return next(branch["enum"] for branch in value.get("anyOf", []) if "enum" in branch)


class OneToolPerResponseModel(FieldResultScriptedModel):
    """Choose one call exclusively from the phase-filtered public tool specs."""

    def __init__(self) -> None:
        super().__init__()
        self.visible: list[dict[str, Any]] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.visible = tool_specs or []
        async for event in super().stream(
            messages, tool_specs, system_prompt, **kwargs
        ):
            yield event

    def _next_calls(self, values):
        names = [item["name"] for item in self.visible]
        if "get_case_context" in names:
            return [("get_case_context", {})]
        if "inspect_current_series" in names:
            return [("inspect_current_series", {})]
        if "inspect_source_health" in names:
            return [("inspect_source_health", {})]
        if "get_field_context" in names:
            return [("get_field_context", {})]
        if "find_relevant_reviews" in names and "stage_assessment" not in names:
            spec = next(item for item in self.visible if item["name"] == "find_relevant_reviews")
            return [("find_relevant_reviews", {"kind": _enum(spec, "kind")[0]})]
        if "stage_assessment" in names:
            return [("stage_assessment", self._assessment_call(values))]
        if "inspect_field_work" in names:
            calls = self._field_calls(values, final=False)
            return [next(call for call in calls if call[0] == "inspect_field_work")]
        if "list_approved_field_locations" in names:
            calls = self._field_calls(values, final=False)
            return [next(call for call in calls if call[0] == "list_approved_field_locations")]
        if "stage_field_decision" in names:
            calls = self._field_calls(values, final=True)
            return [next(call for call in calls if call[0] == "stage_field_decision")]
        return [("end_turn", {})]


def _planner(model, **kwargs):
    return CurrentStrandsPlannerV3(
        model,
        model_id="one-tool-current-field-fixture",
        scripted_test=True,
        **kwargs,
    )


def _functions(evidence: CurrentToolsV3):
    source_names = (
        "get_case_context",
        "inspect_current_series",
        "inspect_source_health",
        "compare_prior_event",
        "find_relevant_reviews",
        "inspect_alternate_sources",
        "stage_assessment",
    )
    functions = [getattr(evidence.source, name) for name in source_names]
    functions += [
        evidence.get_field_context,
        evidence.inspect_field_work,
        evidence.list_approved_field_locations,
        evidence.stage_field_decision,
    ]
    return functions


def _registered(evidence: CurrentToolsV3) -> list[dict[str, Any]]:
    return [tool(function).tool_spec for function in _functions(evidence)]


@pytest.mark.parametrize(
    "outcome,level,expected,calls",
    [
        ("PARTIAL", "VERIFIED", "PROPOSE_FIELD_PLAN", 10),
        ("COMPLETE", "VERIFIED", "NO_NEW_FIELD_PLAN", 9),
        ("COMPLETE", "EVIDENCE_ATTACHED", "AWAIT_VERIFICATION", 9),
    ],
)
def test_actual_sdk_one_tool_per_response_reaches_exact_field_decision(
    field, outcome, level, expected, calls
):
    (verified if level == "VERIFIED" else attached)(field, outcome)
    context = load(field)
    before = contents(field[0])
    model = OneToolPerResponseModel()

    result = _planner(model).plan(context)

    emitted = [name for response in model.responses for name, _ in response]
    assert emitted[:-1] == [
        "get_case_context",
        "inspect_current_series",
        "inspect_source_health",
        "find_relevant_reviews",
        "stage_assessment",
        "get_field_context",
        "inspect_field_work",
        *(["list_approved_field_locations"] if expected == "PROPOSE_FIELD_PLAN" else []),
        "stage_field_decision",
    ]
    assert emitted[-1] == "end_turn"
    assert all(len(response) == 1 for response in model.responses)
    assert result.model_calls == calls <= 12
    assert result.tool_attempts == calls - 1 <= 16
    assert result.assessment.field.disposition == expected
    basis = context.field_work.latest_results[0]
    assert result.assessment.field.basis_plan_id == basis.plan.plan_id
    assert result.assessment.field.basis_report_id == basis.result.report.report_id
    assert result.assessment.field.basis_report_revision == basis.result.report.revision
    assert result.assessment.field.basis_verification_level == level
    assert contents(field[0]) == before

    phases = [[item["name"] for item in specs] for specs in model.tool_specs]
    assert phases[0] == ["get_case_context"]
    first_stage_index = next(
        i for i, phase in enumerate(phases) if "stage_field_decision" in phase
    )
    assert all("stage_field_decision" not in phase for phase in phases[:first_stage_index])
    stage_index = next(i for i, phase in enumerate(phases) if phase == ["stage_field_decision"])
    final_spec = model.tool_specs[stage_index][0]
    final_payload = next(
        args for response in model.responses for name, args in response if name == "stage_field_decision"
    )
    jsonschema.validate(final_payload, final_spec["inputSchema"]["json"])
    assert _enum(final_spec, "basis_report_revision") == [basis.result.report.revision]


def test_filtered_view_does_not_mutate_registry_and_hidden_stage_still_fails_atomically(field):
    context = load(field)
    evidence = CurrentToolsV3(context)
    registered = _registered(evidence)
    original = copy.deepcopy(registered)
    visible = FieldModelProtocol(BatchModel([]), evidence).tool_specs(registered)
    assert [item["name"] for item in visible] == ["get_case_context"]
    assert registered == original and len(registered) == 11

    class CapturingBatch(BatchModel):
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            self.visible = copy.deepcopy(tool_specs)
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                yield event

    payload = {
        "disposition": "NO_NEW_FIELD_PLAN",
        "basis_plan_id": None,
        "basis_report_id": None,
        "basis_report_revision": None,
        "basis_verification_level": None,
        "reason": "A bounded hidden-stage test.",
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
    model = CapturingBatch([[('stage_field_decision', payload)]])
    before = contents(field[0])
    with pytest.raises(CurrentTurnErrorV3) as caught:
        _planner(model).plan(context)
    assert [item["name"] for item in model.visible] == ["get_case_context"]
    assert caught.value.failure.tool_attempts == 1
    assert caught.value.failure.source_trace == () and caught.value.failure.field_trace == ()
    assert contents(field[0]) == before


def test_source_phase_allows_two_distinct_new_reviews_without_forcing_the_second(field):
    original = load(field)
    base = replace(original.base, reviews=(), review_evidence=())
    context = CurrentContextV3(base, original.field_work, original.field_digest)
    evidence = CurrentToolsV3(context)
    protocol = FieldModelProtocol(BatchModel([]), evidence)
    specs = _registered(evidence)
    source = evidence.source
    source.get_case_context()
    source.inspect_current_series()
    source.inspect_source_health()

    first = protocol.tool_specs(specs)
    first_stage = next(item for item in first if item["name"] == "stage_assessment")
    assert first_stage["inputSchema"]["json"]["properties"]["kind"]["type"] == "null"
    assert "find_relevant_reviews" in {item["name"] for item in first}
    source.find_relevant_reviews("OBSERVATION_REVIEW")
    second = protocol.tool_specs(specs)
    assert {"stage_assessment", "find_relevant_reviews"} <= {
        item["name"] for item in second
    }
    source.stage_assessment(
        disposition="PROPOSE_REVIEW",
        kind="OBSERVATION_REVIEW",
        event_id=base.current.event_id,
        target_task_id=None,
        title="Review the current source observation",
        reason="Review the observed value before deciding any field follow-up.",
        next_check_at=None,
        reference_ids=[],
    )
    optional = {item["name"] for item in protocol.tool_specs(specs)}
    assert {"get_field_context", "find_relevant_reviews"} <= optional
    source.find_relevant_reviews("COVERAGE_REVIEW")
    after_lookup = protocol.tool_specs(specs)
    assert {"get_field_context", "stage_assessment"} <= {
        item["name"] for item in after_lookup
    }
    source.stage_assessment(
        disposition="PROPOSE_REVIEW",
        kind="COVERAGE_REVIEW",
        event_id=base.current.event_id,
        target_task_id=None,
        title="Review source coverage",
        reason="Review the recorded source coverage before any field follow-up.",
        next_check_at=None,
        reference_ids=[],
    )
    assert {item["name"] for item in protocol.tool_specs(specs)} == {"get_field_context"}
    assert len(source.finish().decisions) == 2


def test_unrelated_complete_history_keeps_new_parent_eligible_and_no_site_is_not_a_trap(field):
    context = _context(
        field,
        current_parent="observation",
        result_specs=(("unrelated-complete", "observation", "COMPLETE"),),
    )
    evidence = CurrentToolsV3(context)
    _stage_source(evidence)
    evidence.get_field_context()
    protocol = FieldModelProtocol(BatchModel([]), evidence)
    specs = _registered(evidence)
    while True:
        visible = protocol.tool_specs(specs)
        inspect_spec = next(
            (item for item in visible if item["name"] == "inspect_field_work"), None
        )
        if inspect_spec is None:
            break
        evidence.inspect_field_work(_enum(inspect_spec, "plan_id")[0])
    visible = protocol.tool_specs(specs)
    location = next(item for item in visible if item["name"] == "list_approved_field_locations")
    assert "VISUAL_INSPECTION" in _enum(location, "activity")

    field_work = replace(context.field_work, approved_locations=())
    without_sites = CurrentContextV3(
        context.base, field_work, digest(encode(asdict(field_work)))
    )
    no_site = CurrentToolsV3(without_sites)
    _stage_source(no_site)
    no_site.get_field_context()
    no_site_protocol = FieldModelProtocol(BatchModel([]), no_site)
    no_site_specs = _registered(no_site)
    while True:
        visible = no_site_protocol.tool_specs(no_site_specs)
        inspect_spec = next(
            (item for item in visible if item["name"] == "inspect_field_work"), None
        )
        if inspect_spec is None:
            break
        no_site.inspect_field_work(_enum(inspect_spec, "plan_id")[0])
    visible = no_site_protocol.tool_specs(no_site_specs)
    assert [item["name"] for item in visible] == ["stage_field_decision"]
    assert _enum(visible[0], "disposition") == ["NO_NEW_FIELD_PLAN"]


def test_complete_unverified_result_at_second_index_remains_an_exact_await_basis(field):
    context = _context(
        field,
        result_specs=(
            ("verified-first", "observation", "COMPLETE"),
            ("unverified-second", "coverage", "COMPLETE"),
        ),
    )
    first, second = context.field_work.latest_results
    unverified_result = replace(
        second.result,
        verification=None,
        verification_level="EVIDENCE_ATTACHED",
    )
    second = replace(
        second,
        result=unverified_result,
        field_dimension=field_dimension(second.plan, unverified_result),
    )
    field_work = replace(context.field_work, latest_results=(first, second))
    context = CurrentContextV3(
        context.base, field_work, digest(encode(asdict(field_work)))
    )
    evidence = CurrentToolsV3(context)
    _stage_source(evidence)
    evidence.get_field_context()
    protocol = FieldModelProtocol(BatchModel([]), evidence)
    specs = _registered(evidence)
    inspected = []
    while True:
        visible = protocol.tool_specs(specs)
        inspect_spec = next(
            (item for item in visible if item["name"] == "inspect_field_work"), None
        )
        if inspect_spec is None:
            break
        plan_id = _enum(inspect_spec, "plan_id")[0]
        inspected.append(plan_id)
        evidence.inspect_field_work(plan_id)
    assert inspected == [first.plan.plan_id, second.plan.plan_id]
    stage = next(
        item for item in protocol.tool_specs(specs) if item["name"] == "stage_field_decision"
    )
    assert "AWAIT_VERIFICATION" in _enum(stage, "disposition")
    payload = {
        "disposition": "AWAIT_VERIFICATION",
        "basis_plan_id": second.plan.plan_id,
        "basis_report_id": second.result.report.report_id,
        "basis_report_revision": second.result.report.revision,
        "basis_verification_level": second.result.verification_level,
        "reason": "Await human verification of the exact attached complete report.",
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
    jsonschema.validate(payload, stage["inputSchema"]["json"])
    evidence.stage_field_decision(**payload)
    assert evidence.finish().field.basis_plan_id == second.plan.plan_id


@pytest.mark.parametrize(
    "outcomes,eligible",
    [
        (("PARTIAL", "COMPLETE"), True),
        (("COMPLETE", "PARTIAL"), False),
    ],
)
def test_proposal_eligibility_uses_newest_result_for_same_parent(field, outcomes, eligible):
    context = _context(
        field,
        result_specs=(
            ("same-parent-newest", "coverage", outcomes[0]),
            ("same-parent-older", "coverage", outcomes[1]),
        ),
    )
    target = context.base.reviews[0]
    base = replace(
        context.base,
        reviews=(target,),
        review_evidence=((target.task_id, ()),),
    )
    context = CurrentContextV3(base, context.field_work, context.field_digest)
    evidence = CurrentToolsV3(context)
    protocol = FieldModelProtocol(BatchModel([]), evidence)
    activities = protocol._proposal_activities()
    assert bool(activities) is eligible
    if not eligible:
        assert protocol._required_plan_ids() == (
            context.field_work.latest_results[0].plan.plan_id,
        )


def test_max_density_actual_sdk_batches_required_reads_and_finishes_with_eight_receipts(field):
    context = _max_context(field, disposition="NO_NEW_FIELD_PLAN")
    template = context.field_work.latest_results[0]
    coverage, observation = context.base.reviews
    stranded = (
        _active_snapshot(template, coverage, "dense-superseded-coverage", binding="SUPERSEDED"),
        _active_snapshot(template, observation, "dense-superseded-observation", binding="SUPERSEDED"),
        context.field_work.stranded_plans[0],
    )
    latest = (
        template,
        _reported_copy(template, coverage, "dense-second-result"),
        _reported_copy(template, observation, "dense-third-result"),
    )
    field_work = replace(context.field_work, stranded_plans=stranded, latest_results=latest)
    context = CurrentContextV3(
        context.base, field_work, digest(encode(asdict(field_work)))
    )

    class DenseModel(OneToolPerResponseModel):
        def _active_review(self, values):
            reviews = values.get("find_relevant_reviews", {}).get("reviews", [])
            if not reviews:
                raise AssertionError("dense fixture requires a successful review lookup")
            return reviews[0]["kind"], reviews[0]

        def _next_calls(self, values):
            names = [item["name"] for item in self.visible]
            if "inspect_field_work" in names:
                spec = next(item for item in self.visible if item["name"] == "inspect_field_work")
                return [
                    ("inspect_field_work", {"plan_id": plan_id})
                    for plan_id in _enum(spec, "plan_id")
                ]
            if "stage_field_decision" in names:
                index = values["get_field_context"]["latest_results"][0]
                detail = values[f"inspect_field_work:{index['plan_id']}"]
                result = detail["result"]
                return [
                    (
                        "stage_field_decision",
                        {
                            "disposition": "NO_NEW_FIELD_PLAN",
                            "basis_plan_id": detail["plan"]["plan_id"],
                            "basis_report_id": result["report"]["report_id"],
                            "basis_report_revision": result["report"]["revision"],
                            "basis_verification_level": result["verification_level"],
                            "reason": "Keep the exact verified complete field result.",
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
                        },
                    )
                ]
            return super()._next_calls(values)

    model = DenseModel()
    result = _planner(model).plan(context)
    assert len(result.assessment.field_trace) == 8
    assert [item.name for item in result.assessment.field_trace].count("inspect_field_work") == 6
    assert result.assessment.field.disposition == "NO_NEW_FIELD_PLAN"
    assert result.model_calls <= 12 and result.tool_attempts <= 16
    inspect_batches = [
        response for response in model.responses if response and response[0][0] == "inspect_field_work"
    ]
    assert len(inspect_batches) == 1 and len(inspect_batches[0]) == 6


@pytest.mark.parametrize("case", ["inspect_location", "duplicate_read", "stage_then_read"])
def test_actual_sdk_refuses_unsafe_near_capacity_batch_atomically(field, case):
    context = _max_context(field, disposition="NO_NEW_FIELD_PLAN")
    template = context.field_work.latest_results[0]
    coverage = context.base.reviews[0]
    optional = _reported_copy(template, coverage, "near-capacity-optional")
    blocker = _active_snapshot(
        template, coverage, "near-capacity-superseded", binding="SUPERSEDED"
    )
    field_work = replace(
        context.field_work,
        stranded_plans=(blocker, *context.field_work.stranded_plans),
        latest_results=(template, optional),
    )
    context = CurrentContextV3(
        context.base, field_work, digest(encode(asdict(field_work)))
    )
    evidence = CurrentToolsV3(context)
    _stage_source(evidence)
    evidence.get_field_context()
    protocol = FieldModelProtocol(BatchModel([]), evidence)
    basis = None
    for plan_id in protocol._required_plan_ids():
        detail = evidence.inspect_field_work(plan_id)
        if plan_id == template.plan.plan_id:
            basis = detail
    assert basis is not None and len(evidence.field_trace) == 6
    before_trace = evidence.field_trace
    result = basis["result"]
    stage = (
        "stage_field_decision",
        {
            "disposition": "NO_NEW_FIELD_PLAN",
            "basis_plan_id": basis["plan"]["plan_id"],
            "basis_report_id": result["report"]["report_id"],
            "basis_report_revision": result["report"]["revision"],
            "basis_verification_level": result["verification_level"],
            "reason": "Keep the exact verified complete field result.",
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
        },
    )
    inspect_optional = ("inspect_field_work", {"plan_id": optional.plan.plan_id})
    location = ("list_approved_field_locations", {"activity": "VISUAL_INSPECTION"})
    batches = {
        "inspect_location": [inspect_optional, location],
        "duplicate_read": [inspect_optional, inspect_optional],
        "stage_then_read": [stage, inspect_optional],
    }
    functions = _functions(evidence)
    request_budget = FieldSDKToolBudget()
    before = contents(field[0])
    agent = Agent(
        model=BatchModel([batches[case]]),
        tools=[tool(function) for function in functions],
        hooks=[
            request_budget,
            FieldSDKRequestGuard(functions),
            FieldProtocolRequestGuard(protocol),
        ],
        tool_executor=SequentialToolExecutor(),
        callback_handler=None,
        retry_strategy=None,
    )
    completed = asyncio.run(agent.invoke_async("Exercise an unsafe assembled field batch."))
    assert completed.stop_reason == "end_turn"
    assert request_budget.attempts == 2
    statuses = [
        block["toolResult"].get("status")
        for message in agent.messages
        for block in message.get("content", [])
        if "toolResult" in block
    ]
    assert statuses.count("error") == 2
    assert evidence.field_trace == before_trace and evidence._decision is None
    assert contents(field[0]) == before


def test_actual_sdk_refuses_every_tool_after_final_field_stage(field):
    verified(field, "COMPLETE")
    context = load(field)
    evidence = CurrentToolsV3(context)
    _stage_source(evidence)
    evidence.get_field_context()
    protocol = FieldModelProtocol(BatchModel([]), evidence)
    detail = None
    for plan_id in protocol._required_plan_ids():
        detail = evidence.inspect_field_work(plan_id)
    assert detail is not None
    result = detail["result"]
    evidence.stage_field_decision(
        disposition="NO_NEW_FIELD_PLAN",
        basis_plan_id=detail["plan"]["plan_id"],
        basis_report_id=result["report"]["report_id"],
        basis_report_revision=result["report"]["revision"],
        basis_verification_level=result["verification_level"],
        reason="Keep the exact verified complete field result.",
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
    source_before, field_before = evidence.source.trace, evidence.field_trace
    functions = _functions(evidence)
    request_budget = FieldSDKToolBudget()
    agent = Agent(
        model=BatchModel([[("get_case_context", {})]]),
        tools=[tool(function) for function in functions],
        hooks=[
            request_budget,
            FieldSDKRequestGuard(functions),
            FieldProtocolRequestGuard(protocol),
        ],
        tool_executor=SequentialToolExecutor(),
        callback_handler=None,
        retry_strategy=None,
    )
    asyncio.run(agent.invoke_async("Attempt a read after the final stage."))
    statuses = [
        block["toolResult"].get("status")
        for message in agent.messages
        for block in message.get("content", [])
        if "toolResult" in block
    ]
    assert statuses.count("error") == 1 and request_budget.attempts == 1
    assert evidence.source.trace == source_before and evidence.field_trace == field_before


@pytest.mark.parametrize("binding", ["CURRENT", "SUPERSEDED"])
def test_active_or_stranded_work_blocks_duplicate_proposal_visibility(field, binding):
    context = _context(field, current_parent="observation")
    coverage, _ = context.base.reviews
    template = context.field_work.current_plans[0]
    blocker = _active(template, coverage, f"coverage-{binding.lower()}", binding=binding)
    if binding == "CURRENT":
        current = tuple(sorted((*context.field_work.current_plans, blocker), key=lambda x: x.plan.task_id))
        stranded = ()
    else:
        current = context.field_work.current_plans
        stranded = (blocker,)
    field_work = replace(context.field_work, current_plans=current, stranded_plans=stranded)
    blocked_context = CurrentContextV3(
        context.base, field_work, digest(encode(asdict(field_work)))
    )
    evidence = CurrentToolsV3(blocked_context)
    assert FieldModelProtocol(BatchModel([]), evidence)._proposal_activities() == ()


def test_wrapper_preserves_model_api_shape_and_delegates_without_state_changes(field):
    class Delegate(Model):
        def __init__(self):
            self.config = {"model_id": "delegate"}
            self.calls = []

        @property
        def stateful(self):
            return True

        def get_config(self):
            return self.config

        def update_config(self, **model_config):
            self.config.update(model_config)

        async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
            self.calls.append(("structured_output", output_model, prompt, system_prompt, kwargs))
            yield {"output": "structured"}

        async def count_tokens(
            self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None
        ):
            self.calls.append(("count_tokens", messages, tool_specs, system_prompt, system_prompt_content))
            return 23

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            self.calls.append(("stream", messages, tool_specs, system_prompt, kwargs))
            yield {"messageStop": {"stopReason": "end_turn"}}

    delegate = Delegate()
    wrapper = FieldModelProtocol(delegate, CurrentToolsV3(load(field)))
    for method in ("get_config", "update_config", "structured_output", "count_tokens", "stream"):
        expected = inspect.signature(getattr(Model, method))
        actual = inspect.signature(getattr(FieldModelProtocol, method))
        assert tuple(expected.parameters) == tuple(actual.parameters)
        assert [item.kind for item in expected.parameters.values()] == [
            item.kind for item in actual.parameters.values()
        ]
    assert wrapper.stateful and wrapper.get_config() == {"model_id": "delegate"}
    wrapper.update_config(region="test")
    assert delegate.config["region"] == "test"

    async def exercise():
        structured = [item async for item in wrapper.structured_output(dict, [], "system", x=1)]
        tokens = await wrapper.count_tokens([], [], "system", [])
        streamed = [item async for item in wrapper.stream([], [], "system", x=2)]
        return structured, tokens, streamed

    structured, tokens, streamed = asyncio.run(exercise())
    assert structured == [{"output": "structured"}]
    assert tokens == 23
    assert streamed == [{"messageStop": {"stopReason": "end_turn"}}]
    assert [item[0] for item in delegate.calls] == ["structured_output", "count_tokens", "stream"]
