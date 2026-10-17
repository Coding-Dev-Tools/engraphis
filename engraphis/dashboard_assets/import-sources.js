/* Engraphis Library import helpers: Quick import and Code & database sources.

   Quick import uploads files or a dropped/picked folder to /workspaces/import-files, or
   imports a folder by path on the machine running Engraphis (/workspaces/import-folder).
   Every imported file becomes untrusted workspace memory (SECURITY.md §1 and §5); the
   server enforces the path allow-list and size limits, and this module mirrors those
   limits only to fail early with a clear message.

   Sources index a code repository (/code/index) or read a PostgreSQL catalog
   (/resources/postgres). The PostgreSQL connection string may contain a password: it is
   sent once, cleared from the field immediately, and never stored by this page. */
(function () {
  'use strict';

  const MAX_FILES = 1500;
  const MAX_FILE_BYTES = 100_000_000;
  const MAX_TOTAL_BYTES = 750_000_000;
  const LONG_REQUEST_MS = 15 * 60_000;
  const SKIPPED_DIRECTORY = part => part === 'node_modules' || part.startsWith('.');
  const SENSITIVE_FILENAMES = new Set([
    '.env', 'credentials', 'credentials.json', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519',
    '.netrc', '_netrc', '.npmrc', '.pypirc', '.git-credentials', '.pgpass', 'pgpass.conf',
    '.aws', '.ssh', '.gnupg', '.azure', '.kube', '.docker', '.engraphis',
    'authorized_keys', 'known_hosts', 'recovery-codes', 'recovery_codes', 'tokens', 'tokens.json',
    'secrets', 'secrets.json', 'secret', 'secret.json',
  ]);
  const SENSITIVE_DIRECTORIES = new Set([...SENSITIVE_FILENAMES,
    'credential', 'token', '.credential', '.credentials', '.secrets', '.secret', '.token', '.tokens',
  ]);
  const sensitiveDirectory = name => SENSITIVE_DIRECTORIES.has(name.toLowerCase())
    || /^\.env\.|\.(?:pem|key|p12|pfx)$|^\.?recovery[-_ ]?codes?$/i.test(name);
  const sensitiveFilename = name => sensitiveDirectory(name)
    || /credential|recovery[-_ ]?code|secret|token/i.test(name);

  function create({ api, workspace, notify, onImported, confirmDerive }) {
    const byId = id => document.getElementById(id);
    const quickDialog = byId('quick-import-dialog');
    const sourcesDialog = byId('sources-dialog');
    let busy = false;
    let returnFocus = null;

    const count = (value, noun, plural = `${noun}s`) => `${Number(value || 0).toLocaleString()} ${Number(value) === 1 ? noun : plural}`;
    const megabytes = bytes => `${Math.round(bytes / 1_000_000).toLocaleString()} MB`;

    function setStatus(message, tone = '') {
      const target = byId('quick-import-status');
      target.replaceChildren();
      if (message) {
        const line = document.createElement('p');
        line.textContent = message;
        if (tone) line.className = tone === 'error' ? 'form-error' : 'project-help';
        target.append(line);
      }
    }

    function setBusy(next) {
      busy = next;
      ['quick-import-choose-files', 'quick-import-choose-folder', 'quick-import-path-run',
        'quick-import-derive', 'quick-import-close'].forEach(id => {
        const control = byId(id);
        if (control) control.disabled = next;
      });
      byId('quick-import-form').setAttribute('aria-busy', String(next));
      byId('quick-import-drop').classList.toggle('busy', next);
    }

    function renderResult(result, ws) {
      const target = byId('quick-import-status');
      target.replaceChildren();
      const parts = [
        `${count(result.imported, 'file')} imported`,
        `${Number(result.skipped || 0).toLocaleString()} skipped`,
        count(result.errors, 'error'),
      ];
      if (Number(result.derived_facts)) parts.push(count(result.derived_facts, 'derived fact'));
      if (Number(result.unreadable)) parts.push(`${Number(result.unreadable).toLocaleString()} unreadable`);
      const headline = document.createElement('p');
      headline.className = 'quick-import-summary';
      headline.textContent = `${parts.join(' · ')} in ${ws}.`;
      target.append(headline);
      if (result.truncated) {
        const truncated = document.createElement('p');
        truncated.className = 'form-error';
        truncated.textContent = `Only the first ${Number(result.scanned || 0).toLocaleString()} of ${Number(result.matched_total || 0).toLocaleString()} matching files were imported. Narrow the folder or pattern and import the rest separately.`;
        target.append(truncated);
      }
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      if (warnings.length) {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = `${count(warnings.length, 'file')} with warnings`;
        const list = document.createElement('ul');
        warnings.slice(0, 50).forEach(item => {
          const row = document.createElement('li');
          const notes = Array.isArray(item && item.warnings) ? item.warnings.join('; ') : String(item && item.warnings || '');
          row.textContent = `${String(item && item.file || 'file')} — ${notes || 'warning'}`;
          list.append(row);
        });
        if (warnings.length > 50) {
          const more = document.createElement('li');
          more.textContent = `${(warnings.length - 50).toLocaleString()} more not shown.`;
          list.append(more);
        }
        details.append(summary, list);
        target.append(details);
      }
      const next = document.createElement('p');
      next.className = 'project-help';
      next.textContent = 'Imported records are untrusted until you review them. Open a record in the Library and choose Approve for prompt when its source is safe.';
      target.append(next);
    }

    async function deriveAllowed() {
      if (!byId('quick-import-derive').checked) return { derive: false, ok: true };
      const ok = await confirmDerive();
      return { derive: ok, ok };
    }

    function keep(name, isDirectory = false) {
      const parts = String(name || '').replace(/\\/g, '/').split('/').filter(Boolean);
      const directories = isDirectory ? parts : parts.slice(0, -1);
      return parts.length > 0 && !directories.some(SKIPPED_DIRECTORY)
        && !directories.some(sensitiveDirectory)
        && (isDirectory || !sensitiveFilename(parts[parts.length - 1]));
    }

    async function upload(items) {
      if (busy) return;
      const ws = workspace();
      if (!ws) {
        setStatus('Choose or create a workspace before importing.', 'error');
        return;
      }
      const files = items.filter(item => item && item.file && keep(item.name));
      if (!files.length) {
        setStatus(items.length
          ? 'Nothing to import: the selection only contained hidden or dependency folders, or sensitive files.'
          : 'No files were found in that selection.', 'error');
        return;
      }
      if (files.length > MAX_FILES) {
        setStatus(`Choose at most ${MAX_FILES.toLocaleString()} files per import (this selection has ${files.length.toLocaleString()}). Import a smaller folder, or use a folder path with a file pattern.`, 'error');
        return;
      }
      const oversized = files.find(item => item.file.size > MAX_FILE_BYTES);
      if (oversized) {
        setStatus(`${oversized.name} is larger than ${megabytes(MAX_FILE_BYTES)}, the per-file import limit.`, 'error');
        return;
      }
      const total = files.reduce((sum, item) => sum + (item.file.size || 0), 0);
      if (total > MAX_TOTAL_BYTES) {
        setStatus(`This selection is ${megabytes(total)}; one import can carry at most ${megabytes(MAX_TOTAL_BYTES)}.`, 'error');
        return;
      }
      const consent = await deriveAllowed();
      if (!consent.ok) return;
      const form = new FormData();
      form.append('workspace', ws);
      form.append('memory_type', 'semantic');
      form.append('derive_facts', consent.derive ? 'true' : 'false');
      files.forEach(item => form.append('files', item.file, item.name));
      setBusy(true);
      setStatus(`Extracting and importing ${count(files.length, 'file')} into ${ws}…`);
      try {
        const result = await api('/workspaces/import-files', {
          method: 'POST', body: form, timeoutMs: LONG_REQUEST_MS,
        });
        renderResult(result || {}, ws);
        notify(`${count((result || {}).imported, 'file')} imported into ${ws}.`);
        await onImported(ws);
      } catch (error) {
        setStatus(`Import failed: ${error.message}`, 'error');
      } finally {
        setBusy(false);
      }
    }

    async function importPath() {
      if (busy) return;
      const ws = workspace();
      const path = byId('quick-import-path').value.trim();
      const pattern = byId('quick-import-pattern').value.trim() || '*';
      if (!ws) {
        setStatus('Choose or create a workspace before importing.', 'error');
        return;
      }
      if (!path) {
        setStatus('Enter a folder path on the machine running Engraphis.', 'error');
        byId('quick-import-path').focus();
        return;
      }
      const consent = await deriveAllowed();
      if (!consent.ok) return;
      setBusy(true);
      setStatus(`Importing ${pattern} files from ${path}…`);
      try {
        const result = await api('/workspaces/import-folder', {
          method: 'POST',
          timeoutMs: LONG_REQUEST_MS,
          body: { workspace: ws, path, file_pattern: pattern, memory_type: 'semantic', derive_facts: consent.derive },
        });
        renderResult(result || {}, ws);
        notify(`${count((result || {}).imported, 'file')} imported into ${ws}.`);
        await onImported(ws);
      } catch (error) {
        setStatus(`Import failed: ${error.message}`, 'error');
      } finally {
        setBusy(false);
      }
    }

    async function walkEntry(entry, prefix, out) {
      if (out.length > MAX_FILES) return;
      if (!keep(`${prefix}${entry.name}`, entry.isDirectory)) return;
      if (entry.isFile) {
        await new Promise(resolve => entry.file(file => {
          out.push({ file, name: `${prefix}${file.name}` });
          resolve();
        }, () => resolve()));
        return;
      }
      if (!entry.isDirectory || SKIPPED_DIRECTORY(entry.name)) return;
      const reader = entry.createReader();
      const next = () => new Promise(resolve => reader.readEntries(resolve, () => resolve([])));
      let batch;
      do {
        batch = await next();
        for (const child of batch) {
          await walkEntry(child, `${prefix}${entry.name}/`, out);
          if (out.length > MAX_FILES) return;
        }
      } while (batch.length);
    }

    async function onDrop(event) {
      event.preventDefault();
      byId('quick-import-drop').classList.remove('drag-over');
      if (busy || !event.dataTransfer) return;
      /* DataTransfer items are only readable during the event: capture entries first. */
      const entries = [...(event.dataTransfer.items || [])]
        .map(item => (typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null))
        .filter(Boolean);
      const plain = [...(event.dataTransfer.files || [])].map(file => ({ file, name: file.name }));
      if (!entries.length) {
        await upload(plain);
        return;
      }
      setStatus('Reading the dropped items…');
      const out = [];
      for (const entry of entries) {
        await walkEntry(entry, '', out);
        if (out.length > MAX_FILES) break;
      }
      await upload(out);
    }

    function picked(input) {
      const items = [...(input.files || [])].map(file => ({
        file, name: file.webkitRelativePath || file.name,
      }));
      input.value = '';
      void upload(items);
    }

    function openQuick() {
      const ws = workspace();
      byId('quick-import-workspace').textContent = ws || 'this workspace';
      setStatus(ws ? '' : 'Choose or create a workspace before importing.', ws ? '' : 'error');
      returnFocus = document.activeElement;
      if (!quickDialog.open) quickDialog.showModal();
      byId('quick-import-choose-files').focus();
    }

    function closeQuick() {
      if (busy) return;
      if (quickDialog.open) quickDialog.close();
    }

    function sourceStatus(id, message, error = false) {
      const target = byId(id);
      target.textContent = message;
      target.classList.toggle('form-error', error);
    }

    async function indexCode(event) {
      event.preventDefault();
      const ws = workspace();
      const repo = byId('code-index-repo').value.trim();
      const root = byId('code-index-root').value.trim();
      if (!ws) {
        sourceStatus('code-index-status', 'Choose or create a workspace first.', true);
        return;
      }
      if (!repo || !root) {
        sourceStatus('code-index-status', 'Enter a repository name and its path on the machine running Engraphis.', true);
        (repo ? byId('code-index-root') : byId('code-index-repo')).focus();
        return;
      }
      const submit = event.submitter || byId('code-index-form').querySelector('button[type="submit"]');
      if (submit) submit.disabled = true;
      sourceStatus('code-index-status', `Indexing ${repo}… unchanged files are skipped.`);
      try {
        const result = await api('/code/index', {
          method: 'POST', timeoutMs: LONG_REQUEST_MS, body: { workspace: ws, repo, root_path: root },
        });
        sourceStatus('code-index-status', `${repo}: ${Number(result.files_indexed || 0).toLocaleString()} changed and ${Number(result.files_unchanged || 0).toLocaleString()} unchanged files · ${count(result.symbols, 'symbol')} · ${count(result.edges, 'edge')} · ${count(result.code_memory_links, 'memory link')}.`);
        notify(`Repository ${repo} indexed. Turn on the Code overlay in Explore to see it.`);
        await onImported(ws);
      } catch (error) {
        sourceStatus('code-index-status', `Indexing failed: ${error.message}`, true);
      } finally {
        if (submit) submit.disabled = false;
      }
    }

    async function importPostgres(event) {
      event.preventDefault();
      const ws = workspace();
      const field = byId('postgres-dsn');
      const dsn = field.value.trim();
      const repo = byId('postgres-repo').value.trim();
      /* Clear the secret before any await so it never lingers in the page. */
      field.value = '';
      if (!ws) {
        sourceStatus('postgres-import-status', 'Choose or create a workspace first.', true);
        return;
      }
      if (!dsn) {
        sourceStatus('postgres-import-status', 'Enter a PostgreSQL connection string.', true);
        field.focus();
        return;
      }
      const submit = event.submitter || byId('postgres-import-form').querySelector('button[type="submit"]');
      if (submit) submit.disabled = true;
      sourceStatus('postgres-import-status', 'Reading the PostgreSQL catalog…');
      try {
        const result = await api('/resources/postgres', {
          method: 'POST', timeoutMs: LONG_REQUEST_MS, body: { workspace: ws, repo: repo || null, dsn },
        });
        const schema = (result && result.schema) || {};
        sourceStatus('postgres-import-status', `Imported ${count(schema.tables, 'table')}, ${count(result.entities, 'entity', 'entities')} and ${count(result.relations, 'relation')}. The connection string was not stored.`);
        notify('Database schema imported. Open Explore to see its tables and relations.');
        await onImported(ws);
      } catch (error) {
        sourceStatus('postgres-import-status', `Schema import failed: ${error.message}`, true);
      } finally {
        if (submit) submit.disabled = false;
      }
    }

    function openSources() {
      returnFocus = document.activeElement;
      ['code-index-status', 'postgres-import-status'].forEach(id => sourceStatus(id, ''));
      byId('postgres-dsn').value = '';
      if (!sourcesDialog.open) sourcesDialog.showModal();
      byId('code-index-repo').focus();
    }

    function restoreFocus() {
      const target = returnFocus;
      returnFocus = null;
      if (target && target.isConnected && typeof target.focus === 'function') target.focus();
    }

    const drop = byId('quick-import-drop');
    drop.addEventListener('dragover', event => {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = busy ? 'none' : 'copy';
      drop.classList.add('drag-over');
    });
    drop.addEventListener('dragleave', event => {
      if (!drop.contains(event.relatedTarget)) drop.classList.remove('drag-over');
    });
    drop.addEventListener('drop', event => { void onDrop(event); });
    byId('quick-import-choose-files').addEventListener('click', () => byId('quick-import-files').click());
    byId('quick-import-choose-folder').addEventListener('click', () => byId('quick-import-folder').click());
    byId('quick-import-files').addEventListener('change', event => picked(event.currentTarget));
    byId('quick-import-folder').addEventListener('change', event => picked(event.currentTarget));
    byId('quick-import-path-run').addEventListener('click', () => { void importPath(); });
    byId('quick-import-path').addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        void importPath();
      }
    });
    byId('quick-import-form').addEventListener('submit', event => event.preventDefault());
    byId('quick-import-close').addEventListener('click', closeQuick);
    quickDialog.addEventListener('cancel', event => {
      if (busy) event.preventDefault();
    });
    quickDialog.addEventListener('close', restoreFocus);
    byId('code-index-form').addEventListener('submit', indexCode);
    byId('postgres-import-form').addEventListener('submit', importPostgres);
    byId('sources-close').addEventListener('click', () => sourcesDialog.close());
    sourcesDialog.addEventListener('close', () => {
      byId('postgres-dsn').value = '';
      restoreFocus();
    });

    return Object.freeze({ openQuick, openSources, upload });
  }

  window.EngraphisImports = Object.freeze({ create });
})();
