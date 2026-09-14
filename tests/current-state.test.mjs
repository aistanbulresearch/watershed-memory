import assert from 'node:assert/strict';
import test from 'node:test';
import { createResponseMachine, validSnapshot } from '../watershed_memory/static/current-state.mjs';

const now = '2026-09-12T12:00:00+00:00';
const command = { request_id: 'request-1', task_id: 'review-1', expected_revision: 1,
  action: 'MODIFY', note: 'PRIVATE-BROWSER-NOTE', title: 'Check the next reading', next_check_at: '2026-09-12T13:00:00Z' };
function snapshot() {
  return { evaluated_at: now, case: { case_id: 'CASE', revision: 2, simulated: true },
    source: { source_id: 'fixture', station_id: 'USGS-00000000', label: 'Synthetic test source',
      collected_through: now, next_poll_at: now, observation_count: 0, event_count: 0, pending_count: 0,
      error_code: null, missing_parameters: [], stale_parameters: [], null_parameters: [], readings: [] },
    interval: null, work: [], dispatch: null, assessments: [],
    dimensions: ['physical', 'observation', 'field', 'coverage'].map(id => ({ id, label: id, status: 'NOT_ASSESSED', detail: 'Synthetic fixture only.' })) };
}
function success() {
  const receipt = { task_id: 'review-1', revision: 2, status: 'APPROVED', title: command.title,
    next_check_at: '2026-09-12T13:00:00+00:00' };
  const current = snapshot();
  current.work = [{ ...receipt, kind: 'OBSERVATION_REVIEW', reason: 'Synthetic fixture review.',
    created_at: now, updated_at: now, evidence_event_ids: [], available_actions: ['MODIFY', 'CANCEL'] }];
  return { receipt, current_work: { ...receipt }, snapshot: current };
}
function storage() {
  const items = new Map();
  return { getItem: key => items.get(key) ?? null, setItem: (key, value) => items.set(key, value),
    removeItem: key => items.delete(key), items };
}
function machine(saved, send) {
  return createResponseMachine({ caseId: 'CASE', storage: saved, send, origin: 'http://localhost:8771' });
}

test('an initialized source can have no completed collection', () => {
  const value = snapshot();
  value.source.collected_through = null;
  assert.equal(validSnapshot(value), true);
  value.source.collected_through = 'not a timestamp';
  assert.equal(validSnapshot(value), false);
});

test('a lost confirmation retries byte-identical input after reload', async () => {
  const saved = storage(); const sent = [];
  const first = machine(saved, async bytes => { sent.push(bytes); throw new Error('network lost'); });
  first.begin(command);
  assert.equal((await first.attempt()).kind, 'ambiguous');
  const restored = machine(saved, async bytes => { sent.push(bytes); return success(); });
  assert.deepEqual(restored.pending, command);
  assert.equal((await restored.attempt()).kind, 'success');
  assert.equal(sent[0], sent[1]);
  assert.equal(saved.items.size, 0);
  assert.equal(restored.pending, null);
});

for (const status of [0, 408, 429, 500, 503]) {
  test(`ambiguous or temporary ${status} preserves the original operation`, async () => {
    const saved = storage();
    const active = machine(saved, async () => { throw Object.assign(new Error('failure'), { status }); });
    active.begin(command);
    assert.equal((await active.attempt()).kind, 'ambiguous');
    assert.deepEqual(active.pending, command);
    assert.throws(() => active.begin({ ...command, request_id: 'different' }));
  });
}

for (const status of [400, 404, 409, 422]) {
  test(`definite rejection ${status} releases only the rejected operation`, async () => {
    const saved = storage();
    const active = machine(saved, async () => { throw Object.assign(new Error('rejected'), { status }); });
    active.begin(command);
    assert.equal((await active.attempt()).kind, status === 409 ? 'conflict' : 'rejected');
    assert.equal(active.pending, null);
    assert.equal(saved.items.size, 0);
  });
}

for (const corrupt of [value => ({}), value => ({ receipt: {}, snapshot: {} }),
  value => ({ ...value, receipt: { ...value.receipt, task_id: 'other-review' } }),
  value => ({ ...value, snapshot: { ...value.snapshot, case: { ...value.snapshot.case, case_id: 'OTHER' } } }),
  value => ({ ...value, receipt: { ...value.receipt, revision: 3 } }),
  value => ({ ...value, snapshot: { ...value.snapshot, work: null } })]) {
  test('malformed or mismatched successful HTTP payload cannot clear pending identity', async () => {
    const saved = storage();
    const active = machine(saved, async () => corrupt(success()));
    active.begin(command);
    assert.equal((await active.attempt()).kind, 'ambiguous');
    assert.deepEqual(active.pending, command);
    assert.equal(saved.items.size, 1);
  });
}

test('failed durable storage prevents any submission', () => {
  const saved = storage(); let calls = 0;
  saved.setItem = () => { throw new Error('storage unavailable'); };
  const active = machine(saved, async () => { calls++; });
  assert.throws(() => active.begin(command));
  assert.equal(active.pending, null);
  assert.equal(calls, 0);
});

test('a failed storage cleanup keeps a committed operation retryable', async () => {
  const saved = storage();
  const active = machine(saved, async () => success()); active.begin(command);
  saved.removeItem = () => { throw new Error('storage failed'); };
  assert.equal((await active.attempt()).kind, 'ambiguous');
  assert.deepEqual(active.pending, command);
});

test('parallel submission is rejected while one request is in flight', async () => {
  let release;
  const active = machine(storage(), () => new Promise(resolve => { release = resolve; }));
  active.begin(command); const request = active.attempt();
  assert.equal(active.busy, true);
  await assert.rejects(active.attempt());
  release(success());
  assert.equal((await request).kind, 'success');
  assert.equal(active.busy, false);
});

test('corrupt pending storage is retained and fails closed', () => {
  const saved = storage(); machine(saved, async () => {}).begin(command);
  const key = [...saved.items.keys()][0]; saved.items.set(key, '{broken');
  assert.throws(() => machine(saved, async () => {}));
  assert.equal(saved.items.get(key), '{broken');
});

test('origin and case cannot inherit another desk pending response', () => {
  const saved = storage(); machine(saved, async () => {}).begin(command);
  const other = createResponseMachine({ caseId: 'OTHER', storage: saved, send: async () => {}, origin: 'http://localhost:8771' });
  assert.equal(other.pending, null);
});

test('snapshot rejects unsafe numeric revisions rather than rounding them', () => {
  const valid = snapshot(); assert.equal(validSnapshot(valid), true);
  valid.case.revision = Number.MAX_SAFE_INTEGER + 1;
  assert.equal(validSnapshot(valid), false);
});

test('a valid receipt with a stale plan snapshot cannot clear pending identity', async () => {
  const result = success(); result.snapshot.work[0].revision = 1; result.snapshot.work[0].status = 'PROPOSED';
  const active = machine(storage(), async () => result); active.begin(command);
  assert.equal((await active.attempt()).kind, 'ambiguous');
  assert.deepEqual(active.pending, command);
});

for (const terminal of [false, true]) {
  test(`original receipt remains confirmable after a later ${terminal ? 'cancellation' : 'modification'}`, async () => {
    const result = success(); result.snapshot.case.revision = 3;
    result.current_work = { ...result.current_work, revision: 3,
      title: 'A later human plan', status: terminal ? 'CANCELLED' : 'APPROVED',
      next_check_at: terminal ? null : command.next_check_at };
    result.snapshot.work = terminal ? [] : [{ ...result.snapshot.work[0], ...result.current_work }];
    const active = machine(storage(), async () => result); active.begin(command);
    assert.equal((await active.attempt()).kind, 'success');
    assert.equal(active.pending, null);
  });
}

test('nested approval metadata and oversized evidence IDs are rejected', () => {
  const result = success().snapshot;
  result.source.readings = [{ parameter_code: '00060', label: 'Fixture flow', unit: 'ft^3/s',
    value: null, observed_at: null, retrieved_at: null, flags: ['MISSING'], approval_status: { private: 'CANARY' } }];
  assert.equal(validSnapshot(result), false);
  result.source.readings[0].approval_status = null;
  assert.equal(validSnapshot(result), true);
  result.work[0].evidence_event_ids = ['a'.repeat(10000)];
  assert.equal(validSnapshot(result), false);
});

test('a terminal response cannot be followed by an invented reopened task', async () => {
  const result = success();
  result.receipt = { ...result.receipt, status: 'CANCELLED', next_check_at: null };
  result.current_work.revision = 3; result.snapshot.work[0].revision = 3; result.snapshot.case.revision = 3;
  const active = machine(storage(), async () => result);
  active.begin({ ...command, action: 'CANCEL', title: null, next_check_at: null });
  assert.equal((await active.attempt()).kind, 'ambiguous');
  assert.ok(active.pending);
});

for (const note of ['line one\nline two', 'tab\there', 'zero\u200bwidth']) {
  test('nonprintable notes are rejected before storage or transmission', () => {
    const saved = storage(); let sent = 0;
    const active = machine(saved, async () => { sent++; });
    assert.throws(() => active.begin({ ...command, note }));
    assert.equal(sent, 0); assert.equal(saved.items.size, 0);
  });
}
