"""Exact, bounded inputs for replaying admission and quiet-source decisions."""

import json
import sqlite3
from decimal import Decimal

from .assessment_types import _canonical_json
from .attention import AttentionPolicy, AttentionResult, AttentionRule, DueCheck, evaluate_attention
from .case_types import WorkflowConflict
from .context import CurrentContext
from .context_reference import capture_context, decode_reference, encode_reference, restore_context
from .fact_validation import timestamp
from .facts import compare_intervals
from .tools import _json


def decode_policy(raw: str) -> AttentionPolicy:
    _canonical_json(raw, "attention policy", 8192)
    data = json.loads(raw)
    if set(data) != {"policy_id", "rules"} or type(data["rules"]) is not list:
        raise ValueError("invalid attention policy fields")
    rules = []
    fields = set(AttentionRule.__dataclass_fields__)
    for item in data["rules"]:
        if type(item) is not dict or set(item) != fields:
            raise ValueError("invalid attention rule fields")
        for key in ("minimum_absolute_change", "minimum_relative_change"):
            if type(item[key]) is not str:
                raise ValueError("attention thresholds must be decimal strings")
            item[key] = Decimal(item[key])
        rules.append(AttentionRule(**item))
    policy = AttentionPolicy(data["policy_id"], tuple(rules))
    if _json(policy) != raw:
        raise ValueError("attention policy is not canonical")
    return policy


def due_identities(context: CurrentContext) -> tuple[DueCheck, ...]:
    return tuple(
        DueCheck(r.task_id, r.revision, r.next_check_at)
        for r in context.reviews
        if r.next_check_at is not None and r.status in {"PROPOSED", "APPROVED", "DEFERRED"}
    )


def handled_keys(db: sqlite3.Connection, context: CurrentContext) -> tuple[str, ...]:
    result = []
    for item in due_identities(context):
        row = db.execute(
            "SELECT * FROM dispatch_due WHERE case_id=? AND due_key=?", (context.case_id, item.key)
        ).fetchone()
        if row is not None:
            if (row["task_id"], row["revision"], row["next_check_at"]) != (
                item.task_id,
                item.revision,
                item.next_check_at.isoformat(),
            ):
                raise WorkflowConflict("handled due identity differs")
            if timestamp(row["handled_at"]) <= context.evaluated_at:
                result.append(item.key)
    return tuple(result)


def evaluate_admission(context, policy, numeric, health, handled):
    """Late source evidence gets review without inventing a numeric comparison."""
    if (numeric is None) != (health is None):
        raise ValueError("assessed numeric and health baselines must be paired")
    if context.source_health is None:
        raise ValueError("dispatch requires current source health")
    for baseline in (numeric, health):
        if baseline is not None and (
            baseline.case_id != context.case_id
            or baseline.policy_digest != context.policy_digest
            or baseline.source_health is None
            or baseline.evaluated_at > context.evaluated_at
        ):
            raise ValueError("dispatch baseline differs from case, policy or chronology")
    basis = None if numeric is None else numeric.current
    basis_health = None if health is None else health.source_health
    late = False
    if basis is not None and basis.event_id != context.current.event_id:
        current = context.current
        relation = compare_intervals(current, basis)
        linked = any(current.supersedes_event_id in refs for _, refs in context.review_evidence)
        ancestor = next(
            (p for p in context.prior if p.event_id == current.supersedes_event_id), None
        )
        older = current.interval_end <= basis.interval_start
        superseded = (
            current.interval_start == basis.interval_start
            and current.interval_end == basis.interval_end
            and current.revision < basis.revision
        )
        late = (
            not relation.comparable
            and (older or superseded)
            and not (older and linked and ancestor)
        )
    if late:
        basic = evaluate_attention(context, policy, handled_due_keys=handled)
        attention = AttentionResult(
            True,
            ("LATE_EVIDENCE",) + tuple(r for r in basic.reasons if r != "INITIAL_REVIEW"),
            (),
            basic.due_checks,
            policy.policy_digest,
            basis.event_id,
        )
    else:
        attention = evaluate_attention(
            context, policy, basis=basis, basis_health=basis_health, handled_due_keys=handled
        )
    move = (
        basis is None
        or (context.current.interval_start >= basis.interval_end)
        or (context.current.supersedes_event_id == basis.event_id)
    )
    return attention, move


def capture_admission(context, numeric, health, handled, *, source_delivery: bool) -> str:
    def reference(ctx):
        return None if ctx is None else json.loads(encode_reference(capture_context(ctx)))

    result = _json(
        {
            "context": reference(context),
            "numeric": reference(numeric),
            "health": reference(health),
            "handled_due_keys": handled,
            "source_delivery": source_delivery,
        }
    )
    _canonical_json(result, "dispatch replay", 32768)
    return result


def restore_admission(
    db: sqlite3.Connection, raw: str, attention_raw: str, policy: AttentionPolicy
) -> tuple[CurrentContext, AttentionResult, bool]:
    _canonical_json(raw, "dispatch replay", 32768)
    _canonical_json(attention_raw, "attention result", 4096)
    data = json.loads(raw)
    if set(data) != {"context", "numeric", "health", "handled_due_keys", "source_delivery"}:
        raise ValueError("invalid replay fields")
    if type(data["source_delivery"]) is not bool or type(data["handled_due_keys"]) is not list:
        raise ValueError("invalid admission metadata")

    def restore(value):
        return None if value is None else restore_context(db, decode_reference(_json(value)))

    context, numeric, health = (restore(data[k]) for k in ("context", "numeric", "health"))
    if context is None:
        raise ValueError("missing admission context")
    handled = tuple(data["handled_due_keys"])
    known = handled_keys(db, context)
    # Later completions can share an equal timestamp. Original handled inputs
    # must exist, but an append after this admission cannot rewrite its snapshot.
    if any(key not in known for key in handled):
        raise WorkflowConflict("admission claims an unrecorded handled check")
    attention, move = evaluate_admission(context, policy, numeric, health, handled)
    if _json(attention) != attention_raw:
        raise WorkflowConflict("attention result differs from saved admission inputs")
    return context, attention, move
