"""Bounded field-aware dispatch runner for the v3 current-case workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version

from .attention import AttentionResult
from .context3_schema import ensure_schema
from .delivery_types import AllowanceExceeded
from .dispatch_runner import DispatchRunner, DispatchTick
from .field_delivery_types import FieldDeliveryReceipt
from .field_dispatch_store import FieldDispatchStore, _settings_v3
from .field_strands import CurrentStrandsPlannerV3, CurrentTurnErrorV3


@dataclass(frozen=True, slots=True)
class FieldDispatchTick:
    """Detached field dispatch outcome with a typed v3 delivery receipt."""

    case_id: str
    outcome: str
    event_id: str | None = None
    attempt_id: str | None = None
    attention: AttentionResult | None = None
    receipt: FieldDeliveryReceipt | None = None

    def __post_init__(self) -> None:
        delivery = None
        if self.receipt is not None:
            if type(self.receipt) is not FieldDeliveryReceipt:
                raise ValueError("invalid field delivery receipt")
            delivery = self.receipt.delivery
        try:
            DispatchTick(
                self.case_id,
                self.outcome,
                self.event_id,
                self.attempt_id,
                self.attention,
                delivery,
            )
        except ValueError:
            raise


class FieldDispatchRunner(DispatchRunner):
    """Run one v3 field turn while keeping all inference outside SQLite."""

    def __init__(self, store: FieldDispatchStore, planner: CurrentStrandsPlannerV3, *, clock=None):
        if type(store) is not FieldDispatchStore or type(planner) is not CurrentStrandsPlannerV3:
            raise ValueError("typed field dispatch store and planner are required")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.store = store
        self.planner = planner
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def _profile(self, case_id: str):
        with self.store.cases._connect() as db:
            db.execute("BEGIN")
            ensure_schema(db)
            _, _, profile, _ = _settings_v3(db, case_id)
        sdk = version("strands-agents")
        expected_mode = "SCRIPTED_SDK" if self.planner.scripted_test else "STRANDS_CURRENT"
        if (
            profile.model_id != self.planner.model_id
            or profile.instruction_version != "watershed-current-v3"
            or profile.sdk_version != sdk
            or profile.mode != expected_mode
        ):
            raise ValueError("planner profile differs from field dispatch settings")
        return profile

    def tick(self, case_id: str) -> FieldDispatchTick:
        self._profile(case_id)
        try:
            step = self.store.prepare(case_id, now=self.clock())
        except AllowanceExceeded:
            return FieldDispatchTick(case_id, "ALLOWANCE_EXHAUSTED")
        if step.status in {"WAITING_FOR_SOURCE", "QUIET", "SUPPRESSED", "HELD"}:
            return FieldDispatchTick(
                case_id,
                step.status,
                step.event_id,
                step.held_attempt_id,
                step.attention,
            )
        reservation = step.reservation
        if step.status != "RESERVED" or reservation is None:
            raise ValueError("field dispatch reservation is missing")
        try:
            execution = self.planner.plan(reservation.context)
        except CurrentTurnErrorV3 as error:
            try:
                receipt = self.store.fail(reservation.attempt_id, error.failure, now=self.clock())
            except Exception:
                return FieldDispatchTick(
                    case_id,
                    "OUTCOME_UNKNOWN",
                    step.event_id,
                    reservation.attempt_id,
                    step.attention,
                )
            return FieldDispatchTick(
                case_id,
                "FAILED",
                step.event_id,
                reservation.attempt_id,
                step.attention,
                receipt,
            )
        except Exception:
            return FieldDispatchTick(
                case_id,
                "OUTCOME_UNKNOWN",
                step.event_id,
                reservation.attempt_id,
                step.attention,
            )
        try:
            receipt = self.store.finish(reservation.attempt_id, execution, now=self.clock())
        except Exception:
            return FieldDispatchTick(
                case_id,
                "OUTCOME_UNKNOWN",
                step.event_id,
                reservation.attempt_id,
                step.attention,
            )
        return FieldDispatchTick(
            case_id,
            receipt.delivery.status,
            step.event_id,
            reservation.attempt_id,
            step.attention,
            receipt,
        )
