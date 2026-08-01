# Host the free Engraphis customer runtime on Railway

This repository can deploy the local memory engine and single-user customer dashboard. It does
**not** contain the official license issuer, billing fulfillment, Team identity, hosted relay,
managed compute, Auto Dreaming, Auto Consolidation worker, or transactional-email services.

A public deployment is a remote free customer node, not a self-hosted Pro or Team backend. No
service-mode or environment switch adds the missing hosted server implementations.

## Deploy

Use the `Dockerfile`, mount a private persistent volume at `/data`, and configure:

```dotenv
ENGRAPHIS_SERVICE_MODE=customer
ENGRAPHIS_DB_PATH=/data/engraphis.db
ENGRAPHIS_STATE_DIR=/data/.engraphis
ENGRAPHIS_API_TOKEN=<strong-random-secret>
ENGRAPHIS_JSON_LOGS=1
ENGRAPHIS_FORWARDED_ALLOW_IPS=*
```

Set `ENGRAPHIS_FORWARDED_ALLOW_IPS=*` only when the container is reachable exclusively through
Railway's trusted proxy. Set the dashboard's public URL where the runtime supports it, terminate
TLS at the platform edge, and keep the volume private.

The published template derives `ENGRAPHIS_DASHBOARD_URL` from Railway's generated public domain.
That lets the dashboard's MCP-over-HTTP endpoint accept the public dashboard origin without
loosening its host/origin allow-list. If you attach a custom domain, override it with that domain's
canonical HTTPS URL after Railway has activated the domain; do not use an internal Railway domain
or a URL containing credentials.

Do not add Resend (or any other email-provider) credentials to this customer node. The public
runtime has no transactional-email sender, verification, invitation, or billing-email service;
those systems remain in the official hosted control plane.

## Connect to hosted Pro/Team services

Complete onboarding through the official Engraphis Cloud dashboard, then configure only the
customer-side endpoints and credential created for the installation:

```dotenv
ENGRAPHIS_CLOUD_CONTROL_URL=https://api.engraphis.com
ENGRAPHIS_CLOUD_COMPUTE_URL=https://compute.engraphis.com
ENGRAPHIS_CLOUD_ORGANIZATION_ID=org_replace_me
ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL=<secret>
```

Prefer mounting the owner-only cloud session file rather than placing a rotating refresh
credential directly in deployment configuration. An injected environment credential is only the
bootstrap value; after rotation, the owner-only saved replacement takes precedence. **Cloud Sync
encrypts eligible shared-workspace changes end-to-end before they leave the device; Engraphis
Cloud cannot read their contents.** Managed compute is separate: once connected, it is enabled by
default for an authorized customer and may upload a readable snapshot capped at 16 MiB over HTTPS
to produce results. Secret-class and session-scoped rows are excluded client-side, and
secret-class rows are rejected server-side.
Set `ENGRAPHIS_MANAGED_COMPUTE_CONSENT=0` to opt the deployed installation back out.

## Persistence and recovery

The `/data` volume contains the local memory database and customer state. A redeploy without this
volume loses local data. Use Railway volume snapshots or an encrypted backup process and test
restoration into a disposable customer node.

Before relying on the deployment, verify:

- `/api/ready` returns 200 after a clean deploy;
- unauthenticated protected requests are rejected;
- a redeploy preserves the database and owner-only customer state;
- managed-service clients reject redirects and non-HTTPS remote endpoints; and
- browser console output contains no CSP, accessibility, or network errors.

The hosted trial lasts **exactly 3 active days** after email confirmation. A separate local-only
write grace is capped at 24 hours and never extends cloud access.

See [Licensing](LICENSING.md) for the Apache/source boundary and [Cloud Sync](SYNC.md) for the
customer relay client.
