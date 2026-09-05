/* Workspace approval UI. Requests and workspace lifecycle are injected by Ledger. */
(() => {
  'use strict';
  window.EngraphisProcessingControls = {
    create(api) {
      const form = document.getElementById('managed-processing-form');
      const approval = document.getElementById('managed-processing-approval');
      const status = document.getElementById('managed-processing-status');
      const enable = document.getElementById('managed-processing-enable');
      const disable = document.getElementById('managed-processing-disable');
      let workspace = '';
      let generation = 0;
      let request;
      let policy;
      let busy = false;
      const render = () => {
        enable.disabled = busy || !workspace || !policy || !approval.checked || policy.operator_disabled;
        disable.disabled = busy || !workspace || !policy;
      };
      const show = value => {
        busy = false;
        policy = value;
        status.textContent = value.notice || (value.remote_sync_pending
          ? 'Uploads are paused locally. Cloud confirmation is pending; retry Turn off when connected.'
          : value.enabled ? `Managed processing is enabled for ${workspace}.`
          : value.operator_disabled ? 'Managed processing is disabled by this installation.'
          : `Managed processing is off for ${workspace}. ${value.confirmation_required ? 'Review and confirm before any readable workspace content is uploaded.' : 'New readable uploads are paused.'}`);
        approval.checked = false;
        render();
      };
      async function save(enabled) {
        if (busy || !workspace || (enabled && !approval.checked)) return;
        busy = true;
        const selected = workspace;
        const epoch = ++generation;
        if (request) request.abort();
        request = new AbortController();
        enable.disabled = disable.disabled = true;
        status.textContent = enabled ? 'Requesting workspace approval…' : 'Stopping new uploads…';
        try {
          const result = await api('/managed-processing', {
            method: 'POST', signal: request.signal,
            body: { workspace: selected, enabled, confirmed: enabled && approval.checked },
          });
          if (epoch === generation) show(result);
        } catch (error) {
          if (epoch !== generation) return;
          status.textContent = `${error.message} Reload the policy to verify the outcome before trying again.`;
          busy = false;
          policy = null;
          approval.checked = false;
          render();
        }
      }
      form.addEventListener('submit', event => { event.preventDefault(); save(true); });
      disable.addEventListener('click', () => save(false));
      approval.addEventListener('change', render);
      async function selectWorkspace(name) {
        busy = false;
        workspace = name;
        const epoch = ++generation;
        if (request) request.abort();
        request = new AbortController();
        policy = null;
        approval.checked = false;
        render();
        status.textContent = name ? 'Checking workspace processing policy…' : 'Select a workspace.';
        if (!name) return;
        try {
          const result = await api(`/managed-processing?workspace=${encodeURIComponent(name)}`, {
            signal: request.signal,
          });
          if (epoch === generation) show(result);
        } catch (error) {
          if (epoch === generation) status.textContent = error.message;
        }
      }
      document.getElementById('managed-processing-reload').addEventListener('click', () => selectWorkspace(workspace));
      return { selectWorkspace };
    },
  };
})();
