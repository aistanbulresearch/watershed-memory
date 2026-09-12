"""Execute each proof step in a fresh process against the same SQLite database."""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feasibility.workflow import CaseStore

ROOT = Path(__file__).resolve().parent


def worker(args: argparse.Namespace) -> None:
    packets = json.loads((ROOT / "data/derived/replay-packets.json").read_text())
    with CaseStore(args.db) as store:
        if args.worker == "event":
            packet = packets[args.index]
            outcome = store.ingest(packet, now=packet["available_at"])
        elif args.worker in ("acknowledge", "complete_review"):
            task = next(t for t in store.snapshot()["tasks"] if t["kind"] == "MONITORING_REVIEW")
            note = "SIMULATED operator: received the review; monitoring work remains outstanding." if args.worker == "acknowledge" else "SIMULATED operator: reviewed the packet. This completes document review only; the watershed case and evidence-gap task remain open."
            outcome = store.respond(task["task_id"], args.worker, "SIMULATED demo operator", note, simulated=True, response_id=f"proof-{args.worker}-1")
        else:
            outcome = "PERSISTED_STATE_READ_IN_NEW_PROCESS"
        print(json.dumps({"outcome": outcome, "snapshot": store.snapshot()}))


def execute_step(db: Path, action: str, index: int = 0) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "feasibility.run_proof", "--worker", action, "--db", str(db), "--index", str(index)],
        cwd=ROOT.parent, text=True, capture_output=True, check=True, timeout=30,
    )
    return json.loads(completed.stdout)


def build_report(run: Path, steps: list[dict[str, Any]], checks: dict[str, bool]) -> None:
    audit = json.loads((ROOT / "data/derived/event-audit.json").read_text())
    rows = []
    for event in audit["events"]:
        rows.append(f"<tr><td>{event['date']}</td><td>{event['flow_day_peak_m3s']:.2f}</td><td>{event['stations']['P2']['day_turbidity']['maximum']:.2f}</td><td>{event['stations']['P1']['window_turbidity']['numeric_entries']}</td><td>{event['stations']['P2']['window_turbidity']['numeric_entries']}</td><td>{'Missing' if event['rainfall_status']=='NO_RETRIEVED_OBSERVATIONS' else 'Available'}</td></tr>")
    cards = []
    for i, step in enumerate(steps, 1):
        snap = step["snapshot"]
        task_text = " · ".join(f"{t['kind']}: {t['status']}" for t in snap["tasks"])
        cards.append(f"<article><span class='number'>{i:02}</span><div><h3>{html.escape(step['label'])}</h3><p>{html.escape(step['outcome'])}</p><p>{html.escape(task_text)}</p><small>{len(snap['events'])} event packets · {len(snap['tasks'])} tasks · {len(snap['responses'])} simulated responses · confidence {html.escape(snap['case']['monitoring_confidence'])}</small></div></article>")
    limitations = "".join(f"<li>{html.escape(line)}</li>" for line in audit["limitations"])
    check_items = "".join(f"<li>{html.escape(key.replace('_', ' '))}: <strong>{'PASS' if value else 'FAIL'}</strong></li>" for key, value in checks.items())
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Watershed Memory feasibility proof</title>
<style>body{{margin:0;background:#f3f5f0;color:#21382e;font:16px/1.6 system-ui,sans-serif}}main{{max-width:1080px;margin:auto;padding:48px 24px}}h1{{font-size:42px;line-height:1.15;letter-spacing:-1.5px;margin:12px 0}}h2{{font-size:24px;margin-top:40px}}h3{{margin:0;font-size:18px}}p{{margin:8px 0}}.eyebrow{{letter-spacing:2px;font-size:12px;text-transform:uppercase}}.notice{{padding:18px 22px;border-left:4px solid #c59435;background:#fff8e7;margin:22px 0}}.status{{display:flex;gap:12px;flex-wrap:wrap}}.status div{{background:white;padding:14px 20px;border:1px solid #d6ded6;border-radius:8px}}article{{display:flex;gap:20px;padding:20px 0;border-bottom:1px solid #d6ded6}}.number{{font-size:25px;color:#66816d;min-width:40px}}small{{color:#526759}}table{{border-collapse:collapse;width:100%;font-size:14px;background:white}}td,th{{padding:12px;border-bottom:1px solid #d6ded6;text-align:left}}th{{background:#e2e9df}}.scroll{{overflow:auto}}a{{color:#245c45}}li{{margin:8px 0}}footer{{border-top:1px solid #cbd7ca;margin-top:36px;padding-top:20px;color:#526759}}</style>
<main><div class="eyebrow">Executed local experiment · historical environmental data</div><h1>Watershed Memory<br>Feasibility proof</h1><p>One watershed case carries unfinished review work across three storm windows and survives process restarts.</p>
<div class="notice"><strong>Recorded execution, not a live agent interface.</strong> A deterministic Python driver executed real SQLite writes. All operator actions and packet availability times are explicitly simulated. No LLM or Strands runtime was used.</div>
<div class="status"><div><strong>Workflow</strong><br>Executed and verified</div><div><strong>Historical data</strong><br>Usable with limitations</div><div><strong>Agent reasoning</strong><br>Not verified</div></div>
<h2>Three historical windows</h2><p>Each window covers the event date plus one day on either side. Flow peaks below use the event calendar day and official USGS readings. Turbidity peaks and entry counts use the research archive; they are not certified independent measurements.</p>
<div class="scroll"><table><thead><tr><th>Event</th><th>P2 daily peak flow<br>m³/s</th><th>P2 daily peak turbidity<br>FNU convention</th><th>P1 entries<br>72 hours</th><th>P2 entries<br>72 hours</th><th>P2 rainfall</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<h2>What the executed workflow did</h2>{''.join(cards)}
<h2>Verification</h2><ul>{check_items}</ul>
<h2>Evidence boundaries</h2><ul>{limitations}</ul><div class="notice">P1 contains numeric records through August 30 at 10:45, while the paper states an August 28 cutoff. This conflict remains unresolved. September's missing P1 entries are verified; the exact historical outage-detection time is not.</div>
<p><a href="trace.json">Full execution trace</a> · <a href="summary.json">Verification results</a> · <a href="../../data/derived/event-audit.json">Data audit</a></p>
<footer>Case status stays OPEN. Water safety stays NOT_ASSESSED. No external messages, field dispatch, treatment action or cloud deployment occurred.<br>Sources: Nichols et al., Zenodo record 12764157, CC BY 4.0; USGS station 08380500. Generated {html.escape(datetime.now(timezone.utc).isoformat())}.</footer></main></html>"""
    (run / "proof.html").write_text(document, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", choices=("event", "acknowledge", "complete_review", "inspect"))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args()
    if args.worker:
        if args.db is None:
            parser.error("--db is required for a worker step")
        worker(args)
        return
    run = ROOT / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run.mkdir(parents=True, exist_ok=False)
    db = run / "case.sqlite"
    actions = [
        ("July observations create a review task", "event", 0),
        ("August observations attach to the same unfinished task", "event", 1),
        ("Simulated operator acknowledges the outstanding review", "acknowledge", 0),
        ("September observations preserve the review and open evidence-gap work", "event", 2),
        ("Simulated operator completes the evidence review only", "complete_review", 0),
        ("Redelivered September packet creates no duplicate work", "event", 2),
        ("Retried operator response creates no duplicate response", "complete_review", 0),
        ("Another fresh process restores the final state", "inspect", 0),
    ]
    steps = []
    for label, action, index in actions:
        step = execute_step(db, action, index)
        step["label"] = label
        steps.append(step)
    final = steps[-1]["snapshot"]
    reviews = [t for t in final["tasks"] if t["kind"] == "MONITORING_REVIEW"]
    gaps = [t for t in final["tasks"] if t["kind"] == "EVIDENCE_GAP_REVIEW"]
    linked_events = {e["event_id"] for e in final["task_evidence"] if reviews and e["task_id"] == reviews[0]["task_id"]}
    checks = {
        "three_distinct_event_packets": len(final["events"]) == 3,
        "one_review_task_across_three_events": len(reviews) == 1 and linked_events == {e["event_id"] for e in final["events"]},
        "separate_evidence_gap_task": len(final["tasks"]) == 2 and len(gaps) == 1 and gaps[0]["status"] == "OPEN",
        "review_completed_without_closing_gap_task": len(reviews) == 1 and reviews[0]["status"] == "COMPLETED" and len(gaps) == 1 and gaps[0]["status"] == "OPEN",
        "two_labelled_operator_responses": len(final["responses"]) == 2 and all(r["simulated"] == 1 for r in final["responses"]),
        "event_duplicate_ignored": steps[5]["outcome"] == "DUPLICATE_IGNORED",
        "operator_retry_ignored": steps[6]["outcome"] == "DUPLICATE_RESPONSE_IGNORED",
        "fresh_process_restores_exact_state": steps[-1]["snapshot"] == steps[-2]["snapshot"],
        "case_remains_open": final["case"]["status"] == "OPEN",
        "water_safety_not_assessed": final["case"]["water_safety"] == "NOT_ASSESSED",
        "monitoring_confidence_degraded": final["case"]["monitoring_confidence"] == "DEGRADED",
    }
    summary = {"run_directory": str(run), "execution_mode": "DETERMINISTIC_LOCAL_WORKFLOW_NO_LLM", "operator_actions": "SIMULATED", "fresh_process_invocations": len(steps), "checks": checks, "workflow_pass": all(checks.values()), "strands_reasoning": "NOT_VERIFIED"}
    (run / "trace.json").write_text(json.dumps(steps, indent=2), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    build_report(run, steps, checks)
    (ROOT / "runs/latest.json").write_text(json.dumps({"run_directory": str(run)}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if not all(checks.values()):
        raise RuntimeError("At least one proof check failed")


if __name__ == "__main__":
    main()
