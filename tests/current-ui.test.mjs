import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import { createResponseMachine } from '../watershed_memory/static/current-state.mjs';

class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this._textContent = '';
    this.className = '';
  }
  append(...nodes) { this.children.push(...nodes.filter(Boolean)); }
  get textContent() { return this._textContent + this.children.map(child => child?.textContent || '').join(''); }
  set textContent(value) { this._textContent = String(value); }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  dispatch(name, event = {}) {
    return Promise.all((this.listeners[name] || []).map(fn => fn(event)));
  }
  focus() { this.ownerDocument.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() { this.removed = true; }
  querySelector(selector) {
    if (selector.startsWith('#')) return this.find(node => node.attributes.id === selector.slice(1));
    return this.find(node => node.tagName.toLowerCase() === selector);
  }
  find(predicate) {
    for (const child of this.children) {
      if (!child || typeof child !== 'object') continue;
      if (predicate(child)) return child;
      const found = child.find?.(predicate);
      if (found) return found;
    }
    return null;
  }
  findAll(predicate, result = []) {
    for (const child of this.children) {
      if (!child || typeof child !== 'object') continue;
      if (predicate(child)) result.push(child);
      child.findAll?.(predicate, result);
    }
    return result;
  }
}

function storage() {
  const values = new Map();
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
    values,
  };
}
const now = '2026-09-12T12:00:00Z';
const command = {
  request_id: 'old-request',
  task_id: 'review-1',
  expected_revision: 1,
  action: 'MODIFY',
  note: 'original note',
  title: 'Old title',
  next_check_at: '2026-09-12T11:00:00Z',
};
function futureInput(hours) {
  const value = new Date(Date.now() + hours * 3600000);
  const local = new Date(value.valueOf() - value.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}
function snapshot(title = 'Old title', revision = 1) {
  return {
    evaluated_at: now,
    case: { case_id: 'CASE', revision, simulated: true },
    source: {
      source_id: 'fixture', station_id: 'USGS-00000000', label: 'Gallinas River',
      collected_through: now, next_poll_at: now, observation_count: 1, event_count: 1, pending_count: 0,
      error_code: null, missing_parameters: [], stale_parameters: [], null_parameters: [],
      readings: [{ parameter_code: '00060', label: 'Flow', unit: 'ft3/s', value: '4.2',
        observed_at: now, retrieved_at: now, approval_status: null, flags: [] }],
    },
    interval: null,
    work: [{ task_id: 'review-1', revision, kind: 'OBSERVATION_REVIEW', status: 'PROPOSED',
      title, reason: 'Review the current source interval.', next_check_at: command.next_check_at,
      created_at: now, updated_at: now, evidence_event_ids: [], available_actions: ['MODIFY'] }],
    dimensions: ['physical', 'observation', 'field', 'coverage'].map(id => ({
      id, label: id, status: 'NOT_ASSESSED', detail: 'Fixture',
    })),
    dispatch: null, assessments: [],
  };
}
function confirmed(title, nextCheck) {
  const result = snapshot(title, 2);
  result.work[0].status = 'APPROVED';
  result.work[0].next_check_at = nextCheck;
  return {
    receipt: { task_id: 'review-1', revision: 2, status: 'APPROVED',
      title, next_check_at: nextCheck },
    current_work: { task_id: 'review-1', revision: 2, status: 'APPROVED',
      title, next_check_at: nextCheck },
    snapshot: result,
  };
}
function findText(root, text) {
  return root.findAll(node => node.textContent === text)[0];
}
function harness(responses, saved = storage()) {
  const document = {
    activeElement: null,
    body: new Node('body'),
    createElement: tag => { const node = new Node(tag); node.ownerDocument = document; return node; },
    querySelector: selector => selector === '#app'
      ? document.app : selector === '#mode-badge' ? document.badge : null,
  };
  document.app = document.createElement('main');
  document.badge = document.createElement('span');
  let calls = 0;
  const fetch = async (path, options = {}) => {
    if (path === '/api/current/case') return { ok: true, json: async () => snapshot() };
    calls += 1;
    const sent = JSON.parse(options.body);
    responses.push({ sent, raw: options.body });
    if (calls === 1) return { ok: false, status: 400, json: async () => ({ detail: 'Correct the plan.' }) };
    return { ok: true, json: async () => confirmed(sent.title, sent.next_check_at) };
  };
  const moduleText = fs.readFileSync(new URL('../watershed_memory/static/current-state.mjs', import.meta.url), 'utf8')
    .replaceAll('export ', '');
  const source = fs.readFileSync(new URL('../watershed_memory/static/current.js', import.meta.url), 'utf8')
    .replace(/^import[^\n]+\n/, '');
  const context = vm.createContext({
    console, document, fetch, sessionStorage: saved, location: { origin: 'http://localhost:8771' },
    crypto: { randomUUID: () => 'new-request' }, Date, URL, encodeURIComponent, setTimeout,
  });
  vm.runInContext(moduleText + '\
' + source, context);
  return { document, storage: context.sessionStorage, get calls() { return calls; } };
}
test('restored MODIFY response unlocks after 400 and submits corrected plan with a new identity', async () => {
  const sent = [];
  const seeded = storage();
  const machine = createResponseMachine({ caseId: 'CASE', storage: seeded,
    origin: 'http://localhost:8771', send: async () => { throw new Error('seed only'); } });
  machine.begin(command);
  const ui = harness(sent, seeded);
  await new Promise(resolve => setTimeout(resolve, 20));
  const review = findText(ui.document.app, 'Review saved response');
  assert.ok(review);
  await review.dispatch('click');
  const dialog = ui.document.app.find(node => node.tagName === 'DIALOG');
  assert.ok(dialog);
  const inputs = dialog.findAll(node => node.tagName === 'INPUT');
  const title = inputs[0];
  const due = inputs[1];
  assert.ok(title);
  title.value = 'Confirm the next reading';
  const firstDue = futureInput(48);
  due.value = firstDue;
  const form = dialog.querySelector('form');
  await form.dispatch('submit', { preventDefault() {} });
  assert.equal(sent.length, 1);
  assert.equal(title.readOnly, false);
  title.value = 'Updated field plan';
  const correctedDue = futureInput(72);
  due.value = correctedDue;
  await form.dispatch('submit', { preventDefault() {} });
  assert.equal(sent.length, 2);
  assert.notEqual(sent[0].sent.request_id, sent[1].sent.request_id);
  assert.equal(sent[1].sent.task_id, 'review-1');
  assert.equal(sent[1].sent.expected_revision, 1);
  assert.equal(sent[1].sent.title, 'Updated field plan');
  assert.equal(sent[1].sent.next_check_at, new Date(correctedDue).toISOString());
  assert.equal(ui.storage.values.size, 0);
  assert.match(ui.document.app.textContent, /response is saved|saved/i);
});
