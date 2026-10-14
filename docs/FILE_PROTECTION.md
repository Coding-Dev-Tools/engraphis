# Protecting files from coding-agent writes

Engraphis memory governance does not, by itself, mediate filesystem access. A coding agent with an
unrestricted shell has the same file authority as its operating-system user. Engraphis therefore
ships a **host-facing, fail-closed policy evaluator** and an optional Command Code `PreToolUse`
hook; it does not claim that memory permissions are a filesystem sandbox.

## Command Code setup

Create an owner-private policy outside the repository:

```json
{
  "version": 1,
  "roots": [
    {
      "path": "/absolute/path/to/repository",
      "protect": [".env", ".github/workflows/**", "legal/**"],
      "allow_shell": false
    }
  ]
}
```

```bash
mkdir -p ~/.engraphis
chmod 700 ~/.engraphis
$EDITOR ~/.engraphis/file-protection.json
chmod 600 ~/.engraphis/file-protection.json
python scripts/install_cc_hook.py --file-guard
```

The default path is `~/.engraphis/file-protection.json`; set
`ENGRAPHIS_FILE_PROTECTION_POLICY` to an absolute operator-controlled file when needed. The hook
blocks matching `Write`, `Edit`, `MultiEdit`, and `NotebookEdit` targets. It also blocks `Bash`
entirely whenever any rule has `allow_shell: false`: inspecting a shell command cannot prove that
redirections, subprocesses, or invoked scripts will not mutate a protected file. A missing,
malformed, symlinked, non-owner-owned, or group/world-writable policy fails closed.

Set `allow_shell: true` only if a stronger OS/container sandbox independently makes the protected
paths read-only. This option merely allows the shell tool; it does not parse or secure commands.

## Other agent hosts

Call `engraphis.file_guard.load_policy()` and `decide_tool_call()` at the host's tool-authorization
boundary before executing a filesystem mutation. Do not ask the model to call the guard itself:
enforcement must be outside the model-controlled tool sequence. Hosts without a pre-execution hook
must use filesystem ACLs, a read-only bind mount, or another sandbox. Git hooks and CI are useful
detection layers but cannot prevent the initial overwrite.

## Limits

- A process with permission to change ACLs, remount paths, edit the hook configuration, or attach to
  the host can bypass local controls. Run the agent with least privilege.
- Hard-link aliases cannot be recognized from a path pattern alone; use filesystem isolation when
  hostile workspace contents are in scope.
- The bundled hook covers the named Command Code tools. Add new mutating tool names to the host
  matcher before enabling them.
