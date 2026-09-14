"""Bounded, immutable recipes for replaying one trusted current context."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from . import case_records as rows
from .assessment_types import _canonical_json
from .case_types import WorkflowConflict, _identifier, _timestamp
from .context import CurrentContext, _load_context
from .fact_validation import event_id, timestamp
from .tools import _json

_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class ContextReference:
    case_id: str
    event_id: str
    case_revision: int
    evaluated_at: datetime
    prior_event_ids: tuple[str, ...]
    review_refs: tuple[tuple[str, int, tuple[str, ...]], ...]
    source_version_ids: tuple[int | None, ...] | None
    context_digest: str

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        event_id(self.event_id)
        if (
            type(self.case_revision) is not int
            or isinstance(self.case_revision, bool)
            or not 0 <= self.case_revision < 2**63
        ):
            raise ValueError("invalid case revision")
        evaluated = _timestamp(self.evaluated_at, "evaluated_at", required=True)
        if type(self.prior_event_ids) is not tuple or len(self.prior_event_ids) > 3:
            raise ValueError("invalid prior events")
        for identity in self.prior_event_ids:
            event_id(identity)
        if (
            len(set(self.prior_event_ids)) != len(self.prior_event_ids)
            or self.event_id in self.prior_event_ids
        ):
            raise ValueError("invalid prior events")
        if type(self.review_refs) is not tuple or len(self.review_refs) > 3:
            raise ValueError("invalid review references")
        seen_tasks = set()
        allowed = {self.event_id, *self.prior_event_ids}
        for item in self.review_refs:
            if type(item) is not tuple or len(item) != 3:
                raise ValueError("invalid review reference")
            _identifier(item[0], "task_id")
            if type(item[1]) is not int or isinstance(item[1], bool) or not 0 < item[1] < 2**63:
                raise ValueError("invalid review revision")
            evidence = item[2]
            if type(evidence) is not tuple or len(evidence) > 3:
                raise ValueError("invalid review evidence")
            for identity in evidence:
                if type(identity) is not str or identity not in allowed:
                    raise ValueError("review evidence is outside reference")
            if len(set(evidence)) != len(evidence):
                raise ValueError("duplicate review evidence")
            if item[0] in seen_tasks:
                raise ValueError("duplicate review reference")
            seen_tasks.add(item[0])
        if self.source_version_ids is not None:
            if (
                type(self.source_version_ids) is not tuple
                or not 1 <= len(self.source_version_ids) <= 16
            ):
                raise ValueError("invalid source version references")
            present = [value for value in self.source_version_ids if value is not None]
            if any(
                type(value) is not int or isinstance(value, bool) or not 0 < value < 2**63
                for value in present
            ):
                raise ValueError("invalid source version references")
            if len(set(present)) != len(present):
                raise ValueError("duplicate source version reference")
        if type(self.context_digest) is not str or not _HASH.fullmatch(self.context_digest):
            raise ValueError("invalid context digest")
        object.__setattr__(self, "evaluated_at", evaluated)


def capture_context(context: CurrentContext) -> ContextReference:
    if type(context) is not CurrentContext:
        raise ValueError("expected a current context")
    refs = tuple(
        (review.task_id, review.revision, tuple(evidence))
        for review, (_, evidence) in zip(context.reviews, context.review_evidence, strict=True)
    )
    pins = None if context.source_health is None else context.source_health.version_ids
    return ContextReference(
        context.case_id,
        context.current.event_id,
        context.case_revision,
        context.evaluated_at,
        tuple(item.event_id for item in context.prior),
        refs,
        pins,
        rows.digest(_json(context)),
    )


def encode_reference(reference: ContextReference) -> str:
    if type(reference) is not ContextReference:
        raise ValueError("expected a context reference")
    result = _json(reference)
    _canonical_json(result, "context reference", 8192)
    return result


def decode_reference(raw: str) -> ContextReference:
    _canonical_json(raw, "context reference", 8192)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("invalid context reference") from error
    fields = {
        "case_id",
        "event_id",
        "case_revision",
        "evaluated_at",
        "prior_event_ids",
        "review_refs",
        "source_version_ids",
        "context_digest",
    }
    if type(data) is not dict or set(data) != fields:
        raise ValueError("invalid context reference fields")
    if type(data["prior_event_ids"]) is not list or type(data["review_refs"]) is not list:
        raise ValueError("context reference arrays are required")
    if data["source_version_ids"] is not None and type(data["source_version_ids"]) is not list:
        raise ValueError("source reference array is required")
    if any(
        type(item) is not list or len(item) != 3 or type(item[2]) is not list
        for item in data["review_refs"]
    ):
        raise ValueError("invalid review reference array shape")
    try:
        prior = tuple(data["prior_event_ids"])
        review = tuple((item[0], item[1], tuple(item[2])) for item in data["review_refs"])
        source = None if data["source_version_ids"] is None else tuple(data["source_version_ids"])
        return ContextReference(
            data["case_id"],
            data["event_id"],
            data["case_revision"],
            timestamp(data["evaluated_at"]),
            prior,
            review,
            source,
            data["context_digest"],
        )
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError("invalid context reference") from error


def restore_context(db: sqlite3.Connection, reference: ContextReference) -> CurrentContext:
    if not db.in_transaction:
        raise ValueError("context restoration requires an active transaction")
    if type(reference) is not ContextReference:
        raise ValueError("expected a context reference")
    restored = _load_context(
        db,
        reference.case_id,
        reference.event_id,
        evaluated_at=reference.evaluated_at,
        prior_event_ids=reference.prior_event_ids,
        review_refs=reference.review_refs,
        reserved_revision=reference.case_revision,
        include_source_health=reference.source_version_ids is not None,
        source_version_ids=reference.source_version_ids,
    )
    if rows.digest(_json(restored)) != reference.context_digest:
        raise WorkflowConflict("saved context reference differs from trusted context")
    return restored
