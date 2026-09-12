# From an agreed plan to a result the team can trust

Watershed Memory now keeps the field-work cycle together: the review that prompted an inspection, the approved place and plan, the operator's result, its evidence references, and the person's verification. A later correction changes the current result while preserving the earlier account.

This is the implemented field ledger. The browser desk and the next agent decision are the next integration steps.

| Team action | What the ledger preserves |
|---|---|
| Propose an inspection, sampling visit or maintenance review | The exact parent review and approved site revision, purpose, assigned role, time window and required evidence categories. |
| Approve, modify, defer or cancel | A new attributed plan revision, with the earlier plan retained. |
| Report complete, partial or unperformed work | The approved plan that was actually performed and a separate result revision. |
| Attach an evidence reference | Its category, provenance, observation time and optional SHA-256. |
| Verify the report | The authorized verifier, review scope and exact set of evidence references reviewed. |
| Correct the result | A new report revision that requires its own evidence and verification. |

## The result matters as much as the plan

Reported completion, attached evidence and verified completion are distinct states. Verifying a partial visit keeps it partial. A plan tied to an older or closed review retains that relationship; it cannot silently become the team's current assignment.

Evidence references can point to an HTTPS resource or an externally maintained operator record. The ledger records the reference and its provenance. Verification is an attributed human review of the specified report and reference set; it is not an automated inspection of an uploaded file.

Private command notes stay in the private receipt record. The structured projection contains the plan, outcome, evidence attribution and verification record. Demonstration work carries an explicit simulation flag throughout the cycle.

## Built for interruption and concurrent work

Each change saves its records, receipt and case revision in one database transaction. A stale form cannot overwrite a newer decision. Repeating the same request returns its original receipt, including the exact evidence and verification visible then, even after a later correction.

The database links every plan revision to its parent review, every report to its performed plan, and every receipt to its exact result membership. Read paths check those identities against immutable records. Conflicting writers, interrupted transactions and process restarts are covered by tests.

## Rehearsed from an installed package

A local rehearsal copied the saved case containing genuinely fetched USGS observations. With explicitly simulated field actions, it proposed and modified an inspection, reported completion, attached an operator-record reference, verified the result, and then corrected it to partial work.

A separate process recovered both the current partial result and the earlier verified report. Retrying the earlier verification returned its original receipt without changing the case. All pre-existing source, review, delivery and dispatch tables remained unchanged except the case's revision and update time. The rehearsal made no network or model calls.

The field ledger has **144 focused checks**, including real concurrent writers and rollback at transaction failure points. The complete project suite passed **1,269 tests**; **1,070 tests** also passed against a fresh installed package. Field-aware agent reasoning and browser controls will have their own integration proofs.

## Inspect the engineering

- [Validated commands and trusted roles](../watershed_memory/current/field_types.py)
- [Field transactions](../watershed_memory/current/field_store.py) and [action rules](../watershed_memory/current/field_actions.py)
- [Relational schema](../watershed_memory/current/field_schema.py) and [receipt reconstruction](../watershed_memory/current/field_receipts.py)
- [Workflow tests](../tests/test_current_field_store.py), [transaction tests](../tests/test_current_field_transactions.py), and [integrity tests](../tests/test_current_field_integrity.py)
- [Location approval records](CURRENT_WORK.md#a-named-place-with-a-clear-approval-record)

```sh
uv run --locked pytest tests/test_current_field_*.py -q
```

These checks use temporary local databases and make no provider calls.
