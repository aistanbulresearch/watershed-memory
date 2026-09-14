"""Reserve and atomically deliver one field-aware turn without invoking a model."""

import uuid

from . import case_records as rows
from . import field_delivery_records as records
from .case_types import EvidenceLink, ReviewDraft, WorkflowConflict, _identifier
from .context3_schema import ensure_schema
from .context_v3 import _load_context_v3, _require_transaction
from .context_v3_reference import capture_context_v3, encode_reference_v3
from .delivery_store import DeliveryStore, _attempt, _encoded, _time
from .delivery_types import AllowanceExceeded, DeliveryHeld, InvocationProfile
from .fact_validation import event_id as validate_event_id
from .fact_validation import utc
from .field_agent import proposal_command, require_current_site
from .field_delivery_codec import encode_execution, encode_failure
from .field_delivery_types import CurrentExecutionV3, CurrentFailureV3, FieldDeliveryReservation
from .field_store import FieldStore
from .field_tools import validate_assessment_v3
from .health_schema import ensure_schema as ensure_health_schema


class FieldDeliveryStore:
    def __init__(self, journal: DeliveryStore, fields: FieldStore):
        if (
            type(journal) is not DeliveryStore
            or type(fields) is not FieldStore
            or journal.cases is not fields.cases
        ):
            raise ValueError("field delivery requires the same exact case store")
        self.journal, self.fields, self.cases = journal, fields, journal.cases
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)

    def _check(self, db):
        _require_transaction(db, self.fields)
        ensure_schema(db)

    def get(self, case_id, request_id):
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        with self.cases._connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            self._check(db)
            attempt = db.execute(
                "SELECT * FROM delivery_attempts WHERE case_id=? AND request_id=?",
                (case_id, request_id),
            ).fetchone()
            if attempt is None:
                raise KeyError("unknown field delivery request")
            return records.receipt(db, self.fields, attempt)

    def reserve(self, case_id, event_id, *, request_id, profile, source_delivery, now):
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._reserve(
                db,
                case_id,
                event_id,
                request_id=request_id,
                profile=profile,
                source_delivery=source_delivery,
                now=now,
            )

    def _reserve(
        self,
        db,
        case_id,
        event_id,
        *,
        request_id,
        profile,
        source_delivery,
        now,
        prior_event_ids=None,
    ):
        self._check(db)
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        validate_event_id(event_id)
        now = utc(now)
        if (
            type(profile) is not InvocationProfile
            or profile.instruction_version != "watershed-current-v3"
            or type(source_delivery) is not bool
        ):
            raise ValueError("invalid field-aware delivery request")
        request = {
            "event_id": event_id,
            "profile": profile,
            "source_delivery": source_delivery,
            "include_source_health": True,
            "context_version": 3,
        }
        if prior_event_ids is not None:
            if type(prior_event_ids) is not tuple or len(prior_event_ids) > 3:
                raise ValueError("invalid field-aware prior selection")
            for item in prior_event_ids:
                validate_event_id(item)
            if event_id in prior_event_ids or len(set(prior_event_ids)) != len(prior_event_ids):
                raise ValueError("duplicate field-aware prior selection")
            request["prior_event_ids"] = prior_event_ids
        encoded = _encoded(request, limit=4096)
        existing = db.execute(
            "SELECT * FROM delivery_attempts WHERE case_id=? AND request_id=?",
            (case_id, request_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["input_hash64"] != rows.digest(encoded)
                or existing["input_json"] != encoded
            ):
                raise WorkflowConflict("request already binds different field delivery input")
            if existing["status"] == "COMMITTED":
                return records.receipt(db, self.fields, existing)
            raise DeliveryHeld("field turn is already reserved or reconciled; do not invoke again")
        if db.execute(
            "SELECT 1 FROM delivery_attempts WHERE case_id=? AND status IN ('RESERVED','FAILED','STALE') LIMIT 1",
            (case_id,),
        ).fetchone():
            raise DeliveryHeld("case has an unresolved invocation")
        context = _load_context_v3(
            db, self.fields, case_id, event_id, evaluated_at=now, prior_event_ids=prior_event_ids
        )
        base = context.base
        if profile.mode == "SCRIPTED_SDK" and not base.simulated:
            raise ValueError("scripted invocation requires a simulated field case")
        if source_delivery:
            if base.current.case_id != case_id:
                raise ValueError("isolated field work cannot acknowledge the source queue")
            pending = db.execute(
                "SELECT status FROM watch_outbox WHERE event_id=? AND monitor_id=?",
                (event_id, base.current.monitor_id),
            ).fetchone()
            if pending is None or pending[0] != "PENDING":
                raise WorkflowConflict("source event is not pending canonical delivery")
        reference = capture_context_v3(db, self.fields, context)
        reference_json = encode_reference_v3(reference)
        column = "scripted" if profile.mode == "SCRIPTED_SDK" else "provider"
        changed = db.execute(
            f"UPDATE delivery_allowance SET {column}_used={column}_used+1 WHERE singleton=1 AND {column}_used<{column}_limit"
        ).rowcount
        if changed != 1:
            raise AllowanceExceeded("installation invocation allowance is exhausted")
        identity = "attempt-" + uuid.uuid4().hex
        review_refs = _encoded(
            tuple(
                (review.task_id, review.revision, evidence)
                for review, (_, evidence) in zip(base.reviews, base.review_evidence, strict=True)
            ),
            limit=4096,
        )
        db.execute(
            "INSERT INTO delivery_attempts(attempt_id,case_id,monitor_id,event_id,request_id,input_json,input_hash64,profile_json,mode,source_delivery,status,case_revision,coverage_digest64,context_digest64,prior_ids_json,review_refs_json,evaluated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                case_id,
                base.current.monitor_id,
                event_id,
                request_id,
                encoded,
                rows.digest(encoded),
                _encoded(profile, limit=2048),
                profile.mode,
                int(source_delivery),
                "RESERVED",
                base.case_revision,
                base.policy_digest,
                reference.context_digest,
                rows.encode([item.event_id for item in base.prior]),
                review_refs,
                now.isoformat(),
            ),
        )
        ensure_health_schema(db, create=True)
        db.execute(
            "INSERT INTO health_attempts VALUES(?,?)",
            (identity, _encoded(base.source_health.version_ids, limit=512)),
        )
        db.execute(
            "INSERT INTO context3_attempts(attempt_id,case_id,reference_json,context_digest64,field_digest64,created_at) VALUES(?,?,?,?,?,?)",
            (
                identity,
                case_id,
                reference_json,
                reference.context_digest,
                context.field_digest,
                now.isoformat(),
            ),
        )
        return FieldDeliveryReservation(
            identity, request_id, profile, source_delivery, context, now
        )

    def commit(self, attempt_id, execution, *, now):
        if type(execution) is not CurrentExecutionV3:
            raise ValueError("expected a typed field-aware execution")
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._commit(db, attempt_id, execution, now=now)

    def _commit(self, db, attempt_id, execution, *, now):
        self._check(db)
        if type(execution) is not CurrentExecutionV3:
            raise ValueError("expected a typed field-aware execution")
        now, encoded = utc(now), encode_execution(execution)
        attempt = _attempt(db, attempt_id)
        context = records.restore_reserved(db, self.fields, attempt)
        records.validate_identity(attempt, context, execution)
        if attempt["status"] == "COMMITTED":
            if attempt["execution_json"] != encoded:
                raise WorkflowConflict("committed field turn has a different execution")
            return records.receipt(db, self.fields, attempt)
        if attempt["status"] != "RESERVED":
            raise DeliveryHeld("field turn is held or reconciled")
        case = _time(db, attempt, now)
        validate_assessment_v3(context, execution.assessment)
        proposal = execution.assessment.field.proposal
        stale = case["revision"] != context.base.case_revision
        if proposal is not None and not stale:
            try:
                require_current_site(self.fields, context, proposal, now)
            except (KeyError, ValueError):
                stale = True
        if stale:
            db.execute(
                "UPDATE delivery_attempts SET status='STALE',execution_json=?,finished_at=? WHERE attempt_id=?",
                (encoded, now.isoformat(), attempt_id),
            )
            return records.receipt(db, self.fields, _attempt(db, attempt_id))
        # The validated intent is visible only inside this transaction until commit.
        db.execute(
            "UPDATE delivery_attempts SET execution_json=? WHERE attempt_id=?",
            (encoded, attempt_id),
        )
        work, revision = [], context.base.case_revision
        for index, decision in enumerate(execution.assessment.base.decisions):
            args = dict(
                request_id=f"{attempt_id}-{index}", expected_case_revision=revision, now=now
            )
            if decision.disposition == "PROPOSE_REVIEW":
                draft = ReviewDraft(
                    decision.kind,
                    decision.title,
                    decision.reason,
                    decision.event_id,
                    decision.next_check_at,
                )
                saved = self.cases._stage_review(db, context.base.case_id, draft, **args)
            elif decision.disposition == "CONTINUE_EXISTING_REVIEW":
                link = EvidenceLink(decision.target_task_id, decision.event_id, decision.reason)
                saved = self.cases._link_evidence(db, context.base.case_id, link, **args)
            else:
                continue
            work.append(saved)
            revision += 1
        if proposal is not None:
            self.fields._stage_agent_plan(
                db,
                context.base.case_id,
                proposal_command(context, proposal),
                attempt_id=attempt_id,
                request_id=f"{attempt_id}-field-0",
                expected_case_revision=revision,
                reserved_context_digest=attempt["context_digest64"],
                now=now,
            )
        if attempt["source_delivery"]:
            self.journal._acknowledge(db, attempt)
        db.execute(
            "UPDATE current_cases SET revision=revision+1,updated_at=? WHERE case_id=?",
            (now.isoformat(), context.base.case_id),
        )
        db.execute(
            "UPDATE delivery_attempts SET status='COMMITTED',execution_json=?,work_json=?,finished_at=? WHERE attempt_id=?",
            (encoded, _encoded(tuple(work), limit=20000), now.isoformat(), attempt_id),
        )
        return records.receipt(db, self.fields, _attempt(db, attempt_id))

    def fail(self, attempt_id, failure, *, now):
        if type(failure) is not CurrentFailureV3:
            raise ValueError("expected a typed sanitized field-aware failure")
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._fail(db, attempt_id, failure, now=now)

    def _fail(self, db, attempt_id, failure, *, now):
        self._check(db)
        if type(failure) is not CurrentFailureV3:
            raise ValueError("expected a typed sanitized field-aware failure")
        now, encoded = utc(now), encode_failure(failure)
        attempt = _attempt(db, attempt_id)
        context = records.restore_reserved(db, self.fields, attempt)
        records.validate_identity(attempt, context, failure)
        if attempt["status"] == "FAILED":
            if attempt["failure_json"] != encoded:
                raise WorkflowConflict("failed field turn already has different evidence")
            return records.receipt(db, self.fields, attempt)
        if attempt["status"] != "RESERVED":
            raise DeliveryHeld("field turn cannot accept another failure")
        _time(db, attempt, now)
        records.validate_failure(context, failure)
        db.execute(
            "UPDATE delivery_attempts SET status='FAILED',failure_json=?,finished_at=? WHERE attempt_id=?",
            (encoded, now.isoformat(), attempt_id),
        )
        return records.receipt(db, self.fields, _attempt(db, attempt_id))
