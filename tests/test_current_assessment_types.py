"""Immutable bounded staging records; these records do not authorize writes."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from watershed_memory.current.assessment_types import CurrentAssessment, ReviewDecision, ToolReceipt

EVENT, PRIOR, DIGEST = "a" * 64, "b" * 64, "c" * 64
NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)


def decision(**changes):
    return replace(
        ReviewDecision(
            "PROPOSE_REVIEW",
            "COVERAGE_REVIEW",
            EVENT,
            None,
            "Check missing observations",
            "Rain observations are missing.",
            NOW,
            (PRIOR,),
        ),
        **changes,
    )


def assessment(**changes):
    receipt = ToolReceipt("get_case_context", "{}", "{}")
    return replace(
        CurrentAssessment("current-case", 0, DIGEST, EVENT, (decision(),), (receipt,)), **changes
    )


def test_records_are_immutable_and_due_is_normalized_to_utc():
    item = decision(next_check_at=NOW.astimezone(timezone(timedelta(hours=3))))
    assert item.next_check_at.tzinfo == timezone.utc
    with pytest.raises(FrozenInstanceError):
        item.reason = "Changed reason"
    with pytest.raises(FrozenInstanceError):
        assessment().decisions = ()


@pytest.mark.parametrize(
    "changes",
    [
        {"disposition": "COMPLETE"},
        {"kind": "RESULT_VERIFICATION"},
        {"event_id": "z" * 64},
        {"event_id": 1},
        {"reason": "short"},
        {"reason": "a" * 701},
        {"reason": "Private\nnote"},
        {"title": ""},
        {"title": "a" * 121},
        {"target_task_id": "review-123"},
        {"next_check_at": NOW.replace(tzinfo=None)},
        {"next_check_at": "2026-09-12"},
        {"reference_ids": [PRIOR]},
        {"reference_ids": (PRIOR, PRIOR)},
        {"reference_ids": (EVENT,)},
        {"reference_ids": ("x" * 64,)},
        {"reference_ids": tuple(str(i) * 64 for i in range(4))},
    ],
)
def test_invalid_decisions_rejected(changes):
    with pytest.raises(ValueError):
        decision(**changes)


def test_continue_and_no_follow_up_have_no_hidden_plan_edits():
    continued = decision(
        disposition="CONTINUE_EXISTING_REVIEW",
        target_task_id="review-123",
        title=None,
        next_check_at=None,
    )
    assert continued.target_task_id == "review-123"
    for changes in ({"title": "Changed plan"}, {"next_check_at": NOW}, {"target_task_id": None}):
        with pytest.raises(ValueError):
            replace(continued, **changes)
    none = decision(disposition="NO_FOLLOW_UP", kind=None, title=None, next_check_at=None)
    for changes in (
        {"kind": "COVERAGE_REVIEW"},
        {"title": "Plan"},
        {"next_check_at": NOW},
        {"target_task_id": "review-123"},
    ):
        with pytest.raises(ValueError):
            replace(none, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"case_id": "bad case"},
        {"case_revision": True},
        {"case_revision": -1},
        {"case_revision": 2**63},
        {"policy_digest": "x" * 64},
        {"event_id": PRIOR},
        {"decisions": []},
        {"decisions": ()},
        {"decisions": (decision(), decision())},
        {"trace": []},
        {"trace": ()},
        {"trace": (ToolReceipt("get_case_context", "{}", "{}"),) * 13},
    ],
)
def test_invalid_assessment_records_rejected(changes):
    with pytest.raises(ValueError):
        assessment(**changes)


def test_two_distinct_kinds_allowed_but_no_follow_up_is_exclusive():
    assert (
        len(assessment(decisions=(decision(), decision(kind="OBSERVATION_REVIEW"))).decisions) == 2
    )
    none = decision(disposition="NO_FOLLOW_UP", kind=None, title=None, next_check_at=None)
    assert assessment(decisions=(none,)).decisions == (none,)
    with pytest.raises(ValueError):
        assessment(decisions=(none, decision()))


@pytest.mark.parametrize(
    "args",
    [
        ("execute_sql", "{}", "{}"),
        ("finish", "{}", "{}"),
        ("get_case_context", "[]", "{}"),
        ("get_case_context", "{}", "null"),
        ("get_case_context", '{"a": NaN}', "{}"),
        ("get_case_context", '{"a":1,"a":2}', "{}"),
        ("get_case_context", '{ "a":1}', "{}"),
        ("get_case_context", '{"b":1,"a":2}', "{}"),
        ("get_case_context", '{"a":"' + "x" * 4096 + '"}', "{}"),
        ("get_case_context", "{}", '{"a":"' + "x" * 32768 + '"}'),
    ],
)
def test_trace_requires_known_tool_and_bounded_canonical_json_objects(args):
    with pytest.raises(ValueError):
        ToolReceipt(*args)


@pytest.mark.parametrize(
    "field,depth",
    [("input_json", 1900), ("output_json", 10000), ("input_json", 16), ("output_json", 16)],
)
def test_nested_json_is_rejected_as_a_validation_error(field, depth):
    args = dict(name="get_case_context", input_json="{}", output_json="{}")
    args[field] = '{"a":' + "[" * depth + "0" + "]" * depth + "}"
    with pytest.raises(ValueError):
        ToolReceipt(**args)


def test_small_json_cannot_hide_an_unbounded_number_of_nodes():
    with pytest.raises(ValueError):
        ToolReceipt("get_case_context", "{}", '{"a":[' + ",".join("0" for _ in range(4096)) + "]}")
