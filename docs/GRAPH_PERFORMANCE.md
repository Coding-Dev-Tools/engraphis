# Graph performance profiles

The dashboard has two explicit graph presentations:

- **High quality** requests an overview capped at 1,000 entity nodes and 2,000 relations and
  keeps the existing shaded renderer and interaction behavior.
- **All nodes · LOD** requests the complete entity projection up to 20,000 nodes and the
  existing 200,000-relation safety ceiling. An exact repository filter can add its code overlay
  within the same final-node ceiling. All relationships remain indexed in the worker;
  zoomed-out views paint points only, medium zoom paints ranked/visible edges, and focused views
  reveal local labels and relationships. The all-node renderer uses flat dots by design.

All-node preparation runs in `engraphis-graph-worker.js`. WebGL2 is the supported performance
target; browsers without WebGL2 use a flatter Canvas fallback with stricter practical edge
budgets. The all-node path has no live force simulation. Layout presets and force controls run
bounded deterministic settling passes in the worker; relation-flow markers animate only a capped
visible subset and become static directional cues when reduced motion or Freeze is active.

Every shared graph control has an All-node behavior: minimum relations and unlinked toggles filter
worker visibility, neighbourhood depth bounds a focused traversal, relation layers and history
ghosts rebuild the ranked paint set, auto-collapse reduces zoomed-out communities to representative
nodes, and colour, palette, size, labels, line width, fit, reflow, export, and focus remain live.

The Playwright fixture `tests/e2e/graph-all-performance.spec.js` builds 20,000 nodes and 200,000
dense relationships, verifies progressive point/relationship handoff, exercises pan/zoom/focus,
and fails if post-handoff long tasks exceed 50 ms. Run it with the normal Playwright suite on a
mid-range desktop with hardware-accelerated WebGL2 enabled.

If the server has more than 20,000 final nodes or more than 200,000 raw relationships, the
all profile refuses the request with an explicit capacity response. Narrow by repository or entity
type, or reduce the workspace graph; it never silently samples the all-node projection. Time,
layer, and relation filters still shape an accepted scene, but are not advertised as ways around
the raw entity and relationship safety ceilings because those limits are enforced first.
