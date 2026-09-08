# Repair discovery and writer occupancy

This incremental change depends on the reliability candidate at
`31da32c06a5ade989608504472e00dfca6f13f94` (PR #203). It changes candidate
discovery for separate vector indexes. Public entrypoints, repair return fields,
ranking defaults, schema 18, and transaction-sharing native indexes are unchanged.

## Reproduced problem and acceptance

Repair prioritizes erasure/quarantine cleanup before vector updates. Previously,
finding one erasure behind 1,000 queued updates acquired SQLite's writer 1,002
times: registration, 1,000 skipped updates, and one deletion. An independent
connection could not finish a write while classification held that reservation.

Discovery now reads 100-row pages containing IDs, generation, canonical existence,
and provenance/metadata. It avoids memory text and vector payloads. Canonical JSON
decoding and quarantine rules remain shared with the store. Each page is fetched
before yielding; no read transaction or reader lease spans publication. The keyset
advances past the last scanned row even if the page yields no matching candidate.
The store's canonical workspace predicate applies to the memory join, with temporal
filtering disabled. Out-of-binding and missing records remain cleanup candidates;
allowed historical records remain indexable. This matches publication's `get_memory`
view without exposing another workspace's metadata during discovery.

Classification is a hint. Publication still reserves the writer, verifies the
selected generation, rereads current canonical existence, eligibility and vector
identity, applies the provider operation, and acknowledges only that generation.
Stale hints leave recoverable debt. Once the provider-attempt budget is spent,
iteration stops before requesting another filtered candidate, including after a
failed deletion. Otherwise the iterator could scan an irrelevant tail after the
last permitted attempt.

`tests/test_vector_repair_discovery.py` exercises real sync/store sequences for:

- Erasure and quarantine after 105 and 1,000 queued updates; one deletion needs at
  most two writer reservations, independent of the stable update backlog.
- An independent writer completing while discovery is deliberately paused.
- Erasure, same-generation quarantine, vector replacement and restoration between
  discovery and publication, without stale publication or lost repair debt.
- Canonical decoding of malformed and legacy metadata/provenance.
- Workspace-bound cleanup, multiple allowed workspaces and retained historical
  canonical records; external cleanup does not erase the canonical memory.
- Early cleanup, both successful and failing, without scanning newer updates after
  the provider budget is exhausted.

Existing sync and storage tests retain coverage of delayed publication, newer
generations during provider callbacks, process/restart recovery and native rollback.

## Reproducible measurement

Run the repair-only probe from the checkout being measured:

```sh
python -m eval.repair_discovery --backlog 1000 --repetition 1 --output repair-1000-1.json
python -m eval.repair_discovery --backlog 10000 --repetition 1 --output repair-10000-1.json
```

For a baseline that predates the driver, run the same driver file with `runpy` from
the baseline checkout. The imported Engraphis package identifies the measured
source; every report records that source's revision and file hashes independently
of the driver hash. Use the same Python/dependencies, machine, storage location and
driver for both sources. Alternate their order across five independent process
repetitions per backlog. Retain every raw result, including failures.
The CLI records failure type and attempted configuration with source/driver identity
and exits nonzero if setup, repair or verification fails; failed work has no success
measurement. An unwritable output location or forced process termination requires
the invoking runner to retain its own exit-status/log record.

The synthetic dataset has fixed 32-dimensional vectors and one erasure after the
declared update backlog. Bulk setup uses canonical store APIs and queue triggers
on a disposable file-backed SQLite database. Setup is excluded from timing. The
measured invocation includes discovery, writer acquisition, synchronous fixture
publication and pending counts. Writer timing includes commit/release overhead;
nested acknowledgement does not acquire or count a second reservation. The same
timing instrumentation applies to both sources.

The adapter makes no network calls. No embedding, recall, tokenizer or answer
generation is measured. This probe does not establish the 100k operating target,
mixed-workload contention, semantic quality or a provider latency guarantee. The
complete-engine protocol and hardware gates remain in
[ENGINE_CAPACITY_PROTOCOL.md](ENGINE_CAPACITY_PROTOCOL.md).

## Compatibility, backout and next dependencies

There is no new migration, policy, service or default. Backout restores the previous
discovery implementation while retaining the canonical database, durable queue and
generation checks. Never delete pending repair work to recover availability.

The following remain separate work:

1. Discovery can scan the whole queue, and repeated calls can repeat that scan.
   Pages bound row count, not metadata bytes or total time. Pending counts also
   traverse the queue. Measure these costs before adding indexed scheduling state.
2. A permanently failing oldest deletion can consume repeated small attempt
   budgets. Durable fairness/backoff and coordination must preserve cleanup priority
   without acknowledging unapplied work or fabricating canonical generations.
3. Provider calls still occupy the writer. Moving an arbitrary provider outside it
   lets a delayed old upsert recreate an erased vector, even if acknowledgement is
   rejected. Hard deadlines need an adapter-level cancellation/fencing contract;
   current tests do not prove remote completion safety after a process dies.
4. Legacy resource imports still perform filesystem/extraction/embedding preparation
   inside a service writer boundary. A prepared batch must preserve whole-batch
   rollback, per-file outcomes, provenance, and caller-owned transactions before
   replacing that path. Removing its transaction decorator alone is insufficient.

An optional background repair worker must coordinate ownership and shut down its
own connections cleanly. The dependency-light offline library continues to work
without one. This change does not introduce background scheduling.
