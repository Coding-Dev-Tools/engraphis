# Engraphis

[![PyPI version](https://img.shields.io/pypi/v/engraphis.svg)](https://pypi.org/project/engraphis/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/LICENSE)

**Persistent, local-first memory for AI agents.** Engraphis stores scoped project knowledge, retrieves relevant evidence across vector, lexical, graph, and code search, and returns bounded context with sources an agent can inspect.

The local engine uses SQLite and works offline. It keeps changes over time instead of silently replacing facts, and grounded recall cites retrieved memories or abstains when evidence is weak.

<p align="center">
  <img src="https://raw.githubusercontent.com/Coding-Dev-Tools/engraphis/522def372c45a26d46bc7a05829964ae0240f3a9/docs/images/knowledge-graph.png" alt="Engraphis local knowledge graph showing relationships between remembered entities" width="100%">
  <br>
  <sup>Explore memories and their relationships in the local dashboard.</sup>
</p>

## Install and try it

The full local application requires Python 3.10 or newer. The NumPy-only core supports Python 3.9+.

```bash
python -m pip install "engraphis[all]"
engraphis-dashboard
```

The dashboard opens at [http://127.0.0.1:8700](http://127.0.0.1:8700). Local use needs no cloud account or API key.

To use the Python service directly:

```python
from engraphis.service import MemoryService

memory = MemoryService.create("engraphis.db")
memory.remember("Auth migrated from JWT to PASETO.", workspace="acme", repo="api")
hit = memory.recall("Why did we change auth?", workspace="acme", repo="api")
print(hit["context"])
```

Connect a coding agent over MCP with the [agent setup guide](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/AGENT_CONNECT.md).

## What it provides

- **Continuity:** Organize memories by workspace, repository, and session, with a last-session handoff.
- **Useful recall:** Combine vector, lexical, graph, and code-aware retrieval, then pack results to a hard context budget.
- **Grounded answers:** Preserve provenance and temporal history; cite support or abstain when it is insufficient.
- **Operator control:** Keep the core local and offline-capable, with explicit controls for hosted services and external LLM providers.

## Benchmarks

The current registered artifact contains three deterministic offline fixture runs. It separates context size, retrieval quality, and grounded decision checks; these small fixtures do not establish general task performance.

<p align="center">
  <img src="https://raw.githubusercontent.com/Coding-Dev-Tools/engraphis/v1.7.10/docs/images/context-efficiency.svg" alt="Three registered offline fixtures: structure-aware chunking reduced mean retrieved context from 740.3 to 214.3 tokens per question (71.1%, 18 questions); the serialized JSON-shape proxy fell from 24,590 to 11,138 tokens across 26 payload samples and 260 recalls. Candidate and packed retrieval quality (Recall@5, Hit@5, and answer-token recall) are shown separately (each 1.000). Grounded checks show 5/5 answerable queries grounded and 6/6 abstention queries rejected, including a 1/1 quarantined-evidence probe; 11/11 decisions were correct. MCP transport and provider billing were not measured. Artifact SHA-256 prefix 3b1d2127e290; see the benchmark guide for the full checksum." width="100%">
  <br>
  <sup>Three offline fixtures separate context reduction, candidate and packed retrieval quality, and grounded behavior. The chart shows the artifact checksum prefix; see the benchmark guide for the full checksum and reproduction steps.</sup>
</p>

| Fixture | Result | Quality check |
|---|---|---|
| Structure-aware chunking | Mean retrieved top-5 context: 740.3 → 214.3 tokens per question (71.1% lower, 18 questions) | Recall@5 = 1.000 in both modes |
| Recall payload proxy | JSON-shape proxy: 24,590 → 11,138 tokens (54.71% lower, 26 samples; 260 timed recalls) | Candidate and packed Recall@5, Hit@5, and answer-token recall are each 1.000 |
| Grounded decisions | 5/5 answerable queries grounded; 6/6 abstention queries rejected, including 1/1 quarantined-evidence check | 11/11 decisions correct |

The payload figure is a serialized JSON-shape estimate, not an MCP transport measurement or provider billing total. See the [Benchmark methodology](https://github.com/Coding-Dev-Tools/engraphis/blob/v1.7.10/BENCHMARKS.md) for artifact identity, counting methods, reproduction commands, external-evaluation boundaries, and limitations.

## Optional Jev assistance

Jev is Engraphis's advisory decision engine. **It is included free with every paid plan: Pro and Team.** Every legitimate Pro user and each eligible Team named seat, including paid viewers, with an active paid, trial, or test entitlement receives managed Jev at no additional customer charge and without a personal provider key. Local advisory decisions stay free for everyone; BYOK, a personal TypeSafe key, is the optional path for non-paid or direct use, and its provider charges may apply.

Managed Jev is currently `not_yet_available` pending release acceptance and service-capacity qualification, so client configuration does not enable it yet. Each individual receives all three independent rolling limits: **100 evaluated questions per rolling hour, 1,000 per rolling five hours, and 2,000 per rolling 24 hours**. Usage is per person, not pooled across a Team and not monthly; if a batch is evaluated, every question counts, and admitted attempts that fail or are interrupted remain counted. No overage is charged, and the account portal reports each member's remaining usage across all three windows.

The existing production fleet guard remains 100 questions per day across the service and may pause or reject requests earlier; resolve capacity and the fleet guard before launch. Jev is advisory only: it cannot write memories, change grounded-recall requirements, authorize commands, or certify completion. Every remote call requires per-call consent: `allow_remote=true` plus a `data_classification` of `public` or `internal`; `offline_mode=true` blocks remote requests. Recall route selection is experimental and BYOK-only; no retrieval-quality improvement is claimed. See [hosted plans and Jev details](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/HOSTED_PLANS.md#included-system-1-decision-engine-jev).

## Guides

- [Configuration reference](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/CONFIGURATION.md)
- [MCP tool reference](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/MCP_TOOLS.md)
- [Agent and LLM provider setup](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/LLM_PROVIDERS.md)
- [Architecture and query planning](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/ARCHITECTURE_V3.md#query-planning)
- [Docker deployment](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/DOCKER.md)
- [Document import](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/DOCUMENT_IMPORT.md)
- [Cloud Sync](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/SYNC.md)
- [Pi extension](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/integrations/pi/README.md)
- [Memory write review](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/WRITE_REVIEW.md)
- [Coding-agent file protection](https://github.com/Coding-Dev-Tools/engraphis/blob/main/docs/FILE_PROTECTION.md)
- [Security policy](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/SECURITY.md)
- [Hosted plans and licensing](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/HOSTED_PLANS.md)

## License

Engraphis is licensed under Apache-2.0. The license does not grant trademark rights. See [LICENSE](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/LICENSE), [NOTICE](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/NOTICE), and the [licensing guide](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/docs/LICENSING.md). The hosted control plane and managed services are private services.
