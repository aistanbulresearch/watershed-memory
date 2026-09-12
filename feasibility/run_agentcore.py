"""Execute two bounded AgentCore turns against one durable watershed case."""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from watershed_memory.agentcore_client import AgentCoreClient, AgentCoreTurnError
from watershed_memory.budget import BoundedPlanner
from watershed_memory.cli import aws_account
from watershed_memory.planning import GAP, MONITORING
from watershed_memory.service import Service
from watershed_memory.strands_agent import INSTRUCTION_VERSION


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def snapshot_subprocess(path: Path, session: str) -> dict:
    code = "import json,sys;from pathlib import Path;from watershed_memory.service import Service;print(json.dumps(Service(Path(sys.argv[1])).snapshot(sys.argv[2])))"
    result = subprocess.run([sys.executable, "-c", code, str(path), session], capture_output=True,
                            text=True, check=True, timeout=20)
    return json.loads(result.stdout)


def run_gate(args, client_factory=AgentCoreClient, account_lookup=aws_account,
             snapshot_reader=snapshot_subprocess):
    if account_lookup(args.profile, args.region) != args.expected_account:
        raise ValueError("AWS account does not match the explicit proof account.")

    def new_client():
        return client_factory(args.runtime_arn, args.runtime_endpoint, args.region,
            expected_account=args.expected_account, expected_version=args.runtime_version,
            model_id=args.model_id, profile=args.profile)

    bounded = BoundedPlanner(new_client(), limit=2)
    expected_mode = dict(bounded.mode)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root = Path(args.output_root) / f"agentcore-{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    code_root = Path(__file__).resolve().parents[1]
    names = ("feasibility/run_agentcore.py", "watershed_memory/agentcore_client.py",
             "watershed_memory/agentcore_protocol.py", "watershed_memory/service.py",
             "watershed_memory/planning.py", "watershed_memory/strands_agent.py",
             "watershed_memory/budget.py", "watershed_memory/cli.py", "watershed_memory/catalog.py",
             "watershed_memory/data/observations.json")
    hashes = {name: hashlib.sha256((code_root / name).read_bytes()).hexdigest() for name in names}
    manifest = {"created_at": stamp, "execution": "REAL_AGENTCORE_STRANDS" if bounded.mode["agent_enabled"]
                else "SCRIPTED_PROTOCOL_TEST", "expected_account": args.expected_account,
                "region": args.region, "model_id": args.model_id, "runtime_arn": args.runtime_arn,
                "runtime_endpoint": args.runtime_endpoint, "runtime_version": args.runtime_version,
                "sdk": version("strands-agents"), "instruction_version": INSTRUCTION_VERSION,
                "source_sha256": hashes, "total_turn_limit": 2, "setup": "Labelled July rules replay",
                "operator": "Demonstration acknowledgment after August"}
    write(root / "manifest.json", manifest)
    print(f"Evidence directory: {root}", flush=True)
    db = root / "case.sqlite"
    rules = Service(db)
    session = rules.create_session()["session_id"]
    july = rules.advance(session, "rules-july")
    task_id = july["tasks"][0]["id"]
    result = {"passed": False, "execution": manifest["execution"], "stages": {}}
    receipts = []
    last = july
    for index, phase in enumerate(("august", "september")):
        service = Service(db, bounded)
        before = service.snapshot(session)
        write(root / f"before-{phase}.json", before)
        request_id = f"agentcore-{phase}"
        try:
            if index:
                selected = new_client()
                if selected.mode != expected_mode:
                    raise ValueError("The new Runtime client changed the selected execution mode.")
                bounded.planner = selected
            after = service.advance(session, request_id)
            last = after
            write(root / f"after-{phase}.json", after)
            marker = next(entry for entry in after["trace"] if entry["tool"] == "strands_turn")
            cloud = marker["output"]["agentcore"]
            receipts.append(cloud)
            checks = {
                "requested_execution_mode": after["mode"] == expected_mode,
                "exact_turn_allowance": bounded.attempts == index + 1,
                "same_review": after["tasks"][0]["id"] == task_id,
                "linked_evidence_count": len(after["tasks"][0]["evidence"]) == index + 2,
                "operator_responses_preserved": before["responses"] == after["responses"],
                "case_remains_open": after["case"]["status"] == "OPEN",
                "water_safety_unassessed": after["case"]["water_safety"] == "NOT_ASSESSED",
                "session_stop_requested": cloud["stop_status"] == "STOP_REQUEST_ACCEPTED",
                "duplicate_receipt": Service(db, bounded).advance(session, request_id) == after,
            }
            checks["duplicate_did_not_invoke"] = bounded.attempts == index + 1
            if not index:
                checks["one_open_monitoring_review"] = len(after["tasks"]) == 1 and (
                    after["tasks"][0]["kind"], after["tasks"][0]["status"]) == (MONITORING, "OPEN")
            else:
                checks["acknowledged_review_and_separate_gap"] = len(after["tasks"]) == 2 and (
                    after["tasks"][0]["status"] == "ACKNOWLEDGED"
                    and after["tasks"][1]["kind"] == GAP and after["tasks"][1]["status"] == "OPEN")
            result["stages"][phase] = {"passed": all(checks.values()), "checks": checks,
                                       "execution": marker}
            write(root / "results.json", result)
            if not all(checks.values()):
                return root, result
            if not index:
                last = service.respond(session, "demo-ack", task_id, "acknowledge",
                    "Demonstration: the source-water team is carrying this review forward.")
                write(root / "operator-response.json", last)
        except Exception as error:
            failure = {"passed": False, "error_type": type(error).__name__,
                       "state_unchanged": Service(db).snapshot(session) == before,
                       "planner_attempts": bounded.attempts}
            if isinstance(error, AgentCoreTurnError):
                failure["execution"] = error.evidence
            result["stages"][phase] = failure
            write(root / f"failure-{phase}.json", failure)
            write(root / "results.json", result)
            return root, result
    try:
        final_checks = {
            "two_distinct_runtime_sessions": len({row["runtime_session_id"] for row in receipts}) == 2,
            "exactly_two_planner_turns": bounded.attempts == 2,
            "fresh_process_persistence": snapshot_reader(db, session) == last,
        }
        result["final_checks"] = final_checks
        result["passed"] = all(final_checks.values())
    except Exception as error:
        result["final_error_type"] = type(error).__name__
    result["planner_attempts"] = bounded.attempts
    write(root / "results.json", result)
    return root, result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    for name in ("expected-account", "region", "model-id", "runtime-arn", "runtime-endpoint", "runtime-version"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--output-root", type=Path, default=Path("feasibility/runs"))
    args = parser.parse_args()
    try:
        root, result = run_gate(args)
        print(json.dumps({"run": str(root), "passed": result["passed"]}))
        return 0 if result["passed"] else 1
    except Exception as error:
        print(f"Gate could not start: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
