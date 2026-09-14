"""Staged v3 delivery records; persistence and dispatch remain elsewhere."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime

from .assessment_types import ToolReceipt, _canonical_json
from .case_store import _expected
from .case_types import _identifier, _timestamp
from .context_v3_types import CurrentContextV3
from .delivery_types import DeliveryReceipt, InvocationProfile
from .field_assessment_types import (
    CurrentAssessmentV3,
    FieldToolReceipt,
)
from .field_models import FieldMutationReceipt

_ATTEMPT = re.compile(r"attempt-[0-9a-f]{32}\Z", re.ASCII)
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _count(value: object, name: str, maximum: int, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class FieldDeliveryReservation:
    attempt_id: str
    request_id: str
    profile: InvocationProfile
    source_delivery: bool
    context: CurrentContextV3
    reserved_at: datetime

    def __post_init__(self) -> None:
        if type(self.attempt_id) is not str or not _ATTEMPT.fullmatch(self.attempt_id):
            raise ValueError("invalid attempt_id")
        _identifier(self.request_id, "request_id")
        if (
            type(self.profile) is not InvocationProfile
            or self.profile.instruction_version != "watershed-current-v3"
        ):
            raise ValueError("invalid v3 profile")
        if type(self.source_delivery) is not bool or type(self.context) is not CurrentContextV3:
            raise ValueError("invalid v3 reservation")
        reserved = _timestamp(self.reserved_at, "reserved_at", required=True)
        if reserved != self.context.base.evaluated_at:
            raise ValueError("reservation time differs from context")
        object.__setattr__(self, "reserved_at", reserved)


@dataclass(frozen=True, slots=True)
class CurrentExecutionV3:
    assessment: CurrentAssessmentV3
    mode: str
    model_id: str
    instruction_version: str
    sdk_version: str
    model_calls: int
    tool_attempts: int
    elapsed_seconds: float
    usage_json: str
    stop_reason: str

    def __post_init__(self) -> None:
        if (
            type(self.assessment) is not CurrentAssessmentV3
            or type(self.mode) is not str
            or self.mode not in {"SCRIPTED_SDK", "STRANDS_CURRENT"}
        ):
            raise ValueError("invalid v3 execution")
        if self.instruction_version != "watershed-current-v3":
            raise ValueError("invalid v3 instruction")
        for value in (self.model_id, self.instruction_version, self.sdk_version):
            if type(value) is not str or not 1 <= len(value) <= 200 or not value.isprintable():
                raise ValueError("invalid execution profile")
        _count(self.model_calls, "model_calls", 8, 1)
        minimum = len(self.assessment.base.trace) + len(self.assessment.field_trace)
        _count(self.tool_attempts, "tool_attempts", 16, 1)
        if (
            self.tool_attempts < minimum
            or type(self.elapsed_seconds) is not float
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
            or self.stop_reason != "end_turn"
        ):
            raise ValueError("invalid execution bounds")
        _canonical_json(self.usage_json, "usage_json", 1024)


@dataclass(frozen=True, slots=True)
class CurrentFailureV3:
    case_id: str
    case_revision: int
    policy_digest: str
    event_id: str
    context_digest: str
    mode: str
    model_id: str
    instruction_version: str
    sdk_version: str
    model_calls: int
    tool_attempts: int
    source_trace: tuple[ToolReceipt, ...]
    field_trace: tuple[FieldToolReceipt, ...]
    usage_json: str
    status: str = "FAILED"
    code: str = "CURRENT_V3_TURN_FAILED"

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        _expected(self.case_revision)
        for value, name in (
            (self.policy_digest, "policy_digest"),
            (self.event_id, "event_id"),
            (self.context_digest, "context_digest"),
        ):
            if type(value) is not str or not _HASH.fullmatch(value):
                raise ValueError(f"invalid {name}")
        if (
            type(self.mode) is not str
            or self.mode not in {"SCRIPTED_SDK", "STRANDS_CURRENT"}
            or self.instruction_version != "watershed-current-v3"
            or self.status != "FAILED"
            or type(self.code) is not str
            or self.code not in {"CURRENT_V3_TURN_FAILED", "CURRENT_V3_TOOL_BUDGET_EXHAUSTED"}
        ):
            raise ValueError("invalid failure identity")
        for value in (self.model_id, self.instruction_version, self.sdk_version):
            if type(value) is not str or not 1 <= len(value) <= 200 or not value.isprintable():
                raise ValueError("invalid failure profile")
        _count(self.model_calls, "model_calls", 8)
        _count(self.tool_attempts, "tool_attempts", 16)
        if (
            type(self.source_trace) is not tuple
            or len(self.source_trace) > 12
            or any(type(item) is not ToolReceipt for item in self.source_trace)
        ):
            raise ValueError("invalid source trace")
        if (
            type(self.field_trace) is not tuple
            or len(self.field_trace) > 8
            or any(type(item) is not FieldToolReceipt for item in self.field_trace)
        ):
            raise ValueError("invalid field trace")
        if len(self.source_trace) + len(self.field_trace) > self.tool_attempts:
            raise ValueError("failure trace exceeds attempts")
        _canonical_json(self.usage_json, "usage_json", 1024)


@dataclass(frozen=True, slots=True)
class FieldDeliveryReceipt:
    delivery: DeliveryReceipt
    field_plan: FieldMutationReceipt | None = None

    def __post_init__(self) -> None:
        if (
            type(self.delivery) is not DeliveryReceipt
            or self.delivery.profile.instruction_version != "watershed-current-v3"
        ):
            raise ValueError("invalid committed delivery")
        if self.field_plan is None:
            return
        if (
            type(self.field_plan) is not FieldMutationReceipt
            or self.delivery.status != "COMMITTED"
            or self.field_plan.operation != "PROPOSE"
            or self.field_plan.plan.revision != 1
            or self.field_plan.plan.status != "PROPOSED"
            or self.field_plan.plan.author_kind != "AGENT"
            or self.field_plan.plan.case_id != self.delivery.case_id
            or self.field_plan.plan.recorded_by != self.delivery.attempt_id
            or self.field_plan.request_id != self.delivery.attempt_id + "-field-0"
            or self.field_plan.recorded_at != self.delivery.recorded_at
            or self.field_plan.result is not None
            or self.field_plan.evidence is not None
            or self.field_plan.verification is not None
        ):
            raise ValueError("invalid agent field proposal receipt")
