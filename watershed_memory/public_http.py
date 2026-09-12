"""Small in-process HTTP boundary guards for exposed deployments."""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BoundaryConfig:
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    allowed_origins: tuple[str, ...] = ()
    session_create_per_minute: int = 30
    post_per_minute: int = 120

    def __post_init__(self) -> None:
        if type(self.allowed_hosts) is not tuple or type(self.allowed_origins) is not tuple:
            raise ValueError("boundary host and origin lists must be immutable tuples")
        if not self.allowed_hosts:
            raise ValueError("at least one trusted host is required")
        if self.allowed_hosts != ("127.0.0.1", "localhost") and not self.allowed_origins:
            raise ValueError("custom trusted hosts require an HTTPS origin")
        for host in self.allowed_hosts:
            if (type(host) is not str or not host or "*" in host or "://" in host or "/" in host or ":" in host
                    or "@" in host or host != host.lower()
                    or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*", host)):
                raise ValueError("trusted hosts must be exact host names")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (parsed.scheme != "https" or not parsed.netloc or parsed.path != ""
                    or parsed.query or parsed.fragment or parsed.username or parsed.password
                    or parsed.hostname not in self.allowed_hosts or origin != f"https://{parsed.hostname}"):
                raise ValueError("public origins must be exact HTTPS origins")
        if type(self.session_create_per_minute) is not int or not 1 <= self.session_create_per_minute <= 10000:
            raise ValueError("Session-create limit must be an integer from 1 to 10000.")
        if type(self.post_per_minute) is not int or not 1 <= self.post_per_minute <= 10000:
            raise ValueError("POST limit must be an integer from 1 to 10000.")


class BurstLimiter:
    def __init__(self, config: BoundaryConfig, clock: Callable[[], float] = time.monotonic):
        self.config, self.clock, self._lock, self._hits = config, clock, threading.Lock(), deque()

    def allowed(self, kind: str) -> tuple[bool, int]:
        limit = self.config.post_per_minute
        with self._lock:
            now = self.clock()
            while self._hits and now - self._hits[0][0] >= 60:
                self._hits.popleft()
            used = len(self._hits)
            session_used = sum(label == "session" for _, label in self._hits)
            if kind == "session" and session_used >= self.config.session_create_per_minute:
                retry = max(1, math.ceil(60 - (now - min(stamp for stamp, label in self._hits if label == "session"))))
                return False, retry
            if used >= limit:
                retry = max(1, math.ceil(60 - (now - self._hits[0][0])))
                return False, retry
            self._hits.append((now, kind))
            return True, 0


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object, maximum: int = 200) -> bool:
    return type(value) is str and 0 < len(value) <= maximum and all(ord(c) >= 32 for c in value)


def _matches(value: object, pattern: str, maximum: int = 200) -> bool:
    return _text(value, maximum) and re.fullmatch(pattern, value) is not None


def _integer(value: object, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _execution(entry: dict) -> dict:
    """A faulty adapter's metadata cannot become arbitrary browser content."""
    incoming = _mapping(entry.get("input"))
    patterns = {
        "sdk_version": r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.]+)?",
        "instruction_version": r"watershed-review-v[1-9][0-9]*",
        "model_id": r"(?:(?:us|eu|apac|global)\.)?(?:amazon|anthropic|meta|mistral|"
                    r"cohere|ai21|deepseek|qwen|openai)\.[A-Za-z0-9._:-]+",
    }
    inputs = {key: incoming[key] for key, pattern in patterns.items()
              if _matches(incoming.get(key), pattern)}
    if type(incoming.get("scripted_test")) is bool:
        inputs["scripted_test"] = incoming["scripted_test"]
    raw = _mapping(entry.get("output"))
    output = {}
    if _integer(raw.get("model_calls"), 8):
        output["model_calls"] = raw["model_calls"]
    elapsed = raw.get("elapsed_seconds")
    if type(elapsed) in (int, float) and 0 <= elapsed <= 3600 and math.isfinite(elapsed):
        output["elapsed_seconds"] = elapsed
    stop = raw.get("stop_reason")
    if type(stop) is str and stop in {"end_turn", "tool_use", "max_tokens", "stop_sequence",
                                     "content_filtered", "guardrail_intervened"}:
        output["stop_reason"] = stop
    usage = _mapping(raw.get("usage"))
    output["usage"] = {key: usage[key] for key in ("inputTokens", "outputTokens", "totalTokens")
                       if _integer(usage.get(key), 1_000_000_000)}
    cloud = _mapping(raw.get("agentcore"))
    if cloud:
        safe_cloud = {}
        if _matches(cloud.get("endpoint_version_verified_before_call"), r"[1-9][0-9]*", 10):
            safe_cloud["endpoint_version_verified_before_call"] = cloud["endpoint_version_verified_before_call"]
        if type(cloud.get("invocation_attempted")) is bool:
            safe_cloud["invocation_attempted"] = cloud["invocation_attempted"]
        status = cloud.get("stop_status")
        if type(status) is str and status in {"STOP_REQUEST_ACCEPTED", "STOP_FAILED", "NOT_STARTED"}:
            safe_cloud["stop_status"] = status
        output["agentcore"] = safe_cloud
    return {"tool": "strands_turn", "input": inputs, "output": output}


def project_public(value: dict) -> dict:
    """Return the browser contract without modifying canonical snapshots or receipts."""
    fields = ("status", "version", "session_id", "case", "progress", "events", "tasks",
              "responses", "latest_brief", "sources")
    result = {key: deepcopy(value[key]) for key in fields if key in value}
    if "mode" in value:
        mode = _mapping(value["mode"])
        safe_mode = {}
        if _matches(mode.get("id"), r"[a-z][a-z0-9_]*", 64):
            safe_mode["id"] = mode["id"]
        if _text(mode.get("label")):
            safe_mode["label"] = mode["label"]
        if type(mode.get("agent_enabled")) is bool:
            safe_mode["agent_enabled"] = mode["agent_enabled"]
        result["mode"] = safe_mode
    if "trace" in value:
        safe_trace = []
        trace = value["trace"] if isinstance(value["trace"], list) else []
        for entry in trace:
            if not isinstance(entry, dict):
                continue
            tool = entry.get("tool")
            if tool == "strands_turn":
                safe_trace.append(_execution(entry))
            elif type(tool) is str and tool in {"get_case_context", "get_observations",
                                               "propose_review", "commit_case_turn"}:
                safe_trace.append({key: deepcopy(entry[key]) for key in ("tool", "input", "output")
                                   if key in entry})
        result["trace"] = safe_trace
    return result
