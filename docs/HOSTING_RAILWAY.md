# Host on Railway (5 minutes)

Deploy **one** Engraphis instance on Railway and access your memories from any browser,
on any device. Two paths, one repository workflow:

| | **Pro solo** | **Team admin** |
|---|---|---|
| Who | A Pro member (individual) | A Team administrator |
| License | Pro key or Pro trial | Team key or Team trial |
| Users | One admin (you) | Admin + invited members (seats) |
| Agent connect | Local agents sync to your Railway relay | Members connect agents directly to the cloud instance |
| Cost | ~$10–25/mo infra + $10/mo Pro | ~$10–25/mo infra + $20/mo/seat Team |

Both paths use the same Docker image and the same volume + proxy
setup. The only difference is the license key you activate and whether you invite members.

For the solo/local-first lane (agent runs on your machine, optional cloud sync) see
[SYNC.md](./SYNC.md); for how agents connect to a hosted Team instance see
[AGENT_CONNECT.md](./AGENT_CONNECT.md).

---

## Pro solo path (cloud dashboard + sync relay)

A Pro member deploys one instance to get a cloud-accessible dashboard (analytics,
automation, export) and a self-hosted sync relay — your local agents sync through your
own Railway instance instead of the vendor relay. One admin, no member seats.

### What you need
- A Railway account. Roughly **~$10–25/mo** for one small always-on service + a persistent
  volume.
- A **Pro license key** (purchase, or start a Pro trial from the dashboard once it's up).

### 1. Deploy
In Railway choose **New Project → Deploy from GitHub repo**, select your Engraphis fork
or `Coding-Dev-Tools/engraphis`, and deploy it. Railway builds the Dockerfile, which
defaults to the v2 dashboard on port `8700` and runs as a non-root user.

### 2. Add a persistent volume (required)
Without this, activated license keys, the one-time trial, and **all memories** are lost
on every redeploy. In Railway: **service → Settings → Volumes → New Volume → mount path
`/data`**. Allocate at least **3 GiB** with the default 2 GiB per-account relay quota;
the database, license registry, and model cache need additional headroom. The Dockerfile
already writes the DB and license state under `/data`.

### 3. Trust Railway's proxy and set the public URL
Railway fronts the container with a TLS proxy. In Railway: **service → Variables → add**:

```
ENGRAPHIS_FORWARDED_ALLOW_IPS=*
ENGRAPHIS_DASHBOARD_URL=https://<your Railway public domain>
ENGRAPHIS_RELAY_URL=https://<your Railway public domain>
ENGRAPHIS_CLOUD_URL=https://team.engraphis.com
ENGRAPHIS_API_TOKEN=<a random 32-byte-or-longer secret>
```

Use the generated Railway domain first, or a custom domain you own. This is the canonical
URL **for your deployment**; `team.engraphis.com` is Engraphis's managed service, not a
customer-owned hostname. `ENGRAPHIS_DASHBOARD_URL` drives reset links,
redirects, and hosted MCP Host/Origin checks. `ENGRAPHIS_RELAY_URL` makes the cloud
dashboard exchange data through the relay mounted on this same instance.
`ENGRAPHIS_CLOUD_URL` keeps license leases, hosted trials, and fallback invite delivery on
Engraphis's managed issuer instead of sending those requests to the customer sync relay.
`ENGRAPHIS_API_TOKEN` proves deployment ownership when you start a hosted trial or create
the first admin; enter it in the corresponding hosted setup field. You may remove it after
setup unless service automation uses it.

### 4. Activate Pro
Add `ENGRAPHIS_LICENSE_KEY=<your-pro-key>` in Railway Variables and redeploy. The key is
server-validated. Alternatively, open the dashboard, choose **Start Pro trial**, and open
the confirmation link sent to your email. For a hosted first boot, copy the confirmed key
into the private `ENGRAPHIS_LICENSE_KEY` Railway variable and redeploy; browser activation
remains admin-only so an arbitrary key holder cannot claim a fresh public instance.

### 5. Create your admin account
Once `/api/license` reports `plan: "pro"`, the dashboard presents **Create admin
account**. Enter the `ENGRAPHIS_API_TOKEN` from Railway along with the account fields.
This is a single-admin instance — you can't invite members (that requires
Team). The admin account gives you a browser session (session cookie) and the ability to
mint a per-user bearer token for API access.

### 6. Use it
- **Browser dashboard:** sign in at your Railway deployment URL with email + password.
  All Pro features are unlocked: analytics, export, automation, cloud sync.
- **Sync relay:** enable auto-sync in the cloud dashboard (or use **Sync now**). Activate
  the same Pro key on each local instance, then set
  `ENGRAPHIS_RELAY_URL=https://<your Railway public domain>`. Your local agents write locally;
  each sync pass exchanges changes through the hosted relay, and the cloud dashboard
  shows them after its next sync pass.
- **API access:** sign in, open **Settings → Connect your agent → Create token**, and use
  the bearer token for HTTP API access (`GET /api/recall` is read-enabled; write
  endpoints like `POST /api/remember` require a Team license — Pro solo uses cloud sync
  for writes, not direct agent-connect).

### 7. Configure the canonical domain
Follow the Team custom-domain steps below. Set both
`ENGRAPHIS_DASHBOARD_URL` and `ENGRAPHIS_RELAY_URL` to that domain, then update local instances to use
that relay URL so links, password resets, and sync all use the canonical domain.

---

## Team admin path (multi-user, members join with credentials)

Deploy one instance for your team; members sign in at your URL and connect their agents
over HTTP/MCP — **no local install for members**. The admin does a one-time deploy;
everyone else just logs in. A Team license (the instance's) is required for agent-connect;
members never need a key to log in.

### What you need
- A Railway account (the **admin's** — Railway hosting is billed to the admin's account,
  not yours). Roughly **~$10–25/mo** for one small always-on service + a persistent volume.
- A **Team license key** (purchase, or start a Team trial from the dashboard once it's up).

### 1. Deploy
In Railway choose **New Project → Deploy from GitHub repo → select your Engraphis fork
or `Coding-Dev-Tools/engraphis`**. Railway builds from the
Dockerfile, which defaults to the v2 **team** dashboard on port `8700` and runs as a
non-root user. (`railway.json` tells Railway the healthcheck at `/api/health`.)

The canonical public URL is the generated Railway domain or a custom domain **you own**.
Set `ENGRAPHIS_DASHBOARD_URL` to that URL so invites, password resets, redirects, and
hosted MCP checks agree. Set `ENGRAPHIS_RELAY_URL` to the same URL when local Pro clients
will sync through this deployment. `team.engraphis.com` remains the managed vendor
license/relay service; do not point customer DNS at it.

### 2. Add a persistent volume (required)
Without this, activated license keys, the one-time trial, and **all memories** are lost
on every redeploy. In Railway: **service → Settings → Volumes → New Volume → mount path
`/data`**. Allocate at least **3 GiB** with the default 2 GiB per-account relay quota;
the DB, registry, and model cache need headroom. The Dockerfile writes the DB and license
state under `/data`.

### 3. Trust Railway's forwarded headers
Railway fronts the container with a TLS proxy that isn't at `127.0.0.1`. Trust that proxy
so the application interprets the external scheme and client address correctly. In Railway:
**service → Variables → add**:

```
ENGRAPHIS_FORWARDED_ALLOW_IPS=*
ENGRAPHIS_CLOUD_URL=https://team.engraphis.com
ENGRAPHIS_API_TOKEN=<a random 32-byte-or-longer secret>
```

(You can scope this to Railway's egress range instead of `*` if you prefer.)
Keep `ENGRAPHIS_CLOUD_URL` on the managed issuer if this deployment also sets
`ENGRAPHIS_RELAY_URL` to itself for customer-operated sync.
The API token is required only as proof that you control the deployment while creating
the first admin. Keep it if service automation needs a shared credential; otherwise
remove it from Railway after setup.

> **Port:** Railway auto-detects `8700` from the Dockerfile's `EXPOSE`. If the deploy
> shows a port mismatch / 502, set the service's **Port** to `8700`.

### 4. Configure an optional custom domain
For a domain you control, such as `https://memory.example.com`:
1. **Railway → service → Settings → Networking → Custom Domain →** add
   `memory.example.com`; Railway shows a CNAME target.
2. In your DNS, add `memory.example.com CNAME → <railway target>`. Railway auto-issues the
   TLS certificate.
3. **Variables → update:** `ENGRAPHIS_DASHBOARD_URL=https://memory.example.com`
   (with `https://`, no trailing slash).

### 5. Activate Team, then bootstrap the admin
`POST /api/auth/setup` deliberately refuses to create the first admin until a paid
entitlement is active. Bootstrap it one of two ways:

- **Purchased key:** add `ENGRAPHIS_LICENSE_KEY=<your-key>` in Railway Variables and
  redeploy. The key is server-validated and sets the seat cap.
- **Trial:** open the dashboard, enter the deployment's `ENGRAPHIS_API_TOKEN`, choose
  **Start Team trial**, and open the confirmation link sent to your email. Copy the
  displayed key into Railway's private `ENGRAPHIS_LICENSE_KEY` variable and redeploy.
  The trial route works before login but requires the deployment token; activation
  remains admin-only.

Once `/api/license` reports `plan: "team"`, the dashboard presents **Create admin
account**. Enter the `ENGRAPHIS_API_TOKEN` from Railway in the hosted setup form, create
the admin, then use **Settings → License** for later key replacement.
`/api/license/activate` stays admin-only; a purchased key cannot be pasted through that
route before the first admin exists.

### 6. Invite members (seats)
**Team → Add member** (email + initial password + role: viewer/member/admin). Each member
is a seat; you can't add more active members than your Team license's seats. Members get an
invite email pointing at your dashboard URL; they sign in with email + password — **no
key, no local install**. If the license later lapses, the authentication wall stays active
and existing users can still sign in, while Team-gated operations return `402`.

### 7. Members connect their agents
Each member signs in, opens **Settings → Connect your agent → Create token**, and pastes
the one-time bearer token into their agent config. Two transports (see
[AGENT_CONNECT.md](./AGENT_CONNECT.md) for full details):

- **HTTP** (always available): `POST https://<your-domain>/api/remember` and
  `GET https://<your-domain>/api/recall` with `Authorization: Bearer <token>`.
- **MCP-over-HTTP:** point an MCP client at `https://<your-domain>/mcp` with the
  bearer header.

Writes land in the same v2 store the dashboard reads; the instance's Team license is what
unlocks the write endpoints (`402` without it).

---

## Cost & limits
- **Infra:** one flat instance per team or solo user on the admin's/member's Railway
  account (~$10–25/mo), amortized across seats for Team — *not* per user. Team seats are
  $20/mo each; Pro is $10/mo flat ($100/yr).
- **Embedder:** CPU inference of `all-MiniLM-L6-v2` on every write/recall is the main cost
  driver. For write-heavy deployments, set `ENGRAPHIS_EMBED_MODEL` to an external embedding
  API (the config supports an API embedder) to cut Railway CPU and improve latency.
- **Scale:** the dashboard uses a single SQLite (WAL) store — fine for ~tens of concurrent
  agents, not hundreds. Cap seat sales accordingly until a Postgres backend exists.
- **Backups:** Railway volumes are not auto-backed-up. Enable Railway volume backups, or
  run `GET /api/export?workspace=…` on a cron, so you're not on the hook for data loss.

## Security notes
- Expose the instance over **HTTPS only** (Railway does this). Bearer tokens and session
  cookies must not transit cleartext.
- Per-user tokens are SHA-256 hashed at rest; the raw token is shown once. Disabling a
  member instantly invalidates their tokens.
- `/api/remember` and `/mcp` require an active Team license (`402` otherwise).
  A lapse keeps the authentication wall in place; existing users can still log in.
- The auth wall activates on any paid license (Pro or Team), closing the pre-bootstrap
  exposure window on Railway. A free/unlicensed instance stays open for local-only use.
