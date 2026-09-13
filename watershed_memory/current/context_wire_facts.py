"""Validate derived interval summaries before admitting a remote current context."""

from __future__ import annotations

from datetime import datetime

from .case_types import _identifier
from .context_v3_types import CurrentContextV3
from .fact_types import CoveragePolicy, IntervalFacts
from .fact_validation import bounded_number, count, event_id, utc
from .facts import _layout
from .registry import SourceRegistration, gallinas_registry


def _interval(
    facts: IntervalFacts,
    source: SourceRegistration,
    policy: CoveragePolicy,
    evaluated: datetime,
) -> None:
    _layout(facts)
    for name in ("case_id", "monitor_id", "source_id"):
        _identifier(getattr(facts, name), name)
    if (
        facts.station_id != source.station_id
        or facts.source_id != source.source_id
        or source.scope != "CURRENT_OBSERVATION"
        or facts.policy_id != policy.policy_id
        or not 0 < facts.revision < 2**63
        or facts.supersedes_event_id == facts.event_id
        or type(facts.partial_window) is not bool
    ):
        raise ValueError("wire interval differs from its source profile")
    if facts.supersedes_event_id is not None:
        event_id(facts.supersedes_event_id)
    start, end = utc(facts.interval_start), utc(facts.interval_end)
    if not start < end <= evaluated:
        raise ValueError("wire interval is outside evaluation")
    if tuple((item.series_id, item.parameter_code, item.unit) for item in facts.series) != tuple(
        (spec.series_id, spec.parameter_code, spec.unit) for spec in source.series
    ):
        raise ValueError("wire interval series differ from source registration")
    if not set(policy.required_parameters) <= {item.parameter_code for item in facts.series}:
        raise ValueError("wire interval lacks required series")
    for item in facts.series:
        samples, valid = count(item.sample_count), count(item.valid_count)
        if valid > samples or (samples == 0) != (item.latest_at is None):
            raise ValueError("wire interval counts and timestamps disagree")
        if item.latest_at is not None and not start <= utc(item.latest_at) < end:
            raise ValueError("wire sample is outside its interval")
        latest, minimum, maximum = item.latest_value, item.min_value, item.max_value
        for value in (latest, minimum, maximum):
            if value is not None:
                bounded_number(value)
        if valid == 0 and any(value is not None for value in (latest, minimum, maximum)):
            raise ValueError("wire interval has values without valid samples")
        if valid > 0 and (minimum is None or maximum is None or minimum > maximum):
            raise ValueError("wire interval lacks consistent numeric bounds")
        if latest is not None and not minimum <= latest <= maximum:
            raise ValueError("wire latest value is outside numeric bounds")
        if samples > 0 and valid == samples and latest is None:
            raise ValueError("wire fully valid interval has no latest value")
    count(sum(item.sample_count for item in facts.series))
    required = tuple(item for item in facts.series if item.parameter_code in policy.required_parameters)
    missing = tuple(sorted(
        item.parameter_code for item in required if item.valid_count < policy.minimum_valid_samples
    ))
    nulls = tuple(sorted(
        item.parameter_code for item in required if item.sample_count > 0 and item.latest_value is None
    ))
    stale = tuple(sorted(
        item.parameter_code for item in required
        if item.latest_at is None or (evaluated - item.latest_at).total_seconds() > policy.freshness_seconds
    ))
    if not missing and not nulls and not facts.partial_window:
        coverage = "SUFFICIENT"
    elif all(item.valid_count == 0 for item in required):
        coverage = "MISSING"
    else:
        coverage = "DEGRADED"
    freshness = "MISSING" if all(item.latest_at is None for item in required) else (
        "STALE" if stale else "FRESH"
    )
    if (
        facts.missing_parameters != missing or facts.null_latest_parameters != nulls
        or facts.stale_parameters != stale or facts.interval_coverage != coverage
        or facts.freshness != freshness
    ):
        raise ValueError("wire interval derived state is inconsistent")


def validate_context_facts(context: CurrentContextV3) -> None:
    """Check internal consistency; trusted request binding is a separate authority."""
    if type(context) is not CurrentContextV3 or context.base.source_health is None:
        raise ValueError("current wire context requires exact source health")
    base = context.base
    if base.registry != gallinas_registry():
        raise ValueError("wire source registry differs from the admitted current profile")
    if not base.simulated and base.current.case_id != base.case_id:
        raise ValueError("operational source and work cases must match")
    if (
        base.current.supersedes_event_id is not None
        and base.current.supersedes_event_id not in {item.event_id for item in base.prior}
    ):
        raise ValueError("wire current correction requires its direct ancestor")
    evaluated = utc(base.evaluated_at)
    for facts in (base.current, *base.prior):
        try:
            source = base.registry.get(facts.source_id)
        except KeyError:
            raise ValueError("wire interval source is unregistered") from None
        _interval(facts, source, base.source_health.policy, evaluated)
    for review in base.reviews:
        if not review.created_at <= review.updated_at <= evaluated:
            raise ValueError("wire review chronology is inconsistent")
