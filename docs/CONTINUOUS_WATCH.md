# The watershed keeps changing. The record keeps up.

Watershed Memory can collect current rain, flow and turbidity observations from the official USGS Gallinas River station without an operator clicking through events. Each measurement stays linked to its source fetch receipt. New and corrected observations become durable, compact evidence events ready for case assessment.

The collector is a separate current-observation path. The browser walkthrough demonstrates the verified 2022 Strands/AgentCore case journey; the current collector described here performs acquisition and queues evidence without invoking a model.

## Run a bounded watch

From the installed repository, initialize a private local database. Choose a recent UTC start time; older start times are recovered in bounded catch-up windows.

```bash
uv run python -m watershed_memory.watch init --db .local/current-watch.sqlite --monitor gallinas-current --case-id GALLINAS-CURRENT-OBSERVATIONS --start 2026-09-12T13:00:00Z
uv run python -m watershed_memory.watch run --db .local/current-watch.sqlite --monitor gallinas-current --max-polls 2 --max-seconds 400
uv run python -m watershed_memory.watch status --db .local/current-watch.sqlite --monitor gallinas-current
uv run python -m watershed_memory.watch events --db .local/current-watch.sqlite --monitor gallinas-current --limit 5
```

The default Gallinas schedule checks every five minutes and groups evidence into UTC-aligned 15-minute intervals. A two-hour lookback detects late observations and corrections within the revisited window. This setting accounts for a real source check where the latest available measurement was about 50 minutes old; it is configurable with `--overlap-seconds`, and is not a provider freshness guarantee. Repeating initialization with the same configuration preserves the case, schedule and history. A different configuration requires a separate monitor.

The run stops admitting new polls at its time or poll limit. An HTTP request already in progress may finish afterward. Interrupting the process stops further work; its persisted lease prevents a replacement worker from accepting the interrupted worker's late result.

## What the watch preserves

| What happens | What the system does |
|---|---|
| The provider republishes an unchanged measurement with a new identifier | Keeps one semantic observation version and creates no duplicate event. |
| A measurement is corrected | Appends a version and links the revised event to its predecessor. Earlier evidence remains available. |
| An older or ambiguous publication conflicts with current evidence | Rejects the complete poll and records a bounded retry schedule. |
| The source becomes unavailable | Preserves observations and queued work, records the error category and saves the next retry time. |
| Two workers check the same monitor | A persisted lease gives one worker authority to commit. |
| A local write fails midway through an update | Rolls back observations, receipts, event revisions, cursor changes and queued work together. |
| The process restarts | Restores the same case, source progress, pending evidence and schedule. |
| The history or pending queue grows | Reads changed intervals and an ordered, indexed queue; routine status reads use transactional counters. |

Source scanning and observation time have separate cursors. A successful sparse or empty response advances the scan without inventing measurements. An interval that was still open can be sealed by a later successful poll, including one that contains no new measurements.

## Follow the evidence

The source registry fixes the station, series, parameter codes, units and statistics. The HTTP adapter checks every page against the original station and time window, bounds response size and pagination, and rejects incomplete or conflicting batches.

Every observation version references its originating poll. The highest accepted publication authority also retains its own poll reference, even when the value is unchanged. Poll receipts contain the page URL, byte count, retrieval timestamp and SHA-256 digest. Event membership references the exact observation versions used in each summary.

Event summaries preserve decimal values, units, approval states, qualifiers, missing series and null latest measurements. Availability describes the named observation interval. It does not establish water safety, watershed recovery or a failed sensor.

Publication timestamps can exceed request admission by at most 60 seconds to accommodate a bounded clock or publication delay. A later conflicting value requires a strictly newer accepted publication timestamp; a future timestamp cannot indefinitely block legitimate corrections.

## Inspect the implementation

- [Source adapter](../watershed_memory/watch/usgs.py) and [immutable records](../watershed_memory/watch/observations.py)
- [Transactional store](../watershed_memory/watch/store.py), [relational schema](../watershed_memory/watch/store_schema.py) and [revision ingestion](../watershed_memory/watch/store_ingest.py)
- [Bounded event construction](../watershed_memory/watch/store_events.py) and [batch validation](../watershed_memory/watch/store_validation.py)
- [Autonomous runner](../watershed_memory/watch/runner.py) and [command line](../watershed_memory/watch/__main__.py)
- [Source tests](../tests/test_live_sources.py), [durability and growth tests](../tests/test_watch_store.py), [runner tests](../tests/test_watch_runner.py) and [process/CLI tests](../tests/test_watch_cli.py)

The tests include 10,000 accumulated observations, a 2,000-event pending queue, actual SQLite failure injection, concurrent lease claims, late and corrected evidence, source-provenance checks, and separate-process persistence.

USGS station `USGS-08380500` provides the configured discharge (`00060`, `ft^3/s`), precipitation (`00045`, `in`) and turbidity (`63680`, `_FNU`) series. [USGS Water Data API documentation](https://api.waterdata.usgs.gov/docs/ogcapi/).
