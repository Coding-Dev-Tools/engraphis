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
`ENGRAPHIS_FILE_PROTECTION_POLICY` to an absolute operator-controlled file when needed. `protect`
entries are **relative patterns below the root**: an absolute pattern (`/etc/passwd`, `C:\Windows`)
or one that climbs out of the root (`legal/../../secret`) is rejected at load time, because such a
pattern can never match a root-relative target and would silently leave the rule inert.

### What the hook blocks

The evaluator compares a host tool id with separators removed and case folded, so `write_file`,
`write-file` and `Write` name one tool, while a near miss (`write_filex`, `rewrite`) stays a
different tool. The ids it recognizes are:

- **Direct file writes:** `write`, `edit`, `multiedit`, `notebookedit`, `apply_patch`,
  `write_file`, `edit_file`, `create_file`, `delete_file`, `move_file`, `rename_file`, `copy_file`,
  `update_file`, `write_to_file`, `replace_file`, `str_replace_editor`.
- **Shell:** `bash`, `shell`, `terminal`, `execute`, `exec`, `exec_command`, `run_command`,
  `shell_command`, `sh`, `zsh`, `cmd`, `powershell`, `pwsh`, `console_terminal`,
  `run_terminal_cmd`.

The installer writes a matcher that covers the display names (`WRITE`, `EDIT`, `SHELL`), the
Claude-Code-style names (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `ApplyPatch`, `Bash`) **and**
the canonical ids (`write_file`, `edit_file`, `shell_command`), so the guard fires whether the host
matches the matcher against a display name or against the tool id it sends. Re-running the
installer replaces a matcher that an earlier installer wrote; a matcher you customized yourself is
kept.

The shell is blocked entirely whenever any rule has `allow_shell: false`: inspecting a shell
command cannot prove that redirections, subprocesses, or invoked scripts will not mutate a
protected file.

### Failing closed

The hook exits with status 2 (the only status Command Code treats as a denial) for every
failure: a missing `engraphis` install, a missing, malformed, symlinked or unsafe policy, a
payload that is not a JSON object, a payload that is not valid UTF-8, a payload larger than 1 MiB,
a payload nested deeper than 64 levels, and any unexpected error inside the evaluator. A failure
while *reporting* the denial cannot change that status. A missing policy therefore denies every
tool call rather than allowing everything.

### Path matching

Pattern matching is case-insensitive on Windows and on case-insensitive volumes (macOS ships
case-insensitive volumes by default, so `.ENV` would overwrite a protected `.env` there); it is
case-sensitive on a case-sensitive POSIX volume. Names are compared in Unicode NFC form, so the
composed and decomposed spellings of a name match each other. A pattern ending in `/**` protects
both the directory entry and its descendants. Patterns also match targets that do not exist yet.

On Windows, direct file-tool targets that cannot be mapped back to a protected root are denied:
extended-length and device paths (`\\?\`, `\\.\`), UNC and admin-share paths
(`\\localhost\C$\...`), NTFS alternate data streams (`.env::$DATA`), and drive-relative paths
(`C:notes.txt`, which resolves against the current directory *of that drive*). Win32 also strips
trailing dots and spaces from a path component, so `.env ` and `.env.` name the protected `.env`
file; the comparison applies the same normalization. Repositories on network shares therefore need
a different control while the guard is active.

### Self-protection

The policy file cannot be rewritten through a direct file tool, and neither can the host's hook
configuration (`~/.commandcode/settings.json`, or `COMMANDCODE_SETTINGS_PATH` when set): a guarded
process that could edit either one could switch the guard off. The policy file must be a regular
file (a symlinked policy is refused), and on POSIX it must be owned by the current user and must
not be group- or world-writable. Windows ACLs do not map to POSIX mode bits, so on Windows the
ownership of the policy directory is the operator's responsibility; keep the policy in an
owner-only directory.

Set `allow_shell: true` only if a stronger OS/container sandbox independently makes the protected
paths read-only. This option merely allows the shell tool; it does not parse or secure commands.

## Other agent hosts

Call `engraphis.file_guard.load_policy()` and `decide_tool_call()` at the host's tool-authorization
boundary before executing a filesystem mutation. Do not ask the model to call the guard itself:
enforcement must be outside the model-controlled tool sequence. Hosts without a pre-execution hook
must use filesystem ACLs, a read-only bind mount, or another sandbox. Git hooks and CI are useful
detection layers but cannot prevent the initial overwrite.

## Limits

- A process with permission to change ACLs, remount paths, or attach to the host can bypass local
  controls. Run the agent with least privilege.
- Hard-link aliases cannot be recognized from a path pattern alone; use filesystem isolation when
  hostile workspace contents are in scope.
- The decision is made before the host opens the file, so a path that is swapped between the check
  and the write is not detected.
- The bundled hook covers the named Command Code tools. The evaluator allows tool names it does
  not recognize, so adding a new mutating tool to the host matcher alone does not protect it:
  the name must also be recognized by `engraphis.file_guard`.
