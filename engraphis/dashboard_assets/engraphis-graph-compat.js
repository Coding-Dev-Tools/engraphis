/* Engraphis graph compatibility layer.
   Lets engraphis-graph.js replace the graph block in dashboard.js without touching any
   call site: dashboard.js and index.html invoke ~60 `graph*` functions and read a handful
   of `G*` globals by name. This file defines those names and forwards them to the engine.

   Load order in index.html:
     <script src="/static/vendor/d3.min.js"></script>
     <script src="/static/vendor/force-graph.min.js"></script>
     <script src="/static/engraphis-graph.js"></script>
     <script src="/static/engraphis-graph-compat.js"></script>
     <script src="/static/dashboard.js"></script>

   Then delete lines ~453-1035 of dashboard.js (the graph block). Everything outside that
   block — the handler map, explorer, keyboard nav, toasts — keeps working unchanged. */
(function () {
  var GFX = null, EL = null, RAW = { nodes: [], links: [] }, STATS = {}, METRICS = {};
  var KEY_STYLE = 'engraphis-graph-style', KEY_COLORBY = 'engraphis-graph-colorby', KEY_COLORS = 'engraphis-graph-colors';
  var EG = window.EngraphisGraph;

  function read(key, fallback) { try { var v = localStorage.getItem(key); return v == null ? fallback : v; } catch (e) { return fallback; } }
  function write(key, value) { try { localStorage.setItem(key, value); } catch (e) { } }
  function $(id) { return document.getElementById(id); }
  // Node/entity names and types come from ingested (untrusted) memory content; every value
  // built into innerHTML below must be escaped, matching esc() in dashboard.js.
  function esc(value) {
    if (value === undefined || value === null) return '';
    return String(value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ── globals the dashboard reads directly ────────────────────────────────
  window.GRAPH_PRESETS = EG.PRESETS;
  window.GRAPH_PALETTES = EG.PALETTES;
  window.GRAPH_HEAT = EG.GRAPH_HEAT;
  window.COMMUNITY_PALS = EG.COMMUNITY_PALS;
  window.STYLE_LAYERS = EG.STYLE_LAYERS;
  window.STYLE_PAL = EG.STYLE_PAL;
  window.GSTYLE = read(KEY_STYLE, 'cyber');
  window.GCOLORBY = read(KEY_COLORBY, 'community');
  window.GRAPH = null;
  window.GMAXDEG = 1;
  window.GPERF = { large: false, dense: false };
  window.GSET = Object.assign({}, EG.PRESETS.communities, { mode: 'communities', labels: false, flow: true, frozen: false, flowSpeed: 45 });
  var OVERRIDES = (function () { try { return JSON.parse(read(KEY_COLORS, '{}')).colors || {}; } catch (e) { return {}; } })();
  var PALETTE = (function () { try { return JSON.parse(read(KEY_COLORS, '{}')).palette || 'theme'; } catch (e) { return 'theme'; } })();

  function persistColors() { write(KEY_COLORS, JSON.stringify({ palette: PALETTE, colors: OVERRIDES })); }

  // ── DOM readouts the old block maintained ───────────────────────────────
  function setStatus(text, busy) {
    ['graph-layout-status', 'galaxy-layout-status'].forEach(function (id) {
      var n = $(id); if (n) { n.textContent = text; n.classList.toggle('busy', !!busy); }
    });
    ['graph-net', 'galaxy-net'].forEach(function (id) { var n = $(id); if (n) n.setAttribute('aria-busy', String(!!busy)); });
  }
  function updateHud() {
    var mode = $('graph-hud-mode'), count = $('graph-hud-count'), badge = $('graph-performance-badge');
    var preset = EG.PRESETS[window.GSET.mode] || EG.PRESETS.compact;
    if (mode) mode.textContent = preset.label;
    if (count) count.textContent = (STATS.nodes || 0) + ' of ' + (STATS.total || 0) + ' entities · ' + (STATS.links || 0) + ' relations';
    if (badge) badge.textContent = window.GPERF.large ? 'Large graph mode' : 'Adaptive rendering';
  }
  function renderLegend() {
    var box = $('graph-legend'), countEl = $('graph-legend-count');
    if (!box) return;
    var items = [];
    if (window.GCOLORBY === 'community') {
      var pal = EG.COMMUNITY_PALS[window.GSTYLE] || EG.COMMUNITY_PALS.classic, sizes = {};
      RAW.nodes.forEach(function (n) { var c = n.community || 0; sizes[c] = (sizes[c] || 0) + 1; });
      items = Object.keys(sizes).sort(function (a, b) { return sizes[b] - sizes[a]; }).slice(0, 8)
        .map(function (c, i) { return { label: 'Cluster ' + (i + 1), color: pal[c % pal.length], n: sizes[c] }; });
    } else if (window.GCOLORBY === 'connections') {
      items = EG.GRAPH_HEAT.map(function (color, i) { return { label: ['fewest', 'low', 'mid', 'high', 'higher', 'most'][i] + ' links', color: color, n: '' }; });
    } else {
      var counts = {};
      RAW.nodes.forEach(function (n) { counts[n.etype] = (counts[n.etype] || 0) + 1; });
      items = Object.keys(counts).map(function (t) { return { label: String(t).replace(/_/g, ' '), color: window.graphTypeColor(t), n: counts[t] }; });
    }
    box.innerHTML = items.map(function (i) {
      return '<span class="graph-legend-item"><i style="background:' + esc(i.color) + '"></i>' + esc(i.label) + (i.n === '' ? '' : ' <b>' + esc(i.n) + '</b>') + '</span>';
    }).join('');
    if (countEl) countEl.textContent = items.length ? items.length + (items.length === 1 ? ' group' : ' groups') : '';
  }
  function renderStats() {
    var box = $('graph-stats');
    if (!box) return;
    var degs = RAW.nodes.map(function (n) { return n.degree || 0; }).sort(function (a, b) { return a - b; });
    var rows = [
      ['entities', RAW.nodes.length], ['relations', RAW.links.length],
      ['components', new Set(RAW.nodes.map(function (n) { return n.community || 0; })).size],
      ['unlinked', RAW.nodes.filter(function (n) { return !n.degree; }).length],
      ['median degree', degs.length ? degs[Math.floor(degs.length / 2)] : 0],
      ['shown', (STATS.nodes || 0) + ' / ' + (STATS.links || 0)]
    ];
    box.innerHTML = rows.map(function (r) { return '<div class="graph-stat"><span>' + r[0] + '</span><b>' + r[1] + '</b></div>'; }).join('');
  }
  function renderTop() {
    var box = $('graph-top'), countEl = $('graph-top-count');
    if (!box) return;
    var top = RAW.nodes.slice().sort(function (a, b) { return (b.degree || 0) - (a.degree || 0); }).slice(0, 8);
    var max = Math.max(1, top.length ? (top[0].degree || 1) : 1);
    box.innerHTML = top.map(function (n) {
      return '<button type="button" class="graph-top-row" data-node="' + esc(n.id) + '"><span>' + esc(n.name) + '</span>'
        + '<i style="width:' + Math.round((n.degree || 0) / max * 100) + '%"></i><b>' + (n.degree || 0) + '</b></button>';
    }).join('');
    if (countEl) countEl.textContent = RAW.nodes.length + ' entities';
    Array.prototype.forEach.call(box.querySelectorAll('[data-node]'), function (btn) {
      btn.addEventListener('click', function () { window.graphFocus(btn.getAttribute('data-node')); });
    });
  }
  function refreshReadouts() { updateHud(); renderLegend(); renderStats(); renderTop(); }

  // ── colour helpers (same names and semantics as the old block) ──────────
  window.graphValidColor = function (c) { return /^#[0-9a-f]{6}$/i.test(c || ''); };
  window.graphTypeLabel = function (t) { return String(t || 'entity').replace(/_/g, ' '); };
  window.graphTypeColor = function (type) {
    if (OVERRIDES[type]) return OVERRIDES[type];
    if (window.GSTYLE && window.GSTYLE !== 'classic' && EG.STYLE_PAL[window.GSTYLE] && EG.STYLE_PAL[window.GSTYLE][type]) return EG.STYLE_PAL[window.GSTYLE][type];
    return EG.THEME_ETYPE[type] || '#8c83e8';
  };
  window.graphContrastColor = function (color) {
    if (!window.graphValidColor(color)) return '#0e1014';
    var n = parseInt(color.slice(1), 16);
    return (0.2126 * (n >> 16) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) > 150 ? '#111827' : '#f8fafc';
  };
  window.ETYPE_COLOR = new Proxy({}, { get: function (_, t) { return window.graphTypeColor(t); } });
  window.graphNodeColor = function (node) {
    if (window.GCOLORBY === 'community') { var p = EG.COMMUNITY_PALS[window.GSTYLE] || EG.COMMUNITY_PALS.classic; return p[(node.community || 0) % p.length]; }
    if (window.GCOLORBY === 'connections') { var t = (node.rank || 0) / Math.max(1, RAW.nodes.length - 1); return EG.GRAPH_HEAT[Math.min(EG.GRAPH_HEAT.length - 1, Math.floor(t * EG.GRAPH_HEAT.length))]; }
    return window.graphTypeColor(node.etype);
  };
  window.graphCommunityPalette = function () { return EG.COMMUNITY_PALS[window.GSTYLE] || EG.COMMUNITY_PALS.classic; };
  window.graphHeatColor = function (node) { var t = (node.rank || 0) / Math.max(1, RAW.nodes.length - 1); return EG.GRAPH_HEAT[Math.min(EG.GRAPH_HEAT.length - 1, Math.floor(t * EG.GRAPH_HEAT.length))]; };

  window.graphSetTypeColor = function (type, color, persist) {
    if (!type || !window.graphValidColor(color)) return;
    OVERRIDES[type] = color.toLowerCase(); PALETTE = 'custom';
    if (GFX) GFX.setTypeColor(type, OVERRIDES[type]);
    if (persist) persistColors();
    renderLegend();
  };
  window.graphApplyPalette = function (name) {
    if (!Object.prototype.hasOwnProperty.call(EG.PALETTES, name)) name = 'theme';
    PALETTE = name; OVERRIDES = EG.PALETTES[name] ? Object.assign({}, EG.PALETTES[name]) : {};
    persistColors();
    if (GFX) GFX.setPalette(name);
    renderLegend();
  };
  window.graphResetColors = function () {
    window.graphApplyPalette('theme');
    if (typeof toast === 'function') toast('Node colors reset to the active theme', 'ok');
  };
  window.graphRecolor = function () { if (GFX) GFX.setColorBy(window.GCOLORBY); renderLegend(); };
  window.graphRefreshNodeColors = window.graphRecolor;
  window.graphUpdateColorSwatches = function () {
    var box = $('graph-color-controls');
    if (!box) return;
    var types = Object.keys(EG.THEME_ETYPE);
    box.innerHTML = types.map(function (type) {
      return '<label class="graph-color-item" title="Change ' + window.graphTypeLabel(type) + ' node color">'
        + '<input class="graph-color-input" type="color" data-etype="' + type + '" value="' + window.graphTypeColor(type) + '">'
        + '<span>' + window.graphTypeLabel(type) + '</span></label>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.graph-color-input'), function (input) {
      input.addEventListener('change', function () { window.graphSetTypeColor(input.getAttribute('data-etype'), input.value, true); });
    });
  };

  // ── style, colour-by, presets ───────────────────────────────────────────
  window.graphApplyStyleChrome = function () {
    var net = document.querySelector('.graph-network');
    if (net) ['classic', 'galaxy', 'solar', 'cyber'].forEach(function (n) { net.classList.toggle('graph-style-' + n, window.GSTYLE === n); });
    var sel = $('graph-style'); if (sel && sel.value !== window.GSTYLE) sel.value = window.GSTYLE;
  };
  window.graphSetStyle = function (name) {
    if (['classic', 'galaxy', 'solar', 'cyber'].indexOf(name) < 0) name = 'cyber';
    window.GSTYLE = name; write(KEY_STYLE, name);
    if (GFX) GFX.setStyle(name);
    window.graphApplyStyleChrome(); renderLegend(); window.graphUpdateColorSwatches();
  };
  window.graphSetColorBy = function (mode) {
    if (['type', 'community', 'connections'].indexOf(mode) < 0) mode = 'community';
    window.GCOLORBY = mode; write(KEY_COLORBY, mode);
    if (GFX) GFX.setColorBy(mode);
    var sel = $('graph-colorby'); if (sel && sel.value !== mode) sel.value = mode;
    renderLegend();
  };
  window.graphApplyPreset = function (name) {
    if (!GFX) return;
    var applied = GFX.setPreset(name);
    Object.assign(window.GSET, applied);
    window.graphSyncReadouts(); window.graphSyncPresetCards(); window.graphUpdateEditedBadge();
  };
  window.graphResetPreset = function () { window.graphApplyPreset(window.GSET.mode || 'communities'); };
  window.graphSet = function (key, value) {
    window.GSET[key] = value;
    if (['repel', 'link', 'gravity', 'font', 'size', 'linkw', 'labelDensity', 'flowSpeed'].indexOf(key) >= 0) window.GSET.mode = 'custom';
    if (GFX) GFX.setSettings(window.GSET);
    window.graphSyncReadouts(); window.graphUpdateEditedBadge();
  };
  window.graphToggleFlow = function (on) { window.GSET.flow = !!on; if (GFX) GFX.setSettings({ flow: !!on }); };
  window.graphToggleLabels = function (on) { window.GSET.labels = !!on; if (GFX) GFX.setSettings({ labels: !!on }); };
  window.graphToggleFreeze = function (on) {
    window.GSET.frozen = !!on;
    if (GFX) GFX.freeze(!!on);
    setStatus(on ? 'Layout frozen' : 'Layout running', !on);
  };

  // ── scope, focus, navigation ────────────────────────────────────────────
  window.graphFit = function () { if (GFX) GFX.fit(); };
  window.graphReheat = function () { if (GFX) { GFX.reheat(); window.GSET.frozen = false; setStatus('Layout running', true); } };
  window.graphFocus = function (id) {
    if (!GFX) return;
    if (GFX.reveal) GFX.reveal(id);
    else { GFX.focus(id); GFX.zoomToNode(id); }
  };
  window.graphClearFocus = function () { if (GFX) GFX.clearFocus(); };
  window.graphSearch = function (term) {
    if (!GFX || !term) return;
    var q = String(term).toLowerCase();
    var hit = RAW.nodes.filter(function (n) { return String(n.name).toLowerCase().indexOf(q) >= 0; })[0];
    if (hit) { GFX.zoomToNode(hit.id); window.graphSetHighlight(hit.id); }
    else if (typeof toast === 'function') toast('No entity matches "' + term + '"', 'warn');
  };
  // The engine exposes `setHighlight`/no `redraw` (a repaint-only `setSettings({})` triggers
  // the same render pass); `highlight`/`redraw` were never part of its public API and would
  // throw if called.
  window.graphSetHighlight = function (id) { if (GFX) GFX.setHighlight(id || null); };
  window.graphRedraw = function () { if (GFX) GFX.setSettings({}); };
  window.graphInvalidateData = function () { RAW = { nodes: [], links: [] }; };
  window.graphData = function () { return RAW; };
  window.graphSetLayoutStatus = setStatus;
  window.graphSetSimulationStatus = setStatus;
  window.graphUpdateHud = updateHud;
  window.graphRenderLegend = renderLegend;
  window.graphRefreshNodeMetrics = function () { if (GFX) GFX.setSettings({}); };
  window.graphApplyForces = function () { if (GFX) GFX.setSettings(window.GSET); };
  window.graphComputeCommunities = function () { };
  window.graphIndexComponents = function () { };
  window.graphIndexCommunities = function () { };
  window.graphRefreshComponentCenters = function () { };
  window.graphInjectCss = function () { };
  window.graphStyleBackground = function () { };
  window.graphStyleNode = function () { };
  window.graphMakeStars = function () { return []; };
  window.graphAlpha = function (color, a) {
    var hex = /^#([0-9a-f]{6})$/i.exec(color || '');
    if (hex) { var v = parseInt(hex[1], 16); return 'rgba(' + (v >> 16) + ',' + ((v >> 8) & 255) + ',' + (v & 255) + ',' + a + ')'; }
    return color;
  };
  window.graphLoadColorPreferences = function () { };
  window.graphSaveColorPreferences = persistColors;

  // ── mount / render ──────────────────────────────────────────────────────
  window.graphRender = function (fit, reheat) {
    EL = $('graph-net');
    if (!EL) return;
    if (typeof ForceGraph === 'undefined' || typeof d3 === 'undefined') { setStatus('Graph library unavailable', false); return; }
    var empty = $('graph-empty');
    if (!GFX) {
      GFX = EG.create(EL, {
        classicBg: getComputedStyle(document.body).getPropertyValue('--color-panel').trim() || '#16191f',
        onStats: function (s) { STATS = s; refreshReadouts(); },
        onMetrics: function (m) { METRICS = m; },
        onNodeClick: function (node) {
          if (typeof syncGraphExplorerSelection === 'function') syncGraphExplorerSelection(node.id);
          if (typeof graphNodeClick === 'function') graphNodeClick(node.label || node.name || node.id);
        },
        onBackgroundClick: function () { if (GFX) GFX.clearFocus(); }
      });
      GFX.setStyle(window.GSTYLE);
      GFX.setColorBy(window.GCOLORBY);
      if (PALETTE !== 'theme' && PALETTE !== 'custom') GFX.setPalette(PALETTE);
      Object.keys(OVERRIDES).forEach(function (t) { GFX.setTypeColor(t, OVERRIDES[t]); });
      window.graphApplyStyleChrome();
      window.graphUpdateColorSwatches();
    }
    var data = window.GRAPH || { nodes: [], links: [] };
    RAW = data;
    if (!data.nodes || !data.nodes.length) {
      if (empty) { empty.style.display = 'flex'; empty.textContent = 'No entities in this workspace yet.'; }
      setStatus('No entities', false);
      return;
    }
    if (empty) empty.style.display = 'none';
    window.GPERF.large = data.nodes.length > 1500;
    window.GPERF.dense = data.links.length > 4000;
    GFX.setSettings(window.GSET);
    GFX.setData(data);
    window.GMAXDEG = Math.max.apply(null, [1].concat(data.nodes.map(function (n) { return n.degree || 0; })));
    setStatus(window.GSET.frozen ? 'Layout frozen' : 'Layout running', !window.GSET.frozen);
    if (fit !== false) GFX.fit();
    refreshReadouts();
  };

  // Sync helpers the old block owned; kept as thin DOM writers so the rail stays honest.
  window.graphSyncReadouts = function () {
    [['graph-repel', 'repel'], ['graph-link', 'link'], ['graph-gravity', 'gravity'],
     ['graph-size', 'size'], ['graph-font', 'font'], ['graph-linkw', 'linkw'], ['graph-labeld', 'labelDensity']]
      .forEach(function (pair) { var n = $(pair[0]); if (n && n.value !== String(window.GSET[pair[1]])) n.value = window.GSET[pair[1]]; });
  };
  window.graphSyncPresetCards = function () {
    Array.prototype.forEach.call(document.querySelectorAll('[data-preset]'), function (card) {
      card.classList.toggle('active', card.getAttribute('data-preset') === window.GSET.mode);
    });
  };
  window.graphSyncColorSeg = function () { var s = $('graph-colorby'); if (s) s.value = window.GCOLORBY; };
  window.graphSyncStyleSeg = function () { var s = $('graph-style'); if (s) s.value = window.GSTYLE; };
  window.graphUpdateEditedBadge = function () {
    var badge = $('graph-preset-help');
    if (badge) badge.textContent = window.GSET.mode === 'custom' ? 'Custom tuning — press Reset to return to the preset' : (EG.PRESETS[window.GSET.mode] || {}).label || '';
  };
  window.graphDrawPresetThumbs = function () { };

  window.EngraphisGraphCompat = { instance: function () { return GFX; }, metrics: function () { return METRICS; } };
})();
