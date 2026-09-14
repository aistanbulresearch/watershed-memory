"""Reconcile original receipts with exact immutable plan/result/evidence records."""

from . import case_records as cases
from . import field_records as rows
from .fact_validation import timestamp
from .field_codec import decode, encode
from .field_models import FieldMutationReceipt, FieldVerificationRecord


def restore(db, raw, case):
    if cases.digest(raw["result_json"]) != raw["result_hash"]:
        raise ValueError("field receipt content differs from its saved digest")
    value = decode(raw["result_json"], FieldMutationReceipt)
    if (
        (
            value.case_id,
            value.request_id,
            value.operation,
            value.case_revision,
            value.plan.plan_id,
            value.plan.revision,
            value.recorded_at.isoformat(),
        )
        != (
            raw["case_id"],
            raw["request_id"],
            raw["operation"],
            raw["case_revision"],
            raw["plan_id"],
            raw["plan_revision"],
            raw["recorded_at"],
        )
        or value.case_revision > case["revision"]
        or value.recorded_at > timestamp(case["updated_at"])
    ):
        raise ValueError("field receipt differs from its saved identity")
    if rows.plan(db, value.case_id, value.plan.plan_id, value.plan.revision) != value.plan:
        raise ValueError("field receipt plan differs from its immutable revision")
    snapshot = value.result
    member = db.execute(
        "SELECT * FROM field_receipt_results WHERE case_id=? AND request_id=?",
        (value.case_id, value.request_id),
    ).fetchone()
    if snapshot is None:
        if member is not None:
            raise ValueError("field receipt omitted its saved result")
        return value
    if member is None or (
        member["report_id"],
        member["report_revision"],
        member["verification_id"],
        member["evidence_id"],
    ) != (
        snapshot.report.report_id,
        snapshot.report.revision,
        snapshot.verification.verification_id if snapshot.verification else None,
        value.evidence.evidence_id if value.evidence else None,
    ):
        raise ValueError("field receipt result differs from its exact saved membership")
    members = db.execute(
        "SELECT evidence_id,report_id,report_revision FROM field_receipt_evidence "
        "WHERE case_id=? AND request_id=? ORDER BY evidence_id LIMIT 9",
        (value.case_id, value.request_id),
    ).fetchall()
    if tuple(tuple(row) for row in members) != tuple(
        (e.evidence_id, e.report_id, e.report_revision) for e in snapshot.evidence
    ):
        raise ValueError("field receipt evidence differs from its complete saved membership")
    recorded = rows.result(db, value.case_id, snapshot.report.report_id, snapshot.report.revision)
    if recorded.report != snapshot.report or snapshot.report.updated_at > value.recorded_at:
        raise ValueError("field receipt report differs from its immutable revision")
    actual = {item.evidence_id: item for item in recorded.evidence}
    for item in snapshot.evidence:
        if actual.get(item.evidence_id) != item or item.attached_at > value.recorded_at:
            raise ValueError("field receipt evidence differs from its immutable record")
    verification = snapshot.verification
    if verification is not None:
        raw_verification = db.execute(
            "SELECT * FROM field_verifications WHERE case_id=? AND verification_id=?",
            (value.case_id, verification.verification_id),
        ).fetchone()
        if (
            raw_verification is None
            or decode(raw_verification["record_json"], FieldVerificationRecord) != verification
            or verification.report_id != raw_verification["report_id"]
            or verification.report_revision != raw_verification["report_revision"]
            or verification.verified_at.isoformat() != raw_verification["recorded_at"]
            or encode(verification.evidence_ids) != raw_verification["evidence_ids_json"]
            or verification.verified_at > value.recorded_at
        ):
            raise ValueError("field receipt verification differs from its immutable record")
    return value


def save_membership(db, receipt):
    snapshot = receipt.result
    if snapshot is None:
        return
    db.execute(
        "INSERT INTO field_receipt_results VALUES(?,?,?,?,?,?)",
        (
            receipt.case_id,
            receipt.request_id,
            snapshot.report.report_id,
            snapshot.report.revision,
            snapshot.verification.verification_id if snapshot.verification else None,
            receipt.evidence.evidence_id if receipt.evidence else None,
        ),
    )
    db.executemany(
        "INSERT INTO field_receipt_evidence VALUES(?,?,?,?,?)",
        [
            (receipt.case_id, receipt.request_id, e.report_id, e.report_revision, e.evidence_id)
            for e in snapshot.evidence
        ],
    )
