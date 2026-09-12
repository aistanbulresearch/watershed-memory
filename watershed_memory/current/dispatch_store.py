"""Select attention inside SQLite; run the model outside the transaction."""

import json
import uuid
from datetime import datetime

from ..watch.store import WatchStore
from . import case_records as rows
from .assessment_types import _canonical_json
from .attention import AttentionPolicy
from .case_types import WorkflowConflict, _identifier
from .context import _case_config, _load_context
from .context_reference import capture_context, decode_reference, encode_reference, restore_context
from .delivery_store import DeliveryStore, _attempt
from .delivery_types import DeliveryReservation, InvocationProfile
from .dispatch_replay import (
    capture_admission,
    decode_policy,
    evaluate_admission,
    handled_keys,
    restore_admission,
)
from .dispatch_schema import ensure_schema
from .dispatch_types import DispatchStatus, DispatchStep
from .fact_validation import timestamp, utc
from .facts import compare_intervals
from .strands import HEALTH_INSTRUCTION_VERSION, CurrentExecution, CurrentFailure
from .tools import _json


def _settings(db, case_id):
    _identifier(case_id, "case_id")
    row = db.execute("SELECT * FROM dispatch_settings WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise KeyError("case is not activated for dispatch")
    # Invocation rows are bounded by the fixed installation allowance (at most
    # 20,000 across all cases); both joins use identity/case indexes. Never
    # silently adopt work committed through the lower-level journal API.
    external = db.execute(
        "SELECT 1 FROM delivery_attempts d LEFT JOIN dispatch_attempts a "
        "ON a.attempt_id=d.attempt_id AND a.case_id=d.case_id "
        "WHERE d.case_id=? AND a.attempt_id IS NULL LIMIT 1",
        (case_id,),
    ).fetchone()
    if external is not None:
        raise WorkflowConflict("activated case has a delivery outside dispatch")
    policy = decode_policy(row["policy_json"])
    _canonical_json(row["profile_json"], "invocation profile", 2048)
    try:
        profile = InvocationProfile(**json.loads(row["profile_json"]))
    except TypeError as error:
        raise ValueError("invalid dispatch profile fields") from error
    if (
        policy.policy_digest != row["policy_digest"]
        or profile.instruction_version != HEALTH_INSTRUCTION_VERSION
    ):
        raise WorkflowConflict("dispatch settings differ from policy or instruction version")
    case = _case_config(rows.case_row(db, case_id))
    if row["monitor_id"] != case.monitor_id or (
        profile.mode == "SCRIPTED_SDK" and not case.simulated
    ):
        raise WorkflowConflict("dispatch scope or execution mode differs from the case")
    return row, policy, profile


def _baseline(db, raw):
    return None if raw is None else restore_context(db, decode_reference(raw))


def _acknowledge(db, monitor, event, disposition):
    changed = db.execute(
        "UPDATE watch_outbox SET status=? WHERE monitor_id=? AND event_id=? AND status='PENDING'",
        (disposition, monitor, event),
    ).rowcount
    if changed != 1:
        raise WorkflowConflict("source event is no longer pending")
    changed = db.execute(
        "UPDATE monitors SET pending_count=pending_count-1 WHERE monitor_id=? AND pending_count>0",
        (monitor,),
    ).rowcount
    if changed != 1:
        raise WorkflowConflict("source pending counter is inconsistent")


def _outcome(db, state, event_id, kind, now, replay=None, attention=None, attempt=None):
    db.execute(
        "INSERT INTO dispatch_source_outcomes VALUES(?,?,?,?,?,?,?,?)",
        (
            state["case_id"],
            state["monitor_id"],
            event_id,
            kind,
            now.isoformat(),
            replay,
            attention,
            attempt,
        ),
    )


class DispatchStore:
    def __init__(self, journal: DeliveryStore):
        if type(journal) is not DeliveryStore:
            raise ValueError("dispatch requires an initialized delivery journal")
        self.journal = journal
        with journal.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db, create=True)

    def activate(
        self, case_id: str, policy: AttentionPolicy, profile: InvocationProfile, *, now: datetime
    ):
        _identifier(case_id, "case_id")
        now = utc(now)
        if type(policy) is not AttentionPolicy or type(profile) is not InvocationProfile:
            raise ValueError("typed attention policy and invocation profile are required")
        if profile.instruction_version != HEALTH_INSTRUCTION_VERSION:
            raise ValueError("automatic dispatch requires source-health instructions")
        policy_json, profile_json = _json(policy), _json(profile)
        decode_policy(policy_json)
        with self.journal.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)
            case_row = rows.case_row(db, case_id)
            config = _case_config(case_row)
            monitor = WatchStore._config(WatchStore._monitor(db, config.monitor_id))
            if monitor.case_id != case_id:
                raise ValueError("dispatch is limited to the canonical source-origin case")
            if profile.mode == "SCRIPTED_SDK" and not config.simulated:
                raise ValueError("scripted dispatch requires an explicitly simulated case")
            if any(
                not any(
                    r.parameter_code == s.parameter_code and r.unit == s.unit
                    for s in monitor.series
                )
                for r in policy.rules
            ):
                raise ValueError("attention rules do not match the configured source")
            existing = db.execute(
                "SELECT * FROM dispatch_settings WHERE case_id=?", (case_id,)
            ).fetchone()
            if existing is not None:
                _settings(db, case_id)
                if (existing["policy_json"], existing["profile_json"]) != (
                    policy_json,
                    profile_json,
                ):
                    raise WorkflowConflict("dispatch activation already binds different settings")
                return
            rows.check_time(case_row, now)
            if (
                case_row["revision"] != 0
                or db.execute(
                    "SELECT 1 FROM delivery_attempts WHERE case_id=? LIMIT 1", (case_id,)
                ).fetchone()
            ):
                raise WorkflowConflict(
                    "activate dispatch before existing work or delivery attempts"
                )
            db.execute(
                "INSERT INTO dispatch_settings(case_id,monitor_id,policy_json,policy_digest,"
                "profile_json,activated_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    case_id,
                    config.monitor_id,
                    policy_json,
                    policy.policy_digest,
                    profile_json,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    @staticmethod
    def _status(db, state):
        numeric = (
            None
            if state["numeric_context_json"] is None
            else decode_reference(state["numeric_context_json"])
        )
        health = (
            None
            if state["health_context_json"] is None
            else decode_reference(state["health_context_json"])
        )
        if any(ref is not None and ref.case_id != state["case_id"] for ref in (numeric, health)):
            raise WorkflowConflict("dispatch baseline is outside this case")
        attempt = (
            None if state["active_attempt_id"] is None else _attempt(db, state["active_attempt_id"])
        )
        if attempt is not None and attempt["case_id"] != state["case_id"]:
            raise WorkflowConflict("dispatch active attempt is outside this case")
        return DispatchStatus(
            state["case_id"],
            state["monitor_id"],
            timestamp(state["activated_at"]),
            timestamp(state["updated_at"]),
            state["initial_event_id"],
            state["history_count"],
            state["suppressed_count"],
            state["assessment_count"],
            None if numeric is None else numeric.event_id,
            None if health is None else health.evaluated_at,
            state["active_attempt_id"],
            None if attempt is None else attempt["status"],
        )

    def status(self, case_id: str) -> DispatchStatus:
        with self.journal.cases._connect() as db:
            db.execute("BEGIN")
            ensure_schema(db)
            state, _, _ = _settings(db, case_id)
            return self._status(db, state)

    def prepare(self, case_id: str, *, now: datetime) -> DispatchStep:
        now = utc(now)
        with self.journal.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)
            state, policy, profile = _settings(db, case_id)
            status = self._status(db, state)
            if now < status.updated_at:
                raise ValueError("dispatch time precedes its saved state")
            rows.check_time(rows.case_row(db, case_id), now)
            held = db.execute(
                "SELECT attempt_id,event_id FROM delivery_attempts WHERE case_id=? "
                "AND status IN ('RESERVED','FAILED','STALE') LIMIT 1",
                (case_id,),
            ).fetchone()
            if held is not None:
                if (
                    status.active_attempt_id is not None
                    and status.active_attempt_id != held["attempt_id"]
                ):
                    raise WorkflowConflict("dispatch and delivery held states differ")
                return DispatchStep(
                    case_id, "HELD", held["event_id"], held_attempt_id=held["attempt_id"]
                )
            if status.active_attempt_id is not None:
                raise WorkflowConflict("dispatch attempt was completed outside its transaction")
            monitor = state["monitor_id"]
            order = "DESC" if state["initial_event_id"] is None else "ASC"
            event = db.execute(
                "SELECT event_id,interval_start,revision FROM watch_outbox "
                f"WHERE monitor_id=? AND status='PENDING' ORDER BY interval_start {order},revision {order} LIMIT 1",
                (monitor,),
            ).fetchone()
            source_delivery = event is not None
            if event is None and state["initial_event_id"] is None:
                return DispatchStep(case_id, "WAITING_FOR_SOURCE")
            if event is None:
                event = db.execute(
                    "SELECT event_id,interval_start,revision FROM watch_events "
                    "WHERE monitor_id=? ORDER BY interval_start DESC,revision DESC LIMIT 1",
                    (monitor,),
                ).fetchone()
            if event is None:
                raise WorkflowConflict("activated source boundary is missing")
            context = _load_context(
                db, case_id, event["event_id"], evaluated_at=now, include_source_health=True
            )
            if state["initial_event_id"] is None:
                self._bootstrap(db, state, event, now)
            numeric = _baseline(db, state["numeric_context_json"])
            health = _baseline(db, state["health_context_json"])
            if (
                numeric is not None
                and compare_intervals(context.current, numeric.current).comparable
            ):
                # The SDK must be able to inspect the evidence that triggered
                # accumulated attention, even after many quiet intervals.
                ids = tuple(
                    dict.fromkeys(
                        identity
                        for identity in (
                            context.current.supersedes_event_id,
                            numeric.current.event_id,
                            *(item.event_id for item in context.prior),
                        )
                        if identity is not None
                    )
                )[:3]
                context = _load_context(
                    db,
                    case_id,
                    event["event_id"],
                    evaluated_at=now,
                    include_source_health=True,
                    prior_event_ids=ids,
                )
            handled = handled_keys(db, context)
            attention, move = evaluate_admission(context, policy, numeric, health, handled)
            replay = capture_admission(
                context, numeric, health, handled, source_delivery=source_delivery
            )
            if not attention.eligible:
                if source_delivery:
                    _outcome(
                        db, state, event["event_id"], "SUPPRESSED", now, replay, _json(attention)
                    )
                    _acknowledge(db, monitor, event["event_id"], "SUPPRESSED")
                    db.execute(
                        "UPDATE dispatch_settings SET suppressed_count=suppressed_count+1,updated_at=? "
                        "WHERE case_id=?",
                        (now.isoformat(), case_id),
                    )
                return DispatchStep(
                    case_id,
                    "SUPPRESSED" if source_delivery else "QUIET",
                    event["event_id"],
                    attention,
                )
            reservation = self.journal._reserve(
                db,
                case_id,
                event["event_id"],
                request_id="dispatch-" + uuid.uuid4().hex,
                profile=profile,
                source_delivery=source_delivery,
                now=now,
                include_source_health=True,
                prior_event_ids=tuple(item.event_id for item in context.prior),
            )
            if type(reservation) is not DeliveryReservation or reservation.context != context:
                raise WorkflowConflict("reserved context differs from attention inputs")
            db.execute(
                "INSERT INTO dispatch_attempts VALUES(?,?,?,?,?)",
                (reservation.attempt_id, case_id, replay, _json(attention), int(move)),
            )
            db.execute(
                "UPDATE dispatch_settings SET active_attempt_id=?,updated_at=? WHERE case_id=?",
                (reservation.attempt_id, now.isoformat(), case_id),
            )
            return DispatchStep(case_id, "RESERVED", event["event_id"], attention, reservation)

    @staticmethod
    def _bootstrap(db, state, selected, now):
        history = db.execute(
            "SELECT event_id FROM watch_outbox WHERE monitor_id=? AND status='PENDING' "
            "AND (interval_start,revision)<(?,?) ORDER BY interval_start,revision LIMIT 10001",
            (state["monitor_id"], selected["interval_start"], selected["revision"]),
        ).fetchall()
        if len(history) > 10000:
            raise WorkflowConflict("bootstrap backlog exceeds the bounded history limit")
        for event in history:
            _outcome(db, state, event["event_id"], "HISTORY", now)
            _acknowledge(db, state["monitor_id"], event["event_id"], "HISTORY")
        db.execute(
            "UPDATE dispatch_settings SET initial_event_id=?,history_count=?,updated_at=? WHERE case_id=?",
            (selected["event_id"], len(history), now.isoformat(), state["case_id"]),
        )

    def finish(self, attempt_id: str, execution: CurrentExecution, *, now: datetime):
        now = utc(now)
        with self.journal.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)
            attempt = _attempt(db, attempt_id)
            state, policy, profile = _settings(db, attempt["case_id"])
            saved = db.execute(
                "SELECT * FROM dispatch_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if (
                saved is None
                or saved["case_id"] != attempt["case_id"]
                or _json(profile) != attempt["profile_json"]
            ):
                raise WorkflowConflict("attempt is not bound to this dispatch policy")
            context, attention, move = restore_admission(
                db, saved["replay_json"], saved["attention_json"], policy
            )
            data = json.loads(saved["replay_json"])
            if (
                not attention.eligible
                or move != bool(saved["move_numeric"])
                or context.case_id != attempt["case_id"]
                or context.current.event_id != attempt["event_id"]
                or rows.digest(_json(context)) != attempt["context_digest64"]
                or data["source_delivery"] != bool(attempt["source_delivery"])
            ):
                raise WorkflowConflict("dispatch admission differs from reserved delivery")
            if attempt["status"] == "COMMITTED":
                if state["active_attempt_id"] == attempt_id:
                    raise WorkflowConflict("delivery completed outside dispatch transaction")
                return self.journal._commit(db, attempt_id, execution, now=now)
            if state["active_attempt_id"] != attempt_id:
                raise WorkflowConflict("attempt is not the active dispatch admission")
            for name in ("numeric", "health"):
                actual = state[name + "_context_json"]
                expected = None if data[name] is None else _json(data[name])
                if actual != expected:
                    raise WorkflowConflict("assessed baseline changed after dispatch admission")
            if now < timestamp(state["updated_at"]):
                raise ValueError("completion time precedes dispatch state")
            receipt = self.journal._commit(db, attempt_id, execution, now=now)
            if receipt.status != "COMMITTED":
                return receipt
            if attempt["source_delivery"]:
                _outcome(
                    db,
                    state,
                    context.current.event_id,
                    "ASSESSED",
                    now,
                    saved["replay_json"],
                    saved["attention_json"],
                    attempt_id,
                )
            reference = encode_reference(capture_context(context))
            db.execute(
                "UPDATE dispatch_settings SET numeric_context_json=?,health_context_json=?,"
                "active_attempt_id=NULL,assessment_count=assessment_count+1,updated_at=? WHERE case_id=?",
                (
                    reference if move else state["numeric_context_json"],
                    reference,
                    now.isoformat(),
                    context.case_id,
                ),
            )
            for due in attention.due_checks:
                db.execute(
                    "INSERT INTO dispatch_due VALUES(?,?,?,?,?,?,?)",
                    (
                        context.case_id,
                        due.key,
                        due.task_id,
                        due.revision,
                        due.next_check_at.isoformat(),
                        now.isoformat(),
                        attempt_id,
                    ),
                )
            return receipt

    def fail(self, attempt_id: str, failure: CurrentFailure, *, now: datetime):
        # Failure has no dispatcher side effects: its persisted active pointer
        # deliberately survives for human reconciliation after restart.
        with self.journal.cases._connect() as db:
            db.execute("BEGIN")
            ensure_schema(db)
            attempt = _attempt(db, attempt_id)
            state, _, _ = _settings(db, attempt["case_id"])
            if state["active_attempt_id"] != attempt_id:
                raise WorkflowConflict("failure is not for the active dispatch attempt")
        return self.journal.fail(attempt_id, failure, now=now)
