import assert from 'node:assert/strict';
import test from 'node:test';
import {validFieldContext,validFieldWork,validFieldCommand,validFieldReceipt} from '../watershed_memory/static/field-records.mjs';
import {createFieldResponseMachine} from '../watershed_memory/static/field-state.mjs';
import {NOW,START,END,clone,spec,context,work,verifyBody,verifiedResponse,storage} from './field-browser-fixtures.mjs';

const validContext=value=>validFieldContext(value,'CASE',true,NOW,12);
const validWork=value=>validFieldWork(value,'CASE',true,NOW,{detail:true});
const machine=(saved,send)=>createFieldResponseMachine({caseId:'CASE',origin:'http://localhost:8772',storage:saved,send});

test('compact and detailed field views retain exact case and evidence links',()=>{
  assert.equal(validContext(context()),true);
  assert.equal(validWork(work()),true);
  const past=context();delete past.proposal_targets;
  assert.equal(validContext(past),true);
});
test('an enabled empty field view is valid and makes no completion claim',()=>{
  const empty=context();empty.state='EMPTY';empty.latest_results=[];
  assert.equal(validContext(empty),true);
  empty.has_more_results=true;
  assert.equal(validContext(empty),false);
});
for(const change of [v=>v.case_id='OTHER',v=>v.simulated=false,v=>v.case_revision=13,
  v=>v.case_revision=Number.MAX_SAFE_INTEGER+1,v=>v.private_note='PRIVATE',
  v=>v.latest_results[0].plan.case_id='OTHER',v=>v.latest_results[0].plan.location.simulated=false,
  v=>v.latest_results[0].result.report.plan_id='other',v=>v.latest_results[0].result.verification.report_revision=2,
  v=>v.latest_results[0].result.evidence_count=0,v=>v.latest_results.push(clone(v.latest_results[0])),
  v=>v.latest_results[0].available_actions=['ERASE'],v=>v.latest_results[0].plan.spec.private_note='PRIVATE',
  v=>v.proposal_targets[0].revision=0]) {
  test('malformed field data is rejected before rendering',()=>{
    const value=context();change(value);assert.equal(validContext(value),false);
  });
}
for(const change of [v=>v.result.evidence[0].case_id='OTHER',v=>v.result.evidence[0].report_revision=2,
  v=>v.result.evidence[0].reference='javascript:alert(1)',v=>v.result.verification.evidence_ids=['other'],
  v=>v.result.report.performed_plan_revision=1,v=>v.result.evidence_count=2]) {
  test('selected work requires the exact performed plan and evidence revision',()=>{
    const value=work();change(value);assert.equal(validWork(value),false);
  });
}
test('a verified partial report remains PLANNED, not VERIFIED_COMPLETE',()=>{
  const value=work();value.result.report.outcome='PARTIAL';value.field_dimension='PLANNED';
  assert.equal(validWork(value),true);
  value.field_dimension='VERIFIED_COMPLETE';assert.equal(validWork(value),false);
});

const commands=[
  {operation:'PROPOSE',task_id:'review-1',expected_review_revision:1,location_id:'site-1',location_revision:1,spec:spec()},
  {operation:'MODIFY',plan_id:'plan-1',expected_plan_revision:1,location_id:'site-1',location_revision:1,spec:spec()},
  {operation:'DECIDE',plan_id:'plan-1',expected_plan_revision:1,action:'APPROVE',defer_until:null},
  {operation:'REPORT',plan_id:'plan-1',performed_plan_revision:2,outcome:'PARTIAL',summary:'Inspection partly completed.',performed_start:NOW,performed_end:NOW},
  {operation:'CORRECT',report_id:'report-1',expected_report_revision:1,outcome:'PARTIAL',summary:'Inspection partly completed.',performed_start:NOW,performed_end:NOW},
  {operation:'ATTACH',report_id:'report-1',expected_report_revision:1,reference_kind:'OPERATOR_RECORD_REFERENCE',evidence_category:'PHOTO_REFERENCE',reference:'photo-1',provenance:'Operator-maintained photo record.',observed_at:null,sha256:null},
  verifyBody().command,
];
for(const command of commands) {
  test(`strict ${command.operation} browser command is supported without caller authority`,()=>{
    const body={request_id:'new-request',expected_case_revision:10,command:clone(command)};
    assert.equal(validFieldCommand(body),true);
    for(const key of ['private_note','case_id','expected_simulated','principal','roles']) {
      const bad=clone(body);bad.command[key]='PRIVATE';assert.equal(validFieldCommand(bad),false);
    }
    const bad=clone(body);bad.case_id='OTHER';assert.equal(validFieldCommand(bad),false);
  });
}
for(const change of [b=>b.request_id='özel',b=>b.expected_case_revision=true,
  b=>b.command.expected_report_revision='1',b=>b.command.evidence_ids=[],
  b=>b.command.evidence_ids=['same','same'],b=>b.command.evidence_ids=['z','a'],
  b=>b.command.scope='short',b=>b.command.evidence_ids=Array(9).fill('e')]) {
  test('unsafe command identity, revisions and evidence cannot enter saved state',()=>{
    const value=verifyBody();change(value);assert.equal(validFieldCommand(value),false);
  });
}
test('planning and reference boundaries match the typed service',()=>{
  const body={request_id:'new-request',expected_case_revision:10,command:clone(commands[0])};
  body.command.spec.window_start=END;body.command.spec.window_end=START;
  assert.equal(validFieldCommand(body),false);
  body.command=clone(commands[5]);body.command.reference_kind='EXTERNAL_REFERENCE';
  body.command.reference='https://example.org/inspection';assert.equal(validFieldCommand(body),true);
  for(const ref of ['http://example.org/x','https://user:pass@example.org/x','https://example.org/x?q=1','https://example.org:444/x']) {
    body.command.reference=ref;assert.equal(validFieldCommand(body),false);
  }
});
test('exact original receipt is valid even beside a newer corrected result',()=>{
  const response=verifiedResponse(2);
  assert.equal(validFieldReceipt(response.receipt,verifyBody(),'CASE',true),true);
  assert.equal(validWork(response.selected_field_work),true);
});
test('lost confirmation survives reload and retries byte-identical VERIFY input',async()=>{
  const saved=storage(),sent=[];
  const first=machine(saved,async bytes=>{sent.push(bytes);throw new Error('lost');});
  first.begin(verifyBody());assert.equal((await first.attempt()).kind,'ambiguous');
  const next=machine(saved,async bytes=>{sent.push(bytes);return verifiedResponse(2);});
  assert.deepEqual(next.pending,verifyBody());
  assert.equal((await next.attempt()).kind,'success');
  assert.equal(sent[0],sent[1]);assert.equal(saved.items.size,0);
});
for(const change of [r=>r.receipt.request_id='other',r=>r.receipt.operation='ATTACH',
  r=>r.receipt.case_revision=12,r=>r.receipt.result.report.revision=2,
  r=>r.receipt.verification.scope='Another verification scope.',
  r=>r.selected_field_work.plan.plan_id='other',r=>r.snapshot.case.case_id='OTHER',
  r=>r.snapshot.case.revision=10,r=>r.snapshot.current_field_work.latest_results[0].result.report.summary='A different saved report.',
  r=>r.selected_field_work.result.evidence_count=2]) {
  test('misattributed successful HTTP output cannot clear saved field identity',async()=>{
    const response=verifiedResponse();change(response);
    const saved=storage(),active=machine(saved,async()=>response);active.begin(verifyBody());
    assert.equal((await active.attempt()).kind,'ambiguous');assert.deepEqual(active.pending,verifyBody());
  });
}
for(const status of [0,408,429,500,503]) {
  test(`temporary ${status} keeps exact field response pending`,async()=>{
    const active=machine(storage(),async()=>{throw Object.assign(new Error(),{status});});active.begin(verifyBody());
    assert.equal((await active.attempt()).kind,'ambiguous');assert.deepEqual(active.pending,verifyBody());
    assert.throws(()=>active.begin(verifyBody()));
  });
}
for(const status of [400,404,409,422]) {
  test(`definite ${status} releases only the rejected field request`,async()=>{
    const active=machine(storage(),async()=>{throw Object.assign(new Error(),{status});});active.begin(verifyBody());
    assert.equal((await active.attempt()).kind,status===409?'conflict':'rejected');assert.equal(active.pending,null);
  });
}
test('storage failure prevents sending, and cleanup failure preserves retry',async()=>{
  const saved=storage();let calls=0;const set=saved.setItem;
  saved.setItem=()=>{throw new Error('unavailable');};
  const active=machine(saved,async()=>{calls++;return verifiedResponse();});
  assert.throws(()=>active.begin(verifyBody()));assert.equal(calls,0);assert.equal(active.pending,null);
  saved.setItem=set;active.begin(verifyBody());saved.removeItem=()=>{throw new Error('unavailable');};
  assert.equal((await active.attempt()).kind,'ambiguous');assert.deepEqual(active.pending,verifyBody());
});
test('parallel field requests are refused and callers cannot mutate saved input',async()=>{
  let release;const active=machine(storage(),()=>new Promise(resolve=>release=resolve));
  active.begin(verifyBody());const visible=active.pending;visible.command.scope='Modified caller object';
  assert.deepEqual(active.pending,verifyBody());const pending=active.attempt();assert.equal(active.busy,true);
  await assert.rejects(active.attempt());release(verifiedResponse());assert.equal((await pending).kind,'success');
});
test('corrupt pending input is retained, and source-review storage is separate',()=>{
  const saved=storage();saved.setItem('watershed-current-v1:http://localhost:8772:CASE','private-source-record');
  const active=machine(saved,async()=>{});assert.equal(active.pending,null);active.begin(verifyBody());
  const key='watershed-field-v1:http://localhost:8772:CASE';assert.ok(saved.items.has(key));
  saved.setItem(key,'{broken');assert.throws(()=>machine(saved,async()=>{}));assert.equal(saved.getItem(key),'{broken');
  assert.equal(saved.getItem('watershed-current-v1:http://localhost:8772:CASE'),'private-source-record');
});
