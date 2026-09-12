"""Attributed reference places and explicitly approved, case-bound field sites."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from importlib import resources
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from .case_types import _identifier, _text
from .fact_validation import bounded_number, decimal_value, timestamp, utc

_KINDS = {"MONITORING_STATION", "FIELD_SITE"}
_STATUSES = {"REFERENCE_ONLY", "APPROVED", "WITHDRAWN"}
_ACTIVITIES = {"STATION_DATA_REVIEW", "VISUAL_INSPECTION", "SAMPLING", "MAINTENANCE_REVIEW"}
_MAX_METADATA_BYTES = 64 * 1024


def _revision(value: int) -> int:
    if type(value) is not int or not 1 <= value < 2**63:
        raise ValueError("invalid location revision")
    return value


def _url(value: str) -> str:
    _text(value, "source_url", 1, 512)
    if any(char.isspace() for char in value) or any(char in value for char in "?#\\"):
        raise ValueError("invalid source_url")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("invalid source_url")
    return value


@dataclass(frozen=True, slots=True)
class LocationEntry:
    location_id: str
    revision: int
    label: str
    kind: str
    case_id: str | None
    simulated: bool
    status: str
    activities: tuple[str, ...]
    latitude: Decimal | None
    longitude: Decimal | None
    coordinate_system: str | None
    coordinate_accuracy: str | None
    source_url: str | None
    source_label: str
    recorded_at: datetime
    approved_by: str | None
    approved_at: datetime | None

    def __post_init__(self) -> None:
        _identifier(self.location_id, "location_id")
        _revision(self.revision)
        _text(self.label, "label", 1, 160)
        _text(self.source_label, "source_label", 1, 200)
        if type(self.kind) is not str or self.kind not in _KINDS:
            raise ValueError("invalid location kind")
        if type(self.status) is not str or self.status not in _STATUSES:
            raise ValueError("invalid location status")
        if type(self.simulated) is not bool:
            raise ValueError("invalid location simulation flag")
        if self.case_id is not None:
            _identifier(self.case_id, "case_id")
        if (
            type(self.activities) is not tuple or not 1 <= len(self.activities) <= 4
            or any(type(item) is not str or item not in _ACTIVITIES for item in self.activities)
            or len(set(self.activities)) != len(self.activities)
        ):
            raise ValueError("invalid location activities")

        has_coordinates = self.latitude is not None or self.longitude is not None
        if has_coordinates:
            bounded_number(self.latitude)
            bounded_number(self.longitude)
            if not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180:
                raise ValueError("coordinates outside geographic bounds")
            if type(self.coordinate_system) is not str or self.coordinate_system != "WGS84":
                raise ValueError("coordinates require WGS84 attribution")
            _text(self.coordinate_accuracy, "coordinate_accuracy", 1, 160)
        elif self.coordinate_system is not None or self.coordinate_accuracy is not None:
            raise ValueError("coordinate metadata requires a coordinate pair")
        if self.source_url is not None:
            _url(self.source_url)

        recorded = utc(self.recorded_at)
        approved = utc(self.approved_at) if self.approved_at is not None else None
        if (self.approved_by is None) != (approved is None):
            raise ValueError("approval identity and time must be paired")
        if self.approved_by is not None:
            _identifier(self.approved_by, "approved_by")
        if approved is not None and approved > recorded:
            raise ValueError("approval follows the recorded version")
        if self.status == "REFERENCE_ONLY" and self.approved_by is not None:
            raise ValueError("reference-only places cannot carry approval")

        if self.kind == "MONITORING_STATION":
            if (
                self.case_id is not None or self.simulated or self.status != "REFERENCE_ONLY"
                or self.activities != ("STATION_DATA_REVIEW",)
                or self.source_url is None or not has_coordinates
            ):
                raise ValueError("invalid reference-station policy")
        else:
            if self.case_id is None or "STATION_DATA_REVIEW" in self.activities:
                raise ValueError("a field site requires a case and field activities")
            if self.status in {"APPROVED", "WITHDRAWN"} and approved is None:
                raise ValueError("field-site authority must be attributed")
            if self.status == "APPROVED" and not self.simulated and not has_coordinates:
                raise ValueError("real approved field sites require attributed coordinates")
            if self.simulated and not any(
                word in self.source_label.lower().split()
                for word in ("demo", "simulated", "demonstration")
            ):
                raise ValueError("simulated sites require explicit demonstration provenance")
        object.__setattr__(self, "recorded_at", recorded)
        object.__setattr__(self, "approved_at", approved)


def _query(case_id, simulated, activity, now):
    _identifier(case_id, "case_id")
    if type(simulated) is not bool or type(activity) is not str or activity not in _ACTIVITIES:
        raise ValueError("invalid location query")
    return utc(now)


def _eligible(entry, case_id, simulated, activity, now):
    return (
        entry.kind == "FIELD_SITE" and entry.status == "APPROVED"
        and entry.case_id == case_id and entry.simulated == simulated
        and activity in entry.activities and entry.recorded_at <= now
    )


@dataclass(frozen=True, slots=True, init=False)
class LocationRegistry:
    _entries: tuple[LocationEntry, ...]
    _by_revision: Mapping[tuple[str, int], LocationEntry] = field(repr=False)
    _current_by_id: Mapping[str, LocationEntry] = field(repr=False)
    _current_entries: tuple[LocationEntry, ...] = field(repr=False)

    def __init__(self, entries: tuple[LocationEntry, ...]) -> None:
        if type(entries) is not tuple or not 1 <= len(entries) <= 1000:
            raise ValueError("entries must be a bounded tuple")
        if any(type(entry) is not LocationEntry for entry in entries):
            raise ValueError("invalid location entry")
        indexed, current = {}, {}
        for entry in entries:
            key = entry.location_id, entry.revision
            if key in indexed:
                raise ValueError("duplicate location revision")
            indexed[key] = entry
            prior = current.get(entry.location_id)
            if prior is None or entry.revision > prior.revision:
                current[entry.location_id] = entry
        object.__setattr__(self, "_entries", entries)
        object.__setattr__(self, "_by_revision", MappingProxyType(indexed))
        object.__setattr__(self, "_current_by_id", MappingProxyType(current))
        object.__setattr__(self, "_current_entries", tuple(current[key] for key in sorted(current)))

    def get(self, location_id: str, revision: int) -> LocationEntry:
        _identifier(location_id, "location_id")
        _revision(revision)
        return self._by_revision[(location_id, revision)]

    def approved_for(
        self, case_id: str, *, simulated: bool, activity: str, now: datetime,
    ) -> tuple[LocationEntry, ...]:
        moment = _query(case_id, simulated, activity, now)
        return tuple(
            entry for entry in self._current_entries
            if _eligible(entry, case_id, simulated, activity, moment)
        )

    def require_approved(
        self, location_id: str, revision: int, *, case_id: str,
        simulated: bool, activity: str, now: datetime,
    ) -> LocationEntry:
        moment = _query(case_id, simulated, activity, now)
        entry = self.get(location_id, revision)
        if (
            self._current_by_id[location_id] is not entry
            or not _eligible(entry, case_id, simulated, activity, moment)
        ):
            raise ValueError("location revision is not approved for this context")
        return entry


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate location metadata key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("nonfinite location metadata")


def _official_reference() -> LocationEntry:
    return LocationEntry(
        location_id="USGS-08380500", revision=1,
        label="GALLINAS CREEK NEAR MONTEZUMA, NM", kind="MONITORING_STATION",
        case_id=None, simulated=False, status="REFERENCE_ONLY",
        activities=("STATION_DATA_REVIEW",),
        latitude=Decimal("35.6519944444444"), longitude=Decimal("-105.318830555556"),
        coordinate_system="WGS84",
        coordinate_accuracy="Accurate to +/-1 second; interpolated from digital map",
        source_url="https://waterdata.usgs.gov/monitoring-location/USGS-08380500/",
        source_label="USGS Water Data for the Nation monitoring-location metadata",
        recorded_at=timestamp("2026-09-12T20:11:00Z"), approved_by=None, approved_at=None,
    )


def gallinas_location_registry() -> LocationRegistry:
    resource = resources.files("watershed_memory.data").joinpath("gallinas_location.json")
    with resource.open("rb") as stream:
        raw = stream.read(_MAX_METADATA_BYTES + 1)
    if len(raw) > _MAX_METADATA_BYTES:
        raise ValueError("location metadata exceeds its size bound")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_nonfinite)
    except RecursionError as error:
        raise ValueError("location metadata nesting exceeds its bound") from error
    if type(data) is not dict or set(data) != set(LocationEntry.__dataclass_fields__):
        raise ValueError("unexpected location metadata fields")
    if type(data["activities"]) is not list or not 1 <= len(data["activities"]) <= 4:
        raise ValueError("metadata activities must be a bounded array")
    data["activities"] = tuple(data["activities"])
    data["latitude"] = decimal_value(data["latitude"])
    data["longitude"] = decimal_value(data["longitude"])
    data["recorded_at"] = timestamp(data["recorded_at"])
    if data["approved_at"] is not None:
        data["approved_at"] = timestamp(data["approved_at"])
    entry = LocationEntry(**data)
    if entry != _official_reference():
        raise ValueError("bundled metadata differs from the attributed Gallinas reference")
    return LocationRegistry((entry,))
