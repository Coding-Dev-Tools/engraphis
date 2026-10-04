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
| Advisory Jev decisions | Local heuristics; optional BYOK | Included managed decisions after service acceptance | Included managed decisions with a finite pool shared across named seats after service acceptance |
| Hosted Cloud Sync, Analytics, and managed automation | | Yes | Yes |
| Private account and billing support | | Yes | Yes |
| Hosted multi-user dashboard, roles, seats, and audit export | | | Yes |
| Per-user agent and sync tokens | | | Yes |

Start or manage a hosted subscription in the [Engraphis account portal](https://api.engraphis.com/account?plan=pro&interval=monthly&utm_source=engraphis&utm_medium=docs&utm_campaign=pro_conversion&utm_content=hosted_plans_pricing#billing).

## Included System 1 Decision Engine (Jev)

After release acceptance and service enablement, each Pro subscription owner and each active
Team named seat has managed Jev decisions at no additional charge, within a finite rolling
allowance. Team usage is pooled across its active named seats. The account portal reports current
availability and usage. Some admitted requests may count if
they fail or are interrupted; no overage charge is applied. A service protection limit may pause
requests before the included allowance is exhausted.

Managed Jev is currently `not_yet_available` pending release acceptance and service capacity
qualification; client configuration does not enable it. Subscribers do not need a personal
provider key for managed decisions. Direct BYOK is separate and may incur provider charges. No
latency, accuracy, or cost-saving guarantee is established by configuration or a successful
health check.

The managed transport and MCP decision route require client **1.7.9 or newer**.
The published 1.7.8 client has an experimental adapter but does not provide this route.
After 1.7.9 is published, upgrade the Python environment that launches your MCP host
and restart that host. See the [1.7.9 upgrade and release checklist](RELEASE_1_7_9.md).
Installing a new client does not enable a deployment whose managed service is disabled.

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
Managed Jev accepts only the fixed command-review, completion-review, evidence-support,
and contradiction-check operations. It does not perform recall-route selection. The separate
experimental BYOK planner can reorder bounded deterministic query routes before retrieval, but
it cannot write memories or bypass grounded support and abstention checks; current synthetic
fixtures show no retrieval-quality improvement. Local command heuristics never recommend
automatic execution.

The email-confirmed, no-card trial lasts seven active days for Pro and fourteen active days for Team. If hosted entitlement expires,
`workspace_write_grace` can retain only approved hosted-account continuity operations for up to
24 hours. It does not extend a trial or subscription, grant cloud access, or affect the free
local tools. `recovery_read_only` supports hosted account recovery and export after grace.

See [Licensing and commercial service boundary](LICENSING.md) for the full source and service
boundary, and [Cloud Sync](SYNC.md) for the sync security model.
