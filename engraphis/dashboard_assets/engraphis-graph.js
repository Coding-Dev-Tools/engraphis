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
    galaxy: { label: 'Galaxy gravity', repel: 100, link: 8, gravity: 96, font: 12, size: 3, linkw: 0.72, labelDensity: 24, curve: 0.12, particles: 0 },
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
  const GALAXY_LIVE_NODE_LIMIT = 1500;
  const GALAXY_LIVE_LINK_LIMIT = 3000;
  function galaxySceneWithinLiveLimit(data) {
    const scene = data || {};
    return (scene.nodes || []).length <= GALAXY_LIVE_NODE_LIMIT
      && (scene.links || []).length <= GALAXY_LIVE_LINK_LIMIT;
  }
  const GALAXY_EXACT_LIMIT = 64;
  const GALAXY_BARNES_HUT_THETA = 0.85;
  const GALAXY_GRAVITY_MAXIMUM = 400;
  const GALAXY_GRAVITY_MAX_STRENGTH_GAIN = 1.5;
  const GALAXY_GRAVITY_STRENGTH_GAIN_START = 200;
  /* The emergency acceleration cap follows the full visible strength range. Direct callers can
     still pass pathological values, but those values clamp to the same 0..400 physics ceiling. */
  const GALAXY_GRAVITY_CAP_REFERENCE = GALAXY_GRAVITY_MAXIMUM;
  /* One response curve owns every physical layer. It retains the positive quadratic response
     and two C1 smooth boost stages. Local gravity is exactly 120 at the default. Unannotated
     compatibility graphs retain the raw zero endpoint; an explicit painted black hole applies
     the small orbital floor below so the dashboard's "loose" setting never stops the galaxy.
     Independent community stars apply their named minimum and faster clock afterward. */
  function galaxySmoothstep(value) {
    const raw = Number(value);
    const t = Number.isFinite(raw) ? Math.max(0, Math.min(1, raw)) : 0;
    return t * t * (3 - 2 * t);
  }
  /* Keep the established calibration through 200, then make the extended range tighten the
     field smoothly. Multiplying the normalized high-end span by 1.5 makes the stronger response
     arrive 50% sooner while the maximum remains capped at exactly 1.5x. */
  const GALAXY_GRAVITY_RESPONSE_RATE_MULTIPLIER = 1.5;
  function galaxyGravityStrengthMultiplier(setting) {
    const raw = Number(setting);
    const value = Number.isFinite(raw)
      ? Math.max(0, Math.min(GALAXY_GRAVITY_MAXIMUM, raw)) : 0;
    const span = Math.max(1, GALAXY_GRAVITY_MAXIMUM - GALAXY_GRAVITY_STRENGTH_GAIN_START);
    const normalized = (value - GALAXY_GRAVITY_STRENGTH_GAIN_START) / span
      * GALAXY_GRAVITY_RESPONSE_RATE_MULTIPLIER;
    return 1 + (GALAXY_GRAVITY_MAX_STRENGTH_GAIN - 1) * galaxySmoothstep(normalized);
  }
  function galaxyGravityConstant(setting) {
    const raw = Number(setting);
    const value = Number.isFinite(raw) ? Math.max(0, Math.min(GALAXY_GRAVITY_MAXIMUM, raw)) : 0;
    const base = value * (772 + 11 * value) / 2600;
    const boost = 1 + 0.25 * galaxySmoothstep(value / 48)
      + 0.25 * galaxySmoothstep((value - 48) / 52);
    /* Gravity was tuned against the v8-era compact layout, where a 48 setting produced
       comfortable orbital spacing. The galaxy-v12 compact-orbits algorithm places systems
       tighter, so the same setting now reads as too loose. Scale the final constant 20%
       upward so the default (and every other position) feels like the reference layout. */
    return base * boost * 4 * galaxyGravityStrengthMultiplier(value) * 2.0;
  }
  /* Gravity strength is the galaxy-wide black-hole control. The dashboard's Gravity slider
     flows to the explicit global anchor: zero user gravity is a real zero field, and the
     loose ↔ tight endpoints map to distinct central accelerations. Stability for community
     systems is owned by the independent local-stellar well and the rigid event-horizon
     contact layers, neither of which depends on this constant. */
  const GALAXY_GLOBAL_GRAVITY_FLOOR_SETTING = 24;
  function galaxyBlackHoleGravitySetting(setting, explicitGlobal) {
    const raw = Number(setting);
    const value = Number.isFinite(raw) ? Math.max(0, Math.min(GALAXY_GRAVITY_MAXIMUM, raw)) : 0;
    return value;
  }
  function galaxyBlackHoleGravityConstant(setting, explicitGlobal) {
    return galaxyGravityConstant(galaxyBlackHoleGravitySetting(setting, explicitGlobal)) * 2;
  }
  function galaxyLocalGravityConstant(setting) {
    const raw = Number(setting);
    const value = Number.isFinite(raw) ? Math.max(0, Math.min(GALAXY_GRAVITY_MAXIMUM, raw)) : 0;
    /* Routes through galaxyBlackHoleGravityConstant to preserve the canonical
       2x black-hole-to-local scaling; pre-fix code relied on the alias chain
       galaxyLocalGravityConstant = galaxyBlackHoleGravityConstant * 0.5. */
    return galaxyBlackHoleGravityConstant(value) * 0.5;
  }
  /* A fit-to-view galaxy compresses stellar and galactic distances onto one canvas, so using
     one physical clock made a valid planet orbit visually disappear under its system's
     black-hole sweep. Give independent community stars a 3.25x angular clock by multiplying
     their gravitational parameter by clock^2. Both the circular seed and every live
     inverse-square sample consume this same constant: the result is a faster bound central
     orbit, not a per-frame carousel or an unbalanced tangential kick. Direct global children
     use the black-hole clock because their carrier seed and live well are the same field. */
  const GALAXY_STELLAR_ORBIT_CLOCK = 3.25;
  const GALAXY_FALLBACK_STELLAR_ORBIT_CLOCK = 2.5;
  /* The dashboard's Gravity control owns the black-hole well, and the local stellar setting
     flows 1:1 from the slider. All callers pass a finite slider value (or an explicit per-star
     override), so every position 0..200 produces a distinct local well and distinct carrier
     geometry. Authored system stability is owned by the orbital-radius floor and the rigid
     event-horizon contact layers, neither of which depends on this constant.

     The Every-node integration path uses a fixed local setting independent of the slider so
     that mode behaves as a calibrated reference, not as a slider follower. */
  const GALAXY_FIXED_LOCAL_GRAVITY_SETTING = 48;
  function galaxyStellarGravitySetting(setting) {
    const raw = Number(setting);
    return Number.isFinite(raw)
      ? Math.max(0, Math.min(GALAXY_GRAVITY_MAXIMUM, raw)) : 0;
  }
  function galaxyStellarGravityConstant(setting) {
    return galaxyLocalGravityConstant(galaxyStellarGravitySetting(setting))
      * GALAXY_STELLAR_ORBIT_CLOCK * GALAXY_STELLAR_ORBIT_CLOCK;
  }
  function galaxyFallbackStellarGravityConstant(setting) {
    return galaxyLocalGravityConstant(setting)
      * GALAXY_FALLBACK_STELLAR_ORBIT_CLOCK * GALAXY_FALLBACK_STELLAR_ORBIT_CLOCK;
  }
  function galaxyLegacyCommunityGravityConstant(setting) {
    return galaxyLocalGravityConstant(galaxyStellarGravitySetting(setting))
      * GALAXY_FALLBACK_STELLAR_ORBIT_CLOCK * GALAXY_FALLBACK_STELLAR_ORBIT_CLOCK;
  }
  function galaxyLocalGravitySetting(setting, localSetting) {
    return localSetting === undefined ? setting : localSetting;
  }
  function galaxyHasAuthoredParent(node, parent) {
    return !!(node && parent && node.system_anchor_id !== undefined
      && node.system_anchor_id !== null && String(node.system_anchor_id) !== ''
      && String(node.system_anchor_id) === String(parent.id));
  }
  function orderedGalaxyLocalOrbitMembers(members, carrier, byId) {
    const lookup = byId || new Map((members || []).map(item => [String(item.id), item]));
    const depths = new Map();
    const visiting = new Set();
    const depthOf = node => {
      if (!node || node === carrier) return 0;
      if (depths.has(node)) return depths.get(node);
      if (visiting.has(node)) return 1;
      visiting.add(node);
      const parent = galaxyLocalOrbitParent(node, members, carrier, lookup);
      const depth = parent && parent !== node ? depthOf(parent) + 1 : 1;
      visiting.delete(node);
      depths.set(node, depth);
      return depth;
    };
    return (members || []).slice().sort((left, right) => depthOf(left) - depthOf(right)
      || String(left.id).localeCompare(String(right.id)));
  }
  function galaxySystemGravityConstant(anchor, setting, localSetting, authoredHierarchy) {
    const effectiveLocalSetting = galaxyLocalGravitySetting(setting, localSetting);
    if (anchor && anchor.anchor_role === 'global') {
      return galaxyBlackHoleGravityConstant(setting, true);
    }
    if (authoredHierarchy !== false) {
      return galaxyStellarGravityConstant(effectiveLocalSetting);
    }
    return anchor && anchor.anchor_role === 'community'
      ? galaxyLegacyCommunityGravityConstant(effectiveLocalSetting)
      : galaxyFallbackStellarGravityConstant(effectiveLocalSetting);
  }
  function defaultGalaxyStellarAccelerationCap(gravity) {
    /* The local stellar clock is a uniform simulation-time transform: G scales by clock^2,
       therefore its safety acceleration ceiling must scale by the same factor. Leaving this
       cap on the unclocked value made close planets sub-circular even though their seed and
       live force sampled the clocked gravitational parameter. */
    return defaultGalaxyAccelerationCap(galaxyStellarGravitySetting(gravity))
      * GALAXY_STELLAR_ORBIT_CLOCK * GALAXY_STELLAR_ORBIT_CLOCK;
  }
  function defaultGalaxySystemAccelerationCap(anchor, gravity, localSetting,
    authoredHierarchy) {
    const effectiveLocalSetting = galaxyLocalGravitySetting(gravity, localSetting);
    if (anchor && anchor.anchor_role === 'global') {
      return GALAXY_CENTER_ACCELERATION_CAP
        * galaxyBlackHoleGravityConstant(gravity, true) / 24;
    }
    if (authoredHierarchy !== false) {
      return defaultGalaxyStellarAccelerationCap(effectiveLocalSetting);
    }
    const fallbackSetting = anchor && anchor.anchor_role === 'community'
      ? galaxyStellarGravitySetting(effectiveLocalSetting) : effectiveLocalSetting;
    return defaultGalaxyAccelerationCap(fallbackSetting)
      * GALAXY_FALLBACK_STELLAR_ORBIT_CLOCK * GALAXY_FALLBACK_STELLAR_ORBIT_CLOCK;
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
  function defaultGalaxyBlackHoleAccelerationCap(gravity, explicitGlobal) {
    const reference = galaxyAccelerationCapReference(gravity);
    return GALAXY_CENTER_ACCELERATION_CAP
      * galaxyBlackHoleGravityConstant(reference, explicitGlobal) / 24;
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
  /* Stellar gravity owns motion inside a solar system, but a numerical or relation impulse
     must never be allowed to reclassify a planet as free galaxy debris.  The immutable orbit
     seed is the system boundary; 8% leaves room for the intended eccentric phase and the
     orbital-speed radius control without allowing a member to escape its painted system. */
  const GALAXY_LOCAL_ORBIT_BOUNDARY_SLACK = 1.08;
  /* Preserve headroom below the 48-unit emergency guard while allowing real overview systems
     whose physically sampled circular speed exceeds the retired 10-unit presentation cap to
     visibly orbit the black hole. */
  const GALAXY_SYSTEM_ORBIT_SEED_SPEED_LIMIT = 18;
  /* Carrier support follows the same circular-speed law as the galactic field. Presentation
     speed is controlled only by the explicit orbital-speed clock; no hidden visual boost is
     allowed to make a carrier super-circular relative to the acceleration that governs it. */
  const GALAXY_CARRIER_FRAME_SPEED_LIMIT = GALAXY_SYSTEM_ORBIT_SEED_SPEED_LIMIT;
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
  /* `graph-repel` remains the persisted key for saved-view compatibility. In Galaxy, 100 is
     the natural orbital rate; the high end is deliberately gentler than the old 3.4x response
     so the control separates systems without injecting escape energy. Radius growth remains
     independently bounded. */
  const GALAXY_ORBITAL_SPEED_DEFAULT = 100;
  const GALAXY_ORBITAL_SPEED_MAXIMUM_SETTING = 400;
  const GALAXY_ORBITAL_SPEED_MINIMUM = 0.25;
  const GALAXY_ORBITAL_SPEED_RESPONSE_GAIN = 0.5;
  const GALAXY_ORBITAL_SPEED_MAXIMUM = 4.6;
  const GALAXY_ORBITAL_RADIUS_MAXIMUM = 1.24;
  function galaxyOrbitalSpeedMultiplier(setting) {
    const raw = Number(setting);
    const value = Number.isFinite(raw)
      ? Math.max(0, Math.min(GALAXY_ORBITAL_SPEED_MAXIMUM_SETTING, raw))
      : GALAXY_ORBITAL_SPEED_DEFAULT;
    const multiplier = value <= GALAXY_ORBITAL_SPEED_DEFAULT
      ? value / GALAXY_ORBITAL_SPEED_DEFAULT
      : 1 + (value - GALAXY_ORBITAL_SPEED_DEFAULT)
        / GALAXY_ORBITAL_SPEED_DEFAULT * GALAXY_ORBITAL_SPEED_RESPONSE_GAIN;
    return Math.max(GALAXY_ORBITAL_SPEED_MINIMUM,
      Math.min(GALAXY_ORBITAL_SPEED_MAXIMUM, multiplier));
  }
  function galaxyOrbitalRadiusMultiplier(setting) {
    const raw = Number(setting);
    const value = Number.isFinite(raw)
      ? Math.max(0, Math.min(GALAXY_ORBITAL_SPEED_MAXIMUM_SETTING, raw))
      : GALAXY_ORBITAL_SPEED_DEFAULT;
    if (value <= GALAXY_ORBITAL_SPEED_DEFAULT) return 1;
    return 1 + (GALAXY_ORBITAL_RADIUS_MAXIMUM - 1)
      * (value - GALAXY_ORBITAL_SPEED_DEFAULT)
      / (GALAXY_ORBITAL_SPEED_MAXIMUM_SETTING - GALAXY_ORBITAL_SPEED_DEFAULT);
  }
  const GALAXY_ORBITAL_SEPARATION_BASE_SETTING = 60;
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
  /* Legacy telemetry retains this padding name, but cross-system clearance now belongs to the
     complete rigid envelope below—not arbitrary node-pair pressure. */
  const GALAXY_CROSS_SYSTEM_REPULSION_PADDING = 1.5;
  /* Solar systems are packed by their complete painted envelopes, never by pushing arbitrary
     cross-community node pairs.  Eight world units stays visible between two outer planets;
     the bounded response lets live systems keep orbiting while their carrier frames separate. */
  /* Keep the default admission gap equal to the visible envelope contract. Complete solar
     systems must retain eight graph units of screen-space clearance after fit-to-view. */
  const GALAXY_SYSTEM_PACKING_GAP = 8;
  const GALAXY_SYSTEM_PACKING_STRENGTH = 0.45;
  const GALAXY_SYSTEM_PACKING_MAX_CORRECTION = 6;
  /* The orbital-speed control can expand local radii by at most 6%. Keep a small additional
     margin, but do not reserve the old 12% by default because that needlessly adds outer rings. */
  const GALAXY_CARRIER_LANE_SLACK = 1.0384;
  /* Tiny solver drift should keep the deterministic lane phase shared across a ring. A larger
     displacement is an actual contact/boundary correction and is allowed to become phase. */
  const GALAXY_LANE_PHASE_CORRECTION_DISTANCE = 0.5;
  const GALAXY_BRIDGE_SCALE = 0.35;
  const GALAXY_CENTER_ACCELERATION_CAP = 2.5;
  /* The visible black hole is a contact boundary as well as a gravity source. Its skin must
     exceed one emergency-speed drift (48 * 0.032 = 1.536 world units), so a body cannot
     tunnel through the painted edge between fixed steps. The constraint never adds an outward
     kick; deep corrections preserve angular momentum instead of manufacturing orbital speed. */
  const GALAXY_BLACK_HOLE_EXCLUSION_PADDING = 2.5;
  /* The cored-logarithmic halo keeps ordinary systems bound, but a finite visual galaxy also needs a
     dormant outer safety field. It starts well outside the seeded scene, adds a smooth
     inward acceleration only near that edge, then applies an exact last-resort boundary if a
     body still escapes. The cached radius never follows an escaped body outward. */
  /* The finite disk must reserve painted-envelope capacity, not merely the furthest seeded
     carrier. The 2x bound clears the complete 542-node / 36-system overview while explicit
     caller radii remain exact for embedded and boundary-test scenes. */
  const GALAXY_FAR_FIELD_ENVELOPE_SCALE = 2;
  const GALAXY_FAR_FIELD_MIN_RADIUS = 96;
  const GALAXY_FAR_FIELD_SOFT_FRACTION = 0.82;
  const GALAXY_FAR_FIELD_ACCELERATION = 12;
  const GALAXY_FAR_FIELD_MAX_ACCELERATION = 16;
  /* Frozen compatibility nodes swallow Object.defineProperty, so the far-field cache also
     lives in a WeakMap keyed by anchor identity. The property-based path stays for ordinary
     mutable nodes; the WeakMap wins when the anchor is frozen. */
  const galaxyFarFieldEnvelopeCache = typeof WeakMap === 'function' ? new WeakMap() : null;
  const galaxyBlackHoleSpinCache = typeof WeakMap === 'function' ? new WeakMap() : null;
  /* Galaxy has its own physical clock. Thirty fixed steps per second bounds main-thread work,
     while a 0.032 leapfrog slice makes both levels of the hierarchy visibly rotate without
     changing their circular initial conditions or force balance. This is a time-scale increase,
     not an extra tangential kick: planets still orbit only their dominant star and whole systems
     still orbit the black hole. Damping removes numerical noise over minutes rather than erasing
     the seeded angular momentum during the opening animation. */
  const GALAXY_FRAME_INTERVAL_MS = 1000 / 30;
  const GALAXY_MOTION_RATE = 0.68;
  const GALAXY_FIXED_TIMESTEP = 0.032;
  /* The black hole remains the chart's fixed origin, but its visible accretion disk must not
     read as a frozen node when the central community has no separately painted satellites. */
  const GALAXY_BLACK_HOLE_SPIN_RATE = 1.2;
  const GALAXY_MAX_SUBSTEPS = 3;
  /* Galaxy's fixed-step solver is persistent, so it has no cold alpha to reheat. Extra fixed
     slices would literally fast-forward physical time (up to 3x at a 60 Hz render cadence),
     making every system lurch despite adding no random impulse. Keep the public action and its
     activation telemetry, but let it only wake/reset the ordinary clock; no bonus time enters
     the integrator. */
  const GALAXY_REHEAT_STEPS = 0;
  const GALAXY_REHEAT_LARGE_STEPS = 0;
  const GALAXY_VELOCITY_DECAY = 0.00005;
  /* Developer-facing spacetime controls are normalized multipliers around the calibrated
     dashboard physics. Keeping them separate from the established Gravity/Link controls makes
     the advanced panel reversible and avoids changing saved-layout semantics. */
  const GALAXY_GRAVITATIONAL_CONSTANT_MULTIPLIER = 1;
  const GALAXY_LOCAL_GRAVITATIONAL_CONSTANT_MULTIPLIER = 1;
  const GALAXY_BLACK_HOLE_MASS_MULTIPLIER = 1;
  const GALAXY_SPRING_STIFFNESS_MULTIPLIER = 1;
  const GALAXY_FRAME_DRAGGING_FRACTION = 0.018;
  const GALAXY_FRAME_DRAGGING_MAX_ACCELERATION = 0.22;
  const GALAXY_EVENT_HORIZON_INFLUENCE_SCALE = 4.5;
  /* The black-hole node is intentionally painted much larger than ordinary evidence. Letting
     that display radius scale the complete weak-field band made most of a fitted galaxy look
     near-horizon. This finite chart-space thickness keeps curvature local to the event horizon
     while the scale still controls smaller/custom black holes. */
  const GALAXY_EVENT_HORIZON_BAND_LIMIT = 24;
  /* Visual emphasis must not leak into collision, packing, or event-horizon geometry. */
  const GALAXY_BLACK_HOLE_PAINT_SCALE = 2;
  const GALAXY_EVENT_HORIZON_DECAY_RATE = 0.005;
  const GALAXY_EVENT_HORIZON_INWARD_ACCELERATION = 0.28;
  const GALAXY_TIDAL_STRENGTH_FRACTION = 0.18;
  const GALAXY_TIDAL_ACCELERATION_CAP = 0.16;
  const GALAXY_SLINGSHOT_VELOCITY_SCALE = 0.022;
  const GALAXY_SLINGSHOT_SPEED_LIMIT = 24;
  const GALAXY_SLINGSHOT_CAPTURE_RADIUS = 120;
  const GALAXY_SLINGSHOT_ESCAPE_FACTOR = 1.08;
  function galaxyPhysicsMultiplier(value, fallback, maximum) {
    const raw = Number(value);
    return Number.isFinite(raw)
      ? Math.max(0, Math.min(maximum, raw)) : fallback;
  }
  function galaxyLocalGravityMultiplier(anchor, options) {
    const opts = options || {};
    const value = anchor && anchor.anchor_role === 'global'
      ? opts.gravitationalConstant
      : opts.localGravitationalConstant;
    return galaxyPhysicsMultiplier(value,
      GALAXY_LOCAL_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8);
  }
  function galaxyEventHorizonOuterRadius(anchorRadius, contactRadius, influenceScale) {
    const scale = Math.max(1.1, Number(influenceScale) || GALAXY_EVENT_HORIZON_INFLUENCE_SCALE);
    const thickness = Math.max(1, Math.min(GALAXY_EVENT_HORIZON_BAND_LIMIT,
      Math.max(0, Number(anchorRadius) || 0) * (scale - 1)));
    return Math.max(Number(contactRadius) + 1, Number(contactRadius) + thickness);
  }
  /* This is a deliberate external field in the black-hole frame, rather than an
     equal-and-opposite pair force: it makes the visible galaxy contract at a reliable
     wall-clock rate even while orbital forces and drag-derived energy vary. One minute at
     the previous default left 75% of a radius. The motion-rate exponent below now advances
     that same physical trajectory at 68% speed, matching the faster leapfrog clock without
     weakening the force field itself. */
  const GALAXY_INWARD_CONVERGENCE_PER_MINUTE = 0;
  const GALAXY_INWARD_CONVERGENCE_SECONDS = 60;
  const GALAXY_OUTWARD_OVERRIDE = 0.10;

  /* Density follows the same effective-G curve as orbital acceleration. Gravity 0 keeps
     the seeded loose radius (while still rejecting outward escape), the default follows
     the former 25%/minute trajectory at 68% speed, and the former 100-setting response
     remains 3.6x while the extended range adds the stronger high-end response. */
  function galaxyInwardConvergencePerMinute(gravitySetting) {
    const setting = gravitySetting === undefined ? 48 : gravitySetting;
    /* The convergence helper is an optional density response, not the orbital well. Normalize
       against the calibrated reference setting (48) so the ratio stays monotonic across the
       full 0..200 slider span; the rigid event-horizon contact keeps loose-end bodies bound. */
    const relativeGravity = galaxyBlackHoleGravityConstant(setting, false)
      / galaxyBlackHoleGravityConstant(48, true);
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
    const maximum = Math.max(1e-9,
      galaxyBlackHoleGravityConstant(GALAXY_GRAVITY_MAXIMUM, true));
    const normalized = Math.max(0, Math.min(1,
      galaxyBlackHoleGravityConstant(setting, true) / maximum));
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
    const anchor = galaxyGlobalAnchor(nodes);
    const empty = {
      systems: 0, moved: 0, ratio: 1, maximumShift: 0,
      velocityAdjusted: 0, maximumVelocityShift: 0,
      anchorId: anchor ? anchor.id : null,
    };
    if (!anchor || anchor.anchor_role !== 'global') return empty;
    const previous = Number(previousSetting);
    const next = Number(nextSetting);
    if (!Number.isFinite(next) || !Number.isFinite(previous)
      || Math.abs(next - previous) <= 1e-12) return empty;
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const field = galaxyBlackHoleField(bodies, Object.assign({}, opts, { gravity: next }));
    if (!field.anchor || field.anchor.anchor_role !== 'global') return empty;
    const direction = (seededHash(opts.layoutSeed, 'galaxy-spin') & 1) ? 1 : -1;
    const anchorVx = Number.isFinite(anchor.vx) ? anchor.vx : 0;
    const anchorVy = Number.isFinite(anchor.vy) ? anchor.vy : 0;
    const fixedNodeId = opts.fixedNodeId === undefined || opts.fixedNodeId === null
      ? null : String(opts.fixedNodeId);
    const previousField = galaxyBlackHoleField(bodies, Object.assign({}, opts, {
      gravity: previous,
    }));
    let systems = 0, velocityAdjusted = 0, maximumVelocityShift = 0;
    let oldSpeedTotal = 0, newSpeedTotal = 0, speedSamples = 0;
    field.systems.forEach(item => {
      if (!item.carrier || item.nodes.includes(anchor)
        || item.nodes.some(node => fixedNodeId !== null && String(node.id) === fixedNodeId)) return;
      const dx = item.carrier.x - anchor.x, dy = item.carrier.y - anchor.y;
      const radius = Math.hypot(dx, dy);
      if (!(radius > 1e-9)) return;
      const currentVx = (Number.isFinite(item.carrier.vx) ? item.carrier.vx : 0) - anchorVx;
      const currentVy = (Number.isFinite(item.carrier.vy) ? item.carrier.vy : 0) - anchorVy;
      const angular = dx * currentVy - dy * currentVx;
      const orbitDirection = Math.abs(angular) > 1e-9 ? Math.sign(angular) : direction;
      const unitX = dx / radius, unitY = dy / radius;
      const tangentX = -unitY * orbitDirection, tangentY = unitX * orbitDirection;
      const targetSpeed = galaxyCarrierTargetSpeed(field, radius, opts.orbitalSpeed);
      const oldItem = previousField.systems.find(candidate => candidate.id === item.id);
      const oldSpeed = oldItem ? galaxyCarrierTargetSpeed(previousField, radius,
        opts.orbitalSpeed) : targetSpeed;
      if (!(targetSpeed > 0)) return;
      const targetVx = anchorVx + tangentX * targetSpeed;
      const targetVy = anchorVy + tangentY * targetSpeed;
      const deltaVx = targetVx - (Number.isFinite(item.carrier.vx) ? item.carrier.vx : 0);
      const deltaVy = targetVy - (Number.isFinite(item.carrier.vy) ? item.carrier.vy : 0);
      item.nodes.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + deltaVx;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + deltaVy;
        setGalaxySystemOrbitSpeed(node, galaxyOrbitalSpeedMultiplier(opts.orbitalSpeed));
      });
      systems++;
      velocityAdjusted += item.nodes.length;
      maximumVelocityShift = Math.max(maximumVelocityShift, Math.hypot(deltaVx, deltaVy));
      oldSpeedTotal += oldSpeed;
      newSpeedTotal += targetSpeed;
      speedSamples++;
    });
    return {
      systems,
      /* Keep positions authoritative: a slider change changes the next circular velocity,
         while the existing phase and complete local solar-system geometry remain intact. */
      moved: systems,
      ratio: oldSpeedTotal > 1e-9 && speedSamples > 0
        ? (newSpeedTotal / speedSamples) / (oldSpeedTotal / speedSamples) : 1,
      maximumShift: 0,
      velocityAdjusted,
      maximumVelocityShift,
      anchorId: anchor.id,
    };
  }

  /* `zoomToFit()` derives its bounds from force-graph's default node geometry rather than
     our custom canvas radius. A compact, nearly-linear graph can therefore produce a 10×+
     fit zoom even though its rendered nodes already fill the canvas. At that scale a normal
     drag maps to a tiny world-space movement and reheating makes the rest of the layout look
     like it is racing away. Keep auto-fit useful without letting its scale become unstable. */
  const MAX_AUTO_FIT_ZOOM = 4;
  const SETTINGS_ALPHA_TARGET = 0.12;
  const ALPHA_TARGET_HOLD_MS = 180;
  /* Inline utility: bound a value to [min, max]. The dashboard pipeline does not expose
     a shared math helper, so this lives here alongside the spacetime tuners that need it. */
  function clamp(value, min, max) {
    const n = Number(value);
    if (!Number.isFinite(n)) return min;
    return Math.max(min, Math.min(max, n));
  }
  /* Mirror of graphBlackHoleMassMultiplier in ledger.js — kept inline so the d3-force
     d3-install path in this file does not need to cross reference the ledger module. The
     formula is identical: baseline 160 below which the multiplier is value/160, above which
     it climbs linearly at 0.02/unit (so 500 -> 8.80, 1000 -> 21.80). */
  const GRAPH_BLACK_HOLE_MASS_BASELINE = 160;
  function blackHoleMassMultiplier(controlValue) {
    const value = Number(controlValue);
    if (!Number.isFinite(value)) return 1;
    return value <= GRAPH_BLACK_HOLE_MASS_BASELINE
      ? Math.max(0, value / GRAPH_BLACK_HOLE_MASS_BASELINE)
      : 1 + (value - GRAPH_BLACK_HOLE_MASS_BASELINE) * 0.02;
  }

  /* Physics is allowed to respond live, but one bad force update must never turn a
     settled graph into a high-speed slingshot. Keep the bounds in world units so they
     remain meaningful at every camera zoom. */
  const MIN_NODE_SPEED = 8;
  const MAX_NODE_SPEED = 48;
  function galaxyRelativeSpeedBudget(parent, absoluteLimit, requested, directionX, directionY) {
    const limit = Math.max(0.01, Number(absoluteLimit) || MAX_NODE_SPEED);
    const requestedSpeed = Math.max(0, Number(requested) || 0);
    const parentVx = parent && Number.isFinite(parent.vx) ? parent.vx : 0;
    const parentVy = parent && Number.isFinite(parent.vy) ? parent.vy : 0;
    const directionLength = Math.hypot(Number(directionX) || 0, Number(directionY) || 0);
    if (!(directionLength > 1e-9)) {
      return Math.max(0, Math.min(requestedSpeed,
        limit - Math.hypot(parentVx, parentVy)));
    }
    const unitX = directionX / directionLength;
    const unitY = directionY / directionLength;
    const projection = parentVx * unitX + parentVy * unitY;
    /* Solve |parentVelocity + unitTangent * relativeSpeed| <= limit for the largest
       non-negative relativeSpeed. This preserves a perpendicular local orbit even when
       the carrier is already close to the absolute speed ceiling. */
    const discriminant = projection * projection + limit * limit
      - parentVx * parentVx - parentVy * parentVy;
    const maximum = -projection + Math.sqrt(Math.max(0, discriminant));
    return Math.max(0, Math.min(requestedSpeed, maximum));
  }

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
  const BASE_NODE_RADIUS_SCALE = 1.2;
  function radiusFromGravityMass(mass) {
    return BASE_NODE_RADIUS_SCALE
      * (1.5 + 2 * Math.pow(finitePositive(mass, 1, 1000), 2 / 3));
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
    return evidenceRadius;
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
  /* ``system_anchor_id`` is the hierarchy contract for authored Galaxy scenes. Community
     fallback remains only for unannotated compatibility scenes; relation edges never promote
     a node into the black-hole frame. */
  function galaxyOrbitGroups(nodes) {
    const values = Array.isArray(nodes) ? nodes : [];
    const groups = new Map();
    const communityAnchors = new Map();
    const globalAnchor = values.find(node => node && !node.ghost
      && node.anchor_role === 'global');
    const globalId = globalAnchor ? String(globalAnchor.id) : '';
    const byId = new Map(values.filter(node => node && node.id !== undefined)
      .map(node => [String(node.id), node]));
    values.forEach(node => {
      if (!node || node.ghost
        || (node.anchor_role !== 'global' && node.anchor_role !== 'community')) return;
      const key = communityKey(node);
      const existing = communityAnchors.get(key);
      if (!existing || node.anchor_role === 'global') communityAnchors.set(key, node);
    });
    values.forEach(node => {
      if (!node || node.ghost || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      let root = node;
      let current = node;
      const visited = new Set([String(node.id)]);
      while (current && current.system_anchor_id !== undefined
        && current.system_anchor_id !== null) {
        const parentId = String(current.system_anchor_id);
        if (!parentId || parentId === String(current.id) || visited.has(parentId)) break;
        if (parentId === globalId) {
          root = globalAnchor;
          break;
        }
        const parent = byId.get(parentId);
        if (!parent) break;
        visited.add(parentId);
        root = parent;
        current = parent;
      }
      const hasExplicitParent = node.system_anchor_id !== undefined
        && node.system_anchor_id !== null && String(node.system_anchor_id) !== '';
      if (!hasExplicitParent && root === node) {
        const declared = communityAnchors.get(communityKey(node));
        /* A local community anchor is a safe compatibility parent. The global anchor is not:
           sharing its display community must never imply black-hole ancestry. */
        if (declared && declared !== node && declared.anchor_role === 'community') root = declared;
      }
      let rootId = String(root.id);
      const rootParentId = root.system_anchor_id === undefined
        || root.system_anchor_id === null ? '' : String(root.system_anchor_id);
      if (globalAnchor && (root === globalAnchor || rootParentId === globalId)) {
        rootId = globalId;
      } else if (root === node && !hasExplicitParent
        && node.anchor_role !== 'global' && node.anchor_role !== 'community') {
        rootId = communityKey(node);
      }
      const mass = finitePositive(node.gravity_mass, 1, 1000);
      let group = groups.get(rootId);
      if (!group) {
        group = { id: rootId, mass: 0, x: 0, y: 0, nodes: [] };
        groups.set(rootId, group);
      }
      group.mass += mass;
      group.x += node.x * mass;
      group.y += node.y * mass;
      group.nodes.push(node);
    });
    groups.forEach(group => {
      if (group.mass > 0) {
        group.x /= group.mass;
        group.y /= group.mass;
      }
    });
    return groups;
  }
  function galaxySystemAnchor(members) {
    const global = (members || []).find(node => node && !node.ghost
      && node.anchor_role === 'global');
    if (global) return global;
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
  /* Resolve one local orbital parent for every member. Explicit ancestry wins when the parent
     is present in this carrier group; filtered/legacy payloads fall back to the system star.
     The global black hole is a valid parent for direct core satellites. */
  function galaxyLocalOrbitParent(node, members, carrier, byId) {
    if (!node || node === carrier) return null;
    const lookup = byId || new Map((members || []).map(item => [String(item.id), item]));
    const declaredId = node.system_anchor_id === undefined || node.system_anchor_id === null
      ? '' : String(node.system_anchor_id);
    const declared = declaredId ? lookup.get(declaredId) : null;
    if (declared && declared !== node) return declared;
    let communityAnchors = lookup.__galaxyCommunityAnchors;
    if (!communityAnchors) {
      communityAnchors = new Map();
      const declaredIds = new Set((members || []).map(item => item && item.system_anchor_id)
        .filter(value => value !== undefined && value !== null && String(value) !== '')
        .map(String));
      (members || []).forEach(candidate => {
        if (!candidate) return;
        const key = communityKey(candidate);
        const priority = candidate.anchor_role === 'global' ? 3
          : candidate.anchor_role === 'community' ? 2
          : declaredIds.has(String(candidate.id)) ? 1 : 0;
        const previous = communityAnchors.get(key);
        if (!previous || priority > previous.priority
          || (priority === previous.priority
            && finitePositive(candidate.gravity_mass, 1, 1000)
              > finitePositive(previous.node.gravity_mass, 1, 1000))
          || (priority === previous.priority
            && finitePositive(candidate.gravity_mass, 1, 1000)
              === finitePositive(previous.node.gravity_mass, 1, 1000)
            && String(candidate.id).localeCompare(String(previous.node.id)) < 0)) {
          communityAnchors.set(key, { node: candidate, priority });
        }
      });
      try { Object.defineProperty(lookup, '__galaxyCommunityAnchors', {
        value: communityAnchors, configurable: true,
      }); } catch (error) { lookup.__galaxyCommunityAnchors = communityAnchors; }
    }
    const inferred = communityAnchors.get(communityKey(node));
    if (inferred && inferred.node !== node) return inferred.node;
    return carrier && carrier !== node ? carrier : null;
  }
  /* Split the global group into its authoritative top-level carrier trees. */
  function galaxyBlackHoleCoreSystems(members, globalAnchor) {
    const values = (members || []).filter(node => node && node !== globalAnchor);
    const byId = new Map(values.map(node => [String(node.id), node]));
    const globalId = String(globalAnchor && globalAnchor.id);
    const groups = new Map();
    values.forEach(node => {
      let root = node;
      let current = node;
      const visited = new Set([String(node.id)]);
      while (current && current.system_anchor_id !== undefined
        && current.system_anchor_id !== null) {
        const parentId = String(current.system_anchor_id);
        if (!parentId || parentId === String(current.id)
          || parentId === globalId || visited.has(parentId)) break;
        const parent = byId.get(parentId);
        if (!parent) break;
        visited.add(parentId);
        root = parent;
        current = parent;
      }
      const key = String(root.id);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(node);
    });
    return [...groups.values()];
  }

  /* Resolve the one top-level carrier frame that the black hole is allowed to accelerate.
     Ordinary communities already arrive as one galaxyOrbitGroups() entry. Direct black-hole
     children share the global group, so split that group back into one carrier plus its complete
     stellar descendant tree. A planet or moon therefore never becomes an independent galactic
     particle merely because its star is directly linked to the black hole. */
  function galaxyBlackHoleCarrierSystems(nodes, globalAnchor, groupedCenters) {
    if (!globalAnchor) return [];
    const centers = groupedCenters || galaxyOrbitGroups(nodes);
    const coreKey = String(globalAnchor.id);
    const systems = [];
    const append = (members, center, core) => {
      const values = (members || []).filter(node => node && node !== globalAnchor
        && !node.ghost && Number.isFinite(node.x) && Number.isFinite(node.y));
      if (!values.length) return;
      const carrier = galaxySystemAnchor(values) || values[0];
      if (!carrier || carrier === globalAnchor) return;
      let mass = 0, x = 0, y = 0;
      values.forEach(node => {
        const nodeMass = finitePositive(node.gravity_mass, 1, 1000);
        mass += nodeMass; x += node.x * nodeMass; y += node.y * nodeMass;
      });
      const normalizedCenter = core ? {
        id: String(carrier.id), mass,
        x: mass > 0 ? x / mass : carrier.x,
        y: mass > 0 ? y / mass : carrier.y,
        nodes: values,
      } : center;
      systems.push({
        id: String(carrier.id), center: normalizedCenter,
        carrier, nodes: values, core: core === true,
      });
    };
    centers.forEach(center => {
      if (center.id === coreKey) {
        galaxyBlackHoleCoreSystems(center.nodes, globalAnchor)
          .forEach(members => append(members, null, true));
      } else append(center.nodes, center, false);
    });
    return systems;
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
  function setGalaxyOrbitAnchor(node, anchor) {
    const anchorId = anchor && anchor.id !== undefined && anchor.id !== null
      ? String(anchor.id) : '';
    if (!anchorId || !node) return;
    Object.defineProperty(node, '__galaxyOrbitAnchorId', {
      value: anchorId, writable: true, configurable: true, enumerable: false,
    });
  }
  function setGalaxyOrbitSeeded(node) {
    if (!node || node.__galaxyOrbitSeeded === true) return;
    Object.defineProperty(node, '__galaxyOrbitSeeded', {
      value: true, writable: true, configurable: true, enumerable: false,
    });
  }
  function setGalaxyOrbitSpeed(node, multiplier) {
    if (!node) return;
    Object.defineProperty(node, '__galaxyOrbitSpeedMultiplier', {
      value: multiplier, writable: true, configurable: true, enumerable: false,
    });
  }
  function setGalaxyOrbitBaseRadius(node, radius) {
    if (!node || !Number.isFinite(radius) || radius <= 0
      || Number.isFinite(Number(node.__galaxyOrbitBaseRadius))) return;
    Object.defineProperty(node, '__galaxyOrbitBaseRadius', {
      value: radius, writable: true, configurable: true, enumerable: false,
    });
  }
  function setGalaxySystemOrbitSpeed(node, multiplier) {
    if (!node) return;
    Object.defineProperty(node, '__galaxySystemOrbitSpeedMultiplier', {
      value: multiplier, writable: true, configurable: true, enumerable: false,
    });
  }
  /* Seed the same immediate-parent hierarchy used by the live force and kinematic clock. The
     older community pass remains for compatibility payloads, but this final authoritative pass
     repairs cross-community children and nested descendants that community grouping cannot see. */
  function seedGalaxyHierarchicalLocalOrbits(nodes, gravity, softening, options) {
    const opts = options || {};
    const orbitalSpeed = galaxyOrbitalSpeedMultiplier(opts.orbitalSpeed);
    const absoluteSpeedLimit = Math.max(0.01, Number(opts.speedLimit) || MAX_NODE_SPEED);
    const epsilon = Math.max(0.1, Number(softening) || 8);
    const centers = galaxyOrbitGroups(nodes);
    centers.forEach(center => {
      const members = center.nodes || [];
      const carrier = galaxySystemAnchor(members);
      if (!carrier || members.length < 2) return;
      const byId = new Map(members.map(node => [String(node.id), node]));
      orderedGalaxyLocalOrbitMembers(members, carrier, byId).forEach(node => {
        if (node === carrier || node.ghost || node.id === opts.fixedNodeId
          || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
        const parent = galaxyLocalOrbitParent(node, members, carrier, byId) || carrier;
        const dx = node.x - parent.x, dy = node.y - parent.y;
        const radius = Math.hypot(dx, dy);
        if (!(radius > 1e-9)) return;
        const authoredHierarchy = galaxyHasAuthoredParent(node, parent);
        const localGravityMultiplier = galaxyLocalGravityMultiplier(parent, opts);
        const localGravity = galaxySystemGravityConstant(parent, gravity,
          opts.localGravitySetting, authoredHierarchy)
          * localGravityMultiplier;
        const localAccelerationCap = defaultGalaxySystemAccelerationCap(parent, gravity,
          opts.localGravitySetting, authoredHierarchy)
          * Math.max(0.25, localGravityMultiplier);
        const denominator = Math.pow(radius * radius + epsilon * epsilon, 1.5);
        const rawAcceleration = localGravity * finitePositive(parent.gravity_mass, 1, 1000)
          * radius / Math.max(1e-9, denominator);
        const acceleration = localAccelerationCap > 0
          ? Math.min(localAccelerationCap, rawAcceleration) : rawAcceleration;
        const parentVx = Number.isFinite(parent.vx) ? parent.vx : 0;
        const parentVy = Number.isFinite(parent.vy) ? parent.vy : 0;
        const relativeVx = (Number.isFinite(node.vx) ? node.vx : 0) - parentVx;
        const relativeVy = (Number.isFinite(node.vy) ? node.vy : 0) - parentVy;
        const tangentX = -dy / radius, tangentY = dx / radius;
        const currentTangent = relativeVx * tangentX + relativeVy * tangentY;
        const sign = Math.sign(currentTangent)
          || ((seededHash(opts.layoutSeed, 'system:' + String(parent.id)) & 1) ? 1 : -1);
        const targetTangent = galaxyRelativeSpeedBudget(parent, absoluteSpeedLimit,
          Math.min(GALAXY_LOCAL_RELATIVE_SPEED_LIMIT,
            Math.sqrt(Math.max(0, acceleration * radius)) * orbitalSpeed),
          tangentX * sign, tangentY * sign);
        const parentId = String(parent.id);
        const previousParent = typeof node.__galaxyOrbitAnchorId === 'string'
          ? node.__galaxyOrbitAnchorId : '';
        const previousSpeed = Number(node.__galaxyOrbitSpeedMultiplier);
        const speedChanged = !Number.isFinite(previousSpeed)
          || Math.abs(previousSpeed - orbitalSpeed) > 1e-9;
        const needsSeed = previousParent !== parentId || Math.abs(currentTangent) < 1e-8;
        if (needsSeed || speedChanged) {
          node.vx = parentVx + tangentX * targetTangent * sign;
          node.vy = parentVy + tangentY * targetTangent * sign;
        }
        setGalaxyOrbitAnchor(node, parent);
        setGalaxyOrbitSpeed(node, orbitalSpeed);
        setGalaxyOrbitSeeded(node);
      });
    });
    return nodes;
  }
  /* Seed once for each node/central-star pairing. The pairing tag is deliberately
     non-enumerable, so scene export remains portable. More importantly, it makes a
     compatibility node that became eligible only after a later reveal (or a changed declared
     star) receive its one circular local seed without re-seeding healthy planets each frame. */
  function seedGalaxyOrbits(nodes, layoutSeed, gravity, softening, reducedMotion, options) {
    const opts = options || {};
    const orbitalSpeed = galaxyOrbitalSpeedMultiplier(opts.orbitalSpeed);
    const absoluteSpeedLimit = Math.max(0.01, Number(opts.speedLimit) || MAX_NODE_SPEED);
    const orbitalRadius = galaxyOrbitalRadiusMultiplier(opts.orbitalSpeed);
    const speedControlEnabled = opts.restorePhase !== true
      && Number.isFinite(Number(opts.orbitalSpeed));
    /* Direct children of the explicit black hole use compact physical lanes before the
       generic system seed supplies their ordinary black-hole-relative circular tangent.
       A pointer-owned node remains exact and is left for the drag/horizon path. */
    const blackHole = (nodes || []).find(node => node && !node.ghost
      && node.anchor_role === 'global' && Number.isFinite(node.x) && Number.isFinite(node.y));
    if (blackHole) {
      const blackHoleRadius = finitePositive(blackHole.radius,
        evidenceNodeRadius(blackHole, 3), 160);
      const coreSatellites = (nodes || []).filter(node => node && node !== blackHole
        && !node.ghost && node.id !== opts.fixedNodeId
        && String(node.system_anchor_id || '') === String(blackHole.id)
        && Number.isFinite(node.x) && Number.isFinite(node.y));
      /* Coincident core children used to inherit the farthest authored distance, then every
         child was placed on that same distant ring. Admit compact black-hole lanes instead:
         each ring is close to the horizon, each node has a deterministic phase, and overflow
         continues onto the next compact ring with a real radial clearance. The black hole
         remains fixed; these are independent test-particle phases, not a translated system. */
      const penetrating = coreSatellites.slice().sort(
        (left, right) => Number(left.orbit_tier || 0) - Number(right.orbit_tier || 0)
        || String(left.id).localeCompare(String(right.id)));
      const penetratingIds = new Set(penetrating.map(node => String(node.id)));
      const childrenByAnchor = new Map();
      (nodes || []).forEach(candidate => {
        if (!candidate || candidate.system_anchor_id === undefined
          || candidate.system_anchor_id === null) return;
        const parentId = String(candidate.system_anchor_id);
        if (!childrenByAnchor.has(parentId)) childrenByAnchor.set(parentId, []);
        childrenByAnchor.get(parentId).push(candidate);
      });
      const translateSystemDescendants = (root, shiftX, shiftY) => {
        if (!(Math.abs(shiftX) > 1e-12 || Math.abs(shiftY) > 1e-12)) return;
        const pending = [String(root.id)], visited = new Set();
        while (pending.length) {
          const parentId = pending.pop();
          if (visited.has(parentId)) continue;
          visited.add(parentId);
          (childrenByAnchor.get(parentId) || []).forEach(candidate => {
            if (!candidate || candidate === blackHole || penetratingIds.has(String(candidate.id))) return;
            candidate.x += shiftX;
            candidate.y += shiftY;
            pending.push(String(candidate.id));
          });
        }
      };
      const laneGap = Math.max(3, GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING);
      const compactBaseRadius = penetrating.reduce((maximum, node) => {
        const nodeRadius = finitePositive(node.radius, evidenceNodeRadius(node, 3), 160);
        const contact = blackHoleRadius + nodeRadius + GALAXY_BLACK_HOLE_EXCLUSION_PADDING;
        const outsideWarp = galaxyEventHorizonOuterRadius(
          blackHoleRadius, contact, GALAXY_EVENT_HORIZON_INFLUENCE_SCALE) + 1;
        return Math.max(maximum, outsideWarp);
      }, 0);
      const rings = [];
      let ringCursor = 0;
      let previousRingRadius = 0;
      let previousRingExtent = 0;
      while (ringCursor < penetrating.length) {
        const remaining = penetrating.slice(ringCursor);
        const ringExtent = remaining.reduce((maximum, node) => Math.max(maximum,
          finitePositive(node.radius, evidenceNodeRadius(node, 3), 160)), 0);
        const ringRadius = Math.max(compactBaseRadius,
          previousRingRadius + previousRingExtent + ringExtent + laneGap);
        let capacity = 1;
        while (capacity < remaining.length) {
          const candidate = capacity + 1;
          const chord = 2 * ringRadius * Math.sin(Math.PI / candidate);
          if (chord < ringExtent * 2 + laneGap - 1e-9) break;
          capacity = candidate;
        }
        const count = Math.min(capacity, remaining.length);
        rings.push({ start: ringCursor, count, radius: ringRadius, extent: ringExtent });
        ringCursor += count;
        previousRingRadius = ringRadius;
        previousRingExtent = ringExtent;
      }
      const phaseOffset = seededHash(layoutSeed, 'core-lanes:' + String(blackHole.id))
        / 0x100000000 * Math.PI * 2;
      rings.forEach((ring, ringIndex) => {
        const ringPhase = phaseOffset + seededHash(layoutSeed,
          'core-ring:' + String(blackHole.id) + ':' + ringIndex) / 0x100000000 * Math.PI * 2;
        penetrating.slice(ring.start, ring.start + ring.count).forEach((node, slot) => {
        const minimum = blackHoleRadius + finitePositive(node.radius,
          evidenceNodeRadius(node, 3), 160) + GALAXY_BLACK_HOLE_EXCLUSION_PADDING;
        const dx = node.x - blackHole.x, dy = node.y - blackHole.y;
        const distance = Math.hypot(dx, dy);
        const angle = ring.count > 1
          ? ringPhase + slot * Math.PI * 2 / ring.count
          : (distance > 1e-9 ? Math.atan2(dy, dx) : phaseOffset);
        const unitX = Math.cos(angle), unitY = Math.sin(angle);
        const anchorVx = Number.isFinite(blackHole.vx) ? blackHole.vx : 0;
        const anchorVy = Number.isFinite(blackHole.vy) ? blackHole.vy : 0;
        const relativeVx = (Number.isFinite(node.vx) ? node.vx : 0) - anchorVx;
        const relativeVy = (Number.isFinite(node.vy) ? node.vy : 0) - anchorVy;
        const tangentX = -unitY, tangentY = unitX;
        const radialSpeed = relativeVx * unitX + relativeVy * unitY;
        const tangentSpeed = relativeVx * tangentX + relativeVy * tangentY;
        const tangentScale = distance > 1e-9 ? Math.max(0, Math.min(1, distance / minimum)) : 0;
        const cachedLaneRadius = Number(node.__galaxyCoreLaneRadius);
        const cachedLaneAngle = Number(node.__galaxyCoreLaneAngle);
        const admittedRadius = Number.isFinite(cachedLaneRadius) && cachedLaneRadius > 0
          ? Math.max(minimum, cachedLaneRadius) : Math.max(minimum, ring.radius);
        const admittedAngle = Number.isFinite(cachedLaneAngle) ? cachedLaneAngle : angle;
        const admittedUnitX = Math.cos(admittedAngle), admittedUnitY = Math.sin(admittedAngle);
        const previousX = node.x, previousY = node.y;
        node.x = blackHole.x + admittedUnitX * admittedRadius;
        node.y = blackHole.y + admittedUnitY * admittedRadius;
        translateSystemDescendants(node, node.x - previousX, node.y - previousY);
        try {
          Object.defineProperty(node, '__galaxyCoreLaneRadius', {
            value: admittedRadius, writable: true, configurable: true, enumerable: false,
          });
          Object.defineProperty(node, '__galaxyCoreLaneAngle', {
            value: admittedAngle, writable: true, configurable: true, enumerable: false,
          });
        } catch (error) {
          node.__galaxyCoreLaneRadius = admittedRadius;
          node.__galaxyCoreLaneAngle = admittedAngle;
        }
        const admittedTangentX = -admittedUnitY, admittedTangentY = admittedUnitX;
        const admittedRadialSpeed = relativeVx * admittedUnitX + relativeVy * admittedUnitY;
        const admittedTangentSpeed = relativeVx * admittedTangentX + relativeVy * admittedTangentY;
        node.vx = anchorVx + Math.max(0, admittedRadialSpeed) * admittedUnitX
          + admittedTangentSpeed * tangentScale * admittedTangentX;
        node.vy = anchorVy + Math.max(0, admittedRadialSpeed) * admittedUnitY
          + admittedTangentSpeed * tangentScale * admittedTangentY;
        if (Number.isFinite(node.fx)) node.fx = node.x;
        if (Number.isFinite(node.fy)) node.fy = node.y;
        });
      });
    }
    /* Oversized/static renders only need direct black-hole lane admission. Leave ordinary
       local systems untouched so the normal horizon/exclusion pass can report and resolve
       their contacts instead of silently moving them during the seed. */
    if (opts.coreOnly === true) return nodes;
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
    const systemsToCheck = new Map();
    /* Capture this before installing the compatibility flag. A late member can inherit a
       moving star's frame and look tangential despite never receiving its own local orbit. */
    const wasOrbitSeeded = new Map();
    (nodes || []).forEach(node => {
      wasOrbitSeeded.set(node, node.__galaxyOrbitSeeded === true);
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
      if (!systemsToCheck.has(key)) systemsToCheck.set(key, []);
      systemsToCheck.get(key).push(node);
    });
    /* Seed satellites around the evidence-heaviest star from that one dominant attraction.
       A late reveal is expressed in the star's already-moving frame. The dominant node owns the
       local inertial frame: it follows the system's black-hole trajectory but never recoils when
       a planet is admitted, so a real local phase cannot be hidden by whole-system wobble. */
    systemsToCheck.forEach((members, key) => {
      const center = centers.get(key);
      if (!center || center.nodes.length < 2) return;
      const anchor = galaxySystemAnchor(center.nodes);
      /* Ghost/history nodes intentionally remain non-physical and are never promoted into an
         orbit here. The global core retains its established seed law below; its hierarchy is
         later governed by the black-hole frame rather than this repair path. */
      if (!anchor) return;
      setGalaxyOrbitSeeded(anchor);
      const authoredHierarchy = center.nodes.some(node => node !== anchor
        && galaxyHasAuthoredParent(node, anchor));
      const localGravityMultiplier = galaxyLocalGravityMultiplier(anchor, opts);
      const localGravity = galaxySystemGravityConstant(anchor, gravity,
        opts.localGravitySetting, authoredHierarchy)
        * localGravityMultiplier;
      const localAccelerationCap = defaultGalaxySystemAccelerationCap(anchor, gravity,
        opts.localGravitySetting, authoredHierarchy)
        * Math.max(0.25, localGravityMultiplier);
      const anchorMass = finitePositive(anchor.gravity_mass, 1, 1000);
      const anchorVx = Number.isFinite(anchor.vx) ? anchor.vx : 0;
      const anchorVy = Number.isFinite(anchor.vy) ? anchor.vy : 0;
      const direction = anchor.anchor_role === 'global'
        ? ((seededHash(layoutSeed, 'galaxy-spin') & 1) ? 1 : -1)
        : ((seededHash(layoutSeed, 'system:' + key) & 1) ? 1 : -1);
      const anchorId = String(anchor.id);
      const desiredVelocity = new Map();
      const repair = [];
      orderedGalaxySatellites(center.nodes, anchor).forEach(item => {
        const satellite = item.node;
        if (satellite.ghost || satellite.id === opts.fixedNodeId) return;
        let dx = satellite.x - anchor.x, dy = satellite.y - anchor.y;
        let currentRadius = Math.hypot(dx, dy);
        if (!(currentRadius > 1e-9)) return;
        setGalaxyOrbitBaseRadius(satellite, currentRadius);
        const baseRadius = Number(satellite.__galaxyOrbitBaseRadius);
        if (speedControlEnabled) {
          const minimumRadius = finitePositive(anchor.radius, evidenceNodeRadius(anchor, 3), 160)
            + finitePositive(satellite.radius, evidenceNodeRadius(satellite, 3), 160)
            + GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING;
          const targetRadius = Math.max(minimumRadius, baseRadius * orbitalRadius);
          if (Number.isFinite(targetRadius) && Math.abs(targetRadius - currentRadius) > 1e-9) {
            const angle = Math.atan2(dy, dx);
            satellite.x = anchor.x + Math.cos(angle) * targetRadius;
            satellite.y = anchor.y + Math.sin(angle) * targetRadius;
            if (Number.isFinite(satellite.fx)) satellite.fx = satellite.x;
            if (Number.isFinite(satellite.fy)) satellite.fy = satellite.y;
            dx = satellite.x - anchor.x;
            dy = satellite.y - anchor.y;
            currentRadius = targetRadius;
          }
        }
        const speedRadius = speedControlEnabled ? baseRadius : currentRadius;
        const denominator = Math.pow(
          speedRadius * speedRadius + epsilon * epsilon, 1.5);
        const rawInwardAcceleration = denominator > 0
          ? localGravity * anchorMass * speedRadius / denominator : 0;
        const inwardAcceleration = localAccelerationCap > 0
          ? Math.min(localAccelerationCap, rawInwardAcceleration) : rawInwardAcceleration;
        const omega = Math.sqrt(Math.max(0, inwardAcceleration / speedRadius));
        const relativeVx = (Number.isFinite(satellite.vx) ? satellite.vx : 0) - anchorVx;
        const relativeVy = (Number.isFinite(satellite.vy) ? satellite.vy : 0) - anchorVy;
        const tangent = (-dy * relativeVx + dx * relativeVy) / currentRadius;
        const targetTangent = galaxyRelativeSpeedBudget(anchor, absoluteSpeedLimit,
          Math.min(GALAXY_LOCAL_RELATIVE_SPEED_LIMIT,
            omega * speedRadius * orbitalSpeed),
          -dy / currentRadius * direction,
          dx / currentRadius * direction);
        const previousAnchorId = typeof satellite.__galaxyOrbitAnchorId === 'string'
          ? satellite.__galaxyOrbitAnchorId : '';
        const anchoredHere = previousAnchorId === anchorId;
        const anchorChanged = !!previousAnchorId && !anchoredHere;
        const wasSeeded = wasOrbitSeeded.get(satellite) === true;
        const previousSpeed = Number(satellite.__galaxyOrbitSpeedMultiplier);
        const speedKnown = Number.isFinite(previousSpeed);
        const speedChanged = speedKnown
          && Math.abs(previousSpeed - orbitalSpeed) > 1e-9;
        if (wasSeeded && anchoredHere && speedChanged) {
          const unitX = dx / currentRadius, unitY = dy / currentRadius;
          const radialSpeed = relativeVx * unitX + relativeVy * unitY;
          const tangentSpeed = (-unitY * relativeVx + unitX * relativeVy);
          const tangentDirection = Math.sign(tangentSpeed) || direction;
          const signedTarget = targetTangent * tangentDirection;
          satellite.vx = anchorVx + radialSpeed * unitX - unitY * signedTarget;
          satellite.vy = anchorVy + radialSpeed * unitY + unitX * signedTarget;
        }
        setGalaxyOrbitSpeed(satellite, orbitalSpeed);
        /* A preexisting healthy phase only needs its parent tag. Repaired legacy/late nodes
           must be genuinely sub-orbital before we touch them; this one-shot threshold avoids
           resetting a valid eccentric phase on ordinary render calls. */
        const movingLocally = Math.abs(tangent) >= Math.max(0.02, targetTangent * 0.18);
        /* The parent tag is not a permanent exemption: mode restoration, an old pin, or an
           integration failure can zero a previously healthy satellite after it was tagged.
           Repair only a truly frozen tagged phase (rather than every merely eccentric orbit),
           while untagged compatibility nodes still use the conservative sub-orbital check. */
        const frozenLocally = Math.abs(tangent) < 1e-8;
        if (wasSeeded && speedKnown && !anchorChanged
          && ((anchoredHere && !frozenLocally) || (!previousAnchorId && movingLocally))) {
          setGalaxyOrbitAnchor(satellite, anchor);
          setGalaxyOrbitSeeded(satellite);
          return;
        }
        repair.push(satellite);
        const unitX = dx / currentRadius, unitY = dy / currentRadius;
        const tangentX = -unitY * direction, tangentY = unitX * direction;
        desiredVelocity.set(satellite, {
          vx: anchorVx + tangentX * targetTangent,
          vy: anchorVy + tangentY * targetTangent,
        });
      });
      if (!repair.length) return;
      desiredVelocity.forEach((velocity, node) => {
        node.vx = velocity.vx;
        node.vy = velocity.vy;
        setGalaxyOrbitAnchor(node, anchor);
        setGalaxyOrbitSeeded(node);
      });
    });
    seedGalaxyHierarchicalLocalOrbits(nodes, gravity, softening, opts);
    return nodes;
  }

  /* Give whole solar systems one-shot angular momentum around the global evidence anchor.
     Each system follows the composite black-hole field with a bounded eccentric perturbation.
     The tag is intentionally not a permanent exemption: a filter/restore can retain the tag
     while supplying a zeroed velocity.  In that case repair the *system COM* once, preserving
     every local star/planet relative orbit rather than leaving a visibly frozen island. */
  function seedGalaxySystemOrbits(nodes, layoutSeed, gravity, softening, reducedMotion, options) {
    const opts = options || {};
    const orbitalSpeed = galaxyOrbitalSpeedMultiplier(opts.orbitalSpeed);
    /* Compatibility scenes may omit velocity fields on the selected fallback anchor. Give
       every physical body a finite frame velocity before computing system COM tangents; this
       is deliberately not a seed tag, so normal admission/repair policy remains unchanged. */
    (nodes || []).forEach(node => {
      if (!node || node.ghost || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      node.vx = Number.isFinite(node.vx) ? node.vx : 0;
      node.vy = Number.isFinite(node.vy) ? node.vy : 0;
    });
    /* A late external system can arrive exactly on the visible event horizon. Project that
       one contact before sampling its COM radius; otherwise the zero-radius guard below would
       skip it forever and the system would remain tagged but motionless after the next render. */
    if ((nodes || []).some(node => node && !node.ghost && node.anchor_role === 'global')) {
      applyGalaxyBlackHoleExclusion(nodes, {
        padding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING,
      });
    }
    const direction = (seededHash(layoutSeed, 'galaxy-spin') & 1) ? 1 : -1;
    /* Reduced motion is a paint/camera preference. The live solver still advances, so it must
       receive the same barycentric initial condition or whole systems contract radially without
       rotating around the black hole. */
    /* Use the same smooth black-hole field as the integrator, then add a small deterministic
       eccentric/radial perturbation. Systems are bound but not painted onto a rigid circular
       carousel; inner angular frequency remains higher than outer angular frequency. */
    const field = galaxyBlackHoleField(nodes, {
      gravity, softening,
      gravitationalConstant: opts.gravitationalConstant,
      blackHoleMass: opts.blackHoleMass,
    });
    if (!field.anchor || field.anchor.anchor_role !== 'global') {
      /* Compatibility embeds sometimes pass several independent communities without an
         explicit black-hole node. Preserve their historical fallback frame: the heaviest
         community is the stationary reference and each later community receives one bounded,
         deterministic tangent. This branch is intentionally excluded from the live composite
         field, which requires an authored global anchor. */
      const centers = [...communityCenters(nodes).values()];
      const fallbackAnchor = galaxyGlobalAnchor(nodes);
      if (!fallbackAnchor || centers.length < 2) return nodes;
      const fallbackConstant = galaxyFallbackStellarGravityConstant(gravity);
      centers.forEach(center => {
        if (center.nodes.includes(fallbackAnchor)) return;
        const carrier = galaxySystemAnchor(center.nodes) || center.nodes[0];
        const tagged = center.nodes.some(node => node.__galaxySystemOrbitSeeded === true);
        if (tagged) return;
        const dx = carrier.x - fallbackAnchor.x, dy = carrier.y - fallbackAnchor.y;
        const radius = Math.hypot(dx, dy);
        if (!(radius > 1e-9)) return;
        const tangentX = -dy / radius * direction;
        const tangentY = dx / radius * direction;
        const soft = Math.max(0.1, Number(softening) || 40);
        const denominator = Math.pow(radius * radius + soft * soft, 1.5);
        const speed = Math.min(GALAXY_SYSTEM_ORBIT_SEED_SPEED_LIMIT,
          Math.sqrt(Math.max(0, fallbackConstant * fallbackAnchor.gravity_mass * radius
            / Math.max(1e-9, denominator))));
        center.nodes.forEach(node => {
          node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + tangentX * speed;
          node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + tangentY * speed;
          setGalaxySystemOrbitSpeed(node, orbitalSpeed);
          Object.defineProperty(node, '__galaxySystemOrbitSeeded', {
            value: true, writable: true, configurable: true, enumerable: false,
          });
        });
      });
      return nodes;
    }
    if (!(field.gravitationalConstant > 0) || !field.systems.length) return nodes;
    field.systems.forEach(item => {
      if (item.radius <= 1e-9) return;
      const members = item.nodes;
      const carrier = item.carrier;
      const tagged = members.some(node => node.__galaxySystemOrbitSeeded === true);
      const previousSpeed = Number(carrier.__galaxySystemOrbitSpeedMultiplier);
      const speedKnown = Number.isFinite(previousSpeed);
      const speedChanged = speedKnown
        && Math.abs(previousSpeed - orbitalSpeed) > 1e-9;
      /* The dominant star—not the barycentre altered by its planets' local tangents—is the
         galactic carrier. G_star may change planet speed without changing this G_center orbit;
         translating every member by the star's carrier correction preserves all local relative
         velocities exactly. */
      const centerVx = Number.isFinite(carrier.vx) ? carrier.vx : 0;
      const centerVy = Number.isFinite(carrier.vy) ? carrier.vy : 0;
      const outwardX = -item.dx / item.radius, outwardY = -item.dy / item.radius;
      const tangentX = -outwardY * direction, tangentY = outwardX * direction;
      const tangentialSpeed = centerVx * tangentX + centerVy * tangentY;
      /* A tagged eccentric system still has meaningful angular momentum. Repair only a
         visibly sub-orbital COM; this avoids turning normal periapsis and apoapsis into a
         per-render carousel while not accepting a nearly frozen cached tag forever. */
      const stalledThreshold = Math.max(0.0025, item.circularSpeed * 0.18);
      const stalled = Math.abs(tangentialSpeed) < stalledThreshold;
      if (tagged && (!speedKnown || !speedChanged) && !stalled) {
        members.forEach(node => {
          node.vx = Number.isFinite(node.vx) ? node.vx : 0;
          node.vy = Number.isFinite(node.vy) ? node.vy : 0;
          if (node.__galaxySystemOrbitSeeded !== true) {
            Object.defineProperty(node, '__galaxySystemOrbitSeeded', {
              value: true, writable: true, configurable: true, enumerable: false
            });
          }
        });
        return;
      }
      const tangentFactor = 0.92
        + (seededHash(layoutSeed, 'system-speed:' + item.id) / 0x100000000) * 0.12;
      /* Start every system on a gentle settling spiral. A symmetric +/- phase can launch an
         outer system away from the well before gravity turns it around; a bounded inward kick
         gives the black-hole centre first claim on motion while preserving tangential rotation. */
      /* Start on the collision-free lane itself. A compulsory inward kick contradicts the
         circular seed and makes every otherwise healthy system spiral into its neighbours. */
      const radialFactor = 0;
      const authoredCarrierClock = item.core ? 1 : GALAXY_AUTHORED_CARRIER_ORBIT_CLOCK;
      const speed = Math.min(
        GALAXY_SYSTEM_ORBIT_SEED_SPEED_LIMIT * orbitalSpeed * authoredCarrierClock,
        item.circularSpeed * tangentFactor * orbitalSpeed * authoredCarrierClock
      );
      const kick = {
        vx: tangentX * speed + outwardX * speed * radialFactor,
        vy: tangentY * speed + outwardY * speed * radialFactor,
      };
      /* Translate every member by the same COM correction.  That is momentum-balanced inside
         the solar system (and leaves all local relative velocities exactly intact), while the
         fixed black-hole frame is the intentional external momentum reservoir.  Crucially we
         replace a stalled COM instead of adding another kick to a tagged frozen system. */
      const deltaX = kick.vx - centerVx;
      const deltaY = kick.vy - centerVy;
      members.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + deltaX;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + deltaY;
        setGalaxySystemOrbitSpeed(node, orbitalSpeed);
        Object.defineProperty(node, '__galaxySystemOrbitSeeded', {
          value: true, writable: true, configurable: true, enumerable: false
        });
      });
    });
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
    galaxyOrbitGroups(bodies).forEach(center => groups.set(center.id, center.nodes));
    const localGravitySetting = galaxyLocalGravitySetting(opts.gravity,
      opts.localGravitySetting);
    const gravity = galaxyLocalGravityConstant(localGravitySetting);
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
      const isCoreCommunity = coreCommunity !== null
        && (key === coreCommunity || members.some(node =>
          String(node.community_id || '') === coreCommunity));
      const groupSmoothFraction = isCoreCommunity
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
        ? Number(opts.accelerationCap) : defaultGalaxyAccelerationCap(localGravitySetting));
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
     gravity. The dominant star is the local inertial frame: the black-hole and inter-system
     fields translate it with the complete system, while only its planets receive this central
     acceleration. That preserves every planet's sampled relative orbit without a fictitious
     star wobble masking local phase. */
  function applyGalaxySystemAnchorGravity(nodes, options) {
    const opts = options || {};
    const localGravitySetting = galaxyLocalGravitySetting(opts.gravity,
      opts.localGravitySetting);
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const groups = new Map();
    galaxyOrbitGroups(bodies).forEach(center => groups.set(center.id, center.nodes));
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
      localGravitationalConstant: galaxyPhysicsMultiplier(
        opts.localGravitationalConstant,
        GALAXY_LOCAL_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8),
      eligibleStellarAnchors: 0, fallbackAnchors: 0, globalAnchors: 0,
    };
    if (!(alphaValue > 0)) return stats;
    groups.forEach(members => {
      if (members.length < 2) return;
      const anchor = galaxySystemAnchor(members);
      if (!anchor) return;
      stats.anchors++;
      if (anchor.anchor_role === 'community') {
        stats.eligibleStellarAnchors++;
      } else if (anchor.anchor_role === 'global') stats.globalAnchors++;
      else stats.fallbackAnchors++;
      const gravityMultiplier = galaxyLocalGravityMultiplier(anchor, opts);
      const accelerationCap = explicitAccelerationCap !== null
        ? explicitAccelerationCap : defaultGalaxySystemAccelerationCap(anchor, opts.gravity,
          localGravitySetting)
          * Math.max(0.25, gravityMultiplier);
      const accelerations = new Map(members.map(node => [node, { ax: 0, ay: 0 }]));
      let systemMaximumRepulsion = 0, systemMaximumSampledAttraction = 0;
      let systemMaximumNetRepulsion = 0, systemMinimumSurfaceNetRepulsion = null;
      const byId = new Map(members.map(node => [String(node.id), node]));
      const childrenByParent = new Map();
      members.forEach(node => {
        if (node === anchor) return;
        const parent = galaxyLocalOrbitParent(node, members, anchor, byId) || anchor;
        if (!childrenByParent.has(parent)) childrenByParent.set(parent, []);
        childrenByParent.get(parent).push(node);
      });
      childrenByParent.forEach((satellites, parent) => {
        /* The global potential owns every explicitly declared direct child. */
        const skipGlobalParent = parent.anchor_role === 'global'
          && (opts.skipGlobalParent === true || (opts.allowGlobalParent !== true
            && satellites.some(satellite => satellite.system_anchor_id !== undefined
              && satellite.system_anchor_id !== null
              && String(satellite.system_anchor_id) === String(parent.id))));
        if (skipGlobalParent) return;
        const parentMass = finitePositive(parent.gravity_mass, 1, 1000);
        const authoredHierarchy = satellites.some(satellite =>
          galaxyHasAuthoredParent(satellite, parent));
        const parentGravityMultiplier = galaxyLocalGravityMultiplier(parent, opts);
        const parentGravity = galaxySystemGravityConstant(parent, opts.gravity,
          localGravitySetting, authoredHierarchy) * parentGravityMultiplier;
        satellites.sort((left, right) => Number(left.orbit_tier || 0)
          - Number(right.orbit_tier || 0) || String(left.id).localeCompare(String(right.id)));
        satellites.forEach(satellite => {
          let dx = parent.x - satellite.x, dy = parent.y - satellite.y;
          let distance = Math.hypot(dx, dy);
          if (!(distance > 1e-9)) {
            const angle = seededHash(0, 'stellar-pressure:' + String(parent.id)
              + '|' + String(satellite.id)) / 0x100000000 * Math.PI * 2;
            dx = -Math.cos(angle) * 1e-9;
            dy = -Math.sin(angle) * 1e-9;
            distance = 1e-9;
          }
          const denominator = Math.pow(dx * dx + dy * dy + softening * softening, 1.5);
          if (!(denominator > 0) || !Number.isFinite(denominator)) return;
          const scale = parentGravity * alphaValue / denominator;
          const sampledAttraction = distance * scale * parentMass;
          const satelliteAcceleration = accelerations.get(satellite);
          satelliteAcceleration.ax += dx * scale * parentMass;
          satelliteAcceleration.ay += dy * scale * parentMass;
          /* Every local parent owns a painted clearance band. This keeps nested moons from
             colliding with their immediate carrier while preserving the global black-hole
             boundary as a separate constraint. */
          if (parent.anchor_role !== 'global' && repulsionAcceleration > 0) {
            const surfaceDistance = bodyRadius(parent) + bodyRadius(satellite)
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
      });
      /* Do not add an equal-and-opposite local kick to the dominant node. The dashboard renders
         that star as the stationary centre of its own solar system; galaxy-wide fields below
         still give every member the same black-hole-frame translation. */
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

  /* Permanent local-surface contact for every carrier hierarchy. Projection is radial and
     bounded to the exact painted edge; velocity response removes only inward normal motion in
     the parent frame. Tangential velocity is untouched, so contact cannot drain orbital phase
     or manufacture a repulsive slingshot. The global anchor is deliberately excluded here:
     direct-BH carriers and their complete systems use the rigid event-horizon projection. */
  function applyGalaxySystemAnchorExclusion(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const groups = new Map();
    galaxyOrbitGroups(bodies).forEach(center => groups.set(center.id, center.nodes));
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
      if (!anchor) return;
      stats.systems++;
      const byId = new Map(members.map(node => [String(node.id), node]));
      /* Resolve every direct parent instead of projecting every body against the top star. This
         preserves nested moon trajectories and gives each local carrier its own clearance band. */
      const satellites = members.filter(node => node !== anchor).map(node => ({
        node, parent: galaxyLocalOrbitParent(node, members, anchor, byId) || anchor,
      })).filter(item => item.parent.anchor_role !== 'global')
        .sort((left, right) => Number(left.node.orbit_tier || 0)
        - Number(right.node.orbit_tier || 0) || String(left.node.id).localeCompare(String(right.node.id)));
      /* A bounded solve handles pathological dense payloads with 80+ bodies around one dominant
         node. Ordinary non-contact systems still exit after one O(n) scan; every penetration is
         projected in the stationary star frame and therefore closes in one pass per satellite. */
      for (let iteration = 0; iteration < maximumIterations; iteration++) {
        let corrected = false;
        let maximumPenetration = 0;
        satellites.forEach(item => {
          const satellite = item.node;
          const parent = item.parent;
          const minimumDistance = bodyRadius(parent) + bodyRadius(satellite) + padding;
          let dx = satellite.x - parent.x, dy = satellite.y - parent.y;
          let distance = Math.hypot(dx, dy);
          let unitX, unitY;
          if (distance > 1e-9) {
            unitX = dx / distance;
            unitY = dy / distance;
          } else {
            const angle = seededHash(0, String(parent.id) + '|' + String(satellite.id))
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
          const anchorInverseMass = 0;
          const satelliteInverseMass = 1 / satelliteMass;
          const inverseMass = satelliteInverseMass;
          const anchorShift = 0;
          const satelliteShift = correction;
          satellite.x += unitX * satelliteShift;
          satellite.y += unitY * satelliteShift;
          if (Number.isFinite(parent.fx)) parent.fx = parent.x;
          if (Number.isFinite(parent.fy)) parent.fy = parent.y;
          if (Number.isFinite(satellite.fx)) satellite.fx = satellite.x;
          if (Number.isFinite(satellite.fy)) satellite.fy = satellite.y;
          const relativeVx = (Number.isFinite(satellite.vx) ? satellite.vx : 0)
            - (Number.isFinite(parent.vx) ? parent.vx : 0);
          const relativeVy = (Number.isFinite(satellite.vy) ? satellite.vy : 0)
            - (Number.isFinite(parent.vy) ? parent.vy : 0);
          const inwardSpeed = relativeVx * unitX + relativeVy * unitY;
          if (inwardSpeed < 0) {
            const impulse = -inwardSpeed / inverseMass;
            parent.vx -= unitX * impulse * anchorInverseMass;
            parent.vy -= unitY * impulse * anchorInverseMass;
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
        const minimumDistance = bodyRadius(item.parent) + bodyRadius(item.node) + padding;
        const rawClearance = Math.hypot(item.node.x - item.parent.x,
          item.node.y - item.parent.y)
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
    galaxyOrbitGroups(nodes || []).forEach(center => groups.set(center.id, center.nodes));
    let systems = 0, satellites = 0, minimumClearance = null;
    groups.forEach(members => {
      if (members.length < 2) return;
      const anchor = galaxySystemAnchor(members);
      if (!anchor) return;
      systems++;
      const byId = new Map(members.map(node => [String(node.id), node]));
      members.filter(node => node !== anchor).forEach(node => {
        const parent = galaxyLocalOrbitParent(node, members, anchor, byId) || anchor;
        /* `central:false` is the dependency-light legacy two-body contract where a caller may
           label its only star `global` without enabling a galactic black-hole field. Production
           Galaxy mode always enables the central field and therefore always takes this skip. */
        if (parent.anchor_role === 'global' && opts.central !== false) return;
        const clearance = Math.hypot(node.x - parent.x, node.y - parent.y)
          - bodyRadius(parent) - bodyRadius(node) - padding;
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
    const gravityMultiplier = galaxyPhysicsMultiplier(opts.gravitationalConstant,
      GALAXY_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8);
    const gravitationalConstant = galaxyBlackHoleGravityConstant(opts.gravity) * strengthFraction
      * gravityMultiplier;
    const softening = Math.max(0.1, Number(opts.softening)
      || GALAXY_MUTUAL_SYSTEM_SOFTENING);
    const alphaValue = Number.isFinite(opts.alpha) ? Math.max(0, opts.alpha) : 1;
    const exactLimit = Math.max(2, Number(opts.exactLimit) || GALAXY_EXACT_LIMIT);
    const theta = Math.max(0.1, Number(opts.theta) || GALAXY_BARNES_HUT_THETA);
    const accelerationCap = Math.max(0, Number.isFinite(Number(opts.accelerationCap))
      ? Number(opts.accelerationCap)
      : defaultGalaxyAccelerationCap(opts.gravity) * strengthFraction
        * Math.max(0.25, gravityMultiplier));
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
      const nodeRank = Number.isFinite(Number(node.scene_rank)) ? Number(node.scene_rank) : 0;
      const anchorRank = Number.isFinite(Number(anchor.scene_rank)) ? Number(anchor.scene_rank) : 0;
      const nodeStructure = Number.isFinite(Number(node.weighted_degree))
        ? Number(node.weighted_degree) : (Number.isFinite(Number(node.degree)) ? Number(node.degree) : 0);
      const anchorStructure = Number.isFinite(Number(anchor.weighted_degree))
        ? Number(anchor.weighted_degree) : (Number.isFinite(Number(anchor.degree)) ? Number(anchor.degree) : 0);
      if (nodeGlobal > anchorGlobal || (nodeGlobal === anchorGlobal
        && (nodeMass > anchorMass || (nodeMass === anchorMass
          && (nodeRank > anchorRank || (nodeRank === anchorRank
            && (nodeStructure > anchorStructure || (nodeStructure === anchorStructure
              && String(node.id).localeCompare(String(anchor.id)) < 0)))))))) anchor = node;
    });
    return anchor;
  }

  function galaxyBlackHoleSpinAngle(node) {
    if (!node) return 0;
    const propertyAngle = Number(node.__galaxyBlackHoleSpinAngle);
    if (Number.isFinite(propertyAngle)) return propertyAngle;
    const cachedAngle = galaxyBlackHoleSpinCache ? galaxyBlackHoleSpinCache.get(node) : null;
    return Number.isFinite(cachedAngle) ? cachedAngle : 0;
  }

  function setGalaxyBlackHoleSpinAngle(node, angle) {
    if (!node || !Number.isFinite(angle)) return angle;
    if (galaxyBlackHoleSpinCache) galaxyBlackHoleSpinCache.set(node, angle);
    try {
      Object.defineProperty(node, '__galaxyBlackHoleSpinAngle', {
        value: angle, writable: true, configurable: true, enumerable: false,
      });
    } catch (_) {
      /* Frozen compatibility payloads still receive the WeakMap-backed visual phase. */
    }
    return angle;
  }

  function advanceGalaxyBlackHoleSpin(nodes, options) {
    const opts = options || {};
    const anchor = galaxyGlobalAnchor(nodes);
    if (!anchor || anchor.anchor_role !== 'global'
      || opts.frozen === true || opts.orbitPaused === true) {
      return anchor ? galaxyBlackHoleSpinAngle(anchor) : 0;
    }
    const timestep = Math.max(0.001, Math.min(2,
      Number(opts.timestep) || GALAXY_FIXED_TIMESTEP));
    const orbitalSpeed = galaxyOrbitalSpeedMultiplier(opts.orbitalSpeed);
    const direction = (seededHash(opts.layoutSeed, 'black-hole-spin') & 1) ? 1 : -1;
    return setGalaxyBlackHoleSpinAngle(anchor,
      galaxyBlackHoleSpinAngle(anchor) + direction
        * GALAXY_BLACK_HOLE_SPIN_RATE * orbitalSpeed * timestep);
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

  /* Sample the shared galactic rotation curve at one carrier radius. The compact source keeps a
     softened Kepler term; the distributed evidence halo uses a cored logarithmic potential:
       Phi_halo = .5 v0² ln(r² + a²),  v_halo² = v0² r² / (r² + a²).
     Calibrating v0² = G M_halo / (sqrt(2) a) exactly matches the former Plummer halo speed at
     r=a, while producing the observed approximately flat outer rotation curve of disk galaxies.
     The safety cap is per carrier, so one close system can never weaken every outer orbit. */
  function galaxyCarrierOrbitCurve(field, radius) {
    const r = Math.max(0, Number(radius) || 0);
    const gravitationalConstant = Math.max(0, Number(field && field.gravitationalConstant) || 0);
    const coreMass = Math.max(0, Number(field && field.coreMass) || 0);
    const haloMass = Math.max(0, Number(field && field.haloMass) || 0);
    const coreSoftening = Math.max(0.1, Number(field && field.coreSoftening) || 40);
    const haloScale = Math.max(0.1, Number(field && field.haloScale) || coreSoftening * 2);
    const coreDenominator = Math.pow(r * r + coreSoftening * coreSoftening, 1.5);
    const haloVelocitySquared = haloMass > 0
      ? gravitationalConstant * haloMass / (Math.SQRT2 * haloScale) : 0;
    let omegaSquared = gravitationalConstant * coreMass / coreDenominator
      + haloVelocitySquared / (r * r + haloScale * haloScale);
    const rawAcceleration = Math.max(0, omegaSquared) * r;
    const accelerationCap = Math.max(0, Number(field && field.accelerationCap) || 0);
    const capScale = accelerationCap > 0 && rawAcceleration > accelerationCap
      ? accelerationCap / rawAcceleration : 1;
    omegaSquared = Math.max(0, omegaSquared) * capScale;
    const omega = Math.sqrt(omegaSquared);
    return {
      omegaSquared, omega, circularSpeed: omega * r,
      haloVelocitySquared, rawAcceleration,
      acceleration: omegaSquared * r, capScale,
    };
  }

  function galaxyCarrierTargetSpeed(field, radius, orbitalSpeed) {
    const multiplier = galaxyOrbitalSpeedMultiplier(orbitalSpeed);
    return Math.min(GALAXY_CARRIER_FRAME_SPEED_LIMIT * multiplier,
      galaxyCarrierOrbitCurve(field, radius).circularSpeed
        * multiplier);
  }
  /* Authored external systems retain their established lane clock while the physical target
     remains mass- and gravity-aware. The explicit Orbital speed control is calibrated separately
     by galaxyOrbitalSpeedMultiplier. */
  const GALAXY_AUTHORED_CARRIER_ORBIT_CLOCK = 1.3;
  function galaxyAuthoredCarrierTargetSpeed(field, radius, orbitalSpeed) {
    return galaxyCarrierTargetSpeed(field, radius, orbitalSpeed)
      * GALAXY_AUTHORED_CARRIER_ORBIT_CLOCK;
  }

  /* A galaxy is not a collection of peer point masses. The black hole and smooth evidence halo
     act once on each top-level solar-system carrier. Every planet and moon inherits that rigid
     frame translation, then receives only its immediate local parent's stellar physics. */
  function galaxyBlackHoleField(nodes, options) {
    const opts = options || {};
    const centers = galaxyOrbitGroups(nodes);
    const anchor = galaxyGlobalAnchor(nodes);
    if (!anchor) return {
      anchor: null, systems: [], coreMass: 0, haloMass: 0, haloScale: 0, traversals: 0
    };
    const totalMass = [...centers.values()].reduce((sum, center) => sum + center.mass, 0);
    /* The singular center term is sourced by the actual dominant evidence node. Other stars
       in its community remain part of the smooth bulge/halo instead of inflating black-hole
       mass merely because they share a community label. */
    const blackHoleMassMultiplier = galaxyPhysicsMultiplier(opts.blackHoleMass,
      GALAXY_BLACK_HOLE_MASS_MULTIPLIER, 16);
    const baseCoreMass = finitePositive(anchor.gravity_mass, 1, 1000);
    const coreMass = baseCoreMass * blackHoleMassMultiplier;
    /* Black-hole mass tuning changes only the compact central source. It must not create or
       consume halo evidence mass; the scene's remaining authored mass stays invariant. */
    const haloMass = Math.max(0, totalMass - baseCoreMass);
    const carriers = galaxyBlackHoleCarrierSystems(nodes, anchor, centers);
    const coreSoftening = Math.max(0.1, Number(opts.softening) || 40);
    const hintedRadii = carriers.map(item => {
      const hint = item.nodes.map(node => Number(node.galactic_radius))
        .find(value => Number.isFinite(value) && value > 0);
      return hint || Math.hypot(item.carrier.x - anchor.x, item.carrier.y - anchor.y);
    });
    const initialMedianRadius = linearMedian(hintedRadii);
    const explicitScale = Number(opts.haloScale);
    const cachedScale = Number(anchor.__galaxyHaloScale);
    const haloScale = Math.max(coreSoftening * 2,
      Number.isFinite(explicitScale) && explicitScale > 0 ? explicitScale
        : Number.isFinite(cachedScale) && cachedScale > 0 ? cachedScale
          : initialMedianRadius * 0.65);
    /* The halo is part of the scene's potential, not a rubber band fitted to the current
       positions. Recomputing it after every inward step shrinks the halo radius, deepens
       the next step, and creates runaway collapse/ejection. Cache the seed scale on the
       black-hole node; it is non-enumerable, so exports and a fresh setData payload stay clean. */
    if (!(Number.isFinite(cachedScale) && cachedScale > 0)
      && !(Number.isFinite(explicitScale) && explicitScale > 0)) {
      Object.defineProperty(anchor, '__galaxyHaloScale', {
        value: haloScale, writable: false, configurable: true, enumerable: false
      });
    }
    const explicitGlobal = anchor.anchor_role === 'global';
    const gravitationalConstantMultiplier = galaxyPhysicsMultiplier(opts.gravitationalConstant,
      GALAXY_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8);
    const gravitationalConstant = galaxyBlackHoleGravityConstant(opts.gravity, explicitGlobal)
      * gravitationalConstantMultiplier * Math.sqrt(Math.max(0.25, blackHoleMassMultiplier));
    const accelerationCap = Math.max(0, Number.isFinite(Number(opts.accelerationCap))
      ? Number(opts.accelerationCap)
      : defaultGalaxyBlackHoleAccelerationCap(opts.gravity, explicitGlobal)
        * Math.max(0.25, Math.min(8,
          gravitationalConstantMultiplier * Math.max(1, blackHoleMassMultiplier))));
    const haloVelocitySquared = haloMass > 0
      ? gravitationalConstant * haloMass / (Math.SQRT2 * haloScale) : 0;
    const model = {
      coreMass, haloMass, haloScale, coreSoftening, gravitationalConstant,
      accelerationCap, haloVelocitySquared,
    };
    const systems = carriers.map(item => {
      const dx = anchor.x - item.carrier.x;
      const dy = anchor.y - item.carrier.y;
      const radius = Math.hypot(dx, dy);
      const curve = galaxyCarrierOrbitCurve(model, radius);
      return { ...item, dx, dy, radius, ...curve,
        ax: dx * curve.omegaSquared, ay: dy * curve.omegaSquared };
    });
    const maximumAcceleration = systems.reduce(
      (maximum, item) => Math.max(maximum, Math.hypot(item.ax, item.ay)), 0
    );
    const capScale = systems.reduce((minimum, item) => Math.min(minimum, item.capScale), 1);
    return {
      anchor, systems, baseCoreMass, coreMass, haloMass, haloScale, totalMass,
      coreSoftening, haloVelocitySquared, accelerationCap, maximumAcceleration, capScale,
      gravitationalConstant, gravitationalConstantMultiplier,
      blackHoleMassMultiplier,
      gravitySetting: galaxyBlackHoleGravitySetting(opts.gravity, explicitGlobal),
      traversals: centers.size,
    };
  }

  function applyGalaxyBlackHoleGravity(nodes, options) {
    const field = galaxyBlackHoleField(nodes, options);
    field.systems.forEach(item => item.nodes.forEach(node => {
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

  function setGalaxySpacetimeWarp(node, value) {
    if (!node) return;
    const warp = Math.max(0, Math.min(1, Number(value) || 0));
    try {
      if (Object.prototype.hasOwnProperty.call(node, '__galaxySpacetimeWarp')) {
        node.__galaxySpacetimeWarp = warp;
      } else {
        Object.defineProperty(node, '__galaxySpacetimeWarp', {
          value: warp, writable: true, configurable: true, enumerable: false,
        });
      }
    } catch (error) { /* Frozen compatibility payloads still receive the physical field. */ }
  }

  /* Bounded weak-field frame dragging plus a smooth near-horizon acceleration band. Every
     top-level carrier system receives one rigid acceleration, including a star directly linked
     to the black hole. Its planets and moons inherit the frame and never receive an independent
     black-hole kick. The strict painted horizon remains an impenetrable numerical boundary. */
  function applyGalaxySpacetimeAcceleration(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const field = galaxyBlackHoleField(bodies, opts);
    const anchor = field.anchor && field.anchor.anchor_role === 'global' ? field.anchor : null;
    const stats = {
      anchorId: anchor ? anchor.id : null, systems: 0, coreNodes: 0, warpedNodes: 0,
      maximumWarp: 0, maximumFrameDragAcceleration: 0,
      maximumHorizonAcceleration: 0,
      tidalSystems: 0, tidalPlanets: 0, maximumTidalAcceleration: 0,
      accelerations: new Map(),
    };
    bodies.forEach(node => setGalaxySpacetimeWarp(node, node === anchor ? 1 : 0));
    if (!anchor) return stats;
    const anchorRadius = finitePositive(anchor.radius, evidenceNodeRadius(anchor, 3), 160);
    const padding = Math.max(0, Number.isFinite(Number(opts.blackHoleExclusionPadding))
      ? Number(opts.blackHoleExclusionPadding) : GALAXY_BLACK_HOLE_EXCLUSION_PADDING);
    const influenceScale = Math.max(1.1,
      Number.isFinite(Number(opts.eventHorizonInfluenceScale))
        ? Number(opts.eventHorizonInfluenceScale) : GALAXY_EVENT_HORIZON_INFLUENCE_SCALE);
    const draggingFraction = Math.max(0, Number.isFinite(Number(opts.frameDraggingFraction))
      ? Number(opts.frameDraggingFraction) : GALAXY_FRAME_DRAGGING_FRACTION);
    const draggingCap = Math.max(0, Number.isFinite(Number(opts.frameDraggingMaxAcceleration))
      ? Number(opts.frameDraggingMaxAcceleration) : GALAXY_FRAME_DRAGGING_MAX_ACCELERATION);
    const horizonAcceleration = Math.max(0,
      Number.isFinite(Number(opts.eventHorizonInwardAcceleration))
        ? Number(opts.eventHorizonInwardAcceleration)
        : GALAXY_EVENT_HORIZON_INWARD_ACCELERATION);
    const direction = Number(opts.frameDraggingDirection) < 0 ? -1 : 1;
    const bodyRadius = node => finitePositive(
      node.radius, evidenceNodeRadius(node, 3), 160
    );
    const accelerate = (members, dx, dy, contactRadius, gravityAcceleration, scope) => {
      const distance = Math.hypot(dx, dy);
      if (!(distance > 1e-9)) return 0;
      const unitX = dx / distance, unitY = dy / distance;
      /* `contactRadius` includes the complete solar-system radius so its nearest painted
         planet cannot cross the black-hole surface. Multiplying that composite radius made a
         wide solar system look "near horizon" while its star was still far away, draining the
         ordinary galactic orbit. Curvature instead extends a fixed number of black-hole radii
         beyond the safe painted contact: system size affects collision clearance, not the
         spacetime-well thickness. */
      const outerRadius = galaxyEventHorizonOuterRadius(
        anchorRadius, contactRadius, influenceScale);
      const warp = distance < outerRadius
        ? galaxySmoothstep((outerRadius - distance) / Math.max(1e-9, outerRadius - contactRadius))
        : 0;
      const radialAcceleration = horizonAcceleration * warp * warp;
      const frameAcceleration = Math.min(draggingCap,
        Math.max(0, gravityAcceleration) * draggingFraction
          * warp * Math.pow(contactRadius / Math.max(contactRadius, distance), 2));
      const tangentX = -unitY * direction, tangentY = unitX * direction;
      members.forEach(node => {
        stats.accelerations.set(node, {
          ax: -unitX * radialAcceleration + tangentX * frameAcceleration,
          ay: -unitY * radialAcceleration + tangentY * frameAcceleration,
        });
        setGalaxySpacetimeWarp(node, warp);
      });
      if (warp > 0) stats.warpedNodes += members.length;
      stats.maximumWarp = Math.max(stats.maximumWarp, warp);
      stats.maximumFrameDragAcceleration = Math.max(
        stats.maximumFrameDragAcceleration, frameAcceleration);
      stats.maximumHorizonAcceleration = Math.max(
        stats.maximumHorizonAcceleration, radialAcceleration);
      if (scope === 'core') stats.coreNodes += members.length;
      else stats.systems++;
      return warp;
    };
    field.systems.forEach(item => {
      const carrier = item.carrier;
      if (!carrier || !item.nodes.length) return;
      const carrierDx = carrier.x - anchor.x;
      const carrierDy = carrier.y - anchor.y;
      accelerate(item.nodes, carrierDx, carrierDy,
        anchorRadius + bodyRadius(carrier) + padding,
        Math.hypot(item.ax, item.ay), item.core ? 'core' : 'system');
    });
    return stats;
  }

  /* Dissipate only the black-hole-frame carrier tangent in the event-horizon band. Local
     planet/star relative velocity is untouched because every external system receives the same
     delta. This models orbital decay without a singular kick or the violent local reheating that
     per-node damping would cause. */
  function applyGalaxyEventHorizonDecay(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const field = galaxyBlackHoleField(bodies, opts);
    const anchor = field.anchor;
    const rate = Math.max(0, Number.isFinite(Number(opts.eventHorizonDecayRate))
      ? Number(opts.eventHorizonDecayRate) : GALAXY_EVENT_HORIZON_DECAY_RATE);
    const timestep = Math.max(0, Number(opts.timestep) || 1);
    const stats = { anchorId: anchor ? anchor.id : null, systems: 0, nodes: 0,
      maximumWarp: 0, maximumVelocityRemoved: 0 };
    if (!anchor || anchor.anchor_role !== 'global' || !(rate > 0) || !(timestep > 0)) return stats;
    const anchorVx = Number.isFinite(anchor.vx) ? anchor.vx : 0;
    const anchorVy = Number.isFinite(anchor.vy) ? anchor.vy : 0;
    field.systems.forEach(item => {
        const group = item.nodes;
        const carrier = item.carrier;
        if (!group.length || !carrier) return;
        const warp = group.reduce((maximum, node) => Math.max(maximum,
          Number(node.__galaxySpacetimeWarp) || 0), 0);
        if (!(warp > 0)) return;
        const dx = carrier.x - anchor.x, dy = carrier.y - anchor.y;
        const distance = Math.hypot(dx, dy);
        if (!(distance > 1e-9)) return;
        const vx = (Number.isFinite(carrier.vx) ? carrier.vx : 0) - anchorVx;
        const vy = (Number.isFinite(carrier.vy) ? carrier.vy : 0) - anchorVy;
        const unitX = dx / distance, unitY = dy / distance;
        const tangentX = -unitY, tangentY = unitX;
        const tangentSpeed = vx * tangentX + vy * tangentY;
        const keep = Math.exp(-rate * warp * warp * timestep);
        const removed = tangentSpeed * (1 - keep);
        group.forEach(node => {
          node.vx -= tangentX * removed;
          node.vy -= tangentY * removed;
        });
        stats.systems++;
        stats.nodes += group.length;
        stats.maximumWarp = Math.max(stats.maximumWarp, warp);
        stats.maximumVelocityRemoved = Math.max(stats.maximumVelocityRemoved, Math.abs(removed));
    });
    return stats;
  }

  /* Conservative drag-release capture. Only a non-anchor body already declaring a community
     star, or belonging to that star's authored community, is eligible; this never rewrites
     system_anchor_id/community topology. Sub-escape releases inside the bounded capture radius
     are inserted into a softened circular star-relative orbit. High-speed releases retain their
     capped pointer velocity as intentional escape trajectories. */
  function galaxySlingshotCapture(node, nodes, releaseVelocity, options) {
    const opts = options || {};
    const velocity = {
      vx: Number.isFinite(releaseVelocity && releaseVelocity.vx) ? releaseVelocity.vx : 0,
      vy: Number.isFinite(releaseVelocity && releaseVelocity.vy) ? releaseVelocity.vy : 0,
    };
    const result = { eligible: false, captured: false, escaped: false,
      reason: 'ineligible', starId: null, radius: null, circularSpeed: null,
      escapeSpeed: null, vx: velocity.vx, vy: velocity.vy };
    if (!node || node.anchor_role === 'global' || node.anchor_role === 'community'
      || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return result;
    const explicitId = node.system_anchor_id === undefined || node.system_anchor_id === null
      ? '' : String(node.system_anchor_id).trim();
    const stars = (nodes || []).filter(candidate => candidate && candidate !== node
      && !candidate.ghost && candidate.anchor_role === 'community'
      && Number.isFinite(candidate.x) && Number.isFinite(candidate.y));
    let candidates = explicitId
      ? stars.filter(star => String(star.id) === explicitId)
      : stars.filter(star => communityKey(star) === communityKey(node));
    if (!candidates.length) return result;
    candidates = candidates.sort((left, right) =>
      Math.hypot(node.x - left.x, node.y - left.y)
        - Math.hypot(node.x - right.x, node.y - right.y)
      || String(left.id).localeCompare(String(right.id)));
    const star = candidates[0];
    const dx = node.x - star.x, dy = node.y - star.y;
    const radius = Math.hypot(dx, dy);
    const captureRadius = Math.max(1, Number.isFinite(Number(opts.captureRadius))
      ? Number(opts.captureRadius) : GALAXY_SLINGSHOT_CAPTURE_RADIUS);
    result.eligible = true;
    result.starId = star.id;
    result.radius = radius;
    if (!(radius > 1e-9) || radius > captureRadius) {
      result.reason = radius > captureRadius ? 'outside-capture-radius' : 'coincident';
      return result;
    }
      const multiplier = galaxyLocalGravityMultiplier(star, opts);
      const gravitationalParameter = galaxySystemGravityConstant(star, opts.gravity,
      opts.localGravitySetting, true)
      * multiplier * finitePositive(star.gravity_mass, 1, 1000);
    const softening = Math.max(0.1, Number(opts.softening) || 8);
    const denominator = Math.pow(radius * radius + softening * softening, 1.5);
    const sampledInwardAcceleration = denominator > 0
      ? gravitationalParameter * radius / denominator : 0;
    /* Capture must insert at a speed the live local solver can actually sustain. The force
       path applies this same per-system acceleration ceiling; deriving release speed from the
       uncapped field otherwise creates a nominally circular orbit that immediately decays. */
    const explicitAccelerationCap = Number.isFinite(Number(opts.localAccelerationCap))
      ? Math.max(0, Number(opts.localAccelerationCap))
      : Number.isFinite(Number(opts.accelerationCap))
        ? Math.max(0, Number(opts.accelerationCap)) : null;
    const accelerationCap = explicitAccelerationCap !== null
      ? explicitAccelerationCap : defaultGalaxySystemAccelerationCap(star, opts.gravity,
        opts.localGravitySetting, true)
        * Math.max(0.25, multiplier);
    const inwardAcceleration = accelerationCap > 0
      ? Math.min(sampledInwardAcceleration, accelerationCap) : sampledInwardAcceleration;
    const circularSpeed = Math.sqrt(Math.max(0, inwardAcceleration * radius));
    const escapeSpeed = circularSpeed * Math.SQRT2;
    const starVx = Number.isFinite(star.vx) ? star.vx : 0;
    const starVy = Number.isFinite(star.vy) ? star.vy : 0;
    const relativeVx = velocity.vx - starVx, relativeVy = velocity.vy - starVy;
    const relativeSpeed = Math.hypot(relativeVx, relativeVy);
    result.circularSpeed = circularSpeed;
    result.escapeSpeed = escapeSpeed;
    if (relativeSpeed > escapeSpeed * GALAXY_SLINGSHOT_ESCAPE_FACTOR) {
      result.escaped = true;
      result.reason = 'escape-velocity';
      return result;
    }
    const unitX = dx / radius, unitY = dy / radius;
    let direction = Math.sign(-dy * relativeVx + dx * relativeVy);
    if (!direction) direction = (seededHash(opts.layoutSeed,
      'slingshot:' + String(node.id) + '|' + String(star.id)) & 1) ? 1 : -1;
    const insertionSpeed = Math.min(GALAXY_LOCAL_RELATIVE_SPEED_LIMIT, circularSpeed);
    result.vx = starVx - unitY * insertionSpeed * direction;
    result.vy = starVy + unitX * insertionSpeed * direction;
    const absoluteSpeed = Math.hypot(result.vx, result.vy);
    if (absoluteSpeed > GALAXY_SLINGSHOT_SPEED_LIMIT) {
      const scale = GALAXY_SLINGSHOT_SPEED_LIMIT / absoluteSpeed;
      result.vx *= scale; result.vy *= scale;
    }
    result.captured = true;
    result.reason = explicitId ? 'authored-anchor' : 'authored-community';
    return result;
  }

  /* History ghosts are intentionally massless: they never enter community COMs, gravity,
     contacts, or recoil.  They are nevertheless painted by default, so a frozen historical
     marker is visually indistinguishable from a broken galaxy.  Advance each as an exact
     test particle in the same cached core+halo potential used by live systems.  Holding its
     sampled radius constant is deliberate: it gives the dim history layer a calm, bounded
     black-hole sweep without feeding any energy back into the evidence simulation. */
  function integrateGalaxyGhostOrbits(nodes, options) {
    const opts = options || {};
    const ghosts = (nodes || []).filter(node => node && node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    if (!ghosts.length || !bodies.length) return { ghosts: ghosts.length, advanced: 0 };
    const centralSoftening = Math.max(0.1, Number(opts.centralSoftening) || opts.softening || 40);
    const field = galaxyBlackHoleField(bodies, Object.assign({}, opts, { softening: centralSoftening }));
    const anchor = field.anchor && field.anchor.anchor_role === 'global' ? field.anchor : null;
    if (!anchor || !(field.gravitationalConstant > 0)) {
      return { ghosts: ghosts.length, advanced: 0 };
    }
    const envelope = galaxyFarFieldEnvelope(bodies, opts);
    const timestep = Math.max(0.001, Math.min(2, Number(opts.timestep) || 1));
    const direction = (seededHash(opts.layoutSeed, 'galaxy-spin') & 1) ? 1 : -1;
    const anchorRadius = finitePositive(anchor.radius,
      finitePositive(anchor.visual_radius, 3, 160), 160);
    let advanced = 0;
    ghosts.forEach(node => {
      const ghostRadius = finitePositive(node.radius,
        finitePositive(node.visual_radius, 2.5, 64), 64);
      const inner = anchorRadius + ghostRadius + GALAXY_BLACK_HOLE_EXCLUSION_PADDING;
      const outer = Math.max(inner, (Number(envelope.envelopeRadius) || inner) - ghostRadius);
      let radius = Number(node.__galaxyGhostOrbitRadius);
      if (!(Number.isFinite(radius) && radius >= inner && radius <= outer)) {
        radius = Math.max(inner, Math.min(outer, Math.hypot(node.x - anchor.x, node.y - anchor.y)));
        if (!(radius > 1e-9)) radius = inner;
        Object.defineProperty(node, '__galaxyGhostOrbitRadius', {
          value: radius, writable: true, configurable: true, enumerable: false,
        });
      }
      let angle = Math.atan2(node.y - anchor.y, node.x - anchor.x);
      if (!Number.isFinite(angle)) {
        angle = (seededHash(opts.layoutSeed, 'ghost-orbit:' + String(node.id)) / 0x100000000)
          * Math.PI * 2;
      }
      const omega = galaxyCarrierTargetSpeed(field, radius, opts.orbitalSpeed)
        / Math.max(1e-6, radius);
      angle += direction * omega * timestep;
      node.x = anchor.x + Math.cos(angle) * radius;
      node.y = anchor.y + Math.sin(angle) * radius;
      const speed = omega * radius;
      node.vx = -Math.sin(angle) * speed * direction;
      node.vy = Math.cos(angle) * speed * direction;
      Object.defineProperty(node, '__galaxyGhostOrbitSeeded', {
        value: true, writable: true, configurable: true, enumerable: false,
      });
      advanced++;
    });
    return { ghosts: ghosts.length, advanced };
  }

  /* Complete/oversized Galaxy views deliberately bypass the O(n²) live solver. They still
     need to look alive: a static galaxy with thousands of painted bodies reads as a failure,
     not as a performance policy. This O(n) clock advances cached hierarchical phases exactly:
     each dominant star sweeps the black hole, then each satellite sweeps that star. It is
     kinematic only—no mass, contact, link, or recoil is introduced into the evidence model. */
  function advanceGalaxyKinematicLocalMembers(members, carrier, carrierTarget, options) {
    const opts = options || {};
    const orbitalSpeed = galaxyOrbitalSpeedMultiplier(opts.orbitalSpeed);
    const absoluteSpeedLimit = Math.max(0.01, Number(opts.speedLimit) || MAX_NODE_SPEED);
    const orbitalRadius = galaxyOrbitalRadiusMultiplier(opts.orbitalSpeed);
    const localSoftening = Math.max(0.1, Number(opts.localSoftening) || opts.softening || 40);
    const timestep = Math.max(0.001, Math.min(2, Number(opts.timestep) || 1));
    const localOrbitCache = opts.localOrbitCache || '__galaxyKinematicLocalOrbit';
    const nodeRadius = node => finitePositive(node.radius,
      finitePositive(node.visual_radius, 3, 160), 160);
    const byId = new Map((members || []).map(node => [String(node.id), node]));
    const targets = new Map([[carrier, carrierTarget]]);
    const visiting = new Set();
    let satellites = 0;
    const visit = node => {
      if (!node || node === carrier) return carrierTarget;
      const existingTarget = targets.get(node);
      if (existingTarget) return existingTarget;
      if (visiting.has(node)) return carrierTarget;
      visiting.add(node);
      const parent = galaxyLocalOrbitParent(node, members, carrier, byId) || carrier;
      const parentTarget = visit(parent);
      const parentId = String(parent.id);
      const parentX = Number.isFinite(parent.x) ? parent.x : 0;
      const parentY = Number.isFinite(parent.y) ? parent.y : 0;
      const currentRadius = Math.hypot(node.x - parentX, node.y - parentY);
      const minimumRadius = nodeRadius(parent) + nodeRadius(node)
        + GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING;
      let local = node[localOrbitCache];
      if (!local || local.anchorId !== parentId) {
        local = setGalaxyKinematicPhase(node, localOrbitCache, {
          anchorId: parentId,
          baseRadius: Math.max(minimumRadius,
            finitePositive(node.__galaxyOrbitBaseRadius, currentRadius, Infinity)),
          radius: Math.max(minimumRadius, currentRadius),
          angle: currentRadius > 1e-9
            ? Math.atan2(node.y - parentY, node.x - parentX)
            : seededHash(opts.layoutSeed, 'kinematic-local:' + String(node.id))
              / 0x100000000 * Math.PI * 2,
          direction: (seededHash(opts.layoutSeed, 'system:' + parentId) & 1) ? 1 : -1,
        });
      }
      if (!Number.isFinite(local.angle)) local.angle = seededHash(
        opts.layoutSeed, 'kinematic-local:' + String(node.id)) / 0x100000000 * Math.PI * 2;
      if (!(Number.isFinite(Number(local.baseRadius)) && Number(local.baseRadius) > 0)) {
        local.baseRadius = Math.max(minimumRadius, Number(local.radius) || currentRadius || 1);
      }
      const localRadius = Math.max(minimumRadius, local.baseRadius * orbitalRadius);
      local.radius = localRadius;
      const authoredHierarchy = galaxyHasAuthoredParent(node, parent);
      const localGravityMultiplier = galaxyLocalGravityMultiplier(parent, opts);
      const localGravity = galaxySystemGravityConstant(parent, opts.gravity,
        opts.localGravitySetting, authoredHierarchy)
        * localGravityMultiplier;
      const denominator = Math.pow(localRadius * localRadius + localSoftening * localSoftening, 1.5);
      const rawAcceleration = localGravity * finitePositive(parent.gravity_mass, 1, 1000)
        * localRadius / Math.max(1e-9, denominator);
      const acceleration = Math.min(
        defaultGalaxySystemAccelerationCap(parent, opts.gravity, opts.localGravitySetting,
          authoredHierarchy)
          * Math.max(0.25, localGravityMultiplier), rawAcceleration);
      const omega = Math.min(
        Math.sqrt(Math.max(0, acceleration / localRadius)) * orbitalSpeed,
        GALAXY_LOCAL_RELATIVE_SPEED_LIMIT * orbitalSpeed / localRadius);
      const requestedLocalSpeed = omega * localRadius;
      const localTangentX = -Math.sin(local.angle) * local.direction;
      const localTangentY = Math.cos(local.angle) * local.direction;
      const phaseSpeed = Math.min(
        galaxyRelativeSpeedBudget(parentTarget, absoluteSpeedLimit,
          requestedLocalSpeed, localTangentX, localTangentY),
        galaxyRelativeSpeedBudget(parentTarget, absoluteSpeedLimit, requestedLocalSpeed),
      );
      const cappedOmega = phaseSpeed / Math.max(1e-9, localRadius);
      local.angle += local.direction * cappedOmega * timestep;
      const offsetX = Math.cos(local.angle) * localRadius;
      const offsetY = Math.sin(local.angle) * localRadius;
      const advancedTangentX = -Math.sin(local.angle) * local.direction;
      const advancedTangentY = Math.cos(local.angle) * local.direction;
      const target = {
        x: parentTarget.x + offsetX,
        y: parentTarget.y + offsetY,
        vx: parentTarget.vx + advancedTangentX * phaseSpeed,
        vy: parentTarget.vy + advancedTangentY * phaseSpeed,
      };
      targets.set(node, target);
      visiting.delete(node);
      satellites++;
      return target;
    };
    (members || []).forEach(node => { if (node !== carrier) visit(node); });
    targets.forEach((target, node) => {
      if (node === carrier) return;
      node.x = target.x; node.y = target.y; node.vx = target.vx; node.vy = target.vy;
      if (Number.isFinite(node.fx)) node.fx = target.x;
      if (Number.isFinite(node.fy)) node.fy = target.y;
    });
    return { targets, satellites };
  }

  function setGalaxyKinematicPhase(node, name, value) {
    try {
      Object.defineProperty(node, name, {
        value, writable: true, configurable: true, enumerable: false,
      });
    } catch (error) { node[name] = value; }
    return value;
  }

  function advanceGalaxyKinematicOrbits(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const empty = { bodies: bodies.length, systems: 0, satellites: 0,
      systemPacking: { systems: 0, overlaps: 0, adjustedSystems: 0,
        remainingOverlaps: 0, infeasiblePairs: 0, gap: 0 },
      ghostOrbit: { ghosts: 0, advanced: 0 } };
    if (!bodies.length) return empty;
    const centralSoftening = Math.max(0.1,
      Number(opts.centralSoftening) || opts.softening || 40);
    const localSoftening = Math.max(0.1,
      Number(opts.localSoftening) || opts.softening || 40);
    const field = galaxyBlackHoleField(bodies, Object.assign({}, opts, { softening: centralSoftening }));
    const anchor = field.anchor && field.anchor.anchor_role === 'global' ? field.anchor : null;
    if (!anchor) return empty;
    const globalFieldActive = field.gravitationalConstant > 0;
    const timestep = Math.max(0.001, Math.min(2, Number(opts.timestep) || 1));
    const orbitalRadius = galaxyOrbitalRadiusMultiplier(opts.orbitalSpeed);
    const absoluteSpeedLimit = Math.max(0.01, Number(opts.speedLimit) || MAX_NODE_SPEED);
    const direction = (seededHash(opts.layoutSeed, 'galaxy-spin') & 1) ? 1 : -1;
    const envelope = galaxyFarFieldEnvelope(bodies, opts);
    const nodeRadius = node => finitePositive(node.radius,
      finitePositive(node.visual_radius, 3, 160), 160);
    const setPhase = (node, name, value) => {
      try {
        Object.defineProperty(node, name, {
          value, writable: true, configurable: true, enumerable: false,
        });
      } catch (error) { node[name] = value; }
      return value;
    };
    const moveNode = (node, x, y, vx, vy) => {
      node.x = x; node.y = y; node.vx = vx; node.vy = vy;
      if (Number.isFinite(node.fx)) node.fx = x;
      if (Number.isFinite(node.fy)) node.fy = y;
    };
    const angularFrequency = (radius, authoredCarrier) => {
      const requestedSpeed = authoredCarrier
        ? galaxyAuthoredCarrierTargetSpeed(field, radius, opts.orbitalSpeed)
        : galaxyCarrierTargetSpeed(field, radius, opts.orbitalSpeed);
      /* The carrier is the parent frame for every local orbit. Cap it before
         constructing that frame, otherwise a high authored clock can make
         the child speed budget infeasible and scatter the local system. */
      const speed = Math.min(absoluteSpeedLimit, Math.max(0, requestedSpeed));
      return speed / Math.max(1e-6, radius);
    };
    const boundedRadius = (radius, extent) => {
      const inner = nodeRadius(anchor) + Math.max(0, extent)
        + GALAXY_BLACK_HOLE_EXCLUSION_PADDING;
      const outer = Math.max(inner, (Number(envelope.envelopeRadius) || inner) - Math.max(0, extent));
      return Math.max(inner, Math.min(outer, radius));
    };
    let systems = 0, satellites = 0;
    field.systems.forEach(item => {
      const members = item.nodes;
      if (!members.length || members.some(node => node.id === opts.fixedNodeId)) return;
      const star = item.carrier;
      if (!star) return;
      /* The star, rather than the changing system COM, owns both hierarchy frames. Its cached
         black-hole phase is unaffected by the current distribution of planets, and its local
         position never receives an opposite barycentric wobble. */
      const extent = members.reduce((maximum, node) => Math.max(maximum,
        Math.hypot(node.x - star.x, node.y - star.y) + nodeRadius(node)), 0);
      const starRadius = Math.hypot(star.x - anchor.x, star.y - anchor.y);
      const orbitCache = item.core
        ? '__galaxyKinematicCoreOrbit' : '__galaxyKinematicGlobalOrbit';
      let orbit = star[orbitCache];
      if (!orbit || orbit.anchorId !== String(anchor.id) || orbit.systemId !== String(item.id)) {
        const seededRadius = item.core ? Number(star.__galaxyCoreLaneRadius) : NaN;
        const initialRadius = Number.isFinite(seededRadius) && seededRadius > 0
          ? seededRadius : starRadius;
        orbit = setPhase(star, orbitCache, {
          anchorId: String(anchor.id), systemId: String(item.id),
          baseRadius: boundedRadius(initialRadius, extent),
          radius: boundedRadius(initialRadius, extent),
          angle: Math.atan2(star.y - anchor.y, star.x - anchor.x),
        });
      }
      if (!(Number.isFinite(Number(orbit.baseRadius)) && Number(orbit.baseRadius) > 0)) {
        orbit.baseRadius = Number(orbit.radius) || starRadius;
      }
      /* At the zero global-gravity endpoint the black-hole carrier is stationary. Preserve its
         current radius so local stellar members can keep orbiting that fixed carrier instead of
         receiving a one-time radial resize from the orbital-speed presentation multiplier. */
      orbit.radius = globalFieldActive
        ? boundedRadius(orbit.baseRadius * orbitalRadius, extent * orbitalRadius)
        : boundedRadius(Number(orbit.radius) || starRadius, extent);
      if (!Number.isFinite(orbit.angle)) {
        orbit.angle = seededHash(opts.layoutSeed, 'kinematic-system:' + item.id)
          / 0x100000000 * Math.PI * 2;
      }
      const omega = angularFrequency(orbit.radius, !item.core);
      orbit.angle += direction * omega * timestep;
      if (item.core) {
        setPhase(star, '__galaxyCoreLaneRadius', orbit.radius);
        setPhase(star, '__galaxyCoreLaneAngle', orbit.angle);
        if (star.anchor_role === 'community') {
          setPhase(star, '__galaxyKinematicGlobalOrbit', {
            anchorId: String(anchor.id), systemId: String(item.id),
            radius: orbit.radius, angle: orbit.angle,
          });
        }
      }
      const targetX = anchor.x + Math.cos(orbit.angle) * orbit.radius;
      const targetY = anchor.y + Math.sin(orbit.angle) * orbit.radius;
      const globalSpeed = omega * orbit.radius;
      const globalVx = -Math.sin(orbit.angle) * globalSpeed * direction;
      const globalVy = Math.cos(orbit.angle) * globalSpeed * direction;
      moveNode(star, targetX, targetY, globalVx, globalVy);
      const localMotion = advanceGalaxyKinematicLocalMembers(members, star, {
        x: targetX, y: targetY, vx: globalVx, vy: globalVy,
      }, item.core ? Object.assign({}, opts, {
        localOrbitCache: '__galaxyKinematicCoreLocalOrbit',
      }) : opts);
      satellites += localMotion.satellites;
      const carrierContact = nodeRadius(anchor) + nodeRadius(star)
        + GALAXY_BLACK_HOLE_EXCLUSION_PADDING;
      const carrierOuter = galaxyEventHorizonOuterRadius(
        nodeRadius(anchor), carrierContact, GALAXY_EVENT_HORIZON_INFLUENCE_SCALE);
      const systemWarp = Math.max(0, Math.min(1,
        (carrierOuter - orbit.radius) / Math.max(1e-9, carrierOuter - carrierContact)));
      members.forEach(node => setGalaxySpacetimeWarp(node, galaxySmoothstep(systemWarp)));
      systems++;
    });
    const systemPacking = opts.includeSystemPacking === true
      ? applyGalaxySystemPacking(bodies, Object.assign({}, opts, {
        gap: opts.systemPackingGap,
        strength: opts.systemPackingStrength,
        maxCorrection: opts.systemPackingMaxCorrection,
        fixedNodeId: opts.fixedNodeId,
        updateKinematicPhase: true,
      }))
      : { systems: 0, overlaps: 0, adjustedSystems: 0, remainingOverlaps: 0,
        infeasiblePairs: 0, gap: 0 };
    const blackHoleSpinAngle = advanceGalaxyBlackHoleSpin(nodes, opts);
    return { bodies: bodies.length, systems, satellites, systemPacking,
      blackHoleSpinAngle, ghostOrbit: integrateGalaxyGhostOrbits(nodes, opts) };
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
    const localGravitySetting = galaxyLocalGravitySetting(opts.gravity,
      opts.localGravitySetting);
    const gravity = galaxyLocalGravityConstant(localGravitySetting) * gravityMultiplier;
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
    const localGravitySetting = galaxyLocalGravitySetting(opts.gravity,
      opts.localGravitySetting);
    const gravity = galaxyLocalGravityConstant(localGravitySetting)
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
    const phaseAdvances = new Map();
    const phaseAdvanceLimits = new Map();
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
            let minimumDistance = left.radius + right.radius + pairPadding;
            let preservedOrbitPair = null;
            /* Same-star planets are constrained to circular manifolds. A large Repel padding can
               demand a centre distance greater than those two circles can ever supply (the
               release moon fixture requested 46 on two 19.2-radius orbits whose absolute maximum
               chord is 38.4). Do not run a permanent correction against impossible geometry.
               Clamp the target to the maximum feasible chord, then solve the remaining chord
               deficit as a bounded forward angular advance below. */
            if (!crossCommunity && opts.preserveSystemRadii === true && leftGroup.anchor) {
              const anchor = leftGroup.anchor;
              const explicitAnchorId = anchor.id === undefined || anchor.id === null
                ? '' : String(anchor.id);
              const explicitlyAnchored = explicitAnchorId
                && [left.node, right.node].every(node => node.system_anchor_id !== undefined
                  && node.system_anchor_id !== null
                  && String(node.system_anchor_id) === explicitAnchorId);
              if (explicitlyAnchored && left.node !== anchor && right.node !== anchor) {
                const leftOrbit = Math.hypot(left.node.x - anchor.x, left.node.y - anchor.y);
                const rightOrbit = Math.hypot(right.node.x - anchor.x, right.node.y - anchor.y);
                if (leftOrbit > 1e-9 && rightOrbit > 1e-9) {
                  const maximumChord = (leftOrbit + rightOrbit) * (1 - 1e-6);
                  minimumDistance = Math.min(minimumDistance, maximumChord);
                  preservedOrbitPair = { anchor, leftOrbit, rightOrbit };
                }
              }
            }
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
            if (preservedOrbitPair && !leftFixed && !rightFixed) {
              const anchor = preservedOrbitPair.anchor;
              const leftDx = left.node.x - anchor.x, leftDy = left.node.y - anchor.y;
              const rightDx = right.node.x - anchor.x, rightDy = right.node.y - anchor.y;
              const leftAngle = Math.atan2(leftDy, leftDx);
              const rightAngle = Math.atan2(rightDy, rightDx);
              const tangentDirection = (node, dx, dy, radius) => {
                const relativeVx = (Number.isFinite(node.vx) ? node.vx : 0)
                  - (Number.isFinite(anchor.vx) ? anchor.vx : 0);
                const relativeVy = (Number.isFinite(node.vy) ? node.vy : 0)
                  - (Number.isFinite(anchor.vy) ? anchor.vy : 0);
                return Math.sign((-dy * relativeVx + dx * relativeVy) / radius);
              };
              const leftDirection = tangentDirection(
                left.node, leftDx, leftDy, preservedOrbitPair.leftOrbit);
              const rightDirection = tangentDirection(
                right.node, rightDx, rightDy, preservedOrbitPair.rightOrbit);
              const direction = leftDirection && leftDirection === rightDirection
                ? leftDirection : (leftDirection || rightDirection || 1);
              const cosine = Math.max(-1, Math.min(1,
                (preservedOrbitPair.leftOrbit * preservedOrbitPair.leftOrbit
                  + preservedOrbitPair.rightOrbit * preservedOrbitPair.rightOrbit
                  - minimumDistance * minimumDistance)
                / (2 * preservedOrbitPair.leftOrbit * preservedOrbitPair.rightOrbit)));
              const requiredAngle = Math.acos(cosine);
              const fullTurn = Math.PI * 2;
              const directedGap = ((direction * (rightAngle - leftAngle)) % fullTurn
                + fullTurn) % fullTurn;
              const currentAngle = Math.min(directedGap, fullTurn - directedGap);
              const deficit = Math.max(0, requiredAngle - currentAngle);
              if (deficit > 1e-12) {
                /* Advance whichever body already leads in the common orbital direction. Moving
                   the trailer backward would satisfy the contact but visibly reverse a planet. */
                const leading = directedGap <= Math.PI ? right.node : left.node;
                const previous = Number(phaseAdvances.get(leading)) || 0;
                /* An isolated star/planet/moon contact can spend the larger phase budget without
                   interacting with another planet. Dense systems share the conservative release
                   budget so simultaneous contacts cannot aggregate into a visible jump. */
                const maximumDirectPhase = leftGroup.nodes.length <= 3 ? 0.158 : 0.072;
                const advance = Math.min(deficit * pairStrength, maximumDirectPhase);
                phaseAdvances.set(leading, direction * Math.min(
                  maximumDirectPhase, Math.abs(previous) + advance));
                phaseAdvanceLimits.set(leading, maximumDirectPhase);
              }
              contacts.push({
                left: left.node, right: right.node, oldDistance: distance,
                leftInverseMass, rightInverseMass, inverseMass,
              });
              stats.correctionDistance += correction;
              stats.overlaps++;
              return;
            }
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
       The dominant star is the system's external local frame and stays exact while its planets
       move along their circles. A pointer-owned satellite and compatibility systems keep the
       legacy Cartesian projection. Cross-system pressure remains a rigid group translation. */
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
      const externallyFixedAnchor = !fixedMember || fixedMember === anchor;
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
        const directPhase = Number(phaseAdvances.get(node)) || 0;
        let arc = shift.x * tangentX + shift.y * tangentY + directPhase * radius;
        const relativeVx = (Number.isFinite(node.vx) ? node.vx : 0)
          - (Number.isFinite(anchor.vx) ? anchor.vx : 0);
        const relativeVy = (Number.isFinite(node.vy) ? node.vy : 0)
          - (Number.isFinite(anchor.vy) ? anchor.vy : 0);
        const orbitalDirection = Math.sign(relativeVx * tangentX + relativeVy * tangentY);
        /* Contact pressure may advance a planet along its established orbit, but it must never
           step backward through the stationary-star frame. Blocking only the opposing arc keeps
           dense separation dissipative without altering radius or manufacturing phase reversal. */
        if (orbitalDirection && arc * orbitalDirection < 0) {
          arc = 0;
        }
        /* A contact correction is not an orbital clock. Ordinary projected pressure stays below
           the 0.085-rad release gate; the explicit chord-deficit solve may use the larger bounded
           advance needed to clear a deeply overlapping moon within 16 fixed slices. */
        const maximumPhase = directPhase
          ? (phaseAdvanceLimits.get(node) || 0.072) : 0.072;
        arc = Math.sign(arc) * Math.min(Math.abs(arc), radius * maximumPhase);
        return {
          node, mass, radius, angle: Math.atan2(dy, dx),
          arc,
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
       next kick. Apply the identical signed angle to each planet's velocity in the same
       stationary star frame. The dominant star absorbs no local position or velocity correction;
       black-hole-frame translation remains independent. */
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

  /* Build one conservative painted circle per independent solar system.  The dominant star is
     the circle centre and every member contributes its complete painted edge.  Using the star
     rather than the evidence-mass COM is load-bearing: a lopsided planetary system may have a
     displaced COM, but translating this envelope still leaves every local radius and phase
     exactly unchanged. */
  function galaxySystemEnvelopes(nodes, options) {
    const opts = options || {};
    const envelopePadding = Math.max(0, Number(opts.envelopePadding) || 0);
    const fixedNodeId = opts.fixedNodeId === undefined || opts.fixedNodeId === null
      ? null : String(opts.fixedNodeId);
    const timestep = Math.max(0.001, Math.min(2, Number(opts.timestep) || 1));
    const bodyRadius = node => finitePositive(
      node.radius, finitePositive(node.visual_radius,
        radiusFromGravityMass(node.gravity_mass), 80), 160
    );
    const centers = galaxyOrbitGroups(nodes);
    const globalAnchor = galaxyGlobalAnchor(nodes || []);
    /* The packing model must use the same carrier hierarchy as the black-hole field. Otherwise
       a directly linked star is folded into the fixed black-hole envelope during admission even
       though runtime physics later treats that star and its descendants as an independent solar
       system. Keep the black hole itself as one fixed, anchor-only envelope. */
    const sources = globalAnchor && globalAnchor.anchor_role === 'global' ? [{
      id: String(globalAnchor.id), nodes: [globalAnchor], anchor: globalAnchor,
    }].concat(galaxyBlackHoleCarrierSystems(nodes, globalAnchor, centers).map(system => ({
      id: system.id, nodes: system.nodes, anchor: system.carrier,
    }))) : [...centers.values()].map(center => ({
      id: center.id, nodes: center.nodes, anchor: galaxySystemAnchor(center.nodes),
    }));
    return sources.map(source => {
      const members = source.nodes.slice();
      const anchor = source.anchor || galaxySystemAnchor(members);
      if (!anchor) return null;
      const radius = members.reduce((outer, node) => Math.max(outer,
        Math.hypot(node.x - anchor.x, node.y - anchor.y) + bodyRadius(node)
      ), bodyRadius(anchor)) + envelopePadding;
      const mass = members.reduce((sum, node) => sum
        + finitePositive(node.gravity_mass, 1, 1000), 0);
      const fixed = anchor.anchor_role === 'global' || members.some(node =>
        (fixedNodeId !== null && String(node.id) === fixedNodeId)
        || (opts.respectFixedCoordinates !== false
          && Number.isFinite(node.fx) && Number.isFinite(node.fy)));
      return {
        id: source.id, nodes: members, anchor,
        x: anchor.x, y: anchor.y, radius, mass, fixed,
      };
    }).filter(Boolean).sort((left, right) =>
      Number(right.fixed) - Number(left.fixed)
      || Number(right.anchor.anchor_role === 'global')
        - Number(left.anchor.anchor_role === 'global')
      || right.radius - left.radius
      || String(left.id).localeCompare(String(right.id))
    );
  }

  /* Reserve the maximum painted envelope a live local hierarchy can reach before admitting its
     carrier lane.  A flat `system.radius` is only the current snapshot: a nested moon can be
     temporarily inside its planet while its authored orbit still expands at the top slider
     setting.  The live boundary applies the same radius multiplier and slack to each edge, so
     lane admission must sum those edge bounds rather than multiply one current snapshot. */
  function galaxySystemMaximumEnvelopeRadius(system, options) {
    const opts = options || {};
    const members = system && Array.isArray(system.nodes) ? system.nodes : [];
    const anchor = system && system.anchor ? system.anchor : galaxySystemAnchor(members);
    if (!anchor) return 0;
    const byId = new Map(members.filter(node => node && node.id !== undefined)
      .map(node => [String(node.id), node]));
    const bodyRadius = node => finitePositive(
      node && node.radius, finitePositive(node && node.visual_radius,
        radiusFromGravityMass(node && node.gravity_mass), 80), 160
    );
    const maximumRadiusMultiplier = galaxyOrbitalRadiusMultiplier(
      GALAXY_ORBITAL_SPEED_MAXIMUM_SETTING
    );
    const boundarySlack = Math.max(1, Number.isFinite(Number(opts.localOrbitBoundarySlack))
      ? Number(opts.localOrbitBoundarySlack) : GALAXY_LOCAL_ORBIT_BOUNDARY_SLACK);
    const memo = new Map(), visiting = new Set();
    const edgeRadius = (node, parent) => {
      const authored = Number(node && node.orbit_radius);
      const seeded = Number(node && node.__galaxyOrbitBaseRadius);
      const current = parent && Number.isFinite(node && node.x) && Number.isFinite(node && node.y)
        && Number.isFinite(parent.x) && Number.isFinite(parent.y)
        ? Math.hypot(node.x - parent.x, node.y - parent.y) : 0;
      const base = Math.max(
        Number.isFinite(authored) && authored > 0 ? authored : 0,
        Number.isFinite(seeded) && seeded > 0 ? seeded : 0,
        current
      );
      return base * maximumRadiusMultiplier * boundarySlack;
    };
    const distanceFromAnchor = node => {
      if (!node || node === anchor) return 0;
      if (memo.has(node)) return memo.get(node);
      if (visiting.has(node)) return 0;
      visiting.add(node);
      const parent = galaxyLocalOrbitParent(node, members, anchor, byId);
      const distance = parent && parent !== node
        ? distanceFromAnchor(parent) + edgeRadius(node, parent) : edgeRadius(node, anchor);
      visiting.delete(node);
      memo.set(node, distance);
      return distance;
    };
    let maximum = bodyRadius(anchor);
    members.forEach(node => {
      if (!node || node === anchor) return;
      maximum = Math.max(maximum, distanceFromAnchor(node) + bodyRadius(node));
    });
    return Math.max(Number(system.radius) || 0, maximum);
  }

  /* Assign permanent non-intersecting radial lanes to external solar-system envelopes. Two
     circles whose carrier radii differ by at least the sum of their painted extents can never
     collide at any orbital phase, so this admission solve removes the need to teleport systems
     apart while they rotate. The chosen radius is cached on the dominant star and later calls
     only admit newly revealed systems; existing phases remain untouched. */
  function establishGalaxyCarrierLanes(nodes, options) {
    const opts = options || {};
    const gap = Math.max(0, Number.isFinite(Number(opts.gap))
      ? Number(opts.gap) : GALAXY_SYSTEM_PACKING_GAP);
    const anchor = galaxyGlobalAnchor(nodes || []);
    const systems = galaxySystemEnvelopes(nodes, Object.assign({}, opts, {
      respectFixedCoordinates: false,
    })).filter(system => anchor && !system.nodes.includes(anchor));
    const stats = { systems: systems.length, assigned: 0, moved: 0, maximumShift: 0 };
    if (!anchor || anchor.anchor_role !== 'global' || !systems.length) return stats;
    const coreEnvelope = galaxySystemEnvelopes(nodes, Object.assign({}, opts, {
      respectFixedCoordinates: false,
    })).find(system => system.nodes.includes(anchor));
    const maximumExtents = new Map(systems.map(system => [
      system, galaxySystemMaximumEnvelopeRadius(system, opts),
    ]));
    systems.sort((left, right) => maximumExtents.get(right) - maximumExtents.get(left)
      || String(left.id).localeCompare(String(right.id)));
    const coreRadius = Math.max(finitePositive(anchor.radius,
      evidenceNodeRadius(anchor, 3), 160), coreEnvelope ? coreEnvelope.radius : 0);
    let cursor = 0, previousLaneRadius = coreRadius, previousLaneExtent = 0, laneIndex = 0;
    while (cursor < systems.length) {
      /* Reserve the maximum nested local envelope, then keep a small independent lane margin.
         This remains collision-free when the orbital-speed control reaches its maximum. */
      const laneSlack = GALAXY_CARRIER_LANE_SLACK;
      const laneExtent = maximumExtents.get(systems[cursor]) * laneSlack;
      let laneRadius = Math.max(coreRadius + laneExtent + gap
        + GALAXY_BLACK_HOLE_EXCLUSION_PADDING,
      previousLaneRadius + previousLaneExtent + laneExtent + gap);
      /* Use the exact chord, not circumference approximation, to find how many conservative
         maximum extents fit on this ring. Larger outer rings naturally carry more systems. */
      let capacity = 1;
      while (capacity < systems.length - cursor) {
        const nextCapacity = capacity + 1;
        const chord = 2 * laneRadius * Math.sin(Math.PI / nextCapacity);
        if (chord < laneExtent * 2 + gap - 1e-9) break;
        capacity = nextCapacity;
      }
      const count = Math.min(capacity, systems.length - cursor);
      const phaseOffset = seededHash(opts.layoutSeed,
        'carrier-ring:' + String(laneIndex)) / 0x100000000 * Math.PI * 2;
      for (let slot = 0; slot < count; slot++) {
        const system = systems[cursor + slot];
        /* Re-evaluate with the largest member of the next lane only; sorting makes every
           remaining extent no larger than this ring's conservative laneExtent. */
        const angle = phaseOffset + slot * Math.PI * 2 / count;
        const unitX = Math.cos(angle), unitY = Math.sin(angle);
      const shiftX = anchor.x + unitX * laneRadius - system.x;
      const shiftY = anchor.y + unitY * laneRadius - system.y;
      if (Math.hypot(shiftX, shiftY) > 1e-9) {
        system.nodes.forEach(node => { node.x += shiftX; node.y += shiftY; });
        stats.moved++;
        stats.maximumShift = Math.max(stats.maximumShift, Math.hypot(shiftX, shiftY));
      }
      try {
        Object.defineProperty(system.anchor, '__galaxyCarrierLaneRadius', {
          value: laneRadius, writable: true, configurable: true, enumerable: false,
        });
        Object.defineProperty(system.anchor, '__galaxyCarrierLaneBaseRadius', {
          value: laneRadius, writable: true, configurable: true, enumerable: false,
        });
        Object.defineProperty(system.anchor, '__galaxyCarrierLaneAngle', {
          value: angle, writable: true, configurable: true, enumerable: false,
        });
        Object.defineProperty(system.anchor, '__galaxyCarrierLaneManaged', {
          value: true, writable: true, configurable: true, enumerable: false,
        });
      } catch (error) {
        system.anchor.__galaxyCarrierLaneRadius = laneRadius;
        system.anchor.__galaxyCarrierLaneBaseRadius = laneRadius;
        system.anchor.__galaxyCarrierLaneAngle = angle;
        system.anchor.__galaxyCarrierLaneManaged = true;
      }
      stats.assigned++;
      }
      cursor += count;
      previousLaneRadius = laneRadius;
      previousLaneExtent = laneExtent;
      laneIndex++;
    }
    stats.lanes = laneIndex;
    stats.outerRadius = previousLaneRadius + previousLaneExtent;
    return stats;
  }

  /* Deterministic rigid carrier-frame packing.  A sequential golden-angle search finds a clear
     target for each complete system envelope; the live response moves only a bounded fraction
     toward that target.  No member velocity is changed, so packing cannot inject heat or alter
     total momentum, and a star-relative planet vector survives bit-for-bit apart from ordinary
     floating-point translation.  Direct/bootstrap callers may pass strength=1 and an infinite
     maxCorrection to complete the same solve in one call. */
  function applyGalaxySystemPacking(nodes, options) {
    const opts = options || {};
    const gap = Math.max(0, Number.isFinite(Number(opts.gap))
      ? Number(opts.gap) : GALAXY_SYSTEM_PACKING_GAP);
    const strength = Math.max(0, Math.min(1, Number.isFinite(Number(opts.strength))
      ? Number(opts.strength) : GALAXY_SYSTEM_PACKING_STRENGTH));
    const requestedMaximum = Number(opts.maxCorrection);
    const maximumCorrection = Number.isFinite(requestedMaximum)
      ? Math.max(0, requestedMaximum) : (opts.maxCorrection === Infinity
        ? Infinity : GALAXY_SYSTEM_PACKING_MAX_CORRECTION);
    const maximumAttempts = Math.max(32, Math.min(16384,
      Number.isFinite(Number(opts.maximumAttempts)) ? Number(opts.maximumAttempts) : 4096));
    const envelopes = galaxySystemEnvelopes(nodes, opts);
    /* Standalone bootstrap packing intentionally has open space. The finite annulus belongs to
       the live/kinematic solver and is opt-in here through its explicit confinement option. */
    const boundaryField = opts.includeFarFieldConfinement === true
      ? galaxyFarFieldEnvelope(nodes, opts) : null;
    const boundaryAnchor = boundaryField && boundaryField.anchor
      && boundaryField.anchor.anchor_role === 'global' ? boundaryField.anchor : null;
    const boundaryAnchorRadius = boundaryAnchor && boundaryField
      ? boundaryField.bodyRadius(boundaryAnchor) : 0;
    const boundaryPadding = Math.max(0,
      Number.isFinite(Number(opts.blackHoleExclusionPadding))
        ? Number(opts.blackHoleExclusionPadding) : GALAXY_BLACK_HOLE_EXCLUSION_PADDING);
    const stats = {
      systems: envelopes.length, pairs: 0, overlaps: 0, adjustedSystems: 0,
      correctionDistance: 0, maximumShift: 0, remainingOverlaps: 0,
      infeasiblePairs: 0, boundaryViolations: 0,
      minimumBlackHoleClearance: null, minimumOuterClearance: null,
      envelopeRadius: boundaryField ? boundaryField.envelopeRadius : 0, gap,
    };
    if (envelopes.length < 2 || !(strength > 0) || !(maximumCorrection > 0)) return stats;
    const occupied = [];
    const maximumEnvelopeRadius = envelopes.reduce((maximum, system) =>
      Math.max(maximum, system.radius), 0);
    const cellSize = Math.max(1, maximumEnvelopeRadius * 2 + gap);
    const occupiedGrid = new Map();
    const targets = new Map();
    const goldenAngle = Math.PI * (3 - Math.sqrt(5));
    const boundaryRange = system => {
      if (!boundaryAnchor || system.nodes.includes(boundaryAnchor)) return null;
      return {
        minimum: boundaryAnchorRadius + system.radius + boundaryPadding,
        maximum: Math.max(0, boundaryField.envelopeRadius - system.radius),
      };
    };
    const projectIntoBoundary = (system, x, y, salt) => {
      const range = boundaryRange(system);
      if (!range || !(range.maximum >= range.minimum)) return { x, y, feasible: !range };
      const dx = x - boundaryAnchor.x, dy = y - boundaryAnchor.y;
      const distance = Math.hypot(dx, dy);
      let unitX, unitY;
      if (distance > 1e-9) {
        unitX = dx / distance;
        unitY = dy / distance;
      } else {
        const angle = seededHash(0, 'system-pack-boundary:' + String(system.id)
          + ':' + String(salt || 0)) / 0x100000000 * Math.PI * 2;
        unitX = Math.cos(angle);
        unitY = Math.sin(angle);
      }
      const boundedDistance = Math.max(range.minimum, Math.min(range.maximum, distance));
      return {
        x: boundaryAnchor.x + unitX * boundedDistance,
        y: boundaryAnchor.y + unitY * boundedDistance,
        feasible: true,
      };
    };
    const insideBoundary = (system, x, y) => {
      const range = boundaryRange(system);
      if (!range) return true;
      if (!(range.maximum >= range.minimum)) return false;
      const distance = Math.hypot(x - boundaryAnchor.x, y - boundaryAnchor.y);
      return distance >= range.minimum - 1e-9 && distance <= range.maximum + 1e-9;
    };
    const clearAt = (system, x, y) => {
      if (!insideBoundary(system, x, y)) return false;
      const cellX = Math.floor(x / cellSize), cellY = Math.floor(y / cellSize);
      const reach = Math.max(1, Math.ceil(
        (system.radius + maximumEnvelopeRadius + gap) / cellSize));
      for (let offsetX = -reach; offsetX <= reach; offsetX++) {
        for (let offsetY = -reach; offsetY <= reach; offsetY++) {
          const bucket = occupiedGrid.get(
            (cellX + offsetX) + ',' + (cellY + offsetY)) || [];
          for (const other of bucket) {
            stats.pairs++;
            if (Math.hypot(x - other.x, y - other.y)
              < system.radius + other.radius + gap - 1e-9) return false;
          }
        }
      }
      return true;
    };
    envelopes.forEach(system => {
      const initialTarget = system.fixed
        ? { x: system.x, y: system.y, feasible: insideBoundary(system, system.x, system.y) }
        : projectIntoBoundary(system, system.x, system.y, 0);
      let targetX = initialTarget.x, targetY = initialTarget.y;
      const initiallyClear = clearAt(system, targetX, targetY);
      if (!initiallyClear && !system.fixed) {
        stats.overlaps++;
        const seedAngle = seededHash(0, 'system-pack:' + String(system.id))
          / 0x100000000 * Math.PI * 2;
        const radialStep = Math.max(4, system.radius + gap * 0.5);
        let found = false;
        for (let attempt = 1; attempt <= maximumAttempts; attempt++) {
          const reach = radialStep * Math.sqrt(attempt);
          const angle = seedAngle + goldenAngle * attempt;
          const projected = projectIntoBoundary(system,
            system.x + Math.cos(angle) * reach,
            system.y + Math.sin(angle) * reach, attempt);
          if (!projected.feasible) continue;
          const candidateX = projected.x, candidateY = projected.y;
          if (!clearAt(system, candidateX, candidateY)) continue;
          targetX = candidateX;
          targetY = candidateY;
          found = true;
          break;
        }
        if (!found) stats.infeasiblePairs++;
      } else if (!initiallyClear && system.fixed) {
        /* Multiple fixed/pointer-owned systems cannot be separated without violating explicit
           ownership.  Keep them exact and report the unresolved geometry to diagnostics. */
        stats.overlaps++;
        stats.infeasiblePairs++;
      }
      targets.set(system, { x: targetX, y: targetY });
      const occupiedSystem = { x: targetX, y: targetY, radius: system.radius, system };
      occupied.push(occupiedSystem);
      const cellKey = Math.floor(targetX / cellSize) + ',' + Math.floor(targetY / cellSize);
      if (!occupiedGrid.has(cellKey)) occupiedGrid.set(cellKey, []);
      occupiedGrid.get(cellKey).push(occupiedSystem);
    });
    envelopes.forEach(system => {
      if (system.fixed) return;
      const target = targets.get(system);
      let shiftX = (target.x - system.x) * strength;
      let shiftY = (target.y - system.y) * strength;
      const requested = Math.hypot(shiftX, shiftY);
      if (!(requested > 1e-12)) return;
      const scale = requested > maximumCorrection ? maximumCorrection / requested : 1;
      shiftX *= scale;
      shiftY *= scale;
      system.nodes.forEach(node => {
        node.x += shiftX;
        node.y += shiftY;
      });
      if (opts.updateKinematicPhase === true && system.anchor.__galaxyKinematicGlobalOrbit) {
        const globalAnchor = galaxyGlobalAnchor(nodes);
        if (globalAnchor && globalAnchor !== system.anchor) {
          const dx = system.anchor.x - globalAnchor.x;
          const dy = system.anchor.y - globalAnchor.y;
          system.anchor.__galaxyKinematicGlobalOrbit.radius = Math.hypot(dx, dy);
          system.anchor.__galaxyKinematicGlobalOrbit.angle = Math.atan2(dy, dx);
        }
      }
      const applied = Math.hypot(shiftX, shiftY);
      stats.adjustedSystems++;
      stats.correctionDistance += applied;
      stats.maximumShift = Math.max(stats.maximumShift, applied);
    });
    const finalEnvelopes = galaxySystemEnvelopes(nodes, opts);
    const finalGrid = new Map();
    finalEnvelopes.forEach((system, index) => {
      const range = boundaryRange(system);
      if (range) {
        const distance = Math.hypot(system.x - boundaryAnchor.x,
          system.y - boundaryAnchor.y);
        const rawBlackHoleClearance = distance - range.minimum;
        const rawOuterClearance = range.maximum - distance;
        const blackHoleClearance = Math.abs(rawBlackHoleClearance) <= 1e-10
          ? 0 : rawBlackHoleClearance;
        const outerClearance = Math.abs(rawOuterClearance) <= 1e-10
          ? 0 : rawOuterClearance;
        stats.minimumBlackHoleClearance = stats.minimumBlackHoleClearance === null
          ? blackHoleClearance : Math.min(stats.minimumBlackHoleClearance, blackHoleClearance);
        stats.minimumOuterClearance = stats.minimumOuterClearance === null
          ? outerClearance : Math.min(stats.minimumOuterClearance, outerClearance);
        if (blackHoleClearance < -1e-7 || outerClearance < -1e-7) {
          stats.boundaryViolations++;
        }
      }
      const cellX = Math.floor(system.x / cellSize), cellY = Math.floor(system.y / cellSize);
      for (let offsetX = -1; offsetX <= 1; offsetX++) {
        for (let offsetY = -1; offsetY <= 1; offsetY++) {
          const bucket = finalGrid.get(
            (cellX + offsetX) + ',' + (cellY + offsetY)) || [];
          bucket.forEach(other => {
            if (Math.hypot(system.x - other.system.x, system.y - other.system.y)
              < system.radius + other.system.radius + gap - 1e-7) {
              stats.remainingOverlaps++;
            }
          });
        }
      }
      const key = cellX + ',' + cellY;
      if (!finalGrid.has(key)) finalGrid.set(key, []);
      finalGrid.get(key).push({ system, index });
    });
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

    galaxyBlackHoleCarrierSystems(bodies, anchor).forEach(system => {
      const members = system.nodes;
      /* A dragged node is a cursor-owned external source. Rigidly translating its entire
         community when that cursor touches the horizon creates positive feedback: restore
         puts only the source back at the cursor, while every follower retains the displacement
         and inflates the next system radius. Keep the horizon strict per painted member but
         never move those followers as a group. */
      if (members.some(node => node.id === opts.fixedNodeId)) {
        members.forEach(node => {
          if (!projectIndividualNode(node)) return;
          if (system.core) stats.coreNodes++;
          else stats.fixedSystemNodes++;
        });
        return;
      }

      /* Contact uses the complete system envelope about its mass centre, then translates every
         member rigidly. This conserves the group's angular phase without ever peeling a planet
         away from a direct-BH star; the live galactic force still samples the star carrier. */
      const systemRadius = members.reduce((maximum, node) => Math.max(maximum,
        Math.hypot(node.x - system.center.x, node.y - system.center.y) + bodyRadius(node)), 0);
      const radial = radialUnit(system.id,
        system.center.x - anchorX, system.center.y - anchorY);
      const minimumDistance = anchorRadius + systemRadius + padding;
      const correction = minimumDistance - radial.distance;
      if (!(correction > 0) || !Number.isFinite(correction)) return;
      const shiftX = radial.x * correction, shiftY = radial.y * correction;
      members.forEach(node => {
        node.x += shiftX;
        node.y += shiftY;
        if (Number.isFinite(node.fx)) node.fx += shiftX;
        if (Number.isFinite(node.fy)) node.fy += shiftY;
      });
      const velocity = stabilizeSystemContactVelocity(
        members, radial.x, radial.y, radial.distance, minimumDistance
      );
      stats.inwardVelocityRemoved += velocity.inward;
      stats.tangentialVelocityRemoved += velocity.tangential;
      stats.contacts++;
      if (system.core) stats.coreNodes += members.length;
      else stats.systems++;
      stats.repelledNodes += members.length;
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

  /* Bound only anomalous motion inside each solar system. Explicit systems are scaled about the
     dominant star's carrier velocity, keeping that local origin exact while limiting only planet
     motion. Compatibility groups retain their mass-COM reference. One non-negative per-system
     scale preserves every relative direction and cannot manufacture a new radial kick. */
  function stabilizeGalaxySystemVelocities(nodes, options) {
    const opts = options || {};
    const limit = Math.max(0.01, Number.isFinite(Number(opts.limit))
      ? Number(opts.limit) : GALAXY_LOCAL_RELATIVE_SPEED_LIMIT);
    const absoluteLimit = Math.max(0.01, Number.isFinite(Number(opts.absoluteLimit))
      ? Number(opts.absoluteLimit) : Infinity);
    const compatibilitySystems = new Map();
    (nodes || []).forEach(node => {
      if (!node || node.ghost || !Number.isFinite(node.vx) || !Number.isFinite(node.vy)) return;
      const key = communityKey(node);
      if (!compatibilitySystems.has(key)) compatibilitySystems.set(key, []);
      compatibilitySystems.get(key).push(node);
    });
    const globalAnchor = galaxyGlobalAnchor(nodes);
    const systems = globalAnchor && globalAnchor.anchor_role === 'global'
      ? galaxyBlackHoleCarrierSystems(nodes, globalAnchor).map(system => system.nodes)
      : [...compatibilitySystems.values()];
    let limitedSystems = 0, maximumRelativeSpeed = 0, minimumScale = 1;
    systems.forEach(members => {
      if (members.length < 2) return;
      const resolvedAnchor = galaxySystemAnchor(members);
      const declaredIds = new Set(members.map(node => node.system_anchor_id)
        .filter(value => value !== undefined && value !== null).map(String));
      const anchor = members.find(node => node.id === opts.fixedNodeId)
        || (resolvedAnchor && (resolvedAnchor.anchor_role === 'community'
          || declaredIds.has(String(resolvedAnchor.id))) ? resolvedAnchor : null);
      let referenceVx = 0, referenceVy = 0;
      if (anchor) {
        referenceVx = Number.isFinite(anchor.vx) ? anchor.vx : 0;
        referenceVy = Number.isFinite(anchor.vy) ? anchor.vy : 0;
      } else {
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
      let systemMaximum = 0, scale = 1;
      members.forEach(node => {
        if (node === anchor) return;
        const relativeVx = node.vx - referenceVx, relativeVy = node.vy - referenceVy;
        const relativeSpeed = Math.hypot(relativeVx, relativeVy);
        systemMaximum = Math.max(systemMaximum, relativeSpeed);
        if (relativeSpeed > limit) scale = Math.min(scale, limit / relativeSpeed);
      });
      maximumRelativeSpeed = Math.max(maximumRelativeSpeed, systemMaximum);
      let carrierAdjusted = false;
      if (anchor && Number.isFinite(absoluteLimit)) {
        const carrierSpeed = Math.hypot(referenceVx, referenceVy);
        if (carrierSpeed > absoluteLimit + 1e-12) {
          const carrierScale = carrierSpeed > 1e-12 ? absoluteLimit / carrierSpeed : 0;
          const targetVx = referenceVx * carrierScale;
          const targetVy = referenceVy * carrierScale;
          const shiftX = targetVx - referenceVx;
          const shiftY = targetVy - referenceVy;
          members.forEach(node => {
            node.vx += shiftX;
            node.vy += shiftY;
          });
          referenceVx = targetVx;
          referenceVy = targetVy;
          carrierAdjusted = true;
          minimumScale = Math.min(minimumScale, carrierScale);
        }
      }
      let systemLimited = carrierAdjusted || scale < 1 - 1e-12;
      members.forEach(node => {
        if (node === anchor) {
          node.vx = referenceVx;
          node.vy = referenceVy;
          return;
        }
        const relativeVx = node.vx - referenceVx, relativeVy = node.vy - referenceVy;
        const relativeSpeed = Math.hypot(relativeVx, relativeVy);
        if (!(relativeSpeed > 1e-12)) return;
        let localScale = scale;
        if (Number.isFinite(absoluteLimit)) {
          const candidateVx = relativeVx * localScale, candidateVy = relativeVy * localScale;
          const candidateSpeed = Math.hypot(candidateVx, candidateVy);
          if (candidateSpeed > 1e-12) {
            const allowed = galaxyRelativeSpeedBudget(
              { vx: referenceVx, vy: referenceVy }, absoluteLimit, candidateSpeed,
              candidateVx, candidateVy);
            localScale = Math.min(localScale, allowed / candidateSpeed);
          }
        }
        if (localScale < 1 - 1e-12) systemLimited = true;
        minimumScale = Math.min(minimumScale, localScale);
        node.vx = referenceVx + relativeVx * localScale;
        node.vy = referenceVy + relativeVy * localScale;
      });
      if (!systemLimited) return;
      limitedSystems++;
    });
    return {
      systems: systems.length, limitedSystems, maximumRelativeSpeed, minimumScale, limit,
      absoluteLimit,
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
      gravity, softening, alpha: 1, central: opts.central,
      localGravitySetting: opts.localGravitySetting,
      skipGlobalParent: opts.central !== false,
      allowGlobalParent: opts.central === false,
      gravitationalConstant: opts.gravitationalConstant,
      localGravitationalConstant: opts.localGravitationalConstant,
      accelerationCap: opts.localAccelerationCap,
      fixedNodeId: opts.fixedNodeId,
      repulsionPadding: opts.systemAnchorExclusionPadding,
      repulsionRange: opts.systemAnchorRepulsionRange,
      repulsionAcceleration: opts.systemAnchorRepulsionAcceleration,
      authoritativeCarrierPosition: opts.authoritativeCarrierPosition,
    });
    if (opts.central !== false) {
      applyGalaxyBlackHoleGravity(bodies, {
        gravity,
        gravitationalConstant: opts.gravitationalConstant,
        blackHoleMass: opts.blackHoleMass,
        softening: Math.max(36, Number(opts.centralSoftening) || softening * 5),
        accelerationCap: opts.centralAccelerationCap,
      });
    }
    const mutualGravity = opts.includeMutualSystems === true
      ? applyGalaxyMutualSystemGravity(bodies, {
        gravity,
        gravitationalConstant: opts.gravitationalConstant,
        strengthFraction: opts.mutualSystemGravityFraction,
        softening: opts.mutualSystemSoftening,
        accelerationCap: opts.mutualSystemAccelerationCap,
        exactLimit: opts.exactLimit,
        theta: opts.theta,
        alpha: 1,
      })
      : { systems: 0, interactions: 0, traversals: 0, approximations: 0,
        maximumAcceleration: 0, capScale: 1 };
    /* Sample the outer restoring field in both leapfrog kicks. Every carrier—including a
       direct-black-hole star—translates its complete system rigidly, so no descendant can drift
       through the finite painted edge or acquire an independent galactic force. */
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
        strengthMultiplier: (Number(opts.relationStrengthMultiplier) || 1)
          * galaxyPhysicsMultiplier(opts.springStiffness,
            GALAXY_SPRING_STIFFNESS_MULTIPLIER, 8),
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
        localGravitySetting: opts.localGravitySetting,
        softening: opts.dragSoftening,
      }
    ) : { applied: 0, maximumAcceleration: 0, maximumPull: 0 };
    const spacetime = opts.includeSpacetime !== true
      ? { anchorId: null, systems: 0, coreNodes: 0, warpedNodes: 0,
        maximumWarp: 0, maximumFrameDragAcceleration: 0,
        maximumHorizonAcceleration: 0, tidalSystems: 0, tidalPlanets: 0,
        maximumTidalAcceleration: 0, accelerations: new Map() }
      : applyGalaxySpacetimeAcceleration(bodies, opts);
    spacetime.accelerations.forEach((acceleration, node) => {
      node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + acceleration.ax;
      node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + acceleration.ay;
    });
    delete spacetime.accelerations;
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
    accelerations.spacetime = spacetime;
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
     When the galaxy field is enabled, an outward attempt receives at least a 110%
     counter-projection, and only the system COM's radial velocity is changed.

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
    const inwardGravitySetting = opts.inwardGravitySetting === undefined
      ? opts.gravity : opts.inwardGravitySetting;
    const factor = galaxyInwardConvergenceFactor(opts.wallClockSeconds, inwardGravitySetting);
    if (!(factor < 1)) {
      return { applied: 0, outwardCandidates: 0, overrides: 0, factor };
    }
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
      /* The server layout authors a minimum orbital radius per system via
         galactic_target_radius on the carrier node. Convergence must never pull
         a system inside this floor — doing so destroys the even angular spacing
         that the Python layout computed. Read the floor from the carrier or
         any node in the system that carries it. */
      let minimumRadius = 0;
      for (let i = 0; i < center.nodes.length; i++) {
        const nodeTarget = Number(center.nodes[i].galactic_target_radius);
        if (Number.isFinite(nodeTarget) && nodeTarget > 0) {
          minimumRadius = Math.max(minimumRadius, nodeTarget);
        }
      }
      const dx = center.x - anchorX, dy = center.y - anchorY;
      const candidateRadius = Math.hypot(dx, dy);
      if (!Number.isFinite(candidateRadius)) return;
      const scheduledRadius = initialRadius * factor;
      const outwardDistance = Math.max(0, candidateRadius - initialRadius);
      /* Follow the gravity-selected track exactly. When the field is enabled, an outward
         attempted move must finish at least 10% inward from its starting radius. */
      const outwardCeiling = initialRadius - outwardDistance * GALAXY_OUTWARD_OVERRIDE;
      const convergedRadius = Math.max(0, outwardDistance > 0
        && factor < 1 ? Math.min(scheduledRadius, outwardCeiling) : scheduledRadius);
      const finalRadius = minimumRadius > 0
        ? Math.max(minimumRadius, convergedRadius) : convergedRadius;
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
        if (factor < 1) overrides++;
      }
      applied += center.nodes.length;
    });
    return { applied, outwardCandidates, overrides, factor };
  }

  /* Hard radial floor: prevent any solar system from falling inside its server-authored
     galactic_target_radius regardless of gravity, convergence flags, or tangential balance.
     This runs unconditionally every physics slice as the last positional correction before
     horizon/annulus passes. Without it, imperfect tangential seeding plus velocity decay
     causes systems to spiral into the black hole over time. */
  function enforceGalaxyOrbitalFloor(bodies, options) {
    const opts = options || {};
    const anchor = galaxyGlobalAnchor(bodies);
    if (!anchor || !Number.isFinite(anchor.x) || !Number.isFinite(anchor.y)) {
      return { applied: 0, systems: 0 };
    }
    const anchorX = anchor.x, anchorY = anchor.y;
    let applied = 0, systems = 0;
    communityCenters(bodies).forEach(center => {
      if (!center || center.nodes.includes(anchor)
        || center.nodes.some(node => node.anchor_role === 'global'
          || node.id === opts.fixedNodeId)) return;
      /* Read the server-authored minimum orbital radius from any node in this system. */
      let minimumRadius = 0;
      for (let i = 0; i < center.nodes.length; i++) {
        const nodeTarget = Number(center.nodes[i].galactic_target_radius);
        if (Number.isFinite(nodeTarget) && nodeTarget > 0) {
          minimumRadius = Math.max(minimumRadius, nodeTarget);
        }
      }
      if (!(minimumRadius > 0)) return;
      const dx = center.x - anchorX, dy = center.y - anchorY;
      const currentRadius = Math.hypot(dx, dy);
      if (!Number.isFinite(currentRadius) || currentRadius >= minimumRadius) return;
      /* Push the entire system outward to the floor radius as a rigid translation. */
      const unitX = currentRadius > 1e-9 ? dx / currentRadius : 1;
      const unitY = currentRadius > 1e-9 ? dy / currentRadius : 0;
      const shiftX = unitX * (minimumRadius - currentRadius);
      const shiftY = unitY * (minimumRadius - currentRadius);
      center.nodes.forEach(node => {
        node.x += shiftX;
        node.y += shiftY;
        /* Remove inward radial velocity to prevent re-penetration next frame. */
        const vx = Number.isFinite(node.vx) ? node.vx : 0;
        const vy = Number.isFinite(node.vy) ? node.vy : 0;
        const radialV = vx * unitX + vy * unitY;
        if (radialV < 0) {
          node.vx -= radialV * unitX;
          node.vy -= radialV * unitY;
        }
      });
      applied += center.nodes.length;
      systems++;
    });
    return { applied, systems };
  }

  /* Hard outer boundary for every authored local orbit. Black-hole and far-field constraints
     bound the galaxy as a whole, but neither one protects a planet from acquiring enough
     relative energy to leave its star. The first seeded star-relative radius is immutable and
     therefore cannot expand to follow an escaping body. A correction moves the member's full
     explicit descendant subtree and removes only outward radial velocity; tangential motion
     and every nested local frame remain intact. */
  function enforceGalaxyLocalOrbitBoundaries(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const stats = {
      systems: 0, members: 0, correctedNodes: 0, correctedDescendants: 0,
      correctionDistance: 0, maximumShift: 0, outwardVelocityRemoved: 0,
      maximumBoundaryRatioBefore: 0, maximumBoundaryRatioAfter: 0,
    };
    if (bodies.length < 2) return stats;
    const byId = new Map(bodies.map(node => [String(node.id), node]));
    const childrenByAnchor = new Map();
    bodies.forEach(node => {
      const parentId = node.system_anchor_id === undefined
        || node.system_anchor_id === null ? '' : String(node.system_anchor_id);
      if (!parentId || parentId === String(node.id)) return;
      if (!childrenByAnchor.has(parentId)) childrenByAnchor.set(parentId, []);
      childrenByAnchor.get(parentId).push(node);
    });
    const bodyRadius = node => finitePositive(
      node && node.radius, finitePositive(node && node.visual_radius,
        radiusFromGravityMass(node && node.gravity_mass), 80), 160
    );
    const padding = Math.max(0, Number.isFinite(Number(opts.systemAnchorExclusionPadding))
      ? Number(opts.systemAnchorExclusionPadding) : GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING);
    const boundarySlack = Math.max(1, Number.isFinite(Number(opts.localOrbitBoundarySlack))
      ? Number(opts.localOrbitBoundarySlack) : GALAXY_LOCAL_ORBIT_BOUNDARY_SLACK);
    const radiusMultiplier = galaxyOrbitalRadiusMultiplier(opts.orbitalSpeed);
    const processed = new Set(), correctedSystems = new Set();
    galaxyOrbitGroups(bodies).forEach(group => {
      const members = group.nodes || [];
      const carrier = galaxySystemAnchor(members);
      if (!carrier) return;
      orderedGalaxyLocalOrbitMembers(members, carrier, byId).forEach(node => {
        if (!node || node === carrier || processed.has(node)) return;
        processed.add(node);
        const parent = galaxyLocalOrbitParent(node, members, carrier, byId);
        if (!parent || parent === node || !Number.isFinite(parent.x)
          || !Number.isFinite(parent.y)) return;
        /* The pointer-owned source and its immediate orbit are intentionally elastic during a
           gesture. Drag gravity closes that gap gradually; projecting the immutable orbit wall
           here would copy most of the pointer displacement into the planet in one frame. */
        if (node.id === opts.fixedNodeId || parent.id === opts.fixedNodeId) return;
        /* Compatibility graphs without authored hierarchy deliberately keep their historic
           free relation/separation motion.  A system boundary is authoritative only when the
           payload names an orbital parent or radius; inferred communities are not permission
           to manufacture a wall around an arbitrary legacy pair. */
        const declaredParentId = node.system_anchor_id === undefined
          || node.system_anchor_id === null ? '' : String(node.system_anchor_id);
        const authoredRadius = Number(node.orbit_radius);
        if ((!declaredParentId || declaredParentId === String(node.id))
          && !(Number.isFinite(authoredRadius) && authoredRadius > 0)) return;
        let baseRadius = Number(node.__galaxyOrbitBaseRadius);
        if (!(Number.isFinite(baseRadius) && baseRadius > 0)) {
          const currentRadius = Math.hypot(node.x - parent.x, node.y - parent.y);
          baseRadius = Number.isFinite(authoredRadius) && authoredRadius > 0
            ? authoredRadius : currentRadius;
          setGalaxyOrbitBaseRadius(node, baseRadius);
        }
        if (!(Number.isFinite(baseRadius) && baseRadius > 0)) return;
        stats.members++;
        const minimumRadius = bodyRadius(parent) + bodyRadius(node) + padding;
        const maximumRadius = Math.max(minimumRadius,
          baseRadius * radiusMultiplier * boundarySlack);
        const dx = node.x - parent.x, dy = node.y - parent.y;
        const distance = Math.hypot(dx, dy);
        if (!Number.isFinite(distance)) return;
        stats.maximumBoundaryRatioBefore = Math.max(stats.maximumBoundaryRatioBefore,
          distance / Math.max(1e-9, maximumRadius));
        if (!(distance > maximumRadius + 1e-9)) {
          stats.maximumBoundaryRatioAfter = Math.max(stats.maximumBoundaryRatioAfter,
            distance / Math.max(1e-9, maximumRadius));
          return;
        }
        const unitX = distance > 1e-9 ? dx / distance : 1;
        const unitY = distance > 1e-9 ? dy / distance : 0;
        const shiftX = unitX * (maximumRadius - distance);
        const shiftY = unitY * (maximumRadius - distance);
        const parentVx = Number.isFinite(parent.vx) ? parent.vx : 0;
        const parentVy = Number.isFinite(parent.vy) ? parent.vy : 0;
        const relativeVx = (Number.isFinite(node.vx) ? node.vx : 0) - parentVx;
        const relativeVy = (Number.isFinite(node.vy) ? node.vy : 0) - parentVy;
        const outwardSpeed = relativeVx * unitX + relativeVy * unitY;
        const velocityShiftX = outwardSpeed > 0 ? -outwardSpeed * unitX : 0;
        const velocityShiftY = outwardSpeed > 0 ? -outwardSpeed * unitY : 0;
        const subtree = [], subtreeSeen = new Set(), pending = [node];
        while (pending.length) {
          const member = pending.pop();
          if (!member || subtreeSeen.has(member)) continue;
          subtreeSeen.add(member);
          subtree.push(member);
          (childrenByAnchor.get(String(member.id)) || []).forEach(child => {
            if (child !== parent) pending.push(child);
          });
        }
        subtree.forEach((member, index) => {
          member.x += shiftX;
          member.y += shiftY;
          member.vx = (Number.isFinite(member.vx) ? member.vx : 0) + velocityShiftX;
          member.vy = (Number.isFinite(member.vy) ? member.vy : 0) + velocityShiftY;
          if (index > 0) stats.correctedDescendants++;
        });
        correctedSystems.add(String(carrier.id));
        stats.correctedNodes++;
        const correction = Math.hypot(shiftX, shiftY);
        stats.correctionDistance += correction;
        stats.maximumShift = Math.max(stats.maximumShift, correction);
        stats.outwardVelocityRemoved += Math.max(0, outwardSpeed);
        stats.maximumBoundaryRatioAfter = Math.max(stats.maximumBoundaryRatioAfter, 1);
      });
    });
    stats.systems = correctedSystems.size;
    return stats;
  }

  /* Preserve the angular momentum that defines a galaxy after constraint projection and tiny
     numerical damping. Gravity remains the radial force; this is a bounded carrier-frame
     insertion controller that supplies only missing prograde tangent and removes radial lane
     drift. Every member of every solar system receives the same carrier velocity delta, so no
     star/planet relative orbit or link velocity is changed. Direct black-hole children use the
     same carrier curve; their stellar descendants are never supported one body at a time. */
  function supportGalaxyCarrierOrbits(nodes, options) {
    const opts = options || {};
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const field = galaxyBlackHoleField(bodies, opts);
    const anchor = field.anchor && field.anchor.anchor_role === 'global' ? field.anchor : null;
    const stats = {
      anchorId: anchor ? anchor.id : null, eligible: 0, supported: 0,
      coreEligible: 0, coreSupported: 0, minTangentialSpeed: null,
      coreMinTangentialSpeed: null, maximumRadialSpeed: 0,
      maximumVelocityCorrection: 0, corrected: 0, meanAngularVelocity: 0,
      maximumPositionCorrection: 0,
    };
    if (!anchor || !(field.gravitationalConstant > 0)) return stats;
    const direction = (seededHash(opts.layoutSeed, 'galaxy-spin') & 1) ? 1 : -1;
    const anchorVx = Number.isFinite(anchor.vx) ? anchor.vx : 0;
    const anchorVy = Number.isFinite(anchor.vy) ? anchor.vy : 0;
    const fixedNodeId = opts.fixedNodeId === undefined || opts.fixedNodeId === null
      ? null : String(opts.fixedNodeId);
    const timestep = Math.max(0.001, Math.min(2, Number(opts.timestep) || 1));
    let angularVelocitySum = 0;
    const support = (group, carrier, core) => {
      let dx = carrier.x - anchor.x, dy = carrier.y - anchor.y;
      let radius = Math.hypot(dx, dy);
      let targetSpeed = core
        ? galaxyCarrierTargetSpeed(field, radius, opts.orbitalSpeed)
        : galaxyAuthoredCarrierTargetSpeed(field, radius, opts.orbitalSpeed);
      if (!(radius > 1e-9) || !(targetSpeed > 0)) return;
      const laneRadiusKey = core ? '__galaxyCoreLaneRadius' : '__galaxyCarrierLaneRadius';
      const laneAngleKey = core ? '__galaxyCoreLaneAngle' : '__galaxyCarrierLaneAngle';
      const laneBaseRadiusKey = core
        ? '__galaxyCoreLaneBaseRadius' : '__galaxyCarrierLaneBaseRadius';
      let laneRadius = Number(carrier[laneRadiusKey]);
      let laneBaseRadius = Number(carrier[laneBaseRadiusKey]);
      /* A filtered/reloaded scene can reach the live integrator without the one-shot lane
         admission pass having populated a radius cache.  Velocity-only support is not enough
         in that case: the regular force field can leave a whole solar system visually wobbling
         around its old point instead of carrying it around the black hole.  Admit the current
         radius exactly once, then own that radius for the rest of the session.  It is a cached
         painted extent, never a live measurement, so an escaping node cannot enlarge the lane. */
      if (!(Number.isFinite(laneRadius) && laneRadius > 1e-9)
        && opts.authoritativeCarrierPosition === true) {
        laneRadius = radius;
        if (laneRadius > 1e-9) {
          setGalaxyKinematicPhase(carrier, laneRadiusKey, laneRadius);
          setGalaxyKinematicPhase(carrier, laneBaseRadiusKey, laneRadius);
          setGalaxyKinematicPhase(carrier, laneAngleKey, Math.atan2(dy, dx));
          laneBaseRadius = laneRadius;
        }
      }
      /* Managed external lanes expand radially as one common scale. Same-ring phase and chord
         clearances therefore grow together, while the admission pass has already reserved the
         largest possible local-system envelope. Core compatibility lanes retain their authored
         radii because their black-hole horizon packing has a separate minimum-clearance solve. */
      if (!core && carrier.__galaxyCarrierLaneManaged === true) {
        if (!(Number.isFinite(laneBaseRadius) && laneBaseRadius > 0)
          && Number.isFinite(laneRadius) && laneRadius > 0) {
          laneBaseRadius = laneRadius;
          setGalaxyKinematicPhase(carrier, laneBaseRadiusKey, laneBaseRadius);
        }
        if (Number.isFinite(laneBaseRadius) && laneBaseRadius > 0) {
          laneRadius = laneBaseRadius * galaxyOrbitalRadiusMultiplier(opts.orbitalSpeed);
        }
      }
      if (Number.isFinite(laneRadius) && laneRadius > 0) {
        radius = laneRadius;
        targetSpeed = core
          ? galaxyCarrierTargetSpeed(field, radius, opts.orbitalSpeed)
          : galaxyAuthoredCarrierTargetSpeed(field, radius, opts.orbitalSpeed);
        /* Admission owns the phase of every deliberately packed external ring. Systems that
           share one ring must advance by the same angle forever; adopting their independently
           perturbed force positions lets the phase gaps collapse and eventually overlaps two
           complete solar envelopes. Compatibility/core lanes without the admission marker may
           still adopt a genuine contact correction, preserving the historical drag behavior. */
        const currentAngle = Math.atan2(dy, dx);
        const cachedAngle = Number(carrier[laneAngleKey]);
        const advance = direction * targetSpeed / radius * timestep;
        const managedLane = !core && carrier.__galaxyCarrierLaneManaged === true;
        let angle;
        if (Number.isFinite(cachedAngle) && Number.isFinite(currentAngle)) {
          const expectedAngle = cachedAngle + advance;
          const phaseError = Math.atan2(
            Math.sin(currentAngle - expectedAngle), Math.cos(currentAngle - expectedAngle));
          const correctionDistance = 2 * radius * Math.abs(Math.sin(phaseError * 0.5));
          const expectedStepDistance = 2 * radius * Math.abs(Math.sin(advance * 0.5));
          /* Normal leapfrog drift is expected to land near the next cached phase. Only a
             materially displaced carrier represents an impact/boundary correction; adopt that
             phase once and do not add a second orbital step on top of it. */
          angle = !managedLane
            && correctionDistance > GALAXY_LANE_PHASE_CORRECTION_DISTANCE
            + expectedStepDistance
            ? currentAngle : expectedAngle;
        } else {
          angle = Number.isFinite(currentAngle) ? currentAngle + advance : cachedAngle;
        }
        if (!Number.isFinite(angle)) angle = 0;
        setGalaxyKinematicPhase(carrier, laneAngleKey, angle);
        setGalaxyKinematicPhase(carrier, laneRadiusKey, radius);
        const targetX = anchor.x + Math.cos(angle) * radius;
        const targetY = anchor.y + Math.sin(angle) * radius;
        const shiftX = targetX - carrier.x, shiftY = targetY - carrier.y;
        group.forEach(node => { node.x += shiftX; node.y += shiftY; });
        stats.maximumPositionCorrection = Math.max(stats.maximumPositionCorrection,
          Math.hypot(shiftX, shiftY));
        dx = carrier.x - anchor.x; dy = carrier.y - anchor.y;
      }
      const carrierVx = (Number.isFinite(carrier.vx) ? carrier.vx : 0) - anchorVx;
      const carrierVy = (Number.isFinite(carrier.vy) ? carrier.vy : 0) - anchorVy;
      const existingAngular = dx * carrierVy - dy * carrierVx;
      const orbitDirection = core && !(Number.isFinite(laneRadius) && laneRadius > 0)
        && Math.abs(existingAngular) > 1e-9 ? Math.sign(existingAngular) : direction;
      const unitX = dx / radius, unitY = dy / radius;
      const tangentX = -unitY * orbitDirection, tangentY = unitX * orbitDirection;
      const radialSpeed = carrierVx * unitX + carrierVy * unitY;
      const signedTangent = carrierVx * tangentX + carrierVy * tangentY;
      /* Admission assigns collision-free circular lanes. Exact circular carrier velocity keeps
         every member of a shared ring at one angular frequency, so phase gaps and envelope
         clearance cannot drift. This changes only the external carrier frame; local eccentric
         star/planet motion remains entirely in the unchanged relative velocities. */
      const supportedTangent = targetSpeed;
      const supportedRadial = 0;
      const deltaX = (supportedRadial - radialSpeed) * unitX
        + (supportedTangent - signedTangent) * tangentX;
      const deltaY = (supportedRadial - radialSpeed) * unitY
        + (supportedTangent - signedTangent) * tangentY;
      group.forEach(node => {
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + deltaX;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + deltaY;
      });
      const correction = Math.hypot(deltaX, deltaY);
      stats.supported++;
      if (core) stats.coreSupported++;
      if (correction > 1e-12) stats.corrected++;
      stats.maximumRadialSpeed = Math.max(stats.maximumRadialSpeed, Math.abs(supportedRadial));
      stats.maximumVelocityCorrection = Math.max(stats.maximumVelocityCorrection, correction);
      stats.minTangentialSpeed = stats.minTangentialSpeed === null
        ? supportedTangent : Math.min(stats.minTangentialSpeed, supportedTangent);
      if (core) stats.coreMinTangentialSpeed = stats.coreMinTangentialSpeed === null
        ? supportedTangent : Math.min(stats.coreMinTangentialSpeed, supportedTangent);
      angularVelocitySum += supportedTangent / radius;
    };
    field.systems.forEach(item => {
      if (!item.carrier || item.nodes.some(node => node.anchor_role === 'global'
        || (fixedNodeId !== null && String(node.id) === fixedNodeId))) return;
      stats.eligible++;
      if (item.core) stats.coreEligible++;
      support(item.nodes, item.carrier, item.core);
    });
    stats.meanAngularVelocity = stats.eligible > 0
      ? angularVelocitySum / stats.eligible : 0;
    return stats;
  }

  /* The black-hole plus cored-log halo stays smooth at the outer edge so seeded tangential
     motion remains legible. This separate field is an equally smooth, *system*
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
    const systems = galaxyBlackHoleCarrierSystems(bodies, anchor);
    const centers = systems.map(system => system.center);
    const coreKey = String(anchor.id);
    const bodyRadius = node => finitePositive(node.radius, evidenceNodeRadius(node, 3), 160);
    const systemRadius = system => system.nodes.reduce((maximum, node) => Math.max(maximum,
      Math.hypot(node.x - system.carrier.x, node.y - system.carrier.y) + bodyRadius(node)), 0);
    const seededRadius = node => ['galactic_target_radius', 'galactic_radius', 'orbit_radius']
      .reduce((maximum, key) => {
        const value = Number(node[key]);
        return Number.isFinite(value) && value > 0 ? Math.max(maximum, value) : maximum;
      }, 0);
    const anchorRadius = bodyRadius(anchor);
    let hintedExtent = 0, observedExtent = 0, horizonExtent = anchorRadius;
    let hasHint = false;
    systems.forEach(system => {
      const extent = systemRadius(system);
      const radial = Math.hypot(system.carrier.x - anchor.x, system.carrier.y - anchor.y);
      const hint = system.nodes.reduce((maximum, node) => Math.max(maximum, seededRadius(node)), 0);
      /* A declared carrier orbit plus the complete painted system radius is a hard geometric
         seed. This applies identically to ordinary and direct-black-hole carrier systems. */
      if (hint > 0) {
        hintedExtent = Math.max(hintedExtent, hint + extent);
        hasHint = true;
      }
      observedExtent = Math.max(observedExtent, radial + extent);
      horizonExtent = Math.max(horizonExtent,
        anchorRadius + extent * 2 + GALAXY_BLACK_HOLE_EXCLUSION_PADDING);
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
    /* Hints describe preferred carrier radii, not the capacity required after exact admission
       packing. Never let a stale compact hint hide the collision-free observed extent. */
    const seedExtent = Math.max(minimumRadius, hintedExtent, observedExtent);
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
      anchor, systems, centers, coreKey, bodyRadius, systemRadius,
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
    field.systems.forEach(system => {
      if (system.nodes.some(node => node.id === opts.fixedNodeId)) {
        /* Preserve the cursor-owned source exactly, but do not make its companions immune to
           the smooth outer well. They get their own radial sample until the hard cap is needed. */
        system.nodes.forEach(node => {
          if (node.id === opts.fixedNodeId) return;
          const dx = node.x - field.anchor.x, dy = node.y - field.anchor.y;
          accelerate([node], node.id, dx, dy,
            Math.hypot(dx, dy) + field.bodyRadius(node), 'fixed');
        });
        return;
      }
      const dx = system.carrier.x - field.anchor.x;
      const dy = system.carrier.y - field.anchor.y;
      accelerate(system.nodes, system.id, dx, dy,
        Math.hypot(dx, dy) + field.systemRadius(system), system.core ? 'core' : 'system');
    });
    return stats;
  }

  /* Exact outer counterpart to the black-hole contact. External systems are translated as
     rigid bodies; anchor-community satellites are projected one at a time so the anchor never
     moves. In either case only outward radial COM velocity is removed. Because this correction
     moves inward, tangential speed is retained rather than increased (a cap must not inject
     angular energy). An oversized system has a rare per-member fallback, since no rigid
     translation can fit a radius larger than the finite envelope. */
  /* Boundary projections are deliberately bounded per integration slice. A just-released
     pointer can leave a stretched system outside the cached annulus; completing that correction
     in one member-wise teleport makes the first release frame visibly jump even though velocity
     is capped. Track the budget across the alternating outer-boundary passes so the next fixed
     slice can finish the projection without exceeding the 48-unit positional contract. */
  function reserveGalaxyBoundaryCorrection(options, members, requested, scope) {
    const budget = options && options.__positionCorrectionBudget;
    /* A direct annulus projection is the authoritative hard closure for pathological scenes;
       only a feasible rigid carrier correction is deliberately spread across later slices when
       no pointer owns the system. Fixed-node follower projections remain bounded during drag. */
    if (!budget || !Array.isArray(members)
      || (scope !== 'rigid' && options.fixedNodeId == null)
      || members.some(node => node && node.id === options.fixedNodeId)) return requested;
    const limit = Number.isFinite(Number(budget.limit)) ? Math.max(0, Number(budget.limit)) : 48;
    const used = budget.used || (budget.used = new Map());
    const remaining = members.reduce((available, node) => Math.min(available,
      Math.max(0, limit - (used.get(node) || 0))), limit);
    const applied = Math.min(Math.max(0, requested), remaining);
    members.forEach(node => used.set(node, (used.get(node) || 0) + applied));
    return applied;
  }

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
    field.systems.forEach(system => {
      if (system.nodes.some(node => node.id === opts.fixedNodeId)) {
        /* Pointer coordinates are an input target, not permission to paint outside the finite
           galaxy. Cap this stretched system one body at a time—including the source—so a long
           outward hold cannot create release-only geometry. The next pointer event supplies a
           fresh target; its final painted fx/fy remains on the outer annulus. */
        system.nodes.forEach(node => {
          const unit = radial(node.id, node.x - anchorX, node.y - anchorY);
          const targetDistance = Math.max(0, field.envelopeRadius - field.bodyRadius(node));
          const correction = unit.distance - targetDistance;
          if (!(correction > 0)) return;
          const appliedCorrection = reserveGalaxyBoundaryCorrection(opts, [node], correction);
          if (!(appliedCorrection > 0)) return;
          const boundedTargetDistance = unit.distance - appliedCorrection;
          node.x = anchorX + unit.x * boundedTargetDistance;
          node.y = anchorY + unit.y * boundedTargetDistance;
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
      const unit = radial(system.id,
        system.carrier.x - anchorX, system.carrier.y - anchorY);
      const radius = field.systemRadius(system);
      /* A compact system fits inside R after one COM translation. A just-released drag can
         leave a source at the cursor and companions at the cap, making q_s >= R; translating
         that stretched geometry by its COM would throw the already-safe follower hundreds of
         units. Resolve that impossible rigid fit member-by-member for this slice instead. */
      if (radius >= field.envelopeRadius - 1e-9) {
        let bounded = false;
        system.nodes.forEach(node => {
          const memberUnit = radial(node.id, node.x - anchorX, node.y - anchorY);
          const targetDistance = Math.max(0, field.envelopeRadius - field.bodyRadius(node));
          const correction = memberUnit.distance - targetDistance;
          if (!(correction > 1e-9)) return;
          const appliedCorrection = reserveGalaxyBoundaryCorrection(opts, [node], correction);
          if (!(appliedCorrection > 0)) return;
          const boundedTargetDistance = memberUnit.distance - appliedCorrection;
          node.x = anchorX + memberUnit.x * boundedTargetDistance;
          node.y = anchorY + memberUnit.y * boundedTargetDistance;
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
      const appliedCorrection = reserveGalaxyBoundaryCorrection(
        opts, system.nodes, correction, 'rigid'
      );
      if (!(appliedCorrection > 0)) return;
      const shiftX = -unit.x * appliedCorrection, shiftY = -unit.y * appliedCorrection;
      system.nodes.forEach(node => {
        node.x += shiftX;
        node.y += shiftY;
        if (Number.isFinite(node.fx)) node.fx += shiftX;
        if (Number.isFinite(node.fy)) node.fy += shiftY;
      });
      const velocity = stabilizeVelocity(system.nodes, unit.x, unit.y,
        unit.distance, targetDistance);
      stats.boundedSystems++;
      if (system.core) stats.boundedCoreNodes += system.nodes.length;
      stats.correctedDistance += correction;
      stats.maximumShift = Math.max(stats.maximumShift, correction);
      stats.outwardVelocityRemoved += velocity.outward;
      stats.tangentialVelocityRemoved += velocity.tangential;
    });
    /* The COM/system-radius projection above is exact whenever q_s <= R. If an extreme late
       local deformation has made q_s > R, fitting it rigidly is mathematically impossible.
       Finish with a member-level cap so the public invariant remains every free painted node
       lies inside the cached envelope; normal systems never enter this branch. */
    field.systems.forEach(system => {
      system.nodes.forEach(node => {
        if (node === field.anchor || node.id === opts.fixedNodeId) return;
        const unit = radial(node.id, node.x - anchorX, node.y - anchorY);
        const targetDistance = Math.max(0, field.envelopeRadius - field.bodyRadius(node));
        const correction = unit.distance - targetDistance;
        if (!(correction > 1e-9)) return;
        const appliedCorrection = reserveGalaxyBoundaryCorrection(opts, [node], correction);
        if (!(appliedCorrection > 0)) return;
        const boundedTargetDistance = unit.distance - appliedCorrection;
        node.x = anchorX + unit.x * boundedTargetDistance;
        node.y = anchorY + unit.y * boundedTargetDistance;
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
      const requestedCorrection = Math.abs(target - distance);
      const appliedCorrection = reserveGalaxyBoundaryCorrection(
        opts, [node], requestedCorrection
      );
      if (!(appliedCorrection > 0)) return;
      const boundedTarget = target > distance
        ? distance + appliedCorrection : distance - appliedCorrection;
      node.x = anchorX + unitX * boundedTarget;
      node.y = anchorY + unitY * boundedTarget;
      if (Number.isFinite(node.fx)) node.fx = node.x;
      if (Number.isFinite(node.fy)) node.fy = node.y;
      const vx = (Number.isFinite(node.vx) ? node.vx : 0)
        - (Number.isFinite(field.anchor.vx) ? field.anchor.vx : 0);
      const vy = (Number.isFinite(node.vy) ? node.vy : 0)
        - (Number.isFinite(field.anchor.vy) ? field.anchor.vy : 0);
      const tangentX = -unitY, tangentY = unitX;
      const radialSpeed = vx * unitX + vy * unitY;
      const tangentSpeed = vx * tangentX + vy * tangentY;
      const tangentScale = boundedTarget > 1e-9
        ? Math.max(0, Math.min(1, distance / boundedTarget)) : 0;
      const targetRadial = boundedTarget > distance ? Math.max(0, radialSpeed)
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
  /* Keep the percentage clock responsive after gravity has integrated a few frames. Above or
     below the natural 100% rate, raw velocity multiplication is not a bound Newtonian orbit: at
     the old high endpoint it repeatedly injected escape energy and planets scattered through
     neighbouring systems. Managed local members therefore keep a cached rotation direction and
     immutable base radius while adopting the phase produced by contact/relation constraints.
     Each radial correction translates the member's full descendant subtree and changes its
     velocity by one common frame delta, preserving every nested moon/planet orbit without
     fighting legitimate angular separation on the next frame. */
  function applyGalaxyOrbitalSpeedControl(nodes, options) {
    const opts = options || {};
    const orbitalSpeed = galaxyOrbitalSpeedMultiplier(opts.orbitalSpeed);
    const absoluteSpeedLimit = Number.isFinite(Number(opts.speedLimit))
      ? Math.max(0.01, Number(opts.speedLimit)) : Number.POSITIVE_INFINITY;
    const orbitalRadius = galaxyOrbitalRadiusMultiplier(opts.orbitalSpeed);
    const bodies = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const field = galaxyBlackHoleField(bodies, opts);
    const globalAnchor = field.anchor && field.anchor.anchor_role === 'global' ? field.anchor : null;
    const stats = { systems: 0, localSatellites: 0, multiplier: orbitalSpeed,
      radiusMultiplier: orbitalRadius, positionCorrections: 0, maximumPositionCorrection: 0 };
    /* 100 is the shipped orbit rate. The live integrator already supports the galactic carrier
       at that clock, so a second carrier correction is unnecessary once motion exists. Local
       planet control must still run: it owns each cached star-relative direction and prevents
       contact or boundary projections from turning a prograde orbit retrograde. */
    const neutralPhase = Math.abs(orbitalSpeed - 1) <= 1e-9
      && bodies.some(node => Math.hypot(
        Number.isFinite(node.vx) ? node.vx : 0,
        Number.isFinite(node.vy) ? node.vy : 0,
      ) > 1e-8);
    /* A zero global field must not freeze local satellites: the local stellar wells are
       independent of the black-hole constant, so at Gravity=0 the per-frame phase/radius
       controller still has to run. Only carrier support below depends on the global
       field, and `supportCarrier` is skipped per item when the field is inactive.
       PR #177 review thread at this site. */
    const globalFieldActive = field.gravitationalConstant > 0;
    if (!globalAnchor || (!globalFieldActive && !bodies.some(node => node.system_anchor_id != null))) {
      return stats;
    }
    const direction = (seededHash(opts.layoutSeed, 'galaxy-spin') & 1) ? 1 : -1;
    const supportCarrier = (members, carrier) => {
      if (!carrier || carrier === globalAnchor) return;
      const dx = carrier.x - globalAnchor.x, dy = carrier.y - globalAnchor.y;
      const radius = Math.hypot(dx, dy);
      if (!(radius > 1e-9)) return;
      const relativeVx = (Number.isFinite(carrier.vx) ? carrier.vx : 0)
        - (Number.isFinite(globalAnchor.vx) ? globalAnchor.vx : 0);
      const relativeVy = (Number.isFinite(carrier.vy) ? carrier.vy : 0)
        - (Number.isFinite(globalAnchor.vy) ? globalAnchor.vy : 0);
      const unitX = dx / radius, unitY = dy / radius;
      const tangentX = -unitY, tangentY = unitX;
      const currentTangent = relativeVx * tangentX + relativeVy * tangentY;
      const sign = Math.sign(currentTangent) || direction;
      const desiredTangent = galaxyCarrierTargetSpeed(
        field, radius, opts.orbitalSpeed) * sign;
      const delta = desiredTangent - currentTangent;
      members.forEach(node => {
        if (node.id === opts.fixedNodeId) return;
        node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + tangentX * delta;
        node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + tangentY * delta;
      });
      if (Number.isFinite(absoluteSpeedLimit) && carrier.id !== opts.fixedNodeId) {
        const carrierVx = Number.isFinite(carrier.vx) ? carrier.vx : 0;
        const carrierVy = Number.isFinite(carrier.vy) ? carrier.vy : 0;
        const carrierSpeed = Math.hypot(carrierVx, carrierVy);
        if (carrierSpeed > absoluteSpeedLimit) {
          const scale = absoluteSpeedLimit / carrierSpeed;
          const correctionX = carrierVx * scale - carrierVx;
          const correctionY = carrierVy * scale - carrierVy;
          members.forEach(node => {
            if (node.id === opts.fixedNodeId) return;
            node.vx = (Number.isFinite(node.vx) ? node.vx : 0) + correctionX;
            node.vy = (Number.isFinite(node.vy) ? node.vy : 0) + correctionY;
          });
        }
      }
      stats.systems++;
    };
    field.systems.forEach(item => {
      const members = item.nodes;
      const carrier = item.carrier;
      /* Carrier support already runs inside the live integrator at the neutral 100% clock.
         Keep that frame untouched here, but never skip the local controller: its cached
         direction is what prevents a planet from reversing around its authored star after
         contact or boundary corrections. */
      if (!neutralPhase && globalFieldActive) supportCarrier(members, carrier);
      const localAnchor = carrier;
      if (!localAnchor) return;
      const byId = new Map(members.map(node => [String(node.id), node]));
      const childrenByAnchor = new Map();
      members.forEach(candidate => {
        const parentId = candidate && candidate.system_anchor_id !== undefined
          && candidate.system_anchor_id !== null ? String(candidate.system_anchor_id) : '';
        if (!parentId || parentId === String(candidate.id)) return;
        if (!childrenByAnchor.has(parentId)) childrenByAnchor.set(parentId, []);
        childrenByAnchor.get(parentId).push(candidate);
      });
      const subtreeOf = root => {
        const subtree = [], seen = new Set(), pending = [root];
        while (pending.length) {
          const member = pending.pop();
          if (!member || seen.has(member)) continue;
          seen.add(member);
          subtree.push(member);
          (childrenByAnchor.get(String(member.id)) || []).forEach(child => pending.push(child));
        }
        return subtree;
      };
      orderedGalaxyLocalOrbitMembers(members, localAnchor, byId).forEach(node => {
        if (node === localAnchor) return;
        const parent = galaxyLocalOrbitParent(node, members, localAnchor, byId)
          || localAnchor;
        const dx = node.x - parent.x, dy = node.y - parent.y;
        const radius = Math.hypot(dx, dy);
        if (!(radius > 1e-9)) return;
        /* Server-authored lanes are the visual contract. The initial position may be on a
           slightly elliptical seed, so sampling its instantaneous distance would give every
           planet a subtly different circle and recreate the tangled force-cluster look. */
        const authoredRadius = Number(node.orbit_radius);
        let baseRadius = Number.isFinite(authoredRadius) && authoredRadius > 0
          ? authoredRadius : Number(node.__galaxyOrbitBaseRadius);
        if (!(Number.isFinite(baseRadius) && baseRadius > 0)) {
          baseRadius = radius;
          setGalaxyOrbitBaseRadius(node, baseRadius);
        } else if (Number.isFinite(authoredRadius) && authoredRadius > 0
          && Number(node.__galaxyOrbitBaseRadius) !== authoredRadius) {
          node.__galaxyOrbitBaseRadius = authoredRadius;
        }
        const parentRadius = finitePositive(parent.radius,
          finitePositive(parent.visual_radius, 3, 160), 160);
        const nodeRadius = finitePositive(node.radius,
          finitePositive(node.visual_radius, 3, 160), 160);
        const minimumRadius = parentRadius + nodeRadius
          + GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING;
        const targetRadius = Math.max(minimumRadius, baseRadius * orbitalRadius);
        const authoredHierarchy = galaxyHasAuthoredParent(node, parent);
        const localGravityMultiplier = galaxyLocalGravityMultiplier(parent, opts);
        const localGravity = galaxySystemGravityConstant(parent, opts.gravity,
          opts.localGravitySetting, authoredHierarchy)
          * localGravityMultiplier;
        const localAccelerationCap = defaultGalaxySystemAccelerationCap(parent, opts.gravity,
          opts.localGravitySetting, authoredHierarchy)
          * Math.max(0.25, localGravityMultiplier);
        const anchorMass = finitePositive(parent.gravity_mass, 1, 1000);
        const denominator = Math.pow(targetRadius * targetRadius
          + Math.max(0.1, Number(opts.softening) || 8) ** 2, 1.5);
        const rawAcceleration = denominator > 0
          ? localGravity * anchorMass * targetRadius / denominator : 0;
        const acceleration = Math.min(localAccelerationCap, rawAcceleration);
        const baseSpeed = Math.min(GALAXY_LOCAL_RELATIVE_SPEED_LIMIT,
          Math.sqrt(Math.max(0, acceleration * targetRadius)));
        const currentAngle = Math.atan2(dy, dx);
        const relativeVx = (Number.isFinite(node.vx) ? node.vx : 0)
          - (Number.isFinite(parent.vx) ? parent.vx : 0);
        const relativeVy = (Number.isFinite(node.vy) ? node.vy : 0)
          - (Number.isFinite(parent.vy) ? parent.vy : 0);
        const currentTangent = (-dy * relativeVx + dx * relativeVy) / radius;
        const sign = Math.sign(currentTangent)
          || ((seededHash(opts.layoutSeed, 'system:' + String(parent.id)) & 1) ? 1 : -1);
        const parentId = String(parent.id);
        let phase = node.__galaxySpeedControlPhase;
        if (!phase || phase.anchorId !== parentId
          || !Number.isFinite(Number(phase.direction))) {
          phase = setGalaxyKinematicPhase(node, '__galaxySpeedControlPhase', {
            anchorId: parentId, angle: currentAngle, direction: sign,
            multiplier: orbitalSpeed, radiusMultiplier: orbitalRadius,
          });
        } else {
          phase.multiplier = orbitalSpeed;
          phase.radiusMultiplier = orbitalRadius;
        }
        /* Pointer ownership is the one temporary exception to exact lane projection. Let the
           existing bounded drag field pull followers instead of copying the star's pointer
           displacement, while adopting the gesture's latest angle for a snap-free release. */
        if (node.id === opts.fixedNodeId || parent.id === opts.fixedNodeId) {
          phase.angle = currentAngle;
          return;
        }
        /* The local clock owns angular phase just as the scene owns radius. Raw leapfrog,
           collision, and relation work may translate the whole system, but they cannot turn
           a planet backward or pull it onto a chord through the star. */
        const timestep = Math.max(0.001, Math.min(2, Number(opts.timestep) || 1));
        const requestedRelativeSpeed = baseSpeed * orbitalSpeed;
        const phaseTangentX = -Math.sin(phase.angle) * phase.direction;
        const phaseTangentY = Math.cos(phase.angle) * phase.direction;
        /* Use one scalar for the phase clock and emitted velocity. The final tangent rotates
           during the step, so also apply the direction-independent residual cap; reusing a
           pre-step directional budget after that rotation must never exceed the absolute cap. */
        const phaseSpeed = Math.min(
          galaxyRelativeSpeedBudget(parent, absoluteSpeedLimit,
            requestedRelativeSpeed, phaseTangentX, phaseTangentY),
          galaxyRelativeSpeedBudget(parent, absoluteSpeedLimit, requestedRelativeSpeed),
        );
        const angularSpeed = phaseSpeed / Math.max(1e-6, targetRadius);
        phase.angle += phase.direction * angularSpeed * timestep;
        const unitX = Math.cos(phase.angle), unitY = Math.sin(phase.angle);
        const tangentX = -unitY * phase.direction, tangentY = unitX * phase.direction;
        const targetX = parent.x + unitX * targetRadius;
        const targetY = parent.y + unitY * targetRadius;
        const targetVx = (Number.isFinite(parent.vx) ? parent.vx : 0)
          + tangentX * phaseSpeed;
        const targetVy = (Number.isFinite(parent.vy) ? parent.vy : 0)
          + tangentY * phaseSpeed;
        const shiftX = targetX - node.x, shiftY = targetY - node.y;
        const velocityShiftX = targetVx - (Number.isFinite(node.vx) ? node.vx : 0);
        const velocityShiftY = targetVy - (Number.isFinite(node.vy) ? node.vy : 0);
        subtreeOf(node).forEach(member => {
          member.x += shiftX;
          member.y += shiftY;
          member.vx = (Number.isFinite(member.vx) ? member.vx : 0) + velocityShiftX;
          member.vy = (Number.isFinite(member.vy) ? member.vy : 0) + velocityShiftY;
        });
        const positionCorrection = Math.hypot(shiftX, shiftY);
        if (positionCorrection > 1e-12) stats.positionCorrections++;
        stats.maximumPositionCorrection = Math.max(
          stats.maximumPositionCorrection, positionCorrection);
        stats.localSatellites++;
      });
    });
    return stats;
  }

  function integrateGalaxyLeapfrog(nodes, links, bridges, options) {
    // kick-drift-kick: sample at x(t), drift from the half kick, then close at x(t + dt).
    /* Boundary projections are allowed to converge over several fixed slices, but one slice
       must not visibly teleport a released cluster. Keep the budget private to this call so
       every alternating inner/outer projection shares the same positional limit. */
    const opts = Object.assign({}, options || {}, {
      __positionCorrectionBudget: { limit: 48, used: new Map() },
    });
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
    /* System-envelope packing supersedes the legacy monotone inward projection.  Running both
       constraints in one slice makes them exact opponents: packing clears two systems, then
       convergence contracts them back through one another.  Black-hole gravity still owns the
       radial orbit; this disables only the artificial per-slice carrier teleport. */
    const convergenceAnchor = opts.inwardConvergence === true
      ? galaxyGlobalAnchor(bodies) : null;
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
    const eventHorizonDecay = opts.includeSpacetime !== true
      ? { anchorId: null, systems: 0, nodes: 0, maximumWarp: 0,
        maximumVelocityRemoved: 0 }
      : applyGalaxyEventHorizonDecay(bodies, opts);
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
        localGravitySetting: opts.localGravitySetting,
        gravityMultiplier: GALAXY_DRAG_GRAVITY_MULTIPLIER,
        softening: opts.dragSoftening,
        duration: Number.isFinite(Number(opts.wallClockSeconds))
          ? Number(opts.wallClockSeconds) : GALAXY_FRAME_INTERVAL_MS / 1000,
        maximumPull: GALAXY_DRAG_POSITION_MAX_PULL,
        maximumImpulse: 1,
        applyImpulse: true,
        linkSetting: opts.linkSetting,
        padding: opts.relationPadding,
      }
    ) : { applied: 0, maximumAcceleration: 0, maximumPull: 0 };
    const systemVelocity = stabilizeGalaxySystemVelocities(bodies, {
      limit: opts.localRelativeSpeedLimit,
      /* The integrator applies the world-speed ceiling below as one common scale so the
         mass-weighted local frame keeps its momentum. The direct helper still accepts an
         absoluteLimit for callers that need a per-vector projection. */
      absoluteLimit: Infinity,
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
    const convergence = convergenceAnchor && !opts.dragSource
      ? applyGalaxyInwardConvergence(bodies, convergenceAnchor, initialRadii, opts)
      : { applied: 0, outwardCandidates: 0, overrides: 0, factor: 1 };
    /* Hard orbital floor: prevents systems from spiraling inside their server-authored
       galactic_target_radius due to imperfect tangential balance or velocity decay.
       Runs unconditionally regardless of the inwardConvergence flag. */
    const orbitalFloor = !opts.dragSource
      ? enforceGalaxyOrbitalFloor(bodies, opts)
      : { applied: 0, systems: 0 };
    /* Resolve at the carrier-frame level after local/link/convergence corrections.  One
       conservative circle represents the complete painted solar system, so a correction is a
       rigid translation and can never stretch a planet away from its star. */
    const systemPackingPasses = [];
    if (opts.includeSystemPacking === true) {
      systemPackingPasses.push(applyGalaxySystemPacking(bodies, Object.assign({}, opts, {
        gap: opts.systemPackingGap,
        strength: opts.systemPackingStrength,
        maxCorrection: opts.systemPackingMaxCorrection,
        fixedNodeId: opts.fixedNodeId,
      })));
    }
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
    /* Stellar exclusion moves only a penetrating planet in the star frame and can therefore
       shift the evidence-mass COM by a few ulps after the controlled inward projection. Restore
       the exact shared carrier track once after local closure, then reassert only the global
       annulus. The rigid translation cannot reopen a star/planet overlap. */
    const closureConvergence = convergenceAnchor
      ? applyGalaxyInwardConvergence(bodies, convergenceAnchor, initialRadii, opts)
      : { applied: 0, outwardCandidates: 0, overrides: 0, factor: 1 };
    convergence.closureApplied = closureConvergence.applied;
    if (opts.includeSystemPacking === true) {
      systemPackingPasses.push(applyGalaxySystemPacking(bodies, Object.assign({}, opts, {
        gap: opts.systemPackingGap,
        strength: opts.systemPackingStrength,
        maxCorrection: opts.systemPackingMaxCorrection,
        fixedNodeId: opts.fixedNodeId,
      })));
    }
    if (opts.includeFarFieldConfinement !== false) {
      closureConfinements.push(applyGalaxyFarFieldConfinement(bodies, opts));
    }
    closureHorizons.push(projectBlackHoleHorizon());
    annulusPasses.push(opts.includeFarFieldConfinement === false
      ? { anchorId: null, innerCorrectedNodes: 0, outerCorrectedNodes: 0,
        infeasibleNodes: 0 }
      : applyGalaxyAnnularBounds(bodies, opts));
    /* The strict BH/outer closures above can translate a carrier after the previous packing
       pass. Close once more at system-envelope level, then reassert only the global boundaries.
       This alternating projection is bounded and keeps local geometry rigid throughout. */
    if (opts.includeSystemPacking === true) {
      /* Earlier response passes stay bounded. The final painted phase must satisfy its hard
         envelope invariant in this same slice: leaving one deep penetration to future frames
         makes the systems visibly stacked and repeats the collision work indefinitely. This
         exact carrier translation changes no member-relative position or velocity, so it adds
         no kinetic energy; pointer-owned systems remain fixed and any genuinely infeasible
         fixed/boundary conflict is reported rather than moved. */
      const packingClosureLimit = Math.max(1,
        Math.min(256, galaxySystemEnvelopes(bodies, opts).length + 1));
      for (let passIndex = 0; passIndex < packingClosureLimit; passIndex++) {
        const packingPass = applyGalaxySystemPacking(bodies, Object.assign({}, opts, {
          gap: opts.systemPackingGap,
          strength: 1,
          maxCorrection: Infinity,
          fixedNodeId: opts.fixedNodeId,
        }));
        systemPackingPasses.push(packingPass);
        if (!packingPass.remainingOverlaps || packingPass.infeasiblePairs) break;
      }
    }
    /* The annulus can clamp an individual member after the normal stellar closure. Reassert
       the local painted boundary as the final positional constraint so the last frame cannot
       leave a planet intersecting its immediate carrier. */
    const finalStellarPass = applyGalaxySystemAnchorExclusion(bodies, {
      padding: opts.systemAnchorExclusionPadding,
      fixedNodeId: opts.fixedNodeId,
    });
    stellarPasses.push(finalStellarPass);
    const localOrbitBoundary = enforceGalaxyLocalOrbitBoundaries(bodies, opts);
    stellarAudit = galaxySystemAnchorClearance(bodies, {
      padding: opts.systemAnchorExclusionPadding,
    });
    const combinedSystemAnchorExclusion = combineGalaxySystemAnchorExclusions(stellarPasses);
    const systemPacking = {
      systems: systemPackingPasses.reduce((maximum, pass) => Math.max(maximum,
        pass.systems || 0), 0),
      pairs: systemPackingPasses.reduce((sum, pass) => sum + (pass.pairs || 0), 0),
      overlaps: systemPackingPasses.reduce((sum, pass) => sum + (pass.overlaps || 0), 0),
      adjustedSystems: systemPackingPasses.reduce((sum, pass) =>
        sum + (pass.adjustedSystems || 0), 0),
      correctionDistance: systemPackingPasses.reduce((sum, pass) =>
        sum + (pass.correctionDistance || 0), 0),
      maximumShift: systemPackingPasses.reduce((maximum, pass) => Math.max(maximum,
        pass.maximumShift || 0), 0),
      remainingOverlaps: systemPackingPasses.length
        ? systemPackingPasses[systemPackingPasses.length - 1].remainingOverlaps || 0 : 0,
      infeasiblePairs: systemPackingPasses.reduce((sum, pass) =>
        sum + (pass.infeasiblePairs || 0), 0),
      boundaryViolations: systemPackingPasses.length
        ? systemPackingPasses[systemPackingPasses.length - 1].boundaryViolations || 0 : 0,
      minimumBlackHoleClearance: systemPackingPasses.length
        ? systemPackingPasses[systemPackingPasses.length - 1].minimumBlackHoleClearance : null,
      minimumOuterClearance: systemPackingPasses.length
        ? systemPackingPasses[systemPackingPasses.length - 1].minimumOuterClearance : null,
      envelopeRadius: systemPackingPasses.length
        ? systemPackingPasses[systemPackingPasses.length - 1].envelopeRadius || 0 : 0,
      gap: systemPackingPasses.length
        ? systemPackingPasses[systemPackingPasses.length - 1].gap || 0 : 0,
    };
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
    /* Constraint projection can rotate a carrier's position without rotating its velocity.
       Reconcile the final carrier tangent once, after packing and annulus closure, then compose
       the unchanged local planet velocities against that supported star frame. */
    const carrierOrbitSupport = opts.central === false
      ? { anchorId: null, eligible: 0, supported: 0, coreEligible: 0, coreSupported: 0,
        minTangentialSpeed: null, coreMinTangentialSpeed: null,
        maximumRadialSpeed: 0, maximumVelocityCorrection: 0, corrected: 0,
        meanAngularVelocity: 0, maximumPositionCorrection: 0 }
      : supportGalaxyCarrierOrbits(bodies, opts);
    /* All drag position projection finishes before packing, horizon, annulus and carrier
       support. A late per-node pull would bypass those carrier-frame closures and could peel a
       planet away from its star. The live acceleration sample remains active through the full
       leapfrog step; these zero reports keep the aggregate diagnostics backward-compatible. */
    const finalDragPositionGravity = { applied: 0, maximumAcceleration: 0, maximumPull: 0 };
    const secondFinalDragPositionGravity = {
      applied: 0, maximumAcceleration: 0, maximumPull: 0,
    };
    const thirdFinalDragPositionGravity = {
      applied: 0, maximumAcceleration: 0, maximumPull: 0,
    };
    const finalSystemVelocity = stabilizeGalaxySystemVelocities(bodies, {
      limit: opts.localRelativeSpeedLimit,
      /* Keep the final local pass momentum-preserving; the common world-speed projection below
         is the sole absolute cap for a leapfrog slice. */
      absoluteLimit: Infinity,
      fixedNodeId: opts.fixedNodeId,
    });
    systemVelocity.limitedSystems += finalSystemVelocity.limitedSystems;
    systemVelocity.maximumRelativeSpeed = Math.max(systemVelocity.maximumRelativeSpeed,
      finalSystemVelocity.maximumRelativeSpeed);
    systemVelocity.minimumScale = Math.min(systemVelocity.minimumScale,
      finalSystemVelocity.minimumScale);
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
    /* Ghosts are rendered history, not evidence mass. Advance their exact test-particle
       phase only after live constraints and the common speed scale complete, so they cannot
       trigger a contact/reheat or alter any live system's momentum. */
    const blackHoleSpinAngle = advanceGalaxyBlackHoleSpin(nodes, opts);
    const ghostOrbit = integrateGalaxyGhostOrbits(nodes, opts);
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
      blackHoleSpinAngle,
      ghostOrbit,
      maximumSpeed,
      uncappedMaximumSpeed,
      speedCapped: speedScale < 1,
      convergence,
      relationConstraint,
      orbitalSeparation,
      localOrbitBoundary,
      systemPacking,
      systemAnchorExclusion,
      blackHoleExclusion,
      farFieldConfinement,
      farFieldGravity,
      spacetime: end.spacetime || start.spacetime
        || { anchorId: null, systems: 0, coreNodes: 0, warpedNodes: 0,
          maximumWarp: 0, maximumFrameDragAcceleration: 0,
          maximumHorizonAcceleration: 0, tidalSystems: 0, tidalPlanets: 0,
          maximumTidalAcceleration: 0 },
      eventHorizonDecay,
      carrierOrbitSupport,
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
        applied: Math.max(dragAcceleration.applied, dragPositionGravity.applied,
          finalDragPositionGravity.applied, secondFinalDragPositionGravity.applied,
          thirdFinalDragPositionGravity.applied),
        maximumAcceleration: Math.max(
          dragAcceleration.maximumAcceleration, dragPositionGravity.maximumAcceleration,
          finalDragPositionGravity.maximumAcceleration,
          secondFinalDragPositionGravity.maximumAcceleration,
          thirdFinalDragPositionGravity.maximumAcceleration
        ),
        maximumPull: Math.max(dragPositionGravity.maximumPull,
          finalDragPositionGravity.maximumPull, secondFinalDragPositionGravity.maximumPull,
          thirdFinalDragPositionGravity.maximumPull),
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
  function paintMaterialSurface(ctx, x, y, r, scale, recipe, forceLow, forceFull) {
    /* Parent bodies remain the visual landmarks of a large Galaxy. Their cached sprite may be
       scaled down on screen, but it must retain the full gradient, grain, sheen, and bezel
       master instead of inheriting the graph-wide flat signature downgrade. */
    const tier = forceFull ? 'full' : materialTier(r * Math.max(0.01, scale), forceLow);
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

  function galaxyOrbitLaneGeometry(nodes) {
    const values = (nodes || []).filter(node => node && !node.ghost
      && Number.isFinite(node.x) && Number.isFinite(node.y));
    const byId = new Map(values.map(node => [String(node.id), node]));
    const lanes = new Map();
    values.forEach(node => {
      const tier = Number(node.orbit_tier);
      const parentId = node.system_anchor_id === undefined
        || node.system_anchor_id === null ? '' : String(node.system_anchor_id);
      if (!(tier > 0) || !parentId || parentId === String(node.id)) return;
      const anchor = byId.get(parentId);
      if (!anchor) return;
      const measured = Math.hypot(node.x - anchor.x, node.y - anchor.y);
      const radius = finitePositive(node.__galaxyOrbitBaseRadius,
        finitePositive(node.orbit_radius, measured, Infinity), Infinity);
      if (!(radius > 0)) return;
      /* Depth (orbit_tier) and a parent's local ring are separate in a nested hierarchy:
         several planets can be depth 1 while occupying different star-relative lanes. */
      const key = String(anchor.id) + ':' + tier + ':' + Math.round(radius * 1000);
      let lane = lanes.get(key);
      if (!lane) {
        lane = { anchor, tier, radius: 0, samples: 0 };
        lanes.set(key, lane);
      }
      lane.radius += radius;
      lane.samples++;
    });
    return [...lanes.values()].map(lane => ({
      anchorId: String(lane.anchor.id), x: lane.anchor.x, y: lane.anchor.y,
      tier: lane.tier, radius: lane.radius / Math.max(1, lane.samples),
      members: lane.samples, color: lane.anchor.color,
    })).sort((left, right) => left.anchorId.localeCompare(right.anchorId)
      || left.tier - right.tier);
  }

  function galaxyStarAnchorIds(lanes) {
    const connected = new Map();
    (lanes || []).forEach(lane => {
      if (!lane || lane.anchorId === undefined || lane.anchorId === null) return;
      const id = String(lane.anchorId);
      connected.set(id, (connected.get(id) || 0)
        + Math.max(0, Number(lane.members) || 0));
    });
    return new Set([...connected].filter(([, count]) => count > 2).map(([id]) => id));
  }

  function galaxyPrimaryAnchorIds(lanes) {
    return new Set((lanes || [])
      .filter(lane => lane && lane.anchorId !== undefined && lane.anchorId !== null
        && Math.max(0, Number(lane.members) || 0) > 0)
      .map(lane => String(lane.anchorId)));
  }

  function paintGalaxyOrbitLanes(ctx, nodes, scale, accent, preparedLanes) {
    if (!ctx) return 0;
    const lanes = Array.isArray(preparedLanes)
      ? preparedLanes : galaxyOrbitLaneGeometry(nodes);
    const inverseScale = 1 / Math.max(0.1, Number(scale) || 1);
    ctx.save();
    ctx.lineWidth = 0.55 * inverseScale;
    lanes.forEach(lane => {
      ctx.strokeStyle = alpha(lane.color || accent || '#9d7bff', 0.16);
      ctx.beginPath();
      ctx.arc(lane.x, lane.y, lane.radius, 0, 6.2832);
      ctx.stroke();
    });
    ctx.restore();
    return lanes.length;
  }

  function galaxyAnchorAdornmentEligible(node, laneAnchorIds) {
    if (!node || node.ghost) return false;
    if (node.anchor_role === 'global') return true;
    return node.anchor_role === 'community' && laneAnchorIds instanceof Set
      && laneAnchorIds.has(String(node.id));
  }

  function galaxyOrbitalLinkRole(link) {
    const source = link && link.source && typeof link.source === 'object' ? link.source : null;
    const target = link && link.target && typeof link.target === 'object' ? link.target : null;
    if (!source || !target) return 'other';
    const sourceAnchor = source.system_anchor_id === undefined
      || source.system_anchor_id === null ? '' : String(source.system_anchor_id);
    const targetAnchor = target.system_anchor_id === undefined
      || target.system_anchor_id === null ? '' : String(target.system_anchor_id);
    if (!sourceAnchor || !targetAnchor) return 'other';
    if (sourceAnchor === String(target.id) || targetAnchor === String(source.id)) {
      return 'radial';
    }
    if (sourceAnchor !== targetAnchor) return 'other';
    return String(source.id) === sourceAnchor || String(target.id) === sourceAnchor
      ? 'radial' : 'internal';
  }

  function paintGalaxyAnchorAdornment(ctx, node, scale, accent, foreground) {
    if (!ctx || !node || !Number.isFinite(node.x) || !Number.isFinite(node.y)) return 0;
    const role = node.anchor_role;
    if (role !== 'global' && role !== 'community') return 0;
    const radius = finitePositive(node.radius, 3, 160)
      * (role === 'global' ? GALAXY_BLACK_HOLE_PAINT_SCALE : 1);
    const color = accent || node.color || '#9d7bff';
    const inverseScale = 1 / Math.max(0.1, Number(scale) || 1);
    if (role === 'community') {
      if (foreground) return 0;
      ctx.save();
      /* The cached Solar material paints the star itself. This background pass adds only a
         smooth, bounded corona; avoid low-resolution line-art rays and iconography. */
      if (typeof ctx.createRadialGradient === 'function') {
        const corona = ctx.createRadialGradient(
          node.x, node.y, radius * 0.72, node.x, node.y, radius * 2.45
        );
        corona.addColorStop(0, alpha('#fff4cf', 0.22));
        corona.addColorStop(0.34, alpha(color, 0.14));
        corona.addColorStop(1, alpha(color, 0));
        ctx.fillStyle = corona;
        ctx.beginPath(); ctx.arc(node.x, node.y, radius * 2.45, 0, 6.2832); ctx.fill();
      }
      ctx.strokeStyle = alpha('#ffe19a', 0.28);
      ctx.lineWidth = 0.6 * inverseScale;
      ctx.beginPath(); ctx.arc(node.x, node.y, radius * 1.32, 0, 6.2832); ctx.stroke();
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
        ctx.ellipse(node.x, node.y, radius * 1.72, radius * 0.62,
          -0.28 + galaxyBlackHoleSpinAngle(node), 0, 6.2832);
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
      settings: Object.assign({}, PRESETS.galaxy, {
        mode: 'galaxy', labels: false, flow: true, frozen: false,
        gravitationalConstant: GALAXY_GRAVITATIONAL_CONSTANT_MULTIPLIER,
        localGravitationalConstant: GALAXY_LOCAL_GRAVITATIONAL_CONSTANT_MULTIPLIER,
        blackHoleMass: GALAXY_BLACK_HOLE_MASS_MULTIPLIER,
        damping: 1,
        springStiffness: GALAXY_SPRING_STIFFNESS_MULTIPLIER,
        orbitPaused: false,
      }),
      minDegree: 1, showUnlinked: true, focusId: null, depth: 2, layers: { temporal: true, entity: true, causal: true, semantic: true, code: false },
      path: null, asOf: null, ghost: true, sizeBy: 'mass', bridges: false, suggestions: false,
      collapse: 'auto', renderMode: opts.renderMode === 'full' || opts.renderMode === 'all' ? 'full' : 'overview'
    };
    let raw = { nodes: [], links: [], suggestions: [], communities: [], community_bridges: [], meta: {} };
    /* Only anchors with more than two direct orbiting nodes are painted as stars. Smaller
       systems and singleton communities keep the ordinary node material. */
    let galaxyVisibleStarIds = new Set();
    /* Every visible body with at least one direct orbiter is a primary rendering landmark.
       This includes planets with moons without incorrectly turning them into stars. */
    let galaxyPrimaryNodeIds = new Set();
    const galaxyServerPhase = new Map();
    const galaxySavedPhase = new Map();
    /* Mode restoration is a transactional hand-off: a same-task freeze must still expose the
       saved phase byte-for-byte after the render's safety projections. */
    let galaxyPhaseRestorePending = false;
    let preserveGalaxyPhaseOnResume = false;
    let galaxyContactCorrectionDeferred = false;
    let adj = Object.create(null), liveAdj = Object.create(null), hilite = null, hoverSet = null, maxDeg = 1;
    let legacySizeBy = 'degree';
    // The classic renderer treats label density as a hard ranked cap, not merely a looser
    // degree threshold. Keeping chosen IDs outside the paint callback bounds fillText work.
    let labelIds = new Set();
    let pendingLabels = [];
    let zoom = 1, collapsed = false;
    /* Recomputed from the *rendered* data on every render, exactly as the classic path
       recomputes GPERF — filters and focus can take a huge store down to a small view. */
    let large = false, dense = false, materialLow = false;
    let staticFullLayout = false, fullLayoutDirty = true;
    /* The node/link arrays last handed to force-graph. Seeding is not free: the vendor copies
       the data in and d3 resets the simulation alpha to 1, so a paint-only change would restart
       the whole layout. See `sameData`/`render`. */
    let seeded = null;
    let clusterExpandTimer = 0;
    let destroyed = false, running = true, fitTimer = 0, suspended = 0, pendingRender = null;
    let physicsFrame = 0, physicsReheatPending = false;
    let galaxyFrame = 0, galaxyLastFrameTime = null, galaxyAccumulator = 0;
    let galaxyFrames = 0, galaxySteps = 0, galaxyLastSubsteps = 0;
    let galaxyReheatStepsRemaining = 0, galaxyReheatActivations = 0;
    let galaxyReheatStepsApplied = 0, galaxyLastReheatSubsteps = 0, galaxyKinematicSteps = 0;
    let galaxyLastKinetic = 0, galaxyLastCollisions = 0, galaxyLastRelationCorrections = 0;
    let galaxyLastRelationDistance = 0, galaxyLastOrbitalRelationSkips = 0;
    let galaxyLastOrbitalSeparations = 0;
    let galaxyLastCrossSystemSeparations = 0;
    let galaxyLastSystemPacking = {
      systems: 0, overlaps: 0, adjustedSystems: 0, remainingOverlaps: 0,
      infeasiblePairs: 0, correctionDistance: 0, maximumShift: 0,
      gap: GALAXY_SYSTEM_PACKING_GAP,
    };
    let galaxyLastLocalOrbitBoundary = {
      systems: 0, members: 0, correctedNodes: 0, correctedDescendants: 0,
      correctionDistance: 0, maximumShift: 0, outwardVelocityRemoved: 0,
      maximumBoundaryRatioBefore: 0, maximumBoundaryRatioAfter: 0,
    };
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
      systems: 0, moved: 0, ratio: 1, maximumShift: 0,
      velocityAdjusted: 0, maximumVelocityShift: 0, anchorId: null,
    };
    let galaxyLastSpacetime = {
      anchorId: null, systems: 0, coreNodes: 0, warpedNodes: 0,
      maximumWarp: 0, maximumFrameDragAcceleration: 0,
      maximumHorizonAcceleration: 0, tidalSystems: 0, tidalPlanets: 0,
      maximumTidalAcceleration: 0,
    };
    let galaxyLastEventHorizonDecay = {
      anchorId: null, systems: 0, nodes: 0, maximumWarp: 0,
      maximumVelocityRemoved: 0,
    };
    let galaxyLastCarrierOrbitSupport = {
      anchorId: null, eligible: 0, supported: 0, coreEligible: 0, coreSupported: 0,
      minTangentialSpeed: null, coreMinTangentialSpeed: null,
      maximumRadialSpeed: 0, maximumVelocityCorrection: 0, corrected: 0,
      meanAngularVelocity: 0,
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
    let dragReleaseVelocity = null;
    let lastSlingshotRelease = null;

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
      if (state.settings.mode === 'galaxy') {
        const graph = fg.graphData ? fg.graphData() : null;
        const nodes = graph && graph.nodes ? graph.nodes : [];
        const anchor = galaxyGlobalAnchor(nodes);
        if (anchor && Number.isFinite(anchor.x) && Number.isFinite(anchor.y)) {
          /* Reserve each complete stellar envelope, not only every body's current phase. A
             planet that starts on the inward side later sweeps to the outward side without
             changing its system lane; fitting its current coordinate would clip that phase. */
          const diskRadius = galaxySystemEnvelopes(nodes, {
            respectFixedCoordinates: false,
          }).reduce((maximum, system) => Math.max(maximum,
            Math.hypot(system.anchor.x - anchor.x, system.anchor.y - anchor.y)
              + system.radius), 1);
          const available = Math.max(1, Math.min(width, height) - 2 * padding);
          fg.centerAt(anchor.x, anchor.y, duration);
          /* Reserve a small paint/camera margin for trails, labels and sub-pixel transforms;
             the physical lane projector keeps carriers inside this stable disk afterward. */
          fg.zoom(Math.min(MAX_AUTO_FIT_ZOOM, available / (diskRadius * 2.3)), duration);
          return;
        }
      }
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
          _live_members: 0, anchor_role: null
        });
        const group = groups.get(c);
        if (n.anchor_role === 'global') group.anchor_role = 'global';
        else if (n.anchor_role === 'community' && group.anchor_role !== 'global') {
          group.anchor_role = 'community';
        }
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
      /* Spacetime-tuned multipliers: the user reaches these via the Galactic gravity, Black hole
         mass, and Local solar gravity sliders. In non-galaxy mode the d3-force simulator is the
         only consumer, so the multipliers must reach the d3 forces directly.

         The dashboard normalizes these settings in
         ledger.js::graphSpacetimeEngineSettings() to a clean 0..2 range with the visible
         default at 1.0x. Consume the normalized values directly as the multipliers. A
         user-moved 0 reaches the engine as 0 (no force), the default 1.0 (no change), and
         the high end 2.0 (double force). The `Number.isFinite` check handles the *missing*
         case: if ledger.js never supplied a value (the engine was constructed without the
         dashboard wiring), fall back to the neutral 1.0x multiplier so the layout does
         not collapse. */
      const gcRaw = Number(state.settings.gravitationalConstant);
      const lgcRaw = Number(state.settings.localGravitationalConstant);
      const bhmRaw = Number(state.settings.blackHoleMass);
      /* The dashboard adapter emits 0..4 for gravity/local (raw/50) and up to 4.4 for
         mass, so clamping at 2 saturated the upper half of all three sliders (PR #177
         review threads at this site). Accept the full emitted engine ranges. */
      const gravityMultiplier = Number.isFinite(gcRaw) ? clamp(gcRaw, 0, 4) : 1;
      const massMultiplier = Number.isFinite(bhmRaw) ? clamp(bhmRaw, 0, 4.4) : 1;
      const localMultiplier = Number.isFinite(lgcRaw) ? clamp(lgcRaw, 0, 4) : 1;
      const baseRepel = mode === 'communities' ? Math.max(10, s.repel * 0.68) : s.repel;
      /* Galactic gravity is an attractive control. Keep the separate Repel slider on the
         negative many-body charge, and apply this multiplier to the attractive anchor forces
         below so increasing gravity tightens the layout instead of spreading it apart. */
      if (charge && charge.strength) charge.strength(-baseRepel);
      if (link && link.distance) link.distance(s.link);
      if (link && link.strength) link.strength(edge => {
        const source = typeof edge.source === 'object' ? edge.source : layoutById.get(linkEndpoint(edge, 'source'));
        const target = typeof edge.target === 'object' ? edge.target : layoutById.get(linkEndpoint(edge, 'target'));
        const base = 1 / Math.max(1, Math.min(
          source && source.degree || 1, target && target.degree || 1
        ));
        return base * localMultiplier;
      });
      /* Space friction (the dashboard's "damping" slider) maps onto d3's velocityDecay. The
         slider's 0..15 visible range must reach the full d3 decay range so the lower quarter
         is not inert. At the default (slider=1) the size-aware baseline (0.38 small / 0.45
         large) is the neutral settling behaviour, so the slider's effect is a *multiplier*
         on that baseline, not a replacement. Above 1 the layout settles harder, below 1
         it stays more elastic. */
      /* Space friction (the dashboard's "damping" slider) maps onto d3's velocityDecay.
         The slider's 0..15 visible range must reach the full d3 decay range so the lower
         quarter is not inert. At the default (slider=1) the size-aware baseline (0.38 small
         / 0.45 large) is the neutral settling behaviour, so the slider's effect is a
         *multiplier* on that baseline, not a replacement. Above 1 the layout settles
         harder, below 1 it stays more elastic, down to the d3 floor 0.05 at 0. */
      if (fg.d3VelocityDecay) {
        const dampingRaw = Number(state.settings.damping);
        const damping = Number.isFinite(dampingRaw) ? clamp(dampingRaw, 0, 15) : 1;
        const baseline = large ? 0.45 : 0.38;
        const floor = 0.05;
        const ceiling = 0.85;
        const target = damping <= 1
          ? floor + (baseline - floor) * damping
          : baseline + (ceiling - baseline) * (damping - 1) / 14;
        fg.d3VelocityDecay(clamp(target, floor, ceiling));
      }
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
           structure installed above. Black-hole mass multiplies the centering strength so
           the slider visibly pulls nodes toward the origin. */
        const centering = Math.max(0.04, (Number(s.gravity) || 0) / 100)
          * massMultiplier * gravityMultiplier;
        fg.d3Force('x', d3.forceX(0).strength(centering));
        fg.d3Force('y', d3.forceY(0).strength(centering));
      } else if (mode === 'radial' && d3.forceRadial) {
        const outerRadius = Math.max(180, Math.min(360, Math.sqrt(Math.max(1, layoutNodes.length)) * 18 + (Number(s.link) || 16) * 4));
        const degreeScale = Math.max(1, maxOf(layoutNodes.map(node => node.degree || 0), 1));
        const centering = Math.max(0.05, (Number(s.gravity) || 0) / 500)
          * massMultiplier * gravityMultiplier;
        fg.d3Force('x', d3.forceX(0).strength(centering));
        fg.d3Force('y', d3.forceY(0).strength(centering));
        fg.d3Force('radial', d3.forceRadial(node => {
          const hubness = Math.max(0, Math.min(1, (node.degree || 0) / degreeScale));
          return 34 + (outerRadius - 34) * (1 - hubness);
        }).strength(0.72 * gravityMultiplier));
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
        /* Black-hole mass scales the constellation's anchor strength so the slider is
           visible in this preset too. */
        const targetStrength = 0.18 * massMultiplier * gravityMultiplier;
        fg.d3Force('x', d3.forceX(node => target(node).x).strength(targetStrength));
        fg.d3Force('y', d3.forceY(node => target(node).y).strength(targetStrength));
      } else {
        const baseCentering = mode === 'compact'
          ? Math.max(0.24, (Number(s.gravity) || 0) / 100)
          : Math.max(0.06, (Number(s.gravity) || 0) / 100);
        /* Black-hole mass scales the centering so the slider pulls compact and original
           layouts toward the origin in proportion to its setting. */
        const centering = baseCentering * massMultiplier * gravityMultiplier;
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
      const control = (value, fallback, min, max) => Number.isFinite(Number(value))
        ? clamp(value, min, max) : fallback;
      const coreAttraction = control(s.gravitationalConstant, 1, 0, 2);
      const coreMass = control(s.blackHoleMass, 1, 0, 2);
      const clusterCohesion = control(s.localGravitationalConstant, 1, 0, 2);
      const settlingResistance = control(s.damping, 1, 0, 15);
      const linkSpring = control(s.springStiffness, 1, 0, 100 / 32);
      /* Keep zero-force endpoints finite without flattening the lower slider range. The 0.5
         baseline preserves a neutral scale of one at the default product, while the explicit
         bounded response caps a zero product at sqrt(2) instead of spreading the layout across
         millions of world units. */
      const responseScale = product => {
        const magnitude = Math.max(0, Number(product) || 0);
        return 1 / Math.sqrt(0.5 + 0.5 * magnitude);
      };
      const coreScale = responseScale(coreAttraction * coreMass);
      const cohesionScale = responseScale(clusterCohesion * linkSpring);
      const settlingScale = 1 + (settlingResistance - 1) * 0.02;
      const layoutPhysicsScale = coreScale * cohesionScale * settlingScale;
      const localGap = (4 + nodeSize * 1.6 + Math.sqrt(repel) * 0.8 + link * 0.16)
        * compactness * layoutPhysicsScale;
      const columns = Math.max(1, Math.ceil(Math.sqrt(ordered.length)));
      const largestGroup = ordered.reduce((largest, [, nodes]) => Math.max(largest, nodes.length), 1);
      const cell = Math.max(90, Math.sqrt(largestGroup) * localGap * 2.4 + link * 3)
        * compactness * Math.max(0.5, Math.sqrt(layoutPhysicsScale));
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

    function nodePaintRadius(node) {
      const radius = Number(node && node.radius);
      if (!Number.isFinite(radius)) return 0;
      return radius * (state.settings.mode === 'galaxy' && node.anchor_role === 'global'
        ? GALAXY_BLACK_HOLE_PAINT_SCALE : 1);
    }

    function styleNode(node, ctx, scale) {
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) return;
      const focus = hoverSet && hoverSet.size > 1, neighbor = focus && hoverSet.has(node.id), dim = focus && !neighbor;
      let r = nodePaintRadius(node);
      const col = node.color;
      const spacetimeFade = state.settings.mode === 'galaxy' && node.anchor_role !== 'global'
        ? 1 - 0.55 * Math.max(0, Math.min(1, Number(node.__galaxySpacetimeWarp) || 0))
        : 1;
      ctx.globalAlpha = (node.ghost ? 0.22 : (dim ? 0.12 : 1)) * spacetimeFade;
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
        pendingLabels.push({ x: node.x, y: node.y + r * 1.5 + r * 0.5, text: nodeName(node), cluster: true, scale, r });
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
        && galaxyAnchorAdornmentEligible(node, galaxyVisibleStarIds);
      const galaxyPrimary = state.settings.mode === 'galaxy'
        && (node.anchor_role === 'global' || galaxyPrimaryNodeIds.has(String(node.id)));
      const communityStar = galaxyAnchor && node.anchor_role === 'community';
      if (galaxyAnchor) paintGalaxyAnchorAdornment(
        ctx, node, scale, state.themeColors.accent || col, false
      );
      if (communityStar) {
        /* A real multi-planet star gets the same oversampled gradient/grain/bezel pipeline as
           every premium node surface. Only its recipe changes; geometry and hit area do not. */
        const stellarIdentity = mixColours(col, '#ffd166', 0.72);
        nodeMaterial = materialRecipe(
          'solar', state.themeColors, 'stellar', stellarIdentity
        );
        paintMaterialSurface(ctx, node.x, node.y, r, scale, nodeMaterial, materialLow, true);
      } else if (state.styleName === 'galaxy') {
        nodeMaterial = materialRecipe('galaxy', state.themeColors, state.palette, col);
        paintMaterialSurface(ctx, node.x, node.y, r, scale, nodeMaterial,
          materialLow, galaxyPrimary);
      } else if (state.styleName === 'solar') {
        const sun = node.rank === 0;
        nodeMaterial = materialRecipe(
          'solar', state.themeColors, state.palette,
          sun ? mixColours(col, '#d38b43', 0.46) : col
        );
        paintMaterialSurface(ctx, node.x, node.y, r, scale, nodeMaterial,
          materialLow, galaxyPrimary);
      } else if (state.styleName === 'cyber') {
        /* Cyberpunk owns a broad, fixed cyan→violet→magenta PVD face. Palette colour is kept
           out of that film and appears only in the slim identity ring. */
        nodeMaterial = materialRecipe('cyber', state.themeColors, state.palette, col);
        paintMaterialSurface(ctx, node.x, node.y, r, scale, nodeMaterial,
          materialLow, galaxyPrimary);
      } else {
        nodeMaterial = materialRecipe('classic', state.themeColors, state.palette, col);
        paintMaterialSurface(ctx, node.x, node.y, r, scale, nodeMaterial,
          materialLow, galaxyPrimary);
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
      if (showLabel && scale > 0.35) {
        pendingLabels.push({
          x: node.x + r + 1.6, y: node.y, r, text: nodeName(node),
          isHilite: node.id === hilite, scale,
        });
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
      /* `nodeCanvasObject` is a non-updating accessor in force-graph. Reinstalling the same
         callback changes no vendor state, so a Galaxy frame could advance every coordinate
         while the visible canvas stayed on its previous paint. The camera setter is the
         supported redraw invalidation path: setting the current zoom marks `needsRedraw` and
         leaves the camera transform byte-for-byte unchanged. Keep the callback fallback for
         embedders whose graph stub does not expose a readable zoom value. */
      const currentZoom = typeof fg.zoom === 'function' ? fg.zoom() : NaN;
      if (Number.isFinite(currentZoom) && typeof fg.zoom === 'function') {
        fg.zoom(currentZoom);
      } else if (typeof fg.nodeCanvasObject === 'function') {
        fg.nodeCanvasObject(fg.nodeCanvasObject());
      }
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
      /* Galaxy's O(n) kinematic fallback keeps even Complete views moving without the live
         pair solver. Keep it expanded by default; an explicit Collapse control still selects
         the lightweight cluster overview. */
      return state.settings.mode !== 'galaxy';
    }

    function galaxyDynamicsEligible() {
      if (!hasBrowserFrameClock || destroyed || !running || pageHidden()) return false;
      if (state.settings.mode !== 'galaxy' || state.settings.frozen
        || state.settings.orbitPaused === true) return false;
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
      galaxyLastSystemPacking = {
        systems: 0, overlaps: 0, adjustedSystems: 0, remainingOverlaps: 0,
        infeasiblePairs: 0, correctionDistance: 0, maximumShift: 0,
        gap: GALAXY_SYSTEM_PACKING_GAP,
      };
      galaxyLastLocalOrbitBoundary = {
        systems: 0, members: 0, correctedNodes: 0, correctedDescendants: 0,
        correctionDistance: 0, maximumShift: 0, outwardVelocityRemoved: 0,
        maximumBoundaryRatioBefore: 0, maximumBoundaryRatioAfter: 0,
      };
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
      galaxyKinematicSteps = 0;
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
        systems: 0, moved: 0, ratio: 1, maximumShift: 0,
        velocityAdjusted: 0, maximumVelocityShift: 0, anchorId: null,
      };
      galaxyLastSpacetime = {
        anchorId: null, systems: 0, coreNodes: 0, warpedNodes: 0,
        maximumWarp: 0, maximumFrameDragAcceleration: 0,
        maximumHorizonAcceleration: 0, tidalSystems: 0, tidalPlanets: 0,
        maximumTidalAcceleration: 0,
      };
      galaxyLastEventHorizonDecay = {
        anchorId: null, systems: 0, nodes: 0, maximumWarp: 0,
        maximumVelocityRemoved: 0,
      };
      galaxyLastCarrierOrbitSupport = {
        anchorId: null, eligible: 0, supported: 0, coreEligible: 0, coreSupported: 0,
        minTangentialSpeed: null, coreMinTangentialSpeed: null,
        maximumRadialSpeed: 0, maximumVelocityCorrection: 0, corrected: 0,
        meanAngularVelocity: 0,
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
      const orbitalSpeed = galaxyOrbitalSpeedMultiplier(state.settings.repel);
      /* The repurposed control owns angular velocity; keep the physical contact cushion neutral. */
      const orbitalSeparationPadding = galaxyOrbitalSeparationPadding(
        GALAXY_ORBITAL_SEPARATION_BASE_SETTING);
      const orbitalSeparationStrength = galaxyOrbitalSeparationStrength(
        GALAXY_ORBITAL_SEPARATION_BASE_SETTING);
      return {
        fixedNodeId: activeDragNode ? activeDragNode.id : null,
        orbitalSpeed: state.settings.repel,
        layoutSeed: raw.meta && raw.meta.layout_seed !== undefined ? raw.meta.layout_seed : 0,
        dragSource: activeDragNode,
        dragFollowers,
        dragSoftening: activeDragNode ? Math.max(GALAXY_DRAG_GRAVITY_SOFTENING,
          finitePositive(activeDragNode.radius, 2, 160) * 1.5) : GALAXY_DRAG_GRAVITY_SOFTENING,
        gravity: state.settings.gravity,
        localGravitySetting: GALAXY_FIXED_LOCAL_GRAVITY_SETTING,
        /* The dashboard normalises the three spacetime sliders to a 0..2 range
           (default 1.0). Preserve that normalized value at the Galaxy boundary:
           the downstream multiplier helpers clamp their own direct-call range,
           and multiplying here made the default field 4x/8x stronger than the
           value shown by the controls. */
        gravitationalConstant: galaxyPhysicsMultiplier(
          state.settings.gravitationalConstant, GALAXY_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8),
        localGravitationalConstant: galaxyPhysicsMultiplier(
          state.settings.localGravitationalConstant,
          GALAXY_LOCAL_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8),
        blackHoleMass: galaxyPhysicsMultiplier(
          state.settings.blackHoleMass, GALAXY_BLACK_HOLE_MASS_MULTIPLIER, 16),
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
        /* Hooke acceleration is the cohesive topology force; its existing force and
           acceleration caps keep dense hubs bounded. Authored star/planet links remain skipped
           so stellar gravity owns orbital radii. The later contractive PBD pass is only the
           finite-distance safety net for a pathological large error. */
        includeRelationSprings: true,
        orbitScale,
        linkSetting: state.settings.link,
        relationStrengthMultiplier: GALAXY_RELATION_STRENGTH_MULTIPLIER,
        relationForceCap: GALAXY_RELATION_FORCE_CAP,
        relationAccelerationCap: GALAXY_RELATION_ACCELERATION_CAP,
        /* PBD uses one contractive exponential response. Scaling the completed displacement
           above one would cross the target and ping-pong on the next frame. */
        relationConstraintStrengthMultiplier:
          GALAXY_RELATION_CONSTRAINT_STRENGTH_MULTIPLIER * 0.18
          * galaxyPhysicsMultiplier(state.settings.springStiffness,
            GALAXY_SPRING_STIFFNESS_MULTIPLIER, 8),
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
        /* Complete system envelopes own cross-community clearance below.  Leaving node-pair
           pressure active at the same time double-corrects dense contacts and produces the
           visible jitter/reheating that rigid carrier translation is meant to eliminate. */
        crossCommunitySeparationStrength: 0,
        /* A pointer-owned source must be the only moving layout authority. Re-packing every
           other complete envelope during a drag can move an unrelated system sideways or away
           from the dragged mass, masking the bounded gravitational follower field. */
        /* Authored Galaxy scenes are admitted to non-intersecting co-rotating rings once.
           Repacking those managed carriers during their orbit causes visible teleportation. */
        includeSystemPacking: false,
        systemPackingGap: GALAXY_SYSTEM_PACKING_GAP,
        systemPackingStrength: GALAXY_SYSTEM_PACKING_STRENGTH,
        systemPackingMaxCorrection: GALAXY_SYSTEM_PACKING_MAX_CORRECTION,
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
        /* Black-hole gravity and the supported carrier tangent advance a bounded orbit.
           Monotone inward projection destroys angular momentum and re-stacks clear lanes. */
        inwardConvergence: false,
        inwardGravitySetting: state.settings.gravity,
        /* Live Galaxy owns the carrier position phase even when a filtered payload skipped
           one-shot lane admission. Low-level helper callers retain force-only semantics unless
           they opt into this browser clock contract. */
        wallClockSeconds: GALAXY_FRAME_INTERVAL_MS / 1000,
        velocityDecay: GALAXY_VELOCITY_DECAY
          * galaxyPhysicsMultiplier(state.settings.damping, 1, 100),
        includeSpacetime: true,
        frameDraggingFraction: GALAXY_FRAME_DRAGGING_FRACTION,
        frameDraggingMaxAcceleration: GALAXY_FRAME_DRAGGING_MAX_ACCELERATION,
        eventHorizonInfluenceScale: GALAXY_EVENT_HORIZON_INFLUENCE_SCALE,
        eventHorizonDecayRate: GALAXY_EVENT_HORIZON_DECAY_RATE,
        eventHorizonInwardAcceleration: GALAXY_EVENT_HORIZON_INWARD_ACCELERATION,
        tidalStrengthFraction: GALAXY_TIDAL_STRENGTH_FRACTION,
        tidalAccelerationCap: GALAXY_TIDAL_ACCELERATION_CAP,
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
      const orbitalSpeed = galaxyOrbitalSpeedMultiplier(state.settings.repel);
      const diagnosticAnchor = galaxyGlobalAnchor(data.nodes || []);
      /* Keep diagnostics on the same calibrated scalar as galaxyBlackHoleField without
         rebuilding the full O(n) field on every physics callback. Previously these values
         bypassed blackHoleMass, so the control could change force while diagnostics reported
         a constant gravity amount. */
      const diagnosticMass = galaxyPhysicsMultiplier(state.settings.blackHoleMass,
        GALAXY_BLACK_HOLE_MASS_MULTIPLIER, 16);
      const effectiveGravity = galaxyBlackHoleGravityConstant(state.settings.gravity, true)
        * galaxyPhysicsMultiplier(state.settings.gravitationalConstant,
          GALAXY_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8)
        * Math.sqrt(Math.max(0.25, diagnosticMass));
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
        kinematicFallback: staticFullLayout || collapsed,
        oversizedKinematic: staticFullLayout,
        reducedMotion: reduced(),
        hidden: pageHidden(),
        orbitPaused: state.settings.orbitPaused === true,
        dragging: activeDragNode ? activeDragNode.id : null,
        /* Every live body is admitted to the pointer-owned gravity field. Relation and local
           annotations remain visible here, but topology never gates the physical response. */
        dragFollowers: dragFollowers.map(follower => follower.node.id),
        dragFollowerGravity: { ...dragFollowerGravityReport },
        gravitySetting: state.settings.gravity,
        gravityStrengthMultiplier: galaxyGravityStrengthMultiplier(state.settings.gravity),
        gravityResponseRateMultiplier: GALAXY_GRAVITY_RESPONSE_RATE_MULTIPLIER,
        /* The two normalized controls are independent: G_center owns black-hole and
           inter-system motion, while G_star scales the calibrated dominant-star wells. */
        gravitationalConstant: galaxyPhysicsMultiplier(state.settings.gravitationalConstant,
          GALAXY_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8),
        G_center: galaxyPhysicsMultiplier(state.settings.gravitationalConstant,
          GALAXY_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8),
        localGravitationalConstant: galaxyPhysicsMultiplier(
          state.settings.localGravitationalConstant,
          GALAXY_LOCAL_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8),
        G_star: galaxyPhysicsMultiplier(state.settings.localGravitationalConstant,
          GALAXY_LOCAL_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8),
        globalAnchorId: diagnosticAnchor ? diagnosticAnchor.id : null,
        globalAnchorLabel: diagnosticAnchor ? nodeName(diagnosticAnchor) : null,
        blackHoleSpinAngle: diagnosticAnchor ? galaxyBlackHoleSpinAngle(diagnosticAnchor) : 0,
        blackHoleMass: diagnosticMass,
        damping: galaxyPhysicsMultiplier(state.settings.damping, 1, 100),
        springStiffness: galaxyPhysicsMultiplier(state.settings.springStiffness,
          GALAXY_SPRING_STIFFNESS_MULTIPLIER, 8),
        effectiveGravity,
        blackHoleGravity: effectiveGravity,
        localGravity: galaxyLocalGravityConstant(GALAXY_FIXED_LOCAL_GRAVITY_SETTING),
        effectiveLocalGravity: galaxyStellarGravityConstant(GALAXY_FIXED_LOCAL_GRAVITY_SETTING)
          * galaxyPhysicsMultiplier(state.settings.localGravitationalConstant,
            GALAXY_LOCAL_GRAVITATIONAL_CONSTANT_MULTIPLIER, 8),
        immediateGravityResponse: { ...galaxyLastGravityResponse },
        systemGravity: { ...galaxyLastSystemGravity },
        mutualSystemGravity: { ...galaxyLastMutualGravity },
        spacetime: { ...galaxyLastSpacetime },
        tidal: {
          systems: galaxyLastSpacetime.tidalSystems || 0,
          planets: galaxyLastSpacetime.tidalPlanets || 0,
          maximumAcceleration: galaxyLastSpacetime.maximumTidalAcceleration || 0,
        },
        eventHorizonDecay: { ...galaxyLastEventHorizonDecay },
        carrierOrbitSupport: { ...galaxyLastCarrierOrbitSupport },
        coreOrbitSupport: {
          eligible: galaxyLastCarrierOrbitSupport.coreEligible || 0,
          supported: galaxyLastCarrierOrbitSupport.coreSupported || 0,
          minTangentialSpeed: galaxyLastCarrierOrbitSupport.coreMinTangentialSpeed,
        },
        linkSetting: state.settings.link,
        relationOrbitScale: galaxyRelationOrbitScale(state.settings.link),
        relationStrengthMultiplier: GALAXY_RELATION_STRENGTH_MULTIPLIER,
        relationForceCap: GALAXY_RELATION_FORCE_CAP,
        relationAccelerationCap: GALAXY_RELATION_ACCELERATION_CAP,
        relationConstraintStrengthMultiplier:
          GALAXY_RELATION_CONSTRAINT_STRENGTH_MULTIPLIER * 0.18
          * galaxyPhysicsMultiplier(state.settings.springStiffness,
            GALAXY_SPRING_STIFFNESS_MULTIPLIER, 8),
        relationConstraintResponseMultiplier:
          GALAXY_RELATION_CONSTRAINT_RESPONSE_MULTIPLIER,
        relationConstraintMaxCorrection:
          GALAXY_RELATION_CONSTRAINT_MAX_CORRECTION,
        orbitalSpeedSetting: state.settings.repel,
        orbitalSpeedMultiplier: orbitalSpeed,
        orbitalRadiusMultiplier: galaxyOrbitalRadiusMultiplier(state.settings.repel),
        /* Compatibility diagnostics retain the old names for saved-view tooling. */
        orbitalSeparationSetting: state.settings.repel,
        orbitalSeparationPadding: galaxyOrbitalSeparationPadding(
          GALAXY_ORBITAL_SEPARATION_BASE_SETTING),
        orbitalSeparationStrength: galaxyOrbitalSeparationStrength(
          GALAXY_ORBITAL_SEPARATION_BASE_SETTING),
        crossSystemRepulsionPadding: GALAXY_CROSS_SYSTEM_REPULSION_PADDING,
        crossSystemRepulsionStrength: 0,
        localOrbitBoundarySlack: GALAXY_LOCAL_ORBIT_BOUNDARY_SLACK,
        localOrbitBoundary: { ...galaxyLastLocalOrbitBoundary },
        systemPacking: { ...galaxyLastSystemPacking },
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
        velocityDecay: GALAXY_VELOCITY_DECAY
          * galaxyPhysicsMultiplier(state.settings.damping, 1, 100),
        frames: galaxyFrames,
        steps: galaxySteps,
        kinematicSteps: galaxyKinematicSteps,
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
        systemOrbitSeedSpeedLimit: GALAXY_SYSTEM_ORBIT_SEED_SPEED_LIMIT
          * GALAXY_AUTHORED_CARRIER_ORBIT_CLOCK,
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
        galaxyPhaseRestorePending = false;
        const data = fg.graphData() || { nodes: [], links: [] };
        for (let index = 0; index < substeps; index++) {
          const kinematicFallback = staticFullLayout || collapsed;
          const report = kinematicFallback
            ? advanceGalaxyKinematicOrbits(data.nodes || [], galaxyIntegratorOptions())
            : integrateGalaxyLeapfrog(
              data.nodes || [], data.links || [], raw.community_bridges || [],
              galaxyIntegratorOptions()
            );
          if (!kinematicFallback) {
            report.orbitalSpeed = applyGalaxyOrbitalSpeedControl(
              data.nodes || [], galaxyIntegratorOptions());
          }
          galaxySteps++;
          if (kinematicFallback) {
            galaxyKinematicSteps++;
            galaxyLastKinetic = galaxyMotionDiagnostics(data.nodes || []).kineticEnergy;
            galaxyLastCollisions = 0;
            galaxyLastRelationCorrections = 0;
            galaxyLastRelationDistance = 0;
            galaxyLastOrbitalRelationSkips = 0;
            galaxyLastOrbitalSeparations = 0;
            galaxyLastCrossSystemSeparations = 0;
            galaxyLastSystemPacking = report.systemPacking || galaxyLastSystemPacking;
            galaxyLastLocalOrbitBoundary = report.localOrbitBoundary
              || galaxyLastLocalOrbitBoundary;
            galaxyLastOrbitalCorrection = 0;
            galaxyLastLocalVelocityLimits = 0;
          } else {
            galaxyLastKinetic = report.kinetic;
            galaxyLastCollisions = report.collisions;
            galaxyLastRelationCorrections = report.relationConstraint.applied;
            galaxyLastRelationDistance = report.relationConstraint.correctedDistance;
            galaxyLastOrbitalRelationSkips = report.relationConstraint.skippedOrbitalSystem || 0;
            galaxyLastOrbitalSeparations = report.orbitalSeparation.overlaps;
            galaxyLastCrossSystemSeparations =
              report.orbitalSeparation.crossCommunityOverlaps || 0;
            galaxyLastSystemPacking = report.systemPacking || galaxyLastSystemPacking;
            galaxyLastLocalOrbitBoundary = report.localOrbitBoundary
              || galaxyLastLocalOrbitBoundary;
            galaxyLastOrbitalCorrection = report.orbitalSeparation.correctionDistance;
            galaxyLastSystemAnchorExclusion = report.systemAnchorExclusion;
            galaxyLastBlackHoleExclusion = report.blackHoleExclusion;
            galaxyLastFarFieldConfinement = report.farFieldConfinement;
            galaxyLastFarFieldGravity = report.farFieldGravity;
            galaxyLastLocalVelocityLimits = report.systemVelocity.limitedSystems;
            galaxyLastSystemGravity = report.systemGravity;
            galaxyLastMutualGravity = report.mutualGravity;
            galaxyLastSpacetime = report.spacetime;
            galaxyLastEventHorizonDecay = report.eventHorizonDecay;
            galaxyLastCarrierOrbitSupport = report.carrierOrbitSupport
              || galaxyLastCarrierOrbitSupport;
            dragFollowerGravityReport = report.dragGravity;
            if (report.speedCapped) galaxySpeedCaps++;
          }
        }
        galaxyAccumulator = Math.max(0,
          galaxyAccumulator - ordinarySubsteps * GALAXY_FRAME_INTERVAL_MS);
        galaxyReheatStepsRemaining = Math.max(0,
          galaxyReheatStepsRemaining - reheatSubsteps);
        galaxyReheatStepsApplied += reheatSubsteps;
        galaxyFrames++;
        invalidate();
        if (typeof opts.onPhysics === 'function') opts.onPhysics(physicsDiagnostics());
        if (typeof opts.onPhysicsFrame === 'function') opts.onPhysicsFrame(api.getPhysicsSnapshot());
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
        galaxyPhaseRestorePending = true;
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
        galaxyContactCorrectionDeferred = false;
        render(false, true);
        return;
      }
      /* In galaxy mode the d3 reheat is a no-op for layout: galaxy owns the integrator and
         setSettings already scaled carriers. The follow-up render still repaints and
         re-asserts the contact-correction invariant, but those corrections are
         path-dependent on intermediate slider phase and would undo a burst sweep.
         Phase-preserve the contact-correction pass on the very next render
         so the immediate response survives until the live integrator ticks. */
      const phaseLock = state.settings.mode === 'galaxy';
      physicsFrame = requestFrame(() => {
        physicsFrame = 0;
        if (destroyed || suspended || !physicsReheatPending) return;
        physicsReheatPending = false;
        if (phaseLock) {
          preserveGalaxyPhaseOnResume = true;
          /* Apply the painted-edge projection once, after the browser has coalesced the
             complete input burst. Intermediate projections would make the final layout depend
             on how many range-input events happened before this frame. */
          galaxyContactCorrectionDeferred = false;
        }
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
      if (state.settings.labels) {
        const labelCap = Math.max(1, Math.round(Number(state.settings.labelDensity) || 40));
        labelIds = new Set(data.nodes
          .filter(n => !n.cluster && !n.ghost)
          .sort((a, b) => (b.degree || 0) - (a.degree || 0)
            || (b.betweenness || 0) - (a.betweenness || 0)
            || String(a.id).localeCompare(String(b.id)))
          .slice(0, labelCap)
          .map(n => n.id));
      } else labelIds = new Set();
      applyChrome();
      /* graphData() synchronously runs configured warmup ticks. Detach the legacy simulation
         before handing it restored Galaxy coordinates, or Compact's old link/charge field gets
         one last chance to corrupt the physical phase before the custom clock even starts. */
      if (galaxyMode) disableD3GalaxyIntegration();
      if (!reused) {
        if (staticFullLayout) {
          if (galaxyMode) {
            pinGalaxySceneLayout(data);
            seedGalaxyOrbits(
              data.nodes, raw.meta && raw.meta.layout_seed,
              state.settings.gravity, galaxyLiveSoftening(), reducedMotion,
              { fixedNodeId: activeDragNode ? activeDragNode.id : null,
                restorePhase: galaxyPhaseRestorePending,
                coreOnly: true,
                orbitalSpeed: state.settings.repel,
                gravitationalConstant: state.settings.gravitationalConstant,
                localGravitationalConstant: state.settings.localGravitationalConstant,
                localGravitySetting: GALAXY_FIXED_LOCAL_GRAVITY_SETTING }
            );
          } else pinFullGraphLayout(data);
          fullLayoutDirty = false;
        } else if (galaxyMode) {
          /* Preserve finite server coordinates; synthesize positions only for malformed embeds. */
          ensureGalaxyPositions(data.nodes, raw.meta && raw.meta.layout_seed);
          releasePinnedPositions(data);
          const authoredGalaxy = data.nodes.some(node => node.anchor_role === 'global')
            && data.nodes.filter(node => node.anchor_role === 'community').length > 1;
          if (authoredGalaxy) {
            establishGalaxyCarrierLanes(data.nodes, {
              gap: GALAXY_SYSTEM_PACKING_GAP,
              layoutSeed: raw.meta && raw.meta.layout_seed,
            });
            galaxyLastSystemPacking = applyGalaxySystemPacking(data.nodes, {
              gap: GALAXY_SYSTEM_PACKING_GAP,
              strength: 1,
              maxCorrection: Infinity,
              respectFixedCoordinates: false,
            });
          }
          seedGalaxyOrbits(
            data.nodes, raw.meta && raw.meta.layout_seed,
            state.settings.gravity, galaxyLiveSoftening(), reducedMotion,
            { fixedNodeId: activeDragNode ? activeDragNode.id : null,
              restorePhase: galaxyPhaseRestorePending,
              orbitalSpeed: state.settings.repel,
              gravitationalConstant: state.settings.gravitationalConstant,
              localGravitationalConstant: state.settings.localGravitationalConstant,
              localGravitySetting: GALAXY_FIXED_LOCAL_GRAVITY_SETTING }
          );
          seedGalaxySystemOrbits(
            data.nodes, raw.meta && raw.meta.layout_seed,
            state.settings.gravity, Math.max(36, galaxySoftening() * 5), reducedMotion,
            { gravitationalConstant: state.settings.gravitationalConstant,
              blackHoleMass: state.settings.blackHoleMass,
              orbitalSpeed: state.settings.repel,
              localGravitySetting: GALAXY_FIXED_LOCAL_GRAVITY_SETTING }
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
      const skipGalaxyReseed = preserveGalaxyPhaseOnResume;
      preserveGalaxyPhaseOnResume = false;
      if (reused && galaxyMode && !staticFullLayout && !skipGalaxyReseed) {
        seedGalaxyOrbits(
          data.nodes, raw.meta && raw.meta.layout_seed,
          state.settings.gravity, galaxyLiveSoftening(), reducedMotion,
          { fixedNodeId: activeDragNode ? activeDragNode.id : null,
            restorePhase: galaxyPhaseRestorePending,
            orbitalSpeed: state.settings.repel,
            gravitationalConstant: state.settings.gravitationalConstant,
            localGravitationalConstant: state.settings.localGravitationalConstant,
            localGravitySetting: GALAXY_FIXED_LOCAL_GRAVITY_SETTING }
        );
        seedGalaxySystemOrbits(
          data.nodes, raw.meta && raw.meta.layout_seed,
          state.settings.gravity, Math.max(36, galaxySoftening() * 5), reducedMotion,
          { gravitationalConstant: state.settings.gravitationalConstant,
            blackHoleMass: state.settings.blackHoleMass,
            orbitalSpeed: state.settings.repel,
            localGravitySetting: GALAXY_FIXED_LOCAL_GRAVITY_SETTING }
        );
      }
      /* Reused arrays bypass graphData(); size changes, static repins, and restored phases still
         receive the same strict painted-edge invariant before the next redraw — unless the
         slider burst just rescaled carriers, in which case the corrections would fold the
         burst's intermediate ratios into the layout (path dependence). */
      if (reused && galaxyMode && !skipGalaxyReseed) {
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
          acceleratedFixedSource: 0, acceleratedFixedFollowers: 0, maximumAcceleration: 0,
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
      } else if (reused && galaxyMode && skipGalaxyReseed) {
        /* The slider burst just rescaled carriers and their satellites by a known ratio.
           The phase-preserving contact pass is deferred until the coalesced frame below. */
        const skippedAnchor = galaxyGlobalAnchor(data.nodes);
        if (galaxyContactCorrectionDeferred) {
          /* A range-input burst can issue several synchronous settings updates before the
             scheduled browser frame. Preserve the complete multiplicative response first, then
             project once in that final frame. This keeps the painted-edge invariant while
             making a single jump and an equivalent fine sweep converge to the same state. */
          galaxyLastSystemAnchorExclusion = combineGalaxySystemAnchorExclusions([{
            padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
            systems: 0, contacts: 0, correctedDistance: 0, maximumShift: 0,
            inwardVelocityRemoved: 0, tangentialVelocityRemoved: 0,
            minimumClearance: null, iterations: 0,
          }]);
          galaxyLastBlackHoleExclusion = combineGalaxyBlackHoleExclusions([{
            anchorId: skippedAnchor ? skippedAnchor.id : null,
            contacts: 0, systems: 0, coreNodes: 0, fixedSystemNodes: 0,
            repelledNodes: 0, correctedDistance: 0, maximumShift: 0,
            inwardVelocityRemoved: 0, tangentialVelocityRemoved: 0,
            minimumClearance: null,
          }]);
        } else {
          /* The final scheduled frame still enforces both painted contact boundaries, including
             while the Galaxy clock is frozen or orbit-paused. */
          const prePaintHorizon = applyGalaxyBlackHoleExclusion(
            data.nodes, { padding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING }
          );
          const preStarExclusion = applyGalaxySystemAnchorExclusion(data.nodes, {
            padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
            fixAnchors: true,
          });
          const postStarExclusion = applyGalaxySystemAnchorExclusion(data.nodes, {
            padding: GALAXY_SYSTEM_ANCHOR_EXCLUSION_PADDING,
            fixAnchors: true,
          });
          const postPaintHorizon = applyGalaxyBlackHoleExclusion(
            data.nodes, { padding: GALAXY_BLACK_HOLE_EXCLUSION_PADDING }
          );
          galaxyLastSystemAnchorExclusion = combineGalaxySystemAnchorExclusions(
            [preStarExclusion, postStarExclusion]
          );
          galaxyLastBlackHoleExclusion = combineGalaxyBlackHoleExclusions(
            [prePaintHorizon, postPaintHorizon]
          );
        }
        galaxyLastFarFieldConfinement = galaxyLastFarFieldConfinement || {
          anchorId: skippedAnchor ? skippedAnchor.id : null, envelopeRadius: 0, softRadius: 0,
          acceleratedSystems: 0, boundedSystems: 0, boundedCoreNodes: 0,
          boundedFixedSource: 0, boundedFixedFollowers: 0, boundedDeformedSystems: 0,
          boundedOversizedNodes: 0, correctedDistance: 0, maximumShift: 0,
          outwardVelocityRemoved: 0, tangentialVelocityRemoved: 0,
          annulus: { innerCorrectedNodes: 0, outerCorrectedNodes: 0, infeasibleNodes: 0 },
        };
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
        clearTimeout(clusterExpandTimer);
        clusterExpandTimer = setTimeout(() => { clusterExpandTimer = 0; const d = reduced() ? 0 : 500; fg.centerAt(node.x, node.y, d); fg.zoom(1.6, d); }, 60);
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
      dragReleaseVelocity = null;
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
      if (state.settings.mode === 'galaxy' && dragReleaseVelocity) {
        const data = fg.graphData() || {};
        const insertion = galaxySlingshotCapture(node, data.nodes || [],
          dragReleaseVelocity, {
            gravity: state.settings.gravity,
            localGravitySetting: GALAXY_FIXED_LOCAL_GRAVITY_SETTING,
            localGravitationalConstant: state.settings.localGravitationalConstant,
            softening: galaxyLiveSoftening(),
            layoutSeed: raw.meta && raw.meta.layout_seed,
          });
        node.vx = insertion.vx;
        node.vy = insertion.vy;
        lastSlingshotRelease = {
          id: node.id, vx: node.vx, vy: node.vy, speed: Math.hypot(node.vx, node.vy),
          eligible: insertion.eligible, captured: insertion.captured,
          escaped: insertion.escaped, reason: insertion.reason,
          starId: insertion.starId, orbitRadius: insertion.radius,
          circularSpeed: insertion.circularSpeed, escapeSpeed: insertion.escapeSpeed,
        };
        if (typeof opts.onSlingshotRelease === 'function') {
          opts.onSlingshotRelease({ ...lastSlingshotRelease });
        }
      } else if (state.settings.mode === 'galaxy' && dragPreVelocity) {
        node.vx = dragPreVelocity.vx;
        node.vy = dragPreVelocity.vy;
      } else {
        node.vx = 0;
        node.vy = 0;
      }
      dragPreVelocity = null;
      dragReleaseVelocity = null;
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
      .onRenderFramePre((ctx, scale) => {
        try {
          styleBackground(ctx, scale);
          if (state.settings.mode === 'galaxy') {
            const currentData = fg.graphData() || {};
            const lanes = galaxyOrbitLaneGeometry(currentData.nodes || []);
            galaxyVisibleStarIds = galaxyStarAnchorIds(lanes);
            galaxyPrimaryNodeIds = galaxyPrimaryAnchorIds(lanes);
            paintGalaxyOrbitLanes(ctx, currentData.nodes || [], scale,
              state.themeColors.accent, lanes);
          } else {
            galaxyVisibleStarIds = new Set();
            galaxyPrimaryNodeIds = new Set();
          }
        } catch (e) { /* background adornment must never break the render loop */ }
      })
      .onRenderFramePost((ctx, scale) => {
        try {
          const currentData = fg.graphData() || {};
          if (Array.isArray(currentData.nodes)) {
            for (const node of currentData.nodes) paintNodeLabel(node, ctx, scale);
          }
        } catch (e) { /* label pass must never break the render loop */ }
        const batch = pendingLabels;
        pendingLabels = [];
        if (!batch.length) return;
        ctx.save();
        ctx.textBaseline = 'middle';
        for (const label of batch) {
          if (label.cluster) {
            ctx.font = '500 ' + Math.max(2.6, label.r * 0.4) + 'px system-ui, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillStyle = state.themeColors.label || '#e7e9ee';
            ctx.fillText(label.text, label.x, label.y);
            ctx.textAlign = 'left';
          } else {
            const size = Math.max(2, state.settings.font / scale);
            ctx.font = '500 ' + size + 'px system-ui, sans-serif';
            ctx.textAlign = 'left';
            ctx.fillStyle = 'rgba(0,0,0,.5)';
            ctx.fillText(label.text, label.x + 0.3, label.y + 0.3);
            ctx.fillStyle = state.themeColors.label || (label.isHilite ? '#ffffff' : 'rgba(232,236,245,.86)');
            ctx.fillText(label.text, label.x, label.y);
          }
        }
        ctx.restore();
      })
      .nodeCanvasObject((node, ctx, scale) => styleNode(node, ctx, scale))
      .nodePointerAreaPaint((node, color, ctx) => {
        if (!Number.isFinite(node.x) || !Number.isFinite(node.y)
          || !Number.isFinite(node.radius)) return;
        ctx.fillStyle = color; ctx.beginPath();
        ctx.arc(node.x, node.y, nodePaintRadius(node) + 2, 0, 6.2832); ctx.fill();
      })
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
        const orbitalRole = state.settings.mode === 'galaxy'
          ? galaxyOrbitalLinkRole(l) : 'other';
        if (!focus && orbitalRole === 'internal') return alpha(base, 0.055);
        if (!focus && orbitalRole === 'radial') return alpha(base, 0.16);
        return active ? alpha(base, focus ? 0.85 : 0.4) : alpha(base, 0.06);
      })
      .linkLineDash(l => l.suggested ? [2, 2] : (l.ghost ? [1, 3] : null))
      .linkWidth(l => {
        const w = state.settings.linkw || 1;
        const focus = hoverSet && hoverSet.size > 1;
        const s = linkEndpoint(l, 'source'), t = linkEndpoint(l, 'target');
        if (l.aggregate) return Math.min(6, 0.6 + Math.log2(1 + (l.weight || 1)) * 1.4) * w;
        if (state.bridges && l.bridge) return 2.6 * w;
        if (!focus && state.settings.mode === 'galaxy') {
          const orbitalRole = galaxyOrbitalLinkRole(l);
          if (orbitalRole === 'internal') return 0.3 * w;
          if (orbitalRole === 'radial') return 0.52 * w;
        }
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
          /* A cancelled gesture is not a physical release. Discard the sampled pointer velocity
             so finishNodeDrag restores the body's pre-drag orbital phase. */
          if (event.type === 'pointercancel') dragReleaseVelocity = null;
          finishNodeDrag(current.node);
          // The manual controller owns this gesture. Prevent force-graph's pointer-up handler
          // from applying a second release/reheat after the node has been placed exactly at the
          // pointer, which is especially visible when reduced motion disables camera settling.
          event.preventDefault();
          event.stopPropagation();
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
        const sampleTime = Number.isFinite(event.timeStamp) ? event.timeStamp : Date.now();
        const previousSample = manualDrag.lastSample;
        if (previousSample && sampleTime > previousSample.time) {
          const elapsed = Math.max(1, sampleTime - previousSample.time);
          const rawVx = (node.x - previousSample.x) / elapsed / GALAXY_SLINGSHOT_VELOCITY_SCALE;
          const rawVy = (node.y - previousSample.y) / elapsed / GALAXY_SLINGSHOT_VELOCITY_SCALE;
          const speed = Math.hypot(rawVx, rawVy);
          const scale = speed > GALAXY_SLINGSHOT_SPEED_LIMIT
            ? GALAXY_SLINGSHOT_SPEED_LIMIT / speed : 1;
          /* Low-pass two samples so a noisy final pointer event cannot create a release-only
             spike. The cap remains below the solver's emergency speed limit. */
          const sampled = { vx: rawVx * scale, vy: rawVy * scale };
          dragReleaseVelocity = dragReleaseVelocity ? {
            vx: dragReleaseVelocity.vx * 0.35 + sampled.vx * 0.65,
            vy: dragReleaseVelocity.vy * 0.35 + sampled.vy * 0.65,
          } : sampled;
        }
        manualDrag.lastSample = { x: node.x, y: node.y, time: sampleTime };
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
          lastSample: { x: candidate.x, y: candidate.y,
            time: Number.isFinite(event.timeStamp) ? event.timeStamp : Date.now() },
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
      galaxyPhaseRestorePending = false;
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
    const LAYOUT_KEYS = [
      'mode', 'repel', 'link', 'gravity', 'size',
      'gravitationalConstant', 'G_center', 'localGravitationalConstant', 'G_star',
      'blackHoleMass', 'damping', 'springStiffness',
    ];
    api.setSettings = patch => {
      const next = patch && typeof patch === 'object' ? { ...patch } : {};
      if (next.gravitationalConstant === undefined && next.G_center !== undefined) {
        next.gravitationalConstant = next.G_center;
      }
      delete next.G_center;
      if (next.gravitationalConstant !== undefined) next.gravitationalConstant =
        galaxyPhysicsMultiplier(next.gravitationalConstant,
          state.settings.gravitationalConstant, 8);
      if (next.localGravitationalConstant === undefined && next.G_star !== undefined) {
        next.localGravitationalConstant = next.G_star;
      }
      delete next.G_star;
      if (next.localGravitationalConstant !== undefined) next.localGravitationalConstant =
        galaxyPhysicsMultiplier(next.localGravitationalConstant,
          state.settings.localGravitationalConstant, 8);
      if (next.blackHoleMass !== undefined) next.blackHoleMass = galaxyPhysicsMultiplier(
        next.blackHoleMass, state.settings.blackHoleMass, 16);
      if (next.damping !== undefined) next.damping = galaxyPhysicsMultiplier(
        next.damping, state.settings.damping, 100);
      if (next.springStiffness !== undefined) next.springStiffness = galaxyPhysicsMultiplier(
        next.springStiffness, state.settings.springStiffness, 8);
      if (next.orbitPaused !== undefined) next.orbitPaused = next.orbitPaused === true;
      const wasFrozen = state.settings.frozen === true;
      const wasOrbitPaused = state.settings.orbitPaused === true;
      const isUnfreezing = wasFrozen && next.frozen === false;
      const layoutChanged = LAYOUT_KEYS.some(k => next[k] !== undefined);
      const previousMode = state.settings.mode;
      const previousGravity = Number(state.settings.gravity);
      if (layoutChanged) {
        fullLayoutDirty = true;
        cancelAutoFit();
      }
      Object.assign(state.settings, next);
      if (next.orbitPaused !== undefined && previousMode === 'galaxy') {
        if (state.settings.orbitPaused) cancelGalaxyDynamics(true);
        else if (wasOrbitPaused) scheduleGalaxyDynamics(true);
      }
      transitionGalaxyMode(previousMode, state.settings.mode);
      const nextGravity = Number(state.settings.gravity);
      const gravityChanged = next.gravity !== undefined
        && Number.isFinite(previousGravity) && Number.isFinite(nextGravity)
        && Math.abs(nextGravity - previousGravity) > 1e-12;
      /* A galaxy slider burst (gravity / black-hole mass / damping / etc.) is a setting change,
         not a fresh physics seed. Set the phase-preserve flag *before* any render below so the
         inner immediate-render does not re-seed orbits and overwrite the just-scaled carrier
         phase with a fresh seed-time correction. Without this, the user sees the carriers jump
         back outward the moment the radial contraction would have crossed a system-anchor
         exclusion boundary, and path-independence across a burst breaks. */
      if (previousMode === 'galaxy' && state.settings.mode === 'galaxy'
        && next.repel === undefined
        && (next.gravity !== undefined || next.size !== undefined
          || next.gravitationalConstant !== undefined || next.G_center !== undefined
          || next.localGravitationalConstant !== undefined || next.G_star !== undefined
          || next.blackHoleMass !== undefined || next.damping !== undefined
          || next.springStiffness !== undefined)) {
        preserveGalaxyPhaseOnResume = true;
        galaxyContactCorrectionDeferred = true;
      }
      if (gravityChanged && previousMode === 'galaxy' && state.settings.mode === 'galaxy') {
        /* Gravity changes need an immediate, legible density response: a range control whose
           visible result is only a slow orbital-velocity correction reads as broken. Scale
           every carrier's radial position toward/away from the black hole by the ratio of the
           new and old galaxyImmediateGravityRadiusScale values. The mapping is path-independent
           across a burst of input events (each event applies only its own ratio), preserves
           each solar system's internal geometry, and never touches the fixed anchor. */
        const graph = fg.graphData ? fg.graphData() : null;
        const nodes = graph && graph.nodes ? graph.nodes : null;
        if (nodes) {
          const previousScale = galaxyImmediateGravityRadiusScale(previousGravity);
          const nextScale = galaxyImmediateGravityRadiusScale(nextGravity);
          if (previousScale > 0 && nextScale > 0) {
            const ratio = nextScale / previousScale;
            const anchor = galaxyGlobalAnchor(nodes);
            if (anchor && Number.isFinite(anchor.x) && Number.isFinite(anchor.y)) {
              let moved = 0, maximumShift = 0;
              galaxyBlackHoleCarrierSystems(nodes, anchor).forEach(item => {
                if (!item.carrier || item.nodes.includes(anchor)) return;
                const dx = item.carrier.x - anchor.x;
                const dy = item.carrier.y - anchor.y;
                if (!Number.isFinite(dx) || !Number.isFinite(dy)) return;
                item.nodes.forEach(node => {
                  if (node === anchor || node.ghost) return;
                  const nx = anchor.x + (node.x - anchor.x) * ratio;
                  const ny = anchor.y + (node.y - anchor.y) * ratio;
                  if (Number.isFinite(nx) && Number.isFinite(ny)) {
                    maximumShift = Math.max(maximumShift,
                      Math.hypot(nx - node.x, ny - node.y));
                    node.x = nx;
                    node.y = ny;
                  }
                  /* The carrier-orbit support treats the server-authored
                     galactic_target_radius as a hard minimum floor. Without scaling the
                     floor with the position, the next fixed slice immediately pulls the
                     system back out and the user-visible contraction vanishes. */
                  ['galactic_target_radius', 'galactic_radius', 'galactic_preferred_radius']
                    .forEach(key => {
                      const target = Number(node[key]);
                      if (Number.isFinite(target) && target > 0) {
                        node[key] = target * ratio;
                      }
                    });
                });
                moved++;
              });
              galaxyLastGravityResponse = {
                systems: moved, moved, ratio, maximumShift,
                velocityAdjusted: 0, maximumVelocityShift: 0, anchorId: anchor.id,
              };
              render(false, false);
              /* Re-arm: the inner render consumed the flag. The outer render below must also
                 skip the contact-correction pass so the burst's ratios never fold into the
                 layout (path independence). */
              if (gravityChanged && previousMode === 'galaxy'
                && state.settings.mode === 'galaxy') {
                preserveGalaxyPhaseOnResume = true;
              }
            }
          }
        }
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
      /* Re-arm for the outer render + the synchronous physics reheat that schedulePhysicsUpdate
         may run: both share the render path and would otherwise run the path-dependent
         contact corrections on the post-scaling layout, undoing the slider's burst response. */
      if (gravityChanged && previousMode === 'galaxy' && state.settings.mode === 'galaxy') {
        preserveGalaxyPhaseOnResume = true;
      }
      render(false, false);
      /* Re-arm the phase-preserve flag for the synchronous physics reheat that follows. That
         reheat shares the same render path and would otherwise run contact corrections on the
         post-scaling layout, undoing the slider's burst response and breaking path
         independence across burst intermediates. */
      if (gravityChanged && previousMode === 'galaxy' && state.settings.mode === 'galaxy') {
        preserveGalaxyPhaseOnResume = true;
      }
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
      const next = mode === 'full' || mode === 'all' ? 'full' : 'overview';
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
      if (typeof state.repo === 'string') state.repo = state.repo.trim().toLowerCase();
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
    api.graphToScreen = (x, y) => {
      if (!fg.graph2ScreenCoords) return { x: Number(x) || 0, y: Number(y) || 0 };
      const point = fg.graph2ScreenCoords(Number(x) || 0, Number(y) || 0);
      return { x: point.x, y: point.y };
    };
    api.getPhysicsSnapshot = () => {
      const data = fg.graphData() || {};
      const nodes = Array.isArray(data.nodes) ? data.nodes : [];
      const center = galaxyGlobalAnchor(nodes);
      const centerPoint = center ? api.graphToScreen(center.x, center.y) : null;
      const systemAnchors = [];
      communityCenters(nodes).forEach(system => {
        const star = galaxySystemAnchor(system.nodes);
        if (!star || star.anchor_role !== 'community') return;
        systemAnchors.push({
          id: star.id, x: star.x, y: star.y,
          radius: finitePositive(star.radius, evidenceNodeRadius(star, 3), 160),
          mass: finitePositive(star.gravity_mass, 1, 1000),
          memberCount: system.nodes.length,
          systemOrbitRadius: system.nodes.reduce((maximum, node) => node === star
            ? maximum : Math.max(maximum, Math.hypot(node.x - star.x, node.y - star.y)), 0),
          galacticOrbitRadius: center
            ? Math.hypot(star.x - center.x, star.y - center.y) : null,
          communityId: communityKey(star),
        });
      });
      const systemAnchorIds = new Set(systemAnchors.map(star => String(star.id)));
      return {
        center: center ? {
          id: center.id, x: center.x, y: center.y,
          label: nodeName(center),
          screenX: centerPoint.x, screenY: centerPoint.y,
          radius: finitePositive(center.radius, evidenceNodeRadius(center, 3), 160),
        } : null,
        nodes: nodes.filter(node => node && Number.isFinite(node.x)
          && Number.isFinite(node.y)).map(node => ({
          id: node.id, x: node.x, y: node.y,
          vx: Number.isFinite(node.vx) ? node.vx : 0,
          vy: Number.isFinite(node.vy) ? node.vy : 0,
          radius: finitePositive(node.radius, evidenceNodeRadius(node, 3), 160),
          isCentral: node === center,
          isSystemAnchor: systemAnchorIds.has(String(node.id)),
          anchorRole: node.anchor_role || null,
          systemAnchorId: node.system_anchor_id === undefined
            || node.system_anchor_id === null ? null : node.system_anchor_id,
          communityId: communityKey(node),
          orbitRadius: Number.isFinite(Number(node.galactic_radius))
            ? Number(node.galactic_radius) : null,
          orbitTier: Number.isFinite(Number(node.orbit_tier))
            ? Number(node.orbit_tier) : null,
          warp: Number(node.__galaxySpacetimeWarp) || 0,
        })),
        systemAnchors,
        paused: state.settings.orbitPaused === true || state.settings.frozen === true
          || !running || pageHidden(),
        diagnostics: physicsDiagnostics(),
        slingshot: lastSlingshotRelease ? { ...lastSlingshotRelease } : null,
      };
    };
    api.reheat = () => {
      if (destroyed || state.settings.frozen
        || (staticFullLayout && state.settings.mode !== 'galaxy')) return;
      cancelAutoFit();
      if (!staticFullLayout) raw.nodes.forEach(n => { n.fx = undefined; n.fy = undefined; });
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
          const restorePhase = galaxyPhaseRestorePending;
          galaxyReheatStepsRemaining = 0;
          cancelGalaxyDynamics(true);
          setSimulationBudget(false, true);
          render(false, false);
          if (restorePhase && galaxyPhaseRestorePending) {
            restoreGalaxyPhase();
            galaxyPhaseRestorePending = false;
            invalidate();
          }
          return;
        }
        if (!staticFullLayout) raw.nodes.forEach(n => { n.fx = undefined; n.fy = undefined; });
        preserveGalaxyPhaseOnResume = true;
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
      clearTimeout(clusterExpandTimer);
      clusterExpandTimer = 0;
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
      galaxyPhaseRestorePending = false;
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
      galaxyGravityStrengthMultiplier,
      galaxyBlackHoleGravityConstant, galaxyBlackHoleGravitySetting,
      galaxyCarrierTargetSpeed, galaxyAuthoredCarrierTargetSpeed,
      galaxyBlackHoleSpinAngle, advanceGalaxyBlackHoleSpin,
      galaxyLocalGravityConstant,
      galaxyLocalGravityMultiplier,
      galaxyStellarGravityConstant, galaxyFallbackStellarGravityConstant,
      galaxySystemGravityConstant, galaxyStellarGravitySetting,
      defaultGalaxyStellarAccelerationCap, defaultGalaxySystemAccelerationCap,
      galaxySceneWithinLiveLimit,
      galaxyRelationOrbitScale, galaxyOrbitalSpeedMultiplier, galaxyOrbitalRadiusMultiplier,
      applyGalaxyOrbitalSpeedControl,
      galaxyOrbitalSeparationPadding, galaxyOrbitalSeparationStrength,
      communityKey, communityCenters, galaxyOrbitGroups, ensureGalaxyPositions,
      seedGalaxyOrbits, seedGalaxySystemOrbits,
      applyGalaxyGravity, applyGalaxySystemHaloGravity, applyGalaxyEnclosedSystemGravity,
      applyGalaxySystemAnchorGravity, applyGalaxySystemAnchorExclusion,
      galaxySystemAnchorClearance,
      combineGalaxySystemAnchorExclusions,
      applyGalaxyCentralGravity, applyGalaxyMutualSystemGravity, galaxyGlobalAnchor,
      galaxyBlackHoleCarrierSystems, galaxyCarrierOrbitCurve, galaxyCarrierTargetSpeed,
      galaxyBlackHoleField, applyGalaxyBlackHoleGravity, integrateGalaxyGhostOrbits,
      applyGalaxySpacetimeAcceleration, applyGalaxyEventHorizonDecay,
      galaxySlingshotCapture,
      advanceGalaxyKinematicOrbits,
      recenterGalaxyOnAnchor,
      applyCommunityBridgeGravity,
      applyGalaxyRelationSprings, applyGalaxyRelationDistanceConstraints,
      applyDraggedNodeGravity, applyDraggedNodeAcceleration,
      applyGalaxyCollisions, applyGalaxyOrbitalSeparation,
      galaxySystemEnvelopes, applyGalaxySystemPacking,
      establishGalaxyCarrierLanes,
      applyGalaxyBlackHoleExclusion,
      galaxyFarFieldEnvelope, applyGalaxyFarFieldGravity, applyGalaxyFarFieldConfinement,
      applyGalaxyAnnularBounds,
      stabilizeGalaxySystemVelocities,
      galaxyAccelerations, integrateGalaxyLeapfrog, galaxyMotionDiagnostics,
      galaxyInwardConvergencePerMinute, galaxyInwardConvergenceFactor,
      applyGalaxyInwardConvergence, enforceGalaxyOrbitalFloor,
      enforceGalaxyLocalOrbitBoundaries, supportGalaxyCarrierOrbits,
      galaxyImmediateGravityRadiusScale,
      galaxyLayoutCompactness,
      applyGalaxyGravitySettingResponse,
      galaxySpringStrength, galaxySpringDistance, galaxySafeSpringDistance,
      fallbackCommunityBridges, paintFlowArrow,
      nodeName, linkEndpoint, asOfValue, materialRecipe, materialTier,
      paintMaterialDirect, paintMaterialSurface, paintGalaxyAnchorAdornment,
      galaxyOrbitLaneGeometry, paintGalaxyOrbitLanes, galaxyOrbitalLinkRole,
      galaxyAnchorAdornmentEligible, galaxyStarAnchorIds, galaxyPrimaryAnchorIds,
      renderMaterialSample, sampleMaterialColour,
      materialCacheStats, clearMaterialCache, setMaterialCanvasFactory
    }
  };
})();
