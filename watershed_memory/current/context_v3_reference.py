"""Exact field activity recipes, restored independently of today's work pointers."""

import json
from dataclasses import asdict
from dataclasses import fields as dataclass_fields

from . import case_records as cases
from .case_types import WorkflowConflict
from .context_reference import capture_context, decode_reference, encode_reference, restore_context
from .context_v3 import _require_transaction
from .context_v3_types import (
    ApprovedLocationReference,
    ContextReferenceV3,
    CurrentContextV3,
    FieldActivityReference,
    FieldContext,
)
from .field_codec import _pairs, _reject_constant, encode
from .field_context import SELECTIONS, _load_field_context, field_hash
from .field_models import FieldWorkSnapshot, field_dimension
from .field_receipts import restore as restore_receipt
from .tools import _json

_LIMIT = 32768
# Private command input is deliberately excluded from this read capability.
_RECEIPT_COLUMNS = (
    "case_id,request_id,operation,case_revision,plan_id,plan_revision,recorded_at,"
    "result_json,result_hash"
)


def _parent(db, case_id, task_id, revision, evaluated):
    raw = db.execute(
        "SELECT record_json FROM current_review_revisions WHERE case_id=? AND task_id=? "
        "AND revision=?",
        (case_id, task_id, revision),
    ).fetchone()
    if raw is None:
        raise KeyError("field context parent revision does not exist")
    parent = cases.record(json.loads(raw[0]))
    if (
        (parent.case_id, parent.task_id, parent.revision) != (case_id, task_id, revision)
        or parent.updated_at > evaluated
        or parent.kind not in ("OBSERVATION_REVIEW", "COVERAGE_REVIEW")
    ):
        raise ValueError("field context parent differs from its exact revision")
    return parent


def _snapshot(receipt, parent):
    plan = receipt.plan
    if (plan.case_id, plan.task_id, plan.simulated) != (
        parent.case_id,
        parent.task_id,
        parent.simulated,
    ) or parent.revision < plan.review_revision:
        raise ValueError("field context parent differs from its plan")
    binding = (
        "TERMINAL"
        if parent.status in ("CANCELLED", "DISMISSED")
        else "CURRENT"
        if parent.revision == plan.review_revision
        else "SUPERSEDED"
    )
    return FieldWorkSnapshot(plan, receipt.result, binding, field_dimension(plan, receipt.result))


def capture_context_v3(db, fields, context: CurrentContextV3) -> ContextReferenceV3:
    _require_transaction(db, fields)
    if type(context) is not CurrentContextV3:
        raise ValueError("expected an immutable v3 context")
    base = context.base
    case = cases.case_row(db, base.case_id)
    if case["revision"] != base.case_revision:
        raise WorkflowConflict("case changed before field context capture")
    fresh = _load_field_context(db, case, fields.locations, evaluated_at=base.evaluated_at)
    if fresh != context.field_work or field_hash(fresh) != context.field_digest:
        raise WorkflowConflict("field context differs from the current capture snapshot")
    base_reference = capture_context(base)
    if restore_context(db, base_reference) != base:
        raise ValueError("base context differs from its immutable source records")
    activities = []
    for selection, name in SELECTIONS:
        for value in getattr(fresh, name):
            pointer = db.execute(
                "SELECT last_activity_revision FROM field_plans WHERE case_id=? AND plan_id=?",
                (base.case_id, value.plan.plan_id),
            ).fetchone()[0]
            raw = db.execute(
                "SELECT " + _RECEIPT_COLUMNS + " FROM field_receipts WHERE case_id=? "
                "AND plan_id=? AND case_revision=?",
                (base.case_id, value.plan.plan_id, pointer),
            ).fetchone()
            if raw is None:
                raise ValueError("field activity lacks its exact receipt")
            receipt = restore_receipt(db, raw, case)
            current_parent = cases.review_row(db, base.case_id, value.plan.task_id)
            parent = _parent(
                db, base.case_id, value.plan.task_id, current_parent["revision"], base.evaluated_at
            )
            if parent != cases.record(current_parent) or _snapshot(receipt, parent) != value:
                raise ValueError("field activity receipt differs from the captured work")
            activities.append(
                FieldActivityReference(
                    selection,
                    value.plan.plan_id,
                    pointer,
                    receipt.request_id,
                    parent.task_id,
                    parent.revision,
                    parent.status,
                    value.parent_binding,
                    field_hash(value),
                )
            )
    locations = tuple(
        ApprovedLocationReference(site.location_id, site.revision, field_hash(site))
        for site in fresh.approved_locations
    )
    return ContextReferenceV3(
        3,
        encode_reference(base_reference),
        tuple(activities),
        locations,
        fresh.state,
        fresh.has_more_stranded_plans,
        fresh.has_more_results,
        context.field_digest,
        cases.digest(_json(context)),
    )


def encode_reference_v3(reference: ContextReferenceV3) -> str:
    if type(reference) is not ContextReferenceV3:
        raise ValueError("expected a v3 context reference")
    return encode(asdict(reference), limit=_LIMIT)


def _shape(value, cls):
    if type(value) is not dict or set(value) != {item.name for item in dataclass_fields(cls)}:
        raise ValueError("invalid v3 context reference shape")


def decode_reference_v3(raw: str) -> ContextReferenceV3:
    if type(raw) is not str or len(raw.encode("utf-8")) > _LIMIT:
        raise ValueError("v3 context reference exceeds its byte bound")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        _shape(value, ContextReferenceV3)
        for key, cls, bound in (
            ("field_activities", FieldActivityReference, 8),
            ("approved_locations", ApprovedLocationReference, 16),
        ):
            if type(value[key]) is not list or len(value[key]) > bound:
                raise ValueError("invalid v3 reference array")
            for item in value[key]:
                _shape(item, cls)
            value[key] = tuple(cls(**item) for item in value[key])
        reference = ContextReferenceV3(**value)
        if encode_reference_v3(reference) != raw:
            raise ValueError("v3 reference is not canonical")
        return reference
    except (TypeError, OverflowError, RecursionError, KeyError, UnicodeError) as error:
        raise ValueError("invalid v3 context reference") from error


def restore_context_v3(db, fields, reference: ContextReferenceV3) -> CurrentContextV3:
    _require_transaction(db, fields)
    if type(reference) is not ContextReferenceV3:
        raise ValueError("expected a v3 context reference")
    base = restore_context(db, decode_reference(reference.base_reference_json))
    case = cases.case_row(db, base.case_id)
    selections = {selection: [] for selection, _ in SELECTIONS}
    for item in reference.field_activities:
        raw = db.execute(
            "SELECT " + _RECEIPT_COLUMNS + " FROM field_receipts WHERE case_id=? AND request_id=?",
            (base.case_id, item.request_id),
        ).fetchone()
        if raw is None:
            raise KeyError("reserved field activity does not exist")
        receipt = restore_receipt(db, raw, case)
        if (
            (receipt.plan.plan_id, receipt.case_revision, receipt.plan.task_id)
            != (item.plan_id, item.activity_case_revision, item.parent_task_id)
            or receipt.case_revision > base.case_revision
            or receipt.recorded_at > base.evaluated_at
        ):
            raise ValueError("reserved field activity differs from its receipt")
        parent = _parent(
            db, base.case_id, item.parent_task_id, item.parent_revision, base.evaluated_at
        )
        value = _snapshot(receipt, parent)
        if (
            parent.status != item.parent_status
            or value.parent_binding != item.parent_binding
            or field_hash(value) != item.snapshot_digest
        ):
            raise WorkflowConflict("reserved field work differs from its exact snapshot")
        selections[item.selection].append(value)
    locations = []
    for item in reference.approved_locations:
        entry = fields.locations.get(item.location_id, item.revision)
        if field_hash(entry) != item.entry_digest:
            raise WorkflowConflict("reserved approved location differs from its snapshot")
        locations.append(entry)
    field = FieldContext(
        base.case_id,
        base.case_revision,
        base.simulated,
        base.evaluated_at,
        reference.field_state,
        *(tuple(selections[selection]) for selection, _ in SELECTIONS),
        tuple(locations),
        reference.has_more_stranded_plans,
        reference.has_more_results,
    )
    context = CurrentContextV3(base, field, reference.field_digest)
    if cases.digest(_json(context)) != reference.context_digest:
        raise WorkflowConflict("reserved v3 context differs from its saved digest")
    return context
