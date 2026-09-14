"""Optional, transaction-bound source references for the existing delivery journal."""

import sqlite3

from .delivery_schema import ensure_schema as ensure_delivery_schema

SCHEMA = {
    "health_schema_version": """CREATE TABLE health_schema_version (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
    )""",
    "health_attempts": """CREATE TABLE health_attempts (
        attempt_id TEXT PRIMARY KEY REFERENCES delivery_attempts(attempt_id),
        version_ids_json TEXT NOT NULL CHECK(length(version_ids_json)<=512)
    )""",
}


def ensure_schema(db: sqlite3.Connection, *, create: bool = False) -> None:
    """Create only on explicit reservation; replay never repairs missing state."""
    if not db.in_transaction or type(create) is not bool:
        raise ValueError("health schema requires an explicit transaction")
    ensure_delivery_schema(db)
    found = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'health_*'"))
    if not found and create:
        for statement in SCHEMA.values():
            db.execute(statement)
        db.execute("INSERT INTO health_schema_version VALUES(1,1)")
        return
    if set(found) != set(SCHEMA):
        raise ValueError("missing, partial or unknown source-health schema")
    if any(
        " ".join((found[name] or "").split()) != " ".join(sql.split())
        for name, sql in SCHEMA.items()
    ):
        raise ValueError("source-health schema definition differs")
    if [tuple(row) for row in db.execute("SELECT * FROM health_schema_version")] != [(1, 1)]:
        raise ValueError("unsupported source-health schema version")
