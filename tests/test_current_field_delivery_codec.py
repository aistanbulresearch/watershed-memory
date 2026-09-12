"""The v3 delivery codec preserves exact staged decisions without widening them."""

import json
from dataclasses import replace

import pytest
from test_current_field_assessment_types import HASH, assessment
from test_current_field_store import CASE, SPEC

from watershed_memory.current.field_assessment_types import AgentFieldDecision, AgentFieldProposal
from watershed_memory.current.field_delivery_codec import (
    decode_execution,
    decode_failure,
    encode_execution,
    encode_failure,
)
from watershed_memory.current.field_delivery_types import CurrentExecutionV3, CurrentFailureV3


def proposal_assessment():
    proposal = AgentFieldProposal("task-1", 1, "site-1", 1, SPEC)
    decision = AgentFieldDecision(
        "PROPOSE_FIELD_PLAN",
        None,
        None,
        None,
        None,
        "The inspected field result supports a bounded follow-up plan.",
        proposal,
    )
    return replace(assessment(), field=decision)


def execution():
    return CurrentExecutionV3(
        proposal_assessment(),
        "SCRIPTED_SDK",
        "fixture-model",
        "watershed-current-v3",
        "1.55.1",
        1,
        3,
        0.125,
        '{"input_tokens":12}',
        "end_turn",
    )


def failure():
    staged = proposal_assessment()
    return CurrentFailureV3(
        CASE,
        1,
        HASH,
        HASH,
        HASH,
        "STRANDS_CURRENT",
        "fixture-model",
        "watershed-current-v3",
        "1.55.1",
        1,
        3,
        staged.base.trace,
        staged.field_trace,
        '{"output_tokens":4}',
    )


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_execution_round_trip_preserves_proposal_spec_timestamps_and_tuples():
    original = execution()
    encoded = encode_execution(original)
    restored = decode_execution(encoded)
    assert restored == original
    assert type(restored.assessment.base.decisions) is tuple
    assert type(restored.assessment.field_trace) is tuple
    assert type(restored.assessment.field.proposal.spec.required_evidence) is tuple
    assert restored.assessment.field.proposal.spec.window_start == SPEC.window_start
    assert encoded == canonical(json.loads(encoded))


def test_failure_round_trip_preserves_both_partial_traces():
    original = failure()
    encoded = encode_failure(original)
    restored = decode_failure(encoded)
    assert restored == original
    assert restored.status == "FAILED" and restored.code == "CURRENT_V3_TURN_FAILED"
    assert restored.source_trace == original.source_trace
    assert restored.field_trace == original.field_trace


@pytest.mark.parametrize("which", ["execution", "failure"])
def test_unknown_top_level_or_nested_fields_are_rejected(which):
    encoder, decoder, value = (
        (encode_execution, decode_execution, execution())
        if which == "execution"
        else (encode_failure, decode_failure, failure())
    )
    raw = json.loads(encoder(value))
    raw["forged"] = True
    with pytest.raises(ValueError):
        decoder(canonical(raw))

    raw = json.loads(encoder(value))
    trace = raw["assessment"]["base"]["trace"] if which == "execution" else raw["source_trace"]
    trace[0]["forged"] = True
    with pytest.raises(ValueError):
        decoder(canonical(raw))


@pytest.mark.parametrize("which", ["execution", "failure"])
def test_unknown_tool_names_are_rejected(which):
    encoder, decoder, value = (
        (encode_execution, decode_execution, execution())
        if which == "execution"
        else (encode_failure, decode_failure, failure())
    )
    raw = json.loads(encoder(value))
    trace = raw["assessment"]["base"]["trace"] if which == "execution" else raw["source_trace"]
    trace[0]["name"] = "invented_tool"
    with pytest.raises(ValueError):
        decoder(canonical(raw))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text.replace('"mode":', '"mode":"SCRIPTED_SDK","mode":', 1),
        lambda text: text.replace('"elapsed_seconds":0.125', '"elapsed_seconds":NaN', 1),
        lambda text: " " + text,
        lambda text: text + "x" * 524289,
        lambda _text: "\ud800",
        lambda _text: canonical({"nested": [[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]}),
    ],
)
def test_execution_decode_rejects_duplicate_nonfinite_noncanonical_and_bounded_json(mutate):
    with pytest.raises(ValueError):
        decode_execution(mutate(encode_execution(execution())))


def test_array_shapes_are_closed_before_tuple_reconstruction():
    raw = json.loads(encode_execution(execution()))
    raw["assessment"]["field_trace"] = {"0": raw["assessment"]["field_trace"][0]}
    with pytest.raises(ValueError):
        decode_execution(canonical(raw))


@pytest.mark.parametrize("value", [None, {}, execution().assessment])
def test_encode_requires_the_exact_delivery_record_type(value):
    with pytest.raises(ValueError):
        encode_execution(value)
    with pytest.raises(ValueError):
        encode_failure(value)
