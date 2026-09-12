"""Bound a local live demonstration without sharing AWS credentials with its browser."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from watershed_memory.api import create_app
from watershed_memory.budget import BoundedPlanner, DemoBudgetExhausted
from watershed_memory.cli import configured_planner, parser
from watershed_memory.planning import ReplayPlanner
from watershed_memory.service import Service


def test_live_configuration_requires_explicit_account_and_never_silently_falls_back():
    def forbidden_lookup(*_args):
        raise AssertionError("Replay and incomplete arguments must not contact AWS.")

    assert configured_planner(parser().parse_args([]), forbidden_lookup).mode["agent_enabled"] is False
    with pytest.raises(ValueError, match="explicit model"):
        configured_planner(parser().parse_args(["--provider", "bedrock"]), forbidden_lookup)
    args = parser().parse_args(["--provider", "bedrock", "--expected-account", "123456789012",
                               "--region", "us-east-1", "--model-id", "amazon.nova-pro-v1:0"])
    with pytest.raises(ValueError, match="account does not match"):
        configured_planner(args, lambda *_args: "999999999999")
    selected = configured_planner(args, lambda *_args: "123456789012")
    assert selected.mode["agent_enabled"] is True and selected.limit == 6


def test_live_allowance_is_shared_across_sessions_and_retry_receipts_cost_nothing(tmp_path):
    planner = BoundedPlanner(ReplayPlanner(), limit=1)
    service = Service(tmp_path / "quota.sqlite", planner)
    session = service.create_session()["session_id"]
    first = service.advance(session, "first")
    assert service.advance(session, "first") == first
    other = service.create_session()
    with pytest.raises(DemoBudgetExhausted):
        service.advance(other["session_id"], "second")
    assert service.snapshot(other["session_id"]) == other
    assert planner.attempts == 1


def test_failed_attempts_also_consume_allowance_and_concurrent_callers_cannot_exceed_it():
    class Failing:
        mode = ReplayPlanner.mode

        def plan(self, *_args):
            raise RuntimeError("A paid turn can fail after starting.")

    planner = BoundedPlanner(Failing(), limit=2)

    def try_once(_index):
        try:
            planner.plan({}, [])
        except DemoBudgetExhausted:
            return "blocked"
        except RuntimeError:
            return "attempted"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(try_once, range(6)))
    assert results.count("attempted") == 2 and results.count("blocked") == 4


def test_http_budget_response_and_host_boundary(tmp_path):
    service = Service(tmp_path / "api-quota.sqlite", BoundedPlanner(ReplayPlanner(), limit=1))
    with TestClient(create_app(service), base_url="http://127.0.0.1") as client:
        session = client.post("/api/sessions", json={}).json()["session_id"]
        assert client.post(f"/api/sessions/{session}/advance", json={"request_id": "one"}).status_code == 200
        assert client.post(f"/api/sessions/{session}/advance", json={"request_id": "two"}).status_code == 429
        assert client.post("/api/sessions", json={}, headers={"Host": "unrelated.example",
                          "Origin": "http://unrelated.example"}).status_code == 400
