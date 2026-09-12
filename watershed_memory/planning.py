"""Bounded evidence tools and transaction-ready review proposals.

Tools collect proposals; only the service can commit them. A model failure therefore
cannot leave a half-applied action. The replay planner calls the same tool boundary
and is explicitly identified as rules, not as model inference.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

MONITORING = "MONITORING_REVIEW"
GAP = "EVIDENCE_GAP_REVIEW"
TITLES = {
    MONITORING: "Review continuing watershed observations",
    GAP: "Review the missing station evidence",
}


@dataclass
class Plan:
    proposals: list[dict]
    trace: list[dict]


class Planner(Protocol):
    mode: dict

    def plan(self, state: dict, released: list[dict]) -> Plan: ...


def project_case(state: dict) -> dict:
    """Expose review state while keeping free-text operator notes in the case ledger."""
    response_fields = ("task_id", "action", "recorded_at", "simulated")
    return {
        "case": deepcopy(state["case"]),
        "tasks": deepcopy(state["tasks"]),
        "responses": [{key: deepcopy(response[key]) for key in response_fields}
                      for response in state["responses"]],
    }


class EvidenceTools:
    """A per-turn capability: no arbitrary paths, URLs, SQL or operator actions."""

    def __init__(self, state: dict, released: list[dict]):
        self._state = deepcopy(state)
        self._released = {p["event_id"]: deepcopy(p) for p in released}
        self._current = released[-1]
        self._read: set[str] = set()
        self._context_read = False
        self.proposals: list[dict] = []
        self.trace: list[dict] = []

    def _record(self, tool: str, inputs: dict, result: dict) -> dict:
        self.trace.append({"tool": tool, "input": inputs, "output": deepcopy(result)})
        return result

    def get_case_context(self) -> dict:
        """Read saved reviews, their evidence links and demonstration operator responses."""
        self._context_read = True
        return self._record("get_case_context", {}, {
            **project_case(self._state),
            "released_event_ids": list(self._released),
            "current_event_id": self._current["event_id"],
        })

    def get_observations(self, event_id: str) -> dict:
        """Read source-backed evidence for one released historical observation window."""
        if event_id not in self._released:
            raise ValueError("That observation window is not available in this replay turn.")
        self._read.add(event_id)
        return self._record("get_observations", {"event_id": event_id},
                            deepcopy(self._released[event_id]))

    def propose_review(self, kind: str, event_id: str, reason: str,
                       existing_task_id: str | None = None) -> dict:
        """Propose a permitted review; link unfinished work or create new work after completion.

        This stages an action for validation. It does not modify saved state. Kind must be
        MONITORING_REVIEW or EVIDENCE_GAP_REVIEW and evidence must be the current window.
        Supply the exact existing_task_id from saved context for unfinished work; omit it
        only when no unfinished review of this kind exists and new work is warranted.
        """
        if not self._context_read or event_id not in self._read:
            raise ValueError("Read case context and the supporting observations first.")
        if event_id != self._current["event_id"] or kind not in TITLES:
            raise ValueError("Review kind or evidence reference is not permitted.")
        if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 700:
            raise ValueError("Supply a concise reason grounded in the observations.")
        if kind == MONITORING and self._current["p2_turbidity_count"] <= 0:
            raise ValueError("A monitoring review requires available P2 observations.")
        if kind == GAP and all(self._current[f"p{s}_turbidity_count"] > 0 for s in (1, 2)):
            raise ValueError("An evidence gap requires an absent expected station in this window.")
        if any(p["kind"] == kind for p in self.proposals):
            raise ValueError("This turn already proposes that review kind.")
        unfinished = next((t for t in self._state["tasks"]
                           if t["kind"] == kind and t["status"] != "COMPLETED"), None)
        expected_target = unfinished["id"] if unfinished else None
        if existing_task_id != expected_target:
            raise ValueError("Select the matching unfinished task from context, or omit the "
                             "target only when this kind has no unfinished review.")
        proposal = {"kind": kind, "event_id": event_id, "reason": reason.strip(),
                    "existing_task_id": existing_task_id}
        self.proposals.append(proposal)
        return self._record("propose_review", {
            "kind": kind, "event_id": event_id, "reason": reason.strip(),
            "existing_task_id": existing_task_id,
        }, {"status": "STAGED", "operation": "LINK_EVIDENCE" if unfinished else "CREATE_REVIEW",
            "task_id": proposal["existing_task_id"], "committed": False})

    def finish(self) -> Plan:
        """Check the demo review policy before the service commits the complete turn."""
        expected = {MONITORING} if self._current["p2_turbidity_count"] > 0 else set()
        if any(self._current[f"p{s}_turbidity_count"] <= 0 for s in (1, 2)):
            expected.add(GAP)
        if {p["kind"] for p in self.proposals} != expected:
            raise ValueError("The turn did not address the required available evidence and coverage.")
        return Plan(deepcopy(self.proposals), deepcopy(self.trace))


class ReplayPlanner:
    mode = {"id": "historical_replay", "label": "Historical replay · rules", "agent_enabled": False}

    def plan(self, state: dict, released: list[dict]) -> Plan:
        tools = EvidenceTools(state, released)
        context = tools.get_case_context()
        packet = tools.get_observations(context["current_event_id"])
        targets = {t["kind"]: t["id"] for t in context["tasks"] if t["status"] != "COMPLETED"}
        if packet["p2_turbidity_count"] > 0:
            tools.propose_review(MONITORING, packet["event_id"],
                "New P2 observations warrant source-water review. Carry their evidence forward "
                "with the current unfinished review, or start a new review after completed work.",
                existing_task_id=targets.get(MONITORING))
        if any(packet[f"p{s}_turbidity_count"] <= 0 for s in (1, 2)):
            tools.propose_review(GAP, packet["event_id"],
                "Expected station evidence is absent from this archive window. Review coverage "
                "before interpreting changes across stations; absence does not prove sensor failure.",
                existing_task_id=targets.get(GAP))
        return tools.finish()


def validate_plan(plan: Plan, state: dict, released: list[dict]) -> None:
    """Recheck all writes at the service boundary, even for a faulty planner adapter."""
    validator = EvidenceTools(state, released)
    validator.get_case_context()
    validator.get_observations(released[-1]["event_id"])
    for proposal in plan.proposals:
        validator.propose_review(proposal["kind"], proposal["event_id"], proposal["reason"],
                                 existing_task_id=proposal["existing_task_id"])
        if validator.proposals[-1]["existing_task_id"] != proposal["existing_task_id"]:
            raise ValueError("The proposed review target does not match saved unfinished work.")
    validator.finish()
    observed = EvidenceTools(state, released)
    allowed = {"get_case_context", "get_observations", "propose_review"}
    metadata_seen = False
    for entry in plan.trace:
        if entry["tool"] == "strands_turn":
            if metadata_seen:
                raise ValueError("An agent turn can have only one SDK usage record.")
            metadata_seen = True
            continue
        if entry["tool"] not in allowed or metadata_seen:
            raise ValueError("The execution trace contains an unrecognized or out-of-order tool.")
        expected = getattr(observed, entry["tool"])(**entry["input"])
        if expected != entry["output"]:
            raise ValueError("The recorded tool result does not match the released case evidence.")
    if observed.finish().proposals != plan.proposals:
        raise ValueError("The recorded tool work does not match the proposed transaction.")
