"""Pure JSON-ready projections for the current field operator desk."""

from __future__ import annotations

from typing import Any

from .context_v3_types import FieldContext, _snapshot
from .field_models import FieldWorkSnapshot
from .field_types import FieldPrincipal
from .tools import _plain

_ACTION_ORDER = ("APPROVE", "MODIFY", "DEFER", "CANCEL", "REPORT", "CORRECT", "ATTACH", "VERIFY")
_ACTIVE = {"PROPOSED", "APPROVED", "DEFERRED"}


def _principal(principal: FieldPrincipal | None, case_id: str) -> None:
    if principal is not None and (
        type(principal) is not FieldPrincipal or case_id not in principal.case_ids
    ):
        raise ValueError("principal is not bound to this case")


def _check_context(context: object) -> FieldContext:
    if type(context) is not FieldContext:
        raise ValueError("expected an exact field context")
    return context


def _check_snapshot(snapshot: object, context: FieldContext) -> FieldWorkSnapshot:
    _snapshot(snapshot, context.case_id, context.simulated, context.evaluated_at)
    return snapshot


def _exact_approved_site(context: FieldContext, plan: FieldWorkSnapshot) -> bool:
    return any(
        location.location_id == plan.plan.location.location_id
        and location.revision == plan.plan.location.revision
        and location.status == "APPROVED"
        and plan.plan.spec.activity in location.activities
        for location in context.approved_locations
    )


def _actions(
    snapshot: FieldWorkSnapshot,
    context: FieldContext,
    principal: FieldPrincipal | None,
) -> list[str]:
    if principal is None:
        return []
    roles = set(principal.roles)
    plan = snapshot.plan
    if plan.status == "REPORTED":
        result = snapshot.result
        count = len(result.evidence) if result is not None else 0
        actions: list[str] = []
        if "FIELD_OPERATOR" in roles:
            actions.append("CORRECT")
        if ("FIELD_OPERATOR" in roles or "VERIFIER" in roles) and count < 8:
            actions.append("ATTACH")
        if (
            "VERIFIER" in roles
            and result is not None
            and count
            and result.verification_level != "VERIFIED"
        ):
            actions.append("VERIFY")
        return [action for action in _ACTION_ORDER if action in actions]
    if snapshot.parent_binding != "CURRENT":
        return ["CANCEL"] if plan.status in _ACTIVE and "COORDINATOR" in roles else []
    if plan.status not in _ACTIVE or "COORDINATOR" not in roles:
        if plan.status == "APPROVED" and "FIELD_OPERATOR" in roles:
            return ["REPORT"]
        return []
    actions = ["CANCEL"]
    if context.approved_locations:
        actions.insert(0, "MODIFY")
        if (
            plan.status in {"PROPOSED", "DEFERRED"}
            and plan.spec.window_end > context.evaluated_at
            and _exact_approved_site(context, snapshot)
        ):
            actions.insert(0, "APPROVE")
    if plan.spec.window_end > context.evaluated_at:
        index = 1 if "APPROVE" in actions else 0
        actions.insert(index + 1, "DEFER")
    if plan.status == "APPROVED" and "FIELD_OPERATOR" in roles:
        actions.append("REPORT")
    return [action for action in _ACTION_ORDER if action in actions]


def _work(
    snapshot: FieldWorkSnapshot, context: FieldContext, principal, detail: bool
) -> dict[str, Any]:
    result = None
    if snapshot.result is not None:
        saved = snapshot.result
        result = {
            "report": _plain(saved.report),
            "verification_level": saved.verification_level,
            "evidence_count": len(saved.evidence),
            "verification": _plain(saved.verification) if saved.verification else None,
        }
        if detail:
            result["evidence"] = _plain(saved.evidence)
    return {
        "plan": _plain(snapshot.plan),
        "result": result,
        "parent_binding": snapshot.parent_binding,
        "field_dimension": snapshot.field_dimension,
        "available_actions": _actions(snapshot, context, principal),
    }


def project_work(
    snapshot: FieldWorkSnapshot,
    context: FieldContext,
    principal: FieldPrincipal | None = None,
    *,
    detail: bool = False,
) -> dict[str, Any]:
    """Project one exact field snapshot, optionally including full evidence."""
    if type(detail) is not bool:
        raise ValueError("detail must be bool")
    context = _check_context(context)
    snapshot = _check_snapshot(snapshot, context)
    _principal(principal, context.case_id)
    return _work(snapshot, context, principal, detail)


def project_field(
    context: FieldContext,
    principal: FieldPrincipal | None = None,
) -> dict[str, Any]:
    """Project the bounded field context and role-filtered work controls."""
    context = _check_context(context)
    _principal(principal, context.case_id)
    return {
        "case_id": context.case_id,
        "case_revision": context.case_revision,
        "simulated": context.simulated,
        "evaluated_at": context.evaluated_at.isoformat(),
        "state": context.state,
        "current_plans": [_work(item, context, principal, False) for item in context.current_plans],
        "stranded_plans": [
            _work(item, context, principal, False) for item in context.stranded_plans
        ],
        "latest_results": [
            _work(item, context, principal, False) for item in context.latest_results
        ],
        "approved_locations": _plain(context.approved_locations),
        "has_more_stranded_plans": context.has_more_stranded_plans,
        "has_more_results": context.has_more_results,
    }
