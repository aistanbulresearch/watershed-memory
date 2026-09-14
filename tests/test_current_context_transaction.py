"""Reserve and replay the exact bounded source snapshot across later arrivals."""

from datetime import timedelta

import pytest
from test_current_case_store import CASE, LATER, NOW, action, query, stage
from test_current_case_store import ready as ready
from test_current_context import add_interval

from watershed_memory.current.context import _load_context, load_context


def test_pinned_context_does_not_change_when_a_late_prior_interval_arrives(ready):
    path, store, _, _ = ready
    current = add_interval(path, 45)
    evaluated = NOW + timedelta(hours=2)
    original = load_context(store, CASE, current.event_id, evaluated_at=evaluated)
    reserved_ids = tuple(item.event_id for item in original.prior)
    late = add_interval(path, 30)
    dynamic = load_context(store, CASE, current.event_id, evaluated_at=evaluated)
    assert dynamic.prior[0].event_id == late.event_id and dynamic != original
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        restored = _load_context(
            db, CASE, current.event_id, evaluated_at=evaluated, prior_event_ids=reserved_ids
        )
        assert restored == original
        assert db.in_transaction


def test_context_loader_reuses_caller_transaction_without_committing(ready):
    path, store, _, events = ready
    with store._connect() as db:
        with pytest.raises(ValueError):
            _load_context(db, CASE, events[1].event_id, evaluated_at=NOW)
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE current_cases SET revision=1 WHERE case_id=?", (CASE,))
        assert _load_context(db, CASE, events[1].event_id, evaluated_at=NOW).case_revision == 1
        assert query(path, "SELECT revision FROM current_cases") == [(0,)]
        db.rollback()
    assert load_context(store, CASE, events[1].event_id, evaluated_at=NOW).case_revision == 0


@pytest.mark.parametrize("kind", ["mutable", "duplicate", "oversized", "unknown", "self", "later"])
def test_pinned_prior_ids_are_validated_against_the_trusted_monitor(ready, kind):
    _, store, _, events = ready
    previous, current = events[0].event_id, events[1].event_id
    inputs = {
        "mutable": [previous],
        "duplicate": (previous, previous),
        "oversized": ("a" * 64, "b" * 64, "c" * 64, "d" * 64),
        "unknown": ("f" * 64,),
        "self": (current,),
        "later": (current,),
    }
    target = previous if kind == "later" else current
    with store._connect() as db:
        db.execute("BEGIN")
        with pytest.raises((ValueError, KeyError)):
            _load_context(db, CASE, target, evaluated_at=NOW, prior_event_ids=inputs[kind])


def test_pinned_correction_snapshot_must_retain_its_direct_ancestor(ready):
    path, store, _, events = ready
    correction = add_interval(path, 0, revision=2, supersedes=events[0].event_id)
    with store._connect() as db:
        db.execute("BEGIN")
        with pytest.raises(ValueError):
            _load_context(db, CASE, correction.event_id, evaluated_at=NOW, prior_event_ids=())


def test_reserved_context_restores_exact_work_revision_after_a_later_human_change(ready):
    _, store, _, events = ready
    first = stage(store, events[0])
    original = load_context(store, CASE, events[1].event_id, evaluated_at=NOW)
    references = tuple(
        (review.task_id, review.revision, ids)
        for review, (_, ids) in zip(original.reviews, original.review_evidence, strict=True)
    )
    action(
        store,
        first,
        "MODIFY",
        title="The new shift plan",
        next_check_at=LATER + timedelta(hours=1),
        now=LATER,
    )
    with store._connect() as db:
        db.execute("BEGIN")
        restored = _load_context(
            db,
            CASE,
            events[1].event_id,
            evaluated_at=NOW,
            prior_event_ids=tuple(item.event_id for item in original.prior),
            review_refs=references,
            reserved_revision=original.case_revision,
        )
    assert restored == original
    assert restored.reviews[0].title != store.get_review(CASE, first.task_id).title
