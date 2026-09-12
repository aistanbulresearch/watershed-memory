"""Bounded, side-effect-free tools for staging a current assessment."""

from __future__ import annotations

import inspect
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from typing import Any, Callable

from .assessment_types import CurrentAssessment, ReviewDecision, ToolReceipt
from .case_records import encode
from .context import CurrentContext
from .fact_validation import timestamp
from .facts import compare_intervals


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _json(value: Any) -> str:
    return encode(_plain(value))


def _event(item: Any) -> dict[str, Any]:
    return {
        "event_id": item.event_id,
        "interval_start": item.interval_start.isoformat(),
        "interval_end": item.interval_end.isoformat(),
        "revision": item.revision,
        "correction": item.supersedes_event_id is not None,
    }


def _decision(item: ReviewDecision) -> dict[str, Any]:
    return _plain(asdict(item))


def _tool(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(function)

        @wraps(function)
        def wrapped(self: "CurrentTools", *args: Any, **kwargs: Any) -> Any:
            self._charge()
            before = self._state()
            try:
                bound = signature.bind(self, *args, **kwargs)
                bound.apply_defaults()
                payload = {key: value for key, value in bound.arguments.items() if key != "self"}
                input_json = _json(payload)
                ToolReceipt(name, input_json, "{}")
                result = function(self, *args, **kwargs)
                output_json = _json(result)
                receipt = ToolReceipt(name, input_json, output_json)
                self._trace.append(receipt)
                return json.loads(output_json)
            except (KeyError, TypeError, ValueError, RecursionError, UnicodeError):
                self._restore(before)
                raise ValueError("invalid tool request") from None

        return wrapped

    return decorate


class CurrentTools:
    """Read and stage against one immutable trusted ``CurrentContext``."""

    def __init__(self, context: CurrentContext):
        if not isinstance(context, CurrentContext):
            raise ValueError("invalid current context")
        self.context = context
        self._attempts = 0
        self._trace: list[ToolReceipt] = []
        self._exhausted = False
        self._compared: set[str] = set()
        self._checked: set[str] = set()
        self._staged: list[ReviewDecision] = []

    def _charge(self) -> None:
        if self._exhausted:
            raise RuntimeError("tool attempt budget exhausted")
        if self._attempts >= 12:
            self._exhausted = True
            raise RuntimeError("tool attempt budget exhausted")
        self._attempts += 1

    def _state(self) -> tuple[set[str], set[str], list[ReviewDecision], list[ToolReceipt]]:
        return self._compared.copy(), self._checked.copy(), self._staged.copy(), self._trace.copy()

    def _restore(
        self, state: tuple[set[str], set[str], list[ReviewDecision], list[ToolReceipt]]
    ) -> None:
        self._compared, self._checked, self._staged, self._trace = state

    def _require_read(self) -> None:
        names = {receipt.name for receipt in self._trace}
        if not {"get_case_context", "inspect_current_series"} <= names:
            raise ValueError("required context reads are incomplete")

    @property
    def trace(self) -> tuple[ToolReceipt, ...]:
        return tuple(self._trace)

    @property
    def attempts(self) -> int:
        return self._attempts

    @_tool("get_case_context")
    def get_case_context(self) -> dict[str, Any]:
        """Read the bounded case identity, current event, and prior-event index."""
        return {
            "case_id": self.context.case_id,
            "case_revision": self.context.case_revision,
            "policy_digest": self.context.policy_digest,
            "simulated": self.context.simulated,
            "evaluated_at": self.context.evaluated_at.isoformat(),
            "current_event_id": self.context.current.event_id,
            "source_origin_case_id": self.context.current.case_id,
            "active_review_kinds": [item.kind for item in self.context.reviews],
            "available_prior_events": [_event(item) for item in self.context.prior],
        }

    @_tool("inspect_current_series")
    def inspect_current_series(self) -> dict[str, Any]:
        """Read all serialized facts for the current attributed observation."""
        if not any(receipt.name == "get_case_context" for receipt in self._trace):
            raise ValueError("required context read is incomplete")
        current = self.context.current
        result = _plain(asdict(current))
        result.update(
            {
                "evidence_class": "CURRENT_USGS_OBSERVATION",
                "evaluated_at": self.context.evaluated_at.isoformat(),
            }
        )
        return result

    @_tool("compare_prior_event")
    def compare_prior_event(self, event_id: str) -> dict[str, Any]:
        """Compare one allowlisted prior event with the current observation."""
        self._require_read()
        if type(event_id) is not str:
            raise ValueError("invalid prior event")
        previous = next((item for item in self.context.prior if item.event_id == event_id), None)
        if previous is None:
            raise ValueError("event is not an allowlisted prior")
        comparison = compare_intervals(self.context.current, previous)
        if not comparison.comparable:
            raise ValueError("prior event is not comparable")
        self._compared.add(event_id)
        return {
            "event_id": event_id,
            "prior": {
                **_plain(asdict(previous)),
                "evidence_class": "CURRENT_USGS_OBSERVATION",
                "evaluated_at": self.context.evaluated_at.isoformat(),
            },
            "comparison": _plain(asdict(comparison)),
        }

    @_tool("find_relevant_reviews")
    def find_relevant_reviews(self, kind: str) -> dict[str, Any]:
        """Read the active operator plan for one supported review kind."""
        self._require_read()
        if type(kind) is not str or kind not in {"OBSERVATION_REVIEW", "COVERAGE_REVIEW"}:
            raise ValueError("invalid review kind")
        self._checked.add(kind)
        reviews = []
        links = dict(self.context.review_evidence)
        for item in self.context.reviews:
            if item.kind == kind:
                reviews.append(
                    {
                        "task_id": item.task_id,
                        "kind": item.kind,
                        "status": item.status,
                        "revision": item.revision,
                        "title": item.title,
                        "reason": item.reason,
                        "next_check_at": item.next_check_at.isoformat()
                        if item.next_check_at
                        else None,
                        "evidence_ids": list(links.get(item.task_id, ())),
                    }
                )
        return {"kind": kind, "reviews": reviews}

    @_tool("inspect_alternate_sources")
    def inspect_alternate_sources(self, parameter_code: str) -> dict[str, Any]:
        """Inspect configured compatible sources without fetching measurements."""
        self._require_read()
        registration = self.context.registry.get(self.context.current.source_id)
        alternatives = self.context.registry.alternatives(registration.source_id, parameter_code)
        sources = [
            {"source_id": item.source_id, "station_id": item.station_id, "label": item.label}
            for item in alternatives
        ]
        return {
            "parameter_code": parameter_code,
            "status": "CONFIGURED_NOT_FETCHED" if sources else "NO_COMPATIBLE_SOURCE_CONFIGURED",
            "sources": sources,
        }

    @_tool("stage_assessment")
    def stage_assessment(
        self,
        disposition: str,
        kind: str | None,
        event_id: str,
        target_task_id: str | None,
        title: str | None,
        reason: str,
        next_check_at: str | None,
        reference_ids: list[str],
    ) -> dict[str, Any]:
        """Stage a bounded review decision after required reads and comparisons.

        Args:
            disposition: One of the supported review dispositions.
            kind: Review kind, explicitly supplied or ``None`` for no follow-up.
            event_id: Current event identity.
            target_task_id: Existing task identity for continuation, otherwise ``None``.
            title: New plan title, otherwise ``None``.
            reason: Printable operator-facing rationale.
            next_check_at: Strict RFC3339 due time, or ``None``.
            reference_ids: Prior event IDs already compared by this instance.
        """
        self._require_read()
        if event_id != self.context.current.event_id or type(reference_ids) is not list:
            raise ValueError("assessment identity or references differ")
        if any(item not in self._compared for item in reference_ids):
            raise ValueError("references must have been compared")
        if len(reference_ids) > 3 or len(set(reference_ids)) != len(reference_ids):
            raise ValueError("references are not bounded and unique")
        if kind is not None:
            if kind not in self._checked:
                raise ValueError("review kind must be looked up first")
            existing = next((item for item in self.context.reviews if item.kind == kind), None)
            if disposition == "PROPOSE_REVIEW" and existing is not None:
                raise ValueError("active review must be continued")
            if disposition == "CONTINUE_EXISTING_REVIEW":
                if existing is None or target_task_id != existing.task_id:
                    raise ValueError("target is not the active review")
            if kind == "COVERAGE_REVIEW":
                if (
                    self.context.current.interval_coverage == "SUFFICIENT"
                    and self.context.current.freshness == "FRESH"
                ):
                    raise ValueError("complete fresh data has no coverage gap")
            if kind == "OBSERVATION_REVIEW":
                if not any(item.latest_value is not None for item in self.context.current.series):
                    raise ValueError("observation review requires a numeric value")
        if next_check_at is not None:
            due = timestamp(next_check_at)
            if due <= self.context.evaluated_at or due > self.context.evaluated_at + timedelta(
                days=30
            ):
                raise ValueError("next check is outside the allowed window")
        else:
            due = None
        decision = ReviewDecision(
            disposition, kind, event_id, target_task_id, title, reason, due, tuple(reference_ids)
        )
        if any(item.kind == decision.kind for item in self._staged if item.kind is not None):
            raise ValueError("review kind already staged")
        if self._staged and (
            decision.disposition == "NO_FOLLOW_UP" or self._staged[0].disposition == "NO_FOLLOW_UP"
        ):
            raise ValueError("NO_FOLLOW_UP is exclusive")
        self._staged.append(decision)
        return {"status": "STAGED", "committed": False, "decision": _decision(decision)}

    def finish(self) -> CurrentAssessment:
        if self._exhausted:
            raise RuntimeError("tool attempt budget exhausted")
        self._require_read()
        if not self._staged:
            raise ValueError("no staged assessment")
        if any(item.kind not in self._checked for item in self._staged if item.kind is not None):
            raise ValueError("required review lookup is incomplete")
        return CurrentAssessment(
            self.context.case_id,
            self.context.case_revision,
            self.context.policy_digest,
            self.context.current.event_id,
            tuple(self._staged),
            tuple(self._trace),
        )


def validate_assessment(
    context: CurrentContext, assessment: CurrentAssessment
) -> CurrentAssessment:
    if not isinstance(assessment, CurrentAssessment):
        raise ValueError("invalid assessment")
    item = CurrentTools(context)
    try:
        for receipt in assessment.trace:
            arguments = json.loads(receipt.input_json)
            if (
                receipt.name
                not in {
                    "get_case_context",
                    "inspect_current_series",
                    "compare_prior_event",
                    "find_relevant_reviews",
                    "inspect_alternate_sources",
                    "stage_assessment",
                }
                or type(arguments) is not dict
            ):
                raise ValueError("invalid trace receipt")
            result = getattr(item, receipt.name)(**arguments)
            if _json(result) != receipt.output_json:
                raise ValueError("trace output differs")
        if item.finish() != assessment:
            raise ValueError("assessment differs from replay")
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise ValueError("assessment trace is invalid") from error
    return assessment
