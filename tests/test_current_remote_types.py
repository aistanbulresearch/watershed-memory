"""The remote identity binds a real reservation and replayable source/field result."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from test_current_field_dispatch_runner import AT
from test_current_field_dispatch_runner import sdk_field as sdk_field
from test_current_field_store import CASE, contents
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.case_records import digest
from watershed_memory.current.context_wire import encode_context_v3
from watershed_memory.current.field_delivery_codec import encode_execution
from watershed_memory.current.field_delivery_types import CurrentFailureV3
from watershed_memory.current.remote_types import (
    CurrentRemoteRequest,
    CurrentRemoteResponse,
    identity_from,
    result_wire,
    validate_result,
)
from watershed_memory.current.tools import _json


@pytest.fixture
def reserved(sdk_field):
    case, store, local = sdk_field
    reservation = store.prepare(CASE, now=AT).reservation
    return case, store, local, reservation


@pytest.fixture
def executed(reserved):
    case, store, local, reservation = reserved
    return case, store, local, reservation, local.plan_reserved(reservation)


def test_identity_preserves_saved_attempt_and_both_digest_contracts(reserved):
    case, _, _, reservation = reserved
    before = contents(case[0])
    identity = identity_from(reservation)
    request = CurrentRemoteRequest(identity, reservation.context)
    assert identity.attempt_id == reservation.attempt_id
    assert identity.request_id == reservation.request_id
    assert identity.source_delivery is reservation.source_delivery
    assert identity.profile == reservation.profile
    assert identity.reserved_at == reservation.reserved_at
    assert identity.reserved_context_digest == digest(_json(reservation.context))
    assert identity.context_wire_sha256 == sha256(encode_context_v3(reservation.context).encode()).hexdigest()
    assert request.context == reservation.context
    assert contents(case[0]) == before


@pytest.mark.parametrize("change", [
    {"protocol_version": True}, {"protocol_version": 2},
    {"context_version": True}, {"context_version": 2},
    {"attempt_id": "not-an-attempt"}, {"request_id": ""},
    {"case_id": "DIFFERENT-CASE"}, {"case_revision": True}, {"case_revision": 999},
    {"event_id": "0" * 64}, {"policy_digest": "0" * 64},
    {"source_delivery": 1}, {"reserved_context_digest": "0" * 64},
    {"context_wire_sha256": "0" * 64}, {"profile": {}},
])
def test_request_refuses_malformed_or_wrong_context_identity(reserved, change):
    case, _, _, reservation = reserved
    before = contents(case[0])
    identity = identity_from(reservation)
    with pytest.raises(ValueError):
        CurrentRemoteRequest(replace(identity, **change), reservation.context)
    assert contents(case[0]) == before


def test_response_preserves_valid_scripted_sdk_result_without_database_write(executed):
    case, _, _, reservation, execution = executed
    before = contents(case[0])
    identity = identity_from(reservation)
    checked = validate_result(identity, reservation.context, execution)
    assert checked == execution
    assert result_wire(checked) == encode_execution(execution)
    response = CurrentRemoteResponse(identity, "a" * 64, "EXECUTION",
        sha256(result_wire(execution).encode()).hexdigest(), execution, None)
    assert response.execution == execution and response.failure is None
    assert contents(case[0]) == before


@pytest.mark.parametrize("change", [
    {"model_id": "different-model"}, {"sdk_version": "different-sdk"},
    {"mode": "STRANDS_CURRENT"},
])
def test_result_full_profile_must_match_request(executed, change):
    _, _, _, reservation, execution = executed
    with pytest.raises(ValueError):
        validate_result(identity_from(reservation), reservation.context, replace(execution, **change))


def test_valid_shaped_but_forged_field_output_is_replayed_and_refused(executed):
    case, _, _, reservation, execution = executed
    before = contents(case[0])
    trace = execution.assessment.field_trace
    forged = replace(trace[0], output_json="{}")
    assessment = replace(execution.assessment, field_trace=(forged, *trace[1:]))
    with pytest.raises(ValueError):
        validate_result(identity_from(reservation), reservation.context,
            replace(execution, assessment=assessment))
    assert contents(case[0]) == before


def failure_for(reservation, execution):
    profile, base = reservation.profile, reservation.context.base
    return CurrentFailureV3(base.case_id, base.case_revision, base.policy_digest,
        base.current.event_id, digest(_json(reservation.context)), profile.mode,
        profile.model_id, profile.instruction_version, profile.sdk_version, 1, 1,
        execution.assessment.base.trace[:1], (), "{}")


def test_failure_is_checked_against_its_exact_context_and_trace(executed):
    case, _, _, reservation, execution = executed
    before = contents(case[0])
    failure = failure_for(reservation, execution)
    identity = identity_from(reservation)
    assert validate_result(identity, reservation.context, failure) == failure
    wrong = replace(failure.source_trace[0], output_json="{}")
    with pytest.raises(ValueError):
        validate_result(identity, reservation.context, replace(failure, source_trace=(wrong,)))
    assert contents(case[0]) == before


def test_response_requires_exactly_one_result_and_its_standalone_hash(executed):
    _, _, _, reservation, execution = executed
    identity = identity_from(reservation)
    checksum = sha256(result_wire(execution).encode()).hexdigest()
    failure = failure_for(reservation, execution)
    for kind, success, failed, result_hash in (
        ("EXECUTION", None, None, checksum),
        ("EXECUTION", execution, failure, checksum),
        ("FAILURE", execution, None, checksum),
        ("EXECUTION", execution, None, "0" * 64),
    ):
        with pytest.raises(ValueError):
            CurrentRemoteResponse(identity, "a" * 64, kind, result_hash, success, failed)


def test_result_reconstructs_forged_native_nested_records(executed):
    _, _, _, reservation, execution = executed
    forged = deepcopy(execution)
    object.__setattr__(forged.assessment.field, "disposition", "INVENTED")
    with pytest.raises(ValueError):
        validate_result(identity_from(reservation), reservation.context, forged)
