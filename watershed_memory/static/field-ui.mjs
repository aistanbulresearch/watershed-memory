const ACTION_LABELS = {
  APPROVE: 'Approve field plan',
  MODIFY: 'Modify and approve plan',
  DEFER: 'Defer field plan',
  CANCEL: 'Cancel field plan',
  REPORT: 'Record result',
  CORRECT: 'Correct result',
  ATTACH: 'Attach evidence',
  VERIFY: 'Verify report',
};

const OUTCOME_LABELS = {
  COMPLETE: 'Reported complete',
  PARTIAL: 'Partly completed',
  NOT_DONE: 'Not performed',
};

const LEVEL_LABELS = {
  VERIFIED: 'Verified report',
  EVIDENCE_ATTACHED: 'Evidence attached',
  REPORTED: 'Report saved',
};

function copy(value) {
  return JSON.parse(JSON.stringify(value));
}

function addText(element, helpers, tag, value, className = '') {
  element.append(helpers.element(tag, value == null ? '' : String(value), className));
}

function renderPlan(work, helpers, section) {
  const plan = work.plan;
  addText(section, helpers, 'h3', plan.spec?.purpose || 'Field review plan');
  addText(section, helpers, 'p', plan.spec?.purpose || plan.reason || 'Field review plan.');
  if (plan.location?.label) addText(section, helpers, 'p', `Site: ${plan.location.label}`);
  if (plan.spec?.window_start && plan.spec?.window_end) {
    addText(section, helpers, 'p', `Planned window: ${helpers.date(plan.spec.window_start)} to ${helpers.date(plan.spec.window_end)}`);
  }
  if (plan.spec?.assignee_role) addText(section, helpers, 'p', `Role: ${plan.spec.assignee_role}`);
}

function renderResult(result, helpers, section) {
  if (!result) return;
  const report = result.report;
  if (report?.outcome) addText(section, helpers, 'p', OUTCOME_LABELS[report.outcome] || report.outcome);
  if (result.verification_level) addText(section, helpers, 'p', LEVEL_LABELS[result.verification_level] || result.verification_level);
  if (report?.summary) addText(section, helpers, 'p', report.summary);
  if (report?.revision != null) addText(section, helpers, 'p', `Result revision ${report.revision}`);
  if (result.evidence_count != null) addText(section, helpers, 'p', `${result.evidence_count} evidence item${result.evidence_count === 1 ? '' : 's'}`);
}

function renderWork(work, snapshot, helpers, options, section, earlier = false) {
  const article = helpers.element('article', null, 'work-card');
  if (earlier) addText(article, helpers, 'p', 'Earlier review: the parent review changed before this field record was shown.', 'eyebrow');
  renderPlan(work, helpers, article);
  renderResult(work.result, helpers, article);
  const controls = helpers.element('div', '', 'actions');
  const stableWork = copy(work);
  const caseRevision = Number(snapshot.case_revision);
  for (const action of Array.isArray(work.available_actions) ? work.available_actions : []) {
    if (!ACTION_LABELS[action]) continue;
    const control = helpers.button(ACTION_LABELS[action], () => options.onAction({
      action,
      work: copy(stableWork),
      caseRevision,
    }));
    control.disabled = Boolean(options.blocked);
    controls.append(control);
  }
  const inspect = helpers.button('Inspect field record', () => options.onInspect(stableWork.plan.plan_id), true);
  controls.append(inspect);
  article.append(controls);
  section.append(article);
}

export function renderFieldPanel(snapshot, options) {
  if (!snapshot || !snapshot.current_field_work) return null;
  const helpers = options || {};
  const current = snapshot.current_field_work;
  const section = helpers.element('section', null, 'work-card field-panel');
  addText(section, helpers, 'h2', 'Field work, followed through');
  const plans = Array.isArray(current.current_plans) ? current.current_plans : [];
  const stranded = Array.isArray(current.stranded_plans) ? current.stranded_plans : [];
  const results = Array.isArray(current.latest_results) ? current.latest_results : [];
  if (plans.length) {
    addText(section, helpers, 'h3', 'Current field plans');
    for (const work of plans) renderWork(work, current, helpers, options, section);
  }
  if (stranded.length) {
    addText(section, helpers, 'h3', 'Earlier field reviews');
    for (const work of stranded) renderWork(work, current, helpers, options, section, true);
  }
  if (results.length) {
    addText(section, helpers, 'h3', 'Recent field results');
    for (const work of results) renderWork(work, current, helpers, options, section);
  }

  if (current.state === 'EMPTY' && !plans.length && !stranded.length && !results.length) {
    addText(section, helpers, 'p', 'No field work has been proposed yet.');
  } else if (!plans.length && !stranded.length && !results.length) {
    addText(section, helpers, 'p', 'No active field work or recent results in this view.');
  }
  if (current.has_more_results || current.has_more_stranded_plans) addText(section, helpers, 'p', 'More field history');

  const targets = Array.isArray(current.proposal_targets) ? current.proposal_targets : [];
  for (const target of targets) {
    const stableTarget = copy(target);
    const caseRevision = Number(current.case_revision);
    const control = helpers.button(`Plan field work: ${target.title}`, () => helpers.onAction({
      action: 'PROPOSE',
      target: copy(stableTarget),
      caseRevision,
    }));
    control.disabled = Boolean(helpers.blocked);
    section.append(control);
  }
  return section;
}
