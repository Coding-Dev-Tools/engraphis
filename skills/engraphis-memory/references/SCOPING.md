# Scoping — `workspace → repo → session → memory`

Scoping is the highest-leverage decision in Engraphis. Every write sets a scope; every read is
filtered by one. Get it right and memories surface exactly when useful; get it wrong and they
either leak everywhere or never come back.

## Two orthogonal axes — don't conflate them

| Axis | Question it answers | Values | Set by |
|---|---|---|---|
| **scope** | *Who/where can see this?* | `session` · `repo` · `workspace` · `user` | `scope=` on `remember` |
| **type** (`mtype`) | *What kind of thing is this?* | `working` · `episodic` · `semantic` · `procedural` | `mtype=` on `remember` |

A convention is `mtype="semantic"` and probably `scope="repo"`. A user's editor preference is
`mtype="semantic"` but `scope="user"`. Same type, different visibility. Type is covered in
[CONVENTIONS.md](CONVENTIONS.md); this file is about scope.

## The hierarchy

```
workspace            org or product        ("acme")           — always required on a write
  └─ repo            a repository          ("backend")        — omit only for workspace-wide facts
       └─ session    one unit of work      (session_id)       — from engraphis_start_session
            └─ memory                                          — the fact itself
```

Names are **stable identifiers**, not prose. Reuse the exact same `workspace`/`repo` strings every
time — recall filters match on them literally. Pick the repository's canonical name for `repo`
(what you'd `git clone`), and a durable org/product name for `workspace`.

## What each scope means

- **`session`** — visible only within one session. Transient working state ("currently editing the
  auth refactor on branch X"). Ends with the session.
- **`repo`** — the default, and the right answer most of the time. Facts true for one repository:
  conventions, decisions, bug fixes. Requires a `repo`.
- **`workspace`** — true across every repo in the org/product: shared standards, cross-repo
  architecture, team norms. Set `repo=None`.
- **`user`** — follows the human across everything: their preferences and working style, regardless
  of workspace or repo.

## Choose the narrowest scope that stays reusable

Ask: *where would I want this to resurface?* Then scope there — no wider.

- A fix for a quirk in `backend` only → `scope="repo"`.
- "The whole org uses trunk-based dev" → `scope="workspace"`.
- "This developer prefers tabs, hates mocks" → `scope="user"`.
- "I'm mid-way through step 3 of this task" → `scope="session"` (or just an `open_thread`).

Over-scoping (everything `workspace`) pollutes recall in unrelated repos. Under-scoping (everything
`session`) means nothing survives the task. When unsure between `repo` and `workspace`, start at
`repo` — promoting later is cheap; retracting a leaked fact is not.

## Sessions and handoff

A session groups a task's memories and enables resume:

1. `engraphis_start_session(workspace, repo, agent, goal)` → returns `session_id` and a `bootstrap`
   carrying the previous session's `summary` + `open_threads` for this repo.
2. Pass `session_id` to `engraphis_remember` / `engraphis_record_event` during the task.
3. `engraphis_end_session(session_id, summary, outcome, open_threads)` — `open_threads` are the
   unresolved items; they auto-surface when the next session in this repo starts.

Use sessions for any multi-step task. `open_threads` is how the next agent avoids re-discovering
where you stopped.

## Promotion (widening scope)

A learning often starts narrow (a session observation) and proves durable. There is no separate
"promote" call in the current tool set — **promotion is re-`remember` at the wider scope**:

- Session note that turns out to be a real repo convention → `remember(..., scope="repo")`.
  Dedup will reconcile it with anything similar already stored.
- Repo fact that turns out to hold org-wide → `remember(..., scope="workspace", repo=None)`.

Automatic consolidation (episodic→semantic distillation, auto-promotion) is a planned engine
feature, not something the agent should assume happens — promote deliberately when you notice it.

## Reads are scoped too

`engraphis_recall` / `why` / `timeline` take `workspace` (and optional `repo`) and only search
within them. A `repo` filter requires a `workspace`. If you recall with no results and a `note`
that the workspace/repo is unknown, you simply haven't written there yet — create memories first
(any write auto-creates the workspace/repo).
