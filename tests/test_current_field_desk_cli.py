"""Explicit local startup supplies field authority before any browser request."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_current_field_store import CASE, MONITOR, NOW, POLICY, SITE, contents, proposed
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.case_types import CaseConfig
from watershed_memory.current.desk_cli import configured_app, parser
from watershed_memory.current.field_codec import encode
from watershed_memory.current.locations import gallinas_location_registry


def args_for(field, tmp_path, *, site=SITE, principal="local-operator"):
    file = tmp_path / "trusted-site.json"
    file.write_text(encode(asdict(site)) + "\n", encoding="utf-8")
    values = ["--db", str(field[0]), "--case", CASE, "--field-location", str(file)]
    if principal is not None:
        values += ["--field-principal-id", principal]
    return parser().parse_args(values)


def test_explicit_field_launch_reads_and_acts_with_server_bound_identity(field, tmp_path):
    plan = proposed(field).plan
    arguments = args_for(field, tmp_path)
    before = contents(field[0])
    app = configured_app(arguments)
    assert contents(field[0]) == before
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as browser:
        view = browser.get("/api/current/case").json()
        assert "current_field_work" in view
        response = browser.post(
            "/api/current/field-responses",
            json={
                "request_id": "explicit-local-cancel",
                "expected_case_revision": view["case"]["revision"],
                "command": {
                    "operation": "DECIDE",
                    "plan_id": plan.plan_id,
                    "expected_plan_revision": plan.revision,
                    "action": "CANCEL",
                    "defer_until": None,
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["receipt"]["plan"]["recorded_by"] == "local-operator"


@pytest.mark.parametrize("which", ["principal", "locations"])
def test_field_launch_configuration_is_paired_before_any_database_write(field, tmp_path, which):
    arguments = args_for(field, tmp_path)
    setattr(
        arguments,
        "field_principal_id" if which == "principal" else "field_location",
        None if which == "principal" else [],
    )
    before = contents(field[0])
    with pytest.raises(ValueError):
        configured_app(arguments)
    assert contents(field[0]) == before
    assert not field[0].with_suffix(".replay.sqlite").exists()


@pytest.mark.parametrize(
    "which",
    [
        "wrong-case",
        "wrong-simulation",
        "station",
        "duplicate",
        "principal",
        "malformed",
        "oversized",
    ],
)
def test_invalid_trusted_configuration_has_no_launch_side_effects(field, tmp_path, which):
    site = SITE
    if which == "wrong-case":
        site = replace(SITE, case_id="OTHER")
    if which == "wrong-simulation":
        site = replace(
            SITE,
            simulated=False,
            latitude=Decimal("35"),
            longitude=Decimal("-105"),
            coordinate_system="WGS84",
            coordinate_accuracy="Synthetic test coordinates",
        )
    if which == "station":
        site = gallinas_location_registry().get("USGS-08380500", 1)
    arguments = args_for(field, tmp_path, site=site)
    if which == "duplicate":
        arguments.field_location *= 2
    if which == "principal":
        arguments.field_principal_id = "PRIVATE INVALID PRINCIPAL"
    if which == "malformed":
        arguments.field_location[0].write_text(
            '{"private_note":"PRIVATE-INVALID-SITE"}', encoding="utf-8"
        )
    if which == "oversized":
        arguments.field_location[0].write_bytes(b"x" * 131073)
    before = contents(field[0])
    with pytest.raises(ValueError) as caught:
        configured_app(arguments)
    assert "PRIVATE" not in str(caught.value)
    assert contents(field[0]) == before
    assert not field[0].with_suffix(".replay.sqlite").exists()


def test_existing_field_work_needs_explicit_trusted_configuration(field):
    proposed(field)
    before = contents(field[0])
    with pytest.raises(ValueError):
        configured_app(parser().parse_args(["--db", str(field[0]), "--case", CASE]))
    assert contents(field[0]) == before
    assert not field[0].with_suffix(".replay.sqlite").exists()


def test_readonly_configuration_can_retain_withdrawn_site_history(field, tmp_path):
    proposed(field)
    arguments = args_for(field, tmp_path, site=replace(SITE, revision=2, status="WITHDRAWN"))
    before = contents(field[0])
    with TestClient(
        configured_app(arguments), base_url="http://localhost", client=("127.0.0.1", 50000)
    ) as browser:
        view = browser.get("/api/current/case").json()
        assert view["current_field_work"]["approved_locations"] == []
    assert contents(field[0]) == before


def test_input_file_duplicate_keys_are_rejected(field, tmp_path):
    arguments = args_for(field, tmp_path)
    data = json.loads(arguments.field_location[0].read_text())
    canonical = encode(data)
    arguments.field_location[0].write_text('{"revision":1,' + canonical[1:], encoding="utf-8")
    with pytest.raises(ValueError):
        configured_app(arguments)


@pytest.mark.parametrize("kind", ["directory", "corrupt", "unrelated-database"])
def test_bad_replay_target_cannot_initialize_field_schema(ready, tmp_path, kind):
    path, cases, _, _ = ready
    cases.register(CaseConfig(CASE, MONITOR, POLICY, True), now=NOW)
    arguments = args_for((path,), tmp_path)
    replay = tmp_path / "invalid-replay"
    if kind == "directory":
        replay.mkdir()
    elif kind == "corrupt":
        replay.write_bytes(b"not a SQLite replay database")
    else:
        with sqlite3.connect(replay) as db:
            db.execute("CREATE TABLE unrelated (id INTEGER)")
    arguments.replay_db = replay
    before = path.read_bytes()
    replay_before = None if replay.is_dir() else replay.read_bytes()
    with pytest.raises((ValueError, sqlite3.Error)):
        configured_app(arguments)
    assert path.read_bytes() == before
    assert not any(name.startswith("field_") for name in contents(path))
    if replay_before is not None:
        assert replay.read_bytes() == replay_before


@pytest.mark.parametrize("terminal", [False, True])
def test_known_location_revision_cannot_be_redefined_at_launch(field, tmp_path, terminal):
    plan = proposed(field).plan
    if terminal:
        from test_current_field_store import mutate

        from watershed_memory.current.field_types import DecideFieldPlan

        mutate(field, "decide_plan", DecideFieldPlan(plan.plan_id, plan.revision, "CANCEL"))
    arguments = args_for(field, tmp_path, site=replace(SITE, label="A different physical site"))
    before = hashlib.sha256(field[0].read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        configured_app(arguments)
    assert hashlib.sha256(field[0].read_bytes()).hexdigest() == before
    assert not field[0].with_suffix(".replay.sqlite").exists()


def test_existing_replay_sessions_survive_field_desk_restart(field, tmp_path):
    from watershed_memory.service import Service

    arguments = args_for(field, tmp_path)
    replay = field[0].with_suffix(".replay.sqlite")
    service = Service(replay)
    saved = service.create_session()
    before = replay.read_bytes()
    configured_app(arguments)
    assert replay.read_bytes() == before
    assert service.snapshot(saved["session_id"]) == saved


def test_failed_replay_open_does_not_initialize_primary_field_schema(ready, tmp_path, monkeypatch):
    path, cases, _, _ = ready
    cases.register(CaseConfig(CASE, MONITOR, POLICY, True), now=NOW)
    arguments = args_for((path,), tmp_path)

    def unavailable(_):
        raise sqlite3.OperationalError("test replay open failure")

    monkeypatch.setattr("watershed_memory.current.desk_cli.Service", unavailable)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        configured_app(arguments)
    assert path.read_bytes() == before


def test_conflicting_legacy_site_history_is_rejected_even_if_that_version_is_omitted(
    field, tmp_path
):
    from test_current_field_store import mutate

    from watershed_memory.current.field_types import DecideFieldPlan

    plan = proposed(field).plan
    mutate(field, "decide_plan", DecideFieldPlan(plan.plan_id, 1, "CANCEL"))
    # Represent a database saved before the location-identity invariant was enforced.
    with sqlite3.connect(field[0]) as db:
        raw = db.execute(
            "SELECT record_json FROM field_plan_revisions WHERE plan_id=? AND revision=1",
            (plan.plan_id,),
        ).fetchone()[0]
        old = json.loads(raw)
        old["location"]["label"] = "Conflicting legacy location"
        db.execute(
            "UPDATE field_plan_revisions SET record_json=? WHERE plan_id=? AND revision=1",
            (encode(old), plan.plan_id),
        )
    arguments = args_for(field, tmp_path, site=replace(SITE, revision=2, status="WITHDRAWN"))
    before = field[0].read_bytes()
    with pytest.raises(ValueError, match="conflicting location"):
        configured_app(arguments)
    assert field[0].read_bytes() == before
    assert not field[0].with_suffix(".replay.sqlite").exists()
