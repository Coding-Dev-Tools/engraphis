# Host Engraphis on Railway

The v1.0 customer deployment is one persistent dashboard for Pro or Team. It does not
issue licenses, process Polar events, or hold the vendor signing key; those responsibilities
belong only to `license.engraphis.com`.

## Preferred: official template

The composer worksheet is
[`deploy/railway-template.json`](../deploy/railway-template.json). It is not an importable
Railway schema. Once Railway assigns the reviewed public template code, use the button in
the README. Until then, deploy this GitHub repository and apply the same settings manually.

The template must create:

- one service built from `Dockerfile`;
- one persistent volume mounted at `/data`;
- a generated public HTTPS domain;
- `/api/ready` as the health check; and
- an `ENGRAPHIS_DEPLOYMENT_TOKEN` generated with Railway's `${{ secret(48) }}` template
  function. Copy it into onboarding, then seal the variable after first-admin setup.

## Manual deployment

1. Create a Railway project and deploy `Coding-Dev-Tools/engraphis` from GitHub.
2. Add a persistent volume mounted at `/data` before completing onboarding.
3. Generate a Railway public domain for the service.
4. Set these variables, replacing the domain and deployment token:

```dotenv
ENGRAPHIS_SERVICE_MODE=customer
ENGRAPHIS_DB_PATH=/data/engraphis.db
ENGRAPHIS_STATE_DIR=/data/.engraphis
ENGRAPHIS_TEAM_MODE=1
ENGRAPHIS_JSON_LOGS=1
ENGRAPHIS_FORWARDED_ALLOW_IPS=*
ENGRAPHIS_CLOUD_URL=https://license.engraphis.com
ENGRAPHIS_DASHBOARD_URL=https://YOUR-DOMAIN.up.railway.app
ENGRAPHIS_RELAY_URL=https://YOUR-DOMAIN.up.railway.app
ENGRAPHIS_DEPLOYMENT_TOKEN=YOUR-UNIQUE-32+-CHARACTER-SECRET
```

Do not add any of these to a customer service:

- `ENGRAPHIS_VENDOR_SIGNING_KEY`
- `ENGRAPHIS_VENDOR_ADMIN_TOKEN`
- `POLAR_WEBHOOK_SECRET`
- `POLAR_ORGANIZATION_ID`
- `POLAR_*_PRODUCT_ID`
- `ENGRAPHIS_RESEND_API_KEY` for Engraphis-operated transactional mail

Those are control-plane secrets. Customer deployments use the signed license service URL
and can optionally configure their own mail provider. Without one, invitations and password
resets are relayed server-to-server through the control plane using the active Pro/Team key;
reset tokens never appear in browser API responses or deployment logs.

Operators of a separate vendor control plane must follow
[`COMMERCIAL_OPERATIONS.md`](COMMERCIAL_OPERATIONS.md): the vendor admin token is an
independent 32+-character secret, Polar/Resend webhook signing material provides at least
16 raw/decoded bytes, and a file-backed Ed25519 seed is a regular file no larger than 1 KiB
with owner-only (`0600`) permissions on POSIX. These requirements do not change the
prohibition above for customer services.

## Hosted onboarding

Open the generated HTTPS domain while signed out. The wizard performs this sequence:

1. enter the deployment token;
2. choose a Pro or Team three-day trial;
3. enter and confirm an email address;
4. return to the deployment after confirmation; and
5. create the first admin with a chosen password.

The confirmation link uses scanner-safe GET/POST semantics. The browser never receives the
signed key: the customer server claims it server-to-server, stores it under
`/data/.engraphis`, and activates it without a Railway redeploy. A pending claim identifier
is safe to keep in browser storage, so closing and reopening the page recovers activation.

For an existing paid license, set `ENGRAPHIS_LICENSE_KEY` as a Railway secret. Paid Pro and
Team licenses renew online leases at `license.engraphis.com`; Free usage remains local and
does not call the license service.

## Team invitations and sync

Admins invite an email and role; they do not choose a temporary password. The invitation
reserves a seat for 72 hours. Resend invalidates the old link, revoke or expiry releases the
seat, and the user is created only when the recipient chooses a password.

Each user creates their own 90-day bearer token. The server stores only a hash and the
credential is revocable:

- viewers: agent read plus `sync:read`;
- members/admins: agent read/write plus `sync:read` and `sync:write`.

Paste a scoped token into Settings → Cloud sync on each local device. The account-wide Team
license key is never included in member invitations or used as the normal per-user sync
credential. The purchaser receives it only for initial deployment activation and recovery.
Each sync device keeps the raw bearer it must send in an owner-only
`$ENGRAPHIS_STATE_DIR/sync.token` file; protect that file like any other API credential.

## Persistence and recovery

The `/data` volume contains memories, auth state, machine identity, activated license, and
customer relay data. A redeploy without this volume is data loss.

Volume snapshots alone are not enough. Schedule a daily encrypted off-volume backup:

```bash
export ENGRAPHIS_BACKUP_KEY=<64-hex-secret-from-the-production-secret-store>
python -m scripts.commercial_backup backup \
  --output-dir /mounted-off-volume/engraphis \
  --marker /data/.engraphis/backup-status.json \
  --retention-days 30
```

Set `ENGRAPHIS_BACKUP_STATUS_FILE=/data/.engraphis/backup-status.json`. Verify the newest
artifact daily and run a monthly restore drill into an empty directory:

```bash
python -m scripts.commercial_backup verify /mounted-off-volume/engraphis/NEWEST.egbak
python -m scripts.commercial_backup restore /mounted-off-volume/engraphis/NEWEST.egbak \
  --output-dir /tmp/engraphis-restore-drill
```

The backup command refuses a destination on the live data device unless explicitly put in
drill mode. It uses SQLite online backups, checks integrity, encrypts with AES-256-GCM, and
writes the freshness marker only after decrypting the artifact and rechecking every
database checksum and SQLite integrity result. The separate monthly drill exercises the
copy into a new restore directory. Customer backups also preserve a strict allowlist of
machine/license/lease/trial state and the saved sync credential/policy from
`ENGRAPHIS_STATE_DIR`; each existing file is bounded, symlinks are rejected, and no directory
is walked. These credentials remain protected by the encrypted archive.

A restore is staged and never overwrites live data. Set the replacement deployment's target
path environment variables before restoring. The command writes databases at the restore root,
the allowed state files under `<restore-dir>/.engraphis/`, and an owner-only
`RESTORE_PLAN.json` that maps every staged file to its resolved live destination. Review the
plan, stop all writers, then put the databases at those paths and copy the staged `.engraphis`
content into `ENGRAPHIS_STATE_DIR`. After starting the replacement, create a new encrypted
backup and require both `/api/ready` and authenticated `/api/ops/ready` to pass.

For an Engraphis-operated managed deployment, configure a separate strong
`ENGRAPHIS_API_TOKEN` and store the same value in GitHub as
`ENGRAPHIS_CUSTOMER_OPS_TOKEN`. Scheduled backup and authenticated readiness workflows use
this revocable operations credential. They must not use `ENGRAPHIS_DEPLOYMENT_TOKEN`, which
is an ownership/onboarding secret rather than a permanent service-account credential.

## Acceptance checklist

From a logged-out Railway account, prove all of the following before the template is public:

- `/api/ready` returns 200 after a clean deploy;
- Pro and Team confirmation activate automatically without showing a key;
- first-admin setup rejects an incorrect deployment token;
- invitation accept, resend, revoke, expiry, and seat limits work;
- viewer sync is read-only and member/admin sync is read/write;
- a redeploy preserves users, licenses, tokens, and memories;
- a verified backup restores databases and the allowlisted `.engraphis` state into a
  disposable staging deployment; and
- the browser console has no CSP, accessibility, or network errors.

Publishing the template, changing DNS, and enabling public checkout remain release-operator
actions and occur only after the production acceptance gates pass.
