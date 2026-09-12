"""Quiet and assessed decisions retain an exact bounded replay recipe."""

import json
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import timedelta

import pytest
from test_current_case_store import CASE, NOW, action, stage
from test_current_case_store import ready as ready
from test_current_source_health import publish

from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.context import load_context
from watershed_memory.current.context_reference import (
    capture_context,
    decode_reference,
    encode_reference,
    restore_context,
)
from watershed_memory.current.tools import _json


def context(ready, *, health=True):
    return load_context(
        ready[1], CASE, ready[3][1].event_id, evaluated_at=NOW, include_source_health=health
    )


@pytest.mark.parametrize("health", [False, True])
def test_reference_round_trip_restores_exact_original_context_without_writes(ready, health):
    ctx = context(ready, health=health)
    reference = capture_context(ctx)
    assert (reference.source_version_ids is not None) == health
    assert decode_reference(encode_reference(reference)) == reference
    with ready[1]._connect() as db:
        before = db.total_changes
        with pytest.raises(ValueError):
            restore_context(db, reference)
        db.execute("BEGIN")
        assert restore_context(db, reference) == ctx
        assert db.in_transaction and db.total_changes == before


def test_reference_survives_new_human_plan_and_source_correction(ready):
    _, cases, watch, events = ready
    review = stage(cases, events[0])
    original = context(ready)
    recipe = capture_context(original)
    later = NOW + timedelta(minutes=5)
    action(
        cases,
        review,
        "MODIFY",
        title="Newer human plan",
        now=later,
        next_check_at=later + timedelta(hours=1),
        note="PRIVATE-RECIPE-CANARY",
    )
    publish(watch, later, observed_at=original.source_health.series[0].observed_at, value="17")
    restored_recipe = decode_reference(encode_reference(recipe))
    with cases._connect() as db:
        db.execute("BEGIN")
        assert restore_context(db, restored_recipe) == original
    assert "PRIVATE-RECIPE-CANARY" not in encode_reference(recipe)
    assert "Newer human plan" not in encode_reference(recipe)


@pytest.mark.parametrize(
    "changes",
    [
        {"context_digest": "f" * 64},
        {"prior_event_ids": ()},
        {"source_version_ids": (None, None, None)},
    ],
)
def test_validly_typed_but_changed_replay_recipe_is_rejected(ready, changes):
    original = capture_context(context(ready))
    modified = replace(original, **changes)
    with ready[1]._connect() as db:
        db.execute("BEGIN")
        with pytest.raises((ValueError, WorkflowConflict)):
            restore_context(db, modified)


@pytest.mark.parametrize(
    "changes",
    [
        {"case_id": "\n"},
        {"case_revision": True},
        {"case_revision": -1},
        {"evaluated_at": NOW.replace(tzinfo=None)},
        {"prior_event_ids": []},
        {"prior_event_ids": ("bad",)},
        {"review_refs": []},
        {"review_refs": (("task", 0, ()),)},
        {"review_refs": (("task", 1, ("f" * 64,)),)},
        {"source_version_ids": ()},
        {"source_version_ids": (True,)},
        {"source_version_ids": (1, 1)},
        {"source_version_ids": (2**63,)},
        {"context_digest": "invalid"},
    ],
)
def test_internal_reference_rejects_mutable_or_out_of_scope_values(ready, changes):
    with pytest.raises(ValueError):
        replace(capture_context(context(ready)), **changes)


@pytest.mark.parametrize(
    "mutation", ["unknown", "missing", "duplicate", "pretty", "bool", "nested"]
)
def test_reference_decoder_requires_exact_canonical_json_shape(ready, mutation):
    raw = encode_reference(capture_context(context(ready)))
    data = json.loads(raw)
    if mutation == "unknown":
        data["unexpected"] = True
    elif mutation == "missing":
        del data["case_id"]
    elif mutation == "bool":
        data["case_revision"] = True
    elif mutation == "nested":
        data["review_refs"] = [None]
    if mutation == "duplicate":
        raw = '{"case_id":"duplicate",' + raw[1:]
    elif mutation == "pretty":
        raw = json.dumps(data, indent=2)
    else:
        raw = _json(data)
    with pytest.raises(ValueError):
        decode_reference(raw)


def test_reference_is_immutable_and_bounded(ready):
    recipe = capture_context(context(ready))
    with pytest.raises(FrozenInstanceError):
        recipe.case_id = "other"
    with pytest.raises(ValueError):
        capture_context(asdict(context(ready)))
    with pytest.raises(ValueError):
        decode_reference('{"case_id":"' + "x" * 8192 + '"}')


@pytest.mark.parametrize(
    "field,value",
    [
        ("prior_event_ids", {}),
        ("prior_event_ids", ""),
        ("review_refs", {}),
        ("review_refs", ""),
    ],
)
def test_empty_nonarrays_are_not_silently_normalized_into_valid_recipe_arrays(ready, field, value):
    data = json.loads(encode_reference(capture_context(context(ready))))
    data[field] = value
    with pytest.raises(ValueError):
        decode_reference(_json(data))


def test_extra_review_reference_elements_are_not_silently_dropped(ready):
    review = stage(ready[1], ready[3][0])
    data = json.loads(encode_reference(capture_context(context(ready))))
    assert data["review_refs"][0][0] == review.task_id
    data["review_refs"][0].append("unexpected")
    with pytest.raises(ValueError):
        decode_reference(_json(data))


def test_unhashable_work_evidence_is_rejected_as_invalid_input(ready):
    recipe = capture_context(context(ready))
    with pytest.raises(ValueError):
        replace(recipe, review_refs=(("task", 1, ([],)),))
