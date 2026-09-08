(() => {
  'use strict';

  // A submitted intent survives an uncertain response for this open editor only.
  // Memory contents never enter browser storage.
  function create(memory) {
    const base = memory ? { ...memory } : null;
    let submitted = null;
    return {
      body(fields) {
        if (!base || !base.version) throw new Error('Refresh the saved record before revising it.');
        const intent = {
          id: base.id,
          expected_version: base.version,
          ...fields,
        };
        const signature = JSON.stringify(intent);
        if (!submitted || submitted.signature !== signature) {
          const bytes = crypto.getRandomValues(new Uint8Array(16));
          const operation = 'ledger:' + [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
          submitted = { signature, body: { ...intent, operation_id: operation } };
        }
        return { ...submitted.body };
      },
    };
  }

  function sameOwnership(left, right) {
    return ['scope', 'workspace_id', 'repo_id', 'session_id'].every(key =>
      (left[key] || null) === (right[key] || null));
  }

  window.EngraphisMemoryRevision = Object.freeze({ create, sameOwnership });
})();
