"""Bounded acquisition runner; it never invokes a model or retries silently."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Callable

from .observations import SourceError
from .store import LeaseLost, PollResult, WatchStore


@dataclass(frozen=True)
class TickResult:
    outcome: str
    poll_result: PollResult | None
    error_code: str | None
    next_poll_at: str | None


class WatchRunner:
    def __init__(self, store: WatchStore, source, *, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.source = source
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def tick(self, monitor_id: str) -> TickResult:
        acquired_at = self.clock()
        lease = self.store.acquire_poll(monitor_id, now=acquired_at)
        if lease is None:
            status = self.store.status(monitor_id)
            next_poll = self._timestamp(status.get("next_poll_at"), "next_poll_at")
            lease_expires = status.get("poll_lease_expires_at")
            wake_at = max(next_poll, acquired_at + timedelta(seconds=1))
            if lease_expires is not None:
                wake_at = max(wake_at, self._timestamp(lease_expires, "poll_lease_expires_at"))
            return TickResult("NOT_DUE", None, None, wake_at.isoformat())
        try:
            batch = self.source.fetch(lease.start, lease.end, retrieved_at=acquired_at)
        except SourceError as error:
            completed_at = self.clock()
            try:
                self.store.fail_poll(lease, error, now=completed_at)
            except LeaseLost:
                return TickResult(
                    "LEASE_LOST", None, "LEASE_LOST", self.store.status(monitor_id)["next_poll_at"]
                )
            return TickResult(
                "SOURCE_BACKOFF", None, error.code, self.store.status(monitor_id)["next_poll_at"]
            )
        completed_at = self.clock()
        try:
            result = self.store.commit_poll(lease, batch, now=completed_at)
        except LeaseLost:
            return TickResult(
                "LEASE_LOST", None, "LEASE_LOST", self.store.status(monitor_id)["next_poll_at"]
            )
        except SourceError as error:
            try:
                self.store.fail_poll(lease, error, now=self.clock())
            except LeaseLost:
                return TickResult(
                    "LEASE_LOST", None, "LEASE_LOST", self.store.status(monitor_id)["next_poll_at"]
                )
            return TickResult(
                "SOURCE_BACKOFF", None, error.code, self.store.status(monitor_id)["next_poll_at"]
            )
        return TickResult(
            result.outcome, result, None, self.store.status(monitor_id)["next_poll_at"]
        )

    @staticmethod
    def _timestamp(value, name: str) -> datetime:
        if type(value) is not str:
            raise ValueError(f"missing persisted {name}")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"invalid persisted {name}") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"persisted {name} must be timezone-aware")
        return parsed

    def run(
        self,
        monitor_id: str,
        *,
        max_polls: int,
        max_seconds: int | float,
        stop_event: Event | None = None,
    ) -> tuple[TickResult, ...]:
        """Run bounded acquisition; the deadline admits no new polls after expiry.

        A synchronous source fetch already in progress may finish after the
        deadline because the runner cannot interrupt that bounded operation.
        """
        if type(max_polls) is not int or isinstance(max_polls, bool) or not 1 <= max_polls <= 100:
            raise ValueError("max_polls must be an integer from 1 to 100")
        if (
            type(max_seconds) not in (int, float)
            or isinstance(max_seconds, bool)
            or not 1 <= max_seconds <= 21600
        ):
            raise ValueError("max_seconds must be from 1 to 21600")
        stop_event = stop_event or Event()
        deadline = time.monotonic() + max_seconds
        results = []
        while len(results) < max_polls and not stop_event.is_set():
            if time.monotonic() >= deadline:
                break
            result = self.tick(monitor_id)
            if result.outcome != "NOT_DUE":
                results.append(result)
            if result.outcome == "LEASE_LOST":
                break
            if len(results) >= max_polls:
                break
            if result.next_poll_at is None or type(result.next_poll_at) is not str:
                raise ValueError("tick did not return a persisted next_poll_at")
            due_at = self._timestamp(result.next_poll_at, "next_poll_at")
            while time.monotonic() < deadline and not stop_event.is_set():
                due_seconds = (due_at - self.clock()).total_seconds()
                if due_seconds <= 0:
                    break
                if stop_event.wait(min(1.0, due_seconds, deadline - time.monotonic())):
                    break
        return tuple(results)
