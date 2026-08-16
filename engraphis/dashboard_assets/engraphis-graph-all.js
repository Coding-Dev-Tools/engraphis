/* Progressive renderer for the explicit all-node profile. A worker prepares deterministic
   layouts and LOD sets; Galaxy mode advances authored solar systems as rigid orbital groups. */
(function () {
  'use strict';
  const WORKER_URL = '/v2-assets/engraphis-graph-worker.js?v=20260816-galaxy-even-orbits-2';
  const MAX_NODES = 20000;
  const MAX_LINKS = 200000;
  const FLOW_EDGE_LIMIT = 900;
  const FLOW_FRAME_MS = 34;
  const PALETTES = {
    cyber: ['#4bd8df', '#9a7cff', '#ed6fc2', '#6fe6b0', '#f0c674', '#6ba8ff'],
    galaxy: ['#72a8ff', '#9a87ff', '#d987ff', '#59d5e7', '#8ee3c7', '#f4c978'],
    solar: ['#e8a05c', '#e17f65', '#f2c66d', '#d36d8f', '#d9d28b', '#e99767'],
    classic: ['#9ab2c7', '#839db2', '#b0a4c8', '#7aa7a6', '#c0aa7b', '#8aa6c9'],
  };
  const TYPE_COLORS = { person_or_concept: '#8d82e3', mention: '#5ba1a6', hashtag: '#c9a15b', email: '#8eb3e6', organization: '#d48173', location: '#7ebf8e', memory: '#5ba1a6', repo: '#c9a15b', file: '#8eb3e6' };
  const PRESETS = {
    galaxy: { repel: 60, link: 8, gravity: 48, font: 12, size: 3, linkw: 0.72, labelDensity: 24 },
    original: { repel: 120, link: 30, gravity: 14, font: 13, size: 3, linkw: 1, labelDensity: 40 },
    compact: { repel: 42, link: 20, gravity: 26, font: 12, size: 3, linkw: 0.7, labelDensity: 30 },
    communities: { repel: 48, link: 16, gravity: 48, font: 12, size: 3, linkw: 0.72, labelDensity: 24 },
    radial: { repel: 68, link: 26, gravity: 12, font: 13, size: 3, linkw: 0.75, labelDensity: 55 },
    constellation: { repel: 34, link: 16, gravity: 38, font: 12, size: 3, linkw: 0.65, labelDensity: 35 },
  };
  const raf = window.requestAnimationFrame || (callback => window.setTimeout(callback, 16));
  const caf = window.cancelAnimationFrame || (handle => window.clearTimeout(handle));
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const color = value => /^#[0-9a-f]{6}$/i.test(String(value || '')) ? String(value) : '#86a8bf';
  const rgb = value => { const text = color(value).slice(1); return [parseInt(text.slice(0, 2), 16) / 255, parseInt(text.slice(2, 4), 16) / 255, parseInt(text.slice(4, 6), 16) / 255]; };

  function create(element, options) {
    if (!element) throw new Error('all graph renderer requires a host element');
    const opts = options || {}, canvas = document.createElement('canvas'), labels = document.createElement('canvas');
    canvas.className = 'engraphis-all-canvas'; labels.className = 'engraphis-all-labels'; canvas.setAttribute('aria-hidden', 'true'); labels.setAttribute('aria-hidden', 'true'); element.replaceChildren(canvas, labels);
    element.setAttribute('data-graph-style', opts.style || 'cyber');
    const gl = canvas.getContext('webgl2', { antialias: false, alpha: true, powerPreference: 'high-performance' });
    const labelContext = labels.getContext('2d');
    const worker = new Worker(WORKER_URL);
    const state = {
      ids: [], labels: [], types: [], communities: [], positions: new Float32Array(0), nodeVertexPositions: new Float32Array(0), nodeGhosts: new Uint8Array(0), nodeVisible: new Uint8Array(0), degrees: new Float32Array(0), betweenness: new Float32Array(0), evidenceMass: new Float32Array(0),
      edgeSources: new Uint32Array(0), edgeTargets: new Uint32Array(0), edgeBridges: new Uint8Array(0), edgeLayers: [], topNodes: new Uint32Array(0),
      visibleNodes: new Uint32Array(0), visibleEdges: new Uint32Array(0), visibleLabels: new Uint32Array(0),
      edgeVertexPositions: new Float32Array(0), edgeColors: new Float32Array(0), edgeVertexCount: 0, nodeColors: new Float32Array(0), nodeSizes: new Float32Array(0), bounds: null,
      camera: { x: 0, y: 0, scale: 1 }, width: 1, height: 1, dpr: 1, styleName: opts.style || 'cyber', colorBy: 'community', typeColors: {},
      settings: { labels: true, flow: false, flowSpeed: 45, frozen: false, mode: 'communities', repel: 48, link: 16, gravity: 48, font: 12, size: 3, linkw: 0.72, labelDensity: 24 },
      palette: 'theme', themeColors: {}, layers: null, sizeBy: 'degree', bridges: true, ghosts: true,
      scope: { minDegree: 1, showUnlinked: true, depth: 2 }, collapse: false, collapsed: false,
      focus: -1, hover: -1, ready: false, totalLinks: 0, drawnLinks: 0, visibleNodeCount: 0,
      frame: 0, flowPaintAt: 0, motionPending: false, layoutPending: false, hitRequest: 0, drag: null, destroyed: false, error: null,
    };
    let nodeProgram = null, edgeProgram = null, nodeBuffers = {}, edgeBuffers = {};
    let hitFrame = 0, pendingHit = null, layoutFrame = 0, pendingLayoutFit = false;
    const reducedMotion = () => {
      if (typeof opts.reducedMotion === 'function') return opts.reducedMotion() === true;
      if (opts.reducedMotion === true) return true;
      return typeof window.matchMedia === 'function'
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    };
    const screen = (x, y) => [(x - state.camera.x) * state.camera.scale + state.width / 2, (y - state.camera.y) * state.camera.scale + state.height / 2];
    const world = (x, y) => [(x - state.width / 2) / state.camera.scale + state.camera.x, (y - state.height / 2) / state.camera.scale + state.camera.y];
    function drawableNodeIndices() {
      const values = [];
      for (let index = 0; index < state.ids.length; index += 1) {
        const workerVisible = state.nodeVisible.length !== state.ids.length || state.nodeVisible[index];
        if (workerVisible && (state.ghosts || !state.nodeGhosts[index])) values.push(index);
      }
      return new Uint32Array(values);
    }
    function setVisibleNodes(values) {
      state.visibleNodes = values || new Uint32Array(0);
      state.nodeVisible = new Uint8Array(state.ids.length);
      for (let index = 0; index < state.visibleNodes.length; index += 1) {
        state.nodeVisible[state.visibleNodes[index]] = 1;
      }
      state.visibleNodeCount = state.visibleNodes.length;
    }
    function resize() {
      const rect = element.getBoundingClientRect(); state.width = Math.max(1, rect.width || element.clientWidth || 1); state.height = Math.max(1, rect.height || element.clientHeight || 1); state.dpr = Math.min(2, window.devicePixelRatio || 1);
      [canvas, labels].forEach(target => { target.width = Math.max(1, Math.floor(state.width * state.dpr)); target.height = Math.max(1, Math.floor(state.height * state.dpr)); });
      if (gl) gl.viewport(0, 0, canvas.width, canvas.height);
      /* LOD sets are viewport-dependent. A resize must refresh the worker camera too; a repaint
         alone can leave Canvas nodes, edges, and labels clipped to the previous dimensions. */
      if (state.ready) camera(); else schedule();
    }
    function nodeAt(index) { return { id: state.ids[index], label: state.labels[index] || state.ids[index], type: state.types[index] || 'person_or_concept' }; }
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
      const group = String(state.communities[index] || index), hash = Array.from(group).reduce((sum, char) => ((sum * 31) + char.charCodeAt(0)) >>> 0, 7);
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
    function shader(type, source) { const value = gl.createShader(type); gl.shaderSource(value, source); gl.compileShader(value); if (!gl.getShaderParameter(value, gl.COMPILE_STATUS)) throw new Error('all-node shader compilation failed'); return value; }
    function program(vertex, fragment) {
      const value = gl.createProgram();
      const vertexShader = shader(gl.VERTEX_SHADER, vertex);
      const fragmentShader = shader(gl.FRAGMENT_SHADER, fragment);
      gl.attachShader(value, vertexShader); gl.attachShader(value, fragmentShader);
      gl.linkProgram(value);
      const linked = gl.getProgramParameter(value, gl.LINK_STATUS);
      if (typeof gl.detachShader === 'function') {
        gl.detachShader(value, vertexShader); gl.detachShader(value, fragmentShader);
      }
      if (typeof gl.deleteShader === 'function') {
        gl.deleteShader(vertexShader); gl.deleteShader(fragmentShader);
      }
      if (!linked) {
        if (typeof gl.deleteProgram === 'function') gl.deleteProgram(value);
        throw new Error('all-node shader link failed');
      }
      return value;
    }
    function initWebgl() {
      if (!gl) return;
      try {
        const vertex = `#version 300 es
          in vec2 a_position; in vec3 a_color; in float a_size; uniform vec2 u_camera; uniform float u_scale; uniform vec2 u_resolution; out vec3 v_color;
          void main(){vec2 px=(a_position-u_camera)*u_scale+u_resolution*0.5;vec2 clip=px/u_resolution*2.0-1.0;gl_Position=vec4(clip.x,-clip.y,0.0,1.0);gl_PointSize=max(1.0,a_size*u_scale);v_color=a_color;}`;
        const fragment = `#version 300 es
          precision mediump float;in vec3 v_color;out vec4 outputColor;void main(){vec2 p=gl_PointCoord-0.5;if(dot(p,p)>0.25)discard;outputColor=vec4(v_color,0.92);}`;
        const edgeVertex = `#version 300 es
          in vec2 a_position;in vec3 a_color;uniform vec2 u_camera;uniform float u_scale;uniform vec2 u_resolution;out vec3 v_color;void main(){vec2 px=(a_position-u_camera)*u_scale+u_resolution*0.5;vec2 clip=px/u_resolution*2.0-1.0;gl_Position=vec4(clip.x,-clip.y,0.0,1.0);v_color=a_color;}`;
        const edgeFragment = `#version 300 es
          precision mediump float;in vec3 v_color;out vec4 outputColor;void main(){outputColor=vec4(v_color,0.2);}`;
        nodeProgram = program(vertex, fragment); edgeProgram = program(edgeVertex, edgeFragment);
        nodeBuffers.position = gl.createBuffer(); nodeBuffers.color = gl.createBuffer(); nodeBuffers.size = gl.createBuffer(); edgeBuffers.position = gl.createBuffer(); edgeBuffers.color = gl.createBuffer();
        nodeBuffers.attrs = { position: gl.getAttribLocation(nodeProgram, 'a_position'), color: gl.getAttribLocation(nodeProgram, 'a_color'), size: gl.getAttribLocation(nodeProgram, 'a_size'), camera: gl.getUniformLocation(nodeProgram, 'u_camera'), scale: gl.getUniformLocation(nodeProgram, 'u_scale'), resolution: gl.getUniformLocation(nodeProgram, 'u_resolution') };
        edgeBuffers.attrs = { position: gl.getAttribLocation(edgeProgram, 'a_position'), color: gl.getAttribLocation(edgeProgram, 'a_color'), camera: gl.getUniformLocation(edgeProgram, 'u_camera'), scale: gl.getUniformLocation(edgeProgram, 'u_scale'), resolution: gl.getUniformLocation(edgeProgram, 'u_resolution') };
      } catch (error) { nodeProgram = edgeProgram = null; if (window.console && console.warn) console.warn('All-node WebGL2 unavailable; using flat Canvas.', error); }
    }
    function updateNodes() {
      if (!gl || !nodeProgram || !state.ready) return;
      if (state.nodeVertexPositions.length !== state.positions.length) {
        state.nodeVertexPositions = new Float32Array(state.positions.length);
      }
      if (state.nodeColors.length !== state.ids.length * 3) state.nodeColors = new Float32Array(state.ids.length * 3);
      if (state.nodeSizes.length !== state.ids.length) state.nodeSizes = new Float32Array(state.ids.length);
      for (let index = 0; index < state.ids.length; index += 1) {
        const nodeRgb = rgb(nodeColor(index)), colorOffset = index * 3, positionOffset = index * 2;
        const visible = (state.nodeVisible.length !== state.ids.length || state.nodeVisible[index])
          && (state.ghosts || !state.nodeGhosts[index]);
        state.nodeVertexPositions[positionOffset] = visible
          ? state.positions[positionOffset] : Number.NaN;
        state.nodeVertexPositions[positionOffset + 1] = visible
          ? state.positions[positionOffset + 1] : Number.NaN;
        state.nodeColors[colorOffset] = nodeRgb[0]; state.nodeColors[colorOffset + 1] = nodeRgb[1]; state.nodeColors[colorOffset + 2] = nodeRgb[2];
        state.nodeSizes[index] = pointSize(index);
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.position); gl.bufferData(gl.ARRAY_BUFFER, state.nodeVertexPositions, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.color); gl.bufferData(gl.ARRAY_BUFFER, state.nodeColors, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.size); gl.bufferData(gl.ARRAY_BUFFER, state.nodeSizes, gl.DYNAMIC_DRAW);
    }
    function drawCanvas() {
      if (!labelContext) return; labelContext.setTransform(state.dpr, 0, 0, state.dpr, 0, 0); labelContext.clearRect(0, 0, state.width, state.height); labelContext.strokeStyle = 'rgba(124,163,183,0.17)'; labelContext.lineWidth = Math.max(0.35, Number(state.settings.linkw || 0.72)) * (state.camera.scale < 1 ? 0.65 : 1); labelContext.beginPath();
      for (let index = 0; index < state.visibleEdges.length; index += 1) { const edge = state.visibleEdges[index]; if (state.bridges && state.edgeBridges[edge]) continue; const source = state.edgeSources[edge], target = state.edgeTargets[edge], a = screen(state.positions[source * 2], state.positions[source * 2 + 1]), b = screen(state.positions[target * 2], state.positions[target * 2 + 1]); labelContext.moveTo(a[0], a[1]); labelContext.lineTo(b[0], b[1]); }
      labelContext.stroke();
      if (state.bridges) { labelContext.strokeStyle = 'rgba(244,211,127,0.62)'; labelContext.beginPath(); for (let index = 0; index < state.visibleEdges.length; index += 1) { const edge = state.visibleEdges[index]; if (!state.edgeBridges[edge]) continue; const source = state.edgeSources[edge], target = state.edgeTargets[edge], a = screen(state.positions[source * 2], state.positions[source * 2 + 1]), b = screen(state.positions[target * 2], state.positions[target * 2 + 1]); labelContext.moveTo(a[0], a[1]); labelContext.lineTo(b[0], b[1]); } labelContext.stroke(); }
      const visible = state.visibleNodes, compact = state.camera.scale < 0.55;
      for (let cursor = 0; cursor < visible.length; cursor += 1) { const index = visible[cursor], point = screen(state.positions[index * 2], state.positions[index * 2 + 1]); if (point[0] < -4 || point[0] > state.width + 4 || point[1] < -4 || point[1] > state.height + 4) continue; const radius = compact ? 1.3 : clamp(pointSize(index) * Math.min(1, state.camera.scale), 1, 7); labelContext.fillStyle = nodeColor(index); labelContext.fillRect(point[0] - radius, point[1] - radius, radius * 2, radius * 2); }
    }
    function updateEdges() {
      if (!gl || !edgeProgram) return;
      const edges = state.visibleEdges, required = edges.length * 4;
      if (state.edgeVertexPositions.length < required) state.edgeVertexPositions = new Float32Array(required);
      gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuffers.position); gl.bufferData(gl.ARRAY_BUFFER, state.edgeVertexPositions.subarray(0, required), gl.STREAM_DRAW);
      /* LINES consumes two vertices per relation, and an RGB attribute belongs to each vertex. */
      if (state.edgeColors.length < edges.length * 6) state.edgeColors = new Float32Array(edges.length * 6);
      for (let index = 0; index < edges.length; index += 1) {
        const bridge = state.bridges && state.edgeBridges[edges[index]];
        const value = rgb(bridge ? '#f4d37f' : '#638fa6'), offset = index * 6;
        state.edgeColors[offset] = value[0]; state.edgeColors[offset + 1] = value[1]; state.edgeColors[offset + 2] = value[2];
        state.edgeColors[offset + 3] = value[0]; state.edgeColors[offset + 4] = value[1]; state.edgeColors[offset + 5] = value[2];
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuffers.color); gl.bufferData(gl.ARRAY_BUFFER, state.edgeColors.subarray(0, edges.length * 6), gl.DYNAMIC_DRAW);
      state.edgeVertexCount = edges.length * 2;
    }
    function drawWebgl() {
      if (!gl || !nodeProgram || !state.ready) return false; gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT); gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      const edges = state.visibleEdges;
      if (edges.length) { gl.useProgram(edgeProgram); gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuffers.position); gl.enableVertexAttribArray(edgeBuffers.attrs.position); gl.vertexAttribPointer(edgeBuffers.attrs.position, 2, gl.FLOAT, false, 0, 0); gl.bindBuffer(gl.ARRAY_BUFFER, edgeBuffers.color); gl.enableVertexAttribArray(edgeBuffers.attrs.color); gl.vertexAttribPointer(edgeBuffers.attrs.color, 3, gl.FLOAT, false, 0, 0); gl.uniform2f(edgeBuffers.attrs.camera, state.camera.x, state.camera.y); gl.uniform1f(edgeBuffers.attrs.scale, state.camera.scale * state.dpr); gl.uniform2f(edgeBuffers.attrs.resolution, canvas.width, canvas.height); gl.lineWidth(Math.max(1, Number(state.settings.linkw || 0.72) * state.dpr)); gl.drawArrays(gl.LINES, 0, state.edgeVertexCount); }
      gl.useProgram(nodeProgram); gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.position); gl.enableVertexAttribArray(nodeBuffers.attrs.position); gl.vertexAttribPointer(nodeBuffers.attrs.position, 2, gl.FLOAT, false, 0, 0); gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.color); gl.enableVertexAttribArray(nodeBuffers.attrs.color); gl.vertexAttribPointer(nodeBuffers.attrs.color, 3, gl.FLOAT, false, 0, 0); gl.bindBuffer(gl.ARRAY_BUFFER, nodeBuffers.size); gl.enableVertexAttribArray(nodeBuffers.attrs.size); gl.vertexAttribPointer(nodeBuffers.attrs.size, 1, gl.FLOAT, false, 0, 0); gl.uniform2f(nodeBuffers.attrs.camera, state.camera.x, state.camera.y); gl.uniform1f(nodeBuffers.attrs.scale, state.camera.scale * state.dpr); gl.uniform2f(nodeBuffers.attrs.resolution, canvas.width, canvas.height); gl.drawArrays(gl.POINTS, 0, state.ids.length); return true;
    }
    function drawRelationFlow(now) {
      if (!labelContext || !state.settings.flow || !state.visibleEdges.length) return;
      const speed = clamp(Number(state.settings.flowSpeed || 0), 0, 100);
      const moving = speed > 0 && !state.settings.frozen && !state.settings.orbitPaused
        && !reducedMotion();
      const stride = Math.max(1, Math.ceil(state.visibleEdges.length / FLOW_EDGE_LIMIT));
      labelContext.save();
      labelContext.globalCompositeOperation = 'lighter';
      for (let cursor = 0; cursor < state.visibleEdges.length; cursor += stride) {
        const offset = cursor * 4;
        const a = screen(state.edgeVertexPositions[offset], state.edgeVertexPositions[offset + 1]);
        const b = screen(state.edgeVertexPositions[offset + 2], state.edgeVertexPositions[offset + 3]);
        if ((a[0] < -12 && b[0] < -12) || (a[0] > state.width + 12 && b[0] > state.width + 12)
          || (a[1] < -12 && b[1] < -12) || (a[1] > state.height + 12 && b[1] > state.height + 12)) continue;
        const edge = state.visibleEdges[cursor];
        const phase = moving
          ? ((now * (0.00006 + speed * 0.000018) + (edge % 997) / 997) % 1)
          : 0.68;
        const x = a[0] + (b[0] - a[0]) * phase, y = a[1] + (b[1] - a[1]) * phase;
        labelContext.fillStyle = state.bridges && state.edgeBridges[edge]
          ? 'rgba(255,220,132,0.88)' : 'rgba(115,220,239,0.72)';
        labelContext.beginPath();
        labelContext.arc(x, y, state.camera.scale < 0.8 ? 1.15 : 1.65, 0, Math.PI * 2);
        labelContext.fill();
      }
      labelContext.restore();
    }
    function drawLabels(clear = false, now = 0) {
      if (!labelContext) return; labelContext.setTransform(state.dpr, 0, 0, state.dpr, 0, 0); if (clear) labelContext.clearRect(0, 0, state.width, state.height); drawRelationFlow(now); labelContext.save(); const focused = state.focus >= 0 ? state.focus : state.hover;
      if (focused >= 0 && focused < state.ids.length) { const point = screen(state.positions[focused * 2], state.positions[focused * 2 + 1]); labelContext.beginPath(); labelContext.arc(point[0], point[1], clamp(7 + state.camera.scale * 2, 7, 15), 0, Math.PI * 2); labelContext.strokeStyle = '#f4d37f'; labelContext.lineWidth = 1.5; labelContext.stroke(); }
      if (state.settings.labels) { labelContext.font = `${clamp(Number(state.settings.font || 12) + state.camera.scale * 1.5, 8, 24)}px ui-sans-serif,system-ui,sans-serif`; labelContext.textBaseline = 'middle'; labelContext.fillStyle = 'rgba(224,236,241,0.86)'; for (let index = 0; index < state.visibleLabels.length; index += 1) { const item = state.visibleLabels[index], point = screen(state.positions[item * 2], state.positions[item * 2 + 1]); labelContext.fillText(state.labels[item] || state.ids[item], point[0] + 6, point[1] - 6); } }
      labelContext.restore();
    }
    function clearHover() {
      state.hitRequest += 1;
      pendingHit = null;
      if (hitFrame) { caf(hitFrame); hitFrame = 0; }
      state.hover = -1;
      element.classList.remove('engraphis-all-node-hover');
      if (gl && nodeProgram && labelContext) {
        labelContext.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
        labelContext.clearRect(0, 0, state.width, state.height);
      }
      schedule();
    }
    function flowAnimating() {
      return state.settings.flow && state.visibleEdges.length && Number(state.settings.flowSpeed || 0) > 0
        && !state.settings.frozen && !state.settings.orbitPaused && !reducedMotion();
    }
    function orbitAnimating() {
      return state.settings.mode === 'galaxy' && !state.settings.frozen && !state.settings.orbitPaused
        && !state.paused && !reducedMotion();
    }
    function draw(now = 0) {
      state.frame = 0;
      if (state.destroyed || state.paused || !state.ready) return;
      if (flowAnimating() && state.flowPaintAt && now - state.flowPaintAt < FLOW_FRAME_MS) {
        schedule(); return;
      }
      state.flowPaintAt = now;
      const webgl = drawWebgl(); if (!webgl) drawCanvas(); drawLabels(webgl, now);
      if (orbitAnimating() && !state.motionPending) {
        state.motionPending = true;
        worker.postMessage({ type: 'tick', deltaMs: FLOW_FRAME_MS });
      }
      if (flowAnimating() || orbitAnimating()) schedule();
    }
    function schedule() { if (!state.destroyed && !state.paused && !state.frame) state.frame = raf(draw); }
    function camera() { if (!state.ready) return; worker.postMessage({ type: 'camera', x: state.camera.x, y: state.camera.y, scale: state.camera.scale, width: state.width, height: state.height }); schedule(); }
    function postSettings(relayout, fitLayout = false) {
      if (!relayout) {
        worker.postMessage({ type: 'settings', settings: state.settings, relayout: false });
        return;
      }
      pendingLayoutFit = pendingLayoutFit || fitLayout;
      if (layoutFrame) return;
      /* Range inputs can emit faster than a 20k/200k worker pass can settle. Keep only the
         latest values per display frame so stale force calculations never build a queue. */
      layoutFrame = raf(() => {
        layoutFrame = 0;
        const fit = pendingLayoutFit; pendingLayoutFit = false;
        state.layoutPending = true;
        if (state.ready) stats({ layoutPending: true });
        worker.postMessage({ type: 'settings', settings: state.settings, relayout: true, fit });
      });
    }
    function fit() { if (!state.positions.length) return; const bounds = state.bounds || { minX: state.positions[0], maxX: state.positions[0], minY: state.positions[1], maxY: state.positions[1] }; state.camera.x = (bounds.minX + bounds.maxX) / 2; state.camera.y = (bounds.minY + bounds.maxY) / 2; state.camera.scale = clamp(Math.min(state.width / Math.max(120, bounds.maxX - bounds.minX + 120), state.height / Math.max(120, bounds.maxY - bounds.minY + 120)), 0.03, 4); camera(); }
    function stats(extra) { if (typeof opts.onStats === 'function') opts.onStats({ nodes: state.ids.length, visibleNodes: state.visibleNodeCount || state.visibleNodes.length, links: state.totalLinks, drawnLinks: state.drawnLinks, hiddenLinks: Math.max(0, state.totalLinks - state.drawnLinks), collapsed: state.collapsed, relationFlow: state.settings.flow === true, layoutPending: state.layoutPending, presentation: 'all', preset: 'All nodes · LOD', renderer: gl && nodeProgram ? 'webgl2' : 'canvas', ...extra }); }
    /* Coalesce pointer samples to the display cadence. Otherwise a high-polling mouse can queue
       hundreds of obsolete worker hit tests behind the latest camera request. */
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
        worker.postMessage({ type: 'hit', request: ++state.hitRequest,
          x: point[0], y: point[1], scale: state.camera.scale });
      });
    }
    function focus(index) { state.focus = index; worker.postMessage({ type: 'focus', index }); camera(); }
    function handleWorkerFailure(event) {
      if (state.destroyed) return;
      const source = event && event.error;
      const error = source instanceof Error ? source
        : new Error(event && event.type === 'messageerror'
          ? 'All-node worker returned an unreadable response.'
          : 'All-node worker failed while preparing the graph.');
      error.code = error.code || 'GRAPH_WORKER';
      state.error = { code: error.code, message: error.message };
      state.ready = false;
      if (typeof opts.onError === 'function') opts.onError(error);
    }
    worker.addEventListener('error', handleWorkerFailure);
    worker.addEventListener('messageerror', handleWorkerFailure);
    function handleWorkerMessage(event) {
      const message = event.data || {};
      if (message.type === 'capacity') {
        const resource = message.resource === 'relations' ? 'relations' : 'nodes';
        const error = new Error(`All-node capacity exceeded: ${message.count.toLocaleString()} ${resource} (limit ${Number(message.limit || (resource === 'relations' ? MAX_LINKS : MAX_NODES)).toLocaleString()}). Filter the graph before loading all nodes.`);
        error.code = 'GRAPH_CAPACITY';
        state.error = { code: error.code, message: error.message };
        if (typeof opts.onError === 'function') opts.onError(error);
        return;
      }
      if (message.type === 'motion') {
        state.motionPending = false;
        state.positions = message.positions || state.positions;
        state.bounds = message.bounds || state.bounds;
        if (!state.ready) return;
        updateNodes(); camera(); schedule();
        return;
      }
      if (message.type === 'preview') {
        state.ids = message.ids || [];
        state.labels = message.labels || [];
        state.types = message.types || state.types;
        state.positions = message.positions || new Float32Array(0);
        state.nodeGhosts = message.nodeGhosts || state.nodeGhosts;
        state.bounds = message.bounds || null;
        state.communities = message.communities || [];
        state.degrees = new Float32Array(state.ids.length);
        state.nodeVisible = new Uint8Array(state.ids.length); state.nodeVisible.fill(1);
        setVisibleNodes(drawableNodeIndices());
        state.ready = true;
        updateNodes(); fit(); stats({ progressive: true, linksPending: true });
        return;
      }
      if (message.type === 'ready') {
        state.ids = message.ids || [];
        state.labels = message.labels || [];
        state.types = message.types || state.types;
        state.positions = message.positions || new Float32Array(0);
        state.nodeGhosts = message.nodeGhosts || state.nodeGhosts;
        state.bounds = message.bounds || null;
        state.degrees = message.degrees || new Float32Array(0);
        state.betweenness = message.betweenness || new Float32Array(0);
        state.evidenceMass = message.evidenceMass || new Float32Array(0);
        state.communities = message.communities || [];
        state.edgeSources = message.edgeSources || new Uint32Array(0);
        state.edgeTargets = message.edgeTargets || new Uint32Array(0);
        state.edgeBridges = message.edgeBridges || new Uint8Array(0);
        state.edgeLayers = message.edgeLayers || [];
        state.topNodes = message.topNodes || new Uint32Array(0);
        state.totalLinks = Number(message.totalLinks || 0);
        state.nodeVisible = new Uint8Array(state.ids.length); state.nodeVisible.fill(1);
        setVisibleNodes(drawableNodeIndices());
        state.ready = true;
        updateNodes(); fit();
        if (typeof opts.onMetrics === 'function') opts.onMetrics(api.metrics());
        stats({ progressive: true });
        return;
      }
      if (message.type === 'visible') {
        setVisibleNodes(message.nodes || state.visibleNodes);
        state.visibleEdges = message.edges || new Uint32Array(0);
        state.visibleLabels = message.labels || new Uint32Array(0);
        state.edgeVertexPositions = message.edgePositions || new Float32Array(0);
        state.drawnLinks = Number(message.drawnLinks || 0);
        state.collapsed = message.collapsed === true;
        updateEdges(); stats(); schedule();
        return;
      }
      if (message.type === 'collapse') {
        state.collapsed = message.value === true;
        if (typeof opts.onCollapseChange === 'function') opts.onCollapseChange(state.collapsed);
        stats();
        return;
      }
      if (message.type === 'hit') {
        if (message.request !== state.hitRequest) return;
        const next = Number.isInteger(message.index) ? message.index : -1;
        if (next === state.hover) return;
        state.hover = next;
        element.classList.toggle('engraphis-all-node-hover', next >= 0);
        if (typeof opts.onHover === 'function') {
          opts.onHover(next >= 0 ? nodeAt(next) : null);
        }
        schedule();
      }
    }
    worker.onmessage = handleWorkerMessage;
    function handleWorkerLayout(event) {
      const message = event.data || {};
      if (message.type !== 'layout') return;
      state.positions = message.positions || state.positions;
      state.bounds = message.bounds || state.bounds;
      state.layoutPending = false;
      if (!state.ready) return;
      updateNodes();
      if (message.fit) fit(); else camera();
      stats({ layoutPending: false });
      schedule();
    }
    worker.addEventListener('message', handleWorkerLayout);
    canvas.addEventListener('pointerdown', event => { if (event.button !== 0) return; state.drag = { x: event.clientX, y: event.clientY, cameraX: state.camera.x, cameraY: state.camera.y, moved: false }; canvas.setPointerCapture(event.pointerId); event.preventDefault(); });
    canvas.addEventListener('pointermove', event => { if (state.drag) { const dx = event.clientX - state.drag.x, dy = event.clientY - state.drag.y; if (Math.abs(dx) + Math.abs(dy) > 3) state.drag.moved = true; state.camera.x = state.drag.cameraX - dx / state.camera.scale; state.camera.y = state.drag.cameraY - dy / state.camera.scale; camera(); } else requestHit(event); });
    canvas.addEventListener('pointerup', event => { const drag = state.drag; state.drag = null; if (!drag || drag.moved) return; if (state.hover >= 0 && typeof opts.onNodeClick === 'function') opts.onNodeClick(nodeAt(state.hover)); else if (typeof opts.onBackgroundClick === 'function') opts.onBackgroundClick(); });
    canvas.addEventListener('pointerleave', () => { if (!state.drag) clearHover(); });
    canvas.addEventListener('pointerout', event => { if (!state.drag && (!event.relatedTarget || event.relatedTarget !== canvas)) clearHover(); });
    canvas.addEventListener('wheel', event => { const rect = element.getBoundingClientRect(), before = world(event.clientX - rect.left, event.clientY - rect.top), nextScale = clamp(state.camera.scale * Math.exp(-event.deltaY * 0.0012), 0.02, 7); state.camera.scale = nextScale; const after = world(event.clientX - rect.left, event.clientY - rect.top); state.camera.x += before[0] - after[0]; state.camera.y += before[1] - after[1]; camera(); event.preventDefault(); }, { passive: false });
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null; if (observer) observer.observe(element); else window.addEventListener('resize', resize); initWebgl(); worker.postMessage({ type: 'renderer', canvasFallback: !(gl && nodeProgram) }); resize();
    /* WebGL's default drawing buffer is not preserved and labels live on a second canvas. Paint
       once synchronously, then composite both layers while the GPU buffer is still readable. */
    function exportImageCanvas() {
      if (state.destroyed || !state.ready) return null;
      if (state.frame) { caf(state.frame); state.frame = 0; }
      const webgl = drawWebgl();
      if (!webgl) drawCanvas();
      drawLabels(webgl, typeof performance !== 'undefined' ? performance.now() : 0);
      const output = document.createElement('canvas');
      output.width = canvas.width; output.height = canvas.height;
      const context = output.getContext('2d');
      if (!context) return null;
      context.drawImage(canvas, 0, 0);
      context.drawImage(labels, 0, 0);
      return output;
    }
    function destroyGraph() {
      if (state.destroyed) return;
      state.destroyed = true;
      state.paused = true;
      state.hitRequest += 1;
      pendingHit = null;
      if (hitFrame) { caf(hitFrame); hitFrame = 0; }
      if (layoutFrame) { caf(layoutFrame); layoutFrame = 0; }
      if (state.frame) { caf(state.frame); state.frame = 0; }
      worker.onmessage = null;
      worker.removeEventListener('message', handleWorkerLayout);
      worker.removeEventListener('error', handleWorkerFailure);
      worker.removeEventListener('messageerror', handleWorkerFailure);
      worker.terminate();
      if (observer) observer.disconnect();
      else window.removeEventListener('resize', resize);
      if (gl) {
        [nodeBuffers.position, nodeBuffers.color, nodeBuffers.size,
          edgeBuffers.position, edgeBuffers.color].forEach(buffer => {
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
      state.ids = []; state.labels = []; state.types = []; state.communities = [];
      state.positions = state.nodeVertexPositions = new Float32Array(0);
      state.nodeVisible = state.nodeGhosts = new Uint8Array(0);
      state.nodeColors = state.nodeSizes = new Float32Array(0);
      state.visibleNodes = state.visibleEdges = state.visibleLabels = new Uint32Array(0);
      element.removeAttribute('data-graph-style');
      element.replaceChildren();
    }
    const api = {
      exportImageCanvas,
      apply(fn, shouldFit) { if (typeof fn === 'function') fn(api); if (shouldFit) fit(); return api; },
      setData(data) { if (state.destroyed) return api; const nodes = Array.isArray(data && data.nodes) ? data.nodes : [], links = Array.isArray(data && data.links) ? data.links : (data && data.edges) || []; state.ready = false; state.error = null; worker.postMessage({ type: 'prepare', payload: { nodes, links } }); return api; },
      setRenderMode(value) { state.renderMode = value === 'full' ? 'full' : 'all'; return api; },
      setPreset(value) { const preset = PRESETS[value] ? value : 'communities'; const next = { ...state.settings, ...PRESETS[preset], mode: preset }; state.settings = next; pendingLayoutFit = true; postSettings(true, true); updateNodes(); schedule(); return { ...next }; },
      setStyle(value) { state.styleName = value || state.styleName; element.setAttribute('data-graph-style', state.styleName); updateNodes(); schedule(); return api; },
      setColorBy(value) { state.colorBy = value || state.colorBy; updateNodes(); schedule(); return api; },
      setPalette(value) { state.palette = typeof value === 'string' ? value : state.palette; if (state.palette !== 'custom') state.typeColors = {}; updateNodes(); schedule(); return api; },
      setTypeColors(value) { state.typeColors = value && typeof value === 'object' ? { ...state.typeColors, ...value } : {}; updateNodes(); schedule(); return api; },
      setThemeColors(value) { state.themeColors = value && typeof value === 'object' ? { ...value } : {}; updateNodes(); schedule(); return api; },
      setSettings(value) { const patch = value || {}; state.settings = { ...state.settings, ...patch }; state.flowPaintAt = 0; const relayout = Object.keys(patch).some(key => ['mode', 'repel', 'link', 'gravity', 'gravitationalConstant', 'localGravitationalConstant', 'blackHoleMass', 'damping', 'springStiffness'].includes(key)); postSettings(relayout); updateNodes(); camera(); schedule(); return api; },
      setScope(value) { state.scope = value && typeof value === 'object' ? { ...state.scope, ...value } : { minDegree: 1, showUnlinked: true, depth: 2 }; worker.postMessage({ type: 'scope', scope: state.scope }); camera(); return api; },
      setRepoFilter(value) { state.repoFilter = String(value || '').slice(0, 200); return api; },
      setAsOf(value) { state.asOf = value || null; return api; },
      setSizeBy(value) { state.sizeBy = ['degree', 'betweenness', 'evidence_mass'].includes(value) ? value : 'degree'; updateNodes(); schedule(); return api; },
      setBridges(value) { state.bridges = value !== false; updateEdges(); if (typeof opts.onMetrics === 'function') opts.onMetrics(api.metrics()); schedule(); return api; },
      setCollapse(value) { state.collapse = value === true ? true : value === 'auto' ? 'auto' : false; worker.postMessage({ type: 'collapse', value: state.collapse }); camera(); return api; },
      setGhosts(value) { state.ghosts = value !== false; setVisibleNodes(drawableNodeIndices()); updateNodes(); worker.postMessage({ type: 'ghosts', value: state.ghosts }); camera(); schedule(); return api; },
      setLayers(value) { state.layers = value || null; worker.postMessage({ type: 'layers', layers: state.layers }); camera(); return api; }, setHighlight(id) { focus(state.ids.indexOf(String(id))); return api; }, clearFocus() { focus(-1); return api; }, reveal(id) { const index = state.ids.indexOf(String(id)); if (index < 0) return false; state.camera.x = state.positions[index * 2]; state.camera.y = state.positions[index * 2 + 1]; state.camera.scale = Math.max(1.2, state.camera.scale); focus(index); return true; }, focus(id) { return api.reveal(id); }, zoomToNode(id) { return api.reveal(id); }, communityMap() { const result = {}; state.ids.forEach((id, index) => { result[id] = state.communities[index] || index; }); return result; }, resize, fit, reheat() { if (state.settings.frozen) return api; state.layoutPending = true; stats({ layoutPending: true }); worker.postMessage({ type: 'reheat' }); return api; }, freeze(value = true) { state.settings.frozen = value !== false; return api.setSettings({ frozen: state.settings.frozen }); }, pause() { state.paused = true; if (state.frame) { caf(state.frame); state.frame = 0; } return api; }, resume() { state.paused = false; schedule(); return api; }, state() { return { mode: 'all', presentation: 'all', nodeCount: state.ids.length, visibleNodeCount: state.visibleNodeCount, edgeCount: state.totalLinks, drawnEdgeCount: state.drawnLinks, renderer: gl && nodeProgram ? 'webgl2' : 'canvas', collapsed: state.collapsed, collapse: state.collapse, scope: { ...state.scope }, relationFlow: state.settings.flow === true, flowSpeed: Number(state.settings.flowSpeed || 0), layoutPending: state.layoutPending, frozen: state.settings.frozen === true, paused: state.paused === true }; }, metrics() { const bridges = state.edgeBridges.reduce((count, value) => count + (value ? 1 : 0), 0); return { ...api.state(), bridges, top: Array.from(state.topNodes.slice(0, 5), node => ({ id: state.ids[node], name: state.labels[node], score: state.degrees[node] || 0 })) }; }, physicsDiagnostics() { return { mode: 'all', simulation: false, layout: 'deterministic-worker', controls: 'bounded-layout-forces', relationFlow: state.settings.flow === true, frozen: state.settings.frozen === true, paused: state.paused === true }; }, graphToScreen(x, y) { return { x: (Number(x) - state.camera.x) * state.camera.scale + state.width / 2, y: (Number(y) - state.camera.y) * state.camera.scale + state.height / 2 }; }, getPhysicsSnapshot() { const nodes = []; const limit = Math.min(128, state.topNodes.length); for (let index = 0; index < limit; index += 1) { const node = state.topNodes[index]; nodes.push({ id: state.ids[node], x: state.positions[node * 2], y: state.positions[node * 2 + 1], vx: 0, vy: 0, radius: pointSize(node), communityId: state.communities[node] }); } return { center: null, nodes, systemAnchors: [], paused: state.settings.frozen === true || state.paused === true, diagnostics: api.physicsDiagnostics() }; }, destroy: destroyGraph,
    };
    return api;
  }
  window.EngraphisAllGraph = { create, MAX_NODES, MAX_LINKS };
})();
