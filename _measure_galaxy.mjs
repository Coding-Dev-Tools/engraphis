import { readFile } from "node:fs/promises";
import { chromium } from "playwright";
const staticDir = "/sessions/relaxed-stoic-hamilton/mnt/engraphis/engraphis/static";
const source = await readFile(`${staticDir}/galaxy-explorer.js`, "utf8");
const systemCount = 12, members = 8;
const nodes = [], communities = [], bridges = [], edges = [];
for (let s = 0; s < systemCount; s += 1) {
  const sysId = `sys_${s}`, anchorId = `n_${s}_0`;
  communities.push({ id: sysId, anchor_id: anchorId, mass: 40 - s, radius: 36, member_count: members });
  for (let m = 0; m < members; m += 1) {
    nodes.push({ canonical_id: `n_${s}_${m}`, label: `${s}.${m}`, community_id: sysId,
      anchor_role: s === 0 && m === 0 ? "global" : m === 0 ? "community" : "none",
      mass_score: m === 0 ? 0.9 : 0.2, gravity_mass: m === 0 ? 9 : 2, visual_radius: m === 0 ? 8 : 3 });
    if (m > 0) edges.push({ id: `e_${s}_${m}`, source: anchorId, target: `n_${s}_${m}`, relation: "supports", layer: "semantic", strength: 0.8, support_count: 2, visible_by_default: true });
  }
  if (s > 0) bridges.push({ id: `b_${s}`, source_community: `sys_${s-1}`, target_community: sysId, strength: 0.5, physics_strength: 0.5, support_count: 4, edge_count: 2 });
}
const scene = { meta: { level: "complete", complete_scene: true, scene_hash: "measure", layout_seed: 7 }, nodes, edges, communities, community_bridges: bridges };
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
await page.addStyleTag({ path: `${staticDir}/galaxy-explorer.css` });
const out = await page.evaluate(async ({ bundle, scene }) => {
  const url = URL.createObjectURL(new Blob([bundle], { type: "text/javascript" }));
  const galaxy = await import(url);
  const container = document.createElement("main");
  container.style.width = "1000px"; container.style.height = "700px";
  document.body.appendChild(container);
  const controller = galaxy.createGalaxyGraph(container, { forceSynchronous: true });
  const layout = await controller.setScene(scene);
  const blackId = layout.blackHoleId;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const meanDist = () => {
    const c = controller.getNodeGraphPosition(blackId);
    const ds = scene.nodes.filter((n) => n.canonical_id !== blackId)
      .map((n) => { const p = controller.getNodeGraphPosition(n.canonical_id); return Math.hypot(p.x - c.x, p.y - c.y); });
    ds.sort((a, b) => a - b);
    return { mean: ds.reduce((a, b) => a + b, 0) / ds.length, max: ds.at(-1), median: ds[Math.floor(ds.length/2)] };
  };
  const run = async (controls) => { await controller.relayout(scene, controls); controller.setPhysicsEnabled(true); await wait(3000); controller.setPhysicsEnabled(false); return meanDist(); };
  const gravity = {};
  for (const p of [0.65, 1.0, 1.35]) gravity[p] = await run({ systemPull: p, orbitalCompactness: 1, linkCohesion: 1, layoutPreset: "compact" });
  const presets = {};
  for (const preset of ["compact", "spacious", "islands", "radial"]) presets[preset] = await run({ systemPull: 1.0, orbitalCompactness: 1, linkCohesion: 1, layoutPreset: preset });
  controller.destroy(); URL.revokeObjectURL(url);
  return { gravity, presets };
}, { bundle: source, scene });
await browser.close();
console.log("=== GRAVITY sweep (systemPull -> node distance from center) ===");
for (const [p, d] of Object.entries(out.gravity)) console.log(`  systemPull=${p}: mean=${d.mean.toFixed(1)} median=${d.median.toFixed(1)} max=${d.max.toFixed(1)}`);
console.log("=== PRESET sweep (layoutPreset -> distance from center, pull=1.0) ===");
for (const [k, d] of Object.entries(out.presets)) console.log(`  ${k.padEnd(9)}: mean=${d.mean.toFixed(1)} median=${d.median.toFixed(1)} max=${d.max.toFixed(1)}`);
