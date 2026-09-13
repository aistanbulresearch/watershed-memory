"""Read-only browser projections of exact legacy and field-aware assessments."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict

from . import case_records as rows
from . import field_delivery_records
from .case_store import _expected
from .case_types import WorkflowConflict
from .context3_schema import ensure_schema as check_context3_schema
from .delivery_store import _receipt
from .desk_records import assessment_detail, attention_reasons, summary
from .dispatch_schema import ensure_schema as check_dispatch_schema
from .dispatch_store import _settings
from .field_delivery_codec import decode_execution, decode_failure
from .field_desk_records import project_field
from .field_dispatch_store import _read_admission, _settings_v3
from .field_store import FieldStore
from .strands import HEALTH_INSTRUCTION_VERSION, INSTRUCTION_VERSION
from .tools import _plain


def _read_transaction(db: sqlite3.Connection) -> None:
    if (
        not isinstance(db, sqlite3.Connection)
        or not db.in_transaction
        or db.execute("PRAGMA query_only").fetchone()[0] != 1
    ):
        raise ValueError("field assessment history requires a query-only transaction")


def _namespace(db: sqlite3.Connection, prefix: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE name GLOB ? LIMIT 1",
            (prefix + "_*",),
        ).fetchone()
        is not None
    )


def _context3(db: sqlite3.Connection) -> bool:
    present = _namespace(db, "context3")
    if not present:
        return False
    try:
        # Existing validators are authoritative. Query-only mode makes a missing
        # dependency fail instead of letting ensure_schema repair it on a GET.
        check_context3_schema(db)
    except sqlite3.OperationalError as error:
        raise ValueError("incomplete context3 dependencies") from error
    return True


def read_settings(db, fields, case_id):
    """Return validated dispatch settings without creating any namespace."""
    _read_transaction(db)
    if type(fields) is not FieldStore:
        raise ValueError("field history requires an exact field store")
    has_context3 = _context3(db)
    if not _namespace(db, "dispatch"):
        if has_context3:
            raise ValueError("context3 history lacks dispatch dependencies")
        return None
    check_dispatch_schema(db)
    if db.execute("SELECT 1 FROM dispatch_settings WHERE case_id=?", (case_id,)).fetchone() is None:
        return None
    extension = None
    if has_context3:
        extension = db.execute(
            "SELECT 1 FROM context3_dispatch_settings WHERE case_id=?", (case_id,)
        ).fetchone()
    if extension is not None:
        return _settings_v3(db, case_id)
    state, policy, profile = _settings(db, case_id)
    return state, policy, profile, None


def _legacy_version(profile) -> int:
    if profile.instruction_version == INSTRUCTION_VERSION:
        return 1
    if profile.instruction_version == HEALTH_INSTRUCTION_VERSION:
        return 2
    raise ValueError("unsupported saved context version")


def _admission(db, fields, raw, settings, is_v3):
    if not _namespace(db, "dispatch"):
        if settings is not None:
            raise ValueError("saved dispatch settings lack their namespace")
        return None
    membership = db.execute(
        "SELECT attention_json FROM dispatch_attempts WHERE case_id=? AND attempt_id=?",
        (raw["case_id"], raw["attempt_id"]),
    ).fetchone()
    if settings is None:
        if membership is not None:
            raise WorkflowConflict("saved dispatch admission has no activated settings")
        return None
    if type(settings) is not tuple or len(settings) != 4:
        raise ValueError("invalid saved dispatch settings")
    state, policy, current, previous = settings
    if is_v3:
        if previous is None:
            raise WorkflowConflict("field-aware history lacks its v3 settings pin")
        profile = current
    else:
        profile = previous if previous is not None else current
    _, _, _, _, saved = _read_admission(db, fields, raw, state, policy, profile, is_v3)
    return saved["attention_json"]


def _legacy(raw, attention, *, detail):
    # Detail performs the accepted execution/failure identity and codec checks;
    # run it even for the bounded summary projection.
    projected = assessment_detail(raw, attention)
    context_version = _legacy_version(_receipt(raw).profile)
    if not detail:
        result = summary(raw)
        result["context_version"] = context_version
        return result
    projected.update(
        {
            "context_version": context_version,
            "assessed_field_context": None,
            "field_decision": None,
            "field_tool_names": [],
            "field_proposal": None,
        }
    )
    return projected


def _v3(db, fields, raw, attention, *, detail):
    receipt = field_delivery_records.receipt(db, fields, raw)
    context = field_delivery_records.restore_reserved(db, fields, raw)
    execution = None if raw["execution_json"] is None else decode_execution(raw["execution_json"])
    failure = None if raw["failure_json"] is None else decode_failure(raw["failure_json"])
    result = summary(raw)
    result["context_version"] = 3
    if not detail:
        return result

    source_trace = ()
    field_trace = ()
    decisions = []
    field_decision = None
    if execution is not None:
        source_trace = execution.assessment.base.trace
        field_trace = execution.assessment.field_trace
        decisions = _plain(execution.assessment.base.decisions)
        field_decision = _plain(execution.assessment.field)
    elif failure is not None:
        source_trace = failure.source_trace
        field_trace = failure.field_trace
    result.pop("mode")
    result.update(
        {
            "case_revision": raw["case_revision"],
            "profile": asdict(receipt.delivery.profile),
            "attention_reasons": attention_reasons(attention),
            "tool_names": [item.name for item in source_trace],
            "decisions": decisions,
            "context_version": 3,
            "assessed_field_context": project_field(context.field_work, None),
            "field_decision": field_decision,
            "field_tool_names": [item.name for item in field_trace],
            "field_proposal": (
                None if receipt.field_plan is None else _plain(receipt.field_plan.plan)
            ),
        }
    )
    return result


def read_assessment(db, fields, case_id, raw, settings, *, detail=False):
    """Validate and project one case-bound saved assessment."""
    _read_transaction(db)
    if type(fields) is not FieldStore or type(detail) is not bool:
        raise ValueError("invalid field assessment projection")
    _expected(raw["case_revision"])
    if raw["case_id"] != case_id:
        raise KeyError("no saved assessment in this case")
    case = rows.case_row(db, case_id)
    if raw["case_revision"] > case["revision"]:
        raise WorkflowConflict("saved assessment exceeds the case revision")

    has_context3 = _context3(db)
    is_v3 = False
    if has_context3:
        is_v3 = (
            db.execute(
                "SELECT 1 FROM context3_attempts WHERE case_id=? AND attempt_id=?",
                (case_id, raw["attempt_id"]),
            ).fetchone()
            is not None
        )
    profile = _receipt(raw).profile
    if (profile.instruction_version == "watershed-current-v3") != is_v3:
        raise WorkflowConflict("saved v3 profile and context membership differ")
    attention = _admission(db, fields, raw, settings, is_v3)
    if is_v3:
        return _v3(db, fields, raw, attention, detail=detail)
    return _legacy(raw, attention, detail=detail)
