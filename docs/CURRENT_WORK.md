# The next observation keeps the team's plan in view

The current work ledger connects source evidence to a review that the team owns. An operator can change the plan or defer its next check. When another observation arrives, its evidence can join that same review while the human decision stays intact.

This is the local current-work workflow. The [browser walkthrough](https://aistanbulresearch.github.io/watershed-memory/) shows the historical Strands/AgentCore journey.

| Moment | Saved result |
|---|---|
| A review is proposed against a current source event | A named review with its evidence reference and next check. |
| The operator approves, modifies or defers it | A new human revision; the earlier plan remains in history. |
| Another source interval supports that open review | One additional evidence link; the current plan and schedule stay intact. |
| Someone submits approval for an older revision | The operation is refused, preserving the current plan. |
| The operator dismisses or cancels the review | That review stays closed; later work gets its own identity. |
| A request repeats or the process restarts | Its original receipt and the saved current state remain available. |

The workflow has been rehearsed with two genuinely fetched USGS intervals and explicitly simulated operator actions. Creating a review, modifying its plan and attaching the next interval produced one review, two plan revisions and two evidence links. A separate process recovered the same human plan. The source observation queue remained unchanged during this local work-ledger rehearsal.

## How the record holds together

The case stores the full monitoring coverage policy and its digest. Reusing a policy name with different settings cannot silently change the case. Operational work and isolated demonstrations have separate case identities; shared measurements do not transfer human decisions between them.

Each operation atomically saves its applicable work change, revision or evidence link together with its receipt. A failure rolls them back together. The case revision and recorded time prevent stale or backdated changes. Unique active-review and evidence indexes prevent duplicate work and keep ordinary reads bounded as history grows.

Human notes stay in private receipts. The case projection exposes the structured plan, status, source references and schedule.

## Inspect and run

- [Work transactions](../watershed_memory/current/case_store.py)
- [Command records](../watershed_memory/current/case_types.py) and [relational schema](../watershed_memory/current/case_schema.py)
- [Workflow, concurrency, rollback and isolation tests](../tests/test_current_case_store.py)
- [Current source collection and evidence facts](CONTINUOUS_WATCH.md)

```sh
uv run --locked pytest tests/test_current_case_types.py tests/test_current_case_store.py -q
```

These checks use local source fixtures and temporary databases. They make no model or source-network calls.
