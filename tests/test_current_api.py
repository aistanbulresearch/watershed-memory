"""The local current desk shares HTTP guards without exposing private ledgers."""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_current_case_store import CASE, LATER, NOW, stage
from test_current_case_store import ready as ready

from watershed_memory.api import create_app
from watershed_memory.current.desk import CurrentDesk
from watershed_memory.public_http import BoundaryConfig
from watershed_memory.service import Service


@pytest.fixture
def client(ready, tmp_path, monkeypatch):
    from watershed_memory.current import http

    monkeypatch.setattr(http, "_now", lambda: NOW)
    stage(ready[1], ready[3][-1])
    desk = CurrentDesk(ready[1], CASE)
    app = create_app(Service(tmp_path / "historical.sqlite"), current_desk=desk)
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)), desk


def body(client, **changes):
    state = client.get("/api/current/case").json()
    result = {"request_id": "browser-request-1", "task_id": state["work"][0]["task_id"],
              "expected_revision": 1, "action": "MODIFY", "note": "PRIVATE-HTTP-NOTE-CANARY",
              "title": "Verify the next station reading", "next_check_at": LATER.isoformat()}
    result.update(changes)
    return result


def test_actual_action_returns_safe_receipt_and_reload_keeps_plan(client):
    browser, _ = client
    command = body(browser)
    response = browser.post("/api/current/responses", json=command)
    assert response.status_code == 200
    result = response.json()
    assert result["receipt"]["title"] == command["title"] and result["receipt"]["revision"] == 2
    assert "PRIVATE-HTTP-NOTE-CANARY" not in response.text
    assert browser.get("/api/current/case").json()["work"][0]["revision"] == 2
    assert browser.post("/api/current/responses", json=command).json()["receipt"] == result["receipt"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("changes", [
    {"expected_revision": True}, {"expected_revision": "1"}, {"note": 42},
    {"title": 42}, {"request_id": 55}, {"task_id": 88},
    {"unknown": "PRIVATE-EXTRA-CANARY"}, {"next_check_at": 1788000000},
    {"action": "NOT-AN-ACTION"},
])
def test_invalid_request_types_never_echo_private_inputs(client, changes):
    browser, _ = client
    response = browser.post("/api/current/responses", json=body(browser, **changes))
    assert response.status_code == 422
    assert response.json() == {"detail": "Check the response fields and try again."}
    assert browser.get("/api/current/case").json()["work"][0]["revision"] == 1


def test_conflict_and_invalid_action_are_safe_and_do_not_retry(client):
    browser, _ = client
    command = body(browser)
    assert browser.post("/api/current/responses", json=command).status_code == 200
    command["request_id"] = "stale-browser"
    response = browser.post("/api/current/responses", json=command)
    assert response.status_code == 409 and "PRIVATE" not in response.text
    command.update(request_id="invalid-date", expected_revision=2, next_check_at=(NOW-timedelta(seconds=1)).isoformat())
    assert browser.post("/api/current/responses", json=command).status_code == 400


def test_invalid_json_is_not_echoed(client):
    browser, _ = client
    response = browser.post("/api/current/responses", content='{"note":"PRIVATE-MALFORMED', headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and "PRIVATE" not in response.text


def test_same_origin_content_type_and_host_guards_apply(client):
    browser, _ = client
    command = body(browser)
    assert browser.post("/api/current/responses", json=command, headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.post("/api/current/responses", content=json.dumps(command), headers={"Content-Type": "text/plain"}).status_code == 415
    assert browser.get("/api/current/case", headers={"Host": "evil.example"}).status_code == 400
    assert browser.get("/api/current/case").json()["work"][0]["revision"] == 1


def test_failed_snapshot_after_committed_action_is_ambiguous_not_rejected(client, monkeypatch):
    browser, desk = client
    command = body(browser)
    original = desk._read

    def broken(**kwargs):
        raise ValueError("PRIVATE-DB-PATH-CANARY")

    monkeypatch.setattr(desk, "_read", broken)
    result = browser.post("/api/current/responses", json=command)
    assert result.status_code == 503 and "PRIVATE" not in result.text
    monkeypatch.setattr(desk, "_read", original)
    assert browser.post("/api/current/responses", json=command).json()["receipt"]["revision"] == 2


def test_assessment_error_paths_never_reveal_ledger_paths(client, monkeypatch):
    browser, desk = client
    assert browser.get("/api/current/assessments/attempt-" + "a"*32).status_code == 404

    def broken(*args):
        raise RuntimeError("PRIVATE-FILESYSTEM-PATH")

    monkeypatch.setattr(desk, "assessment", broken)
    result = browser.get("/api/current/assessments/attempt-" + "a"*32)
    assert result.status_code == 503 and "PRIVATE" not in result.text


def test_current_desk_cannot_be_configured_on_public_origin(ready, tmp_path):
    with pytest.raises(ValueError, match="local"):
        create_app(Service(tmp_path / "old.sqlite"), current_desk=CurrentDesk(ready[1], CASE),
                   boundary_config=BoundaryConfig(("demo.example",), ("https://demo.example",)))


def test_historical_app_without_desk_keeps_current_routes_disabled(tmp_path):
    browser = TestClient(create_app(Service(tmp_path / "old.sqlite")), base_url="http://localhost")
    assert browser.get("/api/current/case").status_code == 404
    assert browser.get("/current").status_code == 404
    assert browser.post("/api/sessions", json={}).status_code == 201


@pytest.mark.parametrize("peer", ["203.0.113.10", "::ffff:203.0.113.10", "unknown"])
def test_remote_peer_cannot_spoof_local_host_to_read_or_change_case(client, peer):
    browser, _ = client
    command = body(browser)
    remote = TestClient(browser.app, base_url="http://localhost", client=(peer, 50001))
    for path in ("/current", "/api/current/case", "/api/current/assessments/attempt-" + "a"*32):
        assert remote.get(path).status_code == 403
    assert remote.post("/api/current/responses", json=command).status_code == 403
    assert browser.get("/api/current/case").json()["work"][0]["revision"] == 1


def test_ipv6_loopback_peer_can_read_local_case(client):
    browser, _ = client
    local = TestClient(browser.app, base_url="http://localhost", client=("::1", 50001))
    assert local.get("/api/current/case").status_code == 200
