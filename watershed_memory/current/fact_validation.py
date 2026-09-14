"""Revalidate the exact source-store event contract before producing agent facts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from ..watch.observations import bounded_source_text
from ..watch.store import MonitorConfig, WatchEvent
from .fact_types import CoveragePolicy, SeriesFact
from .registry import SourceRegistry

_NUMBER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "evidence_class",
        "policy_version",
        "station_id",
        "interval_start",
        "interval_end",
        "coverage_start",
        "coverage_end",
        "observation_count",
        "series",
    }
)
_SERIES_KEYS = frozenset(
    {
        "sample_count",
        "valid_count",
        "latest_at",
        "latest_value",
        "min_value",
        "max_value",
        "unit",
        "approval_counts",
        "qualifier_counts",
    }
)


def utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("expected an aware datetime")
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ValueError("datetime falls outside the supported range") from error


def timestamp(value: object) -> datetime:
    if type(value) is not str or not _TIME.fullmatch(value):
        raise ValueError("expected an RFC3339 timestamp")
    return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def bounded_number(value: Decimal) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("expected a finite decimal")
    parts = value.as_tuple()
    if not -32 <= parts.exponent <= 12 or value.adjusted() > 75:
        raise ValueError("decimal magnitude or scale exceeds source bounds")
    digits = list(parts.digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
    # C12 expands a 64-digit source mantissa with exponent <=12 into up to76 digits.
    if len(digits) > 64:
        raise ValueError("decimal precision exceeds source bounds")
    return value


def decimal_value(value: object) -> Decimal | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 128 or not _NUMBER.fullmatch(value):
        raise ValueError("expected a bounded ASCII decimal string")
    try:
        return bounded_number(Decimal(value))
    except InvalidOperation as error:
        raise ValueError("invalid decimal") from error


def event_id(value: object) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid event identity")
    return value


def count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 16000:
        raise ValueError("invalid observation count")
    return value


def _counts(summary: dict, samples: int) -> set[str]:
    approvals, qualifiers = summary["approval_counts"], summary["qualifier_counts"]
    if type(approvals) is not dict or set(approvals) - {"Approved", "Provisional"}:
        raise ValueError("invalid approval counts")
    if any(count(v) == 0 for v in approvals.values()) or sum(approvals.values()) != samples:
        raise ValueError("approval counts do not match samples")
    if type(qualifiers) is not dict or len(qualifiers) > 16:
        raise ValueError("invalid qualifier counts")
    if any(
        not bounded_source_text(k, allow_empty=True) or count(v) == 0 for k, v in qualifiers.items()
    ):
        raise ValueError("invalid qualifier metadata")
    if sum(qualifiers.values()) > samples:
        raise ValueError("qualifier counts exceed samples")
    return set(qualifiers)


@dataclass(frozen=True)
class ValidatedInterval:
    source_id: str
    station_id: str
    start: datetime
    end: datetime
    coverage_start: datetime
    series: tuple[SeriesFact, ...]


def validate_event(
    event: WatchEvent,
    config: MonitorConfig,
    registry: SourceRegistry,
    policy: CoveragePolicy,
    evaluated_at: datetime,
) -> ValidatedInterval:
    if (
        type(event) is not WatchEvent
        or type(config) is not MonitorConfig
        or type(registry) is not SourceRegistry
        or type(policy) is not CoveragePolicy
    ):
        raise ValueError("invalid evidence input types")
    event_id(event.event_id)
    if type(event.revision) is not int or event.revision < 1:
        raise ValueError("invalid event revision")
    if event.supersedes_event_id is not None:
        if event_id(event.supersedes_event_id) == event.event_id:
            raise ValueError("event cannot supersede itself")
    start, end, evaluated = utc(event.start), utc(event.end), utc(evaluated_at)
    width = timedelta(seconds=config.interval_seconds)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if end - start != width or (start - epoch) % width or end > evaluated:
        raise ValueError("event interval differs from its registered profile")
    if (event.monitor_id, event.case_id) != (config.monitor_id, config.case_id):
        raise ValueError("event is outside this monitor and case")
    try:
        source = registry.for_station(config.station_id)
    except KeyError as error:
        raise ValueError("unregistered current station") from error
    if source.series != config.series:
        raise ValueError("monitor series differ from registered source authority")
    if not set(policy.required_parameters) <= {s.parameter_code for s in config.series}:
        raise ValueError("coverage policy requires unconfigured parameters")
    payload = event.payload
    if type(payload) is not dict or set(payload) != _EVENT_KEYS:
        raise ValueError("invalid event payload schema")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["evidence_class"] != "CURRENT_USGS_OBSERVATION"
        or payload["station_id"] != source.station_id
        or payload["policy_version"] != config.policy_version
        or timestamp(payload["interval_start"]) != start
        or timestamp(payload["interval_end"]) != end
    ):
        raise ValueError("event payload differs from its source and interval")
    first, last = timestamp(payload["coverage_start"]), timestamp(payload["coverage_end"])
    if first != max(utc(config.start_at), start) or first >= end or last != end:
        raise ValueError("coverage differs from the registered bootstrap window")
    total = count(payload["observation_count"])
    raw_series = payload["series"]
    if type(raw_series) is not dict or set(raw_series) != {s.series_id for s in config.series}:
        raise ValueError("event series differ from the registered source")
    facts, qualifiers = [], set()
    for spec in config.series:
        summary = raw_series[spec.series_id]
        if (
            type(summary) is not dict
            or set(summary) != _SERIES_KEYS
            or summary["unit"] != spec.unit
        ):
            raise ValueError("invalid series summary")
        samples, valid = count(summary["sample_count"]), count(summary["valid_count"])
        if valid > samples:
            raise ValueError("valid count exceeds sample count")
        latest_at = None if summary["latest_at"] is None else timestamp(summary["latest_at"])
        if (
            (samples == 0) != (latest_at is None)
            or latest_at is not None
            and not first <= latest_at < end
        ):
            raise ValueError("sample count and latest timestamp do not agree")
        latest, minimum, maximum = (
            decimal_value(summary[key]) for key in ("latest_value", "min_value", "max_value")
        )
        if valid == 0 and any(v is not None for v in (latest, minimum, maximum)):
            raise ValueError("no valid samples can supply numeric values")
        if valid > 0 and (minimum is None or maximum is None or minimum > maximum):
            raise ValueError("valid samples require consistent value bounds")
        if latest is not None and not minimum <= latest <= maximum:
            raise ValueError("latest value is outside its summary bounds")
        if samples > 0 and valid == samples and latest is None:
            raise ValueError("fully valid samples cannot have a null latest value")
        qualifiers.update(_counts(summary, samples))
        if len(qualifiers) > 16:
            raise ValueError("event qualifier count exceeds source-store bounds")
        facts.append(
            SeriesFact(
                spec.series_id,
                spec.parameter_code,
                spec.unit,
                samples,
                valid,
                latest_at,
                latest,
                minimum,
                maximum,
            )
        )
    if sum(f.sample_count for f in facts) != total:
        raise ValueError("event total does not match series counts")
    if (
        len(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
        > 16384
    ):
        raise ValueError("event exceeds its source-store size bound")
    return ValidatedInterval(source.source_id, source.station_id, start, end, first, tuple(facts))
