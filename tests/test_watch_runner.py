"""A real durable store with fake sources establishes autonomous runner behavior."""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from watershed_memory.watch.observations import SourceError
from watershed_memory.watch.runner import WatchRunner
from watershed_memory.watch.store import MonitorConfig, WatchStore
from watershed_memory.watch.usgs import HTTPResponse, USGSClient

START = datetime(2026, 9, 12, 11, tzinfo=timezone.utc)
NOW = START + timedelta(minutes=30)
MONITOR = "gallinas-runner"


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Source:
    def __init__(self, callback=None):
        self.calls = 0
        self.callback = callback

    def fetch(self, start, end, *, retrieved_at):
        self.calls += 1
        if self.callback:
            self.callback()
        body = json.dumps({"type": "FeatureCollection", "features": [], "links": []}).encode()
        return USGSClient(transport=lambda *a: HTTPResponse(200, body, {})).fetch(
            start, end, retrieved_at=retrieved_at
        )


@pytest.fixture
def ready(tmp_path):
    path = tmp_path / "runner.sqlite"
    store = WatchStore(path)
    store.register(
        MonitorConfig(
            monitor_id=MONITOR,
            station_id="USGS-08380500",
            case_id="GALLINAS-RUNNER-CURRENT",
            start_at=START,
        ),
        now=START,
    )
    return path, store, Clock()


def test_tick_commits_once_and_due_gate_prevents_extra_source_calls(ready):
    _, store, clock = ready
    source = Source()
    runner = WatchRunner(store, source, clock=clock)
    first = runner.tick(MONITOR)
    assert first.outcome == "NO_SOURCE_CHANGE"
    assert first.poll_result is not None and first.error_code is None
    assert first.next_poll_at == (NOW + timedelta(minutes=5)).isoformat()
    assert runner.tick(MONITOR).outcome == "NOT_DUE"
    assert source.calls == 1


def test_source_fetch_has_no_open_database_write_transaction(ready):
    path, store, clock = ready

    def independent_writer():
        with sqlite3.connect(path, timeout=0.05) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()

    result = WatchRunner(store, Source(independent_writer), clock=clock).tick(MONITOR)
    assert result.outcome == "NO_SOURCE_CHANGE"


def test_source_failure_is_durable_and_next_tick_is_not_a_hidden_retry(ready):
    path, store, clock = ready

    def fail():
        raise SourceError("network unavailable", code="TRANSPORT")

    source = Source(fail)
    result = WatchRunner(store, source, clock=clock).tick(MONITOR)
    assert result.outcome == "SOURCE_BACKOFF" and result.error_code == "TRANSPORT"
    assert result.poll_result is None
    restored = WatchRunner(WatchStore(path), source, clock=clock)
    assert restored.tick(MONITOR).outcome == "NOT_DUE"
    assert source.calls == 1


def test_source_finishing_after_lease_expiry_cannot_commit(ready):
    _, store, clock = ready

    def finish_late():
        clock.now += timedelta(minutes=6)

    result = WatchRunner(store, Source(finish_late), clock=clock).tick(MONITOR)
    assert result.outcome == "LEASE_LOST"
    assert store.status(MONITOR)["fetched_through"] == START.isoformat()


def test_unexpected_programming_failure_propagates_without_fake_source_status(ready):
    _, store, clock = ready

    def broken():
        raise RuntimeError("implementation defect")

    with pytest.raises(RuntimeError, match="implementation defect"):
        WatchRunner(store, Source(broken), clock=clock).tick(MONITOR)
    assert store.status(MONITOR)["consecutive_failures"] == 0


def test_run_stops_at_poll_budget_without_hidden_background_work(ready):
    _, store, clock = ready
    source = Source()
    results = WatchRunner(store, source, clock=clock).run(MONITOR, max_polls=1, max_seconds=2)
    assert source.calls == 1
    assert len(results) == 1 and results[0].outcome == "NO_SOURCE_CHANGE"


def test_stop_signal_prevents_new_work(ready):
    _, store, clock = ready
    stop = threading.Event()
    stop.set()
    source = Source()
    results = WatchRunner(store, source, clock=clock).run(
        MONITOR, max_polls=2, max_seconds=2, stop_event=stop
    )
    assert source.calls == 0 and results == ()


@pytest.mark.parametrize(
    "options",
    [
        {"max_polls": 0, "max_seconds": 10},
        {"max_polls": 101, "max_seconds": 10},
        {"max_polls": 1, "max_seconds": 0},
        {"max_polls": True, "max_seconds": 10},
        {"max_polls": 1, "max_seconds": 21601},
    ],
)
def test_run_limits_are_checked_before_fetch(ready, options):
    _, store, clock = ready
    source = Source()
    with pytest.raises(ValueError):
        WatchRunner(store, source, clock=clock).run(MONITOR, **options)
    assert source.calls == 0


def test_source_error_after_lease_expiry_returns_lease_lost(ready):
    _, store, clock = ready

    def late_error():
        clock.now += timedelta(minutes=6)
        raise SourceError("late timeout", code="TRANSPORT")

    result = WatchRunner(store, Source(late_error), clock=clock).tick(MONITOR)
    assert result.outcome == "LEASE_LOST"
    assert store.status(MONITOR)["consecutive_failures"] == 0


def test_run_never_automatically_retries_a_lost_lease(ready):
    _, store, clock = ready

    def late_success():
        clock.now += timedelta(minutes=6)

    source = Source(late_success)
    results = WatchRunner(store, source, clock=clock).run(MONITOR, max_polls=2, max_seconds=2)
    assert len(results) == source.calls == 1
    assert results[0].outcome == "LEASE_LOST"


def test_not_due_waits_without_repeated_database_polling(ready, monkeypatch):
    _, store, clock = ready
    runner = WatchRunner(store, Source(), clock=clock)
    runner.tick(MONITOR)
    ticks = []
    status_calls = []
    original = runner.tick
    original_status = store.status

    def counted_tick(monitor):
        ticks.append(monitor)
        return original(monitor)

    def counted_status(monitor):
        status_calls.append(monitor)
        return original_status(monitor)

    elapsed = [0.0]

    class WaitClock:
        def is_set(self):
            return False

        def wait(self, seconds):
            assert 0 < seconds <= 1
            elapsed[0] += seconds
            return False

    monkeypatch.setattr(runner, "tick", counted_tick)
    monkeypatch.setattr(store, "status", counted_status)
    monkeypatch.setattr("watershed_memory.watch.runner.time.monotonic", lambda: elapsed[0])
    assert runner.run(MONITOR, max_polls=2, max_seconds=5, stop_event=WaitClock()) == ()
    assert len(ticks) == 1
    assert len(status_calls) == 1


@pytest.mark.parametrize("due", [None, "broken", "2026-09-12T11:35:00"])
def test_invalid_persisted_due_time_propagates_without_a_retry_loop(ready, monkeypatch, due):
    from watershed_memory.watch.runner import TickResult

    _, store, clock = ready
    runner = WatchRunner(store, Source(), clock=clock)
    monkeypatch.setattr(runner, "tick", lambda *a: TickResult("NOT_DUE", None, None, due))
    with pytest.raises((ValueError, TypeError)):
        runner.run(MONITOR, max_polls=2, max_seconds=1)


def test_source_conflict_during_commit_is_a_durable_source_failure(ready, monkeypatch):
    _, store, clock = ready

    def source_conflict(*args, **kwargs):
        raise SourceError("ambiguous stale revision", code="DATA_CONFLICT")

    monkeypatch.setattr(store, "commit_poll", source_conflict)
    result = WatchRunner(store, Source(), clock=clock).tick(MONITOR)
    assert result.outcome == "SOURCE_BACKOFF" and result.error_code == "DATA_CONFLICT"


def test_another_active_poll_lease_does_not_cause_busy_database_retries(ready, monkeypatch):
    _, store, clock = ready
    assert store.acquire_poll(MONITOR, now=clock()) is not None
    source = Source()
    runner = WatchRunner(store, source, clock=clock)
    calls = []
    original = store.status
    elapsed = [0.0]

    def status(monitor):
        calls.append(monitor)
        return original(monitor)

    def monotonic():
        # Also moves on a spin loop so a broken implementation fails instead of hanging.
        elapsed[0] += 0.01
        return elapsed[0]

    class Stop:
        def is_set(self):
            return False

        def wait(self, seconds):
            assert 0 < seconds <= 1
            elapsed[0] += seconds
            clock.now += timedelta(seconds=seconds)
            return False

    monkeypatch.setattr(store, "status", status)
    monkeypatch.setattr("watershed_memory.watch.runner.time.monotonic", monotonic)
    assert runner.run(MONITOR, max_polls=2, max_seconds=5, stop_event=Stop()) == ()
    assert source.calls == 0
    assert len(calls) == 1
