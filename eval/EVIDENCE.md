# External benchmark evidence

`eval.benchmark.report_envelope()` is the public-artifact boundary. It records
the dataset and optional source digests, commit and dirty-state digest, command,
configuration digest, environment, model metadata, and token-counting scope.
It removes raw questions, answers, returned context, and prompts from every
record while retaining SHA-256 digests for same-input verification.

After an official LongMemEval-V2 run, keep the upstream `per_question.jsonl`
private and create a redacted artifact:

```bash
python -m eval.longmemeval_v2_evidence \
  --per-question output/per_question.jsonl \
  --questions data/questions.json \
  --haystack data/haystack.json \
  --trajectories data/trajectories.json \
  --memory-config eval/configs/longmemeval_v2_engraphis.json \
  --output artifacts/longmemeval-v2.json
```

The command writes sorted JSON and an adjacent `.sha256` checksum, refusing to
replace a different artifact. It preserves the official harness QA score and
its fixed-reader memory-context item-content token count. That count excludes
chat-prompt framing and inter-item separators, so it is not a total provider
prompt-token claim. It is not a canonical Engraphis retrieval artifact until a
complete run also supplies the required five-budget evidence curve.

The evidence exporter records an intentionally redacted command label. Keep
API keys and raw prompt material only in the private official-run environment.
