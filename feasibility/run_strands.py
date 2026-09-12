"""Run the preregistered three-case Strands gate against real Amazon Bedrock.

This command makes bounded, billable model calls. It requires explicit model,
region and expected account arguments; it never falls back to a scripted model.
Evidence and databases are written into a new local run directory.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import boto3

from watershed_memory.planning import GAP, MONITORING
from watershed_memory.service import Service
from watershed_memory.strands_agent import INSTRUCTION_VERSION, AgentTurnError, bedrock_planner


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def setup(path: Path, case: str) -> tuple[str, dict]:
    replay = Service(path)
    session = replay.create_session()["session_id"]
    state = replay.advance(session, "setup-july")
    if case in ("S2", "S3"):
        state = replay.advance(session, "setup-august")
    task = state["tasks"][0]["id"]
    if case == "S2":
        state = replay.respond(session, "setup-response", task, "acknowledge",
                               "Demonstration: assigned to the source-water team for continuing review.")
    elif case == "S3":
        state = replay.respond(session, "setup-response", task, "complete_review",
                               "Counterfactual demonstration: prior review completed before September.")
    return session, state


def checks(case: str, before: dict, after: dict) -> dict[str, bool]:
    old = before["tasks"][0]
    old_after = next(t for t in after["tasks"] if t["id"] == old["id"])
    unfinished = [t for t in after["tasks"] if t["status"] != "COMPLETED"]
    tools = [t["tool"] for t in after["trace"]]
    results = {
        "retrieved_case_and_observations": "get_case_context" in tools and "get_observations" in tools,
        "explicit_review_proposals": "propose_review" in tools,
        "real_strands_execution": after["mode"]["id"] == "strands_historical_replay",
        "one_window_advanced": after["progress"]["processed"] == before["progress"]["processed"] + 1,
        "case_remains_open": after["case"]["status"] == "OPEN",
        "no_water_safety_assessment": after["case"]["water_safety"] == "NOT_ASSESSED",
        "operator_responses_preserved": before["responses"] == after["responses"],
    }
    if case == "S1":
        results["same_unfinished_review_gained_evidence"] = len(after["tasks"]) == 1 and len(old_after["evidence"]) == 2
    elif case == "S2":
        results["same_review_gained_evidence"] = len(old_after["evidence"]) == 3 and old_after["status"] == "ACKNOWLEDGED"
        results["separate_gap_review"] = {t["kind"] for t in unfinished} == {MONITORING, GAP}
    else:
        results["completed_review_not_reopened"] = old_after == old
        results["new_monitoring_and_gap_work"] = len(after["tasks"]) == 3 and {t["kind"] for t in unfinished} == {MONITORING, GAP}
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--cases", nargs="+", choices=("S1", "S2", "S3"), default=["S1", "S2", "S3"])
    args = parser.parse_args()
    identity = boto3.Session(profile_name=args.profile, region_name=args.region).client("sts").get_caller_identity()
    if identity["Account"] != args.expected_account:
        raise RuntimeError("AWS account does not match the explicit test account.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run = Path(__file__).parent / "runs" / f"strands-{stamp}"
    run.mkdir(parents=True, exist_ok=False)
    data = Path(__file__).resolve().parents[1] / "watershed_memory/data/observations.json"
    manifest = {"created_at": stamp, "execution": "REAL_STRANDS_BEDROCK", "model_id": args.model_id,
        "region": args.region, "sdk": version("strands-agents"), "python": sys.version,
        "instructions": INSTRUCTION_VERSION, "data_sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
        "cases": args.cases, "max_model_calls_per_case": 8, "max_output_tokens_per_call": 1000,
        "timeout_seconds_per_case": 120, "setup": "rules replay with labelled demonstration responses"}
    save(run / "manifest.json", manifest)
    all_results = {}
    print(f"Evidence directory: {run}", flush=True)
    for case in args.cases:
        path = run / f"{case}.sqlite"
        session, before = setup(path, case)
        save(run / f"{case}-before.json", before)
        live = Service(path, bedrock_planner(args.model_id, args.region, args.profile))
        try:
            after = live.advance(session, "live-gate")
            save(run / f"{case}-after.json", after)
            case_checks = checks(case, before, after)
            case_checks["same_request_does_not_repeat_model_or_action"] = live.advance(session, "live-gate") == after
            code = "import json,sys;from pathlib import Path;from watershed_memory.service import Service;print(json.dumps(Service(Path(sys.argv[1])).snapshot(sys.argv[2])))"
            fresh = subprocess.run([sys.executable, "-c", code, str(path), session],
                                   capture_output=True, text=True, check=True, timeout=20)
            case_checks["fresh_process_persistence"] = json.loads(fresh.stdout) == after
            all_results[case] = {"passed": all(case_checks.values()), "checks": case_checks,
                                 "execution": next(t for t in after["trace"] if t["tool"] == "strands_turn")}
        except Exception as error:
            details = error.evidence if isinstance(error, AgentTurnError) else {}
            all_results[case] = {"passed": False, "error_type": type(error).__name__,
                "error": str(error), "execution": details,
                "state_unchanged": live.snapshot(session) == before}
        save(run / "results.json", all_results)
        print(f"{case}: {'PASS' if all_results[case]['passed'] else 'FAIL'}", flush=True)
    return 0 if all(r["passed"] for r in all_results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
