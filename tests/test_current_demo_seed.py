import json
import os
import shlex
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.demo_seed import CASE_ID, main, seed
from watershed_memory.current.field_codec import decode, encode
from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.field_types import (
    AttachFieldEvidence,
    CorrectFieldResult,
    FieldPrincipal,
)
from watershed_memory.current.locations import LocationEntry, LocationRegistry


def test_offline_seed_creates_simulated_partial_verified_case(tmp_path):
    case_id, database = seed(tmp_path / "demo")
    assert case_id == CASE_ID
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT simulated FROM current_cases WHERE case_id=?", (CASE_ID,)
        ).fetchone() == (1,)
        assert db.execute("SELECT status FROM field_plans").fetchone() == ("REPORTED",)
        assert db.execute("SELECT outcome FROM field_reports").fetchone() == ("PARTIAL",)
        assert db.execute("SELECT count(*) FROM field_evidence").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM field_verifications").fetchone() == (1,)
        actual = {row[0] for row in db.execute("SELECT semantic_hash FROM observation_versions")}
    fixture = json.loads(Path("watershed_memory/data/current-demo-observations.json").read_text())
    assert actual == set(fixture["semantic_hashes"])
    site = json.loads((tmp_path / "demo" / "approved-site.json").read_text())
    assert site["simulated"] is True
    assert site["case_id"] == CASE_ID


def test_offline_seed_refuses_existing_output(tmp_path):
    output = tmp_path / "demo"
    seed(output)
    with pytest.raises(FileExistsError):
        seed(output)


def test_offline_seed_does_not_use_network(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network must not be used")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    seed(tmp_path / "demo")


def test_seeded_case_accepts_immediate_correction_and_evidence(tmp_path):
    output = tmp_path / "demo"
    _, database = seed(output)
    case = CaseStore(database)
    site = decode(encode(json.loads((output / "approved-site.json").read_text())), LocationEntry)
    fields = FieldStore(case, LocationRegistry((site,)))
    with sqlite3.connect(database) as db:
        report_id = db.execute("SELECT report_id FROM field_reports").fetchone()[0]
    report = fields.get_result(CASE_ID, report_id)
    principal = FieldPrincipal("local-operator", ("FIELD_OPERATOR",), (CASE_ID,))
    now = datetime.now(timezone.utc)
    corrected = fields.correct_result(
        CASE_ID,
        CorrectFieldResult(
            report.report.report_id,
            report.report.revision,
            "PARTIAL",
            report.report.summary,
            report.report.performed_start,
            report.report.performed_end,
            True,
        ),
        principal=principal,
        request_id="judge-demo-test-correct",
        expected_case_revision=6,
        now=now,
    )
    attached = fields.attach_evidence(
        CASE_ID,
        AttachFieldEvidence(
            corrected.result.report.report_id,
            corrected.result.report.revision,
            "OPERATOR_RECORD_REFERENCE",
            "INSPECTION_RECORD_REFERENCE",
            "judge-demo-correction-record",
            "Demonstration correction record.",
            now,
            None,
            True,
        ),
        principal=principal,
        request_id="judge-demo-test-attach",
        expected_case_revision=7,
        now=now,
    )
    assert attached.result.evidence


def test_seed_rejects_existing_symlink_parent(tmp_path):
    link = tmp_path / "linked"
    target = tmp_path / "real"
    target.mkdir()
    (target / "ordinary").mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory links unavailable: {error}")
    with pytest.raises(ValueError, match="symlink/reparse"):
        seed(link / "demo")
    with pytest.raises(ValueError, match="symlink/reparse"):
        seed(link / "ordinary" / "demo")
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "absent", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink/reparse"):
        seed(dangling / "demo")


def test_printed_command_quotes_space_paths(tmp_path, capsys):
    main(["--output", str(tmp_path / "judge demo")])
    command = capsys.readouterr().out.split("desk_command=", 1)[1]
    assert "judge demo" in command
    if os.name == "nt":
        assert command.startswith("& '")
    else:
        tokens = shlex.split(command)
        assert tokens[1:3] == ["-m", "watershed_memory.current.desk_cli"]
        assert str(tmp_path / "judge demo" / "current-work.sqlite") in tokens


def test_seeded_database_configures_desk_and_serves_current_api(tmp_path):
    from fastapi.testclient import TestClient

    from watershed_memory.current.desk_cli import configured_app, parser

    output = tmp_path / "demo"
    _, database = seed(output)
    arguments = parser().parse_args(
        [
            "--db",
            str(database),
            "--case",
            CASE_ID,
            "--field-location",
            str(output / "approved-site.json"),
            "--field-principal-id",
            "local-operator",
        ]
    )
    with TestClient(
        configured_app(arguments), base_url="http://localhost", client=("127.0.0.1", 50000)
    ) as client:
        assert client.get("/current").status_code == 200
        response = client.get("/api/current/case")
        assert response.status_code == 200
        body = response.json()
        assert body["case"]["case_id"] == CASE_ID
        assert body["case"]["simulated"] is True
        assert body["current_field_work"] is not None
