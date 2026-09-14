import assert from 'node:assert/strict';
import test from 'node:test';
import {createFieldForm} from '../watershed_memory/static/field-forms.mjs';
import {validFieldCommand} from '../watershed_memory/static/field-records.mjs';
import {clone,snapshot,work,site,NOW,START,END} from './field-browser-fixtures.mjs';
import {documentFixture,elementFor,byId,localInput} from './field-dom-fixtures.mjs';

function setup(action,mutate=()=>{}) {
  const data=snapshot(); let selected=work(); selected.available_actions=[action];
  if(['APPROVE','MODIFY','DEFER','CANCEL','REPORT'].includes(action)) {
    selected.plan.status=action==='REPORT'?'APPROVED':'PROPOSED';
    selected.plan.revision=action==='REPORT'?2:1; selected.plan.change_kind=action==='REPORT'?'APPROVE':'PROPOSE';
    selected.result=null; selected.field_dimension=action==='REPORT'?'PLANNED':'NOT_PLANNED';
    data.current_field_work.latest_results=[]; data.current_field_work.current_plans=[clone(selected)];
    data.current_field_work.proposal_targets=[];
  } else if(action==='PROPOSE') {
    data.current_field_work.latest_results=[]; data.current_field_work.state='EMPTY'; selected=null;
  } else if(action==='VERIFY') {
    selected.result.verification=null; selected.result.verification_level='EVIDENCE_ATTACHED'; selected.field_dimension='VERIFICATION_PENDING';
    const compact=clone(selected); delete compact.result.evidence; data.current_field_work.latest_results=[compact];
  } else {
    const compact=clone(selected); delete compact.result.evidence; data.current_field_work.latest_results=[compact];
  }
  const intent={action,caseRevision:12,...(selected?{work:selected}:{target:clone(data.current_field_work.proposal_targets[0])})};
  mutate(data,intent);
  const document=documentFixture(); let instant=Date.parse(NOW);
  const ui=createFieldForm(intent,data,{element:elementFor(document),date:x=>x,now:()=>instant});
  return {ui,data,intent,document,at:value=>{instant=Date.parse(value);},input:id=>byId(ui.form,id)};
}
function fillPlan(s) {
  s.input('activity').value='VISUAL_INSPECTION'; s.input('activity').dispatch('change'); s.input('location').value='site-1';
  s.input('purpose').value='Inspect the accessible sediment marker.'; s.input('assignee').value='Source-water team';
  s.input('window-start').value=localInput(START); s.input('window-end').value=localInput(END);
  s.input('evidence-INSPECTION_RECORD_REFERENCE').checked=true;
}
function fillReport(s) {
  s.input('outcome').value='PARTIAL'; s.input('summary').value='Only the accessible marker could be inspected.';
  s.input('performed-start').value=localInput(NOW); s.input('performed-end').value=localInput(NOW);
}
function built(s) { const body=s.ui.build('request-form'); assert.equal(validFieldCommand(body),true); return body; }

test('proposal captures exact displayed parent/case/site with a complete typed body',()=>{
  const s=setup('PROPOSE'); fillPlan(s); s.data.case.revision=99;
  s.data.current_field_work.approved_locations[0].revision=8; s.intent.target.revision=99;
  const b=built(s); assert.equal(b.expected_case_revision,12); assert.equal(b.command.expected_review_revision,1);
  assert.equal(b.command.location_revision,1); assert.equal(b.command.task_id,'review-1');
  assert.deepEqual(Object.keys(b).sort(),['command','expected_case_revision','request_id']);
  assert.ok(!/private_note|principal|simulated|recorded_by/.test(JSON.stringify(b)));
});
test('modify uses its captured plan and explicitly approves the changed work',()=>{
  const s=setup('MODIFY'); fillPlan(s); s.intent.work.plan.revision=99;
  assert.equal(s.ui.title,'Modify and approve plan'); assert.equal(s.ui.submitLabel,'Modify and approve plan');
  assert.match(s.ui.form.textContent,/approv/i); const b=built(s);
  assert.equal(b.command.operation,'MODIFY'); assert.equal(b.command.expected_plan_revision,1);
});
for(const action of ['APPROVE','CANCEL','DEFER']) test(`${action} sends the exact decision and only relevant fields`,()=>{
  const s=setup(action); if(action==='DEFER')s.input('defer-until').value=localInput(START);
  assert.deepEqual(built(s).command,{operation:'DECIDE',plan_id:'plan-1',expected_plan_revision:1,action,defer_until:action==='DEFER'?new Date(START).toISOString():null});
  assert.match(s.ui.form.textContent,/accessible sediment marker/); assert.match(s.ui.form.textContent,/Demonstration inspection site/);
});
for(const action of ['REPORT','CORRECT']) test(`${action} accepts performance after approval even outside the planned window`,()=>{
  const s=setup(action); fillReport(s); const b=built(s); assert.equal(b.command.operation,action);
  assert.equal(b.command.outcome,'PARTIAL'); assert.equal(b.command.summary,'Only the accessible marker could be inspected.');
  if(action==='REPORT')assert.equal(b.command.performed_plan_revision,2);
  else { assert.equal(b.command.expected_report_revision,1); assert.match(s.ui.form.textContent,/evidence|verif/i); }
});
test('attachment records provenance with optional fields null and no authority',()=>{
  const s=setup('ATTACH'); s.input('reference-kind').value='OPERATOR_RECORD_REFERENCE';
  s.input('category').value='INSPECTION_RECORD_REFERENCE'; s.input('reference').value='inspection-2026-001';
  s.input('provenance').value='Logged by the source-water field team.';
  assert.deepEqual(built(s).command,{operation:'ATTACH',report_id:'report-1',expected_report_revision:1,
    reference_kind:'OPERATOR_RECORD_REFERENCE',evidence_category:'INSPECTION_RECORD_REFERENCE',reference:'inspection-2026-001',
    provenance:'Logged by the source-water field team.',observed_at:null,sha256:null});
  assert.equal(s.ui.fields.some(n=>n.type==='file'),false);
});
test('verification shows and captures the full evidence set of the selected report',()=>{
  const s=setup('VERIFY'); s.input('scope').value='Reviewed this exact inspection record.';
  s.intent.work.result.evidence[0].evidence_id='changed-later';
  const b=built(s); assert.deepEqual(b.command.evidence_ids,['evidence-1']);
  assert.equal(b.command.expected_report_revision,1); assert.match(s.ui.form.textContent,/inspection-record-1/);
  assert.match(s.ui.form.textContent,/Operator-maintained demonstration record/);
});
test('form construction rejects a stale displayed case revision',()=>{
  assert.throws(()=>setup('PROPOSE',(data)=>{data.case.revision=13;}));
});
test('form construction rejects a made-up proposal target or action absent from server permissions',()=>{
  assert.throws(()=>setup('PROPOSE',(_,intent)=>{intent.target.task_id='invented';}));
  assert.throws(()=>setup('REPORT',(_,intent)=>{intent.work.available_actions=[];}));
});
test('a site cannot be selected for an activity it does not support',()=>{
  const s=setup('PROPOSE'); fillPlan(s); s.input('activity').value='SAMPLING'; assert.throws(()=>s.ui.build('bad-site'));
});
test('a typed arbitrary location cannot replace an approved site',()=>{
  const s=setup('PROPOSE'); fillPlan(s); s.input('location').value='unknown'; assert.throws(()=>s.ui.build('bad-site'));
});
test('an approved alternate site supports its own selected activity',async()=>{
  const s=setup('PROPOSE',data=>data.current_field_work.approved_locations.push({...site(),location_id:'sampling-site',activities:['SAMPLING']}));
  fillPlan(s); s.input('activity').value='SAMPLING'; await s.input('activity').dispatch('change');
  s.input('location').value='sampling-site'; assert.equal(built(s).command.location_id,'sampling-site');
});
test('planning fields remain explicit and cannot submit without purpose/team/evidence',()=>{
  const s=setup('PROPOSE'); assert.throws(()=>s.ui.build('empty'));
  fillPlan(s); s.input('evidence-INSPECTION_RECORD_REFERENCE').checked=false; assert.throws(()=>s.ui.build('no-evidence'));
});
test('window and deferral reject past, reversed and beyond-bound times',()=>{
  const s=setup('PROPOSE'); fillPlan(s); s.input('window-start').value=localInput('2026-09-12T12:00:00Z'); assert.throws(()=>s.ui.build('past'));
  fillPlan(s); s.input('window-end').value=localInput('2026-11-01T12:00:00Z'); assert.throws(()=>s.ui.build('too-far'));
  const d=setup('DEFER'); d.input('defer-until').value=localInput(END); assert.throws(()=>d.ui.build('too-late'));
});
test('report rejects future completion and observed evidence cannot be from the future',()=>{
  const s=setup('REPORT'); fillReport(s); s.input('performed-end').value=localInput(START); assert.throws(()=>s.ui.build('future'));
  const e=setup('ATTACH'); e.input('reference-kind').value='OPERATOR_RECORD_REFERENCE';
  e.input('category').value='INSPECTION_RECORD_REFERENCE';e.input('reference').value='sample-1';e.input('provenance').value='Operator-maintained record.';
  e.input('observed-at').value=localInput(START); assert.throws(()=>e.ui.build('future-evidence'));
});
test('invalid external references and unprintable summaries are rejected before submission',()=>{
  const e=setup('ATTACH'); e.input('reference-kind').value='EXTERNAL_REFERENCE'; e.input('category').value='PHOTO_REFERENCE';
  e.input('reference').value='javascript:alert(1)';e.input('provenance').value='Operator-maintained record.';assert.throws(()=>e.ui.build('unsafe'));
  const s=setup('CORRECT');fillReport(s);s.input('summary').value='An actual\nnewline';assert.throws(()=>s.ui.build('newline'));
});
test('freeze and unfreeze cover selects, checkboxes and text fields',()=>{
  const s=setup('PROPOSE'); s.ui.freeze(true);
  for(const n of s.ui.fields)assert.ok(n.disabled || n.readOnly);
  s.ui.freeze(false);for(const n of s.ui.fields){assert.equal(n.disabled,false);assert.equal(n.readOnly,false);}
});
test('all form controls have visible associated labels; times are explicitly local',()=>{
  for(const action of ['PROPOSE','MODIFY','DEFER','REPORT','CORRECT','ATTACH','VERIFY']) {
    const s=setup(action),labels=s.ui.form.findAll(n=>n.tagName==='LABEL');
    for(const field of s.ui.fields)assert.ok(labels.some(n=>n.htmlFor===field.id && n.textContent.length),`${action}:${field.id}`);
    for(const field of s.ui.fields.filter(n=>n.type==='datetime-local'))assert.match(labels.find(n=>n.htmlFor===field.id).textContent,/local time/i);
  }
});
test('unchanged precise report times are preserved during correction',()=>{
  const stamp='2026-09-13T11:59:59.123456Z';
  const s=setup('CORRECT',(data,intent)=>{
    intent.work.result.report.performed_start=stamp;intent.work.result.report.performed_end=stamp;
    data.current_field_work.latest_results[0].result.report.performed_start=stamp;
    data.current_field_work.latest_results[0].result.report.performed_end=stamp;
  });
  const b=built(s); assert.equal(b.command.performed_start,stamp);assert.equal(b.command.performed_end,stamp);
});
test('fields use real HTML controls and select options have readable labels',()=>{
  const s=setup('PROPOSE');
  for(const n of s.ui.fields)assert.ok(['INPUT','SELECT','TEXTAREA'].includes(n.tagName),n.tagName);
  assert.ok(s.input('activity').options.length>0);
  for(const option of s.input('activity').options)assert.equal(option.tagName,'OPTION');
  s.input('activity').value='VISUAL_INSPECTION';
  return s.input('activity').dispatch('change').then(()=>{
    assert.ok(s.input('location').options.some(o=>o.value==='site-1' && o.textContent==='Demonstration inspection site'));
  });
});
test('the intent case revision is captured before an outside mutation',()=>{
  const s=setup('CANCEL');s.intent.caseRevision=99;assert.equal(built(s).expected_case_revision,12);
});
test('MODIFY preselects existing required evidence rather than silently dropping it',()=>{
  const s=setup('MODIFY');assert.equal(s.input('evidence-INSPECTION_RECORD_REFERENCE').checked,true);
  assert.deepEqual(built(s).command.spec.required_evidence,['INSPECTION_RECORD_REFERENCE']);
});
test('REPORT refuses a performed start earlier than the saved approval',()=>{
  const s=setup('REPORT');fillReport(s);s.input('performed-start').value=localInput('2026-09-13T11:00:00Z');
  assert.throws(()=>s.ui.build('before-approval'));
});
test('VERIFY and approval confirmation visibly identify the exact result or planned window',()=>{
  const s=setup('VERIFY');assert.match(s.ui.form.textContent,/result revision 1|report revision 1/i);
  const a=setup('APPROVE');assert.match(a.ui.form.textContent,/2026-09-13T13:00:00Z/);assert.match(a.ui.form.textContent,/2026-09-13T14:00:00Z/);
});
test('a self-declared selected action cannot target work absent from this snapshot',()=>{
  assert.throws(()=>setup('APPROVE',(_,intent)=>{intent.work.plan.plan_id='not-in-current-snapshot';}));
});
test('freezing uses the native select disabled state',()=>{
  const s=setup('PROPOSE');s.ui.freeze(true);
  for(const n of s.ui.fields.filter(n=>n.tagName==='SELECT'))assert.equal(n.disabled,true);
});
test('site labels cannot alias another approved site identity',()=>{
  const s=setup('PROPOSE',data=>{
    data.current_field_work.approved_locations[0].label='other-site';
    data.current_field_work.approved_locations.push({...site(),location_id:'other-site',label:'A second inspection point'});
  });fillPlan(s);s.input('location').value='other-site';assert.equal(built(s).command.location_id,'other-site');
});
test('new report outcome is an explicit choice and correction shows its revision',()=>{
  const s=setup('REPORT');assert.equal(s.input('outcome').value,'');
  assert.match(setup('CORRECT').ui.form.textContent,/report revision 1/i);
});
test('editable fields use the existing layout and support second precision in native time inputs',()=>{
  const s=setup('PROPOSE');for(const field of s.ui.fields)assert.match(field.parentNode.className,/field/);
  for(const field of s.ui.fields.filter(n=>n.type==='datetime-local'))assert.equal(field.step,'0.001');
});
test('MODIFY retains its approved site after native select options are populated',()=>{
  const s=setup('MODIFY');assert.equal(s.input('location').value,'site-1');assert.equal(built(s).command.location_id,'site-1');
});
