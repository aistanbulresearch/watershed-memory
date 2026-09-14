"""Read exact field memory and stage one unapproved decision without database access."""

import json
from datetime import timedelta

from .assessment_types import _TOOLS, ToolReceipt
from .case_records import digest
from .context_v3_types import CurrentContextV3
from .fact_validation import timestamp
from .field_assessment_types import AgentFieldDecision, AgentFieldProposal, CurrentAssessmentV3
from .field_context import ACTIVITIES, SELECTIONS
from .field_tool_runtime import _V3AttemptBudget, _V3SourceTools, field_tool
from .field_types import FieldPlanSpec
from .tools import _json, _plain

_NAMES = {
    "get_field_context",
    "inspect_field_work",
    "list_approved_field_locations",
    "stage_field_decision",
}


def _index(snapshot):
    result = snapshot.result
    return {
        "plan_id": snapshot.plan.plan_id,
        "task_id": snapshot.plan.task_id,
        "status": snapshot.plan.status,
        "parent_binding": snapshot.parent_binding,
        "report_id": result.report.report_id if result else None,
        "report_revision": result.report.revision if result else None,
        "outcome": result.report.outcome if result else None,
        "verification_level": result.verification_level if result else None,
    }


class CurrentToolsV3:
    def __init__(self, context: CurrentContextV3):
        if type(context) is not CurrentContextV3:
            raise ValueError("expected an immutable v3 context")
        self.context = context
        self._budget = _V3AttemptBudget()
        self.source = _V3SourceTools(context.base, self._budget)
        self._trace = []
        self._indexed = False
        self._inspected = set()
        self._locations = set()
        self._decision = None
        self._snapshots = {
            item.plan.plan_id: item
            for _, name in SELECTIONS
            for item in getattr(context.field_work, name)
        }

    @property
    def attempts(self):
        return self._budget.attempts

    @property
    def field_trace(self):
        return tuple(self._trace)

    def _state(self):
        return (
            self._indexed,
            self._inspected.copy(),
            self._locations.copy(),
            self._decision,
            self._trace.copy(),
            self.source.closed,
        )

    def _restore(self, state):
        (
            self._indexed,
            self._inspected,
            self._locations,
            self._decision,
            self._trace,
            self.source.closed,
        ) = state

    def _require_index(self):
        if not self._indexed:
            raise ValueError("field index must be read first")

    @field_tool("get_field_context")
    def get_field_context(self):
        """Read the current, outdated and reported field-work index after source assessment."""
        self.source.finish()
        field = self.context.field_work
        self._indexed = True
        self.source.closed = True
        return {
            "case_id": field.case_id,
            "case_revision": field.case_revision,
            "simulated": field.simulated,
            "evaluated_at": field.evaluated_at.isoformat(),
            "state": field.state,
            "field_digest": self.context.field_digest,
            **{name: [_index(item) for item in getattr(field, name)] for _, name in SELECTIONS},
            "has_more_stranded_plans": field.has_more_stranded_plans,
            "has_more_results": field.has_more_results,
            "approved_location_count": len(field.approved_locations),
        }

    @field_tool("inspect_field_work")
    def inspect_field_work(self, plan_id: str):
        """Inspect one allowlisted plan, report and exact evidence/verification scope."""
        self._require_index()
        if type(plan_id) is not str or plan_id not in self._snapshots:
            raise ValueError("field work is outside this context")
        self._inspected.add(plan_id)
        return _plain(self._snapshots[plan_id])

    @field_tool("list_approved_field_locations")
    def list_approved_field_locations(self, activity: str):
        """Read exact case-bound approved sites for one supported activity."""
        self._require_index()
        if type(activity) is not str or activity not in ACTIVITIES:
            raise ValueError("unsupported field activity")
        self._locations.add(activity)
        return {
            "activity": activity,
            "locations": [
                _plain(item)
                for item in self.context.field_work.approved_locations
                if activity in item.activities
            ],
        }

    def _latest_for(self, task_id):
        return next(
            (
                item
                for item in self.context.field_work.latest_results
                if item.plan.task_id == task_id
            ),
            None,
        )

    def _require_inspection(self, decision):
        field = self.context.field_work
        latest = field.latest_results[:1]
        if decision.proposal is not None:
            target = self._latest_for(decision.proposal.task_id)
            latest = (target,) if target else ()
        elif decision.disposition == "AWAIT_VERIFICATION":
            target = self._snapshots.get(decision.basis_plan_id)
            latest = (target,) if target else ()
        needed = [
            *field.current_plans,
            *(item for item in field.stranded_plans if item.parent_binding == "SUPERSEDED"),
            *tuple(item for item in field.stranded_plans if item.parent_binding == "TERMINAL")[:1],
            *latest,
        ]
        if any(item.plan.plan_id not in self._inspected for item in needed):
            raise ValueError("relevant field work must be inspected first")

    def _basis(self, decision):
        if decision.basis_plan_id is None:
            if decision.disposition == "NO_NEW_FIELD_PLAN" and self._snapshots:
                raise ValueError("existing field work requires an explicit inspected basis")
            return None
        if decision.basis_plan_id not in self._inspected:
            raise ValueError("field decision basis was not inspected")
        value = self._snapshots[decision.basis_plan_id]
        result = value.result
        expected = (
            (result.report.report_id, result.report.revision, result.verification_level)
            if result
            else (None, None, None)
        )
        if (
            decision.basis_report_id,
            decision.basis_report_revision,
            decision.basis_verification_level,
        ) != expected:
            raise ValueError("field decision differs from its exact result basis")
        return value

    def _proposal(self, proposal, basis):
        base, field = self.context.base, self.context.field_work
        parent = next(
            (review for review in base.reviews if review.task_id == proposal.task_id), None
        )
        if (
            parent is None
            or parent.revision != proposal.review_revision
            or parent.kind not in self.source._checked
        ):
            raise ValueError("field proposal requires an inspected exact active parent")
        if any(
            item.plan.task_id == parent.task_id
            for item in (*field.current_plans, *field.stranded_plans)
        ):
            raise ValueError("parent already has an active field plan")
        latest = self._latest_for(parent.task_id)
        if basis is not latest or (
            basis is not None
            and (basis.result is None or basis.result.report.outcome not in {"PARTIAL", "NOT_DONE"})
        ):
            raise ValueError(
                "follow-up requires the latest selected unfinished result for its task"
            )
        spec = proposal.spec
        if spec.activity not in self._locations:
            raise ValueError("approved sites for this activity must be read")
        site = next(
            (
                item
                for item in field.approved_locations
                if (item.location_id, item.revision)
                == (proposal.location_id, proposal.location_revision)
            ),
            None,
        )
        if site is None or spec.activity not in site.activities:
            raise ValueError("site revision does not authorize this activity")
        if spec.window_start < base.evaluated_at or spec.window_end - base.evaluated_at > timedelta(
            days=30
        ):
            raise ValueError("field window is outside the allowed future interval")

    @field_tool("stage_field_decision")
    def stage_field_decision(
        self,
        disposition: str,
        basis_plan_id: str | None,
        basis_report_id: str | None,
        basis_report_revision: int | None,
        basis_verification_level: str | None,
        reason: str,
        task_id: str | None,
        review_revision: int | None,
        location_id: str | None,
        location_revision: int | None,
        activity: str | None,
        purpose: str | None,
        assignee_role: str | None,
        window_start: str | None,
        window_end: str | None,
        required_evidence: list[str] | None,
    ):
        """Stage one field recommendation; approval, reporting and verification remain human actions."""
        self._require_index()
        proposal = None
        if disposition == "PROPOSE_FIELD_PLAN":
            if type(required_evidence) is not list:
                raise ValueError("required evidence must be an explicit array")
            proposal = AgentFieldProposal(
                task_id,
                review_revision,
                location_id,
                location_revision,
                FieldPlanSpec(
                    activity,
                    purpose,
                    assignee_role,
                    timestamp(window_start),
                    timestamp(window_end),
                    tuple(required_evidence),
                ),
            )
        elif any(
            value is not None
            for value in (
                task_id,
                review_revision,
                location_id,
                location_revision,
                activity,
                purpose,
                assignee_role,
                window_start,
                window_end,
                required_evidence,
            )
        ):
            raise ValueError("only a field proposal can carry plan arguments")
        decision = AgentFieldDecision(
            disposition,
            basis_plan_id,
            basis_report_id,
            basis_report_revision,
            basis_verification_level,
            reason,
            proposal,
        )
        self._require_inspection(decision)
        basis = self._basis(decision)
        if disposition == "AWAIT_VERIFICATION" and (
            basis is None or basis.result is None or basis.result.report.outcome != "COMPLETE"
        ):
            raise ValueError("only a reported complete result can await completion verification")
        if proposal is not None:
            self._proposal(proposal, basis)
        self._decision = decision
        return {"status": "STAGED", "committed": False, "decision": _plain(decision)}

    def _result(self):
        if self._budget.exhausted:
            raise RuntimeError("tool attempt budget exhausted")
        if self._decision is None:
            raise ValueError("no staged field decision")
        return CurrentAssessmentV3(
            3, self.source.finish(), self._decision, self.field_trace, digest(_json(self.context))
        )

    def finish(self):
        assessment = self._result()
        return validate_assessment_v3(self.context, assessment)


def validate_assessment_v3(context, assessment):
    if type(context) is not CurrentContextV3 or type(assessment) is not CurrentAssessmentV3:
        raise ValueError("expected an exact v3 context and assessment")
    tools = CurrentToolsV3(context)
    try:
        for receipt in assessment.base.trace:
            if type(receipt) is not ToolReceipt or receipt.name not in _TOOLS:
                raise ValueError("invalid source tool receipt")
            output = getattr(tools.source, receipt.name)(**json.loads(receipt.input_json))
            if _json(output) != receipt.output_json:
                raise ValueError("source tool output differs from replay")
        for receipt in assessment.field_trace:
            if receipt.name not in _NAMES:
                raise ValueError("invalid field tool receipt")
            output = getattr(tools, receipt.name)(**json.loads(receipt.input_json))
            if _json(output) != receipt.output_json:
                raise ValueError("field tool output differs from replay")
        if tools._result() != assessment:
            raise ValueError("field assessment differs from replay")
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise ValueError("field assessment trace is invalid") from error
    return assessment
