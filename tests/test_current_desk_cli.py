"""Local launch selects existing case work without enabling a model or public bind."""

import hashlib

import pytest
from fastapi.testclient import TestClient
from test_current_case_store import CASE, query
from test_current_case_store import ready as ready

from watershed_memory.current.desk_cli import configured_app, parser


def test_existing_case_launch_preserves_source_and_work(ready):
    path, cases, _, _ = ready
    args = parser().parse_args(["--db", str(path), "--case", CASE])
    before = cases.context(CASE)
    app = configured_app(args)
    assert cases.context(CASE) == before
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    assert client.get("/api/current/case").status_code == 200
    assert not query(path, "SELECT name FROM sqlite_master WHERE name LIKE 'delivery_%'")


def test_missing_db_is_rejected_without_creating_anything(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError):
        configured_app(parser().parse_args(["--db", str(path), "--case", CASE]))
    assert list(tmp_path.iterdir()) == []


def test_replay_path_cannot_be_current_database(ready):
    path = ready[0]
    with pytest.raises(ValueError):
        configured_app(parser().parse_args(["--db", str(path), "--case", CASE, "--replay-db", str(path)]))


@pytest.mark.parametrize("port", ["0", "65536"])
def test_bad_port_is_rejected_before_creating_replay_database(ready, port):
    path = ready[0]
    with pytest.raises(ValueError):
        configured_app(parser().parse_args(["--db", str(path), "--case", CASE, "--port", port]))
    assert not path.with_suffix(".replay.sqlite").exists()


def test_unknown_case_is_rejected_before_creating_replay_database(ready):
    path = ready[0]
    with pytest.raises(ValueError):
        configured_app(parser().parse_args(["--db", str(path), "--case", "UNKNOWN"]))
    assert not path.with_suffix(".replay.sqlite").exists()


def test_source_only_database_is_unchanged_after_rejected_launch(tmp_path):
    from watershed_memory.watch.store import WatchStore

    path = tmp_path / "source-only.sqlite"
    WatchStore(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    schema = query(path, "SELECT name,sql FROM sqlite_master ORDER BY name")
    with pytest.raises(ValueError):
        configured_app(parser().parse_args(["--db", str(path), "--case", CASE]))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert query(path, "SELECT name,sql FROM sqlite_master ORDER BY name") == schema
    assert not path.with_suffix(".replay.sqlite").exists()
