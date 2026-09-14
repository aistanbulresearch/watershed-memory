"""Admission records reject misleading or detached field dispatch states."""

from dataclasses import FrozenInstanceError

import pytest
from test_current_field_delivery_store import reserve, store
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.attention import AttentionResult
from watershed_memory.current.field_dispatch_types import FieldDispatchStep


@pytest.fixture
def ticket(field):
    return reserve(store(field), field)


def attention(ticket, eligible=True):
    return AttentionResult(
        eligible,
        ("INITIAL_REVIEW",) if eligible else (),
        (),
        (),
        ticket.context.base.policy_digest,
        None,
    )


def test_valid_reserved_step_is_frozen_and_keeps_exact_ticket(ticket):
    b = ticket.context.base
    step = FieldDispatchStep(b.case_id, "RESERVED", b.current.event_id, attention(ticket), ticket)
    assert step.reservation is ticket
    with pytest.raises(FrozenInstanceError):
        step.status = "QUIET"


@pytest.mark.parametrize("status", ["QUIET", "SUPPRESSED"])
def test_quiet_step_has_no_invocation_ticket(ticket, status):
    b = ticket.context.base
    step = FieldDispatchStep(b.case_id, status, b.current.event_id, attention(ticket, False))
    assert step.reservation is None and step.held_attempt_id is None


@pytest.mark.parametrize("status", ["WAITING_FOR_SOURCE", "HELD"])
def test_nondecision_steps_preserve_only_their_permitted_identity(ticket, status):
    kwargs = {} if status == "WAITING_FOR_SOURCE" else {"held_attempt_id": ticket.attempt_id}
    step = FieldDispatchStep(ticket.context.base.case_id, status, **kwargs)
    assert step.attention is None and step.reservation is None


@pytest.mark.parametrize(
    "change",
    [
        {"case_id": "different-case"},
        {"event_id": "event-" + "f" * 64},
        {"attention": None},
        {"reservation": None},
        {"held_attempt_id": "attempt-" + "a" * 32},
        {"status": "QUIET"},
        {"status": "WAITING_FOR_SOURCE"},
        {"status": "HELD"},
        {"status": "COMMITTED"},
        {"status": False},
    ],
)
def test_admission_cannot_misstate_the_reserved_identity_or_outcome(ticket, change):
    b = ticket.context.base
    args = dict(
        case_id=b.case_id,
        status="RESERVED",
        event_id=b.current.event_id,
        attention=attention(ticket),
        reservation=ticket,
    )
    args.update(change)
    with pytest.raises(ValueError):
        FieldDispatchStep(**args)


@pytest.mark.parametrize("status", ["QUIET", "SUPPRESSED"])
def test_eligible_attention_cannot_be_labelled_quiet(ticket, status):
    b = ticket.context.base
    with pytest.raises(ValueError):
        FieldDispatchStep(b.case_id, status, b.current.event_id, attention(ticket))


def test_held_state_requires_an_exact_attempt_identity(ticket):
    for attempt in (None, "bad-attempt", 1):
        with pytest.raises(ValueError):
            FieldDispatchStep(ticket.context.base.case_id, "HELD", held_attempt_id=attempt)


def test_waiting_cannot_carry_a_selected_source_event(ticket):
    b = ticket.context.base
    with pytest.raises(ValueError):
        FieldDispatchStep(b.case_id, "WAITING_FOR_SOURCE", b.current.event_id)


def test_reservation_rejects_another_well_formed_event(ticket):
    b = ticket.context.base
    event = b.current.event_id[:-1] + ("1" if b.current.event_id[-1] != "1" else "2")
    with pytest.raises(ValueError, match="does not match"):
        FieldDispatchStep(b.case_id, "RESERVED", event, attention(ticket), ticket)
