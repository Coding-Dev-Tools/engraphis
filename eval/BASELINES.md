# Harness baseline semantics

`eval.harness` records a `baseline_execution` object in every report.  A label is
not a display-only alias: it either changes the executed path or fails before an
artifact is returned when the fixture/runtime cannot represent the claim.

- `dense_lexical_rrf` uses vector and lexical arms with graph disabled.  It is
  explicitly recorded as equivalent to `no_graph` because the current pipeline
  applies RRF to every multi-arm retrieval configuration.
- `full_history` returns every stored version in chronological source order,
  including invalidated records; `whole_document` returns each case's raw
  `document`.  Neither query-selects or truncates context, so both reject an
  insufficient explicit token budget.
- `no_reranker` requires a supplied non-identity reranker and disables it for
  the run. `no_temporal_resolution` requires an explicit repeated
  `subject_key`/`claim_kind` fixture and writes without conflict resolution.

Rows with `answerable` labels remain excluded from retrieval metrics when they
have no gold evidence.  Pass `--grounded` to score those same labels with
grounded-answer and abstention precision/recall/F1; otherwise the v2 metrics
publish an explicit unavailable reason rather than an implied zero.
