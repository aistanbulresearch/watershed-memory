# Two cloud sessions. One enduring case.

**[Explore the recorded run](https://aistanbulresearch.github.io/watershed-memory/)** · [Inspect its machine-readable evidence](../site/evidence.json)

On September 12, 2026, Strands executed the August and September observation turns on Amazon Bedrock AgentCore. The same source-water review carried forward across two fresh Runtime sessions. The September turn retained the operator acknowledgment and opened a separate review for absent P1 archive evidence.

| Recorded moment | What happened |
|---|---|
| July: rules setup | The historical replay opened review R-01. This setup step makes no model call. |
| August: real cloud agent | Strands read the saved case and observations, then selected R-01 and linked the new evidence. |
| Operator response | A demonstration acknowledgment was saved with R-01. No model wrote that response. |
| September: real cloud agent | A new Runtime session continued R-01 and proposed a separate evidence-gap review, R-02. |

The public recording has four navigable moments. It reads saved results and makes no cloud calls. Run the [operator workspace](../README.md#try-the-operator-workspace) to create your own local case, or select an authenticated [live provider](AGENTCORE.md#run-a-bounded-live-workspace).

## What the execution proved

- **Continuity:** the existing review retained two, then three historical evidence links.
- **Human ownership:** the acknowledgment survived the next model turn.
- **Separate missing-evidence work:** absent P1 numeric entries produced their own review while the main case stayed open.
- **Duplicate suppression:** repeating each completed request returned its saved receipt with no additional planner invocation.
- **Persistence:** a fresh process loaded a final case equal to the saved result.
- **A pinned cloud build:** both turns verified endpoint `proof_v2`, Runtime version 2, before invoking.

The browser also exercised acknowledgment before August. After a bounded failed attempt preserved the case, the reviewed task-selection fix allowed the exact same request to succeed. September then continued that same browser case. Regression tests cover the tool schema and unchanged-state failure response.

## Execution receipt

| | August | September |
|---|---:|---:|
| Model calls | 4 | 4 |
| Agent turn inside Runtime | 4.989 s | 6.255 s |
| Input tokens | 8,885 | 9,164 |
| Output tokens | 552 | 663 |
| Runtime session | 1 | 2 |
| Session-stop request | Accepted | Accepted |

Model: `amazon.nova-pro-v1:0`. Strands SDK: `1.55.1`. Instruction: `watershed-review-v3`. Recorded at `2026-09-12T06:07:24.743923Z`. Timing covers agent execution inside the Runtime, not the full browser round trip. Session labels replace private identifiers; an accepted stop request is the recorded shutdown result.

The reviewed code artifact is tied to source commit [`83b07fc`](https://github.com/aistanbulresearch/watershed-memory/commit/83b07fc1d9286431a1726b2e1f14bc7a198d5c5b). The evidence export records its SHA-256 and checked source fingerprints. The exporter requires exact gate checks and complete attribution, compares Runtime source files with the selected Git commit and strips private identifiers and operator text. [Exporter](../feasibility/export_evidence.py) · [Exporter tests](../tests/test_evidence_export.py).

## Reproduce the cloud gate

After deploying and verifying your own Runtime:

```bash
uv run python feasibility/run_agentcore.py --profile YOUR_PROFILE --expected-account YOUR_ACCOUNT_ID --region us-east-1 --model-id amazon.nova-pro-v1:0 --runtime-arn YOUR_RUNTIME_ARN --runtime-endpoint YOUR_NAMED_ENDPOINT --runtime-version YOUR_VERIFIED_VERSION
```

The gate makes at most two model-bearing turns. Each turn is capped; failure stops the sequence and keeps its evidence. It saves before/after snapshots, request results, an input/source manifest and persistence checks in a new local run directory. [Gate source](../feasibility/run_agentcore.py) · [Gate tests](../tests/test_agentcore_gate.py).

Historical observations come from the attributed Gallinas extract and USGS gauge 08380500. Operator actions and observation availability are demonstration conventions. The case tracks professional review work; it does not issue a water-safety decision. [Data attribution](../watershed_memory/data/NOTICE.md).
