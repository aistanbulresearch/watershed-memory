"""Local deterministic workflow proof. No LLM, health decision or external action."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CASE_ID = "GALLINAS-HPCC-2022"
POLICY = "DEMO-REVIEW-1"


def aware_time(value: str) -> datetime:
    """Reject ambiguous times rather than silently using the machine timezone."""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("An explicit UTC offset is required")
    return result.astimezone(timezone.utc)


class CaseStore:
    """Own one durable case; all operational operator inputs are simulations."""

    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                monitoring_confidence TEXT NOT NULL, water_safety TEXT NOT NULL,
                latest_available_at TEXT, latest_observations_end TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                payload TEXT NOT NULL, available_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases,
                kind TEXT NOT NULL, status TEXT NOT NULL,
                policy_version TEXT NOT NULL, reason TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_unfinished_task
                ON tasks(case_id, kind) WHERE status != 'COMPLETED';
            CREATE TABLE IF NOT EXISTS task_evidence (
                task_id TEXT NOT NULL REFERENCES tasks,
                event_id TEXT NOT NULL REFERENCES events,
                PRIMARY KEY(task_id, event_id)
            );
            CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY, digest TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks,
                action TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL,
                simulated INTEGER NOT NULL CHECK(simulated = 1), recorded_at TEXT NOT NULL
            );
        """)
        self.db.execute("INSERT OR IGNORE INTO cases VALUES (?, 'OPEN', 'UNKNOWN', 'NOT_ASSESSED', NULL, NULL)", (CASE_ID,))
        self.db.commit()

    def __enter__(self) -> CaseStore:
        return self

    def __exit__(self, *_: Any) -> None:
        self.db.close()

    def ingest(self, event: dict[str, Any], *, now: str) -> str:
        """Consume a replay packet only after its declared availability time."""
        for key in ("event_id", "evidence_class", "availability_basis", "source_ids"):
            if not event.get(key):
                raise ValueError(f"Missing provenance: {key}")
        if event["evidence_class"] not in {"HISTORICAL_ARCHIVE_REPLAY", "SYNTHETIC_TEST_FIXTURE"}:
            raise ValueError("Unsupported evidence class")
        for key in ("p1_turbidity_count", "p2_turbidity_count"):
            if type(event.get(key)) is not int or event[key] < 0:
                raise ValueError(f"Invalid observation count: {key}")
        available = aware_time(event["available_at"])
        if available > aware_time(now):
            raise ValueError("Future evidence cannot be consumed")
        observations_end = aware_time(event["observations_end"])
        if observations_end > available:
            raise ValueError("Observation window must end before evidence is available")
        payload = json.dumps(event, sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT digest FROM events WHERE event_id = ?", (event["event_id"],)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise ValueError("Event ID collision: corrections require a new event ID")
                return "DUPLICATE_IGNORED"
            latest = self.db.execute("SELECT latest_observations_end FROM cases WHERE case_id = ?", (CASE_ID,)).fetchone()[0]
            if latest and observations_end == aware_time(latest):
                raise ValueError("Conflicting packet for an existing observation window")
            self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)", (
                event["event_id"], digest, payload, available.isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ))
            if latest and observations_end < aware_time(latest):
                return "LATE_EVENT_RECORDED_WITHOUT_STATE_ROLLBACK"
            confidence = "LIMITED" if event["p1_turbidity_count"] and event["p2_turbidity_count"] else "DEGRADED"
            self.db.execute("UPDATE cases SET monitoring_confidence = ?, latest_available_at = ?, latest_observations_end = ? WHERE case_id = ?", (confidence, available.isoformat(), observations_end.isoformat(), CASE_ID))
            if event["p2_turbidity_count"]:
                self._attach_task("MONITORING_REVIEW", event["event_id"], "Review new event evidence together with unfinished monitoring work. Demonstration policy; no hazard threshold.")
            if confidence == "DEGRADED":
                self._attach_task("EVIDENCE_GAP_REVIEW", event["event_id"], "An expected monitor has no usable turbidity entries in this window. Verify evidence availability; missing data is not a safety finding.")
        return "EVENT_APPLIED"

    def _attach_task(self, kind: str, event_id: str, reason: str) -> None:
        task = self.db.execute("SELECT task_id FROM tasks WHERE case_id = ? AND kind = ? AND status != 'COMPLETED'", (CASE_ID, kind)).fetchone()
        if task:
            task_id = task["task_id"]
        else:
            task_id = hashlib.sha256(f"{CASE_ID}:{kind}:{event_id}".encode()).hexdigest()[:16]
            self.db.execute("INSERT INTO tasks VALUES (?, ?, ?, 'OPEN', ?, ?)", (task_id, CASE_ID, kind, POLICY, reason))
        self.db.execute("INSERT OR IGNORE INTO task_evidence VALUES (?, ?)", (task_id, event_id))

    def respond(self, task_id: str, action: str, actor: str, note: str, *, simulated: bool, response_id: str | None = None) -> str:
        """A completed review never closes the watershed case or declares safety."""
        allowed = {"acknowledge": "ACKNOWLEDGED", "complete_review": "COMPLETED"}
        if action not in allowed or simulated is not True:
            raise ValueError("Only explicitly simulated review actions are supported")
        if not actor.strip() or not note.strip():
            raise ValueError("Actor and a review note are required")
        payload = json.dumps([task_id, action, actor, note, simulated])
        digest = hashlib.sha256(payload.encode()).hexdigest()
        response_id = response_id or digest
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT digest FROM responses WHERE response_id = ?", (response_id,)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise ValueError("Response ID collision")
                return "DUPLICATE_RESPONSE_IGNORED"
            task = self.db.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise ValueError("Unknown task")
            if task["status"] == "COMPLETED":
                raise ValueError("Completed reviews are immutable in this proof")
            self.db.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (allowed[action], task_id))
            self.db.execute("INSERT INTO responses(response_id, digest, task_id, action, actor, note, simulated, recorded_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)", (response_id, digest, task_id, action, actor, note, datetime.now(timezone.utc).isoformat()))
        return "RESPONSE_APPLIED"

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {"case": dict(self.db.execute("SELECT * FROM cases WHERE case_id = ?", (CASE_ID,)).fetchone())}
        for table in ("events", "tasks", "task_evidence", "responses"):
            result[table] = [dict(row) for row in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        return result
