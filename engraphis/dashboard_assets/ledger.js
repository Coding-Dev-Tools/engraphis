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
    manageTab: 'workspaces',
    refreshEpoch: 0,
    graphWorkspace: '',
    graphData: null,
    graphDataMode: 'overview',
    graphDataIncludeCode: false,
    graphDataShowUnlinked: false,
    graphDataAsOf: null,
    graphMeta: null,
    graphMode: 'overview',
    graphShowUnlinked: false,
    graphEngine: null,
    graphLoadPromise: null,
    graphLoadWorkspace: '',
    graphLoadMode: '',
    graphLoadIncludeCode: false,
    graphLoadShowUnlinked: false,
    graphLoadAsOf: null,
    graphLoadController: null,
    graphConnectionsRequest: 0,
    graphConnectionsController: null,
    graphMetrics: {},
    graphFrozen: false,
    graphIncludeCode: false,
    graphSavedView: 'schema',
    consolidationReview: null,
    hostedLoaded: new Set(),
    license: null,
  };

  const byId = id => document.getElementById(id);
  const all = selector => [...document.querySelectorAll(selector)];
  const text = value => value == null ? '' : String(value);
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
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
  const GRAPH_INITIAL_NODE_LIMIT = 320;
  const GRAPH_FULL_NODE_LIMIT = 20_000;
  const GRAPH_LOAD_TIMEOUT_MS = 12_000;
  const GRAPH_FULL_LOAD_TIMEOUT_MS = 30_000;
  const GRAPH_CONNECTION_MEMORIES_TIMEOUT_MS = 8_000;
  const GRAPH_PREFERENCES_KEY = 'engraphis-ledger-graph-preferences-v1';
  const GRAPH_CUSTOM_VIEW_KEY = 'engraphis-ledger-graph-custom-view-v1';
  const GRAPH_LAYERS = ['temporal', 'entity', 'causal', 'semantic', 'code'];
  const GRAPH_DEFAULT_LAYERS = { temporal: true, entity: true, causal: true, semantic: true, code: false };
  const GRAPH_TUNING = [
    { id: 'graph-repel', key: 'repel', fallback: 48 },
    { id: 'graph-link', key: 'link', fallback: 16 },
    { id: 'graph-gravity', key: 'gravity', fallback: 48 },
    { id: 'graph-node-size', key: 'size', fallback: 3 },
    { id: 'graph-text-size', key: 'font', fallback: 12 },
    { id: 'graph-line-width', key: 'linkw', fallback: 0.72, precision: 2 },
    { id: 'graph-label-density', key: 'labelDensity', fallback: 24 },
  ];
  const GRAPH_PRESET_TUNING = {
    original: { repel: 120, link: 30, gravity: 14, font: 13, size: 3, linkw: 1, labelDensity: 40 },
    compact: { repel: 42, link: 20, gravity: 26, font: 12, size: 3, linkw: 0.7, labelDensity: 30 },
    communities: { repel: 48, link: 16, gravity: 48, font: 12, size: 3, linkw: 0.72, labelDensity: 24 },
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
      layers: { ...GRAPH_DEFAULT_LAYERS }, minDegree: 1, depth: 2, showUnlinked: false, includeCode: false,
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

  async function authenticateBrowser() {
    let token = '';
    try {
      const fragment = new URLSearchParams(location.hash.slice(1));
      token = fragment.get('token') || '';
      if (token) history.replaceState(null, '', `${location.pathname}${location.search}`);
    } catch (_) {}
    if (!token) token = window.prompt('Enter this deployment’s ENGRAPHIS_API_TOKEN:') || '';
    if (!token) return false;
    try {
      await api('/auth/session', { method: 'POST', body: { token } });
      token = '';
      return true;
    } catch (error) {
      token = '';
      showNotice(`Authentication failed: ${error.message}`);
      return false;
    }
  }

  let graphAssetsPromise = null;
  function loadScript(src, globalName) {
    if (window[globalName]) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = src;
      script.onload = () => window[globalName]
        ? resolve()
        : reject(new Error(`${globalName} did not register`));
      script.onerror = () => reject(new Error(`could not load ${src}`));
      document.head.append(script);
    });
  }

  function ensureGraphAssets() {
    if (window.ForceGraph && window.EngraphisGraph) return Promise.resolve();
    if (!graphAssetsPromise) {
      graphAssetsPromise = loadScript(
        '/v2-assets/vendor/d3.min.js?v=20260727-final',
        'd3',
      ).then(() => loadScript(
        '/v2-assets/vendor/force-graph.min.js?v=20260727-final',
        'ForceGraph',
      )).then(() => loadScript(
        '/v2-assets/engraphis-graph.js?v=20260730-drag-stability',
        'EngraphisGraph',
      ));
      graphAssetsPromise.catch(() => {});
    }
    return graphAssetsPromise;
  }

  function showNotice(message) {
    byId('notice-text').textContent = message;
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
    byId('connection-status').textContent = message;
    const dot = document.querySelector('.status-dot');
    dot.classList.toggle('unhealthy', !healthy);
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

  function renderProactive(memories) {
    const target = byId('proactive-list');
    target.replaceChildren();
    if (!memories.length) {
      target.append(empty('No proactive context is available.'));
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
      : state.memories.slice(0, 5);
    renderProactive(proactive);
    renderDecisions(proactive.length ? proactive : state.memories);
    renderActivity(auditResult.status === 'fulfilled' ? auditItems(auditResult.value) : []);
  }

  function renderWorkspaceNames() {
    all('[data-workspace-name]').forEach(element => {
      element.textContent = state.workspace || 'this workspace';
    });
  }

  function workspaceName(item) {
    return typeof item === 'string' ? item : item.name;
  }

  async function selectWorkspace(name) {
    if (!name) return;
    invalidateConsolidationReview();
    const epoch = ++state.refreshEpoch;
    closeGraphConnections();
    state.workspace = name;
    state.graphWorkspace = '';
    state.graphData = null;
    state.graphDataIncludeCode = false;
    state.graphDataShowUnlinked = false;
    state.selectedMemory = '';
    // Detail/editor handlers close over a memory record.  Clear both before the
    // workspace fetches begin so a stale form cannot write that record into the
    // newly selected workspace.
    state.editorMemory = null;
    byId('memory-editor').hidden = true;
    const memoryDetail = byId('memory-detail');
    memoryDetail.replaceChildren();
    memoryDetail.hidden = true;
    if (state.graphEngine) {
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
      await Promise.all([
        loadStats(name, epoch),
        loadMemories(name, epoch),
        loadToday(name, epoch),
      ]);
      if (epoch !== state.refreshEpoch) return;
      renderWorkspaceList();
      if (state.view === 'relations') await loadGraph();
      if (state.view === 'provenance' && state.provenanceTab === 'audit') await loadAudit();
      if (state.view === 'manage') await loadManageTab(state.manageTab);
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
    const filter = byId('library-filter').value.trim().toLowerCase();
    const type = byId('library-type').value;
    return state.memories.filter(memory => {
      const matchesText = !filter || `${memory.title || ''} ${memory.content || ''} ${memory.summary || ''}`
        .toLowerCase().includes(filter);
      return matchesText && (!type || memoryType(memory) === type);
    });
  }

  function renderLibrary() {
    const target = byId('library-list');
    target.replaceChildren();
    const memories = filteredMemories();
    byId('library-count').textContent = `${memories.length.toLocaleString()} ${memories.length === 1 ? 'memory' : 'memories'}`;
    if (!memories.length) {
      target.append(empty(state.memories.length ? 'No memories match these filters.' : 'No active memories in this workspace.'));
      return;
    }
    memories.forEach(memory => target.append(memoryCard(memory)));
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
        ]),
      );
      const actions = node('div', 'detail-actions');
      actions.append(
        button('Edit', 'secondary-button', () => openEditor(memory)),
        button(memory.pinned ? 'Unpin' : 'Pin', 'secondary-button', () => togglePin(memory)),
        button('View timeline', 'secondary-button', () => openMemoryTimeline(memory)),
        button('Forget', 'danger-button', () => forgetMemory(memory)),
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

  async function forgetMemory(memory) {
    if (!window.confirm(`Forget “${memory.title || memory.id}”? The record stays in temporal history but leaves live recall.`)) return;
    try {
      await api('/forget', {
        method: 'POST',
        body: { id: memory.id, workspace: state.workspace, reason: 'forgotten in Ledger' },
      });
      state.selectedMemory = '';
      byId('memory-detail').replaceChildren(empty('Memory moved out of live recall. Its history is retained.'));
      showNotice('Memory forgotten without hard deletion.');
      await selectWorkspace(state.workspace);
    } catch (error) {
      showNotice(`Could not forget memory: ${error.message}`);
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
    showNotice('');
    const k = number(byId('ask-k').value) || 5;
    byId('answer-panel').replaceChildren(empty('Searching, checking support and building citations…'));
    byId('retrieval-list').replaceChildren(empty('Retrieving candidate memories…'));
    try {
      const [answer, retrieval] = await Promise.all([
        api('/answer', {
          method: 'POST',
          body: { query: question, workspace: state.workspace, k: Math.max(8, k), max_citations: k },
        }),
        // The dashboard /recall route is deliberately read-only (reinforce=False).
        // Keep it alongside /answer for uncited raw candidates without a second
        // reinforcement of the memories that answer already cited.
        api(`/recall?q=${encodeURIComponent(question)}&${query()}&k=${Math.max(8, k)}`),
      ]);
      renderAnswer(answer);
      const target = byId('retrieval-list');
      target.replaceChildren();
      const memories = retrieval.memories || [];
      if (!memories.length) target.append(empty('No raw candidates were returned.'));
      else memories.forEach(memory => target.append(simpleMemoryCard(memory)));
    } catch (error) {
      byId('answer-panel').replaceChildren(empty(`Grounded Ask is unavailable: ${error.message}`));
      byId('retrieval-list').replaceChildren(empty('Raw retrieval did not complete.'));
    }
  }

  function graphNodes(payload) {
    const source = payload.nodes || payload.entities || [];
    return source.map(item => ({
      id: item.id,
      name: item.label || item.name || item.id,
      label: item.label || item.name || item.id,
      etype: item.etype || item.type || 'person_or_concept',
      nodeKind: item.node_kind || item.kind || '',
      degree: number(item.degree),
      community: Number.isFinite(Number(item.community)) ? Number(item.community) : undefined,
      repo: item.repo || '',
      topic: item.topic || '',
      valid_from: item.valid_from,
      valid_to: item.valid_to,
    }));
  }

  function graphLinks(payload) {
    const source = payload.edges || payload.links || [];
    return source.map((item, index) => ({
      id: item.id || `edge-${index}`,
      source: item.from || (item.source && (item.source.id || item.source)),
      target: item.to || (item.target && (item.target.id || item.target)),
      label: item.label || item.relation || 'related',
      layer: item.layer || 'semantic',
      valid_from: item.valid_from,
      valid_to: item.valid_to,
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
      const entry = connections.get(other.id) || { item: other, relations: new Set() };
      if (link.label) entry.relations.add(link.label);
      connections.set(other.id, entry);
    });
    return [...connections.values()].sort((left, right) => {
      const degree = number(right.item.degree) - number(left.item.degree);
      return degree || left.item.name.localeCompare(right.item.name);
    });
  }

  async function showGraphConnectionMemories(item) {
    if (!item || !item.id || !state.workspace) return;
    cancelGraphConnectionMemoryLoad();
    const request = ++state.graphConnectionsRequest;
    const workspace = state.workspace;
    const title = item.name || item.label || item.id;
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
        `/graph/entities/${encodeURIComponent(item.id)}/memories?${query(workspace)}${graphAsOfQuery()}`,
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
      button('Memories', 'secondary-button', () => showGraphConnectionMemories(item)),
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
    const preset = GRAPH_PRESET_LABELS[byId('graph-preset').value] || 'Islands';
    byId('graph-mode').textContent = `${full ? 'Full node graph' : 'Responsive overview'} · ${preset}`;
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
      preset: byId('graph-preset').value,
      style: byId('graph-style').value,
      color: byId('graph-color').value,
      palette: byId('graph-palette').value,
      flow: byId('graph-flow').getAttribute('aria-checked') === 'true',
      labels: byId('graph-labels').getAttribute('aria-checked') === 'true',
      frozen: state.graphFrozen,
      tuning: graphTuningSettings(),
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
    const preset = graphPreference('preset', byId('graph-preset').value,
      ['original', 'compact', 'communities', 'radial', 'constellation']);
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
    syncGraphTuning({
      ...graphPresetTuning(preset),
      ...(savedTuning && typeof savedTuning === 'object' ? savedTuning : {}),
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
    setGraphShowUnlinked(graphPreference('showUnlinked', state.graphShowUnlinked) === true);
    byId('graph-bridges').checked = graphPreference('bridges', byId('graph-bridges').checked) === true;
    byId('graph-collapse').checked = graphPreference('collapse', byId('graph-collapse').checked) === true;
    byId('graph-ghosts').checked = graphPreference('ghosts', byId('graph-ghosts').checked) !== false;
    byId('graph-size').value = graphPreference('size', byId('graph-size').value,
      ['degree', 'betweenness']);
    state.graphFrozen = graphPreference('frozen', false) === true;
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
    const asOf = typeof view.asOf === 'string' ? view.asOf : previousAsOf;
    const repoFilter = typeof view.repoFilter === 'string'
      ? view.repoFilter.slice(0, 200) : byId('graph-repo-filter').value;
    state.graphIncludeCode = typeof view.includeCode === 'boolean'
      ? view.includeCode : state.graphIncludeCode;
    state.graphFrozen = typeof view.frozen === 'boolean' ? view.frozen : state.graphFrozen;
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
          flow: byId('graph-flow').getAttribute('aria-checked') === 'true',
          labels: byId('graph-labels').getAttribute('aria-checked') === 'true',
          frozen: state.graphFrozen,
        });
        graph.setScope(graphScope());
        graph.setLayers(graphLayerState());
        graph.setRepoFilter(repoFilter);
        graph.setAsOf(graphAsOfTimestamp());
        graph.setSizeBy(byId('graph-size').value);
        graph.setBridges(byId('graph-bridges').checked);
        graph.setCollapse(byId('graph-collapse').checked ? 'auto' : false);
        graph.setGhosts(byId('graph-ghosts').checked);
      }, false, !state.graphFrozen);
      state.graphEngine.freeze(state.graphFrozen);
    }
    saveGraphPreferences();
    if (previousIncludeCode !== state.graphIncludeCode
      || previousShowUnlinked !== state.graphShowUnlinked || previousAsOf !== asOf) {
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
    setGraphMinDegree(1, false);
    setGraphDepth(2, false);
    setGraphShowUnlinked(false, false);
    setGraphLayers(GRAPH_DEFAULT_LAYERS);
    clearGraphSavedView();
    if (state.graphEngine) {
      state.graphEngine.apply(graph => {
        graph.setPreset(preset);
        graph.setSettings({ ...graphTuningSettings(), frozen: state.graphFrozen });
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

  async function loadGraph({ force = false } = {}) {
    if (!state.workspace) return;
    if (!force && state.graphWorkspace === state.workspace
      && state.graphDataMode === state.graphMode
      && state.graphDataIncludeCode === state.graphIncludeCode
      && state.graphDataShowUnlinked === state.graphShowUnlinked
      && state.graphDataAsOf === graphAsOfTimestamp() && state.graphData) {
      if (state.graphEngine) state.graphEngine.resize();
      return;
    }
    const targetWorkspace = state.workspace;
    const targetMode = state.graphMode;
    const targetIncludeCode = state.graphIncludeCode;
    const targetShowUnlinked = state.graphShowUnlinked;
    const targetAsOf = graphAsOfTimestamp();
    const fullGraph = targetMode === 'full';
    if (state.graphLoadPromise && state.graphLoadWorkspace === targetWorkspace
      && state.graphLoadMode === targetMode && state.graphLoadIncludeCode === targetIncludeCode
      && state.graphLoadShowUnlinked === targetShowUnlinked
      && state.graphLoadAsOf === targetAsOf) {
      return state.graphLoadPromise;
    }
    if (state.graphLoadPromise && state.graphLoadController) state.graphLoadController.abort();
    byId('graph-empty').hidden = false;
    byId('graph-empty').textContent = fullGraph
      ? 'Loading every available graph node…'
      : 'Loading the responsive evidence graph…';
    const task = (async () => {
      const controller = new AbortController();
      state.graphLoadController = controller;
      const timeout = window.setTimeout(
        () => controller.abort(),
        fullGraph ? GRAPH_FULL_LOAD_TIMEOUT_MS : GRAPH_LOAD_TIMEOUT_MS,
      );
      try {
        const limit = fullGraph ? GRAPH_FULL_NODE_LIMIT : GRAPH_INITIAL_NODE_LIMIT;
        const complete = fullGraph ? '&full=true' : '';
        const connectedOnly = !fullGraph && !targetShowUnlinked ? '&connected_only=true' : '';
        const includeCode = targetIncludeCode ? '&include_code=true' : '';
        const asOf = targetAsOf === null ? '' : `&as_of=${encodeURIComponent(targetAsOf / 1000)}`;
        const [payload] = await Promise.all([
          api(`/graph?${query(targetWorkspace)}&limit=${limit}${complete}${connectedOnly}${includeCode}${asOf}`, { signal: controller.signal }),
          ensureGraphAssets(),
        ]);
        if (state.workspace !== targetWorkspace || state.graphMode !== targetMode
          || state.graphIncludeCode !== targetIncludeCode
          || state.graphShowUnlinked !== targetShowUnlinked
          || graphAsOfTimestamp() !== targetAsOf) return;
        const data = { nodes: graphNodes(payload), links: graphLinks(payload), suggestions: payload.suggestions || [] };
        state.graphData = data;
        state.graphWorkspace = targetWorkspace;
        state.graphDataMode = targetMode;
        state.graphDataIncludeCode = targetIncludeCode;
        state.graphDataShowUnlinked = targetShowUnlinked;
        state.graphDataAsOf = targetAsOf;
        state.graphMeta = payload.meta || {
          nodes_available: data.nodes.length,
          nodes_complete: fullGraph,
        };
        if (state.graphEngine) state.graphEngine.destroy();
        if (typeof window.EngraphisGraph === 'undefined') throw new Error('graph engine asset is unavailable');
        state.graphEngine = window.EngraphisGraph.create(byId('graph-canvas'), {
          renderMode: targetMode,
          onNodeClick: item => openGraphConnections(item),
          onBackgroundClick: () => state.graphEngine && state.graphEngine.clearFocus(),
          onStats: graphStatsChanged,
          onMetrics: graphMetricsChanged,
          onCollapseChange: collapsed => {
            if (targetMode === 'overview') showNotice(collapsed ? 'Clusters collapsed for overview.' : '');
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
            flow: byId('graph-flow').getAttribute('aria-checked') === 'true',
            labels: byId('graph-labels').getAttribute('aria-checked') === 'true',
            frozen: state.graphFrozen,
          });
          graph.setScope(graphScope());
          graph.setLayers(graphLayerState());
          graph.setRepoFilter(byId('graph-repo-filter').value);
          graph.setAsOf(graphAsOfTimestamp());
          graph.setSizeBy(byId('graph-size').value);
          graph.setBridges(byId('graph-bridges').checked);
          graph.setCollapse(fullGraph ? false : (byId('graph-collapse').checked ? 'auto' : false));
          graph.setGhosts(byId('graph-ghosts').checked);
        }, false, false);
        state.graphEngine.setData(data);
        state.graphEngine.freeze(state.graphFrozen);
        byId('graph-empty').hidden = Boolean(data.nodes.length);
        if (!data.nodes.length) byId('graph-empty').textContent = 'No entities exist in this workspace yet.';
        updateGraphModeControls();
        updateGraphFacts(data);
        updateGraphLayerCounts(data, payload.layers);
      } catch (error) {
        if (state.workspace !== targetWorkspace || state.graphMode !== targetMode) return;
        byId('graph-empty').hidden = false;
        byId('graph-empty').textContent = error && error.name === 'AbortError'
          ? `${fullGraph ? 'Full graph' : 'Graph'} loading timed out. Choose Retry to try again.`
          : `Graph unavailable: ${error.message}`;
      } finally {
        window.clearTimeout(timeout);
        if (state.graphLoadController === controller) state.graphLoadController = null;
      }
    })();
    state.graphLoadWorkspace = targetWorkspace;
    state.graphLoadMode = targetMode;
    state.graphLoadIncludeCode = targetIncludeCode;
    state.graphLoadShowUnlinked = targetShowUnlinked;
    state.graphLoadAsOf = targetAsOf;
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
    showNotice('');
    const target = byId('why-result');
    target.replaceChildren(empty('Tracing the live belief and supersession chain…'));
    try {
      const payload = await api(`/why?q=${encodeURIComponent(question)}&${query()}&k=8`);
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
    showNotice('');
    target.replaceChildren(empty('Loading temporal history…'));
    try {
      const payload = await api(`/timeline?q=${encodeURIComponent(question)}&${query()}&limit=50`);
      let history = payload.history || [];
      if (supersessionsOnly) history = history.filter(item => item.valid_to || item.expired_at);
      renderMemoryCollection(target, history, supersessionsOnly ? 'No closed versions were found for this topic.' : 'No temporal history was found.');
    } catch (error) {
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
    const target = byId('audit-list');
    target.replaceChildren(empty('Loading audit records and receipts…'));
    try {
      const [audit, receipts] = await Promise.all([
        api(`/audit?${query()}&limit=100`),
        api(`/receipts?${query()}&limit=100`),
      ]);
      renderAuditCards(auditItems(audit), receiptItems(receipts));
    } catch (error) {
      target.replaceChildren(empty(`Could not load provenance records: ${error.message}`));
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
      supersede_sources: byId('consolidate-supersede').checked,
    };
  }

  function sameConsolidationOptions(left, right) {
    return Boolean(left && right)
      && left.workspace === right.workspace
      && left.infer === right.infer
      && left.structured === right.structured
      && left.supersede_sources === right.supersede_sources;
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

  function renderAutomationPolicy(policy) {
    const target = byId('automation-result');
    if (!target) return;
    target.replaceChildren();
    const form = node('form', 'automation-policy-form');
    form.dataset.lastRun = String(policy.last_run || '');
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

  async function saveAutomationPolicy(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const policy = {
      enabled: byId('automation-enabled').checked,
      cadence_hours: Math.max(1, Number(byId('automation-cadence').value) || 1),
      dream_enabled: byId('automation-dream').checked,
      dream_min_new: Math.max(1, Number(byId('automation-dream-min').value) || 1),
      dream_idle_minutes: Math.max(0, Number(byId('automation-dream-idle').value) || 0),
      infer: byId('automation-infer').checked,
    };
    if (policy.enabled && !window.confirm(
      `Save this hosted policy for ${state.workspace}? Engraphis will submit a bounded snapshot of that workspace’s normal and sensitive memory content to Cloud for managed compute.\n\nCloud Sync: ${CLOUD_SYNC_PRIVACY_NOTICE}`,
    )) return;
    const save = form.querySelector('button[type="submit"]');
    if (save) {
      save.disabled = true;
      save.textContent = 'Saving…';
    }
    try {
      const saved = await api(`/automation?${query()}`, { method: 'POST', body: policy });
      renderAutomationPolicy({ ...saved, last_run: form.dataset.lastRun });
      showNotice('Hosted maintenance policy saved to Engraphis Cloud.');
    } catch (error) {
      if (save) {
        save.disabled = false;
        save.textContent = policy.enabled ? 'Save & send policy to Cloud' : 'Save hosted policy';
      }
      showNotice(`Could not save the hosted policy: ${error.message}`);
    }
  }

  async function loadHosted(kind) {
    const target = byId(`${kind}-result`);
    if (state.hostedLoaded.has(`${kind}:${state.workspace}`)) return;
    target.replaceChildren(empty(`Checking ${kind} availability…`));
    try {
      if (kind === 'team') {
        const [auth, license] = await Promise.all([api('/auth/state'), api('/license')]);
        state.license = license;
        updatePlanBadge();
        renderSidebarCta();
        renderObject(target, {
          local_mode: auth.mode || 'open',
          hosted_team: Boolean(auth.hosted_team),
          cloud_access: Boolean(license.cloud_access_active),
          plan: license.plan || 'local',
        }, 'Connection state');
      } else {
        const result = await api(`/${kind}?${query()}`);
        if (kind === 'automation') renderAutomationPolicy(result);
        else renderObject(target, result, `${kind[0].toUpperCase()}${kind.slice(1)} status`);
      }
      state.hostedLoaded.add(`${kind}:${state.workspace}`);
    } catch (error) {
      target.replaceChildren(empty(`${kind[0].toUpperCase()}${kind.slice(1)} is not active: ${error.message}`));
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
    try {
      state.license = state.license || await api('/license');
    } catch (_) {
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

  function switchView(view) {
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
    if (view === 'relations') loadGraph();
    if (view === 'provenance' && state.provenanceTab === 'audit') loadAudit();
    if (view === 'manage') loadManageTab(state.manageTab);
    window.scrollTo({ top: 0, behavior: 'instant' });
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
    const bootstrap = await api('/bootstrap');
    renderUpdateBanner(bootstrap.update);
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
      switchView(view);
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
  byId('plan-badge').addEventListener('click', event => {
    if (event.currentTarget.dataset.opensAccount === 'true') return;
    event.preventDefault();
    switchView('manage');
    switchManageTab('plans');
  });
  all('[data-provenance]').forEach(control => control.addEventListener('click', () => {
    switchView('provenance');
    switchProvenanceTab(control.dataset.provenance);
  }));
  all('[data-provenance-tab]').forEach(control => control.addEventListener('click', () => switchProvenanceTab(control.dataset.provenanceTab)));
  all('[data-manage-tab]').forEach(control => control.addEventListener('click', () => switchManageTab(control.dataset.manageTab)));

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
  byId('graph-retry').addEventListener('click', () => loadGraph({ force: true }));
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
    if (state.graphEngine) state.graphEngine.setSizeBy(event.target.value);
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
  ['consolidate-structured', 'consolidate-supersede'].forEach(id => {
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
