import { createResponseMachine, printable, validSnapshot } from './current-state.mjs';
import { createFieldResponseMachine } from './field-state.mjs';
import { renderFieldPanel } from './field-ui.mjs';
import { createFieldControls } from './field-controls.mjs';
import { renderFieldAssessment } from './field-history.mjs';

const app = document.querySelector('#app');
const badge = document.querySelector('#mode-badge');
const state = { data: null, machine: null, fieldMachine: null, fieldLoading: false, fieldReceiptPlanId: null,
  error: null, notice: null, loading: false, dialog: null, storageError: false };
const fieldControls = createFieldControls({state,app,element,button,fact,date: value => date(value),request,render,load,blocked,
  newId: () => crypto.randomUUID()});
const labels = {
  APPROVE: 'Approve', MODIFY: 'Modify plan', DEFER: 'Defer', DISMISS: 'Dismiss', CANCEL: 'Cancel plan',
  PROPOSED: 'Your decision needed', APPROVED: 'Approved plan', DEFERRED: 'Deferred',
  OBSERVATION_REVIEW: 'Observation review', COVERAGE_REVIEW: 'Coverage review',
  NOT_ASSESSED: 'Awaiting assessment', BASELINE_PENDING: 'Baseline pending', MONITORING: 'Monitoring',
  CHANGE_UNDER_REVIEW: 'Change under review', NOT_PLANNED: 'No field work planned',
  SUFFICIENT: 'Required readings available', DEGRADED: 'Coverage needs attention', MISSING: 'Required readings missing',
  COMMITTED: 'Assessment saved', RESERVED: 'Awaiting confirmation', FAILED: 'Needs reconciliation',
  STALE: 'Plan changed during assessment', ABANDONED: 'Reconciled without applying',
  SCRIPTED_SDK: 'Scripted Strands test', STRANDS_CURRENT: 'Strands assessment',
};
const words = value => labels[value] || String(value || 'Not recorded').replaceAll('_', ' ').toLowerCase();
const date = value => value ? new Date(value).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : 'Not recorded';
const localInput = value => {
  const instant = new Date(value);
  return new Date(instant.valueOf() - instant.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};

function element(tag, text = null, className = '') {
  const node = document.createElement(tag);
  if (text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function button(text, handler, secondary = false) {
  const node = element('button', text, `button${secondary ? ' secondary' : ''}`);
  node.type = 'button';
  node.addEventListener('click', handler);
  return node;
}
function fact(label, value) {
  const node = element('div', null, 'fact');
  node.append(element('small', label), element('strong', value ?? 'Not recorded'));
  return node;
}
function panel(title, detail = '') {
  const node = element('section', null, 'panel');
  const heading = element('div', null, 'section-title');
  heading.append(element('h2', title), element('span', detail));
  node.append(heading);
  return node;
}
function disclosure(title) {
  const node = element('details', null, 'technical-disclosure');
  node.append(element('summary', title));
  return node;
}
function blocked() {
  return state.loading || state.fieldLoading || state.error !== null || state.storageError ||
    Boolean(state.machine?.pending || state.fieldMachine?.pending);
}
async function request(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw Object.assign(new Error('Request unavailable.'), { status: response.status });
  if (!payload || typeof payload !== 'object') throw new Error('Unusable response.');
  return payload;
}

function sourcePanel(source) {
  const node = panel('Latest collected data', source.label);
  node.className += ' source-panel';
  const times = element('div', null, 'facts');
  times.append(fact('Collection through', date(source.collected_through)), fact('Next saved check', date(source.next_poll_at)));
  node.append(times);
  if (source.error_code) node.append(element('p', 'The last collection reported a problem. The saved case is still available.', 'notice'));
  if (!source.readings.some(r => r.observed_at)) node.append(element('p', source.collected_through
    ? 'No readings are saved for this collected window.' : 'Collection has not completed yet.', 'empty'));
  const grid = element('div', null, 'reading-grid');
  source.readings.forEach(reading => {
    const card = element('article', null, 'reading');
    const title = element('h3', reading.label);
    if (reading.flags.length) title.append(element('span', reading.flags.map(flag => ({ MISSING: 'No saved reading', NULL: 'No value', STALE: 'Older reading' })[flag]).join(' · '), 'flag'));
    card.append(title, element('div', reading.value ?? '—', 'value'), element('p', reading.unit, 'unit'),
      element('div', `Measured ${date(reading.observed_at)}`, 'meta'), element('div', `Retrieved ${date(reading.retrieved_at)}`, 'meta'));
    if (reading.approval_status) card.append(element('small', reading.approval_status, 'meta'));
    grid.append(card);
  });
  node.append(grid);
  return node;
}

function workPanel() {
  const node = panel('The plan we are carrying forward', `${state.data.work.length} open review${state.data.work.length === 1 ? '' : 's'}`);
  node.className += ' work-panel';
  if (!state.data.work.length) node.append(element('p', 'No open review needs a response right now. Saved assessments remain available below.', 'empty'));
  state.data.work.forEach(task => {
    const card = element('article', null, 'work-card');
    card.append(element('p', `${words(task.kind)} · ${words(task.status)}`, 'eyebrow'),
      element('h3', task.title), element('p', task.reason), fact('Next check', date(task.next_check_at)));
    const actions = element('div', null, 'actions');
    task.available_actions.forEach(action => {
      const control = button(labels[action], () => responseDialog(task, action), action !== 'APPROVE' && action !== 'MODIFY');
      control.disabled = blocked(); actions.append(control);
    });
    card.append(actions);
    const detail = disclosure('Plan history & evidence references');
    detail.append(fact('Plan revision', task.revision), fact('Created', date(task.created_at)),
      fact('Last updated', date(task.updated_at)), fact('Review ID', task.task_id));
    task.evidence_event_ids.forEach(id => detail.append(fact('Linked source interval', id)));
    card.append(detail); node.append(card);
  });
  return node;
}

function technicalPanel() {
  const node = panel('What supports this case', 'Source and execution details');
  node.className += ' evidence-panel';
  const overview = disclosure('Inspect source window & agent execution');
  overview.append(fact('Case ID', state.data.case.case_id), fact('Case revision', state.data.case.revision));
  const interval = state.data.interval;
  if (interval) {
    overview.append(fact('Latest sealed source window', `${date(interval.start)} – ${date(interval.end)}`),
      fact('Window coverage', words(interval.coverage)), fact('Event ID', interval.event_id));
  } else overview.append(element('p', 'No sealed source window is available yet.'));
  const dispatch = state.data.dispatch;
  if (dispatch) {
    overview.append(fact('Execution mode', words(dispatch.execution_mode)), fact('Assessments saved', dispatch.assessment_count),
      fact('Quiet source intervals', dispatch.suppressed_count), fact('Earlier source intervals', dispatch.history_count),
      fact('Remaining configured attempts', dispatch.remaining_attempts), fact('Last assessed health', date(dispatch.health_evaluated_at)));
  } else overview.append(element('p', 'Automatic assessment has not been activated for this case.'));
  node.append(overview);
  if (!state.data.assessments.length) node.append(element('p', 'No assessment receipts have been saved.', 'quiet'));
  state.data.assessments.forEach(item => {
    const detail = disclosure(`${words(item.status)} · ${date(item.evaluated_at)}`);
    detail.append(element('p', `${words(item.mode)} · ${item.source_delivery ? 'Source event' : 'Scheduled check'}`, 'quiet'));
    const content = element('div');
    const control = button('Inspect this assessment', async () => {
      control.disabled = true; content.replaceChildren(element('p', 'Loading the saved evidence…'));
      try {
        const data = await request(`/api/current/assessments/${encodeURIComponent(item.attempt_id)}`);
        if (data.attempt_id !== item.attempt_id || !Array.isArray(data.tool_names) || data.tool_names.length > 12 ||
            !Array.isArray(data.decisions) || data.decisions.length > 2 || !Array.isArray(data.attention_reasons)) throw new Error('Invalid detail');
        content.replaceChildren(fact('Model', data.profile?.model_id), fact('Strands version', data.profile?.sdk_version),
          fact('Instructions', data.profile?.instruction_version), fact('Why this assessment ran', data.attention_reasons.map(words).join(' · ') || 'Explicit review'),
          fact('Tools used', data.tool_names.join(' → ') || 'No completed tools'));
        data.decisions.forEach(decision => {
          const entry = element('article', null, 'decision');
          entry.append(element('h3', words(decision.disposition)), element('p', decision.reason));
          if (decision.title) entry.append(fact('Proposed plan', decision.title));
          if (decision.target_task_id) entry.append(fact('Continuing review', decision.target_task_id));
          if (decision.next_check_at) entry.append(fact('Proposed check', date(decision.next_check_at)));
          (decision.reference_ids || []).forEach(id => entry.append(fact('Compared source interval', id)));
          content.append(entry);
        });
        const historicalField = renderFieldAssessment(data, state.data.case.case_id, state.data.case.simulated, {element,fact,date});
        if (historicalField) content.append(historicalField);
      } catch (_) {
        content.replaceChildren(element('p', 'This saved assessment could not be loaded. Try again shortly.', 'notice'));
        control.disabled = false;
      }
    }, true);
    detail.append(control, content); node.append(detail);
  });
  return node;
}

function render() {
  app.replaceChildren(); app.setAttribute('aria-busy', String(state.loading || state.fieldLoading));
  if (state.error) app.append(element('p', state.error, 'notice'));
  if (state.notice) app.append(element('p', state.notice, 'notice success'));
  const refresh = button(state.loading ? 'Refreshing…' : 'Refresh saved case', load, true);
  refresh.id = 'refresh-case'; refresh.disabled = state.loading || state.fieldLoading || Boolean(state.machine?.busy || state.fieldMachine?.busy);
  app.append(refresh);
  if (!state.data) return;
  badge.textContent = state.data.case.simulated ? 'Simulated operator case · source readings' : 'Current operator case';
  const hero = element('section', null, 'hero');
  const caseHeading = element('h1', state.data.source.label.split(' - USGS ')[0]);
  caseHeading.id = 'case-heading'; caseHeading.setAttribute('tabindex', '-1');
  hero.append(element('p', 'WATERSHED MEMORY / CURRENT FIELD DESK', 'eyebrow'),
    caseHeading, element('p', 'New readings arrive. The work carries forward.', 'lede'));
  app.append(hero);
  if (state.machine?.pending) {
    const notice = element('section', null, 'notice pending-response');
    notice.append(element('h2', 'One response is awaiting confirmation'),
      element('p', 'Your original response is saved in this tab. Recheck it without creating another action.'),
      button('Review saved response', () => responseDialog(null, null, state.machine.pending)));
    app.append(notice);
  }
  if (state.fieldMachine?.pending) {
    const notice = element('section', null, 'notice pending-response');
    notice.append(element('h2', 'One field response is awaiting confirmation'),
      element('p', 'The exact field response is saved in this tab. Review and retry it without creating another action.'),
      button('Review saved field response', () => fieldControls.restore()));
    app.append(notice);
  }
  if (state.fieldReceiptPlanId) app.append(button('Inspect confirmed field record', () => fieldControls.inspect(state.fieldReceiptPlanId), true));
  if (state.storageError) app.append(element('p', 'The saved response store could not be read. Decisions are paused until local storage can be reconciled.', 'notice'));
  if (state.data.dispatch?.active_attempt_id) app.append(element('p', `Agent review: ${words(state.data.dispatch.active_status)}. Its saved attempt is held for reconciliation.`, 'notice'));
  const workspace = element('div', null, 'workspace-grid');
  app.append(workspace); workspace.append(workPanel());
  const field = renderFieldPanel(state.data, {element,button,panel,fact,date,blocked:blocked(),
    onAction: intent => fieldControls.action(intent), onInspect: id => fieldControls.inspect(id)});
  if (field) { field.className += ' field-panel'; workspace.append(field); }
  workspace.append(sourcePanel(state.data.source));
  const dimensions = panel('Four parts of the same watershed');
  dimensions.className += ' status-panel';
  const grid = element('div', null, 'dimension-grid');
  state.data.dimensions.forEach(item => {
    const card = element('article', null, 'dimension');
    card.append(element('h3', item.label), element('strong', words(item.status)), element('p', item.detail)); grid.append(card);
  });
  dimensions.append(grid); workspace.append(dimensions, technicalPanel());
}

function responseDialog(task, action, pending = null) {
  if (state.dialog || state.fieldLoading || state.fieldMachine?.busy || (!pending && blocked())) return;
  const originalFocus = document.activeElement;
  const command = pending;
  const targetId = task?.task_id || command.task_id;
  const targetRevision = task?.revision || command.expected_revision;
  action = command?.action || action;
  const dialog = element('dialog', null, 'drawer');
  dialog.setAttribute('aria-labelledby', 'response-heading'); state.dialog = dialog;
  const card = element('div', null, 'drawer-card');
  const heading = element('h2', command ? 'Confirm the saved response' : labels[action]); heading.id = 'response-heading';
  card.append(heading, element('p', task?.title || command.title || 'Saved operator decision'));
  const form = element('form'); const fields = [];
  function field(labelText, id, tag = 'input') {
    const wrap = element('div', null, 'field'); const label = element('label', labelText); label.htmlFor = id;
    const input = element(tag); input.id = id; wrap.append(label, input); fields.push(input); form.append(wrap); return input;
  }
  let titleInput = null, dueInput = null;
  if (action === 'MODIFY') {
    titleInput = field('Plan title', 'plan-title'); titleInput.maxLength = 120; titleInput.required = true;
    titleInput.value = command?.title || task.title;
  }
  if (['MODIFY', 'DEFER'].includes(action)) {
    dueInput = field('Next check · your local time', 'next-check'); dueInput.type = 'datetime-local'; dueInput.required = true;
    dueInput.value = command ? localInput(command.next_check_at) : '';
    form.append(element('p', 'Choose a future time within the next 30 days.', 'quiet'));
  }
  const note = field('Private note (optional)', 'private-note', 'textarea'); note.maxLength = 1000; note.value = command?.note || '';
  const feedback = element('p', '', 'notice'); feedback.hidden = true; feedback.setAttribute('role', 'alert'); form.append(feedback);
  const controls = element('div', null, 'actions'); const cancel = button('Close', close, true);
  const submit = element('button', command ? 'Retry exact saved response' : 'Save response', 'button'); submit.type = 'submit';
  controls.append(cancel, submit); form.append(controls); card.append(form); dialog.append(card); app.append(dialog);
  function close() {
    if (state.machine?.busy) return;
    dialog.close(); dialog.remove(); state.dialog = null;
    if (state.machine?.pending) render();
    if (originalFocus?.isConnected) originalFocus.focus(); else document.querySelector('#refresh-case')?.focus();
  }
  function freeze(value) { fields.forEach(input => { input.readOnly = value; }); }
  freeze(Boolean(command));
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const controls = [...fields, cancel, submit].filter(control => !control.disabled);
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });
  dialog.showModal(); (command ? submit : fields[0]).focus();
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (state.machine.busy) return;
    feedback.hidden = true; state.notice = null;
    if (!state.machine.pending) {
      if (!printable(note.value)) {
        feedback.textContent = 'Use a single-line note without line breaks or control characters.';
        feedback.hidden = false; return;
      }
      const title = titleInput ? titleInput.value.trim() : null;
      const due = dueInput && dueInput.value ? new Date(dueInput.value) : null; const now = Date.now();
      if ((titleInput && !title) || (dueInput && (!due || !Number.isFinite(due.valueOf()) || due.valueOf() <= now || due.valueOf() - now > 30 * 86400000))) {
        feedback.textContent = 'Enter a plan title and a future check time within 30 days, where required.'; feedback.hidden = false; return;
      }
      try {
        state.machine.begin({ request_id: crypto.randomUUID(), task_id: targetId, expected_revision: targetRevision,
          action, note: note.value, title, next_check_at: due ? due.toISOString() : null });
      } catch (_) {
        feedback.textContent = 'The response could not be saved in this tab. Nothing was submitted.'; feedback.hidden = false; return;
      }
    }
    freeze(true); submit.disabled = true; cancel.disabled = true;
    const outcome = await state.machine.attempt(); submit.disabled = false; cancel.disabled = false;
    submit.focus();
    if (outcome.kind === 'success') {
      state.data = outcome.result.snapshot;
      state.notice = ['DISMISSED', 'CANCELLED'].includes(outcome.result.current_work.status)
        ? 'Your response is saved. The review is closed and its history is retained.'
        : 'Your response is saved. This plan will carry into the next review.';
      state.error = null;
      close(); render(); document.querySelector('#refresh-case')?.focus();
    } else if (outcome.kind === 'conflict') {
      close(); state.notice = 'The plan changed while this response was open. Review the refreshed plan before deciding again.';
      await load(); document.querySelector('#refresh-case')?.focus();
    } else if (outcome.kind === 'rejected') {
      freeze(false); submit.textContent = 'Save corrected response';
      feedback.textContent = 'The action was not accepted. Check the fields and the future check time.'; feedback.hidden = false;
    } else {
      submit.textContent = 'Retry exact saved response'; cancel.textContent = 'Close; keep saved response';
      feedback.textContent = 'Confirmation was interrupted. The original response is retained; retry it exactly or close and return to it.'; feedback.hidden = false;
    }
  });
}

async function load() {
  if (state.loading || state.fieldLoading || state.machine?.busy || state.fieldMachine?.busy || state.dialog) return;
  state.loading = true; render();
  try {
    const data = await request('/api/current/case');
    if (!validSnapshot(data, state.data?.case.case_id)) throw new Error('Invalid snapshot');
    state.data = data; state.error = null;
    if (!state.machine) {
      try {
        state.machine = createResponseMachine({ caseId: data.case.case_id, origin: location.origin, storage: sessionStorage,
          send: bytes => request('/api/current/responses', { method: 'POST', body: bytes }) });
        state.storageError = false;
      } catch (_) { state.storageError = true; }
    }
    if (!state.fieldMachine) {
      try {
        state.fieldMachine = createFieldResponseMachine({caseId:data.case.case_id,origin:location.origin,storage:sessionStorage,
          send: bytes => request('/api/current/field-responses', {method:'POST',body:bytes})});
      } catch (_) { state.storageError = true; }
    }
    state.storageError = !(state.machine && state.fieldMachine);
  } catch (_) { state.error = 'The saved case could not be loaded. Refresh shortly; decisions remain paused until it is available.'; }
  finally { state.loading = false; render(); }
}
load();
