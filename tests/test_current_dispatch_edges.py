"""Adversarial source, state and query boundaries for selective dispatch."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_case_store import CASE, MONITOR, NOW, START, query
from test_current_case_store import ready as ready
from test_current_dispatch_store import POLICY, initial, next_interval, snapshot
from test_current_dispatch_store import dispatch as dispatch
from test_current_health_integration import HEALTH_PROFILE, outcome
from test_current_source_health import publish

from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import InvocationAllowance
from watershed_memory.current.dispatch_replay import decode_policy
from watershed_memory.current.dispatch_store import DispatchStore
from watershed_memory.current.dispatch_types import DispatchStatus
from watershed_memory.current.tools import _json


def test_new_publication_during_inference_keeps_reserved_evidence_and_queues_correction(
    ready, dispatch
):
    step = dispatch.prepare(CASE, now=NOW)
    pins = step.reservation.context.source_health.version_ids
    at = NOW + timedelta(minutes=5)
    publish(
        ready[2], at, observed_at=START + timedelta(minutes=20), value="24", provider="correction"
    )
    receipt = dispatch.finish(step.reservation.attempt_id, outcome(step.reservation), now=at)
    assert receipt.status == "COMMITTED"
    assert json.loads(query(ready[0], "SELECT health_context_json FROM dispatch_settings")[0][0])[
        "source_version_ids"
    ] == list(pins)
    next_step = dispatch.prepare(CASE, now=at)
    assert next_step.event_id != step.event_id
    assert next_step.attention.reasons == ("SOURCE_CORRECTION", "MATERIAL_CHANGE")


def test_null_latest_and_recovery_each_trigger_once_without_new_sealed_interval(ready, dispatch):
    first = initial(dispatch)
    for minutes, value in ((5, None), (10, "6")):
        at = NOW + timedelta(minutes=minutes)
        publish(ready[2], at, value=value)
        step = dispatch.prepare(CASE, now=at)
        assert step.event_id == first.event_id and not step.reservation.source_delivery
        assert step.attention.reasons == ("COVERAGE_CHANGED",)
        dispatch.finish(step.reservation.attempt_id, outcome(step.reservation), now=at)
        assert dispatch.prepare(CASE, now=at).status == "QUIET"


def test_direct_journal_completion_cannot_be_silently_reconciled_as_dispatch(ready, dispatch):
    step = dispatch.prepare(CASE, now=NOW)
    dispatch.journal.commit(step.reservation.attempt_id, outcome(step.reservation), now=NOW)
    before = snapshot(ready)
    with pytest.raises((ValueError, WorkflowConflict)):
        dispatch.prepare(CASE, now=NOW)
    with pytest.raises(WorkflowConflict):
        dispatch.finish(step.reservation.attempt_id, outcome(step.reservation), now=NOW)
    assert snapshot(ready) == before


def test_restart_and_activation_never_reset_global_allowance(ready, dispatch):
    initial(dispatch)
    reopened = DispatchStore(DeliveryStore(ready[1]))
    before = snapshot(ready)
    reopened.activate(CASE, POLICY, HEALTH_PROFILE, now=NOW)
    assert snapshot(ready) == before
    with pytest.raises(ValueError):
        DeliveryStore(ready[1], allowance=InvocationAllowance(0, 11))
    assert reopened.journal.allowance().provider_used == 1


def test_bootstrap_cap_refuses_without_partly_classifying_history(ready, dispatch):
    # Synthetic historical queue rows exercise the admission bound. The selected
    # current event is genuine fixture ingestion; these rows are never inspected.
    with ready[1]._connect() as db:
        for i in range(10001):
            identity = hashlib.sha256(f"history-{i}".encode()).hexdigest()
            at = START - timedelta(minutes=15 * (i + 1))
            db.execute(
                "INSERT INTO watch_events VALUES(?,?,?,?,?,?,NULL,?,?)",
                (
                    identity,
                    MONITOR,
                    CASE,
                    at.isoformat(),
                    (at + timedelta(minutes=15)).isoformat(),
                    1,
                    "{}",
                    "0" * 64,
                ),
            )
            db.execute(
                "INSERT INTO watch_outbox VALUES(?,?,?,?,?)",
                (identity, MONITOR, at.isoformat(), 1, "PENDING"),
            )
        db.execute(
            "UPDATE monitors SET event_count=event_count+10001,pending_count=pending_count+10001"
        )
    before = snapshot(ready)
    with pytest.raises(WorkflowConflict, match="bootstrap backlog"):
        dispatch.prepare(CASE, now=NOW)
    assert snapshot(ready) == before


def test_wrong_pending_counter_rolls_back_quiet_source_receipt(ready, dispatch):
    initial(dispatch)
    at = next_interval(ready, 45, 5)
    with ready[1]._connect() as db:
        db.execute("UPDATE monitors SET pending_count=0 WHERE monitor_id=?", (MONITOR,))
    before = snapshot(ready)
    with pytest.raises(WorkflowConflict, match="pending counter"):
        dispatch.prepare(CASE, now=at)
    assert snapshot(ready) == before


@pytest.mark.parametrize("change", ["missing", "version", "extra"])
def test_dispatch_extension_never_repairs_partial_or_unknown_schema(ready, dispatch, change):
    with ready[1]._connect() as db:
        if change == "missing":
            db.execute("DROP INDEX dispatch_outcomes_recent")
        elif change == "version":
            db.execute("UPDATE dispatch_schema_version SET version=2")
        else:
            db.execute("CREATE TABLE dispatch_unknown(a TEXT)")
    before = snapshot(ready)
    with pytest.raises(ValueError):
        DispatchStore(DeliveryStore(ready[1]))
    assert snapshot(ready) == before


def test_source_selection_uses_ordered_monitor_indexes(ready, dispatch):
    queries = (
        (
            "SELECT event_id FROM watch_outbox WHERE monitor_id=? AND status='PENDING' ORDER BY interval_start,revision LIMIT 1",
            (MONITOR,),
        ),
        (
            "SELECT event_id FROM watch_outbox WHERE monitor_id=? AND status='PENDING' ORDER BY interval_start DESC,revision DESC LIMIT 1",
            (MONITOR,),
        ),
        (
            "SELECT event_id FROM watch_events WHERE monitor_id=? ORDER BY interval_start DESC,revision DESC LIMIT 1",
            (MONITOR,),
        ),
        (
            "SELECT event_id FROM watch_outbox WHERE monitor_id=? AND status='PENDING' AND (interval_start,revision)<(?,?) ORDER BY interval_start,revision LIMIT 10001",
            (MONITOR, NOW.isoformat(), 1),
        ),
    )
    with ready[1]._connect() as db:
        for sql, args in queries:
            plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql, args))
            assert "SEARCH" in plan and "USING INDEX" in plan
            assert "TEMP B-TREE" not in plan and "SCAN" not in plan


@pytest.mark.parametrize(
    "raw", ["[]", "{}", '{"rules":{}}', _json({"policy_id": "bad", "rules": [None]})]
)
def test_policy_decoder_rejects_malformed_shapes(raw):
    with pytest.raises(ValueError):
        decode_policy(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"health_evaluated_at": NOW + timedelta(seconds=1)},
        {"health_evaluated_at": NOW - timedelta(seconds=1)},
        {"assessment_count": 0, "numeric_event_id": None, "health_evaluated_at": None},
        {"active_attempt_id": "attempt-" + "a" * 32, "active_status": {}},
    ],
)
def test_status_rejects_impossible_lifecycle_and_health_times(changes):
    value = DispatchStatus(CASE, MONITOR, NOW, NOW, "a" * 64, 1, 0, 1, "a" * 64, NOW, None, None)
    with pytest.raises(ValueError):
        replace(value, **changes)


def test_accumulated_change_exposes_exact_assessed_baseline_to_agent_tools(ready, dispatch):
    from watershed_memory.current.tools import CurrentTools, validate_assessment

    first = initial(dispatch)
    for minute, value in ((45, 5), (60, 7), (75, 9), (90, 11)):
        at = next_interval(ready, minute, value)
        assert dispatch.prepare(CASE, now=at).status == "SUPPRESSED"
    at = next_interval(ready, 105, 14)
    decision = dispatch.prepare(CASE, now=at)
    assert decision.attention.reasons == ("MATERIAL_CHANGE",)
    assert decision.attention.basis_event_id == first.event_id
    ctx = decision.reservation.context
    assert first.event_id in {p.event_id for p in ctx.prior}
    tools = CurrentTools(ctx)
    tools.get_case_context()
    tools.inspect_current_series()
    tools.inspect_source_health()
    compared = tools.compare_prior_event(first.event_id)
    assert compared["comparison"]["comparable"]
    tools.stage_assessment(
        "NO_FOLLOW_UP",
        None,
        ctx.current.event_id,
        None,
        None,
        "Synthetic comparison: the assessed baseline is available to the SDK tools.",
        None,
        [first.event_id],
    )
    assessment = validate_assessment(ctx, tools.finish())
    result = replace(outcome(decision.reservation), assessment=assessment, tool_attempts=tools.attempts)
    saved = dispatch.finish(decision.reservation.attempt_id, result, now=at)
    assert saved.status == "COMMITTED"
    request = json.loads(query(ready[0], "SELECT input_json FROM delivery_attempts WHERE attempt_id=?",
        (decision.reservation.attempt_id,))[0][0])
    assert first.event_id in request["prior_event_ids"]


@pytest.mark.parametrize("external_status", ["RESERVED", "COMMITTED", "ABANDONED"])
def test_external_journal_attempt_cannot_silently_replace_dispatch_history(
    ready, dispatch, external_status
):
    event = ready[3][-1].event_id
    ticket = dispatch.journal.reserve(
        CASE,
        event,
        request_id="external-bypass",
        profile=HEALTH_PROFILE,
        source_delivery=True,
        now=NOW,
        include_source_health=True,
    )
    if external_status == "COMMITTED":
        dispatch.journal.commit(ticket.attempt_id, outcome(ticket), now=NOW)
    elif external_status == "ABANDONED":
        dispatch.journal.abandon(
            CASE, ticket.request_id, reason="Synthetic deliberate abandonment.", now=NOW
        )
    before = snapshot(ready)
    for operation in (lambda: dispatch.prepare(CASE, now=NOW), lambda: dispatch.status(CASE)):
        with pytest.raises(WorkflowConflict):
            operation()
    assert snapshot(ready) == before
