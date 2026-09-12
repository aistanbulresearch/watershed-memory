"""Frozen records for the explicit current-agent delivery journal."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .assessment_types import _DIGEST, _canonical_json
from .case_types import ReviewRecord, WorkflowConflict, _identifier, _timestamp
from .context import CurrentContext

_ATTEMPT = re.compile(r"attempt-[0-9a-f]{32}\Z", re.ASCII)
_MODES = {"SCRIPTED_SDK", "STRANDS_CURRENT"}
_STATUSES = {"RESERVED", "COMMITTED", "FAILED", "STALE", "ABANDONED"}


@dataclass(frozen=True, slots=True)
class InvocationAllowance:
    scripted_attempts: int
    provider_attempts: int

    def __post_init__(self):
        for value in (self.scripted_attempts, self.provider_attempts):
            if type(value) is not int or not 0 <= value <= 10000:
                raise ValueError("invalid invocation allowance")


@dataclass(frozen=True, slots=True)
class InvocationProfile:
    mode: str
    model_id: str
    instruction_version: str
    sdk_version: str

    def __post_init__(self):
        if type(self.mode) is not str or self.mode not in _MODES:
            raise ValueError("invalid invocation mode")
        for value in (self.model_id, self.instruction_version, self.sdk_version):
            if type(value) is not str or not 1 <= len(value) <= 200 or not value.isprintable():
                raise ValueError("invalid invocation profile")


@dataclass(frozen=True, slots=True)
class AllowanceSnapshot:
    scripted_limit: int
    scripted_used: int
    provider_limit: int
    provider_used: int

    def __post_init__(self):
        for limit, used in (
            (self.scripted_limit, self.scripted_used),
            (self.provider_limit, self.provider_used),
        ):
            if type(limit) is not int or type(used) is not int or not 0 <= used <= limit <= 10000:
                raise ValueError("invalid allowance snapshot")


@dataclass(frozen=True, slots=True)
class DeliveryReservation:
    attempt_id: str
    request_id: str
    profile: InvocationProfile
    source_delivery: bool
    context: CurrentContext
    reserved_at: datetime

    def __post_init__(self):
        if type(self.attempt_id) is not str or not _ATTEMPT.fullmatch(self.attempt_id):
            raise ValueError("invalid attempt_id")
        _identifier(self.request_id, "request_id")
        if type(self.profile) is not InvocationProfile or type(self.source_delivery) is not bool:
            raise ValueError("invalid reservation")
        if type(self.context) is not CurrentContext:
            raise ValueError("invalid reservation context")
        reserved = _timestamp(self.reserved_at, "reserved_at", required=True)
        if reserved != self.context.evaluated_at:
            raise ValueError("reservation time differs from context")
        object.__setattr__(self, "reserved_at", reserved)


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    attempt_id: str
    request_id: str
    case_id: str
    event_id: str
    profile: InvocationProfile
    status: str
    source_delivery: bool
    recorded_at: datetime
    work: tuple[ReviewRecord, ...] = ()
    execution_json: str | None = None
    failure_json: str | None = None
    reason: str | None = None

    def __post_init__(self):
        if type(self.attempt_id) is not str or not _ATTEMPT.fullmatch(self.attempt_id):
            raise ValueError("invalid attempt_id")
        _identifier(self.request_id, "request_id")
        _identifier(self.case_id, "case_id")
        if type(self.event_id) is not str or not _DIGEST.fullmatch(self.event_id):
            raise ValueError("invalid event_id")
        if (
            type(self.profile) is not InvocationProfile
            or type(self.status) is not str
            or self.status not in _STATUSES
            or type(self.source_delivery) is not bool
        ):
            raise ValueError("invalid delivery receipt")
        recorded = _timestamp(self.recorded_at, "recorded_at", required=True)
        if (
            type(self.work) is not tuple
            or len(self.work) > 2
            or any(type(item) is not ReviewRecord for item in self.work)
        ):
            raise ValueError("invalid receipt work")
        if self.work and (
            len({item.task_id for item in self.work}) != len(self.work)
            or len({item.kind for item in self.work}) != len(self.work)
            or any(item.case_id != self.case_id for item in self.work)
        ):
            raise ValueError("invalid receipt work")
        if self.execution_json is not None:
            _canonical_json(self.execution_json, "execution_json", 524288)
        if self.failure_json is not None:
            _canonical_json(self.failure_json, "failure_json", 524288)
        if self.reason is not None and (
            type(self.reason) is not str
            or not 8 <= len(self.reason) <= 700
            or not self.reason.isprintable()
        ):
            raise ValueError("invalid receipt reason")
        if self.status == "RESERVED" and (
            self.work or self.execution_json or self.failure_json or self.reason
        ):
            raise ValueError("reserved receipt has final fields")
        if self.status == "COMMITTED" and (
            self.execution_json is None or self.failure_json is not None or self.reason is not None
        ):
            raise ValueError("invalid committed receipt")
        if self.status == "FAILED" and (
            self.failure_json is None
            or self.execution_json is not None
            or self.work
            or self.reason is not None
        ):
            raise ValueError("invalid failed receipt")
        if self.status == "STALE" and (
            self.execution_json is None
            or self.failure_json is not None
            or self.work
            or self.reason is not None
        ):
            raise ValueError("invalid stale receipt")
        if self.status == "ABANDONED" and (self.reason is None or self.work):
            raise ValueError("invalid abandoned receipt")
        object.__setattr__(self, "recorded_at", recorded)


class DeliveryHeld(WorkflowConflict):
    pass


class AllowanceExceeded(WorkflowConflict):
    pass
