"""Claim evidence queries contain active prose, with original display offsets kept."""
import pytest

from engraphis.core import spec_crawl
from engraphis.service import MemoryService


SATURN = "Saturn has bright rings composed primarily of frozen water with smaller rocky particles"
ASSERTION = "The deployment database uses MySQL for production."


@pytest.fixture
def service():
    svc = MemoryService.create(":memory:")
    try:
        yield svc
    finally:
        svc.close()


@pytest.mark.parametrize("delimiter", ["`", "``"])
def test_unrelated_inline_example_cannot_ground_an_unsupported_claim(service, delimiter):
    service.remember(SATURN, workspace="alpha", trusted=True)
    before = service.store.conn.total_changes
    plain = service.spec_crawl("# Context\n" + ASSERTION, workspace="alpha")
    text = f"# Context\nThe deployment database uses MySQL {delimiter}{SATURN}{delimiter} for production."
    report = service.spec_crawl(text, workspace="alpha")
    assert plain["claims"][0]["status"] == "untraced"
    assert report["claims"][0]["status"] == "untraced"
    assert report["claims"][0]["memory_id"] is None
    assert report["counts"]["claims_checked"] == 1
    assert report["counts"]["claims_traced"] == 0
    assert report["score"] == plain["score"]
    assert report["score_breakdown"] == plain["score_breakdown"]
    # Reports still display the user's original span, including the example.
    assert SATURN in report["claims"][0]["sentence"]
    assert report["claims"][0]["start"] == len("# Context\n")
    assert report["claims"][0]["end"] == len(text) - 1
    assert service.store.conn.total_changes == before


@pytest.mark.parametrize("body,expected", [
    ("The server uses `UNRELATED` PostgreSQL in production.",
     ["The server uses ; PostgreSQL in production"]),
    ("The server uses ``literal ` UNRELATED`` PostgreSQL in production.",
     ["The server uses ; PostgreSQL in production"]),
    ("The server uses PostgreSQL\n```text\nUNRELATED\n```\nfor production.",
     ["The server uses PostgreSQL"]),
    ("The server uses PostgreSQL\n~~~~text\nUNRELATED\n~~~~\nfor production.",
     ["The server uses PostgreSQL"]),
    ("`UNRELATED` The server uses PostgreSQL in production.",
     ["The server uses PostgreSQL in production"]),
    ("The server uses PostgreSQL in production `UNRELATED`.",
     ["The server uses PostgreSQL in production"]),
    ("`" + "UNRELATED" * 4_000 + "` The server uses PostgreSQL in production.",
     ["The server uses PostgreSQL in production"]),
    ("The server uses `" + "UNRELATED" * 4_000 + "` PostgreSQL in production.",
     ["The server uses ; PostgreSQL in production"]),
], ids=["inline", "nested-backtick", "backtick-fence", "tilde-fence", "prefix", "suffix",
        "long-prefix", "long-middle"])
def test_lookup_queries_exclude_examples_before_excerpting_without_changing_spans(body, expected):
    queries = []

    def lookup(query):
        queries.append(query)
        return None

    text = "# Context\n" + body
    report = spec_crawl.crawl_spec(text, support_lookup=lookup)
    assert queries == expected
    assert report["counts"]["claim_lookup_attempts"] == len(expected)
    assert report["claims"][0]["start"] == text.index("The server")
    if "\n" not in body:
        claim = report["claims"][0]
        assert text[claim["start"]:claim["end"]].startswith("The server uses")
        assert claim["sentence"].startswith("The server uses")
        if "uses `" in body:
            assert "UNRELATED" in claim["sentence"]


@pytest.mark.parametrize("body", [
    "`The server uses PostgreSQL in production.`",
    "``literal ` The server uses PostgreSQL in production.``",
    "```text\nThe server uses PostgreSQL in production.\n```",
    "~~~text\nThe server uses PostgreSQL in production.\n~~~",
])
def test_code_only_documents_never_issue_claim_queries(body):
    queries = []
    report = spec_crawl.crawl_spec("# Context\n" + body, support_lookup=queries.append)
    assert queries == []
    assert report["claims"] == []
    assert report["counts"]["claim_lookup_attempts"] == 0


def test_real_prose_still_traces_to_its_own_evidence_with_unrelated_example_present(service):
    real = service.remember(ASSERTION, workspace="alpha", trusted=True)["id"]
    service.remember(SATURN, workspace="alpha", trusted=True)
    before = service.store.conn.total_changes
    report = service.spec_crawl(
        f"# Context\nThe deployment database uses MySQL `{SATURN}` for production.",
        workspace="alpha",
    )
    claim = report["claims"][0]
    assert claim["status"] == "traced"
    assert claim["memory_id"] == real
    assert claim["support"] == 1.0
    assert service.store.conn.total_changes == before


def test_code_masking_work_does_not_scale_with_claim_lookup_count(monkeypatch):
    original = spec_crawl._mask_code_ranges
    masks = []

    def mask(text, ranges):
        masks.append(len(text))
        return original(text, ranges)

    monkeypatch.setattr(spec_crawl, "_mask_code_ranges", mask)
    line = "The server uses `UNRELATED` PostgreSQL in production.\n"
    spec_crawl.crawl_spec("# Context\n" + line, support_lookup=lambda query: None)
    single_mask_count = len(masks)
    masks.clear()
    report = spec_crawl.crawl_spec("# Context\n" + line * 25, support_lookup=lambda query: None)
    assert report["counts"]["claim_lookup_attempts"] == 25
    assert len(masks) == single_mask_count
