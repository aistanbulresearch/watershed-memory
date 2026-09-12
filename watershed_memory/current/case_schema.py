"""Versioned work-ledger extension; the accepted source schema remains version1."""

import sqlite3

from ..watch.store_schema import APPLICATION_ID, SCHEMA_VERSION

VERSION = 1
SCHEMA = {
    "current_schema_version": """CREATE TABLE current_schema_version (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
    )""",
    "current_cases": """CREATE TABLE current_cases (
        case_id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
        config_json TEXT NOT NULL CHECK(length(config_json)<=4096),
        policy_json TEXT NOT NULL CHECK(length(policy_json)<=4096),
        policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),
        policy_id TEXT NOT NULL, simulated INTEGER NOT NULL CHECK(simulated IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0), created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(case_id,monitor_id)
    )""",
    "current_reviews": """CREATE TABLE current_reviews (
        task_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, monitor_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('OBSERVATION_REVIEW','COVERAGE_REVIEW','RESULT_VERIFICATION')),
        status TEXT NOT NULL CHECK(status IN ('PROPOSED','APPROVED','DEFERRED','DISMISSED','CANCELLED')),
        revision INTEGER NOT NULL CHECK(revision>0), title TEXT NOT NULL, reason TEXT NOT NULL,
        next_check_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(case_id,task_id), UNIQUE(case_id,task_id,monitor_id),
        FOREIGN KEY(case_id,monitor_id) REFERENCES current_cases(case_id,monitor_id),
        FOREIGN KEY(case_id,task_id,revision)
            REFERENCES current_review_revisions(case_id,task_id,revision)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    "current_review_revisions": """CREATE TABLE current_review_revisions (
        case_id TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        author_kind TEXT NOT NULL CHECK(author_kind IN ('PROPOSAL','HUMAN')),
        record_json TEXT NOT NULL CHECK(length(record_json)<=8192), recorded_at TEXT NOT NULL,
        PRIMARY KEY(case_id,task_id,revision),
        FOREIGN KEY(case_id,task_id) REFERENCES current_reviews(case_id,task_id)
    )""",
    "current_review_evidence": """CREATE TABLE current_review_evidence (
        link_id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, task_id TEXT NOT NULL,
        monitor_id TEXT NOT NULL, event_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
        UNIQUE(case_id,task_id,event_id),
        FOREIGN KEY(case_id,task_id,monitor_id) REFERENCES current_reviews(case_id,task_id,monitor_id),
        FOREIGN KEY(event_id,monitor_id) REFERENCES watch_events(event_id,monitor_id)
    )""",
    "current_receipts": """CREATE TABLE current_receipts (
        case_id TEXT NOT NULL, request_id TEXT NOT NULL,
        operation TEXT NOT NULL CHECK(operation IN ('STAGE','LINK','HUMAN')),
        input_hash TEXT NOT NULL CHECK(length(input_hash)=64),
        input_json TEXT NOT NULL CHECK(length(input_json)<=16384),
        result_json TEXT NOT NULL CHECK(length(result_json)<=8192),
        task_id TEXT NOT NULL, task_revision INTEGER NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY(case_id,request_id),
        FOREIGN KEY(case_id,task_id,task_revision)
            REFERENCES current_review_revisions(case_id,task_id,revision)
    )""",
    "current_active_kind": """CREATE UNIQUE INDEX current_active_kind ON current_reviews(case_id,kind)
        WHERE status IN ('PROPOSED','APPROVED','DEFERRED')""",
    "current_evidence_recent": """CREATE INDEX current_evidence_recent
        ON current_review_evidence(case_id,task_id,link_id DESC)""",
}


def ensure_schema(db: sqlite3.Connection) -> None:
    """Run inside one writer transaction, including the schema version marker."""
    if (
        db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
        or db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
    ):
        raise ValueError("current work requires a supported current-watch database")
    present = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'current_*'"))
    if present:
        if set(present) != set(SCHEMA):
            raise ValueError("partial or unknown current-case schema")
        for name, expected in SCHEMA.items():
            if " ".join((present[name] or "").split()) != " ".join(expected.split()):
                raise ValueError("current-case schema definition differs from this version")
        rows = db.execute("SELECT singleton,version FROM current_schema_version").fetchall()
        if [tuple(row) for row in rows] != [(1, VERSION)]:
            raise ValueError("unsupported current-case schema version")
        return
    for statement in SCHEMA.values():
        db.execute(statement)
    db.execute("INSERT INTO current_schema_version VALUES(1,?)", (VERSION,))
