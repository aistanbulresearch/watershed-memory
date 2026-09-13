"""Canonical base64 envelope used by the AgentCore JSON parser."""

from __future__ import annotations

import base64
import binascii
import json
from typing import NoReturn

from .remote_protocol import MAX_REQUEST_BYTES, decode_request

MAX_REQUEST_WRAPPER_BYTES = 393218
_ERROR = "invalid current runtime wrapper"


def _fail() -> NoReturn:
    raise ValueError(_ERROR) from None


def _decode_text(payload: object) -> bytes:
    if type(payload) is not str:
        _fail()
    try:
        if len(payload) + 2 > MAX_REQUEST_WRAPPER_BYTES:
            _fail()
        if any(ord(char) > 127 for char in payload):
            _fail()
        decoded = base64.b64decode(payload.encode("ascii"), validate=True)
        if base64.b64encode(decoded).decode("ascii") != payload:
            _fail()
        if len(decoded) > MAX_REQUEST_BYTES:
            _fail()
        decode_request(decoded)
        return decoded
    except (binascii.Error, UnicodeError, ValueError, TypeError, OverflowError):
        _fail()


def encode_sdk_wrapper(request_bytes: bytes) -> bytes:
    try:
        if type(request_bytes) is not bytes:
            _fail()
        decoded = decode_request(request_bytes)
        del decoded
        encoded_text = base64.b64encode(request_bytes).decode("ascii")
        body = json.dumps(encoded_text, ensure_ascii=False, separators=(",", ":")).encode("ascii")
        if len(body) > MAX_REQUEST_WRAPPER_BYTES:
            _fail()
        return body
    except Exception:
        _fail()


def decode_sdk_wrapper(body: bytes) -> bytes:
    try:
        if type(body) is not bytes or len(body) > MAX_REQUEST_WRAPPER_BYTES:
            _fail()
        parsed = json.loads(body.decode("utf-8"))
        if json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8") != body:
            _fail()
        return _decode_text(parsed)
    except Exception:
        _fail()


def decode_sdk_payload(payload: object) -> bytes:
    try:
        return _decode_text(payload)
    except Exception:
        _fail()
