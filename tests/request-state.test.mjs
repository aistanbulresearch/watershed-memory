import test from 'node:test';
import assert from 'node:assert/strict';
import {isAmbiguousFailure, errorMessage, responseIsRecorded} from '../watershed_memory/static/request-state.mjs';

test('only transport and server uncertainty freeze the submitted payload', () => {
  for (const status of [400, 403, 404, 409, 415, 422, 429]) assert.equal(isAmbiguousFailure(status), false);
  for (const status of [undefined, 500, 502, 503, 504]) assert.equal(isAmbiguousFailure(status), true);
});

test('HTTP validation arrays explain the editable field', () => {
  assert.equal(errorMessage([{loc:['body','note'],msg:'Must contain at most 1000 characters'}],422),
    'note: Must contain at most 1000 characters');
  assert.equal(errorMessage('Already completed.',409),'Already completed.');
});

test('reconciliation needs the exact task, action and note', () => {
  const pending = {taskId:'review-1',action:'acknowledge',note:'Assigned.'};
  assert.equal(responseIsRecorded([{task_id:'review-1',action:'complete_review',note:'Assigned.'}],pending),false);
  assert.equal(responseIsRecorded([{task_id:'review-2',action:'acknowledge',note:'Assigned.'}],pending),false);
  assert.equal(responseIsRecorded([{task_id:'review-1',action:'acknowledge',note:'Assigned.'}],pending),true);
});
