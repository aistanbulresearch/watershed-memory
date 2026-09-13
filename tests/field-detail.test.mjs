import assert from 'node:assert/strict';
import test from 'node:test';
import {validFieldDetail, sameDisplayedField, fieldConfirmationText, renderFieldDetail} from '../watershed_memory/static/field-detail.mjs';
import {snapshot,work,verifiedResponse,clone} from './field-browser-fixtures.mjs';
import {documentFixture,elementFor} from './field-dom-fixtures.mjs';
const detail=()=>({snapshot:snapshot(),selected_field_work:work()});
test('selected field detail binds exact visible work to its current case',()=>{
  assert.equal(validFieldDetail(detail(),'CASE','plan-1'),true);
  assert.equal(validFieldDetail(detail(),'OTHER','plan-1'),false);
  assert.equal(validFieldDetail(detail(),'CASE','other'),false);
});
test('selected detail rejects case or summary disagreement',()=>{
  const d=detail(); d.selected_field_work.result.report.summary='A different summary from another result.';
  assert.equal(validFieldDetail(d,'CASE','plan-1'),false);
  const future=detail();future.selected_field_work.plan.revision=13;
  assert.equal(validFieldDetail(future,'CASE','plan-1'),false);
});
test('a non-visible current active plan is rejected',()=>{
  const d=detail();d.selected_field_work.result=null;
  Object.assign(d.selected_field_work.plan,{status:'APPROVED',change_kind:'APPROVE',revision:2});
  d.selected_field_work.field_dimension='PLANNED';d.snapshot.current_field_work.latest_results=[];
  assert.equal(validFieldDetail(d,'CASE','plan-1'),false);
});
test('historical result outside a full bounded result list remains inspectable',()=>{
  const d=detail();d.snapshot.current_field_work.latest_results=[2,3,4].map(n=>{
    const w=work(1,false);w.plan.plan_id=`plan-${n}`;w.result.report.plan_id=`plan-${n}`;
    w.result.report.report_id=`report-${n}`;w.result.verification.report_id=`report-${n}`;
    return w;
  });d.snapshot.current_field_work.has_more_results=true;
  assert.equal(validFieldDetail(d,'CASE','plan-1'),true);
  d.snapshot.current_field_work.has_more_results=false;assert.equal(validFieldDetail(d,'CASE','plan-1'),false);
});
test('selected cancelled work may be absent from active and result lists',()=>{
  const d=detail();d.snapshot.current_field_work.latest_results=[];
  Object.assign(d.selected_field_work.plan,{status:'CANCELLED',change_kind:'CANCEL'});
  d.selected_field_work.result=null;d.selected_field_work.field_dimension='CANCELLED';d.selected_field_work.available_actions=[];
  assert.equal(validFieldDetail(d,'CASE','plan-1'),true);
});
test('display comparison detects changed evidence, permissions and report revision',()=>{
  const summary=work(1,false),full=work();assert.equal(sameDisplayedField(summary,full),true);
  full.available_actions=[];assert.equal(sameDisplayedField(summary,full),false);
  assert.equal(sameDisplayedField(summary,work(2)),false);
  const extra=work();extra.result.evidence_count=2;assert.equal(sameDisplayedField(summary,extra),false);
});
test('old verification confirmation distinguishes the later partial current result',()=>{
  const text=fieldConfirmationText(verifiedResponse(2));
  assert.match(text,/original.*1.*verified/i);assert.match(text,/current.*2.*partly completed/i);
  assert.doesNotMatch(text,/recovered|safe water|all work complete/i);
});
test('detail rendering shows exact evidence provenance separately from reported outcome',()=>{
  const document=documentFixture(),element=elementFor(document);
  const fact=(label,value)=>element('p',`${label}: ${value}`);
  const node=renderFieldDetail(work(),{element,fact,date:x=>x});
  assert.match(node.textContent,/Reported complete/);assert.match(node.textContent,/Verified report/);
  assert.match(node.textContent,/inspection-record-1/);assert.match(node.textContent,/Operator-maintained demonstration record/);
  const dirty=clone(work());dirty.result.report.summary='<script>bad()</script>';
  assert.match(renderFieldDetail(dirty,{element,fact,date:x=>x}).textContent,/<script>/);
});
test('an unchanged detailed selection matches without discarding its evidence identity',()=>{
  assert.equal(sameDisplayedField(work(),work()),true);
  const changed=work();changed.result.evidence[0].provenance='A different record provenance.';
  assert.equal(sameDisplayedField(work(),changed),false);
});
