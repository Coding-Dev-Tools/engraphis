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


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "engraphis" / "static"
INDEX = STATIC / "index.html"
CSS = STATIC / "dashboard.css"
JS = STATIC / "dashboard.js"

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

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag not in {"style", "script"}:
            return
        if tag == "script" and any(name.lower() == "src" for name, _value in attrs):
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


def _inline_assets(html: str) -> tuple[list[_InlineAsset], list[_InlineAsset]]:
    parser = _InlineAssetParser(html)
    parser.feed(html)
    parser.close()
    parser.finish_unclosed()
    return parser.styles, parser.scripts


def _replace_assets(html: str, replacements: list[tuple[_InlineAsset, str]]) -> str:
    for asset, replacement in sorted(replacements, key=lambda item: item[0].start, reverse=True):
        html = html[:asset.start] + replacement + html[asset.end:]
    return html


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

    CSS.write_text(css, encoding="utf-8", newline="\n")
    JS.write_text(js, encoding="utf-8", newline="\n")
    INDEX.write_text(html, encoding="utf-8", newline="\n")
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
    if STYLE_ATTR.search(js):
        failures.append("inline style attribute in generated dashboard markup")
    if EVENT_ATTR.search(js):
        failures.append("inline event attribute in generated dashboard markup")
    if re.search(r"\.(?:style|cssText)\b|(?:get|set)Attribute\([\"']style[\"']", js):
        failures.append("runtime inline-style mutation")
    if re.search(r"\[on[a-z]+|(?:get|set)Attribute\([\"']on[a-z]+[\"']", js):
        failures.append("legacy inline-handler selector")
    if "${" in css or "'+" in css or "+'" in css:
        failures.append("unresolved JavaScript interpolation in CSS")

    style_refs = set(STYLE_REF.findall(html + "\n" + js))
    style_rules = set(STYLE_RULE.findall(css))
    missing_styles = sorted(style_refs - style_rules)
    if missing_styles:
        failures.append("missing CSP style rules: " + ", ".join(missing_styles))

    handler_refs = HANDLER_REF.findall(html + "\n" + js)
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
