"""Latest publication and replay, independent from queued interval age."""

import json
import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_current_case_store import CASE, CONFIG, MONITOR, NOW, START, query
from test_current_case_store import ready as ready

from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.context import load_context
from watershed_memory.current.source_health import _load_source_health, load_source_health
from watershed_memory.watch.store import MonitorConfig
from watershed_memory.watch.usgs import GALLINAS_SERIES, HTTPResponse, USGSClient


def publish(watch, at, *, observed_at=None, value="6", parameter="00060", provider="new"):
    spec = next(s for s in GALLINAS_SERIES if s.parameter_code == parameter)
    feature = {
        "type": "Feature",
        "id": provider,
        "properties": {
            "monitoring_location_id": "USGS-08380500",
            "time_series_id": spec.series_id,
            "parameter_code": spec.parameter_code,
            "statistic_id": spec.statistic_id,
            "unit_of_measure": spec.unit,
            "time": (observed_at or at).isoformat(),
            "value": value,
            "approval_status": "Provisional",
            "qualifier": None,
            "last_modified": at.isoformat(),
        },
    }
    body = json.dumps({"type": "FeatureCollection", "features": [feature], "links": []}).encode()
    lease = watch.acquire_poll(MONITOR, now=at)
    assert lease is not None
    batch = USGSClient(transport=lambda *a: HTTPResponse(200, body, {})).fetch(
        lease.start, lease.end, retrieved_at=at
    )
    return watch.commit_poll(lease, batch, now=at)


def health(store, at=NOW, **kw):
    return load_source_health(store, CASE, evaluated_at=at, **kw)


def test_latest_readings_have_source_identity_and_original_receipt(ready):
    _, store, _, _ = ready
    state = health(store)
    assert (state.case_id, state.monitor_id, state.source_id, state.station_id) == (
        CASE,
        MONITOR,
        "gallinas-usgs-current",
        "USGS-08380500",
    )
    assert state.policy == CONFIG.coverage_policy and state.evaluated_at == NOW
    assert tuple(s.series_id for s in state.series) == tuple(s.series_id for s in GALLINAS_SERIES)
    flow = state.series[0]
    assert flow.observed_at == START + timedelta(minutes=20)
    assert flow.value == Decimal("4") and flow.unit == "ft^3/s"
    assert flow.retrieved_at == flow.source_modified_at == NOW
    assert flow.approval_status == "Provisional" and flow.qualifier is None
    assert state.version_ids == (flow.version_id, None, None)
    assert state.missing_parameters == ("00045", "63680")
    assert state.stale_parameters == state.null_parameters == ()
    assert len(flow.semantic_hash) == 64
    assert "provider_id" not in asdict(flow)


def test_old_queued_interval_does_not_make_new_station_reading_stale(ready):
    _, store, watch, events = ready
    later = NOW + timedelta(minutes=70)
    publish(watch, later, observed_at=later - timedelta(minutes=5))
    assert (
        load_context(store, CASE, events[0].event_id, evaluated_at=later).current.freshness
        == "STALE"
    )
    state = health(store, later)
    assert state.stale_parameters == ()
    assert state.series[0].value == Decimal("6")
    assert state.series[0].observed_at == later - timedelta(minutes=5)


def test_null_latest_value_is_not_backfilled_from_an_older_numeric_value(ready):
    _, store, watch, _ = ready
    later = NOW + timedelta(minutes=5)
    publish(watch, later, value=None)
    state = health(store, later)
    assert state.series[0].value is None and state.series[0].version_id is not None
    assert state.null_parameters == ("00060",)
    assert state.missing_parameters == ("00045", "63680")
    assert state.stale_parameters == ()


def test_exact_freshness_boundary_and_missing_are_separate(ready):
    _, store, _, _ = ready
    boundary = START + timedelta(minutes=80)
    assert health(store, boundary).stale_parameters == ()
    state = health(store, boundary + timedelta(microseconds=1))
    assert state.stale_parameters == ("00060",)
    assert state.missing_parameters == ("00045", "63680")
    assert all(
        getattr(state.series[1], key) is None
        for key in (
            "version_id",
            "semantic_hash",
            "observed_at",
            "value",
            "approval_status",
            "qualifier",
            "source_modified_at",
            "retrieved_at",
        )
    )


def test_republication_or_older_correction_does_not_refresh_measurement_age(ready):
    _, store, watch, _ = ready
    original = health(store)
    later = NOW + timedelta(minutes=5)
    publish(
        watch,
        later,
        observed_at=START + timedelta(minutes=20),
        value="4.000",
        provider="uuid-refresh",
    )
    assert health(store, later).series == original.series
    later += timedelta(minutes=5)
    publish(watch, later, observed_at=START + timedelta(minutes=10), value="99")
    assert health(store, later).series == original.series


def test_pinned_version_and_missing_readings_replay_after_ingestion_and_restart(ready):
    path, store, watch, _ = ready
    original = health(store)
    later = NOW + timedelta(minutes=5)
    publish(watch, later, observed_at=START + timedelta(minutes=20), value="7")
    assert health(store, later).series[0].value == Decimal("7")
    later += timedelta(minutes=5)
    publish(watch, later, parameter="00045", value="0.25")
    assert health(store, later).missing_parameters == ("63680",)
    assert health(CaseStore(path), version_ids=original.version_ids) == original


@pytest.mark.parametrize(
    "pins",
    [
        (),
        (1,),
        (True, None, None),
        (-1, None, None),
        (0, None, None),
        (2**63, None, None),
        (999, None, None),
        (None, 1, None),
        (1, 1, None),
        [1, None, None],
    ],
)
def test_invalid_or_wrong_series_version_references_fail_closed(ready, pins):
    _, store, _, _ = ready
    with pytest.raises((ValueError, KeyError)):
        health(store, version_ids=pins)


def test_foreign_monitor_cannot_retrieve_another_monitor_version(ready):
    _, store, watch, _ = ready
    version = health(store).version_ids[0]
    watch.register(MonitorConfig("other", "USGS-08380500", "OTHER", START), now=START)
    store.register(replace(CONFIG, case_id="OTHER", monitor_id="other"), now=NOW)
    with pytest.raises((ValueError, KeyError)):
        load_source_health(store, "OTHER", evaluated_at=NOW, version_ids=(version, None, None))
    assert load_source_health(store, "OTHER", evaluated_at=NOW).version_ids == (None, None, None)


@pytest.mark.parametrize(
    "sql,args",
    [
        ("UPDATE observation_versions SET value=?", ("NaN",)),
        ("UPDATE observation_versions SET value=?", ("1e999999",)),
        ("UPDATE observation_versions SET value=?", ("4.1",)),
        ("UPDATE observation_versions SET unit=?", ("m3/s",)),
        ("UPDATE observation_versions SET semantic_hash=?", ("f" * 64,)),
        ("UPDATE observation_versions SET approval_status=?", ("invented",)),
        (
            "UPDATE observation_versions SET source_modified_at=?",
            ((NOW + timedelta(seconds=61)).isoformat(),),
        ),
        ("UPDATE poll_receipts SET retrieved_at=?", ((NOW + timedelta(seconds=1)).isoformat(),)),
        ("UPDATE poll_receipts SET start=?", (NOW.isoformat(),)),
        ("UPDATE monitors SET station_id=?", ("USGS-99999999",)),
        ("UPDATE monitors SET start_at=?", ((START - timedelta(seconds=1)).isoformat(),)),
    ],
)
def test_corrupt_or_future_source_cannot_be_presented_as_a_reading(ready, sql, args):
    path, store, _, _ = ready
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql, args)
        db.commit()
    with pytest.raises((ValueError, KeyError)):
        health(store)


def test_no_implicit_historical_view_after_new_source_arrives(ready):
    _, store, watch, _ = ready
    publish(watch, NOW + timedelta(minutes=5))
    with pytest.raises(ValueError):
        health(store)


def test_snapshot_is_immutable_and_reader_has_no_write_or_network_effects(ready):
    path, store, watch, _ = ready
    before = (watch.status(MONITOR), store.context(CASE), query(path, "SELECT * FROM watch_outbox"))
    state = health(store)
    with pytest.raises(FrozenInstanceError):
        state.series[0].value = Decimal("0")
    with pytest.raises(FrozenInstanceError):
        state.series = ()
    assert before == (
        watch.status(MONITOR),
        store.context(CASE),
        query(path, "SELECT * FROM watch_outbox"),
    )
    with store._connect() as db:
        with pytest.raises(ValueError):
            _load_source_health(db, CASE, evaluated_at=NOW)
        db.execute("BEGIN")
        assert _load_source_health(db, CASE, evaluated_at=NOW) == state


@pytest.mark.parametrize(
    "change",
    [
        {"series": []},
        {"series": ()},
        {"evaluated_at": NOW.replace(tzinfo=None)},
        {"evaluated_at": START},
        {"case_id": "invalid\n"},
        {"policy": {}},
    ],
)
def test_snapshot_record_rejects_invalid_internal_capabilities(ready, change):
    _, store, _, _ = ready
    with pytest.raises((ValueError, TypeError)):
        replace(health(store), **change)


def test_current_pointer_must_match_version_measurement_time(ready):
    path, store, _, _ = ready
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE observations_current SET version_id=1 WHERE version_id=2")
        db.commit()
    with pytest.raises(ValueError):
        health(store)


@pytest.mark.parametrize(
    "field,value",
    [
        ("unit", "fabricated"),
        ("statistic_id", "99999"),
        ("case_id", "OTHER"),
    ],
)
def test_registered_monitor_layout_cannot_be_silently_relabelled(ready, field, value):
    path, store, _, _ = ready
    with closing(sqlite3.connect(path)) as db:
        raw = json.loads(db.execute("SELECT config_json FROM monitors").fetchone()[0])
        if field == "case_id":
            raw[field] = value
        else:
            raw["series"][0][field] = value
        db.execute("UPDATE monitors SET config_json=?",
            (json.dumps(raw, sort_keys=True, separators=(",", ":")),))
        db.commit()
    with pytest.raises(ValueError):
        health(store)


@pytest.mark.parametrize(
    "changes",
    [
        {"unit": "fabricated"},
        {"source_modified_at": None},
        {"approval_status": None},
        {"value": Decimal("NaN")},
        {"value": Decimal("12")},
        {"qualifier": "bad\nmetadata"},
        {"observed_at": NOW.replace(tzinfo=None)},
        {"semantic_hash": "e" * 64},
        {"series_id": "unregistered"},
    ],
)
def test_immutable_reading_capability_cannot_be_forged(ready, changes):
    _, store, _, _ = ready
    original = health(store)
    with pytest.raises((ValueError, TypeError)):
        replace(original, series=(replace(original.series[0], **changes), *original.series[1:]))


def test_required_parameter_order_is_preserved_and_sampling_minimum_is_not_latest_coverage(ready):
    _, store, _, _ = ready
    original = health(store)
    policy = replace(
        original.policy, required_parameters=("63680", "00045", "00060"), minimum_valid_samples=99
    )
    assert replace(original, policy=policy).missing_parameters == ("63680", "00045")
    assert replace(original, policy=policy).null_parameters == ()


@pytest.mark.parametrize("created", ["2026-09-12 12:30:00", "2026-09-12T12:30:00"])
def test_noncanonical_case_creation_time_is_refused(ready, created):
    path, store, _, _ = ready
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE current_cases SET created_at=?", (created,))
        db.commit()
    with pytest.raises(ValueError):
        health(store)
