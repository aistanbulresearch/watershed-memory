"""Browser field commands cannot choose authority or hide changed revisions."""

from dataclasses import asdict

import pytest
from pydantic import ValidationError
from test_current_field_store import NOW, SPEC

from watershed_memory.current.field_http_types import FieldResponseBody
from watershed_memory.current.field_types import (
    AttachFieldEvidence,
    CorrectFieldResult,
    DecideFieldPlan,
    ModifyFieldPlan,
    ProposeFieldPlan,
    ReportFieldResult,
    VerifyFieldReport,
)
from watershed_memory.current.tools import _plain

SPEC_BODY = _plain(SPEC)
COMMANDS = [
    (
        {
            "operation": "PROPOSE",
            "task_id": "review-1",
            "expected_review_revision": 2,
            "location_id": "site-1",
            "location_revision": 1,
            "spec": SPEC_BODY,
        },
        ProposeFieldPlan,
    ),
    (
        {
            "operation": "DECIDE",
            "plan_id": "plan-1",
            "expected_plan_revision": 3,
            "action": "APPROVE",
        },
        DecideFieldPlan,
    ),
    (
        {
            "operation": "MODIFY",
            "plan_id": "plan-1",
            "expected_plan_revision": 3,
            "location_id": "site-1",
            "location_revision": 1,
            "spec": SPEC_BODY,
        },
        ModifyFieldPlan,
    ),
    (
        {
            "operation": "REPORT",
            "plan_id": "plan-1",
            "performed_plan_revision": 3,
            "outcome": "PARTIAL",
            "summary": "One inspection point remains outstanding.",
            "performed_start": NOW.isoformat(),
            "performed_end": NOW.isoformat(),
        },
        ReportFieldResult,
    ),
    (
        {
            "operation": "CORRECT",
            "report_id": "report-1",
            "expected_report_revision": 2,
            "outcome": "COMPLETE",
            "summary": "The inspection register has been reconciled.",
            "performed_start": NOW.isoformat(),
            "performed_end": NOW.isoformat(),
        },
        CorrectFieldResult,
    ),
    (
        {
            "operation": "ATTACH",
            "report_id": "report-1",
            "expected_report_revision": 2,
            "reference_kind": "EXTERNAL_REFERENCE",
            "evidence_category": "PHOTO_REFERENCE",
            "reference": "https://example.org/inspection/17",
            "provenance": "Operator inspection reference.",
            "observed_at": None,
            "sha256": None,
        },
        AttachFieldEvidence,
    ),
    (
        {
            "operation": "VERIFY",
            "report_id": "report-1",
            "expected_report_revision": 2,
            "evidence_ids": ["evidence-1", "evidence-2"],
            "scope": "Reviewed both attached inspection records.",
        },
        VerifyFieldReport,
    ),
]


def parse(command, **overrides):
    return FieldResponseBody.model_validate(
        {
            "request_id": "desk-request-1",
            "expected_case_revision": 7,
            "command": command,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "payload,command_type", COMMANDS, ids=[row[0]["operation"] for row in COMMANDS]
)
def test_operations_preserve_revisions_and_only_server_sets_simulation(payload, command_type):
    body = parse(payload)
    command = body.command.to_command(simulated=True)
    assert type(command) is command_type and command.private_note == ""
    if hasattr(command, "expected_simulated"):
        assert command.expected_simulated is True
        assert body.command.to_command(simulated=False).expected_simulated is False
    for name, value in payload.items():
        if "revision" in name:
            assert asdict(command)[name] == value
    assert body.expected_case_revision == 7


@pytest.mark.parametrize(
    "key,value",
    [
        ("principal", "attacker"),
        ("roles", ["COORDINATOR"]),
        ("case_id", "OTHER"),
        ("expected_simulated", False),
        ("private_note", "PRIVATE-CANARY"),
    ],
)
def test_authority_and_private_note_injection_are_rejected_at_both_depths(key, value):
    command = COMMANDS[0][0]
    with pytest.raises(ValidationError):
        parse(command, **{key: value})
    with pytest.raises(ValidationError):
        parse({**command, key: value})
    with pytest.raises(ValidationError):
        parse({**command, "spec": {**SPEC_BODY, key: value}})


@pytest.mark.parametrize("revision", [True, 2.0, "2", -1, 2**63])
def test_case_revision_does_not_accept_coercion_or_invalid_bounds(revision):
    with pytest.raises(ValidationError):
        parse(COMMANDS[1][0], expected_case_revision=revision)


def test_nested_revision_and_string_fields_do_not_coerce():
    for updates in (
        {"expected_plan_revision": True},
        {"expected_plan_revision": "3"},
        {"plan_id": 123},
    ):
        with pytest.raises(ValidationError):
            parse({**COMMANDS[1][0], **updates})


def test_verification_order_is_not_silently_changed():
    with pytest.raises(ValueError):
        parse({**COMMANDS[-1][0], "evidence_ids": ["evidence-2", "evidence-1"]}).command.to_command(
            simulated=True
        )


def test_only_supported_operations_and_fields_are_admitted():
    for command in (
        {**COMMANDS[1][0], "operation": "RUN_MODEL"},
        {**COMMANDS[1][0], "action": "REPORT"},
        {**COMMANDS[1][0], "request_id": "nested-override"},
    ):
        with pytest.raises(ValidationError):
            parse(command)


def test_domain_rejects_unsafe_reference_and_invalid_defer_shape():
    for command in (
        {**COMMANDS[-2][0], "reference": "javascript:alert(1)"},
        {**COMMANDS[1][0], "action": "DEFER"},
        {**COMMANDS[1][0], "defer_until": NOW.isoformat()},
    ):
        with pytest.raises(ValueError):
            parse(command).command.to_command(simulated=True)
