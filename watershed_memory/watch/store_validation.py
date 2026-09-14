"""Validate attributed source batches before a durable transaction can apply them."""

import re
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .observations import Observation, PageReceipt, SourceBatch, SourceError, bounded_source_text
from .usgs import BASE_URL, USGSClient


def utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("times must be aware datetimes")
    return value.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return utc(value).isoformat()


def decimal_text(value: Decimal) -> str:
    """Expand a previously bounded decimal without ambient-context arithmetic."""
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def validate_batch(batch: SourceBatch, config: Any, lease: Any, now: datetime) -> None:
    if type(batch) is not SourceBatch:
        raise ValueError("expected a source batch")
    for value in (batch.start, batch.end, batch.retrieved_at):
        utc(value)
    if (batch.station_id, batch.start, batch.end) != (config.station_id, lease.start, lease.end):
        raise ValueError("source batch does not match the registered lease scope")
    if not lease.acquired_at <= batch.retrieved_at <= now:
        raise ValueError("source retrieval time is outside the lease")
    if type(batch.observations) is not tuple or len(batch.observations) > 16000:
        raise ValueError("source observation count exceeds the bound")
    if type(batch.pages) is not tuple or not 1 <= len(batch.pages) <= 64:
        raise ValueError("source batch must have bounded page provenance")
    seen_urls: set[str] = set()
    total_bytes = 0
    client = None
    original = ""
    for index, page in enumerate(batch.pages):
        if type(page) is not PageReceipt:
            raise ValueError("invalid page receipt type")
        if (
            page.retrieved_at != batch.retrieved_at
            or type(page.sha256) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", page.sha256)
            or type(page.byte_count) is not int
            or not 1 <= page.byte_count <= 16 * 1024 * 1024
            or type(page.url) is not str
            or len(page.url) > 4096
            or page.url in seen_urls
        ):
            raise ValueError("invalid page provenance")
        total_bytes += page.byte_count
        if total_bytes > 16 * 1024 * 1024:
            raise ValueError("source batch bytes exceed the bound")
        seen_urls.add(page.url)
        try:
            if index == 0:
                parts, base = urlsplit(page.url), urlsplit(BASE_URL)
                query = parse_qs(parts.query, keep_blank_values=True)
                if (parts.scheme, parts.netloc, parts.path) != (
                    base.scheme,
                    base.netloc,
                    base.path,
                ) or parts.fragment:
                    raise ValueError("page is not from the registered source")
                if set(query) != {
                    "f",
                    "monitoring_location_id",
                    "parameter_code",
                    "datetime",
                    "limit",
                }:
                    raise ValueError("first page does not match a complete bounded source request")
                if any(len(v) != 1 for v in query.values()) or not re.fullmatch(
                    r"[1-9][0-9]{0,4}", query["limit"][0]
                ):
                    raise ValueError("invalid source query bounds")
                limit = int(query["limit"][0])
                client = USGSClient(
                    station_id=config.station_id,
                    specs=config.series,
                    page_size=limit,
                    max_pages=len(batch.pages),
                    max_bytes=max(p.byte_count for p in batch.pages if type(p) is PageReceipt),
                )
                original = client._url(lease.start, lease.end)
                if query != parse_qs(urlsplit(original).query):
                    raise ValueError("page source scope differs from the lease")
            else:
                client._next(page.url, original, index)
        except Exception as exc:
            raise ValueError("invalid page source authority") from exc
    specs = {spec.series_id: spec for spec in config.series}
    identities = set()
    previous_order = None
    for observation in batch.observations:
        if type(observation) is not Observation:
            raise ValueError("invalid observation record")
        if not all(
            type(getattr(observation, name)) is str
            for name in (
                "series_id",
                "station_id",
                "parameter_code",
                "unit",
                "approval_status",
                "provider_id",
            )
        ):
            raise ValueError("invalid observation metadata type")
        spec = specs.get(observation.series_id)
        if spec is None or (
            observation.station_id,
            observation.parameter_code,
            observation.unit,
            observation.statistic_id,
        ) != (config.station_id, spec.parameter_code, spec.unit, spec.statistic_id):
            raise ValueError("observation differs from registered source series")
        if not lease.start <= utc(observation.observed_at) <= lease.end:
            raise ValueError("observation falls outside leased window")
        # retrieved_at marks request admission; allow a small publication/clock skew.
        if utc(observation.source_modified_at) > batch.retrieved_at + timedelta(seconds=60):
            raise SourceError(
                "source publication time exceeds the allowed skew", code="DATA_CONFLICT"
            )
        if observation.approval_status not in ("Approved", "Provisional"):
            raise ValueError("unknown observation approval state")
        if observation.qualifier is not None and not bounded_source_text(
            observation.qualifier, allow_empty=True
        ):
            raise ValueError("invalid observation qualifier")
        if not bounded_source_text(observation.provider_id) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]+", observation.provider_id
        ):
            raise ValueError("invalid observation provider identity")
        value = observation.value
        if value is not None and (
            type(value) is not Decimal
            or not value.is_finite()
            or not -32 <= value.as_tuple().exponent <= 12
            or len(value.as_tuple().digits) > 64
        ):
            raise ValueError("invalid or unbounded observation value")
        if observation.identity in identities:
            raise ValueError("batch identities must already be normalized")
        identities.add(observation.identity)
        order = (utc(observation.observed_at), observation.series_id)
        if previous_order is not None and order <= previous_order:
            raise ValueError("batch observations must be in canonical order")
        previous_order = order


def record_dict(record: Any) -> dict[str, Any]:
    return {
        field.name: (iso(value) if isinstance(value, datetime) else value)
        for field in fields(record)
        for value in (getattr(record, field.name),)
    }
