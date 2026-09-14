"""One read snapshot joins current work and exact historical field assessments."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_current_case_store import CONFIG
from test_current_dispatch_store import next_interval, outcome
from test_current_field_dispatch_runner import AT, runner
from test_current_field_dispatch_runner import sdk_field as sdk_field
from test_current_field_dispatch_store import (
    CASE as SOURCE_CASE,
)
from test_current_field_dispatch_store import (
    V3_PROFILE,
    execution,
    failed,
)
from test_current_field_dispatch_store import field_dispatch as field_dispatch
from test_current_field_store import CASE, COORDINATOR, NOW, SITE, contents, proposed, reported
from test_current_field_store import field as field
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.case_store import CaseStore
from watershed_memory.current.case_types import WorkflowConflict
from watershed_memory.current.desk import CurrentDesk
from watershed_memory.current.field_store import FieldStore
from watershed_memory.current.field_types import CorrectFieldResult, DecideFieldPlan, FieldPrincipal
from watershed_memory.current.locations import LocationRegistry
from watershed_memory.watch.store import MonitorConfig


def desk(field, principal=COORDINATOR):
    return CurrentDesk(field[1], CASE, fields=field[2], principal=principal)


def test_field_snapshot_uses_one_read_only_connection_and_preserves_source_dimensions(
    field, monkeypatch
):
    reported(field)
    before = contents(field[0])
    opened, statements = [], []
    connect = field[1]._connect

    def readonly():
        db = connect()
        db.execute("PRAGMA query_only=ON")
        db.set_trace_callback(statements.append)
        opened.append(db)
        return db

    monkeypatch.setattr(field[1], "_connect", readonly)
    value = desk(field).snapshot(now=AT)
    assert len(opened) == 1
    assert (
        value["current_field_work"]["latest_results"][0]["result"]["report"]["outcome"]
        == "COMPLETE"
    )
    assert value["case"]["revision"] == value["current_field_work"]["case_revision"]
    dimensions = {item["id"]: item for item in value["dimensions"]}
    assert dimensions["field"]["status"] == "TRACKED"
    assert dimensions["physical"]["status"] == "NOT_ASSESSED"
    assert dimensions["coverage"]["status"] == "DEGRADED"
    assert value["dispatch"] is None and value["interval"] is not None
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"))
        for sql in statements
    )
    assert contents(field[0]) == before


def test_field_desk_can_open_without_a_source_event_or_new_schema(ready):
    _, cases, watch, _ = ready
    watch.register(MonitorConfig("empty-monitor", "USGS-08380500", "EMPTY", NOW), now=NOW)
    cases.register(
        replace(CONFIG, case_id="EMPTY", monitor_id="empty-monitor", simulated=True), now=NOW
    )
    fields = FieldStore(cases, LocationRegistry((replace(SITE, case_id="EMPTY"),)))
    before = contents(ready[0])
    value = CurrentDesk(cases, "EMPTY", fields=fields).snapshot(now=NOW)
    assert value["interval"] is None
    assert value["current_field_work"]["state"] == "EMPTY"
    assert value["current_field_work"]["approved_locations"]
    assert value["dispatch"] is None
    assert contents(ready[0]) == before


def test_optional_field_store_and_principal_are_exact_server_bindings(field):
    with pytest.raises(ValueError):
        CurrentDesk(field[1], CASE, principal=COORDINATOR)
    with pytest.raises(ValueError):
        CurrentDesk(CaseStore(field[0]), CASE, fields=field[2])
    for principal in ({}, True, FieldPrincipal("elsewhere", ("COORDINATOR",), ("OTHER",))):
        with pytest.raises(ValueError):
            desk(field, principal)
    proposed(field)
    assert (
        desk(field, None).snapshot(now=AT)["current_field_work"]["current_plans"][0][
            "available_actions"
        ]
        == []
    )


def test_legacy_desk_cannot_silently_omit_field_work(field):
    reported(field)
    before = contents(field[0])
    with pytest.raises(WorkflowConflict):
        CurrentDesk(field[1], CASE).snapshot(now=AT)
    assert contents(field[0]) == before


def test_cancelled_plan_is_inspectable_by_exact_identity_after_leaving_index(field):
    original = proposed(field).plan
    field[2].decide_plan(
        CASE,
        DecideFieldPlan(original.plan_id, original.revision, "CANCEL"),
        principal=COORDINATOR,
        request_id="cancel",
        now=AT,
        expected_case_revision=field[1].context(CASE)["revision"],
    )
    view = desk(field).field_work(original.plan_id, now=AT)
    assert view["snapshot"]["current_field_work"]["current_plans"] == []
    assert view["selected_field_work"]["plan"]["status"] == "CANCELLED"
    assert view["selected_field_work"]["available_actions"] == []
    with pytest.raises(KeyError):
        desk(field).field_work("unknown-plan", now=AT)


def test_v3_detail_restores_the_assessed_result_after_a_later_correction(sdk_field):
    field, store, _ = sdk_field
    tick = runner(sdk_field).tick(CASE)
    operator = FieldPrincipal(
        "desk-all-roles", ("COORDINATOR", "FIELD_OPERATOR", "VERIFIER"), (CASE,)
    )
    page = desk(field, operator)
    original = page.assessment(tick.attempt_id)
    assert original["context_version"] == 3
    assert original["field_decision"]["disposition"] == "PROPOSE_FIELD_PLAN"
    assert original["field_proposal"]["status"] == "PROPOSED"
    saved = original["assessed_field_context"]["latest_results"][0]["result"]
    assert saved["report"]["outcome"] == "PARTIAL" and saved["verification_level"] == "VERIFIED"
    assert original["field_tool_names"] and original["tool_names"]
    report = saved["report"]
    command = CorrectFieldResult(
        report["report_id"],
        report["revision"],
        "COMPLETE",
        "Corrected after the recorded assessment",
        NOW + timedelta(minutes=5),
        NOW + timedelta(minutes=10),
        True,
    )
    field[2].correct_result(
        CASE,
        command,
        principal=operator,
        request_id="after-assessment",
        expected_case_revision=field[1].context(CASE)["revision"],
        now=AT + timedelta(minutes=1),
    )
    before = contents(field[0])
    current = page.snapshot(now=AT + timedelta(minutes=1))
    assert current["dispatch"]["assessment_count"] == 1
    assert (
        current["current_field_work"]["latest_results"][0]["result"]["report"]["outcome"]
        == "COMPLETE"
    )
    assert page.assessment(tick.attempt_id) == original
    assert contents(field[0]) == before
    assert not any(
        key in json.dumps(original)
        for key in ("input_json", "output_json", "usage_json", "private_note", str(field[0]))
    )
    assert store.journal.allowance().provider_used == 0


def test_reserved_v3_attempt_is_visible_without_model_calls(sdk_field):
    field, store, _ = sdk_field
    reserved = store.prepare(CASE, now=AT).reservation
    before = contents(field[0])
    page = desk(field)
    assert page.snapshot(now=AT)["dispatch"]["active_status"] == "RESERVED"
    value = page.assessment(reserved.attempt_id)
    assert value["field_decision"] is None and value["field_proposal"] is None
    assert value["field_tool_names"] == value["tool_names"] == []
    assert value["assessed_field_context"]["latest_results"]
    assert contents(field[0]) == before


@pytest.mark.parametrize("mutation", ["missing_pin", "profile", "held_pointer", "extra_schema"])
def test_malformed_v3_state_fails_closed_without_read_side_effects(sdk_field, mutation):
    field, store, _ = sdk_field
    ticket = store.prepare(CASE, now=AT).reservation
    with field[1]._connect() as db:
        if mutation == "missing_pin":
            db.execute("DELETE FROM context3_attempts WHERE attempt_id=?", (ticket.attempt_id,))
        elif mutation == "profile":
            db.execute(
                "UPDATE delivery_attempts SET profile_json=replace(profile_json,'watershed-current-v3','watershed-current-v2')"
            )
        elif mutation == "held_pointer":
            db.execute("UPDATE dispatch_settings SET active_attempt_id=NULL")
        else:
            db.execute("CREATE TABLE context3_unknown (value TEXT)")
    before = contents(field[0])
    with pytest.raises((ValueError, WorkflowConflict)):
        desk(field).snapshot(now=AT)
    assert contents(field[0]) == before


def test_v3_assessment_remains_case_bound(sdk_field):
    field, store, _ = sdk_field
    ticket = store.prepare(CASE, now=AT).reservation
    field[1].register(replace(CONFIG, case_id="OTHER", simulated=True), now=AT)
    other = CurrentDesk(field[1], "OTHER", fields=field[2])
    with pytest.raises(KeyError):
        other.assessment(ticket.attempt_id)


def test_mixed_legacy_and_v3_history_keeps_each_saved_contract(ready, field_dispatch):
    old = field_dispatch.dispatch.prepare(SOURCE_CASE, now=NOW).reservation
    field_dispatch.dispatch.finish(old.attempt_id, outcome(old), now=NOW)
    field_dispatch.upgrade_to_v3(SOURCE_CASE, V3_PROFILE, now=NOW)
    at = next_interval(ready, 45, 50)
    new = field_dispatch.prepare(SOURCE_CASE, now=at).reservation
    field_dispatch.finish(new.attempt_id, execution(new), now=at)
    page = CurrentDesk(ready[1], SOURCE_CASE, fields=field_dispatch.fields)
    before = contents(ready[0])
    current = page.snapshot(now=at)
    assert current["dispatch"]["assessment_count"] == 2
    assert {item["context_version"] for item in current["assessments"]} == {2, 3}
    old_detail = page.assessment(old.attempt_id)
    assert old_detail["context_version"] == 2 and old_detail["assessed_field_context"] is None
    assert old_detail["field_decision"] is None and old_detail["field_tool_names"] == []
    assert page.assessment(new.attempt_id)["context_version"] == 3
    assert contents(ready[0]) == before


def test_failed_v3_assessment_has_safe_saved_detail(ready, field_dispatch):
    field_dispatch.upgrade_to_v3(SOURCE_CASE, V3_PROFILE, now=NOW)
    ticket = field_dispatch.prepare(SOURCE_CASE, now=NOW).reservation
    field_dispatch.fail(ticket.attempt_id, failed(ticket), now=NOW)
    page = CurrentDesk(ready[1], SOURCE_CASE, fields=field_dispatch.fields)
    before = contents(ready[0])
    assert page.snapshot(now=NOW)["dispatch"]["active_status"] == "FAILED"
    detail = page.assessment(ticket.attempt_id)
    assert detail["status"] == "FAILED" and detail["field_decision"] is None
    assert detail["assessed_field_context"]["state"] == "EMPTY"
    assert contents(ready[0]) == before


def test_future_reserved_assessment_cannot_enter_an_earlier_snapshot(sdk_field):
    field, store, _ = sdk_field
    store.prepare(CASE, now=AT)
    before = contents(field[0])
    with pytest.raises(ValueError):
        desk(field).snapshot(now=AT - timedelta(seconds=1))
    assert contents(field[0]) == before


def test_partial_field_namespace_is_not_repaired_on_read(field):
    page = desk(field)
    with field[1]._connect() as db:
        db.execute("CREATE TABLE field_unknown (value TEXT)")
    before = contents(field[0])
    with pytest.raises(ValueError):
        page.snapshot(now=AT)
    assert contents(field[0]) == before


def test_pre_dispatch_legacy_receipt_is_inspectable_after_field_initialization(ready):
    from test_current_delivery_store import PROFILE
    from test_current_delivery_store import execution as source_execution

    from watershed_memory.current.delivery_store import DeliveryStore
    from watershed_memory.current.delivery_types import InvocationAllowance

    journal = DeliveryStore(ready[1], allowance=InvocationAllowance(0, 1))
    ticket = journal.reserve(
        SOURCE_CASE,
        ready[3][-1].event_id,
        profile=PROFILE,
        source_delivery=True,
        request_id="manual-legacy",
        now=NOW,
    )
    journal.commit(ticket.attempt_id, source_execution(ticket), now=NOW)
    fields = FieldStore(ready[1], LocationRegistry((replace(SITE, case_id=SOURCE_CASE),)))
    page = CurrentDesk(ready[1], SOURCE_CASE, fields=fields)
    before = contents(ready[0])
    assert page.snapshot(now=NOW)["dispatch"] is None
    assert page.assessment(ticket.attempt_id)["context_version"] == 1
    assert contents(ready[0]) == before


@pytest.mark.parametrize("transaction,readonly", [(False, False), (True, False), (False, True)])
def test_history_read_requires_an_open_read_only_transaction(field, transaction, readonly):
    from watershed_memory.current.field_desk_history import read_settings

    with field[1]._connect() as db:
        if readonly:
            db.execute("PRAGMA query_only=ON")
        if transaction:
            db.execute("BEGIN")
        with pytest.raises(ValueError, match="query-only"):
            read_settings(db, field[2], CASE)
