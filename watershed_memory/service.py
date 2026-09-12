"""Durable case service with atomic turns and replay-safe request receipts."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .catalog import LABELS, PACKETS, SOURCES, event_cards
from .planning import GAP, TITLES, Planner, ReplayPlanner, validate_plan


class Conflict(ValueError):
    """A request conflicts with current case state or a previous request identity."""


class InProgress(RuntimeError):
    """A claimed turn is still running; retry with the same request identity."""


class SessionNotFound(KeyError):
    """Only a missing session, not an internal data/adapter defect."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Service:
    def __init__(self, path: Path, planner: Planner | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.planner = planner or ReplayPlanner()
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    request_id TEXT NOT NULL, digest TEXT NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY (session_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS claims (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id), request_id TEXT NOT NULL,
                    digest TEXT NOT NULL, owner TEXT NOT NULL, expires REAL NOT NULL
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create_session(self) -> dict:
        session = secrets.token_urlsafe(24)
        state = {
            "session_id": session, "mode": dict(self.planner.mode),
            "case": {"id": "GALLINAS-HPCC-2022", "title": "Gallinas River",
                     "subtitle": "New Mexico · Source-water watch", "status": "OPEN",
                     "coverage": "UNKNOWN", "water_safety": "NOT_ASSESSED"},
            "progress": {"processed": 0, "total": len(PACKETS), "next_label": "July 9 observations"},
            "events": event_cards(0), "tasks": [], "responses": [], "trace": [],
            "latest_brief": {"headline": "Ready for the first observations",
                "summary": "Bring the Gallinas observations into one enduring source-water case.",
                "changes": []}, "sources": SOURCES,
        }
        with closing(self._connect()) as db:
            db.execute("INSERT INTO sessions VALUES (?, 0, ?)", (session, encode(state)))
        return state

    def _read(self, session: str) -> tuple[int, dict]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT revision, state FROM sessions WHERE id=?", (session,)).fetchone()
        if row is None:
            raise SessionNotFound("This replay session was not found.")
        return row[0], json.loads(row[1])

    def snapshot(self, session: str) -> dict:
        return self._read(session)[1]

    def _digest(self, request: str, operation: dict) -> str:
        if not isinstance(request, str) or not 1 <= len(request) <= 128:
            raise ValueError("A request identity of 1–128 characters is required.")
        return hashlib.sha256(encode(operation).encode()).hexdigest()

    @staticmethod
    def _receipt(db: sqlite3.Connection, session: str, request: str, digest: str) -> dict | None:
        row = db.execute("SELECT digest, response FROM requests WHERE session_id=? AND request_id=?",
                         (session, request)).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise Conflict("This request identity was already used for a different operation.")
        return json.loads(row[1])

    def _previous(self, session: str, request: str, digest: str) -> dict | None:
        with closing(self._connect()) as db:
            return self._receipt(db, session, request, digest)

    def _commit(self, session: str, request: str, digest: str, revision: int,
                state: dict, claim: str | None = None) -> dict:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                previous = self._receipt(db, session, request, digest)
                if previous is not None:
                    db.rollback()
                    return previous
                active = db.execute("SELECT owner, expires FROM claims WHERE session_id=?", (session,)).fetchone()
                if claim and (not active or active[0] != claim or active[1] <= time.time()):
                    raise Conflict("This turn lost its execution claim; reload the saved case.")
                if not claim and active and active[1] > time.time():
                    raise InProgress("An observation turn is still running. Retry the same response shortly.")
                payload = encode(state)
                result = db.execute("UPDATE sessions SET state=?, revision=revision+1 "
                                    "WHERE id=? AND revision=?", (payload, session, revision))
                if result.rowcount != 1:
                    raise Conflict("The case changed during this turn. Reload and retry.")
                db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)",
                           (session, request, digest, payload))
                if claim:
                    db.execute("DELETE FROM claims WHERE session_id=? AND owner=?", (session, claim))
                db.commit()
                return state
            except BaseException:
                db.rollback()
                raise

    def advance(self, session: str, request: str) -> dict:
        digest = self._digest(request, {"operation": "advance"})
        claim = secrets.token_urlsafe(16)
        deadline = time.monotonic() + 2
        while True:
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    if (previous := self._receipt(db, session, request, digest)) is not None:
                        db.rollback()
                        return previous
                    if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session,)).fetchone():
                        raise SessionNotFound("This replay session was not found.")
                    active = db.execute("SELECT request_id, digest, expires FROM claims WHERE session_id=?",
                                        (session,)).fetchone()
                    if not active or active[2] <= time.time():
                        db.execute("INSERT OR REPLACE INTO claims VALUES (?, ?, ?, ?, ?)",
                                   (session, request, digest, claim, time.time() + 300))
                        db.commit()
                        break
                    if active[0] == request and active[1] != digest:
                        raise Conflict("This request identity is already claimed for different work.")
                    db.rollback()
                except BaseException:
                    db.rollback()
                    raise
            if time.monotonic() >= deadline:
                raise InProgress("An observation turn is still running. Retry with the same request identity.")
            time.sleep(0.1)
        try:
            return self._advance_claimed(session, request, digest, claim)
        finally:
            with closing(self._connect()) as db:
                db.execute("DELETE FROM claims WHERE session_id=? AND owner=?", (session, claim))

    def _advance_claimed(self, session: str, request: str, digest: str, claim: str) -> dict:
        revision, state = self._read(session)
        index = state["progress"]["processed"]
        if index >= len(PACKETS):
            raise Conflict("All three observation windows are already in this case.")
        released = PACKETS[:index + 1]
        planning_state = deepcopy(state)
        planning_state["_turn"] = {"request_id": request, "revision": revision}
        plan = self.planner.plan(planning_state, deepcopy(released))
        validate_plan(plan, state, released)
        changes = []
        for proposal in plan.proposals:
            existing = next((t for t in state["tasks"] if t["id"] == proposal["existing_task_id"]), None)
            if existing:
                existing["evidence"].append(proposal["event_id"])
                changes.append("New evidence linked to the existing review.")
            else:
                state["tasks"].append({
                    "id": secrets.token_urlsafe(12), "kind": proposal["kind"],
                    "title": TITLES[proposal["kind"]], "status": "OPEN",
                    "reason": proposal["reason"], "evidence": [proposal["event_id"]],
                    "created_at": now(),
                })
                changes.append("Station evidence gap review opened." if proposal["kind"] == GAP
                               else "Source-water monitoring review opened.")
        count = index + 1
        packet = PACKETS[index]
        state["mode"] = dict(self.planner.mode)
        state["events"] = event_cards(count)
        state["progress"] = {"processed": count, "total": len(PACKETS),
                             "next_label": f"{LABELS[count]} observations" if count < len(PACKETS) else None}
        state["case"]["coverage"] = "LIMITED" if packet["p1_turbidity_count"] else "DEGRADED"
        state["latest_brief"] = {
            "headline": ("The first review is in motion", "The next event. The same unfinished work.",
                         "One case. A new gap to investigate.")[index],
            "summary": ("July's observations now have a place in the source-water case.",
                        "August's evidence joins the case without losing earlier work or responses.",
                        "September adds observations while P1 evidence is absent from this archive "
                        "window. Coverage gets its own review.")[index],
            "changes": changes,
        }
        state["trace"] = plan.trace + [{"tool": "commit_case_turn", "input": {"event_id": packet["event_id"]},
                                        "output": {"status": "COMMITTED", "changes": changes}}]
        return self._commit(session, request, digest, revision, state, claim=claim)

    def respond(self, session: str, request: str, task_id: str, action: str, note: str) -> dict:
        if action not in ("acknowledge", "complete_review"):
            raise ValueError("Choose acknowledge or complete_review.")
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000:
            raise ValueError("Add an operator note of 1–1000 characters.")
        note = note.strip()
        digest = self._digest(request, {"operation": "respond", "task_id": task_id,
                                       "action": action, "note": note})
        if (previous := self._previous(session, request, digest)) is not None:
            return previous
        revision, state = self._read(session)
        task = next((t for t in state["tasks"] if t["id"] == task_id), None)
        if task is None or task["status"] == "COMPLETED":
            raise Conflict("That review is unavailable or already completed.")
        if action == "acknowledge" and task["status"] == "ACKNOWLEDGED":
            raise Conflict("This review has already been acknowledged.")
        task["status"] = "ACKNOWLEDGED" if action == "acknowledge" else "COMPLETED"
        state["responses"].append({"task_id": task_id, "action": action, "note": note,
            "recorded_at": now(), "actor": "Demo operator", "simulated": True})
        return self._commit(session, request, digest, revision, state)
