"""Exercise actual adapter -> persisted event -> agent facts with synthetic HTTP."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from watershed_memory.current import CoveragePolicy, gallinas_registry, inspect_interval
from watershed_memory.watch.store import MonitorConfig, WatchStore
from watershed_memory.watch.usgs import GALLINAS_SERIES, HTTPResponse, USGSClient

START = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)


def persist(tmp_path, values, *, qualifier=None):
    config = MonitorConfig("pipeline-monitor", "USGS-08380500", "PIPELINE-TEST", START)
    path = tmp_path / "current.sqlite"
    store = WatchStore(path)
    store.register(config, now=START)
    features = []
    for spec in GALLINAS_SERIES:
        samples = values if spec.parameter_code == "63680" else ["1"]
        for index, value in enumerate(samples):
            features.append(
                {
                    "type": "Feature",
                    "id": f"{spec.parameter_code}-{index}",
                    "properties": {
                        "monitoring_location_id": config.station_id,
                        "time_series_id": spec.series_id,
                        "parameter_code": spec.parameter_code,
                        "statistic_id": spec.statistic_id,
                        "unit_of_measure": spec.unit,
                        "time": (START + timedelta(minutes=index + 1)).isoformat(),
                        "value": value,
                        "approval_status": "Provisional",
                        "qualifier": qualifier,
                        "last_modified": END.isoformat(),
                    },
                }
            )
    body = json.dumps({"type": "FeatureCollection", "features": features, "links": []}).encode()
    client = USGSClient(transport=lambda *args: HTTPResponse(200, body, {}))
    lease = store.acquire_poll(config.monitor_id, now=END)
    batch = client.fetch(lease.start, lease.end, retrieved_at=END)
    result = store.commit_poll(lease, batch, now=END)
    assert len(result.event_ids) == 1
    # Reopen to cover the persisted representation, including decimal expansion.
    event = WatchStore(path).pending_events(config.monitor_id)[0]
    policy = CoveragePolicy("pipeline-v1", tuple(s.parameter_code for s in config.series))
    facts = inspect_interval(event, config, gallinas_registry(), policy, evaluated_at=END)
    return event, facts, next(s for s in facts.series if s.parameter_code == "63680")


def test_later_numeric_sample_restores_latest_value_after_earlier_null(tmp_path):
    _, facts, series = persist(tmp_path, [None, "4.25"])
    assert (series.sample_count, series.valid_count) == (2, 1)
    assert series.latest_value == Decimal("4.25")
    assert facts.interval_coverage == "SUFFICIENT"
    assert facts.null_latest_parameters == ()


@pytest.mark.parametrize("value", ["9" * 64 + "e12", "0." + "0" * 31 + "1"])
def test_facts_accept_the_store_representation_of_source_numeric_extremes(tmp_path, value):
    event, _, series = persist(tmp_path, [value])
    assert series.latest_value == Decimal(value)
    stored = event.payload["series"][series.series_id]["latest_value"]
    assert "e" not in stored.lower()
    assert Decimal(stored) == Decimal(value)


def test_empty_source_qualifier_is_valid_metadata_and_not_an_agent_instruction(tmp_path):
    event, facts, _ = persist(tmp_path, ["4.25"], qualifier="")
    assert all(
        summary["qualifier_counts"] == {"": 1} for summary in event.payload["series"].values()
    )
    assert facts.interval_coverage == "SUFFICIENT"
    assert not hasattr(facts.series[0], "qualifier_counts")
