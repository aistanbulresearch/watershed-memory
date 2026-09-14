# The event under review and the station right now

Watershed Memory keeps two views of the evidence together: the interval that needs review and the station's latest measurements. An older queued interval can be reviewed while the station continues publishing fresh readings.

For each configured USGS series, the latest-source snapshot preserves the measured time, value, unit, approval status and original retrieval provenance. Missing observations, an explicitly NULL latest value, and an aging measurement remain distinct. Republishing an unchanged measurement under a new provider identifier does not make that measurement newer.

## One snapshot throughout a decision

The source-health-enabled Strands workflow reads `inspect_source_health` before staging a decision. Its latest readings are frozen when the invocation is reserved. New arrivals and corrections can continue entering the source store while the agent works; committing the decision replays the original source versions and tool results.

The optional `health_*` journal extension stores those version references in the same transaction as the invocation reservation. Existing interval-only attempts retain their original schemas, request identities and replay behavior. A missing or altered snapshot prevents the result from changing the operator's work or acknowledging the source event.

```python
from watershed_memory.current.context import load_context

context = load_context(
    cases, case_id, event_id,
    evaluated_at=evaluation_time,
    include_source_health=True,
)
```

Use `include_source_health=True` on `DeliveryStore.reserve` to retain this snapshot through the complete [delivery workflow](CURRENT_DELIVERY.md). The planner selects `watershed-current-v2` and exposes seven tools for these contexts; the interval-only workflow retains its six tools and original instruction version. Both retain the same invocation and tool budgets.

Latest-source freshness uses the case's explicit age setting. Interval coverage still uses the full interval and its configured sample minimum. Neither substitutes for a water-safety determination.

## Run the checks

```bash
uv run pytest -q tests/test_current_source_health.py tests/test_current_health_integration.py
```

These checks exercise stale queued evidence beside fresh station readings, NULL and missing values, corrections during a decision, human-plan changes, damaged references, transaction rollback and actual Strands SDK execution with scripted responses. See [the current agent](CURRENT_AGENT.md) and [continuous acquisition](CONTINUOUS_WATCH.md) for the connected components.
