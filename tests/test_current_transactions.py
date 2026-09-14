"""Compose accepted work changes with a delivery receipt in one caller transaction."""

import pytest
from test_current_case_store import CASE, LATER, MONITOR, NOW, action, draft, query, stage
from test_current_case_store import ready as ready

from watershed_memory.current.case_types import EvidenceLink


def propose_in(db, store, event, *, request="atomic-proposal", revision=0, **changes):
    return store._stage_review(
        db,
        CASE,
        draft(event, **changes),
        request_id=request,
        expected_case_revision=revision,
        now=NOW,
    )


def acknowledge_fixture(db, event):
    """Represent the later dispatcher's source-delivery change inside this test transaction."""
    cursor = db.execute(
        "UPDATE watch_outbox SET status='DONE' WHERE event_id=? AND status='PENDING'",
        (event.event_id,),
    )
    assert cursor.rowcount == 1
    db.execute("UPDATE monitors SET pending_count=pending_count-1 WHERE monitor_id=?", (MONITOR,))


def test_caller_transaction_is_required_and_missing_transaction_cannot_write(ready):
    _, store, _, events = ready
    with store._connect() as db:
        with pytest.raises(ValueError, match="transaction"):
            propose_in(db, store, events[0])
    assert store.context(CASE)["revision"] == 0
    existing = stage(store, events[0])
    with store._connect() as db:
        with pytest.raises(ValueError, match="transaction"):
            store._link_evidence(
                db,
                CASE,
                EvidenceLink(
                    existing.task_id, events[1].event_id, "A later interval supports this plan."
                ),
                request_id="atomic-link",
                expected_case_revision=1,
                now=NOW,
            )
    assert store.context(CASE)["revision"] == 1


def test_two_reviews_and_source_ack_commit_together_without_nested_commit(ready):
    path, store, watch, events = ready
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        first = propose_in(db, store, events[1])
        second = propose_in(
            db,
            store,
            events[1],
            request="atomic-observation",
            revision=1,
            kind="OBSERVATION_REVIEW",
        )
        acknowledge_fixture(db, events[1])
        # Another connection must still see the last committed snapshot.
        assert store.context(CASE)["revision"] == 0
        assert watch.status(MONITOR)["pending_count"] == 2
    assert {r.task_id for r in store.context(CASE)["active_reviews"]} == {
        first.task_id,
        second.task_id,
    }
    assert store.context(CASE)["revision"] == 2
    assert query(path, "SELECT COUNT(*) FROM current_receipts") == [(2,)]
    assert watch.status(MONITOR)["pending_count"] == 1


@pytest.mark.parametrize("failure_after", ["first_review", "second_review", "source_ack"])
def test_outer_failure_rolls_back_every_work_receipt_and_source_delivery_change(
    ready, failure_after
):
    path, store, watch, events = ready
    before = store.context(CASE), watch.status(MONITOR), query(path, "SELECT * FROM watch_outbox")
    with pytest.raises(RuntimeError, match="injected"):
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            propose_in(db, store, events[1])
            if failure_after == "first_review":
                raise RuntimeError("injected transaction failure")
            propose_in(
                db, store, events[1], request="second", revision=1, kind="OBSERVATION_REVIEW"
            )
            if failure_after == "second_review":
                raise RuntimeError("injected transaction failure")
            acknowledge_fixture(db, events[1])
            raise RuntimeError("injected transaction failure")
    assert before == (
        store.context(CASE),
        watch.status(MONITOR),
        query(path, "SELECT * FROM watch_outbox"),
    )
    for table in (
        "current_receipts",
        "current_reviews",
        "current_review_revisions",
        "current_review_evidence",
    ):
        assert query(path, f"SELECT COUNT(*) FROM {table}") == [(0,)]


def test_link_composes_atomically_and_does_not_edit_human_plan(ready):
    path, store, _, events = ready
    proposed = stage(store, events[0])
    human = action(store, proposed, "MODIFY", title="Human chosen plan", next_check_at=LATER)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        linked = store._link_evidence(
            db,
            CASE,
            EvidenceLink(human.task_id, events[1].event_id, "A later interval supports this plan."),
            request_id="atomic-link",
            expected_case_revision=2,
            now=NOW,
        )
        assert linked == human
        acknowledge_fixture(db, events[1])
    assert store.get_review(CASE, human.task_id) == human
    assert set(store.evidence(CASE, human.task_id)) == {e.event_id for e in events}
    assert store.context(CASE)["revision"] == 3
    assert query(path, "SELECT COUNT(*) FROM current_review_revisions") == [(2,)]


def test_transaction_primitive_preserves_original_duplicate_receipt(ready):
    _, store, _, events = ready
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        original = propose_in(db, store, events[0])
    human = action(store, original)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        retried = propose_in(db, store, events[0])
        assert retried == original and retried != human
    assert store.get_review(CASE, original.task_id) == human


def test_transaction_primitive_does_not_weaken_command_validation(ready):
    _, store, _, events = ready
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError):
            propose_in(db, store, events[0], kind="RESULT_VERIFICATION")
        with pytest.raises(ValueError):
            propose_in(db, store, events[0], request="bad request id")
        with pytest.raises(ValueError):
            propose_in(db, store, events[0], revision=True)
    assert store.context(CASE)["revision"] == 0
