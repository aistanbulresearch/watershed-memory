# Verified report. Unfinished work. A new plan.

On September 14, 2026, Watershed Memory completed the field-aware current-v3
agent turn twice: directly through Amazon Bedrock and through Amazon Bedrock
AgentCore Runtime. Both real-model executions reached the same operational
result. A verified field report said that the assigned inspection was only
partial, so the agent continued the existing source-water review and proposed
a follow-up visual inspection for a person to approve.

The proposal remained **PROPOSED**. It was not approved or carried out. That
boundary is part of the product: the agent connects evidence to the next plan;
a person owns approval, field reporting and verification.

## What the agent saw

The saved case contained 52 actual observations from USGS station
`USGS-08380500`, observed on September 12. A simulated human workflow added a
field report with outcome `PARTIAL` and verification level `VERIFIED`.
Verification confirms the report's stated scope; it does not turn partial work
into completed work.

The scenario used an accelerated case clock. The proposed September 13 work
window is relative to that saved case, while the real model executions occurred
on September 14.

## What the agent did

The Strands agent followed ten named tool steps in both executions:

1. Read the case context, current series and source health.
2. Compared prior evidence and found the relevant review.
3. Staged the source assessment.
4. Read the field context and inspected the existing field work.
5. Looked up an approved location for the activity.
6. Staged `PROPOSE_FIELD_PLAN` under the existing review.

The domain tools checked the exact review, report revision, verification level,
site approval and future work window before the proposal could commit. The
model could stage a proposal; it could not approve one.

| Execution | Result | Model responses | Tool attempts | Recorded model time | Independent checks |
|---|---:|---:|---:|---:|---:|
| `REAL_BEDROCK_STRANDS` | `COMMITTED` | 11 | 10 | 39.469 s | 9/9 |
| `REAL_AGENTCORE_STRANDS` | `COMMITTED` | 10 | 10 | 39.157 s | 9/9 |

These are recorded execution times for the two accepted runs, rather than a
latency benchmark. Both checks confirmed one provider slot, one saved attempt,
unchanged readings, receipt replay, and an unapproved agent proposal. The
AgentCore result was validated against Runtime version `1` and endpoint
qualifier `current_v3`; session stop returned `STOP_REQUEST_ACCEPTED`.

## Bound execution

The accepted profile used Strands Agents SDK `1.55.1` with
`us.amazon.nova-2-lite-v1:0`, low reasoning effort and a 5,000-token output
limit. The field-aware turn allows at most 12 model responses, 16 assembled
tool requests and 120 seconds, with one provider execution slot for the saved
branch.

The source was commit
`f55c23f23067737038e72d8b953f562c000c673c`. The content-addressed Runtime
artifact has SHA-256
`acc142da8c48feb6b30bde5509988f266652ae7b9f81e2f32d8c3b1d9c37aadb`
and contains 125 application files plus 5,540 dependency files. Its installed
package suite passed 216 tests before execution.

No source request occurred during either model turn: the agent reasoned over
the immutable case context derived from the 52 saved observations. Durable case
history and receipts live in the application's external SQLite case ledger,
not in native AgentCore Memory.

Inspect the machine-readable [public evidence](evidence/current-agentcore-run.json),
the [current Runtime path](CURRENT_RUNTIME.md), and the
[field-context contract](FIELD_CONTEXT.md).
