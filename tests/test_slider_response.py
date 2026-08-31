"""Probe the slider response curve directly. Find the dead zones."""
import json, shutil, subprocess, sys
from pathlib import Path
ROOT = Path(r"C:\Users\jomie\Documents\Github\engraphis")
ASSET = ROOT / "engraphis" / "dashboard_assets" / "ledger.js"

# Extract just the graphSliderResponseValue function
src = ASSET.read_text(encoding="utf-8")
# Find function start and end
start = src.index("function graphSliderResponseValue(")
# Find matching closing brace
depth = 0
i = start
while i < len(src):
    c = src[i]
    if c == '{': depth += 1
    elif c == '}':
        depth -= 1
        if depth == 0: break
    i += 1
fn = src[start:i+1]
print(f"Function length: {len(fn)}", file=sys.stderr)

# Build a minimal browser shim
shim = """
const GRAPH_SLIDER_RESPONSE_PEAK_GAIN = 2;
var byId = function(id) { return { value: '96', min: '0', max: '400' }; };
var graphValueInRange = function(id, v) { return v; };
""" + fn + """
const samples = [];
for (let s = 0; s <= 400; s += 1) {
  const eff = graphSliderResponseValue('graph-gravity', String(s), 96);
  samples.push({ s, eff: Math.round(eff * 100) / 100 });
}
// Group by effective value
const grouped = {};
samples.forEach(({s, eff}) => {
  grouped[eff] = (grouped[eff] || []);
  grouped[eff].push(s);
});
// Find dead zones (multiple raw s map to same effective)
const deadZones = Object.entries(grouped).filter(([_, list]) => list.length > 1).map(([eff, list]) => ({
  effective: Number(eff), count: list.length, first: list[0], last: list[list.length-1],
}));
// Find the full map
const map = samples.filter((_, i) => i % 1 === 0);
// Find first 5 unique effective values and the dead zone boundaries
const transitions = [];
let prevEff = null;
samples.forEach(({s, eff}) => {
  if (eff !== prevEff) {
    transitions.push({ s, eff });
    prevEff = eff;
  }
});
console.log(JSON.stringify({ transitionsCount: transitions.length, transitions: transitions.slice(0, 5), transitionsTail: transitions.slice(-30), deadZonesCount: deadZones.length, topDeadZones: deadZones.slice(0, 10) }, null, 2));
"""

# Run with node
result = subprocess.run(['node', '-e', shim], capture_output=True, text=True, cwd=ROOT)
print("STDOUT:", result.stdout)
print("STDERR:", result.stderr)
