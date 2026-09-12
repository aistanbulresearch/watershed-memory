/* Durable browser request identity for the local current-case operator desk. */
const ACTIONS = ['APPROVE', 'MODIFY', 'DEFER', 'DISMISS', 'CANCEL'];
const STATUSES = ['PROPOSED', 'APPROVED', 'DEFERRED', 'DISMISSED', 'CANCELLED'];
const identifier = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);
const text = (value, max) => typeof value === 'string' && value.length > 0 && value.length <= max;
const integer = value => Number.isSafeInteger(value) && value >= 0;
const time = value => typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
const optionalTime = value => value === null || time(value);
const strings = (value, max, pattern = null) => Array.isArray(value) && value.length <= max &&
  value.every(item => text(item, 128) && (!pattern || pattern.test(item)));
const eventId = /^[a-f0-9]{64}$/;
const parameter = /^\d{5}$/;
export const printable = value => typeof value === 'string' && /^[\p{L}\p{M}\p{N}\p{P}\p{S} ]*$/u.test(value);

export function validSnapshot(value, caseId = null) {
  if (!value || !identifier(value.case?.case_id) || (caseId && value.case.case_id !== caseId) ||
      !integer(value.case.revision) || typeof value.case.simulated !== 'boolean' || !time(value.evaluated_at)) return false;
  const source = value.source;
  if (!source || !text(source.label, 256) || !identifier(source.source_id) || !text(source.station_id, 64) ||
      !optionalTime(source.collected_through) || !time(source.next_poll_at) ||
      !['observation_count', 'event_count', 'pending_count'].every(key => integer(source[key])) ||
      !['missing_parameters', 'stale_parameters', 'null_parameters'].every(key => strings(source[key], 16, parameter)) ||
      !(source.error_code === null || text(source.error_code, 128)) ||
      !Array.isArray(source.readings) || source.readings.length > 16) return false;
  if (!source.readings.every(r => r && typeof r.parameter_code === 'string' && parameter.test(r.parameter_code) && text(r.label, 100) && text(r.unit, 32) &&
      (r.value === null || text(r.value, 200)) && optionalTime(r.observed_at) && optionalTime(r.retrieved_at) &&
      (r.approval_status === null || text(r.approval_status, 64)) &&
      strings(r.flags, 3) && r.flags.every(flag => ['MISSING', 'NULL', 'STALE'].includes(flag)))) return false;
  if (!(value.interval === null || (value.interval && typeof value.interval.event_id === 'string' && eventId.test(value.interval.event_id) &&
      time(value.interval.start) && time(value.interval.end) && text(value.interval.coverage, 64) && strings(value.interval.missing_parameters, 16, parameter)))) return false;
  if (!Array.isArray(value.work) || value.work.length > 3 || !value.work.every(work =>
      identifier(work.task_id) && integer(work.revision) && work.revision > 0 && text(work.title, 120) &&
      text(work.reason, 700) && STATUSES.includes(work.status) && text(work.kind, 64) &&
      optionalTime(work.next_check_at) && time(work.created_at) && time(work.updated_at) &&
      strings(work.evidence_event_ids, 3, eventId) && strings(work.available_actions, 5) && work.available_actions.every(a => ACTIONS.includes(a)))) return false;
  if (!Array.isArray(value.dimensions) || value.dimensions.length !== 4 || !value.dimensions.every(d =>
      text(d.id, 32) && text(d.label, 100) && text(d.status, 64) && text(d.detail, 700))) return false;
  if (!Array.isArray(value.assessments) || value.assessments.length > 10 || !value.assessments.every(a =>
      identifier(a.attempt_id) && typeof a.event_id === 'string' && eventId.test(a.event_id) && text(a.status, 32) && time(a.evaluated_at) &&
      optionalTime(a.finished_at) && typeof a.source_delivery === 'boolean' && ['SCRIPTED_SDK', 'STRANDS_CURRENT'].includes(a.mode))) return false;
  const dispatch = value.dispatch;
  return dispatch === null || Boolean(dispatch &&
      ['history_count', 'suppressed_count', 'assessment_count', 'remaining_attempts'].every(key => integer(dispatch[key])) &&
      (dispatch.active_attempt_id === null || identifier(dispatch.active_attempt_id)) &&
      (dispatch.active_status === null || ['RESERVED', 'FAILED', 'STALE'].includes(dispatch.active_status)) &&
      optionalTime(dispatch.health_evaluated_at) && (dispatch.numeric_event_id === null || (typeof dispatch.numeric_event_id === 'string' && eventId.test(dispatch.numeric_event_id))) &&
      ['SCRIPTED_SDK', 'STRANDS_CURRENT'].includes(dispatch.execution_mode));
}

function validCommand(body) {
  const fields = ['request_id', 'task_id', 'expected_revision', 'action', 'note', 'title', 'next_check_at'];
  if (!body || typeof body !== 'object' || Object.keys(body).length !== fields.length || !fields.every(f => f in body) ||
      !identifier(body.request_id) || !identifier(body.task_id) || !integer(body.expected_revision) || body.expected_revision < 1 ||
      !ACTIONS.includes(body.action) || !printable(body.note) || body.note.length > 1000) return false;
  if (body.action === 'MODIFY') return text(body.title, 120) && printable(body.title) && Boolean(body.title.trim()) && time(body.next_check_at);
  if (body.action === 'DEFER') return body.title === null && time(body.next_check_at);
  return body.title === null && body.next_check_at === null;
}

function sameWork(left, right) {
  return left.task_id === right.task_id && left.revision === right.revision && left.status === right.status &&
    left.title === right.title && (left.next_check_at === null ? right.next_check_at === null : Date.parse(left.next_check_at) === Date.parse(right.next_check_at));
}
function confirmed(result, pending, caseId) {
  const body = JSON.parse(pending.serialized);
  const receipt = result?.receipt;
  const status = { APPROVE: 'APPROVED', MODIFY: 'APPROVED', DEFER: 'DEFERRED', DISMISS: 'DISMISSED', CANCEL: 'CANCELLED' }[body.action];
  const current = result?.current_work;
  if (!receipt || !current || current.task_id !== body.task_id || !integer(current.revision) ||
      current.revision < receipt.revision || !STATUSES.includes(current.status) || !text(current.title, 120) ||
      !optionalTime(current.next_check_at) || !validSnapshot(result.snapshot, caseId) ||
      result.snapshot.case.revision < current.revision ||
      (['DISMISSED', 'CANCELLED'].includes(receipt.status) && !sameWork(current, receipt)) ||
      (current.revision === receipt.revision && !sameWork(current, receipt))) return false;
  const visible = result.snapshot.work.find(work => work.task_id === body.task_id);
  if (['PROPOSED', 'APPROVED', 'DEFERRED'].includes(current.status) ? !visible || !sameWork(visible, current) : Boolean(visible)) return false;
  return receipt.task_id === body.task_id && receipt.revision === body.expected_revision + 1 &&
    receipt.status === status && text(receipt.title, 120) && optionalTime(receipt.next_check_at) &&
    (body.action !== 'MODIFY' || receipt.title === body.title) &&
    (!['MODIFY', 'DEFER'].includes(body.action) || Date.parse(receipt.next_check_at) === Date.parse(body.next_check_at)) &&
    (!['DISMISS', 'CANCEL'].includes(body.action) || receipt.next_check_at === null) &&
    validSnapshot(result.snapshot, caseId);
}

export function createResponseMachine({ caseId, storage, send, origin }) {
  if (!identifier(caseId)) throw new Error('Invalid case identity.');
  const key = `watershed-current-v1:${origin}:${caseId}`;
  let pending = null;
  let busy = false;
  const saved = storage.getItem(key);
  if (saved !== null) {
    const record = JSON.parse(saved);
    if (record?.version !== 1 || record.caseId !== caseId || typeof record.serialized !== 'string' ||
        !validCommand(JSON.parse(record.serialized))) throw new Error('The saved response needs local reconciliation.');
    pending = record;
  }
  function clear() {
    storage.removeItem(key);
    pending = null;
  }
  return {
    get busy() { return busy; },
    get pending() { return pending === null ? null : JSON.parse(pending.serialized); },
    begin(body) {
      if (pending || busy) throw new Error('Finish the saved response first.');
      if (!validCommand(body)) throw new Error('Check the response fields.');
      const record = { version: 1, caseId, serialized: JSON.stringify(body) };
      storage.setItem(key, JSON.stringify(record)); // Persist before any request can start.
      pending = record;
    },
    async attempt() {
      if (!pending || busy) throw new Error('No available saved response.');
      busy = true;
      try {
        const result = await send(pending.serialized);
        if (!confirmed(result, pending, caseId)) throw new Error('The saved response was not confirmed.');
        clear();
        return { kind: 'success', result };
      } catch (error) {
        if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
          try { clear(); } catch (_) { return { kind: 'ambiguous' }; }
          return { kind: error.status === 409 ? 'conflict' : 'rejected' };
        }
        return { kind: 'ambiguous' };
      } finally {
        busy = false;
      }
    },
  };
}
