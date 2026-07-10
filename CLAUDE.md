# CLAUDE.md

The full operating manual for this repo lives in **@AGENTS.md** — read it first. It is the
canonical, vendor-neutral source (architecture, commands, conventions, algorithms, gotchas);
everything there applies to Claude Code. This file adds only Claude-specific guidance, so the
two never drift.

## The one rule that prevents most mistakes

Two codebases share `engraphis/`: **v2** (`core/` + `backends/` — the target) and the **v1**
legacy FastAPI server (`app.py`, `routes/`, `stores/`, `engines/`, flat namespaces). Build new
capability on v2 behind the interfaces in `core/interfaces.py`. Decide which side a change
belongs to before editing. Full table: AGENTS.md §0.

## Before you say "done" — run the offline gate

No network or API key required; this mirrors `.github/workflows/ci.yml` and must stay green:

```bash
python -m pytest tests/ -q && \
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5 && \
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5 && \
python -m eval.ablation
```

If you changed retrieval, scoring, or ranking, add or update an eval — per AGENTS.md §3.7,
"better" needs a number, not an assertion.

## Slash commands available here

- `/init` — regenerate codebase documentation.
- `/review` — review a GitHub pull request (`/code-review` for the local working diff).
- `/security-review` — review pending changes for vulnerabilities. **Run this before finishing
  any change to the write/ingest path:** ingested content is treated as untrusted and memory
  poisoning is an explicit threat. This now also covers
  `MemoryEngine.index_repo()` (reads local files at an agent-supplied path — see
  `SECURITY.md` §5) and the deterministic conflict resolver (`core/resolve.py`).

## Working style in this repo

- **Interface-first & dependency-light** (AGENTS.md §3): keep `core/` runnable on `numpy`
  alone; gate heavy imports behind the backend factories; never import a concrete backend
  inside `core/`.
- **House style:** `ruff` line-length 100, Python 3.9-compatible syntax, pure/tested scoring
  functions, provenance and scope on every memory.
- **Be concise and direct** in chat — explain the *why* of a change briefly, link the file,
  and let the diff speak.
- **When code and docs disagree, the code wins** — then fix the doc in the same change, and
  update `AGENTS.md`/`CLAUDE.md` if a convention or command changed.


## Token & turn discipline (added 2026-07-10, trimmed from FTSO)

- **Set worker models explicitly.** Subagents inherit the parent (premium) model by default.
  Mechanical work — search fan-out, digests, test sweeps, triage — goes to `haiku`/`sonnet`
  workers; keep the premium model for judgment, architecture, review, and synthesis.
- **Pre-digest large outputs.** Don't read raw diffs/logs/reports over ~200 lines directly;
  have a cheap lane produce a bounded digest plus flagged hunks, then inspect only what matters.
- **Dense turns.** While background lanes or long commands run, do useful inline work
  (review, docs, planning) instead of ending the turn to wait.
- **Report-only stops are a bug.** Don't end a working turn with "next I will…" while
  non-blocked work remains — make the first tool call of that next step instead. Valid turn
  ends: a real blocker with one specific question, a user taste/priority fork, a
  safety/permission gate, a background task that will wake the thread, or verified completion.
- **Safety gates always win** (unchanged): the offline gate, `/security-review` on write/ingest
  paths, and eval-backed claims are never skipped to "keep moving".

## Memory typing for recurring events

When writing to Engraphis memory: recurring operational events (ticks, no-ops, health checks)
are **always `episodic`** via `engraphis_record_event` with a stable `kind` — never `working`,
never `semantic` at write time; promotion is `engraphis_consolidate`'s job. Full deterministic
decision test: `skills/engraphis-memory/references/CONVENTIONS.md` §Recurring operational events.
