"""Strict canonical byte protocol for remote current-case inference."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from typing import NoReturn

from .context_wire import decode_context_v3, encode_context_v3
from .delivery_types import InvocationProfile
from .field_delivery_codec import decode_execution, decode_failure
from .field_delivery_types import CurrentExecutionV3, CurrentFailureV3, FieldDeliveryReservation
from .remote_types import (
    CurrentRemoteIdentity,
    CurrentRemoteRequest,
    CurrentRemoteResponse,
    identity_from,
    result_wire,
    validate_result,
)

MAX_REQUEST_BYTES = 294912
MAX_RESPONSE_BYTES = 557056
REQUEST_MAX_DEPTH = 26
REQUEST_MAX_NODES = 16448
RESPONSE_MAX_DEPTH = 18
RESPONSE_MAX_NODES = 4160
_ERROR = "invalid current remote protocol payload"


def _fail() -> NoReturn:
    raise ValueError(_ERROR) from None


def _default(value: object) -> str:
    if type(value) is datetime:
        return value.isoformat()
    _fail()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False, default=_default).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        _fail()


def _bounded(raw: bytes, maximum: int, depth: int, nodes: int) -> object:
    if type(raw) is not bytes or len(raw) > maximum:
        _fail()
    count = 0
    def hook(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                _fail()
            seen[key] = value
        return seen
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=hook,
                           parse_constant=lambda _: _fail())
        pending = [(value, 1)]
        while pending:
            item, level = pending.pop()
            count += 1
            if count > nodes or level > depth:
                _fail()
            if type(item) is dict:
                pending.extend((child, level + 1) for child in item.values())
            elif type(item) is list:
                pending.extend((child, level + 1) for child in item)
        if _canonical(value) != raw:
            _fail()
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError):
        _fail()


def _identity(value: CurrentRemoteIdentity) -> dict:
    if type(value) is not CurrentRemoteIdentity:
        _fail()
    return {"protocol_version": value.protocol_version, "context_version": value.context_version,
            "attempt_id": value.attempt_id, "request_id": value.request_id, "case_id": value.case_id,
            "case_revision": value.case_revision, "event_id": value.event_id,
            "policy_digest": value.policy_digest, "source_delivery": value.source_delivery,
            "reserved_at": value.reserved_at.isoformat(), "profile": asdict(value.profile),
            "reserved_context_digest": value.reserved_context_digest,
            "context_wire_sha256": value.context_wire_sha256}


def _identity_from(raw: object) -> CurrentRemoteIdentity:
    if type(raw) is not dict:
        _fail()
    try:
        data = dict(raw)
        data["reserved_at"] = datetime.fromisoformat(data["reserved_at"])
        data["profile"] = InvocationProfile(**data["profile"])
        return CurrentRemoteIdentity(**data)
    except (KeyError, TypeError, ValueError, OverflowError):
        _fail()


def make_request(reservation: FieldDeliveryReservation) -> CurrentRemoteRequest:
    try:
        return CurrentRemoteRequest(identity_from(reservation), reservation.context)
    except Exception:
        _fail()


def encode_request(value: CurrentRemoteRequest) -> bytes:
    try:
        if type(value) is not CurrentRemoteRequest:
            _fail()
        request = CurrentRemoteRequest(value.identity, value.context)
        payload = {"identity": _identity(request.identity),
                   "context": json.loads(encode_context_v3(request.context))}
        encoded = _canonical(payload)
        _bounded(encoded, MAX_REQUEST_BYTES, REQUEST_MAX_DEPTH, REQUEST_MAX_NODES)
        return encoded
    except Exception:
        _fail()


def decode_request(body: bytes) -> CurrentRemoteRequest:
    try:
        raw = _bounded(body, MAX_REQUEST_BYTES, REQUEST_MAX_DEPTH, REQUEST_MAX_NODES)
        if type(raw) is not dict or set(raw) != {"identity", "context"}:
            _fail()
        identity = _identity_from(raw["identity"])
        context = decode_context_v3(_canonical(raw["context"]).decode("utf-8"))
        request = CurrentRemoteRequest(identity, context)
        if encode_request(request) != body:
            _fail()
        return request
    except Exception:
        _fail()


def _response(
    request: CurrentRemoteRequest, value: CurrentExecutionV3 | CurrentFailureV3,
) -> CurrentRemoteResponse:
    if type(request) is not CurrentRemoteRequest:
        _fail()
    if type(value) not in (CurrentExecutionV3, CurrentFailureV3):
        _fail()
    checked = validate_result(request.identity, request.context, value)
    kind = "EXECUTION" if type(checked) is CurrentExecutionV3 else "FAILURE"
    encoded = result_wire(checked)
    return CurrentRemoteResponse(request.identity, sha256(encode_request(request)).hexdigest(), kind,
                                 sha256(encoded.encode()).hexdigest(),
                                 checked if kind == "EXECUTION" else None,
                                 checked if kind == "FAILURE" else None)


def success_response(request: CurrentRemoteRequest, execution: CurrentExecutionV3) -> CurrentRemoteResponse:
    try:
        if type(request) is not CurrentRemoteRequest or type(execution) is not CurrentExecutionV3:
            _fail()
        return _response(request, execution)
    except Exception:
        _fail()


def failure_response(request: CurrentRemoteRequest, failure: CurrentFailureV3) -> CurrentRemoteResponse:
    try:
        if type(request) is not CurrentRemoteRequest or type(failure) is not CurrentFailureV3:
            _fail()
        return _response(request, failure)
    except Exception:
        _fail()


def encode_response(value: CurrentRemoteResponse, request: CurrentRemoteRequest) -> bytes:
    try:
        if type(value) is not CurrentRemoteResponse or type(request) is not CurrentRemoteRequest:
            _fail()
        checked = CurrentRemoteResponse(value.identity, value.request_sha256, value.result_kind,
                                        value.result_wire_sha256, value.execution, value.failure)
        if checked.identity != request.identity or checked.request_sha256 != sha256(encode_request(request)).hexdigest():
            _fail()
        result = checked.execution if checked.result_kind == "EXECUTION" else checked.failure
        checked_result = validate_result(request.identity, request.context, result)
        kind = "EXECUTION" if type(checked_result) is CurrentExecutionV3 else "FAILURE"
        wire = result_wire(checked_result)
        payload = {"identity": _identity(checked.identity), "request_sha256": checked.request_sha256,
                   "result_kind": kind, "result_wire_sha256": checked.result_wire_sha256,
                   "execution": json.loads(wire) if kind == "EXECUTION" else None,
                   "failure": json.loads(wire) if kind == "FAILURE" else None}
        encoded = _canonical(payload)
        _bounded(encoded, MAX_RESPONSE_BYTES, RESPONSE_MAX_DEPTH, RESPONSE_MAX_NODES)
        return encoded
    except Exception:
        _fail()


def decode_response(body: bytes, request: CurrentRemoteRequest) -> CurrentRemoteResponse:
    try:
        if type(request) is not CurrentRemoteRequest:
            _fail()
        raw = _bounded(body, MAX_RESPONSE_BYTES, RESPONSE_MAX_DEPTH, RESPONSE_MAX_NODES)
        keys = {"identity", "request_sha256", "result_kind", "result_wire_sha256", "execution", "failure"}
        if type(raw) is not dict or set(raw) != keys:
            _fail()
        identity = _identity_from(raw["identity"])
        if identity != request.identity or type(raw["request_sha256"]) is not str:
            _fail()
        if raw["result_kind"] == "EXECUTION" and raw["execution"] is not None and raw["failure"] is None:
            execution = decode_execution(_canonical(raw["execution"]).decode())
            response = CurrentRemoteResponse(identity, raw["request_sha256"], "EXECUTION", raw["result_wire_sha256"], execution, None)
        elif raw["result_kind"] == "FAILURE" and raw["failure"] is not None and raw["execution"] is None:
            failure = decode_failure(_canonical(raw["failure"]).decode())
            response = CurrentRemoteResponse(identity, raw["request_sha256"], "FAILURE", raw["result_wire_sha256"], None, failure)
        else:
            _fail()
        if response.request_sha256 != sha256(encode_request(request)).hexdigest():
            _fail()
        validate_result(request.identity, request.context, response.execution or response.failure)
        if encode_response(response, request) != body:
            _fail()
        return response
    except Exception:
        _fail()
