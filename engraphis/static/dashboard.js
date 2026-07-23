const API=location.origin+'/api',TRIAL_DAYS=3;
let WS=null, WORKSPACES=[], LIC=null;
const TITLES={overview:'Overview',recall:'Recall',memories:'Memories','mem-editor':'Memory',proactive:'Proactive recall',why:'Why',timeline:'Timeline',audit:'Audit trail',graph:'Knowledge Graph',analytics:'Hosted Analytics',consolidate:'Consolidate',automation:'Hosted Automation',workspaces:'Workspaces',team:'Team Cloud',settings:'Settings'};
const ROUTE_SECTIONS={overview:'Operate',recall:'Operate',memories:'Operate','mem-editor':'Operate',proactive:'Operate',why:'History',timeline:'History',audit:'History',graph:'Relations',analytics:'Relations',consolidate:'Engine',automation:'Engine',workspaces:'Operate',team:'Engine',settings:'Engine'};
/* Per-view subtitle rendered in the topbar next to the view name. The body no longer
   repeats the view title/description — the topbar is the single source for both. */
const DESCS={overview:'',recall:'Hybrid semantic + retention search over this workspace.',memories:'Browse and curate the memories in this workspace.','mem-editor':'',proactive:'What matters right now: importance × recency × retention, plus the last session handoff.',why:'The current answer to a question, with the facts it superseded.',timeline:'Bi-temporal history: what was believed, when it was valid, and when it was recorded.',audit:'Local governance history or content-free, tamper-evident receipts for sharing.',graph:'Explore entities and their sourced relationships from this workspace’s memories.',analytics:'Hosted growth, retention, decay, and entity insights for this workspace.',consolidate:'Run the free local consolidation tool manually; dry-run is the safe default.',automation:'Configure hosted Auto Consolidation and Auto Dreaming policies and review managed proposals.',workspaces:'Hard isolation boundaries. The active workspace receives new memories, imports, searches, and graph operations.',team:'Open the hosted organization dashboard for members, roles, named seats, and audit.',settings:'Local engine settings plus hosted-plan, sync, and managed-compute status.'};
let CURRENT_VIEW='overview';
/* Loaders that compute a live subtitle (e.g. Overview's counts) call this instead of
   writing to a body element, so the topbar stays authoritative. */
function setViewDesc(v,text){DESCS[v]=text;if(CURRENT_VIEW===v){const s=document.getElementById('topbar-sub');if(s)s.textContent=text}}
/* Plan pills are async: a slow /analytics response must not repaint the topbar after
   the user has already navigated elsewhere, so writes are dropped once the view changes. */
function setPlanPill(el,text,cls){if(!el)return;const owner=el.id==='an-lock'?'analytics':'automation';if(CURRENT_VIEW!==owner)return;el.textContent=text;el.className=cls+' topbar-lock'}
function esc(s){if(s===undefined||s===null)return '';return (''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
/* Scheme-sanitize URLs interpolated into href attributes. esc() entity-encodes but
   does not block javascript:/data:/vbscript: URIs; a compromised or misconfigured
   license server could otherwise push a crafted upgrade_url that executes script
   when clicked. Non-http(s) URLs collapse to '#' so the link stays inert. */
function safeUrl(u){if(!u||typeof u!=='string')return '#';const s=u.trim();if(/^#/i.test(s))return s;const m=s.match(/^([a-z][a-z0-9+.-]*):/i);if(!m)return s;if(/^(https?|mailto|ftps?)$/i.test(m[1]))return s;return '#'}
function showAs(el,visible,mode){if(!el)return;el.classList.toggle('is-hidden',!visible);for(const name of ['is-flex','is-block','is-inline-flex'])el.classList.remove(name);if(visible&&mode)el.classList.add('is-'+mode)}
function setTone(el,tone){if(!el)return;for(const name of ['tone-red','tone-green','tone-muted'])el.classList.remove(name);if(tone)el.classList.add('tone-'+tone)}
function renderMd(md){try{return DOMPurify.sanitize(marked.parse(md||''))}catch(e){return esc(md)}}
let TOAST_TIMER=null;
function toast(m,t){const e=document.getElementById('toast');const kind=t||'ok';e.textContent=m;e.className='toast toast-'+kind+' show';e.setAttribute('role',kind==='err'?'alert':'status');e.setAttribute('aria-live',kind==='err'?'assertive':'polite');clearTimeout(TOAST_TIMER);TOAST_TIMER=setTimeout(()=>e.classList.remove('show'),3200)}
function fmtRel(ts){if(!ts)return '';if(typeof ts==='string'){const parsed=Date.parse(ts);if(!Number.isFinite(parsed))return '';ts=parsed/1000}const s=Math.max(0,Date.now()/1000-ts);if(s<60)return 'just now';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';if(s<2592000)return Math.floor(s/86400)+'d ago';return new Date(ts*1000).toISOString().slice(0,10)}
async function api(p,o){o=o||{};const r=await fetch(p.startsWith('http')?p:API+p,o);const txt=await r.text();let d=null;try{d=txt?JSON.parse(txt):null}catch(e){}
 if(!r.ok){let msg=(d&&d.detail&&(d.detail.error||d.detail))||(d&&d.error)||(''+r.status);if(typeof msg!=='string')msg=JSON.stringify(msg);const err=new Error(msg);err.status=r.status;err.detail=d&&d.detail;throw err}return d}
let ACTION_RESOLVE=null,ACTION_SPEC=null;
function actionDialog(spec){if(ACTION_RESOLVE)closeActionDialog(null);ACTION_SPEC=spec||{};document.getElementById('action-title').textContent=ACTION_SPEC.title||'Confirm action';document.getElementById('action-message').textContent=ACTION_SPEC.message||'';document.getElementById('action-error').textContent='';const fields=document.getElementById('action-fields');fields.replaceChildren();(ACTION_SPEC.fields||[]).forEach((f,i)=>{const wrap=document.createElement('div');wrap.className='field';const label=document.createElement('label');label.className='field-lbl';label.htmlFor='action-field-'+i;label.textContent=f.label||'Value';const input=f.options?document.createElement('select'):document.createElement(f.multiline?'textarea':'input');input.className=f.options?'select':'input';input.id='action-field-'+i;input.dataset.name=f.name||('field'+i);if(f.type==='password'){input.type='password';input.autocomplete='off'}else if(input.tagName==='INPUT'){input.type=f.type||'text'}if(f.placeholder)input.placeholder=f.placeholder;if(f.value!=null)input.value=f.value;if(f.required)input.required=true;(f.options||[]).forEach(opt=>{const option=document.createElement('option');option.value=opt.value==null?opt:opt.value;option.textContent=opt.label==null?opt:opt.label;input.appendChild(option)});wrap.append(label,input);fields.appendChild(wrap)});const submit=document.getElementById('action-submit');submit.textContent=ACTION_SPEC.submit||'Continue';submit.className='btn '+(ACTION_SPEC.danger?'btn-danger':'btn-primary');document.getElementById('action-overlay').classList.add('show');return new Promise(resolve=>{ACTION_RESOLVE=resolve})}
function closeActionDialog(value){const resolve=ACTION_RESOLVE;ACTION_RESOLVE=null;ACTION_SPEC=null;document.getElementById('action-overlay').classList.remove('show');if(resolve)resolve(value)}
function submitActionDialog(){const values={};for(const input of document.querySelectorAll('#action-fields input,#action-fields select,#action-fields textarea')){if(input.required&&!input.value.trim()){document.getElementById('action-error').textContent='Complete all required fields.';input.focus();return}values[input.dataset.name]=input.value}closeActionDialog(values)}
async function confirmAction(title,message,submit,danger){const result=await actionDialog({title,message,submit:submit||'Continue',danger:!!danger});return result!==null}
async function textAction(title,message,label,value,options){const result=await actionDialog({title,message,submit:(options&&options.submit)||'Save',danger:!!(options&&options.danger),fields:[{name:'value',label,value:value||'',required:!(options&&options.optional),multiline:!!(options&&options.multiline),options:(options&&options.options)||[]} ]});return result===null?null:result.value}
document.getElementById('action-close').addEventListener('click',()=>closeActionDialog(null));
document.getElementById('action-cancel').addEventListener('click',()=>closeActionDialog(null));
document.getElementById('action-submit').addEventListener('click',submitActionDialog);
document.getElementById('action-overlay').addEventListener('click',event=>{if(event.target===event.currentTarget)closeActionDialog(null)});
let DIALOG_ACTIVE=null;
const DIALOG_RETURN=new WeakMap();
function toggleMobileNav(force){const app=document.querySelector('.app'),btn=document.getElementById('mobile-nav-toggle'),side=document.getElementById('app-sidebar'),main=document.getElementById('main-content');if(!app||!btn||!side||!main)return;const mobile=matchMedia('(max-width:768px)').matches;const open=mobile&&(force===undefined?!app.classList.contains('mobile-nav-open'):!!force);app.classList.toggle('mobile-nav-open',open);btn.setAttribute('aria-expanded',String(open));btn.setAttribute('aria-label',open?'Close navigation':'Open navigation');side.inert=mobile&&!open;side.setAttribute('aria-hidden',String(mobile&&!open));main.inert=mobile&&open;if(open){const active=document.querySelector('.nav-item.active');setTimeout(()=>{if(active)active.focus()},0)}}
function closeMobileNav(returnFocus){toggleMobileNav(false);if(returnFocus)setTimeout(()=>document.getElementById('mobile-nav-toggle').focus(),0)}
function syncMobileNavMode(){toggleMobileNav(document.querySelector('.app').classList.contains('mobile-nav-open'))}
window.addEventListener('resize',syncMobileNavMode);
syncMobileNavMode();
function dialogChanged(ov){const open=ov.classList.contains('show');ov.setAttribute('aria-hidden',String(!open));if(open){if(DIALOG_ACTIVE!==ov){DIALOG_RETURN.set(ov,document.activeElement);DIALOG_ACTIVE=ov;setTimeout(()=>{const first=ov.querySelector('.mm-body input:not([disabled]),.mm-body select:not([disabled]),.mm-body textarea:not([disabled])')||ov.querySelector('button:not([disabled]),[href],[tabindex="0"]');if(first)first.focus()},0)}}else if(DIALOG_ACTIVE===ov){DIALOG_ACTIVE=null;const back=DIALOG_RETURN.get(ov);if(back&&document.contains(back)&&back!==document.body)back.focus();else{const heading=document.getElementById('topbar-title');if(heading){heading.tabIndex=-1;heading.focus()}}}}
function trapDialog(e){const ov=DIALOG_ACTIVE;if(!ov||e.key!=='Tab')return;const els=Array.from(ov.querySelectorAll('input:not([disabled]),select:not([disabled]),textarea:not([disabled]),button:not([disabled]),[href],[tabindex="0"]')).filter(x=>x.offsetParent!==null);if(!els.length){e.preventDefault();return}const first=els[0],last=els[els.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}
function ensureDialogFocus(){if(!DIALOG_ACTIVE||DIALOG_ACTIVE.contains(document.activeElement))return;requestAnimationFrame(()=>{if(!DIALOG_ACTIVE)return;const first=DIALOG_ACTIVE.querySelector('.mm-body input:not([disabled]),.mm-body select:not([disabled]),.mm-body textarea:not([disabled])')||DIALOG_ACTIVE.querySelector('button:not([disabled]),[href],[tabindex="0"]');if(first)first.focus()})}
function controlName(el){const named={recall_q:'Recall query',recall_k:'Number of recall results',mem_q:'Memory filter',why_q:'Question to explain',tl_q:'Timeline topic',graph_repo_filter:'Repository filter',graph_search:'Find graph entity',import_path:'Local import path',import_pattern:'File pattern',code_repo:'Repository name',code_root:'Repository path',postgres_dsn:'PostgreSQL DSN',postgres_repo:'PostgreSQL repository scope'};return named[(el.id||'').replace(/-/g,'_')]||(el.placeholder||'').replace(/[…*]+$/,'').trim()||(el.id||el.type||'control').replace(/[-_]+/g,' ')}
function enhanceUi(root){const scope=root&&root.querySelectorAll?root:document;scope.querySelectorAll('.nav-item').forEach(el=>{el.setAttribute('role','link');el.tabIndex=0;el.setAttribute('aria-current',el.classList.contains('active')?'page':'false')});scope.querySelectorAll('button:not([type])').forEach(el=>el.type='button');scope.querySelectorAll('input,select,textarea').forEach((el,i)=>{if(el.labels&&el.labels.length)return;const field=el.closest('.field'),slider=el.closest('.gslider');const label=(field&&field.querySelector('label'))||(slider&&slider.querySelector('label'));if(label){if(!el.id)el.id='ui-control-'+i+'-'+Date.now();label.htmlFor=el.id}else if(!el.getAttribute('aria-label')&&!el.getAttribute('aria-labelledby'))el.setAttribute('aria-label',controlName(el))});scope.querySelectorAll('.recall-card,.gtop-row,.ep-edge,.mem-card').forEach(el=>{if(!el.hasAttribute('tabindex'))el.tabIndex=0;if(!el.hasAttribute('role'))el.setAttribute('role','button');if(!el.getAttribute('aria-label')){const txt=el.querySelector('.recall-title,.gtop-name,.mem-card-title')||el;el.setAttribute('aria-label',(txt.textContent||'Open item').trim())}});scope.querySelectorAll('.vault-card').forEach(el=>{el.setAttribute('role','group');const name=el.querySelector('.vault-card-name');if(name&&!el.getAttribute('aria-label'))el.setAttribute('aria-label','Workspace '+name.textContent.trim())});scope.querySelectorAll('.spinner').forEach(el=>{el.setAttribute('role','status');el.setAttribute('aria-label','Loading')});document.querySelectorAll('.status-region').forEach(el=>{const busy=!!el.querySelector('.spinner');el.setAttribute('aria-busy',String(busy));const text=(el.textContent||'').toLowerCase();el.setAttribute('role',/\b(error|failed|invalid|unavailable|offline)\b/.test(text)?'alert':'status')});document.querySelectorAll('.mm-overlay').forEach(dialogChanged)}
const enhanceUiBase=enhanceUi;
enhanceUi=function(root){
 enhanceUiBase(root);
 const scope=root&&root.querySelectorAll?root:document;
 scope.querySelectorAll('.nav-item').forEach(el=>{
  if(el.tagName==='BUTTON')el.removeAttribute('role');
  else{el.setAttribute('role','button');el.tabIndex=0}
 });
};
function enhanceDynamicUi(){document.querySelectorAll('.vault-card-name,.tl-item.clickable,#mm-body>div[data-onclick]').forEach(el=>{el.setAttribute('role','button');el.tabIndex=0});document.querySelectorAll('.mem-card').forEach(el=>el.setAttribute('aria-describedby','memory-reorder-help'));document.querySelectorAll('.card-head:not(h1):not(h2):not(h3)').forEach(el=>{el.setAttribute('role','heading');el.setAttribute('aria-level','2')});['sync-status','llm-test-result','au-result','tok-created'].forEach(id=>{const el=document.getElementById(id);if(el){el.setAttribute('role','status');el.setAttribute('aria-live','polite')}})}
function enhanceAdjacentLabels(){document.querySelectorAll('input,select,textarea').forEach((el,i)=>{if(el.labels&&el.labels.length)return;const direct=el.previousElementSibling,parent=el.parentElement&&el.parentElement.previousElementSibling,label=(direct&&direct.matches('label')&&direct)||(parent&&parent.matches('label')&&parent);if(!label)return;if(!el.id)el.id='ui-adjacent-'+i+'-'+Date.now();label.htmlFor=el.id;el.removeAttribute('aria-label')})}
function enhanceDecorativeIcons(){document.querySelectorAll('.nav-icon,.brand-mark,.dropzone-icon,.empty-icon').forEach(el=>el.setAttribute('aria-hidden','true'))}
function enhanceExplorerSemantics(){document.querySelectorAll('#graph-entity-list [role="listitem"],#graph-relation-list [role="listitem"]').forEach(el=>el.removeAttribute('role'))}
function enhanceStatusContainers(){document.querySelectorAll('.status-region,#ov-analytics,#ed-history,#sync-body,#lic-body,#tokens-body,#llm-body,#au-result').forEach(el=>{const visible=el.getClientRects().length>0;el.setAttribute('aria-live','polite');el.setAttribute('aria-busy',String(visible&&(!!el.querySelector('.spinner')||/^\s*Loading/.test(el.textContent||''))))})}
// Coalesce the enhancement sweeps to at most one per frame. Each one re-queries the
// whole document, and the observer fires on every keystroke in graph search and every
// drag-over boundary crossing, so running them per mutation batch was quadratic-ish on
// exactly the interactions that need to stay responsive. Dialog bookkeeping stays
// immediate — it drives focus, which cannot wait a frame.
let UI_SWEEP=0;
function scheduleUiSweep(){if(UI_SWEEP)return;UI_SWEEP=requestAnimationFrame(()=>{UI_SWEEP=0;enhanceUi(document);enhanceAdjacentLabels();enhanceDynamicUi();enhanceDecorativeIcons();enhanceStatusContainers();enhanceExplorerSemantics();ensureDialogFocus()})}
const UI_OBSERVER=new MutationObserver(records=>{records.forEach(r=>{if(r.type==='attributes'&&r.target.classList.contains('mm-overlay'))dialogChanged(r.target)});scheduleUiSweep()});
enhanceUi(document);
enhanceAdjacentLabels();
enhanceDynamicUi();
enhanceDecorativeIcons();
enhanceStatusContainers();
enhanceExplorerSemantics();
document.querySelectorAll('.view').forEach(el=>el.setAttribute('aria-hidden',String(!el.classList.contains('active'))));
UI_OBSERVER.observe(document.body,{subtree:true,childList:true,attributes:true,attributeFilter:['class']});
document.addEventListener('keydown',e=>{trapDialog(e);const t=e.target;if(t.matches('.mem-card')){if(e.altKey&&(e.key==='ArrowUp'||e.key==='ArrowDown')){e.preventDefault();memKeyboardMove(t.dataset.id,e.key==='ArrowUp'?-1:1);return}if(e.key==='Enter'||e.key===' '){e.preventDefault();openMem(t.dataset.id);return}}if((e.key==='Enter'||e.key===' ')&&t.matches('[role=button],.nav-item')){if(e.key===' '||t.matches('.nav-item'))e.preventDefault();t.click()}});

/* theme */
const THEMES=[['dark','Dark','#15181e','#8c83e8'],['light','Light','#fbfbfc','#5547b8'],['midnight','Midnight','#111b2d','#79a6ef'],['solarized','Solarized','#073642','#58a7d8'],['sepia','Sepia','#f8f2e4','#925420']];
const THEME_GLYPH={dark:'◑',light:'☀',midnight:'☾',solarized:'◐',sepia:'❂'};
function applyTheme(t){if(!THEME_GLYPH[t])t='dark';document.body.setAttribute('data-theme',t);window.__theme=t;try{localStorage.setItem('engraphis-theme',t)}catch(e){}const b=document.getElementById('theme-btn');if(b){b.textContent=THEME_GLYPH[t];b.setAttribute('aria-label','Change theme. Current theme: '+t)}const sel=document.getElementById('theme-select');if(sel&&sel.value!==t)sel.value=t;renderThemeMenu();if(typeof graphRecolor==='function')graphRecolor()}
function renderThemeMenu(){const m=document.getElementById('theme-menu');if(!m)return;m.innerHTML=THEMES.map(x=>{const on=(x[0]===window.__theme);return `<button type="button" class="theme-opt${on?' sel':''}" data-theme-value="${x[0]}" data-onclick="h83" aria-pressed="${on}"><span class="theme-sw theme-sw-${x[0]}"><i></i></span><span class="theme-opt-name">${x[1]}</span>${on?'<span class="theme-chk" aria-hidden="true">✓</span>':''}</button>`}).join('')}
function toggleThemeMenu(e){if(e)e.stopPropagation();const m=document.getElementById('theme-menu'),b=document.getElementById('theme-btn');if(!m)return;const show=!m.classList.contains('is-open');m.classList.toggle('is-open',show);if(b)b.setAttribute('aria-expanded',String(show));if(show){renderThemeMenu();setTimeout(()=>{const first=m.querySelector('button');if(first)first.focus();document.addEventListener('click',closeThemeMenu)},0)}}
function closeThemeMenu(){const m=document.getElementById('theme-menu'),b=document.getElementById('theme-btn');if(m)m.classList.remove('is-open');if(b)b.setAttribute('aria-expanded','false');document.removeEventListener('click',closeThemeMenu)}
function pickTheme(t){applyTheme(t);closeThemeMenu()}
function toggleTheme(){const ids=THEMES.map(x=>x[0]);const i=ids.indexOf(window.__theme);pickTheme(ids[(i+1)%ids.length])}
function initTheme(){let saved=null;try{saved=localStorage.getItem('engraphis-theme')}catch(e){}applyTheme(saved||window.__theme||'dark')}

/* nav */
function selectView(v){
 document.querySelectorAll('.nav-item').forEach(n=>{const active=n.dataset.view===v;n.classList.toggle('active',active);n.setAttribute('aria-current',active?'page':'false')});
 document.querySelectorAll('.view').forEach(el=>{const active=el.id==='view-'+v;el.classList.toggle('active',active);el.setAttribute('aria-hidden',String(!active))});
 CURRENT_VIEW=v;
 const heading=document.getElementById('topbar-title');
 heading.textContent=TITLES[v]||v;
 const section=document.getElementById('route-section');
 if(section)section.textContent=ROUTE_SECTIONS[v]||'Operate';
 const sub=document.getElementById('topbar-sub');
 if(sub)sub.textContent=DESCS[v]||'';
 /* Plan pills live in the topbar; clear them on navigation so only the active
    view's loader can repopulate one. */
 ['an-lock','au-lock'].forEach(id=>{const p=document.getElementById(id);if(p){p.textContent='';p.className='pill pill-muted topbar-lock'}});
 closeMobileNav();
 (LOADERS[v]||function(){})();
 heading.tabIndex=-1;
 setTimeout(()=>heading.focus(),0);
}
function navTo(v){selectView(v)}
document.querySelectorAll('.nav-item').forEach(it=>it.addEventListener('click',()=>selectView(it.dataset.view)));

/* workspace */
function setWS(name){
 WS=name;
 const shown=name||'—',sw=document.getElementById('vault-switcher'),top=document.getElementById('topbar-workspace');
 document.getElementById('ws-name').textContent=shown;
 if(top)top.textContent=shown;
 if(sw)sw.setAttribute('aria-label',name?'Choose active workspace. Current workspace: '+name:'Choose active workspace');
}
async function loadWorkspaceList(){const d=await api('/workspaces');WORKSPACES=d.workspaces||[];if(!WS&&WORKSPACES.length){WORKSPACES.sort((a,b)=>(b.memories||0)-(a.memories||0));setWS(WORKSPACES[0].name)}}

/* overview */
async function loadOverview(){try{const st=await api('/stats?workspace='+encodeURIComponent(WS||''));setViewDesc('overview',(st.memories||0)+' memories · '+(st.workspaces||0)+' workspaces');const cards=[['Memories',st.memories],['Live rows',st.total_rows],['Workspaces',st.workspaces],['Sessions',st.sessions]];document.getElementById('stat-grid').innerHTML=cards.map(c=>`<div class="stat"><div class="stat-val">${c[1]!=null?c[1]:'—'}</div><div class="stat-lbl">${c[0]}</div></div>`).join('');document.getElementById('nav-mem-count').textContent=st.memories||'';const bt=st.by_type||{};const tot=Object.values(bt).reduce((a,b)=>a+b,0)||1;document.getElementById('ov-types').innerHTML=Object.keys(bt).length?Object.entries(bt).map(([k,v])=>`<div data-csp-style="s62"><div data-csp-style="s63">${esc(k)}</div><progress class="overview-type-bar" max="${tot}" value="${Math.max(Number(v)||0,0)}" aria-label="${esc(k)}: ${v}"></progress><div data-csp-style="s66">${v}</div></div>`).join(''):'<div class="empty" data-csp-style="s67">No memories</div>';loadOverviewAnalytics()}catch(e){const msg='Overview unavailable: '+e.message;setViewDesc('overview',msg);document.getElementById('stat-grid').innerHTML='<div class="empty" data-csp-style="s10">'+esc(msg)+'</div>';document.getElementById('ov-types').innerHTML='<div class="empty" data-csp-style="s67">Memory types could not be loaded.</div>';document.getElementById('ov-analytics').innerHTML='<div class="empty" data-csp-style="s10">Analytics could not be loaded.</div>';toast(msg,'err')}}
async function loadOverviewAnalytics(){
 const el=document.getElementById('ov-analytics'),lock=document.getElementById('ov-lock');
 try{
  const a=await api('/analytics?workspace='+encodeURIComponent(WS||''));
  lock.textContent=(LIC&&LIC.is_trial)?'TRIAL':'';
  lock.className='pill pill-muted';
  const t=a.totals||{},f=a.decay_forecast||{};
  if(t.live==null){
   const nd=a.namespace_distribution||[],mx=Math.max(...nd.map(x=>x.count),1);
   el.innerHTML='<div data-csp-style="s70">Memories by workspace</div>'+nd.map(x=>barRow(x.namespace,x.count,mx,'var(--accent-dim)')).join('');
   return;
  }
  const weeks=a.growth_weekly||[],thisWeek=weeks.length?weeks[weeks.length-1]:0,avg=Math.round((t.avg_retention||0)*100);
  const cell=(v,l,c)=>{const tone=c==='var(--red)'?' tone-red':(c==='var(--amber)'?' tone-amber':(c==='var(--green)'?' tone-green':''));return `<div><div class="overview-stat${tone}">${v}</div><div data-csp-style="s72">${l}</div></div>`};
  el.innerHTML=`<div data-csp-style="s73">${cell(avg+'%','Avg retention',avg<40?'var(--red)':(avg<70?'var(--amber)':'var(--green)'))}${cell(f.at_risk_7d||0,'Fading ≤ 7 days',f.at_risk_7d>0?'var(--amber)':'')}${cell(thisWeek,'Written this week')}${cell(t.pinned||0,'Pinned')}</div><button class="btn btn-ghost btn-sm" data-csp-style="s52" data-onclick="h86">View full analytics →</button>`;
 }catch(e){
  if(e.status===401||e.status===402||e.status===501){
   lock.textContent='PRO';
   lock.className='pill pill-muted';
   const used=LIC&&LIC.trial&&LIC.trial.used;
   el.innerHTML='<div data-csp-style="s68"><div data-csp-style="s69">Hosted growth, retention distribution, and decay forecast.</div>'+(used?'':'<button class="btn btn-primary btn-sm" data-onclick="h84">Start exactly '+TRIAL_DAYS+' days free</button> ')+'<button class="btn btn-ghost btn-sm" data-onclick="h85">Plan details</button></div>';
  }else el.innerHTML='<div class="empty" data-csp-style="s10">'+esc(e.message)+'</div>';
 }
}

/* ── shared hosted upgrade / trial CTA ── */
function hostedPlanUrl(plan,trial){const raw=(LIC&&(plan==='team'?LIC.team_upgrade_url:LIC.pro_upgrade_url))||(LIC&&LIC.upgrade_url);const safe=safeUrl(raw);if(!safe||safe==='#')return '#';try{const url=new URL(safe,location.href);if(trial)url.searchParams.set('trial',plan);return url.href}catch(e){return safe}}
function unlockHtml(feature,plan){const url=hostedPlanUrl(plan),trialUrl=hostedPlanUrl(plan,true);const used=LIC&&LIC.trial&&LIC.trial.used;const trial=plan==='team'?'Start hosted Team trial':'Start hosted Pro trial';const detail=used?'Your free trial has already been used.':`The email-confirmed, no-card trial lasts exactly ${TRIAL_DAYS} active days.`;return `<div class="empty" data-csp-style="s74"><div data-csp-style="s75">☁</div><div data-csp-style="s76">${esc(feature)} runs in Engraphis ${plan==='team'?'Team':'Pro'} Cloud</div><div data-csp-style="s77">${detail} Local-only write grace is separate, capped at 24 hours, and never extends cloud access.</div><div data-csp-style="s78">${used?'':`<a class="btn btn-primary btn-sm" href="${esc(trialUrl)}" target="_blank" rel="noopener">${trial}</a>`}<a class="btn btn-ghost btn-sm" href="${esc(url)}" target="_blank" rel="noopener">${plan==='team'?'View Team plans':'View Pro plans'}</a></div></div>`}
function startTrialPlan(plan){const url=hostedPlanUrl(plan,true);if(url==='#'){toast('Hosted signup URL is not configured','err');return}const link=document.createElement('a');link.href=url;link.target='_blank';link.rel='noopener';link.click()}
function startTrial(){return startTrialPlan('pro')}
function startTeamTrial(){return startTrialPlan('team')}
function updateLicBadge(){const bd=document.getElementById('lic-badge');if(!bd||!LIC)return;const raw=String(LIC.plan||'local').toLowerCase(),hosted=!!LIC.is_trial||raw==='pro'||raw==='team';bd.textContent=LIC.is_trial?'TRIAL':(hosted?raw.toUpperCase():'LOCAL');bd.className='pill '+(hosted?'pill-accent':'pill-muted')}
function updateFeatureLocks(){
 const has=f=>LIC&&(LIC.features||[]).includes(f);
 const apply=(id,feature,label,plan)=>{
  const badge=document.getElementById(id),item=badge&&badge.closest('.nav-item'),locked=!has(feature);
  if(badge)badge.textContent=locked?plan:'';
  if(item){
   item.setAttribute('aria-label',locked?`${label} — ${plan} plan; opens upgrade options`:label);
   item.title=locked?`${plan} plan required; open for trial and upgrade options`:'';
  }
 };
 apply('nav-analytics-lock','analytics','Analytics','PRO');
 apply('nav-automation-lock','automation','Automation','PRO');
 apply('nav-team-lock','team','Team','TEAM');
}

/* ── analytics (Pro) ── */
function barRow(label,val,peak,color){const tone=color==='var(--green)'?' analytics-bar-green':(color==='var(--blue)'?' analytics-bar-blue':(color==='var(--cyan)'?' analytics-bar-cyan':(color==='var(--accent-dim)'?' analytics-bar-dim':'')));return `<div data-csp-style="s79"><div data-csp-style="s80" title="${esc(label)}">${esc(label)}</div><progress class="analytics-bar${tone}" max="${Math.max(Number(peak)||1,1)}" value="${Math.max(Number(val)||0,0)}" aria-label="${esc(label)}: ${Number(val)||0}"></progress><div data-csp-style="s83">${val}</div></div>`}
function statMini(v,l,color){const tone=color==='var(--red)'?' tone-red':(color==='var(--amber)'?' tone-amber':(color==='var(--green)'?' tone-green':''));return `<div class="stat" data-csp-style="s67"><div class="stat-val${tone}">${v}</div><div class="stat-lbl">${esc(l)}</div></div>`}
function renderAnalytics(a,isPortfolio){const t=a.totals||{},f=a.decay_forecast||{};const weeks=a.growth_weekly||[];const gp=Math.max(...weeks,1);const gitems=weeks.map((n,i)=>{const back=weeks.length-1-i;return barRow(back===0?'now':back+'w ago',n,gp,'var(--accent-dim)')}).join('')||'<div class="empty" data-csp-style="s85">No data</div>';const hist=a.retention_histogram||{};const hc=hist.counts||[],hb=hist.buckets||[];const hp=Math.max(...hc,1);const hitems=hb.map((b,i)=>barRow(b,hc[i]||0,hp,'var(--green)')).join('');const mix=a.resolver_mix||{};const mk=Object.keys(mix);const mp=Math.max(...Object.values(mix),1);const mitems=mk.length?mk.map(k=>barRow(k,mix[k],mp,'var(--blue)')).join(''):'<div class="empty" data-csp-style="s85">No resolver events yet.</div>';const bt=a.by_type||{};const btk=Object.keys(bt);const bp=Math.max(...Object.values(bt),1);const btitems=btk.length?btk.map(k=>barRow(k,bt[k],bp,'var(--accent)')).join(''):'<div class="empty" data-csp-style="s85">No memories yet.</div>';const ents=a.top_entities||[];const ep=Math.max(...ents.map(e=>e.n),1);const eitems=ents.length?ents.map(e=>barRow(e.name+(isPortfolio&&e.workspace?' · '+e.workspace:''),e.n,ep,'var(--cyan)')).join(''):'<div class="empty" data-csp-style="s85">No entities yet — they appear as the graph grows.</div>';const avg=Math.round((t.avg_retention||0)*100);let wsTable='';if(isPortfolio&&a.workspaces){wsTable=`<div class="card" data-csp-style="s52"><div class="card-head">Per-workspace breakdown</div><table class="tbl"><thead><tr><th>Workspace</th><th>Live</th><th>Pinned</th><th>Avg ret.</th><th>Fading 7d</th></tr></thead><tbody>${a.workspaces.map(w=>`<tr><td>${esc(w.workspace)}</td><td>${w.live}</td><td>${w.pinned}</td><td>${Math.round((w.avg_retention||0)*100)}%</td><td>${w.at_risk_7d}</td></tr>`).join('')}</tbody></table></div>`}return `<div class="stat-grid">${statMini(t.live!=null?t.live:'—','Live memories')}${statMini(avg+'%','Avg retention',avg<40?'var(--red)':(avg<70?'var(--amber)':'var(--green)'))}${statMini(f.at_risk_7d!=null?f.at_risk_7d:'—','Fading ≤ 7 days',f.at_risk_7d>0?'var(--amber)':'')}${statMini(f.at_risk_30d!=null?f.at_risk_30d:'—','Fading ≤ 30 days')}${statMini(t.pinned!=null?t.pinned:'—','Pinned (protected)')}${isPortfolio?statMini(t.workspaces||0,'Workspaces'):statMini(t.superseded!=null?t.superseded:'—','Superseded (history)')}</div><div class="cols-2" data-csp-style="s52"><div class="card"><div class="card-head">Memories written per week</div>${gitems}</div><div class="card"><div class="card-head">Retention distribution</div>${hitems}</div></div><div class="cols-2" data-csp-style="s52"><div class="card"><div class="card-head">By type</div>${btitems}</div><div class="card"><div class="card-head">Write-path resolver activity</div>${mitems}</div></div><div class="card" data-csp-style="s52"><div class="card-head">Most connected entities</div>${eitems}</div>${wsTable}`}
async function loadAnalytics(){const el=document.getElementById('analytics-body'),lock=document.getElementById('an-lock'),acts=document.getElementById('an-actions');el.innerHTML='<div class="spinner" data-csp-style="s86"></div>';try{const a=await api('/analytics?workspace='+encodeURIComponent(WS||''));setPlanPill(lock,(LIC&&LIC.is_trial)?'TRIAL':'CLOUD','pill pill-accent');showAs(acts,true,'flex');el.innerHTML=renderAnalytics(a,false)}catch(e){if(e.status===401||e.status===402||e.status===501){setPlanPill(lock,'PRO','pill pill-muted');showAs(acts,false);el.innerHTML=unlockHtml('Analytics','pro')}else{el.innerHTML='<div class="empty" data-csp-style="s87">'+esc(e.message)+'</div>'}}}

/* ── hosted automation policy (Pro / Team) ── */
async function loadAutomation(){const el=document.getElementById('automation-body'),lock=document.getElementById('au-lock'),ws='?workspace='+encodeURIComponent(WS||'');el.innerHTML='<div class="spinner" data-csp-style="s86"></div>';try{const p=await api('/automation'+ws);setPlanPill(lock,(LIC&&LIC.is_trial)?'TRIAL':'CLOUD','pill pill-accent');const last=p.last_run?fmtRel(p.last_run):'never',dream=p.dream_enabled!=null?p.dream_enabled:p.dream;el.innerHTML=`<div class="cols-2"><div class="card"><div class="card-head">Hosted maintenance policy</div><label data-csp-style="s88"><input type="checkbox" id="au-enabled" ${p.enabled?'checked':''}> Enable hosted automation</label><div class="field"><label class="field-lbl">Run every (hours)</label><input class="input" id="au-cadence" type="number" min="1" value="${p.cadence_hours||24}"></div><label data-csp-style="s88"><input type="checkbox" id="au-consolidate" ${p.consolidate?'checked':''}> Auto Consolidation</label><div class="field"><label class="field-lbl">Min cluster size</label><input class="input" id="au-mincluster" type="number" min="2" max="20" value="${p.min_cluster||3}"></div><div class="field"><label class="field-lbl">Archive proposal threshold</label><input class="input" id="au-archive" type="number" step="0.01" min="0" max="0.5" value="${p.archive_below!=null?p.archive_below:0.05}"><div class="field-hint">The cloud returns reviewable proposals. Pinned memories remain protected.</div></div><label data-csp-style="s88"><input type="checkbox" id="au-dream" ${dream?'checked':''}> Auto Dreaming after accumulation and idle time</label><div class="field"><label class="field-lbl">Min new memories</label><input class="input" id="au-dream-min" type="number" min="1" value="${p.dream_min_new||20}"></div><div class="field"><label class="field-lbl">Idle minutes</label><input class="input" id="au-dream-idle" type="number" min="0" value="${p.dream_idle_minutes!=null?p.dream_idle_minutes:15}"></div><button class="btn btn-primary btn-sm" data-onclick="h88">Save hosted policy</button></div><div class="card"><div class="card-head">Cloud worker status</div><div class="cfg-row" data-csp-style="s48"><span>Status</span><span class="pill ${p.enabled?'pill-green':'pill-muted'}" data-csp-style="s9">${p.enabled?'ENABLED':'OFF'}</span></div><div class="cfg-row" data-csp-style="s48"><span>Last run</span><span data-csp-style="s50">${esc(last)}</span></div><div data-csp-style="s89"><button class="btn btn-primary btn-sm" data-onclick="h90">Request proposal</button></div><div id="au-result" data-csp-style="s90"></div><div class="field-hint" data-csp-style="s91">Requesting managed work uploads the selected workspace’s normal and sensitive memory content, excluding secret and session-scoped rows, capped at 16 MiB, over HTTPS without end-to-end encryption. Results are proposals and never automatically write the local database.</div></div></div>`}catch(e){if(e.status===401||e.status===402||e.status===501){setPlanPill(lock,'PRO','pill pill-muted');el.innerHTML=unlockHtml('Automation, Auto Consolidation, and Auto Dreaming','pro')}else{el.innerHTML='<div class="empty" data-csp-style="s87">'+esc(e.message)+'</div>'}}}
async function saveAutomation(){const body={enabled:document.getElementById('au-enabled').checked,cadence_hours:Number(document.getElementById('au-cadence').value)||24,consolidate:document.getElementById('au-consolidate').checked,min_cluster:Number(document.getElementById('au-mincluster').value)||3,archive_below:Number(document.getElementById('au-archive').value)||0.05,dream_enabled:document.getElementById('au-dream').checked,dream_min_new:Number(document.getElementById('au-dream-min').value)||20,dream_idle_minutes:Number(document.getElementById('au-dream-idle').value)};try{await api('/automation?workspace='+encodeURIComponent(WS||''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('Hosted policy saved','ok');loadAutomation()}catch(e){toast((e.status===402||e.status===501)?'Hosted Automation requires Pro or Team':e.message,'err')}}
async function runMaintenance(){const el=document.getElementById('au-result');if(el)el.innerHTML='<div class="spinner" data-csp-style="s93"></div>';try{const d=await api('/maintenance/run?workspace='+encodeURIComponent(WS||''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dry_run:true})});if(el)el.innerHTML=`<span class="pill pill-green" data-csp-style="s9">PROPOSAL</span> Hosted work was submitted for review.<pre data-csp-style="s94">${esc(JSON.stringify(d,null,2))}</pre>`;toast('Managed proposal requested','ok')}catch(e){if(el)el.innerHTML='<div class="empty" data-csp-style="s85">'+esc(e.message)+'</div>';toast((e.status===402||e.status===501)?'Hosted Automation requires Pro or Team':e.message,'err')}}

const runMaintenanceBase=runMaintenance;
let MAINTENANCE_PENDING=false;
runMaintenance=async function(dry){
 if(MAINTENANCE_PENDING)return;
  const buttons=Array.from(document.querySelectorAll('#automation-body button[data-onclick="h90"]')),labels=buttons.map(button=>button.textContent);
 MAINTENANCE_PENDING=true;
 buttons.forEach(button=>{button.disabled=true});
 try{return await runMaintenanceBase(dry)}
 finally{
  MAINTENANCE_PENDING=false;
  buttons.forEach((button,index)=>{button.disabled=false;button.textContent=labels[index]});
 }
}
/* ── version-chain word diff (memory detail) ── */
function tokenizeWords(s){return (s||'').split(/(\s+)/)}
function wordDiff(oldS,newS){const a=tokenizeWords(oldS),b=tokenizeWords(newS),n=a.length,m=b.length;if(n*m>200000)return '<span>'+esc(newS)+'</span>';const dp=Array.from({length:n+1},()=>new Uint32Array(m+1));for(let i=n-1;i>=0;i--)for(let j=m-1;j>=0;j--)dp[i][j]=a[i]===b[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1]);let i=0,j=0,out='';while(i<n&&j<m){if(a[i]===b[j]){out+=esc(a[i]);i++;j++}else if(dp[i+1][j]>=dp[i][j+1]){out+='<span class="diff-del">'+esc(a[i])+'</span>';i++}else{out+='<span class="diff-ins">'+esc(b[j])+'</span>';j++}}while(i<n){out+='<span class="diff-del">'+esc(a[i])+'</span>';i++}while(j<m){out+='<span class="diff-ins">'+esc(b[j])+'</span>';j++}return out}
function renderChainDiff(chain){if(!chain||chain.length<2)return '';const c=chain.slice().sort((x,y)=>(x.valid_from||0)-(y.valid_from||0));let h='<h3 data-csp-style="s95">Version history <span data-csp-style="s96">'+c.length+' versions · additions <span class="diff-ins">green</span>, removals <span class="diff-del">struck</span></span></h3><div class="chain" data-csp-style="s97">';for(let k=0;k<c.length;k++){const m=c[k];const cur=!m.valid_to&&!m.expired_at;const prev=k>0?c[k-1]:null;const body=prev?wordDiff(prev.content||'',m.content||''):esc(m.content||'');const when=m.valid_from?new Date(m.valid_from*1000).toISOString().slice(0,10):'—';h+=`<div class="chain-item${cur?'':' old'}"><div data-csp-style="s98">${k===0?'original':'revision '+k} · ${when}${cur?' · <span class="pill pill-green" data-csp-style="s9">current</span>':''}</div><div class="diff-body">${body}</div></div>`}h+='</div>';return h}
function renderAuditMini(audit){const rows=(audit||[]).slice(0,20);if(!rows.length)return '';return '<h3 data-csp-style="s95">Audit trail</h3><div data-csp-style="s99">'+rows.map(r=>`<div class="audit-row"><span class="pill pill-accent" data-csp-style="s9">${esc(r.action||r.op||r.kind||'edit')}</span><span data-csp-style="s100">${esc(r.reason||r.detail||'')}</span><span data-csp-style="s101">${esc(r.actor||'')}</span><span data-csp-style="s101">${(r.ts||r.at)?fmtRel(r.ts||r.at):''}</span></div>`).join('')+'</div>'}

/* recall */
function memCardHtml(m){const sc=m.score!=null?Math.round(Math.min(m.score>1?m.score/5:m.score,1)*100):null;const rc=m.retention!=null?Math.round(Math.min(m.retention,1)*100):null;return `<div class="recall-card" role="button" tabindex="0" data-id="${esc(m.id)}" data-onclick="h91"><div class="recall-head"><div class="recall-title">${esc(m.title||m.id)} ${m.pinned?'<span class="pill pill-amber" data-csp-style="s9">PINNED</span>':''} <span class="pill pill-muted" data-csp-style="s9">${esc(m.memory_type)}</span></div><div class="recall-scores">${sc!=null?'<span>Score '+sc+'%</span>':''}${rc!=null?'<span>Retention '+rc+'%</span>':''}</div></div><div class="recall-body">${esc((m.content||'').slice(0,320))}</div></div>`}
let RECALL_PENDING=false;
async function doRecall(){
 if(RECALL_PENDING)return;
 const q=document.getElementById('recall-q').value.trim();
 if(!q){toast('Enter a query','err');return}
 if(!WS){workspaceRequired('recall-results','recall memories');return}
 const k=document.getElementById('recall-k').value,el=document.getElementById('recall-results'),button=document.querySelector('#view-recall button[data-onclick="h8"]');
 RECALL_PENDING=true;
 if(button){button.disabled=true;button.textContent='Recalling…'}
 el.innerHTML='<div data-csp-style="s102"><div class="spinner" data-csp-style="s103"></div></div>';
 try{
  const d=await api(`/recall?q=${encodeURIComponent(q)}&workspace=${encodeURIComponent(WS||'')}&k=${k}`);
  if(!d.count){el.innerHTML='<div class="empty" data-csp-style="s12">No matching memories.</div>';return}
  el.innerHTML=`<div data-csp-style="s104">${d.count} recalled</div>`+d.memories.map(memCardHtml).join('');
 }catch(e){
  el.innerHTML='<div class="empty" data-csp-style="s105">'+esc(e.message)+'</div>';
 }finally{
  RECALL_PENDING=false;
  if(button){button.disabled=false;button.textContent='Recall'}
 }
}

/* memories */
let MEM_LIST=[], MEM_DRAG=null;
async function loadMemories(){const q=document.getElementById('mem-q').value.trim();const el=document.getElementById('mem-cards');el.innerHTML='<div data-csp-style="s102"><div class="spinner" data-csp-style="s103"></div></div>';try{const d=await api(`/memories?q=${encodeURIComponent(q)}&workspace=${encodeURIComponent(WS||'')}&limit=200`);MEM_LIST=d.memories||[];if(!d.count){el.innerHTML='<div class="empty" data-csp-style="s12">No memories found.</div>';return}el.innerHTML=MEM_LIST.map(memRowHtml).join('')}catch(e){el.innerHTML='<div class="empty" data-csp-style="s105">'+esc(e.message)+'</div>'}}
function memRowHtml(m){return `<div class="mem-card" data-id="${esc(m.id)}" draggable="true" data-ondragstart="h92" data-ondragover="h93" data-ondragleave="h94" data-ondrop="h95" data-ondragend="h96" data-csp-style="s106"><span class="mem-drag-handle" title="Drag to reorder">⠿</span><div data-csp-style="s107" data-onclick="h97"><div class="mem-card-title">${esc(m.title||m.id)}</div><div class="mem-card-meta"><span class="pill pill-muted" data-csp-style="s9">${esc(m.memory_type)}</span>${m.pinned?'<span class="pill pill-amber" data-csp-style="s9">pin</span>':''}<span data-csp-style="s34">${esc((m.content||'').slice(0,60))}</span></div></div></div>`}
/* drag-to-reorder: whole-list resend on drop keeps this simple and robust — see
   MemoryService.reorder_memories for how sort_order is assigned server-side. */
function memDragStart(e,id){MEM_DRAG=id;e.dataTransfer.effectAllowed='move';e.currentTarget.classList.add('dragging')}
function memDragOver(e,id){if(!MEM_DRAG||MEM_DRAG===id)return;e.preventDefault();const el=e.currentTarget,r=el.getBoundingClientRect(),before=(e.clientY-r.top)<r.height/2;el.classList.toggle('drag-over-top',before);el.classList.toggle('drag-over-bottom',!before)}
function memDragLeave(e){e.currentTarget.classList.remove('drag-over-top','drag-over-bottom')}
async function memDrop(e,id){e.preventDefault();const el=e.currentTarget,before=el.classList.contains('drag-over-top');el.classList.remove('drag-over-top','drag-over-bottom');const dragId=MEM_DRAG;MEM_DRAG=null;if(!dragId||dragId===id)return;const from=MEM_LIST.findIndex(m=>m.id===dragId);let to=MEM_LIST.findIndex(m=>m.id===id);if(from<0||to<0)return;const[moved]=MEM_LIST.splice(from,1);to=MEM_LIST.findIndex(m=>m.id===id);MEM_LIST.splice(before?to:to+1,0,moved);document.getElementById('mem-cards').innerHTML=MEM_LIST.map(memRowHtml).join('');try{await api('/memories/reorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:WS,ids:MEM_LIST.map(m=>m.id)})})}catch(err){toast('Reorder failed: '+err.message,'err');loadMemories()}}
function memDragEnd(e){e.currentTarget.classList.remove('dragging');document.querySelectorAll('#mem-cards .mem-card').forEach(c=>c.classList.remove('drag-over-top','drag-over-bottom'))}
async function memKeyboardMove(id,delta){const from=MEM_LIST.findIndex(m=>m.id===id),to=Math.max(0,Math.min(MEM_LIST.length-1,from+delta));if(from<0||from===to){toast(delta<0?'Memory is already first':'Memory is already last','ok');return}const[moved]=MEM_LIST.splice(from,1);MEM_LIST.splice(to,0,moved);document.getElementById('mem-cards').innerHTML=MEM_LIST.map(memRowHtml).join('');try{await api('/memories/reorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:WS,ids:MEM_LIST.map(m=>m.id)})});toast('Moved '+(moved.title||moved.id)+' to position '+(to+1),'ok');setTimeout(()=>{const card=document.querySelector('#mem-cards .mem-card[data-id="'+CSS.escape(id)+'"]');if(card)card.focus()},0)}catch(err){toast('Reorder failed: '+err.message,'err');loadMemories()}}

/* proactive */
async function loadProactive(){const el=document.getElementById('proactive-body');el.innerHTML='<div class="spinner" data-csp-style="s108"></div>';try{const d=await api('/proactive?workspace='+encodeURIComponent(WS||''));let h='';if(d.handoff)h+=`<div class="card" data-csp-style="s11"><div class="card-head">Last session handoff</div><div data-csp-style="s109">${esc(typeof d.handoff==='string'?d.handoff:JSON.stringify(d.handoff))}</div></div>`;h+=(d.memories&&d.memories.length)?d.memories.map(memCardHtml).join(''):'<div class="empty" data-csp-style="s105">Nothing pressing right now.</div>';el.innerHTML=h}catch(e){el.innerHTML='<div class="empty" data-csp-style="s105">'+esc(e.message)+'</div>'}}

/* why */
let WHY_PENDING=false;
async function doWhy(){
 if(WHY_PENDING)return;
 const q=document.getElementById('why-q').value.trim();
 if(!q){toast('Enter a question','err');return}
 if(!WS){workspaceRequired('why-body','explain current beliefs and their history');return}
 const el=document.getElementById('why-body'),button=document.querySelector('#view-why button[data-onclick="h17"]');
 WHY_PENDING=true;
 if(button){button.disabled=true;button.textContent='Explaining…'}
 el.innerHTML='<div class="spinner" data-csp-style="s108"></div>';
 try{
  const d=await api(`/why?q=${encodeURIComponent(q)}&workspace=${encodeURIComponent(WS||'')}`);
  let h='';
  if(d.answer.length){
   h+='<div class="card"><div class="card-head">Current answer</div>'+d.answer.map(m=>`<div data-csp-style="s110"><div data-csp-style="s111">${esc(m.title||m.id)}</div><div data-csp-style="s112">${esc(m.content)}</div></div>`).join('')+'</div>';
  }else{
   h+='<div class="empty" data-csp-style="s105">No current answer found.</div>';
  }
  if(d.supersedes.length){
   h+='<div class="card" data-csp-style="s52"><div class="card-head">Superseded (no longer true)</div><div class="chain" data-csp-style="s97">'+d.supersedes.map(m=>`<div class="chain-item old"><div data-csp-style="s111">${esc(m.title||m.id)}</div><div data-csp-style="s72">${esc(m.content)}</div><div data-csp-style="s72">until ${m.valid_to?new Date(m.valid_to*1000).toISOString().slice(0,10):'—'}</div></div>`).join('')+'</div></div>';
  }
  el.innerHTML=h;
 }catch(e){
  el.innerHTML='<div class="empty" data-csp-style="s105">'+esc(e.message)+'</div>';
 }finally{
  WHY_PENDING=false;
  if(button){button.disabled=false;button.textContent='Explain'}
 }
}

/* timeline */
let TIMELINE_PENDING=false;
async function doTimeline(){
 const q=document.getElementById('tl-q').value.trim();
 if(!q){toast('Enter a topic','err');return}
 if(!WS){workspaceRequired('tl-body','trace memory history');return}
 if(TIMELINE_PENDING)return;
 const el=document.getElementById('tl-body'),button=document.querySelector('#view-timeline button[data-onclick="h19"]');
 TIMELINE_PENDING=true;
 if(button){button.disabled=true;button.textContent='Tracing…'}
 el.innerHTML='<div class="spinner" data-csp-style="s108"></div>';
 try{
  const d=await api(`/timeline?q=${encodeURIComponent(q)}&workspace=${encodeURIComponent(WS||'')}&limit=40`);
  if(!d.history.length){el.innerHTML='<div class="empty" data-csp-style="s105">No history for this topic.</div>';return}
  const exact=value=>value?new Date(value*1000).toISOString():'Unavailable';
  el.innerHTML='<div class="chain">'+d.history.map(m=>{
   const cur=!m.valid_to&&!m.expired_at,validEnd=m.valid_to||m.expired_at;
   const valid=exact(m.valid_from)+(validEnd?' — '+exact(validEnd):' — current');
   const recorded=exact(m.ingested_at),source=(m.provenance&&m.provenance.source)||'Unknown source';
   return `<article class="chain-item${cur?'':' old'}"><div data-csp-style="s111">${esc(m.title||m.id)} ${cur?'<span class="pill pill-green" data-csp-style="s9">current</span>':'<span class="pill pill-muted" data-csp-style="s9">past</span>'}</div><div data-csp-style="s90">${esc(m.content)}</div><dl class="temporal-meta"><div><dt>Valid time</dt><dd>${esc(valid)}</dd></div><div><dt>Recorded time</dt><dd>${esc(recorded)}</dd></div><div><dt>Source</dt><dd>${esc(source)}</dd></div></dl></article>`;
  }).join('')+'</div>';
 }catch(e){
  el.innerHTML='<div class="empty" data-csp-style="s105">'+esc(e.message)+'</div>';
 }finally{
  TIMELINE_PENDING=false;
  if(button){button.disabled=false;button.textContent='Trace'}
 }
}

/* audit */
async function loadAudit(){const el=document.getElementById('audit-body');el.innerHTML='<div class="spinner" data-csp-style="s108"></div>';try{const d=await api('/audit?workspace='+encodeURIComponent(WS||'')+'&limit=200');const rows=d.entries||d.audit||[];if(!rows.length){el.innerHTML='<div class="empty" data-csp-style="s105">No governance actions recorded.</div>';return}el.innerHTML='<div class="card" data-csp-style="s113">'+rows.map(r=>`<div class="audit-row"><span class="pill pill-accent" data-csp-style="s9">${esc(r.action||r.op||r.kind||'edit')}</span><span data-csp-style="s114">${esc(r.memory_id||r.target||r.detail||'')}</span><span data-csp-style="s101">${esc(r.actor||'')}</span><span data-csp-style="s101">${r.ts||r.at?fmtRel(r.ts||r.at):''}</span></div>`).join('')+'</div>'}catch(e){el.innerHTML='<div class="empty" data-csp-style="s105">'+esc(e.message)+'</div>'}}
async function loadReceipts(){const el=document.getElementById('audit-body');el.innerHTML='<div class="spinner" data-csp-style="s108"></div>';try{const d=await api('/receipts?workspace='+encodeURIComponent(WS||'')+'&limit=500');const v=await api('/receipts/verify?workspace='+encodeURIComponent(WS||''));const rows=d.entries||[];el.innerHTML=`<div class="card" data-csp-style="s115"><div class="card-head">Receipt chain <span class="pill ${v.valid?'pill-green':'pill-amber'}">${v.valid?'verified':'invalid'}</span></div><div data-csp-style="s90">${v.count||0} receipts · head <code>${esc((v.head||'').slice(0,24))}</code></div></div>`+(rows.length?'<div class="card" data-csp-style="s113">'+rows.map(r=>`<div class="audit-row"><span class="pill pill-accent" data-csp-style="s9">${esc(r.operation||'operation')}</span><span data-csp-style="s1"><code>${esc((r.hash||'').slice(0,20))}</code> · ${esc(r.status||'ok')} · ${r.target_count||0} target(s)</span><span data-csp-style="s101">${r.ts_ms?fmtRel(r.ts_ms/1000):''}</span></div>`).join('')+'</div>':'<div class="empty" data-csp-style="s105">No receipts yet.</div>')}catch(e){el.innerHTML='<div class="empty" data-csp-style="s105">'+esc(e.message)+'</div>'}}
async function downloadReceipts(){try{const d=await api('/receipts/export?workspace='+encodeURIComponent(WS||''));const blob=new Blob([JSON.stringify(d,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='engraphis-receipts-'+(WS||'workspace')+'.json';a.click();URL.revokeObjectURL(a.href);toast('Privacy-safe receipts exported','ok')}catch(e){toast(e.message,'err')}}

/* consolidate */
async function runConsolidate(dry){const el=document.getElementById('consolidate-body');el.innerHTML='<div class="spinner" data-csp-style="s108"></div>';try{const d=await api('/consolidate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:WS,dry_run:dry})});el.innerHTML=`<div class="card"><div class="card-head">${dry?'Dry run (nothing changed)':'Consolidation complete'}</div><pre data-csp-style="s116">${esc(JSON.stringify(d,null,2))}</pre></div>`;if(!dry)toast('Consolidation done','ok')}catch(e){el.innerHTML='<div class="empty" data-csp-style="s105">'+esc(e.message)+'</div>'}}

const runConsolidateBase=runConsolidate;
let CONSOLIDATE_PENDING=false;
runConsolidate=async function(dry){
 if(CONSOLIDATE_PENDING)return;
 if(!WS){workspaceRequired('consolidate-body','preview or commit consolidation');return}
 if(!dry&&!await confirmAction('Commit consolidation','This may create distilled semantic memories and archive decayed transients in workspace "'+(WS||'')+'". Choose the dry run first if you only want a preview.','Commit consolidation',true))return;
 const buttons=Array.from(document.querySelectorAll('#view-consolidate button[data-onclick="h61"],#view-consolidate button[data-onclick="h62"]')),labels=buttons.map(button=>button.textContent);
 CONSOLIDATE_PENDING=true;
 buttons.forEach(button=>{button.disabled=true});
 try{return await runConsolidateBase(dry)}
 finally{
  CONSOLIDATE_PENDING=false;
  buttons.forEach((button,index)=>{button.disabled=false;button.textContent=labels[index]});
 }
}
/* local single-user workspaces */
function canCreateWs(){return true}
function folderCardName(el){const card=el.closest('.vault-card');return card?card.dataset.workspace:''}
function folderOpen(el){const card=el.closest('.vault-card');if(card)wsSwitch(card.dataset.workspace)}
function folderCardHtml(w,opts){opts=opts||{};const manage=opts.manage!==false,a=w.name===WS,count=Number(w.memories)||0;const actions=manage?`<div class="vault-card-actions"><button class="btn btn-ghost btn-sm" data-onclick="h99">Rename</button><button class="btn btn-ghost btn-sm" data-onclick="h100">Describe</button><button class="btn btn-ghost btn-sm" data-onclick="h101">Merge</button><button class="btn btn-ghost btn-sm" data-onclick="h102">Copy</button><button class="btn btn-danger btn-sm" data-onclick="h103">Delete</button></div>`:'';return `<div class="vault-card${a?' active':''}" data-workspace="${esc(w.name)}" data-memories="${count}">${actions}<div class="vault-card-name" data-csp-style="s117" data-onclick="h104">${esc(w.name)}${a?' <span class="pill pill-green" data-csp-style="s9">active</span>':''}</div>${w.description?`<div class="vault-card-desc">${esc(w.description)}</div>`:''}<div class="vault-card-stats"><span>${count} memories</span>${w.repos&&(Array.isArray(w.repos)?w.repos.length:w.repos)?'<span>'+(Array.isArray(w.repos)?w.repos.length:w.repos)+' repos</span>':''}<a href="#" data-onclick="h105" data-csp-style="s118">View memories →</a></div></div>`}
function tfMemories(name){setWS(name);navTo('memories')}
async function refreshFolders(){await loadWorkspaces()}
async function loadWorkspaces(){
 const el=document.getElementById('ws-cards'),canNew=true,canSources=true;
 const nb=document.getElementById('ws-new-btn'),ic=document.getElementById('import-card'),cc=document.getElementById('code-import-card'),iwn=document.getElementById('import-ws-name');
 if(iwn)iwn.textContent=WS?('"'+WS+'"'):'the active folder';
 if(nb){nb.textContent='New folder';showAs(nb,canNew,'inline-flex')}
 showAs(ic,canNew,'block');
 showAs(cc,canSources,'block');
 try{
  await loadWorkspaceList();
  if(!WORKSPACES.length){
   el.innerHTML='<div class="empty" data-csp-style="s119">No folders yet.<div data-csp-style="s52"><button class="btn btn-primary btn-sm" data-onclick="h63">New folder</button></div></div>';
   return;
  }
  el.innerHTML='<div class="cols-2">'+WORKSPACES.map(w=>folderCardHtml(w,{manage:canNew})).join('')+'</div>';
  }catch(e){el.innerHTML='<div class="empty" data-csp-style="s105">'+esc(e.message)+'</div>'}
}
async function wsCreate(){
 const result=await actionDialog({title:'New folder',message:'Create a local workspace for related memories.',submit:'Create folder',fields:[{name:'name',label:'Folder name',required:true},{name:'description',label:'Description (optional)',optional:true}]});
 if(result===null)return;
 const name=result.name.trim();
 if(!name){toast('Enter a folder name','err');return}
 if(WORKSPACES.some(w=>w.name===name)){toast('A folder named "'+name+'" already exists','err');return}
 try{
  await api('/workspaces/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:name,description:(result.description||'').trim()})});
  await loadWorkspaceList();setWS(name);toast('Folder "'+name+'" created — now the active folder','ok');refreshFolders();
 }catch(e){toast('Create failed: '+e.message,'err')}
}
function wsSwitch(name){setWS(name);toast('Switched to '+name,'ok');loadOverview();navTo('overview')}

/* import (files/folders from this PC — see MemoryService.import_folder/import_files) */
async function importUpload(items){if(!canCreateWs()){toast('Viewers can’t import','err');return}if(!WS){toast('Create or select a folder first','err');return}if(!items||!items.length)return;const fd=new FormData();fd.append('workspace',WS);fd.append('memory_type','semantic');fd.append('derive_facts',document.getElementById('import-derive').checked?'true':'false');for(const it of items)fd.append('files',it.file,it.name);const el=document.getElementById('import-status');el.textContent='Extracting and importing '+items.length+' file(s)…';try{const r=await api('/workspaces/import-files',{method:'POST',body:fd});const wc=(r.warnings||[]).length;el.textContent=r.imported+' imported, '+r.skipped+' skipped, '+r.errors+' error(s), '+(r.derived_facts||0)+' derived fact(s)'+(wc?', '+wc+' warning(s)':'');toast(r.imported+' resource'+(r.imported===1?'':'s')+' imported into "'+WS+'"','ok');refreshFolders()}catch(e){el.textContent='';toast('Import failed: '+e.message,'err')}}
function importFilesPicked(fileList,el){const items=Array.from(fileList||[]).map(f=>({file:f,name:f.webkitRelativePath||f.name}));if(el)el.value='';importUpload(items)}
async function importWalkEntry(entry,path,out){if(entry.isFile){await new Promise(res=>entry.file(f=>{out.push({file:f,name:(path?path+'/':'')+f.name});res()},()=>res()))}else if(entry.isDirectory){const reader=entry.createReader();const readBatch=()=>new Promise(res=>reader.readEntries(res,()=>res([])));let batch;do{batch=await readBatch();for(const e of batch)await importWalkEntry(e,(path?path+'/':'')+entry.name,out)}while(batch.length)}}
async function importDrop(e){e.preventDefault();e.currentTarget.classList.remove('drag');const items=e.dataTransfer.items;const out=[];if(items&&items.length&&items[0].webkitGetAsEntry){for(const it of items){const entry=it.webkitGetAsEntry&&it.webkitGetAsEntry();if(entry)await importWalkEntry(entry,'',out)}}else{for(const f of e.dataTransfer.files)out.push({file:f,name:f.name})}importUpload(out)}
async function importFromPath(){if(!canCreateWs()){toast('Viewers can’t import','err');return}if(!WS){toast('Create or select a folder first','err');return}const path=(document.getElementById('import-path').value||'').trim();const pattern=(document.getElementById('import-pattern').value||'*').trim()||'*';if(!path){toast('Enter a path','err');return}const el=document.getElementById('import-status');el.textContent='Extracting and importing…';try{const r=await api('/workspaces/import-folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:WS,path,file_pattern:pattern,memory_type:'semantic',derive_facts:document.getElementById('import-derive').checked})});const wc=(r.warnings||[]).length;el.textContent=r.imported+' imported, '+r.skipped+' skipped, '+r.errors+' error(s), '+(r.derived_facts||0)+' derived fact(s), scanned '+r.scanned+(wc?', '+wc+' warning(s)':'');toast(r.imported+' resource'+(r.imported===1?'':'s')+' imported into "'+WS+'"','ok');refreshFolders()}catch(e){el.textContent='';toast('Import failed: '+e.message,'err')}}
async function indexRepository(){if(!WS){toast('Select a workspace first','err');return}const repo=(document.getElementById('code-repo').value||'').trim(),root=(document.getElementById('code-root').value||'').trim(),el=document.getElementById('code-import-status');if(!repo||!root){toast('Enter a repository name and path','err');return}el.textContent='Incrementally indexing repository…';try{const r=await api('/code/index',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:WS,repo:repo,root_path:root})});el.textContent=`${r.files_indexed} changed, ${r.files_unchanged} unchanged · ${r.symbols} symbols · ${r.edges} edges · ${r.code_memory_links||0} memory links`;toast('Repository graph updated','ok')}catch(e){el.textContent='';toast(e.message,'err')}}
async function importPostgresSchema(){if(!WS){toast('Select a workspace first','err');return}const dsn=(document.getElementById('postgres-dsn').value||'').trim(),repo=(document.getElementById('postgres-repo').value||'').trim(),el=document.getElementById('code-import-status');if(!dsn){toast('Enter a PostgreSQL DSN','err');return}el.textContent='Reading PostgreSQL catalog…';try{const r=await api('/resources/postgres',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:WS,repo:repo||null,dsn:dsn})});document.getElementById('postgres-dsn').value='';el.textContent=`Imported ${r.schema.tables||0} tables, ${r.entities} entities, and ${r.relations} relations`;toast('Database schema imported','ok')}catch(e){el.textContent='';toast(e.message,'err')}}
async function wsRename(name){const nn=await textAction('Rename workspace','Choose a new name for "'+name+'".','Workspace name',name,{submit:'Rename'});if(nn===null)return;const v=nn.trim();if(!v||v===name)return;try{await api('/workspaces/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:name,new_name:v})});if(WS===name)setWS(v);toast('Renamed','ok');refreshFolders()}catch(e){toast(e.message,'err')}}
async function wsDescribe(name){const cur=((WORKSPACES.find(w=>w.name===name)||{}).description)||'';const d=await textAction('Describe workspace','Update the optional description for "'+name+'".','Description',cur,{submit:'Save',optional:true,multiline:true});if(d===null)return;try{await api('/workspaces/describe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:name,description:d})});toast('Saved','ok');refreshFolders()}catch(e){toast(e.message,'err')}}
async function wsMerge(name){const others=WORKSPACES.map(w=>w.name).filter(n=>n!==name);if(!others.length){toast('No other workspace to merge into','err');return}const result=await actionDialog({title:'Merge workspace',message:'All memories in "'+name+'" will move to the selected workspace and "'+name+'" will be removed. This cannot be undone.',submit:'Merge workspace',danger:true,fields:[{name:'target',label:'Destination workspace',required:true,options:others.map(value=>({value,label:value}))}]});if(result===null)return;const v=result.target;try{const r=await api('/workspaces/merge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:name,target:v})});toast('Merged '+(r.memories_moved||0)+' memories into '+v,'ok');if(WS===name)setWS(v);refreshFolders()}catch(e){toast('Merge failed: '+e.message,'err')}}
async function wsCopy(name){try{const r=await api('/workspaces/copy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:name})});toast('Copied to "'+r.workspace+'" ('+(r.memories_copied||0)+' memories)','ok');refreshFolders()}catch(e){toast('Copy failed: '+e.message,'err')}}
async function wsDelete(name,n){const active=name===WS?' This is the active workspace; another workspace will become active after deletion.':'';if(!await confirmAction('Delete workspace','Delete "'+name+'" and all '+n+' memories in it from this Engraphis store?'+active+' Export anything you need first. This cannot be undone.','Delete workspace',true))return;try{const r=await api('/workspaces/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:name})});toast('Deleted ('+(r.memories_removed||0)+' memories)','ok');if(WS===name){WS=null;await loadWorkspaceList();if(WORKSPACES[0])setWS(WORKSPACES[0].name)}refreshFolders()}catch(e){toast(e.message,'err')}}

/* memory detail + governance — a full-page editor (view-mem-editor), not a popup:
   the raw content is always a live, editable textarea (Obsidian-style), with a
   rendered preview alongside and the version history/audit trail below. */
function setEditorActionsEnabled(enabled){['ed-save-btn','ed-pin-btn','ed-forget-btn'].forEach(id=>{const btn=document.getElementById(id);if(btn)btn.disabled=!enabled})}
async function openMem(id){window.CURMEM=null;setEditorActionsEnabled(false);navTo('mem-editor');const ta=document.getElementById('ed-content');document.getElementById('ed-title').value='';ta.value='';document.getElementById('ed-meta').innerHTML='';document.getElementById('ed-preview').innerHTML='';document.getElementById('ed-history').innerHTML='<div class="spinner" data-csp-style="s108"></div>';document.getElementById('topbar-title').textContent='Loading…';try{const d=await api('/memory/'+encodeURIComponent(id)+'?workspace='+encodeURIComponent(WS||''));const m=d.memory;if(!m){document.getElementById('topbar-title').textContent='Memory unavailable';document.getElementById('ed-history').innerHTML='<div class="empty">Memory not found.</div>';return false}window.CURMEM=m;document.getElementById('ed-title').value=m.title||'';document.getElementById('ed-type').value=m.memory_type||'semantic';ta.value=m.content||'';edPreviewUpdate();edRenderMeta();document.getElementById('topbar-title').textContent=m.title||m.id;document.getElementById('ed-history').innerHTML=renderChainDiff(d.chain)+renderAuditMini(d.audit);setEditorActionsEnabled(true);return true}catch(e){document.getElementById('topbar-title').textContent='Memory unavailable';document.getElementById('ed-history').innerHTML='<div class="empty">Could not load this memory: '+esc(e.message)+'</div>';return false}}
function closeMem(){navTo('memories')}
function edPreviewUpdate(){document.getElementById('ed-preview').innerHTML=renderMd(document.getElementById('ed-content').value)}
function edRenderMeta(){const m=window.CURMEM;if(!m)return;const btn=document.getElementById('ed-pin-btn');if(btn)btn.textContent=m.pinned?'Unpin':'Pin';document.getElementById('ed-meta').innerHTML=`<span class="pill pill-muted" data-csp-style="s9">${esc(m.memory_type)}</span> <span class="pill pill-muted" data-csp-style="s9">${esc(m.scope||'')}</span> ${m.pinned?'<span class="pill pill-amber" data-csp-style="s9">pinned</span>':''} <span data-csp-style="s101">${esc((m.provenance&&m.provenance.source)||'')}${m.provenance&&m.provenance.trusted===false?' · untrusted':''} · id ${esc(m.id)}</span>`}
async function edTogglePin(){const m=window.CURMEM;if(!m)return;try{await api('/pin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:m.id,workspace:WS,pinned:!m.pinned})});m.pinned=!m.pinned;edRenderMeta();toast(m.pinned?'Pinned':'Unpinned','ok')}catch(e){toast(e.message,'err')}}
async function edSave(){const m=window.CURMEM;if(!m)return;const nt=document.getElementById('ed-title').value;const ntype=document.getElementById('ed-type').value;const nc=document.getElementById('ed-content').value;try{let meta=false,body=false,id=m.id;if(nt!==(m.title||'')||ntype!==(m.memory_type||'semantic')){await api('/memory/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,workspace:WS,title:nt,memory_type:ntype})});meta=true}if(nc!==(m.content||'')){const r=await api('/correct',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,workspace:WS,content:nc,reason:'dashboard edit'})});body=true;id=r.id}if(!meta&&!body){toast('No changes','ok');return}toast('Saved','ok');await openMem(id)}catch(e){toast(e.message,'err')}}
async function edForget(){const m=window.CURMEM;if(!m)return;if(!await confirmAction('Forget memory','Close the current validity of "'+(m.title||m.id)+'" in workspace "'+(WS||'')+'"? It will stop appearing as current truth but remain in bi-temporal history.','Forget memory',true))return;try{await api('/forget',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:m.id,workspace:WS,reason:'dashboard'})});toast('Memory closed and retained in history','ok');closeMem()}catch(e){toast(e.message,'err')}}
let EDITOR_BASELINE='',EDITOR_FORCE_CLOSE=false;
function editorSnapshot(){return JSON.stringify({title:document.getElementById('ed-title').value,type:document.getElementById('ed-type').value,content:document.getElementById('ed-content').value})}
function editorIsDirty(){return !!window.CURMEM&&!!EDITOR_BASELINE&&editorSnapshot()!==EDITOR_BASELINE}
function editorRefreshDirty(){const state=document.getElementById('ed-save-state');if(!state)return;const dirty=editorIsDirty();state.textContent=dirty?'Unsaved changes':'Saved';state.classList.toggle('dirty',dirty)}
function editorCommitBaseline(){EDITOR_BASELINE=window.CURMEM?editorSnapshot():'';editorRefreshDirty()}
const openMemWithEditorState=openMem;
openMem=async function(id){const loaded=await openMemWithEditorState(id);if(loaded)editorCommitBaseline();else{EDITOR_BASELINE='';editorRefreshDirty()}return loaded}
const selectViewWithDirtyGuard=selectView;
selectView=async function(v){if(!EDITOR_FORCE_CLOSE&&v!=='mem-editor'&&document.getElementById('view-mem-editor').classList.contains('active')&&editorIsDirty()){const title=document.getElementById('ed-title').value||'this memory';if(!await confirmAction('Discard unsaved changes','Leave the Memory editor? Unsaved changes to "'+title+'" will be lost.','Discard changes',true))return false}selectViewWithDirtyGuard(v);return true}
const edForgetWithEditorState=edForget;
edForget=async function(){EDITOR_FORCE_CLOSE=true;try{return await edForgetWithEditorState()}finally{EDITOR_FORCE_CLOSE=false}}
;['ed-title','ed-type','ed-content'].forEach(id=>{const el=document.getElementById(id);el.addEventListener('input',editorRefreshDirty);el.addEventListener('change',editorRefreshDirty)});
window.addEventListener('beforeunload',e=>{if(!editorIsDirty())return;e.preventDefault();e.returnValue=''});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'&&document.getElementById('view-mem-editor').classList.contains('active')){e.preventDefault();edSave()}});
/* license */
async function loadLicense(){const el=document.getElementById('lic-body');try{const d=await api('/license');LIC=d;updateLicBadge();updateFeatureLocks();renderLicense(d)}catch(e){if(el)el.innerHTML='<div class="empty" data-csp-style="s10">'+esc(e.message)+'</div>'}}
function renderLicense(d){
 const el=document.getElementById('lic-body');if(!el)return;
 const raw=String(d.plan||'local').toLowerCase(),trial=!!d.is_trial;
 const hosted=trial||raw==='pro'||raw==='team';
 const label=trial?(raw==='team'?'TEAM TRIAL':'PRO TRIAL'):(hosted?raw.toUpperCase():'LOCAL CORE');
 const known=d.known_features||{};
 const feats=hosted?Object.keys(known).map(f=>`<span class="lic-feat">${(d.features||[]).includes(f)?'✓':'○'} ${esc(known[f])}</span>`).join(''):'';
 const used=!!(d.trial&&d.trial.used);
 let h=`<div class="cfg-row"><span>${hosted?'Hosted plan':'Local runtime'}</span><span class="pill ${hosted?'pill-accent':'pill-muted'}">${esc(label)}</span></div>`;
 if(d.error)h+=`<div class="trial-banner"><strong>Hosted authorization unavailable</strong> — ${esc(d.error)}</div>`;
 if(hosted&&d.email&&d.email!=='trial')h+=`<div class="cfg-row"><span>Hosted account</span><span>${esc(d.email)}</span></div>`;
 if(hosted&&d.expires)h+=`<div class="cfg-row"><span>${trial?'Trial ends':'Authorization expires'}</span><span>${new Date(d.expires*1000).toISOString().slice(0,10)}</span></div>`;
 if(feats)h+=`<div data-csp-style="s121">${feats}</div>`;
 h+=`<div class="field-hint" data-csp-style="s97">The local core remains free. Pro and Team capabilities execute in Engraphis Cloud. The email-confirmed, no-card trial lasts exactly ${TRIAL_DAYS} active days; local-only write grace is separate, capped at 24 hours, and never extends cloud access.</div>`;
 h+=hosted||used
  ?`<div data-csp-style="s123"><a class="btn btn-primary btn-sm" href="${esc(hostedPlanUrl('pro'))}" target="_blank" rel="noopener">Open Pro Cloud</a><a class="btn btn-ghost btn-sm" href="${esc(hostedPlanUrl('team'))}" target="_blank" rel="noopener">Open Team Cloud</a></div>`
  :`<div data-csp-style="s123"><button class="btn btn-primary btn-sm" data-onclick="h84">Start hosted Pro trial</button><button class="btn btn-ghost btn-sm" data-onclick="h87">Start hosted Team trial</button></div>`;
 el.innerHTML=h;
}
async function exportWorkspace(signed){try{const d=await api('/export?workspace='+encodeURIComponent(WS||'')+(signed?'&signed=1':''));const blob=new Blob([JSON.stringify(d,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='engraphis-export-'+(signed?'signed-':'')+Date.now()+'.json';a.click();URL.revokeObjectURL(a.href);toast(signed?'Signed compliance export downloaded':'Exported','ok')}catch(e){toast(e.status===402?'Export is a Pro feature — start your free trial':e.message,'err')}}

/* Hosted Team is a service CTA; local identity and seat administration are not shipped. */
async function loadTeam(){const el=document.getElementById('team-body');let url=hostedPlanUrl('team');try{const st=await api('/auth/state');if(url==='#'&&st&&st.cloud_url)url=safeUrl(st.cloud_url)}catch(e){}let trialUrl=url;if(url!=='#')try{const parsed=new URL(url,location.href);parsed.searchParams.set('trial','team');trialUrl=parsed.href}catch(e){}el.innerHTML=`<div class="card teaser"><div class="card-head">Engraphis Team Cloud <span class="pill pill-accent" data-csp-style="s9">HOSTED</span></div><div data-csp-style="s149">Organizations, invitations, roles, named seats, scoped device credentials, and team audit run on the private hosted service. This local dashboard is intentionally single-user.</div><div class="field-hint" data-csp-style="s97">The email-confirmed trial lasts exactly ${TRIAL_DAYS} active days. A separate local-only write grace is capped at 24 hours and never extends Team or other cloud access.</div><div data-csp-style="s150"><a class="btn btn-primary btn-sm" href="${esc(trialUrl)}" target="_blank" rel="noopener">Start hosted Team trial</a><a class="btn btn-ghost btn-sm" href="${esc(url)}" target="_blank" rel="noopener">Open Team Cloud</a></div></div>`}
/* health + settings */
function connectionContext(){const host=(location.hostname||'').toLowerCase();return host==='localhost'||host==='127.0.0.1'||host==='::1'||host.endsWith('.localhost')?'Local engine':'Remote customer node'}
async function checkHealth(){const label=connectionContext();try{await api('/health');const d=document.getElementById('health-dot'),t=document.getElementById('health-text');if(d){d.classList.add('health-ok');d.classList.remove('health-error')}if(t)t.textContent=label+' connected'}catch(e){const d=document.getElementById('health-dot'),t=document.getElementById('health-text');if(d){d.classList.add('health-error');d.classList.remove('health-ok')}if(t)t.textContent=label+' unavailable'}}
function loadSettings(){loadLicense();loadSyncStatus();loadHostedAgentAccess();loadLlmStatus();const s=document.getElementById('cfg-store');if(s)s.textContent=location.host}

async function loadLlmStatus(){const el=document.getElementById('llm-body');if(!el)return;try{const st=await api('/llm/status');const ok=st.configured;const badge=ok?'<span class="pill pill-green" data-csp-style="s9">configured</span>':'<span class="pill pill-amber" data-csp-style="s9">not configured</span>';const keyLine=st.key_set?'API key set ✓':'<span data-csp-style="s160">No API key set</span>';let modelSel='<select class="select" id="llm-model" data-csp-style="s49" data-onchange="h128">';const models=(st.default_models||{});if(!Object.values(models).includes(st.model)){modelSel+='<option value="'+esc(st.model)+'" selected>'+esc(st.model)+' (current)</option>'}Object.entries(models).forEach(([p,m])=>{modelSel+='<option value="'+esc(m)+'"'+(m===st.model?' selected':'')+'>'+esc(m)+'</option>'});modelSel+='</select>';let provSel='<select class="select" id="llm-prov" data-csp-style="s49" data-onchange="h129">';['openai','anthropic','google','openrouter'].forEach(p=>{provSel+='<option value="'+p+'"'+(p===st.provider?' selected':'')+'>'+p+'</option>'});provSel+='</select>';el.innerHTML=`<div class="cfg-row" data-csp-style="s110"><span>Provider · Model</span><span>${badge}</span></div><div data-csp-style="s161">${provSel}${modelSel}</div><div class="cfg-row" data-csp-style="s162">${keyLine} · extractor: <code data-csp-style="s159">${esc(st.extractor)}</code></div><div data-csp-style="s163">Add this to your <code data-csp-style="s159">.env</code> and restart Engraphis:</div><div data-csp-style="s164"><textarea id="llm-snippet" class="input" readonly data-csp-style="s165">${esc(st.env_snippet)}</textarea><button class="btn btn-ghost btn-sm" data-csp-style="s166" data-onclick="h130">Copy</button></div><div class="cfg-row" data-csp-style="s110"><span>LLM extraction</span><span class="pill ${st.extractor_enabled?'pill-green':'pill-muted'}" data-csp-style="s9">${st.extractor_enabled?'ON':'OFF'}</span></div><div class="field-hint" data-csp-style="s97">While ON, ingested memory content is sent to your LLM provider for schema-validated extraction. OFF keeps everything on this machine.</div><div data-csp-style="s167"><button class="btn ${st.extractor_enabled?'btn-ghost':'btn-primary'} btn-sm" data-onclick="h150"${(st.extractor_enabled||!st.configured)?' disabled':''}>Turn on</button><button class="btn ${st.extractor_enabled?'btn-danger':'btn-ghost'} btn-sm" data-onclick="h151"${st.extractor_enabled?'':' disabled'}>Turn off</button></div><div data-csp-style="s167"><button class="btn btn-primary btn-sm" data-onclick="h131">Test connection</button><span id="llm-test-result" data-csp-style="s168"></span></div>`}catch(e){el.innerHTML='<div class="empty" data-csp-style="s10">'+esc(e.message)+'</div>'}}
function onLlmProvChange(){const p=document.getElementById('llm-prov').value;const sel=document.getElementById('llm-model');const defs={openai:'gpt-4o-mini',anthropic:'claude-3-5-sonnet-20241022',google:'gemini-1.5-flash',openrouter:'openai/gpt-4o-mini'};if(sel&&defs[p]){sel.value=defs[p]}updateLlmSnippet()}
function updateLlmSnippet(){const p=(document.getElementById('llm-prov')||{}).value||'openai';const m=(document.getElementById('llm-model')||{}).value||'';const ta=document.getElementById('llm-snippet');if(!ta)return;ta.value='ENGRAPHIS_LLM_PROVIDER='+p+'\nENGRAPHIS_LLM_MODEL='+m+'\nENGRAPHIS_LLM_API_KEY=<your-key>\nENGRAPHIS_EXTRACTOR=llm_structured\n'}
function copyLlmSnippet(){const ta=document.getElementById('llm-snippet');if(!ta)return;ta.select();try{navigator.clipboard.writeText(ta.value);toast('Copied .env snippet','ok')}catch(e){toast('Copy failed — select and Ctrl+C','err')}}
async function setLlmExtractor(on){try{const d=await api('/llm/extractor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!!on})});const ok=!!d.extractor_enabled;toast(ok?'LLM extraction turned on — new memories will be sent to your provider':'LLM extraction turned off — memories stay on this machine'+(d.persisted===false?' (could not save for restart)':''),ok?'ok':'muted');loadLlmStatus()}catch(e){toast(e.message,'err')}}
async function testLlm(){const r=document.getElementById('llm-test-result');if(r){r.textContent='Testing…';setTone(r,'muted')}try{const d=await api('/llm/test',{method:'POST'});if(r){if(d.ok){const transient=d.auto_enabled&&d.persisted===false;r.textContent=(transient?'⚠ ':'✓ ')+'Connected — '+esc(d.provider)+'/'+esc(d.model)+(transient?' Extraction is active for this process, but the setting could not be saved for restart. Set ENGRAPHIS_EXTRACTOR=llm_structured and ENGRAPHIS_LLM_AUTO_EXTRACT=1 in the deployment environment.':'');setTone(r,transient?'red':'green')}else{r.textContent='✗ '+(d.error||'failed');setTone(r,'red')}}}catch(e){if(r){r.textContent='✗ '+esc(e.message);setTone(r,'red')}}}

async function loadHostedAgentAccess(){const el=document.getElementById('tokens-body');if(!el)return;let url=hostedPlanUrl('team');try{const st=await api('/auth/state');if(url==='#'&&st&&st.cloud_url)url=safeUrl(st.cloud_url)}catch(e){}el.innerHTML=`<div class="field-hint">Per-member agent accounts, roles, named seats, and rotating device credentials are managed in Team Cloud, not by this local dashboard.</div><div data-csp-style="s167"><a class="btn btn-primary btn-sm" href="${esc(url)}" target="_blank" rel="noopener">Open Team Cloud</a><a class="btn btn-ghost btn-sm" href="https://github.com/Coding-Dev-Tools/engraphis/blob/main/docs/AGENT_CONNECT.md" target="_blank" rel="noopener">Agent Connect guide</a></div>`}
async function loadSyncStatus(){try{const d=await api('/sync/status');renderSync(d)}catch(e){const el=document.getElementById('sync-body');if(el)el.innerHTML=unlockHtml('Cloud Sync','pro')}}
function renderSync(d){const el=document.getElementById('sync-body');if(!el)return;d=d||{};if(!d.available){el.innerHTML=unlockHtml('Cloud Sync','pro');return}const last=d.last;let status='Cloud session connected; no sync recorded on this installation.';if(last){const when=new Date((last.at||0)*1000).toLocaleString();status='Last synced '+when+' — pushed '+(last.exported||0)+', +'+(last.added||0)+' received'+((last.errors&&last.errors.length)?' · '+last.errors.length+' issue(s)':'')+'.'}el.innerHTML=`<div class="cfg-row"><span>Hosted relay</span><span class="pill pill-green">CONNECTED</span></div><div class="field-hint">Relay storage and authorization run in Engraphis Cloud. This package contains only the customer client; it does not run a local relay or background scheduler.</div><div data-csp-style="s177"><button class="btn btn-primary" id="sync-btn" data-onclick="h136">Sync now</button><span class="field-hint" id="sync-status">${esc(status)}</span></div>`}
async function syncNow(){const b=document.getElementById('sync-btn');const s=document.getElementById('sync-status');if(b){b.disabled=true;b.textContent='Syncing…'}if(s)s.textContent='Contacting the cloud…';try{const d=await api('/sync/run',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const su=d.summary||{};toast('Synced — pushed '+(su.exported||0)+', '+(su.added||0)+' new from other devices','ok');await loadSyncStatus()}catch(e){toast('Sync failed: '+e.message,'err');if(b){b.disabled=false;b.textContent='Sync now'}if(s)s.textContent='Sync failed — try again.'}}

/* ─── knowledge graph (force-graph + d3-force: compact defaults and selectable layouts) ─── */
let GRAPH=null, FG=null, GRESIZE=false, GRESIZEFRAME=0, GADJ={}, GCOMM_ADJ={}, GCOMPONENTS={}, GCOMPONENT_LAYOUT=null, GHILITE=null, GHOVERSET=null, GLABELRANK={}, GLABELBOXES=[], GDATA_CACHE=null, GACTIVE_DATA=null, GREDRAWFRAME=0, GPERF={large:false,dense:false};
const GRAPH_PRESETS={
 original:{label:'Original force',repel:120,link:30,gravity:14,font:13,size:3,linkw:1,labelDensity:40,curve:0,particles:0},
 compact:{label:'Compact clusters',repel:42,link:20,gravity:26,font:12,size:3,linkw:.7,labelDensity:30,curve:.08,particles:0},
 communities:{label:'Community islands',repel:48,link:16,gravity:48,font:12,size:3,linkw:.72,labelDensity:24,curve:.12,particles:0},
 radial:{label:'Radial orbit',repel:68,link:26,gravity:12,font:13,size:3,linkw:.75,labelDensity:55,curve:.22,particles:0},
 constellation:{label:'Constellation flow',repel:34,link:16,gravity:38,font:12,size:3,linkw:.65,labelDensity:35,curve:.32,particles:2},
 custom:{label:'Custom tuning',curve:.1,particles:0}
};
window.GSET=window.GSET||{mode:'communities',font:12,size:3,repel:48,link:16,gravity:48,labels:false,linkw:.72,labelDensity:24,flow:true,frozen:false};
const ETYPE_TOKEN={person_or_concept:'--entity-concept',mention:'--entity-mention',hashtag:'--entity-hashtag',email:'--entity-email',organization:'--entity-organization',location:'--entity-location'};
const GRAPH_PALETTES={
 theme:null,
 aurora:{person_or_concept:'#8b7cf6',mention:'#2dd4bf',hashtag:'#fbbf24',email:'#60a5fa',organization:'#f472b6',location:'#a3e635'},
 ocean:{person_or_concept:'#38bdf8',mention:'#2dd4bf',hashtag:'#facc15',email:'#818cf8',organization:'#22d3ee',location:'#34d399'},
 ember:{person_or_concept:'#f97316',mention:'#fb7185',hashtag:'#facc15',email:'#a78bfa',organization:'#ef4444',location:'#84cc16'},
 contrast:{person_or_concept:'#0072b2',mention:'#009e73',hashtag:'#e69f00',email:'#56b4e9',organization:'#cc79a7',location:'#d55e00'}
};
const GRAPH_COLOR_KEY='engraphis-graph-colors-v1';
let GCOLOR_OVERRIDES={}, GCOLOR_PALETTE='theme';
function cssvar(name,fallback){const value=getComputedStyle(document.body).getPropertyValue(name).trim();return value||fallback}
function graphValidColor(color){return /^#[0-9a-f]{6}$/i.test(color||'')}
function graphTypeLabel(type){return String(type||'entity').replace(/_/g,' ')}
function graphLoadColorPreferences(){
 try{
  const saved=JSON.parse(localStorage.getItem(GRAPH_COLOR_KEY)||'{}'),colors=saved&&saved.colors;
  if(colors&&typeof colors==='object')Object.entries(colors).forEach(([type,color])=>{if(graphValidColor(color))GCOLOR_OVERRIDES[type]=color.toLowerCase()});
  if(saved&&saved.palette&&Object.prototype.hasOwnProperty.call(GRAPH_PALETTES,saved.palette))GCOLOR_PALETTE=saved.palette;
  else if(Object.keys(GCOLOR_OVERRIDES).length)GCOLOR_PALETTE='custom';
 }catch(e){GCOLOR_OVERRIDES={};GCOLOR_PALETTE='theme'}
}
function graphSaveColorPreferences(){try{localStorage.setItem(GRAPH_COLOR_KEY,JSON.stringify({palette:GCOLOR_PALETTE,colors:GCOLOR_OVERRIDES}))}catch(e){}}
function graphTypeColor(type){if(GCOLOR_OVERRIDES[type])return GCOLOR_OVERRIDES[type];if(typeof GSTYLE!=='undefined'&&GSTYLE&&GSTYLE!=='classic'&&STYLE_PAL[GSTYLE]&&STYLE_PAL[GSTYLE][type])return STYLE_PAL[GSTYLE][type];return cssvar(ETYPE_TOKEN[type]||'--entity-concept',cssvar('--color-accent','#8c83e8'))}
function graphContrastColor(color){if(!graphValidColor(color))return cssvar('--color-canvas','#0e1014');const n=parseInt(color.slice(1),16),lum=.2126*(n>>16)+.7152*((n>>8)&255)+.0722*(n&255);return lum>150?'#111827':'#f8fafc'}
const ETYPE_COLOR=new Proxy({},{get:(_,type)=>graphTypeColor(type)});
graphLoadColorPreferences();
function graphUpdateColorSwatches(){}
function graphRefreshNodeColors(){
 const nodes=FG&&FG.graphData?FG.graphData().nodes||[]:[];
 nodes.forEach(node=>{node.color=graphNodeColor(node);node.stroke=graphContrastColor(node.color)});
 graphUpdateColorSwatches();
 graphRedraw();
}
function renderGraphColorControls(){
 const box=document.getElementById('graph-color-controls'),picker=document.getElementById('graph-palette');
 if(picker)picker.value=GCOLOR_PALETTE;
 if(!box)return;
 const types=GRAPH&&GRAPH.types&&GRAPH.types.length?GRAPH.types.map(item=>item.etype):Object.keys(ETYPE_TOKEN);
 box.innerHTML=types.map(type=>{const label=graphTypeLabel(type),color=graphTypeColor(type);return `<label class="graph-color-item" title="Change ${esc(label)} node color"><input class="graph-color-input" type="color" value="${color}" data-node-type="${esc(type)}" aria-label="Color for ${esc(label)} nodes" data-oninput="h138" data-onchange="h139"><span>${esc(label)}</span></label>`}).join('');
}
function graphSetTypeColor(type,color,persist){
 if(!type||!graphValidColor(color))return;
 GCOLOR_OVERRIDES[type]=color.toLowerCase();GCOLOR_PALETTE='custom';
 const picker=document.getElementById('graph-palette');if(picker)picker.value='custom';
 graphRefreshNodeColors();
 if(persist)graphSaveColorPreferences();
}
function graphApplyPalette(name){
 if(!Object.prototype.hasOwnProperty.call(GRAPH_PALETTES,name))name='theme';
 GCOLOR_PALETTE=name;GCOLOR_OVERRIDES=GRAPH_PALETTES[name]?{...GRAPH_PALETTES[name]}:{};
 graphSaveColorPreferences();graphRecolor();
}
function graphResetColors(){graphApplyPalette('theme');toast('Node colors reset to the active theme','ok')}
function graphInjectCss(){}
function prefersReducedMotion(){return matchMedia('(prefers-reduced-motion: reduce)').matches}
function graphSetLayoutStatus(text,busy){
 for(const id of ['graph-layout-status','galaxy-layout-status']){const status=document.getElementById(id);if(status){status.textContent=text;status.classList.toggle('busy',!!busy)}}
 for(const id of ['graph-net','galaxy-net']){const net=document.getElementById(id);if(net)net.setAttribute('aria-busy',String(!!busy))}
}
function graphSetSimulationStatus(text,busy){
 graphSetLayoutStatus(text,busy)
}
function graphUpdateHud(data){
 const mode=document.getElementById('graph-hud-mode'),count=document.getElementById('graph-hud-count'),badge=document.getElementById('graph-performance-badge');
 const preset=GRAPH_PRESETS[window.GSET.mode]||GRAPH_PRESETS.compact;
 if(mode)mode.textContent=preset.label||'Custom graph';
 if(count&&data)count.textContent=data.nodes.length.toLocaleString()+' entities · '+data.links.length.toLocaleString()+' relations';
 if(badge)badge.textContent=GPERF.large?'Large graph mode':'Adaptive rendering';
}
function graphInvalidateData(){GDATA_CACHE=null;GACTIVE_DATA=null;GCOMPONENT_LAYOUT=null;GHILITE=null;GHOVERSET=null}
async function loadLegacyGraph(){
 graphInjectCss();graphInvalidateData();GRAPH=null;
 const empty=document.getElementById('graph-empty'),net=document.getElementById('graph-net'),nodesBox=document.getElementById('graph-entity-list'),edgesBox=document.getElementById('graph-relation-list');
 showAs(empty,true,'flex');empty.textContent='Loading graph…';graphSetLayoutStatus('Loading data',true);
 if(net)net.setAttribute('aria-busy','true');
 renderGraphExplorer();
 if(!GRESIZE){
  GRESIZE=true;
  window.addEventListener('resize',()=>{
   if(!FG||GRESIZEFRAME)return;
   GRESIZEFRAME=requestAnimationFrame(()=>{GRESIZEFRAME=0;const element=document.getElementById('graph-net');if(FG&&element)FG.width(element.clientWidth).height(element.clientHeight)});
  });
 }
 const layerInputs=Array.from(document.querySelectorAll('#graph-layer-filters input')),selectedLayers=layerInputs.filter(input=>input.checked).map(input=>input.value),layerFilter=selectedLayers.length===layerInputs.length?'':'&layers='+encodeURIComponent(selectedLayers.join(',')),includeCode=document.getElementById('graph-include-code').checked,repo=(document.getElementById('graph-repo-filter').value||'').trim();
 try{
   GRAPH=await api('/graph?workspace='+encodeURIComponent(WS||'')+layerFilter+'&include_code='+(includeCode?'true':'false')+(repo?'&repo='+encodeURIComponent(repo):''));
  renderGraphSide();graphRender();
 }catch(error){
  showAs(empty,true,'flex');empty.textContent='Graph failed: '+error.message;graphSetLayoutStatus('Load failed',false);
 }finally{
  if(net)net.setAttribute('aria-busy','false');
  if(!GRAPH){
   if(FG)FG.graphData({nodes:[],links:[]});
   const message=empty.textContent||'Graph data unavailable.';
   if(nodesBox)nodesBox.innerHTML='<div class="empty" data-csp-style="s67">'+esc(message)+'</div>';
   if(edgesBox)edgesBox.innerHTML='<div class="empty" data-csp-style="s67">Relations are unavailable until graph data loads.</div>';
  }
 }
}
function graphData(){
 const _si=document.getElementById('graph-show-iso');const hideIso=!(_si&&_si.checked);
 if(GDATA_CACHE&&GDATA_CACHE.graph===GRAPH&&GDATA_CACHE.hideIso===hideIso)return GDATA_CACHE.data;
 let sourceNodes=GRAPH.nodes;if(hideIso)sourceNodes=sourceNodes.filter(node=>node.degree>0);
 const names=new Set(sourceNodes.map(node=>node.id));
 const nodes=sourceNodes.map(node=>({id:node.id,label:node.label||node.id,displayLabel:(node.label||node.id).length>30?(node.label||node.id).slice(0,29)+'…':(node.label||node.id),etype:node.etype,degree:node.degree||0,val:1+(node.degree||0)}));
 nodes.sort((a,b)=>b.degree-a.degree).forEach((node,index)=>{node.rank=index;node.hub=index<24;node.radius=Math.max(1.6,window.GSET.size*Math.sqrt(node.val)*.45);node.color=graphTypeColor(node.etype);node.stroke=graphContrastColor(node.color)});
 const links=GRAPH.edges.filter(edge=>names.has(edge.from)&&names.has(edge.to)).map(edge=>({source:edge.from,target:edge.to,label:edge.label,layer:edge.layer||'semantic'}));
 const data={nodes,links};GDATA_CACHE={graph:GRAPH,hideIso,data};return data;
}
function buildAdj(links){
 GADJ={};GCOMM_ADJ={};
 links.forEach(link=>{
  const source=(link.source&&link.source.id)||link.source,target=(link.target&&link.target.id)||link.target;
  (GADJ[source]=GADJ[source]||new Set()).add(target);(GADJ[target]=GADJ[target]||new Set()).add(source);
  GCOMM_ADJ[source]=GCOMM_ADJ[source]||new Set();GCOMM_ADJ[target]=GCOMM_ADJ[target]||new Set();
  // Influence links often connect otherwise distinct bodies of work. Keep them
  // visible, but do not let a few such bridges collapse all communities into one.
  if(link.label!=='influences'){
   (GCOMM_ADJ[source]=GCOMM_ADJ[source]||new Set()).add(target);(GCOMM_ADJ[target]=GCOMM_ADJ[target]||new Set()).add(source);
  }
 });
}
function graphIndexComponents(nodes){
 const seen=new Set(),components=[];
 nodes.forEach(node=>{
  if(seen.has(node.id))return;
  const ids=[],stack=[node.id];seen.add(node.id);
  while(stack.length){const id=stack.pop();ids.push(id);(GADJ[id]||[]).forEach(next=>{if(!seen.has(next)){seen.add(next);stack.push(next)}})}
  components.push(ids);
 });
 components.sort((a,b)=>b.length-a.length);
 GCOMPONENTS={};
 const cols=Math.max(1,Math.ceil(Math.sqrt(components.length))),gap=Math.max(84,window.GSET.link*4);
 components.forEach((ids,index)=>{
  const row=Math.floor(index/cols),col=index%cols,used=Math.min(cols,components.length-row*cols);
  const x=(col-(used-1)/2)*gap,y=(row-(Math.ceil(components.length/cols)-1)/2)*gap;
  ids.forEach(id=>{GCOMPONENTS[id]={index,size:ids.length,x,y}});
 });
}
function graphIndexCommunities(nodes){
 const groups={};
 nodes.forEach(node=>{const key=Number.isFinite(node.community)?node.community:0;(groups[key]=groups[key]||[]).push(node);});
 const communities=Object.entries(groups).sort((a,b)=>b[1].length-a[1].length);
 const cols=Math.max(1,Math.ceil(Math.sqrt(communities.length))),gap=Math.max(150,window.GSET.link*9);
 GCOMPONENTS={};
 communities.forEach(([key,members],index)=>{
  const row=Math.floor(index/cols),col=index%cols,used=Math.min(cols,communities.length-row*cols);
  const x=(col-(used-1)/2)*gap,y=(row-(Math.ceil(communities.length/cols)-1)/2)*gap;
  members.forEach(node=>{GCOMPONENTS[node.id]={index,size:members.length,x,y,community:Number(key)};});
 });
}
function graphRefreshComponentCenters(nodes,force=false){
 const layout=window.GSET.mode+'|'+window.GSET.link;
 if(!force&&GCOMPONENT_LAYOUT===layout)return;
 if(window.GSET.mode==='communities')graphIndexCommunities(nodes);else graphIndexComponents(nodes);GCOMPONENT_LAYOUT=layout;
}
function graphAlpha(color,alpha){
 const hex=/^#([0-9a-f]{6})$/i.exec(color||'');
 if(hex){const value=parseInt(hex[1],16);return `rgba(${value>>16},${(value>>8)&255},${value&255},${alpha})`}
 const rgb=(color||'').match(/\d+(?:\.\d+)?/g);
 return rgb&&rgb.length>=3?`rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha})`:color;
}
function graphReadThemeColors(){
 const layers=(typeof GSTYLE!=='undefined'&&GSTYLE&&GSTYLE!=='classic'&&STYLE_LAYERS[GSTYLE])?Object.assign({},STYLE_LAYERS[GSTYLE]):{temporal:cssvar('--color-info','#6f9fd8'),entity:cssvar('--color-teal','#5aafb3'),causal:cssvar('--color-warning','#d7a84b'),semantic:cssvar('--color-accent','#8c83e8')};
 const baseAlpha=GPERF.dense?.18:.32,links={};
 Object.entries(layers).forEach(([layer,color])=>{links[layer]={base:graphAlpha(color,baseAlpha),active:graphAlpha(color,.82),dim:graphAlpha(color,.035)}});
 return {label:cssvar('--color-text','#e7e9ee'),dim:cssvar('--color-text-dim','#7e8795'),accent:cssvar('--color-accent','#8c83e8'),panel:cssvar('--color-panel','#15181e'),canvas:cssvar('--color-canvas','#0e1014'),layers,links};
}
/* ─── graph aesthetic styles (Galaxy · Solar system · Cyberpunk), additive ─── */
var STYLE_PAL={
 galaxy:{person_or_concept:'#b789ff',mention:'#7bb4ff',hashtag:'#ffcf6b',email:'#8aa2ff',organization:'#66e0d0',location:'#ff7ea8'},
 solar:{person_or_concept:'#ffb454',mention:'#3fd2c7',hashtag:'#ffd68a',email:'#8ea8ff',organization:'#5b9bff',location:'#ff8f6b'},
 cyber:{person_or_concept:'#ff3ea5',mention:'#b6ff3c',hashtag:'#ffe14d',email:'#8b7bff',organization:'#22e0ff',location:'#ff5c7a'}
};
var STYLE_LAYERS={
 galaxy:{temporal:'#7bb4ff',entity:'#66e0d0',causal:'#ffcf6b',semantic:'#b789ff'},
 solar:{temporal:'#5b9bff',entity:'#3fd2c7',causal:'#ffb454',semantic:'#ffd68a'},
 cyber:{temporal:'#22e0ff',entity:'#b6ff3c',causal:'#ffe14d',semantic:'#ff3ea5'}
};
var STYLE_BG={
 classic:'',
 galaxy:'radial-gradient(58% 50% at 24% 22%,rgba(126,64,208,.30),transparent 66%),radial-gradient(52% 58% at 82% 78%,rgba(220,72,164,.20),transparent 68%),radial-gradient(46% 52% at 62% 42%,rgba(58,120,224,.16),transparent 70%),#06040f',
 solar:'radial-gradient(40% 48% at 50% 50%,rgba(255,184,92,.16),transparent 60%),radial-gradient(90% 90% at 50% 50%,rgba(18,32,64,.55),transparent 82%),#05070d',
 cyber:'linear-gradient(rgba(34,224,255,.055) 1px,transparent 1px) 0 0/30px 30px,linear-gradient(90deg,rgba(34,224,255,.055) 1px,transparent 1px) 0 0/30px 30px,radial-gradient(72% 60% at 50% 0%,rgba(255,62,165,.12),transparent 72%),#050810'
};
var GSTYLE='cyber';try{var _gs=localStorage.getItem('engraphis-graph-style');if(_gs)GSTYLE=_gs;}catch(e){}
function graphMakeStars(){var a=[],c=['#dfe6ff','#dfe6ff','#c9b6ff','#a7c6ff','#ffd9ef'];for(var i=0;i<110;i++){a.push({x:(Math.random()-.5)*1200,y:(Math.random()-.5)*1200,r:Math.random()*1.1+.25,a:Math.random()*.7+.25,tw:Math.random()*1.6+.4,ph:Math.random()*6.28,c:c[i%c.length]});}return a;}
var GSTARS=graphMakeStars();
function graphLighten(c,amt){var r,g,b;if(c[0]==='#'){var n=parseInt(c.slice(1),16);r=n>>16&255;g=n>>8&255;b=n&255;}else{var m=c.match(/\d+/g);r=+m[0];g=+m[1];b=+m[2];}r=Math.round(r+(255-r)*amt);g=Math.round(g+(255-g)*amt);b=Math.round(b+(255-b)*amt);return 'rgb('+r+','+g+','+b+')';}
function graphStyleBackground(ctx,scale){
 if(GSTYLE==='galaxy'){
  if(GPERF.large)return;var t=performance.now()/1000,S=GSTARS;ctx.save();ctx.globalCompositeOperation='lighter';
  for(var i=0;i<S.length;i++){var s=S[i],al=s.a*(.5+.5*Math.sin(t*s.tw+s.ph));if(al<=.02)continue;ctx.globalAlpha=al;ctx.beginPath();ctx.arc(s.x,s.y,s.r,0,6.2832);ctx.fillStyle=s.c;ctx.fill();}ctx.restore();
 }else if(GSTYLE==='solar'){
  ctx.save();var g=ctx.createRadialGradient(0,0,2,0,0,130);g.addColorStop(0,'rgba(255,192,112,.20)');g.addColorStop(.6,'rgba(255,150,80,.05)');g.addColorStop(1,'rgba(255,150,80,0)');ctx.fillStyle=g;ctx.beginPath();ctx.arc(0,0,130,0,6.2832);ctx.fill();
  ctx.strokeStyle='rgba(255,190,120,.10)';ctx.lineWidth=1/scale;var RR=[72,132,200,286,384];for(var k=0;k<RR.length;k++){ctx.beginPath();ctx.ellipse(0,0,RR[k],RR[k]*.66,0,0,6.2832);ctx.stroke();}ctx.restore();
 }
}
function graphStyleNode(node,ctx,scale){
 if(!Number.isFinite(node.x)||!Number.isFinite(node.y))return;
 var focus=GHOVERSET&&GHOVERSET.size>1,neighbor=focus&&GHOVERSET.has(node.id),dim=focus&&!neighbor;
 var r=node.radius,col=node.color,rich=node.id===GHILITE||neighbor||node.hub||node.degree>=3;
 ctx.globalAlpha=dim?.12:1;
 if(GSTYLE==='galaxy'){
  if(rich&&!GPERF.large){ctx.save();ctx.globalCompositeOperation='lighter';var R=r*(node.id===GHILITE?4.4:3.0);var g=ctx.createRadialGradient(node.x,node.y,0,node.x,node.y,R);g.addColorStop(0,graphAlpha(col,dim?.15:.6));g.addColorStop(.42,graphAlpha(col,dim?.05:.16));g.addColorStop(1,graphAlpha(col,0));ctx.fillStyle=g;ctx.beginPath();ctx.arc(node.x,node.y,R,0,6.2832);ctx.fill();ctx.restore();}
  ctx.beginPath();ctx.arc(node.x,node.y,r,0,6.2832);ctx.fillStyle=col;ctx.fill();
  ctx.beginPath();ctx.arc(node.x,node.y,Math.max(.4,r*.4),0,6.2832);ctx.fillStyle='rgba(255,255,255,.9)';ctx.fill();
 }else if(GSTYLE==='solar'){
  var sun=node.rank===0;if(sun)r*=1.7;
  if(rich&&!GPERF.large){ctx.save();ctx.globalCompositeOperation='lighter';var cc=sun?'#ffcf6b':col,R2=r*(sun?3.4:2.1);var g2=ctx.createRadialGradient(node.x,node.y,0,node.x,node.y,R2);g2.addColorStop(0,graphAlpha(cc,dim?.1:(sun?.6:.3)));g2.addColorStop(1,graphAlpha(cc,0));ctx.fillStyle=g2;ctx.beginPath();ctx.arc(node.x,node.y,R2,0,6.2832);ctx.fill();ctx.restore();}
  if(!GPERF.large){var sg=ctx.createRadialGradient(node.x-r*.4,node.y-r*.4,Math.max(.1,r*.12),node.x,node.y,r);sg.addColorStop(0,graphLighten(sun?'#ffe4ad':col,.5));sg.addColorStop(1,sun?'#e08a25':col);ctx.fillStyle=sg;}else{ctx.fillStyle=col;}
  ctx.beginPath();ctx.arc(node.x,node.y,r,0,6.2832);ctx.fill();
 }else{
  ctx.save();if(rich&&!GPERF.large){ctx.shadowColor=col;ctx.shadowBlur=dim?2:r*2.6;}ctx.beginPath();ctx.arc(node.x,node.y,r,0,6.2832);ctx.fillStyle=col;ctx.fill();ctx.restore();
  ctx.beginPath();ctx.arc(node.x,node.y,Math.max(.4,r*.42),0,6.2832);ctx.fillStyle='#eafcff';ctx.fill();
 }
 if(node.id===GHILITE){ctx.lineWidth=1.3/scale;ctx.strokeStyle=GSTYLE==='cyber'?'#ffffff':'rgba(255,255,255,.9)';ctx.beginPath();ctx.arc(node.x,node.y,r+1.4/scale,0,6.2832);ctx.stroke();}
 ctx.globalAlpha=1;
}
function graphApplyStyleChrome(){
 var net=document.querySelector('.graph-network');if(net){for(const name of ['classic','galaxy','solar','cyber'])net.classList.toggle('graph-style-'+name,GSTYLE===name)}
 var sel=document.getElementById('graph-style');if(sel&&sel.value!==GSTYLE)sel.value=GSTYLE;
}
function graphSetStyle(name){
 if(['classic','galaxy','solar','cyber'].indexOf(name)<0)name='cyber';
 GSTYLE=name;try{localStorage.setItem('engraphis-graph-style',name)}catch(e){}
 graphApplyStyleChrome();
 if(GRAPH&&FG){graphRefreshNodeColors();graphRenderLegend();graphRender(false,false);}
}
/* ─── colorful graphs even when every node is one entity type: color by community or connections ─── */
var GRAPH_HEAT=['#3f7bff','#6a5cff','#a24bff','#e0479f','#ff6b6b','#ffc23d'];
var COMMUNITY_PALS={
 classic:['#8c83e8','#5aafb3','#d7a84b','#6f9fd8','#58b882','#df7478','#b07de0','#4fb0a0','#e0894a','#7c9be0','#e06a9a','#9ac25a'],
 galaxy:['#b789ff','#7bb4ff','#66e0d0','#ffcf6b','#ff7ea8','#8aa2ff','#c98bff','#5ad0e0','#ffa0d0','#9d7bff','#6ad0b0','#ffb060'],
 solar:['#ffb454','#5b9bff','#3fd2c7','#ffd68a','#ff8f6b','#8ea8ff','#ffc24a','#6ac0d0','#ff9f7a','#7ab0ff','#e0b050','#5fd0b0'],
 cyber:['#22e0ff','#ff3ea5','#b6ff3c','#ffe14d','#8b7bff','#ff5c7a','#3affd0','#ff7be0','#7affea','#c0ff4a','#5c9bff','#ff9b3c']
};
var GCOLORBY='community';try{var _cb=localStorage.getItem('engraphis-graph-colorby');if(_cb)GCOLORBY=_cb;}catch(e){}
var GMAXDEG=1;
function graphCommunityPalette(){return COMMUNITY_PALS[(typeof GSTYLE!=='undefined'&&COMMUNITY_PALS[GSTYLE])?GSTYLE:'classic'];}
function graphComputeCommunities(nodes){
 var byId={},seen=new Set(),groups=[];nodes.forEach(function(node){byId[node.id]=node;});
 nodes.forEach(function(node){
  if(seen.has(node.id))return;
  var group=[],stack=[node.id];seen.add(node.id);
  while(stack.length){
   var id=stack.pop();group.push(id);var neighbours=GCOMM_ADJ[id]||new Set();
   neighbours.forEach(function(next){if(byId[next]&&!seen.has(next)){seen.add(next);stack.push(next);}});
  }
  groups.push(group);
 });
 groups.sort(function(a,b){return b.length-a.length;});
 groups.forEach(function(group,index){group.forEach(function(id){byId[id].community=index;});});
}
function graphHeatColor(node){var total=(GACTIVE_DATA&&GACTIVE_DATA.nodes.length)||1;var t=(node.rank||0)/Math.max(1,total-1);var idx=Math.min(GRAPH_HEAT.length-1,Math.floor(t*GRAPH_HEAT.length));return GRAPH_HEAT[idx];}
function graphNodeColor(node){
 if(GCOLORBY==='community'){var pal=graphCommunityPalette();return pal[(node.community||0)%pal.length];}
 if(GCOLORBY==='connections'){return graphHeatColor(node);}
 return graphTypeColor(node.etype);
}
function graphRenderLegend(graph){
 graph=graph||GRAPH;var legend=document.getElementById('graph-legend'),legendCount=document.getElementById('graph-legend-count');if(!legend||!graph)return;
 if(GCOLORBY==='community'){
  var nodes=(GACTIVE_DATA&&GACTIVE_DATA.nodes)||[],sizes={};nodes.forEach(function(n){var c=n.community||0;sizes[c]=(sizes[c]||0)+1;});
  var order=Object.keys(sizes).map(Number).sort(function(a,b){return sizes[b]-sizes[a];}),pal=graphCommunityPalette();
  legend.innerHTML=order.slice(0,10).map(function(c,i){return '<div class="gtype-row"><span class="gtype-dot graph-cluster-'+(i%10)+'"></span><span class="gtype-name">Cluster '+(i+1)+'</span><span class="gtype-count">'+sizes[c].toLocaleString()+'</span></div>';}).join('')||'<div class="empty" data-csp-style="s12">None</div>';
  if(legendCount)legendCount.textContent=order.length?order.length+(order.length===1?' cluster':' clusters'):'';
  return;
 }
 if(GCOLORBY==='connections'){
  legend.innerHTML='<div data-csp-style="s184"><div data-csp-style="s185">'+GRAPH_HEAT.map(function(col,i){return '<span class="graph-heat-'+i+'"></span>';}).join('')+'</div><div data-csp-style="s187"><span>Most connected</span><span>Least</span></div></div>';
  if(legendCount)legendCount.textContent='by connections';
  return;
 }
 var types=graph.types||[];
 legend.innerHTML=types.map(function(item){return '<div class="gtype-row"><span class="gtype-dot" data-graph-node-type="'+esc(item.etype)+'"></span><span class="gtype-name">'+esc(graphTypeLabel(item.etype))+'</span><span class="gtype-count">'+item.count.toLocaleString()+'</span></div>';}).join('')||'<div class="empty" data-csp-style="s12">None</div>';
 if(legendCount)legendCount.textContent=types.length?types.length+(types.length===1?' type':' types'):'';
}
function graphSetColorBy(mode){
 if(['type','community','connections'].indexOf(mode)<0)mode='community';
 GCOLORBY=mode;try{localStorage.setItem('engraphis-graph-colorby',mode)}catch(e){}
 var sel=document.getElementById('graph-colorby');if(sel&&sel.value!==mode)sel.value=mode;
 if(GRAPH&&FG&&GACTIVE_DATA){graphComputeCommunities(GACTIVE_DATA.nodes);GMAXDEG=GACTIVE_DATA.nodes.reduce(function(m,n){return Math.max(m,n.degree||0);},1);graphRefreshNodeColors();graphRenderLegend();}
}
function graphApplyForces(){
 if(!FG)return;
 const settings=window.GSET,mode=settings.mode||'compact';
 FG.d3Force('charge').strength(-settings.repel);
 FG.d3Force('link').distance(settings.link);
 if(typeof d3==='undefined')return;
 FG.d3Force('radial',null);
 if(mode==='communities'){
  const target=node=>GCOMPONENTS[node.id]||{x:0,y:0};
  FG.d3Force('x',d3.forceX(node=>target(node).x).strength(settings.gravity/100));
  FG.d3Force('y',d3.forceY(node=>target(node).y).strength(settings.gravity/100));
 }else{
  const centering=mode==='radial'?Math.max(.04,settings.gravity/300):settings.gravity/100;
  FG.d3Force('x',d3.forceX(0).strength(centering));
  FG.d3Force('y',d3.forceY(0).strength(centering));
  if(mode==='radial'&&d3.forceRadial)FG.d3Force('radial',d3.forceRadial(node=>Math.max(0,5-Math.min(5,node.degree||0))*Math.max(8,settings.link*.72)).strength(.32));
 }
 FG.d3Force('collide',d3.forceCollide(node=>node.radius+1.5).iterations(GPERF.large?1:2));
}
function graphSetHighlight(id){
 GHILITE=id||null;
 GHOVERSET=id?new Set([id,...(GADJ[id]||[])]):null;
 graphRedraw();
}
function graphRefreshNodeMetrics(){
 const nodes=FG&&FG.graphData?FG.graphData().nodes||[]:[];
 nodes.forEach(node=>{node.radius=Math.max(1.6,window.GSET.size*Math.sqrt(node.val)*.45)});
}
function graphRedraw(){
 if(!FG||GREDRAWFRAME)return;
 GREDRAWFRAME=requestAnimationFrame(()=>{GREDRAWFRAME=0;if(FG)FG.nodeCanvasObject(FG.nodeCanvasObject())});
}
let FORCE_GRAPH_LOADING=null;
function loadForceGraph(){
 if(typeof ForceGraph!=='undefined')return Promise.resolve();
 if(FORCE_GRAPH_LOADING)return FORCE_GRAPH_LOADING;
 FORCE_GRAPH_LOADING=new Promise((resolve,reject)=>{
  const script=document.createElement('script');
  script.src='/static/vendor/force-graph.min.js';
  script.onload=()=>resolve();
  script.onerror=()=>reject(new Error('Graph engine could not load'));
  document.head.appendChild(script);
 });
 return FORCE_GRAPH_LOADING;
}
function graphRender(fit=true,reheat=true){
 const empty=document.getElementById('graph-empty');
 if(typeof ForceGraph==='undefined'){
  showAs(empty,true,'flex');empty.textContent='Loading graph engine…';
  graphSetLayoutStatus('Loading engine',true);
  loadForceGraph().then(()=>graphRender(fit,reheat)).catch(error=>{
   empty.textContent=error.message+'; refresh or verify the installed static assets.';
   graphSetLayoutStatus('Engine unavailable',false);
  });
  return;
 }
 const element=document.getElementById('graph-net'),settings=window.GSET,mode=GRAPH_PRESETS[settings.mode]||GRAPH_PRESETS.compact,data=graphData(),dataChanged=GACTIVE_DATA!==data;
 GPERF={large:data.nodes.length>600||data.links.length>2400,dense:data.links.length>1500};
 graphSyncReadouts();graphUpdateEditedBadge();
 if(dataChanged){
  buildAdj(data.links);graphComputeCommunities(data.nodes);GMAXDEG=data.nodes.reduce((m,n)=>Math.max(m,n.degree||0),1);data.nodes.forEach(node=>{node.color=graphNodeColor(node);node.stroke=graphContrastColor(node.color)});GLABELRANK={};data.nodes.forEach(node=>{GLABELRANK[node.id]=node.rank});GACTIVE_DATA=data;graphRenderLegend(GRAPH);
 }
 graphRefreshComponentCenters(data.nodes,dataChanged);
 if(dataChanged)graphSetHighlight(null);
 if(!data.nodes.length){
  showAs(empty,true,'flex');
  empty.textContent=GRAPH.nodes.length?('No connected entities — tick "Show unlinked" to see all '+GRAPH.nodes.length+'.'):'No entities in this workspace yet.';
  if(FG)FG.graphData({nodes:[],links:[]});
  graphUpdateHud(data);graphSetLayoutStatus('No entities',false);return;
 }
 showAs(empty,false);window.GCOL=graphReadThemeColors();graphApplyStyleChrome();graphUpdateHud(data);
 const reduced=prefersReducedMotion(),reheatButton=document.querySelector('[data-onclick="h27"]');
 if(reheatButton){reheatButton.setAttribute('aria-disabled',String(reduced));reheatButton.setAttribute('aria-label',reduced?'Reheat layout unavailable while reduced motion is enabled':'Reheat layout');reheatButton.title=reduced?'Unavailable while reduced motion is enabled':''}
 if(!FG){
  FG=ForceGraph()(element);
  FG.backgroundColor('rgba(0,0,0,0)').nodeRelSize(1).autoPauseRedraw(true)
   .onRenderFramePre((ctx,scale)=>{try{graphStyleBackground(ctx,scale)}catch(e){}})
   .onNodeClick(node=>{syncGraphExplorerSelection(node.id);graphNodeClick(node.label||node.id)})
   .onNodeHover(node=>{graphSetHighlight(node&&node.id);element.classList.toggle('cursor-pointer',!!node);element.classList.toggle('cursor-grab',!node)})
   .onEngineStop(()=>graphSetSimulationStatus(prefersReducedMotion()?'Static layout':'Layout settled',false));
 }
 FG.width(element.clientWidth).height(element.clientHeight)
  .cooldownTime(reduced?0:(GPERF.large?1100:2200))
  .cooldownTicks(reduced?1:(GPERF.large?80:160))
  .warmupTicks(reduced?45:(GPERF.large?18:40))
  .autoPauseRedraw(true);
 if(FG.d3AlphaDecay)FG.d3AlphaDecay(GPERF.large?.055:.035);
 if(FG.d3VelocityDecay)FG.d3VelocityDecay(GPERF.large?.45:.38);
 FG.nodeCanvasObject((node,ctx,scale)=>{
  if(typeof GSTYLE!=='undefined'&&GSTYLE!=='classic'){graphStyleNode(node,ctx,scale);return;}
  const focus=GHOVERSET&&GHOVERSET.size>1,neighbor=focus&&GHOVERSET.has(node.id),dim=focus&&!neighbor,radius=node.radius,color=node.color;
  ctx.globalAlpha=dim?.08:1;
  if(!dim&&(node.id===GHILITE||node.hub)){
   ctx.beginPath();ctx.arc(node.x,node.y,radius+(node.id===GHILITE?3.4:2.2)/scale,0,2*Math.PI);ctx.fillStyle=color;ctx.globalAlpha=node.id===GHILITE?.32:.14;ctx.fill();ctx.globalAlpha=1;
  }
  ctx.beginPath();ctx.arc(node.x,node.y,radius,0,2*Math.PI);ctx.fillStyle=color;ctx.fill();
  ctx.lineWidth=.55/scale;ctx.strokeStyle=node.stroke;ctx.globalAlpha=dim?.06:.38;ctx.stroke();
  if(settings.mode==='constellation'&&!GPERF.large&&!dim){ctx.beginPath();ctx.arc(node.x,node.y,Math.max(.7,radius*.24),0,2*Math.PI);ctx.fillStyle=window.GCOL.label;ctx.globalAlpha=.72;ctx.fill()}
  if(node.id===GHILITE){ctx.beginPath();ctx.arc(node.x,node.y,radius+1.4/scale,0,2*Math.PI);ctx.lineWidth=1.5/scale;ctx.strokeStyle=window.GCOL.label;ctx.globalAlpha=1;ctx.stroke()}
  ctx.globalAlpha=1;
 });
 FG.nodePointerAreaPaint((node,color,ctx)=>{const radius=Math.max(3,node.radius)+3;ctx.beginPath();ctx.arc(node.x,node.y,radius,0,2*Math.PI);ctx.fillStyle=color;ctx.fill()});
 FG.linkColor(link=>{
  const focus=GHOVERSET&&GHOVERSET.size>1,source=(link.source&&link.source.id)||link.source,target=(link.target&&link.target.id)||link.target,active=!focus||source===GHILITE||target===GHILITE;
  const palette=window.GCOL.links[link.layer]||window.GCOL.links.semantic;
  if(link.label==='influences')return active?graphAlpha(window.GCOL.layers[link.layer]||window.GCOL.layers.semantic,focus?.34:.18):palette.dim;
  return active?(focus?palette.active:palette.base):palette.dim;
 });
 FG.linkWidth(link=>{const width=window.GSET.linkw||1,focus=GHOVERSET&&GHOVERSET.size>1,bridge=link.label==='influences';if(!focus)return (bridge?.45:(GPERF.dense?.62:.82))*width;const source=(link.source&&link.source.id)||link.source,target=(link.target&&link.target.id)||link.target;return (source===GHILITE||target===GHILITE)?(bridge?1.0:1.8)*width:.25*width});
 if(FG.linkLineDash)FG.linkLineDash(GPERF.dense?null:(link=>link.layer==='temporal'?[4,3]:(link.layer==='causal'?[2,2]:null)));
 if(FG.linkCurvature)FG.linkCurvature(GPERF.dense?0:mode.curve);
 FG.linkDirectionalArrowLength(GPERF.dense?0:2.5).linkDirectionalArrowRelPos(1);
 if(FG.linkDirectionalParticles){FG.linkDirectionalParticles((reduced||data.links.length>800||window.GSET.flow===false)?0:(GSTYLE==='cyber'?2:(mode.particles||2))).linkDirectionalParticleWidth(1.7).linkDirectionalParticleSpeed(.004)}
 if(settings.labels){
  FG.linkCanvasObjectMode(()=>'after').linkCanvasObject((link,ctx,scale)=>{
   if(scale<2.4||!link.label||!link.source.x||(GPERF.dense&&!GHILITE))return;
   const fontSize=(settings.font*.82)/scale;ctx.font=fontSize+'px sans-serif';ctx.fillStyle=window.GCOL.dim;ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(link.label,(link.source.x+link.target.x)/2,(link.source.y+link.target.y)/2);
  });
 }else{FG.linkCanvasObjectMode(()=>undefined)}
 FG.onRenderFramePost((ctx,scale)=>{
  if(!(settings.font>0))return;
  const nodes=(GACTIVE_DATA&&GACTIVE_DATA.nodes)||[];
  if(!nodes.length)return;
  const focus=GHOVERSET&&GHOVERSET.size>1,cap=Math.max(1,Math.round((settings.labelDensity||40)*Math.max(.32,scale-.85))),fontSize=settings.font/scale,boxes=GLABELBOXES,candidateLimit=Math.min(nodes.length,Math.max(72,cap*10));
  boxes.length=0;
  ctx.textAlign='center';ctx.textBaseline='top';ctx.lineJoin='round';ctx.font=fontSize+'px -apple-system,Segoe UI,sans-serif';ctx.lineWidth=3/scale;ctx.strokeStyle=window.GCOL.panel;ctx.fillStyle=window.GCOL.label;
  let drawn=0;
  for(let pass=GHILITE?0:1;pass<2&&drawn<cap;pass++){
   const scanLimit=pass===0?nodes.length:candidateLimit;
   for(let index=0;index<scanLimit&&drawn<cap;index++){
    const node=nodes[index];if(node.x==null||node.y==null)continue;
    const selected=node.id===GHILITE;if((pass===0&&!selected)||(pass===1&&selected))continue;
    if(focus&&!GHOVERSET.has(node.id))continue;
    const y=node.y+node.radius+2/scale,width=Math.max(10/scale,node.displayLabel.length*fontSize*.56),pad=3/scale,left=node.x-width/2-pad,right=node.x+width/2+pad,top=y-pad,bottom=y+fontSize*1.3+pad;
    let collision=false;
    for(let offset=0;offset<boxes.length&&!selected;offset+=4){if(left<boxes[offset+2]&&right>boxes[offset]&&top<boxes[offset+3]&&bottom>boxes[offset+1])collision=true}
    if(collision)continue;
    ctx.strokeText(node.displayLabel,node.x,y);ctx.fillText(node.displayLabel,node.x,y);boxes.push(left,top,right,bottom);drawn++;
   }
  }
 });
 if(dataChanged)FG.graphData(data);
 graphApplyForces();
 if(reheat){graphSetSimulationStatus(reduced?'Static layout':'Arranging entities',!reduced);if(!reduced)FG.d3ReheatSimulation()}
 else graphRedraw();
 clearTimeout(window.__gfit);
 if(fit){
  if(reduced)requestAnimationFrame(()=>{if(FG)FG.zoomToFit(0,72)});
  else window.__gfit=setTimeout(()=>{if(FG)FG.zoomToFit(420,72)},GPERF.large?650:950);
 }
}
function graphSet(key,value){
 window.GSET[key]=Number(value);
 const rd=document.querySelector('[data-graph-val="'+key+'"]');
 if(rd)rd.textContent=key==='linkw'?Number(value).toFixed(1):(key==='size'?String(+Number(value).toFixed(1)):String(Math.round(value)));
 graphUpdateEditedBadge();
 if(!FG)return;
 const layout=key==='repel'||key==='link'||key==='gravity'||key==='size';
 if(key==='size')graphRefreshNodeMetrics();
 if(key==='link'&&GACTIVE_DATA)graphRefreshComponentCenters(GACTIVE_DATA.nodes);
 if(layout)graphApplyForces();
 if(key==='linkw'){FG.linkWidth(FG.linkWidth());FG.linkColor(FG.linkColor())}else graphRedraw();
 if(layout&&!prefersReducedMotion()){graphSetSimulationStatus('Updating layout',true);FG.d3ReheatSimulation()}
}
function graphApplyPreset(name){
 const preset=GRAPH_PRESETS[name]||GRAPH_PRESETS.compact;
 window.GSET.mode=GRAPH_PRESETS[name]?name:'compact';
 if(name!=='custom'){
  ['repel','link','gravity','font','size','linkw','labelDensity'].forEach(key=>{
   window.GSET[key]=preset[key];
   const control=document.querySelector('[data-graph-setting="'+key+'"]');
   if(control)control.value=preset[key]*(Number(control.dataset.graphScale)||1);
  });
 }
 const notes={
  original:'Original force graph · stronger repulsion and weak centering reproduce the previous, wider spacing.',
  compact:'Compact clusters · shorter links, gentler repulsion and stronger centering keep connected memories together.',
  communities:'Community islands · detected communities have their own gravity centers, while sparse bridges remain visible.',
  radial:'Radial orbit · high-degree entities settle near the center while lower-degree entities form outer rings.',
  constellation:'Constellation flow · curved directional relationships for smaller graphs.',
  custom:'Custom tuning · the appearance and physics controls below define this view.'
 };
 const help=document.getElementById('graph-preset-help'),largeNote=GPERF.large?' Expensive animation, curves, and arrows stay off for this large graph.':'';
 if(help)help.textContent=notes[window.GSET.mode]+largeNote;
 graphRefreshNodeMetrics();graphSyncReadouts();graphUpdateEditedBadge();
 if(FG)graphRender(true,true);
}
function graphSyncReadouts(){
 ['repel','link','gravity','font','size','linkw','labelDensity'].forEach(k=>{const rd=document.querySelector('[data-graph-val="'+k+'"]');if(!rd)return;const v=window.GSET[k];rd.textContent=k==='linkw'?Number(v).toFixed(1):(k==='size'?String(+Number(v).toFixed(1)):String(Math.round(v)));const ctl=document.querySelector('[data-graph-setting="'+k+'"]');if(ctl)ctl.value=v*(Number(ctl.dataset.graphScale)||1);});
 graphSyncPresetCards();graphSyncColorSeg();graphSyncStyleSeg();
 const sk=(typeof GSTYLE!=='undefined'?GSTYLE:'');if(window._gxThumbStyle!==sk){window._gxThumbStyle=sk;graphDrawPresetThumbs();}
}
function graphSyncPresetCards(){const mode=window.GSET.mode;document.querySelectorAll('[data-preset-card]').forEach(b=>b.classList.toggle('active',b.dataset.presetCard===mode));const s=document.getElementById('graph-preset');if(s&&GRAPH_PRESETS[mode]&&s.value!==mode)s.value=mode;}
function graphSyncColorSeg(){const m=(typeof GCOLORBY!=='undefined'?GCOLORBY:'community');document.querySelectorAll('[data-colorby]').forEach(b=>b.classList.toggle('active',b.dataset.colorby===m));}
function graphSyncStyleSeg(){const m=(typeof GSTYLE!=='undefined'?GSTYLE:'cyber');document.querySelectorAll('[data-style-seg]').forEach(b=>b.classList.toggle('active',b.dataset.styleSeg===m));}
function graphDrawPresetThumbs(){
 const M={compact:{p:[[.44,.42],[.58,.44],[.5,.58],[.46,.51],[.62,.55],[.38,.55]],l:[[0,3],[3,2],[3,4],[3,5],[0,1],[1,4]]},original:{p:[[.18,.32],[.5,.2],[.82,.34],[.28,.72],[.72,.74],[.5,.5]],l:[[0,5],[1,5],[2,5],[3,5],[4,5]]},communities:{p:[[.2,.34],[.3,.52],[.54,.26],[.64,.42],[.72,.72],[.82,.6]],l:[[0,1],[2,3],[4,5]]},radial:{p:[[.5,.5],[.5,.16],[.8,.4],[.68,.82],[.32,.82],[.2,.4]],l:[[0,1],[0,2],[0,3],[0,4],[0,5]]}};
 const pal=(typeof COMMUNITY_PALS!=='undefined'&&COMMUNITY_PALS[typeof GSTYLE!=='undefined'?GSTYLE:'cyber'])||['#8c83e8','#5aafb3','#58b882','#6f9fd8','#d7a84b','#df7478'];
 document.querySelectorAll('canvas.gx-thumb').forEach(cv=>{const m=M[cv.dataset.preset];if(!m)return;const ctx=cv.getContext('2d'),W=cv.width,H=cv.height,pad=7;const X=x=>pad+x*(W-2*pad),Y=y=>pad+y*(H-2*pad);ctx.clearRect(0,0,W,H);ctx.strokeStyle='rgba(150,152,168,.45)';ctx.lineWidth=1;m.l.forEach(e=>{ctx.beginPath();ctx.moveTo(X(m.p[e[0]][0]),Y(m.p[e[0]][1]));ctx.lineTo(X(m.p[e[1]][0]),Y(m.p[e[1]][1]));ctx.stroke();});m.p.forEach((p,i)=>{ctx.beginPath();ctx.arc(X(p[0]),Y(p[1]),2.6,0,6.2832);ctx.fillStyle=pal[i%pal.length];ctx.fill();});});
}
function graphUpdateEditedBadge(){
 const btn=document.getElementById('graph-reset-preset');if(!btn)return;
 const base=GRAPH_PRESETS[window.GSET.mode];let edited=false;
 if(base&&window.GSET.mode!=='custom')edited=['repel','link','gravity','font','size','linkw','labelDensity'].some(k=>Math.abs((window.GSET[k]||0)-(base[k]||0))>1e-6);
 showAs(btn,edited);
}
function graphResetPreset(){graphApplyPreset(window.GSET.mode==='custom'?'compact':window.GSET.mode);toast('Preset restored','ok')}
function graphToggleFlow(control){window.GSET.flow=control.checked;if(FG)graphRender(false,false)}
function graphToggleFreeze(control){
 window.GSET.frozen=control.checked;if(!FG)return;
 const ns=(FG.graphData().nodes)||[];
 if(control.checked){ns.forEach(n=>{n.fx=n.x;n.fy=n.y});graphSetSimulationStatus('Layout frozen')}
 else{ns.forEach(n=>{n.fx=null;n.fy=null});if(!prefersReducedMotion())FG.d3ReheatSimulation()}
}
function graphToggleLabels(control){window.GSET.labels=control.checked;if(FG)graphRender(false,false)}
function graphRecolor(){
 renderGraphColorControls();
 if(!FG)return;
 window.GCOL=graphReadThemeColors();graphRefreshNodeColors();
 FG.linkColor(FG.linkColor());FG.linkWidth(FG.linkWidth());graphRedraw();
}
function graphFit(){if(FG)FG.zoomToFit(prefersReducedMotion()?0:500,72)}
function graphReheat(){
 if(!FG)return;
 if(prefersReducedMotion()){toast('Layout motion is off because reduced motion is enabled.','ok');return}
 graphSetSimulationStatus('Reheating layout',true);FG.d3ReheatSimulation();
}
function graphFocus(name){
 clearTimeout(window.__gfit);
 const node=FG&&(FG.graphData().nodes||[]).find(item=>item.id===name),duration=prefersReducedMotion()?0:550;
 if(node&&node.x!=null){graphSetHighlight(name);FG.centerAt(node.x,node.y,duration);FG.zoom(5,duration);syncGraphExplorerSelection(name)}
 else{const show=document.getElementById('graph-show-iso');if(show&&!show.checked){show.checked=true;graphRender(false,true);setTimeout(()=>graphFocus(name),duration?500:0)}else toast('Entity not in view','err')}
}
function graphSearch(){
 const query=document.getElementById('graph-search').value.trim().toLowerCase();if(!query||!GRAPH)return;
 const match=GRAPH.nodes.find(node=>(node.label||node.id||'').toLowerCase().includes(query));
 if(match){graphFocus(match.id);const explorer=document.getElementById('graph-explorer-search');if(explorer){explorer.value=query;renderGraphExplorer(query,true)}}
 else toast('No entity matches','err');
}
function closeEntityMems(){document.getElementById('mm-overlay').classList.remove('show')}
async function graphNodeClick(name){const ov=document.getElementById('mm-overlay');ov.classList.add('show');document.getElementById('mm-title').textContent=name;document.getElementById('mm-meta').innerHTML='<span class="pill pill-accent">entity</span>';document.getElementById('mm-body').innerHTML='<div class="spinner" data-csp-style="s129"></div>';document.getElementById('mm-actions').innerHTML='';try{const d=await api('/memories?q='+encodeURIComponent(name)+'&workspace='+encodeURIComponent(WS||'')+'&limit=12');document.getElementById('mm-body').innerHTML=d.memories.length?('<div data-csp-style="s189">Memories mentioning this entity</div>'+d.memories.map(m=>`<div data-memory-id="${esc(m.id)}" data-csp-style="s190" data-onclick="h140"><div data-csp-style="s191">${esc(m.title||m.id)}</div><div data-csp-style="s90">${esc((m.content||'').slice(0,220))}</div></div>`).join('')):'<div class="empty" data-csp-style="s147">No memories mention this entity by name.</div>'}catch(e){document.getElementById('mm-body').innerHTML='<div class="empty">'+esc(e.message)+'</div>'}}
let GKEYINDEX=-1;
let GNODEBYID=new Map(), GGRAPHNAMES=new Map(), GKEYNODES=[];
const GRAPH_EXPLORER_PAGE={nodes:80,edges:100};
let GEXPLORER={graph:null,query:'',nodeLimit:GRAPH_EXPLORER_PAGE.nodes,edgeLimit:GRAPH_EXPLORER_PAGE.edges}, GEXPLORER_TIMER=0;
function renderGraphSide(){
 const graph=GRAPH;if(!graph)return;
 const types=graph.types||[],legend=document.getElementById('graph-legend');
 graphRenderLegend(graph);
 GNODEBYID=new Map((graph.nodes||[]).map(node=>[node.id,node]));GGRAPHNAMES=new Map((graph.nodes||[]).map(node=>[node.id,node.label||node.id]));GKEYNODES=(graph.nodes||[]).slice().sort((a,b)=>(b.degree||0)-(a.degree||0));
 const top=(graph.top||[]).slice(0,8),maxDegree=Math.max(...top.map(item=>item.degree),1),topBox=document.getElementById('graph-top');
 topBox.innerHTML=top.length?top.map((item,index)=>{const type=(GNODEBYID.get(item.id)||{}).etype;return `<div class="gtop-row" title="${esc(item.name)} — ${item.degree} connection${item.degree===1?'':'s'}${type?' · '+esc(graphTypeLabel(type)):''}. Click to focus in the graph." data-onclick="h141" data-entity="${esc(item.id)}"><span class="gtop-rank">${index+1}</span><span class="gtop-dot" data-graph-node-type="${esc(type||'person_or_concept')}"></span><span class="gtop-name">${esc(item.name)}</span><progress class="graph-degree" data-graph-node-type="${esc(type||'person_or_concept')}" max="${maxDegree}" value="${Math.max(Number(item.degree)||0,0)}" aria-label="${item.degree} connections"></progress><span class="gtop-n">${item.degree}</span></div>`}).join(''):'<div class="empty" data-csp-style="s12">No connections</div>';
 const topCount=document.getElementById('graph-top-count');if(topCount)topCount.textContent=top.length===((graph.top||[]).length)?String(top.length):(top.length+' of '+(graph.top||[]).length);
 const stats=graph.stats||{},statRow=(label,value,tone)=>`<div class="graph-stat-row"><span>${label}</span><b class="graph-tone-${tone}">${Number(value||0).toLocaleString()}</b></div>`;
 document.getElementById('graph-stats').innerHTML=statRow('Entities',stats.entities,'blue')+statRow('Relations',stats.edges,'cyan')+statRow('Connected',stats.connected,'green')+statRow('Isolated',stats.isolated,'dim');
 renderGraphColorControls();graphUpdateColorSwatches();
 renderGraphExplorer(document.getElementById('graph-explorer-search').value);
}
function graphExploreEntity(id){const node=GNODEBYID.get(id);if(!node)return;graphFocus(node.id);graphNodeClick(node.label||node.id)}
function graphKeyboard(event){
 if(!GRAPH||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Enter'].includes(event.key))return;
 const nodes=GACTIVE_DATA&&GACTIVE_DATA.nodes.length?GACTIVE_DATA.nodes:GKEYNODES;if(!nodes.length)return;
 event.preventDefault();
 if(event.key==='Enter'){const node=nodes[Math.max(0,GKEYINDEX)];graphExploreEntity(node.id);return}
 const step=(event.key==='ArrowLeft'||event.key==='ArrowUp')?-1:1;GKEYINDEX=(GKEYINDEX+step+nodes.length)%nodes.length;
 const node=nodes[GKEYINDEX],net=document.getElementById('graph-net');graphFocus(node.id);net.setAttribute('aria-label','Selected entity '+(node.label||node.id)+', '+(node.degree||0)+' relations. Press Enter to open. Use arrow keys to move.');
}
function syncGraphExplorerSelection(id){document.querySelectorAll('#graph-entity-list [data-entity]').forEach(button=>{const active=button.dataset.entity===id;button.classList.toggle('active',active);if(active)button.setAttribute('aria-current','true');else button.removeAttribute('aria-current')})}
function graphQueueExplorer(query){clearTimeout(GEXPLORER_TIMER);GEXPLORER_TIMER=setTimeout(()=>renderGraphExplorer(query,true),120)}
function graphExplorerMore(kind){
 if(kind==='nodes')GEXPLORER.nodeLimit+=GRAPH_EXPLORER_PAGE.nodes;else GEXPLORER.edgeLimit+=GRAPH_EXPLORER_PAGE.edges;
 renderGraphExplorer(GEXPLORER.query,false);
}
function renderGraphExplorer(query,reset=false){
 const nodesBox=document.getElementById('graph-entity-list'),edgesBox=document.getElementById('graph-relation-list');if(!nodesBox||!edgesBox)return;
 if(!GRAPH){nodesBox.innerHTML='<div class="empty" data-csp-style="s67">Graph data is loading.</div>';edgesBox.innerHTML='<div class="empty" data-csp-style="s67">Graph data is loading.</div>';return}
 const normalized=(query||'').trim().toLowerCase();
 if(reset||GEXPLORER.graph!==GRAPH||GEXPLORER.query!==normalized){GEXPLORER={graph:GRAPH,query:normalized,nodeLimit:GRAPH_EXPLORER_PAGE.nodes,edgeLimit:GRAPH_EXPLORER_PAGE.edges}}
 const nodes=GKEYNODES,edges=GRAPH.edges||[],shownNodes=normalized?nodes.filter(node=>((node.label||node.id||'')+' '+(node.etype||'')).toLowerCase().includes(normalized)):nodes,shownEdges=normalized?edges.filter(edge=>((GGRAPHNAMES.get(edge.from)||edge.from||'')+' '+(edge.label||'')+' '+(GGRAPHNAMES.get(edge.to)||edge.to||'')+' '+(edge.layer||'')).toLowerCase().includes(normalized)):edges;
 const nodePage=shownNodes.slice(0,GEXPLORER.nodeLimit),edgePage=shownEdges.slice(0,GEXPLORER.edgeLimit);
 document.getElementById('graph-explorer-node-count').textContent=nodePage.length+' of '+shownNodes.length;
 document.getElementById('graph-explorer-edge-count').textContent=edgePage.length+' of '+shownEdges.length;
 nodesBox.innerHTML=nodePage.length?nodePage.map(node=>`<button type="button" class="graph-explorer-item" role="listitem" data-entity="${esc(node.id)}" data-onclick="h142"><span class="gtype-dot" data-graph-node-type="${esc(node.etype||'person_or_concept')}" aria-hidden="true"></span><span>${esc(node.label||node.id)}</span><span class="graph-explorer-meta">${esc(graphTypeLabel(node.etype))} · ${node.degree||0} relation${node.degree===1?'':'s'}</span></button>`).join(''):'<div class="empty" data-csp-style="s67">No entities match this filter.</div>';
 if(nodePage.length<shownNodes.length)nodesBox.insertAdjacentHTML('beforeend',`<button type="button" class="graph-explorer-more" data-onclick="h143">Show ${Math.min(GRAPH_EXPLORER_PAGE.nodes,shownNodes.length-nodePage.length)} more entities</button>`);
 edgesBox.innerHTML=edgePage.length?edgePage.map(edge=>`<div class="graph-explorer-item graph-relation-row" role="listitem"><button type="button" class="graph-entity-link" data-entity="${esc(edge.from)}" data-onclick="h142">${esc(GGRAPHNAMES.get(edge.from)||edge.from)}</button><span class="graph-relation-label">${esc(edge.label||edge.layer||'related to')}</span><button type="button" class="graph-entity-link" data-entity="${esc(edge.to)}" data-onclick="h142">${esc(GGRAPHNAMES.get(edge.to)||edge.to)}</button><span class="graph-explorer-meta">${esc(edge.layer||'semantic')}</span></div>`).join(''):'<div class="empty" data-csp-style="s67">No relations match this filter.</div>';
 if(edgePage.length<shownEdges.length)edgesBox.insertAdjacentHTML('beforeend',`<button type="button" class="graph-explorer-more" data-onclick="h144">Show ${Math.min(GRAPH_EXPLORER_PAGE.edges,shownEdges.length-edgePage.length)} more relations</button>`);
 syncGraphExplorerSelection(GHILITE);
}
/* Search and accessible-table extensions for the shipped ForceGraph + D3 explorer. */
function loadAnalyticsView(){
 return loadAnalytics();
}
function loadAutomationView(){
 return loadAutomation();
}
function workspaceRequired(id,purpose){
 const el=document.getElementById(id);
 if(!el)return;
 el.dataset.workspaceRequired='true';
 el.setAttribute('aria-busy','false');
 el.innerHTML=`<div class="empty" data-csp-style="s67"><div data-csp-style="s196">Create a workspace first</div><div>Engraphis needs an active workspace to ${esc(purpose)}.</div><button class="btn btn-primary btn-sm" data-onclick="h3">Open Workspaces</button></div>`;
}
function clearWorkspaceRequired(id,message){
 const el=document.getElementById(id);
 if(!el||el.dataset.workspaceRequired!=='true')return;
 delete el.dataset.workspaceRequired;
 el.innerHTML=`<div class="empty" data-csp-style="s12">${esc(message)}</div>`;
}
function setWorkspaceControls(viewId,enabled){
 const view=document.getElementById(viewId);
 if(!view)return;
 view.querySelectorAll('button,input,select,textarea').forEach(control=>{
  if(control.dataset.onclick==='h3')return;
  if(enabled){
   if(Object.prototype.hasOwnProperty.call(control.dataset,'workspaceTitle')){
    control.title=control.dataset.workspaceTitle;
    delete control.dataset.workspaceTitle;
   }
   control.disabled=false;
  }else{
   if(!Object.prototype.hasOwnProperty.call(control.dataset,'workspaceTitle'))control.dataset.workspaceTitle=control.title||'';
   control.title='Create a workspace first';
   control.disabled=true;
  }
 });
}
function loadWorkspaceInputView(viewId,target,purpose,initial){
 const enabled=!!WS;
 setWorkspaceControls(viewId,enabled);
 if(!enabled)return workspaceRequired(target,purpose);
 clearWorkspaceRequired(target,initial);
}
function loadProactiveView(){
 if(!WS)return workspaceRequired('proactive-body','surface relevant memories');
 clearWorkspaceRequired('proactive-body','Loading relevant memories…');
 return loadProactive();
}
function loadAuditView(){
 setWorkspaceControls('view-audit',!!WS);
 if(!WS)return workspaceRequired('audit-body','show governance history and receipts');
 clearWorkspaceRequired('audit-body','Loading governance history…');
 return loadAudit();
}
function loadLegacyGraphWorkspaceView(){
 setWorkspaceControls('view-graph',!!WS);
 if(WS){clearWorkspaceRequired('graph-empty','Loading graph...');return loadLegacyGraph()}
 GRAPH=null;const net=document.getElementById('graph-net');if(net)net.setAttribute('aria-busy','false');workspaceRequired('graph-empty','explore the knowledge graph');const entities=document.getElementById('graph-entity-list'),relations=document.getElementById('graph-relation-list');if(entities)entities.innerHTML='<div class="empty" data-csp-style="s67">No entities until a workspace exists.</div>';if(relations)relations.innerHTML='<div class="empty" data-csp-style="s67">No relations until a workspace exists.</div>';
}
function loadGraphWorkspaceView(){return loadLegacyGraphWorkspaceView()}
function loadConsolidateView(){
 setWorkspaceControls('view-consolidate',!!WS);
 if(!WS)return workspaceRequired('consolidate-body','preview or commit consolidation');
 clearWorkspaceRequired('consolidate-body','Run a dry preview before committing consolidation.');
}
const LOADERS={
 overview:loadOverview,
 recall:function(){loadWorkspaceInputView('view-recall','recall-results','recall memories','Enter a query to recall memories.')},
 memories:loadMemories,
 proactive:loadProactiveView,
 why:function(){loadWorkspaceInputView('view-why','why-body','explain current beliefs and their history','Ask a question to see the current answer and its history.')},
 timeline:function(){loadWorkspaceInputView('view-timeline','tl-body','trace memory history','Enter a topic to trace its history.')},
 audit:loadAuditView,
 graph:loadGraphWorkspaceView,
 analytics:loadAnalyticsView,
 consolidate:loadConsolidateView,
 automation:loadAutomationView,
 workspaces:loadWorkspaces,
 team:loadTeam,
 settings:loadSettings
};

function renderSemBanner(eb){
 var sb=document.getElementById('sem-banner');
 if(!sb)return;
 window.__emb=eb;
 if(!eb||eb.semantic){showAs(sb,false);return}
 showAs(sb,true,'block');
 var reason=eb.error?('<div class="system-notice-reason">Why the model did not load: '+esc(eb.error)+'</div>'):'';
 sb.innerHTML='<div class="system-notice"><details><summary><strong>Semantic search is off</strong><span class="system-notice-brief">Keyword fallback is active for Recall, Why and Timeline.</span></summary><div class="system-notice-detail">The embedder loaded at '+(eb.dim||'?')+'-dim but your memories are 384-dim. To enable meaning-based search, close the dashboard window and re-launch <code>scripts/launch_dashboard.ps1</code> (Windows) or <code>python -m scripts.start_server</code> &mdash; it installs the model automatically (one-time), then hard-refresh this page.'+reason+'</div></details><button class="btn btn-ghost btn-sm" data-onclick="h145">Recheck</button></div>';
}
/* Update reminder banner. Fed by /bootstrap's `update` snapshot (fail-silent server side).
   Dismissing hides it until a newer version than the dismissed one ships. Handlers are
   wired with addEventListener (no inline attributes) to satisfy the strict dashboard CSP. */
function renderUpdateBanner(u){
 const el=document.getElementById('update-banner');
 if(!el)return;
 if(!u||!u.enabled||!u.update_available||!u.latest){el.hidden=true;el.textContent='';return}
 let dismissed='';try{dismissed=localStorage.getItem('engraphis-update-dismissed')||''}catch(e){}
 if(dismissed===u.latest){el.hidden=true;el.textContent='';return}
 const link=safeUrl(u.url||'https://github.com/Coding-Dev-Tools/engraphis/releases');
 el.innerHTML='<div class="ub-text"><strong>Update available</strong> — Engraphis '+esc(u.latest)+' is out (you have '+esc(u.current||'')+'). Upgrade with <code>pip install -U engraphis</code>.</div><div class="ub-actions"><a class="btn btn-ghost btn-sm" href="'+esc(link)+'" target="_blank" rel="noopener">View release →</a><button type="button" class="ub-dismiss" aria-label="Dismiss update notice" title="Dismiss">×</button></div>';
 el.hidden=false;
 const btn=el.querySelector('.ub-dismiss');
 if(btn)btn.addEventListener('click',function(){try{localStorage.setItem('engraphis-update-dismissed',u.latest)}catch(e){}el.hidden=true;el.textContent=''});
}
async function boot(){try{const b=await api('/bootstrap');LIC=b.license;renderSemBanner(b.embedder);renderUpdateBanner(b.update);WORKSPACES=b.workspaces||[];if(!WS&&WORKSPACES.length){WORKSPACES.sort((a,b)=>(b.memories||0)-(a.memories||0));setWS(WORKSPACES[0].name)}updateLicBadge();updateFeatureLocks();loadOverview();checkHealth()}catch(e){const msg=e.status===401?'Local API token required. Configure this installation before opening the dashboard.':'Boot failed: '+e.message;document.getElementById('stat-grid').innerHTML='<div class="empty" data-csp-style="s10">'+esc(msg)+'</div>';document.getElementById('ov-types').innerHTML='<div class="empty" data-csp-style="s67">Dashboard data could not be loaded.</div>';document.getElementById('ov-analytics').innerHTML='<div class="empty" data-csp-style="s10">Dashboard data could not be loaded.</div>';toast(msg,'err')}}
initTheme();
boot();
setInterval(checkHealth,30000);
document.addEventListener('keydown',event=>{
 if(event.key!=='Escape')return;
 const action=document.getElementById('action-overlay');
 if(action&&action.classList.contains('show')){closeActionDialog(null);return}
 const memories=document.getElementById('mm-overlay');
 if(memories&&memories.classList.contains('show')){closeEntityMems();return}
 const theme=document.getElementById('theme-menu');
 if(theme&&theme.classList.contains('is-open')){closeThemeMenu();document.getElementById('theme-btn').focus();return}
 if(document.querySelector('.app').classList.contains('mobile-nav-open')){closeMobileNav();document.getElementById('mobile-nav-toggle').focus();return}
 if(document.getElementById('view-mem-editor').classList.contains('active'))closeMem();
});

/* Generated listener registry replacing CSP-blocked inline event attributes. */
const CSP_EVENT_HANDLERS=Object.freeze({
h1:function(event){navTo('settings')},
h2:function(event){toggleThemeMenu(event)},
h3:function(event){navTo('workspaces')},
h4:function(event){closeMobileNav(true)},
h5:function(event){toggleMobileNav()},
h7:function(event){if(event.key==='Enter')doRecall()},
h8:function(event){doRecall()},
h9:function(event){if(event.key==='Enter')loadMemories()},
h10:function(event){loadMemories()},
h11:function(event){closeMem()},
h12:function(event){edTogglePin()},
h13:function(event){edSave()},
h14:function(event){edForget()},
h15:function(event){edPreviewUpdate()},
h16:function(event){if(event.key==='Enter')doWhy()},
h17:function(event){doWhy()},
h18:function(event){if(event.key==='Enter')doTimeline()},
h19:function(event){doTimeline()},
h20:function(event){loadAudit()},
h21:function(event){loadReceipts()},
h22:function(event){downloadReceipts()},
h23:function(event){graphKeyboard(event)},
h24:function(event){loadGraph()},
h25:function(event){if(event.key==='Enter')graphSearch()},
h26:function(event){graphFit()},
h27:function(event){graphReheat()},
h28:function(event){loadGraph()},
h29:function(event){graphApplyPreset('compact');graphSyncPresetCards()},
h30:function(event){graphApplyPreset('original');graphSyncPresetCards()},
h31:function(event){graphApplyPreset('communities');graphSyncPresetCards()},
h32:function(event){graphApplyPreset('radial');graphSyncPresetCards()},
h33:function(event){graphResetPreset()},
h34:function(event){graphSet('repel',this.value)},
h35:function(event){graphSet('link',this.value)},
h36:function(event){graphSet('gravity',this.value)},
h37:function(event){graphSet('size',this.value)},
h38:function(event){graphSet('font',this.value)},
h39:function(event){graphSet('linkw',this.value/10)},
h40:function(event){graphSet('labelDensity',this.value)},
h41:function(event){graphSetColorBy('community');graphSyncColorSeg()},
h42:function(event){graphSetColorBy('connections');graphSyncColorSeg()},
h43:function(event){graphSetColorBy('type');graphSyncColorSeg()},
h44:function(event){graphSetStyle('cyber');graphSyncStyleSeg();graphDrawPresetThumbs()},
h45:function(event){graphSetStyle('galaxy');graphSyncStyleSeg();graphDrawPresetThumbs()},
h46:function(event){graphSetStyle('solar');graphSyncStyleSeg();graphDrawPresetThumbs()},
h47:function(event){graphSetStyle('classic');graphSyncStyleSeg();graphDrawPresetThumbs()},
h48:function(event){graphToggleLabels(this)},
h49:function(event){graphRender()},
h50:function(event){graphToggleFlow(this)},
h51:function(event){graphToggleFreeze(this)},
h52:function(event){if(event.key==='Enter')loadGraph()},
h53:function(event){graphApplyPreset(this.value)},
h54:function(event){graphSetStyle(this.value)},
h55:function(event){graphSetColorBy(this.value)},
h56:function(event){graphApplyPalette(this.value)},
h57:function(event){graphQueueExplorer(this.value)},
h58:function(event){loadAnalytics()},
h61:function(event){runConsolidate(true)},
h62:function(event){runConsolidate(false)},
h63:function(event){wsCreate()},
h64:function(event){event.preventDefault();this.classList.add('drag')},
h65:function(event){this.classList.remove('drag')},
h66:function(event){importDrop(event)},
h67:function(event){document.getElementById('import-file-input').click()},
h68:function(event){document.getElementById('import-dir-input').click()},
h69:function(event){importFilesPicked(this.files,this)},
h70:function(event){if(event.key==='Enter')importFromPath()},
h71:function(event){importFromPath()},
h72:function(event){indexRepository()},
h73:function(event){importPostgresSchema()},
h74:function(event){pickTheme(this.value)},
h75:function(event){if(event.target===this)closeEntityMems()},
h76:function(event){closeEntityMems()},
h83:function(event){pickTheme(this.dataset.themeValue)},
h84:function(event){startTrial()},
h85:function(event){navTo('analytics')},
h86:function(event){navTo('analytics')},
h87:function(event){startTeamTrial()},
h88:function(event){saveAutomation()},
h90:function(event){runMaintenance(false)},
h91:function(event){openMem(this.dataset.id)},
h92:function(event){memDragStart(event,this.dataset.id)},
h93:function(event){memDragOver(event,this.dataset.id)},
h94:function(event){memDragLeave(event)},
h95:function(event){memDrop(event,this.dataset.id)},
h96:function(event){memDragEnd(event)},
h97:function(event){openMem(this.closest('.mem-card').dataset.id)},
h99:function(event){event.stopPropagation();wsRename(folderCardName(this))},
h100:function(event){event.stopPropagation();wsDescribe(folderCardName(this))},
h101:function(event){event.stopPropagation();wsMerge(folderCardName(this))},
h102:function(event){event.stopPropagation();wsCopy(folderCardName(this))},
h103:function(event){event.stopPropagation();wsDelete(folderCardName(this),Number(this.closest('.vault-card').dataset.memories))},
h104:function(event){folderOpen(this)},
h105:function(event){event.stopPropagation();event.preventDefault();tfMemories(folderCardName(this))},
h128:function(event){updateLlmSnippet()},
h129:function(event){onLlmProvChange()},
h130:function(event){copyLlmSnippet()},
h131:function(event){testLlm()},
h150:function(event){setLlmExtractor(true)},
h151:function(event){setLlmExtractor(false)},
h136:function(event){syncNow()},
h138:function(event){graphSetTypeColor(this.dataset.nodeType,this.value,false)},
h139:function(event){graphSetTypeColor(this.dataset.nodeType,this.value,true)},
h140:function(event){closeEntityMems();openMem(this.dataset.memoryId)},
h141:function(event){graphFocus(this.dataset.entity)},
h142:function(event){graphExploreEntity(this.dataset.entity)},
h143:function(event){graphExplorerMore('nodes')},
h144:function(event){graphExplorerMore('edges')},
h145:function(event){boot()},
});
for(const type of ['click','keydown','input','change','dragover','dragleave','drop','dragstart','dragend']){document.addEventListener(type,function(event){const target=event.target instanceof Element?event.target.closest('[data-on'+type+']'):null;if(!target||!document.documentElement.contains(target))return;const handler=CSP_EVENT_HANDLERS[target.getAttribute('data-on'+type)];if(!handler)return;const result=handler.call(target,event);if(result===false){event.preventDefault();event.stopPropagation()}},false)}
