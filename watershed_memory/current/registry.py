"""Explicit source configuration; this module never performs I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..watch.usgs import GALLINAS_SERIES, SeriesSpec

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def _identifier(value: str, name: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class SourceRegistration:
    source_id: str
    station_id: str
    label: str
    series: tuple[SeriesSpec, ...]
    scope: str = "CURRENT_OBSERVATION"

    def __post_init__(self):
        _identifier(self.source_id, "source_id")
        if (
            type(self.station_id) is not str
            or len(self.station_id) > 64
            or not re.fullmatch(r"USGS-[0-9]+\Z", self.station_id, flags=re.ASCII)
        ):
            raise ValueError("invalid station_id")
        if (
            type(self.label) is not str
            or not self.label
            or len(self.label) > 256
            or not all(char.isprintable() for char in self.label)
        ):
            raise ValueError("invalid source label")
        if (
            type(self.series) is not tuple
            or not self.series
            or len(self.series) > 16
            or any(not isinstance(item, SeriesSpec) for item in self.series)
        ):
            raise ValueError("invalid source series")
        if len({item.series_id for item in self.series}) != len(self.series) or len(
            {item.parameter_code for item in self.series}
        ) != len(self.series):
            raise ValueError("duplicate source parameter")
        if self.scope not in {"CURRENT_OBSERVATION", "HISTORICAL_MEASUREMENT"}:
            raise ValueError("invalid source scope")


@dataclass(frozen=True, slots=True)
class Compatibility:
    origin_id: str
    alternate_id: str
    parameter_code: str
    evidence_id: str

    def __post_init__(self):
        _identifier(self.origin_id, "origin_id")
        _identifier(self.alternate_id, "alternate_id")
        if type(self.parameter_code) is not str or not re.fullmatch(
            r"[0-9]{5}", self.parameter_code
        ):
            raise ValueError("invalid compatibility parameter")
        _identifier(self.evidence_id, "evidence_id")


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    sources: tuple[SourceRegistration, ...]
    compatibility: tuple[Compatibility, ...] = ()

    def __post_init__(self):
        sources, compatibility = self.sources, self.compatibility
        if (
            type(sources) is not tuple
            or not sources
            or len(sources) > 16
            or any(not isinstance(item, SourceRegistration) for item in sources)
        ):
            raise ValueError("sources must be a bounded tuple")
        if (
            type(compatibility) is not tuple
            or len(compatibility) > 64
            or any(not isinstance(item, Compatibility) for item in compatibility)
        ):
            raise ValueError("compatibility must be a bounded tuple")
        if len({item.source_id for item in sources}) != len(sources) or len(
            {(item.station_id, item.scope) for item in sources}
        ) != len(sources):
            raise ValueError("duplicate source registration")
        by_id = {item.source_id: item for item in sources}
        seen = set()
        for link in compatibility:
            if (link.origin_id, link.alternate_id, link.parameter_code) in seen:
                raise ValueError("duplicate compatibility link")
            seen.add((link.origin_id, link.alternate_id, link.parameter_code))
            origin, alternate = by_id.get(link.origin_id), by_id.get(link.alternate_id)
            if (
                origin is None
                or alternate is None
                or origin.scope != "CURRENT_OBSERVATION"
                or alternate.scope != "CURRENT_OBSERVATION"
                or origin.station_id == alternate.station_id
            ):
                raise ValueError("compatibility requires distinct current sources")
            first = next(
                (item for item in origin.series if item.parameter_code == link.parameter_code), None
            )
            second = next(
                (item for item in alternate.series if item.parameter_code == link.parameter_code),
                None,
            )
            if (
                first is None
                or second is None
                or (first.unit, first.statistic_id) != (second.unit, second.statistic_id)
            ):
                raise ValueError("incompatible source series")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "compatibility", compatibility)

    def get(self, source_id: str) -> SourceRegistration:
        for source in self.sources:
            if source.source_id == source_id:
                return source
        raise KeyError(source_id)

    def for_station(self, station_id: str) -> SourceRegistration:
        matches = tuple(
            item
            for item in self.sources
            if item.station_id == station_id and item.scope == "CURRENT_OBSERVATION"
        )
        if len(matches) != 1:
            raise KeyError(station_id)
        return matches[0]

    def alternatives(self, source_id: str, parameter_code: str) -> tuple[SourceRegistration, ...]:
        source = self.get(source_id)
        if type(parameter_code) is not str or not re.fullmatch(r"[0-9]{5}", parameter_code):
            raise ValueError("invalid parameter_code")
        if not any(item.parameter_code == parameter_code for item in source.series):
            raise ValueError("parameter is not configured for source")
        links = [
            item
            for item in self.compatibility
            if item.origin_id == source_id and item.parameter_code == parameter_code
        ]
        return tuple(
            sorted(
                (self.get(item.alternate_id) for item in links), key=lambda item: item.source_id
            )[:3]
        )


def gallinas_registry() -> SourceRegistry:
    return SourceRegistry(
        (
            SourceRegistration(
                "gallinas-usgs-current",
                "USGS-08380500",
                "Gallinas River - USGS 08380500",
                GALLINAS_SERIES,
            ),
        )
    )
