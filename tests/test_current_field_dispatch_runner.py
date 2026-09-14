"""The real SDK runs between v3 reservation and atomic dispatch completion."""

import json
import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from importlib.metadata import version
from threading import Event

import pytest
import test_current_case_store as source_fixtures
from test_current_dispatch_store import POLICY
from test_current_field_sdk_budget import BatchModel
from test_current_field_sdk_fixture import FieldResultScriptedModel
from test_current_field_store import CASE, NOW, contents, verified
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import InvocationAllowance, InvocationProfile
from watershed_memory.current.dispatch_store import DispatchStore
from watershed_memory.current.field_delivery_store import FieldDeliveryStore
from watershed_memory.current.field_dispatch_runner import FieldDispatchRunner, FieldDispatchTick
from watershed_memory.current.field_dispatch_store import FieldDispatchStore
from watershed_memory.current.field_strands import CurrentStrandsPlannerV3

AT = NOW + timedelta(minutes=20)


@pytest.fixture
def sdk_field(request, monkeypatch):
    monkeypatch.setattr(source_fixtures, "CASE", CASE)
    monkeypatch.setattr(
        source_fixtures,
        "CONFIG",
        replace(
            source_fixtures.CONFIG,
            case_id=CASE,
            simulated=True,
        ),
    )
    source = request.getfixturevalue("ready")
    journal = DeliveryStore(source[1], allowance=InvocationAllowance(3, 0))
    base = DispatchStore(journal)
    profile = InvocationProfile(
        "SCRIPTED_SDK",
        "field-result-scripted-fixture",
        "watershed-current-v2",
        version("strands-agents"),
    )
    base.activate(CASE, POLICY, profile, now=NOW)
    field_case = request.getfixturevalue("field")
    verified(field_case, "PARTIAL")
    store = FieldDispatchStore(base, FieldDeliveryStore(journal, field_case[2]))
    store.upgrade_to_v3(CASE, replace(profile, instruction_version="watershed-current-v3"), now=AT)
    planner = CurrentStrandsPlannerV3(
        FieldResultScriptedModel(), model_id=profile.model_id, scripted_test=True
    )
    return field_case, store, planner


def runner(sdk_field):
    return FieldDispatchRunner(sdk_field[1], sdk_field[2], clock=lambda: AT)


def test_actual_sdk_field_proposal_commits_after_six_unlocked_responses(sdk_field):
    case, store, planner = sdk_field
    probes = []

    def probe():
        with closing(sqlite3.connect(case[0], timeout=0)) as db:
            db.execute("BEGIN IMMEDIATE")
            assert db.execute("SELECT status FROM delivery_attempts").fetchall() == [("RESERVED",)]
            assert db.execute("SELECT COUNT(*) FROM context3_agent_proposals").fetchone() == (0,)
            db.rollback()
        probes.append(True)

    planner.model = FieldResultScriptedModel(before_response=probe)
    result = runner(sdk_field).tick(CASE)
    assert type(result) is FieldDispatchTick and result.outcome == "COMMITTED"
    assert len(probes) == 6
    assert result.receipt.delivery.status == "COMMITTED"
    assert result.receipt.field_plan.plan.author_kind == "AGENT"
    assert result.receipt.field_plan.plan.status == "PROPOSED"
    assert result.receipt.field_plan.plan.task_id == case[3].task_id
    assert store.status(CASE).active_attempt_id is None
    assert store.status(CASE).assessment_count == 1
    assert store.journal.allowance().scripted_used == 1
    assert store.journal.allowance().provider_used == 0
    assert store.get(CASE, result.receipt.delivery.request_id) == result.receipt
    with pytest.raises(FrozenInstanceError):
        result.outcome = "FAILED"


def test_quiet_check_after_commit_never_calls_model_or_changes_state(sdk_field, monkeypatch):
    active = runner(sdk_field)
    assert active.tick(CASE).outcome == "COMMITTED"
    before = contents(sdk_field[0][0])

    def forbidden(*args):
        raise AssertionError("quiet checks cannot invoke the SDK")

    monkeypatch.setattr(sdk_field[2], "plan", forbidden)
    assert active.tick(CASE).outcome == "QUIET"
    assert contents(sdk_field[0][0]) == before


@pytest.mark.parametrize("attribute,value", [("model_id", "wrong-model"), ("scripted_test", False)])
def test_profile_mismatch_refuses_before_admission(sdk_field, attribute, value):
    setattr(sdk_field[2], attribute, value)
    before = contents(sdk_field[0][0])
    with pytest.raises(ValueError):
        runner(sdk_field).tick(CASE)
    assert contents(sdk_field[0][0]) == before


def test_failed_sdk_turn_is_saved_once_and_held_without_retry(sdk_field):
    sdk_field[2].model = BatchModel([])
    active = runner(sdk_field)
    results = active.run(CASE, max_steps=3, max_seconds=5, check_seconds=1)
    assert len(results) == 1 and results[0].outcome == "FAILED"
    assert results[0].receipt.delivery.failure_json is not None
    before = contents(sdk_field[0][0])
    assert active.tick(CASE).outcome == "HELD"
    assert contents(sdk_field[0][0]) == before
    assert sdk_field[1].journal.allowance().scripted_used == 1


def test_overflow_code_and_exact_admitted_count_survive_failure_persistence(sdk_field):
    sdk_field[2].model = BatchModel([[("unknown_tool", {})] * 17])
    result = runner(sdk_field).tick(CASE)
    assert result.outcome == "FAILED"
    failure = json.loads(result.receipt.delivery.failure_json)
    assert failure["code"] == "CURRENT_V3_TOOL_BUDGET_EXHAUSTED"
    assert failure["tool_attempts"] == 0 and failure["model_calls"] == 1
    assert failure["source_trace"] == [] and failure["field_trace"] == []
    assert sdk_field[1].get(CASE, result.receipt.delivery.request_id) == result.receipt
    assert runner(sdk_field).tick(CASE).outcome == "HELD"
    assert sdk_field[1].journal.allowance().scripted_used == 1


@pytest.mark.parametrize("phase", ["plan", "finish", "fail"])
def test_uncertain_execution_keeps_attempt_identity_and_no_invented_receipt(
    sdk_field,
    monkeypatch,
    phase,
):
    def interrupted(*args, **kwargs):
        raise RuntimeError("PRIVATE-INTERRUPTION-CANARY")

    if phase == "fail":
        sdk_field[2].model = BatchModel([])
    monkeypatch.setattr(sdk_field[2] if phase == "plan" else sdk_field[1], phase, interrupted)
    result = runner(sdk_field).tick(CASE)
    assert result.outcome == "OUTCOME_UNKNOWN" and result.attempt_id
    assert result.receipt is None and "PRIVATE-INTERRUPTION-CANARY" not in repr(result)
    assert sdk_field[1].status(CASE).active_status == "RESERVED"
    assert runner(sdk_field).tick(CASE).outcome == "HELD"


def test_exhausted_allowance_never_enters_sdk(sdk_field):
    case, store, planner = sdk_field
    with store.cases._connect() as db:
        db.execute("UPDATE delivery_allowance SET scripted_used=scripted_limit")
    before = contents(case[0])
    assert runner(sdk_field).tick(CASE).outcome == "ALLOWANCE_EXHAUSTED"
    assert planner.model.response_count == 0
    assert contents(case[0]) == before


def test_explicit_stop_prevents_admission(sdk_field):
    stop = Event()
    stop.set()
    before = contents(sdk_field[0][0])
    assert runner(sdk_field).run(CASE, max_steps=3, max_seconds=5, stop_event=stop) == ()
    assert contents(sdk_field[0][0]) == before


def test_field_tick_rejects_mismatched_receipt_identity(sdk_field):
    tick = runner(sdk_field).tick(CASE)
    with pytest.raises(ValueError):
        replace(tick, case_id="ANOTHER-CASE")
    with pytest.raises(ValueError):
        replace(tick, outcome="FAILED")
