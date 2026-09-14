"""Atomic extension schema; accepted source/current rows remain intact."""

import sqlite3

import pytest
from test_current_case_store import MONITOR, query
from test_current_case_store import ready as ready

from watershed_memory.current.delivery_schema import ensure_schema
from watershed_memory.current.delivery_types import InvocationAllowance

ALLOWANCE = InvocationAllowance(3, 0)


def install(store, allowance=ALLOWANCE):
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_schema(db, allowance)


def test_explicit_schema_initialization_and_reopen_do_not_reset_consumed_allowance(ready):
    path, store, watch, _ = ready
    original = watch.status(MONITOR)
    install(store)
    with store._connect() as db:
        db.execute("UPDATE delivery_allowance SET scripted_used=2")
    install(store, None)
    install(store)
    assert query(
        path,
        "SELECT scripted_limit,scripted_used,provider_limit,provider_used FROM delivery_allowance",
    ) == [(3, 2, 0, 0)]
    assert watch.status(MONITOR) == original
    with pytest.raises(ValueError):
        install(store, InvocationAllowance(4, 0))


def test_schema_requires_transaction_and_new_explicit_allowance(ready):
    path, store, _, _ = ready
    with store._connect() as db:
        with pytest.raises(ValueError):
            ensure_schema(db, ALLOWANCE)
    with pytest.raises(ValueError):
        install(store, None)
    assert query(path, "SELECT name FROM sqlite_master WHERE name GLOB 'delivery_*'") == []


def test_schema_transaction_rolls_back_all_objects_and_initial_allowance(ready):
    path, store, _, _ = ready
    with pytest.raises(RuntimeError):
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db, ALLOWANCE)
            raise RuntimeError("injected after schema initialization")
    assert query(path, "SELECT name FROM sqlite_master WHERE name GLOB 'delivery_*'") == []


@pytest.mark.parametrize("corruption", ["partial", "version", "unknown", "sql"])
def test_unknown_or_partial_delivery_schema_is_refused(ready, corruption):
    path, store, _, _ = ready
    install(store)
    with store._connect() as db:
        if corruption == "partial":
            db.execute("DROP INDEX delivery_active_case")
        elif corruption == "version":
            db.execute("UPDATE delivery_schema_version SET version=99")
        elif corruption == "unknown":
            db.execute("CREATE TABLE delivery_unrecognized(x INTEGER)")
        else:
            db.execute("ALTER TABLE delivery_attempts ADD COLUMN unknown TEXT")
    with pytest.raises(ValueError):
        install(store, None)
    assert query(path, "PRAGMA integrity_check") == [("ok",)]


def test_allowance_range_is_enforced_by_sqlite_as_well_as_records(ready):
    _, store, _, _ = ready
    install(store)
    with store._connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE delivery_allowance SET scripted_used=4")


def test_wrong_allowance_type_is_rejected_before_creating_any_schema(ready):
    path, store, _, _ = ready
    with pytest.raises(ValueError):
        install(store, {"scripted_attempts": 3, "provider_attempts": 0})
    assert query(path, "SELECT name FROM sqlite_master WHERE name GLOB 'delivery_*'") == []
