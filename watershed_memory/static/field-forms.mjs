import {validFieldCommand} from './field-records.mjs';
import {validFieldDetail} from './field-detail.mjs';
import {validSnapshot} from './current-state.mjs';

const ACTIVITIES = ['VISUAL_INSPECTION', 'SAMPLING', 'MAINTENANCE_REVIEW'];
const CATEGORIES = ['PHOTO_REFERENCE', 'SAMPLE_RECORD_REFERENCE', 'INSPECTION_RECORD_REFERENCE', 'MAINTENANCE_RECORD_REFERENCE'];
const OUTCOMES = ['COMPLETE', 'PARTIAL', 'NOT_DONE'];
const LABELS={VISUAL_INSPECTION:'Visual inspection',SAMPLING:'Sampling',MAINTENANCE_REVIEW:'Maintenance review',
  PHOTO_REFERENCE:'Photo reference',SAMPLE_RECORD_REFERENCE:'Sample record',INSPECTION_RECORD_REFERENCE:'Inspection record',
  MAINTENANCE_RECORD_REFERENCE:'Maintenance record',COMPLETE:'Completed',PARTIAL:'Partly completed',NOT_DONE:'Not performed',
  OPERATOR_RECORD_REFERENCE:'Operator record',EXTERNAL_REFERENCE:'External HTTPS reference'};
const ACTIONS = ['APPROVE', 'MODIFY', 'DEFER', 'CANCEL', 'REPORT', 'CORRECT', 'ATTACH', 'VERIFY'];

const copy = value => JSON.parse(JSON.stringify(value));
const instant = value => Date.parse(value);
const localValue = value => {
  const date = new Date(value);
  return new Date(date.valueOf() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 23);
};
const isoValue = value => new Date(value).toISOString();
const safeText = value => typeof value === 'string' && value.length >= 1 && value.length <= 1000 && !/[\u0000-\u001f\u007f]/u.test(value);
const id = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function addField(form, fields, helpers, labelText, idSuffix, type = 'text', value = '') {
  const idValue = `field-${idSuffix}`;
  const label = helpers.element('label', labelText);
  label.htmlFor = idValue;
  const input = helpers.element(type === 'select' ? 'select' : 'input');
  input.id = idValue;
  if (type !== 'select') input.type = type;
  input.value = value;
  input.name = idSuffix;
  if(type==='datetime-local')input.step='0.001';
  input.required=!['observed-at','sha256'].includes(idSuffix) && type!=='checkbox';
  const wrap=helpers.element('div',null,type==='checkbox'?'field field-checkbox':'field');
  wrap.append(label,input);form.append(wrap);
  fields.push(input);
  return input;
}

function addSelect(form, fields, helpers, labelText, idSuffix, values, value = '') {
  const input = addField(form, fields, helpers, labelText, idSuffix, 'select', value);
  for (const optionValue of values) {
    const option = helpers.element('option', optionValue ? LABELS[optionValue] || optionValue : 'Choose an option');
    option.value = optionValue;
    input.append(option);
  }
  input.value = value;
  return input;
}

function requireWork(intent) {
  if (!ACTIONS.includes(intent.action) || !intent.work || !Array.isArray(intent.work.available_actions) || !intent.work.available_actions.includes(intent.action)) {
    throw new Error('This field action is no longer available. Refresh the current record.');
  }
  return copy(intent.work);
}

export function createFieldForm(intent, snapshot, {element, date, now = () => Date.now()}) {
  if (!intent || !snapshot || !snapshot.case || !element || typeof date !== 'function') throw new Error('Invalid field form inputs.');
  if (!Number.isSafeInteger(intent.caseRevision) || snapshot.case.revision !== intent.caseRevision) throw new Error('The displayed case has changed. Refresh before editing.');
  const action = intent.action;
  const caseRevision = intent.caseRevision;
  const current = snapshot.current_field_work;
  if (!current) throw new Error('Current field work is unavailable.');
  const work = action === 'PROPOSE' ? null : requireWork(intent);
  const target = action === 'PROPOSE' ? copy(intent.target) : null;
  if (action === 'PROPOSE' ? !validSnapshot(snapshot, snapshot.case.case_id) : !validFieldDetail({snapshot, selected_field_work: work}, snapshot.case.case_id, work.plan.plan_id)) {
    throw new Error('The displayed field record is no longer valid. Refresh before editing.');
  }
  if (action === 'PROPOSE') {
    if (!target || !Array.isArray(current.proposal_targets) || !current.proposal_targets.some(item => same(item, target))) throw new Error('Choose a field plan supplied by the server.');
  }
  const form = element('form');
  const fields = [];
  const intro = action === 'MODIFY' ? 'Submitting this revised form approves the changed work.' : 'Review the displayed field record before submitting.';
  form.append(element('p', intro));
  if(target)form.append(element('p',target.title));
  if (work?.plan?.spec?.purpose) form.append(element('p', work.plan.spec.purpose));
  if (work?.plan?.location?.label) form.append(element('p', work.plan.location.label));
  if (work?.plan?.spec?.window_start && ['APPROVE', 'DEFER', 'CANCEL','REPORT'].includes(action)) form.append(element('p', `Planned window: ${date(work.plan.spec.window_start)} to ${date(work.plan.spec.window_end)}`));
  if(work?.result)form.append(element('p',`Report revision ${work.result.report.revision}: ${LABELS[work.result.report.outcome]}.`),element('p',work.result.report.summary));
  if (action === 'CORRECT') form.append(element('p', 'A corrected result needs its own evidence or verification review.'));
  const locations = Array.isArray(current.approved_locations) ? copy(current.approved_locations) : [];
  let activity; let location;
  let buildPlan;
  const plan = work?.plan;
  const existingSpec = plan?.spec;
  const selectedActivity = existingSpec?.activity || '';
  if (action === 'PROPOSE' || action === 'MODIFY') {
    const supportedActivities = [...new Set(locations.flatMap(site => site.activities))].filter(value => ACTIVITIES.includes(value));
    activity = addSelect(form, fields, {element}, 'Activity', 'activity', ['', ...supportedActivities], selectedActivity);
    const siteChoices = () => locations.filter(site => site.activities.includes(activity.value));
    location = addSelect(form, fields, {element}, 'Approved site', 'location', [], plan?.location.location_id || '');
    const syncSites = (preferred=location.value) => {
      const previous=preferred;
      const choices = ['', ...siteChoices().map(site => site.location_id)];
      location.replaceChildren?.();
      for (const optionValue of choices) {
        const site = locations.find(item => item.location_id === optionValue);
        const option = element('option', site?.label || 'Choose an approved site');
        option.value = optionValue;
        location.append(option);
      }
      location.value=choices.includes(previous)?previous:'';
    };
    activity.addEventListener('change', () => syncSites());
    syncSites(plan?.location.location_id || '');
    const purpose = addField(form, fields, {element}, 'Purpose', 'purpose', 'text', existingSpec?.purpose || '');
    const assignee = addField(form, fields, {element}, 'Assigned role', 'assignee', 'text', existingSpec?.assignee_role || '');
    const start = addField(form, fields, {element}, 'Window start (local time)', 'window-start', 'datetime-local', existingSpec ? localValue(existingSpec.window_start) : '');
    const end = addField(form, fields, {element}, 'Window end (local time)', 'window-end', 'datetime-local', existingSpec ? localValue(existingSpec.window_end) : '');
    const evidence = {};
    for (const category of CATEGORIES) {
      evidence[category] = addField(form, fields, {element}, `Required evidence: ${LABELS[category]}`, `evidence-${category}`, 'checkbox');
      evidence[category].checked = Boolean(existingSpec?.required_evidence?.includes(category));
    }
    const original = existingSpec ? {start: start.value, end: end.value} : null;
    const buildPlanSpec = () => {
      if (!activity.value || !location.value || !safeText(purpose.value) || purpose.value.length < 8 || !safeText(assignee.value) || !start.value || !end.value) throw new Error('Complete the purpose, role, approved site and planning window.');
      const startIso = original && start.value === original.start ? existingSpec.window_start : isoValue(start.value);
      const endIso = original && end.value === original.end ? existingSpec.window_end : isoValue(end.value);
      if (instant(startIso) < now() || instant(endIso) <= instant(startIso) || instant(endIso) > now() + 30 * 86400000) throw new Error('The field window must be future, ordered and within 30 days.');
      const site = locations.find(item => item.location_id === location.value);
      if (!site || !site.activities.includes(activity.value)) throw new Error('Choose an approved site that supports this activity.');
      const required = CATEGORIES.filter(category => evidence[category].checked);
      if (!required.length) throw new Error('Select at least one required evidence category.');
      return {activity: activity.value, purpose: purpose.value, assignee_role: assignee.value, window_start: startIso, window_end: endIso, required_evidence: required};
    };
    buildPlan = buildPlanSpec;
  }
  let defer; let outcome; let summary; let performedStart; let performedEnd; let referenceKind; let category; let reference; let provenance; let observedAt; let sha256; let scope;
  let originalReportTimes = null;
  if (action === 'DEFER') defer = addField(form, fields, {element}, 'Defer until (local time)', 'defer-until', 'datetime-local');
  if (action === 'REPORT' || action === 'CORRECT') {
    const report = work.result?.report;
    outcome = addSelect(form, fields, {element}, 'Outcome', 'outcome', ['',...OUTCOMES], report?.outcome || '');
    summary = addField(form, fields, {element}, 'Result summary', 'summary', 'text', report?.summary || '');
    performedStart = addField(form, fields, {element}, 'Performed start (local time)', 'performed-start', 'datetime-local', report ? localValue(report.performed_start) : '');
    performedEnd = addField(form, fields, {element}, 'Performed end (local time)', 'performed-end', 'datetime-local', report ? localValue(report.performed_end) : '');
    if (report) originalReportTimes = {start: performedStart.value, end: performedEnd.value, startIso: report.performed_start, endIso: report.performed_end};
  }
  if (action === 'ATTACH') {
    referenceKind = addSelect(form, fields, {element}, 'Reference kind', 'reference-kind', ['', 'OPERATOR_RECORD_REFERENCE', 'EXTERNAL_REFERENCE']);
    category = addSelect(form, fields, {element}, 'Evidence category', 'category', ['',...CATEGORIES]);
    reference = addField(form, fields, {element}, 'Reference', 'reference');
    provenance = addField(form, fields, {element}, 'Provenance', 'provenance');
    observedAt = addField(form, fields, {element}, 'Observed at (local time, optional)', 'observed-at', 'datetime-local');
    sha256 = addField(form, fields, {element}, 'SHA-256 (optional)', 'sha256');
    form.append(element('p','Use an operator record ID or an HTTPS reference without a query string, credentials or fragment. The reference is saved; no file is uploaded.','quiet'));
  }
  if (action === 'VERIFY') {
    form.append(element('p', `Verify report revision ${work.result?.report?.revision ?? ''}.`));
    for (const item of work.result?.evidence || []) form.append(element('p', `${LABELS[item.evidence_category]}: ${item.reference} — ${item.provenance}`));
    scope = addField(form, fields, {element}, 'Verification scope', 'scope');
  }
  const title = action === 'MODIFY' ? 'Modify and approve plan' : action === 'PROPOSE' ? 'Plan field work' : ({REPORT: 'Record result', CORRECT: 'Correct result', ATTACH: 'Attach evidence', VERIFY: 'Verify report'}[action] || `${action[0]}${action.slice(1).toLowerCase()} field work`);
  const submitLabel = title;
  let frozen = false;
  return {form, fields, title, submitLabel,
    build(requestId) {
      if (frozen || typeof requestId !== 'string' || !id(requestId)) throw new Error('This form is unavailable.');
      const body = {request_id: requestId, expected_case_revision: caseRevision, command: {operation: action}};
      if (action === 'PROPOSE' || action === 'MODIFY') {
        const specValue = buildPlan(); const site = locations.find(item => item.location_id === location.value);
        body.command = action === 'PROPOSE' ? {operation: action, task_id: target.task_id, expected_review_revision: target.revision, location_id: site.location_id, location_revision: site.revision, spec: specValue} : {operation: action, plan_id: plan.plan_id, expected_plan_revision: plan.revision, location_id: site.location_id, location_revision: site.revision, spec: specValue};
      } else if (action === 'DEFER') {
        const value = isoValue(defer.value); if (!defer.value || instant(value) <= now() || instant(value)>now()+30*86400000 || instant(value) >= instant(plan.spec.window_end)) throw new Error('Choose a future deferral within 30 days and before the plan window ends.');
        body.command = {operation: 'DECIDE', plan_id: plan.plan_id, expected_plan_revision: plan.revision, action, defer_until: value};
      } else if (['APPROVE', 'CANCEL'].includes(action)) body.command = {operation: 'DECIDE', plan_id: plan.plan_id, expected_plan_revision: plan.revision, action, defer_until: null};
      else if (action === 'REPORT' || action === 'CORRECT') {
        if (!safeText(summary.value) || summary.value.length < 8 || !performedStart.value || !performedEnd.value) throw new Error('Add an outcome, summary and performed times.');
        const start = originalReportTimes && performedStart.value === originalReportTimes.start ? originalReportTimes.startIso : isoValue(performedStart.value);
        const end = originalReportTimes && performedEnd.value === originalReportTimes.end ? originalReportTimes.endIso : isoValue(performedEnd.value);
        if (instant(end) > now() || instant(end) < instant(start) || (action === 'REPORT' && instant(start) < instant(plan.updated_at))) throw new Error('Performed times must be ordered, follow approval and cannot be in the future.');
        body.command = action === 'REPORT' ? {operation: action, plan_id: plan.plan_id, performed_plan_revision: plan.revision, outcome: outcome.value, summary: summary.value, performed_start: start, performed_end: end} : {operation: action, report_id: work.result.report.report_id, expected_report_revision: work.result.report.revision, outcome: outcome.value, summary: summary.value, performed_start: start, performed_end: end};
      } else if (action === 'ATTACH') {
        if (!reference.value || !safeText(provenance.value) || provenance.value.length < 8 || (referenceKind.value === 'EXTERNAL_REFERENCE' && !/^https:\/\//.test(reference.value))) throw new Error('Provide a supported reference and provenance.');
        const observed = observedAt.value ? isoValue(observedAt.value) : null;
        if (observed && instant(observed) > now()) throw new Error('Observed evidence cannot be from the future.');
        body.command = {operation: action, report_id: work.result.report.report_id, expected_report_revision: work.result.report.revision, reference_kind: referenceKind.value, evidence_category: category.value, reference: reference.value, provenance: provenance.value, observed_at: observed, sha256: sha256.value || null};
      } else if (action === 'VERIFY') body.command = {operation: action, report_id: work.result.report.report_id, expected_report_revision: work.result.report.revision, evidence_ids: work.result.evidence.map(item => item.evidence_id), scope: scope.value};
      if (!validFieldCommand(body)) throw new Error('The completed form is not a valid field command.');
      return body;
    },
    freeze(value) { frozen = Boolean(value); for (const field of fields) { field.disabled = frozen && (field.tagName === 'SELECT' || field.type === 'checkbox'); field.readOnly = frozen && !field.disabled; } return fields; },
  };
}
