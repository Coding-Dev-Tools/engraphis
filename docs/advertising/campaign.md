# Engraphis proof-first campaign

Engraphis should be marketed as a memory system that can show its work.

> Grounded, not guessed. Memory with receipts. Local by default.

This guide turns existing demos, benchmark artifacts, and product surfaces into a repeatable campaign. It does not introduce new product claims.

## Message architecture

### The flagship promise

**Engraphis carries project knowledge forward without treating old information as current truth.**

Use this promise when the audience needs the whole story: local storage, scoped recall, temporal validity, provenance, and context packing.

### Four proof stories

| Story | Hook | Existing proof | Primary action |
| --- | --- | --- | --- |
| Local | No account. No API key. Still remembers. | Dashboard quickstart and local-first README copy | Install free |
| Grounded | No support, no answer. | Grounded recall and evidence-backed behavior fixture | Inspect grounded recall |
| Temporal | Facts change. History stays visible. | Continuity demo, invalidation, supersession, and timeline | Watch the continuity demo |
| Efficient | Fewer tokens, better evidence. | Registered `offline-chunking` and `offline-performance` fixtures | Read the benchmark |

Keep one story per asset. Do not combine every feature into one graphic.

## Four-week distribution sequence

### Week 1: Local memory

Lead with the relief of not starting from zero and the control of keeping memory on the machine.

- Short screen clip: open the local dashboard and show the graph, provenance, and receipts.
- Static post: “Your agent’s memory can stay on your machine.”
- Setup post: `pip install "engraphis[mcp]"`, `engraphis-init`, and the Smart MCP command.
- README placement: link the proof gallery directly below the existing knowledge graph image.

### Week 2: Grounded recall

Lead with a cited answer and an explicit abstain. The contrast is more memorable than another generic retrieval diagram.

- Carousel: “Cited answer” beside “No support, no answer.”
- Short clip: ask one supported question, then one off-topic question.
- Technical post: explain why the grounded gate uses absolute support instead of the normalized recall score.
- CTA: “Try grounded recall.”

### Week 3: Temporal memory

Lead with one changing repository decision. Show the old fact, the new fact, and the reason the old record remains queryable.

- Timeline graphic: old validity window closes, new fact becomes current.
- Continuity reel: use the existing 56-second demo artifact.
- Blog post: “Engraphis does not just remember. It remembers what changed.”
- CTA: “Inspect the why and timeline.”

### Week 4: Context economy

Lead with the smallest useful evidence, not a vague claim about speed or cost.

- Stat card: `740.3 -> 214.3` tokens with Recall@5 `1.000` in the registered fixture.
- Evidence card: `162.2 -> 42.4` tokens to the smallest evidence-holding memory.
- Technical post: clarify that the compact payload proxy is separate from chunking and must not be added to it.
- CTA: “Read the benchmark definitions.”

## Reusable post hooks

1. Stop replaying the whole chat.
2. Memory with receipts.
3. No support, no answer.
4. Facts change. History stays visible.
5. Some answers live in the graph, not the note.
6. Your agent does not need more history. It needs the right evidence.
7. Local memory should not require a trust fall.
8. Find the symbol. Explain the decision.
9. Nine Smart MCP tools first. Discover advanced actions only when needed.
10. Bring your memory stack. Publish one immutable run.

## Public benchmark challenge

### Campaign idea

Invite memory-tool builders to run a fixed, public-safe fixture and publish the artifact digest, command, configuration, and result summary.

### Public copy

> Bring your memory stack. Publish one immutable run.
>
> Use the locked fixture, keep the comparison boundary explicit, and share the result without raw questions, answers, prompts, or private records.

### Launch requirements

- Anchor every published number to an evidence ID in `BENCHMARKS.md`.
- Use the public runbook in `docs/PUBLIC_BENCHMARK_RUNBOOK.md` as the execution contract.
- Publish the whole-input and source-file digests required by the runbook.
- Keep raw questions, answers, prompts, context, and per-record content fingerprints out of public artifacts.
- Present the challenge as a reproducibility standard, not as a self-selected victory lap.

### Embed-ready result format

```text
Memory benchmark
Stack: <name and version>
Fixture: <evidence id>
Command: <exact command>
Artifact digest: <digest>
Result summary: <registered metrics only>
Limitations: <fixture and counting boundary>
```

## Remixable diagram set

Create five self-contained HTML or SVG artifacts. Each should have one headline, one visual claim, one source link, and one CTA.

1. **Memory flow:** source material -> scoped memory -> hybrid recall -> task-ready evidence.
2. **Supersession chain:** old fact -> invalidation -> current fact -> why and timeline.
3. **Benchmark flow:** locked fixture -> exact command -> digest -> public result.
4. **Scope hierarchy:** workspace -> repo -> session -> memory.
5. **MCP integration:** install -> initialize -> connect -> recall and remember.

Use the gallery in `docs/advertising/index.html` as the visual reference. Keep the page free of external font, image, and JavaScript dependencies. Existing evidence files remain the canonical source views:

- `docs/images/context-efficiency.svg`
- `docs/images/evidence-backed-agent-examples.svg`
- `docs/images/knowledge-graph.png`
- `demo/engraphis_screen_demo.html`

The linked `diagram-design` project is a useful reference for self-contained HTML/SVG, brand tokens, gallery navigation, and exportable variants: <https://github.com/cathrynlavery/diagram-design>.

## Setup recipes

### Smart MCP

```bash
pip install "engraphis[mcp]"
engraphis-init
codex mcp add engraphis -- engraphis-mcp
```

### Dashboard

```bash
pip install "engraphis[server]"
engraphis-dashboard
```

### Offline Python library

```python
from engraphis.service import MemoryService

memory = MemoryService.create("engraphis.db")
```

Keep hosted sync and Pro trial messaging below the free local path. The local engine is the first conversion step. Hosted services are a separate trust and pricing decision.

## Measurement plan

Track these events without changing existing button labels or URL structure:

| Event | Meaning |
| --- | --- |
| `advertising_gallery_open` | A visitor opened the proof gallery |
| `advertising_demo_open` | A visitor opened the continuity demo |
| `advertising_install_click` | A visitor selected the free install path |
| `advertising_mcp_click` | A visitor selected the MCP path |
| `advertising_graph_click` | A visitor opened the graph quickstart |
| `advertising_trial_click` | A visitor selected an existing Pro or Team trial CTA |
| `advertising_benchmark_click` | A visitor opened the public benchmark material |

Run two headline comparisons:

- “Grounded, not guessed.” versus “Memory that knows where it came from.”
- “Install free” versus “Connect MCP.”

Judge the result by demo opens, install completion, MCP setup completion, graph export usage, and trial clicks. Do not call an experiment successful without a defined denominator and time window.

## Claim guardrails

- Keep `740.3 -> 214.3`, `162.2 -> 42.4`, and `23,810 -> 10,202` as separate measurements with their existing definitions.
- These measurements must not be added together.
- Do not describe deterministic fixtures as official LoCoMo or LongMemEval leaderboard results.
- Do not turn a synthetic fallback screen into customer evidence.
- Do not claim provider billing, universal latency, or customer productivity from the current offline fixtures.
- Keep code `query`, memory-backed `explain`, graph `path`, and graph `impact` as distinct actions.
- Preserve the local versus hosted boundary in every pricing and privacy asset.
