# Engraphis 1.1.2 commercial release audit

Date: 2026-07-27

Scope: every local unmerged file, with four parallel review lanes covering licensing,
entitlements and Team authorization, payments and checkout routing, and end-to-end
dashboard/release integration.

## Outcome

The local 1.1.2 candidate has no known release-blocking defect in the public repository.
Local Free behavior remains Apache-licensed and offline-capable. Pro and Team operations
remain private hosted capabilities whose access is granted only by short-lived,
control-plane-scoped credentials; a client-side plan label cannot grant them.

The audit initially found release blockers in remote-server authentication, browser
authentication, entitlement recovery, single-use credential handling, checkout selection,
automation bootstrap, and release enforcement. Those issues are fixed and regression-tested.

## Corrected findings

| Area | Problem | Final behavior |
|---|---|---|
| Remote authentication | The v1 REST app could serve non-loopback peers without an API token. | Remote peers are denied without a token, and non-loopback v1 startup refuses to proceed without authentication. |
| Browser authentication | A configured bearer token made normal dashboard navigation unusable without encouraging unsafe browser storage. | A one-time token exchange establishes a 12-hour signed HttpOnly session cookie; privileged browser requests also require a dedicated session header. The token is never stored by the page. |
| Commercial invariants | Prices and trial terms were not enforced by the always-run repository check. | Free/Pro/Team price types, monthly and annual relationships, trial duration, card policy, and eligible plans are release invariants. |
| Checkout routing | Purchase links did not reliably preserve plan and interval; billing recovery could send a Team customer to Pro checkout. | Pro/Team monthly/annual actions use four exact validated targets. Active and lapsed subscribers use the plan-neutral account portal. |
| Entitlement settlement | Some 401/402/403 outcomes left stale saved or compatibility entitlement state. | Terminal denials settle every local entitlement view; inactive state exposes no paid features. |
| Refresh pressure | Failed entitlement refreshes could retry at dashboard read rate. | Refresh uses bounded exponential backoff from 30 seconds to 15 minutes. |
| Single-use credentials | An ambiguous or truncated refresh response could cause a possibly spent credential to be replayed. | The credential is durably retired and the customer must reconnect; a successful rotation clears the marker. |
| Credential replacement | A durable "spent" marker could also suppress a newly supplied environment bootstrap credential. | The tombstone is keyed to a digest of the exact credential, so a replacement can bootstrap while the spent value stays blocked; no raw credential is persisted. |
| Paid side effects | A denied managed job could generate and lock a snapshot before authorization. | Authorization completes before any snapshot generation, database lock, or generation receipt. |
| Automation bootstrap | Upload success followed by policy-save failure repeated expensive work on the next GET; concurrent first views could duplicate the upload. | Durable phases resume at the failed step, and a per-organization/workspace bootstrap lock serializes same-process first views so only one snapshot is uploaded and followers receive the saved policy version. |
| Capability mapping | Unknown and Team-only capability handling was incomplete. | Every sold capability has an explicit minimum plan; Team UI state follows authoritative entitlement data. |
| Licensing boundary | Hosted-account grace language could be read as gating local MCP/dashboard writes. | Public docs and the manifest explicitly scope grace/recovery restrictions to private hosted-account growth and hosted writes. |
| Credential hygiene | State directories and log redaction had defense-in-depth gaps. | Credential directories are owner-only where POSIX semantics apply, and refresh/token forms are redacted consistently. |
| Dashboard safety | URL control characters, hidden-state CSS, theme contrast, heading order, and duplicate classic handlers had correctness/accessibility gaps. | URL handling fails closed; Ledger and Classic pass their browser, accessibility, theme, mobile, CSP, and commercial-flow assertions. |
| Receipt export | A normal link navigation to the protected receipt export endpoint omitted the browser-session header. | Ledger fetches the filtered export with the authenticated API helper and downloads the returned JSON blob; it never falls back to an unauthenticated navigation. |
| Container API profile | The documented non-loopback Compose API profile could start without an API token even though the server rejects that configuration. | Compose requires `ENGRAPHIS_API_TOKEN` before interpolation and the README documents the required launch command. |
| Release identity | The dirty candidate still declared immutable, already-published version 1.1.0. | The candidate is version 1.1.2 across Python and plugin metadata, with updated plugin asset hashes and changelog. PyPI currently has no 1.1.2 artifacts. |

## Plan and payment contract checked

- Free: local dashboard, MCP tools, local memory operations, and data portability remain
  available without a hosted entitlement.
- Pro: one owner account; purchase actions preserve monthly or annual cadence.
- Team: per named seat; Team-only capabilities require authoritative Team entitlement.
- Hosted denials and billing recovery use the account portal. Purchase and upgrade actions use
  exact checkout targets. Operator URL overrides accept only absolute HTTPS or loopback HTTP.
- Stripe price IDs and payment credentials are intentionally absent from this public client.
  Live charging, refunds, tax, invoices, and webhook fulfillment are private-service concerns.

## Verification evidence

- Python: **1,761 passed, 12 skipped** in the explicit full suite. Every skip is reported and
  accounted for: optional encryption/sqlite-vec extras or platform permission/symlink semantics.
- Commercial, entitlement, licensing, payment-routing, and release-infrastructure subset:
  **467 passed, 1 expected platform skip**. The final cloud-session/dashboard authorization
  regression subset adds **120 passed**.
- Browser E2E: **26 passed**, covering Ledger, Classic, commercial actions, accessibility,
  themes, responsive layouts, and CSP behavior.
- Retrieval: sample and CodeMem recall@5, hit@5, and answer-token recall are all **1.000**.
  PPR multi-hop arm recall@5 is **1.000** versus **0.000** for the expected one-hop ablation.
- Static gates: Ruff, commercial manifest validation, dashboard externalization/CSP validation,
  JavaScript syntax checks, and `git diff --check` pass.
- Packaging: a fresh 1.1.2 wheel and sdist build successfully from this tree and pass Twine,
  release-artifact validation, and an entry-by-entry wheel-to-source digest comparison. They
  include Ledger, Classic, vendor notices, LICENSE, and NOTICE; neither contains databases,
  `_to_delete`, bytecode, or cache directories.
- Immutable publication guard: 1.1.0 correctly conflicts because it is already published;
  1.1.2 verifies as an unpublished candidate with zero existing artifacts.

## External release gates

No live purchase was attempted: that would spend money and requires the private payment stack
and explicit approval. The Docker daemon is not running in this workstation, though the Compose
API profile was validated client-side with a token and verified to reject an unset token.
`pip-audit` is not installed locally; both it and a daemon-backed container run remain encoded
in the CI/release workflows. These are environmental verification limits, not unresolved defects
in the reviewed tree.
