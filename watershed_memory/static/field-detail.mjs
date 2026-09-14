import {validSnapshot} from './current-state.mjs';
import {validFieldWork} from './field-records.mjs';

const same=(a,b)=>{
  if(a===b)return true;
  if(!a || !b || typeof a!=='object' || typeof b!=='object' || Array.isArray(a)!==Array.isArray(b))return false;
  return Object.keys(a).length===Object.keys(b).length && Object.keys(a).every(k=>Object.hasOwn(b,k) && same(a[k],b[k]));
};
export function sameDisplayedField(summary,detail) {
  if(summary?.result && Object.hasOwn(summary.result,'evidence'))return same(summary,detail);
  const compact=JSON.parse(JSON.stringify(detail)); if(compact.result)delete compact.result.evidence;
  return same(summary,compact);
}
export function validFieldDetail(value,caseId,planId) {
  try {
    const s=value?.snapshot,w=value?.selected_field_work,f=s?.current_field_work;
    if(!validSnapshot(s,caseId) || !f || w?.plan?.plan_id!==planId ||
      !validFieldWork(w,caseId,s.case.simulated,s.evaluated_at,{detail:true,caseRevision:s.case.revision}))return false;
    const visible=[...f.current_plans,...f.stranded_plans,...f.latest_results].find(x=>x.plan.plan_id===planId);
    if(visible)return sameDisplayedField(visible,w);
    if(w.plan.status==='CANCELLED')return true;
    if(w.plan.status==='REPORTED')return f.has_more_results;
    return w.parent_binding!=='CURRENT' && f.has_more_stranded_plans;
  } catch { return false; }
}
const OUTCOMES={COMPLETE:'Reported complete',PARTIAL:'Partly completed',NOT_DONE:'Not performed'};
const LEVELS={VERIFIED:'Verified report',EVIDENCE_ATTACHED:'Evidence attached',REPORTED:'Report saved'};
const STATUS={PROPOSED:'Proposed',APPROVED:'Approved',DEFERRED:'Deferred',CANCELLED:'Cancelled',REPORTED:'Result recorded'};
export function fieldConfirmationText(value) {
  const old=value.receipt,latest=value.selected_field_work;
  if(old.result) return `Original confirmation: report ${old.result.report.revision}, ${LEVELS[old.result.verification_level].toLowerCase()}. `+
    `Current result: report ${latest.result.report.revision}, ${OUTCOMES[latest.result.report.outcome].toLowerCase()}, ${LEVELS[latest.result.verification_level].toLowerCase()}.`;
  return `Field response saved: ${STATUS[old.plan.status].toLowerCase()}. Current field plan: ${STATUS[latest.plan.status].toLowerCase()}.`;
}
export function renderFieldDetail(work,{element,fact,date}) {
  const node=element('section',null,'field-detail'),p=work.plan,r=work.result;
  node.append(element('h3',p.spec.purpose),fact('Plan',STATUS[p.status]),fact('Site',p.location.label),
    fact('Assigned role',p.spec.assignee_role),fact('Planned window',`${date(p.spec.window_start)} – ${date(p.spec.window_end)}`));
  if(work.parent_binding!=='CURRENT')node.append(element('p','This field work belongs to an earlier review.','notice'));
  if(p.deferred_until)node.append(fact('Deferred until',date(p.deferred_until)));
  if(r) {
    node.append(fact('Reported outcome',OUTCOMES[r.report.outcome]),fact('Evidence review',LEVELS[r.verification_level]),
      fact('Result revision',r.report.revision),element('p',r.report.summary),
      fact('Performed',`${date(r.report.performed_start)} – ${date(r.report.performed_end)}`));
    for(const e of r.evidence) {
      const item=element('article',null,'decision');
      item.append(fact('Evidence reference',e.reference),fact('Provenance',e.provenance),fact('Attached',date(e.attached_at)),
        fact('Evidence record',e.evidence_id));
      if(e.observed_at)item.append(fact('Observed',date(e.observed_at)));
      if(e.sha256)item.append(fact('Content fingerprint',e.sha256));
      node.append(item);
    }
    if(!r.evidence.length)node.append(element('p','No evidence is attached to this result revision.','quiet'));
    if(r.verification)node.append(fact('Verification scope',r.verification.scope),fact('Verified',date(r.verification.verified_at)));
  }
  return node;
}
