/* Deterministic layout worker for the Every-node engine. It owns only what must leave the
   main thread: capacity validation, typed-array compaction, deterministic community-seeded
   placement, and bounded streamed relaxation. Nothing here is per-frame: camera moves,
   picking, and LOD shading decisions belong to the renderer and its shaders, so this worker
   goes silent the moment a layout settles. */
(function () {
  'use strict';

  const MAX_NODES = 20000;
  const MAX_LINKS = 200000;
  const REFINE_PASSES = 26;
  const BROADCAST_EVERY = 4;
  /* The scene reads as a map, not a cluster: every spacing constant is multiplied out so
     communities breathe and single relations stretch into visible journeys. */
  const SPACING = 13;
  const MAP_SCALE = 3;
  const BRIDGE_LIMIT = 512;

  let model = null;
  let settings = { repel: 48, link: 16, gravity: 48 };
  let generation = 0;

  function post(message) { self.postMessage(message); }

  /* Endpoint ids may be any JSON value including falsy ones such as 0 or false; string keys
     keep every id addressable while preserving insertion order of first sight. */
  function stableKey(value) {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    if (value && typeof value === 'object' && 'id' in value) return stableKey(value.id);
    return '';
  }

  function hash32(text) {
    let h = 2166136261 >>> 0;
    for (let index = 0; index < text.length; index += 1) {
      h ^= text.charCodeAt(index);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  function mulberry32(seed) {
    let state = seed >>> 0;
    return function () {
      state = (state + 0x6D2B79F5) | 0;
      let t = Math.imul(state ^ (state >>> 15), 1 | state);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function buildModel(payload) {
    const nodes = Array.isArray(payload && payload.nodes) ? payload.nodes : [];
    const rawLinks = Array.isArray(payload && payload.links) ? payload.links
      : Array.isArray(payload && payload.edges) ? payload.edges : [];
    if (nodes.length > MAX_NODES) { post({ type: 'capacity', resource: 'nodes', count: nodes.length, limit: MAX_NODES }); return null; }
    if (rawLinks.length > MAX_LINKS) { post({ type: 'capacity', resource: 'relations', count: rawLinks.length, limit: MAX_LINKS }); return null; }

    const count = nodes.length;
    const ids = new Array(count);
    const indexById = new Map();
    for (let index = 0; index < count; index += 1) {
      const raw = nodes[index];
      const id = stableKey(raw && raw.id !== undefined ? raw.id : index);
      ids[index] = id;
      if (!indexById.has(id)) indexById.set(id, index);
    }

    const labels = new Array(count);
    const types = new Array(count).fill('');
    const ghostFlags = new Uint8Array(count);
    const communities = new Array(count);
    const evidenceMass = new Float32Array(count);
    const communityIndex = new Map();
    for (let index = 0; index < count; index += 1) {
      const node = nodes[index] || {};
      labels[index] = String(node.name || node.label || ids[index]);
      types[index] = String(node.type || '');
      ghostFlags[index] = node.ghost ? 1 : 0;
      /* Untagged nodes share one implicit district so centroid separation stays
         O(distinct community tags), rather than becoming an O(n^2) node pair loop. */
      const group = node.community_id !== undefined && node.community_id !== null
        ? String(node.community_id) : null;
      if (!communityIndex.has(group)) communityIndex.set(group, communityIndex.size);
      communities[index] = communityIndex.get(group);
      const mass = Number(node.evidence_mass);
      evidenceMass[index] = Number.isFinite(mass) && mass > 0 ? mass : 0;
    }

    const degreeCounts = new Uint32Array(count);
    const sources = [];
    const targets = [];
    const weights = [];
    const relations = [];
    for (let index = 0; index < rawLinks.length; index += 1) {
      const link = rawLinks[index] || {};
      const source = indexById.get(stableKey(link.source));
      const target = indexById.get(stableKey(link.target));
      if (source === undefined || target === undefined) continue;
      sources.push(source);
      targets.push(target);
      const weight = Number(link.weight);
      weights.push(Number.isFinite(weight) && weight > 0 ? weight : 1);
      relations.push(String(link.relation || link.label || ""));
      degreeCounts[source] += 1;
      degreeCounts[target] += 1;
    }

    /* Bridges keep distant clusters visually connected: the strongest cross-community
       relations win a bounded budget so far-out zoom still reads one connected scene. */
    const linkCount = sources.length;
    const edgeBridges = new Uint8Array(linkCount);
    const candidates = [];
    for (let index = 0; index < linkCount; index += 1) {
      if (communities[sources[index]] !== communities[targets[index]]) candidates.push([index, weights[index]]);
    }
    candidates.sort((a, b) => b[1] - a[1]);
    const bridgeBudget = Math.min(candidates.length, BRIDGE_LIMIT);
    for (let index = 0; index < bridgeBudget; index += 1) edgeBridges[candidates[index][0]] = 1;

    const degrees = new Float32Array(count);
    degrees.set(degreeCounts);

    const topNodes = new Uint32Array(count);
    for (let index = 0; index < count; index += 1) topNodes[index] = index;
    topNodes.sort((a, b) => degrees[b] - degrees[a]);

    let maxDegree = 0;
    for (let index = 0; index < count; index += 1) maxDegree = Math.max(maxDegree, degrees[index]);
    const betweenness = new Float32Array(count);
    if (maxDegree > 0) {
      const scale = Math.log1p(maxDegree);
      for (let index = 0; index < count; index += 1) betweenness[index] = Math.log1p(degrees[index]) / scale;
    }

    return {
      count, ids, labels, types, ghostFlags, communities, evidenceMass,
      degrees, betweenness, topNodes,
      sources: Uint32Array.from(sources),
      targets: Uint32Array.from(targets),
      weights: Float32Array.from(weights),
      relations,
      edgeBridges,
      totalLinks: linkCount,
      positions: null, bounds: null, dx: null, dy: null,
    };
  }

  function seedPositions() {
    const positions = new Float32Array(model.count * 2);
    const sizes = new Map();
    for (let index = 0; index < model.count; index += 1) {
      const group = model.communities[index];
      sizes.set(group, (sizes.get(group) || 0) + 1);
    }
    const order = Array.from(sizes.keys()).sort((a, b) => sizes.get(b) - sizes.get(a));
    const centreSlot = new Map();
    order.forEach((group, slot) => centreSlot.set(group, slot));

    /* Large communities claim the middle of a sunflower spiral; members fan out on their own
       golden-angle disc so even the seed layout is readable before relaxation touches it. */
    /* Districts read tighter than their spacing: members pack into compact discs while
       centres ride a much wider spiral, so communities look like distinct regions. */
    const scaledSpacing = SPACING * MAP_SCALE;
    const spread = scaledSpacing * 6.5 * Math.sqrt(Math.max(1, model.count));
    const goldenAngle = 2.399963229728653;
    const random = mulberry32(0x9E3779B9 ^ Math.imul(model.count + 1, 2654435761));
    const memberCursor = new Map();

    for (let index = 0; index < model.count; index += 1) {
      const group = model.communities[index];
      const slot = centreSlot.get(group);
      const angle = slot * goldenAngle;
      const radius = slot === 0 ? 0 : spread * Math.sqrt(slot / order.length);
      const cursor = memberCursor.get(group) || 0;
      memberCursor.set(group, cursor + 1);
      const localAngle = cursor * goldenAngle + random() * 0.4;
      const localRadius = scaledSpacing * 0.85 * Math.sqrt(cursor + 0.3);
      positions[index * 2] = Math.cos(angle) * radius + Math.cos(localAngle) * localRadius;
      positions[index * 2 + 1] = Math.sin(angle) * radius + Math.sin(localAngle) * localRadius;
    }
    model.positions = positions;
  }

  function computeBounds() {
    const positions = model.positions, count = model.count;
    if (!count) { model.bounds = { minX: -60, maxX: 60, minY: -60, maxY: 60 }; return; }
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (let index = 0; index < count; index += 1) {
      const x = positions[index * 2], y = positions[index * 2 + 1];
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
    }
    model.bounds = { minX, maxX, minY, maxY };
  }

  function relaxPass() {
    const count = model.count;
    if (!count) return;
    const pos = model.positions, dx = model.dx, dy = model.dy;
    dx.fill(0); dy.fill(0);

    /* Springs pull linked pairs toward a rest length scaled by the link-distance control.
       Intra-community springs run strong so districts hold their shape; cross-community
       springs run weak — they are visual routes between districts, not licence to drag
       the districts into one another over the settle passes. */
    const scaledSpacing = SPACING * MAP_SCALE;
    const rest = Math.max(scaledSpacing * 1.9, Number(settings.link) * 1.6 * (MAP_SCALE * 0.55));
    for (let edge = 0; edge < model.totalLinks; edge += 1) {
      const a = model.sources[edge], b = model.targets[edge];
      const ddx = pos[b * 2] - pos[a * 2], ddy = pos[b * 2 + 1] - pos[a * 2 + 1];
      const dist = Math.sqrt(ddx * ddx + ddy * ddy) || 0.0001;
      const crossCommunity =
        model.communities[a] !== model.communities[b] ? 0.02 : 0.07;
      const force = (dist - rest) / dist * crossCommunity;
      dx[a] -= ddx * force; dy[a] -= ddy * force;
      dx[b] += ddx * force; dy[b] += ddy * force;
    }

    /* Local repulsion through a spatial hash with a per-node visit cap keeps each pass O(n)
       regardless of density; global repulsion is neither affordable nor readable at scale. */
    const cell = SPACING * MAP_SCALE * 2.2;
    const grid = new Map();
    for (let index = 0; index < count; index += 1) {
      const key = (Math.floor(pos[index * 2] / cell) + 32768) * 65536
        + (Math.floor(pos[index * 2 + 1] / cell) + 32768);
      const bucket = grid.get(key);
      if (bucket) bucket.push(index); else grid.set(key, [index]);
    }
    const minDist = SPACING * MAP_SCALE * 1.55;
    const minDist2 = minDist * minDist;
    const push = Number(settings.repel) / 48;
    for (let index = 0; index < count; index += 1) {
      const gx = Math.floor(pos[index * 2] / cell), gy = Math.floor(pos[index * 2 + 1] / cell);
      let checked = 0;
      for (let ox = -1; ox <= 1 && checked < 14; ox += 1) {
        for (let oy = -1; oy <= 1 && checked < 14; oy += 1) {
          const bucket = grid.get((gx + ox + 32768) * 65536 + (gy + oy + 32768));
          if (!bucket) continue;
          for (let slot = 0; slot < bucket.length && checked < 14; slot += 1) {
            const other = bucket[slot];
            if (other === index) continue;
            checked += 1;
            const ddx = pos[index * 2] - pos[other * 2];
            const ddy = pos[index * 2 + 1] - pos[other * 2 + 1];
            const d2 = ddx * ddx + ddy * ddy;
            if (d2 > minDist2 || d2 === 0) continue;
            const dist = Math.sqrt(d2) || 0.001;
            const f = (minDist - dist) / dist * 0.10 * push;
            dx[index] += ddx * f; dy[index] += ddy * f;
          }
        }
      }
    }

    let cx = 0, cy = 0;
    for (let index = 0; index < count; index += 1) { cx += pos[index * 2]; cy += pos[index * 2 + 1]; }
    cx /= count; cy /= count;

    /* District-level separation: repelling community CENTROIDS moves whole neighbourhoods
       apart as units — node-level repulsion alone cannot, its range is far too short.
       Centroids and member lists are recomputed per pass from live positions. */
    const stats = { list: [] };
    {
      const map = new Map();
      for (let index = 0; index < count; index += 1) {
        const group = model.communities[index];
        const entry = map.get(group);
        if (entry) { entry.x += pos[index * 2]; entry.y += pos[index * 2 + 1]; entry.n += 1; }
        else map.set(group, { x: pos[index * 2], y: pos[index * 2 + 1], n: 1 });
      }
      const slots = new Map();
      let slot = 0;
      for (const [group, entry] of map) {
        slots.set(group, slot);
        stats.list.push({
          x: entry.x / entry.n, y: entry.y / entry.n,
          r: scaledSpacing * 0.95 * Math.sqrt(entry.n), members: [],
        });
        slot += 1;
      }
      for (let index = 0; index < count; index += 1) {
        stats.list[slots.get(model.communities[index])].members.push(index);
      }
    }
    const separation = scaledSpacing * 2.6;
    const pushStrength = 0.05;
    for (let a = 0; a < stats.list.length; a += 1) {
      for (let b = a + 1; b < stats.list.length; b += 1) {
        const A = stats.list[a], B = stats.list[b];
        const ddx = B.x - A.x, ddy = B.y - A.y;
        const dist = Math.sqrt(ddx * ddx + ddy * ddy) || 0.001;
        const desired = separation + A.r + B.r;
        if (dist >= desired) continue;
        const f = (desired - dist) / dist * pushStrength;
        const fx = ddx * f / A.members.length, fy = ddy * f / A.members.length;
        const gx = ddx * f / B.members.length, gy = ddy * f / B.members.length;
        for (const index of A.members) { dx[index] -= fx; dy[index] -= fy; }
        for (const index of B.members) { dx[index] += gx; dy[index] += gy; }
      }
    }

    const gravity = Number(settings.gravity) / 48 * 0.0015;
    for (let index = 0; index < count; index += 1) {
      dx[index] += (cx - pos[index * 2]) * gravity;
      dy[index] += (cy - pos[index * 2 + 1]) * gravity;
    }

    /* A tight per-pass step cap keeps the settle from smearing district boundaries. */
    const damp = 0.8, maxStep = SPACING * MAP_SCALE * 0.7;
    for (let index = 0; index < count; index += 1) {
      let vx = dx[index] * damp, vy = dy[index] * damp;
      const speed = Math.sqrt(vx * vx + vy * vy);
      if (speed > maxStep) { vx = vx / speed * maxStep; vy = vy / speed * maxStep; }
      pos[index * 2] += vx;
      pos[index * 2 + 1] += vy;
    }
  }

  function refine(gen, fitFinal) {
    let pass = 0;
    const step = () => {
      if (gen !== generation) return;
      pass += 1;
      relaxPass();
      computeBounds();
      post({ type: 'progress', pass, total: REFINE_PASSES });
      if (pass % BROADCAST_EVERY === 0 || pass === REFINE_PASSES) {
        post({
          type: 'layout',
          positions: model.positions.slice(),
          bounds: { ...model.bounds },
          fit: pass === REFINE_PASSES ? fitFinal === true : false,
        });
      }
      if (pass < REFINE_PASSES) setTimeout(step, 0);
    };
    setTimeout(step, 0);
  }

  self.onmessage = (event) => {
    const data = event.data || {};
    if (data.type === 'prepare') {
      generation += 1;
      const gen = generation;
      model = buildModel(data.payload || {});
      if (!model) return;
      seedPositions();
      model.dx = new Float32Array(model.count);
      model.dy = new Float32Array(model.count);
      computeBounds();
      post({
        type: 'preview',
        ids: model.ids.slice(),
        labels: model.labels.slice(),
        types: model.types.slice(),
        positions: model.positions.slice(),
        bounds: { ...model.bounds },
        nodeGhosts: model.ghostFlags.slice(),
        communities: model.communities.slice(),
      });
      post({
        type: 'ready',
        ids: model.ids,
        labels: model.labels,
        types: model.types,
        positions: model.positions,
        bounds: { ...model.bounds },
        nodeGhosts: model.ghostFlags,
        communities: model.communities,
        degrees: model.degrees,
        betweenness: model.betweenness,
        evidenceMass: model.evidenceMass,
        edgeSources: model.sources,
        edgeTargets: model.targets,
        edgeBridges: model.edgeBridges,
        edgeWeights: model.weights,
        edgeRelations: model.relations,
        edgeLayers: [],
        topNodes: model.topNodes,
        totalLinks: model.totalLinks,
      });
      refine(gen, true);
      return;
    }
    if (data.type === 'settings') {
      const next = data.settings || {};
      settings = {
        repel: Number.isFinite(Number(next.repel)) ? Number(next.repel) : settings.repel,
        link: Number.isFinite(Number(next.link)) ? Number(next.link) : settings.link,
        gravity: Number.isFinite(Number(next.gravity)) ? Number(next.gravity) : settings.gravity,
      };
      if (data.relayout && model) {
        generation += 1;
        const gen = generation;
        seedPositions();
        computeBounds();
        post({ type: 'layout', positions: model.positions.slice(), bounds: { ...model.bounds }, fit: false });
        refine(gen, data.fit === true);
      }
      return;
    }
    if (data.type === 'reheat') {
      if (!model) return;
      generation += 1;
      refine(generation, false);
    }
  };
})();
