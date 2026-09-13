"""V3 rejects SDK coercion, ignored extras and ambiguous batch identity before tools."""

import asyncio
from types import SimpleNamespace

import pytest
from strands import Agent, tool
from strands.tools.executors import SequentialToolExecutor
from test_current_field_sdk_budget import BatchModel

from watershed_memory.current.field_sdk_budget import FieldSDKToolBudget
from watershed_memory.current.field_sdk_requests import FieldSDKRequestGuard


def fixture(batch):
    seen = []

    def read_probe(value: int, tags: list[str] | None):
        """Read exact fixture integers and nullable text lists."""
        seen.append((value, tags))
        return {"accepted": True}

    budget = FieldSDKToolBudget()
    guard = FieldSDKRequestGuard((read_probe,))
    agent = Agent(
        model=BatchModel([batch]),
        tools=[tool(read_probe)],
        hooks=[budget, guard],
        tool_executor=SequentialToolExecutor(),
        callback_handler=None,
        retry_strategy=None,
    )
    result = asyncio.run(agent.invoke_async("Exercise exact argument handling."))
    return seen, budget, result


@pytest.mark.parametrize(
    "inputs",
    [
        {"value": 2, "tags": None},
        {"value": 0, "tags": ["label"]},
    ],
)
def test_exact_typed_arguments_execute_once(inputs):
    seen, budget, result = fixture([("read_probe", inputs)])
    assert seen == [(inputs["value"], inputs["tags"])]
    assert budget.attempts == 1 and result.stop_reason == "end_turn"


@pytest.mark.parametrize(
    "name,inputs",
    [
        ("read_probe", {"value": "2", "tags": None}),
        ("read_probe", {"value": 2.0, "tags": None}),
        ("read_probe", {"value": True, "tags": None}),
        ("read_probe", {"value": 2, "tags": None, "hidden": "DO-NOT-PERSIST"}),
        ("read_probe", {"value": 2}),
        ("read_probe", {"value": 2, "tags": [3]}),
        ("read_probe", {"value": 2, "tags": "label"}),
        ("read_probe", []),
        ("unknown_tool", {}),
        ("invalid name", {}),
    ],
)
def test_invalid_request_cancels_entire_batch_but_counts_each_request(name, inputs):
    seen, budget, result = fixture(
        [
            ("read_probe", {"value": 1, "tags": None}),
            (name, inputs),
        ]
    )
    assert seen == [] and budget.attempts == 2
    assert result.stop_reason == "end_turn"
    last = result.message
    assert "DO-NOT-PERSIST" not in str(last)


def event(*uses):
    return SimpleNamespace(message={"content": [{"toolUse": use} for use in uses]}, cancel=False)


@pytest.mark.parametrize(
    "use",
    [
        None,
        {},
        {"toolUseId": "one"},
        {"toolUseId": None, "name": "read_probe", "input": {}},
        {"toolUseId": "x" * 201, "name": "read_probe", "input": {}},
    ],
)
def test_malformed_identity_stops_with_constant_error(use):
    def read_probe():
        pass

    guard = FieldSDKRequestGuard((read_probe,))
    with pytest.raises(RuntimeError, match="^Invalid field SDK tool request identity\\.$"):
        guard.before_tools(event(use))


def test_duplicate_ids_cannot_address_two_tools_or_reappear_in_later_batch():
    def read_probe():
        pass

    use = {"toolUseId": "one", "name": "read_probe", "input": {}}
    guard = FieldSDKRequestGuard((read_probe,))
    with pytest.raises(RuntimeError):
        guard.before_tools(event(use, use))
    fresh = FieldSDKRequestGuard((read_probe,))
    fresh.before_tools(event(use))
    with pytest.raises(RuntimeError):
        fresh.before_tools(event(use))


def test_unknown_argument_types_are_not_silently_treated_as_any():
    def read_probe(value):
        pass

    with pytest.raises(ValueError):
        FieldSDKRequestGuard((read_probe,))
