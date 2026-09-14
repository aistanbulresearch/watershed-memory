import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import * as currentState from '../watershed_memory/static/current-state.mjs';
import * as fieldState from '../watershed_memory/static/field-state.mjs';
import * as fieldUI from '../watershed_memory/static/field-ui.mjs';
import * as fieldControls from '../watershed_memory/static/field-controls.mjs';
import * as fieldHistory from '../watershed_memory/static/field-history.mjs';
import {snapshot,work,storage,verifiedResponse,verifyBody,clone} from './field-browser-fixtures.mjs';
import {documentFixture,byText,byId} from './field-dom-fixtures.mjs';
const origin='http://localhost:8778';
const settle=()=>new Promise(resolve=>setTimeout(resolve,15));
function eligible() {
  const s=snapshot(),w=work();w.result.verification=null;w.result.verification_level='EVIDENCE_ATTACHED';
  w.field_dimension='VERIFICATION_PENDING';w.available_actions=['VERIFY','CORRECT','ATTACH'];
  const compact=clone(w);delete compact.result.evidence;s.current_field_work.latest_results=[compact];
  return {snapshot:s,selected_field_work:w};
}
function success(body,revision=1) {
  const value=verifiedResponse(revision);value.receipt.request_id=body.request_id;
  value.receipt.case_revision=body.expected_case_revision+1;value.receipt.verification.scope=body.command.scope;
  value.receipt.result.verification.scope=body.command.scope;
  value.snapshot.case.revision=body.expected_case_revision+2;value.snapshot.current_field_work.case_revision=value.snapshot.case.revision;
  if(revision===1){value.selected_field_work.result.verification.scope=body.command.scope;value.snapshot.current_field_work.latest_results[0].result.verification.scope=body.command.scope;}
  return value;
}
function harness({saved=storage(),fresh=()=>eligible(),post=body=>({status:200,value:success(body)}),initial=eligible().snapshot}={}) {
  const document=documentFixture();const calls=[];let ids=0;
  const fetch=async(path,options={})=>{
    calls.push({path,body:options.body});let response;
    if(path==='/api/current/case')response={status:200,value:clone(initial)};
    else if(path.startsWith('/api/current/field-work/'))response={status:200,value:clone(await fresh())};
    else response=await post(JSON.parse(options.body),options.body);
    return {ok:response.status>=200 && response.status<300,status:response.status,json:async()=>response.value??null};
  };
  const source=fs.readFileSync(new URL('../watershed_memory/static/current.js',import.meta.url),'utf8').replace(/^import[^\n]+\n/gm,'');
  const context=vm.createContext({...currentState,...fieldState,...fieldUI,...fieldControls,...fieldHistory,
    document,fetch,sessionStorage:saved,location:{origin},crypto:{randomUUID:()=>`field-request-${++ids}`},Date,URL,encodeURIComponent,setTimeout,console});
  vm.runInContext(source,context);
  return {document,saved,calls,app:document.app,dialog:()=>document.app.find(n=>n.tagName==='DIALOG')};
}
async function openVerify(ui) {
  await settle();const control=byText(ui.app,'Verify report');assert.ok(control);await control.dispatch('click');return ui.dialog();
}
async function submit(ui) { await ui.dialog().querySelector('form').dispatch('submit',{preventDefault(){}}); }

test('current page renders field cards and submits a reviewed exact verification',async()=>{
  const ui=harness();const dialog=await openVerify(ui);assert.ok(dialog);byId(dialog,'scope').value='Reviewed this exact inspection record.';
  await submit(ui);const sent=ui.calls.find(x=>x.path==='/api/current/field-responses');assert.ok(sent);
  const body=JSON.parse(sent.body);assert.equal(body.expected_case_revision,12);assert.deepEqual(body.command.evidence_ids,['evidence-1']);
  assert.match(ui.app.textContent,/Original confirmation/);assert.equal(ui.saved.items.size,0);
});
test('field action refresh detects a stale case and asks for a new review without submitting',async()=>{
  const ui=harness({fresh:()=>{const d=eligible();d.snapshot.case.revision=13;d.snapshot.current_field_work.case_revision=13;return d;}});
  await openVerify(ui);assert.match(ui.dialog().textContent,/changed.*review|review.*changed/i);
  assert.equal(ui.dialog().querySelector('form'),null);assert.equal(ui.calls.some(c=>c.body),false);
});
test('bad selected response pauses mutations without replacing a trusted case',async()=>{
  const ui=harness({fresh:()=>{const d=eligible();d.selected_field_work.plan.plan_id='wrong';return d;}});
  await openVerify(ui);assert.match(ui.app.textContent,/could not|unavailable/i);assert.equal(ui.calls.some(c=>c.body),false);
});
test('lost confirmation survives reload with byte-identical retry and current partial result',async()=>{
  const saved=storage();let firstBytes;
  const ui=harness({saved,post:(_body,bytes)=>{firstBytes=bytes;return {status:503};}});
  const dialog=await openVerify(ui);byId(dialog,'scope').value='Reviewed this exact inspection record.';await submit(ui);
  assert.equal(saved.items.size,1);assert.match(dialog.textContent,/original.*retained|saved response/i);
  const second=harness({saved,post:(body,bytes)=>{assert.equal(bytes,firstBytes);return {status:200,value:success(body,2)};}});
  await settle();await byText(second.app,'Review saved field response').dispatch('click');await submit(second);
  assert.equal(saved.items.size,0);assert.match(second.app.textContent,/current result: report 2, partly completed/i);
});
test('definite rejection unlocks fields for a new explicit identity',async()=>{
  let count=0;const bodies=[];const ui=harness({post:body=>{bodies.push(body);return ++count===1?{status:400}:{status:200,value:success(body)};}});
  const dialog=await openVerify(ui);byId(dialog,'scope').value='Reviewed this exact inspection record.';await submit(ui);
  assert.equal(byId(dialog,'scope').readOnly,false);byId(dialog,'scope').value='Reviewed the field record against the required scope.';await submit(ui);
  assert.equal(bodies.length,2);assert.notEqual(bodies[0].request_id,bodies[1].request_id);assert.equal(ui.saved.items.size,0);
});
test('a saved field response blocks new source and field mutations but permits inspection',async()=>{
  const saved=storage();const machine=fieldState.createFieldResponseMachine({caseId:'CASE',origin,storage:saved,send:()=>{}});machine.begin(verifyBody());
  const ui=harness({saved});await settle();assert.equal(byText(ui.app,'Modify plan').disabled,true);assert.equal(byText(ui.app,'Verify report').disabled,true);
  const inspect=byText(ui.app,'Inspect field record');assert.equal(inspect.disabled,false);await inspect.dispatch('click');
  assert.match(ui.dialog().textContent,/inspection-record-1/);assert.equal(ui.calls.some(c=>c.body),false);
});
test('a saved source response blocks field mutations and corrupt field storage is retained',async()=>{
  const saved=storage();const machine=currentState.createResponseMachine({caseId:'CASE',origin,storage:saved,send:()=>{}});
  machine.begin({request_id:'source-pending',task_id:'review-1',expected_revision:1,action:'CANCEL',note:'',title:null,next_check_at:null});
  const ui=harness({saved});await settle();assert.equal(byText(ui.app,'Verify report').disabled,true);
  const broken=storage();broken.items.set(`watershed-field-v1:${origin}:CASE`,'{bad');
  const ui2=harness({saved:broken});await settle();assert.equal(byText(ui2.app,'Verify report').disabled,true);
  assert.equal(broken.items.size,1);assert.match(ui2.app.textContent,/storage|store/i);
});
test('Escape closes a field modal and restores the focusable case heading after rerender',async()=>{
  const ui=harness();await openVerify(ui);await ui.dialog().dispatch('cancel',{preventDefault(){}});
  assert.equal(ui.dialog(),null);assert.ok(ui.document.activeElement.isConnected);
  assert.equal(ui.document.activeElement.tagName,'H1');
  assert.equal(ui.document.activeElement.id,'case-heading');
  assert.equal(ui.document.activeElement.getAttribute('tabindex'),'-1');
});
test('closing a field inspection restores the case heading after replacing its trigger',async()=>{
  const ui=harness();await settle();const trigger=byText(ui.app,'Inspect field record');
  trigger.focus();await trigger.dispatch('click');await byText(ui.dialog(),'Close').dispatch('click');
  assert.equal(ui.dialog(),null);assert.equal(trigger.isConnected,false);
  assert.equal(ui.document.activeElement.tagName,'H1');
  assert.equal(ui.document.activeElement.id,'case-heading');
});
test('refresh recovers after external reconciliation of corrupt field storage',async()=>{
  const saved=storage(),key=`watershed-field-v1:${origin}:CASE`;saved.items.set(key,'{bad');
  const ui=harness({saved});await settle();assert.equal(byText(ui.app,'Verify report').disabled,true);
  saved.items.delete(key);await byText(ui.app,'Refresh saved case').dispatch('click');
  assert.equal(byText(ui.app,'Verify report').disabled,false);
});
test('inspection can open an unchanged detailed field selection for a new action',async()=>{
  const ui=harness();await settle();await byText(ui.app,'Inspect field record').dispatch('click');
  await byText(ui.dialog(),'Verify report').dispatch('click');assert.ok(ui.dialog().querySelector('form'));
});
test('field detail loading exposes busy state to assistive technology',async()=>{
  let release;const promise=new Promise(resolve=>{release=resolve;});
  const ui=harness({fresh:()=>promise});await settle();
  const opening=byText(ui.app,'Verify report').dispatch('click');
  assert.equal(ui.app.getAttribute('aria-busy'),'true');release(eligible());await opening;
  assert.equal(ui.app.getAttribute('aria-busy'),'false');
});
