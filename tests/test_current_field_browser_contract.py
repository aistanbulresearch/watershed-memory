"""The real HTTP field journey must be accepted by the shipped browser modules."""

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_current_field_desk import desk
from test_current_field_desk_responses import ALL
from test_current_field_store import NOW, SITE
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

import watershed_memory
from watershed_memory.api import create_app
from watershed_memory.current import http
from watershed_memory.service import Service


def test_http_field_lifecycle_matches_browser_confirmation(field, tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Python/browser contract bridge")
    moment = [NOW]
    monkeypatch.setattr(http, "_now", lambda: moment[0])
    page = desk(field, ALL)
    app = create_app(Service(tmp_path / "replay.sqlite"), current_desk=page)
    exchanges = []
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as browser:

        def send(command, minute):
            moment[0] = NOW + timedelta(minutes=minute)
            state = browser.get("/api/current/case")
            assert state.status_code == 200
            body = {
                "request_id": f"bridge-{len(exchanges)}",
                "expected_case_revision": state.json()["case"]["revision"],
                "command": command,
            }
            response = browser.post("/api/current/field-responses", json=body)
            assert response.status_code == 200, response.text
            exchanges.append({"body": body, "response": response.json()})
            return response.json()["selected_field_work"]

        spec = {
            "activity": "VISUAL_INSPECTION",
            "purpose": "Inspect the approved sediment marker.",
            "assignee_role": "Source-water team",
            "window_start": (NOW + timedelta(hours=1)).isoformat(),
            "window_end": (NOW + timedelta(hours=2)).isoformat(),
            "required_evidence": ["INSPECTION_RECORD_REFERENCE"],
        }
        propose = {
            "operation": "PROPOSE",
            "task_id": field[3].task_id,
            "expected_review_revision": field[3].revision,
            "location_id": SITE.location_id,
            "location_revision": SITE.revision,
            "spec": spec,
        }
        current = send(propose, 0)
        current = send(
            {
                "operation": "MODIFY",
                "plan_id": current["plan"]["plan_id"],
                "expected_plan_revision": current["plan"]["revision"],
                "location_id": SITE.location_id,
                "location_revision": SITE.revision,
                "spec": spec,
            },
            1,
        )
        for action, minute in (("DEFER", 2), ("APPROVE", 3)):
            current = send(
                {
                    "operation": "DECIDE",
                    "plan_id": current["plan"]["plan_id"],
                    "expected_plan_revision": current["plan"]["revision"],
                    "action": action,
                    "defer_until": (NOW + timedelta(minutes=30)).isoformat()
                    if action == "DEFER"
                    else None,
                },
                minute,
            )
        times = {
            "performed_start": (NOW + timedelta(minutes=4)).isoformat(),
            "performed_end": (NOW + timedelta(minutes=5)).isoformat(),
        }
        current = send(
            {
                "operation": "REPORT",
                "plan_id": current["plan"]["plan_id"],
                "performed_plan_revision": current["plan"]["revision"],
                "outcome": "COMPLETE",
                "summary": "Inspection completed and recorded.",
                **times,
            },
            6,
        )
        report_id = current["result"]["report"]["report_id"]
        current = send(
            {
                "operation": "ATTACH",
                "report_id": report_id,
                "expected_report_revision": 1,
                "reference_kind": "OPERATOR_RECORD_REFERENCE",
                "evidence_category": "INSPECTION_RECORD_REFERENCE",
                "reference": "inspection-record-1",
                "provenance": "Operator-maintained demonstration record.",
                "observed_at": times["performed_end"],
                "sha256": None,
            },
            7,
        )
        send(
            {
                "operation": "VERIFY",
                "report_id": report_id,
                "expected_report_revision": 1,
                "evidence_ids": [e["evidence_id"] for e in current["result"]["evidence"]],
                "scope": "Reviewed the exact inspection record.",
            },
            8,
        )
        verify_body = exchanges[-1]["body"]
        send(
            {
                "operation": "CORRECT",
                "report_id": report_id,
                "expected_report_revision": 1,
                "outcome": "PARTIAL",
                "summary": "The second inspection point is incomplete.",
                **times,
            },
            9,
        )
        current = send(propose, 10)
        send(
            {
                "operation": "DECIDE",
                "plan_id": current["plan"]["plan_id"],
                "expected_plan_revision": current["plan"]["revision"],
                "action": "CANCEL",
                "defer_until": None,
            },
            11,
        )
        response = browser.post("/api/current/field-responses", json=verify_body)
        assert response.status_code == 200
        assert response.json()["receipt"]["result"]["report"]["outcome"] == "COMPLETE"
        assert response.json()["selected_field_work"]["result"]["report"]["outcome"] == "PARTIAL"
        exchanges.append({"body": verify_body, "response": response.json()})

    payload = {
        "staticDirectory": str(Path(watershed_memory.__file__).parent / "static"),
        "exchanges": exchanges,
    }
    checked = subprocess.run(
        [node, str(Path(__file__).with_name("field-api-check.mjs"))],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout) == {"validated": 11}
