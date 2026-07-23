"""Regression coverage for the dashboard CSP asset release gate."""
from __future__ import annotations

import pytest

from scripts import externalize_dashboard_assets as assets


def test_inline_asset_parser_handles_case_and_malformed_closing_tag():
    styles, scripts = assets._inline_assets(
        "<STYLE>body{color:red}</STYLE><SCRIPT>alert(1)</SCRIPT data-error=\"yes\">"
    )

    assert [asset.content for asset in styles] == ["body{color:red}"]
    assert [asset.content for asset in scripts] == ["alert(1)"]


def test_migrate_uses_parsed_asset_boundaries(tmp_path, monkeypatch):
    index = tmp_path / "index.html"
    css = tmp_path / "dashboard.css"
    js = tmp_path / "dashboard.js"
    index.write_text(
        "<html><head><STYLE>body{color:red}</STYLE></head>"
        "<body><button onclick=\"return false\">Go</button>"
        "<SCRIPT>console.log('ready')</SCRIPT data-error=\"yes\"></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(assets, "INDEX", index)
    monkeypatch.setattr(assets, "CSS", css)
    monkeypatch.setattr(assets, "JS", js)

    assets.migrate()

    html = index.read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/static/dashboard.css">' in html
    assert '<script src="/static/dashboard.js"></script>' in html
    assert "body{color:red}" in css.read_text(encoding="utf-8")
    assert "CSP_EVENT_HANDLERS" in js.read_text(encoding="utf-8")


@pytest.mark.parametrize("tag", ["script", "style"])
def test_check_rejects_unclosed_inline_asset_at_eof(tmp_path, monkeypatch, tag):
    index = tmp_path / "index.html"
    css = tmp_path / "dashboard.css"
    js = tmp_path / "dashboard.js"
    index.write_text(f"<html><body><{tag}>unclosed", encoding="utf-8")
    css.write_text("", encoding="utf-8")
    js.write_text("", encoding="utf-8")
    monkeypatch.setattr(assets, "INDEX", index)
    monkeypatch.setattr(assets, "CSS", css)
    monkeypatch.setattr(assets, "JS", js)

    with pytest.raises(SystemExit, match=f"inline {tag} block"):
        assets.check()
