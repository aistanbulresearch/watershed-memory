"""Public current cases must be isolated, persistent and bounded without AWS."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from deployment.host_current import COOKIE, MAX_BODY, create_public_app

HOST = "watershed.aistanbulresearch.com"
ORIGIN = "https://" + HOST


def make(tmp_path, **kwargs):
    return create_public_app(tmp_path / "sessions", HOST, **kwargs)


def browser(app):
    return TestClient(app, base_url=ORIGIN)


def start(client):
    response = client.get("/current")
    assert response.status_code == 200
    sid = client.cookies.get(COOKIE)
    assert sid and len(sid) == 32
    return sid


def test_static_health_and_root_do_not_allocate(tmp_path):
    app = make(tmp_path)
    client = browser(app)
    assert client.get("/health").status_code == 200
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/current"
    assert client.get("/static/current.css").status_code == 200
    assert not list((tmp_path / "sessions").iterdir())
    assert not client.cookies.get(COOKIE)


def test_secure_cookie_and_independent_cases(tmp_path):
    app = make(tmp_path)
    a, b = browser(app), browser(app)
    sid_a, sid_b = start(a), start(b)
    assert sid_a != sid_b
    snapshot = a.get("/api/current/case").json()
    assert snapshot["case"]["simulated"] is True
    assert a.get("/api/current/case").headers["cache-control"] == "no-store"
    cookie = browser(app).get("/current").headers["set-cookie"]
    for flag in ["HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=86400"]:
        assert flag in cookie
    assert "Domain=" not in cookie


def test_saved_state_survives_restart_and_other_browser(tmp_path):
    app = make(tmp_path)
    a, b = browser(app), browser(app)
    sid_a, sid_b = start(a), start(b)
    before_a = a.get("/api/current/case").json()
    before_b = b.get("/api/current/case").json()
    task = before_a["work"][0]
    payload = {"request_id": "public-isolation-check", "task_id": task["task_id"],
               "expected_revision": task["revision"], "action": "APPROVE", "note": "Only my demonstration"}
    response = a.post("/api/current/responses", json=payload, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    assert b.get("/api/current/case").json()["case"]["revision"] == before_b["case"]["revision"]
    after = a.get("/api/current/case").json()
    assert after["case"]["revision"] > before_a["case"]["revision"]
    restarted = browser(make(tmp_path))
    restarted.cookies.set(COOKIE, sid_a)
    assert restarted.get("/api/current/case").json()["case"]["revision"] == after["case"]["revision"]
    replay = restarted.post("/api/current/responses", json=payload, headers={"Origin": ORIGIN})
    assert replay.status_code == 200
    assert restarted.get("/api/current/case").json()["case"]["revision"] == after["case"]["revision"]
    assert sid_b != sid_a


@pytest.mark.parametrize("headers", [{"Host": "evil.example"}, {"Host": HOST + ":443"}])
def test_wrong_host_before_allocation(tmp_path, headers):
    client = browser(make(tmp_path))
    assert client.get("/current", headers=headers).status_code in (400, 403, 404)
    assert list((tmp_path / "sessions").iterdir()) == []


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://" + HOST])
def test_wrong_origin_rejected_before_allocation(tmp_path, origin):
    client = browser(make(tmp_path))
    headers = {} if origin is None else {"Origin": origin}
    assert client.post("/api/current/responses", json={}, headers=headers).status_code == 403
    assert list((tmp_path / "sessions").iterdir()) == []


def test_missing_cookie_mutation_refused(tmp_path):
    client = browser(make(tmp_path))
    assert client.post("/api/current/responses", json={}, headers={"Origin": ORIGIN}).status_code == 401
    assert list((tmp_path / "sessions").iterdir()) == []


@pytest.mark.parametrize("cookie", ["../../outside", "F" * 32, "a" * 32])
def test_invalid_or_unknown_cookie_no_case_creation(tmp_path, cookie):
    client = browser(make(tmp_path))
    client.cookies.set(COOKIE, cookie)
    assert client.get("/api/current/case").status_code == 401
    assert list((tmp_path / "sessions").iterdir()) == []


def test_body_limit_before_dispatch(tmp_path):
    client = browser(make(tmp_path))
    start(client)
    before = client.get("/api/current/case").json()["case"]["revision"]
    response = client.post("/api/current/responses", content=b"x" * (MAX_BODY + 1),
                           headers={"Origin": ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 413
    assert client.get("/api/current/case").json()["case"]["revision"] == before


def test_json_required(tmp_path):
    client = browser(make(tmp_path))
    assert client.post("/api/current/responses", content="x", headers={"Origin": ORIGIN}).status_code == 415


def test_capacity_and_create_burst(tmp_path):
    app = make(tmp_path, max_sessions=1)
    start(browser(app))
    assert browser(app).get("/current").status_code == 503
    other = create_public_app(tmp_path / "limited", HOST, session_create_per_minute=1)
    start(browser(other))
    denied = browser(other).get("/current")
    assert denied.status_code == 429 and "retry-after" in denied.headers


def test_expiry_does_not_replace_saved_case(tmp_path):
    app = make(tmp_path)
    client = browser(app)
    sid = start(client)
    directory = tmp_path / "sessions" / sid
    meta = json.loads((directory / "metadata.json").read_text())
    meta["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    (directory / "metadata.json").write_text(json.dumps(meta))
    before = (directory / "current-work.sqlite").read_bytes()
    assert client.get("/api/current/case").status_code == 401
    assert client.get("/current").status_code == 401
    assert (directory / "current-work.sqlite").read_bytes() == before
    assert len(list((tmp_path / "sessions").iterdir())) == 1


def test_missing_database_not_recreated(tmp_path):
    client = browser(make(tmp_path))
    sid = start(client)
    path = tmp_path / "sessions" / sid / "current-work.sqlite"
    path.rename(path.with_suffix(".preserved"))
    restarted = browser(make(tmp_path))
    restarted.cookies.set(COOKIE, sid)
    assert restarted.get("/api/current/case").status_code == 503
    assert not path.exists()


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5])
def test_bad_capacity_before_directory_creation(tmp_path, maximum):
    with pytest.raises(ValueError):
        make(tmp_path, max_sessions=maximum)
    assert not (tmp_path / "sessions").exists()


def test_head_does_not_allocate(tmp_path):
    client = browser(make(tmp_path))
    assert client.head("/current").status_code in (401, 405)
    assert list((tmp_path / "sessions").iterdir()) == []


def test_no_aws_construction(tmp_path, monkeypatch):
    import boto3
    def forbidden(*args, **kwargs):
        raise AssertionError("Public demo must not construct AWS sessions")
    monkeypatch.setattr(boto3, "Session", forbidden)
    client = browser(make(tmp_path))
    start(client)
    assert client.get("/api/current/case").status_code == 200


def test_lifespan(tmp_path):
    with browser(make(tmp_path)) as client:
        assert client.get("/health").status_code == 200


def test_invalid_sessions_do_not_consume_post_allowance(tmp_path):
    app = make(tmp_path, post_per_minute=1)
    bad, good = browser(app), browser(app)
    start(good)
    for _ in range(3):
        assert bad.post("/api/current/responses", json={}, headers={"Origin": ORIGIN}).status_code == 401
    assert good.post("/api/current/responses", json={}, headers={"Origin": ORIGIN}).status_code == 422


def test_post_allowance_isolated_per_browser(tmp_path):
    app = make(tmp_path, post_per_minute=2)
    a, b = browser(app), browser(app)
    start(a)
    start(b)
    for _ in range(2):
        assert a.post("/api/current/responses", json={}, headers={"Origin": ORIGIN}).status_code == 422
    assert a.post("/api/current/responses", json={}, headers={"Origin": ORIGIN}).status_code == 429
    assert b.post("/api/current/responses", json={}, headers={"Origin": ORIGIN}).status_code == 422


def test_valid_expired_capacity_reclaimed_on_new_allocation(tmp_path):
    app = make(tmp_path, max_sessions=1)
    sid = start(browser(app))
    directory = tmp_path / "sessions" / sid
    meta_path = directory / "metadata.json"
    meta = json.loads(meta_path.read_text())
    meta["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    meta_path.write_text(json.dumps(meta))
    new_sid = start(browser(app))
    assert new_sid != sid
    assert not directory.exists()
    assert len(list((tmp_path / "sessions").iterdir())) == 1


@pytest.mark.parametrize("corruption", [[], {"session_id": "wrong", "expires_at": "2999-01-01T00:00:00+00:00"},
    {"session_id": "SAME", "expires_at": "2999-01-01T00:00:00"}])
def test_corrupt_metadata_is_generic_503(tmp_path, corruption):
    app = make(tmp_path)
    client = browser(app)
    sid = start(client)
    if isinstance(corruption, dict) and corruption.get("session_id") == "SAME":
        corruption = dict(corruption, session_id=sid)
    (tmp_path / "sessions" / sid / "metadata.json").write_text(json.dumps(corruption))
    response = client.get("/api/current/case")
    assert response.status_code == 503
    assert str(tmp_path) not in response.text


@pytest.mark.parametrize("child", ["metadata.json", "approved-site.json", "current-work.sqlite", "replay.sqlite",
    "current-work.sqlite-wal", "current-work.sqlite-shm"])
def test_linked_session_child_is_refused(tmp_path, child):
    client = browser(make(tmp_path))
    sid = start(client)
    directory = tmp_path / "sessions" / sid
    path = directory / child
    outside = tmp_path / ("outside-" + child)
    content = path.read_bytes() if path.exists() else b"outside-canary"
    outside.write_bytes(content)
    if path.exists():
        path.rename(path.with_suffix(path.suffix + ".preserved"))
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable; repeat on Linux host")
    restarted = browser(make(tmp_path))
    restarted.cookies.set(COOKIE, sid)
    response = restarted.get("/api/current/case")
    assert response.status_code == 503
    assert outside.read_bytes() == content


def raw_request(app, path, method, extra_headers, chunks):
    async def run():
        events = [{"type": "http.request", "body": chunk, "more_body": i < len(chunks)-1}
                  for i, chunk in enumerate(chunks)]
        output = []
        async def receive():
            return events.pop(0) if events else {"type": "http.disconnect"}
        async def send(message):
            output.append(message)
        await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                   "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
                   "query_string": b"", "root_path": "", "server": (HOST,443),
                   "client": ("127.0.0.1", 5000), "headers": [(b"host", HOST.encode()), *extra_headers]}, receive, send)
        return output
    return asyncio.run(run())


def test_chunked_body_limit(tmp_path):
    app = make(tmp_path)
    sid = start(browser(app))
    output = raw_request(app, "/api/current/responses", "POST",
        [(b"origin",ORIGIN.encode()),(b"content-type",b"application/json"),
         (b"cookie",f"{COOKIE}={sid}".encode())], [b"x"*32768, b"x"*32769])
    starts = [x for x in output if x["type"] == "http.response.start"]
    assert len(starts) == 1 and starts[0]["status"] == 413


def test_duplicate_host_no_session_allocation(tmp_path):
    app = make(tmp_path)
    output = raw_request(app,"/current","GET",[(b"host",b"evil.example")],[b""])
    assert next(x for x in output if x["type"]=="http.response.start")["status"] == 400
    assert list((tmp_path / "sessions").iterdir()) == []


def test_fake_cookie_churn_cannot_reset_valid_post_allowance(tmp_path):
    app = make(tmp_path, post_per_minute=1, max_sessions=2)
    good = browser(app)
    sid = start(good)
    assert good.post("/api/current/responses",json={},headers={"Origin":ORIGIN}).status_code == 422
    bad = browser(app)
    for number in range(8):
        bad.cookies.clear()
        bad.cookies.set(COOKIE, format(number,"032x"))
        assert bad.post("/api/current/responses",json={},headers={"Origin":ORIGIN}).status_code == 401
    assert list(app.post_limiters) == [sid]
    assert good.post("/api/current/responses",json={},headers={"Origin":ORIGIN}).status_code == 429


@pytest.mark.parametrize("extra", ["unexpected-wal", "current-work.sqlite-shm"])
def test_damaged_expired_directory_is_never_partly_deleted(tmp_path, extra):
    app=make(tmp_path,max_sessions=1)
    sid=start(browser(app))
    directory=tmp_path/"sessions"/sid
    meta_path=directory/"metadata.json"
    meta=json.loads(meta_path.read_text())
    meta["expires_at"]=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    meta_path.write_text(json.dumps(meta))
    if extra=="unexpected-wal":
        (directory/extra).write_text("preserve me")
    else:
        target=directory/extra
        if target.exists(): target.unlink()
        target.mkdir()
    before={x.name:x.read_bytes() if x.is_file() else None for x in directory.iterdir()}
    assert browser(app).get("/current").status_code == 503
    after={x.name:x.read_bytes() if x.is_file() else None for x in directory.iterdir()}
    assert after == before
