# Deploy — launch-hardened build

This build closes the deploy-topology gaps that made the paid/team system unsellable:
cloud license **revocation** and the Pro **sync relay** are now mounted on the *shipped*
entrypoints (both `engraphis-server` and `engraphis-dashboard`), team mode ships by
default in Compose, and all license/trial/revocation state persists on the data volume.

## Which binary runs what

| Binary | App | Serves |
|---|---|---|
| `engraphis-dashboard` | `engraphis.dashboard_app` | **Team**: per-user auth, roles, seats, audit log, analytics, **+ `/license/v1/*` (register/verify/revoke) + `/relay/v1` sync** |
| `engraphis-server` | `engraphis.app` | Single-user v1 REST API + the same `/license/v1/*` and `/relay/v1` endpoints |

`docker compose up` runs the **dashboard** by default (team-ready). The raw API server is
under a profile: `docker compose --profile api up engraphis-api`.

## Pre-deploy

1. **Freeze the fleet** (autonomous jobs commit concurrently) — take the `_cowork_ops` lock.
2. **Run the offline gate** (mirrors CI): `pytest tests/ -q` + `eval.harness` (sample, codemem, k=5) + `eval.ablation`.
3. **⚠ Verify the signing seed matches the pinned pubkey.** If they don't match, *every key you issue fails to verify on clients.* On the vendor host:
   ```bash
   python -m scripts.license_admin verify-key
   # or:
   python -c "from engraphis.licensing import ed25519_public_key; \
     print(ed25519_public_key(bytes.fromhex(open('.secrets/vendor_signing.key').read().strip())).hex())"
   ```
   The printed hex MUST equal `_VENDOR_PUBKEY_HEX` in `engraphis/licensing.py` (`0f9ede88…6421d`).

## Server / vendor env (Railway, Fly, etc.)

| Variable | Purpose |
|---|---|
| `ENGRAPHIS_VENDOR_SIGNING_KEY` | Vendor Ed25519 seed (64-hex **or** file path) — signs keys *and* leases. Must match the pinned pubkey (see step 3). |
| `ENGRAPHIS_STATE_DIR` | License/trial/machine-id/lease state dir. Docker default `/data/.engraphis` (on the volume). |
| `ENGRAPHIS_DB_KEY` / `_KEY_FILE` | **Optional encryption at rest** (SQLCipher/AES-256) for the memory DB. Install `engraphis[encryption]`. 64-hex = raw key, else passphrase. Lose it = lose the data — inject from a secrets manager. |
| `ENGRAPHIS_RELAY_DB` | Registry + relay + registrations DB. **Persistent volume** or revoked keys un-revoke. Default `$ENGRAPHIS_STATE_DIR/relay.db`. |
| `ENGRAPHIS_API_TOKEN` | Bearer token authorizing the `/license/v1/revoke` endpoint. |
| `ENGRAPHIS_LEASE_TTL_HOURS` | Lease lifetime (default 72). Lower = faster revocation. |
| `POLAR_WEBHOOK_SECRET` | Polar webhook signing secret (`whsec_…`) for order.paid fulfillment. |
| `ENGRAPHIS_RESEND_API_KEY` (or `ENGRAPHIS_SMTP_*`) | License-delivery email. |
| `ENGRAPHIS_PRO_UPGRADE_URL`, `ENGRAPHIS_TEAM_UPGRADE_URL` | Checkout links behind 402/upgrade banners. |
| `ENGRAPHIS_FORWARDED_ALLOW_IPS` | Default `127.0.0.1` (trust nothing). **Behind a TLS proxy set this to the proxy IP/CIDR** (or `*` if reachable only via that proxy) so `request.url.scheme` is https and the session cookie's Secure flag is set. Don't use `*` on a directly-published port — clients could spoof `X-Forwarded-For`. |

## Persistent storage — REQUIRED (Railway / Fly volumes)

**Sync bundles, the license/revocation registry, and the memory DB all live under `/data`
(`ENGRAPHIS_DB_PATH`, `ENGRAPHIS_STATE_DIR`, `ENGRAPHIS_RELAY_DB`). If `/data` is not a
persistent volume, every redeploy wipes all synced data and un-revokes every revoked key.**

- **Railway:** attach a Volume to the service with mount path **`/data`** (Service → `+ Add`
  → Volume → mount path `/data`). One volume per service; it must be in the service's region.
- **Fly:** `fly volumes create engraphis_data --size 1` and mount it at `/data` in `fly.toml`.
- **Docker Compose:** already correct — the named volume `engraphis-data:/data`.

Managed hosts mount the volume **owned by root**, but the app runs as the non-root
`engraphis` user. `docker-entrypoint.sh` handles this: the container starts as root, chowns
`/data` to `engraphis`, then drops privileges via `gosu` before running the server — so a
freshly-attached volume is writable with no manual step. (Symptom if this is ever bypassed:
the container crashes at startup with `sqlite3.OperationalError: unable to open database
file`.)

## Client env (end-user machines)

| Variable | Purpose |
|---|---|
| `ENGRAPHIS_CLOUD_URL` | e.g. `https://<vendor-host>`. **Set = cloud enforcement on** (mandatory registration + revocable leases). Unset = offline/self-hosted (signature-only). |

## Operate

Revoke a compromised/refunded key (takes effect at next lease renewal, ≤ TTL):
```bash
curl -X POST https://<vendor-host>/license/v1/revoke/<key_id> \
  -H "Authorization: Bearer $ENGRAPHIS_API_TOKEN"
```
Check a key's status (public): `GET /license/v1/verify/<key_id>`.
Smoke-test the mount after deploy: `GET /license/v1/verify/anything` should return JSON
`{"known": false, ...}` (a 404 means the endpoints aren't mounted — a launch blocker).

## Enforcement model

- **Sync & Team — server-gated (~unbypassable).** Shared state lives on the relay; a client
  can't reproduce it. Registration is mandatory, seats capped server-side, keys revocable,
  team gate lease-backed in cloud mode.
- **Analytics / export / automation — local, best-effort.** Enforced by the cloud lease
  (stops casual + shared-key + no-code bypass; revocation works); a determined user can
  still patch the open-source client. Not moved server-side by design (local-first; no
  custody of customer memory data).
- **Free solo core** stays free.
