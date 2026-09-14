"""Transport facts describe one pinned attempt without carrying operator data."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from watershed_memory.current.agentcore_evidence import (
    AgentCoreTarget,
    CurrentAgentCoreEvidence,
    CurrentAgentCoreTransportError,
)
from watershed_memory.current.delivery_types import InvocationProfile
from watershed_memory.current.remote_types import CurrentRemoteIdentity

ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/watershedcurrent-abc123"
QUALIFIER = "proof_v3"


@pytest.fixture
def target():
    return AgentCoreTarget(ARN, QUALIFIER, "us-east-1", "123456789012", "1")


@pytest.fixture
def identity():
    return CurrentRemoteIdentity(1, 3, "attempt-" + "a" * 32, "request-1", "FIELD-DEMO", 6,
        "a" * 64, "b" * 64, True, datetime(2026, 9, 13, tzinfo=UTC),
        InvocationProfile("SCRIPTED_SDK", "local-fixture", "watershed-current-v3", "1.55.1"),
        "c" * 64, "d" * 64)


@pytest.fixture
def evidence(target, identity):
    return CurrentAgentCoreEvidence(1, identity, target, "wm-" + "a" * 32,
        "e" * 64, "f" * 64, True, "1", target.endpoint_arn, "aws-request-1", None,
        "1" * 64, "2" * 64, "VALIDATED_EXECUTION", "STOP_REQUEST_ACCEPTED", None, None)


def test_pinned_target_derives_exact_runtime_and_endpoint(target):
    assert target.runtime_id == "watershedcurrent-abc123"
    assert target.endpoint_arn == ARN + "/runtime-endpoint/" + QUALIFIER


@pytest.mark.parametrize("endpoint_length", [320, 321, 322])
def test_target_endpoint_length_is_admitted_before_any_attempt(endpoint_length):
    qualifier = "q" * 48
    prefix = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/a-"
    suffix = "/runtime-endpoint/" + qualifier
    arn = prefix + "a" * (endpoint_length - len(prefix) - len(suffix))
    if endpoint_length == 320:
        target = AgentCoreTarget(arn, qualifier, "us-east-1", "123456789012", "1")
        assert len(target.endpoint_arn) == 320
    else:
        with pytest.raises(ValueError):
            AgentCoreTarget(arn, qualifier, "us-east-1", "123456789012", "1")


@pytest.mark.parametrize("change", [
    {"runtime_arn": None}, {"runtime_arn": ARN + "\n"}, {"runtime_arn": ARN.replace("123456789012", "000000000000")},
    {"region": "eu-north-1"}, {"region": True}, {"expected_account": 123456789012},
    {"expected_account": "123"}, {"qualifier": "DEFAULT"}, {"qualifier": None},
    {"qualifier": "a" * 49}, {"qualifier": "bad/name"},
    {"expected_version": 1}, {"expected_version": "01"}, {"expected_version": "0"},
    {"expected_version": "1" * 20},
])
def test_target_refuses_ambiguous_or_cross_account_configuration(target, change):
    with pytest.raises(ValueError):
        replace(target, **change)


def test_evidence_is_deeply_reconstructed_and_immutable(evidence, identity, target):
    assert evidence.identity == identity and evidence.identity is not identity
    assert evidence.target == target and evidence.target is not target
    with pytest.raises(FrozenInstanceError):
        evidence.outcome = "OUTCOME_UNKNOWN"
    forged = deepcopy(identity)
    object.__setattr__(forged, "source_delivery", 1)
    with pytest.raises(ValueError):
        replace(evidence, identity=forged)
    forged_target = deepcopy(target)
    object.__setattr__(forged_target, "expected_account", "000000000000")
    with pytest.raises(ValueError):
        replace(evidence, target=forged_target)


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"identity": {}}, {"target": {}},
    {"runtime_session_id": "another-session"}, {"request_sha256": "unknown"},
    {"wrapper_sha256": "F" * 64}, {"invocation_attempted": 1},
    {"verified_version": "2"}, {"endpoint_arn": ARN + "/runtime-endpoint/other"},
    {"verified_version": None}, {"endpoint_arn": None},
    {"aws_request_id": "PRIVATE-CANARY\n"}, {"trace_id": "a" * 257},
    {"response_sha256": None}, {"result_wire_sha256": None},
    {"outcome": "COMMITTED"}, {"stop_status": "NOT_STARTED"},
    {"error_code": "TimeoutError"}, {"stop_error_code": "CleanupError"},
])
def test_evidence_refuses_malformed_or_contradictory_success(evidence, change):
    with pytest.raises(ValueError):
        replace(evidence, **change)


def test_unknown_preflight_and_attempted_failures_have_distinct_shapes(evidence):
    preflight = replace(evidence, invocation_attempted=False, verified_version=None,
        endpoint_arn=None, aws_request_id=None, response_sha256=None, result_wire_sha256=None,
        outcome="OUTCOME_UNKNOWN", stop_status="NOT_STARTED", error_code="EndpointMismatch")
    assert preflight.invocation_attempted is False
    for change in ({"response_sha256": "0" * 64}, {"aws_request_id": "req"},
        {"trace_id": "trace"}, {"stop_status": "STOP_REQUEST_ACCEPTED"}, {"error_code": None}):
        with pytest.raises(ValueError):
            replace(preflight, **change)
    attempted = replace(evidence, outcome="OUTCOME_UNKNOWN", result_wire_sha256=None,
        error_code="InvalidResponse")
    assert attempted.response_sha256 == evidence.response_sha256
    with pytest.raises(ValueError):
        replace(attempted, result_wire_sha256="0" * 64)


@pytest.mark.parametrize("outcome", ["VALIDATED_EXECUTION", "VALIDATED_FAILURE"])
def test_cleanup_failure_does_not_erase_a_validated_result(evidence, outcome):
    failed_cleanup = replace(evidence, outcome=outcome, stop_status="STOP_FAILED",
        stop_error_code="CleanupError")
    assert failed_cleanup.outcome == outcome and failed_cleanup.error_code is None
    with pytest.raises(ValueError):
        replace(failed_cleanup, stop_error_code=None)


def test_neutral_transport_error_preserves_only_unknown_immutable_evidence(evidence):
    unknown = replace(evidence, outcome="OUTCOME_UNKNOWN", result_wire_sha256=None,
        error_code="TimeoutError")
    error = CurrentAgentCoreTransportError(unknown)
    assert str(error) == "current AgentCore transport outcome is unknown"
    assert error.evidence == unknown and error.evidence is not unknown
    with pytest.raises(ValueError):
        CurrentAgentCoreTransportError(evidence)
    with pytest.raises(ValueError):
        CurrentAgentCoreTransportError({})
