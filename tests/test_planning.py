"""Guard evidence access and action proposals independently of the planner."""

import pytest

from watershed_memory.catalog import PACKETS
from watershed_memory.planning import GAP, MONITORING, EvidenceTools, Plan, ReplayPlanner
from watershed_memory.service import Service


def test_operator_notes_and_transport_identity_stay_outside_agent_context(tmp_path):
    service = Service(tmp_path / "private-note.sqlite")
    session = service.create_session()["session_id"]
    state = service.advance(session, "july")
    state = service.respond(session, "operator", state["tasks"][0]["id"],
                            "acknowledge", "Private operator text must stay local.")
    context = EvidenceTools(state, PACKETS[:2]).get_case_context()
    assert context["responses"][0]["action"] == "acknowledge"
    assert "note" not in context["responses"][0]
    assert "actor" not in context["responses"][0]
    assert "session_id" not in context
    context["tasks"][0]["status"] = "COMPLETED"
    assert service.snapshot(session)["tasks"][0]["status"] == "ACKNOWLEDGED"
    assert service.snapshot(session)["responses"][0]["note"].startswith("Private operator")


def test_tools_require_read_context_and_released_evidence(tmp_path):
    state = Service(tmp_path / "case.sqlite").create_session()
    tools = EvidenceTools(state, PACKETS[:1])
    with pytest.raises(ValueError):
        tools.get_observations(PACKETS[1]["event_id"])
    with pytest.raises(ValueError):
        tools.propose_review(MONITORING, PACKETS[0]["event_id"], "A new review is warranted.")
    tools.get_case_context()
    tools.get_observations(PACKETS[0]["event_id"])
    with pytest.raises(ValueError):
        tools.propose_review(GAP, PACKETS[0]["event_id"], "Invent a gap despite available data.")
    tools.propose_review(MONITORING, PACKETS[0]["event_id"], "Review the available P2 observations.")
    assert tools.finish().proposals[0]["existing_task_id"] is None


def test_invalid_planner_cannot_bypass_commit_boundary(tmp_path):
    class InvalidPlanner:
        mode = {"id": "test", "label": "test", "agent_enabled": False}

        def plan(self, state, released):
            return Plan([{"kind": GAP, "event_id": released[-1]["event_id"],
                          "existing_task_id": None, "reason": "An invented evidence gap."}], [])

    service = Service(tmp_path / "case.sqlite", planner=InvalidPlanner())
    state = service.create_session()
    with pytest.raises(ValueError):
        service.advance(state["session_id"], "invalid")
    assert service.snapshot(state["session_id"]) == state


def test_failure_after_staging_leaves_no_partial_writes(tmp_path):
    class FailingPlanner:
        mode = {"id": "test", "label": "test", "agent_enabled": False}

        def plan(self, state, released):
            tools = EvidenceTools(state, released)
            tools.get_case_context()
            tools.get_observations(released[-1]["event_id"])
            tools.propose_review(MONITORING, released[-1]["event_id"], "Review this source evidence.")
            raise RuntimeError("Simulated provider failure after tool use")

    service = Service(tmp_path / "case.sqlite", planner=FailingPlanner())
    state = service.create_session()
    with pytest.raises(RuntimeError):
        service.advance(state["session_id"], "failed")
    assert service.snapshot(state["session_id"]) == state


def test_planner_direct_mutation_and_missing_proposals_cannot_commit(tmp_path):
    class MutatingPlanner:
        mode = {"id": "test", "label": "test", "agent_enabled": False}

        def plan(self, state, released):
            state["case"]["water_safety"] = "SAFE"
            released[0]["p1_turbidity_count"] = 0
            return Plan([], [])

    service = Service(tmp_path / "case.sqlite", planner=MutatingPlanner())
    state = service.create_session()
    with pytest.raises(ValueError):
        service.advance(state["session_id"], "mutating")
    assert service.snapshot(state["session_id"]) == state
    assert PACKETS[0]["p1_turbidity_count"] > 0


def test_forged_trace_is_rejected_even_with_valid_proposals(tmp_path):
    class ForgedTrace(ReplayPlanner):
        def plan(self, state, released):
            plan = super().plan(state, released)
            plan.trace[0]["output"]["case"]["water_safety"] = "SAFE"
            return plan

    service = Service(tmp_path / "case.sqlite", planner=ForgedTrace())
    before = service.create_session()
    with pytest.raises(ValueError, match="recorded tool result"):
        service.advance(before["session_id"], "forged")
    assert service.snapshot(before["session_id"]) == before
