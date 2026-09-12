# Replay three storms through one watershed case

This executable slice brings real Gallinas observations into a persistent review workflow. July opens a review, August attaches new evidence, and September adds a separate evidence-gap task. The case and operator responses persist across eight fresh processes.

## Run from the project root

Python 3.10+; verified on Python 3.14.3 on Windows. The current slice uses the standard library.

```bash
python -m feasibility.fetch_data
python -m feasibility.reconcile
python -m unittest discover -s feasibility -p "test_*.py" -v
python -m feasibility.run_proof
```

The first command downloads approximately 138 MB from Zenodo plus USGS JSON, or reuses existing inputs. The archive checksum is verified before selected CSV members are read. New runs create separate timestamped directories.

Open the generated `proof.html` in the printed run directory. It renders the actual saved task sequence. This is a recorded deterministic workflow; the event inputs are historical and the operator responses and packet availability times are simulation conventions. Strands integration follows this working foundation.

## Inspect the result

| Output | What it contains |
|---|---|
| `data/raw/provenance.json` | Source URLs, hashes and acquisition details |
| `data/derived/event-audit.json` | Station coverage, units and timestamp interpretation |
| `data/derived/archive-window-rows.csv` | Extracted rows with original references |
| `data/derived/replay-packets.json` | Inputs supplied to the task engine |
| `runs/<run-id>/trace.json` | Each action and its saved state |
| `runs/<run-id>/summary.json` | Eleven workflow assertions |
| `runs/<run-id>/case.sqlite` | The persisted case and related records |
| `runs/<run-id>/proof.html` | Readable execution record |

`runs/latest.json` identifies the most recent run. Generated inputs and runs stay outside Git. For a repeatable comparison, retain the same downloaded inputs and source manifest.

## Checks worth inspecting

The tests exercise continued work across events, missing evidence, late older packets, timestamp conflicts, retries and transaction rollback. The replay verifies that completing one review leaves the separate outstanding task visible.

[Implementation tour](../docs/ENGINEERING.md) · [Architecture](../docs/ARCHITECTURE.md) · [Source attribution](../THIRD_PARTY_NOTICES.md)
