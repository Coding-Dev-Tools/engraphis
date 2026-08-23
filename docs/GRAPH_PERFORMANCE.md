# Graph performance profiles

The dashboard has two explicit graph presentations:

- **High quality** requests an overview capped at 1,000 entity nodes and 2,000 relations and
  keeps the existing shaded renderer and interaction behavior.
- **Every node** (layout chip "Every node") requests the complete entity projection up to
  20,000 nodes and the existing 200,000-relation safety ceiling via the dedicated Every-node
  engine (`engraphis-graph-every.js` + `engraphis-graph-every-worker.js`). An exact repository
  filter can add its code overlay within the same final-node ceiling.

## The Every-node engine

Design contract: **all geometry is uploaded once and only re-uploaded when data, layout,
colours, or filters change; camera moves touch two uniforms.** Pan/zoom frame cost is
independent of node count — nothing on the GPU moves when you pan.

- **Worker** (`engraphis-graph-every-worker.js`): capacity validation, typed-array
  compaction, deterministic community-seeded placement (districts packed tight, centres
  spread wide), and 26 bounded relaxation passes streamed as `preview → ready → progress →
  layout` messages. Springs are community-aware: intra-district springs run strong,
  cross-district springs weak, and district centroids repel each other so neighbourhoods
  stay separated. The worker is silent once a layout settles — it never sees camera traffic.
- **Renderer**: WebGL2-only (unsupported browsers get an explicit error). Zoom-out
  readability comes from additive glow density — crowded regions melt into brightness —
  with continuous shader-side LOD instead of hard tiers. Edges reveal progressively by
  weight as you zoom (bridges always render, tinted gold). Hovering or highlighting a node
  dims everything outside its direct neighbourhood and marks its relations with directional
  arrows and relation names; picking runs through a local spatial grid with no worker
  round-trip. Labels are decluttered by screen-space occupancy (rank-first). Community
  regions paint as tinted district hulls with hub-derived labels.
- **Interaction**: pointer drag/wheel zoom, two-pointer pinch, keyboard (arrows pan,
  +/- zoom, F fit, Escape clears selection). A screen-reader live region announces scene
  totals and hovered entities; canvases are labelled decorative layers.

Measured worker settle times (deterministic fixture, see `python -m eval.graph_every_bench`,
run inside the dev distrobox where node is available):

| Scale | Settle time |
|---|---|
| 2,000 nodes / 2,667 relations | ~320 ms |
| 20,000 nodes / 26,667 relations | ~1.2 s |

Settle is a one-off cost per data/relayout; per-frame render cost does not grow with node
count. Reduced-motion preferences freeze relation-flow markers. WebGL2 is required.

## Shared controls

Every shared graph control has an Every-node behaviour: minimum relations and unlinked
toggles filter visibility (entering Every-node shows all nodes; leaving restores the
person's overview filters), presets re-run the seeded layout with new force settings,
colour/size/style/palette remain live, and colour, labels, fit, reflow, export, and focus
remain live. Focus-depth traversal and auto-collapse are not yet implemented in the engine;
their controls are disabled honestly while in Every-node mode rather than silently doing
nothing.

The Playwright e2e coverage for graph routing lives in `tests/e2e/ledger.spec.js`; the
worker/renderer contract is pinned by `tests/test_graph_every_asset.py`, which executes the
real worker in Node and asserts the renderer's structural invariants.

If the server has more than 20,000 final nodes or more than 200,000 raw relationships, the
profile refuses the request with an explicit capacity response. Narrow by repository or entity
type, or reduce the workspace graph; it never silently samples the projection.
