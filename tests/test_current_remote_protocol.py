"""Only a canonical response for the exact current request reaches delivery."""

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from test_current_field_dispatch_runner import sdk_field as sdk_field  # noqa: F401
from test_current_field_store import contents
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401
from test_current_remote_types import executed as executed
from test_current_remote_types import failure_for
from test_current_remote_types import reserved as reserved

from watershed_memory.current import remote_protocol as protocol
from watershed_memory.current.context_wire import encode_context_v3
from watershed_memory.current.remote_protocol import (
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    failure_response,
    make_request,
    success_response,
)
from watershed_memory.current.remote_types import CurrentRemoteResponse, result_wire


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def test_request_and_actual_sdk_success_round_trip_without_writes(executed):
    case, _, _, reservation, execution = executed
    before = contents(case[0])
    request = make_request(reservation)
    encoded = encode_request(request)
    assert type(encoded) is bytes
    assert decode_request(encoded) == request
    assert json.loads(encoded)["context"] == json.loads(encode_context_v3(request.context))
    response = success_response(request, execution)
    assert response.request_sha256 == sha256(encoded).hexdigest()
    payload = encode_response(response, request)
    assert decode_response(payload, request) == response
    assert response.execution == execution and response.failure is None
    assert contents(case[0]) == before


def test_typed_failure_round_trip_requires_valid_scope_and_trace(executed):
    case, _, _, reservation, execution = executed
    before = contents(case[0])
    request = make_request(reservation)
    failure = failure_for(reservation, execution)
    response = failure_response(request, failure)
    restored = decode_response(encode_response(response, request), request)
    assert restored.failure == failure and restored.execution is None
    assert contents(case[0]) == before


@pytest.mark.parametrize("path,value", [
    (("identity", "protocol_version"), True),
    (("identity", "context_version"), "3"),
    (("identity", "case_revision"), True),
    (("identity", "source_delivery"), 1),
    (("identity", "reserved_at"), "2026-09-13"),
    (("identity", "context_wire_sha256"), "0" * 64),
    (("identity", "reserved_context_digest"), "0" * 64),
    (("identity", "profile", "sdk_version"), 1),
    (("context", "field_digest"), "0" * 64),
])
def test_request_rejects_wrong_types_and_context_binding(reserved, path, value):
    _, _, _, reservation = reserved
    raw = json.loads(encode_request(make_request(reservation)))
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        decode_request(canonical(raw))


@pytest.mark.parametrize("path,value", [
    (("identity", "attempt_id"), "attempt-" + "f" * 32),
    (("identity", "request_id"), "different-request"),
    (("identity", "source_delivery"), False),
    (("identity", "case_revision"), 999),
    (("identity", "profile", "sdk_version"), "another-sdk"),
    (("request_sha256",), "0" * 64),
    (("result_wire_sha256",), "0" * 64),
    (("result_kind",), "FAILURE"),
    (("execution",), None),
    (("failure",), {}),
])
def test_response_rejects_every_wrong_envelope_before_return(executed, path, value):
    case, _, _, reservation, execution = executed
    request = make_request(reservation)
    raw = json.loads(encode_response(success_response(request, execution), request))
    target = raw
    for key in path[:-1]:
        target = target[key]
    assert target[path[-1]] != value
    target[path[-1]] = value
    before = contents(case[0])
    with pytest.raises(ValueError):
        decode_response(canonical(raw), request)
    assert contents(case[0]) == before


def test_same_context_cannot_reuse_response_from_another_reserved_attempt(executed):
    _, _, _, reservation, execution = executed
    first = make_request(reservation)
    second = make_request(replace(reservation, attempt_id="attempt-" + "f" * 32,
        request_id="new-request", source_delivery=not reservation.source_delivery))
    response = success_response(first, execution)
    with pytest.raises(ValueError):
        encode_response(response, second)
    with pytest.raises(ValueError):
        decode_response(encode_response(response, first), second)


@pytest.mark.parametrize("invalid", [None, {}, "{}", bytearray(b"{}"), b"[]", b"null",
    b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'\xff',
    b'{"x":"PRIVATE-CANARY\\ud800"}', b"[" * 1000 + b"]" * 1000])
def test_malformed_request_has_one_sanitized_failure(invalid):
    with pytest.raises(ValueError) as caught:
        decode_request(invalid)
    assert str(caught.value) == "invalid current remote protocol payload"
    assert caught.value.__suppress_context__


def test_closed_canonical_request_and_response_objects(executed):
    _, _, _, reservation, execution = executed
    request = make_request(reservation)
    response = success_response(request, execution)
    for encoded, decoder in ((encode_request(request), decode_request),
        (encode_response(response, request), lambda raw: decode_response(raw, request))):
        for invalid in (b" " + encoded, encoded + b"\n", encoded.replace(b"+00:00", b"Z")):
            with pytest.raises(ValueError):
                decoder(invalid)
        raw = json.loads(encoded)
        for key in tuple(raw):
            missing = dict(raw)
            del missing[key]
            with pytest.raises(ValueError):
                decoder(canonical(missing))
        raw["prompt"] = "PRIVATE-CANARY"
        with pytest.raises(ValueError):
            decoder(canonical(raw))


def test_protocol_enforces_total_byte_bounds_in_both_directions(executed, monkeypatch):
    _, _, _, reservation, execution = executed
    request = make_request(reservation)
    response = success_response(request, execution)
    request_bytes = encode_request(request)
    response_bytes = encode_response(response, request)
    monkeypatch.setattr(protocol, "MAX_RESPONSE_BYTES", len(response_bytes) - 1)
    with pytest.raises(ValueError):
        encode_response(response, request)
    with pytest.raises(ValueError):
        decode_response(response_bytes, request)
    monkeypatch.setattr(protocol, "MAX_REQUEST_BYTES", len(request_bytes) - 1)
    with pytest.raises(ValueError):
        encode_request(request)
    with pytest.raises(ValueError):
        decode_request(request_bytes)


def test_nested_duplicates_and_unknown_identity_fields_are_refused(executed):
    _, _, _, reservation, execution = executed
    request = make_request(reservation)
    response = success_response(request, execution)
    for encoded, decoder in ((encode_request(request), decode_request),
        (encode_response(response, request), lambda raw: decode_response(raw, request))):
        duplicate = encoded.replace(b'"context_version":3', b'"context_version":3,"context_version":3', 1)
        assert duplicate != encoded
        with pytest.raises(ValueError):
            decoder(duplicate)
        for key in ("identity", "profile"):
            raw = json.loads(encoded)
            target = raw["identity"] if key == "identity" else raw["identity"]["profile"]
            target["prompt"] = "PRIVATE-CANARY"
            with pytest.raises(ValueError):
                decoder(canonical(raw))


def test_native_request_and_response_encoding_revalidate_nested_records(executed):
    _, _, _, reservation, execution = executed
    request = make_request(reservation)
    response = success_response(request, execution)
    forged = deepcopy(request)
    object.__setattr__(forged.identity, "source_delivery", 1)
    with pytest.raises(ValueError):
        encode_request(forged)
    forged_response = deepcopy(response)
    object.__setattr__(forged_response.execution.assessment.field, "disposition", "INVENTED")
    with pytest.raises(ValueError):
        encode_response(forged_response, request)


def test_protocol_counts_actual_json_nodes_and_depth_in_both_directions(executed, monkeypatch):
    from test_current_context_wire import shape

    _, _, _, reservation, execution = executed
    request = make_request(reservation)
    response = success_response(request, execution)
    request_body = encode_request(request)
    response_body = encode_response(response, request)
    for prefix, body, encoder, decoder in (
        ("REQUEST", request_body, lambda: encode_request(request), decode_request),
        ("RESPONSE", response_body, lambda: encode_response(response, request),
            lambda raw: decode_response(raw, request)),
    ):
        nodes, depth = shape(json.loads(body))
        monkeypatch.setattr(protocol, prefix + "_MAX_NODES", nodes)
        monkeypatch.setattr(protocol, prefix + "_MAX_DEPTH", depth)
        assert encoder() == body
        assert decoder(body) == (request if prefix == "REQUEST" else response)
        monkeypatch.setattr(protocol, prefix + "_MAX_NODES", nodes - 1)
        with pytest.raises(ValueError):
            encoder()
        with pytest.raises(ValueError):
            decoder(body)
        monkeypatch.setattr(protocol, prefix + "_MAX_NODES", nodes)
        monkeypatch.setattr(protocol, prefix + "_MAX_DEPTH", depth - 1)
        with pytest.raises(ValueError):
            encoder()
        with pytest.raises(ValueError):
            decoder(body)
        monkeypatch.setattr(protocol, prefix + "_MAX_DEPTH", depth)


@pytest.mark.parametrize("factory", [success_response, failure_response])
def test_response_factory_refuses_the_opposite_result_kind(executed, factory):
    case, _, _, reservation, execution = executed
    before = contents(case[0])
    request = make_request(reservation)
    wrong = failure_for(reservation, execution) if factory is success_response else execution
    with pytest.raises(ValueError, match="^invalid current remote protocol payload$") as caught:
        factory(request, wrong)
    assert caught.value.__suppress_context__
    assert contents(case[0]) == before


@pytest.mark.parametrize("operation", ["encode_request", "success_request", "failure_request",
    "encode_response", "encode_response_request", "decode_response_request"])
@pytest.mark.parametrize("subclass", [False, True])
def test_protocol_requires_exact_native_request_and_response_types(executed, operation, subclass):
    _, _, _, reservation, execution = executed
    request = make_request(reservation)
    response = success_response(request, execution)
    request_type = type("RequestSubclass", (type(request),), {}) if subclass else SimpleNamespace
    response_type = type("ResponseSubclass", (type(response),), {}) if subclass else SimpleNamespace
    request_copy = request_type(identity=request.identity, context=request.context)
    response_copy = response_type(**{
        key: getattr(response, key) for key in response.__dataclass_fields__
    })
    calls = {
        "encode_request": lambda: encode_request(request_copy),
        "success_request": lambda: success_response(request_copy, execution),
        "failure_request": lambda: failure_response(request_copy, failure_for(reservation, execution)),
        "encode_response": lambda: encode_response(response_copy, request),
        "encode_response_request": lambda: encode_response(response, request_copy),
        "decode_response_request": lambda: decode_response(encode_response(response, request), request_copy),
    }
    with pytest.raises(ValueError, match="^invalid current remote protocol payload$") as caught:
        calls[operation]()
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("failed", [False, True])
def test_response_factory_refuses_native_result_subclasses(executed, failed):
    _, _, _, reservation, execution = executed
    value = failure_for(reservation, execution) if failed else execution
    subtype = type("ResultSubclass", (type(value),), {})
    substitute = subtype(**{key: getattr(value, key) for key in value.__dataclass_fields__})
    factory = failure_response if failed else success_response
    with pytest.raises(ValueError, match="^invalid current remote protocol payload$"):
        factory(make_request(reservation), substitute)


def test_response_decoder_refuses_native_substitute_before_reading_attributes(executed):
    _, _, _, reservation, execution = executed
    request = make_request(reservation)
    body = encode_response(success_response(request, execution), request)
    accessed = []

    class Substitute:
        @property
        def identity(self):
            accessed.append("identity")
            return request.identity

    with pytest.raises(ValueError, match="^invalid current remote protocol payload$"):
        decode_response(body, Substitute())
    assert accessed == []


@pytest.mark.parametrize("kind", ["EXECUTION", "FAILURE"])
def test_recomputed_wire_hash_cannot_hide_forged_tool_output(executed, kind):
    case, _, _, reservation, execution = executed
    before = contents(case[0])
    request = make_request(reservation)
    if kind == "EXECUTION":
        trace = execution.assessment.field_trace
        changed = replace(trace[0], output_json="{}")
        forged = replace(execution, assessment=replace(execution.assessment,
            field_trace=(changed, *trace[1:])))
        factory = success_response
    else:
        failure = failure_for(reservation, execution)
        changed = replace(failure.source_trace[0], output_json="{}")
        forged = replace(failure, source_trace=(changed,))
        factory = failure_response
    wire = result_wire(forged)  # The nested record is well-formed, but factually false.
    request_hash = sha256(encode_request(request)).hexdigest()
    result_hash = sha256(wire.encode()).hexdigest()
    native = CurrentRemoteResponse(request.identity, request_hash, kind, result_hash,
        forged if kind == "EXECUTION" else None, forged if kind == "FAILURE" else None)
    raw = {"identity": json.loads(encode_request(request))["identity"],
        "request_sha256": request_hash, "result_kind": kind, "result_wire_sha256": result_hash,
        "execution": json.loads(wire) if kind == "EXECUTION" else None,
        "failure": json.loads(wire) if kind == "FAILURE" else None}
    for invoke in (lambda: factory(request, forged), lambda: encode_response(native, request),
        lambda: decode_response(canonical(raw), request)):
        with pytest.raises(ValueError, match="^invalid current remote protocol payload$") as caught:
            invoke()
        assert caught.value.__suppress_context__
    assert contents(case[0]) == before


def test_malformed_response_has_one_sanitized_failure(executed):
    _, _, _, reservation, _ = executed
    request = make_request(reservation)
    for invalid in (None, {}, "{}", bytearray(b"{}"), b"[]", b"null", b"\xff",
        b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
        b'{"x":"PRIVATE-CANARY\\ud800"}', b"[" * 1000 + b"]" * 1000):
        with pytest.raises(ValueError, match="^invalid current remote protocol payload$") as caught:
            decode_response(invalid, request)
        assert caught.value.__suppress_context__
