# Host on Railway (5 minutes)

Deploy **one** Engraphis instance on Railway and access your memories from any browser,
on any device. Two paths, same button:

| | **Pro solo** | **Team admin** |
|---|---|---|
| Who | A Pro member (individual) | A Team administrator |
| License | Pro key or Pro trial | Team key or Team trial |
| Users | One admin (you) | Admin + invited members (seats) |
| Agent connect | Local agents sync to your Railway relay | Members connect agents directly to the cloud instance |
| Cost | ~$10–25/mo infra + $10/mo Pro | ~$10–25/mo infra + $20/mo/seat Team |

Both paths use the same Docker image, the same Deploy button, and the same volume + proxy
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
Click the "Deploy on Railway" button in the README. Railway builds from the Dockerfile,
which defaults to the v2 dashboard on port `8700` and runs as a non-root user.

### 2. Add a persistent volume (required)
Without this, activated license keys, the one-time trial, and **all memories** are lost
on every redeploy. In Railway: **service → Settings → Volumes → New Volume → mount path
`/data`** (1 GB is plenty). The Dockerfile already writes the DB and license state
under `/data`.

### 3. Set the forwarded-proxy env (required for logins over HTTPS)
Railway fronts the container with a TLS proxy. In Railway: **service → Variables → add**:

```
ENGRAPHIS_FORWARDED_ALLOW_IPS=*
```

### 4. Activate Pro
Add `ENGRAPHIS_LICENSE_KEY=<your-pro-key>` in Railway Variables and redeploy. The key is
server-validated. Alternatively, open the dashboard, choose **Start Pro trial**, and open
the confirmation link sent to your email.

### 5. Create your admin account
Once `/api/license` reports `plan: "pro"`, the dashboard presents **Create admin
account**. This is a single-admin instance — you can't invite members (that requires
Team). The admin account gives you a browser session (session cookie) and the ability to
mint a per-user bearer token for API access.

### 6. Use it
- **Browser dashboard:** sign in at your Railway URL with email + password. All Pro
  features are unlocked: analytics, export, automation, cloud sync.
- **Sync relay:** activate the same Pro key on each local instance, then set
  `ENGRAPHIS_RELAY_URL` to your Railway URL (e.g.
  `https://your-app.up.railway.app`). Your local agents write locally and sync to your
  Railway instance — the dashboard reflects synced memories in real time.
- **API access:** sign in, open **Settings → Connect your agent → Create token**, and use
  the bearer token for HTTP API access (`GET /api/recall` is read-enabled; write
  endpoints like `POST /api/remember` require a Team license — Pro solo uses cloud sync
  for writes, not direct agent-connect).

### 7. (Optional) Custom domain
Same as the Team path (see below). Set `ENGRAPHIS_DASHBOARD_URL=https://your-domain` so
sync links and password-reset emails point at your domain.

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
Click the "Deploy on Railway" button in the README (or in Railway: **New Project →
Deploy from GitHub repo → select `Coding-Dev-Tools/engraphis`**). Railway builds from the
Dockerfile, which defaults to the v2 **team** dashboard on port `8700` and runs as a
non-root user. (`railway.json` tells Railway the healthcheck at `/api/health`.)

Railway gives the service a public URL like `https://engraphis-production.up.railway.app`.
**Do not** set `ENGRAPHIS_DASHBOARD_URL` to that built-in relay-style URL — it's *your
service's* URL, not the cloud-sync relay. (See step 4 for the right value.)

### 2. Add a persistent volume (required)
Without this, activated license keys, the one-time trial, and **all memories** are lost
on every redeploy. In Railway: **service → Settings → Volumes → New Volume → mount path
`/data`** (1 GB is plenty for a small team). The Dockerfile already writes the DB and
license state under `/data`.

### 3. Set the forwarded-proxy env (required for logins over HTTPS)
Railway fronts the container with a TLS proxy that isn't at `127.0.0.1`, so uvicorn won't
mark the session cookie `Secure` unless you allow its forwarded headers. In Railway:
**service → Variables → add**:

```
ENGRAPHIS_FORWARDED_ALLOW_IPS=*
```

(You can scope this to Railway's egress range instead of `*` if you prefer.)

> **Port:** Railway auto-detects `8700` from the Dockerfile's `EXPOSE`. If the deploy
> shows a port mismatch / 502, set the service's **Port** to `8700`.

### 4. (Optional) Custom domain
For `https://team.engraphis.com`:
1. **Railway → service → Settings → Networking → Custom Domain →** add
   `team.engraphis.com`; Railway shows a CNAME target.
2. In your DNS, add `team.engraphis.com CNAME → <railway target>`. Railway auto-issues the
   TLS cert.
3. **Variables → add:** `ENGRAPHIS_DASHBOARD_URL=https://team.engraphis.com`
   (with `https://`, no trailing slash) — this is what invite/password-reset emails link to.

### 5. Activate Team, then bootstrap the admin
`POST /api/auth/setup` deliberately refuses to create the first admin until a paid
entitlement is active. Bootstrap it one of two ways:

- **Purchased key:** add `ENGRAPHIS_LICENSE_KEY=<your-key>` in Railway Variables and
  redeploy. The key is server-validated and sets the seat cap.
- **Trial:** open the dashboard, choose **Start Team trial**, and open the confirmation
  link sent to your email. The public license/trial routes work before login.

Once `/api/license` reports `plan: "team"`, the dashboard presents **Create admin
account**. Create the admin, then use **Settings → License** for later key replacement.
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

- **HTTP** (always available): `POST https://team.engraphis.com/api/remember` and
  `GET https://team.engraphis.com/api/recall` with `Authorization: Bearer <token>`.
- **MCP-over-HTTP:** point an MCP client at `https://team.engraphis.com/mcp` with the
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
