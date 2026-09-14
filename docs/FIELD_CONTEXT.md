# The next decision can see what the team actually did

The field-aware context connects a source observation with the team's current assignments and recent field results. It carries the exact report revision, evidence references, verification level and approved place, so a later agent turn can distinguish completed work from an unresolved inspection.

The field-aware Strands workflow connects remembered results to the next saved proposal. It checks the source evidence, retrieves the team's exact field result and prepares permitted follow-up under the existing review. The [operator desk](CURRENT_DESK.md) connects that proposal with human approval, reporting, evidence, verification and later corrections.

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

Each staged decision has a replayable tool trace. Direct source and field tools share a sixteen-attempt budget. The Strands adapter separately admits at most sixteen assembled SDK tool requests, including requests subsequently refused for invalid names or arguments. An oversized batch is refused before any member executes and adds zero admitted requests; a separate failure code records exhaustion. Existing source-only tools keep their twelve-attempt limit.

## From a field result to the next Strands decision

The adapter runs eleven tools with sequential execution and at most eight model responses. It completes the source assessment before opening field context, then inspects the relevant results and approved locations. Strict input checks preserve exact argument types and required explicit nulls before SDK conversion. Opaque provider reasoning signatures stay outside domain arguments and receipts.

The three-result integration tests run the actual Strands SDK with a clearly labelled handwritten model fixture. Six responses read actual tool outputs and select the corresponding next action. A newer result belonging to another review cannot replace the selected review's basis. These tests verify SDK integration and controlled decision behavior; real-provider execution is a separate acceptance step.

The dispatcher reserves the turn, releases its database transaction during inference, and saves the validated result in a new transaction. Independent writer probes succeed during every model response. A quiet check after completion makes no model call. An explicit v2-to-v3 upgrade preserves earlier dispatch history and its original execution profile.

A separate installed-package rehearsal exercised all three outcomes against copies of the same saved USGS case: three scripted SDK turns, eighteen responses and one unapproved follow-up only in the partial-result case. Checks before the scheduled review and after completion stayed quiet. A fresh process reopened every case and recovered both earlier v2 receipts and the new field-aware decision, including its exact report basis. Retrying the finished decision preserved the original receipt and database bytes. Field actions in this rehearsal are simulated; its handwritten model verifies the integration, while real-provider reasoning remains a separate acceptance step.

## One decision becomes one saved proposal

The delivery transaction saves the source-review work, one optional field proposal and the finished decision together. Automatic dispatch also saves its source outcome, comparison baseline and handled check in that transaction. When processing a canonical source event, its acknowledgement belongs to the same transaction. An interruption rolls back the finish; a repeated or concurrent delivery returns the original receipt without opening another assignment.

The agent can create an **unapproved proposal**. Only a separate human action can approve the plan or record its result. A later approval changes the current plan while preserving the original agent proposal and the evidence behind it.

If the case changes during the turn, or its chosen site approval or work window is no longer valid, the delivery is marked stale and no proposal or follow-through is saved. Receipt reads check the exact source work, field proposal, acknowledgement and case revision against the recorded decision.

The delivery and interruption tests use synthetic execution records with actual tool replay and SQLite transactions. The SDK runner tests additionally execute a controlled Strands turn that saves an unapproved field proposal, reads its original receipt and leaves the following quiet check untouched. Receipt reconciliation refuses a source/field context mismatch or a lower-level commit that did not complete its dispatcher bookkeeping.

An installed-package rehearsal used the saved USGS case and an explicitly simulated partial field report to save one follow-up proposal. A separate human approval advanced the current plan. After a process restart, the original agent proposal, current human approval, partial result and delivery receipt all matched exactly; retrying the delivery created no duplicate work. The rehearsal used a synthetic execution record and made no model or network calls.

## An earlier decision keeps its original evidence

Capturing a context checks the case revision and the complete selected field view in the same database transaction. Each selected activity is pinned to its saved receipt, exact result and evidence membership, parent review revision, and site record.

A later correction, changed review or withdrawn site affects the current view. It does not rewrite what an earlier reserved turn saw. Old site approval remains historical evidence; new proposals must use current approval.

An installed-package rehearsal demonstrated this on a copy of the saved USGS case. After capturing a partial result, simulated human actions corrected the report and changed the parent review; the rehearsal then supplied a later withdrawn site revision. A separate process recovered both the original context and the changed current view exactly. Source and agent-delivery records remained unchanged, and the rehearsal made no model or network calls.

## Carry the same case into the agent invocation

The current context has a complete, validated JSON representation: source measurements, selected earlier evidence, field assignments, report revisions and verification records travel together. Exact decimal values and UTC timestamps survive the round trip. Every nested record is checked again, including its field-evidence relationships. Unknown sources, inconsistent summaries and altered verification records are refused.

The planner boundary binds the full model, instruction and SDK profile before dispatch reserves a turn. It distinguishes a validated failed turn from an uncertain invocation, preserving the application's saved attempt instead of silently retrying. The application remains responsible for committing the result.

Local tests reconstruct all three field-result cases from this representation and run them through the actual Strands SDK with the handwritten model fixture. They preserve the exact report basis and leave the source database untouched. This validates the current transport foundation; the deployed AgentCore walkthrough still uses the separately verified historical workflow.

The current request and response protocol carries the saved attempt identity alongside that complete case. A response must match the exact request, model profile and report context, and its tool outputs are replayed against the supplied evidence before acceptance. A different attempt, altered evidence or a fabricated tool output is refused even when the message carries a recomputed checksum. These checks run locally with the actual Strands SDK and the handwritten model fixture; they prepare the current workflow for remote execution.

The current AgentCore connector now carries that reserved case through the runtime HTTP handler and back to local delivery. It verifies the named runtime version before one invocation, reads a bounded complete response, and records each attempt separately. A lost response holds the saved attempt; a human correction during inference makes the returned proposal stale. A validated result survives a session-cleanup or logging failure. The production entrypoint fixes the model and execution mode at startup.

These paths are tested through the actual local AgentCore and Strands SDKs with a handwritten model, plus real loopback HTTP responses through the AWS SDK's `botocore.response.StreamingBody` response-stream type. These local tests make no AWS service calls. The deployed cloud walkthrough continues to identify its separately verified historical workflow.

## Inspect the engineering

- [One source-and-field snapshot](../watershed_memory/current/context_v3.py)
- [Bounded field selection](../watershed_memory/current/field_context.py)
- [Immutable context records](../watershed_memory/current/context_v3_types.py)
- [Complete context transport](../watershed_memory/current/context_wire.py), [source-summary checks](../watershed_memory/current/context_wire_facts.py), and [round-trip and SDK tests](../tests/test_current_context_wire.py)
- [Typed planner boundary](../watershed_memory/current/field_planner.py) and [profile/failure tests](../tests/test_current_field_planner_boundary.py)
- [Reserved invocation](../tests/test_current_reserved_planner.py), [request and response protocol](../watershed_memory/current/remote_protocol.py), [case identity and result replay](../watershed_memory/current/remote_types.py), and [protocol tests](../tests/test_current_remote_protocol.py)
- [Current AgentCore connector](../watershed_memory/current/field_agentcore.py), [runtime handler](../watershed_memory/current/runtime_handler.py), and [production entrypoint](../runtime/current_entrypoint.py)
- [End-to-end SDK delivery tests](../tests/test_current_agentcore_flow.py), [bounded response handling](../watershed_memory/current/agentcore_transport.py), and [native AWS stream tests](../tests/test_current_agentcore_native_stream.py)
- [Exact capture and historical reconstruction](../watershed_memory/current/context_v3_reference.py)
- [Field decision tools and replay](../watershed_memory/current/field_tools.py), [immutable decisions](../watershed_memory/current/field_assessment_types.py), and [shared attempt budget](../watershed_memory/current/field_tool_runtime.py)
- [Three-outcome decision tests](../tests/test_current_field_tools.py) and [maximum-budget tests](../tests/test_current_field_tool_budget.py)
- [Atomic proposal delivery](../watershed_memory/current/field_delivery_store.py), [agent authority boundary](../watershed_memory/current/field_agent.py), and [receipt validation](../watershed_memory/current/field_delivery_records.py)
- [Field-aware Strands adapter](../watershed_memory/current/field_strands.py), [SDK request admission](../watershed_memory/current/field_sdk_budget.py), and [exact argument checks](../watershed_memory/current/field_sdk_requests.py)
- [Automatic field dispatch](../watershed_memory/current/field_dispatch_store.py), [bounded runner](../watershed_memory/current/field_dispatch_runner.py), and [actual SDK runner tests](../tests/test_current_field_dispatch_runner.py)
- [Paired SDK decisions](../tests/test_current_field_strands.py), [explicit model fixture](../tests/test_current_field_sdk_fixture.py), and [dispatch integrity tests](../tests/test_current_field_dispatch_integrity.py)
- [Persisted outcome tests](../tests/test_current_field_delivery_store.py), [rollback and concurrent delivery tests](../tests/test_current_field_delivery_boundaries.py), and [receipt integrity tests](../tests/test_current_field_delivery_integrity.py)
- [Context and restart tests](../tests/test_current_context_v3.py), [record tests](../tests/test_current_context_v3_types.py), and [boundary tests](../tests/test_current_context_v3_boundaries.py)

Existing v1/v2 source contexts retain their saved formats and restoration behavior. New legacy turns on cases containing field work are refused, requiring the field-aware context so prior results cannot silently disappear.
