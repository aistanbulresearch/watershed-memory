"""Strict JSON command records for the local field desk HTTP boundary."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .fact_validation import timestamp
from .field_types import (
    AttachFieldEvidence,
    CorrectFieldResult,
    DecideFieldPlan,
    FieldPlanSpec,
    ModifyFieldPlan,
    ProposeFieldPlan,
    ReportFieldResult,
    VerifyFieldReport,
)

_ID = Annotated[StrictStr, Field(min_length=1, max_length=128)]
_TEXT = Annotated[StrictStr, Field(min_length=1, max_length=1000)]
_REVISION = Annotated[StrictInt, Field(ge=1, lt=2**63)]
_STAMP = Annotated[StrictStr, Field(min_length=1, max_length=64)]


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SpecBody(_Command):
    activity: StrictStr
    purpose: Annotated[StrictStr, Field(min_length=8, max_length=700)]
    assignee_role: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    window_start: _STAMP
    window_end: _STAMP
    required_evidence: list[StrictStr]

    def to_spec(self) -> FieldPlanSpec:
        return FieldPlanSpec(
            self.activity,
            self.purpose,
            self.assignee_role,
            timestamp(self.window_start),
            timestamp(self.window_end),
            tuple(self.required_evidence),
        )


class ProposeBody(_Command):
    operation: Literal["PROPOSE"]
    task_id: _ID
    expected_review_revision: _REVISION
    location_id: _ID
    location_revision: _REVISION
    spec: SpecBody

    def to_command(self, *, simulated: bool) -> ProposeFieldPlan:
        return ProposeFieldPlan(
            self.task_id,
            self.expected_review_revision,
            self.location_id,
            self.location_revision,
            self.spec.to_spec(),
            simulated,
        )


class DecideBody(_Command):
    operation: Literal["DECIDE"]
    plan_id: _ID
    expected_plan_revision: _REVISION
    action: Literal["APPROVE", "DEFER", "CANCEL"]
    defer_until: _STAMP | None = None

    def to_command(self, *, simulated: bool) -> DecideFieldPlan:
        del simulated
        return DecideFieldPlan(
            self.plan_id,
            self.expected_plan_revision,
            self.action,
            timestamp(self.defer_until) if self.defer_until is not None else None,
        )


class ModifyBody(_Command):
    operation: Literal["MODIFY"]
    plan_id: _ID
    expected_plan_revision: _REVISION
    location_id: _ID
    location_revision: _REVISION
    spec: SpecBody

    def to_command(self, *, simulated: bool) -> ModifyFieldPlan:
        return ModifyFieldPlan(
            self.plan_id,
            self.expected_plan_revision,
            self.location_id,
            self.location_revision,
            self.spec.to_spec(),
            simulated,
        )


class ReportBody(_Command):
    operation: Literal["REPORT"]
    plan_id: _ID
    performed_plan_revision: _REVISION
    outcome: Literal["COMPLETE", "PARTIAL", "NOT_DONE"]
    summary: _TEXT
    performed_start: _STAMP
    performed_end: _STAMP

    def to_command(self, *, simulated: bool) -> ReportFieldResult:
        return ReportFieldResult(
            self.plan_id,
            self.performed_plan_revision,
            self.outcome,
            self.summary,
            timestamp(self.performed_start),
            timestamp(self.performed_end),
            simulated,
        )


class CorrectBody(_Command):
    operation: Literal["CORRECT"]
    report_id: _ID
    expected_report_revision: _REVISION
    outcome: Literal["COMPLETE", "PARTIAL", "NOT_DONE"]
    summary: _TEXT
    performed_start: _STAMP
    performed_end: _STAMP

    def to_command(self, *, simulated: bool) -> CorrectFieldResult:
        return CorrectFieldResult(
            self.report_id,
            self.expected_report_revision,
            self.outcome,
            self.summary,
            timestamp(self.performed_start),
            timestamp(self.performed_end),
            simulated,
        )


class AttachBody(_Command):
    operation: Literal["ATTACH"]
    report_id: _ID
    expected_report_revision: _REVISION
    reference_kind: StrictStr
    evidence_category: StrictStr
    reference: _TEXT
    provenance: Annotated[StrictStr, Field(min_length=8, max_length=500)]
    observed_at: _STAMP | None
    sha256: StrictStr | None

    def to_command(self, *, simulated: bool) -> AttachFieldEvidence:
        return AttachFieldEvidence(
            self.report_id,
            self.expected_report_revision,
            self.reference_kind,
            self.evidence_category,
            self.reference,
            self.provenance,
            timestamp(self.observed_at) if self.observed_at is not None else None,
            self.sha256,
            simulated,
        )


class VerifyBody(_Command):
    operation: Literal["VERIFY"]
    report_id: _ID
    expected_report_revision: _REVISION
    evidence_ids: list[_ID]
    scope: Annotated[StrictStr, Field(min_length=8, max_length=500)]

    def to_command(self, *, simulated: bool) -> VerifyFieldReport:
        return VerifyFieldReport(
            self.report_id,
            self.expected_report_revision,
            tuple(self.evidence_ids),
            self.scope,
            simulated,
        )


CommandBody = Annotated[
    Union[ProposeBody, DecideBody, ModifyBody, ReportBody, CorrectBody, AttachBody, VerifyBody],
    Field(discriminator="operation"),
]


class FieldResponseBody(BaseModel):
    """Validated request envelope; server identity and simulation are excluded."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: _ID
    expected_case_revision: Annotated[StrictInt, Field(ge=0, lt=2**63)]
    command: CommandBody
