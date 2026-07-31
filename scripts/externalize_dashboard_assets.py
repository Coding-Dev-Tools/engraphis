"""Validate dashboard assets and handlers for a strict production CSP.

The dashboard historically lived in one offline HTML file.  ``migrate()`` preserves the
one-time mechanical extraction helper, but the command-line entrypoint is deliberately a
read-only release gate: CI must fail on drift, never rewrite a dirty checkout and pass it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "engraphis" / "static"
INDEX = STATIC / "index.html"
CSS = STATIC / "dashboard.css"
JS = STATIC / "dashboard.js"

#: First-party scripts the dashboard page loads besides ``dashboard.js``.  They run under the
#: same strict CSP, so they are held to the same no-inline-style/no-inline-handler contract.
#: Vendored bundles under ``static/vendor`` are excluded: they are third-party artefacts we
#: do not rewrite, and pinning them is the job of the commercial-manifest check.
V2_ASSETS = ROOT / "engraphis" / "dashboard_assets"
EXTRA_SCRIPTS = (V2_ASSETS / "engraphis-graph.js",)

#: Scripts that must never be referenced from a ``<script src>`` in ``index.html``.
#: ``force-graph.min.js`` applies inline styles at runtime; under the production CSP
#: (``style-src 'self'``) the browser blocks and reports every one of them.  Loading it on
#: *every* dashboard page rather than only when the graph is opened floods the console and
#: fails unrelated e2e assertions on a clean console.  ``dashboard.js`` fetches both on demand
#: instead (``loadForceGraph`` / ``loadGraphEngine``).
DEFERRED_SCRIPTS = ("/static/vendor/force-graph.min.js", "/v2-assets/engraphis-graph.js")

#: ``script.src = "/static/…"`` inside a first-party script — the lazy loaders.  Deferring a
#: script moves it out of the parsed ``<script src>`` set, so without this the "referenced
#: scripts exist" rule would quietly stop covering the assets whose breakage is hardest to notice.
LAZY_SCRIPT_SRC = re.compile(r'\.src\s*=\s*["\'](/(?:static|v2-assets)/[^"\']+)["\']')
# Browsers close raw-text script/style elements even when a malformed end tag carries
# attributes. ``HTMLParser`` only gained matching behavior in newer Python releases, so
# canonicalize those end tags for the parser while preserving every source offset.
MALFORMED_ASSET_END = re.compile(r"</(script|style)(?=\s)[^>]*>", re.IGNORECASE)

STYLE_ATTR = re.compile(r"\sstyle=(?:\"([^\"]*)\"|'([^']*)')")
EVENT_ATTR = re.compile(r"\s(on[a-z]+)=(?:\"([^\"]*)\"|'([^']*)')")
STYLE_REF = re.compile(r'data-csp-style=["\'](s\d+)["\']')
STYLE_RULE = re.compile(r'\[data-csp-style=["\'](s\d+)["\']\]\{')
HANDLER_REF = re.compile(r'data-on([a-z]+)=["\'](h\d+)["\']')
HANDLER_DEF = re.compile(r'^\s*(h\d+):function\(event\)\{', re.MULTILINE)
DELEGATED_EVENTS = re.compile(r"for\(const type of \[([^]]+)\]\)")


@dataclass(frozen=True)
class _InlineAsset:
    start: int
    end: int
    content: str


class _InlineAssetParser(HTMLParser):
    """Locate inline dashboard assets with an HTML parser, not a tag regex.

    Browser HTML parsing accepts malformed closing tags and mixed-case tag names;
    using ``HTMLParser`` keeps the release gate aligned with that parsing model.
    Offsets retain the source bytes exactly, so extraction remains mechanical.
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

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag not in {"style", "script"}:
            return
        if tag == "script":
            # ``HTMLParser`` lower-cases tag and attribute names exactly like a browser, so
            # ``<SCRIPT SRC=…>`` — which browsers load eagerly — is recorded here rather than
            # slipping past a case-sensitive tag regex and out of both script rules below.
            sources = [value for name, value in attrs if name.lower() == "src"]
            if sources:
                self.script_srcs.extend(value for value in sources if value)
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
            asset = _InlineAsset(
                opened[0],
                len(self.source),
                self.source[opened[1]:],
            )
            (self.styles if tag == "style" else self.scripts).append(asset)
        self._open.clear()


def _parse_page(html: str) -> _InlineAssetParser:
    parser = _InlineAssetParser(html)

    def canonical_end_tag(match: re.Match[str]) -> str:
        raw = match.group(0)
        prefix = f"</{match.group(1)}"
        # Keep total length and newline positions stable: parser offsets must continue
        # to address the original source used for mechanical extraction.
        middle = raw[len(prefix):-1]
        return prefix + "".join("\n" if char == "\n" else " " for char in middle) + ">"

    parser.feed(MALFORMED_ASSET_END.sub(canonical_end_tag, html))
    parser.close()
    parser.finish_unclosed()
    return parser


def _inline_assets(html: str) -> tuple[list[_InlineAsset], list[_InlineAsset]]:
    parser = _parse_page(html)
    return parser.styles, parser.scripts


def _eager_scripts(html: str) -> list[str]:
    """``src`` of every locally served script the page loads on view.

    Parsed, never pattern-matched: a case-sensitive tag regex misses ``<SCRIPT SRC=…>``,
    which browsers load exactly like the lowercase spelling.  That gap would let a
    CSP-hostile bundle back onto every page view *and* drop it from the existence check —
    a gate that cannot fail.
    """
    return [
        urlsplit(src).path
        for src in _parse_page(html).script_srcs
        if src.startswith(("/static/", "/v2-assets/"))
    ]


def _replace_assets(html: str, replacements: list[tuple[_InlineAsset, str]]) -> str:
    for asset, replacement in sorted(replacements, key=lambda item: item[0].start, reverse=True):
        html = html[:asset.start] + replacement + html[asset.end:]
    return html


def _write_lf(path: Path, content: str) -> None:
    # ``Path.write_text(..., newline=...)`` is Python 3.10+, while Engraphis keeps
    # Python 3.9 as its package floor.
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _add_generated_listeners(source: str, handlers: dict[tuple[str, str], str]) -> str:
    lines = [
        "",
        "/* Generated listener registry replacing CSP-blocked inline event attributes. */",
        "const CSP_EVENT_HANDLERS=Object.freeze({",
    ]
    for (event_name, body), handler_id in handlers.items():
        normalized = body.replace(r"\'", "'").replace(r'\"', '"')
        lines.append(f"{handler_id}:function(event){{{normalized}}},")
    lines.append("});")
    event_types = list(dict.fromkeys(name[2:] for name, _body in handlers))
    encoded = "[" + ",".join(repr(item) for item in event_types) + "]"
    lines.append(
        f"for(const type of {encoded}){{document.addEventListener(type,function(event){{"
        "const target=event.target instanceof Element?event.target.closest('[data-on'+type+']'):null;"
        "if(!target||!document.documentElement.contains(target))return;"
        "const handler=CSP_EVENT_HANDLERS[target.getAttribute('data-on'+type)];"
        "if(!handler)return;const result=handler.call(target,event);"
        "if(result===false){event.preventDefault();event.stopPropagation()}},false)}"
    )
    return source.rstrip() + "\n" + "\n".join(lines) + "\n"


def migrate() -> None:
    html = INDEX.read_text(encoding="utf-8")
    style_assets, script_assets = _inline_assets(html)
    if not style_assets and not script_assets:
        check()
        return

    styles: dict[str, str] = {}

    def replace_style(match: re.Match[str]) -> str:
        value = match.group(1) if match.group(1) is not None else match.group(2)
        if "${" in value or "'+" in value or "+'" in value:
            raise RuntimeError(
                "dynamic style attributes require a named CSS class before externalization"
            )
        style_id = styles.setdefault(value, f"s{len(styles) + 1}")
        return f' data-csp-style="{style_id}"'

    html = STYLE_ATTR.sub(replace_style, html)

    handlers: dict[tuple[str, str], str] = {}

    def replace_handler(match: re.Match[str]) -> str:
        event_name = match.group(1)
        body = match.group(2) if match.group(2) is not None else match.group(3)
        if "${" in body:
            raise RuntimeError(
                "dynamic event handlers require data-* arguments before externalization"
            )
        key = (event_name, body)
        handler_id = handlers.setdefault(key, f"h{len(handlers) + 1}")
        return f' data-{event_name}="{handler_id}"'

    html = EVENT_ATTR.sub(replace_handler, html).replace("[onclick]", "[data-onclick]")

    style_assets, script_assets = _inline_assets(html)
    if len(style_assets) != 1 or len(script_assets) != 1:
        raise RuntimeError("dashboard must contain exactly one inline style and script block")

    css = style_assets[0].content.rstrip()
    css += "\n\n/* Generated from former static style attributes. */\n"
    css += "".join(
        f'[data-csp-style="{style_id}"]{{{value}}}\n'
        for value, style_id in styles.items()
    )
    js = _add_generated_listeners(script_assets[0].content, handlers)
    html = _replace_assets(html, [
        (style_assets[0], '<link rel="stylesheet" href="/static/dashboard.css">'),
        (script_assets[0], '<script src="/static/dashboard.js"></script>'),
    ])

    _write_lf(CSS, css)
    _write_lf(JS, js)
    _write_lf(INDEX, html)
    check()
    print(f"externalized {len(styles)} styles and {len(handlers)} event handlers")


def check() -> None:
    html = INDEX.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8") if CSS.is_file() else ""
    js = JS.read_text(encoding="utf-8") if JS.is_file() else ""
    failures = []
    style_assets, script_assets = _inline_assets(html)
    if style_assets:
        failures.append("inline style block")
    if script_assets:
        failures.append("inline script block")
    if STYLE_ATTR.search(html):
        failures.append("inline style attribute")
    if EVENT_ATTR.search(html):
        failures.append("inline event attribute")
    if not CSS.is_file() or not JS.is_file():
        failures.append("missing external asset")
    extra = {path.name: path.read_text(encoding="utf-8") for path in EXTRA_SCRIPTS if path.is_file()}
    missing_scripts = sorted(path.name for path in EXTRA_SCRIPTS if not path.is_file())
    if missing_scripts:
        failures.append("missing first-party script: " + ", ".join(missing_scripts))

    eager_scripts = _eager_scripts(html)
    lazy_scripts = [
        urlsplit(reference).path
        for source in [js, *extra.values()]
        for reference in LAZY_SCRIPT_SRC.findall(source)
    ]

    def _script_path(reference: str) -> Path:
        if reference.startswith("/v2-assets/"):
            return V2_ASSETS / reference.removeprefix("/v2-assets/")
        return STATIC / reference.removeprefix("/static/")

    # Every first-party asset the page can ask for must exist, or the dashboard 404s at load time
    # and the strict CSP turns a typo into a silently broken view.  Lazily loaded scripts count:
    # their only reference is a JavaScript string literal, so nothing else catches a rename.
    absent = sorted(
        {
            reference
            for reference in [*eager_scripts, *lazy_scripts]
            if not _script_path(reference).is_file()
        }
    )
    if absent:
        failures.append("referenced script is missing: " + ", ".join(absent))

    # Matched against parsed ``<script src>`` values, not raw text, so index.html may still
    # explain in a comment *why* these are absent.
    eager = sorted(set(eager_scripts) & set(DEFERRED_SCRIPTS))
    if eager:
        failures.append("index.html must not eagerly load: " + ", ".join(eager))

    # The other half of the contract: deferring a script must not orphan it.  Without a loader
    # the graph view has no way to reach force-graph, and ``?graph-engine=next`` degrades to
    # the classic renderer with nothing on the page saying so.
    orphaned = sorted(set(DEFERRED_SCRIPTS) - set(lazy_scripts))
    if orphaned:
        failures.append("deferred script has no lazy loader: " + ", ".join(orphaned))

    for name, source in [("dashboard.js", js), *extra.items()]:
        if STYLE_ATTR.search(source):
            failures.append(f"inline style attribute in {name}")
        if EVENT_ATTR.search(source):
            failures.append(f"inline event attribute in {name}")
        if re.search(r"\.(?:style|cssText)\b|(?:get|set)Attribute\([\"']style[\"']", source):
            failures.append(f"runtime inline-style mutation in {name}")
        if re.search(r"\[on[a-z]+|(?:get|set)Attribute\([\"']on[a-z]+[\"']", source):
            failures.append(f"legacy inline-handler selector in {name}")
    if "${" in css or "'+" in css or "+'" in css:
        failures.append("unresolved JavaScript interpolation in CSS")

    style_refs = set(STYLE_REF.findall("\n".join([html, js, *extra.values()])))
    style_rules = set(STYLE_RULE.findall(css))
    missing_styles = sorted(style_refs - style_rules)
    if missing_styles:
        failures.append("missing CSP style rules: " + ", ".join(missing_styles))

    # Handler *definitions* only ever live in the generated dashboard.js registry, but any
    # first-party script may reference one, so references are collected across all of them.
    handler_refs = HANDLER_REF.findall("\n".join([html, js, *extra.values()]))
    handler_ids = {handler_id for _event, handler_id in handler_refs}
    handler_defs = set(HANDLER_DEF.findall(js))
    missing_handlers = sorted(handler_ids - handler_defs)
    if missing_handlers:
        failures.append("missing CSP event handlers: " + ", ".join(missing_handlers))
    delegated = DELEGATED_EVENTS.search(js)
    delegated_types = set(re.findall(r"[\"']([a-z]+)[\"']", delegated.group(1))) \
        if delegated else set()
    missing_types = sorted({event for event, _handler in handler_refs} - delegated_types)
    if missing_types:
        failures.append("undelegated CSP event types: " + ", ".join(missing_types))
    if failures:
        raise SystemExit("dashboard CSP check failed: " + ", ".join(failures))


if __name__ == "__main__":
    check()
