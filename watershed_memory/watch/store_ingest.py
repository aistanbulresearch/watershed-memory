"""Apply attributed observation revisions within the caller's transaction."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from .observations import SourceBatch, SourceError
from .store_events import mark_dirty
from .store_validation import decimal_text, iso, record_dict

if TYPE_CHECKING:
    from .store import MonitorConfig


def ingest(db: sqlite3.Connection, config: MonitorConfig, batch: SourceBatch) -> tuple[int, int]:
    receipt = db.execute(
        "INSERT INTO poll_receipts(monitor_id,retrieved_at,start,end,pages_json) VALUES(?,?,?,?,?)",
        (
            config.monitor_id,
            iso(batch.retrieved_at),
            iso(batch.start),
            iso(batch.end),
            json.dumps([record_dict(page) for page in batch.pages], sort_keys=True),
        ),
    )
    new_count = corrected = 0
    for item in batch.observations:
        identity = (config.monitor_id, item.series_id, iso(item.observed_at))
        modified = iso(item.source_modified_at)
        current = db.execute(
            "SELECT oc.version_id,oc.last_seen_modified_at,oc.last_seen_provider_id,ov.semantic_hash "
            "FROM observations_current oc JOIN observation_versions ov ON ov.version_id=oc.version_id "
            "WHERE oc.monitor_id=? AND oc.series_id=? AND oc.observed_at=?",
            identity,
        ).fetchone()
        if current and current["semantic_hash"] == item.semantic_hash:
            # Publication metadata orders future corrections but is not a measurement change.
            if (modified, item.provider_id) > (
                current["last_seen_modified_at"],
                current["last_seen_provider_id"],
            ):
                db.execute(
                    "UPDATE observations_current SET last_seen_modified_at=?,last_seen_provider_id=?,last_seen_poll_id=? "
                    "WHERE monitor_id=? AND series_id=? AND observed_at=?",
                    (modified, item.provider_id, receipt.lastrowid, *identity),
                )
            continue
        if current and modified <= current["last_seen_modified_at"]:
            raise SourceError(
                "conflicting observation has no newer publication authority", code="DATA_CONFLICT"
            )
        new_count += current is None
        corrected += current is not None
        version = db.execute(
            "INSERT INTO observation_versions(monitor_id,series_id,observed_at,semantic_hash,value,unit,"
            "approval_status,qualifier,source_modified_at,provider_id,poll_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                *identity,
                item.semantic_hash,
                decimal_text(item.value) if item.value is not None else None,
                item.unit,
                item.approval_status,
                item.qualifier,
                modified,
                item.provider_id,
                receipt.lastrowid,
            ),
        )
        db.execute(
            "INSERT INTO observations_current VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(monitor_id,series_id,observed_at) DO UPDATE SET "
            "version_id=excluded.version_id,last_seen_modified_at=excluded.last_seen_modified_at,"
            "last_seen_provider_id=excluded.last_seen_provider_id,last_seen_poll_id=excluded.last_seen_poll_id",
            (*identity, version.lastrowid, modified, item.provider_id, receipt.lastrowid),
        )
        db.execute(
            "UPDATE series_cursors SET cursor=MAX(cursor,?) WHERE monitor_id=? AND series_id=?",
            (iso(item.observed_at), config.monitor_id, item.series_id),
        )
        mark_dirty(db, config, item.observed_at)
    return new_count, corrected
