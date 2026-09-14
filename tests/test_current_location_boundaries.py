"""Location authority cannot be inferred through coercion or malformed metadata."""

import io
import json
import socket
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_current_locations import NOW, entry

from watershed_memory.current import locations
from watershed_memory.current.locations import LocationEntry, LocationRegistry


def require(registry, revision=1, **changes):
    query = dict(case_id="CASE-1", simulated=True, activity="VISUAL_INSPECTION", now=NOW)
    query.update(changes)
    return registry.require_approved("FIELD-ALPHA", revision, **query)


@pytest.mark.parametrize("revision", [True, 1.0, Decimal("1"), "1", 0, 2**63])
def test_lookup_and_authorization_require_an_exact_bounded_revision(revision):
    registry = LocationRegistry((entry(),))
    with pytest.raises(ValueError):
        registry.get("FIELD-ALPHA", revision)
    with pytest.raises(ValueError):
        require(registry, revision)


@pytest.mark.parametrize("query", [
    {"case_id": "OTHER"}, {"simulated": False}, {"activity": "SAMPLING"},
    {"now": NOW - timedelta(seconds=1)}, {"simulated": 1},
])
def test_existing_site_does_not_authorize_a_different_context(query):
    with pytest.raises(ValueError):
        require(LocationRegistry((entry(),)), **query)


def test_reference_only_records_cannot_carry_approval_identity():
    with pytest.raises(ValueError):
        entry(status="REFERENCE_ONLY")
    station = locations.gallinas_location_registry().get("USGS-08380500", 1)
    with pytest.raises(ValueError):
        replace(station, approved_by="operator", approved_at=NOW)


@pytest.mark.parametrize("field", ["label", "source_label", "coordinate_accuracy"])
@pytest.mark.parametrize("text", ["Demonstration hidden\u200bcharacter", "Demonstration surrogate\ud800", "Demonstration line\nfeed"])
def test_attribution_uses_the_same_printable_text_boundary_as_the_case(field, text):
    values = dict(latitude=Decimal("35"), longitude=Decimal("-105"),
                  coordinate_system="WGS84", coordinate_accuracy="Demonstration accuracy")
    values[field] = text
    with pytest.raises(ValueError):
        entry(**values)


@pytest.mark.parametrize("value", [True, 35.0, Decimal("NaN"), Decimal("Infinity"),
                                   Decimal("90.1"), Decimal("1e-33")])
def test_coordinate_value_is_validated_with_an_otherwise_complete_pair(value):
    with pytest.raises(ValueError):
        entry(latitude=value, longitude=Decimal("0"), coordinate_system="WGS84",
              coordinate_accuracy="Demonstration accuracy")


@pytest.mark.parametrize("url", ["https://@example.com/x", "https://example.com/x?",
    "https://example.com/x#", "https://example.com/x\u200b", "https://example.com:444/x",
    "https://example.com\\@another.example/x", "https://example.com/x\x7f"])
def test_reference_url_has_no_hidden_identity_or_delimiters(url):
    with pytest.raises(ValueError):
        entry(source_url=url)


def test_https_default_port_and_origin_reference_are_allowed():
    assert entry(source_url="https://example.com:443/x").source_url.endswith(":443/x")
    assert entry(source_url="https://example.com").source_url == "https://example.com"


def test_registry_cannot_be_reassigned_after_approval_checks():
    registry = LocationRegistry((entry(),))
    with pytest.raises((AttributeError, TypeError)):
        registry._entries = (entry(status="WITHDRAWN"),)
    assert require(registry).status == "APPROVED"


def test_historical_revisions_remain_readable_but_latest_withdrawal_controls_planning():
    first = entry()
    withdrawn = replace(first, revision=2, status="WITHDRAWN")
    registry = LocationRegistry((withdrawn, first))
    assert registry.get(first.location_id, 1) == first
    with pytest.raises(ValueError):
        require(registry)
    with pytest.raises(ValueError):
        require(registry, 2)


class MetadataResource:
    def __init__(self, raw):
        self.raw = raw
        self.read_sizes = []

    def joinpath(self, name):
        assert name == "gallinas_location.json"
        return self

    def read_bytes(self):
        self.read_sizes.append(-1)
        return self.raw

    def open(self, mode):
        assert mode == "rb"
        resource = self

        class Reader(io.BytesIO):
            def read(self, size=-1):
                resource.read_sizes.append(size)
                return super().read(size)

        return Reader(self.raw)


@pytest.fixture
def packet():
    return json.loads(locations.resources.files("watershed_memory.data")
                      .joinpath("gallinas_location.json").read_bytes())


def fake_resource(monkeypatch, value):
    resource = MetadataResource(value if isinstance(value, bytes) else json.dumps(value).encode())
    monkeypatch.setattr(locations.resources, "files", lambda package: resource)
    return resource


@pytest.mark.parametrize("value", [True, 35.25, 35, "NaN", "３５", "1e999999", " 35", None])
def test_factory_cannot_coerce_coordinate_provenance(monkeypatch, packet, value):
    packet["latitude"] = value
    fake_resource(monkeypatch, packet)
    with pytest.raises(ValueError):
        locations.gallinas_location_registry()


@pytest.mark.parametrize("changes", [
    {"location_id": "OTHER-STATION"}, {"label": "Invented station"},
    {"latitude": "1"}, {"source_url": "https://example.com/claimed-usgs"},
    {"source_label": "Different but printable attribution"},
    {"coordinate_accuracy": "Unverified approximation"},
    {"recorded_at": "2026-09-13T20:11:00Z"},
    {"extra": "unexpected"}, {"activities": "STATION_DATA_REVIEW"},
])
def test_bundled_factory_enforces_its_official_reference_identity(monkeypatch, packet, changes):
    packet.update(changes)
    fake_resource(monkeypatch, packet)
    with pytest.raises(ValueError):
        locations.gallinas_location_registry()


@pytest.mark.parametrize("raw", [b'{"location_id":"a","location_id":"b"}', b'{"x":NaN}',
                                  b'[]', b'null', b'{', b'\xff'])
def test_malformed_metadata_is_rejected(monkeypatch, raw):
    fake_resource(monkeypatch, raw)
    with pytest.raises(ValueError):
        locations.gallinas_location_registry()


def test_factory_bounds_resource_read_before_parsing(monkeypatch):
    resource = fake_resource(monkeypatch, b" " * 100_000)
    with pytest.raises(ValueError):
        locations.gallinas_location_registry()
    assert resource.read_sizes == [64 * 1024 + 1]


def test_official_metadata_remains_exact_without_a_network_call(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("packaged metadata cannot perform network I/O")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    station = locations.gallinas_location_registry().get("USGS-08380500", 1)
    assert station.latitude == Decimal("35.6519944444444")
    assert station.longitude == Decimal("-105.318830555556")
    assert station.coordinate_system == "WGS84" and station.recorded_at == NOW
    assert station.approved_by is None and station.approved_at is None
    assert type(station) is LocationEntry
