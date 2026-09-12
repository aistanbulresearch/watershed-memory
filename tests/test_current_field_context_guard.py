"""Legacy agent paths must not silently omit newly recorded field work."""

from dataclasses import replace

import pytest
from test_current_case_store import CASE as OTHER_CASE
from test_current_field_store import CASE, NOW, contents, proposed
from test_current_field_store import field as field
from test_current_field_store import ready as ready

from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.context import load_context
from watershed_memory.current.context_reference import (
    capture_context,
    encode_reference,
    restore_context,
)
from watershed_memory.current.delivery_store import DeliveryStore
from watershed_memory.current.delivery_types import InvocationAllowance, InvocationProfile

PROFILE = InvocationProfile(
    "SCRIPTED_SDK", "scripted-field-boundary", "watershed-current-v1", "fixture"
)


def event(field):
    with field[1]._connect() as db:
        return db.execute(
            "SELECT event_id FROM watch_events ORDER BY interval_end DESC LIMIT 1"
        ).fetchone()[0]


@pytest.mark.parametrize(
    "version,health", [("watershed-current-v1", False), ("watershed-current-v2", True)]
)
def test_field_record_blocks_legacy_reservation_before_allowance_or_source_writes(
    field, version, health
):
    proposed(field)
    journal = DeliveryStore(field[1], allowance=InvocationAllowance(3, 0))
    before = contents(field[0])
    with pytest.raises(WorkflowConflict, match="field work requires context contract v3"):
        journal.reserve(
            CASE,
            event(field),
            request_id="blocked-legacy",
            profile=replace(PROFILE, instruction_version=version),
            source_delivery=False,
            now=NOW,
            include_source_health=health,
        )
    assert contents(field[0]) == before
    assert journal.allowance().scripted_used == 0


def test_empty_extension_keeps_legacy_context_unchanged_and_other_case_is_isolated(field):
    original = load_context(field[1], CASE, event(field), evaluated_at=NOW)
    reference = capture_context(original)
    encoded = encode_reference(reference)
    proposed(field)
    before = contents(field[0])
    with pytest.raises(WorkflowConflict):
        load_context(field[1], CASE, event(field), evaluated_at=NOW)
    with field[1]._connect() as db:
        db.execute("BEGIN")
        assert restore_context(db, reference) == original
    assert encode_reference(reference) == encoded
    assert load_context(field[1], OTHER_CASE, event(field), evaluated_at=NOW).case_id == OTHER_CASE
    assert contents(field[0]) == before
