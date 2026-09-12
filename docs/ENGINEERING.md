# Follow an observation into lasting work

Start the workspace and bring in July and August. The same review gains evidence. Acknowledge it, bring in September, then complete the monitoring review. The separate P1 evidence-gap review stays open. Reload to restore the saved result.

## Run and inspect

```bash
uv sync --frozen
uv run watershed-memory --port 8765
```

The server binds to loopback and serves only packaged static files. SQLite state lives under `.local/runtime/`; use `--database PATH` for a separate ledger. New replay creates an independent session without deleting previous work. The default workspace makes no AWS calls.

The included historical packets derive from the Gallinas archive and USGS gauge 08380500. Counts describe numeric archive entries, not independent sample counts or safety thresholds. Archive times use the documented MDT interpretation; replay availability is a window-end convention. [Packaged notice](../watershed_memory/data/NOTICE.md).

## Real Strands gate

The live command requires authenticated AWS access, an available Bedrock model and explicit account/region selection. It makes billable calls: three cases by default, capped at eight model calls and 120 seconds per case, with at most 1,000 output tokens per call.

```bash
uv run python feasibility/run_strands.py --profile YOUR_PROFILE --expected-account YOUR_ACCOUNT_ID --region YOUR_REGION --model-id YOUR_BEDROCK_MODEL_ID
```

| Case | Context | Checked outcome |
|---|---|---|
| S1 | New evidence with unfinished work | Select existing task and link August evidence |
| S2 | September with acknowledged review and absent P1 | Keep review, link evidence, open coverage review |
| S3 | Same September evidence after prior completion | Preserve completed work and propose new warranted reviews |

Each run writes a new directory with an input/version manifest, before/after snapshots and results. Success includes repeated-request protection and a fresh-process read. Failures retain observable execution evidence. A scripted model never substitutes for a failed live call.

The agent chooses `get_case_context`, `get_observations` and `propose_review`. Proposals include the current event, a short reason and an explicitly supplied existing task ID, or JSON null when no eligible review exists. Final prose is not a database command. The public evidence contains permitted tool inputs/results, SDK/model identity, call count and aggregate usage; model reasoning text is excluded from the public export.

The operator workspace can use the same live planner, or a pinned AgentCore Runtime endpoint, with an explicit shared turn allowance. [Run the live workspace and inspect its cloud boundary](AGENTCORE.md).

## Meaningful local checks

```bash
uv run pytest -q
node --test tests/request-state.test.mjs
uv run ruff check watershed_memory tests runtime deployment feasibility/run_strands.py feasibility/run_agentcore.py feasibility/export_evidence.py
```

Tests exercise FastAPI, separate sessions, responses, duplicates and concurrent claims. Adversarial planners try direct safety-state mutation, invented coverage gaps and forged tool results; the service rejects them. SDK tests use a labelled scripted provider to exercise the installed Strands protocol: read a task ID from tool output, select it on a later event, handle completed work and enforce a call cap. These validate integration mechanics; the live command supplies model evidence.

JavaScript checks distinguish definitive HTTP rejection from uncertain transport/server failure, normalize validation messages and reconcile exact responses. The interface preserves a pending request's original identity and note while allowing rejected input to be corrected.

## Original data proof

```bash
python -m feasibility.fetch_data
python -m feasibility.reconcile
python -m feasibility.run_proof
python -m unittest discover -s feasibility -p "test_*.py"
```

This downloads about 138 MB of public data and reconstructs the packets. Eight fresh processes exercise the case, evidence links and demonstration responses. Generated `proof.html`, traces, results and database establish the foundation through eleven assertions. [Replay guide](../feasibility/README.md).
