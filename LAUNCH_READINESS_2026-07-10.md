# Engraphis — Launch Readiness (2026-07-10)

One unified product WebUI on **:8700**. The standalone Inspector (**:8710**) is retired, its
best features merged in, the Pro tier is now genuinely worth buying, and the pricing/marketing
is consistent. Backend verified by the test suite; frontend JS syntax-checked.

## What changed

### 1. 8710 Inspector → merged into the 8700 dashboard, then retired
- **Rich analytics** now served on the dashboard (`/api/analytics`) — growth, retention
  histogram, decay forecast, resolver mix, top entities — plus a **shareable HTML report**
  (`/api/analytics/export`) and the cross-workspace **portfolio** view. New **Analytics** tab.
- **Version-chain word diffs** now render in the memory-detail modal (added/removed words
  highlighted) with the audit trail — the Inspector's best feature, in the nicer shell.
- **Offline knowledge graph**: d3, force-graph, marked and DOMPurify are now vendored under
  `engraphis/static/vendor/` — the dashboard renders fully offline (no CDN).
- **`/api/ready`** readiness probe added.
- Inspector removed from `ecosystem.config.js`; `scripts/inspector.py` is now a redirect shim;
  `engraphis/inspector/{app,auth,webhooks}.py` stay as internal libraries (used by the
  dashboard, billing, and tests). Original launcher archived under `_archive/`.

### 2. Pro tier — now actually compelling
- **3-day free trial, in-app, one click, no card** (`POST /api/license/trial`) — unlocks every
  Pro feature locally. The "free trial" claim is now real (was previously only marketing copy).
- **Analytics** (deep, not the old thin summary) + **shareable HTML report**.
- **Automated maintenance** (NEW, Pro): scheduled consolidation + retention thresholds, run
  from the dashboard or on a schedule via `python -m scripts.auto_maintain --apply`.
- **Signed compliance export** (NEW): `/api/export?signed=1` wraps the dump in a SHA-256 +
  version + record-count manifest — an audit-grade, tamper-evident bundle (was a bare `SELECT *`).
- Feature flags: `pro = {analytics, export, automation}`, `team = pro + {team}`.

### 3. Pricing + packaging fixed (Pro $10/mo, Team $20/seat/mo)
- Corrected everywhere: `engraphis.com/product.html`, `index.html`, `README.md`, in-app license panel.
- Pro and Team now have **separate, env-overridable checkout URLs**
  (`ENGRAPHIS_PRO_UPGRADE_URL` / `ENGRAPHIS_TEAM_UPGRADE_URL`).
- Webhook hardened: an unrecognized **paid** product now defaults to **Pro** (never a useless
  free key); key validity matches the billing period (monthly grace / annual detection).
- Removed inaccurate/ vaporware copy (Inspector-as-Pro, "team sync", SSO/RBAC/SLA, BYOC).

## Verification (in an isolated Python 3.10 venv)
- **108 tests passing, 0 failures**: `test_licensing` (31), `test_dashboard_v2` (11),
  `test_analytics`, `test_inspector`, `test_inspector_pro`, `test_v1_licensing`, `test_billing`
  + `test_webhooks` + `test_webhook_e2e` (24), and a new `test_launch_smoke` (4) exercising the
  trial → rich analytics → HTML report → automation → signed export → ready flow end-to-end.
- New dashboard JavaScript passes `node --check` (no syntax errors).
- `ecosystem.config.js` parses (`node -e require`) and `ecosystem.config.known-good.js` refreshed.

> Note: the sandbox's network mount intermittently served stale/truncated reads of the most-edited
> files (a known quirk documented in `_cowork_ops/OPS_CONTRACT.md`). Tests were therefore run
> against a verified off-mount snapshot. The authoritative on-disk files are correct.

## Before selling — status & what to confirm

1. **Vendor signing key — ALREADY ROTATED (verified 2026-07-10). No action needed.** The seed in
   `.secrets/vendor_signing.key` derives to the pinned production key
   (`d3520482…7719e08`); the old compromised dev key (`4722dc14…1c862b7e`) is **not** in use, so
   `is_default_vendor_key()` is False and `production_warnings()` stays clean. Keep the seed only on
   this machine (it's gitignored, never commit it). Rotating again is only needed if that seed is
   ever exposed — and doing so invalidates every already-issued key. (An earlier draft of this doc
   wrongly said the dev key was still shipping; that was a mis-read, now corrected.)
2. **Restart pm2** to drop the retired `engraphis-inspector` process and load the new dashboard
   code: `pm2 reload ecosystem.config.js` (or `pm2 delete engraphis-inspector` then reload).
3. **Confirm Polar env** (lives in `~/.env` + the Polar dashboard — not visible from the sandbox):
   `POLAR_WEBHOOK_SECRET` set, email configured (`ENGRAPHIS_RESEND_API_KEY` or `ENGRAPHIS_SMTP_*`),
   and — for correct Pro-vs-Team routing — two Polar products whose names contain "Pro"/"Team" with
   `ENGRAPHIS_PRO_UPGRADE_URL` / `ENGRAPHIS_TEAM_UPGRADE_URL` pointing at their checkout links.
   Quick end-to-end self-test (signing already works): `python -m scripts.license_admin issue
   --email you@test.co --plan pro --days 1` then `python -m scripts.license_admin verify <key>`
   should print `plan: pro`.
4. **Commit** the working-tree changes from this Windows machine (not committed from the sandbox,
   to avoid committing a truncated mount view). Suggested: run `pytest` locally first as a final gate.
5. *(Optional)* schedule `python -m scripts.auto_maintain --apply` (Task Scheduler / cron) so Pro
   automation runs without the dashboard open.
