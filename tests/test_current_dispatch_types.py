"""Bounded dispatch state records are immutable and status-consistent."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from watershed_memory.current.dispatch_types import DispatchStatus, DispatchStep

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
EVENT = "a" * 64


def test_waiting_step_is_empty_and_frozen():
    step = DispatchStep("case-1", "WAITING_FOR_SOURCE")
    with pytest.raises(FrozenInstanceError):
        step.status = "QUIET"


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "UNKNOWN"},
        {"status": "QUIET", "event_id": None},
        {"status": "HELD"},
        {"case_id": "bad case"},
    ],
)
def test_step_rejects_incomplete_or_malformed_state(changes):
    with pytest.raises(ValueError):
        replace(DispatchStep("case-1", "WAITING_FOR_SOURCE"), **changes)


def test_unactivated_status_has_no_counters_or_active_attempt():
    with pytest.raises(ValueError):
        DispatchStatus("case-1", "monitor-1", NOW, NOW, None, 1, 0, 0, None, None, None, None)


def test_status_counts_and_times_are_bounded():
    value = DispatchStatus(
        "case-1",
        "monitor-1",
        NOW,
        NOW,
        EVENT,
        0,
        0,
        0,
        None,
        None,
        "attempt-" + "a" * 32,
        "RESERVED",
    )
    assert value.initial_event_id == EVENT
    with pytest.raises(ValueError):
        replace(value, history_count=True)
    with pytest.raises(ValueError):
        replace(value, updated_at=NOW.replace(year=2025))


def test_status_enforces_health_window_and_bootstrap_reservation():
    value = DispatchStatus(
        "case-1",
        "monitor-1",
        NOW,
        NOW,
        EVENT,
        0,
        0,
        0,
        None,
        None,
        "attempt-" + "a" * 32,
        "RESERVED",
    )
    with pytest.raises(ValueError):
        replace(value, history_count=10001)
    with pytest.raises(ValueError):
        replace(value, suppressed_count=1)
    with pytest.raises(ValueError):
        replace(
            value,
            health_evaluated_at=NOW.replace(year=2025),
            assessment_count=1,
            numeric_event_id=EVENT,
        )
    with pytest.raises(ValueError):
        replace(value, assessment_count=1, numeric_event_id=EVENT)
    with pytest.raises(ValueError):
        replace(value, active_attempt_id="attempt-" + "a" * 32, active_status=[])
