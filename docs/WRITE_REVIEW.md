# Memory write trust model

## MCP, REST, imports, and sync

Normal local-agent memory creation is immediate. The `agent` and `intent_api` service sources are
stamped `trusted` + `approved` after normal validation, so an agent can create and recall a memory
without waiting for an owner. The write is still scoped, audited, deduplicated, and subject to the
deterministic poisoning guard.

External/imported sources (`web`, `import`, `sync`, `tool`, `api`, `mcp`, and extractor/introspector
feeds) remain `pending`; detector matches are `quarantined` immediately. Pending and quarantined
records remain inspectable and auditable, but cannot enter model-ready recall/context, resolution,
links, graph/code backfill, derived prompt context, or public `why`/`timeline` history.
Corrections, promotions, and merges fail closed when their inputs are untrusted or quarantined.

Approval is only for releasing external evidence. It creates a fresh `approved`
successor and preserves the reviewed source plus an audit link; it never relabels the source in
place. There is deliberately no MCP tool or general REST approval endpoint. A local owner can
approve through the dashboard's **Approve for prompt** action after configuring
`ENGRAPHIS_API_TOKEN` (short-lived browser session plus CSRF confirmation), or from an interactive
terminal:

```bash
python -m scripts.approve_memory mem_... --reason "verified against the owner runbook"
```

The command rejects redirected input and requires typing its displayed confirmation. Hosted
owner/admin approval is performed by the hosted service, not this local package. The direct
in-process `MemoryEngine` remains a documented trusted-code boundary for code that already has
local database authority; do not expose it to untrusted transports.

For a local batch, use the content-free review CLI. Listing never prints memory bodies, approval
is a dry run unless `--apply` is supplied, and one typed confirmation covers the selected batch:

```bash
engraphis-cli review list --namespace vault
engraphis-cli review approve --all --namespace vault \
  --reason "verified local import"                         # dry run
engraphis-cli review approve --all --namespace vault \
  --reason "verified local import" --apply                 # typed confirmation
```

Use `--source web` (repeatable), `--repo NAME`, explicit `mem_...` ids, or
`--legacy-agent-only` to narrow the batch. `--yes` is available to an already-authorized local
automation. Bulk approval always excludes quarantined, retired, future-dated, and already-approved
records. Quarantined evidence requires individual inspection and is not eligible for the approval
primitive; preserve it for audit or create a separately governed replacement.

## Upgrade classification

Schema 11 automatically classifies rows created before explicit review state existed:

- a non-quarantined row carrying the old explicit `trusted: true` authority receives the
  equivalent `review_state: approved` stamp;
- the exact historical local-agent downgrade signature (`agent`/`intent_api`,
  `service_review_gate`, `trust_downgraded: true`) is recovered as approved;
- every ambiguous or external row remains pending.

The migration updates both provenance copies, records per-row and summary audit entries, is
idempotent, and runs behind the normal verified pre-migration backup. It does not approve
quarantined content.

The poisoning rescan is a separate security operation:

```bash
python -m scripts.rescan_poisoning --db engraphis.db
python -m scripts.rescan_poisoning --db engraphis.db --apply
```

The dry run opens the database read-only. The applying pass demotes historical non-approved
records to pending review, quarantines detected payloads, retires their derived bridges, and
records an audit event. It never bulk-approves records; use `engraphis-cli review` for that.
