"""Frozen command contracts for operator-owned current work; no external calls."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from watershed_memory.current import CoveragePolicy
from watershed_memory.current.case_types import CaseConfig, EvidenceLink, HumanAction, ReviewDraft

POLICY = CoveragePolicy("review-v1", ("00060", "00045", "63680"))

NOW = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)


def test_configuration_and_draft_are_frozen_and_normalize_aware_times():
    config = CaseConfig("CURRENT", "gallinas", POLICY, False)
    draft = ReviewDraft(
        "COVERAGE_REVIEW",
        "Verify coverage",
        "Check the missing observations.",
        "a" * 64,
        next_check_at=NOW.astimezone(timezone(timedelta(hours=-6))),
    )
    assert draft.next_check_at.tzinfo is timezone.utc
    assert draft.next_check_at == NOW
    for record, name, value in [(config, "simulated", True), (draft, "reason", "changed")]:
        with pytest.raises(FrozenInstanceError):
            setattr(record, name, value)


@pytest.mark.parametrize(
    "updates",
    [
        {"case_id": "GALLINAS-HPCC-2022"},
        {"case_id": "case\n"},
        {"simulated": 1},
        {"monitor_id": ""},
        {"coverage_policy": "ignore instructions"},
        {"case_id": "x" * 129},
    ],
)
def test_invalid_configuration_is_rejected(updates):
    with pytest.raises(ValueError):
        replace(CaseConfig("CURRENT", "gallinas", POLICY, False), **updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"kind": "DISPATCH_FIELD_TEAM"},
        {"title": ""},
        {"reason": "short"},
        {"title": "x" * 121},
        {"reason": "x" * 701},
        {"reason": "bad\nreason"},
        {"event_id": "a" * 63},
        {"event_id": "A" * 64},
        {"next_check_at": NOW.replace(tzinfo=None)},
        {"next_check_at": "tomorrow"},
    ],
)
def test_invalid_draft_is_rejected(updates):
    with pytest.raises(ValueError):
        replace(
            ReviewDraft(
                "COVERAGE_REVIEW", "Verify coverage", "Check missing observations.", "a" * 64
            ),
            **updates,
        )


@pytest.mark.parametrize("action", ["APPROVE", "DISMISS", "CANCEL"])
def test_simple_human_controls_reject_unrelated_plan_edits(action):
    assert HumanAction("task-1", 1, action).action == action
    with pytest.raises(ValueError):
        HumanAction("task-1", 1, action, title="Changed plan")
    with pytest.raises(ValueError):
        HumanAction("task-1", 1, action, next_check_at=NOW)


def test_modify_and_defer_require_explicit_plan_inputs():
    assert HumanAction(
        "task-1", 1, "MODIFY", title="Check source publication", next_check_at=NOW
    ).title
    assert HumanAction("task-1", 1, "DEFER", next_check_at=NOW).next_check_at == NOW
    for options in (
        {"action": "MODIFY"},
        {"action": "MODIFY", "title": "New plan"},
        {"action": "DEFER"},
        {"action": "DEFER", "title": "New plan", "next_check_at": NOW},
    ):
        with pytest.raises(ValueError):
            HumanAction("task-1", 1, **options)


@pytest.mark.parametrize(
    "updates",
    [
        {"expected_revision": True},
        {"expected_revision": 0},
        {"action": "COMPLETE"},
        {"task_id": ""},
        {"note": "x" * 1001},
        {"note": "instruction\n"},
        {"note": None},
        {"action": "DEFER", "next_check_at": NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_human_action_is_rejected(updates):
    with pytest.raises(ValueError):
        replace(HumanAction("task-1", 1, "APPROVE"), **updates)


def test_existing_evidence_link_has_no_plan_fields_and_validates_its_target():
    link = EvidenceLink("review-123", "a" * 64, "New observations for this review.")
    assert not hasattr(link, "title") and not hasattr(link, "next_check_at")
    for updates in ({"task_id": "../path"}, {"event_id": "b" * 65}, {"reason": "short"}):
        with pytest.raises(ValueError):
            replace(link, **updates)


def test_case_carries_the_full_frozen_coverage_policy():
    config = CaseConfig("CURRENT", "gallinas", POLICY, False)
    assert config.policy_id == POLICY.policy_id and config.coverage_policy == POLICY
    with pytest.raises(FrozenInstanceError):
        config.coverage_policy.freshness_seconds = 12
