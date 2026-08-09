# Secure erasure for accidentally captured secrets

Engraphis rejects credential-shaped values at capture time. The block is enforced before
extraction/embedding and again at the SQLite store boundary, so normal `remember`, `ingest`,
event, sync, import, and direct-store memory writes cannot create a new FTS or vector copy of a
secret.

Use `engraphis_retire` (or `POST /api/retire`) for ordinary stale facts. Retirement is temporal:
the record no longer appears in current recall but remains in history, full-text search, and
vector storage for time-travel reads. `engraphis_forget` and `POST /api/forget` are deprecated
compatibility aliases only.

For an already stored credential, use `engraphis_secure_erase`, `POST /api/secure-erase`, or
`MemoryService.secure_erase()`. This is intentionally irreversible. It removes the specified
memory from the main row, FTS and vector tables, memory links, code links, graph evidence, and
unreferenced extracted entities. It removes the record's old audit details, records a
content-free erasure marker, enables SQLite `secure_delete`, checkpoints/truncates the WAL when
SQLite permits it, and runs `VACUUM` to rebuild the live database without free-page/FTS tombstone
content. Recognised local migration and embed-repair SQLite backups are scanned and rewritten too.
For sync, only a non-secret workspace/repo record receives a `remote_erasure` marker; secret,
session, reserved user-scope, and migrated legacy markers are `never_export` and stay local.

This is best-effort physical remediation, not a promise of universal deletion. The result reports
whether WAL/VACUUM maintenance and injected vector-index deletion succeeded. It cannot erase:

- filesystem snapshots, deleted-file recovery sectors, copied/exported databases, or backup
  systems Engraphis cannot identify and open;
- remote sync peers that have not yet accepted an eligible `remote_erasure` marker, cloud
  backups, or logs outside the local database; `never_export` markers never notify peers;
- values already returned to, cached by, or observed by a running/compromised agent.

Always rotate or revoke the credential first. If an injected external vector backend reports a
failed cleanup, erase it through that backend's own control plane before treating the incident as
contained.
