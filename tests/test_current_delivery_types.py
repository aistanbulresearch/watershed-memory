"""Synthetic typed journal records; this suite makes no provider calls."""

from dataclasses import FrozenInstanceError, replace

import pytest
from test_current_case_store import CASE, NOW
from test_current_case_store import ready as ready
from test_current_tools import context

from watershed_memory.current.delivery_types import (
    AllowanceSnapshot,
    DeliveryReceipt,
    DeliveryReservation,
    InvocationAllowance,
    InvocationProfile,
)

PROFILE = InvocationProfile("STRANDS_CURRENT", "synthetic-model", "watershed-current-v1", "fixture")
ATTEMPT = "attempt-" + "a" * 32


@pytest.mark.parametrize("value", [-1, 10001, True, 1.0, "1"])
def test_allowance_is_an_explicit_bounded_integer_count(value):
    with pytest.raises(ValueError):
        InvocationAllowance(value, 0)
    with pytest.raises(ValueError):
        InvocationAllowance(0, value)


def test_allowance_snapshot_cannot_claim_more_capacity_than_configured():
    snapshot = AllowanceSnapshot(5, 2, 0, 0)
    assert snapshot.scripted_used == 2
    with pytest.raises(FrozenInstanceError):
        snapshot.scripted_used = 0
    with pytest.raises(ValueError):
        replace(snapshot, provider_used=1)


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "RULES"},
        {"model_id": ""},
        {"sdk_version": "x" * 201},
        {"instruction_version": "bad\nversion"},
    ],
)
def test_profile_binds_a_known_mode_and_bounded_version_identity(changes):
    with pytest.raises(ValueError):
        replace(PROFILE, **changes)


def test_reservation_contains_a_frozen_snapshot_and_fixed_time(ready):
    ctx = context(ready)
    reserved = DeliveryReservation(ATTEMPT, "request-1", PROFILE, True, ctx, NOW)
    assert reserved.context is ctx
    with pytest.raises(FrozenInstanceError):
        reserved.profile = replace(PROFILE, model_id="different")
    for changes in (
        {"source_delivery": 1},
        {"request_id": "../bad"},
        {"attempt_id": "attempt-bad"},
        {"reserved_at": NOW.replace(tzinfo=None)},
    ):
        with pytest.raises(ValueError):
            replace(reserved, **changes)


def receipt(**changes):
    value = DeliveryReceipt(ATTEMPT, "request-1", CASE, "b" * 64, PROFILE, "RESERVED", True, NOW)
    return replace(value, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"work": []},
        {"status": "DONE"},
        {"execution_json": "{}"},
        {"status": "COMMITTED"},
        {"status": "FAILED"},
        {"status": "STALE"},
        {"status": "ABANDONED"},
        {"reason": "A reason on an unfinished reservation."},
    ],
)
def test_receipt_status_cannot_misrepresent_its_saved_outcome(changes):
    with pytest.raises(ValueError):
        receipt(**changes)


def test_typed_outcome_records_have_bounded_canonical_json_and_reason():
    assert receipt(status="COMMITTED", execution_json="{}").status == "COMMITTED"
    assert receipt(status="FAILED", failure_json="{}").status == "FAILED"
    assert receipt(status="STALE", execution_json="{}").work == ()
    assert receipt(status="ABANDONED", reason="Explicit reconciliation: no retry needed.")
    for content in (
        "[]",
        '{"a":1,"a":2}',
        '{"x":NaN}',
        '{ "x": 1 }',
        '{"x":"' + "x" * 524288 + '"}',
    ):
        with pytest.raises(ValueError):
            receipt(status="COMMITTED", execution_json=content)


def test_recursive_or_invalid_unicode_receipt_json_fails_with_a_validation_error():
    for content in ('{"x":' + ("[" * 1500) + "0" + ("]" * 1500) + "}", '{"x":"\ud800"}'):
        with pytest.raises(ValueError):
            receipt(status="COMMITTED", execution_json=content)
