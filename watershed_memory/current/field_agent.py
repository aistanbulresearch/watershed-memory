"""Internal v3 dispatcher authority can propose field work, never approve or report it."""

from datetime import timedelta

from . import case_records as rows
from . import field_actions as actions
from .case_store import _expected
from .context_v3 import _require_transaction
from .delivery_store import _attempt
from .fact_validation import utc
from .field_delivery_codec import decode_execution
from .field_delivery_records import agent_input, restore_reserved, source_work, validate_identity
from .field_models import FieldPlanRecord
from .field_records import save_plan
from .field_tools import validate_assessment_v3
from .field_types import ProposeFieldPlan


def proposal_command(context, proposal):
    return ProposeFieldPlan(
        proposal.task_id,
        proposal.review_revision,
        proposal.location_id,
        proposal.location_revision,
        proposal.spec,
        context.base.simulated,
    )


def require_current_site(fields, context, proposal, now):
    spec = proposal.spec
    if (
        spec.window_start < context.base.evaluated_at
        or spec.window_end <= now
        or spec.window_end - context.base.evaluated_at > timedelta(days=30)
    ):
        raise ValueError("agent field window is expired or outside its reserved interval")
    place = fields.locations.require_approved(
        proposal.location_id,
        proposal.location_revision,
        case_id=context.base.case_id,
        simulated=context.base.simulated,
        activity=spec.activity,
        now=now,
    )
    if place not in context.field_work.approved_locations:
        raise ValueError("current site differs from the reserved approval")
    return place


def stage_agent_plan(
    fields,
    db,
    case_id,
    command,
    *,
    attempt_id,
    request_id,
    expected_case_revision,
    reserved_context_digest,
    now,
):
    _require_transaction(db, fields)
    _expected(expected_case_revision)
    now = utc(now)
    attempt = _attempt(db, attempt_id)
    if (
        attempt["case_id"] != case_id
        or attempt["status"] != "RESERVED"
        or attempt["execution_json"] is None
        or request_id != attempt_id + "-field-0"
        or attempt["context_digest64"] != reserved_context_digest
    ):
        raise ValueError("agent proposal requires its active v3 delivery")
    context = restore_reserved(db, fields, attempt)
    execution = decode_execution(attempt["execution_json"])
    validate_identity(attempt, context, execution)
    validate_assessment_v3(context, execution.assessment)
    work = source_work(db, context, execution, attempt_id, now)
    if expected_case_revision != context.base.case_revision + len(work):
        raise ValueError("agent proposal requires its completed source-work sequence")
    proposal = execution.assessment.field.proposal
    if (
        proposal is None
        or type(command) is not ProposeFieldPlan
        or command != proposal_command(context, proposal)
    ):
        raise ValueError("agent command differs from its validated field decision")
    if db.execute(
        "SELECT 1 FROM context3_agent_proposals WHERE attempt_id=?", (attempt_id,)
    ).fetchone():
        raise ValueError("attempt already created its field proposal")
    case = rows.case_row(db, case_id)
    rows.check_revision(case, expected_case_revision)
    rows.check_time(case, now)
    actions._simulation(case, command.expected_simulated)
    actions._parent(db, case_id, command.task_id, command.expected_review_revision)
    place = require_current_site(fields, context, proposal, now)
    if db.execute(
        "SELECT 1 FROM field_plans WHERE case_id=? AND task_id=? AND status IN ('PROPOSED','APPROVED','DEFERRED') LIMIT 1",
        (case_id, command.task_id),
    ).fetchone():
        raise ValueError("review already has active field work")
    value = FieldPlanRecord(
        actions._id("plan"),
        case_id,
        command.task_id,
        command.expected_review_revision,
        1,
        "PROPOSED",
        command.spec,
        place,
        bool(case["simulated"]),
        "PROPOSE",
        "AGENT",
        attempt_id,
        None,
        now,
        now,
    )
    encoded = agent_input(context, proposal, attempt_id, expected_case_revision)
    save_plan(db, value, new=True)
    receipt = fields._save_mutation(
        db, "PROPOSE", case, encoded, value, None, None, None, request_id=request_id, now=now
    )
    db.execute(
        "INSERT INTO context3_agent_proposals(attempt_id,case_id,request_id,plan_id,plan_revision) VALUES(?,?,?,?,?)",
        (attempt_id, case_id, request_id, value.plan_id, value.revision),
    )
    return receipt
