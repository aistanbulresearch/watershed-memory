"""Pure, explicitly configured attention rules for current evidence review."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

from .context import CurrentContext
from .fact_types import IntervalFacts
from .fact_validation import bounded_number, event_id, utc
from .facts import _arithmetic, compare_intervals
from .source_health import SourceHealth

_PARAMETER = re.compile(r"[0-9]{5}\Z", re.ASCII)
_POLICY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", re.ASCII)
_REASONS = (
    "INITIAL_REVIEW",
    "LATE_EVIDENCE",
    "COVERAGE_CHANGED",
    "SOURCE_CORRECTION",
    "MATERIAL_CHANGE",
    "CHECK_DUE",
)


def _finite(value: Decimal, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError(f"invalid {name}")
    try:
        result = bounded_number(value)
    except (ValueError, InvalidOperation) as error:
        raise ValueError(f"invalid {name}") from error
    return result


@dataclass(frozen=True, slots=True)
class AttentionRule:
    parameter_code: str
    unit: str
    metric: str
    minimum_absolute_change: Decimal
    minimum_relative_change: Decimal

    def __post_init__(self) -> None:
        if type(self.parameter_code) is not str or not _PARAMETER.fullmatch(self.parameter_code):
            raise ValueError("invalid parameter_code")
        if (
            type(self.unit) is not str
            or not 1 <= len(self.unit) <= 32
            or not self.unit.isprintable()
        ):
            raise ValueError("invalid unit")
        if type(self.metric) is not str or self.metric not in {"LATEST", "MAX"}:
            raise ValueError("invalid metric")
        absolute = _finite(self.minimum_absolute_change, "minimum_absolute_change")
        relative = _finite(self.minimum_relative_change, "minimum_relative_change")
        if absolute <= 0 or relative < 0 or relative > 1000:
            raise ValueError("invalid attention thresholds")


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, AttentionRule):
        return {
            "parameter_code": value.parameter_code,
            "unit": value.unit,
            "metric": value.metric,
            "minimum_absolute_change": str(value.minimum_absolute_change),
            "minimum_relative_change": str(value.minimum_relative_change),
        }
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AttentionPolicy:
    policy_id: str
    rules: tuple[AttentionRule, ...]

    def __post_init__(self) -> None:
        if type(self.policy_id) is not str or not _POLICY.fullmatch(self.policy_id):
            raise ValueError("invalid policy_id")
        if type(self.rules) is not tuple or not 1 <= len(self.rules) <= 16:
            raise ValueError("rules must be a tuple of one to sixteen items")
        if any(type(rule) is not AttentionRule for rule in self.rules):
            raise ValueError("invalid attention rule")
        if len({rule.parameter_code for rule in self.rules}) != len(self.rules):
            raise ValueError("duplicate attention parameter")

    @property
    def policy_digest(self) -> str:
        payload = {"policy_id": self.policy_id, "rules": _jsonable(self.rules)}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DueCheck:
    task_id: str
    revision: int
    next_check_at: datetime

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", self.task_id, re.ASCII
        ):
            raise ValueError("invalid task_id")
        if (
            type(self.revision) is not int
            or isinstance(self.revision, bool)
            or not 1 <= self.revision < 2**63
        ):
            raise ValueError("invalid revision")
        normalized = utc(self.next_check_at)
        object.__setattr__(self, "next_check_at", normalized)

    @property
    def key(self) -> str:
        value = f"{self.task_id}|{self.revision}|{self.next_check_at.isoformat()}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AttentionResult:
    eligible: bool
    reasons: tuple[str, ...]
    changed_parameters: tuple[str, ...]
    due_checks: tuple[DueCheck, ...]
    policy_digest: str
    basis_event_id: str | None

    def __post_init__(self) -> None:
        if type(self.eligible) is not bool or type(self.reasons) is not tuple:
            raise ValueError("invalid attention result")
        if any(type(reason) is not str for reason in self.reasons):
            raise ValueError("invalid attention reason")
        if len(self.reasons) > len(_REASONS) or len(set(self.reasons)) != len(self.reasons):
            raise ValueError("invalid attention reason")
        if tuple(reason for reason in _REASONS if reason in self.reasons) != self.reasons:
            raise ValueError("invalid attention reason")
        if type(self.changed_parameters) is not tuple or type(self.due_checks) is not tuple:
            raise ValueError("invalid attention result collections")
        if any(
            type(item) is not str or not _PARAMETER.fullmatch(item)
            for item in self.changed_parameters
        ):
            raise ValueError("invalid changed parameter")
        if (
            len(self.changed_parameters) > 16
            or tuple(sorted(set(self.changed_parameters))) != self.changed_parameters
        ):
            raise ValueError("changed parameters must be sorted and unique")
        if ("MATERIAL_CHANGE" in self.reasons) != bool(self.changed_parameters):
            raise ValueError("material change does not match parameters")
        if len(self.due_checks) > 3 or any(type(item) is not DueCheck for item in self.due_checks):
            raise ValueError("due checks are not bounded")
        if len({item.key for item in self.due_checks}) != len(self.due_checks) or len(
            {item.task_id for item in self.due_checks}
        ) != len(self.due_checks):
            raise ValueError("duplicate due check")
        if ("CHECK_DUE" in self.reasons) != bool(self.due_checks):
            raise ValueError("due checks do not match reason")
        if self.eligible != bool(self.reasons):
            raise ValueError("eligible does not match reasons")
        if type(self.policy_digest) is not str or not re.fullmatch(
            r"[0-9a-f]{64}\Z", self.policy_digest, re.ASCII
        ):
            raise ValueError("invalid policy digest")
        if self.basis_event_id is not None:
            event_id(self.basis_event_id)


def _same_layout(current: IntervalFacts, basis: IntervalFacts) -> bool:
    return (
        current.case_id,
        current.monitor_id,
        current.station_id,
        current.source_id,
        current.policy_id,
    ) == (basis.case_id, basis.monitor_id, basis.station_id, basis.source_id, basis.policy_id) and {
        (item.parameter_code, item.series_id, item.unit) for item in current.series
    } == {(item.parameter_code, item.series_id, item.unit) for item in basis.series}


def _metric(item: object, metric: str) -> Decimal | None:
    return item.latest_value if metric == "LATEST" else item.max_value


def evaluate_attention(
    context: CurrentContext,
    policy: AttentionPolicy,
    *,
    basis: IntervalFacts | None = None,
    basis_health: SourceHealth | None = None,
    handled_due_keys: tuple[str, ...] = (),
) -> AttentionResult:
    """Evaluate explicit configured review attention without side effects."""
    if type(context) is not CurrentContext or type(policy) is not AttentionPolicy:
        raise ValueError("invalid attention inputs")
    health = context.source_health
    if basis is not None and type(basis) is not IntervalFacts:
        raise ValueError("invalid attention baseline")
    if health is not None and basis is not None:
        if type(basis_health) is not SourceHealth or (
            basis_health.case_id,
            basis_health.monitor_id,
            basis_health.source_id,
            basis_health.station_id,
            basis_health.policy,
        ) != (
            health.case_id,
            health.monitor_id,
            health.source_id,
            health.station_id,
            health.policy,
        ):
            raise ValueError("attention requires the matching assessed source-health baseline")
        if not basis.interval_end <= basis_health.evaluated_at <= context.evaluated_at:
            raise ValueError("assessed source health is outside the baseline chronology")
    elif basis_health is not None:
        raise ValueError("source-health baseline requires a health-enabled interval baseline")
    if type(handled_due_keys) is not tuple or len(handled_due_keys) > 3:
        raise ValueError("invalid handled due keys")
    if any(
        type(key) is not str or not re.fullmatch(r"[0-9a-f]{64}\Z", key, re.ASCII)
        for key in handled_due_keys
    ):
        raise ValueError("invalid handled due key")
    if len(set(handled_due_keys)) != len(handled_due_keys):
        raise ValueError("duplicate handled due key")
    current = context.current
    if any(
        next((item for item in current.series if item.parameter_code == rule.parameter_code), None)
        is None
        or next(item for item in current.series if item.parameter_code == rule.parameter_code).unit
        != rule.unit
        for rule in policy.rules
    ):
        raise ValueError("attention rule does not match current source facts")
    comparison_basis = basis
    if basis is not None:
        if type(basis) is not IntervalFacts or not _same_layout(current, basis):
            raise ValueError("attention basis does not match current source")
        if basis.interval_end > context.evaluated_at:
            raise ValueError("attention basis is in the future")
        if basis.event_id != current.event_id:
            relation = compare_intervals(current, basis)
            if not relation.comparable:
                ancestor = next(
                    (
                        item
                        for item in context.prior
                        if item.event_id == current.supersedes_event_id
                    ),
                    None,
                )
                linked = any(
                    current.supersedes_event_id in evidence
                    for _, evidence in context.review_evidence
                )
                if (
                    ancestor is None
                    or not _same_layout(current, ancestor)
                    or not linked
                    or current.interval_end > basis.interval_start
                    or basis.interval_end > context.evaluated_at
                ):
                    raise ValueError("attention basis is not chronological or comparable")
                comparison_basis = ancestor
    reasons: list[str] = []
    changed: list[str] = []
    if basis is None:
        reasons.append("INITIAL_REVIEW")
    else:

        def coverage(item: IntervalFacts) -> tuple:
            result = (
                item.missing_parameters,
                item.null_latest_parameters,
                item.interval_coverage,
                item.partial_window,
            )
            return result + ((item.stale_parameters, item.freshness) if health is None else ())

        source_changed = health is not None and (
            health.missing_parameters,
            health.stale_parameters,
            health.null_parameters,
        ) != (
            basis_health.missing_parameters,
            basis_health.stale_parameters,
            basis_health.null_parameters,
        )
        if coverage(current) != coverage(comparison_basis) or source_changed:
            reasons.append("COVERAGE_CHANGED")
        correction = current.supersedes_event_id == basis.event_id
        if not correction:
            for task_id, evidence in context.review_evidence:
                if current.supersedes_event_id in evidence:
                    correction = True
        if correction and current.event_id != basis.event_id:
            reasons.append("SOURCE_CORRECTION")
        for rule in policy.rules:
            now_item = next(
                item for item in current.series if item.parameter_code == rule.parameter_code
            )
            old_item = next(
                item
                for item in comparison_basis.series
                if item.parameter_code == rule.parameter_code
            )
            now_value, old_value = _metric(now_item, rule.metric), _metric(old_item, rule.metric)
            if now_value is None or old_value is None:
                continue
            with localcontext(_arithmetic(160)):
                delta = (now_value - old_value).copy_abs()
                relative = rule.minimum_relative_change * old_value.copy_abs()
            if delta >= rule.minimum_absolute_change and (old_value == 0 or delta >= relative):
                changed.append(rule.parameter_code)
        if changed:
            reasons.append("MATERIAL_CHANGE")
    handled = set(handled_due_keys)
    due: list[DueCheck] = []
    for review in context.reviews:
        if (
            review.status in {"PROPOSED", "APPROVED", "DEFERRED"}
            and review.next_check_at is not None
        ):
            item = DueCheck(review.task_id, review.revision, review.next_check_at)
            if item.next_check_at <= context.evaluated_at and item.key not in handled:
                due.append(item)
    if due:
        reasons.append("CHECK_DUE")
    ordered = tuple(reason for reason in _REASONS if reason in reasons)
    return AttentionResult(
        bool(ordered),
        ordered,
        tuple(sorted(changed)),
        tuple(due),
        policy.policy_digest,
        basis.event_id if basis is not None else None,
    )
