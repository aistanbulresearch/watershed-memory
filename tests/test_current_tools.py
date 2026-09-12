"""Current agent capabilities: relevant evidence, human plans, and staged work."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_case_store import CASE, LATER, MONITOR, NOW, action, query, stage
from test_current_case_store import ready as ready
from test_current_context import add_interval

from watershed_memory.current.assessment_types import ToolReceipt
from watershed_memory.current.context import load_context
from watershed_memory.current.tools import CurrentTools, validate_assessment


def context(ready):
    return load_context(ready[1], CASE, ready[3][1].event_id, evaluated_at=NOW)


def tools(ready):
    item = CurrentTools(context(ready))
    item.get_case_context()
    item.inspect_current_series()
    return item


def propose(item, current_event_id, **changes):
    args = dict(
        disposition="PROPOSE_REVIEW",
        kind="COVERAGE_REVIEW",
        event_id=current_event_id,
        target_task_id=None,
        title="Verify missing rain observations",
        reason="The current interval has no rain observations.",
        next_check_at=LATER.isoformat(),
        reference_ids=[],
    )
    args.update(changes)
    return item.stage_assessment(**args)


def test_current_read_is_precise_attributed_and_separate_from_all_history(ready):
    item = CurrentTools(context(ready))
    brief = item.get_case_context()
    assert brief["case_id"] == brief["source_origin_case_id"] == CASE
    assert brief["simulated"] is False and brief["case_revision"] == 0
    assert brief["available_prior_events"][0]["event_id"] == ready[3][0].event_id
    assert "latest_value" not in json.dumps(brief)
    current = item.inspect_current_series()
    assert current["interval_coverage"] == "DEGRADED" and current["freshness"] == "STALE"
    assert current["event_id"] == ready[3][1].event_id
    flow = next(x for x in current["series"] if x["parameter_code"] == "00060")
    assert flow["latest_value"] == "4" and flow["unit"] == "ft^3/s"
    assert current["evidence_class"] == "CURRENT_USGS_OBSERVATION"
    assert current["evaluated_at"] == NOW.isoformat()
    assert current["station_id"] == "USGS-08380500"
    assert current["missing_parameters"] == ["00045", "63680"]
    assert current["null_latest_parameters"] == [] and current["partial_window"] is False


def test_read_compare_and_new_review_stage_without_any_database_change(ready):
    path, store, watch, events = ready
    before = store.context(CASE), watch.status(MONITOR), query(path, "SELECT * FROM watch_outbox")
    item = tools(ready)
    comparison = item.compare_prior_event(events[0].event_id)
    assert comparison["prior"]["event_id"] == events[0].event_id
    assert comparison["prior"]["station_id"] == "USGS-08380500"
    assert comparison["prior"]["missing_parameters"] == ["00045", "63680"]
    flow = next(x for x in comparison["comparison"]["changes"] if x["parameter_code"] == "00060")
    assert flow["signed_delta"] == "1" and flow["ratio"] == "1.333333333333333333333333333"
    assert item.find_relevant_reviews("COVERAGE_REVIEW") == {
        "kind": "COVERAGE_REVIEW",
        "reviews": [],
    }
    staged = propose(item, events[1].event_id, reference_ids=[events[0].event_id])
    assert staged["status"] == "STAGED" and staged["committed"] is False
    result = item.finish()
    assert len(result.decisions) == 1 and len(result.trace) == 5
    assert validate_assessment(context(ready), result) == result
    assert before == (
        store.context(CASE),
        watch.status(MONITOR),
        query(path, "SELECT * FROM watch_outbox"),
    )
    assert query(path, "SELECT COUNT(*) FROM current_receipts") == [(0,)]


def test_existing_modified_or_deferred_plan_is_targeted_and_never_edited(ready):
    _, store, _, events = ready
    original = stage(store, events[0])
    modified = action(
        store,
        original,
        "MODIFY",
        title="Operator chosen station check",
        next_check_at=LATER,
        note="Private marker: keep out of model inputs",
    )
    deferred = action(store, modified, "DEFER", request="human-defer", next_check_at=LATER)
    item = tools(ready)
    found = item.find_relevant_reviews("COVERAGE_REVIEW")
    assert len(found["reviews"]) == 1
    assert found["reviews"][0]["status"] == "DEFERRED"
    assert found["reviews"][0]["revision"] == 3
    assert found["reviews"][0]["evidence_ids"] == [events[0].event_id]
    with pytest.raises(ValueError):
        propose(item, events[1].event_id)
    propose(
        item,
        events[1].event_id,
        disposition="CONTINUE_EXISTING_REVIEW",
        target_task_id=deferred.task_id,
        title=None,
        next_check_at=None,
    )
    result = item.finish()
    assert result.decisions[0].target_task_id == deferred.task_id
    assert "Private marker" not in str(result)
    assert store.get_review(CASE, deferred.task_id) == deferred
    assert validate_assessment(context(ready), result) == result


def test_missing_alternative_is_an_explicit_result_without_a_fabricated_fetch(ready):
    item = tools(ready)
    alternatives = item.inspect_alternate_sources("63680")
    assert alternatives == {
        "parameter_code": "63680",
        "status": "NO_COMPATIBLE_SOURCE_CONFIGURED",
        "sources": [],
    }
    with pytest.raises(ValueError):
        item.inspect_alternate_sources("99999")


def test_no_follow_up_is_first_class_and_does_not_create_work(ready):
    item = tools(ready)
    propose(
        item,
        ready[3][1].event_id,
        disposition="NO_FOLLOW_UP",
        kind=None,
        title=None,
        next_check_at=None,
        reason="The operator has no new review need for this interval.",
    )
    result = item.finish()
    assert result.decisions[0].disposition == "NO_FOLLOW_UP"
    assert validate_assessment(context(ready), result) == result
    with pytest.raises(ValueError):
        item.find_relevant_reviews("COVERAGE_REVIEW")
        propose(item, ready[3][1].event_id)


def test_two_independent_review_kinds_fit_one_bounded_assessment(ready):
    item = tools(ready)
    for kind in ("OBSERVATION_REVIEW", "COVERAGE_REVIEW"):
        item.find_relevant_reviews(kind)
        propose(item, ready[3][1].event_id, kind=kind)
    result = item.finish()
    assert len(result.decisions) == 2
    assert validate_assessment(context(ready), result) == result
    with pytest.raises(ValueError):
        propose(item, ready[3][1].event_id)


@pytest.mark.parametrize(
    "operation",
    [
        lambda t, e: t.inspect_current_series(),
        lambda t, e: t.compare_prior_event(e[0].event_id),
        lambda t, e: t.find_relevant_reviews("COVERAGE_REVIEW"),
        lambda t, e: t.inspect_alternate_sources("63680"),
        lambda t, e: propose(t, e[1].event_id),
        lambda t, e: t.finish(),
    ],
)
def test_required_reads_cannot_be_skipped(ready, operation):
    with pytest.raises(ValueError):
        operation(CurrentTools(context(ready)), ready[3])


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "f" * 64},
        {"reference_ids": ["f" * 64]},
        {"reference_ids": ["CURRENT_PRIOR"]},
        {"reference_ids": ("x" * 64,)},
        {"kind": "RESULT_VERIFICATION"},
        {"next_check_at": NOW.isoformat()},
        {"next_check_at": (NOW + timedelta(days=31)).isoformat()},
        {"next_check_at": "2026-09-12 13:30:00+00:00"},
        {"next_check_at": "2026-09-12T13:30:00"},
        {
            "disposition": "CONTINUE_EXISTING_REVIEW",
            "target_task_id": "unknown",
            "title": None,
            "next_check_at": None,
        },
    ],
)
def test_invalid_or_unread_decision_context_rejected(ready, changes):
    item = tools(ready)
    item.find_relevant_reviews("COVERAGE_REVIEW")
    if changes.get("reference_ids") == ["CURRENT_PRIOR"]:
        changes = {"reference_ids": [ready[3][0].event_id]}
    with pytest.raises(ValueError):
        propose(item, ready[3][1].event_id, **changes)


def test_kind_lookup_and_exact_existing_target_are_required(ready):
    item = tools(ready)
    with pytest.raises(ValueError):
        propose(item, ready[3][1].event_id)
    stage(ready[1], ready[3][0])
    item = tools(ready)
    item.find_relevant_reviews("COVERAGE_REVIEW")
    with pytest.raises(ValueError):
        propose(
            item,
            ready[3][1].event_id,
            disposition="CONTINUE_EXISTING_REVIEW",
            target_task_id="different-task",
            title=None,
            next_check_at=None,
        )


def test_complete_fresh_data_cannot_support_a_made_up_coverage_gap(ready):
    interval = add_interval(ready[0], 30)
    ctx = load_context(ready[1], CASE, interval.event_id, evaluated_at=interval.end)
    assert ctx.current.interval_coverage == "SUFFICIENT" and ctx.current.freshness == "FRESH"
    item = CurrentTools(ctx)
    item.get_case_context()
    item.inspect_current_series()
    item.find_relevant_reviews("COVERAGE_REVIEW")
    with pytest.raises(ValueError):
        propose(item, interval.event_id)


def test_unknown_or_current_event_is_not_an_allowlisted_prior(ready):
    item = tools(ready)
    for identity in ("f" * 64, ready[3][1].event_id):
        with pytest.raises(ValueError):
            item.compare_prior_event(identity)


def test_attempts_are_bounded_even_when_invalid_calls_keep_failing(ready):
    item = CurrentTools(context(ready))
    for _ in range(12):
        with pytest.raises(ValueError):
            item.inspect_current_series()
    with pytest.raises(RuntimeError):
        item.get_case_context()
    with pytest.raises(RuntimeError):
        item.finish()


def test_bad_arguments_count_toward_tool_attempt_budget(ready):
    item = CurrentTools(context(ready))
    for _ in range(12):
        with pytest.raises((ValueError, TypeError)):
            item.get_case_context(unexpected="bad")
    with pytest.raises(RuntimeError):
        item.get_case_context()


def test_exactly_twelve_attempts_are_valid_but_a_thirteenth_invalidates_the_turn(ready):
    item = tools(ready)
    item.find_relevant_reviews("COVERAGE_REVIEW")
    for _ in range(8):
        item.get_case_context()
    propose(item, ready[3][1].event_id)
    assert item.attempts == 12 and len(item.finish().trace) == 12
    assert validate_assessment(context(ready), item.finish()) == item.finish()
    with pytest.raises(RuntimeError):
        item.get_case_context()
    with pytest.raises(RuntimeError):
        item.finish()


def test_oversized_input_is_rejected_before_context_processing(ready):
    item = tools(ready)
    item.find_relevant_reviews("COVERAGE_REVIEW")
    before = item.trace
    with pytest.raises(ValueError):
        propose(item, ready[3][1].event_id, reason="a" * 100000)
    assert item.trace == before
    propose(item, ready[3][1].event_id)
    assert item.finish().decisions[0].reason == "The current interval has no rain observations."


def test_recursive_input_is_a_normal_rejection_and_does_not_poison_the_turn(ready):
    item = tools(ready)
    item.find_relevant_reviews("COVERAGE_REVIEW")
    nested = []
    for _ in range(10000):
        nested = [nested]
    with pytest.raises(ValueError, match="invalid tool request"):
        propose(item, ready[3][1].event_id, reference_ids=nested)
    assert item.attempts == 4 and len(item.trace) == 3
    propose(item, ready[3][1].event_id)
    assert len(item.finish().decisions) == 1


def test_returned_dict_mutation_cannot_change_private_context_or_trace(ready):
    item = tools(ready)
    returned = item.find_relevant_reviews("COVERAGE_REVIEW")
    returned["reviews"].append({"task_id": "forged"})
    staged = propose(item, ready[3][1].event_id)
    staged["decision"]["title"] = "Altered after return"
    assert item.finish().decisions[0].title == "Verify missing rain observations"
    assert validate_assessment(context(ready), item.finish()) == item.finish()


@pytest.mark.parametrize("change", ["output", "order", "input", "plan", "identity", "omit_read"])
def test_replay_rejects_tampered_trace_and_unearned_decisions(ready, change):
    item = tools(ready)
    item.find_relevant_reviews("COVERAGE_REVIEW")
    propose(item, ready[3][1].event_id)
    saved = item.finish()
    trace = list(saved.trace)
    if change == "output":
        trace[0] = replace(trace[0], output_json="{}")
    elif change == "order":
        trace[:2] = reversed(trace[:2])
    elif change == "input":
        trace[0] = replace(trace[0], input_json='{"unexpected":true}')
    elif change == "omit_read":
        trace = trace[1:]
    elif change == "plan":
        saved = replace(
            saved, decisions=(replace(saved.decisions[0], reason="A different rationale."),)
        )
    elif change == "identity":
        saved = replace(saved, case_revision=saved.case_revision + 1)
    saved = replace(saved, trace=tuple(trace))
    with pytest.raises(ValueError):
        validate_assessment(context(ready), saved)


def test_trace_tool_name_cannot_grant_arbitrary_method_access():
    with pytest.raises(ValueError):
        ToolReceipt("__init__", "{}", "{}")
