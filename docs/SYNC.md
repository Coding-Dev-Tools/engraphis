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

## Trial and grace

The no-card Pro or Team trial begins after email confirmation and lasts **exactly 3 active
days**.

`workspace_write_grace` is separate. It may preserve ordinary writes to an already provisioned
local workspace for at most **24 hours** following an authoritative entitlement denial. It never
extends the trial or subscription, and it never grants Cloud Sync, Analytics, Automation, Auto
Dreaming, Auto Consolidation, Team access, seats, or credentials. Cloud access may stop
immediately even while local write grace remains.

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
  --relay https://team.engraphis.com
```

The dashboard's **Sync now** action invokes the same customer protocol. The public package does
not run a local auto-sync loop or ship a cron/Task Scheduler wrapper. Hosted automation belongs
to the private service.

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

Bundle input is untrusted. The client validates schema and size limits before applying records,
rechecks workspace scope, and retains provenance/audit evidence. A relay cannot inject a record
outside the authorized workspace merely by changing bundle fields.

## Security and privacy

- Use HTTPS for every hosted endpoint. The public client rejects redirects, embedded URL
  credentials, and unsafe remote targets.
- Treat cloud session and refresh files as credentials; keep their directory owner-only.
- `secret` memories are excluded from managed uploads. Managed compute also rejects secret rows
  server-side.
- Relay transport is TLS-protected, but Engraphis does not claim end-to-end encryption until a
  client-side encrypted bundle format ships.
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
