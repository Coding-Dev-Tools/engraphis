# Engraphis — Release Readiness Audit

_Date: 2026-06-30. Scope: make Engraphis optimal and sellable as a self-hosted, open-core AI
memory engine for agents, with value comparable to mem0, Zep/Graphiti, and Letta/MemGPT
(MASTER_PLAN.md's named competitive set)._

## Verdict

**Ready for a developer-facing open-source launch (beta) of the free core + MCP server.**
The headline capability (MCP) exists, the quality gate is green, the product is rebranded and
licensed, the write path is hardened, and the three biggest feature gaps versus the named
competitors — self-maintaining facts, bi-temporal history, and a code-aware graph — are now
closed with local-first, no-LLM implementations. This pass additionally fixed two real bugs in
the v1 dashboard's Knowledge Graph (fragmented entities, so clicking a node could open the wrong
memory or none) and a stored-XSS hole in its markdown rendering — see "What shipped, by pass"
below. A few items remain before charging for a paid tier (see "Before you charge money" and
`docs/GO_TO_MARKET.md` §10, which is now explicit that the $20/mo Pro tier is a well-anchored
*target price*, not yet something with a shipped feature behind it).

## Quality gate — green

| Check | Status |
|------|--------|
| `pytest tests/` | **127 passed, 0 skipped** with `server`+`mcp` extras installed (was 55). In the CI gate's minimal `numpy`+`pytest`-only install, `test_app_auth.py`/`test_mcp_server.py` skip gracefully via `importorskip` instead — everything else, including the new `test_ingest_entities.py`, still runs and passes on `numpy` alone. |
| `eval.harness` (sample + codemem, 14 cases / 26 questions) | green |
| `eval.ablation` | green |
| `ruff check .` | clean |

The offline gate needs no network or API key, runs on `numpy` + `pytest` alone, and mirrors CI
(`.github/workflows/ci.yml`).

## What shipped, by pass

**Dashboard & pricing-validation pass (this pass)**:

- **Fixed the Knowledge Graph node-click bug**: entity extraction (`engines/ingest.py`) built
  its regex input as `title + "\n\n" + content`, letting the capitalized-word entity pattern
  match *across* that boundary — one real entity (e.g. "Alice Johnson") could fragment into
  multiple garbled, differently-named graph nodes, each holding only part of its real document
  set. That's why clicking a node could open the wrong memory, or none. Fixed by extracting
  title/content as independent passes and merging by name; regression-tested end-to-end in the
  new `test_ingest_entities.py` (5 tests). Verified via direct reproduction against the store
  (not just reasoning about the code): the same entity across two documents now correctly
  resolves to one node listing both document ids.
- **Fixed a stored-XSS hole in the same dashboard**: markdown rendering (`marked` v12, which does
  not sanitize embedded HTML) was inserted into `innerHTML` unsanitized at all three render
  sites. A poisoned memory could run arbitrary JavaScript when viewed — the same
  untrusted-content threat model SECURITY.md already names, reachable through the UI rather
  than the API. Fixed with `DOMPurify.sanitize()`; verified against a live payload
  (`<img onerror=...>`) that the attribute is stripped while ordinary markdown is unaffected.
- **Validated the $20/mo pricing target against current (2026-06-30) competitor pricing**:
  mem0 $19/$79/$249, Letta $20 Pro, Zep $125/$375 Flex tiers (credit-metered; corrected
  2026-07-01 — an earlier pass mistook Zep's per-credit overage rate for its base plan price,
  see `docs/GO_TO_MARKET.md` Sources) — see `docs/GO_TO_MARKET.md` §3/§10.
  Finding, stated plainly there: $20/mo is a well-anchored target price, but nothing in the
  product yet is a *shipped, paid-only* feature — the free core is the whole product today. The
  dashboard fixed this pass is the closest candidate for a first Pro deliverable (hosted +
  multi-user), not yet a reason to charge on its own.

**Competitive-feature pass (previous)** — closes the gap to mem0/Zep/Letta named in
MASTER_PLAN.md; full detail in `CHANGELOG.md`:

- Deterministic write-path conflict resolution (`core/resolve.py`): every `remember()` checks
  same-scope neighbors and decides add / reinforce-duplicate / supersede from token overlap —
  no LLM call, so the numpy-only core guarantee holds. This is the mem0-style "self-maintaining
  memory" capability; previously every write was a blind insert.
- Bi-temporal payoff tools: `why` (live answer + what it superseded) and `timeline` (full
  history including invalidated facts) — the Zep/Graphiti-style capability the schema supported
  since Phase 0 but no tool ever surfaced.
- Governance tools: `forget`, `pin`, `correct` — previously there was no audited way to fix or
  remove a bad memory once written.
- Proactive recall + real cross-session handoff (`recall_proactive`, `start_session.bootstrap`).
- Code-symbol graph (`backends/codegraph.py`, `index_repo`/`search_code`): tree-sitter when
  installed, dependency-free regex fallback otherwise — Engraphis's distinct wedge that none of
  the three named competitors target (code-aware memory for coding agents).
- `eval/harness.py` now exercises the real `MemoryEngine` pipeline (RRF + scoring + rerank +
  conflict resolution), not just the bare vector index — the CI gate now measures what ships.
- MCP tool count: 5 → 15.

**Rebrand/MCP-launch pass (earliest)**:

- MCP server (`engraphis-mcp`) on a validated `MemoryService` layer.
- Security hardening: input validation, control-character stripping, size caps, provenance
  (memory-poisoning defense), optional constant-time bearer-token auth, loopback CORS default.
- Clean rebrand off third-party (neocortex/tinyhumansai) lineage; `engraphis.db` default.
- Licensing & packaging: Apache-2.0 `LICENSE` + `NOTICE`; open-core `pyproject.toml`
  (`server`/`mcp`/`code`/`all` extras); `Dockerfile` + `docker-compose.yml`; `CHANGELOG.md`.
- GTM assets: `docs/GO_TO_MARKET.md`, `docs/landing-page.md`.

## Strengths

- Principled, testable core (six-term score, Ebbinghaus decay, RRF, bi-temporal truth, scoping).
- Genuinely local-first and dependency-light — the differentiator vs. hosted competitors — and
  this pass kept that guarantee while adding conflict resolution and code parsing, both of which
  have offline-capable paths with no LLM or network call required.
- Interface-first design keeps backends swappable (numpy → sqlite-vec/Qdrant, regex → tree-sitter)
  without refactors; proven twice now (the original backend factories, and `get_code_indexer`
  added this pass under the same pattern).
- Governance and bi-temporal history are now reachable by an actual agent/user, not just present
  in the schema.

## Before you charge money (prioritized)

1. **Real benchmark numbers** *(adapter now ships: `python -m eval.external --dataset
   locomo10.json --format locomo --k 10` — needs torch + the dataset locally; measures
   evidence retrieval recall@k honestly, not judge-scored QA)*: run LoCoMo/LongMemEval with the sentence-transformers embedder and
   publish. Scope decision this pass was deliberately offline-only (bigger `codemem.jsonl` fixture
   set, harness bug fix) — the offline eval validates pipeline correctness, not semantic recall
   quality against the named competitors' published numbers. Still open.
2. **Trademark/name clearance** for "Engraphis" (a quick search found no obvious collision, but
   that is not legal clearance).
3. **Encryption-at-rest** and **built-in rate limiting** (today: rely on disk encryption + a
   reverse proxy) — required for the regulated/Enterprise ICP.
4. **Per-token tenant authorization** if you sell multi-tenant; today, isolate by instance.
5. **An actual Pro-tier feature to sell.** *(RESOLVED 2026-07-03 — see
   `docs/LAUNCH_PLAN.md` and `CHANGELOG.md`: offline signed license keys now gate three
   shipped features — Pro analytics dashboard, compliance export, and multi-user Team mode
   on the Inspector — with activation UX in the product. Remaining before charging: rotate
   the dev vendor keypair, set the real purchase URL, wire a merchant of record.)* The v1 dashboard had two real bugs fixed this pass
   (Knowledge Graph fragmentation, stored XSS) and is now correct, but "correct" isn't "worth
   $20/mo" — it's still a single-user local dashboard with no hosting, login, or team sync. See
   `docs/GO_TO_MARKET.md` §10 for the specific recommendation (hosted + multi-user version of
   this dashboard is the shortest path to a first real Pro deliverable).
6. **CI matrix** on Python 3.9/3.11 and a published wheel to PyPI; smoke-test the Docker image.
7. **v1 input hardening**: unlike v2's `service.py`, v1's REST models have no content size caps
   or control-character stripping (SECURITY.md, "Known limitations"). Lower priority than the
   XSS fix (that one's exploitable by a poisoned memory alone; this one needs an oversized/
   control-char payload too), but real if v1 stays in the shipped product.

## Known limitations (documented, not blockers for OSS beta)

- **Conflict resolution is heuristic (token-Jaccard), not semantic/LLM-based.** It will miss
  paraphrased contradictions that share no vocabulary and can be tuned via the thresholds in
  `core/resolve.py`. This was an explicit, deliberate scope choice (deterministic + offline over
  LLM-dependent) — not an oversight — but it means resolution quality is a known ceiling versus
  competitors that use an LLM judge.
- **Graph recall is 1-hop entity expansion, not full personalized PageRank** — cheaper and more
  predictable, but it will not surface multi-hop relationships the way Graphiti's graph traversal
  can.
- **No consolidation / "sleep-time compute" loop** (MemGPT-style background summarization or
  memory reorganization) — recall and decay are computed at query/write time only.
- **No LLM-based fact extraction** — callers pass already-distilled text to `remember()`; there is
  no built-in step that turns a raw conversation transcript into discrete facts.
- **Call-graph edges are name-based, not type-resolved** — `search_code`'s `called_by` can include
  false positives when two symbols share a name across files/classes.
- The legacy v1 REST server remains a compatibility surface; new capability targets the v2 core
  and the MCP server.
- The MCP server is single-process/local-user oriented (SQLite, `check_same_thread=False`); fine
  for the wedge, revisit for multi-tenant hosting.
