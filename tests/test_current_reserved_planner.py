"""Inference receives the exact durable reservation without owning its database."""

from dataclasses import replace

import pytest
from test_current_field_dispatch_runner import AT
from test_current_field_dispatch_runner import sdk_field as sdk_field
from test_current_field_planner_boundary import BridgePlanner
from test_current_field_store import CASE, contents
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.field_dispatch_runner import FieldDispatchRunner


def test_default_reserved_path_runs_local_sdk_without_committing(sdk_field):
    case, store, local = sdk_field
    bridge = BridgePlanner(local)
    reservation = store.prepare(CASE, now=AT).reservation
    before = contents(case[0])
    execution = bridge.plan_reserved(reservation)
    assert execution.assessment.field.disposition == "PROPOSE_FIELD_PLAN"
    assert bridge.calls == 1 and local.model.response_count == 6
    assert contents(case[0]) == before
    assert store.status(CASE).active_status == "RESERVED"


def test_runner_passes_the_actual_saved_reservation_to_typed_hook(sdk_field, monkeypatch):
    class CapturingPlanner(BridgePlanner):
        def plan(self, context):
            raise AssertionError("reservation identity must reach the adapter")

        def plan_reserved(self, reservation):
            self.received = reservation
            self.calls += 1
            return self.wrapped.plan(reservation.context)

    _, store, local = sdk_field
    bridge = CapturingPlanner(local)
    original = store.prepare
    captured = []

    def capture(*args, **kwargs):
        step = original(*args, **kwargs)
        captured.append(step.reservation)
        return step

    monkeypatch.setattr(store, "prepare", capture)
    result = FieldDispatchRunner(store, bridge, clock=lambda: AT).tick(CASE)
    assert result.outcome == "COMMITTED"
    saved = captured[0]
    assert bridge.received is saved
    assert saved.attempt_id == result.attempt_id == result.receipt.delivery.attempt_id
    assert saved.request_id == result.receipt.delivery.request_id
    assert saved.source_delivery is result.receipt.delivery.source_delivery
    assert saved.profile == result.receipt.delivery.profile == local.profile
    assert saved.reserved_at == saved.context.base.evaluated_at == AT
    assert bridge.calls == 1


@pytest.mark.parametrize("invalid", [None, {}, object()])
def test_reserved_path_refuses_non_reservations_before_sdk(sdk_field, invalid):
    case, _, local = sdk_field
    bridge = BridgePlanner(local)
    before = contents(case[0])
    with pytest.raises(ValueError):
        bridge.plan_reserved(invalid)
    assert bridge.calls == 0 and local.model.response_count == 0
    assert contents(case[0]) == before


@pytest.mark.parametrize("change", [
    {"model_id": "another-runtime-model"},
    {"sdk_version": "another-runtime-sdk"},
    {"mode": "STRANDS_CURRENT"},
])
def test_reserved_path_refuses_full_profile_mismatch_before_sdk(sdk_field, change):
    case, store, local = sdk_field
    bridge = BridgePlanner(local)
    reservation = store.prepare(CASE, now=AT).reservation
    changed = replace(reservation, profile=replace(reservation.profile, **change))
    before = contents(case[0])
    with pytest.raises(ValueError):
        bridge.plan_reserved(changed)
    assert bridge.calls == 0 and local.model.response_count == 0
    assert contents(case[0]) == before


def test_profile_change_after_reservation_holds_attempt_without_inference(sdk_field, monkeypatch):
    _, store, local = sdk_field
    bridge = BridgePlanner(local)
    runner = FieldDispatchRunner(store, bridge, clock=lambda: AT)
    original = store.prepare

    def change_profile(*args, **kwargs):
        step = original(*args, **kwargs)
        bridge.profile_override = replace(local.profile, sdk_version="changed-after-reservation")
        return step

    monkeypatch.setattr(store, "prepare", change_profile)
    result = runner.tick(CASE)
    assert result.outcome == "OUTCOME_UNKNOWN" and result.receipt is None
    assert store.status(CASE).active_status == "RESERVED"
    assert bridge.calls == 0 and local.model.response_count == 0
    bridge.profile_override = None
    assert runner.tick(CASE).outcome == "HELD"
    assert bridge.calls == 0
