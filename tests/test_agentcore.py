"""Offline runtime acceptance using the real Strands SDK and a scripted provider."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from starlette.testclient import TestClient
from test_strands import ContextDrivenScriptedModel

from runtime import entrypoint
from watershed_memory.agentcore_client import AgentCoreClient, AgentCoreTurnError
from watershed_memory.agentcore_protocol import (
    MAX_RESPONSE_BYTES,
    AgentCoreRequest,
    canonical_hash,
    make_request,
)
from watershed_memory.catalog import PACKETS
from watershed_memory.service import Service
from watershed_memory.strands_agent import StrandsPlanner

ACCOUNT = "123456789012"
ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:runtime/watershed_memory_test-abc123"
MODEL = "context-scripted-provider"


def scripted_planner():
    return StrandsPlanner(ContextDrivenScriptedModel, model_id=MODEL, scripted_test=True)


class Body:
    def __init__(self, value, fail=False):
        self.value, self.fail, self.closed = value, fail, False

    def read(self, amount):
        if self.fail:
            raise TimeoutError("Private remote exception must not enter saved evidence.")
        return self.value[:amount]

    def close(self):
        self.closed = True


class Runtime:
    def __init__(self, fault=None):
        self.fault, self.calls, self.stops, self.bodies = fault, [], [], []

    def get_agent_runtime_endpoint(self, **_kwargs):
        result = {"status": "READY", "agentRuntimeArn": ARN, "liveVersion": "1",
                "agentRuntimeEndpointArn": ARN + ("/runtime-endpoint/wrong" if self.fault == "endpoint_arn"
                                                   else "/runtime-endpoint/proof_v1")}
        if self.fault == "endpoint_version":
            result["targetVersion"] = "2"
        return result

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self.fault == "invoke_error":
            raise TimeoutError("Private remote exception must not enter saved evidence.")
        time.sleep(0.05)
        result = entrypoint.handle(kwargs["payload"], scripted_planner())
        if self.fault == "identity":
            result["revision"] += 1
        if self.fault == "forged_plan":
            result["plan"]["trace"][0]["output"]["case"]["title"] = "Fabricated context"
        if self.fault == "missing_marker":
            result["plan"]["trace"].pop()
        if self.fault == "duplicate_marker":
            result["plan"]["trace"].append(deepcopy(result["plan"]["trace"][-1]))
        if self.fault == "wrong_model":
            result["plan"]["trace"][-1]["input"]["model_id"] = "unexpected-model"
        raw = json.dumps(result).encode()
        if self.fault == "truncated":
            raw = raw[:-7]
        if self.fault == "oversized":
            raw = b" " * (MAX_RESPONSE_BYTES + 2)
        body = Body(raw, fail=self.fault == "read_error")
        self.bodies.append(body)
        return {"response": body, "runtimeSessionId": kwargs["runtimeSessionId"],
                "statusCode": 503 if self.fault == "http_error" else 200,
                "contentType": "text/html" if self.fault == "html" else "application/json",
                "ResponseMetadata": {"RequestId": "x" * 10000 if self.fault == "metadata_size"
                                     else "aws-fixture-request", "HTTPStatusCode": 200}}

    def stop_runtime_session(self, **kwargs):
        self.stops.append(kwargs)
        if self.fault in ("stop_error", "invoke_error"):
            raise TimeoutError("Private cleanup details must not enter saved evidence.")
        return {}


def adapter(runtime, scripted=True):
    return AgentCoreClient(ARN, "proof_v1", "us-east-1", expected_account=ACCOUNT,
        expected_version="1", model_id=MODEL, boto_client=runtime, control_client=runtime,
        scripted_test=scripted)


def test_two_runtime_sessions_continue_saved_case_and_preserve_local_notes(tmp_path):
    path = tmp_path / "case.sqlite"
    service = Service(path)
    session = service.create_session()["session_id"]
    july = service.advance(session, "july")
    task_id = july["tasks"][0]["id"]
    service.respond(session, "ack", task_id, "acknowledge", "This note stays on the operator desk.")
    runtime = Runtime()
    service.planner = adapter(runtime)
    august = service.advance(session, "august")
    september = Service(path, adapter(runtime)).advance(session, "september")
    assert august["tasks"][0]["id"] == task_id
    assert len(september["tasks"][0]["evidence"]) == 3
    assert september["tasks"][0]["status"] == "ACKNOWLEDGED"
    assert september["tasks"][1]["kind"] == "EVIDENCE_GAP_REVIEW"
    assert september["mode"]["agent_enabled"] is False
    assert len(runtime.calls) == 2
    sessions = {call["runtimeSessionId"] for call in runtime.calls}
    assert len(sessions) == 2 and all(len(s) >= 33 for s in sessions)
    assert all("This note stays" not in call["payload"].decode() for call in runtime.calls)
    assert all('"actor"' not in call["payload"].decode() for call in runtime.calls)
    assert all(call["qualifier"] == "proof_v1" for call in runtime.stops)
    assert Service(path).snapshot(session) == september
    assert service.advance(session, "september") == september
    assert len(runtime.calls) == 2
    sdk_markers = [t for t in september["trace"] if t["tool"] == "strands_turn"]
    assert len(sdk_markers) == 1
    assert sdk_markers[0]["output"]["agentcore"]["stop_status"] == "STOP_REQUEST_ACCEPTED"
    assert september["responses"][0]["note"].startswith("This note stays")
    assert "_turn" not in september


@pytest.mark.parametrize("fault", ["identity", "forged_plan", "missing_marker", "duplicate_marker",
    "wrong_model", "truncated", "oversized", "read_error", "http_error", "html", "invoke_error",
    "endpoint_version", "endpoint_arn", "metadata_size"])
def test_transport_failure_never_commits_and_keeps_cleanup_evidence(tmp_path, fault):
    runtime = Runtime(fault)
    service = Service(tmp_path / "failure.sqlite", adapter(runtime))
    before = service.create_session()
    with pytest.raises(AgentCoreTurnError) as caught:
        service.advance(before["session_id"], "failed")
    assert service.snapshot(before["session_id"]) == before
    assert all(body.closed for body in runtime.bodies)
    assert "Private" not in json.dumps(caught.value.evidence)
    assert len(runtime.calls) == (0 if fault in ("endpoint_version", "endpoint_arn") else 1)
    if fault == "invoke_error":
        assert caught.value.evidence["stop_status"] == "STOP_FAILED"
        assert caught.value.evidence["stop_error_code"] == "TimeoutError"


def test_scripted_marker_cannot_be_committed_with_real_agent_label(tmp_path):
    runtime = Runtime()
    service = Service(tmp_path / "label.sqlite", adapter(runtime, scripted=False))
    before = service.create_session()
    with pytest.raises(AgentCoreTurnError):
        service.advance(before["session_id"], "label")
    assert service.snapshot(before["session_id"]) == before


def test_stop_failure_is_visible_after_success_without_losing_valid_plan(tmp_path):
    runtime = Runtime("stop_error")
    service = Service(tmp_path / "stop.sqlite", adapter(runtime))
    state = service.advance(service.create_session()["session_id"], "stop")
    meta = state["trace"][-2]["output"]["agentcore"]
    assert meta["stop_status"] == "STOP_FAILED" and meta["stop_error_code"] == "TimeoutError"


def test_four_service_instances_claim_one_runtime_invocation(tmp_path):
    runtime = Runtime()
    path = tmp_path / "concurrent.sqlite"
    services = [Service(path, adapter(runtime)) for _ in range(4)]
    session = services[0].create_session()["session_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda service: service.advance(session, "same"), services))
    assert all(result == results[0] for result in results)
    assert len(runtime.calls) == len(runtime.stops) == 1
    assert len(results[0]["tasks"]) == 1


def test_nested_fields_hashes_and_future_evidence_are_checked_before_planning(tmp_path):
    state = Service(tmp_path / "schema.sqlite").create_session()
    request = make_request(state, PACKETS[:1], "schema", 0).model_dump()
    for key in ("actor", "note", "unexpected"):
        bad = deepcopy(request)
        bad["state"]["case"][key] = "must not pass"
        bad["state_hash"] = canonical_hash(bad["state"])
        with pytest.raises(ValueError):
            AgentCoreRequest.model_validate(bad)
    bad = deepcopy(request)
    bad["released"] = [PACKETS[1]]
    bad["released_hash"] = canonical_hash(bad["released"])
    bad["event_id"] = PACKETS[1]["event_id"]
    with pytest.raises(ValueError):
        AgentCoreRequest.model_validate(bad)
    bad = deepcopy(request)
    bad["state_hash"] = "0" * 64
    with pytest.raises(ValueError):
        AgentCoreRequest.model_validate(bad)
    bad = deepcopy(request)
    bad["schema_version"] = 2
    with pytest.raises(ValueError):
        AgentCoreRequest.model_validate(bad)
    bad = deepcopy(request)
    bad["released"][0]["summary"]["rain_usgs_inches"]["window"]["rows"] = False
    bad["released_hash"] = canonical_hash(bad["released"])
    with pytest.raises(ValueError):
        AgentCoreRequest.model_validate(bad)
    with pytest.raises(ValueError):
        entrypoint.handle({"oversized": "x" * 65536}, scripted_planner())


def test_runtime_http_protocol_uses_actual_agentcore_server(tmp_path, monkeypatch):
    state = Service(tmp_path / "http.sqlite").create_session()
    request = make_request(state, PACKETS[:1], "http", 0)
    monkeypatch.setenv("WATERSHED_MODEL_ID", MODEL)
    monkeypatch.setenv("WATERSHED_AWS_REGION", "us-east-1")
    monkeypatch.setattr(entrypoint, "bedrock_planner", lambda *_args: scripted_planner())
    with TestClient(entrypoint.app) as client:
        assert client.get("/ping").status_code == 200
        response = client.post("/invocations", json=request.model_dump())
    assert response.status_code == 200
    assert response.json()["plan"]["trace"][-1]["input"]["scripted_test"] is True
