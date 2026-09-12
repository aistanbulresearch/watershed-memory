"""Pure validation tests for the field-aware context DTOs."""

from datetime import datetime, timezone

import pytest

from watershed_memory.current.context_reference import ContextReference, encode_reference
from watershed_memory.current.context_v3_types import (
    ApprovedLocationReference,
    ContextReferenceV3,
    FieldActivityReference,
)

NOW = datetime(2026, 9, 12, 20, 11, tzinfo=timezone.utc)
HASH = "a" * 64


def base_reference():
    return encode_reference(
        ContextReference(
            "CASE-1",
            "a" * 64,
            4,
            NOW,
            (),
            (),
            None,
            HASH,
        )
    )


def activity(selection="CURRENT_PLAN"):
    return FieldActivityReference(
        selection,
        "plan-1",
        4,
        "request-1",
        "task-1",
        2,
        "PROPOSED",
        "CURRENT",
        HASH,
    )


def reference(**changes):
    values = dict(
        schema_version=3,
        base_reference_json=base_reference(),
        field_activities=(activity(),),
        approved_locations=(),
        field_state="PRESENT",
        has_more_stranded_plans=False,
        has_more_results=False,
        field_digest=HASH,
        context_digest=HASH,
    )
    values.update(changes)
    return ContextReferenceV3(**values)


def test_activity_and_location_references_are_frozen_and_bounded():
    item = activity()
    with pytest.raises((AttributeError, TypeError)):
        item.plan_id = "changed"
    assert reference().schema_version == 3


@pytest.mark.parametrize("selection", ["", "UNKNOWN", None, 1])
def test_activity_selection_is_strict(selection):
    with pytest.raises(ValueError):
        activity(selection)


@pytest.mark.parametrize("value", [True, -1, 2**63, 1.5, "4"])
def test_activity_revision_is_exact(value):
    with pytest.raises(ValueError):
        FieldActivityReference(
            "CURRENT_PLAN", "plan-1", value, "request-1", "task-1", 2, "PROPOSED", "CURRENT", HASH
        )


@pytest.mark.parametrize(
    "ids",
    [
        (
            ApprovedLocationReference("site-1", 1, HASH),
            ApprovedLocationReference("site-1", 1, HASH),
        ),
        (
            ApprovedLocationReference("site-2", 1, HASH),
            ApprovedLocationReference("site-1", 1, HASH),
        ),
    ],
)
def test_location_references_are_sorted_and_unique(ids):
    with pytest.raises(ValueError):
        reference(approved_locations=ids)


def test_reference_rejects_wrong_schema_or_duplicate_activity():
    with pytest.raises(ValueError):
        reference(schema_version=2)
    with pytest.raises(ValueError):
        reference(field_activities=(activity(), activity("STRANDED_PLAN")))


def test_reference_rejects_revision_after_base_and_untruthful_truncation():
    with pytest.raises(ValueError):
        reference(
            field_activities=(
                FieldActivityReference(
                    "CURRENT_PLAN",
                    "plan-1",
                    5,
                    "request-1",
                    "task-1",
                    2,
                    "PROPOSED",
                    "CURRENT",
                    HASH,
                ),
            )
        )
    with pytest.raises(ValueError):
        reference(field_state="EMPTY", has_more_stranded_plans=True)


def test_reference_rejects_unknown_parent_status_and_malformed_partition():
    with pytest.raises(ValueError):
        FieldActivityReference(
            "CURRENT_PLAN",
            "plan-1",
            4,
            "request-1",
            "task-1",
            2,
            "made-up",
            "CURRENT",
            HASH,
        )
    with pytest.raises(ValueError):
        reference(field_activities=[activity()])
