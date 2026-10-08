(() => {
  'use strict';

  // The grounded answer and retrieval preview share one server-side recall. This
  // keeps Jev planning, scope, time anchors and trust filtering identical in both
  // panels, and avoids a second remote planning decision.
  function create({ renderAnswer, renderPreview }) {
    const panels = { answer: 'answer-panel', preview: 'retrieval-list' };
    const names = { answer: 'Grounded Ask', preview: 'Raw retrieval' };
    const byId = id => document.getElementById(id);
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
      byId('ask-cancel').hidden = task.answer.status !== 'pending';
      byId('ask-answer-retry').hidden = !['failed', 'canceled'].includes(task.answer.status);
      ['answer', 'preview'].forEach(kind => {
        byId(panels[kind]).setAttribute('aria-busy', String(task[kind].status === 'pending'));
      });
    }

    async function run(task) {
      if (!valid(task)) return;
      const attempt = ++task.answer.attempt;
      task.answer.controller = new AbortController();
      task.answer.status = 'pending';
      task.preview.status = 'pending';
      message('answer', 'Searching, checking support and building citations…');
      message('preview', 'Retrieving candidates from the grounded answer’s retrieval plan…');
      controls(task);
      const current = () => valid(task) && task.answer.attempt === attempt
        && task.answer.status === 'pending';
      try {
        const result = await task.answer.fetch(task.answer.controller.signal);
        if (!current()) return;
        if (!result || typeof result !== 'object') throw new Error('No result was returned.');
        if (!Array.isArray(result.retrieval_preview)) {
          throw new Error('The server omitted candidates from this retrieval plan.');
        }
        renderAnswer(result);
        renderPreview({ memories: result.retrieval_preview });
        task.answer.status = 'succeeded';
        task.preview.status = 'succeeded';
      } catch (error) {
        if (!current()) return;
        task.answer.status = 'failed';
        task.preview.status = 'failed';
        message('answer', names.answer + ' is unavailable: ' + error.message);
        message('preview', names.preview + ' is unavailable because the grounded retrieval did not complete.');
      } finally {
        controls(task);
      }
    }

    function reset() {
      const previous = active;
      active = null;
      if (previous && previous.answer.controller) previous.answer.controller.abort();
      ['ask-cancel', 'ask-answer-retry'].forEach(id => { byId(id).hidden = true; });
      byId('ask-status').textContent = '';
      byId('ask-result-query').textContent = '';
      Object.values(panels).forEach(id => byId(id).setAttribute('aria-busy', 'false'));
    }

    function cancel() {
      const task = active;
      if (!task || !valid(task) || task.answer.status !== 'pending') return;
      task.answer.status = 'canceled';
      task.preview.status = 'canceled';
      ++task.answer.attempt;
      task.answer.controller.abort();
      message('answer', 'Grounded Ask canceled in this browser. The server may still finish processing. Retry this question when ready.');
      message('preview', 'Retrieval preview canceled with the grounded answer.');
      controls(task);
    }

    byId('ask-cancel').addEventListener('click', cancel);
    byId('ask-answer-retry').addEventListener('click', () => {
      if (active && valid(active) && ['failed', 'canceled'].includes(active.answer.status)) void run(active);
    });
    return {
      reset,
      start({ question, scopeLabel, isCurrent, answer }) {
        reset();
        const task = {
          isCurrent,
          answer: { fetch: answer, status: 'pending', attempt: 0, controller: null },
          preview: { status: 'pending' },
        };
        active = task;
        byId('ask-result-query').textContent = 'Results for “' + question + '” in ' + scopeLabel + '.';
        return run(task);
      },
    };
  }
  window.EngraphisAskRequests = Object.freeze({ create });
})();
