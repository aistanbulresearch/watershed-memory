"""Frozen, bounded records for the current-case work ledger."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .facts import CoveragePolicy


class WorkflowConflict(ValueError):
    """A stale revision, duplicate operation, or invalid lifecycle transition."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)
_EVENT = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _identifier(value: str, name: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _text(value: str, name: str, minimum: int, maximum: int) -> str:
    if type(value) is not str or not minimum <= len(value) <= maximum or not value.isprintable():
        raise ValueError(f"invalid {name}")
    return value


def _timestamp(value: datetime | None, name: str, *, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} has an invalid timestamp") from error


@dataclass(frozen=True, slots=True)
class CaseConfig:
    case_id: str
    monitor_id: str
    coverage_policy: CoveragePolicy
    simulated: bool

    def __post_init__(self):
        _identifier(self.case_id, "case_id")
        _identifier(self.monitor_id, "monitor_id")
        if not isinstance(self.coverage_policy, CoveragePolicy):
            raise ValueError("invalid coverage_policy")
        if self.case_id == "GALLINAS-HPCC-2022" or type(self.simulated) is not bool:
            raise ValueError("invalid case configuration")

    @property
    def policy_id(self) -> str:
        return self.coverage_policy.policy_id


@dataclass(frozen=True, slots=True)
class ReviewDraft:
    kind: str
    title: str
    reason: str
    event_id: str
    next_check_at: datetime | None = None

    def __post_init__(self):
        if type(self.kind) is not str or self.kind not in {
            "OBSERVATION_REVIEW",
            "COVERAGE_REVIEW",
            "RESULT_VERIFICATION",
        }:
            raise ValueError("invalid review kind")
        _text(self.title, "title", 1, 120)
        _text(self.reason, "reason", 8, 700)
        if type(self.event_id) is not str or not _EVENT.fullmatch(self.event_id):
            raise ValueError("invalid event_id")
        normalized = _timestamp(self.next_check_at, "next_check_at")
        object.__setattr__(self, "next_check_at", normalized)


@dataclass(frozen=True, slots=True)
class EvidenceLink:
    task_id: str
    event_id: str
    reason: str

    def __post_init__(self):
        _identifier(self.task_id, "task_id")
        if type(self.event_id) is not str or not _EVENT.fullmatch(self.event_id):
            raise ValueError("invalid event_id")
        _text(self.reason, "reason", 8, 700)


@dataclass(frozen=True, slots=True)
class HumanAction:
    task_id: str
    expected_revision: int
    action: str
    note: str = ""
    title: str | None = None
    next_check_at: datetime | None = None

    def __post_init__(self):
        _identifier(self.task_id, "task_id")
        if (
            type(self.expected_revision) is not int
            or isinstance(self.expected_revision, bool)
            or self.expected_revision < 1
        ):
            raise ValueError("invalid expected_revision")
        if type(self.action) is not str or self.action not in {
            "APPROVE",
            "MODIFY",
            "DEFER",
            "DISMISS",
            "CANCEL",
        }:
            raise ValueError("invalid action")
        _text(self.note, "note", 0, 1000)
        normalized = _timestamp(self.next_check_at, "next_check_at")
        if self.action == "MODIFY":
            if self.title is None or normalized is None:
                raise ValueError("MODIFY requires title and next_check_at")
            _text(self.title, "title", 1, 120)
        elif self.action == "DEFER":
            if self.title is not None or normalized is None:
                raise ValueError("DEFER requires only next_check_at")
        elif self.title is not None or normalized is not None:
            raise ValueError("action cannot edit the plan")
        object.__setattr__(self, "next_check_at", normalized)


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    task_id: str
    case_id: str
    kind: str
    status: str
    revision: int
    title: str
    reason: str
    next_check_at: datetime | None
    simulated: bool
    created_at: datetime
    updated_at: datetime

    def __post_init__(self):
        _identifier(self.task_id, "task_id")
        _identifier(self.case_id, "case_id")
        if type(self.kind) is not str or self.kind not in {
            "OBSERVATION_REVIEW",
            "COVERAGE_REVIEW",
            "RESULT_VERIFICATION",
        }:
            raise ValueError("invalid review kind")
        if type(self.status) is not str or self.status not in {
            "PROPOSED",
            "APPROVED",
            "DEFERRED",
            "DISMISSED",
            "CANCELLED",
        }:
            raise ValueError("invalid review status")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("invalid revision")
        _text(self.title, "title", 1, 120)
        _text(self.reason, "reason", 8, 700)
        if type(self.simulated) is not bool:
            raise ValueError("invalid simulated flag")
        next_check = _timestamp(self.next_check_at, "next_check_at")
        created = _timestamp(self.created_at, "created_at", required=True)
        updated = _timestamp(self.updated_at, "updated_at", required=True)
        object.__setattr__(self, "next_check_at", next_check)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
