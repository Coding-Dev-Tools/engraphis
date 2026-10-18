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
  ├─ Concept Graph & Tentacles (shared concepts within and across sections)
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
Fenced and inline code is read for replay, but contributes neither active-prose word credit nor body-signal credit to coverage.

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

Numbers clear a qualifier only in an explicit bounded phrase: `all 5 tests`, `any of the 2 samples`, `every 5 minutes`, or a duration such as `quickly within 5 seconds`. Counts are integers; cadence and duration values can be fractional. The phrase must stay in one active prose sentence and section. Nearby ports, dates, version identifiers, code examples, or a number in another clause do not define the qualifier's scope. Subjective quality words such as `clean` remain questions even when a nearby task count is precise.

Users or orchestrators can answer the clarification directly via:
1. **Interactive Dashboard Modal**: clicking **Clarify** beside an ambiguity opens a prompt asking for concrete bounds (e.g. *"all 15 core unit tests in tests/"*).
2. **REST API**: calling `POST /api/spec/crawl/answer`.
3. **Smart MCP**: discovering and executing `engraphis_spec_crawl_answer`.

The server verifies that the flag matches the current text before replacing its span. Persistence is opt-in: `save_as_memory` defaults to `False`, and the dashboard offers an unchecked save option. With `save_as_memory=True`, it saves a pending procedural memory (`mtype="procedural"`, `source="spec_crawl"`) in the supplied session, or the selected repo/workspace when no session is supplied. Approve it in Library before it can enter model context or procedural-memory crawls. Direct-text crawls evaluate only the supplied text; a clarification does not guarantee a higher score.

---

## 5. Dashboard Spec Studio (`/?view=specstudio`)

The unified dashboard at `http://127.0.0.1:8700` includes a dedicated **Spec Studio** view:
- **Prompt Spec Mode**: Autonomous 16-legged robotic crawler traversing between section particle clouds, deploying radial legs toward active tokens, visualizing cross-section connection tentacles, and providing interactive ambiguity clarifications.
- **Memory Nodes Mode**: Multi-node memory cluster auditor that inspects clusters of active memory records in Engraphis v2 (`workspace -> repo -> session -> memory`):
  - Renders memory nodes colored by type (`semantic`, `procedural`, `working`, `episodic`).
  - Displays links and highlights cross-node parameter / directive contradictions with pulsing red conflict lines.
  - Highlights isolated graph orphans with dashed orange suggestion links.
  - Spider walks between memory nodes, pulsing red on conflicts and orange on orphans.
  - Remediation suggestions include **"Supersede"**, which requires confirmation before closing validity, and **"Auto-Link"**. Suggestions are advisory and use the selected workspace and project.

---

## 6. Multi-Node Memory Cluster Auditing

Beyond inspecting isolated single-text prompt specifications, Spec Crawl provides cluster-level coherence auditing across multiple active memory records (`engraphis/core/spec_crawl.py::analyze_memory_nodes`):
1. **Contradiction Detection**:
   - Parameter collisions for positively matching, nonempty subject keys (e.g. `port: 5432` vs `port: 5433` for the same database).
   - Sentence-final periods on numeric port, timeout, and retry values or simple bare scalar labels (such as `database: postgres.`) do not create a parameter conflict. Embedded dots, dotted versions, host values, repeated dots, and tokens continuing into paths or URLs remain literal; this conservative rule does not infer whether an opaque resource ends a sentence.
   - Opposing modal directives (e.g. `must enforce <target>` vs `never allow / forbid <target>`).
   - Sharing a `(subject_key, claim_kind)` groups facts but does not establish a contradiction. Conflict findings require differing scalar values or opposing directives; compatible properties remain live. Different keyed subjects do not establish parameter conflicts.
   - A keeper suggestion requires distinct, finite `valid_from` dates. Ingestion order never determines which fact is current. Missing, invalid, or equal effective dates require explicit clarification without a retirement target.
2. **Graph Orphan Detection**:
   - Discovers nodes with 0 degrees of connectivity.
   - Compares token overlap to suggest related nodes for explicit linking.
3. **Redundancy & Near-Duplicates**:
   - Detects duplicate memory claims ($\ge 0.75$ Jaccard similarity) and recommends consolidation.
4. **7-Axis Memory Radar & Operational Gaps**:
   - Evaluates whether the workspace contains memory records covering each of the 7 foundational axes (identifies gaps like missing rules, lack of review procedures, or undefined team boundaries).
   - Each memory retains its dominant `axis` label and separately reports `covered_axes`. Nonempty prose sections can cover multiple axes; repeated headings count an axis only once per memory. Code-only and empty sections do not establish coverage.
   - Additional topics use the existing title/alias/signal weights with a three-point threshold, and active directives establish rules coverage. A signal below the threshold does not remove a policy gap even when it is the strongest match; only prose without any axis scores retains the legacy context fallback.
5. **Cluster Health Score**:
   - 0–100 coherence score factoring in coverage, graph connectedness, clean node ratio, and deductions for contradictions and orphans.

Memory bodies are used during analysis but are not returned in node summaries. The workspace audit analyzes at most 150 eligible memories per request and reports its eligible total and truncation state; Spec Studio warns when its health score and findings describe only a partial sample.

Title aliases and body signals match whole words, so titles such as “Preview configuration” do not count as review policies. Similarity comparisons use active title and body prose outside code examples, with at most 512 distinct tokens per memory selected deterministically by token hash. Each pair's result is reused across link, orphan, and redundancy analysis. Reports expose `similarity_token_limit` and `similarity_sampled_node_count`; each node includes `similarity_token_count` and `similarity_sampled`. Sampled similarity is approximate and produces suggestions for review. Parameter and directive conflict checks inspect the complete memory prose outside fenced and inline code examples. Code examples are also excluded from prompt injection scoring.

Parameter conflicts use explicit scalar assignments (`=`, `:`, `is set to`, or `set to`). Bare `is` establishes a scalar value only for numeric port, timeout, and retry fields; descriptive predicates such as “the database is encrypted at rest” do not establish exclusive configuration values. Directive comparisons match the governed action or requirement together with its object and scope, preserving nested negation. Possible overlap between a general policy and a scoped exception requires clarification and never selects a memory to retire. These checks are heuristics, not general natural-language inference.

Directive identities omit optional `a`/`an`/`the` only at grammatical object or scope positions with a following substantive word. Deployment destinations allow the initial `to`/`into` to be omitted; `into` and `to` also agree for the first deployment-destination phrase after an object. Source, method, and condition words such as `from`, `via`, and `without`, their values, quantifiers, keyed subjects, and opaque resource spelling remain distinct. A general deployment policy and a scoped exception still require clarification rather than a retirement recommendation. These limited grammar rules do not infer arbitrary natural-language equivalence.

Recognized coordinated action phrases retain their governing polarity: requiring `build and test` entails both actions, while `never build or test` prohibits either. Positive `or` alternatives, negated `and` groups, and mixed groups retain their whole-expression identity without independently requiring or prohibiting each member. Complete groups still oppose their exact negation. Serial bare-action lists support commas with or without an Oxford comma: the final `and` or `or` governs preceding list commas, while comma-only lists use sequential `and`. Conjunctions inside an object or scoped tail cannot relabel earlier action-list commas. Thus `must build, test, and deploy` entails testing, but `must build, test, or deploy` does not require any one alternative. Fresh modals or subjects, sentence boundaries, subordinate conditions, and ambiguous object or scope lists stop inherited action extraction; adverbs and concrete target values remain part of the identity.

Directive checks normalize straight and typographic contractions of `do`, `does`, `must`, `should`, `may`, and `can`, including `cannot`, to their explicit negative forms. Qualified hosts, addresses, URLs, paths, ports, and version strings remain complete target identifiers. Opaque resource spelling retains its case; ordinary governing and action words remain case-insensitive. Sentence-ending punctuation separates directives without truncating embedded target punctuation, and long target phrases retain their full identity through a bounded hash key.

Inline code examples use matching backtick runs; runs of a different length inside a span remain literal. Outside code, a backslash escapes only the first backtick of a run. Inside code, backslashes do not escape a closing run. Inline spans can contain soft line breaks, but do not cross blank paragraphs or fenced blocks. Code examples remain excluded from active policy checks.

Word classification reads at most `word_limit` (5,000) tokens per memory. Each node reports `word_analysis_truncated` and `word_count_is_lower_bound`; a truncated `word_count` is the number analyzed, not the full memory length. Reports include `word_analysis_truncated_node_count`, and partial nodes receive no clean-node quality credit. Conflict reports count unique detected node pairs across parameter and directive evidence. `conflicts_total` is the total within the analyzed memory selection, not the whole workspace when that selection is bounded. At most `conflict_limit` (50) detailed conflicts are returned; `conflicts_truncated` identifies omitted details and also sets `analysis_truncated`. The remediation plan contains at most `conflict_remediation_limit` (10) conflict suggestions; `conflict_remediation_count` and `conflict_remediation_truncated` disclose that independent plan bound. Exact-limit results remain complete. Dashboard and CLI show these counts and notices alongside node-selection and word-analysis notices, including when several limits apply together.

HTTP and MCP audits set `truncated` and `analysis_truncated` for omitted memories, incomplete word analysis, or omitted conflict details; `node_selection_truncated` identifies an omitted-memory sample separately.
Spec Studio reports these limits separately: omitted memories produce a node-sample warning, while partial word analysis identifies the affected memory count and token limit without claiming that more memories remain.

An empty memory selection returns the same cluster aliases, seven-axis radar, axis labels, score component fields, and policy-gap remedy fields as a populated audit. Health and coverage remain zero; the empty selection retains an empty trace and remediation plan.

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
Performs an offline, read-only analysis of up to 64,000 characters. HTTP callers can send `text` or set `crawl_procedural: true` to analyze approved procedural memories in the selected scope. Procedural crawls return `procedural_total_eligible_memory_count`, `procedural_memory_count`, and `procedural_source_truncated`; the top-level `truncated` flag is also true when source memories or the analysis token limit omit content. Server filesystem paths are rejected. The local-operator MCP path accepts text files only under approved index roots.

If the first approved procedural memory cannot fit the character budget, the crawl returns a validation error instead of an empty-scope audit. Pass a smaller specification or use a selected-memory audit to inspect that record.

Procedural audits do not trace claims: the source memories are the text being audited and cannot independently verify themselves. `claim_tracing` reports `requested`, `enabled`, and `disabled_reason`; when tracing was requested for this mode, the reason is `procedural_sources_are_not_independent_evidence`. Claims remain `unchecked`, with no grounding bonus or untraced-claim penalty. Direct-text audits can still trace claims against memories in their selected scope.

Claim evidence queries exclude fenced and inline code examples before their excerpt limit is applied. Reports keep the original display text and offsets, so example text remains inspectable without contributing evidence for the surrounding assertion.

`procedural_sources` retains the complete `memory_id` and its `start`/`end` range in the composed, NFC-normalized text with LF newlines. Only memories actually included in the text appear. Sections, flags, and claims list their `source_memory_ids` (several when the section cap merges a tail); flags and claims also carry `source_memory_id` when their origin is a single memory. This disambiguates duplicate titles without treating identifiers as instructions or factual claims.

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
Executes an explicit remediation on two live, approved memories after checking scope and ownership, including workspace ancestors visible from the selected repository. Both memory IDs must be visible through the same workspace/repo/session filter. Omitting `session_id` excludes session-only memories; supplying it authorizes and restricts the action to that one session. `node_a` is retained; `node_b` is retired for `supersede` at the retained memory's `valid_from`, preserving world-time and recording-time history. The lifecycle link starts at that same effective timestamp while its `ingested_at` records the resolution time, so historical lineage appears only when the selected knowledge time includes the resolution. It remains visible for inspecting the supersession chain after retirement. A closure before the retired memory's effective date is rejected. Both the link and validity closure are atomic. Supersession requires `confirmed: true` following human approval.

```json
{
  "action": "supersede",
  "node_a": "mem_01...",
  "node_b": "mem_02...",
  "workspace": "default",
  "session_id": "ses_...",
  "confirmed": true
}
```

---

## 9. Smart MCP Tools

In hosted/team Classic MCP, the two read-only crawls allow viewer access; clarification and resolution require member access. In remote Smart MCP, crawls use the viewer-accessible `engraphis_execute_read`, while clarification and resolution use `engraphis_execute_action` and require admin access. Local stdio retains the operator's full capability.
Omit `workspace` when passing a `session_id` to inherit its workspace and repository; explicitly mismatched scope selections are rejected. Default cluster audits return at most 150 eligible nodes and report `truncated`, `node_limit`, and `count_semantics`; the dashboard labels truncated health scores as a sample. Inferred similarity links are suggestions and do not count as stored connectivity or remove orphan remedies.

These four Classic MCP tools are also discoverable and callable through Smart MCP actions:
- **`engraphis_spec_crawl`**: Read-only prompt spec crawler returning scores, radar coverage, classified tokens, connection tentacles, and flags.
- **`engraphis_spec_crawl_answer`**: Clarification answer submission updating specifications and optionally saving pending procedural memories.
- **`engraphis_spec_crawl_memories`**: Audits active workspace memory nodes for contradictions, orphans, redundancies, and cluster health.
- **`engraphis_spec_crawl_resolve`**: Applies an explicitly requested link or confirmed supersession to selected memory nodes.

---

## 10. Evaluation Gate

CI verifies determinism, axis classification, flag recall, qualified-token avoidance, section counts, and score boundaries in both the full stack and NumPy-only jobs. Qualified-token avoidance measures the explicitly labelled negative examples; it is not a general false-positive precision estimate:

The same gate checks eight synthetic memory-audit contracts in both input orders: unrelated-subject noise, agreeing and opposing nested directives, subject and claim-kind separation, stored orphan connectivity, unknown effective dates, and effective-time ordering. It compares exact conflict pairs and retirement targets, requires compact node responses, and reports the fixture digest. These bounded regression fixtures do not establish general classifier quality.

```bash
python -m eval.spec_crawl
```

The gate checks each metric separately: determinism, axis classification, flag recall, qualified-token avoidance, and section-count accuracy must each reach at least 90%. All score-boundary checks must pass (100%), and the mean of the six metrics must reach at least 90%. AGENTS.md dogfooding must also pass its existing checks: a score of at least 50 and at least eight parsed sections. This branch additionally requires every memory audit fixture contract to pass in both input orders.

Scores measure heuristic coverage and bounded ambiguity penalties. They do not prove execution safety or replace release tests. Partial specifications have low expected scores even when their wording is precise. Claim tracing makes at most 40 evidence lookups; reports return at most 200 flags while scores account for all detected flags.
Failed evidence lookups remain `unchecked` and do not count as unsupported claims or lower the score. A successful lookup with no supporting evidence remains `untraced`.
