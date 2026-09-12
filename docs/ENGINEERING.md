# Follow the work through the system

Start with an observation packet and follow it into a persistent task. The current replay exercises that path across three historical windows and eight fresh processes.

## 1. Bring the evidence together

[fetch_data.py](../feasibility/fetch_data.py) downloads a pinned public archive and official USGS observations. It verifies the archive checksum and writes a source manifest.

[reconcile.py](../feasibility/reconcile.py) extracts the relevant station rows, converts discharge units, compares timestamp alignments and builds replay packets. The export preserves original row numbers and source timestamps. Archive timestamps use an explicitly recorded MDT interpretation; official USGS observations carry their own offsets. The audit retains both event-day and full-window statistics.

## 2. Carry unfinished work forward

[workflow.py](../feasibility/workflow.py) persists five connected records: cases, events, tasks, task evidence and responses. A uniqueness rule prevents multiple unfinished tasks of the same kind. New evidence links to the existing review, while a missing expected monitor can create separate evidence-review work.

The demonstration review policy is named in the event packet. Its job is to organize monitoring review; an operator completes the review as a distinct action.

## 3. Keep retries and timing predictable

Event identifiers are checked against payload hashes. Repeated identical input returns a duplicate result; changed content under the same identifier is rejected. Operator responses have the same protection.

Observation recency and replay availability are separate fields. An older packet arriving later can be recorded without rolling back the current case. Transactions protect state and evidence updates from partial writes.

## 4. Exercise the complete sequence

[run_proof.py](../feasibility/run_proof.py) launches a fresh process for every step:

1. Open the July review.
2. Attach August evidence to that review.
3. Save a demonstration acknowledgment.
4. Attach September evidence and open an evidence-gap review.
5. Complete the monitoring review while the gap remains open.
6. Redeliver the September packet.
7. Retry the completion response.
8. Read the exact saved state from another process.

The runner writes `trace.json`, `summary.json`, `case.sqlite` and a readable `proof.html`. Eleven assertions check the resulting sequence. The input events are historical, packet availability is a replay convention, and human responses are simulated.

## 5. Test the failure paths

The 21 tests cover task continuity, absent evidence, repeated inputs, conflicting identities, future/ambiguous timestamps, delayed observations, operator response validation and transaction rollback. Run them locally with the command in the [README](../README.md).

The next implementation milestone connects this tested foundation to a real Strands tool loop. The product demonstration will show the agent's actual tool calls beside the resulting task update.
