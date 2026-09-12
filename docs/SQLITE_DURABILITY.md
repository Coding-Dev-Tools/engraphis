# SQLite durability

Writable file-backed v2 stores default to `sqlite_durability="durable"`: WAL journal
mode and `PRAGMA synchronous=FULL`. Every opened writer requests and verifies this
policy before becoming available. The existing migration backup boundary remains:
the persistent WAL setting is applied only after schema initialization completes.
No schema migration or memory rewrite is introduced by selecting a policy.

`sqlite_durability="balanced"` explicitly selects WAL with `synchronous=NORMAL`.
It reduces commit synchronization, but recent acknowledged transactions can be lost
after an operating-system crash or power failure. Both modes retain SQLite's
transaction boundary; selecting balanced does not authorize partial memory writes.
See SQLite's [synchronous contract](https://www.sqlite.org/pragma.html#pragma_synchronous)
and [WAL performance discussion](https://www.sqlite.org/wal.html#performance_considerations).

Set `ENGRAPHIS_SQLITE_DURABILITY=balanced` in the process environment or the trusted
owner-private `~/.engraphis/config.env` to opt the configured service into balanced
mode. Omission selects durable. Unknown or empty values fail validation. An explicit
`MemoryService.create(..., sqlite_durability="durable")` overrides the setting;
`Store`, `engraphis.create_memory_engine` and `MemoryEngine.create` also accept this
keyword and default to durable independently of service configuration. Every process
writing a shared database must use the intended policy: synchronization is per
connection, and opening one durable writer cannot upgrade another writer.

`Store.durability_health()`, service `stats()["sqlite_durability"]` and factory backend
health report the configured policy, current journal/synchronization settings and
whether they match. They contain no database path, tenant identifier or memory
content. Reading diagnostics does not commit a caller-owned transaction, checkpoint
the WAL, or change settings. These observations describe SQLite configuration only.

In-memory databases report `effective="memory"` and provide no persistent guarantee.
Immutable read-only inspection reports `effective="read_only"`; it preserves the
existing file and sidecars and never applies the requested durability pragmas.
Injected connectors still own opening, encryption and their own lifecycle. Writable
injected connections must honor the requested SQLite policy; a file-backed store
fails startup when the effective settings cannot be verified.

The disposable-file regression suite verifies configuration forwarding, inspection
without writes, caller-owned transactions, abrupt subprocess exit and a SQLite
page-limit `SQLITE_FULL` failure followed by recovery. It checks committed memory,
vectors, lexical visibility, operation receipts and database integrity. `os._exit`
skips process cleanup; it is not a power-cut experiment. A page limit exercises
SQLite's database-full path without exhausting the machine's disk; it does not
certify every filesystem or device failure mode.

FULL asks SQLite's VFS to synchronize commits. Actual persistence still depends on
the OS, filesystem, storage device and their truthful synchronization behavior. This
change does not certify hardware power-loss protection, backup restoration, remote
vector publication, or a recovery-time objective. Measure throughput and latency
with the selected policy recorded; do not compare an unlabelled NORMAL baseline to
FULL or weaken the default to meet a benchmark target.
