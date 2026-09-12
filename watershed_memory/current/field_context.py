"""Bounded partitions of actionable work, stranded plans and recent results."""

from dataclasses import asdict

from . import case_records as cases
from . import field_records as records
from .context_v3_types import FieldContext
from .field_codec import encode
from .locations import LocationRegistry

ACTIVITIES = ("VISUAL_INSPECTION", "SAMPLING", "MAINTENANCE_REVIEW")
SELECTIONS = (
    ("CURRENT_PLAN", "current_plans"),
    ("STRANDED_PLAN", "stranded_plans"),
    ("LATEST_RESULT", "latest_results"),
)
_JOIN = " FROM field_plans p JOIN current_reviews r ON r.case_id=p.case_id AND r.task_id=p.task_id "
_ACTIVE = "p.status IN ('PROPOSED','APPROVED','DEFERRED')"
_PARENT_ACTIVE = "r.status IN ('PROPOSED','APPROVED','DEFERRED')"
_KINDS = ("OBSERVATION_REVIEW", "COVERAGE_REVIEW")


def field_hash(value):
    return cases.digest(encode(asdict(value)))


def _load_field_context(db, case, locations, *, evaluated_at):
    if not db.in_transaction or type(locations) is not LocationRegistry:
        raise ValueError("field context requires a transaction and trusted locations")
    case_id = case["case_id"]
    active = db.execute(
        "SELECT p.plan_id,p.task_id,p.review_revision,p.last_activity_revision,r.revision,r.kind "
        "FROM current_reviews r INDEXED BY current_active_kind "
        "CROSS JOIN field_plans p INDEXED BY field_active_review "
        "ON p.case_id=r.case_id AND p.task_id=r.task_id "
        "WHERE r.case_id=? AND " + _PARENT_ACTIVE + " AND " + _ACTIVE + " LIMIT 3",
        (case_id,),
    ).fetchall()
    if any(row[5] not in _KINDS for row in active):
        raise ValueError("active field plan has an unsupported parent kind")
    if len(active) > 2:
        raise ValueError("too many actionable field plans")
    current = sorted((row for row in active if row[2] == row[4]), key=lambda row: row[1])
    blockers = sorted((row for row in active if row[2] != row[4]), key=lambda row: -row[3])
    terminal = db.execute(
        "SELECT p.plan_id"
        + _JOIN
        + "WHERE p.case_id=? AND "
        + _ACTIVE
        + " AND r.status IN ('CANCELLED','DISMISSED')"
        + " ORDER BY p.last_activity_revision DESC LIMIT ?",
        (case_id, 4 - len(blockers)),
    ).fetchall()
    stranded = [*blockers, *terminal]
    reported = db.execute(
        "SELECT plan_id FROM field_plans WHERE case_id=? AND status='REPORTED' "
        "ORDER BY last_activity_revision DESC LIMIT 4",
        (case_id,),
    ).fetchall()
    approved = {}
    for activity in ACTIVITIES:
        for site in locations.approved_for(
            case_id, simulated=bool(case["simulated"]), activity=activity, now=evaluated_at
        ):
            approved[(site.location_id, site.revision)] = site
    if len(approved) > 16:
        raise ValueError("too many approved field locations")
    exists = db.execute("SELECT 1 FROM field_plans WHERE case_id=? LIMIT 1", (case_id,)).fetchone()

    def snapshots(rows):
        return tuple(records.snapshot(db, case_id, row[0]) for row in rows)

    return FieldContext(
        case_id,
        case["revision"],
        bool(case["simulated"]),
        evaluated_at,
        "PRESENT" if exists else "EMPTY",
        snapshots(current),
        snapshots(stranded[:3]),
        snapshots(reported[:3]),
        tuple(approved[key] for key in sorted(approved)),
        len(stranded) > 3,
        len(reported) > 3,
    )
