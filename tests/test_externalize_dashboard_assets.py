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


def test_inline_asset_parser_resumes_after_malformed_closing_tag():
    styles, scripts = assets._inline_assets(
        "<SCRIPT>first()</SCRIPT data-error=\"yes\"><script>second()</script>"
    )

    assert styles == []
    assert [asset.content for asset in scripts] == ["first()", "second()"]


def test_inline_asset_parser_preserves_offsets_for_multiline_malformed_close():
    html = (
        "<script>first()</script\n data-error=\"yes\">\n"
        "<script>second()</script>"
    )

    _styles, scripts = assets._inline_assets(html)

    assert [asset.content for asset in scripts] == ["first()", "second()"]
    assert [html[asset.start:asset.end] for asset in scripts] == [
        '<script>first()</script\n data-error="yes">',
        "<script>second()</script>",
    ]


def test_migrate_uses_parsed_asset_boundaries(tmp_path, monkeypatch):
    index = tmp_path / "index.html"
    css = tmp_path / "dashboard.css"
    js = tmp_path / "dashboard.js"
    # ``migrate()`` ends by running the full gate over what it just wrote, so the inline script
    # has to carry the deferred-asset loaders the gate now requires.
    index.write_text(
        "<html><head><STYLE>body{color:red}</STYLE></head>"
        "<body><button onclick=\"return false\">Go</button>"
        "<SCRIPT>console.log('ready');"
        "script.src='/static/vendor/force-graph.min.js';"
        "script.src='/v2-assets/engraphis-graph.js'"
        "</SCRIPT data-error=\"yes\"></body></html>",
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


# ── deferred graph assets ───────────────────────────────────────────────────────────────
# force-graph.min.js applies inline styles at runtime, so under `style-src 'self'` loading it
# on every page reported a CSP violation per attempt.  It and the opt-in engine are fetched on
# demand instead; these rules keep that true and keep the deferred references checked.

_LOADERS = (
    "script.src='/static/vendor/force-graph.min.js';"
    "script.src='/v2-assets/engraphis-graph.js';"
)


def _gate(tmp_path, monkeypatch, html: str, js: str) -> None:
    index, css, script = (
        tmp_path / "index.html",
        tmp_path / "dashboard.css",
        tmp_path / "dashboard.js",
    )
    index.write_text(html, encoding="utf-8")
    css.write_text("", encoding="utf-8")
    script.write_text(js, encoding="utf-8")
    monkeypatch.setattr(assets, "INDEX", index)
    monkeypatch.setattr(assets, "CSS", css)
    monkeypatch.setattr(assets, "JS", script)
    # Isolate these cases to the script-reference rules; the first-party CSP scan has its own.
    monkeypatch.setattr(assets, "EXTRA_SCRIPTS", ())


def test_check_rejects_an_eagerly_loaded_csp_hostile_script(tmp_path, monkeypatch):
    """Putting the tag back in index.html is precisely the regression this rule catches."""
    _gate(
        tmp_path,
        monkeypatch,
        '<html><body><script src="/static/vendor/force-graph.min.js"></script></body></html>',
        _LOADERS,
    )

    with pytest.raises(SystemExit, match="must not eagerly load: /static/vendor/force-graph"):
        assets.check()


def test_check_rejects_an_eagerly_loaded_v2_graph_engine(tmp_path, monkeypatch):
    _gate(
        tmp_path,
        monkeypatch,
        '<html><body><script src="/v2-assets/engraphis-graph.js"></script></body></html>',
        _LOADERS,
    )

    with pytest.raises(SystemExit, match="must not eagerly load: /v2-assets/engraphis-graph"):
        assets.check()


def test_check_allows_a_deferred_script_named_only_in_a_comment(tmp_path, monkeypatch):
    """The rule reads parsed ``<script src>`` values, so index.html can still say why."""
    _gate(
        tmp_path,
        monkeypatch,
        "<html><body><!-- force-graph.min.js loads on demand --></body></html>",
        _LOADERS,
    )

    assets.check()


def test_check_rejects_a_deferred_script_with_no_lazy_loader(tmp_path, monkeypatch):
    """Dropping a tag without adding a loader orphans the asset instead of deferring it."""
    _gate(
        tmp_path,
        monkeypatch,
        "<html><body></body></html>",
        "script.src='/static/engraphis-graph.js';",
    )

    with pytest.raises(SystemExit, match="no lazy loader: /static/vendor/force-graph"):
        assets.check()


def test_check_rejects_a_lazy_reference_to_a_missing_file(tmp_path, monkeypatch):
    """A deferred asset's only reference is a JS string literal — nothing else catches a rename."""
    _gate(
        tmp_path,
        monkeypatch,
        "<html><body></body></html>",
        _LOADERS + "script.src='/static/vendor/force-graph.min.mjs';",
    )

    with pytest.raises(SystemExit, match=r"referenced script is missing: .*force-graph\.min\.mjs"):
        assets.check()


# ── the eager-script rules must see every spelling a browser does ───────────────────────
# HTML tag and attribute names are case-insensitive, so `<SCRIPT SRC=…>` loads exactly like
# the lowercase spelling.  A case-sensitive tag regex saw neither, which silently switched
# *both* script rules off for that tag: the CSP-hostile bundle could return to every page
# view, and its path stopped being existence-checked.  `HTMLParser` normalises the case the
# way a browser does, so these are parsed, never pattern-matched.


def test_eager_scripts_are_parsed_in_any_tag_and_attribute_case():
    found = assets._eager_scripts(
        "<html><body>"
        '<SCRIPT SRC="/static/vendor/force-graph.min.js"></SCRIPT>'
        "<Script Src='/static/engraphis-graph.js'></Script>"
        '<script src="/static/dashboard.js"></script>'
        '<script SRC="https://cdn.example.com/x.js"></script>'
        "<!-- <script src=\"/static/commented-out.js\"></script> -->"
        "<script>inline()</script>"
        "</body></html>"
    )

    assert found == [
        "/static/vendor/force-graph.min.js",
        "/static/engraphis-graph.js",
        "/static/dashboard.js",
    ]


def test_eager_script_cache_keys_are_normalized_to_asset_paths():
    found = assets._eager_scripts(
        '<script src="/static/dashboard.js?v=20260728-node-materials-v2"></script>'
        '<script src="/v2-assets/engraphis-graph.js?build=materials#ignored"></script>'
    )

    assert found == [
        "/static/dashboard.js",
        "/v2-assets/engraphis-graph.js",
    ]


def test_check_rejects_an_eagerly_loaded_script_in_any_tag_case(tmp_path, monkeypatch):
    """The uppercase spelling is loaded by browsers, so it must fail the same rule."""
    _gate(
        tmp_path,
        monkeypatch,
        '<html><body><SCRIPT SRC="/static/vendor/force-graph.min.js"></SCRIPT></body></html>',
        _LOADERS,
    )

    with pytest.raises(SystemExit, match="must not eagerly load: /static/vendor/force-graph"):
        assets.check()


def test_check_rejects_a_mixed_case_reference_to_a_missing_file(tmp_path, monkeypatch):
    """The existence check must not have a case-shaped hole either."""
    _gate(
        tmp_path,
        monkeypatch,
        "<html><body><Script SRC='/static/vendor/renamed-bundle.js'></Script></body></html>",
        _LOADERS,
    )

    with pytest.raises(SystemExit, match=r"referenced script is missing: .*renamed-bundle\.js"):
        assets.check()


def test_uppercase_script_src_is_not_mistaken_for_an_inline_block(tmp_path, monkeypatch):
    """The inline-block rule reads the same parse, so an external tag stays external."""
    _gate(
        tmp_path,
        monkeypatch,
        '<html><body><SCRIPT SRC="/static/dashboard.js"></SCRIPT></body></html>',
        _LOADERS,
    )

    assets.check()
