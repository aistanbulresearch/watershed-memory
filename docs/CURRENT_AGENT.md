# New evidence meets the team's existing plan

A source-water team should not have to reconstruct yesterday's decisions every time another measurement arrives. The current Strands tool layer brings the new interval, the relevant earlier evidence and the team's active plan into one review.

The agent can compare a corrected or earlier measurement, investigate missing coverage through the configured source registry, and prepare a targeted review. When the operator has already changed or deferred the plan, the continuation names that exact review and preserves its schedule.

## Six tools, one continuing case

| Tool | What the agent learns or prepares |
|---|---|
| `get_case_context` | Which case is being reviewed, which work remains active, and which earlier intervals are relevant. |
| `inspect_current_series` | Source, station, time, units, measurements, coverage and freshness for the current interval. |
| `compare_prior_event` | Exact changes against an allowed earlier interval, including direct source corrections. |
| `find_relevant_reviews` | The specific active plan, human revision, next check and supporting evidence for a review kind. |
| `inspect_alternate_sources` | Configured compatible sources, or an explicit result that none is configured. |
| `stage_assessment` | A new review, continuation of an existing review, or an explicit no-follow-up decision. |

At most three earlier intervals enter the context: correction ancestry, evidence tied to active work and the latest earlier interval take priority. Older unrelated history cannot turn every review into a growing prompt dump. Private human-action notes stay in the local ledger.

## A decision carries its supporting tool work

Tools prepare immutable decisions. Before accepting one, the validator repeats the recorded tool operations against the same trusted snapshot and checks their exact outputs, references and target. A changed decision, an unread reference or a different human plan fails validation.

The Strands adapter uses sequential tool execution, explicit nullable fields, disabled automatic retries and bounded inference calls, inference time and tool attempts. An initialized provider and local tool setup precede the inference timer. Successful and failed execution records keep their case, model and execution identity; observable tool work and token counters remain separate from the staged decision.

## Run the current SDK exercise

```sh
uv run --locked pytest tests/test_current_context.py tests/test_current_assessment_types.py tests/test_current_tools.py tests/test_current_strands.py -q
```

These local tests execute the installed Strands SDK with a scripted provider and temporary source fixtures. The paired test supplies the same current interval before and after a simulated operator changes the plan: the fixture reads the actual tool results and targets the saved review. This proves SDK execution and context handling; model decision quality is a separate evaluation.

The current adapter stages work for a future delivery service to validate and persist. The [current work ledger](CURRENT_WORK.md) provides durable human actions and evidence links. The [recorded browser walkthrough](https://aistanbulresearch.github.io/watershed-memory/) shows the separately executed historical Strands and AgentCore journey.

- [Current Strands adapter](../watershed_memory/current/strands.py)
- [Tools and exact trace validation](../watershed_memory/current/tools.py)
- [Trusted context selection](../watershed_memory/current/context.py)
- [Current source acquisition](CONTINUOUS_WATCH.md)
