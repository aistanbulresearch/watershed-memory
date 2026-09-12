# A decision the next shift can trust

An agent's recommendation becomes useful when the team can act on it and find it again. Watershed Memory saves the decision, its supporting evidence and the source-delivery acknowledgment together. The next shift sees the same work and the operator's latest plan.

## From a reserved review to saved work

The current delivery journal records an invocation before the model is contacted. That reservation binds the case revision, source interval, coverage policy, relevant earlier measurements and exact model/instruction/SDK profile. New measurements arriving during inference cannot replace the evidence used to validate the result.

The delivery journal checks the returned decision by replaying its recorded tool work against that reserved context. It then saves each new review or evidence link and marks the source interval delivered in one database transaction. An explicit no-follow-up decision is saved too. A failure at any write boundary rolls the transaction back.

If a person changes the plan while the agent is working, the returned result is held as stale. The saved human plan takes precedence. If the process stops before a result is recorded, the reservation remains visible for deliberate reconciliation. Restarting or creating another case does not reset the shared invocation allowance.

## Give attention to changes that matter

The pure attention evaluator identifies five reasons to request a review:

| Reason | What brings the case back to attention |
|---|---|
| Initial review | The case has no assessed baseline yet. |
| Coverage changed | Missing values, source freshness or interval coverage changed. |
| Source correction | A corrected observation supports the assessed baseline or an active plan. |
| Material change | A configured measurement exceeds both the absolute and relative change settings. |
| Check due | An active plan's next check has arrived and that exact revision/check has not been handled. |

Parameter, unit, latest-value or peak metric, and change settings belong to an explicit versioned policy. These settings direct operator review; they are not water-safety limits. Small changes remain measured against the last assessed baseline, so gradual accumulation can still become visible. A correction to older evidence is compared with its direct predecessor.

## Exercise the transaction boundary

```sh
uv run --locked pytest tests/test_current_delivery_store.py tests/test_current_delivery_schema.py tests/test_current_delivery_types.py tests/test_current_attention.py tests/test_current_context_transaction.py -q
```

The tests use temporary source fixtures and synthetic execution records. They exercise simultaneous requests, shared allowances, interrupted attempts, changed human plans, exact duplicate receipts, source acknowledgment and rollback after partial writes. Scripted SDK work belongs to explicitly simulated cases; isolated demonstration cases retain their own work and cannot acknowledge the canonical source queue.

The next integration connects unattended source delivery and due-check scheduling to this transaction boundary. [Current Strands tools](CURRENT_AGENT.md) prepare the decision; the [work ledger](CURRENT_WORK.md) retains human actions; the [continuous watch](CONTINUOUS_WATCH.md) acquires source evidence.

- [Delivery transactions](../watershed_memory/current/delivery_store.py)
- [Invocation and receipt records](../watershed_memory/current/delivery_types.py)
- [Attention evaluator](../watershed_memory/current/attention.py)
- [Transaction challenge tests](../tests/test_current_delivery_store.py)
