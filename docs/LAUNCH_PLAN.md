# Engraphis — Launch Plan (v1.0 release + first paid tier)

_Date: 2026-07-03. Companion to `RELEASE_READINESS.md` (state audit) and
`docs/GO_TO_MARKET.md` (pricing research). This document is the execution plan: what ships,
in what order, and how the paid tier works technically and commercially._

## 1. Where we are

The free core is launch-ready (127 tests, offline gate green, 17 MCP tools, hardened write
path). What was missing for revenue — and what this pass adds — is the commercial layer:
there was no license mechanism, no paid feature, and no upgrade path in the product. See §3.

## 2. Monetization architecture (decided 2026-07-03)

**Model: open-core with offline signed license keys.** The core stays Apache-2.0 and fully
functional. Pro features ship in this repo but activate only with a valid key. Keys are
Ed25519-signed JSON payloads verified **offline** — no phone-home, no license server, which
keeps the local-first promise intact (a memory engine that phones home would undercut the
entire pitch).

- Key format: `ENGR1.<base64url payload>.<base64url signature>`; payload carries plan,
  features, seats, expiry. Verified against the vendor public key pinned in
  `engraphis/licensing.py`. Verification is pure stdlib (RFC 8032 Ed25519), so the
  numpy-only core guarantee holds.
- Issue keys with `python -m scripts.license_admin issue --email … --plan team`. The signing
  key lives in `.secrets/` (gitignored). **Rotate the committed dev public key before selling
  a single license** (`license_admin keygen`), and keep the production private key in a
  password manager, never on a dev box.
- Honesty note (Apache-2.0): a determined user can fork out the gate. That is the accepted
  trade of the Sidekiq-style model — you sell convenience, updates, and support to the honest
  majority. Do not escalate to obfuscation; it poisons trust and never works anyway.

### Tiers

| Tier | Price (target) | What's in it |
|------|----------------|--------------|
| Free | $0 forever | Whole engine: MCP server, recall/why/timeline, governance, code graph, single-user Inspector |
| Pro | $20/mo or $200/yr | Analytics dashboard, compliance export (full bi-temporal JSON dump), priority support |
| Team | $35/user/mo | Pro + multi-user Inspector: logins, roles (admin/member/viewer), seat-limited keys |

$20 anchors against Letta Pro ($20) and mem0 Starter ($19); see GO_TO_MARKET.md §10. Team
pricing is per-seat because the seat count is in the signed key.

### Payments & fulfillment (not yet built — next step after this pass)

Use a merchant-of-record (Polar.sh or Lemon Squeezy — handles VAT/sales tax, ~5% fee) rather
than raw Stripe at this stage. Flow: checkout → webhook → `license_admin issue` → key
emailed. Automate with a ~50-line serverless function when volume justifies it; issue keys
manually for the first customers (it's also a customer-discovery channel). Add the purchase
URL in the Inspector's license dialog once live.

## 3. What ships in this pass (implemented)

1. **`engraphis/licensing.py`** — key parsing/verification, feature registry, cached
   `current_license()`; reads `ENGRAPHIS_LICENSE_KEY` or `~/.engraphis/license.key`.
2. **`scripts/license_admin.py`** — vendor CLI: `keygen`, `issue`, `verify`.
3. **Inspector license UX** — plan badge in the header, license dialog (activate key, see
   features), tasteful locked-tab teasers. Free tier is never nagged mid-workflow; upsell
   surfaces are opt-in clicks.
4. **Pro: Analytics tab** — memory growth, retention distribution, decay forecast (which
   memories fall below the archive threshold in 7/30 days), resolver action mix, top
   entities. Server-side in `engraphis/analytics.py` (numpy/stdlib only), inline-SVG charts
   client-side (no chart library, consistent with the zero-dependency house style).
5. **Pro: compliance export** — one-click full workspace dump (memories incl. superseded
   history, audit trail, sessions) as attachment-download JSON.
6. **Team: multi-user Inspector** — `ENGRAPHIS_TEAM_MODE=1` + a `team` key enables login
   (PBKDF2, HttpOnly session cookie), first-run admin setup, roles enforced server-side
   (viewer=read, member=+governance, admin=+consolidation/users/export), Team tab for user
   management. Without team mode nothing changes for existing single-user setups.
7. **Tests** for all of the above, following the repo's `importorskip` CI-gate convention.

## 4. UI/UX improvements — beyond this pass

Ordered by effort-to-impact; the Inspector is already accessible (ARIA tabs, keyboard nav,
dark/light) so this is polish, not rescue:

1. **First-run experience**: when no workspaces exist, show a guided empty state with the
   exact MCP config snippet to paste into Claude Code/Cursor (copy button), instead of a
   toast. Biggest funnel fix available — the current first screen is blank.
2. **Onboarding command**: `engraphis init` that writes `.env`, picks a DB path, and prints
   the MCP snippet. Closes the "install → configured agent" gap in one step.
3. Global search-as-you-type across tabs (debounced recall), keyboard palette (`/` to focus
   search), relative timestamps ("3d ago") with exact time on hover.
4. Graph visualization of entity/link neighborhoods in the detail dialog (SVG, force layout
   is overkill — radial layout is fine at this scale). Candidate second Pro feature.
5. Retire the v1 dashboard from the default install (`engraphis-server` keeps serving it for
   compat, but docs point at the Inspector only) — one product surface, one story.

## 5. Feature roadmap (next / later)

**Next (pre-1.0):** encryption-at-rest for the SQLite file (SQLCipher optional extra →
`encryption` feature flag, regulated-ICP requirement per RELEASE_READINESS.md §"Before you
charge" #3); consolidation policies (saved schedules with per-scope thresholds) as a Pro
feature; publish LoCoMo/LongMemEval numbers (the eval adapter already exists).

**Later:** SSO/OIDC for Team (gate: first team customer asking); hosted Inspector (gate:
recurring demand — it abandons local-first, so it must be pull, not push); scale backends
(Qdrant/pgvector already behind interfaces); per-token tenant authorization for
multi-tenant hosting.

## 6. Launch checklist (ordered)

1. ~~License mechanism + first three paid features~~ (this pass).
2. Rotate vendor keypair; store private key offline. Set the real purchase URL in the
   Inspector dialog and `licensing.py`.
3. Set up Polar/Lemon Squeezy product + webhook → key issuance.
4. `git commit` (the tree at HEAD must be the audited one), tag `v0.2.0`, push, verify CI
   matrix (3.9/3.11), publish wheel to PyPI, smoke-test `pip install engraphis[all]` and the
   Docker image.
5. Run LoCoMo benchmark, publish numbers in README (honest recall@k, per
   RELEASE_READINESS.md #1).
6. Trademark search on "Engraphis" (#2 there) before spending on brand.
7. Launch free tier loudly (Show HN, MCP directories, r/LocalLLaMA — the local-first angle
   is the hook), sell quietly (license dialog + pricing page). Revisit pricing after ten
   real conversations.

## 7. Risks

- **Someone forks out the gate** — accepted (see §2); mitigation is velocity and support,
  not DRM.
- **Team mode expands the attack surface** — mitigated: PBKDF2-HMAC-SHA256 (600k iters),
  hashed session tokens, SameSite=Strict cookies, login backoff, server-side role checks on
  every route; see SECURITY.md §6. Still run `/security-review` on any change touching it.
- **Paid features drift into the free core's value story** — the line is: *the engine
  remembers for free; seeing, proving, and sharing what it remembers is paid.* Analytics,
  export, and team are all on the right side of that line. Keep it that way.
