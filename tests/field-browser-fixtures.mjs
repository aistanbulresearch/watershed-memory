/* Synthetic public records for browser recovery checks; no deployment data. */
export const NOW = '2026-09-13T12:00:00Z';
export const START = '2026-09-13T13:00:00Z';
export const END = '2026-09-13T14:00:00Z';
export const clone = value => JSON.parse(JSON.stringify(value));
export function site() {
  return {location_id:'site-1', revision:1, label:'Demonstration inspection site',
    kind:'FIELD_SITE', case_id:'CASE', simulated:true, status:'APPROVED',
    activities:['VISUAL_INSPECTION'], latitude:null, longitude:null,
    coordinate_system:null, coordinate_accuracy:null, source_url:null,
    source_label:'Demonstration site for browser checks', recorded_at:NOW,
    approved_by:'approver', approved_at:NOW};
}
export function spec() {
  return {activity:'VISUAL_INSPECTION', purpose:'Inspect the accessible sediment marker.',
    assignee_role:'Source-water team', window_start:START, window_end:END,
    required_evidence:['INSPECTION_RECORD_REFERENCE']};
}
export function plan() {
  return {plan_id:'plan-1', case_id:'CASE', task_id:'review-1', review_revision:1,
    revision:3, status:'REPORTED', spec:spec(), location:site(), simulated:true,
    change_kind:'REPORT', author_kind:'HUMAN', recorded_by:'operator',
    deferred_until:null, created_at:NOW, updated_at:NOW};
}
export function report(revision=1) {
  return {report_id:'report-1', case_id:'CASE', plan_id:'plan-1', performed_plan_revision:2,
    revision, outcome:revision===1?'COMPLETE':'PARTIAL',
    summary:revision===1?'Inspection completed and recorded.':'The second inspection remains incomplete.',
    performed_start:NOW, performed_end:NOW, simulated:true,
    change_kind:revision===1?'REPORT':'CORRECTION', recorded_by:'operator',
    created_at:NOW, updated_at:NOW};
}
export function evidence() {
  return {evidence_id:'evidence-1', case_id:'CASE', report_id:'report-1', report_revision:1,
    reference_kind:'OPERATOR_RECORD_REFERENCE', evidence_category:'INSPECTION_RECORD_REFERENCE',
    reference:'inspection-record-1', provenance:'Operator-maintained demonstration record.',
    observed_at:NOW, sha256:null, simulated:true, attached_by:'operator', attached_at:NOW};
}
export function verification() {
  return {verification_id:'verification-1', case_id:'CASE', report_id:'report-1', report_revision:1,
    evidence_ids:['evidence-1'], scope:'Reviewed this exact inspection record.',
    method:'AUTHORIZED_HUMAN_REVIEW', simulated:true, verified_by:'operator', verified_at:NOW};
}
export function result(revision=1) {
  return {report:report(revision), evidence:revision===1?[evidence()]:[],
    verification:revision===1?verification():null, verification_level:revision===1?'VERIFIED':'REPORTED'};
}
export function work(revision=1, detail=true) {
  const saved=result(revision);
  const shown={report:saved.report, verification_level:saved.verification_level,
    evidence_count:saved.evidence.length, verification:saved.verification};
  if(detail) shown.evidence=saved.evidence;
  return {plan:plan(), result:shown, parent_binding:'CURRENT',
    field_dimension:revision===1?'VERIFIED_COMPLETE':'PLANNED',
    available_actions:['CORRECT','ATTACH']};
}
export function context(revision=1) {
  return {case_id:'CASE', case_revision:12, simulated:true, evaluated_at:NOW, state:'PRESENT',
    current_plans:[], stranded_plans:[], latest_results:[work(revision,false)],
    approved_locations:[site()], has_more_stranded_plans:false, has_more_results:false,
    proposal_targets:[{task_id:'review-1',revision:1,title:'Follow watershed evidence'}]};
}
export function snapshot(revision=1) {
  return {evaluated_at:NOW, case:{case_id:'CASE',revision:12,simulated:true},
    source:{source_id:'fixture',station_id:'USGS-00000000',label:'Synthetic browser source',
      collected_through:NOW,next_poll_at:NOW,observation_count:0,event_count:0,pending_count:0,
      error_code:null,missing_parameters:[],stale_parameters:[],null_parameters:[],readings:[]},
    interval:null, dispatch:null, assessments:[],
    work:[{task_id:'review-1',revision:1,title:'Follow watershed evidence',reason:'Synthetic review for browser tests.',
      status:'APPROVED',kind:'OBSERVATION_REVIEW',next_check_at:null,created_at:NOW,updated_at:NOW,
      evidence_event_ids:[],available_actions:['MODIFY','CANCEL']}],
    dimensions:['physical','observation','field','coverage'].map(id=>({id,label:id,status:'TRACKED',detail:'Synthetic field case.'})),
    current_field_work:context(revision)};
}
export function verifyBody() {
  return {request_id:'request-verify',expected_case_revision:10,
    command:{operation:'VERIFY',report_id:'report-1',expected_report_revision:1,
      evidence_ids:['evidence-1'],scope:verification().scope}};
}
export function verifiedResponse(revision=1) {
  return {receipt:{request_id:'request-verify',operation:'VERIFY',case_id:'CASE',case_revision:11,
      plan:plan(),recorded_at:NOW,result:result(),evidence:null,verification:verification()},
    snapshot:snapshot(revision),selected_field_work:work(revision)};
}
export function storage() {
  const items=new Map();
  return {items,getItem:key=>items.get(key)??null,setItem:(key,value)=>items.set(key,value),removeItem:key=>items.delete(key)};
}
