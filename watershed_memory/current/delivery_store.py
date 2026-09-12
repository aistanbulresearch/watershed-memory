"""Reserve before inference; commit current work and source delivery atomically.

This internal explicit-request boundary performs no network or model calls.
Held attempts require deliberate reconciliation, never an automatic retry.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime

from . import case_records as rows
from .assessment_types import _canonical_json
from .case_store import CaseStore
from .case_types import EvidenceLink, ReviewDraft, WorkflowConflict, _identifier, _text
from .context import _load_context
from .delivery_schema import ensure_schema
from .delivery_types import (
    _ATTEMPT,
    AllowanceExceeded,
    AllowanceSnapshot,
    DeliveryHeld,
    DeliveryReceipt,
    DeliveryReservation,
    InvocationAllowance,
    InvocationProfile,
)
from .fact_validation import event_id as validate_event_id
from .fact_validation import timestamp, utc
from .health_schema import ensure_schema as ensure_health_schema
from .strands import CurrentExecution, CurrentFailure
from .tools import _json, validate_assessment


def _encoded(value: object, *, limit: int) -> str:
    encoded = _json(value)
    try:
        if len(encoded.encode("utf-8")) > limit:
            raise ValueError("delivery record exceeds its byte bound")
    except UnicodeEncodeError as error:
        raise ValueError("invalid delivery record UTF-8") from error
    return encoded


def _attempt(db: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
    if type(attempt_id) is not str or not _ATTEMPT.fullmatch(attempt_id):
        raise ValueError("invalid attempt_id")
    result = db.execute(
        "SELECT * FROM delivery_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if result is None:
        raise KeyError("unknown delivery attempt")
    return result


def _receipt(raw: sqlite3.Row) -> DeliveryReceipt:
    return DeliveryReceipt(
        raw["attempt_id"],
        raw["request_id"],
        raw["case_id"],
        raw["event_id"],
        InvocationProfile(**json.loads(raw["profile_json"])),
        raw["status"],
        bool(raw["source_delivery"]),
        timestamp(raw["finished_at"] or raw["evaluated_at"]),
        tuple(rows.record(item) for item in json.loads(raw["work_json"])),
        raw["execution_json"],
        raw["failure_json"],
        raw["reason"],
    )


def _time(db: sqlite3.Connection, attempt: sqlite3.Row, now: datetime) -> sqlite3.Row:
    if now < timestamp(attempt["finished_at"] or attempt["evaluated_at"]):
        raise ValueError("delivery time precedes its saved state")
    case = rows.case_row(db, attempt["case_id"])
    rows.check_time(case, now)
    return case


def _identity(attempt: sqlite3.Row, result: CurrentExecution | CurrentFailure) -> None:
    profile = InvocationProfile(
        result.mode, result.model_id, result.instruction_version, result.sdk_version
    )
    if rows.encode(asdict(profile)) != attempt["profile_json"]:
        raise ValueError("execution profile differs from the reserved invocation")
    identity = result.assessment if type(result) is CurrentExecution else result
    if (identity.case_id, identity.case_revision, identity.policy_digest, identity.event_id) != (
        attempt["case_id"],
        attempt["case_revision"],
        attempt["coverage_digest64"],
        attempt["event_id"],
    ):
        raise ValueError("execution identity differs from the reserved context")


class DeliveryStore:
    def __init__(self, cases: CaseStore, *, allowance: InvocationAllowance | None = None):
        if type(cases) is not CaseStore:
            raise ValueError("delivery requires an initialized current case store")
        if allowance is not None and type(allowance) is not InvocationAllowance:
            raise ValueError("invalid invocation allowance")
        self.cases = cases
        with cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db, allowance)

    def allowance(self) -> AllowanceSnapshot:
        with self.cases._connect() as db:
            raw = db.execute("SELECT * FROM delivery_allowance WHERE singleton=1").fetchone()
            if raw is None:
                raise ValueError("missing installation allowance")
            return AllowanceSnapshot(
                raw["scripted_limit"],
                raw["scripted_used"],
                raw["provider_limit"],
                raw["provider_used"],
            )

    def get(self, case_id: str, request_id: str) -> DeliveryReceipt:
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        with self.cases._connect() as db:
            raw = db.execute(
                "SELECT * FROM delivery_attempts WHERE case_id=? AND request_id=?",
                (case_id, request_id),
            ).fetchone()
            if raw is None:
                raise KeyError("unknown case delivery request")
            return _receipt(raw)

    def reserve(
        self,
        case_id: str,
        event_id: str,
        *,
        request_id: str,
        profile: InvocationProfile,
        source_delivery: bool,
        now: datetime,
        include_source_health: bool = False,
    ) -> DeliveryReservation | DeliveryReceipt:
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
                include_source_health=include_source_health,
            )

    def _reserve(
        self,
        db,
        case_id: str,
        event_id: str,
        *,
        request_id: str,
        profile: InvocationProfile,
        source_delivery: bool,
        now: datetime,
        include_source_health: bool = False,
        prior_event_ids: tuple[str, ...] | None = None,
    ) -> DeliveryReservation | DeliveryReceipt:
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        validate_event_id(event_id)
        now = utc(now)
        if (
            type(profile) is not InvocationProfile
            or type(source_delivery) is not bool
            or type(include_source_health) is not bool
        ):
            raise ValueError("invalid delivery request")
        profile_json = _encoded(profile, limit=2048)
        request = {"event_id": event_id, "profile": profile, "source_delivery": source_delivery}
        if include_source_health:
            request["include_source_health"] = True
        if prior_event_ids is not None:
            if type(prior_event_ids) is not tuple or len(prior_event_ids) > 3:
                raise ValueError("invalid reserved prior event IDs")
            for identity in prior_event_ids:
                validate_event_id(identity)
            if event_id in prior_event_ids or len(set(prior_event_ids)) != len(prior_event_ids):
                raise ValueError("duplicate reserved prior event IDs")
            request["prior_event_ids"] = prior_event_ids
        encoded = _encoded(request, limit=4096)
        if not db.in_transaction:
            raise ValueError("delivery transaction is required")
        existing = db.execute(
            "SELECT * FROM delivery_attempts WHERE case_id=? AND request_id=?",
            (case_id, request_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["input_hash64"] != rows.digest(encoded)
                or existing["input_json"] != encoded
            ):
                raise WorkflowConflict("delivery request ID already binds different input")
            if existing["status"] == "COMMITTED":
                return _receipt(existing)
            raise DeliveryHeld("request is already reserved or reconciled; do not invoke again")
        held = db.execute(
            "SELECT attempt_id FROM delivery_attempts WHERE case_id=? "
            "AND status IN ('RESERVED','FAILED','STALE') LIMIT 1",
            (case_id,),
        ).fetchone()
        if held is not None:
            raise DeliveryHeld("case has an unresolved invocation")
        context = _load_context(
            db,
            case_id,
            event_id,
            evaluated_at=now,
            include_source_health=include_source_health,
            prior_event_ids=prior_event_ids,
        )
        if profile.mode == "SCRIPTED_SDK" and not context.simulated:
            raise ValueError("scripted invocation requires an explicitly simulated case")
        monitor = context.current.monitor_id
        if source_delivery:
            if context.current.case_id != case_id:
                raise ValueError("isolated work cannot acknowledge the canonical source queue")
            pending = db.execute(
                "SELECT status FROM watch_outbox WHERE event_id=? AND monitor_id=?",
                (event_id, monitor),
            ).fetchone()
            if pending is None or pending[0] != "PENDING":
                raise WorkflowConflict("source event is not pending canonical delivery")
        column = "scripted" if profile.mode == "SCRIPTED_SDK" else "provider"
        changed = db.execute(
            f"UPDATE delivery_allowance SET {column}_used={column}_used+1 "
            f"WHERE singleton=1 AND {column}_used<{column}_limit"
        ).rowcount
        if changed != 1:
            raise AllowanceExceeded("installation invocation allowance is exhausted")
        identity = "attempt-" + uuid.uuid4().hex
        prior = rows.encode([item.event_id for item in context.prior])
        review_refs = _encoded(
            tuple(
                (review.task_id, review.revision, evidence)
                for review, (_, evidence) in zip(
                    context.reviews, context.review_evidence, strict=True
                )
            ),
            limit=4096,
        )
        db.execute(
            "INSERT INTO delivery_attempts(attempt_id,case_id,monitor_id,event_id,request_id,"
            "input_json,input_hash64,profile_json,mode,source_delivery,status,case_revision,"
            "coverage_digest64,context_digest64,prior_ids_json,review_refs_json,evaluated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                case_id,
                monitor,
                event_id,
                request_id,
                encoded,
                rows.digest(encoded),
                profile_json,
                profile.mode,
                int(source_delivery),
                "RESERVED",
                context.case_revision,
                context.policy_digest,
                rows.digest(_json(context)),
                prior,
                review_refs,
                now.isoformat(),
            ),
        )
        if include_source_health:
            ensure_health_schema(db, create=True)
            db.execute(
                "INSERT INTO health_attempts VALUES(?,?)",
                (
                    identity,
                    _encoded(context.source_health.version_ids, limit=512),
                ),
            )
        return DeliveryReservation(identity, request_id, profile, source_delivery, context, now)

    def commit(
        self, attempt_id: str, execution: CurrentExecution, *, now: datetime
    ) -> DeliveryReceipt:
        if type(execution) is not CurrentExecution:
            raise ValueError("expected a typed current execution")
        now = utc(now)
        encoded = _encoded(execution, limit=524288)
        _canonical_json(encoded, "execution", 524288)
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._commit(db, attempt_id, execution, now=now)

    def _commit(
        self, db, attempt_id: str, execution: CurrentExecution, *, now: datetime
    ) -> DeliveryReceipt:
        if type(execution) is not CurrentExecution:
            raise ValueError("expected a typed current execution")
        now = utc(now)
        encoded = _encoded(execution, limit=524288)
        _canonical_json(encoded, "execution", 524288)
        if not db.in_transaction:
            raise ValueError("delivery transaction is required")
        attempt = _attempt(db, attempt_id)
        _identity(attempt, execution)
        if attempt["status"] == "COMMITTED":
            if attempt["execution_json"] != encoded:
                raise WorkflowConflict("committed attempt already has a different execution")
            return _receipt(attempt)
        if attempt["status"] != "RESERVED":
            raise DeliveryHeld("attempt is held or reconciled and cannot accept this result")
        case = _time(db, attempt, now)
        request = json.loads(attempt["input_json"])
        include_health = request.get("include_source_health", False)
        if type(include_health) is not bool:
            raise ValueError("invalid reserved source-health mode")
        source_pins = None
        if include_health:
            ensure_health_schema(db)
            health_row = db.execute(
                "SELECT version_ids_json FROM health_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if health_row is None:
                raise ValueError("missing reserved source-health references")
            source_pins = tuple(json.loads(health_row[0]))
        context = _load_context(
            db,
            attempt["case_id"],
            attempt["event_id"],
            evaluated_at=timestamp(attempt["evaluated_at"]),
            prior_event_ids=tuple(json.loads(attempt["prior_ids_json"])),
            review_refs=tuple(
                (item[0], item[1], tuple(item[2]))
                for item in json.loads(attempt["review_refs_json"])
            ),
            reserved_revision=attempt["case_revision"],
            include_source_health=include_health,
            source_version_ids=source_pins,
        )
        if rows.digest(_json(context)) != attempt["context_digest64"]:
            raise WorkflowConflict("reserved context differs from trusted source replay")
        validate_assessment(context, execution.assessment)
        if case["revision"] != attempt["case_revision"]:
            db.execute(
                "UPDATE delivery_attempts SET status='STALE',execution_json=?,finished_at=? "
                "WHERE attempt_id=?",
                (encoded, now.isoformat(), attempt_id),
            )
            return _receipt(_attempt(db, attempt_id))
        work = []
        revision = context.case_revision
        for index, decision in enumerate(execution.assessment.decisions):
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
                saved = self.cases._stage_review(db, context.case_id, draft, **args)
            elif decision.disposition == "CONTINUE_EXISTING_REVIEW":
                link = EvidenceLink(decision.target_task_id, decision.event_id, decision.reason)
                saved = self.cases._link_evidence(db, context.case_id, link, **args)
            else:
                continue
            work.append(saved)
            revision += 1
        if attempt["source_delivery"]:
            self._acknowledge(db, attempt)
        db.execute(
            "UPDATE current_cases SET revision=revision+1,updated_at=? WHERE case_id=?",
            (now.isoformat(), context.case_id),
        )
        db.execute(
            "UPDATE delivery_attempts SET status='COMMITTED',execution_json=?,work_json=?,"
            "finished_at=? WHERE attempt_id=?",
            (encoded, _encoded(tuple(work), limit=20000), now.isoformat(), attempt_id),
        )
        return _receipt(_attempt(db, attempt_id))

    @staticmethod
    def _acknowledge(db: sqlite3.Connection, attempt: sqlite3.Row) -> None:
        changed = db.execute(
            "UPDATE watch_outbox SET status='DONE' WHERE event_id=? AND monitor_id=? "
            "AND status='PENDING'",
            (attempt["event_id"], attempt["monitor_id"]),
        ).rowcount
        if changed != 1:
            raise WorkflowConflict("canonical source delivery is no longer pending")
        changed = db.execute(
            "UPDATE monitors SET pending_count=pending_count-1 WHERE monitor_id=? "
            "AND pending_count>0",
            (attempt["monitor_id"],),
        ).rowcount
        if changed != 1:
            raise WorkflowConflict("canonical source pending counter is inconsistent")

    def fail(self, attempt_id: str, failure: CurrentFailure, *, now: datetime) -> DeliveryReceipt:
        if type(failure) is not CurrentFailure:
            raise ValueError("expected a typed sanitized current failure")
        now = utc(now)
        encoded = _encoded(failure, limit=524288)
        _canonical_json(encoded, "failure", 524288)
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = _attempt(db, attempt_id)
            _identity(attempt, failure)
            if attempt["status"] == "FAILED":
                if attempt["failure_json"] != encoded:
                    raise WorkflowConflict("failed attempt already binds a different failure")
                return _receipt(attempt)
            if attempt["status"] != "RESERVED":
                raise DeliveryHeld("attempt cannot accept another failure")
            _time(db, attempt, now)
            db.execute(
                "UPDATE delivery_attempts SET status='FAILED',failure_json=?,finished_at=? "
                "WHERE attempt_id=?",
                (encoded, now.isoformat(), attempt_id),
            )
            return _receipt(_attempt(db, attempt_id))

    def abandon(
        self, case_id: str, request_id: str, *, reason: str, now: datetime
    ) -> DeliveryReceipt:
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        _text(reason, "reconciliation reason", 8, 700)
        now = utc(now)
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = db.execute(
                "SELECT * FROM delivery_attempts WHERE case_id=? AND request_id=?",
                (case_id, request_id),
            ).fetchone()
            if attempt is None:
                raise KeyError("unknown case delivery request")
            if attempt["status"] == "ABANDONED":
                if attempt["reason"] != reason:
                    raise WorkflowConflict("reconciliation already binds a different reason")
                return _receipt(attempt)
            if attempt["status"] == "COMMITTED":
                raise WorkflowConflict("a committed decision cannot be abandoned")
            _time(db, attempt, now)
            db.execute(
                "UPDATE delivery_attempts SET status='ABANDONED',reason=?,finished_at=? "
                "WHERE attempt_id=?",
                (reason, now.isoformat(), attempt["attempt_id"]),
            )
            return _receipt(_attempt(db, attempt["attempt_id"]))
