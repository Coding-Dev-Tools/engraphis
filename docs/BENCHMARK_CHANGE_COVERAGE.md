# Benchmark change coverage

This is the change-to-evidence map for the screenshot reference at `a4c19eee76163bd4e4435ae5724472f51a1ed5f3`, the `v1.7.4` release at `ea6ed79c6d86e69f98b075614e64e50d9fd24d11`, and the campaign's frozen pre-expansion main endpoint at `ca790261f499e0d124cdc7131235ffff89637248`. The subsequent benchmark implementation is recorded in local commits `11abc345`, `7370e148` and `cc25ac5b`; the original OAuth pilot binds `7370e148` and its eligible continuation binds `cc25ac5b`. This is a historical capability inventory, not a claim that remote main still has the same head.

The screenshot commit is not an ancestor of current `main`. The two histories meet at
`54c9985aa41304d8420437d388b7ee8ac1be56dd`, so a direct two-dot diff would mix branch-only
changes with the chronological release history. This map uses the screenshot commit as the
visual baseline, then uses the current first-parent history, `CHANGELOG.md`, current source, and
current tests for the capability inventory. It does not infer a user outcome from a file diff.

The screenshot's tiny synthetic evidence remains historical context: 35/35 quality on its fixture
and three graph questions. Its 98.21% replay-volume panel is retired because a matching retained
result artifact was not found. These image claims are not coding-agent
session outcomes, independent repository evidence, model-comparison results, or an end-to-end quality
claim. The current public numeric registry is the checksum-bound record in
[`BENCHMARKS.md`](../BENCHMARKS.md) and [`docs/benchmark-evidence/offline-fixtures-v23.json`](benchmark-evidence/offline-fixtures-v23.json).

The audit inputs are reproducible with `git show -s --format=fuller a4c19eee`,
`git show -s --format=fuller ea6ed79c`, `git show -s --format=fuller ca790261`,
`git merge-base a4c19eee ca790261`, and `git log --first-parent --format='%h %ad %s'
54c9985a..ca790261`. The capability descriptions come from the current
[`CHANGELOG.md`](../CHANGELOG.md), current source and tests, and the evidence contracts in
[`docs/RELIABILITY_PROGRAM.md`](RELIABILITY_PROGRAM.md), [`BENCHMARKS.md`](../BENCHMARKS.md), and
[`docs/PUBLIC_BENCHMARK_RUNBOOK.md`](PUBLIC_BENCHMARK_RUNBOOK.md).

## Evidence status

| Status | Meaning |
|---|---|
| `CHECKED_IN_ARTIFACT` | A current checksum-bound artifact exists and its metric boundary is stated. |
| `LOCAL_GATE` | A deterministic evaluator or focused regression command exists in the current tree. This label does not mean it was rerun for this document. |
| `PARTIAL` | Local tests or plumbing cover part of the behavior, but a user-outcome, independent, external, browser, or production-like measurement is missing. |
| `NOT_RUN` | The command or protocol is available, but no result is claimed here. |
| `BLOCKED` | A required dataset, paid/provider call, protected authority, attended browser/host, or independent reviewer is absent. |

A benchmark row must keep task success, evidence retention, citation support, answer completeness,
answerable abstention, critical violations, context usage, and operational timing as separate fields.
Token overlap is an answer-token diagnostic and is never a semantic completeness score. A missing
gold evidence set is `N/A`, not a perfect retention result. The local coding corpus is explicitly
implementation-authored synthetic plumbing and cannot stand in for independent human-authored
coding tasks.

## Capability coverage

### Evidence, retrieval, and coding outcomes

| Change and user outcome | Executable benchmark, metric, and evidence | Status and boundary |
|---|---|---|
| Screenshot context-efficiency visual and its later checksum-bound replacement (`a4c19eee`, `078370ee`, `e25eb0ab`, `bb3a8f05`) | `python -m eval.chunking_eval --dataset eval/datasets/longdoc.jsonl --k 5`; `python -m eval.performance --dataset eval/datasets/codemem.jsonl --k 5 --iterations 10 --json <report.json>`; `python scripts/render_benchmark_report.py --report docs/benchmark-evidence/offline-fixtures-v23.json --output <svg>`; `tests/test_benchmark_evidence.py` | `CHECKED_IN_ARTIFACT` for v23 fixture values, source/config/suite digests, and the rendered SVG. Historical v22, v21, v20, v19, v18, and screenshot values are retained only as historical evidence. Transport bytes, provider billing, and end-to-end model quality are not measured. |
| Core retrieval arms, temporal facts, scope filters, graph expansion, retention, and rank-sensitive quality (`3b5d4b12`, `3258aeb9`, `139cbb47`, `18329d8c`, `4e327f51`) | `python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5`; repeat for `codemem.jsonl` and `graph_multihop.jsonl`; `python -m eval.ablation`; `tests/test_bitemporal_recall.py`, `tests/test_recall.py`, `tests/test_context_scope_grounding.py`, `tests/test_graphrank.py` | `LOCAL_GATE`. Metrics are recall@k, hit@k, MRR, nDCG, and temporal/scope correctness on implementation-authored fixtures. This is retrieval evidence, not coding-agent task success or a neutral ecosystem ranking. |
| Optional sentence-transformer/cross-encoder retrieval, embedding-space provenance, native exact KNN, and safe fallback (`18329d8c`, `4e327f51`, `d7d597ce`) | `python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5`; `python -m eval.agent_benchmarks --help`; `tests/test_backends_factories.py`, `tests/test_vector_sqlitevec_backend.py`, `tests/test_native_coverage_equivalence.py` | `PARTIAL`. Full LoCoMo and LongMemEval retrieval diagnostics now use pinned semantic MiniLM with checksummed artifacts; see [measured results](BENCHMARK_EXPANSION_RESULTS.md). They do not establish reranker, competitor or coding-task superiority. |
| Grounded answers, citations, abstention, and poisoned-memory exclusion (`e25eb0ab`, `18329d8c`, `d7d597ce`) | `python -m eval.grounded`; `python -m eval.adversarial_memory_security`; `python -m eval.redteam_poisoning`; `tests/test_grounded.py`, `tests/test_context_scope_grounding.py`, `tests/test_poisoning.py`, `tests/test_rescan_poisoning.py` | `LOCAL_GATE`. Reports answerable grounding, unsupported abstention, quarantine, and authorization boundaries. No LLM answer-quality or customer study is asserted. |
| Reworded mutable-fact correction, false-NOOP protection, temporal splice narrowing, and batch write resolution (`439aec4f`, `d83b324f`, `550602d7`, `0d607f4b`, `d8c97d90`) | `python -m eval.resolver_reworded_corrections --strict`; `python -m eval.coding_acceptance --fixture`; `tests/test_resolve.py`, `tests/test_resolver_acceptance.py`, `tests/test_remember_many.py`, `tests/test_storage_concurrency_repair.py`, `tests/test_release_write_path.py` | `LOCAL_GATE` for deterministic 44-pair and real-write fixtures. The 44 pairs are implementation-authored and do not prove independent mutable-fact generalization. |
| Ebbinghaus retention, interaction reinforcement, proactive agenda, and decay trajectories (`3b5d4b12`, `18329d8c`, `d7d597ce`) | `python -m eval.reinforcement`; `python -m eval.proactive_ranking`; `tests/test_retention.py`, `tests/test_decay_idempotent.py`, `tests/test_proactive_context.py`, `tests/test_proactive_ranking.py` | `LOCAL_GATE`. Metrics are deterministic retention/ordering trajectories and bounded stability on fixtures. They do not measure a user's long-run productivity or recall quality. |
| Code-symbol retrieval and memory bridges, including same-file call fallback (`813475ff`, `45ed3200`) | `python -m eval.code_arm`; `tests/test_eval_code_arm.py`, `tests/test_code_recall_arm.py`, `tests/test_codegraph.py`; repository indexing through `tests/test_service.py` | `LOCAL_GATE` for code evidence retrieval and symbol/call plumbing. No model-generated coding patch success or held-out repository result is claimed. |
| Adaptive context, planned recall, context routing, source-unit retention, and context savings (`2fce865d`, `17aa1d6f`, `a8eeed02`, `439aec4f`, `d7d597ce`) | `python -m eval.context_economy --dataset eval/datasets/codemem.jsonl --token-budget 512 --k 5`; `python -m eval.productivity --dataset eval/datasets/codemem.jsonl --max-context-tokens 512 --retrieval-token-budget 256`; `python -m eval.planned_recall`; `python -m eval.performance --dataset eval/datasets/codemem.jsonl --k 5 --iterations 10 --json <report.json>`; `tests/test_context_economy.py`, `tests/test_context_evidence_preservation.py`, `tests/test_adaptive_context.py`, `tests/test_planned_recall_eval.py` | `PARTIAL`. The measurable fields are task/retrieval quality, packed token count, source completeness, citations, and omissions. The historical 49-fact savings point is a fixture observation, not an ecosystem result; no provider token bill or end-to-end coding benefit is claimed. |
| Implementation-authored coding corpus, repository oracle, reader callback, family-clustered holdout, and separate five-arm acceptance binding (`d7d597ce`, `ea6ed79c`, current campaign work) | `python -m eval.coding_corpus --materialize --verify`; `python -m pytest tests/test_coding_corpus.py -q`; `python -m eval.coding_acceptance --corpus eval/datasets/coding_memory_v1/manifest.json --fixture`; campaign wiring is described in [`docs/CODING_ACCEPTANCE_CORPUS.md`](CODING_ACCEPTANCE_CORPUS.md) | `PARTIAL`. The 400 scenarios are implementation-authored synthetic disposable repositories with 40 family IDs in ten template groups. Eight development, eight validation, and 24 held-out families are structurally reserved, but this is not independent human or real-customer evidence. |
| Additional public datasets and adapters: MemoryAgentBench, LoCoMo-Plus, and Mem2ActBench (`d1be5bc6`, `a4c19eee`, current adapter) | `python -m eval.agent_benchmarks --dataset <JSON-or-JSONL> --format <format> --k 5 --json <report.json> --artifact <artifact.json>` where `<format>` is one of `memoryagentbench`, `locomo_plus`, or `mem2actbench`; source pins, private prepared inputs, and boundaries are in [`docs/ADDITIONAL_BENCHMARK_DIAGNOSTICS.md`](ADDITIONAL_BENCHMARK_DIAGNOSTICS.md) and [`eval/configs/additional-benchmark-sources.json`](../eval/configs/additional-benchmark-sources.json) | `PARTIAL`. The 380-case Mem2ActBench retrieval diagnostic is complete with 20 declared exclusions; Two MemoryAgentBench splits are complete; the other two and the LoCoMo-Plus Cognitive slice remain queued. LoCoMo-Plus all-category loading remains `BLOCKED` by upstream missing-answer and missing-evidence rows. These adapters do not reproduce upstream LLM judges, agent orchestration or tool execution. |
| LongMemEval V2, LoCoMo evidence repair, external retrieval, and model-dependent evaluation (`a4c19eee`, `18329d8c`, current repair manifests) | `python -m eval.external --dataset <pinned-dataset> --format locomo --offline --limit 2`; full external command in [`BENCHMARKS.md`](../BENCHMARKS.md); `python -m eval.run_longmemeval_v2 --help`; `tests/test_eval_external.py`, `tests/test_benchmark_longmemeval_v2.py` | `PARTIAL` overall. Full-source LoCoMo and LongMemEval retrieval diagnostics are `COMPLETE`, with pinned model/source bytes, packed-evidence metrics, confidence intervals and retained artifacts in [the results report](BENCHMARK_EXPANSION_RESULTS.md). Official LongMemEval-V2 remains `BLOCKED` on resources and a qualified OAuth judge path. No leaderboard or end-to-end QA score is claimed. |
| Operational memory capacity at 100k memories and multiple agents (`2fce865d`, `d1be5bc6`, `ea6ed79c`, [`docs/ENGINE_CAPACITY_PROTOCOL.md`](ENGINE_CAPACITY_PROTOCOL.md)) | `python -m eval.local_capacity_campaign --help`; `python -m eval.engine_capacity --help`; `python -m pytest tests/test_capacity_matrix.py tests/test_engine_capacity.py tests/test_local_capacity_campaign.py -q` | `PARTIAL`. Both four-process backend smokes completed with 100 operations and no observed correctness failures. The serial local queue includes all 24 current-host cells with five fresh repetitions each. Those queued cells are not observed results; no 100k end-to-end capacity claim exists. |

### Documents, sync, storage, and multi-agent state

| Change and user outcome | Executable benchmark, metric, and evidence | Status and boundary |
|---|---|---|
| Universal local document import, Obsidian import, resumable manifests, temporal re-import, mixed formats, truncation, per-file errors, and worker recovery (`043dd65c`, `8cab3c82`, `b21d859a`, `b07ac337`, `607af8b5`) | Focused regression: `python -m pytest tests/test_document_importer.py tests/test_document_import_cli.py tests/test_documents.py tests/test_obsidian_importer.py tests/test_obsidian_service.py tests/test_resource_import_atomicity.py -q`; executable journeys: `python -m eval.user_journeys --journey mixed_document_import`; [`docs/USER_JOURNEY_BENCHMARKS.md`](USER_JOURNEY_BENCHMARKS.md) | `PARTIAL`. Counts, digest lineage, parse failures, truncation, re-import deduplication, and rollback are measurable. The journey runner is local disposable-store evidence, not a large-vault throughput or customer import study. |
| Encrypted Cloud Sync bundles, source scope, tombstones, generation-aware repair, and current-canonical publication (`fd150ffe`, `a0c867b6`, `21113180`, `705e4639`, `5373e2c0`) | `python -m pytest tests/test_sync.py tests/test_sync_e2ee.py tests/test_sync_cli.py tests/test_sync_index_repair.py tests/test_sync_tombstones.py -q`; `python -m scripts.sync --help` | `LOCAL_GATE` for local encryption, scope, tombstone, and repair contracts. Relay, cloud authorization, restore/RPO/RTO, and multi-device production behavior remain unverified. |
| Writable SQLite WAL/FULL durability, balanced policy, abrupt-exit rollback, vector repair, secure erase, and cross-process writes (`18329d8c`, `d94c6f81`, `550602d7`, `c4964809`, `ea6ed79c`) | `python -m pytest tests/test_sqlite_durability.py tests/test_storage_concurrency_repair.py tests/test_conflict_repair.py tests/test_recall_recovery.py -q`; `python -m eval.repair_discovery`; [`docs/SQLITE_DURABILITY.md`](SQLITE_DURABILITY.md) | `LOCAL_GATE`. Disposable process-exit and database-full tests cover software behavior. Hardware power loss, filesystem corruption, and production restore objectives remain unverified. |
| Schema receipts, expected versions, typed conflicts, project workflows, paginated history, cursors, bounded read snapshots, and no repeated graph migrations (`4f775c62`, `8015713e`, `68dcc3a9`, `ea6ed79c`) | `python -m pytest tests/test_receipts.py tests/test_memory_revisions.py tests/test_history_scope.py tests/test_memory_browsing.py tests/test_browse_portable.py tests/test_live_read_snapshots.py tests/test_startup_transform_gates.py -q`; `python -m eval.user_journeys --journey index_repair --journey erase_restart` | `PARTIAL`. Correctness and restart behavior are locally exercisable. No multi-host cursor SLO or hosted workflow latency is claimed. |
| Request-scoped services, hosted context isolation, managed-processing approval and explicit operator opt-out (`12e6f136`, `8015713e`, `8a5181ff`, `550602d7`) | `python -m pytest tests/test_service_context.py tests/test_v2_service_binding.py tests/test_service_isolation.py tests/test_managed_processing_policy.py tests/test_cloud_session.py -q`; `python -m pytest tests/test_hosted_client.py tests/test_hosted_evidence.py -q` | `PARTIAL`. Local authorization and isolation contracts are covered. Real Workers, account identity, deployment, billing, and production tenant isolation require attended hosted evidence. |
| Device connect, hosted plan/trial state, license authority, and authenticated local browser sessions (`3258aeb9`, `139cbb47`, `18329d8c`, `4f775c62`) | `python -m pytest tests/test_agent_connect.py tests/test_device_connect.py tests/test_cloud_features.py tests/test_hosted_plan_resolution.py tests/test_local_auth_browser_session.py -q`; `python scripts/connect.py --help` | `PARTIAL`. Local identity, plan resolution, and fail-closed authorization are testable. Account identity, live entitlement, email, payment, and hosted deployment evidence remain attended gates. |
| Multi-agent session start/end identity, scope ownership, handoff continuity, and Command Code context hook (`139cbb47`, `d7d597ce`, `439aec4f`, `230573d0`) | `python -m pytest tests/test_session_idempotent.py tests/test_session_start_hook.py tests/test_install_cc_hook.py tests/test_workspace_isolation.py -q`; hook install/doctor commands are in [`docs/AGENT_CONNECT.md`](AGENT_CONNECT.md) | `PARTIAL`. Session and bounded `additionalContext` contracts are local. A coding-agent user-outcome run must still compare no-memory, full-history, lexical, dense, and hybrid arms on held-out tasks. |

### Interfaces, graph UX, and release-facing behavior

| Change and user outcome | Executable benchmark, metric, and evidence | Status and boundary |
|---|---|---|
| Smart and Classic MCP surfaces, budget-bound recall context, subject-key forwarding, error envelopes, capability discovery, and content-free telemetry (`139cbb47`, `d7d597ce`, `550602d7`, `a74edeb4`) | `python -m pytest tests/test_smart_mcp_gateway.py tests/test_mcp_contract.py tests/test_mcp_server.py tests/test_mcp_annotation_idempotency.py -q`; `python scripts/export_mcp_contract.py` | `LOCAL_GATE`. Contract/schema and context-budget assertions exist. They do not establish tool-call success with independent clients or provider latency. |
| MCP stdio wire isolation, handshake-before-warmup, thread-safe startup, cached semantic model, stateless HTTP restarts, and Pi compatibility (`c9c9ca2f`, `607af8b5`, `6bd7e214`, `ea6ed79c`) | `python -m pytest tests/test_mcp_server.py tests/test_mcp_contract.py tests/test_start_dashboard.py`; `Set-Location integrations/pi; npm test` when the Node dependencies are installed; `Set-Location integrations/pi; npm run typecheck` for Pi typing | `PARTIAL`. Protocol and package tests are executable. Cross-client installed binaries, cold-start distributions, and production HTTP restarts remain separate release evidence. |
| Galaxy v6 through v13, physics controls, central/local orbit invariants, scene caps, Every-node renderer, accessibility, and cold graph-load protections (`4e327f51`, `81d342e5`, `5e61b07a`, `d7d597ce`, `e260794d`, `d5b819b2`, `51992d94`, `1.7.3`) | `python -m eval.graph_every_bench`; `npm test -- --runInBand` for the browser suite; `tests/test_graph_every_asset.py`, `tests/test_graph_scene_contract.py`, `tests/test_galaxy_gravity_slider.py`, `tests/test_galaxy_operating_bounds.py`; [`docs/GRAPH_PERFORMANCE.md`](GRAPH_PERFORMANCE.md) | `PARTIAL`. Scene correctness, WebGL2 fallback, accessibility, and bounded layouts are tested. A real 20k-node/200k-relation latency curve and user interaction study are not current public measurements. |
| Dashboard Ledger/Ask/Home/Explore, project workflows, history, independent Ask states, renderer lifecycle, and visible capability diagnostics (`1.4.5`, `1.6`, `4f775c62`, `ea6ed79c`) | Browser: `npx playwright test tests/e2e/ledger.spec.js tests/e2e/ask-home.spec.js tests/e2e/history-project.spec.js tests/e2e/memory-workflow.spec.js`; focused Python: `python -m pytest tests/test_dashboard_v2.py tests/test_service_graph.py tests/test_workflow_diagnostics.py -q` | `PARTIAL`. Browser behavior is covered when the attended browser and assets are installed. No production browser fleet or accessibility certification is claimed. |
| Prime-agent fleet wrapper, shared workspace, install path, and agent tool contract (`886ee210`) | `python -m pytest integrations/prime_agent/tests -q`; `python -m engraphis_prime_agent --help`; [`integrations/prime_agent/README.md`](../integrations/prime_agent/README.md) | `LOCAL_GATE`. Package and contract tests do not measure multi-agent task success, memory isolation at 100k scale, or external agent adoption. |
| Release identity, installed MCP/dashboard writes, restart and correction history, build/capability diagnostics, and source/distribution receipts (`4fe7f1c3`, `3ab83fbb`, `c4964809`, `ea6ed79c`, `ca790261`) | `python scripts/check_release_readiness.py --help`; `python scripts/verify_release_qualification.py --help`; `python scripts/smoke_installed_product.py --help`; `python -m pytest tests/test_release_evidence.py tests/test_release_qualification.py tests/test_product_release_readiness.py tests/test_installed_release_evidence.py -q`; `ruff check .`; `python scripts/check_commercial_manifest.py` | `PARTIAL`. Source and distribution checks are executable. Owner-signed qualification, protected release authority, external deployment, and real tenant writes are attended release gates, not implied by a green local suite. |
| Security and trust boundaries: path redaction, SQL parameterization, private state, pypdf floor, review/quarantine, and sync pointer validation (`2632c8a7`, `4e327f51`, `705e4639`, `9ea6965f`) | `python -m pytest tests/test_hotfix_security.py tests/test_http_security.py tests/test_secret_hygiene.py tests/test_private_state_boundaries.py tests/test_read_only_api.py tests/test_sync_e2ee.py tests/test_release_grounded_eval.py -q`; dependency audit in release workflow | `LOCAL_GATE` for source-level contracts and redaction. A third-party penetration test, malicious document corpus, and deployed relay threat assessment are not current evidence. |

## Release coverage index

This index ties the detailed rows to the user-facing release checkpoints in the requested interval.
Changelog entries remain the source of the complete prose; the rows above define the executable
metric and its boundary.

| Checkpoint | User-facing capabilities covered by the rows above |
|---|---|
| Screenshot baseline `a4c19eee` and shared ancestor `54c9985a` | Context-efficiency image, graph visual, Smart/Classic documentation, benchmark artifact provenance, LongMemEval and external-loader plumbing, local dashboard and MCP behavior. Historical tiny-fixture values remain non-promotional. |
| 1.2.0, 1.2.1, and 1.2.5 (`3258aeb9`, `fd150ffe`, `d1be5bc6`) | Hard context budget, valid-at/known-at/as-of temporal reads, code graph and memory entities, encrypted sync, adaptive context, productivity/context-economy metrics, external loader contracts, and evidence metadata. Covered by retrieval, context, sync, and external rows. |
| 1.3.0 and 1.4.5 (`17aa1d6f`, `139cbb47`) | Governed recall/consolidation, planned recall, Smart gateway, Pi surface, scoped writes, schema migrations, authorization and release boundaries. Covered by memory, storage, MCP, and hosted rows. |
| 1.5.0 and release-readiness follow-up (`18329d8c`, `3ab83fbb`) | Embedding provenance and rebuild safety, native exact KNN, poisoning/review gates, failure-atomic supersession, release evidence, installed smoke, and version diagnostics. Covered by retrieval, security, and release rows. |
| 1.6 and 1.6.1 (`043dd65c`, `2632c8a7`) | Universal documents and Obsidian, Galaxy v6, schema 14-16 source lineage and erasure, preview tokens, read-only inspection, SEC-001 path redaction, pypdf floor, and CI audit. Covered by documents, storage, graph, and security rows. |
| 1.7 through 1.7.3 (`4e327f51`, `d7d597ce`, `c9c9ca2f`, `550602d7`, `4f775c62`, `1.7.3`) | SEC-002, Galaxy v6 to v13, Every-node, context packing and routing, reranker option, reworded correction, narrow-arm controls, SessionStart and prime-agent integrations, MCP startup, scoped reliability, projects/history, hosted request isolation. Covered by all four capability sections. |
| 1.7.4 through the frozen main endpoint (`c4964809`, `ea6ed79c`, `ca790261`) | Durable SQLite policy, consolidation visibility batching, schema 18 receipts and revalidation, generation-aware sync repair, evidence-preserving context fallback, project/history/cursor workflows, Ask/Home/Explore states, strict coding and capacity infrastructure, Pi compatibility, and CodeQL permission fix. Covered by storage, state, coding, interface, and release rows. That production endpoint has no verified 100k claim. |

## Benchmark implementation after the frozen main endpoint

The local implementation commits add separate retrieved/packed evidence metrics, executable
corpus/oracles, isolated adapters, conservative reservations, resumable checkpoints, native
Codex OAuth-only readers, and source-bound serial local queues. They preserve production engine
defaults. The original pilot source is `7370e148d7793aeb632bc92494528376ec35267e`.
The continuation source `cc25ac5b59e3b5e04a79ee09b8d7454d01cb31c1` adds durable allocation
protection, preserves unknown calls and fixture exclusions, distinguishes ambiguous oracle
execution from task failure, and repairs the future v2 long-document contract. Its queue
watchdog also terminates Windows launcher descendants. These benchmark controls
are validated by the offline suite and focused regressions described in
[BENCHMARK_EXPANSION_RESULTS.md](BENCHMARK_EXPANSION_RESULTS.md). Experimental execution status
is maintained there; infrastructure completion does not imply completion of all experiment stages.

## Claims allowed now

The current evidence supports these bounded statements:

- The checked-in offline fixture artifact is checksum-bound and exposes its metric boundaries.
- Deterministic local evaluators and focused tests exercise temporal/scope recall, mutable-fact
  resolution, grounded abstention, code evidence, context packing, source imports, sync repair,
  MCP contracts, graph contracts, and release invariants.
- The implementation-authored coding corpus can verify disposable repository oracles and reader
  plumbing while preserving development, validation, and held-out family reservations.
- Full LoCoMo and LongMemEval retrieval runs separate retrieved evidence from what fits into the
  reader context. The completed LoCoMo k=20 comparison reports a measured opt-in tradeoff.
- The additional upstream adapters have source-pinned retrieval-only boundaries. The Mem2ActBench
  small-slice diagnostic and two MemoryAgentBench splits are complete; remaining local runs are queued and upstream scores remain absent.

The following remain open: equal-coverage coding-agent outcome results, independent human-authored
held-out tasks, competitor/model comparisons, 100k memories with multiple concurrent agents,
browser and hosted operational SLOs, hardware durability, official external answer/action scores,
and separately authorized model evaluation. [`docs/RELIABILITY_PROGRAM.md`](RELIABILITY_PROGRAM.md)
and [`docs/PAID_EVALUATION_PROPOSAL.md`](PAID_EVALUATION_PROPOSAL.md) retain the required gates,
cost proposal, and authority boundaries.

## Next evidence package

Before publishing a new number, freeze the exact source, corpus and authorship statement, reader
and model revisions, adapter/parser, token counter, prompts, budgets, baseline and competitor arms,
execution count, uncertainty method, and exclusions. Run the local protocol first, then obtain the
cost proposal and approvals for any provider-dependent arm. Publish only a redacted envelope with
checksums and the honest result status. Do not promote unit-test counts, screenshot values, replay
volume, index-only throughput, or structural capacity matrices into user-outcome claims.
