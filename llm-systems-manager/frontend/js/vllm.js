// ---------------------------------------------------------------------------
// vLLM metrics + server control — from agent (#125). Mirrors lmstudio.js.
// ---------------------------------------------------------------------------
let _vllmMetrics = {};

// True when a vLLM SUB-tab is the active view (cards + log live there only).
function _vllmLogViewActive() {
  const t = _activeTab;
  const ds = _subTabState && _subTabState['dashboard'];
  const ls = _subTabState && _subTabState['llm'];
  return (t === 'dashboard' && ds === 'vllm') || (t === 'llm' && ls === 'vllm');
}
// Full metrics feed LLM Control · vLLM and Overall; on Dashboards · vLLM the host page
// polls its own data, so there only the header pill is kept current (light fetch).
function _vllmMetricsViewActive() {
  return _activeTab === 'overall' || (_activeTab === 'llm' && !!_subTabState && _subTabState.llm === 'vllm');
}

// Header vLLM state pill from a /api/vllm/metrics payload.
function _updateVllmHeaderPill(d) {
  const vBanner = document.getElementById('vllmStateBanner');
  const vText   = document.getElementById('vllmStateText');
  if (!vBanner || !vText) return;
  const v = d.vllm || {};
  const online = d.agent_online === true;
  if (!online) {
    vBanner.className = 'state-banner state-unknown';
    vText.textContent = 'VLLM · offline';
  } else if (v.state === 'running') {
    const modelShort = (v.model || '').split('/').pop() || 'model';
    vBanner.className = 'state-banner state-awake';
    vText.textContent = `VLLM · Active · ${modelShort}`;
  } else {
    vBanner.className = 'state-banner state-sleeping';
    vText.textContent = `VLLM · ${v.state || 'server down'}`;
  }
}

let _vllmPillTick = 0;
async function fetchVllmMetrics() {
  if (document.hidden) return;
  // Off-view only the header pill is kept current, on every 5th tick.
  const pillOnly = !_vllmMetricsViewActive();
  _vllmPillTick = (_vllmPillTick + 1) % 5;
  if (pillOnly && (_vllmPillTick !== 0 || _pillHidden('vllmStateBanner'))) return;
  const _lk = _agentClaimKey(pillOnly ? 'fetchVllmMetrics:pill' : 'fetchVllmMetrics', 'vllm');
  if (!_claim(_lk)) return;
  try {
    const d = await _fetchT(pillOnly ? '/api/vllm/metrics?light=1' : '/api/vllm/metrics', {}, 10000).then(r => r.json());
    if (pillOnly) { _updateVllmHeaderPill(d); return; }
    _vllmMetrics = d;

    const v      = d.vllm || {};
    const online = d.agent_online === true;
    const up     = online && v.state === 'running';

    const srvPill = document.getElementById('vllmSrvPill');
    if (srvPill) {
      srvPill.className = 'mc-pill ' + (up ? 'p-active' : 'p-unloaded') + ' llm-sec-pill';
      srvPill.textContent = up ? ('Server ON' + (v.port ? ' · port ' + v.port : '')) : 'Server OFF';
    }

    renderVllmModelCards(up ? (v.models || []) : [], v.model);
    _setVllmBtns(up);
    const ctrlBadge = document.getElementById('vllmCtrlBadge');
    if (ctrlBadge) {
      const mod = up ? 'ok' : (online ? 'warn' : 'crit');
      const txt = up ? `running — ${v.model || '?'}` : (online ? 'server down' : 'agent offline');
      ctrlBadge.className = `status status--${mod}`;
      ctrlBadge.innerHTML = '<span class="status__dot"></span>' + _esc(txt);
    }

    _updateVllmHeaderPill(d);
  } catch (e) {
    console.warn('fetchVllmMetrics:', e);
  } finally {
    _release(_lk);
  }
}

// Enable/disable vLLM server control buttons (Start only when down).
function _setVllmBtns(serverUp) {
  ['vllmBtnStop', 'vllmBtnRestart', 'vllmBtnStatus'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = !serverUp;
  });
  const start = document.getElementById('vllmBtnStart');
  if (start) start.disabled = serverUp;
}

// Stored vllm bench serve results for the model-card badges (moved from the retired overlay, #894).
window._vbenchData = window._vbenchData || {};
async function loadVllmBenchData() {
  try {
    const r = await fetch(window._withAgentParam('/api/benchmark/results?provider=vllm')).then(x => x.json());
    const map = {};
    for (const row of ((r && r.results) || [])) map[row.model_id] = row;
    window._vbenchData = map;
    if (typeof fetchVllmMetrics === 'function') fetchVllmMetrics();
  } catch (e) { /* offline manager: badges simply stay hidden */ }
}
loadVllmBenchData();

let _vllmLastModels = null, _vllmLastActive = null;

function renderVllmModelCards(models, activeId) {
  const host = document.getElementById('vllmModelCards');
  if (!host) return;
  _vllmLastModels = models;
  _vllmLastActive = activeId;
  if (!models.length) {
    host.className = 'model-cards';
    host.innerHTML = '<div class="sub">No models served — start the vLLM unit or check Server Config.</div>';
    return;
  }

  const descriptor = (id) => {
    const serving = id === activeId;
    const b = (window._vbenchData || {})[id];
    const stats = b ? [
      ...(b.avg_gen_tps != null ? [{ l: 'Gen',   v: Number(b.avg_gen_tps).toFixed(1), unit: 't/s' }] : []),
      ...(b.avg_pg_tps  != null ? [{ l: 'Total', v: Number(b.avg_pg_tps).toFixed(0),  unit: 't/s' }] : []),
    ] : [];
    return {
      id, actAttr: 'data-vllmact',
      name: shortName(id), repo: id,
      pill: serving ? { state: 'active', label: 'Serving' } : { state: 'unloaded', label: 'Adapter' },
      specs: [], stats, benchAge: b && b.ts ? MC.age(b.ts) : null,
      fresh: null,
      benchTitle: 'Benchmark results — not live throughput' + (b && b.ts && MC.age(b.ts) ? ' (last run ' + MC.age(b.ts) + ')' : ''),
      benchClick: serving ? 'bench' : null,
      primary: null, buttons: [],
      menu: serving ? [
        { act: 'bench',    icon: '◷', label: 'Benchmark' },
        { act: 'autotune', icon: '⌖', label: 'Autotune' },
      ] : [],
      csub: [
        b && b.avg_gen_tps != null ? `gen <b>${MC.esc(Number(b.avg_gen_tps).toFixed(1))} t/s</b>` : null,
        serving ? 'serving' : 'adapter',
      ].filter(Boolean).join(' · '),
      open: MC.isOpen('vllm', id),
    };
  };

  const view = MC.viewOf(typeof layout === 'object' ? layout : null, 'vllm');
  MC.syncSeg('vllm');
  if (view === 'list') {
    host.className = '';
    host.innerHTML = `<div class="mc-listwrap"><div class="mc-list">${MC.rowHeader('Offline bench (t/s)', '', 'Benchmark results — not live throughput')}${models.map(id => MC.row(descriptor(id))).join('')}</div></div>`;
  } else {
    const compactView = view === 'compact';
    host.className = 'mc-grid' + (compactView ? ' mc-compactgrid' : '');
    host.innerHTML = models.map(id => compactView ? MC.compact(descriptor(id)) : MC.card(descriptor(id))).join('');
  }

  MC.bindContainer(host, 'vllm', () => renderVllmModelCards(_vllmLastModels || [], _vllmLastActive));
  if (!host._actBound) {
    host._actBound = true;
    host.addEventListener('click', ev => {
      const btn = ev.target.closest('button[data-vllmact]');
      if (!btn) return;
      const act = btn.dataset.vllmact;
      const agent = (typeof _selectedAgent === 'function' && _selectedAgent('vllm')) || (typeof toolsDefaultAgentFor === 'function' && toolsDefaultAgentFor('vllm')) || null;
      const id = btn.dataset.id || null;
      if (act === 'bench')         { MC.closeMenus(); toolsDeepLink('benchmark', id, { provider: 'vllm', agent, mode: 'offline' }); }
      else if (act === 'autotune') { MC.closeMenus(); toolsDeepLink('autotune', id, { provider: 'vllm', agent }); }
    });
  }
}

// Wire the vLLM view segment once at load.
if (typeof MC !== 'undefined' && document.getElementById('vllmMcSeg')) {
  MC.initToolbar('vllm', { segId: 'vllmMcSeg', render: () => renderVllmModelCards(_vllmLastModels || [], _vllmLastActive) });
}

async function vllmServerAction(action) {
  const _prompts = {
    stop:    { title: 'Stop the vLLM server?',    body: 'Any active inference will be interrupted.', label: 'Stop' },
    restart: { title: 'Restart the vLLM server?', body: 'Any active inference will be interrupted; the model reloads from scratch.', label: 'Restart' },
  };
  if (_prompts[action]) {
    const p = _prompts[action];
    const ok = await _themedConfirm({
      title: p.title, bodyHtml: p.body, confirmLabel: p.label, cancelLabel: 'Cancel',
    });
    if (!ok) return;
  }
  const statusEl = document.getElementById('vllmCtrlStatus');
  statusEl.style.color = 'var(--warn)';
  statusEl.textContent = action === 'status' ? 'Checking...' : `${action.charAt(0).toUpperCase() + action.slice(1)}ing...`;
  try {
    const r = await fetch(`/api/vllm/server/${action}`, {
      method: action === 'status' ? 'GET' : 'POST'
    }).then(r => r.json());
    statusEl.style.color = r.ok ? 'var(--ok)' : 'var(--crit)';
    statusEl.textContent = r.output?.trim().split('\n')[0] || (r.ok ? 'OK' : r.error || 'failed');
    if (_vllmLogOpen && r.output) {
      const box = document.getElementById('vllmLogBox');
      if (box) { box.textContent = r.output; box.scrollTop = box.scrollHeight; }
    }
    if (action !== 'status') setTimeout(fetchVllmMetrics, 2000);
  } catch (e) {
    statusEl.style.color = 'var(--crit)';
    statusEl.textContent = String(e);
  }
  setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = 'var(--fg-dim)'; }, 12000);
}

// ---------------------------------------------------------------------------
// Journal log panel — polled tail like the LMS log (#115 view-gating applies).
// ---------------------------------------------------------------------------
let _vllmLogOpen  = false;
let _vllmLogTimer = null;

function startVllmLogRefresh() {
  fetchVllmLog();
  if (_vllmLogTimer) clearInterval(_vllmLogTimer);
  _vllmLogTimer = LivePause.every(fetchVllmLog, 8000);
}
function stopVllmLogRefresh() {
  if (_vllmLogTimer) {
    clearInterval(_vllmLogTimer);
    _vllmLogTimer = null;
  }
}

function toggleVllmLog() {
  const panel = document.getElementById('vllmLogPanel');
  _vllmLogOpen = !_vllmLogOpen;
  panel.style.display = _vllmLogOpen ? '' : 'none';
  if (_vllmLogOpen) startVllmLogRefresh();
  else stopVllmLogRefresh();
}

async function fetchVllmLog() {
  if (!_vllmLogViewActive()) return;
  const box = document.getElementById('vllmLogBox');
  if (!box) return;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  try {
    const r = await fetch('/api/vllm/server/log').then(r => r.json());
    const lines = r.lines || [];
    const text  = lines.length ? lines.join('\n') : (r.error || '(no journal lines found)');
    if (box.textContent !== text) {
      box.textContent = text;
      if (atBottom || box.textContent === '') box.scrollTop = box.scrollHeight;
    }
  } catch (e) {
    box.textContent = 'Error: ' + e;
  }
}

// Open the current vLLM log in a standalone window (mirror popOutLmsLog).
function popOutVllmLog() {
  const box = document.getElementById('vllmLogBox');
  const content = box ? box.textContent : '';
  const win = window.open('', 'vllmlog', 'width=900,height=600,resizable=yes,scrollbars=yes,toolbar=no,menubar=no');
  if (!win) { _toastErr('Pop-out blocked — allow pop-ups for this page.'); return; }
  win.document.write(`<!DOCTYPE html><html><head><title>vLLM Server Log</title>
  <style>*{box-sizing:border-box;margin:0;padding:0;}body{background:#0a0a0a;color:#8a8;font-family:monospace;font-size:0.88em;display:flex;flex-direction:column;height:100vh;}
  #toolbar{background:var(--bg);border-bottom:1px solid var(--bg-card-alt);display:flex;align-items:center;gap:10px;padding:8px 12px;flex-shrink:0;}
  #toolbar span{color:var(--fg-dim);font-size:0.85em;}#log{flex:1;overflow-y:auto;padding:12px;white-space:pre-wrap;word-break:break-all;}</style>
  </head><body><div id="toolbar"><span>vLLM Server Log</span></div>
  <div id="log">${content.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>
  <script>document.getElementById('log').scrollTop=document.getElementById('log').scrollHeight;<\/script>
  </body></html>`);
}

function fullscreenVllmLog() {
  const box = document.getElementById('vllmLogBox');
  if (!box) return;
  if (box.requestFullscreen) box.requestFullscreen();
  else if (box.webkitRequestFullscreen) box.webkitRequestFullscreen();
}

// ---------------------------------------------------------------------------
// LoRA adapters (opt-in on the agent via VLLM_LORA_ENABLED)
// ---------------------------------------------------------------------------
async function _vllmLoraCall(path, body) {
  const statusEl = document.getElementById('vllmCtrlStatus');
  statusEl.style.color = 'var(--warn)';
  statusEl.textContent = 'Working…';
  try {
    const r = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(r => r.json());
    statusEl.style.color = r.ok ? 'var(--ok)' : 'var(--crit)';
    statusEl.textContent = r.ok ? '✓ done' : (r.error || r.output || `HTTP ${r.status || '?'}`);
    if (r.ok) setTimeout(fetchVllmMetrics, 2000);
  } catch (e) {
    statusEl.style.color = 'var(--crit)';
    statusEl.textContent = String(e);
  }
  setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = 'var(--fg-dim)'; }, 12000);
}

function vllmLoraLoad() {
  const name = document.getElementById('vllmLoraName')?.value.trim();
  const path = document.getElementById('vllmLoraPath')?.value.trim();
  if (!name || !path) {
    const statusEl = document.getElementById('vllmCtrlStatus');
    if (statusEl) { statusEl.style.color = 'var(--crit)'; statusEl.textContent = 'adapter name and path required'; }
    return;
  }
  _vllmLoraCall('/api/vllm/lora/load', { lora_name: name, lora_path: path });
}

function vllmLoraUnload() {
  const name = document.getElementById('vllmLoraName')?.value.trim();
  if (!name) {
    const statusEl = document.getElementById('vllmCtrlStatus');
    if (statusEl) { statusEl.style.color = 'var(--crit)'; statusEl.textContent = 'adapter name required'; }
    return;
  }
  _vllmLoraCall('/api/vllm/lora/unload', { lora_name: name });
}
