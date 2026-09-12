"""Explicit browser projections of validated current assessment records."""

import json
from dataclasses import asdict

from .assessment_types import CurrentAssessment, ReviewDecision, ToolReceipt, _canonical_json
from .attention import AttentionResult, DueCheck
from .case_store import _expected
from .delivery_store import _identity, _receipt
from .fact_validation import timestamp
from .strands import CurrentExecution, CurrentFailure


def iso(value):
    return None if value is None else value.isoformat()


def object_fields(raw, record):
    if type(raw) is not dict or set(raw) != set(record.__dataclass_fields__):
        raise ValueError("invalid saved record fields")
    return dict(raw)


def array(raw, limit):
    if type(raw) is not list or len(raw) > limit:
        raise ValueError("invalid saved record collection")
    return raw


def decode_execution(encoded, *, failure=False):
    _canonical_json(encoded, "saved execution", 524288)
    record = CurrentFailure if failure else CurrentExecution
    raw = object_fields(json.loads(encoded), record)
    assessment = raw if failure else object_fields(raw["assessment"], CurrentAssessment)
    assessment["trace"] = tuple(
        ToolReceipt(**object_fields(item, ToolReceipt))
        for item in array(assessment["trace"], 12)
    )
    if not failure:
        decisions = []
        for item in array(assessment["decisions"], 2):
            fields = object_fields(item, ReviewDecision)
            fields["reference_ids"] = tuple(array(fields["reference_ids"], 3))
            if fields["next_check_at"] is not None:
                fields["next_check_at"] = timestamp(fields["next_check_at"])
            decisions.append(ReviewDecision(**fields))
        assessment["decisions"] = tuple(decisions)
        raw["assessment"] = CurrentAssessment(**assessment)
    return record(**raw)


def attention_reasons(encoded):
    if encoded is None:
        return []
    _canonical_json(encoded, "saved attention", 4096)
    raw = object_fields(json.loads(encoded), AttentionResult)
    raw["reasons"] = tuple(array(raw["reasons"], 6))
    raw["changed_parameters"] = tuple(array(raw["changed_parameters"], 16))
    due = []
    for item in array(raw["due_checks"], 3):
        fields = object_fields(item, DueCheck)
        fields["next_check_at"] = timestamp(fields["next_check_at"])
        due.append(DueCheck(**fields))
    raw["due_checks"] = tuple(due)
    return list(AttentionResult(**raw).reasons)


def summary(raw):
    _expected(raw["case_revision"])
    receipt = _receipt(raw)
    # The selected browser identity is the reservation identity, not an adapter's text.
    evaluated = timestamp(raw["evaluated_at"])
    finished = None if raw["finished_at"] is None else timestamp(raw["finished_at"])
    if finished is not None and finished < evaluated:
        raise ValueError("saved completion predates reservation")
    if type(raw["source_delivery"]) is not int or raw["source_delivery"] not in (0, 1) or raw["mode"] != receipt.profile.mode:
        raise ValueError("invalid saved delivery mode")
    return {
        "attempt_id": receipt.attempt_id, "event_id": receipt.event_id,
        "status": receipt.status, "source_delivery": receipt.source_delivery,
        "evaluated_at": evaluated.isoformat(), "finished_at": iso(finished),
        "mode": receipt.profile.mode,
    }


def assessment_detail(raw, attention):
    result = summary(raw)
    receipt = _receipt(raw)
    tools, decisions = [], []
    for field, failed in (("execution_json", False), ("failure_json", True)):
        if raw[field] is None:
            continue
        execution = decode_execution(raw[field], failure=failed)
        _identity(raw, execution)
        assessment = execution if failed else execution.assessment
        tools = [entry.name for entry in assessment.trace]
        if not failed:
            for decision in assessment.decisions:
                value = asdict(decision)
                value["next_check_at"] = iso(decision.next_check_at)
                value["reference_ids"] = list(decision.reference_ids)
                decisions.append(value)
    result.pop("mode")
    result.update({
        "case_revision": raw["case_revision"], "profile": asdict(receipt.profile),
        "attention_reasons": attention_reasons(attention),
        "tool_names": tools, "decisions": decisions,
    })
    return result


def work_receipt(review):
    return {"task_id": review.task_id, "revision": review.revision,
            "status": review.status, "title": review.title,
            "next_check_at": iso(review.next_check_at)}
