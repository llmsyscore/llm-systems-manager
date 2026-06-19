// ─── region 1: assembled from index.html post-F3 lines 1398-1550 ────────────
// ---------------------------------------------------------------------------
// Server Config modal
// ---------------------------------------------------------------------------
const _SVC_ARG_DESCS = {
  '--threads':             'CPU threads for inference',
  '--timeout':             'Request timeout (seconds)',
  '--log-file':            'Path to log output file',
  '--kv-unified':          'Use unified KV cache across parallel slots',
  '--sleep-idle-seconds':  'Seconds idle before server enters sleep mode',
  '--host':                'Bind address (0.0.0.0 = all interfaces)',
  '--port':                'Listen port',
  '--parallel':            'Max parallel inference slots',
  '--models-max':          'Max models loaded simultaneously',
  '--mlock':               'Lock model weights in RAM (prevents swap)',
  '--models-preset':       'Path to model config INI file',
  '--metrics':             'Enable /metrics Prometheus endpoint',
  '--flash-attn':          'Enable flash attention (faster, less VRAM)',
  '--n-gpu-layers':        'GPU layers to offload (-1 = all)',
  '--ctx-size':            'Context window size (tokens)',
  '--batch-size':          'Logical batch size for prompt processing',
  '--ubatch-size':         'Physical max batch size',
  '--cache-type-k':        'KV cache quantization type for K',
  '--cache-type-v':        'KV cache quantization type for V',
  '--perf':                'Enable internal libllama performance timings',
};

let _svcBinary = '';
let _svcArgs   = [];

// Called by rendered inputs — updates _svcArgs without relying on closured index
function _svcArgVal(i, val) { if (_svcArgs[i]) _svcArgs[i].value = val; }

function _renderSvcArgs() {
  const el = document.getElementById('svcArgList');
  if (!el) return;
  if (!_svcArgs.length) {
    el.innerHTML = '<div style="color:var(--fg-dim);font-size:0.82em;padding:8px 0;">No arguments.</div>';
    return;
  }
  // Build rows using DOM so no innerHTML attribute-injection issues
  el.innerHTML = '';
  _svcArgs.forEach((a, i) => {
    const desc = _SVC_ARG_DESCS[a.flag] || '';
    const row = document.createElement('div');
    row.className = 'svcarg-row';
    const info = document.createElement('div');
    info.innerHTML = `<div class="svcarg-flag">${_esc(a.flag)}</div><div class="svcarg-desc">${_esc(desc)}</div>`;
    const valWrap = document.createElement('div');
    if (a.bool) {
      valWrap.innerHTML = `<span class="svcarg-bool"><input type="checkbox" checked disabled> flag only</span>`;
    } else {
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'svcarg-val';
      inp.value = a.value ?? '';
      inp.placeholder = 'value';
      inp.addEventListener('input', () => { if (_svcArgs[i]) _svcArgs[i].value = inp.value; });
      valWrap.appendChild(inp);
    }
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'svcarg-del';
    del.title = 'Remove';
    del.textContent = '✕';
    del.addEventListener('click', () => _svcDelArg(i));
    row.append(info, valWrap, del);
    el.appendChild(row);
  });
}

function _svcDelArg(i) {
  _svcArgs.splice(i, 1);
  _renderSvcArgs();
}

function svcAddArg() {
  const flag = document.getElementById('svcAddFlag').value.trim();
  if (!flag || !flag.startsWith('-')) {
    document.getElementById('svcConfigStatus').textContent = 'Flag must start with -';
    return;
  }
  const isBool = document.getElementById('svcAddBool').checked;
  const val    = document.getElementById('svcAddVal').value.trim();
  _svcArgs.push({ flag, value: isBool ? null : val, bool: isBool });
  document.getElementById('svcAddFlag').value = '';
  document.getElementById('svcAddVal').value  = '';
  document.getElementById('svcAddBool').checked = false;
  document.getElementById('svcConfigStatus').textContent = '';
  _renderSvcArgs();
}

// Parse a JSON response safely — surfaces the actual HTTP status and body
// when the server returns HTML or other non-JSON (e.g. Flask 500 page).
// Safari's native .json() throws an opaque "The string did not match the
// expected pattern" for non-JSON bodies; this makes the real error visible.
async function _jsonOrThrow(resp) {
  const ct = resp.headers.get('Content-Type') || '';
  const text = await resp.text();
  if (!resp.ok) {
    // Try to extract readable message from HTML error page
    const short = text.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 200);
    throw new Error(`HTTP ${resp.status}: ${short || 'no body'}`);
  }
  if (!ct.toLowerCase().includes('json')) {
    throw new Error(`Non-JSON response (${ct || 'no content-type'}): ${text.slice(0, 200)}`);
  }
  try { return JSON.parse(text); }
  catch(e) { throw new Error(`Invalid JSON: ${text.slice(0, 200)}`); }
}

async function openServerConfig() {
  document.getElementById('svcConfigStatus').textContent = 'Loading…';
  document.getElementById('svcArgList').innerHTML = '';
  document.getElementById('svcConfigBinary').textContent = '…';
  document.getElementById('svcConfigOverlay').classList.add('open');
  try {
    const d = await _jsonOrThrow(await fetch('/api/llm/server/svcconfig'));
    if (!d.ok) throw new Error(d.error);
    _svcBinary = d.binary;
    _svcArgs   = (d.args || []).map(a => ({...a})); // shallow copy
    document.getElementById('svcConfigBinary').textContent = _svcBinary;
    document.getElementById('svcConfigStatus').textContent = '';
    _renderSvcArgs();
  } catch(e) {
    console.error('openServerConfig failed:', e);
    document.getElementById('svcConfigStatus').textContent = 'Error: ' + e.message;
  }
}

function closeSvcConfig() {
  document.getElementById('svcConfigOverlay').classList.remove('open');
}

async function saveSvcConfig(doRestart) {
  const statusEl = document.getElementById('svcConfigStatus');
  statusEl.style.color = 'var(--warn)';
  statusEl.textContent = doRestart ? 'Saving and restarting…' : 'Saving…';
  try {
    const r = await _jsonOrThrow(await fetch('/api/llm/server/svcconfig', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ binary: _svcBinary, args: _svcArgs, restart: doRestart }),
    }));
    if (!r.ok) throw new Error(r.error);
    statusEl.style.color = 'var(--ok)';
    statusEl.textContent = doRestart ? '✓ Saved and restarted.' : '✓ Saved. daemon-reload complete.';
    if (doRestart) setTimeout(() => { pollServerState(); fetchMetrics(); }, 4000);
  } catch(e) {
    console.error('saveSvcConfig failed:', e);
    statusEl.style.color = 'var(--crit)';
    statusEl.textContent = 'Error: ' + e.message;
  }
}

// ─── region 2: assembled from index.html post-F3 lines 1555-1603 ────────────
// ---------------------------------------------------------------------------
// Llama Server Control
// ---------------------------------------------------------------------------
let _logEventSrc   = null;
let _logPanelOpen  = false;

async function serverAction(action) {
  const labels = { start: 'Start', stop: 'Stop', restart: 'Restart' };
  const warnings = {
    start:   { title: 'Start the llama server?',   body: '' },
    stop:    { title: 'Stop the llama server?',    body: 'Any active inference will be interrupted.' },
    restart: { title: 'Restart the llama server?', body: 'Any active inference will be interrupted.' },
  };
  const w = warnings[action] || { title: `${labels[action]} server?`, body: '' };
  const ok = await _themedConfirm({
    title:        w.title,
    bodyHtml:     w.body,
    confirmLabel: labels[action] || 'OK',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;
  const statusEl = document.getElementById('serverCtrlStatus');
  statusEl.style.color = 'var(--warn)';
  statusEl.textContent = `${action.charAt(0).toUpperCase() + action.slice(1)}ing...`;

  try {
    const r = await fetch(`/api/llm/server/${action}`, {method: 'POST'}).then(r => r.json());
    if (r.ok) {
      statusEl.style.color = 'var(--ok)';
      statusEl.textContent = `✓ ${action.charAt(0).toUpperCase() + action.slice(1)} successful`;
      // Refresh model cards after a moment for restart/start
      if (action !== 'stop') {
        setTimeout(() => refreshLLMTab(), 3000);
      } else {
        setTimeout(() => refreshLLMTab(), 1000);
      }
    } else {
      statusEl.style.color = 'var(--crit)';
      statusEl.textContent = `✗ ${r.error || 'failed'}`;
    }
  } catch(e) {
    statusEl.style.color = 'var(--crit)';
    statusEl.textContent = `✗ ${e}`;
  }

  // Clear status after 8 seconds
  setTimeout(() => { statusEl.textContent = ''; }, 8000);
}


// ─── region 3: assembled from index.html post-F3 lines 1607-1807 ────────────

async function serverStatus() {
  const statusEl = document.getElementById('serverCtrlStatus');
  statusEl.style.color = 'var(--fg-muted)';
  statusEl.textContent = 'Checking...';
  try {
    const r = await fetch('/api/llm/server/status').then(r => r.json());
    // Pause the live log stream — otherwise new lines keep appending and
    // scroll the status output out of view almost immediately.
    stopLogStream();
    const panel = document.getElementById('serverLogPanel');
    const box   = document.getElementById('serverLogBox');
    panel.style.display = '';
    _logPanelOpen = true;
    box.textContent =
      `── systemctl status llama_server ─────────────────────────────────\n` +
      (r.output || r.error || '(no output)') +
      `\n\n── log stream paused — click ↺ Refresh to resume ──`;
    box.scrollTop = 0;
    statusEl.textContent = '';
  } catch(e) {
    statusEl.style.color = 'var(--crit)';
    statusEl.textContent = `✗ ${e}`;
  }
}

async function serverWake() {
  const statusEl = document.getElementById('serverCtrlStatus');
  const btn = document.getElementById('llamaBtnWake');
  statusEl.style.color = 'var(--fg-muted)';
  statusEl.textContent = 'Waking llama-server…';
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/llm/server/wake', {method:'POST'}).then(r => r.json());
    if (r.ok) {
      statusEl.style.color = 'var(--ok)';
      statusEl.textContent = `✓ wake request acknowledged (HTTP ${r.status ?? 200})`;
    } else {
      statusEl.style.color = 'var(--crit)';
      statusEl.textContent = `✗ wake failed: ${r.error || `HTTP ${r.status}`}`;
    }
  } catch(e) {
    statusEl.style.color = 'var(--crit)';
    statusEl.textContent = `✗ ${e}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function serverPerfMode(mode) {
  const statusEl = document.getElementById('serverCtrlStatus');
  const btnIds = {performance:'llamaBtnPerfPerformance', powersave:'llamaBtnPerfPowersave'};
  const btn = document.getElementById(btnIds[mode]);
  statusEl.style.color = 'var(--fg-muted)';
  statusEl.textContent = `Switching agent host to ${mode}…`;
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/benchmark/perf-mode', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode}),
    }).then(r => r.json());
    if (r.ok) {
      statusEl.style.color = 'var(--ok)';
      statusEl.textContent = `✓ perf mode set to ${mode}`;
    } else {
      statusEl.style.color = 'var(--crit)';
      statusEl.textContent = `✗ perf-mode ${mode} failed: ${r.error || 'unknown'}`;
    }
  } catch(e) {
    statusEl.style.color = 'var(--crit)';
    statusEl.textContent = `✗ ${e}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Drag-to-resize log box
(function() {
  let dragging = false, startY = 0, startH = 0, activeBox = null, activeXtermKey = null;
  function _attachHandle(handle, box) {
    if (!handle || !box) return;
    handle.addEventListener('mousedown', e => {
      dragging = true; activeBox = box;
      activeXtermKey = handle.dataset.fitXterm || null;
      startY = e.clientY; startH = box.offsetHeight;
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
  }
  function _fitXterm(key) {
    // xterm.js' FitAddon recomputes cols/rows from the mount's pixel
    // dimensions — call it after we change height so the terminal grid
    // actually fills the new space.
    try {
      if (key === 'llama' && typeof _termFit !== 'undefined' && _termFit) _termFit.fit();
      else if (key === 'lms' && typeof _lmsTermFit !== 'undefined' && _lmsTermFit) _lmsTermFit.fit();
    } catch (_) {}
  }
  document.addEventListener('DOMContentLoaded', () => {
    _attachHandle(document.getElementById('logResizeHandle'),
                  document.getElementById('serverLogBox'));
    document.querySelectorAll('.dl-log-resize').forEach(h => {
      _attachHandle(h, document.getElementById(h.dataset.resizeTarget));
    });
  });
  document.addEventListener('mousemove', e => {
    if (!dragging || !activeBox) return;
    const newH = Math.max(80, Math.min(window.innerHeight * 0.9, startH + (e.clientY - startY)));
    activeBox.style.height = newH + 'px';
    if (activeXtermKey) _fitXterm(activeXtermKey);
  });
  document.addEventListener('mouseup', () => {
    if (dragging && activeXtermKey) _fitXterm(activeXtermKey);
    dragging = false; activeBox = null; activeXtermKey = null;
    document.body.style.userSelect = '';
  });
})();
// ---------------------------------------------------------------------------
// Model card performance snapshot — updates every 60s when LLM tab is open
// ---------------------------------------------------------------------------
let _perfTimer    = null;
let _cardPollTimer = null;

// True when the LLM Control llama.cpp sub-tab is the active view — the only
// place the model cards live. Gates the 5s model poll so it doesn't hit
// /api/llm/models from the LM Studio sub-tab (issue #120).
function _llamaCtrlViewActive() {
  return _activeTab === 'llm' && _subTabState && _subTabState['llm'] === 'llamacpp';
}

function _pollModelCards() {
  // Only refresh model status — not full config reload
  if (!_llamaCtrlViewActive()) return;
  fetch('/api/llm/models').then(r => r.json()).then(mr => {
    const newModels = mr.data || [];
    // Check if any status changed
    const changed = newModels.some((m, i) => {
      const old = (_llmModels || []).find(o => o.id === m.id);
      return !old || old.status?.value !== m.status?.value;
    });
    if (changed) {
      _llmModels = newModels;
      renderModelCards();
      setTimeout(_updateModelPerf, 100);
    }
  }).catch(() => {});
}

// Last-known perf values (gen/ppt/ts) for the card with this safeModelId,
// or dashes when the active llama model doesn't match (suffix like "(sleeping)" stripped).
function _llamaPerfSeed(safeModelId) {
  const ll = window._latestMetric && window._latestMetric.llama;
  if (!ll || !ll.model || ll.sleeping) return { gen: '—', ppt: '—', ts: '' };
  const safeId = ll.model.replace(/\s*\([^)]+\)$/, '').trim().replace(/[^a-z0-9]/gi, '_');
  if (safeId !== safeModelId) return { gen: '—', ppt: '—', ts: '' };
  return {
    gen: ll.tokens_per_second        != null ? ll.tokens_per_second.toFixed(1)        : '—',
    ppt: ll.prompt_tokens_per_second != null ? ll.prompt_tokens_per_second.toFixed(1) : '—',
    ts:  new Date().toLocaleTimeString(),
  };
}

function _updateModelPerf() {
  if (document.getElementById('llmTab').style.display === 'none') return;
  const ll = window._latestMetric && window._latestMetric.llama;
  if (!ll || !ll.model || ll.sleeping) return;
  const safeId = ll.model.replace(/\s*\([^)]+\)$/, '').trim().replace(/[^a-z0-9]/gi, '_');
  const genEl = document.getElementById(`perf-gen-${safeId}`);
  if (!genEl) return; // card not rendered yet (still loading)
  const seed = _llamaPerfSeed(safeId);
  const pptEl = document.getElementById(`perf-ppt-${safeId}`);
  const tsEl  = document.getElementById(`perf-ts-${safeId}`);
  genEl.textContent = seed.gen;
  pptEl && (pptEl.textContent = seed.ppt);
  tsEl  && (tsEl.textContent  = seed.ts);
}

function startPerfRefresh() {
  if (_perfTimer) clearInterval(_perfTimer);
  if (_cardPollTimer) clearInterval(_cardPollTimer);
  _updateModelPerf();
  _perfTimer     = setInterval(_updateModelPerf, 30000);
  _cardPollTimer = setInterval(_pollModelCards, 5000);
}

function stopPerfRefresh() {
  if (_perfTimer)     { clearInterval(_perfTimer);     _perfTimer     = null; }
  if (_cardPollTimer) { clearInterval(_cardPollTimer); _cardPollTimer = null; }
}

let _llmSectionsInited = false;

function _initLLMSections() {
  if (_llmSectionsInited) return;
  _llmSectionsInited = true;

  // Models — expanded
  document.getElementById('secModels').classList.remove('collapsed');
  // Download — collapsed
  document.getElementById('secDownload').classList.add('collapsed');
  // Cache — collapsed
  document.getElementById('secCache').classList.add('collapsed');
  // Trending — expanded
  document.getElementById('secTrending').classList.remove('collapsed');

  // Open server log panel
  const panel = document.getElementById('serverLogPanel');
  if (panel) {
    panel.style.display = '';
    _logPanelOpen = true;
    startLogStream();
  }
}

function toggleSection(id) {
  document.getElementById(id).classList.toggle('collapsed');
}

