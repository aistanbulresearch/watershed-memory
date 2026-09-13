import assert from 'node:assert/strict';
import test from 'node:test';
const {renderFieldPanel} = await import(process.env.WATERSHED_FIELD_UI_MODULE || '../watershed_memory/static/field-ui.mjs');
import {clone,context,plan,snapshot,work} from './field-browser-fixtures.mjs';

function setup(data=snapshot(), blocked=false) {
  const calls=[];
  const element=(tag,text=null,className='')=>({tag,text:text??'',className,children:[],disabled:false,
    append(...children){this.children.push(...children);},setAttribute(name,value){this[name]=value;}});
  const button=(text,handler,secondary=false)=>({...element('button',text,secondary?'button secondary':'button'),handler});
  const panel=(title,detail='')=>{const v=element('section');v.append(element('h2',title),element('p',detail));return v;};
  const fact=(label,value)=>{const v=element('div');v.append(element('small',label),element('strong',value));return v;};
  return {calls,node:renderFieldPanel(data,{element,button,panel,fact,date:value=>value,blocked,
    onAction:args=>calls.push(args),onInspect:id=>calls.push({inspect:id})})};
}
const all=node=>node?[node,...node.children.flatMap(all)]:[];
const content=node=>all(node).map(n=>n.text).join(' ');
test('legacy source-only desk does not invent a field panel',()=>{
  const data=snapshot();delete data.current_field_work;assert.equal(setup(data).node,null);
});
test('empty field case shows an honest next step and server-supplied proposal target',()=>{
  const data=snapshot();data.current_field_work={...context(),state:'EMPTY',latest_results:[]};
  const ui=setup(data);assert.match(content(ui.node),/No field work has been proposed/);
  const propose=all(ui.node).find(n=>n.tag==='button' && n.text==='Plan field work: Follow watershed evidence');
  assert.ok(propose);propose.handler();assert.equal(ui.calls[0].action,'PROPOSE');
  assert.equal(ui.calls[0].target.task_id,'review-1');assert.equal(ui.calls[0].caseRevision,12);
});
test('verified partial result still says partly completed',()=>{
  const data=snapshot();const partial=work(1,false);partial.result.report.outcome='PARTIAL';partial.field_dimension='PLANNED';
  data.current_field_work.latest_results=[partial];const text=content(setup(data).node);
  assert.match(text,/Partly completed/);assert.match(text,/Verified report/);assert.doesNotMatch(text,/All work complete|Watershed recovered|Reported complete/);
});
test('a result exposes only server-provided actions and an inspect control',()=>{
  const data=snapshot();data.current_field_work.proposal_targets=[];
  data.current_field_work.latest_results[0].available_actions=['CORRECT'];const ui=setup(data);
  const controls=all(ui.node).filter(n=>n.tag==='button');
  assert.deepEqual(controls.map(n=>n.text).sort(),['Correct result','Inspect field record']);
  controls.find(n=>n.text==='Correct result').handler();assert.equal(ui.calls[0].action,'CORRECT');
  assert.equal(ui.calls[0].work.plan.plan_id,'plan-1');assert.equal(ui.calls[0].caseRevision,12);
  controls.find(n=>n.text==='Inspect field record').handler();assert.deepEqual(ui.calls[1],{inspect:'plan-1'});
});
test('earlier parent review is visible and does not invent approval permission',()=>{
  const data=snapshot();const old={...work(),plan:{...plan(),status:'PROPOSED',change_kind:'PROPOSE',revision:1},
    result:null,parent_binding:'SUPERSEDED',field_dimension:'NOT_PLANNED',available_actions:['CANCEL']};
  data.current_field_work.stranded_plans=[old];data.current_field_work.latest_results=[];data.current_field_work.proposal_targets=[];
  const ui=setup(data);assert.match(content(ui.node),/Earlier review/);
  assert.deepEqual(all(ui.node).filter(n=>n.tag==='button').map(n=>n.text).sort(),['Cancel field plan','Inspect field record']);
});
test('pending or busy page blocks mutations but leaves inspection available',()=>{
  const ui=setup(snapshot(),true);const controls=all(ui.node).filter(n=>n.tag==='button');
  for(const control of controls) assert.equal(control.disabled,control.text!=='Inspect field record');
});
test('truncated history is described without claiming all work is shown',()=>{
  const data=snapshot();data.current_field_work.has_more_results=true;data.current_field_work.has_more_stranded_plans=true;
  assert.match(content(setup(data).node),/More field history/);
});
test('a form handoff captures rendered revision and records before later mutation',()=>{
  const data=snapshot(),ui=setup(data);data.case.revision=99;
  data.current_field_work.case_revision=99;
  data.current_field_work.latest_results[0].result.report.summary='Changed later';
  all(ui.node).find(n=>n.text==='Correct result').handler();
  assert.equal(ui.calls[0].caseRevision,12);assert.notEqual(ui.calls[0].work.result.report.summary,'Changed later');
});
test('a proposed field form captures the target before later source changes',()=>{
  const data=snapshot(),ui=setup(data);data.current_field_work.case_revision=99;
  data.current_field_work.proposal_targets[0].task_id='changed-later';
  all(ui.node).find(n=>n.text==='Plan field work: Follow watershed evidence').handler();
  assert.equal(ui.calls[0].caseRevision,12);assert.equal(ui.calls[0].target.task_id,'review-1');
});
test('field cards lead with the purpose and do not use opaque identifiers as headlines',()=>{
  const ui=setup(snapshot());const cards=all(ui.node).filter(n=>n.tag==='article');assert.equal(cards.length,1);
  const headings=all(ui.node).filter(n=>n.tag==='h3');
  assert.ok(headings.some(n=>n.text==='Inspect the accessible sediment marker.'));
  assert.ok(headings.every(n=>n.text!=='plan-1'));
});
test('modifying field work explicitly includes approval in its action label',()=>{
  const data=snapshot();data.current_field_work.latest_results=[];
  data.current_field_work.current_plans=[{...work(),plan:{...plan(),status:'PROPOSED',change_kind:'PROPOSE',revision:1},
    result:null,field_dimension:'NOT_PLANNED',available_actions:['MODIFY']}];
  const ui=setup(data),control=all(ui.node).find(n=>n.text==='Modify and approve plan');
  assert.ok(control);control.handler();assert.equal(ui.calls[0].action,'MODIFY');
});
