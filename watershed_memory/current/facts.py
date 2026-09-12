"""Pure current-evidence facts; measurements remain distinct from operational decisions."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, DivisionByZero, InvalidOperation, Overflow

from ..watch.store import MonitorConfig, WatchEvent
from .fact_types import (
    CoveragePolicy,
    IntervalComparison,
    IntervalFacts,
    SeriesChange,
    SeriesFact,
)
from .fact_validation import bounded_number, event_id, utc, validate_event
from .registry import SourceRegistry

__all__ = [
    "CoveragePolicy",
    "IntervalComparison",
    "IntervalFacts",
    "SeriesChange",
    "SeriesFact",
    "compare_intervals",
    "inspect_interval",
]


def inspect_interval(
    event: WatchEvent,
    config: MonitorConfig,
    registry: SourceRegistry,
    policy: CoveragePolicy,
    *,
    evaluated_at: datetime,
) -> IntervalFacts:
    """Describe source coverage and age, without inferring water safety or recovery."""
    validated = validate_event(event, config, registry, policy, evaluated_at)
    required = set(policy.required_parameters)
    series = validated.series
    required_items = tuple(item for item in series if item.parameter_code in required)
    missing = tuple(
        sorted(
            item.parameter_code
            for item in required_items
            if item.valid_count < policy.minimum_valid_samples
        )
    )
    null_latest = tuple(
        sorted(
            item.parameter_code
            for item in required_items
            if item.sample_count > 0 and item.latest_value is None
        )
    )
    evaluated = utc(evaluated_at)
    stale = tuple(
        sorted(
            item.parameter_code
            for item in required_items
            if item.latest_at is None
            or (evaluated - item.latest_at).total_seconds() > policy.freshness_seconds
        )
    )
    partial = validated.coverage_start > validated.start
    if not missing and not null_latest and not partial:
        coverage = "SUFFICIENT"
    elif all(item.valid_count == 0 for item in required_items):
        coverage = "MISSING"
    else:
        coverage = "DEGRADED"
    if all(item.latest_at is None for item in required_items):
        freshness = "MISSING"
    else:
        freshness = "STALE" if stale else "FRESH"
    return IntervalFacts(
        event_id=event.event_id,
        monitor_id=event.monitor_id,
        case_id=event.case_id,
        station_id=validated.station_id,
        source_id=validated.source_id,
        revision=event.revision,
        supersedes_event_id=event.supersedes_event_id,
        interval_start=validated.start,
        interval_end=validated.end,
        policy_id=policy.policy_id,
        interval_coverage=coverage,
        freshness=freshness,
        missing_parameters=missing,
        null_latest_parameters=null_latest,
        stale_parameters=stale,
        partial_window=partial,
        series=series,
    )


def _layout(facts: IntervalFacts) -> dict[str, SeriesFact]:
    """Bound direct callers as well as records returned by inspect_interval."""
    if type(facts) is not IntervalFacts:
        raise ValueError("expected interval facts")
    event_id(facts.event_id)
    if utc(facts.interval_start) >= utc(facts.interval_end):
        raise ValueError("invalid facts interval")
    if type(facts.revision) is not int or facts.revision < 1:
        raise ValueError("invalid facts revision")
    if facts.supersedes_event_id is not None:
        event_id(facts.supersedes_event_id)
    if type(facts.series) is not tuple or not 1 <= len(facts.series) <= 16:
        raise ValueError("expected a bounded series tuple")
    for item in facts.series:
        if type(item) is not SeriesFact:
            raise ValueError("expected series facts")
        for identity in (item.series_id, item.parameter_code, item.unit):
            if type(identity) is not str or not identity or len(identity) > 128:
                raise ValueError("invalid series identity")
        if item.latest_value is not None:
            bounded_number(item.latest_value)
    if len({item.series_id for item in facts.series}) != len(facts.series) or len(
        {item.parameter_code for item in facts.series}
    ) != len(facts.series):
        raise ValueError("duplicate facts series identity")
    return {item.parameter_code: item for item in facts.series}


def _arithmetic(precision: int) -> Context:
    # Set every context setting, so caller rounding, traps and exponent limits cannot leak in.
    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def compare_intervals(current: IntervalFacts, previous: IntervalFacts) -> IntervalComparison:
    """Compare exact source values; ratio is a factor rounded to28 significant digits."""
    current_by, previous_by = _layout(current), _layout(previous)
    if (
        (current.case_id, current.monitor_id, current.station_id, current.source_id)
        != (previous.case_id, previous.monitor_id, previous.station_id, previous.source_id)
        or set(current_by) != set(previous_by)
        or any(
            (current_by[key].unit, current_by[key].series_id)
            != (previous_by[key].unit, previous_by[key].series_id)
            for key in current_by
        )
    ):
        return IntervalComparison(current.event_id, previous.event_id, False, "SOURCE_MISMATCH", ())
    if current.event_id == previous.event_id:
        return IntervalComparison(current.event_id, previous.event_id, False, "SAME_EVENT", ())
    if (current.interval_start, current.interval_end) == (
        previous.interval_start,
        previous.interval_end,
    ):
        if (
            current.supersedes_event_id != previous.event_id
            or current.revision != previous.revision + 1
        ):
            return IntervalComparison(
                current.event_id, previous.event_id, False, "UNLINKED_OR_OVERLAPPING_INTERVALS", ()
            )
        reason = "CORRECTION"
    elif previous.interval_end <= current.interval_start:
        reason = "SUCCESSIVE_INTERVALS"
    else:
        return IntervalComparison(
            current.event_id, previous.event_id, False, "UNLINKED_OR_OVERLAPPING_INTERVALS", ()
        )
    exact, ratios = _arithmetic(160), _arithmetic(28)
    changes = []
    for parameter in sorted(current_by):
        now, before = current_by[parameter], previous_by[parameter]
        if now.latest_value is None or before.latest_value is None:
            changes.append(
                SeriesChange(
                    parameter,
                    now.unit,
                    "NOT_COMPARABLE",
                    before.latest_value,
                    now.latest_value,
                    None,
                    None,
                )
            )
            continue
        delta = exact.subtract(now.latest_value, before.latest_value)
        ratio = (
            None
            if before.latest_value == 0
            else ratios.divide(now.latest_value, before.latest_value)
        )
        changes.append(
            SeriesChange(
                parameter,
                now.unit,
                "COMPARABLE",
                before.latest_value,
                now.latest_value,
                delta,
                ratio,
            )
        )
    return IntervalComparison(current.event_id, previous.event_id, True, reason, tuple(changes))
