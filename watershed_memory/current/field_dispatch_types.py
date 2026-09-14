"""Immutable dispatch admission state for field-aware v3 turns."""

from __future__ import annotations

from dataclasses import dataclass

from .attention import AttentionResult
from .case_types import _identifier
from .delivery_types import _ATTEMPT
from .fact_validation import event_id
from .field_delivery_types import FieldDeliveryReservation

_STATUSES = {"WAITING_FOR_SOURCE", "QUIET", "SUPPRESSED", "RESERVED", "HELD"}


@dataclass(frozen=True, slots=True)
class FieldDispatchStep:
    case_id: str
    status: str
    event_id: str | None = None
    attention: AttentionResult | None = None
    reservation: FieldDeliveryReservation | None = None
    held_attempt_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        if type(self.status) is not str or self.status not in _STATUSES:
            raise ValueError("invalid dispatch status")
        if self.event_id is not None:
            event_id(self.event_id)
        if self.attention is not None and type(self.attention) is not AttentionResult:
            raise ValueError("invalid attention result")
        if self.reservation is not None and type(self.reservation) is not FieldDeliveryReservation:
            raise ValueError("invalid field reservation")
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
            context = self.reservation.context
            if (
                context.base.case_id != self.case_id
                or context.base.current.event_id != self.event_id
            ):
                raise ValueError("reservation does not match dispatch step")
        elif self.reservation is not None or self.attention is not None:
            raise ValueError("held step cannot carry a live reservation")
        if self.status == "HELD" and self.held_attempt_id is None:
            raise ValueError("held step requires an attempt")
