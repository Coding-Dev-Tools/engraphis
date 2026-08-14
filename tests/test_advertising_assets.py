from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
GALLERY = ROOT / "docs" / "advertising" / "index.html"
CAMPAIGN = ROOT / "docs" / "advertising" / "campaign.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_advertising_gallery_contains_the_proof_narrative() -> None:
    gallery = _read(GALLERY)

    for value in (
        "Grounded,",
        "not guessed.",
        "Install free",
        "Watch continuity demo",
        "740.3 → 214.3",
        "CITED",
        "ABSTAIN",
        "HISTORY",
        "Memory has a timeline.",
        "Search the code. Explain the decision.",
        "Connect MCP",
        "Open dashboard",
        "Use Python",
    ):
        assert value in gallery

    assert "../images/context-efficiency.svg" in gallery
    assert "../images/evidence-backed-agent-examples.svg" in gallery
    assert "../../demo/engraphis_screen_demo.html" in gallery
    assert chr(0x2014) not in gallery
    assert chr(0x2013) not in gallery


def test_advertising_gallery_local_links_resolve() -> None:
    gallery = _read(GALLERY)
    hrefs = re.findall(r'href="([^"]+)"', gallery)

    for href in hrefs:
        parsed = urlsplit(href)
        if parsed.scheme or href.startswith("#"):
            continue
        target = (GALLERY.parent / parsed.path).resolve()
        assert target.is_file(), f"Broken advertising gallery link: {href}"


def test_advertising_gallery_has_no_network_dependencies() -> None:
    gallery = _read(GALLERY).lower()

    assert "<script" not in gallery
    assert "<link" not in gallery
    assert "http://" not in gallery
    assert "https://" not in gallery


def test_campaign_uses_registered_evidence_and_guardrails() -> None:
    campaign = _read(CAMPAIGN)

    for value in (
        "offline-chunking",
        "offline-performance",
        "docs/PUBLIC_BENCHMARK_RUNBOOK.md",
        "No support, no answer.",
        "Facts change. History stays visible.",
        "advertising_gallery_open",
        "advertising_trial_click",
    ):
        assert value in campaign

    assert "must not be added together" in campaign
    assert "provider billing" in campaign
    assert chr(0x2014) not in campaign
    assert chr(0x2013) not in campaign


def test_readme_links_to_the_advertising_surface() -> None:
    readme = _read(ROOT / "README.md")

    assert "https://github.com/Coding-Dev-Tools/engraphis/tree/main/docs/advertising" in readme
    assert "https://github.com/Coding-Dev-Tools/engraphis/blob/main/docs/advertising/campaign.md" in readme
