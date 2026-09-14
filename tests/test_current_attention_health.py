"""Selective attention uses current publication health alongside interval coverage."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_current_case_store import CONFIG, NOW
from test_current_case_store import ready as ready
from test_current_source_health import publish

from watershed_memory.current.attention import AttentionPolicy, AttentionRule, evaluate_attention
from watershed_memory.current.context import load_context

POLICY = AttentionPolicy(
    "attention-health-test",
    (AttentionRule("00060", "ft^3/s", "LATEST", Decimal("10"), Decimal("0")),),
)


def setup_case(ready):
    cfg = replace(
        CONFIG,
        case_id="attention-live",
        simulated=True,
        coverage_policy=replace(CONFIG.coverage_policy, required_parameters=("00060",)),
    )
    ready[1].register(cfg, now=NOW)
    return cfg.case_id


def context(ready, case, index=1, at=NOW):
    return load_context(
        ready[1], case, ready[3][index].event_id, evaluated_at=at, include_source_health=True
    )


def evaluate(ctx, before):
    return evaluate_attention(ctx, POLICY, basis=before.current, basis_health=before.source_health)


def test_initial_health_enabled_context_requires_no_invented_baseline(ready):
    ctx = context(ready, setup_case(ready))
    assert evaluate_attention(ctx, POLICY).reasons == ("INITIAL_REVIEW",)


def test_old_queued_interval_age_does_not_interrupt_a_fresh_station(ready):
    case = setup_case(ready)
    before = context(ready, case, index=0)
    later = NOW + timedelta(minutes=70)
    publish(ready[2], later, observed_at=later - timedelta(minutes=5))
    current = context(ready, case, at=later)
    assert current.current.freshness == "STALE" and before.current.freshness == "FRESH"
    result = evaluate(current, before)
    assert not result.eligible and result.reasons == ()
    assert result.basis_event_id == before.current.event_id


def test_real_source_age_change_triggers_once_against_assessed_health(ready):
    case = setup_case(ready)
    before = context(ready, case)
    stale = context(ready, case, at=NOW + timedelta(minutes=51))
    assert evaluate(stale, before).reasons == ("COVERAGE_CHANGED",)
    unchanged = context(ready, case, at=NOW + timedelta(minutes=52))
    assert not evaluate(unchanged, stale).eligible


def test_fresh_publication_recovery_is_visible_without_a_new_sealed_interval(ready):
    case = setup_case(ready)
    later = NOW + timedelta(minutes=70)
    stale = context(ready, case, at=later)
    publish(ready[2], later, observed_at=later - timedelta(minutes=5))
    restored = context(ready, case, at=later)
    assert restored.current.event_id == stale.current.event_id
    assert evaluate(restored, stale).reasons == ("COVERAGE_CHANGED",)


def test_null_latest_publication_does_not_disappear_behind_old_numeric_interval(ready):
    case = setup_case(ready)
    before = context(ready, case)
    later = NOW + timedelta(minutes=5)
    publish(ready[2], later, value=None)
    current = context(ready, case, at=later)
    assert current.current.null_latest_parameters == ()
    assert current.source_health.null_parameters == ("00060",)
    assert evaluate(current, before).reasons == ("COVERAGE_CHANGED",)


@pytest.mark.parametrize("kind", ["missing", "type", "case", "policy", "future", "pre_interval"])
def test_health_baseline_must_match_case_policy_and_assessment_time(ready, kind):
    case = setup_case(ready)
    ctx = context(ready, case)
    old = ctx.source_health
    if kind == "missing":
        old = None
    elif kind == "type":
        old = {}
    elif kind == "case":
        old = replace(old, case_id="other")
    elif kind == "policy":
        old = replace(old, policy=replace(old.policy, freshness_seconds=12))
    elif kind == "future":
        old = replace(old, evaluated_at=NOW + timedelta(seconds=1))
    else:
        # A legitimate all-missing snapshot at an earlier time cannot be the
        # source state from a later interval assessment.
        empty = tuple(
            replace(
                r,
                version_id=None,
                semantic_hash=None,
                observed_at=None,
                value=None,
                approval_status=None,
                qualifier=None,
                source_modified_at=None,
                retrieved_at=None,
            )
            for r in old.series
        )
        old = replace(old, series=empty, evaluated_at=NOW - timedelta(minutes=1))
    with pytest.raises(ValueError):
        evaluate_attention(ctx, POLICY, basis=ctx.current, basis_health=old)


def test_no_health_baseline_is_allowed_for_initial_or_legacy_context(ready):
    case = setup_case(ready)
    ctx = context(ready, case)
    with pytest.raises(ValueError):
        evaluate_attention(ctx, POLICY, basis_health=ctx.source_health)
    with pytest.raises(ValueError):
        evaluate_attention(
            replace(ctx, source_health=None),
            POLICY,
            basis=ctx.current,
            basis_health=ctx.source_health,
        )
