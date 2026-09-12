"""Public-hosting boundary acceptance without any AWS invocation."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from watershed_memory import cli
from watershed_memory.api import create_app
from watershed_memory.planning import ReplayPlanner
from watershed_memory.public_http import BoundaryConfig, BurstLimiter, project_public
from watershed_memory.service import Service

ORIGIN = "https://demo.example"
HEADERS = {"Origin": ORIGIN}
CANARY = "private-infrastructure-canary"


def exposed(session_limit=30, post_limit=120):
    return BoundaryConfig(("demo.example",), (ORIGIN,), session_limit, post_limit)


class CountingPlanner(ReplayPlanner):
    def __init__(self):
        self.calls = 0

    def plan(self, state, released):
        self.calls += 1
        return super().plan(state, released)


class FakeCloudPlanner(CountingPlanner):
    mode = {"id": "test_cloud", "label": "Scripted cloud metadata test",
            "agent_enabled": True, "private_mode": CANARY}

    def plan(self, state, released):
        plan = super().plan(state, released)
        plan.trace.append({"tool": "strands_turn", "input": {
            "sdk_version": "test", "model_id": "test-model", "scripted_test": True,
            "instruction_version": "test", "private_config": CANARY}, "output": {
                "model_calls": 1, "elapsed_seconds": 0.1, "stop_reason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2,
                          "unknown_metadata": {"runtime_arn": CANARY}},
                "private_output": CANARY, "agentcore": {
                    "runtime_arn": CANARY, "qualifier": CANARY,
                    "runtime_session_id": CANARY, "request_id": CANARY,
                    "trace_id": CANARY, "state_sha256": CANARY,
                    "aws_request_id": CANARY, "error_code": CANARY,
                    "endpoint_version_verified_before_call": "2",
                    "invocation_attempted": True, "stop_status": "STOP_REQUEST_ACCEPTED"}}})
        return plan


@pytest.mark.parametrize("hosts,origins", [
    ((), ()), (("*.example",), (ORIGIN,)), (("https://demo.example",), (ORIGIN,)),
    (("demo.example:443",), (ORIGIN,)), (("demo.example/path",), (ORIGIN,)),
    (("demo.example",), ()), (("demo.example",), ("https://sibling.example",)),
    (("demo.example",), ("http://demo.example",)),
    (("demo.example",), ("https://demo.example/",)),
    (("demo.example",), ("https://demo.example?x=1",)),
    (("demo.example",), ("https://demo.example#fragment",)),
    (("demo.example",), ("https://user@demo.example",)),
    (("demo.example",), ("https://demo.example:invalid",)),
    (("demo.example",), ("https://demo.example:0",)),
    (["demo.example"], (ORIGIN,)),
    (("demo.example",), [ORIGIN]), (("démö.example",), ("https://démö.example",)),
    (("-demo.example",), ("https://-demo.example",)),
    (("demo..example",), ("https://demo..example",)),
])
def test_invalid_configuration_is_rejected(hosts, origins):
    with pytest.raises(ValueError):
        BoundaryConfig(hosts, origins)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, 10001])
def test_limits_are_positive_integers(limit):
    with pytest.raises(ValueError):
        exposed(limit, 10)
    with pytest.raises(ValueError):
        exposed(10, limit)


def test_config_cannot_be_mutated():
    config = exposed()
    with pytest.raises(AttributeError):
        config.allowed_hosts = ("attacker.example",)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8765", "http://localhost:8765"])
def test_default_loopback_accepts_actual_browser_origin(tmp_path, origin):
    service = Service(tmp_path / "loopback.sqlite")
    with TestClient(create_app(service), base_url=origin) as client:
        assert client.post("/api/sessions", json={}, headers={"Origin": origin}).status_code == 201


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
@pytest.mark.parametrize("scheme,port", [("https", 8765), ("http", 9999), ("https", 9999)])
def test_loopback_rejects_cross_scheme_or_port(tmp_path, host, scheme, port):
    service = Service(tmp_path / "cross-port.sqlite")
    with TestClient(create_app(service), base_url=f"http://{host}:8765") as client:
        response = client.post("/api/sessions", json={}, headers={"Origin": f"{scheme}://{host}:{port}"})
        assert response.status_code == 403
        with service._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("origin,host", [
    (None, "demo.example"), ("http://demo.example", "demo.example"),
    ("https://sibling.example", "demo.example"),
    ("https://demo.example.attacker.example", "demo.example"),
    (ORIGIN, "demo.example.attacker.example"), (ORIGIN, "sibling.example"),
    ("null", "demo.example"), (ORIGIN + "/", "demo.example"),
    (ORIGIN, "demo.example:8443"),
])
def test_invalid_request_boundary_never_enters_planner(tmp_path, origin, host):
    planner = CountingPlanner()
    service = Service(tmp_path / "boundary.sqlite", planner)
    original = service.create_session()
    headers = {"Host": host}
    if origin is not None:
        headers["Origin"] = origin
    with TestClient(create_app(service, boundary_config=exposed()), base_url=ORIGIN) as client:
        response = client.post(f"/api/sessions/{original['session_id']}/advance",
                               json={"request_id": "bad"}, headers=headers)
        assert response.status_code in (400, 403)
        assert planner.calls == 0
        assert service.snapshot(original["session_id"]) == original


@pytest.mark.parametrize("planner_class", [CountingPlanner, FakeCloudPlanner])
def test_public_journey_projection_receipts_and_session_isolation(tmp_path, planner_class):
    planner = planner_class()
    service = Service(tmp_path / "journey.sqlite", planner)
    with TestClient(create_app(service, boundary_config=exposed()), base_url=ORIGIN) as client:
        health = client.get("/api/health")
        assert health.status_code == 200 and CANARY not in health.text
        assert planner.calls == 0
        created = client.post("/api/sessions", json={}, headers=HEADERS)
        assert created.status_code == 201
        first = created.json()
        assert CANARY not in created.text
        base = f"/api/sessions/{first['session_id']}"
        advanced = client.post(base + "/advance", json={"request_id": "july"}, headers=HEADERS)
        assert advanced.status_code == 200
        state = advanced.json()
        assert CANARY not in advanced.text
        assert client.get(base).json() == state
        duplicate = client.post(base + "/advance", json={"request_id": "july"}, headers=HEADERS)
        assert duplicate.json() == state and planner.calls == 1
        canonical = service.snapshot(first["session_id"])
        if planner_class is FakeCloudPlanner:
            assert CANARY in json.dumps(canonical)
            cloud = next(e for e in state["trace"] if e["tool"] == "strands_turn")["output"]["agentcore"]
            assert cloud == {"endpoint_version_verified_before_call": "2",
                             "invocation_attempted": True, "stop_status": "STOP_REQUEST_ACCEPTED"}
        body = {"request_id": "ack", "task_id": state["tasks"][0]["id"],
                "action": "acknowledge", "note": "Private demonstration operator note."}
        acknowledged = client.post(base + "/responses", json=body, headers=HEADERS)
        assert acknowledged.status_code == 200
        assert acknowledged.json()["responses"][0]["note"] == body["note"]
        assert CANARY not in acknowledged.text
        assert client.get(base).json() == acknowledged.json()
        other = client.post("/api/sessions", json={}, headers=HEADERS).json()
        other_base = f"/api/sessions/{other['session_id']}"
        assert body["note"] not in client.get(other_base).text
        assert client.post(other_base + "/responses", json=body, headers=HEADERS).status_code == 409
        assert client.get(other_base).json() == other
        assert client.post(base + "/advance", json={"request_id": "july"},
                           headers=HEADERS).json() == state


def test_projection_drops_unknown_top_level_fields_and_is_a_deep_copy(tmp_path):
    canonical = Service(tmp_path / "copy.sqlite").create_session()
    canonical["private_server_config"] = {"nested": CANARY}
    before = deepcopy(canonical)
    public = project_public(canonical)
    assert "private_server_config" not in public
    assert CANARY not in json.dumps(public)
    public["case"]["title"] = "Changed in response copy"
    assert canonical == before


@pytest.mark.parametrize("metadata", [None, [], CANARY, {"agentcore": CANARY},
    {"usage": {"inputTokens": {"secret": CANARY}, "outputTokens": -2,
               "totalTokens": True}, "agentcore": {"endpoint_version_verified_before_call": {"secret": CANARY},
               "stop_status": {"secret": CANARY}, "invocation_attempted": CANARY}}])
def test_malformed_sdk_metadata_is_not_published(tmp_path, metadata):
    canonical = Service(tmp_path / "malformed-metadata.sqlite").create_session()
    canonical["trace"] = [{"tool": "strands_turn", "input": None,
                           "output": metadata, "private_extra": CANARY}]
    before = deepcopy(canonical)
    projected = project_public(canonical)
    assert CANARY not in json.dumps(projected)
    assert canonical == before


@pytest.mark.parametrize("mode,trace", [(None, None), ([], {}), (CANARY, CANARY),
    ({"id": {"secret": CANARY}, "label": {"secret": CANARY}, "agent_enabled": CANARY}, [None])])
def test_malformed_mode_and_trace_have_safe_empty_projection(mode, trace):
    assert project_public({"mode": mode, "trace": trace}) == {"mode": {}, "trace": []}


@pytest.mark.parametrize("bad_number", [float("nan"), float("inf"), -float("inf"), 10**1000, True])
def test_nonfinite_or_invalid_execution_numbers_cannot_break_json(bad_number):
    value = {"trace": [{"tool": "strands_turn", "input": {}, "output": {
        "elapsed_seconds": bad_number, "model_calls": bad_number,
        "usage": {"inputTokens": bad_number}}}]}
    projected = project_public(value)
    json.dumps(projected, allow_nan=False)
    output = projected["trace"][0]["output"]
    assert "elapsed_seconds" not in output and "model_calls" not in output
    assert output["usage"] == {}


def test_private_text_in_execution_marker_fields_is_not_public():
    value = {"trace": [{"tool": "strands_turn", "input": {
        "model_id": "arn:aws:bedrock:us-east-1:123456789012:model/private",
        "sdk_version": CANARY, "instruction_version": CANARY, "scripted_test": CANARY},
        "output": {"stop_reason": CANARY, "agentcore": {
            "endpoint_version_verified_before_call": CANARY,
            "stop_status": CANARY, "invocation_attempted": CANARY}}}]}
    serialized = json.dumps(project_public(value))
    assert CANARY not in serialized and "arn:aws" not in serialized


def test_concurrent_session_creation_is_bounded_before_service(tmp_path):
    service = Service(tmp_path / "session-burst.sqlite", CountingPlanner())
    with TestClient(create_app(service, boundary_config=exposed(5, 8)), base_url=ORIGIN) as client:
        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(lambda _: client.post("/api/sessions", json={}, headers=HEADERS), range(24)))
        assert sum(r.status_code == 201 for r in responses) == 5
        blocked = [r for r in responses if r.status_code == 429]
        assert len(blocked) == 19
        assert all(1 <= int(r.headers["Retry-After"]) <= 60 for r in blocked)
        with service._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 5
        assert service.planner.calls == 0


def test_every_session_create_also_counts_against_global_posts(tmp_path):
    service = Service(tmp_path / "global.sqlite", CountingPlanner())
    with TestClient(create_app(service, boundary_config=exposed(10, 2)), base_url=ORIGIN) as client:
        first = client.post("/api/sessions", json={}, headers=HEADERS).json()
        assert client.post("/api/sessions", json={}, headers=HEADERS).status_code == 201
        assert client.post(f"/api/sessions/{first['session_id']}/advance",
                           json={"request_id": "blocked"}, headers=HEADERS).status_code == 429
        for _ in range(5):
            assert client.get("/api/health").status_code == 200
            assert client.get(f"/api/sessions/{first['session_id']}").json() == first
        assert service.planner.calls == 0


def test_malformed_posts_consume_admission_and_health_does_not(tmp_path):
    service = Service(tmp_path / "malformed.sqlite", CountingPlanner())
    with TestClient(create_app(service, boundary_config=exposed(10, 2)), base_url=ORIGIN) as client:
        for _ in range(5):
            assert client.get("/api/health").status_code == 200
        assert client.post("/api/sessions", content="{}", headers=HEADERS).status_code == 415
        assert client.post("/api/sessions", content="not-json",
                           headers={**HEADERS, "Content-Type": "application/json"}).status_code == 422
        assert client.post("/api/sessions", json={}, headers=HEADERS).status_code == 429
        assert service.planner.calls == 0


def test_rejected_hosts_and_origins_do_not_consume_judge_admission(tmp_path):
    service = Service(tmp_path / "foreign.sqlite", CountingPlanner())
    with TestClient(create_app(service, boundary_config=exposed(1, 1)), base_url=ORIGIN) as client:
        assert client.post("/api/sessions", json={}).status_code == 403
        assert client.post("/api/sessions", json={}, headers={"Origin": "https://other.example"}).status_code == 403
        assert client.post("/api/sessions", json={},
                           headers={**HEADERS, "Host": "other.example"}).status_code in (400, 403)
        assert client.post("/api/sessions", json={}, headers=HEADERS).status_code == 201
        assert client.post("/api/sessions", json={}, headers=HEADERS).status_code == 429


def test_rejected_api_responses_keep_private_cache_and_security_headers(tmp_path):
    service = Service(tmp_path / "headers.sqlite")
    with TestClient(create_app(service, boundary_config=exposed(1, 1)), base_url=ORIGIN) as client:
        responses = [client.post("/api/sessions", json={}),
                     client.post("/api/sessions", content="{}", headers=HEADERS),
                     client.post("/api/sessions", json={}, headers=HEADERS),
                     client.get("/api/health", headers={"Host": "other.example"})]
        assert [r.status_code for r in responses] == [403, 415, 429, 400]
        for response in responses:
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_concurrent_global_admission_and_monotonic_rollover():
    instant = [100.0]
    limiter = BurstLimiter(exposed(30, 7), clock=lambda: instant[0])
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: limiter.allowed("post"), range(24)))
    assert sum(allowed for allowed, _ in results) == 7
    instant[0] = 159.2
    assert limiter.allowed("post") == (False, 1)
    instant[0] = 160.0
    assert limiter.allowed("post") == (True, 0)


@pytest.mark.parametrize("options", [
    ["--port", "0"], ["--port", "65536"], ["--bind-host", "invalid bind"],
    ["--session-create-limit", "0"], ["--post-limit", "0"],
    ["--trusted-host", "demo.example"], ["--public-origin", ORIGIN],
    ["--bind-host", "0.0.0.0"],
    ["--bind-host", "0.0.0.0", "--trusted-host", "demo.example", "--public-origin", ORIGIN],
    ["--bind-host", "0.0.0.0", "--trusted-host", "demo.example", "--public-origin", "https://other.example",
     "--session-create-limit", "5", "--post-limit", "20"],
    ["--bind-host", "0.0.0.0", "--trusted-host", "*", "--public-origin", ORIGIN,
     "--session-create-limit", "5", "--post-limit", "20"],
    ["--bind-host", "invalid bind", "--trusted-host", "demo.example", "--public-origin", ORIGIN,
     "--session-create-limit", "5", "--post-limit", "20"],
])
def test_invalid_exposure_fails_before_planner_or_aws(monkeypatch, options):
    def forbidden(_args):
        raise AssertionError("Invalid exposure must fail before configuring a planner or contacting AWS")

    monkeypatch.setattr(cli, "configured_planner", forbidden)
    monkeypatch.setattr("sys.argv", ["watershed-memory", *options])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2


def test_valid_exposed_cli_uses_one_worker_and_no_capability_access_logs(tmp_path, monkeypatch):
    launched = {}
    monkeypatch.setattr(cli, "configured_planner", lambda _args: ReplayPlanner())
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: launched.update(kwargs))
    monkeypatch.setattr("sys.argv", ["watershed-memory", "--database", str(tmp_path / "cli.sqlite"),
        "--bind-host", "0.0.0.0", "--trusted-host", "demo.example", "--public-origin", ORIGIN,
        "--session-create-limit", "5", "--post-limit", "20"])
    cli.main()
    assert launched == {"host": "0.0.0.0", "port": 8765, "workers": 1, "access_log": False}
