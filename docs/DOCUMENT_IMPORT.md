# Import local documents

Engraphis imports local document collections into the v2 memory engine without uploading the
source. The dependency-free parser describes readable source material; the normal memory write
path then stores scoped, temporal memories and derives local search indexes. A source folder is
provenance, never an implicit `workspace`, `repo`, or `session` scope.

Obsidian is a rich Markdown adapter within this architecture. Use
[the Obsidian adapter guide](OBSIDIAN_IMPORT.md) when frontmatter, aliases, wikilinks, block or
heading fragments, and attachment references matter.

## CLI

Preview is strict and creates no database records:

```bash
engraphis import documents /path/to/collection --workspace acme --dry-run
```

After reviewing the preview, explicitly confirm the trusted-local write:

```bash
engraphis import documents /path/to/collection --workspace acme --repo product --yes
```

Choose a session target when needed, or reuse an existing source collection identity:

```bash
engraphis import documents /path/to/collection --workspace acme --repo product \
  --session ses_01EXAMPLE --scope session --source-id vlt_01EXAMPLE --yes
```

`engraphis-import documents ...` is the equivalent installed console entrypoint. Non-interactive
and JSON-mode writes require `--yes`; an interactive terminal asks for confirmation. Use `--db`
to select another v2 database, `--source-label` for a new collection’s display name, and
`--on-conflict error|replace|new` to choose the divergent-lineage policy. `--limit N` pauses a
write after N documents and leaves it resumable; a dry run always previews the whole collection.

CLI imports never download an embedding model. The configured `ENGRAPHIS_EMBED_MODEL` must
already be cached or use a `local:/absolute/path` selector. To deliberately use Engraphis's
dependency-free deterministic hashing embedder instead, set `ENGRAPHIS_EMBED_MODEL` to an empty
value for the import process; recall will then expose lexical degraded mode rather than claiming
semantic-vector support.

## Dashboard

Open the dashboard and choose **Import local documents**. Select **Documents** mode, choose files
or a folder, then set the source label, workspace, optional repository/session, scope, memory
type, and conflict policy. **Preview import** performs zero writes and shows every candidate,
format, warning, skip, rejection, update, rename, conflict, and missing source. Check the local
document confirmation and choose **Import documents** only after reviewing that report.

A new browser source requires a nonblank Source label; folder selection prefills its root folder
name. This label is part of the local source identity, so unrelated collections cannot silently
share a re-import lineage. Select a saved source when resuming or re-importing that collection.

The browser processes selected bytes locally and does not retain a dashboard upload copy. The
trusted dashboard flow requires its local owner-browser/CSRF boundary and an
`ENGRAPHIS_API_TOKEN`; in zero-token loopback mode, use the CLI.

## Supported formats

The built-in parser is intentionally small and uses only the Python standard library:

| Format | Extensions | Preserved/readable structure |
|---|---|---|
| Markdown | `.md`, `.markdown`, `.mdown` | Canonical Markdown; the Obsidian adapter adds frontmatter and note-link metadata. |
| Plain text | `.txt`, `.text`, `.log` | Canonical text. |
| reStructuredText | `.rst`, `.rest` | Canonical text and simple underline headings. |
| HTML | `.html`, `.htm`, `.xhtml` | Original HTML plus readable text; scripts, styles, templates, and noscript content are excluded from readable text. |
| JSON | `.json`, `.jsonl`, `.ndjson` | Structured, readable JSON/JSON Lines where valid; malformed JSON is preserved as text with a warning. |
| CSV/TSV tables | `.csv`, `.tsv`, `.tab` | Original table text with bounded header/row metadata. |
| Word-processing documents (DOCX, ODT, RTF) | `.docx`, `.odt`, `.rtf` | Readable paragraphs/text via bounded ZIP/XML or conservative RTF parsing; rich layout, comments, and tracked changes are not reproduced. |
| Spreadsheets (XLSX, ODS) | `.xlsx`, `.ods` | Bounded worksheet/cell values as readable rows; formulas are never executed and workbook layout is not reproduced. |
| Presentations (PPTX, ODP) | `.pptx`, `.odp` | Bounded slide text in slide order; animations, speaker media, and visual layout are not reproduced. |
| EPUB | `.epub` | Readable spine/chapter text via bounded ZIP/XML/HTML parsing. |
| Configuration/XML text | `.yaml`, `.yml`, `.toml`, `.ini`, `.cfg`, `.xml` | Readable source text; credentials and secret-like values are rejected. |
| Source code | `.py`, `.pyi`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.go`, `.rs`, `.java`, `.cs`, `.c`, `.h`, `.cc`, `.cpp`, `.cxx`, `.hpp`, `.hh`, `.hxx`, `.sql`, `.tf`, `.tfvars`, `.hcl`, `.sh`, `.ps1`, `.rb`, `.php`, `.swift`, `.kt`, `.kts`, `.scala`, `.lua`, `.r`, `.css` | Canonical source text. Link, tag, and heading discovery is disabled for source code so examples do not create graph relationships. |

The outer v2 importer also reuses Engraphis's existing local resource adapters. With
`engraphis[documents]`, PDF text and image OCR are available; OCR additionally needs the local
Tesseract executable. Audio/video transcription needs `engraphis[transcription]` and
`ENGRAPHIS_WHISPER_MODEL` must point to an existing local model file or directory. The importer
refuses a model name or missing path so this workflow cannot trigger a model download.

The parser retains original readable content or structure and source metadata. It masks fenced and
inline code before generic tag/link discovery, so examples and snippets cannot manufacture source
relationships. It never executes HTML, Markdown plugins, scripts, macros, or embedded content.

## Safety and privacy

Only regular files below the selected root are considered. The scanner rejects a symlinked root,
skips symlinks and hidden/configuration paths, rejects common credential/key filenames and
secret-like contents, bounds individual documents and the whole collection, and verifies a file
did not change while it was read. ZIP containers have member-count, decompressed-size,
compression-ratio, path, and DTD/entity protections. Unsafe, malformed, unreadable, oversized,
binary, or unsupported files are catalogued per file; one failure does not stop the rest of the
collection. Reports never echo secret-like source content.

Default filename exclusions include `.env` variants, credentials, secrets, tokens, recovery
codes, SSH identity files, and `.pem`, `.key`, `.p12`, and `.pfx` material. A collection is
bounded to 10,000 encountered files and 250 MB of read bytes; an individual adapter input is
bounded to 100 MB, while canonical memory text is capped at 100,000 characters and is rejected
rather than silently split. Containers are additionally capped at 2,000 members and 20 MB of
declared decompressed content. Invalid UTF-8/UTF-16 in permitted text is replaced explicitly and
reported as a parsing warning.

Markdown uses the Obsidian adapter's stricter 2 MB raw-note limit before decoding.

Review a preview before importing sensitive material. The local importer makes no network request
and does not copy source folders or attachments into a hidden second collection.

CLI dry runs inspect an existing plaintext or configured SQLCipher manifest through an immutable,
query-only connection. They tolerate a pre-importer database as an empty manifest and refuse an
active uncheckpointed WAL instead of creating or consulting writable sidecars.

## Re-import, history, and conflicts

Each collection has a stable local source identity plus per-document path and content identity.
The same collection and target scope re-import idempotently: unchanged documents are not
duplicated, changed documents follow the selected conflict policy, and unique exact-content moves
can be reported as renames. Source files missing on a later scan are reported; their memories are
not hard-deleted.

`replace` creates a temporal successor under the normal v2 rules, `new` creates a distinct memory,
and the default `error` reports a divergent lineage without silently overwriting it. Progress is
recorded per document. Rerun an interrupted or `--limit`-paused command with the same root and
target to resume safely; the final report includes imported, updated, renamed, skipped, rejected,
conflict, missing, warning, and error counts.

## Optional adapters and limits

The universal core parser deliberately has no hard dependency on OCR, PDF decoding,
audio/video transcription, office applications, or a hosted model. The outer importer invokes
only installed local adapters for PDF/OCR/transcription and reports a per-file rejection when an
adapter or local executable/model is unavailable. Legacy OLE Office files (`.doc`, `.xls`,
`.ppt`), encrypted or DRM-protected documents, unknown containers, and arbitrary binary files
are never guessed or decoded as text. A PDF adapter extracts embedded PDF text; it does not OCR
scanned PDF pages. Import page images separately when local OCR is required.

The parser does not render HTML/CSS, execute JavaScript or macros, interpret every RST directive,
evaluate spreadsheet formulas, reproduce presentation/EPUB layout, or preserve every
rich-document annotation. YAML and TOML are preserved safely but are not fully interpreted; RTF,
spreadsheets, presentations, and EPUB are readable-text imports rather than editing-fidelity
conversions. Unsupported files remain explicit per-file report entries and can be added later
through the bounded adapter interface; “universal” never means guessing arbitrary binary bytes.
For Obsidian-specific unsupported syntax and attachment behavior, see
[the Obsidian adapter guide](OBSIDIAN_IMPORT.md).
