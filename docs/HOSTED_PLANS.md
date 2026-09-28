# Local and hosted plans

## Local, free software

The local memory engine, dashboard, MCP server, and manual consolidation are Apache-2.0 and free.
They run on your machine and do not require a cloud account.

## Hosted services

Pro and Team subscriptions provide access to Engraphis hosted services. The private control plane
runs sync, analytics, automation, billing, account management, and Team identity. Those server
implementations are not part of this repository.

| | Free | Pro: $10/month or $100/year | Team: $20/seat/month or $200/seat/year |
|---|---|---|---|
| Local dashboard, memory engine, and MCP tools | Yes | Yes | Yes |
| Local version history, graph, and manual consolidation | Yes | Yes | Yes |
| Local workspace export | Yes | Yes | Yes |
| Advisory Jev decisions | Local heuristics; optional BYOK | Included managed allowance when enabled | Included pooled allowance when enabled |
| Hosted Cloud Sync, Analytics, and managed automation | | Yes | Yes |
| Private account and billing support | | Yes | Yes |
| Hosted multi-user dashboard, roles, seats, and audit export | | | Yes |
| Per-user agent and sync tokens | | | Yes |

Start or manage a hosted subscription in the [Engraphis account portal](https://api.engraphis.com/account?plan=pro&interval=monthly&utm_source=engraphis&utm_medium=docs&utm_campaign=pro_conversion&utm_content=hosted_plans_pricing#billing).

## Included System 1 Decision Engine (Jev)

Pro and Team include a managed allowance for advisory typed decisions through the private
Cloud service (`POST /v1/jev/decide`). This client implementation does not establish that a
particular deployment has enabled Jev; the service checks current entitlement and allowance.
Usage and availability are reported by the account portal. No latency, accuracy, or cost-saving
guarantee is established by client configuration or a successful health check.

Set `ENGRAPHIS_DECISION_BACKEND=managed` and connect the installation through the ordinary
Cloud account flow. The client refreshes its saved session and sends only to that session's
bound control origin. `auto` chooses this managed route when configured; it never silently
switches to a personal TypeSafe key. Direct `byok` is an explicit alternative and may incur
charges from TypeSafe. Legacy `typesafe`, `jev`, and `system1` selectors mean BYOK.

The default `none` and `local` selectors keep decisions local. Every remote MCP call also
requires `allow_remote=true` and `data_classification="public"` or `"internal"`; secret
content is rejected. This is permission for the supplied text only, not a standing permission
to upload memory. `offline_mode=true` always prevents remote requests. Known secret patterns
are filtered before credential refresh; this does not guarantee arbitrary prose is secret-free.

The model is pinned to `jev-1.13.0`. Choice/score confidence is provider-supplied; Noul
confidence is explicitly labelled derived decisiveness, not measured calibration. Uncertain,
malformed, unavailable and fallback results remain distinguishable. Local heuristic confidence
is unmeasured. All decisions are advisory: deterministic authorization, memory governance,
executable checks and the user's approval remain authoritative.

The email-confirmed, no-card trial lasts seven active days for Pro and fourteen active days for Team. If hosted entitlement expires,
`workspace_write_grace` can retain only approved hosted-account continuity operations for up to
24 hours. It does not extend a trial or subscription, grant cloud access, or affect the free
local tools. `recovery_read_only` supports hosted account recovery and export after grace.

See [Licensing and commercial service boundary](LICENSING.md) for the full source and service
boundary, and [Cloud Sync](SYNC.md) for the sync security model.
