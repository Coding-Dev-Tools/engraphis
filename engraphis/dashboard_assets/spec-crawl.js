/**
 * Spec Studio — Interactive Prompt Crawler & Multi-Node Memory Auditor
 *
 * Implements:
 * 1. Single-text prompt/spec crawl replay (spider walks sections, detects flags).
 * 2. Multi-node memory cluster audit (spider walks active memory graph,
 *    detects cross-node parameter/directive contradictions, highlights graph orphans,
 *    surfaces 7-axis operational gaps, and provides one-click conflict resolution).
 */
(function() {
  'use strict';

  let currentMode = 'prompt'; // 'prompt' | 'memories'
  let currentReport = null;
  let currentSpecText = '';
  let currentWorkspace = 'default';
  let currentRepo = '';
  let requestEpoch = 0;
  let reportScopeKey = '';
  let mutationPending = false;
  let modalReturnFocus = null;
  let traceSteps = [];
  let currentStep = 0;
  let isPlaying = false;
  let playSpeed = 2; // steps per tick
  let animTimer = null;

  // Canvas & layout state
  let canvas = null;
  let ctx = null;
  let width = 800;
  let height = 480;
  let sectionLayout = [];
  let nodeLayout = {};
  let activeTokens = [];
  let crossLinks = [];

  // Spider coordinates
  const spider = {
    x: 400,
    y: 240,
    targetX: 400,
    targetY: 240,
    legs: 16,
    legPhase: 0,
    radius: 14,
    alertColor: '#00f0ff'
  };

  function init() {
    canvas = document.getElementById('spec-canvas');
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    resizeCanvas();
    window.addEventListener('resize', resizeCanvas);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && isPlaying) wakeAnimation();
    });

    setupControls();
    document.addEventListener('keydown', event => {
      const modal = document.querySelector('.spec-modal-backdrop:not([hidden])');
      if (!modal) return;
      if (event.key === 'Escape') { event.preventDefault(); closeModals(); }
      if (event.key === 'Tab') {
        const controls = Array.from(modal.querySelectorAll('button, textarea, input, [tabindex="0"]'));
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    loadDefaultCrawl();
  }

  function resizeCanvas() {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    width = rect.width || 800;
    height = rect.height || 480;
    canvas.width = width * (window.devicePixelRatio || 1);
    canvas.height = height * (window.devicePixelRatio || 1);
    if (ctx) ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);
    if (currentMode === 'memories') {
      recomputeNodeLayout();
    } else {
      recomputeSectionLayout();
    }
    drawScene();
    if (isPlaying) wakeAnimation();
  }

  function recomputeSectionLayout() {
    if (!currentReport || !currentReport.sections) return;
    const count = currentReport.sections.length;
    const centerX = width / 2;
    const centerY = height / 2;
    const radius = Math.min(width, height) * 0.35;

    sectionLayout = currentReport.sections.map((sec, i) => {
      const angle = (i / count) * Math.PI * 2 - Math.PI / 2;
      return {
        index: sec.index,
        title: sec.title,
        axis: sec.axis,
        x: centerX + Math.cos(angle) * radius,
        y: centerY + Math.sin(angle) * radius
      };
    });
  }

  function recomputeNodeLayout() {
    if (!currentReport || !currentReport.nodes) return;
    const allNodes = currentReport.nodes;
    if (allNodes.length === 0) {
      nodeLayout = {};
      return;
    }

    // Determine Focal Subgraph (max 20 nodes for clear orbital ring)
    const focalSet = new Set();
    const conflicts = currentReport.conflicts || [];
    conflicts.forEach(c => {
      focalSet.add(c.node_a);
      focalSet.add(c.node_b);
    });
    const orphans = currentReport.orphans || [];
    orphans.forEach(o => {
      if (focalSet.size < 16) focalSet.add(o.node_id);
    });
    for (let i = 0; i < allNodes.length; i++) {
      if (focalSet.size >= 20) break;
      focalSet.add(allNodes[i].id);
    }

    const focalNodes = allNodes.filter(n => focalSet.has(n.id));
    const count = focalNodes.length;
    const centerX = width / 2;
    const centerY = height / 2;
    const radius = Math.min(width, height) * 0.36;

    nodeLayout = {};
    focalNodes.forEach((n, i) => {
      const angle = (i / count) * Math.PI * 2 - Math.PI / 2;
      nodeLayout[n.id] = {
        id: n.id,
        title: n.title,
        mtype: n.mtype || 'semantic',
        axis: n.axis || 'context',
        is_orphan: !!n.is_orphan,
        x: centerX + Math.cos(angle) * radius,
        y: centerY + Math.sin(angle) * radius
      };
    });
  }

  function setupControls() {
    const playBtn = document.getElementById('spec-btn-play');
    if (playBtn) {
      playBtn.addEventListener('click', togglePlay);
    }

    const speedSelect = document.getElementById('spec-select-speed');
    if (speedSelect) {
      speedSelect.addEventListener('change', (e) => {
        playSpeed = parseFloat(e.target.value) || 2;
      });
    }

    const shipBtn = document.getElementById('spec-btn-ship');
    if (shipBtn) {
      shipBtn.addEventListener('click', () => jumpToFinal());
    }

    const inputBtn = document.getElementById('spec-btn-new');
    if (inputBtn) {
      inputBtn.addEventListener('click', () => {
        if (currentMode === 'memories') {
          loadMemoryClusterCrawl();
        } else {
          openInputModal();
        }
      });
    }

    const modePrompt = document.getElementById('spec-mode-prompt');
    const modeMemories = document.getElementById('spec-mode-memories');

    if (modePrompt) {
      modePrompt.addEventListener('click', () => {
        if (currentMode === 'prompt') return;
        setMode('prompt');
      });
    }

    if (modeMemories) {
      modeMemories.addEventListener('click', () => {
        if (currentMode === 'memories') return;
        setMode('memories');
      });
    }

    const modalCancel = document.getElementById('spec-modal-cancel');
    if (modalCancel) modalCancel.addEventListener('click', closeModals);

    const modalSubmit = document.getElementById('spec-modal-submit');
    if (modalSubmit) modalSubmit.addEventListener('click', submitNewCrawl);

    const askCancel = document.getElementById('spec-ask-cancel');
    if (askCancel) askCancel.addEventListener('click', closeModals);

    const askSubmit = document.getElementById('spec-ask-submit');
    if (askSubmit) askSubmit.addEventListener('click', submitAskAnswer);
  }

  function setMode(mode) {
    currentMode = mode;
    requestEpoch++;
    clearReport();
    const modePrompt = document.getElementById('spec-mode-prompt');
    const modeMemories = document.getElementById('spec-mode-memories');
    if (modePrompt) modePrompt.classList.toggle('active', mode === 'prompt');
    if (modeMemories) modeMemories.classList.toggle('active', mode === 'memories');
    if (modePrompt) modePrompt.setAttribute('aria-pressed', String(mode === 'prompt'));
    if (modeMemories) modeMemories.setAttribute('aria-pressed', String(mode === 'memories'));

    const scoreLabel = document.getElementById('spec-hud-score-label');
    const targetLabel = document.getElementById('spec-hud-target');
    const panel2Title = document.getElementById('spec-panel2-title');
    const panel4Title = document.getElementById('spec-panel4-title');
    const panel5Title = document.getElementById('spec-panel5-title');
    const newBtn = document.getElementById('spec-btn-new');

    if (mode === 'memories') {
      if (scoreLabel) scoreLabel.textContent = 'CLUSTER HEALTH';
      if (targetLabel) targetLabel.textContent = `ws:${currentWorkspace}`;
      if (panel2Title) panel2Title.textContent = 'MEMORY NODES';
      if (panel4Title) panel4Title.textContent = 'CONFLICTS & REMEDIATION';
      if (panel5Title) panel5Title.textContent = 'CLUSTER HEALTH';
      if (newBtn) newBtn.textContent = 'Audit Workspace';
      loadMemoryClusterCrawl();
    } else {
      if (scoreLabel) scoreLabel.textContent = 'SPEC SCORE';
      if (targetLabel) targetLabel.textContent = 'studio.prompt';
      if (panel2Title) panel2Title.textContent = 'SECTIONS';
      if (panel4Title) panel4Title.textContent = 'WORD HEAT';
      if (panel5Title) panel5Title.textContent = 'SPEC SCORE';
      if (newBtn) newBtn.textContent = 'New Crawl';
      loadDefaultCrawl();
    }
  }

  function getActiveWorkspace() {
    const wsSelect = document.getElementById('workspace-select');
    if (wsSelect && wsSelect.value) {
      return wsSelect.value.trim();
    }
    return currentWorkspace || 'default';
  }

  function apiHeaders() {
    return {
      'Content-Type': 'application/json',
      'X-Engraphis-Browser-Session': '1'
    };
  }

  function scope() {
    return { workspace: currentWorkspace, repo: currentRepo || null };
  }

  function scopeKey() { return JSON.stringify(scope()); }

  function clearReport(message = 'Enter a specification to begin.') {
    currentReport = null;
    reportScopeKey = '';
    traceSteps = [];
    currentStep = 0;
    isPlaying = false;
    if (animTimer !== null) cancelAnimationFrame(animTimer);
    isLoopRunning = false;
    activeTokens = [];
    crossLinks = [];
    sectionLayout = [];
    nodeLayout = {};
    closeModals();
    renderScore('—', 0);
    updateKindCounts();
    updateTelemetryCounters({read: 0, links: 0, flagged: 0});
    for (const id of ['spec-sections-list', 'spec-panel4-body', 'spec-stepper', 'spec-radar-svg', 'spec-log-body']) {
      const element = document.getElementById(id);
      if (element) element.replaceChildren();
    }
    const panel = document.getElementById('spec-panel4-body');
    document.getElementById('spec-sections-count').textContent = '0';
    document.getElementById('spec-panel4-badge').textContent = '0';
    if (panel) panel.textContent = message;
    const play = document.getElementById('spec-btn-play');
    if (play) play.textContent = 'Play';
    drawScene();
  }

  function setScope(workspace, repo = '') {
    const ws = workspace || 'default';
    const rp = repo || '';
    if (ws === currentWorkspace && rp === currentRepo) return;
    currentWorkspace = ws;
    currentRepo = rp;
    requestEpoch++;
    clearReport('Scope changed. Run a new crawl for the selected workspace and project.');
    const target = document.getElementById('spec-hud-target');
    if (target) target.textContent = `ws:${ws}${rp ? ' · ' + rp : ''}`;
    if (currentMode === 'memories') void loadMemoryClusterCrawl();
  }

  async function postSpec(url, body, mode) {
    const epoch = ++requestEpoch;
    const key = scopeKey();
    try {
      const response = await fetch(url, {
        method: 'POST', headers: apiHeaders(), body: JSON.stringify({...body, ...scope()})
      });
      const data = await response.json();
      if (epoch !== requestEpoch || mode !== currentMode || key !== scopeKey()) return null;
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`);
      return data;
    } catch (error) {
      if (epoch === requestEpoch && mode === currentMode && key === scopeKey()) {
        const message = `Crawl unavailable: ${error.message}`;
        clearReport(message);
        showToast(message);
      }
      return null;
    }
  }

  async function loadDefaultCrawl() {
    if (!currentSpecText) { clearReport(); return; }
    const text = currentSpecText;
    const data = await postSpec('/api/spec/crawl', {text, include_trace: true, trace_claims: true}, 'prompt');
    if (data) setPromptReport(data);
  }

  async function loadMemoryClusterCrawl() {
    clearReport('Loading the selected memory scope…');
    const data = await postSpec('/api/spec/crawl/memories', {include_trace: true}, 'memories');
    if (data) setMemoryReport(data);
  }

  function setPromptReport(report) {
    currentReport = report;
    reportScopeKey = scopeKey();
    traceSteps = report.trace || [];
    currentStep = 0;
    recomputeSectionLayout();
    renderPromptStepper();
    renderPromptSectionsPanel();
    renderRadar();
    updateKindCounts();
    renderHeatGrid();
    renderScore(report.score, report.flags ? report.flags.length : 0);

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      jumpToFinal();
    } else {
      startAnimation();
    }
  }

  function setMemoryReport(report) {
    currentReport = report;
    reportScopeKey = scopeKey();
    traceSteps = report.trace || [];
    currentStep = 0;
    recomputeNodeLayout();
    renderMemoryStepper();
    renderMemoryNodesPanel();
    renderRadar();
    updateKindCounts();
    renderConflictsAndRemediationPanel();
    renderMemoryHealthScore(report.cluster_health || report.cluster_health_score || 0);

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      jumpToFinal();
    } else {
      startAnimation();
    }
  }

  let isLoopRunning = false;

  function wakeAnimation() {
    if (!isLoopRunning) {
      isLoopRunning = true;
      animTimer = requestAnimationFrame(animationLoop);
    }
  }

  function togglePlay() {
    if (!traceSteps.length) { jumpToFinal(); return; }
    if (currentStep >= traceSteps.length) currentStep = 0;
    isPlaying = !isPlaying;
    const btn = document.getElementById('spec-btn-play');
    if (btn) btn.textContent = isPlaying ? 'Pause' : 'Play';
    if (isPlaying) wakeAnimation();
  }

  function startAnimation() {
    if (!traceSteps.length) { jumpToFinal(); return; }
    isPlaying = true;
    const btn = document.getElementById('spec-btn-play');
    if (btn) btn.textContent = 'Pause';
    wakeAnimation();
  }

  function jumpToFinal() {
    isPlaying = false;
    currentStep = traceSteps.length;
    const btn = document.getElementById('spec-btn-play');
    if (btn) btn.textContent = 'Replay';

    highlightStepper(999);
    if (currentMode === 'memories' && currentReport) {
      renderMemoryHealthScore(currentReport.cluster_health || currentReport.cluster_health_score || 0);
    } else if (currentReport) {
      renderScore(currentReport.score, currentReport.flags ? currentReport.flags.length : 0);
      updateTelemetryCounters(currentReport.counts);
    }
    drawScene();
  }

  function animationLoop() {
    if (!canvas || document.hidden || canvas.offsetParent === null) {
      isLoopRunning = false;
      return;
    }

    if (isPlaying && traceSteps.length > 0) {
      for (let s = 0; s < playSpeed; s++) {
        if (currentStep < traceSteps.length) {
          processStep(traceSteps[currentStep]);
          currentStep++;
        } else {
          jumpToFinal();
          break;
        }
      }
    }
    drawScene();

    const dx = Math.abs(spider.targetX - spider.x);
    const dy = Math.abs(spider.targetY - spider.y);
    const hasActiveFx = activeTokens.length > 0 || crossLinks.length > 0;
    const isSpiderMoving = dx > 0.5 || dy > 0.5;

    if (!isPlaying && !isSpiderMoving && !hasActiveFx) {
      isLoopRunning = false;
      return;
    }

    animTimer = requestAnimationFrame(animationLoop);
  }

  function processStep(step) {
    if (!step) return;

    if (currentMode === 'memories') {
      processMemoryStep(step);
    } else {
      processPromptStep(step);
    }
  }

  function processPromptStep(step) {
    const sec = sectionLayout[step.section];
    if (sec) {
      spider.targetX = sec.x + (Math.random() - 0.5) * 40;
      spider.targetY = sec.y + (Math.random() - 0.5) * 40;
    }

    if (step.token && sec) {
      activeTokens.push({
        text: step.token,
        kind: step.kind,
        x: spider.targetX,
        y: spider.targetY,
        alpha: 1.0,
        age: 0
      });
      if (activeTokens.length > 25) activeTokens.shift();
    }

    if (step.verb === 'link' && step.cross_from !== null && step.cross_from !== undefined) {
      const fromSec = sectionLayout[step.cross_from];
      if (fromSec && sec) {
        crossLinks.push({
          x1: fromSec.x,
          y1: fromSec.y,
          x2: sec.x,
          y2: sec.y,
          alpha: 1.0
        });
        if (crossLinks.length > 15) crossLinks.shift();
      }
    }

    appendLog(step);
    highlightStepper(step.section);
    highlightCrawlerCode(step.verb);
  }

  function processMemoryStep(step) {
    spider.alertColor = '#00f0ff';
    if (step.verb === 'node_visit' || step.verb === 'node_classify') {
      const node = nodeLayout[step.node_id];
      if (node) {
        spider.targetX = node.x;
        spider.targetY = node.y;
      }
    } else if (step.verb === 'conflict_detect') {
      const nA = nodeLayout[step.node_a];
      const nB = nodeLayout[step.node_b];
      if (nA && nB) {
        spider.targetX = (nA.x + nB.x) / 2;
        spider.targetY = (nA.y + nB.y) / 2;
        spider.alertColor = '#f43f5e';
      }
      activeTokens.push({
        text: `CONFLICT: ${step.subject || 'DIVERGENCE'}`,
        kind: 'conflict',
        x: spider.targetX,
        y: spider.targetY,
        alpha: 1.0,
        age: 0
      });
    } else if (step.verb === 'orphan_detect') {
      const node = nodeLayout[step.node_id];
      if (node) {
        spider.targetX = node.x;
        spider.targetY = node.y;
        spider.alertColor = '#f97316';
      }
      activeTokens.push({
        text: `ORPHAN: ${step.node_id}`,
        kind: 'orphan',
        x: spider.targetX,
        y: spider.targetY,
        alpha: 1.0,
        age: 0
      });
    } else if (step.verb === 'gap_detect') {
      spider.alertColor = '#f59e0b';
    }

    appendLog(step);
    highlightCrawlerCode(step.verb);
  }

  function drawScene() {
    if (!ctx) return;
    ctx.clearRect(0, 0, width, height);

    if (currentMode === 'memories') {
      drawMemoryScene();
    } else {
      drawPromptScene();
    }

    drawActiveTokens();
  }

  function drawPromptScene() {
    for (let i = 0; i < sectionLayout.length; i++) {
      const sec = sectionLayout[i];
      ctx.save();
      ctx.translate(sec.x, sec.y);

      ctx.fillStyle = '#64748b';
      ctx.font = '10px ui-monospace, monospace';
      ctx.textAlign = 'center';
      ctx.fillText(sec.title, 0, -28);

      ctx.beginPath();
      ctx.arc(0, 0, 18, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(148, 163, 184, 0.12)';
      ctx.fill();
      ctx.strokeStyle = 'rgba(148, 163, 184, 0.35)';
      ctx.lineWidth = 1;
      ctx.stroke();

      ctx.restore();
    }

    for (let i = 0; i < crossLinks.length; i++) {
      const link = crossLinks[i];
      ctx.beginPath();
      ctx.moveTo(link.x1, link.y1);
      const cpx = (link.x1 + link.x2) / 2 + 30;
      const cpy = (link.y1 + link.y2) / 2 - 30;
      ctx.quadraticCurveTo(cpx, cpy, link.x2, link.y2);
      ctx.strokeStyle = `rgba(245, 158, 11, ${link.alpha * 0.6})`;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      link.alpha = Math.max(0, link.alpha - 0.01);
    }
    crossLinks = crossLinks.filter(l => l.alpha > 0.05);

    drawSpider();
  }

  function drawMemoryScene() {
    if (!currentReport) return;

    // 1. Draw declared links (batched single path)
    const links = currentReport.links || [];
    ctx.beginPath();
    let hasLinks = false;
    for (let i = 0; i < links.length; i++) {
      const a = nodeLayout[links[i].a];
      const b = nodeLayout[links[i].b];
      if (a && b) {
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        hasLinks = true;
      }
    }
    if (hasLinks) {
      ctx.strokeStyle = 'rgba(56, 189, 248, 0.35)';
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }

    // 2. Draw orphan suggested links (dashed orange, batched)
    const orphans = currentReport.orphans || [];
    let hasOrphans = false;
    ctx.save();
    ctx.beginPath();
    ctx.setLineDash([4, 4]);
    for (let i = 0; i < orphans.length; i++) {
      const o = orphans[i];
      if (o.suggested_link_to) {
        const from = nodeLayout[o.node_id];
        const to = nodeLayout[o.suggested_link_to];
        if (from && to) {
          ctx.moveTo(from.x, from.y);
          ctx.lineTo(to.x, to.y);
          hasOrphans = true;
        }
      }
    }
    if (hasOrphans) {
      ctx.strokeStyle = 'rgba(249, 115, 22, 0.55)';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    ctx.restore();

    // 3. Draw conflict lines (dual-pass neon laser glow, zero CPU shadowBlur)
    const conflicts = currentReport.conflicts || [];
    const conflictLines = [];
    const conflictNodeIds = new Set();
    for (let i = 0; i < conflicts.length; i++) {
      const c = conflicts[i];
      conflictNodeIds.add(c.node_a);
      conflictNodeIds.add(c.node_b);
      const a = nodeLayout[c.node_a];
      const b = nodeLayout[c.node_b];
      if (a && b) {
        conflictLines.push({ x1: a.x, y1: a.y, x2: b.x, y2: b.y });
      }
    }

    if (conflictLines.length > 0) {
      // Pass 1: Wide outer glow
      ctx.beginPath();
      for (let i = 0; i < conflictLines.length; i++) {
        const cl = conflictLines[i];
        ctx.moveTo(cl.x1, cl.y1);
        ctx.lineTo(cl.x2, cl.y2);
      }
      ctx.strokeStyle = 'rgba(244, 63, 94, 0.25)';
      ctx.lineWidth = 6;
      ctx.stroke();

      // Pass 2: Sharp laser core
      ctx.beginPath();
      for (let i = 0; i < conflictLines.length; i++) {
        const cl = conflictLines[i];
        ctx.moveTo(cl.x1, cl.y1);
        ctx.lineTo(cl.x2, cl.y2);
      }
      ctx.strokeStyle = '#f43f5e';
      ctx.lineWidth = 1.8;
      ctx.stroke();
    }

    // 4. Draw Focal Nodes
    const focalNodes = Object.values(nodeLayout);
    for (let i = 0; i < focalNodes.length; i++) {
      const node = focalNodes[i];
      ctx.save();
      ctx.translate(node.x, node.y);

      // Type-based colors
      let strokeCol = '#00f0ff';
      let fillCol = 'rgba(0, 240, 255, 0.15)';
      if (node.mtype === 'episodic') {
        strokeCol = '#f59e0b';
        fillCol = 'rgba(245, 158, 11, 0.15)';
      } else if (node.mtype === 'procedural') {
        strokeCol = '#a855f7';
        fillCol = 'rgba(168, 85, 247, 0.15)';
      } else if (node.mtype === 'working') {
        strokeCol = '#10b981';
        fillCol = 'rgba(16, 185, 129, 0.15)';
      }

      // Outer alert rings
      if (conflictNodeIds.has(node.id)) {
        ctx.beginPath();
        ctx.arc(0, 0, 22, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(244, 63, 94, 0.85)';
        ctx.lineWidth = 1.8;
        ctx.stroke();
      } else if (node.is_orphan) {
        ctx.save();
        ctx.beginPath();
        ctx.setLineDash([3, 3]);
        ctx.arc(0, 0, 20, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(249, 115, 22, 0.75)';
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.restore();
      }

      // Node Core Circle
      ctx.beginPath();
      ctx.arc(0, 0, 15, 0, Math.PI * 2);
      ctx.fillStyle = fillCol;
      ctx.fill();
      ctx.strokeStyle = strokeCol;
      ctx.lineWidth = 1.8;
      ctx.stroke();

      // Node Type initials
      ctx.fillStyle = strokeCol;
      ctx.font = '8px ui-monospace, monospace';
      ctx.textAlign = 'center';
      ctx.fillText(node.mtype.substring(0, 3).toUpperCase(), 0, 3);

      // Node Title label
      ctx.fillStyle = '#cbd5e1';
      ctx.font = '10px ui-monospace, monospace';
      const cleanTitle = (node.title || node.id).substring(0, 16);
      ctx.fillText(cleanTitle, 0, -26);

      ctx.restore();
    }

    drawSpider();
  }

  function drawSpider() {
    spider.x += (spider.targetX - spider.x) * 0.15;
    spider.y += (spider.targetY - spider.y) * 0.15;
    spider.legPhase += 0.15;

    // Batch spider legs in a single path
    ctx.beginPath();
    for (let i = 0; i < spider.legs; i++) {
      const angle = (i / spider.legs) * Math.PI * 2 + Math.sin(spider.legPhase + i) * 0.1;
      const legLen = spider.radius * 2.0 + Math.sin(spider.legPhase * 2 + i) * 3;
      const lx = spider.x + Math.cos(angle) * legLen;
      const ly = spider.y + Math.sin(angle) * legLen;

      ctx.moveTo(spider.x, spider.y);
      ctx.lineTo(lx, ly);
    }
    ctx.strokeStyle = spider.alertColor || 'rgba(0, 240, 255, 0.5)';
    ctx.lineWidth = 1.2;
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(spider.x, spider.y, spider.radius, 0, Math.PI * 2);
    ctx.fillStyle = '#0f172a';
    ctx.fill();
    ctx.strokeStyle = spider.alertColor || '#00f0ff';
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(spider.x, spider.y, 4, 0, Math.PI * 2);
    ctx.fillStyle = '#f43f5e';
    ctx.fill();
  }

  function drawActiveTokens() {
    activeTokens.forEach(tok => {
      ctx.save();
      ctx.font = '10px ui-monospace, monospace';
      const label = tok.kind ? `${tok.text} · ${tok.kind}` : tok.text;
      const textWidth = ctx.measureText(label).width;
      const pad = 6;
      const bw = textWidth + pad * 2;
      const bh = 18;
      const bx = tok.x - bw / 2;
      const by = tok.y - bh / 2 - 24;

      const isAlert = tok.kind === 'conflict' || tok.kind === 'vague' || tok.kind === 'injection';
      const isOrphan = tok.kind === 'orphan';

      let bg = 'rgba(15, 23, 42, 0.9)';
      let border = '#38bdf8';
      if (isAlert) {
        bg = 'rgba(244, 63, 94, 0.95)';
        border = '#f43f5e';
      } else if (isOrphan) {
        bg = 'rgba(249, 115, 22, 0.95)';
        border = '#f97316';
      }

      ctx.globalAlpha = tok.alpha;
      ctx.fillStyle = bg;
      ctx.strokeStyle = border;
      ctx.lineWidth = 1;
      ctx.fillRect(bx, by, bw, bh);
      ctx.strokeRect(bx, by, bw, bh);

      ctx.fillStyle = '#ffffff';
      ctx.textAlign = 'center';
      ctx.fillText(label, tok.x, by + 12);
      ctx.restore();

      tok.age += 1;
      tok.alpha = Math.max(0, 1 - tok.age / 90);
    });
    activeTokens = activeTokens.filter(t => t.alpha > 0);
  }

  function appendLog(step) {
    const logBody = document.getElementById('spec-log-body');
    if (!logBody) return;
    const entry = document.createElement('div');
    entry.className = 'log-entry';

    const verbSpan = document.createElement('span');
    verbSpan.className = `log-verb-${step.verb}`;
    verbSpan.textContent = step.verb;

    const tokenSpan = document.createElement('span');
    let detail = step.token || step.node_id || step.subject || '';
    if (step.verb === 'conflict_detect') {
      detail = `${step.node_a} <-> ${step.node_b} (${step.subject})`;
    }
    tokenSpan.textContent = detail ? ` ${detail}` : '';

    entry.appendChild(verbSpan);
    entry.appendChild(tokenSpan);
    logBody.appendChild(entry);
    if (logBody.children.length > 50) {
      logBody.removeChild(logBody.firstChild);
    }
    logBody.scrollTop = logBody.scrollHeight;
  }

  let cachedStepperSteps = [];

  function renderPromptStepper() {
    const stepper = document.getElementById('spec-stepper');
    if (!stepper || !currentReport || !currentReport.sections) return;
    stepper.innerHTML = '';
    cachedStepperSteps = [];

    currentReport.sections.forEach(sec => {
      const step = document.createElement('div');
      step.className = 'spec-step';
      step.setAttribute('role', 'listitem');
      step.dataset.sectionIndex = sec.index;

      const num = document.createElement('span');
      num.className = 'spec-step-num';
      num.textContent = `0${sec.index + 1}`;

      const title = document.createElement('span');
      title.className = 'spec-step-title';
      title.textContent = sec.title;

      step.appendChild(num);
      step.appendChild(title);
      stepper.appendChild(step);
      cachedStepperSteps.push(step);
    });
  }

  function renderMemoryStepper() {
    const stepper = document.getElementById('spec-stepper');
    if (!stepper || !currentReport || !currentReport.nodes) return;
    stepper.innerHTML = '';
    cachedStepperSteps = [];

    currentReport.nodes.slice(0, 12).forEach((n, idx) => {
      const step = document.createElement('div');
      step.className = 'spec-step';
      step.setAttribute('role', 'listitem');

      const num = document.createElement('span');
      num.className = 'spec-step-num';
      num.textContent = `N${idx + 1}`;

      const title = document.createElement('span');
      title.className = 'spec-step-title';
      title.textContent = (n.title || n.id).substring(0, 14);

      step.appendChild(num);
      step.appendChild(title);
      stepper.appendChild(step);
      cachedStepperSteps.push(step);
    });
  }

  function highlightStepper(activeIdx) {
    for (let idx = 0; idx < cachedStepperSteps.length; idx++) {
      const el = cachedStepperSteps[idx];
      el.classList.toggle('active', idx === activeIdx);
      if (idx < activeIdx) el.classList.add('done');
    }
  }

  function renderPromptSectionsPanel() {
    const list = document.getElementById('spec-sections-list');
    const badge = document.getElementById('spec-sections-count');
    if (!list || !currentReport || !currentReport.sections) return;
    if (badge) badge.textContent = currentReport.sections.length;
    list.innerHTML = '';

    currentReport.sections.forEach(sec => {
      const item = document.createElement('div');
      item.className = 'sec-bar-item';

      const label = document.createElement('div');
      label.className = 'sec-bar-label';
      const heading = document.createElement('span');
      heading.textContent = sec.title;
      const words = document.createElement('span');
      words.textContent = `${sec.words}w`;
      label.append(heading, words);

      const track = document.createElement('div');
      track.className = 'sec-bar-track';
      const fill = document.createElement('div');
      fill.className = 'sec-bar-fill';
      fill.style.width = `${Math.min(100, (sec.words / 40) * 100)}%`;

      track.appendChild(fill);
      item.appendChild(label);
      item.appendChild(track);
      list.appendChild(item);
    });
  }

  function renderMemoryNodesPanel() {
    const list = document.getElementById('spec-sections-list');
    const badge = document.getElementById('spec-sections-count');
    if (!list || !currentReport || !currentReport.nodes) return;
    if (badge) badge.textContent = currentReport.nodes.length;
    list.innerHTML = '';

    currentReport.nodes.forEach(node => {
      const item = document.createElement('div');
      item.className = 'spec-node-item';
      if (node.is_orphan) item.classList.add('orphan');

      const info = document.createElement('div');
      info.textContent = (node.title || node.id).substring(0, 24);

      const typeBadge = document.createElement('span');
      typeBadge.className = `spec-node-badge spec-node-badge-${node.mtype}`;
      typeBadge.textContent = node.mtype;

      item.appendChild(info);
      item.appendChild(typeBadge);
      list.appendChild(item);
    });
  }

  function renderRadar() {
    const svg = document.getElementById('spec-radar-svg');
    if (!svg || !currentReport) return;
    const cov = currentReport.coverage || currentReport.radar;
    if (!cov) return;

    const axes = Object.keys(cov);
    const count = axes.length;
    const center = 70;
    const radius = 50;
    const points = [];

    axes.forEach((axis, i) => {
      const angle = (i / count) * Math.PI * 2 - Math.PI / 2;
      const val = cov[axis] || 0;
      const r = radius * val;
      const x = center + Math.cos(angle) * r;
      const y = center + Math.sin(angle) * r;
      points.push(`${x},${y}`);
    });

    svg.innerHTML = `
      <polygon points="${points.join(' ')}" fill="rgba(0, 255, 170, 0.2)" stroke="#00ffaa" stroke-width="1.5" />
      <circle cx="${center}" cy="${center}" r="${radius}" fill="none" stroke="#1e293b" stroke-width="1" />
    `;
  }

  function renderHeatGrid() {
    const panel = document.getElementById('spec-panel4-body');
    const badge = document.getElementById('spec-panel4-badge');
    if (!panel || !currentReport) return;
    panel.replaceChildren();
    if (badge) badge.textContent = currentReport.word_count || 0;
    const counts = document.createElement('p');
    counts.textContent = `${currentReport.word_count || 0} words · ${(currentReport.flags || []).length} displayed flags`;
    panel.appendChild(counts);
    for (const flag of currentReport.flags || []) {
      const card = document.createElement('div');
      card.className = 'spec-conflict-card';
      const question = document.createElement('p');
      question.textContent = flag.question || flag.sentence;
      card.appendChild(question);
      if (flag.kind === 'vague' && currentSpecText) {
        const ask = document.createElement('button');
        ask.type = 'button';
        ask.className = 'spec-action-btn';
        ask.textContent = `Clarify ${flag.token}`;
        ask.addEventListener('click', () => openAskModal(flag));
        card.appendChild(ask);
      }
      panel.appendChild(card);
    }
  }

  function renderConflictsAndRemediationPanel() {
    const panel4Body = document.getElementById('spec-panel4-body');
    const badge = document.getElementById('spec-panel4-badge');
    if (!panel4Body || !currentReport) return;

    const conflicts = currentReport.conflicts || [];
    const orphans = currentReport.orphans || [];
    const gaps = currentReport.policy_gaps || [];

    if (badge) badge.textContent = `${conflicts.length} conf · ${orphans.length} orph`;

    const container = document.createElement('div');
    container.className = 'spec-conflicts-list';

    if (conflicts.length === 0 && orphans.length === 0 && gaps.length === 0) {
      container.innerHTML = '<div style="color:#10b981;padding:12px;font-size:11px;">Zero conflicts or gaps detected. Cluster is coherent!</div>';
      panel4Body.innerHTML = '';
      panel4Body.appendChild(container);
      return;
    }

    conflicts.forEach(c => {
      const card = document.createElement('div');
      card.className = 'spec-conflict-card';

      const title = document.createElement('div');
      title.className = 'spec-conflict-title';
      title.textContent = `CONFLICT: ${c.subject || 'Parameter'}`;

      const resolveBtn = document.createElement('button');
      resolveBtn.className = 'spec-action-btn btn-resolve';
      resolveBtn.type = 'button';
      resolveBtn.textContent = 'Supersede';
      if (c.remedy && c.remedy.action === 'supersede') {
        resolveBtn.addEventListener('click', () => {
          resolveRemedy('supersede', c.remedy.keep_node, c.remedy.retire_node);
        });
        title.appendChild(resolveBtn);
      }

      const reason = document.createElement('div');
      reason.className = 'spec-conflict-reason';
      reason.textContent = c.reason || c.detail;

      card.appendChild(title);
      card.appendChild(reason);
      if (c.remedy && c.remedy.action === 'clarify') {
        const instructions = document.createElement('div');
        instructions.className = 'spec-conflict-reason';
        instructions.textContent = c.remedy.recommendation || c.remedy.detail || 'Choose the retained memory explicitly.';
        card.appendChild(instructions);
        [c.node_a, c.node_b].forEach(id => {
          const node = (currentReport.nodes || []).find(item => item.id === id);
          const candidate = document.createElement('div');
          candidate.className = 'spec-conflict-reason';
          const validFrom = node && node.valid_from;
          const effectiveDate = new Date(typeof validFrom === 'number' ? validFrom * 1000 : NaN);
          const date = Number.isFinite(effectiveDate.getTime()) ? effectiveDate.toISOString() : 'unknown';
          candidate.textContent = `${node && node.title ? node.title : id} (${id}) — effective date: ${date}`;
          card.appendChild(candidate);
        });
      }
      container.appendChild(card);
    });

    orphans.forEach(o => {
      const card = document.createElement('div');
      card.className = 'spec-orphan-card';

      const title = document.createElement('div');
      title.className = 'spec-orphan-title';
      title.textContent = `ORPHAN: ${(o.title || o.node_id).substring(0, 18)}`;

      if (o.suggested_link_to) {
        const linkBtn = document.createElement('button');
        linkBtn.className = 'spec-action-btn btn-link';
        linkBtn.type = 'button';
        linkBtn.textContent = 'Auto-Link';
        linkBtn.addEventListener('click', () => {
          resolveRemedy('link', o.node_id, o.suggested_link_to);
        });
        title.appendChild(linkBtn);
      }

      card.appendChild(title);
      container.appendChild(card);
    });

    gaps.forEach(gap => {
      const card = document.createElement('p');
      card.className = 'spec-conflict-card';
      card.textContent = gap.description || (gap.remedy && gap.remedy.recommendation) || gap.axis;
      container.appendChild(card);
    });
    panel4Body.replaceChildren(container);
  }

  async function resolveRemedy(action, nodeA, nodeB) {
    if (mutationPending || reportScopeKey !== scopeKey() || currentMode !== 'memories') return;
    const confirmed = action === 'supersede' && window.confirm(`Retire memory '${nodeB}' from live recall and retain '${nodeA}'? Its history remains available.`);
    if (action === 'supersede' && !confirmed) return;
    mutationPending = true;
    try {
      const data = await postSpec('/api/spec/crawl/memories/resolve', {
        action, node_a: nodeA, node_b: nodeB, confirmed
      }, 'memories');
      if (!data) return;
      showToast(data.message || 'Memory update completed.');
      await loadMemoryClusterCrawl();
    } finally { mutationPending = false; }
  }

  function showToast(msg) {
    const toast = document.getElementById('spec-toast');
    if (!toast) return;
    toast.textContent = msg;
    toast.removeAttribute('hidden');
    setTimeout(() => {
      toast.setAttribute('hidden', '');
    }, 3200);
  }

  function renderScore(score, flagCount) {
    const ring = document.getElementById('spec-score-ring');
    if (ring) ring.setAttribute('stroke-dashoffset', String(251 * (1 - Math.max(0, Math.min(100, Number(score) || 0)) / 100)));
    const scoreNum = document.getElementById('spec-score-num');
    if (scoreNum) scoreNum.textContent = score;

    const display = document.getElementById('spec-score-display');
    if (display) display.textContent = score;

    const flagsAsk = document.getElementById('spec-flags-ask');
    if (flagsAsk) {
      flagsAsk.textContent = `flags to ask · ${flagCount}`;
    }

    const motto = document.getElementById('spec-score-motto');
    if (motto) motto.textContent = "ask, don't guess";
  }

  function renderMemoryHealthScore(score) {
    renderScore(score, 0);
    const scoreNum = document.getElementById('spec-score-num');
    if (scoreNum) scoreNum.textContent = score;

    const display = document.getElementById('spec-score-display');
    if (display) display.textContent = score;

    const flagsAsk = document.getElementById('spec-flags-ask');
    if (flagsAsk && currentReport) {
      const confCount = (currentReport.conflicts || []).length;
      const orphCount = (currentReport.orphans || []).length;
      flagsAsk.textContent = `${confCount} conflicts · ${orphCount} orphans`;
    }

    const motto = document.getElementById('spec-score-motto');
    if (motto) motto.textContent = 'cluster health';
  }

  function updateKindCounts() {
    const kinds = {};
    for (const section of (currentReport && currentReport.sections) || []) {
      for (const [kind, count] of Object.entries(section.kinds || {})) kinds[kind] = (kinds[kind] || 0) + count;
    }
    for (const [kind, id] of [['claim', 'spec-count-claims'], ['owner', 'spec-count-owners'], ['approval', 'spec-count-approvals']]) {
      document.getElementById(id).textContent = currentMode === 'memories' && currentReport ? '—' : (kinds[kind] || 0);
    }
  }

  function updateTelemetryCounters(counts) {
    if (!counts) return;
    const readEl = document.getElementById('spec-hud-read');
    if (readEl) readEl.textContent = `${counts.read}/${counts.read}`;
    const linksEl = document.getElementById('spec-hud-links');
    if (linksEl) linksEl.textContent = counts.links || 0;
    const flagsEl = document.getElementById('spec-hud-flags');
    if (flagsEl) flagsEl.textContent = counts.flagged || 0;
  }

  let cachedCodeLines = null;
  function highlightCrawlerCode(verb) {
    if (!cachedCodeLines) {
      cachedCodeLines = {
        walk: document.getElementById('code-line-walk'),
        classify: document.getElementById('code-line-classify'),
        flag: document.getElementById('code-line-flag'),
        all: Array.from(document.querySelectorAll('.spec-code-body .code-line'))
      };
    }
    for (let i = 0; i < cachedCodeLines.all.length; i++) {
      cachedCodeLines.all[i].classList.remove('active');
    }
    if (verb === 'walk' || verb === 'node_visit') {
      if (cachedCodeLines.walk) cachedCodeLines.walk.classList.add('active');
    } else if (verb === 'read' || verb === 'link' || verb === 'node_classify' || verb === 'edge_traverse') {
      if (cachedCodeLines.classify) cachedCodeLines.classify.classList.add('active');
    } else if (verb === 'flag' || verb === 'conflict_detect' || verb === 'orphan_detect') {
      if (cachedCodeLines.flag) cachedCodeLines.flag.classList.add('active');
    }
  }

  function openInputModal() {
    const modal = document.getElementById('spec-modal-backdrop');
    if (modal) {
      modalReturnFocus = document.activeElement;
      modal.removeAttribute('hidden');
      document.getElementById('spec-input-text').focus();
    }
  }

  function closeModals() {
    document.querySelectorAll('.spec-modal-backdrop').forEach(m => m.setAttribute('hidden', ''));
    if (modalReturnFocus && modalReturnFocus.isConnected) modalReturnFocus.focus();
    modalReturnFocus = null;
  }

  function openAskModal(flag) {
    if (reportScopeKey !== scopeKey()) return;
    const modal = document.getElementById('spec-ask-modal-backdrop');
    const input = document.getElementById('spec-ask-input');
    document.getElementById('spec-ask-question').textContent = flag.question;
    input.dataset.flagId = flag.id;
    input.value = '';
    modalReturnFocus = document.activeElement;
    modal.removeAttribute('hidden');
    input.focus();
  }

  async function submitNewCrawl() {
    const input = document.getElementById('spec-input-text');
    const text = input ? input.value.trim() : '';
    if (!text) return;
    closeModals();
    const data = await postSpec('/api/spec/crawl', {text, include_trace: true, trace_claims: true}, 'prompt');
    if (data) { currentSpecText = text; setPromptReport(data); }
  }

  async function submitAskAnswer() {
    const input = document.getElementById('spec-ask-input');
    const answer = input ? input.value.trim() : '';
    const flag = currentReport && (currentReport.flags || []).find(f => f.id === input.dataset.flagId);
    if (!answer || !flag || reportScopeKey !== scopeKey() || mutationPending) return;
    const specText = currentSpecText;
    closeModals();
    mutationPending = true;
    try {
      const data = await postSpec('/api/spec/crawl/answer', {
        flag, answer, spec_text: specText, save_as_memory: true
      }, 'prompt');
      if (data) {
        currentSpecText = data.updated_text;
        setPromptReport(data.new_report);
        showToast(data.memory_id ? 'Clarification saved for review. Approve it in Library before using it as model context.' : 'Specification updated.');
      }
    } finally { mutationPending = false; }
  }

  window.SpecStudio = { init, setScope, setPromptReport, setMemoryReport, setMode, resizeCanvas };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
