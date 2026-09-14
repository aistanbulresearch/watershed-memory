# The next decision, with the team's plan still in view

The current field desk connects the station's latest collected readings, the team's plan, field results and the next check. An operator can plan an inspection, approve the work, record its outcome and review its evidence in the same durable case.

Source evidence and execution details sit one level deeper. Open a saved assessment to see its model profile, actual tool sequence, decision and linked evidence. Open the plan history to inspect its revision and source references.

## Open an existing current case

### Open the offline judge demonstration

From a fresh clone, create a disposable current field case from the bundled saved USGS fixture:

```sh
uv run python -m watershed_memory.current.demo_seed --output .local/judge-demo
```

The command makes no network, AWS or model calls. It prints the exact loopback launch command. Run that command, then open **http://127.0.0.1:8771/current**. The case is explicitly simulated and opens with one approved visual inspection whose result is **PARTIAL**: the outer demonstration marker was inaccessible, an operator record is attached, and the exact scope is verified. The output directory must be new; the seed never overwrites an existing directory.

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

## Field results carry the next decision forward

The field-enabled service follows an agent's proposed inspection through a human-approved plan, a reported result, supporting evidence and an authorized verification. It also preserves corrections: a later partial result remains visible beside the original verification receipt. A past agent assessment continues to show the exact result and evidence it used at the time.

The screen gives each field plan a purpose, approved site, assigned role and time window. Choose **Modify and approve plan** to approve a revised assignment explicitly. Record **Completed**, **Partly completed** or **Not performed**, then attach evidence to that particular result revision. **Verify report** displays the exact evidence being reviewed before accepting the verification scope.

The service exposes seven typed operations: propose, decide, modify, report, correct, attach evidence and verify. Applications bind an initialized `FieldStore` and a trusted `FieldPrincipal` to `CurrentDesk`; the server supplies the case and operator authority. The browser uses `GET /api/current/field-work/{plan_id}` and `POST /api/current/field-responses` for these controls.

An installed-package HTTP rehearsal exercised a saved USGS case with simulated field work: revise and approve an agent proposal in one human action, report completion, attach an inspection record, then lose the confirmation after verification saved. A later correction changed the result to partial. Retrying the original request returned its original verified receipt alongside that newer result. A separate process recovered the same state without changing any database table or byte; all three earlier assessments retained their original evidence. This rehearsal used local HTTP handlers and made no model or cloud calls.

Field response payloads omit private notes. A malformed command is rejected before a write; a changed request with a reused identity conflicts. A saved result, its attached evidence and its verification remain distinct, so correcting a report requires verification of the new revision.

For an existing field case, the local launcher accepts explicit trusted site files and a local operator identity:

```sh
uv run python -m watershed_memory.current.desk_cli --db .local/current-work.sqlite --case YOUR_EXISTING_CASE_ID --field-location .local/approved-site.json --field-principal-id local-operator --port 8771
```

Each site file contains one canonical `LocationEntry` JSON record; repeat `--field-location` for additional versions. Configure both site files and the operator identity together. The local operator has coordinator, field-operator and verifier permissions within that case. These are trusted launch settings, never browser-supplied roles. A used site version keeps its original meaning across plan history; changed site information requires a new version. Invalid replay databases and conflicting site history are rejected before field-schema initialization.

The installed-browser rehearsal exercised every field operation, including approval, deferral and cancellation. A second browser tab added evidence while a verification form was open; the desk rejected the outdated form and showed the updated record. A later verification saved but lost its confirmation. After a later response corrected the result to partial, reloading and retrying the original response displayed both facts: **the original report was verified; the current result is partly completed and awaits its own evidence review**. The rehearsal used saved USGS observations and simulated field work under one trusted local operator identity, with no new model or cloud calls.

Pending field and source responses share one pause on new actions while inspection stays available. Recent results remain distinct from the **Historical field evidence used by this assessment** panel: a past decision retains its exact report revision and verification basis even after newer results move that work out of the short current list. The saved agent proposal is shown alongside the decision that produced it.

The field browser modules validate all seven operation receipts against the exact submitted plan, report and evidence. Their recovery tests cover temporary HTTP failures, storage failures and a past verification beside a corrected current result. An additional contract test sends eleven actual local HTTP exchanges through those same JavaScript modules, including every operation and all three plan decisions.

## Inspect the engineering

- [Read-only case projection and response reconciliation](../watershed_memory/current/desk.py)
- [Bounded assessment projection](../watershed_memory/current/desk_records.py) and [HTTP routes](../watershed_memory/current/http.py)
- [Browser response state machine](../watershed_memory/static/current-state.mjs) and [operator interface](../watershed_memory/static/current.js)
- [Desk, privacy and revision checks](../tests/test_current_desk.py), [HTTP boundary checks](../tests/test_current_api.py) and [launch checks](../tests/test_current_desk_cli.py)
- [Current field views](../watershed_memory/current/field_desk_records.py), [historical assessment restoration](../watershed_memory/current/field_desk_history.py) and [typed field requests](../watershed_memory/current/field_http_types.py)
- [Field service and retry checks](../tests/test_current_field_desk_responses.py) and [field HTTP checks](../tests/test_current_field_api.py)
- [Field confirmation and retry modules](../watershed_memory/static/field-state.mjs), [strict public records](../watershed_memory/static/field-records.mjs) and [Python-to-browser contract check](../tests/test_current_field_browser_contract.py)
- [Field forms](../watershed_memory/static/field-forms.mjs), [browser controls](../watershed_memory/static/field-controls.mjs), [historical field evidence](../watershed_memory/static/field-history.mjs) and [integrated browser behavior checks](../tests/current-field-ui.test.mjs)

```sh
uv run pytest tests/test_current_desk.py tests/test_current_api.py tests/test_current_desk_cli.py -q
node --test tests/current-state.test.mjs tests/current-ui.test.mjs
node --test tests/field-forms.test.mjs tests/current-field-ui.test.mjs tests/field-history.test.mjs
```
