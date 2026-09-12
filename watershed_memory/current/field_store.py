"""Atomic case-bound field work and original receipts across restart and retries."""

from dataclasses import asdict

from . import case_records as cases
from . import field_actions as actions
from . import field_records as rows
from .case_store import CaseStore, _expected
from .case_types import WorkflowConflict, _identifier
from .context import _case_config
from .fact_validation import utc
from .field_codec import encode
from .field_models import FieldMutationReceipt
from .field_receipts import restore as restore_receipt
from .field_receipts import save_membership
from .field_schema import ensure_schema
from .field_types import (
    AttachFieldEvidence,
    CorrectFieldResult,
    DecideFieldPlan,
    FieldPrincipal,
    ModifyFieldPlan,
    ProposeFieldPlan,
    ReportFieldResult,
    VerifyFieldReport,
    _revision,
)
from .locations import LocationRegistry

_OPERATIONS = {
    "PROPOSE": (ProposeFieldPlan, ("COORDINATOR",), actions.propose),
    "DECIDE": (DecideFieldPlan, ("COORDINATOR",), actions.decide),
    "MODIFY": (ModifyFieldPlan, ("COORDINATOR",), actions.modify),
    "REPORT": (ReportFieldResult, ("FIELD_OPERATOR",), actions.report),
    "CORRECT": (CorrectFieldResult, ("FIELD_OPERATOR",), actions.correct),
    "ATTACH": (AttachFieldEvidence, ("FIELD_OPERATOR", "VERIFIER"), actions.attach),
    "VERIFY": (VerifyFieldReport, ("VERIFIER",), actions.verify),
}


def _preflight(operation, case_id, command, *, principal, request_id, expected_case_revision, now):
    command_type, roles, handler = _OPERATIONS[operation]
    if type(command) is not command_type or type(principal) is not FieldPrincipal:
        raise ValueError("invalid field command or trusted principal")
    _identifier(case_id, "case_id")
    _identifier(request_id, "request_id")
    _expected(expected_case_revision)
    now = utc(now)
    if case_id not in principal.case_ids or not set(principal.roles).intersection(roles):
        raise ValueError("principal is not authorized for this field operation and case")
    return now, handler


class FieldStore:
    def __init__(self, case_store: CaseStore, locations: LocationRegistry):
        if type(case_store) is not CaseStore or type(locations) is not LocationRegistry:
            raise ValueError("field work requires a case store and trusted location registry")
        self.cases, self.locations = case_store, locations
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)

    def _write(self, operation, case_id, command, **arguments):
        _preflight(operation, case_id, command, **arguments)
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._mutate(db, operation, case_id, command, **arguments)

    def _mutate(
        self, db, operation, case_id, command, *, principal, request_id, expected_case_revision, now
    ):
        if not db.in_transaction:
            raise ValueError("field mutation requires an explicit transaction")
        now, handler = _preflight(
            operation,
            case_id,
            command,
            principal=principal,
            request_id=request_id,
            expected_case_revision=expected_case_revision,
            now=now,
        )
        case = cases.case_row(db, case_id)
        _case_config(case)
        encoded = encode(
            {
                "operation": operation,
                "case_id": case_id,
                "policy_digest": case["policy_digest"],
                "command": asdict(command),
                "principal": asdict(principal),
                "expected_case_revision": expected_case_revision,
            },
            limit=65536,
        )
        prior = db.execute(
            "SELECT * FROM field_receipts WHERE case_id=? AND request_id=?", (case_id, request_id)
        ).fetchone()
        if prior is not None:
            if prior["input_hash"] != cases.digest(encoded) or prior["input_json"] != encoded:
                raise WorkflowConflict("request ID was already used for different field input")
            return restore_receipt(db, prior, case)
        cases.check_revision(case, expected_case_revision)
        cases.check_time(case, now)
        plan, result, evidence, verification = handler(
            db, case, command, principal, now, self.locations
        )
        receipt = FieldMutationReceipt(
            request_id,
            operation,
            case_id,
            case["revision"] + 1,
            plan,
            now,
            result,
            evidence,
            verification,
        )
        db.execute(
            "UPDATE current_cases SET revision=revision+1,updated_at=? WHERE case_id=?",
            (now.isoformat(), case_id),
        )
        db.execute(
            "UPDATE field_plans SET last_activity_revision=? WHERE case_id=? AND plan_id=?",
            (receipt.case_revision, case_id, plan.plan_id),
        )
        result_json = encode(asdict(receipt))
        db.execute(
            "INSERT INTO field_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                request_id,
                operation,
                cases.digest(encoded),
                encoded,
                result_json,
                cases.digest(result_json),
                receipt.case_revision,
                plan.plan_id,
                plan.revision,
                now.isoformat(),
            ),
        )
        save_membership(db, receipt)
        return receipt

    def propose_plan(self, case_id, command, *, principal, request_id, expected_case_revision, now):
        return self._write(
            "PROPOSE",
            case_id,
            command,
            principal=principal,
            request_id=request_id,
            expected_case_revision=expected_case_revision,
            now=now,
        )

    def decide_plan(self, case_id, command, *, principal, request_id, expected_case_revision, now):
        return self._write(
            "DECIDE",
            case_id,
            command,
            principal=principal,
            request_id=request_id,
            expected_case_revision=expected_case_revision,
            now=now,
        )

    def modify_plan(self, case_id, command, *, principal, request_id, expected_case_revision, now):
        return self._write(
            "MODIFY",
            case_id,
            command,
            principal=principal,
            request_id=request_id,
            expected_case_revision=expected_case_revision,
            now=now,
        )

    def report_result(
        self, case_id, command, *, principal, request_id, expected_case_revision, now
    ):
        return self._write(
            "REPORT",
            case_id,
            command,
            principal=principal,
            request_id=request_id,
            expected_case_revision=expected_case_revision,
            now=now,
        )

    def correct_result(
        self, case_id, command, *, principal, request_id, expected_case_revision, now
    ):
        return self._write(
            "CORRECT",
            case_id,
            command,
            principal=principal,
            request_id=request_id,
            expected_case_revision=expected_case_revision,
            now=now,
        )

    def attach_evidence(
        self, case_id, command, *, principal, request_id, expected_case_revision, now
    ):
        return self._write(
            "ATTACH",
            case_id,
            command,
            principal=principal,
            request_id=request_id,
            expected_case_revision=expected_case_revision,
            now=now,
        )

    def verify_result(
        self, case_id, command, *, principal, request_id, expected_case_revision, now
    ):
        return self._write(
            "VERIFY",
            case_id,
            command,
            principal=principal,
            request_id=request_id,
            expected_case_revision=expected_case_revision,
            now=now,
        )

    def get_plan(self, case_id, plan_id, *, revision=None):
        _identifier(case_id, "case_id")
        _identifier(plan_id, "plan_id")
        if revision is not None:
            _revision(revision)
        with self.cases._connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            return rows.plan(db, case_id, plan_id, revision)

    def get_result(self, case_id, report_id, *, revision=None):
        _identifier(case_id, "case_id")
        _identifier(report_id, "report_id")
        if revision is not None:
            _revision(revision)
        with self.cases._connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            return rows.result(db, case_id, report_id, revision)

    def current_for_review(self, case_id, task_id):
        _identifier(case_id, "case_id")
        _identifier(task_id, "task_id")
        with self.cases._connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            cases.review_row(db, case_id, task_id)
            raw = db.execute(
                "SELECT plan_id FROM field_plans WHERE case_id=? AND task_id=? "
                "AND status IN ('PROPOSED','APPROVED','DEFERRED') LIMIT 1",
                (case_id, task_id),
            ).fetchone()
            if raw is None:
                raw = db.execute(
                    "SELECT plan_id FROM field_plans WHERE case_id=? AND task_id=? "
                    "ORDER BY last_activity_revision DESC LIMIT 1",
                    (case_id, task_id),
                ).fetchone()
            return rows.snapshot(db, case_id, raw[0]) if raw else None

    def recent(self, case_id, *, limit=20):
        _identifier(case_id, "case_id")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("field history limit must be between 1 and 100")
        with self.cases._connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            cases.case_row(db, case_id)
            ids = db.execute(
                "SELECT plan_id FROM field_plans WHERE case_id=? "
                "ORDER BY last_activity_revision DESC LIMIT ?",
                (case_id, limit),
            ).fetchall()
            return tuple(rows.snapshot(db, case_id, raw[0]) for raw in ids)
