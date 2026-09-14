"""Bind a remote current decision to its exact application reservation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256

from .case_records import digest
from .case_store import _expected
from .case_types import _identifier
from .context_v3_types import CurrentContextV3
from .context_wire import decode_context_v3, encode_context_v3
from .delivery_types import _ATTEMPT, InvocationProfile
from .fact_validation import event_id, utc
from .field_delivery_codec import decode_execution, decode_failure, encode_execution, encode_failure
from .field_delivery_records import validate_failure
from .field_delivery_types import CurrentExecutionV3, CurrentFailureV3, FieldDeliveryReservation
from .field_tools import validate_assessment_v3
from .tools import _json


@dataclass(frozen=True, slots=True)
class CurrentRemoteIdentity:
    protocol_version: int
    context_version: int
    attempt_id: str
    request_id: str
    case_id: str
    case_revision: int
    event_id: str
    policy_digest: str
    source_delivery: bool
    reserved_at: datetime
    profile: InvocationProfile
    reserved_context_digest: str
    context_wire_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.protocol_version) is not int or self.protocol_version != 1
            or type(self.context_version) is not int or self.context_version != 3
            or type(self.source_delivery) is not bool
            or type(self.attempt_id) is not str or not _ATTEMPT.fullmatch(self.attempt_id)
            or type(self.profile) is not InvocationProfile
        ):
            raise ValueError("invalid current remote identity")
        _identifier(self.request_id, "request_id")
        _identifier(self.case_id, "case_id")
        _expected(self.case_revision)
        for value in (
            self.event_id, self.policy_digest, self.reserved_context_digest,
            self.context_wire_sha256,
        ):
            event_id(value)
        profile = replace(self.profile)
        if profile.instruction_version != "watershed-current-v3":
            raise ValueError("remote identity requires the current field instruction")
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "reserved_at", utc(self.reserved_at))


def identity_from(reservation: FieldDeliveryReservation) -> CurrentRemoteIdentity:
    """Derive both digest namespaces without changing the saved reservation."""
    if type(reservation) is not FieldDeliveryReservation:
        raise ValueError("expected an exact field delivery reservation")
    reservation = replace(reservation)
    encoded = encode_context_v3(reservation.context)
    context = decode_context_v3(encoded)
    if reservation.profile.mode == "SCRIPTED_SDK" and not context.base.simulated:
        raise ValueError("scripted remote requests require a simulated case")
    base = context.base
    return CurrentRemoteIdentity(
        1, 3, reservation.attempt_id, reservation.request_id, base.case_id,
        base.case_revision, base.current.event_id, base.policy_digest,
        reservation.source_delivery, reservation.reserved_at, reservation.profile,
        digest(_json(context)), sha256(encoded.encode("utf-8")).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class CurrentRemoteRequest:
    identity: CurrentRemoteIdentity
    context: CurrentContextV3

    def __post_init__(self) -> None:
        if type(self.identity) is not CurrentRemoteIdentity or type(self.context) is not CurrentContextV3:
            raise ValueError("invalid current remote request")
        identity = replace(self.identity)
        context = decode_context_v3(encode_context_v3(self.context))
        reservation = FieldDeliveryReservation(
            identity.attempt_id, identity.request_id, identity.profile,
            identity.source_delivery, context, identity.reserved_at,
        )
        if identity_from(reservation) != identity:
            raise ValueError("remote identity differs from its complete context")
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "context", context)


def result_wire(value: CurrentExecutionV3 | CurrentFailureV3) -> str:
    """Reconstruct all nested result records before accepting their wire hash."""
    if type(value) is CurrentExecutionV3:
        encoded = encode_execution(value)
        decode_execution(encoded)
        return encoded
    if type(value) is CurrentFailureV3:
        encoded = encode_failure(value)
        decode_failure(encoded)
        return encoded
    raise ValueError("expected exact current execution or failure")


def _result_identity(
    identity: CurrentRemoteIdentity, value: CurrentExecutionV3 | CurrentFailureV3,
) -> None:
    profile = InvocationProfile(value.mode, value.model_id, value.instruction_version, value.sdk_version)
    base = value.assessment.base if type(value) is CurrentExecutionV3 else value
    context_digest = (
        value.assessment.context_digest if type(value) is CurrentExecutionV3 else value.context_digest
    )
    if (
        profile != identity.profile
        or (base.case_id, base.case_revision, base.event_id, base.policy_digest, context_digest)
        != (
            identity.case_id, identity.case_revision, identity.event_id,
            identity.policy_digest, identity.reserved_context_digest,
        )
    ):
        raise ValueError("remote result differs from its reserved identity")


@dataclass(frozen=True, slots=True)
class CurrentRemoteResponse:
    identity: CurrentRemoteIdentity
    request_sha256: str
    result_kind: str
    result_wire_sha256: str
    execution: CurrentExecutionV3 | None
    failure: CurrentFailureV3 | None

    def __post_init__(self) -> None:
        if type(self.identity) is not CurrentRemoteIdentity or type(self.result_kind) is not str:
            raise ValueError("invalid current remote response")
        identity = replace(self.identity)
        event_id(self.request_sha256)
        event_id(self.result_wire_sha256)
        if self.result_kind == "EXECUTION":
            if type(self.execution) is not CurrentExecutionV3 or self.failure is not None:
                raise ValueError("execution response requires exactly one execution")
            encoded = result_wire(self.execution)
            value = decode_execution(encoded)
            object.__setattr__(self, "execution", value)
        elif self.result_kind == "FAILURE":
            if type(self.failure) is not CurrentFailureV3 or self.execution is not None:
                raise ValueError("failure response requires exactly one failure")
            encoded = result_wire(self.failure)
            value = decode_failure(encoded)
            object.__setattr__(self, "failure", value)
        else:
            raise ValueError("unknown current remote result kind")
        if sha256(encoded.encode("utf-8")).hexdigest() != self.result_wire_sha256:
            raise ValueError("remote result hash differs from its canonical record")
        _result_identity(identity, value)
        object.__setattr__(self, "identity", identity)


def validate_result(
    identity: CurrentRemoteIdentity,
    context: CurrentContextV3,
    value: CurrentExecutionV3 | CurrentFailureV3,
) -> CurrentExecutionV3 | CurrentFailureV3:
    """Replay all supplied tool evidence in memory; never commit application work."""
    request = CurrentRemoteRequest(identity, context)
    encoded = result_wire(value)
    if type(value) is CurrentExecutionV3:
        checked = decode_execution(encoded)
        _result_identity(request.identity, checked)
        validate_assessment_v3(request.context, checked.assessment)
    else:
        checked = decode_failure(encoded)
        _result_identity(request.identity, checked)
        validate_failure(request.context, checked)
    return checked
