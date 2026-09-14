"""Actual Strands tool execution with controlled field results, never paid inference."""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest
import test_current_field_store as field_fixtures
from test_current_context_v3 import load
from test_current_field_sdk_budget import BatchModel
from test_current_field_store import CANARY, CASE, NOW, attached, contents, verified
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.case_records import digest
from watershed_memory.current.case_types import ReviewDraft
from watershed_memory.current.field_strands import CurrentStrandsPlannerV3, CurrentTurnErrorV3
from watershed_memory.current.tools import _json


def planner(model=None, **kwargs):
    return CurrentStrandsPlannerV3(
        model or BatchModel([]),
        model_id="field-result-scripted-fixture",
        scripted_test=True,
        **kwargs,
    )


@pytest.mark.parametrize(
    "options",
    [
        {"max_calls": 0},
        {"max_calls": 13},
        {"max_calls": True},
        {"max_calls": 1.0},
        {"seconds": 0},
        {"seconds": -1},
        {"seconds": 121},
        {"seconds": True},
        {"seconds": float("nan")},
        {"seconds": float("inf")},
        {"seconds": "1"},
    ],
)
def test_invalid_limits_are_rejected_before_model_use(options):
    with pytest.raises(ValueError):
        planner(**options)


def test_model_factory_is_never_called():
    called = []
    with pytest.raises(ValueError):
        planner(lambda: called.append(True))
    assert not called


def test_legacy_context_is_rejected_before_model_use(field):
    model = BatchModel([])
    with pytest.raises(ValueError):
        planner(model).plan(load(field).base)
    assert model.responses == 0


@pytest.mark.parametrize(
    "outcome,level,expected",
    [
        ("COMPLETE", "VERIFIED", "NO_NEW_FIELD_PLAN"),
        ("COMPLETE", "EVIDENCE_ATTACHED", "AWAIT_VERIFICATION"),
        ("PARTIAL", "VERIFIED", "PROPOSE_FIELD_PLAN"),
    ],
)
def test_actual_sdk_uses_exact_field_result_and_leaves_database_untouched(
    field,
    outcome,
    level,
    expected,
):
    from test_current_field_sdk_fixture import FieldResultScriptedModel

    (verified if level == "VERIFIED" else attached)(field, outcome)
    context = load(field)
    before = contents(field[0])
    probes = []

    def writer_probe():
        with closing(sqlite3.connect(field[0], timeout=0)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        probes.append(True)

    model = FieldResultScriptedModel(before_response=writer_probe)
    result = planner(model).plan(context)
    basis = context.field_work.latest_results[0]
    assert result.mode == "SCRIPTED_SDK" and result.instruction_version == "watershed-current-v3"
    assert result.model_calls == 6 and len(probes) == 6
    assert result.stop_reason == "end_turn" and result.sdk_version
    assert 1 <= result.tool_attempts <= 16
    assert result.tool_attempts == len(result.assessment.base.trace) + len(
        result.assessment.field_trace
    )
    assert result.assessment.field.disposition == expected
    assert result.assessment.field.basis_plan_id == basis.plan.plan_id
    assert result.assessment.field.basis_report_id == basis.result.report.report_id
    assert result.assessment.field.basis_report_revision == basis.result.report.revision
    assert result.assessment.field.basis_verification_level == level
    assert result.assessment.base.decisions[0].target_task_id == field[3].task_id
    assert CANARY not in str(result)
    assert contents(field[0]) == before
    assert [item["name"] for item in model.tool_specs[0]] == ["get_case_context"]
    field_schema = next(
        item["inputSchema"]["json"]
        for specs in model.tool_specs
        for item in specs
        if item["name"] == "stage_field_decision"
    )
    assert len(field_schema["required"]) == 16
    assert set(field_schema["required"]) == set(field_schema["properties"])
    if expected == "PROPOSE_FIELD_PLAN":
        assert result.assessment.field.proposal.task_id == field[3].task_id
        assert result.assessment.field.proposal.review_revision == field[3].revision
    else:
        assert result.assessment.field.proposal is None


def test_newer_other_review_result_does_not_replace_selected_review_basis(field, monkeypatch):
    from test_current_field_sdk_fixture import FieldResultScriptedModel

    verified(field, "COMPLETE")
    original = load(field).field_work.latest_results[0]
    at = NOW + timedelta(hours=1)
    other_review = field[1].stage_review(
        CASE,
        ReviewDraft(
            "OBSERVATION_REVIEW",
            "Review another observation",
            "A separate review for a controlled history test.",
            load(field).base.current.event_id,
        ),
        request_id="other-field-review",
        expected_case_revision=field[1].context(CASE)["revision"],
        now=at,
    )
    monkeypatch.setattr(field_fixtures, "NOW", at)
    monkeypatch.setattr(
        field_fixtures,
        "SPEC",
        replace(
            field_fixtures.SPEC,
            window_start=at,
            window_end=at + timedelta(hours=2),
        ),
    )
    original_mutate = field_fixtures.mutate

    def later_mutation(*args, **kwargs):
        kwargs.setdefault("now", at)
        return original_mutate(*args, **kwargs)

    monkeypatch.setattr(field_fixtures, "mutate", later_mutation)
    verified((field[0], field[1], field[2], other_review), "PARTIAL")
    context = load(field, at=at + timedelta(minutes=20))
    assert context.base.reviews[0].task_id == field[3].task_id
    assert context.field_work.latest_results[0].plan.task_id == other_review.task_id
    before = contents(field[0])
    model = FieldResultScriptedModel()
    result = planner(model).plan(context)
    assert result.assessment.field.disposition == "NO_NEW_FIELD_PLAN"
    assert result.assessment.field.basis_plan_id == original.plan.plan_id
    assert result.assessment.field.basis_report_id == original.result.report.report_id
    assert len(model.field_work_results) == 2
    assert contents(field[0]) == before


@pytest.mark.parametrize(
    "batch,attempts,trace_count",
    [
        ([], 0, 0),
        ([("unknown_tool", {})], 1, 0),
        ([("invalid name", {})], 1, 0),
        ([("inspect_field_work", {})], 1, 0),
        ([("get_case_context", {}), ("inspect_field_work", {})], 2, 0),
        ([("get_field_context", {})], 1, 0),
        ([("unknown_tool", {"canary": "SECRET-EXCEPTION-CANARY"})] * 17, 0, 0),
        ([("", {})], 0, 0),
    ],
)
def test_invalid_or_partial_turn_has_frozen_sanitized_failure(field, batch, attempts, trace_count):
    context = load(field)
    before = contents(field[0])
    with pytest.raises(CurrentTurnErrorV3) as caught:
        planner(BatchModel([batch])).plan(context)
    failure = caught.value.failure
    assert failure.context_digest == digest(_json(context))
    assert failure.case_id == context.base.case_id
    assert failure.case_revision == context.base.case_revision
    assert failure.tool_attempts == attempts
    assert len(failure.source_trace) + len(failure.field_trace) == trace_count
    expected_code = (
        "CURRENT_V3_TOOL_BUDGET_EXHAUSTED" if len(batch) > 16 else "CURRENT_V3_TURN_FAILED"
    )
    assert failure.code == expected_code
    assert "SECRET-EXCEPTION-CANARY" not in str(caught.value.evidence)
    assert CANARY not in str(caught.value.evidence)
    with pytest.raises(FrozenInstanceError):
        failure.status = "COMMITTED"
    caught.value.evidence["status"] = "COMMITTED"
    assert caught.value.evidence["status"] == "FAILED"
    assert contents(field[0]) == before


def test_model_response_budget_stops_before_ninth_response(field):
    model = BatchModel([[("unknown_tool", {})]] * 9)
    with pytest.raises(CurrentTurnErrorV3) as caught:
        planner(model, max_calls=8).plan(load(field))
    assert model.responses == 8
    assert caught.value.failure.model_calls == 8
    assert caught.value.failure.tool_attempts == 8


def test_timeout_cancels_model_and_preserves_case(field):
    class SlowModel(BatchModel):
        async def stream(self, *args, **kwargs):
            await asyncio.sleep(2)
            async for event in super().stream(*args, **kwargs):
                yield event

    before = contents(field[0])
    with pytest.raises(CurrentTurnErrorV3) as caught:
        planner(SlowModel([]), seconds=0.02).plan(load(field))
    assert caught.value.failure.model_calls == 1
    assert caught.value.failure.tool_attempts == 0
    assert contents(field[0]) == before
