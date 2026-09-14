/* Strict public field records. These functions do not access storage or the network. */
const ACTIVITIES = ['VISUAL_INSPECTION','SAMPLING','MAINTENANCE_REVIEW'];
const CATEGORIES = ['PHOTO_REFERENCE','SAMPLE_RECORD_REFERENCE','INSPECTION_RECORD_REFERENCE','MAINTENANCE_RECORD_REFERENCE'];
const OUTCOMES = ['COMPLETE','PARTIAL','NOT_DONE'];
const CHANGES = {PROPOSE:'PROPOSED',APPROVE:'APPROVED',MODIFY:'APPROVED',DEFER:'DEFERRED',CANCEL:'CANCELLED',REPORT:'REPORTED'};
const id = v => typeof v === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(v);
const count = v => Number.isSafeInteger(v) && v >= 0;
const revision = v => count(v) && v > 0;
const text = (v,min,max) => typeof v === 'string' && [...v].length >= min && [...v].length <= max && /^[\p{L}\p{M}\p{N}\p{P}\p{S} ]*$/u.test(v);
const exact = (v,keys) => Boolean(v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === keys.length && keys.every(k=>Object.hasOwn(v,k)));
const list = (v,min,max,check) => Array.isArray(v) && v.length >= min && v.length <= max && v.every(check);
const unique = v => new Set(v).size === v.length;
const ordered = v => unique(v) && v.every((x,i)=>i === 0 || v[i-1] < x);
const safe = fn => (...args) => { try { return Boolean(fn(...args)); } catch { return false; } };
function time(v) {
  if (typeof v !== 'string' || v.length > 64) return false;
  const p=/^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,6}))?(Z|[+-]\d\d:\d\d)$/.exec(v);
  if (!p || !Number.isFinite(Date.parse(v)) || +p[1] < 1 || +p[4] > 23 || +p[5] > 59 || +p[6] > 59) return false;
  const d=new Date(`${p[1]}-${p[2]}-${p[3]}T00:00:00Z`);
  return d.getUTCFullYear() === +p[1] && d.getUTCMonth()+1 === +p[2] && d.getUTCDate() === +p[3];
}
// Compare UTC instants without losing Python's microsecond precision.
function instant(v) {
  const f=/\.(\d+)(?=Z|[+-]\d\d:\d\d$)/.exec(v);
  return BigInt(Date.parse(v.replace(/\.\d+(?=Z|[+-]\d\d:\d\d$)/,'')))*1000n + BigInt((f?.[1] || '').padEnd(6,'0'));
}
const before = (a,b) => instant(a) <= instant(b);
const stamp = (v,end) => time(v) && before(v,end);
const sameTime = (a,b) => a === null || b === null ? a === b : time(a) && time(b) && instant(a) === instant(b);
function same(a,b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
  return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k=>Object.hasOwn(b,k) && same(a[k],b[k]));
}
function https(v) {
  if (!text(v,1,512) || /[\s?#\\]/u.test(v) || !v.startsWith('https://')) return false;
  try { const u=new URL(v); return Boolean(u.hostname) && u.protocol === 'https:' && !u.username && !u.password && !u.search && !u.hash && ['', '443'].includes(u.port); }
  catch { return false; }
}
function coordinate(v,max) {
  if (typeof v !== 'string' || v.length > 128) return false;
  const m=/^[+-]?(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/.exec(v);
  if (!m) return false;
  const scale=Number(m[3] || 0)-(m[2]?.length || 0);
  const digits=(m[1]+(m[2] || '')).replace(/^0+/,'') || '0';
  if (!Number.isSafeInteger(scale) || scale < -32 || scale > 12 || digits.length-1+scale > 75 || (digits.replace(/0+$/,'') || '0').length > 64) return false;
  const coefficient=BigInt(digits),limit=BigInt(max);
  return scale >= 0 ? coefficient*10n**BigInt(scale) <= limit : coefficient <= limit*10n**BigInt(-scale);
}
function location(v,caseId,simulated,at) {
  if (!exact(v,['location_id','revision','label','kind','case_id','simulated','status','activities','latitude','longitude','coordinate_system',
    'coordinate_accuracy','source_url','source_label','recorded_at','approved_by','approved_at']) || !id(v.location_id) || !revision(v.revision) ||
    !text(v.label,1,160) || v.kind !== 'FIELD_SITE' || v.case_id !== caseId || v.simulated !== simulated || v.status !== 'APPROVED' ||
    !list(v.activities,1,3,a=>ACTIVITIES.includes(a)) || !unique(v.activities) || !text(v.source_label,1,200) ||
    !(v.source_url === null || https(v.source_url)) || !id(v.approved_by) || !stamp(v.recorded_at,at) || !stamp(v.approved_at,v.recorded_at)) return false;
  if (v.latitude === null && v.longitude === null) {
    if (!simulated || v.coordinate_system !== null || v.coordinate_accuracy !== null) return false;
  } else if (!coordinate(v.latitude,90) || !coordinate(v.longitude,180) || v.coordinate_system !== 'WGS84' || !text(v.coordinate_accuracy,1,160)) return false;
  return !simulated || v.source_label.toLowerCase().split(/\s+/).some(w=>['demo','demonstration','simulated'].includes(w));
}
function spec(v) {
  return exact(v,['activity','purpose','assignee_role','window_start','window_end','required_evidence']) && ACTIVITIES.includes(v.activity) &&
    text(v.purpose,8,700) && text(v.assignee_role,1,120) && time(v.window_start) && time(v.window_end) && instant(v.window_start) < instant(v.window_end) &&
    list(v.required_evidence,1,4,e=>CATEGORIES.includes(e)) && unique(v.required_evidence);
}
function plan(v,caseId,simulated,at) {
  return exact(v,['plan_id','case_id','task_id','review_revision','revision','status','spec','location','simulated','change_kind','author_kind',
    'recorded_by','deferred_until','created_at','updated_at']) && id(v.plan_id) && v.case_id === caseId && id(v.task_id) && revision(v.review_revision) &&
    revision(v.revision) && Object.hasOwn(CHANGES,v.change_kind) && CHANGES[v.change_kind] === v.status && spec(v.spec) &&
    location(v.location,caseId,simulated,at) && v.location.activities.includes(v.spec.activity) && v.simulated === simulated &&
    (v.author_kind === 'HUMAN' || (v.author_kind === 'AGENT' && v.change_kind === 'PROPOSE')) && id(v.recorded_by) &&
    stamp(v.updated_at,at) && stamp(v.created_at,v.updated_at) && before(v.location.recorded_at,v.updated_at) &&
    (v.status === 'DEFERRED' ? time(v.deferred_until) && instant(v.deferred_until) > instant(v.updated_at) : v.deferred_until === null);
}
function report(v,planId,caseId,simulated,at) {
  return exact(v,['report_id','case_id','plan_id','performed_plan_revision','revision','outcome','summary','performed_start','performed_end','simulated',
    'change_kind','recorded_by','created_at','updated_at']) && id(v.report_id) && v.case_id === caseId && v.plan_id === planId &&
    revision(v.performed_plan_revision) && revision(v.revision) && OUTCOMES.includes(v.outcome) && text(v.summary,8,1000) && v.simulated === simulated &&
    ['REPORT','CORRECTION'].includes(v.change_kind) && id(v.recorded_by) && stamp(v.updated_at,at) && stamp(v.created_at,v.updated_at) &&
    stamp(v.performed_end,v.updated_at) && stamp(v.performed_start,v.performed_end);
}
function evidenceFields(v) {
  return ['EXTERNAL_REFERENCE','OPERATOR_RECORD_REFERENCE'].includes(v.reference_kind) && CATEGORIES.includes(v.evidence_category) &&
    (v.reference_kind === 'EXTERNAL_REFERENCE' ? https(v.reference) : id(v.reference)) && text(v.provenance,8,500) &&
    (v.observed_at === null || time(v.observed_at)) && (v.sha256 === null || (typeof v.sha256 === 'string' && /^[a-f0-9]{64}$/.test(v.sha256)));
}
function evidence(v,r,at) {
  return exact(v,['evidence_id','case_id','report_id','report_revision','reference_kind','evidence_category','reference','provenance','observed_at',
    'sha256','simulated','attached_by','attached_at']) && id(v.evidence_id) && v.case_id === r.case_id && v.report_id === r.report_id &&
    v.report_revision === r.revision && v.simulated === r.simulated && evidenceFields(v) && id(v.attached_by) && stamp(v.attached_at,at) &&
    before(r.updated_at,v.attached_at) && (v.observed_at === null || before(v.observed_at,v.attached_at));
}
function verification(v,r,at) {
  return exact(v,['verification_id','case_id','report_id','report_revision','evidence_ids','scope','method','simulated','verified_by','verified_at']) &&
    id(v.verification_id) && v.case_id === r.case_id && v.report_id === r.report_id && v.report_revision === r.revision && v.simulated === r.simulated &&
    list(v.evidence_ids,1,8,id) && ordered(v.evidence_ids) && text(v.scope,8,500) && v.method === 'AUTHORIZED_HUMAN_REVIEW' && id(v.verified_by) &&
    stamp(v.verified_at,at) && before(r.updated_at,v.verified_at);
}
function result(v,p,caseId,simulated,at,detail,receipt=false) {
  if (!exact(v,['report','verification_level','verification',...(receipt?[]:['evidence_count']),...(detail?['evidence']:[])]) ||
    !report(v.report,p.plan_id,caseId,simulated,at) || v.report.performed_plan_revision !== p.revision-1) return false;
  const n=receipt?v.evidence?.length:v.evidence_count;
  if (!count(n) || n > 8 || (detail && (!list(v.evidence,n,n,e=>evidence(e,v.report,at)) || !ordered(v.evidence.map(e=>e.evidence_id))))) return false;
  if (v.verification !== null && (!verification(v.verification,v.report,at) || !n || v.verification.evidence_ids.length !== n)) return false;
  if (detail && v.verification && (!same(v.verification.evidence_ids,v.evidence.map(e=>e.evidence_id)) || v.evidence.some(e=>!before(e.attached_at,v.verification.verified_at)))) return false;
  return v.verification_level === (v.verification?'VERIFIED':n?'EVIDENCE_ATTACHED':'REPORTED');
}
function joined(v,caseId,simulated,at,detail,receipt=false) {
  return plan(v.plan,caseId,simulated,at) && ((v.plan.status === 'REPORTED') === (v.result !== null)) &&
    (v.result === null || result(v.result,v.plan,caseId,simulated,at,detail,receipt));
}
function dimension(v) {
  if (v.plan.status === 'PROPOSED') return 'NOT_PLANNED';
  if (v.plan.status === 'CANCELLED') return 'CANCELLED';
  if (!v.result || v.result.report.outcome !== 'COMPLETE') return 'PLANNED';
  return {REPORTED:'REPORTED_COMPLETE',EVIDENCE_ATTACHED:'VERIFICATION_PENDING',VERIFIED:'VERIFIED_COMPLETE'}[v.result.verification_level];
}
const withinCase = (v,limit) => count(limit) && v.plan.revision <= limit && v.plan.review_revision <= limit &&
  (!v.result || v.result.report.revision <= limit);
export const validFieldWork = safe((v,caseId,simulated,at,{detail=false,caseRevision=null}={}) =>
  id(caseId) && typeof simulated === 'boolean' && time(at) && exact(v,['plan','result','parent_binding','field_dimension','available_actions']) &&
  joined(v,caseId,simulated,at,detail) && (caseRevision === null || withinCase(v,caseRevision)) &&
  ['CURRENT','SUPERSEDED','TERMINAL'].includes(v.parent_binding) && v.field_dimension === dimension(v) &&
  list(v.available_actions,0,8,a=>['APPROVE','MODIFY','DEFER','CANCEL','REPORT','CORRECT','ATTACH','VERIFY'].includes(a)) && unique(v.available_actions));

export const validFieldContext = safe((v,caseId,simulated,at,caseRevision) => {
  const keys=['case_id','case_revision','simulated','evaluated_at','state','current_plans','stranded_plans','latest_results','approved_locations','has_more_stranded_plans','has_more_results'];
  if (Object.hasOwn(v,'proposal_targets')) keys.push('proposal_targets');
  if (!exact(v,keys) || !id(caseId) || typeof simulated !== 'boolean' || !count(caseRevision) || v.case_id !== caseId || v.case_revision !== caseRevision ||
    v.simulated !== simulated || !sameTime(v.evaluated_at,at) || !['EMPTY','PRESENT'].includes(v.state) ||
    typeof v.has_more_results !== 'boolean' || typeof v.has_more_stranded_plans !== 'boolean') return false;
  for (const [key,max] of [['current_plans',2],['stranded_plans',3],['latest_results',3]]) {
    if (!list(v[key],0,max,w=>validFieldWork(w,caseId,simulated,at,{caseRevision}))) return false;
    if (v[key].some(w=>key === 'latest_results' ? w.plan.status !== 'REPORTED' :
      !['PROPOSED','APPROVED','DEFERRED'].includes(w.plan.status) || (key === 'current_plans' ? w.parent_binding !== 'CURRENT' : w.parent_binding === 'CURRENT'))) return false;
  }
  const all=[...v.current_plans,...v.stranded_plans,...v.latest_results];
  if (!unique(all.map(w=>w.plan.plan_id)) || (v.state === 'EMPTY' && (all.length || v.has_more_results || v.has_more_stranded_plans)) ||
    (v.has_more_results && v.latest_results.length !== 3) || (v.has_more_stranded_plans && v.stranded_plans.length !== 3)) return false;
  if (!list(v.approved_locations,0,16,l=>location(l,caseId,simulated,at)) || !unique(v.approved_locations.map(l=>l.location_id))) return false;
  return !Object.hasOwn(v,'proposal_targets') || (list(v.proposal_targets,0,2,t=>exact(t,['task_id','revision','title']) &&
    id(t.task_id) && revision(t.revision) && t.revision <= caseRevision && text(t.title,1,120)) && unique(v.proposal_targets.map(t=>t.task_id)));
});
const SHAPES={
  PROPOSE:['operation','task_id','expected_review_revision','location_id','location_revision','spec'],
  MODIFY:['operation','plan_id','expected_plan_revision','location_id','location_revision','spec'],
  DECIDE:['operation','plan_id','expected_plan_revision','action','defer_until'],
  REPORT:['operation','plan_id','performed_plan_revision','outcome','summary','performed_start','performed_end'],
  CORRECT:['operation','report_id','expected_report_revision','outcome','summary','performed_start','performed_end'],
  ATTACH:['operation','report_id','expected_report_revision','reference_kind','evidence_category','reference','provenance','observed_at','sha256'],
  VERIFY:['operation','report_id','expected_report_revision','evidence_ids','scope'],
};
export const validFieldCommand = safe(body => {
  if (!exact(body,['request_id','expected_case_revision','command']) || !id(body.request_id) || !count(body.expected_case_revision) ||
    body.expected_case_revision === Number.MAX_SAFE_INTEGER || !Object.hasOwn(SHAPES,body.command.operation)) return false;
  const c=body.command;
  if (!exact(c,SHAPES[c.operation])) return false;
  for (const k of Object.keys(c)) {
    if (k.endsWith('_id') && !id(c[k])) return false;
    if (k.endsWith('_revision') && (!revision(c[k]) || c[k] === Number.MAX_SAFE_INTEGER)) return false;
  }
  if (['PROPOSE','MODIFY'].includes(c.operation)) return spec(c.spec);
  if (c.operation === 'DECIDE') return ['APPROVE','DEFER','CANCEL'].includes(c.action) && (c.action === 'DEFER'?time(c.defer_until):c.defer_until === null);
  if (['REPORT','CORRECT'].includes(c.operation)) return OUTCOMES.includes(c.outcome) && text(c.summary,8,1000) && time(c.performed_start) && time(c.performed_end) && before(c.performed_start,c.performed_end);
  if (c.operation === 'ATTACH') return evidenceFields(c);
  return list(c.evidence_ids,1,8,id) && ordered(c.evidence_ids) && text(c.scope,8,500);
});
const sameSpec = (a,b) => ['activity','purpose','assignee_role','required_evidence'].every(k=>same(a[k],b[k])) && sameTime(a.window_start,b.window_start) && sameTime(a.window_end,b.window_end);
const sameReport = (a,b) => a.outcome === b.outcome && a.summary === b.summary && sameTime(a.performed_start,b.performed_start) && sameTime(a.performed_end,b.performed_end);
export const validFieldReceipt = safe((v,body,caseId,simulated) => {
  if (!validFieldCommand(body) || !id(caseId) || typeof simulated !== 'boolean' ||
    !exact(v,['request_id','operation','case_id','case_revision','plan','recorded_at','result','evidence','verification']) ||
    v.request_id !== body.request_id || v.operation !== body.command.operation || v.case_id !== caseId ||
    v.case_revision !== body.expected_case_revision+1 || !time(v.recorded_at) || !joined(v,caseId,simulated,v.recorded_at,true,true) || !withinCase(v,v.case_revision)) return false;
  const c=body.command,p=v.plan,r=v.result?.report;
  if (['REPORT','CORRECT','ATTACH','VERIFY'].includes(c.operation) !== (v.result !== null) ||
    (c.operation === 'ATTACH') !== (v.evidence !== null) || (c.operation === 'VERIFY') !== (v.verification !== null)) return false;
  if (['PROPOSE','MODIFY','DECIDE'].includes(c.operation)) {
    if (p.author_kind !== 'HUMAN' || p.change_kind !== (c.operation === 'DECIDE'?c.action:c.operation) || !sameTime(p.updated_at,v.recorded_at)) return false;
    if (c.operation === 'PROPOSE' ? p.revision !== 1 || p.task_id !== c.task_id || p.review_revision !== c.expected_review_revision : p.plan_id !== c.plan_id || p.revision !== c.expected_plan_revision+1) return false;
    return c.operation === 'DECIDE' ? sameTime(p.deferred_until,c.defer_until) : p.location.location_id === c.location_id && p.location.revision === c.location_revision && sameSpec(p.spec,c.spec);
  }
  if (c.operation === 'REPORT') return p.plan_id === c.plan_id && p.revision === c.performed_plan_revision+1 && r.revision === 1 &&
    r.change_kind === 'REPORT' && sameReport(r,c) && sameTime(r.updated_at,v.recorded_at) && v.result.verification_level === 'REPORTED';
  if (r.report_id !== c.report_id) return false;
  if (c.operation === 'CORRECT') return r.revision === c.expected_report_revision+1 && r.change_kind === 'CORRECTION' && sameReport(r,c) && sameTime(r.updated_at,v.recorded_at) && v.result.verification_level === 'REPORTED';
  if (r.revision !== c.expected_report_revision) return false;
  if (c.operation === 'ATTACH') return evidence(v.evidence,r,v.recorded_at) && v.result.evidence.some(e=>same(e,v.evidence)) &&
    ['reference_kind','evidence_category','reference','provenance','sha256'].every(k=>v.evidence[k] === c[k]) &&
    sameTime(v.evidence.observed_at,c.observed_at) && sameTime(v.evidence.attached_at,v.recorded_at);
  return same(v.verification,v.result.verification) && v.result.verification_level === 'VERIFIED' &&
    same(v.verification.evidence_ids,c.evidence_ids) && v.verification.scope === c.scope && sameTime(v.verification.verified_at,v.recorded_at);
});
