# Handoff — Engraphis, 2026-07-01

You're picking up a local-first AI memory engine for agents (self-hosted alternative to
mem0/Zep/Letta). This doc is written to be read on its own — it orients you, tells you exactly
what's verified vs. assumed, and hands you a prioritized punch list. Read `AGENTS.md` next for
architecture; this doc is about *state and next steps*, not the system design.

## 0. Orient yourself in 60 seconds

Two codebases live in one repo. **v2** (`engraphis/core/`, `engraphis/backends/`) is the target —
scoped, bi-temporal, interface-driven, numpy-only core. **v1** (`engraphis/app.py`, `routes/`,
`stores/`, `engines/`, `static/`) is a legacy FastAPI server + dashboard, kept running as a
compatibility surface. Build new capability on v2. `AGENTS.md` §0 has the full table — read it
before touching anything; confusing the two sides is the single most common mistake here.

The headline feature is the MCP server (`engraphis/mcp_server.py`) — 15 tools, backed by
`MemoryService` (`engraphis/service.py`), backed by `MemoryEngine` (`engraphis/core/engine.py`).
That three-layer stack is solid and well-tested. The v1 REST server + dashboard is older,
flat-namespace code that got two real bug fixes this pass but is not where you should build new
capability.

## 1. What's actually verified right now (I re-checked all of it, not just re-asserted it)

- `pytest tests/ -q` → **127 passed, 0 failed, 0 skipped, 0 errors**, but only when `server` +
  `mcp` extras are installed locally (this sandbox has fastapi 0.138.2, mcp 1.28.1, tree-sitter
  0.26.0). **GitHub Actions does not install these** — see §3.2. Don't quote "127, 0 skipped" as
  a CI fact; it's a fully-installed-local fact.
- `ruff check .` → clean, but **ruff is not part of `.github/workflows/ci.yml`** — see §3.2. It's
  a local-only check today despite being described as part of "the gate that mirrors CI."
- `eval.harness` on both datasets → 1.000/1.000/1.000, `eval.ablation` → 1.0/1.0. These are real
  numbers against real code paths (the harness was fixed this pass to exercise the actual
  `MemoryEngine` pipeline, not just the bare vector index) — but the fixture sets are tiny
  (4 + 22 = 26 hand-authored questions total). Perfect scores on a 26-question fixture you wrote
  yourself is a sanity check, not a benchmark. LoCoMo/LongMemEval against the real embedder is
  still not done — this is the single biggest credibility gap before you publish any quality claim.
- The two bugs this pass fixed are verified two ways each: unit tests (`test_ingest_entities.py`,
  a Node.js DOMPurify repro) *and* a fresh, independent live repro I ran during this check (ingest
  two docs with an entity spanning a title/content boundary → one clean node, both doc ids; render
  a fresh XSS payload through the actual shipped `renderMd()` → attribute stripped, markdown
  intact). Both hold.
- `_check_owns()` in `service.py` is real and wired into all four governance methods
  (forget/pin/correct/link) — confirmed by reading the code, not just the changelog entry.
- All 15 `engraphis_*` functions in `mcp_server.py` carry `@mcp.tool` decorators 1:1 — confirmed
  by grep, not by trusting the docstring. (This matters because the docs said 13 — see §3.1.)

## 2. What's NOT verified / still aspirational

- **No real benchmark.** LoCoMo/LongMemEval numbers against named competitors: not run.
- **No shipped Pro feature.** The $20/mo/seat target price is anchored against real, freshly
  re-verified competitor pricing (mem0 $19/$79/$249 confirmed accurate; Letta $20 Pro confirmed
  accurate; Zep — see §3.3, the docs had this wrong) but nothing in the product is a paid-only
  capability yet. `docs/GO_TO_MARKET.md` §10 is explicit about this — read it before quoting a
  price to anyone.
- **Cross-tenant read isolation is not enforced.** See §4, this is the top security item.
- **v1's REST input validation** has no size caps or control-char stripping (unlike v2's
  `service.py`). Documented in `SECURITY.md`, not fixed.
- **Nothing is committed.** See §3.4.

## 3. Landmines found this pass — read before you trust anything you `cat`

### 3.1 Docs had two confidently-wrong numbers. Verify claims against code, not against other docs.
The changelog, README, AGENTS.md, and RELEASE_READINESS.md all said "13 tools" — the actual count
(grep `@mcp.tool` in `mcp_server.py`) is **15**. All five files have been corrected. The lesson
isn't "it's fixed now" — it's that a wrong number repeated across five files still looks
unanimous. When a number matters (tool counts, test counts, prices), check it against the
primitive source (code, or a direct fetch of the vendor's page), not against how many of your own
docs agree with each other.

### 3.2 CI is narrower than the docs imply.
`.github/workflows/ci.yml` installs only `numpy pytest` and runs pytest + both eval datasets +
ablation. It does **not** install `ruff`, and does **not** install `server`/`mcp`/`code` extras.
Practically: a lint violation will not fail CI, and `test_app_auth.py`/`test_mcp_server.py` will
always show as *skipped* (not run) on every real GitHub Actions run, only executing when someone
runs the full local install. If you want ruff and the extras-gated tests actually enforced,
update the workflow — right now "mirrors CI" in `AGENTS.md`/`CLAUDE.md` is aspirational for those
two pieces.

### 3.3 Zep's pricing in the docs was wrong by ~5x — a real research error, not a typo.
The docs said Flex = $25/mo, Flex Plus = $75/mo, free tier = 1k credits. A direct fetch of
`getzep.com/pricing` during this check shows the real numbers: **Flex = $125/mo (50k
credits/mo)**, **Flex Plus = $375/mo (200k credits/mo)**, free tier = **10k credits/mo**. The
$25/$75 figures are Zep's *per-block overage rate* once you exceed your monthly credits, not the
base plan price — an easy trap in credit-metered pricing pages, and the kind of error that
doesn't announce itself (the wrong numbers are plausible-looking and internally consistent).
Corrected in `docs/GO_TO_MARKET.md` and `RELEASE_READINESS.md`, with the Sources section explaining
the correction. Net effect on the $20/mo target: it now looks *more* conservative, not less — Zep's
real entry price is ~6x Engraphis's target — but re-verify all three vendors again before you
publish anything with a date on it; pricing pages move.

### 3.4 File-sync staleness between the shell and the canonical file view.
This repo sits on a synced/mounted drive. Repeatedly this session, a file I'd just edited would
read back truncated or stale when catted/grepped from the shell — sometimes mid-sentence, missing
the back half of the file — while the canonical editor view was correct. It hit `static/index.html`,
`CHANGELOG.md`, `SECURITY.md`, `RELEASE_READINESS.md`, `docs/GO_TO_MARKET.md`, and `AGENTS.md` at
various points, sometimes minutes after an edit that looked fine at the time. Symptoms: a shell
command reads back fewer lines than you just wrote, a test import raises `AttributeError` for a
method you can see in the file, `wc -l` disagrees with what you just wrote. Clearing `__pycache__`
does **not** fix it. The reliable fix: rewrite the whole file from the shell itself (a heredoc)
using content you've freshly confirmed correct, then re-verify with `wc -l` + `tail` in the same
shell call before trusting anything downstream (a test run, a syntax check) against it. This is
now documented in `AGENTS.md` §7 — if you hit unexplained staleness, that's the section to check,
and this is the fix to reach for before assuming your code is broken.

### 3.5 `.gitignore` didn't cover the current default database filename.
It ignored the literal `neocortex.db` (the old name) but had no glob for `*.db`, so the
rebrand's new default (`engraphis.db`) would have been tracked by git the first time anyone ran
`git add -A` after running the server locally — directly contradicting `SECURITY.md`'s claim that
`*.db` is git-ignored. Fixed by adding `*.db`/`*.db-wal`/`*.db-shm` globs. Worth a moment's
paranoia any time a project renames its data file: grep the actual `.gitignore`, don't trust the
security doc's description of it.

## 4. Priority-ordered next steps

1. **Commit and push.** `git log` shows exactly one commit ("Initial commit"). Everything since —
   the entire competitive-feature pass, the dashboard fixes, this cleanup — is uncommitted in the
   working tree (`git status` showed ~36 modified + ~20 new files as of 2026-07-01). A real remote
   is configured (`origin` → `github.com/Coding-Dev-Tools/engraphis`) and CI is wired to
   `push`/`pull_request` on `main`, but none of it has ever actually run against this code because
   none of it has been pushed. Commit in logical chunks (rebrand/security pass, competitive-feature
   pass, dashboard-fix pass are natural boundaries — `CHANGELOG.md` is already organized this way)
   rather than one giant commit, then push and watch CI actually run for the first time.
2. **Close the cross-tenant read gap.** `MASTER_PLAN.md` §16 states plainly that `workspace` must
   be "the hard isolation boundary" with scope "enforced server-side on every read/write — never
   trust client-supplied scope alone." This pass added `_check_owns()` so a caller can't *mutate*
   (forget/pin/correct/link) a memory in a workspace it doesn't own by reusing a leaked id — but
   `recall`/`why`/`timeline`/`recall_proactive` still take the caller's asserted `workspace` at
   face value for *reads*. Any MCP client that can guess or already knows a workspace name can
   read it. `SECURITY.md` §3 discloses this honestly; it's still open. This is the highest-value
   security fix available and it's scoped clearly enough to start immediately.
3. **Get CI parity right** (§3.2): either add `ruff check .` and a `pip install -e ".[all]"` step
   to `ci.yml` so the full 127-test suite and lint actually run on every PR, or stop describing the
   local gate as "mirroring CI" until it does. Cheap, and removes a real trust gap between what the
   docs claim and what GitHub actually enforces.
4. **Pick one Pro feature and build it for real**, per `docs/GO_TO_MARKET.md` §10's own
   recommendation: host the now-fixed v1 dashboard, add basic multi-user login, sell that. Don't
   publish the $20/mo price until something on the Pro list exists outside the free core.
5. **Run a real benchmark.** LoCoMo or LongMemEval with the sentence-transformers embedder
   (`pip install -e ".[all]"`), published with numbers, not vibes. This is what actually
   substantiates "value comparable to leading competitors" instead of asserting it.
6. Lower priority, already scoped in `RELEASE_READINESS.md`'s "Before you charge money" list:
   trademark clearance, encryption-at-rest, rate limiting, a Python 3.9/3.11 CI matrix + PyPI
   wheel, and v1's missing input-size caps.

## 5. Before you believe any status claim (including this one)

Don't take "127 tests pass" or "the fix works" as settled just because a doc says so — that's
exactly the failure mode this handoff exists to break. Re-run the gate yourself:

```bash
python -m pytest tests/ -q
python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5
python -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5
python -m eval.ablation
ruff check .
```

And when you fix something non-trivial, don't stop at the unit test you wrote for it — reproduce
the original bug independently (a small standalone script, a fresh payload) and confirm the fix
holds outside the test you wrote to prove it. That's what caught both real bugs this pass, and
it's what caught the tool-count and Zep-pricing errors during this cleanup — the unit tests were
never wrong about what they tested, the *docs* were wrong about facts nothing was testing.

## 6. Where things are

- `AGENTS.md` — architecture, conventions, commands, the two-codebase split. Read this first.
- `MASTER_PLAN.md` — the original build spec; §16 (security/multi-tenancy) and §18 (roadmap) are
  the sections most worth re-reading now.
- `SECURITY.md` — threat model and honest disclosure of what's not yet mitigated.
- `CHANGELOG.md` — what shipped, organized by pass, with root causes for both bug fixes.
- `RELEASE_READINESS.md` — quality gate status and the prioritized pre-monetization list.
- `docs/GO_TO_MARKET.md` — positioning, pricing, and the corrected competitor numbers.
