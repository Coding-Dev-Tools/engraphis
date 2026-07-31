# Dashboard button QA

Date: 2026-07-29
Scope: v2 Ledger (`/`) and legacy Classic (`/classic`) dashboards.

## Test setup

The manual pass used four parallel browser lanes and an isolated local v2 server on
`127.0.0.1:8701` with deterministic embeddings. The fixture contained the `demo` and
`beta` workspaces plus representative memories, graph data, provenance, timeline, and
consolidation state. The four lanes covered:

- primary Ledger navigation, memory creation, grounded Ask, and theme controls;
- Library, import/editor actions, and empty-form behavior;
- Graph & Relations, Provenance, Manage, exports, saved views, and switches;
- broad regression including Classic and responsive/mobile keyboard behavior.

## Button coverage

The pass exercised the primary navigation, workspace selector, dashboard/theme switcher,
New memory, Save/Close, memory card actions, Import files, grounded answer, provenance
trace, timeline/history, supersessions, all Provenance tabs, all Graph tabs/styles/layouts/
palettes/saved views/layers/toggles/actions/exports, all Manage tabs, workspace create and
workspace actions, consolidation preview and commit confirmation, plan comparison, and
Classic navigation/mobile-nav controls.

## Failures found and fixed

1. **Empty Save memory was silent.** Native form validation prevented the JavaScript
   handler from running, leaving the editor open with no explanation. The editor now uses
   explicit validation, an alert-region error, `aria-invalid`, focus on the content field,
   and a status announcement.
2. **Closing the modern editor lost focus.** Close now returns focus to the button or card
   that opened the editor, with a safe New memory fallback.
3. **Empty Ask, Provenance, Timeline/Supersessions, and workspace-create actions were
   silent for the same native-validation reason.** These forms now use custom validation
   messages and focus the relevant field. Successful submissions clear the prior status
   message so an old validation error cannot remain beside a successful result.
4. **Classic mobile Escape closed the menu without reliably returning focus.** Escape now
   closes the menu through the shared focus-restoring path.

## Environment notes

- At the time of this manual pass, one parallel lane could not start against the repository's
  default database because the checkout then supported schema version 4 while that existing
  database was schema version 5. This historical environment/data compatibility issue was not a
  dashboard button failure. The isolated schema-4 fixture started and exercised the UI
  successfully.
- The browser harness did not expose programmatic download events for the PNG/JSON export
  anchors, but the dashboard status confirmed both exports completed. No application
  console errors were observed during the manual pass.

## Regression checks

The focused static regression checks live in
`tests/test_dashboard_button_regressions.py` and cover each repaired failure mode.
