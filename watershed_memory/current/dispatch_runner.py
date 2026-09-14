"""Bounded dispatcher: reserve in SQLite, invoke outside it, then reconcile."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from threading import Event

from .attention import AttentionResult
from .case_types import WorkflowConflict, _identifier
from .delivery_types import _ATTEMPT, AllowanceExceeded, DeliveryReceipt, DeliveryReservation
from .dispatch_schema import ensure_schema
from .dispatch_store import DispatchStore, _settings
from .fact_validation import event_id
from .strands import (
    HEALTH_INSTRUCTION_VERSION,
    CurrentStrandsPlanner,
    CurrentTurnError,
)


@dataclass(frozen=True, slots=True)
class DispatchTick:
    case_id: str
    outcome: str
    event_id: str | None = None
    attempt_id: str | None = None
    attention: AttentionResult | None = None
    receipt: DeliveryReceipt | None = None

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        if type(self.outcome) is not str:
            raise ValueError("invalid dispatch tick")
        allowed = {
            "WAITING_FOR_SOURCE",
            "QUIET",
            "SUPPRESSED",
            "HELD",
            "COMMITTED",
            "FAILED",
            "STALE",
            "ALLOWANCE_EXHAUSTED",
            "OUTCOME_UNKNOWN",
        }
        if self.outcome not in allowed:
            raise ValueError("invalid dispatch outcome")
        if self.event_id is not None:
            event_id(self.event_id)
        if self.attempt_id is not None and (
            type(self.attempt_id) is not str or not _ATTEMPT.fullmatch(self.attempt_id)
        ):
            raise ValueError("invalid dispatch attempt")
        if self.attention is not None and type(self.attention) is not AttentionResult:
            raise ValueError("invalid attention record")
        if self.receipt is not None and type(self.receipt) is not DeliveryReceipt:
            raise ValueError("invalid delivery receipt")
        if self.outcome in {"WAITING_FOR_SOURCE", "ALLOWANCE_EXHAUSTED"}:
            if any(
                v is not None
                for v in (self.event_id, self.attempt_id, self.attention, self.receipt)
            ):
                raise ValueError("non-admission tick carries decision fields")
        elif self.outcome in {"QUIET", "SUPPRESSED"}:
            if (
                self.event_id is None
                or self.attention is None
                or self.attention.eligible
                or self.attempt_id is not None
                or self.receipt is not None
            ):
                raise ValueError("quiet tick requires an ineligible attention result")
        elif self.outcome == "HELD":
            if self.attempt_id is None or self.attention is not None or self.receipt is not None:
                raise ValueError("held tick requires only saved attempt identity")
        else:
            if (
                self.event_id is None
                or self.attempt_id is None
                or self.attention is None
                or not self.attention.eligible
            ):
                raise ValueError("attempt outcome requires its admission identity")
            if self.outcome == "OUTCOME_UNKNOWN":
                if self.receipt is not None:
                    raise ValueError("unknown outcome cannot claim a receipt")
            elif self.receipt is None or (
                self.receipt.case_id,
                self.receipt.event_id,
                self.receipt.attempt_id,
                self.receipt.status,
            ) != (self.case_id, self.event_id, self.attempt_id, self.outcome):
                raise ValueError("receipt identity differs from outcome")


class DispatchRunner:
    def __init__(self, store: DispatchStore, planner: CurrentStrandsPlanner, *, clock=None):
        if type(store) is not DispatchStore or type(planner) is not CurrentStrandsPlanner:
            raise ValueError("typed dispatch store and planner are required")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.store = store
        self.planner = planner
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def _profile(self, case_id: str):
        with self.store.journal.cases._connect() as db:
            db.execute("BEGIN")
            ensure_schema(db)
            _, _, profile = _settings(db, case_id)
        sdk = version("strands-agents")
        if (
            profile.model_id != self.planner.model_id
            or profile.instruction_version != HEALTH_INSTRUCTION_VERSION
            or profile.sdk_version != sdk
            or profile.mode != ("SCRIPTED_SDK" if self.planner.scripted_test else "STRANDS_CURRENT")
        ):
            raise ValueError("planner profile differs from dispatch settings")
        return profile

    def tick(self, case_id: str) -> DispatchTick:
        self._profile(case_id)
        try:
            step = self.store.prepare(case_id, now=self.clock())
        except AllowanceExceeded:
            return DispatchTick(case_id, "ALLOWANCE_EXHAUSTED")
        if step.status in {"WAITING_FOR_SOURCE", "QUIET", "SUPPRESSED", "HELD"}:
            return DispatchTick(
                case_id, step.status, step.event_id, step.held_attempt_id, step.attention
            )
        reservation = step.reservation
        if not isinstance(reservation, DeliveryReservation):
            raise WorkflowConflict("dispatch reservation is missing")
        try:
            execution = self.planner.plan(reservation.context)
        except CurrentTurnError as error:
            try:
                receipt = self.store.fail(reservation.attempt_id, error.failure, now=self.clock())
            except Exception:
                return DispatchTick(
                    case_id,
                    "OUTCOME_UNKNOWN",
                    step.event_id,
                    reservation.attempt_id,
                    step.attention,
                )
            return DispatchTick(
                case_id, "FAILED", step.event_id, reservation.attempt_id, step.attention, receipt
            )
        except Exception:
            return DispatchTick(
                case_id, "OUTCOME_UNKNOWN", step.event_id, reservation.attempt_id, step.attention
            )
        try:
            receipt = self.store.finish(reservation.attempt_id, execution, now=self.clock())
        except Exception:
            return DispatchTick(
                case_id, "OUTCOME_UNKNOWN", step.event_id, reservation.attempt_id, step.attention
            )
        return DispatchTick(
            case_id, receipt.status, step.event_id, reservation.attempt_id, step.attention, receipt
        )

    def run(
        self,
        case_id: str,
        *,
        max_steps: int,
        max_seconds: float,
        check_seconds: float = 30,
        stop_event: Event | None = None,
    ) -> tuple[DispatchTick, ...]:
        if type(max_steps) is not int or isinstance(max_steps, bool) or not 1 <= max_steps <= 100:
            raise ValueError("max_steps must be between1and100")
        if (
            type(max_seconds) not in (int, float)
            or isinstance(max_seconds, bool)
            or not math.isfinite(max_seconds)
            or not 1 <= max_seconds <= 21600
        ):
            raise ValueError("max_seconds is out of bounds")
        if (
            type(check_seconds) not in (int, float)
            or isinstance(check_seconds, bool)
            or not 1 <= check_seconds <= 3600
        ):
            raise ValueError("check_seconds is out of bounds")
        if stop_event is not None and not isinstance(stop_event, Event):
            raise ValueError("stop_event must be a threading Event")
        stop_event = stop_event if stop_event is not None else Event()
        if stop_event.is_set():
            return ()
        deadline = time.monotonic() + max_seconds
        results = []
        while len(results) < max_steps and time.monotonic() < deadline and not stop_event.is_set():
            result = self.tick(case_id)
            results.append(result)
            if len(results) >= max_steps or result.outcome in {
                "HELD",
                "FAILED",
                "STALE",
                "ALLOWANCE_EXHAUSTED",
                "OUTCOME_UNKNOWN",
            }:
                break
            remaining = min(check_seconds, max(0, deadline - time.monotonic()))
            if remaining and stop_event.wait(remaining):
                break
        return tuple(results)
