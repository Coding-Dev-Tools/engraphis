(() => {
  'use strict';

  /* A canvas-only cosmetic layer for the Galaxy renderer. It deliberately owns no graph
     state or forces: the physics engine is the source of truth and may provide snapshots by
     `getPhysicsSnapshot()` or `engraphisgraphphysicschange` on the graph container. Keeping
     this external means a 500-node scene adds one canvas and bounded drawing work, not 500 DOM
     nodes or a second simulation loop. */
  const MAX_TRAIL_NODES = 160;
  const TRAIL_POINTS = 8;
  const TRAIL_NODE_LIMIT = 600;
  const SAMPLE_INTERVAL = 33;
  const GRID_RINGS = 9;
  const GRID_SPOKES = 20;
  const MAX_LOCAL_WELLS = 24;
  const finite = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const reducedMotion = () => typeof matchMedia === 'function'
    && matchMedia('(prefers-reduced-motion: reduce)').matches;

  function snapshotCenter(snapshot) {
    const center = snapshot && snapshot.center;
    return center && Number.isFinite(center.x) && Number.isFinite(center.y) ? center : null;
  }

  function bestNodes(snapshot) {
    const nodes = Array.isArray(snapshot && snapshot.nodes) ? snapshot.nodes : [];
    return nodes.filter(node => node && Number.isFinite(node.x) && Number.isFinite(node.y)
      && node.isCentral !== true && node.central !== true)
      .sort((a, b) => Math.hypot(finite(b.vx), finite(b.vy)) - Math.hypot(finite(a.vx), finite(a.vy)))
      .slice(0, MAX_TRAIL_NODES);
  }

  function localAnchors(snapshot) {
    const supplied = Array.isArray(snapshot && snapshot.systemAnchors) ? snapshot.systemAnchors : [];
    const fallback = Array.isArray(snapshot && snapshot.nodes) ? snapshot.nodes.filter(node => node
      && (node.isSystemAnchor === true || node.anchorRole === 'community')) : [];
    return (supplied.length ? supplied : fallback).filter(anchor => anchor
      && Number.isFinite(anchor.x) && Number.isFinite(anchor.y))
      .sort((left, right) => finite(right.mass || right.gravityMass || right.radius)
        - finite(left.mass || left.gravityMass || left.radius))
      .slice(0, MAX_LOCAL_WELLS);
  }

  function create(container, engine) {
    if (!container || !container.appendChild) return null;
    const canvas = document.createElement('canvas');
    canvas.className = 'graph-spacetime-overlay';
    canvas.setAttribute('aria-hidden', 'true');
    container.appendChild(canvas);
    const ctx = canvas.getContext('2d', { alpha: true });
    if (!ctx) { canvas.remove(); return null; }
    const trails = new Map();
    let active = false;
    let frame = 0;
    let lastSample = 0;
    let latest = null;
    let destroyed = false;

    const physicalToScreen = point => {
      if (!point) return null;
      if (engine && typeof engine.graphToScreen === 'function') {
        const screen = engine.graphToScreen(point.x, point.y);
        if (screen && Number.isFinite(screen.x) && Number.isFinite(screen.y)) return screen;
      }
      const viewport = latest && latest.viewport;
      if (viewport && Number.isFinite(viewport.zoom)) {
        return { x: point.x * viewport.zoom + finite(viewport.x), y: point.y * viewport.zoom + finite(viewport.y) };
      }
      /* A rendering engine that cannot expose its viewport still gets a centred, harmless
         lens/grid rather than an incorrect coordinate transform. */
      return { x: canvas.width / (2 * devicePixelRatio), y: canvas.height / (2 * devicePixelRatio) };
    };

    const screenRadius = physicalCenter => {
      if (!physicalCenter) return 20;
      const center = physicalToScreen(physicalCenter);
      const edge = physicalToScreen({ x: physicalCenter.x + finite(physicalCenter.radius), y: physicalCenter.y });
      return center && edge ? Math.max(10, Math.abs(edge.x - center.x)) : Math.max(10, finite(physicalCenter.radius));
    };

    const screenDistance = (origin, graphDistance, fallback) => {
      const start = physicalToScreen(origin);
      const edge = physicalToScreen({ x: origin.x + graphDistance, y: origin.y });
      return start && edge ? Math.max(fallback, Math.abs(edge.x - start.x)) : fallback;
    };

    const resize = () => {
      const ratio = Math.min(2, Math.max(1, finite(window.devicePixelRatio) || 1));
      const width = Math.max(1, container.clientWidth);
      const height = Math.max(1, container.clientHeight);
      if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
      }
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      return { width, height };
    };

    const drawGrid = (center, bounds) => {
      const maxRadius = Math.hypot(bounds.width, bounds.height) * .72;
      const horizon = Math.max(12, finite(center.radius) * 1.55);
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      ctx.lineWidth = 1;
      for (let ring = 1; ring <= GRID_RINGS; ring += 1) {
        const rest = horizon + (maxRadius - horizon) * ring / GRID_RINGS;
        ctx.beginPath();
        for (let i = 0; i <= 96; i += 1) {
          const angle = i / 96 * Math.PI * 2;
          /* A saturating gravity well: rings visibly pinch near the event horizon but do not
             explode at r=0 or consume the entire canvas under a high mass setting. */
          const warped = rest - Math.min(rest * .38, (horizon * horizon * 1.9) / Math.max(rest, horizon));
          const x = center.x + Math.cos(angle) * warped;
          const y = center.y + Math.sin(angle) * warped * .82;
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = `rgba(96, 198, 255, ${0.025 + ring * .004})`;
        ctx.stroke();
      }
      for (let spoke = 0; spoke < GRID_SPOKES; spoke += 1) {
        const angle = spoke / GRID_SPOKES * Math.PI * 2;
        ctx.beginPath();
        for (let step = 0; step <= 16; step += 1) {
          const rest = horizon + (maxRadius - horizon) * step / 16;
          const warped = rest - Math.min(rest * .38, (horizon * horizon * 1.9) / Math.max(rest, horizon));
          const x = center.x + Math.cos(angle + .12 * (1 - step / 16)) * warped;
          const y = center.y + Math.sin(angle + .12 * (1 - step / 16)) * warped * .82;
          if (step === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = 'rgba(128, 107, 255, .07)';
        ctx.stroke();
      }
      const lens = ctx.createRadialGradient(center.x, center.y, Math.max(1, horizon * .35), center.x, center.y, horizon * 2.8);
      lens.addColorStop(0, 'rgba(0, 0, 0, .36)');
      lens.addColorStop(.48, 'rgba(123, 170, 255, .11)');
      lens.addColorStop(.78, 'rgba(221, 175, 255, .035)');
      lens.addColorStop(1, 'rgba(84, 148, 255, 0)');
      ctx.fillStyle = lens;
      ctx.beginPath();
      ctx.arc(center.x, center.y, horizon * 2.8, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    };

    const drawLocalWells = snapshot => {
      const anchors = localAnchors(snapshot);
      if (!anchors.length) return;
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      anchors.forEach(anchor => {
        const center = physicalToScreen(anchor);
        if (!center || center.x < -160 || center.y < -160
          || center.x > container.clientWidth + 160 || center.y > container.clientHeight + 160) return;
        const radius = screenRadius(anchor);
        const orbitRadius = Math.max(radius * 2.5, Math.min(96, screenDistance(anchor,
          finite(anchor.systemOrbitRadius || anchor.orbitRadius) || radius * 5, radius * 5)));
        const glow = ctx.createRadialGradient(center.x, center.y, Math.max(1, radius * .5),
          center.x, center.y, orbitRadius * 1.15);
        glow.addColorStop(0, 'rgba(255, 210, 116, .08)');
        glow.addColorStop(.55, 'rgba(245, 169, 80, .025)');
        glow.addColorStop(1, 'rgba(245, 169, 80, 0)');
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(center.x, center.y, orbitRadius * 1.15, 0, Math.PI * 2);
        ctx.fill();
        for (let ring = 1; ring <= 2; ring += 1) {
          ctx.beginPath();
          ctx.ellipse(center.x, center.y, orbitRadius * ring / 2,
            orbitRadius * ring * .70 / 2, .18, 0, Math.PI * 2);
          ctx.strokeStyle = `rgba(255, 191, 116, ${.045 + ring * .018})`;
          ctx.lineWidth = .7;
          ctx.stroke();
        }
      });
      ctx.restore();
    };

    const drawTrails = () => {
      if (reducedMotion()) return;
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      trails.forEach(points => {
        if (points.length < 2) return;
        const gradient = ctx.createLinearGradient(points[0].x, points[0].y,
          points[points.length - 1].x, points[points.length - 1].y);
        gradient.addColorStop(0, 'rgba(154, 216, 255, .01)');
        gradient.addColorStop(1, 'rgba(154, 216, 255, .20)');
        ctx.strokeStyle = gradient;
        ctx.lineWidth = 1.1;
        ctx.beginPath();
        ctx.moveTo(points[0].x, points[0].y);
        for (let i = 1; i < points.length; i += 1) ctx.lineTo(points[i].x, points[i].y);
        ctx.stroke();
      });
      ctx.restore();
    };

    const sample = stamp => {
      if (!latest || stamp - lastSample < SAMPLE_INTERVAL) return;
      lastSample = stamp;
      const seen = new Set();
      const allNodes = Array.isArray(latest && latest.nodes) ? latest.nodes : [];
      if (allNodes.length > TRAIL_NODE_LIMIT) {
        trails.clear();
        return;
      }
      bestNodes(latest).forEach(node => {
        const screen = physicalToScreen(node);
        if (!screen) return;
        const id = String(node.id || '');
        if (!id) return;
        seen.add(id);
        const points = trails.get(id) || [];
        points.push({ x: screen.x, y: screen.y });
        if (points.length > TRAIL_POINTS) points.splice(0, points.length - TRAIL_POINTS);
        trails.set(id, points);
      });
      trails.forEach((_points, id) => { if (!seen.has(id)) trails.delete(id); });
    };

    const draw = stamp => {
      if (destroyed) return;
      const bounds = resize();
      ctx.clearRect(0, 0, bounds.width, bounds.height);
      if (active && engine && typeof engine.getPhysicsSnapshot === 'function') {
        latest = engine.getPhysicsSnapshot() || latest;
      }
      if (active && latest) {
        sample(stamp);
        const physicalCenter = snapshotCenter(latest);
        const center = physicalToScreen(physicalCenter);
        if (center) drawGrid({ ...center, radius: screenRadius(physicalCenter) }, bounds);
        drawLocalWells(latest);
        drawTrails();
      }
      /* Paused physics retains one static spacetime paint, then releases the compositor.
         Local wells and guide rings are deliberately still visible under reduced motion;
         only sampled velocity trails are suppressed there. */
      if (active && !document.hidden && !(latest && latest.paused)) frame = requestAnimationFrame(draw);
      else frame = 0;
    };

    const wake = () => {
      if (!frame && !destroyed && active && !document.hidden) frame = requestAnimationFrame(draw);
    };
    const onFrame = event => {
      latest = event && event.detail ? event.detail : latest;
      if (active) wake();
    };
    container.addEventListener('engraphisgraphphysicschange', onFrame);
    const onVisibilityChange = () => { if (!document.hidden) wake(); };
    document.addEventListener('visibilitychange', onVisibilityChange);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(resize);
    if (observer) observer.observe(container);
    return {
      setEngine(next) { engine = next || engine; },
      setEnabled(on) {
        active = on === true;
        if (!active) trails.clear();
        wake();
      },
      setSnapshot(snapshot) { latest = snapshot || null; if (active) wake(); },
      destroy() {
        destroyed = true;
        cancelAnimationFrame(frame);
        container.removeEventListener('engraphisgraphphysicschange', onFrame);
        document.removeEventListener('visibilitychange', onVisibilityChange);
        if (observer) observer.disconnect();
        canvas.remove();
        trails.clear();
      },
    };
  }

  window.EngraphisSpacetime = { create };
})();
