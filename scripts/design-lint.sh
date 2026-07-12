#!/usr/bin/env bash
# Design-slop linter for the Engraphis dashboard.
#
# Runs pbakaus/impeccable's deterministic detector (AI-slop + design-quality
# rules, no LLM / no API key) against the static dashboard and prints a summary.
#
# Manual use:
#   bash scripts/design-lint.sh                       # warn-only, always exit 0
#   bash scripts/design-lint.sh --strict              # exit 1 if any *error*-severity issue
#   bash scripts/design-lint.sh path/to/file.html     # lint a different file
#
# Requires only Node (for `npx`). If Node is missing or the machine is offline,
# the script skips cleanly (exit 0) so it never blocks work.
set -uo pipefail

TARGET="engraphis/static/index.html"
STRICT=0
for a in "$@"; do
  case "$a" in
    --strict) STRICT=1 ;;
    -*) : ;;                       # ignore unknown flags
    *) TARGET="$a" ;;
  esac
done

command -v node >/dev/null 2>&1 || { echo "design-lint: node not found — skipping"; exit 0; }
[ -f "$TARGET" ] || { echo "design-lint: $TARGET not found — skipping"; exit 0; }

TMP="$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/design-lint.$$.json")"
trap 'rm -f "$TMP"' EXIT

if ! timeout 120 npx -y impeccable@latest detect --fast --json "$TARGET" >"$TMP" 2>/dev/null; then
  # npx returns non-zero both when the tool is unavailable AND when issues are
  # found; distinguish by whether we got parseable JSON back.
  if ! node -e 'JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"))' "$TMP" >/dev/null 2>&1; then
    echo "design-lint: detector unavailable (offline / install failed) — skipping"
    exit 0
  fi
fi

node -e '
const fs = require("fs");
let d; try { d = JSON.parse(fs.readFileSync(process.argv[1], "utf8")); } catch { process.exit(0); }
if (!Array.isArray(d)) process.exit(0);
const target = process.argv[2], strict = process.argv[3] === "1";
if (d.length === 0) { console.log(`design-lint: ✔ 0 issues (${target})`); process.exit(0); }
const by = {}; let errors = 0;
for (const x of d) { by[x.antipattern] = (by[x.antipattern] || 0) + 1; if (x.severity === "error") errors++; }
console.log(`design-lint: ${d.length} issue(s) in ${target}`);
for (const k of Object.keys(by).sort((a,b)=>by[b]-by[a])) console.log(`  ${String(by[k]).padStart(3)}  ${k}`);
if (strict && errors > 0) { console.log(`design-lint: ${errors} error-severity issue(s) — failing (strict)`); process.exit(1); }
' "$TMP" "$TARGET" "$STRICT"
