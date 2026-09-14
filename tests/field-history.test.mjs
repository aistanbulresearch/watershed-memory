import assert from 'node:assert/strict';
import test from 'node:test';
import {renderFieldAssessment} from '../watershed_memory/static/field-history.mjs';
import {context,NOW,plan,spec} from './field-browser-fixtures.mjs';
import {documentFixture,elementFor} from './field-dom-fixtures.mjs';
const helpers=()=>{const element=elementFor(documentFixture());return {element,fact:(k,v)=>element('p',`${k}: ${v}`),date:x=>x};};
const data=()=>({attempt_id:'attempt-1',status:'COMMITTED',finished_at:NOW,context_version:3,case_revision:12,evaluated_at:NOW,assessed_field_context:context(),field_tool_names:['get_field_context','inspect_field_work','stage_field_decision'],
  field_decision:{disposition:'NO_NEW_FIELD_PLAN',basis_plan_id:'plan-1',basis_report_id:'report-1',basis_report_revision:1,basis_verification_level:'VERIFIED',
    reason:'The required inspection is complete and verified.',proposal:null},field_proposal:null});
test('saved assessment shows historical exact result and its effect on the next decision',()=>{
  const node=renderFieldAssessment(data(),'CASE',true,helpers());assert.match(node.textContent,/Historical field evidence/);
  assert.match(node.textContent,/Report revision: 1/);assert.match(node.textContent,/Verified report/);
  assert.match(node.textContent,/No new field plan/);assert.match(node.textContent,/inspection is complete and verified/);
});
test('legacy and failed field assessments do not invent a successful field decision',()=>{
  assert.equal(renderFieldAssessment({},'CASE',true,helpers()),null);
  const d=data();d.field_decision=null;assert.match(renderFieldAssessment(d,'CASE',true,helpers()).textContent,/No field decision was saved/);
});
test('history rejects mismatched case, time or decision result basis',()=>{
  assert.throws(()=>renderFieldAssessment(data(),'OTHER',true,helpers()));
  const d=data();d.evaluated_at='2026-09-14T12:00:00Z';assert.throws(()=>renderFieldAssessment(d,'CASE',true,helpers()));
  const r=data();r.field_decision.basis_report_revision=2;assert.throws(()=>renderFieldAssessment(r,'CASE',true,helpers()));
});
test('historical field tools remain distinct from source tools',()=>{
  const node=renderFieldAssessment(data(),'CASE',true,helpers());assert.match(node.textContent,/Field tools/);
  const d=data();d.field_tool_names=['invented_tool'];assert.throws(()=>renderFieldAssessment(d,'CASE',true,helpers()));
});
function proposed() {
  const d=data();d.field_decision.disposition='PROPOSE_FIELD_PLAN';
  d.field_decision.proposal={task_id:'review-1',review_revision:1,location_id:'site-1',location_revision:1,spec:spec()};
  d.field_proposal={...plan(),plan_id:'new-field-plan',revision:1,status:'PROPOSED',change_kind:'PROPOSE',author_kind:'AGENT',recorded_by:'attempt-1'};
  return d;
}
test('committed agent follow-through shows the exact saved proposal as a separate fact',()=>{
  const node=renderFieldAssessment(proposed(),'CASE',true,helpers());assert.match(node.textContent,/Saved agent proposal/);
});
for(const mutate of [
  d=>{d.field_proposal=null;},d=>{d.field_proposal.recorded_by='other-attempt';},d=>{d.field_proposal.spec.purpose='A different inspection purpose.';},
  d=>{d.field_proposal.location.label='Changed site meaning';},d=>{d.field_tool_names=['stage_field_decision'];},
  d=>{d.field_tool_names=['get_field_context','stage_field_decision','stage_field_decision'];},
])test('invalid committed field decision/proposal provenance is rejected',()=>{
  const d=proposed();mutate(d);assert.throws(()=>renderFieldAssessment(d,'CASE',true,helpers()));
});
test('stale field decision can retain its assessment proposal without a saved plan',()=>{
  const d=proposed();d.status='STALE';d.field_proposal=null;
  const node=renderFieldAssessment(d,'CASE',true,helpers());assert.doesNotMatch(node.textContent,/Saved agent proposal/);
});
test('a non-proposing decision cannot carry a saved agent plan',()=>{
  const d=data();d.field_proposal=proposed().field_proposal;assert.throws(()=>renderFieldAssessment(d,'CASE',true,helpers()));
});
for(const key of ['assessed_field_context','field_decision','field_tool_names','field_proposal'])test(`legacy ${key} must have its exact null/empty shape`,()=>{
  const d={context_version:2,assessed_field_context:null,field_decision:null,field_tool_names:[],field_proposal:null};
  assert.equal(renderFieldAssessment(d,'CASE',true,helpers()),null);d[key]='';assert.throws(()=>renderFieldAssessment(d,'CASE',true,helpers()));
});
test('saved proposal time is the exact commit time after a nonzero inference interval',()=>{
  const d=proposed();d.finished_at='2026-09-13T12:05:00.123456Z';d.field_proposal.created_at=d.finished_at;d.field_proposal.updated_at=d.finished_at;
  assert.ok(renderFieldAssessment(d,'CASE',true,helpers()));
  d.field_proposal.created_at='2026-09-13T12:05:00.123455Z';d.field_proposal.updated_at=d.field_proposal.created_at;
  assert.throws(()=>renderFieldAssessment(d,'CASE',true,helpers()));
});
