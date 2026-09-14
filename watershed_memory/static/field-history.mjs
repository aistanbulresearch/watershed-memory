import {validFieldContext,validFieldCommand,validFieldWork} from './field-records.mjs';

const DISPOSITIONS={NO_NEW_FIELD_PLAN:'No new field plan',AWAIT_VERIFICATION:'Await evidence verification',PROPOSE_FIELD_PLAN:'Propose follow-up field work'};
const LEVELS={REPORTED:'Report saved',EVIDENCE_ATTACHED:'Evidence attached',VERIFIED:'Verified report'};
const OUTCOMES={COMPLETE:'Reported complete',PARTIAL:'Partly completed',NOT_DONE:'Not performed'};
const TOOLS=['get_field_context','inspect_field_work','list_approved_field_locations','stage_field_decision'];
const text=value=>typeof value==='string' && value.length>=8 && value.length<=700 && !/[\u0000-\u001f\u007f]/u.test(value);
// Inputs have already passed field-records' RFC3339 validation; retain microseconds.
const instant=value=>{
  const fraction=/\.(\d+)(?=Z|[+-]\d\d:\d\d$)/.exec(value);
  return BigInt(Date.parse(value.replace(/\.\d+(?=Z|[+-]\d\d:\d\d$)/,'')))*1000n+BigInt((fraction?.[1] || '').padEnd(6,'0'));
};
const same=(a,b)=>{
  if(a===b)return true;
  if(!a || !b || typeof a!=='object' || typeof b!=='object' || Array.isArray(a)!==Array.isArray(b))return false;
  return Object.keys(a).length===Object.keys(b).length && Object.keys(a).every(k=>Object.hasOwn(b,k) && same(a[k],b[k]));
};

export function renderFieldAssessment(data,caseId,simulated,{element,fact,date}) {
  if(data.context_version!==3) {
    const keys=['assessed_field_context','field_decision','field_tool_names','field_proposal'];
    if(!keys.some(k=>Object.hasOwn(data,k)) && data.context_version===undefined)return null;
    if(![1,2].includes(data.context_version) || data.assessed_field_context!==null || data.field_decision!==null || data.field_proposal!==null ||
      !Array.isArray(data.field_tool_names) || data.field_tool_names.length)throw new Error('Unexpected field history');
    return null;
  }
  const context=data.assessed_field_context,d=data.field_decision;
  if(!validFieldContext(context,caseId,simulated,data.evaluated_at,data.case_revision) ||
    !Array.isArray(data.field_tool_names) || data.field_tool_names.length>8 || !data.field_tool_names.every(x=>TOOLS.includes(x)))throw new Error('Invalid historical field evidence');
  const records=[...context.current_plans,...context.stranded_plans,...context.latest_results];
  let basis=null;
  if(d!==null) {
    if(data.field_tool_names.length<2 || data.field_tool_names[0]!=='get_field_context' || data.field_tool_names.at(-1)!=='stage_field_decision' ||
      data.field_tool_names.filter(x=>x==='stage_field_decision').length!==1 ||
      (data.tool_names && data.tool_names.length+data.field_tool_names.length>16))throw new Error('Invalid successful field trace');
    const keys=['disposition','basis_plan_id','basis_report_id','basis_report_revision','basis_verification_level','reason','proposal'];
    if(!d || Object.keys(d).length!==keys.length || !keys.every(k=>Object.hasOwn(d,k)) || !Object.hasOwn(DISPOSITIONS,d.disposition) || !text(d.reason))throw new Error('Invalid field decision');
    basis=records.find(w=>w.plan.plan_id===d.basis_plan_id);
    if(d.basis_plan_id!==null && !basis)throw new Error('Missing historical basis');
    if(d.basis_report_id===null) {
      if(d.basis_report_revision!==null || d.basis_verification_level!==null)throw new Error('Unexpected report basis');
    } else if(!basis?.result || basis.result.report.report_id!==d.basis_report_id || basis.result.report.revision!==d.basis_report_revision ||
      basis.result.verification_level!==d.basis_verification_level)throw new Error('Historical report basis changed');
    if(d.disposition==='AWAIT_VERIFICATION' && (!basis?.result || basis.result.report.outcome!=='COMPLETE' || d.basis_report_id===null || d.basis_verification_level==='VERIFIED'))throw new Error('Invalid pending verification');
    if(d.disposition==='PROPOSE_FIELD_PLAN') {
      const p=d.proposal;
      if(!p || !validFieldCommand({request_id:'historical-proposal',expected_case_revision:context.case_revision,command:{operation:'PROPOSE',
        task_id:p.task_id,expected_review_revision:p.review_revision,location_id:p.location_id,location_revision:p.location_revision,spec:p.spec}}) ||
        !context.approved_locations.some(l=>l.location_id===p.location_id && l.revision===p.location_revision && l.activities.includes(p.spec.activity)))throw new Error('Invalid historical proposal');
    } else if(d.proposal!==null)throw new Error('Unexpected historical proposal');
  }
  const saved=data.field_proposal;
  const committedProposal=data.status==='COMMITTED' && d?.disposition==='PROPOSE_FIELD_PLAN';
  if(committedProposal !== (saved!==null))throw new Error('Saved proposal does not match delivery status');
  if(saved!==null) {
    const p=d.proposal,site=context.approved_locations.find(l=>l.location_id===p.location_id && l.revision===p.location_revision);
    if(!validFieldWork({plan:saved,result:null,parent_binding:'CURRENT',field_dimension:'NOT_PLANNED',available_actions:[]},
      caseId,simulated,data.finished_at,{caseRevision:context.case_revision+1}) || saved.revision!==1 || saved.status!=='PROPOSED' ||
      saved.change_kind!=='PROPOSE' || saved.author_kind!=='AGENT' || saved.recorded_by!==data.attempt_id || saved.task_id!==p.task_id ||
      saved.review_revision!==p.review_revision || !same(saved.location,site) || !same(saved.spec,p.spec) ||
      instant(saved.created_at)!==instant(saved.updated_at) || instant(saved.created_at)!==instant(data.finished_at))throw new Error('Saved agent proposal changed');
  }
  const node=element('section',null,'decision');
  node.append(element('h3','Historical field evidence used by this assessment'),fact('Assessed at',date(data.evaluated_at)),
    fact('Field tools',data.field_tool_names.join(' → ') || 'No completed field tools'));
  if(basis) {
    node.append(fact('Field work considered',basis.plan.spec.purpose));
    if(d.basis_report_id!==null)node.append(fact('Report revision',d.basis_report_revision),fact('Reported outcome',OUTCOMES[basis.result.report.outcome]),
      fact('Evidence review at assessment',LEVELS[d.basis_verification_level]),element('p',basis.result.report.summary));
  }
  if(d) {
    node.append(fact('Field decision',DISPOSITIONS[d.disposition]),element('p',d.reason));
    if(d.proposal)node.append(fact('Proposed follow-up',d.proposal.spec.purpose));
    if(saved)node.append(fact('Saved agent proposal',saved.spec.purpose),fact('Saved at',date(saved.created_at)));
  } else node.append(element('p','No field decision was saved for this assessment.','quiet'));
  return node;
}
