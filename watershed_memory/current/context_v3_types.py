"""Immutable field-aware context records used by the v3 integration boundary."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from . import case_records
from .case_types import _identifier, _timestamp
from .context import CurrentContext
from .context_reference import decode_reference
from .field_codec import encode
from .field_models import FieldWorkSnapshot
from .locations import LocationEntry

_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SELECTIONS = {"CURRENT_PLAN", "STRANDED_PLAN", "LATEST_RESULT"}
_BINDINGS = {"CURRENT", "SUPERSEDED", "TERMINAL"}
_PARENT_STATUSES = {"PROPOSED", "APPROVED", "DEFERRED", "DISMISSED", "CANCELLED"}


def _revision(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError(f"invalid {name}")
    return value


def _positive_revision(value: object, name: str) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError(f"invalid {name}")
    return value


def _tuple(value: object, name: str, maximum: int) -> tuple:
    if type(value) is not tuple or len(value) > maximum:
        raise ValueError(f"invalid {name}")
    return value


def _hash(value: object, name: str) -> str:
    if type(value) is not str or not _HASH.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _snapshot(value: object, case_id: str, simulated: bool, evaluated: datetime) -> None:
    if type(value) is not FieldWorkSnapshot:
        raise ValueError("invalid field snapshot")
    plan = value.plan
    if (
        plan.case_id != case_id
        or plan.simulated is not simulated
        or value.parent_binding not in _BINDINGS
    ):
        raise ValueError("field snapshot identity mismatch")
    if plan.created_at > evaluated or plan.updated_at > evaluated:
        raise ValueError("field snapshot is from the future")
    if value.result is not None:
        result = value.result
        if result.report.case_id != case_id or result.report.simulated is not simulated:
            raise ValueError("field result identity mismatch")
        if result.report.created_at > evaluated or result.report.updated_at > evaluated:
            raise ValueError("field result is from the future")
        for evidence in result.evidence:
            if (
                evidence.case_id != case_id
                or evidence.simulated is not simulated
                or evidence.attached_at > evaluated
            ):
                raise ValueError("field evidence identity mismatch")
        if result.verification is not None and (
            result.verification.case_id != case_id
            or result.verification.simulated is not simulated
            or result.verification.verified_at > evaluated
        ):
            raise ValueError("field verification identity mismatch")
    location = plan.location
    if (
        type(location) is not LocationEntry
        or location.case_id != case_id
        or location.simulated is not simulated
    ):
        raise ValueError("field location identity mismatch")
    if location.recorded_at > evaluated or (
        location.approved_at is not None and location.approved_at > evaluated
    ):
        raise ValueError("field location is from the future")


@dataclass(frozen=True, slots=True)
class FieldContext:
    case_id: str
    case_revision: int
    simulated: bool
    evaluated_at: datetime
    state: str
    current_plans: tuple[FieldWorkSnapshot, ...]
    stranded_plans: tuple[FieldWorkSnapshot, ...]
    latest_results: tuple[FieldWorkSnapshot, ...]
    approved_locations: tuple[LocationEntry, ...]
    has_more_stranded_plans: bool
    has_more_results: bool

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        _revision(self.case_revision, "case_revision")
        if (
            type(self.simulated) is not bool
            or type(self.state) is not str
            or self.state not in {"EMPTY", "PRESENT"}
        ):
            raise ValueError("invalid field context identity")
        evaluated = _timestamp(self.evaluated_at, "evaluated_at", required=True)
        for name, values, maximum in (
            ("current_plans", self.current_plans, 2),
            ("stranded_plans", self.stranded_plans, 3),
            ("latest_results", self.latest_results, 3),
        ):
            _tuple(values, name, maximum)
            for item in values:
                _snapshot(item, self.case_id, self.simulated, evaluated)
            ids = [item.plan.plan_id for item in values]
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate {name}")
            if name == "current_plans" and any(item.parent_binding != "CURRENT" for item in values):
                raise ValueError("current plan has non-current binding")
            if name in ("current_plans", "stranded_plans") and any(
                item.plan.status not in {"PROPOSED", "APPROVED", "DEFERRED"} for item in values
            ):
                raise ValueError("current plan has terminal status")
            if name == "stranded_plans" and any(
                item.parent_binding not in {"SUPERSEDED", "TERMINAL"} for item in values
            ):
                raise ValueError("stranded plan has current binding")
            if name == "latest_results" and any(item.plan.status != "REPORTED" for item in values):
                raise ValueError("latest result is not reported")
        all_ids = [
            item.plan.plan_id
            for values in (self.current_plans, self.stranded_plans, self.latest_results)
            for item in values
        ]
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("field plan appears in multiple partitions")
        if tuple((item.plan.task_id, item.plan.plan_id) for item in self.current_plans) != tuple(
            sorted((item.plan.task_id, item.plan.plan_id) for item in self.current_plans)
        ):
            raise ValueError("current plans are not deterministically ordered")
        if type(self.approved_locations) is not tuple or len(self.approved_locations) > 16:
            raise ValueError("invalid approved locations")
        keys = []
        for location in self.approved_locations:
            if (
                type(location) is not LocationEntry
                or location.case_id != self.case_id
                or location.simulated is not self.simulated
                or location.kind != "FIELD_SITE"
                or location.status != "APPROVED"
                or location.approved_at is None
                or location.recorded_at > evaluated
                or location.approved_at > evaluated
            ):
                raise ValueError("invalid approved location")
            keys.append((location.location_id, location.revision))
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            raise ValueError("approved locations must be sorted and unique")
        if (
            type(self.has_more_stranded_plans) is not bool
            or type(self.has_more_results) is not bool
        ):
            raise ValueError("invalid truncation flag")
        if self.state == "EMPTY" and (
            self.current_plans or self.stranded_plans or self.latest_results
        ):
            raise ValueError("empty field context has records")
        if self.state == "EMPTY" and (self.has_more_stranded_plans or self.has_more_results):
            raise ValueError("empty field context has truncated history")
        if self.has_more_stranded_plans and len(self.stranded_plans) != 3:
            raise ValueError("stranded truncation flag requires a full partition")
        if self.has_more_results and len(self.latest_results) != 3:
            raise ValueError("result truncation flag requires a full partition")
        object.__setattr__(self, "evaluated_at", evaluated)


@dataclass(frozen=True, slots=True)
class CurrentContextV3:
    base: CurrentContext
    field_work: FieldContext
    field_digest: str

    def __post_init__(self) -> None:
        if type(self.base) is not CurrentContext or type(self.field_work) is not FieldContext:
            raise ValueError("invalid v3 context")
        if (
            self.base.case_id,
            self.base.case_revision,
            self.base.simulated,
            self.base.evaluated_at,
        ) != (
            self.field_work.case_id,
            self.field_work.case_revision,
            self.field_work.simulated,
            self.field_work.evaluated_at,
        ):
            raise ValueError("base and field context differ")
        active = {
            (review.task_id, review.revision): review.status
            for review in self.base.reviews
            if review.status in {"PROPOSED", "APPROVED", "DEFERRED"}
        }
        for item in self.field_work.current_plans:
            key = (item.plan.task_id, item.plan.review_revision)
            if key not in active:
                raise ValueError("current field plan is not bound to an active base review")
        expected = case_records.digest(encode(asdict(self.field_work)))
        if self.field_digest != expected:
            raise ValueError("field context digest mismatch")


@dataclass(frozen=True, slots=True)
class FieldActivityReference:
    selection: str
    plan_id: str
    activity_case_revision: int
    request_id: str
    parent_task_id: str
    parent_revision: int
    parent_status: str
    parent_binding: str
    snapshot_digest: str

    def __post_init__(self) -> None:
        if type(self.selection) is not str or self.selection not in _SELECTIONS:
            raise ValueError("invalid selection")
        _identifier(self.plan_id, "plan_id")
        _positive_revision(self.activity_case_revision, "activity_case_revision")
        _identifier(self.request_id, "request_id")
        _identifier(self.parent_task_id, "parent_task_id")
        _positive_revision(self.parent_revision, "parent_revision")
        if type(self.parent_status) is not str or self.parent_status not in _PARENT_STATUSES:
            raise ValueError("invalid parent_status")
        if type(self.parent_binding) is not str or self.parent_binding not in _BINDINGS:
            raise ValueError("invalid parent_binding")
        if (self.parent_status in ("CANCELLED", "DISMISSED")) != (
            self.parent_binding == "TERMINAL"
        ):
            raise ValueError("parent status and binding disagree")
        _hash(self.snapshot_digest, "snapshot_digest")


@dataclass(frozen=True, slots=True)
class ApprovedLocationReference:
    location_id: str
    revision: int
    entry_digest: str

    def __post_init__(self) -> None:
        _identifier(self.location_id, "location_id")
        _positive_revision(self.revision, "revision")
        _hash(self.entry_digest, "entry_digest")


@dataclass(frozen=True, slots=True)
class ContextReferenceV3:
    schema_version: Literal[3]
    base_reference_json: str
    field_activities: tuple[FieldActivityReference, ...]
    approved_locations: tuple[ApprovedLocationReference, ...]
    field_state: str
    has_more_stranded_plans: bool
    has_more_results: bool
    field_digest: str
    context_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != 3 or type(self.schema_version) is not int:
            raise ValueError("invalid v3 schema")
        if type(self.base_reference_json) is not str:
            raise ValueError("invalid base reference")
        base = decode_reference(self.base_reference_json)
        _tuple(self.field_activities, "field_activities", 8)
        _tuple(self.approved_locations, "approved_locations", 16)
        if any(type(item) is not FieldActivityReference for item in self.field_activities) or any(
            type(item) is not ApprovedLocationReference for item in self.approved_locations
        ):
            raise ValueError("invalid nested reference")
        activity_keys = [item.plan_id for item in self.field_activities]
        if len(set(activity_keys)) != len(activity_keys):
            raise ValueError("duplicate field activity")
        if (
            type(self.has_more_stranded_plans) is not bool
            or type(self.has_more_results) is not bool
        ):
            raise ValueError("invalid truncation flag")
        selected = {
            name: [item for item in self.field_activities if item.selection == name]
            for name in _SELECTIONS
        }
        if (
            len(selected["CURRENT_PLAN"]) > 2
            or len(selected["STRANDED_PLAN"]) > 3
            or len(selected["LATEST_RESULT"]) > 3
        ):
            raise ValueError("field activity partition is too large")
        if self.field_activities != tuple(
            selected["CURRENT_PLAN"] + selected["STRANDED_PLAN"] + selected["LATEST_RESULT"]
        ):
            raise ValueError("field activity partitions are out of order")
        if self.has_more_stranded_plans and len(selected["STRANDED_PLAN"]) != 3:
            raise ValueError("stranded truncation flag is untruthful")
        if self.has_more_results and len(selected["LATEST_RESULT"]) != 3:
            raise ValueError("result truncation flag is untruthful")
        for item in self.field_activities:
            if item.activity_case_revision > base.case_revision:
                raise ValueError("activity is newer than base context")
            if item.selection == "CURRENT_PLAN" and item.parent_binding != "CURRENT":
                raise ValueError("current activity has non-current binding")
            if item.selection == "STRANDED_PLAN" and item.parent_binding not in {
                "SUPERSEDED",
                "TERMINAL",
            }:
                raise ValueError("stranded activity has current binding")
        location_keys = [(item.location_id, item.revision) for item in self.approved_locations]
        if location_keys != sorted(location_keys) or len(set(location_keys)) != len(location_keys):
            raise ValueError("invalid location reference order")
        if (
            type(self.field_state) is not str
            or self.field_state not in {"EMPTY", "PRESENT"}
            or type(self.has_more_stranded_plans) is not bool
            or type(self.has_more_results) is not bool
        ):
            raise ValueError("invalid field reference state")
        if self.field_state == "EMPTY" and (
            self.field_activities or self.has_more_stranded_plans or self.has_more_results
        ):
            raise ValueError("empty field reference carries work")
        _hash(self.field_digest, "field_digest")
        _hash(self.context_digest, "context_digest")
