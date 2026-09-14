"""Durable current observations, poll leases and an atomic event outbox."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .observations import SeriesSpec, SourceBatch, SourceError
from .store_events import seal_events
from .store_ingest import ingest
from .store_schema import APPLICATION_ID, SCHEMA, SCHEMA_VERSION
from .store_validation import iso, utc, validate_batch
from .usgs import GALLINAS_SERIES, USGSClient


@dataclass(frozen=True)
class MonitorConfig:
    monitor_id: str
    station_id: str
    case_id: str
    start_at: datetime
    series: tuple[SeriesSpec, ...] = GALLINAS_SERIES
    policy_version: str = "watch-v1"
    interval_seconds: int = 900
    poll_seconds: int = 300
    overlap_seconds: int = 1800
    lease_seconds: int = 300

    def __post_init__(self) -> None:
        for name in ("monitor_id", "station_id", "case_id", "policy_version"):
            value = getattr(self, name)
            if type(value) is not str or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value
            ):
                raise ValueError(f"invalid {name}")
        utc(self.start_at)
        if (
            type(self.series) is not tuple
            or not self.series
            or any(type(s) is not SeriesSpec for s in self.series)
        ):
            raise ValueError("series must be a non-empty tuple of SeriesSpec values")
        # Reuse the source's configuration checks without performing a request.
        USGSClient(station_id=self.station_id, specs=self.series)
        for value in (
            self.interval_seconds,
            self.poll_seconds,
            self.overlap_seconds,
            self.lease_seconds,
        ):
            if type(value) is not int or not 1 <= value <= 86400:
                raise ValueError("monitor timing values are out of bounds")
        if self.case_id == "GALLINAS-HPCC-2022" or self.overlap_seconds >= 86400:
            raise ValueError("monitor configuration is outside current-watch bounds")


@dataclass(frozen=True)
class PollLease:
    monitor_id: str
    token: str
    start: datetime
    end: datetime
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class PollResult:
    outcome: str
    new_observations: int
    corrected_observations: int
    event_ids: tuple[str, ...]


@dataclass(frozen=True)
class WatchEvent:
    event_id: str
    monitor_id: str
    case_id: str
    start: datetime
    end: datetime
    revision: int
    supersedes_event_id: str | None
    payload: dict[str, Any]


class LeaseLost(RuntimeError):
    """The caller no longer owns the exact persisted poll scope."""


class _Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class WatchStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            populated = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            if app_id == 0 and version == 0 and not populated:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif app_id != APPLICATION_ID or version != SCHEMA_VERSION:
                raise ValueError("database is not a supported current-watch store")
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, factory=_Connection)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
        except Exception:
            db.close()
            raise
        return db

    @staticmethod
    def _config_json(config: MonitorConfig) -> str:
        values = dict(config.__dict__)
        values["start_at"] = iso(config.start_at)
        values["series"] = [spec.__dict__ for spec in config.series]
        return json.dumps(values, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _config(row: sqlite3.Row) -> MonitorConfig:
        raw = json.loads(row["config_json"])
        raw["start_at"] = datetime.fromisoformat(raw["start_at"])
        raw["series"] = tuple(SeriesSpec(**item) for item in raw["series"])
        return MonitorConfig(**raw)

    @staticmethod
    def _monitor(db: sqlite3.Connection, monitor_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM monitors WHERE monitor_id=?", (monitor_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown monitor: {monitor_id}")
        return row

    def get_config(self, monitor_id: str) -> MonitorConfig:
        with self._connect() as db:
            return self._config(self._monitor(db, monitor_id))

    def register(self, config: MonitorConfig, *, now: datetime) -> None:
        utc(now)
        if type(config) is not MonitorConfig:
            raise ValueError("expected a monitor configuration")
        encoded = self._config_json(config)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT config_json FROM monitors WHERE monitor_id=?", (config.monitor_id,)
            ).fetchone()
            if row:
                if row[0] != encoded:
                    raise ValueError("monitor configuration cannot be changed")
                return
            start = iso(config.start_at)
            db.execute(
                "INSERT INTO monitors(monitor_id,config_json,station_id,case_id,start_at,fetched_through,next_poll_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    config.monitor_id,
                    encoded,
                    config.station_id,
                    config.case_id,
                    start,
                    start,
                    start,
                ),
            )
            db.executemany(
                "INSERT INTO series_cursors VALUES(?,?,?)",
                [(config.monitor_id, spec.series_id, start) for spec in config.series],
            )

    def acquire_poll(self, monitor_id: str, *, now: datetime) -> PollLease | None:
        now = utc(now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._monitor(db, monitor_id)
            existing = db.execute(
                "SELECT expires_at FROM leases WHERE monitor_id=?", (monitor_id,)
            ).fetchone()
            if existing and datetime.fromisoformat(existing[0]) > now:
                return None
            if datetime.fromisoformat(row["next_poll_at"]) > now:
                return None
            config = self._config(row)
            fetched = datetime.fromisoformat(row["fetched_through"])
            start = max(utc(config.start_at), fetched - timedelta(seconds=config.overlap_seconds))
            end = min(now, start + timedelta(hours=24))
            if end <= start:
                return None
            lease = PollLease(
                monitor_id,
                uuid.uuid4().hex,
                start,
                end,
                now,
                now + timedelta(seconds=config.lease_seconds),
            )
            db.execute(
                "INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?)",
                (monitor_id, lease.token, iso(start), iso(end), iso(now), iso(lease.expires_at)),
            )
            return lease

    def _guard(self, db: sqlite3.Connection, lease: PollLease, now: datetime) -> sqlite3.Row:
        if type(lease) is not PollLease:
            raise LeaseLost("invalid poll lease")
        persisted = db.execute(
            "SELECT * FROM leases WHERE monitor_id=? AND token=?", (lease.monitor_id, lease.token)
        ).fetchone()
        if persisted is None or not datetime.fromisoformat(
            persisted["acquired_at"]
        ) <= now < datetime.fromisoformat(persisted["expires_at"]):
            raise LeaseLost("poll lease is no longer active")
        for name in ("start", "end", "acquired_at", "expires_at"):
            if iso(getattr(lease, name)) != persisted[name]:
                raise LeaseLost("poll lease scope does not match")
        return self._monitor(db, lease.monitor_id)

    def commit_poll(self, lease: PollLease, batch: SourceBatch, *, now: datetime) -> PollResult:
        now = utc(now)
        if type(lease) is not PollLease:
            raise LeaseLost("invalid poll lease")
        config = self.get_config(lease.monitor_id)
        validate_batch(batch, config, lease, now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._guard(db, lease, now)
            if self._config_json(config) != row["config_json"]:
                raise LeaseLost("monitor configuration changed during validation")
            new_count, corrected = ingest(db, config, batch)
            event_ids = seal_events(db, config, batch.end)
            outcome = (
                "EVENTS_READY"
                if event_ids
                else ("ROWS_BUFFERED" if new_count or corrected else "NO_SOURCE_CHANGE")
            )
            fetched = max(datetime.fromisoformat(row["fetched_through"]), batch.end)
            db.execute(
                "UPDATE monitors SET fetched_through=?,next_poll_at=?,consecutive_failures=0,"
                "last_error_code=NULL,status=?,observation_count=observation_count+?,version_count=version_count+?,"
                "event_count=event_count+?,pending_count=pending_count+? WHERE monitor_id=?",
                (
                    iso(fetched),
                    iso(now + timedelta(seconds=config.poll_seconds)),
                    outcome,
                    new_count,
                    new_count + corrected,
                    len(event_ids),
                    len(event_ids),
                    lease.monitor_id,
                ),
            )
            db.execute(
                "DELETE FROM leases WHERE monitor_id=? AND token=?", (lease.monitor_id, lease.token)
            )
            return PollResult(outcome, new_count, corrected, event_ids)

    def fail_poll(self, lease: PollLease, error: SourceError, *, now: datetime) -> None:
        now = utc(now)
        if not isinstance(error, SourceError):
            raise ValueError("expected a bounded source error")
        # Validate even a mutated exception before persisting its safe fields.
        checked = SourceError(
            "source request failed", code=error.code, retry_after_seconds=error.retry_after_seconds
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._guard(db, lease, now)
            config = self._config(row)
            failures = min(row["consecutive_failures"] + 1, 1000000)
            delay = min(
                3600,
                max(
                    config.poll_seconds * 2 ** min(failures - 1, 12),
                    checked.retry_after_seconds or 0,
                ),
            )
            db.execute(
                "UPDATE monitors SET consecutive_failures=?,last_error_code=?,status='SOURCE_BACKOFF',next_poll_at=? "
                "WHERE monitor_id=?",
                (failures, checked.code, iso(now + timedelta(seconds=delay)), lease.monitor_id),
            )
            db.execute(
                "DELETE FROM leases WHERE monitor_id=? AND token=?", (lease.monitor_id, lease.token)
            )

    def status(self, monitor_id: str) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN")
            row = self._monitor(db, monitor_id)
            fields = (
                "monitor_id",
                "case_id",
                "status",
                "fetched_through",
                "next_poll_at",
                "consecutive_failures",
                "last_error_code",
                "observation_count",
                "version_count",
                "event_count",
                "pending_count",
            )
            result = {name: row[name] for name in fields}
            result["series_cursors"] = dict(
                db.execute(
                    "SELECT series_id,cursor FROM series_cursors WHERE monitor_id=?", (monitor_id,)
                )
            )
            lease = db.execute(
                "SELECT expires_at FROM leases WHERE monitor_id=?", (monitor_id,)
            ).fetchone()
            result["poll_lease_expires_at"] = lease[0] if lease else None
            return result

    def pending_events(self, monitor_id: str, *, limit: int = 100) -> tuple[WatchEvent, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as db:
            self._monitor(db, monitor_id)
            rows = db.execute(
                "SELECT e.* FROM watch_outbox o JOIN watch_events e ON e.event_id=o.event_id "
                "WHERE o.monitor_id=? AND o.status='PENDING' ORDER BY o.interval_start,o.revision LIMIT ?",
                (monitor_id, limit),
            ).fetchall()
            return tuple(
                WatchEvent(
                    row["event_id"],
                    monitor_id,
                    row["case_id"],
                    datetime.fromisoformat(row["interval_start"]),
                    datetime.fromisoformat(row["interval_end"]),
                    row["revision"],
                    row["supersedes_event_id"],
                    json.loads(row["payload_json"]),
                )
                for row in rows
            )
