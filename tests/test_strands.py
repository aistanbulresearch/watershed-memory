"""Execute the installed Strands SDK with a scripted provider, never billed inference."""

import json

import pytest
from strands.models.model import Model

from watershed_memory.catalog import PACKETS
from watershed_memory.planning import ReplayPlanner
from watershed_memory.service import Service
from watershed_memory.strands_agent import AgentTurnError, StrandsPlanner


class ScriptedModel(Model):
    """SDK protocol fixture: emits known tool calls to test the real tool execution loop."""

    def __init__(self, calls):
        self.calls = iter(calls)

    def get_config(self):
        return {"model_id": "scripted-test-provider"}

    def update_config(self, **_kwargs):
        pass

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        call = next(self.calls, None)
        yield {"messageStart": {"role": "assistant"}}
        if call:
            name, inputs = call
            yield {"contentBlockStart": {"contentBlockIndex": 0,
                                         "start": {"toolUse": {"toolUseId": f"tool-{len(messages)}", "name": name}}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": json.dumps(inputs)}}}}
        else:
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "The review proposal is ready."}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "tool_use" if call else "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}}


def calls_for(packet):
    return [
        ("get_case_context", {}),
        ("get_observations", {"event_id": packet["event_id"]}),
        ("propose_review", {"kind": "MONITORING_REVIEW", "event_id": packet["event_id"],
                            "existing_task_id": None,
                            "reason": "Review the new P2 observations in the source-water case."}),
    ]


def test_real_sdk_executes_tools_and_service_commits(tmp_path):
    planner = StrandsPlanner(lambda: ScriptedModel(calls_for(PACKETS[0])),
                            model_id="scripted-test-provider", scripted_test=True)
    service = Service(tmp_path / "case.sqlite", planner)
    session = service.create_session()["session_id"]
    state = service.advance(session, "sdk-test")
    assert len(state["tasks"]) == 1
    assert state["mode"]["id"] == "scripted_sdk_test"
    assert state["mode"]["agent_enabled"] is False
    assert [item["tool"] for item in state["trace"]] == [
        "get_case_context", "get_observations", "propose_review", "strands_turn", "commit_case_turn"]
    assert state["trace"][-2]["output"]["model_calls"] == 4


def test_sdk_budget_failure_leaves_case_unchanged(tmp_path):
    planner = StrandsPlanner(lambda: ScriptedModel(calls_for(PACKETS[0])),
                            model_id="scripted-test-provider", scripted_test=True, max_calls=2)
    service = Service(tmp_path / "case.sqlite", planner)
    state = service.create_session()
    with pytest.raises(RuntimeError, match="budget"):
        service.advance(state["session_id"], "budget-test")
    assert service.snapshot(state["session_id"]) == state


def test_sdk_model_initialization_error_is_recorded(tmp_path):
    def broken_factory():
        raise ValueError("Test configuration is missing")

    planner = StrandsPlanner(broken_factory, model_id="scripted-test-provider", scripted_test=True)
    service = Service(tmp_path / "case.sqlite", planner)
    state = service.create_session()
    with pytest.raises(AgentTurnError, match="configuration") as error:
        service.advance(state["session_id"], "configuration-test")
    assert error.value.evidence["model_calls_attempted"] == 0
    assert service.snapshot(state["session_id"]) == state


class ContextDrivenScriptedModel(ScriptedModel):
    """Explicit rules fixture that reads real SDK tool results; not an LLM."""

    def __init__(self):
        self.step = 0
        self.packet = None
        self.context = None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.step == 0:
            call = ("get_case_context", {})
        elif self.step == 1:
            result = messages[-1]["content"][0]["toolResult"]["content"][0]
            self.context = result.get("json") or json.loads(result["text"])
            call = ("get_observations", {"event_id": self.context["current_event_id"]})
        elif self.step == 2:
            result = messages[-1]["content"][0]["toolResult"]["content"][0]
            self.packet = result.get("json") or json.loads(result["text"])
            target = next((t["id"] for t in self.context["tasks"]
                           if t["kind"] == "MONITORING_REVIEW" and t["status"] != "COMPLETED"), None)
            call = ("propose_review", {"kind": "MONITORING_REVIEW",
                "event_id": self.packet["event_id"], "existing_task_id": target,
                "reason": "Link the current P2 observations to the context-selected review."})
        elif self.step == 3 and self.packet["p1_turbidity_count"] == 0:
            call = ("propose_review", {"kind": "EVIDENCE_GAP_REVIEW",
                "existing_task_id": None,
                "event_id": self.packet["event_id"],
                "reason": "P1 numeric evidence is absent from the current historical window."})
        else:
            call = None
        self.step += 1
        self.calls = iter([call]) if call else iter([])
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


@pytest.mark.parametrize("completed", [False, True])
def test_sdk_reads_context_to_link_or_create_and_add_gap(tmp_path, completed):
    service = Service(tmp_path / "case.sqlite", ReplayPlanner())
    session = service.create_session()["session_id"]
    old = service.advance(session, "july")["tasks"][0]["id"]
    service.planner = StrandsPlanner(ContextDrivenScriptedModel,
                                    model_id="context-scripted-provider", scripted_test=True)
    august = service.advance(session, "august")
    assert august["tasks"][0]["id"] == old
    assert len(august["tasks"][0]["evidence"]) == 2
    if completed:
        service.respond(session, "complete", old, "complete_review", "Prior review complete.")
    september = service.advance(session, "september")
    assert len(september["tasks"]) == (3 if completed else 2)
    assert september["tasks"][0]["status"] == ("COMPLETED" if completed else "OPEN")
    assert len(september["tasks"][0]["evidence"]) == (2 if completed else 3)
    proposal = next(item for item in september["trace"] if item["tool"] == "propose_review")
    assert proposal["input"]["existing_task_id"] == (None if completed else old)
