# Deploy — verified licensing build

**Deployable commit:** `b91bd7d` (fast-forward on `origin/main` `324ba42`).

Verified before writing this: full `pytest` suite green (2 skips), eval harness
`sample`/`codemem` = 1.000, ablation all 1.0, Python-3.9-clean imports. Security invariants
on `HEAD`: no `ENGRAPHIS_DEV_MODE` pubkey bypass, rotated vendor key `0f9ede88…` pinned,
`ENGRAPHIS_LICENSE_PUBKEY` override is pytest-only, all four enforcement modules present
(`sync_relay`, `license_cloud`, `license_registry`, `cloud_license`).

## Pre-deploy (do this first)

1. **Freeze the fleet.** The autonomous jobs commit to this repo concurrently; pause them
   (or take the `_cowork_ops` lock) so nothing lands between verification and build.
2. **Clean checkout of the exact commit:**
   ```bash
   cd C:\Users\jomie\Documents\Github\engraphis
   git fetch origin && git checkout b91bd7d
   git status   # expect clean (ignore untracked scripts/pxpipe-trial and .fuse_hidden*)
   ```
3. **Re-run the offline gate** (mirrors CI):
   ```bash
   cd C:\Users\jomie\Documents\Github\engraphis; .venv\Scripts\python.exe -m pytest tests/ -q
   cd C:\Users\jomie\Documents\Github\engraphis; .venv\Scripts\python.exe -m eval.harness --dataset eval/datasets/sample.jsonl --k 5
   cd C:\Users\jomie\Documents\Github\engraphis; .venv\Scripts\python.exe -m eval.harness --dataset eval/datasets/codemem.jsonl --k 5
   cd C:\Users\jomie\Documents\Github\engraphis; .venv\Scripts\python.exe -m eval.ablation
   ```

## Server env (Railway)

| Variable | Purpose |
|---|---|
| `ENGRAPHIS_VENDOR_SIGNING_KEY` | Vendor Ed25519 seed (64-char hex **or** file path) — signs license keys *and* leases. Keep secret. |
| `ENGRAPHIS_RELAY_DB` | SQLite path for the registry + relay bundles + registrations (persistent volume). |
| `ENGRAPHIS_API_TOKEN` | Bearer token that authorizes the revoke endpoint. |
| `ENGRAPHIS_LEASE_TTL_HOURS` | Optional; lease lifetime (default 72). Lower = faster revocation, more phone-home. |
| `POLAR_WEBHOOK_SECRET`, `ENGRAPHIS_RESEND_API_KEY` | Existing fulfillment (Polar → key → email). |

## Client env (end-user machines)

| Variable | Purpose |
|---|---|
| `ENGRAPHIS_CLOUD_URL` | e.g. `https://<your-railway-host>`. **Set = cloud enforcement on.** Unset = offline/self-hosted (signature-only). |

## Deploy

```bash
cd C:\Users\jomie\Documents\Github\engraphis; git push origin b91bd7d:main
```
Railway auto-builds from `main` (or `railway up` if deploying by CLI).

## Operate

Revoke a compromised/refunded key (takes effect at the next lease renewal, ≤ TTL):
```bash
curl -X POST https://<your-railway-host>/license/v1/revoke/<key_id> \
  -H "Authorization: Bearer $ENGRAPHIS_API_TOKEN"
```
Check a key's status (public): `GET /license/v1/verify/<key_id>`.

## Enforcement model (what's actually protected)

- **Sync & Team — server-gated (~unbypassable).** Shared multi-user/multi-device state
  lives on the relay; a client can't reproduce it. Registration is mandatory, seats are
  capped server-side, keys are revocable, and the team gate is lease-backed in cloud mode.
- **Analytics / export / automation — local, best-effort.** Enforced by the cloud lease
  (stops casual + shared-key + no-code bypass; revocation works), but a determined user can
  patch the open-source client. Not moved server-side by design (keeps local-first; no
  custody of customer memory data).
- **Free solo core** stays free — the open-source floor, intentionally not locked.
