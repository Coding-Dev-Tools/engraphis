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
| Advisory Jev decisions | Local heuristics; optional BYOK | Included individual allowance after service acceptance | Included allowance per named seat after service acceptance |
| Hosted Cloud Sync, Analytics, and managed automation | | Yes | Yes |
| Private account and billing support | | Yes | Yes |
| Hosted multi-user dashboard, roles, seats, and audit export | | | Yes |
| Per-user agent and sync tokens | | | Yes |

Start or manage a hosted subscription in the [Engraphis account portal](https://api.engraphis.com/account?plan=pro&interval=monthly&utm_source=engraphis&utm_medium=docs&utm_campaign=pro_conversion&utm_content=hosted_plans_pricing#billing).

## Included System 1 Decision Engine (Jev)

After release acceptance and service enablement, every legitimate paid Pro user and every
paid Team named seat, including a viewer seat, includes managed Jev at no additional charge
and without a personal provider API key. Active legitimate trial/test entitlements receive
the same limits. Every individual has 100 evaluated questions per rolling hour, 1,000 per rolling five hours, and 2,000 per rolling 24 hours. Team usage is per seat, not pooled. Each individual has all three rolling caps:

| Rolling window | Maximum evaluated questions per individual |
|---|---|
| 1 hour | 100 |
| 5 hours | 1,000 |
| 24 hours | 2,000 |

Each evaluated question counts as one use, including questions in a batch. The command
workflow evaluates two questions and consumes two uses; the other three workflows each
evaluate one. All three caps are enforced, including the five-hour cap. Usage follows the
individual member rather than a device or seat allocation: reassigning a seat does not reset
that member's recent usage. There is no monthly allowance, organization pool, or unlimited
access. Revocation or expiry prevents further managed access. The account portal reports
the signed-in member's individual window usage, next release times and availability; capacity returns as admissions age out of each
window rather than at a calendar reset.

Admission reserves question usage atomically before a provider request. Admitted failed,
interrupted, or subsequently revoked requests remain counted; requests rejected before
admission do not consume an individual use. No overage charge is applied. The production fleet guard remains
**100 questions/day** across the service. That guard is an unresolved launch conflict with
the individual allowances and can pause service before a user's caps are reached.
The service is currently `not_yet_available` pending release acceptance and service capacity qualification; client configuration
does not enable it. Launch also requires provider-terms review, live acceptance and quality
evaluation. Deterministic fixtures validate integration and fallback behavior, not model
accuracy; no latency, accuracy, or cost-saving guarantee follows from configuration or a
successful health check.

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

Managed access is limited to the following concrete Engraphis workflows. The client and
service validate the required context and fixed question schema, rather than trusting a
purpose label alone:

| Workflow | Required context | Fixed evaluation |
|---|---|---|
| `guard_command` | Command text | Destructive-loss/secret-leak safety and operation category |
| `classify_contradiction` | Existing memory and candidate fact | Supersedes, reinforces, or orthogonal |
| `verify_support` | Query and supplied evidence | Whether the evidence directly supports the query |
| `verify_completion` | Goal and supplied output; optional recent actions | Whether the evidence establishes the goal |

The context remains caller-supplied: schema validation cannot establish its truth or provenance.
Managed `custom` and `query_planning` requests fail closed before credential refresh or network
requests. Experimental recall route planning and custom remote questions require explicit BYOK;
managed failure never selects BYOK automatically. Classic/direct advisory decisions are available
to authorized viewers without granting memory writes or administration. The generic Smart
`engraphis_execute_action` gateway still requires admin because it can execute stateful actions;
the private hosted Team tool catalog is unchanged.

The managed transport accepts an optional opaque `request_key` (16–64 ASCII letters, digits,
underscores or hyphens) and generates a new key when it is omitted. A caller can preserve the
key for an explicit retry after a lost reply. A duplicate admitted key for the same organization
and member receives a 409 outcome without another provider request or question increment,
including when the supplied content changes. No answer is stored for replay and the transport
does not automatically retry. A new key is a new admission and can consume another use.
`request_key` is a transport parameter, not an MCP or dashboard field.

Existing clients can continue sending the legacy managed envelope for these four canonical
workflows and fixed questions; the service also accepts its typed operation envelope. Legacy
requests without a key retain their original single-call behavior and lack retry deduplication.
Legacy arbitrary custom payloads are no longer admitted by managed access; choose local behavior
or explicit BYOK if that separate capability is intended.

The model is pinned to `jev-1.13.0`. Choice/score confidence is provider-supplied; Noul
confidence is explicitly labelled derived decisiveness, not measured calibration. Uncertain,
malformed, unavailable and fallback results remain distinguishable. Local heuristic confidence
is unmeasured. All decisions are advisory: deterministic authorization, memory governance,
executable checks and the user's approval remain authoritative.
The advisory adapter is not connected to core memory writes or grounded recall.
Local command heuristics never recommend automatic execution.

The email-confirmed, no-card trial lasts seven active days for Pro and fourteen active days for Team. If hosted entitlement expires,
`workspace_write_grace` can retain only approved hosted-account continuity operations for up to
24 hours. It does not extend a trial or subscription, grant cloud access, or affect the free
local tools. `recovery_read_only` supports hosted account recovery and export after grace.

See [Licensing and commercial service boundary](LICENSING.md) for the full source and service
boundary, and [Cloud Sync](SYNC.md) for the sync security model.
