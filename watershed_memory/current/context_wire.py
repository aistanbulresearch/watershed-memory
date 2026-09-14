"""Closed, canonical JSON codec for the current v3 context."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from types import UnionType
from typing import NoReturn, get_args, get_origin, get_type_hints

from ..watch.observations import SeriesSpec
from .case_types import ReviewRecord
from .context import CurrentContext
from .context_v3_types import CurrentContextV3, FieldContext
from .context_wire_facts import validate_context_facts
from .fact_types import CoveragePolicy, IntervalFacts, SeriesFact
from .fact_validation import bounded_number, decimal_value, timestamp, utc
from .field_models import (
    FieldEvidenceRecord,
    FieldPlanRecord,
    FieldReportRecord,
    FieldResultSnapshot,
    FieldVerificationRecord,
    FieldWorkSnapshot,
)
from .field_types import FieldPlanSpec
from .locations import LocationEntry
from .registry import Compatibility, SourceRegistration, SourceRegistry
from .source_health import LatestReading, SourceHealth

CONTEXT_MAX_BYTES = 262144
MAX_DEPTH = 24
MAX_NODES = 16384
_ALLOWED = (
    CurrentContextV3, FieldContext, CurrentContext, IntervalFacts, SeriesFact,
    CoveragePolicy, SourceRegistry, SourceRegistration, Compatibility, SeriesSpec,
    SourceHealth, LatestReading, ReviewRecord, LocationEntry, FieldPlanSpec,
    FieldPlanRecord, FieldReportRecord, FieldEvidenceRecord, FieldVerificationRecord,
    FieldResultSnapshot, FieldWorkSnapshot,
)
_ALLOWED_SET = frozenset(_ALLOWED)
_INVALID = (
    ValueError, TypeError, KeyError, AttributeError, OverflowError,
    RecursionError, UnicodeError, InvalidOperation,
)


def _fail() -> NoReturn:
    raise ValueError("invalid current context wire payload") from None


@lru_cache(maxsize=len(_ALLOWED))
def _hints(cls: type) -> dict[str, object]:
    # Only locally admitted classes reach this function. No wire-selected imports.
    if cls not in _ALLOWED_SET:
        _fail()
    namespace = dict(vars(sys.modules[cls.__module__]))
    namespace.update({item.__name__: item for item in _ALLOWED})
    hints = get_type_hints(cls, globalns=namespace, localns=namespace)
    if set(hints) != {field.name for field in fields(cls)}:
        _fail()
    return hints


@dataclass
class _Budget:
    nodes: int = 0
    string_bytes: int = 0

    def check(self, value: object, *, native: bool, depth: int = 1) -> None:
        """Bound the entire input before constructing any domain records."""
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            _fail()
        if type(value) is str:
            self.string_bytes += len(value.encode("utf-8", "strict"))
            if self.string_bytes > CONTEXT_MAX_BYTES:
                _fail()
        if native and type(value) in _ALLOWED_SET:
            children = (getattr(value, field.name) for field in fields(value))
        elif native and type(value) is tuple or not native and type(value) is list:
            children = iter(value)
        elif not native and type(value) is dict:
            children = iter(value.values())
        else:
            children = iter(())
        for child in children:
            self.check(child, native=native, depth=depth + 1)


def _restore(value: object, annotation: object, *, wire: bool) -> object:
    """Rebuild every admitted record, validating both native and wire inputs."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is UnionType:
        choices = tuple(item for item in args if item is not type(None))
        if type(None) not in args or len(choices) != 1:
            _fail()
        if value is None:
            return None
        return _restore(value, choices[0], wire=wire)
    if annotation in (str, bool, int):
        if type(value) is not annotation:
            _fail()
        if annotation is int and not -(2**63) <= value < 2**63:
            _fail()
        return value
    if annotation is Decimal:
        if type(value) is not (str if wire else Decimal):
            _fail()
        return decimal_value(value) if wire else bounded_number(value)
    if annotation is datetime:
        if type(value) is not (str if wire else datetime):
            _fail()
        return timestamp(value) if wire else utc(value)
    if origin is tuple:
        if type(value) is not (list if wire else tuple):
            _fail()
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_restore(item, args[0], wire=wire) for item in value)
        if len(value) != len(args):
            _fail()
        return tuple(
            _restore(item, item_type, wire=wire)
            for item, item_type in zip(value, args, strict=True)
        )
    if annotation not in _ALLOWED_SET:
        _fail()
    hints = _hints(annotation)
    if wire:
        if type(value) is not dict or set(value) != set(hints):
            _fail()
        items = value
    else:
        if type(value) is not annotation:
            _fail()
        items = {name: getattr(value, name) for name in hints}
    restored = {
        name: _restore(items[name], item_type, wire=wire)
        for name, item_type in hints.items()
    }
    return annotation(**restored)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _reject_number(_: str) -> NoReturn:
    # This closed schema has no floating-point JSON numbers.
    _fail()


def _loads(encoded: str) -> object:
    if type(encoded) is not str or len(encoded.encode("utf-8", "strict")) > CONTEXT_MAX_BYTES:
        _fail()
    value = json.loads(
        encoded, object_pairs_hook=_pairs,
        parse_constant=_reject_number, parse_float=_reject_number,
    )
    _Budget().check(value, native=False)
    return value


def _json_default(value: object) -> str:
    if type(value) is Decimal:
        return str(bounded_number(value))
    if type(value) is datetime:
        return utc(value).isoformat()
    _fail()


def _canonical(context: CurrentContextV3) -> str:
    encoded = json.dumps(
        asdict(context), default=_json_default, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )
    if len(encoded.encode("utf-8", "strict")) > CONTEXT_MAX_BYTES:
        _fail()
    return encoded


def encode_context_v3(context: CurrentContextV3) -> str:
    """Validate and encode a complete immutable context without external I/O."""
    try:
        _Budget().check(context, native=True)
        restored = _restore(context, CurrentContextV3, wire=False)
        validate_context_facts(restored)
        return _canonical(restored)
    except _INVALID:
        _fail()


def decode_context_v3(encoded: str) -> CurrentContextV3:
    """Accept only the exact canonical representation of an admitted context."""
    try:
        context = _restore(_loads(encoded), CurrentContextV3, wire=True)
        validate_context_facts(context)
        if _canonical(context) != encoded:
            _fail()
        return context
    except _INVALID:
        _fail()
