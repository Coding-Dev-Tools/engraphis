from pathlib import Path

import pytest

from engraphis.core.obsidian import (
    normalize_obsidian_path,
    parse_obsidian_note,
    scan_obsidian_vault,
)


def test_parses_frontmatter_title_tags_dates_and_headings():
    note = parse_obsidian_note(
        b"\xef\xbb\xbf---\ntitle: Vault title\naliases: [One, 'Two']\ntags:\n  - projects\n  - #python\ncreated: 2024-01-02\n---\n# Body title\n## Details\n#inline-tag\n",
        "Projects/Note.md",
    )
    assert note.title == "Vault title"
    assert note.title_source == "frontmatter"
    assert note.aliases == ["One", "Two"]
    assert note.tags == ["projects", "python", "inline-tag"]
    assert note.dates == {"created": "2024-01-02"}
    assert note.headings == ["Body title", "Details"]
    assert note.relative_path == "Projects/Note.md"
    assert len(note.raw_sha256) == len(note.canonical_sha256) == 64


def test_title_falls_back_to_h1_then_stem():
    heading = parse_obsidian_note(b"# H1\n", "one.md")
    fallback = parse_obsidian_note(b"No title\n", "Folder/two.md")
    assert (heading.title, heading.title_source) == ("H1", "heading")
    assert (fallback.title, fallback.title_source) == ("two", "filename")


def test_discovers_wikilinks_embeds_blocks_and_attachments_but_not_code():
    note = parse_obsidian_note(
        b"[[Note|Read this]] [[Note#Heading]] [[Note^block]] ![[image.png]]\n"
        b"![alt](docs/file.pdf)\n`[[inline]]`\n```md\n![[hidden.jpg]]\n```\n",
        "links.md",
    )
    assert [(link.target, link.display_text, link.heading, link.block_id, link.embedded) for link in note.links] == [
        ("Note", "Read this", None, None, False), ("Note", None, "Heading", None, False),
        ("Note", None, None, "block", False), ("image.png", None, None, None, True),
    ]
    assert [attachment.path for attachment in note.attachments] == ["image.png", "docs/file.pdf"]


def test_unclosed_and_variable_length_fences_cannot_create_graph_metadata():
    closed = parse_obsidian_note(
        b"```md\n[[Hidden]] #hidden\n````\n[[Visible]] #visible\n",
        "closed.md",
    )
    assert [link.target for link in closed.links] == ["Visible"]
    assert closed.tags == ["visible"]

    unclosed = parse_obsidian_note(
        b"# Visible heading\n[[Visible]] #visible\n````md\n"
        b"[[Injected]] #injected\n## Forged heading\n",
        "unclosed.md",
    )
    assert [link.target for link in unclosed.links] == ["Visible"]
    assert unclosed.tags == ["visible"]
    assert unclosed.headings == ["Visible heading"]


def test_malformed_frontmatter_is_a_warning_and_is_not_fatal():
    note = parse_obsidian_note(b"---\ntitle: unfinished\n# Body\n", "bad.md")
    assert note.title == "Body"
    assert note.warnings == ["unclosed YAML frontmatter treated as Markdown"]


def test_invalid_utf8_is_repaired_with_a_warning():
    note = parse_obsidian_note(b"# Title\n\xff", "bad-utf8.md")
    assert "invalid UTF-8" in note.warnings[0]
    assert "\ufffd" in note.content


def test_frontmatter_only_note_is_not_a_memory_candidate():
    with pytest.raises(ValueError, match="note produced no readable text"):
        parse_obsidian_note(b"---\ntitle: Metadata only\n---\n\n", "empty.md")


@pytest.mark.parametrize("body", [
    b"-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
    b"api_key: this-is-a-long-secret-value",
])
def test_secret_content_is_rejected_without_echoing_it(body):
    with pytest.raises(ValueError, match="source appears to contain a secret"):
        parse_obsidian_note(body, "note.md")


def test_vault_scan_skips_hidden_config_symlinks_and_rejects_secrets(tmp_path: Path):
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "ok.md").write_text("# Safe", encoding="utf-8")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "config.md").write_text("# Ignore", encoding="utf-8")
    (tmp_path / ".env.md").write_text("# Ignore", encoding="utf-8")
    (tmp_path / "secret.md").write_text("password: very-secret-value", encoding="utf-8")
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# Outside", encoding="utf-8")
    try:
        (tmp_path / "linked.md").symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable on this platform")
    report = scan_obsidian_vault(tmp_path)
    assert [note.relative_path for note in report.notes] == ["Notes/ok.md"]
    assert {issue.relative_path for issue in report.rejected} == {"secret.md"}
    assert {issue.relative_path for issue in report.skipped} >= {".obsidian", ".env.md", "linked.md"}


def test_large_notes_are_rejected(tmp_path: Path):
    (tmp_path / "large.md").write_text("x" * 100_001, encoding="utf-8")
    report = scan_obsidian_vault(tmp_path)
    assert report.notes == []
    assert report.rejected[0].reason == "note exceeds 100000 character safety limit"


def test_oversized_note_bytes_are_rejected_before_read(tmp_path: Path):
    (tmp_path / "large.md").write_bytes(b"x" * 2_000_001)
    report = scan_obsidian_vault(tmp_path)
    assert report.notes == []
    assert report.rejected[0].reason == "note exceeds 2000000 byte safety limit"


@pytest.mark.parametrize("path", [
    "../note.md", "/note.md", "C:/note.md", r"C:\note.md", "C:note.md",
    r"\\server\share\note.md", "//server/share/note.md",
    "folder/note.md:stream", " note.md", "note.md\x00", "",
])
def test_uploaded_relative_paths_reject_traversal_and_absolute_paths(path):
    with pytest.raises(ValueError, match="safe vault-relative"):
        normalize_obsidian_path(path)


def test_relative_path_allows_dots_inside_a_filename():
    assert normalize_obsidian_path("notes/version..history.md") == "notes/version..history.md"


def test_relative_path_is_nfc_and_bounded():
    assert normalize_obsidian_path("notes/cafe\u0301.md") == "notes/café.md"
    with pytest.raises(ValueError, match="4096"):
        normalize_obsidian_path(("a" * 4094) + ".md")


def test_vault_scan_rejects_portable_case_collisions(tmp_path: Path):
    (tmp_path / "A.md").write_text("first", encoding="utf-8")
    (tmp_path / "a.md").write_text("second", encoding="utf-8")
    report = scan_obsidian_vault(tmp_path)
    assert len(report.notes) == 1
    if len(list(tmp_path.iterdir())) == 2:
        assert any(issue.reason == "duplicate normalized source path" for issue in report.rejected)


def test_vault_scan_rejects_a_file_changed_during_read(monkeypatch, tmp_path: Path):
    note = tmp_path / "racing.md"
    note.write_text("# original", encoding="utf-8")
    from engraphis.core import obsidian

    original_read = obsidian.os.read
    changed = False

    def racing_read(fd, size):
        nonlocal changed
        chunk = original_read(fd, size)
        if chunk and not changed:
            changed = True
            note.write_text("# replacement with a different size", encoding="utf-8")
        return chunk

    monkeypatch.setattr(obsidian.os, "read", racing_read)
    report = scan_obsidian_vault(tmp_path)
    assert report.notes == []
    assert [(issue.relative_path, issue.reason) for issue in report.rejected] == [
        ("racing.md", "file changed during scan"),
    ]


def test_vault_root_symlink_is_rejected(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    linked = tmp_path / "linked-vault"
    try:
        linked.symlink_to(vault, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks unavailable on this platform")
    with pytest.raises(ValueError, match="root cannot be a symlink"):
        scan_obsidian_vault(linked)
