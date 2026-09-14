"""Seal only changed, closed intervals into immutable evidence events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .store_validation import decimal_text, iso

if TYPE_CHECKING:
    from .store import MonitorConfig


def mark_dirty(db: sqlite3.Connection, config: MonitorConfig, observed: datetime) -> None:
    width = timedelta(seconds=config.interval_seconds)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    start = epoch + ((observed - epoch) // width) * width
    db.execute(
        "INSERT OR IGNORE INTO dirty_intervals VALUES(?,?,?)",
        (config.monitor_id, iso(start), iso(start + width)),
    )


def _payload(
    config: MonitorConfig, start: datetime, end: datetime, members: list[sqlite3.Row]
) -> dict[str, Any]:
    qualifiers = {row["qualifier"] for row in members if row["qualifier"] is not None}
    if len(qualifiers) > 16:
        raise ValueError("event qualifier bound exceeded")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "evidence_class": "CURRENT_USGS_OBSERVATION",
        "policy_version": config.policy_version,
        "station_id": config.station_id,
        "interval_start": iso(start),
        "interval_end": iso(end),
        "coverage_start": iso(max(config.start_at, start)),
        "coverage_end": iso(end),
        "observation_count": len(members),
        "series": {},
    }
    for spec in config.series:
        group = [row for row in members if row["series_id"] == spec.series_id]
        values = [Decimal(row["value"]) for row in group if row["value"] is not None]
        latest = max(group, key=lambda row: row["observed_at"]) if group else None
        payload["series"][spec.series_id] = {
            "sample_count": len(group),
            "valid_count": len(values),
            "latest_at": latest["observed_at"] if latest else None,
            "latest_value": latest["value"] if latest else None,
            "min_value": decimal_text(min(values)) if values else None,
            "max_value": decimal_text(max(values)) if values else None,
            "unit": spec.unit,
            "approval_counts": dict(Counter(row["approval_status"] for row in group)),
            "qualifier_counts": dict(
                Counter(row["qualifier"] for row in group if row["qualifier"] is not None)
            ),
        }
    return payload


def seal_events(
    db: sqlite3.Connection, config: MonitorConfig, through: datetime
) -> tuple[str, ...]:
    """Dirty intervals survive restarts and allow an empty later poll to seal work."""
    dirty = db.execute(
        "SELECT interval_start,interval_end FROM dirty_intervals "
        "WHERE monitor_id=? AND interval_end<=? ORDER BY interval_start",
        (config.monitor_id, iso(through)),
    ).fetchall()
    event_ids = []
    for interval in dirty:
        start_text, end_text = interval["interval_start"], interval["interval_end"]
        members = db.execute(
            "SELECT oc.series_id,oc.observed_at,oc.version_id,ov.value,ov.unit,"
            "ov.approval_status,ov.qualifier,ov.semantic_hash "
            "FROM observations_current oc JOIN observation_versions ov "
            "ON ov.version_id=oc.version_id WHERE oc.monitor_id=? "
            "AND oc.observed_at >= ? AND oc.observed_at < ? "
            "ORDER BY oc.observed_at,oc.series_id",
            (config.monitor_id, start_text, end_text),
        ).fetchall()
        if members:
            digest = hashlib.sha256(
                json.dumps(
                    sorted(
                        (
                            row["version_id"],
                            row["series_id"],
                            row["observed_at"],
                            row["semantic_hash"],
                        )
                        for row in members
                    ),
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            prior = db.execute(
                "SELECT event_id,revision,member_digest FROM watch_events "
                "WHERE monitor_id=? AND interval_start=? ORDER BY revision DESC LIMIT 1",
                (config.monitor_id, start_text),
            ).fetchone()
            if prior is None or prior["member_digest"] != digest:
                revision = 1 if prior is None else prior["revision"] + 1
                identity = [config.monitor_id, config.policy_version, start_text, revision, digest]
                event_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                payload = _payload(
                    config,
                    datetime.fromisoformat(start_text),
                    datetime.fromisoformat(end_text),
                    members,
                )
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                if len(encoded.encode()) > 16384:
                    raise ValueError("event payload exceeds bound")
                db.execute(
                    "INSERT INTO watch_events VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        event_id,
                        config.monitor_id,
                        config.case_id,
                        start_text,
                        end_text,
                        revision,
                        prior["event_id"] if prior else None,
                        encoded,
                        digest,
                    ),
                )
                db.executemany(
                    "INSERT INTO event_members VALUES(?,?,?,?,?)",
                    [
                        (
                            event_id,
                            config.monitor_id,
                            row["version_id"],
                            row["series_id"],
                            row["observed_at"],
                        )
                        for row in members
                    ],
                )
                db.execute(
                    "INSERT INTO watch_outbox(event_id,monitor_id,interval_start,revision) VALUES(?,?,?,?)",
                    (event_id, config.monitor_id, start_text, revision),
                )
                event_ids.append(event_id)
        db.execute(
            "DELETE FROM dirty_intervals WHERE monitor_id=? AND interval_start=?",
            (config.monitor_id, start_text),
        )
    return tuple(event_ids)
