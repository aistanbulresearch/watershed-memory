import assert from 'node:assert/strict';
import test from 'node:test';
import {validFieldContext, validFieldWork, validFieldCommand, validFieldReceipt} from '../watershed_memory/static/field-records.mjs';
import {createFieldResponseMachine} from '../watershed_memory/static/field-state.mjs';
import {validSnapshot} from '../watershed_memory/static/current-state.mjs';
import {NOW, clone, spec, plan, report, evidence, context, snapshot, work, verifyBody, verifiedResponse, storage} from './field-browser-fixtures.mjs';

const machine = (saved, send) => createFieldResponseMachine({caseId:'CASE', origin:'http://localhost:8772', storage:saved, send});
function operationPair(operation) {
  const response = verifiedResponse(), receipt = response.receipt;
  const body = verifyBody(); body.command.operation = operation; receipt.operation = operation;
  receipt.evidence = null; receipt.verification = null; receipt.result = null;
  if (['PROPOSE','MODIFY','DECIDE'].includes(operation)) {
    receipt.plan = {...plan(), status:operation==='PROPOSE'?'PROPOSED':'APPROVED',
      revision:operation==='PROPOSE'?1:2, change_kind:operation==='DECIDE'?'APPROVE':operation};
    body.command = operation==='DECIDE'
      ? {operation, plan_id:'plan-1', expected_plan_revision:1, action:'APPROVE', defer_until:null}
      : {operation, ...(operation==='PROPOSE'?{task_id:'review-1', expected_review_revision:1}:{plan_id:'plan-1', expected_plan_revision:1}),
        location_id:'site-1', location_revision:1, spec:spec()};
  } else if (['REPORT','CORRECT'].includes(operation)) {
    const saved = report(operation==='REPORT'?1:2);
    receipt.result = {report:saved, evidence:[], verification:null, verification_level:'REPORTED'};
    body.command = {operation, ...(operation==='REPORT'?{plan_id:'plan-1',performed_plan_revision:2}:{report_id:'report-1',expected_report_revision:1}),
      outcome:saved.outcome, summary:saved.summary, performed_start:NOW, performed_end:NOW};
  } else if (operation==='ATTACH') {
    receipt.evidence = evidence();
    receipt.result = {report:report(),evidence:[evidence()],verification:null,verification_level:'EVIDENCE_ATTACHED'};
    const {reference_kind,evidence_category,reference,provenance,observed_at,sha256} = evidence();
    body.command = {operation,report_id:'report-1',expected_report_revision:1,reference_kind,evidence_category,reference,provenance,observed_at,sha256};
  } else return {body:verifyBody(), receipt:verifiedResponse().receipt};
  return {body, receipt};
}
for (const operation of ['PROPOSE','MODIFY','DECIDE','REPORT','CORRECT','ATTACH','VERIFY']) {
  test(`${operation} accepted response reconciles through the complete browser machine`,async()=>{
    const {body,receipt}=operationPair(operation), response=verifiedResponse();
    response.receipt=receipt;
    const detail={plan:receipt.plan,result:receipt.result && {...receipt.result,evidence_count:receipt.result.evidence.length},
      parent_binding:'CURRENT',field_dimension:receipt.plan.status==='PROPOSED'?'NOT_PLANNED':receipt.result && receipt.result.report.outcome==='COMPLETE'?
      ({REPORTED:'REPORTED_COMPLETE',EVIDENCE_ATTACHED:'VERIFICATION_PENDING',VERIFIED:'VERIFIED_COMPLETE'})[receipt.result.verification_level]:'PLANNED',available_actions:[]};
    response.selected_field_work=detail;
    const shown=clone(detail);if(shown.result)delete shown.result.evidence;
    response.snapshot.current_field_work.current_plans=receipt.result?[]:[shown];
    response.snapshot.current_field_work.latest_results=receipt.result?[shown]:[];
    response.snapshot.current_field_work.proposal_targets=receipt.result?response.snapshot.current_field_work.proposal_targets:[];
    const active=machine(storage(),async()=>response);active.begin(body);
    assert.equal((await active.attempt()).kind,'success');assert.equal(active.pending,null);
  });
}
for (const operation of ['PROPOSE','MODIFY','DECIDE','REPORT','CORRECT','ATTACH','VERIFY']) {
  test(`${operation} confirmation proves the exact submitted operation`, () => {
    const {body, receipt} = operationPair(operation);
    assert.equal(validFieldReceipt(receipt, body, 'CASE', true), true);
    if (body.command.spec) body.command.spec.purpose = 'A different requested inspection.';
    else if (operation==='DECIDE') body.command.plan_id = 'another-plan';
    else if (['REPORT','CORRECT'].includes(operation)) body.command.summary = 'A different requested result.';
    else if (operation==='ATTACH') body.command.reference = 'another-record';
    else body.command.scope = 'A different requested verification.';
    assert.equal(validFieldReceipt(receipt, body, 'CASE', true), false);
  });
}
for (const status of [502,504,599,302]) test(`${status} cannot discard an uncertain operation`, async () => {
  const active = machine(storage(), async()=>{throw Object.assign(new Error(), {status});});
  active.begin(verifyBody()); assert.equal((await active.attempt()).kind,'ambiguous');
  assert.deepEqual(active.pending,verifyBody());
});
test('selected historical result can be outside the bounded recent list', async () => {
  const response = verifiedResponse(2);
  const recent = Array.from({length:3},(_,i)=> {
    const item=work(2,false); item.plan.plan_id=`recent-${i}`; item.result.report.plan_id=`recent-${i}`;
    item.result.report.report_id=`recent-report-${i}`; return item;
  });
  response.snapshot.current_field_work.latest_results = recent;
  response.snapshot.current_field_work.has_more_results = true;
  const active=machine(storage(),async()=>response); active.begin(verifyBody());
  assert.equal((await active.attempt()).kind,'success');
});
for (const mutate of [r=>delete r.snapshot.source, r=>r.snapshot.current_field_work.evaluated_at='2026-09-13T11:00:00Z',
  r=>r.selected_field_work.result.report.summary='Different immutable same-revision content.']) {
  test('a malformed or inconsistent current snapshot cannot confirm a receipt',async()=>{
    const response=verifiedResponse(); mutate(response);
    if (response.selected_field_work.result.report.summary.startsWith('Different'))
      response.snapshot.current_field_work.latest_results[0].result.report.summary=response.selected_field_work.result.report.summary;
    const active=machine(storage(),async()=>response); active.begin(verifyBody());
    assert.equal((await active.attempt()).kind,'ambiguous');
  });
}
for (const mutate of [w=>w.plan.author_kind='UNTRUSTED',w=>w.plan.location.kind='MONITORING_STATION',
  w=>w.plan.location.approved_by=null,w=>w.result.evidence[0].private_note='PRIVATE',
  w=>w.result.verification.private_note='PRIVATE',w=>w.result.evidence[0].evidence_category='UNSUPPORTED',
  w=>w.plan.updated_at='2026-09-14T12:00:00Z',w=>{w.result.evidence[0].reference_kind='EXTERNAL_REFERENCE';w.result.evidence[0].reference='https://example.org\\path';}]) {
  test('nested attribution and record invariants are checked before display',()=>{
    const value=work();mutate(value);assert.equal(validFieldWork(value,'CASE',true,NOW,{detail:true}),false);
  });
}
test('active and cancelled plans retain their actual field dimensions',()=>{
  for (const [status,kind,dimension] of [['APPROVED','APPROVE','PLANNED'],['CANCELLED','CANCEL','CANCELLED'],['PROPOSED','PROPOSE','NOT_PLANNED']]) {
    const value={...work(),plan:{...plan(),status,change_kind:kind},result:null,field_dimension:dimension,available_actions:[]};
    assert.equal(validFieldWork(value,'CASE',true,NOW,{detail:true}),true);
  }
});
test('zero revisions and missing attachment identity are rejected',()=>{
  for (const operation of ['PROPOSE','MODIFY','DECIDE','REPORT','CORRECT','ATTACH','VERIFY']) {
    const {body}=operationPair(operation);
    const revision=Object.keys(body.command).find(k=>k.endsWith('_revision'));
    body.command[revision]=0;assert.equal(validFieldCommand(body),false);
  }
});
test('partition and proposal-target shapes remain explicit',()=>{
  const value=context();value.current_plans=value.latest_results;value.latest_results=[];
  assert.equal(validFieldContext(value,'CASE',true,NOW,12),false);
  const next=context();next.proposal_targets[0].private_note='PRIVATE';
  assert.equal(validFieldContext(next,'CASE',true,NOW,12),false);
});
test('failed removal after a definite rejection remains retryable',async()=>{
  const saved=storage(); saved.removeItem=()=>{throw new Error('storage unavailable');};
  const active=machine(saved,async()=>{throw Object.assign(new Error(),{status:409});}); active.begin(verifyBody());
  assert.equal((await active.attempt()).kind,'ambiguous');assert.deepEqual(active.pending,verifyBody());
});

test('approved site list offers one current revision per site',()=>{
  const value=context();value.approved_locations.push({...clone(value.approved_locations[0]),revision:2});
  assert.equal(validFieldContext(value,'CASE',true,NOW,12),false);
});
for (const change of [v=>v.latest_results[0].plan.review_revision=13,v=>v.latest_results[0].plan.revision=14,
  v=>v.latest_results[0].result.report.revision=13]) test('field records cannot outrun their enclosing case revision',()=>{
  const value=context(2);change(value);assert.equal(validFieldContext(value,'CASE',true,NOW,12),false);
});
for (const latitude of ['1e-999999','90.000000000000000000000000000001','0'.repeat(129),'1e13']) {
  test('coordinates obey exact decimal source bounds before geographic comparison',()=>{
    const value=context();Object.assign(value.approved_locations[0],{latitude,longitude:'0',coordinate_system:'WGS84',coordinate_accuracy:'Synthetic coordinates'});
    assert.equal(validFieldContext(value,'CASE',true,NOW,12),false);
  });
}
for (const change of [v=>v.current_field_work.proposal_targets[0].task_id='arbitrary-review',
  v=>v.current_field_work.proposal_targets[0].revision=2,v=>v.current_field_work.proposal_targets[0].title='Wrong source title',
  v=>v.work[0].kind='UNSUPPORTED_REVIEW']) test('proposal targets are bound to the displayed supported source review',()=>{
  const value=snapshot();change(value);assert.equal(validSnapshot(value,'CASE'),false);
});
