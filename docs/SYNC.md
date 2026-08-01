# Cloud Sync

Engraphis remains local-first: the free engine stores memories in local SQLite and works
without an account or network. **Cloud Sync** is a hosted Pro/Team service that connects
authorized installations through Engraphis-managed relay storage.

The public repository contains the customer-side protocol, deterministic merge engine, and
relay client required to participate in that service. It does **not** contain the hosted relay,
organization authorization, entitlement registry, storage credentials, automatic scheduler, or
operations tooling. An environment variable cannot turn the public image into the official relay.

## Product boundary

| Layer | Public Apache package | Private hosted service |
|---|---|---|
| Local memory database and free engine | Yes | No requirement |
| Deterministic bundle/merge protocol | Yes | Uses the same contract |
| Customer relay client | Yes | Authenticates it |
| Relay storage and tenant isolation | No | Yes |
| Device registration and credential rotation | Client only | Authority |
| Organization membership and named seats | No | Yes |
| Automated cloud cadence and operations | No | Yes |

The split is deliberate. Local checks in Apache-licensed code are not DRM and can be changed by
a fork. The paid boundary is authorization to use the official private service and its operated
infrastructure.

Cloud Sync is available with hosted Pro and Team plans. See [local and hosted plans](HOSTED_PLANS.md)
for pricing and included services.

## Trial and grace

The no-card Pro or Team trial begins after email confirmation and lasts **exactly 3 active
days**.

`workspace_write_grace` is separate and private-service enforced. It may preserve bounded
hosted-account continuity operations for at most **24 hours** following an authoritative
entitlement denial. It never extends the trial or subscription, and it never grants Cloud Sync,
Analytics, Automation, Auto Dreaming, Auto Consolidation, Team access, seats, or credentials.
Cloud access may stop immediately. The free local sync-folder primitive and local core are not
gated by this hosted lifecycle state.

## Configure a customer installation

Hosted onboarding creates an owner-only cloud session under `~/.engraphis` (or
`ENGRAPHIS_STATE_DIR`). For non-interactive clients, inject credentials through a secrets manager:

```dotenv
ENGRAPHIS_CLOUD_CONTROL_URL=https://api.engraphis.com
ENGRAPHIS_CLOUD_COMPUTE_URL=https://compute.engraphis.com
ENGRAPHIS_CLOUD_ORGANIZATION_ID=org_replace_me
ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL=<secret>
```

The refresh credential rotates. Refresh is serialized across threads and cooperating processes,
and the client stores only the replacement needed for the next session in an owner-only file.
After the first rotation, that saved replacement takes precedence over a still-present bootstrap
environment credential. Do not place either value in source, documentation, container images,
shell history, or support logs.

The one-shot customer client remains available for explicit sync operations:

```bash
python -m scripts.sync \
  --db engraphis.db \
  --workspace acme \
  --relay https://relay.engraphis.com
```

Cloud Sync is fail-closed: install `engraphis[cloud-sync]` on Python 3.10+ and provision a
32-byte URL-safe-base64 workspace key as `ENGRAPHIS_SYNC_E2EE_KEY` on every authorized device
before the first upload. Generate it once on a trusted device and transfer it only through your
own secure channel; Engraphis Cloud never receives, derives, or recovers this key. For a
one-off command, pass the same value with `--relay-e2ee-key`. A missing or malformed key stops
Cloud Sync rather than uploading a plaintext bundle.

```bash
python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('='))"
```

The dashboard's **Sync now** action invokes the same customer protocol. The public package does
not run a local auto-sync loop or ship a cron/Task Scheduler wrapper. Hosted automation belongs
to the private service. If the relay denies every attempted shared workspace because the session
is expired, revoked, or no longer entitled, the dashboard returns to the hosted Pro/Team recovery
CTA instead of reporting a successful empty sync. A successful empty or read-only workspace keeps
the result partial so another workspace's denial is not misreported as a total authorization loss.

### Local folder transport

The public protocol also retains a manual folder transport for development, backup interchange,
and offline testing:

```bash
python -m scripts.sync \
  --db engraphis.db \
  --workspace acme \
  --remote /path/to/shared-folder \
  --dry-run
```

This is a customer-controlled file exchange primitive, not the official Cloud Sync service. It
has no hosted identity, seat, availability, support, or managed-storage guarantees.

## Merge semantics

Sync exchanges bounded workspace snapshots and merges them deterministically. Existing
bi-temporal history is preserved: conflicts close validity windows or create explicit successor
records rather than destructively overwriting facts. The public merge code is necessary so a
customer can verify how their local database changes.

Session scope is strictly device-local. Every exported workspace or repo bundle excludes both
live and invalidated session-scoped rows, as well as `secret` rows, and includes a memory link only
when both endpoints remain in the export. Inbound legacy or untrusted bundles cannot create,
relabel, or overwrite session-scoped state because the sync format carries no authenticated
session owner or lifecycle contract.

Bundle format v2 preserves durable claim identity and the system-time at which a
world-time invalidation was learned. Current Engraphis accepts inbound v1 bundles for
compatibility but exports v2. Older clients reject v2 instead of silently forwarding a
downgraded bundle that loses those fields.

Bundle input is untrusted. The client validates schema and size limits before applying records,
rechecks workspace scope, and retains provenance/audit evidence. Every inbound memory is re-homed
under local `source: sync, trusted: false` provenance; a peer's serialized trust label, graph
metadata, retention hints, or extractor output has no authority. Suspicious payloads are
quarantined before indexing. A relay cannot inject a record outside the authorized workspace
merely by changing bundle fields.

An inbound bundle also cannot overwrite a locally approved memory with the same id. The local
record remains the safe winner and a content-free `sync_trust_conflict` audit event records a
competing peer payload. This intentionally favors integrity over automatic last-writer-wins for
cross-trust collisions; promote/approve a fresh local record if the peer's information is verified.
Likewise, unauthenticated bundle links may connect only records that remain in the untrusted
replica; a peer cannot attach graph edges to locally approved memories.

## Security and privacy

- Local-only installations send no memory content to Engraphis. **Cloud Sync encrypts eligible
  shared-workspace changes end-to-end before they leave this device. Engraphis Cloud cannot read
  their contents; secret and session-scoped memories stay local.** Managed compute is separate:
  connecting an installation to Engraphis Cloud accepts its terms and enables it by default;
  operators may opt out with `ENGRAPHIS_MANAGED_COMPUTE_CONSENT=0`. It sends a readable, bounded
  snapshot over TLS because Engraphis Cloud must process that snapshot to produce results.
- Treat cloud session and refresh files as credentials; keep their directory owner-only.
- `secret` memories are excluded from managed uploads. Managed compute also rejects secret rows
  server-side.
- Cloud Sync's end-to-end encryption applies to sync bundles, not to managed-compute snapshots or
  content deliberately submitted to a configured LLM provider. Those processors must be able to
  read the submitted content to perform the requested work.
- Cloud Sync uses a fresh ChaCha20-Poly1305 nonce for each upload and authenticates the stored
  opaque bundle name plus workspace as associated data. The relay can store or replay ciphertext,
  but a tampered, renamed, cross-workspace, wrong-key, or legacy plaintext bundle is rejected
  before it reaches the merge engine.
- Device credentials are not seats. Team seats are named organization members managed by the
  hosted control plane.
- Revocation and expiry are authoritative server decisions. A locally modified client does not
  acquire service access without a valid hosted credential.

## What Apache forks can do

Apache-2.0 rights in code already published here are perpetual under that license and cannot be
clawed back. A fork may alter or reuse the public client and merge protocol. That does not grant
access to Engraphis-operated infrastructure, private service code, signing keys, customer data,
support, or trademarks.

This is why future defensible value lives in the private hosted relay, compute, identity,
automation, security operations, and customer experience rather than in a local feature flag.
