"""Offline gate orchestration uses an explicitly scripted SDK transport."""

import json
from types import SimpleNamespace

import pytest
from test_agentcore import ACCOUNT, ARN, MODEL, Runtime, adapter

from feasibility.run_agentcore import run_gate


def args(tmp_path):
    return SimpleNamespace(profile=None, expected_account=ACCOUNT, region="us-east-1",
        model_id=MODEL, runtime_arn=ARN, runtime_endpoint="proof_v1", runtime_version="1",
        output_root=tmp_path)


def execute(tmp_path, runtime):
    return run_gate(args(tmp_path), lambda *_a, **_kw: adapter(runtime, scripted=True),
                    account_lookup=lambda *_a: ACCOUNT)


def test_gate_checks_two_sessions_receipts_acknowledgment_and_fresh_process(tmp_path):
    runtime = Runtime()
    root, result = execute(tmp_path, runtime)
    assert result["passed"] and result["planner_attempts"] == 2
    assert result["execution"] == "SCRIPTED_PROTOCOL_TEST"
    assert all(result["final_checks"].values())
    assert all(stage["passed"] for stage in result["stages"].values())
    assert len(runtime.calls) == 2 and len(runtime.stops) == 2
    assert len({call["runtimeSessionId"] for call in runtime.calls}) == 2
    assert json.loads((root / "manifest.json").read_text())["execution"] == "SCRIPTED_PROTOCOL_TEST"
    assert (root / "before-august.json").exists() and (root / "before-september.json").exists()
    before = json.loads((root / "before-september.json").read_text())
    assert len(before["responses"]) == 1 and before["responses"][0]["simulated"] is True


def test_first_failure_is_saved_and_prevents_second_paid_turn(tmp_path):
    runtime = Runtime("invoke_error")
    root, result = execute(tmp_path, runtime)
    assert not result["passed"] and len(runtime.calls) == 1
    failure = json.loads((root / "failure-august.json").read_text())
    assert failure["state_unchanged"] and failure["execution"]["invocation_attempted"]
    assert "september" not in result["stages"]
    assert not (root / "before-september.json").exists()


def test_second_failure_preserves_first_turn_and_compares_against_its_own_before_state(tmp_path):
    runtime = Runtime()
    invoke = runtime.invoke_agent_runtime

    def fail_second(**kwargs):
        if len(runtime.calls) == 1:
            runtime.fault = "invoke_error"
        return invoke(**kwargs)

    runtime.invoke_agent_runtime = fail_second
    root, result = execute(tmp_path, runtime)
    assert not result["passed"] and len(runtime.calls) == 2
    assert result["stages"]["august"]["passed"]
    assert result["stages"]["september"]["state_unchanged"]
    assert (root / "after-august.json").exists() and (root / "operator-response.json").exists()
    assert not (root / "after-september.json").exists()


def test_wrong_account_never_constructs_a_runtime_client(tmp_path):
    def forbidden(*_a, **_kw):
        raise AssertionError("Wrong account must stop before Runtime client creation.")

    with pytest.raises(ValueError, match="account"):
        run_gate(args(tmp_path), forbidden, account_lookup=lambda *_a: "999999999999")
    assert list(tmp_path.glob("agentcore-*")) == []


def test_second_client_setup_failure_is_recorded_without_second_invocation(tmp_path):
    runtime = Runtime()
    factories = []

    def factory(*_a, **_kw):
        factories.append(True)
        if len(factories) == 2:
            raise ValueError("Client configuration failed.")
        return adapter(runtime, scripted=True)

    root, result = run_gate(args(tmp_path), factory, account_lookup=lambda *_a: ACCOUNT)
    assert not result["passed"] and len(runtime.calls) == 1
    assert result["stages"]["september"]["state_unchanged"]
    assert (root / "failure-september.json").exists()


def test_second_client_cannot_change_the_labelled_execution_mode(tmp_path):
    runtime = Runtime()
    calls = []

    def factory(*_a, **_kw):
        calls.append(True)
        client = adapter(runtime, scripted=True)
        if len(calls) == 2:
            client.mode = {**client.mode, "agent_enabled": True}
        return client

    root, result = run_gate(args(tmp_path), factory, account_lookup=lambda *_a: ACCOUNT)
    assert not result["passed"] and len(runtime.calls) == 1
    assert result["stages"]["september"]["state_unchanged"]
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["execution"] == "SCRIPTED_PROTOCOL_TEST"
    assert all(f"watershed_memory/{name}.py" in manifest["source_sha256"]
               for name in ("budget", "cli", "catalog"))
