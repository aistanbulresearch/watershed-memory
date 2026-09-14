"""One immutable source-and-field snapshot, loaded without any model invocation."""

from pathlib import Path

from . import case_records as cases
from .case_store import CaseStore
from .context import _load_base_context
from .context_v3_types import CurrentContextV3
from .field_context import _load_field_context, field_hash
from .field_schema import ensure_schema
from .field_store import FieldStore


def _require_transaction(db, fields):
    if type(fields) is not FieldStore or not db.in_transaction:
        raise ValueError("field context requires its store and an explicit transaction")
    main = [row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"]
    if len(main) != 1 or Path(main[0]).resolve() != fields.cases.path:
        raise ValueError("field context connection differs from its store")
    if (
        db.execute("SELECT 1 FROM sqlite_master WHERE name='field_schema_version'").fetchone()
        is None
    ):
        raise ValueError("field context requires an initialized field schema")
    ensure_schema(db)


def load_context_v3(
    store: CaseStore,
    fields: FieldStore,
    case_id: str,
    event_id: str,
    *,
    evaluated_at,
    include_source_health: bool = True,
) -> CurrentContextV3:
    if type(store) is not CaseStore or type(fields) is not FieldStore or fields.cases is not store:
        raise ValueError("source and field context require the same exact case store")
    with store._connect() as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        return _load_context_v3(
            db,
            fields,
            case_id,
            event_id,
            evaluated_at=evaluated_at,
            include_source_health=include_source_health,
        )


def _load_context_v3(
    db,
    fields: FieldStore,
    case_id: str,
    event_id: str,
    *,
    evaluated_at,
    prior_event_ids=None,
    include_source_health: bool = True,
) -> CurrentContextV3:
    _require_transaction(db, fields)
    base = _load_base_context(
        db,
        case_id,
        event_id,
        evaluated_at=evaluated_at,
        prior_event_ids=prior_event_ids,
        include_source_health=include_source_health,
    )
    field = _load_field_context(
        db, cases.case_row(db, case_id), fields.locations, evaluated_at=base.evaluated_at
    )
    return CurrentContextV3(base, field, field_hash(field))
