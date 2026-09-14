"""CLI validation and persistence across genuine separate local processes."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watershed_memory.watch import __main__ as command
from watershed_memory.watch.runner import WatchRunner
from watershed_memory.watch.store import MonitorConfig, WatchStore
from watershed_memory.watch.usgs import HTTPResponse, USGSClient


@pytest.mark.parametrize(
    "arguments",
    [
        ["init"],
        ["init", "--case-id", "CASE", "--start", "2026-09-12"],
        ["init", "--case-id", "CASE", "--start", "2026-09-12 11:00:00+00:00"],
        ["run"],
        ["run", "--max-polls", "0", "--max-seconds", "10"],
        ["events", "--limit", "0"],
    ],
)
def test_bad_arguments_fail_before_filesystem_or_network(tmp_path, monkeypatch, arguments):
    path = tmp_path / "must-not-exist" / "watch.sqlite"

    def unexpected(*args, **kwargs):
        pytest.fail("invalid input reached the network boundary")

    monkeypatch.setattr(command.USGSClient, "fetch", unexpected)
    with pytest.raises(SystemExit):
        command.main([*arguments, "--db", str(path), "--monitor", "gallinas"])
    assert not path.parent.exists()


def test_read_command_cannot_create_a_missing_database(tmp_path):
    path = tmp_path / "missing" / "watch.sqlite"
    with pytest.raises(SystemExit):
        command.main(["status", "--db", str(path), "--monitor", "gallinas"])
    assert not path.parent.exists()


@pytest.mark.parametrize(
    "monitor,case",
    [
        ("invalid monitor", "CURRENT"),
        ("good", "GALLINAS-HPCC-2022"),
        ("good", "bad case"),
    ],
)
def test_invalid_initial_config_creates_no_files(tmp_path, monitor, case):
    path = tmp_path / "uncreated" / "watch.sqlite"
    with pytest.raises(SystemExit):
        command.main(
            [
                "init",
                "--db",
                str(path),
                "--monitor",
                monitor,
                "--case-id",
                case,
                "--start",
                "2026-09-12T11:00:00Z",
            ]
        )
    assert not path.parent.exists()


@pytest.mark.parametrize("polls,seconds", [("0", "10"), ("101", "10"), ("1", "0"), ("1", "21601")])
def test_invalid_run_limits_never_open_an_existing_database(tmp_path, monkeypatch, polls, seconds):
    path = tmp_path / "foreign.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sentinel(value TEXT)")
        db.execute("INSERT INTO sentinel VALUES('preserve')")
    before = path.read_bytes()

    def unexpected(*args, **kwargs):
        pytest.fail("invalid CLI arguments reached the database constructor")

    monkeypatch.setattr(command, "WatchStore", unexpected)
    with pytest.raises(SystemExit):
        command.main(
            [
                "run",
                "--db",
                str(path),
                "--monitor",
                "gallinas",
                "--max-polls",
                polls,
                "--max-seconds",
                seconds,
            ]
        )
    assert path.read_bytes() == before


def test_cli_init_and_status_are_separate_processes_with_same_case(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "watch.sqlite"
    common = ["--db", str(database), "--monitor", "gallinas-cli"]
    env = dict(os.environ, PYTHONPATH=str(root))

    def invoke(*arguments):
        result = subprocess.run(
            [sys.executable, "-m", "watershed_memory.watch", *arguments],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    invoke("init", *common, "--case-id", "GALLINAS-CLI-CURRENT", "--start", "2026-09-12T11:00:00Z")
    first = invoke("status", *common)
    second = invoke("status", *common)
    assert first == second
    assert first["case_id"] == "GALLINAS-CLI-CURRENT"
    assert invoke("events", *common) == []


def test_mismatched_source_config_is_rejected_before_fetch(tmp_path, monkeypatch):
    path = tmp_path / "watch.sqlite"
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    store = WatchStore(path)
    store.register(
        MonitorConfig(
            monitor_id="other", station_id="USGS-12345678", case_id="OTHER-CURRENT", start_at=now
        ),
        now=now,
    )
    calls = []
    monkeypatch.setattr(command.USGSClient, "fetch", lambda *a, **kw: calls.append(1))
    with pytest.raises(SystemExit):
        command.main(["poll", "--db", str(path), "--monitor", "other"])
    assert calls == []


def test_keyboard_interrupt_is_clean_and_returns_stable_exit_code(tmp_path, monkeypatch, capsys):
    path = tmp_path / "watch.sqlite"
    common = ["--db", str(path), "--monitor", "gallinas-interrupt"]
    command.main(
        [
            "init",
            *common,
            "--case-id",
            "GALLINAS-INTERRUPT-CURRENT",
            "--start",
            "2026-09-12T11:00:00Z",
        ]
    )

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(command.WatchRunner, "run", interrupted)
    assert command.main(["run", *common, "--max-polls", "1", "--max-seconds", "10"]) == 130
    assert "Traceback" not in capsys.readouterr().err


def test_gallinas_preset_recovers_an_observation_published_55_minutes_late(tmp_path):
    path = tmp_path / "late-source.sqlite"
    monitor = "gallinas-delayed"
    start = datetime(2026, 9, 12, 11, tzinfo=timezone.utc)
    command.main(
        [
            "init",
            "--db",
            str(path),
            "--monitor",
            monitor,
            "--case-id",
            "GALLINAS-DELAYED-CURRENT",
            "--start",
            start.isoformat(),
        ]
    )
    store = WatchStore(path)
    clock = [start + timedelta(hours=1)]
    publication = start + timedelta(hours=1, minutes=5)
    observed = start + timedelta(minutes=10)

    def source(url, *args):
        window = parse_qs(urlsplit(url).query)["datetime"][0].split("/")
        first, last = (datetime.fromisoformat(s.replace("Z", "+00:00")) for s in window)
        features = []
        if clock[0] >= publication and first <= observed <= last:
            features.append(
                {
                    "type": "Feature",
                    "id": "late-published",
                    "properties": {
                        "monitoring_location_id": "USGS-08380500",
                        "time_series_id": "9857d8ac60994a19bc7a8ec28809066f",
                        "parameter_code": "00060",
                        "statistic_id": "00011",
                        "unit_of_measure": "ft^3/s",
                        "time": observed.isoformat(),
                        "value": "3.64",
                        "approval_status": "Provisional",
                        "qualifier": None,
                        "last_modified": publication.isoformat(),
                    },
                }
            )
        return HTTPResponse(
            200,
            json.dumps({"type": "FeatureCollection", "features": features, "links": []}).encode(),
            {},
        )

    runner = WatchRunner(store, USGSClient(transport=source), clock=lambda: clock[0])
    assert runner.tick(monitor).outcome == "NO_SOURCE_CHANGE"
    clock[0] = publication
    second = runner.tick(monitor)
    assert second.poll_result.new_observations == 1
    assert (
        store.status(monitor)["series_cursors"]["9857d8ac60994a19bc7a8ec28809066f"]
        == observed.isoformat()
    )
