"""Integrity regressions for the field-dispatch composition boundary."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_case_store import CASE, NOW, draft
from test_current_case_store import ready as ready
from test_current_dispatch_store import POLICY, outcome
from test_current_field_dispatch_store import V3_PROFILE, execution, failed, snapshot
from test_current_field_store import SITE
from test_current_health_integration import HEALTH_PROFILE

from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.context import load_context
from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import InvocationAllowance
from watershed_memory.current.dispatch_replay import capture_admission, evaluate_admission
from watershed_memory.current.dispatch_store import DispatchStore
from watershed_memory.current.field_delivery_store import FieldDeliveryStore
from watershed_memory.current.field_dispatch_store import FieldDispatchStore
from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.locations import LocationRegistry
from watershed_memory.current.tools import _json


@pytest.fixture
def dispatch(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    source = DispatchStore(journal)
    source.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    fields = FieldStore(ready[1], LocationRegistry((replace(SITE, case_id=CASE),)))
    store = FieldDispatchStore(source, FieldDeliveryStore(journal, fields))
    store.upgrade_to_v3(CASE, V3_PROFILE, now=NOW)
    return store


def completed_v2(ready):
    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 10))
    source = DispatchStore(journal)
    source.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    step = source.prepare(CASE, now=NOW)
    source.finish(step.reservation.attempt_id, outcome(step.reservation), now=NOW)
    fields = FieldStore(ready[1], LocationRegistry((replace(SITE, case_id=CASE),)))
    return FieldDispatchStore(source, FieldDeliveryStore(journal, fields)), step


@pytest.mark.parametrize("operation", ["get", "fail"])
def test_substituted_coherent_source_replay_cannot_replace_reserved_v3_base(
    ready, dispatch, operation
):
    step = dispatch.prepare(CASE, now=NOW)
    ticket = step.reservation
    alternate = load_context(
        ready[1],
        CASE,
        step.event_id,
        evaluated_at=NOW + timedelta(seconds=1),
        include_source_health=True,
    )
    attention, move = evaluate_admission(alternate, POLICY, None, None, ())
    replay = capture_admission(alternate, None, None, (), source_delivery=ticket.source_delivery)
    with ready[1]._connect() as db:
        db.execute(
            "UPDATE dispatch_attempts SET replay_json=?,attention_json=?,move_numeric=? "
            "WHERE case_id=? AND attempt_id=?",
            (replay, _json(attention), int(move), CASE, ticket.attempt_id),
        )
    assert alternate.evaluated_at != ticket.context.base.evaluated_at
    before = snapshot(ready)
    calls = {
        "get": lambda: dispatch.get(CASE, ticket.request_id),
        "fail": lambda: dispatch.fail(ticket.attempt_id, failed(ticket), now=NOW),
    }
    with pytest.raises(WorkflowConflict):
        calls[operation]()
    assert snapshot(ready) == before


@pytest.mark.parametrize("operation", ["get", "status", "prepare"])
def test_lower_level_commit_leaves_detectable_split_dispatch_state(ready, dispatch, operation):
    step = dispatch.prepare(CASE, now=NOW)
    ticket = step.reservation
    committed = dispatch.delivery.commit(ticket.attempt_id, execution(ticket), now=NOW)
    assert committed.delivery.status == "COMMITTED"
    with ready[1]._connect() as db:
        state = db.execute(
            "SELECT active_attempt_id,numeric_context_json,health_context_json "
            "FROM dispatch_settings WHERE case_id=?",
            (CASE,),
        ).fetchone()
        assert tuple(state) == (ticket.attempt_id, None, None)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM dispatch_source_outcomes WHERE attempt_id=?",
                (ticket.attempt_id,),
            ).fetchone()[0]
            == 0
        )
    before = snapshot(ready)
    calls = {
        "get": lambda: dispatch.get(CASE, ticket.request_id),
        "status": lambda: dispatch.status(CASE),
        "prepare": lambda: dispatch.prepare(CASE, now=NOW),
    }
    with pytest.raises(WorkflowConflict):
        calls[operation]()
    assert snapshot(ready) == before


def test_upgrade_rejects_tampered_valid_legacy_attempt_profile_before_writes(ready):
    store, step = completed_v2(ready)
    other = replace(HEALTH_PROFILE, model_id="other-valid-model")
    with ready[1]._connect() as db:
        db.execute(
            "UPDATE delivery_attempts SET profile_json=? WHERE attempt_id=?",
            (_json(other), step.reservation.attempt_id),
        )
    before = snapshot(ready)
    with pytest.raises(WorkflowConflict):
        store.upgrade_to_v3(CASE, V3_PROFILE, now=NOW + timedelta(seconds=1))
    assert snapshot(ready) == before


def test_upgrade_rejects_context3_membership_on_legacy_history_before_writes(ready):
    store, step = completed_v2(ready)
    with ready[1]._connect() as db:
        db.execute(
            "INSERT INTO context3_attempts VALUES(?,?,?,?,?,?)",
            (
                step.reservation.attempt_id,
                CASE,
                "{}",
                "0" * 64,
                "1" * 64,
                NOW.isoformat(),
            ),
        )
    before = snapshot(ready)
    with pytest.raises(WorkflowConflict):
        store.upgrade_to_v3(CASE, V3_PROFILE, now=NOW + timedelta(seconds=1))
    assert snapshot(ready) == before


def test_repeat_upgrade_cannot_precede_current_human_case_time(ready, dispatch):
    human_at = NOW + timedelta(minutes=10)
    ready[1].stage_review(
        CASE,
        draft(ready[3][0]),
        request_id="later-human-review",
        expected_case_revision=0,
        now=human_at,
    )
    before = snapshot(ready)
    with pytest.raises(ValueError):
        dispatch.upgrade_to_v3(CASE, V3_PROFILE, now=human_at - timedelta(minutes=1))
    assert snapshot(ready) == before


@pytest.mark.parametrize("held_status", ["RESERVED", "FAILED", "STALE"])
@pytest.mark.parametrize("operation", ["get", "status", "prepare"])
def test_held_attempt_and_active_pointer_must_match_in_both_directions(
    ready,
    dispatch,
    held_status,
    operation,
):
    ticket = dispatch.prepare(CASE, now=NOW).reservation
    if held_status == "FAILED":
        dispatch.fail(ticket.attempt_id, failed(ticket), now=NOW)
    elif held_status == "STALE":
        ready[1].stage_review(
            CASE,
            draft(ready[3][0]),
            request_id="human-changed-plan",
            expected_case_revision=0,
            now=NOW,
        )
        assert (
            dispatch.finish(ticket.attempt_id, execution(ticket), now=NOW).delivery.status
            == "STALE"
        )
    with ready[1]._connect() as db:
        db.execute("UPDATE dispatch_settings SET active_attempt_id=NULL WHERE case_id=?", (CASE,))
    before = snapshot(ready)
    calls = {
        "get": lambda: dispatch.get(CASE, ticket.request_id),
        "status": lambda: dispatch.status(CASE),
        "prepare": lambda: dispatch.prepare(CASE, now=NOW),
    }
    with pytest.raises(WorkflowConflict):
        calls[operation]()
    assert snapshot(ready) == before
