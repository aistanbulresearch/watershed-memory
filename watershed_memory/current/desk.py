"""One locally bound operator desk; reads never create schemas or invoke agents."""

from datetime import datetime, timedelta

from ..watch.store import WatchStore
from . import case_records as rows
from . import field_types
from .case_store import CaseStore, _expected
from .case_types import HumanAction, WorkflowConflict, _identifier
from .context import _case_config, _load_base_context, _load_context
from .delivery_schema import ensure_schema as check_delivery_schema
from .delivery_store import _attempt
from .delivery_types import AllowanceSnapshot
from .desk_records import assessment_detail, iso, summary, work_receipt
from .dispatch_schema import ensure_schema as check_dispatch_schema
from .dispatch_store import DispatchStore, _settings
from .fact_validation import event_id, timestamp, utc
from .field_context import _load_field_context
from .field_desk_records import project_field, project_work
from .field_presence import require_legacy_context
from .field_records import snapshot as field_snapshot
from .field_schema import ensure_schema as check_field_schema
from .field_store import FieldStore
from .field_types import FieldPrincipal
from .registry import gallinas_registry
from .source_health import _load_source_health
from .tools import _plain

LABELS = {"00060": "River flow", "00045": "Precipitation", "63680": "Turbidity"}
FIELD_METHODS = {
    field_types.ProposeFieldPlan: "propose_plan",
    field_types.DecideFieldPlan: "decide_plan",
    field_types.ModifyFieldPlan: "modify_plan",
    field_types.ReportFieldResult: "report_result",
    field_types.CorrectFieldResult: "correct_result",
    field_types.AttachFieldEvidence: "attach_evidence",
    field_types.VerifyFieldReport: "verify_result",
}


class DeskUnavailable(RuntimeError):
    """The response may already be committed; preserve its request identity."""


def _has_extension(db, name):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE name GLOB ? LIMIT 1", (name + "_*",)
        ).fetchone()
        is not None
    )


def _dispatch(db, case_id, *, settings=None):
    if not _has_extension(db, "dispatch"):
        return None
    check_dispatch_schema(db)
    if not db.execute("SELECT 1 FROM dispatch_settings WHERE case_id=?", (case_id,)).fetchone():
        return None
    state, _, profile = _settings(db, case_id) if settings is None else settings[:3]
    status = DispatchStore._status(db, state)
    raw = db.execute("SELECT * FROM delivery_allowance WHERE singleton=1").fetchone()
    if raw is None:
        raise ValueError("missing saved allowance")
    allowance = AllowanceSnapshot(
        *(
            raw[key]
            for key in ("scripted_limit", "scripted_used", "provider_limit", "provider_used")
        )
    )
    remaining = (
        allowance.scripted_limit - allowance.scripted_used
        if profile.mode == "SCRIPTED_SDK"
        else allowance.provider_limit - allowance.provider_used
    )
    return {
        "history_count": status.history_count,
        "suppressed_count": status.suppressed_count,
        "assessment_count": status.assessment_count,
        "active_attempt_id": status.active_attempt_id,
        "active_status": status.active_status,
        "health_evaluated_at": iso(status.health_evaluated_at),
        "numeric_event_id": status.numeric_event_id,
        "execution_mode": profile.mode,
        "remaining_attempts": remaining,
    }


def _work(db, case_id, now):
    work = []
    for raw in db.execute(
        "SELECT r.*,c.simulated FROM current_reviews r JOIN current_cases c ON c.case_id=r.case_id "
        "WHERE r.case_id=? AND r.status IN " + rows.ACTIVE + " ORDER BY r.kind LIMIT 3",
        (case_id,),
    ).fetchall():
        review = rows.record(raw)
        actions = ["MODIFY", "DEFER", "DISMISS", "CANCEL"]
        if review.status in ("PROPOSED", "DEFERRED") and (
            review.next_check_at is None
            or timedelta(0) < review.next_check_at - now <= timedelta(days=30)
        ):
            actions.insert(0, "APPROVE")
        evidence = [
            row[0]
            for row in db.execute(
                "SELECT event_id FROM current_review_evidence WHERE case_id=? AND task_id=? "
                "ORDER BY link_id DESC LIMIT 3",
                (case_id, review.task_id),
            )
        ]
        for identity in evidence:
            event_id(identity)
        work.append(
            {
                **work_receipt(review),
                "kind": review.kind,
                "reason": review.reason,
                "created_at": iso(review.created_at),
                "updated_at": iso(review.updated_at),
                "evidence_event_ids": evidence,
                "available_actions": actions,
            }
        )
    return work


def _dimensions(work, health, baseline_assessed):
    observation = any(item["kind"] == "OBSERVATION_REVIEW" for item in work)
    missing = set(health.missing_parameters) | set(health.null_parameters)
    coverage_issue = bool(missing or health.stale_parameters)
    coverage = (
        "MISSING"
        if set(health.policy.required_parameters) <= missing
        else "DEGRADED"
        if coverage_issue
        else "SUFFICIENT"
    )
    observation_status = (
        "CHANGE_UNDER_REVIEW"
        if observation
        else "MONITORING"
        if baseline_assessed
        else "BASELINE_PENDING"
    )
    return [
        {
            "id": "physical",
            "label": "Watershed recovery",
            "status": "NOT_ASSESSED",
            "detail": "Awaiting a scoped recovery assessment.",
        },
        {
            "id": "observation",
            "label": "Source-water review",
            "status": observation_status,
            "detail": (
                "An observation review is being followed."
                if observation
                else "No observation review is currently open."
                if baseline_assessed
                else "Awaiting the first source assessment."
            ),
        },
        {
            "id": "field",
            "label": "Field work & verification",
            "status": "NOT_PLANNED",
            "detail": "No structured field work has been planned.",
        },
        {
            "id": "coverage",
            "label": "Monitoring coverage",
            "status": coverage,
            "detail": "Some required readings are missing, stale or null."
            if coverage_issue
            else "Required station readings are available within the freshness policy.",
        },
    ]


class CurrentDesk:
    def __init__(
        self,
        cases: CaseStore,
        case_id: str,
        *,
        fields: FieldStore | None = None,
        principal: FieldPrincipal | None = None,
    ):
        if type(cases) is not CaseStore:
            raise ValueError("current desk requires an initialized case store")
        _identifier(case_id, "case_id")
        if fields is not None and (type(fields) is not FieldStore or fields.cases is not cases):
            raise ValueError("field desk requires the exact shared case store")
        if principal is not None and (
            fields is None
            or type(principal) is not FieldPrincipal
            or case_id not in principal.case_ids
        ):
            raise ValueError("field principal is not bound to this desk")
        self.cases, self.case_id = cases, case_id
        self.fields, self.principal = fields, principal

    def snapshot(self, *, now: datetime) -> dict:
        return self._read(now=now)[0]

    def _read(
        self,
        *,
        now: datetime,
        task_id: str | None = None,
        field_plan_id: str | None = None,
    ) -> tuple[dict, dict | None]:
        now = utc(now)
        with self.cases._connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            case = rows.case_row(db, self.case_id)
            config = _case_config(case)
            _expected(case["revision"])
            rows.check_time(case, now)
            field_context, history_settings = None, None
            if self.fields is None:
                require_legacy_context(db, self.case_id)
            else:
                from .field_desk_history import read_settings

                if not _has_extension(db, "field"):
                    raise ValueError("field desk schema is missing")
                check_field_schema(db)
                field_context = _load_field_context(
                    db, case, self.fields.locations, evaluated_at=now
                )
                history_settings = read_settings(db, self.fields, self.case_id)
                if (
                    history_settings is not None
                    and timestamp(history_settings[0]["updated_at"]) > now
                ):
                    raise ValueError("dispatch state is newer than the requested snapshot")
            health = _load_source_health(db, self.case_id, evaluated_at=now)
            monitor = WatchStore._monitor(db, config.monitor_id)
            fetched = timestamp(monitor["fetched_through"])
            start = timestamp(monitor["start_at"])
            if fetched < start:
                raise ValueError("source progress precedes monitoring start")
            source = {
                "source_id": health.source_id,
                "station_id": health.station_id,
                "label": gallinas_registry().get(health.source_id).label,
                "collected_through": fetched.isoformat() if fetched > start else None,
                "next_poll_at": timestamp(monitor["next_poll_at"]).isoformat(),
                "error_code": "COLLECTION_ERROR" if monitor["last_error_code"] else None,
                "missing_parameters": list(health.missing_parameters),
                "stale_parameters": list(health.stale_parameters),
                "null_parameters": list(health.null_parameters),
                "readings": [],
            }
            for name in ("observation_count", "event_count", "pending_count"):
                _expected(monitor[name])
                source[name] = monitor[name]
            for reading in health.series:
                flags = []
                if reading.version_id is None:
                    flags.append("MISSING")
                elif reading.value is None:
                    flags.append("NULL")
                if reading.observed_at is not None and now - reading.observed_at > timedelta(
                    seconds=health.policy.freshness_seconds
                ):
                    flags.append("STALE")
                source["readings"].append(
                    {
                        "parameter_code": reading.parameter_code,
                        "label": LABELS[reading.parameter_code],
                        "unit": reading.unit,
                        "value": None if reading.value is None else str(reading.value),
                        "observed_at": iso(reading.observed_at),
                        "retrieved_at": iso(reading.retrieved_at),
                        "approval_status": reading.approval_status,
                        "flags": flags,
                    }
                )
            latest = db.execute(
                "SELECT event_id FROM watch_events WHERE monitor_id=? "
                "ORDER BY interval_start DESC,revision DESC LIMIT 1",
                (config.monitor_id,),
            ).fetchone()
            interval = None
            if latest is not None:
                loader = _load_base_context if self.fields is not None else _load_context
                facts = loader(db, self.case_id, latest[0], evaluated_at=now).current
                interval = {
                    "event_id": facts.event_id,
                    "start": iso(facts.interval_start),
                    "end": iso(facts.interval_end),
                    "coverage": facts.interval_coverage,
                    "missing_parameters": list(facts.missing_parameters),
                }
            work = _work(db, self.case_id, now)
            dispatch = _dispatch(db, self.case_id, settings=history_settings)
            assessments = []
            baseline_assessed = False
            if _has_extension(db, "delivery"):
                check_delivery_schema(db)
                baseline_assessed = (
                    db.execute(
                        "SELECT 1 FROM delivery_attempts WHERE case_id=? AND status='COMMITTED' LIMIT 1",
                        (self.case_id,),
                    ).fetchone()
                    is not None
                )
                recent = db.execute(
                    "SELECT * FROM delivery_attempts WHERE case_id=? "
                    "ORDER BY evaluated_at DESC,attempt_id DESC LIMIT 10",
                    (self.case_id,),
                ).fetchall()
                for row in recent:
                    if timestamp(row["evaluated_at"]) > now or (
                        row["finished_at"] is not None and timestamp(row["finished_at"]) > now
                    ):
                        raise ValueError("saved assessment is newer than the requested snapshot")
                    if self.fields is None:
                        projected = summary(row)
                    else:
                        from .field_desk_history import read_assessment

                        projected = read_assessment(
                            db, self.fields, self.case_id, row, history_settings
                        )
                    if row["case_revision"] > case["revision"]:
                        raise WorkflowConflict("saved assessment exceeds the case revision")
                    assessments.append(projected)
            value = {
                "evaluated_at": now.isoformat(),
                "case": {
                    "case_id": self.case_id,
                    "revision": case["revision"],
                    "simulated": config.simulated,
                },
                "source": source,
                "interval": interval,
                "work": work,
                "dispatch": dispatch,
                "dimensions": _dimensions(work, health, baseline_assessed),
                "assessments": assessments,
            }
            if field_context is not None:
                from .field_desk_targets import proposal_targets

                value["current_field_work"] = project_field(field_context, self.principal)
                value["current_field_work"]["proposal_targets"] = proposal_targets(
                    db, field_context, self.principal
                )
                if field_context.state == "PRESENT":
                    value["dimensions"][2].update(
                        {
                            "status": "TRACKED",
                            "detail": "Field plans and results are recorded. Open each item for its status and verification.",
                        }
                    )
            current_work = None
            if task_id is not None:
                review = rows.record(rows.review_row(db, self.case_id, task_id))
                if review.revision > case["revision"]:
                    raise WorkflowConflict("saved work exceeds the case revision")
                current_work = work_receipt(review)
            if field_plan_id is not None:
                if field_context is None:
                    raise ValueError("field work requires a field-enabled desk")
                current_work = project_work(
                    field_snapshot(db, self.case_id, field_plan_id),
                    field_context,
                    self.principal,
                    detail=True,
                )
            return value, current_work

    def field_work(self, plan_id: str, *, now: datetime) -> dict:
        _identifier(plan_id, "plan_id")
        snapshot, selected = self._read(now=now, field_plan_id=plan_id)
        return {"snapshot": snapshot, "selected_field_work": selected}

    def respond_field(
        self,
        command: object,
        *,
        request_id: str,
        expected_case_revision: int,
        now: datetime,
    ) -> dict:
        if self.fields is None or self.principal is None:
            raise ValueError("field responses require a trusted field operator")
        method = FIELD_METHODS.get(type(command))
        if method is None or command.private_note != "":
            raise ValueError("field desk requires a typed command without a private note")
        receipt = getattr(self.fields, method)(
            self.case_id,
            command,
            principal=self.principal,
            request_id=request_id,
            expected_case_revision=expected_case_revision,
            now=now,
        )
        try:
            snapshot, selected = self._read(now=now, field_plan_id=receipt.plan.plan_id)
        except Exception as error:
            raise DeskUnavailable("saved field response view is temporarily unavailable") from error
        return {"receipt": _plain(receipt), "snapshot": snapshot, "selected_field_work": selected}

    def respond(self, action: HumanAction, *, request_id: str, now: datetime) -> dict:
        receipt = self.cases.act(self.case_id, action, request_id=request_id, now=now)
        try:
            snapshot, current_work = self._read(now=now, task_id=receipt.task_id)
        except Exception as error:
            raise DeskUnavailable("saved response view is temporarily unavailable") from error
        return {
            "receipt": work_receipt(receipt),
            "snapshot": snapshot,
            "current_work": current_work,
        }

    def assessment(self, attempt_id: str) -> dict:
        with self.cases._connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            case = rows.case_row(db, self.case_id)
            if not _has_extension(db, "delivery"):
                raise KeyError("no saved assessment")
            check_delivery_schema(db)
            raw = _attempt(db, attempt_id)
            if raw["case_id"] != self.case_id:
                raise KeyError("no saved assessment in this case")
            _expected(raw["case_revision"])
            if raw["case_revision"] > case["revision"]:
                raise WorkflowConflict("saved assessment exceeds the case revision")
            if self.fields is not None:
                from .field_desk_history import read_assessment, read_settings

                if not _has_extension(db, "field"):
                    raise ValueError("field desk schema is missing")
                check_field_schema(db)
                settings = read_settings(db, self.fields, self.case_id)
                return read_assessment(db, self.fields, self.case_id, raw, settings, detail=True)
            attention = None
            if _has_extension(db, "dispatch"):
                check_dispatch_schema(db)
                item = db.execute(
                    "SELECT attention_json FROM dispatch_attempts WHERE case_id=? AND attempt_id=?",
                    (self.case_id, attempt_id),
                ).fetchone()
                if item is not None:
                    attention = item[0]
            return assessment_detail(raw, attention)
