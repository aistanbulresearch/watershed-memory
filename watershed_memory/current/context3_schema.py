"""Strict SQLite extension for field-aware current-delivery attempts."""

from __future__ import annotations

import sqlite3

from .delivery_schema import ensure_schema as ensure_delivery_schema
from .dispatch_schema import ensure_schema as ensure_dispatch_schema
from .field_schema import ensure_schema as ensure_field_schema

VERSION = 1
SCHEMA = {
    "context3_schema_version": """CREATE TABLE context3_schema_version (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
    )""",
    "context3_delivery_case_attempt": """CREATE UNIQUE INDEX context3_delivery_case_attempt
        ON delivery_attempts(case_id,attempt_id)""",
    "context3_field_receipt_identity": """CREATE UNIQUE INDEX context3_field_receipt_identity
        ON field_receipts(case_id,request_id,plan_id,plan_revision)""",
    "context3_attempts": """CREATE TABLE context3_attempts (
        attempt_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
        reference_json TEXT NOT NULL CHECK(length(reference_json)<=32768),
        context_digest64 TEXT NOT NULL CHECK(length(context_digest64)=64),
        field_digest64 TEXT NOT NULL CHECK(length(field_digest64)=64),
        created_at TEXT NOT NULL,
        UNIQUE(case_id,attempt_id),
        FOREIGN KEY(case_id,attempt_id)
          REFERENCES delivery_attempts(case_id,attempt_id)
    )""",
    "context3_agent_proposals": """CREATE TABLE context3_agent_proposals (
        attempt_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, request_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        plan_revision INTEGER NOT NULL CHECK(plan_revision>0),
        UNIQUE(case_id,attempt_id),
        FOREIGN KEY(case_id,attempt_id)
          REFERENCES context3_attempts(case_id,attempt_id),
        FOREIGN KEY(case_id,request_id,plan_id,plan_revision)
          REFERENCES field_receipts(case_id,request_id,plan_id,plan_revision),
        FOREIGN KEY(case_id,plan_id,plan_revision)
          REFERENCES field_plan_revisions(case_id,plan_id,revision)
    )""",
    "context3_dispatch_settings": """CREATE TABLE context3_dispatch_settings (
        case_id TEXT PRIMARY KEY,
        previous_profile_json TEXT NOT NULL CHECK(length(previous_profile_json)<=2048),
        v3_profile_json TEXT NOT NULL CHECK(length(v3_profile_json)<=2048),
        activated_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        context_version INTEGER NOT NULL CHECK(context_version=3),
        FOREIGN KEY(case_id) REFERENCES dispatch_settings(case_id)
    )""",
}


def _normalized(sql: str | None) -> str:
    return " ".join((sql or "").split())


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create or verify the complete extension in the caller's transaction."""
    if not db.in_transaction:
        raise ValueError("context3 schema requires an explicit transaction")

    ensure_field_schema(db)
    ensure_delivery_schema(db)
    ensure_dispatch_schema(db, create=True)

    present = dict(
        db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'context3_*'")
    )
    if not present:
        for statement in SCHEMA.values():
            db.execute(statement)
        db.execute("INSERT INTO context3_schema_version VALUES(1,?)", (VERSION,))
        return

    if set(present) != set(SCHEMA):
        raise ValueError("partial or unknown context3 schema")
    if any(_normalized(present[name]) != _normalized(sql) for name, sql in SCHEMA.items()):
        raise ValueError("context3 schema definition differs")
    rows = db.execute("SELECT singleton,version FROM context3_schema_version").fetchall()
    if [tuple(row) for row in rows] != [(1, VERSION)]:
        raise ValueError("unsupported context3 schema version")
