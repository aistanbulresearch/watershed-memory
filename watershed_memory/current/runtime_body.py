"""Pre-parser ASGI middleware preserving the exact AgentCore request bytes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from .agentcore_wrapper import MAX_REQUEST_WRAPPER_BYTES, decode_sdk_wrapper

BODY_READ_SECONDS = 10
MAX_BODY_MESSAGES = 1024
_REJECTED = b'{"error":"CURRENT_REQUEST_REJECTED"}'


def _headers(scope: MutableMapping[str, Any]) -> tuple[dict[bytes, list[bytes]], bool]:
    raw = scope.get("headers")
    if type(raw) is not list:
        raise ValueError
    result: dict[bytes, list[bytes]] = {}
    for pair in raw:
        if type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not bytes or type(pair[1]) is not bytes:
            raise ValueError
        result.setdefault(pair[0].lower(), []).append(pair[1])
    return result, True


def _reject_status(headers: dict[bytes, list[bytes]]) -> int:
    if len(headers.get(b"content-encoding", [])) > 0:
        raise PermissionError
    content_type = headers.get(b"content-type", [])
    if len(content_type) != 1:
        raise PermissionError
    try:
        parts = [part.strip().lower() for part in content_type[0].decode("ascii").split(";")]
    except UnicodeError:
        raise PermissionError from None
    if parts not in (["application/json"], ["application/json", "charset=utf-8"]):
        raise PermissionError
    lengths = headers.get(b"content-length", [])
    if len(lengths) > 1:
        raise ValueError
    if lengths:
        text = lengths[0]
        if not text or (len(text) > 1 and text.startswith(b"0")) or not text.isdigit():
            raise ValueError
        length = int(text)
        if length > MAX_REQUEST_WRAPPER_BYTES:
            return 413
    return 0


class CurrentInvocationBodyMiddleware:
    """Validate and replay a bounded invocation body before SDK JSON parsing."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: MutableMapping[str, Any], receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != "/invocations":
            await self.app(scope, receive, send)
            return
        try:
            headers, _ = _headers(scope)
            status = _reject_status(headers)
            if status:
                raise OverflowError
            expected = int(headers[b"content-length"][0]) if headers.get(b"content-length") else None
            started = time.monotonic()
            chunks: list[bytes] = []
            size = 0
            for _ in range(MAX_BODY_MESSAGES):
                remaining = BODY_READ_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(receive(), remaining)
                if type(message) is not dict or type(message.get("type")) is not str:
                    raise ValueError
                if message["type"] == "http.disconnect":
                    raise ValueError
                if message["type"] != "http.request":
                    raise ValueError
                chunk = message.get("body", b"")
                more_body = message.get("more_body", False)
                if type(chunk) is not bytes or type(more_body) is not bool:
                    raise ValueError
                if size + len(chunk) > MAX_REQUEST_WRAPPER_BYTES:
                    raise OverflowError
                chunks.append(chunk)
                size += len(chunk)
                if not more_body:
                    break
            else:
                raise ValueError
            if expected is not None and expected != size:
                raise ValueError
            outer = b"".join(chunks)
            inner = decode_sdk_wrapper(outer)
            scope["watershed.current_request_bytes"] = inner
        except PermissionError:
            await self._error(send, 415)
            return
        except OverflowError:
            await self._error(send, 413)
            return
        except TimeoutError:
            await self._error(send, 408)
            return
        except (ValueError, asyncio.TimeoutError):
            await self._error(send, 400)
            return

        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": outer, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _error(send: Callable[[dict], Awaitable[None]], status: int) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(_REJECTED)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": _REJECTED})
