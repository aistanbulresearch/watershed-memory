"""Audit original archive entries without executing any third-party source code."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import statistics
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "derived"
MDT = timezone(timedelta(hours=-6))  # Verified USGS summer offset; archive inference.
DATES = ("2022-07-09", "2022-08-03", "2022-09-10")
CFS_TO_CMS = 0.028316846592
EXPECTED_MD5 = "42cf095c013aeb3009fa2a773b21f1f9"


def number(value: str | float | None) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def archive_time(value: str) -> datetime:
    """Assume the P2 discharge-supported MDT offset, explicitly reported as inference."""
    return datetime.strptime(value, "%m/%d/%Y %H:%M").replace(tzinfo=MDT)


def stats(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    valid = [(r["timestamp"], r[field]) for r in rows if r.get(field) is not None]
    if not valid:
        return {"rows": len(rows), "numeric_entries": 0, "minimum": None, "maximum": None, "peak_time": None}
    peak = max(valid, key=lambda x: x[1])
    return {"rows": len(rows), "numeric_entries": len(valid), "minimum": min(v for _, v in valid), "maximum": peak[1], "peak_time": peak[0].isoformat()}


def select(rows: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [r for r in rows if start <= r["timestamp"] < end]


def load_archive() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    path = RAW / "HPCC-wildfire-v1.0.1.zip"
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    if digest != EXPECTED_MD5:
        raise ValueError("Source archive checksum mismatch")
    records: dict[str, list[dict[str, Any]]] = {}
    manifest: dict[str, Any] = {"archive_md5": digest, "archive_bytes": path.stat().st_size, "members": {}}
    with zipfile.ZipFile(path) as archive:
        lfs = [n for n in archive.namelist() if n.endswith(".dss") and archive.getinfo(n).file_size < 200]
        manifest["dss_lfs_pointer_count"] = len(lfs)
        for station in ("P1", "P2", "P3"):
            member = next(n for n in archive.namelist() if n.endswith(f"/Raw Data/{station}Data.csv"))
            payload = archive.read(member)
            manifest["members"][station] = {"path": member, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
            rows = []
            for row_number, row in enumerate(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))), start=2):
                rows.append({"source_row": row_number, "original_timestamp": row["TimeStamp"], "timestamp": archive_time(row["TimeStamp"]), "turbidity": number(row["Turbidity"]), "discharge_cfs": number(row["Discharge"])})
            records[station] = rows
            valid = [r for r in rows if r["turbidity"] is not None]
            manifest["members"][station].update({
                "rows": len(rows), "turbidity_numeric_entries": len(valid),
                "first_numeric_original_time": valid[0]["original_timestamp"],
                "last_numeric_original_time": valid[-1]["original_timestamp"],
                "timestamp_duplicates": len(rows) - len({r["timestamp"] for r in rows}),
                "csv_quality_flags": "ABSENT",
            })
    return records, manifest


def load_usgs(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    result: dict[str, Any] = {}
    for series in data["value"]["timeSeries"]:
        code = series["variable"]["variableCode"][0]["value"]
        station = series["sourceInfo"]["siteCode"][0]["value"]
        rows = []
        for block in series["values"]:
            for row in block["value"]:
                val = number(row["value"])
                rows.append({"timestamp": datetime.fromisoformat(row["dateTime"]), "value": val, "qualifiers": row.get("qualifiers", [])})
        result[f"{station}:{code}"] = {"rows": rows, "site": series["sourceInfo"], "variable": series["variable"]}
    return result


def match_discharge(archive: list[dict[str, Any]], official: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_time = {r["timestamp"]: r["value"] for r in official if r["value"] is not None}
    result = []
    for hours in (-7, -6, -1, 0, 1, 6, 7):
        deltas = []
        for row in archive:
            shifted = row["timestamp"] + timedelta(hours=hours)
            if row["discharge_cfs"] is not None and shifted in by_time:
                deltas.append(abs(row["discharge_cfs"] - by_time[shifted]))
        result.append({"shift_hours_relative_to_MDT": hours, "matched": len(deltas), "exact_matches": sum(d < 1e-6 for d in deltas), "mean_absolute_error_cfs": statistics.mean(deltas) if deltas else None})
    return result


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    archive, manifest = load_archive()
    official = load_usgs(RAW / "usgs-iv-gallinas-2022.json")
    additional_rain = load_usgs(RAW / "usgs-iv-rainfall-2022.json")
    flow = official["08380500:00060"]["rows"]
    rain = official["08380500:00045"]["rows"]
    evidence = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": manifest,
        "timezone_assessment": {
            "usgs": "EXPLICIT_MDT_UTC_MINUS_06",
            "archive": "INFERRED_MDT_FROM_P2_DISCHARGE_ALIGNMENT; NOT AUTHOR-CONFIRMED",
            "P1_and_P3": "ASSUMED_SHARED_ARCHIVE_TIMEBASE; NOT INDEPENDENTLY_CONFIRMED",
            "matching": match_discharge(archive["P2"], flow),
        },
        "units": {"discharge": "USGS ft3/s; converted using 0.028316846592", "turbidity": "FNU per paper convention; CSV unit metadata absent", "precipitation": "USGS parameter 00045, inches; no inferred I15 or watershed storm total"},
        "additional_paper_rain_gauges_returned": list(additional_rain),
        "p1_cutoff": {"paper_date": "2022-08-28", "archive_last_numeric": manifest["members"]["P1"]["last_numeric_original_time"], "status": "CONFLICT_UNRESOLVED", "damage_description": "Paper retrospectively reports 2022-09-10 damage. No contemporaneous notification is provided."},
        "limitations": [
            "Downloaded archive contains historical research observations, not evidence of a live telemetry feed.",
            "Archive CSVs lack explicit units, timezone and per-reading quality flags; numeric entries are not certified valid measurements.",
            "Different sampling cadences and potentially repeated/interpolated water-quality entries prevent treating row count as independent samples.",
            "P2 archive and currently approved USGS discharge differ; use USGS for reported flow and retain archive differences.",
            "No July 9 rainfall returned from P2 or the eight paper-listed gauges queried. No substitution from the separate debris-flow station.",
            "True historical evidence availability is unknown. Replay packets become available at the end of each window by a simulation convention, not a historical latency claim.",
            "Event selection follows the paper and is not an unbiased performance benchmark.",
        ],
        "events": [],
    }
    packets = []
    export_rows = []
    for date in DATES:
        day = datetime.fromisoformat(date).replace(tzinfo=MDT)
        start, end = day - timedelta(days=1), day + timedelta(days=2)
        item: dict[str, Any] = {"date": date, "window_start_assumed_MDT": start.isoformat(), "window_end_exclusive_assumed_MDT": end.isoformat(), "window_hours": 72, "stations": {}}
        for station, rows in archive.items():
            window = select(rows, start, end)
            item["stations"][station] = {"window_turbidity": stats(window, "turbidity"), "day_turbidity": stats(select(rows, day, day + timedelta(days=1)), "turbidity")}
            for row in window:
                export_rows.append({"event_date": date, "station": station, "source_row": row["source_row"], "original_timestamp": row["original_timestamp"], "timestamp_utc_INFERRED": row["timestamp"].astimezone(timezone.utc).isoformat(), "turbidity_archive": row["turbidity"], "discharge_cfs_archive": row["discharge_cfs"]})
        for label, rows in (("flow_usgs_cfs", flow), ("rain_usgs_inches", rain)):
            item[label] = {"window": stats(select(rows, start, end), "value"), "day": stats(select(rows, day, day + timedelta(days=1)), "value")}
        item["flow_day_peak_m3s"] = item["flow_usgs_cfs"]["day"]["maximum"] * CFS_TO_CMS
        item["flow_window_peak_m3s"] = item["flow_usgs_cfs"]["window"]["maximum"] * CFS_TO_CMS
        item["rainfall_status"] = "NO_RETRIEVED_OBSERVATIONS" if not item["rain_usgs_inches"]["window"]["numeric_entries"] else "POINT_GAUGE_OBSERVATIONS_AVAILABLE"
        evidence["events"].append(item)
        packets.append({
            "event_id": f"gallinas-{date}-72h-v1",
            "evidence_class": "HISTORICAL_ARCHIVE_REPLAY",
            "available_at": end.astimezone(timezone.utc).isoformat(),
            "availability_basis": "SIMULATED: window end; historical publication/telemetry latency unknown",
            "observations_end": end.astimezone(timezone.utc).isoformat(),
            "p2_turbidity_count": item["stations"]["P2"]["window_turbidity"]["numeric_entries"],
            "p1_turbidity_count": item["stations"]["P1"]["window_turbidity"]["numeric_entries"],
            "source_ids": ["zenodo:12764157", "USGS:08380500:00060"] + (
                ["USGS:08380500:00045"]
                if item["rain_usgs_inches"]["window"]["numeric_entries"] else []
            ),
            "rainfall_status": item["rainfall_status"],
            "timezone_status": "ARCHIVE_INFERRED_MDT_NOT_AUTHOR_CONFIRMED",
            "summary": item,
        })
    for name, obj in (("event-audit.json", evidence), ("replay-packets.json", packets)):
        (OUT / name).write_text(json.dumps(obj, indent=2, allow_nan=False), encoding="utf-8")
    with (OUT / "archive-window-rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(export_rows[0]))
        writer.writeheader()
        writer.writerows(export_rows)
    print(json.dumps({"events": [{"date": e["date"], "flow_day_peak_m3s": round(e["flow_day_peak_m3s"], 3), "P2_turbidity_day_peak": e["stations"]["P2"]["day_turbidity"]["maximum"], "P1_entries_72h": e["stations"]["P1"]["window_turbidity"]["numeric_entries"], "P2_entries_72h": e["stations"]["P2"]["window_turbidity"]["numeric_entries"], "rain": e["rainfall_status"]} for e in evidence["events"]], "p1_cutoff": evidence["p1_cutoff"], "archive_rows_exported": len(export_rows)}, indent=2))


if __name__ == "__main__":
    main()
