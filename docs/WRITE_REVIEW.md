# Public write review gate

## MCP, REST, imports, and sync

Every public write enters review as `pending`, regardless of a caller-supplied `source` or
`trusted` label. That includes MCP, dashboard/REST intent writes, imports, sync, and extractor
output. Detector matches are instead `quarantined` immediately. Pending and quarantined records
remain inspectable and auditable, but cannot enter model-ready recall/context, resolution,
links, graph/code backfill, derived prompt context, or public `why`/`timeline` history.
Corrections, promotions, and merges fail closed unless every input is explicitly approved.

Approval creates a fresh `approved` successor and preserves the reviewed source plus an audit
link; it never relabels the source in place. There is deliberately no MCP tool or general REST
approval endpoint. A local owner can approve through the dashboard's **Approve for prompt**
action after configuring `ENGRAPHIS_API_TOKEN` (short-lived browser session plus CSRF confirmation),
or from an interactive terminal:

```bash
python -m scripts.approve_memory mem_... --reason "verified against the owner runbook"
```

The command rejects redirected input and requires typing its displayed confirmation. Hosted
owner/admin approval is performed by the hosted service, not this local package. The direct
in-process `MemoryEngine` remains a documented trusted-code boundary for code that already has
local database authority; do not expose it to untrusted transports. Existing stores can be
inspected without writes, then migrated deliberately:

```bash
python -m scripts.rescan_poisoning --db engraphis.db
python -m scripts.rescan_poisoning --db engraphis.db --apply
```

The dry run opens the database read-only. The applying pass demotes historical non-approved
records to pending review, quarantines detected payloads, retires their derived bridges, and
records an audit event.
