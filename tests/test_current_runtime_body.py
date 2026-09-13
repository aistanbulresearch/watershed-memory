"""Reject malformed/oversized requests before the AgentCore SDK parses JSON."""

import asyncio
import json

import pytest
from test_current_agentcore_wrapper import request_bytes as request_bytes  # noqa: F401
from test_current_field_dispatch_runner import sdk_field as sdk_field  # noqa: F401
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401
from test_current_remote_types import reserved as reserved  # noqa: F401

from watershed_memory.current import runtime_body
from watershed_memory.current.agentcore_wrapper import encode_sdk_wrapper
from watershed_memory.current.runtime_body import CurrentInvocationBodyMiddleware

JSON_HEADERS = [(b"content-type", b"application/json")]


def invoke(messages, *, headers=None, path="/invocations", method="POST", pause=False,
           downstream_error=False, existing=None):
    sent, calls = [], []
    pending = list(messages)
    scope = {"type": "http", "path": path, "method": method,
        "headers": JSON_HEADERS if headers is None else headers,
        "watershed.current_request_bytes": existing}

    async def receive():
        if pause:
            await asyncio.Event().wait()
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def downstream(received_scope, received, send):
        calls.append((dict(received_scope), await received()))
        if downstream_error:
            raise RuntimeError("DOWNSTREAM-CANARY")
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    asyncio.run(CurrentInvocationBodyMiddleware(downstream)(scope, receive, send))
    return sent, calls


def chunk(body, more=False):
    return {"type": "http.request", "body": body, "more_body": more}


def refused(sent, calls, status):
    assert calls == []
    assert sent[0]["status"] == status
    body = b"".join(m.get("body", b"") for m in sent[1:])
    assert json.loads(body) == {"error": "CURRENT_REQUEST_REJECTED"}
    assert len(body) < 100


@pytest.mark.parametrize("length", [False, True])
def test_valid_chunks_replay_exact_outer_and_bind_inner_scope(request_bytes, length):
    body = encode_sdk_wrapper(request_bytes)
    headers = JSON_HEADERS + ([(b"content-length", str(len(body)).encode())] if length else [])
    sent, calls = invoke([chunk(body[:13], True), chunk(body[13:])], headers=headers,
        existing=b"UNTRUSTED-OLD-SCOPE")
    assert sent[0]["status"] == 204 and len(calls) == 1
    scope, replay = calls[0]
    assert scope["watershed.current_request_bytes"] == request_bytes
    assert replay == chunk(body)


@pytest.mark.parametrize("length", [b"-1", b"+1", b"01", b" 1", b"1 ", b"1,1", b"", b"99999999999"])
def test_invalid_or_excess_length_refused_before_body_read(length):
    sent, calls = invoke([], headers=JSON_HEADERS + [(b"content-length", length)])
    refused(sent, calls, 413 if length == b"99999999999" else 400)


@pytest.mark.parametrize("delta", [-1, 1])
def test_lying_length_refused_after_actual_body(request_bytes, delta):
    body = encode_sdk_wrapper(request_bytes)
    sent, calls = invoke([chunk(body)],
        headers=JSON_HEADERS + [(b"content-length", str(len(body) + delta).encode())])
    refused(sent, calls, 400)


def test_duplicate_length_refused_even_when_equal(request_bytes):
    body = encode_sdk_wrapper(request_bytes)
    header = (b"content-length", str(len(body)).encode())
    refused(*invoke([chunk(body)], headers=JSON_HEADERS + [header, header]), 400)


@pytest.mark.parametrize("headers", [[], [(b"content-type", b"text/plain")],
    JSON_HEADERS * 2, JSON_HEADERS + [(b"content-encoding", b"gzip")],
    [(b"content-type", b"application/json; charset=latin1")]])
def test_unsupported_media_never_reaches_parser(headers):
    refused(*invoke([], headers=headers), 415)


def test_utf8_media_type_is_accepted(request_bytes):
    body = encode_sdk_wrapper(request_bytes)
    sent, calls = invoke([chunk(body)], headers=[(b"content-type", b"application/json; charset=utf-8")])
    assert sent[0]["status"] == 204 and len(calls) == 1


@pytest.mark.parametrize("empty_chunk", [False, True])
def test_asgi_optional_body_and_more_body_defaults_preserve_valid_request(request_bytes, empty_chunk):
    body = encode_sdk_wrapper(request_bytes)
    messages = [{"type": "http.request", "body": body}]
    if empty_chunk:
        messages.insert(0, {"type": "http.request", "more_body": True})
    sent, calls = invoke(messages)
    assert sent[0]["status"] == 204 and len(calls) == 1
    assert calls[0][0]["watershed.current_request_bytes"] == request_bytes


@pytest.mark.parametrize("media", [b"application/json; application/json",
    b"application/json; charset=utf-8; charset=utf-8", b"charset=utf-8; application/json"])
def test_duplicate_or_invented_media_parameters_are_refused(media):
    refused(*invoke([], headers=[(b"content-type", media)]), 415)


@pytest.mark.parametrize("message", [{"type": "http.disconnect"}, {"type": "other"},
    {"type": "http.request", "body": "PRIVATE-CANARY"},
    {"type": "http.request", "body": b"", "more_body": 1}])
def test_malformed_or_disconnected_body_has_constant_error(message):
    refused(*invoke([message]), 400)


def test_total_bytes_bound_across_chunks_with_absent_or_lying_length(request_bytes, monkeypatch):
    body = encode_sdk_wrapper(request_bytes)
    monkeypatch.setattr(runtime_body, "MAX_REQUEST_WRAPPER_BYTES", len(body) - 1)
    for headers in (JSON_HEADERS, JSON_HEADERS + [(b"content-length", b"1")]):
        refused(*invoke([chunk(body[:10], True), chunk(body[10:])], headers=headers), 413)


def test_empty_chunk_flood_is_bounded(monkeypatch):
    monkeypatch.setattr(runtime_body, "MAX_BODY_MESSAGES", 2)
    refused(*invoke([chunk(b"", True)] * 3), 400)


def test_entire_body_has_a_wall_clock_deadline(monkeypatch):
    monkeypatch.setattr(runtime_body, "BODY_READ_SECONDS", 0.005)
    refused(*invoke([], pause=True), 408)


@pytest.mark.parametrize("body", [b"{}", b'"e30="', b'"PRIVATE-CANARY\\ud800"', b"\xff"])
def test_bad_wrapper_refused_before_sdk(body):
    refused(*invoke([chunk(body)]), 400)


def test_ping_passes_through_without_invocation_policy():
    sent, calls = invoke([], headers=[], path="/ping", method="GET")
    assert sent[0]["status"] == 204 and len(calls) == 1


def test_downstream_exception_is_not_relabelled_as_invalid_request(request_bytes):
    with pytest.raises(RuntimeError, match="DOWNSTREAM-CANARY"):
        invoke([chunk(encode_sdk_wrapper(request_bytes))], downstream_error=True)
