"""V3 journal records preserve held states and cannot impersonate human work."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_context_v3 import load
from test_current_field_assessment_types import HASH, assessment
from test_current_field_store import CASE, NOW, proposed
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.delivery_types import DeliveryReceipt, InvocationProfile
from watershed_memory.current.field_delivery_types import (
    CurrentExecutionV3,
    CurrentFailureV3,
    FieldDeliveryReceipt,
    FieldDeliveryReservation,
)

ATTEMPT = "attempt-" + "a" * 32
PROFILE = InvocationProfile("SCRIPTED_SDK", "fixture", "watershed-current-v3", "1.55.1")


def execution(**changes):
    return replace(
        CurrentExecutionV3(
            assessment(),
            "SCRIPTED_SDK",
            "fixture",
            "watershed-current-v3",
            "1.55.1",
            1,
            3,
            0.0,
            "{}",
            "end_turn",
        ),
        **changes,
    )


def failure(**changes):
    return replace(
        CurrentFailureV3(
            CASE,
            1,
            HASH,
            HASH,
            HASH,
            "SCRIPTED_SDK",
            "fixture",
            "watershed-current-v3",
            "1.55.1",
            0,
            0,
            (),
            (),
            "{}",
        ),
        **changes,
    )


def delivery(status="COMMITTED", **changes):
    value = DeliveryReceipt(
        ATTEMPT,
        "delivery-request",
        CASE,
        HASH,
        PROFILE,
        status,
        False,
        NOW,
        execution_json="{}" if status in {"COMMITTED", "STALE"} else None,
        failure_json="{}" if status == "FAILED" else None,
        reason="Explicitly abandoned by the operator." if status == "ABANDONED" else None,
    )
    return replace(value, **changes)


def agent_receipt(field):
    original = proposed(field)
    return replace(
        original,
        request_id=ATTEMPT + "-field-0",
        plan=replace(original.plan, author_kind="AGENT", recorded_by=ATTEMPT),
    )


def test_execution_and_failure_are_immutable_and_preserve_charged_attempts():
    value = execution(tool_attempts=16)
    assert value.tool_attempts > len(value.assessment.base.trace) + len(
        value.assessment.field_trace
    )
    failed = failure(
        model_calls=1,
        tool_attempts=4,
        source_trace=assessment().base.trace,
        field_trace=assessment().field_trace,
    )
    for item in (value, failed):
        assert not hasattr(item, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            item.mode = "STRANDS_CURRENT"


@pytest.mark.parametrize(
    "change",
    [
        {"model_calls": True},
        {"model_calls": 0},
        {"model_calls": 9},
        {"tool_attempts": True},
        {"tool_attempts": 2},
        {"tool_attempts": 17},
        {"elapsed_seconds": float("inf")},
        {"elapsed_seconds": float("nan")},
        {"elapsed_seconds": -1.0},
        {"stop_reason": "max_tokens"},
        {"instruction_version": "watershed-current-v2"},
        {"usage_json": '{"n":1,"n":2}'},
    ],
)
def test_invalid_execution_is_rejected(change):
    with pytest.raises(ValueError):
        execution(**change)


@pytest.mark.parametrize(
    "change",
    [
        {"case_revision": True},
        {"context_digest": "A" * 64},
        {"model_calls": 9},
        {"tool_attempts": 17},
        {"source_trace": assessment().base.trace},
        {"field_trace": list(assessment().field_trace)},
        {"instruction_version": "watershed-current-v2"},
        {"code": "CURRENT_TURN_FAILED"},
    ],
)
def test_invalid_failure_cannot_misstate_counts_or_context(change):
    with pytest.raises(ValueError):
        failure(**change)


@pytest.mark.parametrize("status", ["RESERVED", "COMMITTED", "FAILED", "STALE", "ABANDONED"])
def test_receipt_preserves_each_delivery_state_without_inventing_field_work(status):
    original = delivery(status)
    value = FieldDeliveryReceipt(original)
    assert value.delivery == original and value.field_plan is None


def test_legacy_profile_is_not_a_v3_receipt():
    with pytest.raises(ValueError):
        FieldDeliveryReceipt(
            delivery(profile=replace(PROFILE, instruction_version="watershed-current-v2"))
        )


def test_agent_proposal_receipt_binds_attempt_case_and_time_without_human_authority(field):
    proposal = agent_receipt(field)
    value = FieldDeliveryReceipt(delivery(), proposal)
    assert value.field_plan.plan.author_kind == "AGENT"
    assert value.field_plan.plan.status == "PROPOSED"
    assert value.delivery.work == ()
    assert not hasattr(value, "principal")
    for wrong in (
        replace(proposal, request_id="unrelated-request"),
        replace(proposal, plan=replace(proposal.plan, recorded_by="other-attempt")),
        replace(proposal, recorded_at=NOW + timedelta(seconds=1)),
    ):
        with pytest.raises(ValueError):
            FieldDeliveryReceipt(delivery(), wrong)
    with pytest.raises(ValueError):
        FieldDeliveryReceipt(delivery("STALE"), proposal)


def test_human_plan_receipt_cannot_be_presented_as_an_agent_proposal(field):
    with pytest.raises(ValueError):
        FieldDeliveryReceipt(delivery(), proposed(field))


def test_reservation_requires_exact_context_time_and_v3_profile(field):
    context = load(field)
    value = FieldDeliveryReservation(
        ATTEMPT, "reserved-request", PROFILE, False, context, context.base.evaluated_at
    )
    for change in (
        {"context": context.base},
        {"profile": replace(PROFILE, instruction_version="watershed-current-v2")},
        {"source_delivery": 1},
        {"reserved_at": context.base.evaluated_at + timedelta(seconds=1)},
    ):
        with pytest.raises(ValueError):
            replace(value, **change)
