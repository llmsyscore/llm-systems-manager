// ---------------------------------------------------------------------------
// vLLM metrics + server control — from agent (#125). Mirrors lmstudio.js.
// ---------------------------------------------------------------------------
let _vllmMetrics = {};

const vllmKvChartCtx = document.getElementById('vllmKvChart')?.getContext('2d');
const vllmKvChart = vllmKvChartCtx ? new Chart(vllmKvChartCtx, {
  type: 'line',
  data: { datasets: [{ label: 'KV %', data: [], borderColor: '#7af', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3 }] },
  options: { animation: false, responsive: true, maintainAspectRatio: false, interaction: _sparkInteraction, scales: { x: { type: 'time', display: false }, y: { min: 0, max: 100, display: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 }, callback: v => v + '%' } } }, plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts } }
}) : null;

const vllmTpsChartCtx = document.getElementById('vllmTpsChart')?.getContext('2d');
const vllmTpsChart = vllmTpsChartCtx ? new Chart(vllmTpsChartCtx, {
  type: 'line',
  data: { datasets: [
    { label: 'Gen t/s',    data: [], borderColor: '#7af', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3 },
    { label: 'Prompt t/s', data: [], borderColor: '#fa7', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3 },
  ]},
  options: { animation: false, responsive: true, maintainAspectRatio: false, interaction: _sparkInteraction, scales: { x: { type: 'time', display: false }, y: { min: 0, display: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 } } } }, plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts } }
}) : null;

// Clear both vLLM chart series (called on agent-picker switch).
function _resetVllmCharts() {
  [vllmKvChart, vllmTpsChart].forEach(ch => {
    if (!ch) return;
    ch.data.datasets.forEach(ds => { ds.data = []; });
    ch.update('none');
  });
}

// True when a vLLM SUB-tab is the active view (cards + log live there only).
function _vllmLogViewActive() {
  const t = _activeTab;
  const ds = _subTabState && _subTabState['dashboard'];
  const ls = _subTabState && _subTabState['llm'];
  return (t === 'dashboard' && ds === 'vllm') || (t === 'llm' && ls === 'vllm');
}
// Metrics also feed the LLM Overall tab's vLLM fleet tile.
function _vllmMetricsViewActive() {
  return _activeTab === 'overall' || _vllmLogViewActive();
}

async function fetchVllmMetrics() {
  if (document.hidden) return;
  if (!_vllmMetricsViewActive()) return;
  const _lk = _agentClaimKey('fetchVllmMetrics', 'vllm');
  if (!_claim(_lk)) return;
  try {
    const d = await _fetchT('/api/vllm/metrics', {}, 10000).then(r => r.json());
    _vllmMetrics = d;

    const v      = d.vllm || {};
    const online = d.agent_online === true;
    const up     = online && v.state === 'running';
    const ts     = d.ts ? new Date(d.ts) : new Date();

    const badge = document.getElementById('vllm-dash-badge');
    if (badge) {
      badge.className = `status ${online ? 'status--ok' : 'status--crit'}`;
      badge.innerHTML = '<span class="status__dot"></span>' + (online ? 'online' : 'offline');
    }

    _setEl('vllm-active-model', up ? (v.model || '(no model)') : '—');
    _setEl('vllm-active-state', 'state ' + (online ? (v.state || 'unknown') : 'agent offline'));
    _setEl('vllm-req-running', up && v.requests_running != null ? String(v.requests_running) : '—');
    _setEl('vllm-req-waiting', up && v.requests_waiting != null ? String(v.requests_waiting) : '—');

    const kv = up && v.kv_cache_usage_pct != null ? v.kv_cache_usage_pct : null;
    _setEl('vllm-kv-pct', kv != null ? kv.toFixed(1) + '%' : '—');
    if (vllmKvChart && kv != null) pushPoint(vllmKvChart, ts, kv);

    const tps = up && v.tokens_per_second != null ? v.tokens_per_second : null;
    const pps = up && v.prompt_tokens_per_second != null ? v.prompt_tokens_per_second : null;
    _setEl('vllm-tps', tps != null ? tps.toFixed(1) : '—');
    _setEl('vllm-pps', pps != null ? pps.toFixed(1) : '—');
    if (vllmTpsChart && (tps != null || pps != null)) pushDual(vllmTpsChart, ts, tps || 0, pps || 0);

    renderVllmModelCards(up ? (v.models || []) : [], v.model);
    _setVllmBtns(up);
    const ctrlBadge = document.getElementById('vllmCtrlBadge');
    if (ctrlBadge) {
      const mod = up ? 'ok' : (online ? 'warn' : 'crit');
      const txt = up ? `running — ${v.model || '?'}` : (online ? 'server down' : 'agent offline');
      ctrlBadge.className = `status status--${mod}`;
      ctrlBadge.innerHTML = '<span class="status__dot"></span>' + _esc(txt);
    }

    // Update the header vLLM state pill (same lifecycle as the LMS pill).
    const vBanner = document.getElementById('vllmStateBanner');
    const vText   = document.getElementById('vllmStateText');
    if (vBanner && vText) {
      if (!online) {
        vBanner.className = 'state-banner state-unknown';
        vText.textContent = 'VLLM · offline';
      } else if (up) {
        const modelShort = (v.model || '').split('/').pop() || 'model';
        vBanner.className = 'state-banner state-awake';
        vText.textContent = `VLLM · Active · ${modelShort}`;
      } else {
        vBanner.className = 'state-banner state-sleeping';
        vText.textContent = `VLLM · ${v.state || 'server down'}`;
      }
    }

    const sev = !online ? 'dash-off' : (up ? 'dash-ok' : 'dash-warn');
    ['vllm-server', 'vllm-requests', 'vllm-kv', 'vllm-throughput'].forEach(c => _dashSetStatus(c, sev));
  } catch (_) {
  } finally {
    _release(_lk);
  }
  if (typeof syncBorrowedCards === 'function') syncBorrowedCards();
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

function renderVllmModelCards(models, activeId) {
  const host = document.getElementById('vllmModelCards');
  if (!host) return;
  if (!models.length) {
    host.innerHTML = '<div class="sub">No models served — start the vLLM unit or check Server Config.</div>';
    return;
  }
  host.innerHTML = models.map(id => `
    <div class="model-card" data-id="${_esc(id)}">
      <div class="model-card-title" style="word-break:break-all;">${_esc(id)}</div>
      <span class="status ${id === activeId ? 'status--ok' : ''}"><span class="status__dot"></span>${id === activeId ? 'serving' : 'adapter'}</span>
    </div>`).join('');
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
  _vllmLogTimer = setInterval(fetchVllmLog, 8000);
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
  if (!win) { alert('Pop-out blocked — allow pop-ups for this page.'); return; }
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
