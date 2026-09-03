# Docker Compose deployment

## Start the local dashboard

From a fresh clone, start the Docker Compose deployment with:

```bash
docker compose up
```

The dashboard is available at [http://127.0.0.1:8700](http://127.0.0.1:8700).

## Persistence and loopback port configuration

A fresh clone needs no `.env`: the service runs `engraphis-dashboard --no-open` and stores the v2
database plus the optional customer-side cloud session and non-authoritative entitlement display
cache on a named volume mounted at `/data`. Generic `.env` settings can supply optional runtime
configuration, but Compose deliberately keeps its container bind address and `/data` paths fixed;
that prevents a desktop `ENGRAPHIS_HOST` or `ENGRAPHIS_DB_PATH` from breaking container reachability
or persistence. To use another loopback port, set `ENGRAPHIS_COMPOSE_PORT` in `.env` or the shell:

```dotenv
ENGRAPHIS_COMPOSE_PORT=8787
```

Then open `http://127.0.0.1:8787`. License issuance, trials, leases, and revocations remain on the private control plane.

> Port precedence: the dashboard binds `$PORT` when the platform injects one, falling back
> to `ENGRAPHIS_PORT` (then `8700`). Compose sets both from `ENGRAPHIS_COMPOSE_PORT` so the
> published host port and the in-container bind stay in sync; a stray desktop `ENGRAPHIS_PORT`
> cannot desynchronise them.

## LAN exposure and HTTP MCP

Compose publishes only on loopback by default. To expose it on a LAN, set a strong API token and
the exact URL clients will use:

```dotenv
ENGRAPHIS_API_TOKEN=<a-long-random-secret>
ENGRAPHIS_DASHBOARD_URL=http://<host-LAN-IP>:8700
```

Then start the token-required LAN overlay (Docker Compose v2.24.4+):

```bash
docker compose -f docker-compose.yml -f docker-compose.lan.yml up -d
```

The URL variable alone does not expose or secure the service. The LAN overlay refuses to render
without `ENGRAPHIS_API_TOKEN`; it replaces the loopback port mapping with an all-IPv4-interface mapping.
After this opt-in, other machines on the LAN can use
`http://<host-LAN-IP>:8700`.

The Docker image includes the streamable HTTP MCP endpoint at `/mcp/` (the `/mcp` path redirects
there). Configure `ENGRAPHIS_DASHBOARD_URL` to the exact LAN IP or hostname clients use so MCP's
DNS-rebinding protection accepts the request. For example, use
`http://192.168.10.151:8700` for direct LAN access, or `http://engraphis.local` behind Traefik.
For an HTTP-enabled deployment, use the dashboard port (replace `8700` with your
`ENGRAPHIS_COMPOSE_PORT` value when you override it):

```json
{
  "engraphis": {
    "transport": "http",
    "enabled": true,
    "url": "http://<host-LAN-IP>:8700/mcp/"
  }
}
```

When `ENGRAPHIS_API_TOKEN` is set, configure the client to send
`Authorization: Bearer <ENGRAPHIS_API_TOKEN>`. Remote requests without a token are rejected.
