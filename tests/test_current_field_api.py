"""Local HTTP field work is role-bound, private by default and retry-safe."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_current_field_desk import desk
from test_current_field_store import CASE, NOW, contents, proposed
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.api import create_app
from watershed_memory.service import Service


@pytest.fixture
def field_client(field, tmp_path, monkeypatch):
    from watershed_memory.current import http

    monkeypatch.setattr(http, "_now", lambda: NOW + timedelta(minutes=20))
    proposed(field)
    page = desk(field)
    app = create_app(Service(tmp_path / "replay.sqlite"), current_desk=page)
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)), page, field


def body(client):
    state = client.get("/api/current/case").json()
    plan = state["current_field_work"]["current_plans"][0]["plan"]
    return {
        "request_id": "field-browser-1",
        "expected_case_revision": state["case"]["revision"],
        "command": {
            "operation": "DECIDE",
            "plan_id": plan["plan_id"],
            "expected_plan_revision": plan["revision"],
            "action": "APPROVE",
        },
    }


def test_field_approval_retry_and_reload_return_original_receipt(field_client):
    browser, _, field = field_client
    command = body(browser)
    result = browser.post("/api/current/field-responses", json=command)
    assert result.status_code == 200
    saved = result.json()
    assert saved["receipt"]["plan"]["status"] == "APPROVED"
    assert "PRIVATE" not in result.text and "private_note" not in result.text
    before = contents(field[0])
    assert (
        browser.post("/api/current/field-responses", json=command).json()["receipt"]
        == saved["receipt"]
    )
    detail = browser.get("/api/current/field-work/" + command["command"]["plan_id"])
    assert detail.status_code == 200
    assert detail.json()["selected_field_work"]["plan"]["status"] == "APPROVED"
    assert contents(field[0]) == before
    assert detail.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "key,value",
    [
        ("principal", "PRIVATE-PRINCIPAL"),
        ("roles", ["COORDINATOR"]),
        ("case_id", "OTHER"),
        ("expected_simulated", False),
        ("private_note", "PRIVATE-NOTE"),
    ],
)
def test_client_authority_fields_are_refused_without_echo_or_write(field_client, key, value):
    browser, _, field = field_client
    command = body(browser)
    before = contents(field[0])
    for payload in (
        {**command, key: value},
        {**command, "command": {**command["command"], key: value}},
    ):
        response = browser.post("/api/current/field-responses", json=payload)
        assert response.status_code == 422
        assert response.json() == {"detail": "Check the response fields and try again."}
    assert contents(field[0]) == before


def test_field_conflict_never_becomes_a_second_success(field_client):
    browser, _, field = field_client
    command = body(browser)
    assert browser.post("/api/current/field-responses", json=command).status_code == 200
    before = contents(field[0])
    changed = {**command, "command": {**command["command"], "action": "CANCEL"}}
    assert browser.post("/api/current/field-responses", json=changed).status_code == 409
    assert (
        browser.post(
            "/api/current/field-responses", json={**changed, "request_id": "stale-new-id"}
        ).status_code
        == 409
    )
    assert contents(field[0]) == before


def test_http_lost_confirmation_retains_exact_retry(field_client, monkeypatch):
    browser, page, field = field_client
    command = body(browser)
    read = page._read

    def unavailable(**kwargs):
        raise RuntimeError("PRIVATE-POSTCOMMIT-ERROR")

    monkeypatch.setattr(page, "_read", unavailable)
    response = browser.post("/api/current/field-responses", json=command)
    assert response.status_code == 503 and "PRIVATE" not in response.text
    assert field[2].get_plan(CASE, command["command"]["plan_id"]).status == "APPROVED"
    monkeypatch.setattr(page, "_read", read)
    before = contents(field[0])
    assert browser.post("/api/current/field-responses", json=command).status_code == 200
    assert contents(field[0]) == before


def test_field_endpoints_keep_the_existing_local_origin_boundary(field_client):
    browser, _, field = field_client
    command = body(browser)
    before = contents(field[0])
    assert (
        browser.post(
            "/api/current/field-responses", json=command, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        browser.post(
            "/api/current/field-responses", content="{}", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    assert browser.get("/api/current/field-work/unknown-plan").status_code == 404
    assert contents(field[0]) == before


def test_read_only_server_cannot_become_a_coordinator_from_the_request(field_client):
    browser, page, field = field_client
    command = body(browser)
    page.principal = None
    before = contents(field[0])
    assert browser.post("/api/current/field-responses", json=command).status_code == 400
    assert contents(field[0]) == before


@pytest.mark.parametrize("identity", ["x" * 129, "%00PRIVATE-PATH", "%C3%B6zel-kayit"])
def test_invalid_field_path_is_a_permanent_client_error_without_echo(field_client, identity):
    browser, _, field = field_client
    before = contents(field[0])
    response = browser.get("/api/current/field-work/" + identity)
    assert response.status_code == 400
    assert response.json() == {"detail": "Check the selected field work identifier."}
    assert contents(field[0]) == before
