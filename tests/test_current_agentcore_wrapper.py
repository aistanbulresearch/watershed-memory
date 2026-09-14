"""The SDK JSON parser must preserve every byte of the reserved request."""

import base64
import json

import pytest
from test_current_field_dispatch_runner import sdk_field as sdk_field  # noqa: F401
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401
from test_current_remote_types import reserved as reserved  # noqa: F401

from watershed_memory.current import agentcore_wrapper as wrapper
from watershed_memory.current.agentcore_wrapper import (
    decode_sdk_payload,
    decode_sdk_wrapper,
    encode_sdk_wrapper,
)
from watershed_memory.current.remote_protocol import encode_request, make_request


@pytest.fixture
def request_bytes(reserved):
    return encode_request(make_request(reserved[3]))


def test_canonical_wrapper_survives_actual_json_parse(request_bytes):
    body = encode_sdk_wrapper(request_bytes)
    assert type(body) is bytes
    assert body == b'"' + base64.b64encode(request_bytes) + b'"'
    assert decode_sdk_wrapper(body) == request_bytes
    assert decode_sdk_payload(json.loads(body)) == request_bytes
    assert wrapper.MAX_REQUEST_WRAPPER_BYTES == 393218


@pytest.mark.parametrize("bad", [None, {}, "{}", bytearray(b"{}"), b"{}", b"[]", b"null",
    b'""', b'"PRIVATE-CANARY\\ud800"', b'"____"', b'"e30="', b'"e30=="', b'" e30="', b"\xff"])
def test_malformed_wrapper_has_one_suppressed_failure(bad):
    with pytest.raises(ValueError, match="^invalid current runtime wrapper$") as caught:
        decode_sdk_wrapper(bad)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("bad", [None, {}, [], b"e30=", 1, True, "", "____", "e30=",
    "e30==", "PRIVATE-CANARY\ud800", pytest.param("A" * 393217, id="oversize")])
def test_sdk_payload_is_an_exact_canonical_string(bad):
    with pytest.raises(ValueError, match="^invalid current runtime wrapper$") as caught:
        decode_sdk_payload(bad)
    assert caught.value.__suppress_context__


def test_outer_whitespace_escape_and_padding_aliases_fail(request_bytes):
    body = encode_sdk_wrapper(request_bytes)
    alias = b'"\\u' + f'{body[1]:04x}'.encode() + body[2:]
    for bad in (b" " + body, body + b"\n", alias, body[:-1] + b'=\"'):
        with pytest.raises(ValueError, match="^invalid current runtime wrapper$"):
            decode_sdk_wrapper(bad)


def test_inner_duplicate_and_noncanonical_bytes_fail_in_both_directions(request_bytes):
    duplicate = request_bytes.replace(b'"context_version":3',
        b'"context_version":3,"context_version":3', 1)
    assert duplicate != request_bytes
    for bad in (duplicate, request_bytes + b"\n", b"{}", request_bytes.decode(), bytearray(request_bytes)):
        with pytest.raises(ValueError, match="^invalid current runtime wrapper$"):
            encode_sdk_wrapper(bad)
        if type(bad) is bytes:
            text = base64.b64encode(bad).decode()
            for invoke in (lambda: decode_sdk_payload(text),
                lambda: decode_sdk_wrapper(json.dumps(text).encode())):
                with pytest.raises(ValueError, match="^invalid current runtime wrapper$"):
                    invoke()


def test_wrapper_byte_bound_applies_to_all_public_paths(request_bytes, monkeypatch):
    body = encode_sdk_wrapper(request_bytes)
    monkeypatch.setattr(wrapper, "MAX_REQUEST_WRAPPER_BYTES", len(body))
    assert encode_sdk_wrapper(request_bytes) == body
    assert decode_sdk_wrapper(body) == request_bytes
    assert decode_sdk_payload(json.loads(body)) == request_bytes
    monkeypatch.setattr(wrapper, "MAX_REQUEST_WRAPPER_BYTES", len(body) - 1)
    for invoke in (lambda: encode_sdk_wrapper(request_bytes), lambda: decode_sdk_wrapper(body),
        lambda: decode_sdk_payload(json.loads(body))):
        with pytest.raises(ValueError, match="^invalid current runtime wrapper$"):
            invoke()
