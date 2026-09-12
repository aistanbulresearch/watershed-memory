"""Bounded row/receipt helpers shared by current work transactions."""

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime

from .case_types import ReviewRecord, WorkflowConflict

ACTIVE = "('PROPOSED','APPROVED','DEFERRED')"


def encode(value: object) -> str:
    def timestamp(item: object) -> str:
        if not isinstance(item, datetime):
            raise TypeError("unsupported record value")
        return item.isoformat()

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=timestamp,
        allow_nan=False,
        ensure_ascii=False,
    )


def digest(encoded: str) -> str:
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record(raw: dict | sqlite3.Row) -> ReviewRecord:
    values = {field: raw[field] for field in ReviewRecord.__dataclass_fields__}
    values["simulated"] = bool(values["simulated"])
    for field in ("created_at", "updated_at", "next_check_at"):
        if values[field] is not None:
            values[field] = datetime.fromisoformat(values[field])
    return ReviewRecord(**values)


def case_row(db: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM current_cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise KeyError("unknown current case")
    return row


def review_row(db: sqlite3.Connection, case_id: str, task_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT r.*,c.simulated FROM current_reviews r JOIN current_cases c "
        "ON c.case_id=r.case_id WHERE r.case_id=? AND r.task_id=?",
        (case_id, task_id),
    ).fetchone()
    if row is None:
        raise KeyError("review is not in this case")
    return row


def prior_receipt(
    db: sqlite3.Connection, case_id: str, request_id: str, encoded: str
) -> ReviewRecord | None:
    row = db.execute(
        "SELECT input_hash,result_json FROM current_receipts WHERE case_id=? AND request_id=?",
        (case_id, request_id),
    ).fetchone()
    if row is None:
        return None
    if row["input_hash"] != digest(encoded):
        raise WorkflowConflict("request ID was already used for different input")
    return record(json.loads(row["result_json"]))


def revision(db: sqlite3.Connection, value: ReviewRecord, author: str, now: datetime) -> None:
    db.execute(
        "INSERT INTO current_review_revisions VALUES(?,?,?,?,?,?)",
        (
            value.case_id,
            value.task_id,
            value.revision,
            author,
            encode(asdict(value)),
            now.isoformat(),
        ),
    )


def receipt(
    db: sqlite3.Connection,
    value: ReviewRecord,
    request_id: str,
    operation: str,
    encoded: str,
    now: datetime,
) -> ReviewRecord:
    db.execute(
        "UPDATE current_cases SET revision=revision+1,updated_at=? WHERE case_id=?",
        (now.isoformat(), value.case_id),
    )
    db.execute(
        "INSERT INTO current_receipts VALUES(?,?,?,?,?,?,?,?,?)",
        (
            value.case_id,
            request_id,
            operation,
            digest(encoded),
            encoded,
            encode(asdict(value)),
            value.task_id,
            value.revision,
            now.isoformat(),
        ),
    )
    return value


def link(
    db: sqlite3.Connection, case: sqlite3.Row, task_id: str, event_id: str, now: datetime
) -> None:
    found = db.execute(
        "SELECT interval_end FROM watch_events WHERE event_id=? AND monitor_id=?",
        (event_id, case["monitor_id"]),
    ).fetchone()
    if not found:
        raise ValueError("evidence does not belong to this case source")
    if datetime.fromisoformat(found["interval_end"]) > now:
        raise ValueError("evidence interval has not ended at this operation time")
    db.execute(
        "INSERT INTO current_review_evidence(case_id,task_id,monitor_id,event_id,recorded_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(case_id,task_id,event_id) DO NOTHING",
        (case["case_id"], task_id, case["monitor_id"], event_id, now.isoformat()),
    )


def check_revision(case: sqlite3.Row, expected: int) -> None:
    if case["revision"] != expected:
        raise WorkflowConflict("case changed after this operation was prepared")


def check_time(case: sqlite3.Row, now: datetime) -> None:
    if now < datetime.fromisoformat(case["updated_at"]):
        raise ValueError("a new operation cannot predate the current case revision")
