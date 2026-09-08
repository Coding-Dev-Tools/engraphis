"""Exercise final painted/orbital bounds through the shipped graph engine."""
import json

import pytest

from tests.test_graph_engine_asset import _run_engine, _run_node, requires_node


@requires_node
def test_equivalent_systems_keep_their_lanes_after_rigid_translation():
    report = _run_node("""
        const measure = (offset, reverse, rotation = 0, noise = 0) => {
          const nodes = [{ id: 'bh', anchor_role: 'global', system_anchor_id: 'bh',
            community_id: 'core', radius: 8, gravity_mass: 64,
            x: offset, y: offset }];
          for (let index = 0; index < 24; index++) {
            const id = 'system-' + String(index).padStart(2, '0');
            const angle = rotation + index * 2.399963229728653;
            const x = offset + 100 * Math.cos(angle), y = offset + 100 * Math.sin(angle);
            nodes.push({ id, anchor_role: 'community', system_anchor_id: id,
              community_id: id, radius: 5, gravity_mass: 8, x, y });
            nodes.push({ id: id + '-planet', system_anchor_id: id, community_id: id,
              orbit_radius: 53, radius: 2, gravity_mass: 1,
              x: x + 53 * Math.cos(angle) + (index % 2 ? noise : -noise),
              y: y + 53 * Math.sin(angle) });
          }
          if (reverse) nodes.reverse();
          I.establishGalaxyCarrierLanes(nodes, { layoutSeed: 3031 });
          const anchor = nodes.find(node => node.id === 'bh');
          const systems = I.galaxySystemEnvelopes(nodes, { respectFixedCoordinates: false })
            .filter(system => system.anchor !== anchor);
          let clearance = Infinity;
          systems.forEach((left, index) => systems.slice(index + 1).forEach(right => {
            clearance = Math.min(clearance, Math.hypot(left.x - right.x, left.y - right.y)
              - left.radius - right.radius);
          }));
          const carriers = nodes.filter(node => node.anchor_role === 'community')
            .sort((left, right) => left.id.localeCompare(right.id));
          return { clearance, positions: carriers.map(node => [
            node.x - anchor.x, node.y - anchor.y, node.__galaxyCarrierLaneRadius,
          ]) };
        };
        emit([measure(0, false), measure(1000, false), measure(1000, true),
          measure(-1000, true, Math.PI / 7), measure(1000, false, 0, 1e-12)]);
    """)
    for measured in report:
        assert measured["clearance"] >= 0, measured
        for expected, actual in zip(report[0]["positions"], measured["positions"]):
            assert actual == pytest.approx(expected, rel=0, abs=1e-9), measured


@requires_node
@pytest.mark.parametrize("width,height", [(800, 600), (420, 800), (1600, 900)])
def test_auto_fit_contains_complete_orbital_envelopes(width, height):
    result = _run_engine(
        f"el.clientWidth={width}; el.clientHeight={height};\n" + """
        const pending = new Map();
        let timerId = 0;
        globalThis.setTimeout = fn => { pending.set(++timerId, fn); return timerId; };
        globalThis.clearTimeout = id => pending.delete(id);
        store.getGraphBbox = { x: [-100, 100], y: [-100, 100] };
        const api = G.create(el, { settings: { mode: 'galaxy' }, collapse: 'never' });
        api.setData({ nodes: [
          { id: 'bh', anchor_role: 'global', community_id: 'core',
            system_anchor_id: 'bh', gravity_mass: 12, radius: 8, x: 0, y: 0 },
          { id: 'star', anchor_role: 'community', community_id: 'solar',
            system_anchor_id: 'star', gravity_mass: 4, radius: 5, x: 100, y: 0 },
          { id: 'planet', community_id: 'solar', system_anchor_id: 'star',
            gravity_mass: 1, radius: 2, orbit_radius: 20, x: 120, y: 0 },
        ], edges: [] });
        const timers = [...pending.values()]; pending.clear();
        timers.forEach(fn => fn());
        const nodes = store.graphData.nodes;
        const anchor = nodes.find(node => node.anchor_role === 'global');
        const radius = I.galaxySystemEnvelopes(nodes, {
          respectFixedCoordinates: false,
        }).reduce((maximum, system) => Math.max(maximum,
          Math.hypot(system.anchor.x - anchor.x, system.anchor.y - anchor.y)
            + system.radius), 1);
        const zoom = Array.isArray(store.zoom) ? store.zoom[0] : store.zoom;
        emit({ available: Math.min(el.clientWidth, el.clientHeight) - 80,
          diameter: radius * 2 * zoom, zoom, radius });
        api.destroy();
        """
    )
    assert result["diameter"] > 0
    assert result["diameter"] <= result["available"], result


@requires_node
@pytest.mark.parametrize("parent_speed,angle,timestep", [
    (12, 4.539601384437251, 0.032),
    (47.999, 0.7037167544041136, 1),
    (47.999, 0.7037167544041136, 2),
    (0, 0.4, 2),
])
def test_live_phase_and_emitted_velocity_share_a_safe_endpoint(parent_speed, angle, timestep):
    result = _run_node(
        "const probe = " + json.dumps({"speed": parent_speed, "angle": angle, "dt": timestep}) + ";\n" + """
        const a = probe.angle, r = 5;
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            system_anchor_id: 'black-hole', gravity_mass: 16, radius: 8,
            x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'star', anchor_role: 'community', community_id: 'solar',
            system_anchor_id: 'star', orbit_tier: 0, gravity_mass: 100, radius: 1,
            x: 1000, y: 0, vx: probe.speed, vy: 0 },
          { id: 'planet', community_id: 'solar', system_anchor_id: 'star',
            orbit_tier: 1, orbit_radius: r, gravity_mass: 1, radius: .5,
            x: 1000 + r * Math.cos(a), y: r * Math.sin(a),
            vx: probe.speed - Math.sin(a), vy: Math.cos(a) },
        ];
        const before = Math.atan2(nodes[2].y - nodes[1].y, nodes[2].x - nodes[1].x);
        I.applyGalaxyOrbitalSpeedControl(nodes, {
          gravity: 48, softening: 1, centralSoftening: 40,
          localGravitySetting: 48, localGravitationalConstant: 8, orbitalSpeed: 100,
          layoutSeed: 19, timestep: probe.dt, speedLimit: 48,
        });
        const planet = nodes[2], star = nodes[1];
        const radius = Math.hypot(planet.x - star.x, planet.y - star.y);
        // The unwrapped clock avoids principal-angle aliasing at supported long steps.
        const delta = Math.abs(planet.__galaxySpeedControlPhase.angle - before);
        emit({ speed: Math.hypot(planet.vx, planet.vy), radius,
          phaseSpeed: delta * radius / probe.dt,
          relativeSpeed: Math.hypot(planet.vx - star.vx, planet.vy - star.vy) });
        """
    )
    assert result["speed"] <= 48, result
    assert result["radius"] == pytest.approx(5), result
    assert result["phaseSpeed"] == pytest.approx(result["relativeSpeed"], rel=1e-9, abs=1e-10), result


@requires_node
def test_live_black_hole_exclusion_preserves_parent_orbit():
    report = _run_engine("""
        let nextFrame = 1;
        const queue = new Map();
        window.requestAnimationFrame = callback => {
          const id = nextFrame++; queue.set(id, callback); return id;
        };
        window.cancelAnimationFrame = id => queue.delete(id);
        const flush = time => {
          const callbacks = [...queue.values()]; queue.clear();
          callbacks.forEach(callback => callback(time));
        };
        let frames = 0, minRadius = Infinity;
        let minParentClearance = Infinity, minBhClearance = Infinity, maxRadiusError = 0;
        const sample = () => {
          const nodes = store.graphData.nodes;
          const byId = new Map(nodes.map(node => [node.id, node]));
          const bh = byId.get('bh'), star = byId.get('star'), planet = byId.get('planet');
          const radius = Math.hypot(planet.x - star.x, planet.y - star.y);
          const minimum = star.radius + planet.radius + 1.5;
          const expected = Math.max(planet.orbit_radius, minimum);
          minRadius = Math.min(minRadius, radius);
          minParentClearance = Math.min(minParentClearance, radius - minimum);
          maxRadiusError = Math.max(maxRadiusError, Math.abs(radius - expected));
          nodes.forEach(node => {
            if (node === bh) return;
            minBhClearance = Math.min(minBhClearance,
              Math.hypot(node.x - bh.x, node.y - bh.y) - bh.radius * 2 - node.radius - 2.5);
          });
          frames++;
        };
        const api = G.create(el, { reducedMotion: () => false, onPhysicsFrame: sample });
        api.setPreset('galaxy');
        api.setSettings({ gravity: 48 });
        api.setData({ nodes: [
          { id: 'bh', anchor_role: 'global', system_anchor_id: 'bh', community_id: 'core',
            gravity_mass: 64, visual_radius: 8, orbit_tier: 0, x: 0, y: 0 },
          { id: 'star', anchor_role: 'community', system_anchor_id: 'star', community_id: 'solar',
            gravity_mass: 12, visual_radius: 8, orbit_tier: 0, x: 70.4, y: 0 },
          { id: 'planet', anchor_role: 'none', system_anchor_id: 'star', community_id: 'solar',
            gravity_mass: 2, visual_radius: 8, orbit_tier: 1, orbit_radius: 19.2, x: 83.2, y: 14.4 },
          { id: 'other-star', anchor_role: 'community', system_anchor_id: 'other-star',
            community_id: 'other', gravity_mass: 9, visual_radius: 8, orbit_tier: 0, x: -16, y: 113.6 },
        ], edges: [{ id: 'orbit', source: 'star', target: 'planet', relation: 'orbits',
                     rest_length: 19.2, spring_strength: .08 }],
          communities: [
            { id: 'core', anchor_id: 'bh', mass: 64, member_count: 1 },
            { id: 'solar', anchor_id: 'star', mass: 14, member_count: 2 },
            { id: 'other', anchor_id: 'other-star', mass: 9, member_count: 1 },
          ], community_bridges: [],
          meta: { algorithm_version: 'galaxy-v6', layout_seed: 91, total_nodes: 4, truncated: false },
        });
        for (let step = 0; step < 120; step++) flush(100 + step * (1000 / 30));
        const steps = api.physicsDiagnostics().steps;
        api.destroy();
        emit({ frames, steps, minRadius, minParentClearance, minBhClearance, maxRadiusError });
    """)
    assert report["frames"] == report["steps"] == 120
    assert report["minRadius"] > 8
    assert report["maxRadiusError"] < 1e-7
    assert report["minParentClearance"] >= -1e-7
    assert report["minBhClearance"] >= -1e-7


@requires_node
@pytest.mark.parametrize("global_gravity", [0, 1])
def test_kinematic_orbits_clear_the_painted_black_hole(global_gravity):
    report = _run_node(f"const globalGravity = {global_gravity};\n" + """
        const nodes = [
          { id: 'black-hole', anchor_role: 'global', community_id: 'core',
            system_anchor_id: 'black-hole', gravity_mass: 16, radius: 8,
            x: 0, y: 0, vx: 0, vy: 0 },
          { id: 'star', anchor_role: 'community', community_id: 'solar',
            system_anchor_id: 'star', gravity_mass: 4, radius: 2,
            x: 20, y: 0, vx: 0, vy: 0 },
          { id: 'planet', community_id: 'solar', system_anchor_id: 'star',
            orbit_tier: 1, orbit_radius: 6, gravity_mass: 1, radius: 1,
            x: 26, y: 0, vx: 0, vy: 0 },
        ];
        let minimumClearance = Infinity, minimumParentClearance = Infinity, maximumRadiusError = 0;
        for (let step = 0; step < 120; step++) {
          I.advanceGalaxyKinematicOrbits(nodes, {
            gravity: 48, softening: 12, centralSoftening: 40, localSoftening: 12,
            gravitationalConstant: globalGravity,
            orbitalSpeed: 100, layoutSeed: 19, timestep: .032, speedLimit: 48,
          });
          for (const node of nodes.slice(1)) {
            minimumClearance = Math.min(minimumClearance,
              Math.hypot(node.x - nodes[0].x, node.y - nodes[0].y)
                - nodes[0].radius * 2 - node.radius - 2.5);
          }
          minimumParentClearance = Math.min(minimumParentClearance,
            Math.hypot(nodes[2].x - nodes[1].x, nodes[2].y - nodes[1].y)
              - nodes[1].radius - nodes[2].radius - 1.5);
          maximumRadiusError = Math.max(maximumRadiusError,
            Math.abs(Math.hypot(nodes[2].x - nodes[1].x, nodes[2].y - nodes[1].y) - 6));
        }
        emit({ minimumClearance, minimumParentClearance, maximumRadiusError });
    """)
    assert report["minimumClearance"] >= -1e-7, report
    assert report["minimumParentClearance"] >= -1e-7, report
    assert report["maximumRadiusError"] < 1e-7, report
