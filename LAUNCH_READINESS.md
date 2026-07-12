# Launch readiness (living doc)

Supersedes the dated 2026-07-10 snapshot, which pointed at the retired `d3520482…`
signing key and is no longer accurate.

## Closed (this launch-hardening pass)
- **Revocation + Pro sync are served in production.** `/license/v1/*` and `/relay/v1` are
  mounted on both shipped entrypoints (`engraphis.app`, `engraphis.dashboard_app`) via
  `engraphis/inspector/cloud_mount.py`; regression-tested in `tests/test_cloud_endpoints_mounted.py`.
- **Team mode ships by default** — `docker compose up` runs `engraphis-dashboard`.
- **State persists across redeploys** — license/trial/machine-id/lease/registry live under
  `ENGRAPHIS_STATE_DIR=/data/.engraphis` (on the volume).
- **In-app trial no longer silently dies** on read-only/ephemeral homes — `machine_id()` is
  process-stable and logs (doesn't swallow) persistence failures.
- **Secure cookie behind a TLS proxy** — uvicorn started with `proxy_headers=True`.
- **Vendor key rotated**, `ENGRAPHIS_LICENSE_PUBKEY` forgery bypass closed, purchase URL is a
  real Polar checkout.

## Must verify on the live host before charging (cannot be checked from source)
1. **Signing seed ↔ pinned pubkey match** — see DEPLOY.md step 3. If wrong, every issued key
   fails to verify. **Highest-priority check.**
2. `POLAR_WEBHOOK_SECRET` set in Polar + the vendor host; product names map to plans.
3. `ENGRAPHIS_RELAY_DB` on a persistent volume (revocations must survive restart).
4. `ENGRAPHIS_CLOUD_URL` set on clients if you want revocation to actually bite.

## Known honest limits
- Analytics/export/automation are local best-effort (patchable open-source client); only
  sync/team execute server-side and are truly non-bypassable.
- Single-tenant per instance (run one per customer); not a shared multi-tenant SaaS.
