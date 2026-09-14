"""Read-only discovery of current review parents eligible for a field plan."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from . import case_records
from .context_v3_types import FieldContext
from .field_context import _KINDS
from .field_types import FieldPrincipal

_ACTIVE = ("PROPOSED", "APPROVED", "DEFERRED")


def _validate_case(db: sqlite3.Connection, context: FieldContext) -> None:
    row = db.execute(
        "SELECT case_id,revision,simulated,updated_at FROM current_cases WHERE case_id=?",
        (context.case_id,),
    ).fetchone()
    if row is None:
        raise ValueError("field context case is not saved")
    if (
        type(row["revision"]) is not int
        or type(row["simulated"]) is not int
        or row["simulated"] not in (0, 1)
        or row["revision"] != context.case_revision
        or bool(row["simulated"]) != context.simulated
        or type(row["updated_at"]) is not str
    ):
        raise ValueError("saved case identity differs from field context")
    try:
        updated = datetime.fromisoformat(row["updated_at"])
        if updated.tzinfo is None or updated > context.evaluated_at:
            raise ValueError("saved case time differs from field context")
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("saved case time is invalid") from error


def _review(row: sqlite3.Row, context: FieldContext) -> dict[str, Any]:
    if (
        type(row["simulated"]) is not int
        or row["simulated"] not in (0, 1)
        or type(row["revision"]) is not int
        or isinstance(row["revision"], bool)
    ):
        raise ValueError("saved parent review is malformed")
    value = case_records.record(row)
    if (
        value.case_id != context.case_id
        or value.simulated != context.simulated
        or value.kind not in _KINDS
        or value.status not in _ACTIVE
        or value.revision < 1
        or value.revision > context.case_revision
        or value.updated_at > context.evaluated_at
    ):
        raise ValueError("saved parent review differs from field context")
    return {"task_id": value.task_id, "revision": value.revision, "title": value.title}


def proposal_targets(
    db: sqlite3.Connection,
    context: FieldContext,
    principal: FieldPrincipal | None,
) -> list[dict[str, Any]]:
    """Return active source reviews for which a new field plan is admissible."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("proposal discovery requires a transaction")
    if db.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("proposal discovery requires query-only access")
    if type(context) is not FieldContext:
        raise ValueError("expected an exact field context")
    if principal is not None and (
        type(principal) is not FieldPrincipal or context.case_id not in principal.case_ids
    ):
        raise ValueError("principal is not bound to this case")
    _validate_case(db, context)
    if principal is None or "COORDINATOR" not in principal.roles or not context.approved_locations:
        return []
    rows = db.execute(
        "SELECT r.*,c.simulated FROM current_reviews r INDEXED BY current_active_kind "
        "JOIN current_cases c ON c.case_id=r.case_id "
        "WHERE r.case_id=? AND r.kind IN (?,?) "
        "AND r.status IN " + case_records.ACTIVE + " "
        "AND NOT EXISTS (SELECT 1 FROM field_plans p INDEXED BY field_active_review "
        "WHERE p.case_id=r.case_id AND p.task_id=r.task_id AND p.status IN "
        + case_records.ACTIVE
        + ") "
        "ORDER BY r.kind,r.task_id LIMIT 3",
        (context.case_id, *_KINDS),
    ).fetchall()
    if len(rows) > 2:
        raise ValueError("too many active proposal parents")
    return [_review(row, context) for row in rows]
