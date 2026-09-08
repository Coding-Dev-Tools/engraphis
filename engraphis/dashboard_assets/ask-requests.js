(() => {
  'use strict';

  // Each panel owns its retry and cancellation, while all attempts stay bound to
  // the submitted question and its workspace/project snapshot.
  function create({ renderAnswer, renderPreview }) {
    const byId = id => document.getElementById(id);
    const panels = { answer: 'answer-panel', preview: 'retrieval-list' };
    const names = { answer: 'Grounded Ask', preview: 'Raw retrieval' };
    let active = null;
    const valid = task => active === task && task.isCurrent();
    const message = (kind, value) => {
      const text = document.createElement('p');
      text.className = 'empty-state';
      text.textContent = value;
      byId(panels[kind]).replaceChildren(text);
    };

    function controls(task) {
      if (!valid(task)) return;
      const labels = { pending: 'loading', succeeded: 'ready', failed: 'failed', canceled: 'canceled' };
      byId('ask-status').textContent = 'Answer ' + labels[task.answer.status]
        + ' · Preview ' + labels[task.preview.status] + '.';
      byId('ask-cancel').hidden = !['answer', 'preview'].some(kind => task[kind].status === 'pending');
      ['answer', 'preview'].forEach(kind => {
        byId('ask-' + kind + '-retry').hidden = !['failed', 'canceled'].includes(task[kind].status);
        byId(panels[kind]).setAttribute('aria-busy', String(task[kind].status === 'pending'));
      });
    }

    async function run(task, kind) {
      if (!valid(task)) return;
      const slot = task[kind];
      const attempt = ++slot.attempt;
      slot.controller = new AbortController();
      slot.status = 'pending';
      message(kind, kind === 'answer'
        ? 'Searching, checking support and building citations…' : 'Retrieving candidate memories…');
      controls(task);
      const current = () => valid(task) && slot.attempt === attempt && slot.status === 'pending';
      try {
        const result = await slot.fetch(slot.controller.signal);
        if (!current()) return;
        if (!result || typeof result !== 'object') throw new Error('No result was returned.');
        if (kind === 'answer') renderAnswer(result);
        else renderPreview(result);
        slot.status = 'succeeded';
      } catch (error) {
        if (!current()) return;
        slot.status = 'failed';
        message(kind, names[kind] + ' is unavailable: ' + error.message);
      } finally {
        controls(task);
      }
    }

    function reset() {
      const previous = active;
      active = null;
      if (previous) ['answer', 'preview'].forEach(kind => {
        if (previous[kind].controller) previous[kind].controller.abort();
      });
      ['ask-cancel', 'ask-answer-retry', 'ask-preview-retry'].forEach(id => { byId(id).hidden = true; });
      byId('ask-status').textContent = '';
      byId('ask-result-query').textContent = '';
      Object.values(panels).forEach(id => byId(id).setAttribute('aria-busy', 'false'));
    }

    function cancel() {
      const task = active;
      if (!task || !valid(task)) return;
      ['answer', 'preview'].forEach(kind => {
        const slot = task[kind];
        if (slot.status !== 'pending') return;
        slot.status = 'canceled';
        ++slot.attempt;
        slot.controller.abort();
        message(kind, names[kind] + ' canceled in this browser. The server may still finish processing. Retry this panel when ready.');
      });
      controls(task);
    }

    byId('ask-cancel').addEventListener('click', cancel);
    ['answer', 'preview'].forEach(kind => {
      byId('ask-' + kind + '-retry').addEventListener('click', () => {
        if (active && valid(active) && ['failed', 'canceled'].includes(active[kind].status)) void run(active, kind);
      });
    });
    return {
      reset,
      start({ question, scopeLabel, isCurrent, answer, preview }) {
        reset();
        const task = {
          isCurrent,
          answer: { fetch: answer, status: 'pending', attempt: 0 },
          preview: { fetch: preview, status: 'pending', attempt: 0 },
        };
        active = task;
        byId('ask-result-query').textContent = 'Results for “' + question + '” in ' + scopeLabel + '.';
        return Promise.allSettled([run(task, 'answer'), run(task, 'preview')]);
      },
    };
  }
  window.EngraphisAskRequests = Object.freeze({ create });
})();
