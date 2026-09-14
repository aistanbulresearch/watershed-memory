"""Real installed SDK, scripted provider: protocol plumbing, not model reasoning."""

import asyncio
import json
from dataclasses import FrozenInstanceError

import pytest
from test_current_case_store import CASE, LATER, MONITOR, action, stage
from test_current_case_store import ready as ready
from test_current_tools import context
from test_strands import ScriptedModel

from watershed_memory.current.strands import CurrentStrandsPlanner, CurrentTurnError
from watershed_memory.current.tools import validate_assessment


class CurrentScriptedModel(ScriptedModel):
    """Handwritten fixture rules read actual tool outputs to choose exact work IDs."""

    def __init__(self):
        self.step = 0
        self.outputs = []
        self.schemas = None
        self.prompt = None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.schemas, self.prompt = tool_specs, system_prompt
        if self.step:
            block = messages[-1]["content"][0]["toolResult"]
            assert block["status"] == "success"
            value = block["content"][0]
            self.outputs.append(value.get("json") or json.loads(value["text"]))
        if self.step == 0:
            call = ("get_case_context", {})
        elif self.step == 1:
            call = ("inspect_current_series", {})
        elif self.step == 2:
            call = (
                "compare_prior_event",
                {"event_id": self.outputs[0]["available_prior_events"][0]["event_id"]},
            )
        elif self.step == 3:
            call = ("find_relevant_reviews", {"kind": "COVERAGE_REVIEW"})
        elif self.step == 4:
            call = ("inspect_alternate_sources", {"parameter_code": "63680"})
        elif self.step == 5:
            reviews = self.outputs[3]["reviews"]
            call = (
                "stage_assessment",
                {
                    "disposition": "CONTINUE_EXISTING_REVIEW" if reviews else "PROPOSE_REVIEW",
                    "kind": "COVERAGE_REVIEW",
                    "event_id": self.outputs[0]["current_event_id"],
                    "target_task_id": reviews[0]["task_id"] if reviews else None,
                    "title": None if reviews else "Verify missing turbidity observations",
                    "reason": "Turbidity observations remain absent; no compatible source is configured.",
                    "next_check_at": None if reviews else LATER.isoformat(),
                    "reference_ids": [self.outputs[0]["available_prior_events"][0]["event_id"]],
                },
            )
        else:
            call = None
        self.step += 1
        self.calls = iter([call]) if call else iter([])
        async for chunk in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield chunk


def planner(model_factory=CurrentScriptedModel, **options):
    return CurrentStrandsPlanner(
        model_factory(), model_id="current-scripted-fixture", scripted_test=True, **options
    )


def test_current_sdk_retrieves_compares_investigates_and_stages_without_committing(ready):
    model = CurrentScriptedModel()
    ctx = context(ready)
    before = ready[1].context(CASE), ready[2].status(MONITOR)
    execution = planner(lambda: model).plan(ctx)
    assert execution.mode == "SCRIPTED_SDK" and execution.model_calls == 7
    assert execution.tool_attempts == 6
    assert execution.instruction_version == "watershed-current-v1"
    assert execution.sdk_version and execution.stop_reason == "end_turn"
    assert json.loads(execution.usage_json)["inputTokens"] == 7
    assert execution.elapsed_seconds >= 0
    assert [r.name for r in execution.assessment.trace] == [
        "get_case_context",
        "inspect_current_series",
        "compare_prior_event",
        "find_relevant_reviews",
        "inspect_alternate_sources",
        "stage_assessment",
    ]
    assert validate_assessment(ctx, execution.assessment) == execution.assessment
    assert before == (ready[1].context(CASE), ready[2].status(MONITOR))
    with pytest.raises(FrozenInstanceError):
        execution.mode = "STRANDS_CURRENT"
    names = {spec["name"] for spec in model.schemas}
    assert names == {r.name for r in execution.assessment.trace}
    schema = next(s for s in model.schemas if s["name"] == "stage_assessment")["inputSchema"][
        "json"
    ]
    assert set(schema["required"]) == {
        "disposition",
        "kind",
        "event_id",
        "target_task_id",
        "title",
        "reason",
        "next_check_at",
        "reference_ids",
    }
    assert "historical replay" not in str(model.prompt).lower()


def test_same_source_event_sdk_target_changes_with_the_saved_human_context(ready):
    first = planner().plan(context(ready))
    assert first.assessment.decisions[0].disposition == "PROPOSE_REVIEW"
    review = stage(ready[1], ready[3][0])
    human = action(
        ready[1],
        review,
        "MODIFY",
        title="Call the station contact tomorrow",
        next_check_at=LATER,
        note="PRIVATE-CANARY credentials must never enter model",
    )
    model = CurrentScriptedModel()
    second = planner(lambda: model).plan(context(ready))
    selected = second.assessment.decisions[0]
    assert first.assessment.event_id == second.assessment.event_id
    assert (
        selected.disposition == "CONTINUE_EXISTING_REVIEW"
        and selected.target_task_id == human.task_id
    )
    assert selected.title is None and selected.next_check_at is None
    assert "PRIVATE-CANARY" not in str(model.outputs) + str(second) + str(model.prompt)
    assert ready[1].get_review(CASE, human.task_id) == human


def test_model_call_budget_stops_before_more_provider_work_and_retains_no_decision(ready):
    before = ready[1].context(CASE)
    with pytest.raises(CurrentTurnError) as caught:
        planner(max_calls=2).plan(context(ready))
    assert caught.value.model_calls == 2
    assert len(caught.value.trace) == 2
    assert ready[1].context(CASE) == before


def test_failed_provider_text_cannot_enter_a_persistable_failure_record(ready):
    class FailureModel(ScriptedModel):
        async def stream(self, *args, **kwargs):
            raise ValueError("PRIVATE-PROVIDER-SECRET http://localhost/private")
            yield  # pragma: no cover

    with pytest.raises(CurrentTurnError) as caught:
        planner(lambda: FailureModel([])).plan(context(ready))
    assert caught.value.model_calls == 1 and caught.value.trace == ()
    assert "PRIVATE-PROVIDER-SECRET" not in str(caught.value) + str(caught.value.evidence)


def test_failure_carries_immutable_case_and_execution_identity(ready):
    with pytest.raises(CurrentTurnError) as caught:
        planner(lambda: ScriptedModel([])).plan(context(ready))
    error = caught.value
    failure = error.failure
    assert failure.mode == "SCRIPTED_SDK" and failure.model_id == "current-scripted-fixture"
    assert failure.instruction_version == "watershed-current-v1" and failure.sdk_version
    assert failure.case_id == CASE and failure.event_id == ready[3][1].event_id
    assert failure.case_revision == 0 and failure.policy_digest == context(ready).policy_digest
    with pytest.raises(FrozenInstanceError):
        failure.status = "COMPLETED"
    public = error.evidence
    public["status"] = "COMPLETED"
    public["trace"] = [{"name": "forged"}]
    assert error.evidence["status"] == "FAILED" and error.evidence["trace"] == []


def test_provider_construction_is_an_explicit_prerequisite_not_an_unbounded_turn_factory():
    called = []

    def unbounded_factory():
        called.append(True)
        raise AssertionError("Factory must not be executed by the adapter")

    with pytest.raises(ValueError):
        CurrentStrandsPlanner(unbounded_factory, model_id="fixture", scripted_test=True)
    assert called == []


def test_sdk_text_without_staged_work_is_not_an_assessment(ready):
    with pytest.raises(CurrentTurnError):
        planner(lambda: ScriptedModel([])).plan(context(ready))


def test_sdk_timeout_cancels_the_turn_without_ledger_changes(ready):
    class SlowModel(ScriptedModel):
        async def stream(self, *args, **kwargs):
            await asyncio.sleep(1)
            async for chunk in super().stream(*args, **kwargs):
                yield chunk

    with pytest.raises(CurrentTurnError):
        planner(lambda: SlowModel([]), seconds=0.03).plan(context(ready))
    assert ready[1].context(CASE)["revision"] == 0


@pytest.mark.parametrize(
    "options",
    [
        {"max_calls": 0},
        {"max_calls": 9},
        {"max_calls": True},
        {"seconds": 0},
        {"seconds": 121},
        {"seconds": float("nan")},
        {"seconds": True},
    ],
)
def test_planner_cannot_silently_expand_per_turn_guards(options):
    with pytest.raises(ValueError):
        planner(**options)
