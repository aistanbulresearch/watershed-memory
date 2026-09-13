"""Field-aware v3 dispatch using the accepted source-attention ledger."""

import json
import uuid

from . import case_records as rows
from . import field_delivery_records
from .assessment_types import _canonical_json
from .case_types import WorkflowConflict, _identifier
from .context import _case_config
from .context3_schema import ensure_schema
from .context_reference import capture_context, encode_reference
from .context_v3 import _load_context_v3
from .delivery_store import _attempt, _receipt
from .delivery_types import InvocationProfile
from .dispatch_replay import (
    capture_admission,
    decode_policy,
    evaluate_admission,
    handled_keys,
    restore_admission,
)
from .dispatch_store import DispatchStore, _acknowledge, _baseline, _outcome, _settings
from .fact_validation import timestamp, utc
from .facts import compare_intervals
from .field_delivery_store import FieldDeliveryStore
from .field_delivery_types import (
    CurrentExecutionV3,
    CurrentFailureV3,
    FieldDeliveryReservation,
)
from .field_dispatch_types import FieldDispatchStep
from .strands import HEALTH_INSTRUCTION_VERSION
from .tools import _json

V3_INSTRUCTION_VERSION = "watershed-current-v3"
_HELD = {"RESERVED", "FAILED", "STALE"}


def _profile(raw):
    _canonical_json(raw, "invocation profile", 2048)
    try:
        return InvocationProfile(**json.loads(raw))
    except TypeError as error:
        raise ValueError("invalid dispatch profile fields") from error


def _settings_v3(db, case_id):
    _identifier(case_id, "case_id")
    state = db.execute("SELECT * FROM dispatch_settings WHERE case_id=?", (case_id,)).fetchone()
    extension = db.execute(
        "SELECT * FROM context3_dispatch_settings WHERE case_id=?", (case_id,)
    ).fetchone()
    if state is None or extension is None:
        raise KeyError("case is not upgraded for field-aware dispatch")
    policy = decode_policy(state["policy_json"])
    previous = _profile(extension["previous_profile_json"])
    profile = _profile(extension["v3_profile_json"])
    if (
        extension["context_version"] != 3
        or state["profile_json"] != extension["v3_profile_json"]
        or policy.policy_digest != state["policy_digest"]
        or previous.instruction_version != HEALTH_INSTRUCTION_VERSION
        or profile.instruction_version != V3_INSTRUCTION_VERSION
        or (profile.mode, profile.model_id, profile.sdk_version)
        != (previous.mode, previous.model_id, previous.sdk_version)
    ):
        raise WorkflowConflict("field dispatch settings differ from their exact upgrade")
    case = _case_config(rows.case_row(db, case_id))
    if state["monitor_id"] != case.monitor_id or (
        profile.mode == "SCRIPTED_SDK" and not case.simulated
    ):
        raise WorkflowConflict("field dispatch scope differs from the case")

    attempts = db.execute(
        "SELECT d.*,a.attempt_id AS dispatch_id,a.replay_json AS admission_replay,"
        "a.attention_json AS admission_attention,"
        "c.attempt_id AS context3_id FROM delivery_attempts d "
        "LEFT JOIN dispatch_attempts a ON a.case_id=d.case_id AND a.attempt_id=d.attempt_id "
        "LEFT JOIN context3_attempts c ON c.case_id=d.case_id AND c.attempt_id=d.attempt_id "
        "WHERE d.case_id=?",
        (case_id,),
    ).fetchall()
    held = [attempt["attempt_id"] for attempt in attempts if attempt["status"] in _HELD]
    if len(held) > 1 or state["active_attempt_id"] != (held[0] if held else None):
        raise WorkflowConflict("dispatch active pointer and unresolved delivery differ")
    if state["assessment_count"] != sum(attempt["status"] == "COMMITTED" for attempt in attempts):
        raise WorkflowConflict("dispatch completion count differs from committed deliveries")
    for attempt in attempts:
        if attempt["dispatch_id"] is None:
            raise WorkflowConflict("field dispatch found delivery outside dispatch")
        if attempt["context3_id"] is None:
            if attempt["profile_json"] != extension["previous_profile_json"]:
                raise WorkflowConflict("legacy dispatch attempt has the wrong profile")
            if attempt["status"] in _HELD:
                raise WorkflowConflict("active legacy attempt cannot cross the v3 upgrade")
        elif attempt["profile_json"] != extension["v3_profile_json"]:
            raise WorkflowConflict("field-aware attempt has the wrong profile")
        if attempt["status"] == "COMMITTED" and attempt["source_delivery"]:
            outcome = db.execute(
                "SELECT * FROM dispatch_source_outcomes WHERE case_id=? AND event_id=?",
                (case_id, attempt["event_id"]),
            ).fetchone()
            if outcome is None or (
                outcome["monitor_id"],
                outcome["kind"],
                outcome["attempt_id"],
                outcome["recorded_at"],
                outcome["replay_json"],
                outcome["attention_json"],
            ) != (
                attempt["monitor_id"],
                "ASSESSED",
                attempt["attempt_id"],
                attempt["finished_at"],
                attempt["admission_replay"],
                attempt["admission_attention"],
            ):
                raise WorkflowConflict("committed source work lacks exact dispatch finalization")
    return state, policy, profile, previous


class FieldDispatchStore:
    def __init__(self, dispatch: DispatchStore, delivery: FieldDeliveryStore):
        if (
            type(dispatch) is not DispatchStore
            or type(delivery) is not FieldDeliveryStore
            or dispatch.journal is not delivery.journal
            or delivery.cases is not dispatch.journal.cases
        ):
            raise ValueError("field dispatch requires one exact shared journal")
        self.dispatch = dispatch
        self.delivery = delivery
        self.journal = delivery.journal
        self.fields = delivery.fields
        self.cases = delivery.cases
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)

    def upgrade_to_v3(self, case_id, profile, *, now):
        _identifier(case_id, "case_id")
        now = utc(now)
        if (
            type(profile) is not InvocationProfile
            or profile.instruction_version != V3_INSTRUCTION_VERSION
        ):
            raise ValueError("field dispatch requires an exact v3 profile")
        profile_json = _json(profile)
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)
            existing = db.execute(
                "SELECT 1 FROM context3_dispatch_settings WHERE case_id=?", (case_id,)
            ).fetchone()
            if existing is not None:
                state, _, saved, _ = _settings_v3(db, case_id)
                rows.check_time(rows.case_row(db, case_id), now)
                if profile != saved:
                    raise WorkflowConflict("field dispatch upgrade already binds another profile")
                if now < timestamp(state["updated_at"]):
                    raise ValueError("upgrade time precedes dispatch state")
                return
            state, _, previous = _settings(db, case_id)
            if (profile.mode, profile.model_id, profile.sdk_version) != (
                previous.mode,
                previous.model_id,
                previous.sdk_version,
            ):
                raise ValueError("v3 upgrade may change only the instruction version")
            if (
                state["active_attempt_id"] is not None
                or db.execute(
                    "SELECT 1 FROM delivery_attempts WHERE case_id=? "
                    "AND status IN ('RESERVED','FAILED','STALE') LIMIT 1",
                    (case_id,),
                ).fetchone()
            ):
                raise WorkflowConflict("resolve active delivery before the v3 upgrade")
            legacy = db.execute(
                "SELECT d.profile_json,c.attempt_id AS context3_id FROM delivery_attempts d "
                "LEFT JOIN context3_attempts c ON c.case_id=d.case_id AND c.attempt_id=d.attempt_id "
                "WHERE d.case_id=?",
                (case_id,),
            ).fetchall()
            if any(
                attempt["profile_json"] != state["profile_json"]
                or attempt["context3_id"] is not None
                for attempt in legacy
            ):
                raise WorkflowConflict("legacy history differs from the exact pre-upgrade profile")
            case = rows.case_row(db, case_id)
            rows.check_time(case, now)
            if now < timestamp(state["updated_at"]):
                raise ValueError("upgrade time precedes dispatch state")
            db.execute(
                "UPDATE dispatch_settings SET profile_json=?,updated_at=? WHERE case_id=?",
                (profile_json, now.isoformat(), case_id),
            )
            db.execute(
                "INSERT INTO context3_dispatch_settings VALUES(?,?,?,?,?,3)",
                (
                    case_id,
                    state["profile_json"],
                    profile_json,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    def status(self, case_id):
        with self.cases._connect() as db:
            db.execute("BEGIN")
            ensure_schema(db)
            state, _, _, _ = _settings_v3(db, case_id)
            return DispatchStore._status(db, state)

    def get(self, case_id, request_id):
        _identifier(case_id, "case_id")
        _identifier(request_id, "request_id")
        with self.cases._connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            ensure_schema(db)
            state, policy, profile, previous = _settings_v3(db, case_id)
            attempt = db.execute(
                "SELECT * FROM delivery_attempts WHERE case_id=? AND request_id=?",
                (case_id, request_id),
            ).fetchone()
            if attempt is None:
                raise KeyError("unknown field dispatch request")
            is_v3 = (
                db.execute(
                    "SELECT 1 FROM context3_attempts WHERE case_id=? AND attempt_id=?",
                    (case_id, attempt["attempt_id"]),
                ).fetchone()
                is not None
            )
            self._admission(db, attempt, state, policy, profile if is_v3 else previous, is_v3)
            return (
                field_delivery_records.receipt(db, self.fields, attempt)
                if is_v3
                else _receipt(attempt)
            )

    def _admission(self, db, attempt, state, policy, profile, is_v3):
        saved = db.execute(
            "SELECT * FROM dispatch_attempts WHERE case_id=? AND attempt_id=?",
            (attempt["case_id"], attempt["attempt_id"]),
        ).fetchone()
        if saved is None or attempt["profile_json"] != _json(profile):
            raise WorkflowConflict("attempt is not bound to this dispatch profile")
        context, attention, move = restore_admission(
            db, saved["replay_json"], saved["attention_json"], policy
        )
        data = json.loads(saved["replay_json"])
        if (
            not attention.eligible
            or move != bool(saved["move_numeric"])
            or context.case_id != attempt["case_id"]
            or context.current.event_id != attempt["event_id"]
            or data["source_delivery"] != bool(attempt["source_delivery"])
            or (not is_v3 and rows.digest(_json(context)) != attempt["context_digest64"])
        ):
            raise WorkflowConflict("dispatch admission differs from its delivery")
        if is_v3:
            full = field_delivery_records.restore_reserved(db, self.fields, attempt)
            if full.base != context:
                raise WorkflowConflict("field and source admission contexts differ")
        return context, attention, move, data, saved

    def prepare(self, case_id, *, now):
        now = utc(now)
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)
            state, policy, profile, _ = _settings_v3(db, case_id)
            status = DispatchStore._status(db, state)
            if now < status.updated_at:
                raise ValueError("dispatch time precedes its saved state")
            rows.check_time(rows.case_row(db, case_id), now)
            held = db.execute(
                "SELECT attempt_id,event_id FROM delivery_attempts WHERE case_id=? "
                "AND status IN ('RESERVED','FAILED','STALE') LIMIT 1",
                (case_id,),
            ).fetchone()
            if held is not None:
                if status.active_attempt_id != held["attempt_id"]:
                    raise WorkflowConflict("dispatch and delivery held states differ")
                return FieldDispatchStep(
                    case_id, "HELD", held["event_id"], held_attempt_id=held["attempt_id"]
                )
            if status.active_attempt_id is not None:
                raise WorkflowConflict("dispatch attempt completed outside its transaction")
            monitor = state["monitor_id"]
            order = "DESC" if state["initial_event_id"] is None else "ASC"
            event = db.execute(
                "SELECT event_id,interval_start,revision FROM watch_outbox "
                f"WHERE monitor_id=? AND status='PENDING' "
                f"ORDER BY interval_start {order},revision {order} LIMIT 1",
                (monitor,),
            ).fetchone()
            source_delivery = event is not None
            if event is None and state["initial_event_id"] is None:
                return FieldDispatchStep(case_id, "WAITING_FOR_SOURCE")
            if event is None:
                event = db.execute(
                    "SELECT event_id,interval_start,revision FROM watch_events "
                    "WHERE monitor_id=? ORDER BY interval_start DESC,revision DESC LIMIT 1",
                    (monitor,),
                ).fetchone()
            if event is None:
                raise WorkflowConflict("activated source boundary is missing")
            context = _load_context_v3(
                db, self.fields, case_id, event["event_id"], evaluated_at=now
            )
            if state["initial_event_id"] is None:
                DispatchStore._bootstrap(db, state, event, now)
            numeric = _baseline(db, state["numeric_context_json"])
            health = _baseline(db, state["health_context_json"])
            if (
                numeric is not None
                and compare_intervals(context.base.current, numeric.current).comparable
            ):
                ids = tuple(
                    dict.fromkeys(
                        identity
                        for identity in (
                            context.base.current.supersedes_event_id,
                            numeric.current.event_id,
                            *(item.event_id for item in context.base.prior),
                        )
                        if identity is not None
                    )
                )[:3]
                context = _load_context_v3(
                    db,
                    self.fields,
                    case_id,
                    event["event_id"],
                    evaluated_at=now,
                    prior_event_ids=ids,
                )
            handled = handled_keys(db, context.base)
            attention, move = evaluate_admission(context.base, policy, numeric, health, handled)
            replay = capture_admission(
                context.base, numeric, health, handled, source_delivery=source_delivery
            )
            if not attention.eligible:
                if source_delivery:
                    _outcome(
                        db, state, event["event_id"], "SUPPRESSED", now, replay, _json(attention)
                    )
                    _acknowledge(db, monitor, event["event_id"], "SUPPRESSED")
                    db.execute(
                        "UPDATE dispatch_settings SET suppressed_count=suppressed_count+1,"
                        "updated_at=? WHERE case_id=?",
                        (now.isoformat(), case_id),
                    )
                return FieldDispatchStep(
                    case_id,
                    "SUPPRESSED" if source_delivery else "QUIET",
                    event["event_id"],
                    attention,
                )
            reservation = self.delivery._reserve(
                db,
                case_id,
                event["event_id"],
                request_id="dispatch-" + uuid.uuid4().hex,
                profile=profile,
                source_delivery=source_delivery,
                now=now,
                prior_event_ids=tuple(item.event_id for item in context.base.prior),
            )
            if type(reservation) is not FieldDeliveryReservation or reservation.context != context:
                raise WorkflowConflict("v3 reserved context differs from attention inputs")
            db.execute(
                "INSERT INTO dispatch_attempts VALUES(?,?,?,?,?)",
                (reservation.attempt_id, case_id, replay, _json(attention), int(move)),
            )
            db.execute(
                "UPDATE dispatch_settings SET active_attempt_id=?,updated_at=? WHERE case_id=?",
                (reservation.attempt_id, now.isoformat(), case_id),
            )
            return FieldDispatchStep(case_id, "RESERVED", event["event_id"], attention, reservation)

    def finish(self, attempt_id, execution, *, now):
        if type(execution) is not CurrentExecutionV3:
            raise ValueError("expected a typed field-aware execution")
        now = utc(now)
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)
            attempt = _attempt(db, attempt_id)
            state, policy, profile, _ = _settings_v3(db, attempt["case_id"])
            source, attention, move, data, saved = self._admission(
                db, attempt, state, policy, profile, True
            )
            if (
                db.execute(
                    "SELECT 1 FROM context3_attempts WHERE case_id=? AND attempt_id=?",
                    (attempt["case_id"], attempt_id),
                ).fetchone()
                is None
            ):
                raise WorkflowConflict("attempt is not a field-aware dispatch delivery")
            context = field_delivery_records.restore_reserved(db, self.fields, attempt)
            if context.base != source:
                raise WorkflowConflict("field and source admission contexts differ")
            if attempt["status"] == "COMMITTED":
                if state["active_attempt_id"] == attempt_id:
                    raise WorkflowConflict("delivery completed outside dispatch transaction")
                return self.delivery._commit(db, attempt_id, execution, now=now)
            if state["active_attempt_id"] != attempt_id:
                raise WorkflowConflict("attempt is not the active field dispatch admission")
            for name in ("numeric", "health"):
                expected = None if data[name] is None else _json(data[name])
                if state[name + "_context_json"] != expected:
                    raise WorkflowConflict("assessed baseline changed after dispatch admission")
            if now < timestamp(state["updated_at"]):
                raise ValueError("completion time precedes dispatch state")
            receipt = self.delivery._commit(db, attempt_id, execution, now=now)
            if receipt.delivery.status != "COMMITTED":
                return receipt
            if attempt["source_delivery"]:
                _outcome(
                    db,
                    state,
                    source.current.event_id,
                    "ASSESSED",
                    now,
                    saved["replay_json"],
                    saved["attention_json"],
                    attempt_id,
                )
            reference = encode_reference(capture_context(context.base))
            db.execute(
                "UPDATE dispatch_settings SET numeric_context_json=?,health_context_json=?,"
                "active_attempt_id=NULL,assessment_count=assessment_count+1,updated_at=? "
                "WHERE case_id=?",
                (
                    reference if move else state["numeric_context_json"],
                    reference,
                    now.isoformat(),
                    source.case_id,
                ),
            )
            for due in attention.due_checks:
                db.execute(
                    "INSERT INTO dispatch_due VALUES(?,?,?,?,?,?,?)",
                    (
                        source.case_id,
                        due.key,
                        due.task_id,
                        due.revision,
                        due.next_check_at.isoformat(),
                        now.isoformat(),
                        attempt_id,
                    ),
                )
            return receipt

    def fail(self, attempt_id, failure, *, now):
        if type(failure) is not CurrentFailureV3:
            raise ValueError("expected a typed field-aware failure")
        with self.cases._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)
            attempt = _attempt(db, attempt_id)
            state, policy, profile, _ = _settings_v3(db, attempt["case_id"])
            if (
                db.execute(
                    "SELECT 1 FROM context3_attempts WHERE case_id=? AND attempt_id=?",
                    (attempt["case_id"], attempt_id),
                ).fetchone()
                is None
            ):
                raise WorkflowConflict("attempt is not a field-aware dispatch delivery")
            self._admission(db, attempt, state, policy, profile, True)
            if state["active_attempt_id"] != attempt_id:
                raise WorkflowConflict("failure is not for the active field dispatch attempt")
            return self.delivery._fail(db, attempt_id, failure, now=now)
