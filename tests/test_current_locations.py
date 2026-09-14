"""Named places retain attribution without granting implied field authority."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from watershed_memory.current.locations import (
    LocationEntry,
    LocationRegistry,
    gallinas_location_registry,
)

NOW = datetime(2026, 9, 12, 20, 11, tzinfo=timezone.utc)
URL = "https://waterdata.usgs.gov/monitoring-location/USGS-08380500/"


def entry(**changes):
    values = {
        "location_id": "FIELD-ALPHA",
        "revision": 1,
        "label": "Gallinas field site",
        "kind": "FIELD_SITE",
        "case_id": "CASE-1",
        "simulated": True,
        "status": "APPROVED",
        "activities": ("VISUAL_INSPECTION",),
        "latitude": None,
        "longitude": None,
        "coordinate_system": None,
        "coordinate_accuracy": None,
        "source_url": None,
        "source_label": "Demonstration field site configuration",
        "recorded_at": NOW,
        "approved_by": "operator-1",
        "approved_at": NOW,
    }
    values.update(changes)
    return LocationEntry(**values)


def test_entries_are_frozen_and_registry_is_deterministic():
    first = entry(location_id="FIELD-Z")
    second = entry(location_id="FIELD-A")
    registry = LocationRegistry((first, second))

    assert registry.get("FIELD-Z", 1) == first
    assert tuple(item.location_id for item in registry.approved_for(
        "CASE-1", simulated=True, activity="VISUAL_INSPECTION", now=NOW
    )) == ("FIELD-A", "FIELD-Z")
    with pytest.raises((AttributeError, TypeError)):
        first.label = "changed"


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 0},
        {"revision": True},
        {"label": ""},
        {"activities": ("VISUAL_INSPECTION", "VISUAL_INSPECTION")},
        {"latitude": float("nan")},
        {"longitude": float("inf")},
        {"latitude": Decimal("1e100000")},
        {"source_url": "http://waterdata.usgs.gov/x"},
        {"source_url": "https://user:secret@waterdata.usgs.gov/x"},
        {"source_url": "https://waterdata.usgs.gov/x?private=1"},
        {"source_url": "https://waterdata.usgs.gov/x#fragment"},
    ],
)
def test_entry_rejects_unbounded_or_unsafe_scalars(changes):
    with pytest.raises((TypeError, ValueError)):
        entry(**changes)


def test_station_is_reference_only_and_cannot_authorize_field_work():
    station = LocationEntry(
        location_id="USGS-08380500",
        revision=1,
        label="GALLINAS CREEK NEAR MONTEZUMA, NM",
        kind="MONITORING_STATION",
        case_id=None,
        simulated=False,
        status="REFERENCE_ONLY",
        activities=("STATION_DATA_REVIEW",),
        latitude=Decimal("35.6519944444444"),
        longitude=Decimal("-105.318830555556"),
        coordinate_system="WGS84",
        coordinate_accuracy="Accurate to +/-1 second; interpolated from digital map",
        source_url=URL,
        source_label="USGS Water Data for the Nation monitoring-location metadata",
        recorded_at=NOW,
        approved_by=None,
        approved_at=None,
    )
    registry = LocationRegistry((station,))
    assert registry.approved_for(
        "CASE-1", simulated=False, activity="STATION_DATA_REVIEW", now=NOW
    ) == ()
    with pytest.raises((KeyError, ValueError)):
        registry.require_approved(
            station.location_id, 1, case_id="CASE-1", simulated=False,
            activity="STATION_DATA_REVIEW", now=NOW
        )


def test_newer_withdrawal_removes_older_approved_revision():
    approved = entry(revision=1)
    withdrawn = entry(revision=2, status="WITHDRAWN", approved_at=NOW)
    registry = LocationRegistry((approved, withdrawn))
    assert registry.approved_for(
        "CASE-1", simulated=True, activity="VISUAL_INSPECTION", now=NOW
    ) == ()
    with pytest.raises((KeyError, ValueError)):
        registry.require_approved(
            "FIELD-ALPHA", 1, case_id="CASE-1", simulated=True,
            activity="VISUAL_INSPECTION", now=NOW
        )


def test_registry_rejects_duplicate_versions_and_unbounded_size():
    with pytest.raises(ValueError):
        LocationRegistry((entry(), entry()))
    with pytest.raises(ValueError):
        LocationRegistry(tuple(entry(location_id=f"FIELD-{index}") for index in range(1001)))


def test_real_approved_site_requires_attributed_coordinates():
    with pytest.raises(ValueError):
        entry(simulated=False, latitude=None, longitude=None,
              coordinate_system=None, coordinate_accuracy=None)


def test_future_record_is_not_eligible_until_time_passes():
    future = entry(recorded_at=NOW.replace(year=2027), approved_at=NOW.replace(year=2027))
    registry = LocationRegistry((future,))
    assert registry.approved_for(
        "CASE-1", simulated=True, activity="VISUAL_INSPECTION", now=NOW
    ) == ()



def test_simulation_and_case_scope_are_exact():
    registry = LocationRegistry((entry(), entry(location_id="FIELD-BETA", case_id="CASE-2")))
    assert len(registry.approved_for(
        "CASE-1", simulated=True, activity="VISUAL_INSPECTION", now=NOW
    )) == 1
    assert registry.approved_for(
        "CASE-1", simulated=False, activity="VISUAL_INSPECTION", now=NOW
    ) == ()


def test_official_factory_has_only_the_reference_station():
    registry = gallinas_location_registry()
    station = registry.get("USGS-08380500", 1)
    assert station.label == "GALLINAS CREEK NEAR MONTEZUMA, NM"
    assert station.source_url == URL
    assert station.source_label.startswith("USGS Water Data for the Nation")
    assert station.status == "REFERENCE_ONLY"
    assert station.case_id is None
    assert station.activities == ("STATION_DATA_REVIEW",)
