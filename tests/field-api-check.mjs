/* Validate real Python HTTP output with the same modules loaded by the desk. */
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
let input='';
for await (const part of process.stdin) input+=part;
const {staticDirectory,exchanges}=JSON.parse(input);
const {createFieldResponseMachine}=await import(pathToFileURL(join(staticDirectory,'field-state.mjs')));
const {validSnapshot}=await import(pathToFileURL(join(staticDirectory,'current-state.mjs')));
for (const {body,response} of exchanges) {
  const caseId=response.snapshot.case.case_id;
  assert.equal(validSnapshot(response.snapshot,caseId),true,body.command.operation+' snapshot');
  const items=new Map();
  const storage={getItem:k=>items.get(k)??null,setItem:(k,v)=>items.set(k,v),removeItem:k=>items.delete(k)};
  const machine=createFieldResponseMachine({caseId,origin:'http://localhost',storage,send:async()=>response});
  machine.begin(body);
  const result=await machine.attempt();
  assert.equal(result.kind,'success',body.command.operation+' '+body.request_id);
  assert.deepEqual(result.result,response);
  assert.equal(machine.pending,null);
}
process.stdout.write(JSON.stringify({validated:exchanges.length}));
