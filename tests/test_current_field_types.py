"""Preregistered field command boundaries; these records grant no browser authority."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from watershed_memory.current.field_types import (
    AttachFieldEvidence,
    CorrectFieldResult,
    DecideFieldPlan,
    FieldPlanSpec,
    FieldPrincipal,
    ModifyFieldPlan,
    ProposeFieldPlan,
    ReportFieldResult,
    VerifyFieldReport,
)

NOW = datetime(2026, 9, 12, 21, tzinfo=timezone.utc)
SPEC = FieldPlanSpec(
    "VISUAL_INSPECTION",
    "Inspect the accessible sediment marker.",
    "Field coordinator",
    NOW,
    NOW + timedelta(hours=2),
    ("INSPECTION_RECORD_REFERENCE",),
)


def commands():
    return (
        ProposeFieldPlan("review-1", 1, "demo-site", 1, SPEC, True),
        DecideFieldPlan("plan-1", 1, "APPROVE"),
        ModifyFieldPlan("plan-1", 1, "demo-site", 1, SPEC, True),
        ReportFieldResult(
            "plan-1", 1, "COMPLETE", "Marker inspected and recorded.", NOW, NOW, True
        ),
        CorrectFieldResult(
            "report-1", 1, "PARTIAL", "Only the access route was inspected.", NOW, NOW, True
        ),
        AttachFieldEvidence(
            "report-1",
            1,
            "EXTERNAL_REFERENCE",
            "INSPECTION_RECORD_REFERENCE",
            "https://example.org/record/1",
            "Demonstration inspection record.",
            NOW,
            None,
            True,
        ),
        VerifyFieldReport("report-1", 1, ("evidence-1",), "Review of the inspection record.", True),
    )


def test_commands_are_frozen_and_accept_empty_private_note():
    for command in commands():
        assert command.private_note == ""
        assert not hasattr(command, "__dict__")
        with pytest.raises((FrozenInstanceError, AttributeError)):
            command.private_note = "changed"


@pytest.mark.parametrize("value", [True, 1.0, Decimal(1), "1", 0, -1, 2**63])
def test_revisions_are_exact_bounded_integers(value):
    for command in commands():
        field = next(name for name in command.__dataclass_fields__ if name.endswith("revision"))
        with pytest.raises(ValueError):
            replace(command, **{field: value})


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_simulation_is_never_coerced(value):
    for command in commands():
        if hasattr(command, "expected_simulated"):
            with pytest.raises(ValueError):
                replace(command, expected_simulated=value)


@pytest.mark.parametrize("value", ["x\nsecret", "x\u200bsecret", "x\ud800secret", "x" * 1001])
def test_private_notes_are_bounded_printable_and_error_does_not_echo(value):
    for command in commands():
        with pytest.raises(ValueError) as error:
            replace(command, private_note=value)
        assert value not in str(error.value)


@pytest.mark.parametrize(
    "roles,cases",
    [
        ([], ("case-1",)),
        (("COORDINATOR",) * 2, ("case-1",)),
        (("ADMIN",), ("case-1",)),
        (("COORDINATOR",), []),
        (("COORDINATOR",), ("case-1",) * 2),
        (("COORDINATOR",), tuple(f"case-{i}" for i in range(33))),
    ],
)
def test_principal_scope_is_explicit_immutable_and_bounded(roles, cases):
    with pytest.raises(ValueError):
        FieldPrincipal("person-1", roles, cases)


def test_principal_has_no_wildcard_scope():
    principal = FieldPrincipal("person-1", ("COORDINATOR", "VERIFIER"), ("case-1",))
    assert principal.case_ids == ("case-1",)
    with pytest.raises(ValueError):
        replace(principal, case_ids=("*",))


@pytest.mark.parametrize(
    "updates",
    [
        {"activity": "STATION_DATA_REVIEW"},
        {"purpose": "short"},
        {"purpose": "x" * 701},
        {"assignee_role": ""},
        {"window_end": NOW},
        {"window_start": NOW.replace(tzinfo=None)},
        {"required_evidence": []},
        {"required_evidence": ()},
        {"required_evidence": ("PHOTO_REFERENCE",) * 2},
        {"required_evidence": ("OTHER",)},
    ],
)
def test_plan_spec_contract(updates):
    with pytest.raises(ValueError):
        replace(SPEC, **updates)


def test_aware_times_normalize_to_utc():
    shifted = SPEC.window_start.astimezone(timezone(timedelta(hours=3)))
    result = replace(SPEC, window_start=shifted)
    assert result.window_start == NOW and result.window_start.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "updates",
    [
        {"action": "MODIFY"},
        {"action": "DEFER"},
        {"defer_until": NOW},
        {"action": "CANCEL", "defer_until": NOW},
    ],
)
def test_decision_shape(updates):
    with pytest.raises(ValueError):
        replace(commands()[1], **updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"outcome": "VERIFIED"},
        {"summary": "short"},
        {"summary": "x" * 1001},
        {"performed_start": NOW + timedelta(seconds=1)},
        {"performed_end": NOW.replace(tzinfo=None)},
    ],
)
def test_report_and_correction_contract(updates):
    for command in commands()[3:5]:
        with pytest.raises(ValueError):
            replace(command, **updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"reference_kind": "STORED_RECORD_REFERENCE"},
        {"evidence_category": "OTHER"},
        {"reference": "http://example.org/record"},
        {"reference": "https://example.org/record?key=secret"},
        {"reference": "C:/private/data"},
        {"reference": "https://@example.org/record"},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"provenance": "short"},
    ],
)
def test_evidence_attribution_reference_and_digest_contract(updates):
    with pytest.raises(ValueError):
        replace(commands()[5], **updates)


def test_opaque_operator_reference_is_explicitly_external():
    value = replace(
        commands()[5], reference_kind="OPERATOR_RECORD_REFERENCE", reference="inspection-1"
    )
    assert value.reference == "inspection-1"
    with pytest.raises(ValueError):
        replace(value, reference="https://example.org/record")


@pytest.mark.parametrize("ids", [[], (), ("b", "a"), ("a", "a"), tuple(f"e-{i}" for i in range(9))])
def test_verification_set_is_nonempty_sorted_unique_and_bounded(ids):
    with pytest.raises(ValueError):
        replace(commands()[6], evidence_ids=ids)


@pytest.mark.parametrize("identity", ["", "x" * 129, "x\n", "x\u200b", "/private/file"])
def test_verification_ids_use_the_same_identifier_contract(identity):
    with pytest.raises(ValueError):
        replace(commands()[6], evidence_ids=(identity,))
