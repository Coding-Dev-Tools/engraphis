import { readFile } from "node:fs/promises";
import { chromium } from "playwright";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "engraphis", "static");
const d3src = await readFile(`${root}/vendor/d3.min.js`, "utf8");
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
await page.addScriptTag({ content: d3src });
const res = await page.evaluate(() => {
  // synthetic galaxy: 10 communities, hub + 7 members each, intra links + ring cross-links
  const nodes = [], links = [];
  const C = 10, M = 8;
  for (let c = 0; c < C; c++) for (let m = 0; m < M; m++) {
    nodes.push({ id: `n${c}_${m}`, community: c, degree: m===0?M:1, val: m===0?9:2, radius: m===0?6:3,
      x: (Math.random()-.5)*800, y: (Math.random()-.5)*800 });
    if (m>0) links.push({ source: `n${c}_0`, target: `n${c}_${m}` });
  }
  for (let c=1;c<C;c++) links.push({ source:`n${c-1}_0`, target:`n${c}_0` });

  function clusterForce(strength){ let ns; function f(alpha){ if(!ns)return; const sx={},sy={},cn={};
    for(const n of ns){const c=n.community||0;sx[c]=(sx[c]||0)+n.x;sy[c]=(sy[c]||0)+n.y;cn[c]=(cn[c]||0)+1;}
    const k=alpha*strength; for(const n of ns){const c=n.community||0,m=cn[c];if(m<2)continue;n.vx-=(n.x-sx[c]/m)*k;n.vy-=(n.y-sy[c]/m)*k;} }
    f.initialize=n=>{ns=n}; return f; }

  const repel=42, link=20;
  function run(cfg){
    // fresh positions
    nodes.forEach(n=>{n.x=(Math.random()-.5)*800;n.y=(Math.random()-.5)*800;n.vx=0;n.vy=0;});
    const sim=window.d3.forceSimulation(nodes)
      .force('charge',window.d3.forceManyBody().strength(-repel))
      .force('link',window.d3.forceLink(links).id(d=>d.id).distance(link))
      .force('x',window.d3.forceX(0).strength(cfg.center))
      .force('y',window.d3.forceY(0).strength(cfg.center))
      .force('cluster',clusterForce(cfg.cluster))
      .force('collide',window.d3.forceCollide(n=>n.radius+1.5))
      .stop();
    for(let i=0;i<400;i++) sim.tick();
    const ds=nodes.map(n=>Math.hypot(n.x,n.y)).sort((a,b)=>a-b);
    return { mean:+(ds.reduce((a,b)=>a+b,0)/ds.length).toFixed(1), max:+ds.at(-1).toFixed(1) };
  }
  const out={};
  for(const grav of [10,26,40]){
    const g=grav/100;
    out[grav]={ OLD:run({center:.012, cluster:Math.max(.06,g*1.7)}),
                NEW:run({center:Math.max(.02,g), cluster:Math.max(.04,g*0.55)}) };
  }
  return out;
});
await browser.close();
console.log("gravity | OLD mean(max) [center=.012 fixed] | NEW mean(max) [center=g]");
for(const [grav,o] of Object.entries(res))
  console.log(`  ${String(grav).padStart(2)}    |  OLD ${String(o.OLD.mean).padStart(6)} (${o.OLD.max})   |  NEW ${String(o.NEW.mean).padStart(6)} (${o.NEW.max})`);
