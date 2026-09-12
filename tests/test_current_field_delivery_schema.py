"""The strict context3 namespace binds attempts to exact field receipts."""

import sqlite3
from contextlib import closing

import pytest
from test_current_case_store import MONITOR
from test_current_field_store import CASE, NOW, proposed
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.context3_schema import SCHEMA, VERSION, ensure_schema
from watershed_memory.current.delivery_schema import ensure_schema as ensure_delivery_schema
from watershed_memory.current.delivery_types import InvocationAllowance


def rows(path, sql, parameters=()):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(sql, parameters).fetchall()


def prepare_delivery(field):
    _, cases, _, _ = field
    with cases._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_delivery_schema(db, InvocationAllowance(2, 2))


def create_context3(field):
    prepare_delivery(field)
    _, cases, _, _ = field
    with cases._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_schema(db)


def insert_delivery_attempt(field, attempt_id="attempt-" + "a" * 32):
    path, cases, _, review = field
    event_id = cases.evidence(CASE, review.task_id)[0]
    revision = cases.context(CASE)["revision"]
    with cases._connect() as db:
        db.execute(
            """INSERT INTO delivery_attempts(
              attempt_id,case_id,monitor_id,event_id,request_id,input_json,input_hash64,
              profile_json,mode,source_delivery,status,case_revision,coverage_digest64,
              context_digest64,prior_ids_json,review_refs_json,evaluated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                attempt_id,
                CASE,
                MONITOR,
                event_id,
                "delivery-request",
                "{}",
                "a" * 64,
                "{}",
                "SCRIPTED_SDK",
                0,
                "RESERVED",
                revision,
                "b" * 64,
                "c" * 64,
                "[]",
                "[]",
                NOW.isoformat(),
            ),
        )
    assert rows(path, "PRAGMA foreign_key_check") == []
    return attempt_id


def test_creation_is_explicit_transactional_and_does_not_charge_or_configure(field):
    prepare_delivery(field)
    path, cases, _, _ = field
    before = rows(path, "SELECT * FROM delivery_allowance")
    with pytest.raises(ValueError, match="transaction"):
        with cases._connect() as db:
            ensure_schema(db)
    with cases._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_schema(db)
    objects = dict(
        (name, sql)
        for name, sql in rows(
            path, "SELECT name,sql FROM sqlite_master WHERE name GLOB 'context3_*'"
        )
    )
    assert set(objects) == set(SCHEMA)
    assert rows(path, "SELECT * FROM context3_schema_version") == [(1, VERSION)]
    assert rows(path, "SELECT * FROM delivery_allowance") == before
    assert rows(path, "SELECT COUNT(*) FROM delivery_attempts") == [(0,)]
    assert rows(path, "SELECT COUNT(*) FROM dispatch_settings") == [(0,)]
    assert rows(path, "PRAGMA foreign_key_check") == []


def test_attempt_and_agent_proposal_require_exact_case_attempt_receipt_and_plan(field):
    receipt = proposed(field)
    create_context3(field)
    attempt_id = insert_delivery_attempt(field)
    path, cases, _, _ = field
    with cases._connect() as db:
        db.execute(
            "INSERT INTO context3_attempts VALUES(?,?,?,?,?,?)",
            (attempt_id, CASE, "{}", "c" * 64, "d" * 64, NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO context3_agent_proposals VALUES(?,?,?,?,?)",
            (
                attempt_id,
                CASE,
                receipt.request_id,
                receipt.plan.plan_id,
                receipt.plan.revision,
            ),
        )
    assert rows(path, "PRAGMA foreign_key_check") == []

    with cases._connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO context3_attempts VALUES(?,?,?,?,?,?)",
                ("attempt-" + "b" * 32, CASE, "{}", "c" * 64, "d" * 64, NOW.isoformat()),
            )
        db.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO context3_agent_proposals VALUES(?,?,?,?,?)",
                (
                    attempt_id,
                    CASE,
                    "unrelated-request",
                    receipt.plan.plan_id,
                    receipt.plan.revision,
                ),
            )
        db.rollback()


def test_cross_case_attempt_membership_and_receipt_identity_fail_closed(field):
    receipt = proposed(field)
    create_context3(field)
    attempt_id = insert_delivery_attempt(field)
    _, cases, _, _ = field
    with cases._connect() as db:
        for statement, values in (
            (
                "INSERT INTO context3_attempts VALUES(?,?,?,?,?,?)",
                (attempt_id, "OTHER", "{}", "c" * 64, "d" * 64, NOW.isoformat()),
            ),
            (
                "INSERT INTO context3_agent_proposals VALUES(?,?,?,?,?)",
                (
                    attempt_id,
                    "OTHER",
                    receipt.request_id,
                    receipt.plan.plan_id,
                    receipt.plan.revision,
                ),
            ),
            (
                "INSERT INTO context3_dispatch_settings VALUES(?,?,?,?,?,?)",
                ("OTHER", "{}", "{}", NOW.isoformat(), NOW.isoformat(), 3),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement, values)
            db.rollback()


def test_unknown_or_partial_namespace_rolls_back_prerequisite_dispatch_creation(field):
    prepare_delivery(field)
    path, cases, _, _ = field
    with cases._connect() as db:
        db.execute("CREATE TABLE context3_foreign(value TEXT)")
    assert rows(path, "SELECT name FROM sqlite_master WHERE name GLOB 'dispatch_*'") == []
    with cases._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="context3"):
            ensure_schema(db)
        db.rollback()
    assert rows(path, "SELECT name FROM sqlite_master WHERE name GLOB 'dispatch_*'") == []
    assert rows(path, "SELECT name FROM sqlite_master WHERE name GLOB 'context3_*'") == [
        ("context3_foreign",)
    ]


def test_changed_definition_is_rejected(field):
    create_context3(field)
    _, cases, _, _ = field
    with cases._connect() as db:
        db.execute("DROP TABLE context3_dispatch_settings")
        db.execute("CREATE TABLE context3_dispatch_settings(case_id TEXT PRIMARY KEY)")
    with cases._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="definition"):
            ensure_schema(db)
        db.rollback()


def test_changed_version_is_rejected(field):
    create_context3(field)
    _, cases, _, _ = field
    with cases._connect() as db:
        db.execute("UPDATE context3_schema_version SET version=99")
    with cases._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="version"):
            ensure_schema(db)
        db.rollback()
