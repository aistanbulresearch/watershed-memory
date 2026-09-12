"""Adversarial receipt, ordering and preflight checks for the complete field ledger."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from test_current_field_store import (
    CASE,
    COORDINATOR,
    NOW,
    OPERATOR,
    SITE,
    SPEC,
    contents,
    mutate,
    proposed,
)
from test_current_field_store import field as field
from test_current_field_store import ready as ready

from watershed_memory.current.field_codec import encode
from watershed_memory.current.field_types import (
    DecideFieldPlan,
    ProposeFieldPlan,
    ReportFieldResult,
)


@pytest.mark.parametrize("part", ["case_revision", "plan", "location"])
@pytest.mark.parametrize("rewrite_hash", [False, True])
def test_canonical_receipt_tamper_is_not_returned_as_original(field, part, rewrite_hash):
    receipt = proposed(field)
    with closing(sqlite3.connect(field[0])) as db:
        raw = json.loads(
            db.execute(
                "SELECT result_json FROM field_receipts WHERE request_id=?", (receipt.request_id,)
            ).fetchone()[0]
        )
        if part == "case_revision":
            raw["case_revision"] += 1
        elif part == "plan":
            raw["plan"]["spec"]["purpose"] = "Forged purpose substituted for the saved plan."
        else:
            raw["plan"]["location"]["approved_by"] = "forged-approver"
        db.execute(
            "UPDATE field_receipts SET result_json=? WHERE request_id=?",
            (encode(raw), receipt.request_id),
        )
        if rewrite_hash:
            from watershed_memory.current.case_records import digest

            db.execute(
                "UPDATE field_receipts SET result_hash=? WHERE request_id=?",
                (digest(encode(raw)), receipt.request_id),
            )
        db.commit()
    before = contents(field[0])
    command = ProposeFieldPlan(
        field[3].task_id, 1, SITE.location_id, 1, SPEC, True, "PRIVATE-FIELD-NOTE-CANARY"
    )
    with pytest.raises(ValueError):
        mutate(
            field,
            "propose_plan",
            command,
            request=receipt.request_id,
            revision=receipt.case_revision - 1,
        )
    assert contents(field[0]) == before


@pytest.mark.parametrize(
    "part", ["command", "principal", "case_id", "request_id", "revision", "time"]
)
def test_invalid_input_does_not_acquire_database_writer(field, monkeypatch, part):
    store = field[2]
    command = ProposeFieldPlan(field[3].task_id, 1, SITE.location_id, 1, SPEC, True)
    values = dict(
        case_id=CASE,
        command=command,
        principal=COORDINATOR,
        request_id="no-write",
        expected_case_revision=1,
        now=NOW,
    )
    if part == "command":
        values["command"] = {}
    elif part == "principal":
        values["principal"] = OPERATOR
    elif part == "case_id":
        values["case_id"] = "wrong-case"
    elif part == "request_id":
        values["request_id"] = "invalid/request"
    elif part == "revision":
        values["expected_case_revision"] = True
    else:
        values["now"] = NOW.replace(tzinfo=None)

    def forbidden():
        raise AssertionError("invalid input attempted to open a writer")

    monkeypatch.setattr(field[1], "_connect", forbidden)
    with pytest.raises(ValueError):
        store.propose_plan(**values)


def test_agent_label_is_reserved_for_an_unapproved_proposal(field):
    plan = proposed(field).plan
    agent = replace(plan, author_kind="AGENT", recorded_by="attempt-1")
    assert agent.status == "PROPOSED"
    with pytest.raises(ValueError):
        replace(agent, status="APPROVED", change_kind="APPROVE")
    with pytest.raises(ValueError):
        replace(plan, author_kind="UNTRUSTED")


def test_equal_timestamp_latest_work_uses_transaction_order(field, monkeypatch):
    from watershed_memory.current import field_actions

    ids = iter(("field-plan-a", "field-report-a", "field-plan-z"))
    monkeypatch.setattr(field_actions, "_id", lambda _: next(ids))
    original = proposed(field).plan
    approval = mutate(field, "decide_plan", DecideFieldPlan(original.plan_id, 1, "APPROVE"))
    mutate(
        field,
        "report_result",
        ReportFieldResult(
            original.plan_id,
            approval.plan.revision,
            "PARTIAL",
            "Demonstration partial work at the same recorded clock time.",
            NOW,
            NOW,
            True,
        ),
        who=OPERATOR,
    )
    second = proposed(field).plan
    cancelled = mutate(field, "decide_plan", DecideFieldPlan(second.plan_id, 1, "CANCEL"))
    latest = field[2].current_for_review(CASE, field[3].task_id)
    assert latest.plan == cancelled.plan
    assert field[2].recent(CASE)[0].plan == cancelled.plan


def original_result_request(field, operation):
    from test_current_field_store import VERIFIER

    from watershed_memory.current.fact_validation import timestamp
    from watershed_memory.current.field_types import AttachFieldEvidence, VerifyFieldReport

    methods = {
        "REPORT": ("report_result", ReportFieldResult, OPERATOR),
        "ATTACH": ("attach_evidence", AttachFieldEvidence, OPERATOR),
        "VERIFY": ("verify_result", VerifyFieldReport, VERIFIER),
    }
    with closing(sqlite3.connect(field[0])) as db:
        row = db.execute(
            "SELECT request_id,input_json,result_json FROM field_receipts WHERE operation=?",
            (operation,),
        ).fetchone()
    original = json.loads(row[1])
    data = original["command"]
    for name in ("performed_start", "performed_end", "observed_at"):
        if name in data and data[name] is not None:
            data[name] = timestamp(data[name])
    if "evidence_ids" in data:
        data["evidence_ids"] = tuple(data["evidence_ids"])
    method, cls, who = methods[operation]
    return method, cls(**data), who, row[0], original["expected_case_revision"], row[2]


@pytest.mark.parametrize("operation", ["REPORT", "ATTACH", "VERIFY"])
def test_original_result_receipt_survives_later_correction(field, operation):
    from dataclasses import asdict
    from datetime import timedelta

    from test_current_field_store import verified

    from watershed_memory.current.field_types import CorrectFieldResult

    receipt = verified(field)
    method, command, who, request_id, revision, original_json = original_result_request(
        field, operation
    )
    mutate(
        field,
        "correct_result",
        CorrectFieldResult(
            receipt.result.report.report_id,
            1,
            "PARTIAL",
            "Corrected outcome with an uninspected part of the site.",
            NOW,
            NOW,
            True,
        ),
        who=OPERATOR,
        now=NOW + timedelta(minutes=18),
    )
    before = contents(field[0])
    result = mutate(field, method, command, who=who, request=request_id, revision=revision, now=NOW)
    assert encode(asdict(result)) == original_json
    assert contents(field[0]) == before


@pytest.mark.parametrize("part", ["report", "evidence", "verification"])
def test_nested_receipt_content_must_match_immutable_records_even_with_new_digest(field, part):
    from test_current_field_store import verified

    from watershed_memory.current.case_records import digest

    receipt = verified(field)
    method, command, who, request_id, revision, encoded = original_result_request(field, "VERIFY")
    raw = json.loads(encoded)
    if part == "report":
        raw["result"]["report"]["summary"] = "A canonically forged inspection outcome summary."
    elif part == "evidence":
        raw["result"]["evidence"][0]["provenance"] = "A canonically forged evidence attribution."
    else:
        raw["verification"]["scope"] = "A canonically forged verification scope."
        raw["result"]["verification"]["scope"] = raw["verification"]["scope"]
    encoded = encode(raw)
    with closing(sqlite3.connect(field[0])) as db:
        db.execute(
            "UPDATE field_receipts SET result_json=?,result_hash=? WHERE request_id=?",
            (encoded, digest(encoded), receipt.request_id),
        )
        db.commit()
    before = contents(field[0])
    with pytest.raises(ValueError):
        mutate(field, method, command, who=who, request=request_id, revision=revision, now=NOW)
    assert contents(field[0]) == before


@pytest.mark.parametrize("attack", ["omit_prior", "include_same_clock_future", "swap_specific"])
def test_receipt_requires_complete_membership_at_its_operation(field, attack):
    from dataclasses import asdict
    from datetime import timedelta

    from test_current_field_store import reported

    from watershed_memory.current.case_records import digest
    from watershed_memory.current.field_types import AttachFieldEvidence

    report_receipt = reported(field)
    report = report_receipt.result.report
    when = NOW + timedelta(minutes=15)
    command_a = AttachFieldEvidence(
        report.report_id,
        1,
        "OPERATOR_RECORD_REFERENCE",
        "INSPECTION_RECORD_REFERENCE",
        "same-clock-a",
        "First same-clock inspection reference.",
        NOW,
        None,
        True,
    )
    first = mutate(field, "attach_evidence", command_a, who=OPERATOR, now=when)
    command_b = replace(command_a, reference="same-clock-b")
    second = mutate(field, "attach_evidence", command_b, who=OPERATOR, now=when)
    if attack in ("omit_prior", "swap_specific"):
        target = second
        method, command, who, request_id, revision = (
            "attach_evidence",
            command_b,
            OPERATOR,
            second.request_id,
            second.case_revision - 1,
        )
        raw = asdict(second)
        if attack == "omit_prior":
            raw["result"]["evidence"] = (asdict(second.evidence),)
        else:
            raw["evidence"] = asdict(first.evidence)
    else:
        target = report_receipt
        method, command, who, request_id, revision, _ = original_result_request(field, "REPORT")
        raw = asdict(report_receipt)
        raw["result"]["evidence"] = (asdict(second.evidence),)
        raw["result"]["verification_level"] = "EVIDENCE_ATTACHED"
    encoded = encode(raw)
    with closing(sqlite3.connect(field[0])) as db:
        db.execute(
            "UPDATE field_receipts SET result_json=?,result_hash=? WHERE request_id=?",
            (encoded, digest(encoded), target.request_id),
        )
        db.commit()
    before = contents(field[0])
    with pytest.raises(ValueError):
        mutate(field, method, command, who=who, request=request_id, revision=revision, now=when)
    assert contents(field[0]) == before


@pytest.mark.parametrize("wrong_pointer", ["other_plan", "nonexistent"])
def test_activity_pointer_must_belong_to_its_plan_receipt(field, wrong_pointer):
    first = proposed(field)
    mutate(field, "decide_plan", DecideFieldPlan(first.plan.plan_id, 1, "CANCEL"))
    second = proposed(field)
    invalid = first.case_revision if wrong_pointer == "other_plan" else 9999
    before = contents(field[0])
    with closing(sqlite3.connect(field[0])) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError), db:
            db.execute(
                "UPDATE field_plans SET last_activity_revision=? WHERE plan_id=?",
                (invalid, second.plan.plan_id),
            )
    assert contents(field[0]) == before


def test_activity_pointer_cannot_rewind_to_an_older_receipt_of_the_same_plan(field):
    first = proposed(field)
    mutate(field, "decide_plan", DecideFieldPlan(first.plan.plan_id, 1, "APPROVE"))
    before = contents(field[0])
    with closing(sqlite3.connect(field[0])) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError), db:
            db.execute(
                "UPDATE field_plans SET last_activity_revision=? WHERE plan_id=?",
                (first.case_revision, first.plan.plan_id),
            )
    assert contents(field[0]) == before
