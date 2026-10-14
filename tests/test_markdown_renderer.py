"""Contract tests for the dashboard's Markdown renderer (``dashboard_assets/markdown.js``).

Memory content is untrusted input (SECURITY.md §1): it can come from imported files,
connected agents or a synced peer. The renderer must keep it inert -- only elements from a
fixed set, text nodes, and absolute http(s)/mailto links -- and it must stay fast on crafted
input, so a hostile memory cannot freeze the page someone uses to review and retire it.

The shipped renderer runs here under Node against a recording DOM stand-in.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_JS = ROOT / "engraphis" / "dashboard_assets" / "markdown.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to run the dashboard renderer",
)

ALLOWED_TAGS = {
    "a", "blockquote", "br", "code", "del", "div", "em", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "li", "ol", "p", "pre", "span", "strong", "table", "tbody", "td", "th", "thead",
    "tr", "ul",
}
ALLOWED_PROPERTIES = {"className", "href", "target", "rel", "title", "scope", "start"}
ALLOWED_ATTRIBUTES = {"role", "aria-label"}

_HARNESS = r"""
'use strict';
const created = [];
class Node {
  constructor(tag) {
    this.tagName = tag; this.childNodes = []; this.props = {}; this.attrs = {};
    this.dataset = {}; this.text = '';
  }
  append(...nodes) { this.childNodes.push(...nodes); }
  replaceChildren(...nodes) { this.childNodes = nodes; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  set textContent(value) { this.text = String(value); this.childNodes = []; }
  get textContent() { return this.text + this.childNodes.map(child => child.textContent).join(''); }
}
const OWN = new Set(['tagName', 'childNodes', 'props', 'attrs', 'dataset', 'text', 'textContent']);
function element(tag) {
  const node = new Node(tag);
  created.push(node);
  return new Proxy(node, {
    set(target, key, value) {
      if (OWN.has(key)) target[key] = value;
      else target.props[key] = String(value);
      return true;
    },
  });
}
class Text { constructor(data) { this.data = String(data); } get textContent() { return this.data; } }
global.window = {};
global.document = {
  createElement: element,
  createTextNode: data => new Text(data),
  createDocumentFragment: () => new Node('#fragment'),
};
require(process.argv[2]);
const md = window.EngraphisMarkdown;

function tree(node) {
  if (!(node instanceof Node)) return { text: node.data };
  return {
    tag: node.tagName, props: node.props, attrs: node.attrs, dataset: node.dataset,
    text: node.text, children: node.childNodes.map(tree),
  };
}

const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = {};
for (const [name, spec] of Object.entries(cases)) {
  const source = 'unit' in spec ? spec.prefix + spec.unit.repeat(spec.repeat) + spec.suffix
    : spec.source;
  created.length = 0;
  const started = process.hrtime.bigint();
  const fragment = md.render(source, spec.options || {});
  const elapsedMs = Number(process.hrtime.bigint() - started) / 1e6;
  out[name] = {
    elapsedMs,
    length: source.length,
    elements: created.map(node => ({
      tag: node.tagName, props: node.props, attrs: node.attrs, dataset: node.dataset,
    })),
    tree: spec.tree ? tree(fragment) : null,
    text: fragment.textContent,
  };
}
process.stdout.write(JSON.stringify(out));
"""


def _render(tmp_path, cases):
    runner = tmp_path / "markdown_harness.js"
    runner.write_text(_HARNESS, encoding="utf-8")
    result = subprocess.run(
        ["node", str(runner), str(MARKDOWN_JS)],
        input=json.dumps(cases), capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _source(text, **extra):
    return {"source": text, **extra}


_HOSTILE = {
    "js-scheme": "[x](javascript:alert(1))",
    "js-mixed-case": "[x](JaVaScRiPt:alert(1))",
    "js-leading-space": "[x]( javascript:alert(1))",
    "js-tab": "[x](\tjavascript:alert(1))",
    "js-autolink": "<javascript:alert(1)>",
    "data-url": "[x](data:text/html,<script>alert(1)</script>)",
    "vbscript": "[x](vbscript:msgbox(1))",
    "entity-scheme": "[x](&#106;avascript:alert(1))",
    "relative": "[x](/api/workspaces/delete)",
    "protocol-relative": "[x](//evil.example/path)",
    "attribute-break": '[x](http://a.example" onmouseover="alert(1))',
    "image-js": "![i](javascript:alert(1))",
    "image-remote": "![i](https://evil.example/pixel.png)",
    "raw-html": '<img src=x onerror=alert(1)><script>alert(1)</script><a href="javascript:1">a</a>',
    "fence-language": '```js" onload="alert(1)\ncode\n```',
    "wikilink-html": "[[a|<b onclick=alert(1)>]]",
    "table-cells": "| a | b |\n|---|---|\n| <i>x</i> | [y](javascript:1) |",
    "nested-link": "[a [b](http://inner.example) c](javascript:alert(1))",
    "bare-url-markup": "http://a.example/<img src=x onerror=alert(1)>",
    "nul-and-separators": "a\u0000b [x](javascript:1) c",
}


def test_hostile_markdown_renders_only_inert_elements_and_safe_links(tmp_path):
    rendered = _render(tmp_path, {name: _source(text) for name, text in _HOSTILE.items()})

    for name, result in rendered.items():
        for item in result["elements"]:
            assert item["tag"] in ALLOWED_TAGS, (name, item)
            assert set(item["props"]) <= ALLOWED_PROPERTIES, (name, item)
            assert set(item["attrs"]) <= ALLOWED_ATTRIBUTES, (name, item)
            assert set(item["dataset"]) <= {"language"}, (name, item)
            if item["tag"] == "a":
                href = item["props"]["href"]
                assert href.startswith(("http://", "https://", "mailto:")), (name, href)
                assert item["props"]["target"] == "_blank", name
                assert {"noopener", "noreferrer"} <= set(item["props"]["rel"].split()), name
            if "language" in item["dataset"]:
                assert '"' not in item["dataset"]["language"], name

    # Markup in a memory is text, not structure.
    assert "<script>alert(1)</script>" in rendered["raw-html"]["text"]
    assert not any(item["tag"] == "a" for item in rendered["raw-html"]["elements"])
    assert "<b onclick=alert(1)>" in rendered["wikilink-html"]["text"]
    for name in ("js-scheme", "js-mixed-case", "js-leading-space", "js-tab", "js-autolink",
                 "data-url", "vbscript", "entity-scheme", "relative", "protocol-relative",
                 "image-js"):
        assert not any(item["tag"] == "a" for item in rendered[name]["elements"]), name
    # Images never load: they become an ordinary link that says what it points at.
    image_links = [item for item in rendered["image-remote"]["elements"] if item["tag"] == "a"]
    assert [link["props"]["href"] for link in image_links] == ["https://evil.example/pixel.png"]
    assert "Image: i" in rendered["image-remote"]["text"]


def test_common_markdown_keeps_its_structure(tmp_path):
    rendered = _render(tmp_path, {
        "document": _source(
            "# Title ##\n\nSome *em*, **strong**, ~~gone~~ and `code`.\n\n"
            "- one\n- [x] done\n\n7. seven\n8. eight\n\n"
            "```py\nprint(1)\n```\n\n> quoted\n\n"
            "| h1 | h2 |\n|:--|--:|\n| c1 | c2 |\n\n[[Note|Alias]] and [site](https://example.com).",
        ),
        "mismatched-table": _source("| a | b | c |\n|---|---|\n| 1 | 2 | 3 |"),
        "heading-offset": _source("# Top", options={"headingOffset": 0}),
        "empty-heading": _source("# ###"),
    })

    document = rendered["document"]
    tags = [item["tag"] for item in document["elements"]]
    assert tags[0] == "h3"  # `#` nests under the page's own headings by default
    for tag in ("em", "strong", "del", "code", "ul", "ol", "li", "pre", "blockquote",
                "table", "thead", "tbody", "th", "td", "a"):
        assert tag in tags, tag
    ordered = next(item for item in document["elements"] if item["tag"] == "ol")
    assert ordered["props"]["start"] == "7"
    pre = next(item for item in document["elements"] if item["tag"] == "pre")
    assert pre["dataset"] == {"language": "py"}
    wikilink = next(item for item in document["elements"]
                    if item["props"].get("className") == "md-wikilink")
    assert wikilink["tag"] == "span"
    assert "Alias" in document["text"] and "[[" not in document["text"]
    assert "Title" in document["text"] and "##" not in document["text"]
    task = next(item for item in document["elements"] if item["props"].get("className") == "md-task-box")
    assert task["attrs"] == {"role": "img", "aria-label": "Done"}

    # GFM: a delimiter row with a different column count does not make a table.
    assert "table" not in [item["tag"] for item in rendered["mismatched-table"]["elements"]]
    assert "| a | b | c |" in rendered["mismatched-table"]["text"]
    assert rendered["heading-offset"]["elements"][0]["tag"] == "h1"
    assert rendered["empty-heading"]["text"] == ""


# Inputs from the security review that made the previous renderer spend 10-23 seconds of
# main-thread time (catastrophic regex backtracking) or create millions of elements. Each is
# about 100,000 characters, the service's memory content limit.
_ADVERSARIAL = {
    "heading-spaces": {"prefix": "# a", "unit": " ", "repeat": 99990, "suffix": "b"},
    "table-divider-spaces": {"prefix": "|\n|-", "unit": " ", "repeat": 99990, "suffix": "x"},
    "list-line-separator": {"prefix": "- ", "unit": " ", "repeat": 99990, "suffix": " "},
    "fence-line-separator": {"prefix": "```", "unit": "`", "repeat": 99990, "suffix": " "},
    "link-title-spaces": {"prefix": "", "unit": "[a](b" * 300 + " " * 1990 + "x\n",
                          "repeat": 30, "suffix": ""},
    "unclosed-brackets": {"prefix": "", "unit": "[a", "repeat": 50000, "suffix": ""},
    "unclosed-wikilinks": {"prefix": "", "unit": "[[", "repeat": 50000, "suffix": ""},
    "emphasis-runs": {"prefix": "", "unit": "*a", "repeat": 50000, "suffix": ""},
    "nested-quotes": {"prefix": "", "unit": ">", "repeat": 100000, "suffix": ""},
    "wide-table": {"prefix": "a|" * 2000 + "\n" + "-|" * 2000 + "\n", "unit": "|\n",
                   "repeat": 45000, "suffix": ""},
    "long-table": {"prefix": "a|b\n-|-\n", "unit": "1|2\n", "repeat": 25000, "suffix": ""},
    "many-line-breaks": {"prefix": "", "unit": "a\n", "repeat": 60000, "suffix": ""},
    "emphasis-over-unclosed-code": {
        "prefix": "", "unit": "", "repeat": 0,
        "suffix": "".join("*" + "`" * size + "a* " for size in range(440, 0, -1)),
    },
    # Backtick runs of decreasing length behind failing emphasis openers: one substring
    # search per distinct run length took 1.6 s in Chromium at this size (14 s at 200k).
    "decreasing-backtick-runs": {
        "prefix": "*a _a **a __a ~~a ", "unit": "", "repeat": 0,
        "suffix": "".join("`" * size + "x " for size in range(444, 0, -1)),
    },
    "nested-openers-over-backtick-runs": {
        "prefix": "~~ __ ** _ * ~~x __x **x _x *x ", "unit": "", "repeat": 0,
        "suffix": "".join("`" * size + "x " for size in range(440, 0, -1)),
    },
}


def test_crafted_markdown_cannot_freeze_or_flood_the_page(tmp_path):
    rendered = _render(tmp_path, _ADVERSARIAL)

    for name, result in rendered.items():
        assert result["length"] >= 50000, name
        # The old renderer took 10-23 s on these; linear scanning takes milliseconds. The
        # bound is loose enough for a slow CI runner and still far below a visible hang.
        assert result["elapsedMs"] < 2500, (name, result["elapsedMs"])
        assert len(result["elements"]) <= 50002, (name, len(result["elements"]))

    wide = rendered["wide-table"]["elements"]
    assert "table" not in [item["tag"] for item in wide]  # more than 64 columns is text
    rows = [item for item in rendered["long-table"]["elements"] if item["tag"] == "tr"]
    assert len(rows) == 1 + 500  # header plus the capped body
    # Running out of the element budget is said plainly rather than silently dropped.
    assert rendered["many-line-breaks"]["text"].endswith("Preview truncated.")
