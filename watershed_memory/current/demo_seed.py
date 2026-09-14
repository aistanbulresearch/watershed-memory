"""Create an offline, disposable current field desk demonstration."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

CASE_ID = "GALLINAS-JUDGE-DEMO"
MONITOR_ID = "gallinas-judge-demo-watch"


def _reject_links(path: Path) -> None:
    """Reject links in the existing lexical path before resolving it."""
    current = path.absolute()
    while True:
        try:
            info = os.lstat(current)
            if os.path.islink(current) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("output path or parent is a symlink/reparse point")
        except FileNotFoundError:
            pass
        parent = current.parent
        if parent == current:
            break
        current = parent


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_json(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {key: _json(item) for key, item in asdict(value).items()}
    return value


def _fixture(path: Path):
    from ..watch.observations import Observation, PageReceipt, SeriesSpec, SourceBatch
    from ..watch.store import MonitorConfig

    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        raw.get("schema_version") != 1
        or raw.get("execution_label") != "SAVED_USGS_OBSERVATIONS_OFFLINE_DEMO"
    ):
        raise ValueError("unsupported offline demonstration fixture")
    config_raw = dict(raw["monitor_config"])
    config_raw["start_at"] = _time(config_raw["start_at"])
    config_raw["series"] = tuple(SeriesSpec(**item) for item in config_raw["series"])
    config = MonitorConfig(**config_raw)
    batches = []
    for item in raw["batches"]:
        observations = tuple(
            Observation(
                row["station_id"],
                row["series_id"],
                row["parameter_code"],
                row["statistic_id"],
                _time(row["observed_at"]),
                None if row["value"] is None else Decimal(row["value"]),
                row["unit"],
                row["approval_status"],
                row["qualifier"],
                _time(row["source_modified_at"]),
                row["provider_id"],
            )
            for row in item["observations"]
        )
        pages = tuple(
            PageReceipt(p["url"], p["sha256"], p["byte_count"], _time(p["retrieved_at"]))
            for p in item["pages"]
        )
        batches.append(
            SourceBatch(
                item["station_id"],
                _time(item["start"]),
                _time(item["end"]),
                _time(item["retrieved_at"]),
                observations,
                pages,
            )
        )
    hashes = sorted(item.semantic_hash for batch in batches for item in batch.observations)
    if len(hashes) != raw.get("observation_count") or hashes != sorted(
        raw.get("semantic_hashes", [])
    ):
        raise ValueError("offline fixture semantic hashes do not match observations")
    return raw, config, tuple(batches)


def seed(output: Path) -> tuple[str, Path]:
    from ..watch.store import WatchStore
    from .case_store import CaseStore
    from .case_types import CaseConfig, ReviewDraft
    from .fact_types import CoveragePolicy
    from .field_codec import encode
    from .field_store import FieldStore
    from .field_types import (
        AttachFieldEvidence,
        DecideFieldPlan,
        FieldPlanSpec,
        FieldPrincipal,
        ProposeFieldPlan,
        ReportFieldResult,
        VerifyFieldReport,
    )
    from .locations import LocationEntry, LocationRegistry

    _reject_links(output)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("output directory already exists; refusing overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    fixture_path = Path(__file__).resolve().parents[1] / "data" / "current-demo-observations.json"
    raw, monitor, batches = _fixture(fixture_path)
    output.mkdir()
    db_path = output / "current-work.sqlite"
    site_path = output / "approved-site.json"
    # The source timestamps remain historical; human actions use a fresh bounded
    # planning anchor so the seeded desk opens with an actionable future window.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sequence_now = now - timedelta(minutes=20)
    watch = WatchStore(db_path)
    watch.register(monitor, now=monitor.start_at)
    for batch in batches:
        lease = watch.acquire_poll(monitor.monitor_id, now=batch.end)
        if lease is None:
            raise RuntimeError("fixture poll window was not available")
        watch.commit_poll(lease, batch, now=batch.retrieved_at)
    events = watch.pending_events(MONITOR_ID)
    if not events:
        raise RuntimeError("fixture produced no source event")
    cases = CaseStore(db_path)
    policy = CoveragePolicy(
        "judge-demo-policy", tuple(item.parameter_code for item in monitor.series)
    )
    cases.register(CaseConfig(CASE_ID, MONITOR_ID, policy, True), now=sequence_now)
    site = LocationEntry(
        "gallinas-judge-demo-site",
        1,
        "Gallinas demonstration outer marker",
        "FIELD_SITE",
        CASE_ID,
        True,
        "APPROVED",
        ("VISUAL_INSPECTION",),
        Decimal("35.6519944444444"),
        Decimal("-105.318830555556"),
        "WGS84",
        "Demonstration location; coordinates inherited from the attributed station reference.",
        "https://waterdata.usgs.gov/monitoring-location/USGS-08380500/",
        "Demonstration field record; U.S. Geological Survey station reference.",
        sequence_now,
        "local-coordinator",
        sequence_now,
    )
    site_path.write_text(encode(asdict(site)) + "\n", encoding="utf-8", newline="\n")
    locations = LocationRegistry((site,))
    fields = FieldStore(cases, locations)
    coordinator = FieldPrincipal("local-coordinator", ("COORDINATOR",), (CASE_ID,))
    operator = FieldPrincipal("local-operator", ("FIELD_OPERATOR",), (CASE_ID,))
    verifier = FieldPrincipal("local-verifier", ("VERIFIER",), (CASE_ID,))
    due = sequence_now + timedelta(hours=1)
    review = cases.stage_review(
        CASE_ID,
        ReviewDraft(
            "COVERAGE_REVIEW",
            "Review the saved Gallinas observation",
            "Review the saved source observation before the next demonstration field check.",
            events[-1].event_id,
            due,
        ),
        request_id="judge-demo-review",
        expected_case_revision=0,
        now=sequence_now,
    )
    work_start = sequence_now + timedelta(seconds=5)
    spec = FieldPlanSpec(
        "VISUAL_INSPECTION",
        "Inspect the demonstration outer marker and document inaccessible areas.",
        "Demonstration field operator",
        work_start,
        work_start + timedelta(minutes=30),
        ("INSPECTION_RECORD_REFERENCE",),
    )
    proposed = fields.propose_plan(
        CASE_ID,
        ProposeFieldPlan(review.task_id, review.revision, site.location_id, 1, spec, True),
        principal=coordinator,
        request_id="judge-demo-propose",
        expected_case_revision=1,
        now=sequence_now + timedelta(seconds=1),
    )
    approved = fields.decide_plan(
        CASE_ID,
        DecideFieldPlan(proposed.plan.plan_id, proposed.plan.revision, "APPROVE"),
        principal=coordinator,
        request_id="judge-demo-approve",
        expected_case_revision=2,
        now=sequence_now + timedelta(seconds=2),
    )
    reported = fields.report_result(
        CASE_ID,
        ReportFieldResult(
            approved.plan.plan_id,
            approved.plan.revision,
            "PARTIAL",
            "The outer demonstration marker was inaccessible.",
            sequence_now + timedelta(minutes=1),
            sequence_now + timedelta(minutes=8),
            True,
        ),
        principal=operator,
        request_id="judge-demo-report",
        expected_case_revision=3,
        now=sequence_now + timedelta(minutes=9),
    )
    attached = fields.attach_evidence(
        CASE_ID,
        AttachFieldEvidence(
            reported.result.report.report_id,
            reported.result.report.revision,
            "OPERATOR_RECORD_REFERENCE",
            "INSPECTION_RECORD_REFERENCE",
            "judge-demo-inspection-record",
            "Demonstration field record entered by the operator.",
            sequence_now + timedelta(minutes=8),
            None,
            True,
        ),
        principal=operator,
        request_id="judge-demo-evidence",
        expected_case_revision=4,
        now=sequence_now + timedelta(minutes=10),
    )
    fields.verify_result(
        CASE_ID,
        VerifyFieldReport(
            reported.result.report.report_id,
            reported.result.report.revision,
            tuple(item.evidence_id for item in attached.result.evidence),
            "Review of the demonstrated partial inspection scope and operator record.",
            True,
        ),
        principal=verifier,
        request_id="judge-demo-verify",
        expected_case_revision=5,
        now=sequence_now + timedelta(minutes=11),
    )
    return CASE_ID, db_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    case_id, db = seed(args.output)
    site = args.output.resolve() / "approved-site.json"
    print(f"case_id={case_id}")
    args = [
        sys.executable,
        "-m",
        "watershed_memory.current.desk_cli",
        "--db",
        str(db),
        "--case",
        case_id,
        "--field-location",
        str(site),
        "--field-principal-id",
        "local-operator",
        "--port",
        "8771",
    ]
    command = (
        shlex.join(args)
        if os.name != "nt"
        else "& " + " ".join("'" + value.replace("'", "''") + "'" for value in args)
    )
    print("desk_command=" + command)


if __name__ == "__main__":
    main()
