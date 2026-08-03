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
