"""Contradictory field partitions must fail before reference restoration."""

from dataclasses import replace

import pytest
from test_current_context_v3 import capture, load
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401
from test_current_field_store import verified


@pytest.mark.parametrize("flag", ["has_more_results", "has_more_stranded_plans"])
def test_present_field_context_cannot_claim_truncation_without_full_partition(field, flag):
    context = load(field)
    with pytest.raises(ValueError):
        replace(context.field_work, state="PRESENT", **{flag: True})


def test_stranded_partition_cannot_contain_a_reported_plan(field):
    verified(field)
    context = load(field)
    reported = replace(context.field_work.latest_results[0], parent_binding="TERMINAL")
    with pytest.raises(ValueError):
        replace(context.field_work, latest_results=(), stranded_plans=(reported,))


def test_empty_reference_cannot_carry_saved_activity(field):
    verified(field)
    reference = capture(field, load(field))
    with pytest.raises(ValueError):
        replace(reference, field_state="EMPTY")


def test_reference_parent_status_and_binding_must_agree(field):
    verified(field)
    reference = capture(field, load(field))
    with pytest.raises(ValueError):
        replace(reference.field_activities[0], parent_status="CANCELLED", parent_binding="CURRENT")
