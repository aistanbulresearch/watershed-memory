"""Closed, canonical field-record codec; decimals and timestamps retain exact values."""

import json
from dataclasses import asdict, fields
from datetime import datetime
from decimal import Decimal

from .fact_validation import decimal_value, timestamp
from .field_models import (
    FieldEvidenceRecord,
    FieldMutationReceipt,
    FieldPlanRecord,
    FieldReportRecord,
    FieldResultSnapshot,
    FieldVerificationRecord,
    FieldWorkSnapshot,
)
from .field_types import FieldPlanSpec
from .locations import LocationEntry

_CLASSES = {
    LocationEntry,
    FieldPlanSpec,
    FieldPlanRecord,
    FieldReportRecord,
    FieldEvidenceRecord,
    FieldVerificationRecord,
    FieldResultSnapshot,
    FieldWorkSnapshot,
    FieldMutationReceipt,
}
_NESTED = {
    "location": LocationEntry,
    "spec": FieldPlanSpec,
    "plan": FieldPlanRecord,
    "report": FieldReportRecord,
    "result": FieldResultSnapshot,
    "verification": FieldVerificationRecord,
}
_TIMES = {
    "window_start",
    "window_end",
    "performed_start",
    "performed_end",
    "recorded_at",
    "approved_at",
    "created_at",
    "updated_at",
    "deferred_until",
    "observed_at",
    "attached_at",
    "verified_at",
}


def encode(value: object, *, limit: int = 131072) -> str:
    def convert(item):
        if type(item) is datetime:
            return item.isoformat()
        if type(item) is Decimal:
            return str(item)
        raise ValueError("unsupported field record value")

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=convert,
    )
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError("field record exceeds its byte bound")
    return encoded


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate field record key")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("nonfinite field record value")


def _record(raw, cls):
    if cls not in _CLASSES or type(raw) is not dict or set(raw) != {f.name for f in fields(cls)}:
        raise ValueError("invalid field record shape")
    values = dict(raw)
    for name, value in values.items():
        if name == "evidence":
            if cls is FieldMutationReceipt:
                values[name] = None if value is None else _record(value, FieldEvidenceRecord)
            else:
                if type(value) is not list or len(value) > 8:
                    raise ValueError("invalid field evidence array")
                values[name] = tuple(_record(item, FieldEvidenceRecord) for item in value)
        elif name in _NESTED and value is not None:
            values[name] = _record(value, _NESTED[name])
        elif name in _TIMES and value is not None:
            values[name] = timestamp(value)
        elif name in ("activities", "required_evidence", "evidence_ids"):
            if type(value) is not list or len(value) > 8:
                raise ValueError("invalid field record array")
            values[name] = tuple(value)
        elif name in ("latitude", "longitude"):
            values[name] = decimal_value(value)
    return cls(**values)


def decode(encoded: str, cls):
    if type(encoded) is not str or len(encoded.encode("utf-8")) > 131072:
        raise ValueError("field record exceeds its byte bound")
    try:
        raw = json.loads(encoded, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        record = _record(raw, cls)
        if encode(asdict(record)) != encoded:
            raise ValueError("field record is not canonical")
        return record
    except (TypeError, OverflowError, RecursionError, KeyError) as error:
        raise ValueError("invalid field record") from error
