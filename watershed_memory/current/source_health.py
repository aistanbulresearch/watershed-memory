"""Read-only latest source observations, independent of queued interval age."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from ..watch.observations import Observation, SeriesSpec, bounded_source_text
from ..watch.store import WatchStore
from . import case_records as rows
from .case_store import CaseStore
from .case_types import _identifier
from .context import _case_config
from .fact_types import CoveragePolicy
from .fact_validation import bounded_number, decimal_value, event_id, timestamp, utc
from .registry import gallinas_registry


@dataclass(frozen=True, slots=True)
class LatestReading:
    series_id: str
    parameter_code: str
    unit: str
    version_id: int | None
    semantic_hash: str | None
    observed_at: datetime | None
    value: Decimal | None
    approval_status: str | None
    qualifier: str | None
    source_modified_at: datetime | None
    retrieved_at: datetime | None

    def __post_init__(self):
        SeriesSpec(self.series_id, self.parameter_code, self.unit, None)
        if self.version_id is None:
            if any(
                value is not None
                for value in (
                    self.semantic_hash,
                    self.observed_at,
                    self.value,
                    self.approval_status,
                    self.qualifier,
                    self.source_modified_at,
                    self.retrieved_at,
                )
            ):
                raise ValueError("missing reading contains data")
            return
        if type(self.version_id) is not int or not 0 < self.version_id < 2**63:
            raise ValueError("invalid source version")
        event_id(self.semantic_hash)
        if self.value is not None:
            bounded_number(self.value)
        if self.approval_status not in ("Approved", "Provisional"):
            raise ValueError("invalid source approval")
        if self.qualifier is not None and not bounded_source_text(self.qualifier, allow_empty=True):
            raise ValueError("invalid source qualifier")
        for name in ("observed_at", "source_modified_at", "retrieved_at"):
            object.__setattr__(self, name, utc(getattr(self, name)))
        if (
            self.observed_at > self.retrieved_at
            or self.source_modified_at - self.retrieved_at > timedelta(seconds=60)
        ):
            raise ValueError("invalid reading chronology")


@dataclass(frozen=True, slots=True)
class SourceHealth:
    case_id: str
    monitor_id: str
    source_id: str
    station_id: str
    evaluated_at: datetime
    policy: CoveragePolicy
    series: tuple[LatestReading, ...]

    def __post_init__(self):
        _identifier(self.case_id, "case_id")
        _identifier(self.monitor_id, "monitor_id")
        _identifier(self.source_id, "source_id")
        evaluated = utc(self.evaluated_at)
        if (
            type(self.policy) is not CoveragePolicy
            or type(self.series) is not tuple
            or not 1 <= len(self.series) <= 16
        ):
            raise ValueError("invalid source health")
        if any(type(item) is not LatestReading for item in self.series):
            raise ValueError("invalid latest reading type")
        try:
            source = gallinas_registry().get(self.source_id)
        except KeyError as error:
            raise ValueError("unknown source health identity") from error
        if self.station_id != source.station_id or tuple(
            (item.series_id, item.parameter_code, item.unit) for item in self.series
        ) != tuple((spec.series_id, spec.parameter_code, spec.unit) for spec in source.series):
            raise ValueError("source health differs from registered layout")
        required = set(self.policy.required_parameters)
        if not required <= {item.parameter_code for item in self.series}:
            raise ValueError("required source series are absent")
        for item, spec in zip(self.series, source.series, strict=True):
            if item.version_id is not None:
                if (
                    item.observed_at > item.retrieved_at
                    or item.observed_at > evaluated
                    or item.retrieved_at > evaluated
                ):
                    raise ValueError("reading is outside evaluation")
                observation = Observation(
                    self.station_id,
                    spec.series_id,
                    spec.parameter_code,
                    spec.statistic_id,
                    item.observed_at,
                    item.value,
                    item.unit,
                    item.approval_status,
                    item.qualifier,
                    item.source_modified_at,
                    "snapshot",
                )
                # Publication identity is intentionally absent from the semantic hash.
                if observation.semantic_hash != item.semantic_hash:
                    raise ValueError("reading semantic hash differs")
        object.__setattr__(self, "evaluated_at", evaluated)

    @property
    def version_ids(self) -> tuple[int | None, ...]:
        return tuple(item.version_id for item in self.series)

    @property
    def missing_parameters(self) -> tuple[str, ...]:
        missing = {item.parameter_code for item in self.series if item.version_id is None}
        return tuple(key for key in self.policy.required_parameters if key in missing)

    @property
    def stale_parameters(self) -> tuple[str, ...]:
        stale = {
            item.parameter_code
            for item in self.series
            if item.observed_at is not None
            and self.evaluated_at - item.observed_at
            > timedelta(seconds=self.policy.freshness_seconds)
        }
        return tuple(key for key in self.policy.required_parameters if key in stale)

    @property
    def null_parameters(self) -> tuple[str, ...]:
        missing = {
            item.parameter_code
            for item in self.series
            if item.version_id is not None and item.value is None
        }
        return tuple(key for key in self.policy.required_parameters if key in missing)


def _stored_time(raw: object) -> datetime:
    result = timestamp(raw)
    if result.isoformat() != raw:
        raise ValueError("stored timestamp is not canonical UTC")
    return result


def _load_source_health(
    db: sqlite3.Connection, case_id: str, *, evaluated_at: datetime, version_ids=None
) -> SourceHealth:
    if not db.in_transaction:
        raise ValueError("source health requires an active transaction")
    evaluated = utc(evaluated_at)
    _identifier(case_id, "case_id")
    case = rows.case_row(db, case_id)
    config = _case_config(case)
    monitor_row = WatchStore._monitor(db, config.monitor_id)
    monitor = WatchStore._config(monitor_row)
    registry = gallinas_registry()
    source = registry.for_station(monitor.station_id)
    if (
        monitor.station_id != source.station_id
        or monitor_row["station_id"] != source.station_id
        or _stored_time(monitor_row["start_at"]) != monitor.start_at
        or case["monitor_id"] != monitor.monitor_id
        or config.case_id != case_id
        or monitor.monitor_id != monitor_row["monitor_id"]
        or monitor.case_id != monitor_row["case_id"]
        or (not config.simulated and case_id != monitor.case_id)
        or monitor.series != source.series
        or WatchStore._config_json(monitor) != monitor_row["config_json"]
    ):
        raise ValueError("stored source configuration differs")
    if evaluated < _stored_time(case["created_at"]):
        raise ValueError("evaluation precedes case creation")
    if version_ids is not None:
        if type(version_ids) is not tuple or len(version_ids) != len(source.series):
            raise ValueError("version_ids must match configured series")
        if any(
            value is not None
            and (type(value) is not int or isinstance(value, bool) or not 0 < value < 2**63)
            for value in version_ids
        ):
            raise ValueError("invalid version reference")
        if len({value for value in version_ids if value is not None}) != len(
            [value for value in version_ids if value is not None]
        ):
            raise ValueError("duplicate version reference")
    readings = []
    for index, spec in enumerate(source.series):
        current = None
        if version_ids is None:
            current = db.execute(
                "SELECT version_id,observed_at FROM observations_current WHERE monitor_id=? AND series_id=? ORDER BY observed_at DESC LIMIT 1",
                (monitor.monitor_id, spec.series_id),
            ).fetchone()
            selected = current[0] if current else None
        else:
            selected = version_ids[index]
        if selected is None:
            readings.append(
                LatestReading(
                    spec.series_id,
                    spec.parameter_code,
                    spec.unit,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
            continue
        row = db.execute(
            "SELECT v.*,p.retrieved_at,p.start,p.end FROM observation_versions v JOIN poll_receipts p ON p.poll_id=v.poll_id AND p.monitor_id=v.monitor_id WHERE v.version_id=? AND v.monitor_id=? AND v.series_id=?",
            (selected, monitor.monitor_id, spec.series_id),
        ).fetchone()
        if row is None:
            raise KeyError("unknown source version")
        if current is not None and current["observed_at"] != row["observed_at"]:
            raise ValueError("latest pointer differs from its immutable version identity")
        observed, modified, retrieved, start, end = map(
            _stored_time,
            (
                row["observed_at"],
                row["source_modified_at"],
                row["retrieved_at"],
                row["start"],
                row["end"],
            ),
        )
        if (
            observed > retrieved
            or observed < start
            or observed > end
            or end > retrieved
            or modified - retrieved > timedelta(seconds=60)
            or observed > evaluated
            or retrieved > evaluated
            or row["unit"] != spec.unit
        ):
            raise ValueError("invalid source provenance")
        if not bounded_source_text(row["provider_id"]) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]+", row["provider_id"]
        ):
            raise ValueError("invalid source provider identity")
        value = decimal_value(row["value"])
        observation = Observation(
            source.station_id,
            spec.series_id,
            spec.parameter_code,
            spec.statistic_id,
            observed,
            value,
            row["unit"],
            row["approval_status"],
            row["qualifier"],
            modified,
            row["provider_id"],
        )
        if observation.semantic_hash != row["semantic_hash"]:
            raise ValueError("source semantic hash differs")
        readings.append(
            LatestReading(
                spec.series_id,
                spec.parameter_code,
                spec.unit,
                row["version_id"],
                row["semantic_hash"],
                observed,
                value,
                row["approval_status"],
                row["qualifier"],
                modified,
                retrieved,
            )
        )
    return SourceHealth(
        case_id,
        monitor.monitor_id,
        source.source_id,
        source.station_id,
        evaluated,
        config.coverage_policy,
        tuple(readings),
    )


def load_source_health(
    store: CaseStore,
    case_id: str,
    *,
    evaluated_at: datetime,
    version_ids: tuple[int | None, ...] | None = None,
) -> SourceHealth:
    if type(store) is not CaseStore:
        raise ValueError("expected a current case store")
    with store._connect() as db:
        db.execute("BEGIN")
        return _load_source_health(db, case_id, evaluated_at=evaluated_at, version_ids=version_ids)
