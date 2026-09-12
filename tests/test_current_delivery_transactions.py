"""Admission, decisions and their future dispatcher state share one transaction."""

import pytest
from test_current_case_store import CASE, MONITOR, NOW, query
from test_current_case_store import ready as ready
from test_current_delivery_store import PROFILE, execution, reserve
from test_current_delivery_store import journal as journal
from test_current_health_integration import HEALTH_PROFILE, outcome

from watershed_memory.current.delivery_types import DeliveryReservation


def fields(ready, *, health=True):
    return dict(
        case_id=CASE,
        event_id=ready[3][1].event_id,
        request_id="outer-1",
        profile=HEALTH_PROFILE if health else PROFILE,
        source_delivery=True,
        now=NOW,
        include_source_health=health,
    )


def snapshot(ready, journal):
    path, cases, watch, _ = ready
    return (
        cases.context(CASE),
        watch.status(MONITOR),
        journal.allowance(),
        query(path, "SELECT * FROM delivery_attempts"),
        query(path, "SELECT * FROM current_receipts"),
        query(path, "SELECT * FROM watch_outbox"),
    )


def test_shared_helpers_require_an_existing_caller_transaction(ready, journal):
    before = snapshot(ready, journal)
    with ready[1]._connect() as db:
        with pytest.raises(ValueError):
            journal._reserve(db, **fields(ready))
    ticket = reserve(journal, ready)
    with ready[1]._connect() as db:
        with pytest.raises(ValueError):
            journal._commit(db, ticket.attempt_id, execution(ticket), now=NOW)
    assert journal.get(CASE, ticket.request_id).status == "RESERVED"
    assert ready[1].context(CASE) == before[0]


def test_caller_failure_after_reservation_restores_allowance_and_optional_schema(ready, journal):
    before = snapshot(ready, journal)
    with pytest.raises(RuntimeError, match="dispatcher admission failed"):
        with ready[1]._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ticket = journal._reserve(db, **fields(ready))
            assert type(ticket) is DeliveryReservation
            assert db.in_transaction
            assert db.execute("SELECT COUNT(*) FROM health_attempts").fetchone()[0] == 1
            raise RuntimeError("dispatcher admission failed")
    assert snapshot(ready, journal) == before
    assert query(ready[0], "SELECT name FROM sqlite_master WHERE name GLOB 'health_*'") == []


def test_shared_reservation_does_not_open_or_finish_an_independent_transaction(
    ready, journal, monkeypatch
):
    statements = []

    def forbidden():
        raise AssertionError("unexpected independent connection")

    with ready[1]._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.set_trace_callback(statements.append)
        with monkeypatch.context() as patch:
            patch.setattr(ready[1], "_connect", forbidden)
            ticket = journal._reserve(db, **fields(ready))
        assert db.in_transaction
        assert not any(s.split()[0].upper() in ("BEGIN", "COMMIT", "ROLLBACK") for s in statements)
    assert journal.get(CASE, ticket.request_id).status == "RESERVED"
    assert journal.allowance().provider_used == 1


@pytest.mark.parametrize("health", [False, True])
def test_caller_failure_after_decision_rolls_back_work_source_and_delivery(ready, journal, health):
    args = fields(ready, health=health)
    ticket = journal.reserve(**args)
    result = outcome(ticket) if health else execution(ticket)
    before = snapshot(ready, journal)
    with pytest.raises(RuntimeError, match="attention checkpoint failed"):
        with ready[1]._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = journal._commit(db, ticket.attempt_id, result, now=NOW)
            assert saved.status == "COMMITTED" and db.in_transaction
            assert (
                db.execute(
                    "SELECT status FROM watch_outbox WHERE event_id=?", (args["event_id"],)
                ).fetchone()[0]
                == "DONE"
            )
            raise RuntimeError("attention checkpoint failed")
    assert snapshot(ready, journal) == before
    assert journal.get(CASE, ticket.request_id).status == "RESERVED"


def test_shared_commit_leaves_final_commit_to_caller_and_preserves_duplicates(
    ready, journal, monkeypatch
):
    ticket = journal.reserve(**fields(ready))
    result = outcome(ticket)
    statements = []

    def forbidden():
        raise AssertionError("unexpected independent connection")

    with ready[1]._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.set_trace_callback(statements.append)
        with monkeypatch.context() as patch:
            patch.setattr(ready[1], "_connect", forbidden)
            saved = journal._commit(db, ticket.attempt_id, result, now=NOW)
            duplicate = journal._commit(db, ticket.attempt_id, result, now=NOW)
        assert saved == duplicate and db.in_transaction
        assert not any(s.split()[0].upper() in ("BEGIN", "COMMIT", "ROLLBACK") for s in statements)
    assert journal.get(CASE, ticket.request_id) == saved
    assert journal.reserve(**fields(ready)) == saved
