# Spec Crawl: Agent Specification Quality & Prompt Crawler

**Spec Crawl** is an offline, deterministic quality crawler and visualizer for AI agent specifications, prompts, and procedural memories in Engraphis v2.

Spec Crawl decomposes specifications into structured sections, classifies tokens, calculates seven-axis coverage, maps cross-section concepts, flags ambiguous phrasing (*"ask, don't guess"*), and screens for injection patterns. Clarifications can be saved as pending procedural memories for review.

---

## 1. Core Architecture

Spec Crawl is built in `engraphis/core/spec_crawl.py` as a pure, dependency-free core engine with zero external imports.

```
Specification Text / Procedural Memories
  │
  ├─ Section Decomposition (numbered headings / markdown hierarchy)
  │
  ├─ Token Classification (functional kinds: owner, approval, spec, source, claim, action, vague)
  │
  ├─ Concept Graph & Tentacles (intra-section and cross-section semantic connections)
  │
  ├─ Seven-Axis Radar Coverage (role, objective, context, team, rules, review, start)
  │
  ├─ Security & Ambiguity Flags (unbounded qualifiers, prompt injection skeletons)
  │
  └─ Score Synthesis (0–100 bounded score: coverage + ownership + approvals − penalties)
```

---

## 2. Seven-Axis Radar Coverage

Every specification is evaluated against 7 foundational axes of agent readiness:

| Axis | Label | Focus Area |
|---|---|---|
| `role` | ROLE | Identity, persona, boundaries, and primary responsibilities |
| `objective` | OBJ | Goals, outcomes, deliverables, and acceptance criteria |
| `context` | CTX | System architecture, data models, environment, and invariants |
| `team` | TEAM | Subagent delegation, bounded workers, and handoff protocols |
| `rules` | RULES | Non-negotiable constraints, load-bearing guidelines, and invariants |
| `review` | REV | Verification, test execution, gate requirements, and sanity checks |
| `start` | START | Commands, quickstart guide, setup instructions, and installation |

Coverage is normalized between 0.0 and 1.0 per axis and displayed as an SVG radar polygon in the dashboard and ASCII bar meters in the CLI.

---

## 3. Token Classification Kinds

Tokens within each section are tagged with functional semantic kinds:

| Kind | Dashboard Color | Semantics |
|---|---|---|
| `owner` | Cyan (`#00e5ff`) | Identities, agents, developers, operators, personas |
| `approval` | Emerald (`#00e676`) | Authorizations, verification, gating, review confirmations |
| `spec` | Blue (`#2979ff`) | Architectural requirements, schemas, APIs, protocols |
| `source` | Purple (`#d500f9`) | Grounded files, datasets, repos, provenance paths |
| `claim` | Amber (`#ffd600`) | Empirical assertions, guarantees, factual statements |
| `action` | Magenta (`#ff4081`) | Executable commands, functions, operations, tools |
| `vague` | Deep Amber (`#ff9100`) | Unbounded qualifiers triggering *"ask, don't guess"* flags |
| `word` | Neutral Grey (`#8892b0`) | Standard grammatical connective tokens |

---

## 4. The "Ask, Don't Guess" Clarification Loop

When prompt specifications contain ambiguous or unquantified qualifiers (such as *"all"*, *"every"*, *"clean"*, *"appropriate"*, *"fast"*, *"sufficient"*, *"properly"*), Spec Crawl flags them immediately with targeted questions:

> *[VAGUE] "all" is unbounded in: "We must verify all unit tests before release." What exact scope, number, or condition do you mean?*

Users or orchestrators can answer the clarification directly via:
1. **Interactive Dashboard Modal**: clicking any flagged token opens a prompt asking for concrete bounds (e.g. *"all 15 core unit tests in tests/"*).
2. **REST API**: calling `POST /api/spec/crawl/answer`.
3. **Smart MCP**: discovering and executing `engraphis_spec_crawl_answer`.

The server verifies that the flag matches the current text before replacing its span. With `save_as_memory=True`, it saves a pending procedural memory (`mtype="procedural"`, `source="spec_crawl"`). Approve it in Library before it can enter model context or procedural-memory crawls. Direct-text crawls evaluate only the supplied text; a clarification does not guarantee a higher score.

---

## 5. Dashboard Spec Studio (`/?view=specstudio`)

The unified dashboard at `http://127.0.0.1:8700` includes a dedicated **Spec Studio** view:
- **Prompt Spec Mode**: Autonomous 16-legged robotic crawler traversing between section particle clouds, deploying radial legs toward active tokens, visualizing cross-section connection tentacles, and providing interactive ambiguity clarifications.
- **Memory Nodes Mode**: Multi-node memory cluster auditor that inspects clusters of active memory records in Engraphis v2 (`workspace -> repo -> session -> memory`):
  - Renders memory nodes colored by type (`semantic`, `procedural`, `working`, `episodic`).
  - Displays links and highlights cross-node parameter / directive contradictions with pulsing red conflict lines.
  - Highlights isolated graph orphans with dashed orange suggestion links.
  - Spider walks between memory nodes, pulsing red on conflicts and orange on orphans.
  - Remediation suggestions include **"Supersede Older"**, which requires confirmation before closing validity, and **"Auto-Link"**. Suggestions are advisory and use the selected workspace and project.

---

## 6. Multi-Node Memory Cluster Auditing

Beyond inspecting isolated single-text prompt specifications, Spec Crawl provides cluster-level coherence auditing across multiple active memory records (`engraphis/core/spec_crawl.py::analyze_memory_nodes`):
1. **Contradiction Detection**:
   - Parameter collisions (e.g. `port: 5432` vs `port: 5433`, timeouts, database names, versions).
   - Opposing modal directives (e.g. `must enforce <target>` vs `never allow / forbid <target>`).
   - Divergences for the same `(subject_key, claim_kind)` are review suggestions; different keyed subjects do not establish parameter conflicts.
2. **Graph Orphan Detection**:
   - Discovers nodes with 0 degrees of connectivity.
   - Computes semantic similarity to suggest nearest-neighbor auto-linking targets.
3. **Redundancy & Near-Duplicates**:
   - Detects duplicate memory claims ($\ge 0.75$ Jaccard similarity) and recommends consolidation.
4. **7-Axis Memory Radar & Operational Gaps**:
   - Evaluates whether the workspace contains memory records covering each of the 7 foundational axes (identifies gaps like missing rules, lack of review procedures, or undefined team boundaries).
5. **Cluster Health Score**:
   - 0–100 coherence score factoring in coverage, graph connectedness, clean node ratio, and deductions for contradictions and orphans.

---

## 7. Command Line Interface (CLI)

Run Spec Crawl directly against any specification file or audit a workspace's memory cluster:

```bash
# Prompt Spec Crawl
python -m scripts.spec_crawl AGENTS.md
python -m scripts.spec_crawl AGENTS.md --json
python -m scripts.spec_crawl AGENTS.md --min-score 70
cat prompt.md | python -m scripts.spec_crawl -

# Multi-Node Memory Cluster Audit
python -m scripts.spec_crawl --workspace acme
python -m scripts.spec_crawl --workspace acme --repo engraphis --json
python -m scripts.spec_crawl --workspace acme --min-score 75
```

---

## 8. REST API Endpoints

### `POST /api/spec/crawl`
Performs an offline, read-only analysis of up to 64,000 characters. HTTP callers must send `text`; server filesystem paths are rejected. The local-operator MCP path accepts text files only under approved index roots.

### `POST /api/spec/crawl/answer`
Submits a clarification for a current flagged ambiguity, returns updated text and a new report, and optionally persists a pending procedural memory.

### `POST /api/spec/crawl/memories`
Audits an active cluster of workspace memories for contradictions, orphans, and operational gaps.

```json
{
  "workspace": "default",
  "include_trace": true
}
```

### `POST /api/spec/crawl/memories/resolve`
Executes an explicit remediation on two live, approved memories after checking scope and ownership. `node_a` is retained; `node_b` is retired for `supersede`. Both the link and validity closure are atomic. Supersession requires `confirmed: true` following human approval.

```json
{
  "action": "supersede",
  "node_a": "mem_01...",
  "node_b": "mem_02...",
  "workspace": "default",
  "confirmed": true
}
```

---

## 9. Smart MCP Tools

These four Classic MCP tools are also discoverable and callable through Smart MCP actions:
- **`engraphis_spec_crawl`**: Read-only prompt spec crawler returning scores, radar coverage, classified tokens, connection tentacles, and flags.
- **`engraphis_spec_crawl_answer`**: Clarification answer submission updating specifications and writing pending procedural memories.
- **`engraphis_spec_crawl_memories`**: Audits active workspace memory nodes for contradictions, orphans, redundancies, and cluster health.
- **`engraphis_spec_crawl_resolve`**: Executes remediation actions (superseding older conflicting nodes or auto-linking orphans).

---

## 10. Evaluation Gate

CI verifies determinism, axis classification, flag recall, qualifier precision, section counts, and score boundaries in both the full stack and NumPy-only jobs:

```bash
python -m eval.spec_crawl
```

Scores measure heuristic coverage and bounded ambiguity penalties. They do not prove execution safety or replace release tests. Partial specifications have low expected scores even when their wording is precise. Claim tracing makes at most 40 evidence lookups; reports return at most 200 flags while scores account for all detected flags.
