"""Closed canonical JSON codec for v3 delivery executions and failures."""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import datetime
from typing import Callable, TypeVar

from .assessment_types import CurrentAssessment, ReviewDecision, ToolReceipt
from .fact_validation import timestamp
from .field_assessment_types import (
    AgentFieldDecision,
    AgentFieldProposal,
    CurrentAssessmentV3,
    FieldToolReceipt,
)
from .field_delivery_types import CurrentExecutionV3, CurrentFailureV3
from .field_types import FieldPlanSpec

_LIMIT = 524288
_MAX_DEPTH = 16
_MAX_NODES = 4096
_T = TypeVar("_T", CurrentExecutionV3, CurrentFailureV3)


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate v3 delivery key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite v3 delivery value: {value}")


def _default(value: object) -> str:
    if type(value) is datetime:
        return value.isoformat()
    raise ValueError("unsupported v3 delivery value")


def _canonical(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=_default,
        )
        if len(encoded.encode("utf-8")) > _LIMIT:
            raise ValueError("v3 delivery record exceeds its byte bound")
        return encoded
    except (TypeError, OverflowError, RecursionError, UnicodeError) as error:
        raise ValueError("invalid v3 delivery record") from error


def _load(encoded: str) -> dict[str, object]:
    if type(encoded) is not str:
        raise ValueError("v3 delivery record must be a string")
    try:
        payload = encoded.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("invalid v3 delivery UTF-8") from error
    if len(payload) > _LIMIT:
        raise ValueError("v3 delivery record exceeds its byte bound")
    try:
        raw = json.loads(
            encoded,
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("invalid v3 delivery JSON") from error
    if type(raw) is not dict:
        raise ValueError("v3 delivery record must encode an object")

    pending: list[tuple[object, int]] = [(raw, 1)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > _MAX_NODES or depth > _MAX_DEPTH:
            raise ValueError("v3 delivery JSON exceeds structural bounds")
        if type(item) is dict:
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)

    if _canonical(raw).encode("utf-8") != payload:
        raise ValueError("v3 delivery record is not canonical JSON")
    return raw


def _record(raw: object, cls: type) -> dict[str, object]:
    expected = {field.name for field in fields(cls)}
    if type(raw) is not dict or set(raw) != expected:
        raise ValueError(f"invalid {cls.__name__} shape")
    return dict(raw)


def _array(raw: object, name: str) -> list[object]:
    if type(raw) is not list:
        raise ValueError(f"invalid {name} array")
    return raw


def _optional_time(raw: object, name: str) -> datetime | None:
    if raw is None:
        return None
    if type(raw) is not str:
        raise ValueError(f"invalid {name}")
    return timestamp(raw)


def _required_time(raw: object, name: str) -> datetime:
    value = _optional_time(raw, name)
    if value is None:
        raise ValueError(f"invalid {name}")
    return value


def _tool_receipt(raw: object) -> ToolReceipt:
    return ToolReceipt(**_record(raw, ToolReceipt))


def _review_decision(raw: object) -> ReviewDecision:
    values = _record(raw, ReviewDecision)
    values["next_check_at"] = _optional_time(values["next_check_at"], "next_check_at")
    values["reference_ids"] = tuple(_array(values["reference_ids"], "reference_ids"))
    return ReviewDecision(**values)


def _assessment(raw: object) -> CurrentAssessment:
    values = _record(raw, CurrentAssessment)
    values["decisions"] = tuple(
        _review_decision(item) for item in _array(values["decisions"], "decisions")
    )
    values["trace"] = tuple(
        _tool_receipt(item) for item in _array(values["trace"], "trace")
    )
    return CurrentAssessment(**values)


def _plan_spec(raw: object) -> FieldPlanSpec:
    values = _record(raw, FieldPlanSpec)
    values["window_start"] = _required_time(values["window_start"], "window_start")
    values["window_end"] = _required_time(values["window_end"], "window_end")
    values["required_evidence"] = tuple(
        _array(values["required_evidence"], "required_evidence")
    )
    return FieldPlanSpec(**values)


def _proposal(raw: object) -> AgentFieldProposal:
    values = _record(raw, AgentFieldProposal)
    values["spec"] = _plan_spec(values["spec"])
    return AgentFieldProposal(**values)


def _field_decision(raw: object) -> AgentFieldDecision:
    values = _record(raw, AgentFieldDecision)
    if values["proposal"] is not None:
        values["proposal"] = _proposal(values["proposal"])
    return AgentFieldDecision(**values)


def _field_tool_receipt(raw: object) -> FieldToolReceipt:
    return FieldToolReceipt(**_record(raw, FieldToolReceipt))


def _assessment_v3(raw: object) -> CurrentAssessmentV3:
    values = _record(raw, CurrentAssessmentV3)
    values["base"] = _assessment(values["base"])
    values["field"] = _field_decision(values["field"])
    values["field_trace"] = tuple(
        _field_tool_receipt(item)
        for item in _array(values["field_trace"], "field_trace")
    )
    return CurrentAssessmentV3(**values)


def _execution(raw: object) -> CurrentExecutionV3:
    values = _record(raw, CurrentExecutionV3)
    values["assessment"] = _assessment_v3(values["assessment"])
    return CurrentExecutionV3(**values)


def _failure(raw: object) -> CurrentFailureV3:
    values = _record(raw, CurrentFailureV3)
    values["source_trace"] = tuple(
        _tool_receipt(item) for item in _array(values["source_trace"], "source_trace")
    )
    values["field_trace"] = tuple(
        _field_tool_receipt(item)
        for item in _array(values["field_trace"], "field_trace")
    )
    return CurrentFailureV3(**values)


def encode_execution(value: CurrentExecutionV3) -> str:
    if type(value) is not CurrentExecutionV3:
        raise ValueError("invalid v3 execution")
    return _canonical(asdict(value))


def decode_execution(encoded: str) -> CurrentExecutionV3:
    return _decode(encoded, _execution, encode_execution)


def encode_failure(value: CurrentFailureV3) -> str:
    if type(value) is not CurrentFailureV3:
        raise ValueError("invalid v3 failure")
    return _canonical(asdict(value))


def decode_failure(encoded: str) -> CurrentFailureV3:
    return _decode(encoded, _failure, encode_failure)


def _decode(
    encoded: str,
    builder: Callable[[object], _T],
    encoder: Callable[[_T], str],
) -> _T:
    try:
        value = builder(_load(encoded))
        if encoder(value) != encoded:
            raise ValueError("v3 delivery record does not round trip")
        return value
    except ValueError:
        raise
    except (TypeError, KeyError, OverflowError, RecursionError, UnicodeError) as error:
        raise ValueError("invalid v3 delivery record") from error
