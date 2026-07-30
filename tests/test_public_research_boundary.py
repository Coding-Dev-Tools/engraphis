"""Private narrative research must never enter public package/release inputs."""
from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    ROOT / "COMMERCIAL_AUDIT.md",
    ROOT / "COMPETITIVE_ANALYSIS.md",
    ROOT / "docs" / "COMMERCIAL_AUDIT.md",
    ROOT / "docs" / "COMPETITIVE_ANALYSIS.md",
)


def test_private_research_files_are_absent_and_ignored():
    assert all(not path.exists() for path in FORBIDDEN)
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for path in (
        "/COMMERCIAL_AUDIT.md",
        "/COMPETITIVE_ANALYSIS.md",
        "/docs/COMMERCIAL_AUDIT.md",
        "/docs/COMPETITIVE_ANALYSIS.md",
        "*[Cc][Oo][Mm][Mm][Ee][Rr][Cc][Ii][Aa][Ll]*[Aa][Uu][Dd][Ii][Tt]*",
        "*[Cc][Oo][Mm][Pp][Ee][Tt][Ii][Tt][Ii][Vv][Ee]*[Aa][Nn][Aa][Ll][Yy][Ss][Ii][Ss]*",
        "*[Cc][Oo][Mm][Pp][Ee][Tt][Ii][Tt][Oo][Rr]*[Rr][Ee][Ss][Ee][Aa][Rr][Cc][Hh]*",
        "*[Mm][Aa][Rr][Kk][Ee][Tt]*[Rr][Ee][Ss][Ee][Aa][Rr][Cc][Hh]*",
        "*[Pp][Rr][Ii][Cc][Ii][Nn][Gg]*[Rr][Ee][Ss][Ee][Aa][Rr][Cc][Hh]*",
        "*[Pp][Rr][Ii][Vv][Aa][Tt][Ee]*[Rr][Ee][Ss][Ee][Aa][Rr][Cc][Hh]*",
    ):
        assert path in ignored


def test_private_research_ignore_rules_work_for_mixed_case_paths():
    """Exercise Git's matcher, not only the spelling of ``.gitignore``."""
    candidates = (
        "cOmMeRcIaL-AuDiT-notes.md",
        "COMPETITIVE_analysis.md",
        "cOmPeTiToR_ReSeArCh.md",
        "mArKeT-ReSeArCh.md",
        "pRiCiNg_ReSeArCh.md",
        "docs/CoMmErCiAl_AuDiT.md",
        "docs/cOmPeTiTiVe-analysis.md",
        "docs/COMPETITOR_research.md",
        "docs/MaRkEt_ReSeArCh.md",
        "docs/PrIcInG_ReSeArCh.md",
        "notes/internal/CoMmErCiAl_AuDiT.md",
        "research/archive/cOmPeTiTiVe-analysis.md",
        "customer/private/COMPETITOR_research.md",
        "planning/2026/MaRkEt_ReSeArCh.md",
        "teams/sales/PrIcInG_ReSeArCh.md",
        "nested/private-research/notes.md",
        "nested/PrIvAtE_ReSeArCh/notes.md",
        "notes/private_research_findings.md",
    )
    for candidate in candidates:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", candidate],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, candidate


def test_private_migration_backups_are_ignored_for_any_database_filename():
    for candidate in (
        "engraphis.db.pre-migration-v5.bak",
        "customer-memory.pre-migration-v4.bak",
        "unextended-database.pre-migration-v123.bak",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", candidate],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, candidate


def test_public_narrative_surfaces_do_not_embed_competitor_research():
    public_surfaces = (
        ROOT / "README.md",
        ROOT / "BENCHMARKS.md",
        ROOT / "eval" / "external.py",
        ROOT / "engraphis" / "backends" / "extractor.py",
        ROOT / "engraphis" / "backends" / "sync_folder.py",
        ROOT / "engraphis" / "core" / "consolidate.py",
    )
    forbidden_phrases = (
        "## why it wins",
        "| axis | obsidian",
        "mem0",
        "zep",
        "letta",
        "obsidian users",
    )
    for path in public_surfaces:
        content = path.read_text(encoding="utf-8").casefold()
        assert all(phrase not in content for phrase in forbidden_phrases), path


def test_public_narrative_has_no_named_competitor_positioning():
    """Named products belong only in reproducible benchmark artifacts/leaderboards.

    Product names in executable importers are not narrative positioning, so this
    deliberately scans public Markdown surfaces rather than implementation code.
    """
    public_markdown = sorted({
        *ROOT.glob("*.md"),
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "skills").rglob("*.md"),
        *(ROOT / "eval").rglob("*.md"),
    })
    named_competitors = ("obsidian", "mem0", "zep", "letta")
    private_research_phrases = (
        "commercial audit",
        "competitive analysis",
        "competitor research",
        "market research",
        "pricing research",
        "competitive positioning",
        "why it wins",
    )
    for path in public_markdown:
        content = path.read_text(encoding="utf-8").casefold()
        assert all(name not in content for name in named_competitors), path
        assert all(phrase not in content for phrase in private_research_phrases), path
