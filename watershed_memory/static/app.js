import {isAmbiguousFailure, errorMessage, responseIsRecorded} from './request-state.mjs';
(() => {
  'use strict';
  const KEY = 'watershed_memory_session_id';
  const $ = (s, root = document) => root.querySelector(s);
  const els = { welcome: $('#welcome'), workspace: $('#workspace'), empty: $('#empty'), app: $('#app'), status: $('#status'), statusText: $('#status-text'), toast: $('#toast') };
  let snapshot = null;
  let toastTimer;
  let advanceRequestId = null;
  const responseRequestIds = new Map();
  const responsePayloads = new Map();
  const pendingResponses = new Map();
  let mutationLock = false;
  let restoreSessionId = null;
  const safe = (value, fallback = '—') => value === null || value === undefined || value === '' ? fallback : String(value);
  const escapeLabel = value => safe(value).replace(/[<>]/g, '');
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);
  function showToast(message) { els.toast.textContent = message; els.toast.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => els.toast.classList.remove('show'), 5000); }
  function status(message, kind = '') { els.status.className = `notice ${kind}`; els.statusText.textContent = message; }
  function busy(message) { clearTimeout(toastTimer); els.toast.textContent = ''; els.toast.classList.remove('show'); els.app.setAttribute('aria-busy', 'true'); status(message, 'pending'); }
  function ready(message, kind = '') { els.app.setAttribute('aria-busy', 'false'); status(message, kind); }
  function lockMutation() { mutationLock = true; document.querySelectorAll('button').forEach(button => { if (!button.closest('.empty-state')) button.disabled = true; }); }
  function unlockMutation() {
    mutationLock = false;
    document.querySelectorAll('button').forEach(button => { button.disabled = false; });
    if (snapshot) $('#advance').disabled = snapshot.progress.processed >= snapshot.progress.total;
    for (const [taskId, pending] of pendingResponses) {
      const save = document.querySelector(`[data-save="${CSS.escape(taskId)}"]`);
      if (!save) continue;
      const form = save.closest('.note-form');
      form.dataset.action = pending.action;
      $('textarea', form).value = pending.note;
      freezePendingForm(taskId, form);
    }
  }
  const humanKind = kind => ({ MONITORING_REVIEW: 'Monitoring review', EVIDENCE_GAP_REVIEW: 'Evidence gap review' }[kind] || 'Watershed review');
  const humanStatus = value => ({ OPEN: 'Open', ACKNOWLEDGED: 'Acknowledged', COMPLETED: 'Completed' }[value] || 'Open');
  const eventLabel = id => (snapshot.events || []).find(event => event.id === id)?.label || id;
  async function request(path, options = {}) {
    const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
    let body = {};
    try { body = await response.json(); } catch (_) {
      if (response.ok) throw new Error('The server response could not be read. Retry to reconcile the action.');
    }
    if (!response.ok) { const error = new Error(errorMessage(body.detail, response.status)); error.status = response.status; throw error; }
    return body;
  }
  function render(snapshotData) {
    snapshot = snapshotData;
    if (!snapshot || !snapshot.case) return showEmpty('The replay returned no case data.');
    try { localStorage.setItem(KEY, snapshot.session_id); } catch (_) { showToast('Browser storage is unavailable; keep this tab open to retain the session.'); }
    els.welcome.classList.add('hidden'); els.empty.classList.add('hidden'); els.workspace.classList.remove('hidden');
    $('#mode-label').textContent = safe(snapshot.mode?.label);
    $('#case-id').textContent = safe(snapshot.case.id); $('#case-title').textContent = safe(snapshot.case.title); $('#case-subtitle').textContent = safe(snapshot.case.subtitle);
    $('#mode-tag').textContent = safe(snapshot.mode?.label, 'Historical observations');
    $('#case-status').textContent = safe(snapshot.case.status).replaceAll('_', ' '); $('#case-coverage').textContent = safe(snapshot.case.coverage).replaceAll('_', ' '); $('#case-safety').textContent = safe(snapshot.case.water_safety).replaceAll('_', ' ');
    $('#brief-headline').textContent = safe(snapshot.latest_brief?.headline); $('#brief-summary').textContent = safe(snapshot.latest_brief?.summary);
    const changes = $('#brief-changes'); changes.replaceChildren();
    (snapshot.latest_brief?.changes || []).forEach(change => { const p = document.createElement('p'); p.className = 'change'; p.textContent = `↳ ${safe(change)}`; changes.appendChild(p); });
    $('#progress-label').textContent = `${snapshot.progress?.processed || 0} OF ${snapshot.progress?.total || 0} PROCESSED`;
    renderTimeline(); renderTasks(); renderSources(); renderTrace();
    const advance = $('#advance'); advance.disabled = (snapshot.progress?.processed || 0) >= (snapshot.progress?.total || 0); advance.textContent = advance.disabled ? 'All observations processed  ✓' : `Process ${safe(snapshot.progress?.next_label, 'next observation')}  →`;
  }
  function renderTimeline() {
    const timeline = $('#timeline'); timeline.replaceChildren();
    (snapshot.events || []).forEach(event => {
      const article = document.createElement('article'); article.className = `event ${event.processed ? 'processed' : ''}`;
      const card = document.createElement('div'); card.className = 'event-card';
      const date = document.createElement('span'); date.className = 'event-date'; date.textContent = safe(event.date); card.appendChild(date);
      const state = document.createElement('span'); state.className = 'event-status'; state.textContent = event.processed ? 'PROCESSED' : 'QUEUED'; card.appendChild(state);
      const heading = document.createElement('h4'); heading.textContent = safe(event.label || event.headline); card.appendChild(heading);
      if (event.processed) {
        const values = document.createElement('div'); values.className = 'event-values';
        [['P1 archive count', event.p1_count], ['P2 archive count', event.p2_count], ['Peak flow', event.flow_peak_cfs == null ? null : `${event.flow_peak_cfs} CFS`]].forEach(([label, value]) => { const wrap = document.createElement('span'); wrap.className = 'value'; wrap.textContent = label; const strong = document.createElement('strong'); strong.textContent = safe(value, '—'); wrap.appendChild(strong); values.appendChild(wrap); });
        card.appendChild(values);
      } else { const pending = document.createElement('div'); pending.className = 'event-values'; pending.textContent = 'Waiting for this observation'; card.appendChild(pending); }
      const marker = document.createElement('span'); marker.className = 'event-marker'; marker.setAttribute('aria-hidden', 'true'); article.append(marker, card); timeline.appendChild(article);
    });
  }
  function renderTasks() {
    const tasks = $('#tasks'); tasks.replaceChildren();
    if (!snapshot.tasks?.length) { const p = document.createElement('p'); p.className = 'microcopy'; p.textContent = 'No open reviews in this snapshot.'; tasks.appendChild(p); return; }
    snapshot.tasks.forEach(task => {
      const item = document.createElement('article'); item.className = `task ${String(task.status || '').toLowerCase()}`;
      const kicker = document.createElement('div'); kicker.className = 'task-kicker'; const kind = document.createElement('span'); kind.textContent = humanKind(task.kind); const statusLabel = document.createElement('span'); statusLabel.textContent = humanStatus(task.status); kicker.append(kind, statusLabel); item.appendChild(kicker);
      const title = document.createElement('h4'); title.textContent = safe(task.title); item.appendChild(title); const reason = document.createElement('p'); reason.textContent = safe(task.reason); item.appendChild(reason);
      const chips = document.createElement('div'); chips.className = 'chips'; (task.evidence || []).forEach(id => { const chip = document.createElement('span'); chip.className = 'chip'; chip.textContent = `historical evidence · ${escapeLabel(eventLabel(id))}`; chips.appendChild(chip); }); item.appendChild(chips);
      const responses = (snapshot.responses || []).filter(response => response.task_id === task.id);
      if (responses.length) { const recorded = document.createElement('p'); recorded.className = 'microcopy'; recorded.textContent = `Response recorded · ${safe(responses[responses.length - 1].note)} · demonstration operator`; item.appendChild(recorded); }
      if (task.status !== 'COMPLETED') {
        const actions = document.createElement('div'); actions.className = 'task-actions';
        const choices = task.status === 'ACKNOWLEDGED' ? [['complete_review', 'Complete review']] : [['acknowledge', 'Acknowledge review'], ['complete_review', 'Complete review']];
        choices.forEach(([action, label]) => { const button = document.createElement('button'); button.className = `task-action ${action === 'complete_review' ? 'complete' : ''}`; button.type = 'button'; button.textContent = label; button.dataset.task = task.id; button.dataset.action = action; actions.appendChild(button); });
        const form = document.createElement('div'); form.className = 'note-form'; const heading = document.createElement('p'); heading.className = 'note-heading'; heading.textContent = 'Choose an action above to record it.'; const textarea = document.createElement('textarea'); textarea.required = true; textarea.maxLength = 1000; textarea.placeholder = 'Record what the team reviewed…'; textarea.setAttribute('aria-label', 'Operator note'); const save = document.createElement('button'); save.className = 'task-action complete'; save.type = 'button'; save.textContent = 'Save selected response'; save.dataset.save = task.id; form.append(heading, textarea, save); item.append(actions, form);
      } else if (!responses.length) { const done = document.createElement('p'); done.className = 'microcopy'; done.textContent = 'Response recorded · demonstration operator'; item.appendChild(done); }
      tasks.appendChild(item);
    });
  }
  function renderSources() { const list = $('#sources'); list.replaceChildren(); (snapshot.sources || []).forEach(source => { const a = document.createElement('a'); a.textContent = safe(source.label); a.href = safe(source.url, '#'); a.target = '_blank'; a.rel = 'noreferrer'; list.appendChild(a); }); }
  function renderTrace() {
    const list = $('#trace'); list.replaceChildren();
    const trace = snapshot.trace || [];
    const execution = trace.find(entry => entry.tool === 'strands_turn');
    if (execution) {
      const receipt = document.createElement('div'); receipt.className = 'execution-receipt';
      const heading = document.createElement('strong'); heading.textContent = 'Recorded agent execution';
      const provider = document.createElement('p');
      provider.textContent = `${safe(execution.input?.model_id)} · Strands ${safe(execution.input?.sdk_version)}`;
      const metrics = document.createElement('p');
      metrics.textContent = `${safe(execution.output?.model_calls)} model calls · ${safe(execution.output?.elapsed_seconds)} s · ${safe(execution.output?.usage?.totalTokens)} tokens`;
      receipt.append(heading, provider, metrics);
      const cloud = execution.output?.agentcore;
      if (cloud) {
        const runtime = document.createElement('p');
        const stop = {STOP_REQUEST_ACCEPTED: 'session stop requested',
          STOP_FAILED: 'session stop request failed', NOT_STARTED: 'session not started'};
        runtime.textContent = `AgentCore · verified Runtime version ${safe(cloud.endpoint_version_verified_before_call)} · ${stop[cloud.stop_status] || 'session stop status unavailable'}`;
        receipt.appendChild(runtime);
      }
      list.appendChild(receipt);
    }
    const labels = {get_case_context: 'Read the saved case', get_observations: 'Read released observations',
      strands_turn: 'Record the model execution', commit_case_turn: 'Save validated work'};
    trace.forEach((entry, index) => {
      const item = document.createElement('div'); item.className = 'trace-item';
      const tool = document.createElement('strong');
      const action = entry.tool === 'propose_review' ? (entry.output?.operation === 'LINK_EVIDENCE'
        ? 'Propose linking evidence to the existing review' : 'Propose a new review') : labels[entry.tool];
      tool.textContent = `${index + 1}. ${safe(action, entry.tool)}`;
      const detail = document.createElement('details'); detail.className = 'trace-detail';
      const summary = document.createElement('summary'); summary.textContent = `${safe(entry.tool)} · inputs & results`;
      const io = document.createElement('pre');
      io.textContent = JSON.stringify({input: entry.input || {}, output: entry.output || {}}, null, 2);
      detail.append(summary, io); item.append(tool, detail); list.appendChild(item);
    });
  }
  function showEmpty(message) { els.welcome.classList.add('hidden'); els.workspace.classList.add('hidden'); els.empty.classList.remove('hidden'); $('#empty-text').textContent = message; els.app.setAttribute('aria-busy', 'false'); }
  function freezePendingForm(taskId, form) { form.classList.add('visible'); $('textarea', form).disabled = true; form.closest('.task').querySelectorAll('button[data-action]').forEach(button => { button.disabled = true; }); $('.note-heading', form).textContent = 'Pending response · reconcile before editing'; const save = $('[data-save]', form); save.textContent = 'Retry / reconcile response'; save.dataset.retry = taskId; }
  function clearPending(taskId) {
    pendingResponses.delete(taskId);
    responseRequestIds.delete(taskId);
    responsePayloads.delete(taskId);
  }
  async function reconcileResponse(taskId) {
    const pending = pendingResponses.get(taskId);
    if (!pending || !snapshot) return;
    const authoritative = await request(`/api/sessions/${encodeURIComponent(snapshot.session_id)}`);
    const found = responseIsRecorded(authoritative.responses || [], pending);
    const task = authoritative.tasks.find(item => item.id === taskId);
    const superseded = !task || task.status === 'COMPLETED' ||
      (pending.action === 'acknowledge' && task.status === 'ACKNOWLEDGED');
    if (found || superseded) clearPending(taskId);
    render(authoritative);
    ready(found ? 'The response was already recorded. The case is reconciled.' :
      superseded ? 'The review changed. The latest saved case is now displayed.' :
      'The response is still pending. Retry with its original note and action.', found ? '' : 'error');
    return found;
  }
  async function start() {
    if (mutationLock) return;
    lockMutation(); busy('Opening the historical replay…');
    try {
      const created = await request('/api/sessions', {method:'POST', body:'{}'});
      pendingResponses.clear(); responsePayloads.clear(); responseRequestIds.clear();
      advanceRequestId = null;
      render(created);
      await advanceUnlocked();
    } catch (error) {
      ready('The replay could not be opened.', 'error'); showToast(error.message);
    } finally { unlockMutation(); }
  }
  async function restore(id) { if (mutationLock) return; restoreSessionId = id; lockMutation(); busy('Restoring the saved case…'); try { render(await request(`/api/sessions/${encodeURIComponent(id)}`)); restoreSessionId = null; unlockMutation(); ready('Saved case restored. The review continues here.'); } catch (error) { if (error.status === 404) { localStorage.removeItem(KEY); restoreSessionId = null; } unlockMutation(); showEmpty(error.status === 404 ? 'That saved replay is no longer available.' : 'We couldn’t restore the saved case.'); $('[data-retry-restore]').classList.toggle('hidden', !restoreSessionId); ready('Restore failed.', 'error'); showToast(`${error.message} Retry when ready.`); } }
  async function advanceUnlocked() {
    busy('Processing the next observation…'); advanceRequestId ||= uuid();
    try {
      const state = await request(`/api/sessions/${encodeURIComponent(snapshot.session_id)}/advance`,
        {method:'POST', body:JSON.stringify({request_id:advanceRequestId})});
      advanceRequestId = null; render(state);
      ready('Observation processed. The case and its open work moved forward.');
    } catch (error) {
      if (!isAmbiguousFailure(error.status)) advanceRequestId = null;
      if (error.status === 409) {
        try { render(await request(`/api/sessions/${encodeURIComponent(snapshot.session_id)}`)); } catch (_) { /* keep retry visible */ }
      }
      ready('The observation could not be confirmed. Retry when ready.', 'error'); showToast(error.message);
    }
  }
  async function advance() {
    if (!snapshot || mutationLock) return;
    lockMutation();
    try { await advanceUnlocked(); } finally { unlockMutation(); }
  }
  function respond(button) {
    if (mutationLock || pendingResponses.has(button.dataset.task)) return;
    const form = $('.note-form', button.closest('.task'));
    form.classList.add('visible'); form.dataset.action = button.dataset.action;
    $('.note-heading', form).textContent = `${button.textContent} · add an operator note`;
    $('[data-save]', form).textContent = `Save ${button.textContent.toLowerCase()}`;
    $('textarea', form).focus();
  }
  async function saveResponse(save) {
    if (mutationLock) return;
    const form = save.closest('.note-form'), textarea = $('textarea', form), taskId = save.dataset.save;
    const pending = pendingResponses.get(taskId);
    const note = pending ? pending.note : textarea.value.trim();
    if (!note || note.length > 1000) {
      textarea.focus(); showToast('Add an operator note of 1–1000 characters.'); return;
    }
    const action = pending ? pending.action : form.dataset.action;
    if (!action) return;
    if (!pending) {
      responseRequestIds.set(taskId, uuid());
      pendingResponses.set(taskId, {taskId, action, note});
    }
    lockMutation(); busy('Saving the demonstration response…');
    try {
      const updated = await request(`/api/sessions/${encodeURIComponent(snapshot.session_id)}/responses`,
        {method:'POST', body:JSON.stringify({request_id:responseRequestIds.get(taskId), task_id:taskId, action, note})});
      clearPending(taskId); render(updated);
      ready('Response saved. The operator record is visible on this case.');
    } catch (error) {
      let confirmed = false;
      if (isAmbiguousFailure(error.status)) {
        try { confirmed = await reconcileResponse(taskId); }
        catch (_) { ready('Response could not be confirmed. Retry the original response before editing.', 'error'); }
      } else {
        clearPending(taskId);
        textarea.disabled = false;
        if (error.status === 409) {
          try { render(await request(`/api/sessions/${encodeURIComponent(snapshot.session_id)}`)); } catch (_) { /* preserve editable form */ }
        }
        ready('The response was rejected. Review the message and correct it.', 'error');
      }
      if (!confirmed) showToast(error.message);
    } finally { unlockMutation(); }
  }
  document.addEventListener('click', event => { const startButton = event.target.closest('[data-start]'); if (startButton) start(); if (event.target.closest('[data-new]')) start(); if (event.target.closest('[data-retry-restore]') && restoreSessionId) restore(restoreSessionId); if (event.target.closest('#advance')) advance(); const action = event.target.closest('button[data-action]'); if (action) respond(action); const save = event.target.closest('[data-save]'); if (save) saveResponse(save); });
  let saved = null; try { saved = localStorage.getItem(KEY); } catch (_) { /* session still works in this tab */ } if (saved) restore(saved); else { els.app.setAttribute('aria-busy', 'false'); }
})();
