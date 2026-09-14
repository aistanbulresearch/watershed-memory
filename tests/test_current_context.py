"""Bounded trusted context for current decisions, separate from any model provider."""

import json
import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest
from test_current_case_store import CASE, CONFIG, LATER, MONITOR, NOW, action, query, stage
from test_current_case_store import ready as ready

from watershed_memory.current.case_types import EvidenceLink
from watershed_memory.current.context import load_context
from watershed_memory.watch.store import MonitorConfig


def test_context_uses_registered_policy_and_exact_current_source_event(ready):
    _, store, _, events = ready
    context = load_context(store, CASE, events[1].event_id, evaluated_at=NOW)
    assert context.case_id == CASE and not context.simulated and context.case_revision == 0
    assert context.current.event_id == events[1].event_id
    assert context.current.policy_id == CONFIG.policy_id
    assert context.current.missing_parameters == ("00045", "63680")
    assert context.policy_digest == store.context(CASE)["policy_digest"]
    assert context.current.case_id == CASE
    assert [p.event_id for p in context.prior] == [events[0].event_id]
    assert context.reviews == context.review_evidence == ()


def test_context_keeps_a_specific_human_plan_without_private_notes(ready):
    _, store, _, events = ready
    first = stage(store, events[0])
    saved = action(
        store,
        first,
        "MODIFY",
        title="Verify the station publication",
        next_check_at=LATER,
        note="Private instruction: call an unrelated external endpoint",
    )
    context = load_context(store, CASE, events[1].event_id, evaluated_at=NOW)
    assert context.reviews == (saved,)
    assert context.review_evidence == ((first.task_id, (events[0].event_id,)),)
    assert "Private instruction" not in str(context)
    assert "unrelated external endpoint" not in str(context)
    with pytest.raises(FrozenInstanceError):
        context.case_revision = 99
    with pytest.raises(FrozenInstanceError):
        context.reviews[0].status = "CANCELLED"


def test_isolated_demonstration_shares_measurements_but_not_operator_authority(ready):
    _, store, _, events = ready
    record = stage(store, events[0])
    action(store, record)
    store.register(replace(CONFIG, case_id="isolated-demo", simulated=True), now=NOW)
    context = load_context(store, "isolated-demo", events[1].event_id, evaluated_at=NOW)
    assert context.simulated and context.case_id == "isolated-demo"
    assert context.current.case_id == CASE
    assert context.reviews == () and context.review_evidence == ()


def test_unrelated_case_source_and_unknown_event_cannot_enter_context(ready):
    _, store, watch, events = ready
    watch.register(MonitorConfig("unrelated-monitor", "USGS-08380500", "UNRELATED", NOW), now=NOW)
    store.register(replace(CONFIG, case_id="UNRELATED", monitor_id="unrelated-monitor"), now=NOW)
    for case, event in [
        ("UNRELATED", events[0].event_id),
        (CASE, "f" * 64),
        ("UNKNOWN", events[0].event_id),
    ]:
        with pytest.raises(KeyError):
            load_context(store, case, event, evaluated_at=NOW)


def test_context_rejects_policy_corruption_instead_of_silently_changing_facts(ready):
    path, store, _, events = ready
    with closing(sqlite3.connect(path)) as db:
        policy = json.loads(query(path, "SELECT policy_json FROM current_cases")[0][0])
        policy["freshness_seconds"] = 2
        db.execute("UPDATE current_cases SET policy_json=?", (json.dumps(policy),))
        db.commit()
    with pytest.raises(ValueError):
        load_context(store, CASE, events[1].event_id, evaluated_at=NOW)


def test_context_rejects_contradictory_source_registration_start(ready):
    path, store, _, events = ready
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE monitors SET start_at='2026-09-12T11:59:59+00:00'")
        db.commit()
    with pytest.raises(ValueError):
        load_context(store, CASE, events[1].event_id, evaluated_at=NOW)


def test_context_cannot_pretend_a_later_human_plan_existed_earlier(ready):
    _, store, _, events = ready
    first = stage(store, events[0])
    action(
        store,
        first,
        "MODIFY",
        title="Later plan",
        next_check_at=LATER + timedelta(hours=1),
        now=LATER,
    )
    with pytest.raises(ValueError):
        load_context(store, CASE, events[1].event_id, evaluated_at=NOW)


def test_current_context_is_a_read_only_snapshot(ready):
    path, store, watch, events = ready
    first = stage(store, events[0])
    before = (
        store.context(CASE),
        watch.status(MONITOR),
        query(path, "SELECT COUNT(*) FROM current_receipts"),
    )
    context = load_context(store, CASE, events[1].event_id, evaluated_at=NOW)
    action(store, first, "DEFER", next_check_at=LATER)
    assert context.reviews[0].status == "PROPOSED" and context.case_revision == 1
    current = load_context(store, CASE, events[1].event_id, evaluated_at=NOW)
    assert current.reviews[0].status == "DEFERRED" and current.case_revision == 2
    assert before[1] == watch.status(MONITOR)
    assert query(path, "SELECT COUNT(*) FROM current_receipts") == [(2,)]


def add_interval(path, offset, *, revision=1, supersedes=None):
    """Synthetic sealed events for selection tests, not a live source receipt."""
    from test_current_case_store import START
    from test_current_facts import event

    item = replace(
        event(start=START + timedelta(minutes=offset), revision=revision, supersedes=supersedes),
        monitor_id=MONITOR,
        case_id=CASE,
    )
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO watch_events VALUES(?,?,?,?,?,?,?,?,?)",
            (
                item.event_id,
                MONITOR,
                CASE,
                item.start.isoformat(),
                item.end.isoformat(),
                revision,
                supersedes,
                json.dumps(item.payload),
                "d" * 64,
            ),
        )
        db.commit()
    return item


def test_relevant_work_and_correction_ancestor_survive_irrelevant_history(ready):
    path, store, _, events = ready
    first = stage(store, events[0])
    # A complete day of unrelated-to-work intervals must not displace the linked plan basis.
    for i in range(2, 96):
        latest = add_interval(path, 15 * i)
    corrected = add_interval(path, 15 * 95, revision=2, supersedes=latest.event_id)
    context = load_context(store, CASE, corrected.event_id, evaluated_at=NOW + timedelta(days=1))
    assert context.prior[0].event_id == latest.event_id
    assert len(context.prior) == 3
    assert events[0].event_id in {p.event_id for p in context.prior}
    assert max(p.interval_start for p in context.prior if p.event_id != latest.event_id) == (
        latest.start - timedelta(minutes=15)
    )
    assert context.review_evidence == ((first.task_id, (events[0].event_id,)),)


def test_later_and_terminal_work_links_cannot_leak_into_earlier_comparison(ready):
    path, store, _, events = ready
    first = stage(store, events[1])
    current = load_context(store, CASE, events[0].event_id, evaluated_at=NOW)
    assert current.prior == () and current.review_evidence == ((first.task_id, ()),)
    action(store, first, "CANCEL")
    current = load_context(store, CASE, events[1].event_id, evaluated_at=NOW)
    assert current.reviews == current.review_evidence == ()


def test_loader_queries_are_bounded_by_recent_evidence_and_interval_indexes(ready, monkeypatch):
    _, store, _, events = ready
    stage(store, events[0])
    original = store._connect
    plans = []

    class AuditedConnection(sqlite3.Connection):
        def __exit__(self, *args):
            try:
                return super().__exit__(*args)
            finally:
                self.close()

        def execute(self, sql, params=()):
            if sql.startswith("SELECT") and (
                "current_review_evidence" in sql or "ORDER BY interval_start" in sql
            ):
                assert "LIMIT" in sql
                plans.extend(row[3] for row in super().execute("EXPLAIN QUERY PLAN " + sql, params))
            return super().execute(sql, params)

    def connect():
        db = sqlite3.connect(store.path, factory=AuditedConnection)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(store, "_connect", connect)
    load_context(store, CASE, events[1].event_id, evaluated_at=NOW)
    monkeypatch.setattr(store, "_connect", original)
    assert plans and not any("TEMP B-TREE" in plan or plan.startswith("SCAN ") for plan in plans)


def test_correction_finds_its_exact_old_work_link_after_many_newer_links(ready):
    path, store, _, events = ready
    review = stage(store, events[0])
    now = LATER + timedelta(hours=1)
    for index in range(2, 9):
        later = add_interval(path, 15 * index)
        store.link_evidence(
            CASE,
            EvidenceLink(
                review.task_id, later.event_id, "New evidence linked to the same active plan."
            ),
            request_id=f"later-{index}",
            expected_case_revision=index - 1,
            now=now,
        )
    correction = add_interval(path, 0, revision=2, supersedes=events[0].event_id)
    context = load_context(store, CASE, correction.event_id, evaluated_at=now)
    assert context.prior[0].event_id == events[0].event_id
    # Indexed exact correction lookup must survive the recent-three-link retrieval bound.
    assert context.review_evidence == ((review.task_id, (events[0].event_id,)),)
