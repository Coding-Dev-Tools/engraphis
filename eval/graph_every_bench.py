"""Deterministic Every-node worker benchmark: measures prepare->settled-layout wall time
at the dashboard's two real scales. Run inside the dev distrobox (needs node):

    distrobox enter dev -- bash -lc 'cd <repo> && python -m eval.graph_every_bench'

This is the number behind the performance claim in docs/GRAPH_PERFORMANCE.md: the worker
settle is the only O(n)-ish phase; per-frame render cost is node-count independent."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "engraphis" / "dashboard_assets" / "engraphis-graph-every-worker.js"

SCALES = (2000, 20000)

HARNESS = """
const vm = require('vm'); const fs = require('fs'); const messages = [];
const src = fs.readFileSync(process.argv[1], 'utf8');
const ctx = { self: { postMessage: m => messages.push(m) },
  setTimeout: f => setTimeout(f, 0), clearTimeout: t => clearTimeout(t) };
vm.runInNewContext(src, ctx);
const n = Number(process.argv[2]), clusters = 40;
const nodes = [], links = [];
for (let i = 0; i < n; i += 1) {
  nodes.push({ id: `n${i}`, name: `Entity ${i}`, community_id: `c${i % clusters}` });
  links.push({ source: `n${i}`, target: `n${(i + 1) % n}` });
  if (i % 3 === 0) links.push({ source: `n${i}`, target: `n${(i * 13 + 7) % n}`, weight: 2 });
}
const t0 = Date.now();
ctx.self.onmessage({ data: { type: 'prepare', payload: { nodes, links } } });
const waitQuiet = cb => {
  let count = messages.length;
  setTimeout(function tick() {
    if (messages.length === count) return cb(Date.now() - t0);
    count = messages.length;
    setTimeout(tick, 100);
  }, 200);
};
waitQuiet(ms => {
  const ready = messages.find(m => m.type === 'ready');
  console.log(JSON.stringify({
    nodes: ready.ids.length, links: ready.totalLinks,
    settle_ms: ms, layouts_streamed: messages.filter(m => m.type === 'layout').length,
  }));
});
"""


def main() -> None:
    results = []
    for scale in SCALES:
        out = subprocess.run(
            ["node", "-e", HARNESS, str(WORKER), str(scale)],
            cwd=ROOT, check=True, capture_output=True, text=True, timeout=600,
        )
        report = json.loads(out.stdout.strip().splitlines()[-1])
        results.append(report)
        print(f"{scale:>6} nodes / {report['links']:>6} links: "
              f"settle {report['settle_ms']:>5} ms "
              f"({report['layouts_streamed']} streamed layout passes)")
    ratio = results[-1]["settle_ms"] / max(1, results[0]["settle_ms"])
    print(f"20k/2k settle-time ratio: {ratio:.1f}x (one-off cost; frame rate is scale-independent)")


if __name__ == "__main__":
    main()
