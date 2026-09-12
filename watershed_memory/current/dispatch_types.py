"""Immutable state records for bounded current dispatch decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .attention import AttentionResult
from .case_types import _identifier, _timestamp
from .delivery_types import _ATTEMPT, DeliveryReservation
from .fact_validation import event_id

_STATUSES = {"WAITING_FOR_SOURCE", "QUIET", "SUPPRESSED", "RESERVED", "HELD"}


@dataclass(frozen=True, slots=True)
class DispatchStep:
    case_id: str
    status: str
    event_id: str | None = None
    attention: AttentionResult | None = None
    reservation: DeliveryReservation | None = None
    held_attempt_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        if type(self.status) is not str or self.status not in _STATUSES:
            raise ValueError("invalid dispatch status")
        if self.event_id is not None:
            event_id(self.event_id)
        if self.attention is not None and type(self.attention) is not AttentionResult:
            raise ValueError("invalid attention result")
        if self.reservation is not None and type(self.reservation) is not DeliveryReservation:
            raise ValueError("invalid reservation")
        if self.held_attempt_id is not None and (
            type(self.held_attempt_id) is not str or not _ATTEMPT.fullmatch(self.held_attempt_id)
        ):
            raise ValueError("invalid held attempt")
        if self.status == "WAITING_FOR_SOURCE":
            if any(
                value is not None
                for value in (self.event_id, self.attention, self.reservation, self.held_attempt_id)
            ):
                raise ValueError("waiting step cannot carry a decision")
        elif self.status in {"QUIET", "SUPPRESSED"}:
            if (
                self.event_id is None
                or self.attention is None
                or self.attention.eligible
                or self.reservation is not None
                or self.held_attempt_id is not None
            ):
                raise ValueError("quiet step must carry an ineligible assessment")
        elif self.status == "RESERVED":
            if (
                self.event_id is None
                or self.attention is None
                or not self.attention.eligible
                or self.reservation is None
                or self.held_attempt_id is not None
            ):
                raise ValueError("reserved step is incomplete")
            if (
                self.reservation.context.case_id != self.case_id
                or self.reservation.context.current.event_id != self.event_id
            ):
                raise ValueError("reservation does not match dispatch step")
        elif self.reservation is not None or self.attention is not None:
            raise ValueError("held step cannot carry a live reservation")
        if self.status == "HELD" and self.held_attempt_id is None:
            raise ValueError("held step requires an attempt")


@dataclass(frozen=True, slots=True)
class DispatchStatus:
    case_id: str
    monitor_id: str
    activated_at: datetime
    updated_at: datetime
    initial_event_id: str | None
    history_count: int
    suppressed_count: int
    assessment_count: int
    numeric_event_id: str | None
    health_evaluated_at: datetime | None
    active_attempt_id: str | None
    active_status: str | None

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        _identifier(self.monitor_id, "monitor_id")
        activated = _timestamp(self.activated_at, "activated_at", required=True)
        updated = _timestamp(self.updated_at, "updated_at", required=True)
        if updated < activated:
            raise ValueError("updated_at precedes activation")
        if (
            type(self.history_count) is not int
            or isinstance(self.history_count, bool)
            or not 0 <= self.history_count <= 10000
        ):
            raise ValueError("invalid history count")
        for value in (self.suppressed_count, self.assessment_count):
            if type(value) is not int or isinstance(value, bool) or not 0 <= value < 2**63:
                raise ValueError("invalid dispatch count")
        if self.suppressed_count > 0 and self.assessment_count == 0:
            raise ValueError("suppression requires an assessment")
        for value in (self.initial_event_id, self.numeric_event_id):
            if value is not None:
                event_id(value)
        health = (
            None
            if self.health_evaluated_at is None
            else _timestamp(self.health_evaluated_at, "health_evaluated_at", required=True)
        )
        if (self.numeric_event_id is None) != (health is None):
            raise ValueError("numeric baseline and health time must be paired")
        if (self.assessment_count > 0) != (self.numeric_event_id is not None):
            raise ValueError("assessment count does not match baseline")
        if health is not None and not activated <= health <= updated:
            raise ValueError("health evaluation is outside active interval")
        if self.initial_event_id is None and (
            self.history_count
            or self.suppressed_count
            or self.assessment_count
            or self.numeric_event_id is not None
            or self.active_attempt_id is not None
            or self.active_status is not None
        ):
            raise ValueError("unactivated dispatch has state")
        if (
            self.initial_event_id is not None
            and self.assessment_count == 0
            and self.active_attempt_id is None
        ):
            raise ValueError("initial dispatch requires an active reservation")
        if (self.active_attempt_id is None) != (self.active_status is None):
            raise ValueError("active attempt and status must be paired")
        if self.active_attempt_id is not None and (
            type(self.active_attempt_id) is not str
            or not _ATTEMPT.fullmatch(self.active_attempt_id)
            or type(self.active_status) is not str
            or self.active_status not in {"RESERVED", "FAILED", "STALE"}
        ):
            raise ValueError("invalid active attempt")
        object.__setattr__(self, "activated_at", activated)
        object.__setattr__(self, "updated_at", updated)
        object.__setattr__(self, "health_evaluated_at", health)
