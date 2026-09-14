"""Field work follows an exact human decision through its reported and verified result."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from test_current_case_store import MONITOR, NOW, POLICY, ready, stage  # noqa: F401

from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.case_types import CaseConfig, HumanAction, WorkflowConflict
from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.field_types import (
    AttachFieldEvidence,
    CorrectFieldResult,
    DecideFieldPlan,
    FieldPlanSpec,
    FieldPrincipal,
    ModifyFieldPlan,
    ProposeFieldPlan,
    ReportFieldResult,
    VerifyFieldReport,
)
from watershed_memory.current.locations import LocationEntry, LocationRegistry

CASE = "FIELD-DEMO"
CANARY = "PRIVATE-FIELD-NOTE-CANARY"
COORDINATOR = FieldPrincipal("demo-coordinator", ("COORDINATOR",), (CASE,))
OPERATOR = FieldPrincipal("demo-operator", ("FIELD_OPERATOR",), (CASE,))
VERIFIER = FieldPrincipal("demo-verifier", ("VERIFIER",), (CASE,))
SPEC = FieldPlanSpec(
    "VISUAL_INSPECTION",
    "Inspect the accessible sediment marker.",
    "Field coordinator",
    NOW,
    NOW + timedelta(hours=2),
    ("INSPECTION_RECORD_REFERENCE",),
)
SITE = LocationEntry(
    "demo-site",
    1,
    "Demonstration inspection site",
    "FIELD_SITE",
    CASE,
    True,
    "APPROVED",
    ("VISUAL_INSPECTION",),
    None,
    None,
    None,
    None,
    None,
    "Demonstration site configured for workflow tests",
    NOW,
    "demo-site-approver",
    NOW,
)


@pytest.fixture
def field(ready):  # noqa: F811 - pytest fixture imported from the case ledger suite
    path, cases, watch, events = ready
    cases.register(CaseConfig(CASE, MONITOR, POLICY, True), now=NOW)
    review = stage(cases, events[0], case=CASE)
    store = FieldStore(cases, LocationRegistry((SITE,)))
    return path, cases, store, review


def mutate(field, method, command, *, who=COORDINATOR, request=None, now=NOW, revision=None):
    _, cases, store, _ = field
    revision = cases.context(CASE)["revision"] if revision is None else revision
    return getattr(store, method)(
        CASE,
        command,
        principal=who,
        request_id=request or f"request-{revision}",
        expected_case_revision=revision,
        now=now,
    )


def proposed(field):
    review = field[3]
    return mutate(
        field,
        "propose_plan",
        ProposeFieldPlan(review.task_id, review.revision, SITE.location_id, 1, SPEC, True, CANARY),
    )


def approved(field):
    plan = proposed(field).plan
    return mutate(field, "decide_plan", DecideFieldPlan(plan.plan_id, plan.revision, "APPROVE"))


def reported(field, outcome="COMPLETE"):
    plan = approved(field).plan
    return mutate(
        field,
        "report_result",
        ReportFieldResult(
            plan.plan_id,
            plan.revision,
            outcome,
            "Demonstration field inspection result.",
            NOW,
            NOW + timedelta(minutes=10),
            True,
            CANARY,
        ),
        who=OPERATOR,
        now=NOW + timedelta(minutes=15),
    )


def attached(field, outcome="COMPLETE"):
    report = reported(field, outcome).result.report
    return mutate(
        field,
        "attach_evidence",
        AttachFieldEvidence(
            report.report_id,
            1,
            "OPERATOR_RECORD_REFERENCE",
            "INSPECTION_RECORD_REFERENCE",
            "demo-inspection-1",
            "Demonstration externally maintained inspection record.",
            NOW,
            None,
            True,
            CANARY,
        ),
        who=OPERATOR,
        now=NOW + timedelta(minutes=16),
    )


def verified(field, outcome="COMPLETE"):
    result = attached(field, outcome).result
    return mutate(
        field,
        "verify_result",
        VerifyFieldReport(
            result.report.report_id,
            1,
            tuple(e.evidence_id for e in result.evidence),
            "Demonstration review of inspection scope.",
            True,
            CANARY,
        ),
        who=VERIFIER,
        now=NOW + timedelta(minutes=17),
    )


def contents(path):
    with closing(sqlite3.connect(path)) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in names
        }


def test_full_cycle_survives_reopen_and_private_notes_stay_private(field):
    path, cases, store, review = field
    before = contents(path)
    case_revision = cases.context(CASE)["revision"]
    receipt = verified(field)
    result = receipt.result
    assert result.verification_level == "VERIFIED"
    assert result.report.outcome == "COMPLETE" and result.report.simulated
    assert result.verification.verified_by == VERIFIER.principal_id
    assert result.report.performed_plan_revision == 2
    assert receipt.plan.revision == 3 and receipt.plan.status == "REPORTED"
    snapshot = store.current_for_review(CASE, review.task_id)
    assert snapshot.field_dimension == "VERIFIED_COMPLETE"
    reopened = FieldStore(CaseStore(path), LocationRegistry((SITE,)))
    assert reopened.current_for_review(CASE, review.task_id) == snapshot
    assert reopened.get_plan(CASE, receipt.plan.plan_id, revision=2).status == "APPROVED"
    assert reopened.get_result(CASE, result.report.report_id) == result
    assert reopened.recent(CASE) == (snapshot,)
    encoded = json.dumps(asdict(snapshot), default=str)
    assert CANARY not in encoded and CANARY not in json.dumps(asdict(receipt), default=str)
    after = contents(path)
    assert CANARY in str(after["field_receipts"])
    assert cases.context(CASE)["revision"] == case_revision + 5
    for name in before:
        if name != "current_cases" and not name.startswith("field_"):
            assert before[name] == after[name], name
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("outcome", ["PARTIAL", "NOT_DONE"])
def test_verified_incomplete_work_never_becomes_complete(field, outcome):
    receipt = verified(field, outcome)
    snapshot = field[2].current_for_review(CASE, field[3].task_id)
    assert receipt.result.verification_level == "VERIFIED"
    assert snapshot.result.report.outcome == outcome and snapshot.field_dimension == "PLANNED"


def test_correction_drops_verification_but_preserves_exact_history(field):
    original = verified(field).result
    current = mutate(
        field,
        "correct_result",
        CorrectFieldResult(
            original.report.report_id,
            1,
            "PARTIAL",
            "Correction: the far marker could not be inspected.",
            NOW,
            NOW + timedelta(minutes=10),
            True,
        ),
        who=OPERATOR,
        now=NOW + timedelta(minutes=18),
    ).result
    assert current.report.revision == 2 and current.verification_level == "REPORTED"
    assert current.evidence == () and current.verification is None
    assert field[2].get_result(CASE, original.report.report_id, revision=1) == original


def test_new_evidence_requires_verification_of_expanded_exact_set(field):
    original = verified(field).result
    current = mutate(
        field,
        "attach_evidence",
        AttachFieldEvidence(
            original.report.report_id,
            1,
            "OPERATOR_RECORD_REFERENCE",
            "PHOTO_REFERENCE",
            "demo-photo-2",
            "Demonstration photograph reference supplied by operator.",
            NOW,
            "a" * 64,
            True,
        ),
        who=VERIFIER,
        now=NOW + timedelta(minutes=18),
    ).result
    assert current.verification_level == "EVIDENCE_ATTACHED" and current.verification is None
    before = contents(field[0])
    with pytest.raises(WorkflowConflict):
        mutate(
            field,
            "verify_result",
            VerifyFieldReport(
                original.report.report_id,
                1,
                original.verification.evidence_ids,
                "Review of the earlier evidence only.",
                True,
            ),
            who=VERIFIER,
            now=NOW + timedelta(minutes=19),
        )
    assert contents(field[0]) == before


def test_receipt_retry_after_later_edit_returns_original_without_mutation(field):
    review = field[3]
    command = ProposeFieldPlan(review.task_id, 1, SITE.location_id, 1, SPEC, True, CANARY)
    revision = field[1].context(CASE)["revision"]
    first = mutate(field, "propose_plan", command, request="exact-retry", revision=revision)
    mutate(field, "decide_plan", DecideFieldPlan(first.plan.plan_id, 1, "CANCEL"))
    before = contents(field[0])
    assert (
        mutate(
            field,
            "propose_plan",
            command,
            request="exact-retry",
            revision=revision,
            now=NOW - timedelta(days=1),
        )
        == first
    )
    assert contents(field[0]) == before
    with pytest.raises(WorkflowConflict):
        mutate(
            field,
            "propose_plan",
            replace(command, private_note="changed"),
            request="exact-retry",
            revision=revision,
        )


@pytest.mark.parametrize("action", ["MODIFY", "CANCEL", "DISMISS"])
def test_parent_review_change_blocks_reporting_but_allows_plan_retirement(field, action):
    plan = approved(field).plan
    parent = field[3]
    field[1].act(
        CASE,
        HumanAction(
            parent.task_id,
            parent.revision,
            action,
            title="Changed field review" if action == "MODIFY" else None,
            next_check_at=NOW + timedelta(hours=1) if action == "MODIFY" else None,
        ),
        request_id="parent-change",
        now=NOW,
    )
    before = contents(field[0])
    with pytest.raises(WorkflowConflict):
        mutate(
            field,
            "report_result",
            ReportFieldResult(
                plan.plan_id,
                plan.revision,
                "COMPLETE",
                "Report for an outdated parent decision.",
                NOW,
                NOW,
                True,
            ),
            who=OPERATOR,
        )
    assert contents(field[0]) == before
    snapshot = field[2].current_for_review(CASE, parent.task_id)
    assert snapshot.parent_binding == ("SUPERSEDED" if action == "MODIFY" else "TERMINAL")
    cancelled = mutate(field, "decide_plan", DecideFieldPlan(plan.plan_id, plan.revision, "CANCEL"))
    assert cancelled.plan.status == "CANCELLED"


@pytest.mark.parametrize(
    "who", [OPERATOR, VERIFIER, replace(COORDINATOR, case_ids=("wrong-case",))]
)
def test_wrong_role_or_scope_has_no_writes(field, who):
    before = contents(field[0])
    review = field[3]
    with pytest.raises(ValueError):
        mutate(
            field,
            "propose_plan",
            ProposeFieldPlan(review.task_id, 1, SITE.location_id, 1, SPEC, True),
            who=who,
        )
    assert contents(field[0]) == before


def test_withdrawn_site_cannot_be_approved_from_old_snapshot(field):
    receipt = proposed(field)
    withdrawn = replace(SITE, revision=2, status="WITHDRAWN")
    store = FieldStore(field[1], LocationRegistry((SITE, withdrawn)))
    before = contents(field[0])
    with pytest.raises(ValueError):
        mutate(
            (field[0], field[1], store, field[3]),
            "decide_plan",
            DecideFieldPlan(receipt.plan.plan_id, 1, "APPROVE"),
        )
    assert contents(field[0]) == before


def test_modified_plan_requires_its_new_approval_revision(field):
    plan = approved(field).plan
    modified = mutate(
        field,
        "modify_plan",
        ModifyFieldPlan(
            plan.plan_id,
            plan.revision,
            SITE.location_id,
            1,
            replace(SPEC, purpose="Inspect the nearer sediment marker instead."),
            True,
        ),
    )
    assert modified.plan.status == "APPROVED" and modified.plan.revision == 3
    before = contents(field[0])
    with pytest.raises(WorkflowConflict):
        mutate(
            field,
            "report_result",
            ReportFieldResult(
                plan.plan_id,
                plan.revision,
                "COMPLETE",
                "Result for the earlier version of the work.",
                NOW,
                NOW,
                True,
            ),
            who=OPERATOR,
        )
    assert contents(field[0]) == before
