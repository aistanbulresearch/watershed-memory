"""Atomic, exact field-ledger extension without changing accepted case tables."""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.case_types import CaseConfig
from watershed_memory.current.facts import CoveragePolicy
from watershed_memory.current.field_schema import SCHEMA, ensure_schema
from watershed_memory.watch.store import MonitorConfig, WatchStore

NOW = datetime(2026, 9, 12, 21, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "schema.sqlite"
    watch = WatchStore(path)
    watch.register(MonitorConfig("watch-1", "USGS-08380500", "case-1", NOW), now=NOW)
    CaseStore(path).register(
        CaseConfig("case-1", "watch-1", CoveragePolicy("coverage-v1", ("00060",)), False), now=NOW
    )
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        yield db


def objects(db, prefix):
    return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB ?", (prefix,)))


def test_extension_requires_explicit_transaction(database):
    before = objects(database, "*")
    with pytest.raises(ValueError):
        ensure_schema(database)
    assert objects(database, "*") == before


def test_complete_extension_and_marker_rollback_together(database):
    before = objects(database, "*")
    database.execute("BEGIN IMMEDIATE")
    ensure_schema(database)
    assert set(objects(database, "field_*")) == set(SCHEMA)
    assert database.execute("SELECT * FROM field_schema_version").fetchall() == [(1, 1)]
    database.rollback()
    assert objects(database, "*") == before


def test_init_is_idempotent_and_preserves_prior_schema(database):
    before = objects(database, "*")
    with database:
        database.execute("BEGIN IMMEDIATE")
        ensure_schema(database)
    for key, value in before.items():
        assert objects(database, "*")[key] == value
    with database:
        database.execute("BEGIN IMMEDIATE")
        ensure_schema(database)
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("name", list(SCHEMA))
def test_any_partial_extension_is_rejected(database, name):
    with database:
        database.execute("BEGIN IMMEDIATE")
        ensure_schema(database)
    database.execute("PRAGMA foreign_keys=OFF")
    object_type = (
        "INDEX" if SCHEMA[name].startswith("CREATE UNIQUE INDEX") else SCHEMA[name].split()[1]
    )
    database.execute(f'DROP {object_type} "{name}"')
    before = objects(database, "*")
    with pytest.raises(ValueError), database:
        database.execute("BEGIN IMMEDIATE")
        ensure_schema(database)
    assert objects(database, "*") == before


@pytest.mark.parametrize("corruption", ["extra", "changed", "marker", "source_version"])
def test_unknown_or_changed_definitions_are_rejected(database, corruption):
    with database:
        database.execute("BEGIN IMMEDIATE")
        ensure_schema(database)
    if corruption == "extra":
        database.execute("CREATE TABLE field_extra(value TEXT)")
    elif corruption == "changed":
        database.execute("ALTER TABLE field_schema_version ADD COLUMN extra TEXT")
    elif corruption == "marker":
        database.execute("UPDATE field_schema_version SET version=99")
    else:
        database.execute("PRAGMA user_version=99")
    database.commit()
    before = objects(database, "*")
    with pytest.raises(ValueError), database:
        database.execute("BEGIN IMMEDIATE")
        ensure_schema(database)
    assert objects(database, "*") == before


def seed_relational_rows(db):
    """Small SQL-only identities for exercising constraints independent of codecs."""
    db.execute("BEGIN IMMEDIATE")
    ensure_schema(db)
    for task in ("review-1", "review-2"):
        kind = "OBSERVATION_REVIEW" if task == "review-1" else "COVERAGE_REVIEW"
        db.execute(
            "INSERT INTO current_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                task,
                "case-1",
                "watch-1",
                kind,
                "APPROVED",
                2,
                "Review",
                "Review measurements",
                None,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        for revision in (1, 2):
            db.execute(
                "INSERT INTO current_review_revisions VALUES(?,?,?,?,?,?)",
                ("case-1", task, revision, "HUMAN", "{}", NOW.isoformat()),
            )
    for plan, review, revision in (("plan-1", "review-1", 1), ("plan-2", "review-2", 2)):
        db.execute(
            "INSERT INTO field_plans(plan_id,case_id,task_id,review_revision,"
            "current_revision,status,simulated,created_at,updated_at,last_activity_revision) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                plan,
                "case-1",
                review,
                revision,
                1,
                "APPROVED",
                0,
                NOW.isoformat(),
                NOW.isoformat(),
                revision,
            ),
        )
        db.execute(
            "INSERT INTO field_plan_revisions VALUES(?,?,?,?,?,?,?)",
            ("case-1", plan, 1, review, revision, "{}", NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO field_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "case-1",
                "seed-" + plan,
                "PROPOSE",
                "a" * 64,
                "{}",
                "{}",
                "a" * 64,
                revision,
                plan,
                1,
                NOW.isoformat(),
            ),
        )
    db.execute(
        "INSERT INTO field_reports VALUES(?,?,?,?,?,?,?,?,?)",
        ("report-1", "case-1", "plan-1", 1, 1, "COMPLETE", 0, NOW.isoformat(), NOW.isoformat()),
    )
    db.execute(
        "INSERT INTO field_report_revisions VALUES(?,?,?,?,?,?,?)",
        ("case-1", "report-1", 1, "plan-1", 1, "{}", NOW.isoformat()),
    )
    db.commit()


@pytest.mark.parametrize(
    "target,task,review_revision",
    [
        ("plan", "review-1", 2),
        ("plan", "review-2", 2),
        ("report", "plan-2", 1),
    ],
)
def test_revision_cannot_switch_its_immutable_parent(database, target, task, review_revision):
    seed_relational_rows(database)
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError), database:
        if target == "plan":
            database.execute(
                "INSERT INTO field_plan_revisions VALUES(?,?,?,?,?,?,?)",
                ("case-1", "plan-1", 2, task, review_revision, "{}", NOW.isoformat()),
            )
        else:
            database.execute(
                "INSERT INTO field_report_revisions VALUES(?,?,?,?,?,?,?)",
                ("case-1", "report-1", 2, task, review_revision, "{}", NOW.isoformat()),
            )
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
