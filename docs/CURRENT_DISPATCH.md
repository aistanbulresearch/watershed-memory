# Keep watching. Bring back the decision.

Watershed Memory collects observations while the operator is away. Selective dispatch connects that evidence stream to the agent: routine updates stay in the case, an accumulated change can request a review, and the operator's next check comes back at its recorded time.

The comparison remains anchored to the last completed assessment. Three individually small changes can therefore deserve attention together. A quiet update does not move that reference forward and hide the accumulation.

## One case, three useful outcomes

| What arrives | What the watch does |
|---|---|
| Historical backlog at first activation | Starts with the newest sealed interval and retains earlier queued evidence as history. |
| Routine new evidence | Records why no agent invocation was needed and preserves the assessed comparison. |
| A configured change, coverage transition, correction or due check | Reserves one agent invocation against exact evidence and the current human plan. |

Late corrections remain visible. A correction supporting active work is compared with its predecessor; other newly arriving older evidence receives a conservative review. Completing that review preserves the newer main comparison. A due check belongs to an exact task revision and time, so changing the human plan creates a new check identity.

## A decision and its progress commit together

`DispatchStore.prepare` selects evidence, evaluates attention and reserves capacity in one SQLite transaction. The model runs after that transaction ends. `finish` saves the validated work, source acknowledgment, assessed references and handled check identities together. An injected failure rolls all of those completion writes back.

`DispatchRunner` checks the configured model, instruction version and installed SDK before reserving an invocation. Its run has explicit step and time bounds and an interruptible wait. A model call already admitted can finish within its own bounded turn budget; an uncertain completion stops further automatic decisions for reconciliation. Source acquisition runs independently, so it can keep collecting while a decision is held.

If a person changes the plan during inference, the returned assessment remains held as stale and the human plan stays intact. A reserved or failed attempt survives restart for deliberate reconciliation. Another process cannot use that case to start a duplicate invocation, and reopening the database does not reset the shared allowance.

Source delivery has explicit dispositions:

| Disposition | Meaning |
|---|---|
| `PENDING` | Evidence still awaits classification or completion. |
| `HISTORY` | Earlier queued evidence retained when the watch first starts. |
| `SUPPRESSED` | Evidence checked by the configured attention policy; no invocation needed. |
| `DONE` | The agent assessment and delivery acknowledgment committed together. |

Each quiet-source receipt preserves the exact current context, assessed numeric and source-health references, handled checks and attention result. Its explanation can be replayed after newer measurements and human responses arrive. Source observations and their revisions remain in the source store.

When the assessed comparison triggers a change, its comparable source event is included among the agent's bounded prior references. The agent can inspect that exact comparison even after many quiet intervals. A direct correction retains its predecessor too. An activated case rejects journal attempts that bypass its dispatcher records.

## Configure the watch explicitly

Activate a fresh canonical source-origin case with an `AttentionPolicy` and a `watershed-current-v2` invocation profile. Parameter, unit, metric and absolute/relative change settings are explicit review settings, not water-safety thresholds. Activation does not grant or increase model capacity. Scripted SDK execution requires an explicitly simulated case.

The first admission classifies at most 10,000 queued historical events and selects the newest interval. Each later call handles at most one pending source event; when none remain, it checks the latest sealed interval for current publication health and due work. These selections use indexed queries. A quiet clock check writes no receipt.

## Exercise the behavior

```bash
uv run --locked pytest -q tests/test_current_dispatch_store.py tests/test_current_dispatch_edges.py tests/test_current_dispatch_types.py tests/test_current_dispatch_runner.py
```

The fixtures exercise accumulated changes, quiet periods, due checks, source corrections during inference, NULL/recovery transitions, concurrency, rollback, restart, immutable allowances and exact receipt replay. Store tests use synthetic execution envelopes. Runner tests execute the actual Strands SDK with scripted responses and verify that a separate SQLite writer can run during inference. The separately documented cloud run identifies its provider execution mode explicitly.

- [Selective dispatch](../watershed_memory/current/dispatch_store.py)
- [Bounded SDK runner](../watershed_memory/current/dispatch_runner.py)
- [Replayable admission inputs](../watershed_memory/current/dispatch_replay.py)
- [Reserved decision delivery](CURRENT_DELIVERY.md)
- [Latest station evidence](CURRENT_SOURCE_HEALTH.md)
- [Continuous source acquisition](CONTINUOUS_WATCH.md)
