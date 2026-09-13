import {validFieldCommand, validFieldReceipt, validFieldWork} from './field-records.mjs';
import {validSnapshot} from './current-state.mjs';

const clone = value => JSON.parse(JSON.stringify(value));
const same = (a,b) => {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
  return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k=>Object.hasOwn(b,k) && same(a[k],b[k]));
};
function compact(work) {
  const value=clone(work);
  if (value.result) delete value.result.evidence;
  return value;
}
function confirmed(value,body,caseId) {
  const receipt=value?.receipt, snapshot=value?.snapshot, current=value?.selected_field_work;
  if (!validSnapshot(snapshot,caseId) || !snapshot.current_field_work ||
    !validFieldReceipt(receipt,body,caseId,snapshot.case.simulated) || snapshot.case.revision < receipt.case_revision ||
    !validFieldWork(current,caseId,snapshot.case.simulated,snapshot.evaluated_at,{detail:true,caseRevision:snapshot.case.revision})) return false;
  const old=receipt.plan, latest=current.plan;
  if (latest.plan_id !== old.plan_id || latest.task_id !== old.task_id || latest.review_revision !== old.review_revision ||
    latest.created_at !== old.created_at || latest.revision < old.revision ||
    (latest.revision === old.revision && !same(latest,old)) || (old.status === 'CANCELLED' && !same(latest,old))) return false;
  if (receipt.result) {
    const saved=receipt.result, now=current.result;
    if (!now || saved.report.report_id !== now.report.report_id || saved.report.performed_plan_revision !== now.report.performed_plan_revision ||
      now.report.revision < saved.report.revision) return false;
    if (now.report.revision === saved.report.revision && (!same(now.report,saved.report) ||
      saved.evidence.some(e=>!now.evidence.some(n=>same(e,n))))) return false;
  }
  const field=snapshot.current_field_work;
  const visible=[...field.current_plans,...field.stranded_plans,...field.latest_results].find(w=>w.plan.plan_id === latest.plan_id);
  if (visible) return same(visible,compact(current));
  // Cancelled plans are deliberately absent; only historical partitions truncate.
  if (latest.status === 'CANCELLED') return true;
  if (latest.status === 'REPORTED') return field.has_more_results;
  return current.parent_binding !== 'CURRENT' && field.has_more_stranded_plans;
}
export function createFieldResponseMachine({caseId,storage,send,origin}) {
  if (typeof caseId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(caseId) || typeof origin !== 'string' || typeof send !== 'function')
    throw new Error('Invalid field response configuration.');
  const key=`watershed-field-v1:${origin}:${caseId}`;
  let pending=null, busy=false;
  const saved=storage.getItem(key);
  if (saved !== null) {
    const record=JSON.parse(saved);
    if (!record || Object.keys(record).length !== 3 || record.version !== 1 || record.caseId !== caseId ||
      typeof record.serialized !== 'string' || !validFieldCommand(JSON.parse(record.serialized))) throw new Error('The saved field response needs local reconciliation.');
    pending=record;
  }
  function clear() { storage.removeItem(key); pending=null; }
  return {
    get pending() { return pending === null ? null : JSON.parse(pending.serialized); },
    get busy() { return busy; },
    begin(body) {
      if (pending || busy) throw new Error('Finish the saved field response first.');
      if (!validFieldCommand(body)) throw new Error('Check the field response.');
      const record={version:1,caseId,serialized:JSON.stringify(body)};
      storage.setItem(key,JSON.stringify(record));
      pending=record;
    },
    async attempt() {
      if (!pending || busy) throw new Error('No available saved field response.');
      busy=true;
      try {
        let result;
        try { result=await send(pending.serialized); }
        catch (error) {
          if (Number.isInteger(error?.status) && error.status >= 400 && error.status < 500 && ![408,429].includes(error.status)) {
            try { clear(); } catch { return {kind:'ambiguous'}; }
            return {kind:error.status === 409?'conflict':'rejected'};
          }
          return {kind:'ambiguous'};
        }
        try {
          if (!confirmed(result,JSON.parse(pending.serialized),caseId)) return {kind:'ambiguous'};
          const copied=clone(result);
          clear(); return {kind:'success',result:copied};
        } catch { return {kind:'ambiguous'}; }
      } finally { busy=false; }
    },
  };
}
