# Commercial operations runbook (v1.0)

## Production topology

Production has three distinct trust roles. Do not collapse them into `combined` mode:

1. `license.engraphis.com` runs `ENGRAPHIS_SERVICE_MODE=vendor`. It owns license issuance,
   leases, deployment trials, Polar webhooks, transactional email, the authoritative license
   registry, and the private relay-token signing seed. It does not mount bundle routes.
2. The Engraphis-managed relay data plane runs `ENGRAPHIS_SERVICE_MODE=relay` as a
   dedicated deployment and volume. It receives only the relay-token public verifier, never
   either private signing seed, billing credentials, customer license keys, or a copy of the
   vendor registry. Its ingress must expose only the relay and health/readiness surfaces (plus
   the explicitly bounded compatibility routes until sunset), not the general dashboard or
   memory API.
3. Each ordinary Pro/Team customer deployment also runs `customer` mode, but authenticates
   sync with locally issued named-user tokens. It does not need the vendor relay-token keypair.

New signed keys use `https://license.engraphis.com`. The retired
`team.engraphis.com/license/v1/*` proxy exists only through **17 October 2026 00:00 UTC**.
Before that instant it forwards an explicit legacy route/method allowlist and strips
Authorization, cookies, customer credentials, forwarding headers, and vendor secrets; at and
after the deadline it returns HTTP 410 without contacting the control plane. Remove the proxy
routes in the next release after the deadline.
The pre-sunset allowlist is limited to `POST register`, `GET/HEAD verify/{key_id}`, `POST
team-invite`, `POST password-reset`, `POST start-trial`, and `GET/HEAD/POST
start-trial/verify`. It never proxies administrative, device-token, trial-claim, or arbitrary
future license routes.

Never place the Ed25519 signing seed, Polar webhook secret, vendor admin token, or
Engraphis Resend key on a customer service.

## Release readiness

`GET /api/ready` is the public serving gate used by the orchestrator. On an ordinary customer
service it checks the database/embedder path. On the vendor service it checks service mode,
the approved license signer, the separate relay-token issuer keypair and TTL, writable registry,
exact Polar webhook/organization/products and idempotency store, mail configuration and worker
liveness, and disk capacity. It deliberately does not include backup age, admin-monitoring
configuration, delivery backlogs, or externally triggerable alert counters: those conditions
require operator action, and restarting or draining an otherwise healthy first deployment
cannot repair them.

The dedicated managed relay has a separate, secret-free
`commercial.managed_relay_verifier_readiness()` contract. Its deployment probe must require
`service_mode`, `relay_token_verifier`, `relay_db`, and `disk` to be true. This is intentionally
not folded into ordinary customer readiness: provisioned customer dashboards use their own
named-user tokens and must not be forced to install a vendor verifier merely to stay healthy.

The full operational gates remain authenticated. `GET /api/ops/ready` on the customer
service requires an admin or operations bearer and returns boolean-only service-mode,
database-volume capacity, and backup-freshness checks. `GET /ops/ready` on the control plane
requires the vendor admin bearer and adds admin-token configuration, backup freshness,
delivery-webhook configuration, webhook/email backlog health, and the Polar processing-lease
check. A content-free rejected-lease count is reported for alerting but cannot make readiness
fail, because invalid public traffic must not let an attacker drain the service. None of these
endpoints returns secrets, customer addresses, license keys, storage paths, or provider
payloads.

Generate the vendor administrator credential independently of every customer/API token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`ENGRAPHIS_VENDOR_ADMIN_TOKEN` fails closed unless it contains 32-4096 non-whitespace
characters without control ASCII. Use the provider-generated Polar and Resend webhook
secrets; each verifier/readiness gate requires at least 16 bytes of raw key material (or
16 decoded bytes for an encoded `whsec_` value). Short placeholders are intentionally
rejected. The Ed25519 seed is exactly 32 bytes represented by 64 hex characters. When
`ENGRAPHIS_VENDOR_SIGNING_KEY` names a file, the resolved target must be a non-empty regular
file no larger than 1 KiB and, on POSIX, owner-only (`chmod 600`). Prefer
`scripts.license_admin keygen` over creating this file by hand.

The signer release flag deliberately remains false until the issued-key inventory and
offline rotation ceremony are complete. If no keys exist, rotate cleanly. If keys exist,
ship key IDs plus the dual verifier, reissue, retain the old verifier for 30 days, then
revoke and remove it. Keep the private seed only in the production secret store and an
encrypted recovery backup.

Run the PII-free inventory against the production registry before the ceremony:

```bash
python -m scripts.license_admin inventory --db-path <production-relay.db>
```

`rotation_requires_migration: true` means the reviewed release must pin the old verifier
alongside the new signing-key ID, reissue active keys, and keep the old verifier for 30 days.
New manual keys default to the signed `https://license.engraphis.com` control-plane URL and
are recorded in the registry as part of issuance. Inventory groups persisted
`signing_key_id` values; pre-migration rows are reported as `unknown` rather than assigned an
unverified signer. Cached leases accept only explicitly pinned current/previous keys and fail
closed as soon as their signer is removed from that set.

Do not generate or install the new signer until the inventory, an online SQLite backup of
the registry, and an encrypted recovery backup of the old seed are recorded. Generate into
a new offline path; do not overwrite the old seed:

```bash
python -m scripts.license_admin keygen \
  --key-file <secure-offline-path>/vendor_signing-YYYY-MM-DD.key
```

For a non-empty registry, deploy verifier compatibility before reissuing: pin the new public
key as `_VENDOR_PUBKEY_HEX`, retain the old public key in
`_PREVIOUS_VENDOR_PUBKEY_HEXES`, and leave `VENDOR_SIGNER_RELEASE_READY = False`. Create a
private source file containing one existing license key per line. The registry intentionally
does not retain raw keys; recover them only from protected fulfillment/customer records. The
command refuses a missing or extra fingerprint relative to inventory, preserves every signed
claim except `signing_key_id`, prints no customer address or license key, and leaves old keys
active:

```bash
# Preflight only: no registry or output mutation.
python -m scripts.license_admin rotation-reissue \
  --db-path <production-relay.db> \
  --source-file <protected-active-keys.txt> \
  --new-key-file <secure-offline-path>/vendor_signing-YYYY-MM-DD.key \
  --output-file <protected-replacements.json>

# After operator/reviewer approval, write the 0600 delivery manifest and registry audit.
python -m scripts.license_admin rotation-reissue \
  --db-path <production-relay.db> \
  --source-file <protected-active-keys.txt> \
  --new-key-file <secure-offline-path>/vendor_signing-YYYY-MM-DD.key \
  --output-file <protected-replacements.json> \
  --apply
```

The replacement manifest contains customer addresses and live license keys. Never commit,
email as an attachment, or log it; use it only through the approved delivery channel. If the
process stops after writing the manifest but before committing the registry, rerun the same
command with `--apply --resume`.

After every replacement is delivered and activated, preflight retirement. The apply command
is hard-gated by the registry audit's 30-day age and refuses to revoke a source without an
active replacement:

```bash
python -m scripts.license_admin rotation-retire \
  --db-path <production-relay.db> \
  --manifest-file <protected-replacements.json>

python -m scripts.license_admin rotation-retire \
  --db-path <production-relay.db> \
  --manifest-file <protected-replacements.json> \
  --confirm-activated --apply
```

Only then remove the old public key from `_PREVIOUS_VENDOR_PUBKEY_HEXES`, deploy, and rerun
readiness plus production synthetics. The retirement command never edits the verifier pin or
destroys either seed.

## Relay-token issuer, audience, and rotation

Relay-device credentials use a dedicated Ed25519 keypair; never reuse the license/lease
signer. Configure the control plane with all of:

- `ENGRAPHIS_RELAY_TOKEN_SIGNING_KEY`: the 32-byte private seed as 64 hex characters;
- `ENGRAPHIS_RELAY_TOKEN_PUBKEY`: its matching 32-byte public key as 64 hex characters;
- `ENGRAPHIS_RELAY_TOKEN_AUDIENCE`: the exact canonical HTTPS origin of the managed relay;
  and
- optional `ENGRAPHIS_RELAY_DEVICE_TOKEN_TTL_SECONDS`, from 300 through 3600 (default and
  hard maximum 3600).

Configure the separate managed-relay data plane with the same audience and current public
key, but **not** the signing seed. The audience is an origin only: no credentials, path, query,
or fragment. Default ports and host casing are canonicalized, then issuance and verification
must match exactly. This prevents a bearer minted for one relay from being replayed at another.
`vendor_serving_readiness()` fails until the issuer seed, public half, audience, previous-key
metadata, and TTL are valid. The managed relay must independently require
`managed_relay_verifier_readiness()["ready"]` before receiving traffic.

Rotation uses strict JSON in `ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS`:

```json
[
  {
    "public_key": "<retiring-64-hex-public-key>",
    "issued_before": 1785000000,
    "not_after": 1785003600
  }
]
```

Both timestamps are integer Unix epochs. `issued_before` is the cutover instant at which the
old issuer must already have stopped; old-key tokens issued at or after that instant are
rejected. `not_after` must be later and no more than 3600 seconds after the cutover. At most
three previous keys are accepted. Once verifier time reaches `not_after`, leaving that stale
entry configured is an error and readiness/verification fails closed. The retired unbounded
`ENGRAPHIS_RELAY_TOKEN_PREVIOUS_PUBKEYS` setting is rejected, not treated as a fallback.

Use this order so no valid token is stranded and no retired key can mint fresh credentials:

1. Generate the replacement seed offline and derive its public key. Choose cutover `T` no
   more than five minutes ahead and `N = T + 3600` (or the shorter active token TTL).
2. First deploy the managed relay with the replacement as the current public key and the old
   public key in `PREVIOUS_KEYS` with `issued_before=T` and `not_after=N`. Keep the audience
   unchanged. Require verifier readiness.
3. At `T`, atomically update the control plane's signing seed and current public key to the
   replacement. Give it the same bounded previous-key metadata and require issuer readiness
   before restoring traffic.
4. At `N`, atomically remove the previous-key entry from both deployments before restoring
   traffic, then rerun readiness. Never retain expired metadata “just in case”; stale metadata
   deliberately fails closed, and possession of a retired private seed must not remain useful.

The two services may share the public verifier and audience, but never a database or private
seed. HTTPS remains mandatory. Relay bundles are not end-to-end encrypted and the relay
operator can read their plaintext contents.

## Authoritative-registry migration window

`ENGRAPHIS_LEGACY_LICENSE_MIGRATION_UNTIL` is a one-time vendor-control-plane escape hatch for
signed keys sold before authoritative issuance rows existed. Set it only to an absolute Unix
timestamp in the future and no more than 30 days away. During that window, an otherwise valid
legacy key missing from the registry is atomically enrolled and audited. Unset, malformed,
expired, or overly distant values fail closed. Inventory and migrate the known legacy cohort,
monitor `legacy_license_migrated` events, then delete the variable; it is not a standing
compatibility or disaster-recovery mode.

## Billing

Production requires `POLAR_ORGANIZATION_ID`, `POLAR_WEBHOOK_SECRET`, and all four exact
product IDs from `engraphis/commercial_manifest.json`. Unknown products, wrong-organization
events, and malformed events are rejected. Durable event/order idempotency covers duplicate
delivery. Polar provides paid monthly/annual checkout only; the application owns the
three-day no-card trial.

Exercise every product and lifecycle in Polar test mode. A real Pro monthly purchase and a
real Team monthly purchase/refund require designated inboxes and execution-time approval.

## Transactional email

Trial, purchase, invitation, reset, and key-reissue messages enter the durable outbox.
`GET /ops/email` returns redacted state and provider-message fingerprints; failed operations
remain recoverable. An authenticated operator may retry exactly one selected failed row with
`POST /ops/email/retry?message_id=eml_...`; each row has a permanent two-requeue cap, so this
endpoint cannot amplify all failures into a bulk send. Verified Resend webhook events update
delivered, bounced, and complained states. Readiness fails on terminal delivery failures, an
old backlog, or a statistically meaningful bounce rate above the configured threshold.

After manually delivering or otherwise reconciling one terminal failed message, close it with
`POST /ops/email/resolve?message_id=eml_...&acknowledged=true`. This authenticated,
one-selected-ID action is irreversible: it marks only a `failed` row `resolved` and atomically
clears its recipient, subject, body, reply-to, retention claim, and last error. A paid-license
row cannot be resolved until its durable Polar fulfillment tombstone exists; its registry
recovery copy is then removed in the same relay-database transaction. The idempotency tombstone
and redacted audit metadata remain. Readiness stays red until retry succeeds or this explicit
operator close-out completes; there is no automatic purge of recoverable failures.

The live vendor registry/outbox database is ordinary SQLite and can temporarily contain
recipient PII and a signed license body while that message is pending, retryable, or retained
for recovery after final failure. Put the vendor volume on encrypted storage and restrict its
filesystem permissions. Once the provider accepts a message and the matching Polar
fulfillment claim is durable, Engraphis clears the body, reply-to value, and retention link;
startup recovery completes that cleanup after a crash. A provider-only outage leaves the key
solely in that durable outbox; it does not create a redundant plaintext fallback.
`undelivered_license_keys.tsv` is created only when durable enqueue itself fails. While that
fallback exists, authenticated vendor operations readiness reports `manual_fulfillment=false`
until an operator completes the documented reconcile/deliver/remove-or-encrypted-archive
procedure.

Customer deployments with no local provider relay password-reset requests server-to-server
to `POST /license/v1/password-reset` using their active Pro/Team key. The control plane checks
revocation, binds the reset link to the deployment-claim origin (trial) or the key's pinned
origin (paid), applies per-key/per-recipient limits, and idempotently queues the message. It
never returns or logs the reset token. Provider downtime leaves the outbox item pending for
bounded retry; the public forgot-password response remains the same for known and unknown
addresses.

Preserve the existing Resend DKIM and `send.engraphis.com` SPF records. Before GA, configure
inbound `keys@`, `billing@`, `support@`, and `dmarc@`; publish DMARC at `p=none`, observe for
seven days, review alignment reports, then move to `p=quarantine`. DNS changes require
execution-time authorization.

## Backup and restore

Run `scripts/commercial_backup.py backup` daily against an off-volume mount with a 64-hex
`ENGRAPHIS_BACKUP_KEY`. Retain 30 daily artifacts. The command snapshots memory, user/auth,
relay/control-plane, and durable Polar webhook SQLite databases with the online backup API,
runs integrity checks, encrypts the archive with AES-256-GCM, decrypts and verifies it, then
updates the marker used by readiness. The vendor archive therefore preserves the registry's
issued-license/order state and the Polar store's delivery claims and `subscription_seats`
ordering baseline; both are required to prevent duplicate fulfillment or stale seat changes
after a restore.

Customer-mode archives also include only this reviewed state allowlist when each file exists:
`license.key`, `machine_id`, `lease.sig`, `sync.token`, `sync.read_only`, `trial.json`,
`trial_used.json`, and `.clock_anchor`. Each entry is capped at 1 MiB, symlinks are rejected,
and the backup code never walks the state directory. Vendor mode includes none of those
customer files. Its separate, strict allowlist contains only `undelivered_license_keys.tsv`
beside `ENGRAPHIS_WEBHOOK_STATE`, when that manual-fulfillment fallback exists. It can contain
buyer addresses and live license keys; it is capped at 1 MiB and restored with mode `0600`.
No unreviewed file or signing seed can enter either archive. Both encrypted archive types
contain live credentials, so protect each artifact and its backup key accordingly.

Set `ENGRAPHIS_RELAY_DB` and `ENGRAPHIS_WEBHOOK_STATE` to persistent-volume paths on the
vendor service. Vendor-mode backup fails before creating an artifact or updating readiness if
either database is absent. Do not create an empty substitute during a backup: initialize the
real control-plane stores during staging setup and prove they contain the expected tables.

Set `ENGRAPHIS_BACKUP_OUTPUT_DIR` to the off-volume mount and
`ENGRAPHIS_BACKUP_STATUS_FILE` to the on-volume marker on both managed services. The daily
`commercial encrypted backups` workflow calls the protected customer and control-plane
backup endpoints; neither response exposes the artifact path or encryption key.
Set a separate strong `ENGRAPHIS_API_TOKEN` on the managed customer service and copy it to
the Actions secret `ENGRAPHIS_CUSTOMER_OPS_TOKEN`. Do not put the deployment ownership
token in that secret: it intentionally does not bypass Team authentication after the first
admin exists.

The `commercial encrypted restore drill` workflow runs monthly without production secrets. It
creates representative vendor registry, order, delivery-idempotency, and seat-baseline state,
encrypts it with an ephemeral key, and restores it only into a new runner-temporary directory.
The restore command refuses any destination that already exists, so the drill cannot overwrite
staging or production data.

Restore output is deliberately staged: SQLite databases appear at the restore root and the
reviewed private-state allowlist appears under `<restore-dir>/.engraphis/` with mode `0600`. Set the
target deployment's path environment variables before running restore so its owner-only
`RESTORE_PLAN.json` resolves the intended live destinations. Review that
`engraphis-restore-plan/v1` document and validate every staged file before acting. With all
writers stopped, place the databases at the reviewed paths and copy each staged `.engraphis`
file only to the exact destination recorded in the plan. Customer files target
`ENGRAPHIS_STATE_DIR`; the vendor `undelivered_license_keys.tsv` targets the directory beside
`ENGRAPHIS_WEBHOOK_STATE`. Before restarting a restored vendor service, reconcile every fallback
row against the restored registry, Polar order state, and durable email outbox. Deliver each
still-undelivered key exactly once, then remove the live fallback file or move it to an encrypted,
access-restricted retention archive. The plan records
`automatic_overwrite=false`; the restore command never overwrites live data for you. After
restart, create a new encrypted backup and require both serving and authenticated operations
readiness.

That synthetic drill validates the mechanism, not backup freshness or access to the production
archive store. Run `verify` daily and, before every production release, restore a current
production-like artifact into an empty staging directory, restore the staged customer state
into a disposable `ENGRAPHIS_STATE_DIR`, and run the commercial smoke suite. Losing the backup
key makes encrypted backups unrecoverable.

## Monitoring and rollback

The hourly `commercial production synthetics` workflow checks public customer readiness,
authenticated customer storage readiness, control-plane readiness/details, and the
non-mutating trial dependency chain. GitHub Actions failure notifications are the baseline
alert channel. Infrastructure alerts must also cover volume free space and uptime.
Set `ENGRAPHIS_JSON_LOGS=1` on both hosted services. JSON logs contain bounded event text
and exception types; bearer tokens, license keys, secret assignments, and email-address
shapes are redacted before emission.
The control-plane outbox readiness check also alerts on exhausted retries, stale backlog,
and the 24-hour bounce/complaint rate. Tune `ENGRAPHIS_EMAIL_MAX_BACKLOG_AGE_SECONDS`,
`ENGRAPHIS_EMAIL_MAX_BOUNCE_RATE`, and `ENGRAPHIS_EMAIL_BOUNCE_MIN_SAMPLE` only from
observed production delivery volume.

Roll back immediately on entitlement leakage, unsigned issuance, duplicate trials,
invitation privilege escalation, purchase without delivery, data-loss symptoms, or sustained
readiness failure. Public checkout remains disabled until 24 hours of clean production
synthetics after deployment.
