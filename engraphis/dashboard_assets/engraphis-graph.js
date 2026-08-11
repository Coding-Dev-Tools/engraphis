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
    galaxy: { label: 'Galaxy gravity', repel: 60, link: 8, gravity: 48, font: 12, size: 3, linkw: 0.72, labelDensity: 24, curve: 0.12, particles: 0 },
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
  /* The v2 overview scene is bounded at 1,000 nodes / 2,000 edges. Galaxy keeps that
     complete overview physical even after the canvas enters its cheaper 600-node material
     tier. Non-Galaxy complete snapshots retain the older FULL_FORCE_* fallback. */
  const GALAXY_LIVE_NODE_LIMIT = 1000;
  const GALAXY_LIVE_LINK_LIMIT = 2000;
  function galaxySceneWithinLiveLimit(data) {
    const scene = data || {};
    return (scene.nodes || []).length <= GALAXY_LIVE_NODE_LIMIT
      && (scene.links || []).length <= GALAXY_LIVE_LINK_LIMIT;
  }
  const GALAXY_EXACT_LIMIT = 64;
  const GALAXY_BARNES_HUT_THETA = 0.85;
  const GALAXY_GRAVITY_MAXIMUM = 400;
  /* The emergency acceleration cap follows the full visible strength range. Direct callers can
     still pass pathological values, but those values clamp to the same 0..400 physics ceiling. */
  const GALAXY_GRAVITY_CAP_REFERENCE = GALAXY_GRAVITY_MAXIMUM;
  /* One response curve owns every physical layer. It retains the positive quadratic response
     and two C1 smooth boost stages. Local gravity is exactly 120 at the default; zero remains
     the black-hole field's true endpoint. Independent community stars apply their named
     minimum and faster clock afterward. */
  function galaxySmoothstep(value) {
    const raw = Number(value);
    const t = Number.isFinite(raw) ? Math.max(0, Math.min(1, raw)) : 0;
    return t * t * (3 - 2 * t);
  }
  function galaxyGravityConstant(setting) {
    const raw = Number(setting);
    const value = Number.isFinite(raw) ? Math.max(0, Math.min(GALAXY_GRAVITY_MAXIMUM, raw)) : 0;
    const base = value * (772 + 11 * value) / 2600;
    const boost = 1 + 0.25 * galaxySmoothstep(value / 48)
      + 0.25 * galaxySmoothstep((value - 48) / 52);
    return base * boost * 4;
  }
  /* Gravity strength is a black-hole control first. The center field is twice the prior
     all-purpose response and the compatibility local field remains half of that. */
  function galaxyBlackHoleGravityConstant(setting) {
    return galaxyGravityConstant(setting) * 2;
  }
  function galaxyLocalGravityConstant(setting) {
    return galaxyBlackHoleGravityConstant(setting) * 0.5;
  }
  /* A fit-to-view galaxy compresses stellar and galactic distances onto one canvas, so using
     one physical clock made a valid planet orbit visually disappear under its system's
     black-hole sweep. Give independent community stars a 2.5x angular clock by multiplying
     their gravitational parameter by clock^2. Both the circular seed and every live
     inverse-square sample consume this same constant: the result is a faster bound central
     orbit, not a per-frame carousel or an unbalanced tangential kick. The global anchor keeps
     the original local scale because its surrounding bulge belongs to the black-hole well. */
  const GALAXY_STELLAR_ORBIT_CLOCK = 2.5;
  /* The dashboard's Gravity control owns the black-hole well. A saved zero value must not
     erase every independent solar system: eligible community stars retain the calibrated
     default stellar well while the global anchor remains a true zero-gravity endpoint. */
  const GALAXY_STELLAR_GRAVITY_FLOOR_SETTING = 48;
  function galaxyStellarGravitySetting(setting) {
    const raw = Number(setting);
    const value = Number.isFinite(raw)
      ? Math.max(0, Math.min(GALAXY_GRAVITY_MAXIMUM, raw)) : 0;
    return Math.max(GALAXY_STELLAR_GRAVITY_FLOOR_SETTING, value);
  }
  function galaxyStellarGravityConstant(setting) {
    return galaxyLocalGravityConstant(galaxyStellarGravitySetting(setting))
      * GALAXY_STELLAR_ORBIT_CLOCK * GALAXY_STELLAR_ORBIT_CLOCK;
  }
  function galaxyFallbackStellarGravityConstant(setting) {
    return galaxyLocalGravityConstant(setting)
      * GALAXY_STELLAR_ORBIT_CLOCK * GALAXY_STELLAR_ORBIT_CLOCK;
  }
  function galaxySystemGravityConstant(anchor, setting) {
    if (anchor && anchor.anchor_role === 'global') return galaxyLocalGravityConstant(setting);
    if (anchor && anchor.anchor_role === 'community') {
      return galaxyStellarGravityConstant(setting);
    }
    return galaxyFallbackStellarGravityConstant(setting);
  }
  function defaultGalaxyStellarAccelerationCap(gravity) {
    return defaultGalaxyAccelerationCap(galaxyStellarGravitySetting(gravity));
  }
  function defaultGalaxySystemAccelerationCap(anchor, gravity) {
    return anchor && anchor.anchor_role === 'community'
      ? defaultGalaxyStellarAccelerationCap(gravity)
      : defaultGalaxyAccelerationCap(gravity);
  }
  function galaxyAccelerationCapReference(gravity) {
    const raw = Number(gravity);
    return Number.isFinite(raw)
      ? Math.max(0, Math.min(GALAXY_GRAVITY_CAP_REFERENCE, raw)) : 0;
  }
  function defaultGalaxyAccelerationCap(gravity) {
    const reference = galaxyAccelerationCapReference(gravity);
    return GALAXY_CENTER_ACCELERATION_CAP * galaxyLocalGravityConstant(reference) / 24;
  }
  function defaultGalaxyBlackHoleAccelerationCap(gravity) {
    const reference = galaxyAccelerationCapReference(gravity);
    return GALAXY_CENTER_ACCELERATION_CAP * galaxyBlackHoleGravityConstant(reference) / 24;
  }
  const GALAXY_LINK_DEFAULT = 8;
  const GALAXY_LINK_REFERENCE = 16;
  const GALAXY_LINK_MINIMUM = 4;
  const GALAXY_LINK_MAXIMUM = 80;
  const GALAXY_RELATION_STRENGTH_MULTIPLIER = 2;
  const GALAXY_RELATION_FORCE_CAP = 1.6;
  const GALAXY_RELATION_ACCELERATION_CAP = 3.2;
  const GALAXY_RELATION_CONSTRAINT_STRENGTH_MULTIPLIER = 2;
  const GALAXY_RELATION_CONSTRAINT_RESPONSE_MULTIPLIER = 1;
  const GALAXY_RELATION_CONSTRAINT_RATE = 24;
  /* Position constraints must remain contractive. A larger per-frame displacement cap made
     dense relation hubs snap by a visible distance even after the response itself was bounded.
     Keep the established release cap and one monotone exponential response. */
  const GALAXY_RELATION_CONSTRAINT_MAX_CORRECTION = 12;
  /* A valid inner orbit can be faster than 16 world units at ordinary gravity. Keep the local
     guard at the engine's true emergency ceiling; a lower arbitrary cap makes a circular
     planet sub-orbital and spirals it into the star even though the integrator is stable. */
  const GALAXY_LOCAL_RELATIVE_SPEED_LIMIT = 48;
  /* Preserve headroom below the 48-unit emergency guard while allowing real overview systems
     whose physically sampled circular speed exceeds the retired 10-unit presentation cap to
     visibly orbit the black hole. */
  const GALAXY_SYSTEM_ORBIT_SEED_SPEED_LIMIT = 18;
  const GALAXY_DRAG_GRAVITY_TIME = 6;
  const GALAXY_DRAG_GRAVITY_SOFTENING = 12;
  const GALAXY_DRAG_GRAVITY_MAX_PULL = 36;
  const GALAXY_DRAG_GRAVITY_MAX_IMPULSE = 8;
  const GALAXY_DRAG_GRAVITY_CAPTURE_RADIUS = 180;
  const GALAXY_DRAG_GRAVITY_MULTIPLIER = 2;
  /* Solar systems are not isolated islands. A deliberately weaker mutual field lets nearby
     evidence-heavy systems perturb one another while the dominant black hole remains the
     galaxy-wide potential. Mass and inverse-square distance, rather than graph topology,
     determine this secondary attraction. */
  const GALAXY_MUTUAL_SYSTEM_GRAVITY_FRACTION = 0.12;
  const GALAXY_MUTUAL_SYSTEM_SOFTENING = 80;
  const GALAXY_DRAG_POSITION_MAX_PULL = 2;
  const GALAXY_ORBITAL_SEPARATION_MULTIPLIER = 2;
  /* Link distance is a physical scale, so doubled sensitivity uses the squared response
     (setting/reference)^2. The UI's 4..80 range spans 1/16x through 25x; the shipped setting
     remains 8 (0.25x). Authored star/planet topology is excluded from this constraint so the
     dominant stellar potential still owns orbital radii. */
  function galaxyRelationOrbitScale(setting) {
    const raw = Number(setting);
    const value = Number.isFinite(raw)
      ? Math.max(GALAXY_LINK_MINIMUM, Math.min(GALAXY_LINK_MAXIMUM, raw))
      : GALAXY_LINK_DEFAULT;
    const ratio = value / GALAXY_LINK_REFERENCE;
    return ratio * ratio;
  }
  function galaxyOrbitalSeparationPadding(setting) {
    const raw = Number(setting);
    const value = Number.isFinite(raw) ? Math.max(0, Math.min(120, raw)) : 48;
    /* The old latent cushion was one eighth world unit per slider point. Doubling that
       response makes the control visibly span touching orbits through a 30-unit envelope. */
    return value * 0.125 * GALAXY_ORBITAL_SEPARATION_MULTIPLIER;
  }
  function galaxyOrbitalSeparationStrength(setting) {
    const raw = Number(setting);
    const value = Number.isFinite(raw) ? Math.max(0, Math.min(120, raw)) : 48;
    /* A penetration projection must remain at or below one. Crossing the contact manifold
       reverses the correction on the next frame and reheats dense systems. */
    return Math.min(1, value / 120 * GALAXY_ORBITAL_SEPARATION_MULTIPLIER);
  }
  const GALAXY_LOCAL_PAIR_FRACTION = 0.15;
  const GALAXY_CORE_PAIR_MULTIPLIER = 0.75;
  /* A community's dominant evidence node is its only local gravity well. Its painted edge is
     also a permanent stellar surface: relation constraints and dense layouts may touch it,
     but a satellite can never be placed through the star. This cushion is deliberately not
     slider-controlled; Repel may add more room, never remove the minimum physical surface. */
  const GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING = 1.5;
  /* A short conservative pressure band makes the painted stellar surface a real repulsive
     field instead of relying only on post-step projection. This value is the bounded net-outward
     margin at the hard surface: the live pressure first cancels the sampled stellar attraction,
     then adds this small margin, tapering C1 to zero across the band. The hard exclusion remains
     the exact no-overlap fallback for pathological payloads and pointer teleports. */
  const GALAXY_SYSTEM_ANCHOR_REPULSION_RANGE = 6;
  const GALAXY_SYSTEM_ANCHOR_REPULSION_ACCELERATION = 0.12;
  /* Different solar systems still need a small visual contact pressure when their painted
     nodes touch. Keep it far weaker than local orbital separation so systems can pass close
     without the cross-system slingshots caused by the generic collision force. */
  const GALAXY_CROSS_SYSTEM_REPULSION_FRACTION = 0.18;
  const GALAXY_CROSS_SYSTEM_REPULSION_PADDING = 1.5;
  const GALAXY_BRIDGE_SCALE = 0.35;
  const GALAXY_CENTER_ACCELERATION_CAP = 2.5;
  /* The visible black hole is a contact boundary as well as a gravity source. Its skin must
     exceed one emergency-speed drift (48 * 0.032 = 1.536 world units), so a body cannot
     tunnel through the painted edge between fixed steps. The constraint never adds an outward
     kick; deep corrections preserve angular momentum instead of manufacturing orbital speed. */
  const GALAXY_BLACK_HOLE_EXCLUSION_PADDING = 2.5;
  /* The Plummer well keeps ordinary systems bound, but a finite visual galaxy also needs a
     dormant outer safety field. It starts well outside the seeded scene, adds a smooth
     inward acceleration only near that edge, then applies an exact last-resort boundary if a
     body still escapes. The cached radius never follows an escaped body outward. */
  const GALAXY_FAR_FIELD_ENVELOPE_SCALE = 1.75;
  const GALAXY_FAR_FIELD_MIN_RADIUS = 96;
  const GALAXY_FAR_FIELD_SOFT_FRACTION = 0.82;
  const GALAXY_FAR_FIELD_ACCELERATION = 12;
  const GALAXY_FAR_FIELD_MAX_ACCELERATION = 16;
  /* Frozen compatibility nodes swallow Object.defineProperty, so the far-field cache also
     lives in a WeakMap keyed by anchor identity. The property-based path stays for ordinary
     mutable nodes; the WeakMap wins when the anchor is frozen. */
  const galaxyFarFieldEnvelopeCache = typeof WeakMap === 'function' ? new WeakMap() : null;
  /* Galaxy has its own physical clock. Thirty fixed steps per second bounds main-thread work,
     while a 0.032 leapfrog slice makes both levels of the hierarchy visibly rotate without
     changing their circular initial conditions or force balance. This is a time-scale increase,
     not an extra tangential kick: planets still orbit only their dominant star and whole systems
     still orbit the black hole. Damping removes numerical noise over minutes rather than erasing
     the seeded angular momentum during the opening animation. */
  const GALAXY_FRAME_INTERVAL_MS = 1000 / 30;
  const GALAXY_MOTION_RATE = 0.68;
  const GALAXY_FIXED_TIMESTEP = 0.032;
  const GALAXY_MAX_SUBSTEPS = 3;
  /* Galaxy's fixed-step solver is persistent, so it has no cold alpha to reheat. Extra fixed
     slices would literally fast-forward physical time (up to 3x at a 60 Hz render cadence),
     making every system lurch despite adding no random impulse. Keep the public action and its
     activation telemetry, but let it only wake/reset the ordinary clock; no bonus time enters
     the integrator. */
  const GALAXY_REHEAT_STEPS = 0;
  const GALAXY_REHEAT_LARGE_STEPS = 0;
  const GALAXY_VELOCITY_DECAY = 0.00005;
  /* This is a deliberate external field in the black-hole frame, rather than an
     equal-and-opposite pair force: it makes the visible galaxy contract at a reliable
     wall-clock rate even while orbital forces and drag-derived energy vary. One minute at
     the previous default left 75% of a radius. The motion-rate exponent below now advances
     that same physical trajectory at 68% speed, matching the faster leapfrog clock without
     weakening the force field itself. */
  const GALAXY_INWARD_CONVERGENCE_PER_MINUTE = 0.25;
  const GALAXY_INWARD_CONVERGENCE_SECONDS = 60;
  const GALAXY_OUTWARD_OVERRIDE = 0.10;

  /* Density follows the same effective-G curve as orbital acceleration. Gravity 0 keeps
     the seeded loose radius (while still rejecting outward escape), the default follows
     the former 25%/minute trajectory at 68% speed, and the former 100-setting response
     remains 3.6x while the extended range adds two additional movement spans. This makes the
     full slider visibly control whole-galaxy looseness instead of changing only imperceptible
     acceleration underneath a fixed radial projector. */
  function galaxyInwardConvergencePerMinute(gravitySetting) {
    const setting = gravitySetting === undefined ? 48 : gravitySetting;
    const relativeGravity = galaxyBlackHoleGravityConstant(setting)
      / galaxyBlackHoleGravityConstant(48);
    return 1 - Math.pow(1 - GALAXY_INWARD_CONVERGENCE_PER_MINUTE,
      relativeGravity * GALAXY_MOTION_RATE);
  }

  /* Acceleration alone is intentionally gradual; a range control still needs an immediate,
     legible density response. Map the same black-hole G curve onto a reversible 1.0..0.6
     system-radius scale, then apply only the ratio between the old and new settings. This is
     path-independent across a burst of input events, preserves every solar system's internal
     geometry and velocity, and never wakes D3. Lowering gravity is an explicit user-requested
     loosening action; automatic dynamics remain inward-only. */
  function galaxyImmediateGravityRadiusScale(setting) {
    const maximum = Math.max(1e-9, galaxyBlackHoleGravityConstant(GALAXY_GRAVITY_MAXIMUM));
    const normalized = Math.max(0, Math.min(1,
      galaxyBlackHoleGravityConstant(setting) / maximum));
    return Math.exp(Math.log(0.6) * normalized);
  }

  /* The oversized-scene fallback has no live integrator, so its grid must map the complete
     slider range directly. Keeping the old `setting / 100` scale made compactness hit its
     minimum near 112 and left every higher gravity value visually identical. */
  const GALAXY_LAYOUT_COMPACTNESS_MAXIMUM = 1.75;
  const GALAXY_LAYOUT_COMPACTNESS_MINIMUM = 0.18;
  function galaxyLayoutCompactness(setting) {
    const raw = Number(setting);
    const normalized = Number.isFinite(raw)
      ? Math.max(0, Math.min(1, raw / GALAXY_GRAVITY_MAXIMUM)) : 0;
    return GALAXY_LAYOUT_COMPACTNESS_MAXIMUM
      - (GALAXY_LAYOUT_COMPACTNESS_MAXIMUM - GALAXY_LAYOUT_COMPACTNESS_MINIMUM) * normalized;
  }

  function applyGalaxyGravitySettingResponse(nodes, previousSetting, nextSetting, options) {
    const opts = options || {};
    const previousScale = galaxyImmediateGravityRadiusScale(previousSetting);
    const nextScale = galaxyImmediateGravityRadiusScale(nextSetting);
    const ratio = nextScale / Math.max(1e-9, previousScale);
    const anchor = galaxyGlobalAnchor(nodes);
    if (!anchor || !Number.isFinite(ratio) || Math.abs(ratio - 1) <= 1e-12) {
      return { systems: 0, moved: 0, ratio: Number.isFinite(ratio) ? ratio : 1,
        maximumShift: 0, anchorId: anchor ? anchor.id : null };
    }
    let systems = 0, moved = 0, maximumShift = 0;
    communityCenters(nodes).forEach(center => {
      if (!center || center.nodes.includes(anchor)
        || center.nodes.some(node => node.anchor_role === 'global'
          || node.id === opts.fixedNodeId)) return;
      const dx = center.x - anchor.x, dy = center.y - anchor.y;
      const shiftX = dx * (ratio - 1), shiftY = dy * (ratio - 1);
      if (!Number.isFinite(shiftX) || !Number.isFinite(shiftY)) return;
      center.nodes.forEach(node => {
        node.x += shiftX;
        node.y += shiftY;
        moved++;
      });
      maximumShift = Math.max(maximumShift, Math.hypot(shiftX, shiftY));
      systems++;
    });
    return { systems, moved, ratio, maximumShift, anchorId: anchor.id };
  }

  /* `zoomToFit()` derives its bounds from force-graph's default node geometry rather than
     our custom canvas radius. A compact, nearly-linear graph can therefore produce a 10×+
     fit zoom even though its rendered nodes already fill the canvas. At that scale a normal
     drag maps to a tiny world-space movement and reheating makes the rest of the layout look
     like it is racing away. Keep auto-fit useful without letting its scale become unstable. */
  const MAX_AUTO_FIT_ZOOM = 4;
  const SETTINGS_ALPHA_TARGET = 0.12;
  const ALPHA_TARGET_HOLD_MS = 180;

  /* Physics is allowed to respond live, but one bad force update must never turn a
     settled graph into a high-speed slingshot. Keep the bounds in world units so they
     remain meaningful at every camera zoom. */
  const MIN_NODE_SPEED = 8;
  const MAX_NODE_SPEED = 48;

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

  function hasOwn(value, key) {
    return value != null && Object.prototype.hasOwnProperty.call(value, key);
  }
  function idOf(value) { return value && typeof value === 'object' ? value.id : value; }
  function nodeName(node) {
    if (node === undefined || node === null) return '';
    if (typeof node !== 'object' && typeof node !== 'function') return String(node);
    return String(node.name || node.label || node.id || '');
  }
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
  function finitePositive(value, fallback, ceiling) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return fallback;
    return Math.min(number, ceiling === undefined ? Number.MAX_VALUE : ceiling);
  }
  function communityKey(node) {
    if (node && node.community_id !== undefined && node.community_id !== null) {
      return String(node.community_id);
    }
    return String(node && node.community !== undefined && node.community !== null
      ? node.community : 0);
  }
  function fallbackGravityMass(degree, maxDegree) {
    const normalized = Math.max(0, Math.min(1,
      finitePositive(degree, 0, Number.MAX_VALUE) / Math.max(1, Number(maxDegree) || 1)));
    return 1 + 15 * normalized * normalized;
  }
  function radiusFromGravityMass(mass) {
    return 1.5 + 2 * Math.pow(finitePositive(mass, 1, 1000), 2 / 3);
  }
  /* Scene evidence is the authority in Galaxy mode. Compatibility payloads without mass use
     one deterministic degree fallback; malformed values never inject NaN/Infinity. Radius is
     always derived from the sanitized mass, making visual scale and gravitational pull one
     contract and preventing a bad sibling radius from flattening every later node. */
  function sanitizeEvidenceMetrics(nodes, maxDegree) {
    const values = Array.isArray(nodes) ? nodes : [];
    values.forEach(node => {
      if (node.ghost) {
        node.gravity_mass = 0;
        node.visual_radius = finitePositive(node.visual_radius, 2.5, 64);
        return;
      }
      node.gravity_mass = finitePositive(
        node.gravity_mass, fallbackGravityMass(node.degree, maxDegree), 1000
      );
      /* Radius is a view of mass, never an independent sibling input. Trusting a stale or
         flattened visual_radius made every star identical even when its evidence differed. */
      node.visual_radius = Math.min(64, radiusFromGravityMass(node.gravity_mass));
    });
    return values;
  }
  function evidenceNodeRadius(node, base) {
    const scale = finitePositive(base, 3, 100) / 3;
    if (node && node.cluster) {
      if (node.ghost || !(Number(node.gravity_mass) > 0)) return 2.5 * scale;
      return Math.max(2, Math.min(80 * scale,
        radiusFromGravityMass(node.gravity_mass) * scale));
    }
    const evidenceRadius = Math.max(0.8, Math.min(80 * scale,
      finitePositive(node && node.visual_radius,
        radiusFromGravityMass(node && node.gravity_mass), 64) * scale));
    /* The global evidence anchor is both the physical and visual black hole. Double only its
       rendered/hit radius; gravity_mass remains canonical and community stars retain ordinary
       evidence geometry. Adornments consume node.radius, so their halo follows this scale. */
    return node && !node.ghost && node.anchor_role === 'global'
      ? evidenceRadius * 2 : evidenceRadius;
  }

  function seededHash(seed, value) {
    const text = String(seed === undefined ? 0 : seed) + ':' + String(value);
    let hash = 2166136261;
    for (let i = 0; i < text.length; i++) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }
  function ensureGalaxyPositions(nodes, layoutSeed) {
    const groups = new Map();
    (nodes || []).forEach(node => {
      const key = communityKey(node);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(node);
    });
    [...groups.keys()].sort().forEach((key, groupIndex) => {
      const members = groups.get(key).sort((a, b) => String(a.id).localeCompare(String(b.id)));
      const positioned = members.filter(node => Number.isFinite(node.x) && Number.isFinite(node.y));
      let centerX = 0, centerY = 0;
      if (positioned.length) {
        positioned.forEach(node => { centerX += node.x; centerY += node.y; });
        centerX /= positioned.length;
        centerY /= positioned.length;
      } else if (groups.size > 1) {
        const angle = (seededHash(layoutSeed, key) / 0x100000000) * Math.PI * 2;
        const reach = 90 * Math.sqrt(groupIndex + 1);
        centerX = Math.cos(angle) * reach;
        centerY = Math.sin(angle) * reach;
      }
      members.forEach((node, index) => {
        if (Number.isFinite(node.x) && Number.isFinite(node.y)) return;
        const hash = seededHash(layoutSeed, node.id);
        const angle = (hash / 0x100000000) * Math.PI * 2;
        const orbit = index === 0 ? 0 : 14 + 7 * Math.sqrt(index + 1);
        node.x = centerX + Math.cos(angle) * orbit;
        node.y = centerY + Math.sin(angle) * orbit;
      });
    });
    return nodes;
  }
  function communityCenters(nodes) {
    const centers = new Map();
    (nodes || []).forEach(node => {
      if (node.ghost || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      const mass = finitePositive(node.gravity_mass, 1, 1000);
      const key = communityKey(node);
      let center = centers.get(key);
      if (!center) {
        center = { id: key, mass: 0, x: 0, y: 0, nodes: [] };
        centers.set(key, center);
      }
      center.mass += mass;
      center.x += node.x * mass;
      center.y += node.y * mass;
      center.nodes.push(node);
    });
    centers.forEach(center => {
      if (center.mass > 0) { center.x /= center.mass; center.y /= center.mass; }
    });
    return centers;
  }
  function galaxySystemAnchor(members) {
    const declaredIds = new Set((members || []).map(node => node && node.system_anchor_id)
      .filter(value => value !== undefined && value !== null).map(String));
    return (members || []).slice().sort((left, right) => {
      const leftDeclared = declaredIds.has(String(left.id)) ? 1 : 0;
      const rightDeclared = declaredIds.has(String(right.id)) ? 1 : 0;
      const leftRole = left.anchor_role === 'global' ? 2
        : left.anchor_role === 'community' ? 1 : 0;
      const rightRole = right.anchor_role === 'global' ? 2
        : right.anchor_role === 'community' ? 1 : 0;
      return rightDeclared - leftDeclared || rightRole - leftRole
        || finitePositive(right.gravity_mass, 1, 1000)
          - finitePositive(left.gravity_mass, 1, 1000)
        || String(left.id).localeCompare(String(right.id));
    })[0] || null;
  }
  function orderedGalaxySatellites(members, anchor) {
    return (members || []).filter(node => node !== anchor).map(node => {
      if (!node.__galaxyOrbitOrder) {
        const hint = Number(node.orbit_tier);
        Object.defineProperty(node, '__galaxyOrbitOrder', {
          value: {
            tier: Number.isFinite(hint) ? hint : Number.POSITIVE_INFINITY,
            seedRadius: Math.hypot(node.x - anchor.x, node.y - anchor.y),
          },
          writable: false, configurable: true, enumerable: false,
        });
      }
      return { node, tier: node.__galaxyOrbitOrder.tier,
        radius: node.__galaxyOrbitOrder.seedRadius };
    }).sort((left, right) => left.tier - right.tier || left.radius - right.radius
      || String(left.node.id).localeCompare(String(right.node.id)));
  }
  /* Seed once per node object. The flag is deliberately non-enumerable, so scene export remains
     portable and a fresh setData payload is a fresh seed while reheat/drag/unfreeze are not. */
  function seedGalaxyOrbits(nodes, layoutSeed, gravity, softening, reducedMotion) {
    /* Establish each painted stellar surface before sampling the central field. Otherwise a
       payload that starts a planet inside its star seeds circular speed at an impossible
       radius and immediately converts the later contact correction into eccentric energy. */
    applyGalaxySystemAnchorExclusion(nodes, {
      padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
      fixAnchors: true,
    });
    const centers = communityCenters(nodes);
    const epsilon = Math.max(0.1, Number(softening) || 8);
    /* Seed from the satellite's dominant-star attraction only. Aggregate star recoil contains
       the summed pull of every planet; projecting that aggregate onto one planet's radial axis
       can point outward in a dense/asymmetric system and incorrectly seed zero angular motion.
       Other satellites and the near-surface pressure are perturbations for the live integrator,
       not independent local wells or inputs to a planet's circular initial condition. */
    const freshlySeeded = new Map();
    (nodes || []).forEach(node => {
      if (node.__galaxyOrbitSeeded) return;
      Object.defineProperty(node, '__galaxyOrbitSeeded', {
        value: true, writable: true, configurable: true, enumerable: false
      });
      node.vx = Number.isFinite(node.vx) ? node.vx : 0;
      node.vy = Number.isFinite(node.vy) ? node.vy : 0;
      if (node.ghost) {
        node.vx = 0;
        node.vy = 0;
        return;
      }
      /* Reduced motion suppresses cosmetic particles and animated camera travel; it does not
         switch the persistent Galaxy solver to a radial-only physical model. The clock remains
         active under that preference, so omitting this one-shot angular seed makes every planet
         fall straight into its dominant star. Freeze/static layout are the no-physics controls. */
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      const key = communityKey(node);
      if (!freshlySeeded.has(key)) freshlySeeded.set(key, []);
      freshlySeeded.get(key).push(node);
    });
    /* Seed satellites around the evidence-heaviest star from that one dominant attraction.
       A late reveal is expressed in the star's already-moving frame. Its added momentum is
       balanced by one common recoil translation across the existing free system, preserving all
       established relative phases while keeping the new star-relative tangent exact. */
    freshlySeeded.forEach((members, key) => {
      const center = centers.get(key);
      if (!center || center.nodes.length < 2) return;
      const anchor = galaxySystemAnchor(center.nodes);
      const localGravity = galaxySystemGravityConstant(anchor, gravity);
      const localAccelerationCap = defaultGalaxySystemAccelerationCap(anchor, gravity);
      const anchorMass = finitePositive(anchor.gravity_mass, 1, 1000);
      const anchorVx = Number.isFinite(anchor.vx) ? anchor.vx : 0;
      const anchorVy = Number.isFinite(anchor.vy) ? anchor.vy : 0;
      const direction = (seededHash(layoutSeed, 'system:' + key) & 1) ? 1 : -1;
      const desiredVelocity = new Map(members.map(node => [node, {
        vx: Number.isFinite(node.vx) ? node.vx : 0,
        vy: Number.isFinite(node.vy) ? node.vy : 0,
      }]));
      orderedGalaxySatellites(center.nodes, anchor).forEach(item => {
        if (!members.includes(item.node)) return;
        const dx = item.node.x - anchor.x, dy = item.node.y - anchor.y;
        /* Contact projection may have moved this phase after its deterministic sort key was
           cached. Ordering stays seeded; circular speed must use the repaired live radius. */
        const currentRadius = Math.hypot(dx, dy);
        if (currentRadius <= 1e-9) return;
        const denominator = Math.pow(
          currentRadius * currentRadius + epsilon * epsilon, 1.5);
        const rawInwardAcceleration = denominator > 0
          ? localGravity * anchorMass * currentRadius / denominator : 0;
        const inwardAcceleration = localAccelerationCap > 0
          ? Math.min(localAccelerationCap, rawInwardAcceleration) : rawInwardAcceleration;
        const omega = Math.sqrt(inwardAcceleration / currentRadius);
        desiredVelocity.set(item.node, {
          vx: anchorVx - dy * omega * direction,
          vy: anchorVy + dx * omega * direction,
        });
      });
      const fresh = new Set(members);
      const existing = center.nodes.filter(node => !fresh.has(node));
      const externallyFixed = anchor.anchor_role === 'global' || center.nodes.some(node =>
        Number.isFinite(node.fx) || Number.isFinite(node.fy));
      if (externallyFixed) {
        /* A pinned/global star is an external momentum reservoir. Recoiling its established
           system would fight pointer restoration or black-hole recentering every tick. */
        desiredVelocity.forEach((velocity, node) => {
          node.vx = velocity.vx;
          node.vy = velocity.vy;
        });
        return;
      }
      let totalMass = 0, addedMomentumX = 0, addedMomentumY = 0;
      center.nodes.forEach(node => {
        totalMass += finitePositive(node.gravity_mass, 1, 1000);
      });
      members.forEach(node => {
        const nodeMass = finitePositive(node.gravity_mass, 1, 1000);
        const desired = desiredVelocity.get(node);
        addedMomentumX += nodeMass * (desired.vx
          - (Number.isFinite(node.vx) ? node.vx : 0));
        addedMomentumY += nodeMass * (desired.vy
          - (Number.isFinite(node.vy) ? node.vy : 0));
      });
      if (!(totalMass > 0)) return;
      const recoilX = -addedMomentumX / totalMass;
      const recoilY = -addedMomentumY / totalMass;
      existing.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + recoilX;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + recoilY;
      });
      desiredVelocity.forEach((velocity, node) => {
        node.vx = velocity.vx + recoilX;
        node.vy = velocity.vy + recoilY;
      });
    });
    return nodes;
  }

  /* Give whole solar systems one-shot angular momentum around the global evidence anchor.
     Each system follows the composite black-hole field with a bounded eccentric perturbation;
     reheat, unfreeze, and manual placement never inject a second orbital sweep. */
  function seedGalaxySystemOrbits(nodes, layoutSeed, gravity, softening, reducedMotion) {
    const centers = [...communityCenters(nodes).values()];
    const gravitationalConstant = galaxyBlackHoleGravityConstant(gravity);
    const direction = (seededHash(layoutSeed, 'galaxy-spin') & 1) ? 1 : -1;
    const freshBySystem = new Map();
    const hadSeededSystem = centers.some(center => center.nodes.some(
      node => node.__galaxySystemOrbitSeeded
    ));
    centers.forEach(center => {
      const fresh = center.nodes.filter(node => !node.__galaxySystemOrbitSeeded);
      fresh.forEach(node => {
        node.vx = Number.isFinite(node.vx) ? node.vx : 0;
        node.vy = Number.isFinite(node.vy) ? node.vy : 0;
        Object.defineProperty(node, '__galaxySystemOrbitSeeded', {
          value: true, writable: true, configurable: true, enumerable: false
        });
      });
      if (fresh.length) freshBySystem.set(center.id, fresh);
    });
    /* The barycentric sweep is a scene-level initial condition, not a per-node decoration.
       Scope/filter changes reuse node objects; sweeping only newly revealed systems would add
       momentum without counter-motion from the already seeded galaxy. Mark late arrivals but
       let ordinary gravity settle them instead of injecting a partial second initial condition. */
    /* Reduced motion is a paint/camera preference. The live solver still advances, so it must
       receive the same barycentric initial condition or whole systems contract radially without
       rotating around the black hole. */
    if (hadSeededSystem || gravitationalConstant <= 0 || centers.length < 2) {
      return nodes;
    }

    /* Use the same smooth black-hole field as the integrator, then add a small deterministic
       eccentric/radial perturbation. Systems are bound but not painted onto a rigid circular
       carousel; inner angular frequency remains higher than outer angular frequency. */
    const field = galaxyBlackHoleField(nodes, {
      gravity, softening,
    });
    field.systems.forEach(item => {
      const fresh = freshBySystem.get(item.center.id) || [];
      if (!fresh.length || item.radius <= 1e-9) return;
      const outwardX = -item.dx / item.radius, outwardY = -item.dy / item.radius;
      const tangentX = -outwardY * direction, tangentY = outwardX * direction;
      const tangentFactor = 0.92
        + (seededHash(layoutSeed, 'system-speed:' + item.center.id) / 0x100000000) * 0.12;
      /* Start every system on a gentle settling spiral. A symmetric +/- phase can launch an
         outer system away from the well before gravity turns it around; a bounded inward kick
         gives the black-hole centre first claim on motion while preserving tangential rotation. */
      const radialFactor = -0.04
        + (seededHash(layoutSeed, 'system-radial:' + item.center.id) / 0x100000000) * 0.02;
      const speed = Math.min(GALAXY_SYSTEM_ORBIT_SEED_SPEED_LIMIT, item.circularSpeed);
      const kick = {
        vx: tangentX * speed * tangentFactor + outwardX * speed * radialFactor,
        vy: tangentY * speed * tangentFactor + outwardY * speed * radialFactor,
      };
      fresh.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + kick.vx;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + kick.vy;
      });
    });
    /* A uniform translation changes no orbit. Remove the seeded galaxy's residual momentum;
       the integrator will subsequently express the same relative phase in the anchor frame. */
    let freshMass = 0, momentumX = 0, momentumY = 0;
    freshBySystem.forEach(fresh => fresh.forEach(node => {
      const mass = finitePositive(node.gravity_mass, 1, 1000);
      freshMass += mass;
      momentumX += mass * (Number.isFinite(node.vx) ? node.vx : 0);
      momentumY += mass * (Number.isFinite(node.vy) ? node.vy : 0);
    }));
    if (freshMass > 0) freshBySystem.forEach(fresh => fresh.forEach(node => {
      node.vx -= momentumX / freshMass;
      node.vy -= momentumY / freshMass;
    }));
    return nodes;
  }

  function addGravityPair(left, right, gravitationalConstant, softening, alphaValue) {
    const dx = right.x - left.x, dy = right.y - left.y;
    const distanceSquared = dx * dx + dy * dy;
    const denominator = Math.pow(distanceSquared + softening * softening, 1.5);
    if (!Number.isFinite(denominator) || denominator <= 0) return;
    const scale = gravitationalConstant * alphaValue / denominator;
    const leftMass = finitePositive(left.gravity_mass, 1, 1000);
    const rightMass = finitePositive(right.gravity_mass, 1, 1000);
    left.vx = (Number.isFinite(left.vx) ? left.vx : 0) + scale * rightMass * dx;
    left.vy = (Number.isFinite(left.vy) ? left.vy : 0) + scale * rightMass * dy;
    right.vx = (Number.isFinite(right.vx) ? right.vx : 0) - scale * leftMass * dx;
    right.vy = (Number.isFinite(right.vy) ? right.vy : 0) - scale * leftMass * dy;
  }

  function buildGravityQuad(nodes, x, y, size, depth) {
    const quad = { x, y, size, mass: 0, cx: 0, cy: 0, bodies: null, children: null };
    nodes.forEach(node => {
      const mass = finitePositive(node.gravity_mass, 1, 1000);
      quad.mass += mass;
      quad.cx += node.x * mass;
      quad.cy += node.y * mass;
    });
    if (quad.mass) { quad.cx /= quad.mass; quad.cy /= quad.mass; }
    if (nodes.length <= 1 || depth >= 24 || size <= 1e-7) {
      quad.bodies = nodes;
      return quad;
    }
    const half = size / 2, midX = x + half, midY = y + half;
    const buckets = [[], [], [], []];
    nodes.forEach(node => {
      const index = (node.x >= midX ? 1 : 0) + (node.y >= midY ? 2 : 0);
      buckets[index].push(node);
    });
    const childBoxes = [
      [x, y], [midX, y], [x, midY], [midX, midY]
    ];
    quad.children = [];
    buckets.forEach((bucket, index) => {
      if (bucket.length) quad.children.push(buildGravityQuad(
        bucket, childBoxes[index][0], childBoxes[index][1], half, depth + 1
      ));
    });
    return quad;
  }
  function gravityQuad(nodes) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach(node => {
      minX = Math.min(minX, node.x); minY = Math.min(minY, node.y);
      maxX = Math.max(maxX, node.x); maxY = Math.max(maxY, node.y);
    });
    const size = Math.max(1e-6, maxX - minX, maxY - minY) * 1.000001;
    return buildGravityQuad(nodes, minX, minY, size, 0);
  }
  function applyQuadGravity(target, quad, gravitationalConstant, softening, alphaValue, theta, stats) {
    stats.traversals++;
    if (quad.bodies) {
      quad.bodies.forEach(source => {
        if (source === target) return;
        const proxy = { x: source.x, y: source.y, gravity_mass: source.gravity_mass, vx: 0, vy: 0 };
        addGravityPair(target, proxy, gravitationalConstant, softening, alphaValue);
        stats.interactions++;
      });
      return;
    }
    const dx = quad.cx - target.x, dy = quad.cy - target.y;
    const distance = Math.hypot(dx, dy);
    const containsTarget = target.x >= quad.x && target.x < quad.x + quad.size
      && target.y >= quad.y && target.y < quad.y + quad.size;
    if (!containsTarget && distance > 0 && quad.size / distance < theta) {
      const denominator = Math.pow(dx * dx + dy * dy + softening * softening, 1.5);
      const scale = gravitationalConstant * alphaValue * quad.mass / denominator;
      target.vx = (Number.isFinite(target.vx) ? target.vx : 0) + scale * dx;
      target.vy = (Number.isFinite(target.vy) ? target.vy : 0) + scale * dy;
      stats.approximations++;
      return;
    }
    quad.children.forEach(child => applyQuadGravity(
      target, child, gravitationalConstant, softening, alphaValue, theta, stats
    ));
  }
  function applyGalaxyGravity(nodes, options) {
    const opts = options || {};
    const active = (nodes || []).filter(node => !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const groups = new Map();
    active.forEach(node => {
      const key = communityKey(node);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(node);
    });
    const explicitGravity = Number(opts.effectiveGravity);
    const gravitationalConstant = Number.isFinite(explicitGravity) && explicitGravity >= 0
      ? explicitGravity : galaxyLocalGravityConstant(opts.gravity);
    const pairFraction = Math.max(0, Math.min(1,
      Number.isFinite(Number(opts.pairFraction)) ? Number(opts.pairFraction) : 1));
    const corePairFraction = Math.max(0, Math.min(1,
      Number.isFinite(Number(opts.corePairFraction)) ? Number(opts.corePairFraction)
        : pairFraction));
    const coreCommunity = opts.coreCommunity === undefined || opts.coreCommunity === null
      ? null : String(opts.coreCommunity);
    const softening = Math.max(0.1, Number(opts.softening) || 8);
    const alphaValue = Number.isFinite(opts.alpha) ? Math.max(0, opts.alpha) : 1;
    const exactLimit = Math.max(2, Number(opts.exactLimit) || GALAXY_EXACT_LIMIT);
    const theta = Math.max(0.1, Number(opts.theta) || GALAXY_BARNES_HUT_THETA);
    const stats = { communities: groups.size, interactions: 0, traversals: 0, approximations: 0 };
    groups.forEach((group, key) => {
      const groupGravity = gravitationalConstant
        * (coreCommunity !== null && key === coreCommunity
          ? corePairFraction : pairFraction);
      if (group.length <= exactLimit) {
        for (let i = 0; i < group.length; i++) {
          for (let j = i + 1; j < group.length; j++) {
            addGravityPair(group[i], group[j], groupGravity, softening, alphaValue);
            stats.interactions++;
          }
        }
        return;
      }
      const quad = gravityQuad(group);
      let groupMass = 0, momentumBeforeX = 0, momentumBeforeY = 0;
      group.forEach(node => {
        const mass = finitePositive(node.gravity_mass, 1, 1000);
        groupMass += mass;
        momentumBeforeX += mass * (Number.isFinite(node.vx) ? node.vx : 0);
        momentumBeforeY += mass * (Number.isFinite(node.vy) ? node.vy : 0);
      });
      group.forEach(node => applyQuadGravity(
        node, quad, groupGravity, softening, alphaValue, theta, stats
      ));
      /* Barnes-Hut approximates each target separately, so its truncation error can create a
         tiny net force. Remove only that shared reference-frame drift; relative acceleration
         and the internal orbit are unchanged. Exact pair communities need no correction. */
      if (groupMass > 0) {
        let momentumAfterX = 0, momentumAfterY = 0;
        group.forEach(node => {
          const mass = finitePositive(node.gravity_mass, 1, 1000);
          momentumAfterX += mass * node.vx;
          momentumAfterY += mass * node.vy;
        });
        const driftX = (momentumAfterX - momentumBeforeX) / groupMass;
        const driftY = (momentumAfterY - momentumBeforeY) / groupMass;
        group.forEach(node => {
          node.vx -= driftX;
          node.vy -= driftY;
        });
      }
    });
    return stats;
  }

  /* Most of a solar system's field is a smooth Plummer halo rather than repeated close stellar
     encounters. Every satellite sees the total evidence mass of its community; subtracting the
     mass-weighted mean from a free system preserves its COM without changing any relative
     acceleration. A small direct-pair fraction remains for organic multi-star perturbations. */
  function applyGalaxySystemHaloGravity(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const groups = new Map();
    bodies.forEach(node => {
      const key = communityKey(node);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(node);
    });
    const gravity = galaxyLocalGravityConstant(opts.gravity);
    const smoothFraction = Math.max(0, Math.min(1,
      Number.isFinite(Number(opts.smoothFraction)) ? Number(opts.smoothFraction) : 0.85));
    const coreSmoothFraction = Math.max(0, Math.min(1,
      Number.isFinite(Number(opts.coreSmoothFraction)) ? Number(opts.coreSmoothFraction)
        : smoothFraction));
    const coreCommunity = opts.coreCommunity === undefined || opts.coreCommunity === null
      ? null : String(opts.coreCommunity);
    const alphaValue = Number.isFinite(opts.alpha) ? Math.max(0, opts.alpha) : 1;
    const softening = Math.max(0.1, Number(opts.softening) || 8);
    const stats = { communities: groups.size, satellites: 0 };
    if (gravity <= 0 || Math.max(smoothFraction, coreSmoothFraction) <= 0
      || alphaValue <= 0) return stats;
    groups.forEach((members, key) => {
      if (members.length < 2) return;
      const anchor = galaxySystemAnchor(members);
      const pinnedAnchor = anchor.anchor_role === 'global';
      const groupSmoothFraction = coreCommunity !== null && key === coreCommunity
        ? coreSmoothFraction : smoothFraction;
      const communityMass = members.reduce((sum, node) => sum
        + finitePositive(node.gravity_mass, 1, 1000), 0);
      const accelerations = new Map(members.map(node => [node, { ax: 0, ay: 0 }]));
      orderedGalaxySatellites(members, anchor).forEach(item => {
        const dx = anchor.x - item.node.x, dy = anchor.y - item.node.y;
        const denominator = Math.pow(
          dx * dx + dy * dy + softening * softening, 1.5
        );
        if (Number.isFinite(denominator) && denominator > 0) {
          const scale = gravity * groupSmoothFraction * alphaValue
            * communityMass / denominator;
          const acceleration = accelerations.get(item.node);
          acceleration.ax += dx * scale;
          acceleration.ay += dy * scale;
          stats.satellites++;
        }
      });
      let totalMass = 0, driftX = 0, driftY = 0;
      members.forEach(node => {
        const mass = finitePositive(node.gravity_mass, 1, 1000);
        const acceleration = accelerations.get(node);
        totalMass += mass;
        driftX += mass * acceleration.ax;
        driftY += mass * acceleration.ay;
      });
      if (!pinnedAnchor && totalMass > 0) { driftX /= totalMass; driftY /= totalMass; }
      else { driftX = 0; driftY = 0; }
      const accelerationCap = Math.max(0, Number.isFinite(Number(opts.accelerationCap))
        ? Number(opts.accelerationCap) : defaultGalaxyAccelerationCap(opts.gravity));
      const maximumAcceleration = members.reduce((maximum, node) => {
        const acceleration = accelerations.get(node);
        return Math.max(maximum,
          Math.hypot(acceleration.ax - driftX, acceleration.ay - driftY));
      }, 0);
      const capScale = accelerationCap > 0 && maximumAcceleration > accelerationCap
        ? accelerationCap / maximumAcceleration : 1;
      members.forEach(node => {
        const acceleration = accelerations.get(node);
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0)
          + (acceleration.ax - driftX) * capScale;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0)
          + (acceleration.ay - driftY) * capScale;
      });
    });
    return stats;
  }
  /* Compatibility name for embedders that exercised the experimental enclosed-mass helper. */
  const applyGalaxyEnclosedSystemGravity = applyGalaxySystemHaloGravity;

  /* Hierarchical local gravity. A real solar system is not an all-to-all attraction graph:
     one dominant star supplies the central well and the smaller bodies orbit that source.
     The declared system anchor/role wins; compatibility scenes fall back to evidence mass
     (which already has the deterministic degree-derived fallback). Satellites never become
     independent wells, so a dense community cannot scramble itself through planet-to-planet
     gravity. One shared barycentric frame correction preserves each free system's momentum and
     every planet's central relative acceleration, while the galaxy-wide black-hole acceleration
     is added later as one shared acceleration per system. */
  function applyGalaxySystemAnchorGravity(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const groups = new Map();
    bodies.forEach(node => {
      const key = communityKey(node);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(node);
    });
    const softening = Math.max(0.1, Number(opts.softening) || 8);
    const alphaValue = Number.isFinite(opts.alpha) ? Math.max(0, opts.alpha) : 1;
    const explicitAccelerationCap = Number.isFinite(Number(opts.accelerationCap))
      ? Math.max(0, Number(opts.accelerationCap)) : null;
    const repulsionPadding = Math.max(0, Number.isFinite(Number(opts.repulsionPadding))
      ? Number(opts.repulsionPadding) : GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING);
    const repulsionRange = Math.max(0.1, Number.isFinite(Number(opts.repulsionRange))
      ? Number(opts.repulsionRange) : GALAXY_SYSTEM_ANCHOR_REPULSION_RANGE);
    const repulsionAcceleration = Math.max(0,
      Number.isFinite(Number(opts.repulsionAcceleration))
        ? Number(opts.repulsionAcceleration) : GALAXY_SYSTEM_ANCHOR_REPULSION_ACCELERATION);
    const bodyRadius = node => finitePositive(
      node.radius, finitePositive(node.visual_radius,
        radiusFromGravityMass(node.gravity_mass), 80), 160
    );
    const stats = {
      systems: groups.size, anchors: 0, satellites: 0,
      repulsions: 0, surfaceRepulsions: 0,
      maximumRepulsion: 0, maximumSampledAttraction: 0, maximumNetRepulsion: 0,
      minimumSurfaceNetRepulsion: null,
      repulsionPadding, repulsionRange, repulsionAcceleration,
      maximumAcceleration: 0, capScale: 1,
      gravitySetting: galaxyAccelerationCapReference(opts.gravity),
      stellarGravityFloorSetting: GALAXY_STELLAR_GRAVITY_FLOOR_SETTING,
      stellarGravity: galaxyStellarGravityConstant(opts.gravity),
      eligibleStellarAnchors: 0, fallbackAnchors: 0, globalAnchors: 0,
      stellarFloorActive: false,
    };
    if (!(alphaValue > 0)) return stats;
    groups.forEach(members => {
      if (members.length < 2) return;
      const anchor = galaxySystemAnchor(members);
      if (!anchor) return;
      stats.anchors++;
      if (anchor.anchor_role === 'community') {
        stats.eligibleStellarAnchors++;
        if (galaxyStellarGravitySetting(opts.gravity)
          > galaxyAccelerationCapReference(opts.gravity)) stats.stellarFloorActive = true;
      } else if (anchor.anchor_role === 'global') stats.globalAnchors++;
      else stats.fallbackAnchors++;
      const gravity = galaxySystemGravityConstant(anchor, opts.gravity);
      const accelerationCap = explicitAccelerationCap !== null
        ? explicitAccelerationCap : defaultGalaxySystemAccelerationCap(anchor, opts.gravity);
      const anchorMass = finitePositive(anchor.gravity_mass, 1, 1000);
      const accelerations = new Map(members.map(node => [node, { ax: 0, ay: 0 }]));
      let systemMaximumRepulsion = 0, systemMaximumSampledAttraction = 0;
      let systemMaximumNetRepulsion = 0, systemMinimumSurfaceNetRepulsion = null;
      orderedGalaxySatellites(members, anchor).forEach(item => {
        const satellite = item.node;
        let dx = anchor.x - satellite.x, dy = anchor.y - satellite.y;
        let distance = Math.hypot(dx, dy);
        if (!(distance > 1e-9)) {
          const angle = seededHash(0, 'stellar-pressure:' + String(anchor.id)
            + '|' + String(satellite.id)) / 0x100000000 * Math.PI * 2;
          /* dx/dy point toward the star. A deterministic opposite normal therefore sends an
             exactly coincident satellite outward without inventing a random orbital phase. */
          dx = -Math.cos(angle) * 1e-9;
          dy = -Math.sin(angle) * 1e-9;
          distance = 1e-9;
        }
        const denominator = Math.pow(dx * dx + dy * dy + softening * softening, 1.5);
        if (!(denominator > 0) || !Number.isFinite(denominator)) return;
        const scale = gravity * alphaValue / denominator;
        const sampledAttraction = distance * scale * anchorMass;
        const satelliteAcceleration = accelerations.get(satellite);
        satelliteAcceleration.ax += dx * scale * anchorMass;
        satelliteAcceleration.ay += dy * scale * anchorMass;
        /* Only the selected dominant star owns this near-surface pressure. At and inside the
           painted surface it cancels this exact softened attraction and leaves the configured
           small net-outward margin. Across the outer six-unit band the whole pressure tapers C1
           to zero, so there is no discontinuous kick or gravity-400 energy spike. Satellites
           never become secondary wells; the global anchor retains its separate event horizon. */
        if (anchor.anchor_role !== 'global' && repulsionAcceleration > 0) {
          const surfaceDistance = bodyRadius(anchor) + bodyRadius(satellite)
            + repulsionPadding;
          const pressureEdge = surfaceDistance + repulsionRange;
          if (distance < pressureEdge) {
            const depth = galaxySmoothstep((pressureEdge - distance) / repulsionRange);
            const outwardAcceleration = (sampledAttraction
              + repulsionAcceleration * alphaValue) * depth;
            const netRepulsion = outwardAcceleration - sampledAttraction;
            const unitX = dx / distance, unitY = dy / distance;
            satelliteAcceleration.ax -= unitX * outwardAcceleration;
            satelliteAcceleration.ay -= unitY * outwardAcceleration;
            stats.repulsions++;
            systemMaximumRepulsion = Math.max(systemMaximumRepulsion, outwardAcceleration);
            systemMaximumSampledAttraction = Math.max(
              systemMaximumSampledAttraction, sampledAttraction);
            systemMaximumNetRepulsion = Math.max(systemMaximumNetRepulsion, netRepulsion);
            if (distance <= surfaceDistance + 1e-9) {
              stats.surfaceRepulsions++;
              systemMinimumSurfaceNetRepulsion = systemMinimumSurfaceNetRepulsion === null
                ? netRepulsion : Math.min(systemMinimumSurfaceNetRepulsion, netRepulsion);
            }
          }
        }
        stats.satellites++;
      });
      /* Express every central acceleration in one freely falling system frame. Assigning each
         pair's reaction directly to the star makes its acceleration the vector sum of all
         planets; subtracting that aggregate from each planet can invert a dense planet's radial
         force. One shared mass-weighted drift preserves total momentum while cancelling from
         every planet-minus-star relative acceleration. The global black hole and a pointer-owned
         system are external pinned frames, so their members keep the unshifted source field. */
      const systemFixed = opts.fixedNodeId != null
        && members.some(node => node.id === opts.fixedNodeId);
      if (anchor.anchor_role !== 'global' && !systemFixed) {
        const totalMass = members.reduce(
          (sum, node) => sum + finitePositive(node.gravity_mass, 1, 1000), 0);
        let driftX = 0, driftY = 0;
        members.forEach(node => {
          const nodeMass = finitePositive(node.gravity_mass, 1, 1000);
          const acceleration = accelerations.get(node);
          driftX += nodeMass * acceleration.ax;
          driftY += nodeMass * acceleration.ay;
        });
        driftX /= totalMass;
        driftY /= totalMass;
        members.forEach(node => {
          const acceleration = accelerations.get(node);
          acceleration.ax -= driftX;
          acceleration.ay -= driftY;
        });
      }
      const maximum = members.reduce((value, node) => {
        const acceleration = accelerations.get(node);
        return Math.max(value, Math.hypot(acceleration.ax, acceleration.ay));
      }, 0);
      const scale = accelerationCap > 0 && maximum > accelerationCap
        ? accelerationCap / maximum : 1;
      stats.maximumAcceleration = Math.max(stats.maximumAcceleration, maximum * scale);
      stats.maximumRepulsion = Math.max(
        stats.maximumRepulsion, systemMaximumRepulsion * scale);
      stats.maximumSampledAttraction = Math.max(
        stats.maximumSampledAttraction, systemMaximumSampledAttraction * scale);
      stats.maximumNetRepulsion = Math.max(
        stats.maximumNetRepulsion, systemMaximumNetRepulsion * scale);
      if (systemMinimumSurfaceNetRepulsion !== null) {
        const boundedSurfaceNet = systemMinimumSurfaceNetRepulsion * scale;
        stats.minimumSurfaceNetRepulsion = stats.minimumSurfaceNetRepulsion === null
          ? boundedSurfaceNet : Math.min(stats.minimumSurfaceNetRepulsion, boundedSurfaceNet);
      }
      stats.capScale = Math.min(stats.capScale, scale);
      members.forEach(node => {
        const acceleration = accelerations.get(node);
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + acceleration.ax * scale;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + acceleration.ay * scale;
      });
    });
    return stats;
  }

  /* Permanent stellar-surface contact for each community's dominant node. Projection is
     radial and bounded to the exact painted edge; velocity response removes only inward
     normal motion in the star frame. Tangential velocity is untouched, so contact cannot
     drain orbital phase or manufacture a repulsive slingshot. The global anchor has its own
     stricter black-hole horizon and is intentionally excluded here. */
  function applyGalaxySystemAnchorExclusion(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const groups = new Map();
    bodies.forEach(node => {
      const key = communityKey(node);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(node);
    });
    const padding = Math.max(0, Number.isFinite(Number(opts.padding))
      ? Number(opts.padding) : GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING);
    const maximumIterations = Math.max(1, Math.min(64,
      Number.isFinite(Number(opts.maximumIterations))
        ? Math.floor(Number(opts.maximumIterations)) : 24));
    const clearanceEpsilon = Math.max(1e-12,
      Number.isFinite(Number(opts.clearanceEpsilon))
        ? Number(opts.clearanceEpsilon) : 1e-9);
    const bodyRadius = node => finitePositive(
      node.radius, finitePositive(node.visual_radius,
        radiusFromGravityMass(node.gravity_mass), 80), 160
    );
    const stats = {
      padding,
      systems: 0, contacts: 0, correctedDistance: 0, maximumShift: 0,
      inwardVelocityRemoved: 0, tangentialVelocityRemoved: 0,
      minimumClearance: null, iterations: 0,
    };
    groups.forEach(members => {
      if (members.length < 2) return;
      const anchor = galaxySystemAnchor(members);
      if (!anchor || anchor.anchor_role === 'global') return;
      stats.systems++;
      const anchorRadius = bodyRadius(anchor);
      const anchorMass = finitePositive(anchor.gravity_mass, 1, 1000);
      /* Pointer ownership fixes the local reference frame for the whole gesture. If any member
         is restored from pointer coordinates each tick, letting contact recoil the star would
         repeat that correction indefinitely (the same positive feedback as the former
         black-hole drag bug). The penetrating satellite is still projected to the edge. */
      const anchorFixed = opts.fixAnchors === true
        || members.some(node => node.id === opts.fixedNodeId);
      const satellites = orderedGalaxySatellites(members, anchor);
      /* A bounded convergent solve closes the clearance reopened when a free star recoils from
         a later satellite. Real overview payloads can place 80+ bodies around one dominant
         node, so six passes are not sufficient. Ordinary non-contact systems still exit after
         one O(n) scan; pathological dense payloads receive up to 24 passes, stopping as soon as
         every remaining penetration is at floating-point tolerance. Every pair projection
         independently preserves the free-system COM and momentum. */
      for (let iteration = 0; iteration < maximumIterations; iteration++) {
        let corrected = false;
        let maximumPenetration = 0;
        satellites.forEach(item => {
          const satellite = item.node;
          const minimumDistance = anchorRadius + bodyRadius(satellite) + padding;
          let dx = satellite.x - anchor.x, dy = satellite.y - anchor.y;
          let distance = Math.hypot(dx, dy);
          let unitX, unitY;
          if (distance > 1e-9) {
            unitX = dx / distance;
            unitY = dy / distance;
          } else {
            const angle = seededHash(0, String(anchor.id) + '|' + String(satellite.id))
              / 0x100000000 * Math.PI * 2;
            unitX = Math.cos(angle);
            unitY = Math.sin(angle);
            distance = 0;
          }
          const penetration = minimumDistance - distance;
          if (penetration <= clearanceEpsilon) return;
          corrected = true;
          maximumPenetration = Math.max(maximumPenetration, penetration);
          const correction = penetration;
          const satelliteMass = finitePositive(satellite.gravity_mass, 1, 1000);
          const anchorInverseMass = anchorFixed ? 0 : 1 / anchorMass;
          const satelliteInverseMass = 1 / satelliteMass;
          const inverseMass = anchorInverseMass + satelliteInverseMass;
          const projection = correction / inverseMass;
          const anchorShift = projection * anchorInverseMass;
          const satelliteShift = projection * satelliteInverseMass;
          anchor.x -= unitX * anchorShift;
          anchor.y -= unitY * anchorShift;
          satellite.x += unitX * satelliteShift;
          satellite.y += unitY * satelliteShift;
          if (Number.isFinite(anchor.fx)) anchor.fx = anchor.x;
          if (Number.isFinite(anchor.fy)) anchor.fy = anchor.y;
          if (Number.isFinite(satellite.fx)) satellite.fx = satellite.x;
          if (Number.isFinite(satellite.fy)) satellite.fy = satellite.y;
          const relativeVx = (Number.isFinite(satellite.vx) ? satellite.vx : 0)
            - (Number.isFinite(anchor.vx) ? anchor.vx : 0);
          const relativeVy = (Number.isFinite(satellite.vy) ? satellite.vy : 0)
            - (Number.isFinite(anchor.vy) ? anchor.vy : 0);
          const inwardSpeed = relativeVx * unitX + relativeVy * unitY;
          if (inwardSpeed < 0) {
            const impulse = -inwardSpeed / inverseMass;
            anchor.vx -= unitX * impulse * anchorInverseMass;
            anchor.vy -= unitY * impulse * anchorInverseMass;
            satellite.vx += unitX * impulse * satelliteInverseMass;
            satellite.vy += unitY * impulse * satelliteInverseMass;
            stats.inwardVelocityRemoved += -inwardSpeed;
          }
          stats.contacts++;
          stats.correctedDistance += correction;
          stats.maximumShift = Math.max(stats.maximumShift, anchorShift, satelliteShift);
        });
        stats.iterations = Math.max(stats.iterations, iteration + 1);
        if (!corrected) break;
        if (maximumPenetration <= clearanceEpsilon) break;
      }
      satellites.forEach(item => {
        const minimumDistance = anchorRadius + bodyRadius(item.node) + padding;
        const rawClearance = Math.hypot(item.node.x - anchor.x, item.node.y - anchor.y)
          - minimumDistance;
        /* Avoid reporting harmless binary rounding as an overlap. The actual phase remains
           within the same 1e-9 solver tolerance; larger residuals are never hidden. */
        const clearance = rawClearance >= -clearanceEpsilon ? Math.max(0, rawClearance)
          : rawClearance;
        stats.minimumClearance = stats.minimumClearance === null
          ? clearance : Math.min(stats.minimumClearance, clearance);
      });
    });
    return stats;
  }

  /* Read-only final audit for the composite black-hole/outer-wall/stellar closure. Keeping the
     measurement separate from projection prevents diagnostics from claiming the pre-annulus
     clearance after a member-wise outer clamp has moved a planet back through its star. */
  function galaxySystemAnchorClearance(nodes, options) {
    const opts = options || {};
    const padding = Math.max(0, Number.isFinite(Number(opts.padding))
      ? Number(opts.padding) : GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING);
    const bodyRadius = node => finitePositive(
      node.radius, finitePositive(node.visual_radius,
        radiusFromGravityMass(node.gravity_mass), 80), 160
    );
    const groups = new Map();
    (nodes || []).forEach(node => {
      if (!node || node.ghost || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      const key = communityKey(node);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(node);
    });
    let systems = 0, satellites = 0, minimumClearance = null;
    groups.forEach(members => {
      if (members.length < 2) return;
      const anchor = galaxySystemAnchor(members);
      if (!anchor || anchor.anchor_role === 'global') return;
      systems++;
      const anchorRadius = bodyRadius(anchor);
      orderedGalaxySatellites(members, anchor).forEach(item => {
        const clearance = Math.hypot(item.node.x - anchor.x, item.node.y - anchor.y)
          - anchorRadius - bodyRadius(item.node) - padding;
        minimumClearance = minimumClearance === null
          ? clearance : Math.min(minimumClearance, clearance);
        satellites++;
      });
    });
    return { padding, systems, satellites, minimumClearance };
  }

  function combineGalaxySystemAnchorExclusions(passes) {
    const usable = (passes || []).filter(Boolean);
    if (!usable.length) return {
      padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
      systems: 0, contacts: 0, correctedDistance: 0, maximumShift: 0,
      inwardVelocityRemoved: 0, tangentialVelocityRemoved: 0,
      minimumClearance: null, iterations: 0,
    };
    const final = usable[usable.length - 1];
    return {
      padding: final.padding,
      systems: Math.max(...usable.map(pass => pass.systems || 0)),
      contacts: usable.reduce((sum, pass) => sum + (pass.contacts || 0), 0),
      correctedDistance: usable.reduce(
        (sum, pass) => sum + (pass.correctedDistance || 0), 0),
      maximumShift: Math.max(...usable.map(pass => pass.maximumShift || 0)),
      inwardVelocityRemoved: usable.reduce(
        (sum, pass) => sum + (pass.inwardVelocityRemoved || 0), 0),
      tangentialVelocityRemoved: usable.reduce(
        (sum, pass) => sum + (pass.tangentialVelocityRemoved || 0), 0),
      minimumClearance: final.minimumClearance,
      iterations: usable.reduce((sum, pass) => sum + (pass.iterations || 0), 0),
    };
  }

  /* Treat every community as one solar system and apply exact softened Newtonian attraction
     between system pairs. One acceleration is applied to every member of a system, preserving
     its internal orbit, while each pair contributes equal-and-opposite momentum. A single
     common cap scale bounds the final acceleration without changing any system's direction or
     manufacturing the outward impulses caused by post-hoc drift subtraction. Community count
     is bounded by the live-scene ceiling, so O(nodes + systems^2) remains cheaper and more
     physically faithful than another approximation layer here. */
  function applyGalaxyCentralGravity(nodes, options) {
    const opts = options || {};
    const centers = [...communityCenters(nodes).values()];
    const gravitationalConstant = galaxyBlackHoleGravityConstant(opts.gravity);
    const softening = Math.max(0.1, Number(opts.softening) || 40);
    const alphaValue = Number.isFinite(opts.alpha) ? Math.max(0, opts.alpha) : 1;
    const accelerationCap = Math.max(0, Number.isFinite(Number(opts.accelerationCap))
      ? Number(opts.accelerationCap) : defaultGalaxyBlackHoleAccelerationCap(opts.gravity));
    const totalMass = centers.reduce((sum, center) => sum + center.mass, 0);
    if (centers.length < 2 || totalMass <= 0 || gravitationalConstant <= 0 || alphaValue <= 0) {
      return { systems: centers.length, applied: 0, totalMass };
    }
    const accelerations = centers.map(center => ({ center, ax: 0, ay: 0 }));
    let applied = 0;
    for (let leftIndex = 0; leftIndex < centers.length; leftIndex++) {
      const left = centers[leftIndex];
      for (let rightIndex = leftIndex + 1; rightIndex < centers.length; rightIndex++) {
        const right = centers[rightIndex];
        const dx = right.x - left.x, dy = right.y - left.y;
        const denominator = Math.pow(dx * dx + dy * dy + softening * softening, 1.5);
        if (!Number.isFinite(denominator) || denominator <= 0) continue;
        const scale = gravitationalConstant * alphaValue / denominator;
        accelerations[leftIndex].ax += scale * right.mass * dx;
        accelerations[leftIndex].ay += scale * right.mass * dy;
        accelerations[rightIndex].ax -= scale * left.mass * dx;
        accelerations[rightIndex].ay -= scale * left.mass * dy;
        applied++;
      }
    }
    const maximumAcceleration = accelerations.reduce(
      (maximum, item) => Math.max(maximum, Math.hypot(item.ax, item.ay)), 0
    );
    const capScale = accelerationCap > 0 && maximumAcceleration > accelerationCap
      ? accelerationCap / maximumAcceleration : 1;
    accelerations.forEach(item => {
      const ax = item.ax * capScale, ay = item.ay * capScale;
      item.center.nodes.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + ax;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + ay;
      });
    });
    return { systems: centers.length, applied, totalMass };
  }

  /* Nearby solar systems exert a secondary Newtonian field on one another even when no
     evidence edge connects them. The black-hole community is excluded here because it already
     owns the stronger global potential below. Each system receives one rigid acceleration, so
     cross-system attraction cannot tear apart its local orbit. Exact pairs preserve momentum;
     Barnes-Hut removes only approximation drift for large scenes. */
  function applyGalaxyMutualSystemGravity(nodes, options) {
    const opts = options || {};
    const allCenters = [...communityCenters(nodes).values()];
    const anchor = galaxyGlobalAnchor(nodes);
    const coreKey = anchor ? communityKey(anchor) : null;
    const centers = allCenters.filter(center => center && center.mass > 0
      && (coreKey === null || center.id !== coreKey));
    const strengthFraction = Math.max(0, Math.min(1,
      Number.isFinite(Number(opts.strengthFraction))
        ? Number(opts.strengthFraction) : GALAXY_MUTUAL_SYSTEM_GRAVITY_FRACTION));
    const gravitationalConstant = galaxyLocalGravityConstant(opts.gravity) * strengthFraction;
    const softening = Math.max(0.1, Number(opts.softening)
      || GALAXY_MUTUAL_SYSTEM_SOFTENING);
    const alphaValue = Number.isFinite(opts.alpha) ? Math.max(0, opts.alpha) : 1;
    const exactLimit = Math.max(2, Number(opts.exactLimit) || GALAXY_EXACT_LIMIT);
    const theta = Math.max(0.1, Number(opts.theta) || GALAXY_BARNES_HUT_THETA);
    const accelerationCap = Math.max(0, Number.isFinite(Number(opts.accelerationCap))
      ? Number(opts.accelerationCap)
      : defaultGalaxyAccelerationCap(opts.gravity) * strengthFraction);
    const stats = {
      systems: centers.length, interactions: 0, traversals: 0, approximations: 0,
      maximumAcceleration: 0, capScale: 1,
    };
    if (centers.length < 2 || gravitationalConstant <= 0 || alphaValue <= 0) return stats;
    const proxies = centers.map(center => ({
      id: center.id, x: center.x, y: center.y, gravity_mass: center.mass,
      vx: 0, vy: 0, center,
    }));
    if (proxies.length <= exactLimit) {
      for (let left = 0; left < proxies.length; left++) {
        for (let right = left + 1; right < proxies.length; right++) {
          addGravityPair(
            proxies[left], proxies[right], gravitationalConstant, softening, alphaValue
          );
          stats.interactions++;
        }
      }
    } else {
      const quad = gravityQuad(proxies);
      proxies.forEach(proxy => applyQuadGravity(
        proxy, quad, gravitationalConstant, softening, alphaValue, theta, stats
      ));
      let totalMass = 0, momentumX = 0, momentumY = 0;
      proxies.forEach(proxy => {
        totalMass += proxy.gravity_mass;
        momentumX += proxy.gravity_mass * proxy.vx;
        momentumY += proxy.gravity_mass * proxy.vy;
      });
      if (totalMass > 0) proxies.forEach(proxy => {
        proxy.vx -= momentumX / totalMass;
        proxy.vy -= momentumY / totalMass;
      });
    }
    stats.maximumAcceleration = proxies.reduce((maximum, proxy) => Math.max(
      maximum, Math.hypot(proxy.vx, proxy.vy)
    ), 0);
    stats.capScale = accelerationCap > 0 && stats.maximumAcceleration > accelerationCap
      ? accelerationCap / stats.maximumAcceleration : 1;
    proxies.forEach(proxy => proxy.center.nodes.forEach(node => {
      node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + proxy.vx * stats.capScale;
      node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + proxy.vy * stats.capScale;
    }));
    return stats;
  }

  function galaxyGlobalAnchor(nodes) {
    let anchor = null;
    (nodes || []).forEach(node => {
      if (!node || node.ghost || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      if (!anchor) { anchor = node; return; }
      const nodeGlobal = node.anchor_role === 'global' ? 1 : 0;
      const anchorGlobal = anchor.anchor_role === 'global' ? 1 : 0;
      const nodeMass = finitePositive(node.gravity_mass, 1, 1000);
      const anchorMass = finitePositive(anchor.gravity_mass, 1, 1000);
      if (nodeGlobal > anchorGlobal || (nodeGlobal === anchorGlobal
        && (nodeMass > anchorMass || (nodeMass === anchorMass
          && String(node.id).localeCompare(String(anchor.id)) < 0)))) anchor = node;
    });
    return anchor;
  }

  function linearMedian(values) {
    if (!values.length) return 0;
    const data = values.slice();
    const target = Math.floor((data.length - 1) / 2);
    let left = 0, right = data.length - 1;
    while (left < right) {
      const pivot = data[(left + right) >> 1];
      let low = left, high = right;
      while (low <= high) {
        while (data[low] < pivot) low++;
        while (data[high] > pivot) high--;
        if (low <= high) {
          const swap = data[low]; data[low] = data[high]; data[high] = swap;
          low++; high--;
        }
      }
      if (target <= high) right = high;
      else if (target >= low) left = low;
      else break;
    }
    return data[target];
  }

  /* A galaxy is not a collection of peer point masses: its dominant evidence node is the
     black hole, its community is the dense bulge, and all remaining evidence supplies a
     smooth halo. This Plummer composite is O(nodes + systems), continuous across system-rank
     changes, and conservative in the black-hole frame. It also gives differential rotation:
     omega² = G[M_core/(r²+eps²)^(3/2) + M_halo/(r²+a²)^(3/2)]. */
  function galaxyBlackHoleField(nodes, options) {
    const opts = options || {};
    const centers = communityCenters(nodes);
    const anchor = galaxyGlobalAnchor(nodes);
    if (!anchor) return {
      anchor: null, systems: [], coreMass: 0, haloMass: 0, haloScale: 0, traversals: 0
    };
    const coreKey = communityKey(anchor);
    const totalMass = [...centers.values()].reduce((sum, center) => sum + center.mass, 0);
    /* The singular center term is sourced by the actual dominant evidence node. Other stars
       in its community remain part of the smooth bulge/halo instead of inflating black-hole
       mass merely because they share a community label. */
    const coreMass = finitePositive(anchor.gravity_mass, 1, 1000);
    const haloMass = Math.max(0, totalMass - coreMass);
    const external = [...centers.values()].filter(center => center.id !== coreKey).map(center => ({
      center,
      dx: anchor.x - center.x,
      dy: anchor.y - center.y,
      radius: Math.hypot(center.x - anchor.x, center.y - anchor.y),
    }));
    const coreSoftening = Math.max(0.1, Number(opts.softening) || 40);
    const hintedRadii = external.map(item => {
      const hint = item.center.nodes.map(node => Number(node.galactic_radius))
        .find(value => Number.isFinite(value) && value > 0);
      return hint || item.radius;
    });
    const initialMedianRadius = linearMedian(hintedRadii);
    const explicitScale = Number(opts.haloScale);
    const cachedScale = Number(anchor.__galaxyHaloScale);
    const haloScale = Math.max(coreSoftening * 2,
      Number.isFinite(explicitScale) && explicitScale > 0 ? explicitScale
        : Number.isFinite(cachedScale) && cachedScale > 0 ? cachedScale
          : initialMedianRadius * 0.65);
    /* The halo is part of the scene's potential, not a rubber band fitted to the current
       positions. Recomputing it after every inward step shrinks the Plummer radius, deepens
       the next step, and creates runaway collapse/ejection. Cache the seed scale on the
       black-hole node; it is non-enumerable, so exports and a fresh setData payload stay clean. */
    if (!(Number.isFinite(cachedScale) && cachedScale > 0)
      && !(Number.isFinite(explicitScale) && explicitScale > 0)) {
      Object.defineProperty(anchor, '__galaxyHaloScale', {
        value: haloScale, writable: false, configurable: true, enumerable: false
      });
    }
    const gravitationalConstant = galaxyBlackHoleGravityConstant(opts.gravity);
    const accelerationCap = Math.max(0, Number.isFinite(Number(opts.accelerationCap))
      ? Number(opts.accelerationCap) : defaultGalaxyBlackHoleAccelerationCap(opts.gravity));
    const systems = external.map(item => {
      const coreDenominator = Math.pow(
        item.radius * item.radius + coreSoftening * coreSoftening, 1.5
      );
      const haloDenominator = Math.pow(
        item.radius * item.radius + haloScale * haloScale, 1.5
      );
      const omegaSquared = gravitationalConstant * (
        coreMass / coreDenominator + (haloMass > 0 ? haloMass / haloDenominator : 0)
      );
      const omega = Math.sqrt(Math.max(0, omegaSquared));
      return { ...item, omega, circularSpeed: omega * item.radius,
        ax: item.dx * omegaSquared, ay: item.dy * omegaSquared };
    });
    const maximumAcceleration = systems.reduce(
      (maximum, item) => Math.max(maximum, Math.hypot(item.ax, item.ay)), 0
    );
    const capScale = accelerationCap > 0 && maximumAcceleration > accelerationCap
      ? accelerationCap / maximumAcceleration : 1;
    if (capScale < 1) systems.forEach(item => {
      item.ax *= capScale;
      item.ay *= capScale;
      item.omega *= Math.sqrt(capScale);
      item.circularSpeed *= Math.sqrt(capScale);
    });
    return {
      anchor, systems, coreMass, haloMass, haloScale, totalMass,
      traversals: centers.size,
    };
  }

  function applyGalaxyBlackHoleGravity(nodes, options) {
    const field = galaxyBlackHoleField(nodes, options);
    field.systems.forEach(item => item.center.nodes.forEach(node => {
      node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + item.ax;
      node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + item.ay;
    }));
    return {
      anchorId: field.anchor ? field.anchor.id : null,
      systems: field.systems.length,
      coreMass: field.coreMass,
      haloMass: field.haloMass,
      haloScale: field.haloScale,
      traversals: field.traversals,
    };
  }

  function recenterGalaxyOnAnchor(nodes) {
    const anchor = galaxyGlobalAnchor(nodes);
    if (!anchor) return null;
    const shiftX = Number.isFinite(anchor.x) ? anchor.x : 0;
    const shiftY = Number.isFinite(anchor.y) ? anchor.y : 0;
    const shiftVx = Number.isFinite(anchor.vx) ? anchor.vx : 0;
    const shiftVy = Number.isFinite(anchor.vy) ? anchor.vy : 0;
    (nodes || []).forEach(node => {
      if (Number.isFinite(node.x)) node.x -= shiftX;
      if (Number.isFinite(node.y)) node.y -= shiftY;
      node.vx = (Number.isFinite(node.vx) ? node.vx : 0) - shiftVx;
      node.vy = (Number.isFinite(node.vy) ? node.vy : 0) - shiftVy;
    });
    anchor.x = 0; anchor.y = 0; anchor.vx = 0; anchor.vy = 0;
    return anchor;
  }

  function applyCommunityBridgeGravity(nodes, bridges, options) {
    const opts = options || {};
    const centers = communityCenters(nodes);
    const gravitationalConstant = GALAXY_BRIDGE_SCALE
      * galaxyLocalGravityConstant(opts.gravity);
    const softening = Math.max(0.1, Number(opts.softening) || 32);
    const alphaValue = Number.isFinite(opts.alpha) ? Math.max(0, opts.alpha) : 1;
    let applied = 0;
    (bridges || []).forEach(bridge => {
      if (!bridge || bridge.ghost) return;
      const sourceId = idOf(bridge.source_community !== undefined
        ? bridge.source_community : bridge.source);
      const targetId = idOf(bridge.target_community !== undefined
        ? bridge.target_community : bridge.target);
      const source = centers.get(String(sourceId)), target = centers.get(String(targetId));
      if (!source || !target || source === target) return;
      const physicsStrength = Math.max(0, Math.min(1,
        Number.isFinite(Number(bridge.physics_strength))
          ? Number(bridge.physics_strength) : Number(bridge.strength) || 0));
      if (!physicsStrength) return;
      const dx = target.x - source.x, dy = target.y - source.y;
      const denominator = Math.pow(dx * dx + dy * dy + softening * softening, 1.5);
      if (!Number.isFinite(denominator) || denominator <= 0) return;
      const scale = gravitationalConstant * physicsStrength * alphaValue / denominator;
      source.nodes.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + scale * target.mass * dx;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + scale * target.mass * dy;
      });
      target.nodes.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) - scale * source.mass * dx;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) - scale * source.mass * dy;
      });
      applied++;
    });
    return { bridges: applied, communities: centers.size };
  }
  function galaxySpringStrength(link, nodesById) {
    if (!link || link.ghost || link.suggested || Number(link.physics_strength) === 0) return 0;
    const source = typeof link.source === 'object' ? link.source : nodesById.get(linkEndpoint(link, 'source'));
    const target = typeof link.target === 'object' ? link.target : nodesById.get(linkEndpoint(link, 'target'));
    if (!source || !target || source.ghost || target.ghost
        || communityKey(source) !== communityKey(target)) return 0;
    return Math.max(0, Math.min(0.25,
      Number.isFinite(Number(link.spring_strength)) ? Number(link.spring_strength) : 0.05));
  }
  function galaxySpringDistance(link, orbitScale) {
    const base = finitePositive(link && link.rest_length, 24, 240);
    return base * Math.max(1 / 16, Math.min(25, Number(orbitScale) || 1));
  }
  function galaxySafeSpringDistance(link, orbitScale, left, right, padding = 1.5) {
    const radius = node => finitePositive(node && node.radius,
      finitePositive(node && node.visual_radius,
        radiusFromGravityMass(node && node.gravity_mass), 80), 160);
    return Math.max(galaxySpringDistance(link, orbitScale),
      radius(left) + radius(right) + Math.max(0, Number(padding) || 0));
  }
  /* The scene contract marks every member of a server-authored solar system with the same
     non-empty anchor id. Those links remain useful evidence to paint and traverse, but their
     length is not a second orbital law: dominant-star gravity owns the shared system's phase
     and radius. Compatibility callers without this explicit metadata retain relation physics. */
  function galaxySameExplicitOrbitalSystem(left, right) {
    if (!left || !right || communityKey(left) !== communityKey(right)) return false;
    const leftAnchor = left.system_anchor_id === undefined
      || left.system_anchor_id === null ? '' : String(left.system_anchor_id).trim();
    const rightAnchor = right.system_anchor_id === undefined
      || right.system_anchor_id === null ? '' : String(right.system_anchor_id).trim();
    return leftAnchor !== '' && leftAnchor === rightAnchor;
  }
  function applyGalaxyRelationSprings(nodes, links, options) {
    const opts = options || {};
    const byId = new Map((nodes || []).map(node => [node.id, node]));
    const systemAnchors = new Map();
    if (opts.skipSystemAnchorRelations === true) {
      const groups = new Map();
      (nodes || []).forEach(node => {
        const key = communityKey(node);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(node);
      });
      groups.forEach((members, key) => systemAnchors.set(key, galaxySystemAnchor(members)));
    }
    const alphaValue = Number.isFinite(opts.alpha) ? Math.max(0, opts.alpha) : 1;
    const orbitScale = Math.max(1 / 16, Math.min(25, Number(opts.orbitScale) || 1));
    const strengthMultiplier = Math.max(0, Math.min(4,
      Number.isFinite(Number(opts.strengthMultiplier)) ? Number(opts.strengthMultiplier) : 1));
    const forceCap = Math.max(0, Number.isFinite(Number(opts.forceCap))
      ? Number(opts.forceCap) : 0.8);
    const accelerationCap = Math.max(0, Number.isFinite(Number(opts.accelerationCap))
      ? Number(opts.accelerationCap) : Number.POSITIVE_INFINITY);
    const initialVelocity = new Map((nodes || []).map(node => [node, {
      vx: Number.isFinite(node.vx) ? node.vx : 0,
      vy: Number.isFinite(node.vy) ? node.vy : 0,
    }]));
    let applied = 0, skippedOrbitalSystem = 0;
    (links || []).forEach(link => {
      const left = byId.get(linkEndpoint(link, 'source'));
      const right = byId.get(linkEndpoint(link, 'target'));
      const strength = galaxySpringStrength(link, byId) * strengthMultiplier;
      if (!left || !right || left === right || strength <= 0) return;
      if (opts.skipFixedNodeRelations === true
        && (left.id === opts.fixedNodeId || right.id === opts.fixedNodeId)) return;
      if (opts.skipOrbitalSystemRelations === true
        && galaxySameExplicitOrbitalSystem(left, right)) {
        skippedOrbitalSystem++;
        return;
      }
      const systemAnchor = systemAnchors.get(communityKey(left));
      if (opts.skipSystemAnchorRelations === true
        && communityKey(left) === communityKey(right)
        && (left === systemAnchor || right === systemAnchor)) return;
      const dx = right.x - left.x, dy = right.y - left.y;
      const distance = Math.hypot(dx, dy);
      if (!Number.isFinite(distance) || distance <= 1e-9) return;
      let force = (distance - galaxySafeSpringDistance(
        link, orbitScale, left, right, opts.padding
      )) * strength * alphaValue;
      if (forceCap > 0) force = Math.max(-forceCap, Math.min(forceCap, force));
      const fx = force * dx / distance, fy = force * dy / distance;
      const leftMass = finitePositive(left.gravity_mass, 1, 1000);
      const rightMass = finitePositive(right.gravity_mass, 1, 1000);
      left.vx = (Number.isFinite(left.vx) ? left.vx : 0) + fx / leftMass;
      left.vy = (Number.isFinite(left.vy) ? left.vy : 0) + fy / leftMass;
      right.vx = (Number.isFinite(right.vx) ? right.vx : 0) - fx / rightMass;
      right.vy = (Number.isFinite(right.vy) ? right.vy : 0) - fy / rightMass;
      applied++;
    });
    /* A hub can own many valid relations. Cap the aggregate relation acceleration with one
       common scale rather than clipping nodes independently; this preserves the springs'
       equal-and-opposite evidence-mass momentum while preventing a dense hub slingshot. */
    let maximumAcceleration = 0;
    initialVelocity.forEach((before, node) => {
      maximumAcceleration = Math.max(maximumAcceleration,
        Math.hypot((Number(node.vx) || 0) - before.vx, (Number(node.vy) || 0) - before.vy));
    });
    const accelerationScale = accelerationCap > 0 && maximumAcceleration > accelerationCap
      ? accelerationCap / maximumAcceleration : 1;
    if (accelerationScale < 1) initialVelocity.forEach((before, node) => {
      node.vx = before.vx + ((Number(node.vx) || 0) - before.vx) * accelerationScale;
      node.vy = before.vy + ((Number(node.vy) || 0) - before.vy) * accelerationScale;
    });
    return {
      applied,
      skippedOrbitalSystem,
      maximumAcceleration,
      accelerationCapped: accelerationScale < 1,
    };
  }

  /* Spring acceleration alone became visually inert as the fixed timestep was repeatedly
     reduced. This position-based companion resolves a bounded fraction of relation error per
     wall-clock frame. It only acts inside a solar system; mass-weighted inverse corrections
     preserve that system's centre of mass, while the black-hole boundary remains responsible
     for system-scale motion. */
  function applyGalaxyRelationDistanceConstraints(nodes, links, options) {
    const opts = options || {};
    const byId = new Map((nodes || []).map(node => [node.id, node]));
    const systemAnchors = new Map();
    if (opts.skipSystemAnchorRelations === true) {
      const groups = new Map();
      (nodes || []).forEach(node => {
        const key = communityKey(node);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(node);
      });
      groups.forEach((members, key) => systemAnchors.set(key, galaxySystemAnchor(members)));
    }
    const orbitScale = Math.max(1 / 16, Math.min(25, Number(opts.orbitScale) || 1));
    const strengthMultiplier = Math.max(0, Math.min(2,
      Number.isFinite(Number(opts.strengthMultiplier)) ? Number(opts.strengthMultiplier) : 1));
    const responseMultiplier = Math.max(0, Math.min(2,
      Number.isFinite(Number(opts.responseMultiplier)) ? Number(opts.responseMultiplier) : 1));
    const wallClockSeconds = Math.max(0, Number.isFinite(Number(opts.wallClockSeconds))
      ? Number(opts.wallClockSeconds) : GALAXY_FRAME_INTERVAL_MS / 1000);
    const rate = Math.max(0, Number.isFinite(Number(opts.rate))
      ? Number(opts.rate) : GALAXY_RELATION_CONSTRAINT_RATE);
    const maximumCorrection = Math.max(0, Number.isFinite(Number(opts.maxCorrection))
      ? Number(opts.maxCorrection) : GALAXY_RELATION_CONSTRAINT_MAX_CORRECTION);
    const shifts = new Map((nodes || []).map(node => [node, { x: 0, y: 0 }]));
    let applied = 0, skippedFixedEndpoint = 0, skippedSystemAnchor = 0;
    let skippedOrbitalSystem = 0;
    let maximumError = 0, requestedDistance = 0;
    (links || []).forEach(link => {
      const left = byId.get(linkEndpoint(link, 'source'));
      const right = byId.get(linkEndpoint(link, 'target'));
      if (!left || !right || left === right || left.ghost || right.ghost
        || communityKey(left) !== communityKey(right)) return;
      /* A pointer-owned node is an externally imposed moving source, not a spring endpoint.
         Otherwise the fixed-endpoint correction assigns the entire (up to 4-unit) Link error
         to its connected peer every physics slice, which turns a long pointer move into a
         rapid positional slingshot. The bounded drag gravity below is the sole follower path
         during a gesture; ordinary fixed-node callers retain the legacy constraint behavior. */
      if (opts.skipFixedNodeRelations === true
        && (left.id === opts.fixedNodeId || right.id === opts.fixedNodeId)) {
        skippedFixedEndpoint++;
        return;
      }
      if (opts.skipOrbitalSystemRelations === true
        && galaxySameExplicitOrbitalSystem(left, right)) {
        skippedOrbitalSystem++;
        return;
      }
      const systemAnchor = systemAnchors.get(communityKey(left));
      if (opts.skipSystemAnchorRelations === true
        && (left === systemAnchor || right === systemAnchor)) {
        /* The dominant star/planet radius belongs to the central potential, not Link PBD.
           Re-projecting it to a slider target every tick erases the orbital phase. */
        skippedSystemAnchor++;
        return;
      }
      const strength = galaxySpringStrength(link, byId) * strengthMultiplier;
      if (!(strength > 0)) return;
      const dx = right.x - left.x, dy = right.y - left.y;
      const distance = Math.hypot(dx, dy);
      if (!Number.isFinite(distance) || distance <= 1e-9) return;
      const error = distance - galaxySafeSpringDistance(
        link, orbitScale, left, right, opts.padding
      );
      /* Response multipliers belong inside the exponential. Multiplying the completed
         displacement can exceed one, cross the requested rest length and reverse on the next
         frame. Scaling the exponent changes the continuous convergence rate while preserving
         the solver's invariant 0 <= response < 1 for every Link setting and frame duration. */
      const response = 1 - Math.exp(
        -rate * strength * wallClockSeconds * responseMultiplier
      );
      let correction = error * response;
      if (maximumCorrection > 0) correction = Math.max(
        -maximumCorrection, Math.min(maximumCorrection, correction));
      if (!Number.isFinite(correction) || Math.abs(correction) <= 1e-12) return;
      const leftMass = finitePositive(left.gravity_mass, 1, 1000);
      const rightMass = finitePositive(right.gravity_mass, 1, 1000);
      const leftInverseMass = left.anchor_role === 'global' || left.id === opts.fixedNodeId
        ? 0 : 1 / leftMass;
      const rightInverseMass = right.anchor_role === 'global' || right.id === opts.fixedNodeId
        ? 0 : 1 / rightMass;
      const inverseMass = leftInverseMass + rightInverseMass;
      if (!(inverseMass > 0)) return;
      const unitX = dx / distance, unitY = dy / distance;
      const leftShift = shifts.get(left), rightShift = shifts.get(right);
      leftShift.x += unitX * correction * leftInverseMass / inverseMass;
      leftShift.y += unitY * correction * leftInverseMass / inverseMass;
      rightShift.x -= unitX * correction * rightInverseMass / inverseMass;
      rightShift.y -= unitY * correction * rightInverseMass / inverseMass;
      applied++;
      maximumError = Math.max(maximumError, Math.abs(error));
      requestedDistance += Math.abs(correction);
    });
    /* Apply one Jacobi-style update from the unchanged phase snapshot. Sequential mutation
       made high-degree hubs order-dependent: their last edge undid their first edge and the
       cycle restarted next frame. One common aggregate cap preserves every pair's mass-weighted
       balance while preventing a hub with many links from moving N times farther than a leaf. */
    let maximumNodeShift = 0;
    shifts.forEach(shift => {
      maximumNodeShift = Math.max(maximumNodeShift, Math.hypot(shift.x, shift.y));
    });
    const aggregateScale = maximumCorrection > 0 && maximumNodeShift > maximumCorrection
      ? maximumCorrection / maximumNodeShift : 1;
    shifts.forEach((shift, node) => {
      node.x += shift.x * aggregateScale;
      node.y += shift.y * aggregateScale;
    });
    return {
      applied,
      skippedFixedEndpoint,
      skippedSystemAnchor,
      skippedOrbitalSystem,
      maximumError,
      correctedDistance: requestedDistance * aggregateScale,
      maximumNodeShift: maximumNodeShift * aggregateScale,
      aggregateLimited: aggregateScale < 1,
      strengthMultiplier,
      responseMultiplier,
    };
  }

  /* A pointer temporarily makes the dragged body an externally positioned gravitational
     source. Every live body responds to the same evidence mass and softened inverse-square law
     as the persistent Galaxy solver; topology can strengthen a relation but never decides
     whether gravity exists. The relation's safe orbital distance is a periapsis boundary, not
     a copied offset: nearby unlinked stars follow because the moved mass attracts them, while
     distant systems receive only the naturally weaker tail. */
  function applyDraggedNodeGravity(source, followers, options) {
    const opts = options || {};
    if (!source || !Number.isFinite(source.x) || !Number.isFinite(source.y)) {
      return { applied: 0, maximumAcceleration: 0, maximumPull: 0 };
    }
    const sourceMass = finitePositive(source.gravity_mass, 1, 1000);
    const gravityMultiplier = Math.max(0, Number.isFinite(Number(opts.gravityMultiplier))
      ? Number(opts.gravityMultiplier) : 1);
    const gravity = galaxyLocalGravityConstant(opts.gravity) * gravityMultiplier;
    const softening = finitePositive(opts.softening,
      GALAXY_DRAG_GRAVITY_SOFTENING, 240);
    const duration = finitePositive(opts.duration, GALAXY_DRAG_GRAVITY_TIME, 60);
    const maximumPull = finitePositive(opts.maximumPull,
      GALAXY_DRAG_GRAVITY_MAX_PULL, 240);
    const explicitMaximumImpulse = Number(opts.maximumImpulse);
    const maximumImpulse = Number.isFinite(explicitMaximumImpulse) && explicitMaximumImpulse >= 0
      ? Math.min(MAX_NODE_SPEED, explicitMaximumImpulse)
      : GALAXY_DRAG_GRAVITY_MAX_IMPULSE;
    const orbitScale = galaxyRelationOrbitScale(opts.linkSetting);
    let applied = 0, maximumAcceleration = 0, largestPull = 0;
    (followers || []).forEach(entry => {
      const node = entry && entry.node ? entry.node : entry;
      const link = entry && entry.link ? entry.link : null;
      if (!node || node === source || node.ghost || node.anchor_role === 'global'
        || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      const dx = source.x - node.x, dy = source.y - node.y;
      const distance = Math.hypot(dx, dy);
      if (!Number.isFinite(distance) || distance <= 1e-9) return;
      const byId = new Map([[source.id, source], [node.id, node]]);
      /* Evidence-backed relations strengthen capture, but even compatibility links without
         spring metadata retain half coupling so old payloads still behave physically. */
      const relationStrength = link ? galaxySpringStrength(link, byId) : 0.125;
      /* Nearby and same-system bodies follow ordinary unit gravity. An explicit evidence edge
         can strengthen capture up to 1.5x, but never turns topology into a teleport spring. */
      const coupling = Math.max(0.5, Math.min(1.5, 0.5 + relationStrength * 4));
      const softened = distance * distance + softening * softening;
      const acceleration = gravity * sourceMass * coupling * distance
        / Math.pow(softened, 1.5);
      if (!Number.isFinite(acceleration) || acceleration <= 0) return;
      const unitX = dx / distance, unitY = dy / distance;
      const safeDistance = link
        ? galaxySafeSpringDistance(link, orbitScale, source, node, opts.padding)
        : finitePositive(source.radius, 2, 160) + finitePositive(node.radius, 2, 160)
          + Math.max(0, Number(opts.padding) || 0);
      const radialError = Math.max(0, distance - safeDistance);
      const response = 1 - Math.exp(-acceleration * duration);
      const pull = Math.min(maximumPull, radialError * response);
      if (pull > 0) {
        node.x += unitX * pull;
        node.y += unitY * pull;
      }
      /* Preserve the existing tangential orbit and add only the gravitational impulse. The
         impulse has its own local bound; the ordinary Galaxy emergency ceiling is applied only
         if repeated pointer events would otherwise accumulate an unsafe release velocity. */
      if (opts.applyImpulse !== false && maximumImpulse > 0) {
        const impulse = Math.min(maximumImpulse, acceleration * duration);
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + unitX * impulse;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + unitY * impulse;
        const speed = Math.hypot(node.vx, node.vy);
        if (speed > MAX_NODE_SPEED) {
          const scale = MAX_NODE_SPEED / speed;
          node.vx *= scale;
          node.vy *= scale;
        }
      }
      applied++;
      maximumAcceleration = Math.max(maximumAcceleration, acceleration);
      largestPull = Math.max(largestPull, pull);
      if (entry && entry.node) {
        entry.lastAcceleration = acceleration;
        entry.lastPull = pull;
      }
    });
    return { applied, maximumAcceleration, maximumPull: largestPull };
  }

  /* Live dragging samples a force, never a pointer-event displacement. Pointermove frequency
     varies wildly by browser and input device; applying the positional helper above on every
     event compounded eight small events into a violent 180-unit jump. This acceleration-only
     field is sampled by the same fixed-step leapfrog clock as the rest of the Galaxy. Direct
     evidence relations may strengthen capture, while every unlinked body still receives the
     requested doubled local gravity without copying the pointer offset. */
  function applyDraggedNodeAcceleration(source, followers, options) {
    const opts = options || {};
    if (!source || !Number.isFinite(source.x) || !Number.isFinite(source.y)) {
      return { applied: 0, maximumAcceleration: 0, maximumPull: 0 };
    }
    const sourceMass = finitePositive(source.gravity_mass, 1, 1000);
    const gravity = galaxyLocalGravityConstant(opts.gravity)
      * GALAXY_DRAG_GRAVITY_MULTIPLIER;
    const softening = finitePositive(opts.softening,
      GALAXY_DRAG_GRAVITY_SOFTENING, 240);
    let applied = 0, maximumAcceleration = 0;
    (followers || []).forEach(entry => {
      const node = entry && entry.node ? entry.node : entry;
      const link = entry && entry.link ? entry.link : null;
      if (!node || node === source || node.ghost || node.anchor_role === 'global'
        || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      const dx = source.x - node.x, dy = source.y - node.y;
      const distance = Math.hypot(dx, dy);
      if (!Number.isFinite(distance) || distance <= 1e-9) return;
      const byId = new Map([[source.id, source], [node.id, node]]);
      const relationStrength = link ? galaxySpringStrength(link, byId) : 0.125;
      const coupling = Math.max(0.5, Math.min(1.5, 0.5 + relationStrength * 4));
      const softened = distance * distance + softening * softening;
      const acceleration = gravity * sourceMass * coupling * distance
        / Math.pow(softened, 1.5);
      if (!Number.isFinite(acceleration) || acceleration <= 0) return;
      node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + dx / distance * acceleration;
      node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + dy / distance * acceleration;
      applied++;
      maximumAcceleration = Math.max(maximumAcceleration, acceleration);
    });
    return { applied, maximumAcceleration, maximumPull: 0 };
  }

  /* D3's stock collision force divides the correction by painted radius squared. Evidence
     radius is not inertial mass, so a large star touching a small planet can inject momentum
     and eject their whole solar system. This deterministic spatial-grid pass uses evidence
     mass for the impulse split: m1*dv1 + m2*dv2 is exactly zero for every contact. The grid
     keeps ordinary traversal near O(n); only genuinely crowded cells pay pairwise cost. */
  function applyGalaxyCollisions(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const padding = Math.max(0, Number.isFinite(Number(opts.padding))
      ? Number(opts.padding) : 1.5);
    const strength = Math.max(0, Math.min(1, Number.isFinite(Number(opts.strength))
      ? Number(opts.strength) : 0.7));
    const settleNormal = opts.settleNormal === true;
    const iterations = Math.max(1, Math.min(4, Math.floor(Number(opts.iterations) || 1)));
    const stats = {
      bodies: bodies.length, pairs: 0, overlaps: 0, cells: 0, correctionDistance: 0,
    };
    if (bodies.length < 2 || strength <= 0) return stats;
    const bodyRadius = node => finitePositive(
      node.radius, finitePositive(node.visual_radius, radiusFromGravityMass(node.gravity_mass), 80), 160
    );
    const maximumRadius = bodies.reduce(
      (maximum, node) => Math.max(maximum, bodyRadius(node)), 0
    );
    const cellSize = Math.max(1, maximumRadius * 2 + padding);
    for (let iteration = 0; iteration < iterations; iteration++) {
      const grid = new Map();
      bodies.forEach((node, index) => {
        const x = node.x, y = node.y;
        const cellX = Math.floor(x / cellSize), cellY = Math.floor(y / cellSize);
        const key = cellX + ',' + cellY;
        if (!grid.has(key)) grid.set(key, []);
        grid.get(key).push({ node, index, x, y, radius: bodyRadius(node), cellX, cellY });
      });
      stats.cells = Math.max(stats.cells, grid.size);
      grid.forEach(bucket => bucket.forEach(left => {
        for (let offsetX = -1; offsetX <= 1; offsetX++) {
          for (let offsetY = -1; offsetY <= 1; offsetY++) {
            const candidates = grid.get(
              (left.cellX + offsetX) + ',' + (left.cellY + offsetY)
            ) || [];
            candidates.forEach(right => {
              if (right.index <= left.index) return;
              if (opts.sameCommunityOnly === true
                && communityKey(left.node) !== communityKey(right.node)) return;
              stats.pairs++;
              const minimumDistance = left.radius + right.radius + padding;
              if (Math.hypot(right.x - left.x, right.y - left.y) >= minimumDistance) return;
              let normalX = right.node.x - left.node.x;
              let normalY = right.node.y - left.node.y;
              let normalDistance = Math.hypot(normalX, normalY);
              const separationDistance = normalDistance;
              if (normalDistance <= 1e-9) {
                const angle = seededHash(0, String(left.node.id) + '|' + String(right.node.id))
                  / 0x100000000 * Math.PI * 2;
                normalX = Math.cos(angle);
                normalY = Math.sin(angle);
                normalDistance = 1;
              }
              const relativeCorrection = (minimumDistance - separationDistance) * strength;
              if (!(relativeCorrection > 0) || !Number.isFinite(relativeCorrection)) return;
              stats.correctionDistance += relativeCorrection;
              const leftMass = finitePositive(left.node.gravity_mass, 1, 1000);
              const rightMass = finitePositive(right.node.gravity_mass, 1, 1000);
              const leftInverseMass = left.node.anchor_role === 'global' ? 0 : 1 / leftMass;
              const rightInverseMass = right.node.anchor_role === 'global' ? 0 : 1 / rightMass;
              if (leftInverseMass + rightInverseMass <= 0) return;
              const inverseMass = leftInverseMass + rightInverseMass;
              const projection = relativeCorrection / inverseMass;
              const unitX = normalX / normalDistance, unitY = normalY / normalDistance;
              /* Resolve penetration geometrically. Turning overlap depth into velocity adds
                 kinetic energy every fixed step and eventually slingshots a member out of a
                 crowded system. The mass-weighted projection preserves the pair COM. */
              left.node.x -= unitX * projection * leftInverseMass;
              left.node.y -= unitY * projection * leftInverseMass;
              right.node.x += unitX * projection * rightInverseMass;
              right.node.y += unitY * projection * rightInverseMass;

              /* Cancel only closing normal motion (zero restitution). Enlarging the lever arm
                 during projection would otherwise manufacture angular momentum even with no
                 impulse, so scale the pair's tangential relative speed by old/new separation.
                 This is the unique momentum-preserving remap of the projected phase point; its
                 factor is <= 1, hence it can only remove energy. */
              const leftVx = Number.isFinite(left.node.vx) ? left.node.vx : 0;
              const leftVy = Number.isFinite(left.node.vy) ? left.node.vy : 0;
              const rightVx = Number.isFinite(right.node.vx) ? right.node.vx : 0;
              const rightVy = Number.isFinite(right.node.vy) ? right.node.vy : 0;
              const tangentX = -unitY, tangentY = unitX;
              const relativeVx = rightVx - leftVx, relativeVy = rightVy - leftVy;
              const normalSpeed = relativeVx * unitX + relativeVy * unitY;
              const tangentSpeed = relativeVx * tangentX + relativeVy * tangentY;
              const projectedDistance = separationDistance + relativeCorrection;
              const tangentScale = projectedDistance > 1e-9
                ? Math.min(1, separationDistance / projectedDistance) : 0;
              const targetNormalSpeed = settleNormal ? 0 : Math.max(0, normalSpeed);
              const deltaVx = (targetNormalSpeed - normalSpeed) * unitX
                + (tangentSpeed * tangentScale - tangentSpeed) * tangentX;
              const deltaVy = (targetNormalSpeed - normalSpeed) * unitY
                + (tangentSpeed * tangentScale - tangentSpeed) * tangentY;
              left.node.vx = leftVx - deltaVx * leftInverseMass / inverseMass;
              left.node.vy = leftVy - deltaVy * leftInverseMass / inverseMass;
              right.node.vx = rightVx + deltaVx * rightInverseMass / inverseMass;
              right.node.vy = rightVy + deltaVy * rightInverseMass / inverseMass;
              stats.overlaps++;
            });
          }
        }
      }));
    }
    return stats;
  }

  /* Stable Jacobi projection for the persistent Orbital-separation layer. The generic
     collision helper above intentionally retains its pair-at-a-time contract for legacy
     callers; the live Galaxy cannot use that ordering because a dense hub would be shifted
     repeatedly within one frame. Every pair here samples one immutable phase, accumulates a
     mass-balanced correction, and applies one globally bounded update. Local contacts use the
     full adjustable pressure; an opt-in weaker cross-community pressure prevents painted nodes
     from different systems bunching without turning the galaxy into hard billiards. A cross-
     community contact translates each whole system, preserving its internal orbit geometry. */
  function applyGalaxyOrbitalSeparation(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const padding = Math.max(0, Number.isFinite(Number(opts.padding))
      ? Number(opts.padding) : 1.5);
    const strength = Math.max(0, Math.min(1, Number.isFinite(Number(opts.strength))
      ? Number(opts.strength) : 0.7));
    const crossCommunityPadding = Math.max(0,
      Number.isFinite(Number(opts.crossCommunityPadding))
        ? Number(opts.crossCommunityPadding) : 1.5);
    const crossCommunityStrength = Math.max(0, Math.min(1,
      Number.isFinite(Number(opts.crossCommunityStrength))
        ? Number(opts.crossCommunityStrength) : 0));
    const maximumCorrection = Math.max(0, Number.isFinite(Number(opts.maxCorrection))
      ? Number(opts.maxCorrection) : 4);
    const maximumVelocityCorrection = Math.max(0,
      Number.isFinite(Number(opts.maxVelocityCorrection))
        ? Number(opts.maxVelocityCorrection) : 8);
    const stats = {
      bodies: bodies.length, pairs: 0, overlaps: 0, cells: 0,
      crossCommunityPairs: 0, crossCommunityOverlaps: 0,
      correctionDistance: 0, crossCommunityCorrectionDistance: 0,
      maximumNodeShift: 0, aggregateLimited: false,
      radialPreservedContacts: 0, radiusPreservedNodes: 0,
    };
    if (bodies.length < 2 || Math.max(strength, crossCommunityStrength) <= 0) return stats;
    const bodyRadius = node => finitePositive(
      node.radius, finitePositive(node.visual_radius,
        radiusFromGravityMass(node.gravity_mass), 80), 160
    );
    const maximumRadius = bodies.reduce(
      (maximum, node) => Math.max(maximum, bodyRadius(node)), 0
    );
    const cellSize = Math.max(
      1, maximumRadius * 2 + Math.max(padding, crossCommunityPadding)
    );
    const grid = new Map();
    const shifts = new Map(bodies.map(node => [node, { x: 0, y: 0 }]));
    const velocityShifts = new Map(bodies.map(node => [node, { x: 0, y: 0 }]));
    const groups = new Map();
    const groupForNode = new Map();
    const contacts = [];
    bodies.forEach((node, index) => {
      const groupKey = communityKey(node);
      if (!groups.has(groupKey)) {
        groups.set(groupKey, {
          nodes: [], mass: 0, fixed: false, shift: { x: 0, y: 0 },
        });
      }
      const group = groups.get(groupKey);
      const mass = finitePositive(node.gravity_mass, 1, 1000);
      group.nodes.push(node);
      group.mass += mass;
      group.fixed = group.fixed || node.anchor_role === 'global' || node.id === opts.fixedNodeId;
      groupForNode.set(node, group);
      const cellX = Math.floor(node.x / cellSize), cellY = Math.floor(node.y / cellSize);
      const key = cellX + ',' + cellY;
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push({
        node, index, x: node.x, y: node.y, radius: bodyRadius(node), cellX, cellY,
      });
    });
    groups.forEach(group => { group.anchor = galaxySystemAnchor(group.nodes); });
    stats.cells = grid.size;
    grid.forEach(bucket => bucket.forEach(left => {
      for (let offsetX = -1; offsetX <= 1; offsetX++) {
        for (let offsetY = -1; offsetY <= 1; offsetY++) {
          const candidates = grid.get(
            (left.cellX + offsetX) + ',' + (left.cellY + offsetY)
          ) || [];
          candidates.forEach(right => {
            if (right.index <= left.index) return;
            const crossCommunity = communityKey(left.node) !== communityKey(right.node);
            const leftGroup = groupForNode.get(left.node);
            const rightGroup = groupForNode.get(right.node);
            if (!crossCommunity && opts.skipSystemAnchorPairs === true
              && (left.node === leftGroup.anchor || right.node === leftGroup.anchor)) return;
            const pairStrength = crossCommunity ? crossCommunityStrength : strength;
            if (!(pairStrength > 0)) return;
            const pairPadding = crossCommunity ? crossCommunityPadding : padding;
            stats.pairs++;
            if (crossCommunity) stats.crossCommunityPairs++;
            const minimumDistance = left.radius + right.radius + pairPadding;
            let normalX = right.x - left.x, normalY = right.y - left.y;
            let distance = Math.hypot(normalX, normalY);
            if (distance >= minimumDistance) return;
            if (distance <= 1e-9) {
              const angle = seededHash(0, String(left.node.id) + '|' + String(right.node.id))
                / 0x100000000 * Math.PI * 2;
              normalX = Math.cos(angle);
              normalY = Math.sin(angle);
              distance = 0;
            }
            const unitDistance = Math.max(1, Math.hypot(normalX, normalY));
            const unitX = normalX / unitDistance, unitY = normalY / unitDistance;
            const correction = (minimumDistance - distance) * pairStrength;
            if (!(correction > 0) || !Number.isFinite(correction)) return;
            const leftMass = crossCommunity
              ? leftGroup.mass : finitePositive(left.node.gravity_mass, 1, 1000);
            const rightMass = crossCommunity
              ? rightGroup.mass : finitePositive(right.node.gravity_mass, 1, 1000);
            const leftFixed = crossCommunity ? leftGroup.fixed
              : left.node.anchor_role === 'global' || left.node.id === opts.fixedNodeId;
            const rightFixed = crossCommunity ? rightGroup.fixed
              : right.node.anchor_role === 'global' || right.node.id === opts.fixedNodeId;
            const leftInverseMass = leftFixed ? 0 : 1 / leftMass;
            const rightInverseMass = rightFixed ? 0 : 1 / rightMass;
            const inverseMass = leftInverseMass + rightInverseMass;
            if (!(inverseMass > 0)) return;
            const projection = correction / inverseMass;
            const leftShift = crossCommunity ? leftGroup.shift : shifts.get(left.node);
            const rightShift = crossCommunity ? rightGroup.shift : shifts.get(right.node);
            leftShift.x -= unitX * projection * leftInverseMass;
            leftShift.y -= unitY * projection * leftInverseMass;
            rightShift.x += unitX * projection * rightInverseMass;
            rightShift.y += unitY * projection * rightInverseMass;
            /* Rigid cross-system position projection is complete here. Do not enqueue those
               dense contacts for the member-level velocity pass below: it is intentionally
               reserved for dissipating local overlaps inside one solar system. */
            if (!crossCommunity) contacts.push({
              left: left.node, right: right.node, oldDistance: distance,
              leftInverseMass, rightInverseMass, inverseMass,
            });
            stats.correctionDistance += correction;
            stats.overlaps++;
            if (crossCommunity) {
              stats.crossCommunityCorrectionDistance += correction;
              stats.crossCommunityOverlaps++;
            }
          });
        }
      }
    }));
    /* Generic planet/planet pressure should change orbital phase, not silently inflate the
       orbit. For a free server-authored system, map each accumulated local correction onto the
       circular manifold about its declared dominant star. Expressing the tangent displacement
       as an arc (rather than adding the tangent vector as a chord) preserves radius exactly.
       The common mass-weighted shift is then removed from every member, including the star, so
       the free system retains its barycentre. A pointer-owned dominant star is an external
       reservoir and stays exact while its planets still move along their circles; a pointer-
       owned satellite and compatibility systems keep the legacy Cartesian projection. Cross-
       system pressure remains a rigid group translation. */
    const preservedGroups = [];
    if (opts.preserveSystemRadii === true) groups.forEach(group => {
      const anchor = group.anchor;
      const anchorId = anchor && anchor.id !== undefined && anchor.id !== null
        ? String(anchor.id) : '';
      const explicitlyAnchored = anchorId && group.nodes.some(node =>
        node.system_anchor_id !== undefined && node.system_anchor_id !== null
        && String(node.system_anchor_id) === anchorId);
      const fixedMember = opts.fixedNodeId === undefined || opts.fixedNodeId === null
        ? null : group.nodes.find(node => node.id === opts.fixedNodeId) || null;
      const externallyFixedAnchor = fixedMember === anchor;
      if (!anchor || anchor.anchor_role === 'global'
        || (group.fixed && !externallyFixedAnchor) || !explicitlyAnchored) return;
      const entries = group.nodes.map(node => {
        const mass = finitePositive(node.gravity_mass, 1, 1000);
        if (node === anchor) return { node, mass, radius: 0, angle: 0, arc: 0 };
        const dx = node.x - anchor.x, dy = node.y - anchor.y;
        const radius = Math.hypot(dx, dy);
        if (!(radius > 1e-9)) return { node, mass, radius: 0, angle: 0, arc: 0 };
        const shift = shifts.get(node);
        const tangentX = -dy / radius, tangentY = dx / radius;
        return {
          node, mass, radius, angle: Math.atan2(dy, dx),
          arc: shift.x * tangentX + shift.y * tangentY,
        };
      });
      const totalMass = entries.reduce((sum, entry) => sum + entry.mass, 0);
      const contactCount = contacts.reduce((count, contact) =>
        count + (groupForNode.get(contact.left) === group ? 1 : 0), 0);
      if (!(totalMass > 0) || !contactCount) return;
      stats.radialPreservedContacts += contactCount;
      stats.radiusPreservedNodes += entries.filter(entry =>
        entry.radius > 0 && Math.abs(entry.arc) > 1e-12).length;
      preservedGroups.push({ group, anchor, entries, totalMass, externallyFixedAnchor });
      const rotations = entries.map(entry => {
        if (!(entry.radius > 0)) return { entry, x: 0, y: 0 };
        entry.appliedAngle = entry.arc / entry.radius;
        const angle = entry.angle + entry.appliedAngle;
        return { entry,
          x: Math.cos(angle) * entry.radius - (entry.node.x - anchor.x),
          y: Math.sin(angle) * entry.radius - (entry.node.y - anchor.y),
        };
      });
      const driftX = externallyFixedAnchor ? 0 : rotations.reduce(
        (sum, item) => sum + item.entry.mass * item.x, 0) / totalMass;
      const driftY = externallyFixedAnchor ? 0 : rotations.reduce(
        (sum, item) => sum + item.entry.mass * item.y, 0) / totalMass;
      rotations.forEach(item => {
        const shift = shifts.get(item.entry.node);
        shift.x = item.x - driftX;
        shift.y = item.y - driftY;
      });
    });
    groups.forEach(group => group.nodes.forEach(node => {
      const shift = shifts.get(node);
      shift.x += group.shift.x;
      shift.y += group.shift.y;
    }));
    let maximumNodeShift = 0;
    shifts.forEach(shift => {
      maximumNodeShift = Math.max(maximumNodeShift, Math.hypot(shift.x, shift.y));
    });
    const positionScale = maximumCorrection > 0 && maximumNodeShift > maximumCorrection
      ? maximumCorrection / maximumNodeShift : 1;
    const preservedNodes = new Set();
    if (positionScale < 1) preservedGroups.forEach(info => {
      const rotations = info.entries.map(entry => {
        preservedNodes.add(entry.node);
        if (!(entry.radius > 0)) return { entry, x: 0, y: 0 };
        entry.appliedAngle = entry.arc * positionScale / entry.radius;
        const angle = entry.angle + entry.appliedAngle;
        return { entry,
          x: Math.cos(angle) * entry.radius - (entry.node.x - info.anchor.x),
          y: Math.sin(angle) * entry.radius - (entry.node.y - info.anchor.y),
        };
      });
      const driftX = info.externallyFixedAnchor ? 0 : rotations.reduce(
        (sum, item) => sum + item.entry.mass * item.x, 0) / info.totalMass;
      const driftY = info.externallyFixedAnchor ? 0 : rotations.reduce(
        (sum, item) => sum + item.entry.mass * item.y, 0) / info.totalMass;
      rotations.forEach(item => {
        const shift = shifts.get(item.entry.node);
        shift.x = item.x - driftX + info.group.shift.x * positionScale;
        shift.y = item.y - driftY + info.group.shift.y * positionScale;
      });
    });
    shifts.forEach((shift, node) => {
      const scale = preservedNodes.has(node) ? 1 : positionScale;
      node.x += shift.x * scale;
      node.y += shift.y * scale;
    });
    stats.correctionDistance *= positionScale;
    stats.crossCommunityCorrectionDistance *= positionScale;
    stats.maximumNodeShift = maximumNodeShift * positionScale;
    stats.aggregateLimited = positionScale < 1;

    /* The radius vector and its star-relative velocity are one phase-space state. Rotating only
       the position turns a circular tangent partly radial and manufactures eccentricity on the
       next kick. Apply the identical signed angle to each relative velocity, then subtract one
       mass-weighted common velocity from every free member. That common term cancels from every
       planet-minus-star velocity while preserving total system momentum. A fixed star remains
       the external frame and absorbs the corresponding momentum. */
    preservedGroups.forEach(info => {
      const anchorVx = Number.isFinite(info.anchor.vx) ? info.anchor.vx : 0;
      const anchorVy = Number.isFinite(info.anchor.vy) ? info.anchor.vy : 0;
      const rotations = info.entries.map(entry => {
        if (!(entry.radius > 0) || !Number.isFinite(entry.appliedAngle)) {
          return { entry, x: 0, y: 0 };
        }
        const nodeVx = Number.isFinite(entry.node.vx) ? entry.node.vx : 0;
        const nodeVy = Number.isFinite(entry.node.vy) ? entry.node.vy : 0;
        const relativeVx = nodeVx - anchorVx, relativeVy = nodeVy - anchorVy;
        const cosine = Math.cos(entry.appliedAngle), sine = Math.sin(entry.appliedAngle);
        return { entry,
          x: relativeVx * cosine - relativeVy * sine - relativeVx,
          y: relativeVx * sine + relativeVy * cosine - relativeVy,
        };
      });
      const driftX = info.externallyFixedAnchor ? 0 : rotations.reduce(
        (sum, item) => sum + item.entry.mass * item.x, 0) / info.totalMass;
      const driftY = info.externallyFixedAnchor ? 0 : rotations.reduce(
        (sum, item) => sum + item.entry.mass * item.y, 0) / info.totalMass;
      rotations.forEach(item => {
        const shift = velocityShifts.get(item.entry.node);
        shift.x += item.x - driftX;
        shift.y += item.y - driftY;
      });
    });

    /* Recompute same-system normals after the simultaneous projection, then remove only the
       local contact's relative radial motion and the angular momentum manufactured by its
       enlarged lever arm. Cross-system geometry never reaches this velocity pass, so dense
       contacts cannot drain the solar-system COM orbits around the black hole. Velocity
       deltas are accumulated from the unchanged phase and share one cap. */
    const preservedGroupSet = new Set(preservedGroups.map(info => info.group));
    contacts.forEach(contact => {
      /* The circular-manifold solve already resolved this contact without changing orbital
         energy. A Cartesian pair-normal impulse here would reintroduce a star-relative radial
         velocity immediately after the phase-space rotation. */
      if (preservedGroupSet.has(groupForNode.get(contact.left))) return;
      const dx = contact.right.x - contact.left.x;
      const dy = contact.right.y - contact.left.y;
      const distance = Math.hypot(dx, dy);
      if (!(distance > 1e-9)) return;
      const unitX = dx / distance, unitY = dy / distance;
      const tangentX = -unitY, tangentY = unitX;
      const leftDelta = velocityShifts.get(contact.left);
      const rightDelta = velocityShifts.get(contact.right);
      const leftVx = (Number.isFinite(contact.left.vx) ? contact.left.vx : 0) + leftDelta.x;
      const leftVy = (Number.isFinite(contact.left.vy) ? contact.left.vy : 0) + leftDelta.y;
      const rightVx = (Number.isFinite(contact.right.vx) ? contact.right.vx : 0) + rightDelta.x;
      const rightVy = (Number.isFinite(contact.right.vy) ? contact.right.vy : 0) + rightDelta.y;
      const relativeVx = rightVx - leftVx, relativeVy = rightVy - leftVy;
      const normalSpeed = relativeVx * unitX + relativeVy * unitY;
      const tangentSpeed = relativeVx * tangentX + relativeVy * tangentY;
      const tangentScale = opts.preserveTangentialVelocity === true
        ? 1 : Math.min(1, contact.oldDistance / distance);
      const targetNormalSpeed = Math.max(0, normalSpeed);
      const deltaVx = (targetNormalSpeed - normalSpeed) * unitX
        + (tangentSpeed * tangentScale - tangentSpeed) * tangentX;
      const deltaVy = (targetNormalSpeed - normalSpeed) * unitY
        + (tangentSpeed * tangentScale - tangentSpeed) * tangentY;
      leftDelta.x -= deltaVx * contact.leftInverseMass / contact.inverseMass;
      leftDelta.y -= deltaVy * contact.leftInverseMass / contact.inverseMass;
      rightDelta.x += deltaVx * contact.rightInverseMass / contact.inverseMass;
      rightDelta.y += deltaVy * contact.rightInverseMass / contact.inverseMass;
    });
    let maximumVelocityShift = 0;
    velocityShifts.forEach(shift => {
      maximumVelocityShift = Math.max(maximumVelocityShift, Math.hypot(shift.x, shift.y));
    });
    const velocityScale = maximumVelocityCorrection > 0
      && maximumVelocityShift > maximumVelocityCorrection
      ? maximumVelocityCorrection / maximumVelocityShift : 1;
    velocityShifts.forEach((shift, node) => {
      node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + shift.x * velocityScale;
      node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + shift.y * velocityScale;
    });
    stats.maximumVelocityShift = maximumVelocityShift * velocityScale;
    stats.velocityLimited = velocityScale < 1;
    return stats;
  }

  /* The black hole is an impenetrable visual boundary, not a generic collision partner.
     External solar systems cross that boundary as one rigid translation so their local
     geometry and relative velocities survive the contact. Members of the black-hole system
     are handled individually because translating that system would move the anchor itself.

     This is a zero-restitution contact constraint: project only the penetration, remove inward
     radial velocity, and scale BH-frame tangential speed by old/new radius. A grazing body keeps
     essentially all of its orbit, while a deep correction cannot manufacture angular momentum
     or a repulsive slingshot. */
  function applyGalaxyBlackHoleExclusion(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const candidate = galaxyGlobalAnchor(bodies);
    /* Compatibility payloads can omit anchor roles. They still receive a smooth central field,
       but no node is painted as a black hole, so inventing a collision disc would rewrite their
       server coordinates. The hard horizon belongs only to the explicit global anchor. */
    const anchor = candidate && candidate.anchor_role === 'global' ? candidate : null;
    const stats = {
      anchorId: anchor ? anchor.id : null,
      contacts: 0, systems: 0, coreNodes: 0, fixedSystemNodes: 0, repelledNodes: 0,
      correctedDistance: 0, maximumShift: 0, inwardVelocityRemoved: 0,
      tangentialVelocityRemoved: 0,
      minimumClearance: null,
    };
    if (!anchor || bodies.length < 2) return stats;
    const padding = Math.max(0, Number.isFinite(Number(opts.padding))
      ? Number(opts.padding) : GALAXY_BLACK_HOLE_EXCLUSION_PADDING);
    const bodyRadius = node => finitePositive(
      node.radius, evidenceNodeRadius(node, 3), 160
    );
    const anchorRadius = bodyRadius(anchor);
    const anchorX = anchor.x, anchorY = anchor.y;
    const anchorVx = Number.isFinite(anchor.vx) ? anchor.vx : 0;
    const anchorVy = Number.isFinite(anchor.vy) ? anchor.vy : 0;
    const coreKey = communityKey(anchor);
    const radialUnit = (key, dx, dy) => {
      const distance = Math.hypot(dx, dy);
      if (distance > 1e-9) return { x: dx / distance, y: dy / distance, distance };
      const angle = seededHash(0, 'black-hole-horizon:' + String(key))
        / 0x100000000 * Math.PI * 2;
      return { x: Math.cos(angle), y: Math.sin(angle), distance: 0 };
    };
    const stabilizeSystemContactVelocity = (
      members, unitX, unitY, oldDistance, newDistance
    ) => {
      let totalMass = 0, velocityX = 0, velocityY = 0;
      members.forEach(node => {
        const mass = finitePositive(node.gravity_mass, 1, 1000);
        totalMass += mass;
        velocityX += mass * (Number.isFinite(node.vx) ? node.vx : 0);
        velocityY += mass * (Number.isFinite(node.vy) ? node.vy : 0);
      });
      if (!(totalMass > 0)) return { inward: 0, tangential: 0 };
      const relativeVx = velocityX / totalMass - anchorVx;
      const relativeVy = velocityY / totalMass - anchorVy;
      const tangentX = -unitY, tangentY = unitX;
      const radialSpeed = relativeVx * unitX + relativeVy * unitY;
      const tangentialSpeed = relativeVx * tangentX + relativeVy * tangentY;
      const tangentScale = newDistance > 1e-9
        ? Math.max(0, Math.min(1, oldDistance / newDistance)) : 0;
      const targetRadialSpeed = Math.max(0, radialSpeed);
      const targetTangentialSpeed = tangentialSpeed * tangentScale;
      const targetVx = targetRadialSpeed * unitX + targetTangentialSpeed * tangentX;
      const targetVy = targetRadialSpeed * unitY + targetTangentialSpeed * tangentY;
      const shiftVx = targetVx - relativeVx, shiftVy = targetVy - relativeVy;
      members.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + shiftVx;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + shiftVy;
      });
      return {
        inward: Math.max(0, -radialSpeed),
        tangential: Math.abs(tangentialSpeed) * (1 - tangentScale),
      };
    };
    const projectIndividualNode = node => {
      const radial = radialUnit(node.id, node.x - anchorX, node.y - anchorY);
      const minimumDistance = anchorRadius + bodyRadius(node) + padding;
      const correction = minimumDistance - radial.distance;
      if (!(correction > 0) || !Number.isFinite(correction)) return false;
      node.x = anchorX + radial.x * minimumDistance;
      node.y = anchorY + radial.y * minimumDistance;
      if (Number.isFinite(node.fx)) node.fx = node.x;
      if (Number.isFinite(node.fy)) node.fy = node.y;
      const velocity = stabilizeSystemContactVelocity(
        [node], radial.x, radial.y, radial.distance, minimumDistance
      );
      stats.inwardVelocityRemoved += velocity.inward;
      stats.tangentialVelocityRemoved += velocity.tangential;
      stats.contacts++;
      stats.repelledNodes++;
      stats.correctedDistance += correction;
      stats.maximumShift = Math.max(stats.maximumShift, correction);
      return true;
    };

    communityCenters(bodies).forEach(center => {
      if (center.id === coreKey) {
        center.nodes.forEach(node => {
          if (node === anchor) return;
          if (projectIndividualNode(node)) stats.coreNodes++;
        });
        return;
      }

      /* A dragged node is a cursor-owned external source. Rigidly translating its entire
         community when that cursor touches the horizon creates positive feedback: restore
         puts only the source back at the cursor, while every follower retains the displacement
         and inflates the next system radius. Keep the horizon strict per painted member but
         never move those followers as a group. */
      if (center.nodes.some(node => node.id === opts.fixedNodeId)) {
        center.nodes.forEach(node => {
          if (projectIndividualNode(node)) stats.fixedSystemNodes++;
        });
        return;
      }

      /* A circular envelope around the evidence-mass COM is conservative but exact as a
         safety bound: once its near edge clears the black hole, every painted member does. */
      const systemRadius = center.nodes.reduce((maximum, node) => Math.max(maximum,
        Math.hypot(node.x - center.x, node.y - center.y) + bodyRadius(node)), 0);
      const radial = radialUnit(center.id, center.x - anchorX, center.y - anchorY);
      const minimumDistance = anchorRadius + systemRadius + padding;
      const correction = minimumDistance - radial.distance;
      if (!(correction > 0) || !Number.isFinite(correction)) return;
      const shiftX = radial.x * correction, shiftY = radial.y * correction;
      center.nodes.forEach(node => {
        node.x += shiftX;
        node.y += shiftY;
        if (Number.isFinite(node.fx)) node.fx += shiftX;
        if (Number.isFinite(node.fy)) node.fy += shiftY;
      });
      const velocity = stabilizeSystemContactVelocity(
        center.nodes, radial.x, radial.y, radial.distance, minimumDistance
      );
      stats.inwardVelocityRemoved += velocity.inward;
      stats.tangentialVelocityRemoved += velocity.tangential;
      stats.contacts++;
      stats.systems++;
      stats.repelledNodes += center.nodes.length;
      stats.correctedDistance += correction;
      stats.maximumShift = Math.max(stats.maximumShift, correction);
    });

    bodies.forEach(node => {
      if (node === anchor) return;
      const clearance = Math.hypot(node.x - anchorX, node.y - anchorY)
        - anchorRadius - bodyRadius(node) - padding;
      stats.minimumClearance = stats.minimumClearance === null
        ? clearance : Math.min(stats.minimumClearance, clearance);
    });
    return stats;
  }

  function combineGalaxyBlackHoleExclusions(passes) {
    const usable = (passes || []).filter(pass => pass && typeof pass === 'object');
    const last = usable[usable.length - 1] || {
      anchorId: null, contacts: 0, systems: 0, coreNodes: 0, fixedSystemNodes: 0,
      repelledNodes: 0,
      correctedDistance: 0, maximumShift: 0, inwardVelocityRemoved: 0,
      tangentialVelocityRemoved: 0, minimumClearance: null,
    };
    return {
      anchorId: usable.map(pass => pass.anchorId).find(Boolean) || null,
      contacts: usable.reduce((sum, pass) => sum + (pass.contacts || 0), 0),
      systems: usable.reduce((sum, pass) => sum + (pass.systems || 0), 0),
      coreNodes: usable.reduce((sum, pass) => sum + (pass.coreNodes || 0), 0),
      fixedSystemNodes: usable.reduce((sum, pass) => sum + (pass.fixedSystemNodes || 0), 0),
      repelledNodes: usable.reduce((sum, pass) => sum + (pass.repelledNodes || 0), 0),
      correctedDistance: usable.reduce((sum, pass) => sum + (pass.correctedDistance || 0), 0),
      maximumShift: usable.reduce((maximum, pass) => Math.max(maximum,
        pass.maximumShift || 0), 0),
      inwardVelocityRemoved: usable.reduce((sum, pass) => sum + (pass.inwardVelocityRemoved || 0), 0),
      tangentialVelocityRemoved: usable.reduce((sum, pass) => sum
        + (pass.tangentialVelocityRemoved || 0), 0),
      minimumClearance: last.minimumClearance,
    };
  }

  /* Bound only anomalous motion inside each solar system. Free systems are scaled about their
     evidence-mass COM velocity, preserving their exact momentum and black-hole orbit. The
     global anchor is an intentional fixed external frame, so its own velocity remains zero
     while only its satellites are dissipated. A common per-system scale preserves relative
     directions and cannot manufacture a new radial kick. */
  function stabilizeGalaxySystemVelocities(nodes, options) {
    const opts = options || {};
    const limit = Math.max(0.01, Number.isFinite(Number(opts.limit))
      ? Number(opts.limit) : GALAXY_LOCAL_RELATIVE_SPEED_LIMIT);
    const systems = new Map();
    (nodes || []).forEach(node => {
      if (!node || node.ghost || !Number.isFinite(node.vx) || !Number.isFinite(node.vy)) return;
      const key = communityKey(node);
      if (!systems.has(key)) systems.set(key, []);
      systems.get(key).push(node);
    });
    let limitedSystems = 0, maximumRelativeSpeed = 0, minimumScale = 1;
    systems.forEach(members => {
      if (members.length < 2) return;
      const anchor = members.find(node => node.anchor_role === 'global')
        || members.find(node => node.id === opts.fixedNodeId);
      let referenceVx = 0, referenceVy = 0;
      if (!anchor) {
        let totalMass = 0;
        members.forEach(node => {
          const mass = finitePositive(node.gravity_mass, 1, 1000);
          totalMass += mass;
          referenceVx += mass * node.vx;
          referenceVy += mass * node.vy;
        });
        referenceVx /= Math.max(1e-9, totalMass);
        referenceVy /= Math.max(1e-9, totalMass);
      }
      let systemMaximum = 0;
      members.forEach(node => {
        if (node === anchor) return;
        systemMaximum = Math.max(systemMaximum,
          Math.hypot(node.vx - referenceVx, node.vy - referenceVy));
      });
      maximumRelativeSpeed = Math.max(maximumRelativeSpeed, systemMaximum);
      if (!(systemMaximum > limit)) return;
      const scale = limit / systemMaximum;
      members.forEach(node => {
        if (node === anchor) {
          node.vx = 0;
          node.vy = 0;
          return;
        }
        node.vx = referenceVx + (node.vx - referenceVx) * scale;
        node.vy = referenceVy + (node.vy - referenceVy) * scale;
      });
      limitedSystems++;
      minimumScale = Math.min(minimumScale, scale);
    });
    return {
      systems: systems.size, limitedSystems, maximumRelativeSpeed, minimumScale, limit,
    };
  }

  /* Galaxy owns its time integration instead of donating it to D3's alpha clock.  The
     force helpers above are deliberately still useful on their own (and are tested as
     such), so this small adapter samples their acceleration field with a clean velocity
     buffer.  That lets a browser run a fixed kick-drift-kick step without treating an
     alpha decay or a render cadence as physical time.

     `vx`/`vy` are the integrator's velocity slots.  The browser adapter may mirror them
     into private fields before calling this helper, but keeping the pure function on the
     familiar node shape makes deterministic tests and non-DOM embeds straightforward. */
  function galaxyAccelerations(nodes, links, bridges, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const saved = new Map(bodies.map(node => [node, {
      vx: Number.isFinite(node.vx) ? node.vx : 0,
      vy: Number.isFinite(node.vy) ? node.vy : 0,
    }]));
    bodies.forEach(node => { node.vx = 0; node.vy = 0; });
    const gravity = Math.max(0, Number(opts.gravity) || 0);
    const softening = Math.max(0.1, Number(opts.softening) || 8);
    const anchor = galaxyGlobalAnchor(bodies);
    const systemGravity = applyGalaxySystemAnchorGravity(bodies, {
      gravity, softening, alpha: 1,
      accelerationCap: opts.localAccelerationCap,
      fixedNodeId: opts.fixedNodeId,
      repulsionPadding: opts.systemAnchorExclusionPadding,
      repulsionRange: opts.systemAnchorRepulsionRange,
      repulsionAcceleration: opts.systemAnchorRepulsionAcceleration,
    });
    if (opts.central !== false) {
      applyGalaxyBlackHoleGravity(bodies, {
        gravity,
        softening: Math.max(36, Number(opts.centralSoftening) || softening * 5),
        accelerationCap: opts.centralAccelerationCap,
      });
    }
    const mutualGravity = opts.includeMutualSystems === true
      ? applyGalaxyMutualSystemGravity(bodies, {
        gravity,
        strengthFraction: opts.mutualSystemGravityFraction,
        softening: opts.mutualSystemSoftening,
        accelerationCap: opts.mutualSystemAccelerationCap,
        exactLimit: opts.exactLimit,
        theta: opts.theta,
        alpha: 1,
      })
      : { systems: 0, interactions: 0, traversals: 0, approximations: 0,
        maximumAcceleration: 0, capScale: 1 };
    /* Sample the outer restoring field in both leapfrog kicks. External systems receive one
       shared COM acceleration; anchor-community satellites receive their own radial sample so
       neither population can drift through the finite painted edge. */
    const farFieldGravity = opts.includeFarFieldConfinement === false
      ? { anchorId: null, envelopeRadius: 0, softRadius: 0,
        acceleratedSystems: 0, acceleratedCoreNodes: 0, acceleratedFixedFollowers: 0,
        maximumAcceleration: 0 }
      : applyGalaxyFarFieldGravity(bodies, opts);
    /* Cross-system bridges and relation springs are intentionally opt-in at the
       integrator boundary.  A caller that wants the evidence layout enables bridges;
       relation springs stay a weak visual constraint, never an accidental replacement for
       gravity in a pure orbital simulation. */
    if (opts.includeBridges === true) {
      applyCommunityBridgeGravity(bodies, bridges || [], {
        gravity,
        softening: Math.max(24, Number(opts.bridgeSoftening) || softening * 4),
        alpha: 1,
      });
    }
    if (opts.includeRelations === true && opts.includeRelationSprings !== false) {
      applyGalaxyRelationSprings(bodies, links || [], {
        alpha: 1,
        orbitScale: opts.orbitScale,
        forceCap: opts.relationForceCap,
        strengthMultiplier: opts.relationStrengthMultiplier,
        accelerationCap: opts.relationAccelerationCap,
        padding: opts.relationPadding,
        fixedNodeId: opts.fixedNodeId,
        skipFixedNodeRelations: !!opts.dragSource,
        skipSystemAnchorRelations: opts.skipSystemAnchorRelations === true,
        skipOrbitalSystemRelations: opts.skipOrbitalSystemRelations === true,
      });
    }
    const dragGravity = opts.dragSource ? applyDraggedNodeAcceleration(
      opts.dragSource, opts.dragFollowers || [], {
        gravity,
        softening: opts.dragSoftening,
      }
    ) : { applied: 0, maximumAcceleration: 0, maximumPull: 0 };
    if (anchor && (opts.central !== false || anchor.anchor_role === 'global')) {
      /* The global evidence node is the chart's black-hole potential, not a light particle
         that its own bulge can kick. Satellites still receive the local equal field; fixing
         the source prevents that recoil from becoming a fictitious uniform acceleration when
         the next step is expressed in the black-hole frame. */
      anchor.vx = 0;
      anchor.vy = 0;
    }
    const accelerations = new Map(bodies.map(node => [node, {
      ax: Number.isFinite(node.vx) ? node.vx : 0,
      ay: Number.isFinite(node.vy) ? node.vy : 0,
    }]));
    bodies.forEach(node => {
      const velocity = saved.get(node);
      node.vx = velocity.vx;
      node.vy = velocity.vy;
    });
    accelerations.dragGravity = dragGravity;
    accelerations.systemGravity = systemGravity;
    accelerations.mutualGravity = mutualGravity;
    accelerations.farFieldGravity = farFieldGravity;
    return accelerations;
  }

  function galaxyInwardConvergenceFactor(wallClockSeconds, gravitySetting) {
    const elapsed = Number.isFinite(Number(wallClockSeconds))
      ? Math.max(0, Number(wallClockSeconds))
      : GALAXY_FRAME_INTERVAL_MS / 1000;
    return Math.pow(1 - galaxyInwardConvergencePerMinute(gravitySetting),
      elapsed / GALAXY_INWARD_CONVERGENCE_SECONDS);
  }

  /* Project solar-system centres into a monotone, slowly contracting black-hole frame. The
     leapfrog field remains responsible for orbital phase and local structure; every member
     receives the same position/velocity translation, so Link distance can tighten or loosen
     connected nodes without the central boundary crushing their internal orbit. A late outward
     kick can never make an external system fall away from the centre. Each ordinary step follows
     the controlled track exactly. We retain the candidate angle and system tangential velocity.
     An outward attempt receives at least a 110% counter-projection, and only the system COM's
     radial velocity is changed.

     This intentionally does not conserve whole-scene momentum: the global evidence anchor
     is an external black-hole frame, already pinned by `recenterGalaxyOnAnchor`, not a light
     particle that recoils. Keeping that caveat here prevents a future "conservative" cleanup
     from silently restoring outward drift. */
  function applyGalaxyInwardConvergence(bodies, anchor, initialRadii, options) {
    const opts = options || {};
    if (!anchor || !initialRadii || typeof initialRadii.get !== 'function') {
      return { applied: 0, outwardCandidates: 0, overrides: 0, factor: 1 };
    }
    const anchorX = Number.isFinite(anchor.x) ? anchor.x : 0;
    const anchorY = Number.isFinite(anchor.y) ? anchor.y : 0;
    const factor = galaxyInwardConvergenceFactor(opts.wallClockSeconds, opts.gravity);
    const timestep = Number.isFinite(Number(opts.timestep))
      ? Math.max(0.001, Number(opts.timestep)) : GALAXY_FIXED_TIMESTEP;
    let applied = 0, outwardCandidates = 0, overrides = 0;
    communityCenters(bodies).forEach(center => {
      if (!center || center.nodes.includes(anchor)
        || center.nodes.some(node => node.anchor_role === 'global'
          || node.id === opts.fixedNodeId)) return;
      const initialState = initialRadii.get(center.id);
      const initialRadius = Number(initialState && typeof initialState === 'object'
        ? initialState.radius : initialState);
      if (!Number.isFinite(initialRadius)
        || !Number.isFinite(center.x) || !Number.isFinite(center.y)) return;
      const dx = center.x - anchorX, dy = center.y - anchorY;
      const candidateRadius = Math.hypot(dx, dy);
      if (!Number.isFinite(candidateRadius)) return;
      const scheduledRadius = initialRadius * factor;
      const outwardDistance = Math.max(0, candidateRadius - initialRadius);
      /* Follow the gravity-selected track exactly. For an outward attempted move, require
         a final position at least 10% of that attempted distance inward from the starting
         radius, even when that is more inward than the scheduled track. */
      const outwardCeiling = initialRadius - outwardDistance * GALAXY_OUTWARD_OVERRIDE;
      const finalRadius = Math.max(0, outwardDistance > 0
        ? Math.min(scheduledRadius, outwardCeiling) : scheduledRadius);
      const unitX = candidateRadius > 1e-9 ? dx / candidateRadius : 1;
      const unitY = candidateRadius > 1e-9 ? dy / candidateRadius : 0;
      const finalX = anchorX + unitX * finalRadius;
      const finalY = anchorY + unitY * finalRadius;
      const shiftX = finalX - center.x, shiftY = finalY - center.y;
      let centerVx = 0, centerVy = 0;
      center.nodes.forEach(node => {
        const mass = finitePositive(node.gravity_mass, 1, 1000);
        centerVx += mass * (Number.isFinite(node.vx) ? node.vx : 0);
        centerVy += mass * (Number.isFinite(node.vy) ? node.vy : 0);
      });
      centerVx /= Math.max(1e-9, center.mass);
      centerVy /= Math.max(1e-9, center.mass);
      const tangentVelocity = centerVx * -unitY + centerVy * unitX;
      /* The system radial component follows the projection's actual displacement. Relative
         positions and velocities are untouched, preserving local gravity and link springs. */
      const radialVelocity = (finalRadius - initialRadius) / timestep;
      const targetVx = radialVelocity * unitX - tangentVelocity * unitY;
      const targetVy = radialVelocity * unitY + tangentVelocity * unitX;
      const velocityShiftX = targetVx - centerVx;
      const velocityShiftY = targetVy - centerVy;
      center.nodes.forEach(node => {
        node.x += shiftX;
        node.y += shiftY;
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + velocityShiftX;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + velocityShiftY;
      });
      if (outwardDistance > 0) {
        outwardCandidates++;
        overrides++;
      }
      applied += center.nodes.length;
    });
    return { applied, outwardCandidates, overrides, factor };
  }

  /* The black-hole Plummer field deliberately stays gentle at the outer edge so seeded
     tangential motion remains legible.  This separate field is an equally smooth, *system*
     level restoring term in the narrow outer band.  It is not fitted from live coordinates:
     the painted extent is derived once from scene hints and retained on the explicit global
     anchor, so one bad outward kick cannot make the galaxy's permitted radius grow with it. */
  function galaxyFarFieldEnvelope(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const candidate = galaxyGlobalAnchor(bodies);
    const anchor = candidate && candidate.anchor_role === 'global' ? candidate : null;
    const empty = {
      anchor: null, centers: [], coreKey: null, envelopeRadius: 0, softRadius: 0,
    };
    if (!anchor) return empty;
    const centers = [...communityCenters(bodies).values()];
    const coreKey = communityKey(anchor);
    const bodyRadius = node => finitePositive(node.radius, evidenceNodeRadius(node, 3), 160);
    const systemRadius = center => center.nodes.reduce((maximum, node) => Math.max(maximum,
      Math.hypot(node.x - center.x, node.y - center.y) + bodyRadius(node)), 0);
    const seededRadius = node => ['galactic_target_radius', 'galactic_radius', 'orbit_radius']
      .reduce((maximum, key) => {
        const value = Number(node[key]);
        return Number.isFinite(value) && value > 0 ? Math.max(maximum, value) : maximum;
      }, 0);
    const anchorRadius = bodyRadius(anchor);
    let hintedExtent = 0, observedExtent = 0, horizonExtent = anchorRadius;
    let hasHint = false;
    centers.forEach(center => {
      const extent = systemRadius(center);
      const radial = Math.hypot(center.x - anchor.x, center.y - anchor.y);
      const hint = center.nodes.reduce((maximum, node) => Math.max(maximum, seededRadius(node)), 0);
      if (center.id === coreKey) {
        center.nodes.forEach(node => {
          if (node === anchor) return;
          const radius = bodyRadius(node);
          const nodeHint = seededRadius(node);
          if (nodeHint > 0) {
            hintedExtent = Math.max(hintedExtent, nodeHint + radius);
            hasHint = true;
          }
          observedExtent = Math.max(observedExtent,
            Math.hypot(node.x - anchor.x, node.y - anchor.y) + radius);
          /* The eventual outer edge must leave enough radial room for both sides of this
             satellite's painted body at the inner horizon. */
          horizonExtent = Math.max(horizonExtent,
            anchorRadius + radius * 2 + GALAXY_BLACK_HOLE_EXCLUSION_PADDING);
        });
      } else {
        /* A declared system orbit plus the real painted system radius is a hard geometric
           seed. Include the radius even when a stale payload claims a tiny orbit. */
        if (hint > 0) {
          hintedExtent = Math.max(hintedExtent, hint + extent);
          hasHint = true;
        }
        observedExtent = Math.max(observedExtent, radial + extent);
        /* A rigid system that is just clear of the black hole extends one full system radius
           again on its far side. Reserve that geometry before caching the finite envelope. */
        horizonExtent = Math.max(horizonExtent,
          anchorRadius + extent * 2 + GALAXY_BLACK_HOLE_EXCLUSION_PADDING);
      }
    });
    const configuredMinimum = Number.isFinite(Number(opts.farFieldMinimumRadius))
      ? Number(opts.farFieldMinimumRadius) : GALAXY_FAR_FIELD_MIN_RADIUS;
    const minimumRadius = Math.max(1, configuredMinimum, horizonExtent);
    const scale = Math.max(1, Number.isFinite(Number(opts.farFieldEnvelopeScale))
      ? Number(opts.farFieldEnvelopeScale) : GALAXY_FAR_FIELD_ENVELOPE_SCALE);
    const explicitRadius = Number(opts.farFieldEnvelopeRadius);
    const weakCached = galaxyFarFieldEnvelopeCache
      ? galaxyFarFieldEnvelopeCache.get(anchor) : undefined;
    const propCached = anchor.__galaxyFarFieldEnvelope;
    const cachedRadius = Number(
      Number.isFinite(Number(weakCached)) && Number(weakCached) > 0 ? weakCached : propCached
    );
    const seedExtent = Math.max(minimumRadius,
      hasHint ? hintedExtent : observedExtent);
    const envelopeRadius = Number.isFinite(explicitRadius) && explicitRadius > 0
      ? Math.max(minimumRadius, explicitRadius)
      : Number.isFinite(cachedRadius) && cachedRadius > 0 ? cachedRadius
        : Math.max(minimumRadius, seedExtent * scale);
    if (!(Number.isFinite(cachedRadius) && cachedRadius > 0)
      && !(Number.isFinite(explicitRadius) && explicitRadius > 0)) {
      if (galaxyFarFieldEnvelopeCache) galaxyFarFieldEnvelopeCache.set(anchor, envelopeRadius);
      try {
        Object.defineProperty(anchor, '__galaxyFarFieldEnvelope', {
          value: envelopeRadius, writable: false, configurable: true, enumerable: false,
        });
      } catch (error) { /* Frozen compatibility nodes keep the WeakMap value above. */ }
    }
    const softFraction = Math.max(0, Math.min(1, Number.isFinite(Number(opts.farFieldSoftFraction))
      ? Number(opts.farFieldSoftFraction) : GALAXY_FAR_FIELD_SOFT_FRACTION));
    const requestedBand = Number(opts.farFieldSoftBand);
    const softBand = Number.isFinite(requestedBand) && requestedBand > 0
      ? Math.min(envelopeRadius, requestedBand)
      : Math.max(16, Math.min(32, envelopeRadius * (1 - softFraction)));
    return {
      anchor, centers, coreKey, bodyRadius, systemRadius,
      envelopeRadius, softRadius: Math.max(0, envelopeRadius - softBand),
    };
  }

  function applyGalaxyFarFieldGravity(nodes, options) {
    const opts = options || {};
    const field = galaxyFarFieldEnvelope(nodes, opts);
    const stats = {
      anchorId: field.anchor ? field.anchor.id : null,
      envelopeRadius: field.envelopeRadius, softRadius: field.softRadius,
      acceleratedSystems: 0, acceleratedCoreNodes: 0, acceleratedFixedFollowers: 0,
      maximumAcceleration: 0,
    };
    if (!field.anchor || opts.includeFarFieldConfinement === false) return stats;
    const acceleration = Math.max(0, Number.isFinite(Number(opts.farFieldAcceleration))
      ? Number(opts.farFieldAcceleration) : GALAXY_FAR_FIELD_ACCELERATION);
    const accelerationCap = Math.max(0, Number.isFinite(Number(opts.farFieldMaxAcceleration))
      ? Number(opts.farFieldMaxAcceleration) : GALAXY_FAR_FIELD_MAX_ACCELERATION);
    const band = Math.max(1e-9, field.envelopeRadius - field.softRadius);
    const accelerate = (members, key, dx, dy, outerRadius, scope) => {
      if (!(outerRadius > field.softRadius)) return;
      const distance = Math.hypot(dx, dy);
      let unitX = 1, unitY = 0;
      if (distance > 1e-9) {
        unitX = dx / distance;
        unitY = dy / distance;
      } else {
        const angle = seededHash(0, 'far-field:' + String(key)) / 0x100000000 * Math.PI * 2;
        unitX = Math.cos(angle);
        unitY = Math.sin(angle);
      }
      const ratio = (outerRadius - field.softRadius) / band;
      const magnitude = Math.min(acceleration,
        accelerationCap > 0 ? accelerationCap : acceleration,
        acceleration * galaxySmoothstep(ratio));
      if (!(magnitude > 0) || !Number.isFinite(magnitude)) return;
      members.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) - unitX * magnitude;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) - unitY * magnitude;
      });
      if (scope === 'core') stats.acceleratedCoreNodes += members.length;
      else if (scope === 'fixed') stats.acceleratedFixedFollowers += members.length;
      else stats.acceleratedSystems++;
      stats.maximumAcceleration = Math.max(stats.maximumAcceleration, magnitude);
    };
    field.centers.forEach(center => {
      if (center.id === field.coreKey) {
        center.nodes.forEach(node => {
          if (node === field.anchor || node.id === opts.fixedNodeId) return;
          const dx = node.x - field.anchor.x, dy = node.y - field.anchor.y;
          accelerate([node], node.id, dx, dy,
            Math.hypot(dx, dy) + field.bodyRadius(node), 'core');
        });
        return;
      }
      if (center.nodes.some(node => node.id === opts.fixedNodeId)) {
        /* Preserve the cursor-owned source exactly, but do not make its companions immune to
           the smooth outer well. They get their own radial sample until the hard cap is needed. */
        center.nodes.forEach(node => {
          if (node.id === opts.fixedNodeId) return;
          const dx = node.x - field.anchor.x, dy = node.y - field.anchor.y;
          accelerate([node], node.id, dx, dy,
            Math.hypot(dx, dy) + field.bodyRadius(node), 'fixed');
        });
        return;
      }
      const dx = center.x - field.anchor.x, dy = center.y - field.anchor.y;
      accelerate(center.nodes, center.id, dx, dy,
        Math.hypot(dx, dy) + field.systemRadius(center), 'system');
    });
    return stats;
  }

  /* Exact outer counterpart to the black-hole contact. External systems are translated as
     rigid bodies; anchor-community satellites are projected one at a time so the anchor never
     moves. In either case only outward radial COM velocity is removed. Because this correction
     moves inward, tangential speed is retained rather than increased (a cap must not inject
     angular energy). An oversized system has a rare per-member fallback, since no rigid
     translation can fit a radius larger than the finite envelope. */
  function applyGalaxyFarFieldConfinement(nodes, options) {
    const opts = options || {};
    const field = galaxyFarFieldEnvelope(nodes, opts);
    const stats = {
      anchorId: field.anchor ? field.anchor.id : null,
      envelopeRadius: field.envelopeRadius, softRadius: field.softRadius,
      acceleratedSystems: 0, boundedSystems: 0, boundedCoreNodes: 0,
      boundedFixedSource: 0, boundedFixedFollowers: 0, boundedDeformedSystems: 0,
      boundedOversizedNodes: 0,
      correctedDistance: 0, maximumShift: 0, outwardVelocityRemoved: 0,
      tangentialVelocityRemoved: 0,
      annulus: { anchorId: null, innerCorrectedNodes: 0, outerCorrectedNodes: 0,
        infeasibleNodes: 0 },
    };
    if (!field.anchor || opts.includeFarFieldConfinement === false) return stats;
    const anchorX = field.anchor.x, anchorY = field.anchor.y;
    const anchorVx = Number.isFinite(field.anchor.vx) ? field.anchor.vx : 0;
    const anchorVy = Number.isFinite(field.anchor.vy) ? field.anchor.vy : 0;
    const radial = (key, dx, dy) => {
      const distance = Math.hypot(dx, dy);
      if (distance > 1e-9) return { x: dx / distance, y: dy / distance, distance };
      const angle = seededHash(0, 'far-field-boundary:' + String(key))
        / 0x100000000 * Math.PI * 2;
      return { x: Math.cos(angle), y: Math.sin(angle), distance: 0 };
    };
    const stabilizeVelocity = (members, unitX, unitY, oldDistance, newDistance) => {
      let mass = 0, velocityX = 0, velocityY = 0;
      members.forEach(node => {
        const nodeMass = finitePositive(node.gravity_mass, 1, 1000);
        mass += nodeMass;
        velocityX += nodeMass * (Number.isFinite(node.vx) ? node.vx : 0);
        velocityY += nodeMass * (Number.isFinite(node.vy) ? node.vy : 0);
      });
      if (!(mass > 0)) return { outward: 0, tangential: 0 };
      const relativeX = velocityX / mass - anchorVx;
      const relativeY = velocityY / mass - anchorVy;
      const tangentX = -unitY, tangentY = unitX;
      const radialSpeed = relativeX * unitX + relativeY * unitY;
      const tangentSpeed = relativeX * tangentX + relativeY * tangentY;
      const tangentScale = newDistance > 1e-9
        ? Math.max(0, Math.min(1, oldDistance / newDistance)) : 0;
      const targetRadial = Math.min(0, radialSpeed);
      const targetTangent = tangentSpeed * tangentScale;
      const targetX = targetRadial * unitX + targetTangent * tangentX;
      const targetY = targetRadial * unitY + targetTangent * tangentY;
      const shiftX = targetX - relativeX, shiftY = targetY - relativeY;
      members.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + shiftX;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + shiftY;
      });
      return {
        outward: Math.max(0, radialSpeed),
        tangential: Math.abs(tangentSpeed) * (1 - tangentScale),
      };
    };
    field.centers.forEach(center => {
      if (center.id === field.coreKey) {
        center.nodes.forEach(node => {
          if (node === field.anchor || node.id === opts.fixedNodeId) return;
          const unit = radial(node.id, node.x - anchorX, node.y - anchorY);
          const targetDistance = Math.max(0, field.envelopeRadius - field.bodyRadius(node));
          const correction = unit.distance - targetDistance;
          if (!(correction > 0)) return;
          node.x = anchorX + unit.x * targetDistance;
          node.y = anchorY + unit.y * targetDistance;
          if (Number.isFinite(node.fx)) node.fx = node.x;
          if (Number.isFinite(node.fy)) node.fy = node.y;
          const velocity = stabilizeVelocity([node], unit.x, unit.y,
            unit.distance, targetDistance);
          stats.boundedCoreNodes++;
          stats.correctedDistance += correction;
          stats.maximumShift = Math.max(stats.maximumShift, correction);
          stats.outwardVelocityRemoved += velocity.outward;
          stats.tangentialVelocityRemoved += velocity.tangential;
        });
        return;
      }
      if (center.nodes.some(node => node.id === opts.fixedNodeId)) {
        /* Pointer coordinates are an input target, not permission to paint outside the finite
           galaxy. Cap this stretched system one body at a time—including the source—so a long
           outward hold cannot create release-only geometry. The next pointer event supplies a
           fresh target; its final painted fx/fy remains on the outer annulus. */
        center.nodes.forEach(node => {
          const unit = radial(node.id, node.x - anchorX, node.y - anchorY);
          const targetDistance = Math.max(0, field.envelopeRadius - field.bodyRadius(node));
          const correction = unit.distance - targetDistance;
          if (!(correction > 0)) return;
          node.x = anchorX + unit.x * targetDistance;
          node.y = anchorY + unit.y * targetDistance;
          if (Number.isFinite(node.fx)) node.fx = node.x;
          if (Number.isFinite(node.fy)) node.fy = node.y;
          const velocity = stabilizeVelocity([node], unit.x, unit.y,
            unit.distance, targetDistance);
          if (node.id === opts.fixedNodeId) stats.boundedFixedSource++;
          else stats.boundedFixedFollowers++;
          stats.correctedDistance += correction;
          stats.maximumShift = Math.max(stats.maximumShift, correction);
          stats.outwardVelocityRemoved += velocity.outward;
          stats.tangentialVelocityRemoved += velocity.tangential;
        });
        return;
      }
      const unit = radial(center.id, center.x - anchorX, center.y - anchorY);
      const radius = field.systemRadius(center);
      /* A compact system fits inside R after one COM translation. A just-released drag can
         leave a source at the cursor and companions at the cap, making q_s >= R; translating
         that stretched geometry by its COM would throw the already-safe follower hundreds of
         units. Resolve that impossible rigid fit member-by-member for this slice instead. */
      if (radius >= field.envelopeRadius - 1e-9) {
        let bounded = false;
        center.nodes.forEach(node => {
          const memberUnit = radial(node.id, node.x - anchorX, node.y - anchorY);
          const targetDistance = Math.max(0, field.envelopeRadius - field.bodyRadius(node));
          const correction = memberUnit.distance - targetDistance;
          if (!(correction > 1e-9)) return;
          node.x = anchorX + memberUnit.x * targetDistance;
          node.y = anchorY + memberUnit.y * targetDistance;
          if (Number.isFinite(node.fx)) node.fx = node.x;
          if (Number.isFinite(node.fy)) node.fy = node.y;
          const velocity = stabilizeVelocity([node], memberUnit.x, memberUnit.y,
            memberUnit.distance, targetDistance);
          stats.boundedOversizedNodes++;
          stats.correctedDistance += correction;
          stats.maximumShift = Math.max(stats.maximumShift, correction);
          stats.outwardVelocityRemoved += velocity.outward;
          stats.tangentialVelocityRemoved += velocity.tangential;
          bounded = true;
        });
        if (bounded) stats.boundedDeformedSystems++;
        return;
      }
      const targetDistance = Math.max(0, field.envelopeRadius - radius);
      const correction = unit.distance - targetDistance;
      if (!(correction > 0)) return;
      const shiftX = -unit.x * correction, shiftY = -unit.y * correction;
      center.nodes.forEach(node => {
        node.x += shiftX;
        node.y += shiftY;
        if (Number.isFinite(node.fx)) node.fx += shiftX;
        if (Number.isFinite(node.fy)) node.fy += shiftY;
      });
      const velocity = stabilizeVelocity(center.nodes, unit.x, unit.y,
        unit.distance, targetDistance);
      stats.boundedSystems++;
      stats.correctedDistance += correction;
      stats.maximumShift = Math.max(stats.maximumShift, correction);
      stats.outwardVelocityRemoved += velocity.outward;
      stats.tangentialVelocityRemoved += velocity.tangential;
    });
    /* The COM/system-radius projection above is exact whenever q_s <= R. If an extreme late
       local deformation has made q_s > R, fitting it rigidly is mathematically impossible.
       Finish with a member-level cap so the public invariant remains every free painted node
       lies inside the cached envelope; normal systems never enter this branch. */
    field.centers.forEach(center => {
      center.nodes.forEach(node => {
        if (node === field.anchor || node.id === opts.fixedNodeId) return;
        const unit = radial(node.id, node.x - anchorX, node.y - anchorY);
        const targetDistance = Math.max(0, field.envelopeRadius - field.bodyRadius(node));
        const correction = unit.distance - targetDistance;
        if (!(correction > 1e-9)) return;
        node.x = anchorX + unit.x * targetDistance;
        node.y = anchorY + unit.y * targetDistance;
        if (Number.isFinite(node.fx)) node.fx = node.x;
        if (Number.isFinite(node.fy)) node.fy = node.y;
        const velocity = stabilizeVelocity([node], unit.x, unit.y,
          unit.distance, targetDistance);
        stats.boundedOversizedNodes++;
        stats.correctedDistance += correction;
        stats.maximumShift = Math.max(stats.maximumShift, correction);
        stats.outwardVelocityRemoved += velocity.outward;
        stats.tangentialVelocityRemoved += velocity.tangential;
      });
    });
    return stats;
  }

  /* Last coordinate check after alternating the two system-level contacts. A normal scene is
     already feasible (the cached envelope reserved its horizon geometry), so this is a no-op.
     It exists for a pathological late deformation whose system radius grew beyond that cache:
     individual members are then the only way to satisfy both painted edges at once. A dragged
     source is likewise clamped here: its pointer target is preserved as input, while the final
     painted coordinate always remains inside the finite annulus. */
  function applyGalaxyAnnularBounds(nodes, options) {
    const opts = options || {};
    const field = galaxyFarFieldEnvelope(nodes, opts);
    const stats = { anchorId: field.anchor ? field.anchor.id : null,
      innerCorrectedNodes: 0, outerCorrectedNodes: 0, infeasibleNodes: 0 };
    if (!field.anchor || opts.includeFarFieldConfinement === false) return stats;
    const anchorX = field.anchor.x, anchorY = field.anchor.y;
    const anchorRadius = field.bodyRadius(field.anchor);
    const padding = Math.max(0, Number.isFinite(Number(opts.blackHoleExclusionPadding))
      ? Number(opts.blackHoleExclusionPadding) : GALAXY_BLACK_HOLE_EXCLUSION_PADDING);
    field.centers.forEach(center => center.nodes.forEach(node => {
      if (node === field.anchor) return;
      const dx = node.x - anchorX, dy = node.y - anchorY;
      const distance = Math.hypot(dx, dy);
      const radius = field.bodyRadius(node);
      const lower = anchorRadius + radius + padding;
      const upper = field.envelopeRadius - radius;
      if (!(upper >= lower)) {
        /* This can only arise from an externally forced, mathematically impossible geometry.
           Keep the black-hole edge authoritative rather than emitting a non-finite position. */
        stats.infeasibleNodes++;
        return;
      }
      const target = Math.max(lower, Math.min(upper, distance));
      if (!(Math.abs(target - distance) > 1e-9)) return;
      let unitX = 1, unitY = 0;
      if (distance > 1e-9) {
        unitX = dx / distance;
        unitY = dy / distance;
      } else {
        const angle = seededHash(0, 'galaxy-annulus:' + String(node.id))
          / 0x100000000 * Math.PI * 2;
        unitX = Math.cos(angle);
        unitY = Math.sin(angle);
      }
      node.x = anchorX + unitX * target;
      node.y = anchorY + unitY * target;
      if (Number.isFinite(node.fx)) node.fx = node.x;
      if (Number.isFinite(node.fy)) node.fy = node.y;
      const vx = (Number.isFinite(node.vx) ? node.vx : 0)
        - (Number.isFinite(field.anchor.vx) ? field.anchor.vx : 0);
      const vy = (Number.isFinite(node.vy) ? node.vy : 0)
        - (Number.isFinite(field.anchor.vy) ? field.anchor.vy : 0);
      const tangentX = -unitY, tangentY = unitX;
      const radialSpeed = vx * unitX + vy * unitY;
      const tangentSpeed = vx * tangentX + vy * tangentY;
      const tangentScale = target > 1e-9 ? Math.max(0, Math.min(1, distance / target)) : 0;
      const targetRadial = target > distance ? Math.max(0, radialSpeed)
        : Math.min(0, radialSpeed);
      node.vx = (Number.isFinite(field.anchor.vx) ? field.anchor.vx : 0)
        + targetRadial * unitX + tangentSpeed * tangentScale * tangentX;
      node.vy = (Number.isFinite(field.anchor.vy) ? field.anchor.vy : 0)
        + targetRadial * unitY + tangentSpeed * tangentScale * tangentY;
      if (target > distance) stats.innerCorrectedNodes++;
      else stats.outerCorrectedNodes++;
    }));
    return stats;
  }

  /* One deterministic velocity-Verlet / leapfrog step.  The time step is intentionally
     dimensionless: the force constants were calibrated in force-graph tick units, so a
     value of one is the physically equivalent fixed replacement for one former D3 tick.
     A caller can substep at a stable wall-clock cadence without ever scaling force by D3
     alpha.  Collision impulses happen after the second kick and the damping is a property
     of this integrator, not a side effect of D3's simulation. */
  function integrateGalaxyLeapfrog(nodes, links, bridges, options) {
    // kick-drift-kick: sample at x(t), drift from the half kick, then close at x(t + dt).
    const opts = options || {};
    /* Pointer coordinates are already expressed in the currently rendered chart frame. Do
       not translate that frame underneath an active drag: it remains the source target while
       every other body integrates around it. The final inner/outer annulus may clamp the
       painted source edge; once released, the next ordinary step may recenter normally. */
    const requestedFixedNode = opts.fixedNodeId == null ? null : (nodes || []).find(
      node => node && !node.ghost && node.id === opts.fixedNodeId
        && Number.isFinite(node.x) && Number.isFinite(node.y)
    ) || null;
    const anchorFrame = opts.central !== false || (nodes || []).some(
      node => node && !node.ghost && node.anchor_role === 'global'
    );
    const recenterFrame = anchorFrame && !requestedFixedNode;
    if (recenterFrame) recenterGalaxyOnAnchor(nodes);
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const fixedNode = requestedFixedNode && bodies.includes(requestedFixedNode)
      ? requestedFixedNode : null;
    const fixedPhase = fixedNode ? { x: fixedNode.x, y: fixedNode.y } : null;
    const restoreFixedNode = () => {
      if (!fixedNode || !fixedPhase) return;
      fixedNode.x = fixedPhase.x;
      fixedNode.y = fixedPhase.y;
      fixedNode.vx = 0;
      fixedNode.vy = 0;
    };
    const timestep = Math.max(0.001, Math.min(2, Number(opts.timestep) || 1));
    const velocityDecay = Math.max(0, Math.min(0.99,
      Number.isFinite(Number(opts.velocityDecay)) ? Number(opts.velocityDecay) : 0.002));
    const speedLimit = Math.max(0.01, Number(opts.speedLimit) || MAX_NODE_SPEED);
    if (!bodies.length) return { bodies: 0, collisions: 0, kinetic: 0 };
    const horizonEnabled = anchorFrame && opts.includeBlackHoleExclusion !== false;
    const projectBlackHoleHorizon = () => horizonEnabled
      ? applyGalaxyBlackHoleExclusion(bodies, {
        padding: opts.blackHoleExclusionPadding,
        fixedNodeId: opts.fixedNodeId,
      })
      : {
        anchorId: null, contacts: 0, systems: 0, coreNodes: 0, fixedSystemNodes: 0,
        repelledNodes: 0,
        correctedDistance: 0, maximumShift: 0, inwardVelocityRemoved: 0,
        tangentialVelocityRemoved: 0,
        minimumClearance: null,
      };
    /* Fresh payloads and pointer updates may begin a slice inside the boundary. Repair that
       phase before either acceleration sample or the convergence track observes it. */
    const initialHorizon = projectBlackHoleHorizon();
    const precomputedCenters = communityCenters(bodies);
    const convergenceAnchor = opts.inwardConvergence === true ? galaxyGlobalAnchor(bodies) : null;
    const initialRadii = convergenceAnchor ? new Map(
      [...precomputedCenters.entries()].map(([id, center]) => [id, {
        radius: Math.hypot(center.x - convergenceAnchor.x,
          center.y - convergenceAnchor.y),
      }])
    ) : null;

    const start = galaxyAccelerations(bodies, links, bridges, opts);
    bodies.forEach(node => {
      if (node === fixedNode) {
        node.vx = 0;
        node.vy = 0;
        return;
      }
      const acceleration = start.get(node) || { ax: 0, ay: 0 };
      node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + acceleration.ax * timestep * 0.5;
      node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + acceleration.ay * timestep * 0.5;
      node.x += node.vx * timestep;
      node.y += node.vy * timestep;
    });
    /* Clamp before the second force sample so a tunnelling body never contributes an
       acceleration from inside the painted black-hole disc. */
    const driftHorizon = projectBlackHoleHorizon();
    const end = galaxyAccelerations(bodies, links, bridges, opts);
    bodies.forEach(node => {
      if (node === fixedNode) return;
      const acceleration = end.get(node) || { ax: 0, ay: 0 };
      node.vx += acceleration.ax * timestep * 0.5;
      node.vy += acceleration.ay * timestep * 0.5;
    });
    const collision = opts.includeCollisions === false ? { overlaps: 0 }
      : applyGalaxyCollisions(bodies, {
        padding: opts.collisionPadding,
        strength: opts.collisionStrength,
        iterations: opts.collisionIterations,
      });
    /* Decay is expressed per full fixed tick, then exponentiated for substeps. This avoids
       changing the physical settling rate merely because a slow frame consumed two steps. */
    const dampingFactor = Math.pow(1 - velocityDecay, timestep);
    let maximumSpeed = 0;
    bodies.forEach(node => {
      node.vx = (Number.isFinite(node.vx) ? node.vx : 0) * dampingFactor;
      node.vy = (Number.isFinite(node.vy) ? node.vy : 0) * dampingFactor;
    });
    /* Work in the chart's black-hole frame. Translation by the dominant node's phase changes
       no relative orbit, while guaranteeing the visual/physical anchor is exactly 0/0/0/0. */
    if (recenterFrame) recenterGalaxyOnAnchor(nodes);
    const relationConstraint = opts.includeRelations === true
      ? applyGalaxyRelationDistanceConstraints(bodies, links || [], {
        orbitScale: opts.orbitScale,
        /* Standalone callers historically supplied one relation multiplier. The live engine
           splits spring and PBD calibration, but the older option remains the fallback. */
        strengthMultiplier: Number.isFinite(Number(opts.relationConstraintStrengthMultiplier))
          ? Number(opts.relationConstraintStrengthMultiplier)
          : opts.relationStrengthMultiplier,
        responseMultiplier: opts.relationConstraintResponseMultiplier,
        wallClockSeconds: opts.wallClockSeconds,
        rate: opts.relationConstraintRate,
        maxCorrection: opts.relationConstraintMaxCorrection,
        padding: opts.relationPadding,
        fixedNodeId: opts.fixedNodeId,
        skipFixedNodeRelations: !!opts.dragSource,
        skipSystemAnchorRelations: opts.skipSystemAnchorRelations === true,
        skipOrbitalSystemRelations: opts.skipOrbitalSystemRelations === true,
      })
      : { applied: 0, maximumError: 0, correctedDistance: 0 };
    /* Orbital separation is a dissipative close-range pressure, not negative gravity. It uses
       full pressure inside a solar system and a weak contact-only pressure across systems,
       preserves evidence-mass momentum, and removes closing energy instead of injecting a
       repulsive slingshot. Applying it after Link constraints makes separation the final local
       safety envelope before the strict black-hole horizon pass. */
    const orbitalSeparation = opts.includeOrbitalSeparation === true
      ? applyGalaxyOrbitalSeparation(bodies, {
        padding: opts.orbitalSeparationPadding,
        strength: opts.orbitalSeparationStrength,
        crossCommunityPadding: opts.crossCommunitySeparationPadding,
        crossCommunityStrength: opts.crossCommunitySeparationStrength,
        maxCorrection: opts.orbitalSeparationMaxCorrection,
        maxVelocityCorrection: opts.orbitalSeparationMaxVelocityCorrection,
        preserveTangentialVelocity: opts.preserveLocalTangentialVelocity === true,
        preserveSystemRadii: opts.preserveSystemRadii === true,
        skipSystemAnchorPairs: opts.skipSystemAnchorPairs === true,
        fixedNodeId: opts.fixedNodeId,
      })
      : { bodies: bodies.length, pairs: 0, overlaps: 0, cells: 0, correctionDistance: 0 };
    /* Leapfrog acceleration alone is intentionally gentle at the tiny live timestep. While a
       pointer owns a mass, add one bounded wall-clock projection from that same softened field
       so nearby unlinked bodies visibly follow instead of appearing frozen. This runs once per
       physics slice (never per pointer event), injects no velocity, and remains inverse-square
       and evidence-mass weighted. */
    const dragPositionGravity = opts.dragSource ? applyDraggedNodeGravity(
      opts.dragSource, opts.dragFollowers || [], {
        gravity: opts.gravity,
        gravityMultiplier: GALAXY_DRAG_GRAVITY_MULTIPLIER,
        softening: opts.dragSoftening,
        duration: Number.isFinite(Number(opts.wallClockSeconds))
          ? Number(opts.wallClockSeconds) : GALAXY_FRAME_INTERVAL_MS / 1000,
        maximumPull: GALAXY_DRAG_POSITION_MAX_PULL,
        maximumImpulse: 0,
        applyImpulse: false,
        linkSetting: opts.linkSetting,
        padding: opts.relationPadding,
      }
    ) : { applied: 0, maximumAcceleration: 0, maximumPull: 0 };
    const systemVelocity = stabilizeGalaxySystemVelocities(bodies, {
      limit: opts.localRelativeSpeedLimit,
      fixedNodeId: opts.fixedNodeId,
    });
    /* Restore the pointer target before the final contacts. The strict horizon and cached outer
       annulus then clamp only an actual penetration/escape, so dragging cannot paint a node
       through either boundary or leave a release-only stretched system. */
    restoreFixedNode();
    /* Relation PBD, local/cross-system contact and drag are all late positional corrections.
       Project the solar-system COM track only after those layers, otherwise a constraint can
       undo the monotone black-hole fall during the same slice. Pointer-owned systems remain
       excluded by applyGalaxyInwardConvergence, and all strict painted boundaries still close
       after this translation. */
    const convergence = convergenceAnchor
      ? applyGalaxyInwardConvergence(bodies, convergenceAnchor, initialRadii, opts)
      : { applied: 0, outwardCandidates: 0, overrides: 0, factor: 1 };
    /* Relations, cross-system contact and drag can all add a finite late displacement. Alternate
       the strict inner and outer contacts, then verify their annulus member-by-member only for
       a pathological oversized system that no rigid translation can satisfy. */
    const preOuterHorizon = projectBlackHoleHorizon();
    const farFieldConfinement = opts.includeFarFieldConfinement === false
      ? { anchorId: null, envelopeRadius: 0, softRadius: 0,
        acceleratedSystems: 0, boundedSystems: 0, boundedCoreNodes: 0,
        boundedFixedSource: 0, boundedFixedFollowers: 0, boundedDeformedSystems: 0,
        boundedOversizedNodes: 0,
        correctedDistance: 0, maximumShift: 0, outwardVelocityRemoved: 0,
        tangentialVelocityRemoved: 0 }
      : applyGalaxyFarFieldConfinement(bodies, opts);
    const outerHorizon = projectBlackHoleHorizon();
    const initialAnnulus = opts.includeFarFieldConfinement === false
      ? { anchorId: null, innerCorrectedNodes: 0, outerCorrectedNodes: 0, infeasibleNodes: 0 }
      : applyGalaxyAnnularBounds(bodies, opts);
    /* Stellar contact and the member-wise outer annulus are coupled constraints: clamping an
       outer planet can place it back through its star. Alternate the mass-balanced stellar
       projection with the strict black-hole/annulus closures until a read-only audit confirms
       the final painted phase satisfies all three. Normal scenes exit after one pass; the
       bounded loop handles a late oversized or pointer-deformed system without feedback kicks. */
    const stellarPasses = [], closureConfinements = [], closureHorizons = [];
    const annulusPasses = [initialAnnulus];
    let stellarAudit = galaxySystemAnchorClearance(bodies, {
      padding: opts.systemAnchorExclusionPadding,
    });
    let boundaryIterations = 0;
    for (let iteration = 0; iteration < 24; iteration++) {
      stellarPasses.push(applyGalaxySystemAnchorExclusion(bodies, {
        padding: opts.systemAnchorExclusionPadding,
        fixedNodeId: opts.fixedNodeId,
      }));
      /* Re-run the system-level outer solve before falling back to individual members. A
         feasible external system is translated inward as one rigid body, preserving the
         repaired star/planet separation and avoiding the slow mass-ratio recurrence produced
         by repeatedly clamping only the light planet. */
      if (opts.includeFarFieldConfinement !== false) {
        closureConfinements.push(applyGalaxyFarFieldConfinement(bodies, opts));
      }
      closureHorizons.push(projectBlackHoleHorizon());
      annulusPasses.push(opts.includeFarFieldConfinement === false
        ? { anchorId: null, innerCorrectedNodes: 0, outerCorrectedNodes: 0,
          infeasibleNodes: 0 }
        : applyGalaxyAnnularBounds(bodies, opts));
      stellarAudit = galaxySystemAnchorClearance(bodies, {
        padding: opts.systemAnchorExclusionPadding,
      });
      boundaryIterations = iteration + 1;
      if (stellarAudit.minimumClearance === null
        || stellarAudit.minimumClearance >= -1e-9) break;
    }
    const combinedSystemAnchorExclusion = combineGalaxySystemAnchorExclusions(stellarPasses);
    const rawFinalStellarClearance = stellarAudit.minimumClearance;
    const systemAnchorExclusion = Object.assign(combinedSystemAnchorExclusion, {
      boundaryIterations,
      rawMinimumClearance: rawFinalStellarClearance,
      minimumClearance: rawFinalStellarClearance !== null
        && rawFinalStellarClearance >= -1e-9 ? Math.max(0, rawFinalStellarClearance)
        : rawFinalStellarClearance,
    });
    const finalHorizon = closureHorizons[closureHorizons.length - 1];
    const annulus = {
      anchorId: annulusPasses.map(pass => pass.anchorId).find(Boolean) || null,
      innerCorrectedNodes: annulusPasses.reduce(
        (sum, pass) => sum + (pass.innerCorrectedNodes || 0), 0),
      outerCorrectedNodes: annulusPasses.reduce(
        (sum, pass) => sum + (pass.outerCorrectedNodes || 0), 0),
      infeasibleNodes: annulusPasses.reduce(
        (sum, pass) => sum + (pass.infeasibleNodes || 0), 0),
    };
    const confinementCountFields = [
      'acceleratedSystems', 'boundedSystems', 'boundedCoreNodes',
      'boundedFixedSource', 'boundedFixedFollowers', 'boundedDeformedSystems',
      'boundedOversizedNodes',
    ];
    closureConfinements.forEach(pass => {
      confinementCountFields.forEach(field => {
        farFieldConfinement[field] = (farFieldConfinement[field] || 0) + (pass[field] || 0);
      });
      farFieldConfinement.correctedDistance += pass.correctedDistance || 0;
      farFieldConfinement.maximumShift = Math.max(
        farFieldConfinement.maximumShift || 0, pass.maximumShift || 0);
      farFieldConfinement.outwardVelocityRemoved += pass.outwardVelocityRemoved || 0;
      farFieldConfinement.tangentialVelocityRemoved += pass.tangentialVelocityRemoved || 0;
    });
    farFieldConfinement.annulus = annulus;
    const horizonPasses = [
      initialHorizon, driftHorizon, preOuterHorizon, outerHorizon, ...closureHorizons,
    ];
    const blackHoleExclusion = {
      anchorId: finalHorizon.anchorId || driftHorizon.anchorId || initialHorizon.anchorId,
      contacts: horizonPasses.reduce((sum, pass) => sum + pass.contacts, 0),
      systems: horizonPasses.reduce((sum, pass) => sum + pass.systems, 0),
      coreNodes: horizonPasses.reduce((sum, pass) => sum + pass.coreNodes, 0),
      fixedSystemNodes: horizonPasses.reduce(
        (sum, pass) => sum + (pass.fixedSystemNodes || 0), 0
      ),
      repelledNodes: horizonPasses.reduce((sum, pass) => sum + pass.repelledNodes, 0),
      correctedDistance: horizonPasses.reduce(
        (sum, pass) => sum + pass.correctedDistance, 0
      ),
      maximumShift: Math.max(...horizonPasses.map(pass => pass.maximumShift)),
      inwardVelocityRemoved: horizonPasses.reduce(
        (sum, pass) => sum + pass.inwardVelocityRemoved, 0
      ),
      tangentialVelocityRemoved: horizonPasses.reduce(
        (sum, pass) => sum + pass.tangentialVelocityRemoved, 0
      ),
      minimumClearance: finalHorizon.minimumClearance,
    };
    bodies.forEach(node => {
      maximumSpeed = Math.max(maximumSpeed, Math.hypot(node.vx, node.vy));
    });
    /* A single scale preserves total momentum and differential directions. Per-node clipping
       looks safer, but quietly makes a heavy star push a light one without receiving the
       matching reaction. */
    const uncappedMaximumSpeed = maximumSpeed;
    /* Leave a machine-epsilon margin so the common multiplication cannot round a capped
       vector back above the caller's strict limit (for example 24.000000000000004). */
    const strictSpeedLimit = speedLimit * (1 - 4 * Number.EPSILON);
    const speedScale = uncappedMaximumSpeed > speedLimit
      ? strictSpeedLimit / uncappedMaximumSpeed : 1;
    maximumSpeed = 0;
    let kinetic = 0;
    bodies.forEach(node => {
      node.vx *= speedScale;
      node.vy *= speedScale;
      maximumSpeed = Math.max(maximumSpeed, Math.hypot(node.vx, node.vy));
      const mass = finitePositive(node.gravity_mass, 1, 1000);
      kinetic += 0.5 * mass * (node.vx * node.vx + node.vy * node.vy);
    });
    const dragAcceleration = end.dragGravity || start.dragGravity
      || { applied: 0, maximumAcceleration: 0, maximumPull: 0 };
    /* A leapfrog step samples the field twice. Keep both counts rather than overwriting the
       first kick with the second, so live diagnostics can distinguish a dormant envelope from
       a system that actually entered its smooth outer band during this physical slice. */
    const farFieldSamples = [start.farFieldGravity, end.farFieldGravity].filter(Boolean);
    const farFieldGravity = {
      anchorId: farFieldSamples.map(sample => sample.anchorId).find(Boolean) || null,
      envelopeRadius: farFieldSamples.reduce((radius, sample) => Math.max(radius,
        Number(sample.envelopeRadius) || 0), 0),
      softRadius: farFieldSamples.reduce((radius, sample) => Math.max(radius,
        Number(sample.softRadius) || 0), 0),
      samples: farFieldSamples.length,
      acceleratedSystems: farFieldSamples.reduce((sum, sample) => sum
        + (sample.acceleratedSystems || 0), 0),
      acceleratedCoreNodes: farFieldSamples.reduce((sum, sample) => sum
        + (sample.acceleratedCoreNodes || 0), 0),
      acceleratedFixedFollowers: farFieldSamples.reduce((sum, sample) => sum
        + (sample.acceleratedFixedFollowers || 0), 0),
      maximumAcceleration: farFieldSamples.reduce((maximum, sample) => Math.max(maximum,
        sample.maximumAcceleration || 0), 0),
    };
    return {
      bodies: bodies.length,
      collisions: collision.overlaps,
      kinetic,
      maximumSpeed,
      uncappedMaximumSpeed,
      speedCapped: speedScale < 1,
      convergence,
      relationConstraint,
      orbitalSeparation,
      systemAnchorExclusion,
      blackHoleExclusion,
      farFieldConfinement,
      farFieldGravity,
      systemVelocity,
      systemGravity: end.systemGravity || start.systemGravity
        || { systems: 0, anchors: 0, satellites: 0,
          repulsions: 0, surfaceRepulsions: 0,
          maximumRepulsion: 0, maximumSampledAttraction: 0, maximumNetRepulsion: 0,
          minimumSurfaceNetRepulsion: null,
          maximumAcceleration: 0, capScale: 1 },
      mutualGravity: end.mutualGravity || start.mutualGravity
        || { systems: 0, interactions: 0, traversals: 0, approximations: 0,
          maximumAcceleration: 0, capScale: 1 },
      dragGravity: {
        applied: Math.max(dragAcceleration.applied, dragPositionGravity.applied),
        maximumAcceleration: Math.max(
          dragAcceleration.maximumAcceleration, dragPositionGravity.maximumAcceleration
        ),
        maximumPull: dragPositionGravity.maximumPull,
      },
    };
  }

  /* Read-only motion telemetry shared by the browser API and deterministic tests. Evidence
     mass weights every aggregate so a light planet moving quickly cannot masquerade as a heavy
     system-wide kick. Invalid coordinates are reported, never allowed to poison the totals. */
  function galaxyMotionDiagnostics(nodes) {
    const bodies = (nodes || []).filter(node => node && !node.ghost);
    let totalMass = 0, centerX = 0, centerY = 0;
    let momentumX = 0, momentumY = 0, kineticEnergy = 0, maxSpeed = 0;
    let invalidBodies = 0;
    bodies.forEach(node => {
      const mass = finitePositive(node.gravity_mass, 1, 1000);
      const positionFinite = Number.isFinite(node.x) && Number.isFinite(node.y);
      const velocityFinite = Number.isFinite(node.vx) && Number.isFinite(node.vy);
      if (!positionFinite || !velocityFinite) invalidBodies++;
      const x = positionFinite ? node.x : 0, y = positionFinite ? node.y : 0;
      const vx = velocityFinite ? node.vx : 0, vy = velocityFinite ? node.vy : 0;
      const speedSquared = vx * vx + vy * vy;
      totalMass += mass;
      centerX += x * mass;
      centerY += y * mass;
      momentumX += vx * mass;
      momentumY += vy * mass;
      kineticEnergy += 0.5 * mass * speedSquared;
      maxSpeed = Math.max(maxSpeed, Math.sqrt(speedSquared));
    });
    if (totalMass > 0) {
      centerX /= totalMass;
      centerY /= totalMass;
    }
    let angularMomentum = 0;
    bodies.forEach(node => {
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)
        || !Number.isFinite(node.vx) || !Number.isFinite(node.vy)) return;
      const mass = finitePositive(node.gravity_mass, 1, 1000);
      angularMomentum += mass * (
        (node.x - centerX) * node.vy - (node.y - centerY) * node.vx
      );
    });
    return {
      bodies: bodies.length, invalidBodies, totalMass,
      centerX, centerY, momentumX, momentumY,
      momentum: Math.hypot(momentumX, momentumY),
      angularMomentum, kineticEnergy, maxSpeed,
    };
  }

  function fallbackCommunityBridges(nodes, links) {
    const byId = new Map((nodes || []).map(node => [node.id, node]));
    const grouped = new Map();
    (links || []).forEach(link => {
      if (!link || link.ghost || Number(link.physics_strength) === 0) return;
      const source = byId.get(linkEndpoint(link, 'source'));
      const target = byId.get(linkEndpoint(link, 'target'));
      if (!source || !target || source.ghost || target.ghost) return;
      let left = communityKey(source), right = communityKey(target);
      if (left === right) return;
      if (right < left) { const swap = left; left = right; right = swap; }
      const key = left + '|' + right;
      let bridge = grouped.get(key);
      if (!bridge) {
        bridge = {
          id: 'compat-bridge-' + seededHash(0, key),
          source_community: left, target_community: right,
          physics_strength: 0, edge_count: 0
        };
        grouped.set(key, bridge);
      }
      bridge.edge_count++;
      bridge.physics_strength += Math.max(0, Math.min(1,
        Number.isFinite(Number(link.strength)) ? Number(link.strength) : 0.2));
    });
    const bridges = [...grouped.values()];
    bridges.forEach(bridge => {
      bridge.physics_strength = Math.max(0.05, Math.min(1,
        bridge.physics_strength / Math.max(1, bridge.edge_count)));
    });
    return bridges.sort((a, b) => a.id.localeCompare(b.id));
  }
  function validNodeId(value) {
    const type = typeof value;
    return type === 'string' || type === 'boolean'
      || (type === 'number' && Number.isFinite(value));
  }
  function linkEndpoint(link, side) {
    if (!link || (typeof link !== 'object' && typeof link !== 'function')) return null;
    const value = link[side] !== undefined ? link[side] : link[side === 'source' ? 'from' : 'to'];
    return idOf(value);
  }
  function asOfValue(value) {
    if (value instanceof Date) {
      const parsed = value.getTime();
      return Number.isFinite(parsed) ? parsed : null;
    }
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
    if (!item || (typeof item !== 'object' && typeof item !== 'function')) return fallback;
    const value = item[key] !== undefined ? item[key] : item[key === 'valid_from' ? 'born' : 'closed'];
    if (value === undefined || value === null || value === '') return fallback;
    const parsed = asOfValue(value);
    return parsed === null ? fallback : parsed;
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
    const fallback = [140, 131, 232];
    if (typeof c !== 'string') return fallback;
    const value = c.trim();
    if (!value) return fallback;
    if (value[0] === '#') {
      const hex = value.length === 4
        ? value[1] + value[1] + value[2] + value[2] + value[3] + value[3]
        : value.slice(1, 7);
      if (!/^[0-9a-f]{6}$/i.test(hex)) return fallback;
      const n = parseInt(hex, 16);
      return [n >> 16 & 255, n >> 8 & 255, n & 255];
    }
    const matches = value.match(/-?\d+(?:\.\d+)?/g) || [];
    if (matches.length < 3) return fallback;
    return matches.slice(0, 3).map(component => Math.max(0, Math.min(255, Math.round(Number(component)))));
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
    return !!(link && hasOwn(CLUSTER_EXCLUDED_LABELS, link.label));
  }

  function communities(nodes, links) {
    const adj = Object.create(null);
    // Traversal adjacency (hover neighbourhood, focus depth, bridges, betweenness) keeps every
    // relation; only the community BFS below reads `clusterAdj`.
    const clusterAdj = Object.create(null);
    const nodesById = new Map(nodes.map(node => [node.id, node]));
    nodes.forEach(n => { adj[n.id] = []; clusterAdj[n.id] = []; });
    links.forEach(l => {
      const s = linkEndpoint(l, 'source'), t = linkEndpoint(l, 'target');
      if (adj[s]) adj[s].push(t);
      if (adj[t]) adj[t].push(s);
      if (l.ghost || clustersAcross(l)) return;
      if (clusterAdj[s]) clusterAdj[s].push(t);
      if (clusterAdj[t]) clusterAdj[t].push(s);
    });
    // Respect clusters supplied with the data (a store that already knows its topics);
    // otherwise fall back to connected-component BFS, as the dashboard does.
    if (nodes.length && nodes.every(n => n.community !== undefined && n.community !== null)) return adj;
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
    const bc = Object.create(null);
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
      const stack = [], pred = Object.create(null), sigma = Object.create(null);
      const dist = Object.create(null), delta = Object.create(null);
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
  function edgeKey(a, b) {
    const left = JSON.stringify([typeof a, String(a)]);
    const right = JSON.stringify([typeof b, String(b)]);
    return left < right ? left + '|' + right : right + '|' + left;
  }
  function findBridges(nodes, links, adj) {
    const disc = Object.create(null), low = Object.create(null);
    const parent = Object.create(null), bridges = new Set();
    const multiplicity = Object.create(null);
    links.forEach(link => {
      const s = linkEndpoint(link, 'source'), t = linkEndpoint(link, 'target');
      const key = edgeKey(s, t);
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
          const key = edgeKey(p, u);
          if (low[u] > disc[p] && multiplicity[key] === 1) {
            bridges.add(edgeKey(p, u));
          }
        }
      }
    };
    nodes.forEach(n => { if (!disc[n.id]) visit(n.id); });
    links.forEach(l => {
      const s = linkEndpoint(l, 'source'), t = linkEndpoint(l, 'target');
      l.bridge = bridges.has(edgeKey(s, t));
    });
    return bridges;
  }

  function paintGalaxyAnchorAdornment(ctx, node, scale, accent, foreground) {
    if (!ctx || !node || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return 0;
    const role = node.anchor_role;
    if (role !== 'global' && role !== 'community') return 0;
    const radius = finitePositive(node.radius, 3, 160);
    const color = accent || node.color || '#9d7bff';
    const inverseScale = 1 / Math.max(0.1, Number(scale) || 1);
    if (role === 'community') {
      if (foreground) return 0;
      ctx.save();
      ctx.strokeStyle = alpha(color, 0.28);
      ctx.lineWidth = 0.75 * inverseScale;
      ctx.beginPath(); ctx.arc(node.x, node.y, radius * 1.42, 0, 6.2832); ctx.stroke();
      ctx.restore();
      return 1;
    }
    ctx.save();
    if (!foreground) {
      if (typeof ctx.createRadialGradient === 'function') {
        const halo = ctx.createRadialGradient(
          node.x, node.y, radius * 0.55, node.x, node.y, radius * 3.2
        );
        halo.addColorStop(0, alpha(color, 0.38));
        halo.addColorStop(0.42, alpha(color, 0.16));
        halo.addColorStop(1, alpha(color, 0));
        ctx.fillStyle = halo;
      } else ctx.fillStyle = alpha(color, 0.12);
      ctx.beginPath(); ctx.arc(node.x, node.y, radius * 3.2, 0, 6.2832); ctx.fill();
      ctx.strokeStyle = alpha(color, 0.72);
      ctx.lineWidth = 1.15 * inverseScale;
      ctx.beginPath();
      if (typeof ctx.ellipse === 'function') {
        ctx.ellipse(node.x, node.y, radius * 1.72, radius * 0.62, -0.28, 0, 6.2832);
      } else ctx.arc(node.x, node.y, radius * 1.45, 0, 6.2832);
      ctx.stroke();
    } else {
      /* The opaque event-horizon core is deliberately smaller than the evidence radius; the
         material rim and hit area retain the canonical mass-authoritative geometry. */
      ctx.fillStyle = '#020308';
      ctx.beginPath(); ctx.arc(node.x, node.y, radius * 0.68, 0, 6.2832); ctx.fill();
      ctx.strokeStyle = alpha('#ffffff', 0.34);
      ctx.lineWidth = 0.55 * inverseScale;
      ctx.beginPath(); ctx.arc(node.x, node.y, radius * 0.78, 0, 6.2832); ctx.stroke();
    }
    ctx.restore();
    return 1;
  }

  function create(el, options) {
    if (typeof ForceGraph === 'undefined') throw new Error('force-graph not loaded');
    if (!el || typeof el.getAttribute !== 'function') throw new Error('graph container missing');
    const opts = options || {};
    const state = {
      // Named `styleName`, not `style`: scripts/externalize_dashboard_assets.py scans this
      // asset for runtime inline-style mutation with a text pattern, and a plain data field
      // by the shorter name reads as one. The longer name keeps that gate honest.
      styleName: 'cyber', colorBy: 'community', palette: 'theme',
      overrides: Object.create(null), themeColors: Object.create(null),
      settings: Object.assign({}, PRESETS.galaxy, { mode: 'galaxy', labels: false, flow: true, frozen: false }),
      minDegree: 1, showUnlinked: true, focusId: null, depth: 2, layers: { temporal: true, entity: true, causal: true, semantic: true, code: false },
      path: null, asOf: null, ghost: true, sizeBy: 'mass', bridges: false, suggestions: false,
      collapse: 'auto', renderMode: opts.renderMode === 'full' ? 'full' : 'overview'
    };
    let raw = { nodes: [], links: [], suggestions: [], communities: [], community_bridges: [], meta: {} };
    const galaxyServerPhase = new Map();
    const galaxySavedPhase = new Map();
    let adj = Object.create(null), liveAdj = Object.create(null), hilite = null, hoverSet = null, maxDeg = 1;
    let legacySizeBy = 'degree';
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
    let physicsFrame = 0, physicsReheatPending = false;
    let galaxyFrame = 0, galaxyLastFrameTime = null, galaxyAccumulator = 0;
    let galaxyFrames = 0, galaxySteps = 0, galaxyLastSubsteps = 0;
    let galaxyReheatStepsRemaining = 0, galaxyReheatActivations = 0;
    let galaxyReheatStepsApplied = 0, galaxyLastReheatSubsteps = 0;
    let galaxyLastKinetic = 0, galaxyLastCollisions = 0, galaxyLastRelationCorrections = 0;
    let galaxyLastRelationDistance = 0, galaxyLastOrbitalRelationSkips = 0;
    let galaxyLastOrbitalSeparations = 0;
    let galaxyLastCrossSystemSeparations = 0;
    let galaxyLastOrbitalCorrection = 0, galaxyLastLocalVelocityLimits = 0;
    let galaxySpeedCaps = 0;
    let galaxyLastBlackHoleExclusion = {
      anchorId: null, contacts: 0, systems: 0, coreNodes: 0, fixedSystemNodes: 0,
      repelledNodes: 0,
      correctedDistance: 0, maximumShift: 0, inwardVelocityRemoved: 0,
      tangentialVelocityRemoved: 0,
      minimumClearance: null,
    };
    let galaxyLastSystemAnchorExclusion = {
      padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
      systems: 0, contacts: 0, correctedDistance: 0, maximumShift: 0,
      inwardVelocityRemoved: 0, tangentialVelocityRemoved: 0,
      minimumClearance: null, iterations: 0,
    };
    let galaxyLastFarFieldConfinement = {
      anchorId: null, envelopeRadius: 0, softRadius: 0,
      acceleratedSystems: 0, boundedSystems: 0, boundedCoreNodes: 0,
      boundedFixedSource: 0, boundedFixedFollowers: 0, boundedDeformedSystems: 0,
      boundedOversizedNodes: 0,
      correctedDistance: 0, maximumShift: 0, outwardVelocityRemoved: 0,
      tangentialVelocityRemoved: 0,
      annulus: { anchorId: null, innerCorrectedNodes: 0, outerCorrectedNodes: 0,
        infeasibleNodes: 0 },
    };
    let galaxyLastFarFieldGravity = {
      anchorId: null, envelopeRadius: 0, softRadius: 0, samples: 0,
      acceleratedSystems: 0, acceleratedCoreNodes: 0, acceleratedFixedFollowers: 0,
      maximumAcceleration: 0,
    };
    let galaxyLastMutualGravity = {
      systems: 0, interactions: 0, traversals: 0, approximations: 0,
      maximumAcceleration: 0, capScale: 1,
    };
    let galaxyLastSystemGravity = {
      systems: 0, anchors: 0, satellites: 0, repulsions: 0, surfaceRepulsions: 0,
      maximumRepulsion: 0, maximumSampledAttraction: 0, maximumNetRepulsion: 0,
      minimumSurfaceNetRepulsion: null,
      repulsionPadding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
      repulsionRange: GALAXY_SYSTEM_ANCHOR_REPULSION_RANGE,
      repulsionAcceleration: GALAXY_SYSTEM_ANCHOR_REPULSION_ACCELERATION,
      maximumAcceleration: 0, capScale: 1,
    };
    let galaxyLastGravityResponse = {
      systems: 0, moved: 0, ratio: 1, maximumShift: 0, anchorId: null,
    };
    let softAlphaTimer = 0, initialFitFrame = 0;
    let suppressNodeClickAfterDrag = false, dragClickFrame = 0;
    const hasBrowserFrameClock = typeof window !== 'undefined'
      && typeof window.requestAnimationFrame === 'function';
    const requestFrame = hasBrowserFrameClock
      ? window.requestAnimationFrame.bind(window)
      : callback => setTimeout(callback, 0);
    const cancelFrame = typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function'
      ? window.cancelAnimationFrame.bind(window)
      : clearTimeout;
    let betweennessReady = false;
    const fg = ForceGraph()(el);
    const api = {};
    const visibilityDocument = typeof document !== 'undefined' ? document : null;
    let detachVisibility = null;

    let activeDragNode = null;
    let galaxyGravityForce = null, galaxyCenterForce = null, communityBridgeForce = null;
    let galaxyRelationForce = null, galaxyCollisionForce = null;
    let dragFollowers = [];
    let dragFollowerGravityReport = { applied: 0, maximumAcceleration: 0, maximumPull: 0 };
    let dragPreVelocity = null;

    function setActiveDragNode(node) {
      activeDragNode = node || null;
    }

    function galaxySoftening() {
      const raw = Number(state.settings.repel);
      const separation = Number.isFinite(raw) ? Math.max(0, Math.min(120, raw))
        : PRESETS.galaxy.repel;
      return Math.max(3, separation * 0.16);
    }

    /* Interactive evidence systems often contain several large stars at close range. Treating
       those as point masses produces slingshots that a browser-sized fixed step cannot resolve.
       Keep the live local potential smooth below the scale of a system orbit. */
    function galaxyLiveSoftening() {
      return Math.max(32, galaxySoftening() * 4);
    }

    function makeGalaxyGravityForce() {
      const force = alphaValue => {
        if (state.settings.frozen || staticFullLayout) return;
        applyGalaxyGravity(force.nodes || fg.graphData().nodes || [], {
          gravity: state.settings.gravity,
          softening: galaxySoftening(), alpha: alphaValue,
          exactLimit: GALAXY_EXACT_LIMIT, theta: GALAXY_BARNES_HUT_THETA
        });
      };
      force.initialize = nodes => { force.nodes = nodes; };
      return force;
    }

    function makeGalaxyRelationForce() {
      const force = alphaValue => {
        if (state.settings.frozen || staticFullLayout) return;
        const orbitScale = galaxyRelationOrbitScale(state.settings.link);
        applyGalaxyRelationSprings(
          force.nodes || fg.graphData().nodes || [], fg.graphData().links || [],
          {
            alpha: alphaValue, orbitScale,
            strengthMultiplier: GALAXY_RELATION_STRENGTH_MULTIPLIER,
            forceCap: GALAXY_RELATION_FORCE_CAP,
            accelerationCap: GALAXY_RELATION_ACCELERATION_CAP,
          }
        );
      };
      force.initialize = nodes => { force.nodes = nodes; };
      return force;
    }

    function makeGalaxyCollisionForce() {
      const force = () => {
        if (state.settings.frozen || staticFullLayout) return;
        applyGalaxyCollisions(force.nodes || fg.graphData().nodes || [], {
          padding: 1.5, strength: 0.7, iterations: large ? 1 : 2
        });
      };
      force.initialize = nodes => { force.nodes = nodes; };
      return force;
    }

    function makeCommunityBridgeForce() {
      const force = alphaValue => {
        if (state.settings.frozen || staticFullLayout) return;
        applyCommunityBridgeGravity(force.nodes || fg.graphData().nodes || [], raw.community_bridges, {
          gravity: state.settings.gravity,
          softening: Math.max(24, galaxySoftening() * 4), alpha: alphaValue
        });
      };
      force.initialize = nodes => { force.nodes = nodes; };
      return force;
    }

    function makeGalaxyCenterForce() {
      const force = alphaValue => {
        if (state.settings.frozen || staticFullLayout) return;
        applyGalaxyCentralGravity(force.nodes || fg.graphData().nodes || [], {
          gravity: state.settings.gravity,
          softening: Math.max(36, galaxySoftening() * 5), alpha: alphaValue
        });
      };
      force.initialize = nodes => { force.nodes = nodes; };
      return force;
    }

    let velocityGuardForce = null;

    function nodeSpeedLimit() {
      const link = Math.max(8, Number(state.settings.link) || 16);
      return Math.max(MIN_NODE_SPEED, Math.min(MAX_NODE_SPEED, link * 0.9));
    }

    function makeVelocityGuardForce() {
      const force = () => {
        const nodes = force.nodes || fg.graphData().nodes || [];
        const limit = nodeSpeedLimit();
        let maximumSpeed = 0;
        nodes.forEach(node => {
          if (node.ghost) {
            node.vx = 0;
            node.vy = 0;
            return;
          }
          node.vx = Number.isFinite(node.vx) ? node.vx : 0;
          node.vy = Number.isFinite(node.vy) ? node.vy : 0;
          maximumSpeed = Math.max(maximumSpeed, Math.hypot(node.vx, node.vy));
        });
        /* One common scale preserves every equal-and-opposite impulse and therefore total
           evidence-mass momentum. Per-node clipping made the light side of a contact lose more
           velocity than its star, manufacturing the same system drift the guard should prevent. */
        const scale = maximumSpeed > limit ? limit / maximumSpeed : 1;
        if (scale < 1) nodes.forEach(node => {
          if (node.ghost) return;
          node.vx *= scale;
          node.vy *= scale;
        });
      };
      force.initialize = nodes => { force.nodes = nodes; };
      return force;
    }

    function installVelocityGuard() {
      if (!velocityGuardForce) velocityGuardForce = makeVelocityGuardForce();
      // Keep this boundary available to dependency-light callers too. In a browser D3
      // invokes it after the motion forces; in the Node/static harness it still provides
      // the same finite-value and shared-scale contract when D3 is absent.
      fg.d3Force('velocityGuard', null);
      fg.d3Force('velocityGuard', velocityGuardForce);
    }

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

    function cancelAutoFit() {
      clearTimeout(fitTimer);
      fitTimer = 0;
      cancelFrame(initialFitFrame);
      initialFitFrame = 0;
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

    /* Reduced motion still controls cosmetic animation and camera transitions. Physics is
       deliberately controlled by the visible Freeze switch instead: otherwise the switch can
       say "off" while an OS preference silently leaves every graph static. */
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
      /* The fixed Galaxy clock invalidates at its bounded cadence. Only a legacy layout wearing
         the animated Galaxy paint needs force-graph's independent full-rate redraw loop. */
      return !reduced() && state.styleName === 'galaxy'
        && state.settings.mode !== 'galaxy' && !large;
    }
    /* Betweenness is the one analysis that is superlinear in the store size, and nothing in
       the default view consumes it — the bridge overlay and betweenness-sizing are both off.
       Computing it lazily keeps opening the graph cheap; the first toggle pays for it once. */
    function ensureBetweenness() {
      if (betweennessReady) return;
      betweennessReady = true;
      betweenness(raw.nodes, liveAdj && Object.keys(liveAdj).length ? liveAdj : adj);
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
        const queuedPhysics = physicsReheatPending;
        physicsReheatPending = false;
        pendingRender = null;
        render(!!fit, !!reheat || queuedPhysics);
      }
    }

    /* Priority mirrors the classic renderer's graphTypeColor(): an explicit user override wins,
       then a non-classic style's own palette, then the *active theme*. The theme tier is the
       reason `themeColors` exists — it cannot be folded into `overrides`, which outrank
       STYLE_PAL. The dashboard owns the CSS custom properties (`--entity-*`), so it supplies
       the resolved values through setThemeColors() on every applyTheme()/graphRecolor();
       THEME_ETYPE stays only as the standalone-embed fallback for a caller that never does. */
    function etypeColor(type) {
      const override = hasOwn(state.overrides, type) ? state.overrides[type] : null;
      if (typeof override === 'string' && override) return override;
      const stylePalette = state.styleName !== 'classic' ? STYLE_PAL[state.styleName] : null;
      const styled = stylePalette && hasOwn(stylePalette, type) ? stylePalette[type] : null;
      if (typeof styled === 'string' && styled) return styled;
      const themed = hasOwn(state.themeColors, type) ? state.themeColors[type] : null;
      if (typeof themed === 'string' && themed) return themed;
      return hasOwn(THEME_ETYPE, type) ? THEME_ETYPE[type] : '#8c83e8';
    }
    function selectedPalette() {
      const palette = hasOwn(PALETTES, state.palette) ? PALETTES[state.palette] : null;
      if (!palette) return null;
      const values = Object.values(palette).filter(value => typeof value === 'string' && value);
      return values.length ? values : null;
    }
    /* A palette is a colour family, not merely an entity-type override. Previously the
       default Community and Connections modes skipped `overrides`, so choosing Aurora,
       Ocean, Ember, or High contrast changed no pixels unless the user also discovered the
       separate Entity type selector. Use the selected family in every node-colour mode;
       Theme retains the active style's deliberately tuned defaults. */
    function commPal() {
      return selectedPalette() || COMMUNITY_PALS[state.styleName] || COMMUNITY_PALS.classic;
    }
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
    function layerColor(layer) {
      const layers = STYLE_LAYERS[state.styleName] || STYLE_LAYERS.classic;
      return (hasOwn(layers, layer) && layers[layer]) || '#8c83e8';
    }

    function born(item) { return temporalValue(item, 'valid_from', -Infinity); }
    function closed(item) { return temporalValue(item, 'valid_to', null); }
    function aliveAt(item, date) {
      const start = born(item), end = closed(item);
      return start <= date && (end === null || end > date);
    }

    function collapsedData(nodes, links) {
      const groups = new Map();
      nodes.forEach(n => {
        const c = communityKey(n);
        if (!groups.has(c)) groups.set(c, {
          id: 'cluster-' + c, cluster: true, community: n.community || 0,
          community_id: c, name: (n.topic || 'Cluster ' + (Number(n.community || 0) + 1)),
          etype: n.etype, members: 0, degree: 0, betweenness: 0,
          gravity_mass: 0, visual_radius: 0, x: 0, y: 0,
          _position_mass: 0, _fallback_x: 0, _fallback_y: 0, _fallback_count: 0,
          _live_members: 0
        });
        const group = groups.get(c);
        group.members++;
        if (!n.ghost) group._live_members++;
        group.degree += n.degree || 0;
        const mass = n.ghost ? 0 : finitePositive(n.gravity_mass, 1, 1000);
        group.gravity_mass += mass;
        if (Number.isFinite(n.x) && Number.isFinite(n.y)) {
          if (mass) {
            group.x += n.x * mass;
            group.y += n.y * mass;
            group._position_mass += mass;
          } else {
            group._fallback_x += n.x;
            group._fallback_y += n.y;
            group._fallback_count++;
          }
        }
        group.betweenness = Math.max(group.betweenness, n.betweenness || 0);
      });
      const cnodes = [...groups.values()];
      cnodes.forEach(node => {
        node.ghost = node._live_members === 0;
        node.visual_radius = node.ghost ? 0 : radiusFromGravityMass(node.gravity_mass);
        if (node._position_mass) {
          node.x /= node._position_mass;
          node.y /= node._position_mass;
        } else if (node._fallback_count) {
          node.x = node._fallback_x / node._fallback_count;
          node.y = node._fallback_y / node._fallback_count;
        } else {
          node.x = undefined;
          node.y = undefined;
        }
        delete node._position_mass;
        delete node._fallback_x;
        delete node._fallback_y;
        delete node._fallback_count;
        delete node._live_members;
      });
      const seen = Object.create(null);
      const clinks = [];
      // Indexed lookup, not Array#find per endpoint: auto-collapse fires on every zoom-out,
      // and the scan made that O(nodes x links) — a visible freeze on a real store.
      const byId = new Map(raw.nodes.map(n => [n.id, n]));
      links.forEach(l => {
        const s = byId.get(linkEndpoint(l, 'source'));
        const t = byId.get(linkEndpoint(l, 'target'));
        if (!s || !t) return;
        const a = 'cluster-' + communityKey(s), b = 'cluster-' + communityKey(t);
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
      const keepLayer = l => {
        const layers = state.layers;
        return !layers || !hasOwn(layers, l.layer) || layers[l.layer] !== false;
      };
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
        const live = nodes.filter(n => aliveAt(n, state.asOf) && !n._historyGhost);
        const ghosts = state.ghost ? nodes.filter(n => (n._historyGhost || !aliveAt(n, state.asOf)) && born(n) <= state.asOf).map(n => Object.assign(n, { ghost: true })) : [];
        live.forEach(n => { n.ghost = false; });
        nodes = live.concat(ghosts);
      } else {
        nodes.forEach(n => { n.ghost = n._historyGhost === true; });
        if (!state.ghost) nodes = nodes.filter(n => !n.ghost);
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
        links.forEach(l => { l.ghost = l._historyGhost === true || !aliveAt(l, state.asOf); });
        if (!state.ghost) links = links.filter(l => !l.ghost);
        links = links.filter(l => born(l) <= state.asOf);
      } else {
        links.forEach(l => { l.ghost = l._historyGhost === true; });
        if (!state.ghost) links = links.filter(l => !l.ghost);
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

    function disableD3GalaxyIntegration() {
      ['charge', 'link', 'center', 'x', 'y', 'radial', 'galaxy', 'galaxyCenter',
        'galaxyRelations', 'communityBridges', 'collide', 'velocityGuard']
        .forEach(name => fg.d3Force(name, null));
      setSimulationBudget(false, true);
    }

    function applyForces() {
      /* Extremely large complete snapshots use the deterministic fallback, but a normal
         full graph remains a live layout. The previous `renderMode === 'full'` guard removed
         every force and pinned every node, which is why the gravity slider could read 98
         while the canvas stayed on a wide ring. */
      if (staticFullLayout) {
        if ((state.settings.mode || 'compact') === 'galaxy') {
          disableD3GalaxyIntegration();
          return;
        }
        fg.d3Force('charge', null);
        fg.d3Force('galaxy', null);
        fg.d3Force('galaxyCenter', null);
        fg.d3Force('galaxyRelations', null);
        fg.d3Force('communityBridges', null);
        fg.d3Force('link', null);
        fg.d3Force('x', null);
        fg.d3Force('y', null);
        fg.d3Force('radial', null);
        fg.d3Force('collide', null);
        fg.d3Force('velocityGuard', null);
        return;
      }
      const s = state.settings, mode = s.mode || 'compact';
      let link = fg.d3Force('link');
      if (!link && typeof d3 !== 'undefined' && d3.forceLink) {
        link = d3.forceLink().id(node => node.id);
        fg.d3Force('link', link);
      }
      fg.d3Force('radial', null);
      const layoutNodes = fg.graphData().nodes || [];
      const layoutById = new Map(layoutNodes.map(node => [node.id, node]));
      if (mode === 'galaxy') {
        /* Galaxy is integrated by the fixed physical clock below. Leaving even one D3 force or
           its velocity/position tick installed would apply the field twice and reintroduce alpha
           decay, global reheats, and frame-rate-dependent motion. force-graph remains the canvas
           and hit-test host only. */
        disableD3GalaxyIntegration();
        return;
      }
      fg.d3Force('galaxy', null);
      fg.d3Force('galaxyCenter', null);
      fg.d3Force('galaxyRelations', null);
      fg.d3Force('communityBridges', null);
      let charge = fg.d3Force('charge');
      if (!charge && typeof d3 !== 'undefined' && d3.forceManyBody) {
        charge = d3.forceManyBody();
        fg.d3Force('charge', charge);
      }
      if (charge && charge.strength) charge.strength(-(mode === 'communities' ? Math.max(10, s.repel * 0.68) : s.repel));
      if (link && link.distance) link.distance(s.link);
      if (link && link.strength) link.strength(edge => {
        const source = typeof edge.source === 'object' ? edge.source : layoutById.get(linkEndpoint(edge, 'source'));
        const target = typeof edge.target === 'object' ? edge.target : layoutById.get(linkEndpoint(edge, 'target'));
        return 1 / Math.max(1, Math.min(
          source && source.degree || 1, target && target.degree || 1
        ));
      });
      if (typeof d3 === 'undefined') {
        installVelocityGuard();
        return;
      }
      /* The layout buttons are arrangements, not just five nearby slider presets. Keep the
         ordinary force settings as the local texture, then give each named mode its own
         geometry so switching modes is visible even when the graph has only one component.
         Centering must stay gentle and origin-based: a function target at a distant grid
         slot would fight an explicit drag, and a released node must stay where the user
         dropped it (the e2e drag-release contract). */
      if (mode === 'communities') {
        const communityKeys = [], seenCommunities = new Set();
        layoutNodes.forEach(node => {
          const key = Number.isFinite(node.community) ? node.community : 0;
          if (!seenCommunities.has(key)) { seenCommunities.add(key); communityKeys.push(key); }
        });
        communityKeys.sort((a, b) => a - b);
        const columns = Math.max(1, Math.ceil(Math.sqrt(communityKeys.length)));
        const rows = Math.max(1, Math.ceil(communityKeys.length / columns));
        const gap = Math.max(180, (Number(s.link) || 16) * 10);
        const targets = new Map();
        communityKeys.forEach((key, index) => {
          const column = index % columns, row = Math.floor(index / columns);
          targets.set(key, {
            x: (column - (columns - 1) / 2) * gap,
            y: (row - (rows - 1) / 2) * gap * 0.72,
          });
        });
        /* A gentle origin-based centering keeps the layout coherent without fighting a
           drag; the community grid is still visible through the charge/repel and link
           structure installed above. */
        const centering = Math.max(0.04, (Number(s.gravity) || 0) / 100);
        fg.d3Force('x', d3.forceX(0).strength(centering));
        fg.d3Force('y', d3.forceY(0).strength(centering));
      } else if (mode === 'radial' && d3.forceRadial) {
        const outerRadius = Math.max(180, Math.min(360, Math.sqrt(Math.max(1, layoutNodes.length)) * 18 + (Number(s.link) || 16) * 4));
        const degreeScale = Math.max(1, maxOf(layoutNodes.map(node => node.degree || 0), 1));
        fg.d3Force('x', d3.forceX(0).strength(Math.max(0.05, (Number(s.gravity) || 0) / 500)));
        fg.d3Force('y', d3.forceY(0).strength(Math.max(0.05, (Number(s.gravity) || 0) / 500)));
        fg.d3Force('radial', d3.forceRadial(node => {
          const hubness = Math.max(0, Math.min(1, (node.degree || 0) / degreeScale));
          return 34 + (outerRadius - 34) * (1 - hubness);
        }).strength(0.72));
      } else if (mode === 'constellation') {
        const positions = new Map(), total = Math.max(1, layoutNodes.length - 1);
        const reach = Math.max(160, Math.min(330, 80 + Math.sqrt(Math.max(1, layoutNodes.length)) * 10));
        layoutNodes.forEach((node, index) => {
          const rank = Number.isFinite(node.rank) ? node.rank : index;
          const fraction = Math.max(0, Math.min(1, rank / total));
          const angle = index * 2.399963229728653;
          const radius = 48 + fraction * reach;
          positions.set(node.id, { x: Math.cos(angle) * radius * 1.18, y: Math.sin(angle) * radius * 0.76 });
        });
        const target = node => positions.get(node.id) || { x: 0, y: 0 };
        fg.d3Force('x', d3.forceX(node => target(node).x).strength(0.18));
        fg.d3Force('y', d3.forceY(node => target(node).y).strength(0.18));
      } else {
        const centering = mode === 'compact' ? Math.max(0.24, (Number(s.gravity) || 0) / 100) : Math.max(0.06, (Number(s.gravity) || 0) / 100);
        fg.d3Force('x', d3.forceX(0).strength(centering));
        fg.d3Force('y', d3.forceY(0).strength(centering));
      }
      /* One collision pass on a large graph, two otherwise — the classic path's
         `.iterations(GPERF.large?1:2)`. The second pass costs another full quadtree traversal
         per node on every tick, and a large store pays that on the initial layout and on every
         reheat, which is exactly where it is least affordable. */
      if (d3.forceCollide) fg.d3Force('collide', d3.forceCollide(n => n.radius + 1.5).iterations(large ? 1 : 2));
      /* D3 applies forces in insertion order. Register the guard after every motion force so
         it is the final velocity boundary. A drag then removes it with every other global force. */
      installVelocityGuard();
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

    function releasePinnedPositions(data) {
      data.nodes.forEach(node => {
        node.fx = undefined;
        node.fy = undefined;
        node.vx = Number.isFinite(node.vx) ? node.vx : 0;
        node.vy = Number.isFinite(node.vy) ? node.vy : 0;
      });
    }

    function pinGalaxySceneLayout(data) {
      const layoutSeed = raw.meta && raw.meta.layout_seed !== undefined
        ? raw.meta.layout_seed : 0;
      ensureGalaxyPositions(data.nodes, layoutSeed);
      data.nodes.forEach(node => {
        node.vx = 0;
        node.vy = 0;
        node.fx = node.x;
        node.fy = node.y;
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
      const repel = Math.max(0, Number(s.repel) || 0);
      const link = Math.max(4, Number(s.link) || 4);
      const nodeSize = Math.max(1, Number(s.size) || 3);
      const compactness = galaxyLayoutCompactness(s.gravity);
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
      const galaxyAnchor = state.settings.mode === 'galaxy'
        && (node.anchor_role === 'global' || node.anchor_role === 'community');
      if (galaxyAnchor) paintGalaxyAnchorAdornment(
        ctx, node, scale, state.themeColors.accent || col, false
      );
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
      if (galaxyAnchor) paintGalaxyAnchorAdornment(
        ctx, node, scale, state.themeColors.accent || nodeMaterial.identity, true
      );
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
      // Labels are deferred to onRenderFramePost so they always render above
      // every node body regardless of iteration order.
      ctx.globalAlpha = 1;
    }

    function paintNodeLabel(node, ctx, scale) {
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      const focus = hoverSet && hoverSet.size > 1, neighbor = focus && hoverSet.has(node.id);
      const r = node.radius;
      const showLabel = (state.settings.labels && labelIds.has(node.id)) || node.id === hilite || neighbor;
      if (!showLabel || scale <= 0.35) return;
      const size = Math.max(2, state.settings.font / scale);
      ctx.font = '500 ' + size + 'px system-ui, sans-serif';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = 'rgba(0,0,0,.5)';
      ctx.fillText(nodeName(node), node.x + r + 1.6 + 0.3, node.y + 0.3);
      ctx.fillStyle = state.themeColors.label || (node.id === hilite ? '#ffffff' : 'rgba(232,236,245,.86)');
      ctx.fillText(nodeName(node), node.x + r + 1.6, node.y);
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
    function pageHidden() {
      return !!(visibilityDocument && visibilityDocument.hidden === true);
    }

    function autoCollapseEligible() {
      if (raw.nodes.length <= 500) return false;
      /* Keep a bounded Galaxy overview expanded after auto-fit. Explicit Collapse=true still
         works; oversized Galaxy and every non-Galaxy layout retain automatic collapse. */
      return state.settings.mode !== 'galaxy' || !galaxySceneWithinLiveLimit({
        nodes: raw.nodes, links: raw.links,
      });
    }

    function galaxyDynamicsEligible() {
      if (!hasBrowserFrameClock || destroyed || !running || pageHidden()) return false;
      if (state.settings.mode !== 'galaxy' || state.settings.frozen
        || staticFullLayout || collapsed) return false;
      const data = fg.graphData() || {};
      return Array.isArray(data.nodes) && data.nodes.some(node => node && !node.ghost);
    }

    function resetGalaxyClock() {
      galaxyLastFrameTime = null;
      galaxyAccumulator = 0;
      galaxyLastSubsteps = 0;
    }

    function resetGalaxyDiagnostics() {
      galaxyFrames = 0;
      galaxySteps = 0;
      galaxyLastKinetic = 0;
      galaxyLastCollisions = 0;
      galaxyLastRelationCorrections = 0;
      galaxyLastRelationDistance = 0;
      galaxyLastOrbitalRelationSkips = 0;
      galaxyLastOrbitalSeparations = 0;
      galaxyLastCrossSystemSeparations = 0;
      galaxyLastOrbitalCorrection = 0;
      galaxyLastLocalVelocityLimits = 0;
      galaxySpeedCaps = 0;
      galaxyLastBlackHoleExclusion = {
        anchorId: null, contacts: 0, systems: 0, coreNodes: 0, fixedSystemNodes: 0,
        repelledNodes: 0,
        correctedDistance: 0, maximumShift: 0, inwardVelocityRemoved: 0,
        tangentialVelocityRemoved: 0,
        minimumClearance: null,
      };
      galaxyLastSystemAnchorExclusion = {
        padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
        systems: 0, contacts: 0, correctedDistance: 0, maximumShift: 0,
        inwardVelocityRemoved: 0, tangentialVelocityRemoved: 0,
        minimumClearance: null, iterations: 0,
      };
      galaxyLastFarFieldConfinement = {
        anchorId: null, envelopeRadius: 0, softRadius: 0,
        acceleratedSystems: 0, boundedSystems: 0, boundedCoreNodes: 0,
        boundedFixedSource: 0, boundedFixedFollowers: 0, boundedDeformedSystems: 0,
        boundedOversizedNodes: 0,
        correctedDistance: 0, maximumShift: 0, outwardVelocityRemoved: 0,
        tangentialVelocityRemoved: 0,
        annulus: { anchorId: null, innerCorrectedNodes: 0, outerCorrectedNodes: 0,
          infeasibleNodes: 0 },
      };
      galaxyLastFarFieldGravity = {
        anchorId: null, envelopeRadius: 0, softRadius: 0, samples: 0,
        acceleratedSystems: 0, acceleratedCoreNodes: 0, acceleratedFixedFollowers: 0,
        maximumAcceleration: 0,
      };
      galaxyReheatStepsRemaining = 0;
      galaxyReheatActivations = 0;
      galaxyReheatStepsApplied = 0;
      galaxyLastReheatSubsteps = 0;
      galaxyLastMutualGravity = {
        systems: 0, interactions: 0, traversals: 0, approximations: 0,
        maximumAcceleration: 0, capScale: 1,
      };
      galaxyLastSystemGravity = {
        systems: 0, anchors: 0, satellites: 0, repulsions: 0, surfaceRepulsions: 0,
        maximumRepulsion: 0, maximumSampledAttraction: 0, maximumNetRepulsion: 0,
        minimumSurfaceNetRepulsion: null,
        repulsionPadding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
        repulsionRange: GALAXY_SYSTEM_ANCHOR_REPULSION_RANGE,
        repulsionAcceleration: GALAXY_SYSTEM_ANCHOR_REPULSION_ACCELERATION,
        maximumAcceleration: 0, capScale: 1,
      };
      galaxyLastGravityResponse = {
        systems: 0, moved: 0, ratio: 1, maximumShift: 0, anchorId: null,
      };
      resetGalaxyClock();
    }

    function cancelGalaxyDynamics(resetClock = true) {
      cancelFrame(galaxyFrame);
      galaxyFrame = 0;
      if (resetClock) resetGalaxyClock();
    }

    function galaxyIntegratorOptions() {
      const orbitScale = galaxyRelationOrbitScale(state.settings.link);
      const orbitalSeparationPadding = galaxyOrbitalSeparationPadding(state.settings.repel);
      const orbitalSeparationStrength = galaxyOrbitalSeparationStrength(state.settings.repel);
      return {
        fixedNodeId: activeDragNode ? activeDragNode.id : null,
        dragSource: activeDragNode,
        dragFollowers,
        dragSoftening: activeDragNode ? Math.max(GALAXY_DRAG_GRAVITY_SOFTENING,
          finitePositive(activeDragNode.radius, 2, 160) * 1.5) : GALAXY_DRAG_GRAVITY_SOFTENING,
        gravity: state.settings.gravity,
        softening: galaxyLiveSoftening(),
        centralSoftening: Math.max(36, galaxySoftening() * 5),
        bridgeSoftening: Math.max(24, galaxySoftening() * 4),
        exactLimit: GALAXY_EXACT_LIMIT,
        theta: GALAXY_BARNES_HUT_THETA,
        localPairFraction: GALAXY_LOCAL_PAIR_FRACTION,
        corePairMultiplier: GALAXY_CORE_PAIR_MULTIPLIER,
        /* Evidence bridges remain exported and independently testable, but are not another
           live gravity source. On real 24-system scenes even a 0.35-scaled bridge field added
           enough non-central energy to eject outer systems from the black-hole potential. */
        includeBridges: false,
        /* Every external solar system feels a weak mass-aware field from the others. This is
           independent of evidence links; inverse-square distance naturally favors neighbors,
           while the black-hole potential remains the dominant galaxy-wide force. */
        includeMutualSystems: true,
        mutualSystemGravityFraction: GALAXY_MUTUAL_SYSTEM_GRAVITY_FRACTION,
        mutualSystemSoftening: GALAXY_MUTUAL_SYSTEM_SOFTENING,
        /* Only same-community live relations become springs. Their bounded response makes Link
           distance a real tight/loose control without letting a cross-system evidence edge pull
           two solar systems out of the black-hole hierarchy. */
        includeRelations: true,
        /* Star/planet edges describe topology, not a second radial potential. The selected
           dominant node owns that orbit; non-anchor relations retain the Link control. */
        skipSystemAnchorRelations: true,
        /* Server-authored systems give every member the same explicit anchor id. Keep all of
           those evidence links painted, but let the hierarchy's central potential—not Link
           PBD—own every orbital radius inside that system. */
        skipOrbitalSystemRelations: true,
        /* The bounded positional relation solver is the live authority. Running the older
           velocity spring at the same time continuously added energy around the same target
           and made a few systems look reheated even with a stationary slider. */
        includeRelationSprings: false,
        orbitScale,
        linkSetting: state.settings.link,
        relationStrengthMultiplier: GALAXY_RELATION_STRENGTH_MULTIPLIER,
        relationForceCap: GALAXY_RELATION_FORCE_CAP,
        relationAccelerationCap: GALAXY_RELATION_ACCELERATION_CAP,
        /* PBD uses one contractive exponential response. Scaling the completed displacement
           above one would cross the target and ping-pong on the next frame. */
        relationConstraintStrengthMultiplier:
          GALAXY_RELATION_CONSTRAINT_STRENGTH_MULTIPLIER,
        relationConstraintResponseMultiplier:
          GALAXY_RELATION_CONSTRAINT_RESPONSE_MULTIPLIER,
        relationConstraintRate: GALAXY_RELATION_CONSTRAINT_RATE,
        relationConstraintMaxCorrection: GALAXY_RELATION_CONSTRAINT_MAX_CORRECTION,
        /* Link and separation must share one lower bound. Independent targets made Link pull
           inward and Orbital separation push outward on every tick, which looked exactly like
           repeated reheating even though D3 was off. */
        relationPadding: Math.max(1.5, orbitalSeparationPadding),
        /* The explicit local pressure is what makes Orbital separation visible. Its response
           and target cushion are both 2x the retired normalized control. */
        includeOrbitalSeparation: true,
        orbitalSeparationPadding,
        orbitalSeparationStrength,
        crossCommunitySeparationPadding: GALAXY_CROSS_SYSTEM_REPULSION_PADDING,
        crossCommunitySeparationStrength: orbitalSeparationStrength
          * GALAXY_CROSS_SYSTEM_REPULSION_FRACTION,
        /* Dense hubs sample one immutable phase and receive at most one bounded correction
           per frame, irrespective of how many members touch them. */
        orbitalSeparationMaxCorrection: 4,
        orbitalSeparationMaxVelocityCorrection: 8,
        /* Contacts must not erase a planet's tangential phase. The dominant-star surface
           handles that hard minimum; generic pressure remains active for non-anchor pairs. */
        preserveLocalTangentialVelocity: true,
        /* Dense planet/planet contacts resolve along each declared stellar orbit instead of
           pumping the system radially outward. The manifold projection is mass-balanced and
           keeps a pointer-owned dominant star as its external fixed frame. */
        preserveSystemRadii: true,
        skipSystemAnchorPairs: true,
        systemAnchorExclusionPadding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
        systemAnchorRepulsionRange: GALAXY_SYSTEM_ANCHOR_REPULSION_RANGE,
        systemAnchorRepulsionAcceleration: GALAXY_SYSTEM_ANCHOR_REPULSION_ACCELERATION,
        /* The black-hole contact is independent of the adjustable local separation pressure.
           It is always strong enough to keep painted geometry outside the event horizon. */
        includeBlackHoleExclusion: true,
        blackHoleExclusionPadding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING,
        /* The outer well is intentionally scene-seeded, not coupled to a slider. A cached
           envelope makes its threshold deterministic across normal frames and drag release. */
        includeFarFieldConfinement: true,
        farFieldEnvelopeScale: GALAXY_FAR_FIELD_ENVELOPE_SCALE,
        farFieldMinimumRadius: GALAXY_FAR_FIELD_MIN_RADIUS,
        farFieldSoftFraction: GALAXY_FAR_FIELD_SOFT_FRACTION,
        farFieldAcceleration: GALAXY_FAR_FIELD_ACCELERATION,
        farFieldMaxAcceleration: GALAXY_FAR_FIELD_MAX_ACCELERATION,
        localRelativeSpeedLimit: GALAXY_LOCAL_RELATIVE_SPEED_LIMIT,
        timestep: GALAXY_FIXED_TIMESTEP,
        /* The render loop consumes one fixed 30 Hz physical slice per substep. Passing that
           wall-clock slice explicitly keeps convergence identical after a throttled render
           frame is split into several steps. */
        inwardConvergence: true,
        wallClockSeconds: GALAXY_FRAME_INTERVAL_MS / 1000,
        velocityDecay: GALAXY_VELOCITY_DECAY,
        /* The legacy limit is derived from link distance (14.4 at Galaxy defaults) and can
           clamp an otherwise valid inner orbit. Common-scaling every body then strips angular
           momentum from the entire disk. The physical solver uses only the true emergency cap. */
        speedLimit: MAX_NODE_SPEED,
        /* The smooth local potential prevents singular packing. Even an energy-dissipating
           projection can repeatedly remap phase space in a densely overlapping real scene, so
           collision remains an optional helper rather than part of the persistent clock. */
        includeCollisions: false,
        collisionPadding: 1.5,
        collisionStrength: 0.7,
        collisionIterations: 1,
      };
    }

    function physicsDiagnostics() {
      const data = fg.graphData() || {};
      return Object.assign(galaxyMotionDiagnostics(data.nodes || []), {
        mode: state.settings.mode,
        running,
        frozen: state.settings.frozen === true,
        staticLayout: staticFullLayout,
        renderedNodes: (data.nodes || []).length,
        renderedLinks: (data.links || []).length,
        galaxyLiveNodeLimit: GALAXY_LIVE_NODE_LIMIT,
        galaxyLiveLinkLimit: GALAXY_LIVE_LINK_LIMIT,
        withinGalaxyLiveLimit: galaxySceneWithinLiveLimit(data),
        /* Large paint omits decorative material work while the bounded physical solver can
           remain live when motion is enabled. */
        largeRenderTier: materialLow,
        collapsed,
        reducedMotion: reduced(),
        hidden: pageHidden(),
        dragging: activeDragNode ? activeDragNode.id : null,
        /* Every live body is admitted to the pointer-owned gravity field. Relation and local
           annotations remain visible here, but topology never gates the physical response. */
        dragFollowers: dragFollowers.map(follower => follower.node.id),
        dragFollowerGravity: { ...dragFollowerGravityReport },
        gravitySetting: state.settings.gravity,
        /* Gravity strength is centered on the dominant evidence mass. Local solar-system
           attraction intentionally receives exactly half of this black-hole field. */
        effectiveGravity: galaxyBlackHoleGravityConstant(state.settings.gravity),
        blackHoleGravity: galaxyBlackHoleGravityConstant(state.settings.gravity),
        localGravity: galaxyLocalGravityConstant(state.settings.gravity),
        immediateGravityResponse: { ...galaxyLastGravityResponse },
        systemGravity: { ...galaxyLastSystemGravity },
        mutualSystemGravity: { ...galaxyLastMutualGravity },
        linkSetting: state.settings.link,
        relationOrbitScale: galaxyRelationOrbitScale(state.settings.link),
        relationStrengthMultiplier: GALAXY_RELATION_STRENGTH_MULTIPLIER,
        relationForceCap: GALAXY_RELATION_FORCE_CAP,
        relationAccelerationCap: GALAXY_RELATION_ACCELERATION_CAP,
        relationConstraintStrengthMultiplier:
          GALAXY_RELATION_CONSTRAINT_STRENGTH_MULTIPLIER,
        relationConstraintResponseMultiplier:
          GALAXY_RELATION_CONSTRAINT_RESPONSE_MULTIPLIER,
        relationConstraintMaxCorrection:
          GALAXY_RELATION_CONSTRAINT_MAX_CORRECTION,
        orbitalSeparationSetting: state.settings.repel,
        orbitalSeparationPadding: galaxyOrbitalSeparationPadding(state.settings.repel),
        orbitalSeparationStrength: galaxyOrbitalSeparationStrength(state.settings.repel),
        crossSystemRepulsionPadding: GALAXY_CROSS_SYSTEM_REPULSION_PADDING,
        crossSystemRepulsionStrength: galaxyOrbitalSeparationStrength(state.settings.repel)
          * GALAXY_CROSS_SYSTEM_REPULSION_FRACTION,
        systemAnchorExclusionPadding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
        systemAnchorRepulsionRange: GALAXY_SYSTEM_ANCHOR_REPULSION_RANGE,
        systemAnchorRepulsionAcceleration: GALAXY_SYSTEM_ANCHOR_REPULSION_ACCELERATION,
        systemAnchorExclusion: { ...galaxyLastSystemAnchorExclusion },
        blackHoleExclusionPadding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING,
        blackHoleExclusion: { ...galaxyLastBlackHoleExclusion },
        farFieldEnvelopeScale: GALAXY_FAR_FIELD_ENVELOPE_SCALE,
        farFieldMinimumRadius: GALAXY_FAR_FIELD_MIN_RADIUS,
        farFieldSoftFraction: GALAXY_FAR_FIELD_SOFT_FRACTION,
        farFieldAcceleration: GALAXY_FAR_FIELD_ACCELERATION,
        farFieldMaxAcceleration: GALAXY_FAR_FIELD_MAX_ACCELERATION,
        farFieldConfinement: { ...galaxyLastFarFieldConfinement },
        farFieldGravity: { ...galaxyLastFarFieldGravity },
        active: galaxyDynamicsEligible(),
        scheduled: galaxyFrame !== 0,
        frameIntervalMs: GALAXY_FRAME_INTERVAL_MS,
        timestep: GALAXY_FIXED_TIMESTEP,
        maxSubsteps: GALAXY_MAX_SUBSTEPS,
        reheatActivations: galaxyReheatActivations,
        reheatStepsRemaining: galaxyReheatStepsRemaining,
        reheatStepsApplied: galaxyReheatStepsApplied,
        lastReheatSubsteps: galaxyLastReheatSubsteps,
        velocityDecay: GALAXY_VELOCITY_DECAY,
        frames: galaxyFrames,
        steps: galaxySteps,
        lastSubsteps: galaxyLastSubsteps,
        lastIntegratorKinetic: galaxyLastKinetic,
        lastCollisions: galaxyLastCollisions,
        lastRelationCorrections: galaxyLastRelationCorrections,
        lastRelationCorrectionDistance: galaxyLastRelationDistance,
        lastOrbitalSystemRelationSkips: galaxyLastOrbitalRelationSkips,
        lastOrbitalSeparations: galaxyLastOrbitalSeparations,
        lastCrossSystemSeparations: galaxyLastCrossSystemSeparations,
        lastOrbitalCorrectionDistance: galaxyLastOrbitalCorrection,
        lastLocalVelocityLimits: galaxyLastLocalVelocityLimits,
        localRelativeSpeedLimit: GALAXY_LOCAL_RELATIVE_SPEED_LIMIT,
        systemOrbitSeedSpeedLimit: GALAXY_SYSTEM_ORBIT_SEED_SPEED_LIMIT,
        speedCapActivations: galaxySpeedCaps,
      });
    }

    function runGalaxyFrame(timestamp) {
      galaxyFrame = 0;
      if (!galaxyDynamicsEligible()) {
        resetGalaxyClock();
        return;
      }
      const now = Number.isFinite(timestamp)
        ? timestamp
        : (window.performance && typeof window.performance.now === 'function'
          ? window.performance.now() : Date.now());
      /* The first visible frame receives one ordinary step, never the wall time accumulated
         while a tab was hidden, the graph was frozen, or a pointer owned a node. */
      if (galaxyLastFrameTime === null) {
        galaxyLastFrameTime = now;
        galaxyAccumulator = GALAXY_FRAME_INTERVAL_MS;
      } else {
        const elapsed = Math.max(0, Math.min(
          GALAXY_FRAME_INTERVAL_MS * GALAXY_MAX_SUBSTEPS,
          now - galaxyLastFrameTime
        ));
        galaxyLastFrameTime = now;
        galaxyAccumulator = Math.min(
          GALAXY_FRAME_INTERVAL_MS * GALAXY_MAX_SUBSTEPS,
          galaxyAccumulator + elapsed
        );
      }
      const ordinarySubsteps = Math.min(GALAXY_MAX_SUBSTEPS,
        Math.floor((galaxyAccumulator + 1e-9) / GALAXY_FRAME_INTERVAL_MS));
      /* Galaxy is already live. Reheat must never add fixed slices or fast-forward time, even
         if a future caller accidentally leaves a stale non-zero budget in the telemetry slot. */
      const reheatSubsteps = 0;
      const substeps = ordinarySubsteps + reheatSubsteps;
      galaxyLastSubsteps = substeps;
      galaxyLastReheatSubsteps = reheatSubsteps;
      if (substeps > 0) {
        const data = fg.graphData() || { nodes: [], links: [] };
        for (let index = 0; index < substeps; index++) {
          const report = integrateGalaxyLeapfrog(
            data.nodes || [], data.links || [], raw.community_bridges || [],
            galaxyIntegratorOptions()
          );
          galaxySteps++;
          galaxyLastKinetic = report.kinetic;
          galaxyLastCollisions = report.collisions;
          galaxyLastRelationCorrections = report.relationConstraint.applied;
          galaxyLastRelationDistance = report.relationConstraint.correctedDistance;
          galaxyLastOrbitalRelationSkips = report.relationConstraint.skippedOrbitalSystem || 0;
          galaxyLastOrbitalSeparations = report.orbitalSeparation.overlaps;
          galaxyLastCrossSystemSeparations =
            report.orbitalSeparation.crossCommunityOverlaps || 0;
          galaxyLastOrbitalCorrection = report.orbitalSeparation.correctionDistance;
          galaxyLastSystemAnchorExclusion = report.systemAnchorExclusion;
          galaxyLastBlackHoleExclusion = report.blackHoleExclusion;
          galaxyLastFarFieldConfinement = report.farFieldConfinement;
          galaxyLastFarFieldGravity = report.farFieldGravity;
          galaxyLastLocalVelocityLimits = report.systemVelocity.limitedSystems;
          galaxyLastSystemGravity = report.systemGravity;
          galaxyLastMutualGravity = report.mutualGravity;
          dragFollowerGravityReport = report.dragGravity;
          if (report.speedCapped) galaxySpeedCaps++;
        }
        galaxyAccumulator = Math.max(0,
          galaxyAccumulator - ordinarySubsteps * GALAXY_FRAME_INTERVAL_MS);
        galaxyReheatStepsRemaining = Math.max(0,
          galaxyReheatStepsRemaining - reheatSubsteps);
        galaxyReheatStepsApplied += reheatSubsteps;
        galaxyFrames++;
        invalidate();
        if (typeof opts.onPhysics === 'function') opts.onPhysics(physicsDiagnostics());
      }
      if (galaxyDynamicsEligible()) galaxyFrame = requestFrame(runGalaxyFrame);
    }

    function scheduleGalaxyDynamics(resetClock = false) {
      if (resetClock) resetGalaxyClock();
      if (!galaxyDynamicsEligible()) {
        cancelGalaxyDynamics(resetClock);
        return;
      }
      if (!galaxyFrame) galaxyFrame = requestFrame(runGalaxyFrame);
    }

    function setGalaxySeedFlag(node, name, value) {
      if (!value) {
        delete node[name];
        return;
      }
      Object.defineProperty(node, name, {
        value: true, writable: true, configurable: true, enumerable: false
      });
    }

    function saveGalaxyPhase() {
      raw.nodes.forEach(node => {
        if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
        galaxySavedPhase.set(node.id, {
          x: node.x, y: node.y,
          vx: Number.isFinite(node.vx) ? node.vx : 0,
          vy: Number.isFinite(node.vy) ? node.vy : 0,
          orbitSeeded: node.__galaxyOrbitSeeded === true,
          systemOrbitSeeded: node.__galaxySystemOrbitSeeded === true,
        });
      });
    }

    function restoreGalaxyPhase() {
      raw.nodes.forEach(node => {
        const saved = galaxySavedPhase.get(node.id);
        const server = galaxyServerPhase.get(node.id);
        const phase = saved || server;
        node.x = phase && Number.isFinite(phase.x) ? phase.x : undefined;
        node.y = phase && Number.isFinite(phase.y) ? phase.y : undefined;
        node.vx = saved && Number.isFinite(saved.vx) ? saved.vx : 0;
        node.vy = saved && Number.isFinite(saved.vy) ? saved.vy : 0;
        node.fx = undefined;
        node.fy = undefined;
        setGalaxySeedFlag(node, '__galaxyOrbitSeeded', !!(saved && saved.orbitSeeded));
        setGalaxySeedFlag(
          node, '__galaxySystemOrbitSeeded', !!(saved && saved.systemOrbitSeeded)
        );
      });
      ensureGalaxyPositions(raw.nodes, raw.meta && raw.meta.layout_seed);
    }

    function transitionGalaxyMode(previousMode, nextMode) {
      if (previousMode === nextMode) return;
      cancelGalaxyDynamics(true);
      if (previousMode === 'galaxy') saveGalaxyPhase();
      if (nextMode === 'galaxy') {
        /* A legacy settings timer must not fire after Galaxy takes ownership and reset D3's
           countdown underneath the fixed clock. Lowering an existing target is not a wake. */
        const hadSoftAlphaTimer = softAlphaTimer !== 0;
        clearTimeout(softAlphaTimer);
        softAlphaTimer = 0;
        if (hadSoftAlphaTimer && typeof fg.d3AlphaTarget === 'function') fg.d3AlphaTarget(0);
        restoreGalaxyPhase();
      }
      /* Never hand force-graph the array that the other integrator mutated. A fresh visible()
         projection preserves object identity for nodes but prevents its cached legacy cluster
         or link endpoint objects from contaminating the restored phase space. */
      seeded = null;
      fullLayoutDirty = true;
    }

    // Rendering while frozen deliberately gives force-graph a one-tick budget. Keep the
    // matching live values in one place so unfreezing after a style, scope, or data render
    // cannot reheat against that stale one-tick budget.
    function setSimulationBudget(live, fullyStopped = false) {
      const simulate = live && !staticFullLayout;
      if (fg.cooldownTime) fg.cooldownTime(simulate ? (large ? 1100 : 2200) : 0);
      if (fg.cooldownTicks) fg.cooldownTicks(
        simulate ? (large ? 80 : 160) : (fullyStopped ? 0 : 1)
      );
      if (fg.warmupTicks) fg.warmupTicks(simulate ? (large ? 18 : 40) : 0);
    }
    function prepareReheat() {
      const nodes = fg.graphData().nodes || [];
      nodes.forEach(node => {
        if (node === activeDragNode || node.fx !== undefined || node.fy !== undefined) {
          node.vx = 0;
          node.vy = 0;
          return;
        }
        node.vx = Number.isFinite(node.vx) ? node.vx * 0.25 : 0;
        node.vy = Number.isFinite(node.vy) ? node.vy * 0.25 : 0;
      });
    }

    function supportsSoftAlpha() {
      return typeof d3 !== 'undefined'
        && typeof fg.d3AlphaTarget === 'function'
        && typeof fg.resetCountdown === 'function';
    }

    function releaseSoftAlpha() {
      clearTimeout(softAlphaTimer);
      softAlphaTimer = 0;
      if (!supportsSoftAlpha()) return;
      fg.d3AlphaTarget(0);
      fg.resetCountdown();
    }

    function softReheat() {
      if (!supportsSoftAlpha()) {
        /* Keep the dependency-light Node harness and older vendor bundles working. The real
           browser bundle takes the bounded alpha-target path above. */
        if (fg.d3ReheatSimulation) fg.d3ReheatSimulation();
        return;
      }
      clearTimeout(softAlphaTimer);
      softAlphaTimer = 0;
      fg.d3AlphaTarget(SETTINGS_ALPHA_TARGET);
      fg.resetCountdown();
      softAlphaTimer = setTimeout(() => {
        softAlphaTimer = 0;
        if (!destroyed && !activeDragNode) releaseSoftAlpha();
      }, ALPHA_TARGET_HOLD_MS);
    }

    function cancelSoftAlphaForDrag() {
      if (!softAlphaTimer) return;
      clearTimeout(softAlphaTimer);
      softAlphaTimer = 0;
      /* Lowering an already-active target cannot wake the simulation and needs no countdown
         reset. Without this cancellation, a 180 ms settings timer can fire just after pointer
         release and make an otherwise localized drag appear to reheat the whole galaxy. */
      if (typeof fg.d3AlphaTarget === 'function') fg.d3AlphaTarget(0);
    }

    function schedulePhysicsUpdate() {
      cancelAutoFit();
      physicsReheatPending = true;
      if (suspended || physicsFrame || destroyed) return;
      /* The dependency-light Node harness has no browser frame clock. Keep its public
         behaviour synchronous while browsers coalesce a burst of range-input events. */
      if (typeof window === 'undefined' || typeof window.requestAnimationFrame !== 'function') {
        physicsReheatPending = false;
        render(false, true);
        return;
      }
      physicsFrame = requestFrame(() => {
        physicsFrame = 0;
        if (destroyed || suspended || !physicsReheatPending) return;
        physicsReheatPending = false;
        render(false, true);
      });
    }

    function render(fit, reheat, dragging = false) {
      if (destroyed) return;
      if (suspended) {
        pendingRender = pendingRender
          ? [pendingRender[0] || fit, pendingRender[1] || reheat, pendingRender[2] || dragging]
          : [fit, reheat, dragging];
        return;
      }
      const motion = !state.settings.frozen;
      const reducedMotion = reduced();
      const next = visible();
      /* Reuse the arrays force-graph already holds when the view is unchanged: the sizing and
         colouring pass below must write onto the objects the vendor is painting from, and the
         collapsed view hands out freshly built cluster nodes on every call. */
      const reused = sameData(seeded, next);
      const data = reused ? seeded : next;
      const fullGraph = state.renderMode === 'full';
      const galaxyMode = state.settings.mode === 'galaxy';
      const wasStatic = staticFullLayout;
      const overGalaxyLiveLimit = !galaxySceneWithinLiveLimit(data);
      const overFullForceLimit = data.nodes.length > FULL_FORCE_NODE_LIMIT
        || data.links.length > FULL_FORCE_LINK_LIMIT;
      staticFullLayout = galaxyMode
        ? overGalaxyLiveLimit
        : fullGraph && overFullForceLimit;
      materialLow = data.nodes.length > LARGE_NODE_LIMIT || data.links.length > LARGE_LINK_LIMIT;
      large = fullGraph || data.nodes.length > LARGE_NODE_LIMIT || data.links.length > LARGE_LINK_LIMIT;
      dense = data.links.length > DENSE_LINK_LIMIT;
      const sizeMetric = n => state.sizeBy === 'betweenness' ? (n.betweenness || 0) : ((n.degree || 0) / Math.max(1, maxDeg));
      data.nodes.forEach(n => {
        const base = (state.settings.size || 3);
        n.radius = galaxyMode
          ? evidenceNodeRadius(n, base)
          : graphNodeRadius(n, base, sizeMetric(n));
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
      /* graphData() synchronously runs configured warmup ticks. Detach the legacy simulation
         before handing it restored Galaxy coordinates, or Compact's old link/charge field gets
         one last chance to corrupt the physical phase before the custom clock even starts. */
      if (galaxyMode) disableD3GalaxyIntegration();
      if (!reused) {
        if (staticFullLayout) {
          if (galaxyMode) pinGalaxySceneLayout(data);
          else pinFullGraphLayout(data);
          fullLayoutDirty = false;
        } else if (galaxyMode) {
          /* Canonical v5 scenes already carry compact deterministic coordinates. Compatibility
             payloads and direct embeds may not: D3 is intentionally disabled in Galaxy mode,
             so fill only those missing positions before the one-shot orbital seed. Finite
             server coordinates are preserved byte-for-byte by ensureGalaxyPositions(). */
          ensureGalaxyPositions(data.nodes, raw.meta && raw.meta.layout_seed);
          releasePinnedPositions(data);
          seedGalaxyOrbits(
            data.nodes, raw.meta && raw.meta.layout_seed,
            state.settings.gravity, galaxyLiveSoftening(), reducedMotion,
            GALAXY_LOCAL_PAIR_FRACTION, GALAXY_CORE_PAIR_MULTIPLIER
          );
          seedGalaxySystemOrbits(
            data.nodes, raw.meta && raw.meta.layout_seed,
            state.settings.gravity, Math.max(36, galaxySoftening() * 5), reducedMotion
          );
        } else clearPinnedPositions(data);
        /* graphData() may paint synchronously. Enforce the event horizon after every layout
           seed (including the pinned oversized layout) before the vendor sees the payload. */
        if (galaxyMode) {
          const prePaintHorizon = applyGalaxyBlackHoleExclusion(
            data.nodes, { padding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING }
          );
          const preStarExclusion = applyGalaxySystemAnchorExclusion(data.nodes, {
            padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
            fixAnchors: true,
          });
          /* Static and reused payloads do not enter the live integrator, but still paint the
             same finite galaxy. Apply the exact outer extent before handing coordinates to
             force-graph, then reassert the inner horizon after any inward system shift. */
          galaxyLastFarFieldConfinement = applyGalaxyFarFieldConfinement(data.nodes, {
            includeFarFieldConfinement: true,
            farFieldEnvelopeScale: GALAXY_FAR_FIELD_ENVELOPE_SCALE,
            farFieldMinimumRadius: GALAXY_FAR_FIELD_MIN_RADIUS,
            farFieldSoftFraction: GALAXY_FAR_FIELD_SOFT_FRACTION,
          });
          galaxyLastFarFieldGravity = {
            anchorId: galaxyLastFarFieldConfinement.anchorId,
            envelopeRadius: galaxyLastFarFieldConfinement.envelopeRadius,
            softRadius: galaxyLastFarFieldConfinement.softRadius,
            samples: 0, acceleratedSystems: 0, acceleratedCoreNodes: 0,
            acceleratedFixedFollowers: 0, maximumAcceleration: 0,
          };
          const postOuterHorizon = applyGalaxyBlackHoleExclusion(
            data.nodes, { padding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING }
          );
          galaxyLastFarFieldConfinement.annulus = applyGalaxyAnnularBounds(data.nodes, {
            includeFarFieldConfinement: true,
            blackHoleExclusionPadding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING,
          });
          const postStarExclusion = applyGalaxySystemAnchorExclusion(data.nodes, {
            padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
            fixAnchors: true,
          });
          galaxyLastSystemAnchorExclusion = combineGalaxySystemAnchorExclusions(
            [preStarExclusion, postStarExclusion]
          );
          const postStarHorizon = applyGalaxyBlackHoleExclusion(
            data.nodes, { padding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING }
          );
          galaxyLastBlackHoleExclusion = combineGalaxyBlackHoleExclusions(
            [prePaintHorizon, postOuterHorizon, postStarHorizon]
          );
        }
        fg.graphData(data);
        seeded = data;
      } else if (staticFullLayout && fullLayoutDirty) {
        if (galaxyMode) pinGalaxySceneLayout(data);
        else pinFullGraphLayout(data);
        fullLayoutDirty = false;
      } else if (wasStatic && !staticFullLayout) {
        releasePinnedPositions(data);
      }
      if (reused && galaxyMode && !staticFullLayout) {
        seedGalaxyOrbits(
          data.nodes, raw.meta && raw.meta.layout_seed,
          state.settings.gravity, galaxyLiveSoftening(), reducedMotion,
          GALAXY_LOCAL_PAIR_FRACTION, GALAXY_CORE_PAIR_MULTIPLIER
        );
        seedGalaxySystemOrbits(
          data.nodes, raw.meta && raw.meta.layout_seed,
          state.settings.gravity, Math.max(36, galaxySoftening() * 5), reducedMotion
        );
      }
      /* Reused arrays bypass graphData(); size changes, static repins, and restored phases still
         receive the same strict painted-edge invariant before the next redraw. */
      if (reused && galaxyMode) {
        const prePaintHorizon = applyGalaxyBlackHoleExclusion(
          data.nodes, { padding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING }
        );
        const preStarExclusion = applyGalaxySystemAnchorExclusion(data.nodes, {
          padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
          fixAnchors: true,
        });
        galaxyLastFarFieldConfinement = applyGalaxyFarFieldConfinement(data.nodes, {
          includeFarFieldConfinement: true,
          farFieldEnvelopeScale: GALAXY_FAR_FIELD_ENVELOPE_SCALE,
          farFieldMinimumRadius: GALAXY_FAR_FIELD_MIN_RADIUS,
          farFieldSoftFraction: GALAXY_FAR_FIELD_SOFT_FRACTION,
        });
        galaxyLastFarFieldGravity = {
          anchorId: galaxyLastFarFieldConfinement.anchorId,
          envelopeRadius: galaxyLastFarFieldConfinement.envelopeRadius,
          softRadius: galaxyLastFarFieldConfinement.softRadius,
          samples: 0, acceleratedSystems: 0, acceleratedCoreNodes: 0,
          acceleratedFixedFollowers: 0, maximumAcceleration: 0,
        };
        const postOuterHorizon = applyGalaxyBlackHoleExclusion(
          data.nodes, { padding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING }
        );
        galaxyLastFarFieldConfinement.annulus = applyGalaxyAnnularBounds(data.nodes, {
          includeFarFieldConfinement: true,
          blackHoleExclusionPadding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING,
        });
        const postStarExclusion = applyGalaxySystemAnchorExclusion(data.nodes, {
          padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
          fixAnchors: true,
        });
        galaxyLastSystemAnchorExclusion = combineGalaxySystemAnchorExclusions(
          [preStarExclusion, postStarExclusion]
        );
        const postStarHorizon = applyGalaxyBlackHoleExclusion(
          data.nodes, { padding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING }
        );
        galaxyLastBlackHoleExclusion = combineGalaxyBlackHoleExclusions(
          [prePaintHorizon, postOuterHorizon, postStarHorizon]
        );
      }
      applyForces();
      fg.autoPauseRedraw(!needsContinuousFrames());
      /* Bound the simulation the way the classic path does. Without these force-graph keeps its
         15-second default window, so every load and every reheat of a large store runs the
         layout — and repaints every node and link — for more than ten seconds longer. */
      setSimulationBudget(galaxyMode ? false : motion, galaxyMode);
      /* D3 is only the renderer in Galaxy mode. Its alpha, velocity decay and countdown are
         intentionally untouched; the fixed-step clock owns all three physical concerns. */
      if (!galaxyMode && fg.d3AlphaDecay) fg.d3AlphaDecay(staticFullLayout ? 1 : alphaDecay());
      if (!galaxyMode && fg.d3VelocityDecay) {
        fg.d3VelocityDecay(large ? 0.45 : 0.38);
      }
      if (fg.linkCurvature) {
        fg.linkCurvature(dense ? 0 : ((PRESETS[state.settings.mode] || PRESETS.compact).curve || 0));
      }
      fg.linkDirectionalArrowLength(dense ? 0 : 0.625).linkDirectionalArrowRelPos(1);
      applyLinkLabels();
      if (fg.linkDirectionalParticles) {
        const flowing = !fullGraph
          && state.settings.flow !== false
          && motion
          && !reducedMotion
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
      if (!galaxyMode && reheat && motion && !staticFullLayout && !state.settings.frozen) {
        prepareReheat();
        softReheat();
      }
      if (!galaxyMode && (staticFullLayout || state.settings.frozen || !motion)
        && fg.d3AlphaDecay) { /* keep painting, stop layout */ fg.d3AlphaDecay(1); }
      if (galaxyMode) scheduleGalaxyDynamics(!reused || wasStatic !== staticFullLayout);
      else cancelGalaxyDynamics(true);
      /* Nothing was reseeded, so force-graph's own change detection saw no reason to repaint —
         but Style, Color by and Labels all just changed how the *same* data must be drawn. */
      if (reused) invalidate();
      if (fit) {
        const animateFit = motion && !reducedMotion;
        cancelAutoFit();
        fitTimer = setTimeout(() => { if (!destroyed) autoFit(animateFit ? 600 : 0, 40); }, animateFit ? 320 : 0);
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

    function dragNodeEligible(node) {
      return !!node && !node.ghost && !node._historyGhost
        && node.static !== true && node.frozen !== true;
    }

    function dragFollowerEligible(node) {
      /* The evidence black hole may be the dragged primary, but it can never be displaced as
         another body's follower. The fixed Galaxy step owns its origin invariant. */
      return dragNodeEligible(node) && node.anchor_role !== 'global';
    }

    /* Every live body participates in the dragged mass field. Evidence relations and local
       membership annotate stronger structure, while distance alone governs unlinked bodies.
       This is intentionally not a graph-neighbour filter: a nearby unlinked star must feel the
       same softened gravity as a linked one, and distant systems simply receive a weaker tail. */
    function captureDragFollowers(node) {
      const data = fg.graphData() || {};
      const nodes = Array.isArray(data.nodes) ? data.nodes : [];
      const related = new Map();
      (Array.isArray(data.links) ? data.links : []).forEach(link => {
        if (!link || link.ghost || link._historyGhost || link.static === true) return;
        const source = linkEndpoint(link, 'source');
        const target = linkEndpoint(link, 'target');
        const otherId = source === node.id ? target : (target === node.id ? source : null);
        if (otherId != null && !related.has(otherId)) related.set(otherId, link);
      });
      const followers = [];
      if (state.settings.mode === 'galaxy') nodes.forEach(other => {
        if (!other || other.id === node.id
          || !dragFollowerEligible(other)
          || !Number.isFinite(other.x) || !Number.isFinite(other.y)) return;
        const distance = Math.hypot(other.x - node.x, other.y - node.y);
        const link = related.get(other.id) || null;
        const proximity = link ? 'related'
          : communityKey(other) === communityKey(node) ? 'system'
            : distance <= GALAXY_DRAG_GRAVITY_CAPTURE_RADIUS ? 'nearby' : 'field';
        followers.push({ node: other, link, proximity, distance });
      });
      else nodes.forEach(other => {
        const link = other ? related.get(other.id) : null;
        if (!link || !dragFollowerEligible(other)
          || !Number.isFinite(other.x) || !Number.isFinite(other.y)) return;
        followers.push({ node: other, link, proximity: 'related',
          distance: Math.hypot(other.x - node.x, other.y - node.y) });
      });
      return followers;
    }

    function followDraggedNode(node) {
      /* Re-sample proximity at the current pointer position so bodies encountered along the
         path begin responding; direct relations and same-system members remain included. */
      dragFollowers = captureDragFollowers(node);
      /* The fixed-step solver samples this source/follower set. Pointermove only updates the
         source position and membership; it never stacks a displacement or velocity impulse. */
      dragFollowerGravityReport = {
        applied: dragFollowers.length, maximumAcceleration: 0, maximumPull: 0,
      };
    }

    function beginNodeDrag(node) {
      if (destroyed || state.settings.frozen || staticFullLayout || !dragNodeEligible(node)) return false;
      if (activeDragNode) return activeDragNode.id === node.id;
      setActiveDragNode(node);
      dragFollowers = captureDragFollowers(node);
      dragFollowerGravityReport = { applied: 0, maximumAcceleration: 0, maximumPull: 0 };
      /* The graph keeps evolving while the pointer owns this node. The custom integrator treats
         it as a fixed moving mass source; no global force is detached and no alpha is changed. */
      cancelSoftAlphaForDrag();
      dragPreVelocity = { vx: Number.isFinite(node.vx) ? node.vx : 0, vy: Number.isFinite(node.vy) ? node.vy : 0 };
      node.vx = 0;
      node.vy = 0;
      if (state.settings.mode === 'galaxy') scheduleGalaxyDynamics(false);
      return true;
    }

    function finishNodeDrag(node) {
      if (!node || !activeDragNode || activeDragNode.id !== node.id) return;
      const retainAnchor = state.settings.frozen || staticFullLayout;
      if (!retainAnchor) {
        node.fx = undefined;
        node.fy = undefined;
      }
      setActiveDragNode(null);
      dragFollowers = [];
      if (state.settings.mode === 'galaxy' && dragPreVelocity) {
        node.vx = dragPreVelocity.vx;
        node.vy = dragPreVelocity.vy;
      } else {
        node.vx = 0;
        node.vy = 0;
      }
      dragPreVelocity = null;
      if (state.settings.mode === 'galaxy') {
        disableD3GalaxyIntegration();
        scheduleGalaxyDynamics(false);
      }
    }

    /* A drag uses fx/fy only while the pointer is down. The fixed-step Galaxy clock remains
       live throughout the gesture; pointer-up merely releases that one moving mass source. */
    fg.backgroundColor('rgba(0,0,0,0)').nodeRelSize(1)
      .enableNodeDrag(false).autoPauseRedraw(true)
      /* force-graph's default `nodeLabel`/`linkLabel` is the literal accessor "name", and its
         tooltip renders a string label with innerHTML. Node names here are entity labels
         extracted from ingested memories — untrusted input — so both accessors are set
         explicitly and escaped rather than left on the vendor default. */
      .nodeLabel(node => esc(nodeName(node)))
      .linkLabel(link => esc(link && link.label ? link.label : ''))
      .onRenderFramePre((ctx, scale) => { try { styleBackground(ctx, scale); } catch (e) { } })
      .onRenderFramePost((ctx, scale) => {
        try {
          const currentData = fg.graphData() || {};
          if (!Array.isArray(currentData.nodes)) return;
          for (const node of currentData.nodes) paintNodeLabel(node, ctx, scale);
        } catch (e) { /* label pass must never break the render loop */ }
      })
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
      .onBackgroundClick(() => { if (opts.onBackgroundClick) opts.onBackgroundClick(); })
      .onZoom(z => {
        zoom = z.k || 1;
        if (state.collapse !== 'auto') return;
        /* Layout presets can legitimately occupy more of the canvas than the compact default.
           Keep auto-collapse for true zoom-out, but do not hide a freshly selected arrangement
           merely because its fit scale is below the old, overly eager threshold. */
        const collapseThreshold = state.settings.mode === 'communities' ? 0.22 : 0.42;
        const canAutoCollapse = autoCollapseEligible();
        const next = canAutoCollapse && zoom < collapseThreshold;
        if (next !== collapsed) {
          collapsed = next;
          render(false, true);
          if (opts.onCollapseChange) opts.onCollapseChange(collapsed);
        }
      });

    /* Older force-graph bundles do not expose a drag-start accessor. Manual pointer capture
       remains the primary controller, but register vendor callbacks when available. */
    if (typeof fg.onNodeDragStart === 'function') {
      fg.onNodeDragStart(node => {
        beginNodeDrag(node);
      });
    }
    if (typeof fg.onNodeDragEnd === 'function') {
      fg.onNodeDragEnd(node => finishNodeDrag(node));
    }

    /* force-graph's built-in drag always reheats the entire simulation. The scoped controller
       instead turns one node into a moving gravity source while the existing solver stays live.
       Capturing pointer-down prevents the vendor's alpha kick from seeing node gestures while
       preserving its background pan/zoom path. */
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
          finishNodeDrag(current.node);
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
        let started = false;
        if (!manualDrag.dragged) {
          if (Math.hypot(dx, dy) < 3) {
            event.preventDefault();
            event.stopPropagation();
            return;
          }
          manualDrag.dragged = true;
          started = true;
        }
        if (started && !beginNodeDrag(manualDrag.node)) {
          manualDrag.dragged = false;
          return;
        }
        const node = manualDrag.node;
        node.x = node.fx = point.x + manualDrag.offsetX;
        node.y = node.fy = point.y + manualDrag.offsetY;
        followDraggedNode(node);
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
        if (!dragNodeEligible(candidate)) return;
        cancelAutoFit();
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
      if (destroyed) return;
      cancelGalaxyDynamics(true);
      resetGalaxyDiagnostics();
      galaxyServerPhase.clear();
      galaxySavedPhase.clear();
      const inputNodes = Array.isArray(data && data.nodes) ? data.nodes : [];
      const nodes = [], nodeIds = new Set();
      inputNodes.forEach(node => {
        if (!node || (typeof node !== 'object' && typeof node !== 'function')
          || !validNodeId(node.id) || nodeIds.has(node.id)) return;
        nodeIds.add(node.id);
        const copy = Object.assign({}, node, { name: nodeName(node) });
        galaxyServerPhase.set(copy.id, Object.freeze({
          x: Number.isFinite(copy.x) ? copy.x : undefined,
          y: Number.isFinite(copy.y) ? copy.y : undefined,
        }));
        Object.defineProperty(copy, '_historyGhost', {
          value: node.ghost === true, writable: true, configurable: true, enumerable: false
        });
        nodes.push(copy);
      });
      const linkInput = Array.isArray(data && data.links)
        ? data.links
        : (Array.isArray(data && data.edges) ? data.edges : []);
      const links = linkInput
        .filter(link => link && (typeof link === 'object' || typeof link === 'function'))
        .map(link => {
          const source = linkEndpoint(link, 'source'), target = linkEndpoint(link, 'target');
          const copy = Object.assign({}, link, { source, target });
          Object.defineProperty(copy, '_historyGhost', {
            value: link.ghost === true, writable: true, configurable: true, enumerable: false
          });
          return copy;
        })
        .filter(link => link.source != null && link.target != null
          && nodeIds.has(link.source) && nodeIds.has(link.target));
      const suggestions = (Array.isArray(data && data.suggestions) ? data.suggestions : [])
        .filter(link => link && (typeof link === 'object' || typeof link === 'function'))
        .map(link => Object.assign({}, link, {
          source: linkEndpoint(link, 'source'), target: linkEndpoint(link, 'target')
        }))
        .filter(link => link.source != null && link.target != null);
      const sceneCommunities = (Array.isArray(data && data.communities) ? data.communities : [])
        .filter(community => community && typeof community === 'object')
        .map(community => ({ ...community }));
      const declaredCommunityIds = [];
      const extraCommunityIds = [];
      const seenCommunityIds = new Set();
      sceneCommunities.forEach(community => {
        if (community.id === undefined || community.id === null) return;
        const key = String(community.id);
        if (!seenCommunityIds.has(key)) {
          seenCommunityIds.add(key);
          declaredCommunityIds.push(key);
        }
      });
      nodes.forEach(node => {
        const supplied = node.community_id !== undefined && node.community_id !== null
          ? node.community_id
          : (typeof node.community === 'string' ? node.community : null);
        if (supplied === null) return;
        const key = String(supplied);
        node.community_id = key;
        if (!seenCommunityIds.has(key)) {
          seenCommunityIds.add(key);
          extraCommunityIds.push(key);
        }
      });
      /* Scene order is stable and meaningful (mass-ranked). Unknown compatibility IDs are
         appended deterministically so node colour and grouping never depend on payload order. */
      const communityOrder = declaredCommunityIds.concat(extraCommunityIds.sort());
      const communityIndex = new Map(communityOrder.map((id, index) => [id, index]));
      nodes.forEach(node => {
        if (node.community_id !== undefined && communityIndex.has(String(node.community_id))) {
          node.community = communityIndex.get(String(node.community_id));
        }
      });
      const sceneMetaSource = data && (data.meta || data.metadata);
      const sceneMeta = sceneMetaSource && typeof sceneMetaSource === 'object'
        ? { ...sceneMetaSource } : {};
      if (sceneMeta.layout_seed === undefined && data && data.layout_seed !== undefined) {
        sceneMeta.layout_seed = data.layout_seed;
      }
      const suppliedBridges = Array.isArray(data && data.community_bridges)
        ? data.community_bridges
        : (Array.isArray(data && data.communityBridges) ? data.communityBridges : []);
      let communityBridges = suppliedBridges
        .filter(bridge => bridge && typeof bridge === 'object')
        .map(bridge => ({ ...bridge }));
      /* A fresh payload means fresh node objects, so the cached seed is stale even when the
         ids are identical — force-graph must be re-pointed at the new objects or the render
         below would style ones nobody is painting from. */
      seeded = null;
      fullLayoutDirty = true;
      raw = {
        nodes, links, suggestions, communities: sceneCommunities,
        community_bridges: communityBridges, meta: sceneMeta
      };
      adj = communities(raw.nodes, raw.links);
      const deg = Object.create(null);
      raw.links.forEach(l => {
        if (l.ghost) return;
        const s = linkEndpoint(l, 'source'), t = linkEndpoint(l, 'target');
        deg[s] = (deg[s] || 0) + 1;
        deg[t] = (deg[t] || 0) + 1;
      });
      raw.nodes.forEach(n => { n.degree = deg[n.id] || 0; n.betweenness = 0; });
      maxDeg = maxOf(raw.nodes.map(n => n.degree), 1);
      sanitizeEvidenceMetrics(raw.nodes, maxDeg);
      if (!communityBridges.length) {
        communityBridges = fallbackCommunityBridges(raw.nodes, raw.links);
        raw.community_bridges = communityBridges;
      }
      const ranked = [...raw.nodes].sort((a, b) => b.degree - a.degree);
      ranked.forEach((n, i) => { n.rank = i; n.hub = i < 6; });
      // A refresh can replace the workspace while a prior focus/highlight still names an old id.
      // Drop those references before visible() so the next render cannot isolate an empty view or
      // paint a stale hover neighbourhood.
      if (state.focusId != null && !nodeIds.has(state.focusId)) state.focusId = null;
      if (hilite != null && !nodeIds.has(hilite)) hilite = null;
      hoverSet = hilite == null ? null : new Set([hilite].concat(adj[hilite] || []));
      // Bridge *edges* are cheap (linear) and feed the stats readout, so they stay eager.
      const liveLinks = raw.links.filter(link => !link.ghost);
      // Build adjacency from live links only — ghost links would create false alternative
      // paths in the DFS, causing real bridges to be missed.
      liveAdj = Object.create(null);
      raw.nodes.forEach(n => { liveAdj[n.id] = []; });
      liveLinks.forEach(l => {
        const s = linkEndpoint(l, 'source'), t = linkEndpoint(l, 'target');
        if (liveAdj[s]) liveAdj[s].push(t);
        if (liveAdj[t]) liveAdj[t].push(s);
      });
      findBridges(raw.nodes, liveLinks, liveAdj);
      raw.links.filter(link => link.ghost)
        .forEach(link => { link.bridge = false; });
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
      const next = patch && typeof patch === 'object' ? patch : {};
      const wasFrozen = state.settings.frozen === true;
      const isUnfreezing = wasFrozen && next.frozen === false;
      const layoutChanged = LAYOUT_KEYS.some(k => next[k] !== undefined);
      const previousMode = state.settings.mode;
      const previousGravity = Number(state.settings.gravity);
      if (layoutChanged) {
        fullLayoutDirty = true;
        cancelAutoFit();
      }
      Object.assign(state.settings, next);
      transitionGalaxyMode(previousMode, state.settings.mode);
      const nextGravity = Number(state.settings.gravity);
      const gravityChanged = next.gravity !== undefined
        && Number.isFinite(previousGravity) && Number.isFinite(nextGravity)
        && Math.abs(nextGravity - previousGravity) > 1e-12;
      if (gravityChanged && previousMode === 'galaxy' && state.settings.mode === 'galaxy'
        && !state.settings.frozen && !staticFullLayout && !collapsed) {
        const data = fg.graphData() || {};
        galaxyLastGravityResponse = applyGalaxyGravitySettingResponse(
          data.nodes || [], previousGravity, nextGravity,
          { fixedNodeId: activeDragNode ? activeDragNode.id : null }
        );
      }
      if (state.settings.mode === 'galaxy') {
        if (previousMode !== 'galaxy' && state.sizeBy !== 'mass') legacySizeBy = state.sizeBy;
        state.sizeBy = 'mass';
      } else if (previousMode === 'galaxy' && state.sizeBy === 'mass') {
        state.sizeBy = legacySizeBy;
      }
      /* Classic synchronises the complete GSET object during a redraw. If the visible switch
         was turned off by that sync after an earlier freeze, a plain render restores the
         paint settings but leaves d3 at its old alpha/charge state. Route the transition
         through the same release path as the visible control so both dashboards resume. */
      if (isUnfreezing) {
        api.freeze(false);
        return;
      }
      render(false, false);
      if (layoutChanged) schedulePhysicsUpdate();
    };
    api.setPreset = name => {
      const p = PRESETS[name] || PRESETS.compact;
      const previousMode = state.settings.mode;
      state.settings.mode = PRESETS[name] ? name : 'compact';
      transitionGalaxyMode(previousMode, state.settings.mode);
      if (state.settings.mode === 'galaxy') {
        if (previousMode !== 'galaxy' && state.sizeBy !== 'mass') legacySizeBy = state.sizeBy;
        state.sizeBy = 'mass';
      } else if (previousMode === 'galaxy' && state.sizeBy === 'mass') {
        state.sizeBy = legacySizeBy;
      }
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
    api.setColorBy = name => {
      state.colorBy = name;
      clearMaterialCache();
      refreshColors();
      render(false, false);
    };
    api.setPalette = name => {
      state.palette = typeof name === 'string' ? name : 'theme';
      state.overrides = Object.create(null);
      if (hasOwn(PALETTES, state.palette)) Object.assign(state.overrides, PALETTES[state.palette]);
      clearMaterialCache();
      refreshColors();
    };
    api.setTypeColor = (type, color) => {
      if (type == null || typeof color !== 'string') return;
      state.overrides[String(type)] = color;
      state.palette = 'custom';
      clearMaterialCache();
      refreshColors();
    };
    /* Rehydrating saved overrides is not a user edit, so it must not flip the palette
       selector to "custom" behind the user's back the way setTypeColor deliberately does. */
    api.setTypeColors = map => {
      const next = map && typeof map === 'object' ? map : {};
      Object.keys(next).forEach(type => {
        if (typeof next[type] === 'string') state.overrides[type] = next[type];
      });
      clearMaterialCache();
      refreshColors();
    };
    /* The active theme's resolved `--entity-*` values. Replaced wholesale rather than merged:
       a theme switch must not leave the previous theme's colour for a type the new one omits. */
    api.setThemeColors = map => {
      const next = Object.create(null);
      if (map && typeof map === 'object') {
        Object.keys(map).forEach(key => {
          if (typeof map[key] === 'string') next[key] = map[key];
        });
      }
      state.themeColors = next;
      clearMaterialCache();
      refreshColors();
    };
    /* One render for a whole batch of setters — see `batch`. */
    api.apply = (fn, fit, reheat) => { batch(typeof fn === 'function' ? fn : () => {}, fit, reheat); };
    api.setHighlight = id => {
      hilite = id == null ? null : id;
      hoverSet = id == null ? null : new Set([id].concat(adj[id] || []));
      invalidate();
    };
    api.setScope = patch => {
      if (!patch || typeof patch !== 'object') return;
      Object.assign(state, patch);
      if (!state.layers || typeof state.layers !== 'object') state.layers = {};
      render(false, true);
    };
    api.setLayers = layers => {
      state.layers = layers && typeof layers === 'object' ? { ...layers } : {};
      render(false, false);
    };
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
        meta: { ...raw.meta },
        communities: raw.communities.map(community => ({ ...community })),
        community_bridges: raw.community_bridges.map(bridge => ({ ...bridge })),
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
    api.physicsDiagnostics = () => physicsDiagnostics();
    api.reheat = () => {
      if (destroyed || state.settings.frozen || staticFullLayout) return;
      cancelAutoFit();
      raw.nodes.forEach(n => { n.fx = undefined; n.fy = undefined; });
      if (state.settings.mode === 'galaxy') {
        /* Persistent physics has no cold alpha to restart. Wake its ordinary fixed clock while
           preserving phase and velocity; never inject bonus slices that fast-forward all orbits. */
        galaxyReheatStepsRemaining = Math.max(galaxyReheatStepsRemaining,
          large ? GALAXY_REHEAT_LARGE_STEPS : GALAXY_REHEAT_STEPS);
        galaxyReheatActivations++;
        scheduleGalaxyDynamics(true);
        return;
      }
      prepareReheat();
      if (fg.d3AlphaDecay) fg.d3AlphaDecay(alphaDecay());
      softReheat();
    };
    api.freeze = on => {
      state.settings.frozen = on === true;
      if (state.settings.mode === 'galaxy') {
        if (state.settings.frozen) {
          galaxyReheatStepsRemaining = 0;
          cancelGalaxyDynamics(true);
          setSimulationBudget(false, true);
          render(false, false);
          return;
        }
        if (staticFullLayout) return;
        raw.nodes.forEach(n => { n.fx = undefined; n.fy = undefined; });
        render(false, false);
        scheduleGalaxyDynamics(true);
        return;
      }
      if (state.settings.frozen) {
        const charge = fg.d3Force('charge');
        if (charge && charge.strength) charge.strength(0);
        setSimulationBudget(true);
        fg.d3AlphaDecay(1);
        return;
      }
      // Dragging pins a node with fx/fy. Unfreezing is a request to resume the layout, not
      // merely the unpinned subset, so release those anchors before the simulation reheats.
      if (staticFullLayout) return;
      raw.nodes.forEach(n => { n.fx = undefined; n.fy = undefined; });
      applyForces();
      prepareReheat();
      setSimulationBudget(true);
      // A frozen render removes relation-flow particles. Reapply the live paint settings
      // before reheating so the enabled flow switch immediately becomes visible again.
      render(false, false);
      fg.d3AlphaDecay(alphaDecay());
      softReheat();
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
      const map = Object.create(null);
      raw.nodes.forEach(n => { map[n.id] = n.community || 0; });
      return map;
    };
    api.setGhosts = on => { state.ghost = on === true; render(false, false); };
    api.setRepoFilter = repo => {
      state.repo = typeof repo === 'string' ? repo.trim().toLowerCase() : '';
      render(false, true);
    };
    api.setAsOf = date => { state.asOf = asOfValue(date); render(false, true); };
    api.setSizeBy = metric => {
      if (state.settings.mode === 'galaxy') state.sizeBy = 'mass';
      else {
        state.sizeBy = metric === 'betweenness' ? metric : 'degree';
        legacySizeBy = state.sizeBy;
      }
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
      const collapseThreshold = state.settings.mode === 'communities' ? 0.22 : 0.42;
      const canAutoCollapse = autoCollapseEligible();
      const next = state.renderMode !== 'full' && (mode === true || (mode === 'auto' && canAutoCollapse && zoom < collapseThreshold));
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
      cancelGalaxyDynamics(true);
      if (fg.pauseAnimation) fg.pauseAnimation();
    };
    api.resume = () => {
      if (destroyed || running) return;
      running = true;
      if (fg.resumeAnimation) fg.resumeAnimation();
      measure();
      scheduleGalaxyDynamics(true);
    };
    api.destroyed = () => destroyed;
    api.destroy = () => {
      if (destroyed) return;
      destroyed = true;
      running = false;
      cancelGalaxyDynamics(true);
      clearTimeout(fitTimer);
      fitTimer = 0;
      clearTimeout(softAlphaTimer);
      softAlphaTimer = 0;
      cancelFrame(initialFitFrame);
      initialFitFrame = 0;
      cancelFrame(dragClickFrame);
      dragClickFrame = 0;
      cancelFrame(physicsFrame);
      physicsFrame = 0;
      physicsReheatPending = false;
      pendingRender = null;
      setActiveDragNode(null);
      try {
        if (detachVisibility) { detachVisibility(); detachVisibility = null; }
        if (detachManualDrag) { detachManualDrag(); detachManualDrag = null; }
        if (api._ro) { api._ro.disconnect(); api._ro = null; }
        // `_destructor` pauses the rAF and drops the graph data; it does not detach the
        // canvas, so clear the container too or a re-create leaves the old one attached.
        if (fg._destructor) fg._destructor();
        el.removeAttribute('data-graph-style');
        el.classList.remove('engraphis-graph-node-hover');
        el.innerHTML = '';
      } catch (e) { /* teardown is best-effort: never let it block a view change */ }
      raw = { nodes: [], links: [], suggestions: [], communities: [], community_bridges: [], meta: {} };
      galaxyServerPhase.clear();
      galaxySavedPhase.clear();
      adj = Object.create(null);
      liveAdj = Object.create(null);
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
    if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
      initialFitFrame = requestFrame(() => {
        initialFitFrame = 0;
        if (destroyed) return;
        measure();
        autoFit(reduced() ? 0 : 400, 40);
      });
    }
    if (typeof ResizeObserver !== 'undefined') {
      api._ro = new ResizeObserver(() => measure());
      api._ro.observe(el);
    }
    if (visibilityDocument && typeof visibilityDocument.addEventListener === 'function') {
      const handleVisibility = () => {
        if (pageHidden()) cancelGalaxyDynamics(true);
        else scheduleGalaxyDynamics(true);
      };
      visibilityDocument.addEventListener('visibilitychange', handleVisibility);
      detachVisibility = () => visibilityDocument.removeEventListener(
        'visibilitychange', handleVisibility
      );
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
      graphNodeRadius, evidenceNodeRadius, sanitizeEvidenceMetrics, fallbackGravityMass,
      radiusFromGravityMass, galaxyGravityConstant, galaxyGravityMaximum: GALAXY_GRAVITY_MAXIMUM,
      galaxyBlackHoleGravityConstant, galaxyLocalGravityConstant,
      galaxyStellarGravityConstant, galaxyFallbackStellarGravityConstant,
      galaxySystemGravityConstant, galaxyStellarGravitySetting,
      galaxyStellarGravityFloorSetting: GALAXY_STELLAR_GRAVITY_FLOOR_SETTING,
      defaultGalaxyStellarAccelerationCap, defaultGalaxySystemAccelerationCap,
      galaxySceneWithinLiveLimit,
      galaxyRelationOrbitScale,
      galaxyOrbitalSeparationPadding, galaxyOrbitalSeparationStrength,
      communityKey, communityCenters, ensureGalaxyPositions,
      seedGalaxyOrbits, seedGalaxySystemOrbits,
      applyGalaxyGravity, applyGalaxySystemHaloGravity, applyGalaxyEnclosedSystemGravity,
      applyGalaxySystemAnchorGravity, applyGalaxySystemAnchorExclusion,
      galaxySystemAnchorClearance,
      combineGalaxySystemAnchorExclusions,
      applyGalaxyCentralGravity, applyGalaxyMutualSystemGravity, galaxyGlobalAnchor,
      galaxyBlackHoleField, applyGalaxyBlackHoleGravity, recenterGalaxyOnAnchor,
      applyCommunityBridgeGravity,
      applyGalaxyRelationSprings, applyGalaxyRelationDistanceConstraints,
      applyDraggedNodeGravity, applyDraggedNodeAcceleration,
      applyGalaxyCollisions, applyGalaxyOrbitalSeparation,
      applyGalaxyBlackHoleExclusion,
      galaxyFarFieldEnvelope, applyGalaxyFarFieldGravity, applyGalaxyFarFieldConfinement,
      applyGalaxyAnnularBounds,
      stabilizeGalaxySystemVelocities,
      galaxyAccelerations, integrateGalaxyLeapfrog, galaxyMotionDiagnostics,
      galaxyInwardConvergencePerMinute, galaxyInwardConvergenceFactor,
      applyGalaxyInwardConvergence, galaxyImmediateGravityRadiusScale,
      galaxyLayoutCompactness,
      applyGalaxyGravitySettingResponse,
      galaxySpringStrength, galaxySpringDistance, galaxySafeSpringDistance,
      fallbackCommunityBridges, paintFlowArrow,
      nodeName, linkEndpoint, asOfValue, materialRecipe, materialTier,
      paintMaterialDirect, paintGalaxyAnchorAdornment,
      renderMaterialSample, sampleMaterialColour,
      materialCacheStats, clearMaterialCache, setMaterialCanvasFactory
    }
  };
})();
