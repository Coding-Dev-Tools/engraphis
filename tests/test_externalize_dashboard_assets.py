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


def test_the_shipped_dashboard_passes_the_gate():
    """The real Ledger page, scripts and stylesheets satisfy the production CSP contract."""
    assets.check()


# ── a minimal dashboard tree for rule-by-rule cases ─────────────────────────────────────
# force-graph.min.js applies inline styles at runtime, so under `style-src 'self'` loading it
# on every page reported a CSP violation per attempt.  It and the graph engines are fetched on
# demand instead; these rules keep that true and keep the deferred references checked.

_DEFERRED = (
    "vendor/force-graph.min.js",
    "engraphis-graph.js",
    "engraphis-graph-every.js",
    "engraphis-spacetime.js",
)
_LOADERS = "".join(f"script.src='/v2-assets/{name}';" for name in _DEFERRED)


def _gate(tmp_path, monkeypatch, html: str, js: str = _LOADERS, css: str = "") -> None:
    root = tmp_path / "dashboard_assets"
    (root / "vendor").mkdir(parents=True)
    (root / "index.html").write_text(html, encoding="utf-8")
    (root / "ledger.js").write_text(js, encoding="utf-8")
    (root / "ledger.css").write_text(css, encoding="utf-8")
    for name in _DEFERRED:
        (root / name).write_text("// loaded on demand\n", encoding="utf-8")
    monkeypatch.setattr(assets, "ASSETS", root)
    monkeypatch.setattr(assets, "INDEX", root / "index.html")


@pytest.mark.parametrize("tag", ["script", "style"])
def test_check_rejects_unclosed_inline_asset_at_eof(tmp_path, monkeypatch, tag):
    _gate(tmp_path, monkeypatch, f"<html><body><{tag}>unclosed")

    with pytest.raises(SystemExit, match=f"inline {tag} block"):
        assets.check()


@pytest.mark.parametrize(("markup", "message"), [
    ('<p style="color:red">x</p>', "inline style attribute"),
    ("<button onclick='go()'>x</button>", "inline event attribute"),
    ('<div data-csp-style="s1">x</div>', "retired data-csp-style"),
    ('<button data-onclick="h1">x</button>', "retired data-csp-style"),
])
def test_check_rejects_inline_presentation_and_handlers(tmp_path, monkeypatch, markup, message):
    _gate(tmp_path, monkeypatch, f"<html><body>{markup}</body></html>")

    with pytest.raises(SystemExit, match=message):
        assets.check()


_STYLE_FAILURE = "runtime inline-style mutation in ledger.js"
_HANDLER_FAILURE = "inline event-handler attribute or selector in ledger.js"
_INDIRECT_FAILURE = "indirect attribute write in ledger.js"
_HTML_FAILURE = "HTML string injection in ledger.js"


@pytest.mark.parametrize(("source", "message"), [
    ("element.style.color = 'red';", _STYLE_FAILURE),
    ("element.setAttribute('style', 'color:red');", _STYLE_FAILURE),
    # Attribute names are case-insensitive, and the namespaced form sets the same attribute.
    ("element.setAttribute('STYLE', 'color:red');", _STYLE_FAILURE),
    ("element.setAttributeNS(null, 'style', 'color:red');", _STYLE_FAILURE),
    ("Object.assign(element.style, { color: 'red' });", _STYLE_FAILURE),
    ("element['style'].color = 'red';", _STYLE_FAILURE),
    ("element.style?.setProperty('color', 'red');", _STYLE_FAILURE),
    ("element.setAttribute?.('style', 'color:red');", _STYLE_FAILURE),
    ("document.createAttribute('style');", _STYLE_FAILURE),
    ("document.head.append(document.createElement('style'));", _STYLE_FAILURE),
    ("element.setAttribute('onclick', 'go()');", _HANDLER_FAILURE),
    ("element.setAttribute('ONCLICK', 'go()');", _HANDLER_FAILURE),
    ("element.setAttributeNS(null, 'onload', 'go()');", _HANDLER_FAILURE),
    ("element['onclick'] = 'go()';", _HANDLER_FAILURE),
    ("document.querySelectorAll('[onclick]');", _HANDLER_FAILURE),
    ("document.querySelector(\"button, [onload^='x']\");", _HANDLER_FAILURE),
    ("document.createAttribute('onclick');", _HANDLER_FAILURE),
    ("Element.prototype.setAttribute.call(element, name, value);", _INDIRECT_FAILURE),
    ("element['setAttribute'](name, value);", _INDIRECT_FAILURE),
    ("element.setAttributeNode(attribute);", _INDIRECT_FAILURE),
    ("target.innerHTML = label;", _HTML_FAILURE),
    ("target.innerHTML += label;", _HTML_FAILURE),
    ("target['innerHTML'] = label;", _HTML_FAILURE),
    ("Object.assign(target, { innerHTML: label });", _HTML_FAILURE),
    ("target.outerHTML = label;", _HTML_FAILURE),
    ("target.insertAdjacentHTML('beforeend', label);", _HTML_FAILURE),
    ("target.insertAdjacentHTML ('beforeend', label);", _HTML_FAILURE),
    ("document.write(label);", _HTML_FAILURE),
    ("range.createContextualFragment(label);", _HTML_FAILURE),
    ("new DOMParser().parseFromString(label, 'text/html');", _HTML_FAILURE),
    ("target.setHTMLUnsafe(label);", _HTML_FAILURE),
    ("frame.srcdoc = label;", _HTML_FAILURE),
    ("frame['srcdoc'] = label;", _HTML_FAILURE),
    ("Reflect.set(target, 'innerHTML', label);", _HTML_FAILURE),
])
def test_check_rejects_unsafe_first_party_script_patterns(tmp_path, monkeypatch, source, message):
    """Entity names and memory text are untrusted; scripts must build DOM nodes instead."""
    _gate(tmp_path, monkeypatch, "<html><body></body></html>", _LOADERS + source)

    with pytest.raises(SystemExit, match=message):
        assets.check()


@pytest.mark.parametrize("source", [
    "graph.setStyle('cyber');",
    "const palette = STYLE_PAL.cyber;",
    "node.textContent = '[Only the first 40 are shown]';",
    "if (target.innerHTML === previous) render();",
    "button.onclick = handler;",
    # Lookalikes from the security review that an earlier pattern set wrongly refused.
    "const [one, two] = pair;",
    "const next = list[ongoingIndex];",
    "const label = flag ? node.innerHTML : '';",
    "// Never use an iframe srcdoc here.",
    "if (a.style == b.style) refresh();",
    "const styleName = options['style'] || 'cyber';",
])
def test_check_allows_safe_lookalikes(tmp_path, monkeypatch, source):
    """DOM-building code that only resembles a sink must not fail the release gate."""
    _gate(tmp_path, monkeypatch, "<html><body></body></html>", _LOADERS + source)

    assets.check()


def test_check_rejects_a_missing_stylesheet(tmp_path, monkeypatch):
    _gate(
        tmp_path,
        monkeypatch,
        '<html><head><link rel="stylesheet" href="/v2-assets/missing.css?v=1"></head></html>',
    )

    with pytest.raises(SystemExit, match=r"referenced asset is missing: /v2-assets/missing\.css"):
        assets.check()


def test_check_rejects_interpolation_left_in_a_stylesheet(tmp_path, monkeypatch):
    _gate(
        tmp_path,
        monkeypatch,
        '<html><head><link rel="stylesheet" href="/v2-assets/ledger.css"></head></html>',
        css=".x { color: ${accent}; }",
    )

    with pytest.raises(SystemExit, match="unresolved JavaScript interpolation in ledger.css"):
        assets.check()


def test_check_rejects_an_eagerly_loaded_csp_hostile_script(tmp_path, monkeypatch):
    """Putting the tag back in index.html is precisely the regression this rule catches."""
    _gate(
        tmp_path,
        monkeypatch,
        '<html><body><script src="/v2-assets/vendor/force-graph.min.js"></script></body></html>',
    )

    with pytest.raises(SystemExit, match="must not eagerly load: /v2-assets/vendor/force-graph"):
        assets.check()


def test_check_rejects_an_eagerly_loaded_graph_engine(tmp_path, monkeypatch):
    _gate(
        tmp_path,
        monkeypatch,
        '<html><body><script src="/v2-assets/engraphis-graph.js"></script></body></html>',
    )

    with pytest.raises(SystemExit, match="must not eagerly load: /v2-assets/engraphis-graph"):
        assets.check()


def test_check_allows_a_deferred_script_named_only_in_a_comment(tmp_path, monkeypatch):
    """The rule reads parsed ``<script src>`` values, so index.html can still say why."""
    _gate(tmp_path, monkeypatch, "<html><body><!-- force-graph.min.js loads on demand --></body></html>")

    assets.check()


def test_check_rejects_a_deferred_script_with_no_lazy_loader(tmp_path, monkeypatch):
    """Dropping a tag without adding a loader orphans the asset instead of deferring it."""
    _gate(
        tmp_path,
        monkeypatch,
        "<html><body></body></html>",
        "script.src='/v2-assets/engraphis-graph.js';",
    )

    with pytest.raises(SystemExit, match="no lazy loader: /v2-assets/engraphis-graph-every"):
        assets.check()


def test_check_rejects_a_lazy_reference_to_a_missing_file(tmp_path, monkeypatch):
    """A deferred asset's only reference is a JS string literal — nothing else catches a rename."""
    _gate(
        tmp_path,
        monkeypatch,
        "<html><body></body></html>",
        _LOADERS + "script.src='/v2-assets/vendor/force-graph.min.mjs';",
    )

    with pytest.raises(SystemExit, match=r"referenced asset is missing: .*force-graph\.min\.mjs"):
        assets.check()


def test_check_follows_graph_asset_loader_and_worker_references(tmp_path, monkeypatch):
    _gate(
        tmp_path,
        monkeypatch,
        "<html><body></body></html>",
        _LOADERS
        + "loadScript(graphAssetSource('/v2-assets/renamed-engine.js?v=1'), 'X');"
        + "const WORKER_URL = '/v2-assets/renamed-worker.js?v=1';",
    )

    with pytest.raises(SystemExit, match=r"renamed-engine\.js.*renamed-worker\.js"):
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
        '<SCRIPT SRC="/v2-assets/vendor/force-graph.min.js"></SCRIPT>'
        "<Script Src='/v2-assets/engraphis-graph.js'></Script>"
        '<script src="/v2-assets/ledger.js"></script>'
        '<script SRC="https://cdn.example.com/x.js"></script>'
        "<!-- <script src=\"/v2-assets/commented-out.js\"></script> -->"
        "<script>inline()</script>"
        "</body></html>"
    )

    assert found == [
        "/v2-assets/vendor/force-graph.min.js",
        "/v2-assets/engraphis-graph.js",
        "/v2-assets/ledger.js",
    ]


def test_eager_script_cache_keys_are_normalized_to_asset_paths():
    found = assets._eager_scripts(
        '<script src="/v2-assets/ledger.js?v=20261009-ledger-only-1"></script>'
        '<script src="/v2-assets/engraphis-graph.js?build=materials#ignored"></script>'
    )

    assert found == [
        "/v2-assets/ledger.js",
        "/v2-assets/engraphis-graph.js",
    ]


def test_check_rejects_an_eagerly_loaded_script_in_any_tag_case(tmp_path, monkeypatch):
    """The uppercase spelling is loaded by browsers, so it must fail the same rule."""
    _gate(
        tmp_path,
        monkeypatch,
        '<html><body><SCRIPT SRC="/v2-assets/vendor/force-graph.min.js"></SCRIPT></body></html>',
    )

    with pytest.raises(SystemExit, match="must not eagerly load: /v2-assets/vendor/force-graph"):
        assets.check()


def test_check_rejects_a_mixed_case_reference_to_a_missing_file(tmp_path, monkeypatch):
    """The existence check must not have a case-shaped hole either."""
    _gate(
        tmp_path,
        monkeypatch,
        "<html><body><Script SRC='/v2-assets/vendor/renamed-bundle.js'></Script></body></html>",
    )

    with pytest.raises(SystemExit, match=r"referenced asset is missing: .*renamed-bundle\.js"):
        assets.check()


def test_uppercase_script_src_is_not_mistaken_for_an_inline_block(tmp_path, monkeypatch):
    """The inline-block rule reads the same parse, so an external tag stays external."""
    _gate(
        tmp_path,
        monkeypatch,
        '<html><body><SCRIPT SRC="/v2-assets/ledger.js"></SCRIPT></body></html>',
    )

    assets.check()
