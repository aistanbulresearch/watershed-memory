"""Exercise the real HTTP contract, including the publication boundary."""

import pytest
from fastapi.testclient import TestClient

from watershed_memory.api import create_app
from watershed_memory.service import Service


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Service(tmp_path / "http.sqlite"))) as instance:
        yield instance


def test_http_journey_and_restore(client):
    response = client.post("/api/sessions", json={})
    assert response.status_code == 201
    session = response.json()["session_id"]
    base = f"/api/sessions/{session}"
    for index in range(3):
        response = client.post(f"{base}/advance", json={"request_id": f"event-{index}"})
        assert response.status_code == 200
    state = response.json()
    result = client.post(f"{base}/responses", json={"request_id": "response-1",
        "task_id": state["tasks"][0]["id"], "action": "acknowledge", "note": "Assigned for review."})
    assert result.status_code == 200
    assert client.get(base).json() == result.json()
    assert result.json()["responses"][0]["simulated"] is True


def test_bad_requests_and_cross_origin_cannot_change_case(client):
    session = client.post("/api/sessions", json={}).json()["session_id"]
    base = f"/api/sessions/{session}"
    before = client.get(base).json()
    assert client.post(f"{base}/advance", json={"request_id": "a", "event_id": "future"}).status_code == 422
    assert client.post(f"{base}/advance", json={"request_id": "a"},
                       headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.post(f"{base}/advance", content="{}").status_code == 415
    assert client.get(base).json() == before
    assert client.get("/api/sessions/unknown").status_code == 404


def test_only_packaged_interface_is_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    for private in ("/.local/director/STATUS.md", "/AGENTS.md", "/.git/config", "/static/../service.py"):
        assert client.get(private).status_code == 404
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]
    assert client.get("/api/health").headers["cache-control"] == "no-store"
