const API=location.origin+'/api';
let WS=null, WORKSPACES=[], LIC=null, TRIAL_DAYS=3, AN_PORTFOLIO=false, TRIAL_POLLING=false;
let AUTH_SKIPPED=false, AUTH_MODE='login', RESET_TOKEN=null, INVITE_TOKEN=null, TEAM_ENABLED=false, HOSTED_BOOTSTRAP=false;
const TITLES={overview:'Overview',recall:'Recall',memories:'Memories','mem-editor':'Memory',proactive:'Proactive recall',why:'Why',timeline:'Timeline',audit:'Audit trail',graph:'Knowledge Graph',analytics:'Analytics',consolidate:'Consolidate',automation:'Automated maintenance',workspaces:'Workspaces',team:'Team',settings:'Settings'};
const ROUTE_SECTIONS={overview:'Operate',recall:'Operate',memories:'Operate','mem-editor':'Operate',proactive:'Operate',why:'History',timeline:'History',audit:'History',graph:'Relations',analytics:'Relations',consolidate:'Engine',automation:'Engine',workspaces:'Operate',team:'Engine',settings:'Engine'};
/* Per-view subtitle rendered in the topbar next to the view name. The body no longer
   repeats the view title/description — the topbar is the single source for both. */
const DESCS={overview:'',recall:'Hybrid semantic + retention search over this workspace.',memories:'Browse and curate the memories in this workspace.','mem-editor':'',proactive:'What matters right now: importance × recency × retention, plus the last session handoff.',why:'The current answer to a question, with the facts it superseded.',timeline:'Bi-temporal history: what was believed, when it was valid, and when it was recorded.',audit:'Local governance history or content-free, tamper-evident receipts for sharing.',graph:'Explore entities and their sourced relationships from this workspace’s memories.',analytics:'Growth, retention, decay forecast and entity insights for this workspace.',consolidate:'Preview or commit a sweep that distils episodic memories into semantic facts and archives decayed transients.',automation:'Schedule consolidation with explicit retention thresholds for the workspaces you can manage.',workspaces:'Hard isolation boundaries. The active workspace receives new memories, imports, searches, and graph operations.',team:'Role-scoped multi-user access, seats, folders, and governance.',settings:'Engine connection, optional services, licensing, appearance, and agent access.'};
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
function fmtRel(ts){if(!ts)return '';const s=Math.max(0,Date.now()/1000-ts);if(s<60)return 'just now';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';if(s<2592000)return Math.floor(s/86400)+'d ago';return new Date(ts*1000).toISOString().slice(0,10)}
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
 if(!(LIC&&(LIC.features||[]).includes('analytics'))){
  lock.textContent='PRO';
  lock.className='pill pill-muted';
  const used=LIC&&LIC.trial&&LIC.trial.used;
  el.innerHTML='<div data-csp-style="s68"><div data-csp-style="s69">Growth, retention distribution, and decay forecast.</div>'+(used?'':'<button class="btn btn-primary btn-sm" data-onclick="h84">Start '+TRIAL_DAYS+'-day free trial</button> ')+'<button class="btn btn-ghost btn-sm" data-onclick="h85">Plan details</button></div>';
  return;
 }
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
  el.innerHTML='<div class="empty" data-csp-style="s10">'+esc(e.message)+'</div>';
 }
}

/* ── shared upgrade / trial CTA ── */
function unlockHtml(feature,plan){const url=safeUrl((LIC&&(plan==='team'?LIC.team_upgrade_url:LIC.pro_upgrade_url))||(LIC&&LIC.upgrade_url));const used=LIC&&LIC.trial&&LIC.trial.used;const trialBtn=plan==='team'?`<button class="btn btn-primary btn-sm" data-onclick="h87">Start ${TRIAL_DAYS}-day free Team trial</button>`:(used?'':`<button class="btn btn-primary btn-sm" data-onclick="h84">Start ${TRIAL_DAYS}-day free trial</button>`);return `<div class="empty" data-csp-style="s74"><div data-csp-style="s75">🔒</div><div data-csp-style="s76">${esc(feature)} is ${plan==='team'?'a Team':'a Pro'} feature</div><div data-csp-style="s77">${plan==='team'?'Try every Team feature free for '+TRIAL_DAYS+' days — confirm by email, no card, then invite your team.':(used?'Your free trial has been used — upgrade to unlock this again.':'Try every Pro feature free for '+TRIAL_DAYS+' days — confirm by email, no card.')}</div><div data-csp-style="s78">${trialBtn}<a class="btn btn-ghost btn-sm" href="${esc(url)}" target="_blank" rel="noopener">${plan==='team'?'Get Team — $20/seat/mo (or $200/seat/yr)':'Upgrade to Pro — $10/mo (or $100/yr)'}</a></div></div>`}
async function startTrialPlan(plan){const local=connectionContext()==='Local engine';const hostedToken=((document.getElementById('hosted-api-token')||{}).value||'').trim();const fields=[{name:'email',label:'Email address',type:'email',required:true,placeholder:'you@example.com'}];if(!local||HOSTED_BOOTSTRAP)fields.push({name:'deployment_token',label:'Deployment token',type:'password',required:true,value:hostedToken,placeholder:'Secret configured as ENGRAPHIS_DEPLOYMENT_TOKEN'});const message=(local&&!HOSTED_BOOTSTRAP)?('Confirm the email we send to start your '+(plan==='team'?'Team':'Pro')+' trial. No card, and no deployment token is needed on a local engine.'):'Verify ownership of this deployment, then confirm the email we send. No card is required and the signed key is activated automatically.';const result=await actionDialog({title:'Start '+(plan==='team'?'Team':'Pro')+' trial',message,submit:'Send confirmation',fields});if(result===null)return;try{const body={email:result.email.trim(),plan};if(!local||HOSTED_BOOTSTRAP)body.deployment_token=result.deployment_token;const d=await api('/license/trials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('Confirmation queued. You may safely close this tab and return after confirming.','ok');if(d.claim_id){try{localStorage.setItem('engraphis-trial-claim',JSON.stringify({id:d.claim_id,plan}))}catch(e){}pollTrialClaim(d.claim_id,plan,0)}}catch(e){toast('Trial: '+e.message,'err')}}
function startTrial(){return startTrialPlan('pro')}
function startTeamTrial(){return startTrialPlan('team')}
function clearTrialClaim(){TRIAL_POLLING=false;try{localStorage.removeItem('engraphis-trial-claim')}catch(e){}}
function resumeTrialClaim(){if(TRIAL_POLLING)return;try{const pending=JSON.parse(localStorage.getItem('engraphis-trial-claim')||'null');if(pending&&pending.id&&['pro','team'].includes(pending.plan))pollTrialClaim(pending.id,pending.plan,0)}catch(e){clearTrialClaim()}}
async function pollTrialClaim(claimId,plan,attempt){TRIAL_POLLING=true;try{const d=await api('/license/trials/'+encodeURIComponent(claimId));if(d.active){clearTrialClaim();toast((plan==='team'?'Team':'Pro')+' trial activated automatically','ok');LIC=d.license||await api('/license');updateLicBadge();updateFeatureLocks();HOSTED_BOOTSTRAP=false;const st=await api('/auth/state');TEAM_ENABLED=!!st.enabled;if(st.enabled&&!st.user){showAuth(st);return}boot();return}if(d.status==='expired'){clearTrialClaim();toast('The confirmation link expired. Start a new claim to resend it.','err');return}}catch(e){if(attempt>2)toast('Activation check: '+e.message,'err')}if(attempt<600)setTimeout(()=>pollTrialClaim(claimId,plan,attempt+1),3000);else TRIAL_POLLING=false}
function getTrialIntent(){try{const plan=(new URLSearchParams(location.search).get('trial')||'').trim().toLowerCase();return ['pro','team'].includes(plan)?plan:null}catch(e){return null}}
function updateLicBadge(){const bd=document.getElementById('lic-badge');if(!bd||!LIC)return;bd.textContent=LIC.is_trial?'TRIAL':(LIC.plan||'free').toUpperCase();bd.className='pill '+((LIC.plan&&LIC.plan!=='free')?'pill-accent':'pill-muted')}
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
 if(LIC&&LIC.trial&&LIC.trial.trial_days)TRIAL_DAYS=LIC.trial.trial_days;
}

/* ── analytics (Pro) ── */
function barRow(label,val,peak,color){const tone=color==='var(--green)'?' analytics-bar-green':(color==='var(--blue)'?' analytics-bar-blue':(color==='var(--cyan)'?' analytics-bar-cyan':(color==='var(--accent-dim)'?' analytics-bar-dim':'')));return `<div data-csp-style="s79"><div data-csp-style="s80" title="${esc(label)}">${esc(label)}</div><progress class="analytics-bar${tone}" max="${Math.max(Number(peak)||1,1)}" value="${Math.max(Number(val)||0,0)}" aria-label="${esc(label)}: ${Number(val)||0}"></progress><div data-csp-style="s83">${val}</div></div>`}
function statMini(v,l,color){const tone=color==='var(--red)'?' tone-red':(color==='var(--amber)'?' tone-amber':(color==='var(--green)'?' tone-green':''));return `<div class="stat" data-csp-style="s67"><div class="stat-val${tone}">${v}</div><div class="stat-lbl">${esc(l)}</div></div>`}
function renderAnalytics(a,isPortfolio){const t=a.totals||{},f=a.decay_forecast||{};const weeks=a.growth_weekly||[];const gp=Math.max(...weeks,1);const gitems=weeks.map((n,i)=>{const back=weeks.length-1-i;return barRow(back===0?'now':back+'w ago',n,gp,'var(--accent-dim)')}).join('')||'<div class="empty" data-csp-style="s85">No data</div>';const hist=a.retention_histogram||{};const hc=hist.counts||[],hb=hist.buckets||[];const hp=Math.max(...hc,1);const hitems=hb.map((b,i)=>barRow(b,hc[i]||0,hp,'var(--green)')).join('');const mix=a.resolver_mix||{};const mk=Object.keys(mix);const mp=Math.max(...Object.values(mix),1);const mitems=mk.length?mk.map(k=>barRow(k,mix[k],mp,'var(--blue)')).join(''):'<div class="empty" data-csp-style="s85">No resolver events yet.</div>';const bt=a.by_type||{};const btk=Object.keys(bt);const bp=Math.max(...Object.values(bt),1);const btitems=btk.length?btk.map(k=>barRow(k,bt[k],bp,'var(--accent)')).join(''):'<div class="empty" data-csp-style="s85">No memories yet.</div>';const ents=a.top_entities||[];const ep=Math.max(...ents.map(e=>e.n),1);const eitems=ents.length?ents.map(e=>barRow(e.name+(isPortfolio&&e.workspace?' · '+e.workspace:''),e.n,ep,'var(--cyan)')).join(''):'<div class="empty" data-csp-style="s85">No entities yet — they appear as the graph grows.</div>';const avg=Math.round((t.avg_retention||0)*100);let wsTable='';if(isPortfolio&&a.workspaces){wsTable=`<div class="card" data-csp-style="s52"><div class="card-head">Per-workspace breakdown</div><table class="tbl"><thead><tr><th>Workspace</th><th>Live</th><th>Pinned</th><th>Avg ret.</th><th>Fading 7d</th></tr></thead><tbody>${a.workspaces.map(w=>`<tr><td>${esc(w.workspace)}</td><td>${w.live}</td><td>${w.pinned}</td><td>${Math.round((w.avg_retention||0)*100)}%</td><td>${w.at_risk_7d}</td></tr>`).join('')}</tbody></table></div>`}return `<div class="stat-grid">${statMini(t.live!=null?t.live:'—','Live memories')}${statMini(avg+'%','Avg retention',avg<40?'var(--red)':(avg<70?'var(--amber)':'var(--green)'))}${statMini(f.at_risk_7d!=null?f.at_risk_7d:'—','Fading ≤ 7 days',f.at_risk_7d>0?'var(--amber)':'')}${statMini(f.at_risk_30d!=null?f.at_risk_30d:'—','Fading ≤ 30 days')}${statMini(t.pinned!=null?t.pinned:'—','Pinned (protected)')}${isPortfolio?statMini(t.workspaces||0,'Workspaces'):statMini(t.superseded!=null?t.superseded:'—','Superseded (history)')}</div><div class="cols-2" data-csp-style="s52"><div class="card"><div class="card-head">Memories written per week</div>${gitems}</div><div class="card"><div class="card-head">Retention distribution</div>${hitems}</div></div><div class="cols-2" data-csp-style="s52"><div class="card"><div class="card-head">By type</div>${btitems}</div><div class="card"><div class="card-head">Write-path resolver activity</div>${mitems}</div></div><div class="card" data-csp-style="s52"><div class="card-head">Most connected entities</div>${eitems}</div>${wsTable}`}
async function loadAnalytics(){const el=document.getElementById('analytics-body'),lock=document.getElementById('an-lock'),acts=document.getElementById('an-actions');el.innerHTML='<div class="spinner" data-csp-style="s86"></div>';try{const path=AN_PORTFOLIO?'/analytics/portfolio':'/analytics?workspace='+encodeURIComponent(WS||'');const a=await api(path);setPlanPill(lock,(LIC&&LIC.is_trial)?'TRIAL':'PRO','pill pill-accent');showAs(acts,true,'flex');const pb=document.getElementById('an-portfolio-btn');if(pb)pb.textContent=AN_PORTFOLIO?'Single workspace':'Portfolio · all workspaces';el.innerHTML=renderAnalytics(a,AN_PORTFOLIO)}catch(e){if(e.status===402){setPlanPill(lock,'PRO','pill pill-muted');showAs(acts,false);el.innerHTML=unlockHtml('Analytics','pro')}else{el.innerHTML='<div class="empty" data-csp-style="s87">'+esc(e.message)+'</div>'}}}
function togglePortfolio(){AN_PORTFOLIO=!AN_PORTFOLIO;loadAnalytics()}
async function downloadAnalyticsReport(){try{const r=await fetch(API+'/analytics/export?workspace='+encodeURIComponent(WS||''));if(!r.ok){if(r.status===402){toast('Analytics report is a Pro feature — start your free trial','err');return}throw new Error('HTTP '+r.status)}const blob=await r.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='engraphis-analytics-'+(WS||'workspace')+'.html';a.click();URL.revokeObjectURL(a.href);toast('Report downloaded','ok')}catch(e){toast('Report: '+e.message,'err')}}

/* ── automated maintenance (Pro) ── */
async function loadAutomation(){const el=document.getElementById('automation-body'),lock=document.getElementById('au-lock');el.innerHTML='<div class="spinner" data-csp-style="s86"></div>';try{const p=await api('/automation');setPlanPill(lock,(LIC&&LIC.is_trial)?'TRIAL':'PRO','pill pill-accent');const last=p.last_run?fmtRel(p.last_run):'never';el.innerHTML=`<div class="cols-2"><div class="card"><div class="card-head">Maintenance policy</div><label data-csp-style="s88"><input type="checkbox" id="au-enabled" ${p.enabled?'checked':''}> Enable scheduled maintenance</label><div class="field"><label class="field-lbl">Run every (hours)</label><input class="input" id="au-cadence" type="number" min="1" value="${p.cadence_hours||24}"></div><label data-csp-style="s88"><input type="checkbox" id="au-consolidate" ${p.consolidate?'checked':''}> Run consolidation sweep</label><div class="field"><label class="field-lbl">Min cluster size</label><input class="input" id="au-mincluster" type="number" min="2" max="20" value="${p.min_cluster||3}"></div><div class="field"><label class="field-lbl">Archive below retention</label><input class="input" id="au-archive" type="number" step="0.01" min="0" max="0.5" value="${p.archive_below!=null?p.archive_below:0.05}"><div class="field-hint">Memories fading below this (0–0.5) get archived. Pinned memories are always protected.</div></div><label data-csp-style="s88"><input type="checkbox" id="au-dream" ${p.dream?'checked':''}> Auto-dream (background consolidation on idle)</label><div class="field"><label class="field-lbl">Min new memories to trigger a dream</label><input class="input" id="au-dream-min" type="number" min="1" value="${p.dream_min_new||20}"></div><div class="field"><label class="field-lbl">Idle minutes before dreaming</label><input class="input" id="au-dream-idle" type="number" min="0" value="${p.dream_idle_minutes!=null?p.dream_idle_minutes:15}"></div><label data-csp-style="s88"><input type="checkbox" id="au-infer" ${p.infer?'checked':''}> Run inference extraction during maintenance</label><button class="btn btn-primary btn-sm" data-onclick="h88">Save policy</button></div><div class="card"><div class="card-head">Run &amp; schedule</div><div class="cfg-row" data-csp-style="s48"><span>Status</span><span class="pill ${p.enabled?'pill-green':'pill-muted'}" data-csp-style="s9">${p.enabled?'ENABLED':'OFF'}</span></div><div class="cfg-row" data-csp-style="s48"><span>Last run</span><span data-csp-style="s50">${esc(last)}</span></div><div data-csp-style="s89"><button class="btn btn-ghost btn-sm" data-onclick="h89">Preview (dry run)</button><button class="btn btn-primary btn-sm" data-onclick="h90">Run now</button></div><div id="au-result" data-csp-style="s90"></div><div class="field-hint" data-csp-style="s91">To run automatically without the dashboard open, schedule the CLI:<br><code data-csp-style="s92">python -m scripts.auto_maintain --apply</code><br>(e.g. Windows Task Scheduler or cron).</div></div></div>`}catch(e){if(e.status===402){setPlanPill(lock,'PRO','pill pill-muted');el.innerHTML=unlockHtml('Automated maintenance','pro')}else{el.innerHTML='<div class="empty" data-csp-style="s87">'+esc(e.message)+'</div>'}}}
async function saveAutomation(){const body={enabled:document.getElementById('au-enabled').checked,cadence_hours:Number(document.getElementById('au-cadence').value)||24,consolidate:document.getElementById('au-consolidate').checked,min_cluster:Number(document.getElementById('au-mincluster').value)||3,archive_below:Number(document.getElementById('au-archive').value)||0.05,dream:document.getElementById('au-dream').checked,dream_min_new:Number(document.getElementById('au-dream-min').value)||20,dream_idle_minutes:Number(document.getElementById('au-dream-idle').value),infer:document.getElementById('au-infer').checked};try{await api('/automation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('Policy saved','ok');loadAutomation()}catch(e){toast(e.status===402?'Automation is a Pro feature':e.message,'err')}}
async function runMaintenance(dry){const el=document.getElementById('au-result');if(el)el.innerHTML='<div class="spinner" data-csp-style="s93"></div>';try{const d=await api('/maintenance/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dry_run:dry})});const n=(d.runs||[]).length;if(el)el.innerHTML=`<span class="pill pill-green" data-csp-style="s9">${dry?'DRY RUN':'DONE'}</span> Swept ${n} workspace${n===1?'':'s'}.<pre data-csp-style="s94">${esc(JSON.stringify(d.runs,null,2))}</pre>`;if(!dry)toast('Maintenance complete','ok')}catch(e){if(el)el.innerHTML='<div class="empty" data-csp-style="s85">'+esc(e.message)+'</div>';toast(e.status===402?'Automation is a Pro feature':e.message,'err')}}

const runMaintenanceBase=runMaintenance;
let MAINTENANCE_PENDING=false;
runMaintenance=async function(dry){
 if(MAINTENANCE_PENDING)return;
 if(!dry&&!await confirmAction('Commit maintenance','The saved policy will run across the workspaces available to this account and may consolidate memories or archive items below the configured retention threshold. Use Preview for a no-change report.','Commit maintenance',true))return;
 const buttons=Array.from(document.querySelectorAll('#automation-body button[data-onclick="h89"],#automation-body button[data-onclick="h90"]')),labels=buttons.map(button=>button.textContent);
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
/* workspaces */
/* Folder creation is a member+ action in team mode (server enforces via the POST role
   gate); viewers can't create. Outside team mode TEAM_USER is null and anyone can. */
function canCreateWs(){return !TEAM_USER||TEAM_USER.role==='member'||TEAM_USER.role==='admin'}
/* A folder card, shared by the Workspaces tab and the Team dashboard's Folders panel.
   opts.manage controls curation actions; opts.onName controls which view remains active.
   Folder creation and privacy selection stay available in Workspaces even in team mode. */
function folderCardName(el){const card=el.closest('.vault-card');return card?card.dataset.workspace:''}
function folderOpen(el){const card=el.closest('.vault-card');if(!card)return;const name=card.dataset.workspace;if(card.dataset.open==='tfOpen')tfOpen(name);else wsSwitch(name)}
function folderCardHtml(w,opts){opts=opts||{};const manage=opts.manage!==false;const onName=opts.onName==='tfOpen'?'tfOpen':'wsSwitch';const a=w.name===WS;const count=Number(w.memories)||0;const personal=w.visibility==='personal';const badge=personal?'<span class="pill pill-accent" data-csp-style="s9" title="Personal — visible only to you">personal</span>':(TEAM_ENABLED?'<span class="pill pill-muted" data-csp-style="s9" title="Shared with your whole team">shared</span>':'');const canChangeAccess=personal||w.can_change_access===true;const nextVisibility=personal?'shared':'personal';const access=TEAM_ENABLED?`<button class="btn btn-ghost btn-sm" data-next-visibility="${nextVisibility}"${canChangeAccess?'':' disabled title="Only the original sharer or an admin can unshare this folder"'} data-onclick="h98">${personal?'Share':'Unshare'}</button>`:'';const actions=manage?`<div class="vault-card-actions">${access}<button class="btn btn-ghost btn-sm" data-onclick="h99">Rename</button><button class="btn btn-ghost btn-sm" data-onclick="h100">Describe</button><button class="btn btn-ghost btn-sm" data-onclick="h101">Merge</button><button class="btn btn-ghost btn-sm" data-onclick="h102">Copy</button><button class="btn btn-danger btn-sm" data-onclick="h103">Delete</button></div>`:'';return `<div class="vault-card${a?' active':''}" data-workspace="${esc(w.name)}" data-memories="${count}" data-open="${onName}">${actions}<div class="vault-card-name" data-csp-style="s117" data-onclick="h104">${esc(w.name)} ${badge}${a?' <span class="pill pill-green" data-csp-style="s9">active</span>':''}</div>${w.description?`<div class="vault-card-desc">${esc(w.description)}</div>`:''}<div class="vault-card-stats"><span>${count} memories</span>${w.repos&&(Array.isArray(w.repos)?w.repos.length:w.repos)?'<span>'+(Array.isArray(w.repos)?w.repos.length:w.repos)+' repos</span>':''}<a href="#" data-onclick="h105" data-csp-style="s118">View memories →</a></div></div>`}
function tfOpen(name){setWS(name);toast('Active folder: '+name,'ok');if(document.getElementById('view-team').classList.contains('active'))renderTeamFolders();else if(document.getElementById('view-workspaces').classList.contains('active'))loadWorkspaces()}
function tfMemories(name){setWS(name);navTo('memories')}
async function refreshFolders(){if(document.getElementById('view-team').classList.contains('active')){await renderTeamFolders()}else{await loadWorkspaces()}}
async function loadWorkspaces(){
 const el=document.getElementById('ws-cards'),canNew=canCreateWs(),canSources=!TEAM_USER||TEAM_USER.role==='admin';
 const nb=document.getElementById('ws-new-btn'),ic=document.getElementById('import-card'),cc=document.getElementById('code-import-card'),iwn=document.getElementById('import-ws-name');
 if(iwn)iwn.textContent=WS?('"'+WS+'"'):'the active folder';
 if(nb){nb.textContent='New folder';showAs(nb,canNew,'inline-flex')}
 showAs(ic,canNew,'block');
 showAs(cc,canSources,'block');
 try{
  await loadWorkspaceList();
  const banner=TEAM_ENABLED?'<div class="field-hint" data-csp-style="s11">Select the folder that receives new memories and imports. Every new folder asks you to choose <b>Personal and private</b> or <b>Shared with team</b>.</div>':'';
  if(!WORKSPACES.length){
   el.innerHTML=banner+(canNew?'<div class="empty" data-csp-style="s119">No folders yet.<div data-csp-style="s52"><button class="btn btn-primary btn-sm" data-onclick="h63">New folder</button></div></div>':'<div class="empty" data-csp-style="s12">No folders yet. Ask an admin or member to create one.</div>');
   return;
  }
  el.innerHTML=banner+'<div class="cols-2">'+WORKSPACES.map(w=>folderCardHtml(w,{manage:canNew,onName:'wsSwitch'})).join('')+'</div>';
 }catch(e){el.innerHTML='<div class="empty" data-csp-style="s105">'+esc(e.message)+'</div>'}
}
function updateFolderCreateButton(){
 const selected=document.querySelector('input[name="folder-visibility"]:checked'),button=document.getElementById('folder-create-submit');
 if(button)button.disabled=!selected;
}
function openFolderCreate(){
 if(!canCreateWs()){toast('Viewers can’t create folders','err');return}
 const overlay=document.getElementById('folder-overlay');
 document.getElementById('folder-name').value='';
 document.getElementById('folder-description').value='';
 document.querySelectorAll('input[name="folder-visibility"]').forEach(input=>{input.checked=input.value==='personal'});
 document.getElementById('folder-create-status').textContent='Personal and private is selected by default. Sharing requires confirmation.';
 updateFolderCreateButton();
 overlay.classList.add('show');
}
function closeFolderCreate(){document.getElementById('folder-overlay').classList.remove('show')}
async function submitFolderCreate(){
 const name=document.getElementById('folder-name').value.trim(),description=document.getElementById('folder-description').value.trim();
 const selected=document.querySelector('input[name="folder-visibility"]:checked'),status=document.getElementById('folder-create-status'),button=document.getElementById('folder-create-submit');
 if(!name){status.textContent='Enter a folder name.';document.getElementById('folder-name').focus();return}
 if(WORKSPACES.some(w=>w.name===name)){status.textContent='A folder named "'+name+'" already exists.';return}
 if(!selected){status.textContent='Choose Personal and private or Shared with team.';return}
 const visibility=selected.value;
 button.disabled=true;status.textContent='Creating '+(visibility==='personal'?'a private folder…':'a team folder…');
 try{
  if(visibility==='shared'&&!await confirmAction('Share folder','Every team member will be able to see and use "'+name+'".','Share with team')){button.disabled=false;status.textContent='Folder remains personal and private.';return}
  await api('/workspaces/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:name,description,visibility,confirmed:visibility==='shared'})});
  await loadWorkspaceList();setWS(name);closeFolderCreate();
  toast('Folder "'+name+'" created — '+(visibility==='personal'?'personal and private':'shared with team'),'ok');
  await refreshFolders();
 }catch(e){status.textContent='Create failed: '+e.message;button.disabled=false}
}
async function wsCreate(){
 if(!canCreateWs()){toast('Viewers can’t create folders','err');return}
 if(TEAM_ENABLED){openFolderCreate();return}
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
async function wsChangeVisibility(name,visibility){
 const sharing=visibility==='shared';
 const question=sharing?'Share "'+name+'" with the whole team? Every team member will be able to see and use it.':'Unshare "'+name+'"? It will become personal and private to you.';
 if(!await confirmAction(sharing?'Share folder':'Make folder private',question,sharing?'Share with team':'Make private'))return;
 try{await api('/workspaces/visibility',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:name,visibility,confirmed:true})});await loadWorkspaceList();toast('Folder "'+name+'" '+(sharing?'shared with team':'is now personal and private'),'ok');await refreshFolders()}catch(e){toast('Could not change folder access: '+e.message,'err')}
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
function renderLicense(d){const el=document.getElementById('lic-body');if(!el)return;const paid=d.plan&&d.plan!=='free';const trial=!!d.is_trial;const ts=d.trial||{};const known=d.known_features||{};const feats=Object.keys(known).map(f=>`<span class="lic-feat">${(d.features||[]).includes(f)?'✓':'○'} ${esc(known[f])}</span>`).join('');let h='';if(d.error&&!paid){h+=`<div class="trial-banner" data-csp-style="s120"><strong>A license key is configured but isn't active</strong> — ${esc(d.error)}</div>`}if(trial){const dl=ts.days_left||0;h+=`<div class="trial-banner"><strong>Free trial active</strong> — ${dl} day${dl===1?'':'s'} of Pro left. <a href="${esc(safeUrl(d.pro_upgrade_url))}" target="_blank" rel="noopener" data-csp-style="s118">Upgrade to keep it →</a></div>`}h+=`<div class="cfg-row"><span>Plan</span><span class="pill ${paid?'pill-accent':'pill-muted'}">${trial?'TRIAL · Pro':esc((d.plan||'free').toUpperCase())}</span></div>`;if(d.email&&d.email!=='trial')h+=`<div class="cfg-row"><span>Licensed to</span><span>${esc(d.email)}</span></div>`;if(d.expires)h+=`<div class="cfg-row"><span>${trial?'Trial ends':'Expires'}</span><span>${new Date(d.expires*1000).toISOString().slice(0,10)}</span></div>`;h+=`<div data-csp-style="s121">${feats}</div>`;if(paid){h+=`<div data-csp-style="s122"><button class="btn btn-ghost btn-sm" data-onclick="h106">Export JSON</button><button class="btn btn-ghost btn-sm" data-onclick="h107">Signed compliance export</button></div>`;const canTeam=!(d.features||[]).includes('team');if(trial){h+=`<div class="field" data-csp-style="s52"><div data-csp-style="s123"><a class="btn btn-primary btn-sm" href="${esc(safeUrl(d.pro_upgrade_url))}" target="_blank" rel="noopener">Buy Pro — $10/mo (or $100/yr)</a><a class="btn btn-ghost btn-sm" href="${esc(safeUrl(d.team_upgrade_url))}" target="_blank" rel="noopener">Team — $20/seat/mo (or $200/seat/yr)</a></div><label class="field-lbl">Already have a key?</label><div data-csp-style="s124"><input class="input" id="lic-key" placeholder="ENGR1.…" data-csp-style="s1"><button class="btn btn-primary btn-sm" data-onclick="h108">Activate</button></div></div>`}else if(canTeam){h+=`<div class="field-hint" data-csp-style="s125">Need multi-user access with roles &amp; seats? <a href="${esc(safeUrl(d.team_upgrade_url))}" target="_blank" rel="noopener">Upgrade to Team — $20/seat/mo or $200/seat/yr →</a></div>`}}else{const used=ts.used;h+=`<div class="field" data-csp-style="s97">`;if(!used){h+=`<button class="btn btn-primary" data-csp-style="s126" data-onclick="h84">Start ${ts.trial_days||TRIAL_DAYS}-day free trial — unlock all Pro features</button>`}else{h+=`<div class="field-hint" data-csp-style="s127">Your free Pro trial has been used.</div>`}h+=`<button class="btn btn-ghost" data-csp-style="s128" data-onclick="h87">Start ${TRIAL_DAYS}-day free Team trial — multi-user, invite your team</button>`;h+=`<label class="field-lbl">Activate a Pro / Team key</label><div data-csp-style="s124"><input class="input" id="lic-key" placeholder="ENGR1.…" data-csp-style="s1"><button class="btn ${used?'btn-primary':'btn-ghost'} btn-sm" data-onclick="h108">Activate</button></div><div class="field-hint" data-csp-style="s97">Free forever at the core. <a href="${esc(safeUrl(d.pro_upgrade_url))}" target="_blank" rel="noopener">Pro — $10/mo or $100/yr</a> · <a href="${esc(safeUrl(d.team_upgrade_url))}" target="_blank" rel="noopener">Team — $20/seat/mo or $200/seat/yr</a></div><div class="field-hint" data-csp-style="s99"><a href="https://github.com/Coding-Dev-Tools/engraphis/blob/main/docs/HOSTING_RAILWAY.md" target="_blank" rel="noopener">Host on Railway →</a></div></div>`}el.innerHTML=h}
const renderLicenseBase=renderLicense;
renderLicense=function(d){
 renderLicenseBase(d);
 if(!d||!d.is_trial)return;
 const plan=d.plan==='team'?'Team':'Pro';
 const el=document.getElementById('lic-body');
 if(!el)return;
 const banner=el.querySelector('.trial-banner');
 if(banner)banner.innerHTML=banner.innerHTML.replace(' of Pro left.',' of '+plan+' left.');
 const pill=el.querySelector('.cfg-row .pill');
 if(pill)pill.textContent='TRIAL · '+plan;
};
async function activateLicense(){const i=document.getElementById('lic-key');const k=i?i.value.trim():'';if(!k){toast('Paste a key','err');return}try{const d=await api('/license/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})});LIC=d;updateLicBadge();updateFeatureLocks();if((!d.plan||d.plan==='free')&&d.error){toast('Key accepted but not active — '+d.error,'err')}else{toast('Activated — '+(d.plan||'').toUpperCase()+' plan','ok')}await loadLicense();loadTeam()}catch(e){toast('Activation failed: '+e.message,'err')}}
async function exportWorkspace(signed){try{const d=await api('/export?workspace='+encodeURIComponent(WS||'')+(signed?'&signed=1':''));const blob=new Blob([JSON.stringify(d,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='engraphis-export-'+(signed?'signed-':'')+Date.now()+'.json';a.click();URL.revokeObjectURL(a.href);toast(signed?'Signed compliance export downloaded':'Exported','ok')}catch(e){toast(e.status===402?'Export is a Pro feature — start your free trial':e.message,'err')}}

/* team (full CRUD: add users, change roles, disable/enable, remove, logout, seat display) */
let TEAM_USER=null;
async function loadTeam(){const el=document.getElementById('team-body');try{const st=await api('/auth/state');TEAM_ENABLED=!!st.enabled;if(!st.enabled){el.innerHTML=teamTeaser(st);return}if(!st.user){el.innerHTML='<div class="empty" data-csp-style="s105">Sign in to manage the team.</div>';showAuth(st);return}TEAM_USER=st.user;updateSessionIndicator(st.user);const isAdmin=st.user.role==='admin';let license={},users=[],overview=null,invitations=[];try{license=await api('/license')}catch(e){}try{users=(await api('/auth/users')).users||[]}catch(e){}if(isAdmin){try{overview=await api('/auth/overview')}catch(e){}try{invitations=(await api('/auth/invitations')).invitations||[]}catch(e){}}el.innerHTML=teamHeaderCard(st.user)+teamOverviewCard(license,users,overview)+'<div id="team-folders"></div>'+teamMembersCard(st.user,license,users,overview,invitations)+(isAdmin?'<div id="team-audit"><div class="card" data-csp-style="s52"><div class="card-head">Team audit log</div><div class="spinner" data-csp-style="s129"></div></div></div>':'');renderTeamFolders();if(isAdmin)loadTeamAudit()}catch(e){el.innerHTML='<div class="empty" data-csp-style="s10">Team access could not be loaded: '+esc(e.message)+'</div>'}}
function teamHeaderCard(u){return `<div class="card"><div class="card-head">Signed in as ${esc(u.email)} <span class="pill pill-accent" data-csp-style="s9">${esc(u.role)}</span></div><div data-csp-style="s130"><button class="btn btn-ghost btn-sm" data-onclick="h109">Logout</button></div></div>`}
function teamOverviewCard(license,users,overview){const seats=(overview&&overview.seats)?overview.seats:{used:nUsersActive(users),limit:(license.seats||0),available:Math.max(0,(license.seats||0)-nUsersActive(users))};const lim=seats.limit||0;const plan=(license.plan||'').toUpperCase();const grid=`<div class="stat-grid" data-csp-style="s131">${statMini(seats.used!=null?seats.used:'—','Seats in use')}${statMini(lim?lim:'∞','Seats licensed')}${statMini(lim?seats.available:'—','Seats available',(lim&&seats.available===0)?'var(--amber)':'')}${overview?statMini(overview.events_total!=null?overview.events_total:'—','Audit events'):statMini(users.length,'Members')}</div>`;let activity='';if(overview&&overview.activity&&Object.keys(overview.activity).length){const a=overview.activity,mx=Math.max.apply(null,Object.values(a).concat([1]));activity=`<div class="card" data-csp-style="s52"><div class="card-head">Team activity</div>${Object.entries(a).sort((x,y)=>y[1]-x[1]).slice(0,8).map(kv=>barRow(kv[0],kv[1],mx,'var(--accent)')).join('')}</div>`}return `<div class="card" data-csp-style="s52"><div class="card-head">Team overview${plan?'<span class="count">'+esc(plan)+' plan</span>':''}</div>${grid}</div>${activity}`}
async function renderTeamFolders(){
 const box=document.getElementById('team-folders');
 if(!box)return;
 const canNew=canCreateWs();
 try{await loadWorkspaceList()}catch(e){}
 const createRow=canNew?`<div data-csp-style="s132">
  <div class="field" data-csp-style="s133"><label class="field-lbl" for="tf-name">Folder name</label><input class="input" id="tf-name" placeholder="e.g. Product research" data-csp-style="s134" data-onkeydown="h110"></div>
  <div class="field" data-csp-style="s133"><label class="field-lbl" for="tf-desc">Description <span data-csp-style="s57">(optional)</span></label><input class="input" id="tf-desc" placeholder="What belongs here" data-csp-style="s134"></div>
  <div class="field" data-csp-style="s135"><label class="field-lbl" for="tf-vis">Who can access?</label><select class="select" id="tf-vis"><option value="personal" selected>Personal · private to me</option><option value="shared">Shared · whole team</option></select></div>
  <button class="btn btn-primary btn-sm" data-onclick="h111">Create folder</button>
 </div>`:'<div class="field-hint" data-csp-style="s115">Only admins and members can create folders. You have view access.</div>';
 const cards=WORKSPACES.length?('<div class="cols-2">'+WORKSPACES.map(w=>folderCardHtml(w,{manage:canNew,onName:'tfOpen'})).join('')+'</div>'):'<div class="empty" data-csp-style="s87">No folders yet.'+(canNew?' Create the first one above.':'')+'</div>';
 box.innerHTML=`<div class="card" data-csp-style="s52"><div class="card-head">Folders <span class="count">${WORKSPACES.length}</span></div><div class="field-hint" data-csp-style="s136">New folders are <b>personal</b> by default. Share one only after confirming that the whole team should have access. Click a folder to make it active, or “View memories”.</div>${createRow}${cards}</div>`;
}
async function tfCreate(){
 if(!canCreateWs()){toast('Viewers can’t create folders','err');return}
 const nm=(document.getElementById('tf-name').value||'').trim();
 if(!nm){toast('Enter a folder name','err');return}
 if(WORKSPACES.some(w=>w.name===nm)){toast('A folder named "'+nm+'" already exists','err');return}
 const desc=(document.getElementById('tf-desc').value||'').trim(),vis=(document.getElementById('tf-vis')||{}).value||'';
 if(vis!=='personal'&&vis!=='shared'){toast('Choose Personal and private or Shared with team','err');return}
  if(vis==='shared'&&!await confirmAction('Share folder','Every team member will be able to see and use "'+nm+'".','Share with team'))return;
 try{
  await api('/workspaces/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:nm,description:desc,visibility:vis,confirmed:vis==='shared'})});
  setWS(nm);toast('Folder "'+nm+'" created — '+(vis==='personal'?'personal and private':'shared with team'),'ok');renderTeamFolders();
 }catch(e){toast('Create failed: '+e.message,'err')}
}
function teamMembersCard(me,license,users,overview,invitations){
 invitations=invitations||[];const now=Date.now()/1000,pending=invitations.filter(i=>!i.accepted_at&&!i.revoked_at&&i.expires_at>=now);
 const seats=license.seats||0,active=nUsersActive(users),canAdd=me.role==='admin'&&(!seats||active+pending.length<seats);
 const laMap={};
 if(overview&&overview.members)overview.members.forEach(m=>{laMap[m.id]=m.last_active});
  const add=me.role==='admin'?`<div class="card" data-csp-style="s52"><div class="card-head">Invite member</div>
  <div data-csp-style="s137">
   <div class="field" data-csp-style="s138"><label class="field-lbl" for="tu-email">Email</label><input class="input" id="tu-email" type="email" autocomplete="email" placeholder="person@example.com"></div>
   <div class="field" data-csp-style="s139"><label class="field-lbl" for="tu-name">Name</label><input class="input" id="tu-name" autocomplete="name" placeholder="Full name"></div>
   <div class="field" data-csp-style="s140"><label class="field-lbl" for="tu-role">Role</label><select class="select" id="tu-role"><option value="viewer">Viewer</option><option value="member" selected>Member</option><option value="admin">Admin</option></select></div>
   <button class="btn btn-primary btn-sm" data-onclick="h112"${canAdd?'':' disabled title="Seat limit reached"'}>Send invitation</button>
  </div>
   ${seats?`<div class="field-hint" data-csp-style="s97">${active} active + ${pending.length} reserved of ${seats} seat${seats===1?'':'s'}${canAdd?'':' — at limit'}. Invitations expire after 72 hours.</div>`:'<div class="field-hint" data-csp-style="s97">Invitations expire after 72 hours and create the account only after the recipient chooses a password.</div>'}
  </div>`:'';
  const invites=me.role==='admin'?`<div class="card" data-csp-style="s141"><div class="card-head" data-csp-style="s142">Pending invitations (${pending.length})</div>${pending.map(invitationRow).join('')||'<div class="empty" data-csp-style="s10">No pending invitations.</div>'}</div>`:'';
  return add+invites+`<div class="card" data-csp-style="s141"><div class="card-head" data-csp-style="s142">Members (${users.length})${seats?` · ${active}/${seats} active seats`:' · no seat limit'}</div>${users.map(u=>teamUserRow(u,me,laMap[u.id])).join('')||'<div class="empty" data-csp-style="s10">No users</div>'}</div>`;
}
function invitationRow(inv){const expires=new Date(inv.expires_at*1000).toLocaleString();const state=inv.delivery_state||'pending';return `<div class="audit-row" data-invite-id="${esc(inv.id)}" data-invite-email="${esc(inv.email)}"><span data-csp-style="s1">${esc(inv.email)} <span class="pill pill-muted" data-csp-style="s9">${esc(inv.role)}</span></span><span class="field-hint">email ${esc(state)} · expires ${esc(expires)}</span><button class="btn btn-ghost btn-sm" data-onclick="h146">Resend</button><button class="btn btn-danger btn-sm" data-onclick="h147">Revoke</button></div>`}
function teamUserRow(u,me,lastActive){
 const isMe=u.id===me.id,isAdmin=me.role==='admin';
 let actions='';
 if(isAdmin&&!isMe){
  actions=`<select class="select select-sm" aria-label="Role for ${esc(u.email)}" data-onchange="h113" data-csp-style="s143"><option value="viewer" ${u.role==='viewer'?'selected':''}>Viewer</option><option value="member" ${u.role==='member'?'selected':''}>Member</option><option value="admin" ${u.role==='admin'?'selected':''}>Admin</option></select><button class="btn btn-ghost btn-sm" data-current-disabled="${u.disabled}" aria-label="${u.disabled?'Enable':'Disable'} ${esc(u.email)}" data-onclick="h114" data-csp-style="s144">${u.disabled?'Enable':'Disable'}</button><button class="btn btn-danger btn-sm" aria-label="Remove ${esc(u.email)}" data-onclick="h115" data-csp-style="s144">Remove</button>`;
 }
 const la=lastActive?`<span data-csp-style="s145" title="Last active">${esc(fmtRel(lastActive))}</span>`:'';
 return `<div class="audit-row" data-user-id="${esc(u.id)}" data-user-email="${esc(u.email)}"><span data-csp-style="s1">${esc(u.email)}${isMe?' <span data-csp-style="s145">(you)</span>':''}${u.disabled?' <span class="pill pill-amber" data-csp-style="s9">DISABLED</span>':''}</span>${la}${actions}<span class="pill pill-muted" data-csp-style="s9">${esc(u.role)}</span></div>`;
}
async function loadTeamAudit(){const box=document.getElementById('team-audit');if(!box)return;try{const d=await api('/auth/audit?limit=50');const rows=d.events||[];const body=rows.length?('<div class="card" data-csp-style="s146">'+rows.map(e=>`<div class="audit-row"><span class="pill pill-accent" data-csp-style="s9">${esc(e.action||'')}</span><span data-csp-style="s114">${esc(e.actor_email||'system')}${e.target?' → '+esc(e.target):''}${e.detail?' · '+esc(e.detail):''}</span><span data-csp-style="s101">${e.ts?fmtRel(e.ts):''}</span></div>`).join('')+'</div>'):'<div class="empty" data-csp-style="s147">No audit events yet.</div>';box.innerHTML=`<div class="card" data-csp-style="s52"><div class="card-head">Team audit log<span class="count">${d.total!=null?d.total:rows.length} total</span></div><div data-csp-style="s148"><button class="btn btn-ghost btn-sm" data-onclick="h116">Refresh</button><button class="btn btn-ghost btn-sm" data-onclick="h117">⭳ Export CSV</button></div>${body}</div>`}catch(e){box.innerHTML=(e.status===403)?'':`<div class="card" data-csp-style="s52"><div class="empty" data-csp-style="s147">${esc(e.message)}</div></div>`}}
function downloadTeamAudit(){const a=document.createElement('a');a.href=API+'/auth/audit/export';a.download='engraphis_team_audit.csv';document.body.appendChild(a);a.click();a.remove();toast('Audit CSV downloading','ok')}
function nUsersActive(users){return users.filter(u=>!u.disabled).length}
async function doAddUser(){const e=document.getElementById('tu-email'),n=document.getElementById('tu-name'),r=document.getElementById('tu-role');if(!e)return;const v=e.value.trim(),name=n?n.value.trim():'',role=r?r.value:'member';if(!v){toast('Email required','err');return}try{const res=await api('/auth/invitations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:v,name,role})});toast(res&&res.invited?'Invitation email sent':'Invitation reserved — email delivery needs attention','ok');loadTeam()}catch(x){toast(x.message,'err')}}
async function resendInvitation(id){try{const res=await api('/auth/invitations/'+encodeURIComponent(id)+'/resend',{method:'POST'});toast(res&&res.ok?'Invitation resent':'Invitation remains pending — email delivery needs attention',res&&res.ok?'ok':'err');loadTeam()}catch(x){toast(x.message,'err')}}
async function revokeInvitation(id,email){if(!await confirmAction('Revoke invitation','Revoke the invitation for '+email+'? Its reserved seat will be released and the current link will stop working.','Revoke invitation',true))return;try{await api('/auth/invitations/'+encodeURIComponent(id),{method:'DELETE'});toast('Invitation revoked and seat released','ok');loadTeam()}catch(x){toast(x.message,'err')}}
async function doChgRole(id,role){const impact={viewer:'Viewer can read but cannot create folders, import, or administer the team.',member:'Member can create folders and import, but cannot administer the team.',admin:'Admin can manage members, roles, sources, and team settings.'}[role]||'This changes the member’s permissions.';if(!await confirmAction('Change member role',impact,'Change to '+role)){loadTeam();return}try{await api('/auth/users/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:id,role})});toast('Role updated to '+role,'ok');loadTeam()}catch(x){toast(x.message,'err');loadTeam()}}
async function doToggleUser(id,currentlyDisabled){const dis=!currentlyDisabled;if(dis&&!await confirmAction('Disable member','They will lose dashboard access immediately. Their active sessions and tokens will be invalidated, while the account remains listed and its seat becomes available.','Disable member',true)){loadTeam();return}try{await api('/auth/users/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:id,disabled:dis})});toast(dis?'Member disabled — access suspended':'Member enabled — access restored','ok');loadTeam()}catch(x){toast(x.message,'err')}}
async function doDeleteUser(id,email){if(!await confirmAction('Remove member','Remove '+email+' from this team? Their sessions and tokens are revoked and their seat is freed. They can be invited again later, but this removal cannot be undone.','Remove member',true))return;try{await api('/auth/users/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:id})});toast('Member removed and seat freed','ok');loadTeam()}catch(x){toast(x.message,'err')}}
async function doLogout(){try{await api('/auth/logout',{method:'POST'});TEAM_USER=null;updateSessionIndicator(null);
 // An explicit logout must not immediately re-trap the user behind the sign-in modal:
 // boot() below will 401 on /bootstrap (team mode requires a session for it) and, before
 // this line existed, that 401 handler reopened showAuth() unconditionally -- so clicking
 // "Logout" instantly slammed the login modal back over the page, which looks and feels
 // exactly like "I can't log out." Setting AUTH_SKIPPED here is the same thing "Skip for
 // now" already does: it tells boot()'s 401 handler to show the signed-out banner instead
 // of reopening the modal, landing the user in an actual logged-out view (Settings ->
 // License, including the free-trial buttons, is reachable there since those routes don't
 // require a session).
 AUTH_SKIPPED=true;
 toast('Signed out','ok');boot()}catch(x){toast(x.message,'err')}}
function updateSessionIndicator(user){const b=document.getElementById('session-action');if(!b)return;showAs(b,TEAM_ENABLED,'inline-flex');if(!TEAM_ENABLED)return;if(user){b.textContent=user.email.split('@')[0];b.title='Signed in as '+user.email;b.setAttribute('aria-label','Open team account')}else{b.textContent='Sign in';b.title='Sign in to the dashboard';b.setAttribute('aria-label','Sign in to the dashboard')}}
async function onSessionIndicatorClick(){try{const st=await api('/auth/state');if(!st.enabled)return;if(st.user){navTo('team');return}showAuth(st)}catch(e){}}
function teamTeaser(st){const paid=LIC&&(LIC.features||[]).includes('team');return `<div class="card teaser"><div class="card-head">Team mode</div><div data-csp-style="s149">Multi-user access with admin / member / viewer roles, PBKDF2 logins and per-seat keys.</div>${paid?'<div class="field-hint">Team mode is enabled by default; set ENGRAPHIS_TEAM_MODE=0 to opt out.</div>':'<div data-csp-style="s150"><button class="btn btn-primary btn-sm" data-onclick="h87">Start free Team trial</button><button class="btn btn-ghost btn-sm" data-onclick="h118">Team — Unlock</button></div>'}</div>`}
function showAuth(st){AUTH_MODE=(st&&st.needs_setup)?'setup':'login';const ov=document.getElementById('auth-overlay');ov.classList.add('show');const first=!!(st&&st.needs_setup);document.getElementById('auth-title').textContent=first?'Create admin account':'Sign in';document.getElementById('auth-body').innerHTML=`<div class="field"><label class="field-lbl" for="au-email">Email</label><input class="input" id="au-email" autocomplete="email"></div>${first?'<div class="field"><label class="field-lbl" for="au-name">Name</label><input class="input" id="au-name" autocomplete="name"></div>':''}<div class="field"><label class="field-lbl" for="au-pass">Password</label><input class="input" type="password" id="au-pass" autocomplete="${first?'new-password':'current-password'}" data-auth-first="${first}" data-onkeydown="h119"></div>${first?'<div class="field"><label class="field-lbl" for="au-token">Deployment token <span data-csp-style="s151">(hosted setup only)</span></label><input class="input" type="password" id="au-token" autocomplete="off"><div class="field-hint">Enter ENGRAPHIS_DEPLOYMENT_TOKEN from the deployment environment. Leave blank on loopback.</div></div>':''}<button class="btn btn-primary" data-auth-first="${first}" data-csp-style="s152" data-onclick="h120">${first?'Create admin':'Sign in'}</button>${first?'':'<div data-csp-style="s153"><a href="#" data-onclick="h121" data-csp-style="s154">Forgot password?</a></div>'}<div data-csp-style="s155"><a href="#" data-onclick="h122" data-csp-style="s145">Skip for now</a></div>`}
async function doAuth(first){const email=(document.getElementById('au-email')||{}).value,pass=(document.getElementById('au-pass')||{}).value,name=(document.getElementById('au-name')||{}).value,token=((document.getElementById('au-token')||{}).value||'').trim();try{if(first){const headers={'Content-Type':'application/json'};if(token)headers.Authorization='Bearer '+token;await api('/auth/setup',{method:'POST',headers,body:JSON.stringify({email,name,password:pass})})}else{await api('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pass})})}AUTH_SKIPPED=false;document.getElementById('auth-overlay').classList.remove('show');renderAuthBanner(false);toast('Signed in','ok');boot()}catch(e){toast(e.message,'err')}}
/* forgot / reset password — same overlay, swapped body; AUTH_MODE tracks which form is
   showing so the single closeAuth() (X / backdrop / Escape) knows what "close" means. */
function showForgot(){AUTH_MODE='forgot';document.getElementById('auth-title').textContent='Reset your password';document.getElementById('auth-body').innerHTML=`<div class="field-hint" data-csp-style="s115">Enter your account email — if it matches an account, we'll send a reset link.</div><div class="field"><label class="field-lbl" for="au-forgot-email">Email</label><input class="input" id="au-forgot-email" autocomplete="email" data-onkeydown="h123"></div><button class="btn btn-primary" data-csp-style="s152" data-onclick="h124">Send reset link</button><div data-csp-style="s153"><a href="#" data-onclick="h125" data-csp-style="s145">Back to sign in</a></div>`}
async function doForgot(){const i=document.getElementById('au-forgot-email');const email=i?i.value.trim():'';if(!email){toast('Enter your email','err');return}try{await api('/auth/forgot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});document.getElementById('auth-title').textContent='Check your email';document.getElementById('auth-body').innerHTML=`<div class="field-hint">If <b>${esc(email)}</b> matches an account, a reset link is on its way — it expires in 30 minutes.</div><div data-csp-style="s156"><a href="#" data-onclick="h125" data-csp-style="s154">Back to sign in</a></div>`}catch(e){toast(e.message,'err')}}
async function backToSignIn(){try{const st=await api('/auth/state');if(st&&st.enabled)showAuth(st)}catch(e){}}
function getAuthLinkToken(name){try{const raw=(location.hash||'').replace(/^#/,'');const t=raw.includes('=')?new URLSearchParams(raw).get(name):null;return (t&&t.trim())?t.trim():null}catch(e){return null}}
function scrubAuthLinkTokens(){try{const url=new URL(location.href);url.searchParams.delete('invite_token');url.searchParams.delete('reset_token');const raw=(url.hash||'').replace(/^#/,'');if(raw.includes('=')){const params=new URLSearchParams(raw);params.delete('invite_token');params.delete('reset_token');url.hash=params.toString()}history.replaceState(null,'',url.pathname+url.search+url.hash)}catch(e){}}
function getResetToken(){return getAuthLinkToken('reset_token')}
function getInvitationToken(){return getAuthLinkToken('invite_token')}
function showInvitationForm(){AUTH_MODE='invitation';const ov=document.getElementById('auth-overlay');ov.classList.add('show');document.getElementById('auth-title').textContent='Accept team invitation';document.getElementById('auth-body').innerHTML=`<div class="field-hint" data-csp-style="s115">Choose your password to create the reserved account. The invitation is single-use and expires 72 hours after it was sent.</div><div class="field"><label class="field-lbl" for="invite-password">Password</label><input class="input" type="password" id="invite-password" autocomplete="new-password" data-onkeydown="h148"></div><div class="field"><label class="field-lbl" for="invite-password-confirm">Confirm password</label><input class="input" type="password" id="invite-password-confirm" autocomplete="new-password" data-onkeydown="h148"></div><button class="btn btn-primary" data-csp-style="s152" data-onclick="h149">Create account and sign in</button>`}
async function acceptInvitation(){const password=((document.getElementById('invite-password')||{}).value||''),confirmPassword=((document.getElementById('invite-password-confirm')||{}).value||'');if(password.length<10){toast('Use a password with at least 10 characters','err');return}if(password!==confirmPassword){toast('Passwords do not match','err');return}try{await api('/auth/invitations/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:INVITE_TOKEN,password})});INVITE_TOKEN=null;AUTH_SKIPPED=false;scrubAuthLinkTokens();document.getElementById('auth-overlay').classList.remove('show');renderAuthBanner(false);toast('Invitation accepted — you are signed in','ok');boot()}catch(e){toast('Invitation: '+e.message,'err')}}
async function cancelInvitation(){INVITE_TOKEN=null;AUTH_SKIPPED=false;scrubAuthLinkTokens();try{const st=await api('/auth/state');if(st&&st.enabled&&!st.user){showAuth(st);return}}catch(e){}document.getElementById('auth-overlay').classList.remove('show')}
function showResetForm(){AUTH_MODE='reset';const ov=document.getElementById('auth-overlay');ov.classList.add('show');document.getElementById('auth-title').textContent='Set a new password';document.getElementById('auth-body').innerHTML=`<div class="field-hint" data-csp-style="s115">Choose a new password for your account. This link works once.</div><div class="field"><label class="field-lbl" for="au-newpass">New password</label><input class="input" type="password" id="au-newpass" autocomplete="new-password" data-onkeydown="h126"></div><button class="btn btn-primary" data-csp-style="s152" data-onclick="h127">Set new password</button><div data-csp-style="s153"><a href="#" data-onclick="h122" data-csp-style="s145">Cancel</a></div>`}
async function doReset(){const i=document.getElementById('au-newpass');const pass=i?i.value:'';try{await api('/auth/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:RESET_TOKEN,password:pass})});RESET_TOKEN=null;AUTH_SKIPPED=false;scrubAuthLinkTokens();document.getElementById('auth-overlay').classList.remove('show');renderAuthBanner(false);toast('Password updated — you are signed in','ok');boot()}catch(e){toast(e.message,'err')}}
async function cancelReset(){RESET_TOKEN=null;AUTH_SKIPPED=false;scrubAuthLinkTokens();try{const st=await api('/auth/state');if(st&&st.enabled&&!st.user){showAuth(st);return}}catch(e){}document.getElementById('auth-overlay').classList.remove('show')}
function closeAuth(){if(AUTH_MODE==='reset'){cancelReset();return}if(AUTH_MODE==='invitation'){cancelInvitation();return}document.getElementById('auth-overlay').classList.remove('show');AUTH_SKIPPED=true;renderAuthBanner(true)}
function renderAuthBanner(show){const b=document.getElementById('auth-banner');if(!b)return;showAs(b,show,'block');if(!show)return;b.innerHTML=`<div data-csp-style="s157"><div data-csp-style="s1">You're browsing signed out — some data and actions are locked.</div><button class="btn btn-primary btn-sm" data-onclick="h6">Sign in</button></div>`}

/* health + settings */
function connectionContext(){const host=(location.hostname||'').toLowerCase();return host==='localhost'||host==='127.0.0.1'||host==='::1'||host.endsWith('.localhost')?'Local engine':'Hosted deployment'}
async function checkHealth(){const label=connectionContext();try{await api('/health');const d=document.getElementById('health-dot'),t=document.getElementById('health-text');if(d){d.classList.add('health-ok');d.classList.remove('health-error')}if(t)t.textContent=label+' connected'}catch(e){const d=document.getElementById('health-dot'),t=document.getElementById('health-text');if(d){d.classList.add('health-error');d.classList.remove('health-ok')}if(t)t.textContent=label+' unavailable'}}
function renderHostedBootstrap(message){const intent=getTrialIntent(),selected=intent?intent[0].toUpperCase()+intent.slice(1):'';const reason=(intent?selected+' trial selected. ':'')+(message||'Data access stays locked until the deployment is licensed and the first admin is created.');const lic=document.getElementById('lic-body'),sync=document.getElementById('sync-body'),tokens=document.getElementById('tokens-body'),llm=document.getElementById('llm-body');if(lic)lic.innerHTML=`<div class="trial-banner" data-csp-style="s158"><strong>Hosted onboarding</strong> — ${esc(reason)}</div><ol class="field-hint"><li>Choose a Pro or Team trial.</li><li>Enter the deployment token and confirm your email.</li><li>Activation completes automatically on this server.</li><li>Create the first admin with the same deployment token.</li></ol><div class="field"><label class="field-lbl" for="hosted-api-token">Deployment token</label><input class="input" type="password" id="hosted-api-token" autocomplete="off"><div class="field-hint">Use the secret configured as <code data-csp-style="s159">ENGRAPHIS_DEPLOYMENT_TOKEN</code>. It is sent only to this deployment and the license control plane.</div></div><div class="hosted-trial-actions"><button class="btn btn-primary" data-onclick="h84">Start Pro trial</button><button class="btn btn-ghost" data-onclick="h87">Start Team trial</button></div><div class="field-hint" data-csp-style="s97">No card is required. The browser never receives the signed key and Railway does not redeploy.</div>`;if(sync)sync.innerHTML='<div class="empty" data-csp-style="s10">Cloud sync becomes available after activation and admin sign-in.</div>';if(tokens)tokens.innerHTML='<div class="empty" data-csp-style="s10">Scoped agent and sync tokens become available after the first admin signs in.</div>';if(llm)llm.innerHTML='<div class="empty" data-csp-style="s10">LLM settings become available after the first admin signs in.</div>';setViewDesc('settings','Verify ownership, activate automatically, and create the first admin.')}
async function showHostedBootstrap(message){HOSTED_BOOTSTRAP=true;try{LIC=await api('/license');updateLicBadge();updateFeatureLocks()}catch(e){}await selectView('settings');renderHostedBootstrap(message);checkHealth();const intent=getTrialIntent();if(intent)setTimeout(()=>startTrialPlan(intent),0)}
function loadSettings(){if(HOSTED_BOOTSTRAP){renderHostedBootstrap();const s=document.getElementById('cfg-store');if(s)s.textContent=location.host;return}loadLicense();loadSyncStatus();loadApiTokens();loadLlmStatus();const s=document.getElementById('cfg-store');if(s)s.textContent=location.host}

async function loadLlmStatus(){const el=document.getElementById('llm-body');if(!el)return;try{const st=await api('/llm/status');const ok=st.configured;const badge=ok?'<span class="pill pill-green" data-csp-style="s9">configured</span>':'<span class="pill pill-amber" data-csp-style="s9">not configured</span>';const keyLine=st.key_set?'API key set ✓':'<span data-csp-style="s160">No API key set</span>';let modelSel='<select class="select" id="llm-model" data-csp-style="s49" data-onchange="h128">';const models=(st.default_models||{});if(!Object.values(models).includes(st.model)){modelSel+='<option value="'+esc(st.model)+'" selected>'+esc(st.model)+' (current)</option>'}Object.entries(models).forEach(([p,m])=>{modelSel+='<option value="'+esc(m)+'"'+(m===st.model?' selected':'')+'>'+esc(m)+'</option>'});modelSel+='</select>';let provSel='<select class="select" id="llm-prov" data-csp-style="s49" data-onchange="h129">';['openai','anthropic','google','openrouter'].forEach(p=>{provSel+='<option value="'+p+'"'+(p===st.provider?' selected':'')+'>'+p+'</option>'});provSel+='</select>';el.innerHTML=`<div class="cfg-row" data-csp-style="s110"><span>Provider · Model</span><span>${badge}</span></div><div data-csp-style="s161">${provSel}${modelSel}</div><div class="cfg-row" data-csp-style="s162">${keyLine} · extractor: <code data-csp-style="s159">${esc(st.extractor)}</code></div><div data-csp-style="s163">Add this to your <code data-csp-style="s159">.env</code> and restart Engraphis:</div><div data-csp-style="s164"><textarea id="llm-snippet" class="input" readonly data-csp-style="s165">${esc(st.env_snippet)}</textarea><button class="btn btn-ghost btn-sm" data-csp-style="s166" data-onclick="h130">Copy</button></div><div data-csp-style="s167"><button class="btn btn-primary btn-sm" data-onclick="h131">Test connection</button><span id="llm-test-result" data-csp-style="s168"></span></div>`}catch(e){el.innerHTML='<div class="empty" data-csp-style="s10">'+esc(e.message)+'</div>'}}
function onLlmProvChange(){const p=document.getElementById('llm-prov').value;const sel=document.getElementById('llm-model');const defs={openai:'gpt-4o-mini',anthropic:'claude-3-5-sonnet-20241022',google:'gemini-1.5-flash',openrouter:'openai/gpt-4o-mini'};if(sel&&defs[p]){sel.value=defs[p]}updateLlmSnippet()}
function updateLlmSnippet(){const p=(document.getElementById('llm-prov')||{}).value||'openai';const m=(document.getElementById('llm-model')||{}).value||'';const ta=document.getElementById('llm-snippet');if(!ta)return;ta.value='ENGRAPHIS_LLM_PROVIDER='+p+'\nENGRAPHIS_LLM_MODEL='+m+'\nENGRAPHIS_LLM_API_KEY=<your-key>\nENGRAPHIS_EXTRACTOR=llm_structured\n'}
function copyLlmSnippet(){const ta=document.getElementById('llm-snippet');if(!ta)return;ta.select();try{navigator.clipboard.writeText(ta.value);toast('Copied .env snippet','ok')}catch(e){toast('Copy failed — select and Ctrl+C','err')}}
async function testLlm(){const r=document.getElementById('llm-test-result');if(r){r.textContent='Testing…';setTone(r,'muted')}try{const d=await api('/llm/test',{method:'POST'});if(r){if(d.ok){const transient=d.auto_enabled&&d.persisted===false;r.textContent=(transient?'⚠ ':'✓ ')+'Connected — '+esc(d.provider)+'/'+esc(d.model)+' replied: '+esc(d.reply||'(empty)')+(transient?' Extraction is active for this process, but the setting could not be saved for restart. Set ENGRAPHIS_EXTRACTOR=llm_structured and ENGRAPHIS_LLM_AUTO_EXTRACT=1 in the deployment environment.':'');setTone(r,transient?'red':'green')}else{r.textContent='✗ '+(d.error||'failed');setTone(r,'red')}}}catch(e){if(r){r.textContent='✗ '+esc(e.message);setTone(r,'red')}}}

async function renderTokList(){try{const toks=(await api('/auth/tokens')).tokens||[];const el=document.getElementById('tok-list');if(!el)return;el.innerHTML=toks.length?toks.map(t=>`<div class="audit-row"><span data-csp-style="s1">${esc(t.label||'(unlabelled)')} <span data-csp-style="s145">· ${t.revoked?'revoked':fmtRel(t.created_at)}${t.last_used_at?' · used '+fmtRel(t.last_used_at):''}</span></span>${t.revoked?'':`<button class="btn btn-ghost btn-sm" data-token-id="${esc(t.id)}" data-onclick="h132">Revoke</button>`}</div>`).join(''):'<div class="empty" data-csp-style="s85">No tokens yet.</div>'}catch(e){}}
async function loadApiTokens(){const el=document.getElementById('tokens-body');if(!el)return;try{const st=await api('/auth/state');if(!st.enabled){el.innerHTML='<div class="empty" data-csp-style="s10">Team mode is off — activate a Team license to connect agents to this instance.</div>';return}if(!st.user){el.innerHTML='<div class="empty" data-csp-style="s10">Sign in to create and manage agent tokens.</div>';return}let ci={};try{ci=await api('/auth/connect-info')}catch(e){}const base=ci.api_base||(API||'');el.innerHTML=`<div class="cfg-row" data-csp-style="s169">Point an agent at this instance with a per-user bearer token (the server stores only its hash; the raw value is shown once). <code data-csp-style="s159">POST ${esc(base)}/remember</code> · <code data-csp-style="s159">GET ${esc(base)}/recall</code></div><div data-csp-style="s167"><input class="input" id="tok-label" placeholder="token label (e.g. claude-code)" data-csp-style="s1"><button class="btn btn-primary btn-sm" data-onclick="h133">Create token</button></div><div id="tok-created" data-csp-style="s170"></div><div data-csp-style="s171">Your tokens</div><div id="tok-list"></div>`;renderTokList()}catch(e){el.innerHTML='<div class="empty" data-csp-style="s10">'+esc(e.message)+'</div>'}}
async function createApiToken(){const label=(document.getElementById('tok-label').value||'').trim();try{const d=await api('/auth/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label})});document.getElementById('tok-created').innerHTML=`<div class="card" data-csp-style="s172"><div data-csp-style="s173">Copy this token now — it won't be shown again:</div><code data-csp-style="s174">${esc(d.token)}</code><div data-csp-style="s175">Add to your agent config: <code data-csp-style="s159">Authorization: Bearer ${esc(d.token)}</code></div></div>`;document.getElementById('tok-label').value='';renderTokList()}catch(e){toast('Token: '+e.message,'err')}}
async function revokeApiToken(id){if(!await confirmAction('Revoke agent token','Every agent currently using this token will lose API access immediately. Other tokens and dashboard sessions are unaffected. This cannot be undone.','Revoke token',true))return;try{await api('/auth/token/'+id,{method:'DELETE'});toast('Agent token revoked — clients using it no longer have access','ok');renderTokList()}catch(e){toast(e.message,'err')}}
async function loadSyncStatus(){try{const d=await api('/sync/status');renderSync(d);if(d&&d.available)loadAutoSync()}catch(e){const el=document.getElementById('sync-body');if(el)el.textContent='Cloud sync is unavailable right now.'}}
function renderSync(d){const el=document.getElementById('sync-body');if(!el)return;d=d||{};const tokenForm=`<div class="field-hint" data-csp-style="s115">Paste a scoped per-user device token created in Agent tokens. The server stores only its hash; this device must keep the raw bearer in an owner-only local credential file so it can sync. Revoking the token invalidates that saved credential without rotating the account license.</div><label class="field-lbl" for="sync-token">Scoped device token</label><div data-csp-style="s176"><input class="input" type="password" id="sync-token" placeholder="engr_ut_…" autocomplete="off" data-csp-style="s1"><button class="btn btn-primary btn-sm" data-onclick="h134">Connect</button></div><label class="field-hint"><input type="checkbox" id="sync-read-only" checked> Read-only on this device (download/list only)</label>`;if(!d.available){el.innerHTML=tokenForm+`<div class="field-hint">Need an entitlement? <a href="#" data-onclick="h135" data-csp-style="s118">Start a free 3-day Pro trial</a> or <a href="${esc(safeUrl(d.upgrade_url))}" target="_blank" rel="noopener" data-csp-style="s118">get ${esc(d.tier_required||'pro')} →</a></div>`;return}const last=d.last;let status='Not synced yet on this device.';if(last){const when=new Date((last.at||0)*1000).toLocaleString();status='Last synced '+when+' — pushed '+(last.exported||0)+', +'+(last.added||0)+' new from your other devices'+((last.errors&&last.errors.length)?' · '+last.errors.length+' issue(s)':'')+'.'}const credential=d.has_user_token?'<span class="pill pill-green">scoped user token</span>':`<span class="pill pill-amber">legacy license-key migration</span>${tokenForm}`;el.innerHTML=`<div class="cfg-row"><span>Credential</span>${credential}</div><div class="field-hint" data-csp-style="s170">Shared folders sync through ${esc(d.relay_url||'the configured relay')}. Personal folders stay local. ${d.read_only?'This device downloads only.':'This device may upload and download.'}</div><div data-csp-style="s177"><button class="btn btn-primary" id="sync-btn" data-onclick="h136">Sync now</button><span class="field-hint" id="sync-status">${esc(status)}</span></div><div id="autosync-row" data-csp-style="s178"></div>`}
async function configureSyncToken(){const input=document.getElementById('sync-token'),token=((input||{}).value||'').trim(),readOnly=!!((document.getElementById('sync-read-only')||{}).checked);if(!token){toast('Paste a scoped device token','err');return}try{await api('/sync/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,read_only:readOnly})});if(input)input.value='';toast(readOnly?'Scoped sync connected in read-only mode':'Scoped sync connected with upload access','ok');loadSyncStatus()}catch(e){toast('Sync token: '+e.message,'err')}}
async function activateSyncLicense(){const i=document.getElementById('sync-key');const k=i?i.value.trim():'';if(!k){toast('Paste your license key','err');return}try{const d=await api('/license/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})});LIC=d;try{updateLicBadge();updateFeatureLocks()}catch(e){}if((!d.plan||d.plan==='free')&&d.error){toast('Key accepted but not active — '+d.error,'err')}else{toast('Activated — '+(d.plan||'').toUpperCase()+' plan','ok')}await loadSyncStatus();try{loadLicense()}catch(e){}}catch(e){toast('Activation failed: '+e.message,'err')}}
async function loadAutoSync(){const el=document.getElementById('autosync-row');if(!el)return;try{const p=await api('/sync/auto');let canEdit=true;try{const st=await api('/auth/state');if(st.enabled)canEdit=!!(st.user&&st.user.role==='admin')}catch(e){}const cad=p.cadence_minutes||15;const last=p.last_run?('Last auto-sync '+fmtRel(p.last_run)):'';const dis=canEdit?'':'disabled';const oc=canEdit?' data-onchange="h137"':'';const op=canEdit?'':' is-disabled-visual';el.innerHTML=`<div data-csp-style="s179">Automatic sync</div><label class="${op}" data-csp-style="s180"><input type="checkbox" id="autosync-on" ${p.enabled?'checked':''} ${dis}${oc}> Sync automatically every <input class="input" id="autosync-cad" type="number" min="5" value="${cad}" ${dis}${oc} data-csp-style="s181"> min</label><div class="field-hint" data-csp-style="s99">Background sync on a schedule (5 min minimum) while the dashboard server is up — keeps team memory converged for everyone on the license.${last?' · '+esc(last):''}</div>${canEdit?'':'<div class="field-hint" data-csp-style="s182">Only an admin can change team auto-sync. You can still see the current setting.</div>'}`}catch(e){el.innerHTML='<div class="empty" data-csp-style="s85">Automatic sync status could not be loaded: '+esc(e.message)+'</div>'}}
async function saveAutoSync(){const on=document.getElementById('autosync-on'),cad=document.getElementById('autosync-cad');if(!on)return;const body={enabled:on.checked,cadence_minutes:Math.max(5,Number(cad&&cad.value)||15)};try{await api('/sync/auto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast(body.enabled?('Auto-sync on — every '+body.cadence_minutes+' min'):'Auto-sync off','ok');loadAutoSync()}catch(e){toast(e.status===402?'Cloud sync is a Pro feature — start your free trial':(e.status===403?'Only an admin can change team auto-sync':('Auto-sync: '+e.message)),'err');loadAutoSync()}}
async function syncNow(){const b=document.getElementById('sync-btn');const s=document.getElementById('sync-status');if(b){b.disabled=true;b.textContent='Syncing…'}if(s)s.textContent='Contacting the cloud…';try{const d=await api('/sync/run',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const su=d.summary||{};toast('Synced — pushed '+(su.exported||0)+', '+(su.added||0)+' new from other devices','ok');await loadSyncStatus()}catch(e){toast('Sync failed: '+e.message,'err');if(b){b.disabled=false;b.textContent='Sync now'}if(s)s.textContent='Sync failed — try again.'}}

/* ─── knowledge graph (force-graph + d3-force: compact defaults and selectable layouts) ─── */
let GRAPH=null, FG=null, GRESIZE=false, GRESIZEFRAME=0, GADJ={}, GCOMPONENTS={}, GCOMPONENT_LAYOUT=null, GHILITE=null, GHOVERSET=null, GLABELRANK={}, GLABELBOXES=[], GDATA_CACHE=null, GACTIVE_DATA=null, GREDRAWFRAME=0, GPERF={large:false,dense:false};
const GRAPH_UI_V2=false;
const GRAPH_PRESETS={
 original:{label:'Original force',repel:120,link:30,gravity:14,font:13,size:3,linkw:1,labelDensity:40,curve:0,particles:0},
 compact:{label:'Compact clusters',repel:42,link:20,gravity:26,font:12,size:3,linkw:.7,labelDensity:30,curve:.08,particles:0},
 communities:{label:'Community islands',repel:58,link:22,gravity:26,font:13,size:3,linkw:.8,labelDensity:50,curve:.16,particles:0},
 radial:{label:'Radial orbit',repel:68,link:26,gravity:12,font:13,size:3,linkw:.75,labelDensity:55,curve:.22,particles:0},
 constellation:{label:'Constellation flow',repel:34,link:16,gravity:38,font:12,size:3,linkw:.65,labelDensity:35,curve:.32,particles:2},
 custom:{label:'Custom tuning',curve:.1,particles:0}
};
window.GSET=window.GSET||{mode:'compact',font:12,size:3,repel:42,link:20,gravity:26,labels:false,linkw:.7,labelDensity:30,flow:true,frozen:false};
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
 const mode=document.getElementById(GRAPH_UI_V2?'galaxy-hud-mode':'graph-hud-mode'),count=document.getElementById(GRAPH_UI_V2?'galaxy-hud-count':'graph-hud-count'),badge=document.getElementById('graph-performance-badge');
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
function buildAdj(links){GADJ={};links.forEach(link=>{const source=(link.source&&link.source.id)||link.source,target=(link.target&&link.target.id)||link.target;(GADJ[source]=GADJ[source]||new Set()).add(target);(GADJ[target]=GADJ[target]||new Set()).add(source)})}
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
function graphRefreshComponentCenters(nodes,force=false){
 const layout=window.GSET.mode+'|'+window.GSET.link;
 if(!force&&GCOMPONENT_LAYOUT===layout)return;
 graphIndexComponents(nodes);GCOMPONENT_LAYOUT=layout;
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
 var label={},ids=[];nodes.forEach(function(n,i){label[n.id]=i;ids.push(n.id);});
 for(var iter=0;iter<7;iter++){
  var changed=false;
  for(var a=ids.length-1;a>0;a--){var b=Math.floor(Math.random()*(a+1));var t=ids[a];ids[a]=ids[b];ids[b]=t;}
  for(var j=0;j<ids.length;j++){
   var id=ids[j],nb=GADJ[id];if(!nb||!nb.size)continue;
   var counts={},best=label[id],bestC=-1;
   nb.forEach(function(x){var l=label[x];counts[l]=(counts[l]||0)+1;if(counts[l]>bestC||(counts[l]===bestC&&l<best)){bestC=counts[l];best=l;}});
   if(label[id]!==best){label[id]=best;changed=true;}
  }
  if(!changed)break;
 }
 var groups={};nodes.forEach(function(n){var l=label[n.id];(groups[l]=groups[l]||[]).push(n);});
 var order=Object.keys(groups).sort(function(x,y){return groups[y].length-groups[x].length;});
 var remap={};order.forEach(function(l,i){remap[l]=i;});
 nodes.forEach(function(n){n.community=remap[label[n.id]];});
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
 const empty=document.getElementById(GRAPH_UI_V2?'galaxy-empty':'graph-empty');
 if(typeof ForceGraph==='undefined'){
  showAs(empty,true,'flex');empty.textContent='Loading graph engine…';
  graphSetLayoutStatus('Loading engine',true);
  loadForceGraph().then(()=>graphRender(fit,reheat)).catch(error=>{
   empty.textContent=error.message+'; refresh or verify the installed static assets.';
   graphSetLayoutStatus('Engine unavailable',false);
  });
  return;
 }
 const element=document.getElementById(GRAPH_UI_V2?'galaxy-net':'graph-net'),settings=window.GSET,mode=GRAPH_PRESETS[settings.mode]||GRAPH_PRESETS.compact,data=graphData(),dataChanged=GACTIVE_DATA!==data;
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
  const palette=window.GCOL.links[link.layer]||window.GCOL.links.semantic;return active?(focus?palette.active:palette.base):palette.dim;
 });
 FG.linkWidth(link=>{const width=window.GSET.linkw||1,focus=GHOVERSET&&GHOVERSET.size>1;if(!focus)return (GPERF.dense?.62:.82)*width;const source=(link.source&&link.source.id)||link.source,target=(link.target&&link.target.id)||link.target;return (source===GHILITE||target===GHILITE)?1.8*width:.25*width});
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
  communities:'Community islands · connected components are packed around nearby component centers.',
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
/* World-Class Galaxy Explorer. The legacy ForceGraph implementation above stays available as
   a no-WebGL/module fallback for one compatibility release. */
function loadAnalyticsView(){
 if(LIC&&(LIC.features||[]).includes('analytics'))return loadAnalytics();
 const el=document.getElementById('analytics-body'),lock=document.getElementById('an-lock'),acts=document.getElementById('an-actions');
 setPlanPill(lock,'PRO','pill pill-muted');
 showAs(acts,false);
 if(el)el.innerHTML=unlockHtml('Analytics','pro');
}
function loadAutomationView(){
 if(LIC&&(LIC.features||[]).includes('automation'))return loadAutomation();
 const el=document.getElementById('automation-body'),lock=document.getElementById('au-lock');
 setPlanPill(lock,'PRO','pill pill-muted');
 if(el)el.innerHTML=unlockHtml('Automated maintenance','pro');
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
async function boot(){try{const b=await api('/bootstrap');HOSTED_BOOTSTRAP=false;LIC=b.license;renderSemBanner(b.embedder);WORKSPACES=b.workspaces||[];if(!WS&&WORKSPACES.length){WORKSPACES.sort((a,b)=>(b.memories||0)-(a.memories||0));setWS(WORKSPACES[0].name)}updateLicBadge();updateFeatureLocks();loadOverview();checkHealth();renderAuthBanner(false);try{const st=await api('/auth/state');TEAM_ENABLED=!!st.enabled;TEAM_USER=st.enabled?(st.user||null):null;if(st.enabled)updateSessionIndicator(st.user||null)}catch(e){}}catch(e){if(e.status===401){renderAuthBanner(true);if(!AUTH_SKIPPED){try{const st=await api('/auth/state');if(st.enabled){showAuth(st)}else{toast('Boot failed: '+e.message,'err')}}catch(x){toast('Boot failed: '+e.message,'err')}}}else if(e.status===403){await showHostedBootstrap(e.message);resumeTrialClaim()}else{const msg='Boot failed: '+e.message;document.getElementById('stat-grid').innerHTML='<div class="empty" data-csp-style="s10">'+esc(msg)+'</div>';document.getElementById('ov-types').innerHTML='<div class="empty" data-csp-style="s67">Dashboard data could not be loaded.</div>';document.getElementById('ov-analytics').innerHTML='<div class="empty" data-csp-style="s10">Dashboard data could not be loaded.</div>';toast(msg,'err')}}}
initTheme();
// Gate on team auth if enabled, else boot directly. Auth links carry secrets only in the
// fragment so HTTP/access logs never receive them. Legacy query credentials are scrubbed
// immediately but deliberately ignored.
// (someone landed here from a password-reset email) and must not be clobbered by the
// ordinary sign-in prompt — AUTH_SKIPPED keeps boot()'s own 401 handling from reopening
// the login form underneath the reset form.
(async function(){resumeTrialClaim();INVITE_TOKEN=getInvitationToken();RESET_TOKEN=INVITE_TOKEN?null:getResetToken();scrubAuthLinkTokens();if(INVITE_TOKEN){AUTH_SKIPPED=true;showInvitationForm();boot();return}if(RESET_TOKEN){AUTH_SKIPPED=true;showResetForm();boot();return}try{const st=await api('/auth/state');TEAM_ENABLED=!!st.enabled;TEAM_USER=st.enabled?(st.user||null):null;updateSessionIndicator(TEAM_USER);if(st.enabled&&!st.user){showAuth(st)}}catch(e){}boot()})();
setInterval(checkHealth,30000);
document.addEventListener('keydown',event=>{
 if(event.key!=='Escape')return;
 const action=document.getElementById('action-overlay');
 if(action&&action.classList.contains('show')){closeActionDialog(null);return}
 const memories=document.getElementById('mm-overlay');
 if(memories&&memories.classList.contains('show')){closeEntityMems();return}
 const folder=document.getElementById('folder-overlay');
 if(folder&&folder.classList.contains('show')){closeFolderCreate();return}
 const auth=document.getElementById('auth-overlay');
 if(auth&&auth.classList.contains('show')){closeAuth();return}
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
h6:function(event){onSessionIndicatorClick()},
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
h59:function(event){togglePortfolio()},
h60:function(event){downloadAnalyticsReport()},
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
h77:function(event){if(event.target===this)closeAuth()},
h78:function(event){closeAuth()},
h79:function(event){if(event.target===this)closeFolderCreate()},
h80:function(event){closeFolderCreate()},
h81:function(event){updateFolderCreateButton()},
h82:function(event){submitFolderCreate()},
h83:function(event){pickTheme(this.dataset.themeValue)},
h84:function(event){startTrial()},
h85:function(event){navTo('analytics')},
h86:function(event){navTo('analytics')},
h87:function(event){startTeamTrial()},
h88:function(event){saveAutomation()},
h89:function(event){runMaintenance(true)},
h90:function(event){runMaintenance(false)},
h91:function(event){openMem(this.dataset.id)},
h92:function(event){memDragStart(event,this.dataset.id)},
h93:function(event){memDragOver(event,this.dataset.id)},
h94:function(event){memDragLeave(event)},
h95:function(event){memDrop(event,this.dataset.id)},
h96:function(event){memDragEnd(event)},
h97:function(event){openMem(this.closest('.mem-card').dataset.id)},
h98:function(event){event.stopPropagation();wsChangeVisibility(folderCardName(this),this.dataset.nextVisibility)},
h99:function(event){event.stopPropagation();wsRename(folderCardName(this))},
h100:function(event){event.stopPropagation();wsDescribe(folderCardName(this))},
h101:function(event){event.stopPropagation();wsMerge(folderCardName(this))},
h102:function(event){event.stopPropagation();wsCopy(folderCardName(this))},
h103:function(event){event.stopPropagation();wsDelete(folderCardName(this),Number(this.closest('.vault-card').dataset.memories))},
h104:function(event){folderOpen(this)},
h105:function(event){event.stopPropagation();event.preventDefault();tfMemories(folderCardName(this))},
h106:function(event){exportWorkspace(false)},
h107:function(event){exportWorkspace(true)},
h108:function(event){activateLicense()},
h109:function(event){doLogout()},
h110:function(event){if(event.key==='Enter')tfCreate()},
h111:function(event){tfCreate()},
h112:function(event){doAddUser()},
h113:function(event){doChgRole(this.closest('.audit-row').dataset.userId,this.value)},
h114:function(event){doToggleUser(this.closest('.audit-row').dataset.userId,this.dataset.currentDisabled==='true')},
h115:function(event){doDeleteUser(this.closest('.audit-row').dataset.userId,this.closest('.audit-row').dataset.userEmail)},
h116:function(event){loadTeamAudit()},
h117:function(event){downloadTeamAudit()},
h118:function(event){navTo('settings')},
h119:function(event){if(event.key==='Enter')doAuth(this.dataset.authFirst==='true')},
h120:function(event){doAuth(this.dataset.authFirst==='true')},
h121:function(event){showForgot();return false},
h122:function(event){closeAuth();return false},
h123:function(event){if(event.key==='Enter')doForgot()},
h124:function(event){doForgot()},
h125:function(event){backToSignIn();return false},
h126:function(event){if(event.key==='Enter')doReset()},
h127:function(event){doReset()},
h128:function(event){updateLlmSnippet()},
h129:function(event){onLlmProvChange()},
h130:function(event){copyLlmSnippet()},
h131:function(event){testLlm()},
h132:function(event){revokeApiToken(this.dataset.tokenId)},
h133:function(event){createApiToken()},
h134:function(event){configureSyncToken()},
h135:function(event){startTrial();return false},
h136:function(event){syncNow()},
h137:function(event){saveAutoSync()},
h138:function(event){graphSetTypeColor(this.dataset.nodeType,this.value,false)},
h139:function(event){graphSetTypeColor(this.dataset.nodeType,this.value,true)},
h140:function(event){closeEntityMems();openMem(this.dataset.memoryId)},
h141:function(event){graphFocus(this.dataset.entity)},
h142:function(event){graphExploreEntity(this.dataset.entity)},
h143:function(event){graphExplorerMore('nodes')},
h144:function(event){graphExplorerMore('edges')},
h145:function(event){boot()},
h146:function(event){resendInvitation(this.closest('[data-invite-id]').dataset.inviteId)},
h147:function(event){const row=this.closest('[data-invite-id]');revokeInvitation(row.dataset.inviteId,row.dataset.inviteEmail)},
h148:function(event){if(event.key==='Enter')acceptInvitation()},
h149:function(event){acceptInvitation()},
});
for(const type of ['click','keydown','input','change','dragover','dragleave','drop','dragstart','dragend']){document.addEventListener(type,function(event){const target=event.target instanceof Element?event.target.closest('[data-on'+type+']'):null;if(!target||!document.documentElement.contains(target))return;const handler=CSP_EVENT_HANDLERS[target.getAttribute('data-on'+type)];if(!handler)return;const result=handler.call(target,event);if(result===false){event.preventDefault();event.stopPropagation()}},false)}
