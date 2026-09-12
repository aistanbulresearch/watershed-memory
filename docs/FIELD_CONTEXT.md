# The next decision can see what the team actually did

The field-aware context connects a source observation with the team's current assignments and recent field results. It carries the exact report revision, evidence references, verification level and approved place, so a later agent turn can distinguish completed work from an unresolved inspection.

The context, historical reconstruction, field decision tools and persistent proposal delivery are implemented. A validated recommendation becomes a saved proposal that awaits human approval. Field-aware Strands dispatch and browser controls are the next integration steps.

## A useful view of the case

| What the context contains | Why it matters |
|---|---|
| Every actionable field plan, up to two | The next proposal must account for work already assigned under the current reviews. |
| Up to three unfinished plans tied to older or closed reviews | Plans that still block an active task take priority over recent closed-task history. |
| The three most recently updated field results | A follow-up plan does not hide the earlier outcome that motivated it. |
| Up to sixteen currently approved field sites | The agent receives explicit place and activity approval records. |

Truncation flags show when more outdated plans or results exist. The monitoring station remains a source reference; field-site approval is separately attributed. Private operator notes are excluded.

## Three outcomes, three useful decisions

| Saved field result | Supported staged decision |
|---|---|
| Complete, with verified evidence | Remember the exact completed report without proposing more work. |
| Complete, with evidence awaiting verification | Request verification of that exact report. |
| Partial or not done | Propose follow-up under the same active task, citing its latest selected result. |

The tools check the report identity, revision and verification level rather than trusting the recommendation's wording. A supplied follow-up basis must belong to the proposed task and cannot substitute an older partial report for its newer complete result. When no result for that task is selected, a proposal may omit its result basis; the visible truncation flag prevents this from claiming that no older history exists.

An agent must read the source assessment, inspect existing assignments and relevant results, and look up an approved place before staging a proposal. That proposal carries an exact parent task, site revision, activity and bounded future work window. Approval, field reporting and verification remain separate human actions.

Each staged decision has a replayable tool trace. One shared sixteen-attempt budget covers source and field tools, including failed requests. Tests exercise both maximum-length paths and permanent exhaustion; existing source-only tools keep their twelve-attempt limit.

## One decision becomes one saved proposal

The delivery transaction saves the source-review work, one optional field proposal and the finished decision together. When processing a canonical source event, its acknowledgement belongs to that same transaction. An interruption rolls back the finish; a repeated or concurrent delivery returns the original receipt without opening another assignment.

The agent can create an **unapproved proposal**. Only a separate human action can approve the plan or record its result. A later approval changes the current plan while preserving the original agent proposal and the evidence behind it.

If the case changes during the turn, or its chosen site approval or work window is no longer valid, the delivery is marked stale and no proposal or follow-through is saved. Receipt reads check the exact source work, field proposal, acknowledgement and case revision against the recorded decision.

The three-outcome delivery and interruption tests use synthetic execution records with actual tool replay and SQLite transactions. They establish persistent workflow behavior; the current field-aware SDK and provider runs have separate integration gates.

An installed-package rehearsal used the saved USGS case and an explicitly simulated partial field report to save one follow-up proposal. A separate human approval advanced the current plan. After a process restart, the original agent proposal, current human approval, partial result and delivery receipt all matched exactly; retrying the delivery created no duplicate work. The rehearsal used a synthetic execution record and made no model or network calls.

## An earlier decision keeps its original evidence

Capturing a context checks the case revision and the complete selected field view in the same database transaction. Each selected activity is pinned to its saved receipt, exact result and evidence membership, parent review revision, and site record.

A later correction, changed review or withdrawn site affects the current view. It does not rewrite what an earlier reserved turn saw. Old site approval remains historical evidence; new proposals must use current approval.

An installed-package rehearsal demonstrated this on a copy of the saved USGS case. After capturing a partial result, simulated human actions corrected the report and changed the parent review; the rehearsal then supplied a later withdrawn site revision. A separate process recovered both the original context and the changed current view exactly. Source and agent-delivery records remained unchanged, and the rehearsal made no model or network calls.

## Inspect the engineering

- [One source-and-field snapshot](../watershed_memory/current/context_v3.py)
- [Bounded field selection](../watershed_memory/current/field_context.py)
- [Immutable context records](../watershed_memory/current/context_v3_types.py)
- [Exact capture and historical reconstruction](../watershed_memory/current/context_v3_reference.py)
- [Field decision tools and replay](../watershed_memory/current/field_tools.py), [immutable decisions](../watershed_memory/current/field_assessment_types.py), and [shared attempt budget](../watershed_memory/current/field_tool_runtime.py)
- [Three-outcome decision tests](../tests/test_current_field_tools.py) and [maximum-budget tests](../tests/test_current_field_tool_budget.py)
- [Atomic proposal delivery](../watershed_memory/current/field_delivery_store.py), [agent authority boundary](../watershed_memory/current/field_agent.py), and [receipt validation](../watershed_memory/current/field_delivery_records.py)
- [Persisted outcome tests](../tests/test_current_field_delivery_store.py), [rollback and concurrent delivery tests](../tests/test_current_field_delivery_boundaries.py), and [receipt integrity tests](../tests/test_current_field_delivery_integrity.py)
- [Context and restart tests](../tests/test_current_context_v3.py), [record tests](../tests/test_current_context_v3_types.py), and [boundary tests](../tests/test_current_context_v3_boundaries.py)

Existing v1/v2 source contexts retain their saved formats and restoration behavior. New legacy turns on cases containing field work are refused, requiring the field-aware context so prior results cannot silently disappear.
