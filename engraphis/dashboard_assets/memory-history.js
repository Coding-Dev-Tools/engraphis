(() => {
  'use strict';

  function element(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text != null) item.textContent = String(text);
    return item;
  }

  function create({ api, id, workspace, repo, isCurrent, onOpen, onUseBase, original }) {
    const root = element('section', 'record-history');
    root.setAttribute('aria-label', 'Record history');
    root.append(element('h3', '', 'Record history'));
    root.append(element('p', 'project-help', 'Supersession chain: linked versions of this record, oldest first. Topic search is separate.'));
    const status = element('p', 'project-help');
    status.setAttribute('role', 'status');
    const list = element('div', 'timeline-list');
    const actions = element('div', 'form-actions');
    const refresh = element('button', 'secondary-button', 'Refresh history');
    const more = element('button', 'secondary-button', 'Load more versions');
    refresh.type = more.type = 'button';
    more.hidden = true;
    actions.append(refresh, more);
    root.append(status, list, actions);
    let cursor = null;
    let controller = null;
    let destroyed = false;
    let rows = [];
    let generation = 0;
    const current = () => !destroyed && isCurrent();

    function render() {
      list.replaceChildren();
      rows.forEach(record => {
        const card = element('article', 'timeline-card');
        card.dataset.historyId = record.id;
        card.append(element('h4', '', window.EngraphisWorkflow.title(record)));
        card.append(element('p', 'history-content', record.content || record.summary || 'No content.'));
        const date = value => value == null ? 'open' : new Date(value * 1000).toLocaleString();
        card.append(element('p', 'project-help', 'Valid: ' + date(record.valid_from) + ' — ' + date(record.valid_to)));
        card.append(element('p', 'project-help', 'Source: ' + ((record.provenance || {}).source || 'unrecorded')));
        if (onOpen) {
          const open = element('button', 'text-button', 'Inspect version');
          open.type = 'button';
          open.addEventListener('click', () => { if (current()) onOpen(record); });
          card.append(open);
        }
        if (onUseBase && record.can_revise === true && record.version
          && window.EngraphisMemoryRevision.sameOwnership(original, record)) {
          const choose = element('button', 'secondary-button', 'Use this version as editing base');
          choose.type = 'button';
          choose.addEventListener('click', () => { if (current()) onUseBase(record); });
          card.append(choose);
        }
        list.append(card);
      });
    }

    async function load(reset = false) {
      if (!current()) return;
      if (controller) controller.abort();
      controller = new AbortController();
      const request = ++generation;
      if (reset) {
        rows = [];
        cursor = null;
        more.hidden = true;
        list.replaceChildren();
      }
      const selectedCursor = cursor;
      refresh.disabled = more.disabled = true;
      root.setAttribute('aria-busy', 'true');
      status.textContent = 'Loading saved versions…';
      try {
        const params = new URLSearchParams({ workspace, limit: '50' });
        if (repo) params.set('repo', repo);
        if (selectedCursor) params.set('cursor', selectedCursor);
        const result = await api('/memory/' + encodeURIComponent(id) + '/history?' + params, { signal: controller.signal });
        if (!current() || request !== generation) return;
        if (!result || !Array.isArray(result.versions)) throw new Error('No history returned.');
        rows = rows.concat(result.versions);
        cursor = result.next_cursor || null;
        render();
        status.textContent = rows.length + ' of ' + result.total_count + ' saved versions.';
        more.hidden = !cursor;
      } catch (error) {
        if (!current() || request !== generation) return;
        status.textContent = error.status === 409
          ? 'History changed while paging. Refresh history to start again.'
          : 'Could not load history: ' + error.message;
        // A stale cursor cannot be retried against changed history.
        if (error.status === 409) { cursor = null; more.hidden = true; }
      } finally {
        if (current() && request === generation) {
          refresh.disabled = more.disabled = false;
          root.setAttribute('aria-busy', 'false');
        }
      }
    }
    refresh.addEventListener('click', () => { void load(true); });
    more.addEventListener('click', () => { void load(); });
    return {
      element: root,
      load,
      destroy() { destroyed = true; if (controller) controller.abort(); },
    };
  }

  window.EngraphisMemoryHistory = Object.freeze({ create });
})();
