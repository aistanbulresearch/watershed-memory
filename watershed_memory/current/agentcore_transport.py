"""Bounded, redacted response-body handling for an AgentCore invocation."""

from __future__ import annotations

from time import monotonic
from typing import NoReturn

from botocore.response import StreamingBody
from urllib3.response import HTTPResponse

from .remote_protocol import MAX_RESPONSE_BYTES

BODY_READ_SECONDS = 15
MAX_READS = 64
_ERROR = "invalid current AgentCore response"


def _fail() -> NoReturn:
    raise ValueError(_ERROR) from None


def _native_closed(stream: object) -> bool:
    # Botocore loses its socket after urllib3 consumes a complete response.
    # Exact closed native responses can only drain memory or return EOF.
    if type(stream) is not StreamingBody:
        return False
    raw = getattr(stream, "_raw_stream", None)
    return type(raw) is HTTPResponse and raw.closed is True


def _service_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or not value.isprintable()
        or value != value.strip()
    ):
        _fail()
    return value


def _content_type(value: object) -> None:
    if type(value) is not str or not value or len(value) > 128 or not value.isprintable():
        _fail()
    parts = [part.strip().lower() for part in value.split(";")]
    if (
        parts[0] != "application/json"
        or parts[1:] not in ([], ["charset=utf-8"])
    ):
        _fail()


def _response_metadata(result: dict) -> tuple[str | None, str | None]:
    metadata = result.get("ResponseMetadata")
    if "ResponseMetadata" in result and type(metadata) is not dict:
        _fail()
    if metadata is not None and "HTTPStatusCode" in metadata and (
        type(metadata["HTTPStatusCode"]) is not int or metadata["HTTPStatusCode"] != 200
    ):
        _fail()
    request_id = _service_text(metadata.get("RequestId") if metadata else None, 128)
    trace_id = _service_text(result.get("traceId"), 256)
    return request_id, trace_id


def read_response_body(
    result: object, runtime_session_id: str,
) -> tuple[bytes, str | None, str | None]:
    """Read one response to EOF, closing its stream before returning bytes."""
    if type(result) is not dict or type(runtime_session_id) is not str:
        _fail()
    stream = result.get("response")
    try:
        if (
            type(result.get("statusCode")) is not int
            or result["statusCode"] != 200
            or type(result.get("runtimeSessionId")) is not str
            or result.get("runtimeSessionId") != runtime_session_id
        ):
            _fail()
        _content_type(result.get("contentType"))
        request_id, trace_id = _response_metadata(result)
        if (
            stream is None
            or not callable(getattr(stream, "read", None))
            or not callable(getattr(stream, "set_socket_timeout", None))
            or not callable(getattr(stream, "close", None))
        ):
            _fail()
        started = monotonic()
        chunks: list[bytes] = []
        total = 0
        for _ in range(MAX_READS):
            elapsed = monotonic() - started
            remaining_time = BODY_READ_SECONDS - elapsed
            if remaining_time <= 0:
                _fail()
            amount = MAX_RESPONSE_BYTES + 1 - total
            if amount <= 0:
                _fail()
            if not _native_closed(stream):
                stream.set_socket_timeout(remaining_time)
            chunk = stream.read(amount)
            if type(chunk) is not bytes:
                _fail()
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                _fail()
            if chunk:
                chunks.append(chunk)
            if monotonic() - started >= BODY_READ_SECONDS:
                _fail()
            if not chunk:
                return b"".join(chunks), request_id, trace_id
        _fail()
    except Exception:
        _fail()
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                _fail()
