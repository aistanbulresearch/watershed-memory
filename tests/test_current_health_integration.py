"""Source freshness stays pinned throughout a current decision and its replay."""

import json
from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from test_current_case_store import CASE, CONFIG, MONITOR, NOW, action, query, stage
from test_current_case_store import ready as ready
from test_current_delivery_store import PROFILE, execution, reserve
from test_current_delivery_store import journal as journal
from test_current_source_health import publish
from test_strands import ScriptedModel

from watershed_memory.current.case_records import digest
from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.context import load_context
from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.strands import CurrentExecution, CurrentStrandsPlanner
from watershed_memory.current.tools import CurrentTools, _json, validate_assessment

HEALTH_PROFILE = replace(PROFILE, instruction_version="watershed-current-v2")


def context(ready, *, at=NOW, case=CASE, event=None):
    return load_context(
        ready[1],
        case,
        event or ready[3][1].event_id,
        evaluated_at=at,
        include_source_health=True,
    )


def stage_decision(tools, ctx, kind=None):
    if kind:
        tools.find_relevant_reviews(kind)
    return tools.stage_assessment(
        "PROPOSE_REVIEW" if kind else "NO_FOLLOW_UP",
        kind,
        ctx.current.event_id,
        None,
        "Verify source coverage" if kind else None,
        "Synthetic check of station publication and the selected interval.",
        None,
        [],
    )


def outcome(ticket):
    tools = CurrentTools(ticket.context)
    tools.get_case_context()
    tools.inspect_current_series()
    tools.inspect_source_health()
    stage_decision(tools, ticket.context)
    profile = ticket.profile
    return CurrentExecution(
        tools.finish(),
        profile.mode,
        profile.model_id,
        profile.instruction_version,
        profile.sdk_version,
        1,
        tools.attempts,
        0.1,
        "{}",
        "end_turn",
    )


def test_health_is_explicit_and_legacy_context_serialization_is_unchanged(ready):
    old = load_context(ready[1], CASE, ready[3][1].event_id, evaluated_at=NOW)
    assert old.source_health is None and "source_health" not in json.loads(_json(old))
    assert context(ready).source_health.version_ids[0] is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"case_id": "OTHER"},
        {"monitor_id": "other"},
        {"station_id": "USGS-12345"},
        {"source_id": "other"},
        {"evaluated_at": NOW + timedelta(seconds=1)},
        {"policy": replace(CONFIG.coverage_policy, freshness_seconds=1)},
    ],
)
def test_context_rejects_mismatched_source_capability(ready, changes):
    ctx = context(ready)
    with pytest.raises(ValueError):
        replace(ctx, source_health=replace(ctx.source_health, **changes))


def test_health_read_is_required_before_staging_and_replay_is_exact(ready):
    ctx = context(ready)
    tools = CurrentTools(ctx)
    with pytest.raises(ValueError):
        tools.inspect_source_health()
    tools.get_case_context()
    tools.inspect_current_series()
    with pytest.raises(ValueError):
        stage_decision(tools, ctx)
    snapshot = tools.inspect_source_health()
    assert snapshot["missing_parameters"] == ["00045", "63680"]
    assert snapshot["stale_parameters"] == snapshot["null_parameters"] == []
    assert snapshot["series"][0]["value"] == "4"
    snapshot["series"][0]["value"] = "forged"
    stage_decision(tools, ctx)
    assessed = tools.finish()
    assert validate_assessment(ctx, assessed) == assessed
    assert "forged" not in str(assessed)
    assert any(t.name == "inspect_source_health" for t in assessed.trace)


def test_legacy_context_cannot_access_unreserved_latest_data(ready):
    ctx = load_context(ready[1], CASE, ready[3][1].event_id, evaluated_at=NOW)
    tools = CurrentTools(ctx)
    tools.get_case_context()
    tools.inspect_current_series()
    with pytest.raises(ValueError):
        tools.inspect_source_health()
    stage_decision(tools, ctx)
    assert validate_assessment(ctx, tools.finish())


def test_complete_old_interval_with_fresh_station_has_no_coverage_gap(ready):
    _, cases, watch, events = ready
    config = replace(
        CONFIG,
        case_id="flow-only",
        simulated=True,
        coverage_policy=replace(CONFIG.coverage_policy, required_parameters=("00060",)),
    )
    cases.register(config, now=NOW)
    later = NOW + timedelta(minutes=70)
    publish(watch, later, observed_at=later - timedelta(minutes=5))
    ctx = context(ready, at=later, case=config.case_id, event=events[0].event_id)
    assert ctx.current.freshness == "STALE" and ctx.current.interval_coverage == "SUFFICIENT"
    assert not ctx.source_health.stale_parameters
    tools = CurrentTools(ctx)
    tools.get_case_context()
    tools.inspect_current_series()
    tools.inspect_source_health()
    with pytest.raises(ValueError):
        stage_decision(tools, ctx, "COVERAGE_REVIEW")
    stage_decision(tools, ctx)
    assert tools.finish().decisions[0].disposition == "NO_FOLLOW_UP"


def test_missing_interval_or_latest_source_staleness_can_still_warrant_review(ready):
    for at in (NOW, NOW + timedelta(hours=2)):
        ctx = context(ready, at=at)
        tools = CurrentTools(ctx)
        tools.get_case_context()
        tools.inspect_current_series()
        tools.inspect_source_health()
        stage_decision(tools, ctx, "COVERAGE_REVIEW")
        assert tools.finish().decisions[0].kind == "COVERAGE_REVIEW"


def test_journal_preserves_original_health_after_correction_and_fresh_process(ready, journal):
    path, cases, watch, _ = ready
    ticket = reserve(journal, ready, include_source_health=True, profile=HEALTH_PROFILE)
    result = outcome(ticket)
    later = NOW + timedelta(minutes=5)
    publish(watch, later, observed_at=ticket.context.source_health.series[0].observed_at, value="7")
    assert (
        context(ready, at=later).source_health.series[0].value
        != ticket.context.source_health.series[0].value
    )
    saved = DeliveryStore(CaseStore(path)).commit(ticket.attempt_id, result, now=later)
    assert saved.status == "COMMITTED"
    trace = json.loads(saved.execution_json)["assessment"]["trace"]
    source = next(t for t in trace if t["name"] == "inspect_source_health")
    assert json.loads(source["output_json"])["series"][0]["value"] == "4"
    assert journal.allowance().provider_used == 1
    assert query(path, "SELECT COUNT(*) FROM health_attempts") == [(1,)]
    assert (
        reserve(journal, ready, include_source_health=True, profile=HEALTH_PROFILE, now=later)
        == saved
    )
    with pytest.raises(WorkflowConflict):
        reserve(journal, ready, profile=HEALTH_PROFILE, now=later)


def test_legacy_journal_still_replays_without_health_extension(ready, journal):
    path, _, _, _ = ready
    ticket = reserve(journal, ready)
    legacy = asdict(ticket.context)
    legacy.pop("source_health")
    assert query(path, "SELECT context_digest64 FROM delivery_attempts") == [
        (digest(_json(legacy)),)
    ]
    assert query(path, "SELECT name FROM sqlite_master WHERE name GLOB 'health_*'") == []
    assert journal.commit(ticket.attempt_id, execution(ticket), now=NOW).status == "COMMITTED"


@pytest.mark.parametrize("corrupt", ["missing", "references", "schema"])
def test_damaged_health_pin_never_saves_work_or_acknowledges_source(ready, journal, corrupt):
    path, cases, watch, _ = ready
    ticket = reserve(journal, ready, include_source_health=True, profile=HEALTH_PROFILE)
    result = outcome(ticket)
    before = cases.context(CASE), watch.status(MONITOR)
    with cases._connect() as db:
        if corrupt == "missing":
            db.execute("DELETE FROM health_attempts")
        elif corrupt == "references":
            db.execute("UPDATE health_attempts SET version_ids_json='[null,null,null]'")
        else:
            db.execute("UPDATE health_schema_version SET version=99")
    with pytest.raises((ValueError, KeyError, WorkflowConflict)):
        journal.commit(ticket.attempt_id, result, now=NOW)
    assert before == (cases.context(CASE), watch.status(MONITOR))
    assert journal.get(CASE, ticket.request_id).status == "RESERVED"


def test_new_health_schema_and_reservation_rollback_together(ready, journal, monkeypatch):
    path, cases, _, _ = ready
    import watershed_memory.current.delivery_store as module

    original = module.DeliveryReservation

    def rejected(*args, **kwargs):
        raise RuntimeError("injected final construction failure")

    monkeypatch.setattr(module, "DeliveryReservation", rejected)
    with pytest.raises(RuntimeError):
        reserve(journal, ready, include_source_health=True, profile=HEALTH_PROFILE)
    monkeypatch.setattr(module, "DeliveryReservation", original)
    assert query(path, "SELECT name FROM sqlite_master WHERE name GLOB 'health_*'") == []
    assert journal.allowance().provider_used == 0 and cases.context(CASE)["revision"] == 0


def test_human_change_still_validates_original_source_health_before_stale_receipt(ready, journal):
    _, cases, watch, events = ready
    review = stage(cases, events[0])
    ticket = reserve(journal, ready, include_source_health=True, profile=HEALTH_PROFILE)
    result = outcome(ticket)
    later = NOW + timedelta(minutes=5)
    human = action(
        cases,
        review,
        "MODIFY",
        title="Retain the operator plan",
        now=later,
        next_check_at=later + timedelta(hours=1),
    )
    publish(watch, later, value="8")
    assert journal.commit(ticket.attempt_id, result, now=later).status == "STALE"
    assert cases.get_review(CASE, review.task_id) == human
    assert watch.status(MONITOR)["pending_count"] > 0


def test_health_sdk_uses_seventh_tool_and_v2_instruction_within_existing_budget(ready):
    ctx = context(ready)
    call = {
        "disposition": "NO_FOLLOW_UP",
        "kind": None,
        "event_id": ctx.current.event_id,
        "target_task_id": None,
        "title": None,
        "reason": "Synthetic SDK publication check.",
        "next_check_at": None,
        "reference_ids": [],
    }
    model = ScriptedModel(
        [
            ("get_case_context", {}),
            ("inspect_current_series", {}),
            ("inspect_source_health", {}),
            ("stage_assessment", call),
        ]
    )
    result = CurrentStrandsPlanner(model, model_id="health-scripted", scripted_test=True).plan(ctx)
    assert result.instruction_version == "watershed-current-v2"
    assert result.model_calls == 5 and result.tool_attempts == 4
    assert validate_assessment(ctx, result.assessment) == result.assessment
