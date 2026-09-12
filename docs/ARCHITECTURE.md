# One case, successive observations

The source-water case survives individual model calls, browser tabs and server restarts. An agent turn reads that case, selects evidence and proposes permitted work. The service checks the whole turn before saving it.

```mermaid
flowchart LR
    O[Source-water operator] --> UI[Operator workspace]
    UI --> API[Case API]
    API --> Claim[Claim request identity]
    Claim --> Planner{Selected planner}
    Planner --> Rules[Historical rules replay]
    Planner --> Strands[Strands agent / Bedrock]
    Rules --> Tools[Context, observations, review proposals]
    Strands --> Tools
    Archive[Attributed historical packets] --> Tools
    DB[(Durable case and request ledger)] --> Tools
    Tools --> Validate[Validate proposals and recorded evidence]
    Validate --> Commit[Atomic update and request receipt]
    Commit --> DB
    DB --> UI
    UI --> Response[Explicit operator response]
    Response --> DB
```

The default browser experience uses historical rules replay. The Strands adapter runs through the same guarded tools; the explicit live-gate command invokes Bedrock. AgentCore Runtime plus Observability is the deployment target, with case storage external to runtime sessions.

## Decision boundaries

| Boundary | Responsibility |
|---|---|
| Catalog | Pinned source windows, units and provenance; measurements released sequentially |
| Planner | Retrieve context/evidence and explicitly select a task or propose new work |
| Evidence tools | Check references, review kinds, expected coverage and existing targets |
| Case service | Independently revalidate proposals and recorded tool results |
| Request ledger | Claim work before inference; commit state and response receipt atomically |
| Operator | Acknowledge/complete a review with a note, separately from agent work |

## Retry and restart

SQLite stores independent session snapshots, immutable request receipts and a durable per-session execution claim. The claim is acquired before planning, so a repeated concurrent request does not start another model turn. An active claim returns a retryable response; the client keeps the request identity. A commit requires the expected case revision and current claim owner.

A crashed process leaves a claim for up to five minutes. An expired claim can be replaced; the previous owner cannot later commit. Each turn is capped at eight model calls and 120 seconds, with provider timeouts. A committed request receipt prevents repeating the saved action.

The planner receives copies of case state and released packets. Tools stage proposals. The service builds the saved state itself: direct planner mutation, forged targets, missing actions and inconsistent tool results are rejected. Model/tool failure leaves the saved case unchanged.

## Implementation map

| Component | Source |
|---|---|
| Evidence and release boundaries | [catalog.py](../watershed_memory/catalog.py) |
| Tools and review policy | [planning.py](../watershed_memory/planning.py) |
| Strands and inference guards | [strands_agent.py](../watershed_memory/strands_agent.py) |
| Sessions, claims and receipts | [service.py](../watershed_memory/service.py) |
| HTTP and static serving | [api.py](../watershed_memory/api.py) |
| Operator experience | [static](../watershed_memory/static/) |
| Original reconciliation | [reconcile.py](../feasibility/reconcile.py) |
