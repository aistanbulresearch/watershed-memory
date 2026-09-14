"""Strict bounded adapter for the USGS Water Data OGC API."""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

from .observations import (
    Observation,
    PageReceipt,
    SeriesSpec,
    SourceBatch,
    SourceError,
    bounded_source_text,
)

BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/continuous/items"
DEFAULT_STATION = "USGS-08380500"
GALLINAS_SERIES = (
    SeriesSpec("9857d8ac60994a19bc7a8ec28809066f", "00060", "ft^3/s", "00011"),
    SeriesSpec("9ff19bc69da442b3b96ef5bdff3732e2", "00045", "in", None),
    SeriesSpec("4fb226f4b9aa49c8a04d181db42095d1", "63680", "_FNU", "00011"),
)


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def __post_init__(self):
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("HTTP status must be an integer response code")
        if type(self.body) is not bytes:
            raise TypeError("HTTP body must be bytes")
        if not isinstance(self.headers, Mapping) or any(
            type(k) is not str or type(v) is not str for k, v in self.headers.items()
        ):
            raise TypeError("HTTP headers must map strings to strings")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


def _parse_time(value):
    if type(value) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise SourceError("timestamp must be RFC3339", "SCHEMA")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError("timestamp must be RFC3339", "SCHEMA") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceError("timestamp must include an offset", "SCHEMA")
    return parsed.astimezone(timezone.utc)


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


def _json(body: bytes):
    try:
        return json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=lambda pairs: _object(pairs),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceError("response is not strict JSON", "SCHEMA") from exc


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


class USGSClient:
    def __init__(
        self,
        *,
        station_id=DEFAULT_STATION,
        specs=GALLINAS_SERIES,
        transport: Callable | None = None,
        timeout=15,
        max_pages=16,
        page_size=1000,
        max_bytes=1048576,
    ):
        if (
            type(station_id) is not str
            or not re.fullmatch(r"USGS-[0-9]+", station_id)
            or len(station_id) > 64
        ):
            raise ValueError("invalid station identifier")
        if type(specs) is not tuple or not specs or len(specs) > 16:
            raise ValueError("specs must be a non-empty tuple")
        seen = set()
        seen_parameters = set()
        for spec in specs:
            if not isinstance(spec, SeriesSpec) or spec.series_id in seen:
                raise ValueError("series specifications must be unique")
            seen.add(spec.series_id)
            if (
                not spec.parameter_code
                or spec.parameter_code in seen_parameters
                or not spec.unit
                or spec.statistic_id is not None
                and not spec.statistic_id
            ):
                raise ValueError("invalid series specification")
            seen_parameters.add(spec.parameter_code)
        if (
            type(timeout) not in (int, float)
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ValueError("timeout must be finite and bounded")
        for name, value, cap in (
            ("max_pages", max_pages, 64),
            ("page_size", page_size, 10000),
            ("max_bytes", max_bytes, 16 * 1024 * 1024),
        ):
            if type(value) is not int or not 0 < value <= cap:
                raise ValueError(f"{name} must be a bounded positive integer")
        if max_pages * page_size > 16000 or max_pages * max_bytes > 16 * 1024 * 1024:
            raise ValueError("total pagination work exceeds the hard bound")
        if transport is not None and not callable(transport):
            raise ValueError("transport must be callable")
        self.station_id, self.specs, self.transport = (
            station_id,
            specs,
            transport or self._transport,
        )
        self.timeout, self.max_pages, self.page_size, self.max_bytes = (
            timeout,
            max_pages,
            page_size,
            max_bytes,
        )
        self._by_series = {item.series_id: item for item in specs}

    @staticmethod
    def _transport(url, timeout, max_bytes):
        request = urllib.request.Request(url, headers={"User-Agent": "watershed-memory/1.0"})
        try:

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *_args, **_kwargs):
                    return None

            opener = urllib.request.build_opener(NoRedirect)
            with opener.open(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
                return HTTPResponse(response.status, body, dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            try:
                return HTTPResponse(exc.code, exc.read(max_bytes + 1), dict(exc.headers.items()))
            finally:
                exc.close()
        except Exception as exc:
            raise SourceError("USGS transport failed", "TRANSPORT") from exc

    def _url(self, start, end):
        query = {
            "f": "json",
            "monitoring_location_id": self.station_id,
            "parameter_code": ",".join(sorted(spec.parameter_code for spec in self.specs)),
            "datetime": f"{start.isoformat().replace('+00:00', 'Z')}/{end.isoformat().replace('+00:00', 'Z')}",
            "limit": str(self.page_size),
        }
        url = BASE_URL + "?" + urlencode(query)
        if len(url) > 4096:
            raise ValueError("initial source URL exceeds bound")
        return url

    def _next(self, url: str, original: str, next_page_number: int) -> str:
        if len(url) > 4096:
            raise SourceError("pagination URL exceeds bound", "PAGINATION")
        parsed = urlsplit(url)
        base = urlsplit(BASE_URL)
        if (
            parsed.scheme != "https"
            or parsed.netloc != base.netloc
            or parsed.path != base.path
            or parsed.username
            or parsed.fragment
        ):
            raise SourceError("pagination escaped the fixed USGS endpoint", "PAGINATION")
        query = parse_qs(parsed.query, keep_blank_values=True)
        expected = parse_qs(urlsplit(original).query, keep_blank_values=True)
        extra_keys = set(query) - set(expected)
        if extra_keys not in ({"cursor"}, {"offset"}) or any(len(v) != 1 for v in query.values()):
            raise SourceError("pagination query changed", "PAGINATION")
        for key, value in expected.items():
            if query.get(key) != value:
                raise SourceError("pagination filters changed", "PAGINATION")
        if "cursor" in query:
            if not re.fullmatch(r"[A-Za-z0-9_=-]{1,2048}", query["cursor"][0]):
                raise SourceError("pagination cursor is invalid", "PAGINATION")
        elif query["offset"][0] != str(next_page_number * self.page_size):
            raise SourceError("pagination offset skips or repeats a page", "PAGINATION")
        return url

    def fetch(self, start, end, *, retrieved_at):
        if any(
            item.tzinfo is None or item.utcoffset() is None for item in (start, end, retrieved_at)
        ):
            raise ValueError("fetch timestamps must be timezone-aware")
        start, end, retrieved_at = (
            item.astimezone(timezone.utc) for item in (start, end, retrieved_at)
        )
        if end < start or end - start > timedelta(hours=24) or end > retrieved_at:
            raise ValueError("fetch window is outside bounded limits")
        original = self._url(start, end)
        next_url, pages, observations, seen_urls = original, [], [], set()
        for page_number in range(self.max_pages):
            if next_url in seen_urls:
                raise SourceError("pagination cycle", "PAGINATION")
            seen_urls.add(next_url)
            try:
                response = self.transport(next_url, self.timeout, self.max_bytes)
            except SourceError:
                raise
            except Exception as exc:
                raise SourceError("USGS transport failed", "TRANSPORT") from exc
            if len(response.body) > self.max_bytes:
                raise SourceError("response body exceeds bound", "BOUNDS")
            if response.status != 200:
                retry = None
                if response.status in (429, 503):
                    try:
                        retry = min(3600, max(0, int(response.headers.get("Retry-After", "0"))))
                    except (TypeError, ValueError):
                        retry = None
                raise SourceError(f"USGS returned HTTP {response.status}", "HTTP", retry)
            data = _json(response.body)
            if (
                not isinstance(data, dict)
                or data.get("type") != "FeatureCollection"
                or not isinstance(data.get("features"), list)
                or not isinstance(data.get("links"), list)
            ):
                raise SourceError("response is not a FeatureCollection", "SCHEMA")
            if len(data["features"]) > self.page_size:
                raise SourceError("feature page exceeds bound", "BOUNDS")
            pages.append(
                PageReceipt(
                    next_url,
                    hashlib.sha256(response.body).hexdigest(),
                    len(response.body),
                    retrieved_at,
                )
            )
            observations.extend(self._parse_features(data["features"], start, end))
            if any(not isinstance(link, dict) for link in data["links"]):
                raise SourceError("malformed pagination link", "PAGINATION")
            next_links = [link for link in data["links"] if link.get("rel") == "next"]
            if len(next_links) > 1:
                raise SourceError("multiple next links", "PAGINATION")
            if not next_links:
                break
            if page_number + 1 >= self.max_pages:
                raise SourceError("pagination page cap exhausted", "PAGINATION")
            if type(next_links[0].get("href")) is not str:
                raise SourceError("malformed pagination link", "PAGINATION")
            next_url = self._next(next_links[0]["href"], original, page_number + 1)
        else:
            raise SourceError("pagination page cap exhausted", "PAGINATION")
        unique = {}
        for item in observations:
            prior = unique.get(item.identity)
            if prior is not None and prior.semantic_hash != item.semantic_hash:
                raise SourceError("conflicting observations for identity", "DATA_CONFLICT")
            if prior is None or (item.source_modified_at, item.provider_id) > (
                prior.source_modified_at,
                prior.provider_id,
            ):
                unique[item.identity] = item
        ordered = tuple(
            sorted(unique.values(), key=lambda item: (item.observed_at, item.series_id))
        )
        return SourceBatch(self.station_id, start, end, retrieved_at, ordered, tuple(pages))

    def _parse_features(self, features, start, end):
        parsed = []
        for feature in features:
            if (
                not isinstance(feature, dict)
                or feature.get("type") != "Feature"
                or not isinstance(feature.get("properties"), dict)
            ):
                raise SourceError("invalid feature", "SCHEMA")
            props = feature["properties"]
            required = (
                "time_series_id",
                "monitoring_location_id",
                "parameter_code",
                "statistic_id",
                "time",
                "value",
                "unit_of_measure",
                "approval_status",
                "qualifier",
                "last_modified",
            )
            if any(key not in props for key in required):
                raise SourceError("feature omitted required field", "SCHEMA")
            if (
                type(props["time_series_id"]) is not str
                or type(props["monitoring_location_id"]) is not str
                or type(props["parameter_code"]) is not str
                or type(props["unit_of_measure"]) is not str
                or (props["statistic_id"] is not None and type(props["statistic_id"]) is not str)
            ):
                raise SourceError("invalid source field type", "SCHEMA")
            spec = self._by_series.get(props["time_series_id"])
            if (
                spec is None
                or props["monitoring_location_id"] != self.station_id
                or props["parameter_code"] != spec.parameter_code
                or props["unit_of_measure"] != spec.unit
                or props["statistic_id"] != spec.statistic_id
            ):
                raise SourceError("feature does not match configured series", "SCHEMA")
            observed = _parse_time(props["time"])
            modified = _parse_time(props["last_modified"])
            if (
                not start <= observed <= end
                or props["approval_status"] not in ("Approved", "Provisional")
                or (
                    props["qualifier"] is not None
                    and not bounded_source_text(props["qualifier"], allow_empty=True)
                )
            ):
                raise SourceError("invalid observation metadata", "SCHEMA")
            raw = props["value"]
            if raw is not None and type(raw) is not str:
                raise SourceError("observation value must be a string or null", "SCHEMA")
            value = None
            if raw is not None:
                if len(raw) > 128:
                    raise SourceError("observation value exceeds bound", "BOUNDS")
                if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?)(?:[eE][+-]?[0-9]+)?", raw):
                    raise SourceError("observation value is not an ASCII decimal", "SCHEMA")
                try:
                    value = Decimal(raw)
                except InvalidOperation as exc:
                    raise SourceError("invalid decimal value", "SCHEMA") from exc
                if (
                    not value.is_finite()
                    or value.as_tuple().exponent < -32
                    or value.as_tuple().exponent > 12
                    or len(value.as_tuple().digits) > 64
                ):
                    raise SourceError("decimal value exceeds bound", "BOUNDS")
            provider_id = feature.get("id")
            if not bounded_source_text(provider_id) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]+", provider_id
            ):
                raise SourceError("provider identifier is invalid", "SCHEMA")
            parsed.append(
                Observation(
                    self.station_id,
                    spec.series_id,
                    spec.parameter_code,
                    spec.statistic_id,
                    observed,
                    value,
                    spec.unit,
                    props["approval_status"],
                    props["qualifier"],
                    modified,
                    provider_id,
                )
            )
        return parsed
