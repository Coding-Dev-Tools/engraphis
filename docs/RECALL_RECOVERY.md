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

## Irrelevant or identical semantic results

Stored vectors have one authoritative active fingerprint derived from backend identity, model
version, and dimension. Sentence Transformers, deterministic hashing, and API embeddings publish
durable identities. Any configured-space change, including A -> B -> A, rebuilds every
non-quarantined vector before that fingerprint becomes active.

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

## Provenance audit

New service writes include `writer_policy: service-v11` and an internal `ingress` label such as
`mcp`, `intent_api`, `cli`, or `service`. These fields make a later trust decision attributable
without relying on the caller-controlled `source` label. `trust_origin` remains the authority
decision: local agent sources are approved, external sources are pending, and poisoning matches
are quarantined.
