# Agent Connect — point your agent at a hosted Engraphis instance

Team members can connect their coding agents (Claude Code, Cursor, any HTTP-capable
agent) **directly to a hosted Engraphis dashboard** and store memories in the cloud
instance instead of running Engraphis locally. A Team license (the instance's) is
required to write — a free / lapsed instance refuses agent writes with `402`.

This is the team counterpart to the local-first model in [SYNC.md](./SYNC.md): instead
of each member running a local MCP server + syncing, one admin hosts a single instance
(e.g. on Railway) and everyone else just connects.

## How it works

1. **Admin** deploys one instance (see the Dockerfile / `docker compose up`) and activates
   a **Team license** (Settings → License → paste key). The Team key sets the seat cap.
2. **Admin** invites members (Add member → email + password). Each member is a seat.
3. **Member** signs in at the dashboard URL (e.g. `https://team.engraphis.com/`) with
   email + password — no key, no local install. (Login is never license-gated, so a lapsed
   key never locks people out of the UI.)
4. **Member** opens **Settings → API tokens → Create token**, copies the bearer token.
5. **Member** configures their agent to call the instance's HTTP API with that token.

## Agent authentication

Agents authenticate with a **per-user bearer token** (`Authorization: Bearer <token>`),
minted from the dashboard. The token is bound to the member: their role, their personal
folders, and their seat. Disabling the member instantly invalidates their token.

Token management (requires a browser session):

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/auth/token` `{label}` | Mint a token (raw token returned **once**) |
| `GET`  | `/api/auth/tokens` | List your tokens (never includes the raw token) |
| `DELETE` | `/api/auth/token/{id}` | Revoke one of your tokens |
| `GET`  | `/api/auth/connect-info` | Verify a token + discover the API base / snippet |

`GET /api/auth/connect-info` works with either a cookie or a bearer, so an agent can hit
it first to confirm its token is valid and learn the base URL:

```bash
curl -H "Authorization: Bearer <token>" https://team.engraphis.com/api/auth/connect-info
```

## The agent write/read API

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/remember` | **Team-gated** (402 without a Team license). Same params as the local `engraphis_remember` MCP tool. |
| `GET`  | `/api/recall?q=…&workspace=…` | Read (not gated). |
| `GET`  | `/api/memory/{id}?workspace=…` | One memory. |
| `GET`  | `/api/why?q=…&workspace=…` / `/api/timeline?…` | Provenance / history. |

`POST /api/remember` body (all optional except `content`):

```json
{
  "content": "We use pnpm for all frontend repos.",
  "workspace": "default",
  "repo": null,
  "mtype": "semantic",
  "scope": "repo",
  "title": "",
  "importance": 0.0,
  "keywords": null,
  "metadata": null,
  "source": "agent",
  "trusted": true,
  "dedupe": true
}
```

Example:

```bash
curl -X POST https://team.engraphis.com/api/remember \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"content":"Redis caches the gateway.","workspace":"default"}'

curl "https://team.engraphis.com/api/recall?q=Redis&workspace=default" \
  -H "Authorization: Bearer <token>"
```

Writes go to the **same v2 store the dashboard reads** — there is no separate agent DB,
so memories written by an agent immediately appear in the UI and in every other member's
recall (subject to workspace / personal-folder scoping).

## "They need a Team license to connect"

The Team license is the **instance's** (the admin activates one key; the relay enforces
the seat cap server-side). Members never present a license to log in or connect — they
present a **seat** (an account) and a **token**. The `402` on `POST /api/remember` fires
only when the **instance** has no active Team key (or it has lapsed / been revoked), which
is exactly "a Team license is required to host team agents."

## Security notes

- Tokens are stored **SHA-256 hashed** (like session cookies); a leaked users DB contains
  no usable bearer secrets. The raw token is shown **once** at creation.
- Disable a member → their tokens stop resolving immediately (no need to revoke each).
- Expose the instance over **HTTPS** only (the session/token cookies and bearer tokens
  must not transit cleartext). Behind Railway/a proxy, set
  `ENGRAPHIS_FORWARDED_ALLOW_IPS=*` so the `Secure` flag is applied.
- The agent endpoints are rate-limit candidates for high-write deployments (the trial
  endpoint already rate-limits; mirror that pattern if you expose this publicly).

## MCP-over-HTTP (`/mcp`)

A streamable-HTTP MCP endpoint is mounted at `/mcp` on the dashboard, so an **MCP-native
agent** (Claude Code, Cursor, ...) points one URL at the cloud instance and reuses the same
v2 store the dashboard reads (the MCP tools share the dashboard's single `MemoryService` —
no second SQLite writer). It is Team-gated and member-authenticated exactly like
`/api/remember` (402 without a Team license, 401 without a per-user bearer token).
Browser session cookies are deliberately not accepted on this machine-to-machine endpoint.

Agent config (streamable-http transport) — add to your MCP client:

```json
{
  "engraphis": {
    "url": "https://team.engraphis.com/mcp",
    "headers": { "Authorization": "Bearer <your-token>" }
  }
}
```

The tools are the same as the local `engraphis-mcp` server (`engraphis_remember`,
`engraphis_recall`, `engraphis_start_session`, ...) — an agent gets identical semantics
whether it writes locally or to the cloud.

**Security note:** MCP's built-in DNS-rebinding protection remains enabled. Loopback hosts
remain allowed by default; a hosted deployment must set `ENGRAPHIS_DASHBOARD_URL` to its
canonical public URL (for example, `https://team.engraphis.com`) so that exact Host and
Origin are added to the transport allowlist. Requests with any other Host are rejected.
The per-user bearer token is checked on every request, and dashboard roles carry through to
tools: viewers may use read tools, members may use mutating tools, and
`engraphis_consolidate` requires admin. The standalone `engraphis-mcp-http` launcher keeps
its own SDK defaults.
