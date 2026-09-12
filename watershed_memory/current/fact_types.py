"""Immutable records for attributed current evidence and derived comparisons."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class CoveragePolicy:
    policy_id: str
    required_parameters: tuple[str, ...]
    minimum_valid_samples: int = 1
    freshness_seconds: int = 3600

    def __post_init__(self):
        if type(self.policy_id) is not str or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", self.policy_id
        ):
            raise ValueError("invalid policy_id")
        if (
            type(self.required_parameters) is not tuple
            or not self.required_parameters
            or len(self.required_parameters) > 16
            or any(
                type(item) is not str or not re.fullmatch(r"[0-9]{5}", item)
                for item in self.required_parameters
            )
            or len(set(self.required_parameters)) != len(self.required_parameters)
        ):
            raise ValueError("invalid required parameters")
        if (
            type(self.minimum_valid_samples) is not int
            or not 1 <= self.minimum_valid_samples <= 10000
        ):
            raise ValueError("invalid sample minimum")
        if type(self.freshness_seconds) is not int or not 1 <= self.freshness_seconds <= 86400:
            raise ValueError("invalid freshness bound")


@dataclass(frozen=True)
class SeriesFact:
    series_id: str
    parameter_code: str
    unit: str
    sample_count: int
    valid_count: int
    latest_at: datetime | None
    latest_value: Decimal | None
    min_value: Decimal | None
    max_value: Decimal | None


@dataclass(frozen=True)
class IntervalFacts:
    event_id: str
    monitor_id: str
    case_id: str
    station_id: str
    source_id: str
    revision: int
    supersedes_event_id: str | None
    interval_start: datetime
    interval_end: datetime
    policy_id: str
    interval_coverage: str
    freshness: str
    missing_parameters: tuple[str, ...]
    null_latest_parameters: tuple[str, ...]
    stale_parameters: tuple[str, ...]
    partial_window: bool
    series: tuple[SeriesFact, ...]


@dataclass(frozen=True)
class SeriesChange:
    parameter_code: str
    unit: str
    status: str
    previous_value: Decimal | None
    current_value: Decimal | None
    signed_delta: Decimal | None
    ratio: Decimal | None


@dataclass(frozen=True)
class IntervalComparison:
    current_event_id: str
    previous_event_id: str
    comparable: bool
    reason: str
    changes: tuple[SeriesChange, ...]
    ratio_precision: int = 28
