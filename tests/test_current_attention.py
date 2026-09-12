"""Explicit synthetic review thresholds; no hydrologic safety thresholds or provider calls."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import ROUND_UP, Decimal, DefaultContext, localcontext

import pytest
from test_current_facts import END, event, inspect

from watershed_memory.current.attention import (
    AttentionPolicy,
    AttentionResult,
    AttentionRule,
    DueCheck,
    evaluate_attention,
)
from watershed_memory.current.case_types import ReviewRecord
from watershed_memory.current.context import CurrentContext
from watershed_memory.current.registry import gallinas_registry

RULE = AttentionRule("00060", "ft^3/s", "LATEST", Decimal("1"), Decimal("0.5"))
POLICY = AttentionPolicy("synthetic-attention-v1", (RULE,))


def ctx(*, before="3", current="4", review=None, previous_age=None):
    previous_event = event(values={"00060": before})
    current_event = event(start=END, values={"00060": current})
    previous = inspect(previous_event, evaluated_at=previous_age or END)
    latest = inspect(current_event, evaluated_at=END + timedelta(minutes=15))
    context = CurrentContext(
        "attention-fixture",
        0,
        "d" * 64,
        True,
        END + timedelta(minutes=15),
        latest,
        (previous,),
        (review,) if review else (),
        ((review.task_id, (previous.event_id,)),) if review else (),
        gallinas_registry(),
    )
    return context, previous


def test_explicit_policy_hash_covers_every_threshold_and_record_is_frozen():
    assert len(POLICY.policy_digest) == 64
    assert (
        replace(
            POLICY, rules=(replace(RULE, minimum_relative_change=Decimal("0.6")),)
        ).policy_digest
        != POLICY.policy_digest
    )
    with pytest.raises(FrozenInstanceError):
        RULE.unit = "in"


@pytest.mark.parametrize(
    "changes",
    [
        {"parameter_code": "bad"},
        {"unit": ""},
        {"metric": "AVERAGE"},
        {"minimum_absolute_change": Decimal("0")},
        {"minimum_absolute_change": 1},
        {"minimum_absolute_change": Decimal("NaN")},
        {"minimum_relative_change": Decimal("-1")},
        {"minimum_relative_change": Decimal("1001")},
    ],
)
def test_invalid_attention_settings_rejected(changes):
    with pytest.raises(ValueError):
        replace(RULE, **changes)


def test_empty_duplicate_or_mutable_policy_is_rejected():
    for rules in ((), [RULE], (RULE, RULE)):
        with pytest.raises(ValueError):
            AttentionPolicy("synthetic-attention-v1", rules)


def test_first_case_review_is_eligible_and_small_unchanged_condition_is_quiet():
    context, basis = ctx()
    initial = evaluate_attention(context, POLICY)
    assert initial.eligible and initial.reasons == ("INITIAL_REVIEW",)
    quiet = evaluate_attention(context, POLICY, basis=basis)
    assert not quiet.eligible and quiet.reasons == () and quiet.changed_parameters == ()


@pytest.mark.parametrize(
    "before,current,eligible",
    [
        ("3", "4.5", True),
        ("3", "4.499", False),
        ("10", "4", True),
        ("0", "1", True),
        ("0", "0.99", False),
        ("1", "-1", True),
    ],
)
def test_absolute_and_relative_change_are_both_required(before, current, eligible):
    context, basis = ctx(before=before, current=current)
    result = evaluate_attention(context, POLICY, basis=basis)
    assert result.eligible == eligible
    assert result.changed_parameters == (("00060",) if eligible else ())


def test_arithmetic_is_independent_of_ambient_decimal_rounding():
    context, basis = ctx(before="3", current="4.49999999999999999999")
    expected = evaluate_attention(context, POLICY, basis=basis)
    with localcontext() as decimal_context:
        decimal_context.prec = 1
        decimal_context.rounding = ROUND_UP
        actual = evaluate_attention(context, POLICY, basis=basis)
    assert actual == expected and not actual.eligible


def test_due_check_has_one_stable_key_for_each_human_plan_revision():
    review = ReviewRecord(
        "review-due",
        "attention-fixture",
        "OBSERVATION_REVIEW",
        "DEFERRED",
        2,
        "A human-chosen check",
        "Review the next available station update.",
        END + timedelta(minutes=10),
        True,
        END,
        END,
    )
    context, basis = ctx(review=review)
    due = evaluate_attention(context, POLICY, basis=basis)
    assert due.reasons == ("CHECK_DUE",) and len(due.due_checks) == 1
    assert due.due_checks[0] == DueCheck(review.task_id, review.revision, review.next_check_at)
    handled = evaluate_attention(
        context, POLICY, basis=basis, handled_due_keys=(due.due_checks[0].key,)
    )
    assert not handled.eligible
    assert replace(due.due_checks[0], revision=3).key != due.due_checks[0].key


def test_original_baseline_freshness_is_retained_for_aging_and_same_event_due_checks():
    context, _ = ctx()
    original = context.current
    aged = replace(original, freshness="STALE", stale_parameters=("00060", "00045", "63680"))
    context = replace(context, evaluated_at=context.evaluated_at + timedelta(hours=2), current=aged)
    result = evaluate_attention(context, POLICY, basis=original)
    assert result.reasons == ("COVERAGE_CHANGED",)


def test_unit_or_source_mismatch_is_rejected_instead_of_becoming_quiet():
    context, basis = ctx()
    bad = replace(POLICY, rules=(replace(RULE, unit="in"),))
    with pytest.raises(ValueError):
        evaluate_attention(context, bad, basis=basis)
    with pytest.raises(ValueError):
        evaluate_attention(context, POLICY, basis=replace(basis, monitor_id="unrelated"))


def test_peak_metric_can_surface_a_change_after_the_latest_value_has_fallen():
    context, basis = ctx(before="3", current="3")
    series = tuple(
        replace(item, max_value=Decimal("9")) if item.parameter_code == "00060" else item
        for item in context.current.series
    )
    context = replace(context, current=replace(context.current, series=series))
    policy = replace(POLICY, rules=(replace(RULE, metric="MAX"),))
    assert evaluate_attention(context, policy, basis=basis).reasons == ("MATERIAL_CHANGE",)
    assert not evaluate_attention(context, POLICY, basis=basis).eligible


def test_gradual_change_is_measured_against_the_retained_assessed_baseline():
    first, basis = ctx(before="3", current="4")
    second, _ = ctx(before="4", current="5")
    assert not evaluate_attention(first, POLICY, basis=basis).eligible
    assert evaluate_attention(second, POLICY, basis=basis).reasons == ("MATERIAL_CHANGE",)


def test_correction_of_the_assessed_event_is_eligible_without_erasing_its_basis():
    original_event = event()
    original = inspect(original_event)
    correction_event = event(revision=2, supersedes=original_event.event_id)
    corrected = inspect(correction_event)
    context = CurrentContext(
        "attention-fixture",
        0,
        "d" * 64,
        True,
        END,
        corrected,
        (original,),
        (),
        (),
        gallinas_registry(),
    )
    result = evaluate_attention(context, POLICY, basis=original)
    assert result.reasons == ("SOURCE_CORRECTION",)
    assert result.basis_event_id == original.event_id and original.revision == 1


@pytest.mark.parametrize("offset", [0, 15])
def test_unlinked_overlap_or_future_basis_is_rejected(offset):
    context, _ = ctx()
    source = event(start=END + timedelta(minutes=offset))
    unrelated = inspect(source, evaluated_at=END + timedelta(hours=1))
    with pytest.raises(ValueError):
        evaluate_attention(context, POLICY, basis=unrelated)


def test_linked_old_correction_compares_to_its_ancestor_not_a_later_baseline():
    original_event = event(values={"00060": "3"})
    correction = event(revision=2, supersedes=original_event.event_id, values={"00060": "4"})
    original, corrected = inspect(original_event), inspect(correction)
    later = inspect(
        event(start=END, values={"00060": "9"}), evaluated_at=END + timedelta(minutes=15)
    )
    review = ReviewRecord(
        "review-old",
        "attention-fixture",
        "OBSERVATION_REVIEW",
        "APPROVED",
        2,
        "Keep the chosen plan",
        "Review subsequent station evidence.",
        None,
        True,
        END,
        END,
    )
    context = CurrentContext(
        "attention-fixture",
        0,
        "d" * 64,
        True,
        END + timedelta(minutes=15),
        corrected,
        (original,),
        (review,),
        ((review.task_id, (original.event_id,)),),
        gallinas_registry(),
    )
    result = evaluate_attention(context, POLICY, basis=later)
    assert result.reasons == ("SOURCE_CORRECTION",)
    assert result.changed_parameters == () and result.basis_event_id == later.event_id
    with pytest.raises(ValueError):
        evaluate_attention(replace(context, reviews=(), review_evidence=()), POLICY, basis=later)
    foreign_policy = replace(context, prior=(replace(original, policy_id="foreign-policy"),))
    with pytest.raises(ValueError):
        evaluate_attention(foreign_policy, POLICY, basis=later)


def test_decimal_default_context_cannot_change_or_overflow_attention(monkeypatch):
    context, basis = ctx(before="3", current="40")
    expected = evaluate_attention(context, POLICY, basis=basis)
    monkeypatch.setattr(DefaultContext, "Emax", 0)
    monkeypatch.setattr(DefaultContext, "Emin", 0)
    monkeypatch.setattr(DefaultContext, "rounding", ROUND_UP)
    assert evaluate_attention(context, POLICY, basis=basis) == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"reasons": ({"mutable": "reason"},)},
        {"eligible": False},
        {"reasons": ("MATERIAL_CHANGE", "MATERIAL_CHANGE")},
        {"reasons": ("CHECK_DUE", "MATERIAL_CHANGE")},
        {"changed_parameters": ()},
        {"changed_parameters": ("00060", "00060")},
        {"changed_parameters": ("63680", "00060")},
        {"due_checks": ({"task_id": "mutable"},)},
    ],
)
def test_attention_result_rejects_inconsistent_unbounded_or_mutable_members(changes):
    result = AttentionResult(True, ("MATERIAL_CHANGE",), ("00060",), (), "d" * 64, "a" * 64)
    with pytest.raises(ValueError):
        replace(result, **changes)


def test_due_result_is_bounded_and_revision_fits_the_durable_ledger():
    due = DueCheck("review-due", 1, END)
    result = AttentionResult(True, ("CHECK_DUE",), (), (due,), "d" * 64, "a" * 64)
    for members in ((due, due), tuple(replace(due, task_id=f"review-{i}") for i in range(4))):
        with pytest.raises(ValueError):
            replace(result, due_checks=members)
    with pytest.raises(ValueError):
        replace(due, revision=2**63)
