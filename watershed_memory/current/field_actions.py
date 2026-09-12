"""Field transition handlers. No handler commits or performs external work."""

import uuid
from dataclasses import asdict, replace
from datetime import timedelta

from . import case_records as cases
from . import field_records as rows
from .case_types import WorkflowConflict
from .field_codec import encode
from .field_models import (
    FieldEvidenceRecord,
    FieldPlanRecord,
    FieldReportRecord,
    FieldVerificationRecord,
)


def _id(kind):
    return f"field-{kind}-{uuid.uuid4().hex}"


def _simulation(case, expected):
    if expected != bool(case["simulated"]):
        raise WorkflowConflict("field command simulation differs from its case")


def _parent(db, case_id, task_id, revision):
    parent = cases.review_row(db, case_id, task_id)
    if (
        parent["revision"] != revision
        or parent["status"] not in ("PROPOSED", "APPROVED", "DEFERRED")
        or parent["kind"] not in ("OBSERVATION_REVIEW", "COVERAGE_REVIEW")
    ):
        raise WorkflowConflict("field work is bound to a superseded or closed review")


def _target(db, case, command):
    value = rows.plan(db, case["case_id"], command.plan_id)
    if value.revision != command.expected_plan_revision:
        raise WorkflowConflict("field plan changed after this decision was prepared")
    if value.status not in ("PROPOSED", "APPROVED", "DEFERRED"):
        raise WorkflowConflict("field plan no longer accepts decisions")
    return value


def _window(spec, now):
    if spec.window_start < now or spec.window_end - now > timedelta(days=30):
        raise ValueError("field work must be planned within the next 30 days")


def _place(locations, case, location_id, revision, spec, now):
    return locations.require_approved(
        location_id,
        revision,
        case_id=case["case_id"],
        simulated=bool(case["simulated"]),
        activity=spec.activity,
        now=now,
    )


def propose(db, case, command, principal, now, locations):
    _simulation(case, command.expected_simulated)
    _parent(db, case["case_id"], command.task_id, command.expected_review_revision)
    _window(command.spec, now)
    place = _place(
        locations, case, command.location_id, command.location_revision, command.spec, now
    )
    if db.execute(
        "SELECT 1 FROM field_plans WHERE case_id=? AND task_id=? "
        "AND status IN ('PROPOSED','APPROVED','DEFERRED') LIMIT 1",
        (case["case_id"], command.task_id),
    ).fetchone():
        raise WorkflowConflict("this review already has active field work")
    value = FieldPlanRecord(
        _id("plan"),
        case["case_id"],
        command.task_id,
        command.expected_review_revision,
        1,
        "PROPOSED",
        command.spec,
        place,
        bool(case["simulated"]),
        "PROPOSE",
        "HUMAN",
        principal.principal_id,
        None,
        now,
        now,
    )
    rows.save_plan(db, value, new=True)
    return value, None, None, None


def decide(db, case, command, principal, now, locations):
    value = _target(db, case, command)
    if command.action != "CANCEL":
        _parent(db, case["case_id"], value.task_id, value.review_revision)
        if value.spec.window_end <= now:
            raise WorkflowConflict("the field planning window has expired")
    if command.action == "APPROVE":
        if value.status == "APPROVED":
            raise WorkflowConflict("field plan is already approved")
        _place(
            locations, case, value.location.location_id, value.location.revision, value.spec, now
        )
    if command.action == "DEFER" and not (
        now < command.defer_until <= now + timedelta(days=30)
        and command.defer_until < value.spec.window_end
    ):
        raise ValueError("field deferral must be before the planned window ends")
    status = {"APPROVE": "APPROVED", "DEFER": "DEFERRED", "CANCEL": "CANCELLED"}[command.action]
    changed = replace(
        value,
        revision=value.revision + 1,
        status=status,
        change_kind=command.action,
        recorded_by=principal.principal_id,
        author_kind="HUMAN",
        updated_at=now,
        deferred_until=command.defer_until,
    )
    rows.save_plan(db, changed)
    return changed, None, None, None


def modify(db, case, command, principal, now, locations):
    value = _target(db, case, command)
    _simulation(case, command.expected_simulated)
    _parent(db, case["case_id"], value.task_id, value.review_revision)
    _window(command.spec, now)
    place = _place(
        locations, case, command.location_id, command.location_revision, command.spec, now
    )
    changed = replace(
        value,
        revision=value.revision + 1,
        status="APPROVED",
        spec=command.spec,
        location=place,
        change_kind="MODIFY",
        recorded_by=principal.principal_id,
        author_kind="HUMAN",
        updated_at=now,
        deferred_until=None,
    )
    rows.save_plan(db, changed)
    return changed, None, None, None


def _performed(command, approved, now):
    if command.performed_start < approved.updated_at or command.performed_end > now:
        raise ValueError("field performance must follow approval and precede reporting")


def report(db, case, command, principal, now, locations):
    value = rows.plan(db, case["case_id"], command.plan_id)
    _simulation(case, command.expected_simulated)
    if value.status != "APPROVED" or value.revision != command.performed_plan_revision:
        raise WorkflowConflict("field report requires the exact current approved plan")
    _parent(db, case["case_id"], value.task_id, value.review_revision)
    _performed(command, value, now)
    item = FieldReportRecord(
        _id("report"),
        case["case_id"],
        value.plan_id,
        value.revision,
        1,
        command.outcome,
        command.summary,
        command.performed_start,
        command.performed_end,
        bool(case["simulated"]),
        "REPORT",
        principal.principal_id,
        now,
        now,
    )
    changed = replace(
        value,
        revision=value.revision + 1,
        status="REPORTED",
        change_kind="REPORT",
        recorded_by=principal.principal_id,
        author_kind="HUMAN",
        updated_at=now,
    )
    rows.save_report(db, item, new=True)
    rows.save_plan(db, changed)
    return changed, rows.result(db, case["case_id"], item.report_id), None, None


def _current_result(db, case, command):
    _simulation(case, command.expected_simulated)
    value = rows.result(db, case["case_id"], command.report_id)
    if value.report.revision != command.expected_report_revision:
        raise WorkflowConflict("field report changed after this operation was prepared")
    return value


def correct(db, case, command, principal, now, locations):
    value = _current_result(db, case, command)
    old = value.report
    approved = rows.plan(db, case["case_id"], old.plan_id, old.performed_plan_revision)
    _performed(command, approved, now)
    changed = replace(
        old,
        revision=old.revision + 1,
        outcome=command.outcome,
        summary=command.summary,
        performed_start=command.performed_start,
        performed_end=command.performed_end,
        change_kind="CORRECTION",
        recorded_by=principal.principal_id,
        updated_at=now,
    )
    rows.save_report(db, changed)
    return (
        rows.plan(db, case["case_id"], old.plan_id),
        rows.result(db, case["case_id"], old.report_id),
        None,
        None,
    )


def attach(db, case, command, principal, now, locations):
    value = _current_result(db, case, command)
    if len(value.evidence) >= 8:
        raise WorkflowConflict("this report revision already has eight evidence references")
    item = FieldEvidenceRecord(
        _id("evidence"),
        case["case_id"],
        command.report_id,
        command.expected_report_revision,
        command.reference_kind,
        command.evidence_category,
        command.reference,
        command.provenance,
        command.observed_at,
        command.sha256,
        bool(case["simulated"]),
        principal.principal_id,
        now,
    )
    digest = rows.evidence_digest(item)
    if any(rows.evidence_digest(e) == digest for e in value.evidence):
        raise WorkflowConflict(
            "this evidence reference is already attached to this report revision"
        )
    db.execute(
        "INSERT INTO field_evidence VALUES(?,?,?,?,?,?,?)",
        (
            item.evidence_id,
            item.case_id,
            item.report_id,
            item.report_revision,
            digest,
            encode(asdict(item)),
            now.isoformat(),
        ),
    )
    return (
        rows.plan(db, case["case_id"], value.report.plan_id),
        rows.result(db, case["case_id"], command.report_id),
        item,
        None,
    )


def verify(db, case, command, principal, now, locations):
    value = _current_result(db, case, command)
    if command.evidence_ids != tuple(e.evidence_id for e in value.evidence):
        raise WorkflowConflict("verification must cover the exact current evidence set")
    item = FieldVerificationRecord(
        _id("verification"),
        case["case_id"],
        command.report_id,
        command.expected_report_revision,
        command.evidence_ids,
        command.scope,
        "AUTHORIZED_HUMAN_REVIEW",
        bool(case["simulated"]),
        principal.principal_id,
        now,
    )
    db.execute(
        "INSERT INTO field_verifications(verification_id,case_id,report_id,report_revision,"
        "evidence_ids_json,record_json,recorded_at) VALUES(?,?,?,?,?,?,?)",
        (
            item.verification_id,
            item.case_id,
            item.report_id,
            item.report_revision,
            encode(item.evidence_ids),
            encode(asdict(item)),
            now.isoformat(),
        ),
    )
    return (
        rows.plan(db, case["case_id"], value.report.plan_id),
        rows.result(db, case["case_id"], command.report_id),
        None,
        item,
    )
