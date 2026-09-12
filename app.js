(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  let data = null;
  let index = 0;
  const human = {OPEN: 'Open', ACKNOWLEDGED: 'Acknowledged', COMPLETED: 'Completed',
    LIMITED: 'Limited', DEGRADED: 'Gap to review', NOT_ASSESSED: 'Not assessed'};
  const node = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value != null) element.textContent = String(value);
    return element;
  };
  const text = (selector, value) => { $(selector).textContent = value ?? '—'; };
  const labels = {
    get_case_context: 'Read the saved case',
    get_observations: 'Read the historical observations',
    propose_review: 'Propose the next review action',
    commit_case_turn: 'Save the case and request receipt',
    strands_turn: 'Record the Strands execution'
  };
  const sourceURL = value => {
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  };
  function renderTabs() {
    const restoreFocus = $('#step-tabs').contains(document.activeElement);
    const buttons = data.steps.map((step, position) => {
      const button = node('button', 'step-tab', `${String(position + 1).padStart(2, '0')}  ${step.title}`);
      button.type = 'button';
      button.classList.toggle('active', position === index);
      button.setAttribute('aria-pressed', String(position === index));
      button.setAttribute('aria-controls', 'snapshot');
      button.addEventListener('click', () => { index = position; render(); });
      return button;
    });
    $('#step-tabs').replaceChildren(...buttons);
    if (restoreFocus) buttons[index].focus({preventScroll: true});
  }
  function renderSnapshot(state) {
    const panel = $('#snapshot');
    const title = node('div');
    title.append(node('p', 'case-reference', state.case?.id),
      node('h3', 'case-title', state.case?.title), node('p', 'case-sub', state.case?.subtitle));
    const head = node('div', 'snapshot-head');
    head.append(title, node('span', 'snapshot-meta', human[state.case?.status] || state.case?.status));
    const signals = node('div', 'fact-row');
    signals.append(
      node('span', 'chip', `Archive coverage: ${human[state.case?.coverage] || state.case?.coverage}`),
      node('span', 'chip', `Water safety: ${human[state.case?.water_safety] || state.case?.water_safety}`)
    );
    panel.replaceChildren(head, signals);
    for (const task of state.tasks || []) {
      const card = node('article', 'task-card');
      card.dataset.review = task.id;
      const status = node('div', 'task-kicker');
      status.append(node('span', 'task-reference', `Review ${task.id}`),
        node('span', 'task-status', human[task.status] || task.status));
      card.append(status, node('h4', '', task.title), node('p', '', task.reason));
      const evidence = node('div', 'fact-row');
      for (const id of task.evidence || []) {
        const event = state.events?.find(item => item.id === id);
        evidence.append(node('span', 'chip active', event?.label || id));
      }
      card.append(evidence);
      for (const response of (state.responses || []).filter(item => item.task_id === task.id)) {
        card.append(node('div', 'response',
          `${response.action === 'acknowledge' ? 'Acknowledgment saved' : 'Review completion saved'} · demonstration operator`));
      }
      panel.append(card);
    }
  }
  function describe(entry) {
    if (entry.tool === 'get_case_context') {
      return `Retrieved ${entry.output.tasks?.length || 0} saved review(s), evidence links and operator response actions.`;
    }
    if (entry.tool === 'get_observations') return `Retrieved source-backed evidence for ${entry.output.summary?.date || entry.input.event_id}.`;
    if (entry.tool === 'propose_review') return entry.output.operation === 'LINK_EVIDENCE'
      ? `Proposed adding evidence to review ${entry.output.task_id}. The case service validates before saving.`
      : 'Proposed a new review supported by the retrieved observations.';
    if (entry.tool === 'commit_case_turn') return (entry.output.changes || []).join(' ');
    return 'SDK, model, timing and token usage recorded after the real turn.';
  }
  function renderExecution(step) {
    const trace = step.snapshot.trace || [];
    const marker = trace.find(entry => entry.tool === 'strands_turn');
    const summary = $('#execution-summary');
    summary.replaceChildren();
    if (marker) {
      const output = marker.output;
      const metadata = marker.input;
      const cloud = output.agentcore || {};
      summary.append(node('p', 'execution-label', 'RECORDED REAL MODEL EXECUTION'));
      const facts = node('dl', 'execution-facts');
      const rows = [
        ['Model', metadata.model_id], ['Strands SDK', metadata.sdk_version],
        ['Model calls', output.model_calls], ['Agent turn', `${output.elapsed_seconds} seconds`],
        ['Total tokens', output.usage?.totalTokens?.toLocaleString('en-US') ?? 'Not reported'],
        ['Cloud session', cloud.runtime_session_alias],
        ['Endpoint', `${cloud.qualifier} · version ${cloud.endpoint_version_verified_before_call}`]
      ];
      for (const [label, value] of rows) facts.append(node('dt', '', label), node('dd', '', value));
      summary.append(facts, node('p', 'source-note', 'Timing covers the agent turn inside the Runtime. The session-stop request was accepted.'));
    } else {
      summary.append(node('p', 'source-note', step.id === 'operator'
        ? 'The operator acknowledgment was saved outside the Runtime. No model call occurs at this recorded moment.'
        : 'July establishes the saved case using the labelled historical rules replay. The following two observation turns use real Strands on AgentCore.'));
    }
    const rows = trace.map(entry => {
      const row = node('article', 'trace-step');
      row.append(node('h4', '', labels[entry.tool] || entry.tool), node('p', '', describe(entry)));
      const details = node('details', 'raw-detail');
      details.append(node('summary', '', 'Recorded tool JSON'),
        node('pre', '', JSON.stringify(entry, null, 2)));
      row.append(details);
      return row;
    });
    $('#trace').replaceChildren(...rows);
  }
  function render() {
    const step = data.steps[index];
    renderTabs();
    text('#step-count', `${index + 1} / ${data.steps.length}`);
    $('#back').disabled = index === 0;
    $('#next').disabled = index === data.steps.length - 1;
    text('#step-label', step.execution_label);
    text('#step-title', step.title);
    text('#step-caption', step.caption);
    text('#state-line', `${step.snapshot.progress.processed} of ${step.snapshot.progress.total} historical observations processed`);
    renderSnapshot(step.snapshot);
    renderExecution(step);
  }
  function renderEvidence() {
    const seen = new Set();
    $('#sources').replaceChildren();
    for (const source of data.steps.flatMap(step => step.snapshot.sources || [])) {
      const url = sourceURL(source.url);
      if (!url || seen.has(url)) continue;
      seen.add(url);
      const link = node('a', 'source-link', source.label);
      link.href = url; link.target = '_blank'; link.rel = 'noreferrer';
      $('#sources').append(link);
    }
    $('#checks').replaceChildren(...Object.entries(data.checks).map(([key, value]) =>
      node('span', 'chip', `${value === true ? '✓' : '—'} ${key.replaceAll('_', ' ')}`)));
    const provenance = $('#provenance');
    provenance.replaceChildren(node('p', 'source-note', `Recorded ${data.recorded_at}`));
    const commit = data.code?.source_commit;
    if (/^[0-9a-f]{40}$/.test(commit || '')) {
      const link = node('a', 'source-link', `Runtime source: ${commit.slice(0, 12)} ↗`);
      link.href = `https://github.com/aistanbulresearch/watershed-memory/tree/${commit}`;
      provenance.append(link);
    }
    provenance.append(node('p', 'source-note hash', `Reviewed artifact SHA-256: ${data.code?.artifact_sha256}`),
      node('p', 'source-note', data.code?.source_comparison),
      node('p', 'source-note', data.publication_notes));
  }
  async function load() {
    try {
      const response = await fetch('./evidence.json', {cache: 'no-store'});
      if (!response.ok) throw new Error('Evidence unavailable');
      const received = await response.json();
      if (received.schema_version !== 1 || received.execution !== 'RECORDED_AGENTCORE_STRANDS'
          || !Array.isArray(received.steps) || received.steps.length !== 4) throw new Error('Invalid recording');
      data = received;
      render();
      renderEvidence();
    } catch {
      data = null;
      $('#back').disabled = true; $('#next').disabled = true;
      $('#step-tabs').replaceChildren();
      text('#step-title', 'The recorded run could not be loaded.');
      text('#step-caption', 'The source and live-workspace instructions are available on GitHub.');
      const link = node('a', '', 'Open the project on GitHub ↗');
      link.href = 'https://github.com/aistanbulresearch/watershed-memory';
      $('#load-message').replaceChildren(link);
    }
  }
  $('#back').addEventListener('click', () => { if (data && index > 0) { index -= 1; render(); } });
  $('#next').addEventListener('click', () => { if (data && index < data.steps.length - 1) { index += 1; render(); } });
  load();
})();
