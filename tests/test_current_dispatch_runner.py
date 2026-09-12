"""Real scripted Strands execution runs after durable admission releases SQLite."""

import sqlite3
from dataclasses import replace
from importlib.metadata import version
from threading import Event

import pytest
import test_current_case_store as fixtures
from test_current_case_store import CASE, NOW, query
from test_current_case_store import ready as ready
from test_current_dispatch_store import POLICY, snapshot
from test_strands import ScriptedModel

from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import InvocationAllowance, InvocationProfile
from watershed_memory.current.dispatch_runner import DispatchRunner, DispatchTick
from watershed_memory.current.dispatch_store import DispatchStore
from watershed_memory.current.strands import CurrentStrandsPlanner


@pytest.fixture
def sdk_case(request, monkeypatch):
    monkeypatch.setattr(fixtures, "CONFIG", replace(fixtures.CONFIG, simulated=True))
    source = request.getfixturevalue("ready")
    store = DispatchStore(DeliveryStore(source[1], allowance=InvocationAllowance(2, 0)))
    profile = InvocationProfile(
        "SCRIPTED_SDK", "dispatch-sdk-fixture", "watershed-current-v2", version("strands-agents")
    )
    store.activate(CASE, POLICY, profile, now=NOW)
    calls = [
        ("get_case_context", {}),
        ("inspect_current_series", {}),
        ("inspect_source_health", {}),
        (
            "stage_assessment",
            dict(
                disposition="NO_FOLLOW_UP",
                kind=None,
                event_id=source[3][-1].event_id,
                target_task_id=None,
                title=None,
                reason="Synthetic SDK integration: no additional follow-up is requested.",
                next_check_at=None,
                reference_ids=[],
            ),
        ),
    ]
    planner = CurrentStrandsPlanner(
        ScriptedModel(calls), model_id=profile.model_id, scripted_test=True
    )
    return source, store, planner, calls


def runner(sdk_case):
    return DispatchRunner(sdk_case[1], sdk_case[2], clock=lambda: NOW)


def test_actual_sdk_turn_commits_only_after_reservation_and_releases_writer(sdk_case):
    source, store, planner, calls = sdk_case
    with source[1]._connect() as db:
        db.execute("CREATE TABLE fixture_probe(sequence INTEGER PRIMARY KEY)")

    class ProbeModel(ScriptedModel):
        async def stream(self, messages, *args, **kwargs):
            with sqlite3.connect(source[0], timeout=0) as db:
                db.execute("BEGIN IMMEDIATE")
                assert db.execute("SELECT status FROM delivery_attempts").fetchall() == [
                    ("RESERVED",)
                ]
                assert (
                    db.execute("SELECT assessment_count FROM dispatch_settings").fetchone()[0] == 0
                )
                db.execute("INSERT INTO fixture_probe DEFAULT VALUES")
            async for item in super().stream(messages, *args, **kwargs):
                yield item

    planner.model = ProbeModel(calls)
    result = runner(sdk_case).tick(CASE)
    assert result.outcome == "COMMITTED" and result.receipt.status == "COMMITTED"
    assert query(source[0], "SELECT COUNT(*) FROM fixture_probe") == [(5,)]
    assert store.journal.allowance().scripted_used == 1
    assert store.status(CASE).assessment_count == 1
    assert result.attention.reasons == ("INITIAL_REVIEW",)


def test_quiet_tick_does_not_call_sdk_again(sdk_case, monkeypatch):
    active = runner(sdk_case)
    active.tick(CASE)

    def forbidden(*args):
        raise AssertionError("quiet evidence must not invoke")

    monkeypatch.setattr(sdk_case[2], "plan", forbidden)
    before = snapshot(sdk_case[0])
    result = active.tick(CASE)
    assert result.outcome == "QUIET" and result.receipt is None and result.attempt_id is None
    assert snapshot(sdk_case[0]) == before


@pytest.mark.parametrize("field,value", [("model_id", "wrong"), ("scripted_test", False)])
def test_planner_profile_mismatch_fails_before_admission(sdk_case, field, value):
    setattr(sdk_case[2], field, value)
    before = snapshot(sdk_case[0])
    with pytest.raises(ValueError):
        runner(sdk_case).tick(CASE)
    assert snapshot(sdk_case[0]) == before


def test_actual_sdk_failure_is_sanitized_held_and_never_retried(sdk_case):
    sdk_case[2].model = ScriptedModel([])
    result = runner(sdk_case).tick(CASE)
    assert result.outcome == "FAILED" and result.receipt.status == "FAILED"
    assert result.receipt.failure_json is not None
    assert sdk_case[1].status(CASE).active_status == "FAILED"
    assert runner(sdk_case).tick(CASE).outcome == "HELD"
    assert sdk_case[1].journal.allowance().scripted_used == 1


@pytest.mark.parametrize("phase", ["inference", "commit"])
def test_unexpected_error_returns_only_unknown_identity_and_keeps_held(
    sdk_case, monkeypatch, phase
):
    private = "PRIVATE-RUNNER-EXCEPTION-CANARY"

    def fail(*args, **kwargs):
        raise RuntimeError(private)

    monkeypatch.setattr(
        sdk_case[2] if phase == "inference" else sdk_case[1],
        "plan" if phase == "inference" else "finish",
        fail,
    )
    result = runner(sdk_case).tick(CASE)
    assert result.outcome == "OUTCOME_UNKNOWN" and result.attempt_id is not None
    assert result.receipt is None and private not in repr(result)
    assert sdk_case[1].status(CASE).active_status == "RESERVED"
    assert runner(sdk_case).tick(CASE).outcome == "HELD"


def test_held_and_exhausted_tick_never_enters_model(sdk_case, monkeypatch):
    source, store, planner, _ = sdk_case

    def forbidden(*args):
        raise AssertionError("no invocation allowed")

    monkeypatch.setattr(planner, "plan", forbidden)
    with source[1]._connect() as db:
        db.execute("UPDATE delivery_allowance SET scripted_used=scripted_limit")
    before = snapshot(source)
    result = runner(sdk_case).tick(CASE)
    assert result.outcome == "ALLOWANCE_EXHAUSTED" and result.attempt_id is None
    assert snapshot(source) == before


def test_preexisting_stop_prevents_any_admission(sdk_case):
    stop = Event()
    stop.set()
    before = snapshot(sdk_case[0])
    assert runner(sdk_case).run(CASE, max_steps=2, max_seconds=10, stop_event=stop) == ()
    assert snapshot(sdk_case[0]) == before


def test_runner_stops_after_failure_instead_of_retrying(sdk_case):
    sdk_case[2].model = ScriptedModel([])
    results = runner(sdk_case).run(CASE, max_steps=10, max_seconds=10, check_seconds=1)
    assert len(results) == 1 and results[0].outcome == "FAILED"
    assert sdk_case[1].journal.allowance().scripted_used == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_steps": True},
        {"max_steps": 101},
        {"max_steps": 0},
        {"max_seconds": float("nan")},
        {"max_seconds": 21601},
        {"max_seconds": True},
        {"check_seconds": 0},
        {"check_seconds": 3601},
        {"check_seconds": True},
    ],
)
def test_run_bounds_reject_before_any_admission(sdk_case, kwargs):
    before = snapshot(sdk_case[0])
    args = dict(max_steps=1, max_seconds=10, check_seconds=1)
    args.update(kwargs)
    with pytest.raises(ValueError):
        runner(sdk_case).run(CASE, **args)
    assert snapshot(sdk_case[0]) == before


def test_default_runner_clock_is_an_aware_wall_clock(sdk_case):
    result = DispatchRunner(sdk_case[1], sdk_case[2]).tick(CASE)
    assert result.outcome == "COMMITTED"


def test_failed_receipt_write_returns_unknown_without_exposing_exception(sdk_case, monkeypatch):
    sdk_case[2].model = ScriptedModel([])

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE-FAILURE-WRITE-CANARY")

    monkeypatch.setattr(sdk_case[1], "fail", fail)
    result = runner(sdk_case).tick(CASE)
    assert result.outcome == "OUTCOME_UNKNOWN" and result.receipt is None
    assert "PRIVATE-FAILURE-WRITE-CANARY" not in repr(result)
    assert sdk_case[1].status(CASE).active_status == "RESERVED"


def test_runner_does_not_wait_after_last_allowed_step(sdk_case):
    class NoWait(Event):
        def wait(self, timeout=None):
            raise AssertionError("no wait after the final admitted step")

    results = runner(sdk_case).run(CASE, max_steps=1, max_seconds=10, stop_event=NoWait())
    assert len(results) == 1 and results[0].outcome == "COMMITTED"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"case_id": "bad case", "outcome": "WAITING_FOR_SOURCE"},
        {"case_id": CASE, "outcome": "COMMITTED"},
        {"case_id": CASE, "outcome": "QUIET"},
        {"case_id": CASE, "outcome": "HELD", "attempt_id": "attempt-malformed"},
        {"case_id": CASE, "outcome": "HELD", "attempt_id": []},
        {"case_id": CASE, "outcome": "WAITING_FOR_SOURCE", "attention": {}},
        {"case_id": CASE, "outcome": "OUTCOME_UNKNOWN", "attempt_id": "attempt-" + "a" * 32},
    ],
)
def test_tick_record_rejects_incomplete_and_malformed_results(kwargs):
    with pytest.raises(ValueError):
        DispatchTick(**kwargs)


def test_tick_record_binds_receipt_case_event_attempt_and_status(sdk_case):
    result = runner(sdk_case).tick(CASE)
    for changes in (
        {"case_id": "another-case"},
        {"event_id": "f" * 64},
        {"attempt_id": "attempt-" + "f" * 32},
        {"outcome": "FAILED"},
        {"attention": {}},
    ):
        with pytest.raises(ValueError):
            replace(result, **changes)
