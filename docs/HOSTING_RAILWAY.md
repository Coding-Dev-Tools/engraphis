# Host your team on Railway (5 minutes)

Deploy **one** Engraphis instance on Railway and your teammates sign in at your URL and
connect their agents over HTTP/MCP — **no local install for members**. The admin does a
one-time deploy; everyone else just logs in. A Team license (the instance's) is required
for agent-connect; members never need a key to log in.

This is the team lane from the deployment plan. For the solo/local-first lane (agent runs
on your machine, optional cloud sync) see [SYNC.md](./SYNC.md); for how agents connect to
this hosted instance see [AGENT_CONNECT.md](./AGENT_CONNECT.md).

## What you need
- A Railway account (the **admin's** — Railway hosting is billed to the admin's account,
  not yours). Roughly **~$10–25/mo** for one small always-on service + a persistent volume;
  the sentence-transformers embedder is the main cost lever (see *Tuning cost* below).
- A **Team license key** (purchase, or start a Team trial from the dashboard once it's up).

## 1. Deploy
Click the "Deploy on Railway" button in the README (or in Railway: **New Project →
Deploy from GitHub repo → select `Coding-Dev-Tools/engraphis`**). Railway builds from the
Dockerfile, which defaults to the v2 **team** dashboard on port `8700` and runs as a
non-root user. (`railway.json` tells Railway the healthcheck at `/api/health`.)

Railway gives the service a public URL like `https://engraphis-production.up.railway.app`.
**Do not** set `ENGRAPHIS_DASHBOARD_URL` to that built-in relay-style URL — it's *your
service's* URL, not the cloud-sync relay. (See step 4 for the right value.)

## 2. Add a persistent volume (required)
Without this, activated license keys, the one-time trial, and **all memories** are lost
on every redeploy. In Railway: **service → Settings → Volumes → New Volume → mount path
`/data`** (1 GB is plenty for a small team). The Dockerfile already writes the DB and
license state under `/data`.

## 3. Set the forwarded-proxy env (required for logins over HTTPS)
Railway fronts the container with a TLS proxy that isn't at `127.0.0.1`, so uvicorn won't
mark the session cookie `Secure` unless you allow its forwarded headers. In Railway:
**service → Variables → add**:

```
ENGRAPHIS_FORWARDED_ALLOW_IPS=*
```

(You can scope this to Railway's egress range instead of `*` if you prefer.)

> **Port:** Railway auto-detects `8700` from the Dockerfile's `EXPOSE`. If the deploy
> shows a port mismatch / 502, set the service's **Port** to `8700`.

## 4. (Optional) Custom domain
For `https://team.engraphis.com`:
1. **Railway → service → Settings → Networking → Custom Domain →** add
   `team.engraphis.com`; Railway shows a CNAME target.
2. In your DNS, add `team.engraphis.com CNAME → <railway target>`. Railway auto-issues the
   TLS cert.
3. **Variables → add:** `ENGRAPHIS_DASHBOARD_URL=https://team.engraphis.com`
   (with `https://`, no trailing slash) — this is what invite/password-reset emails link to.

## 5. Bootstrap the admin + activate the Team license
Open your Railway URL. The first `/api/auth/setup` creates the **admin** account (email +
password) — the bootstrap admin is **exempt** from the license gate, so you can set up with
no key yet. Then **Settings → License → paste your Team key → Activate**. The Team key sets
the **seat cap** and is server-validated against the relay; a free/lapsed instance keeps
the UI working but gates agent-connect to `402`.

## 6. Invite members (seats)
**Team → Add member** (email + initial password + role: viewer/member/admin). Each member
is a seat; you can't add more active members than your Team license's seats. Members get an
invite email pointing at your dashboard URL; they sign in with email + password — **no
key, no local install**. (Login is deliberately never license-gated, so a lapsed key never
locks the team out of the UI.)

## 7. Members connect their agents
Each member signs in, opens **Settings → Connect your agent → Create token**, and pastes
the one-time bearer token into their agent config. Two transports (see
[AGENT_CONNECT.md](./AGENT_CONNECT.md) for full details):

- **HTTP** (always available): `POST https://team.engraphis.com/api/remember` and
  `GET https://team.engraphis.com/api/recall` with `Authorization: Bearer <token>`.
- **MCP-over-HTTP** (once the `/mcp` mount lands): point an MCP client at
  `https://team.engraphis.com/mcp` with the bearer header.

Writes land in the same v2 store the dashboard reads; the instance's Team license is what
unlocks the write endpoints (`402` without it).

## Cost & limits
- **Infra:** one flat instance per team on the admin's Railway account (~$10–25/mo),
  amortized across seats — *not* per user. Team seats are $20/mo each.
- **Embedder:** CPU inference of `all-MiniLM-L6-v2` on every write/recall is the main cost
  driver. For write-heavy teams, set `ENGRAPHIS_EMBED_MODEL` to an external embedding API
  (the config supports an API embedder) to cut Railway CPU and improve latency.
- **Scale:** the dashboard uses a single SQLite (WAL) store — fine for ~tens of concurrent
  agents, not hundreds. Cap seat sales accordingly until a Postgres backend exists.
- **Backups:** Railway volumes are not auto-backed-up. Enable Railway volume backups, or
  run `GET /api/export?workspace=…` on a cron, so you're not on the hook for data loss.

## Security notes
- Expose the instance over **HTTPS only** (Railway does this). Bearer tokens and session
  cookies must not transit cleartext.
- Per-user tokens are SHA-256 hashed at rest; the raw token is shown once. Disabling a
  member instantly invalidates their tokens.
- `/api/remember` (and `/mcp`) require an active Team license (`402` otherwise) — that's
  the "a Team license is required to connect" gate. Login is never gated.