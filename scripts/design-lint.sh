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
# Requires the repository-pinned impeccable package installed by `npm ci`.
# Missing dependencies and invalid detector output fail explicitly instead of silently passing.
set -uo pipefail

TARGET="engraphis/dashboard_assets/index.html"
STRICT=0
for a in "$@"; do
  case "$a" in
    --strict) STRICT=1 ;;
    -*) : ;;                       # ignore unknown flags
    *) TARGET="$a" ;;
  esac
done

command -v node >/dev/null 2>&1 || { echo "design-lint: node not found" >&2; exit 2; }
[ -f "$TARGET" ] || { echo "design-lint: $TARGET not found"; exit 2; }
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
IMPECCABLE="$ROOT/node_modules/.bin/impeccable"
if [ ! -x "$IMPECCABLE" ]; then
  echo "design-lint: pinned local impeccable dependency unavailable; run npm ci" >&2
  exit 2
fi

TMP="$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/design-lint.$$.json")"
trap 'rm -f "$TMP"' EXIT

if ! timeout 120 "$IMPECCABLE" detect --json "$TARGET" >"$TMP" 2>/dev/null; then
  if ! node -e 'const d=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));if(!Array.isArray(d))process.exit(1)' "$TMP" >/dev/null 2>&1; then
    echo "design-lint: detector failed without valid JSON output" >&2
    exit 2
  fi
fi

node -e '
const fs = require("fs");
let d; try { d = JSON.parse(fs.readFileSync(process.argv[1], "utf8")); }
catch (error) { console.error(`design-lint: invalid detector output: ${error.message}`); process.exit(2); }
if (!Array.isArray(d)) { console.error("design-lint: detector output must be an array"); process.exit(2); }
const target = process.argv[2], strict = process.argv[3] === "1";
if (d.length === 0) { console.log(`design-lint: ✔ 0 issues (${target})`); process.exit(0); }
const by = {}; let errors = 0;
for (const x of d) { by[x.antipattern] = (by[x.antipattern] || 0) + 1; if (x.severity === "error") errors++; }
console.log(`design-lint: ${d.length} issue(s) in ${target}`);
for (const k of Object.keys(by).sort((a,b)=>by[b]-by[a])) console.log(`  ${String(by[k]).padStart(3)}  ${k}`);
if (strict && errors > 0) { console.log(`design-lint: ${errors} error-severity issue(s) — failing (strict)`); process.exit(1); }
' "$TMP" "$TARGET" "$STRICT"
