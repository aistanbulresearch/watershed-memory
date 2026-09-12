# The next decision, with the team's plan still in view

The current field desk puts three things together: the station's latest collected readings, the plan the operator is carrying forward, and the next check. The operator can approve, modify, defer, dismiss or cancel work. Each response becomes part of the same durable case.

Source evidence and execution details sit one level deeper. Open a saved assessment to see its model profile, actual tool sequence, decision and linked evidence. Open the plan history to inspect its revision and source references.

## Open an existing current case

Run the desk against a private database that already contains a [current work case](CURRENT_WORK.md). The collector, case and [dispatcher](CURRENT_DISPATCH.md) share that database when configured together.

```sh
uv run python -m watershed_memory.current.desk_cli --db .local/current-work.sqlite --case YOUR_EXISTING_CASE_ID --port 8771
```

Open **http://127.0.0.1:8771/current**. The launcher checks the existing database and case before opening the service. A missing database, unknown case or source-only database is rejected without initializing a new work ledger. Historical replay uses a separate database.

Opening or refreshing the desk reads saved evidence. It does not start collection or invoke a model. The collector and dispatcher keep their explicit schedules and attempt allowances. Collection time, measurement time and the selected source window are shown separately; the last saved check is not a claim that a worker is currently running.

This launch is bound to the local machine. Host, Origin and peer checks protect current actions; the current routes cannot be enabled with the historical app's public-host configuration. The public recorded walkthrough continues to show the verified historical Strands/AgentCore journey.

## A response survives the awkward moments

| What happens | What the operator sees |
|---|---|
| The response saves, but its confirmation is lost | The original response remains saved in that tab, ready for an exact retry after reload. |
| A second operator changes the plan before that retry | Confirmation preserves the original action receipt and displays the newer plan. |
| An open form refers to an older plan revision | The desk refuses the outdated change, refreshes the plan and asks for a new decision. |
| A future check time is rejected | The operator can correct the fields and submit a new response. |
| Required readings are missing or old | The desk shows the missing value or older reading and the coverage state. |
| An agent attempt is awaiting reconciliation | Its held status remains visible; refreshing the screen does not invoke the model again. |

The pending response lives in session storage scoped to the origin, case and browser tab. Private notes remain in private work receipts and the pending local form; they are excluded from case snapshots and assessment details. A pending response is cleared only after a validated confirmation or a definite rejection.

## Four parts of one watershed

Watershed recovery, source-water review, field work and monitoring coverage have separate status cards. The desk derives each from its own recorded evidence and workflow. A station reading does not complete field work, and closing a review does not establish physical recovery.

The first browser rehearsal uses genuinely fetched USGS observations and explicitly simulated operator work, with saved scripted Strands assessments. It exercised real database writes, reload recovery, an intentionally lost confirmation, a later human revision and rejection of an outdated form. The historical provider/AgentCore execution is documented in [the verified run](VERIFIED_RUN.md).

## Inspect the engineering

- [Read-only case projection and response reconciliation](../watershed_memory/current/desk.py)
- [Bounded assessment projection](../watershed_memory/current/desk_records.py) and [HTTP routes](../watershed_memory/current/http.py)
- [Browser response state machine](../watershed_memory/static/current-state.mjs) and [operator interface](../watershed_memory/static/current.js)
- [Desk, privacy and revision checks](../tests/test_current_desk.py), [HTTP boundary checks](../tests/test_current_api.py) and [launch checks](../tests/test_current_desk_cli.py)

```sh
uv run pytest tests/test_current_desk.py tests/test_current_api.py tests/test_current_desk_cli.py -q
node --test tests/current-state.test.mjs tests/current-ui.test.mjs
```
