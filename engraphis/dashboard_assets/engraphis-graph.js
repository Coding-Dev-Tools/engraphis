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
  /* Community colour is the *palette slot*, not the node: `nodeColor` indexes this by the
     community id, and communities are numbered by size (largest == 0). The legend beside the
     canvas paints its swatches from `.graph-cluster-N` in dashboard.css, which encodes the
     Cyber palette — the default style — slot for slot. These arrays must therefore stay
     byte-identical to `COMMUNITY_PALS` in dashboard.js, or "Cluster 1" gets one colour in the
     legend and another on the canvas. Ordering is load-bearing; this is not free-choice art. */
  const COMMUNITY_PALS = {
    classic: ['#8c83e8', '#5aafb3', '#d7a84b', '#6f9fd8', '#58b882', '#df7478', '#b07de0', '#4fb0a0', '#e0894a', '#7c9be0', '#e06a9a', '#9ac25a'],
    galaxy: ['#b789ff', '#7bb4ff', '#66e0d0', '#ffcf6b', '#ff7ea8', '#8aa2ff', '#c98bff', '#5ad0e0', '#ffa0d0', '#9d7bff', '#6ad0b0', '#ffb060'],
    solar: ['#ffb454', '#5b9bff', '#3fd2c7', '#ffd68a', '#ff8f6b', '#8ea8ff', '#ffc24a', '#6ac0d0', '#ff9f7a', '#7ab0ff', '#e0b050', '#5fd0b0'],
    cyber: ['#22e0ff', '#ff3ea5', '#b6ff3c', '#ffe14d', '#8b7bff', '#ff5c7a', '#3affd0', '#ff7be0', '#7affea', '#c0ff4a', '#5c9bff', '#ff9b3c']
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

  /* "Show all nodes" may return twenty thousand entities. A D3 simulation for even a
     few thousand of them monopolises the main thread long enough to make the Ledger feel
     hung, irrespective of its eventual tick/cooldown limit. Keep live centre gravity for
     overview-sized full graphs only; anything beyond the same large-graph cut-off as the
     classic renderer uses the centred deterministic layout below. That preserves every node,
     makes the gravity control compact/expand the layout, and leaves the UI responsive. */
  const FULL_FORCE_NODE_LIMIT = LARGE_NODE_LIMIT;
  const FULL_FORCE_LINK_LIMIT = LARGE_LINK_LIMIT;

  /* `zoomToFit()` derives its bounds from force-graph's default node geometry rather than
     our custom canvas radius. A compact, nearly-linear graph can therefore produce a 10×+
     fit zoom even though its rendered nodes already fill the canvas. At that scale a normal
     drag maps to a tiny world-space movement and reheating makes the rest of the layout look
     like it is racing away. Keep auto-fit useful without letting its scale become unstable. */
  const MAX_AUTO_FIT_ZOOM = 4;

  /* The classic renderer's *dense* signal (`GPERF.dense`, `links>1500` in dashboard.js). Past
     it the classic path turns off the two per-edge costs that scale with the link count and
     buy nothing at that density: link curvature (a quadratic bezier per relation instead of a
     straight line) and the directional arrowhead (a filled triangle per relation, recomputed
     every frame). Relation labels get the same treatment unless one node is highlighted. Same
     thresholds and same behaviour here — a second signal would only drift. */
  const DENSE_LINK_LIMIT = 1500;

  /* Relation labels are the noisiest layer on the canvas, so — exactly as the classic
     `linkCanvasObject` does — they only appear once the user has zoomed in past this scale. */
  const LINK_LABEL_MIN_SCALE = 2.4;

  function idOf(value) { return value && typeof value === 'object' ? value.id : value; }
  function nodeName(node) { return String(node.name || node.label || node.id || ''); }
  function showRelationLabel(label) {
    return Boolean(label) && String(label).toLowerCase() !== 'co_occurs';
  }
  /* Replace force-graph's round flow particles with a small directional glyph. The vendor
     callback supplies the particle's current position and its link; the context already has
     the resolved particle colour, so this only changes the silhouette and orientation. */
  function paintFlowArrow(x, y, link, ctx, globalScale) {
    const source = link && link.source;
    const target = link && link.target;
    if (!source || !target || !Number.isFinite(source.x) || !Number.isFinite(target.x)) return;
    const dx = target.x - source.x;
    const dy = target.y - source.y;
    if (!dx && !dy) return;
    const size = 1 / Math.sqrt(Math.max(0.01, Number(globalScale) || 1));
    const angle = Math.atan2(dy, dx);
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(angle);
    ctx.beginPath();
    ctx.moveTo(size * 0.55, 0);
    ctx.lineTo(-size * 0.45, size * 0.32);
    ctx.lineTo(-size * 0.45, -size * 0.32);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }
  /* Keep node geometry in the same compact world-space range as the Classic/Ledger renderer.
     The previous overview formula used the full size-slider value plus a normalized degree
     bonus, which made a seven-node workspace occupy only a small simulation area while each
     node still had a dense-graph radius. `zoomToFit()` then magnified those radii into large
     discs. Material style must not change geometry; it only changes the painted surface. */
  function graphNodeRadius(node, base, metric) {
    const size = Number.isFinite(+base) && +base > 0 ? +base : 3;
    if (node && node.cluster) {
      const members = Math.max(1, Number(node.members) || 1);
      const radius = size * 0.45 * (1.4 + Math.min(3, Math.sqrt(members) * 0.7));
      return Math.max(2, Math.min(size * 2.7, radius));
    }
    const normalized = Math.max(0, Math.min(1, Number(metric) || 0));
    const radius = size * 0.45 * (0.55 + Math.min(1.6, normalized * 1.9));
    return Math.max(0.8, Math.min(size * 1.1, radius));
  }
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
  function mixColours(a, b, amount) {
    const [ar, ag, ab] = hexRgb(a), [br, bg, bb] = hexRgb(b), t = Math.max(0, Math.min(1, amount));
    return 'rgb(' + Math.round(ar + (br - ar) * t) + ',' + Math.round(ag + (bg - ag) * t) + ',' + Math.round(ab + (bb - ab) * t) + ')';
  }
  function contrastOn(c) { const [r, g, b] = hexRgb(c); return (0.2126 * r + 0.7152 * g + 0.0722 * b) > 150 ? '#111827' : '#f8fafc'; }

  const MATERIAL_CACHE_CAPACITY = 192;
  const MATERIAL_CACHE = new Map();
  const MATERIAL_CACHE_METRICS = {
    hits: 0, misses: 0, allocations: 0, evictions: 0, clears: 0
  };
  /* Full sprites are intentionally oversampled. A 24px master blurred the grain back into
     the same soft radial blob when a hub was displayed at 35–55 screen pixels. */
  const MATERIAL_RADIUS = { signature: 5, bezel: 12, full: 40 };
  let materialCanvasFactory = null;
  let materialCacheDpr = null;

  function colourKey(c) { return hexRgb(c).join(','); }
  function rgbString(c) { const [r, g, b] = hexRgb(c); return 'rgb(' + r + ',' + g + ',' + b + ')'; }

  /* Screen-space detail is deliberately independent of the simulation's world-space radius.
     A distant hub and a nearby leaf therefore spend the same work for the same visible size. */
  function materialTier(screenRadius, forceLow) {
    if (forceLow || !Number.isFinite(+screenRadius) || +screenRadius < 6) return 'signature';
    return +screenRadius < 12 ? 'bezel' : 'full';
  }

  /* The preferred signature is (style, themeColors, paletteName, identity). The older
     (style, identity, themeColors) ordering remains accepted for test and compatibility seams. */
  function materialRecipe(styleName, themeOrIdentity, paletteOrTheme, maybeIdentity) {
    let themeColors, paletteName, identity;
    if (themeOrIdentity && typeof themeOrIdentity === 'object') {
      themeColors = themeOrIdentity;
      paletteName = typeof paletteOrTheme === 'string' ? paletteOrTheme : 'theme';
      identity = maybeIdentity || themeColors.accent || '#8c83e8';
    } else {
      identity = themeOrIdentity || '#8c83e8';
      themeColors = paletteOrTheme && typeof paletteOrTheme === 'object' ? paletteOrTheme : {};
      paletteName = 'theme';
    }
    const style = ['cyber', 'galaxy', 'solar', 'classic'].indexOf(styleName) < 0 ? 'classic' : styleName;
    const surface = themeColors.surface || themeColors.canvas || '#0e1014';
    const substrate = mixColours(surface, '#02050a', style === 'classic' ? 0.68 : 0.78);
    const base = {
      styleName: style, paletteName, substrate, identity: rgbString(identity),
      identityKey: colourKey(identity), substrateKey: colourKey(substrate)
    };
    if (style === 'cyber') {
      const fixedPalette = {
        cyan: '#21dff3', blue: '#367cff', violet: '#8d61ff',
        magenta: '#ec4fc4', teal: '#4ce4cf'
      };
      return Object.assign(base, {
        family: 'iridescent-pvd', fixedPalette, film: fixedPalette,
        outer: mixColours(substrate, '#01040a', 0.82),
        bezel: mixColours(substrate, '#101626', 0.46),
        face: mixColours(substrate, '#182237', 0.48),
        edge: '#677386', sheen: '#8d61ff'
      });
    }
    if (style === 'galaxy') {
      const fixedPalette = {
        navy: '#111a3b', blue: '#3979e8', violet: '#8d68df', highlight: '#aab9ee'
      };
      return Object.assign(base, {
        family: 'anodized-alloy', fixedPalette,
        outer: mixColours(substrate, '#02040d', 0.76),
        bezel: mixColours(substrate, '#151a34', 0.54),
        face: mixColours(substrate, fixedPalette.navy, 0.68),
        edge: '#7587bb', sheen: fixedPalette.blue
      });
    }
    if (style === 'solar') {
      const fixedPalette = {
        ember: '#713018', copper: '#b85c2f', amber: '#f18a32',
        gold: '#ffc46b', shadow: '#2b1008'
      };
      return Object.assign(base, {
        family: 'brushed-copper', fixedPalette,
        outer: mixColours(substrate, '#0a0402', 0.72),
        bezel: mixColours(substrate, '#351609', 0.62),
        face: mixColours(substrate, fixedPalette.copper, 0.48),
        edge: fixedPalette.amber, sheen: fixedPalette.gold
      });
    }
    const fixedPalette = {
      charcoal: '#242d36', steel: '#778593', highlight: '#c0c9cf', coolEdge: '#8aa7bd'
    };
    return Object.assign(base, {
      family: 'satin-gunmetal', fixedPalette,
      outer: mixColours(substrate, '#05080b', 0.68),
      bezel: mixColours(substrate, '#20272e', 0.52),
      face: mixColours(substrate, fixedPalette.charcoal, 0.72),
      edge: fixedPalette.coolEdge, sheen: fixedPalette.highlight
    });
  }

  function fillCircle(ctx, x, y, r, fill) {
    ctx.beginPath(); ctx.arc(x, y, Math.max(0.1, r), 0, 6.2832); ctx.fillStyle = fill; ctx.fill();
  }
  function strokeCircle(ctx, x, y, r, stroke, width) {
    ctx.beginPath(); ctx.arc(x, y, Math.max(0.1, r), 0, 6.2832);
    ctx.lineWidth = width; ctx.strokeStyle = stroke; ctx.stroke();
  }
  function gradient(ctx, kind, args, stops) {
    const maker = ctx[kind];
    if (typeof maker !== 'function') return stops[Math.floor(stops.length / 2)][1];
    const result = maker.apply(ctx, args);
    stops.forEach(stop => result.addColorStop(stop[0], stop[1]));
    return result;
  }
  function identityRing(ctx, x, y, r, recipe, strength) {
    strokeCircle(ctx, x, y, r * 0.955, alpha(recipe.identity, strength), Math.max(0.32, r * 0.045));
  }
  function materialHalo(ctx, x, y, r, tier, colour, opacity, shiftX, shiftY) {
    if (tier === 'signature') return;
    const reach = tier === 'full' ? 1.12 : 1.14;
    const halo = gradient(ctx, 'createRadialGradient', [
      x + r * (shiftX || 0), y + r * (shiftY || 0), r * 0.48,
      x, y, r * reach
    ], [
      [0, alpha(colour, opacity)], [0.68, alpha(colour, opacity * 0.42)],
      [1, alpha(colour, 0)]
    ]);
    fillCircle(ctx, x, y, r * reach, halo);
  }

  function directionalBrush(ctx, x, y, r, angle, dark, light, strength) {
    if (typeof ctx.moveTo !== 'function' || typeof ctx.lineTo !== 'function') return;
    const alongX = Math.cos(angle), alongY = Math.sin(angle);
    const normalX = -alongY, normalY = alongX;
    const bound = r * 0.76;
    for (let i = -13; i <= 13; i++) {
      const offset = i * r * 0.052;
      const span = Math.sqrt(Math.max(0, bound * bound - offset * offset));
      const cx = x + normalX * offset, cy = y + normalY * offset;
      ctx.lineWidth = Math.max(0.18, r * (0.007 + Math.abs(i % 3) * 0.002));
      ctx.strokeStyle = alpha(i % 4 === 0 ? dark : light,
        strength * (0.48 + Math.abs(i % 5) * 0.13));
      ctx.beginPath();
      ctx.moveTo(cx - alongX * span, cy - alongY * span);
      ctx.lineTo(cx + alongX * span, cy + alongY * span);
      ctx.stroke();
    }
  }

  function paintCyberMaterial(ctx, x, y, r, recipe, tier) {
    const f = recipe.fixedPalette;
    materialHalo(ctx, x, y, r, tier, f.cyan, 0.20, -0.15, 0.12);
    materialHalo(ctx, x, y, r, tier, f.magenta, 0.17, 0.16, -0.14);
    fillCircle(ctx, x, y, r, recipe.outer);
    fillCircle(ctx, x, y, r * 0.94, recipe.bezel);
    if (tier === 'signature') {
      fillCircle(ctx, x, y, r * 0.79, mixColours(f.magenta, f.cyan, 0.58));
      strokeCircle(ctx, x, y, r * 0.82, alpha(f.violet, 0.84), Math.max(0.35, r * 0.09));
      identityRing(ctx, x, y, r, recipe, 0.88);
      return;
    }
    const rimMaker = typeof ctx.createConicGradient === 'function' ? 'createConicGradient' : 'createLinearGradient';
    const rimArgs = rimMaker === 'createConicGradient'
      ? [-2.2, x, y] : [x - r * 0.8, y - r * 0.8, x + r * 0.8, y + r * 0.8];
    const rim = gradient(ctx, rimMaker, rimArgs, [
      [0, f.cyan], [0.20, f.blue], [0.40, f.violet], [0.61, f.magenta],
      [0.80, f.teal], [1, f.cyan]
    ]);
    fillCircle(ctx, x, y, r * 0.89, rim);
    /* The PVD spectrum owns the face, not just its rim: a fixed warm crown crosses a
       graphite-violet mid-band into a visibly cyan lower face. */
    const film = gradient(ctx, 'createLinearGradient',
      [x - r * 0.16, y - r * 0.80, x + r * 0.22, y + r * 0.80], [
        [0, mixColours(recipe.face, f.magenta, 0.82)],
        [0.22, mixColours(recipe.face, f.violet, 0.78)],
        [0.48, mixColours(recipe.face, f.blue, 0.58)],
        [0.73, mixColours(recipe.face, f.cyan, 0.82)],
        [1, mixColours(recipe.face, f.teal, 0.68)]
      ]);
    fillCircle(ctx, x, y, r * 0.81, film);
    const spectralBand = gradient(ctx, 'createLinearGradient',
      [x - r * 0.78, y + r * 0.48, x + r * 0.72, y - r * 0.56], [
        [0, alpha(f.cyan, 0)], [0.31, alpha(f.cyan, 0.16)],
        [0.48, alpha('#eef8ff', 0.28)], [0.58, alpha(f.magenta, 0.18)],
        [1, alpha(f.magenta, 0)]
      ]);
    fillCircle(ctx, x, y, r * 0.80, spectralBand);
    const shade = gradient(ctx, 'createRadialGradient',
      [x - r * 0.27, y - r * 0.34, r * 0.04, x, y, r * 0.82], [
        [0, alpha('#f3f7ff', 0.38)], [0.23, alpha('#aebcff', 0.08)],
        [0.66, alpha('#02040a', 0.03)], [1, alpha('#010207', 0.42)]
      ]);
    fillCircle(ctx, x, y, r * 0.80, shade);
    if (tier === 'full') {
      for (let i = 0; i < 13; i++) {
        ctx.lineWidth = Math.max(0.25, r * (0.009 + (i % 3) * 0.003));
        ctx.strokeStyle = alpha(i % 3 === 0 ? f.cyan : (i % 3 === 1 ? f.violet : f.magenta),
          0.075 + (i % 4) * 0.018);
        ctx.beginPath(); ctx.arc(x, y, r * (0.16 + i * 0.048), -2.88, 0.72); ctx.stroke();
      }
    }
    ctx.lineWidth = Math.max(0.36, r * 0.030);
    ctx.strokeStyle = alpha('#f5fbff', 0.48);
    ctx.beginPath(); ctx.arc(x, y, r * 0.73, -2.66, -1.14); ctx.stroke();
    identityRing(ctx, x, y, r, recipe, 0.78);
  }

  function paintGalaxyMaterial(ctx, x, y, r, recipe, tier) {
    const f = recipe.fixedPalette;
    materialHalo(ctx, x, y, r, tier, mixColours(f.blue, f.violet, 0.48), 0.11, -0.10, -0.10);
    fillCircle(ctx, x, y, r, recipe.outer);
    fillCircle(ctx, x, y, r * 0.93, recipe.bezel);
    if (tier === 'signature') {
      fillCircle(ctx, x, y, r * 0.80, recipe.face);
      strokeCircle(ctx, x, y, r * 0.84, alpha(f.violet, 0.82), Math.max(0.35, r * 0.08));
      identityRing(ctx, x, y, r, recipe, 0.82);
      return;
    }
    const face = gradient(ctx, 'createLinearGradient',
      [x - r * 0.72, y - r * 0.72, x + r * 0.72, y + r * 0.72], [
        [0, mixColours(recipe.face, f.highlight, 0.34)],
        [0.26, mixColours(recipe.face, f.blue, 0.40)],
        [0.52, mixColours(recipe.face, f.violet, 0.28)],
        [0.76, recipe.face], [1, mixColours(recipe.face, f.navy, 0.72)]
      ]);
    fillCircle(ctx, x, y, r * 0.83, face);
    const sheen = gradient(ctx, 'createLinearGradient',
      [x - r * 0.76, y + r * 0.64, x + r * 0.68, y - r * 0.70], [
        [0, alpha(f.navy, 0)], [0.34, alpha(f.blue, 0.07)],
        [0.47, alpha(f.violet, 0.34)], [0.56, alpha(f.highlight, 0.24)],
        [0.68, alpha(f.blue, 0.08)],
        [1, alpha(f.navy, 0)]
      ]);
    fillCircle(ctx, x, y, r * 0.82, sheen);
    if (tier === 'full') {
      directionalBrush(ctx, x, y, r, -0.54, f.navy, f.highlight, 0.13);
      for (let i = 0; i < 14; i++) {
        ctx.lineWidth = Math.max(0.20, r * (0.008 + (i % 2) * 0.003));
        ctx.strokeStyle = alpha(i % 2 ? f.blue : f.violet, 0.055 + (i % 4) * 0.018);
        ctx.beginPath(); ctx.arc(x, y, r * (0.14 + i * 0.047), -2.94, 0.46); ctx.stroke();
      }
    }
    ctx.lineWidth = Math.max(0.34, r * 0.026);
    ctx.strokeStyle = alpha(f.highlight, 0.38);
    ctx.beginPath(); ctx.arc(x, y, r * 0.75, -2.70, -1.18); ctx.stroke();
    strokeCircle(ctx, x, y, r * 0.88, alpha(f.violet, 0.72), Math.max(0.38, r * 0.046));
    identityRing(ctx, x, y, r, recipe, 0.76);
  }

  function paintSolarMaterial(ctx, x, y, r, recipe, tier) {
    const f = recipe.fixedPalette;
    materialHalo(ctx, x, y, r, tier, f.amber, 0.14, -0.08, -0.12);
    fillCircle(ctx, x, y, r, recipe.outer);
    fillCircle(ctx, x, y, r * 0.95, recipe.bezel);
    if (tier === 'signature') {
      fillCircle(ctx, x, y, r * 0.78, f.copper);
      strokeCircle(ctx, x, y, r * 0.84, f.amber, Math.max(0.42, r * 0.10));
      identityRing(ctx, x, y, r, recipe, 0.70);
      return;
    }
    const copper = gradient(ctx, 'createRadialGradient',
      [x - r * 0.20, y - r * 0.24, r * 0.025, x, y, r * 0.86], [
        [0, f.gold], [0.15, f.amber], [0.38, '#c66a38'],
        [0.68, f.copper], [0.86, f.ember], [1, f.shadow]
      ]);
    fillCircle(ctx, x, y, r * 0.82, copper);
    const copperSheen = gradient(ctx, 'createLinearGradient',
      [x - r * 0.74, y + r * 0.52, x + r * 0.70, y - r * 0.60], [
        [0, alpha(f.shadow, 0)], [0.38, alpha(f.amber, 0.08)],
        [0.50, alpha(f.gold, 0.34)], [0.62, alpha(f.ember, 0.10)],
        [1, alpha(f.shadow, 0)]
      ]);
    fillCircle(ctx, x, y, r * 0.80, copperSheen);
    strokeCircle(ctx, x, y, r * 0.90, f.gold, Math.max(0.42, r * 0.055));
    strokeCircle(ctx, x, y, r * 0.85, alpha(f.ember, 0.94), Math.max(0.34, r * 0.036));
    if (tier === 'full') {
      /* Fixed phase and opacity sequences make the circular brush grain deterministic. */
      for (let i = 0; i < 25; i++) {
        const radius = r * (0.12 + i * 0.027);
        ctx.lineWidth = Math.max(0.19, r * (0.008 + (i % 3) * 0.0025));
        ctx.strokeStyle = alpha(i % 4 === 0 ? f.gold : f.shadow, 0.085 + (i % 5) * 0.018);
        ctx.beginPath();
        ctx.arc(x, y, radius, -3.02 + (i % 3) * 0.07, 2.94 - (i % 4) * 0.05);
        ctx.stroke();
      }
    }
    ctx.lineWidth = Math.max(0.38, r * 0.030);
    ctx.strokeStyle = alpha('#fff0c0', 0.48);
    ctx.beginPath(); ctx.arc(x, y, r * 0.73, -2.70, -1.14); ctx.stroke();
    identityRing(ctx, x, y, r, recipe, 0.66);
  }

  function paintClassicMaterial(ctx, x, y, r, recipe, tier) {
    const f = recipe.fixedPalette;
    fillCircle(ctx, x, y, r, recipe.outer);
    fillCircle(ctx, x, y, r * 0.94, recipe.bezel);
    if (tier === 'signature') {
      fillCircle(ctx, x, y, r * 0.79, recipe.face);
      strokeCircle(ctx, x, y, r * 0.84, alpha(f.coolEdge, 0.76), Math.max(0.35, r * 0.08));
      identityRing(ctx, x, y, r, recipe, 0.68);
      return;
    }
    const steel = gradient(ctx, 'createLinearGradient',
      [x - r * 0.72, y - r * 0.72, x + r * 0.72, y + r * 0.72], [
        [0, mixColours(recipe.face, f.highlight, 0.48)],
        [0.24, mixColours(recipe.face, f.steel, 0.38)],
        [0.50, recipe.face], [0.76, mixColours(recipe.face, '#111820', 0.34)],
        [1, mixColours(recipe.face, '#05080b', 0.66)]
      ]);
    fillCircle(ctx, x, y, r * 0.83, steel);
    const satin = gradient(ctx, 'createRadialGradient',
      [x - r * 0.26, y - r * 0.31, r * 0.04, x, y, r * 0.86], [
        [0, alpha(f.highlight, 0.26)], [0.38, alpha(f.steel, 0.03)],
        [0.74, alpha('#070a0d', 0.08)], [1, alpha('#020304', 0.42)]
      ]);
    fillCircle(ctx, x, y, r * 0.82, satin);
    if (tier === 'full' && typeof ctx.moveTo === 'function' && typeof ctx.lineTo === 'function') {
      directionalBrush(ctx, x, y, r, 0.04, '#020507', f.highlight, 0.16);
    }
    ctx.lineWidth = Math.max(0.34, r * 0.026);
    ctx.strokeStyle = alpha('#edf5fb', 0.34);
    ctx.beginPath(); ctx.arc(x, y, r * 0.74, -2.70, -1.16); ctx.stroke();
    strokeCircle(ctx, x, y, r * 0.88, alpha(f.coolEdge, 0.62), Math.max(0.34, r * 0.040));
    identityRing(ctx, x, y, r, recipe, 0.62);
  }

  function paintMaterialDirect(ctx, x, y, r, recipe, tier) {
    const detail = tier || 'full';
    if (recipe.family === 'iridescent-pvd') paintCyberMaterial(ctx, x, y, r, recipe, detail);
    else if (recipe.family === 'anodized-alloy') paintGalaxyMaterial(ctx, x, y, r, recipe, detail);
    else if (recipe.family === 'brushed-copper') paintSolarMaterial(ctx, x, y, r, recipe, detail);
    else paintClassicMaterial(ctx, x, y, r, recipe, detail);
  }

  function clearMaterialCache(resetStats) {
    MATERIAL_CACHE.clear();
    materialCacheDpr = null;
    MATERIAL_CACHE_METRICS.clears += 1;
    if (resetStats) {
      MATERIAL_CACHE_METRICS.hits = 0;
      MATERIAL_CACHE_METRICS.misses = 0;
      MATERIAL_CACHE_METRICS.allocations = 0;
      MATERIAL_CACHE_METRICS.evictions = 0;
      MATERIAL_CACHE_METRICS.clears = 0;
    }
  }
  function materialCacheStats() {
    return {
      size: MATERIAL_CACHE.size, capacity: MATERIAL_CACHE_CAPACITY,
      limit: MATERIAL_CACHE_CAPACITY, hits: MATERIAL_CACHE_METRICS.hits,
      misses: MATERIAL_CACHE_METRICS.misses, allocations: MATERIAL_CACHE_METRICS.allocations,
      evictions: MATERIAL_CACHE_METRICS.evictions, clears: MATERIAL_CACHE_METRICS.clears
    };
  }
  function setMaterialCanvasFactory(factory) {
    materialCanvasFactory = typeof factory === 'function' ? factory : null;
    clearMaterialCache();
  }
  function makeMaterialCanvas(width, height) {
    if (materialCanvasFactory) return materialCanvasFactory(width, height);
    if (typeof OffscreenCanvas !== 'undefined') return new OffscreenCanvas(width, height);
    if (typeof document !== 'undefined' && document.createElement) {
      const canvas = document.createElement('canvas');
      canvas.width = width; canvas.height = height;
      return canvas;
    }
    return null;
  }
  function normalDpr(value) {
    const dpr = Number.isFinite(+value) ? +value : 1;
    return Math.max(1, Math.min(3, Math.round(dpr * 2) / 2));
  }
  function currentDpr() {
    return normalDpr(typeof window !== 'undefined' && window.devicePixelRatio ? window.devicePixelRatio : 1);
  }
  function materialCacheKey(recipe, tier, dpr) {
    return [
      recipe.styleName, recipe.substrateKey, recipe.identityKey,
      tier, normalDpr(dpr)
    ].join('|');
  }
  function createMaterialSprite(recipe, tier, dpr) {
    const radius = MATERIAL_RADIUS[tier] || MATERIAL_RADIUS.full;
    const padding = tier === 'full' ? 3 : 1.5;
    const half = radius + padding;
    const ratio = normalDpr(dpr);
    const pixels = Math.max(2, Math.ceil(half * 2 * ratio));
    const canvas = makeMaterialCanvas(pixels, pixels);
    if (!canvas || typeof canvas.getContext !== 'function') return null;
    const spriteCtx = canvas.getContext('2d');
    if (!spriteCtx) return null;
    if (typeof spriteCtx.scale === 'function') {
      spriteCtx.scale(ratio, ratio);
      paintMaterialDirect(spriteCtx, half, half, radius, recipe, tier);
    } else {
      paintMaterialDirect(spriteCtx, half * ratio, half * ratio, radius * ratio, recipe, tier);
    }
    MATERIAL_CACHE_METRICS.allocations += 1;
    return { canvas, half, radius, width: pixels, height: pixels };
  }
  function materialSprite(recipe, tier, dpr) {
    const ratio = normalDpr(dpr);
    if (materialCacheDpr !== null && materialCacheDpr !== ratio) clearMaterialCache();
    materialCacheDpr = ratio;
    const key = materialCacheKey(recipe, tier, ratio);
    if (MATERIAL_CACHE.has(key)) {
      const value = MATERIAL_CACHE.get(key);
      MATERIAL_CACHE.delete(key); MATERIAL_CACHE.set(key, value);
      MATERIAL_CACHE_METRICS.hits += 1;
      return value;
    }
    MATERIAL_CACHE_METRICS.misses += 1;
    const value = createMaterialSprite(recipe, tier, ratio);
    if (!value) return null;
    MATERIAL_CACHE.set(key, value);
    if (MATERIAL_CACHE.size > MATERIAL_CACHE_CAPACITY) {
      MATERIAL_CACHE.delete(MATERIAL_CACHE.keys().next().value);
      MATERIAL_CACHE_METRICS.evictions += 1;
    }
    return value;
  }
  function paintMaterialSurface(ctx, x, y, r, scale, recipe, forceLow) {
    const tier = materialTier(r * Math.max(0.01, scale), forceLow);
    const sprite = materialSprite(recipe, tier, currentDpr());
    if (sprite && typeof ctx.drawImage === 'function') {
      const half = r * sprite.half / sprite.radius;
      ctx.drawImage(sprite.canvas, x - half, y - half, half * 2, half * 2);
    } else {
      paintMaterialDirect(ctx, x, y, r, recipe, tier);
    }
    return tier;
  }

  function sampleMaterialColour(styleName, position, identity, themeColors) {
    const recipe = materialRecipe(styleName, themeColors || {}, 'theme', identity || '#8c83e8');
    const p = position || 'center';
    let colour;
    if (recipe.family === 'iridescent-pvd') {
      colour = p === 'top'
        ? mixColours(recipe.face, recipe.fixedPalette.magenta, 0.64)
        : p === 'bottom'
          ? mixColours(recipe.face, recipe.fixedPalette.cyan, 0.65)
          : mixColours(recipe.face, recipe.fixedPalette.violet, 0.54);
    } else if (recipe.family === 'anodized-alloy') {
      colour = p === 'top'
        ? mixColours(recipe.face, recipe.fixedPalette.violet, 0.30)
        : p === 'bottom'
          ? mixColours(recipe.face, recipe.fixedPalette.navy, 0.44)
          : mixColours(recipe.face, recipe.fixedPalette.blue, 0.22);
    } else if (recipe.family === 'brushed-copper') {
      colour = p === 'top' ? recipe.fixedPalette.amber
        : p === 'bottom' ? recipe.fixedPalette.ember : recipe.fixedPalette.copper;
    } else {
      colour = p === 'top'
        ? mixColours(recipe.face, recipe.fixedPalette.highlight, 0.26)
        : p === 'bottom'
          ? mixColours(recipe.face, '#11161b', 0.36)
          : mixColours(recipe.face, recipe.fixedPalette.steel, 0.16);
    }
    const rgb = hexRgb(colour);
    return [rgb[0], rgb[1], rgb[2], 255];
  }

  function renderMaterialSample(options, identity, themeColors, screenRadius, dpr, forceLow) {
    let styleName, paletteName;
    if (options && typeof options === 'object') {
      styleName = options['style'] || 'cyber';
      identity = options.identityColor || options.identity || '#8c83e8';
      themeColors = options.themeColors || {};
      paletteName = options.palette || 'theme';
      screenRadius = options.screenRadius === undefined
        ? (options.radius === undefined ? 16 : options.radius)
        : options.screenRadius;
      dpr = options.dpr === undefined ? 1 : options.dpr;
      forceLow = !!options.forceLow;
    } else {
      styleName = options || 'cyber';
      paletteName = 'theme';
      identity = identity || '#8c83e8';
      themeColors = themeColors || {};
      screenRadius = screenRadius === undefined ? 16 : screenRadius;
      dpr = dpr === undefined ? 1 : dpr;
    }
    const recipe = materialRecipe(styleName, themeColors, paletteName, identity);
    const tier = materialTier(screenRadius, forceLow);
    const sprite = materialSprite(recipe, tier, dpr);
    let pixels = [];
    if (sprite && sprite.canvas && typeof sprite.canvas.getContext === 'function') {
      const sampleCtx = sprite.canvas.getContext('2d');
      if (sampleCtx && typeof sampleCtx.getImageData === 'function') {
        try { pixels = Array.from(sampleCtx.getImageData(0, 0, sprite.width, sprite.height).data); } catch (_err) { pixels = []; }
      }
    }
    return {
      canvas: sprite ? sprite.canvas : null,
      width: sprite ? sprite.width : 0, height: sprite ? sprite.height : 0,
      pixels, tier, recipe, cache: materialCacheStats()
    };
  }

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
      path: null, asOf: null, ghost: true, sizeBy: 'degree', bridges: false, suggestions: false,
      collapse: 'auto', renderMode: opts.renderMode === 'full' ? 'full' : 'overview'
    };
    let raw = { nodes: [], links: [], suggestions: [] }, adj = {}, hilite = null, hoverSet = null, maxDeg = 1;
    // The classic renderer treats label density as a hard ranked cap, not merely a looser
    // degree threshold. Keeping chosen IDs outside the paint callback bounds fillText work.
    let labelIds = new Set();
    let zoom = 1, collapsed = false;
    /* Recomputed from the *rendered* data on every render, exactly as the classic path
       recomputes GPERF — filters and focus can take a huge store down to a small view. */
    let large = false, dense = false, materialLow = false;
    let staticFullLayout = false, fullLayoutDirty = true;
    /* The node/link arrays last handed to force-graph. Seeding is not free: the vendor copies
       the data in and d3 resets the simulation alpha to 1, so a paint-only change would restart
       the whole layout. See `sameData`/`render`. */
    let seeded = null;
    let destroyed = false, running = true, fitTimer = 0, suspended = 0, pendingRender = null;
    let suppressNodeClickAfterDrag = false, dragClickFrame = 0;
    const requestFrame = typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function'
      ? window.requestAnimationFrame.bind(window)
      : callback => setTimeout(callback, 0);
    const cancelFrame = typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function'
      ? window.cancelAnimationFrame.bind(window)
      : clearTimeout;
    let betweennessReady = false;
    const fg = ForceGraph()(el);
    const api = {};

    function autoFit(duration, padding) {
      const bbox = fg.getGraphBbox && fg.getGraphBbox();
      const width = el.clientWidth, height = el.clientHeight;
      if (!bbox || !bbox.x || !bbox.y || !Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return;
      const xSpan = bbox.x[1] - bbox.x[0], ySpan = bbox.y[1] - bbox.y[0];
      if (!Number.isFinite(xSpan) || !Number.isFinite(ySpan)) return;
      const zoom = Math.min(MAX_AUTO_FIT_ZOOM, Math.max(
        1e-12,
        Math.min((width - 2 * padding) / Math.max(xSpan, 1e-12), (height - 2 * padding) / Math.max(ySpan, 1e-12)),
      ));
      fg.centerAt((bbox.x[0] + bbox.x[1]) / 2, (bbox.y[0] + bbox.y[1]) / 2, duration);
      fg.zoom(zoom, duration);
    }

    function suppressNodeClick() {
      suppressNodeClickAfterDrag = true;
      cancelFrame(dragClickFrame);
      // force-graph dispatches its synthetic click from pointer-up on the next animation
      // frame. Clear after that frame, not a zero-delay timer, so dragging a node can never
      // open the click-only connections panel.
      dragClickFrame = requestFrame(() => {
        suppressNodeClickAfterDrag = false;
        dragClickFrame = 0;
      });
    }

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
       intent of the individual setters is not the caller's: `setSettings` asks for a reheat
       whenever the patch carries a physics key, and the dashboard's sync hands it the whole
       GSET — so it would reheat even on a `render(false, false)` refresh. */
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
    function selectedPalette() {
      const palette = PALETTES[state.palette];
      return palette ? Object.values(palette) : null;
    }
    /* A palette is a colour family, not merely an entity-type override. Previously the
       default Community and Connections modes skipped `overrides`, so choosing Aurora,
       Ocean, Ember, or High contrast changed no pixels unless the user also discovered the
       separate Entity type selector. Use the selected family in every node-colour mode;
       Theme retains the active style's deliberately tuned defaults. */
    function commPal() { return selectedPalette() || COMMUNITY_PALS[state.styleName] || COMMUNITY_PALS.classic; }
    function heatColor(node) {
      const t = (node.rank || 0) / Math.max(1, raw.nodes.length - 1);
      const colors = selectedPalette() || GRAPH_HEAT;
      return colors[Math.min(colors.length - 1, Math.floor(t * colors.length))];
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
      let nodes = raw.nodes.filter(n => (n.degree > 0 && n.degree >= state.minDegree)
        || (state.showUnlinked && n.degree === 0));
      if (state.repo) {
        nodes = nodes.filter(n => [n.repo, n.topic, nodeName(n)]
          .filter(Boolean)
          .join(' ')
          .toLowerCase()
          .includes(state.repo));
      }
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
      if (collapsed && state.renderMode !== 'full') return collapsedData(nodes, links.filter(l => !l.suggested));
      return { nodes, links };
    }

    function applyForces() {
      /* Extremely large complete snapshots use the deterministic fallback, but a normal
         full graph remains a live layout. The previous `renderMode === 'full'` guard removed
         every force and pinned every node, which is why the gravity slider could read 98
         while the canvas stayed on a wide ring. */
      if (staticFullLayout) {
        fg.d3Force('charge', null);
        fg.d3Force('link', null);
        fg.d3Force('x', null);
        fg.d3Force('y', null);
        fg.d3Force('radial', null);
        fg.d3Force('collide', null);
        return;
      }
      const s = state.settings, mode = s.mode || 'compact';
      let charge = fg.d3Force('charge');
      let link = fg.d3Force('link');
      if (!charge && typeof d3 !== 'undefined' && d3.forceManyBody) {
        charge = d3.forceManyBody();
        fg.d3Force('charge', charge);
      }
      if (!link && typeof d3 !== 'undefined' && d3.forceLink) {
        link = d3.forceLink().id(node => node.id);
        fg.d3Force('link', link);
      }
      if (charge && charge.strength) charge.strength(-s.repel);
      if (link && link.distance) link.distance(s.link);
      if (typeof d3 === 'undefined') return;
      fg.d3Force('radial', null);
      /* Community detection still controls colour and link structure, but it must not give
         each community a separate orbit target. The default used those scattered targets and
         made a connected graph settle as a giant ring around empty space. Every standard
         layout now shares the origin as its gravitational centre; repulsion and link distance
         retain the useful local separation without sacrificing a coherent overview. */
      const centering = mode === 'radial' ? Math.max(0.04, s.gravity / 300) : s.gravity / 100;
      fg.d3Force('x', d3.forceX(0).strength(centering));
      fg.d3Force('y', d3.forceY(0).strength(centering));
      if (mode === 'radial' && d3.forceRadial) fg.d3Force('radial', d3.forceRadial(n => Math.max(0, 5 - Math.min(5, n.degree || 0)) * Math.max(8, s.link * 0.72)).strength(0.32));
      /* One collision pass on a large graph, two otherwise — the classic path's
         `.iterations(GPERF.large?1:2)`. The second pass costs another full quadtree traversal
         per node on every tick, and a large store pays that on the initial layout and on every
         reheat, which is exactly where it is least affordable. */
      if (d3.forceCollide) fg.d3Force('collide', d3.forceCollide(n => n.radius + 1.5).iterations(large ? 1 : 2));
    }

    function clearPinnedPositions(data) {
      data.nodes.forEach(node => {
        node.x = undefined;
        node.y = undefined;
        node.vx = undefined;
        node.vy = undefined;
        node.fx = undefined;
        node.fy = undefined;
      });
    }

    function pinFullGraphLayout(data) {
      /* The rare fallback above the live-force ceiling is deterministic and bounded, but it
         must still answer the tuning controls. A centred grid avoids the old empty-core ring;
         higher gravity compacts it, while repel/link/node-size determine local spacing. */
      const groups = new Map();
      data.nodes.forEach(node => {
        const key = `${node.community || 0}:${node.etype || 'entity'}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(node);
      });
      const ordered = [...groups.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
      const s = state.settings;
      const gravity = Math.max(0, Math.min(1, Number(s.gravity) / 100 || 0));
      const repel = Math.max(0, Number(s.repel) || 0);
      const link = Math.max(4, Number(s.link) || 4);
      const nodeSize = Math.max(1, Number(s.size) || 3);
      const compactness = 1.75 - gravity * 1.4;
      const localGap = (4 + nodeSize * 1.6 + Math.sqrt(repel) * 0.8 + link * 0.16) * compactness;
      const columns = Math.max(1, Math.ceil(Math.sqrt(ordered.length)));
      const largestGroup = ordered.reduce((largest, [, nodes]) => Math.max(largest, nodes.length), 1);
      const cell = Math.max(90, Math.sqrt(largestGroup) * localGap * 2.4 + link * 3) * compactness;
      const golden = Math.PI * (3 - Math.sqrt(5));
      ordered.forEach(([, nodes], groupIndex) => {
        nodes.sort((a, b) => (b.degree || 0) - (a.degree || 0) || String(a.id).localeCompare(String(b.id)));
        const column = groupIndex % columns;
        const row = Math.floor(groupIndex / columns);
        const centerX = (column - (columns - 1) / 2) * cell;
        const centerY = (row - (Math.ceil(ordered.length / columns) - 1) / 2) * cell * 0.72;
        const nodeColumns = Math.max(1, Math.ceil(Math.sqrt(nodes.length)));
        const nodeRows = Math.ceil(nodes.length / nodeColumns);
        nodes.forEach((node, index) => {
          /* A spiral makes a large single community read as an empty-core ring. Pack the
             deterministic fallback around its group centre instead, preserving every node
             while keeping the complete graph visually centred and bounded. */
          const x = centerX + ((index % nodeColumns) - (nodeColumns - 1) / 2) * localGap;
          const y = centerY + (Math.floor(index / nodeColumns) - (nodeRows - 1) / 2) * localGap;
          node.x = x;
          node.y = y;
          node.vx = 0;
          node.vy = 0;
          node.fx = x;
          node.fy = y;
        });
      });
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
      const col = node.color;
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
        // Cluster names sit outside the coloured bubble.  They therefore need the active
        // theme's text colour, not the dark-theme near-white that disappears on light canvas.
        ctx.fillStyle = state.themeColors.label || '#e7e9ee';
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
      /* Material gradients, grain, and halos live in the bounded sprite cache. The direct
         fallback preserves them when detached canvases are unavailable, while a large graph
         forces the gradient-free signature tier. */
      let nodeMaterial;
      if (state.styleName === 'galaxy') {
        nodeMaterial = materialRecipe('galaxy', state.themeColors, state.palette, col);
        paintMaterialSurface(ctx, node.x, node.y, r, scale, nodeMaterial, materialLow);
      } else if (state.styleName === 'solar') {
        const sun = node.rank === 0;
        nodeMaterial = materialRecipe(
          'solar', state.themeColors, state.palette,
          sun ? mixColours(col, '#d38b43', 0.46) : col
        );
        paintMaterialSurface(ctx, node.x, node.y, r, scale, nodeMaterial, materialLow);
      } else if (state.styleName === 'cyber') {
        /* Cyberpunk owns a broad, fixed cyan→violet→magenta PVD face. Palette colour is kept
           out of that film and appears only in the slim identity ring. */
        nodeMaterial = materialRecipe('cyber', state.themeColors, state.palette, col);
        paintMaterialSurface(ctx, node.x, node.y, r, scale, nodeMaterial, materialLow);
      } else {
        nodeMaterial = materialRecipe('classic', state.themeColors, state.palette, col);
        paintMaterialSurface(ctx, node.x, node.y, r, scale, nodeMaterial, materialLow);
        if (node.hub) { ctx.lineWidth = 0.8 / scale; ctx.strokeStyle = node.stroke; ctx.stroke(); }
      }
      if (node.id === hilite) {
        /* Hover lifts exposure without changing the material or rotating its light. The two
           unblurred rings remain crisp at every DPR and also serve explicit selection. */
        fillCircle(ctx, node.x, node.y, r * 0.76, alpha('#ffffff', 0.065));
        ctx.lineWidth = 1.15 / scale;
        ctx.strokeStyle = alpha(nodeMaterial.sheen, 0.98);
        ctx.beginPath(); ctx.arc(node.x, node.y, r + 1.35 / scale, 0, 6.2832); ctx.stroke();
        ctx.lineWidth = 0.55 / scale;
        ctx.strokeStyle = alpha(nodeMaterial.identity, 0.92);
        ctx.beginPath(); ctx.arc(node.x, node.y, r + 2.45 / scale, 0, 6.2832); ctx.stroke();
      }
      const showLabel = (state.settings.labels && labelIds.has(node.id)) || node.id === hilite || neighbor;
      if (showLabel && scale > 0.35) {
        // The dashboard setting is a screen-space font size.  As on the classic renderer,
        // compensate only for graph zoom; an extra artistic divisor makes a configured 12px
        // label unreadable at normal zoom and makes the Font size control misleading.
        const size = Math.max(2, state.settings.font / scale);
        ctx.font = '500 ' + size + 'px system-ui, sans-serif';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = 'rgba(0,0,0,.5)';
        ctx.fillText(nodeName(node), node.x + r + 1.6 + 0.3, node.y + 0.3);
        // Node names sit directly on the canvas, so Classic's light and sepia themes need
        // the dashboard-resolved text colour just like relation and collapsed-cluster labels.
        ctx.fillStyle = state.themeColors.label || (node.id === hilite ? '#ffffff' : 'rgba(232,236,245,.86)');
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

    /* The dashboard's **Labels** checkbox turns on *both* label layers on the classic path:
       entity names (painted by styleNode) and relation names (a `linkCanvasObject`, drawn
       'after' the line so it sits on top of it). Without this second half the checkbox silently
       did half its job under `?graph-engine=next` and a relation name could only be read by
       hovering one edge at a time. Same gates as classic graphRender(): zoomed in past
       LINK_LABEL_MIN_SCALE, the relation carries a meaningful label (implicit co-occurrences
       are graph structure, not canvas text), and — on a dense graph — only while something is
       highlighted, so thousands of overlapping strings are never
       painted at once. Canvas text is not an HTML sink, so the raw label is drawn here; the
       escaped copy is for `linkLabel`, whose tooltip *is* one. */
    function applyLinkLabels() {
      if (!fg.linkCanvasObject || !fg.linkCanvasObjectMode) return;
      if (!state.settings.labels) { fg.linkCanvasObjectMode(() => undefined); return; }
      fg.linkCanvasObjectMode(() => 'after').linkCanvasObject((link, ctx, scale) => {
        if (!link || !showRelationLabel(link.label) || scale < LINK_LABEL_MIN_SCALE) return;
        if (dense && !hilite) return;
        const source = link.source, target = link.target;
        if (!source || !target || typeof source !== 'object' || typeof target !== 'object') return;
        if (!Number.isFinite(source.x) || !Number.isFinite(source.y)) return;
        if (!Number.isFinite(target.x) || !Number.isFinite(target.y)) return;
        if (link.ghost) return;
        ctx.font = ((state.settings.font || 12) * 0.82) / scale + 'px system-ui, sans-serif';
        ctx.fillStyle = state.themeColors.relation_label || '#7e8795';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(link.label), (source.x + target.x) / 2, (source.y + target.y) / 2);
        ctx.textAlign = 'left';
      });
    }

    /* Does this render show the same entities and relations as the one force-graph is already
       holding? Compared by identity of the *view*, not of the payload: `visible()` allocates
       fresh arrays every call (and `collapsedData` fresh cluster nodes), so an object compare
       would report a change for Style, Color by, Labels and Flow — none of which move a node. */
    function sameData(previous, next) {
      if (!previous) return false;
      if (previous.nodes.length !== next.nodes.length) return false;
      if (previous.links.length !== next.links.length) return false;
      for (let i = 0; i < next.nodes.length; i++) {
        if (previous.nodes[i].id !== next.nodes[i].id) return false;
      }
      for (let i = 0; i < next.links.length; i++) {
        const a = previous.links[i], b = next.links[i];
        if (linkEndpoint(a, 'source') !== linkEndpoint(b, 'source')) return false;
        if (linkEndpoint(a, 'target') !== linkEndpoint(b, 'target')) return false;
        if ((a.layer || '') !== (b.layer || '')) return false;
        if (!a.suggested !== !b.suggested) return false;
        if (!a.ghost !== !b.ghost) return false;
      }
      return true;
    }

    /* Large graphs settle harder, exactly as the classic path does (`GPERF.large?.055:.035`).
       Shared so reheat() and freeze() cannot drift back to the small-graph constant. */
    function alphaDecay() { return large ? 0.055 : 0.035; }

    function render(fit, reheat) {
      if (destroyed) return;
      if (suspended) {
        pendingRender = pendingRender ? [pendingRender[0] || fit, pendingRender[1] || reheat] : [fit, reheat];
        return;
      }
      const motion = !reduced();
      const next = visible();
      /* Reuse the arrays force-graph already holds when the view is unchanged: the sizing and
         colouring pass below must write onto the objects the vendor is painting from, and the
         collapsed view hands out freshly built cluster nodes on every call. */
      const reused = sameData(seeded, next);
      const data = reused ? seeded : next;
      const fullGraph = state.renderMode === 'full';
      staticFullLayout = fullGraph
        && (data.nodes.length > FULL_FORCE_NODE_LIMIT || data.links.length > FULL_FORCE_LINK_LIMIT);
      materialLow = data.nodes.length > LARGE_NODE_LIMIT || data.links.length > LARGE_LINK_LIMIT;
      large = fullGraph || data.nodes.length > LARGE_NODE_LIMIT || data.links.length > LARGE_LINK_LIMIT;
      dense = data.links.length > DENSE_LINK_LIMIT;
      const sizeMetric = n => state.sizeBy === 'betweenness' ? (n.betweenness || 0) : ((n.degree || 0) / Math.max(1, maxDeg));
      data.nodes.forEach(n => {
        const base = (state.settings.size || 3);
        n.radius = graphNodeRadius(n, base, sizeMetric(n));
        n.color = nodeColor(n);
        n.stroke = contrastOn(n.color);
      });
      const labelCap = Math.max(1, Math.round(Number(state.settings.labelDensity) || 40));
      labelIds = new Set(data.nodes
        .filter(n => !n.cluster && !n.ghost)
        .sort((a, b) => (b.degree || 0) - (a.degree || 0)
          || (b.betweenness || 0) - (a.betweenness || 0)
          || String(a.id).localeCompare(String(b.id)))
        .slice(0, labelCap)
        .map(n => n.id));
      applyChrome();
      if (!reused) {
        if (staticFullLayout) {
          pinFullGraphLayout(data);
          fullLayoutDirty = false;
        } else clearPinnedPositions(data);
        fg.graphData(data);
        seeded = data;
      } else if (staticFullLayout && fullLayoutDirty) {
        pinFullGraphLayout(data);
        fullLayoutDirty = false;
      }
      applyForces();
      fg.autoPauseRedraw(!needsContinuousFrames());
      /* Bound the simulation the way the classic path does. Without these force-graph keeps its
         15-second default window, so every load and every reheat of a large store runs the
         layout — and repaints every node and link — for more than ten seconds longer. */
      if (fg.cooldownTime) fg.cooldownTime(motion && !staticFullLayout ? (large ? 1100 : 2200) : 0);
      if (fg.cooldownTicks) fg.cooldownTicks(motion && !staticFullLayout ? (large ? 80 : 160) : 1);
      if (fg.warmupTicks) fg.warmupTicks(motion && !staticFullLayout ? (large ? 18 : 40) : 0);
      if (fg.d3AlphaDecay) fg.d3AlphaDecay(staticFullLayout ? 1 : alphaDecay());
      if (fg.d3VelocityDecay) fg.d3VelocityDecay(large ? 0.45 : 0.38);
      if (fg.linkCurvature) {
        fg.linkCurvature(dense ? 0 : ((PRESETS[state.settings.mode] || PRESETS.compact).curve || 0));
      }
      fg.linkDirectionalArrowLength(dense ? 0 : 0.625).linkDirectionalArrowRelPos(1);
      applyLinkLabels();
      if (fg.linkDirectionalParticles) {
        const flowing = !fullGraph
          && state.settings.flow !== false
          && motion
          && data.links.length <= PARTICLE_LINK_LIMIT;
        const particles = !flowing
          ? 0
          : (state.styleName === 'cyber' ? 3 : ((PRESETS[state.settings.mode] || {}).particles || 2));
        fg.linkDirectionalParticles(l => l.suggested || l.ghost ? 0 : particles)
          .linkDirectionalParticleWidth(1)
          .linkDirectionalParticleCanvasObject(paintFlowArrow)
          .linkDirectionalParticleColor(l => alpha(layerColor(l.layer), 0.95))
          .linkDirectionalParticleSpeed(l => 0.002 + ((state.settings.flowSpeed || 45) / 100) * 0.008);
      }
      if (reheat && motion && !staticFullLayout && !state.settings.frozen && fg.d3ReheatSimulation) fg.d3ReheatSimulation();
      if ((staticFullLayout || state.settings.frozen || !motion) && fg.d3AlphaDecay) { /* keep painting, stop layout */ fg.d3AlphaDecay(1); }
      /* Nothing was reseeded, so force-graph's own change detection saw no reason to repaint —
         but Style, Color by and Labels all just changed how the *same* data must be drawn. */
      if (reused) invalidate();
      if (fit) {
        clearTimeout(fitTimer);
        fitTimer = setTimeout(() => { if (!destroyed) autoFit(motion ? 600 : 0, 40); }, motion ? 320 : 0);
      }
      if (opts.onStats) opts.onStats({ nodes: data.nodes.length, links: data.links.length, total: raw.nodes.length, totalLinks: raw.links.length, preset: (PRESETS[state.settings.mode] || PRESETS.compact).label, collapsed: collapsed, ghosts: data.nodes.filter(n => n.ghost).length, bridges: data.links.filter(l => l.bridge).length, suggested: data.links.filter(l => l.suggested).length });
    }

    function handleNodeClick(node) {
      if (suppressNodeClickAfterDrag) {
        suppressNodeClickAfterDrag = false;
        return;
      }
      if (node.cluster) {
        collapsed = false;
        state.collapse = false;
        render(false, true);
        setTimeout(() => { fg.centerAt(node.x, node.y, 500); fg.zoom(1.6, 500); }, 60);
        if (opts.onCollapseChange) opts.onCollapseChange(false);
        return;
      }
      if (opts.onNodeClick) opts.onNodeClick(node);
    }

    fg.backgroundColor('rgba(0,0,0,0)').nodeRelSize(1)
      .enableNodeDrag(false).autoPauseRedraw(true)
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
        /* The reference boards use one coherent lighting system per visual style. Relation
           layers still affect behaviour and particles, but should not turn Galaxy green or
           Solar pink simply because the source relation has that semantic layer. */
        let base = layerColor(l.layer);
        if (state.styleName === 'galaxy') base = l.layer === 'causal' ? '#c58bff' : '#91a8ff';
        else if (state.styleName === 'solar') base = l.layer === 'causal' ? '#ffc06d' : '#ef913e';
        else if (state.styleName === 'cyber') base = l.layer === 'causal' ? '#ec71d2' : '#6edce6';
        else if (state.styleName === 'classic') base = l.layer === 'causal' ? '#b9c8da' : '#86c7d1';
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
      .onNodeClick(handleNodeClick)
      // Kept as the pinning contract for embedders that opt back into vendor dragging;
      // Ledger itself disables that path and uses the scoped pointer controller below.
      .onNodeDragEnd(node => { node.fx = node.x; node.fy = node.y; suppressNodeClick(); })
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

    /* force-graph's built-in drag always reheats the entire simulation. Ledger treats manual
       placement as a pin, so install a small scoped drag controller and leave global physics
       changes to the explicit Reheat control. Capturing pointer-down prevents the vendor's
       drag handler from seeing node gestures while preserving its background pan/zoom path. */
    let detachManualDrag = null;
    if (typeof window !== 'undefined' && typeof window.addEventListener === 'function'
      && typeof el.addEventListener === 'function' && typeof el.querySelector === 'function') {
      let manualDrag = null;
      const graphPoint = event => {
        const canvas = el.querySelector('canvas');
        if (!canvas || !canvas.getBoundingClientRect || !fg.screen2GraphCoords) return null;
        const box = canvas.getBoundingClientRect();
        return fg.screen2GraphCoords(event.clientX - box.left, event.clientY - box.top);
      };
      const endManualDrag = event => {
        if (!manualDrag || (event.pointerId != null && event.pointerId !== manualDrag.pointerId)) return;
        const current = manualDrag;
        manualDrag = null;
        window.removeEventListener('pointermove', moveManualDrag, true);
        window.removeEventListener('pointerup', endManualDrag, true);
        window.removeEventListener('pointercancel', endManualDrag, true);
        if (current.dragged) {
          current.node.fx = current.node.x;
          current.node.fy = current.node.y;
          current.node.vx = 0;
          current.node.vy = 0;
          suppressNodeClick();
        } else if (event.type !== 'pointercancel') {
          // Our capture listener owns the direct click. Suppress force-graph's
          // later pointer-up callback only after dispatching this click ourselves.
          handleNodeClick(current.node);
          suppressNodeClick();
        }
      };
      const moveManualDrag = event => {
        if (!manualDrag || event.pointerId !== manualDrag.pointerId) return;
        const point = graphPoint(event);
        if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) return;
        const dx = event.clientX - manualDrag.startClientX;
        const dy = event.clientY - manualDrag.startClientY;
        if (!manualDrag.dragged) {
          if (Math.hypot(dx, dy) < 3) {
            event.preventDefault();
            event.stopPropagation();
            return;
          }
          manualDrag.dragged = true;
        }
        const node = manualDrag.node;
        node.x = node.fx = point.x + manualDrag.offsetX;
        node.y = node.fy = point.y + manualDrag.offsetY;
        node.vx = 0;
        node.vy = 0;
        invalidate();
        event.preventDefault();
        event.stopPropagation();
      };
      const beginManualDrag = event => {
        if (event.button !== 0 || event.isPrimary === false) return;
        const point = graphPoint(event);
        if (!point) return;
        let candidate = null;
        let distance = Infinity;
        (fg.graphData().nodes || []).forEach(node => {
          if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
          const d = Math.hypot(node.x - point.x, node.y - point.y);
          const hitRadius = (node.radius || 1) + 5 / Math.max(zoom, 0.1);
          if (d <= hitRadius && d < distance) { candidate = node; distance = d; }
        });
        if (!candidate) return;
        manualDrag = {
          node: candidate, pointerId: event.pointerId, startClientX: event.clientX,
          startClientY: event.clientY, offsetX: candidate.x - point.x,
          offsetY: candidate.y - point.y, dragged: false,
        };
        window.addEventListener('pointermove', moveManualDrag, true);
        window.addEventListener('pointerup', endManualDrag, true);
        window.addEventListener('pointercancel', endManualDrag, true);
        event.preventDefault();
        event.stopPropagation();
      };
      el.addEventListener('pointerdown', beginManualDrag, true);
      detachManualDrag = () => {
        manualDrag = null;
        el.removeEventListener('pointerdown', beginManualDrag, true);
        window.removeEventListener('pointermove', moveManualDrag, true);
        window.removeEventListener('pointerup', endManualDrag, true);
        window.removeEventListener('pointercancel', endManualDrag, true);
      };
    }

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
      /* A fresh payload means fresh node objects, so the cached seed is stale even when the
         ids are identical — force-graph must be re-pointed at the new objects or the render
         below would style ones nobody is painting from. */
      seeded = null;
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
      if ((state.bridges || state.sizeBy === 'betweenness') && opts.onMetrics) {
        opts.onMetrics(api.metrics());
      }
      render(true, true);
    };
    /* Which of these settings changes the *layout* rather than just the paint, matching the
       classic path's `key==='repel'||key==='link'||key==='gravity'||key==='size'` in
       dashboard.js::graphSet — `size` counts because it feeds d3.forceCollide, and `mode`
       swaps the whole force arrangement. applyForces() only writes the new charge / link /
       forceX-forceY / collide values into the simulation force-graph is already running, and a
       settled graph sits at alpha~0, so without the reheat those sliders install a force that
       moves nothing. The paint-only settings must keep the arrangement the user is reading.
       render() applies the reduced-motion exemption (`if(layout&&!prefersReducedMotion())`). */
    const LAYOUT_KEYS = ['mode', 'repel', 'link', 'gravity', 'size'];
    api.setSettings = patch => {
      if (LAYOUT_KEYS.some(k => patch && patch[k] !== undefined)) fullLayoutDirty = true;
      Object.assign(state.settings, patch);
      render(false, LAYOUT_KEYS.some(k => patch && patch[k] !== undefined));
    };
    api.setPreset = name => {
      const p = PRESETS[name] || PRESETS.compact;
      state.settings.mode = PRESETS[name] ? name : 'compact';
      ['repel', 'link', 'gravity', 'font', 'size', 'linkw', 'labelDensity'].forEach(k => { if (p[k] !== undefined) state.settings[k] = p[k]; });
      fullLayoutDirty = true;
      render(true, true);
      return { ...state.settings };
    };
    api.setStyle = name => {
      state.styleName = ['classic', 'galaxy', 'solar', 'cyber'].indexOf(name) < 0 ? 'cyber' : name;
      clearMaterialCache();
      render(false, false);
    };
    api.setRenderMode = mode => {
      const next = mode === 'full' ? 'full' : 'overview';
      if (state.renderMode === next) return;
      state.renderMode = next;
      if (next === 'full') {
        state.collapse = false;
        collapsed = false;
      }
      seeded = null;
      fullLayoutDirty = true;
      render(true, true);
    };
    api.setColorBy = name => { state.colorBy = name; clearMaterialCache(); refreshColors(); render(false, false); };
    api.setPalette = name => {
      state.palette = name;
      state.overrides = PALETTES[name] ? { ...PALETTES[name] } : {};
      clearMaterialCache();
      refreshColors();
    };
    api.setTypeColor = (type, color) => {
      state.overrides[type] = color;
      state.palette = 'custom';
      clearMaterialCache();
      refreshColors();
    };
    /* Rehydrating saved overrides is not a user edit, so it must not flip the palette
       selector to "custom" behind the user's back the way setTypeColor deliberately does. */
    api.setTypeColors = map => { Object.assign(state.overrides, map || {}); clearMaterialCache(); refreshColors(); };
    /* The active theme's resolved `--entity-*` values. Replaced wholesale rather than merged:
       a theme switch must not leave the previous theme's colour for a type the new one omits. */
    api.setThemeColors = map => {
      state.themeColors = map && typeof map === 'object' ? { ...map } : {};
      clearMaterialCache();
      refreshColors();
    };
    /* One render for a whole batch of setters — see `batch`. */
    api.apply = (fn, fit, reheat) => { batch(fn, fit, reheat); };
    api.setHighlight = id => {
      hilite = id == null ? null : id;
      hoverSet = id == null ? null : new Set([id].concat(adj[id] || []));
      invalidate();
    };
    api.setScope = patch => { Object.assign(state, patch); render(false, true); };
    api.setLayers = layers => { state.layers = layers; render(false, false); };
    /* `focus` remains the explicit neighbourhood-isolation action. It must not schedule a
       delayed zoom-to-fit: callers that also centre a node otherwise start two competing
       camera animations, and the late fit wins by dragging the selected entity away. */
    api.focus = id => {
      if (destroyed || !raw.nodes.some(node => node.id === id)) return false;
      state.focusId = id;
      hilite = id;
      hoverSet = new Set([id].concat(adj[id] || []));
      clearTimeout(fitTimer);
      fitTimer = 0;
      render(false, true);
      return true;
    };
    api.clearFocus = () => {
      state.focusId = null;
      hilite = null;
      hoverSet = null;
      render(true, true);
    };
    /* Export the graph the person is actually looking at, not the unfiltered response
       retained for later scope changes. Strip force-graph's transient coordinates and turn
       endpoint objects back into stable ids so the resulting JSON is portable. */
    api.exportData = () => {
      const data = visible();
      return {
        nodes: data.nodes.map(node => {
          const { x, y, vx, vy, fx, fy, color, stroke, radius, ...stable } = node;
          return stable;
        }),
        links: data.links.map(link => ({
          ...link,
          source: linkEndpoint(link, 'source'),
          target: linkEndpoint(link, 'target'),
        })),
      };
    };
    api.fit = () => { if (!destroyed) fg.zoomToFit(reduced() ? 0 : 500, 40); };
    api.reheat = () => {
      if (destroyed || reduced() || staticFullLayout) return;
      raw.nodes.forEach(n => { n.fx = undefined; n.fy = undefined; });
      if (fg.d3ReheatSimulation) { fg.d3AlphaDecay(alphaDecay()); fg.d3ReheatSimulation(); }
    };
    api.freeze = on => {
      state.settings.frozen = on;
      if (on) {
        const charge = fg.d3Force('charge');
        if (charge && charge.strength) charge.strength(0);
        fg.d3AlphaDecay(1);
        return;
      }
      // Dragging pins a node with fx/fy. Unfreezing is a request to resume the layout, not
      // merely the unpinned subset, so release those anchors before the simulation reheats.
      if (staticFullLayout) return;
      raw.nodes.forEach(n => { n.fx = undefined; n.fy = undefined; });
      applyForces();
      if (reduced()) return;
      fg.d3AlphaDecay(alphaDecay());
      if (fg.d3ReheatSimulation) fg.d3ReheatSimulation();
    };
    function renderedNode(id) {
      return ((fg.graphData() || {}).nodes || []).find(node => node && node.id === id) || null;
    }

    function centerRenderedNode(id) {
      const node = renderedNode(id);
      if (!node || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return false;
      // A pending fit comes from an earlier layout action. Cancelling it makes one selection
      // correspond to exactly one camera target instead of letting a delayed whole-graph fit
      // override `centerAt` midway through its animation.
      clearTimeout(fitTimer);
      fitTimer = 0;
      const duration = reduced() ? 0 : 500;
      fg.centerAt(node.x, node.y, duration);
      fg.zoom(3, duration);
      return true;
    }

    /* Returning `false` is not a failure: it is the signal the dashboard's graphFocus() uses to
       run its recovery path ("show unlinked", then retry, then say so). Reporting success for an
       entity that is not on the canvas is therefore worse than reporting failure — the user gets
       a camera move to nothing and no explanation. Two ways that happened: the auto-collapsed
       view paints only `cluster-*` bubbles, and any filtered-out node keeps the x/y force-graph
       left on it from an earlier render, so "found in `raw.nodes` with finite coordinates" was
       never evidence of visibility. Expand a collapsed view first — focusing a named entity is
       an explicit request to see it — then confirm against the data force-graph is holding. */
    api.zoomToNode = id => {
      if (destroyed) return false;
      if (!raw.nodes.some(node => node.id === id)) return false;
      clearTimeout(fitTimer);
      fitTimer = 0;
      if (collapsed) {
        collapsed = false;
        state.collapse = false;
        render(false, false);
        if (opts.onCollapseChange) opts.onCollapseChange(false);
      }
      return centerRenderedNode(id);
    };
    /* Graph facts and search results are reveal actions, not requests to restart or isolate the
       layout. Keep the current graph stable, expand a collapsed view when needed, highlight the
       exact rendered entity, and centre it without a competing fit animation. */
    api.reveal = id => {
      if (destroyed || !raw.nodes.some(node => node.id === id)) return false;
      clearTimeout(fitTimer);
      fitTimer = 0;
      let changedView = false;
      if (state.focusId !== null) {
        state.focusId = null;
        changedView = true;
      }
      if (collapsed) {
        collapsed = false;
        state.collapse = false;
        changedView = true;
        if (opts.onCollapseChange) opts.onCollapseChange(false);
      }
      if (changedView) render(false, false);
      hilite = id;
      hoverSet = new Set([id].concat(adj[id] || []));
      invalidate();
      return centerRenderedNode(id);
    };
    api.state = () => ({ ...state, collapsed, highlight: hilite });
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
      if (state.sizeBy === 'betweenness') {
        ensureBetweenness();
        if (opts.onMetrics) opts.onMetrics(api.metrics());
      }
      render(false, false);
    };
    api.setBridges = on => {
      state.bridges = on;
      if (on) {
        ensureBetweenness();
        if (opts.onMetrics) opts.onMetrics(api.metrics());
      }
      render(false, false);
    };
    /* Forces the lazy analysis for an explicit analysis control or the Graph facts readout. */
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
      state.collapse = state.renderMode === 'full' ? false : mode;
      const next = state.renderMode !== 'full' && (mode === true || (mode === 'auto' && zoom < 0.55));
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
      cancelFrame(dragClickFrame);
      try {
        if (detachManualDrag) { detachManualDrag(); detachManualDrag = null; }
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
      seeded = null;
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
    requestAnimationFrame(() => { if (destroyed) return; measure(); autoFit(reduced() ? 0 : 400, 40); });
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
      graphNodeRadius, paintFlowArrow,
      nodeName, linkEndpoint, asOfValue, materialRecipe, materialTier,
      paintMaterialDirect, renderMaterialSample, sampleMaterialColour,
      materialCacheStats, clearMaterialCache, setMaterialCanvasFactory
    }
  };
})();
