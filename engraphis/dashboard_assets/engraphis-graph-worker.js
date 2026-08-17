/* Worker for the explicit 20k-node profile. Object-heavy preparation, deterministic placement,
   ranked edge selection and spatial hit testing stay off the UI thread. */
(function () {
  'use strict';
  const MAX_NODES = 20000;
  const MAX_LINKS = 200000;
  const LOW_ZOOM_EDGE_LIMIT = 0;
  const MEDIUM_ZOOM_EDGE_LIMIT = 25000;
  const HIGH_ZOOM_EDGE_LIMIT = 75000;
  const CANVAS_MEDIUM_ZOOM_EDGE_LIMIT = 8000;
  const CANVAS_HIGH_ZOOM_EDGE_LIMIT = 25000;
  const LABEL_LIMIT = 220;
  const CELL_SIZE = 48;
  const FAR_ENTER = 0.35, FAR_EXIT = 0.45;
  const MEDIUM_ENTER = 0.9, MEDIUM_EXIT = 1.2;
  const FAR_NODE_BUDGET = 500, MEDIUM_NODE_BUDGET = 3000;
  const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
  const state = {
    ids: [], labels: [], types: [], positions: new Float32Array(0), basePositions: new Float32Array(0), degrees: new Float32Array(0), betweenness: new Float32Array(0), evidenceMass: new Float32Array(0), nodeGhosts: new Uint8Array(0),
    communities: [], topNodes: new Uint32Array(0), edgeSources: new Uint32Array(0),
    edgeTargets: new Uint32Array(0), edgeStrength: new Float32Array(0), edgeLayers: [], edgeBridges: new Uint8Array(0), edgeGhosts: new Uint8Array(0),
    edgeOrder: new Uint32Array(0), edgeRank: new Uint32Array(0), adjacencyOffsets: new Uint32Array(0),
    adjacencyEdges: new Uint32Array(0), edgeSeen: new Uint32Array(0), edgeStamp: 0,
    nodeSeen: new Uint32Array(0), nodeStamp: 0,
    allNodes: new Uint32Array(0), grid: new Map(), layers: null, focusIndex: -1,
    lastCameraKey: '', lastVisibleNodes: new Uint32Array(0), lastVisibleEdges: new Uint32Array(0),
    lastVisibleLabels: new Uint32Array(0), canvasFallback: false, showBridges: true, showGhosts: true, paintOrder: new Uint32Array(0),
    layoutSettings: {}, labelDensity: 24,
    scope: { minDegree: 1, showUnlinked: true, depth: 2 }, collapseMode: false,
    collapsed: false, lodTier: 'medium', lastVisibleMask: new Uint8Array(0),
    layoutRevision: 0,
  };
  const finite = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const key = value => String(value == null ? '' : value);
  /* Preserve valid falsy ids such as 0 and false. A boolean fallback chain drops them and can
     stringify endpoint objects as "[object Object]" instead of reading their stable id. */
  function endpoint(link, side) {
    if (!link || typeof link !== 'object') return '';
    const alternate = side === 'source' ? 'from' : 'to';
    const value = link[side] !== undefined ? link[side] : link[alternate];
    return key(value && typeof value === 'object' ? value.id : value);
  }
  const cellKey = (x, y) => `${Math.floor(x / CELL_SIZE)},${Math.floor(y / CELL_SIZE)}`;
  function rebuildGrid() {
    state.grid = new Map();
    for (let index = 0; index < state.ids.length; index += 1) {
      const bucket = cellKey(state.positions[index * 2], state.positions[index * 2 + 1]);
      if (!state.grid.has(bucket)) state.grid.set(bucket, []);
      state.grid.get(bucket).push(index);
    }
  }
  function cameraKey(camera) {
    return [
      finite(camera && camera.x, 0), finite(camera && camera.y, 0),
      finite(camera && camera.scale, 1), finite(camera && camera.width, 1),
      finite(camera && camera.height, 1), state.focusIndex,
      state.layers ? JSON.stringify(state.layers) : '',
      state.scope.minDegree, state.scope.showUnlinked, state.scope.depth,
      state.collapseMode || '', state.showGhosts,
      state.lodTier,
    ].join('|');
  }
  function makePositions(nodes, groups) {
    const result = new Float32Array(nodes.length * 2);
    const order = [...groups.keys()].sort((a, b) => groups.get(b).length - groups.get(a).length || a.localeCompare(b));
    const groupIndex = new Map(order.map((value, index) => [value, index]));
    const radius = Math.max(280, Math.sqrt(nodes.length) * 18), offsets = new Map();
    nodes.forEach((node, index) => {
      const group = key(node && (node.community_id != null ? node.community_id : node.community));
      const ordinal = offsets.get(group) || 0; offsets.set(group, ordinal + 1);
      const groupNumber = groupIndex.get(group) || 0, count = Math.max(1, order.length);
      const angle = groupNumber * GOLDEN_ANGLE * 7;
      const groupRadius = count === 1 ? 0 : radius * (0.35 + 0.65 * Math.sqrt((groupNumber + 1) / count));
      const localRadius = Math.max(16, Math.sqrt((groups.get(group) || []).length) * 13);
      const localAngle = ordinal * GOLDEN_ANGLE, distance = Math.min(Math.sqrt(ordinal + 1) * 5.5, localRadius);
      const x = finite(node && node.x, NaN), y = finite(node && node.y, NaN);
      result[index * 2] = Number.isFinite(x) ? x : Math.cos(angle) * groupRadius + Math.cos(localAngle) * distance;
      result[index * 2 + 1] = Number.isFinite(y) ? y : Math.sin(angle) * groupRadius * 0.72 + Math.sin(localAngle) * distance * 0.8;
    });
    return result;
  }
  function makeBounds(positions) {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (let index = 0; index < positions.length; index += 2) {
      minX = Math.min(minX, positions[index]); maxX = Math.max(maxX, positions[index]);
      minY = Math.min(minY, positions[index + 1]); maxY = Math.max(maxY, positions[index + 1]);
    }
    return { minX: Number.isFinite(minX) ? minX : 0, maxX: Number.isFinite(maxX) ? maxX : 0, minY: Number.isFinite(minY) ? minY : 0, maxY: Number.isFinite(maxY) ? maxY : 0 };
  }
  function applyLayout(notify = false, fit = false) {
    if (!state.basePositions.length) return;
    const settings = state.layoutSettings || {}, mode = key(settings.mode || 'communities');
    /* Galaxy coordinates and hierarchy are server-authored. Full-mode LOD may cull and paint
       them, but it must not run a second client layout or spiral transform over that scene. */
    if (mode === 'galaxy') {
      state.positions = state.basePositions.slice();
      rebuildGrid();
      state.lastCameraKey = '';
      if (notify) {
        const positions = state.positions.slice();
        self.postMessage(
          { type: 'layout', positions, bounds: makeBounds(state.positions), fit },
          [positions.buffer],
        );
      }
      return;
    }
    const repel = Math.max(0, finite(settings.repel, 48)), link = Math.max(1, finite(settings.link, 16));
    const gravity = Math.max(0, finite(settings.gravity, 48));
    const galacticGravity = Math.max(0, finite(settings.gravitationalConstant, 1));
    const blackHoleMass = Math.max(0.1, finite(settings.blackHoleMass, 1));
    const localGravity = Math.max(0, finite(settings.localGravitationalConstant, 1));
    const damping = Math.max(0, finite(settings.damping, 1));
    const spring = Math.max(0, finite(settings.springStiffness, 1));
    const modeScale = { original: 1.32, compact: 0.76, communities: 1, radial: 1.08, constellation: 1.18, galaxy: 1.04 }[mode] || 1;
    /* The All profile stays deterministic and worker-only, but its controls are real forces:
       repel expands the initial envelope, link is the spring target below, and gravity pulls
       the settled result toward the global centre. The bounded passes are O(nodes + links). */
    const repelSpread = 0.58 + repel / 72;
    const gravityTightening = 1 / (0.72 + gravity / 128 + galacticGravity * blackHoleMass * 0.05);
    const spaceSpread = 0.86 + localGravity * 0.07 - Math.min(2, damping) * 0.035;
    const spread = modeScale * clamp(repelSpread * gravityTightening * spaceSpread, 0.42, 3.2);
    const baseBounds = makeBounds(state.basePositions), centerX = (baseBounds.minX + baseBounds.maxX) / 2, centerY = (baseBounds.minY + baseBounds.maxY) / 2;
    state.positions = new Float32Array(state.basePositions.length);
    for (let index = 0; index < state.basePositions.length; index += 2) {
      let x = state.basePositions[index] - centerX, y = state.basePositions[index + 1] - centerY;
      if (mode === 'radial') { const angle = Math.atan2(y, x), radius = Math.hypot(x, y) * spread; x = Math.cos(angle) * radius; y = Math.sin(angle) * radius; }
      else if (mode === 'constellation') { x *= spread; y = y * spread * 0.72 + Math.sin(index * GOLDEN_ANGLE + state.layoutRevision) * 8; }
      else if (mode === 'galaxy') { const radius = Math.hypot(x, y) * spread, angle = Math.atan2(y, x) + radius * 0.0007; x = Math.cos(angle) * radius; y = Math.sin(angle) * radius * 0.72; }
      else { x *= spread; y *= spread; }
      if (state.layoutRevision) {
        const phase = (index / 2 + 1) * GOLDEN_ANGLE + state.layoutRevision * 0.73;
        const jitter = Math.min(10, 1.5 + link * 0.08);
        x += Math.cos(phase) * jitter; y += Math.sin(phase) * jitter;
      }
      state.positions[index] = x + centerX; state.positions[index + 1] = y + centerY;
    }
    if (state.edgeSources.length) {
      const nodeCount = state.positions.length / 2;
      const desired = clamp(10 + link * 1.25, 14, 112);
      const springForce = clamp(0.025 + spring * 0.018, 0.025, 0.2)
        * (mode === 'compact' ? 1.22 : mode === 'original' ? 0.72 : 1);
      const settle = 1 / (1 + Math.min(12, damping) * 0.18);
      const passes = nodeCount > 12000 ? 1 : 2;
      const delta = new Float32Array(state.positions.length);
      const counts = new Float32Array(nodeCount);
      for (let pass = 0; pass < passes; pass += 1) {
        delta.fill(0); counts.fill(0);
        for (let edge = 0; edge < state.edgeSources.length; edge += 1) {
          const source = state.edgeSources[edge], target = state.edgeTargets[edge];
          const sx = state.positions[source * 2], sy = state.positions[source * 2 + 1];
          const dx = state.positions[target * 2] - sx, dy = state.positions[target * 2 + 1] - sy;
          const distance = Math.max(0.001, Math.hypot(dx, dy));
          const strength = clamp(Math.log1p(Math.max(0, state.edgeStrength[edge])) / 3, 0.3, 1.4);
          const pull = clamp((distance - desired) / distance, -1.5, 1.5) * springForce * strength;
          delta[source * 2] += dx * pull; delta[source * 2 + 1] += dy * pull;
          delta[target * 2] -= dx * pull; delta[target * 2 + 1] -= dy * pull;
          counts[source] += 1; counts[target] += 1;
        }
        const centrePull = clamp((gravity / 400 + galacticGravity * blackHoleMass * 0.035) * 0.05, 0, 0.075);
        for (let index = 0; index < nodeCount; index += 1) {
          const offset = index * 2, divisor = Math.max(1, counts[index]);
          const x = state.positions[offset], y = state.positions[offset + 1];
          state.positions[offset] = x + delta[offset] / divisor * settle + (centerX - x) * centrePull;
          state.positions[offset + 1] = y + delta[offset + 1] / divisor * settle + (centerY - y) * centrePull;
        }
      }
    }
    rebuildGrid(); state.lastCameraKey = '';
    if (notify) { const positions = state.positions.slice(); self.postMessage({ type: 'layout', positions, bounds: makeBounds(state.positions), fit }, [positions.buffer]); }
  }
  function edgeAllowed(edge) {
    if (state.layers && state.layers[state.edgeLayers[edge]] === false) return false;
    if (!state.showGhosts && state.edgeGhosts[edge]) return false;
    return true;
  }
  function rebuildPaintOrder() {
    const values = [];
    for (let index = 0; index < state.edgeOrder.length; index += 1) {
      const edge = state.edgeOrder[index];
      if (edgeAllowed(edge)) values.push(edge);
    }
    state.paintOrder = new Uint32Array(values);
  }
  function prepare(payload) {
    const input = (Array.isArray(payload && payload.nodes) ? payload.nodes : []).slice().sort((a, b) => key(a && a.id).localeCompare(key(b && b.id)));
    const inputLinks = Array.isArray(payload && (payload.links || payload.edges)) ? (payload.links || payload.edges) : [];
    if (input.length > MAX_NODES) { self.postMessage({ type: 'capacity', resource: 'nodes', count: input.length, limit: MAX_NODES }); return; }
    if (inputLinks.length > MAX_LINKS) { self.postMessage({ type: 'capacity', resource: 'relations', count: inputLinks.length, limit: MAX_LINKS }); return; }
    const nodes = [], ids = [], labels = [], nodeIndex = new Map(), groups = new Map();
    input.forEach(node => {
      const id = key(node && node.id); if (!id || nodeIndex.has(id)) return;
      nodeIndex.set(id, ids.length); nodes.push(node || {}); ids.push(id);
      labels.push(key(node && (node.label || node.name || id)));
      const group = key(node && (node.community_id != null ? node.community_id : node.community));
      if (!groups.has(group)) groups.set(group, []); groups.get(group).push(ids.length - 1);
    });
    const positions = makePositions(nodes, groups);
    const nodeGhosts = new Uint8Array(nodes.map(node => node && node.ghost === true ? 1 : 0));
    state.basePositions = positions.slice();
    state.positions = positions.slice();
    state.layoutRevision = 0;
    state.lastVisibleMask = new Uint8Array(ids.length);
    const communities = nodes.map(node => key(
      node && (node.community_id != null ? node.community_id : node.community)));
    const types = nodes.map(node => key(
      node && (node.etype || node.type || 'person_or_concept')));
    const previewPositions = state.positions.slice();
    const previewGhosts = nodeGhosts.slice();
    self.postMessage({ type: 'preview', ids, labels, types, positions: previewPositions, communities, nodeGhosts: previewGhosts, bounds: makeBounds(state.positions), totalNodes: ids.length }, [previewPositions.buffer, previewGhosts.buffer]);
    const degrees = new Float32Array(ids.length), edges = [];
    inputLinks.forEach((link, ordinal) => {
      const source = endpoint(link, 'source');
      const target = endpoint(link, 'target');
      const sourceIndex = nodeIndex.get(source), targetIndex = nodeIndex.get(target);
      if (sourceIndex == null || targetIndex == null || sourceIndex === targetIndex) return;
      degrees[sourceIndex] += 1; degrees[targetIndex] += 1;
      edges.push({ source: sourceIndex, target: targetIndex, strength: finite(link && (link.strength || link.weight), 1), layer: key(link && link.layer || 'semantic'), bridge: link && link.bridge === true, ghost: link && link.ghost === true, ordinal });
    });
    const order = edges.map((_value, index) => index).sort((a, b) => edges[b].strength - edges[a].strength || edges[a].ordinal - edges[b].ordinal);
    const edgeRank = new Uint32Array(edges.length);
    order.forEach((edge, rank) => { edgeRank[edge] = rank; });
    const betweenness = new Float32Array(ids.length), evidenceMass = new Float32Array(ids.length);
    nodes.forEach((node, index) => {
      betweenness[index] = Math.max(0, finite(
        node && (node.betweenness || node.bridge_score || node.pagerank), 0));
      evidenceMass[index] = Math.max(0, finite(
        node && (node.evidence_mass || node.evidenceMass || node.gravity_mass
          || node.mass_score || node.mass), degrees[index] || 0));
    });
    state.ids = ids; state.labels = labels; state.types = types; state.degrees = degrees; state.betweenness = betweenness; state.evidenceMass = evidenceMass; state.nodeGhosts = nodeGhosts; state.communities = communities;
    state.edgeSources = new Uint32Array(edges.map(edge => edge.source)); state.edgeTargets = new Uint32Array(edges.map(edge => edge.target));
    state.edgeStrength = new Float32Array(edges.map(edge => edge.strength)); state.edgeLayers = edges.map(edge => edge.layer); state.edgeBridges = new Uint8Array(edges.map(edge => edge.bridge ? 1 : 0)); state.edgeGhosts = new Uint8Array(edges.map(edge => edge.ghost ? 1 : 0)); state.edgeOrder = new Uint32Array(order); state.edgeRank = edgeRank;
    applyLayout(false);
    const incidence = new Uint32Array(ids.length);
    edges.forEach(edge => { incidence[edge.source] += 1; incidence[edge.target] += 1; });
    const adjacencyOffsets = new Uint32Array(ids.length + 1);
    for (let index = 0; index < ids.length; index += 1) adjacencyOffsets[index + 1] = adjacencyOffsets[index] + incidence[index];
    const adjacencyEdges = new Uint32Array(adjacencyOffsets[ids.length]), cursor = adjacencyOffsets.slice(0, -1);
    edges.forEach((_edge, edgeIndex) => { const source = state.edgeSources[edgeIndex], target = state.edgeTargets[edgeIndex]; adjacencyEdges[cursor[source]++] = edgeIndex; adjacencyEdges[cursor[target]++] = edgeIndex; });
    for (let index = 0; index < ids.length; index += 1) {
      const start = adjacencyOffsets[index], end = adjacencyOffsets[index + 1], segment = Array.from(adjacencyEdges.slice(start, end));
      segment.sort((a, b) => edgeRank[a] - edgeRank[b]);
      adjacencyEdges.set(segment, start);
    }
    state.adjacencyOffsets = adjacencyOffsets; state.adjacencyEdges = adjacencyEdges; state.edgeSeen = new Uint32Array(edges.length); state.edgeStamp = 0;
    state.nodeSeen = new Uint32Array(ids.length); state.nodeStamp = 0;
    state.topNodes = new Uint32Array(Array.from({ length: ids.length }, (_v, index) => index).sort((a, b) => degrees[b] - degrees[a] || a - b));
    state.allNodes = new Uint32Array(ids.length); for (let index = 0; index < ids.length; index += 1) state.allNodes[index] = index;
    rebuildPaintOrder();
    rebuildGrid();
    state.lastCameraKey = '';
    const positionsOut = state.positions.slice(), degreesOut = degrees.slice(), betweennessOut = betweenness.slice(), evidenceMassOut = evidenceMass.slice(), nodeGhostsOut = nodeGhosts.slice(), edgeSourcesOut = state.edgeSources.slice(), edgeTargetsOut = state.edgeTargets.slice(), edgeStrengthOut = state.edgeStrength.slice(), edgeBridgesOut = state.edgeBridges.slice(), topNodesOut = state.topNodes.slice();
    self.postMessage({ type: 'ready', ids, labels, types, positions: positionsOut, degrees: degreesOut, betweenness: betweennessOut, evidenceMass: evidenceMassOut, nodeGhosts: nodeGhostsOut, communities, bounds: makeBounds(state.positions), edgeSources: edgeSourcesOut, edgeTargets: edgeTargetsOut, edgeStrength: edgeStrengthOut, edgeBridges: edgeBridgesOut, edgeLayers: state.edgeLayers, topNodes: topNodesOut, totalNodes: ids.length, totalLinks: edges.length }, [positionsOut.buffer, degreesOut.buffer, betweennessOut.buffer, evidenceMassOut.buffer, nodeGhostsOut.buffer, edgeSourcesOut.buffer, edgeTargetsOut.buffer, edgeStrengthOut.buffer, edgeBridgesOut.buffer, topNodesOut.buffer]);
  }
  function inViewport(index, camera, padding = 1) {
    const scale = Math.max(0.01, finite(camera && camera.scale, 1)), width = Math.max(1, finite(camera && camera.width, 1)), height = Math.max(1, finite(camera && camera.height, 1));
    const halfWidth = width / scale / 2 * padding, halfHeight = height / scale / 2 * padding, x = state.positions[index * 2], y = state.positions[index * 2 + 1];
    return x >= finite(camera && camera.x, 0) - halfWidth && x <= finite(camera && camera.x, 0) + halfWidth && y >= finite(camera && camera.y, 0) - halfHeight && y <= finite(camera && camera.y, 0) + halfHeight;
  }
  function focusMask() {
    if (state.focusIndex < 0 || state.focusIndex >= state.ids.length) return null;
    const mask = new Uint8Array(state.ids.length), depth = clamp(Math.round(finite(state.scope.depth, 2)), 1, 4);
    mask[state.focusIndex] = 1;
    let frontier = [state.focusIndex];
    for (let hop = 0; hop < depth && frontier.length; hop += 1) {
      const next = [];
      for (let cursor = 0; cursor < frontier.length; cursor += 1) {
        const node = frontier[cursor], start = state.adjacencyOffsets[node] || 0;
        const end = state.adjacencyOffsets[node + 1] || 0;
        for (let edgeCursor = start; edgeCursor < end; edgeCursor += 1) {
          const edge = state.adjacencyEdges[edgeCursor];
          const neighbour = state.edgeSources[edge] === node ? state.edgeTargets[edge] : state.edgeSources[edge];
          if (!mask[neighbour]) { mask[neighbour] = 1; next.push(neighbour); }
        }
      }
      frontier = next;
    }
    return mask;
  }
  function nodeAllowed(index, focused) {
    if (index < 0 || index >= state.ids.length) return false;
    if (!state.showGhosts && state.nodeGhosts[index]) return false;
    if (focused && !focused[index]) return false;
    const degree = state.degrees[index] || 0;
    return (degree > 0 && degree >= state.scope.minDegree)
      || (degree === 0 && state.scope.showUnlinked);
  }
  function setCollapsed(value) {
    const next = value === true;
    if (next === state.collapsed) return;
    state.collapsed = next;
    self.postMessage({ type: 'collapse', value: next });
  }
  function collapseRepresentatives(values) {
    const representatives = new Map();
    for (let cursor = 0; cursor < values.length; cursor += 1) {
      const index = values[cursor];
      const community = state.communities[index] || `__node:${index}`;
      const previous = representatives.get(community);
      if (previous == null || state.degrees[index] > state.degrees[previous]
        || (state.degrees[index] === state.degrees[previous] && index < previous)) {
        representatives.set(community, index);
      }
    }
    return new Uint32Array([...representatives.values()].sort((a, b) => a - b));
  }
  function resolveLodTier(scale) {
    const current = state.lodTier;
    if (current === 'far') {
      if (scale < FAR_EXIT) return 'far';
      return scale >= MEDIUM_EXIT ? 'near' : 'medium';
    }
    if (current === 'near') {
      if (scale >= MEDIUM_ENTER) return 'near';
      return scale < FAR_ENTER ? 'far' : 'medium';
    }
    if (scale < FAR_ENTER) return 'far';
    if (scale >= MEDIUM_EXIT) return 'near';
    return 'medium';
  }
  function boundedNodes(values, limit) {
    if (values.length <= limit) return values;
    state.nodeStamp = (state.nodeStamp + 1) >>> 0;
    if (!state.nodeStamp) { state.nodeSeen.fill(0); state.nodeStamp = 1; }
    for (let index = 0; index < values.length; index += 1) {
      state.nodeSeen[values[index]] = state.nodeStamp;
    }
    const retained = [];
    const focused = state.focusIndex;
    if (focused >= 0 && state.nodeSeen[focused] === state.nodeStamp) retained.push(focused);
    for (let index = 0; index < state.topNodes.length && retained.length < limit; index += 1) {
      const node = state.topNodes[index];
      if (node !== focused && state.nodeSeen[node] === state.nodeStamp) retained.push(node);
    }
    retained.sort((left, right) => left - right);
    return new Uint32Array(retained);
  }
  function visibleNodes(camera) {
    const scale = Math.max(0.01, finite(camera && camera.scale, 1));
    const focused = focusMask();
    const allVisibleNodes = () => {
      const result = [];
      for (let index = 0; index < state.ids.length; index += 1) {
        if (nodeAllowed(index, focused)) result.push(index);
      }
      return new Uint32Array(result);
    };
    const shouldCollapse = state.focusIndex < 0 && (
      state.collapseMode === true
      || (state.collapseMode === 'auto' && state.lodTier === 'far')
    );
    if (state.lodTier === 'far') {
      const values = allVisibleNodes();
      setCollapsed(shouldCollapse);
      const representatives = shouldCollapse ? collapseRepresentatives(values) : values;
      return boundedNodes(representatives, FAR_NODE_BUDGET);
    }
    const width = Math.max(1, finite(camera && camera.width, 1));
    const height = Math.max(1, finite(camera && camera.height, 1));
    const halfWidth = width / scale / 2 * 1.05, halfHeight = height / scale / 2 * 1.05;
    const minCellX = Math.floor((finite(camera && camera.x, 0) - halfWidth) / CELL_SIZE);
    const maxCellX = Math.floor((finite(camera && camera.x, 0) + halfWidth) / CELL_SIZE);
    const minCellY = Math.floor((finite(camera && camera.y, 0) - halfHeight) / CELL_SIZE);
    const maxCellY = Math.floor((finite(camera && camera.y, 0) + halfHeight) / CELL_SIZE);
    let values;
    if (maxCellX - minCellX > 256 || maxCellY - minCellY > 256) {
      values = allVisibleNodes();
    } else {
      const result = [];
      for (let cellY = minCellY; cellY <= maxCellY; cellY += 1) {
        for (let cellX = minCellX; cellX <= maxCellX; cellX += 1) {
          const bucket = state.grid.get(`${cellX},${cellY}`);
          if (bucket) bucket.forEach(index => {
            if (nodeAllowed(index, focused)) result.push(index);
          });
        }
      }
      values = new Uint32Array(result);
    }
    setCollapsed(shouldCollapse);
    if (shouldCollapse) values = collapseRepresentatives(values);
    return state.lodTier === 'medium'
      ? boundedNodes(values, MEDIUM_NODE_BUDGET) : values;
  }
  function visibleEdges(camera, nodes) {
    const tier = state.lodTier;
    const limit = tier === 'far' ? LOW_ZOOM_EDGE_LIMIT
      : tier === 'medium'
        ? (state.canvasFallback ? CANVAS_MEDIUM_ZOOM_EDGE_LIMIT : MEDIUM_ZOOM_EDGE_LIMIT)
        : (state.canvasFallback ? CANVAS_HIGH_ZOOM_EDGE_LIMIT : HIGH_ZOOM_EDGE_LIMIT);
    if (!limit) return new Uint32Array(0);
    const visible = new Uint8Array(state.ids.length);
    for (let index = 0; index < nodes.length; index += 1) visible[nodes[index]] = 1;
    if (state.focusIndex < 0 && nodes.length > state.ids.length * 0.55) {
      const result = [];
      for (let index = 0; index < state.paintOrder.length && result.length < limit; index += 1) {
        const edge = state.paintOrder[index];
        if (visible[state.edgeSources[edge]] && visible[state.edgeTargets[edge]]) result.push(edge);
      }
      return new Uint32Array(result);
    }
    const selected = state.focusIndex, candidates = [], result = [];
    state.edgeStamp = (state.edgeStamp + 1) >>> 0; if (!state.edgeStamp) { state.edgeSeen.fill(0); state.edgeStamp = 1; }
    const addNode = index => { const start = state.adjacencyOffsets[index] || 0, end = state.adjacencyOffsets[index + 1] || 0; for (let cursor = start; cursor < end; cursor += 1) { const edge = state.adjacencyEdges[cursor]; if (state.edgeSeen[edge] !== state.edgeStamp) { state.edgeSeen[edge] = state.edgeStamp; candidates.push(edge); } } };
    if (selected >= 0) addNode(selected);
    for (let index = 0; index < nodes.length; index += 1) addNode(nodes[index]);
    candidates.sort((a, b) => state.edgeRank[a] - state.edgeRank[b]);
    for (let index = 0; index < candidates.length && result.length < limit; index += 1) {
      const edge = candidates[index];
      if (!edgeAllowed(edge)) continue;
      if (!visible[state.edgeSources[edge]] || !visible[state.edgeTargets[edge]]) continue;
      result.push(edge);
    }
    return new Uint32Array(result);
  }
  function visibleLabels(camera, nodes) {
    if (state.lodTier === 'far') return new Uint32Array(0);
    const density = Math.max(0.25, Math.min(3, finite(state.labelDensity, 24) / 24));
    const limit = Math.min(LABEL_LIMIT, Math.max(12, Math.floor(80 * finite(camera && camera.scale, 1) * density))), result = [], visible = new Uint8Array(state.ids.length);
    for (let index = 0; index < nodes.length; index += 1) visible[nodes[index]] = 1;
    for (let index = 0; index < state.topNodes.length && result.length < limit; index += 1) if (visible[state.topNodes[index]]) result.push(state.topNodes[index]);
    return new Uint32Array(result);
  }
  function camera(message) {
    const scale = Math.max(0.01, finite(message && message.scale, 1));
    state.lodTier = resolveLodTier(scale);
    const nextKey = cameraKey(message);
    if (nextKey === state.lastCameraKey) {
      self.postMessage({ type: 'camera-ack', revision: message && message.revision });
      return;
    }
    state.lastCameraKey = nextKey;
    const nodes = visibleNodes(message), edges = visibleEdges(message, nodes), labels = visibleLabels(message, nodes);
    const visibleMask = new Uint8Array(state.ids.length);
    for (let index = 0; index < nodes.length; index += 1) visibleMask[nodes[index]] = 1;
    const edgePositions = new Float32Array(edges.length * 4);
    for (let index = 0; index < edges.length; index += 1) { const edge = edges[index], source = state.edgeSources[edge], target = state.edgeTargets[edge], offset = index * 4; edgePositions[offset] = state.positions[source * 2]; edgePositions[offset + 1] = state.positions[source * 2 + 1]; edgePositions[offset + 2] = state.positions[target * 2]; edgePositions[offset + 3] = state.positions[target * 2 + 1]; }
    state.lastVisibleNodes = nodes; state.lastVisibleEdges = edges; state.lastVisibleLabels = labels;
    state.lastVisibleMask = visibleMask;
    self.postMessage({ type: 'visible', revision: message && message.revision,
      nodes, edges, labels, edgePositions, totalLinks: state.edgeSources.length,
      drawnLinks: edges.length, visibleNodeCount: nodes.length,
      collapsed: state.collapsed, lodTier: state.lodTier },
    [nodes.buffer, edges.buffer, labels.buffer, edgePositions.buffer]);
  }
  function hit(message) {
    const x = finite(message && message.x, 0), y = finite(message && message.y, 0), cellX = Math.floor(x / CELL_SIZE), cellY = Math.floor(y / CELL_SIZE), maxDistance = Math.max(8, 12 / Math.max(0.01, finite(message && message.scale, 1))), maxSquared = maxDistance * maxDistance;
    let best = -1, distance = maxSquared;
    const cellRadius = Math.max(1, Math.ceil(maxDistance / CELL_SIZE));
    for (let dx = -cellRadius; dx <= cellRadius; dx += 1) for (let dy = -cellRadius; dy <= cellRadius; dy += 1) (state.grid.get(`${cellX + dx},${cellY + dy}`) || []).forEach(index => {
      const deltaX = state.positions[index * 2] - x, deltaY = state.positions[index * 2 + 1] - y, next = deltaX * deltaX + deltaY * deltaY;
      if ((!state.showGhosts && state.nodeGhosts[index])
        || (state.lastVisibleMask.length && !state.lastVisibleMask[index])) return;
      if (next < distance) { best = index; distance = next; }
    });
    self.postMessage({ type: 'hit', request: message && message.request, index: best });
  }
  self.onmessage = event => {
    const message = event.data || {};
    if (message.type === 'prepare') prepare(message.payload || {});
    else if (message.type === 'camera') camera(message);
    else if (message.type === 'hit') hit(message);
    else if (message.type === 'focus') {
      state.focusIndex = Number.isInteger(message.index) ? message.index : -1;
      state.lastCameraKey = '';
    } else if (message.type === 'layers') {
      state.layers = message.layers || null; rebuildPaintOrder(); state.lastCameraKey = '';
    } else if (message.type === 'renderer') {
      state.canvasFallback = message.canvasFallback === true; state.lastCameraKey = '';
    } else if (message.type === 'settings') {
      state.layoutSettings = message.settings && typeof message.settings === 'object'
        ? { ...state.layoutSettings, ...message.settings } : state.layoutSettings;
      state.labelDensity = finite(state.layoutSettings.labelDensity, state.labelDensity);
      if (message.relayout === true) applyLayout(true, message.fit === true);
      state.lastCameraKey = '';
    } else if (message.type === 'scope') {
      const scope = message.scope && typeof message.scope === 'object' ? message.scope : {};
      state.scope = {
        minDegree: clamp(Math.round(finite(scope.minDegree, state.scope.minDegree)), 0, 12),
        showUnlinked: scope.showUnlinked !== false,
        depth: clamp(Math.round(finite(scope.depth, state.scope.depth)), 1, 4),
      };
      state.lastCameraKey = '';
    } else if (message.type === 'collapse') {
      state.collapseMode = message.value === true ? true : message.value === 'auto' ? 'auto' : false;
      if (!state.collapseMode) setCollapsed(false);
      state.lastCameraKey = '';
    } else if (message.type === 'reheat') {
      state.layoutRevision += 1;
      applyLayout(true, false);
      state.lastCameraKey = '';
    } else if (message.type === 'bridges') {
      state.showBridges = message.value !== false; state.lastCameraKey = '';
    } else if (message.type === 'ghosts') {
      state.showGhosts = message.value !== false; rebuildPaintOrder(); state.lastCameraKey = '';
    }
  };
})();
