# Put memories in the right workspace

A workspace groups a client, product, or area of work. A repo groups one project inside it.
Memory types (`semantic`, `episodic`, `procedural`, and `working`) describe the kind of memory;
they do not select its workspace.

| Work | Workspace | Repo |
|---|---|---|
| Product development | `acme` | `backend` |
| Work for another client | `client-north` | `website` |
| Research across projects | `research` | Omit for workspace-wide facts |

Reuse stable names. Engraphis matches the `repo` supplied by an agent, not the topic it guesses
from a conversation. Two projects that share the same repo name need distinct names or an
explicit workspace choice.

## Why memories land in `default`

`default` remains the compatibility destination when an agent has no workspace, session, or
saved project mapping. An agent or hook that explicitly sends `workspace="default"` is making
a workspace choice; a saved mapping cannot override it.

The dashboard workspace selector changes the dashboard's context. It does not change the
arguments sent by an already-connected agent. To route new agent work, save a project mapping
or put the selected workspace into that agent's project instructions.

## Set up a project once

1. Open **Connections**, choose the workspace, and enter or select the repo name the agent
   will send in the project selector.
2. Under **Default workspace for this project**, select **Save project routing**.
   **Remove project routing** removes the saved association.
3. Under **Use this workspace in your agent**, select **Copy workspace instructions** and put
   those instructions in the project's instructions file.

The copy button becomes available after the selected project's default is saved. These
instructions follow that saved default; changing it later needs no instruction-file edit.
For workspace-wide work without a project, copied instructions use the selected workspace directly.

The mapping is stored in the same Engraphis database used by the dashboard and MCP server.
Authenticated users have their own mappings; standalone local clients share the local mapping.

For a project that should always use an explicit destination, the essential instruction is:

```text
Use Engraphis workspace="client-north" and repo="website" for this project.
Start a session, retain its session_id, and use that session for recall and remember.
Do not replace the workspace with "default".
```

The recommended project instructions let the saved mapping choose the destination:

```text
For this project, use Engraphis repo="website".
Honor an explicit workspace choice for the current task. Otherwise omit workspace
so Engraphis can resolve the saved project mapping.
Retain the returned session_id and use it for recall and remember during the task.
Check the returned workspace when the session starts.
```

Remove conflicting hardcoded workspace values from global instructions, project instructions,
and hooks. In particular, `workspace="default"` is explicit, not an instruction to use the
saved mapping. Changing a mapping affects future calls that use it; an existing session stays
bound to its original workspace.

## How the destination is resolved

Session starts use an explicit workspace, then a saved repo mapping, then `default`. Routine
remember and recall calls resolve their destination in this order:

1. An explicit `workspace` argument.
2. The workspace of the supplied, authorized `session_id`.
3. The saved mapping for the supplied `repo`.
4. `default` when writing without another destination, or recalling with a repo
   that has no mapping.

An explicit workspace or repo that conflicts with a supplied session is rejected, as is an
unauthorized session. An unknown session cannot supply a destination or accept a write;
an explicitly scoped read with an unknown session returns no memories. It never falls back
to `default`. Omitting workspace does not select some other recently active session: supply
its `session_id` to inherit it.
Without a workspace, repo, or session, local recall retains its broad search behavior.
Authenticated scope and session ownership checks still apply.

For example, after saving `website` → `client-north`:

```text
engraphis_session(action="start", repo="website", goal="Update the contact form")
  → workspace: "client-north", session_id: "ses_..."

engraphis_remember(content="Contact requests use the shared intake API.",
                  session_id="ses_...")
  → workspace: "client-north", repo: "website"

engraphis_recall_context(query="How do contact requests work?", session_id="ses_...")
```

For work without a repo, start with an explicit workspace and keep using its session. Choose
another workspace explicitly when the task changes clients or areas of work; memory type is
not a routing rule.

## Command Code hook

The SessionStart hook uses the nearest Git root's folder name as `repo`, including roots with
a `.git` file such as linked worktrees. Starting in `website/src` therefore uses `website`.
Outside Git it uses the current folder name. Save the mapping under that exact name; a checkout
whose root folder has a different name needs a matching mapping or an explicit override.

With no `ENGRAPHIS_HOOK_WORKSPACE`, the hook omits workspace so the server can apply the mapping.
A nonblank `ENGRAPHIS_HOOK_WORKSPACE` is an explicit override. Clear a previous `default`
override to use project mappings. The recalled-context header names the workspace returned by
the server. The hook remains silent on errors or empty recall results.

## Organize existing memories

Routing changes future writes. Existing memories stay where they are until explicitly moved.
In **Library**, choose **Select memories**, select the records to reorganize, and choose
**Move selected**. In **Move selected memories**, choose a **Destination workspace** and
select **Preview move**. Review the source, destination, selected and related-history totals,
and any blockers before selecting **Move memories**. Confirmation is available only after a
clean preview.

Related records must stay together. The preview can expand a selection to include correction,
promotion, consolidation, and other linked memories, plus complete closed sessions and their
events. Review that expanded list before confirming. A move keeps memory IDs, contents,
historical records, validity history, links, and graph evidence; repo names remain the same inside the
destination workspace. Existing receipts and audit history retain their original workspace,
and the move records a new audit entry. Workspace membership changes in place: time-travel
reads do not recreate a record's former workspace ownership.
Correction and review operation records move with their memories so retries and history ordering
continue to work. The preview shows Personal or Shared access for both workspaces; a shared
destination allows other users with workspace access to read workspace and project memories.

A preview blocks the move when the connected selection:

- Includes an active session, session-owned job/source collection, source-imported document,
  or a code link. Finish the session or use the corresponding source/code workflow first.
- Includes memories that were exported or arrived through sync, or associated tombstones.
- Contains missing or foreign history/graph evidence, or would collide with a keyed claim or
  live graph edge or correction operation ID in the destination.
- Has incoming event references outside the closed sessions being moved. Use a whole-workspace
  operation to preserve that event history.
- Exceeds the 500-memory limit after adding related records.

Use a small, coherent selection for each client or project. A workspace rename, copy, or merge
acts on a whole workspace; it is not a substitute for reviewing a mixed `default` workspace.
The move preview explains records that require another workflow.

## Integrations and troubleshooting

- The dashboard and agent must use the same `ENGRAPHIS_DB_PATH` to share mappings and memories.
- A saved mapping requires the agent to send its `repo`; the MCP server cannot infer a remote
  client's working directory.
- Use the session response's resolved `workspace` to diagnose routing. Check explicit agent
  arguments and hook overrides first if it differs from the saved mapping.
- Saving a mapping does not grant access to a workspace. Normal workspace authorization and
  session ownership checks still apply.
- Pi and Prime Agent can use just `ENGRAPHIS_REPO` with a saved mapping; leave
  `ENGRAPHIS_WORKSPACE` unset to use it. Explicit configured workspaces still take priority.
  Prime Agent keeps the resolved destination for the session and checks the mapping again
  when starting a new session.
- Discovery can find the workspace-list and project-routing actions. Execute the returned
  capability and exact schema rather than inventing action IDs.

For a custom integration, `GET /api/workspace-routing?repo=website` returns
`{repo, workspace, configured, source}`; an unmapped project has `workspace:null` and
`source:"default"`. Save with `POST /api/workspace-routing` and
`{"workspace":"client-north","repo":"website","enabled":true}`. Use `enabled:false` to
remove that association. These endpoints retain the dashboard's normal authorization and
request protection.

Classic clients can use `engraphis_list_workspaces`, `engraphis_get_workspace_routing(repo)`,
and `engraphis_set_workspace_routing(workspace, repo, enabled)`. Smart clients discover these
capabilities and execute the returned read or action schema. Session and remember responses
include `workspace_source` (`explicit`, `project`, or `default`, plus `session` for inherited
remember calls) alongside the actual workspace and repo.

See the [MCP reference](MCP_TOOLS.md) and the skill's
[scoping reference](../skills/engraphis-memory/references/SCOPING.md) for agent-facing details.
