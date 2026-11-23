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
| Included OpenRouter AI LLM | Optional BYOK | Free managed AI LLM: up to 100 calls per person per day (active paid and trial), auto-enabled on connect | Free managed AI LLM: up to 100 calls per person per day per named seat (active paid and trial); no shared pool |
| Advisory Jev decisions | Local heuristics; optional BYOK | Free managed Jev: up to 250 calls per person per day for eligible individuals (active paid and trial) | Free managed Jev: up to 250 calls per person per day for eligible named seats (active paid and trial); no shared pool |
| Hosted Cloud Sync, Analytics, and managed automation | | Yes | Yes |
| Private account and billing support | | Yes | Yes |
| Team account roster, roles, named seats, invitations, and activity | | | Yes |
| Per-user agent and sync tokens | | | Yes |

Team activity covers selected account events and is visible to current owners and admins.
Shared hosted Team memory and hosted MCP are currently `not_yet_available`.

Start or manage a hosted subscription in the [Engraphis account portal](https://api.engraphis.com/account?plan=pro&interval=monthly&utm_source=engraphis&utm_medium=docs&utm_campaign=pro_conversion&utm_content=hosted_plans_pricing#billing).

Managed Automation schedules consolidation and dreaming proposals. Results require
review and never automatically rewrite the local database; local governance tools
apply the changes you choose.

## Included OpenRouter AI LLM (100 calls/day)

Every legitimate Pro user and each eligible Team named seat (including paid viewers) with an active paid or trial entitlement automatically receives up to **100 AI LLM calls per person per day**, free, backed by Engraphis Cloud's zero-cost OpenRouter `:free` tier (`max_price = 0`). No personal OpenRouter API key is needed and no overage is charged.

Once you connect your client with `engraphis connect --token <CONNECT_TOKEN>`, your Engraphis runtime automatically uses this included LLM for tangible day-to-day improvements:

- **Automatic structured fact and entity extraction:** Ingested session notes and documents are distilled into clean atomic facts, entities, keywords, and knowledge-graph relations (`llm_structured`) so later recalls are sharper and cleaner without manual tagging.
- **Cited proactive briefings and grounded Q&A:** `engraphis_proactive_context(synthesize=True)` and `engraphis_grounded_recall(synthesize=True)` produce concise, citation-verified summaries directly from your memories.
- **Structured sleep-time consolidation and thoughts:** `engraphis_consolidate(structured=True)` and thought synthesis produce higher-signal semantic digests.
- **Safe fallback:** If you are offline, have reached the 100/day rolling cap, or explicitly configure `ENGRAPHIS_EXTRACTOR=none` or your own BYOK key, Engraphis gracefully uses local deterministic processing or your chosen provider with zero lost writes.

## Included System 1 Decision Engine (Jev)

Every legitimate Pro user and each
eligible Team named seat, including paid viewers, with an active paid, trial, or test
entitlement receives managed Jev at no additional customer charge and without a personal
provider key.

Each paid individual receives up to **250 Jev calls per person per day**, free. Usage is per person, not pooled across a Team and not monthly. Each evaluated call counts once, including a batched request; calls beyond the daily maximum are not served. Admitted attempts that fail or are interrupted remain counted, and no overage is charged. The account portal reports that member's remaining calls for the current day.

Managed Jev is available for paid plans; the hosted control service enables it with
its provider key. Direct BYOK is separate and may incur provider charges.
Configuration or a successful health check does not establish latency, accuracy, or
cost savings.

The managed transport and MCP decision route require client **1.7.9 or newer**.
The published 1.7.8 client has an experimental adapter but does not provide this route.
Version 1.7.9 was published on October 1, 2026. Upgrade the Python environment that launches your MCP host
and restart that host. See the [1.7.9 upgrade and release checklist](RELEASE_1_7_9.md).
Installing a new client does not enable a deployment whose managed service is disabled; the hosted control service enables it with its provider key.

Once you connect a paid or trial Pro/Team account with `engraphis connect`, both managed Jev (up
to 250 calls per person per day) and included OpenRouter AI LLM (up to 100 calls per person per
day) activate automatically when `ENGRAPHIS_DECISION_BACKEND` and `ENGRAPHIS_EXTRACTOR` are unset.
To pin managed routing explicitly in `~/.engraphis/config.env` and verify both live allowances in
one non-interactive command:

```bash
engraphis connect --token <CONNECT_TOKEN> --enable-managed-jev --enable-managed-llm --verify
```

The client refreshes its saved session and sends only to that session's
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
