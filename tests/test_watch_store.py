"""Durable acquisition acceptance, independent of network and model providers."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import localcontext

import pytest

from watershed_memory.watch.observations import SourceError
from watershed_memory.watch.store import LeaseLost, MonitorConfig, WatchStore
from watershed_memory.watch.usgs import HTTPResponse, USGSClient

UTC = timezone.utc
START = datetime(2026, 9, 12, 11, 0, tzinfo=UTC)
FIRST = START + timedelta(minutes=30)
FLOW = "9857d8ac60994a19bc7a8ec28809066f"
SITE = "USGS-08380500"
MONITOR = "gallinas-current"
CASE = "GALLINAS-CURRENT-OBSERVATIONS"


def item(at=START, value="3.64", *, modified=None, provider="source-1"):
    return {
        "type": "Feature",
        "id": provider,
        "properties": {
            "monitoring_location_id": SITE,
            "time_series_id": FLOW,
            "parameter_code": "00060",
            "statistic_id": "00011",
            "unit_of_measure": "ft^3/s",
            "time": at.isoformat(),
            "value": value,
            "approval_status": "Provisional",
            "qualifier": None,
            "last_modified": (modified or FIRST).isoformat(),
        },
    }


def batch(lease, *items):
    body = json.dumps({"type": "FeatureCollection", "features": items, "links": []}).encode()
    client = USGSClient(transport=lambda *args: HTTPResponse(status=200, body=body, headers={}))
    return client.fetch(lease.start, lease.end, retrieved_at=lease.acquired_at)


@pytest.fixture
def registered(tmp_path):
    path = tmp_path / "current-watch.sqlite"
    store = WatchStore(path)
    config = MonitorConfig(monitor_id=MONITOR, station_id=SITE, case_id=CASE, start_at=START)
    store.register(config, now=START)
    return path, store, config


def commit(store, when, *items):
    lease = store.acquire_poll(MONITOR, now=when)
    assert lease is not None
    return store.commit_poll(lease, batch(lease, *items), now=when)


def test_restart_preserves_case_observation_watermarks_events_and_due_time(registered):
    path, store, config = registered
    result = commit(store, FIRST, item(), item(START + timedelta(minutes=20), "4.0"))
    assert result.new_observations == 2 and result.corrected_observations == 0
    assert result.outcome == "EVENTS_READY" and len(result.event_ids) == 2
    before = store.status(MONITOR)
    restored = WatchStore(path)
    restored.register(config, now=FIRST)
    assert restored.status(MONITOR) == before
    assert before["case_id"] == CASE
    assert before["fetched_through"] == FIRST.isoformat()
    assert before["series_cursors"][FLOW] == (START + timedelta(minutes=20)).isoformat()
    assert before["observation_count"] == before["version_count"] == 2
    assert before["event_count"] == before["pending_count"] == 2
    assert restored.acquire_poll(MONITOR, now=FIRST + timedelta(seconds=1)) is None
    assert [e.event_id for e in restored.pending_events(MONITOR)] == list(result.event_ids)


def test_registration_cannot_reset_or_silently_reconfigure_monitor(registered):
    _, store, config = registered
    commit(store, FIRST, item())
    before = store.status(MONITOR)
    store.register(config, now=FIRST)
    changed = MonitorConfig(monitor_id=MONITOR, station_id=SITE, case_id="OTHER", start_at=START)
    with pytest.raises(ValueError):
        store.register(changed, now=FIRST)
    assert store.status(MONITOR) == before


def test_repeated_provider_publication_creates_no_new_work(registered):
    _, store, _ = registered
    initial = commit(store, FIRST, item())
    later = FIRST + timedelta(minutes=5)
    result = commit(store, later, item(value="3.6400", modified=later, provider="new-uuid"))
    assert result.outcome == "NO_SOURCE_CHANGE"
    assert result.new_observations == result.corrected_observations == 0
    assert result.event_ids == ()
    status = store.status(MONITOR)
    assert status["observation_count"] == status["version_count"] == 1
    assert status["event_count"] == 1
    assert store.pending_events(MONITOR)[0].event_id == initial.event_ids[0]


def test_corrected_evidence_and_return_to_old_value_append_linked_revisions(registered):
    _, store, _ = registered
    # Stay inside all three advertised overlap windows; never move the scan cursor backwards.
    observed = START + timedelta(minutes=10)
    first = commit(store, FIRST, item(observed))
    second = commit(
        store,
        FIRST + timedelta(minutes=5),
        item(observed, value="8.0", modified=FIRST + timedelta(minutes=5)),
    )
    third = commit(
        store,
        FIRST + timedelta(minutes=10),
        item(observed, value="3.64", modified=FIRST + timedelta(minutes=10)),
    )
    assert second.corrected_observations == third.corrected_observations == 1
    events = store.pending_events(MONITOR)
    assert [event.revision for event in events] == [1, 2, 3]
    assert events[0].supersedes_event_id is None
    assert events[1].supersedes_event_id == first.event_ids[0]
    assert events[2].supersedes_event_id == second.event_ids[0]
    assert len({*first.event_ids, *second.event_ids, *third.event_ids}) == 3
    assert store.status(MONITOR)["version_count"] == 3
    assert store.status(MONITOR)["observation_count"] == 1
    assert [e.payload["series"][FLOW]["latest_value"] for e in events] == ["3.64", "8", "3.64"]


def test_late_sample_revises_old_interval_without_rolling_watermark_back(registered):
    _, store, _ = registered
    latest = START + timedelta(minutes=20)
    commit(store, FIRST, item(), item(latest))
    previous = store.pending_events(MONITOR)[0]
    result = commit(store, FIRST + timedelta(minutes=5), item(START + timedelta(minutes=10)))
    assert result.new_observations == 1
    assert store.status(MONITOR)["series_cursors"][FLOW] == latest.isoformat()
    new = next(e for e in store.pending_events(MONITOR) if e.event_id == result.event_ids[0])
    assert new.start == START and new.supersedes_event_id == previous.event_id
    assert new.payload["series"][FLOW]["sample_count"] == 2


def test_buffered_interval_can_seal_on_empty_later_poll(registered):
    _, store, _ = registered
    result = commit(
        store, START + timedelta(minutes=5), item(modified=START + timedelta(minutes=5))
    )
    assert result.outcome == "ROWS_BUFFERED" and result.event_ids == ()
    result = commit(store, START + timedelta(minutes=15))
    assert result.new_observations == 0
    assert result.outcome == "EVENTS_READY" and len(result.event_ids) == 1


def test_null_latest_and_missing_series_are_visible_without_safety_claim(registered):
    _, store, _ = registered
    commit(store, FIRST, item(), item(START + timedelta(minutes=5), None))
    payload = store.pending_events(MONITOR)[0].payload
    assert payload["evidence_class"] == "CURRENT_USGS_OBSERVATION"
    assert payload["schema_version"] == 1
    flow = payload["series"][FLOW]
    assert (flow["sample_count"], flow["valid_count"]) == (2, 1)
    assert flow["latest_value"] is None
    assert flow["min_value"] == flow["max_value"] == "3.64"
    assert flow["approval_counts"] == {"Provisional": 2}
    missing = [s for key, s in payload["series"].items() if key != FLOW]
    assert len(missing) == 2 and all(s["sample_count"] == 0 for s in missing)
    assert all(s["latest_value"] is None for s in missing)
    assert "safe" not in payload and "risk_level" not in payload


def test_concurrent_pollers_have_one_lease(registered):
    path, store, _ = registered
    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = list(
            executor.map(lambda _: WatchStore(path).acquire_poll(MONITOR, now=FIRST), range(16))
        )
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    store.commit_poll(winners[0], batch(winners[0], item()), now=FIRST)
    assert store.status(MONITOR)["pending_count"] == 1


def test_expired_lease_cannot_commit_or_release_replacement(registered):
    _, store, _ = registered
    old = store.acquire_poll(MONITOR, now=FIRST)
    assert old is not None
    later = old.expires_at + timedelta(seconds=1)
    replacement = store.acquire_poll(MONITOR, now=later)
    assert replacement is not None and replacement.token != old.token
    with pytest.raises(LeaseLost):
        store.commit_poll(old, batch(old, item()), now=later)
    with pytest.raises(LeaseLost):
        store.fail_poll(old, SourceError("timeout", code="TRANSPORT"), now=later)
    store.commit_poll(replacement, batch(replacement, item()), now=later)
    assert store.status(MONITOR)["event_count"] == 1


def test_poll_batch_window_cannot_bypass_claim_scope(registered):
    _, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    body = json.dumps({"type": "FeatureCollection", "features": [], "links": []}).encode()
    forged = USGSClient(transport=lambda *a: HTTPResponse(200, body, {})).fetch(
        START, FIRST + timedelta(seconds=1), retrieved_at=FIRST + timedelta(seconds=1)
    )
    with pytest.raises(ValueError):
        store.commit_poll(lease, forged, now=FIRST + timedelta(seconds=1))
    assert store.status(MONITOR)["fetched_through"] == START.isoformat()
    store.commit_poll(lease, batch(lease, item()), now=FIRST + timedelta(seconds=1))


def test_source_failure_backoff_survives_restart_without_cursor_or_work_changes(registered):
    path, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    store.fail_poll(
        lease,
        SourceError("do not persist this raw text", code="HTTP", retry_after_seconds=600),
        now=FIRST,
    )
    restored = WatchStore(path)
    status = restored.status(MONITOR)
    assert status["status"] == "SOURCE_BACKOFF"
    assert status["fetched_through"] == START.isoformat()
    assert status["observation_count"] == status["pending_count"] == 0
    assert status["last_error_code"] == "HTTP" and status["consecutive_failures"] == 1
    assert status["next_poll_at"] == (FIRST + timedelta(seconds=600)).isoformat()
    assert restored.acquire_poll(MONITOR, now=FIRST + timedelta(seconds=599)) is None
    due = FIRST + timedelta(seconds=600)
    result = commit(restored, due, item())
    assert result.new_observations == 1
    assert restored.status(MONITOR)["consecutive_failures"] == 0
    assert restored.status(MONITOR)["last_error_code"] is None
    assert b"do not persist this raw text" not in path.read_bytes()


def test_local_write_failure_rolls_back_everything_and_exact_retry_works(registered):
    path, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    before = store.status(MONITOR)
    source = batch(lease, item())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON watch_events "
            "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError):
        store.commit_poll(lease, source, now=FIRST)
    assert store.status(MONITOR) == before
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER fail_event")
    result = store.commit_poll(lease, source, now=FIRST)
    assert len(result.event_ids) == 1
    assert store.status(MONITOR)["version_count"] == 1


def test_catchup_windows_are_bounded_and_do_not_skip_outage(registered):
    _, store, _ = registered
    far_future = START + timedelta(days=3)
    lease = store.acquire_poll(MONITOR, now=far_future)
    assert lease.start == START and lease.end == START + timedelta(days=1)
    store.commit_poll(lease, batch(lease), now=far_future)
    next_lease = store.acquire_poll(MONITOR, now=far_future + timedelta(minutes=5))
    assert next_lease.start == START + timedelta(days=1, minutes=-30)
    assert next_lease.end <= next_lease.start + timedelta(days=1)


def test_event_summary_is_bounded_and_reads_do_not_mutate_canonical_payload(registered):
    _, store, _ = registered
    samples = [item(START + timedelta(seconds=i), str(i % 10)) for i in range(800)]
    commit(store, FIRST, *samples)
    event = store.pending_events(MONITOR)[0]
    assert event.payload["observation_count"] == 800
    assert event.payload["series"][FLOW]["sample_count"] == 800
    assert len(json.dumps(event.payload)) < 6000
    event.payload["observation_count"] = -1
    assert store.pending_events(MONITOR)[0].payload["observation_count"] == 800


def test_invalid_monitor_config_rejected_before_registration(registered):
    _, store, _ = registered
    for changes in (
        {"interval_seconds": 0},
        {"poll_seconds": 0},
        {"overlap_seconds": -1},
        {"lease_seconds": 0},
        {"start_at": START.replace(tzinfo=None)},
    ):
        values = dict(monitor_id=MONITOR, station_id=SITE, case_id=CASE, start_at=START)
        values.update(changes)
        with pytest.raises(ValueError):
            config = MonitorConfig(**values)
            store.register(config, now=START)


def test_successful_scan_advances_through_sparse_data_and_does_not_get_stuck(registered):
    _, store, _ = registered
    later = START + timedelta(hours=3)
    commit(store, later, item())
    assert store.status(MONITOR)["fetched_through"] == later.isoformat()
    lease = store.acquire_poll(MONITOR, now=later + timedelta(minutes=5))
    assert lease.start == later - timedelta(minutes=30)


def test_intervals_are_utc_aligned_and_expose_partial_bootstrap_coverage(tmp_path):
    store = WatchStore(tmp_path / "partial.sqlite")
    bootstrap = START + timedelta(minutes=7)
    store.register(
        MonitorConfig(monitor_id=MONITOR, station_id=SITE, case_id=CASE, start_at=bootstrap),
        now=bootstrap,
    )
    commit(store, FIRST, item(START + timedelta(minutes=8)))
    event = store.pending_events(MONITOR)[0]
    assert event.start == START
    assert event.end == START + timedelta(minutes=15)
    assert event.payload["coverage_start"] == bootstrap.isoformat()
    assert event.payload["coverage_end"] == event.end.isoformat()


def test_storage_does_not_round_measurements_using_ambient_decimal_context(registered):
    _, store, _ = registered
    value = "3.12345678901234567890123456789"
    with localcontext() as context:
        context.prec = 4
        commit(store, FIRST, item(value=value))
    summary = store.pending_events(MONITOR)[0].payload["series"][FLOW]
    assert summary["latest_value"] == summary["min_value"] == summary["max_value"] == value


def test_lease_token_does_not_authorize_forged_request_scope(registered):
    _, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    forged = replace(lease, start=START + timedelta(minutes=5))
    before = store.status(MONITOR)
    with pytest.raises((ValueError, LeaseLost)):
        store.commit_poll(forged, batch(forged), now=FIRST)
    assert store.status(MONITOR) == before
    store.commit_poll(lease, batch(lease, item()), now=FIRST)


@pytest.mark.parametrize(
    "changes",
    [
        {"station_id": "USGS-OTHER"},
        {"unit": "m3/s"},
        {"parameter_code": "00045"},
        {"observed_at": START - timedelta(minutes=1)},
        {"approval_status": "Guaranteed"},
    ],
)
def test_store_validates_records_against_registered_series_and_lease(registered, changes):
    _, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    source = batch(lease, item())
    forged = replace(source, observations=(replace(source.observations[0], **changes),))
    with pytest.raises(ValueError):
        store.commit_poll(lease, forged, now=FIRST)
    assert store.status(MONITOR)["version_count"] == 0


def test_each_observation_version_retains_its_originating_fetch_receipt(registered):
    path, store, _ = registered
    commit(store, FIRST, item())
    commit(
        store, FIRST + timedelta(minutes=5), item(value="4", modified=FIRST + timedelta(minutes=5))
    )
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT v.value,p.start,p.end,p.pages_json "
            "FROM observation_versions v JOIN poll_receipts p ON p.poll_id=v.poll_id "
            "ORDER BY v.version_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "3.64" and rows[1][0] == "4"
        assert rows[0][2] != rows[1][2]
        assert all(len(json.loads(row[3])) == 1 for row in rows)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_database_connections_are_closed_after_operations(tmp_path, monkeypatch):
    connections = []
    original = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    store = WatchStore(tmp_path / "closed.sqlite")
    store.register(
        MonitorConfig(monitor_id=MONITOR, station_id=SITE, case_id=CASE, start_at=START), now=START
    )
    commit(store, FIRST, item())
    store.status(MONITOR)
    store.pending_events(MONITOR)
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_database_refuses_orphan_or_cross_monitor_references(registered):
    path, store, _ = registered
    commit(store, FIRST, item())
    other = MonitorConfig(
        monitor_id="other", station_id=SITE, case_id="OTHER-CURRENT", start_at=START
    )
    store.register(other, now=START)
    lease = store.acquire_poll("other", now=FIRST)
    store.commit_poll(lease, batch(lease, item(value="9")), now=FIRST)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        other_version = db.execute(
            "SELECT version_id FROM observation_versions WHERE monitor_id='other'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE observations_current SET version_id=? WHERE monitor_id=?",
                (other_version, MONITOR),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO watch_outbox(event_id,monitor_id) VALUES('missing-event',?)",
                (MONITOR,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM monitors WHERE monitor_id=?", (MONITOR,))


@pytest.mark.parametrize(
    "changes",
    [
        {"overlap_seconds": 86400},
        {"series": (None,)},
        {"case_id": "GALLINAS-HPCC-2022"},
    ],
)
def test_config_cannot_stall_catchup_or_use_historical_case(registered, changes):
    _, store, _ = registered
    values = dict(monitor_id="other", station_id=SITE, case_id="OTHER-CURRENT", start_at=START)
    values.update(changes)
    with pytest.raises(ValueError):
        config = MonitorConfig(**values)
        store.register(config, now=START)


def test_failure_before_acquisition_and_unknown_error_code_cannot_change_schedule(registered):
    _, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    before = store.status(MONITOR)
    with pytest.raises((ValueError, LeaseLost)):
        store.fail_poll(
            lease, SourceError("timeout", code="TRANSPORT"), now=FIRST - timedelta(seconds=1)
        )
    with pytest.raises(ValueError):
        store.fail_poll(lease, SourceError("untrusted", code="untrusted-status"), now=FIRST)
    assert store.status(MONITOR) == before


def test_many_source_qualifiers_cannot_create_unbounded_event_context(registered):
    _, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    items = [item(START + timedelta(seconds=i)) for i in range(40)]
    for i, sample in enumerate(items):
        sample["properties"]["qualifier"] = f"provider-flag-{i:03d}"
    with pytest.raises(ValueError):
        store.commit_poll(lease, batch(lease, *items), now=FIRST)
    assert store.status(MONITOR)["version_count"] == 0


def test_monitor_growth_queries_have_index_support(registered):
    path, _, _ = registered
    with sqlite3.connect(path) as db:
        version_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM observation_versions WHERE monitor_id=?",
            (MONITOR,),
        ).fetchall()
        pending_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM watch_outbox "
            "WHERE monitor_id=? AND status='PENDING'",
            (MONITOR,),
        ).fetchall()
    assert any("USING" in row[3] for row in version_plan)
    assert any("USING" in row[3] for row in pending_plan)


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://untrusted.example/data"},
        {"sha256": "not-a-digest"},
        {"byte_count": -1},
        {"retrieved_at": FIRST - timedelta(seconds=1)},
    ],
)
def test_page_receipts_must_bind_to_the_actual_source_batch(registered, changes):
    _, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    source = batch(lease, item())
    forged = replace(source, pages=(replace(source.pages[0], **changes),))
    with pytest.raises(ValueError):
        store.commit_poll(lease, forged, now=FIRST)
    assert store.status(MONITOR)["version_count"] == 0


@pytest.mark.parametrize("pages", [(), ("not-a-receipt",)])
def test_unattributed_batch_cannot_be_committed(registered, pages):
    _, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    source = replace(batch(lease, item()), pages=pages)
    with pytest.raises(ValueError):
        store.commit_poll(lease, source, now=FIRST)


@pytest.mark.parametrize("modified", [FIRST - timedelta(seconds=1), FIRST])
def test_conflicting_stale_or_ambiguous_revision_never_overwrites_current(registered, modified):
    _, store, _ = registered
    commit(store, FIRST, item())
    lease = store.acquire_poll(MONITOR, now=FIRST + timedelta(minutes=5))
    before = store.status(MONITOR)
    with pytest.raises(SourceError) as caught:
        store.commit_poll(
            lease,
            batch(lease, item(value="99", modified=modified)),
            now=FIRST + timedelta(minutes=5),
        )
    assert caught.value.code == "DATA_CONFLICT"
    assert store.status(MONITOR) == before


def test_republished_same_value_updates_revision_authority_without_new_event(registered):
    _, store, _ = registered
    observed = START + timedelta(minutes=10)
    commit(store, FIRST, item(observed, modified=START))
    commit(store, FIRST + timedelta(minutes=5), item(observed, modified=FIRST))
    assert store.status(MONITOR)["version_count"] == store.status(MONITOR)["event_count"] == 1
    lease = store.acquire_poll(MONITOR, now=FIRST + timedelta(minutes=10))
    with pytest.raises(SourceError):
        store.commit_poll(
            lease,
            batch(lease, item(observed, value="99", modified=FIRST - timedelta(seconds=1))),
            now=FIRST + timedelta(minutes=10),
        )


def test_database_binds_exact_identity_and_non_null_provenance(registered):
    path, store, _ = registered
    commit(store, FIRST, item(), item(START + timedelta(minutes=5)))
    store.register(
        MonitorConfig(
            monitor_id="another", station_id=SITE, case_id="ANOTHER-CURRENT", start_at=START
        ),
        now=START,
    )
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        versions = db.execute(
            "SELECT version_id FROM observation_versions ORDER BY version_id"
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE observations_current SET version_id=? WHERE version_id=?",
                (versions[1][0], versions[0][0]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE observation_versions SET poll_id=NULL")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE event_members SET version_id=999999")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE poll_receipts SET monitor_id='missing-monitor'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE watch_outbox SET monitor_id='another'")


def test_quiet_tick_and_status_do_not_scan_old_observation_history(registered, monkeypatch):
    path, store, _ = registered
    # Sealed history is irrelevant to a later empty poll, regardless of its size.
    commit(store, FIRST, *[item(START + timedelta(seconds=i)) for i in range(800)])
    queries = []
    original = sqlite3.connect

    def traced(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced)
    commit(WatchStore(path), FIRST + timedelta(minutes=5))
    store.status(MONITOR)
    reads = [sql.upper() for sql in queries if sql.lstrip().upper().startswith("SELECT")]
    assert not any("COUNT(" in sql for sql in reads)
    for sql in reads:
        if "FROM OBSERVATIONS_CURRENT" in sql:
            assert "OBSERVED_AT >=" in sql and "OBSERVED_AT <" in sql


def test_foreign_or_unsupported_database_is_preserved(tmp_path):
    path = tmp_path / "historical.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE historical_case(value TEXT)")
        db.execute("INSERT INTO historical_case VALUES('preserve')")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="supported current-watch"):
        WatchStore(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("historical_case",)
        ]


def test_extreme_failure_count_uses_capped_math_and_persisted_backoff(registered):
    path, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE monitors SET consecutive_failures=1000000")
    store.fail_poll(lease, SourceError("unavailable", code="TRANSPORT"), now=FIRST)
    status = store.status(MONITOR)
    assert status["consecutive_failures"] == 1000000
    assert status["next_poll_at"] == (FIRST + timedelta(hours=1)).isoformat()


def test_qualifier_limit_applies_to_all_interval_members_across_polls(registered):
    _, store, _ = registered
    samples = [item(START + timedelta(seconds=i)) for i in range(16)]
    for index, sample in enumerate(samples):
        sample["properties"]["qualifier"] = f"flag-{index}"
    commit(store, FIRST, *samples)
    lease = store.acquire_poll(MONITOR, now=FIRST + timedelta(minutes=5))
    before = store.status(MONITOR)
    sample = item(START + timedelta(minutes=5))
    sample["properties"]["qualifier"] = "seventeenth"
    with pytest.raises(ValueError, match="qualifier bound"):
        store.commit_poll(lease, batch(lease, sample), now=FIRST + timedelta(minutes=5))
    assert store.status(MONITOR) == before


def test_ten_thousand_observations_keep_quiet_and_correction_work_local(registered, monkeypatch):
    path, store, _ = registered
    for chunk in range(10):
        end = START + timedelta(seconds=(chunk + 1) * 1000)
        samples = [
            item(START + timedelta(seconds=i), str(i % 100), modified=end)
            for i in range(chunk * 1000, (chunk + 1) * 1000)
        ]
        commit(store, end, *samples)
    assert store.status(MONITOR)["observation_count"] == 10000
    assert all(len(json.dumps(e.payload)) < 6000 for e in store.pending_events(MONITOR))
    queries = []
    original = sqlite3.connect

    def traced(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced)
    later = START + timedelta(seconds=10800)
    # A later empty response seals just the one previously buffered interval.
    commit(WatchStore(path), later)
    store.status(MONITOR)
    interval_reads = [q for q in queries if "FROM observations_current" in q]
    assert len(interval_reads) == 1
    assert all("observed_at >=" in q and "observed_at <" in q for q in interval_reads)
    assert not any("COUNT(" in q.upper() for q in queries)
    queries.clear()
    later += timedelta(minutes=5)
    commit(store, later, item(START + timedelta(seconds=9900), "99.5", modified=later))
    assert store.status(MONITOR)["version_count"] == 10001
    reads = [q for q in queries if "FROM observations_current" in q]
    assert len(reads) == 2  # One stable-identity lookup and one changed interval.
    assert sum("observed_at >=" in q and "observed_at <" in q for q in reads) == 1


def test_batch_validation_holds_no_database_writer_lock(registered, monkeypatch):
    import watershed_memory.watch.store as storage

    path, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    original = storage.validate_batch

    def validate(*args, **kwargs):
        with sqlite3.connect(path, timeout=0.01) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "validate_batch", validate)
    store.commit_poll(lease, batch(lease, item()), now=FIRST)


def test_permuted_batch_is_rejected_before_assigning_version_ids(registered):
    _, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    source = batch(lease, item(), item(START + timedelta(minutes=5)))
    with pytest.raises(ValueError, match="order"):
        store.commit_poll(
            lease, replace(source, observations=tuple(reversed(source.observations))), now=FIRST
        )
    assert store.status(MONITOR)["version_count"] == 0


def test_future_publication_cannot_poison_correction_authority(registered):
    _, store, _ = registered
    lease = store.acquire_poll(MONITOR, now=FIRST)
    source = batch(lease, item(modified=FIRST + timedelta(days=1)))
    with pytest.raises(SourceError, match="publication"):
        store.commit_poll(lease, source, now=FIRST)
    assert store.status(MONITOR)["version_count"] == 0


def test_latest_publication_authority_retains_its_matching_poll_receipt(registered):
    path, store, _ = registered
    commit(store, FIRST, item())
    later = FIRST + timedelta(minutes=5)
    commit(store, later, item(modified=later, provider="refreshed-source"))
    assert store.status(MONITOR)["version_count"] == 1
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        seen = db.execute(
            "SELECT c.last_seen_modified_at,p.retrieved_at,p.pages_json "
            "FROM observations_current c JOIN poll_receipts p "
            "ON p.poll_id=c.last_seen_poll_id AND p.monitor_id=c.monitor_id"
        ).fetchone()
        assert seen[:2] == (later.isoformat(), later.isoformat())
        assert len(json.loads(seen[2])) == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE observations_current SET last_seen_poll_id=999999")


def test_large_pending_backlog_uses_index_order_before_limit(tmp_path, monkeypatch):
    path = tmp_path / "pending.sqlite"
    store = WatchStore(path)
    store.register(
        MonitorConfig(
            monitor_id=MONITOR, station_id=SITE, case_id=CASE, start_at=START, interval_seconds=1
        ),
        now=START,
    )
    for chunk in range(2):
        end = START + timedelta(seconds=(chunk + 1) * 1000)
        commit(
            store,
            end,
            *[
                item(START + timedelta(seconds=i), modified=end)
                for i in range(chunk * 1000, (chunk + 1) * 1000)
            ],
        )
    assert store.status(MONITOR)["pending_count"] == 2000
    queries = []
    original = sqlite3.connect

    def traced(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(queries.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    events = store.pending_events(MONITOR, limit=7)
    assert len(events) == 7 and events[0].start == START
    query = next(q for q in queries if "JOIN watch_events" in q)
    with sqlite3.connect(path) as db:
        plan = db.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert not any("TEMP B-TREE" in row[3] for row in plan)
