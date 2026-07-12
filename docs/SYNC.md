# Cloud Sync

Engraphis is local-first: your memory store is a SQLite file on your machine, and
everything works with no account and no network. **Cloud sync** is the Pro feature that
keeps that store consistent across *all* your machines — and, on the Team tier, across a
group — without giving up local-first ownership.

This is the same value proposition that makes sync the one paid feature people reliably
buy from an otherwise-free local app (it's Obsidian's headline paid add-on). The
difference is what happens when two devices change the same knowledge: most tools drop a
`conflict copy` on the floor and make you clean it up. Engraphis already has a
deterministic, bi-temporal conflict resolver at its core, so sync **merges** instead.

---

## Why this belongs in Engraphis specifically

Sync is usually the hard part of a local-first product — reconciling concurrent edits
without a central arbiter is a genuine distributed-systems problem. Engraphis was already
90% of the way there, because the v2 data model was built for exactly this:

- **Globally unique identity.** Every memory id is a ULID (`core/ids.py`) with 80 bits of
  CSPRNG randomness, minted locally. Two offline devices generate ids that never collide,
  so "write now, merge later" needs no coordinating server.
- **Bi-temporal truth.** A "delete" is a `valid_to` timestamp, not a destructive row
  removal (`AGENTS.md` §3.2/§3.3). That means an invalidation is *state you can merge*,
  not an event you can lose.
- **Idempotent writes.** `Store.add_memory` is an `INSERT ... ON CONFLICT(id) DO UPDATE`
  that only fills timestamps when they're null — so re-applying a remote write verbatim is
  safe and repeatable.
- **A deterministic resolver.** `core/resolve.py` already decides ADD / NOOP / INVALIDATE
  from pure signals, no LLM, no network.

Because of this, sync is a thin, **state-based CRDT** over memory rows — not a bespoke
replication log.

---

## Architecture

Two pieces, split along the open-core line:

```
your memory store (SQLite)                        another device's store
        │                                                   │
        ▼                                                   ▼
  SyncEngine.export_bundle ─► full-state JSON snapshot ◄─ SyncEngine.export_bundle
        │                          (per workspace)          │
        │                                                   │
        └────────► SyncTransport (shared folder / relay) ◄──┘
                            │
                            ▼
                  SyncEngine.apply_bundle  ── deterministic merge into local store
```

- **`engraphis/core/sync.py` — `SyncEngine`** (open, Apache-2.0, `numpy`-only, no license
  code). Exports a workspace as a full-state bundle, and applies a remote bundle with a
  convergent merge. This is the reusable engine; it contains **no** gate.
- **`engraphis/core/interfaces.py` — `SyncTransport`** Protocol. Three calls
  (`push`/`pull`/`list_names`) over opaque named byte blobs. Same interface-first swap as
  `VectorIndex`/`Embedder`: the engine doesn't care whether bytes land in a folder or a
  managed relay.
- **`engraphis/backends/sync_folder.py` — `FolderTransport`.** The zero-infrastructure
  transport: any directory both devices can see — a Dropbox / iCloud / OneDrive folder, a
  Syncthing share, a mounted drive, even a git repo. Each device writes one full-state
  bundle (`bundle-<device_id>.json`) and overwrites it each sync, so the folder stays small.
- **`engraphis/backends/sync_relay.py` — `RelayTransport`.** The managed transport: the
  same three `SyncTransport` calls over HTTPS against the vendor relay
  (`engraphis/inspector/sync_relay.py`), carrying the device's license key as a bearer
  token. The relay verifies that key **server-side** and (on Team) holds each device to a
  seat, so patching the local feature check can't unlock sync. Bundles are namespaced by an
  account id derived from the license, so customers never see each other's data.
- **`get_transport(kind, **kw)`** (in `sync_folder.py`) selects between them by name —
  `"folder"` (needs `root=`) or `"relay"` (needs `base_url=` + `workspace_id=`) — the same
  factory pattern as `get_embedder`/`get_vector_index`; `relay` is imported lazily so a
  folder-only install stays dependency-light.
- **`scripts/sync.py` — the CLI** (`--remote <folder>` or `--relay [<url>]`) and the place
  the **Pro gate** lives (`require_feature("sync")`), exactly like `scripts/consolidate.py`
  gates `--report`.

### How the merge converges

For each memory id, both devices compute the same merged record because every field is
resolved by a commutative, associative, idempotent rule:

| Field(s) | Rule | Why |
|---|---|---|
| `valid_to`, `expired_at` | earliest non-null wins | an invalidation on any device sticks everywhere; never resurrected |
| `stability`, `access_count`, `last_access` | `max` | reinforcement is monotone (the spacing effect only grows stability) |
| `pinned` | logical OR | pin on any device = pinned |
| `content`, `title`, `keywords`, … | last-writer-wins by `(last_access, ingested_at, content-hash)` | a deterministic *total order* — the winner depends on the data, never on who synced first |

The content-hash tiebreak is what makes the merge order-independent even when two devices
edited at the same clock instant: `merge(a, b) == merge(b, a)`, and re-applying a bundle
is a no-op. Scope pointers (`workspace_id`/`repo_id`) are **not** merged — they're
per-device ULIDs, so every incoming row is re-homed into the local workspace *by name*
(the same technique `scripts/migrate_to_v2.py` uses), while the globally-stable memory id
carries identity across devices.

### The one honest limitation

Without a per-field logical clock (an HLC), a *simultaneous in-place relabel of the same
field on two devices* resolves by the deterministic order above rather than by true
causality. It always converges — no divergence, no lost row — it may just pick a
well-defined winner a human wouldn't have. In practice this is rare: corrections go
through `MemoryEngine.correct` (a new bi-temporal row, not an edit), so it only touches
raw `title`/`mtype` relabels. A follow-up increment adds an HLC to close this.

---

## Usage

Point two or more devices at one shared folder and sync a workspace:

```bash
# Preview what a sync would change — writes nothing, locally or to the folder
python -m scripts.sync --db engraphis.db --workspace acme --remote ~/Dropbox/engraphis --dry-run

# Sync for real: publish this device's snapshot, pull + merge every other device's
python -m scripts.sync --db engraphis.db --workspace acme --remote ~/Dropbox/engraphis

# Restrict to a single repo
python -m scripts.sync --db engraphis.db --workspace acme --remote ~/Dropbox/engraphis --repo frontend
```

Or use the **managed relay** instead of a shared folder — same command, `--relay` in place
of `--remote`. The relay authenticates with your license key server-side, so no folder to
set up and no way to bypass the gate by patching the client:

```bash
# Point at a relay host explicitly …
python -m scripts.sync --db engraphis.db --workspace acme --relay https://sync.engraphis.app

# … or set ENGRAPHIS_RELAY_URL once and pass a bare --relay
python -m scripts.sync --db engraphis.db --workspace acme --relay
```

The relay is namespaced by workspace **name**, so every device on the account that syncs
workspace `acme` shares one bucket; the license key isolates your account from every other
customer's. `--relay-key` overrides the device's configured license key if needed.

Schedule it like any other local job:

```
# cron — every 15 minutes (folder or --relay, same idea)
*/15 * * * *  cd /path/to/repo && python -m scripts.sync --db engraphis.db --workspace acme --remote ~/Dropbox/engraphis
```

Exactly one of `--remote` / `--relay` is required. Sync is full-state and idempotent, so
running it on any cadence — or interrupting it — is safe. It's a **Pro** feature; the 3-day
local trial (Settings → License, one click, no key) unlocks it for evaluation.

---

## Security model

A pulled bundle is **untrusted input** — `SECURITY.md` treats memory poisoning as an
explicit threat — so `SyncEngine.apply_bundle` is the trust boundary and treats every
bundle as hostile:

- **Scope confinement.** Incoming rows are re-homed into the workspace you're syncing, and
  a row is only ever merged into an existing memory that *already lives in that
  workspace*. A bundle cannot reach across into a workspace (or another repo, with
  `--repo`) the peer wasn't syncing — matching the confinement guarantee every other write
  tool in the codebase enforces.
- **Validation & clamping.** Every field is type-checked and bounded: content/title/
  keyword lengths, metadata size, numeric ranges (`importance`, `stability`, `surprise`,
  `access_count`), and timestamps (clamped to a small clock-skew window so a forged
  `last_access` can't permanently pin poisoned content). Control/ANSI-escape bytes are
  stripped exactly as the rest of the ingest surface does.
- **Fail-safe parsing.** Non-finite JSON (`Infinity`/`NaN`) is rejected, and one malformed
  or hostile bundle is recorded and skipped — it never aborts the whole sync.
- **No secrets on the wire.** Embeddings are never serialized (rebuilt locally);
  `secret`-flagged memories are excluded from export by default; the auth/license database
  is a separate file that is never part of a bundle.
- **Provenance.** Every synced-in memory is tagged `provenance.synced_from_device`, so
  "why is this known?" stays answerable.

**Trust boundary, stated plainly:** within a workspace you *choose* to sync, any peer can
add, relabel, or invalidate memories — that's what sharing a replica means (like any
collaborator in a shared doc), and it's bi-temporal and audited, never a hard delete.
Sync only ever moves data within the scope you pointed it at.

---

## Roadmap

This ships the engine, the self-hostable folder transport, **and** the managed relay
transport (`RelayTransport` + the license-gated server) — real multi-device sync today over
either, fully offline-testable, both reachable from `scripts/sync.py`. Planned next:

1. **End-to-end encryption for the relay** — the relay is already a "dumb" blob store that
   never parses bundle contents, so a client that encrypts in `push` / decrypts in `pull`
   makes it zero-knowledge with **no** server change (the `SyncTransport` contract already
   allows a transport to encrypt). Today's `RelayTransport` sends plaintext-over-TLS; this
   closes the gap so the relay operator can't read bundle contents either.
2. **HLC per-field clock** — precise causal resolution for concurrent in-place edits.
3. **Entity/edge graph sync** — v1 syncs memories + their links; the knowledge graph
   (with cross-device entity reconciliation) comes next.
4. **`engraphis_sync` MCP tool + Inspector "Devices" panel** — sync from inside the agent
   and the UI (each must call `require_feature("sync")` itself — `core/sync.py` has no
   gate by design).
5. **Incremental deltas** — cursor-based "changed since" bundles for very large stores
   (today's full-state snapshot is fine well into tens of thousands of memories).

---

## Positioning vs. Obsidian Sync

Obsidian's paid Sync ($4–8/mo) syncs files and, on conflict, either textually patches them
(and "may create duplicate text or formatting problems," per its own docs) or drops a
conflict-copy file. Across the local-first field — Standard Notes, Syncthing, iCloud —
conflict-copy duplication is the norm; only Anytype does automatic CRDT merge.

Engraphis's wedge is that it's a **memory engine**, not a file syncer: it merges at the
level of individual, bi-temporal facts with a *deterministic* resolver, so a "delete on
laptop, edit on desktop" reconciles into one coherent, auditable history instead of two
files you have to diff by hand. Sync isn't bolted on — it's the same `resolve()`/validity
machinery the engine already runs, pointed across devices.
