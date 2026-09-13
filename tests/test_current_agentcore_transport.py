"""A bounded response reaches explicit EOF before semantic validation."""

import pytest

from watershed_memory.current import agentcore_transport as transport
from watershed_memory.current.remote_protocol import MAX_RESPONSE_BYTES

SESSION = "wm-" + "a" * 32


class StringSubclass(str):
    pass


class Body:
    def __init__(self, chunks=(), *, failure=None, close_failure=False):
        self.chunks = list(chunks)
        self.failure = failure
        self.close_failure = close_failure
        self.reads, self.timeouts, self.closes = [], [], 0

    def read(self, amount):
        self.reads.append(amount)
        if self.failure:
            raise self.failure
        return self.chunks.pop(0) if self.chunks else b""

    def set_socket_timeout(self, seconds):
        self.timeouts.append(seconds)

    def close(self):
        self.closes += 1
        if self.close_failure:
            raise RuntimeError("PRIVATE-CANARY")


def response(body, **changes):
    return {"statusCode": 200, "contentType": "application/json",
        "runtimeSessionId": SESSION, "response": body,
        "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "request-1"},
        "traceId": "trace-1", **changes}


def test_short_reads_are_joined_until_explicit_eof():
    body = Body([b'{"valid":true}', b"suffix", b""])
    raw, request_id, trace_id = transport.read_response_body(response(body), SESSION)
    assert raw == b'{"valid":true}suffix'
    assert (request_id, trace_id) == ("request-1", "trace-1")
    assert body.closes == 1 and len(body.reads) == 3
    assert body.reads == [MAX_RESPONSE_BYTES + 1,
        MAX_RESPONSE_BYTES + 1 - 14, MAX_RESPONSE_BYTES + 1 - 20]
    assert all(0 < t <= 15 for t in body.timeouts)


@pytest.mark.parametrize("count,accepted", [(63, True), (64, False)])
def test_read_limit_includes_the_required_eof(count, accepted):
    body = Body([b"a"] * count)
    if accepted:
        assert transport.read_response_body(response(body), SESSION)[0] == b"a" * count
    else:
        with pytest.raises(ValueError):
            transport.read_response_body(response(body), SESSION)
    assert body.closes == 1 and len(body.reads) == 64


@pytest.mark.parametrize("extra", [b"", b"a"])
def test_exact_byte_limit_still_requires_one_eof_probe(extra):
    body = Body([b"a" * MAX_RESPONSE_BYTES, extra])
    if extra:
        with pytest.raises(ValueError):
            transport.read_response_body(response(body), SESSION)
    else:
        assert len(transport.read_response_body(response(body), SESSION)[0]) == MAX_RESPONSE_BYTES
    assert body.reads == [MAX_RESPONSE_BYTES + 1, 1]
    assert body.closes == 1


@pytest.mark.parametrize("change", [
    {"statusCode": True}, {"statusCode": "200"}, {"statusCode": 500},
    {"runtimeSessionId": SESSION + "b"}, {"runtimeSessionId": None},
    {"runtimeSessionId": StringSubclass(SESSION)},
    {"contentType": "text/event-stream"}, {"contentType": None},
    {"contentType": "application/json; ignored=true"},
    {"contentType": "charset=utf-8;application/json"},
    {"ResponseMetadata": None}, {"ResponseMetadata": {"RequestId": 1}},
    {"ResponseMetadata": {"HTTPStatusCode": 500}},
    {"ResponseMetadata": {"RequestId": "a" * 129}},
    {"traceId": "PRIVATE-CANARY\n"}, {"traceId": "a" * 257},
])
def test_bad_headers_close_without_reading_or_disclosing(change):
    body = Body([b"unused"])
    with pytest.raises(ValueError) as caught:
        transport.read_response_body(response(body, **change), SESSION)
    assert body.closes == 1 and not body.reads
    assert "PRIVATE-CANARY" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_absent_optional_service_ids_and_utf8_content_type():
    body = Body([b"ok"])
    result = response(body, ResponseMetadata={}, contentType="application/json; charset=utf-8")
    del result["traceId"]
    assert transport.read_response_body(result, SESSION) == (b"ok", None, None)


@pytest.mark.parametrize("chunk", ["abc", bytearray(b"a"), None])
def test_non_bytes_chunks_are_refused(chunk):
    body = Body([chunk])
    with pytest.raises(ValueError):
        transport.read_response_body(response(body), SESSION)
    assert body.closes == 1


@pytest.mark.parametrize("method", ["read", "set_socket_timeout"])
def test_injected_stream_must_obey_the_same_interface(method):
    body = Body()
    setattr(body, method, None)
    with pytest.raises(ValueError):
        transport.read_response_body(response(body), SESSION)
    assert body.closes == 1


@pytest.mark.parametrize("phase", ["read", "timeout", "close"])
def test_stream_errors_are_redacted_and_close_once(phase):
    body = Body([b"valid"])
    if phase == "read":
        body.failure = TimeoutError("PRIVATE-CANARY")
    elif phase == "close":
        body.close_failure = True
    else:
        def fail(_):
            raise RuntimeError("PRIVATE-CANARY")
        body.set_socket_timeout = fail
    with pytest.raises(ValueError) as caught:
        transport.read_response_body(response(body), SESSION)
    assert body.closes == 1 and "PRIVATE-CANARY" not in str(caught.value)


def test_all_chunks_share_one_deadline(monkeypatch):
    moments = iter([0, 0, 8, 8, 16])
    monkeypatch.setattr(transport, "monotonic", lambda: next(moments))
    body = Body([b"a", b"b", b""])
    with pytest.raises(ValueError):
        transport.read_response_body(response(body), SESSION)
    assert body.timeouts == [15, 7] and body.closes == 1


def test_deadline_expiry_before_read_is_also_refused(monkeypatch):
    moments = iter([0, 15])
    monkeypatch.setattr(transport, "monotonic", lambda: next(moments))
    body = Body([b"a"])
    with pytest.raises(ValueError):
        transport.read_response_body(response(body), SESSION)
    assert not body.reads and body.closes == 1


def test_non_mapping_response_is_redacted():
    with pytest.raises(ValueError) as caught:
        transport.read_response_body(None, SESSION)
    assert caught.value.__suppress_context__
