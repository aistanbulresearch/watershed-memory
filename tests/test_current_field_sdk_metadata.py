"""SDK metadata is accepted without entering domain tool arguments or receipts."""

import asyncio
from types import SimpleNamespace

import pytest
from strands import Agent, tool
from strands.tools.executors import SequentialToolExecutor
from test_current_field_sdk_budget import BatchModel

from watershed_memory.current.field_sdk_budget import FieldSDKToolBudget
from watershed_memory.current.field_sdk_requests import FieldSDKRequestGuard


class SignatureModel(BatchModel):
    async def stream(self, *args, **kwargs):
        async for chunk in super().stream(*args, **kwargs):
            start = chunk.get("contentBlockStart", {}).get("start", {})
            if "toolUse" in start:
                start["toolUse"]["reasoningSignature"] = "OPAQUE-SIGNATURE-FIXTURE"
            yield chunk


def test_actual_sdk_accepts_optional_reasoning_signature_without_passing_it_to_tool():
    seen = []

    def read_probe(value: int):
        """Read an exact integer; provider metadata is not a domain argument."""
        seen.append(value)
        return {"value": value}

    budget = FieldSDKToolBudget()
    agent = Agent(
        model=SignatureModel([[("read_probe", {"value": 2})]]),
        tools=[tool(read_probe)],
        hooks=[budget, FieldSDKRequestGuard((read_probe,))],
        tool_executor=SequentialToolExecutor(),
        callback_handler=None,
        retry_strategy=None,
    )
    result = asyncio.run(agent.invoke_async("Run the metadata fixture."))
    assert result.stop_reason == "end_turn" and seen == [2]
    assert budget.attempts == 1


@pytest.mark.parametrize("signature", ["", "opaque-string"])
def test_assembled_signature_is_optional_sdk_metadata(signature):
    def read_probe():
        pass

    event = SimpleNamespace(
        message={
            "content": [
                {
                    "toolUse": {
                        "toolUseId": "one",
                        "name": "read_probe",
                        "input": {},
                        "reasoningSignature": signature,
                    }
                }
            ]
        },
        cancel=False,
    )
    FieldSDKRequestGuard((read_probe,)).before_tools(event)
    assert event.cancel is False


@pytest.mark.parametrize("signature", [None, 2, "x" * 65537], ids=["null", "integer", "oversized"])
def test_invalid_or_oversized_signature_is_rejected_with_constant_error(signature):
    def read_probe():
        pass

    event = SimpleNamespace(
        message={
            "content": [
                {
                    "toolUse": {
                        "toolUseId": "one",
                        "name": "read_probe",
                        "input": {},
                        "reasoningSignature": signature,
                    }
                }
            ]
        },
        cancel=False,
    )
    with pytest.raises(RuntimeError, match="^Invalid field SDK tool request identity\\.$"):
        FieldSDKRequestGuard((read_probe,)).before_tools(event)
