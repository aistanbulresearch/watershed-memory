"""Phase-bound model view of current source and field tools."""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.hooks import BeforeToolsEvent, HookProvider, HookRegistry
from strands.models.model import Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from .field_tools import CurrentToolsV3

_KINDS = ("OBSERVATION_REVIEW", "COVERAGE_REVIEW")
_EVIDENCE = (
    "PHOTO_REFERENCE",
    "SAMPLE_RECORD_REFERENCE",
    "INSPECTION_RECORD_REFERENCE",
    "MAINTENANCE_RECORD_REFERENCE",
)
T = TypeVar("T", bound=BaseModel)
_FIELD_TOOLS = {
    "get_field_context",
    "inspect_field_work",
    "list_approved_field_locations",
    "stage_field_decision",
}


def _inputs(receipts: tuple[Any, ...], name: str) -> list[dict[str, Any]]:
    result = []
    for receipt in receipts:
        if receipt.name != name:
            continue
        value = json.loads(receipt.input_json)
        if type(value) is not dict:
            raise RuntimeError("invalid successful tool receipt")
        result.append(value)
    return result


def _names(receipts: tuple[Any, ...]) -> set[str]:
    return {receipt.name for receipt in receipts}


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _nullable_enum(schema: dict[str, Any], values: tuple[Any, ...], kind: str) -> dict[str, Any]:
    description = schema.get("description")
    choices = [value for value in values if value is not None]
    branches: list[dict[str, Any]] = []
    if choices:
        branches.append({"type": kind, "enum": choices})
    if None in values:
        branches.append({"type": "null"})
    value: dict[str, Any]
    if len(branches) == 1:
        value = branches[0]
    else:
        value = {"anyOf": branches}
    if isinstance(description, str):
        value["description"] = description
    return value


class FieldModelProtocol(Model):
    """Delegate a model while exposing only tools valid for successful protocol state."""

    def __init__(self, model: Model, tools: CurrentToolsV3):
        if not isinstance(model, Model) or type(tools) is not CurrentToolsV3:
            raise ValueError("field model protocol requires exact model and tools")
        self.model = model
        self.tools = tools

    @property
    def stateful(self) -> bool:
        return self.model.stateful

    def get_config(self) -> Any:
        return self.model.get_config()

    def update_config(self, **model_config: Any) -> None:
        self.model.update_config(**model_config)

    async def structured_output(
        self,
        output_model: type[T],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        async for event in self.model.structured_output(
            output_model, prompt, system_prompt, **kwargs
        ):
            yield event


    async def count_tokens(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
    ) -> int:
        return await self.model.count_tokens(
            messages,
            self.tool_specs(tool_specs),
            system_prompt,
            system_prompt_content,
        )

    def _source_phase(
        self,
    ) -> tuple[set[str], dict[str, dict[str, tuple[Any, ...]]]] | None:
        receipts = self.tools.source.trace
        names = _names(receipts)
        context = self.tools.context.base
        if "get_case_context" not in names:
            return {"get_case_context"}, {}
        if "inspect_current_series" not in names:
            return {"inspect_current_series"}, {}
        if context.source_health is not None and "inspect_source_health" not in names:
            return {"inspect_source_health"}, {}

        checked = {
            item["kind"]
            for item in _inputs(receipts, "find_relevant_reviews")
            if item.get("kind") in _KINDS
        }
        active = _unique([item.kind for item in context.reviews])
        outstanding_active = tuple(kind for kind in active if kind not in checked)
        compared = {
            item["event_id"]
            for item in _inputs(receipts, "compare_prior_event")
            if type(item.get("event_id")) is str
        }
        prior = tuple(item.event_id for item in context.prior if item.event_id not in compared)
        alternate = {
            item["parameter_code"]
            for item in _inputs(receipts, "inspect_alternate_sources")
            if type(item.get("parameter_code")) is str
        }
        missing = tuple(
            item for item in context.current.missing_parameters if item not in alternate
        )
        enrichment = set()
        restrictions: dict[str, dict[str, tuple[Any, ...]]] = {}
        if prior:
            enrichment.add("compare_prior_event")
            restrictions["compare_prior_event"] = {"event_id": prior}
        if missing:
            enrichment.add("inspect_alternate_sources")
            restrictions["inspect_alternate_sources"] = {"parameter_code": missing}
        if outstanding_active:
            enrichment.add("find_relevant_reviews")
            restrictions["find_relevant_reviews"] = {"kind": outstanding_active}
            return enrichment, restrictions

        staged = _inputs(receipts, "stage_assessment")
        if not staged:
            unchecked = tuple(kind for kind in _KINDS if kind not in checked)
            allowed = {"stage_assessment", *enrichment}
            restrictions["stage_assessment"] = {
                "disposition": ("NO_FOLLOW_UP", "PROPOSE_REVIEW", "CONTINUE_EXISTING_REVIEW"),
                "kind": (None, *tuple(sorted(checked))),
                "event_id": (context.current.event_id,),
                "target_task_id": (None, *[item.task_id for item in context.reviews]),
            }
            if unchecked:
                allowed.add("find_relevant_reviews")
                restrictions["find_relevant_reviews"] = {"kind": unchecked}
            return allowed, restrictions
        if any(item.get("disposition") == "NO_FOLLOW_UP" for item in staged):
            return {"get_field_context"}, {}
        staged_kinds = {item.get("kind") for item in staged}
        unstaged = tuple(kind for kind in _KINDS if kind not in staged_kinds)
        checked_unstaged = tuple(kind for kind in unstaged if kind in checked)
        unchecked = tuple(kind for kind in unstaged if kind not in checked)
        allowed = {"get_field_context"}
        restrictions = {}
        if checked_unstaged:
            allowed.add("stage_assessment")
            restrictions["stage_assessment"] = {
                "disposition": ("PROPOSE_REVIEW", "CONTINUE_EXISTING_REVIEW"),
                "kind": checked_unstaged,
                "event_id": (context.current.event_id,),
                "target_task_id": (None, *[item.task_id for item in context.reviews]),
            }
        if unchecked:
            allowed.add("find_relevant_reviews")
            restrictions["find_relevant_reviews"] = {"kind": unchecked}
        return allowed, restrictions

    def _required_plan_ids(self) -> tuple[str, ...]:
        field = self.tools.context.field_work
        snapshots = [*field.current_plans]
        snapshots.extend(
            item for item in field.stranded_plans if item.parent_binding == "SUPERSEDED"
        )
        terminal = next(
            (item for item in field.stranded_plans if item.parent_binding == "TERMINAL"), None
        )
        if terminal is not None:
            snapshots.append(terminal)
        if field.latest_results:
            snapshots.append(field.latest_results[0])
        active_tasks = _unique(
            [item.task_id for item in self.tools.context.base.reviews]
        )
        blocked_tasks = {
            item.plan.task_id for item in (*field.current_plans, *field.stranded_plans)
        }
        latest = {}
        for item in field.latest_results:
            latest.setdefault(item.plan.task_id, item)
        snapshots.extend(
            latest[task_id]
            for task_id in active_tasks
            if task_id not in blocked_tasks
            and task_id in latest
            and latest[task_id].result is not None
            and latest[task_id].result.report.outcome in {"PARTIAL", "NOT_DONE"}
        )
        return _unique([item.plan.plan_id for item in snapshots])

    def _proposal_activities(self) -> tuple[str, ...]:
        context = self.tools.context
        field = context.field_work
        approved = {
            activity for site in field.approved_locations for activity in site.activities
        }
        active_tasks = {item.task_id for item in context.base.reviews}
        blocked_tasks = {
            item.plan.task_id for item in (*field.current_plans, *field.stranded_plans)
        }
        latest = {}
        for item in field.latest_results:
            latest.setdefault(item.plan.task_id, item)
        eligible = any(
            task_id not in blocked_tasks
            and (
                task_id not in latest
                or (
                    latest[task_id].result is not None
                    and latest[task_id].result.report.outcome in {"PARTIAL", "NOT_DONE"}
                )
            )
            for task_id in active_tasks
        )
        return tuple(sorted(approved)) if eligible else ()

    def _field_phase(self) -> tuple[set[str], dict[str, dict[str, tuple[Any, ...]]]]:
        receipts = self.tools.field_trace
        names = _names(receipts)
        if "stage_field_decision" in names:
            return set(), {}
        if "get_field_context" not in names:
            return {"get_field_context"}, {}
        inspected = {
            item["plan_id"]
            for item in _inputs(receipts, "inspect_field_work")
            if type(item.get("plan_id")) is str
        }
        outstanding = tuple(
            plan_id for plan_id in self._required_plan_ids() if plan_id not in inspected
        )
        if outstanding:
            return {"inspect_field_work"}, {"inspect_field_work": {"plan_id": outstanding}}

        allowed = {"stage_field_decision"}
        restrictions: dict[str, dict[str, tuple[Any, ...]]] = {}
        room_for_optional_and_stage = len(receipts) < 7 and self.tools.attempts < 15
        optional = _unique(
            [
                item.plan.plan_id
                for item in self.tools.context.field_work.latest_results
                if item.plan.plan_id not in inspected
                and item.result is not None
                and item.result.report.outcome == "COMPLETE"
                and item.result.verification_level in {"REPORTED", "EVIDENCE_ATTACHED"}
            ]
        )
        if optional and room_for_optional_and_stage:
            allowed.add("inspect_field_work")
            restrictions["inspect_field_work"] = {"plan_id": optional}

        activities = self._proposal_activities()
        looked_up = {
            item["activity"]
            for item in _inputs(receipts, "list_approved_field_locations")
            if type(item.get("activity")) is str
        }
        if (
            activities
            and not any(activity in looked_up for activity in activities)
            and room_for_optional_and_stage
        ):
            allowed.add("list_approved_field_locations")
            restrictions["list_approved_field_locations"] = {"activity": activities}
        return allowed, restrictions

    def _phase(self) -> tuple[set[str], dict[str, dict[str, tuple[Any, ...]]]]:
        if not _inputs(self.tools.field_trace, "get_field_context"):
            source = self._source_phase()
            if source is not None:
                return source
        return self._field_phase()

    def _restrict(self, spec: ToolSpec, values: dict[str, tuple[Any, ...]]) -> None:
        schema = spec.get("inputSchema", {}).get("json", {})
        properties = schema.get("properties", {})
        for name, choices in values.items():
            current = properties.get(name)
            if isinstance(current, dict) and choices:
                nonnull = tuple(item for item in choices if item is not None)
                kind = (
                    "integer"
                    if nonnull and all(type(item) is int for item in nonnull)
                    else "string"
                )
                properties[name] = _nullable_enum(current, choices, kind)

    def _stage_restrictions(self, specs: list[ToolSpec]) -> None:
        context = self.tools.context
        inspected = {
            item["plan_id"]
            for item in _inputs(self.tools.field_trace, "inspect_field_work")
            if type(item.get("plan_id")) is str
        }
        snapshots = [
            item
            for collection in (
                context.field_work.current_plans,
                context.field_work.stranded_plans,
                context.field_work.latest_results,
            )
            for item in collection
            if item.plan.plan_id in inspected
        ]
        looked_up = {
            item["activity"]
            for item in _inputs(self.tools.field_trace, "list_approved_field_locations")
            if type(item.get("activity")) is str
        }
        sites = [
            item
            for item in context.field_work.approved_locations
            if any(activity in looked_up for activity in item.activities)
        ]
        reviews = list(context.base.reviews)
        dispositions = ["NO_NEW_FIELD_PLAN"]
        if any(
            item.result is not None
            and item.result.report.outcome == "COMPLETE"
            and item.result.verification_level in {"REPORTED", "EVIDENCE_ATTACHED"}
            for item in snapshots
        ):
            dispositions.append("AWAIT_VERIFICATION")
        if looked_up and self._proposal_activities():
            dispositions.append("PROPOSE_FIELD_PLAN")
        verification_levels = _unique(
            [
                item.result.verification_level
                for item in snapshots
                if item.result is not None
            ]
        )
        values = {
            "disposition": tuple(dispositions),
            "basis_plan_id": (None, *[item.plan.plan_id for item in snapshots]),
            "basis_report_id": (
                None,
                *[item.result.report.report_id for item in snapshots if item.result is not None],
            ),
            "basis_report_revision": (
                None,
                *[item.result.report.revision for item in snapshots if item.result is not None],
            ),
            "basis_verification_level": (None, *verification_levels),
            "task_id": (None, *[item.task_id for item in reviews]),
            "review_revision": (None, *[item.revision for item in reviews]),
            "location_id": (None, *[item.location_id for item in sites]),
            "location_revision": (None, *[item.revision for item in sites]),
            "activity": (None, *sorted(looked_up)),
        }
        for spec in specs:
            if spec.get("name") != "stage_field_decision":
                continue
            self._restrict(spec, values)
            schema = spec.get("inputSchema", {}).get("json", {})
            required = schema.get("properties", {}).get("required_evidence")
            if isinstance(required, dict):
                description = required.get("description")
                array = {"type": "array", "items": {"type": "string", "enum": list(_EVIDENCE)}}
                properties = {"anyOf": [array, {"type": "null"}]}
                if isinstance(description, str):
                    properties["description"] = description
                schema["properties"]["required_evidence"] = properties

    def tool_specs(self, tool_specs: list[ToolSpec] | None) -> list[ToolSpec] | None:
        if tool_specs is None:
            return None
        copied = copy.deepcopy(tool_specs)
        allowed, restrictions = self._phase()
        selected = [spec for spec in copied if spec.get("name") in allowed]
        for spec in selected:
            self._restrict(spec, restrictions.get(spec.get("name", ""), {}))
        self._stage_restrictions(selected)
        return selected

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        cancel_signal: threading.Event | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        async for event in self.model.stream(
            messages,
            self.tool_specs(tool_specs),
            system_prompt,
            tool_choice=tool_choice,
            system_prompt_content=system_prompt_content,
            invocation_state=invocation_state,
            cancel_signal=cancel_signal,
            **kwargs,
        ):
            yield event


class FieldProtocolRequestGuard(HookProvider):
    """Atomically reserve bounded field receipts for an assembled SDK batch."""

    def __init__(self, protocol: FieldModelProtocol):
        if type(protocol) is not FieldModelProtocol:
            raise ValueError("field protocol guard requires an exact protocol")
        self.protocol = protocol

    def register_hooks(self, registry: HookRegistry, **_kwargs: Any) -> None:
        registry.add_callback(BeforeToolsEvent, self.before_tools)

    def before_tools(self, event: BeforeToolsEvent) -> None:
        uses = [
            block.get("toolUse")
            for block in event.message.get("content", [])
            if type(block) is dict and "toolUse" in block
        ]
        tools = self.protocol.tools
        if tools._decision is not None and uses:
            event.cancel = "Invalid field SDK phase request."
            return
        field_uses = [
            use for use in uses if type(use) is dict and use.get("name") in _FIELD_TOOLS
        ]
        if not field_uses:
            return
        prior = len(tools.field_trace)
        stage_indexes = [
            index
            for index, use in enumerate(uses)
            if type(use) is dict and use.get("name") == "stage_field_decision"
        ]
        if stage_indexes:
            if (
                len(stage_indexes) != 1
                or stage_indexes[0] != len(uses) - 1
                or prior + len(field_uses) > 8
            ):
                event.cancel = "Invalid field SDK phase request."
            return
        if prior + len(field_uses) > 7:
            event.cancel = "Invalid field SDK phase request."
