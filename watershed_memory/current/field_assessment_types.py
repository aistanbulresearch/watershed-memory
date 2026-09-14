"""Closed, immutable field decisions staged by the current assessment agent."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .assessment_types import CurrentAssessment, _canonical_json
from .case_types import _identifier, _text
from .field_types import FieldPlanSpec

_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_NAMES = {
    "get_field_context",
    "inspect_field_work",
    "list_approved_field_locations",
    "stage_field_decision",
}


def _positive(value: object, name: str) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError(f"invalid {name}")
    return value


def _optional_identifier(value: object, name: str) -> None:
    if value is not None:
        _identifier(value, name)


@dataclass(frozen=True, slots=True)
class AgentFieldProposal:
    task_id: str
    review_revision: int
    location_id: str
    location_revision: int
    spec: FieldPlanSpec

    def __post_init__(self) -> None:
        _identifier(self.task_id, "task_id")
        _positive(self.review_revision, "review_revision")
        _identifier(self.location_id, "location_id")
        _positive(self.location_revision, "location_revision")
        if type(self.spec) is not FieldPlanSpec:
            raise ValueError("invalid field plan spec")


@dataclass(frozen=True, slots=True)
class AgentFieldDecision:
    disposition: str
    basis_plan_id: str | None
    basis_report_id: str | None
    basis_report_revision: int | None
    basis_verification_level: str | None
    reason: str
    proposal: AgentFieldProposal | None

    def __post_init__(self) -> None:
        if type(self.disposition) is not str or self.disposition not in {
            "NO_NEW_FIELD_PLAN",
            "AWAIT_VERIFICATION",
            "PROPOSE_FIELD_PLAN",
        }:
            raise ValueError("invalid field disposition")
        _optional_identifier(self.basis_plan_id, "basis_plan_id")
        _optional_identifier(self.basis_report_id, "basis_report_id")
        if self.basis_report_revision is not None:
            _positive(self.basis_report_revision, "basis_report_revision")
        if self.basis_report_id is None and self.basis_report_revision is not None:
            raise ValueError("report revision requires report")
        if self.basis_report_id is not None and self.basis_plan_id is None:
            raise ValueError("report requires basis plan")
        if self.basis_report_id is None and self.basis_verification_level is not None:
            raise ValueError("verification level requires report")
        if self.basis_report_id is not None and (
            self.basis_report_revision is None
            or type(self.basis_verification_level) is not str
            or self.basis_verification_level not in {"REPORTED", "EVIDENCE_ATTACHED", "VERIFIED"}
        ):
            raise ValueError("invalid verification level")
        _text(self.reason, "reason", 8, 700)
        if self.proposal is not None and type(self.proposal) is not AgentFieldProposal:
            raise ValueError("invalid field proposal")
        if self.disposition == "AWAIT_VERIFICATION":
            if (
                self.proposal is not None
                or self.basis_report_id is None
                or self.basis_verification_level not in {"REPORTED", "EVIDENCE_ATTACHED"}
            ):
                raise ValueError("awaiting verification requires an unverified report")
        elif self.disposition == "PROPOSE_FIELD_PLAN":
            if type(self.proposal) is not AgentFieldProposal:
                raise ValueError("field proposal is required")
        elif self.proposal is not None:
            raise ValueError("no-new-plan decision cannot carry a proposal")


@dataclass(frozen=True, slots=True)
class FieldToolReceipt:
    name: str
    input_json: str
    output_json: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or self.name not in _NAMES:
            raise ValueError("invalid field tool name")
        _canonical_json(self.input_json, "input_json", 4096)
        _canonical_json(self.output_json, "output_json", 32768)


@dataclass(frozen=True, slots=True)
class CurrentAssessmentV3:
    schema_version: int
    base: CurrentAssessment
    field: AgentFieldDecision
    field_trace: tuple[FieldToolReceipt, ...]
    context_digest: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 3:
            raise ValueError("invalid field assessment schema")
        if type(self.base) is not CurrentAssessment or type(self.field) is not AgentFieldDecision:
            raise ValueError("invalid field assessment records")
        if (
            type(self.field_trace) is not tuple
            or not 2 <= len(self.field_trace) <= 8
            or any(type(item) is not FieldToolReceipt for item in self.field_trace)
        ):
            raise ValueError("invalid field trace")
        names = tuple(item.name for item in self.field_trace)
        if (
            names[0] != "get_field_context"
            or names[-1] != "stage_field_decision"
            or names.count("stage_field_decision") != 1
        ):
            raise ValueError("field trace must inspect context and stage last")
        if len(self.base.trace) + len(self.field_trace) > 16:
            raise ValueError("combined assessment trace is too large")
        if type(self.context_digest) is not str or not _HASH.fullmatch(self.context_digest):
            raise ValueError("invalid context digest")
