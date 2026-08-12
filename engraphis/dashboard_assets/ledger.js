(() => {
  'use strict';

  const apiRoot = `${location.origin}/api`;
  const state = {
    workspace: '',
    workspaces: [],
    stats: {},
    memories: [],
    selectedMemory: '',
    editorMemory: null,
    editorReturnFocus: null,
    view: 'today',
    provenanceTab: 'belief',
    savingsPreset: 'all',
    manageTab: 'workspaces',
    refreshEpoch: 0,
    graphWorkspace: '',
    graphData: null,
    graphDataMode: 'overview',
    graphDataIncludeCode: false,
    graphDataShowUnlinked: false,
    graphDataAsOf: null,
    graphDataRepo: '',
    graphMeta: null,
    graphMode: 'overview',
    graphShowUnlinked: true,
    graphEngine: null,
    graphLoadPromise: null,
    graphLoadWorkspace: '',
    graphLoadMode: '',
    graphLoadIncludeCode: false,
    graphLoadShowUnlinked: false,
    graphLoadAsOf: null,
    graphLoadKey: '',
    graphLoadRequest: 0,
    graphRetryPending: false,
    graphLoadController: null,
    graphConnectionsRequest: 0,
    graphConnectionsController: null,
    graphMetrics: {},
    graphFrozen: false,
    graphOrbitPaused: false,
    graphSpacetimeOverlay: null,
    graphIncludeCode: false,
    graphSavedView: 'schema',
    consolidationReview: null,
    reviewCsrf: '',
    hostedLoaded: new Set(),
    scopedRequests: Object.create(null),
    syncStatus: null,
    license: null,
    releaseVersion: '',
  };

  const byId = id => document.getElementById(id);
  const all = selector => [...document.querySelectorAll(selector)];
  const text = value => value == null ? '' : String(value);
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const NOTICE_DURATION_MS = 3000;
  let noticeTimer = null;
  const CLOUD_SYNC_PRIVACY_NOTICE = 'Cloud Sync encrypts eligible shared-workspace changes end-to-end before they leave this device. Engraphis Cloud cannot read their contents; secret and session-scoped memories stay local.';
  const EXTERNAL_LLM_PRIVACY_NOTICE = 'Memory text is sent to your configured LLM provider for processing under that provider’s terms. The provider must read that text to return extracted facts.';
  const truncate = (value, length = 260) => {
    const source = text(value).trim();
    return source.length > length ? `${source.slice(0, length - 1)}…` : source;
  };
  const empty = (message, className = 'empty-state') => {
    const node = document.createElement('p');
    node.className = className;
    node.textContent = message;
    return node;
  };
  const node = (tag, className = '', content = '') => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== '') element.textContent = text(content);
    return element;
  };
  const button = (label, className, action) => {
    const control = node('button', className, label);
    control.type = 'button';
    control.addEventListener('click', action);
    return control;
  };
  const option = (value, label, selected = false) => {
    const item = node('option', '', label);
    item.value = value;
    item.selected = selected;
    return item;
  };
  const query = (name = state.workspace) => `workspace=${encodeURIComponent(name || '')}`;
  const beginScopedRequest = kind => {
    const generation = number(state.scopedRequests[kind]) + 1;
    state.scopedRequests[kind] = generation;
    return {
      kind,
      generation,
      workspace: state.workspace,
      epoch: state.refreshEpoch,
    };
  };
  const isCurrentScopedRequest = request => Boolean(request
    && request.workspace === state.workspace
    && request.epoch === state.refreshEpoch
    && state.scopedRequests[request.kind] === request.generation);
  const invalidateScopedRequests = () => {
    Object.keys(state.scopedRequests).forEach(kind => {
      state.scopedRequests[kind] = number(state.scopedRequests[kind]) + 1;
    });
  };
  const GRAPH_INITIAL_NODE_LIMIT = 1000;
  const GRAPH_INITIAL_EDGE_LIMIT = 2000;
  const GRAPH_FULL_NODE_LIMIT = 40_000;
  const GRAPH_LOAD_TIMEOUT_MS = 12_000;
  const GRAPH_FULL_LOAD_TIMEOUT_MS = 30_000;
  const GRAPH_CONNECTION_MEMORIES_TIMEOUT_MS = 8_000;
  const GRAPH_PREFERENCES_KEY = 'engraphis-ledger-graph-preferences-v1';
  const GRAPH_PHYSICS_VERSION = 2;
  const GRAPH_CUSTOM_VIEW_KEY = 'engraphis-ledger-graph-custom-view-v1';
  const GRAPH_LAYERS = ['temporal', 'entity', 'causal', 'semantic', 'code'];
  const GRAPH_DEFAULT_LAYERS = { temporal: true, entity: true, causal: true, semantic: true, code: false };
  const GRAPH_TUNING = [
    { id: 'graph-repel', key: 'repel', fallback: 60 },
    { id: 'graph-link', key: 'link', fallback: 8 },
    { id: 'graph-gravity', key: 'gravity', fallback: 48 },
    { id: 'graph-node-size', key: 'size', fallback: 3 },
    { id: 'graph-text-size', key: 'font', fallback: 12 },
    { id: 'graph-line-width', key: 'linkw', fallback: 0.72, precision: 2 },
    { id: 'graph-label-density', key: 'labelDensity', fallback: 24 },
  ];
  const GRAPH_SPACETIME_TUNING = [
    { id: 'graph-gravitational-constant', key: 'gravitationalConstant', fallback: 100 },
    { id: 'graph-black-hole-mass', key: 'blackHoleMass', fallback: 160 },
    { id: 'graph-local-gravitational-constant', key: 'localGravitationalConstant', fallback: 100 },
    { id: 'graph-space-damping', key: 'damping', fallback: 1, precision: 1 },
    { id: 'graph-spring-stiffness', key: 'springStiffness', fallback: 32 },
  ];
  const GRAPH_PRESET_TUNING = {
    original: { repel: 120, link: 30, gravity: 14, font: 13, size: 3, linkw: 1, labelDensity: 40 },
    compact: { repel: 42, link: 20, gravity: 26, font: 12, size: 3, linkw: 0.7, labelDensity: 30 },
    communities: { repel: 48, link: 16, gravity: 48, font: 12, size: 3, linkw: 0.72, labelDensity: 24 },
    galaxy: { repel: 60, link: 8, gravity: 48, font: 12, size: 3, linkw: 0.72, labelDensity: 24 },
    radial: { repel: 68, link: 26, gravity: 12, font: 13, size: 3, linkw: 0.75, labelDensity: 55 },
    constellation: { repel: 34, link: 16, gravity: 38, font: 12, size: 3, linkw: 0.65, labelDensity: 35 },
  };
  const GRAPH_SAVED_VIEWS = {
    operations: {
      preset: 'compact', style: 'cyber', color: 'connections', palette: 'contrast',
      layers: { temporal: false, entity: true, causal: true, semantic: false, code: false },
      minDegree: 2, depth: 1, showUnlinked: false, includeCode: false,
    },
    schema: {
      preset: 'communities', style: 'cyber', color: 'community', palette: 'theme',
      layers: { ...GRAPH_DEFAULT_LAYERS }, minDegree: 1, depth: 2, showUnlinked: true, includeCode: false,
    },
    people: {
      preset: 'radial', style: 'galaxy', color: 'community', palette: 'aurora',
      layers: { temporal: false, entity: true, causal: false, semantic: true, code: false },
      minDegree: 1, depth: 2, showUnlinked: false, includeCode: false,
    },
    code: {
      preset: 'constellation', style: 'cyber', color: 'type', palette: 'ocean',
      layers: { temporal: false, entity: true, causal: false, semantic: true, code: true },
      minDegree: 1, depth: 2, showUnlinked: false, includeCode: true,
    },
  };
  const GRAPH_PRESET_LABELS = {
    original: 'Spacious',
    compact: 'Compact',
    communities: 'Islands',
    radial: 'Radial',
    constellation: 'Constellation',
    galaxy: 'Galaxy gravity',
  };
  const GRAPH_STYLE_NOTES = {
    cyber: 'Iridescent PVD over graphite — cyan, violet, and magenta across each node.',
    galaxy: 'Deep anodized alloy with a cool blue-violet directional sheen.',
    solar: 'Brushed copper faces with amber bezels and warm radial grain.',
    classic: 'Neutral satin gunmetal with a restrained cool steel edge.',
  };
  const GRAPH_CUSTOM_PALETTE = {
    person_or_concept: '#8d82e3',
    mention: '#5ba1a6',
    hashtag: '#c9a15b',
    email: '#8eb3e6',
    organization: '#d48173',
    location: '#7ebf8e',
    memory: '#5ba1a6',
    repo: '#c9a15b',
    file: '#8eb3e6',
  };
  const relative = value => {
    const raw = typeof value === 'number' && value < 1e12 ? value * 1000 : value;
    const time = typeof raw === 'number' ? raw : Date.parse(raw);
    if (!Number.isFinite(time)) return 'stored locally';
    const seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(time);
  };
  const errorMessage = (payload, status) => {
    const detail = payload && (payload.detail || payload.error);
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail.error === 'string') return detail.error;
    return `Request failed (${status})`;
  };

  async function api(path, options = {}) {
    const init = { ...options, headers: { ...(options.headers || {}) } };
    init.headers['X-Engraphis-Browser-Session'] = '1';
    if (init.body && !(init.body instanceof FormData) && typeof init.body !== 'string') {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(init.body);
    }
    const response = await fetch(`${apiRoot}${path}`, init);
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(errorMessage(payload, response.status));
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function promptBrowserToken(message = '') {
    const dialog = byId('browser-auth-dialog');
    const form = byId('browser-auth-form');
    const input = byId('browser-auth-token');
    const error = byId('browser-auth-error');
    const cancel = byId('browser-auth-cancel');
    if (!dialog || !form || !input || !error || !cancel) return Promise.resolve('');

    error.textContent = message;
    error.hidden = !message;
    input.value = '';
    const returnFocus = document.activeElement;

    return new Promise(resolve => {
      let settled = false;
      const cleanup = () => {
        form.removeEventListener('submit', submit);
        cancel.removeEventListener('click', dismiss);
        dialog.removeEventListener('cancel', dismiss);
        dialog.removeEventListener('close', closed);
      };
      const finish = value => {
        if (settled) return;
        settled = true;
        cleanup();
        input.value = '';
        if (dialog.open) dialog.close();
        if (returnFocus && typeof returnFocus.focus === 'function') returnFocus.focus();
        resolve(value);
      };
      const submit = event => {
        event.preventDefault();
        const value = input.value.trim();
        if (!value) {
          error.textContent = 'Enter the deployment token.';
          error.hidden = false;
          input.focus();
          return;
        }
        finish(value);
      };
      const dismiss = event => {
        if (event) event.preventDefault();
        finish('');
      };
      const closed = () => finish('');

      form.addEventListener('submit', submit);
      cancel.addEventListener('click', dismiss);
      dialog.addEventListener('cancel', dismiss);
      dialog.addEventListener('close', closed);
      if (!dialog.open) dialog.showModal();
      input.focus();
    });
  }

  async function authenticateBrowser() {
    let token = '';
    let failure = '';
    try {
      const fragment = new URLSearchParams(location.hash.slice(1));
      token = fragment.get('token') || '';
      if (token) history.replaceState(null, '', `${location.pathname}${location.search}`);
    } catch (_) {}
    while (true) {
      if (!token) token = await promptBrowserToken(failure);
      if (!token) return false;
      let submitted = token;
      token = '';
      try {
        const session = await api('/auth/session', {
          method: 'POST',
          body: { token: submitted },
        });
        state.reviewCsrf = text(session && session.review_csrf_token);
        submitted = '';
        return true;
      } catch (error) {
        submitted = '';
        failure = error.message;
        showNotice(`Authentication failed: ${failure}`);
      }
    }
  }

  async function reviewCsrfToken() {
    if (state.reviewCsrf) return state.reviewCsrf;
    const response = await fetch(`${location.origin}/dashboard/review/csrf`, {
      headers: { 'X-Engraphis-Browser-Session': '1' },
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload || !payload.review_csrf_token) {
      const error = new Error(errorMessage(payload, response.status));
      error.status = response.status;
      throw error;
    }
    state.reviewCsrf = text(payload.review_csrf_token);
    return state.reviewCsrf;
  }

  async function approveForPrompt(memory) {
    if (!memory || !memory.id) return;
    const provenance = memory.provenance || {};
    const reviewState = provenance.review_state || 'pending';
    const reason = window.prompt(
      `Why is this ${reviewState} record safe to include in model context?`,
    );
    if (reason === null) return;
    if (!reason.trim()) {
      showNotice('A non-empty review reason is required.');
      return;
    }
    if (!window.confirm(
      'Approve this record for model context? This creates a fresh, audited approved memory; the reviewed source remains preserved.',
    )) return;
    try {
      const csrf = await reviewCsrfToken();
      const response = await fetch(`${location.origin}/dashboard/review/approve`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Engraphis-Browser-Session': '1',
          'X-Engraphis-Review-CSRF': csrf,
        },
        body: JSON.stringify({ memory_id: memory.id, reason: reason.trim() }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        const error = new Error(errorMessage(payload, response.status));
        error.status = response.status;
        throw error;
      }
      showNotice('Approved successor created. The reviewed source remains in the audit trail.');
      await selectWorkspace(state.workspace);
      if (payload.id) await selectMemory(payload.id);
    } catch (error) {
      showNotice(`Could not approve this memory: ${error.message}`);
    }
  }

  let graphAssetsPromise = null;
  let graphAssetsController = null;
  let graphAssetsRetry = 0;
  const graphAssetSource = source => graphAssetsRetry ? `${source}&retry=${graphAssetsRetry}` : source;
  function loadScript(src, globalName, signal) {
    if (window[globalName]) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      let settled = false;
      const cleanup = () => {
        if (signal) signal.removeEventListener('abort', abort);
      };
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        cleanup();
        callback(value);
      };
      const abort = () => {
        script.remove();
        const error = new Error(`loading ${globalName} was aborted`);
        error.name = 'AbortError';
        finish(reject, error);
      };
      script.src = src;
      script.dataset.engraphisGraphAsset = 'true';
      script.onload = () => window[globalName]
        ? finish(resolve)
        : finish(reject, new Error(`${globalName} did not register`));
      script.onerror = () => finish(reject, new Error(`could not load ${src}`));
      if (signal) {
        if (signal.aborted) {
          abort();
          return;
        }
        signal.addEventListener('abort', abort, { once: true });
      }
      document.head.append(script);
    });
  }

  function ensureGraphAssets() {
    if (window.ForceGraph && window.EngraphisGraph && window.EngraphisSpacetime) return Promise.resolve();
    if (!graphAssetsPromise) {
      const controller = new AbortController();
      const attempt = loadScript(
        graphAssetSource('/v2-assets/vendor/d3.min.js?v=20260727-final'),
        'd3', controller.signal,
      ).then(() => loadScript(
        graphAssetSource('/v2-assets/vendor/force-graph.min.js?v=20260727-final'),
        'ForceGraph', controller.signal,
      )).then(() => loadScript(
        graphAssetSource('/v2-assets/engraphis-graph.js?v=20260812-black-hole-density-orbits-1'),
        'EngraphisGraph', controller.signal,
      )).then(() => loadScript(
        graphAssetSource('/v2-assets/engraphis-spacetime.js?v=20260812-stable-orbit-lanes-6'),
        'EngraphisSpacetime', controller.signal,
      ));
      graphAssetsPromise = attempt;
      graphAssetsController = controller;
      attempt.catch(() => {
        /* A fetched script can load successfully while failing to execute (for example, a
           stale cached parse error). Retire that URL immediately so the next explicit Reload
           advances the retry query instead of replaying the same broken response forever. */
        if (graphAssetsPromise === attempt) releaseGraphAssetsAttempt(attempt);
      });
    }
    return graphAssetsPromise;
  }

  function releaseGraphAssetsAttempt(attempt) {
    // A browser can leave a script fetch pending indefinitely. Do not let that stale promise
    // become a permanent single-flight lock: remove its fetches and give the next explicit
    // reload a unique URL so it cannot join the browser's already-stalled request.
    if (graphAssetsPromise !== attempt) return;
    graphAssetsPromise = null;
    const controller = graphAssetsController;
    graphAssetsController = null;
    graphAssetsRetry = Math.min(graphAssetsRetry + 1, 10);
    if (controller) controller.abort();
    all('script[data-engraphis-graph-asset="true"]').forEach(script => script.remove());
  }

  function showNotice(message) {
    const text = String(message || '');
    if (noticeTimer !== null) {
      clearTimeout(noticeTimer);
      noticeTimer = null;
    }
    const textEl = byId('notice-text');
    if (textEl) textEl.textContent = text;
    const banner = byId('notice-banner');
    if (!banner) return;
    banner.textContent = text;
    banner.hidden = !text;
    if (!text) {
      banner.removeAttribute('data-tone');
      return;
    }
    banner.dataset.tone = /\b(could not|unavailable|failed|broken|error)\b/i.test(text) ? 'error' : 'info';
    noticeTimer = setTimeout(() => {
      noticeTimer = null;
      if (banner.textContent !== text) return;
      banner.textContent = '';
      banner.hidden = true;
      if (textEl) textEl.textContent = '';
    }, NOTICE_DURATION_MS);
  }

  function updateReleaseUrl(value) {
    const fallback = 'https://github.com/Coding-Dev-Tools/engraphis/releases';
    try {
      const url = new URL(value || fallback, location.href);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : fallback;
    } catch (_) {
      return fallback;
    }
  }

  // A compromised or misconfigured license server could otherwise push a crafted
  // upgrade_url (e.g. `javascript:...`) that executes script when the plan link is
  // clicked. Only http(s) survives; anything else — including a relative/empty value —
  // returns '' so the caller falls back to an inert '#' href.
  function safeUrl(value) {
    if (!value || typeof value !== 'string') return '';
    try {
      const url = new URL(value, location.href);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_) {
      return '';
    }
  }

  function licenseAccessState(license = state.license) {
    const value = license && license.access_state;
    return ['active', 'trial', 'trial_expired', 'lapsed'].includes(value) ? value : 'inactive';
  }

  function licensePlanKey(license = state.license) {
    const value = String((license && license.plan) || 'local').toLowerCase();
    return value === 'pro' || value === 'team' ? value : '';
  }

  function licenseTrialAvailable(license = state.license) {
    return Boolean(license && license.trial && license.trial.available
      && licenseAccessState(license) === 'inactive' && license.plan_source === 'local');
  }

  function licenseHasHostedAccess(license = state.license) {
    const access = licenseAccessState(license);
    return access === 'active' || access === 'trial';
  }

  function withCtaAttribution(raw, content, medium = 'product') {
    const safe = safeUrl(raw);
    if (!safe) return '';
    try {
      const url = new URL(safe, location.href);
      url.searchParams.set('utm_source', 'engraphis');
      url.searchParams.set('utm_medium', medium);
      url.searchParams.set('utm_campaign', 'pro_conversion');
      url.searchParams.set('utm_content', content || 'plans');
      return url.href;
    } catch (_) {
      return safe;
    }
  }

  function hostedPlanUrl(plan, trial, interval = 'monthly', content = plan) {
    const cadence = interval === 'annual' ? 'annual' : 'monthly';
    const license = state.license || {};
    const raw = license[`${plan}_${cadence}_upgrade_url`]
      || license[`${plan}_upgrade_url`] || license.upgrade_url;
    const safe = safeUrl(raw);
    if (!safe) return '';
    try {
      const url = new URL(safe, location.href);
      url.searchParams.set('plan', plan);
      url.searchParams.set('interval', cadence);
      if (trial) url.searchParams.set('trial', plan);
      if (!url.hash) url.hash = 'billing';
      return withCtaAttribution(url.href, content);
    } catch (_) {
      return safe;
    }
  }

  function hostedAccountUrl(content = 'account') {
    const license = state.license || {};
    return withCtaAttribution(license.account_url || license.upgrade_url, content);
  }

  function hostedCta(plan = 'pro', content = 'plans', interval = 'monthly') {
    const stateName = licenseAccessState();
    const currentPlan = licensePlanKey();
    const name = plan === 'team' ? 'Team' : 'Pro';
    if (stateName === 'lapsed') {
      return { label: 'Update billing', href: hostedAccountUrl(content), kind: 'account' };
    }
    if (licenseHasHostedAccess() && (currentPlan === plan
      || (currentPlan === 'team' && plan === 'pro'))) {
      return {
        label: currentPlan === 'team' && plan === 'team' ? 'Open Team Cloud' : 'Open Engraphis Cloud',
        href: hostedAccountUrl(content),
        kind: 'account',
      };
    }
    const trial = licenseTrialAvailable() && stateName === 'inactive';
    return {
      label: trial ? `Start 3-day ${name} trial` : `Subscribe to ${name}`,
      href: hostedPlanUrl(plan, trial, interval, content),
      kind: trial ? 'trial' : 'subscribe',
    };
  }

  function updatePlanBadge() {
    const badge = byId('plan-badge');
    if (!badge || !state.license) return;
    const access = licenseAccessState();
    const plan = licensePlanKey();
    const trial = licenseTrialAvailable();
    const label = access === 'active' ? plan.toUpperCase()
      : access === 'trial' ? 'TRIAL'
        : access === 'lapsed' ? 'BILLING'
          : trial ? 'TRY PRO' : 'GET PRO';
    badge.hidden = access === 'inactive' && trial;
    const aria = licenseHasHostedAccess() ? 'Open Engraphis Cloud account'
      : access === 'lapsed' ? 'Update billing in Plans and billing'
        : trial ? 'Start the 3-day Pro trial in Plans and billing'
          : 'Subscribe to Pro in Plans and billing';
    badge.textContent = label;
    badge.setAttribute('aria-label', aria);
    badge.title = aria;
    const cta = hostedCta(plan || 'pro', 'header');
    const opensAccount = cta.kind === 'account' && Boolean(cta.href);
    badge.href = opensAccount ? cta.href : '#';
    badge.target = opensAccount ? '_blank' : '';
    badge.rel = opensAccount ? 'noopener' : '';
    badge.dataset.opensAccount = String(opensAccount);
  }

  function renderSidebarCta() {
    const copy = byId('sidebar-pro-copy');
    const detail = byId('sidebar-pro-detail');
    const link = byId('sidebar-pro-cta');
    if (!copy || !detail || !link || !state.license) return;
    const renderFeatureCtas = () => {
      [
        ['analytics-pro-cta', 'analytics', 'pro'],
        ['automation-pro-cta', 'automation', 'pro'],
        ['team-cloud-cta', 'team', 'team'],
      ].forEach(([id, content, plan]) => {
        const featureLink = byId(id);
        if (!featureLink) return;
        const featureCta = hostedCta(plan, content);
        featureLink.textContent = featureCta.label;
        featureLink.href = featureCta.href || '#';
        featureLink.setAttribute('aria-disabled', featureCta.href ? 'false' : 'true');
      });
    };
    if (licenseHasHostedAccess()) {
      const cta = hostedCta(licensePlanKey() || 'pro', 'sidebar');
      copy.textContent = 'Thank you for supporting Engraphis.';
      detail.textContent = 'Your subscription funds hosted infrastructure and ongoing development.';
      link.hidden = false;
      link.textContent = cta.label;
      link.href = cta.href || '#';
      link.setAttribute('aria-disabled', cta.href ? 'false' : 'true');
      renderFeatureCtas();
      return;
    }
    const cta = hostedCta('pro', 'sidebar');
    copy.textContent = 'Support continued Engraphis development with Pro.';
    detail.textContent = 'Cloud Sync, Analytics, and managed memory maintenance.';
    link.hidden = false;
    link.textContent = cta.label;
    link.href = cta.href || '#';
    link.setAttribute('aria-disabled', cta.href ? 'false' : 'true');
    link.dataset.proCta = 'sidebar';
    renderFeatureCtas();
  }

  function renderCloudAccountSettings() {
    const target = byId('cloud-account-settings');
    if (!target) return;
    target.replaceChildren();
    const plan = licensePlanKey() || 'pro';
    const cta = hostedCta(plan, 'settings');
    const live = licenseHasHostedAccess();
    const detail = live
      ? 'Your hosted account is connected. Manage membership in Cloud, or edit this workspace’s hosted maintenance policy locally.'
      : licenseAccessState() === 'lapsed'
        ? 'Your hosted subscription needs attention. Update billing in Engraphis Cloud to restore hosted features.'
        : 'Open Engraphis Cloud to start a trial, subscribe, or manage a connected hosted account.';
    const action = node('a', 'primary-button', cta.label);
    action.href = cta.href || '#';
    if (cta.href) {
      action.target = '_blank';
      action.rel = 'noopener';
    } else {
      action.addEventListener('click', event => {
        event.preventDefault();
        showNotice('Connect this installation to Engraphis Cloud to open hosted account settings.');
      });
    }
    const actions = node('div', 'automation-policy-actions');
    actions.append(action);
    if (live) actions.append(button('Configure hosted policy', 'secondary-button', () => switchManageTab('automation')));
    target.append(node('p', 'automation-policy-note', detail), actions);
  }

  function renderUpdateBanner(update) {
    const target = byId('update-banner');
    if (!target) return;
    target.replaceChildren();
    if (!update || !update.enabled || !update.update_available || !update.latest) {
      target.hidden = true;
      return;
    }
    let dismissed = '';
    try {
      dismissed = localStorage.getItem('engraphis-update-dismissed') || '';
    } catch (_) {}
    if (dismissed === update.latest) {
      target.hidden = true;
      return;
    }
    const copy = node('div', 'update-copy');
    copy.append(
      node('strong', '', 'Update available'),
      document.createTextNode(` — Engraphis ${text(update.latest)} is out (you have ${text(update.current || '?')}). Upgrade with `),
      node('code', '', 'pip install -U engraphis'),
      document.createTextNode('.'),
    );
    const actions = node('div', 'update-actions');
    const release = node('a', 'text-button', 'View release →');
    release.href = updateReleaseUrl(update.url);
    release.target = '_blank';
    release.rel = 'noopener';
    const dismiss = button('Dismiss', 'update-dismiss', () => {
      try {
        localStorage.setItem('engraphis-update-dismissed', text(update.latest));
      } catch (_) {}
      target.hidden = true;
      target.replaceChildren();
    });
    actions.append(release, dismiss);
    target.append(copy, actions);
    target.hidden = false;
  }

  function setConnection(message, healthy = true) {
    const status = byId('connection-status');
    if (status) status.textContent = message;
    const dot = document.querySelector('.status-dot');
    if (dot) dot.classList.toggle('unhealthy', !healthy);
  }

  function setDeploymentMode(mode) {
    const el = byId('deployment-mode-badge');
    if (!el) return;
    const isLocal = mode === 'local';
    el.textContent = isLocal ? 'LOCAL' : 'HOSTED';
    el.title = isLocal
      ? 'Local mode: no hosted cloud configured. Data stays on this machine.'
      : 'Hosted mode: connected to Engraphis Cloud.';
    el.classList.toggle('mode-local', isLocal);
    el.classList.toggle('mode-hosted', !isLocal);
    el.hidden = false;
  }

  function memoryType(memory) {
    return memory.memory_type || memory.mtype || 'semantic';
  }

  function memoryTime(memory) {
    return memory.ingested_at || memory.valid_from || memory.last_access;
  }

  function memoryMeta(memory) {
    const meta = node('div', 'memory-meta');
    meta.append(
      node('span', 'type-chip', memoryType(memory)),
      node('span', '', memory.scope || 'workspace'),
      node('span', '', relative(memoryTime(memory))),
    );
    if (memory.pinned) meta.append(node('span', '', 'pinned'));
    return meta;
  }

  function renderMetricValues(stats) {
    const values = [
      stats.memories,
      stats.total_rows,
      stats.workspaces || state.workspaces.length,
      stats.sessions,
    ];
    all('#metrics strong').forEach((element, index) => {
      element.textContent = values[index] == null ? '—' : number(values[index]).toLocaleString();
    });
  }

  function renderTypeBars(stats) {
    const target = byId('type-bars');
    target.replaceChildren();
    const types = stats.by_type || {};
    const entries = Object.entries(types).sort((a, b) => number(b[1]) - number(a[1]));
    if (!entries.length) {
      target.append(empty('No typed memories yet.'));
      return;
    }
    const max = Math.max(1, ...entries.map(([, value]) => number(value)));
    entries.forEach(([name, value]) => {
      const row = node('div', 'type-bar');
      row.append(node('span', '', name));
      const bar = document.createElement('progress');
      bar.max = max;
      bar.value = number(value);
      bar.setAttribute('aria-label', `${name}: ${number(value)}`);
      row.append(bar, node('strong', '', number(value).toLocaleString()));
      target.append(row);
    });
  }

  function savingsQuery(preset = 'all') {
    if (preset === 'current' && state.releaseVersion) {
      return `?release_version=${encodeURIComponent(state.releaseVersion)}`;
    }
    if (preset === '7d') return `?from_ts=${encodeURIComponent(Date.now() / 1000 - 604800)}`;
    return '';
  }

  function savingsScopeLabel(payload) {
    if (payload && payload.scope && payload.scope.workspace === 'all') {
      return ` across ${number(payload.workspace_count).toLocaleString()} visible workspaces`;
    }
    return '';
  }

  function formatSavingsTokens(value) {
    return Math.max(0, Math.round(number(value))).toLocaleString();
  }

  function savingsRatio(value) {
    return Math.max(0, Math.min(1, number(value)));
  }

  function savingsCounts(payload) {
    const estimate = payload && payload.estimated ? payload.estimated : {};
    return {
      estimate,
      eligible: number(estimate.eligible_receipt_count),
      excluded: number(estimate.excluded_receipt_count)
        + number(estimate.unclassified_receipt_count)
        + number(estimate.invalid_estimate_count),
    };
  }

  function renderSavingsOverview(payload) {
    const { estimate, eligible, excluded } = savingsCounts(payload);
    const scopeLabel = savingsScopeLabel(payload);
    const persistentValue = byId('context-savings-persistent-value');
    const persistentMeta = byId('context-savings-persistent-meta');
    const persistentRate = byId('context-savings-persistent-rate');
    const setPersistent = (value, meta, rate = '—') => {
      if (persistentValue) persistentValue.textContent = value;
      if (persistentMeta) persistentMeta.textContent = meta;
      if (persistentRate) persistentRate.textContent = rate;
    };
    if (!eligible) {
      setPersistent('—', excluded ? `${excluded} excluded or unclassified deliveries so far.` : 'Tracking starts with the first eligible delivery.');
      return;
    }
    const ratio = savingsRatio(estimate.savings_ratio);
    setPersistent(
      formatSavingsTokens(estimate.saved_tokens),
      `Across ${eligible.toLocaleString()} eligible context deliveries${scopeLabel} · ${estimate.confidence || 'unknown'} confidence`,
      `${(ratio * 100).toFixed(1)}% estimated reduction`,
    );
  }

  function renderSavingsDetail(payload) {
    const target = byId('savings-detail');
    if (!target) return;
    const { estimate, eligible, excluded } = savingsCounts(payload);
    const scopeLabel = savingsScopeLabel(payload);
    target.replaceChildren();
    const header = node('div', 'savings-detail-header');
    header.append(
      node('strong', 'savings-number', `${formatSavingsTokens(estimate.saved_tokens)} tokens`),
      node('span', '', eligible
        ? `${eligible} eligible deliveries${scopeLabel} · ${(number(estimate.savings_ratio) * 100).toFixed(1)}% estimated reduction`
        : 'No eligible estimates in this range.'),
    );
    const presets = node('div', 'savings-presets');
    [
      ['since', 'Since tracking started'],
      ['current', 'Current release'],
      ['7d', 'Last 7 days'],
      ['all', 'All time'],
    ].forEach(([value, label]) => {
      const control = button(label, '', () => {
        state.savingsPreset = value;
        loadAudit();
      });
      control.classList.toggle('active', state.savingsPreset === value);
      control.setAttribute('aria-pressed', String(state.savingsPreset === value));
      presets.append(control);
    });
    header.append(presets);
    target.append(header);
    if (eligible) {
      target.append(node('p', 'field-note', `Baseline ${formatSavingsTokens(estimate.baseline_tokens)} → emitted ${formatSavingsTokens(estimate.emitted_tokens)} · confidence: ${text(estimate.confidence || 'unknown')}`));
      target.append(node('p', 'field-note', 'Packed context is packing savings; adaptive history is estimated avoided prompt context.'));
      const basisTitle = node('h3', '', 'Savings basis');
      const basisRows = node('div', 'savings-breakdown');
      (estimate.by_basis || []).forEach(row => {
        const item = node('div', 'savings-breakdown-row');
        item.append(
          node('span', '', `${text(row.basis || 'unclassified').replaceAll('_', ' ')} · ${text(row.confidence || 'unknown')}`),
          node('span', '', `${formatSavingsTokens(row.baseline_tokens)} → ${formatSavingsTokens(row.emitted_tokens)} · ${formatSavingsTokens(row.saved_tokens)} saved`),
        );
        basisRows.append(item);
      });
      target.append(basisTitle, basisRows);
      if ((estimate.by_token_counter || []).length) {
        target.append(node('h3', '', 'Token counters'));
        const counterRows = node('div', 'savings-breakdown');
        (estimate.by_token_counter || []).forEach(row => {
          const item = node('div', 'savings-breakdown-row');
          item.append(
            node('span', '', text(row.token_counter || 'unknown')),
            node('span', '', `${formatSavingsTokens(row.saved_tokens)} saved · ${row.receipt_count || 0} eligible deliver${number(row.receipt_count) === 1 ? 'y' : 'ies'}`),
          );
          counterRows.append(item);
        });
        target.append(counterRows);
      }
    }
    target.append(node('p', 'savings-note', `${excluded} excluded or unclassified deliver${excluded === 1 ? 'y' : 'ies'}. Measures estimated prompt-context reduction; it does not measure provider billing.`));
  }

  function renderDecisions(memories) {
    const target = byId('decision-list');
    target.replaceChildren();
    const candidates = memories.slice(0, 3);
    if (!candidates.length) {
      target.append(empty('No high-signal memories need review.'));
      return;
    }
    candidates.forEach(memory => {
      const card = node(memory.id ? 'button' : 'article', 'decision-card memory-link-card');
      if (memory.id) {
        card.type = 'button';
        card.dataset.memoryId = memory.id;
        card.addEventListener('click', () => openMemory(memory));
      }
      const header = node('div', 'decision-card-header');
      header.append(
        node('span', 'tag', memory.pinned ? 'Pinned' : memoryType(memory)),
        node('h3', '', memory.title || memory.id || 'Untitled memory'),
      );
      card.append(header, node('p', '', truncate(memory.content || memory.summary, 360)));
      target.append(card);
    });
  }

  function auditItems(payload) {
    if (Array.isArray(payload)) return payload;
    return payload.audit || payload.entries || payload.records || payload.events || [];
  }

  function receiptItems(payload) {
    if (Array.isArray(payload)) return payload;
    return payload.receipts || payload.entries || payload.records || [];
  }

  function provenanceTimestampMs(item) {
    // Audit rows use seconds (`ts`), while receipts use milliseconds (`ts_ms`).
    // Normalize before merging so both the newest-first order and 120-row cap are
    // chronological across the two independently paginated feeds.
    const raw = item && (item.ts_ms ?? item.ts ?? item.timestamp ?? item.created_at);
    const numeric = Number(raw);
    if (Number.isFinite(numeric)) return numeric < 1e12 ? numeric * 1000 : numeric;
    const parsed = Date.parse(raw);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function auditField(item, ...names) {
    for (const name of names) {
      if (item && item[name] != null && item[name] !== '') return item[name];
    }
    return '';
  }

  function renderActivity(items) {
    const target = byId('activity-body');
    target.replaceChildren();
    if (!items.length) {
      const row = node('tr');
      const cell = node('td', '', 'No audit entries yet.');
      cell.colSpan = 5;
      row.append(cell);
      target.append(row);
      return;
    }
    items.slice(0, 8).forEach(item => {
      const row = node('tr');
      const timestamp = auditField(item, 'ts', 'timestamp', 'created_at', 'valid_from');
      const values = [
        relative(timestamp),
        auditField(item, 'actor', 'source') || 'local operator',
        auditField(item, 'action', 'operation', 'event') || 'recorded',
        auditField(item, 'scope', 'workspace', 'target') || state.workspace,
        truncate(auditField(item, 'hash', 'id', 'receipt_id'), 14) || '—',
      ];
      values.forEach(value => row.append(node('td', '', value)));
      target.append(row);
    });
  }

  function renderProactive(memories, unavailableMessage = '') {
    const target = byId('proactive-list');
    target.replaceChildren();
    if (!memories.length) {
      target.append(empty(unavailableMessage || 'No proactive context is available.'));
      return;
    }
    memories.slice(0, 5).forEach(memory => {
      const row = node('button', 'compact-row');
      row.type = 'button';
      if (memory.id) row.dataset.memoryId = memory.id;
      row.append(
        node('strong', '', memory.title || memory.id || 'Memory'),
        node('span', '', truncate(memory.summary || memory.content, 140)),
      );
      row.addEventListener('click', () => openMemory(memory));
      target.append(row);
    });
  }

  async function loadStats(workspace, epoch) {
    const stats = await api(`/stats?${query(workspace)}`);
    if (epoch !== state.refreshEpoch) return;
    state.stats = stats;
    renderMetricValues(stats);
    renderTypeBars(stats);
  }

  async function loadSavings(epoch) {
    try {
      const payload = await api(`/context-savings${savingsQuery()}`);
      if (epoch !== state.refreshEpoch) return;
      renderSavingsOverview(payload);
    } catch (error) {
      if (epoch !== state.refreshEpoch) return;
      const persistentValue = byId('context-savings-persistent-value');
      const persistentMeta = byId('context-savings-persistent-meta');
      const persistentRate = byId('context-savings-persistent-rate');
      if (persistentValue) persistentValue.textContent = 'Unavailable';
      if (persistentMeta) persistentMeta.textContent = 'Receipt-backed estimate could not be loaded.';
      if (persistentRate) persistentRate.textContent = '—';
    }
  }

  async function loadMemories(workspace, epoch) {
    const payload = await api(`/memories?${query(workspace)}&limit=500`);
    if (epoch !== state.refreshEpoch) return;
    state.memories = payload.memories || [];
    renderLibrary();
  }

  async function loadToday(workspace, epoch) {
    const [proactiveResult, auditResult] = await Promise.allSettled([
      api(`/proactive?${query(workspace)}&k=8`),
      api(`/audit?${query(workspace)}&limit=12`),
    ]);
    if (epoch !== state.refreshEpoch) return;
    const proactive = proactiveResult.status === 'fulfilled'
      ? (proactiveResult.value.memories || proactiveResult.value.results || [])
      : [];
    renderProactive(proactive, proactiveResult.status === 'rejected'
      ? 'Strongest memories are unavailable. Try refreshing this workspace.' : '');
    renderDecisions(proactive);
    renderActivity(auditResult.status === 'fulfilled' ? auditItems(auditResult.value) : []);
    if (auditResult.status === 'rejected') {
      const cell = byId('activity-body').querySelector('td');
      if (cell) cell.textContent = 'Activity is unavailable. Try refreshing this workspace.';
    }
  }

  function renderWorkspaceNames() {
    all('[data-workspace-name]').forEach(element => {
      element.textContent = state.workspace || 'this workspace';
    });
  }

  function workspaceName(item) {
    return typeof item === 'string' ? item : item.name;
  }
  function resetScopedPanels() {
    const messages = {
      'answer-panel': 'Ask a question to receive a grounded answer with citations.',
      'retrieval-list': 'Retrieved memories will appear here.',
      'why-result': 'Trace a claim to inspect live and superseded support.',
      'timeline-result': 'Search a topic to inspect its temporal history.',
      'supersession-list': 'Search a topic to compare closed and current records.',
      'audit-list': 'Open Audit to load this workspace’s records and receipts.',
      'savings-detail': 'Open Audit to load this workspace’s receipt-backed estimate.',
      'analytics-result': 'Open this tab to check availability.',
      'automation-result': 'Open this tab to check availability.',
      'team-result': 'Open this tab to check connection state.',
    };
    Object.entries(messages).forEach(([id, message]) => {
      const target = byId(id);
      if (target) target.replaceChildren(empty(message));
    });
  }

  async function selectWorkspace(name) {
    if (!name) return;
    invalidateConsolidationReview();
    const epoch = ++state.refreshEpoch;
    invalidateScopedRequests();
    closeGraphConnections();
    state.workspace = name;
    state.graphWorkspace = '';
    state.graphData = null;
    state.graphDataIncludeCode = false;
    state.graphDataShowUnlinked = false;
    state.graphDataRepo = '';
    state.selectedMemory = '';
    // Detail/editor handlers close over a memory record.  Clear both before the
    // workspace fetches begin so a stale form cannot write that record into the
    // newly selected workspace.
    state.editorMemory = null;
    byId('memory-editor').hidden = true;
    const memoryDetail = byId('memory-detail');
    memoryDetail.replaceChildren();
    memoryDetail.hidden = true;
    resetScopedPanels();
    state.syncStatus = null;
    if (state.graphEngine) {
      if (state.graphSpacetimeOverlay) {
        state.graphSpacetimeOverlay.destroy();
        state.graphSpacetimeOverlay = null;
      }
      state.graphEngine.destroy();
      state.graphEngine = null;
    }
    byId('workspace-select').value = name;
    renderWorkspaceNames();
    try {
      localStorage.setItem('engraphis-workspace', name);
    } catch (_) {}
    showNotice('');
    try {
      const results = await Promise.allSettled([
        loadStats(name, epoch),
        loadMemories(name, epoch),
        loadToday(name, epoch),
      ]);
      if (epoch !== state.refreshEpoch) return;
      const failed = results.find(result => result.status === 'rejected');
      if (failed) showNotice(`Some workspace panels could not refresh: ${failed.reason.message}`);
      renderWorkspaceList();
      if (state.view === 'relations') await loadGraph();
      if (state.view === 'provenance' && state.provenanceTab === 'audit') await loadAudit();
      if (state.view === 'manage') {
        await loadSavings(epoch);
        await loadManageTab(state.manageTab);
      }
    } catch (error) {
      if (epoch === state.refreshEpoch) showNotice(`Could not refresh ${name}: ${error.message}`);
    }
  }

  function memoryCard(memory) {
    const card = node('button', 'memory-card');
    card.type = 'button';
    card.setAttribute('role', 'option');
    card.dataset.memoryId = memory.id;
    card.setAttribute('aria-selected', String(state.selectedMemory === memory.id));
    if (state.selectedMemory === memory.id) card.classList.add('selected');
    card.append(
      node('h2', '', memory.title || memory.id || 'Untitled memory'),
      node('p', '', truncate(memory.content || memory.summary, 240)),
      memoryMeta(memory),
    );
    card.addEventListener('click', () => openMemory(memory));
    return card;
  }

  function filteredMemories() {
    const filterEl = byId('library-filter');
    const typeEl = byId('library-type');
    const filter = filterEl ? filterEl.value.trim().toLowerCase() : '';
    const type = typeEl ? typeEl.value : '';
    return state.memories.filter(memory => {
      const matchesText = !filter || `${memory.title || ''} ${memory.content || ''} ${memory.summary || ''}`
        .toLowerCase().includes(filter);
      return matchesText && (!type || memoryType(memory) === type);
    });
  }

  function renderLibrary() {
    const target = byId('library-list');
    if (!target.dataset.keyboardBound) {
      target.dataset.keyboardBound = 'true';
      target.addEventListener('keydown', event => {
        const cards = [...target.querySelectorAll('[role="option"]')];
        const current = event.target.closest('[role="option"]');
        if (!current || !cards.length) return;
        let index = cards.indexOf(current);
        if (event.key === 'Home') index = 0;
        else if (event.key === 'End') index = cards.length - 1;
        else if (event.key === 'ArrowDown' || event.key === 'ArrowRight') index = Math.min(cards.length - 1, index + 1);
        else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') index = Math.max(0, index - 1);
        else return;
        event.preventDefault();
        cards.forEach((card, cardIndex) => { card.tabIndex = cardIndex === index ? 0 : -1; });
        cards[index].focus();
      });
    }
    target.replaceChildren();
    const memories = filteredMemories();
    byId('library-count').textContent = `${memories.length.toLocaleString()} ${memories.length === 1 ? 'memory' : 'memories'}`;
    if (!memories.length) {
      target.append(empty(state.memories.length ? 'No memories match these filters.' : 'No active memories in this workspace.'));
      return;
    }
    memories.forEach(memory => target.append(memoryCard(memory)));
    const cards = [...target.querySelectorAll('[role="option"]')];
    const selectedIndex = cards.findIndex(card => card.getAttribute('aria-selected') === 'true');
    cards.forEach((card, index) => { card.tabIndex = index === (selectedIndex >= 0 ? selectedIndex : 0) ? 0 : -1; });
  }

  function definitionList(entries) {
    const list = node('dl', 'definition-list');
    entries.forEach(([term, value]) => {
      const row = node('div');
      row.append(node('dt', '', term), node('dd', '', value || '—'));
      list.append(row);
    });
    return list;
  }

  async function selectMemory(id) {
    state.selectedMemory = id;
    renderLibrary();
    const target = byId('memory-detail');
    target.hidden = false;
    byId('memory-editor').hidden = true;
    target.replaceChildren(empty('Loading memory…'));
    try {
      const payload = await api(`/memory/${encodeURIComponent(id)}?${query()}`);
      const memory = payload.memory || state.memories.find(item => item.id === id);
      if (!memory || state.selectedMemory !== id) return;
      state.editorMemory = memory;
      target.replaceChildren();
      target.append(
        node('p', 'eyebrow', `${memoryType(memory)} · ${memory.scope || 'workspace'}`),
        node('h2', '', memory.title || memory.id || 'Untitled memory'),
        node('p', '', memory.content || memory.summary || 'No content.'),
        memoryMeta(memory),
        definitionList([
          ['Memory id', memory.id],
          ['Importance', memory.importance == null ? '—' : number(memory.importance).toFixed(2)],
          ['Valid from', relative(memory.valid_from)],
          ['Valid to', memory.valid_to ? relative(memory.valid_to) : 'current'],
          ['Source', memory.provenance && (memory.provenance.source || memory.provenance.kind)],
          ['Review', memory.provenance && (memory.provenance.review_state || 'pending')],
        ]),
      );
      const actions = node('div', 'detail-actions');
      const provenance = memory.provenance || {};
      if (provenance.review_state !== 'approved' || provenance.trusted !== true) {
        actions.append(button('Approve for prompt…', 'primary-button', () => approveForPrompt(memory)));
      }
      actions.append(
        button('Edit', 'secondary-button', () => openEditor(memory)),
        button(memory.pinned ? 'Unpin' : 'Pin', 'secondary-button', () => togglePin(memory)),
        button('View timeline', 'secondary-button', () => openMemoryTimeline(memory)),
        button('Retire', 'danger-button', () => retireMemory(memory)),
        button('Secure erase leak', 'danger-button', () => secureEraseMemory(memory)),
      );
      target.append(actions);
      const chain = payload.chain || [];
      if (chain.length) {
        target.append(node('h3', '', 'Supersession chain'));
        const list = node('div', 'timeline-list');
        chain.forEach(item => list.append(simpleMemoryCard(item, 'timeline-card')));
        target.append(list);
      }
    } catch (error) {
      if (state.selectedMemory === id) target.replaceChildren(empty(`Could not inspect memory: ${error.message}`));
    }
  }

  function openMemory(memory) {
    if (!memory || !memory.id) {
      showNotice('This result no longer identifies a memory to inspect.');
      return;
    }
    switchView('library');
    selectMemory(memory.id);
  }

  function simpleMemoryCard(memory, className = 'memory-card') {
    const interactive = Boolean(memory && memory.id);
    const card = node(interactive ? 'button' : 'article', `${className}${interactive ? ' memory-link-card' : ''}`);
    if (interactive) {
      card.type = 'button';
      card.dataset.memoryId = memory.id;
      card.addEventListener('click', () => openMemory(memory));
    }
    card.append(
      node('h3', '', memory.title || memory.id || 'Memory'),
      node('p', '', truncate(memory.content || memory.summary, 500)),
      memoryMeta(memory),
    );
    return card;
  }

  function openEditor(memory = null) {
    state.editorMemory = memory;
    state.editorReturnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement : byId('new-memory-button');
    byId('memory-detail').hidden = true;
    const editor = byId('memory-editor');
    editor.hidden = false;
    byId('editor-title').textContent = memory ? 'Revise memory' : 'New memory';
    byId('editor-memory-title').value = memory ? (memory.title || '') : '';
    byId('editor-memory-type').value = memory ? memoryType(memory) : 'semantic';
    byId('editor-memory-content').value = memory ? (memory.content || memory.summary || '') : '';
    byId('editor-memory-content').removeAttribute('aria-invalid');
    byId('editor-error').hidden = true;
    byId('editor-error').textContent = '';
    byId('editor-memory-importance').value = memory && memory.importance != null ? memory.importance : 0.5;
    byId('editor-memory-title').focus();
  }

  function closeEditor() {
    const returnFocus = state.editorReturnFocus;
    byId('memory-editor').hidden = true;
    byId('memory-detail').hidden = false;
    state.editorMemory = null;
    state.editorReturnFocus = null;
    if (returnFocus && document.contains(returnFocus) && !returnFocus.hidden
      && !returnFocus.disabled) returnFocus.focus();
    else byId('new-memory-button').focus();
  }

  async function saveMemory(event) {
    event.preventDefault();
    const current = state.editorMemory;
    const title = byId('editor-memory-title').value.trim();
    const memoryTypeValue = byId('editor-memory-type').value;
    const content = byId('editor-memory-content').value.trim();
    const importance = number(byId('editor-memory-importance').value);
    const currentImportance = current && current.importance != null
      ? number(current.importance) : 0.5;
    const contentField = byId('editor-memory-content');
    const editorError = byId('editor-error');
    contentField.removeAttribute('aria-invalid');
    editorError.hidden = true;
    editorError.textContent = '';
    if (!content) {
      contentField.setAttribute('aria-invalid', 'true');
      editorError.textContent = 'Enter memory content before saving.';
      editorError.hidden = false;
      showNotice('Enter memory content before saving.');
      contentField.focus();
      return;
    }
    try {
      if (current) {
        if (content !== (current.content || current.summary || '')) {
          const corrected = await api('/correct', {
            method: 'POST',
            body: { id: current.id, workspace: state.workspace, content, reason: 'revised in Ledger' },
          });
          // A correction intentionally creates a replacement.  The core inherits the
          // source importance; carry any label edits to that replacement rather than
          // accidentally applying them to the historical source record.
          if (title !== (current.title || '') || memoryTypeValue !== memoryType(current)
            || importance !== currentImportance) {
            await api('/memory/update', {
              method: 'POST',
              body: {
                id: corrected.id,
                workspace: state.workspace,
                title,
                memory_type: memoryTypeValue,
                importance,
              },
            });
          }
        } else if (title !== (current.title || '') || memoryTypeValue !== memoryType(current)
          || importance !== currentImportance) {
          await api('/memory/update', {
            method: 'POST',
            body: {
              id: current.id,
              workspace: state.workspace,
              title,
              memory_type: memoryTypeValue,
              importance,
            },
          });
        }
        showNotice('Memory revision recorded with temporal history preserved.');
      } else {
        await api('/remember', {
          method: 'POST',
          body: {
            workspace: state.workspace,
            content,
            title,
            mtype: memoryTypeValue,
            scope: 'workspace',
            importance,
            source: 'human:ledger',
            trusted: true,
          },
        });
        showNotice('Memory saved locally.');
      }
      closeEditor();
      await selectWorkspace(state.workspace);
    } catch (error) {
      showNotice(`Could not save memory: ${error.message}`);
    }
  }

  async function togglePin(memory) {
    try {
      await api('/pin', {
        method: 'POST',
        body: { id: memory.id, workspace: state.workspace, pinned: !memory.pinned },
      });
      showNotice(memory.pinned ? 'Memory unpinned.' : 'Memory pinned against decay.');
      await selectWorkspace(state.workspace);
      selectMemory(memory.id);
    } catch (error) {
      showNotice(`Could not change pin: ${error.message}`);
    }
  }

  async function retireMemory(memory) {
    if (!window.confirm(`Retire “${memory.title || memory.id}”? The record stays in temporal history but leaves live recall.`)) return;
    try {
      await api('/retire', {
        method: 'POST',
        body: { id: memory.id, workspace: state.workspace, reason: 'retired in Ledger' },
      });
      state.selectedMemory = '';
      byId('memory-detail').replaceChildren(empty('Memory moved out of live recall. Its history is retained.'));
      showNotice('Memory retired without hard deletion.');
      await selectWorkspace(state.workspace);
    } catch (error) {
      showNotice(`Could not retire memory: ${error.message}`);
    }
  }

  async function secureEraseMemory(memory) {
    const name = memory.title || memory.id;
    if (!window.confirm(`Securely erase “${name}”? This destroys temporal history and local index copies. Rotate the leaked credential; copied exports, snapshots, remote peers, and an already-compromised agent cannot be erased here.`)) return;
    try {
      const result = await api('/secure-erase', {
        method: 'POST', body: { id: memory.id, workspace: state.workspace },
      });
      state.selectedMemory = '';
      byId('memory-detail').replaceChildren(empty('Memory securely erased from this local store. Review the reported backup limitations and rotate the credential.'));
      showNotice(result.vector_index_cleanup === 'deleted'
        ? 'Memory securely erased from local persistence.'
        : 'Memory removed locally; configured vector index needs separate remediation.');
      await selectWorkspace(state.workspace);
    } catch (error) {
      showNotice(`Could not securely erase memory: ${error.message}`);
    }
  }

  function openMemoryTimeline(memory) {
    switchView('provenance');
    switchProvenanceTab('timeline');
    byId('timeline-input').value = memory.title || truncate(memory.content, 80);
    byId('timeline-form').requestSubmit();
  }

  async function importFiles(files) {
    if (!files.length) return;
    const form = new FormData();
    form.append('workspace', state.workspace);
    form.append('memory_type', 'semantic');
    form.append('derive_facts', 'false');
    [...files].forEach(file => form.append('files', file));
    try {
      showNotice(`Importing ${files.length} ${files.length === 1 ? 'file' : 'files'} locally…`);
      const result = await api('/workspaces/import-files', { method: 'POST', body: form });
      showNotice(`Import complete${result.count != null ? ` · ${result.count} memories` : ''}.`);
      await selectWorkspace(state.workspace);
    } catch (error) {
      showNotice(`Import failed: ${error.message}`);
    } finally {
      byId('import-files').value = '';
    }
  }

  const obsidianImport = {
    preview: null, job: null, poll: null, selection: null, sources: [],
    jobWorkspace: '', running: false, reviewGeneration: 0,
  };
  let documentExtensions = null;

  async function obsidianApi(path, options = {}) {
    const csrf = await reviewCsrfToken();
    return api(path, {
      ...options,
      headers: { ...(options.headers || {}), 'X-Engraphis-Review-CSRF': csrf },
    });
  }

  function obsidianSelection() {
    const files = [
      ...byId('obsidian-import-files').files,
      ...byId('obsidian-import-folder').files,
    ];
    const sourceMode = byId('obsidian-source-mode').value;
    const markdown = files.filter(file => /\.md$/i.test(file.name));
    const documents = files.filter(file => {
      const suffix = (file.name.split('.').pop() || '').toLowerCase();
      // The format endpoint is an owner-only convenience hint.  The server still
      // enforces its registry for every byte if the hint is temporarily unavailable.
      return !documentExtensions || documentExtensions.has(suffix);
    });
    const uploadFiles = sourceMode === 'obsidian' ? markdown : documents;
    const attachments = sourceMode === 'obsidian'
      ? files.filter(file => !/\.md$/i.test(file.name)).map(file => ({
      path: file.webkitRelativePath || file.name, size: file.size,
      })) : [];
    const unsupported = sourceMode === 'obsidian'
      ? 0 : files.length - uploadFiles.length;
    const fields = {
      workspace: byId('obsidian-workspace').value.trim(),
      repo: byId('obsidian-repo').value.trim(),
      session_id: byId('obsidian-session').value.trim(),
      scope: byId('obsidian-scope').value.trim(),
      memory_type: byId('obsidian-memory-type').value,
      source_id: byId('obsidian-vault-id').value,
      source_label: byId('obsidian-vault-label').value.trim(),
      on_conflict: byId('obsidian-conflict').value,
      source_mode: sourceMode,
    };
    return { uploadFiles, attachments, unsupported, sourceMode, fields };
  }

  function obsidianFormData(selection, { confirmed = false, reviewToken = '' } = {}) {
    const form = new FormData();
    Object.entries(selection.fields).forEach(([name, value]) => form.append(name, value));
    form.append('confirmed', confirmed ? 'true' : 'false');
    if (reviewToken) form.append('review_token', reviewToken);
    form.append('attachment_manifest', JSON.stringify(selection.attachments));
    selection.uploadFiles.forEach(file => (
      form.append('files', file, file.webkitRelativePath || file.name)
    ));
    return form;
  }

  function invalidateDocumentImportPreview(message = 'Selection changed. Preview again before importing.') {
    obsidianImport.reviewGeneration += 1;
    obsidianImport.preview = null;
    obsidianImport.selection = null;
    byId('obsidian-confirmed').checked = false;
    byId('obsidian-run').disabled = true;
    if (obsidianImport.running) return;
    obsidianImport.job = null;
    obsidianImport.jobWorkspace = '';
    byId('obsidian-cancel').hidden = true;
    delete byId('obsidian-cancel').dataset.jobId;
    renderObsidianReport(null);
    if (message) byId('obsidian-import-progress').textContent = message;
  }

  function updateDocumentImportMode() {
    const obsidian = byId('obsidian-source-mode').value === 'obsidian';
    byId('obsidian-files-label').textContent = obsidian ? 'Individual Markdown notes' : 'Individual documents';
    byId('obsidian-folder-label').textContent = obsidian ? 'Obsidian vault folder' : 'Document folder';
    byId('obsidian-import-description').textContent = obsidian
      ? 'Choose an Obsidian vault folder. Engraphis previews Markdown note bytes and attachment metadata before it writes anything; attachment bytes are never uploaded.'
      : 'Choose individual files or a folder. Engraphis previews supported document formats before it writes anything; uploaded bytes are processed locally and are not kept as dashboard upload copies.';
    byId('obsidian-run').textContent = obsidian ? 'Import vault notes' : 'Import documents';
    byId('obsidian-import-files').value = '';
    byId('obsidian-import-folder').value = '';
    invalidateDocumentImportPreview('Choose files or a folder to preview its import.');
  }

  function updateSourceLabelRequirement() {
    const label = byId('obsidian-vault-label');
    const isNewSource = !byId('obsidian-vault-id').value;
    label.required = isNewSource;
    label.setAttribute('aria-required', isNewSource ? 'true' : 'false');
    label.placeholder = isNewSource ? 'Required for a new source' : 'Saved source label';
  }

  function prefillNewSourceLabelFromFolder() {
    if (byId('obsidian-vault-id').value || byId('obsidian-vault-label').value.trim()) return;
    const firstFolderFile = [...byId('obsidian-import-folder').files]
      .find(file => file.webkitRelativePath && file.webkitRelativePath.includes('/'));
    if (!firstFolderFile) return;
    const folderName = firstFolderFile.webkitRelativePath.split('/')[0].trim();
    if (folderName) byId('obsidian-vault-label').value = folderName;
  }

  function requireNewSourceLabel() {
    if (byId('obsidian-vault-id').value || byId('obsidian-vault-label').value.trim()) return true;
    byId('obsidian-import-progress').textContent = 'Enter a Source label before creating a new source.';
    byId('obsidian-vault-label').focus();
    return false;
  }

  function obsidianRows(result) {
    const rows = result && (result.files || result.details || result.entries || []);
    return Array.isArray(rows) ? rows : [];
  }

  function renderObsidianReport(result) {
    const target = byId('obsidian-import-report');
    const wanted = byId('obsidian-report-filter').value;
    target.replaceChildren();
    const rows = obsidianRows(result).filter(row => {
      const status = String(row.status || row.action || row.result || '').toLowerCase();
      if (wanted === 'all') return true;
      if (wanted === 'reject') return /reject|error|warn|conflict/.test(status) || Boolean(row.warning || row.error);
      return status.includes(wanted);
    });
    if (!rows.length) {
      target.append(empty(wanted === 'all' ? 'No per-file details were returned.' : 'No files match this filter.'));
      return;
    }
    const list = node('ul');
    rows.forEach(row => {
      const status = String(row.status || row.action || row.result || 'reported').toLowerCase();
      const action = row.action && String(row.action).toLowerCase() !== status
        ? ` · action: ${row.action}` : '';
      const format = row.format || row.format_name ? ` · format: ${row.format || row.format_name}` : '';
      const warning = row.warning || row.error || row.reason
        || (Number(row.warning_count) ? `${row.warning_count} warning(s)` : '');
      const item = node('li', '', `${status.toUpperCase()} · ${row.path || row.file || row.relative_path || 'unnamed document'}${format}${action}${warning ? ` · ${warning}` : ''}`);
      item.dataset.status = /reject|error/.test(status) || row.error || row.reason ? 'reject' : status;
      list.append(item);
    });
    target.append(list);
  }

  function obsidianSummary(result, prefix = 'Preview') {
    const counts = result && (result.counts || result);
    const keys = ['documents', 'markdown', 'formats', 'imported', 'updated', 'renamed', 'skipped', 'rejected', 'conflict', 'missing', 'error'];
    const summary = keys.filter(key => Number.isFinite(Number(counts && counts[key])))
      .map(key => `${key.replace('_', ' ')}: ${counts[key]}`);
    const unsupported = obsidianImport.selection && obsidianImport.selection.unsupported;
    const warning = unsupported ? ` · warning: ${unsupported} unsupported files were not uploaded` : '';
    byId('obsidian-import-progress').textContent = summary.length ? `${prefix} · ${summary.join(' · ')}${warning}` : `${prefix} ready.${warning}`;
  }

  async function loadObsidianVaults() {
    const select = byId('obsidian-vault-id');
    try {
      const result = await obsidianApi(`/workspaces/import-documents/sources?${query(state.workspace)}`);
      const vaults = result.sources || result.vaults || result || [];
      obsidianImport.sources = Array.isArray(vaults) ? vaults : [];
      select.replaceChildren(option('', 'New source'));
      obsidianImport.sources.forEach(vault => select.append(option(vault.id, vault.label || vault.name || vault.id)));
    } catch (_) {
      // A first-run vault list is optional; preview/import still present a useful error.
      select.replaceChildren(option('', 'New source'));
      obsidianImport.sources = [];
    }
  }

  async function loadDocumentFormats() {
    try {
      const result = await obsidianApi('/workspaces/import-documents/formats');
      const extensions = Array.isArray(result.extensions) ? result.extensions : [];
      documentExtensions = new Set(extensions.map(extension => String(extension).replace(/^\./, '').toLowerCase()));
    } catch (_) {
      // Server-side validation remains authoritative; do not invent a stale client registry.
      documentExtensions = null;
    }
  }

  function applySelectedDocumentSource() {
    const source = obsidianImport.sources.find(item => item.id === byId('obsidian-vault-id').value);
    if (!source) {
      byId('obsidian-vault-label').value = '';
      updateSourceLabelRequirement();
      invalidateDocumentImportPreview();
      return;
    }
    byId('obsidian-vault-label').value = source.label || source.name || '';
    if (source.repo != null) byId('obsidian-repo').value = source.repo;
    if (source.session_id != null) byId('obsidian-session').value = source.session_id;
    if (source.scope) byId('obsidian-scope').value = source.scope;
    if (source.memory_type) byId('obsidian-memory-type').value = source.memory_type;
    byId('obsidian-source-mode').value = source.adapter === 'obsidian' || source.kind === 'obsidian'
      ? 'obsidian' : 'documents';
    updateSourceLabelRequirement();
    updateDocumentImportMode();
  }

  async function previewObsidianImport() {
    if (obsidianImport.running) return;
    if (!requireNewSourceLabel()) return;
    const selection = obsidianSelection();
    if (!selection.uploadFiles.length) {
      byId('obsidian-import-progress').textContent = selection.sourceMode === 'obsidian'
        ? 'Choose a folder containing Markdown notes.'
        : 'Choose supported documents to import.';
      return;
    }
    invalidateDocumentImportPreview('');
    const generation = obsidianImport.reviewGeneration;
    const type = selection.sourceMode === 'obsidian' ? 'Markdown notes' : 'supported documents';
    const ignored = selection.unsupported ? ` · ${selection.unsupported} unsupported files will not be uploaded` : '';
    byId('obsidian-import-progress').textContent = `Previewing ${selection.uploadFiles.length} ${type}${selection.attachments.length ? ` and ${selection.attachments.length} attachment manifests` : ''}${ignored}…`;
    byId('obsidian-preview').disabled = true;
    try {
      const preview = await obsidianApi('/workspaces/import-documents/preview', {
        method: 'POST', body: obsidianFormData(selection),
      });
      if (generation !== obsidianImport.reviewGeneration) return;
      if (!preview || typeof preview.review_token !== 'string' || !preview.review_token) {
        throw new Error('The server did not bind this preview. Preview again.');
      }
      selection.reviewToken = preview.review_token;
      obsidianImport.selection = selection;
      obsidianImport.preview = preview;
      byId('obsidian-confirmed').checked = false;
      renderObsidianReport(obsidianImport.preview);
      obsidianSummary(obsidianImport.preview);
      byId('obsidian-run').disabled = false;
    } catch (error) {
      if (generation !== obsidianImport.reviewGeneration) return;
      obsidianImport.selection = null;
      obsidianImport.preview = null;
      byId('obsidian-import-progress').textContent = `Preview failed: ${error.message}`;
      byId('obsidian-run').disabled = true;
    } finally {
      byId('obsidian-preview').disabled = false;
    }
  }

  async function pollObsidianImport(jobId, workspace) {
    try {
      const result = await obsidianApi(`/workspaces/import-documents/jobs/${encodeURIComponent(jobId)}?${query(workspace)}`);
      obsidianImport.job = result;
      renderObsidianReport(result);
      obsidianSummary(result, 'Import');
      if (!['complete', 'completed', 'partial', 'failed', 'cancelled'].includes(String(result.state || result.status || '').toLowerCase())) {
        obsidianImport.poll = window.setTimeout(() => pollObsidianImport(jobId, workspace), 750);
        return;
      }
      obsidianImport.running = false;
      obsidianImport.poll = null;
      obsidianImport.selection = null;
      obsidianImport.preview = null;
      byId('obsidian-confirmed').checked = false;
      byId('obsidian-cancel').hidden = true;
      byId('obsidian-run').disabled = true;
      byId('obsidian-preview').disabled = false;
      showNotice('Document import finished.');
      await selectWorkspace(state.workspace);
    } catch (error) {
      byId('obsidian-import-progress').textContent = `Could not read import progress: ${error.message}`;
      byId('obsidian-run').disabled = true;
    }
  }

  async function runObsidianImport(event) {
    event.preventDefault();
    if (!requireNewSourceLabel()) return;
    if (!byId('obsidian-confirmed').checked) {
      byId('obsidian-import-progress').textContent = 'Confirm the selected scope before importing.';
      byId('obsidian-confirmed').focus();
      return;
    }
    const selection = obsidianImport.selection;
    if (!selection || !selection.reviewToken) {
      byId('obsidian-import-progress').textContent = 'Preview this exact selection before importing.';
      byId('obsidian-run').disabled = true;
      return;
    }
    const workspace = selection.fields.workspace;
    const runBody = obsidianFormData(selection, {
      confirmed: true, reviewToken: selection.reviewToken,
    });
    // The server token is one-time. Clear the client copy before the request so
    // a double submit or ambiguous network failure cannot reuse it.
    selection.reviewToken = '';
    byId('obsidian-run').disabled = true;
    byId('obsidian-preview').disabled = true;
    byId('obsidian-import-progress').textContent = 'Starting local document import…';
    obsidianImport.running = true;
    obsidianImport.jobWorkspace = workspace;
    try {
      const result = await obsidianApi('/workspaces/import-documents/run', {
        method: 'POST',
        body: runBody,
      });
      obsidianImport.job = result;
      renderObsidianReport(result);
      obsidianSummary(result, 'Import');
      const jobId = result.job_id || result.id;
      if (jobId) {
        byId('obsidian-cancel').hidden = false;
        byId('obsidian-cancel').dataset.jobId = jobId;
        byId('obsidian-cancel').dataset.workspace = workspace;
        await pollObsidianImport(jobId, workspace);
      }
      else {
        obsidianImport.running = false;
        obsidianImport.selection = null;
        obsidianImport.preview = null;
        byId('obsidian-confirmed').checked = false;
        byId('obsidian-run').disabled = true;
        byId('obsidian-preview').disabled = false;
        showNotice('Document import finished.');
        await selectWorkspace(state.workspace);
      }
    } catch (error) {
      obsidianImport.running = false;
      obsidianImport.selection = null;
      obsidianImport.preview = null;
      byId('obsidian-confirmed').checked = false;
      byId('obsidian-import-progress').textContent = `Import failed: ${error.message} Preview again before retrying.`;
      byId('obsidian-run').disabled = true;
      byId('obsidian-preview').disabled = false;
    }
  }

  async function cancelObsidianImport() {
    const button = byId('obsidian-cancel');
    const jobId = button.dataset.jobId;
    const workspace = button.dataset.workspace || obsidianImport.jobWorkspace;
    if (!jobId || !workspace) return;
    button.disabled = true;
    const form = new FormData();
    form.append('workspace', workspace);
    try {
      await obsidianApi(`/workspaces/import-documents/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', body: form });
      byId('obsidian-import-progress').textContent = 'Cancellation requested; finishing the current document safely…';
    } catch (error) {
      byId('obsidian-import-progress').textContent = `Could not cancel import: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function openObsidianImport() {
    const dialog = byId('obsidian-import-dialog');
    byId('obsidian-confirmed').checked = false;
    if (!obsidianImport.running) {
      if (obsidianImport.poll) window.clearTimeout(obsidianImport.poll);
      obsidianImport.preview = null;
      obsidianImport.job = null;
      obsidianImport.poll = null;
      obsidianImport.selection = null;
      obsidianImport.jobWorkspace = '';
      delete byId('obsidian-cancel').dataset.jobId;
      delete byId('obsidian-cancel').dataset.workspace;
    }
    byId('obsidian-workspace').value = state.workspace;
    byId('obsidian-repo').value = '';
    byId('obsidian-session').value = '';
    byId('obsidian-vault-label').value = '';
    if (!obsidianImport.running) {
      byId('obsidian-import-progress').textContent = 'Choose individual files or a folder to preview its import.';
    }
    byId('obsidian-run').disabled = true;
    byId('obsidian-preview').disabled = obsidianImport.running;
    byId('obsidian-cancel').hidden = !obsidianImport.running;
    if (!obsidianImport.running) renderObsidianReport(null);
    await Promise.all([loadObsidianVaults(), loadDocumentFormats()]);
    byId('obsidian-vault-id').value = '';
    updateSourceLabelRequirement();
    updateDocumentImportMode();
    dialog.showModal();
    byId('obsidian-import-files').focus();
  }

  function renderAnswer(result) {
    const target = byId('answer-panel');
    target.replaceChildren();
    const meta = node('div', 'answer-meta');
    const grounded = Boolean(result.grounded);
    meta.append(
      node('span', `support-pill ${grounded ? 'grounded' : 'abstained'}`, grounded ? 'Grounded' : 'Abstained'),
      node('span', 'support-pill', `Support ${number(result.support).toFixed(2)}`),
      node('span', 'support-pill', `${(result.citations || []).length} citations`),
    );
    target.append(meta);
    if (!grounded) {
      target.append(
        node('h2', '', 'Insufficient evidence'),
        node('p', 'answer-copy', result.reason || 'The active workspace does not support a grounded answer.'),
      );
      return;
    }
    target.append(node('p', 'answer-copy', result.answer || 'The cited memories support this answer.'));
    const citations = node('div', 'citation-list');
    (result.citations || []).forEach(citation => {
      const card = node(citation.id ? 'button' : 'article', 'citation-card memory-link-card');
      if (citation.id) {
        card.type = 'button';
        card.dataset.memoryId = citation.id;
        card.addEventListener('click', () => openMemory(citation));
      }
      card.append(
        node('h3', '', `[${citation.n || citation.number || '•'}] ${citation.title || citation.id || 'Memory'}`),
        node('p', '', citation.content || citation.summary || ''),
        node('div', 'memory-meta', `support ${number(citation.support || citation.score).toFixed(2)} · ${citation.id || ''}`),
      );
      citations.append(card);
    });
    target.append(citations);
  }

  async function askMemory(event) {
    event.preventDefault();
    const input = byId('ask-input');
    const question = input.value.trim();
    if (!question) {
      showNotice('Enter a question before requesting a grounded answer.');
      input.focus();
      return;
    }
    if (!state.workspace) {
      showNotice('Choose a workspace before requesting a grounded answer.');
      return;
    }
    const request = beginScopedRequest('ask');
    const workspace = request.workspace;
    showNotice('');
    const k = number(byId('ask-k').value) || 5;
    byId('answer-panel').replaceChildren(empty('Searching, checking support and building citations…'));
    byId('retrieval-list').replaceChildren(empty('Retrieving candidate memories…'));
    try {
      const [answer, retrieval] = await Promise.all([
        api('/answer', {
          method: 'POST',
          body: { query: question, workspace, k: Math.max(8, k), max_citations: k },
        }),
        // The dashboard /recall route is deliberately read-only (reinforce=False).
        // Keep it alongside /answer for uncited raw candidates without a second
        // reinforcement of the memories that answer already cited.
        api(`/recall?q=${encodeURIComponent(question)}&${query(workspace)}&k=${Math.max(8, k)}`),
      ]);
      if (!isCurrentScopedRequest(request)) return;
      renderAnswer(answer);
      const target = byId('retrieval-list');
      target.replaceChildren();
      const memories = retrieval.memories || [];
      if (!memories.length) target.append(empty('No raw candidates were returned.'));
      else memories.forEach(memory => target.append(simpleMemoryCard(memory)));
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      byId('answer-panel').replaceChildren(empty(`Grounded Ask is unavailable: ${error.message}`));
      byId('retrieval-list').replaceChildren(empty('Raw retrieval did not complete.'));
    }
  }

  function graphCommunityIndex(value) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric;
    const source = text(value);
    let hash = 0;
    for (let index = 0; index < source.length; index += 1) hash = ((hash * 31) + source.charCodeAt(index)) | 0;
    return Math.abs(hash);
  }

  function optionalGraphNumber(value) {
    return value == null || value === '' ? undefined : number(value);
  }

  function graphNodes(payload) {
    const source = payload.nodes || payload.entities || [];
    return source.map(item => ({
      ...item,
      id: item.id,
      name: item.label || item.name || item.id,
      label: item.label || item.name || item.id,
      etype: item.etype || item.type || 'person_or_concept',
      nodeKind: item.node_kind || item.kind || '',
      degree: number(item.degree != null ? item.degree : item.weighted_degree),
      community: item.community_id != null ? graphCommunityIndex(item.community_id)
        : (item.community != null ? graphCommunityIndex(item.community) : undefined),
      community_id: item.community_id == null ? item.community : item.community_id,
      gravity_mass: optionalGraphNumber(item.gravity_mass),
      visual_radius: optionalGraphNumber(item.visual_radius),
      anchor_role: item.anchor_role || '',
      x: Number.isFinite(Number(item.x)) ? Number(item.x) : undefined,
      y: Number.isFinite(Number(item.y)) ? Number(item.y) : undefined,
      repo_names: Array.isArray(item.repo_names) ? item.repo_names.filter(name => typeof name === 'string') : [],
      // The legacy engine reads `repo`; scene-aware engines use `repo_names`. Keeping both
      // makes filtering work during an asset-cache transition without mutating scene data.
      repo: item.repo || (Array.isArray(item.repo_names) ? item.repo_names.join(' ') : ''),
      topic: item.topic || '',
      valid_from: item.valid_from,
      valid_to: item.valid_to,
      ghost: item.ghost === true,
      member_count: optionalGraphNumber(item.member_count),
      visible_by_default: item.visible_by_default !== false,
    }));
  }

  function graphLinks(payload) {
    const source = payload.edges || payload.links || [];
    return source.map((item, index) => ({
      ...item,
      id: item.id || `edge-${index}`,
      source: item.from || (item.source && (item.source.id || item.source)),
      target: item.to || (item.target && (item.target.id || item.target)),
      label: item.label || item.relation || 'related',
      layer: item.layer || 'semantic',
      valid_from: item.valid_from,
      valid_to: item.valid_to,
      rest_length: optionalGraphNumber(item.rest_length),
      spring_strength: optionalGraphNumber(item.spring_strength),
      physics_strength: optionalGraphNumber(item.physics_strength),
      strength: optionalGraphNumber(item.strength),
      ghost: item.ghost === true,
      bridge: item.bridge === true,
      visible_by_default: item.visible_by_default !== false,
    })).filter(item => item.source && item.target);
  }

  function revealGraphNode(id, label = 'Selected entity') {
    const engine = state.graphEngine;
    if (!engine) return;
    let attempts = 0;
    const reveal = () => {
      if (state.graphEngine !== engine) return;
      if (engine.reveal(id)) return;
      attempts += 1;
      if (attempts < 8) {
        window.requestAnimationFrame(reveal);
        return;
      }
      showNotice(`${label} is outside the current graph scope.`);
    };
    reveal();
  }

  function cancelGraphConnectionMemoryLoad() {
    state.graphConnectionsRequest += 1;
    if (state.graphConnectionsController) state.graphConnectionsController.abort();
    state.graphConnectionsController = null;
  }

  function closeGraphConnections() {
    cancelGraphConnectionMemoryLoad();
    const dialog = byId('graph-connections-dialog');
    if (dialog.open) dialog.close();
  }

  function graphMemoryCard(evidence) {
    return {
      id: evidence.memory_id || evidence.id,
      title: evidence.title || evidence.label || evidence.memory_id || evidence.id,
      content: evidence.excerpt || evidence.content || evidence.summary || '',
      mtype: evidence.memory_type || evidence.mtype,
      valid_from: evidence.valid_from,
      valid_to: evidence.valid_to,
      ingested_at: evidence.ingested_at,
      provenance: evidence.provenance,
    };
  }

  function graphMemoryEvidenceCard(memory) {
    const card = node('article', 'graph-memory-evidence');
    card.append(
      node('h4', '', memory.title || memory.id || 'Memory'),
      node('p', '', truncate(memory.content || memory.summary, 500)),
      memoryMeta(memory),
    );
    if (memory.id) {
      card.append(button('Open in Library', 'secondary-button', () => {
        closeGraphConnections();
        openMemory(memory);
      }));
    }
    return card;
  }

  function renderGraphConnectionMemories(memories, message) {
    const target = byId('graph-connection-memory-list');
  target.replaceChildren();
  if (!memories.length) {
    const placeholder = empty(message);
    placeholder.setAttribute('role', 'listitem');
    target.append(placeholder);
    return;
  }
  memories.forEach(memory => {
    const card = graphMemoryEvidenceCard(memory);
    card.setAttribute('role', 'listitem');
    target.append(card);
  });
  }

  function isGraphMemoryNode(item) {
    const kind = String(item.nodeKind || '').toLowerCase();
    const type = String(item.etype || '').toLowerCase();
    return kind === 'memory' || type === 'memory' || type.startsWith('memory_');
  }

  function graphConnectionEntries(item) {
    const graph = state.graphEngine && state.graphEngine.exportData
      ? state.graphEngine.exportData() : state.graphData;
    if (!graph) return [];
    const nodes = new Map(graph.nodes.map(candidate => [candidate.id, candidate]));
    const connections = new Map();
    graph.links.forEach(link => {
      const source = link.source;
      const target = link.target;
      if (source !== item.id && target !== item.id) return;
      const otherId = source === item.id ? target : source;
      const other = nodes.get(otherId);
      if (!other || other.id === item.id) return;
      const entry = connections.get(other.id) || {
        item: other, relations: new Set(), includeHistory: false,
      };
      if (link.label) entry.relations.add(link.label);
      entry.includeHistory = entry.includeHistory || link.ghost === true;
      connections.set(other.id, entry);
    });
    return [...connections.values()].sort((left, right) => {
      const degree = number(right.item.degree) - number(left.item.degree);
      return degree || left.item.name.localeCompare(right.item.name);
    });
  }

  async function showGraphConnectionMemories(item, includeHistory = false) {
    if (!item || !item.id || !state.workspace) return;
    cancelGraphConnectionMemoryLoad();
    const request = ++state.graphConnectionsRequest;
    const workspace = state.workspace;
    const repo = (byId('graph-repo-filter').value || '').trim();
    const title = item.name || item.label || item.id;
    const historicalMemberId = includeHistory && item.ghost && Array.isArray(item.member_ids)
      ? item.member_ids.find(value => typeof value === 'string' && value) || ''
      : '';
    const historyQuery = includeHistory
      ? `&include_history=true${historicalMemberId ? `&member_id=${encodeURIComponent(historicalMemberId)}` : ''}`
      : '';
    byId('graph-connection-memory-title').textContent = `Memories for ${title}`;
    renderGraphConnectionMemories([], 'Loading memory evidence…');
    if (isGraphMemoryNode(item)) {
      const known = state.memories.find(memory => memory.id === item.id);
      if (request !== state.graphConnectionsRequest || workspace !== state.workspace) return;
      renderGraphConnectionMemories(
        [known || graphMemoryCard(item)], 'No memory details are available for this node.',
      );
      return;
    }
    const controller = new AbortController();
    state.graphConnectionsController = controller;
    const timeout = window.setTimeout(() => controller.abort(), GRAPH_CONNECTION_MEMORIES_TIMEOUT_MS);
    try {
      const detail = await api(
        `/graph/entities/${encodeURIComponent(item.id)}/memories?${query(workspace)}${repo ? `&repo=${encodeURIComponent(repo)}` : ''}${graphAsOfQuery()}${historyQuery}`,
        { signal: controller.signal },
      );
      if (request !== state.graphConnectionsRequest || workspace !== state.workspace) return;
      const evidence = detail.evidence || [];
      const total = number(detail.totals && detail.totals.evidence) || evidence.length;
      byId('graph-connection-memory-title').textContent = `${total} ${total === 1 ? 'memory' : 'memories'} for ${title}`;
      renderGraphConnectionMemories(
        evidence.map(graphMemoryCard),
        'No active memories support this connected node.',
      );
    } catch (error) {
      if (request !== state.graphConnectionsRequest || workspace !== state.workspace) return;
      byId('graph-connection-memory-title').textContent = `Memories for ${title}`;
      renderGraphConnectionMemories([], error && error.name === 'AbortError'
        ? 'Memory evidence loading timed out. Choose this node again to retry.'
        : `Could not load memory evidence: ${error.message}`);
    } finally {
      window.clearTimeout(timeout);
      if (state.graphConnectionsController === controller) state.graphConnectionsController = null;
    }
  }

  function graphConnectionRow(entry) {
    const item = entry.item;
    const row = node('article', 'graph-connection-row');
    row.setAttribute('role', 'listitem');
    const details = node('div');
    const relations = [...entry.relations];
    const relationLabel = relations.length ? ` · ${relations.join(', ')}` : '';
    details.append(
      node('h3', '', item.name),
      node('p', '', `${number(item.degree)} connections · ${item.etype}${relationLabel}`),
    );
    const actions = node('div', 'graph-connection-actions');
    actions.append(
      button('Focus graph', 'secondary-button', () => {
        closeGraphConnections();
        revealGraphNode(item.id, item.name);
      }),
      button('Memories', 'secondary-button', () => (
        showGraphConnectionMemories(item, entry.includeHistory)
      )),
    );
    row.append(details, actions);
    return row;
  }

  function openGraphConnections(item) {
    if (!item || !item.id) return;
    cancelGraphConnectionMemoryLoad();
    const dialog = byId('graph-connections-dialog');
    const entries = graphConnectionEntries(item);
    const title = item.name || item.label || item.id;
    byId('graph-connections-title').textContent = `Connected to ${title}`;
    byId('graph-connections-meta').textContent = `${entries.length} direct ${entries.length === 1 ? 'connection' : 'connections'} visible in this graph view`;
    const target = byId('graph-connections-list');
    target.replaceChildren();
    if (!entries.length) target.append(empty('No connected nodes are visible in this graph view.'));
    else entries.forEach(entry => target.append(graphConnectionRow(entry)));
    byId('graph-connection-memory-title').textContent = 'Memories';
    renderGraphConnectionMemories([], 'Choose a connected node to inspect its memory evidence.');
    if (!dialog.open) dialog.showModal();
  }

  function updateGraphFacts(data) {
    const stats = byId('graph-stats');
    stats.replaceChildren();
    const degrees = data.nodes.map(item => number(item.degree)).sort((a, b) => a - b);
    const values = [
      ['Entities', data.nodes.length],
      ['Relations', data.links.length],
      ['Unlinked', data.nodes.filter(item => !number(item.degree)).length],
      ['Median links', degrees.length ? degrees[Math.floor(degrees.length / 2)] : 0],
    ];
    values.forEach(([label, value]) => {
      const item = node('div', 'stat-item');
      item.append(node('span', '', label), node('strong', '', number(value).toLocaleString()));
      stats.append(item);
    });
    const top = byId('graph-top');
    top.replaceChildren();
    [...data.nodes].sort((a, b) => number(b.degree) - number(a.degree)).slice(0, 7).forEach(item => {
      const control = node('button', 'compact-row');
      control.type = 'button';
      control.append(node('strong', '', item.name), node('span', '', `${number(item.degree)} connections · ${item.etype}`));
      control.addEventListener('click', () => openGraphConnections(item));
      top.append(control);
    });
  }

  function updateGraphModeControls() {
    const full = state.graphMode === 'full';
    ['graph-min-degree', 'graph-tune-min-degree', 'graph-collapse'].forEach(id => {
      const scopeControl = byId(id);
      scopeControl.disabled = full;
      scopeControl.title = full
        ? 'Full node graph always includes unlinked nodes and never collapses clusters.'
        : '';
    });
    updateGraphGalaxyControls();
    const preset = GRAPH_PRESET_LABELS[byId('graph-preset').value] || 'Galaxy gravity';
    byId('graph-mode').textContent = `${full ? 'Full node graph' : 'Responsive overview'} · ${preset}`;
  }

  function graphIsGalaxy() {
    return byId('graph-preset').value === 'galaxy';
  }

  function graphSizeBy() {
    return graphIsGalaxy() ? 'evidence_mass' : byId('graph-size').value;
  }

  function updateGraphGalaxyControls() {
    const galaxy = graphIsGalaxy();
    const size = byId('graph-size');
    if (galaxy) {
      if (['degree', 'betweenness'].includes(size.value)) size.dataset.legacyValue = size.value;
      size.value = 'evidence_mass';
      size.disabled = true;
      size.title = 'Galaxy gravity sizes stars by evidence mass.';
    } else {
      size.disabled = false;
      size.title = '';
      if (size.value === 'evidence_mass') size.value = size.dataset.legacyValue || 'degree';
    }
    const labels = galaxy
      ? ['Orbital speed', 'Link distance · tight ↔ loose', 'Gravity strength · loose ↔ tight']
      : ['Repel force', 'Link distance', 'Centre gravity'];
    ['graph-repel-label', 'graph-link-label', 'graph-gravity-label'].forEach((id, index) => {
      const label = byId(id);
      if (label) label.textContent = labels[index];
    });
    byId('graph-spacetime-tuning').hidden = !galaxy;
  }

  function setChoicePressed(selector, dataKey, selected) {
    all(selector).forEach(control => {
      const active = control.dataset[dataKey] === selected;
      control.classList.toggle('active', active);
      control.setAttribute('aria-pressed', String(active));
    });
  }

  function syncGraphChoices() {
    const preset = byId('graph-preset').value;
    const style = byId('graph-style').value;
    const color = byId('graph-color').value;
    const palette = byId('graph-palette').value;
    setChoicePressed('[data-graph-preset-choice]', 'graphPresetChoice', preset);
    setChoicePressed('[data-graph-style-choice]', 'graphStyleChoice', style);
    setChoicePressed('[data-graph-color-choice]', 'graphColorChoice', color);
    setChoicePressed('[data-graph-palette-choice]', 'graphPaletteChoice', palette);
    byId('graph-style-note').textContent = GRAPH_STYLE_NOTES[style] || GRAPH_STYLE_NOTES.classic;
    updateGraphGalaxyControls();
    syncGraphSavedViews();
  }

  function setGraphSwitch(id, on) {
    const control = byId(id);
    control.classList.toggle('on', on);
    control.setAttribute('aria-checked', String(on));
  }

  function graphValueInRange(id, value, fallback) {
    const control = byId(id);
    const raw = Number(value);
    const safe = Number.isFinite(raw) ? raw : fallback;
    const min = Number(control.min);
    const max = Number(control.max);
    return Math.min(Number.isFinite(max) ? max : safe, Math.max(Number.isFinite(min) ? min : safe, safe));
  }

  function graphPresetTuning(preset) {
    const available = window.EngraphisGraph && window.EngraphisGraph.PRESETS;
    const source = (available && available[preset]) || GRAPH_PRESET_TUNING[preset] || GRAPH_PRESET_TUNING.communities;
    return GRAPH_TUNING.reduce((settings, item) => {
      settings[item.key] = source && Number.isFinite(Number(source[item.key]))
        ? Number(source[item.key]) : item.fallback;
      return settings;
    }, {});
  }

  function setGraphTuningControl(item, value) {
    const control = byId(item.id);
    const next = graphValueInRange(item.id, value, item.fallback);
    control.value = String(next);
    const rendered = item.precision ? next.toFixed(item.precision) : String(Math.round(next));
    const output = byId(`${item.id}-output`);
    output.value = rendered;
    output.textContent = rendered;
    return next;
  }

  function graphTuningSettings() {
    return GRAPH_TUNING.reduce((settings, item) => {
      settings[item.key] = number(byId(item.id).value);
      return settings;
    }, { flowSpeed: number(byId('graph-flow-speed').value) });
  }

  function setGraphSpacetimeControl(item, value) {
    const control = byId(item.id);
    const next = graphValueInRange(item.id, value, item.fallback);
    control.value = String(next);
    const rendered = item.precision ? next.toFixed(item.precision) : String(Math.round(next));
    const output = byId(`${item.id}-output`);
    output.value = rendered;
    output.textContent = rendered;
    return next;
  }

  function graphSpacetimeControlSettings() {
    return GRAPH_SPACETIME_TUNING.reduce((settings, item) => {
      settings[item.key] = number(byId(item.id).value);
      return settings;
    }, { orbitPaused: state.graphOrbitPaused });
  }

  function graphSpacetimeSettings() {
    /* The control surface is expressed in intelligible 0–200 / 20–500 ranges while the
       integrator uses dimensionless multipliers. These baseline divisors are deliberate:
       opening the new panel must reproduce the established Galaxy orbit exactly. */
    const controls = graphSpacetimeControlSettings();
    return {
      gravitationalConstant: controls.gravitationalConstant / 100,
      blackHoleMass: controls.blackHoleMass / 160,
      localGravitationalConstant: controls.localGravitationalConstant / 100,
      damping: controls.damping,
      springStiffness: controls.springStiffness / 32,
      orbitPaused: controls.orbitPaused,
    };
  }

  function syncGraphSpacetimeTuning(settings) {
    GRAPH_SPACETIME_TUNING.forEach(item => setGraphSpacetimeControl(item,
      settings && settings[item.key]));
    setGraphSwitch('graph-orbits-pause', settings && settings.orbitPaused === true);
  }

  function syncGraphTuning(settings) {
    GRAPH_TUNING.forEach(item => setGraphTuningControl(item, settings && settings[item.key]));
    const flowSpeed = graphValueInRange('graph-flow-speed', settings && settings.flowSpeed, 45);
    byId('graph-flow-speed').value = String(flowSpeed);
    byId('graph-flow-speed-output').value = String(Math.round(flowSpeed));
    byId('graph-flow-speed-output').textContent = String(Math.round(flowSpeed));
  }

  function graphScope() {
    const full = state.graphMode === 'full';
    return {
      minDegree: full ? 0 : number(byId('graph-min-degree').value),
      showUnlinked: full || state.graphShowUnlinked,
      depth: number(byId('graph-depth').value),
    };
  }

  function applyGraphScope() {
    if (state.graphEngine) state.graphEngine.setScope(graphScope());
  }

  function setGraphMinDegree(value, apply = true) {
    const next = graphValueInRange('graph-min-degree', value, 1);
    byId('graph-min-degree').value = String(next);
    byId('graph-min-degree-output').value = String(Math.round(next));
    byId('graph-min-degree-output').textContent = String(Math.round(next));
    byId('graph-tune-min-degree').value = String(next);
    byId('graph-tune-min-degree-output').value = String(Math.round(next));
    byId('graph-tune-min-degree-output').textContent = String(Math.round(next));
    if (apply) applyGraphScope();
  }

  function setGraphDepth(value, apply = true) {
    const next = graphValueInRange('graph-depth', value, 2);
    byId('graph-depth').value = String(next);
    byId('graph-depth-output').value = String(Math.round(next));
    byId('graph-depth-output').textContent = String(Math.round(next));
    if (apply) applyGraphScope();
  }

  function setGraphShowUnlinked(on, apply = true) {
    const next = on === true;
    state.graphShowUnlinked = next;
    const control = byId('graph-show-unlinked');
    control.textContent = next ? 'Hide unlinked nodes' : 'Show unlinked nodes';
    control.setAttribute('aria-pressed', String(next));
    control.title = next
      ? 'Hide entities that have no relations in this graph view'
      : 'Show entities that have no relations in this graph view';
    if (apply) applyGraphScope();
  }

  function graphLayerState() {
    return all('[data-graph-layer]').reduce((layers, control) => {
      layers[control.dataset.graphLayer] = control.getAttribute('aria-pressed') === 'true';
      return layers;
    }, {});
  }

  function setGraphLayers(layers) {
    const source = layers && typeof layers === 'object' ? layers : GRAPH_DEFAULT_LAYERS;
    all('[data-graph-layer]').forEach(control => {
      const active = source[control.dataset.graphLayer] !== false;
      control.classList.toggle('active', active);
      control.setAttribute('aria-pressed', String(active));
    });
  }

  function updateGraphLayerCounts(data, supplied) {
    const counts = GRAPH_LAYERS.reduce((result, layer) => { result[layer] = 0; return result; }, {});
    if (Array.isArray(supplied)) supplied.forEach(item => {
      if (item && GRAPH_LAYERS.includes(item.layer)) counts[item.layer] = number(item.count);
    });
    else (data.links || []).forEach(link => {
      if (GRAPH_LAYERS.includes(link.layer)) counts[link.layer] += 1;
    });
    GRAPH_LAYERS.forEach(layer => { byId(`graph-layer-${layer}-count`).textContent = counts[layer].toLocaleString(); });
  }

  function syncGraphSavedViews() {
    all('[data-graph-saved-view]').forEach(control => {
      const active = control.dataset.graphSavedView === state.graphSavedView;
      control.classList.toggle('active', active);
      control.setAttribute('aria-pressed', String(active));
    });
  }

  function clearGraphSavedView() {
    if (!state.graphSavedView) return;
    state.graphSavedView = '';
    syncGraphSavedViews();
  }

  function graphPreference(name, fallback, allowed) {
    try {
      const saved = JSON.parse(localStorage.getItem(GRAPH_PREFERENCES_KEY) || '{}');
      const value = saved && typeof saved === 'object' ? saved[name] : undefined;
      return allowed && !allowed.includes(value) ? fallback : value === undefined ? fallback : value;
    } catch (_) {
      return fallback;
    }
  }

  function graphPreferenceSnapshot() {
    return {
      physicsVersion: GRAPH_PHYSICS_VERSION,
      preset: byId('graph-preset').value,
      style: byId('graph-style').value,
      color: byId('graph-color').value,
      palette: byId('graph-palette').value,
      flow: byId('graph-flow').getAttribute('aria-checked') === 'true',
      labels: byId('graph-labels').getAttribute('aria-checked') === 'true',
      tuning: graphTuningSettings(),
      spacetimeTuning: graphSpacetimeControlSettings(),
      minDegree: number(byId('graph-min-degree').value),
      depth: number(byId('graph-depth').value),
      showUnlinked: state.graphShowUnlinked,
      layers: graphLayerState(),
      includeCode: state.graphIncludeCode,
      savedView: state.graphSavedView,
      bridges: byId('graph-bridges').checked,
      collapse: byId('graph-collapse').checked,
      asOf: byId('graph-as-of').value,
      ghosts: byId('graph-ghosts').checked,
      size: byId('graph-size').value,
      repoFilter: byId('graph-repo-filter').value.slice(0, 200),
    };
  }

  function saveGraphPreferences() {
    try {
      localStorage.setItem(GRAPH_PREFERENCES_KEY, JSON.stringify(graphPreferenceSnapshot()));
    } catch (_) {}
  }

  function restoreGraphPreferences() {
    let hasSavedPreferences = false;
    try { hasSavedPreferences = localStorage.getItem(GRAPH_PREFERENCES_KEY) !== null; } catch (_) {}
    const preset = graphPreference('preset', byId('graph-preset').value,
      ['original', 'compact', 'communities', 'radial', 'constellation', 'galaxy']);
    const style = graphPreference('style', byId('graph-style').value,
      ['classic', 'galaxy', 'solar', 'cyber']);
    const color = graphPreference('color', byId('graph-color').value,
      ['community', 'connections', 'type']);
    const palette = graphPreference('palette', byId('graph-palette').value,
      ['theme', 'aurora', 'ocean', 'ember', 'contrast', 'custom']);
    byId('graph-preset').value = preset;
    byId('graph-style').value = style;
    byId('graph-color').value = color;
    byId('graph-palette').value = palette;

    const savedTuning = graphPreference('tuning', {});
    const savedPhysicsVersion = Number(graphPreference('physicsVersion', 0));
    const legacyPhysics = hasSavedPreferences
      && (!Number.isFinite(savedPhysicsVersion) || savedPhysicsVersion < GRAPH_PHYSICS_VERSION);
    const effectiveTuning = savedTuning && typeof savedTuning === 'object'
      ? { ...savedTuning } : {};
    /* Version-one preferences persisted the retired Galaxy default as if it were a custom
       choice. Migrate only that exact old default; a deliberate Gravity 0 or any custom
       spacing/style/layer remains untouched. Once versioned, a later user-selected 48 stays 48. */
    if (legacyPhysics && preset === 'galaxy' && Number(effectiveTuning.repel) === 48) {
      effectiveTuning.repel = 60;
    }
    syncGraphTuning({
      ...graphPresetTuning(preset),
      ...effectiveTuning,
    });
    const savedSpacetimeTuning = graphPreference('spacetimeTuning', {});
    state.graphOrbitPaused = savedSpacetimeTuning && savedSpacetimeTuning.orbitPaused === true;
    syncGraphSpacetimeTuning(savedSpacetimeTuning && typeof savedSpacetimeTuning === 'object'
      ? savedSpacetimeTuning : {});

    const savedMin = Number(graphPreference('minDegree', number(byId('graph-min-degree').value)));
    const minDegree = Number.isFinite(savedMin) ? Math.max(0, Math.min(12, Math.round(savedMin))) : 1;
    setGraphMinDegree(minDegree);
    setGraphDepth(graphPreference('depth', 2));
    const savedRepo = graphPreference('repoFilter', '');
    byId('graph-repo-filter').value = typeof savedRepo === 'string' ? savedRepo.slice(0, 200) : '';
    const savedAsOf = graphPreference('asOf', '');
    byId('graph-as-of').value = typeof savedAsOf === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(savedAsOf)
      ? savedAsOf : '';
    setGraphShowUnlinked(graphPreference('showUnlinked', state.graphShowUnlinked) === true);
    byId('graph-bridges').checked = graphPreference('bridges', byId('graph-bridges').checked) === true;
    byId('graph-collapse').checked = graphPreference('collapse', byId('graph-collapse').checked) === true;
    byId('graph-ghosts').checked = graphPreference('ghosts', byId('graph-ghosts').checked) !== false;
    byId('graph-size').value = graphPreference('size', byId('graph-size').value,
      ['degree', 'betweenness', 'evidence_mass']);
    // Freeze is deliberately session-only. A previously frozen arrangement must not make a
    // freshly opened graph look broken; physics starts live until the person clicks Freeze.
    state.graphFrozen = false;
    setGraphSwitch('graph-freeze', state.graphFrozen);
    setGraphSwitch('graph-flow', graphPreference('flow', true) !== false);
    setGraphSwitch('graph-labels', graphPreference('labels', false) === true);
    const savedLayers = graphPreference('layers', GRAPH_DEFAULT_LAYERS);
    setGraphLayers(GRAPH_LAYERS.reduce((layers, layer) => {
      layers[layer] = !savedLayers || typeof savedLayers !== 'object' || savedLayers[layer] !== false;
      return layers;
    }, {}));
    state.graphIncludeCode = graphPreference('includeCode', false) === true;
    state.graphSavedView = graphPreference('savedView', 'schema', ['', ...Object.keys(GRAPH_SAVED_VIEWS)]);
    syncGraphSavedViews();
    if (legacyPhysics) saveGraphPreferences();
  }

  function savedGraphView(id) {
    if (id === 'custom') {
      try {
        const custom = JSON.parse(localStorage.getItem(GRAPH_CUSTOM_VIEW_KEY) || 'null');
        return custom && typeof custom === 'object' ? custom : null;
      } catch (_) {
        return null;
      }
    }
    return GRAPH_SAVED_VIEWS[id] || null;
  }

  function applyGraphView(id) {
    const view = savedGraphView(id);
    if (!view) {
      showNotice(id === 'custom' ? 'No locally saved graph view yet.' : 'That saved graph view is unavailable.');
      return;
    }
    const preset = Object.prototype.hasOwnProperty.call(GRAPH_PRESET_LABELS, view.preset)
      ? view.preset : byId('graph-preset').value;
    const style = ['classic', 'galaxy', 'solar', 'cyber'].includes(view.style) ? view.style : byId('graph-style').value;
    const color = ['community', 'connections', 'type'].includes(view.color) ? view.color : byId('graph-color').value;
    const palette = ['theme', 'aurora', 'ocean', 'ember', 'contrast', 'custom'].includes(view.palette)
      ? view.palette : byId('graph-palette').value;
    const previousIncludeCode = state.graphIncludeCode;
    const previousShowUnlinked = state.graphShowUnlinked;
    const previousAsOf = byId('graph-as-of').value;
    const previousRepo = (byId('graph-repo-filter').value || '').trim();
    const asOf = typeof view.asOf === 'string' ? view.asOf : previousAsOf;
    const repoFilter = typeof view.repoFilter === 'string'
      ? view.repoFilter.slice(0, 200) : byId('graph-repo-filter').value;
    const nextRepo = repoFilter.trim();
    state.graphIncludeCode = typeof view.includeCode === 'boolean'
      ? view.includeCode : state.graphIncludeCode;
    byId('graph-preset').value = preset;
    byId('graph-style').value = style;
    byId('graph-color').value = color;
    byId('graph-palette').value = palette;
    byId('graph-as-of').value = asOf;
    byId('graph-repo-filter').value = repoFilter;
    if (typeof view.ghosts === 'boolean') byId('graph-ghosts').checked = view.ghosts;
    if (['degree', 'betweenness'].includes(view.size)) byId('graph-size').value = view.size;
    if (typeof view.bridges === 'boolean') byId('graph-bridges').checked = view.bridges;
    if (typeof view.collapse === 'boolean') byId('graph-collapse').checked = view.collapse;
    if (typeof view.flow === 'boolean') setGraphSwitch('graph-flow', view.flow);
    if (typeof view.labels === 'boolean') setGraphSwitch('graph-labels', view.labels);
    setGraphSwitch('graph-freeze', state.graphFrozen);
    syncGraphTuning({
      ...graphPresetTuning(preset),
      ...(view.tuning && typeof view.tuning === 'object' ? view.tuning : {}),
    });
    setGraphMinDegree(view.minDegree == null ? 1 : view.minDegree, false);
    setGraphDepth(view.depth == null ? 2 : view.depth, false);
    setGraphShowUnlinked(view.showUnlinked === true, false);
    setGraphLayers(view.layers);
    state.graphSavedView = id === 'custom' ? '' : id;
    syncGraphChoices();
    if (state.graphEngine) {
      state.graphEngine.apply(graph => {
        graph.setPreset(preset);
        graph.setStyle(style);
        graph.setColorBy(color);
        applyGraphPalette(palette);
        graph.setSettings({
          ...graphTuningSettings(),
          ...graphSpacetimeSettings(),
          flow: byId('graph-flow').getAttribute('aria-checked') === 'true',
          labels: byId('graph-labels').getAttribute('aria-checked') === 'true',
          frozen: state.graphFrozen,
        });
        graph.setScope(graphScope());
        graph.setLayers(graphLayerState());
        graph.setRepoFilter(repoFilter);
        graph.setAsOf(graphAsOfTimestamp());
        graph.setSizeBy(graphSizeBy());
        graph.setBridges(byId('graph-bridges').checked);
        graph.setCollapse(byId('graph-collapse').checked ? 'auto' : false);
        graph.setGhosts(byId('graph-ghosts').checked);
      }, false, !state.graphFrozen);
      state.graphEngine.freeze(state.graphFrozen);
    }
    saveGraphPreferences();
    if (previousIncludeCode !== state.graphIncludeCode
      || previousShowUnlinked !== state.graphShowUnlinked || previousAsOf !== asOf
      || previousRepo !== nextRepo) {
      loadGraph({ force: true });
    }
    const label = all('[data-graph-saved-view]').find(control => control.dataset.graphSavedView === id);
    showNotice(`${id === 'custom' ? 'Saved' : (label ? label.textContent : 'Saved')} graph view applied.`);
  }

  function saveCurrentGraphView() {
    try {
      localStorage.setItem(GRAPH_CUSTOM_VIEW_KEY, JSON.stringify(graphPreferenceSnapshot()));
      byId('graph-saved-view-status').textContent = 'Current graph view saved locally.';
      showNotice('Current graph view saved locally.');
    } catch (_) {
      showNotice('Could not save this graph view in local storage.');
    }
  }

  function resetGraphTuning() {
    const preset = byId('graph-preset').value;
    const previousIncludeCode = state.graphIncludeCode;
    const previousShowUnlinked = state.graphShowUnlinked;
    state.graphIncludeCode = false;
    syncGraphTuning({ ...graphPresetTuning(preset), flowSpeed: 45 });
    state.graphOrbitPaused = false;
    syncGraphSpacetimeTuning({});
    setGraphMinDegree(1, false);
    setGraphDepth(2, false);
    setGraphShowUnlinked(true, false);
    setGraphLayers(GRAPH_DEFAULT_LAYERS);
    clearGraphSavedView();
    if (state.graphEngine) {
      state.graphEngine.apply(graph => {
        graph.setPreset(preset);
        graph.setSettings({ ...graphTuningSettings(), ...graphSpacetimeSettings(), frozen: state.graphFrozen });
        graph.setScope(graphScope());
        graph.setLayers(graphLayerState());
      }, false, !state.graphFrozen);
      state.graphEngine.freeze(state.graphFrozen);
    }
    saveGraphPreferences();
    if (previousIncludeCode || previousShowUnlinked) loadGraph({ force: true });
    showNotice('Graph tuning reset to the selected layout defaults.');
  }

  function applyGraphPalette(name) {
    const graph = state.graphEngine;
    if (!graph) return;
    graph.setPalette(name);
    if (name === 'custom') graph.setTypeColors(GRAPH_CUSTOM_PALETTE);
  }

  function graphThemeColors() {
    const css = getComputedStyle(document.body);
    return {
      accent: css.getPropertyValue('--c-acc').trim() || '#a39bf1',
      surface: css.getPropertyValue('--c-surface').trim() || '#16191f',
      canvas: css.getPropertyValue('--c-bg').trim() || '#0e1014',
      label: css.getPropertyValue('--c-fg').trim() || '#e7e9ee',
      relation_label: css.getPropertyValue('--c-dim').trim() || '#929baa',
    };
  }

  function setGraphTab(tab) {
    all('[data-graph-tab]').forEach(control => {
      const active = control.dataset.graphTab === tab;
      control.classList.toggle('active', active);
      control.setAttribute('aria-selected', String(active));
      control.tabIndex = active ? 0 : -1;
    });
    all('[data-graph-tab-panel]').forEach(panel => {
      panel.hidden = panel.dataset.graphTabPanel !== tab;
    });
  }

  function downloadGraphFile(blob, name) {
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(href), 0);
  }

  function exportGraphJson() {
    const graph = state.graphEngine && state.graphEngine.exportData
      ? state.graphEngine.exportData()
      : state.graphData || { nodes: [], links: [] };
    const payload = {
      workspace: state.workspace,
      exported_at: new Date().toISOString(),
      nodes: graph.nodes,
      links: graph.links,
    };
    downloadGraphFile(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }), 'engraphis-graph.json');
    showNotice('Graph data exported as JSON.');
  }

  function exportGraphPng() {
    const canvas = byId('graph-canvas').querySelector('canvas');
    if (!canvas || !canvas.toBlob) {
      showNotice('The graph image is not ready yet. Export JSON data instead.');
      return;
    }
    canvas.toBlob(blob => {
      if (!blob) {
        showNotice('Could not capture the graph image. Export JSON data instead.');
        return;
      }
      downloadGraphFile(blob, 'engraphis-graph.png');
      showNotice('Graph image exported as PNG.');
    }, 'image/png');
  }

  function graphCountText(nodes, links) {
    const available = number(state.graphMeta && state.graphMeta.nodes_available) || nodes;
    const prefix = state.graphMode === 'full' && state.graphMeta && state.graphMeta.nodes_complete
      ? 'Full graph'
      : 'Overview';
    const entityText = available > nodes
      ? `${number(nodes).toLocaleString()} of ${available.toLocaleString()} entities`
      : `${number(nodes).toLocaleString()} entities`;
    return `${prefix} · ${entityText} · ${number(links).toLocaleString()} relations`;
  }

  function graphStatsChanged(stats) {
    if (!stats) return;
    const nodes = stats.nodes == null ? state.graphData.nodes.length : stats.nodes;
    const links = stats.links == null ? state.graphData.links.length : stats.links;
    byId('graph-count').textContent = graphCountText(nodes, links);
  }

  function graphMetricsChanged(metrics) {
    state.graphMetrics = metrics || {};
    byId('graph-bridge-count').textContent = metrics && metrics.bridges != null
      ? `${metrics.bridges} bridge ${metrics.bridges === 1 ? 'edge' : 'edges'}`
      : '';
  }

  function graphAsOfTimestamp() {
    const value = byId('graph-as-of').value;
    if (!value) return null;
    // A date picker represents the complete selected day, not midnight at its start.
    const timestamp = Date.parse(`${value}T23:59:59.999Z`);
    return Number.isFinite(timestamp) ? timestamp : null;
  }

  function graphAsOfQuery() {
    const timestamp = graphAsOfTimestamp();
    return timestamp === null ? '' : `&as_of=${encodeURIComponent(timestamp / 1000)}`;
  }

  function graphLoadKey(workspace, mode, includeCode, showUnlinked, asOf, repo) {
    return JSON.stringify([workspace, mode, includeCode, showUnlinked, asOf, repo || '']);
  }

  function graphRepositoryNames() {
    const names = new Set();
    const add = value => {
      const name = text(value).trim();
      if (name) names.add(name);
    };
    if (state.graphData && Array.isArray(state.graphData.repositories)) {
      state.graphData.repositories.forEach(add);
    }
    if (state.graphData && Array.isArray(state.graphData.nodes)) {
      state.graphData.nodes.forEach(item => {
        if (item && Array.isArray(item.repo_names)) item.repo_names.forEach(add);
      });
    }
    return names;
  }

  function validatedGraphRepository(value) {
    const candidate = text(value).trim().toLowerCase();
    if (!candidate) return '';
    for (const name of graphRepositoryNames()) {
      if (name.toLowerCase() === candidate) return name;
    }
    return '';
  }

  function isCurrentGraphLoad(request) {
    return Boolean(request
      && request.id === state.graphLoadRequest
      && request.key === state.graphLoadKey
      && request.workspace === state.workspace
      && request.mode === state.graphMode
      && request.includeCode === state.graphIncludeCode
      && request.showUnlinked === state.graphShowUnlinked
      && request.asOf === graphAsOfTimestamp());
  }

  function retryGraphLoad() {
    // A Retry click starts a new request rather than inheriting a timed-out promise. Keep its
    // pending state local to the button so rapid clicks cannot repeatedly cancel fresh work.
    if (state.graphRetryPending) return;
    state.graphRetryPending = true;
    Promise.resolve(loadGraph({ force: true })).finally(() => {
      state.graphRetryPending = false;
    });
  }

  async function loadGraph({ force = false } = {}) {
    if (!state.workspace) return;
    const currentRepo = (byId('graph-repo-filter').value || '').trim();
    if (!force && state.graphWorkspace === state.workspace
      && state.graphDataMode === state.graphMode
      && state.graphDataIncludeCode === state.graphIncludeCode
      && state.graphDataShowUnlinked === state.graphShowUnlinked
      && state.graphDataAsOf === graphAsOfTimestamp()
      && state.graphDataRepo === currentRepo && state.graphData) {
      if (state.graphEngine) state.graphEngine.resize();
      return;
    }
    const targetWorkspace = state.workspace;
    const targetMode = state.graphMode;
    const targetIncludeCode = state.graphIncludeCode;
    const targetShowUnlinked = state.graphShowUnlinked;
    const targetAsOf = graphAsOfTimestamp();
    const targetRepo = currentRepo;
    const fullGraph = targetMode === 'full';
    const key = graphLoadKey(
      targetWorkspace, targetMode, targetIncludeCode, targetShowUnlinked, targetAsOf, targetRepo,
    );
    if (!force && state.graphLoadPromise && state.graphLoadKey === key) {
      return state.graphLoadPromise;
    }
    const request = {
      id: state.graphLoadRequest + 1,
      key,
      workspace: targetWorkspace,
      mode: targetMode,
      includeCode: targetIncludeCode,
      showUnlinked: targetShowUnlinked,
      asOf: targetAsOf,
    };
    const controller = new AbortController();
    const previousController = state.graphLoadController;
    // Publish the new identity before cancelling the old request. Its timeout/error handler
    // then becomes a no-op even when the next request has identical filters (a true retry).
    state.graphLoadRequest = request.id;
    state.graphLoadKey = key;
    state.graphLoadWorkspace = targetWorkspace;
    state.graphLoadMode = targetMode;
    state.graphLoadIncludeCode = targetIncludeCode;
    state.graphLoadShowUnlinked = targetShowUnlinked;
    state.graphLoadAsOf = targetAsOf;
    state.graphLoadController = controller;
    if (previousController && !previousController.signal.aborted) previousController.abort();
    byId('graph-empty').hidden = false;
    byId('graph-empty').textContent = fullGraph
      ? 'Loading every available graph node…'
      : 'Loading the responsive evidence graph…';
    const task = (async () => {
      const assets = ensureGraphAssets();
      const deadline = fullGraph ? GRAPH_FULL_LOAD_TIMEOUT_MS : GRAPH_LOAD_TIMEOUT_MS;
      let rejectTimeout;
      const timeoutPromise = new Promise((_, reject) => {
        rejectTimeout = reject;
      });
      const timeout = window.setTimeout(() => {
        releaseGraphAssetsAttempt(assets);
        if (!controller.signal.aborted) controller.abort();
        const error = new Error('graph loading timed out');
        error.name = 'AbortError';
        rejectTimeout(error);
      }, deadline);
      try {
        const level = fullGraph ? 'complete' : 'overview';
        const limits = fullGraph ? ''
          : `&node_limit=${GRAPH_INITIAL_NODE_LIMIT}&edge_limit=${GRAPH_INITIAL_EDGE_LIMIT}`;
        const connectedOnly = !fullGraph && !targetShowUnlinked ? '&connected_only=true' : '';
        const includeCode = targetIncludeCode ? '&include_code=true' : '';
        const validatedRepo = targetIncludeCode ? validatedGraphRepository(targetRepo) : '';
        const codeRepo = validatedRepo
          ? `&repo=${encodeURIComponent(validatedRepo)}` : '';
        const asOf = targetAsOf === null ? '' : `&as_of=${encodeURIComponent(targetAsOf / 1000)}`;
        const history = targetAsOf === null ? '' : '&include_history=true';
        // Complete Ledger views are canonical entity projections. Memory nodes remain available
        // to compatible callers, but must not change the existing entity evidence click path.
        const memoryProjection = fullGraph ? '&include_memory_nodes=false' : '';
        const [payload] = await Promise.race([
          Promise.all([
            api(`/graph/scene?${query(targetWorkspace)}&level=${level}${limits}${connectedOnly}${includeCode}${codeRepo}${asOf}${history}${memoryProjection}`, { signal: controller.signal }),
            assets,
          ]),
          timeoutPromise,
        ]);
        if (!isCurrentGraphLoad(request)) return;
        if (payload && payload.error) throw new Error(String(payload.error));
        const scene = payload.scene && typeof payload.scene === 'object' ? payload.scene : payload;
        const data = {
          nodes: graphNodes(scene),
          links: graphLinks(scene),
          repositories: Array.isArray(scene.repos)
            ? scene.repos.filter(repo => typeof repo === 'string') : [],
          suggestions: scene.suggestions || [],
          communities: scene.communities || [],
          community_bridges: scene.community_bridges || scene.bridges || [],
          meta: scene.meta || payload.meta || {},
          metadata: scene.metadata || payload.metadata || {},
          layout_seed: scene.layout_seed ?? (scene.meta && scene.meta.layout_seed) ?? (payload.meta && payload.meta.layout_seed),
        };
        state.graphData = data;
        state.graphWorkspace = targetWorkspace;
        state.graphDataMode = targetMode;
        state.graphDataIncludeCode = targetIncludeCode;
        state.graphDataShowUnlinked = targetShowUnlinked;
        state.graphDataAsOf = targetAsOf;
        state.graphDataRepo = targetRepo;
        const sceneMeta = scene.meta || payload.meta || {};
        if (sceneMeta.degraded && sceneMeta.requested_include_code
          && sceneMeta.include_code === false) {
          state.graphIncludeCode = false;
          state.graphDataIncludeCode = false;
          setGraphLayers({ ...graphLayerState(), code: false });
          saveGraphPreferences();
          showNotice(sceneMeta.degraded_reason === 'code_overlay_requires_repository_filter'
            ? 'Code overlay skipped for this workspace. Choose a repository filter to include code relationships.'
            : 'Code overlay was unavailable for this request. Showing the entity graph.');
        }
        state.graphMeta = {
          ...sceneMeta,
          nodes_available: sceneMeta.nodes_available == null ? (sceneMeta.total_nodes == null
            ? data.nodes.length : sceneMeta.total_nodes) : sceneMeta.nodes_available,
          nodes_complete: sceneMeta.nodes_complete == null
            ? (sceneMeta.truncated == null ? fullGraph : !sceneMeta.truncated)
            : sceneMeta.nodes_complete,
        };
        if (state.graphSpacetimeOverlay) {
          state.graphSpacetimeOverlay.destroy();
          state.graphSpacetimeOverlay = null;
        }
        if (state.graphEngine) state.graphEngine.destroy();
        if (typeof window.EngraphisGraph === 'undefined') throw new Error('graph engine asset is unavailable');
        state.graphEngine = window.EngraphisGraph.create(byId('graph-canvas'), {
          renderMode: targetMode,
          onNodeClick: item => openGraphConnections(item),
          onBackgroundClick: () => state.graphEngine && state.graphEngine.clearFocus(),
          onStats: stats => {
            if (state.graphLoadRequest === request.id) graphStatsChanged(stats);
          },
          onMetrics: metrics => {
            if (state.graphLoadRequest === request.id) graphMetricsChanged(metrics);
          },
          onCollapseChange: collapsed => {
            if (targetMode === 'overview') showNotice(collapsed ? 'Clusters collapsed for overview.' : '');
          },
          onSlingshotRelease: () => {
            if (state.graphSpacetimeOverlay && state.graphEngine
              && typeof state.graphEngine.getPhysicsSnapshot === 'function') {
              state.graphSpacetimeOverlay.setSnapshot(state.graphEngine.getPhysicsSnapshot());
            }
          },
        });
        state.graphEngine.apply(graph => {
          graph.setPreset(byId('graph-preset').value);
          graph.setStyle(byId('graph-style').value);
          graph.setColorBy(byId('graph-color').value);
          graph.setThemeColors(graphThemeColors());
          applyGraphPalette(byId('graph-palette').value);
          graph.setSettings({
            ...graphTuningSettings(),
            ...graphSpacetimeSettings(),
            flow: byId('graph-flow').getAttribute('aria-checked') === 'true',
            labels: byId('graph-labels').getAttribute('aria-checked') === 'true',
            frozen: state.graphFrozen,
          });
          graph.setScope(graphScope());
          graph.setLayers(graphLayerState());
          graph.setRepoFilter(byId('graph-repo-filter').value);
          graph.setAsOf(graphAsOfTimestamp());
          graph.setSizeBy(graphSizeBy());
          graph.setBridges(byId('graph-bridges').checked);
          graph.setCollapse(fullGraph ? false : (byId('graph-collapse').checked ? 'auto' : false));
          graph.setGhosts(byId('graph-ghosts').checked);
        }, false, false);
        if (window.EngraphisSpacetime && window.EngraphisSpacetime.create) {
          state.graphSpacetimeOverlay = window.EngraphisSpacetime.create(
            byId('graph-canvas'), state.graphEngine
          );
          state.graphSpacetimeOverlay.setEnabled(graphIsGalaxy());
        }
        state.graphEngine.setData(data);
        state.graphEngine.freeze(state.graphFrozen);
        byId('graph-empty').hidden = Boolean(data.nodes.length);
        if (!data.nodes.length) byId('graph-empty').textContent = 'No entities exist in this workspace yet.';
        updateGraphModeControls();
        updateGraphFacts(data);
        updateGraphLayerCounts(data, scene.layers || payload.layers);
      } catch (error) {
        if (!isCurrentGraphLoad(request)) return;
        byId('graph-empty').hidden = false;
        byId('graph-empty').textContent = error && error.name === 'AbortError'
          ? `${fullGraph ? 'Full graph' : 'Graph'} loading timed out. Choose Retry to try again.`
          : `Graph unavailable: ${error.message}`;
      } finally {
        window.clearTimeout(timeout);
        if (state.graphLoadController === controller) state.graphLoadController = null;
      }
    })();
    state.graphLoadPromise = task;
    try {
      return await task;
    } finally {
      if (state.graphLoadPromise === task) {
        state.graphLoadPromise = null;
        state.graphLoadWorkspace = '';
        state.graphLoadMode = '';
        state.graphLoadIncludeCode = false;
        state.graphLoadShowUnlinked = false;
        state.graphLoadAsOf = null;
        state.graphLoadKey = '';
      }
    }
  }

  function searchGraph(value) {
    const target = byId('graph-search-results');
    target.replaceChildren();
    const needle = value.trim().toLowerCase();
    if (!needle || !state.graphData) return;
    state.graphData.nodes
      .filter(item => item.name.toLowerCase().includes(needle))
      .slice(0, 8)
      .forEach(item => {
        target.append(button(`${item.name} · ${item.degree}`, 'search-result', () => {
          revealGraphNode(item.id, item.name);
          target.replaceChildren();
          openGraphConnections(item);
        }));
      });
  }

  function renderMemoryCollection(target, memories, message) {
    target.replaceChildren();
    if (!memories.length) {
      target.append(empty(message));
      return;
    }
    memories.forEach(memory => target.append(simpleMemoryCard(memory)));
  }

  function switchProvenanceTab(tab) {
    state.provenanceTab = tab;
    all('[data-provenance-tab]').forEach(control => {
      const active = control.dataset.provenanceTab === tab;
      control.classList.toggle('active', active);
      control.setAttribute('aria-selected', String(active));
      control.tabIndex = active ? 0 : -1;
    });
    all('[data-provenance-panel]').forEach(panel => panel.classList.toggle('active', panel.dataset.provenancePanel === tab));
    if (tab === 'audit') loadAudit();
  }

  async function whySearch(event) {
    event.preventDefault();
    const question = byId('why-input').value.trim();
    if (!question) {
      showNotice('Enter a claim or topic before tracing belief.');
      byId('why-input').focus();
      return;
    }
    const request = beginScopedRequest('why');
    showNotice('');
    const target = byId('why-result');
    target.replaceChildren(empty('Tracing the live belief and supersession chain…'));
    try {
      const payload = await api(`/why?q=${encodeURIComponent(question)}&${query(request.workspace)}&k=8`);
      if (!isCurrentScopedRequest(request)) return;
      target.replaceChildren();
      const live = payload.answer || [];
      const superseded = payload.supersedes || [];
      target.append(node('h2', '', 'Live support'));
      if (!live.length) target.append(empty('No live supporting memory was found.'));
      else live.forEach(memory => target.append(simpleMemoryCard(memory)));
      target.append(node('h2', '', 'Superseded history'));
      if (!superseded.length) target.append(empty('No superseded versions were found.'));
      else superseded.forEach(memory => target.append(simpleMemoryCard(memory, 'timeline-card')));
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      target.replaceChildren(empty(`Could not trace belief: ${error.message}`));
    }
  }

  async function timelineSearch(event, supersessionsOnly = false) {
    event.preventDefault();
    const input = byId(supersessionsOnly ? 'supersession-input' : 'timeline-input');
    const target = byId(supersessionsOnly ? 'supersession-list' : 'timeline-result');
    const question = input.value.trim();
    if (!question) {
      showNotice(`Enter a topic before ${supersessionsOnly ? 'finding supersessions' : 'showing history'}.`);
      input.focus();
      return;
    }
    const request = beginScopedRequest(supersessionsOnly ? 'supersessions' : 'timeline');
    showNotice('');
    target.replaceChildren(empty('Loading temporal history…'));
    try {
      const payload = await api(`/timeline?q=${encodeURIComponent(question)}&${query(request.workspace)}&limit=50`);
      if (!isCurrentScopedRequest(request)) return;
      let history = payload.history || [];
      if (supersessionsOnly) history = history.filter(item => item.valid_to || item.expired_at);
      renderMemoryCollection(target, history, supersessionsOnly ? 'No closed versions were found for this topic.' : 'No temporal history was found.');
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      target.replaceChildren(empty(`Could not load history: ${error.message}`));
    }
  }

  function renderAuditCards(audit, receipts) {
    const target = byId('audit-list');
    target.replaceChildren();
    const combined = [
      ...audit.map(item => ({ ...item, _kind: 'audit' })),
      ...receipts.map(item => ({ ...item, _kind: 'receipt' })),
    ].sort((a, b) => provenanceTimestampMs(b) - provenanceTimestampMs(a));
    if (!combined.length) {
      target.append(empty('No audit records or receipts yet.'));
      return;
    }
    combined.slice(0, 120).forEach(item => {
      const card = node('article', 'audit-card');
      card.append(
        node('span', '', relative(provenanceTimestampMs(item))),
        node('strong', '', item.actor || item.source || 'local operator'),
        node('span', 'tag', item.operation || item.action || item.event || item._kind),
        node('span', '', item.scope || item.workspace || item.status || state.workspace),
        node('code', '', truncate(item.hash || item.id || item.receipt_id, 24) || '—'),
      );
      target.append(card);
    });
  }

  async function loadAudit() {
    const request = beginScopedRequest('audit');
    const target = byId('audit-list');
    target.replaceChildren(empty('Loading audit records and receipts…'));
    byId('savings-detail').replaceChildren(empty('Loading receipt-backed estimate…'));
    const [auditResult, receiptsResult, savingsResult] = await Promise.allSettled([
      api(`/audit?${query(request.workspace)}&limit=100`),
      api(`/receipts?${query(request.workspace)}&limit=100`),
      api(`/context-savings${savingsQuery(state.savingsPreset)}`),
    ]);
    if (!isCurrentScopedRequest(request)) return;
    if (savingsResult.status === 'fulfilled') {
      renderSavingsDetail(savingsResult.value);
    } else {
      byId('savings-detail').replaceChildren(empty(`Could not load context savings: ${savingsResult.reason.message}`));
    }
    const audit = auditResult.status === 'fulfilled' ? auditItems(auditResult.value) : [];
    const receipts = receiptsResult.status === 'fulfilled' ? receiptItems(receiptsResult.value) : [];
    if (auditResult.status === 'rejected' && receiptsResult.status === 'rejected') {
      target.replaceChildren(empty('Could not load audit records or receipts. Try again.'));
    } else {
      renderAuditCards(audit, receipts);
    }
    if (auditResult.status === 'rejected' || receiptsResult.status === 'rejected') {
      showNotice('Some provenance data could not be loaded; available records remain visible.');
    }
  }

  async function verifyReceipts() {
    try {
      const result = await api(`/receipts/verify?${query()}`);
      const valid = result.valid != null ? result.valid : result.verified;
      showNotice(valid === false ? 'Receipt verification found a broken chain.' : 'Receipt chain verified.');
    } catch (error) {
      showNotice(`Could not verify receipts: ${error.message}`);
    }
  }

  async function exportReceipts() {
    try {
      const receipts = await api(`/receipts/export?${query()}`);
      const blob = new Blob([JSON.stringify(receipts, null, 2)], { type: 'application/json' });
      const link = document.createElement('a');
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = `engraphis-receipts-${state.workspace || 'workspace'}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      showNotice('Privacy-safe receipts exported.');
    } catch (error) {
      showNotice(`Could not export receipts: ${error.message}`);
    }
  }

  function switchManageTab(tab) {
    state.manageTab = tab;
    all('[data-manage-tab]').forEach(control => {
      const active = control.dataset.manageTab === tab;
      control.classList.toggle('active', active);
      control.setAttribute('aria-selected', String(active));
      control.tabIndex = active ? 0 : -1;
    });
    all('[data-manage-panel]').forEach(panel => panel.classList.toggle('active', panel.dataset.managePanel === tab));
    loadManageTab(tab);
  }

  async function loadManageTab(tab) {
    if (tab === 'workspaces') renderWorkspaceList();
    if (tab === 'settings') await loadSettings();
    if (tab === 'plans') await loadPlans();
    if (tab === 'analytics') await loadHosted('analytics');
    if (tab === 'automation') await loadHosted('automation');
    if (tab === 'team') await loadHosted('team');
    if (tab === 'sync') await loadSync();
  }

  function renderWorkspaceList() {
    const target = byId('workspace-list');
    target.replaceChildren();
    if (!state.workspaces.length) {
      target.append(empty('Create the first workspace to begin.'));
      return;
    }
    state.workspaces.forEach(item => {
      const name = workspaceName(item);
      const card = node('article', `workspace-card${name === state.workspace ? ' active' : ''}`);
      const copy = node('div');
      copy.append(
        node('h3', '', name),
        node('p', '', item.description || `${number(item.memories).toLocaleString()} memories · ${item.visibility || 'local'}`),
      );
      const actions = node('div', 'workspace-card-actions');
      if (name !== state.workspace) actions.append(button('Switch to', 'secondary-button', () => selectWorkspace(name)));
      actions.append(
        button('Rename', 'secondary-button', () => renameWorkspace(name)),
        button('Copy', 'secondary-button', () => copyWorkspace(name)),
      );
      if (name !== state.workspace) actions.append(button('Delete', 'danger-button', () => deleteWorkspace(name)));
      card.append(copy, actions);
      target.append(card);
    });
  }

  async function createWorkspace(event) {
    event.preventDefault();
    const name = byId('new-workspace-name').value.trim();
    const description = byId('new-workspace-description').value.trim();
    if (!name) {
      showNotice('Enter a workspace name before creating it.');
      byId('new-workspace-name').focus();
      return;
    }
    showNotice('');
    try {
      await api('/workspaces/create', {
        method: 'POST',
        body: { workspace: name, description, visibility: 'personal', confirmed: false },
      });
      showNotice(`Workspace ${name} created.`);
      byId('create-workspace-form').reset();
      byId('create-workspace-form').hidden = true;
      await refreshBootstrap(name);
    } catch (error) {
      showNotice(`Could not create workspace: ${error.message}`);
    }
  }

  async function renameWorkspace(name) {
    const next = window.prompt(`Rename ${name} to:`, name);
    if (!next || next === name) return;
    try {
      await api('/workspaces/rename', { method: 'POST', body: { workspace: name, new_name: next } });
      showNotice(`Workspace renamed to ${next}.`);
      await refreshBootstrap(name === state.workspace ? next : state.workspace);
    } catch (error) {
      showNotice(`Could not rename workspace: ${error.message}`);
    }
  }

  async function copyWorkspace(name) {
    try {
      const result = await api('/workspaces/copy', { method: 'POST', body: { workspace: name } });
      showNotice(`Workspace copied${result.name ? ` to ${result.name}` : ''}.`);
      await refreshBootstrap(state.workspace);
    } catch (error) {
      showNotice(`Could not copy workspace: ${error.message}`);
    }
  }

  async function deleteWorkspace(name) {
    if (!window.confirm(`Delete workspace “${name}”? Its memories are retired through the governed workspace operation.`)) return;
    try {
      await api('/workspaces/delete', { method: 'POST', body: { workspace: name } });
      showNotice(`Workspace ${name} deleted.`);
      await refreshBootstrap(state.workspace);
    } catch (error) {
      showNotice(`Could not delete workspace: ${error.message}`);
    }
  }

  function renderObject(target, payload, title = 'Result') {
    target.replaceChildren();
    target.append(node('h3', '', title));
    const entries = Object.entries(payload || {}).filter(([, value]) => ['string', 'number', 'boolean'].includes(typeof value)).slice(0, 12);
    if (entries.length) target.append(definitionList(entries.map(([key, value]) => [key.replaceAll('_', ' '), text(value)])));
    else target.append(node('p', '', 'The operation completed.'));
  }

  function consolidationOptions() {
    return {
      workspace: state.workspace,
      infer: false,
      structured: byId('consolidate-structured').checked,
    };
  }

  function sameConsolidationOptions(left, right) {
    return Boolean(left && right)
      && left.workspace === right.workspace
      && left.infer === right.infer
      && left.structured === right.structured;
  }

  function invalidateConsolidationReview() {
    state.consolidationReview = null;
    byId('consolidate-commit').disabled = true;
  }

  async function previewConsolidation(event) {
    event.preventDefault();
    const options = consolidationOptions();
    invalidateConsolidationReview();
    const target = byId('consolidate-result');
    target.replaceChildren(empty('Scanning local memory without writing changes…'));
    try {
      const result = await api('/consolidate', {
        method: 'POST',
        body: {
          ...options,
          dry_run: true,
        },
      });
      // The preview is an approval only for the exact workspace and choices that
      // produced it; never let a late response authorize a changed form.
      if (!sameConsolidationOptions(options, consolidationOptions())) return;
      state.consolidationReview = options;
      byId('consolidate-commit').disabled = false;
      renderObject(target, result, 'Dry preview complete · nothing written');
    } catch (error) {
      invalidateConsolidationReview();
      target.replaceChildren(empty(`Preview failed: ${error.message}`));
    }
  }

  async function commitConsolidation() {
    const options = consolidationOptions();
    if (!sameConsolidationOptions(state.consolidationReview, options)) {
      invalidateConsolidationReview();
      showNotice('Run a new dry preview after changing the workspace or consolidation options.');
      return;
    }
    if (!window.confirm(`Commit the reviewed consolidation result for ${state.workspace}? Original records remain in temporal history.`)) return;
    const target = byId('consolidate-result');
    target.replaceChildren(empty('Committing the reviewed local consolidation…'));
    try {
      const result = await api('/consolidate', {
        method: 'POST',
        body: {
          ...options,
          dry_run: false,
        },
      });
      invalidateConsolidationReview();
      renderObject(target, result, 'Consolidation committed');
      await selectWorkspace(state.workspace);
    } catch (error) {
      target.replaceChildren(empty(`Commit failed: ${error.message}`));
    }
  }

  function automationCheckbox(id, label, checked) {
    const field = node('label', 'check-row');
    const input = node('input');
    input.id = id;
    input.type = 'checkbox';
    input.checked = Boolean(checked);
    field.htmlFor = id;
    field.append(input, document.createTextNode(label));
    return field;
  }

  function automationNumber(id, label, value, min, max) {
    const field = node('label', '', label);
    const input = node('input');
    input.id = id;
    input.type = 'number';
    input.min = String(min);
    input.max = String(max);
    input.value = String(value);
    field.htmlFor = id;
    field.append(input);
    return field;
  }

  function renderAutomationPolicy(policy, workspace = state.workspace) {
    const target = byId('automation-result');
    if (!target) return;
    target.replaceChildren();
    const form = node('form', 'automation-policy-form');
    form.dataset.workspace = workspace;
    form.dataset.lastRun = String(policy.last_run || '');
    if (policy.bootstrap_required) {
      form.append(
        node('p', 'automation-policy-note', 'Hosted automation is not initialized for this workspace. Initializing it uploads one bounded workspace snapshot and saves the default Cloud policy. No upload occurs until you choose this action.'),
      );
      const actions = node('div', 'automation-policy-actions');
      const bootstrap = node('button', 'primary-button', 'Initialize hosted automation');
      bootstrap.type = 'button';
      bootstrap.addEventListener('click', () => bootstrapAutomation(workspace, bootstrap));
      actions.append(bootstrap);
      form.append(actions);
      target.append(form);
      return;
    }
    const enabled = Boolean(policy.enabled);
    const dreamEnabled = policy.dream_enabled != null ? policy.dream_enabled : policy.dream;
    const lastRun = policy.last_run ? ` Last managed run: ${relative(policy.last_run)}.` : '';
    form.append(
      node('p', 'automation-policy-note', enabled
        ? `This workspace has an active hosted maintenance policy.${lastRun}`
        : 'Hosted maintenance is paused for this workspace.'),
      automationCheckbox('automation-enabled', 'Enable hosted maintenance', enabled),
      automationNumber('automation-cadence', 'Run every (hours)', Math.max(1, Number(policy.cadence_hours) || 24), 1, 8760),
      automationCheckbox('automation-dream', 'Enable Auto Dreaming after accumulation and idle time', dreamEnabled),
      automationNumber('automation-dream-min', 'Minimum new memories', Math.max(1, Number(policy.dream_min_new) || 25), 1, 100000),
      automationNumber('automation-dream-idle', 'Idle minutes before Dreaming', Math.max(0, Number(policy.dream_idle_minutes) || 0), 0, 10080),
      automationCheckbox('automation-infer', 'Allow hosted relationship inference proposals', policy.infer),
      node('p', 'automation-policy-note', `Cloud Sync: ${CLOUD_SYNC_PRIVACY_NOTICE} Managed compute: saving an enabled policy submits a bounded snapshot of this workspace’s normal and sensitive memory content to Engraphis Cloud. Cloud work returns proposals and never silently changes the local database.`),
    );
    const actions = node('div', 'automation-policy-actions');
    const save = node('button', 'primary-button', enabled ? 'Save & send policy to Cloud' : 'Save hosted policy');
    save.type = 'submit';
    actions.append(save);
    form.append(actions);
    form.addEventListener('submit', saveAutomationPolicy);
    target.append(form);
  }

  async function bootstrapAutomation(workspace, control) {
    if (!workspace || workspace !== state.workspace) return;
    if (!window.confirm(
      `Initialize hosted automation for ${workspace}? Engraphis will upload one bounded snapshot of that workspace's normal and sensitive memory content and save the default Cloud policy.`,
    )) return;
    const request = beginScopedRequest('automation-bootstrap');
    control.disabled = true;
    control.textContent = 'Initializing…';
    try {
      const policy = await api(`/automation/bootstrap?${query(workspace)}`, { method: 'POST' });
      if (!isCurrentScopedRequest(request) || !control.isConnected) return;
      state.hostedLoaded.add(`automation:${workspace}`);
      renderAutomationPolicy(policy, workspace);
      showNotice('Hosted automation initialized.');
    } catch (error) {
      if (!isCurrentScopedRequest(request) || !control.isConnected) return;
      control.disabled = false;
      control.textContent = 'Initialize hosted automation';
      showNotice(`Could not initialize hosted automation: ${error.message}`);
    }
  }

  async function saveAutomationPolicy(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const workspace = form.dataset.workspace || '';
    if (!workspace || workspace !== state.workspace) {
      showNotice('This policy belongs to a different workspace. Reloading the active workspace policy.');
      state.hostedLoaded.delete(`automation:${state.workspace}`);
      await loadHosted('automation');
      return;
    }
    const request = beginScopedRequest('automation-save');
    const policy = {
      enabled: byId('automation-enabled').checked,
      cadence_hours: Math.max(1, Number(byId('automation-cadence').value) || 1),
      dream_enabled: byId('automation-dream').checked,
      dream_min_new: Math.max(1, Number(byId('automation-dream-min').value) || 1),
      dream_idle_minutes: Math.max(0, Number(byId('automation-dream-idle').value) || 0),
      infer: byId('automation-infer').checked,
    };
    if (policy.enabled && !window.confirm(
      `Save this hosted policy for ${workspace}? Engraphis will submit a bounded snapshot of that workspace’s normal and sensitive memory content to Cloud for managed compute.\n\nCloud Sync: ${CLOUD_SYNC_PRIVACY_NOTICE}`,
    )) return;
    const save = form.querySelector('button[type="submit"]');
    if (save) {
      save.disabled = true;
      save.textContent = 'Saving…';
    }
    try {
      const saved = await api(`/automation?${query(workspace)}`, { method: 'POST', body: policy });
      if (!isCurrentScopedRequest(request) || !form.isConnected) return;
      state.hostedLoaded.add(`automation:${workspace}`);
      renderAutomationPolicy({ ...saved, last_run: form.dataset.lastRun }, workspace);
      showNotice('Hosted maintenance policy saved to Engraphis Cloud.');
    } catch (error) {
      if (!isCurrentScopedRequest(request) || !form.isConnected) return;
      if (save) {
        save.disabled = false;
        save.textContent = policy.enabled ? 'Save & send policy to Cloud' : 'Save hosted policy';
      }
      showNotice(`Could not save the hosted policy: ${error.message}`);
    }
  }

  async function loadHosted(kind) {
    const request = beginScopedRequest(`hosted-${kind}`);
    const workspace = request.workspace;
    const cacheKey = `${kind}:${workspace}`;
    const target = byId(`${kind}-result`);
    if (state.hostedLoaded.has(cacheKey)) return;
    target.replaceChildren(empty(`Checking ${kind} availability…`));
    try {
      if (kind === 'team') {
        const [auth, license] = await Promise.all([api('/auth/state'), api('/license')]);
        if (!isCurrentScopedRequest(request)) return;
        state.license = license;
        updatePlanBadge();
        renderSidebarCta();
        setDeploymentMode(auth.deployment_mode || 'local');
        renderObject(target, {
          deployment_mode: auth.deployment_mode || 'local',
          local_mode: auth.mode || 'open',
          hosted_team: Boolean(auth.hosted_team),
          local_invitations: Boolean(auth.local_invitations),
          cloud_access: Boolean(license.cloud_access_active),
          plan: license.plan || 'local',
        }, 'Connection state');
      } else {
        const result = await api(`/${kind}?${query(workspace)}`);
        if (!isCurrentScopedRequest(request)) return;
        if (kind === 'automation') renderAutomationPolicy(result, workspace);
        else renderObject(target, result, `${kind[0].toUpperCase()}${kind.slice(1)} status`);
      }
      if (isCurrentScopedRequest(request)) state.hostedLoaded.add(cacheKey);
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      target.replaceChildren(empty(`${kind[0].toUpperCase()}${kind.slice(1)} is not active: ${error.message}`));
    }
  }
  function syncSummaryMessage(summary) {
    if (!summary) return 'No sync has run in this dashboard process.';
    const attempted = number(summary.attempted);
    const succeeded = number(summary.succeeded);
    const errors = Array.isArray(summary.errors) ? summary.errors : [];
    const complete = summary.complete === true
      || (summary.complete !== false && errors.length === 0 && succeeded >= attempted);
    const counts = `${succeeded}/${attempted} eligible workspaces completed`;
    const changes = `${number(summary.added)} added · ${number(summary.updated)} updated · ${number(summary.exported)} exported`;
    return `${complete ? 'Last sync complete' : 'Last sync incomplete'} · ${counts} · ${changes}${errors.length ? ` · ${errors.length} ${errors.length === 1 ? 'error' : 'errors'}` : ''}.`;
  }

  function renderSyncStatus(status, message = '') {
    state.syncStatus = status || {};
    const target = byId('sync-result');
    if (!target) return;
    target.replaceChildren();
    if (message) target.append(empty(message, 'form-error'));
    target.append(
      node('p', 'automation-policy-note', syncSummaryMessage(state.syncStatus.last)),
      definitionList([
        ['Connection', state.syncStatus.available ? 'Connected' : 'Not connected'],
        ['Mode', state.syncStatus.read_only ? 'Read only · pull without upload' : 'Push and pull'],
        ['Credential', state.syncStatus.has_cloud_session
          ? 'Managed Cloud session'
          : (state.syncStatus.has_user_token ? 'Local sync token' : 'None')],
      ]),
      node('p', 'automation-policy-note', CLOUD_SYNC_PRIVACY_NOTICE),
    );
    const actions = node('div', 'automation-policy-actions');
    const run = button('Sync now', 'primary-button', runCloudSync);
    run.id = 'sync-now';
    run.disabled = !state.syncStatus.available;
    actions.append(run);
    if (!state.syncStatus.available) {
      const url = safeUrl(state.syncStatus.upgrade_url) || hostedAccountUrl('sync');
      if (url) {
        const connect = node('a', 'secondary-button', 'Connect Engraphis Cloud');
        connect.href = url;
        connect.target = '_blank';
        connect.rel = 'noopener';
        actions.append(connect);
      }
    }
    target.append(actions);
  }

  async function loadSync() {
    const request = beginScopedRequest('sync-status');
    const target = byId('sync-result');
    if (!target) return;
    target.replaceChildren(empty('Checking Cloud Sync connection…'));
    try {
      const status = await api('/sync/status');
      if (!isCurrentScopedRequest(request)) return;
      renderSyncStatus(status);
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      target.replaceChildren(empty(`Could not load Cloud Sync status: ${error.message}`, 'form-error'));
    }
  }

  async function runCloudSync() {
    const request = beginScopedRequest('sync-run');
    const buttonNode = byId('sync-now');
    if (buttonNode) {
      buttonNode.disabled = true;
      buttonNode.textContent = 'Syncing…';
    }
    try {
      const result = await api('/sync/run', { method: 'POST' });
      if (!isCurrentScopedRequest(request)) return;
      const summary = result && result.summary ? result.summary : {};
      const responseOk = Boolean(result) && result.ok !== false;
      const displayedSummary = responseOk ? summary : { ...summary, complete: false };
      renderSyncStatus({ ...(state.syncStatus || {}), last: displayedSummary });
      const errors = Array.isArray(summary.errors) ? summary.errors : [];
      const complete = responseOk && (summary.complete === true
        || (summary.complete !== false && errors.length === 0
          && number(summary.succeeded) >= number(summary.attempted)));
      showNotice(complete
        ? 'Cloud Sync completed for every eligible workspace.'
        : 'Cloud Sync is incomplete. Review the status before retrying.');
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      renderSyncStatus(state.syncStatus || {}, `Cloud Sync failed: ${error.message}`);
      showNotice(`Cloud Sync failed: ${error.message}`);
    }
  }

  function planPrices() {
    const annual = byId('billing-select').value === 'annual';
    return annual
      ? { free: '$0', pro: '$100 / owner / year', team: '$200 / seat / year' }
      : { free: '$0', pro: '$10 / owner / month', team: '$20 / seat / month' };
  }

  function renderPlans() {
    const target = byId('plan-cards');
    target.replaceChildren();
    const prices = planPrices();
    const plans = [
      { id: 'free', name: 'Free', price: prices.free, note: 'The complete local memory engine and every core operation.', action: 'Current local plan' },
      { id: 'pro', name: 'Pro', price: prices.pro, note: 'Cloud sync, managed automation and portfolio analytics.' },
      { id: 'team', name: 'Team', price: prices.team, note: 'Shared workspaces, member roles, seats and remote agents.' },
    ];
    plans.forEach(plan => {
      const card = node('article', `plan-card${plan.id === 'pro' ? ' featured' : ''}`);
      card.append(
        node('p', 'eyebrow', plan.id === (state.license && state.license.plan) ? 'Current plan' : plan.id),
        node('h2', '', plan.name),
        node('div', 'price', plan.price),
        node('p', '', plan.note),
      );
      if (plan.id === 'pro') {
        card.append(
          node('p', 'plan-support', 'Support continued Engraphis development with Pro. Your subscription helps cover hosted infrastructure and ongoing development.'),
          node('p', 'plan-benefits', 'Cloud Sync, Analytics, Auto Consolidation, and Auto Dreaming across your installations.'),
        );
      }
      if (plan.id === 'free') {
        const status = node('span', 'secondary-button', plan.action);
        card.append(status);
      } else {
        const interval = byId('billing-select').value === 'annual' ? 'annual' : 'monthly';
        const cta = hostedCta(plan.id, 'plans', interval);
        const action = node('a', 'primary-button', cta.label);
        const url = cta.href;
        action.dataset.proCta = plan.id;
        action.href = url || '#';
        if (url) {
          action.target = '_blank';
          action.rel = 'noopener';
        } else {
          action.addEventListener('click', event => {
            event.preventDefault();
            showNotice('Connect this installation to Engraphis Cloud to open hosted plan options.');
          });
        }
        card.append(action);
      }
      target.append(card);
    });
  }

  async function loadPlans() {
    const request = beginScopedRequest('plans');
    try {
      const license = await api(`/license?${query(request.workspace)}`);
      if (!isCurrentScopedRequest(request)) return;
      state.license = license;
    } catch (_) {
      if (!isCurrentScopedRequest(request)) return;
      state.license = { plan: 'free' };
    }
    updatePlanBadge();
    renderSidebarCta();
    renderPlans();
  }

  function llmSnippet(provider, model, keySet) {
    return [
      `ENGRAPHIS_LLM_PROVIDER=${provider}`,
      `ENGRAPHIS_LLM_MODEL=${model}`,
      'ENGRAPHIS_LLM_API_KEY=<your-key>',
      keySet ? 'ENGRAPHIS_EXTRACTOR=llm_structured' : '# set ENGRAPHIS_EXTRACTOR=llm_structured to use it',
      'ENGRAPHIS_LLM_AUTO_EXTRACT=1',
    ].join('\n');
  }

  function setLlmTestResult(message, tone = '') {
    const target = byId('llm-test-result');
    if (!target) return;
    target.textContent = message;
    target.dataset.tone = tone;
  }

  function updateLlmSnippet(status) {
    const provider = byId('llm-provider').value;
    const model = byId('llm-model').value;
    byId('llm-env-snippet').value = llmSnippet(provider, model, Boolean(status.key_set));
  }

  function renderLlmSettings(status) {
    const target = byId('llm-connection');
    target.replaceChildren();
    const defaults = status.default_models || {};
    const provider = status.provider || 'openai';
    const model = status.model || defaults[provider] || '';
    const providers = [...new Set([...Object.keys(defaults), provider])];
    const models = [...new Set([model, ...Object.values(defaults)].filter(Boolean))];
    const configured = Boolean(status.configured);
    const extractionEnabled = Boolean(status.extractor_enabled);
    const stateLabel = status.working ? 'verified' : (configured ? 'configured' : 'not configured');

    const overview = node('div', 'llm-status-line');
    overview.append(
      node('span', '', 'Provider · Model'),
      node('span', `llm-status-badge ${configured ? 'ready' : 'muted'}`, stateLabel),
    );

    const pickerGrid = node('div', 'llm-picker-grid');
    const providerLabel = node('label', '', 'Provider');
    const providerSelect = node('select');
    providerSelect.id = 'llm-provider';
    providers.forEach(value => providerSelect.append(option(value, value, value === provider)));
    providerLabel.htmlFor = providerSelect.id;
    providerLabel.append(providerSelect);
    const modelLabel = node('label', '', 'Model');
    const modelSelect = node('select');
    modelSelect.id = 'llm-model';
    models.forEach(value => modelSelect.append(option(value, value, value === model)));
    modelLabel.htmlFor = modelSelect.id;
    modelLabel.append(modelSelect);
    pickerGrid.append(providerLabel, modelLabel);

    const keyState = node('p', 'llm-key-state', status.key_set ? 'API key set' : 'No API key set');
    keyState.append(node('span', '', ` · extractor: ${status.extractor || 'none'}`));
    const setupNote = node('p', 'llm-setup-note', 'Choose a provider and model for the copyable .env snippet. Update it locally, then restart Engraphis to apply the change.');
    const snippetLabel = node('label', 'llm-snippet-label', 'Local .env setup');
    const snippet = node('textarea', 'llm-env-snippet');
    snippet.id = 'llm-env-snippet';
    snippet.readOnly = true;
    snippet.rows = 5;
    snippet.value = llmSnippet(provider, model, Boolean(status.key_set));
    snippetLabel.htmlFor = snippet.id;
    snippetLabel.append(snippet);
    const copy = button('Copy', 'secondary-button', copyLlmSnippet);
    copy.classList.add('llm-copy-button');
    const snippetWrap = node('div', 'llm-snippet-wrap');
    snippetWrap.append(snippetLabel, copy);

    const extraction = node('div', 'llm-status-line');
    extraction.append(
      node('span', '', 'LLM extraction'),
      node('span', `llm-status-badge ${extractionEnabled ? 'ready' : 'muted'}`, extractionEnabled ? 'ON' : 'OFF'),
    );
    const extractionNote = node('p', 'llm-extraction-note', 'While ON, ingested memory content is sent to your configured provider for schema-validated extraction. OFF disables extraction transfers only; retention supervision is configured separately.');
    const retentionUsesLlm = text(status.retention_supervisor).toLowerCase() === 'llm';
    const retentionNote = node(
      'p',
      'llm-extraction-note',
      retentionUsesLlm
        ? 'Retention supervision is ON. New memories may send their title and a bounded excerpt to the configured provider.'
        : 'Retention supervision is OFF.',
    );
    const extractionActions = node('div', 'llm-actions');
    const turnOn = button('Turn on', 'primary-button', () => setLlmExtractor(true));
    turnOn.disabled = extractionEnabled || !configured;
    const turnOff = button('Turn off', 'secondary-button', () => setLlmExtractor(false));
    turnOff.disabled = !extractionEnabled;
    extractionActions.append(turnOn, turnOff);

    const testActions = node('div', 'llm-actions');
    testActions.append(button('Test connection', 'secondary-button', testLlm));
    const testResult = node('p', 'llm-test-result');
    testResult.id = 'llm-test-result';
    testResult.setAttribute('role', 'status');
    testResult.setAttribute('aria-live', 'polite');
    testActions.append(testResult);

    providerSelect.addEventListener('change', () => {
      const defaultModel = defaults[providerSelect.value];
      if (defaultModel && models.includes(defaultModel)) modelSelect.value = defaultModel;
      updateLlmSnippet(status);
    });
    modelSelect.addEventListener('change', () => updateLlmSnippet(status));
    target.append(overview, pickerGrid, keyState, setupNote, snippetWrap, extraction, extractionNote, retentionNote, extractionActions, testActions);
  }

  async function copyLlmSnippet() {
    const snippet = byId('llm-env-snippet');
    try {
      await navigator.clipboard.writeText(snippet.value);
      showNotice('Copied the local .env setup snippet.');
    } catch (_) {
      snippet.focus();
      snippet.select();
      if (document.execCommand('copy')) showNotice('Copied the local .env setup snippet.');
      else showNotice('Select the snippet and copy it manually.');
    }
  }

  async function loadSettings() {
    try {
      state.license = await api('/license');
      updatePlanBadge();
      renderSidebarCta();
    } catch (_) {}
    renderCloudAccountSettings();
    try {
      renderLlmSettings(await api('/llm/status'));
    } catch (error) {
      byId('llm-connection').replaceChildren(empty(`Model status unavailable: ${error.message}`));
    }
  }

  async function setLlmExtractor(enabled) {
    if (enabled && !window.confirm(`Turn on LLM extraction? ${EXTERNAL_LLM_PRIVACY_NOTICE}`)) return;
    setLlmTestResult(enabled ? 'Verifying the configured provider…' : 'Turning extraction off…');
    try {
      const result = await api('/llm/extractor', { method: 'POST', body: { enabled } });
      await loadSettings();
      const state = result.extractor_enabled ? 'LLM extraction is on for new ingested memories.' : 'LLM extraction is off for new ingested memories.';
      setLlmTestResult(`${state}${result.persisted === false ? ' The restart setting could not be saved.' : ''}`, result.extractor_enabled ? 'ready' : 'muted');
    } catch (error) {
      setLlmTestResult(`Could not change extraction: ${error.message}`, 'error');
    }
  }

  async function testLlm() {
    setLlmTestResult('Testing the configured model…');
    try {
      const result = await api('/llm/test', { method: 'POST' });
      await loadSettings();
      if (result.ok) {
        const suffix = result.auto_enabled ? ' Extraction is active for new ingested memories.' : '';
        setLlmTestResult(`Connected — ${result.provider}/${result.model}.${suffix}`, 'ready');
      } else {
        setLlmTestResult(`Could not connect: ${result.error || 'Check the provider, model, API key, and network.'}`, 'error');
      }
    } catch (error) {
      setLlmTestResult(`Model connection failed: ${error.message}`, 'error');
    }
  }

  function switchView(view, { pushHistory = true } = {}) {
    const validViews = ['today', 'ask', 'library', 'relations', 'provenance', 'manage'];
    if (!validViews.includes(view)) view = 'today';
    if (pushHistory && state.view !== view) {
      const url = new URL(location.href);
      url.searchParams.set('view', view);
      window.history.pushState({ view }, '', url);
    }
    state.view = view;
    all('[data-view-panel]').forEach(panel => panel.classList.toggle('active', panel.dataset.viewPanel === view));
    all('[data-view]').forEach(control => {
      const active = control.dataset.view === view;
      control.classList.toggle('active', active);
      if (active) control.setAttribute('aria-current', 'page');
      else control.removeAttribute('aria-current');
    });
    try {
      localStorage.setItem('engraphis-ledger-view', view);
    } catch (_) {}
    if (state.graphSpacetimeOverlay) {
      state.graphSpacetimeOverlay.setEnabled(view === 'relations' && graphIsGalaxy());
    }
    if (view === 'relations') loadGraph();
    if (view === 'provenance' && state.provenanceTab === 'audit') loadAudit();
    if (view === 'manage') {
      loadSavings(state.refreshEpoch);
      loadManageTab(state.manageTab);
    }
    window.scrollTo({ top: 0, behavior: 'instant' });
    const heading = byId(`${view}-title`);
    if (heading) {
      heading.setAttribute('tabindex', '-1');
      heading.focus({ preventScroll: true });
    }
  }

  function applyTheme(theme) {
    const valid = ['slate', 'midnight', 'paper', 'matrix'];
    const selected = valid.includes(theme) ? theme : 'slate';
    document.body.dataset.theme = selected;
    byId('theme-select').value = selected;
    byId('sidebar-theme-select').value = selected;
    try {
      localStorage.setItem('engraphis-ledger-theme', selected);
      localStorage.setItem('engraphis-theme', ({ slate: 'dark', paper: 'light', midnight: 'midnight', matrix: 'matrix' })[selected]);
    } catch (_) {}
    if (state.graphEngine) state.graphEngine.setThemeColors(graphThemeColors());
  }

  async function refreshBootstrap(preferred = '') {
    const bootstrap = (await api('/bootstrap')) || {};
    renderUpdateBanner(bootstrap.update);
    if (typeof bootstrap.version === 'string' && bootstrap.version.trim()) {
      state.releaseVersion = bootstrap.version.trim();
    }
    state.workspaces = bootstrap.workspaces || [];
    state.license = bootstrap.license || state.license;
    updatePlanBadge();
    renderSidebarCta();
    const select = byId('workspace-select');
    select.replaceChildren();
    state.workspaces.forEach(item => {
      const name = workspaceName(item);
      select.append(option(name, name));
    });
    if (!state.workspaces.length) {
      select.append(option('', 'No workspace'));
      select.disabled = true;
      setConnection('Local engine connected · no workspace');
      state.workspace = '';
      renderWorkspaceNames();
      renderWorkspaceList();
      renderMetricValues({ memories: 0, total_rows: 0, workspaces: 0, sessions: 0 });
      byId('decision-list').replaceChildren(empty('Create a workspace in Manage to start reviewing memory.'));
      const emptyActivity = node('tr');
      const emptyActivityCell = node('td', '', 'No workspace selected yet.');
      emptyActivityCell.colSpan = 5;
      emptyActivity.append(emptyActivityCell);
      byId('activity-body').replaceChildren(emptyActivity);
      byId('proactive-list').replaceChildren(empty('Create a workspace to see proactive context.'));
      byId('context-savings-persistent-value').textContent = '—';
      byId('context-savings-persistent-meta').textContent = 'Create a workspace to start tracking context savings.';
      byId('context-savings-persistent-rate').textContent = '—';
      return;
    }
    select.disabled = false;
    let saved = preferred;
    try {
      saved = preferred || localStorage.getItem('engraphis-workspace') || '';
    } catch (_) {}
    const names = state.workspaces.map(workspaceName);
    const selected = names.includes(saved)
      ? saved
      : workspaceName([...state.workspaces].sort((a, b) => number(b.memories) - number(a.memories))[0]);
    await selectWorkspace(selected);
    setConnection('Local engine connected');
  }

  async function boot() {
    byId('today-date').textContent = new Intl.DateTimeFormat(undefined, { dateStyle: 'long' }).format(new Date());
    let theme = 'slate';
    try {
      theme = localStorage.getItem('engraphis-ledger-theme') || theme;
    } catch (_) {}
    applyTheme(theme);
    try {
      await refreshBootstrap();
      let view = 'today';
      try {
        const saved = localStorage.getItem('engraphis-ledger-view');
        if (['today', 'ask', 'library', 'relations', 'provenance', 'manage'].includes(saved)) view = saved;
      } catch (_) {}
      const urlView = new URL(location.href).searchParams.get('view');
      switchView(['today', 'ask', 'library', 'relations', 'provenance', 'manage'].includes(urlView) ? urlView : view, { pushHistory: false });
    } catch (error) {
      if (error.status === 401 && await authenticateBrowser()) {
        location.reload();
        return;
      }
      setConnection('Local engine unavailable', false);
      showNotice(`Ledger could not connect: ${error.message}`);
    }
  }

  all('[data-view]').forEach(control => control.addEventListener('click', () => switchView(control.dataset.view)));
  all('[data-go]').forEach(control => control.addEventListener('click', () => switchView(control.dataset.go)));
  all('[data-manage]').forEach(control => control.addEventListener('click', () => {
    switchView('manage');
    switchManageTab(control.dataset.manage);
  }));
  const planBadge = byId('plan-badge');
  if (planBadge) {
    planBadge.addEventListener('click', event => {
      if (event.currentTarget.dataset.opensAccount === 'true') return;
      event.preventDefault();
      switchView('manage');
      switchManageTab('plans');
    });
  }
  all('[data-provenance]').forEach(control => control.addEventListener('click', () => {
    switchView('provenance');
    switchProvenanceTab(control.dataset.provenance);
  }));
  all('[data-provenance-tab]').forEach(control => control.addEventListener('click', () => switchProvenanceTab(control.dataset.provenanceTab)));
  all('[data-manage-tab]').forEach(control => control.addEventListener('click', () => switchManageTab(control.dataset.manageTab)));
  function wireTabKeyboard(selector, dataKey, activate) {
    const controls = all(selector);
    controls.forEach((control, index) => {
      control.tabIndex = control.getAttribute('aria-selected') === 'true' ? 0 : (index ? -1 : 0);
      control.addEventListener('keydown', event => {
        const direction = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1
          : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 0;
        let nextIndex = index;
        if (event.key === 'Home') nextIndex = 0;
        else if (event.key === 'End') nextIndex = controls.length - 1;
        else if (direction) nextIndex = (index + direction + controls.length) % controls.length;
        else return;
        event.preventDefault();
        const next = controls[nextIndex];
        next.focus();
        activate(next.dataset[dataKey]);
      });
    });
  }
  wireTabKeyboard('[data-graph-tab]', 'graphTab', setGraphTab);
  wireTabKeyboard('[data-provenance-tab]', 'provenanceTab', switchProvenanceTab);
  wireTabKeyboard('[data-manage-tab]', 'manageTab', switchManageTab);
  window.addEventListener('popstate', event => {
    const view = event.state && event.state.view
      ? event.state.view
      : new URL(location.href).searchParams.get('view') || 'today';
    switchView(view, { pushHistory: false });
  });

  byId('workspace-select').addEventListener('change', event => selectWorkspace(event.target.value));
  byId('ask-form').addEventListener('submit', askMemory);
  byId('library-filter').addEventListener('input', renderLibrary);
  byId('library-type').addEventListener('change', renderLibrary);
  byId('new-memory-button').addEventListener('click', () => openEditor());
  byId('editor-close').addEventListener('click', closeEditor);
  byId('editor-cancel').addEventListener('click', closeEditor);
  byId('memory-editor').addEventListener('submit', saveMemory);
  byId('import-button').addEventListener('click', () => byId('import-files').click());
  byId('import-files').addEventListener('change', event => importFiles(event.target.files));
  byId('obsidian-import-button').addEventListener('click', openObsidianImport);
  byId('obsidian-import-close').addEventListener('click', () => byId('obsidian-import-dialog').close());
  byId('obsidian-preview').addEventListener('click', previewObsidianImport);
  byId('obsidian-cancel').addEventListener('click', cancelObsidianImport);
  byId('obsidian-import-form').addEventListener('submit', runObsidianImport);
  byId('obsidian-source-mode').addEventListener('change', updateDocumentImportMode);
  byId('obsidian-vault-id').addEventListener('change', applySelectedDocumentSource);
  byId('obsidian-import-files').addEventListener('change', () => invalidateDocumentImportPreview());
  byId('obsidian-import-folder').addEventListener('change', () => {
    prefillNewSourceLabelFromFolder();
    invalidateDocumentImportPreview();
  });
  [
    ['obsidian-workspace', 'input'],
    ['obsidian-repo', 'input'],
    ['obsidian-session', 'input'],
    ['obsidian-scope', 'change'],
    ['obsidian-memory-type', 'change'],
    ['obsidian-vault-label', 'input'],
    ['obsidian-conflict', 'change'],
  ].forEach(([id, eventName]) => {
    byId(id).addEventListener(eventName, () => invalidateDocumentImportPreview());
  });
  byId('obsidian-report-filter').addEventListener('change', () => renderObsidianReport(obsidianImport.job || obsidianImport.preview));

  all('[data-graph-tab]').forEach(control => control.addEventListener('click', () => setGraphTab(control.dataset.graphTab)));
  byId('graph-fit').addEventListener('click', () => state.graphEngine && state.graphEngine.fit());
  byId('graph-reheat').addEventListener('click', () => state.graphEngine && state.graphEngine.reheat());
  byId('graph-clear-focus').addEventListener('click', () => {
    if (state.graphEngine) state.graphEngine.clearFocus();
  });
  byId('graph-freeze').addEventListener('click', () => {
    state.graphFrozen = !state.graphFrozen;
    setGraphSwitch('graph-freeze', state.graphFrozen);
    if (state.graphEngine) state.graphEngine.freeze(state.graphFrozen);
    saveGraphPreferences();
  });
  byId('graph-flow').addEventListener('click', event => {
    const on = event.currentTarget.getAttribute('aria-checked') !== 'true';
    setGraphSwitch('graph-flow', on);
    if (state.graphEngine) state.graphEngine.setSettings({ flow: on });
    clearGraphSavedView();
    saveGraphPreferences();
  });
  byId('graph-labels').addEventListener('click', event => {
    const on = event.currentTarget.getAttribute('aria-checked') !== 'true';
    setGraphSwitch('graph-labels', on);
    if (state.graphEngine) state.graphEngine.setSettings({ labels: on });
    clearGraphSavedView();
    saveGraphPreferences();
  });
  byId('graph-flow-speed').addEventListener('input', event => {
    const speed = graphValueInRange('graph-flow-speed', event.target.value, 45);
    byId('graph-flow-speed').value = String(speed);
    byId('graph-flow-speed-output').value = String(Math.round(speed));
    byId('graph-flow-speed-output').textContent = String(Math.round(speed));
    if (state.graphEngine) state.graphEngine.setSettings({ flowSpeed: speed });
    clearGraphSavedView();
    saveGraphPreferences();
  });
  byId('graph-search').addEventListener('input', event => searchGraph(event.target.value));
  byId('graph-repo-filter').addEventListener('input', event => {
    if (state.graphEngine) state.graphEngine.setRepoFilter(event.target.value);
    clearGraphSavedView();
    saveGraphPreferences();
    // Code overlay payloads are repository-scoped server-side. Changing the
    // filter while code overlay is active must trigger a fresh request or
    // the rendered graph keeps the previous repository's relations.
    if (state.graphIncludeCode) loadGraph({ force: true });
  });
  all('[data-graph-preset-choice]').forEach(control => control.addEventListener('click', () => {
    const preset = control.dataset.graphPresetChoice;
    const resumeLayout = state.graphFrozen;
    byId('graph-preset').value = preset;
    if (state.graphEngine && resumeLayout) {
      // Freeze is the safe default for arranging nodes by hand. Selecting a named layout is an
      // explicit request to run physics, so make that transition visible and leave the switch
      // truthful; the person can freeze the settled arrangement again when they are happy.
      state.graphFrozen = false;
      setGraphSwitch('graph-freeze', false);
      state.graphEngine.freeze(false);
    }
    let settings = graphPresetTuning(preset);
    if (state.graphEngine) settings = state.graphEngine.setPreset(preset);
    syncGraphTuning(settings);
    updateGraphModeControls();
    if (state.graphEngine) state.graphEngine.setSizeBy(graphSizeBy());
    if (state.graphSpacetimeOverlay) state.graphSpacetimeOverlay.setEnabled(graphIsGalaxy());
    clearGraphSavedView();
    syncGraphChoices();
    saveGraphPreferences();
    if (resumeLayout) showNotice('Layout applied. Simulation resumed — freeze it to lock node positions.');
  }));
  all('[data-graph-style-choice]').forEach(control => control.addEventListener('click', () => {
    byId('graph-style').value = control.dataset.graphStyleChoice;
    if (state.graphEngine) state.graphEngine.setStyle(control.dataset.graphStyleChoice);
    clearGraphSavedView();
    syncGraphChoices();
    saveGraphPreferences();
  }));
  all('[data-graph-color-choice]').forEach(control => control.addEventListener('click', () => {
    byId('graph-color').value = control.dataset.graphColorChoice;
    if (state.graphEngine) state.graphEngine.setColorBy(control.dataset.graphColorChoice);
    clearGraphSavedView();
    syncGraphChoices();
    saveGraphPreferences();
  }));
  all('[data-graph-palette-choice]').forEach(control => control.addEventListener('click', () => {
    const palette = control.dataset.graphPaletteChoice;
    byId('graph-palette').value = palette;
    applyGraphPalette(palette);
    clearGraphSavedView();
    syncGraphChoices();
    saveGraphPreferences();
    showNotice(`${control.textContent.trim()} palette applied to the graph.`);
  }));
  byId('graph-min-degree').addEventListener('input', event => {
    setGraphMinDegree(event.target.value);
    clearGraphSavedView();
    saveGraphPreferences();
  });
  byId('graph-show-unlinked').addEventListener('click', event => {
    setGraphShowUnlinked(event.currentTarget.getAttribute('aria-pressed') !== 'true');
    clearGraphSavedView();
    saveGraphPreferences();
    loadGraph({ force: true });
  });
  byId('graph-tune-min-degree').addEventListener('input', event => {
    setGraphMinDegree(event.target.value);
    clearGraphSavedView();
    saveGraphPreferences();
  });
  byId('graph-depth').addEventListener('input', event => {
    setGraphDepth(event.target.value);
    clearGraphSavedView();
    saveGraphPreferences();
  });
  GRAPH_TUNING.forEach(item => byId(item.id).addEventListener('input', event => {
    const value = setGraphTuningControl(item, event.target.value);
    if (state.graphEngine) state.graphEngine.setSettings({ [item.key]: value });
    clearGraphSavedView();
    saveGraphPreferences();
  }));
  GRAPH_SPACETIME_TUNING.forEach(item => byId(item.id).addEventListener('input', event => {
    setGraphSpacetimeControl(item, event.target.value);
    /* Controls use human-scale values (G=100, mass=160, spring=32), while the engine API is
       normalized around 1. Apply the same conversion used during graph creation on every live
       input event; passing the raw slider value would immediately clamp G to 8 and mass to 16. */
    if (state.graphEngine) {
      const settings = graphSpacetimeSettings();
      state.graphEngine.setSettings({ [item.key]: settings[item.key] });
    }
    clearGraphSavedView();
    saveGraphPreferences();
  }));
  byId('graph-orbits-pause').addEventListener('click', event => {
    state.graphOrbitPaused = event.currentTarget.getAttribute('aria-checked') !== 'true';
    setGraphSwitch('graph-orbits-pause', state.graphOrbitPaused);
    if (state.graphEngine) state.graphEngine.setSettings({ orbitPaused: state.graphOrbitPaused });
    clearGraphSavedView();
    saveGraphPreferences();
  });
  all('[data-graph-layer]').forEach(control => control.addEventListener('click', () => {
    const layers = graphLayerState();
    const layer = control.dataset.graphLayer;
    layers[layer] = !layers[layer];
    const previousIncludeCode = state.graphIncludeCode;
    state.graphIncludeCode = layers.code === true;
    setGraphLayers(layers);
    if (state.graphEngine) state.graphEngine.setLayers(layers);
    clearGraphSavedView();
    saveGraphPreferences();
    if (previousIncludeCode !== state.graphIncludeCode) loadGraph({ force: true });
  }));
  all('[data-graph-saved-view]').forEach(control => control.addEventListener('click', () => applyGraphView(control.dataset.graphSavedView)));
  byId('graph-save-view').addEventListener('click', saveCurrentGraphView);
  byId('graph-reset-tuning').addEventListener('click', resetGraphTuning);
  byId('graph-retry').addEventListener('click', retryGraphLoad);
  byId('graph-bridges').addEventListener('change', event => {
    if (state.graphEngine) state.graphEngine.setBridges(event.target.checked);
    saveGraphPreferences();
  });
  byId('graph-collapse').addEventListener('change', event => {
    if (state.graphEngine) state.graphEngine.setCollapse(event.target.checked ? 'auto' : false);
    saveGraphPreferences();
  });
  byId('graph-as-of').addEventListener('change', event => {
    if (state.graphEngine) state.graphEngine.setAsOf(graphAsOfTimestamp());
    saveGraphPreferences();
    loadGraph({ force: true });
  });
  byId('graph-ghosts').addEventListener('change', event => {
    if (state.graphEngine) state.graphEngine.setGhosts(event.target.checked);
    saveGraphPreferences();
  });
  byId('graph-size').addEventListener('change', event => {
    if (state.graphEngine) state.graphEngine.setSizeBy(graphSizeBy());
    saveGraphPreferences();
  });
  byId('graph-export').addEventListener('click', () => {
    const menu = byId('graph-export-menu');
    const open = menu.hidden;
    menu.hidden = !open;
    byId('graph-export').setAttribute('aria-expanded', String(open));
  });
  byId('graph-export-png').addEventListener('click', () => {
    byId('graph-export-menu').hidden = true;
    byId('graph-export').setAttribute('aria-expanded', 'false');
    exportGraphPng();
  });
  byId('graph-export-json').addEventListener('click', () => {
    byId('graph-export-menu').hidden = true;
    byId('graph-export').setAttribute('aria-expanded', 'false');
    exportGraphJson();
  });
  byId('graph-connections-close').addEventListener('click', closeGraphConnections);
  byId('graph-connections-dialog').addEventListener('click', event => {
    if (event.target === event.currentTarget) closeGraphConnections();
  });
  restoreGraphPreferences();
  syncGraphChoices();

  byId('why-form').addEventListener('submit', whySearch);
  byId('timeline-form').addEventListener('submit', event => timelineSearch(event, false));
  byId('supersession-form').addEventListener('submit', event => timelineSearch(event, true));
  byId('verify-receipts').addEventListener('click', verifyReceipts);
  byId('export-receipts').addEventListener('click', exportReceipts);

  byId('create-workspace-toggle').addEventListener('click', () => {
    byId('create-workspace-form').hidden = !byId('create-workspace-form').hidden;
    if (!byId('create-workspace-form').hidden) byId('new-workspace-name').focus();
  });
  byId('create-workspace-form').addEventListener('submit', createWorkspace);
  byId('consolidate-form').addEventListener('submit', previewConsolidation);
  byId('consolidate-commit').addEventListener('click', commitConsolidation);
  ['consolidate-structured'].forEach(id => {
    byId(id).addEventListener('change', invalidateConsolidationReview);
  });
  byId('billing-select').addEventListener('change', renderPlans);
  byId('dashboard-select').addEventListener('change', event => {
    location.assign(event.target.value === 'classic' ? '/classic' : '/');
  });
  byId('theme-select').addEventListener('change', event => applyTheme(event.target.value));
  byId('sidebar-theme-select').addEventListener('change', event => applyTheme(event.target.value));
  boot();
})();
