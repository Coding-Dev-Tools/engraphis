/* Every-node renderer: an ultra high performance WebGL2 presentation for complete graphs.
   Design contract: ALL geometry is uploaded once and only re-uploaded when data, layout,
   colours, or filters change; the camera lives entirely in uniforms so pan/zoom stays
   frame-rate independent of node count; zoom-out readability comes from additive glow
   density instead of shrinking datasets; picking is a local spatial grid with no worker
   round-trip; labels are decluttered on a 2D overlay by screen-space occupancy.
   WebGL2 is required — hosts show their own unsupported card via onError. */
(function () {
  'use strict';

  const WORKER_URL = '/v2-assets/engraphis-graph-every-worker.js?v=20260822-every-17';
  const MAX_NODES = 20000;
  const MAX_LINKS = 200000;
  const LABEL_MAX = 220;
  const FLOW_EDGE_LIMIT = 900;
  const FLOW_FRAME_MS = 34;
  /* Zoom bands are RATIOS of the current camera scale to the fitted-scene scale, so the
     LOD behaviour is identical whether the world holds 500 or 20,000 nodes: below ~half
     of fit the scene melts into additive glow density; edges fade in from ~45% of fit. */
  const GLOW_END = 0.55;
  const EDGE_START = 0.45;

  const PALETTES = {
    cyber: ['#4bd8df', '#9a7cff', '#ed6fc2', '#6fe6b0', '#f0c674', '#6ba8ff'],
    galaxy: ['#72a8ff', '#9a87ff', '#d987ff', '#59d5e7', '#8ee3c7', '#f4c978'],
    solar: ['#e8a05c', '#e17f65', '#f2c66d', '#d36d8f', '#d9d28b', '#e99767'],
    classic: ['#9ab2c7', '#839db2', '#b0a4c8', '#7aa7a6', '#c0aa7b', '#8aa6c9'],
  };
  const TYPE_COLORS = { person_or_concept: '#8d82e3', mention: '#5ba1a6', hashtag: '#c9a15b', email: '#8eb3e6', organization: '#d48173', location: '#7ebf8e', memory: '#5ba1a6', repo: '#c9a15b', file: '#8eb3e6' };
  const PRESETS = {
    galaxy: { repel: 60, link: 8, gravity: 48, font: 12, size: 3, linkw: 0.72 },
    original: { repel: 120, link: 30, gravity: 14, font: 13, size: 3, linkw: 1 },
    compact: { repel: 42, link: 20, gravity: 26, font: 12, size: 3, linkw: 0.7 },
    communities: { repel: 48, link: 16, gravity: 48, font: 12, size: 3, linkw: 0.72 },
    radial: { repel: 68, link: 26, gravity: 12, font: 13, size: 3, linkw: 0.75 },
    constellation: { repel: 34, link: 16, gravity: 38, font: 12, size: 3, linkw: 0.65 },
    every: { repel: 48, link: 16, gravity: 48, font: 12, size: 3, linkw: 0.72 },
  };

  const raf = window.requestAnimationFrame || (callback => window.setTimeout(callback, 16));
  const caf = window.cancelAnimationFrame || (handle => window.clearTimeout(handle));
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const color = value => /^#[0-9a-f]{6}$/i.test(String(value || '')) ? String(value) : '#86a8bf';
  const rgb = value => {
    const text = color(value).slice(1);
    return [parseInt(text.slice(0, 2), 16) / 255, parseInt(text.slice(2, 4), 16) / 255, parseInt(text.slice(4, 6), 16) / 255];
  };

  const NODE_VS = `#version 300 es
    in vec2 a_position; in vec3 a_color; in float a_size; in float a_flag;
    uniform vec2 u_camera; uniform float u_scale; uniform vec2 u_resolution; uniform float u_glow;
    out vec3 v_color; out float v_alpha; out float v_glow;
    void main(){
      bool alive = a_flag > 0.5;
      v_glow = u_glow;
      vec2 px = (a_position - u_camera) * u_scale + u_resolution * 0.5;
      vec2 clip = px / u_resolution * 2.0 - 1.0;
      gl_Position = alive ? vec4(clip.x, -clip.y, 0.0, 1.0) : vec4(0.0, 0.0, 2.0, 1.0);
      gl_PointSize = alive ? clamp(a_size * (1.0 + u_glow * 2.4) * u_scale, 1.0, 90.0) : 0.0;
      v_color = a_color;
      float dim = a_flag > 1.5 ? 0.10 : 1.0;
      v_alpha = alive ? mix(0.92, 0.15, u_glow) * dim : 0.0;
    }`;
  const NODE_FS = `#version 300 es
    precision mediump float;
    in vec3 v_color; in float v_alpha; in float v_glow; out vec4 outputColor;
    void main(){
      vec2 p = gl_PointCoord - 0.5;
      float r2 = dot(p, p);
      float core = 1.0 - smoothstep(0.16, 0.5, r2);
      float halo = 1.0 - smoothstep(0.0, 0.5, r2);
      float alpha = mix(core, halo * halo, v_glow) * v_alpha;
      if (alpha < 0.004) discard;
      outputColor = vec4(v_color, alpha);
    }`;
  const EDGE_VS = `#version 300 es
    in vec2 a_position; in float a_factor;
    uniform vec2 u_camera; uniform float u_scale; uniform vec2 u_resolution;
    uniform vec2 u_hotA; uniform float u_hotAOn;
    uniform vec2 u_hotB; uniform float u_hotBOn;
    out float v_factor;
    void main(){
      vec2 px = (a_position - u_camera) * u_scale + u_resolution * 0.5;
      vec2 clip = px / u_resolution * 2.0 - 1.0;
      gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
      /* An endpoint sitting exactly on the hovered or highlighted node's coordinates marks
         the relation hot without touching any buffer: edge endpoints share node positions. */
      bool hot = (u_hotAOn > 0.5 && distance(a_position, u_hotA) < 0.001)
        || (u_hotBOn > 0.5 && distance(a_position, u_hotB) < 0.001);
      v_factor = hot ? a_factor + 10.0 : a_factor;
    }`;
  const EDGE_FS = `#version 300 es
    precision mediump float;
    in float v_factor;
    uniform float u_edgeAlpha; uniform float u_focusFade; uniform float u_weightFloor;
    out vec4 outputColor;
    void main(){
      bool bridge = v_factor >= 9.5;
      /* A map reveals routes progressively: far out only the strongest relations survive,
         and the floor drops as you zoom until every relation is drawn. */
      if (!bridge && v_factor < u_weightFloor) discard;
      vec3 steel = vec3(0.389, 0.561, 0.651);
      vec3 gold = vec3(0.957, 0.827, 0.498);
      vec3 tint = mix(steel, gold, step(0.5, v_factor));
      float alpha = u_edgeAlpha * mix(1.0, 2.6, step(0.5, v_factor)) * (bridge ? 2.2 : u_focusFade);
      if (alpha < 0.004) discard;
      outputColor = vec4(tint, min(alpha, 0.85));
    }`;

  function create(element, options) {
    if (!element) throw new Error('every-node renderer requires a host element');
    const opts = options || {};
    const underlay = document.createElement('canvas');
    const canvas = document.createElement('canvas'), labels = document.createElement('canvas');
    canvas.className = 'engraphis-all-canvas';
    labels.className = 'engraphis-all-labels';
    underlay.className = 'engraphis-all-underlay';
    canvas.setAttribute('aria-hidden', 'true');
    labels.setAttribute('aria-hidden', 'true');
    underlay.setAttribute('aria-hidden', 'true');
    /* Region hulls live UNDER the GL scene; node points and lit paths sit in the middle;
       decluttered labels, flow markers, arrows, and the hover card paint on top. */
    element.replaceChildren(underlay, canvas, labels);
    element.appendChild(liveRegion);
    const underlayContext = underlay.getContext('2d');
    element.setAttribute('data-graph-style', opts.style || 'cyber');
    /* Screen-reader surface: the canvases are decorative; the live region announces the
       scene summary and hovered entity, and the host carries a descriptive label. */
    const liveRegion = document.createElement('div');
    liveRegion.className = 'sr-only';
    liveRegion.setAttribute('aria-live', 'polite');

    const gl = canvas.getContext('webgl2', { antialias: false, alpha: true, powerPreference: 'high-performance' });
    const labelContext = labels.getContext('2d');

    const state = {
      ids: [], idIndex: new Map(), labels: [], types: [], communities: [],
      positions: new Float32Array(0), bounds: null,
      nodeGhosts: new Uint8Array(0), nodeFlags: new Float32Array(0),
      nodeColors: new Float32Array(0), nodeSizes: new Float32Array(0),
      degrees: new Float32Array(0), betweenness: new Float32Array(0), evidenceMass: new Float32Array(0),
      topNodes: new Uint32Array(0),
      edgeSources: new Uint32Array(0), edgeTargets: new Uint32Array(0),
      edgeBridges: new Uint8Array(0), edgeWeights: new Float32Array(0),
      edgeRelations: [],
      totalLinks: 0, edgeVertexCount: 0,
      camera: { x: 0, y: 0, scale: 1 }, baseScale: 1, width: 1, height: 1, dpr: 1,
      styleName: opts.style || 'cyber', colorBy: 'community', typeColors: {}, themeColors: {}, palette: 'theme',
      settings: { labels: true, flow: false, flowSpeed: 45, frozen: false, mode: 'communities', repel: 48, link: 16, gravity: 48, font: 12, size: 3, linkw: 0.72 },
      sizeBy: 'degree', bridges: true, ghosts: true,
      scope: { minDegree: 0, showUnlinked: true, depth: 2 },
      collapse: false, collapsed: false,
      focus: -1, hover: -1, hoverPoint: [0, 0], focusPoint: [0, 0],
      neighbors: null, ready: false, visibleCount: 0,
      frame: 0, labelFrame: 0, flowPaintAt: 0, layoutPending: false, lastLabelKey: '',
      drag: null, pickGrid: null, pickDirty: true,
      destroyed: false, paused: false, unsupported: !gl, error: null,
      labelMetrics: new Map(),
    };

    let worker = null, nodeProgram = null, edgeProgram = null;
    let nodeBuffers = {}, edgeBuffers = {};
    let hitFrame = 0, pendingHit = null, layoutFrame = 0, pendingLayoutFit = false;

    const reducedMotion = () => {
      if (typeof opts.reducedMotion === 'function') return opts.reducedMotion() === true;
      if (opts.reducedMotion === true) return true;
      return typeof window.matchMedia === 'function'
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    };
    const screen = (x, y) => [(x - state.camera.x) * state.camera.scale + state.width / 2, (y - state.camera.y) * state.camera.scale + state.height / 2];
    const world = (x, y) => [(x - state.width / 2) / state.camera.scale + state.camera.x, (y - state.height / 2) / state.camera.scale + state.camera.y];
    const zoomRatio = () => {
      if (!state.baseScale) return 1;
      return state.camera.scale / state.baseScale;
    };
    const glowAmount = () => clamp((GLOW_END - zoomRatio()) / GLOW_END, 0, 1);

    function rebuildIdIndex() {
      state.idIndex = new Map();
      for (let index = 0; index < state.ids.length; index += 1) {
        if (!state.idIndex.has(state.ids[index])) state.idIndex.set(state.ids[index], index);
      }
    }
    function nodeAt(index) {
      return { id: state.ids[index], label: state.labels[index] || state.ids[index], type: state.types[index] || 'person_or_concept' };
    }
    function activePalette() {
      if (state.palette === 'ember') return PALETTES.solar;
      if (state.palette === 'ocean') return PALETTES.classic;
      if (state.palette === 'contrast') return ['#ffffff', '#8fe8ff', '#ffd166', '#ff7aa2', '#b9ffb0', '#d6b3ff'];
      if (state.palette === 'aurora') return PALETTES.cyber;
      return PALETTES[state.styleName] || PALETTES.cyber;
    }
    function nodeColor(index) {
      const item = nodeAt(index);
      const themed = state.typeColors[item.type] || state.themeColors[item.type] || TYPE_COLORS[item.type];
      if (state.colorBy === 'type' || state.palette === 'custom') return color(themed || item.color);
      const palette = activePalette();
      if (state.colorBy === 'connections') return palette[Math.min(5, Math.floor(Math.log1p(state.degrees[index] || 0) * 1.5))];
      const group = String(state.communities[index] || index);
      const hash = Array.from(group).reduce((sum, char) => ((sum * 31) + char.charCodeAt(0)) >>> 0, 7);
      return palette[hash % palette.length];
    }
    function metricValue(index) {
      if (state.sizeBy === 'betweenness') return state.betweenness[index] || 0;
      if (state.sizeBy === 'evidence_mass') return state.evidenceMass[index] || 0;
      return state.degrees[index] || 0;
    }
    function pointSize(index = 0) {
      const metric = Math.log1p(Math.max(0, metricValue(index)));
      return clamp(2.4 + Number(state.settings.size || 3) * 0.62 + Math.min(4.5, metric * 0.55), 2.5, 12);
    }

    /* ── GL plumbing ─────────────────────────────────────────────────────────────── */
    function shader(type, source) {
      const value = gl.createShader(type);
      gl.shaderSource(value, source);
      gl.compileShader(value);
      if (!gl.getShaderParameter(value, gl.COMPILE_STATUS)) throw new Error('every-node shader compilation failed');
      return value;
    }
    function program(vertexSource, fragmentSource) {
      const value = gl.createProgram();
      const vertexShader = shader(gl.VERTEX_SHADER, vertexSource);
      const fragmentShader = shader(gl.FRAGMENT_SHADER, fragmentSource);
      gl.attachShader(value, vertexShader);
      gl.attachShader(value, fragmentShader);
      gl.linkProgram(value);
      gl.detachShader(value, vertexShader);
      gl.detachShader(value, fragmentShader);
      gl.deleteShader(vertexShader);
      gl.deleteShader(fragmentShader);
      if (!gl.getProgramParameter(value, gl.LINK_STATUS)) {
        gl.deleteProgram(value);
        throw new Error('every-node shader link failed');
      }
      return value;
    }
    function initWebgl() {
      if (!gl) return;
      try {
        nodeProgram = program(NODE_VS, NODE_FS);
        edgeProgram = program(EDGE_VS, EDGE_FS);
        nodeBuffers.position = gl.createBuffer();
        nodeBuffers.color = gl.createBuffer();
        nodeBuffers.size = gl.createBuffer();
        nodeBuffers.flag = gl.createBuffer();
        edgeBuffers.position = gl.createBuffer();
        edgeBuffers.factor = gl.createBuffer();
        nodeBuffers.attrs = {
          position: gl.getAttribLocation(nodeProgram, 'a_position'),
          color: gl.getAttribLocation(nodeProgram, 'a_color'),
          size: gl.getAttribLocation(nodeProgram, 'a_size'),
          flag: gl.getAttribLocation(nodeProgram, 'a_flag'),
          camera: gl.getUniformLocation(nodeProgram, 'u_camera'),
          scale: gl.getUniformLocation(nodeProgram, 'u_scale'),
          resolution: gl.getUniformLocation(nodeProgram, 'u_resolution'),
          glow: gl.getUniformLocation(nodeProgram, 'u_glow'),
        };
        edgeBuffers.attrs = {
          position: gl.getAttribLocation(edgeProgram, 'a_position'),
          factor: gl.getAttribLocation(edgeProgram, 'a_factor'),
          camera: gl.getUniformLocation(edgeProgram, 'u_camera'),
          scale: gl.getUniformLocation(edgeProgram, 'u_scale'),
          resolution: gl.getUniformLocation(edgeProgram, 'u_resolution'),
          edgeAlpha: gl.getUniformLocation(edgeProgram, 'u_edgeAlpha'),
          focusFade: gl.getUniformLocation(edgeProgram, 'u_focusFade'),
          weightFloor: gl.getUniformLocation(edgeProgram, 'u_weightFloor'),
          hotA: gl.getUniformLocation(edgeProgram, 'u_hotA'),
          hotAOn: gl.getUniformLocation(edgeProgram, 'u_hotAOn'),
          hotB: gl.getUniformLocation(edgeProgram, 'u_hotB'),
          hotBOn: gl.getUniformLocation(edgeProgram, 'u_hotBOn'),
        };
      } catch (error) {
        nodeProgram = edgeProgram = null;
        state.error = { code: 'WEBGL2_UNSUPPORTED', message: String(error && error.message || error) };
        if (window.console && console.warn) console.warn('Every-node engine could not initialise WebGL2.', error);
      }
    }

    /* ── Visibility & uploads (change-driven, never per-frame) ──────────────────── */
    function passesFilters(index) {
      if (!state.ghosts && state.nodeGhosts[index]) return false;
      if (!state.scope.showUnlinked && !(state.degrees[index] > 0)) return false;
      if (state.scope.minDegree > 0 && !(state.degrees[index] >= state.scope.minDegree)) return false;
      return true;
    }
    function refreshVisibility(repaint = true) {
      let visible = 0;
      for (let index = 0; index < state.ids.length; index += 1) {
        const pass = passesFilters(index);
        state.nodeFlags[index] = pass ? 1 : 0;
        if (pass) visible += 1;
      }
      state.visibleCount = visible;
      state.pickDirty = true;
      uploadNodeMeta();
      applyHoverToFlags();
      if (repaint) scheduleLabels(true);
    }
    /* Hover dimming overlays the filter visibility: flag 1 stays bright (the hovered node
       plus its neighbours), flag 2 is a visible node pushed into the background. */
    function applyHoverToFlags() {
      if (!state.ready || !gl || !nodeProgram) return;
      /* The hovered node AND the last highlighted node anchor the lit neighbourhood, so a
         clicked selection keeps its paths visible after the pointer moves on. */
      const anchors = [];
      if (state.hover >= 0) anchors.push(state.hover);
      if (state.focus >= 0 && state.focus !== state.hover) anchors.push(state.focus);
      const keep = new Set(anchors);
      for (const anchor of anchors) {
        if (state.neighbors && state.neighbors[anchor]) {
          for (const neighbor of state.neighbors[anchor]) keep.add(neighbor);
        }
      }
      /* Flags hold filter visibility (0/1); 2 is a transient dim overlay that must clear
         before each re-application so ending a hover restores the full scene. */
      for (let index = 0; index < state.nodeFlags.length; index += 1) {
        if (state.nodeFlags[index] === 2) state.nodeFlags[index] = 1;
      }
      if (keep.size) {
        for (let index = 0; index < state.nodeFlags.length; index += 1) {
          if (state.nodeFlags[index] === 1 && !keep.has(index)) state.nodeFlags[index] = 2;
        }
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.flag);
      gl.bufferData(gl.ARRAY_BUFFER, state.nodeFlags, gl.DYNAMIC_DRAW);
    }
    function uploadNodePositions() {
      if (!gl || !nodeProgram) return;
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.position);
      gl.bufferData(gl.ARRAY_BUFFER, state.positions, gl.DYNAMIC_DRAW);
    }
    function uploadNodeMeta() {
      if (!gl || !nodeProgram) return;
      const count = state.ids.length;
      if (state.nodeColors.length !== count * 3) state.nodeColors = new Float32Array(count * 3);
      if (state.nodeSizes.length !== count) state.nodeSizes = new Float32Array(count);
      if (state.nodeFlags.length !== count) state.nodeFlags = new Float32Array(count);
      for (let index = 0; index < count; index += 1) {
        const tint = rgb(nodeColor(index));
        state.nodeColors[index * 3] = tint[0];
        state.nodeColors[index * 3 + 1] = tint[1];
        state.nodeColors[index * 3 + 2] = tint[2];
        state.nodeSizes[index] = pointSize(index);
        state.nodeFlags[index] = passesFilters(index) ? 1 : 0;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.color);
      gl.bufferData(gl.ARRAY_BUFFER, state.nodeColors, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.size);
      gl.bufferData(gl.ARRAY_BUFFER, state.nodeSizes, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.flag);
      gl.bufferData(gl.ARRAY_BUFFER, state.nodeFlags, gl.DYNAMIC_DRAW);
      applyHoverToFlags();
    }
    function uploadEdges() {
      if (!gl || !edgeProgram) return;
      const links = state.totalLinks;
      const positions = new Float32Array(links * 4);
      const factors = new Float32Array(links * 2);
      let maxWeight = 1;
      for (let index = 0; index < links; index += 1) maxWeight = Math.max(maxWeight, state.edgeWeights[index]);
      const norms = new Float32Array(links);
      let bridgeCount = 0;
      for (let index = 0; index < links; index += 1) {
        const source = state.edgeSources[index], target = state.edgeTargets[index];
        positions[index * 4] = state.positions[source * 2];
        positions[index * 4 + 1] = state.positions[source * 2 + 1];
        positions[index * 4 + 2] = state.positions[target * 2];
        positions[index * 4 + 3] = state.positions[target * 2 + 1];
        if (state.bridges && state.edgeBridges[index]) {
          bridgeCount += 1;
          factors[index * 2] = 10;
          factors[index * 2 + 1] = 10;
          norms[index] = -1; /* bridges always render; excluded from the floor search */
        } else {
          const norm = clamp(state.edgeWeights[index] / maxWeight, 0, 0.49);
          factors[index * 2] = norm;
          factors[index * 2 + 1] = norm;
          norms[index] = norm;
        }
      }
      /* Honest drawn-edge stats: sorted non-bridge weights let stats() binary-search how
         many survive the current zoom's weight floor without touching GPU state. */
      state.bridgeCount = bridgeCount;
      state.weightFloorSorted = Float32Array.from(norms).filter(v => v >= 0).sort();
      gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuffers.position);
      gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuffers.factor);
      gl.bufferData(gl.ARRAY_BUFFER, factors, gl.STATIC_DRAW);
      state.edgeVertexCount = links * 2;
    }

    /* ── Community regions: soft district outlines that make the scene read as a map ── */
    function computeCommunityRegions() {
      const groups = new Map();
      for (let index = 0; index < state.ids.length; index += 1) {
        if (!state.nodeFlags[index]) continue;
        const group = String(state.communities[index] ?? index);
        const bucket = groups.get(group);
        if (bucket) bucket.push(index); else groups.set(group, [index]);
      }
      const regions = [];
      for (const [group, members] of groups) {
        if (members.length < 3) continue;
        let cx = 0, cy = 0;
        let topIndex = members[0];
        for (const index of members) {
          cx += state.positions[index * 2]; cy += state.positions[index * 2 + 1];
          if ((state.degrees[index] || 0) > (state.degrees[topIndex] || 0)) topIndex = index;
        }
        cx /= members.length; cy /= members.length;
        let radius = 0;
        for (const index of members) {
          const dx = state.positions[index * 2] - cx, dy = state.positions[index * 2 + 1] - cy;
          radius = Math.max(radius, Math.sqrt(dx * dx + dy * dy));
        }
        const hash = Array.from(group).reduce((sum, ch) => ((sum * 31) + ch.charCodeAt(0)) >>> 0, 7);
        regions.push({
          x: cx, y: cy, r: radius * 1.12,
          tint: rgb(activePalette()[hash % activePalette().length]),
          /* Districts are named after their strongest hub: real graphs usually have a
             recognisable anchor entity; synthetic ones still get something readable. */
          name: labelText(topIndex),
          count: members.length,
        });
      }
      state.communityRegions = regions;
    }
    function drawRegions() {
      if (!underlayContext || !state.ready || !state.communityRegions.length) return;
      const ratio = zoomRatio();
      /* Regions aid orientation at browsing distance; they vanish into the glow when far
         out and get out of the way of close reading. Strong enough to survive the edge
         field painted over them. */
      const strength = clamp(1 - Math.abs(ratio - 0.9) / 1.4, 0, 1) * 0.26;
      if (strength <= 0.005) { underlayContext.clearRect(0, 0, state.width, state.height); return; }
      underlayContext.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
      underlayContext.clearRect(0, 0, state.width, state.height);
      const viewRadius = Math.hypot(state.width / (2 * state.camera.scale), state.height / (2 * state.camera.scale)) + 80;
      for (const region of state.communityRegions) {
        const point = screen(region.x, region.y);
        const radius = region.r * state.camera.scale;
        if (radius < 14) continue;
        if (Math.hypot(point[0] - state.width / 2, point[1] - state.height / 2) > viewRadius * state.camera.scale + radius) continue;
        const tint = `rgba(${region.tint[0] * 255 | 0},${region.tint[1] * 255 | 0},${region.tint[2] * 255 | 0},`;
        const gradient = underlayContext.createRadialGradient(point[0], point[1], radius * 0.3, point[0], point[1], radius);
        gradient.addColorStop(0, `${tint}${strength})`);
        gradient.addColorStop(0.75, `${tint}${strength * 0.55})`);
        gradient.addColorStop(1, 'rgba(0,0,0,0)');
        underlayContext.fillStyle = gradient;
        underlayContext.beginPath();
        underlayContext.arc(point[0], point[1], radius, 0, Math.PI * 2);
        underlayContext.fill();
        /* Thin rim gives each district a defined boundary against the edge field. */
        underlayContext.strokeStyle = `${tint}${Math.min(0.5, strength * 2.2)})`;
        underlayContext.lineWidth = 1.5;
        underlayContext.beginPath();
        underlayContext.arc(point[0], point[1], radius * 0.98, 0, Math.PI * 2);
        underlayContext.stroke();
        /* District label: hub-named, member-counted, drawn only when the district is
           large enough on screen to be a place rather than a smudge. */
        if (radius >= 46) {
          const name = region.name || '';
          if (name) {
            const fontPx = clamp(11 + radius / state.camera.scale * 0.004, 11, 17);
            underlayContext.font = `600 ${fontPx}px ui-sans-serif,system-ui,sans-serif`;
            underlayContext.textAlign = 'center';
            underlayContext.textBaseline = 'middle';
            const labelAlpha = Math.min(0.85, strength * 3);
            const title = name.length > 28 ? `${name.slice(0, 27)}…` : name;
            underlayContext.shadowColor = 'rgba(4,8,12,0.9)';
            underlayContext.shadowBlur = 4;
            underlayContext.fillStyle = `rgba(236,244,248,${labelAlpha})`;
            underlayContext.fillText(title, point[0], point[1]);
            underlayContext.font = `10px ui-sans-serif,system-ui,sans-serif`;
            underlayContext.fillStyle = `rgba(190,208,220,${labelAlpha * 0.8})`;
            underlayContext.fillText(`${region.count} nodes`, point[0], point[1] + fontPx + 3);
            underlayContext.shadowColor = 'transparent';
            underlayContext.shadowBlur = 0;
            underlayContext.textAlign = 'left';
          }
        }
      }
    }

    /* ── Picking: local spatial grid, no worker latency ─────────────────────────── */
    function buildPickGrid() {
      const cell = 39;
      const grid = new Map();
      for (let index = 0; index < state.ids.length; index += 1) {
        if (!state.nodeFlags[index]) continue;
        const key = (Math.floor(state.positions[index * 2] / cell) + 32768) * 65536
          + (Math.floor(state.positions[index * 2 + 1] / cell) + 32768);
        const bucket = grid.get(key);
        if (bucket) bucket.push(index); else grid.set(key, [index]);
      }
      state.pickGrid = grid;
      state.pickCell = cell;
      state.pickDirty = false;
    }
    function pickAt(worldX, worldY) {
      if (!state.ready) return -1;
      if (state.pickDirty) buildPickGrid();
      const cell = state.pickCell;
      const gx = Math.floor(worldX / cell), gy = Math.floor(worldY / cell);
      let best = -1, bestDist = Infinity;
      for (let ox = -1; ox <= 1; ox += 1) {
        for (let oy = -1; oy <= 1; oy += 1) {
          const bucket = state.pickGrid.get((gx + ox + 32768) * 65536 + (gy + oy + 32768));
          if (!bucket) continue;
          for (let slot = 0; slot < bucket.length; slot += 1) {
            const index = bucket[slot];
            const dx = state.positions[index * 2] - worldX;
            const dy = state.positions[index * 2 + 1] - worldY;
            const dist = dx * dx + dy * dy;
            if (dist < bestDist) { bestDist = dist; best = index; }
          }
        }
      }
      if (best < 0) return -1;
      const reach = pointSize(best) * state.camera.scale + 7;
      return bestDist <= reach * reach ? best : -1;
    }

    /* ── Overlay: decluttered labels + capped relation flow ─────────────────────── */
    function labelText(index) { return state.labels[index] || state.ids[index]; }
    function drawOverlay(now = 0) {
      state.labelFrame = 0;
      if (!labelContext || state.destroyed || state.paused) return;
      labelContext.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
      labelContext.clearRect(0, 0, state.width, state.height);
      drawRelationFlow(now);
      drawFocusRing();
      drawDeclutteredLabels();
      /* Lit paths and their arrows paint above background structure... */
      drawHotEdgeDecorations();
      /* ...and the hover card paints above absolutely everything. */
      drawHoverCardLayer();
    }
    function scheduleLabels(immediate = false) {
      if (state.destroyed || state.paused || !labelContext) return;
      if (immediate) { if (state.labelFrame) caf(state.labelFrame); state.labelFrame = raf(() => drawOverlay()); return; }
      if (!state.labelFrame) state.labelFrame = raf(() => drawOverlay());
    }
    /* Lit-path decorations: the hovered/highlighted node's relations get direction arrows
       and their relation name, so reading a connection does not require opening anything. */
    function hotAnchors() {
      const anchors = [];
      if (state.hover >= 0) anchors.push(state.hover);
      if (state.focus >= 0 && state.focus !== state.hover) anchors.push(state.focus);
      return anchors;
    }
    function drawHotEdgeDecorations() {
      if (!labelContext || !state.ready) return;
      const anchors = hotAnchors();
      if (!anchors.length || !state.edgeVertexCount) return;
      const anchorSet = new Set(anchors);
      const showRelation = zoomRatio() > 0.55;
      labelContext.save();
      labelContext.strokeStyle = "rgba(244,211,127,0.9)";
      labelContext.fillStyle = "rgba(244,211,127,0.9)";
      for (let edge = 0; edge < state.totalLinks; edge += 1) {
        const source = state.edgeSources[edge], target = state.edgeTargets[edge];
        if (!anchorSet.has(source) && !anchorSet.has(target)) continue;
        const a = screen(state.positions[source * 2], state.positions[source * 2 + 1]);
        const b = screen(state.positions[target * 2], state.positions[target * 2 + 1]);
        if ((a[0] < -20 && b[0] < -20) || (a[0] > state.width + 20 && b[0] > state.width + 20)
          || (a[1] < -20 && b[1] < -20) || (a[1] > state.height + 20 && b[1] > state.height + 20)) continue;
        labelContext.lineWidth = 1.6;
        labelContext.beginPath(); labelContext.moveTo(a[0], a[1]); labelContext.lineTo(b[0], b[1]); labelContext.stroke();
        /* Direction cue at 62% of the run: source → target. Dark halo keeps it visible
           over dense background lines; size grows as you zoom in. */
        const t = 0.62, tipX = a[0] + (b[0] - a[0]) * t, tipY = a[1] + (b[1] - a[1]) * t;
        const angle = Math.atan2(b[1] - a[1], b[0] - a[0]);
        const head = clamp(6 + zoomRatio() * 3, 6, 13);
        labelContext.strokeStyle = 'rgba(6,10,14,0.85)';
        labelContext.lineWidth = head * 0.9;
        labelContext.lineCap = 'round';
        labelContext.beginPath(); labelContext.moveTo(a[0], a[1]); labelContext.lineTo(b[0], b[1]); labelContext.stroke();
        labelContext.strokeStyle = 'rgba(244,211,127,0.95)';
        labelContext.lineWidth = 2;
        labelContext.beginPath(); labelContext.moveTo(a[0], a[1]); labelContext.lineTo(b[0], b[1]); labelContext.stroke();
        labelContext.fillStyle = 'rgba(244,211,127,0.98)';
        labelContext.beginPath();
        labelContext.moveTo(tipX, tipY);
        labelContext.lineTo(tipX - Math.cos(angle - 0.42) * head, tipY - Math.sin(angle - 0.42) * head);
        labelContext.lineTo(tipX - Math.cos(angle + 0.42) * head, tipY - Math.sin(angle + 0.42) * head);
        labelContext.closePath(); labelContext.fill();
        const relation = String(state.edgeRelations[edge] || "");
        if (showRelation && relation && relation !== "relates_to") {
          const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
          labelContext.font = "10px ui-sans-serif,system-ui,sans-serif";
          const width = labelContext.measureText(relation).width;
          labelContext.fillStyle = "rgba(9,14,20,0.88)";
          labelContext.fillRect(mx - width / 2 - 4, my - 8, width + 8, 15);
          labelContext.fillStyle = "rgba(244,222,168,0.95)";
          labelContext.textBaseline = "middle"; labelContext.textAlign = "left";
          labelContext.fillText(relation, mx - width / 2, my);
          labelContext.fillStyle = "rgba(244,211,127,0.9)";
        }
      }
      labelContext.restore();
    }
    function drawFocusRing() {
      const focused = state.focus >= 0 ? state.focus : state.hover;
      if (focused < 0 || focused >= state.ids.length || !state.nodeFlags[focused]) return;
      const point = screen(state.positions[focused * 2], state.positions[focused * 2 + 1]);
      labelContext.beginPath();
      labelContext.arc(point[0], point[1], clamp(7 + state.camera.scale * 2, 7, 15), 0, Math.PI * 2);
      labelContext.strokeStyle = '#f4d37f';
      labelContext.lineWidth = 1.5;
      labelContext.stroke();
    }
    /* The callout is the single most important overlay: it always paints last so no
       line, label, or arrow can ever cover it. */
    function drawHoverCardLayer() {
      const index = state.hover;
      if (index < 0 || index >= state.ids.length || !state.nodeFlags[index]) return;
      drawHoverCard(index, screen(state.positions[index * 2], state.positions[index * 2 + 1]));
    }
    /* Hover callout: hubs are meaningless as bare dots — spell out what the entity is,
       which category it belongs to, and how many relations it carries. */
    function drawHoverCard(index, point) {
      const title = labelText(index);
      const rawType = String(state.types[index] || '').trim();
      const category = rawType
        ? rawType.replace(/[_-]+/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase())
        : 'Entity';
      const meta = `${category} · ${Math.round(state.degrees[index] || 0)} relation${(state.degrees[index] || 0) === 1 ? '' : 's'}`;
      const titleFont = '600 13px ui-sans-serif,system-ui,sans-serif';
      const metaFont = '11px ui-sans-serif,system-ui,sans-serif';
      /* Relation analysis at a glance: the strongest connections, strongest first. */
      let connections = [];
      if (state.neighbors && state.neighbors[index]) {
        connections = state.neighbors[index]
          .slice().sort((a, b) => (state.degrees[b] || 0) - (state.degrees[a] || 0))
          .slice(0, 3);
      }
      labelContext.save();
      labelContext.font = titleFont;
      const titleWidth = labelContext.measureText(title).width;
      labelContext.font = metaFont;
      const metaWidth = labelContext.measureText(meta).width;
      let connectionWidths = [];
      if (connections.length) {
        labelContext.font = metaFont;
        connectionWidths = connections.map(neighbor => labelContext.measureText(`↳ ${labelText(neighbor)}`).width);
      }
      const padX = 10, padY = 8, gap = 4;
      const cardW = Math.ceil(Math.max(titleWidth, metaWidth, ...connectionWidths)) + padX * 2;
      const cardH = 13 + 11 + gap + padY * 2 + connectionWidths.length * 14;
      const radius = pointSize(index) * Math.min(1.6, state.camera.scale) + 4;
      let x = point[0] - cardW / 2;
      let y = point[1] - radius - 10 - cardH;
      if (y < 4) y = point[1] + radius + 10;
      x = clamp(x, 4, Math.max(4, state.width - cardW - 4));
      labelContext.font = metaFont;
      if (typeof labelContext.roundRect === 'function') {
        labelContext.beginPath();
        labelContext.roundRect(x, y, cardW, cardH, 7);
        labelContext.fillStyle = 'rgba(9,14,20,0.94)';
        labelContext.fill();
        labelContext.strokeStyle = 'rgba(244,211,127,0.55)';
        labelContext.lineWidth = 1;
        labelContext.stroke();
      } else {
        labelContext.fillStyle = 'rgba(9,14,20,0.94)';
        labelContext.fillRect(x, y, cardW, cardH);
      }
      labelContext.textBaseline = 'alphabetic';
      labelContext.textAlign = 'left';
      labelContext.font = titleFont;
      labelContext.fillStyle = '#f4d37f';
      labelContext.fillText(title, x + padX, y + padY + 12);
      labelContext.font = metaFont;
      labelContext.fillStyle = 'rgba(214,228,236,0.85)';
      labelContext.fillText(meta, x + padX, y + padY + 12 + gap + 11);
      connections.forEach((neighbor, slot) => {
        const lineY = y + padY + 12 + gap + 11 + (slot + 1) * 14;
        labelContext.fillStyle = 'rgba(150,205,220,0.92)';
        labelContext.fillText(`↳ ${labelText(neighbor)}`, x + padX, lineY);
      });
      labelContext.restore();
    }
    function drawRelationFlow(now) {
      if (!state.settings.flow || !state.totalLinks) return;
      const speed = clamp(Number(state.settings.flowSpeed || 0), 0, 100);
      const moving = speed > 0 && !state.settings.frozen && !reducedMotion();
      const stride = Math.max(1, Math.ceil(state.totalLinks / FLOW_EDGE_LIMIT));
      labelContext.save();
      labelContext.globalCompositeOperation = 'lighter';
      for (let cursor = 0; cursor < state.totalLinks; cursor += stride) {
        if (!state.edgeBridges[cursor] && zoomRatio() < EDGE_START) continue;
        const ax = state.positions[state.edgeSources[cursor] * 2];
        const ay = state.positions[state.edgeSources[cursor] * 2 + 1];
        const bx = state.positions[state.edgeTargets[cursor] * 2];
        const by = state.positions[state.edgeTargets[cursor] * 2 + 1];
        const a = screen(ax, ay), b = screen(bx, by);
        if ((a[0] < -12 && b[0] < -12) || (a[0] > state.width + 12 && b[0] > state.width + 12)
          || (a[1] < -12 && b[1] < -12) || (a[1] > state.height + 12 && b[1] > state.height + 12)) continue;
        const phase = moving ? ((now * (0.00006 + speed * 0.000018) + (cursor % 997) / 997) % 1) : 0.68;
        const x = a[0] + (b[0] - a[0]) * phase, y = a[1] + (b[1] - a[1]) * phase;
        labelContext.fillStyle = state.edgeBridges[cursor]
          ? 'rgba(255,220,132,0.88)' : 'rgba(115,220,239,0.72)';
        labelContext.beginPath();
        labelContext.arc(x, y, zoomRatio() < 0.8 ? 1.15 : 1.65, 0, Math.PI * 2);
        labelContext.fill();
      }
      labelContext.restore();
    }
    function drawDeclutteredLabels() {
      if (!state.settings.labels || !state.ready) return;
      /* Camera key quantises scale so tiny wheel nudges do not invalidate the occupancy
         pass; candidates walk topNodes rank-first so important names always win space. */
      const key = `${Math.round(state.camera.x)}:${Math.round(state.camera.y)}:${Math.round(state.camera.scale * 24)}`;
      const fontPx = clamp(Number(state.settings.font || 12) + state.camera.scale * 1.5, 8, 24);
      const font = `${fontPx}px ui-sans-serif,system-ui,sans-serif`;
      const cacheKey = `${font}|${key}`;
      if (cacheKey === state.lastLabelKey) return;
      state.lastLabelKey = cacheKey;
      labelContext.font = font;
      labelContext.textBaseline = 'middle';
      labelContext.shadowColor = 'rgba(4,8,12,0.85)';
      labelContext.shadowBlur = 3;
      labelContext.fillStyle = 'rgba(224,236,241,0.86)';
      const cell = 14;
      const occupied = new Set();
      let drawn = 0;
      const consider = index => {
        if (drawn >= LABEL_MAX || !state.nodeFlags[index] || state.nodeFlags[index] === 2) return;
        const point = screen(state.positions[index * 2], state.positions[index * 2 + 1]);
        if (point[0] < -40 || point[0] > state.width + 40 || point[1] < -20 || point[1] > state.height + 20) return;
        const text = labelText(index);
        let width = state.labelMetrics.get(text);
        if (width === undefined) { width = labelContext.measureText(text).width; if (state.labelMetrics.size > 8000) state.labelMetrics.clear(); state.labelMetrics.set(text, width); }
        const left = Math.floor((point[0] + 6) / cell), right = Math.ceil((point[0] + 6 + width) / cell);
        const top = Math.floor((point[1] - 6 - fontPx / 2) / cell), bottom = Math.ceil((point[1] - 6 + fontPx / 2) / cell);
        for (let gx = left; gx <= right; gx += 1) {
          for (let gy = top; gy <= bottom; gy += 1) {
            if (occupied.has(`${gx}:${gy}`)) return;
          }
        }
        for (let gx = left; gx <= right; gx += 1) {
          for (let gy = top; gy <= bottom; gy += 1) occupied.add(`${gx}:${gy}`);
        }
        labelContext.fillText(text, point[0] + 6, point[1] - 6);
        drawn += 1;
      };
      const anchor = state.hover >= 0 ? state.hover
        : (state.focus >= 0 && state.nodeFlags[state.focus] === 1 ? state.focus : -1);
      const focusNeighborhood = anchor >= 0 && state.neighbors && state.neighbors[anchor];
      if (focusNeighborhood) {
        consider(anchor);
        for (const neighbor of state.neighbors[anchor]) {
          if (drawn >= LABEL_MAX) break;
          consider(neighbor);
        }
      }
      for (let rank = 0; rank < state.topNodes.length && drawn < LABEL_MAX; rank += 1) {
        if (!focusNeighborhood) consider(state.topNodes[rank]);
        else break;
      }
      labelContext.shadowColor = 'transparent';
      labelContext.shadowBlur = 0;
    }

    /* ── Frame loop ─────────────────────────────────────────────────────────────── */
    function flowAnimating() {
      return state.settings.flow && state.totalLinks && Number(state.settings.flowSpeed || 0) > 0
        && !state.settings.frozen && !reducedMotion();
    }
    function draw(now = 0) {
      state.frame = 0;
      if (state.destroyed || state.paused || !state.ready || !nodeProgram) return;
      if (flowAnimating() && state.flowPaintAt && now - state.flowPaintAt < FLOW_FRAME_MS) {
        schedule();
        return;
      }
      state.flowPaintAt = now;
      const glow = glowAmount();
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.enable(gl.BLEND);

      const edgeAlpha = clamp((zoomRatio() - EDGE_START) * 0.8, 0, 0.16)
        * clamp(Number(state.settings.linkw || 0.72), 0.2, 3);
      if (edgeAlpha > 0.01 && state.edgeVertexCount) {
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
        gl.useProgram(edgeProgram);
        bindEdgeGeometry();
        gl.uniform2f(edgeBuffers.attrs.camera, state.camera.x, state.camera.y);
        gl.uniform1f(edgeBuffers.attrs.scale, state.camera.scale * state.dpr);
        gl.uniform2f(edgeBuffers.attrs.resolution, canvas.width, canvas.height);
        gl.uniform1f(edgeBuffers.attrs.edgeAlpha, edgeAlpha);
        /* Most WebGL2 implementations clamp line width to 1: the linkw control scales the
         * shader alpha (see edgeAlpha above), not geometric width. */
        gl.uniform1f(edgeBuffers.attrs.focusFade,
          state.hover >= 0 || state.focus >= 0 ? 0.10 : 1.0);
        /* Far out only the strongest routes render; the floor empties as you zoom in.
           Scaled into the [0, 0.49] weight-norm band — an unscaled floor would gate out
           every normal edge until well past fit zoom, then dump them all at once. */
        const ratio = zoomRatio();
        gl.uniform1f(edgeBuffers.attrs.weightFloor,
          clamp(1.15 - ratio * 0.55, 0, 1) * 0.49);
        gl.uniform2f(edgeBuffers.attrs.hotA, state.hoverPoint[0], state.hoverPoint[1]);
        gl.uniform1f(edgeBuffers.attrs.hotAOn, state.hover >= 0 ? 1.0 : 0.0);
        gl.uniform2f(edgeBuffers.attrs.hotB, state.focusPoint[0], state.focusPoint[1]);
        gl.uniform1f(edgeBuffers.attrs.hotBOn, state.focus >= 0 ? 1.0 : 0.0);
        gl.lineWidth(Math.max(1, Number(state.settings.linkw || 0.72) * state.dpr));
        gl.drawArrays(gl.LINES, 0, state.edgeVertexCount);
      }

      gl.useProgram(nodeProgram);
      bindNodeGeometry();
      gl.uniform2f(nodeBuffers.attrs.camera, state.camera.x, state.camera.y);
      gl.uniform1f(nodeBuffers.attrs.scale, state.camera.scale * state.dpr);
      gl.uniform2f(nodeBuffers.attrs.resolution, canvas.width, canvas.height);
      gl.uniform1f(nodeBuffers.attrs.glow, glow);
      if (glow > 0.02) {
        /* Density pass: additive soft sprites turn crowded regions into brightness so the
           zoomed-out scene reads as structure instead of overlapping dots. */
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
        gl.drawArrays(gl.POINTS, 0, state.ids.length);
      }
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawArrays(gl.POINTS, 0, state.ids.length);

      drawRegions();

      if (flowAnimating()) schedule();
      scheduleLabels();
    }
    function bindNodeGeometry() {
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.position);
      gl.enableVertexAttribArray(nodeBuffers.attrs.position);
      gl.vertexAttribPointer(nodeBuffers.attrs.position, 2, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.color);
      gl.enableVertexAttribArray(nodeBuffers.attrs.color);
      gl.vertexAttribPointer(nodeBuffers.attrs.color, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.size);
      gl.enableVertexAttribArray(nodeBuffers.attrs.size);
      gl.vertexAttribPointer(nodeBuffers.attrs.size, 1, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.flag);
      gl.enableVertexAttribArray(nodeBuffers.attrs.flag);
      gl.vertexAttribPointer(nodeBuffers.attrs.flag, 1, gl.FLOAT, false, 0, 0);
    }
    function bindEdgeGeometry() {
      gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuffers.position);
      gl.enableVertexAttribArray(edgeBuffers.attrs.position);
      gl.vertexAttribPointer(edgeBuffers.attrs.position, 2, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuffers.factor);
      gl.enableVertexAttribArray(edgeBuffers.attrs.factor);
      gl.vertexAttribPointer(edgeBuffers.attrs.factor, 1, gl.FLOAT, false, 0, 0);
    }
    function schedule() {
      if (!state.destroyed && !state.paused && !state.frame) state.frame = raf(draw);
    }

    /* ── Camera & sizing ────────────────────────────────────────────────────────── */
    /* The camera touches uniforms only — the worker never hears about pan/zoom again. */
    function camera() { schedule(); scheduleLabels(); }
    function resize() {
      const rect = element.getBoundingClientRect();
      state.width = Math.max(1, rect.width || element.clientWidth || 1);
      state.height = Math.max(1, rect.height || element.clientHeight || 1);
      state.dpr = Math.min(2, window.devicePixelRatio || 1);
      [underlay, canvas, labels].forEach(target => {
        target.width = Math.max(1, Math.floor(state.width * state.dpr));
        target.height = Math.max(1, Math.floor(state.height * state.dpr));
      });
      state.lastLabelKey = '';
      if (state.ready) { schedule(); scheduleLabels(true); }
    }
    function fit() {
      if (!state.bounds) return;
      const bounds = state.bounds;
      state.camera.x = (bounds.minX + bounds.maxX) / 2;
      state.camera.y = (bounds.minY + bounds.maxY) / 2;
      state.camera.scale = clamp(
        Math.min(
          state.width / Math.max(120, bounds.maxX - bounds.minX + 120),
          state.height / Math.max(120, bounds.maxY - bounds.minY + 120),
        ), 0.005, 4);
      /* The fitted scale is the yardstick every LOD band measures against. */
      state.baseScale = state.camera.scale || 1;
      camera();
    }
    function applyLayout(positions, bounds, doFit) {
      state.positions = positions || state.positions;
      state.bounds = bounds || state.bounds;
      uploadNodePositions();
      uploadEdges();
      state.pickDirty = true;
      state.lastLabelKey = '';
      if (doFit) fit(); else camera();
      computeCommunityRegions();
      drawRegions();
    }

    /* ── Worker messages ────────────────────────────────────────────────────────── */
    function handleCapacity(message) {
      const resource = message.resource === 'relations' ? 'relations' : 'nodes';
      const error = new Error(`Every-node capacity exceeded: ${Number(message.count).toLocaleString()} ${resource} (limit ${Number(message.limit || (resource === 'relations' ? MAX_LINKS : MAX_NODES)).toLocaleString()}). Filter the graph before loading all nodes.`);
      error.code = 'GRAPH_CAPACITY';
      state.error = { code: error.code, message: error.message };
      if (typeof opts.onError === 'function') opts.onError(error);
    }
    function adoptCommon(message) {
      state.ids = message.ids || [];
      rebuildIdIndex();
      state.labels = message.labels || [];
      state.types = message.types || state.types;
      state.communities = message.communities || [];
      state.nodeGhosts = message.nodeGhosts || new Uint8Array(state.ids.length);
      state.bounds = message.bounds || null;
      const count = state.ids.length;
      if (state.nodeFlags.length !== count) state.nodeFlags = new Float32Array(count);
      if (state.nodeColors.length !== count * 3) state.nodeColors = new Float32Array(count * 3);
      if (state.nodeSizes.length !== count) state.nodeSizes = new Float32Array(count);
    }
    function handleWorkerMessage(event) {
      const message = event.data || {};
      if (message.type === 'capacity') { handleCapacity(message); return; }
      if (message.type === 'preview' || message.type === 'ready') {
        adoptCommon(message);
        canvas.setAttribute('role', 'img');
        canvas.setAttribute('aria-label',
          `Graph with ${state.ids.length} entities and ${Number(message.totalLinks || 0)} relations`);
        if (message.type === 'ready') {
          state.degrees = message.degrees || new Float32Array(state.ids.length);
          state.betweenness = message.betweenness || new Float32Array(state.ids.length);
          state.evidenceMass = message.evidenceMass || new Float32Array(state.ids.length);
          state.edgeSources = message.edgeSources || new Uint32Array(0);
          state.edgeTargets = message.edgeTargets || new Uint32Array(0);
          state.edgeBridges = message.edgeBridges || new Uint8Array(0);
          state.edgeWeights = message.edgeWeights || new Float32Array(0);
          state.edgeRelations = message.edgeRelations || [];
          state.topNodes = message.topNodes || new Uint32Array(0);
          state.totalLinks = Number(message.totalLinks || 0);
          /* Neighbourhood adjacency powers hover focus: hovering a node dims everything
             that is not the node, its direct relations, or their connecting edges. */
          state.neighbors = state.ids.map(() => []);
          for (let edge = 0; edge < state.totalLinks; edge += 1) {
            const source = state.edgeSources[edge], target = state.edgeTargets[edge];
            if (state.neighbors[source]) state.neighbors[source].push(target);
            if (state.neighbors[target]) state.neighbors[target].push(source);
          }
        }
        state.ready = true;
        refreshVisibility(false);
        uploadNodePositions();
        uploadEdges();
        fit();
        if (message.type === 'preview') stats({ progressive: true, linksPending: true });
        else if (typeof opts.onMetrics === 'function') opts.onMetrics(api.metrics());
        schedule();
        scheduleLabels(true);
        return;
      }
      if (message.type === 'progress') {
        state.layoutPending = Number(message.pass) < Number(message.total);
        stats({ layoutPending: state.layoutPending });
        return;
      }
      if (message.type === 'layout') {
        state.layoutPending = false;
        if (!state.ready) return;
        applyLayout(message.positions, message.bounds, message.fit === true);
        stats({ layoutPending: false });
        return;
      }
    }
    function handleWorkerFailure(event) {
      if (state.destroyed) return;
      const source = event && event.error;
      const error = source instanceof Error ? source
        : new Error(event && event.type === 'messageerror'
          ? 'Every-node worker returned an unreadable response.'
          : 'Every-node worker failed while preparing the graph.');
      error.code = error.code || 'GRAPH_WORKER';
      state.error = { code: error.code, message: error.message };
      state.ready = false;
      if (typeof opts.onError === 'function') opts.onError(error);
    }

    function postSettings(relayout, fitLayout = false) {
      if (!worker) return;
      if (!relayout) {
        worker.postMessage({ type: 'settings', settings: state.settings, relayout: false });
        return;
      }
      pendingLayoutFit = pendingLayoutFit || fitLayout;
      if (layoutFrame) return;
      layoutFrame = raf(() => {
        layoutFrame = 0;
        const fitWanted = pendingLayoutFit; pendingLayoutFit = false;
        state.layoutPending = true;
        stats({ layoutPending: true });
        worker.postMessage({ type: 'settings', settings: state.settings, relayout: true, fit: fitWanted });
      });
    }
    function drawnEdgeEstimate() {
      if (!state.totalLinks) return 0;
      if (zoomRatio() <= EDGE_START) return state.bridgeCount;
      /* Mirrors the shader floor exactly, including the 0.49 norm-band scaling. */
      const floor = clamp(1.15 - zoomRatio() * 0.55, 0, 1) * 0.49;
      const sorted = state.weightFloorSorted;
      let lo = 0, hi = sorted.length;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (sorted[mid] < floor) lo = mid + 1; else hi = mid; }
      return state.bridgeCount + (sorted.length - lo);
    }
    function stats(extra) {
      if (typeof opts.onStats !== 'function') return;
      const drawn = drawnEdgeEstimate();
      opts.onStats({
        nodes: state.ids.length,
        visibleNodes: state.visibleCount,
        links: state.totalLinks,
        drawnLinks: drawn,
        hiddenLinks: Math.max(0, state.totalLinks - drawn),
        collapsed: state.collapsed,
        relationFlow: state.settings.flow === true,
        layoutPending: state.layoutPending,
        presentation: 'all',
        preset: 'Every node · LOD',
        renderer: gl && nodeProgram ? 'webgl2' : 'unsupported',
        ...extra,
      });
    }
    function clearHover() {
      state.hover = -1;
      state.hoverPoint = [0, 0];
      applyHoverToFlags();
      element.classList.remove('engraphis-all-node-hover');
      state.lastLabelKey = '';
      scheduleLabels(true);
    }
    function requestHit(event) {
      if (state.destroyed) return;
      pendingHit = { x: event.clientX, y: event.clientY };
      if (hitFrame) return;
      hitFrame = raf(() => {
        hitFrame = 0;
        const sample = pendingHit;
        pendingHit = null;
        if (!sample || state.destroyed) return;
        const rect = element.getBoundingClientRect();
        const point = world(sample.x - rect.left, sample.y - rect.top);
        const next = pickAt(point[0], point[1]);
        if (next === state.hover) return;
        state.hover = next;
        state.hoverPoint = next >= 0
          ? [state.positions[next * 2], state.positions[next * 2 + 1]]
          : [0, 0];
        applyHoverToFlags();
        state.lastLabelKey = '';
        element.classList.toggle('engraphis-all-node-hover', next >= 0);
        liveRegion.textContent = next >= 0
          ? `${labelText(next)}, ${Math.round(state.degrees[next] || 0)} relations`
          : '';
        if (typeof opts.onHover === 'function') opts.onHover(next >= 0 ? nodeAt(next) : null);
        scheduleLabels(true);
      });
    }

    /* ── Interaction ────────────────────────────────────────────────────────────── */
    /* ── Touch: two-pointer pinch zoom about the pinch midpoint ─────────────────── */
    const activePointers = new Map();
    function applyPinch() {
      if (activePointers.size !== 2) return;
      const [p1, p2] = [...activePointers.values()];
      const dist = Math.hypot(p2.x - p1.x, p2.y - p1.y) || 1;
      const rect = element.getBoundingClientRect();
      const midX = (p1.x + p2.x) / 2 - rect.left, midY = (p1.y + p2.y) / 2 - rect.top;
      const before = world(midX, midY);
      state.camera.scale = clamp(state.camera.scale * (dist / (state.pinchDist || dist)), 0.005, 7);
      state.pinchDist = dist;
      const after = world(midX, midY);
      state.camera.x += before[0] - after[0];
      state.camera.y += before[1] - after[1];
      camera();
    }
    canvas.addEventListener('pointerdown', event => {
      activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (activePointers.size === 2) { state.drag = null; state.pinchDist = 0; }
      if (event.button !== 0 || activePointers.size >= 2) return;
      state.drag = { x: event.clientX, y: event.clientY, cameraX: state.camera.x, cameraY: state.camera.y, moved: false };
      canvas.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    canvas.addEventListener('pointermove', event => {
      if (activePointers.has(event.pointerId)) {
        activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
        if (activePointers.size === 2) { applyPinch(); return; }
      }
      if (state.drag) {
        const dx = event.clientX - state.drag.x, dy = event.clientY - state.drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) state.drag.moved = true;
        state.camera.x = state.drag.cameraX - dx / state.camera.scale;
        state.camera.y = state.drag.cameraY - dy / state.camera.scale;
        camera();
      } else requestHit(event);
    });
    canvas.addEventListener('pointerup', event => {
      activePointers.delete(event.pointerId);
      state.pinchDist = 0;
      const drag = state.drag;
      state.drag = null;
      if (!drag || drag.moved) return;
      if (state.hover >= 0 && typeof opts.onNodeClick === 'function') opts.onNodeClick(nodeAt(state.hover));
      else if (typeof opts.onBackgroundClick === 'function') opts.onBackgroundClick();
    });
    canvas.addEventListener('pointerleave', () => {
      activePointers.clear();
      state.pinchDist = 0;
      if (!state.drag) clearHover();
    });
    canvas.addEventListener('pointercancel', event => {
      activePointers.delete(event.pointerId);
      state.pinchDist = 0;
      state.drag = null;
    });
    canvas.addEventListener('pointerout', event => {
      if (!state.drag && (!event.relatedTarget || event.relatedTarget !== canvas)) clearHover();
    });
    canvas.addEventListener('wheel', event => {
      const rect = element.getBoundingClientRect();
      const before = world(event.clientX - rect.left, event.clientY - rect.top);
      state.camera.scale = clamp(state.camera.scale * Math.exp(-event.deltaY * 0.0012), 0.005, 7);
      const after = world(event.clientX - rect.left, event.clientY - rect.top);
      state.camera.x += before[0] - after[0];
      state.camera.y += before[1] - after[1];
      camera();
      event.preventDefault();
    }, { passive: false });
    /* Keyboard browsing: arrows pan, +/- zoom, F fits, Escape drops the selection.
       Named handler: the host element outlives engine instances, so destroy must remove
       it or handlers accumulate across mode switches and multiply pan/zoom steps. */
    const handleKeydown = event => {
      const step = 90 / Math.max(0.02, state.camera.scale);
      let handled = true;
      switch (event.key) {
        case 'ArrowLeft': state.camera.x -= step; break;
        case 'ArrowRight': state.camera.x += step; break;
        case 'ArrowUp': state.camera.y -= step; break;
        case 'ArrowDown': state.camera.y += step; break;
        case '+': case '=':
          state.camera.scale = clamp(state.camera.scale * 1.25, 0.005, 7); break;
        case '-': case '_':
          state.camera.scale = clamp(state.camera.scale / 1.25, 0.005, 7); break;
        case 'f': case 'F': fit(); break;
        case 'Escape': clearFocus(); clearHover(); break;
        default: handled = false;
      }
      if (handled) { camera(); event.preventDefault(); }
    };
    element.tabIndex = 0;
    element.addEventListener('keydown', handleKeydown);

    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
    if (observer) observer.observe(element); else window.addEventListener('resize', resize);

    initWebgl();
    if (gl && nodeProgram) {
      worker = new Worker(WORKER_URL);
      worker.onmessage = handleWorkerMessage;
      worker.addEventListener('error', handleWorkerFailure);
      worker.addEventListener('messageerror', handleWorkerFailure);
    } else {
      const error = new Error('This browser does not provide WebGL2, which the Every-node view requires.');
      error.code = 'WEBGL2_UNSUPPORTED';
      state.error = { code: error.code, message: error.message };
      if (typeof opts.onError === 'function') opts.onError(error);
    }
    resize();

    function exportImageCanvas() {
      if (state.destroyed || !state.ready || !nodeProgram) return null;
      if (state.frame) { caf(state.frame); state.frame = 0; }
      if (state.labelFrame) { caf(state.labelFrame); state.labelFrame = 0; }
      /* Paint both layers synchronously: draw() alone would leave the overlay on a rAF
         and the composite below would read the previous camera's labels/arrows/card. */
      const now = typeof performance !== 'undefined' ? performance.now() : 0;
      draw(now);
      drawRegions();
      drawOverlay(now);
      const output = document.createElement('canvas');
      output.width = canvas.width;
      output.height = canvas.height;
      const context = output.getContext('2d');
      if (!context) return null;
      context.drawImage(underlay, 0, 0);
      context.drawImage(canvas, 0, 0);
      context.drawImage(labels, 0, 0);
      return output;
    }
    function destroyGraph() {
      if (state.destroyed) return;
      state.destroyed = true;
      state.paused = true;
      if (hitFrame) { caf(hitFrame); hitFrame = 0; }
      if (layoutFrame) { caf(layoutFrame); layoutFrame = 0; }
      if (state.frame) { caf(state.frame); state.frame = 0; }
      if (state.labelFrame) { caf(state.labelFrame); state.labelFrame = 0; }
      if (worker) {
        worker.onmessage = null;
        worker.removeEventListener('error', handleWorkerFailure);
        worker.removeEventListener('messageerror', handleWorkerFailure);
        worker.terminate();
        worker = null;
      }
      if (observer) observer.disconnect();
      else window.removeEventListener('resize', resize);
      element.removeEventListener('keydown', handleKeydown);
      if (gl) {
        [nodeBuffers.position, nodeBuffers.color, nodeBuffers.size, nodeBuffers.flag,
          edgeBuffers.position, edgeBuffers.factor].forEach(buffer => {
          if (buffer && typeof gl.deleteBuffer === 'function') gl.deleteBuffer(buffer);
        });
        [nodeProgram, edgeProgram].forEach(value => {
          if (value && typeof gl.deleteProgram === 'function') gl.deleteProgram(value);
        });
        const loseContext = typeof gl.getExtension === 'function'
          ? gl.getExtension('WEBGL_lose_context') : null;
        if (loseContext && typeof loseContext.loseContext === 'function') loseContext.loseContext();
      }
      nodeProgram = edgeProgram = null;
      nodeBuffers = {}; edgeBuffers = {};
      state.ids = []; state.idIndex = new Map(); state.labels = []; state.types = []; state.communities = [];
      state.positions = new Float32Array(0);
      state.nodeFlags = state.nodeGhosts = new Uint8Array(0);
      state.nodeColors = state.nodeSizes = new Float32Array(0);
      element.removeAttribute('data-graph-style');
      element.replaceChildren();
    }

    const api = {
      exportImageCanvas,
      apply(fn, shouldFit) { if (typeof fn === 'function') fn(api); if (shouldFit) fit(); return api; },
      setData(data) {
        if (state.destroyed || !worker) return api;
        const nodes = Array.isArray(data && data.nodes) ? data.nodes : [];
        const links = Array.isArray(data && data.links) ? data.links : (data && data.edges) || [];
        state.ready = false; state.error = null; state.lastLabelKey = '';
        worker.postMessage({ type: 'prepare', payload: { nodes, links } });
        return api;
      },
      setRenderMode(value) { state.renderMode = value === 'full' ? 'full' : 'all'; return api; },
      setPreset(value) {
        const preset = PRESETS[value] ? value : 'communities';
        state.settings = { ...state.settings, ...PRESETS[preset], mode: preset };
        pendingLayoutFit = true;
        postSettings(true, true);
        uploadNodeMeta();
        schedule();
        scheduleLabels(true);
        return { ...state.settings };
      },
      setStyle(value) { state.styleName = value || state.styleName; element.setAttribute('data-graph-style', state.styleName); uploadNodeMeta(); schedule(); scheduleLabels(true); return api; },
      setColorBy(value) { state.colorBy = value || state.colorBy; uploadNodeMeta(); schedule(); return api; },
      setPalette(value) { state.palette = typeof value === 'string' ? value : state.palette; if (state.palette !== 'custom') state.typeColors = {}; uploadNodeMeta(); schedule(); return api; },
      setTypeColors(value) { state.typeColors = value && typeof value === 'object' ? { ...state.typeColors, ...value } : {}; uploadNodeMeta(); schedule(); return api; },
      setThemeColors(value) { state.themeColors = value && typeof value === 'object' ? { ...value } : {}; uploadNodeMeta(); schedule(); return api; },
      setSettings(value) {
        const patch = value || {};
        state.settings = { ...state.settings, ...patch };
        state.flowPaintAt = 0;
        const relayout = Object.keys(patch).some(key => ['mode', 'repel', 'link', 'gravity'].includes(key));
        postSettings(relayout);
        uploadNodeMeta();
        camera();
        scheduleLabels(true);
        return api;
      },
      setScope(value) {
        state.scope = value && typeof value === 'object'
          ? { ...state.scope, ...value }
          : { minDegree: 0, showUnlinked: true, depth: 2 };
        refreshVisibility();
        camera();
        return api;
      },
      setRepoFilter(value) { state.repoFilter = String(value || '').slice(0, 200); return api; },
      setAsOf(value) { state.asOf = value || null; return api; },
      setSizeBy(value) { state.sizeBy = ['degree', 'betweenness', 'evidence_mass'].includes(value) ? value : 'degree'; uploadNodeMeta(); schedule(); return api; },
      setBridges(value) { state.bridges = value !== false; uploadEdges(); schedule(); if (typeof opts.onMetrics === 'function') opts.onMetrics(api.metrics()); return api; },
      setCollapse(value) {
        state.collapse = value === true ? true : value === 'auto' ? 'auto' : false;
        state.collapsed = value === true;
        if (typeof opts.onCollapseChange === 'function') opts.onCollapseChange(state.collapsed);
        stats();
        return api;
      },
      setGhosts(value) { state.ghosts = value !== false; refreshVisibility(); camera(); return api; },
      setLayers(value) { state.layers = value || null; return api; },
      setHighlight(id) {
        const index = state.idIndex.get(String(id));
        state.focus = index === undefined ? -1 : index;
        state.focusPoint = index === undefined ? [0, 0] : [state.positions[index * 2], state.positions[index * 2 + 1]];
        applyHoverToFlags();
        state.lastLabelKey = '';
        scheduleLabels(true);
        return api;
      },
      clearFocus() {
        state.focus = -1;
        state.focusPoint = [0, 0];
        applyHoverToFlags();
        state.lastLabelKey = '';
        scheduleLabels(true);
        return api;
      },
      reveal(id) {
        const index = state.idIndex.get(String(id));
        if (index === undefined || !state.nodeFlags[index]) return false;
        state.camera.x = state.positions[index * 2];
        state.camera.y = state.positions[index * 2 + 1];
        state.camera.scale = Math.max(1.2, state.camera.scale);
        state.focus = index;
        state.focusPoint = [state.positions[index * 2], state.positions[index * 2 + 1]];
        applyHoverToFlags();
        camera();
        return true;
      },
      focus(id) { return api.reveal(id); },
      zoomToNode(id) { return api.reveal(id); },
      communityMap() {
        const result = {};
        state.ids.forEach((id, index) => { result[id] = state.communities[index] || index; });
        return result;
      },
      resize, fit,
      reheat() {
        if (state.settings.frozen || !worker) return api;
        state.layoutPending = true;
        stats({ layoutPending: true });
        worker.postMessage({ type: 'reheat' });
        return api;
      },
      freeze(value = true) { state.settings.frozen = value !== false; return api.setSettings({ frozen: state.settings.frozen }); },
      pause() { state.paused = true; if (state.frame) { caf(state.frame); state.frame = 0; } return api; },
      resume() { state.paused = false; schedule(); scheduleLabels(); return api; },
      state() {
        return {
          mode: 'all', presentation: 'all',
          nodeCount: state.ids.length,
          visibleNodeCount: state.visibleCount,
          edgeCount: state.totalLinks,
          drawnEdgeCount: drawnEdgeEstimate(),
          renderer: gl && nodeProgram ? 'webgl2' : 'unsupported',
          collapsed: state.collapsed, collapse: state.collapse,
          scope: { ...state.scope },
          relationFlow: state.settings.flow === true,
          flowSpeed: Number(state.settings.flowSpeed || 0),
          layoutPending: state.layoutPending,
          frozen: state.settings.frozen === true,
          paused: state.paused === true,
        };
      },
      metrics() {
        const bridges = state.edgeBridges.reduce((count, value) => count + (value ? 1 : 0), 0);
        return {
          ...api.state(), bridges,
          top: Array.from(state.topNodes.slice(0, 5), node => ({
            id: state.ids[node], name: state.labels[node], score: state.degrees[node] || 0,
          })),
        };
      },
      physicsDiagnostics() {
        return {
          mode: 'all', simulation: false, layout: 'deterministic-worker',
          controls: 'bounded-layout-forces',
          relationFlow: state.settings.flow === true,
          frozen: state.settings.frozen === true,
          paused: state.paused === true,
        };
      },
      graphToScreen(x, y) {
        return {
          x: (Number(x) - state.camera.x) * state.camera.scale + state.width / 2,
          y: (Number(y) - state.camera.y) * state.camera.scale + state.height / 2,
        };
      },
      getPhysicsSnapshot() {
        const nodes = [];
        const limit = Math.min(128, state.topNodes.length);
        for (let index = 0; index < limit; index += 1) {
          const node = state.topNodes[index];
          nodes.push({
            id: state.ids[node], x: state.positions[node * 2], y: state.positions[node * 2 + 1],
            vx: 0, vy: 0, radius: pointSize(node), communityId: state.communities[node],
          });
        }
        return {
          center: null, nodes, systemAnchors: [],
          paused: state.settings.frozen === true || state.paused === true,
          diagnostics: api.physicsDiagnostics(),
        };
      },
      destroy: destroyGraph,
    };
    return api;
  }

  window.EngraphisEveryGraph = { create, MAX_NODES, MAX_LINKS };
  /* Compatibility alias: the legacy classic/static dashboards still address the all-node
     engine by its historical global; they get the Every-node implementation. */
  window.EngraphisAllGraph = window.EngraphisEveryGraph;
})();
