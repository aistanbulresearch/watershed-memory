// Shared request semantics. A definite rejection is editable; uncertainty keeps its identity.
export function isAmbiguousFailure(status) {
  return status === undefined || status >= 500;
}

export function errorMessage(detail, status) {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    return detail.map(item => {
      const field = (item.loc || []).filter(part => part !== 'body').join('.');
      return `${field ? `${field}: ` : ''}${item.msg || 'Invalid value'}`;
    }).join('; ');
  }
  return `Request failed (${status})`;
}

export function responseIsRecorded(responses, pending) {
  return responses.some(response => response.task_id === pending.taskId &&
    response.action === pending.action && response.note === pending.note);
}
