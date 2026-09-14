"""Count SDK requests before name lookup and argument validation, without paid inference."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from strands import Agent, tool
from strands.tools.executors import SequentialToolExecutor
from strands.types.exceptions import EventLoopException
from test_strands import ScriptedModel

from watershed_memory.current.field_sdk_budget import FieldSDKToolBudget


class BatchModel(ScriptedModel):
    def __init__(self, batches):
        self.batches = iter(batches)
        self.responses = 0

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        batch = next(self.batches, [])
        self.responses += 1
        yield {"messageStart": {"role": "assistant"}}
        for index, (name, inputs) in enumerate(batch):
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": index,
                    "start": {
                        "toolUse": {
                            "toolUseId": f"request-{self.responses}-{index}",
                            "name": name,
                        }
                    },
                }
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": index,
                    "delta": {
                        "toolUse": {"input": json.dumps(inputs)},
                    },
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": index}}
        if not batch:
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Done."}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "tool_use" if batch else "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


def invoke(batches, budget, seen):
    @tool
    def read_probe(value: int):
        """Read one integer in an isolated SDK fixture."""
        seen.append(value)
        return {"value": value}

    agent = Agent(
        model=BatchModel(batches),
        tools=[read_probe],
        hooks=[budget],
        tool_executor=SequentialToolExecutor(),
        callback_handler=None,
        retry_strategy=None,
    )
    return asyncio.run(agent.invoke_async("Exercise the bounded fixture."))


@pytest.mark.parametrize(
    "batch,executed",
    [
        ([("read_probe", {"value": 1})], [1]),
        ([("missing_tool", {})], []),
        ([("invalid name", {})], []),
        ([("read_probe", {})], []),
        ([("missing_tool", {}), ("read_probe", {"value": 2}), ("read_probe", {})], [2]),
    ],
)
def test_actual_sdk_counts_attempts_before_validation(batch, executed):
    budget, seen = FieldSDKToolBudget(), []
    result = invoke([batch], budget, seen)
    assert result.stop_reason == "end_turn"
    assert budget.attempts == len(batch)
    assert not budget.exhausted
    assert seen == executed


def test_actual_sdk_accepts_exactly_sixteen_requests_once():
    budget, seen = FieldSDKToolBudget(), []
    invoke([[("read_probe", {"value": n}) for n in range(16)]], budget, seen)
    assert seen == list(range(16))
    assert budget.attempts == 16 and not budget.exhausted


@pytest.mark.parametrize(
    "batches,executed,admitted",
    [
        ([[("read_probe", {"value": n}) for n in range(17)]], [], 0),
        (
            [
                [("read_probe", {"value": n}) for n in range(15)],
                [("read_probe", {"value": 15}), ("missing_tool", {})],
            ],
            list(range(15)),
            15,
        ),
        ([[("missing_tool", {}) for _ in range(16)], [("read_probe", {"value": 1})]], [], 16),
    ],
)
def test_actual_sdk_refuses_whole_overflow_batch_before_any_bound_call(batches, executed, admitted):
    budget, seen = FieldSDKToolBudget(), []
    with pytest.raises(EventLoopException, match="^Field SDK tool request limit exceeded\\.$"):
        invoke(batches, budget, seen)
    assert seen == executed
    assert budget.attempts == admitted and budget.exhausted


def test_assembled_tool_blocks_are_charged_before_identity_validation():
    budget = FieldSDKToolBudget()
    budget.before_tools(
        SimpleNamespace(
            message={
                "content": [
                    {"text": "ordinary text"},
                    {"toolUse": {}},
                    {"toolUse": None},
                    {"toolUse": {"input": "PRIVATE-INPUT-CANARY"}},
                ]
            }
        )
    )
    assert budget.attempts == 3


def test_exhausted_guard_cannot_be_reopened():
    budget = FieldSDKToolBudget()
    event = SimpleNamespace(message={"content": [{"toolUse": {}}] * 17})
    with pytest.raises(RuntimeError):
        budget.before_tools(event)
    with pytest.raises(RuntimeError):
        budget.before_tools(SimpleNamespace(message={"content": []}))
    assert budget.attempts == 0 and budget.exhausted
