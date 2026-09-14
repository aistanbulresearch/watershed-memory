"""Small single-worker public facade for isolated current demonstration cases."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from watershed_memory.api import create_app
from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.demo_seed import CASE_ID, _reject_links, seed
from watershed_memory.current.desk import CurrentDesk
from watershed_memory.current.field_codec import decode
from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.field_types import FieldPrincipal
from watershed_memory.current.http import router as current_router
from watershed_memory.current.locations import LocationEntry, LocationRegistry
from watershed_memory.public_http import BoundaryConfig, BurstLimiter
from watershed_memory.service import Service

COOKIE = "__Host-wm-current"
SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_BODY = 65536
SESSION_TTL = timedelta(hours=24)
SESSION_FILES = {"metadata.json", "approved-site.json", "current-work.sqlite", "replay.sqlite"}


def _json_response(detail: str, status: int, headers: dict[str, str] | None = None):
    response = JSONResponse({"detail": detail}, status_code=status, headers=headers or {})
    response.headers["Cache-Control"] = "no-store"
    return response


class _Sessions:
    def __init__(self, root: Path, hostname: str, maximum: int):
        _reject_links(root)
        self.root, self.hostname, self.maximum = root.absolute(), hostname, maximum
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.apps: OrderedDict[str, object] = OrderedDict()

    def _dirs(self):
        return tuple(item for item in self.root.iterdir() if item.is_dir() and SESSION_RE.fullmatch(item.name))

    def _valid_id(self, value):
        return isinstance(value, str) and SESSION_RE.fullmatch(value) is not None

    def _validated_files(self, path: Path):
        _reject_links(path)
        files = tuple(path.iterdir())
        names = {item.name for item in files}
        allowed = SESSION_FILES | {
            database + suffix for database in ("current-work.sqlite", "replay.sqlite")
            for suffix in ("-wal", "-shm")
        }
        if not SESSION_FILES <= names or not names <= allowed:
            raise ValueError("invalid session files")
        for item in files:
            _reject_links(item)
            if not item.is_file():
                raise ValueError("session child must be a regular file")
        return files

    def _metadata(self, path: Path):
        raw = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        if type(raw) is not dict or set(raw) != {"session_id", "expires_at"} or raw["session_id"] != path.name:
            raise ValueError("invalid session metadata")
        if type(raw["expires_at"]) is not str:
            raise ValueError("invalid session expiry")
        expires = datetime.fromisoformat(raw["expires_at"])
        if expires.tzinfo is None:
            raise ValueError("session expiry must be timezone aware")
        if expires <= datetime.now(timezone.utc):
            return None
        return raw

    def _build(self, sid: str, directory: Path):
        site_raw = (directory / "approved-site.json").read_text(encoding="utf-8").strip()
        site = decode(site_raw, LocationEntry)
        cases = CaseStore(directory / "current-work.sqlite")
        fields = FieldStore(cases, LocationRegistry((site,)))
        principal = FieldPrincipal("demo-operator", ("COORDINATOR", "FIELD_OPERATOR", "VERIFIER"), (CASE_ID,))
        desk = CurrentDesk(
            cases, CASE_ID, fields=fields, principal=principal
        )
        config = BoundaryConfig(allowed_hosts=(self.hostname,), allowed_origins=(f"https://{self.hostname}",))
        inner = create_app(Service(directory / "replay.sqlite"), boundary_config=config, current_desk=None)
        inner.include_router(current_router(desk))
        return inner

    async def get(self, sid: str):
        if not self._valid_id(sid):
            return None
        path = self.root / sid
        try:
            _reject_links(path)
            if not path.is_dir():
                return None
            self._validated_files(path)
            if self._metadata(path) is None:
                return None
            app = self.apps.get(sid)
            if app is None:
                app = self._build(sid, path)
            self.apps.pop(sid, None)
            self.apps[sid] = app
            while len(self.apps) > 32:
                self.apps.popitem(last=False)
            return app
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            raise RuntimeError("damaged session")

    async def create(self):
        async with self.lock:
            directories = self._dirs()
            if len(directories) >= self.maximum:
                for directory in directories:
                    try:
                        files = self._validated_files(directory)
                        metadata = self._metadata(directory)
                        if metadata is None:
                            for item in files:
                                item.unlink()
                            directory.rmdir()
                            self.apps.pop(directory.name, None)
                    except (OSError, ValueError, KeyError, json.JSONDecodeError):
                        continue
                if len(self._dirs()) >= self.maximum:
                    raise OverflowError("session capacity reached")
            for _ in range(8):
                sid = secrets.token_hex(16)
                path = self.root / sid
                try:
                    _reject_links(path)
                    seed(path)
                    Service(path / "replay.sqlite")
                    expires = datetime.now(timezone.utc) + SESSION_TTL
                    (path / "metadata.json").write_text(json.dumps({"session_id": sid, "expires_at": expires.isoformat()}, separators=(",", ":")), encoding="utf-8")
                    return sid
                except FileExistsError:
                    continue
            raise RuntimeError("session allocation unavailable")


class PublicCurrent:
    def __init__(self, data_dir: Path, hostname: str, max_sessions=256, session_create_per_minute=20, post_per_minute=120):
        self.hostname = hostname
        self.post_per_minute = post_per_minute
        self.sessions = _Sessions(data_dir, hostname, max_sessions)
        self.limiter = BurstLimiter(BoundaryConfig(allowed_hosts=(hostname,), allowed_origins=(f"https://{hostname}",), session_create_per_minute=session_create_per_minute, post_per_minute=post_per_minute))
        self.static = StaticFiles(directory=Path(__file__).resolve().parents[1] / "watershed_memory" / "static")
        self.post_limiters: OrderedDict[str, BurstLimiter] = OrderedDict()

    def _cookie(self, scope: Scope):
        values = [value.decode("latin1") for key, value in scope.get("headers", []) if key.lower() == b"cookie"]
        if len(values) != 1:
            return None
        parsed = SimpleCookie()
        try:
            parsed.load(values[0])
        except CookieError:
            return None
        morsel = parsed.get(COOKIE)
        return morsel.value if morsel is not None and SESSION_RE.fullmatch(morsel.value) else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        method, path = scope["method"], scope["path"]
        headers = {}
        for key, value in scope.get("headers", []):
            key = key.decode().lower()
            if key in headers and key in {"host", "origin", "cookie"}:
                return await self._send(_json_response("Malformed request headers.", 400), scope, receive, send)
            headers[key] = value.decode("latin1")
        if headers.get("host") != self.hostname:
            return await self._send(_json_response("Not found.", 404), scope, receive, send)
        if method not in {"GET", "HEAD", "POST"}:
            return await self._send(_json_response("Method not allowed.", 405), scope, receive, send)
        if path == "/health":
            return await self._send(_json_response("ok", 200), scope, receive, send)
        if path == "/":
            return await self._send(RedirectResponse("/current", status_code=307), scope, receive, send)
        if not (path == "/current" or path.startswith("/api/current/") or path.startswith("/static/")):
            return await self._send(_json_response("Not found.", 404), scope, receive, send)
        if path.startswith("/static/"):
            static_scope = dict(scope)
            static_scope["path"] = path[len("/static"):] or "/"
            return await self.static(static_scope, receive, self._security_send(send, path))
        sid = self._cookie(scope)
        app = None
        if method == "POST":
            if headers.get("origin") != f"https://{self.hostname}":
                return await self._send(_json_response("Use this workspace to submit the action.", 403), scope, receive, send)
            if headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return await self._send(_json_response("Send a JSON request.", 415), scope, receive, send)
            if not sid:
                return await self._send(_json_response("A valid demo session is required.", 401), scope, receive, send)
            try:
                app = await self.sessions.get(sid)
            except RuntimeError:
                return await self._send(_json_response("This demo session is temporarily unavailable.", 503), scope, receive, send)
            if app is None:
                return await self._send(_json_response("A valid demo session is required.", 401), scope, receive, send)
            limiter = self.post_limiters.get(sid)
            if limiter is None:
                limiter = BurstLimiter(BoundaryConfig(allowed_hosts=(self.hostname,), allowed_origins=(f"https://{self.hostname}",), post_per_minute=self.post_per_minute))
                self.post_limiters[sid] = limiter
                while len(self.post_limiters) > self.sessions.maximum:
                    self.post_limiters.popitem(last=False)
            self.post_limiters.move_to_end(sid)
            allowed, retry = limiter.allowed("post")
            if not allowed:
                return await self._send(_json_response("Request allowance reached; retry shortly.", 429, {"Retry-After": str(retry)}), scope, receive, send)
            events, received = [], 0
            while True:
                event = await receive()
                received += len(event.get("body", b""))
                if received > MAX_BODY:
                    return await self._send(_json_response("Request body exceeds the 65536 byte limit.", 413), scope, receive, send)
                events.append(event)
                if not event.get("more_body", False):
                    break
            async def replay():
                return events.pop(0) if events else {"type": "http.disconnect"}
            receive = replay
        set_cookie = None
        if path == "/current" and method == "GET" and not sid:
            allowed, retry = self.limiter.allowed("session")
            if not allowed:
                return await self._send(_json_response("Request allowance reached; retry shortly.", 429, {"Retry-After": str(retry)}), scope, receive, send)
            try:
                sid = await self.sessions.create()
            except OverflowError:
                return await self._send(_json_response("Demo session capacity reached.", 503), scope, receive, send)
            set_cookie = f"{COOKIE}={sid}; Path=/; Max-Age=86400; HttpOnly; Secure; SameSite=Strict"
        if not sid:
            return await self._send(_json_response("A valid demo session is required.", 401), scope, receive, send)
        try:
            if app is None:
                app = await self.sessions.get(sid)
        except RuntimeError:
            return await self._send(_json_response("This demo session is temporarily unavailable.", 503), scope, receive, send)
        if app is None:
            return await self._send(_json_response("A valid demo session is required.", 401), scope, receive, send)
        async def guarded_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"), (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")]
                if (path.startswith("/api/") or path == "/current") and not any(key.lower() == b"cache-control" for key, _ in message["headers"]):
                    message["headers"].append((b"cache-control", b"no-store"))
                if set_cookie:
                    message["headers"].append((b"set-cookie", set_cookie.encode()))
            await send(message)
        await app(scope, receive, guarded_send)

    def _security_send(self, send, path):
        async def guarded(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"), (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"), (b"cache-control", b"no-store" if path == "/current" or path.startswith("/api/") else b"public, max-age=3600")]
            await send(message)
        return guarded

    async def _send(self, response, scope, receive, send):
        await response(scope, receive, send)


def create_public_app(data_dir: Path, hostname: str, max_sessions=256, session_create_per_minute=20, post_per_minute=120):
    if not isinstance(data_dir, Path) or type(hostname) is not str or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*", hostname):
        raise ValueError("exact public hostname required")
    if any(type(value) is not int or value < 1 for value in (max_sessions, session_create_per_minute, post_per_minute)):
        raise ValueError("limits must be positive integers")
    return PublicCurrent(data_dir, hostname, max_sessions, session_create_per_minute, post_per_minute)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--port", type=int, default=8782)
    args = parser.parse_args(argv)
    import uvicorn
    uvicorn.run(create_public_app(args.data_dir, args.hostname), host="127.0.0.1", port=args.port, workers=1, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
