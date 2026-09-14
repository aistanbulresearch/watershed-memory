"""Current evidence -> durable review -> version-bound operator response."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest

from watershed_memory.current import CoveragePolicy
from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.case_types import (
    CaseConfig,
    EvidenceLink,
    HumanAction,
    ReviewDraft,
    WorkflowConflict,
)
from watershed_memory.watch.store import MonitorConfig, WatchStore
from watershed_memory.watch.usgs import GALLINAS_SERIES, HTTPResponse, USGSClient

START = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
NOW = START + timedelta(minutes=30)
LATER = NOW + timedelta(hours=1)
CASE, MONITOR = "GALLINAS-CURRENT-WORK", "gallinas-case-watch"
POLICY = CoveragePolicy("review-v1", ("00060", "00045", "63680"))
CONFIG = CaseConfig(CASE, MONITOR, POLICY, False)


def query(path, sql, args=()):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(sql, args).fetchall()


@pytest.fixture
def ready(tmp_path):
    path = tmp_path / "current.sqlite"
    watch = WatchStore(path)
    watch.register(MonitorConfig(MONITOR, "USGS-08380500", CASE, START), now=START)
    features = []
    for index, at in enumerate((START + timedelta(minutes=5), START + timedelta(minutes=20))):
        spec = GALLINAS_SERIES[0]
        features.append(
            {
                "type": "Feature",
                "id": f"flow-{index}",
                "properties": {
                    "monitoring_location_id": "USGS-08380500",
                    "time_series_id": spec.series_id,
                    "parameter_code": spec.parameter_code,
                    "statistic_id": spec.statistic_id,
                    "unit_of_measure": spec.unit,
                    "time": at.isoformat(),
                    "value": str(index + 3),
                    "approval_status": "Provisional",
                    "qualifier": None,
                    "last_modified": NOW.isoformat(),
                },
            }
        )
    body = json.dumps({"type": "FeatureCollection", "features": features, "links": []}).encode()
    lease = watch.acquire_poll(MONITOR, now=NOW)
    batch = USGSClient(transport=lambda *args: HTTPResponse(200, body, {})).fetch(
        lease.start, lease.end, retrieved_at=NOW
    )
    watch.commit_poll(lease, batch, now=NOW)
    events = watch.pending_events(MONITOR)
    store = CaseStore(path)
    store.register(CONFIG, now=NOW)
    return path, store, watch, events


def draft(event, **changes):
    return replace(
        ReviewDraft(
            "COVERAGE_REVIEW",
            "Verify observation coverage",
            "Review the missing turbidity observations.",
            event.event_id,
            next_check_at=LATER,
        ),
        **changes,
    )


def stage(store, event, *, request="proposal-1", case=CASE, **changes):
    target = changes.pop("existing_task_id", None)
    if target is not None:
        link = EvidenceLink(
            target,
            event.event_id,
            changes.pop("reason", "New observations for the existing review."),
        )
        assert not changes
        return store.link_evidence(
            case,
            link,
            request_id=request,
            expected_case_revision=store.context(case)["revision"],
            now=NOW,
        )
    return store.stage_review(
        case,
        draft(event, **changes),
        request_id=request,
        expected_case_revision=store.context(case)["revision"],
        now=NOW,
    )


def action(store, record, action="APPROVE", *, request="human-1", now=NOW, **changes):
    return store.act(
        record.case_id,
        HumanAction(record.task_id, record.revision, action, **changes),
        request_id=request,
        now=now,
    )


def test_extension_preserves_source_schema_and_restores_work_after_restart(ready):
    path, store, watch, events = ready
    before = watch.status(MONITOR)
    record = stage(store, events[0])
    restored = CaseStore(path)
    assert restored.context(CASE)["revision"] == 1
    assert restored.context(CASE)["active_reviews"] == (record,)
    assert restored.get_review(CASE, record.task_id) == record
    assert restored.evidence(CASE, record.task_id) == (events[0].event_id,)
    assert WatchStore(path).status(MONITOR) == before
    assert query(path, "PRAGMA user_version") == [(1,)]
    assert query(path, "PRAGMA foreign_key_check") == []


def test_repeated_registration_cannot_change_scope_policy_or_revision(ready):
    _, store, _, events = ready
    record = stage(store, events[0])
    store.register(CONFIG, now=LATER)
    assert store.get_review(CASE, record.task_id) == record
    for config in (
        replace(CONFIG, coverage_policy=replace(POLICY, freshness_seconds=7200)),
        replace(CONFIG, simulated=True),
    ):
        with pytest.raises(WorkflowConflict):
            store.register(config, now=NOW)


def test_operational_case_must_match_monitor_and_simulated_case_is_isolated(ready):
    _, store, _, events = ready
    with pytest.raises((ValueError, KeyError)):
        store.register(replace(CONFIG, case_id="another-operator"), now=NOW)
    store.register(replace(CONFIG, case_id="judge-case", simulated=True), now=NOW)
    private = stage(store, events[0])
    public = stage(store, events[0], case="judge-case")
    assert public.simulated and not private.simulated and public.task_id != private.task_id
    for operation in (
        lambda: store.get_review("judge-case", private.task_id),
        lambda: store.evidence("judge-case", private.task_id),
        lambda: store.act(
            "judge-case",
            HumanAction(private.task_id, 1, "APPROVE"),
            request_id="cross-case",
            now=NOW,
        ),
    ):
        with pytest.raises(KeyError):
            operation()


def test_unconfigured_monitor_and_unrelated_source_event_are_rejected(ready):
    _, store, watch, events = ready
    with pytest.raises(KeyError):
        store.register(
            replace(CONFIG, monitor_id="missing", case_id="missing", simulated=True), now=NOW
        )
    watch.register(MonitorConfig("other-monitor", "USGS-08380500", "OTHER", START), now=START)
    store.register(CaseConfig("OTHER", "other-monitor", POLICY, False), now=NOW)
    with pytest.raises((ValueError, KeyError)):
        stage(store, events[0], case="OTHER")
    assert store.context("OTHER")["revision"] == 0


def test_duplicate_proposal_returns_original_receipt_after_human_change_and_due_time(ready):
    path, store, _, events = ready
    initial = stage(store, events[0])
    approved = action(store, initial)
    retried = CaseStore(path).stage_review(
        CASE,
        draft(events[0]),
        request_id="proposal-1",
        expected_case_revision=0,
        now=LATER + timedelta(days=2),
    )
    assert retried == initial and retried.status == "PROPOSED"
    assert store.get_review(CASE, initial.task_id) == approved
    assert store.context(CASE)["revision"] == 2


def test_reused_request_id_with_different_input_or_command_conflicts(ready):
    _, store, _, events = ready
    record = stage(store, events[0])
    with pytest.raises(WorkflowConflict):
        store.stage_review(
            CASE,
            draft(events[0], reason="Different proposed source review."),
            request_id="proposal-1",
            expected_case_revision=0,
            now=NOW,
        )
    with pytest.raises(WorkflowConflict):
        action(store, record, request="proposal-1")


def test_new_evidence_preserves_human_modified_plan_and_links_once(ready):
    _, store, _, events = ready
    initial = stage(store, events[0])
    modified = action(
        store,
        initial,
        "MODIFY",
        title="Check the source publication tomorrow",
        next_check_at=LATER + timedelta(days=1),
        note="Private owner context",
    )
    linked = stage(store, events[1], request="next-evidence", existing_task_id=initial.task_id)
    assert linked == modified and linked.revision == 2 and linked.status == "APPROVED"
    stage(store, events[1], request="same-evidence-new-receipt", existing_task_id=initial.task_id)
    assert set(store.evidence(CASE, initial.task_id)) == {e.event_id for e in events}
    assert "Private owner context" not in str(store.context(CASE))
    assert "Private owner context" not in str(asdict(store.get_review(CASE, initial.task_id)))


def test_new_evidence_preserves_defer_and_requires_exact_active_target(ready):
    _, store, _, events = ready
    initial = stage(store, events[0])
    deferred = action(store, initial, "DEFER", next_check_at=LATER)
    with pytest.raises(WorkflowConflict):
        stage(store, events[1], request="wrong-new")
    linked = stage(store, events[1], request="correct-existing", existing_task_id=initial.task_id)
    assert linked == deferred and linked.status == "DEFERRED"


@pytest.mark.parametrize(
    "control,expected", [("APPROVE", "APPROVED"), ("DISMISS", "DISMISSED"), ("CANCEL", "CANCELLED")]
)
def test_human_controls_have_persisted_revision_bound_receipts(ready, control, expected):
    path, store, _, events = ready
    record = stage(store, events[0])
    command = HumanAction(record.task_id, 1, control, note="Local private note")
    changed = store.act(CASE, command, request_id="control", now=NOW)
    assert changed.status == expected and changed.revision == 2
    assert (
        CaseStore(path).act(CASE, command, request_id="control", now=LATER + timedelta(days=2))
        == changed
    )
    with pytest.raises(WorkflowConflict):
        store.act(CASE, replace(command, note="Changed note"), request_id="control", now=NOW)
    assert store.context(CASE)["revision"] == 2


def test_approval_for_old_revision_cannot_authorize_modified_plan(ready):
    _, store, _, events = ready
    first = stage(store, events[0])
    newer = action(store, first, "MODIFY", title="New human plan", next_check_at=LATER)
    with pytest.raises(WorkflowConflict):
        action(store, first, request="stale-approval")
    assert store.get_review(CASE, first.task_id) == newer


@pytest.mark.parametrize("control", ["DISMISS", "CANCEL"])
def test_terminal_work_is_preserved_and_new_review_has_new_identity(ready, control):
    path, store, _, events = ready
    first = stage(store, events[0])
    terminal = action(store, first, control)
    with pytest.raises(WorkflowConflict):
        stage(store, events[1], request="bad-reopen", existing_task_id=first.task_id)
    with pytest.raises(WorkflowConflict):
        action(store, terminal, request="bad-approval")
    second = stage(store, events[1], request="new-review")
    assert second.task_id != first.task_id and second.status == "PROPOSED"
    assert store.get_review(CASE, first.task_id) == terminal
    assert query(path, "SELECT COUNT(*) FROM current_review_revisions") == [(3,)]


def test_stale_case_revision_and_concurrent_creations_cannot_duplicate_active_work(ready):
    path, store, _, events = ready

    def run(request):
        try:
            return CaseStore(path).stage_review(
                CASE, draft(events[0]), request_id=request, expected_case_revision=0, now=NOW
            )
        except WorkflowConflict:
            return "CONFLICT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["concurrent-1", "concurrent-2"]))
    assert results.count("CONFLICT") == 1
    assert len(store.context(CASE)["active_reviews"]) == 1
    assert store.context(CASE)["revision"] == 1


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(days=31)])
def test_new_schedules_are_bounded_and_cannot_be_past(ready, offset):
    _, store, _, events = ready
    with pytest.raises(ValueError):
        stage(store, events[0], next_check_at=NOW + offset)
    assert store.context(CASE)["revision"] == 0


def test_due_plan_requires_rescheduling_before_new_approval(ready):
    _, store, _, events = ready
    first = stage(store, events[0])
    with pytest.raises(ValueError):
        action(store, first, now=LATER)
    deferred = action(store, first, "DEFER", next_check_at=LATER + timedelta(hours=1), now=LATER)
    assert action(store, deferred, request="approve-rescheduled", now=LATER).status == "APPROVED"


def test_verification_review_cannot_be_invented_without_field_result_gate(ready):
    _, store, _, events = ready
    with pytest.raises(ValueError):
        stage(store, events[0], kind="RESULT_VERIFICATION")


def test_atomic_receipt_failure_rolls_back_all_work_and_retries_once(ready):
    path, store, watch, events = ready
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON current_receipts BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END"
        )
        db.commit()
    before = watch.status(MONITOR)
    with pytest.raises(sqlite3.IntegrityError, match="injected receipt failure"):
        stage(store, events[0])
    for table in (
        "current_reviews",
        "current_review_revisions",
        "current_review_evidence",
        "current_receipts",
    ):
        assert query(path, f"SELECT COUNT(*) FROM {table}") == [(0,)]
    assert store.context(CASE)["revision"] == 0 and watch.status(MONITOR) == before
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TRIGGER fail_receipt")
        db.commit()
    assert stage(store, events[0]).revision == 1


def test_extension_refuses_missing_foreign_unknown_or_partial_database(tmp_path, ready):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError):
        CaseStore(missing)
    assert not missing.exists()
    foreign = tmp_path / "foreign.sqlite"
    with closing(sqlite3.connect(foreign)) as db:
        db.execute("CREATE TABLE private_history(value TEXT)")
        db.commit()
    with pytest.raises(ValueError):
        CaseStore(foreign)
    assert query(foreign, "SELECT name FROM sqlite_master WHERE type='table'") == [
        ("private_history",)
    ]
    partial = tmp_path / "partial.sqlite"
    WatchStore(partial)
    with closing(sqlite3.connect(partial)) as db:
        db.execute("CREATE TABLE current_receipts(unrecognized TEXT)")
        db.commit()
    with pytest.raises(ValueError):
        CaseStore(partial)
    path, _, _, _ = ready
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE current_schema_version SET version=99")
        db.commit()
    with pytest.raises(ValueError):
        CaseStore(path)


def test_bounded_evidence_query_and_private_receipt_retention(ready):
    path, store, _, events = ready
    first = stage(store, events[0])
    stage(store, events[1], request="link-next", existing_task_id=first.task_id)
    action(store, first, note="Preserve this private reason")
    assert store.evidence(CASE, first.task_id, limit=1) == (events[1].event_id,)
    for limit in (0, 101, True):
        with pytest.raises(ValueError):
            store.evidence(CASE, first.task_id, limit=limit)
    with closing(sqlite3.connect(path)) as db:
        assert "Preserve this private reason" in "\n".join(db.iterdump())


def test_linking_after_due_time_preserves_the_due_human_plan(ready):
    _, store, _, events = ready
    first = stage(store, events[0])
    deferred = action(store, first, "DEFER", next_check_at=LATER)
    linked = store.link_evidence(
        CASE,
        EvidenceLink(
            first.task_id, events[1].event_id, "Later source evidence for the existing review."
        ),
        request_id="late-link",
        expected_case_revision=2,
        now=LATER + timedelta(hours=1),
    )
    assert linked == deferred and linked.next_check_at == LATER


@pytest.mark.parametrize("request_id", ["", "x" * 129, "bad\nrequest", "１２３", None, "../path"])
def test_receipt_keys_are_bounded_ascii_before_mutation(ready, request_id):
    _, store, _, events = ready
    with pytest.raises(ValueError):
        store.stage_review(
            CASE, draft(events[0]), request_id=request_id, expected_case_revision=0, now=NOW
        )
    assert store.context(CASE)["revision"] == 0


def test_policy_digest_is_persisted_and_same_name_different_semantics_conflicts(ready):
    path, store, _, _ = ready
    digest = store.context(CASE)["policy_digest"]
    assert len(digest) == 64 and CaseStore(path).context(CASE)["policy_digest"] == digest
    with pytest.raises(WorkflowConflict):
        store.register(
            replace(CONFIG, coverage_policy=replace(POLICY, minimum_valid_samples=2)), now=NOW
        )
    assert store.context(CASE)["policy_digest"] == digest


@pytest.mark.parametrize("operation", ["HUMAN", "LINK"])
def test_failed_followup_receipt_preserves_plan_revision_and_evidence(ready, operation):
    path, store, _, events = ready
    first = stage(store, events[0])
    before = store.context(CASE), store.evidence(CASE, first.task_id)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TRIGGER fail_followup BEFORE INSERT ON current_receipts BEGIN SELECT RAISE(ABORT,'failed followup'); END"
        )
        db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="failed followup"):
        if operation == "HUMAN":
            action(store, first)
        else:
            stage(store, events[1], request="failed-link", existing_task_id=first.task_id)
    assert (store.context(CASE), store.evidence(CASE, first.task_id)) == before
    assert query(path, "SELECT COUNT(*) FROM current_review_revisions") == [(1,)]
    assert query(path, "SELECT COUNT(*) FROM current_receipts") == [(1,)]


def test_schema_creation_failure_is_atomic_and_source_remains_usable(tmp_path, monkeypatch):
    from watershed_memory.current import case_schema

    path = tmp_path / "migration.sqlite"
    source = WatchStore(path)
    source.register(MonitorConfig(MONITOR, "USGS-08380500", CASE, START), now=START)
    before = source.status(MONITOR)
    with monkeypatch.context() as patch:
        patch.setitem(
            case_schema.SCHEMA, "current_injected_failure", "INVALID SQL FOR FAILURE TEST"
        )
        with pytest.raises(sqlite3.OperationalError):
            CaseStore(path)
    assert query(path, "SELECT name FROM sqlite_master WHERE name GLOB 'current_*'") == []
    assert WatchStore(path).status(MONITOR) == before
    CaseStore(path).register(CONFIG, now=NOW)


def test_foreign_keys_reject_wrong_monitor_link_and_unrecorded_plan_revision(ready):
    path, store, watch, events = ready
    first = stage(store, events[0])
    watch.register(MonitorConfig("other-monitor", "USGS-08380500", "OTHER", START), now=START)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO current_review_evidence(case_id,task_id,monitor_id,event_id,recorded_at) VALUES(?,?,?,?,?)",
                (CASE, first.task_id, "other-monitor", events[1].event_id, NOW.isoformat()),
            )
        db.rollback()
        db.execute("UPDATE current_reviews SET revision=2 WHERE task_id=?", (first.task_id,))
        with pytest.raises(sqlite3.IntegrityError):
            db.commit()
        db.rollback()
    assert store.get_review(CASE, first.task_id) == first


def test_extension_checks_index_presence_before_accepting_its_version(ready):
    path, _, _, _ = ready
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP INDEX current_active_kind")
        db.commit()
    with pytest.raises(ValueError):
        CaseStore(path)


def test_unicode_command_boundaries_survive_json_receipts_and_private_projection(ready):
    _, store, _, events = ready
    first = stage(store, events[0], title="🌊" * 120, reason="🌊" * 700)
    changed = action(
        store, first, "MODIFY", title="🌲" * 120, next_check_at=LATER, note="🔐" * 1000
    )
    assert changed.title == "🌲" * 120
    assert "🔐" not in str(store.context(CASE))


def test_policy_cannot_require_a_variable_outside_registered_monitor(ready):
    _, store, _, _ = ready
    unsupported = CoveragePolicy("unknown-parameter", ("99999",))
    with pytest.raises(ValueError):
        store.register(CaseConfig("isolated", MONITOR, unsupported, True), now=NOW)


def test_bounded_context_and_evidence_reads_use_their_indexes(ready):
    path, store, _, events = ready
    first = stage(store, events[0])
    plans = [
        query(
            path,
            "EXPLAIN QUERY PLAN SELECT task_id FROM current_reviews WHERE case_id=? "
            "AND status IN ('PROPOSED','APPROVED','DEFERRED') ORDER BY kind LIMIT 3",
            (CASE,),
        ),
        query(
            path,
            "EXPLAIN QUERY PLAN SELECT event_id FROM current_review_evidence WHERE case_id=? "
            "AND task_id=? ORDER BY link_id DESC LIMIT 20",
            (CASE, first.task_id),
        ),
    ]
    for plan, index in zip(plans, ("current_active_kind", "current_evidence_recent"), strict=True):
        text = " ".join(row[3] for row in plan)
        assert index in text and "TEMP B-TREE" not in text


def test_new_review_cannot_predate_case_registration(ready):
    _, store, _, events = ready
    with pytest.raises(ValueError):
        store.stage_review(
            CASE,
            draft(events[0]),
            request_id="backdated-review",
            expected_case_revision=0,
            now=START + timedelta(minutes=15),
        )
    assert store.context(CASE)["revision"] == 0


def test_new_evidence_link_cannot_predate_the_human_revision_it_preserves(ready):
    _, store, _, events = ready
    first = stage(store, events[0])
    newer = action(
        store,
        first,
        "MODIFY",
        title="Human plan made later",
        next_check_at=LATER + timedelta(hours=1),
        now=LATER,
    )
    before = store.context(CASE), store.evidence(CASE, first.task_id)
    with pytest.raises(ValueError):
        store.link_evidence(
            CASE,
            EvidenceLink(
                first.task_id, events[1].event_id, "New evidence for the later human plan."
            ),
            request_id="backdated-link",
            expected_case_revision=2,
            now=NOW,
        )
    assert (store.context(CASE), store.evidence(CASE, first.task_id)) == before
    assert store.get_review(CASE, first.task_id) == newer


def test_case_revision_timeline_is_monotonic_across_different_review_kinds(ready):
    _, store, _, events = ready
    first = stage(store, events[0])
    action(store, first, "CANCEL", now=LATER)
    with pytest.raises(ValueError):
        stage(store, events[1], request="other-kind-backdated", kind="OBSERVATION_REVIEW")
    assert store.context(CASE)["revision"] == 2


def test_original_receipt_reconciliation_precedes_new_operation_chronology(ready):
    _, store, _, events = ready
    first = stage(store, events[0])
    changed = action(store, first, "CANCEL", now=LATER)
    assert (
        store.stage_review(
            CASE, draft(events[0]), request_id="proposal-1", expected_case_revision=0, now=START
        )
        == first
    )
    assert store.get_review(CASE, first.task_id) == changed
