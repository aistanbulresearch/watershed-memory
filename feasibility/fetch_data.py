"""Fetch public inputs; reuse existing files and never execute archive contents."""

from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAW = Path(__file__).resolve().parent / "data/raw"
SOURCES = {
    "zenodo-record.json": "https://zenodo.org/api/records/12764157",
    "HPCC-wildfire-v1.0.1.zip": "https://zenodo.org/api/records/12764157/files/rialgopi/HPCC-wildfire-v1.0.1.zip/content",
    "usgs-iv-gallinas-2022.json": "https://waterservices.usgs.gov/nwis/iv/?format=json&sites=08380500,08380400&startDT=2022-07-08&endDT=2022-09-12&parameterCd=00060,00045&siteStatus=all",
    "usgs-iv-rainfall-2022.json": "https://waterservices.usgs.gov/nwis/iv/?format=json&sites=08379990,08380040,08380070,08380088,08380400,354142105184301,354150105275301,354633105323601&startDT=2022-07-08&endDT=2022-09-12&parameterCd=00045&siteStatus=all",
}


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    manifest_path = RAW / "provenance.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    previous_files = {record["file"]: record for record in previous.get("sources", [])}
    records = []
    for name, url in SOURCES.items():
        target = RAW / name
        reused = target.exists()
        if not reused:
            request = urllib.request.Request(url, headers={"User-Agent": "WatershedMemory-Feasibility/0.1"})
            partial = target.with_suffix(target.suffix + ".partial")
            with urllib.request.urlopen(request, timeout=180) as response, partial.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            partial.replace(target)
        payload = target.read_bytes()
        if name.endswith(".zip") and hashlib.md5(payload).hexdigest() != "42cf095c013aeb3009fa2a773b21f1f9":
            raise ValueError("Pinned archive checksum mismatch")
        if name.endswith(".json"):
            parsed = json.loads(payload.decode("utf-8-sig"))
            if name == "zenodo-record.json" and parsed["id"] != 12764157:
                raise ValueError("Metadata does not describe the pinned archive record")
        old = previous_files.get(name, {})
        acquisition_url = old.get("acquisition_url", old.get("url", url)) if reused else url
        records.append({"file": name, "reproduction_url": url, "acquisition_url": acquisition_url, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "reused_existing_file": reused, "acquisition_method": "PRE_EXISTING_CACHE; original transport not inferred" if reused else "Python urllib.request", "local_file_modified_at_utc": datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).isoformat()})
    manifest = {"verified_at_utc": datetime.now(timezone.utc).isoformat(), "sources": records, "note": "USGS approved historical values can be revised. Reusing these cached inputs reproduces this proof; a fresh download may change values. Reproduction URLs are pinned where possible; acquisition URLs preserve earlier cache provenance."}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
