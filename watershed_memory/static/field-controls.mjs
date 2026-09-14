import {createFieldForm} from './field-forms.mjs';
import {validSnapshot} from './current-state.mjs';
import {validFieldDetail,sameDisplayedField,fieldConfirmationText,renderFieldDetail} from './field-detail.mjs';

const copy=v=>JSON.parse(JSON.stringify(v));
const LABELS={PROPOSE:'Plan field work',APPROVE:'Approve field plan',MODIFY:'Modify and approve plan',
  DEFER:'Defer field plan',CANCEL:'Cancel field plan',REPORT:'Record result',CORRECT:'Correct result',ATTACH:'Attach evidence',VERIFY:'Verify report'};

export function createFieldControls({state,app,element,button,fact,date,request,render,load,blocked,newId}) {
  const document=app.ownerDocument;
  const busy=()=>Boolean(state.machine?.busy || state.fieldMachine?.busy);
  const focusFallback=()=>{
    const heading=document.querySelector('#case-heading');
    if(heading){heading.focus();return;}
    document.querySelector('#refresh-case')?.focus();
  };
  function modal(title,originalFocus) {
    const dialog=element('dialog',null,'drawer'),card=element('div',null,'drawer-card');
    const heading=element('h2',title);heading.id='field-heading';dialog.setAttribute('aria-labelledby','field-heading');
    card.append(heading);dialog.append(card);app.append(dialog);state.dialog=dialog;
    const fields=[];
    function close() {
      if(busy())return;
      dialog.close();dialog.remove();state.dialog=null;render();
      if(originalFocus?.isConnected)originalFocus.focus();else focusFallback();
    }
    dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
    dialog.addEventListener('keydown',event=>{
      if(event.key!=='Tab')return;
      const active=fields.filter(n=>!n.disabled),first=active[0],last=active.at(-1);
      if(event.shiftKey && document.activeElement===first){event.preventDefault();last?.focus();}
      else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first?.focus();}
    });
    return {dialog,card,fields,close,show(focus){dialog.showModal();focus?.focus();}};
  }
  function inspectView(work,originalFocus,message=null) {
    const view=modal('Current field record',originalFocus);
    if(message)view.card.append(element('p',message,'notice'));
    view.card.append(renderFieldDetail(work,{element,fact,date}));
    const controls=element('div',null,'actions');
    // Stale forms require returning to the refreshed case before a new decision.
    if(!message && !blocked())for(const action of work.available_actions) {
      const intent={action,caseRevision:state.data.case.revision,work:copy(work)};
      const control=button(LABELS[action],()=>{view.close();return begin(intent);});controls.append(control);view.fields.push(control);
    }
    const close=button('Close',view.close,true);controls.append(close);view.fields.push(close);view.card.append(controls);view.show(close);
  }
  function pendingDescription(body) {
    const node=element('section'),c=body.command;
    node.append(element('p','This is the original saved response. Retrying sends it unchanged.'),
      fact('Action',LABELS[c.operation==='DECIDE'?c.action:c.operation]),fact('Case revision reviewed',body.expected_case_revision));
    if(c.spec)node.append(fact('Purpose',c.spec.purpose),fact('Assigned role',c.spec.assignee_role),
      fact('Planned window',`${date(c.spec.window_start)} – ${date(c.spec.window_end)}`),fact('Site reference',c.location_id));
    if(c.summary)node.append(fact('Reported outcome',c.outcome),element('p',c.summary),fact('Performed',`${date(c.performed_start)} – ${date(c.performed_end)}`));
    if(c.reference)node.append(fact('Evidence reference',c.reference),fact('Provenance',c.provenance));
    if(c.scope)node.append(fact('Verification scope',c.scope));
    if(c.report_id)node.append(fact('Report revision',c.expected_report_revision));
    if(c.defer_until)node.append(fact('Deferred until',date(c.defer_until)));
    for(const id of c.evidence_ids || [])node.append(fact('Evidence record',id));
    return node;
  }
  function formView(intent,originalFocus,pending=null) {
    const ui=pending?null:createFieldForm(intent,state.data,{element,date});
    const view=modal(pending?'Confirm the saved field response':ui.title,originalFocus);
    const form=ui?.form || element('form');if(pending)form.append(pendingDescription(pending));
    const feedback=element('p','','notice');feedback.hidden=true;feedback.setAttribute('role','alert');
    const controls=element('div',null,'actions'),close=button('Close',view.close,true);
    const submit=element('button',pending?'Retry exact saved field response':ui.submitLabel,'button');submit.type='submit';
    controls.append(close,submit);form.append(feedback,controls);view.card.append(form);view.fields.push(...(ui?.fields || []),close,submit);
    const showError=text=>{feedback.textContent=text;feedback.hidden=false;};
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(busy())return;
      feedback.hidden=true;state.notice=null;
      if(!state.fieldMachine.pending) {
        if(!ui){view.close();state.notice='The saved response was rejected. Review the current record before entering a corrected response.';await load();return;}
        try {state.fieldMachine.begin(ui.build(newId()));}
        catch(error){showError(error.message || 'The field response could not be saved in this tab. Nothing was submitted.');return;}
      }
      ui?.freeze(true);submit.disabled=true;close.disabled=true;
      const outcome=await state.fieldMachine.attempt();submit.disabled=false;close.disabled=false;submit.focus();
      if(outcome.kind==='success') {
        state.data=outcome.result.snapshot;state.error=null;state.notice=fieldConfirmationText(outcome.result);
        state.fieldReceiptPlanId=outcome.result.selected_field_work.plan.plan_id;
        view.close();
      } else if(outcome.kind==='conflict') {
        view.close();state.notice='The field record changed while this response was open. Review the refreshed record before deciding again.';await load();focusFallback();
      } else if(outcome.kind==='rejected') {
        if(!ui){view.close();state.notice='The saved response was rejected. Review the current field record before entering a corrected response.';await load();}
        else {ui.freeze(false);submit.textContent='Save corrected field response';showError('The action was not accepted. Check the fields, approved site and times before submitting again.');}
      } else {
        submit.textContent='Retry exact saved field response';close.textContent='Close; keep saved response';
        showError('Confirmation was interrupted. The original response is retained; retry it exactly or close and return to it.');
      }
    });
    view.show(pending?submit:ui.fields[0] || submit);
  }
  async function fetchSelected(planId) {
    const value=await request(`/api/current/field-work/${encodeURIComponent(planId)}`);
    if(!validFieldDetail(value,state.data.case.case_id,planId))throw new Error('Invalid field detail');
    return copy(value);
  }
  async function begin(input) {
    if(state.dialog || blocked())return;
    const intent=copy(input),originalFocus=document.activeElement;
    state.fieldLoading=true;render();
    try {
      let fresh,selected=null;
      if(intent.action==='PROPOSE') {
        fresh=await request('/api/current/case');
        if(!validSnapshot(fresh,state.data.case.case_id) || !fresh.current_field_work)throw new Error('Invalid field snapshot');
      } else {const value=await fetchSelected(intent.work.plan.plan_id);fresh=value.snapshot;selected=value.selected_field_work;}
      const changed=fresh.case.revision!==intent.caseRevision || (selected && !sameDisplayedField(intent.work,selected));
      state.data=fresh;state.fieldLoading=false;state.error=null;render();
      if(changed) {
        const message='The field record changed. Review the current details before choosing an action.';
        if(selected)inspectView(selected,originalFocus,message);else {state.notice=message;render();focusFallback();}
        return;
      }
      if(selected)intent.work=selected;
      formView(intent,originalFocus);
    } catch (_) {
      state.fieldLoading=false;state.error='The current field record could not be loaded. Refresh before making a new decision.';render();focusFallback();
    }
  }
  return {
    action:begin,
    async inspect(planId) {
      if(state.dialog || state.loading || state.fieldLoading || busy())return;
      const originalFocus=document.activeElement;state.fieldLoading=true;render();
      try {const value=await fetchSelected(planId);state.data=value.snapshot;state.fieldLoading=false;render();inspectView(value.selected_field_work,originalFocus);}
      catch (_){state.fieldLoading=false;state.error='The field record is unavailable. Refresh the saved case and try again.';render();focusFallback();}
    },
    restore() {
      if(state.dialog || state.loading || state.fieldLoading || busy() || !state.fieldMachine?.pending)return;
      formView(null,document.activeElement,state.fieldMachine.pending);
    },
  };
}
