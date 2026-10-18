"""Execute shipped dashboard callbacks and temporal helpers against real JavaScript."""
from __future__ import annotations

import json
import subprocess

import pytest

from tests.test_dashboard_auth_placement import (
    _HARNESS_PRELUDE,
    _ledger_declaration,
)
from tests.test_graph_engine_asset import ASSETS, NODE, ROOT, _run_engine


pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run_dates(driver: str) -> dict:
    names = (
        "byId", "text", "empty", "node", "truncate", "validDate", "relative",
        "provenanceTimestampMs", "temporalTime", "automationTime", "renderAuditCards",
    )
    declarations = "\n".join(_ledger_declaration(name) for name in names)
    program = _HARNESS_PRELUDE + declarations + "\n" + driver
    result = subprocess.run(
        [NODE, "-e", program], cwd=ROOT, capture_output=True, text=True, timeout=20,
        check=True,
    )
    return json.loads(result.stdout)


def test_keyboard_cursor_traverses_disconnected_components_without_isolating_graph():
    declarations = "\n".join(_ledger_declaration(name) for name in (
        "revealGraphNode", "focusGraphNode", "graphExplorer",
    ))
    explorer_source = (ASSETS / "graph-explorer.js").read_text(encoding="utf-8")
    report = _run_engine(
        """
const vm = require('vm');
const document = { getElementById() { return null; } };
let explorer = null;
const api = G.create(el, {
  reducedMotion: () => true,
  onStats() { if (explorer) explorer.setData(api.visibleData()); },
});
api.setData({
  nodes: ['a', 'b', 'c', 'd', 'e', 'f'].map((id, index) => ({
    id, name: id, x: index * 20, y: index * 10,
  })),
  links: [
    { source: 'a', target: 'b' }, { source: 'b', target: 'c' },
    { source: 'd', target: 'e' }, { source: 'e', target: 'f' },
  ],
});
"""
        + f"new Function('window', 'document', {json.dumps(explorer_source)})(window, document);\n"
        + """
const status = { textContent: '' }, opened = [];
const context = {
  window, state: { graphEngine: api }, number: Number,
  byId: () => status, showNotice() {},
  openGraphConnections: item => opened.push(item.id),
};
"""
        + f"vm.runInNewContext({json.dumps(declarations + '; globalThis.explorer = graphExplorer;')}, context);\n"
        + """
explorer = context.explorer;
explorer.setData(api.visibleData());
const visited = [], sizes = [];
for (let step = 0; step < 6; step++) {
  explorer.handleKey({ key: 'ArrowRight' });
  visited.push(api.state().highlight);
  sizes.push(api.visibleData().nodes.length);
}
const cursorFocus = api.state().focusId;
explorer.handleKey({ key: 'Enter' });
context.focusGraphNode('a');
const explicitFocus = { id: api.state().focusId, size: api.visibleData().nodes.length };
explorer.handleKey({ key: 'Escape' });
emit({ visited, sizes, cursorFocus, opened, explicitFocus,
  cleared: api.state().focusId, restored: api.visibleData().nodes.length });
api.destroy();
"""
    )
    assert set(report["visited"]) == set("abcdef")
    assert report["sizes"] == [6] * 6
    assert report["cursorFocus"] is None
    assert report["opened"] == [report["visited"][-1]]
    assert report["explicitFocus"] == {"id": "a", "size": 3}
    assert report["cleared"] is None
    assert report["restored"] == 6


def test_historical_dates_preserve_milliseconds_seconds_date_objects_and_iso():
    report = _run_dates(r"""
const cases = [
  [946684800000, 946684800000], ['946684800000', 946684800000],
  [946684800, 946684800000], ['946684800', 946684800000],
  ['2000-01-01T00:00:00Z', 946684800000],
  [new Date(946684800000), 946684800000], [new Date(0), 0],
  [0, 0], [-86400, -86400000], [new Date(-86400000), -86400000],
  [-315619200000, -315619200000], [1735689600000, 1735689600000],
];
const actual = cases.map(([value]) => validDate(value)?.getTime() ?? null);
const expected = cases.map(([, expected]) => expected);
const historical = new Date(946684800000);
const expectedTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(historical);
const expectedRelative = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(historical);
const invalid = [null, undefined, '', '  ', true, false, {}, [], NaN, Infinity, -Infinity, 1e300];
console.log(JSON.stringify({ actual, expected, temporal: temporalTime(946684800000),
  automation: automationTime(946684800000), relative: relative(946684800000),
  expectedTime, expectedRelative, invalid: invalid.map(value => validDate(value)),
  explicitMilliseconds: validDate(86400000, 'milliseconds')?.getTime(),
  explicitSeconds: validDate(100000000000, 'seconds')?.getTime() }));
""")
    assert report["actual"] == report["expected"]
    assert report["temporal"] == report["expectedTime"]
    assert report["automation"] == report["expectedTime"]
    assert report["relative"] == report["expectedRelative"]
    assert report["invalid"] == [None] * 12
    assert report["explicitMilliseconds"] == 86400000
    assert report["explicitSeconds"] == 100000000000000


def test_audit_receipt_order_and_display_honor_declared_timestamp_units():
    report = _run_dates(r"""
const inputs = [
  { ts_ms: 946684800000 }, { ts_ms: 86400000 }, { ts_ms: 0, ts: 100 },
  { ts: 946684800 }, { ts: -86400 }, { created_at: 946684800 },
  { timestamp: '2000-01-01T00:00:00Z' },
];
const target = new FakeElement('section');
document.getElementById = () => target;
renderAuditCards([{ actor: 'audit-2001', ts: 978307200 }], [
  { actor: 'receipt-2000', ts_ms: 946684800000 },
  { actor: 'receipt-epoch', ts_ms: 86400000 },
]);
console.log(JSON.stringify({
  normalized: inputs.map(provenanceTimestampMs),
  actors: target.children.map(card => card.children[1].textContent),
  dates: target.children.map(card => card.children[0].textContent),
  expectedDates: [978307200000, 946684800000, 86400000].map(value =>
    new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(value))),
}));
""")
    assert report["normalized"] == [
        946684800000, 86400000, 0, 946684800000, -86400000, 946684800000,
        946684800000,
    ]
    assert report["actors"] == ["audit-2001", "receipt-2000", "receipt-epoch"]
    assert report["dates"] == report["expectedDates"]
