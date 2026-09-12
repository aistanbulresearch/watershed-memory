"""Field memory is exact, bounded and recoverable after later human changes."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_case_store import HumanAction
from test_current_field_store import (
    CANARY,
    CASE,
    NOW,
    OPERATOR,
    SITE,
    SPEC,
    attached,
    contents,
    mutate,
    proposed,
    verified,
)
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.context import load_context
from watershed_memory.current.context_reference import (
    capture_context,
    encode_reference,
    restore_context,
)
from watershed_memory.current.context_v3 import load_context_v3
from watershed_memory.current.context_v3_reference import (
    capture_context_v3,
    decode_reference_v3,
    encode_reference_v3,
    restore_context_v3,
)
from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.field_types import (
    AttachFieldEvidence,
    CorrectFieldResult,
    DecideFieldPlan,
    ProposeFieldPlan,
    ReportFieldResult,
)
from watershed_memory.current.locations import LocationRegistry


def event_id(field):
    with field[1]._connect() as db:
        return db.execute(
            "SELECT event_id FROM watch_events ORDER BY interval_end DESC LIMIT 1"
        ).fetchone()[0]


def load(field, at=NOW + timedelta(minutes=20)):
    return load_context_v3(field[1], field[2], CASE, event_id(field), evaluated_at=at)


def capture(field, context):
    with field[1]._connect() as db:
        db.execute("BEGIN")
        return capture_context_v3(db, field[2], context)


def restore(field, reference, fields=None):
    with field[1]._connect() as db:
        db.execute("BEGIN")
        return restore_context_v3(db, fields or field[2], reference)


def test_empty_field_context_preserves_base_and_has_no_writes(field):
    before = contents(field[0])
    value = load(field)
    assert value.base == load_context(
        field[1],
        CASE,
        event_id(field),
        evaluated_at=NOW + timedelta(minutes=20),
        include_source_health=True,
    )
    assert value.field_work.state == "EMPTY"
    assert value.field_work.current_plans == value.field_work.stranded_plans == ()
    assert value.field_work.latest_results == ()
    assert value.field_work.approved_locations == (SITE,)
    reference = capture(field, value)
    assert restore(field, decode_reference_v3(encode_reference_v3(reference))) == value
    assert contents(field[0]) == before


def test_verified_result_is_exact_private_notes_are_not_retrieved(field):
    saved = verified(field)
    value = load(field)
    assert value.field_work.state == "PRESENT"
    assert value.field_work.latest_results[0].result == saved.result
    assert value.field_work.latest_results[0].field_dimension == "VERIFIED_COMPLETE"
    assert value.field_work.current_plans == ()
    assert CANARY not in str(value)
    reference = capture(field, value)
    assert CANARY not in encode_reference_v3(reference)
    assert restore(field, reference) == value


def test_reserved_result_and_parent_survive_correction_and_withdrawn_location(field):
    original = verified(field)
    value = load(field)
    reference = capture(field, value)
    mutate(
        field,
        "correct_result",
        CorrectFieldResult(
            original.result.report.report_id,
            1,
            "PARTIAL",
            "The outer marker was inaccessible.",
            NOW,
            NOW + timedelta(minutes=10),
            True,
        ),
        who=OPERATOR,
        now=NOW + timedelta(minutes=21),
    )
    review = field[1].get_review(CASE, field[3].task_id)
    field[1].act(
        CASE,
        HumanAction(review.task_id, review.revision, "CANCEL"),
        request_id="close-parent",
        now=NOW + timedelta(minutes=22),
    )
    withdrawn = replace(
        SITE, revision=2, status="WITHDRAWN", recorded_at=NOW + timedelta(minutes=22)
    )
    changed_fields = FieldStore(field[1], LocationRegistry((SITE, withdrawn)))
    before = contents(field[0])
    assert restore(field, reference, changed_fields) == value
    fresh = load_context_v3(
        field[1], changed_fields, CASE, event_id(field), evaluated_at=NOW + timedelta(minutes=23)
    )
    assert fresh.field_work.latest_results[0].result.report.outcome == "PARTIAL"
    assert fresh.field_work.latest_results[0].result.verification_level == "REPORTED"
    assert fresh.field_work.latest_results[0].parent_binding == "TERMINAL"
    assert fresh.field_work.approved_locations == ()
    assert contents(field[0]) == before


def test_evidence_pin_does_not_expand_when_later_reference_is_attached(field):
    first = attached(field)
    value = load(field)
    reference = capture(field, value)
    mutate(
        field,
        "attach_evidence",
        AttachFieldEvidence(
            first.result.report.report_id,
            1,
            "OPERATOR_RECORD_REFERENCE",
            "INSPECTION_RECORD_REFERENCE",
            "later-record",
            "Demonstration later inspection record.",
            NOW,
            None,
            True,
        ),
        who=OPERATOR,
        now=NOW + timedelta(minutes=21),
    )
    assert (
        len(load(field, NOW + timedelta(minutes=22)).field_work.latest_results[0].result.evidence)
        == 2
    )
    assert restore(field, reference) == value
    assert len(restore(field, reference).field_work.latest_results[0].result.evidence) == 1


def test_capture_refuses_context_loaded_before_field_mutation(field):
    value = load(field, NOW)
    proposed(field)
    before = contents(field[0])
    with pytest.raises(WorkflowConflict):
        capture(field, value)
    assert contents(field[0]) == before


def test_legacy_v1_v2_references_restore_unchanged_after_field_work(field):
    for health in (False, True):
        old = load_context(
            field[1], CASE, event_id(field), evaluated_at=NOW, include_source_health=health
        )
        ref = capture_context(old)
        raw = encode_reference(ref)
        if not health:
            legacy = old, ref, raw
        else:
            current = old, ref, raw
    proposed(field)
    for old, ref, raw in (legacy, current):
        with field[1]._connect() as db:
            db.execute("BEGIN")
            assert restore_context(db, ref) == old
        assert encode_reference(ref) == raw
    with pytest.raises(WorkflowConflict, match="context contract v3"):
        load_context(field[1], CASE, event_id(field), evaluated_at=NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "missing-receipt"},
        {"plan_id": "unrelated-plan"},
        {"parent_revision": 99},
        {"parent_status": "CANCELLED", "parent_binding": "TERMINAL"},
        {"snapshot_digest": "a" * 64},
    ],
)
def test_changed_field_reference_fails_without_writing(field, change):
    verified(field)
    reference = capture(field, load(field))
    before = contents(field[0])
    with pytest.raises((ValueError, KeyError, WorkflowConflict)):
        altered = replace(
            reference, field_activities=(replace(reference.field_activities[0], **change),)
        )
        restore(field, altered)
    assert contents(field[0]) == before


def test_empty_canonical_reference_has_strict_shape_and_bound(field):
    raw = encode_reference_v3(capture(field, load(field)))
    for invalid in (
        raw + " ",
        raw.replace('"schema_version":3', '"schema_version":true'),
        raw.replace('"schema_version":3', '"schema_version":3,"schema_version":3'),
        '{"extra":0,' + raw[1:],
        " " * 32769,
    ):
        with pytest.raises(ValueError):
            decode_reference_v3(invalid)


def test_context_requires_same_store_and_transaction(field, ready):
    value = load(field)
    equivalent = CaseStore(field[0])
    with pytest.raises(ValueError):
        load_context_v3(equivalent, field[2], CASE, event_id(field), evaluated_at=NOW)
    with field[1]._connect() as db:
        with pytest.raises(ValueError):
            capture_context_v3(db, field[2], value)
    # A separate valid database cannot be substituted for the bound store.
    other_path = field[0].parent / "other.sqlite"
    with field[1]._connect() as db:
        import sqlite3

        with sqlite3.connect(other_path) as target:
            db.backup(target)
    other = CaseStore(other_path)
    with other._connect() as db:
        db.execute("BEGIN")
        with pytest.raises(ValueError):
            capture_context_v3(db, field[2], value)


def test_latest_results_are_bounded_and_follow_activity_revision(field):
    saved = []
    for index in range(4):
        proposal = mutate(
            field,
            "propose_plan",
            ProposeFieldPlan(field[3].task_id, field[3].revision, SITE.location_id, 1, SPEC, True),
        )
        approved = mutate(
            field, "decide_plan", DecideFieldPlan(proposal.plan.plan_id, 1, "APPROVE")
        )
        result = mutate(
            field,
            "report_result",
            ReportFieldResult(
                approved.plan.plan_id,
                2,
                "PARTIAL",
                f"Demonstration partial inspection {index}.",
                NOW,
                NOW,
                True,
            ),
            who=OPERATOR,
        )
        saved.append(result.plan.plan_id)
    value = load(field, NOW)
    assert [item.plan.plan_id for item in value.field_work.latest_results] == list(reversed(saved))[
        :3
    ]
    assert value.field_work.has_more_results
    assert restore(field, capture(field, value)) == value


def test_stranded_work_accumulates_without_hiding_actionable_plan(field):
    from watershed_memory.current.case_types import ReviewDraft

    parent = field[3]
    stranded = []
    for index in range(4):
        value = mutate(
            field,
            "propose_plan",
            ProposeFieldPlan(parent.task_id, parent.revision, SITE.location_id, 1, SPEC, True),
        )
        stranded.append(value.plan.plan_id)
        field[1].act(
            CASE,
            HumanAction(parent.task_id, parent.revision, "CANCEL"),
            request_id=f"close-{index}",
            now=NOW,
        )
        parent = field[1].stage_review(
            CASE,
            ReviewDraft(
                "COVERAGE_REVIEW",
                "Check the next station update",
                "Review the missing station measurements.",
                event_id(field),
                NOW + timedelta(hours=1),
            ),
            request_id=f"new-parent-{index}",
            expected_case_revision=field[1].context(CASE)["revision"],
            now=NOW,
        )
    active = mutate(
        field,
        "propose_plan",
        ProposeFieldPlan(parent.task_id, parent.revision, SITE.location_id, 1, SPEC, True),
    )
    context = load(field, NOW)
    assert [item.plan.plan_id for item in context.field_work.current_plans] == [active.plan.plan_id]
    assert [item.plan.plan_id for item in context.field_work.stranded_plans] == list(
        reversed(stranded)
    )[:3]
    assert all(item.parent_binding == "TERMINAL" for item in context.field_work.stranded_plans)
    assert context.field_work.has_more_stranded_plans
    assert restore(field, capture(field, context)) == context


def test_changed_parent_revision_strands_old_plan(field):
    original = proposed(field)
    field[1].act(
        CASE,
        HumanAction(
            field[3].task_id,
            field[3].revision,
            "MODIFY",
            title="A changed human observation plan",
            next_check_at=NOW + timedelta(hours=1),
        ),
        request_id="change-parent",
        now=NOW,
    )
    context = load(field, NOW)
    assert context.field_work.current_plans == ()
    assert context.field_work.stranded_plans[0].plan == original.plan
    assert context.field_work.stranded_plans[0].parent_binding == "SUPERSEDED"
    assert restore(field, capture(field, context)) == context


def test_cancel_only_history_is_present_without_actionable_work(field):
    value = proposed(field)
    mutate(field, "decide_plan", DecideFieldPlan(value.plan.plan_id, 1, "CANCEL"))
    context = load(field, NOW)
    assert context.field_work.state == "PRESENT"
    assert (
        context.field_work.current_plans
        == context.field_work.stranded_plans
        == context.field_work.latest_results
        == ()
    )
    assert restore(field, capture(field, context)) == context


def test_capture_refuses_forged_field_omission_even_with_recomputed_digest(field):
    from watershed_memory.current.field_context import field_hash

    proposed(field)
    value = load(field, NOW)
    omitted = replace(value.field_work, current_plans=())
    forged = replace(value, field_work=omitted, field_digest=field_hash(omitted))
    before = contents(field[0])
    with pytest.raises(WorkflowConflict):
        capture(field, forged)
    assert contents(field[0]) == before


def test_location_selection_is_exact_bounded_and_excludes_reference_station(field):
    from watershed_memory.current.locations import gallinas_location_registry

    official = gallinas_location_registry().get("USGS-08380500", 1)
    other_case = replace(SITE, location_id="other-case-site", case_id="UNRELATED")
    withdrawn = replace(SITE, location_id="withdrawn-site", status="WITHDRAWN")
    registry = LocationRegistry((official, other_case, withdrawn, SITE))
    store = FieldStore(field[1], registry)
    context = load_context_v3(field[1], store, CASE, event_id(field), evaluated_at=NOW)
    assert context.field_work.approved_locations == (SITE,)
    too_many = LocationRegistry(tuple(replace(SITE, location_id=f"site-{i:02}") for i in range(17)))
    store = FieldStore(field[1], too_many)
    before = contents(field[0])
    with pytest.raises(ValueError, match="too many approved"):
        load_context_v3(field[1], store, CASE, event_id(field), evaluated_at=NOW)
    assert contents(field[0]) == before


def test_changed_location_content_and_reference_are_rejected(field):
    context = load(field)
    reference = capture(field, context)
    changed = FieldStore(
        field[1], LocationRegistry((replace(SITE, label="Altered same revision"),))
    )
    with pytest.raises(WorkflowConflict):
        restore(field, reference, changed)
    with pytest.raises((ValueError, WorkflowConflict)):
        restore(
            field,
            replace(
                reference,
                approved_locations=(
                    replace(reference.approved_locations[0], entry_digest="b" * 64),
                ),
            ),
        )
