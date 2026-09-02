from __future__ import annotations

import html
from pathlib import Path


WIDTH = 1600
HEIGHT = 1240
OUT = Path(__file__).with_name("engraphis-v2-architecture.svg")


lines: list[str] = []
late_labels: list[str] = []


def add(value: str) -> None:
    lines.append(value)


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def text(x: float, y: float, value: str, *, size: float = 14, fill: str = "#0f172a",
         weight: str = "400", anchor: str = "start", letter: str = "0") -> None:
    add(
        f'<text x="{x}" y="{y}" font-size="{size}px" fill="{fill}" '
        f'font-weight="{weight}" text-anchor="{anchor}" letter-spacing="{letter}px">'
        f'{esc(value)}</text>'
    )


def rect(x: float, y: float, w: float, h: float, *, fill: str = "#ffffff",
         stroke: str = "#cbd5e1", width: float = 1, radius: float = 12,
         dash: str = "") -> None:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    add(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{width}"{dash_attr}/>'
    )


def region(x: float, y: float, w: float, h: float, title: str, fill: str) -> None:
    rect(x, y, w, h, fill=fill, stroke="#cbd5e1", width=1.2, radius=18, dash="8 6")
    text(x + 20, y + 27, title, size=12, fill="#475569", weight="700", letter="1.2")


def node(x: float, y: float, w: float, h: float, title: str, subtitle: str,
         accent: str, *, fill: str = "#ffffff", title_size: float = 15,
         subtitle_size: float = 11.5) -> None:
    rect(x, y, w, h, fill=fill, stroke="#cbd5e1", width=1.2, radius=12)
    rect(x, y, 7, h, fill=accent, stroke=accent, width=0, radius=4)
    text(x + 20, y + 30, title, size=title_size, weight="700")
    text(x + 20, y + 53, subtitle, size=subtitle_size, fill="#475569")


def storage_node(x: float, y: float, w: float, h: float, title: str,
                 bullets: list[str], accent: str) -> None:
    rect(x, y, w, h, fill="#ffffff", stroke="#cbd5e1", width=1.2, radius=12)
    rect(x, y, 7, h, fill=accent, stroke=accent, width=0, radius=4)
    text(x + 20, y + 29, title, size=14.5, weight="700")
    for index, bullet in enumerate(bullets):
        yy = y + 53 + index * 20
        add(f'<circle cx="{x + 24}" cy="{yy - 4}" r="2.5" fill="{accent}"/>')
        text(x + 34, yy, bullet, size=11.5, fill="#475569")


def path(points: list[tuple[float, float]], color: str, marker: str, *, dash: str = "",
         width: float = 2, opacity: float = 1.0) -> None:
    data = "M " + " L ".join(f"{x},{y}" for x, y in points)
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    add(
        f'<path d="{data}" fill="none" stroke="{color}" stroke-width="{width}" '
        f'stroke-linecap="round" stroke-linejoin="round" marker-end="url(#{marker})" '
        f'opacity="{opacity}"{dash_attr}/>'
    )


def label(x: float, y: float, value: str, *, color: str = "#475569", anchor: str = "middle") -> None:
    # Render labels after nodes so a short label never disappears beneath a box.
    late_labels.append(
        f'<text x="{x}" y="{y}" font-size="10.5px" fill="{color}" '
        f'font-weight="600" text-anchor="{anchor}">{esc(value)}</text>'
    )


add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}">')
add("  <defs>")
add('    <style>text { font-family: Inter, Segoe UI, Arial, sans-serif; }</style>')
add('    <marker id="arrow-blue" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><path d="M 0,0 L 10,3.5 L 0,7 z" fill="#2563eb"/></marker>')
add('    <marker id="arrow-green" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><path d="M 0,0 L 10,3.5 L 0,7 z" fill="#059669"/></marker>')
add('    <marker id="arrow-purple" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><path d="M 0,0 L 10,3.5 L 0,7 z" fill="#7c3aed"/></marker>')
add('    <marker id="arrow-orange" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><path d="M 0,0 L 10,3.5 L 0,7 z" fill="#ea580c"/></marker>')
add('    <marker id="arrow-gray" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><path d="M 0,0 L 10,3.5 L 0,7 z" fill="#64748b"/></marker>')
add("  </defs>")
add('<rect x="0" y="0" width="1600" height="1240" fill="#f8fafc"/>')

text(56, 52, "How Engraphis works", size=28, weight="700")
text(56, 82, "v2 local-first agent memory: scoped facts in, grounded context out", size=15, fill="#475569")
text(1544, 52, "CURRENT V2 ARCHITECTURE", size=11, fill="#2563eb", weight="700", anchor="end", letter="1.4")
text(1544, 78, "schema 16 · legacy v1 omitted", size=11.5, fill="#64748b", anchor="end")

region(48, 110, 1504, 120, "ENTRY POINTS & INPUTS", "#eff6ff")
region(48, 260, 1504, 142, "TRANSPORT + COMPOSITION ROOT", "#f0fdf4")
region(48, 432, 1504, 374, "CORE ORCHESTRATION", "#faf5ff")
region(48, 836, 1504, 182, "PERSISTENCE + DERIVED INDEXES", "#f8fafc")
region(48, 1048, 1504, 114, "INVARIANTS THAT SHAPE EVERY OPERATION", "#fff7ed")

# Entry-point and composition arrows.
path([(480, 230), (480, 255), (255, 255), (255, 300)], "#2563eb", "arrow-blue", width=2.2)
label(366, 249, "tool / SDK calls", color="#2563eb")
path([(1110, 230), (1110, 286)], "#ea580c", "arrow-orange", width=1.8)
label(1150, 263, "ingest / index", color="#ea580c", anchor="start")
path([(1400, 230), (1400, 300)], "#ea580c", "arrow-orange", width=1.8)
label(1440, 263, "optional", color="#ea580c", anchor="start")
path([(420, 336), (510, 336)], "#2563eb", "arrow-blue", width=2)
label(465, 326, "validated", color="#2563eb")
path([(810, 336), (900, 336)], "#2563eb", "arrow-blue", width=2)
label(855, 326, "constructs", color="#2563eb")
path([(1320, 336), (1250, 336)], "#ea580c", "arrow-orange", width=1.8)
label(1285, 326, "injects", color="#ea580c")

# Write path arrows: dashed green means memory write.
write_y = 537
path([(276, write_y), (300, write_y)], "#059669", "arrow-green", dash="7 5", width=2)
path([(488, write_y), (512, write_y)], "#059669", "arrow-green", dash="7 5", width=2)
path([(717, write_y), (741, write_y)], "#059669", "arrow-green", dash="7 5", width=2)
path([(961, write_y), (985, write_y)], "#059669", "arrow-green", dash="7 5", width=2)
path([(1195, write_y), (1219, write_y)], "#059669", "arrow-green", dash="7 5", width=2)
label(288, 480, "raw", color="#059669")
label(500, 480, "facts", color="#059669")
label(729, 480, "decision", color="#059669")
label(973, 480, "links", color="#059669")
label(1207, 480, "receipt", color="#059669")

# Read path arrows: blue means the primary request/data path.
read_y = 698
path([(290, read_y), (330, read_y)], "#2563eb", "arrow-blue", width=2.2)
path([(580, read_y), (630, read_y)], "#2563eb", "arrow-blue", width=2.2)
path([(845, read_y), (875, read_y)], "#2563eb", "arrow-blue", width=2.2)
path([(1055, read_y), (1085, read_y)], "#2563eb", "arrow-blue", width=2.2)
path([(1290, read_y), (1325, read_y)], "#2563eb", "arrow-blue", width=2.2)
label(310, 648, "scope + time", color="#2563eb")
label(605, 648, "candidates", color="#2563eb")
label(860, 648, "ranked", color="#2563eb")
label(1070, 648, "packed", color="#2563eb")
label(1307, 648, "cite / abstain", color="#2563eb")

# Write/read connections to local state. These use open corridors between rows.
path([(615, 582), (615, 620), (600, 620), (600, 820), (710, 820), (710, 878)], "#7c3aed", "arrow-purple", width=1.8)
label(658, 612, "embeddings", color="#7c3aed")
path([(851, 582), (851, 620), (310, 620), (310, 878)], "#059669", "arrow-green", dash="7 5", width=1.8)
label(565, 612, "bi-temporal rows", color="#059669")
path([(1090, 582), (1090, 620), (1055, 620), (1055, 878)], "#7c3aed", "arrow-purple", width=1.8)
label(1110, 612, "graph bridges", color="#7c3aed", anchor="start")
path([(1329, 582), (1329, 620), (1540, 620), (1540, 850), (1375, 850), (1375, 878)], "#64748b", "arrow-gray", dash="5 4", width=1.6)
label(1450, 812, "audit + sync", color="#64748b")

# Read connections from persistent state, routed below the read row.
path([(310, 878), (310, 820), (875, 820), (875, 736)], "#059669", "arrow-green", width=1.8)
label(585, 812, "history", color="#059669")
path([(710, 878), (710, 820), (575, 820), (575, 736)], "#059669", "arrow-green", width=1.8)
label(642, 812, "vector / FTS", color="#059669")
path([(1055, 878), (1055, 820), (600, 820), (600, 760), (580, 760), (580, 736)], "#059669", "arrow-green", width=1.8)
label(830, 812, "graph / code", color="#059669")

# Input surfaces.
node(80, 145, 250, 62, "Agent / host LLM", "remember · recall · actions", "#2563eb", fill="#ffffff")
node(355, 145, 250, 62, "MCP tools", "smart + classic surfaces", "#2563eb", fill="#ffffff")
node(630, 145, 250, 62, "CLI + dashboard", "local HTTP / graph views", "#2563eb", fill="#ffffff")
node(960, 145, 300, 62, "Local docs / repo", "document import + code index", "#ea580c", fill="#ffffff")
node(1300, 145, 200, 62, "Optional backends", "LLM · models · sync", "#ea580c", fill="#ffffff", title_size=14)

# Composition and orchestration.
node(90, 300, 330, 72, "MemoryService", "validate · resolve names · return JSON", "#2563eb", fill="#f8fbff")
node(510, 290, 300, 92, "factory.py", "select + inject concrete adapters", "#ea580c", fill="#fffaf5")
node(900, 286, 350, 100, "MemoryEngine", "write + recall orchestration", "#7c3aed", fill="#fbf8ff", title_size=17)
node(1320, 300, 200, 72, "Protocols", "embedder · index · LLM", "#ea580c", fill="#fffaf5", title_size=14)

# Write path.
node(88, 492, 188, 90, "remember / ingest", "facts enter", "#059669", fill="#f0fdf4", title_size=14)
node(300, 492, 188, 90, "optional extract", "raw → discrete facts", "#7c3aed", fill="#faf5ff", title_size=14)
node(512, 492, 205, 90, "embed + resolve", "ADD · NOOP · INVALIDATE", "#7c3aed", fill="#faf5ff", title_size=14)
node(741, 492, 220, 90, "append / close validity", "never overwrite history", "#059669", fill="#f0fdf4", title_size=14)
node(985, 492, 210, 90, "evolve + reinforce", "links · neighbors · decay", "#7c3aed", fill="#faf5ff", title_size=14)
node(1219, 492, 220, 90, "audit + receipt", "hashed, content-free trail", "#64748b", fill="#f8fafc", title_size=14)

# Read path.
node(90, 660, 200, 76, "recall(query, filter)", "scope + valid_at + known_at", "#2563eb", fill="#eff6ff", title_size=14)
node(330, 660, 250, 76, "planner + 4 retrieval arms", "vector · lexical · graph · code", "#2563eb", fill="#eff6ff", title_size=14)
node(630, 660, 215, 76, "fuse + rerank", "RRF + weighted score", "#7c3aed", fill="#faf5ff", title_size=14)
node(875, 660, 180, 76, "pack context", "hard token budget", "#2563eb", fill="#eff6ff", title_size=14)
node(1085, 660, 205, 76, "grounded gate", "absolute support floor", "#7c3aed", fill="#faf5ff", title_size=14)
node(1325, 660, 190, 76, "answer", "citations or abstain", "#059669", fill="#f0fdf4", title_size=14)

# Persistent state.
storage_node(90, 878, 420, 110, "SQLite v2 Store", [
    "typed + scoped memories",
    "validity + system-time history",
    "events · jobs · audit",
], "#059669")
storage_node(550, 878, 300, 110, "Derived indexes", [
    "mem_vectors: NumPy / sqlite-vec",
    "mem_fts: FTS5 or LIKE fallback",
    "normalized embeddings",
], "#7c3aed")
storage_node(900, 878, 320, 110, "Knowledge + code graphs", [
    "entities + layered edges",
    "symbols + calls/imports",
    "memory ↔ code bridges",
], "#2563eb")
storage_node(1250, 878, 270, 110, "Receipts + sync", [
    "operation receipts",
    "source manifests",
    "tombstones + cursors",
], "#64748b")

# Arrow labels sit above/below their corridors and remain visible over node paint.
lines.extend(late_labels)

# Cross-cutting invariants.
node(80, 1084, 235, 56, "Scopes", "workspace → repo → session", "#ea580c", fill="#fffaf5", title_size=13.5, subtitle_size=11)
node(340, 1084, 235, 56, "Memory types", "working · episodic · semantic · procedural", "#ea580c", fill="#fffaf5", title_size=13.5, subtitle_size=10.2)
node(600, 1084, 255, 56, "Bi-temporal truth", "valid time + known time", "#ea580c", fill="#fffaf5", title_size=13.5, subtitle_size=11)
node(880, 1084, 280, 56, "Provenance + governance", "trust · review · secure erasure", "#ea580c", fill="#fffaf5", title_size=13.5, subtitle_size=11)
node(1185, 1084, 335, 56, "Grounded output", "cited evidence or explicit abstain", "#ea580c", fill="#fffaf5", title_size=13.5, subtitle_size=11)

# Legend and footer.
text(56, 1195, "Flow semantics", size=11, fill="#475569", weight="700")
path([(165, 1191), (205, 1191)], "#2563eb", "arrow-blue", width=2)
text(216, 1195, "request / data", size=10.5, fill="#475569")
path([(330, 1191), (370, 1191)], "#059669", "arrow-green", width=2)
text(381, 1195, "memory read", size=10.5, fill="#475569")
path([(495, 1191), (535, 1191)], "#059669", "arrow-green", dash="7 5", width=2)
text(546, 1195, "memory write", size=10.5, fill="#475569")
path([(680, 1191), (720, 1191)], "#7c3aed", "arrow-purple", width=2)
text(731, 1195, "transform / feedback", size=10.5, fill="#475569")
path([(900, 1191), (940, 1191)], "#ea580c", "arrow-orange", width=2)
text(951, 1195, "control / trigger", size=10.5, fill="#475569")
text(1544, 1195, "Local-first by default; optional heavy backends stay behind interfaces.", size=10.5, fill="#64748b", anchor="end")
text(56, 1220, "Diagram reflects the current v2 core, backends, service facade, and schema documented in this repository.", size=10.5, fill="#94a3b8")
text(1544, 1220, "Engraphis", size=10.5, fill="#94a3b8", anchor="end")

add("</svg>")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"Wrote {OUT}")
