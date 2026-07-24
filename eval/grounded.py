"""Grounded-recall eval: does the abstain gate fire correctly? (AGENTS.md §3.7)

Retrieval evals (``eval.harness``) measure "did we fetch the right memory". Grounded
recall adds a *decision*: answer only when the evidence supports it, else abstain. This
scores that decision as a number — answerable queries should ground, off-topic ones
should abstain — so a change to the support signal or the floor is caught by a metric
rather than by vibes. Runs offline with the deterministic embedder.

    python -m eval.grounded
"""
from __future__ import annotations

from engraphis.core.engine import MemoryEngine

FACTS = [
    ("We standardised on PASETO tokens for auth, replacing JWT.", "auth"),
    ("The default package manager for frontend repos is pnpm.", "pkg"),
    ("Rate limiting is 100 requests per minute per API key.", "rate"),
    ("Database migrations run via alembic on deploy.", "db"),
    ("The staging environment redeploys on every merge to main.", "staging"),
    ("Application secrets are stored in Vault, never in the repository.", "secrets"),
]

ANSWERABLE = [
    "which auth scheme did we standardise on?",
    "what package manager do we use for the frontend?",
    "what is the API rate limit per key?",
    "how do database migrations run?",
    "where are application secrets stored?",
]

UNANSWERABLE = [
    "how do I bake sourdough bread?",
    "what is the airspeed velocity of an unladen swallow?",
    "who won the world cup in 1998?",
    "what is the capital of France?",
    "recommend a good pizza topping",
]


def _engine():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("eval")
    rid = eng.store.get_or_create_repo(wid, "grounded")
    for text, title in FACTS:
        eng.remember(text, workspace_id=wid, repo_id=rid, title=title)
    return eng, wid, rid


def run() -> dict:
    eng, wid, rid = _engine()
    grounded_hits = sum(
        eng.grounded_recall(q, workspace_id=wid, repo_id=rid).grounded for q in ANSWERABLE)
    abstain_hits = sum(
        eng.grounded_recall(q, workspace_id=wid, repo_id=rid).abstained for q in UNANSWERABLE)
    n_ans, n_un = len(ANSWERABLE), len(UNANSWERABLE)
    return {
        "answer_rate": grounded_hits / n_ans,
        "abstain_rate": abstain_hits / n_un,
        "accuracy": (grounded_hits + abstain_hits) / (n_ans + n_un),
        "grounded_hits": grounded_hits, "abstain_hits": abstain_hits,
        "n_answerable": n_ans, "n_unanswerable": n_un,
    }


def main() -> None:
    r = run()
    print("\nEngraphis grounded-recall eval (deterministic embedder)")
    # Keep the documented offline gate usable from the Windows console's default
    # cp1252 encoding, which cannot emit a Unicode arrow.
    print(f"  answerable -> grounded  : {r['answer_rate']:.3f}  "
          f"({r['grounded_hits']}/{r['n_answerable']})")
    print(f"  off-topic  -> abstained : {r['abstain_rate']:.3f}  "
          f"({r['abstain_hits']}/{r['n_unanswerable']})")
    print(f"  decision accuracy      : {r['accuracy']:.3f}  "
          f"({r['grounded_hits'] + r['abstain_hits']}/{r['n_answerable'] + r['n_unanswerable']})\n")


if __name__ == "__main__":
    main()
