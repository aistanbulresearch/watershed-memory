"""Real botocore/urllib3 socket lifecycle remains compatible with bounded EOF."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from threading import Thread

import pytest
from botocore.response import StreamingBody
from test_current_agentcore_transport import SESSION, Body, response
from urllib3 import PoolManager, Timeout
from urllib3.response import HTTPResponse

from watershed_memory.current.agentcore_transport import read_response_body
from watershed_memory.current.remote_protocol import MAX_RESPONSE_BYTES


@pytest.mark.parametrize("size", [14, MAX_RESPONSE_BYTES])
def test_real_loopback_content_length_body_reaches_eof_after_socket_release(size, monkeypatch):
    payload = b"a" * size
    calls = []
    original = StreamingBody.set_socket_timeout
    def set_timeout(self, value):
        calls.append(self._raw_stream.closed)
        return original(self, value)
    monkeypatch.setattr(StreamingBody, "set_socket_timeout", set_timeout)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        pool = PoolManager(timeout=Timeout(connect=2, read=2), retries=False)
        try:
            raw = pool.request("GET", f"http://127.0.0.1:{server.server_port}/body",
                preload_content=False)
            assert type(raw) is HTTPResponse
            body = StreamingBody(raw, raw.headers["Content-Length"])
            assert read_response_body(response(body), SESSION)[0] == payload
            assert calls == [False] and raw.closed
            assert body._amount_read == size
        finally:
            pool.clear()
            server.shutdown()
            thread.join(timeout=3)
            assert not thread.is_alive()


def test_exact_closed_native_body_still_reads_and_checks_declared_length(monkeypatch):
    raw = HTTPResponse(body=BytesIO(b""), preload_content=False)
    raw._fp.close()
    assert raw.closed
    body = StreamingBody(raw, "1")
    reads = []
    original = body.read
    def read(amount):
        reads.append(amount)
        return original(amount)
    monkeypatch.setattr(body, "read", read)
    with pytest.raises(ValueError):
        read_response_body(response(body), SESSION)
    assert body._amount_read == 0
    assert reads == [MAX_RESPONSE_BYTES + 1]


@pytest.mark.parametrize("payload", [b"", b"buffered data"])
def test_closed_native_buffers_are_drained_before_explicit_eof(payload):
    raw = HTTPResponse(body=BytesIO(b""), preload_content=False)
    raw._fp.close()
    raw._decoded_buffer.put(payload)
    body = StreamingBody(raw, str(len(payload)))
    assert read_response_body(response(body), SESSION)[0] == payload
    assert body._amount_read == len(payload)


def test_fake_closed_attribute_does_not_bypass_timeout_requirement():
    body = Body([b"data"])
    body.closed = True
    body._raw_stream = type("FakeRaw", (), {"closed": True})()
    def failed(_):
        raise AttributeError("PRIVATE-CANARY")
    body.set_socket_timeout = failed
    with pytest.raises(ValueError):
        read_response_body(response(body), SESSION)
    assert not body.reads and body.closes == 1


def test_streaming_body_subclass_gets_no_native_closed_exemption(monkeypatch):
    class CustomBody(StreamingBody):
        pass
    raw = HTTPResponse(body=BytesIO(b""), preload_content=False)
    raw._fp.close()
    body = CustomBody(raw, "0")
    calls = []
    def failed(_):
        calls.append(True)
        raise AttributeError("PRIVATE-CANARY")
    monkeypatch.setattr(body, "set_socket_timeout", failed)
    with pytest.raises(ValueError):
        read_response_body(response(body), SESSION)
    assert calls == [True]
