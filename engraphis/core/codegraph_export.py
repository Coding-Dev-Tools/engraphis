"""Presentation-layer renderers for the code-graph export.

Pure functions of the ``export_code_graph`` payload (no store/engine access, stdlib
only) — extracted from ``core/engine.py`` so the engine facade stays a thin
orchestration layer instead of carrying a ~230-line inline HTML/JS template.
``MemoryEngine.code_graph_report`` / ``code_graph_html`` delegate here.
"""
from __future__ import annotations

import html as _html
import json as _json
import time


def render_report(payload: dict) -> str:
    """Human-readable GRAPH_REPORT.md companion to ``export_code_graph``."""
    analysis = payload["analysis"]
    lines = [
        "# Engraphis Code Graph Report",
        "",
        f"- Generated: "
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(payload['generated_at']))}",
        f"- Files indexed: {len(payload['files'])}",
        f"- Symbols: {len(payload['nodes'])}",
        f"- Relationships: {len(payload['edges'])}",
        f"- Communities: {len(analysis['communities'])}",
        "",
        "## Hotspots",
        "",
    ]
    if analysis["hotspots"]:
        lines.extend(
            f"- `{item['node']}` — degree {item['degree']}"
            for item in analysis["hotspots"]
        )
    else:
        lines.append("- No connected code nodes yet.")
    lines.extend(["", "## Communities", ""])
    for community in analysis["communities"][:20]:
        top = ", ".join(
            f"`{item['node']}` ({item['degree']})"
            for item in community["top_nodes"]
        )
        lines.append(
            f"- Community {community['id']}: {community['size']} nodes"
            + (f" — {top}" if top else "")
        )
    return "\n".join(lines) + "\n"


def render_html(payload: dict) -> str:
    """Self-contained, dependency-free graph.html export."""
    safe_json = _json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    rows = []
    for node in payload["nodes"][:5_000]:
        rows.append(
            "<tr><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                _html.escape(str(node.get("fqname") or node.get("name") or "")),
                _html.escape(str(node.get("kind") or "")),
                _html.escape(str(node.get("file") or "")),
                _html.escape(str(node.get("span") or "")),
            )
        )
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Engraphis Code Graph</title>
<style>
body{font:14px system-ui;margin:0;color:#17202a;background:#f8fafc}
main{max-width:1600px;margin:auto;padding:1.5rem}
.toolbar{display:flex;gap:.7rem;flex-wrap:wrap;align-items:center}
input,select{padding:.65rem;border:1px solid #94a3b8;border-radius:.4rem;background:white}
input{width:min(42rem,80vw)}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 18rem;gap:1rem;margin-top:1rem}
.canvas{background:#0f172a;border-radius:.6rem;overflow:hidden;min-height:36rem}
svg{width:100%;height:70vh;min-height:36rem;display:block;touch-action:none}
.edge{stroke:#64748b;stroke-opacity:.42;stroke-width:1}
.edge.memory{stroke:#f59e0b;stroke-dasharray:4 3}
.node circle{stroke:#e2e8f0;stroke-width:1.2;cursor:pointer}
.node text{fill:#e2e8f0;font:10px ui-monospace,monospace;pointer-events:none}
.node.memory circle{fill:#f59e0b}.node.external circle{fill:#64748b}
.node.code circle{fill:#38bdf8}.node.match circle{stroke:#fb7185;stroke-width:4}
aside{background:white;border:1px solid #e2e8f0;border-radius:.6rem;padding:1rem}
aside code{overflow-wrap:anywhere}.muted{color:#64748b}
table{width:100%;border-collapse:collapse;margin-top:1rem;background:white}
th,td{text-align:left;padding:.55rem;border-bottom:1px solid #e2e8f0}
code{font:12px ui-monospace,monospace}
@media(max-width:850px){.layout{grid-template-columns:1fr}aside{order:-1}}
</style></head><body>
<main>
<h1>Engraphis Code Graph</h1>
<p class="muted"><span id="summary"></span></p>
<div class="toolbar">
<input id="filter" placeholder="Filter symbols, files, kinds, or relations"
 aria-label="Filter graph">
<select id="relation" aria-label="Filter by relationship"><option value="">All relations</option>
</select>
<span class="muted">Scroll to zoom; drag the canvas to pan.</span>
</div>
<div class="layout">
<div class="canvas"><svg id="graph" role="img"
 aria-label="Interactive code and memory relationship graph"></svg></div>
<aside><h2>Selection</h2><div id="details" class="muted">
Select a node to inspect its type, file, signature, and connections.</div>
<hr><p class="muted" id="render-note"></p></aside>
</div>
<details><summary>Accessible symbol table</summary>
<table><thead><tr><th>Symbol</th><th>Kind</th><th>File</th><th>Span</th></tr></thead>
<tbody id="rows">""" + "".join(rows) + """</tbody></table></details>
<script type="application/json" id="graph-data">""" + safe_json + """</script>
<script>
const graph=JSON.parse(document.getElementById('graph-data').textContent);
document.getElementById('summary').textContent =
  `${graph.files.length} files · ${graph.nodes.length} symbols · ` +
  `${graph.edges.length} relations · ${graph.memory_links.length} memory links`;
const rows=[...document.querySelectorAll('#rows tr')];
const svg=document.getElementById('graph');
const NS='http://www.w3.org/2000/svg';
const MAX_NODES=1000,MAX_EDGES=3000;
const nodes=[],byKey=new Map();
function rememberKey(key,node){if(key&&!byKey.has(String(key)))byKey.set(String(key),node)}
function addNode(raw,kind='code'){
  if(nodes.length>=MAX_NODES)return null;
  const node={...raw,_kind:kind,_i:nodes.length};
  node.label=String(raw.fqname||raw.name||raw.title||raw.file||raw.id||'unknown');
  nodes.push(node);
  [raw.id,raw.fqname,raw.name,raw.file].forEach(key=>rememberKey(key,node));
  return node;
}
(graph.nodes||[]).slice(0,900).forEach(node=>addNode(node));
function endpoint(value){
  const key=String(value||'');
  if(!key)return null;
  return byKey.get(key)||addNode({id:`external:${key}`,name:key,kind:'external'},'external');
}
const edges=[];
(graph.edges||[]).slice(0,MAX_EDGES).forEach(edge=>{
  const source=endpoint(edge.src),target=endpoint(edge.dst);
  if(source&&target)edges.push({...edge,source,target,_kind:'code'});
});
(graph.memory_links||[]).forEach(link=>{
  if(edges.length>=MAX_EDGES)return;
  const source=byKey.get(String(link.symbol_id||''));
  let target=byKey.get(String(link.memory_id||''));
  if(!target)target=addNode({
    id:link.memory_id,name:link.title||link.memory_id,kind:link.mtype||'memory'
  },'memory');
  if(source&&target)edges.push({
    source,target,relation:link.relation||'mentions',_kind:'memory'
  });
});
const groups=new Map();
nodes.forEach(node=>{
  const group=node._kind==='memory'?'Memories':String(node.file||'(external)');
  if(!groups.has(group))groups.set(group,[]);
  groups.get(group).push(node);
});
const cols=Math.max(1,Math.ceil(Math.sqrt(groups.size)));
const cellW=300,cellH=230,width=Math.max(900,cols*cellW);
const height=Math.max(650,Math.ceil(groups.size/cols)*cellH);
svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
[...groups.entries()].forEach(([group,items],index)=>{
  const cx=(index%cols)*cellW+cellW/2;
  const cy=Math.floor(index/cols)*cellH+cellH/2;
  const radius=Math.min(92,32+items.length*2.2);
  items.forEach((node,i)=>{
    const angle=(Math.PI*2*i/Math.max(1,items.length))-(Math.PI/2);
    node.x=cx+(items.length===1?0:Math.cos(angle)*radius);
    node.y=cy+(items.length===1?0:Math.sin(angle)*radius);
    node.group=group;
  });
});
const relationSelect=document.getElementById('relation');
[...new Set(edges.map(edge=>edge.relation||''))].sort().forEach(relation=>{
  const option=document.createElement('option');option.value=relation;
  option.textContent=relation||'(unlabeled)';relationSelect.appendChild(option);
});
const edgeLayer=document.createElementNS(NS,'g');
const nodeLayer=document.createElementNS(NS,'g');
svg.append(edgeLayer,nodeLayer);
edges.forEach(edge=>{
  const line=document.createElementNS(NS,'line');
  line.setAttribute('x1',edge.source.x);line.setAttribute('y1',edge.source.y);
  line.setAttribute('x2',edge.target.x);line.setAttribute('y2',edge.target.y);
  line.setAttribute('class',`edge ${edge._kind}`);
  const title=document.createElementNS(NS,'title');
  title.textContent=edge.relation||'';line.appendChild(title);
  edge.el=line;edgeLayer.appendChild(line);
});
nodes.forEach(node=>{
  const group=document.createElementNS(NS,'g');
  group.setAttribute('class',`node ${node._kind}`);
  group.setAttribute('transform',`translate(${node.x} ${node.y})`);
  group.setAttribute('tabindex','0');group.setAttribute('role','button');
  group.setAttribute('aria-label',node.label);
  const circle=document.createElementNS(NS,'circle');
  circle.setAttribute('r',node._kind==='memory'?8:node._kind==='external'?5:6);
  const title=document.createElementNS(NS,'title');title.textContent=node.label;
  circle.appendChild(title);group.appendChild(circle);
  if(nodes.length<=260){
    const text=document.createElementNS(NS,'text');text.setAttribute('x',9);
    text.setAttribute('y',3);text.textContent=node.label.slice(0,38);group.appendChild(text);
  }
  const select=()=>showNode(node);
  group.addEventListener('click',select);
  group.addEventListener('keydown',event=>{
    if(event.key==='Enter'||event.key===' '){event.preventDefault();select()}
  });
  node.el=group;nodeLayer.appendChild(group);
});
function showNode(node){
  const connected=edges.filter(edge=>edge.source===node||edge.target===node);
  const box=document.getElementById('details');box.textContent='';
  const title=document.createElement('h3');title.textContent=node.label;box.appendChild(title);
  const facts=[
    ['Type',node._kind==='memory'?(node.kind||'memory'):(node.kind||node._kind)],
    ['File',node.file||''],['Span',node.span||''],['Group',node.group||''],
    ['Connections',String(connected.length)]
  ];
  facts.filter(item=>item[1]).forEach(([label,value])=>{
    const p=document.createElement('p'),strong=document.createElement('strong');
    strong.textContent=`${label}: `;p.append(strong,document.createTextNode(String(value)));
    box.appendChild(p);
  });
  if(node.signature){
    const pre=document.createElement('code');pre.textContent=node.signature;box.appendChild(pre);
  }
  if(node.docstring){
    const p=document.createElement('p');p.textContent=node.docstring;box.appendChild(p);
  }
  connected.slice(0,20).forEach(edge=>{
    const p=document.createElement('p');p.className='muted';
    const other=edge.source===node?edge.target:edge.source;
    p.textContent=`${edge.relation||'related'} → ${other.label}`;box.appendChild(p);
  });
}
function applyFilters(){
  const q=document.getElementById('filter').value.trim().toLowerCase();
  const relation=relationSelect.value;
  nodes.forEach(node=>{
    const match=!q||[node.label,node.file,node.kind,node.docstring]
      .some(value=>String(value||'').toLowerCase().includes(q));
    node.el.classList.toggle('match',Boolean(q&&match));
    node.el.style.opacity=match?'1':q?'.18':'1';
  });
  edges.forEach(edge=>{
    const qMatch=!q||[edge.relation,edge.source.label,edge.target.label]
      .some(value=>String(value||'').toLowerCase().includes(q));
    edge.el.style.display=(!relation||edge.relation===relation)&&qMatch?'':'none';
  });
  rows.forEach(row=>{row.hidden=Boolean(q&&!row.textContent.toLowerCase().includes(q))});
}
document.getElementById('filter').addEventListener('input',applyFilters);
relationSelect.addEventListener('change',applyFilters);
let view={x:0,y:0,w:width,h:height},drag=null;
function setView(){svg.setAttribute('viewBox',`${view.x} ${view.y} ${view.w} ${view.h}`)}
svg.addEventListener('wheel',event=>{
  event.preventDefault();const factor=event.deltaY>0?1.12:.88;
  const rect=svg.getBoundingClientRect();
  const px=view.x+(event.clientX-rect.left)/rect.width*view.w;
  const py=view.y+(event.clientY-rect.top)/rect.height*view.h;
  view.x=px-(px-view.x)*factor;view.y=py-(py-view.y)*factor;
  view.w*=factor;view.h*=factor;setView();
},{passive:false});
svg.addEventListener('pointerdown',event=>{
  drag={x:event.clientX,y:event.clientY,vx:view.x,vy:view.y};
  svg.setPointerCapture(event.pointerId);
});
svg.addEventListener('pointermove',event=>{
  if(!drag)return;const rect=svg.getBoundingClientRect();
  view.x=drag.vx-(event.clientX-drag.x)/rect.width*view.w;
  view.y=drag.vy-(event.clientY-drag.y)/rect.height*view.h;setView();
});
svg.addEventListener('pointerup',()=>{drag=null});
document.getElementById('render-note').textContent=
  `Rendered ${nodes.length}/${graph.nodes.length} nodes and `+
  `${edges.length}/${graph.edges.length+graph.memory_links.length} edges.`;
</script></main></body></html>"""
