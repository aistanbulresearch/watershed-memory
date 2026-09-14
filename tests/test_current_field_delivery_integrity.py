"""Retrieval binds visible v3 outcomes to their original input and work receipts."""

import json
from datetime import timedelta

import pytest
from test_current_field_delivery_boundaries import canonical as canonical
from test_current_field_delivery_store import AT, execution, reserve, store
from test_current_field_store import CASE
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.case_records import digest
from watershed_memory.current.field_codec import encode


@pytest.mark.parametrize("raw", ["null", "true", "1", '"scalar"', "[]"])
def test_reserved_request_requires_a_json_object(field, raw):
    delivery = store(field)
    ticket = reserve(delivery, field)
    with field[1]._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE delivery_attempts SET input_json=?,input_hash64=? WHERE attempt_id=?",
            (raw, digest(raw), ticket.attempt_id),
        )
    with pytest.raises(ValueError, match="object"):
        delivery.get(CASE, ticket.request_id)


def test_repeated_source_link_preserves_earlier_evidence_time(canonical):
    from test_current_field_delivery_boundaries import execution as source_execution
    from test_current_field_delivery_boundaries import reserve as source_reserve

    from watershed_memory.current.case_types import EvidenceLink

    _, cases, _, events, review, site, _, delivery = canonical
    case_id = review.case_id
    linked_at = AT - timedelta(minutes=1)
    cases.link_evidence(
        case_id,
        EvidenceLink(review.task_id, events[-1].event_id, "Previously linked source evidence"),
        request_id="prelinked-source-evidence",
        expected_case_revision=cases.context(case_id)["revision"],
        now=linked_at,
    )
    ticket = source_reserve(canonical)
    receipt = delivery.commit(ticket.attempt_id, source_execution(ticket, site), now=AT)
    assert delivery.get(case_id, ticket.request_id) == receipt
    with cases._connect() as db:
        saved = db.execute(
            "SELECT recorded_at FROM current_review_evidence "
            "WHERE case_id=? AND task_id=? AND event_id=?",
            (case_id, review.task_id, events[-1].event_id),
        ).fetchall()
    assert [row[0] for row in saved] == [linked_at.isoformat()]


def test_delivery_cannot_claim_evidence_linked_after_its_commit(field):
    delivery = store(field)
    ticket = reserve(delivery, field)
    delivery.commit(ticket.attempt_id, execution(ticket), now=AT)
    with field[1]._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE current_review_evidence SET recorded_at=? "
            "WHERE case_id=? AND task_id=? AND event_id=?",
            (
                (AT + timedelta(seconds=1)).isoformat(),
                CASE,
                field[3].task_id,
                ticket.context.base.current.event_id,
            ),
        )
    with pytest.raises(ValueError):
        delivery.get(CASE, ticket.request_id)


@pytest.mark.parametrize("with_proposal", [True, False])
@pytest.mark.parametrize("marker", ["revision", "updated_at"])
def test_committed_delivery_requires_final_case_marker(field, with_proposal, marker):
    from test_current_field_store import verified

    if not with_proposal:
        verified(field, "COMPLETE")
    delivery = store(field)
    ticket = reserve(delivery, field)
    disposition = "PROPOSE_FIELD_PLAN" if with_proposal else "NO_NEW_FIELD_PLAN"
    delivery.commit(ticket.attempt_id, execution(ticket, disposition), now=AT)
    with field[1]._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if marker == "revision":
            db.execute("UPDATE current_cases SET revision=revision-1 WHERE case_id=?", (CASE,))
        else:
            db.execute(
                "UPDATE current_cases SET updated_at=? WHERE case_id=?",
                ((AT - timedelta(seconds=1)).isoformat(), CASE),
            )
    with pytest.raises(ValueError):
        delivery.get(CASE, ticket.request_id)


@pytest.mark.parametrize("changed", ["reservation", "agent_actor", "work_list", "work_operation"])
def test_delivery_read_refuses_detached_input_or_work_attribution(field, changed):
    delivery = store(field)
    ticket = reserve(delivery, field)
    receipt = delivery.commit(ticket.attempt_id, execution(ticket), now=AT)
    with field[1]._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if changed == "reservation":
            db.execute(
                "UPDATE delivery_attempts SET input_json='{}' WHERE attempt_id=?",
                (ticket.attempt_id,),
            )
        elif changed == "agent_actor":
            key = (CASE, receipt.field_plan.request_id)
            raw = db.execute(
                "SELECT input_json FROM field_receipts WHERE case_id=? AND request_id=?", key
            ).fetchone()[0]
            value = json.loads(raw)
            value["actor"] = {"kind": "HUMAN", "attempt_id": ticket.attempt_id}
            raw = encode(value)
            db.execute(
                "UPDATE field_receipts SET input_json=?,input_hash=? WHERE case_id=? AND request_id=?",
                (raw, digest(raw), *key),
            )
        elif changed == "work_list":
            db.execute(
                "UPDATE delivery_attempts SET work_json='[]' WHERE attempt_id=?",
                (ticket.attempt_id,),
            )
        else:
            db.execute(
                "UPDATE current_receipts SET operation='HUMAN' WHERE case_id=? AND request_id=?",
                (CASE, ticket.attempt_id + "-0"),
            )
    with pytest.raises(ValueError):
        delivery.get(CASE, ticket.request_id)


def test_committed_source_receipt_requires_its_exact_acknowledgement(canonical):
    from test_current_field_delivery_boundaries import execution as source_execution
    from test_current_field_delivery_boundaries import reserve as source_reserve

    _, cases, _, _, _, site, _, delivery = canonical
    ticket = source_reserve(canonical)
    delivery.commit(ticket.attempt_id, source_execution(ticket, site), now=AT)
    with cases._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE watch_outbox SET status='PENDING' WHERE monitor_id=? AND event_id=?",
            (ticket.context.base.current.monitor_id, ticket.context.base.current.event_id),
        )
    with pytest.raises(ValueError):
        delivery.get(ticket.context.base.case_id, ticket.request_id)


@pytest.mark.parametrize("violation", ["different_task", "private_note"])
def test_agent_command_cannot_change_after_source_work_is_staged(field, monkeypatch, violation):
    from dataclasses import replace

    from test_current_field_store import contents

    from watershed_memory.current.field_store import FieldStore

    delivery = store(field)
    ticket = reserve(delivery, field)
    run = execution(ticket)
    before = contents(field[0])
    original = FieldStore._stage_agent_plan

    def changed(self, db, case_id, command, **kwargs):
        alteration = (
            {"task_id": "unrelated-task"}
            if violation == "different_task"
            else {"private_note": "Model must not supply a human note."}
        )
        return original(self, db, case_id, replace(command, **alteration), **kwargs)

    monkeypatch.setattr(FieldStore, "_stage_agent_plan", changed)
    with pytest.raises(ValueError):
        delivery.commit(ticket.attempt_id, run, now=AT)
    assert contents(field[0]) == before
