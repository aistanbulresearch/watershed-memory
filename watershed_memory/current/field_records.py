"""Bounded relational field reads and append helpers, inside caller transactions."""

from dataclasses import asdict

from . import case_records as cases
from .field_codec import decode, encode
from .field_models import (
    FieldEvidenceRecord,
    FieldPlanRecord,
    FieldReportRecord,
    FieldResultSnapshot,
    FieldVerificationRecord,
    FieldWorkSnapshot,
    field_dimension,
)


def _same(record, raw, mapping):
    for field, column in mapping.items():
        value = getattr(record, field)
        if field.endswith("_at"):
            value = value.isoformat()
        if field == "simulated":
            value = int(value)
        if value != raw[column]:
            raise ValueError("field row identity differs from its saved record")


def plan(db, case_id, plan_id, revision=None):
    current = db.execute(
        "SELECT * FROM field_plans WHERE case_id=? AND plan_id=?", (case_id, plan_id)
    ).fetchone()
    if current is None:
        raise KeyError("field plan is not in this case")
    target = current["current_revision"] if revision is None else revision
    raw = db.execute(
        "SELECT * FROM field_plan_revisions WHERE case_id=? AND plan_id=? AND revision=?",
        (case_id, plan_id, target),
    ).fetchone()
    if raw is None:
        raise KeyError("unknown field plan revision")
    record = decode(raw["record_json"], FieldPlanRecord)
    _same(
        record,
        raw,
        {
            "case_id": "case_id",
            "plan_id": "plan_id",
            "revision": "revision",
            "task_id": "task_id",
            "review_revision": "review_revision",
            "updated_at": "recorded_at",
        },
    )
    _same(
        record,
        current,
        {
            "task_id": "task_id",
            "review_revision": "review_revision",
            "simulated": "simulated",
            "created_at": "created_at",
        },
    )
    case = cases.case_row(db, case_id)
    if int(record.simulated) != case["simulated"]:
        raise ValueError("field plan simulation differs from its case")
    if target == current["current_revision"]:
        _same(record, current, {"status": "status", "updated_at": "updated_at"})
    return record


def report(db, case_id, report_id, revision=None):
    current = db.execute(
        "SELECT * FROM field_reports WHERE case_id=? AND report_id=?", (case_id, report_id)
    ).fetchone()
    if current is None:
        raise KeyError("field result is not in this case")
    target = current["current_revision"] if revision is None else revision
    raw = db.execute(
        "SELECT * FROM field_report_revisions WHERE case_id=? AND report_id=? AND revision=?",
        (case_id, report_id, target),
    ).fetchone()
    if raw is None:
        raise KeyError("unknown field report revision")
    record = decode(raw["record_json"], FieldReportRecord)
    _same(
        record,
        raw,
        {
            "case_id": "case_id",
            "report_id": "report_id",
            "revision": "revision",
            "plan_id": "plan_id",
            "performed_plan_revision": "performed_plan_revision",
            "updated_at": "recorded_at",
        },
    )
    _same(
        record,
        current,
        {
            "plan_id": "plan_id",
            "performed_plan_revision": "performed_plan_revision",
            "simulated": "simulated",
            "created_at": "created_at",
        },
    )
    performed = plan(db, case_id, record.plan_id, record.performed_plan_revision)
    if (
        performed.status != "APPROVED"
        or performed.simulated != record.simulated
        or performed.updated_at > record.performed_start
    ):
        raise ValueError("field report does not match an approved performed plan")
    if target == current["current_revision"]:
        _same(record, current, {"outcome": "outcome", "updated_at": "updated_at"})
    return record


def evidence_digest(record):
    return cases.digest(
        encode(
            {
                name: getattr(record, name)
                for name in ("reference_kind", "evidence_category", "reference")
            }
        )
    )


def result(db, case_id, report_id, revision=None):
    value = report(db, case_id, report_id, revision)
    evidence = []
    for raw in db.execute(
        "SELECT * FROM field_evidence WHERE case_id=? AND report_id=? "
        "AND report_revision=? ORDER BY evidence_id LIMIT 9",
        (case_id, report_id, value.revision),
    ):
        item = decode(raw["record_json"], FieldEvidenceRecord)
        _same(
            item,
            raw,
            {
                "case_id": "case_id",
                "report_id": "report_id",
                "report_revision": "report_revision",
                "evidence_id": "evidence_id",
                "attached_at": "recorded_at",
            },
        )
        if evidence_digest(item) != raw["evidence_digest"]:
            raise ValueError("field evidence digest differs from its record")
        evidence.append(item)
    if len(evidence) > 8:
        raise ValueError("too many field evidence records")
    raw = db.execute(
        "SELECT * FROM field_verifications WHERE case_id=? AND report_id=? "
        "AND report_revision=? ORDER BY sequence DESC LIMIT 1",
        (case_id, report_id, value.revision),
    ).fetchone()
    verification = None
    if raw is not None:
        item = decode(raw["record_json"], FieldVerificationRecord)
        _same(
            item,
            raw,
            {
                "case_id": "case_id",
                "report_id": "report_id",
                "report_revision": "report_revision",
                "verification_id": "verification_id",
                "verified_at": "recorded_at",
            },
        )
        if encode(item.evidence_ids) != raw["evidence_ids_json"]:
            raise ValueError("verification evidence differs from its indexed record")
        by_id = {e.evidence_id: e for e in evidence}
        if (
            not set(item.evidence_ids) <= set(by_id)
            or item.simulated != value.simulated
            or any(by_id[e].attached_at > item.verified_at for e in item.evidence_ids)
        ):
            raise ValueError("field verification refers to invalid evidence")
        if tuple(by_id) == item.evidence_ids:
            verification = item
    level = "VERIFIED" if verification else "EVIDENCE_ATTACHED" if evidence else "REPORTED"
    return FieldResultSnapshot(value, tuple(evidence), verification, level)


def snapshot(db, case_id, plan_id):
    current = plan(db, case_id, plan_id)
    report_id = db.execute(
        "SELECT report_id FROM field_reports WHERE case_id=? AND plan_id=?", (case_id, plan_id)
    ).fetchone()
    outcome = result(db, case_id, report_id[0]) if report_id else None
    parent = cases.review_row(db, case_id, current.task_id)
    binding = (
        "TERMINAL"
        if parent["status"] in ("CANCELLED", "DISMISSED")
        else "CURRENT"
        if parent["revision"] == current.review_revision
        else "SUPERSEDED"
    )
    return FieldWorkSnapshot(current, outcome, binding, field_dimension(current, outcome))


def save_plan(db, value, *, new=False):
    if new:
        db.execute(
            "INSERT INTO field_plans VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                value.plan_id,
                value.case_id,
                value.task_id,
                value.review_revision,
                value.revision,
                value.status,
                int(value.simulated),
                value.created_at.isoformat(),
                value.updated_at.isoformat(),
                cases.case_row(db, value.case_id)["revision"] + 1,
            ),
        )
    else:
        db.execute(
            "UPDATE field_plans SET current_revision=?,status=?,updated_at=? "
            "WHERE case_id=? AND plan_id=?",
            (
                value.revision,
                value.status,
                value.updated_at.isoformat(),
                value.case_id,
                value.plan_id,
            ),
        )
    db.execute(
        "INSERT INTO field_plan_revisions VALUES(?,?,?,?,?,?,?)",
        (
            value.case_id,
            value.plan_id,
            value.revision,
            value.task_id,
            value.review_revision,
            encode(asdict(value)),
            value.updated_at.isoformat(),
        ),
    )


def save_report(db, value, *, new=False):
    if new:
        db.execute(
            "INSERT INTO field_reports VALUES(?,?,?,?,?,?,?,?,?)",
            (
                value.report_id,
                value.case_id,
                value.plan_id,
                value.performed_plan_revision,
                value.revision,
                value.outcome,
                int(value.simulated),
                value.created_at.isoformat(),
                value.updated_at.isoformat(),
            ),
        )
    else:
        db.execute(
            "UPDATE field_reports SET current_revision=?,outcome=?,updated_at=? "
            "WHERE case_id=? AND report_id=?",
            (
                value.revision,
                value.outcome,
                value.updated_at.isoformat(),
                value.case_id,
                value.report_id,
            ),
        )
    db.execute(
        "INSERT INTO field_report_revisions VALUES(?,?,?,?,?,?,?)",
        (
            value.case_id,
            value.report_id,
            value.revision,
            value.plan_id,
            value.performed_plan_revision,
            encode(asdict(value)),
            value.updated_at.isoformat(),
        ),
    )
