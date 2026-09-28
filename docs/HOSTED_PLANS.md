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
| System 1 Decision Gating (Jev) | Free offline heuristics or BYOK | Included (Managed cloud proxy) | Included (Pooled team quota) |
| Hosted Cloud Sync, Analytics, and managed automation | | Yes | Yes |
| Priority support | | Yes | Yes |
| Hosted multi-user dashboard, roles, seats, and audit export | | | Yes |
| Per-user agent and sync tokens | | | Yes |

Start or manage a hosted subscription in the [Engraphis account portal](https://api.engraphis.com/account?plan=pro&interval=monthly&utm_source=engraphis&utm_medium=docs&utm_campaign=pro_conversion&utm_content=hosted_plans_pricing#billing).

## Included System 1 Decision Engine (Jev)

Pro and Team subscriptions include access to managed **System 1 decision gating powered by Jev (TypeSafe AI)** through the Engraphis Cloud proxy (`POST /v1/jev/decide`). This provides sub-300ms, zero-token-waste micro-decisions for agent tool guardrails, context pruning, contradiction resolution, and hallucination screening at no additional cost. Free and offline installations retain full access to deterministic local heuristics and optional Bring-Your-Own-Key (BYOK) operation without cloud connectivity.


The email-confirmed, no-card trial lasts three active days for Pro and ten active days for Team. If hosted entitlement expires,
`workspace_write_grace` can retain only approved hosted-account continuity operations for up to
24 hours. It does not extend a trial or subscription, grant cloud access, or affect the free
local tools. `recovery_read_only` supports hosted account recovery and export after grace.

See [Licensing and commercial service boundary](LICENSING.md) for the full source and service
boundary, and [Cloud Sync](SYNC.md) for the sync security model.
