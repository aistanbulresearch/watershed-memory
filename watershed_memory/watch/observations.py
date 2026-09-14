"""Immutable, hashable records produced by live-source adapters."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

SOURCE_ERROR_CODES = frozenset(
    {"SCHEMA", "HTTP", "TRANSPORT", "PAGINATION", "BOUNDS", "DATA_CONFLICT"}
)


def bounded_source_text(value: object, *, allow_empty: bool = False) -> bool:
    """Source text has one shared printable, bounded representation."""
    return (
        type(value) is str
        and len(value) <= 128
        and (bool(value) or allow_empty)
        and (value.isprintable() or value == "")
    )


class SourceError(Exception):
    def __init__(self, message: str, code: str = "SCHEMA", retry_after_seconds: int | None = None):
        if type(code) is not str or code not in SOURCE_ERROR_CODES:
            raise ValueError("unknown source error code")
        if retry_after_seconds is not None and (
            type(retry_after_seconds) is not int or not 0 <= retry_after_seconds <= 3600
        ):
            raise ValueError("retry delay must be an integer from zero through 3600")
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


@dataclass(frozen=True)
class SeriesSpec:
    series_id: str
    parameter_code: str
    unit: str
    statistic_id: str | None

    def __post_init__(self):
        for value in (self.series_id, self.parameter_code, self.unit):
            if not bounded_source_text(value) or any(char in value for char in ",|"):
                raise ValueError(
                    "series identifiers and units must be bounded delimiter-free strings"
                )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.series_id):
            raise ValueError("invalid series identifier")
        if not re.fullmatch(r"[0-9]{5}", self.parameter_code):
            raise ValueError("parameter code must contain five digits")
        if self.statistic_id is not None and (
            type(self.statistic_id) is not str or not re.fullmatch(r"[0-9]{5}", self.statistic_id)
        ):
            raise ValueError("statistic code must contain five digits")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal_key(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("decimal must be finite")
    if value == 0:
        return "0"
    sign, digits, exponent = value.as_tuple()
    digits = list(digits)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    prefix = "-" if sign else ""
    return f"{prefix}{''.join(str(d) for d in digits)}e{exponent}"


@dataclass(frozen=True)
class Observation:
    station_id: str
    series_id: str
    parameter_code: str
    statistic_id: str | None
    observed_at: datetime
    value: Decimal | None
    unit: str
    approval_status: str
    qualifier: str | None
    source_modified_at: datetime
    provider_id: str

    def __post_init__(self):
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        object.__setattr__(self, "source_modified_at", _utc(self.source_modified_at))
        if self.value is not None and not isinstance(self.value, Decimal):
            raise TypeError("observation values must be Decimal or None")

    @property
    def identity(self) -> tuple[str, datetime]:
        return self.series_id, self.observed_at

    @property
    def semantic_hash(self) -> str:
        payload = [
            self.station_id,
            self.series_id,
            self.parameter_code,
            self.statistic_id,
            self.observed_at.isoformat(),
            None if self.value is None else _decimal_key(self.value),
            self.unit,
            self.approval_status,
            self.qualifier,
        ]
        encoded = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PageReceipt:
    url: str
    sha256: str
    byte_count: int
    retrieved_at: datetime

    def __post_init__(self):
        object.__setattr__(self, "retrieved_at", _utc(self.retrieved_at))


@dataclass(frozen=True)
class SourceBatch:
    station_id: str
    start: datetime
    end: datetime
    retrieved_at: datetime
    observations: tuple[Observation, ...]
    pages: tuple[PageReceipt, ...]

    def __post_init__(self):
        object.__setattr__(self, "start", _utc(self.start))
        object.__setattr__(self, "end", _utc(self.end))
        object.__setattr__(self, "retrieved_at", _utc(self.retrieved_at))
