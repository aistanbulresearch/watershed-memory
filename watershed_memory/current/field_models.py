"""Immutable public field records; private command notes never enter these types."""

from dataclasses import dataclass
from datetime import datetime

from .case_types import _identifier
from .fact_validation import utc
from .field_types import (
    AttachFieldEvidence,
    FieldPlanSpec,
    ProposeFieldPlan,
    ReportFieldResult,
    VerifyFieldReport,
    _revision,
)
from .locations import LocationEntry


def _identities(record, *names):
    for name in names:
        _identifier(getattr(record, name), name)


def _times(record, *names):
    for name in names:
        value = getattr(record, name)
        object.__setattr__(record, name, utc(value))


@dataclass(frozen=True, slots=True)
class FieldPlanRecord:
    plan_id: str
    case_id: str
    task_id: str
    review_revision: int
    revision: int
    status: str
    spec: FieldPlanSpec
    location: LocationEntry
    simulated: bool
    change_kind: str
    author_kind: str
    recorded_by: str
    deferred_until: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self):
        _identities(self, "plan_id", "case_id", "recorded_by")
        _revision(self.revision)
        if type(self.location) is not LocationEntry:
            raise ValueError("invalid field location snapshot")
        ProposeFieldPlan(
            self.task_id,
            self.review_revision,
            self.location.location_id,
            self.location.revision,
            self.spec,
            self.simulated,
        )
        if (
            self.location.kind != "FIELD_SITE"
            or self.location.status != "APPROVED"
            or self.location.case_id != self.case_id
            or self.location.simulated != self.simulated
            or self.spec.activity not in self.location.activities
        ):
            raise ValueError("field location snapshot does not authorize this plan")
        statuses = {
            "PROPOSE": "PROPOSED",
            "APPROVE": "APPROVED",
            "MODIFY": "APPROVED",
            "DEFER": "DEFERRED",
            "CANCEL": "CANCELLED",
            "REPORT": "REPORTED",
        }
        if type(self.change_kind) is not str or statuses.get(self.change_kind) != self.status:
            raise ValueError("invalid field plan transition record")
        if (
            type(self.author_kind) is not str
            or self.author_kind not in ("HUMAN", "AGENT")
            or (self.author_kind == "AGENT" and self.change_kind != "PROPOSE")
        ):
            raise ValueError("unsupported field plan author")
        _times(self, "created_at", "updated_at")
        if not self.location.recorded_at <= self.updated_at or self.created_at > self.updated_at:
            raise ValueError("invalid field plan record time")
        if self.deferred_until is not None:
            object.__setattr__(self, "deferred_until", utc(self.deferred_until))
        if (self.status == "DEFERRED") != (self.deferred_until is not None):
            raise ValueError("invalid plan deferral record")
        if self.deferred_until is not None and self.deferred_until <= self.updated_at:
            raise ValueError("invalid plan deferral time")


@dataclass(frozen=True, slots=True)
class FieldReportRecord:
    report_id: str
    case_id: str
    plan_id: str
    performed_plan_revision: int
    revision: int
    outcome: str
    summary: str
    performed_start: datetime
    performed_end: datetime
    simulated: bool
    change_kind: str
    recorded_by: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self):
        _identities(self, "report_id", "case_id", "recorded_by")
        _revision(self.revision)
        ReportFieldResult(
            self.plan_id,
            self.performed_plan_revision,
            self.outcome,
            self.summary,
            self.performed_start,
            self.performed_end,
            self.simulated,
        )
        _times(self, "performed_start", "performed_end", "created_at", "updated_at")
        if self.change_kind not in ("REPORT", "CORRECTION"):
            raise ValueError("invalid report change kind")
        if self.performed_end > self.updated_at or self.created_at > self.updated_at:
            raise ValueError("invalid field report record time")


@dataclass(frozen=True, slots=True)
class FieldEvidenceRecord:
    evidence_id: str
    case_id: str
    report_id: str
    report_revision: int
    reference_kind: str
    evidence_category: str
    reference: str
    provenance: str
    observed_at: datetime | None
    sha256: str | None
    simulated: bool
    attached_by: str
    attached_at: datetime

    def __post_init__(self):
        _identities(self, "evidence_id", "case_id", "attached_by")
        AttachFieldEvidence(
            self.report_id,
            self.report_revision,
            self.reference_kind,
            self.evidence_category,
            self.reference,
            self.provenance,
            self.observed_at,
            self.sha256,
            self.simulated,
        )
        _times(self, "attached_at")
        if self.observed_at is not None:
            _times(self, "observed_at")
            if self.observed_at > self.attached_at:
                raise ValueError("evidence observation is after attachment")


@dataclass(frozen=True, slots=True)
class FieldVerificationRecord:
    verification_id: str
    case_id: str
    report_id: str
    report_revision: int
    evidence_ids: tuple[str, ...]
    scope: str
    method: str
    simulated: bool
    verified_by: str
    verified_at: datetime

    def __post_init__(self):
        _identities(self, "verification_id", "case_id", "verified_by")
        VerifyFieldReport(
            self.report_id, self.report_revision, self.evidence_ids, self.scope, self.simulated
        )
        if self.method != "AUTHORIZED_HUMAN_REVIEW":
            raise ValueError("unsupported field verification method")
        _times(self, "verified_at")


@dataclass(frozen=True, slots=True)
class FieldResultSnapshot:
    report: FieldReportRecord
    evidence: tuple[FieldEvidenceRecord, ...]
    verification: FieldVerificationRecord | None
    verification_level: str

    def __post_init__(self):
        if (
            type(self.report) is not FieldReportRecord
            or type(self.evidence) is not tuple
            or len(self.evidence) > 8
            or any(type(item) is not FieldEvidenceRecord for item in self.evidence)
        ):
            raise ValueError("invalid field result snapshot")
        ids = tuple(e.evidence_id for e in self.evidence)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("invalid evidence order or identity")
        identity = (
            self.report.case_id,
            self.report.report_id,
            self.report.revision,
            self.report.simulated,
        )
        for item in self.evidence:
            if (
                item.case_id,
                item.report_id,
                item.report_revision,
                item.simulated,
            ) != identity or item.attached_at < self.report.updated_at:
                raise ValueError("field evidence is outside this report revision")
        expected = "EVIDENCE_ATTACHED" if ids else "REPORTED"
        item = self.verification
        if item is not None:
            if (
                type(item) is not FieldVerificationRecord
                or not ids
                or (item.case_id, item.report_id, item.report_revision, item.simulated) != identity
                or item.evidence_ids != ids
                or any(e.attached_at > item.verified_at for e in self.evidence)
            ):
                raise ValueError("verification does not cover this exact report and evidence")
            expected = "VERIFIED"
        if self.verification_level != expected:
            raise ValueError("incorrect field verification level")


def field_dimension(plan: FieldPlanRecord, result: FieldResultSnapshot | None) -> str:
    if plan.status == "PROPOSED":
        return "NOT_PLANNED"
    if plan.status == "CANCELLED":
        return "CANCELLED"
    if result is None or result.report.outcome != "COMPLETE":
        return "PLANNED"
    return {
        "REPORTED": "REPORTED_COMPLETE",
        "EVIDENCE_ATTACHED": "VERIFICATION_PENDING",
        "VERIFIED": "VERIFIED_COMPLETE",
    }[result.verification_level]


def _joined_result(plan, result):
    if type(plan) is not FieldPlanRecord:
        raise ValueError("invalid field plan record")
    if (plan.status == "REPORTED") != (result is not None):
        raise ValueError("field plan and result disagree")
    if result is not None and (
        type(result) is not FieldResultSnapshot
        or (result.report.case_id, result.report.plan_id, result.report.simulated)
        != (plan.case_id, plan.plan_id, plan.simulated)
        or result.report.performed_plan_revision != plan.revision - 1
    ):
        raise ValueError("field result is outside this performed plan")


@dataclass(frozen=True, slots=True)
class FieldWorkSnapshot:
    plan: FieldPlanRecord
    result: FieldResultSnapshot | None
    parent_binding: str
    field_dimension: str

    def __post_init__(self):
        _joined_result(self.plan, self.result)
        if self.parent_binding not in ("CURRENT", "SUPERSEDED", "TERMINAL"):
            raise ValueError("invalid field parent binding")
        if self.field_dimension != field_dimension(self.plan, self.result):
            raise ValueError("invalid field work dimension")


@dataclass(frozen=True, slots=True)
class FieldMutationReceipt:
    request_id: str
    operation: str
    case_id: str
    case_revision: int
    plan: FieldPlanRecord
    recorded_at: datetime
    result: FieldResultSnapshot | None = None
    evidence: FieldEvidenceRecord | None = None
    verification: FieldVerificationRecord | None = None

    def __post_init__(self):
        _identities(self, "request_id", "case_id")
        _revision(self.case_revision)
        _times(self, "recorded_at")
        _joined_result(self.plan, self.result)
        shapes = {
            "PROPOSE": (False, False, False),
            "DECIDE": (False, False, False),
            "MODIFY": (False, False, False),
            "REPORT": (True, False, False),
            "CORRECT": (True, False, False),
            "ATTACH": (True, True, False),
            "VERIFY": (True, False, True),
        }
        if (
            type(self.operation) is not str
            or shapes.get(self.operation)
            != (self.result is not None, self.evidence is not None, self.verification is not None)
            or self.plan.case_id != self.case_id
            or self.recorded_at < self.plan.updated_at
        ):
            raise ValueError("invalid field receipt shape")
        if self.evidence is not None and self.evidence not in self.result.evidence:
            raise ValueError("receipt evidence is outside its result")
        if self.verification is not None and self.verification != self.result.verification:
            raise ValueError("receipt verification is outside its result")
