# Import an Obsidian vault

Obsidian is Engraphis’s rich Markdown adapter inside the universal local-document
import architecture. Use the source-neutral [document import guide](DOCUMENT_IMPORT.md)
for the shared safety, privacy, resume, history, conflict, dashboard, and supported-format
contract. This guide covers what the Obsidian adapter adds: frontmatter, aliases, tags,
wikilinks, block/heading fragments, and attachment references.

The source Markdown remains your canonical data; Engraphis stores normal readable memory
records and derives embeddings, full-text search, and graph indexes from them. The importer
makes no network requests.

## Quick start

Preview an import before opening or changing the target database:

```bash
engraphis import obsidian /path/to/vault --dry-run
```

After reviewing the preview, confirm an unattended import explicitly:

```bash
engraphis import obsidian /path/to/vault --workspace acme --yes
```

Choose the existing target scope explicitly when it is not supplied by the
active session:

```bash
engraphis import obsidian /path/to/vault --workspace acme --repo product \
  --session ses_01EXAMPLE --scope session --yes
```

`engraphis-import obsidian ...` is an equivalent installed console entrypoint.
Unattended and JSON-mode writes require `--yes`; an interactive terminal asks for
confirmation. An unattended preview must also name `--workspace`. Use `--db` to
select a database other than the configured v2 database.

The dashboard offers the same flow through **Import local documents**: select **Obsidian vault**
as the import mode, select the workspace/repository/session target, review the preview, then
start the import. The preview is strict: it performs zero database writes.
It shows discovered Markdown files, folders, tags, aliases, links, attachments,
warnings, and the files classified as new, changed, unchanged, skipped, or
rejected.

A new browser vault requires a nonblank source label; folder selection prefills its root folder
name. Select the saved source identity when resuming or re-importing that vault.

The trusted dashboard wizard uses the existing owner browser-session and CSRF
confirmation boundary, so the local dashboard must have `ENGRAPHIS_API_TOKEN`
configured. In zero-token loopback mode, use the CLI importer instead.

## What is imported

Markdown is read recursively and retains its relative vault path, original
title, readable Markdown body, YAML frontmatter metadata, aliases, tags,
headings, common date fields, and source identity. Wikilinks such as
`[[Note]]`, `[[Note|label]]`, heading/block fragments, and `![[embed]]` are
discovered for graph linking. Referenced attachments are catalogued, not copied
into a hidden second vault. Fenced and inline code are retained in the note body
but ignored when discovering links, tags, and attachments.

The import target follows Engraphis's existing scope hierarchy; vault folders
are source metadata, not invented scopes. Imported notes use the normal memory
write path and its normal indexing behavior.

## Privacy and filesystem safety

The shared source-neutral safety and privacy contract is maintained in the
[document import guide](DOCUMENT_IMPORT.md). The Obsidian-specific exclusions below are in
addition to that contract.

Only files below the selected vault are considered. The importer does not follow
symlinks, skips hidden/VCS/configuration paths (including `.obsidian`), and
rejects common sensitive filenames and note contents that look like credentials
or private keys. It never prints secret-like contents in reports. Unsupported,
malformed, unreadable, or oversized files are reported per file and do not stop
a vault import. Invalid UTF-8 is replaced explicitly and reported as a warning.

Review the dry-run report before importing a vault that contains personal or
work-sensitive material. The importer does not upload vault data or make API
calls.

## Re-import, history, and recovery

Each source file has a stable identity based on the local vault identity and its
relative path, plus recorded content hashes and importer version. A later run
skips unchanged notes, imports new notes, identifies changed and renamed notes,
and reports deleted source files. Deleted files never trigger automatic hard
deletion of Engraphis memories.

Changed notes preserve Engraphis's temporal history rather than silently
overwriting it. When an existing target has a conflict, choose an explicit
conflict option in the CLI or dashboard to replace according to temporal rules
or create a distinct memory. The default is non-destructive reporting.

Progress is recorded per note. If an import is interrupted, run the same command
again to resume safely; completed, unchanged source records are not duplicated.
The final report includes counts and paths for imported, updated, skipped,
rejected, conflicts, and warnings.

The deprecated `python -m scripts.seed_from_obsidian` command remains available
for old local automation. Its `--namespace NAME` option maps directly to
`--workspace NAME`, and invoking the historical write command counts as explicit
local confirmation. Its `--limit N` option processes at most N notes and leaves a
resumable partial run; rerun without `--limit` to finish link reconciliation and
the missing-source report. The primary importer accepts the same compatibility
option, but a dry run always previews the entire vault because it processes no
notes.

Exit status is `0` for a completed import or clean preview, `2` for invalid input,
`3` for a partial/conflicted/limit-paused run, and `130` for an operator cancellation.

## Current limitations

The frontmatter reader intentionally supports common top-level scalar and list
forms, not every YAML feature. Obsidian plugin-specific syntax (Dataview,
Canvas, queries, templates, and executable/plugin content) is not interpreted.
Attachments are referenced but not copied or OCRed; unresolved links remain
unresolved until a matching note is present. The importer preserves Markdown,
but does not attempt to reproduce Obsidian's rendered/transclusion behavior.
Automatic rename detection is limited to unique exact-content matches.
