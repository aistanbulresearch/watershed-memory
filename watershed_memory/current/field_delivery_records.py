"""Bind v3 delivery evidence to its exact reserved source-and-field snapshot."""

import json
from dataclasses import asdict

from . import case_records as rows
from .assessment_types import _canonical_json
from .case_types import EvidenceLink, ReviewDraft, ReviewRecord
from .context_v3_reference import decode_reference_v3, restore_context_v3
from .delivery_store import _encoded, _receipt
from .delivery_types import InvocationProfile
from .fact_validation import timestamp
from .field_codec import encode
from .field_delivery_codec import decode_execution, decode_failure
from .field_delivery_types import CurrentExecutionV3, CurrentFailureV3, FieldDeliveryReceipt
from .field_receipts import restore as restore_field_receipt
from .field_tools import CurrentToolsV3, validate_assessment_v3
from .field_types import ProposeFieldPlan
from .tools import _json


def restore_reserved(db, fields, attempt):
    pin = db.execute(
        "SELECT * FROM context3_attempts WHERE attempt_id=? AND case_id=?",
        (attempt["attempt_id"], attempt["case_id"]),
    ).fetchone()
    if pin is None:
        raise ValueError("attempt has no field-aware context binding")
    reference = decode_reference_v3(pin["reference_json"])
    context = restore_context_v3(db, fields, reference)
    base = context.base
    profile = InvocationProfile(**json.loads(attempt["profile_json"]))
    if profile.instruction_version != "watershed-current-v3":
        raise ValueError("attempt is not field-aware")
    _canonical_json(attempt["input_json"], "reserved input", 4096)
    requested = json.loads(attempt["input_json"])
    expected_request = {
        "event_id": base.current.event_id,
        "profile": profile,
        "source_delivery": bool(attempt["source_delivery"]),
        "include_source_health": True,
        "context_version": 3,
    }
    if "prior_event_ids" in requested:
        expected_request["prior_event_ids"] = tuple(item.event_id for item in base.prior)
    expected_input = _encoded(expected_request, limit=4096)
    if (
        attempt["input_json"] != expected_input
        or attempt["input_hash64"] != rows.digest(expected_input)
        or (attempt["source_delivery"] and base.current.case_id != base.case_id)
    ):
        raise ValueError("reserved request differs from its exact context")
    if (
        base.case_id,
        base.current.monitor_id,
        base.current.event_id,
        base.case_revision,
        base.policy_digest,
        base.evaluated_at,
        reference.context_digest,
    ) != (
        attempt["case_id"],
        attempt["monitor_id"],
        attempt["event_id"],
        attempt["case_revision"],
        attempt["coverage_digest64"],
        timestamp(attempt["evaluated_at"]),
        attempt["context_digest64"],
    ) or (
        pin["context_digest64"] != reference.context_digest
        or pin["field_digest64"] != context.field_digest
        or timestamp(pin["created_at"]) != base.evaluated_at
        or attempt["mode"] != profile.mode
        or (profile.mode == "SCRIPTED_SDK" and not base.simulated)
        or _encoded(profile, limit=2048) != attempt["profile_json"]
    ):
        raise ValueError("attempt differs from its exact context reference")
    priors = rows.encode([item.event_id for item in base.prior])
    reviews = _encoded(
        tuple(
            (review.task_id, review.revision, evidence)
            for review, (_, evidence) in zip(base.reviews, base.review_evidence, strict=True)
        ),
        limit=4096,
    )
    health = db.execute(
        "SELECT version_ids_json FROM health_attempts WHERE attempt_id=?", (attempt["attempt_id"],)
    ).fetchone()
    if (
        attempt["prior_ids_json"] != priors
        or attempt["review_refs_json"] != reviews
        or base.source_health is None
        or health is None
        or health[0] != _encoded(base.source_health.version_ids, limit=512)
    ):
        raise ValueError("reserved source pins differ from field-aware context")
    return context


def agent_input(context, proposal, attempt_id, revision):
    command = ProposeFieldPlan(
        proposal.task_id,
        proposal.review_revision,
        proposal.location_id,
        proposal.location_revision,
        proposal.spec,
        context.base.simulated,
    )
    return encode(
        {
            "operation": "PROPOSE",
            "case_id": context.base.case_id,
            "policy_digest": context.base.policy_digest,
            "command": asdict(command),
            "actor": {"kind": "AGENT", "attempt_id": attempt_id},
            "expected_case_revision": revision,
        },
        limit=65536,
    )


def source_work(db, context, execution, attempt_id, now):
    saved = []
    base = context.base
    for index, decision in enumerate(execution.assessment.base.decisions):
        if decision.disposition == "NO_FOLLOW_UP":
            continue
        operation = "STAGE" if decision.disposition == "PROPOSE_REVIEW" else "LINK"
        command = (
            ReviewDraft(
                decision.kind,
                decision.title,
                decision.reason,
                decision.event_id,
                decision.next_check_at,
            )
            if operation == "STAGE"
            else EvidenceLink(decision.target_task_id, decision.event_id, decision.reason)
        )
        encoded = rows.encode(
            {
                "operation": operation,
                "case_id": base.case_id,
                "policy_digest": base.policy_digest,
                "command": asdict(command),
                "expected_case_revision": base.case_revision + len(saved),
            }
        )
        raw = db.execute(
            "SELECT * FROM current_receipts WHERE case_id=? AND request_id=?",
            (base.case_id, f"{attempt_id}-{index}"),
        ).fetchone()
        if (
            raw is None
            or raw["operation"] != operation
            or raw["input_hash"] != rows.digest(encoded)
            or raw["input_json"] != encoded
            or timestamp(raw["recorded_at"]) != now
        ):
            raise ValueError("source work lacks its exact delivery receipt")
        value = rows.record(json.loads(raw["result_json"]))
        expected = (
            ReviewRecord(
                value.task_id,
                base.case_id,
                decision.kind,
                "PROPOSED",
                1,
                decision.title,
                decision.reason,
                decision.next_check_at,
                base.simulated,
                now,
                now,
            )
            if operation == "STAGE"
            else next(
                (item for item in base.reviews if item.task_id == decision.target_task_id), None
            )
        )
        immutable = db.execute(
            "SELECT record_json FROM current_review_revisions WHERE case_id=? AND task_id=? AND revision=?",
            (base.case_id, value.task_id, value.revision),
        ).fetchone()
        evidence = db.execute(
            "SELECT recorded_at FROM current_review_evidence "
            "WHERE case_id=? AND task_id=? AND event_id=?",
            (base.case_id, value.task_id, decision.event_id),
        ).fetchone()
        if (
            value != expected
            or rows.encode(asdict(value)) != raw["result_json"]
            or (raw["task_id"], raw["task_revision"]) != (value.task_id, value.revision)
            or immutable is None
            or immutable[0] != raw["result_json"]
            or evidence is None
            or timestamp(evidence[0]) > now
        ):
            raise ValueError("source receipt differs from its immutable work and evidence")
        saved.append(value)
    return tuple(saved)


def validate_identity(attempt, context, value):
    if type(value) not in (CurrentExecutionV3, CurrentFailureV3):
        raise ValueError("expected typed v3 execution evidence")
    profile = InvocationProfile(
        value.mode, value.model_id, value.instruction_version, value.sdk_version
    )
    identity = value.assessment.base if type(value) is CurrentExecutionV3 else value
    context_digest = (
        value.assessment.context_digest
        if type(value) is CurrentExecutionV3
        else value.context_digest
    )
    if (
        _encoded(profile, limit=2048) != attempt["profile_json"]
        or (identity.case_id, identity.case_revision, identity.policy_digest, identity.event_id)
        != (
            context.base.case_id,
            context.base.case_revision,
            context.base.policy_digest,
            context.base.current.event_id,
        )
        or context_digest != attempt["context_digest64"]
    ):
        raise ValueError("v3 execution differs from its reserved invocation")


def validate_failure(context, failure):
    tools = CurrentToolsV3(context)
    try:
        for target, receipts in (
            (tools.source, failure.source_trace),
            (tools, failure.field_trace),
        ):
            for item in receipts:
                output = getattr(target, item.name)(**json.loads(item.input_json))
                if _json(output) != item.output_json:
                    raise ValueError("failed turn receipt differs from replay")
    except (KeyError, TypeError, RuntimeError, ValueError) as error:
        raise ValueError("failed turn trace is invalid") from error


def receipt(db, fields, attempt):
    context = restore_reserved(db, fields, attempt)
    delivery = _receipt(attempt)
    execution = None
    if attempt["execution_json"] is not None:
        execution = decode_execution(attempt["execution_json"])
        validate_identity(attempt, context, execution)
        validate_assessment_v3(context, execution.assessment)
    if attempt["failure_json"] is not None:
        failure = decode_failure(attempt["failure_json"])
        validate_identity(attempt, context, failure)
        validate_failure(context, failure)
    if delivery.status == "COMMITTED":
        work = source_work(db, context, execution, attempt["attempt_id"], delivery.recorded_at)
        if work != delivery.work:
            raise ValueError("delivery work differs from its exact source receipts")
        case = rows.case_row(db, attempt["case_id"])
        minimum_revision = (
            context.base.case_revision
            + len(work)
            + int(execution.assessment.field.proposal is not None)
            + 1
        )
        if (
            case["revision"] < minimum_revision
            or timestamp(case["updated_at"]) < delivery.recorded_at
        ):
            raise ValueError("committed delivery lacks its final case marker")
        if delivery.source_delivery:
            acknowledged = db.execute(
                "SELECT status FROM watch_outbox WHERE monitor_id=? AND event_id=?",
                (attempt["monitor_id"], attempt["event_id"]),
            ).fetchone()
            if acknowledged is None or acknowledged[0] != "DONE":
                raise ValueError("committed source delivery lacks its acknowledgement")
    membership = db.execute(
        "SELECT * FROM context3_agent_proposals WHERE attempt_id=?", (attempt["attempt_id"],)
    ).fetchone()
    proposal = execution.assessment.field.proposal if execution else None
    expected = delivery.status == "COMMITTED" and proposal is not None
    if expected != (membership is not None):
        raise ValueError("delivery proposal membership differs from its decision")
    result = None
    if membership is not None:
        raw = db.execute(
            "SELECT * FROM field_receipts WHERE case_id=? AND request_id=?",
            (membership["case_id"], membership["request_id"]),
        ).fetchone()
        if raw is None:
            raise ValueError("agent proposal receipt is missing")
        result = restore_field_receipt(db, raw, rows.case_row(db, attempt["case_id"]))
        plan = result.plan
        revision = context.base.case_revision + len(delivery.work)
        expected_input = agent_input(context, proposal, attempt["attempt_id"], revision)
        if (
            membership["case_id"] != attempt["case_id"]
            or result.case_revision != revision + 1
            or raw["input_json"] != expected_input
            or raw["input_hash"] != rows.digest(expected_input)
            or (membership["plan_id"], membership["plan_revision"]) != (plan.plan_id, plan.revision)
            or (
                plan.task_id,
                plan.review_revision,
                plan.location.location_id,
                plan.location.revision,
                plan.spec,
            )
            != (
                proposal.task_id,
                proposal.review_revision,
                proposal.location_id,
                proposal.location_revision,
                proposal.spec,
            )
            or plan.simulated is not context.base.simulated
        ):
            raise ValueError("saved agent proposal differs from its staged decision")
    return FieldDeliveryReceipt(delivery, result)
