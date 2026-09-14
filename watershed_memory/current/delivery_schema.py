"""Versioned SQLite schema for the current delivery journal."""

from __future__ import annotations

import sqlite3

from ..watch.store_schema import APPLICATION_ID, SCHEMA_VERSION
from .case_schema import ensure_schema as ensure_current_schema
from .delivery_types import InvocationAllowance

VERSION = 1
SCHEMA = {
    "delivery_schema_version": """CREATE TABLE delivery_schema_version (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
    )""",
    "delivery_allowance": """CREATE TABLE delivery_allowance (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        scripted_limit INTEGER NOT NULL CHECK(scripted_limit BETWEEN 0 AND 10000),
        scripted_used INTEGER NOT NULL CHECK(scripted_used BETWEEN 0 AND scripted_limit),
        provider_limit INTEGER NOT NULL CHECK(provider_limit BETWEEN 0 AND 10000),
        provider_used INTEGER NOT NULL CHECK(provider_used BETWEEN 0 AND provider_limit)
    )""",
    "delivery_attempts": """CREATE TABLE delivery_attempts (
        attempt_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, monitor_id TEXT NOT NULL,
        event_id TEXT NOT NULL, request_id TEXT NOT NULL, input_json TEXT NOT NULL CHECK(length(input_json)<=4096),
        input_hash64 TEXT NOT NULL CHECK(length(input_hash64)=64), profile_json TEXT NOT NULL CHECK(length(profile_json)<=2048),
        mode TEXT NOT NULL CHECK(mode IN ('SCRIPTED_SDK','STRANDS_CURRENT')), source_delivery INTEGER NOT NULL CHECK(source_delivery IN (0,1)),
        status TEXT NOT NULL CHECK(status IN ('RESERVED','COMMITTED','FAILED','STALE','ABANDONED')),
        case_revision INTEGER NOT NULL CHECK(case_revision>=0), coverage_digest64 TEXT NOT NULL CHECK(length(coverage_digest64)=64),
        context_digest64 TEXT NOT NULL CHECK(length(context_digest64)=64), prior_ids_json TEXT NOT NULL CHECK(length(prior_ids_json)<=512),
        review_refs_json TEXT NOT NULL CHECK(length(review_refs_json)<=4096),
        evaluated_at TEXT NOT NULL, finished_at TEXT, execution_json TEXT CHECK(execution_json IS NULL OR length(execution_json)<=524288),
        failure_json TEXT CHECK(failure_json IS NULL OR length(failure_json)<=524288), work_json TEXT NOT NULL DEFAULT '[]' CHECK(length(work_json)<=20000), reason TEXT,
        UNIQUE(case_id,request_id), FOREIGN KEY(case_id,monitor_id) REFERENCES current_cases(case_id,monitor_id),
        FOREIGN KEY(event_id,monitor_id) REFERENCES watch_events(event_id,monitor_id)
    )""",
    "delivery_active_case": """CREATE UNIQUE INDEX delivery_active_case ON delivery_attempts(case_id)
        WHERE status IN ('RESERVED','FAILED','STALE')""",
}


def ensure_schema(db: sqlite3.Connection, allowance: InvocationAllowance | None = None) -> None:
    """Create or verify the delivery extension inside the caller's transaction."""
    if allowance is not None and type(allowance) is not InvocationAllowance:
        raise ValueError("invalid invocation allowance")
    if not db.in_transaction:
        raise ValueError("delivery schema requires an active transaction")
    if (
        db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
        or db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
    ):
        raise ValueError("unsupported current-watch database")
    ensure_current_schema(db)
    present = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'delivery_*'"))
    if not present:
        if allowance is None:
            raise ValueError("new delivery schema requires explicit allowance")
        for statement in SCHEMA.values():
            db.execute(statement)
        db.execute("INSERT INTO delivery_schema_version VALUES(1,?)", (VERSION,))
        db.execute(
            "INSERT INTO delivery_allowance VALUES(1,?,?,?,?)",
            (allowance.scripted_attempts, 0, allowance.provider_attempts, 0),
        )
        return
    if set(present) != set(SCHEMA):
        raise ValueError("partial or unknown delivery schema")
    for name, expected in SCHEMA.items():
        if " ".join((present[name] or "").split()) != " ".join(expected.split()):
            raise ValueError("delivery schema definition differs")
    if [
        tuple(row) for row in db.execute("SELECT singleton,version FROM delivery_schema_version")
    ] != [(1, VERSION)]:
        raise ValueError("unsupported delivery schema version")
    row = db.execute(
        "SELECT scripted_limit,provider_limit FROM delivery_allowance WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise ValueError("missing delivery allowance")
    if allowance is not None and (row[0], row[1]) != (
        allowance.scripted_attempts,
        allowance.provider_attempts,
    ):
        raise ValueError("delivery allowance differs")
