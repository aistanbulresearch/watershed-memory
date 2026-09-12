"""Durable current work, evidence links and revision-bound human commands.

This internal transaction boundary does not invoke models or acknowledge the
source outbox. The assessment dispatcher will own that separate delivery gate.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from . import case_records as rows
from .case_schema import ensure_schema
from .case_types import (
    CaseConfig,
    EvidenceLink,
    HumanAction,
    ReviewDraft,
    ReviewRecord,
    WorkflowConflict,
    _identifier,
)
from .fact_validation import utc


class _Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _expected(value: int) -> None:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("invalid expected case revision")


def _future(due: datetime | None, now: datetime) -> None:
    if due is not None and not timedelta(0) < due - now <= timedelta(days=30):
        raise ValueError("a new check must be in the next30days")


class CaseStore:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError("initialize a current-watch database before adding case work")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=30, factory=_Connection
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
        except Exception:
            db.close()
            raise
        return db

    def register(self, config: CaseConfig, *, now: datetime) -> None:
        now = utc(now)
        if type(config) is not CaseConfig:
            raise ValueError("expected a current case configuration")
        encoded = rows.encode(asdict(config))
        policy = rows.encode(asdict(config.coverage_policy))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT config_json FROM current_cases WHERE case_id=?", (config.case_id,)
            ).fetchone()
            if existing:
                if existing[0] != encoded:
                    raise WorkflowConflict("current case configuration is immutable")
                return
            monitor = db.execute(
                "SELECT case_id,config_json FROM monitors WHERE monitor_id=?", (config.monitor_id,)
            ).fetchone()
            if monitor is None:
                raise KeyError("unknown source monitor")
            if not config.simulated and config.case_id != monitor["case_id"]:
                raise ValueError("an operational case must match its source monitor")
            specs = json.loads(monitor["config_json"])["series"]
            if not set(config.coverage_policy.required_parameters) <= {
                s["parameter_code"] for s in specs
            }:
                raise ValueError("case policy requires an unconfigured source parameter")
            db.execute(
                "INSERT INTO current_cases(case_id,monitor_id,config_json,policy_json,policy_digest,"
                "policy_id,simulated,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    config.case_id,
                    config.monitor_id,
                    encoded,
                    policy,
                    rows.digest(policy),
                    config.policy_id,
                    int(config.simulated),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    def context(self, case_id: str) -> dict:
        _identifier(case_id, "case_id")
        with self._connect() as db:
            db.execute("BEGIN")
            case = rows.case_row(db, case_id)
            active = db.execute(
                "SELECT r.*,c.simulated FROM current_reviews r JOIN current_cases c "
                "ON c.case_id=r.case_id WHERE r.case_id=? AND r.status IN "
                + rows.ACTIVE
                + " ORDER BY r.kind LIMIT 3",
                (case_id,),
            ).fetchall()
            return {
                "case_id": case_id,
                "monitor_id": case["monitor_id"],
                "policy_id": case["policy_id"],
                "policy_digest": case["policy_digest"],
                "simulated": bool(case["simulated"]),
                "revision": case["revision"],
                "updated_at": case["updated_at"],
                "active_reviews": tuple(rows.record(r) for r in active),
            }

    def get_review(self, case_id: str, task_id: str) -> ReviewRecord:
        _identifier(case_id, "case_id")
        _identifier(task_id, "task_id")
        with self._connect() as db:
            return rows.record(rows.review_row(db, case_id, task_id))

    def evidence(self, case_id: str, task_id: str, *, limit: int = 20) -> tuple[str, ...]:
        _identifier(case_id, "case_id")
        _identifier(task_id, "task_id")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("evidence limit must be between1and100")
        with self._connect() as db:
            db.execute("BEGIN")
            rows.review_row(db, case_id, task_id)
            result = db.execute(
                "SELECT event_id FROM current_review_evidence WHERE case_id=? AND task_id=? "
                "ORDER BY link_id DESC LIMIT ?",
                (case_id, task_id, limit),
            ).fetchall()
            return tuple(row[0] for row in result)

    @staticmethod
    def _input(
        case: sqlite3.Row, operation: str, command: object, expected: int | None = None
    ) -> str:
        return rows.encode(
            {
                "operation": operation,
                "case_id": case["case_id"],
                "policy_digest": case["policy_digest"],
                "command": asdict(command),
                "expected_case_revision": expected,
            }
        )

    def stage_review(
        self,
        case_id: str,
        draft: ReviewDraft,
        *,
        request_id: str,
        expected_case_revision: int,
        now: datetime,
    ) -> ReviewRecord:
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        _expected(expected_case_revision)
        now = utc(now)
        if type(draft) is not ReviewDraft or draft.kind == "RESULT_VERIFICATION":
            raise ValueError("expected an observation or coverage review draft")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._stage_review(
                db, case_id, draft, request_id=request_id,
                expected_case_revision=expected_case_revision, now=now,
            )

    def _stage_review(
        self, db: sqlite3.Connection, case_id: str, draft: ReviewDraft, *,
        request_id: str, expected_case_revision: int, now: datetime,
    ) -> ReviewRecord:
        if not db.in_transaction:
            raise ValueError("stage_review requires an active transaction")
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        _expected(expected_case_revision)
        now = utc(now)
        if type(draft) is not ReviewDraft or draft.kind == "RESULT_VERIFICATION":
            raise ValueError("expected an observation or coverage review draft")
        case = rows.case_row(db, case_id)
        encoded = self._input(case, "STAGE", draft, expected_case_revision)
        saved = rows.prior_receipt(db, case_id, request_id, encoded)
        if saved is not None:
            return saved
        rows.check_revision(case, expected_case_revision)
        rows.check_time(case, now)
        _future(draft.next_check_at, now)
        active = db.execute(
            "SELECT task_id FROM current_reviews WHERE case_id=? AND kind=? AND status IN "
            + rows.ACTIVE, (case_id, draft.kind),
        ).fetchone()
        if active:
            raise WorkflowConflict("link evidence to the existing review instead of duplicating it")
        task_id = "review-" + uuid.uuid4().hex
        due = draft.next_check_at.isoformat() if draft.next_check_at is not None else None
        db.execute(
            "INSERT INTO current_reviews(task_id,case_id,monitor_id,kind,status,revision,"
            "title,reason,next_check_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, case_id, case["monitor_id"], draft.kind, "PROPOSED", 1,
             draft.title, draft.reason, due, now.isoformat(), now.isoformat()),
        )
        value = rows.record(rows.review_row(db, case_id, task_id))
        rows.revision(db, value, "PROPOSAL", now)
        rows.link(db, case, task_id, draft.event_id, now)
        return rows.receipt(db, value, request_id, "STAGE", encoded, now)

    def link_evidence(
        self,
        case_id: str,
        link: EvidenceLink,
        *,
        request_id: str,
        expected_case_revision: int,
        now: datetime,
    ) -> ReviewRecord:
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        _expected(expected_case_revision)
        now = utc(now)
        if type(link) is not EvidenceLink:
            raise ValueError("expected an evidence link")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._link_evidence(
                db, case_id, link, request_id=request_id,
                expected_case_revision=expected_case_revision, now=now,
            )

    def _link_evidence(
        self, db: sqlite3.Connection, case_id: str, link: EvidenceLink, *,
        request_id: str, expected_case_revision: int, now: datetime,
    ) -> ReviewRecord:
        if not db.in_transaction:
            raise ValueError("link_evidence requires an active transaction")
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        _expected(expected_case_revision)
        now = utc(now)
        if type(link) is not EvidenceLink:
            raise ValueError("expected an evidence link")
        case = rows.case_row(db, case_id)
        encoded = self._input(case, "LINK", link, expected_case_revision)
        saved = rows.prior_receipt(db, case_id, request_id, encoded)
        if saved is not None:
            return saved
        rows.check_revision(case, expected_case_revision)
        rows.check_time(case, now)
        value = rows.record(rows.review_row(db, case_id, link.task_id))
        if value.status not in ("PROPOSED", "APPROVED", "DEFERRED"):
            raise WorkflowConflict("terminal work cannot receive new evidence implicitly")
        rows.link(db, case, value.task_id, link.event_id, now)
        return rows.receipt(db, value, request_id, "LINK", encoded, now)

    def act(
        self, case_id: str, action: HumanAction, *, request_id: str, now: datetime
    ) -> ReviewRecord:
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        now = utc(now)
        if type(action) is not HumanAction:
            raise ValueError("expected a human action")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            case = rows.case_row(db, case_id)
            encoded = self._input(case, "HUMAN", action)
            saved = rows.prior_receipt(db, case_id, request_id, encoded)
            if saved is not None:
                return saved
            rows.check_time(case, now)
            prior = rows.record(rows.review_row(db, case_id, action.task_id))
            if prior.revision != action.expected_revision or prior.status not in (
                "PROPOSED",
                "APPROVED",
                "DEFERRED",
            ):
                raise WorkflowConflict("review revision or state no longer permits this action")
            if now < prior.updated_at:
                raise ValueError("human action cannot precede the current review revision")
            status, title, due = self._transition(prior, action, now)
            db.execute(
                "UPDATE current_reviews SET status=?,revision=revision+1,title=?,next_check_at=?,updated_at=? "
                "WHERE case_id=? AND task_id=?",
                (
                    status,
                    title,
                    due.isoformat() if due is not None else None,
                    now.isoformat(),
                    case_id,
                    prior.task_id,
                ),
            )
            value = rows.record(rows.review_row(db, case_id, prior.task_id))
            rows.revision(db, value, "HUMAN", now)
            return rows.receipt(db, value, request_id, "HUMAN", encoded, now)

    @staticmethod
    def _transition(
        prior: ReviewRecord, action: HumanAction, now: datetime
    ) -> tuple[str, str, datetime | None]:
        if action.action == "APPROVE":
            if prior.status not in ("PROPOSED", "DEFERRED"):
                raise WorkflowConflict("this review is already approved")
            _future(prior.next_check_at, now)
            return "APPROVED", prior.title, prior.next_check_at
        if action.action in ("MODIFY", "DEFER"):
            _future(action.next_check_at, now)
            return (
                ("APPROVED", action.title, action.next_check_at)
                if action.action == "MODIFY"
                else ("DEFERRED", prior.title, action.next_check_at)
            )
        return ("DISMISSED" if action.action == "DISMISS" else "CANCELLED"), prior.title, None
