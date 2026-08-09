# Recall recovery and upgrade health

Engraphis upgrades should not require direct SQLite edits. Schema migration, vector-space
validation, review diagnostics, and governed approval all have operator paths.

## Zero results after an upgrade

Recall responses now distinguish an empty scope from a review gate. When memories exist but none
are approved, the response includes a content-free `eligibility` count and an actionable `note`.
The same counts are available under `prompt_eligibility` in `MemoryService.stats()`.

Inspect and approve local batches with:

```bash
engraphis-cli review list --namespace vault
engraphis-cli review approve --all --namespace vault \
  --reason "verified against the local source"             # dry run
engraphis-cli review approve --all --namespace vault \
  --reason "verified against the local source" --apply
```

Do not edit `provenance` in SQLite. Schema 11 automatically preserves the old explicit-trust
contract and recovers the known historical local-agent downgrade. Unknown and external evidence
stays pending; quarantined evidence is never included in bulk approval.

## Legacy v1 migration

Migrate a flat v1 database through the staged v2 importer:

```bash
python -m scripts.migrate_to_v2 --old engraphis_v1.db --new engraphis_v2.db --dry-run
python -m scripts.migrate_to_v2 --old engraphis_v1.db --new engraphis_v2.db
```

The migration preserves valid legacy data and publishes the new database only after validation.
Legacy SQLite columns are dynamically typed, so malformed numeric, temporal, and vector values are
deterministically repaired or quarantined rather than called lossless. The summary reports
`quarantined` and `repaired_fields` counts; each migrated record retains typed provenance such as
`v1_memory_id`, `v1_thought_id`, or `v1_document_id` when that source identifier exists.

`engraphis-cli delete-namespace NAME --force` is not physical deletion. It closes the current
validity of every live memory in that workspace and records an audited receipt; use governed secure
erase only for the narrower leaked-secret contract and its documented external-copy limitations.

## Irrelevant or identical semantic results

Stored vectors have one authoritative active fingerprint derived from backend identity, model
version, and dimension. Deterministic hashing publishes a stable identity; Sentence Transformers
use the resolved Hub commit or a manifest of local artifacts, and API embeddings require an
operator/provider `space_version` for persistent use. If identity is mutable or unresolved, the
embedder may still serve ephemeral calls but persistent vector recall stays gated. Any
configured-space change, including A → B → A, rebuilds every non-quarantined vector before that
fingerprint becomes active.

The engine commits a rebuild gate before replacing the first vector. Until the rebuild completes,
the vector arm is disabled and recall safely degrades to lexical, graph, and code retrieval.
An interruption leaves the gate in place, so mixed embeddings are never queried.

Check `MemoryService.stats()`:

```json
{
  "embedding": {
    "configured": "emb:v1:...",
    "active": "emb:v1:...",
    "rebuilding": "",
    "ready": true,
    "vectors": 570,
    "current_vectors": 570,
    "stale_vectors": 0
  }
}
```

If `ready` is false, stop other writers and restart Engraphis with the intended embedding
configuration. `MemoryEngine.create()` resumes a full guarded rebuild. A model load or embedding
failure aborts startup and retains the rebuild gate; fix that model configuration and restart.
Do not clear `embedding_state` or rewrite `mem_vectors` manually.

For an explicit repair, `python -m scripts.repair_embed_dim` defaults to
`ENGRAPHIS_DB_PATH`, selects the configured embedding fingerprint and vector backend, takes a
consistent backup when work is needed, and rebuilds through the same governed gate. It never
rewrites rows to an arbitrary observed dimension.

## Provenance audit

New service writes include `writer_policy: service-v11` and an internal `ingress` label such as
`mcp`, `intent_api`, `cli`, or `service`. These fields make a later trust decision attributable
without relying on the caller-controlled `source` label. `trust_origin` remains the authority
decision: local agent sources are approved, external sources are pending, and poisoning matches
are quarantined.
