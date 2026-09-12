"""Immutable, bounded records staged by the current assessment tools."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from .case_types import _EVENT, _identifier, _text, _timestamp

_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TOOLS = frozenset(
    {
        "get_case_context",
        "inspect_current_series",
        "compare_prior_event",
        "find_relevant_reviews",
        "inspect_alternate_sources",
        "stage_assessment",
    }
)
_KINDS = frozenset({"OBSERVATION_REVIEW", "COVERAGE_REVIEW"})
_DISPOSITIONS = frozenset({"NO_FOLLOW_UP", "PROPOSE_REVIEW", "CONTINUE_EXISTING_REVIEW"})


def _duplicate_key(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON value: {value}")


def _canonical_json(value: str, field: str, limit: int) -> None:
    if type(value) is not str:
        raise ValueError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"invalid {field} UTF-8") from error
    if len(encoded) > limit:
        raise ValueError(f"{field} is too large")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_duplicate_key,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(f"invalid {field} JSON") from error
    if type(parsed) is not dict:
        raise ValueError(f"{field} must encode an object")
    pending = [(parsed, 1)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 4096 or depth > 16:
            raise ValueError(f"{field} exceeds JSON structural bounds")
        if type(item) is dict:
            pending.extend((value, depth + 1) for value in item.values())
        elif type(item) is list:
            pending.extend((value, depth + 1) for value in item)
    try:
        canonical = json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise ValueError(f"invalid {field} JSON") from error
    if canonical != encoded:
        raise ValueError(f"{field} is not canonical JSON")


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    disposition: str
    kind: str | None
    event_id: str
    target_task_id: str | None
    title: str | None
    reason: str
    next_check_at: datetime | None
    reference_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.disposition) is not str or self.disposition not in _DISPOSITIONS:
            raise ValueError("invalid disposition")
        if self.kind is not None and (type(self.kind) is not str or self.kind not in _KINDS):
            raise ValueError("invalid decision kind")
        if type(self.event_id) is not str or not _EVENT.fullmatch(self.event_id):
            raise ValueError("invalid event_id")
        if self.target_task_id is not None:
            _identifier(self.target_task_id, "target_task_id")
        if self.title is not None:
            _text(self.title, "title", 1, 120)
        _text(self.reason, "reason", 8, 700)
        normalized = _timestamp(self.next_check_at, "next_check_at")
        if type(self.reference_ids) is not tuple or len(self.reference_ids) > 3:
            raise ValueError("reference_ids must be a tuple of at most three IDs")
        for reference in self.reference_ids:
            if type(reference) is not str or not _EVENT.fullmatch(reference):
                raise ValueError("invalid reference_id")
            if reference == self.event_id:
                raise ValueError("current event cannot be a reference")
        if len(set(self.reference_ids)) != len(self.reference_ids):
            raise ValueError("reference_ids must be unique")
        if self.disposition == "NO_FOLLOW_UP":
            if any(
                value is not None
                for value in (self.kind, self.target_task_id, self.title, normalized)
            ):
                raise ValueError("NO_FOLLOW_UP cannot carry a plan")
        elif self.disposition == "PROPOSE_REVIEW":
            if self.kind is None or self.title is None or self.target_task_id is not None:
                raise ValueError("PROPOSE_REVIEW requires a new titled kind")
        elif (
            self.kind is None
            or self.target_task_id is None
            or self.title is not None
            or normalized is not None
        ):
            raise ValueError("CONTINUE_EXISTING_REVIEW requires an exact target")
        object.__setattr__(self, "next_check_at", normalized)


@dataclass(frozen=True, slots=True)
class ToolReceipt:
    name: str
    input_json: str
    output_json: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or self.name not in _TOOLS:
            raise ValueError("unknown tool receipt")
        _canonical_json(self.input_json, "input_json", 4096)
        _canonical_json(self.output_json, "output_json", 32768)


@dataclass(frozen=True, slots=True)
class CurrentAssessment:
    case_id: str
    case_revision: int
    policy_digest: str
    event_id: str
    decisions: tuple[ReviewDecision, ...]
    trace: tuple[ToolReceipt, ...]

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        if type(self.case_revision) is not int or not 0 <= self.case_revision < 2**63:
            raise ValueError("invalid case_revision")
        if type(self.policy_digest) is not str or not _DIGEST.fullmatch(self.policy_digest):
            raise ValueError("invalid policy_digest")
        if type(self.event_id) is not str or not _EVENT.fullmatch(self.event_id):
            raise ValueError("invalid event_id")
        if type(self.decisions) is not tuple or not 1 <= len(self.decisions) <= 2:
            raise ValueError("decisions must contain one or two decisions")
        if type(self.trace) is not tuple or not 1 <= len(self.trace) <= 12:
            raise ValueError("trace must contain one to twelve receipts")
        if any(not isinstance(item, ReviewDecision) for item in self.decisions):
            raise ValueError("invalid decision record")
        if any(item.event_id != self.event_id for item in self.decisions):
            raise ValueError("decision event does not match assessment event")
        if any(not isinstance(item, ToolReceipt) for item in self.trace):
            raise ValueError("invalid trace receipt")
        if any(item.disposition == "NO_FOLLOW_UP" for item in self.decisions):
            if len(self.decisions) != 1:
                raise ValueError("NO_FOLLOW_UP is exclusive")
        else:
            kinds = [item.kind for item in self.decisions]
            if len(set(kinds)) != len(kinds):
                raise ValueError("decision kinds must be unique")
