(() => {
  'use strict';

  function element(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text != null) item.textContent = String(text);
    return item;
  }

  /* Word-level diff between two saved versions (longest common subsequence over words and
     the whitespace between them). Returns null when the pair is too large to compare
     interactively, so a very long record never stalls the page. */
  const DIFF_CELL_LIMIT = 250000;
  function wordDiff(before, after) {
    const a = String(before || '').split(/(\s+)/).filter(Boolean);
    const b = String(after || '').split(/(\s+)/).filter(Boolean);
    const n = a.length;
    const m = b.length;
    if ((n + 1) * (m + 1) > DIFF_CELL_LIMIT) return null;
    const width = m + 1;
    const table = new Uint32Array((n + 1) * width);
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        table[i * width + j] = a[i] === b[j]
          ? table[(i + 1) * width + j + 1] + 1
          : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
      }
    }
    const parts = [];
    const push = (type, value) => {
      const last = parts[parts.length - 1];
      if (last && last.type === type) last.text += value;
      else parts.push({ type, text: value });
    };
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) { push('same', a[i]); i += 1; j += 1; }
      else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) { push('del', a[i]); i += 1; }
      else { push('ins', b[j]); j += 1; }
    }
    while (i < n) { push('del', a[i]); i += 1; }
    while (j < m) { push('ins', b[j]); j += 1; }
    return parts;
  }

  function diffContent(previous, record) {
    const content = record.content || record.summary || '';
    const body = element('p', 'history-content');
    if (!previous) {
      body.textContent = content || 'No content.';
      return [body];
    }
    const parts = wordDiff(previous.content || previous.summary || '', content);
    if (!parts) {
      body.textContent = content || 'No content.';
      return [body, element('p', 'project-help', 'This version is too long to compare word by word.')];
    }
    if (!parts.some(part => part.type !== 'same')) {
      body.textContent = content || 'No content.';
      return [body, element('p', 'project-help', 'Text unchanged from the previous version.')];
    }
    body.classList.add('history-diff');
    parts.forEach(part => {
      if (part.type === 'same') body.append(document.createTextNode(part.text));
      else body.append(element(part.type, '', part.text));
    });
    return [body];
  }

  function create({ api, id, workspace, repo, isCurrent, onOpen, onUseBase, original }) {
    const root = element('section', 'record-history');
    root.setAttribute('aria-label', 'Record history');
    root.append(element('h3', '', 'Record history'));
    root.append(element('p', 'project-help', 'Supersession chain: linked versions of this record, oldest first. Each revision marks what changed from the version before it: added words are underlined, removed words are struck through. Topic search is separate.'));
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
      rows.forEach((record, index) => {
        const card = element('article', 'timeline-card');
        card.dataset.historyId = record.id;
        card.append(element('h4', '', window.EngraphisWorkflow.title(record)));
        card.append(element('p', 'eyebrow', index === 0 ? 'Earliest loaded version' : `Revision ${index}`));
        card.append(...diffContent(index > 0 ? rows[index - 1] : null, record));
        const date = value => {
          if (value == null) return 'open';
          const when = new Date(Number(value) * 1000);
          return Number.isNaN(when.getTime()) ? 'unknown' : when.toLocaleString();
        };
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

  window.EngraphisMemoryHistory = Object.freeze({ create, wordDiff });
})();
