"""A remote-capable planner keeps the existing dispatch and authority boundary."""

from dataclasses import replace

import pytest
from test_current_field_dispatch_runner import AT  # noqa: F401
from test_current_field_dispatch_runner import sdk_field as sdk_field
from test_current_field_sdk_budget import BatchModel
from test_current_field_store import CASE, contents
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current.field_dispatch_runner import FieldDispatchRunner
from watershed_memory.current.field_planner import FieldPlannerErrorV3, FieldPlannerV3
from watershed_memory.current.field_strands import CurrentStrandsPlannerV3, CurrentTurnErrorV3


class BridgePlanner(FieldPlannerV3):
    """A typed test adapter delegates to the actual local SDK without network I/O."""

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.model_id = wrapped.model_id
        self.scripted_test = wrapped.scripted_test
        self.calls = 0
        self.profile_override = None

    @property
    def profile(self):
        return self.profile_override or replace(
            self.wrapped.profile,
            model_id=self.model_id,
            mode="SCRIPTED_SDK" if self.scripted_test else "STRANDS_CURRENT",
        )

    def plan(self, context):
        self.calls += 1
        return self.wrapped.plan(context)


def test_contract_is_abstract_and_local_planner_explicitly_implements_it():
    with pytest.raises(TypeError):
        FieldPlannerV3()
    assert issubclass(CurrentStrandsPlannerV3, FieldPlannerV3)


def test_typed_adapter_commits_actual_sdk_result_and_quiet_check_does_not_repeat(sdk_field):
    _, store, local = sdk_field
    bridge = BridgePlanner(local)
    runner = FieldDispatchRunner(store, bridge, clock=lambda: AT)
    result = runner.tick(CASE)
    assert result.outcome == "COMMITTED"
    assert result.receipt.field_plan.plan.status == "PROPOSED"
    assert result.receipt.field_plan.plan.author_kind == "AGENT"
    assert bridge.calls == 1 and local.model.response_count == 6
    assert store.get(CASE, result.receipt.delivery.request_id) == result.receipt
    assert runner.tick(CASE).outcome == "QUIET"
    assert bridge.calls == 1


def test_typed_adapter_preserves_recorded_failure_without_reinvocation(sdk_field):
    _, store, local = sdk_field
    local.model = BatchModel([])
    bridge = BridgePlanner(local)
    runner = FieldDispatchRunner(store, bridge, clock=lambda: AT)
    result = runner.tick(CASE)
    assert result.outcome == "FAILED" and result.receipt.delivery.failure_json
    assert runner.tick(CASE).outcome == "HELD"
    assert bridge.calls == 1


def test_provider_neutral_typed_failure_commits_failed_once(sdk_field):
    class RemoteFailurePlanner(BridgePlanner):
        def plan(self, context):
            self.calls += 1
            try:
                return self.wrapped.plan(context)
            except CurrentTurnErrorV3 as error:
                boundary_error = FieldPlannerErrorV3(error.failure)
                assert not isinstance(boundary_error, CurrentTurnErrorV3)
                raise boundary_error from None

    _, store, local = sdk_field
    local.model = BatchModel([])
    bridge = RemoteFailurePlanner(local)
    runner = FieldDispatchRunner(store, bridge, clock=lambda: AT)
    result = runner.tick(CASE)
    assert result.outcome == "FAILED" and result.receipt.delivery.failure_json
    assert runner.tick(CASE).outcome == "HELD" and bridge.calls == 1


@pytest.mark.parametrize("change", [
    {"sdk_version": "different-runtime-sdk"},
    {"instruction_version": "watershed-current-v2"},
    {"model_id": "different-runtime-model"},
    {"mode": "STRANDS_CURRENT"},
])
def test_entire_remote_profile_is_checked_before_reservation(sdk_field, change):
    case, store, local = sdk_field
    bridge = BridgePlanner(local)
    bridge.profile_override = replace(local.profile, **change)
    before = contents(case[0])
    with pytest.raises(ValueError):
        FieldDispatchRunner(store, bridge, clock=lambda: AT).tick(CASE)
    assert bridge.calls == 0 and contents(case[0]) == before


def test_failure_contract_requires_exact_typed_failure():
    with pytest.raises(ValueError):
        FieldPlannerErrorV3({"failure": "PRIVATE-CANARY"})


def test_adapter_transport_uncertainty_retains_reserved_attempt(sdk_field):
    class UncertainPlanner(BridgePlanner):
        def plan(self, context):
            self.calls += 1
            raise RuntimeError("PRIVATE-TRANSPORT-CANARY")

    _, store, local = sdk_field
    bridge = UncertainPlanner(local)
    runner = FieldDispatchRunner(store, bridge, clock=lambda: AT)
    result = runner.tick(CASE)
    assert result.outcome == "OUTCOME_UNKNOWN" and result.attempt_id
    assert result.receipt is None and "PRIVATE-TRANSPORT-CANARY" not in repr(result)
    assert store.status(CASE).active_status == "RESERVED"
    assert runner.tick(CASE).outcome == "HELD"
    assert bridge.calls == 1


@pytest.mark.parametrize("attribute,value", [("model_id", "different-model"), ("scripted_test", False)])
def test_adapter_profile_mismatch_fails_before_reservation(sdk_field, attribute, value):
    case, store, local = sdk_field
    bridge = BridgePlanner(local)
    setattr(bridge, attribute, value)
    before = contents(case[0])
    with pytest.raises(ValueError):
        FieldDispatchRunner(store, bridge, clock=lambda: AT).tick(CASE)
    assert bridge.calls == 0 and contents(case[0]) == before


@pytest.mark.parametrize("attribute,value", [
    ("model_id", None), ("model_id", ""), ("model_id", 1),
    ("scripted_test", 0), ("scripted_test", 1), ("scripted_test", "false"),
    ("plan", None),
])
def test_invalid_adapter_contract_fails_before_any_state_change(sdk_field, attribute, value):
    case, store, local = sdk_field
    bridge = BridgePlanner(local)
    setattr(bridge, attribute, value)
    before = contents(case[0])
    with pytest.raises(ValueError):
        FieldDispatchRunner(store, bridge, clock=lambda: AT)
    assert contents(case[0]) == before


def test_duck_typed_object_is_not_an_explicit_planner(sdk_field):
    class Impostor:
        model_id = "field-result-scripted-fixture"
        scripted_test = True

        def plan(self, context):
            raise AssertionError("an unregistered planner must not execute")

    case, store, _ = sdk_field
    before = contents(case[0])
    with pytest.raises(ValueError):
        FieldDispatchRunner(store, Impostor(), clock=lambda: AT)
    assert contents(case[0]) == before


def test_missing_adapter_metadata_is_a_validation_error(sdk_field):
    class IncompletePlanner(FieldPlannerV3):
        @property
        def profile(self):
            return None

        def plan(self, context):
            raise AssertionError("metadata must be validated first")

    case, store, _ = sdk_field
    before = contents(case[0])
    with pytest.raises(ValueError):
        FieldDispatchRunner(store, IncompletePlanner(), clock=lambda: AT)
    assert contents(case[0]) == before
