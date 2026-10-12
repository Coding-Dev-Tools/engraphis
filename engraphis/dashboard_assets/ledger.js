(() => {
  'use strict';

  const apiRoot = `${location.origin}/api`;
  const state = {
    workspace: '',
    project: '',
    workspaces: [],
    stats: {},
    memories: [],
    libraryTotal: 0,
    libraryNextCursor: null,
    libraryCursors: [null],
    libraryPage: 0,
    libraryLoading: false,
    librarySelecting: false,
    moveMemoryIds: new Set(),
    memoryMove: null,
    selectedMemory: '',
    editorMemory: null,
    editorSession: null,
    memoryHistory: null,
    editorReturnFocus: null,
    view: 'today',
    provenanceTab: 'belief',
    savingsPreset: 'all',
    manageTab: 'workspaces',
    refreshEpoch: 0,
    graphWorkspace: '',
    graphData: null,
    graphDataMode: 'overview',
    graphGalaxyQuality: false,
    graphDataPreset: 'galaxy',
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
    graphLoadRepo: '',
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
    graphSavedView: '',
    consolidationReview: null,
    reviewCsrf: '',
    hostedLoaded: new Set(),
    analyticsStarting: new Set(),
    analyticsJobs: new Map(),
    analyticsResults: new Map(),
    analyticsStartErrors: new Map(),
    scopedRequests: Object.create(null),
    scopedControllers: Object.create(null),
    syncStatus: null,
    license: null,
    releaseVersion: '',
    embedder: null,
    booted: false,
    connectionLost: false,
    bootFailure: '',
    editorBaseline: '',
    libraryDragId: '',
    graphTypeColors: {},
  };

  const byId = id => document.getElementById(id);
  const runtimeApi = byId('ledger-runtime-api');
  if (runtimeApi) runtimeApi.textContent = location.host;
  const memoryTitle = window.EngraphisWorkflow.title;
  const dialogs = window.EngraphisDialog;
  const markdown = window.EngraphisMarkdown;
  const all = selector => [...document.querySelectorAll(selector)];
  const text = value => value == null ? '' : String(value);
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const NOTICE_DURATION_MS = 3000;
  let noticeTimer = null;
  let graphRepoLoadTimer = null;
  let librarySearchTimer = null;
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
  const memoryMutationButton = (label, className, action) => {
    const control = button(label, className, async () => {
      if (control.disabled) return;
      control.disabled = true;
      try { await action(); } finally { control.disabled = false; }
    });
    return control;
  };
  const option = (value, label, selected = false) => {
    const item = node('option', '', label);
    item.value = value;
    item.selected = selected;
    return item;
  };
  const query = (name = state.workspace) => `workspace=${encodeURIComponent(name || '')}`;
  const memoryQuery = (workspace = state.workspace, project = state.project) => query(workspace)
    + (project ? '&repo=' + encodeURIComponent(project) : '');
  const beginScopedRequest = kind => {
    if (state.scopedControllers[kind]) state.scopedControllers[kind].abort();
    const controller = new AbortController();
    state.scopedControllers[kind] = controller;
    const generation = number(state.scopedRequests[kind]) + 1;
    state.scopedRequests[kind] = generation;
    return {
      kind,
      generation,
      workspace: state.workspace,
      project: state.project,
      epoch: state.refreshEpoch,
      signal: controller.signal,
    };
  };
  const isCurrentScopedRequest = request => Boolean(request
    && request.workspace === state.workspace
    && request.project === state.project
    && request.epoch === state.refreshEpoch
    && state.scopedRequests[request.kind] === request.generation);
  const invalidateScopedRequests = () => {
    Object.values(state.scopedControllers).forEach(controller => controller.abort());
    state.scopedControllers = Object.create(null);
    Object.keys(state.scopedRequests).forEach(kind => {
      state.scopedRequests[kind] = number(state.scopedRequests[kind]) + 1;
    });
  };
  const GRAPH_INITIAL_NODE_LIMIT = 1500;
  const GRAPH_INITIAL_EDGE_LIMIT = 3000;
  const GRAPH_ALL_NODE_LIMIT = 20_000;
  const GRAPH_ALL_EDGE_LIMIT = 200_000;
  const GRAPH_LOAD_TIMEOUT_MS = 60_000;
  // Cold complete scenes can legitimately spend ~25s in the bounded server projection
  // before the renderer receives its payload. Keep enough headroom for the response body,
  // graph assets, and renderer readiness instead of turning a healthy cold request into a
  // generic client timeout.
  const GRAPH_FULL_LOAD_TIMEOUT_MS = 90_000;
  const GRAPH_CONNECTION_MEMORIES_TIMEOUT_MS = 8_000;
  const GRAPH_PREFERENCES_KEY = 'engraphis-ledger-graph-preferences-v1';
  const GRAPH_PHYSICS_VERSION = 6;
  const GRAPH_CUSTOM_VIEW_KEY = 'engraphis-ledger-graph-custom-view-v1';
  const GRAPH_LAYERS = ['temporal', 'entity', 'causal', 'semantic', 'code'];
  const GRAPH_DEFAULT_LAYERS = { temporal: true, entity: true, causal: true, semantic: true, code: false };
  const GRAPH_TUNING = [
    { id: 'graph-repel', key: 'repel', fallback: 100 },
    { id: 'graph-link', key: 'link', fallback: 8 },
    { id: 'graph-gravity', key: 'gravity', fallback: 72 },
    { id: 'graph-node-size', key: 'size', fallback: 3 },
    { id: 'graph-text-size', key: 'font', fallback: 12 },
    { id: 'graph-line-width', key: 'linkw', fallback: 0.72, precision: 2 },
    { id: 'graph-label-density', key: 'labelDensity', fallback: 24 },
  ];
  const GRAPH_SPACETIME_TUNING = [
    { id: 'graph-gravitational-constant', key: 'gravitationalConstant', fallback: 60 },
    { id: 'graph-black-hole-mass', key: 'blackHoleMass', fallback: 96 },
    { id: 'graph-local-gravitational-constant', key: 'localGravitationalConstant', fallback: 60 },
    { id: 'graph-space-damping', key: 'damping', fallback: 1, precision: 1 },
    { id: 'graph-spring-stiffness', key: 'springStiffness', fallback: 32 },
  ];
  const GRAPH_PRESET_TUNING = {
    original: { repel: 120, link: 30, gravity: 14, font: 13, size: 3, linkw: 1, labelDensity: 40 },
    compact: { repel: 42, link: 20, gravity: 26, font: 12, size: 3, linkw: 0.7, labelDensity: 30 },
    communities: { repel: 48, link: 16, gravity: 48, font: 12, size: 3, linkw: 0.72, labelDensity: 24 },
    galaxy: { repel: 100, link: 8, gravity: 72, font: 12, size: 3, linkw: 0.72, labelDensity: 24 },
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
    every: 'Every node',
  };
  const GRAPH_STYLE_NOTES = {
    cyber: 'Iridescent PVD over graphite — cyan, violet, and magenta across each node.',
    galaxy: 'Deep anodized alloy with a cool blue-violet directional sheen.',
    solar: 'Brushed copper faces with amber bezels and warm radial grain.',
    classic: 'Neutral satin gunmetal with a restrained cool steel edge.',
  };
  const GRAPH_LOD_STYLE_NOTES = {
    cyber: 'High-contrast cyan, violet and magenta points tuned for dense LOD views.',
    galaxy: 'Cool blue-violet points separate clusters clearly across wide zoom ranges.',
    solar: 'Warm copper and amber points keep dense relation fields legible.',
    classic: 'Restrained steel points prioritize structure and long-session readability.',
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
  /* Server timestamps are seconds (sometimes milliseconds or ISO text). Any finite number
     passes validation and memory metadata is untrusted, so a value that is not a real date
     reads as unknown instead of throwing from Date/Intl formatting and breaking a panel. */
  /* Server timestamps are epoch seconds or milliseconds, or ISO strings. Anything else
     (zero, blank text, booleans, arrays) is "not recorded" rather than the 1970 epoch. */
  const validDate = value => {
    if (value == null || typeof value === 'boolean') return null;
    if (typeof value === 'object' && !(value instanceof Date)) return null;
    if (typeof value === 'string' && !value.trim()) return null;
    const numeric = Number(value);
    if (numeric === 0) return null;
    const date = Number.isFinite(numeric)
      ? new Date(numeric < 1e12 ? numeric * 1000 : numeric)
      : new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  };
  const relative = value => {
    const date = validDate(value);
    if (!date) return 'stored locally';
    const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(date);
  };
  const errorMessage = (payload, status) => {
    const detail = payload && (payload.detail || payload.error);
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail.error === 'string') return detail.error;
    return `Request failed (${status})`;
  };

  async function api(path, options = {}) {
    const { timeoutMs = 30_000, signal, ...requestOptions } = options;
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal) {
      if (signal.aborted) abort();
      else signal.addEventListener('abort', abort, { once: true });
    }
    let timedOut = false;
    const timer = window.setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    const init = { ...requestOptions, signal: controller.signal, headers: { ...(options.headers || {}) } };
    init.headers['X-Engraphis-Browser-Session'] = '1';
    if (init.body && !(init.body instanceof FormData) && typeof init.body !== 'string') {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(init.body);
    }
    try {
      const response = await fetch(`${apiRoot}${path}`, init);
      const payload = await response.json().catch(error => {
        if (controller.signal.aborted) throw error;
        return null;
      });
      if (!response.ok) {
        const error = new Error(errorMessage(payload, response.status));
        error.status = response.status;
        error.code = payload && ((payload.detail && payload.detail.code) || payload.code);
        throw error;
      }
      return payload;
    } catch (error) {
      if (timedOut) throw new Error('The request timed out. Please try again.');
      throw error;
    } finally {
      window.clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', abort);
    }
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
    const request = beginScopedRequest('memory-mutation');
    const editorSession = state.editorSession;
    const provenance = memory.provenance || {};
    const reviewState = provenance.review_state || 'pending';
    const review = await dialogs.open({
      title: 'Approve for prompt context',
      message: `Approving “${memoryTitle(memory)}” creates a fresh, audited approved memory that agents may include in model context. The reviewed ${reviewState} source remains preserved.`,
      fields: [{
        name: 'reason', type: 'textarea', rows: 3, required: true, maxLength: 500,
        label: `Why is this ${reviewState} record safe to include in model context?`,
      }],
      confirmLabel: 'Approve for prompt',
    });
    if (review === null) return;
    if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
      || state.selectedMemory !== memory.id) return;
    const reason = review.reason;
    if (!reason) {
      showNotice('A non-empty review reason is required.');
      return;
    }
    try {
      const csrf = await reviewCsrfToken();
      if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
        || state.selectedMemory !== memory.id) return;
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
      if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
        || state.selectedMemory !== memory.id) return;
      showNotice('Approved successor created. The reviewed source remains in the audit trail.');
      await selectWorkspace(request.workspace);
      if (payload.id && state.workspace === request.workspace
        && state.refreshEpoch === request.epoch + 1
        && !state.editorSession && !state.selectedMemory) await selectMemory(payload.id);
    } catch (error) {
      if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
        || state.selectedMemory !== memory.id) return;
      showNotice(`Could not approve this memory: ${error.message}`);
    }
  }

  let graphAssetsPromise = null;
  let graphAssetsController = null;
  let graphAllAssetsPromise = null;
  let graphAllAssetsController = null;
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
      /* Graph assets download in parallel and each runs as soon as it arrives: none needs
         another at load time, and callers wait for every global before creating a renderer.
         Ordered execution (async = false) would let one stalled download hold back every
         later attempt, even after Reload removed its tag. */
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

  function ensureGraphAllAsset() {
    if (window.EngraphisEveryGraph) return Promise.resolve();
    if (!graphAllAssetsPromise) {
      const controller = new AbortController();
      const attempt = loadScript(
        graphAssetSource('/v2-assets/engraphis-graph-every.js?v=20261012-stacked-ledger-1'),
        'EngraphisEveryGraph', controller.signal,
      );
      graphAllAssetsPromise = attempt;
      graphAllAssetsController = controller;
      attempt.catch(() => {
        if (graphAllAssetsPromise === attempt) releaseGraphAllAssetsAttempt(attempt);
      });
    }
    return graphAllAssetsPromise;
  }

  function ensureGraphAssets(loadAll = false) {
    /* The complete profile is an independent worker/WebGL renderer. Galaxy is the exception:
       its solar-system view needs the authoritative hierarchical orbit integrator, so a full
       Galaxy request uses the quality engine with the complete payload instead of the static
       all-node worker. The factory decision is data-sensitive, not toolbar-sensitive: the
       Every-node chip changes the preset to "every" before the scene arrives, and a fast click
       can race the overview load. Keep both candidates available so an authored star/planet
       scene cannot select an engine whose asset is still in flight. */
    if (loadAll) {
      return Promise.all([ensureGraphAllAsset(), ensureGraphAssets(false)]);
    }
    /* Every global counts, d3 included: the engine only degrades without it, so a d3 load that
       failed while the others succeeded must still be retried by the next Reload. */
    const coreReady = window.d3 && window.ForceGraph && window.EngraphisGraph
      && window.EngraphisSpacetime;
    if (!coreReady && !graphAssetsPromise) {
      const controller = new AbortController();
      const attempt = Promise.all([
        loadScript(graphAssetSource('/v2-assets/vendor/d3.min.js?v=20260727-final'), 'd3', controller.signal),
        loadScript(graphAssetSource('/v2-assets/vendor/force-graph.min.js?v=20260727-final'), 'ForceGraph', controller.signal),
        loadScript(graphAssetSource('/v2-assets/engraphis-graph.js?v=20261009-ledger-only-1'), 'EngraphisGraph', controller.signal),
        loadScript(graphAssetSource('/v2-assets/engraphis-spacetime.js?v=20260927-unmerged-readiness-3'), 'EngraphisSpacetime', controller.signal),
      ]);
      graphAssetsPromise = attempt;
      graphAssetsController = controller;
      attempt.catch(() => {
        /* A fetched script can load successfully while failing to execute (for example, a
           stale cached parse error). Retire that URL immediately so the next explicit Reload
           advances the retry query instead of replaying the same broken response forever. */
        if (graphAssetsPromise === attempt) releaseGraphAssetsAttempt(attempt);
      });
    }
    const core = coreReady ? Promise.resolve() : graphAssetsPromise;
    return core;
  }

  function releaseGraphAssetsAttempt(attempt) {
    // A browser can leave a script fetch pending indefinitely. Do not let that stale promise
    // become a permanent single-flight lock: remove its fetches and give the next explicit
    // reload a unique URL so it cannot join the browser's already-stalled request.
    if (!attempt || graphAssetsPromise !== attempt) return;
    graphAssetsPromise = null;
    const controller = graphAssetsController;
    graphAssetsController = null;
    graphAssetsRetry = Math.min(graphAssetsRetry + 1, 10);
    if (controller) controller.abort();
    all('script[data-engraphis-graph-asset="true"]').forEach(script => script.remove());
  }

  function releaseGraphAllAssetsAttempt(attempt) {
    if (!attempt || graphAllAssetsPromise !== attempt) return;
    graphAllAssetsPromise = null;
    const controller = graphAllAssetsController;
    graphAllAssetsController = null;
    graphAssetsRetry = Math.min(graphAssetsRetry + 1, 10);
    if (controller) controller.abort();
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

  function licenseTrialDays(plan = 'pro') {
    const trial = (state.license && state.license.trial) || {};
    const days = (trial.days_by_plan || {})[plan] ?? (plan === 'pro' ? trial.trial_days : null);
    return Number.isSafeInteger(days) && days > 0 ? days : null;
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
    const days = licenseTrialDays(plan);
    return {
      label: trial ? `Start ${days ? `${days}-day ` : ''}${name} trial` : `Subscribe to ${name}`,
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
        : trial ? `${hostedCta('pro', 'header').label} in Plans and billing`
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
    byId('home-engine-status').textContent = 'Dashboard: ' + message + '. Checked at ' + new Date().toLocaleTimeString() + '.';
    const dot = document.querySelector('.status-dot');
    if (dot) dot.classList.toggle('unhealthy', !healthy);
  }

  function connectionLabel() {
    const host = (location.hostname || '').toLowerCase();
    return ['localhost', '127.0.0.1', '::1', '[::1]'].includes(host) || host.endsWith('.localhost')
      ? 'Local engine' : 'Engraphis server';
  }

  /* A persistent banner, not a transient notice: when the server stops answering, every
     panel goes stale, so the page says so until a retry or health check succeeds. */
  function showConnectionProblem(detail) {
    state.connectionLost = true;
    setConnection(`${connectionLabel()} unavailable`, false);
    byId('connection-banner-detail').textContent = detail;
    byId('connection-banner').hidden = false;
  }

  function clearConnectionProblem() {
    if (!state.connectionLost) return;
    state.connectionLost = false;
    byId('connection-banner').hidden = true;
    setConnection(`${connectionLabel()} connected`);
  }

  function connectionProblemDetail(error) {
    if (error && error.status === 401) {
      return 'This Engraphis deployment needs its API token before the dashboard can load. Choose Retry now to enter it.';
    }
    if (error && error.status === 403) {
      // The server explains a refused address or remote access; show its reason.
      return `The dashboard server refused this page: ${error.message}`;
    }
    if (error && error.status === 404) {
      return 'Dashboard APIs are unavailable at this address. If the legacy v1 reference server is running on this port, stop it and start engraphis-dashboard.';
    }
    if (error && error.status >= 500) {
      return `The dashboard server answered with an error: ${error.message}. Run engraphis-init --check for diagnostics, then retry.`;
    }
    return 'This page cannot reach the dashboard server. Check that Engraphis is still running, then retry.';
  }

  let healthTimer = null;
  let healthCheckRunning = false;
  async function checkHealth() {
    if (document.hidden || healthCheckRunning) return;
    healthCheckRunning = true;
    try {
      await api('/health', { timeoutMs: 10_000 });
      if (!state.booted) {
        if (state.bootFailure !== 'auth') await boot();
      } else if (state.connectionLost) {
        clearConnectionProblem();
        showNotice('Connection to Engraphis restored.');
      }
    } catch (error) {
      /* Any 4xx means the server answered; authentication is handled by boot. */
      if (error.status && error.status < 500) clearConnectionProblem();
      else showConnectionProblem(connectionProblemDetail(error));
    } finally {
      healthCheckRunning = false;
    }
  }

  function startHealthChecks() {
    if (healthTimer !== null) return;
    healthTimer = window.setInterval(() => { void checkHealth(); }, 30_000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.connectionLost) void checkHealth();
    });
  }

  async function retryConnection() {
    const control = byId('connection-retry');
    control.disabled = true;
    control.textContent = 'Retrying…';
    try {
      if (!state.booted) await boot();
      else {
        /* The page still holds the open workspace, graph and any unsaved edit. Confirm the
           server answers; re-running bootstrap would reselect the workspace and drop them. */
        await api('/health', { timeoutMs: 10_000 });
        clearConnectionProblem();
        showNotice('Connection to Engraphis restored.');
      }
    } catch (error) {
      showConnectionProblem(connectionProblemDetail(error));
    } finally {
      control.disabled = false;
      control.textContent = 'Retry now';
    }
  }

  function embedderDescription(embedder) {
    if (!embedder) return 'Unavailable';
    const dim = number(embedder.dim);
    if (embedder.semantic === false) return `Hashing embedder · ${dim}-dim · word matching`;
    const model = text(embedder.model).split('/').pop() || text(embedder.class) || 'Embedding model';
    return `${model} · ${dim}-dim · semantic`;
  }

  /* Only warn when a model was configured but did not load. An empty model setting is a
     deliberate offline choice, and the runtime card still reports the hashing embedder. */
  function renderEmbedderBanner(embedder) {
    state.embedder = embedder || null;
    byId('runtime-embedder').textContent = embedderDescription(embedder);
    const banner = byId('embedder-banner');
    const model = embedder ? text(embedder.model).trim() : '';
    if (!embedder || embedder.semantic !== false || !model) {
      banner.hidden = true;
      return;
    }
    byId('embedder-banner-detail').textContent = `The configured model ${model} is not loaded, so Engraphis is using its dependency-free hashing embedder: Ask, Library search and Activity match shared words rather than meaning. Restart engraphis-dashboard with network access so it can download the model once, then choose Recheck.`;
    banner.hidden = false;
  }

  function renderRuntimeCard() {
    byId('runtime-version').textContent = state.releaseVersion || 'Unknown';
    byId('runtime-host').textContent = location.host || 'This browser';
    byId('runtime-embedder').textContent = embedderDescription(state.embedder);
  }

  async function recheckEmbedder() {
    const control = byId('embedder-recheck');
    control.disabled = true;
    try {
      /* Recheck the embedder only. A full bootstrap would reselect the workspace and discard
         an open graph or a memory being edited. */
      const bootstrap = (await api('/bootstrap')) || {};
      renderEmbedderBanner(bootstrap.embedder);
      renderRuntimeCard();
      showNotice(state.embedder && state.embedder.semantic === false
        ? 'Semantic search is still off. Restart engraphis-dashboard so it can load the model.'
        : 'Semantic search is on.');
    } catch (error) {
      showNotice(`Could not recheck the embedder: ${error.message}`);
    } finally {
      control.disabled = false;
    }
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

  const memoryOwnershipLabels = new WeakMap();

  function refreshMemoryOwnershipLabels() {
    all('[data-memory-ownership]').forEach(label => {
      const owner = memoryOwnershipLabels.get(label);
      if (owner) label.textContent = workflow.ownership(owner.memory, owner.workspace);
    });
  }

  function memoryMeta(memory) {
    const meta = node('div', 'memory-meta');
    const ownership = node('span', 'memory-ownership', workflow.ownership(memory, state.workspace));
    ownership.dataset.memoryOwnership = '';
    // Bind the authorized read context, never the selected project preference.
    memoryOwnershipLabels.set(ownership, { memory, workspace: state.workspace });
    meta.append(
      node('span', 'type-chip', memoryType(memory)),
      ownership,
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
    const packed = (payload && Array.isArray(payload.by_token_counter) ? payload.by_token_counter : [])
      .filter(row => number(row.receipt_count) > 0 || number(row.source_tokens) > 0);
    target.append(node('h3', '', 'Packed context accounting'));
    target.append(node('p', 'field-note', 'Retrieved source tokens compared with the context actually emitted. Packing savings are not added again to the estimate above.'));
    if (!packed.length) {
      target.append(node('p', 'field-note', 'No complete context-usage receipts yet.'));
    } else {
      const packedRows = node('div', 'savings-breakdown');
      packed.forEach(row => {
        const item = node('div', 'savings-breakdown-row');
        item.append(
          node('span', '', text(row.token_counter || 'unknown')),
          node('span', '', `${formatSavingsTokens(row.context_tokens)} packed / ${formatSavingsTokens(row.source_tokens)} source · ${formatSavingsTokens(row.saved_tokens)} saved`),
        );
        packedRows.append(item);
      });
      target.append(packedRows);
    }
    target.append(node('p', 'savings-note', `${excluded} excluded or unclassified deliver${excluded === 1 ? 'y' : 'ies'}. Measures estimated prompt-context reduction; it does not measure provider billing.`));
  }

  function renderReviewInbox(result) {
    const target = byId('decision-list');
    target.replaceChildren();
    const items = result.items;
    const partial = result.has_more || result.truncated;
    byId('review-status').textContent = `${items.length} review ${items.length === 1 ? 'item' : 'items'} shown.`
      + (partial ? ' This is a partial list; more records may need review.' : '');
    if (!items.length) {
      target.append(empty(partial ? 'No review items in this sample. More records may remain.'
        : 'No records currently need review in this context.'));
      return;
    }
    items.forEach((item, index) => {
      const card = node(item.id ? 'button' : 'article', 'decision-card memory-link-card');
      if (item.id) {
        card.type = 'button';
        card.dataset.memoryId = item.id;
        card.addEventListener('click', () => openMemory(item));
      }
      const states = [];
      if (item.quarantined) states.push('Quarantined');
      if (item.conflict_with) states.push('Conflicting evidence');
      if (item.review_state === 'pending') states.push('Source review pending');
      const header = node('div', 'decision-card-header');
      header.append(
        node('span', 'tag', states.join(' · ') || 'Review required'),
        node('h3', '', 'Inspect review item ' + (index + 1)),
      );
      card.append(header, node('p', '', item.excerpt || 'Open the record to review its source and history.'));
      target.append(card);
    });
  }

  async function loadReviewInbox() {
    if (!state.workspace) return;
    const request = beginScopedRequest('reviews');
    const target = byId('decision-list');
    target.replaceChildren(empty('Loading review state…'));
    target.setAttribute('aria-busy', 'true');
    byId('review-status').textContent = '';
    byId('review-refresh').disabled = true;
    try {
      const result = await api('/review-inbox?' + memoryQuery(request.workspace, request.project) + '&limit=6', { signal: request.signal });
      if (!isCurrentScopedRequest(request)) return;
      if (!result || !Array.isArray(result.items)) throw new Error('No review state was returned.');
      renderReviewInbox(result);
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      target.replaceChildren(empty('Review state is unavailable: ' + error.message));
      byId('review-status').textContent = 'Review status is unknown. Refresh reviews to try again.';
    } finally {
      if (isCurrentScopedRequest(request)) {
        target.setAttribute('aria-busy', 'false');
        byId('review-refresh').disabled = false;
      }
    }
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
        node('strong', '', memoryTitle(memory)),
        node('span', '', truncate(memory.summary || memory.content, 140)),
      );
      row.addEventListener('click', () => openMemory(memory));
      target.append(row);
    });
  }

  async function loadStats(workspace, epoch) {
    const request = beginScopedRequest('stats');
    try {
      const stats = await api(`/stats?${query(workspace)}`, { signal: request.signal });
      if (epoch !== state.refreshEpoch) return;
      state.stats = stats;
      renderMetricValues(stats);
      renderTypeBars(stats);
      renderFirstMemoryJourney();
    } catch (error) {
      if (epoch === state.refreshEpoch) byId('type-bars').replaceChildren(empty('Workspace composition is unavailable.'));
      throw error;
    }
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

  async function loadMemories(workspace, epoch, page = 0) {
    resetMoveSelection();
    const request = beginScopedRequest('library');
    const params = new URLSearchParams({ workspace, limit: '100' });
    if (request.project) params.set('repo', request.project);
    const search = byId('library-filter').value.trim();
    const type = byId('library-type').value;
    if (search) params.set('q', search);
    if (type) params.set('mtype', type);
    const cursor = state.libraryCursors[page];
    if (cursor) params.set('cursor', cursor);
    state.libraryLoading = true;
    renderLibraryPaging();
    try {
      const payload = await api(`/memories?${params}`, { signal: request.signal });
      if (epoch !== state.refreshEpoch || !isCurrentScopedRequest(request)) return;
      state.memories = payload.memories || [];
      state.libraryTotal = number(payload.total_count == null ? state.memories.length : payload.total_count);
      state.libraryNextCursor = payload.next_cursor || null;
      state.libraryPage = page;
      if (!page) state.libraryCursors = [null];
      if (state.libraryNextCursor) state.libraryCursors[page + 1] = state.libraryNextCursor;
      state.libraryLoading = false;
      renderLibrary();
      renderFirstMemoryJourney();
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      if (cursor && error.status === 409 && error.code === 'cursor_stale') {
        state.libraryCursors = [null];
        showNotice('Memory changed. Showing the first page with your filters preserved.');
        return await loadMemories(workspace, epoch);
      }
      byId('library-list').replaceChildren(empty(`Library is unavailable: ${error.message}`));
      throw error;
    } finally {
      if (isCurrentScopedRequest(request)) {
        state.libraryLoading = false;
        renderLibraryPaging();
      }
    }
  }

  function renderLibraryPaging() {
    byId('library-previous').disabled = state.libraryLoading || state.libraryPage === 0;
    byId('library-next').disabled = state.libraryLoading || !state.libraryNextCursor;
    byId('library-refresh').disabled = state.libraryLoading || !state.workspace;
    byId('library-list').setAttribute('aria-busy', String(state.libraryLoading));
    byId('library-list').querySelectorAll('.memory-card').forEach(card => {
      card.disabled = state.libraryLoading;
    });
    renderMoveSelection();
  }

  function refreshLibrary(page = 0) {
    window.clearTimeout(librarySearchTimer);
    if (!state.workspace) return;
    loadMemories(state.workspace, state.refreshEpoch, page).catch(error => showNotice(error.message));
  }

  function renderFirstMemoryJourney() {
    const journey = byId('first-memory-journey');
    if (!journey) return;
    journey.hidden = Boolean(state.workspace) && (state.stats.memories == null
      || number(state.stats.memories) > 0 || state.libraryTotal > 0);
    byId('first-memory-add').textContent = state.workspace ? 'Add your first memory' : 'Create your first workspace';
  }

  /* Local memory health is computed on this database for every plan: retention buckets,
     memories without entity links, and recorded conflicts. */
  function renderHealth(payload, error = null) {
    const target = byId('home-health');
    target.replaceChildren();
    if (!payload) {
      target.append(empty(error ? `Memory health is unavailable: ${error.message}` : 'Memory health is unavailable.'));
      return;
    }
    const distribution = Array.isArray(payload.decay_distribution) ? payload.decay_distribution : [];
    const conflicts = payload.conflict_frequency || {};
    const total = distribution.reduce((sum, item) => sum + number(item.count), 0);
    const stats = node('div', 'stat-grid');
    [
      ['Without entity links', payload.orphan_count],
      ['Conflicts · 7 days', conflicts.last_7d],
      ['Conflicts · all time', conflicts.total],
    ].forEach(([label, value]) => {
      const item = node('div', 'stat-item');
      item.append(node('span', '', label), node('strong', '', number(value).toLocaleString()));
      stats.append(item);
    });
    target.append(stats);
    if (!total) {
      target.append(empty('Retention appears once this workspace has live memories.'));
      return;
    }
    const bars = node('div', 'type-bars');
    bars.setAttribute('role', 'group');
    bars.setAttribute('aria-label', 'Live memories by current retention');
    const max = Math.max(1, ...distribution.map(item => number(item.count)));
    distribution.forEach(item => {
      const label = text(item.label || item.bucket);
      const row = node('div', 'type-bar');
      const bar = document.createElement('progress');
      bar.max = max;
      bar.value = number(item.count);
      bar.setAttribute('aria-label', `Retention ${label}: ${number(item.count)} memories`);
      row.append(node('span', '', label), bar, node('strong', '', number(item.count).toLocaleString()));
      bars.append(row);
    });
    target.append(node('p', 'project-help', 'Current retention of live memories. Recall, pins and edits strengthen a memory; time weakens it without deleting it.'), bars);
  }

  /* The last-session handoff is recorded per project, so it is only shown when the sidebar
     names one. An empty handoff ({}) is treated as none. */
  function renderHandoff(project, handoff) {
    const section = byId('home-handoff');
    const body = byId('home-handoff-body');
    body.replaceChildren();
    const value = handoff && typeof handoff === 'object' ? handoff : {};
    const summary = text(typeof handoff === 'string' ? handoff : value.summary).trim();
    const outcome = text(value.outcome).trim();
    const threads = (Array.isArray(value.open_threads) ? value.open_threads : [])
      .map(item => text(typeof item === 'string' ? item : (item && (item.title || item.text || item.summary)) || '').trim())
      .filter(Boolean);
    if (!project || (!summary && !outcome && !threads.length)) {
      section.hidden = true;
      return;
    }
    byId('home-handoff-project').textContent = project;
    if (summary) body.append(node('p', '', summary));
    if (outcome) body.append(node('p', 'project-help', `Outcome: ${outcome}`));
    if (threads.length) {
      const list = node('ul');
      threads.slice(0, 8).forEach(thread => list.append(node('li', '', thread)));
      body.append(node('h3', '', 'Open threads'), list);
    }
    section.hidden = false;
  }

  async function loadToday(workspace, epoch) {
    const request = beginScopedRequest('today');
    const project = state.project;
    const [proactiveResult, auditResult, healthResult, handoffResult] = await Promise.allSettled([
      api(`/proactive?${query(workspace)}&k=8`, { signal: request.signal }),
      api(`/audit?${query(workspace)}&limit=12`, { signal: request.signal }),
      api(`/analytics/health?${query(workspace)}`, { signal: request.signal }),
      project
        ? api(`/proactive?${query(workspace)}&repo=${encodeURIComponent(project)}&k=1`, { signal: request.signal })
        : Promise.resolve(null),
    ]);
    if (epoch !== state.refreshEpoch) return;
    renderHealth(healthResult.status === 'fulfilled' ? healthResult.value : null,
      healthResult.status === 'rejected' ? healthResult.reason : null);
    renderHandoff(project, handoffResult.status === 'fulfilled' && handoffResult.value
      ? handoffResult.value.handoff : null);
    const proactive = proactiveResult.status === 'fulfilled'
      ? (proactiveResult.value.memories || proactiveResult.value.results || [])
      : [];
    renderProactive(proactive, proactiveResult.status === 'rejected'
      ? 'Strongest memories are unavailable. Try refreshing this workspace.' : '');
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

  const graphLifecycle = window.EngraphisGraphLifecycle.create({
    isVisible: () => state.view === 'relations',
    onStatus: ({ visible, capability }) => {
      const status = byId('graph-lifecycle-status');
      status.dataset.lifecycle = capability === 'drawing' ? (visible ? 'active' : 'paused') : capability;
      status.textContent = capability === 'unloaded' ? 'The graph loads when you open Explore.'
        : capability === 'physics' ? 'This renderer can pause physics only when you leave.'
        : capability === 'unsupported' ? 'Automatic pause is unavailable for this renderer.'
        : visible ? 'Select a node to inspect its evidence. Drawing pauses when you leave.'
        : 'Graph drawing paused. Camera and selection are kept.';
    },
    onError: () => showNotice('A graph resource could not be paused or released. Reload this page if it stays active.'),
  });
  const graphExplorer = window.EngraphisGraphExplorer.create({
    onOpen: item => openGraphConnections(item),
    onFocus: (item, position, total) => {
      focusGraphNode(item.id, item.name);
      byId('graph-keyboard-status').textContent = `${item.name}, ${number(item.relations)} ${number(item.relations) === 1 ? 'relation' : 'relations'}, ${position} of ${total}. Press Enter to open its connections.`;
    },
    onClear: () => {
      if (state.graphEngine) state.graphEngine.clearFocus();
      byId('graph-keyboard-status').textContent = 'Selection cleared.';
    },
  });
  const narrowSidebar = window.matchMedia('(max-width: 860px)');
  const syncSidebarOptions = () => { byId('sidebar-options').open = !narrowSidebar.matches; };
  if (narrowSidebar.addEventListener) narrowSidebar.addEventListener('change', syncSidebarOptions);
  else narrowSidebar.addListener(syncSidebarOptions);
  syncSidebarOptions();

  const processingControls = window.EngraphisProcessingControls.create(api);
  const askRequests = window.EngraphisAskRequests.create({ renderAnswer, renderPreview });
  const workflow = window.EngraphisWorkflow.create({
    api,
    beforeProjectChange: () => confirmDiscardEditor(),
    onProjectChange: () => { void selectWorkspace(state.workspace); },
    onNavigate: view => switchView(view),
    onNewMemory: async () => {
      if (!await confirmDiscardEditor()) return;
      switchView('library');
      openEditor();
    },
  });

  async function loadProjects(workspace, epoch) {
    const request = beginScopedRequest('projects');
    try {
      const result = await api('/repos?' + query(workspace), { signal: request.signal });
      if (epoch === state.refreshEpoch && isCurrentScopedRequest(request)) {
        workflow.setProjects(result && result.repos);
        refreshMemoryOwnershipLabels();
      }
    } catch (_) {
      if (isCurrentScopedRequest(request)) {
        workflow.projectsUnavailable();
        refreshMemoryOwnershipLabels();
      }
    }
  }

  async function selectWorkspace(name) {
    if (!name) return;
    resetMoveSelection(true);
    invalidateConsolidationReview();
    const epoch = ++state.refreshEpoch;
    invalidateScopedRequests();
    if (state.graphLoadController) state.graphLoadController.abort();
    askRequests.reset();
    window.clearTimeout(librarySearchTimer);
    state.libraryCursors = [null];
    state.libraryPage = 0;
    state.libraryNextCursor = null;
    state.libraryTotal = 0;
    state.memories = [];
    renderLibrary();
    renderLibraryPaging();
    closeGraphConnections();
    state.workspace = name;
    state.stats = {};
    renderMetricValues({});
    byId('type-bars').replaceChildren(empty('Loading workspace composition…'));
    renderFirstMemoryJourney();
    workflow.selectWorkspace(name);
    state.project = workflow.project();
    if (window.SpecStudio) window.SpecStudio.setScope(name, state.project);
    void processingControls.selectWorkspace(name);
    state.graphWorkspace = '';
    state.graphData = null;
    state.graphDataPreset = 'galaxy';
    state.graphDataIncludeCode = false;
    state.graphDataShowUnlinked = false;
    state.graphDataRepo = '';
    state.selectedMemory = '';
    // Detail/editor handlers close over a memory record.  Clear both before the
    // workspace fetches begin so a stale form cannot write that record into the
    // newly selected workspace.
    state.editorMemory = null;
    resetEditorSession();
    destroyMemoryHistory();
    byId('memory-editor').hidden = true;
    state.editorBaseline = '';
    setEditorMode(false);
    refreshEditorDirty();
    const memoryDetail = byId('memory-detail');
    memoryDetail.replaceChildren();
    memoryDetail.hidden = true;
    resetScopedPanels();
    state.syncStatus = null;
    graphLifecycle.clear();
    state.graphSpacetimeOverlay = null;
    state.graphEngine = null;
    byId('graph-legend').hidden = true;
    graphExplorer.setData(null);
    byId('workspace-select').value = name;
    renderWorkspaceNames();
    try {
      localStorage.setItem('engraphis-workspace', name);
    } catch (_) {}
    showNotice('');
    try {
      // Start the visible panel independently of hidden Library/Home requests.
      const visiblePanel = state.view === 'relations' ? loadGraph()
        : state.view === 'provenance' && state.provenanceTab === 'audit' ? loadAudit()
        : state.view === 'manage' ? Promise.all([loadSavings(epoch), loadManageTab(state.manageTab)])
        : Promise.resolve();
      const results = await Promise.allSettled([
        loadStats(name, epoch),
        loadProjects(name, epoch),
        loadMemories(name, epoch),
        loadReviewInbox(),
        loadToday(name, epoch),
        visiblePanel,
      ]);
      if (epoch !== state.refreshEpoch) return;
      const failed = results.find(result => result.status === 'rejected');
      if (failed) showNotice(`Some workspace panels could not refresh: ${failed.reason.message}`);
      renderWorkspaceList();
    } catch (error) {
      if (epoch === state.refreshEpoch) showNotice(`Could not refresh ${name}: ${error.message}`);
    }
  }

  /* Manual order is one global position per memory, so it is offered only on the unfiltered
     first page of the all-projects list, where the visible order is the stored order. */
  function libraryReorderEnabled() {
    return Boolean(state.workspace) && !state.project && !state.librarySelecting
      && !state.libraryLoading && state.libraryPage === 0 && state.memories.length > 1
      && !byId('library-filter').value.trim() && !byId('library-type').value;
  }

  let libraryOrderWrite = Promise.resolve();

  async function persistLibraryOrder(movedId, message) {
    const workspace = state.workspace;
    const epoch = state.refreshEpoch;
    const ids = state.memories.map(memory => memory.id);
    renderLibrary();
    const moved = byId('library-list').querySelector(`[data-memory-id="${CSS.escape(movedId)}"]`);
    if (moved) {
      byId('library-list').querySelectorAll('[role="option"]').forEach(card => { card.tabIndex = card === moved ? 0 : -1; });
      moved.focus();
    }
    // Preserve the order of accepted gestures even when the server is slow.
    const write = libraryOrderWrite.catch(() => {}).then(() => api('/memories/reorder', {
      method: 'POST', body: { workspace, ids },
    }));
    libraryOrderWrite = write;
    try {
      await write;
      if (write !== libraryOrderWrite) return;
      if (workspace === state.workspace && epoch === state.refreshEpoch) byId('library-reorder-status').textContent = message;
    } catch (error) {
      if (write !== libraryOrderWrite) return;
      if (workspace !== state.workspace || epoch !== state.refreshEpoch) return;
      showNotice(`Could not save the new order: ${error.message}`);
      refreshLibrary();
    }
  }

  function moveLibraryMemory(id, delta) {
    const from = state.memories.findIndex(memory => memory.id === id);
    const to = Math.max(0, Math.min(state.memories.length - 1, from + delta));
    if (from < 0) return;
    if (from === to) {
      byId('library-reorder-status').textContent = delta < 0 ? 'Already first.' : 'Already last on this page.';
      return;
    }
    const [moved] = state.memories.splice(from, 1);
    state.memories.splice(to, 0, moved);
    void persistLibraryOrder(id, `${memoryTitle(moved)} moved to position ${to + 1}.`);
  }

  function dropLibraryMemory(dragId, targetId, before) {
    if (!dragId || dragId === targetId) return;
    const from = state.memories.findIndex(memory => memory.id === dragId);
    if (from < 0) return;
    const [moved] = state.memories.splice(from, 1);
    const anchor = state.memories.findIndex(memory => memory.id === targetId);
    if (anchor < 0) {
      state.memories.splice(from, 0, moved);
      return;
    }
    const to = before ? anchor : anchor + 1;
    state.memories.splice(to, 0, moved);
    void persistLibraryOrder(dragId, `${memoryTitle(moved)} moved to position ${to + 1}.`);
  }

  function enableCardDragging(card, memory) {
    card.draggable = true;
    const handle = node('span', 'memory-drag-handle', '⠿');
    handle.setAttribute('aria-hidden', 'true');
    handle.title = 'Drag to reorder';
    card.prepend(handle);
    card.addEventListener('dragstart', event => {
      state.libraryDragId = memory.id;
      card.classList.add('dragging');
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', memory.id);
      }
    });
    card.addEventListener('dragover', event => {
      if (!state.libraryDragId || state.libraryDragId === memory.id) return;
      event.preventDefault();
      const bounds = card.getBoundingClientRect();
      const before = event.clientY - bounds.top < bounds.height / 2;
      card.classList.toggle('drag-over-top', before);
      card.classList.toggle('drag-over-bottom', !before);
    });
    card.addEventListener('dragleave', () => card.classList.remove('drag-over-top', 'drag-over-bottom'));
    card.addEventListener('drop', event => {
      event.preventDefault();
      const before = card.classList.contains('drag-over-top');
      card.classList.remove('drag-over-top', 'drag-over-bottom');
      const dragId = state.libraryDragId;
      state.libraryDragId = '';
      dropLibraryMemory(dragId, memory.id, before);
    });
    card.addEventListener('dragend', () => {
      state.libraryDragId = '';
      byId('library-list').querySelectorAll('.memory-card').forEach(item => {
        item.classList.remove('dragging', 'drag-over-top', 'drag-over-bottom');
      });
    });
  }

  function memoryCard(memory, reorderable = false) {
    const card = node('button', 'memory-card');
    card.type = 'button';
    card.setAttribute('role', 'option');
    card.dataset.memoryId = memory.id;
    const selected = state.librarySelecting ? state.moveMemoryIds.has(memory.id) : state.selectedMemory === memory.id;
    card.setAttribute('aria-selected', String(selected));
    if (selected) card.classList.add('selected');
    if (state.librarySelecting) {
      const marker = node('span', 'memory-selection-marker', selected ? 'Selected for move' : 'Select for move');
      marker.setAttribute('aria-hidden', 'true');
      card.append(marker);
    }
    card.append(
      node('h2', '', memoryTitle(memory)),
      node('p', '', truncate(memory.content || memory.summary, 240)),
      memoryMeta(memory),
    );
    if (reorderable) {
      enableCardDragging(card, memory);
      card.setAttribute('aria-keyshortcuts', 'Alt+ArrowUp Alt+ArrowDown');
    }
    card.addEventListener('click', () => {
      if (!state.librarySelecting) {
        void openMemory(memory);
        return;
      }
      if (state.libraryLoading) return;
      if (state.moveMemoryIds.has(memory.id)) state.moveMemoryIds.delete(memory.id);
      else state.moveMemoryIds.add(memory.id);
      closeMemoryMove();
      renderLibrary();
      byId('library-list').querySelectorAll('[data-memory-id]').forEach(item => {
        if (item.dataset.memoryId === memory.id) { item.tabIndex = 0; item.focus(); }
        else item.tabIndex = -1;
      });
    });
    return card;
  }

  function filteredMemories() {
    return state.memories;
  }

  function renderLibrary() {
    const target = byId('library-list');
    target.setAttribute('aria-multiselectable', String(state.librarySelecting));
    renderMoveSelection();
    if (!target.dataset.keyboardBound) {
      target.dataset.keyboardBound = 'true';
      target.addEventListener('keydown', event => {
        const cards = [...target.querySelectorAll('[role="option"]')];
        const current = event.target.closest('[role="option"]');
        if (!current || !cards.length) return;
        if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
          event.preventDefault();
          if (libraryReorderEnabled()) moveLibraryMemory(current.dataset.memoryId, event.key === 'ArrowUp' ? -1 : 1);
          else byId('library-reorder-status').textContent = 'Clear the search, type and project filters and return to the first page to reorder.';
          return;
        }
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
    const total = state.libraryTotal;
    const start = state.libraryPage * 100 + (memories.length ? 1 : 0);
    byId('library-count').textContent = total > memories.length
      ? `${start.toLocaleString()}–${(start + memories.length - 1).toLocaleString()} of ${total.toLocaleString()} memories`
      : `${total.toLocaleString()} ${total === 1 ? 'memory' : 'memories'}`;
    if (!memories.length) {
      byId('library-reorder-hint').hidden = true;
      target.append(empty(byId('library-filter').value.trim() || byId('library-type').value
        ? 'No memories match these filters.' : 'No active memories in this workspace. Add a fact or import local documents to begin.'));
      return;
    }
    const reorderable = libraryReorderEnabled();
    byId('library-reorder-hint').hidden = !reorderable;
    memories.forEach(memory => target.append(memoryCard(memory, reorderable)));
    const cards = [...target.querySelectorAll('[role="option"]')];
    const selectedIndex = cards.findIndex(card => card.getAttribute('aria-selected') === 'true');
    cards.forEach((card, index) => { card.tabIndex = index === (selectedIndex >= 0 ? selectedIndex : 0) ? 0 : -1; });
  }

  function renderMoveSelection() {
    const toggle = byId('library-selection-toggle');
    toggle.disabled = state.libraryLoading || !state.workspace || !state.memories.length;
    toggle.setAttribute('aria-pressed', String(state.librarySelecting));
    toggle.textContent = state.librarySelecting ? 'Cancel selection' : 'Select memories';
    byId('library-selection-status').textContent = state.librarySelecting
      ? `${state.moveMemoryIds.size} selected on this page. Changing results clears the selection.`
      : 'Select memories on this page to move them to another workspace.';
    byId('library-move').disabled = state.libraryLoading || !state.moveMemoryIds.size;
  }

  function resetMoveSelection(finish = false) {
    closeMemoryMove();
    state.moveMemoryIds.clear();
    if (finish) state.librarySelecting = false;
    renderMoveSelection();
  }

  function closeMemoryMove() {
    const dialog = byId('memory-move-dialog');
    const move = state.memoryMove;
    state.memoryMove = null;
    beginScopedRequest('memory-move');
    if (dialog.open) dialog.close();
    if (move && move.returnFocus && move.returnFocus.isConnected) move.returnFocus.focus();
  }

  function invalidateMemoryMovePreview() {
    const move = state.memoryMove;
    if (!move || move.applying) return;
    beginScopedRequest('memory-move');
    move.preview = null;
    move.loading = false;
    byId('memory-move-error').hidden = true;
    byId('memory-move-preview').replaceChildren(empty('Choose a destination, then preview the move.'));
    renderMemoryMoveControls();
  }

  function renderMemoryMoveControls() {
    const move = state.memoryMove;
    if (!move) return;
    const busy = move.loading || move.applying;
    const target = byId('memory-move-target');
    target.disabled = move.applying;
    byId('memory-move-cancel').disabled = move.applying;
    byId('memory-move-preview-button').disabled = busy || !target.value;
    byId('memory-move-apply').disabled = busy || !move.preview || move.preview.can_move !== true;
    byId('memory-move-form').setAttribute('aria-busy', String(busy));
  }

  function openMemoryMove() {
    if (!state.moveMemoryIds.size || state.libraryLoading) return;
    closeMemoryMove();
    state.memoryMove = {
      workspace: state.workspace, project: state.project,
      ids: [...state.moveMemoryIds].sort(), preview: null, loading: false, applying: false,
      returnFocus: document.activeElement,
    };
    const target = byId('memory-move-target');
    target.replaceChildren(option('', 'Choose a workspace'));
    state.workspaces.filter(item => workspaceName(item) && workspaceName(item) !== state.workspace)
      .sort((a, b) => workspaceName(a).localeCompare(workspaceName(b))).forEach(item => {
        const name = workspaceName(item);
        const access = item.visibility === 'personal' ? 'Personal'
          : item.visibility === 'shared' ? 'Shared' : 'Access shown in preview';
        target.append(option(name, `${name} · ${access}`));
      });
    byId('memory-move-source').textContent = `From ${JSON.stringify(state.workspace)} · ${state.moveMemoryIds.size} selected`;
    invalidateMemoryMovePreview();
    if (target.options.length === 1) {
      byId('memory-move-preview').replaceChildren(empty('Create another accessible workspace in Settings before moving memories.'));
    }
    byId('memory-move-dialog').showModal();
    target.focus();
  }

  function memoryMoveBody(move) {
    return { workspace: move.workspace, target_workspace: byId('memory-move-target').value, memory_ids: move.ids };
  }

  function renderMemoryMovePreview(preview, move) {
    const target = byId('memory-move-preview');
    const rows = [
      ['From workspace', preview.source], ['To workspace', preview.target],
      ['Selected memories', move.ids.length], ['Total records to move', preview.count],
      ['Related memories and history', preview.related_count],
    ];
    if (preview.sessions != null) rows.push(['Closed sessions included', preview.sessions]);
    if (preview.graph_edges != null) rows.push(['Graph relationships included', preview.graph_edges]);
    if (Array.isArray(preview.repos) && preview.repos.length) rows.push(['Projects preserved', preview.repos.join(', ')]);
    target.replaceChildren(definitionList(rows.map(([label, value]) => [label, text(value)])));
    const visibilityLabel = value => value === 'personal' ? 'Personal' : value === 'shared' ? 'Shared' : 'Unknown';
    target.append(node('p', 'project-help', `Access: ${visibilityLabel(preview.source_visibility)} → ${visibilityLabel(preview.target_visibility)}.`));
    if (preview.target_visibility === 'shared') {
      target.append(node('p', 'project-help', 'The destination is shared. Other users with workspace access can read the moved workspace and project memories. Session ownership restrictions still apply.'));
    } else if (preview.target_visibility === 'personal') {
      target.append(node('p', 'project-help', 'The destination is personal. Its owner controls access to the moved memories.'));
    }
    target.append(node('p', 'project-help', 'Related memories and their history, closed sessions and their events move together. Original identifiers and preserved history stay attached to the memories.'));
    if (Array.isArray(preview.memories) && preview.memories.length) {
      const details = node('details', 'memory-move-records');
      details.append(node('summary', '', `Review all ${preview.memories.length} records`));
      const list = node('ul');
      preview.memories.forEach(memory => {
        const item = node('li');
        item.append(node('strong', '', memory.title || memory.id),
          node('span', '', ` · ${memory.id}${memory.related ? ' · related record' : ' · selected'}`));
        list.append(item);
      });
      details.append(list);
      target.append(details);
    }
    const blockers = Array.isArray(preview.blockers) ? preview.blockers : [];
    if (blockers.length) {
      target.append(node('p', 'form-error', 'These records cannot be moved yet:'));
      const list = node('ul', 'memory-move-blockers');
      blockers.forEach(blocker => list.append(node('li', '', blocker.message || blocker.code || 'Move is blocked.')));
      target.append(list);
    } else if (preview.can_move === true) {
      target.append(node('p', 'project-help', 'Review this destination and complete record list, then choose Move memories.'));
    }
  }

  async function previewMemoryMove() {
    const move = state.memoryMove;
    if (!move || move.loading || move.applying || !byId('memory-move-target').value) return;
    const request = beginScopedRequest('memory-move');
    const body = memoryMoveBody(move);
    move.preview = null;
    move.loading = true;
    byId('memory-move-error').hidden = true;
    byId('memory-move-preview').replaceChildren(empty('Checking the complete move and its related history…'));
    renderMemoryMoveControls();
    try {
      const preview = await api('/memories/move-preview', { method: 'POST', body, signal: request.signal });
      if (state.memoryMove !== move || !isCurrentScopedRequest(request) || body.target_workspace !== byId('memory-move-target').value) return;
      const sameIds = Array.isArray(preview.requested_ids)
        && JSON.stringify([...preview.requested_ids].sort()) === JSON.stringify(move.ids);
      if (preview.source !== body.workspace || preview.target !== body.target_workspace || !sameIds
        || (preview.can_move === true && (typeof preview.preview_token !== 'string' || !preview.preview_token))) {
        throw new Error('The preview does not match this selection. Request a new preview.');
      }
      if (Array.isArray(preview.blockers) && preview.blockers.length) preview.can_move = false;
      move.preview = preview;
      renderMemoryMovePreview(preview, move);
    } catch (error) {
      if (state.memoryMove !== move || !isCurrentScopedRequest(request)) return;
      byId('memory-move-preview').replaceChildren(empty('No move has been submitted.'));
      byId('memory-move-error').textContent = `Could not preview the move: ${error.message}`;
      byId('memory-move-error').hidden = false;
    } finally {
      if (state.memoryMove === move && isCurrentScopedRequest(request)) {
        move.loading = false;
        renderMemoryMoveControls();
      }
    }
  }

  async function applyMemoryMove(event) {
    event.preventDefault();
    const move = state.memoryMove;
    if (!move || move.loading || move.applying || !move.preview || move.preview.can_move !== true) return;
    const request = beginScopedRequest('memory-move');
    const body = { ...memoryMoveBody(move), preview_token: move.preview.preview_token, confirmed: true };
    if (body.target_workspace !== move.preview.target || body.workspace !== move.preview.source) {
      invalidateMemoryMovePreview();
      return;
    }
    move.applying = true;
    byId('memory-move-error').hidden = true;
    renderMemoryMoveControls();
    try {
      const result = await api('/memories/move', { method: 'POST', body, signal: request.signal });
      if (state.memoryMove !== move || !isCurrentScopedRequest(request)) return;
      closeMemoryMove();
      await selectWorkspace(move.workspace);
      if (state.workspace === move.workspace) showNotice(`Moved ${number(result.count)} records to ${JSON.stringify(result.workspace)}. History is preserved.`);
    } catch (error) {
      if (state.memoryMove !== move || !isCurrentScopedRequest(request)) return;
      move.preview = null;
      byId('memory-move-error').textContent = error.status === 409
        ? 'Memory or workspace state changed. Preview the move again before continuing.'
        : `The move could not be confirmed: ${error.message} Preview again to check the current records before retrying.`;
      byId('memory-move-error').hidden = false;
    } finally {
      if (state.memoryMove === move && isCurrentScopedRequest(request)) {
        move.applying = false;
        renderMemoryMoveControls();
      }
    }
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

  /* Governance actions recorded against this exact record, newest first. */
  function auditTrail(rows) {
    const entries = (Array.isArray(rows) ? rows : []).slice(-20).reverse();
    if (!entries.length) return null;
    const section = node('section', 'audit-trail');
    section.setAttribute('aria-labelledby', 'memory-audit-title');
    const heading = node('h3', '', 'Audit trail');
    heading.id = 'memory-audit-title';
    const list = node('ol');
    entries.forEach(row => {
      const item = node('li');
      const when = node('time', '', relative(row.ts || row.at));
      const date = validDate(row.ts || row.at);
      if (date && date.getTime() > 0) when.dateTime = date.toISOString();
      item.append(
        node('span', 'tag', text(row.action || row.op || 'change').replaceAll('_', ' ')),
        node('span', '', text(row.detail || row.reason || '')),
        when,
      );
      if (row.actor) item.append(node('span', 'audit-actor', text(row.actor)));
      list.append(item);
    });
    section.append(heading, list);
    return section;
  }

  async function selectMemory(id) {
    const workspace = state.workspace;
    const project = state.project;
    const request = beginScopedRequest('memory-detail');
    state.selectedMemory = id;
    resetEditorSession();
    destroyMemoryHistory();
    renderLibrary();
    const target = byId('memory-detail');
    target.hidden = false;
    byId('memory-editor').hidden = true;
    state.editorBaseline = '';
    setEditorMode(false);
    refreshEditorDirty();
    target.replaceChildren(empty('Loading memory…'));
    try {
      const payload = await api(`/memory/${encodeURIComponent(id)}?${query(workspace)}`, { signal: request.signal });
      const memory = payload.memory || state.memories.find(item => item.id === id);
      if (!memory || state.selectedMemory !== id || !isCurrentScopedRequest(request)) return;
      state.editorMemory = memory;
      target.replaceChildren();
      const title = node('h2', '', memoryTitle(memory));
      title.id = 'memory-detail-title';
      const content = node('div', 'markdown-body memory-content');
      markdown.renderInto(content, memory.content || memory.summary || 'No content.');
      target.append(
        node('p', 'eyebrow', `${memoryType(memory)} · ${memory.scope || 'workspace'}`),
        title,
        content,
        memoryMeta(memory),
        definitionList([
          ['Memory id', memory.id],
          ['Workspace id', memory.workspace_id],
          ['Project id', memory.repo_id],
          ['Session id', memory.session_id],
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
        actions.append(memoryMutationButton('Approve for prompt…', 'primary-button', () => approveForPrompt(memory)));
      }
      actions.append(
        button('Review with Jev', 'secondary-button', () => openJevReview(memory)),
        button(memory.can_revise === false ? 'Review saved versions' : 'Edit', 'secondary-button', () => openEditor(memory)),
        memoryMutationButton(memory.pinned ? 'Unpin' : 'Pin', 'secondary-button', () => togglePin(memory)),
        button('Search topic timeline', 'secondary-button', () => openMemoryTimeline(memory)),
        memoryMutationButton('Retire', 'danger-button', () => retireMemory(memory)),
        memoryMutationButton('Secure erase leak', 'danger-button', () => secureEraseMemory(memory)),
      );
      target.append(actions);
      const trail = auditTrail(payload.audit);
      if (trail) target.append(trail);
      state.memoryHistory = window.EngraphisMemoryHistory.create({
        api, id, workspace, repo: project,
        isCurrent: () => isCurrentScopedRequest(request) && state.selectedMemory === id,
        onOpen: openMemory,
      });
      target.append(state.memoryHistory.element);
      void state.memoryHistory.load();
    } catch (error) {
      if (state.selectedMemory === id && isCurrentScopedRequest(request)) {
        target.replaceChildren(empty(`Could not inspect memory: ${error.message}`));
      }
    }
  }

  function openJevReview(memory) {
    const target = byId('memory-detail');
    const prior = byId('jev-review-panel');
    if (prior) { prior.remove(); return; }
    const panel = node('section', 'jev-review-panel');
    panel.id = 'jev-review-panel';
    panel.setAttribute('aria-labelledby', 'jev-review-title');
    panel.append(
      node('h3', '', 'Review selected evidence with Jev'),
      node('p', 'project-help', 'This advisory checks whether the selected memory supports your claim and, if you choose a second memory, whether the two conflict. It never changes stored memories.'),
    );
    panel.querySelector('h3').id = 'jev-review-title';
    const form = node('form');
    const claimLabel = node('label');
    claimLabel.append(node('span', '', 'Question for the selected evidence'));
    const claim = node('textarea');
    claim.rows = 3;
    claim.maxLength = 1200;
    claim.required = true;
    claim.value = memory.title || '';
    claim.placeholder = 'What should this memory support?';
    claimLabel.append(claim);
    const peerLabel = node('label');
    peerLabel.append(node('span', '', 'Compare with another memory (optional)'));
    const peer = node('select');
    peer.append(option('', 'No contradiction comparison'));
    state.memories.filter(item => item && item.id && item.id !== memory.id).forEach(item => {
      peer.append(option(item.id, memoryTitle(item)));
    });
    peerLabel.append(peer);
    const remoteLabel = node('label', 'check-row');
    const remote = node('input');
    remote.type = 'checkbox';
    remoteLabel.append(remote, document.createTextNode(' Allow this action to send the question and selected excerpts to Jev'));
    const classificationLabel = node('label');
    classificationLabel.append(node('span', '', 'Data classification for this action'));
    const classification = node('select');
    classification.append(option('internal', 'Internal', true), option('public', 'Public'));
    classificationLabel.append(classification);
    const status = node('p', 'project-help');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const result = node('div', 'jev-review-result');
    const submit = node('button', 'primary-button', 'Run read-only review');
    submit.type = 'submit';
    form.append(claimLabel, peerLabel, remoteLabel, classificationLabel, submit);
    form.addEventListener('submit', event => { event.preventDefault(); void runJevReview(); });
    panel.append(form, status, result);
    target.append(panel);

    async function runJevReview() {
      const claimText = claim.value.trim();
      if (!claimText) {
        status.textContent = 'Enter a question before reviewing.';
        claim.focus();
        return;
      }
      if (!state.workspace || state.selectedMemory !== memory.id) return;
      const request = beginScopedRequest('jev-review');
      const memoryIds = [memory.id, ...(peer.value ? [peer.value] : [])];
      submit.disabled = true;
      status.textContent = 'Running a read-only advisory review…';
      result.replaceChildren();
      try {
        const response = await api('/jev/review', {
          method: 'POST', signal: request.signal,
          body: {
            workspace: request.workspace,
            ...(request.project ? { repo: request.project } : {}),
            memory_ids: memoryIds,
            claim: claimText,
            allow_remote: remote.checked,
            data_classification: classification.value,
          },
        });
        if (!isCurrentScopedRequest(request) || state.selectedMemory !== memory.id) return;
        renderJevReviewResult(result, response);
        status.textContent = response.remote_blocked_reason
          ? `Remote consent was granted, but sending was blocked (${response.remote_blocked_reason.replaceAll('_', ' ')}). See the fallback result.`
          : response.remote_consent_granted
            ? 'Remote consent was granted for this action only. This does not confirm a provider request; check each result for fallback or uncertainty.'
            : 'Remote consent was not granted for this action. Check each result for fallback or uncertainty.';
      } catch (error) {
        if (isCurrentScopedRequest(request) && state.selectedMemory === memory.id) {
          status.textContent = `Review unavailable: ${error.message}`;
        }
      } finally {
        submit.disabled = false;
      }
    }
  }

  function renderJevReviewResult(target, response) {
    const fallbackLabels = {
      allowance_exhausted: 'The included Jev allowance is currently used up. Check account usage for the next available window.',
      provider_protection_limit: 'Jev is temporarily paused by a service-protection limit. Try again later.',
      remote_timeout: 'Jev did not respond before the request deadline. You can retry later.',
      session_changed: 'The Cloud session changed during the request. Sign in again before retrying.',
      managed_operation_unsupported: 'This Jev operation is unavailable in the managed service.',
      remote_unavailable: 'The Jev service is unavailable. You can retry later.',
      malformed_response: 'Jev returned a response that could not be validated. Review the sources manually.',
    };
    const labelStatus = item => item.status === 'decision' ? 'Advisory decision'
      : item.status === 'uncertain' ? 'Uncertain; no conclusion'
      : item.status === 'not_requested' ? 'Not checked'
      : 'Fallback; review manually';
    const describe = (name, item) => {
      const card = node('article');
      card.append(node('strong', '', `${name} · ${labelStatus(item)}`));
      if (item.status === 'decision') {
        const value = item.value === true ? 'Evidence appears sufficient to answer the question.'
          : item.value === false ? 'Evidence appears insufficient to answer the question.'
          : item.value === 'contradicts_and_supersedes' ? 'The memories may contradict.'
          : item.value === 'reinforces' ? 'The memories appear consistent.'
          : item.value === 'orthogonal' ? 'No direct relationship was found.'
          : 'Jev returned an advisory result.';
        card.append(node('p', '', value));
      } else if (item.reason) card.append(node('p', '', item.reason.replaceAll('_', ' ')));
      if (item.fallback_reason) card.append(node('p', '', fallbackLabels[item.fallback_reason]
        || 'Jev could not complete this check. Review the sources manually.'));
      if (Number.isFinite(item.probability)) card.append(node('p', '', `Support probability: ${item.probability.toFixed(2)} · advisory only`));
      if (Number.isFinite(item.confidence)) card.append(node('p', '', `Confidence: ${item.confidence.toFixed(2)} · advisory only`));
      target.append(card);
    };
    target.replaceChildren();
    describe('Evidence support', response.support || {});
    describe('Contradiction check', response.contradiction || {});
    target.append(node('p', 'project-help', 'Jev cannot authorize a memory change, grounded answer, or command. Review the sources yourself.'));
  }

  async function openMemory(memory) {
    if (!memory || !memory.id) {
      showNotice('This result no longer identifies a memory to inspect.');
      return;
    }
    if (!await confirmDiscardEditor()) return;
    switchView('library');
    selectMemory(memory.id);
  }

  /* ── editor draft state: unsaved-change indicator, discard guard, preview ── */
  function editorSnapshot() {
    return JSON.stringify([
      byId('editor-memory-title').value,
      byId('editor-memory-type').value,
      byId('editor-memory-content').value,
      byId('editor-memory-importance').value,
      byId('editor-memory-scope').value,
    ]);
  }

  function editorOpen() {
    return !byId('memory-editor').hidden && Boolean(state.editorSession);
  }

  /* A submitted draft is being saved, not abandoned, so it does not count as unsaved while the
     save is in flight; confirmDiscardEditor() asks separately before leaving such a save. */
  function editorDirty() {
    return editorOpen() && !state.editorSession.busy && Boolean(state.editorBaseline)
      && editorSnapshot() !== state.editorBaseline;
  }

  function refreshEditorDirty() {
    const label = byId('editor-save-state');
    const dirty = editorDirty();
    label.textContent = dirty ? 'Unsaved changes' : '';
    label.classList.toggle('dirty', dirty);
  }

  function commitEditorBaseline() {
    state.editorBaseline = editorOpen() ? editorSnapshot() : '';
    refreshEditorDirty();
  }

  async function confirmDiscardEditor() {
    if (editorOpen() && state.editorSession.busy) {
      /* The save is still in flight. Leaving does not cancel it, but if it then fails the
         draft is gone with the editor, so say so instead of asking to "discard". */
      return dialogs.confirm({
        title: 'Leave while saving?',
        message: 'This memory is still being saved. If the save fails after you leave, the draft cannot be recovered.',
        confirmLabel: 'Leave anyway',
        cancelLabel: 'Stay',
      });
    }
    if (!editorDirty()) return true;
    const title = byId('editor-memory-title').value.trim() || 'this memory';
    return dialogs.confirm({
      title: 'Discard unsaved changes?',
      message: `Your changes to “${title}” have not been saved. Discard them?`,
      confirmLabel: 'Discard changes',
      cancelLabel: 'Keep editing',
      danger: true,
    });
  }

  async function requestCloseEditor() {
    if (await confirmDiscardEditor()) closeEditor();
  }

  function setEditorMode(preview) {
    const content = byId('editor-memory-content');
    const panel = byId('editor-preview');
    if (preview) markdown.renderInto(panel, content.value.trim() ? content.value : 'Nothing to preview yet.');
    else panel.replaceChildren();
    panel.hidden = !preview;
    content.hidden = preview;
    [['editor-write-mode', !preview], ['editor-preview-mode', preview]].forEach(([id, active]) => {
      const control = byId(id);
      control.classList.toggle('active', active);
      control.setAttribute('aria-pressed', String(active));
    });
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
      node('h3', '', memoryTitle(memory)),
      node('p', '', truncate(memory.content || memory.summary, 500)),
      memoryMeta(memory),
    );
    return card;
  }

  function openEditor(memory = null) {
    // A late detail response must not turn a new draft into an old revision.
    beginScopedRequest('memory-detail');
    resetEditorSession();
    state.editorSession = {
      revision: window.EngraphisMemoryRevision.create(memory), busy: false,
      conflicted: Boolean(memory && memory.can_revise === false),
    };
    state.editorMemory = memory;
    state.editorReturnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement : byId('new-memory-button');
    byId('memory-detail').hidden = true;
    const editor = byId('memory-editor');
    editor.hidden = false;
    byId('editor-title').textContent = memory ? 'Revise memory' : 'New memory';
    byId('editor-memory-title').value = memory ? (memory.title || '') : '';
    byId('editor-memory-type').value = memory ? memoryType(memory) : 'semantic';
    const scope = byId('editor-memory-scope');
    scope.querySelector('[value="repo"]').disabled = !state.project;
    scope.querySelector('[value="repo"]').textContent = state.project
      ? 'Project: ' + state.project : 'Select a project first';
    scope.value = state.project ? 'repo' : 'workspace';
    scope.disabled = Boolean(memory);
    byId('editor-scope-control').hidden = Boolean(memory);
    byId('editor-scope-note').textContent = memory
      ? 'This revision preserves the existing ' + (memory.scope || 'workspace') + ' scope.'
      : 'Workspace: ' + state.workspace + '. Choose where this new memory belongs.';
    byId('editor-memory-content').value = memory ? (memory.content || memory.summary || '') : '';
    byId('editor-memory-content').removeAttribute('aria-invalid');
    byId('editor-error').hidden = true;
    byId('editor-error').textContent = '';
    const importanceControl = byId('editor-memory-importance');
    const storedImportance = memory && memory.importance != null ? memory.importance : 0.5;
    importanceControl.value = String(graphSliderInputValue(
      'editor-memory-importance', storedImportance, 0.5,
    ));
    const respVal = graphSliderResponseValue(
      'editor-memory-importance', importanceControl.value, 0.5,
    );
    importanceControl.setAttribute('aria-valuetext', `${respVal.toFixed(2)} importance`);
    const importanceOutput = byId('editor-memory-importance-output');
    if (importanceOutput) importanceOutput.textContent = respVal.toFixed(2);
    if (state.editorSession.conflicted) {
      byId('editor-error').textContent = 'This version cannot be revised. Refresh saved versions and choose an editing base; your draft will be retained.';
      byId('editor-error').hidden = false;
      byId('editor-refresh').hidden = false;
      setEditorBusy(false);
    }
    setEditorMode(false);
    commitEditorBaseline();
    byId('editor-memory-title').focus();
  }

  function destroyMemoryHistory() {
    if (state.memoryHistory) state.memoryHistory.destroy();
    state.memoryHistory = null;
  }

  function resetEditorSession() {
    const session = state.editorSession;
    if (session && session.history) session.history.destroy();
    state.editorSession = null;
    byId('editor-refresh').hidden = true;
    byId('editor-history').replaceChildren();
    byId('editor-history').hidden = true;
    setEditorBusy(false);
  }

  function setEditorBusy(busy) {
    const editor = byId('memory-editor');
    editor.setAttribute('aria-busy', String(busy));
    editor.querySelectorAll('input, select, textarea, button').forEach(control => { control.disabled = busy; });
    byId('editor-memory-scope').disabled = busy || Boolean(state.editorMemory);
    editor.querySelector('button[type="submit"]').disabled = busy || Boolean(state.editorSession && state.editorSession.conflicted);
  }

  function refreshEditorVersions() {
    const session = state.editorSession;
    const original = state.editorMemory;
    const workspace = state.workspace;
    const project = state.project;
    if (!session || !original || session.busy) return;
    if (session.history) session.history.destroy();
    const target = byId('editor-history');
    target.hidden = false;
    session.history = window.EngraphisMemoryHistory.create({
      api, id: original.id, workspace, repo: project, original,
      isCurrent: () => state.editorSession === session && state.workspace === workspace && state.project === project,
      onUseBase: record => {
        state.editorMemory = record;
        session.revision = window.EngraphisMemoryRevision.create(record);
        session.conflicted = false;
        session.history.destroy();
        target.replaceChildren();
        target.hidden = true;
        byId('editor-refresh').hidden = true;
        byId('editor-error').textContent = 'Editing base refreshed. Your draft is unchanged; review it before saving.';
        byId('editor-error').hidden = false;
        setEditorBusy(false);
        byId('editor-memory-content').focus();
      },
    });
    target.replaceChildren(session.history.element);
    void session.history.load();
  }

  function closeEditor() {
    const returnFocus = state.editorReturnFocus;
    resetEditorSession();
    setEditorMode(false);
    state.editorBaseline = '';
    byId('memory-editor').hidden = true;
    refreshEditorDirty();
    byId('memory-detail').hidden = false;
    state.editorMemory = null;
    state.editorReturnFocus = null;
    if (returnFocus && document.contains(returnFocus) && !returnFocus.hidden
      && !returnFocus.disabled) returnFocus.focus();
    else byId('new-memory-button').focus();
  }

  async function saveMemory(event) {
    event.preventDefault();
    const session = state.editorSession;
    if (!session || session.busy || session.conflicted) return;
    const current = state.editorMemory;
    let savedId = current && current.id;
    const workspace = state.workspace;
    const epoch = state.refreshEpoch;
    const title = byId('editor-memory-title').value.trim();
    const memoryTypeValue = byId('editor-memory-type').value;
    const content = byId('editor-memory-content').value.trim();
    const importance = graphSliderResponseValue(
      'editor-memory-importance', number(byId('editor-memory-importance').value), 0.5,
    );
    const creationScope = byId('editor-memory-scope').value;
    const creationProject = creationScope === 'repo' ? state.project : '';
    const currentImportance = current && current.importance != null
      ? number(current.importance) : 0.5;
    const contentField = byId('editor-memory-content');
    const editorError = byId('editor-error');
    contentField.removeAttribute('aria-invalid');
    editorError.hidden = true;
    editorError.textContent = '';
    if (!current && creationScope === 'repo' && !creationProject) {
      editorError.textContent = 'Select a project before saving a project memory.';
      editorError.hidden = false;
      return;
    }
    if (!content) {
      setEditorMode(false);
      contentField.setAttribute('aria-invalid', 'true');
      editorError.textContent = 'Enter memory content before saving.';
      editorError.hidden = false;
      showNotice('Enter memory content before saving.');
      contentField.focus();
      return;
    }
    let written = false;
    try {
      session.busy = true;
      setEditorBusy(true);
      if (current) {
        if (content !== (current.content || current.summary || '')
          || title !== (current.title || '') || memoryTypeValue !== memoryType(current)
          || importance !== currentImportance) {
          const body = session.revision.body({ workspace, content, title, mtype: memoryTypeValue, importance, reason: 'revised in Ledger' });
          const revised = await api('/memory/revise', { method: 'POST', body });
          if (!revised || !revised.id || !revised.version || !revised.receipt
            || revised.receipt.status !== 'committed' || revised.receipt.operation !== 'revise'
            || revised.receipt.operation_id !== body.operation_id) {
            throw new Error('The save could not be confirmed. Retry the unchanged draft to check this operation.');
          }
          savedId = revised.id;
          if (state.editorSession === session) {
            state.editorMemory = { ...current, id: revised.id, version: revised.version, content, title, memory_type: memoryTypeValue, importance };
          }
        }
        if (workspace === state.workspace && epoch === state.refreshEpoch) {
          showNotice('Memory revision recorded with temporal history preserved.');
        }
      } else {
        const saved = await api('/remember', {
          method: 'POST',
          body: {
            workspace,
            content,
            title,
            mtype: memoryTypeValue,
            scope: creationScope,
            ...(creationProject ? { repo: creationProject } : {}),
            importance,
            source: 'human:ledger',
            trusted: true,
          },
        });
        savedId = saved && saved.id;
        if (workspace === state.workspace && epoch === state.refreshEpoch) {
          showNotice('Memory saved locally. Review its source before approving it for model context.');
        }
      }
      written = true;
      if (workspace !== state.workspace || epoch !== state.refreshEpoch || state.editorSession !== session) return;
      closeEditor();
      await selectWorkspace(workspace);
      if (savedId && workspace === state.workspace && state.refreshEpoch === epoch + 1
        && !state.editorSession && !state.selectedMemory) await selectMemory(savedId);
    } catch (error) {
      if (workspace === state.workspace && epoch === state.refreshEpoch && state.editorSession === session) {
        session.conflicted = Boolean(current && (error.status === 409 || !current.version));
        editorError.textContent = session.conflicted
          ? 'This saved record changed. Your draft is retained. Refresh saved versions and choose an editing base before saving.'
          : `Could not save memory: ${error.message} Your draft is retained.`;
        editorError.hidden = false;
        byId('editor-refresh').hidden = !session.conflicted;
        showNotice(editorError.textContent);
      } else if (!written) {
        // The person left the editor while this save was in flight; still say it failed.
        showNotice(`Could not save “${title || 'the memory'}”: ${error.message}`);
      }
    } finally {
      session.busy = false;
      if (state.editorSession === session) setEditorBusy(false);
    }
  }

  async function togglePin(memory) {
    const request = beginScopedRequest('memory-mutation');
    const editorSession = state.editorSession;
    try {
      await api('/pin', {
        method: 'POST',
        body: { id: memory.id, workspace: request.workspace, pinned: !memory.pinned },
      });
      if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
        || state.selectedMemory !== memory.id) return;
      showNotice(memory.pinned ? 'Memory unpinned.' : 'Memory pinned against decay.');
      await selectWorkspace(request.workspace);
      if (state.workspace === request.workspace && state.refreshEpoch === request.epoch + 1
        && !state.editorSession && !state.selectedMemory) await selectMemory(memory.id);
    } catch (error) {
      if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
        || state.selectedMemory !== memory.id) return;
      showNotice(`Could not change pin: ${error.message}`);
    }
  }

  async function retireMemory(memory) {
    const request = beginScopedRequest('memory-mutation');
    const editorSession = state.editorSession;
    if (!await dialogs.confirm({
      title: 'Retire memory',
      message: `Retire “${memory.title || memory.id}”? The record stays in temporal history but leaves live recall.`,
      confirmLabel: 'Retire memory',
      danger: true,
    })) return;
    if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
      || state.selectedMemory !== memory.id) return;
    try {
      await api('/retire', {
        method: 'POST',
        body: { id: memory.id, workspace: request.workspace, reason: 'retired in Ledger' },
      });
      if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
        || state.selectedMemory !== memory.id) return;
      state.selectedMemory = '';
      byId('memory-detail').replaceChildren(empty('Memory moved out of live recall. Its history is retained.'));
      showNotice('Memory retired without hard deletion.');
      await selectWorkspace(request.workspace);
    } catch (error) {
      if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
        || state.selectedMemory !== memory.id) return;
      showNotice(`Could not retire memory: ${error.message}`);
    }
  }

  async function secureEraseMemory(memory) {
    const request = beginScopedRequest('memory-mutation');
    const editorSession = state.editorSession;
    const name = memory.title || memory.id;
    if (!await dialogs.confirm({
      title: 'Securely erase a leaked secret',
      message: [
        `Securely erase “${name}”? This destroys its temporal history and local index copies and cannot be undone.`,
        'Rotate the leaked credential as well: copied exports, snapshots, remote peers, and an already-compromised agent cannot be erased here.',
      ],
      confirmLabel: 'Securely erase',
      danger: true,
    })) return;
    if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
      || state.selectedMemory !== memory.id) return;
    try {
      /* The confirmation above is the operator attestation the service requires; it
         refuses an erase without `confirmed: true`. */
      const result = await api('/secure-erase', {
        method: 'POST', body: { id: memory.id, workspace: request.workspace, confirmed: true },
      });
      if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
        || state.selectedMemory !== memory.id) return;
      state.selectedMemory = '';
      byId('memory-detail').replaceChildren(empty('Memory securely erased from this local store. Review the reported backup limitations and rotate the credential.'));
      await selectWorkspace(request.workspace);
      if (state.workspace !== request.workspace || state.refreshEpoch !== request.epoch + 1
        || state.editorSession || state.selectedMemory) return;
      showNotice(result.vector_index_cleanup === 'deleted'
        ? 'Memory securely erased from this local store. Rotate the leaked credential too: exports, snapshots and remote peers keep their own copies.'
        : 'Memory removed locally, but the configured vector index needs separate remediation. Rotate the leaked credential too.');
    } catch (error) {
      if (!isCurrentScopedRequest(request) || state.editorSession !== editorSession
        || state.selectedMemory !== memory.id) return;
      showNotice(`Could not securely erase memory: ${error.message}`);
    }
  }

  function openMemoryTimeline(memory) {
    switchView('provenance');
    switchProvenanceTab('timeline');
    byId('timeline-input').value = memory.title || truncate(memory.content, 80);
    byId('timeline-form').requestSubmit();
  }

  const obsidianImport = {
    preview: null, job: null, poll: null, selection: null, sources: [],
    jobWorkspace: '', jobId: '', running: false, reviewGeneration: 0,
    polling: false, pollFailures: 0, reportPage: 0, reportKey: '',
  };
  let documentExtensions = null;

  async function obsidianApi(path, options = {}) {
    const attempt = async () => {
      const csrf = await reviewCsrfToken();
      return api(path, {
        ...options,
        headers: { ...(options.headers || {}), 'X-Engraphis-Review-CSRF': csrf },
      });
    };
    try {
      return await attempt();
    } catch (error) {
      if (error && error.status === 403) {
        // A dashboard restart or re-login invalidates the cached process-local
        // nonce. Refresh it once so the wizard recovers without a page reload.
        state.reviewCsrf = '';
        return attempt();
      }
      throw error;
    }
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
    // Keep monitoring closed imports without rebuilding an invisible report.
    if (!byId('obsidian-import-dialog').open) return;
    const rows = obsidianRows(result).filter(row => {
      const status = String(row.status || row.action || row.result || '').toLowerCase();
      if (wanted === 'all') return true;
      if (wanted === 'reject') return /reject|error|warn|conflict/.test(status) || Boolean(row.warning || row.error);
      return status.includes(wanted);
    });
    const pageSize = 100;
    obsidianImport.reportPage = Math.min(obsidianImport.reportPage, Math.max(0, Math.ceil(rows.length / pageSize) - 1));
    const start = obsidianImport.reportPage * pageSize;
    const page = rows.slice(start, start + pageSize);
    const reportKey = JSON.stringify([wanted, start, rows.length, page]);
    if (reportKey === obsidianImport.reportKey) return;
    obsidianImport.reportKey = reportKey;
    byId('obsidian-report-count').textContent = rows.length ? `${start + 1}–${start + page.length} of ${rows.length} files` : '0 files';
    byId('obsidian-report-previous').disabled = start === 0;
    byId('obsidian-report-next').disabled = start + page.length >= rows.length;
    target.replaceChildren();
    if (!rows.length) {
      target.append(empty(wanted === 'all' ? 'No per-file details were returned.' : 'No files match this filter.'));
      return;
    }
    const list = node('ul');
    page.forEach(row => {
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
    const requiredScopeField = !selection.fields.workspace
      ? ['obsidian-workspace', 'Enter a workspace before previewing this import.']
      : selection.fields.scope === 'repo' && !selection.fields.repo
        ? ['obsidian-repo', 'Repository scope requires a repository name.']
        : selection.fields.scope === 'session' && !selection.fields.session_id
          ? ['obsidian-session', 'Session scope requires a session ID.']
          : null;
    if (requiredScopeField) {
      byId('obsidian-import-progress').textContent = requiredScopeField[1];
      byId(requiredScopeField[0]).focus();
      return;
    }
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
    if (obsidianImport.polling) return;
    obsidianImport.polling = true;
    obsidianImport.jobId = jobId;
    obsidianImport.jobWorkspace = workspace;
    obsidianImport.poll = null;
    try {
      const result = await obsidianApi(`/workspaces/import-documents/jobs/${encodeURIComponent(jobId)}?${query(workspace)}`);
      obsidianImport.job = result;
      obsidianImport.pollFailures = 0;
      byId('obsidian-retry-progress').hidden = true;
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
      if (workspace === state.workspace && !state.editorSession) await selectWorkspace(workspace);
    } catch (error) {
      obsidianImport.pollFailures += 1;
      const retrying = obsidianImport.pollFailures <= 3;
      byId('obsidian-import-progress').textContent = `Could not read import progress: ${error.message}. ${retrying ? 'Reconnecting to this job…' : 'Retry progress to reconnect to this job.'}`;
      byId('obsidian-run').disabled = true;
      byId('obsidian-retry-progress').hidden = false;
      if (retrying) obsidianImport.poll = window.setTimeout(() => pollObsidianImport(jobId, workspace), Math.min(6000, 750 * 2 ** obsidianImport.pollFailures));
    } finally {
      obsidianImport.polling = false;
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
    obsidianImport.pollFailures = 0;
    byId('obsidian-retry-progress').hidden = true;
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
        if (workspace === state.workspace && !state.editorSession) await selectWorkspace(workspace);
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
      obsidianImport.jobId = '';
      obsidianImport.pollFailures = 0;
      byId('obsidian-retry-progress').hidden = true;
      delete byId('obsidian-cancel').dataset.jobId;
      delete byId('obsidian-cancel').dataset.workspace;
    }
    byId('obsidian-workspace').value = state.workspace;
    byId('obsidian-repo').value = state.project;
    byId('obsidian-scope').value = state.project ? 'repo' : 'workspace';
    byId('obsidian-session').value = '';
    byId('obsidian-vault-label').value = '';
    if (!obsidianImport.running) {
      byId('obsidian-import-progress').textContent = 'Choose individual files or a folder to preview its import.';
    }
    byId('obsidian-run').disabled = true;
    byId('obsidian-preview').disabled = obsidianImport.running;
    byId('obsidian-cancel').hidden = !obsidianImport.running;
    await Promise.all([loadObsidianVaults(), loadDocumentFormats()]);
    byId('obsidian-vault-id').value = '';
    updateSourceLabelRequirement();
    updateDocumentImportMode();
    dialog.showModal();
    obsidianImport.reportKey = '';
    if (!obsidianImport.running) obsidianImport.reportPage = 0;
    renderObsidianReport(obsidianImport.job || obsidianImport.preview);
    byId('obsidian-import-files').focus();
  }

  function renderAnswer(result) {
    const target = byId('answer-panel');
    target.replaceChildren();
    const meta = node('div', 'answer-meta');
    const grounded = Boolean(result.grounded);
    meta.append(
      node('span', `support-pill ${grounded ? 'grounded' : 'abstained'}`, grounded ? 'Grounded' : 'Abstained'),
      node('span', 'support-pill', Number.isFinite(result.support) ? `Support ${result.support.toFixed(2)}` : 'Support unavailable'),
      node('span', 'support-pill', `${(result.citations || []).length} ${(result.citations || []).length === 1 ? 'citation' : 'citations'}`),
    );
    target.append(meta);
    const advisory = result.planning_advisory;
    if (advisory && typeof advisory === 'object') {
      const reasonLabels = {
        route_selected: 'Jev prioritized a locally generated alternate route. A 40-task synthetic check found no retrieval-metric change; user-workload benefit is unproven.',
        jev_uncertain: 'Jev was uncertain, so the deterministic route order was used.',
        remote_consent_required: 'Remote consent was not granted, so deterministic route order was used.',
        planning_disabled: 'Query planning is disabled, so the original query route was used.',
        backend_unavailable: 'Jev is unavailable, so deterministic route order was used.',
        allowance_exhausted: 'The included Jev allowance is currently used up, so deterministic route order was used. Check account usage for the next available window.',
        provider_protection_limit: 'Jev is temporarily paused by a service-protection limit, so deterministic route order was used. Try again later.',
        remote_timeout: 'Jev did not respond before the request deadline, so deterministic route order was used. You can retry later.',
        session_changed: 'The Cloud session changed during the request, so deterministic route order was used. Sign in again before retrying.',
        managed_operation_unsupported: 'This Jev operation is unavailable in the managed service, so deterministic route order was used.',
        planner_timeout: 'Route planning reached its deadline, so deterministic route order was used.',
        remote_unavailable: 'The Jev request failed, so deterministic route order was used.',
        malformed_response: 'Jev returned an invalid response, so deterministic route order was used.',
        sensitive_content: 'The route text was not sent because it matched a sensitive-data filter; deterministic route order was used.',
      };
      const message = reasonLabels[advisory.reason]
        || (advisory.status === 'decision'
          ? 'Jev prioritized a locally generated route. A 40-task synthetic check found no retrieval-metric change; user-workload benefit is unproven.'
          : 'The Jev advisory did not select a route; deterministic route order was used.');
      target.append(node('p', 'project-help jev-recall-status', message));
    }
    const coverage = ['unknown', 'partial', 'complete'].includes(result.answer_coverage) ? result.answer_coverage : 'unknown';
    const coverageNote = {
      unknown: 'This answer has not been checked against every part of your question.',
      partial: 'The answering service reports that some requested information is missing.',
      complete: 'The answering service reports that every part of the question is covered.',
    };
    const coveragePanel = node('div', 'answer-coverage');
    coveragePanel.dataset.coverage = coverage;
    coveragePanel.append(node('strong', '', 'Question coverage: ' + coverage),
      node('p', '', coverageNote[coverage] + ' Support and citations describe the returned evidence.'));
    target.append(coveragePanel);
    if (!grounded) {
      target.append(
        node('h2', '', 'Insufficient evidence'),
        node('p', 'answer-copy', result.reason || 'The active workspace does not support a grounded answer.'),
      );
      return;
    }
    target.append(node('h2', '', 'Answer'), node('p', 'answer-copy', result.answer || 'The cited memories support this answer.'));
    const citations = node('div', 'citation-list');
    (result.citations || []).forEach(citation => {
      const card = node(citation.id ? 'button' : 'article', 'citation-card memory-link-card');
      if (citation.id) {
        card.type = 'button';
        card.dataset.memoryId = citation.id;
        card.addEventListener('click', () => openMemory(citation));
      }
      card.append(
        node('h3', '', `[${citation.n || citation.number || '•'}] ${memoryTitle(citation)}`),
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
    const question = input.value;
    if (!question.trim()) {
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
    const jevAssisted = byId('ask-jev-assisted').checked;
    const allowRemote = jevAssisted && byId('ask-jev-remote').checked;
    const body = {
      query: question,
      workspace,
      ...(request.project ? { repo: request.project } : {}),
      k: Math.max(8, k),
      max_citations: k,
      planning: jevAssisted ? 'auto' : 'off',
      jev_assisted: jevAssisted,
      allow_remote: allowRemote,
      include_retrieval_preview: true,
      ...(jevAssisted ? { data_classification: byId('ask-jev-classification').value } : {}),
    };
    await askRequests.start({
      question,
      scopeLabel: workspace + (request.project ? ' / ' + request.project : ' / all projects'),
      isCurrent: () => isCurrentScopedRequest(request),
      answer: signal => api('/answer', {
        method: 'POST', signal, body,
      }),
    });
  }

  function retrievalScore(label, value, { percent = false, max = 1 } = {}) {
    const score = node('div', 'retrieval-score');
    const numeric = Number(value);
    score.append(node('span', '', label));
    if (value == null || value === '' || !Number.isFinite(numeric)) {
      score.append(node('strong', '', '—'));
      return score;
    }
    const bounded = Math.max(0, Math.min(max, numeric));
    const bar = document.createElement('progress');
    bar.max = max;
    bar.value = bounded;
    bar.setAttribute('aria-hidden', 'true');
    score.append(node('strong', '', percent ? `${Math.round(bounded / max * 100)}%` : numeric.toFixed(2)), bar);
    return score;
  }

  /* Rank is relative to this answer's candidates; support is the absolute overlap that the
     grounding gate uses; retention is how strongly the record is currently held. */
  function renderPreview(retrieval) {
    const target = byId('retrieval-list');
    target.replaceChildren();
    const memories = retrieval.memories || [];
    if (!memories.length) {
      target.append(empty('No raw candidates were returned.'));
      return;
    }
    const rankOf = memory => Number(memory.relative_score != null ? memory.relative_score : memory.score);
    const top = Math.max(0, ...memories.map(rankOf).filter(Number.isFinite));
    memories.forEach((memory, index) => {
      const card = simpleMemoryCard(memory);
      const scores = node('div', 'retrieval-scores');
      scores.append(
        retrievalScore(`Rank #${index + 1}`, rankOf(memory), { max: top > 0 ? top : 1 }),
        retrievalScore('Support', memory.absolute_support),
        retrievalScore('Retention', memory.retention, { percent: true }),
      );
      card.append(scores);
      if (memory.arm) card.append(node('p', 'project-help', `Found by ${text(memory.arm).replaceAll('_', ' ')} retrieval`));
      target.append(card);
    });
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
      evidence_mass: optionalGraphNumber(item.evidence_mass ?? item.gravity_mass ?? item.mass_score),
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

  function graphEndpoint(value) {
    if (value && typeof value === 'object') return value.id ?? value;
    return value;
  }

  function graphLinks(payload) {
    const source = payload.edges || payload.links || [];
    return source.map((item, index) => ({
      ...item,
      id: item.id || `edge-${index}`,
      source: item.from ?? graphEndpoint(item.source),
      target: item.to ?? graphEndpoint(item.target),
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
    })).filter(item => item.source !== undefined && item.source !== null
      && item.target !== undefined && item.target !== null);
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

  function focusGraphNode(id, label = 'Selected entity') {
    const engine = state.graphEngine;
    if (!engine) return;
    let attempted = false;
    const focus = () => {
      if (state.graphEngine !== engine || attempted) return;
      attempted = true;
      if (typeof engine.focus === 'function' && engine.focus(id)) return;
      if (typeof engine.reveal === 'function' && engine.reveal(id)) return;
      showNotice(`${label} is outside the current graph scope.`);
    };
    focus();
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
      node('h4', '', memoryTitle(memory)),
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
    if (!item || item.id === undefined || item.id === null || !state.workspace) return;
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
      if (detail.truncation && detail.truncation.evidence) {
        byId('graph-connection-memory-title').textContent += ' · bounded evidence sample';
      }
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
    if (!item || item.id === undefined || item.id === null) return;
    cancelGraphConnectionMemoryLoad();
    state.graphConnectionsFocusId = String(item.id);
    state.graphConnectionsFocusLabel = item.name || item.label || item.id;
    const focusButton = byId('graph-connections-focus');
    if (focusButton) focusButton.hidden = !state.graphEngine;
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
    const repoFilter = byId('graph-repo-filter');
    const repoLabel = document.querySelector('label[for="graph-repo-filter"]');
    if (repoFilter) {
      repoFilter.placeholder = full
        ? 'Filter by exact repository name…'
        : 'Filter to a repository or topic…';
      repoFilter.title = full
        ? 'All Nodes accepts an exact repository name from this workspace.'
        : '';
    }
    if (repoLabel) repoLabel.textContent = full
      ? 'Filter by exact repository name'
      : 'Filter to a repository or topic';
    ['graph-min-degree', 'graph-collapse', 'graph-depth',
      'graph-show-unlinked', 'graph-flow', 'graph-flow-speed'].forEach(id => {
      const control = byId(id);
      if (control) { control.disabled = false; control.title = ''; }
    });
    all('[data-graph-layer="code"]').forEach(control => {
      control.disabled = false;
      control.title = full
        ? 'Choose an exact repository first, then add its code overlay within the All Nodes capacity.'
        : '';
    });
    /* Focus-depth and auto-collapse are not implemented in the Every-node engine yet.
       Disable them honestly instead of leaving controls that silently do nothing. */
    if (full && byId('graph-preset').value === 'every') {
      ['graph-collapse', 'graph-depth'].forEach(id => {
        const control = byId(id);
        if (!control) return;
        control.disabled = true;
        control.title = 'Not yet available in the Every-node view.';
      });
    }
    const lodNote = byId('graph-lod-note');
    if (lodNote) lodNote.hidden = !full;
    byId('graph-reheat').textContent = full ? 'Reflow layout' : 'Reheat layout';
    const freezeRow = byId('graph-freeze-row');
    if (freezeRow) freezeRow.hidden = full;
    const orbitPause = byId('graph-orbit-pause-row');
    const galaxy = graphIsGalaxy();
    const orbitCapable = galaxy && (!full || state.graphGalaxyQuality);
    if (orbitPause) orbitPause.hidden = !orbitCapable;
    const style = byId('graph-style').value;
    const styleNotes = full ? GRAPH_LOD_STYLE_NOTES : GRAPH_STYLE_NOTES;
    byId('graph-style-note').textContent = styleNotes[style] || styleNotes.classic;
    updateGraphGalaxyControls();
    const preset = GRAPH_PRESET_LABELS[byId('graph-preset').value] || 'Galaxy gravity';
    byId('graph-mode').textContent = `${full ? 'All nodes · LOD' : 'High quality'} · ${preset}`;
  }

  function graphIsGalaxy() {
    return byId('graph-preset').value === 'galaxy';
  }

  function graphOverlayEnabled(galaxyQuality = state.graphGalaxyQuality) {
    // Every can use the authored Galaxy renderer even though its toolbar preset is 'every'.
    return graphIsGalaxy() || (galaxyQuality && byId('graph-preset').value === 'every');
  }

  function graphSizeBy() {
    return graphIsGalaxy() && state.graphMode !== 'full'
      ? 'evidence_mass' : byId('graph-size').value;
  }

  function updateGraphGalaxyControls() {
    const galaxy = graphIsGalaxy();
    const full = state.graphMode === 'full';
    const orbitCapable = galaxy && (!full || state.graphGalaxyQuality);
    const size = byId('graph-size');
    if (galaxy && !full) {
      if (['degree', 'betweenness'].includes(size.value)) size.dataset.legacyValue = size.value;
      size.value = 'evidence_mass';
      size.disabled = true;
      size.title = 'Galaxy gravity sizes stars by evidence mass.';
    } else {
      size.disabled = false;
      size.title = '';
      if (size.value === 'evidence_mass') size.value = size.dataset.legacyValue || 'degree';
    }
    const labels = full
      ? ['Repel force', 'Link distance', 'Centre gravity']
      : galaxy
      ? ['Orbital speed', 'Link distance · tight ↔ loose', 'Galactic gravity · loose ↔ tight']
      : ['Repel force', 'Link distance', 'Centre gravity'];
    ['graph-repel-label', 'graph-link-label', 'graph-gravity-label'].forEach((id, index) => {
      const label = byId(id);
      if (label) label.textContent = labels[index];
    });
    /* Keep the shared spacetime controls available for every renderer. Galaxy-specific rows
       are gated below, while the normalized force controls remain useful to compact and
       community layouts as well. */
    byId('graph-spacetime-tuning').hidden = false;
    const forceLabels = full
      ? ['Core attraction', 'Core mass', 'Cluster cohesion', 'Settling resistance', 'Link spring']
      : ['Galactic gravity', 'Black hole mass', 'Local solar gravity', 'Space friction', 'Spring stiffness'];
    ['graph-gravitational-constant-label', 'graph-black-hole-mass-label',
      'graph-local-gravitational-constant-label', 'graph-space-damping-label',
'graph-spring-stiffness-label'].forEach((id, index) => {
      const label = byId(id);
      if (label) label.textContent = forceLabels[index];
    });
    const springLabel = byId('graph-spring-stiffness-label');
    const springCapable = galaxy || (full && !state.graphGalaxyQuality);
    if (springLabel && springLabel.parentElement) {
      springLabel.parentElement.hidden = !springCapable;
    }
    const galaxyRenderer = galaxy && (!full || state.graphGalaxyQuality);
    const everyRenderer = full && !state.graphGalaxyQuality;
    byId('graph-spacetime-summary').textContent = galaxyRenderer
      ? 'Spacetime · black-hole orbit controls'
      : everyRenderer ? 'All-node force refinement' : 'Responsive force controls';
    byId('graph-spacetime-note').textContent = galaxyRenderer
      ? 'Drag and release a node to slingshot it into a new orbit.'
      : everyRenderer
        ? 'These values refine the settled worker layout.'
        : 'These values tune the responsive force layout.';
    byId('graph-orbits-pause-label').textContent = 'Pause orbits';
    byId('graph-orbits-pause-detail').textContent = 'physics';
    byId('graph-orbits-pause').setAttribute('aria-label', 'Pause orbital physics');
    const orbitPauseRow = byId('graph-orbit-pause-row');
    if (orbitPauseRow) orbitPauseRow.hidden = !orbitCapable;
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
    const styleNotes = state.graphMode === 'full' ? GRAPH_LOD_STYLE_NOTES : GRAPH_STYLE_NOTES;
    byId('graph-style-note').textContent = styleNotes[style] || styleNotes.classic;
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
    if (!control) return safe;
    const min = Number(control.min);
    const max = Number(control.max);
    return Math.min(Number.isFinite(max) ? max : safe, Math.max(Number.isFinite(min) ? min : safe, safe));
  }
  /* Controls keep their human-readable ranges and defaults. The engine value is computed by
     a clamped identity 1:1 mapping: the slider's raw position flows straight to the engine,
     bounded by the HTML min/max. The earlier 2x response saturated against the HTML bounds
     for slider values near the loose and tight ends, producing visible plateaus where the
     user dragged the slider but the engine value didn't change. Identity 1:1 with
     [min, max] clamping gives every integer tick in the slider's full HTML range a
     strictly distinct engine value. */
  function graphSliderResponseBaseline(item) {
    if (!item) return 0;
    if (item.id === 'graph-flow-speed') return 45;
    const preset = byId('graph-preset');
    const tuning = preset ? graphPresetTuning(preset.value) : null;
    const candidate = tuning && tuning[item.key];
    return Number.isFinite(Number(candidate)) ? Number(candidate) : item.fallback;
  }
  function graphSliderResponseValue(id, value, baseline) {
    const control = byId(id);
    if (!control) return Number.isFinite(Number(value)) ? Number(value) : baseline;
    /* Flow speed is a linear control: the 2x response centered at 45 would
       clamp every visible value in the lower quarter of the slider to
       0, which the compat engine then treats as "stop" and the user
       sees an inert slider end. Use the raw value for flow-speed and
       keep the 2x response for the geometry controls (gravity, repel,
       link, etc.) where the centered calibration is intentional. */
    if (id === 'graph-flow-speed') {
      const rawFlow = graphValueInRange(id, value, baseline);
      return Number.isFinite(Number(rawFlow)) ? Number(rawFlow) : baseline;
    }
    const raw = graphValueInRange(id, value, baseline);
    const min = Number(control.min);
    const max = Number(control.max);
    return Math.min(Number.isFinite(max) ? max : raw,
      Math.max(Number.isFinite(min) ? min : raw, raw));
  }
  function graphSliderInputValue(id, value, baseline) {
    const control = byId(id);
    if (!control) return Number.isFinite(Number(value)) ? Number(value) : baseline;
    const min = Number(control.min);
    const max = Number(control.max);
    const safe = graphValueInRange(id, value, baseline);
    /* Identity inverse of the clamped identity response. */
    return Math.min(Number.isFinite(max) ? max : safe,
      Math.max(Number.isFinite(min) ? min : safe, safe));
  }

  function graphScopeValue(id, value, fallback) {
    const control = byId(id);
    const raw = Number(value);
    const safe = Number.isFinite(raw) ? raw : fallback;
    if (!control) return Math.round(safe);
    const min = Number(control.min);
    const max = Number(control.max);
    return Math.round(Math.min(Number.isFinite(max) ? max : safe,
      Math.max(Number.isFinite(min) ? min : safe, safe)));
  }


  function graphTuningEngineSettings() {
    return GRAPH_TUNING.reduce((settings, item) => {
      const raw = number(byId(item.id).value);
      settings[item.key] = graphSliderResponseValue(
        item.id, raw, graphSliderResponseBaseline(item),
      );
      return settings;
    }, {
      flowSpeed: graphSliderResponseValue(
        'graph-flow-speed', number(byId('graph-flow-speed').value), 45,
      ),
    });
  }

  function graphSpacetimeEngineSettings() {
    const controls = GRAPH_SPACETIME_TUNING.reduce((settings, item) => {
      const raw = number(byId(item.id).value);
      settings[item.key] = graphSliderResponseValue(item.id, raw, item.fallback);
      return settings;
    }, {});
    return {
      gravitationalConstant: controls.gravitationalConstant / 30,
      blackHoleMass: graphBlackHoleMassMultiplier(controls.blackHoleMass),
      localGravitationalConstant: controls.localGravitationalConstant / 30,
      damping: controls.damping,
      springStiffness: controls.springStiffness / 32,
      orbitPaused: state.graphOrbitPaused,
    };
  }

  function graphScopeEngine() {
    return {
      minDegree: graphScopeValue('graph-min-degree', byId('graph-min-degree').value, 1),
      showUnlinked: state.graphShowUnlinked,
      depth: graphScopeValue('graph-depth', byId('graph-depth').value, 2),
    };
  }


  let graphPreferencesSaveScheduled = false;
  let graphPreferencesSaveCancel = null;
  function scheduleGraphPreferencesSave() {
    if (graphPreferencesSaveScheduled) return;
    graphPreferencesSaveScheduled = true;
    const flush = () => {
      if (!graphPreferencesSaveScheduled) return;
      graphPreferencesSaveScheduled = false;
      graphPreferencesSaveCancel = null;
      saveGraphPreferences();
    };
    if (typeof requestAnimationFrame === 'function') {
      const frame = requestAnimationFrame(flush);
      graphPreferencesSaveCancel = () => cancelAnimationFrame(frame);
    } else {
      const timer = setTimeout(flush, 0);
      graphPreferencesSaveCancel = () => clearTimeout(timer);
    }
  }

  function flushGraphPreferencesSave() {
    if (!graphPreferencesSaveScheduled) return;
    graphPreferencesSaveScheduled = false;
    const cancel = graphPreferencesSaveCancel;
    graphPreferencesSaveCancel = null;
    if (cancel) cancel();
    saveGraphPreferences();
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


  const GRAPH_BLACK_HOLE_MASS_BASELINE = 96;
  function graphBlackHoleMassMultiplier(controlValue) {
    const value = number(controlValue);
    /* Below the 96 baseline the multiplier scales linearly. Above it, every +10 slider units
       adds exactly +0.10 to the compact central-mass multiplier: 96→1.0, 106→1.1, 116→1.2.
       Local stellar wells remain owned exclusively by Local solar gravity. */
    return value <= GRAPH_BLACK_HOLE_MASS_BASELINE
      ? Math.max(0, value / GRAPH_BLACK_HOLE_MASS_BASELINE)
      : 1 + (value - GRAPH_BLACK_HOLE_MASS_BASELINE) / 100;
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
    return graphScopeEngine();
  }

  function applyGraphScope() {
    if (state.graphEngine) state.graphEngine.setScope(graphScope());
  }

  function setGraphMinDegree(value, apply = true) {
    const next = graphScopeValue('graph-min-degree', value, 1);
    byId('graph-min-degree').value = String(next);
    byId('graph-min-degree-output').value = String(Math.round(next));
    byId('graph-min-degree-output').textContent = String(Math.round(next));
    if (apply) applyGraphScope();
  }

  function setGraphDepth(value, apply = true) {
    const next = graphScopeValue('graph-depth', value, 2);
    byId('graph-depth').value = String(next);
    byId('graph-depth-output').value = String(Math.round(next));
    byId('graph-depth-output').textContent = String(Math.round(next));
    if (apply) applyGraphScope();
  }

  function setGraphShowUnlinked(on, apply = true) {
    const next = on === true;
    state.graphShowUnlinked = next;
    const control = byId('graph-show-unlinked');
    control.textContent = 'Unlinked nodes';
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
    const layers = graphLayerState();
    return {
      physicsVersion: GRAPH_PHYSICS_VERSION,
      preset: byId('graph-preset').value,
      style: byId('graph-style').value,
      color: byId('graph-color').value,
      palette: byId('graph-palette').value,
      typeColors: { ...state.graphTypeColors },
      flow: byId('graph-flow').getAttribute('aria-checked') === 'true',
      labels: byId('graph-labels').getAttribute('aria-checked') === 'true',
      tuning: graphTuningSettings(),
      /* Pause is a session action, like Freeze. Persist the numeric spacetime tuning without
         silently reopening a future dashboard with every orbit stopped. */
      spacetimeTuning: GRAPH_SPACETIME_TUNING.reduce((settings, item) => {
        settings[item.key] = number(byId(item.id).value);
        return settings;
      }, {}),
      minDegree: number(byId('graph-min-degree').value),
      depth: number(byId('graph-depth').value),
      showUnlinked: state.graphShowUnlinked,
      layers,
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
    const savedTypeColors = graphPreference('typeColors', {});
    state.graphTypeColors = {};
    if (savedTypeColors && typeof savedTypeColors === 'object') {
      Object.entries(savedTypeColors).slice(0, 40).forEach(([type, value]) => {
        if (type.length <= 80 && validGraphColor(value)) state.graphTypeColors[type] = value.toLowerCase();
      });
    }

    const savedTuning = graphPreference('tuning', {});
    const savedPhysicsVersion = Number(graphPreference('physicsVersion', 0));
    const sourcePhysicsVersion = Number.isFinite(savedPhysicsVersion) ? savedPhysicsVersion : 0;
    const needsPhysicsMigration = version => hasSavedPreferences
      && sourcePhysicsVersion < version;
    const legacyPhysics = needsPhysicsMigration(GRAPH_PHYSICS_VERSION);
    const needsPhysicsV3Migration = needsPhysicsMigration(3);
    const needsPhysicsV4Migration = needsPhysicsMigration(4);
    const effectiveTuning = savedTuning && typeof savedTuning === 'object'
      ? { ...savedTuning } : {};
    const savedSpacetimeTuning = graphPreference('spacetimeTuning', {});
    /* A failed physics-control experiment could persist every attractive force at its maximum,
       friction at zero, and the Galaxy spacing control at 400. That exact vector is not a
       useful custom preset: it collapses the visible graph and can reduce hundreds of loaded
       entities to a small central knot. Physics v3 resets only this known-bad snapshot. */
    const staleMaxedPhysics = needsPhysicsV3Migration && Number(effectiveTuning.gravity) === 400
      && Number(savedSpacetimeTuning && savedSpacetimeTuning.gravitationalConstant) === 200
      && Number(savedSpacetimeTuning && savedSpacetimeTuning.blackHoleMass) === 500
      && Number(savedSpacetimeTuning && savedSpacetimeTuning.localGravitationalConstant) === 200
      && Number(savedSpacetimeTuning && savedSpacetimeTuning.damping) === 0
      && Number(savedSpacetimeTuning && savedSpacetimeTuning.springStiffness) === 100;
    if (staleMaxedPhysics) {
      delete effectiveTuning.repel;
      delete effectiveTuning.link;
      delete effectiveTuning.gravity;
    }
    /* Older preferences persisted 48 and then 60 as Galaxy's default orbital speed. Physics v4
       defines the control as a percentage with 100 as neutral, so migrate only those exact
       retired defaults. Every other custom speed and every unrelated preference remains intact. */
    if (needsPhysicsV4Migration && preset === 'galaxy'
      && [48, 60].includes(Number(effectiveTuning.repel))) {
      effectiveTuning.repel = 100;
    }
    /* Physics v5 made 120 the Galaxy gravity default. Migrate only the exact retired default;
       a saved 96 in an already-versioned v5 snapshot remains an intentional user choice. */
    if (needsPhysicsMigration(5) && preset === 'galaxy' && Number(effectiveTuning.gravity) === 96) {
      effectiveTuning.gravity = 120;
    }
    /* Physics v6 recalibrates Galaxy gravity to 72 and the spacetime panel to 60/96/60. Move
       only the exact retired default vector, so a value the person actually chose survives. */
    if (needsPhysicsMigration(6)) {
      if (preset === 'galaxy' && Number(effectiveTuning.gravity) === 120) {
        effectiveTuning.gravity = 72;
      }
      const retiredSpacetimeDefaults = {
        gravitationalConstant: 100, blackHoleMass: 160, localGravitationalConstant: 100,
      };
      const savedSpacetime = savedSpacetimeTuning && typeof savedSpacetimeTuning === 'object'
        ? savedSpacetimeTuning : {};
      const retiredKeys = Object.keys(retiredSpacetimeDefaults);
      const hasRetiredVector = retiredKeys.every(key =>
        Number(savedSpacetime[key]) === retiredSpacetimeDefaults[key]);
      if (hasRetiredVector) {
        retiredKeys.forEach(key => {
          savedSpacetime[key] = GRAPH_SPACETIME_TUNING.find(item => item.key === key).fallback;
        });
      }
    }
    syncGraphTuning({
      ...graphPresetTuning(preset),
      ...effectiveTuning,
    });
    /* Pause orbits is deliberately session-only. Old snapshots may contain orbitPaused=true;
       ignore it so a fresh dashboard always starts with live galactic motion. */
    state.graphOrbitPaused = false;
    syncGraphSpacetimeTuning({
      ...(!staleMaxedPhysics && savedSpacetimeTuning
        && typeof savedSpacetimeTuning === 'object'
        ? savedSpacetimeTuning : {}),
      orbitPaused: false,
    });

    const savedMin = Number(graphPreference('minDegree', number(byId('graph-min-degree').value)));
    const minDegree = Number.isFinite(savedMin) ? Math.max(0, Math.min(12, Math.round(savedMin))) : 1;
    setGraphMinDegree(minDegree);
    setGraphDepth(graphPreference('depth', 2));
    const savedRepo = graphPreference('repoFilter', '');
    byId('graph-repo-filter').value = typeof savedRepo === 'string' ? savedRepo.slice(0, 200) : '';
    const savedAsOf = graphPreference('asOf', '');
    byId('graph-as-of').value = typeof savedAsOf === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(savedAsOf)
      ? savedAsOf : '';
    setGraphShowUnlinked(staleMaxedPhysics
      || graphPreference('showUnlinked', state.graphShowUnlinked) === true);
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
    state.graphSavedView = graphPreference('savedView', '', ['', ...Object.keys(GRAPH_SAVED_VIEWS)]);
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
    if (['degree', 'betweenness', 'evidence_mass'].includes(view.size)) byId('graph-size').value = view.size;
    if (typeof view.bridges === 'boolean') byId('graph-bridges').checked = view.bridges;
    if (typeof view.collapse === 'boolean') byId('graph-collapse').checked = view.collapse;
    if (typeof view.flow === 'boolean') setGraphSwitch('graph-flow', view.flow);
    if (typeof view.labels === 'boolean') setGraphSwitch('graph-labels', view.labels);
    setGraphSwitch('graph-freeze', state.graphFrozen);
    syncGraphTuning({
      ...graphPresetTuning(preset),
      ...(view.tuning && typeof view.tuning === 'object' ? view.tuning : {}),
    });
    syncGraphSpacetimeTuning(
      view.spacetimeTuning && typeof view.spacetimeTuning === 'object'
        ? view.spacetimeTuning : {}
    );
    setGraphMinDegree(view.minDegree == null ? 1 : view.minDegree, false);
    setGraphDepth(view.depth == null ? 2 : view.depth, false);
    setGraphShowUnlinked(view.showUnlinked === true, false);
    setGraphLayers(view.layers);
    state.graphSavedView = id === 'custom' ? '' : id;
    syncGraphChoices();
    renderGraphTypeColors();
    renderGraphLegend();
    if (state.graphEngine) {
      state.graphEngine.apply(graph => {
        graph.setPreset(preset);
        graph.setStyle(style);
        graph.setColorBy(color);
        applyGraphPalette(palette);
        graph.setSettings({
          ...graphTuningEngineSettings(),
          ...graphSpacetimeEngineSettings(),
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
    graphLifecycle.sync();
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
        graph.setSettings({
          ...graphTuningEngineSettings(),
          ...graphSpacetimeEngineSettings(),
          frozen: state.graphFrozen,
        });
        graph.setScope(graphScope());
        graph.setLayers(graphLayerState());
      }, false, !state.graphFrozen);
      state.graphEngine.freeze(state.graphFrozen);
    }
    saveGraphPreferences();
    if (previousIncludeCode || previousShowUnlinked) loadGraph({ force: true });
    showNotice('Graph tuning reset to the selected layout defaults.');
  }

  const GRAPH_TYPE_LABELS = {
    person_or_concept: 'Person or concept', mention: 'Mention', hashtag: 'Hashtag', email: 'Email',
    organization: 'Organization', location: 'Location', memory: 'Memory', repo: 'Repository', file: 'File',
  };
  const validGraphColor = value => /^#[0-9a-f]{6}$/i.test(String(value || ''));
  const graphTypeLabel = type => GRAPH_TYPE_LABELS[type] || text(type || 'entity').replace(/_/g, ' ');

  function customGraphColors() {
    return { ...GRAPH_CUSTOM_PALETTE, ...state.graphTypeColors };
  }

  function applyGraphPalette(name) {
    const graph = state.graphEngine;
    if (graph) {
      graph.setPalette(name);
      if (name === 'custom') graph.setTypeColors(customGraphColors());
    }
    renderGraphTypeColors();
    renderGraphLegend();
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';
  function legendSwatch(color) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 10 10');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const dot = document.createElementNS(SVG_NS, 'circle');
    dot.setAttribute('cx', '5');
    dot.setAttribute('cy', '5');
    dot.setAttribute('r', '4.5');
    dot.setAttribute('fill', text(color) || '#888888');
    svg.append(dot);
    return svg;
  }

  /* The canvas cannot explain its own colours, so the engine reports what each colour
     means and the legend is drawn from that report (SVG fills, no inline styles). */
  function renderGraphLegend() {
    const section = byId('graph-legend');
    if (!section) return;
    const engine = state.graphEngine;
    const legend = engine && typeof engine.legend === 'function' ? engine.legend(8) : null;
    const items = legend && Array.isArray(legend.items) ? legend.items : [];
    if (!legend || (legend.mode !== 'connections' && !items.length)) {
      section.hidden = true;
      return;
    }
    const titles = { community: 'Clusters', type: 'Entity types', connections: 'Connections' };
    byId('graph-legend-title').textContent = titles[legend.mode] || 'Colours';
    const list = byId('graph-legend-list');
    const note = byId('graph-legend-note');
    list.replaceChildren();
    note.textContent = '';
    if (legend.mode === 'connections') {
      const colors = Array.isArray(legend.scale) ? legend.scale : [];
      const row = node('li', 'graph-legend-scale');
      const svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('viewBox', `0 0 ${Math.max(1, colors.length)} 1`);
      svg.setAttribute('preserveAspectRatio', 'none');
      svg.setAttribute('aria-hidden', 'true');
      svg.setAttribute('focusable', 'false');
      colors.forEach((color, index) => {
        const band = document.createElementNS(SVG_NS, 'rect');
        band.setAttribute('x', String(index));
        band.setAttribute('y', '0');
        band.setAttribute('width', '1');
        band.setAttribute('height', '1');
        band.setAttribute('fill', text(color));
        svg.append(band);
      });
      const ends = node('div', 'graph-legend-ends');
      ends.append(node('span', '', legend.first || 'Most connected'), node('span', '', legend.last || 'Least connected'));
      row.append(svg, ends);
      list.append(row);
    } else {
      items.forEach(entry => {
        const row = node('li');
        row.append(legendSwatch(entry.color),
          node('span', '', legend.mode === 'type' ? graphTypeLabel(entry.key) : text(entry.label)),
          node('small', '', number(entry.count).toLocaleString()));
        list.append(row);
      });
      const hidden = number(legend.total) - items.length;
      if (hidden > 0) note.textContent = `${hidden.toLocaleString()} more ${legend.mode === 'type' ? 'types' : 'clusters'} not shown`;
    }
    section.hidden = false;
  }

  function graphLegendColor(type) {
    const engine = state.graphEngine;
    const legend = engine && typeof engine.legend === 'function' ? engine.legend(24) : null;
    const entry = legend && legend.mode === 'type' && Array.isArray(legend.items)
      ? legend.items.find(item => item.key === type) : null;
    if (entry && validGraphColor(entry.color)) return entry.color.toLowerCase();
    const custom = customGraphColors()[type];
    return validGraphColor(custom) ? custom.toLowerCase() : '#8c83e8';
  }

  /* Per-type colour pickers. They appear in Entity type mode; editing one switches the
     palette to Custom so the change persists alongside the other saved graph settings. */
  function renderGraphTypeColors() {
    const target = byId('graph-type-colors');
    if (!target) return;
    target.replaceChildren();
    if (byId('graph-color').value !== 'type') return;
    const counts = new Map();
    ((state.graphData && state.graphData.nodes) || []).forEach(item => {
      counts.set(item.etype, (counts.get(item.etype) || 0) + 1);
    });
    const types = counts.size
      ? [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([type]) => type).slice(0, 16)
      : Object.keys(GRAPH_TYPE_LABELS).slice(0, 6);
    types.forEach(type => {
      const label = node('label', 'graph-type-color');
      const input = node('input');
      input.type = 'color';
      input.value = graphLegendColor(type);
      input.dataset.nodeType = type;
      input.setAttribute('aria-label', `Colour for ${graphTypeLabel(type)} nodes`);
      input.addEventListener('input', () => setGraphTypeColor(type, input.value, false));
      input.addEventListener('change', () => setGraphTypeColor(type, input.value, true));
      label.append(input, node('span', '', graphTypeLabel(type)));
      target.append(label);
    });
  }

  function setGraphTypeColor(type, color, persist) {
    if (!type || !validGraphColor(color)) return;
    if (byId('graph-palette').value !== 'custom') {
      byId('graph-palette').value = 'custom';
      if (state.graphEngine) {
        state.graphEngine.setPalette('custom');
        state.graphEngine.setTypeColors(customGraphColors());
      }
    }
    state.graphTypeColors = { ...state.graphTypeColors, [type]: color.toLowerCase() };
    if (state.graphEngine) state.graphEngine.setTypeColor(type, color.toLowerCase());
    syncGraphChoices();
    renderGraphLegend();
    if (persist) {
      clearGraphSavedView();
      saveGraphPreferences();
    }
  }

  function resetGraphColors() {
    state.graphTypeColors = {};
    byId('graph-palette').value = 'theme';
    applyGraphPalette('theme');
    clearGraphSavedView();
    syncGraphChoices();
    saveGraphPreferences();
    showNotice('Graph colours reset to the active theme.');
  }

  function graphThemeColors() {
    const css = getComputedStyle(document.body);
    const colors = {
      accent: css.getPropertyValue('--c-acc').trim() || '#a39bf1',
      surface: css.getPropertyValue('--c-surface').trim() || '#16191f',
      canvas: css.getPropertyValue('--c-bg').trim() || '#0e1014',
      label: css.getPropertyValue('--c-fg').trim() || '#e7e9ee',
      relation_label: css.getPropertyValue('--c-dim').trim() || '#929baa',
    };
    /* Entity-type colours belong to the theme (ledger.css `--c-entity-*`), so colouring by
       type with the Theme palette stays legible on light themes as well as dark ones. */
    [['person_or_concept', 'concept'], ['mention', 'mention'], ['hashtag', 'hashtag'],
      ['email', 'email'], ['organization', 'organization'], ['location', 'location']]
      .forEach(([type, token]) => {
        const value = css.getPropertyValue(`--c-entity-${token}`).trim();
        if (validGraphColor(value)) colors[type] = value;
      });
    return colors;
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

  function setGraphExportMenuOpen(open, restoreTriggerFocus = false) {
    const trigger = byId('graph-export');
    const menu = byId('graph-export-menu');
    if (open) {
      menu.hidden = false;
      trigger.setAttribute('aria-expanded', 'true');
      byId('graph-export-png').focus();
      return;
    }
    trigger.setAttribute('aria-expanded', 'false');
    if (restoreTriggerFocus || menu.contains(document.activeElement)) trigger.focus();
    menu.hidden = true;
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
    // Pretty-print normal exports for readability. An All Nodes payload stays compact
    // to avoid the indentation expansion and extra main-thread work at the release limit.
    const indentation = state.graphMode === 'full' ? undefined : 2;
    downloadGraphFile(new Blob([JSON.stringify(payload, null, indentation)], { type: 'application/json' }), 'engraphis-graph.json');
    showNotice('Graph data exported as JSON.');
  }

  function exportGraphPng() {
    const canvas = state.graphEngine && typeof state.graphEngine.exportImageCanvas === 'function'
      ? state.graphEngine.exportImageCanvas()
      : byId('graph-canvas').querySelector('canvas');
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

  function graphCountText(nodes, links, drawnLinks = null, visibleNodes = null) {
    const available = number(state.graphMeta && state.graphMeta.nodes_available) || nodes;
    const prefix = state.graphMode === 'full' ? 'All nodes · LOD' : 'High quality';
    const entityText = visibleNodes != null && number(visibleNodes) < number(nodes)
      ? `${number(visibleNodes).toLocaleString()} visible of ${number(nodes).toLocaleString()} entities`
      : available > nodes
      ? `${number(nodes).toLocaleString()} of ${available.toLocaleString()} entities`
      : `${number(nodes).toLocaleString()} entities`;
    const totalRelations = state.graphMeta && (state.graphMeta.relations_available != null
      ? state.graphMeta.relations_available : state.graphMeta.total_edges);
    const hiddenRelations = drawnLinks == null
      ? (totalRelations == null ? null : Math.max(0, number(totalRelations) - number(links)))
      : Math.max(0, number(links) - number(drawnLinks));
    const hidden = state.graphMode === 'full' && hiddenRelations != null
      ? ` · ${hiddenRelations.toLocaleString()} hidden relationships`
      : '';
    return `${prefix} · ${entityText} · ${number(links).toLocaleString()} relations${hidden}`;
  }

  function graphStatsChanged(stats) {
    if (!stats) return;
    const nodes = stats.nodes == null ? state.graphData.nodes.length : stats.nodes;
    const links = stats.links == null ? state.graphData.links.length : stats.links;
    byId('graph-count').textContent = graphCountText(
      nodes, links, stats.drawnLinks, stats.visibleNodes,
    );
    if (state.graphMode === 'full') {
      const note = byId('graph-lod-note');
      const detail = note && note.querySelector('span');
      if (detail) detail.textContent = stats.layoutPending
        ? 'Reflowing the complete graph in the background…'
        : stats.collapsed
          ? 'Clusters are condensed into representative nodes. Zoom in to expand them.'
          : 'Layout, forces, scope, colour and relation flow update without reloading the complete graph.';
    }
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
    const workspace = state.workspaces.find(item => workspaceName(item) === state.workspace);
    if (workspace && Array.isArray(workspace.repos)) workspace.repos.forEach(add);
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

  function cancelGraphRepositoryReload() {
    if (graphRepoLoadTimer === null) return;
    window.clearTimeout(graphRepoLoadTimer);
    graphRepoLoadTimer = null;
  }

  function scheduleGraphRepositoryReload() {
    cancelGraphRepositoryReload();
    graphRepoLoadTimer = window.setTimeout(() => {
      graphRepoLoadTimer = null;
      if (state.view === 'relations'
        && (state.graphIncludeCode || state.graphMode === 'full')) {
        loadGraph({ force: true });
      }
    }, 250);
  }

  function isCurrentGraphLoad(request) {
    return Boolean(request
      && request.id === state.graphLoadRequest
      && request.key === state.graphLoadKey
      && request.workspace === state.workspace
      && request.mode === state.graphMode
      && request.includeCode === state.graphIncludeCode
      && request.showUnlinked === state.graphShowUnlinked
      && request.asOf === graphAsOfTimestamp()
      && request.repo === (byId('graph-repo-filter').value || '').trim());
  }

  function setGraphLoadControlsBusy(busy, disableRetry = true) {
    const controls = disableRetry ? ['graph-retry'] : [];
    controls.forEach(id => {
      const control = byId(id);
      if (control) control.disabled = busy;
    });
    const every = document.querySelector('[data-graph-preset-choice="every"]');
    if (every) every.disabled = busy;
    const retry = byId('graph-retry');
    if (retry && ((busy && disableRetry) || !busy)) {
      retry.textContent = busy ? 'Reloading graph…' : 'Reload data';
    }
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
      graphLifecycle.sync();
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
      repo: targetRepo,
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
    state.graphLoadRepo = targetRepo;
    state.graphLoadController = controller;
    if (previousController && !previousController.signal.aborted) previousController.abort();
    // The initial load remains retryable; once a user explicitly starts a replacement, lock
    // the retry control until that transaction settles so repeated clicks cannot churn it.
    setGraphLoadControlsBusy(true, force);
    const oldEngine = state.graphEngine;
    /* Loading is a transaction: freeze the committed renderer before fetching its replacement.
       A failed request restores it; a successful request destroys it immediately before commit. */
    if (state.graphEngine && typeof state.graphEngine.freeze === 'function') {
      state.graphEngine.freeze(true);
    }
    if (state.graphSpacetimeOverlay) state.graphSpacetimeOverlay.setEnabled(false);
    const canvasEl = byId('graph-canvas');
    if (canvasEl) canvasEl.setAttribute('aria-busy', 'true');
    byId('graph-empty').hidden = false;
    byId('graph-empty').textContent = fullGraph
      ? 'Loading all nodes with progressive level of detail…'
      : 'Loading the responsive evidence graph…';
    const task = (async () => {
      const assets = ensureGraphAssets(fullGraph);
      const deadline = fullGraph ? GRAPH_FULL_LOAD_TIMEOUT_MS : GRAPH_LOAD_TIMEOUT_MS;
      let rejectTimeout;
      const timeoutPromise = new Promise((_, reject) => {
        rejectTimeout = reject;
      });
      // Transactional candidate tracking: host/engine/overlay live here until commit.
      // destroyCandidate() is the single cleanup path for stale, error, and timeout outcomes.
      const restoreCommittedRenderer = () => {
        /* A newer request owns the committed renderer while it is replacing this one. Do not
           thaw that renderer from a stale response; its own transaction will settle it. */
        if (state.graphEngine !== oldEngine
          || (state.graphLoadController && state.graphLoadController !== controller)) return;
        if (oldEngine && typeof oldEngine.freeze === 'function') {
          oldEngine.freeze(state.graphFrozen);
        }
        graphLifecycle.sync();
      };
      let candidateHost = null;
      let candidateEngine = null;
      let candidateOverlay = null;
      let candidateStats = null;
      let candidateMetrics = null;
      let candidatePhysicsSnapshot = null;
      const destroyCandidate = () => {
        if (!candidateEngine) {
          candidateOverlay = null;
          if (candidateHost && candidateHost.parentNode) {
            candidateHost.remove();
          }
          candidateHost = null;
          return;
        }
        // Keep committed references live: renderer callbacks close over candidateEngine.
        // Nulling it here would make every post-readiness stats/metrics callback look stale.
        if (state.graphEngine === candidateEngine) return;
        graphLifecycle.release(candidateEngine, candidateOverlay);
        candidateOverlay = null;
        candidateEngine = null;
        if (candidateHost && candidateHost.parentNode) {
          candidateHost.remove();
        }
        candidateHost = null;
      };
      const timeout = window.setTimeout(() => {
        // The same globals ensureGraphAssets() waits for, so a stalled d3 is released too.
        if (!window.d3 || !window.ForceGraph || !window.EngraphisGraph
          || !window.EngraphisSpacetime) {
          releaseGraphAssetsAttempt(graphAssetsPromise);
        }
        if (fullGraph && !window.EngraphisEveryGraph) {
          releaseGraphAllAssetsAttempt(graphAllAssetsPromise);
        }
        if (!controller.signal.aborted) controller.abort();
        const error = new Error('graph loading timed out');
        error.name = 'AbortError';
        rejectTimeout(error);
      }, deadline);
      try {
        const level = fullGraph ? 'complete' : 'overview';
        const presentation = fullGraph ? '&presentation=all' : '&presentation=quality';
        const limits = fullGraph ? ''
          : `&node_limit=${GRAPH_INITIAL_NODE_LIMIT}&edge_limit=${GRAPH_INITIAL_EDGE_LIMIT}`;
        const connectedOnly = !fullGraph && !targetShowUnlinked ? '&connected_only=true' : '';
        const includeCode = targetIncludeCode ? '&include_code=true' : '';
        const validatedRepo = targetIncludeCode || fullGraph
          ? validatedGraphRepository(targetRepo) : '';
        const scopedRepo = validatedRepo
          ? `&repo=${encodeURIComponent(validatedRepo)}` : '';
        const asOf = targetAsOf === null ? '' : `&as_of=${encodeURIComponent(targetAsOf / 1000)}`;
        const history = targetAsOf === null ? '' : '&include_history=true';
        // Complete Ledger views are canonical entity projections. Memory nodes remain available
        // to compatible callers, but must not change the existing entity evidence click path.
        const memoryProjection = fullGraph ? '&include_memory_nodes=false' : '';
        const [payload] = await Promise.race([
          Promise.all([
            api(`/graph/scene?${query(targetWorkspace)}&level=${level}${presentation}${limits}${connectedOnly}${includeCode}${scopedRepo}${asOf}${history}${memoryProjection}`, {
              signal: controller.signal,
              timeoutMs: deadline,
            }),
            assets,
          ]),
          timeoutPromise,
        ]);
        if (!isCurrentGraphLoad(request)) {
          restoreCommittedRenderer();
          return;
        }
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
        const sceneMeta = scene.meta || payload.meta || {};
        const nextMeta = {
          ...sceneMeta,
          nodes_available: sceneMeta.nodes_available == null ? (sceneMeta.total_nodes == null
            ? data.nodes.length : sceneMeta.total_nodes) : sceneMeta.nodes_available,
          nodes_complete: sceneMeta.nodes_complete == null
            ? (sceneMeta.truncated == null ? fullGraph : !sceneMeta.truncated)
            : sceneMeta.nodes_complete,
        };
        const responseIncludeCode = sceneMeta.include_code === false ? false : targetIncludeCode;
        const codeOverlayDegraded = targetIncludeCode && !responseIncludeCode;
        const oldHost = byId('graph-canvas');
        if (!oldHost) throw new Error('graph canvas container missing');
        // oldEngine was captured before the first await so the failure path
        // can restore the exact committed renderer even when candidate setup never begins.
        candidateHost = oldHost.cloneNode(false);
        candidateHost.id = `graph-canvas-candidate-${request.id}`;
        candidateHost.classList.add('graph-canvas-candidate');
        candidateHost.setAttribute('aria-hidden', 'true');
        candidateHost.tabIndex = -1;
        oldHost.insertAdjacentElement('afterend', candidateHost);
        /* Authored-Galaxy detection must key off scene markers, not the toolbar preset:
           entering Every via its chip sets the preset to 'every', but a complete scene
           with system anchors still needs the hierarchical orbit engine and overlay.
           The live-limit guard keeps very large complete scenes on the lightweight
           Every renderer instead of the full physics galaxy. */
        const galaxyWithinLiveLimit = data.nodes.length <= GRAPH_INITIAL_NODE_LIMIT
          && data.links.length <= GRAPH_INITIAL_EDGE_LIMIT;
        const galaxyQuality = fullGraph && galaxyWithinLiveLimit
          && data.nodes.some(node => node.anchor_role === 'community'
            && (node.system_anchor_id !== undefined
              || Number.isFinite(Number(node.galactic_radius))));
        const graphFactory = galaxyQuality ? window.EngraphisGraph
          : fullGraph ? window.EngraphisEveryGraph : window.EngraphisGraph;
        if (!graphFactory || typeof graphFactory.create !== 'function') {
          throw new Error(fullGraph
            ? 'all-node graph engine asset is unavailable'
            : 'graph engine asset is unavailable');
        }
        candidateEngine = graphFactory.create(candidateHost, {
          renderMode: fullGraph ? 'all' : 'overview',
          onNodeClick: item => {
            graphExplorer.select(item && item.id);
            openGraphConnections(item);
          },
          onBackgroundClick: () => candidateEngine.clearFocus(),
          onStats: stats => {
            if (state.graphEngine === candidateEngine
              && state.graphLoadRequest === request.id) graphStatsChanged(stats);
            else if (state.graphLoadRequest === request.id) candidateStats = stats;
          },
          onMetrics: metrics => {
            if (state.graphEngine === candidateEngine
              && state.graphLoadRequest === request.id) graphMetricsChanged(metrics);
            else if (state.graphLoadRequest === request.id) candidateMetrics = metrics;
          },
          onPhysicsFrame: snapshot => {
            candidatePhysicsSnapshot = snapshot;
            if (candidateOverlay) candidateOverlay.setSnapshot(snapshot);
          },
          onError: error => {
            if (state.graphEngine !== candidateEngine || !fullGraph
              || state.graphLoadRequest !== request.id || state.graphMode !== 'full') return;
            byId('graph-empty').hidden = false;
            byId('graph-empty').textContent = error && error.code === 'GRAPH_CAPACITY'
              ? `All nodes exceed renderer capacity. Enter an exact repository filter or reduce the workspace graph. (${error.message})`
              : 'The all-node renderer stopped. Choose Reload data to start a fresh worker.';
            byId('graph-canvas').setAttribute('aria-busy', 'false');
          },
          onCollapseChange: collapsed => {
            if (targetMode === 'overview') showNotice(collapsed ? 'Clusters collapsed for overview.' : '');
            else {
              const note = byId('graph-lod-note');
              const detail = note && note.querySelector('span');
              if (detail) detail.textContent = collapsed
                ? 'Clusters are condensed into representative nodes. Zoom in to expand them.'
                : 'Layout, forces, scope, colour and relation flow update without reloading the complete graph.';
            }
          },
          onSlingshotRelease: () => {
            if (state.graphSpacetimeOverlay && state.graphEngine
              && typeof state.graphEngine.getPhysicsSnapshot === 'function') {
              state.graphSpacetimeOverlay.setSnapshot(state.graphEngine.getPhysicsSnapshot());
            }
          },
        });
        candidateEngine.apply(graph => {
          const preset = byId('graph-preset').value;
          graph.setPreset(galaxyQuality && preset === 'every' ? 'galaxy' : preset);
          graph.setStyle(byId('graph-style').value);
          graph.setColorBy(byId('graph-color').value);
          graph.setThemeColors(graphThemeColors());
          const paletteName = byId('graph-palette').value;
          graph.setPalette(paletteName);
          if (paletteName === 'custom') graph.setTypeColors(customGraphColors());
          graph.setSettings({
            ...graphTuningEngineSettings(),
            ...graphSpacetimeEngineSettings(),
            flow: byId('graph-flow').getAttribute('aria-checked') === 'true',
            labels: byId('graph-labels').getAttribute('aria-checked') === 'true',
            frozen: fullGraph ? false : state.graphFrozen,
          });
          graph.setScope(graphScope());
          graph.setLayers(graphLayerState());
          graph.setRepoFilter(byId('graph-repo-filter').value);
          graph.setAsOf(graphAsOfTimestamp());
          graph.setSizeBy(graphSizeBy());
          graph.setBridges(byId('graph-bridges').checked);
          graph.setCollapse(byId('graph-collapse').checked ? 'auto' : false);
          graph.setGhosts(byId('graph-ghosts').checked);
        }, false, false);
        /* Renderers may make their host focusable; a hidden candidate must not be. */
        candidateHost.tabIndex = -1;
        candidateEngine.setData(data);
        candidateEngine.freeze(fullGraph ? false : state.graphFrozen);
        if ((!fullGraph || galaxyQuality) && window.EngraphisSpacetime
          && window.EngraphisSpacetime.create) {
          candidateOverlay = window.EngraphisSpacetime.create(
            candidateHost, candidateEngine
          );
          candidateOverlay.setEnabled(galaxyQuality || graphIsGalaxy());
          if (candidatePhysicsSnapshot) candidateOverlay.setSnapshot(candidatePhysicsSnapshot);
        }
        // Pending renderers also follow visibility while finite worker preparation finishes.
        graphLifecycle.track(candidateEngine, candidateOverlay, () => graphOverlayEnabled(galaxyQuality));
        if (typeof candidateEngine.whenReady === 'function') {
          await Promise.race([candidateEngine.whenReady(), timeoutPromise]);
          if (!isCurrentGraphLoad(request)) {
            destroyCandidate();
            restoreCommittedRenderer();
            return;
          }
        }
        oldHost.id = `graph-canvas-retired-${request.id}`;
        candidateHost.id = 'graph-canvas';
        candidateHost.classList.remove('graph-canvas-candidate');
        candidateHost.removeAttribute('aria-hidden');
        candidateHost.tabIndex = 0;
        oldHost.replaceWith(candidateHost);
        candidateHost = null;
        state.graphEngine = candidateEngine;
        state.graphSpacetimeOverlay = candidateOverlay;
        state.graphData = data;
        state.graphWorkspace = targetWorkspace;
        state.graphDataMode = targetMode;
        state.graphGalaxyQuality = galaxyQuality;
        state.graphDataPreset = byId('graph-preset').value;
        state.graphDataIncludeCode = responseIncludeCode;
        state.graphDataShowUnlinked = targetShowUnlinked;
        state.graphDataAsOf = targetAsOf;
        state.graphDataRepo = targetRepo;
        state.graphMeta = nextMeta;
        if (codeOverlayDegraded) {
          state.graphIncludeCode = false;
          const layers = { ...graphLayerState(), code: false };
          setGraphLayers(layers);
          candidateEngine.setLayers(layers);
          clearGraphSavedView();
          saveGraphPreferences();
          showNotice(sceneMeta.degraded_reason === 'code_overlay_requires_repository_filter'
            ? 'Code overlay needs a repository filter; showing entity relationships only.'
            : 'Code overlay is unavailable; showing entity relationships only.');
        }
        graphLifecycle.replace(candidateEngine, candidateOverlay,
          () => graphOverlayEnabled());
        byId('graph-empty').hidden = Boolean(data.nodes.length);
        if (!data.nodes.length) byId('graph-empty').textContent = 'No entities exist in this workspace yet.';
        updateGraphModeControls();
        updateGraphFacts(data);
        graphExplorer.setData(data);
        renderGraphTypeColors();
        renderGraphLegend();
        // Candidate stats emitted before commit are retained, then published only after the
        // renderer transaction commits. Raw payload counts are wrong when a frozen renderer
        // has already applied repository, layer, ghost, or collapse visibility filters.
        graphStatsChanged(candidateStats || { nodes: data.nodes.length, links: data.links.length });
        if (candidateMetrics) graphMetricsChanged(candidateMetrics);
        updateGraphLayerCounts(data, scene.layers || payload.layers);
      } catch (error) {
        if (!isCurrentGraphLoad(request)) {
          destroyCandidate();
          restoreCommittedRenderer();
          return;
        }
        destroyCandidate();
        byId('graph-empty').hidden = false;
        byId('graph-empty').textContent = error && error.name === 'AbortError'
          ? `${fullGraph ? 'All-node graph' : 'High-quality graph'} loading timed out. Choose Reload data to try again.`
          : fullGraph && (error.status === 413 || error.code === 'GRAPH_CAPACITY')
            ? `All nodes exceed the server capacity. Enter an exact repository filter or reduce the workspace graph. (${error.message})`
          : `Graph unavailable: ${error.message}. Choose Reload data to try again.`;
        if (state.graphData) {
          if (state.graphDataMode !== targetMode) state.graphMode = state.graphDataMode;
          /* The toolbar preset changes before a replacement request begins. Restore the
             committed preset together with the committed renderer so a failed Every-node
             transition cannot leave aria-pressed and the active engine disagreeing. */
          byId('graph-preset').value = state.graphDataPreset || 'galaxy';
          updateGraphModeControls();
          syncGraphChoices();
        }
        // Restore the committed renderer's freeze/overlay state. The old engine survived
        // because we never mutated state.graphEngine on the failure path.
        if (oldEngine && typeof oldEngine.freeze === 'function') {
          oldEngine.freeze(state.graphFrozen);
        }
        graphLifecycle.sync();
      } finally {
        // Safety net: if control left the try/catch without committing or cleaning up
        // (e.g. an unexpected throw in finally itself), ensure no candidate leaks.
        destroyCandidate();
        window.clearTimeout(timeout);
        if (state.graphLoadRequest === request.id && state.graphLoadController === controller) {
          const canvasEl = byId('graph-canvas');
          if (canvasEl) canvasEl.setAttribute('aria-busy', 'false');
          setGraphLoadControlsBusy(false);
        }
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
        state.graphLoadRepo = '';
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

  function temporalTime(value) {
    const date = validDate(value);
    return date ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date) : '';
  }

  /* Bi-temporal card: when the fact was true in the world, when Engraphis learned it, and
     where it came from. A closed valid time marks a superseded or retired version. */
  function temporalCard(memory) {
    const current = !memory.valid_to && !memory.expired_at;
    const card = simpleMemoryCard(memory, 'timeline-card temporal-card');
    const heading = card.querySelector('h3');
    if (heading) {
      heading.append(document.createTextNode(' '),
        node('span', `temporal-state${current ? ' current' : ''}`, current ? 'current' : 'past'));
    }
    const ended = memory.valid_to || memory.expired_at;
    const meta = node('dl', 'temporal-meta');
    [
      ['Valid time', `${temporalTime(memory.valid_from) || 'unrecorded'} — ${ended ? temporalTime(ended) || 'closed' : 'now'}`],
      ['Recorded', temporalTime(memory.ingested_at) || 'unrecorded'],
      ['Source', text((memory.provenance && (memory.provenance.source || memory.provenance.kind)) || 'unrecorded')],
    ].forEach(([term, value]) => {
      const row = node('div');
      row.append(node('dt', '', term), node('dd', '', value));
      meta.append(row);
    });
    card.append(meta);
    return card;
  }

  function searchModeNote(payload) {
    return payload && payload.mode === 'keyword'
      ? node('p', 'search-mode-note', 'Keyword match: semantic search is unavailable on this server, so these results share words with your query rather than its meaning.')
      : null;
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
      const note = searchModeNote(payload);
      if (note) target.append(note);
      target.append(node('h2', '', 'Live support'));
      if (!live.length) target.append(empty('No live supporting memory was found.'));
      else live.forEach(memory => target.append(simpleMemoryCard(memory)));
      target.append(node('h2', '', 'Superseded history'));
      if (!superseded.length) target.append(empty('No superseded versions were found.'));
      else superseded.forEach(memory => target.append(temporalCard(memory)));
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      target.replaceChildren(empty(`Could not trace belief: ${error.message}`));
    }
  }

  async function timelineSearch(event) {
    event.preventDefault();
    const input = byId('timeline-input');
    const target = byId('timeline-result');
    const closedOnly = byId('timeline-closed-only').checked;
    const question = input.value.trim();
    if (!question) {
      showNotice('Enter a topic before showing history.');
      input.focus();
      return;
    }
    const request = beginScopedRequest('timeline');
    showNotice('');
    target.replaceChildren(empty('Loading temporal history…'));
    try {
      const payload = await api(`/timeline?q=${encodeURIComponent(question)}&${query(request.workspace)}&limit=50`);
      if (!isCurrentScopedRequest(request)) return;
      let history = payload.history || [];
      if (closedOnly) history = history.filter(item => item.valid_to || item.expired_at);
      target.replaceChildren();
      const note = searchModeNote(payload);
      if (note) target.append(note);
      if (!history.length) {
        target.append(empty(closedOnly ? 'No superseded versions were found for this topic.' : 'No temporal history was found.'));
      } else history.forEach(memory => target.append(temporalCard(memory)));
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

  function renderReceiptChain(result, error = null) {
    const target = byId('receipt-chain-status');
    target.replaceChildren();
    if (!result) {
      target.dataset.state = 'unknown';
      target.textContent = `The receipt chain could not be checked${error ? `: ${error.message}` : '.'}`;
      return;
    }
    const valid = result.valid != null ? result.valid : result.verified;
    const count = number(result.count);
    if (valid !== true && valid !== false) {
      target.dataset.state = 'unknown';
      target.textContent = 'The receipt chain status is unavailable.';
      return;
    }
    target.dataset.state = valid ? 'valid' : 'invalid';
    target.append(document.createTextNode(valid === false
      ? `Receipt chain broken · ${count.toLocaleString()} receipts checked`
      : `Receipt chain verified · ${count.toLocaleString()} ${count === 1 ? 'receipt' : 'receipts'}`));
    if (result.head) target.append(document.createTextNode(' · head '), node('code', '', truncate(result.head, 24)));
    if (valid === false && Array.isArray(result.errors) && result.errors.length) {
      target.append(document.createTextNode(` · first problem at receipt ${number(result.errors[0].index) + 1}`));
    }
    target.append(document.createTextNode(` · checked ${new Date().toLocaleTimeString()}`));
  }

  async function loadAudit() {
    const request = beginScopedRequest('audit');
    const target = byId('audit-list');
    target.replaceChildren(empty('Loading audit records and receipts…'));
    byId('savings-detail').replaceChildren(empty('Loading receipt-backed estimate…'));
    byId('receipt-chain-status').textContent = 'Checking the receipt chain…';
    byId('receipt-chain-status').dataset.state = 'unknown';
    const [auditResult, receiptsResult, savingsResult, chainResult] = await Promise.allSettled([
      api(`/audit?${query(request.workspace)}&limit=100`, { signal: request.signal }),
      api(`/receipts?${query(request.workspace)}&limit=100`, { signal: request.signal }),
      api(`/context-savings${savingsQuery(state.savingsPreset)}`, { signal: request.signal }),
      api(`/receipts/verify?${query(request.workspace)}`, { signal: request.signal }),
    ]);
    if (!isCurrentScopedRequest(request)) return;
    renderReceiptChain(chainResult.status === 'fulfilled' ? chainResult.value : null,
      chainResult.status === 'rejected' ? chainResult.reason : null);
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
    const workspace = state.workspace;
    byId('receipt-chain-status').textContent = 'Checking the receipt chain…';
    try {
      const result = await api(`/receipts/verify?${query(workspace)}`);
      if (workspace !== state.workspace) return;
      renderReceiptChain(result);
      const valid = result.valid != null ? result.valid : result.verified;
      showNotice(valid === false ? 'Receipt verification found a broken chain.'
        : valid === true ? 'Receipt chain verified.' : 'The receipt chain status is unavailable.');
    } catch (error) {
      if (workspace !== state.workspace) return;
      renderReceiptChain(null, error);
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

  function workspaceRepoCount(item) {
    if (Array.isArray(item.repos)) return item.repos.length;
    return number(item.repos);
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
      const active = name === state.workspace;
      const card = node('article', `workspace-card${active ? ' active' : ''}`);
      const copy = node('div');
      const title = node('h3', '', name);
      if (active) title.append(document.createTextNode(' '), node('span', 'tag', 'active'));
      const repos = workspaceRepoCount(item);
      copy.append(
        title,
        ...(item.description ? [node('p', '', item.description)] : []),
        node('p', '', `${number(item.memories).toLocaleString()} ${number(item.memories) === 1 ? 'memory' : 'memories'}${repos ? ` · ${repos.toLocaleString()} ${repos === 1 ? 'project' : 'projects'}` : ''} · ${item.visibility === 'personal'
          ? 'Personal · excluded from Cloud Sync'
          : item.visibility === 'shared'
            ? 'Shared · eligible for encrypted Cloud Sync'
            : 'Local workspace'}`),
      );
      const actions = node('div', 'workspace-card-actions');
      if (!active) actions.append(button('Switch to', 'secondary-button', () => selectWorkspace(name)));
      actions.append(
        button('Open library', 'secondary-button', () => openWorkspaceLibrary(name)),
        button('Rename', 'secondary-button', () => renameWorkspace(name)),
        button('Describe', 'secondary-button', () => describeWorkspace(name)),
        button('Copy', 'secondary-button', () => copyWorkspace(name)),
        button('Merge into…', 'secondary-button', () => mergeWorkspace(name)),
        button('Export JSON', 'secondary-button', () => exportWorkspace(name)),
      );
      if (item.visibility === 'personal' && item.can_change_access === true) {
        actions.append(button('Share for Cloud Sync', 'secondary-button', () => changeWorkspaceVisibility(name, 'shared')));
      } else if (item.visibility === 'shared' && item.can_change_access === true) {
        actions.append(button('Make personal', 'secondary-button', () => changeWorkspaceVisibility(name, 'personal')));
      }
      actions.append(button('Delete', 'danger-button', () => deleteWorkspace(name)));
      card.append(copy, actions);
      target.append(card);
    });
  }

  async function openWorkspaceLibrary(name) {
    if (name !== state.workspace) {
      if (!await confirmDiscardEditor()) return;
      await selectWorkspace(name);
    }
    switchView('library');
  }

  /* Changing the workspace list reselects a workspace, which clears notices. Report the
     outcome once the list is current, so the confirmation is not wiped as it appears. */
  async function refreshAfterWorkspaceChange(preferred, message) {
    try {
      await refreshBootstrap(preferred);
      showNotice(message);
    } catch (error) {
      showNotice(`${message} The workspace list could not be refreshed: ${error.message}`);
    }
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
      byId('create-workspace-form').reset();
      byId('create-workspace-form').hidden = true;
      await refreshAfterWorkspaceChange(name, `Workspace ${name} created.`);
    } catch (error) {
      showNotice(`Could not create workspace: ${error.message}`);
    }
  }

  async function changeWorkspaceVisibility(name, visibility) {
    const sharing = visibility === 'shared';
    const question = sharing
      ? `Share “${name}” with signed-in Team members and make it eligible for encrypted Cloud Sync? Other Team members with access can read this workspace. This changes its visibility from personal to shared.`
      : `Make “${name}” personal? Other Team members will lose access and future Cloud Sync rounds will exclude it. This does not erase data already uploaded to the encrypted relay.`;
    if (!await dialogs.confirm({
      title: sharing ? 'Share workspace' : 'Make workspace personal',
      message: question,
      confirmLabel: sharing ? 'Share workspace' : 'Make personal',
    })) return;
    try {
      await api('/workspaces/visibility', {
        method: 'POST',
        body: { workspace: name, visibility, confirmed: true },
      });
      await refreshBootstrap(state.workspace);
      showNotice(sharing
        ? `Workspace ${name} is shared and eligible for Cloud Sync. Run Sync now to upload eligible changes.`
        : `Workspace ${name} is personal and excluded from future Cloud Sync rounds.`);
    } catch (error) {
      showNotice(`Could not change access for workspace ${name}: ${error.message}`);
    }
  }

  async function renameWorkspace(name) {
    const next = await dialogs.prompt({
      title: 'Rename workspace',
      message: `Choose a new name for “${name}”. Agents that name this workspace explicitly need the new name.`,
      label: 'Workspace name',
      value: name,
      maxLength: 200,
      confirmLabel: 'Rename',
    });
    if (!next || next === name) return;
    try {
      await api('/workspaces/rename', { method: 'POST', body: { workspace: name, new_name: next } });
      await refreshAfterWorkspaceChange(name === state.workspace ? next : state.workspace,
        `Workspace renamed to ${next}.`);
    } catch (error) {
      showNotice(`Could not rename workspace: ${error.message}`);
    }
  }

  async function describeWorkspace(name) {
    const item = state.workspaces.find(entry => workspaceName(entry) === name) || {};
    const description = await dialogs.prompt({
      title: 'Describe workspace',
      message: `Say what belongs in “${name}”. The description helps you and your agents choose the right workspace.`,
      label: 'Description (optional)',
      value: item.description || '',
      multiline: true,
      required: false,
      maxLength: 500,
      confirmLabel: 'Save description',
    });
    if (description === null) return;
    try {
      await api('/workspaces/describe', { method: 'POST', body: { workspace: name, description } });
      await refreshAfterWorkspaceChange(state.workspace,
        description ? `Description saved for ${name}.` : `Description cleared for ${name}.`);
    } catch (error) {
      showNotice(`Could not save the description: ${error.message}`);
    }
  }

  async function copyWorkspace(name) {
    try {
      const result = await api('/workspaces/copy', { method: 'POST', body: { workspace: name } });
      const copied = number(result && result.memories_copied);
      await refreshAfterWorkspaceChange(state.workspace,
        `Workspace copied${result && result.workspace ? ` to ${result.workspace}` : ''} · ${copied.toLocaleString()} ${copied === 1 ? 'memory' : 'memories'}.`);
    } catch (error) {
      showNotice(`Could not copy workspace: ${error.message}`);
    }
  }

  async function mergeWorkspace(name) {
    const others = state.workspaces.map(workspaceName).filter(other => other !== name);
    if (!others.length) {
      showNotice('Create another workspace before merging this one into it.');
      return;
    }
    const item = state.workspaces.find(entry => workspaceName(entry) === name) || {};
    const count = number(item.memories);
    const result = await dialogs.open({
      title: 'Merge workspace',
      message: [
        `Move all ${count.toLocaleString()} ${count === 1 ? 'memory' : 'memories'} in “${name}”, with their history, projects and graph, into another workspace. “${name}” is removed afterwards.`,
        'The moved memories take on the destination workspace’s access rules. This cannot be undone; export the workspace first if you may need it separately.',
      ],
      fields: [{
        name: 'target', type: 'select', label: 'Destination workspace', required: true,
        options: others.map(value => ({ value, label: value })), value: others[0],
      }],
      confirmLabel: 'Merge workspace',
      danger: true,
    });
    if (result === null || !result.target) return;
    try {
      const merged = await api('/workspaces/merge', { method: 'POST', body: { source: name, target: result.target } });
      const moved = number(merged && merged.memories_moved);
      await refreshAfterWorkspaceChange(name === state.workspace ? result.target : state.workspace,
        `Merged ${moved.toLocaleString()} ${moved === 1 ? 'memory' : 'memories'} into ${result.target}.`);
    } catch (error) {
      showNotice(`Could not merge workspace: ${error.message}`);
    }
  }

  async function exportWorkspace(name) {
    try {
      const snapshot = await api(`/export?${query(name)}`, { timeoutMs: 120_000 });
      const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' });
      const link = document.createElement('a');
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = `engraphis-export-${name.replace(/[^\w.-]+/g, '-') || 'workspace'}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      showNotice(`Exported ${name} as portable JSON. It contains memory content; store it like the database itself.`);
    } catch (error) {
      showNotice(`Could not export workspace: ${error.message}`);
    }
  }

  async function deleteWorkspace(name) {
    const item = state.workspaces.find(entry => workspaceName(entry) === name) || {};
    const count = number(item.memories);
    const active = name === state.workspace;
    const confirmed = await dialogs.confirm({
      title: 'Delete workspace permanently',
      message: [
        `Permanently delete “${name}” and its ${count.toLocaleString()} ${count === 1 ? 'memory' : 'memories'}, sessions, projects and graph from this Engraphis store? This is a hard delete and cannot be undone; only the audit log keeps a record of it.`,
        active ? 'This is the active workspace. Another workspace becomes active afterwards.' : '',
        'Export the workspace first if you might need its contents.',
      ],
      confirmLabel: 'Delete workspace',
      danger: true,
    });
    if (!confirmed) return;
    try {
      const result = await api('/workspaces/delete', { method: 'POST', body: { workspace: name } });
      const removed = number(result && result.memories_removed);
      await refreshAfterWorkspaceChange(active ? '' : state.workspace,
        `Workspace ${name} deleted · ${removed.toLocaleString()} ${removed === 1 ? 'memory' : 'memories'} removed.`);
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

  function runFreshAnalytics(workspace) {
    if (workspace !== state.workspace) return;
    state.analyticsJobs.delete(workspace);
    state.analyticsResults.delete(workspace);
    state.analyticsStartErrors.delete(workspace);
    void loadHosted('analytics');
  }

  /* Why a hosted request was refused, in the customer's terms. Only 402/501 are entitlement
     answers. 409 consent_required means the plan is fine but this workspace has not been
     approved for readable processing. 401 is a credential problem — except on an
     unconfigured installation that may still start a trial. Anything else is not a denial. */
  function lockReason() {
    const access = licenseAccessState();
    const plan = (licensePlanKey() || 'pro').toUpperCase();
    if (access === 'trial_expired') return 'Your free trial has ended, so hosted features are locked. The trial cannot be started again.';
    if (access === 'lapsed') return `Your ${plan} subscription is no longer active, so hosted features are locked until billing is up to date.`;
    if (access === 'trial') {
      const ends = licenseTrialEnds();
      return `Your free trial is live${ends ? ` until ${ends}` : ''}, but it does not include this.`;
    }
    if (access === 'active') return `Your ${plan} plan does not include this.`;
    if (licenseTrialAvailable()) {
      const days = licenseTrialDays('pro');
      return days ? `An email-confirmed, no-card Pro trial lasts exactly ${days} active days.` : 'An email-confirmed, no-card Pro trial is available.';
    }
    return 'Hosted features need an active Pro or Team plan.';
  }

  function hostedFailure(kind, error) {
    const feature = kind === 'analytics' ? 'Analytics' : kind === 'automation' ? 'Hosted automation'
      : kind === 'sync' ? 'Cloud Sync' : 'This feature';
    const status = error && error.status;
    if (status === 409 && error.code === 'consent_required') {
      return {
        title: `${feature} is waiting for your approval`,
        detail: licenseHasHostedAccess()
          ? `Your plan includes ${feature}. Readable managed processing is paused until you approve this workspace in Settings → Preferences. Secret and session-scoped memories stay local.`
          : 'After connecting to Engraphis Cloud, approve each workspace in Settings → Preferences before readable processing starts. Secret and session-scoped memories stay local.',
        action: 'consent',
      };
    }
    if (status === 402 || status === 501) {
      return { title: `${feature} needs Engraphis Pro`, detail: lockReason(), action: 'plan' };
    }
    if (status === 401) {
      if (error.code === 'cloud_unconfigured' && licenseTrialAvailable()) {
        return { title: `${feature} runs in Engraphis Cloud`, detail: `${lockReason()} Start a trial to connect this installation, then approve each workspace in Settings → Preferences.`, action: 'plan' };
      }
      return {
        title: 'Reconnect Engraphis Cloud',
        detail: 'This installation’s Engraphis Cloud session expired or was revoked. Connect again to resume hosted features; your plan is unchanged.',
        action: 'reconnect',
      };
    }
    return null;
  }

  function renderHostedFailure(target, failure) {
    target.replaceChildren(node('h3', '', failure.title), node('p', 'automation-policy-note', failure.detail));
    const actions = node('div', 'automation-policy-actions');
    if (failure.action === 'consent') {
      actions.append(button('Review workspace processing', 'primary-button', () => switchManageTab('settings')));
    } else if (failure.action === 'reconnect') {
      const href = hostedAccountUrl('reconnect');
      if (href) {
        const link = node('a', 'secondary-button', 'Open Engraphis Cloud');
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener';
        actions.append(link);
      }
    } else actions.append(button('Compare plans', 'secondary-button', () => switchManageTab('plans')));
    target.append(actions);
  }

  function renderAnalyticsStartError(target, workspace, error) {
    const failure = hostedFailure('analytics', error);
    if (failure) {
      renderHostedFailure(target, failure);
      return;
    }
    const message = error && error.message ? error.message : text(error);
    target.replaceChildren(
      node('h3', '', 'Analytics request unconfirmed'),
      node('p', 'automation-policy-note',
        `Could not confirm the Analytics run: ${message} It may still be processing in Cloud. Starting a fresh analysis creates another run.`),
      button('Run fresh analysis', 'secondary-button', () => runFreshAnalytics(workspace)),
    );
  }

  function hasAnalyticsMetrics(result) {
    const totals = result && result.totals;
    const forecast = result && result.decay_forecast;
    return result && result.kind === 'analytics'
      && Number.isInteger(result.memory_count) && result.memory_count >= 0
      && totals && Number.isInteger(totals.live) && totals.live >= 0
      && Number.isInteger(totals.pinned) && totals.pinned >= 0
      && Number.isFinite(totals.avg_retention)
      && forecast && Number.isInteger(forecast.at_risk_7d);
  }

  function renderAnalyticsResult(target, result, workspace, stale = false) {
    const totals = result.totals;
    const live = totals.live;
    target.replaceChildren(node('h3', '', live === 0 ? 'Analytics has no eligible memories' : 'Analytics result'));
    if (live === 0) {
      target.append(node('p', 'automation-policy-note',
        'This result has no eligible memories. Add a workspace memory, then run a fresh analysis.'));
    } else {
      const metrics = [
        ['Live memories', live.toLocaleString()],
        ['Average retention', `${Math.round(totals.avg_retention * 100)}%`],
        ['At risk within 7 days', result.decay_forecast.at_risk_7d.toLocaleString()],
        ['Pinned memories', totals.pinned.toLocaleString()],
      ];
      const grid = node('div', 'stat-grid');
      metrics.forEach(([label, value]) => {
        const item = node('div', 'stat-item');
        item.append(node('span', '', label), node('strong', '', value));
        grid.append(item);
      });
      target.append(grid);
    }
    if (stale) target.append(node('p', 'automation-policy-note',
      'A newer workspace snapshot exists. Run a fresh analysis to see current data.'));
    target.append(button('Run fresh analysis', 'secondary-button', () => runFreshAnalytics(workspace)));
  }

  function renderAnalyticsPending(target, workspace, jobId, status, error = '') {
    target.replaceChildren(
      node('h3', '', error ? 'Analytics result unavailable' : 'Analytics is processing'),
      node('p', 'automation-policy-note', error ||
        `The Cloud job is ${status === 'running' ? 'running' : 'queued'}. Check this result again without starting another run.`),
      button('Refresh result', 'secondary-button', () => { void refreshAnalyticsResult(workspace, jobId); }),
    );
    if (error) target.append(button('Run fresh analysis', 'secondary-button',
      () => runFreshAnalytics(workspace)));
  }

  async function refreshAnalyticsResult(workspace, jobId) {
    if (workspace !== state.workspace || state.analyticsJobs.get(workspace) !== jobId) return;
    const request = beginScopedRequest('hosted-analytics');
    const target = byId('analytics-result');
    target.replaceChildren(empty('Checking the existing Analytics job…'));
    try {
      const status = await api(`/analytics/job-result?${query(workspace)}&job_id=${encodeURIComponent(jobId)}`,
        { signal: request.signal, timeoutMs: 45_000 });
      if (!isCurrentScopedRequest(request)) return;
      if (status.pending === true) {
        renderAnalyticsPending(target, workspace, jobId, status.state);
      } else if (status.failed === true) {
        state.analyticsJobs.delete(workspace);
        target.replaceChildren(
          node('p', 'automation-policy-note', 'This Analytics run did not complete.'),
          button('Run fresh analysis', 'secondary-button', () => runFreshAnalytics(workspace)),
        );
      } else if (hasAnalyticsMetrics(status.result)) {
        state.analyticsJobs.delete(workspace);
        state.analyticsResults.set(workspace, { result: status.result, stale: status.state === 'stale' });
        renderAnalyticsResult(target, status.result, workspace, status.state === 'stale');
      } else {
        throw new Error('The Analytics result is unavailable.');
      }
    } catch (error) {
      if (isCurrentScopedRequest(request)) {
        renderAnalyticsPending(target, workspace, jobId, '', `Could not check this result: ${error.message}`);
      }
    }
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
    if (!await dialogs.confirm({
      title: 'Commit consolidation',
      message: `Commit the reviewed consolidation result for ${state.workspace}? Original records remain in temporal history.`,
      confirmLabel: 'Commit result',
    })) return;
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
    // The label names the transfer before it happens, so it follows the checkbox.
    const toggle = form.querySelector('#automation-enabled');
    if (toggle) toggle.addEventListener('change', () => {
      save.textContent = toggle.checked ? 'Save & send policy to Cloud' : 'Save hosted policy';
    });
    actions.append(save);
    form.append(actions);
    form.addEventListener('submit', saveAutomationPolicy);
    target.append(form, automationRunPanel(policy, workspace));
  }

  function automationTime(value) {
    const date = validDate(value);
    return date ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date) : '';
  }

  function automationRunPanel(policy, workspace) {
    const panel = node('section', 'automation-run-panel');
    panel.setAttribute('aria-label', 'Hosted maintenance runs');
    panel.append(node('h3', '', 'Runs'), definitionList([
      ['Status', policy.enabled ? 'Enabled' : 'Paused'],
      ['Last run', policy.last_run ? `${automationTime(policy.last_run)} (${relative(policy.last_run)})` : 'Never'],
      ['Next run', policy.enabled ? (automationTime(policy.next_run_at) || 'Scheduled by Engraphis Cloud') : 'Paused'],
    ]));
    const request = button('Request a proposal now', 'secondary-button', () => requestMaintenanceProposal(workspace, request, result));
    const result = node('div', 'automation-run-result');
    result.setAttribute('role', 'status');
    result.setAttribute('aria-live', 'polite');
    panel.append(
      node('p', 'automation-policy-note', 'A proposal uploads this workspace’s normal and sensitive memory content, excluding secret and session-scoped records, for one managed consolidation pass. Results are proposals and never change the local database on their own.'),
      request, result,
    );
    return panel;
  }

  async function requestMaintenanceProposal(workspace, control, target) {
    if (!workspace || workspace !== state.workspace) return;
    if (!await dialogs.confirm({
      title: 'Request a hosted proposal',
      message: `Send ${workspace}’s normal and sensitive memory content to Engraphis Cloud for a reviewable consolidation proposal? Secret and session-scoped records stay local.`,
      confirmLabel: 'Request proposal',
    })) return;
    const request = beginScopedRequest('automation-proposal');
    control.disabled = true;
    target.replaceChildren(empty('Requesting a managed proposal…'));
    try {
      const response = await api(`/maintenance/run?${query(workspace)}`, {
        method: 'POST', body: { dry_run: true }, timeoutMs: 120_000,
      });
      if (!isCurrentScopedRequest(request) || !target.isConnected) return;
      const run = (response && Array.isArray(response.runs) && response.runs[0]) || {};
      renderObject(target, run.consolidate && typeof run.consolidate === 'object' ? run.consolidate : response, 'Proposal submitted for review');
      showNotice('Managed proposal requested. Review it before applying anything locally.');
    } catch (error) {
      if (!isCurrentScopedRequest(request) || !target.isConnected) return;
      const failure = hostedFailure('automation', error);
      if (failure) renderHostedFailure(target, failure);
      else target.replaceChildren(empty(`Could not request a proposal: ${error.message}`, 'form-error'));
    } finally {
      control.disabled = false;
    }
  }

  async function bootstrapAutomation(workspace, control) {
    if (!workspace || workspace !== state.workspace) return;
    if (!await dialogs.confirm({
      title: 'Initialize hosted automation',
      message: `Initialize hosted automation for ${workspace}? Engraphis will upload one bounded snapshot of that workspace's normal and sensitive memory content and save the default Cloud policy.`,
      confirmLabel: 'Upload snapshot and initialize',
    })) return;
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
    if (policy.enabled && !await dialogs.confirm({
      title: 'Save hosted policy',
      message: [
        `Save this hosted policy for ${workspace}? Engraphis will submit a bounded snapshot of that workspace’s normal and sensitive memory content to Cloud for managed compute.`,
        `Cloud Sync: ${CLOUD_SYNC_PRIVACY_NOTICE}`,
      ],
      confirmLabel: 'Save and send policy',
    })) return;
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
      const failure = hostedFailure('automation', error);
      if (failure) {
        const notice = node('div', 'automation-run-result');
        renderHostedFailure(notice, failure);
        form.append(notice);
      }
      showNotice(`Could not save the hosted policy: ${error.message}`);
    }
  }

  async function loadHosted(kind) {
    if (kind === 'analytics' && state.analyticsStarting.has(state.workspace)) {
      byId('analytics-result').replaceChildren(empty('An Analytics request is already running. Reopen this tab if it does not update.'));
      return;
    }
    const request = beginScopedRequest(`hosted-${kind}`);
    const workspace = request.workspace;
    const cacheKey = `${kind}:${workspace}`;
    const target = byId(`${kind}-result`);
    if (kind === 'analytics') {
      const completed = state.analyticsResults.get(workspace);
      if (completed) {
        renderAnalyticsResult(target, completed.result, workspace, completed.stale);
        return;
      }
      const pendingJob = state.analyticsJobs.get(workspace);
      if (pendingJob) {
        await refreshAnalyticsResult(workspace, pendingJob);
        return;
      }
      const previousError = state.analyticsStartErrors.get(workspace);
      if (previousError) {
        renderAnalyticsStartError(target, workspace, previousError);
        return;
      }
    } else if (state.hostedLoaded.has(cacheKey)) return;
    target.replaceChildren(empty(`Checking ${kind} availability…`));
    try {
      if (kind === 'team') {
        const [auth, license] = await Promise.all([api('/auth/state'), api('/license')]);
        if (!isCurrentScopedRequest(request)) return;
        state.license = license;
        updatePlanBadge();
        renderSidebarCta();
        setDeploymentMode(auth.deployment_mode || 'local');
        renderTeamState(target, auth, license);
      } else {
        if (kind === 'analytics') state.analyticsStarting.add(workspace);
        const result = await api(`/${kind}?${query(workspace)}`,
          kind === 'analytics' ? { timeoutMs: 90_000 } : {});
        // A submitted Analytics job can outlive a workspace selection change. Keep its
        // identifier/result for that workspace even when the visible panel has moved on.
        if (kind !== 'analytics' && !isCurrentScopedRequest(request)) return;
        if (kind === 'analytics' && !state.workspaces.some(item => workspaceName(item) === workspace)) return;
        const showAnalytics = kind === 'analytics' && state.workspace === workspace
          && state.view === 'manage' && state.manageTab === 'analytics';
        if (kind === 'automation') renderAutomationPolicy(result, workspace);
        else if (kind === 'analytics' && result.pending === true) {
          const jobId = text(result.job_id);
          if (!/^[A-Za-z0-9_-]{1,64}$/.test(jobId)) {
            throw new Error('Engraphis Cloud did not return a usable Analytics job.');
          }
          state.analyticsJobs.set(workspace, jobId);
          state.analyticsStartErrors.delete(workspace);
          if (showAnalytics) renderAnalyticsPending(target, workspace, jobId, result.state);
        } else if (kind === 'analytics') {
          if (!hasAnalyticsMetrics(result)) {
            throw new Error('Engraphis Cloud did not return a completed Analytics result.');
          }
          const stale = result.state === 'stale';
          state.analyticsResults.set(workspace, { result, stale });
          state.analyticsStartErrors.delete(workspace);
          if (showAnalytics) renderAnalyticsResult(target, result, workspace, stale);
        } else renderObject(target, result, `${kind[0].toUpperCase()}${kind.slice(1)} status`);
      }
      if (kind !== 'analytics' && isCurrentScopedRequest(request)) state.hostedLoaded.add(cacheKey);
    } catch (error) {
      if (kind === 'analytics') {
        state.analyticsStartErrors.set(workspace, error);
        if (state.workspace === workspace && state.view === 'manage' && state.manageTab === 'analytics') {
          renderAnalyticsStartError(target, workspace, error);
        }
      } else if (isCurrentScopedRequest(request)) {
        const failure = hostedFailure(kind, error);
        if (failure) renderHostedFailure(target, failure);
        else target.replaceChildren(empty(`${kind[0].toUpperCase()}${kind.slice(1)} is not active: ${error.message}`));
      }
    } finally {
      if (kind === 'analytics') state.analyticsStarting.delete(workspace);
    }
  }
  function renderTeamState(target, auth, license) {
    target.replaceChildren();
    const access = licenseAccessState(license);
    const teamLive = licensePlanKey(license) === 'team' && licenseHasHostedAccess(license);
    const ends = licenseTrialEnds(license);
    const note = teamLive
      ? (access === 'trial'
        ? `Your free trial includes Team${ends ? ` until ${ends}` : ''}. Organizations, roles and seats are managed in Engraphis Cloud.`
        : 'Your Team subscription is active. Organizations, roles and seats are managed in Engraphis Cloud.')
      : access === 'lapsed'
        ? 'Your hosted subscription is not active, so Team features are paused until billing is up to date.'
        : 'Team is a hosted plan. Per-member agent accounts, roles, named seats and rotating device credentials are managed in Engraphis Cloud, not by this dashboard.';
    target.append(node('p', 'automation-policy-note', note), definitionList([
      ['Deployment', auth.deployment_mode === 'local' || !auth.deployment_mode ? 'Local · single user' : 'Hosted'],
      ['Dashboard access', auth.enabled ? 'Deployment token required' : 'Open on this machine'],
      ['Plan', licensePlanLabel(license)],
    ]));
  }

  function syncSummaryMessage(summary) {
    if (!summary) return 'No sync has run in this dashboard process.';
    const attempted = number(summary.attempted);
    const succeeded = number(summary.succeeded);
    const errors = Array.isArray(summary.errors) ? summary.errors : [];
    const complete = summary.complete === true
      || (summary.complete !== false && errors.length === 0 && succeeded >= attempted);
    if (complete && attempted === 0) {
      return 'Last sync found no eligible shared workspaces · 0/0 completed. Create a workspace, or share a personal Team workspace you own, then try again.';
    }
    const counts = `${succeeded}/${attempted} eligible workspaces completed`;
    const changes = `${number(summary.added)} added · ${number(summary.updated)} updated · ${number(summary.exported)} exported`;
    return `${complete ? 'Last sync complete' : 'Last sync incomplete'} · ${counts} · ${changes}${errors.length ? ` · ${errors.length} ${errors.length === 1 ? 'error' : 'errors'}` : ''}.`;
  }

  function renderSyncStatus(status, message = '') {
    state.syncStatus = status || {};
    const connected = state.syncStatus.available === true;
    const ready = connected && state.syncStatus.ready === true;
    const hasKey = state.syncStatus.has_key === true;
    const target = byId('sync-result');
    if (!target) return;
    target.replaceChildren();
    if (message) target.append(empty(message, 'form-error'));
    target.append(
      node('p', 'automation-policy-note', syncSummaryMessage(state.syncStatus.last)),
      definitionList([
        ['Connection', connected ? 'Connected' : 'Not connected'],
        ['Encryption key', hasKey ? 'Valid' : state.syncStatus.key_state === 'invalid'
          ? 'Invalid' : 'Not configured'],
        ['Sync readiness', ready ? 'Ready' : 'Needs setup'],
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
    run.disabled = !ready;
    actions.append(run);
    if (state.syncStatus.last && number(state.syncStatus.last.attempted) === 0) {
      actions.append(button('Review workspaces', 'secondary-button', () => switchManageTab('workspaces')));
    }
    if (!connected) {
      const url = safeUrl(state.syncStatus.upgrade_url) || hostedAccountUrl('sync');
      if (url) {
        const connect = node('a', 'secondary-button', 'Connect Engraphis Cloud');
        connect.href = url;
        connect.target = '_blank';
        connect.rel = 'noopener';
        actions.append(connect);
      }
    } else if (!ready) {
      const missingKey = !hasKey;
      const detail = missingKey
        ? state.syncStatus.key_state === 'invalid'
          ? 'The configured Cloud Sync encryption key is invalid. Replace it with a 32-byte URL-safe base64 key, then restart this dashboard.'
          : 'Cloud Sync needs a user-held encryption key. Configure ENGRAPHIS_SYNC_E2EE_KEY on this device, then restart this dashboard.'
        : 'Cloud Sync encryption support is unavailable. Install engraphis[cloud-sync] and restart this dashboard.';
      target.append(node('p', 'automation-policy-note', detail));
      const guide = node('a', 'secondary-button', 'Cloud Sync setup guide');
      guide.href = 'https://github.com/Coding-Dev-Tools/engraphis/blob/main/docs/SYNC.md#configure-a-customer-installation';
      guide.target = '_blank';
      guide.rel = 'noopener';
      actions.append(guide);
    }
    target.append(actions);
  }

  async function loadSync() {
    const request = beginScopedRequest('sync-status');
    const target = byId('sync-result');
    if (!target) return;
    target.replaceChildren(empty('Checking Cloud Sync connection…'));
    try {
      const status = await api('/sync/status', { signal: request.signal });
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
      showNotice(complete && number(summary.attempted) === 0
        ? 'No eligible shared workspaces were synced.'
        : complete
          ? 'Cloud Sync completed for every eligible workspace.'
          : 'Cloud Sync is incomplete. Review the status before retrying.');
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      renderSyncStatus(state.syncStatus || {}, `Cloud Sync failed: ${error.message}`);
      /* A refused sync gets the same classified next step as the other hosted features. */
      const failure = hostedFailure('sync', error);
      const target = byId('sync-result');
      if (failure && target) {
        const panel = node('div', 'automation-run-result');
        renderHostedFailure(panel, failure);
        target.append(panel);
      }
      showNotice(`Cloud Sync failed: ${error.message}`);
    }
  }

  function planPrices() {
    const annual = byId('billing-select').value === 'annual';
    return annual
      ? { free: '$0', pro: '$100 / owner / year', team: '$200 / seat / year' }
      : { free: '$0', pro: '$10 / owner / month', team: '$20 / seat / month' };
  }

  /* What each hosted plan adds, as docs/HOSTED_PLANS.md describes it. */
  const PLAN_BENEFITS = {
    pro: [
      'Hosted Cloud Sync across your installations',
      'Growth, retention, decay and entity Analytics',
      'Auto Consolidation with hosted retention policies',
      'Auto Dreaming with reviewable managed proposals',
      'Private account and billing support',
    ],
    team: [
      'Everything in Pro',
      'Hosted organizations, invitations and named seats',
      'Roles, scoped credentials and Team audit history',
    ],
  };

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
      if (PLAN_BENEFITS[plan.id]) {
        const list = node('ul', 'plan-benefit-list');
        list.setAttribute('aria-label', `${plan.name} includes`);
        PLAN_BENEFITS[plan.id].forEach(item => list.append(node('li', '', item)));
        card.append(list);
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

  const LICENSE_STATUS_NOTES = {
    past_due: 'the last payment did not go through',
    canceled: 'the subscription was cancelled',
    expired: 'the billing period ended',
    revoked: 'access was revoked',
  };
  const LICENSE_SOURCES = {
    environment: 'Operator override',
    override: 'Operator override',
    session: 'Cloud handshake',
    cloud: 'Cloud entitlements',
    connected: 'Not yet confirmed',
    local: 'Not connected',
  };

  function licenseTrialEnds(license = state.license) {
    const ends = Number(license && license.trial && license.trial.ends_at);
    const date = ends > 0 ? validDate(ends) : null;
    return date ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(date) : '';
  }

  function licensePlanLabel(license = state.license) {
    const access = licenseAccessState(license);
    const plan = (licensePlanKey(license) || 'pro').toUpperCase();
    if (access === 'active') return plan;
    if (access === 'trial') return `${plan} trial`;
    if (access === 'trial_expired') return `${plan} trial ended`;
    if (access === 'lapsed') return `${plan} inactive`;
    return 'Local core (free)';
  }

  /* Say why hosted features are locked and what fixes it — never a bare plan badge. */
  function renderLicenseStatus(license, error = null) {
    const target = byId('license-status-body');
    if (!target) return;
    target.replaceChildren();
    if (!license) {
      target.append(empty(`Plan status is unavailable${error ? `: ${error.message}` : '.'}`));
      return;
    }
    const access = licenseAccessState(license);
    const ends = licenseTrialEnds(license);
    const rows = [['Plan', licensePlanLabel(license)]];
    if (access !== 'inactive' && license.email && license.email !== 'trial') rows.push(['Hosted account', text(license.email)]);
    if (access === 'trial' && ends) rows.push(['Trial ends', ends]);
    else if (access !== 'inactive' && license.expires) {
      const expires = Number(license.expires) > 0 ? validDate(license.expires) : null;
      if (expires) rows.push(['Authorization expires', new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(expires)]);
    }
    if (license.plan_source) {
      rows.push(['Plan source', `${LICENSE_SOURCES[license.plan_source] || text(license.plan_source)}${license.plan_checked_at ? ` · confirmed ${relative(license.plan_checked_at)}` : ''}`]);
    }
    target.append(definitionList(rows));
    const callout = (title, detail) => {
      const box = node('p', 'license-callout');
      box.append(node('strong', '', title), document.createTextNode(detail));
      target.append(box);
    };
    if (license.error) callout('Hosted authorization unavailable', text(license.error));
    if (access === 'trial_expired') {
      callout(`Your free trial has ended${ends ? ` on ${ends}` : ''}`, 'Hosted features are locked. Everything you wrote is still in your local database and fully usable. The trial runs once per account, so restoring hosted features means subscribing.');
    } else if (access === 'lapsed') {
      const reason = LICENSE_STATUS_NOTES[license.entitlement_status];
      callout(`Your ${(licensePlanKey(license) || 'hosted').toUpperCase()} subscription is no longer active`,
        `${reason ? `${reason[0].toUpperCase()}${reason.slice(1)}, so hosted` : 'Hosted'} features are locked until billing is up to date. Local memories are unaffected.`);
    } else if (access === 'inactive') {
      callout('No hosted plan on this installation', 'The local memory engine is free and complete on its own. Cloud Sync, Analytics, Automation and Team administration run in Engraphis Cloud.');
    }
    const known = license.known_features && typeof license.known_features === 'object' ? license.known_features : {};
    const included = new Set(Array.isArray(license.features) ? license.features : []);
    if (access !== 'inactive' && Object.keys(known).length) {
      const list = node('ul', 'license-features');
      list.setAttribute('aria-label', 'Hosted features');
      Object.entries(known).forEach(([key, label]) => {
        const item = node('li', included.has(key) ? 'included' : '', `${included.has(key) ? '✓' : '○'} ${text(label)}`);
        item.setAttribute('aria-label', `${text(label)}: ${included.has(key) ? 'included' : 'not included'}`);
        list.append(item);
      });
      target.append(list);
    }
    if (access === 'active') target.append(node('p', 'plan-support', 'Thank you for supporting Engraphis. Your subscription funds hosted infrastructure and ongoing development.'));
  }

  async function loadPlans() {
    const request = beginScopedRequest('plans');
    let failure = null;
    try {
      const license = await api(`/license?${query(request.workspace)}`);
      if (!isCurrentScopedRequest(request)) return;
      state.license = license;
    } catch (error) {
      if (!isCurrentScopedRequest(request)) return;
      failure = error;
      state.license = { plan: 'free' };
    }
    updatePlanBadge();
    renderSidebarCta();
    renderLicenseStatus(failure ? null : state.license, failure);
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
    const setupNote = node('p', 'llm-setup-note', 'Choose a provider and model for the copyable configuration snippet. Save it in your owner-private ~/.engraphis/config.env, then restart Engraphis to apply the change.');
    const snippetLabel = node('label', 'llm-snippet-label', 'Owner-private config.env setup');
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
    renderRuntimeCard();
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
    if (enabled && !await dialogs.confirm({
      title: 'Turn on LLM extraction',
      message: `Turn on LLM extraction? ${EXTERNAL_LLM_PRIVACY_NOTICE}`,
      confirmLabel: 'Turn on extraction',
    })) return;
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
        /* A successful test can switch extraction on for this process; when the setting could
           not be written for restart, say so rather than letting it silently lapse. */
        const transient = result.auto_enabled && result.persisted === false;
        const suffix = transient
          ? ' Extraction is active for this process, but the setting could not be saved for restart. Set ENGRAPHIS_EXTRACTOR=llm_structured and ENGRAPHIS_LLM_AUTO_EXTRACT=1 in the deployment environment.'
          : result.auto_enabled ? ' Extraction is active for new ingested memories.' : '';
        setLlmTestResult(`Connected — ${result.provider}/${result.model}.${suffix}`, transient ? 'error' : 'ready');
      } else {
        setLlmTestResult(`Could not connect: ${result.error || 'Check the provider, model, API key, and network.'}`, 'error');
      }
    } catch (error) {
      setLlmTestResult(`Model connection failed: ${error.message}`, 'error');
    }
  }

  function switchView(view, { pushHistory = true } = {}) {
    const validViews = ['today', 'ask', 'library', 'connections', 'relations', 'specstudio', 'provenance', 'manage'];
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
    graphLifecycle.sync();
    if (state.workspace && view === 'relations') loadGraph();
    if (state.workspace && view === 'provenance' && state.provenanceTab === 'audit') loadAudit();
    if (state.workspace && view === 'manage') {
      loadSavings(state.refreshEpoch);
      loadManageTab(state.manageTab);
    }
    if (view === 'specstudio' && window.SpecStudio && typeof window.SpecStudio.resizeCanvas === 'function') {
      window.SpecStudio.setScope(state.workspace, state.project);
      window.SpecStudio.resizeCanvas();
    }
    window.scrollTo({ top: 0, behavior: 'instant' });
    const heading = byId(`${view}-title`);
    if (heading) {
      heading.setAttribute('tabindex', '-1');
      heading.focus({ preventScroll: true });
    }
  }

  const THEMES = ['slate', 'midnight', 'solarized', 'paper', 'sepia', 'matrix'];
  /* Theme names used by the retired Classic dashboard, read once so a person who chose a
     theme there keeps it. */
  const CLASSIC_THEMES = {
    dark: 'slate', light: 'paper', midnight: 'midnight', solarized: 'solarized', sepia: 'sepia', matrix: 'matrix',
  };

  function applyTheme(theme) {
    const selected = THEMES.includes(theme) ? theme : 'slate';
    document.body.dataset.theme = selected;
    byId('sidebar-theme-select').value = selected;
    try {
      localStorage.setItem('engraphis-ledger-theme', selected);
    } catch (_) {}
    if (state.graphEngine) state.graphEngine.setThemeColors(graphThemeColors());
    renderGraphLegend();
  }

  async function refreshBootstrap(preferred = '') {
    const bootstrap = (await api('/bootstrap?include_stats=false')) || {};
    renderUpdateBanner(bootstrap.update);
    if (typeof bootstrap.version === 'string' && bootstrap.version.trim()) {
      state.releaseVersion = bootstrap.version.trim();
    }
    renderEmbedderBanner(bootstrap.embedder);
    renderRuntimeCard();
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
      state.project = '';
      workflow.selectWorkspace('');
      state.stats = {};
      state.libraryTotal = 0;
      renderFirstMemoryJourney();
      renderWorkspaceNames();
      renderWorkspaceList();
      renderMetricValues({ memories: 0, total_rows: 0, workspaces: 0, sessions: 0 });
      byId('decision-list').replaceChildren(empty('Create a workspace in Manage to start reviewing memory.'));
      byId('review-status').textContent = 'Create a workspace to load its review state.';
      byId('review-refresh').disabled = true;
      const emptyActivity = node('tr');
      const emptyActivityCell = node('td', '', 'No workspace selected yet.');
      emptyActivityCell.colSpan = 5;
      emptyActivity.append(emptyActivityCell);
      byId('activity-body').replaceChildren(emptyActivity);
      byId('proactive-list').replaceChildren(empty('Create a workspace to see proactive context.'));
      byId('home-health').replaceChildren(empty('Create a workspace to see its memory health.'));
      byId('home-handoff').hidden = true;
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
    setConnection('Local engine connected');
    await selectWorkspace(selected);
  }

  let bootPromise = null;
  function boot() {
    if (!bootPromise) bootPromise = runBoot().finally(() => { bootPromise = null; });
    return bootPromise;
  }

  async function runBoot() {
    byId('today-date').textContent = new Intl.DateTimeFormat(undefined, { dateStyle: 'long' }).format(new Date());
    let theme = 'slate';
    try {
      theme = localStorage.getItem('engraphis-ledger-theme')
        || CLASSIC_THEMES[localStorage.getItem('engraphis-theme')] || theme;
    } catch (_) {}
    applyTheme(theme);
    startHealthChecks();
    try {
      const entry = new URL(location.href);
      // Links may name a workspace (for example from an approval prompt). Bootstrap
      // still validates the name against the authorized workspace list.
      let view = 'today';
      try {
        const saved = localStorage.getItem('engraphis-ledger-view');
        if (['today', 'ask', 'library', 'connections', 'relations', 'specstudio', 'provenance', 'manage'].includes(saved)) view = saved;
      } catch (_) {}
      const urlView = new URL(location.href).searchParams.get('view');
      switchView(['today', 'ask', 'library', 'connections', 'relations', 'specstudio', 'provenance', 'manage'].includes(urlView) ? urlView : view, { pushHistory: false });
      await refreshBootstrap(entry.searchParams.get('workspace') || '');
      state.booted = true;
      state.bootFailure = '';
      clearConnectionProblem();
      const requestedManageTab = entry.searchParams.get('tab');
      if (urlView === 'manage' && ['settings', 'sync', 'analytics'].includes(requestedManageTab)) {
        switchManageTab(requestedManageTab);
      }
    } catch (error) {
      if (error.status === 401 && await authenticateBrowser()) {
        location.reload();
        return;
      }
      state.bootFailure = error.status === 401 ? 'auth' : 'connection';
      showConnectionProblem(connectionProblemDetail(error));
    }
  }

  all('[data-view]').forEach(control => control.addEventListener('click', () => switchView(control.dataset.view)));
  all('[data-go]').forEach(control => control.addEventListener('click', () => switchView(control.dataset.go)));
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

  byId('workspace-select').addEventListener('change', async event => {
    const next = event.target.value;
    if (!await confirmDiscardEditor()) {
      event.target.value = state.workspace;
      return;
    }
    void selectWorkspace(next);
  });
  byId('ask-form').addEventListener('submit', askMemory);
  const updateJevRecallControls = () => {
    const enabled = byId('ask-jev-assisted').checked;
    byId('ask-jev-remote').disabled = !enabled;
    byId('ask-jev-classification').disabled = !enabled;
    if (!enabled) byId('ask-jev-remote').checked = false;
  };
  ['ask-jev-assisted', 'ask-jev-remote', 'ask-jev-classification'].forEach(id => {
    byId(id).addEventListener('change', () => {
      updateJevRecallControls();
      beginScopedRequest('ask');
      askRequests.reset();
      byId('answer-panel').replaceChildren(empty('Submit the question with the current Jev settings.'));
      byId('retrieval-list').replaceChildren(empty('The next answer will include its matching retrieval preview.'));
      byId('ask-status').textContent = 'Jev settings changed. Submit the question to apply them.';
    });
  });
  updateJevRecallControls();
  byId('review-refresh').addEventListener('click', () => { void loadReviewInbox(); });
  byId('library-filter').addEventListener('input', () => {
    resetMoveSelection();
    window.clearTimeout(librarySearchTimer);
    // Invalidate immediately: an earlier query must not paint while the new one debounces.
    beginScopedRequest('library');
    state.libraryLoading = Boolean(state.workspace);
    renderLibraryPaging();
    librarySearchTimer = window.setTimeout(() => refreshLibrary(), 250);
  });
  byId('library-type').addEventListener('change', () => refreshLibrary());
  byId('library-previous').addEventListener('click', () => refreshLibrary(state.libraryPage - 1));
  byId('library-next').addEventListener('click', () => refreshLibrary(state.libraryPage + 1));
  byId('library-refresh').addEventListener('click', () => refreshLibrary());
  byId('library-selection-toggle').addEventListener('click', () => {
    state.librarySelecting = !state.librarySelecting;
    resetMoveSelection();
    renderLibrary();
  });
  byId('library-move').addEventListener('click', openMemoryMove);
  byId('memory-move-target').addEventListener('change', invalidateMemoryMovePreview);
  byId('memory-move-preview-button').addEventListener('click', () => { void previewMemoryMove(); });
  byId('memory-move-form').addEventListener('submit', applyMemoryMove);
  byId('memory-move-cancel').addEventListener('click', closeMemoryMove);
  byId('memory-move-dialog').addEventListener('cancel', event => {
    event.preventDefault();
    if (!state.memoryMove || !state.memoryMove.applying) closeMemoryMove();
  });
  byId('first-memory-add').addEventListener('click', async () => {
    if (!await confirmDiscardEditor()) return;
    if (!state.workspace) {
      switchView('manage');
      switchManageTab('workspaces');
      byId('create-workspace-form').hidden = false;
      byId('new-workspace-name').focus();
      return;
    }
    switchView('library');
    openEditor();
  });
  byId('new-memory-button').addEventListener('click', async () => {
    if (await confirmDiscardEditor()) openEditor();
  });
  byId('editor-close').addEventListener('click', () => { void requestCloseEditor(); });
  byId('editor-cancel').addEventListener('click', () => { void requestCloseEditor(); });
  byId('editor-write-mode').addEventListener('click', () => {
    setEditorMode(false);
    byId('editor-memory-content').focus();
  });
  byId('editor-preview-mode').addEventListener('click', () => setEditorMode(true));
  ['editor-memory-title', 'editor-memory-type', 'editor-memory-content', 'editor-memory-importance',
    'editor-memory-scope'].forEach(id => {
    byId(id).addEventListener('input', refreshEditorDirty);
    byId(id).addEventListener('change', refreshEditorDirty);
  });
  document.addEventListener('keydown', event => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey || event.key.toLowerCase() !== 's') return;
    if (!editorOpen() || state.view !== 'library') return;
    event.preventDefault();
    if (state.editorSession && !state.editorSession.busy) byId('memory-editor').requestSubmit();
  });
  window.addEventListener('beforeunload', event => {
    // Leaving the page can also cancel a save that is still in flight.
    if (!editorDirty() && !(editorOpen() && state.editorSession.busy)) return;
    event.preventDefault();
    event.returnValue = '';
  });
  byId('editor-refresh').addEventListener('click', refreshEditorVersions);
  byId('memory-editor').addEventListener('submit', saveMemory);
  byId('editor-memory-importance').addEventListener('input', event => {
    const effective = graphSliderResponseValue(
      'editor-memory-importance', event.target.value, 0.5,
    );
    event.target.setAttribute('aria-valuetext', `${effective.toFixed(2)} importance`);
    const importanceOutput = byId('editor-memory-importance-output');
    if (importanceOutput) importanceOutput.textContent = effective.toFixed(2);
  });
  const importMenuWrap = byId('import-menu-button').closest('.menu-wrap');
  function setImportMenuOpen(open, restoreFocus = false) {
    const trigger = byId('import-menu-button');
    const menu = byId('import-menu');
    menu.hidden = !open;
    trigger.setAttribute('aria-expanded', String(open));
    if (open) menu.querySelector('button').focus();
    else if (restoreFocus) trigger.focus();
  }
  byId('import-menu-button').addEventListener('click', () => setImportMenuOpen(byId('import-menu').hidden));
  importMenuWrap.addEventListener('keydown', event => {
    const menu = byId('import-menu');
    if (menu.hidden) return;
    const items = [...menu.querySelectorAll('button')];
    const index = items.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      setImportMenuOpen(false, true);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      items[(index + step + items.length) % items.length].focus();
    }
  });
  ['pointerdown', 'focusin'].forEach(type => document.addEventListener(type, event => {
    if (!byId('import-menu').hidden && !importMenuWrap.contains(event.target)) setImportMenuOpen(false);
  }));
  const imports = window.EngraphisImports.create({
    api,
    workspace: () => state.workspace,
    notify: showNotice,
    onImported: async workspace => {
      if (workspace !== state.workspace) return;
      // Keep open drafts, including pending saves whose failure must retain the draft.
      if (editorOpen()) refreshLibrary();
      else await selectWorkspace(workspace);
    },
    confirmDerive: () => dialogs.confirm({
      title: 'Derive facts with your LLM provider?',
      message: ['Deriving facts sends the imported text to your configured LLM provider for extraction.', EXTERNAL_LLM_PRIVACY_NOTICE],
      confirmLabel: 'Send for extraction',
    }),
  });
  byId('import-quick-button').addEventListener('click', () => {
    setImportMenuOpen(false, true);
    imports.openQuick();
  });
  byId('obsidian-import-button').addEventListener('click', () => {
    setImportMenuOpen(false, true);
    openObsidianImport();
  });
  byId('import-sources-button').addEventListener('click', () => {
    setImportMenuOpen(false, true);
    imports.openSources();
  });
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
  byId('obsidian-report-filter').addEventListener('change', () => {
    obsidianImport.reportPage = 0;
    renderObsidianReport(obsidianImport.job || obsidianImport.preview);
  });
  [['obsidian-report-previous', -1], ['obsidian-report-next', 1]].forEach(([id, delta]) => {
    byId(id).addEventListener('click', () => {
      obsidianImport.reportPage = Math.max(0, obsidianImport.reportPage + delta);
      renderObsidianReport(obsidianImport.job || obsidianImport.preview);
    });
  });
  byId('obsidian-retry-progress').addEventListener('click', () => {
    if (!obsidianImport.running || !obsidianImport.jobId || obsidianImport.polling) return;
    window.clearTimeout(obsidianImport.poll);
    obsidianImport.pollFailures = 0;
    void pollObsidianImport(obsidianImport.jobId, obsidianImport.jobWorkspace);
  });

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
    const effectiveSpeed = graphSliderResponseValue('graph-flow-speed', speed, 45);
    byId('graph-flow-speed').value = String(speed);
    byId('graph-flow-speed-output').value = String(Math.round(speed));
    byId('graph-flow-speed-output').textContent = String(Math.round(speed));
    if (state.graphEngine) state.graphEngine.setSettings({ flowSpeed: effectiveSpeed });
    clearGraphSavedView();
    scheduleGraphPreferencesSave();
  });
  byId('graph-search').addEventListener('input', event => searchGraph(event.target.value));
  byId('graph-repo-filter').addEventListener('input', event => {
    if (state.graphEngine) state.graphEngine.setRepoFilter(event.target.value);
    clearGraphSavedView();
    scheduleGraphPreferencesSave();
    // Repository-scoped payloads need a server reload, but do not issue a 20k-node request
    // for every keystroke. The current input is still reflected immediately by the renderer.
    if (state.graphMode === 'full') {
      const candidate = (event.target.value || '').trim();
      if (candidate && !validatedGraphRepository(candidate)) {
        cancelGraphRepositoryReload();
        return;
      }
    }
    if (state.graphIncludeCode || state.graphMode === 'full') scheduleGraphRepositoryReload();
  });
  all('[data-graph-preset-choice]').forEach(control => control.addEventListener('click', () => {
    const preset = control.dataset.graphPresetChoice;
    /* Every node is its own presentation: selecting the Every node chip loads the
       complete LOD scene; the Show-all toggle remains the canonical exit that
       restores overview filters. Other layout presets while in Every-node re-run
       the seeded Every-node layout without leaving the presentation. */
    if (preset === 'every') {
      if (state.graphMode !== 'full') {
        byId('graph-preset').value = preset;
        clearGraphSavedView();
        syncGraphChoices();
        saveGraphPreferences();
        /* Every node means every node: entering the map clears the unlinked/degree
           filters that the overview uses, but remembers them so leaving restores the
           person's overview exactly as they had configured it. */
        if (!state.everyPriorFilters) {
          state.everyPriorFilters = {
            minDegree: Number(byId('graph-min-degree').value) || 0,
            unlinked: byId('graph-show-unlinked').getAttribute('aria-pressed') === 'true',
          };
        }
        setGraphMinDegree(0, false);
        setGraphShowUnlinked(true, false);
        cancelGraphRepositoryReload();
        state.graphMode = 'full';
        updateGraphModeControls();
        loadGraph({ force: true });
      } else {
        // Clicking Every node while already in Every-node exits back to overview,
        // mirroring the old Show-all toggle but now via the layout chip.
        if (state.everyPriorFilters) {
          const prior = state.everyPriorFilters;
          state.everyPriorFilters = null;
          setGraphMinDegree(prior.minDegree, false);
          setGraphShowUnlinked(prior.unlinked, false);
        }
        byId('graph-preset').value = 'galaxy';
        cancelGraphRepositoryReload();
        state.graphMode = 'overview';
        clearGraphSavedView();
        syncGraphChoices();
        saveGraphPreferences();
        updateGraphModeControls();
        loadGraph({ force: true });
      }
      return;
    }
    if (state.graphMode === 'full') {
      byId('graph-preset').value = preset;
      clearGraphSavedView();
      syncGraphChoices();
      saveGraphPreferences();
      if (state.graphEngine && state.graphEngine.setPreset) {
        const result = state.graphEngine.setPreset(preset);
        if (result && typeof result === 'object') syncGraphTuning(result);
      } else {
        syncGraphTuning(graphPresetTuning(preset));
      }
      updateGraphModeControls();
      if (state.graphEngine) state.graphEngine.setSizeBy(graphSizeBy());
      graphLifecycle.sync();
      return;
    }
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
    graphLifecycle.sync();
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
    renderGraphLegend();
  }));
  all('[data-graph-color-choice]').forEach(control => control.addEventListener('click', () => {
    byId('graph-color').value = control.dataset.graphColorChoice;
    if (state.graphEngine) state.graphEngine.setColorBy(control.dataset.graphColorChoice);
    clearGraphSavedView();
    syncGraphChoices();
    saveGraphPreferences();
    renderGraphTypeColors();
    renderGraphLegend();
  }));
  byId('graph-reset-colors').addEventListener('click', resetGraphColors);
  document.querySelector('.graph-stage').addEventListener('keydown', event => {
    if (event.target.id !== 'graph-canvas' || event.defaultPrevented
      || event.altKey || event.ctrlKey || event.metaKey) return;
    if (graphExplorer.handleKey(event)) event.preventDefault();
  });
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
    scheduleGraphPreferencesSave();
  });
  byId('graph-show-unlinked').addEventListener('click', event => {
    setGraphShowUnlinked(event.currentTarget.getAttribute('aria-pressed') !== 'true');
    clearGraphSavedView();
    saveGraphPreferences();
    if (state.graphMode !== 'full') loadGraph({ force: true });
  });
  byId('graph-depth').addEventListener('input', event => {
    setGraphDepth(event.target.value);
    clearGraphSavedView();
    scheduleGraphPreferencesSave();
  });
  GRAPH_TUNING.forEach(item => byId(item.id).addEventListener('input', event => {
    const value = setGraphTuningControl(item, event.target.value);
    const effectiveValue = graphSliderResponseValue(
      item.id, value, graphSliderResponseBaseline(item),
    );
    if (state.graphEngine) state.graphEngine.setSettings({ [item.key]: effectiveValue });
    clearGraphSavedView();
    scheduleGraphPreferencesSave();
  }));
  GRAPH_SPACETIME_TUNING.forEach(item => byId(item.id).addEventListener('input', event => {
    setGraphSpacetimeControl(item, event.target.value);
    /* Controls use human-scale values (G=100, mass=160, spring=32), while the engine API is
       normalized around 1. Apply the same conversion used during graph creation on every live
       input event; passing the raw slider value would immediately clamp G to 8 and mass to 16. */
    if (state.graphEngine) {
      const settings = graphSpacetimeEngineSettings();
      state.graphEngine.setSettings({ [item.key]: settings[item.key] });
    }
    clearGraphSavedView();
    scheduleGraphPreferencesSave();
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
    const next = !layers[layer];
    if (layer === 'code' && next && state.graphMode === 'full'
      && !validatedGraphRepository(byId('graph-repo-filter').value)) {
      showNotice('Choose an exact repository before adding its code overlay to All nodes.');
      byId('graph-repo-filter').focus();
      return;
    }
    layers[layer] = next;
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
    clearGraphSavedView();
    if (state.graphEngine) state.graphEngine.setBridges(event.target.checked);
    saveGraphPreferences();
  });
  byId('graph-collapse').addEventListener('change', event => {
    clearGraphSavedView();
    if (state.graphEngine) state.graphEngine.setCollapse(event.target.checked ? 'auto' : false);
    saveGraphPreferences();
  });
  byId('graph-as-of').addEventListener('change', event => {
    clearGraphSavedView();
    if (state.graphEngine) state.graphEngine.setAsOf(graphAsOfTimestamp());
    saveGraphPreferences();
    loadGraph({ force: true });
  });
  byId('graph-ghosts').addEventListener('change', event => {
    clearGraphSavedView();
    if (state.graphEngine) state.graphEngine.setGhosts(event.target.checked);
    saveGraphPreferences();
  });
  byId('graph-size').addEventListener('change', event => {
    clearGraphSavedView();
    if (state.graphEngine) state.graphEngine.setSizeBy(graphSizeBy());
    saveGraphPreferences();
  });
  const graphExportWrap = byId('graph-export').closest('.graph-export-wrap');
  byId('graph-export').addEventListener('click', () => {
    setGraphExportMenuOpen(byId('graph-export-menu').hidden);
  });
  graphExportWrap.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || byId('graph-export-menu').hidden) return;
    event.preventDefault();
    event.stopPropagation();
    setGraphExportMenuOpen(false, true);
  });
  document.addEventListener('focusin', event => {
    if (!byId('graph-export-menu').hidden && !graphExportWrap.contains(event.target)) {
      setGraphExportMenuOpen(false);
    }
  });
  document.addEventListener('pointerdown', event => {
    if (!byId('graph-export-menu').hidden && !graphExportWrap.contains(event.target)) {
      setGraphExportMenuOpen(false);
    }
  });
  byId('graph-export-png').addEventListener('click', () => {
    setGraphExportMenuOpen(false, true);
    exportGraphPng();
  });
  byId('graph-export-json').addEventListener('click', () => {
    setGraphExportMenuOpen(false, true);
    exportGraphJson();
  });
  byId('graph-connections-close').addEventListener('click', closeGraphConnections);
  byId('graph-connections-dialog').addEventListener('close', cancelGraphConnectionMemoryLoad);
  byId('graph-connections-focus').addEventListener('click', () => {
    const id = state.graphConnectionsFocusId;
    if (!id) return;
    closeGraphConnections();
    focusGraphNode(id, state.graphConnectionsFocusLabel || 'Selected entity');
  });
  byId('graph-connections-dialog').addEventListener('click', event => {
    if (event.target === event.currentTarget) closeGraphConnections();
  });
  restoreGraphPreferences();
  syncGraphChoices();

  byId('why-form').addEventListener('submit', whySearch);
  byId('timeline-form').addEventListener('submit', timelineSearch);
  byId('timeline-closed-only').addEventListener('change', () => {
    if (byId('timeline-input').value.trim()) byId('timeline-form').requestSubmit();
  });
  byId('verify-receipts').addEventListener('click', verifyReceipts);
  byId('export-receipts').addEventListener('click', exportReceipts);

  byId('create-workspace-toggle').addEventListener('click', () => {
    byId('create-workspace-form').hidden = !byId('create-workspace-form').hidden;
    if (!byId('create-workspace-form').hidden) byId('new-workspace-name').focus();
  });
  window.addEventListener('pagehide', flushGraphPreferencesSave);
  byId('create-workspace-form').addEventListener('submit', createWorkspace);
  byId('consolidate-form').addEventListener('submit', previewConsolidation);
  byId('consolidate-commit').addEventListener('click', commitConsolidation);
  ['consolidate-structured'].forEach(id => {
    byId(id).addEventListener('change', invalidateConsolidationReview);
  });
  byId('billing-select').addEventListener('change', renderPlans);
  byId('sidebar-theme-select').addEventListener('change', event => applyTheme(event.target.value));
  byId('connection-retry').addEventListener('click', () => { void retryConnection(); });
  byId('embedder-recheck').addEventListener('click', () => { void recheckEmbedder(); });
  boot();
})();
