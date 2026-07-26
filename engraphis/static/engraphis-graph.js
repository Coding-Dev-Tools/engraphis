/* Engraphis knowledge graph — the dashboard's opt-in force-graph engine.
   Restores the shipped behaviour: GRAPH_PRESETS, GSTYLE render modes (cyber/galaxy/solar/classic),
   STYLE_PAL / STYLE_LAYERS / STYLE_BG, COMMUNITY_PALS, GRAPH_HEAT, colour-by community/type/connections,
   GRAPH_PALETTES with per-entity-type overrides, d3 force wiring, directional particles, label ranking,
   hover neighbourhood highlight, freeze, fit and reheat. Values copied from dashboard.js.

   The public graph endpoint calls its fields `label`, `from` and `to`; the engine also
   accepts the renderer-friendly `name`, `source` and `target` aliases so it can be used
   with both the dashboard adapter and standalone scene payloads. */
(function () {
  const PRESETS = {
    original: { label: 'Original force', repel: 120, link: 30, gravity: 14, font: 13, size: 3, linkw: 1, labelDensity: 40, curve: 0, particles: 0 },
    compact: { label: 'Compact clusters', repel: 42, link: 20, gravity: 26, font: 12, size: 3, linkw: 0.7, labelDensity: 30, curve: 0.08, particles: 0 },
    communities: { label: 'Community islands', repel: 48, link: 16, gravity: 48, font: 12, size: 3, linkw: 0.72, labelDensity: 24, curve: 0.12, particles: 0 },
    radial: { label: 'Radial orbit', repel: 68, link: 26, gravity: 12, font: 13, size: 3, linkw: 0.75, labelDensity: 55, curve: 0.22, particles: 0 },
    constellation: { label: 'Constellation flow', repel: 34, link: 16, gravity: 38, font: 12, size: 3, linkw: 0.65, labelDensity: 35, curve: 0.32, particles: 2 },
    custom: { label: 'Custom tuning', curve: 0.1, particles: 0 }
  };

  const STYLE_PAL = {
    galaxy: { person_or_concept: '#b789ff', mention: '#7bb4ff', hashtag: '#ffcf6b', email: '#8aa2ff', organization: '#66e0d0', location: '#ff7ea8' },
    solar: { person_or_concept: '#ffb454', mention: '#3fd2c7', hashtag: '#ffd68a', email: '#8ea8ff', organization: '#5b9bff', location: '#ff8f6b' },
    cyber: { person_or_concept: '#ff3ea5', mention: '#b6ff3c', hashtag: '#ffe14d', email: '#8b7bff', organization: '#22e0ff', location: '#ff5c7a' }
  };
  const STYLE_LAYERS = {
    classic: { temporal: '#6f9fd8', entity: '#5aafb3', causal: '#d7a84b', semantic: '#8c83e8' },
    galaxy: { temporal: '#7bb4ff', entity: '#66e0d0', causal: '#ffcf6b', semantic: '#b789ff' },
    solar: { temporal: '#5b9bff', entity: '#3fd2c7', causal: '#ffb454', semantic: '#ffd68a' },
    cyber: { temporal: '#22e0ff', entity: '#b6ff3c', causal: '#ffe14d', semantic: '#ff3ea5' }
  };
  /* The per-style pane backgrounds are NOT defined here. `style-src-attr 'none'` forbids
     writing them onto the element, so dashboard.css owns them behind
     `#graph-net[data-graph-style="galaxy|solar|cyber"]` and this file only sets that
     attribute. Keeping a second copy of the gradients in JS would be dead drift. */
  const PALETTES = {
    theme: null,
    aurora: { person_or_concept: '#8b7cf6', mention: '#2dd4bf', hashtag: '#fbbf24', email: '#60a5fa', organization: '#f472b6', location: '#a3e635' },
    ocean: { person_or_concept: '#38bdf8', mention: '#2dd4bf', hashtag: '#facc15', email: '#818cf8', organization: '#22d3ee', location: '#34d399' },
    ember: { person_or_concept: '#f97316', mention: '#fb7185', hashtag: '#facc15', email: '#a78bfa', organization: '#ef4444', location: '#84cc16' },
    contrast: { person_or_concept: '#0072b2', mention: '#009e73', hashtag: '#e69f00', email: '#56b4e9', organization: '#cc79a7', location: '#d55e00' }
  };
  const THEME_ETYPE = { person_or_concept: '#8c83e8', mention: '#5aafb3', hashtag: '#d7a84b', email: '#6f9fd8', organization: '#58b882', location: '#df7478' };
  const COMMUNITY_PALS = {
    classic: ['#8c83e8', '#5aafb3', '#d7a84b', '#6f9fd8', '#58b882', '#df7478', '#b07de0', '#4fb0a0', '#e0894a', '#7c9be0', '#e06a9a', '#9ac25a'],
    galaxy: ['#b789ff', '#7bb4ff', '#66e0d0', '#ffcf6b', '#ff7ea8', '#8aa2ff', '#c98bff', '#5ad0e0', '#ffa0d0', '#9d7bff', '#6ad0b0', '#ffb060'],
    solar: ['#ffb454', '#3fd2c7', '#ffd68a', '#5b9bff', '#ff8f6b', '#8ea8ff', '#ffc98a', '#4fc0b0', '#ff9f5b', '#7fb0ff', '#ffd0a0', '#5ad0c0'],
    cyber: ['#ff3ea5', '#22e0ff', '#b6ff3c', '#ffe14d', '#8b7bff', '#ff5c7a', '#3cffd0', '#ff9ae0', '#5ce0ff', '#d0ff3c', '#ffb03c', '#a06bff']
  };
  const GRAPH_HEAT = ['#3f7bff', '#6a5cff', '#a24bff', '#e0479f', '#ff6b6b', '#ffc23d'];

  /* Flow particles are per *relation*, and force-graph advances every one of them on every
     frame — three particles on a few thousand relations is tens of thousands of animated
     objects and a canvas that stops responding. The classic renderer already refuses to draw
     them past this many links (`data.links.length>800` in dashboard.js's graphRender); the
     opt-in engine uses the same cutoff rather than inventing a second large-graph signal. */
  const PARTICLE_LINK_LIMIT = 800;

  /* The classic renderer's large-graph signal (`GPERF` in dashboard.js, set from the rendered
     data as `nodes>600 || links>2400`). Past it the classic path drops the galaxy starfield
     outright — `if(GPERF.large)return` in graphStyleBackground — because repainting 110 stars
     plus every node and link on every frame is what makes a big store unusable. The opt-in
     engine reuses the same thresholds rather than inventing a second signal. */
  const LARGE_NODE_LIMIT = 600;
  const LARGE_LINK_LIMIT = 2400;

  function idOf(value) { return value && typeof value === 'object' ? value.id : value; }
  function nodeName(node) { return String(node.name || node.label || node.id || ''); }
  function linkEndpoint(link, side) {
    return idOf(link[side] !== undefined ? link[side] : link[side === 'source' ? 'from' : 'to']);
  }
  function asOfValue(value) {
    if (value instanceof Date) return value.getTime();
    if (typeof value === 'number') return Number.isFinite(value) ? value * (value < 1e11 ? 1000 : 1) : null;
    if (typeof value === 'string' && value.trim()) {
      const numeric = Number(value);
      if (Number.isFinite(numeric)) return asOfValue(numeric);
      const parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    return null;
  }
  function temporalValue(item, key, fallback) {
    const value = item[key] !== undefined ? item[key] : item[key === 'valid_from' ? 'born' : 'closed'];
    if (value === undefined || value === null || value === '') return fallback;
    return asOfValue(value);
  }

  /* Node and link labels come from ingested memories, i.e. untrusted text. force-graph's
     tooltip renders a string label through `innerHTML` (see float-tooltip in
     vendor/force-graph.min.js), so every label handed to it must already be escaped. */
  function esc(value) {
    if (value === undefined || value === null) return '';
    return String(value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function hexRgb(c) {
    if (!c) return [140, 131, 232];
    if (c[0] === '#') {
      const hex = c.length === 4 ? c[1] + c[1] + c[2] + c[2] + c[3] + c[3] : c.slice(1, 7);
      const n = parseInt(hex, 16);
      if (!Number.isFinite(n)) return [140, 131, 232];
      return [n >> 16 & 255, n >> 8 & 255, n & 255];
    }
    const m = c.match(/\d+/g) || [140, 131, 232];
    return [+m[0], +m[1], +m[2]];
  }
  function alpha(c, a) { const [r, g, b] = hexRgb(c); return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')'; }
  function lighten(c, amt) { let [r, g, b] = hexRgb(c); r = Math.round(r + (255 - r) * amt); g = Math.round(g + (255 - g) * amt); b = Math.round(b + (255 - b) * amt); return 'rgb(' + r + ',' + g + ',' + b + ')'; }
  function contrastOn(c) { const [r, g, b] = hexRgb(c); return (0.2126 * r + 0.7152 * g + 0.0722 * b) > 150 ? '#111827' : '#f8fafc'; }

  function makeStars() {
    const a = [], c = ['#dfe6ff', '#dfe6ff', '#c9b6ff', '#a7c6ff', '#ffd9ef'];
    for (let i = 0; i < 110; i++) a.push({ x: (Math.random() - 0.5) * 1200, y: (Math.random() - 0.5) * 1200, r: Math.random() * 1.1 + 0.25, a: Math.random() * 0.7 + 0.25, tw: Math.random() * 1.6 + 0.4, ph: Math.random() * 6.28, c: c[i % c.length] });
    return a;
  }
  const STARS = makeStars();

  /* Relations that cross topics rather than describe one. The classic renderer keeps them
     visible and traversable but builds its *clustering* adjacency without them (`GCOMM_ADJ`
     in dashboard.js), because a single sparse `influences` edge otherwise fuses two unrelated
     topics into one connected component — one Community-Islands colour and one force centre
     for both. Same semantics here. */
  const CLUSTER_EXCLUDED_LABELS = { influences: true };
  function clustersAcross(link) {
    return !!(link && CLUSTER_EXCLUDED_LABELS[link.label]);
  }

  function communities(nodes, links) {
    const adj = {};
    // Traversal adjacency (hover neighbourhood, focus depth, bridges, betweenness) keeps every
    // relation; only the community BFS below reads `clusterAdj`.
    const clusterAdj = {};
    const nodesById = new Map(nodes.map(node => [node.id, node]));
    nodes.forEach(n => { adj[n.id] = []; clusterAdj[n.id] = []; });
    links.forEach(l => {
      const s = linkEndpoint(l, 'source'), t = linkEndpoint(l, 'target');
      if (adj[s]) adj[s].push(t);
      if (adj[t]) adj[t].push(s);
      if (clustersAcross(l)) return;
      if (clusterAdj[s]) clusterAdj[s].push(t);
      if (clusterAdj[t]) clusterAdj[t].push(s);
    });
    // Respect clusters supplied with the data (a store that already knows its topics);
    // otherwise fall back to connected-component BFS, as the dashboard does.
    if (nodes.length && nodes.every(n => typeof n.community === 'number')) return adj;
    const seen = new Set();
    const groups = [];
    nodes.forEach(n => {
      if (seen.has(n.id)) return;
      // Read head instead of Array#shift: shift() is O(n) per pop, which turns this BFS
      // quadratic on the large stores the dashboard is expected to open.
      const queue = [n.id];
      let head = 0;
      seen.add(n.id);
      while (head < queue.length) {
        const id = queue[head++];
        (clusterAdj[id] || []).forEach(next => { if (!seen.has(next)) { seen.add(next); queue.push(next); } });
      }
      // `queue` has accumulated the whole component by now, so it *is* the group.
      groups.push(queue);
    });
    /* Rank by size before the IDs become visible. `graphRenderLegend()` sorts communities by
       size and labels the largest "Cluster 1", while node colour indexes the palette by the
       community ID itself (`nodeColor` -> `commPal()[community % n]`). Assigning IDs in raw
       node order therefore let the legend describe one component with another's swatch
       whenever a smaller component happened to appear first in the payload. The classic
       renderer sorts its components the same way (`graphComputeCommunities` in dashboard.js),
       so largest == community 0 == palette slot 0 == "Cluster 1" on both paths. */
    groups.sort((a, b) => b.length - a.length);
    groups.forEach((group, index) => {
      group.forEach(id => { const node = nodesById.get(id); if (node) node.community = index; });
    });
    return adj;
  }

  function maxOf(values, floor) {
    // Math.max(...array) throws RangeError once the array outgrows the argument limit,
    // which a real store reaches long before the renderer gets slow.
    let best = floor;
    for (let i = 0; i < values.length; i++) if (values[i] > best) best = values[i];
    return best;
  }

  /* Brandes betweenness — which entity is the bridge whose loss would split a topic.
     Brandes is O(V·E); on a large store that is seconds of blocked main thread, so above
     BETWEENNESS_PIVOTS sources we run the standard pivot approximation over a deterministic,
     evenly-spaced sample. The score is only ever used as a *relative* size/highlight signal
     (it is normalised to the maximum), so a sampled estimate is fit for purpose. */
  const BETWEENNESS_PIVOTS = 220;
  const BETWEENNESS_BUDGET = 1.5e6;
  function betweenness(nodes, adj) {
    const bc = {};
    nodes.forEach(n => { bc[n.id] = 0; });
    // Each pivot costs O(V) just to initialise its bookkeeping, so cap pivots by total work
    // as well as by count: without the budget a 60k-entity store blocks the main thread for
    // ~25s. This is a relative sizing signal, so fewer pivots degrades quality, not truth.
    const pivots = Math.max(1, Math.min(
      BETWEENNESS_PIVOTS,
      Math.floor(BETWEENNESS_BUDGET / Math.max(1, nodes.length))
    ));
    const stride = nodes.length > pivots ? Math.ceil(nodes.length / pivots) : 1;
    for (let index = 0; index < nodes.length; index += stride) {
      const src = nodes[index];
      const stack = [], pred = {}, sigma = {}, dist = {}, delta = {};
      nodes.forEach(n => { pred[n.id] = []; sigma[n.id] = 0; dist[n.id] = -1; delta[n.id] = 0; });
      sigma[src.id] = 1; dist[src.id] = 0;
      const queue = [src.id];
      let head = 0;
      while (head < queue.length) {
        const v = queue[head++];
        stack.push(v);
        (adj[v] || []).forEach(w => {
          if (dist[w] < 0) { dist[w] = dist[v] + 1; queue.push(w); }
          if (dist[w] === dist[v] + 1) { sigma[w] += sigma[v]; pred[w].push(v); }
        });
      }
      while (stack.length) {
        const w = stack.pop();
        pred[w].forEach(v => { delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w]); });
        if (w !== src.id) bc[w] += delta[w];
      }
    }
    const max = maxOf(Object.values(bc), 1);
    nodes.forEach(n => { n.betweenness = bc[n.id] / max; });
    return bc;
  }

  /* Bridge edges (Tarjan): removing one disconnects part of the store. */
  function findBridges(nodes, links, adj) {
    const disc = {}, low = {}, parent = {}, bridges = new Set();
    const multiplicity = {};
    links.forEach(link => {
      const s = linkEndpoint(link, 'source'), t = linkEndpoint(link, 'target');
      const key = s < t ? s + '|' + t : t + '|' + s;
      multiplicity[key] = (multiplicity[key] || 0) + 1;
    });
    let timer = 0;
    // Iterative Tarjan. The recursive form recurses once per node along a path, so a
    // chain-shaped component of a few thousand entities overflows the call stack and takes
    // the whole render down with it — an explicit frame stack has no such ceiling.
    const visit = root => {
      const frames = [{ u: root, i: 0 }];
      disc[root] = low[root] = ++timer;
      while (frames.length) {
        const frame = frames[frames.length - 1];
        const u = frame.u, neighbors = adj[u] || [];
        if (frame.i < neighbors.length) {
          const v = neighbors[frame.i++];
          if (!disc[v]) {
            parent[v] = u;
            disc[v] = low[v] = ++timer;
            frames.push({ u: v, i: 0 });
          } else if (v !== parent[u]) {
            low[u] = Math.min(low[u], disc[v]);
          }
          continue;
        }
        frames.pop();
        const p = parent[u];
        if (p !== undefined) {
          low[p] = Math.min(low[p], low[u]);
          const key = p < u ? p + '|' + u : u + '|' + p;
          if (low[u] > disc[p] && multiplicity[key] === 1) {
            bridges.add(p + '|' + u);
            bridges.add(u + '|' + p);
          }
        }
      }
    };
    nodes.forEach(n => { if (!disc[n.id]) visit(n.id); });
    links.forEach(l => {
      const s = linkEndpoint(l, 'source'), t = linkEndpoint(l, 'target');
      l.bridge = bridges.has(s + '|' + t);
    });
    return bridges;
  }

  function create(el, options) {
    if (typeof ForceGraph === 'undefined') throw new Error('force-graph not loaded');
    if (!el || typeof el.getAttribute !== 'function') throw new Error('graph container missing');
    const opts = options || {};
    const state = {
      // Named `styleName`, not `style`: scripts/externalize_dashboard_assets.py scans this
      // asset for runtime inline-style mutation with a text pattern, and a plain data field
      // by the shorter name reads as one. The longer name keeps that gate honest.
      styleName: 'cyber', colorBy: 'community', palette: 'theme', overrides: {}, themeColors: {},
      settings: Object.assign({}, PRESETS.communities, { mode: 'communities', labels: false, flow: true, frozen: false }),
      minDegree: 1, showUnlinked: false, focusId: null, depth: 2, layers: { temporal: true, entity: true, causal: true, semantic: true, code: false },
      path: null, asOf: null, ghost: true, sizeBy: 'degree', bridges: false, suggestions: false, collapse: 'auto'
    };
    let raw = { nodes: [], links: [], suggestions: [] }, adj = {}, hilite = null, hoverSet = null, maxDeg = 1;
    let zoom = 1, collapsed = false;
    /* Recomputed from the *rendered* data on every render, exactly as the classic path
       recomputes GPERF — filters and focus can take a huge store down to a small view. */
    let large = false;
    let destroyed = false, running = true, fitTimer = 0, suspended = 0, pendingRender = null;
    let betweennessReady = false;
    const fg = ForceGraph()(el);
    const api = {};

    /* The dashboard already honours `prefers-reduced-motion` for the classic renderer; this
       engine must not quietly reintroduce perpetual motion for the same user. */
    function reduced() {
      if (typeof opts.reducedMotion === 'function') return !!opts.reducedMotion();
      try {
        return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
      } catch (e) { return false; }
    }
    /* force-graph already keeps redrawing while the simulation runs or any link still has
       particles in flight, so `autoPauseRedraw(false)` is only needed for paint this engine
       does behind its back: the galaxy starfield lives in onRenderFramePre and is invisible
       to that change detection. Everywhere else, letting force-graph park the redraw is what
       keeps a settled graph off the CPU. */
    function needsContinuousFrames() {
      return !reduced() && state.styleName === 'galaxy' && !large;
    }
    /* Betweenness is the one analysis that is superlinear in the store size, and nothing in
       the default view consumes it — the bridge overlay and betweenness-sizing are both off.
       Computing it lazily keeps opening the graph cheap; the first toggle pays for it once. */
    function ensureBetweenness() {
      if (betweennessReady) return;
      betweennessReady = true;
      betweenness(raw.nodes, adj);
    }
    /* Apply a batch of setters with exactly one render at the end. Each public setter renders
       on its own, so a single dashboard sync used to cost six full re-simulations (and six
       zoom-to-fit timers). The caller also states the intent explicitly, because the merged
       intent of the individual setters is not the caller's: `setSettings` always asks for a
       reheat, which would reheat even on a `render(false, false)` refresh. */
    function batch(fn, fit, reheat) {
      suspended++;
      try { fn(api); } finally {
        suspended--;
        pendingRender = null;
        render(!!fit, !!reheat);
      }
    }

    /* Priority mirrors the classic renderer's graphTypeColor(): an explicit user override wins,
       then a non-classic style's own palette, then the *active theme*. The theme tier is the
       reason `themeColors` exists — it cannot be folded into `overrides`, which outrank
       STYLE_PAL. The dashboard owns the CSS custom properties (`--entity-*`), so it supplies
       the resolved values through setThemeColors() on every applyTheme()/graphRecolor();
       THEME_ETYPE stays only as the standalone-embed fallback for a caller that never does. */
    function etypeColor(type) {
      if (state.overrides[type]) return state.overrides[type];
      if (state.styleName !== 'classic' && STYLE_PAL[state.styleName] && STYLE_PAL[state.styleName][type]) return STYLE_PAL[state.styleName][type];
      return state.themeColors[type] || THEME_ETYPE[type] || '#8c83e8';
    }
    function commPal() { return COMMUNITY_PALS[state.styleName] || COMMUNITY_PALS.classic; }
    function heatColor(node) {
      const t = (node.rank || 0) / Math.max(1, raw.nodes.length - 1);
      return GRAPH_HEAT[Math.min(GRAPH_HEAT.length - 1, Math.floor(t * GRAPH_HEAT.length))];
    }
    function nodeColor(node) {
      if (state.colorBy === 'community') { const p = commPal(); return p[(node.community || 0) % p.length]; }
      if (state.colorBy === 'connections') return heatColor(node);
      return etypeColor(node.etype);
    }
    function layerColor(layer) { return (STYLE_LAYERS[state.styleName] || STYLE_LAYERS.classic)[layer] || '#8c83e8'; }

    function born(item) { return temporalValue(item, 'valid_from', -Infinity); }
    function closed(item) { return temporalValue(item, 'valid_to', null); }
    function aliveAt(item, date) {
      const start = born(item), end = closed(item);
      return start <= date && (end === null || end > date);
    }

    function collapsedData(nodes, links) {
      const groups = {};
      nodes.forEach(n => {
        const c = n.community || 0;
        if (!groups[c]) groups[c] = { id: 'cluster-' + c, cluster: true, community: c, name: (n.topic || 'Cluster ' + (c + 1)), etype: n.etype, members: 0, degree: 0, betweenness: 0 };
        groups[c].members++;
        groups[c].degree += n.degree || 0;
        groups[c].betweenness = Math.max(groups[c].betweenness, n.betweenness || 0);
      });
      const cnodes = Object.values(groups);
      const seen = {};
      const clinks = [];
      // Indexed lookup, not Array#find per endpoint: auto-collapse fires on every zoom-out,
      // and the scan made that O(nodes x links) — a visible freeze on a real store.
      const byId = new Map(raw.nodes.map(n => [n.id, n]));
      links.forEach(l => {
        const s = byId.get(linkEndpoint(l, 'source'));
        const t = byId.get(linkEndpoint(l, 'target'));
        if (!s || !t) return;
        const a = 'cluster-' + (s.community || 0), b = 'cluster-' + (t.community || 0);
        if (a === b) return;
        const key = a < b ? a + '|' + b : b + '|' + a;
        if (seen[key]) { seen[key].weight++; return; }
        const link = { source: a, target: b, layer: l.layer, weight: 1, aggregate: true };
        seen[key] = link;
        clinks.push(link);
      });
      return { nodes: cnodes, links: clinks };
    }

    function visible() {
      const keepLayer = l => state.layers[l.layer] !== false;
      let nodes = raw.nodes.filter(n => (state.showUnlinked || n.degree > 0) && n.degree >= state.minDegree);
      if (state.repo) nodes = nodes.filter(n => (n.repo || n.topic || '').toLowerCase().includes(state.repo) || nodeName(n).toLowerCase().includes(state.repo));
      if (state.asOf !== null) {
        const live = nodes.filter(n => aliveAt(n, state.asOf));
        const ghosts = state.ghost ? nodes.filter(n => !aliveAt(n, state.asOf) && born(n) <= state.asOf).map(n => Object.assign(n, { ghost: true })) : [];
        live.forEach(n => { n.ghost = false; });
        nodes = live.concat(ghosts);
      } else {
        nodes.forEach(n => { n.ghost = false; });
      }
      if (state.focusId != null) {
        const keep = new Set([state.focusId]);
        let frontier = [state.focusId];
        for (let h = 0; h < state.depth; h++) {
          const next = [];
          frontier.forEach(id => (adj[id] || []).forEach(n => { if (!keep.has(n)) { keep.add(n); next.push(n); } }));
          frontier = next;
        }
        nodes = nodes.filter(n => keep.has(n.id));
      }
      const ids = new Set(nodes.map(n => n.id));
      let links = raw.links.filter(l => keepLayer(l) && ids.has(linkEndpoint(l, 'source')) && ids.has(linkEndpoint(l, 'target')));
      if (state.asOf !== null) {
        links.forEach(l => { l.ghost = !aliveAt(l, state.asOf); });
        if (!state.ghost) links = links.filter(l => !l.ghost);
        links = links.filter(l => born(l) <= state.asOf);
      } else {
        links.forEach(l => { l.ghost = false; });
      }
      if (state.suggestions && raw.suggestions) {
        raw.suggestions.forEach(s => {
          const source = linkEndpoint(s, 'source'), target = linkEndpoint(s, 'target');
          if (ids.has(source) && ids.has(target)) links = links.concat([Object.assign({}, s, { source, target, layer: 'semantic', suggested: true })]);
        });
      }
      if (collapsed) return collapsedData(nodes, links.filter(l => !l.suggested));
      return { nodes, links };
    }

    function applyForces() {
      const s = state.settings, mode = s.mode || 'compact';
      fg.d3Force('charge').strength(-s.repel);
      fg.d3Force('link').distance(s.link);
      if (typeof d3 === 'undefined') return;
      fg.d3Force('radial', null);
      if (mode === 'communities') {
        const target = node => {
          const c = node.community || 0;
          const ring = 240 + (c % 3) * 90;
          const a = (c * 2.399);
          return { x: Math.cos(a) * ring, y: Math.sin(a) * ring * 0.62 };
        };
        fg.d3Force('x', d3.forceX(n => target(n).x).strength(s.gravity / 100));
        fg.d3Force('y', d3.forceY(n => target(n).y).strength(s.gravity / 100));
      } else {
        const centering = mode === 'radial' ? Math.max(0.04, s.gravity / 300) : s.gravity / 100;
        fg.d3Force('x', d3.forceX(0).strength(centering));
        fg.d3Force('y', d3.forceY(0).strength(centering));
        if (mode === 'radial' && d3.forceRadial) fg.d3Force('radial', d3.forceRadial(n => Math.max(0, 5 - Math.min(5, n.degree || 0)) * Math.max(8, s.link * 0.72)).strength(0.32));
      }
      if (d3.forceCollide) fg.d3Force('collide', d3.forceCollide(n => n.radius + 1.5).iterations(2));
    }

    function styleBackground(ctx, scale) {
      if (state.styleName === 'galaxy') {
        /* Matches the classic path's `if(GPERF.large)return`. Paired with the `large` term in
           needsContinuousFrames(), this is what lets a big galaxy graph settle: the starfield
           is the only paint force-graph cannot see, so once it is skipped there is nothing
           left that requires a frame the vendor would not have scheduled itself. */
        if (large) return;
        const t = performance.now() / 1000;
        ctx.save();
        ctx.globalCompositeOperation = 'lighter';
        for (let i = 0; i < STARS.length; i++) {
          const s = STARS[i], al = s.a * (0.5 + 0.5 * Math.sin(t * s.tw + s.ph));
          if (al <= 0.02) continue;
          ctx.globalAlpha = al;
          ctx.beginPath();
          ctx.arc(s.x, s.y, s.r, 0, 6.2832);
          ctx.fillStyle = s.c;
          ctx.fill();
        }
        ctx.restore();
      } else if (state.styleName === 'solar') {
        ctx.save();
        const g = ctx.createRadialGradient(0, 0, 2, 0, 0, 130);
        g.addColorStop(0, 'rgba(255,192,112,.20)');
        g.addColorStop(0.6, 'rgba(255,150,80,.05)');
        g.addColorStop(1, 'rgba(255,150,80,0)');
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(0, 0, 130, 0, 6.2832);
        ctx.fill();
        ctx.strokeStyle = 'rgba(255,190,120,.10)';
        ctx.lineWidth = 1 / scale;
        [72, 132, 200, 286, 384].forEach(r => { ctx.beginPath(); ctx.ellipse(0, 0, r, r * 0.66, 0, 0, 6.2832); ctx.stroke(); });
        ctx.restore();
      }
    }

    function styleNode(node, ctx, scale) {
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      const focus = hoverSet && hoverSet.size > 1, neighbor = focus && hoverSet.has(node.id), dim = focus && !neighbor;
      let r = node.radius;
      const col = node.color, rich = node.id === hilite || neighbor || node.hub || node.degree >= 3;
      ctx.globalAlpha = node.ghost ? 0.22 : (dim ? 0.12 : 1);
      if (node.ghost) {
        ctx.lineWidth = 1.1 / scale;
        ctx.strokeStyle = col;
        ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 6.2832); ctx.stroke();
        ctx.globalAlpha = 1;
        return;
      }
      if (node.cluster) {
        const g = ctx.createRadialGradient(node.x, node.y, r * 0.2, node.x, node.y, r * 1.5);
        g.addColorStop(0, alpha(col, 0.9));
        g.addColorStop(0.7, alpha(col, 0.35));
        g.addColorStop(1, alpha(col, 0));
        ctx.fillStyle = g;
        ctx.beginPath(); ctx.arc(node.x, node.y, r * 1.5, 0, 6.2832); ctx.fill();
        ctx.fillStyle = contrastOn(col);
        ctx.font = '600 ' + Math.max(3, r * 0.55) + 'px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(node.members), node.x, node.y);
        ctx.font = '500 ' + Math.max(2.6, r * 0.4) + 'px system-ui, sans-serif';
        ctx.fillStyle = 'rgba(232,236,245,.9)';
        ctx.fillText(nodeName(node), node.x, node.y + r * 1.5 + r * 0.5);
        ctx.textAlign = 'left';
        ctx.globalAlpha = 1;
        return;
      }
      if (state.bridges && node.betweenness > 0.35) {
        ctx.save();
        ctx.strokeStyle = alpha('#ff5c7a', 0.75);
        ctx.lineWidth = 1.2 / scale;
        ctx.setLineDash([2 / scale, 2 / scale]);
        ctx.beginPath(); ctx.arc(node.x, node.y, r + 3 / scale, 0, 6.2832); ctx.stroke();
        ctx.restore();
      }
      if (state.styleName === 'galaxy') {
        if (rich) {
          ctx.save();
          ctx.globalCompositeOperation = 'lighter';
          const R = r * (node.id === hilite ? 4.4 : 3.0);
          const g = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, R);
          g.addColorStop(0, alpha(col, dim ? 0.15 : 0.6));
          g.addColorStop(0.42, alpha(col, dim ? 0.05 : 0.16));
          g.addColorStop(1, alpha(col, 0));
          ctx.fillStyle = g;
          ctx.beginPath();
          ctx.arc(node.x, node.y, R, 0, 6.2832);
          ctx.fill();
          ctx.restore();
        }
        ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 6.2832); ctx.fillStyle = col; ctx.fill();
        ctx.beginPath(); ctx.arc(node.x, node.y, Math.max(0.4, r * 0.4), 0, 6.2832); ctx.fillStyle = 'rgba(255,255,255,.9)'; ctx.fill();
      } else if (state.styleName === 'solar') {
        const sun = node.rank === 0;
        if (sun) r *= 1.7;
        if (rich) {
          ctx.save();
          ctx.globalCompositeOperation = 'lighter';
          const cc = sun ? '#ffcf6b' : col, R2 = r * (sun ? 3.4 : 2.1);
          const g2 = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, R2);
          g2.addColorStop(0, alpha(cc, dim ? 0.1 : (sun ? 0.6 : 0.3)));
          g2.addColorStop(1, alpha(cc, 0));
          ctx.fillStyle = g2;
          ctx.beginPath(); ctx.arc(node.x, node.y, R2, 0, 6.2832); ctx.fill();
          ctx.restore();
        }
        const sg = ctx.createRadialGradient(node.x - r * 0.4, node.y - r * 0.4, Math.max(0.1, r * 0.12), node.x, node.y, r);
        sg.addColorStop(0, lighten(sun ? '#ffe4ad' : col, 0.5));
        sg.addColorStop(1, sun ? '#e08a25' : col);
        ctx.fillStyle = sg;
        ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 6.2832); ctx.fill();
      } else if (state.styleName === 'cyber') {
        ctx.save();
        if (rich) { ctx.shadowColor = col; ctx.shadowBlur = dim ? 2 : r * 2.6; }
        ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 6.2832); ctx.fillStyle = col; ctx.fill();
        ctx.restore();
        ctx.beginPath(); ctx.arc(node.x, node.y, Math.max(0.4, r * 0.42), 0, 6.2832); ctx.fillStyle = '#eafcff'; ctx.fill();
      } else {
        ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 6.2832); ctx.fillStyle = col; ctx.fill();
        if (node.hub) { ctx.lineWidth = 0.8 / scale; ctx.strokeStyle = node.stroke; ctx.stroke(); }
      }
      if (node.id === hilite) {
        ctx.lineWidth = 1.3 / scale;
        ctx.strokeStyle = state.styleName === 'cyber' ? '#ffffff' : 'rgba(255,255,255,.9)';
        ctx.beginPath(); ctx.arc(node.x, node.y, r + 1.4 / scale, 0, 6.2832); ctx.stroke();
      }
      const showLabel = (state.settings.labels && node.degree >= Math.max(1, 12 - state.settings.labelDensity / 6)) || node.id === hilite || neighbor;
      if (showLabel && scale > 0.35) {
        const size = Math.max(2, state.settings.font / scale / 3.4);
        ctx.font = '500 ' + size + 'px system-ui, sans-serif';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = 'rgba(0,0,0,.5)';
        ctx.fillText(nodeName(node), node.x + r + 1.6 + 0.3, node.y + 0.3);
        ctx.fillStyle = node.id === hilite ? '#ffffff' : 'rgba(232,236,245,.86)';
        ctx.fillText(nodeName(node), node.x + r + 1.6, node.y);
      }
      ctx.globalAlpha = 1;
    }

    function applyChrome() {
      // Keep the asset compatible with `style-src-attr 'none'`: the CSP-safe dashboard
      // stylesheet owns the visual backgrounds, while the canvas owns the data-driven paint.
      el.setAttribute('data-graph-style', state.styleName);
    }

    /* force-graph parks its redraw loop as soon as the simulation settles and no particle is in
       flight (`autoPauseRedraw`), and it has no way to know that `hilite`/`hoverSet` — plain
       closure state read by the paint callbacks — changed. Re-setting an accessor to its own
       value is the vendor's own invalidation hook, so highlight changes still paint with
       reduced motion on, flow off, or a settled graph. */
    function invalidate() {
      if (destroyed) return;
      fg.nodeCanvasObject(fg.nodeCanvasObject());
    }

    function refreshColors() {
      const nodes = fg.graphData().nodes || [];
      nodes.forEach(n => { n.color = nodeColor(n); n.stroke = contrastOn(n.color); });
      invalidate();
    }

    function render(fit, reheat) {
      if (destroyed) return;
      if (suspended) {
        pendingRender = pendingRender ? [pendingRender[0] || fit, pendingRender[1] || reheat] : [fit, reheat];
        return;
      }
      const motion = !reduced();
      const data = visible();
      large = data.nodes.length > LARGE_NODE_LIMIT || data.links.length > LARGE_LINK_LIMIT;
      const sizeMetric = n => state.sizeBy === 'betweenness' ? (n.betweenness || 0) : ((n.degree || 0) / Math.max(1, maxDeg));
      data.nodes.forEach(n => {
        const base = (state.settings.size || 3);
        n.radius = n.cluster
          ? Math.max(3, base * (1.4 + Math.min(3, Math.sqrt(n.members || 1) * 0.7)))
          : Math.max(0.8, base * (0.55 + Math.min(1.6, sizeMetric(n) * 1.9)));
        n.color = nodeColor(n);
        n.stroke = contrastOn(n.color);
      });
      applyChrome();
      fg.graphData(data);
      applyForces();
      fg.autoPauseRedraw(!needsContinuousFrames());
      if (fg.d3AlphaDecay) fg.d3AlphaDecay(0.035);
      if (fg.d3VelocityDecay) fg.d3VelocityDecay(0.38);
      if (fg.linkCurvature) fg.linkCurvature((PRESETS[state.settings.mode] || PRESETS.compact).curve || 0);
      fg.linkDirectionalArrowLength(2.5).linkDirectionalArrowRelPos(1);
      if (fg.linkDirectionalParticles) {
        const flowing = state.settings.flow !== false
          && motion
          && data.links.length <= PARTICLE_LINK_LIMIT;
        const particles = !flowing
          ? 0
          : (state.styleName === 'cyber' ? 3 : ((PRESETS[state.settings.mode] || {}).particles || 2));
        fg.linkDirectionalParticles(l => l.suggested || l.ghost ? 0 : particles)
          .linkDirectionalParticleWidth(2)
          .linkDirectionalParticleColor(l => alpha(layerColor(l.layer), 0.95))
          .linkDirectionalParticleSpeed(l => 0.002 + ((state.settings.flowSpeed || 45) / 100) * 0.008);
      }
      if (reheat && motion && !state.settings.frozen && fg.d3ReheatSimulation) fg.d3ReheatSimulation();
      if ((state.settings.frozen || !motion) && fg.d3AlphaDecay) { /* keep painting, stop layout */ fg.d3AlphaDecay(1); }
      if (fit) {
        clearTimeout(fitTimer);
        fitTimer = setTimeout(() => { if (!destroyed) fg.zoomToFit(motion ? 600 : 0, 40); }, motion ? 320 : 0);
      }
      if (opts.onStats) opts.onStats({ nodes: data.nodes.length, links: data.links.length, total: raw.nodes.length, totalLinks: raw.links.length, preset: (PRESETS[state.settings.mode] || PRESETS.compact).label, collapsed: collapsed, ghosts: data.nodes.filter(n => n.ghost).length, bridges: data.links.filter(l => l.bridge).length, suggested: data.links.filter(l => l.suggested).length });
    }

    fg.backgroundColor('rgba(0,0,0,0)').nodeRelSize(1).autoPauseRedraw(true)
      /* force-graph's default `nodeLabel`/`linkLabel` is the literal accessor "name", and its
         tooltip renders a string label with innerHTML. Node names here are entity labels
         extracted from ingested memories — untrusted input — so both accessors are set
         explicitly and escaped rather than left on the vendor default. */
      .nodeLabel(node => esc(nodeName(node)))
      .linkLabel(link => esc(link && link.label ? link.label : ''))
      .onRenderFramePre((ctx, scale) => { try { styleBackground(ctx, scale); } catch (e) { } })
      .nodeCanvasObject((node, ctx, scale) => styleNode(node, ctx, scale))
      .nodePointerAreaPaint((node, color, ctx) => { ctx.fillStyle = color; ctx.beginPath(); ctx.arc(node.x, node.y, node.radius + 2, 0, 6.2832); ctx.fill(); })
      .linkColor(l => {
        const focus = hoverSet && hoverSet.size > 1;
        const s = linkEndpoint(l, 'source'), t = linkEndpoint(l, 'target');
        const active = !focus || s === hilite || t === hilite;
        if (l.suggested) return alpha('#ffffff', active ? 0.34 : 0.1);
        if (l.ghost) return alpha(layerColor(l.layer), 0.12);
        if (state.bridges && l.bridge) return alpha('#ff5c7a', active ? 0.95 : 0.5);
        const base = layerColor(l.layer);
        return active ? alpha(base, focus ? 0.85 : 0.4) : alpha(base, 0.06);
      })
      .linkLineDash(l => l.suggested ? [2, 2] : (l.ghost ? [1, 3] : null))
      .linkWidth(l => {
        const w = state.settings.linkw || 1;
        const focus = hoverSet && hoverSet.size > 1;
        const s = linkEndpoint(l, 'source'), t = linkEndpoint(l, 'target');
        if (l.aggregate) return Math.min(6, 0.6 + Math.log2(1 + (l.weight || 1)) * 1.4) * w;
        if (state.bridges && l.bridge) return 2.6 * w;
        if (!focus) return 0.82 * w;
        return (s === hilite || t === hilite) ? 2.4 * w : 0.4 * w;
      })
      .onNodeHover(node => {
        hilite = node ? node.id : null;
        hoverSet = node ? new Set([node.id].concat(adj[node.id] || [])) : null;
        el.classList.toggle('engraphis-graph-node-hover', !!node);
        invalidate();
      })
      .onNodeClick(node => {
        if (node.cluster) { collapsed = false; state.collapse = false; render(false, true); setTimeout(() => { fg.centerAt(node.x, node.y, 500); fg.zoom(1.6, 500); }, 60); if (opts.onCollapseChange) opts.onCollapseChange(false); return; }
        if (opts.onNodeClick) opts.onNodeClick(node);
      })
      .onNodeDragEnd(node => { node.fx = node.x; node.fy = node.y; })
      .onBackgroundClick(() => { if (opts.onBackgroundClick) opts.onBackgroundClick(); })
      .onZoom(z => {
        zoom = z.k || 1;
        if (state.collapse !== 'auto') return;
        const next = zoom < 0.55;
        if (next !== collapsed) {
          collapsed = next;
          render(false, true);
          if (opts.onCollapseChange) opts.onCollapseChange(collapsed);
        }
      });

    api.setData = data => {
      const inputNodes = Array.isArray(data && data.nodes) ? data.nodes : [];
      const nodes = inputNodes
        .filter(node => node && node.id != null)
        .map(node => Object.assign({}, node, { name: nodeName(node) }));
      const nodeIds = new Set(nodes.map(node => node.id));
      const links = (Array.isArray(data && (data.links || data.edges)) ? (data.links || data.edges) : [])
        .map(link => {
          const source = linkEndpoint(link, 'source'), target = linkEndpoint(link, 'target');
          return Object.assign({}, link, { source, target });
        })
        .filter(link => link.source != null && link.target != null && nodeIds.has(link.source) && nodeIds.has(link.target));
      const suggestions = (Array.isArray(data && data.suggestions) ? data.suggestions : [])
        .map(link => Object.assign({}, link, { source: linkEndpoint(link, 'source'), target: linkEndpoint(link, 'target') }))
        .filter(link => link.source != null && link.target != null);
      raw = { nodes, links, suggestions };
      adj = communities(raw.nodes, raw.links);
      const deg = {};
      raw.links.forEach(l => { const s = linkEndpoint(l, 'source'), t = linkEndpoint(l, 'target'); deg[s] = (deg[s] || 0) + 1; deg[t] = (deg[t] || 0) + 1; });
      raw.nodes.forEach(n => { n.degree = deg[n.id] || 0; n.betweenness = 0; });
      maxDeg = maxOf(raw.nodes.map(n => n.degree), 1);
      const ranked = [...raw.nodes].sort((a, b) => b.degree - a.degree);
      ranked.forEach((n, i) => { n.rank = i; n.hub = i < 6; });
      // Bridge *edges* are cheap (linear) and feed the stats readout, so they stay eager.
      // Betweenness is not: see ensureBetweenness.
      findBridges(raw.nodes, raw.links, adj);
      betweennessReady = false;
      if (state.bridges || state.sizeBy === 'betweenness') ensureBetweenness();
      render(true, true);
    };
    api.setSettings = patch => { Object.assign(state.settings, patch); render(false, patch.mode !== undefined); };
    api.setPreset = name => {
      const p = PRESETS[name] || PRESETS.compact;
      state.settings.mode = PRESETS[name] ? name : 'compact';
      ['repel', 'link', 'gravity', 'font', 'size', 'linkw', 'labelDensity'].forEach(k => { if (p[k] !== undefined) state.settings[k] = p[k]; });
      render(true, true);
      return { ...state.settings };
    };
    api.setStyle = name => { state.styleName = ['classic', 'galaxy', 'solar', 'cyber'].indexOf(name) < 0 ? 'cyber' : name; render(false, false); };
    api.setColorBy = name => { state.colorBy = name; refreshColors(); render(false, false); };
    api.setPalette = name => { state.palette = name; state.overrides = PALETTES[name] ? { ...PALETTES[name] } : {}; refreshColors(); };
    api.setTypeColor = (type, color) => { state.overrides[type] = color; state.palette = 'custom'; refreshColors(); };
    /* Rehydrating saved overrides is not a user edit, so it must not flip the palette
       selector to "custom" behind the user's back the way setTypeColor deliberately does. */
    api.setTypeColors = map => { Object.assign(state.overrides, map || {}); refreshColors(); };
    /* The active theme's resolved `--entity-*` values. Replaced wholesale rather than merged:
       a theme switch must not leave the previous theme's colour for a type the new one omits. */
    api.setThemeColors = map => { state.themeColors = map && typeof map === 'object' ? { ...map } : {}; refreshColors(); };
    /* One render for a whole batch of setters — see `batch`. */
    api.apply = (fn, fit, reheat) => { batch(fn, fit, reheat); };
    api.setHighlight = id => {
      hilite = id == null ? null : id;
      hoverSet = id == null ? null : new Set([id].concat(adj[id] || []));
      invalidate();
    };
    api.setScope = patch => { Object.assign(state, patch); render(false, true); };
    api.setLayers = layers => { state.layers = layers; render(false, false); };
    api.focus = id => { state.focusId = id; render(true, true); };
    api.clearFocus = () => { state.focusId = null; render(true, true); };
    api.fit = () => { if (!destroyed) fg.zoomToFit(reduced() ? 0 : 500, 40); };
    api.reheat = () => {
      if (destroyed || reduced()) return;
      raw.nodes.forEach(n => { n.fx = undefined; n.fy = undefined; });
      if (fg.d3ReheatSimulation) { fg.d3AlphaDecay(0.035); fg.d3ReheatSimulation(); }
    };
    api.freeze = on => {
      state.settings.frozen = on;
      if (on) { fg.d3Force('charge').strength(0); fg.d3AlphaDecay(1); return; }
      applyForces();
      if (reduced()) return;
      fg.d3AlphaDecay(0.035);
      if (fg.d3ReheatSimulation) fg.d3ReheatSimulation();
    };
    api.zoomToNode = id => {
      const n = raw.nodes.find(x => x.id === id);
      if (!n || !Number.isFinite(n.x) || !Number.isFinite(n.y)) return false;
      const duration = reduced() ? 0 : 500;
      fg.centerAt(n.x, n.y, duration);
      fg.zoom(3, duration);
      return true;
    };
    api.state = () => ({ ...state, collapsed });
    /* The engine clusters its own copies of the nodes, so a caller that renders a cluster
       legend from the source data would otherwise report a single community. */
    api.communityMap = () => {
      const map = {};
      raw.nodes.forEach(n => { map[n.id] = n.community || 0; });
      return map;
    };
    api.setGhosts = on => { state.ghost = on; render(false, false); };
    api.setRepoFilter = repo => { state.repo = (repo || '').trim().toLowerCase(); render(false, true); };
    api.setAsOf = date => { state.asOf = asOfValue(date); render(false, true); };
    api.setSizeBy = metric => {
      state.sizeBy = metric === 'betweenness' ? metric : 'degree';
      if (state.sizeBy === 'betweenness') ensureBetweenness();
      render(false, false);
    };
    api.setBridges = on => { state.bridges = on; if (on) ensureBetweenness(); render(false, false); };
    /* Forces the lazy analysis; the dashboard does not display these yet, so nothing calls it
       on the render path. Kept as the seam a "most load-bearing entity" panel would use. */
    api.metrics = () => {
      ensureBetweenness();
      return {
        top: [...raw.nodes].sort((a, b) => b.betweenness - a.betweenness).slice(0, 5)
          .map(n => ({ id: n.id, name: nodeName(n), score: n.betweenness })),
        bridges: raw.links.filter(l => l.bridge).length
      };
    };
    api.setSuggestions = on => { state.suggestions = on; render(false, true); };
    api.setCollapse = mode => {
      state.collapse = mode;
      const next = mode === true || (mode === 'auto' && zoom < 0.55);
      collapsed = next;
      render(true, true);
    };
    api.presets = PRESETS;
    api.resize = () => { measure(); };
    /* Leaving the graph view must stop the simulation loop. force-graph keeps a rAF alive
       for as long as it is resumed, so a hidden pane would otherwise repaint forever. */
    api.pause = () => {
      if (destroyed || !running) return;
      running = false;
      if (fg.pauseAnimation) fg.pauseAnimation();
    };
    api.resume = () => {
      if (destroyed || running) return;
      running = true;
      if (fg.resumeAnimation) fg.resumeAnimation();
      measure();
    };
    api.destroyed = () => destroyed;
    api.destroy = () => {
      if (destroyed) return;
      destroyed = true;
      clearTimeout(fitTimer);
      try {
        if (api._ro) { api._ro.disconnect(); api._ro = null; }
        // `_destructor` pauses the rAF and drops the graph data; it does not detach the
        // canvas, so clear the container too or a re-create leaves the old one attached.
        if (fg._destructor) fg._destructor();
        el.removeAttribute('data-graph-style');
        el.classList.remove('engraphis-graph-node-hover');
        el.innerHTML = '';
      } catch (e) { /* teardown is best-effort: never let it block a view change */ }
      raw = { nodes: [], links: [], suggestions: [] };
      adj = {};
      hilite = null;
      hoverSet = null;
    };

    // A hidden pane measures 0x0; writing that into force-graph collapses the canvas and
    // nothing restores it, so only a real box is ever applied.
    const measure = () => {
      if (destroyed) return;
      const w = el.clientWidth, h = el.clientHeight;
      if (w > 0 && h > 0) fg.width(w).height(h);
    };
    measure();
    requestAnimationFrame(() => { if (destroyed) return; measure(); fg.zoomToFit(reduced() ? 0 : 400, 40); });
    if (typeof ResizeObserver !== 'undefined') {
      api._ro = new ResizeObserver(() => measure());
      api._ro.observe(el);
    }
    applyChrome();
    return api;
  }

  window.EngraphisGraph = {
    create, PRESETS, PALETTES, STYLE_LAYERS, COMMUNITY_PALS, GRAPH_HEAT, THEME_ETYPE, STYLE_PAL,
    /* Pure helpers, exported so the offline test suite can assert real behaviour (escaping,
       component labelling, bridge detection, stack safety) without a browser or a bundler.
       Nothing in the dashboard uses these; treat them as the engine's unit-test seam. */
    _internals: {
      esc, hexRgb, alpha, contrastOn, communities, betweenness, findBridges, maxOf,
      nodeName, linkEndpoint, asOfValue
    }
  };
})();
