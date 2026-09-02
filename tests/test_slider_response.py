"""Pin the production slider response curve: strictly monotone, no dead zones.

Extracts ``graphSliderResponseValue`` from the shipped ``ledger.js`` (the same
function the browser runs), drives it across the full 0..400 HTML range, and
asserts every raw value maps to a unique, strictly increasing effective value.
Any dead zone (multiple raw values collapsing to one effective value) or
saturation plateau fails the test — this is the exact regression the floor
removal fixed.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "engraphis" / "dashboard_assets" / "ledger.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = """
const emit = value => console.log(JSON.stringify(value));
"""


def _extract_function(source: str, name: str) -> str:
    """Pull a top-level ``function <name>(...) {...}`` body via brace matching."""
    start = source.index(f"function {name}(")
    depth = 0
    i = start
    while i < len(source):
        c = source[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                break
        i += 1
    return source[start:i + 1]


@requires_node
def test_slider_response_is_strictly_monotone_without_dead_zones() -> None:
    """Every raw slider value 0..400 must map to a unique effective value.

    The production mapping is extracted from the shipped ledger.js, not
    re-implemented, so any regression (dead zone, floor, saturation) is
    caught against the real code the browser runs.
    """
    src = LEDGER.read_text(encoding="utf-8")
    response_fn = _extract_function(src, "graphSliderResponseValue")
    in_range_fn = _extract_function(src, "graphValueInRange")

    gain_match = src.rfind("GRAPH_SLIDER_RESPONSE_PEAK_GAIN")
    if gain_match == -1:
        peak_gain = 1
    else:
        # Evaluate the constant the same way ledger.js defines it.
        m = None
        for chunk in src.split(';'):
            if 'GRAPH_SLIDER_RESPONSE_PEAK_GAIN' in chunk and '=' in chunk:
                m = chunk.split('=', 1)[1].strip()
                break
        peak_gain = float(m) if m and m.replace('.', '', 1).isdigit() else 1

    script = (
        "const GRAPH_SLIDER_RESPONSE_PEAK_GAIN = "
        + json.dumps(peak_gain) + ";\n"
        + "const byId = id => ({ value: '96', min: '0', max: '400' });\n"
        + in_range_fn + "\n"
        + response_fn + "\n"
        + """
const samples = [];
for (let s = 0; s <= 400; s += 1) {
  const raw = String(s);
  const inRange = graphValueInRange('graph-gravity', raw, 96);
  const eff = graphSliderResponseValue('graph-gravity', inRange, 96);
  samples.push({ s, inRange, eff });
}
emit({ samples });
"""
    )

    result = subprocess.run(
        [NODE, "-e", PRELUDE + script, str(LEDGER)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    samples = report["samples"]

    # 1. Full range reaches the engine verbatim (clamped, no floor).
    assert len(samples) == 401
    assert samples[0]["eff"] == 0, f"slider 0 must map to 0, got {samples[0]}"
    assert samples[-1]["eff"] == 400, (
        f"slider 400 must map to 400, got {samples[-1]}")

    # 2. Strictly monotone: no dead zone anywhere in 0..400.
    for i in range(1, len(samples)):
        prev, cur = samples[i - 1], samples[i]
        assert cur["eff"] > prev["eff"], (
            f"dead zone / non-monotone at raw {prev['s']}->{cur['s']}: "
            f"effective {prev['eff']} -> {cur['eff']}")

    # 3. No saturation plateau at the top end (the old 2x gain clipped at 200).
    unique_eff = {s["eff"] for s in samples}
    assert len(unique_eff) == 401, (
        f"expected 401 unique effective values, got {len(unique_eff)}")
