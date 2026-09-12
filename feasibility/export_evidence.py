"""Export an allowlisted public recording from a successful two-session cloud gate."""

import argparse
import hashlib
import json
import re
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from watershed_memory.catalog import PACKETS, SOURCES

COMMON_CHECKS = frozenset("requested_execution_mode exact_turn_allowance same_review "
    "linked_evidence_count operator_responses_preserved case_remains_open water_safety_unassessed "
    "session_stop_requested duplicate_receipt duplicate_did_not_invoke".split())
STAGE_CHECKS = {"august": COMMON_CHECKS | {"one_open_monitoring_review"},
                "september": COMMON_CHECKS | {"acknowledged_review_and_separate_gap"}}
FINAL_CHECKS = frozenset("two_distinct_runtime_sessions exactly_two_planner_turns "
                         "fresh_process_persistence".split())


def gate_checks(results: dict) -> dict:
    if (results.get("passed") is not True or results.get("execution") != "REAL_AGENTCORE_STRANDS"
            or set(results.get("stages", {})) != set(STAGE_CHECKS)
            or set(results.get("final_checks", {})) != FINAL_CHECKS):
        raise ValueError("Only the complete successful real AgentCore gate can be published.")
    checks = dict(results["final_checks"])
    for phase, expected in STAGE_CHECKS.items():
        stage = results["stages"][phase]
        if stage.get("passed") is not True or set(stage.get("checks", {})) != expected:
            raise ValueError("Recorded gate check names are missing or unrecognized.")
        checks.update({f"{phase}_{key}": value for key, value in stage["checks"].items()})
    if any(value is not True for value in checks.values()):
        raise ValueError("Every published gate check must have passed.")
    return checks


def pick(value: dict, fields: str) -> dict:
    return {key: deepcopy(value[key]) for key in fields.split() if key in value}


class PublicRecording:
    """Keep observable tool work; exclude transport identity, notes and model text."""

    def __init__(self):
        self.tasks: dict[str, str] = {}
        self.sessions: dict[str, str] = {}

    def task_id(self, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in self.tasks:
            self.tasks[value] = f"R-{len(self.tasks) + 1:02d}"
        return self.tasks[value]

    def task(self, value: dict) -> dict:
        return {**pick(value, "kind status title reason evidence"), "id": self.task_id(value["id"])}

    def response(self, value: dict) -> dict:
        return {**pick(value, "action simulated"), "task_id": self.task_id(value["task_id"])}

    def context(self, value: dict) -> dict:
        return {"case": pick(value["case"], "id title subtitle status coverage water_safety"),
                "tasks": [self.task(row) for row in value.get("tasks", [])],
                "responses": [self.response(row) for row in value.get("responses", [])]}

    def trace_entry(self, entry: dict) -> dict:
        name, incoming, outgoing = entry["tool"], entry["input"], entry["output"]
        if name == "get_case_context":
            incoming = {}
            outgoing = {**self.context(outgoing),
                        **pick(outgoing, "released_event_ids current_event_id")}
        elif name == "get_observations":
            incoming = pick(incoming, "event_id")
            canonical = next((p for p in PACKETS if p["event_id"] == incoming.get("event_id")), None)
            if canonical != outgoing:
                raise ValueError("Recorded observations differ from the attributed public extract.")
            outgoing = deepcopy(canonical)
        elif name == "propose_review":
            incoming = {**pick(incoming, "kind event_id reason"),
                        "existing_task_id": self.task_id(incoming.get("existing_task_id"))}
            outgoing = {**pick(outgoing, "status operation committed"),
                        "task_id": self.task_id(outgoing.get("task_id"))}
        elif name == "commit_case_turn":
            incoming, outgoing = pick(incoming, "event_id"), pick(outgoing, "status changes")
        elif name == "strands_turn":
            incoming = pick(incoming, "instruction_version model_id sdk_version scripted_test")
            cloud = outgoing["agentcore"]
            identity = cloud["runtime_session_id"]
            if identity not in self.sessions:
                self.sessions[identity] = f"Runtime session {len(self.sessions) + 1}"
            outgoing = {**pick(outgoing, "model_calls elapsed_seconds stop_reason"),
                        "usage": pick(outgoing.get("usage", {}), "inputTokens outputTokens totalTokens"),
                        "agentcore": {**pick(cloud, "qualifier endpoint_version_verified_before_call stop_status"),
                                      "runtime_session_alias": self.sessions[identity]}}
        else:
            raise ValueError("Unknown tool cannot enter the public recording.")
        return {"tool": name, "input": incoming, "output": outgoing}

    def snapshot(self, value: dict) -> dict:
        if value.get("sources") != SOURCES:
            raise ValueError("Public recording must preserve the complete canonical source attribution.")
        return {**self.context(value),
                "progress": pick(value.get("progress", {}), "processed total next_label"),
                "events": [pick(row, "id label date processed p1_count p2_count flow_peak_cfs")
                           for row in value.get("events", [])],
                "latest_brief": pick(value.get("latest_brief", {}), "headline summary changes"),
                "sources": [pick(row, "label url") for row in value.get("sources", [])],
                "trace": [self.trace_entry(entry) for entry in value.get("trace", [])]}


def build_recording(run: Path, artifact: Path, source_commit: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("Use a complete Git commit for the recorded Runtime source.")
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((run / "results.json").read_text(encoding="utf-8"))
    if manifest["execution"] != "REAL_AGENTCORE_STRANDS":
        raise ValueError("Only a successful real AgentCore gate can become this recording.")
    checks = gate_checks(results)
    runtime_files = ("watershed_memory/agentcore_protocol.py", "watershed_memory/planning.py",
                     "watershed_memory/strands_agent.py", "watershed_memory/catalog.py",
                     "watershed_memory/data/observations.json", "runtime/entrypoint.py", "runtime/launch.py")
    code_root = Path(__file__).resolve().parents[1]
    if not set(runtime_files[:5]).issubset(manifest.get("source_sha256", {})):
        raise ValueError("The gate must fingerprint every shared Runtime source and data file.")
    with ZipFile(artifact) as archive:
        for name in runtime_files:
            payload = archive.read(name)
            committed = subprocess.run(["git", "-C", str(code_root), "show", f"{source_commit}:{name}"],
                                       check=True, capture_output=True, timeout=10).stdout
            if payload.replace(b"\r\n", b"\n") != committed.replace(b"\r\n", b"\n"):
                raise ValueError("Runtime artifact does not match the recorded source commit.")
            expected = manifest["source_sha256"].get(name)
            if expected and hashlib.sha256(payload).hexdigest() != expected:
                raise ValueError("Runtime artifact does not match the gate's source fingerprint.")
    moments = [
        ("july", "July · the review opens", "July's observations open the source-water monitoring review.",
         "Historical rules setup · recorded state", "before-august.json"),
        ("august", "August · the same work continues", "A fresh cloud session adds August's evidence to the same unfinished review.",
         "Recorded Strands execution on AgentCore", "after-august.json"),
        ("operator", "The operator takes ownership", "The demonstration operator acknowledges the review. This response belongs to the saved case.",
         "Recorded demonstration operator action", "operator-response.json"),
        ("september", "September · a new gap appears", "Another cloud session keeps the acknowledgment, links September's evidence and opens a separate P1 evidence-gap review.",
         "Recorded Strands execution on AgentCore", "after-september.json"),
    ]
    public = PublicRecording()
    steps = []
    for key, title, caption, label, filename in moments:
        original = json.loads((run / filename).read_text(encoding="utf-8"))
        if key in results["stages"]:
            markers = [row for row in original["trace"] if row["tool"] == "strands_turn"]
            if (markers != [results["stages"][key]["execution"]]
                    or markers[0]["input"]["scripted_test"] is not False):
                raise ValueError("Saved state and real execution receipt do not agree.")
        snapshot = public.snapshot(original)
        if key == "operator":
            snapshot["trace"] = []  # Acknowledgment does not execute the earlier model turn again.
        steps.append({"id": key, "title": title, "caption": caption, "execution_label": label,
                      "snapshot": snapshot})
    recorded_at = datetime.strptime(manifest["created_at"], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
    return {"schema_version": 1, "title": "Three storms. One memory.",
            "recorded_at": recorded_at.isoformat(), "execution": "RECORDED_AGENTCORE_STRANDS",
            "code": {"source_commit": source_commit,
                     "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                     "source_comparison": "Git source matches after LF normalization; the SHA-256 identifies the exact reviewed artifact."},
            "checks": checks, "steps": steps,
            "publication_notes": "Observable tool receipts and saved states. Transport identifiers use stable aliases; operator note text and model message contents are excluded."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_recording(args.run, args.artifact, args.source_commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Exported {len(result['steps'])} recorded moments to {args.output}")


if __name__ == "__main__":
    main()
