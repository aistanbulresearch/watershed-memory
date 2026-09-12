"""Immutable, validated commands for operator-owned field work."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .case_types import _identifier, _text
from .fact_validation import utc
from .locations import _url

_ROLES = {"COORDINATOR", "FIELD_OPERATOR", "VERIFIER"}
_ACTIVITIES = {"VISUAL_INSPECTION", "SAMPLING", "MAINTENANCE_REVIEW"}
_EVIDENCE = {
    "PHOTO_REFERENCE",
    "SAMPLE_RECORD_REFERENCE",
    "INSPECTION_RECORD_REFERENCE",
    "MAINTENANCE_RECORD_REFERENCE",
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _revision(value: object, name: str = "revision") -> int:
    if type(value) is not int or not 1 <= value < 2**63:
        raise ValueError(f"invalid {name}")
    return value


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"invalid {name}")
    return value


def _tuple(
    value: object, name: str, maximum: int, choices: set[str] | None = None
) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or not 1 <= len(value) <= maximum
        or any(type(item) is not str for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"invalid {name}")
    if any(
        type(item) is not str or (choices is not None and item not in choices) for item in value
    ):
        raise ValueError(f"invalid {name}")
    return value


def _note(value: object) -> str:
    if type(value) is not str or len(value) > 1000 or not value.isprintable():
        raise ValueError("invalid private_note")
    return value


def _time(value: object, name: str, required: bool = True) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, datetime):
        raise ValueError(f"invalid {name}")
    return utc(value)


@dataclass(frozen=True, slots=True)
class FieldPrincipal:
    principal_id: str
    roles: tuple[str, ...]
    case_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.principal_id, "principal_id")
        _tuple(self.roles, "roles", 3, _ROLES)
        _tuple(self.case_ids, "case_ids", 32)
        for case_id in self.case_ids:
            _identifier(case_id, "case_id")


@dataclass(frozen=True, slots=True)
class FieldPlanSpec:
    activity: str
    purpose: str
    assignee_role: str
    window_start: datetime
    window_end: datetime
    required_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.activity) is not str or self.activity not in _ACTIVITIES:
            raise ValueError("invalid activity")
        _text(self.purpose, "purpose", 8, 700)
        _text(self.assignee_role, "assignee_role", 1, 120)
        start = _time(self.window_start, "window_start")
        end = _time(self.window_end, "window_end")
        if start >= end:
            raise ValueError("invalid planning window")
        _tuple(self.required_evidence, "required_evidence", 4, _EVIDENCE)
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)


class _Command:
    __slots__ = ()

    def _common(self) -> None:
        _note(self.private_note)


@dataclass(frozen=True, slots=True)
class ProposeFieldPlan(_Command):
    task_id: str
    expected_review_revision: int
    location_id: str
    location_revision: int
    spec: FieldPlanSpec
    expected_simulated: bool
    private_note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.task_id, "task_id")
        _revision(self.expected_review_revision, "expected_review_revision")
        _identifier(self.location_id, "location_id")
        _revision(self.location_revision, "location_revision")
        if type(self.spec) is not FieldPlanSpec:
            raise ValueError("invalid spec")
        _bool(self.expected_simulated, "expected_simulated")
        self._common()


@dataclass(frozen=True, slots=True)
class DecideFieldPlan(_Command):
    plan_id: str
    expected_plan_revision: int
    action: str
    defer_until: datetime | None = None
    private_note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.plan_id, "plan_id")
        _revision(self.expected_plan_revision, "expected_plan_revision")
        if type(self.action) is not str or self.action not in {"APPROVE", "DEFER", "CANCEL"}:
            raise ValueError("invalid action")
        when = _time(self.defer_until, "defer_until", False)
        if self.action == "DEFER" and when is None:
            raise ValueError("defer_until is required")
        if self.action != "DEFER" and when is not None:
            raise ValueError("defer_until is only valid for DEFER")
        object.__setattr__(self, "defer_until", when)
        self._common()


@dataclass(frozen=True, slots=True)
class ModifyFieldPlan(_Command):
    plan_id: str
    expected_plan_revision: int
    location_id: str
    location_revision: int
    spec: FieldPlanSpec
    expected_simulated: bool
    private_note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.plan_id, "plan_id")
        _revision(self.expected_plan_revision, "expected_plan_revision")
        _identifier(self.location_id, "location_id")
        _revision(self.location_revision, "location_revision")
        if type(self.spec) is not FieldPlanSpec:
            raise ValueError("invalid spec")
        _bool(self.expected_simulated, "expected_simulated")
        self._common()


@dataclass(frozen=True, slots=True)
class ReportFieldResult(_Command):
    plan_id: str
    performed_plan_revision: int
    outcome: str
    summary: str
    performed_start: datetime
    performed_end: datetime
    expected_simulated: bool
    private_note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.plan_id, "plan_id")
        _revision(self.performed_plan_revision, "performed_plan_revision")
        if type(self.outcome) is not str or self.outcome not in {"COMPLETE", "PARTIAL", "NOT_DONE"}:
            raise ValueError("invalid outcome")
        _text(self.summary, "summary", 8, 1000)
        start = _time(self.performed_start, "performed_start")
        end = _time(self.performed_end, "performed_end")
        if start > end:
            raise ValueError("invalid performed window")
        _bool(self.expected_simulated, "expected_simulated")
        object.__setattr__(self, "performed_start", start)
        object.__setattr__(self, "performed_end", end)
        self._common()


@dataclass(frozen=True, slots=True)
class CorrectFieldResult(_Command):
    report_id: str
    expected_report_revision: int
    outcome: str
    summary: str
    performed_start: datetime
    performed_end: datetime
    expected_simulated: bool
    private_note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.report_id, "report_id")
        _revision(self.expected_report_revision, "expected_report_revision")
        if type(self.outcome) is not str or self.outcome not in {"COMPLETE", "PARTIAL", "NOT_DONE"}:
            raise ValueError("invalid outcome")
        _text(self.summary, "summary", 8, 1000)
        start = _time(self.performed_start, "performed_start")
        end = _time(self.performed_end, "performed_end")
        if start > end:
            raise ValueError("invalid performed window")
        _bool(self.expected_simulated, "expected_simulated")
        object.__setattr__(self, "performed_start", start)
        object.__setattr__(self, "performed_end", end)
        self._common()


@dataclass(frozen=True, slots=True)
class AttachFieldEvidence(_Command):
    report_id: str
    expected_report_revision: int
    reference_kind: str
    evidence_category: str
    reference: str
    provenance: str
    observed_at: datetime | None
    sha256: str | None
    expected_simulated: bool
    private_note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.report_id, "report_id")
        _revision(self.expected_report_revision, "expected_report_revision")
        if type(self.reference_kind) is not str or self.reference_kind not in {
            "EXTERNAL_REFERENCE",
            "OPERATOR_RECORD_REFERENCE",
        }:
            raise ValueError("invalid reference_kind")
        if type(self.evidence_category) is not str or self.evidence_category not in _EVIDENCE:
            raise ValueError("invalid evidence_category")
        if self.reference_kind == "EXTERNAL_REFERENCE":
            _url(self.reference)
        else:
            _identifier(self.reference, "reference")
        _text(self.provenance, "provenance", 8, 500)
        observed = _time(self.observed_at, "observed_at", False)
        if self.sha256 is not None and (
            type(self.sha256) is not str or not _DIGEST.fullmatch(self.sha256)
        ):
            raise ValueError("invalid sha256")
        _bool(self.expected_simulated, "expected_simulated")
        object.__setattr__(self, "observed_at", observed)
        self._common()


@dataclass(frozen=True, slots=True)
class VerifyFieldReport(_Command):
    report_id: str
    expected_report_revision: int
    evidence_ids: tuple[str, ...]
    scope: str
    expected_simulated: bool
    private_note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.report_id, "report_id")
        _revision(self.expected_report_revision, "expected_report_revision")
        if type(self.evidence_ids) is not tuple or not 1 <= len(self.evidence_ids) <= 8:
            raise ValueError("invalid evidence_ids")
        for evidence_id in self.evidence_ids:
            _identifier(evidence_id, "evidence_id")
        if tuple(sorted(self.evidence_ids)) != self.evidence_ids or len(
            set(self.evidence_ids)
        ) != len(self.evidence_ids):
            raise ValueError("invalid evidence_ids")
        _text(self.scope, "scope", 8, 500)
        _bool(self.expected_simulated, "expected_simulated")
        self._common()
