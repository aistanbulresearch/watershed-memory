"""Transactional field-ledger tests using isolated SQLite trigger failures."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from threading import Barrier

import pytest
from test_current_case_store import ready  # noqa: F401
from test_current_field_store import (
    CANARY,
    COORDINATOR,
    NOW,
    OPERATOR,
    SPEC,
    VERIFIER,
    approved,
    attached,
    mutate,
    proposed,
    reported,
)
from test_current_field_store import field as field

from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.field_types import (
    AttachFieldEvidence,
    CorrectFieldResult,
    DecideFieldPlan,
    ModifyFieldPlan,
    ProposeFieldPlan,
    ReportFieldResult,
    VerifyFieldReport,
)


def _contents(path):
    with closing(sqlite3.connect(path)) as db:
        names = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in names
        }


def _abort_receipts(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute("""CREATE TRIGGER abort_field_receipt BEFORE INSERT ON field_receipts
                     BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END""")
        db.commit()


def _abort_table(path, table, event):
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"""CREATE TRIGGER abort_{table} BEFORE {event} ON {table}
                     BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END""")
        db.commit()


def _assert_rollback(field, operation):
    path, _, store, review = field
    before = _contents(path)
    _abort_receipts(path)
    with pytest.raises(sqlite3.IntegrityError, match="injected transaction failure"):
        operation(store, review)
    assert _contents(path) == before


def test_propose_rolls_back_revision_and_immutable_rows(field):
    def operation(store, review):
        mutate(
            field,
            "propose_plan",
            ProposeFieldPlan(
                review.task_id,
                review.revision,
                "demo-site",
                1,
                SPEC,
                True,
                CANARY,
            ),
        )

    _assert_rollback(field, operation)


def test_decide_rolls_back_after_plan_append(field):
    proposal = proposed(field)

    def operation(store, review):
        mutate(field, "decide_plan", DecideFieldPlan(proposal.plan.plan_id, 1, "APPROVE"))

    _assert_rollback(field, operation)


def test_modify_rolls_back_full_replacement(field):
    proposal = proposed(field)

    def operation(store, review):
        mutate(
            field,
            "modify_plan",
            ModifyFieldPlan(
                proposal.plan.plan_id,
                1,
                "demo-site",
                1,
                SPEC,
                True,
            ),
        )

    _assert_rollback(field, operation)


def test_report_rolls_back_case_revision_and_report_history(field):
    plan = approved(field).plan

    def operation(store, review):
        mutate(
            field,
            "report_result",
            ReportFieldResult(
                plan.plan_id,
                plan.revision,
                "COMPLETE",
                "Field result recorded for review.",
                plan.spec.window_start,
                plan.spec.window_start,
                True,
                CANARY,
            ),
            who=OPERATOR,
            now=NOW + timedelta(minutes=20),
        )

    _assert_rollback(field, operation)


def test_correct_rolls_back_report_revision(field):
    result = reported(field).result

    def operation(store, review):
        mutate(
            field,
            "correct_result",
            CorrectFieldResult(
                result.report.report_id,
                1,
                "PARTIAL",
                "Corrected field result recorded.",
                result.report.performed_start,
                result.report.performed_end,
                True,
            ),
            who=OPERATOR,
            now=result.report.updated_at,
        )

    _assert_rollback(field, operation)


def test_attach_rolls_back_evidence_and_receipt(field):
    result = reported(field).result

    def operation(store, review):
        mutate(
            field,
            "attach_evidence",
            AttachFieldEvidence(
                result.report.report_id,
                1,
                "OPERATOR_RECORD_REFERENCE",
                "INSPECTION_RECORD_REFERENCE",
                "field-record-1",
                "Attributed field inspection record.",
                result.report.performed_end,
                None,
                True,
                CANARY,
            ),
            who=OPERATOR,
            now=NOW + timedelta(minutes=21),
        )

    _assert_rollback(field, operation)


def test_verify_rolls_back_verification_and_receipt(field):
    result = attached(field).result

    def operation(store, review):
        mutate(
            field,
            "verify_result",
            VerifyFieldReport(
                result.report.report_id,
                1,
                tuple(item.evidence_id for item in result.evidence),
                "Verifier reviewed the attached field record.",
                True,
                CANARY,
            ),
            who=VERIFIER,
            now=result.report.updated_at + timedelta(minutes=2),
        )

    _assert_rollback(field, operation)


def test_same_revision_decisions_have_one_winner(field):
    proposal = proposed(field)
    command = DecideFieldPlan(proposal.plan.plan_id, 1, "APPROVE")
    first = mutate(field, "decide_plan", command, request="decision-a")
    with pytest.raises(WorkflowConflict):
        mutate(
            field, "decide_plan", command, request="decision-b", revision=first.case_revision - 1
        )
    assert first.plan.revision == 2


def test_concurrent_decisions_have_one_commit_and_one_conflict(field):
    path, cases, _, _ = field
    proposal = proposed(field)
    expected = cases.context("FIELD-DEMO")["revision"]
    barrier = Barrier(2)

    def attempt(request_id):
        from test_current_field_store import SITE

        from watershed_memory.current.case_store import CaseStore
        from watershed_memory.current.field_store import FieldStore
        from watershed_memory.current.locations import LocationRegistry

        store = FieldStore(CaseStore(path), LocationRegistry((SITE,)))
        barrier.wait()
        try:
            return store.decide_plan(
                "FIELD-DEMO",
                DecideFieldPlan(proposal.plan.plan_id, 1, "APPROVE"),
                principal=COORDINATOR,
                request_id=request_id,
                expected_case_revision=expected,
                now=NOW,
            )
        except WorkflowConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("concurrent-a", "concurrent-b")))
    assert sum(result is not None for result in results) == 1
    assert cases.context("FIELD-DEMO")["revision"] == expected + 1


def test_current_case_update_failure_rolls_back(field):
    path, _, _, _ = field
    before = _contents(path)
    _abort_table(path, "current_cases", "UPDATE")
    with pytest.raises(sqlite3.IntegrityError):
        proposed(field)
    assert _contents(path) == before


def test_plan_revision_insert_failure_rolls_back(field):
    path, _, _, _ = field
    before = _contents(path)
    _abort_table(path, "field_plan_revisions", "INSERT")
    with pytest.raises(sqlite3.IntegrityError):
        proposed(field)
    assert _contents(path) == before
