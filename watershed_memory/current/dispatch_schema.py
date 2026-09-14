"""Strict dispatch extension; source and work schemas remain unchanged."""

import sqlite3

from .delivery_schema import ensure_schema as ensure_delivery_schema

SCHEMA = {
    "dispatch_schema_version": """CREATE TABLE dispatch_schema_version (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
    )""",
    "dispatch_settings": """CREATE TABLE dispatch_settings (
        case_id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL,
        policy_json TEXT NOT NULL CHECK(length(policy_json)<=8192),
        policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),
        profile_json TEXT NOT NULL CHECK(length(profile_json)<=2048),
        activated_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        initial_event_id TEXT,
        history_count INTEGER NOT NULL DEFAULT 0 CHECK(history_count BETWEEN 0 AND 10000),
        suppressed_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(suppressed_count)='integer' AND suppressed_count>=0),
        assessment_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(assessment_count)='integer' AND assessment_count>=0),
        numeric_context_json TEXT CHECK(length(numeric_context_json)<=8192),
        health_context_json TEXT CHECK(length(health_context_json)<=8192),
        active_attempt_id TEXT,
        UNIQUE(case_id,monitor_id),
        FOREIGN KEY(case_id,monitor_id) REFERENCES current_cases(case_id,monitor_id),
        FOREIGN KEY(initial_event_id,monitor_id) REFERENCES watch_events(event_id,monitor_id),
        FOREIGN KEY(case_id,active_attempt_id) REFERENCES dispatch_attempts(case_id,attempt_id),
        CHECK((numeric_context_json IS NULL)=(health_context_json IS NULL)),
        CHECK((assessment_count=0)=(numeric_context_json IS NULL))
    )""",
    "dispatch_attempts": """CREATE TABLE dispatch_attempts (
        attempt_id TEXT PRIMARY KEY REFERENCES delivery_attempts(attempt_id),
        case_id TEXT NOT NULL REFERENCES dispatch_settings(case_id),
        replay_json TEXT NOT NULL CHECK(length(replay_json)<=32768),
        attention_json TEXT NOT NULL CHECK(length(attention_json)<=4096),
        move_numeric INTEGER NOT NULL CHECK(move_numeric IN (0,1)),
        UNIQUE(case_id,attempt_id)
    )""",
    "dispatch_source_outcomes": """CREATE TABLE dispatch_source_outcomes (
        case_id TEXT NOT NULL, monitor_id TEXT NOT NULL, event_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('HISTORY','SUPPRESSED','ASSESSED')),
        recorded_at TEXT NOT NULL,
        replay_json TEXT CHECK(length(replay_json)<=32768),
        attention_json TEXT CHECK(length(attention_json)<=4096), attempt_id TEXT,
        PRIMARY KEY(case_id,event_id),
        FOREIGN KEY(case_id,monitor_id) REFERENCES dispatch_settings(case_id,monitor_id),
        FOREIGN KEY(event_id,monitor_id) REFERENCES watch_events(event_id,monitor_id),
        FOREIGN KEY(case_id,attempt_id) REFERENCES dispatch_attempts(case_id,attempt_id),
        CHECK((kind='HISTORY' AND replay_json IS NULL AND attention_json IS NULL AND attempt_id IS NULL)
          OR (kind='SUPPRESSED' AND replay_json IS NOT NULL AND attention_json IS NOT NULL AND attempt_id IS NULL)
          OR (kind='ASSESSED' AND replay_json IS NOT NULL AND attention_json IS NOT NULL AND attempt_id IS NOT NULL))
    )""",
    "dispatch_outcomes_recent": """CREATE INDEX dispatch_outcomes_recent
        ON dispatch_source_outcomes(case_id,recorded_at DESC,event_id)""",
    "dispatch_due": """CREATE TABLE dispatch_due (
        case_id TEXT NOT NULL REFERENCES dispatch_settings(case_id),
        due_key TEXT NOT NULL CHECK(length(due_key)=64), task_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>0),
        next_check_at TEXT NOT NULL, handled_at TEXT NOT NULL, attempt_id TEXT NOT NULL,
        PRIMARY KEY(case_id,due_key),
        FOREIGN KEY(case_id,task_id,revision) REFERENCES current_review_revisions(case_id,task_id,revision),
        FOREIGN KEY(case_id,attempt_id) REFERENCES dispatch_attempts(case_id,attempt_id)
    )""",
}


def ensure_schema(db: sqlite3.Connection, *, create: bool = False) -> None:
    if not db.in_transaction or type(create) is not bool:
        raise ValueError("dispatch schema requires an explicit transaction")
    ensure_delivery_schema(db)
    found = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'dispatch_*'"))
    if not found and create:
        for statement in SCHEMA.values():
            db.execute(statement)
        db.execute("INSERT INTO dispatch_schema_version VALUES(1,1)")
        return
    if set(found) != set(SCHEMA) or any(
        " ".join((found[name] or "").split()) != " ".join(sql.split())
        for name, sql in SCHEMA.items()
    ):
        raise ValueError("missing, partial or changed dispatch schema")
    if [tuple(r) for r in db.execute("SELECT * FROM dispatch_schema_version")] != [(1, 1)]:
        raise ValueError("unsupported dispatch schema version")
