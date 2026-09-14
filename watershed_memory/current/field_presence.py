"""Prevent a new legacy agent context from silently leaving field results behind."""

from .case_types import WorkflowConflict
from .field_schema import ensure_schema


def require_legacy_context(db, case_id: str) -> None:
    present = db.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'field_*' LIMIT 1").fetchone()
    if present is None:
        return
    # A present extension is verified, never repaired or partly created here.
    ensure_schema(db)
    if db.execute("SELECT 1 FROM field_plans WHERE case_id=? LIMIT 1", (case_id,)).fetchone():
        raise WorkflowConflict("field work requires context contract v3")
