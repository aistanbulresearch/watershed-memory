"""Handwritten current-field model fixture for the installed Strands SDK.

This is deliberately a rules fixture.  It reads the tool results in the SDK
conversation and never receives a context object or an expected answer from a
caller.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from strands.models.model import Model


def _tool_results(messages: list[dict[str, Any]]) -> dict[str, Any]:
    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    blocks = [
        block
        for message in messages
        if isinstance(message, dict)
        for block in message.get("content", [])
        if isinstance(block, dict)
    ]
    for block in blocks:
        use = block.get("toolUse")
        if isinstance(use, dict) and isinstance(use.get("toolUseId"), str):
            names[use["toolUseId"]] = use.get("name", "")
    for block in blocks:
        result = block.get("toolResult")
        if not isinstance(result, dict):
            continue
        if result.get("status") != "success":
            error = result.get("error")
            raise AssertionError(
                f"field fixture received SDK tool error status={result.get('status')!r}; "
                f"error={error!r}"
            )
        use_id = result.get("toolUseId")
        if use_id not in names:
            continue
        content = result.get("content", [])
        value = content[0] if isinstance(content, list) and content else {}
        if isinstance(value, dict) and "json" in value:
            parsed = value["json"]
        elif isinstance(value, dict) and isinstance(value.get("text"), str):
            parsed = json.loads(value["text"])
        else:
            parsed = value
        name = names[use_id]
        if name == "inspect_field_work":
            plan = _first(parsed, "plan", default={})
            plan_id = _first(parsed, "plan_id", "id", default=_first(plan, "plan_id", default=use_id))
            values[f"inspect_field_work:{plan_id}"] = parsed
        values[name] = parsed
    return values


def _first(value: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(value, dict):
        return default
    for key in keys:
        if key in value:
            return value[key]
    return default


def _items(value: Any, key: str) -> list[dict[str, Any]]:
    rows = value.get(key, []) if isinstance(value, dict) else []
    return [row for row in rows if isinstance(row, dict)]


class FieldResultScriptedModel(Model):
    """Rules-only field fixture that drives six real SDK tool responses."""

    def __init__(
        self,
        before_response: Callable[[int], Any] | None = None,
    ) -> None:
        self.before_response = before_response
        self.response_count = 0
        self.tool_specs: list[list[dict[str, Any]]] = []
        self.tool_results: dict[str, Any] = {}
        self.captured_tool_results: dict[str, Any] = {}
        self.field_work_results: dict[str, Any] = {}
        self.responses: list[tuple[tuple[str, dict[str, Any]], ...]] = []

    @property
    def captured_tool_specs(self) -> list[list[dict[str, Any]]]:
        return self.tool_specs

    def get_config(self) -> dict[str, str]:
        return {"model_id": "scripted-current-field-fixture"}

    def update_config(self, **_: Any) -> None:
        return None

    async def structured_output(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError("the field fixture uses tool streaming")

    def _active_review(self, values: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        case = values.get("get_case_context", {})
        kinds = _first(case, "active_review_kinds", default=[])
        if not isinstance(kinds, list) or not kinds:
            raise AssertionError("field fixture requires an active review kind")
        kind = kinds[0]
        found = values.get("find_relevant_reviews", {})
        reviews = _items(found, "reviews")
        review = next((item for item in reviews if item.get("kind") == kind), None)
        if review is None:
            raise AssertionError("active review was not returned by the tool")
        return kind, review

    def _assessment_call(self, values: dict[str, Any]) -> dict[str, Any]:
        kind, review = self._active_review(values)
        case = values["get_case_context"]
        event_id = case["current_event_id"]
        return {
            "disposition": "CONTINUE_EXISTING_REVIEW",
            "kind": kind,
            "event_id": event_id,
            "target_task_id": review["task_id"],
            "title": None,
            "reason": "Continue the saved review while the current field evidence is checked.",
            "next_check_at": None,
            "reference_ids": [],
        }

    def _field_calls(
        self, values: dict[str, Any], *, final: bool
    ) -> list[tuple[str, dict[str, Any]]]:
        field_context = values.get("get_field_context", {})
        rows: list[dict[str, Any]] = []
        rows.extend(_items(field_context, "current_plans"))
        rows.extend(
            row
            for row in _items(field_context, "stranded_plans")
            if row.get("parent_binding") == "SUPERSEDED"
        )
        terminal = next(
            (
                row
                for row in _items(field_context, "stranded_plans")
                if row.get("parent_binding") == "TERMINAL"
            ),
            None,
        )
        if terminal is not None:
            rows.append(terminal)

        latest = _items(field_context, "latest_results")
        _, active_review = self._active_review(values)
        selected = next(
            (row for row in latest if row.get("task_id") == active_review["task_id"]),
            None,
        )
        if selected is None and latest:
            selected = latest[0]
        if selected is not None:
            rows.append(selected)
        if latest and selected is not latest[0]:
            rows.append(latest[0])

        seen: set[str] = set()
        calls: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            plan_id = _first(row, "plan_id", "id")
            if (
                isinstance(plan_id, str)
                and plan_id not in seen
                and f"inspect_field_work:{plan_id}" not in values
            ):
                seen.add(plan_id)
                calls.append(("inspect_field_work", {"plan_id": plan_id}))
        if selected is not None and final:
            plan_id = _first(selected, "plan_id", "basis_plan_id")
            inspected = values.get(f"inspect_field_work:{plan_id}")
            if inspected is not None:
                selected = inspected

        index = next(
            (row for row in latest if row.get("task_id") == active_review["task_id"]),
            None,
        )
        indexed_outcome = _first(index, "outcome", default=None)
        indexed_level = _first(index, "verification_level", default=None)
        inspected_result = _first(selected, "result", default={}) if final else {}
        inspected_report = _first(inspected_result, "report", default={})
        outcome = _first(inspected_report, "outcome", default=indexed_outcome)
        level = _first(selected, "verification_level", default=indexed_level)
        if final and index is not None and (outcome, level) != (indexed_outcome, indexed_level):
            raise AssertionError("field result inspection disagrees with its bounded index")
        if outcome == "COMPLETE" and level == "VERIFIED":
            disposition = "NO_NEW_FIELD_PLAN"
        elif outcome == "COMPLETE" and level == "EVIDENCE_ATTACHED":
            disposition = "AWAIT_VERIFICATION"
        elif outcome == "PARTIAL" and level == "VERIFIED":
            disposition = "PROPOSE_FIELD_PLAN"
        elif selected is None:
            disposition = "NO_NEW_FIELD_PLAN"
        else:
            raise AssertionError("unsupported or incomplete field result fixture")

        location: dict[str, Any] | None = None
        if disposition == "PROPOSE_FIELD_PLAN" and not final:
            calls.append(("list_approved_field_locations", {"activity": "VISUAL_INSPECTION"}))
        if disposition == "PROPOSE_FIELD_PLAN" and final and "list_approved_field_locations" not in {
            name for response in self.responses for name, _ in response
        }:
            raise AssertionError("approved locations must be read before staging")
        if not final:
            return calls

        if disposition == "PROPOSE_FIELD_PLAN":
            locations = _items(values.get("list_approved_field_locations", {}), "locations")
            location = locations[0] if locations else None
            if location is None:
                raise AssertionError("proposal fixture requires an approved location")

        base = selected or {}
        plan = _first(base, "plan", default={})
        result = _first(base, "result", default={})
        report = _first(result, "report", default={})
        case = values["get_case_context"]
        args: dict[str, Any] = {
            "disposition": disposition,
            "basis_plan_id": _first(
                base, "plan_id", default=_first(plan, "plan_id", default=None)
            ),
            "basis_report_id": _first(
                report, "report_id", default=_first(base, "report_id", default=None)
            ),
            "basis_report_revision": _first(
                report, "revision", default=_first(base, "report_revision", default=None)
            ),
            "basis_verification_level": level,
            "reason": "Use the inspected field result and approved work location to preserve the operator plan.",
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
        if disposition == "PROPOSE_FIELD_PLAN":
            kind, review = self._active_review(values)
            del kind
            evaluated = case["evaluated_at"]
            # The tool accepts RFC3339 strings; retain the exact evaluated value
            # and use a bounded, explicit one-hour proposal window.
            from datetime import datetime, timedelta

            end = (datetime.fromisoformat(evaluated.replace("Z", "+00:00")) + timedelta(hours=1)).isoformat()
            args.update(
                task_id=review["task_id"],
                review_revision=review["revision"],
                location_id=location["location_id"],
                location_revision=location["revision"],
                activity="VISUAL_INSPECTION",
                purpose="Record a visual inspection for the watershed field review.",
                assignee_role="SOURCE_WATER_OPERATOR",
                window_start=evaluated,
                window_end=end,
                required_evidence=["INSPECTION_RECORD_REFERENCE"],
            )
        calls.append(("stage_field_decision", args))
        return calls

    def _next_calls(self, values: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if self.response_count == 0:
            return [("get_case_context", {})]
        if self.response_count == 1:
            return [
                ("inspect_current_series", {}),
                ("inspect_source_health", {}),
                (
                    "find_relevant_reviews",
                    {"kind": _first(values["get_case_context"], "active_review_kinds")[0]},
                ),
            ]
        if self.response_count == 2:
            return [("stage_assessment", self._assessment_call(values)), ("get_field_context", {})]
        if self.response_count == 3:
            return self._field_calls(values, final=False)
        if self.response_count == 4:
            return self._field_calls(values, final=True)
        return [("end_turn", {})]

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        **_: Any,
    ):
        del system_prompt
        values = _tool_results(messages)
        self.tool_results = dict(values)
        self.captured_tool_results = dict(values)
        self.field_work_results = {
            key.partition(":")[2]: value
            for key, value in values.items()
            if key.startswith("inspect_field_work:")
        }
        if tool_specs is not None:
            self.tool_specs.append(tool_specs)
        if self.before_response is not None:
            callback = self.before_response
            try:
                accepts_number = len(inspect.signature(callback).parameters) > 0
            except (TypeError, ValueError):
                accepts_number = True
            result = callback(self.response_count + 1) if accepts_number else callback()
            if isinstance(result, Awaitable):
                await result
        calls = self._next_calls(values)
        self.response_count += 1
        self.responses.append(tuple(calls))

        yield {"messageStart": {"role": "assistant"}}
        if calls and calls[0][0] != "end_turn":
            for index, (name, inputs) in enumerate(calls):
                tool_id = f"field-tool-{self.response_count}-{index}"
                yield {
                    "contentBlockStart": {
                        "contentBlockIndex": index,
                        "start": {"toolUse": {"toolUseId": tool_id, "name": name}},
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "contentBlockIndex": index,
                        "delta": {"toolUse": {"input": json.dumps(inputs)}},
                    }
                }
                yield {"contentBlockStop": {"contentBlockIndex": index}}
            stop_reason = "tool_use"
        else:
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Field assessment complete."}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            stop_reason = "end_turn"
        yield {"messageStop": {"stopReason": stop_reason}}
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": 1,
                    "outputTokens": 1,
                    "totalTokens": 2,
                },
                "metrics": {"latencyMs": 1},
            }
        }
