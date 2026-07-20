# Commercial operations runbook (v1.0)

## Production topology

`team.engraphis.com` runs `ENGRAPHIS_SERVICE_MODE=customer`: dashboard, memory API,
authentication, invitations, and customer sync only. `license.engraphis.com` runs
`ENGRAPHIS_SERVICE_MODE=vendor`: issuance, leases, deployment trials, Polar webhooks,
transactional email, and authenticated operations checks. New signed keys use
`https://license.engraphis.com`; the old `team.engraphis.com/license/v1/*` proxy is retained
for the 90-day compatibility window, with `Deprecation`, `Sunset`, and successor `Link`
headers. It is removed in v1.1; the customer proxy strips cookies, forwarding headers, and
all vendor secrets.

Never place the Ed25519 signing seed, Polar webhook secret, vendor admin token, or
Engraphis Resend key on a customer service.

## Release readiness

`GET /api/ready` is the public serving gate used by the orchestrator. On the customer
service it checks the database/embedder path. On the vendor service it checks service mode,
the approved signer, writable registry, exact Polar webhook/organization/products and
idempotency store, mail configuration and worker liveness, and disk capacity. It deliberately
does not include backup age, admin-monitoring configuration, delivery backlogs, or externally
triggerable alert counters: those conditions require operator action, and restarting or
draining an otherwise healthy first deployment cannot repair them.

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
remain recoverable, and `POST /ops/email/retry` retries due work. Verified Resend webhook
events update delivered, bounced, and complained states. Readiness fails on terminal delivery
failures, an old backlog, or a statistically meaningful bounce rate above the configured
threshold. A provider-only outage leaves the key solely in that durable outbox; it does not
create a redundant plaintext fallback. `undelivered_license_keys.tsv` is created only when
durable enqueue itself fails. While that fallback exists, authenticated vendor operations
readiness reports `manual_fulfillment=false` until an operator completes the documented
reconcile/deliver/remove-or-encrypted-archive procedure.

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
