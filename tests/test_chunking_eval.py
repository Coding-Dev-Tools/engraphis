"""Regression test for the chunking payoff (eval/chunking_eval.py).

Locks in the headline: sub-file chunking returns the relevant passage instead of the
whole document, so recall holds while context tokens collapse. Runs offline on the
deterministic embedder, so the assertions are on the robust, non-flaky signal
(context reduction + recall non-regression), not on a specific model's score.
"""
from pathlib import Path

from engraphis.backends.extractor import ChunkingExtractor
from eval.chunking_eval import compare, load, run_eval

INLINE = [
    {"id": "a",
     "document": ("# Auth\n\nThe API uses PASETO tokens, not JWT. Keys rotate daily.\n\n"
                  "# Storage\n\nSecrets live in the vault. Never commit them to git.\n\n"
                  "# Deploy\n\nDeploys run on Railway with a /data volume chowned at boot."),
     "questions": [{"q": "what token format is used", "evidence": "PASETO"},
                   {"q": "where do secrets live", "evidence": "vault"}]},
    {"id": "b",
     "document": ("# Decay\n\nRetention follows an Ebbinghaus curve.\n\n"
                  "# Recall\n\nThree arms fuse with reciprocal rank fusion.\n\n"
                  "# Grounding\n\nGrounded recall abstains below a support floor."),
     "questions": [{"q": "what forgetting curve is used", "evidence": "Ebbinghaus"},
                   {"q": "what does grounded recall do when unsure", "evidence": "abstains"}]},
]


def test_chunked_cuts_context_tokens_without_hurting_recall():
    whole = run_eval(INLINE, mode="whole", k=5)
    chunked = run_eval(INLINE, mode="chunked", k=5)
    assert chunked["memories_stored"] > whole["memories_stored"]         # actually chunked
    assert chunked["recall_at_k"] >= whole["recall_at_k"] - 1e-9         # no recall regression
    assert chunked["mean_context_tokens"] < whole["mean_context_tokens"]  # cheaper context


def test_shipped_longdoc_dataset_shows_material_reduction():
    path = Path(__file__).resolve().parents[1] / "eval" / "datasets" / "longdoc.jsonl"
    result = compare(load(str(path)), k=5, embed_model=None)
    whole = result["reports"]["whole"]
    chunked = result["reports"]["chunked"]
    assert chunked["recall_at_k"] >= whole["recall_at_k"] - 1e-9
    assert result["context_reduction_pct"] > 40.0   # deterministic ~73% at time of writing
    assert whole["token_counter"] == chunked["token_counter"] == "engraphis.chars4.v1"


def test_eval_uses_injected_reader_counter_for_ingest_and_measurement():
    class WordCounter:
        identity = "test.words.v1"

        def __call__(self, text):
            return len(text.split())

    counter = WordCounter()
    chunker = ChunkingExtractor(
        target_tokens=16,
        overlap_tokens=4,
        token_counter=counter,
    )

    report = run_eval(
        INLINE,
        mode="chunked",
        k=5,
        chunk_extractor=chunker,
    )

    assert report["token_counter"] == "test.words.v1"
    assert report["max_stored_tokens"] <= chunker.target_tokens
