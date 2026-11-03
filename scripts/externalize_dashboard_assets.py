"""Validate the dashboard's assets for a strict production CSP.

The dashboard (Ledger) is one page, ``engraphis/dashboard_assets/index.html``, served under
``/v2-assets/``.  Production sends ``script-src 'self'; script-src-attr 'none';
style-src 'self'; style-src-attr 'none'``, so every style and script must be an external
first-party file and no element may carry an inline style or event-handler attribute.
This is a read-only release gate: CI must fail on drift, never rewrite a checkout and pass.

It checks that:

* ``index.html`` has no inline ``<style>``/``<script>`` blocks, ``style=`` or ``on*=``
  attributes, or the retired ``data-csp-style``/``data-on*`` registry markers;
* every stylesheet and script the page loads, eagerly or through a lazy loader, exists;
* CSP-hostile graph bundles are only ever loaded on demand, and each has a loader;
* first-party scripts never build inline styles or handlers at runtime.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "engraphis" / "dashboard_assets"
INDEX = ASSETS / "index.html"
ASSET_PREFIX = "/v2-assets/"

#: Scripts that must never be referenced from a ``<script src>`` in ``index.html``.
#: ``force-graph.min.js`` applies inline styles at runtime; under ``style-src 'self'`` the
#: browser blocks and reports every one of them, so loading it on every page (rather than
#: only when Explore opens) floods the console and fails unrelated browser tests.  The graph
#: engines are large and only needed by Explore.  ``ledger.js`` fetches all of them on demand.
DEFERRED_SCRIPTS = (
    "/v2-assets/vendor/force-graph.min.js",
    "/v2-assets/engraphis-graph.js",
    "/v2-assets/engraphis-graph-every.js",
    "/v2-assets/engraphis-spacetime.js",
)

#: ``script.src = "/v2-assets/…"`` or ``graphAssetSource("/v2-assets/…")`` inside a
#: first-party script — the lazy loaders.  Deferring a script moves it out of the parsed
#: ``<script src>`` set, so without these the existence rule would stop covering the assets
#: whose breakage is hardest to notice.
LAZY_SCRIPT_SRC = re.compile(r'\.src\s*=\s*["\'](/v2-assets/[^"\']+)["\']')
LAZY_GRAPH_ASSET_SRC = re.compile(r'graphAssetSource\(\s*["\'](/v2-assets/[^"\']+)["\']')
WORKER_URL = re.compile(r'WORKER_URL\s*=\s*["\'](/v2-assets/[^"\']+)["\']')
# Browsers close raw-text script/style elements even when a malformed end tag carries
# attributes. ``HTMLParser`` only gained matching behavior in newer Python releases, so
# canonicalize those end tags for the parser while preserving every source offset.
MALFORMED_ASSET_END = re.compile(r"</(script|style)(?=\s)[^>]*>", re.IGNORECASE)

STYLE_ATTR = re.compile(r"\sstyle\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
EVENT_ATTR = re.compile(r"\son[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
REGISTRY_ATTR = re.compile(r"\sdata-(?:csp-style|on[a-z]+)\s*=", re.IGNORECASE)
# Attribute names are case-insensitive in HTML, so ``setAttribute('STYLE', …)`` and the
# namespaced form create the same blocked attribute as the lowercase spelling. These are
# drift heuristics for first-party code, not a sandbox: the browser's CSP is the boundary,
# and the browser suites fail on any violation it reports.
_ATTRIBUTE_CALL = r"(?:get|set|create)Attribute(?:NS)?\s*(?:\?\.\s*)?\(\s*(?:[^,()]+,\s*)?"
RUNTIME_STYLE = re.compile(
    # ``el.style.x``, ``el.style = …``, ``el.style?.…`` and ``el.style[…]``; not ``a.style == b``.
    r"\.(?:style|cssText)\s*(?:[.\[]|\?\.|=(?!=))"
    r"|\[\s*[\"'`](?:style|cssText)[\"'`]\s*\]\s*(?:[.\[]|\?\.|=(?!=))"
    r"|" + _ATTRIBUTE_CALL + r"[\"'`](?i:style)[\"'`]"
    r"|createElement(?:NS)?\s*\(\s*(?:[^,()]+,\s*)?[\"'`](?i:style)[\"'`]"
    r"|Object\.assign\(\s*[^,()]*\.style\s*,"
)
RUNTIME_HANDLER = re.compile(
    # An ``[onclick]`` attribute selector inside a selector string. The lookbehind keeps
    # array destructuring (``const [one, two]``) and indexing (``list[ongoing]``) out.
    r"(?<=[\"'`\s,>+~(])\[on[a-z]+\s*(?:[~|^$*]?=|\])"
    r"|" + _ATTRIBUTE_CALL + r"[\"'`](?i:on[a-z]+)[\"'`]"
    r"|\[\s*[\"'`](?i:on[a-z]+)[\"'`]\s*\]"
)
# Indirect attribute writes hide the attribute name from every check above.
INDIRECT_ATTRIBUTE = re.compile(
    r"\[\s*[\"'`](?:get|set)Attribute(?:NS)?[\"'`]\s*\]"
    r"|\b(?:get|set)Attribute(?:NS)?\s*\.\s*(?:call|apply|bind)\s*\("
    r"|\bsetAttributeNode(?:NS)?\s*\("
)
# Every way a script can turn a string into markup: assignment (including ``+=``), the
# bracket and object-literal spellings of the same properties, and the parser entry points.
HTML_INJECTION = re.compile(
    r"\.(?:inner|outer)HTML\s*\+?=(?!=)"
    r"|\[\s*[\"'`](?:inner|outer)HTML[\"'`]\s*\]"
    # Object-literal keys only, so a ternary such as ``flag ? node.innerHTML : ''`` passes.
    r"|[{,]\s*[\"'`]?(?:inner|outer)HTML[\"'`]?\s*:"
    r"|\bReflect\s*\.\s*set\s*\(\s*[^,()]+,\s*[\"'`](?:inner|outer)HTML[\"'`]"
    r"|\binsertAdjacentHTML\s*\("
    r"|\bdocument\s*\.\s*write(?:ln)?\s*\("
    r"|\b(?:createContextualFragment|parseFromString|setHTMLUnsafe)\s*\("
    r"|\.\s*srcdoc\b|[\"'`]srcdoc[\"'`]"
)


@dataclass(frozen=True)
class _InlineAsset:
    start: int
    end: int
    content: str


class _InlineAssetParser(HTMLParser):
    """Locate inline assets and external references with an HTML parser, not a regex.

    Browser HTML parsing accepts malformed closing tags and mixed-case tag names;
    using ``HTMLParser`` keeps the release gate aligned with that parsing model.
    """

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self._line_offsets = [0]
        self._line_offsets.extend(
            index + 1 for index, char in enumerate(source) if char == "\n"
        )
        self._open: dict[str, tuple[int, int]] = {}
        self.styles: list[_InlineAsset] = []
        self.scripts: list[_InlineAsset] = []
        #: ``src`` of every ``<script src>`` the page loads eagerly, in document order.
        self.script_srcs: list[str] = []
        #: ``href`` of every ``<link rel="stylesheet">``, in document order.
        self.stylesheet_hrefs: list[str] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        values = {name.lower(): value for name, value in attrs}
        if tag == "link":
            rel = (values.get("rel") or "").lower().split()
            if "stylesheet" in rel and values.get("href"):
                self.stylesheet_hrefs.append(values["href"] or "")
            return
        if tag not in {"style", "script"}:
            return
        if tag == "script" and values.get("src"):
            # ``HTMLParser`` lower-cases tag and attribute names exactly like a browser, so
            # ``<SCRIPT SRC=…>`` — which browsers load eagerly — is recorded here rather
            # than slipping past a case-sensitive tag regex.
            self.script_srcs.append(values["src"] or "")
            return
        start = self._offset()
        self._open[tag] = (start, start + len(self.get_starttag_text()))

    def handle_endtag(self, tag: str) -> None:
        opened = self._open.pop(tag, None)
        if opened is None:
            return
        close_start = self._offset()
        close_end = self.source.find(">", close_start)
        if close_end < 0:
            return
        asset = _InlineAsset(opened[0], close_end + 1, self.source[opened[1]:close_start])
        (self.styles if tag == "style" else self.scripts).append(asset)

    def finish_unclosed(self) -> None:
        """Treat an asset tag that reaches EOF as inline browser content."""

        for tag, opened in self._open.items():
            asset = _InlineAsset(opened[0], len(self.source), self.source[opened[1]:])
            (self.styles if tag == "style" else self.scripts).append(asset)
        self._open.clear()


def _parse_page(html: str) -> _InlineAssetParser:
    parser = _InlineAssetParser(html)

    def canonical_end_tag(match: re.Match[str]) -> str:
        raw = match.group(0)
        prefix = f"</{match.group(1)}"
        # Keep total length and newline positions stable so parser offsets continue to
        # address the original source.
        middle = raw[len(prefix):-1]
        return prefix + "".join("\n" if char == "\n" else " " for char in middle) + ">"

    parser.feed(MALFORMED_ASSET_END.sub(canonical_end_tag, html))
    parser.close()
    parser.finish_unclosed()
    return parser


def _inline_assets(html: str) -> tuple[list[_InlineAsset], list[_InlineAsset]]:
    parser = _parse_page(html)
    return parser.styles, parser.scripts


def _local(references: list[str]) -> list[str]:
    return [urlsplit(reference).path for reference in references
            if reference.startswith(ASSET_PREFIX)]


def _eager_scripts(html: str) -> list[str]:
    """Path of every locally served script the page loads on view.

    Parsed, never pattern-matched: a case-sensitive tag regex misses ``<SCRIPT SRC=…>``,
    which browsers load exactly like the lowercase spelling.  That gap would let a
    CSP-hostile bundle back onto every page view *and* drop it from the existence check.
    """
    return _local(_parse_page(html).script_srcs)


def _stylesheets(html: str) -> list[str]:
    return _local(_parse_page(html).stylesheet_hrefs)


def _asset_path(reference: str) -> Path:
    return ASSETS / reference[len(ASSET_PREFIX):]


def _first_party_scripts() -> list[Path]:
    """Every script the dashboard serves that we author (vendored bundles excluded)."""
    return sorted(path for path in ASSETS.glob("*.js") if path.is_file())


def check() -> None:
    html = INDEX.read_text(encoding="utf-8")
    failures: list[str] = []
    style_assets, script_assets = _inline_assets(html)
    if style_assets:
        failures.append("inline style block")
    if script_assets:
        failures.append("inline script block")
    if STYLE_ATTR.search(html):
        failures.append("inline style attribute")
    if EVENT_ATTR.search(html):
        failures.append("inline event attribute")
    if REGISTRY_ATTR.search(html):
        failures.append("retired data-csp-style/data-on* attribute")

    scripts = {path.name: path.read_text(encoding="utf-8") for path in _first_party_scripts()}
    eager_scripts = _eager_scripts(html)
    stylesheets = _stylesheets(html)
    lazy_scripts = [
        urlsplit(reference).path
        for source in scripts.values()
        for pattern in (LAZY_SCRIPT_SRC, LAZY_GRAPH_ASSET_SRC, WORKER_URL)
        for reference in pattern.findall(source)
    ]

    # Every first-party asset the page can ask for must exist, or the dashboard 404s at load
    # time and the strict CSP turns a typo into a silently broken view.  Lazily loaded scripts
    # count: their only reference is a JavaScript string literal.
    absent = sorted({
        reference
        for reference in [*eager_scripts, *lazy_scripts, *stylesheets]
        if not _asset_path(reference).is_file()
    })
    if absent:
        failures.append("referenced asset is missing: " + ", ".join(absent))

    # Matched against parsed ``<script src>`` values, not raw text, so index.html may still
    # explain in a comment *why* these are absent.
    eager = sorted(set(eager_scripts) & set(DEFERRED_SCRIPTS))
    if eager:
        failures.append("index.html must not eagerly load: " + ", ".join(eager))

    # The other half of the contract: deferring a script must not orphan it.
    orphaned = sorted(set(DEFERRED_SCRIPTS) - set(lazy_scripts))
    if orphaned:
        failures.append("deferred script has no lazy loader: " + ", ".join(orphaned))

    for name, source in scripts.items():
        if STYLE_ATTR.search(source) and re.search(r"<[a-z][^>]*\sstyle\s*=", source, re.I):
            failures.append(f"inline style attribute in {name}")
        if re.search(r"<[a-z][^>]*\son[a-z]+\s*=", source, re.I):
            failures.append(f"inline event attribute in {name}")
        if RUNTIME_STYLE.search(source):
            failures.append(f"runtime inline-style mutation in {name}")
        if RUNTIME_HANDLER.search(source):
            failures.append(f"inline event-handler attribute or selector in {name}")
        if INDIRECT_ATTRIBUTE.search(source):
            failures.append(f"indirect attribute write in {name}")
        if HTML_INJECTION.search(source):
            failures.append(f"HTML string injection in {name}")

    for reference in stylesheets:
        path = _asset_path(reference)
        css = path.read_text(encoding="utf-8") if path.is_file() else ""
        if "${" in css or "'+" in css or "+'" in css:
            failures.append(f"unresolved JavaScript interpolation in {path.name}")

    if failures:
        raise SystemExit("dashboard CSP check failed: " + ", ".join(failures))


if __name__ == "__main__":
    check()
