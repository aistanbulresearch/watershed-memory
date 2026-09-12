"""Protect the public recording boundary without invoking any cloud service."""

import json
from copy import deepcopy

import pytest

from feasibility.export_evidence import FINAL_CHECKS, STAGE_CHECKS, PublicRecording, gate_checks
from watershed_memory.catalog import SOURCES


def sample_snapshot():
    return {
        "session_id": "private-browser-session",
        "case": {"id": "GALLINAS-HPCC-2022", "title": "Gallinas River"},
        "progress": {"processed": 2, "total": 3},
        "tasks": [{"id": "private-task", "kind": "MONITORING_REVIEW", "status": "ACKNOWLEDGED",
                   "title": "Review observations", "reason": "P2 observations are available.",
                   "evidence": ["event-1", "event-2"]}],
        "responses": [{"id": "private-response", "task_id": "private-task", "action": "acknowledge",
                       "note": "PRIVATE-NOTE-CANARY", "actor": "PRIVATE-ACTOR", "simulated": True}],
        "events": [], "sources": deepcopy(SOURCES), "latest_brief": {}, "trace": [{
            "tool": "strands_turn",
            "input": {"scripted_test": False, "model_id": "model", "sdk_version": "sdk"},
            "output": {"model_calls": 4, "elapsed_seconds": 5.0, "usage": {"inputTokens": 10},
                       "reasoning": "PRIVATE-MODEL-MESSAGE", "agentcore": {
                           "runtime_arn": "arn:aws:private", "aws_request_id": "private-request",
                           "runtime_session_id": "private-runtime-session", "qualifier": "proof_v1",
                           "endpoint_version_verified_before_call": "1",
                           "stop_status": "STOP_REQUEST_ACCEPTED"}}}],
        "private": "PRIVATE-UNKNOWN-FIELD",
    }


def test_public_recording_excludes_private_fields_and_keeps_stable_aliases():
    recording = PublicRecording()
    first = recording.snapshot(sample_snapshot())
    second = recording.snapshot(sample_snapshot())
    raw = json.dumps(first)
    assert "PRIVATE" not in raw and "private-" not in raw and "arn:aws:" not in raw
    assert first == second
    assert first["tasks"][0]["id"] == "R-01"
    assert first["responses"][0]["task_id"] == "R-01"
    assert first["trace"][0]["output"]["agentcore"]["runtime_session_alias"] == "Runtime session 1"
    assert first["trace"][0]["output"]["model_calls"] == 4
    assert "note" not in first["responses"][0]


def test_public_recording_rejects_unknown_tool_content():
    snapshot = sample_snapshot()
    snapshot["trace"].append({"tool": "arbitrary_private_tool", "input": {}, "output": {}})
    with pytest.raises(ValueError, match="tool"):
        PublicRecording().snapshot(snapshot)


def test_different_runtime_sessions_receive_distinct_aliases():
    recording = PublicRecording()
    first = recording.snapshot(sample_snapshot())
    other = sample_snapshot()
    other["trace"][0]["output"]["agentcore"]["runtime_session_id"] = "second-runtime-session"
    second = recording.snapshot(other)
    assert first["trace"][0]["output"]["agentcore"]["runtime_session_alias"] != (
        second["trace"][0]["output"]["agentcore"]["runtime_session_alias"])


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown"])
def test_attribution_cannot_be_removed_duplicated_or_replaced(change):
    snapshot = sample_snapshot()
    if change == "missing":
        snapshot["sources"].pop()
    elif change == "duplicate":
        snapshot["sources"].append(snapshot["sources"][0])
    else:
        snapshot["sources"][0]["url"] = "https://unrecognized.example/"
    with pytest.raises(ValueError, match="attribution"):
        PublicRecording().snapshot(snapshot)


@pytest.mark.parametrize("change", ["invented_stage", "invented_final", "failed", "scripted"])
def test_invented_or_failed_gate_checks_cannot_be_published(change):
    result = {"passed": True, "execution": "REAL_AGENTCORE_STRANDS",
              "final_checks": dict.fromkeys(FINAL_CHECKS, True),
              "stages": {phase: {"passed": True, "checks": dict.fromkeys(keys, True)}
                         for phase, keys in STAGE_CHECKS.items()}}
    assert gate_checks(result)
    if change == "invented_stage":
        result["stages"]["august"]["checks"] = {"invented_check": True}
    elif change == "invented_final":
        result["final_checks"] = {"invented_check": True}
    elif change == "failed":
        result["stages"]["august"]["checks"]["same_review"] = False
    else:
        result["execution"] = "SCRIPTED_PROTOCOL_TEST"
    with pytest.raises(ValueError):
        gate_checks(result)
