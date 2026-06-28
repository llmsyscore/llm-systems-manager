// ---------------------------------------------------------------------------
// LM Studio metrics — from agent
// ---------------------------------------------------------------------------
let _lmsMetrics = {};

// Normalize for matching: replace slashes + hyphens with underscores, lowercase.
// Keep the @quant suffix so q4_k_m != iq4_xs.
function normId(id) { return (id || '').replace(/[\/\-]/g, '_').toLowerCase(); }

const lmsRamChartCtx = document.getElementById('lmsRamChart')?.getContext('2d');
const lmsRamChart = lmsRamChartCtx ? new Chart(lmsRamChartCtx, {
  type: 'line',
  data: { datasets: [{ label: 'RAM %', data: [], borderColor: '#7af', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3 }] },
  options: { animation: false, responsive: true, maintainAspectRatio: false, interaction: _sparkInteraction, scales: { x: { type: 'time', display: false }, y: { min: 0, max: 100, display: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 }, callback: v => v + '%' } } }, plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts } }
}) : null;

const lmsCpuChartCtx = document.getElementById('lmsCpuChart')?.getContext('2d');
const lmsCpuChart = lmsCpuChartCtx ? new Chart(lmsCpuChartCtx, {
  type: 'line',
  data: { datasets: [{ label: 'CPU %', data: [], borderColor: '#e05', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3 }] },
  options: { animation: false, responsive: true, maintainAspectRatio: false, interaction: _sparkInteraction, scales: { x: { type: 'time', display: false }, y: { min: 0, max: 100, display: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 }, callback: v => v + '%' } } }, plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts } }
}) : null;

const lmsNetChartCtx = document.getElementById('lmsNetChart')?.getContext('2d');
const lmsNetChart = lmsNetChartCtx ? new Chart(lmsNetChartCtx, {
  type: 'line',
  data: { datasets: [
    { label: 'Out', data: [], borderColor: '#fa7', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3 },
    { label: 'In',  data: [], borderColor: '#4e9', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3 },
  ]},
  options: { animation: false, responsive: true, maintainAspectRatio: false, interaction: _sparkInteraction, scales: { x: { type: 'time', display: false }, y: { min: 0, display: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 } } } }, plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts } }
}) : null;

const ovLlamaChartCtx = document.getElementById('ovLlamaChart')?.getContext('2d');
const ovLlamaChart = ovLlamaChartCtx ? new Chart(ovLlamaChartCtx, {
  type: 'line',
  data: { datasets: [
    { label: 'Gen t/s',    data: [], borderColor: '#7af', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3 },
    { label: 'Prompt t/s', data: [], borderColor: '#fa7', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3 },
  ]},
  options: { animation: false, responsive: true, maintainAspectRatio: false, interaction: _sparkInteraction, scales: { x: { type: 'time', display: false }, y: { min: 0, display: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 } } } }, plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts } }
}) : null;

function _fmtBytes(b) {
  if (!b) return '—';
  if (b > 1e9) return (b / 1e9).toFixed(1) + ' GB';
  return (b / 1e6).toFixed(0) + ' MB';
}

function _setEl(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? '—';
}

// True when an LM Studio SUB-tab is the active view (the only place the LMS
// cards + log live). Used to gate LMS polls so agent-scoped pages don't fire
// cross-provider calls (issue #115).
function _lmsLogViewActive() {
  const t = _activeTab;
  const ds = _subTabState && _subTabState['dashboard'];
  const ls = _subTabState && _subTabState['llm'];
  return (t === 'dashboard' && ds === 'lmstudio') || (t === 'llm' && ls === 'lmstudio');
}
// Metrics also feed the LLM Overall tab's mirrored LMS cards, so they're
// needed there too — the log panel is not, hence the separate predicate.
function _lmsMetricsViewActive() {
  return _activeTab === 'overall' || _lmsLogViewActive();
}

async function fetchLMStudioMetrics() {
  if (document.hidden) return;
  // Skip when the user isn't looking at LM Studio data. The payload feeds:
  //  • LLM Overall tab (mirrored LMS cards)
  //  • Dashboard tab → LM Studio sub-tab
  //  • LLM Control tab → LM Studio sub-tab
  // On every other tab/sub-tab combination this fetch is pure waste.
  // Explicit user actions (start/stop/load/unload model, log open, …)
  // still call this directly via setTimeout — those paths bypass the gate
  // intentionally because the user just triggered a state change that
  // they're about to look at.
  if (!_lmsMetricsViewActive()) return;
  const _lk = _agentClaimKey('fetchLMStudioMetrics', 'lms');
  if (!_claim(_lk)) return;
  try {
    const d = await _fetchT('/api/lmstudio/metrics', {}, 10000).then(r => r.json());
    _lmsMetrics = d;

    const sys    = d.system || {};
    const ps     = d.ps || [];
    const models = d.models || [];
    const ram    = sys.ram || {};
    const net    = sys.net || {};
    const cpu    = sys.cpu_per_core || [];
    const online = d.agent_online === true;
    const ts     = d.ts ? new Date(d.ts) : new Date();

    // Active model — from ps output (most reliable for status)
    const activePs = ps.find(p => p.status && !['IDLE','STOPPED'].includes(p.status));
    const activeData = activePs || d.active || null;

    // Agent badge updates — LMS-scoped only. Earlier this used a blanket
    // .agent-badge selector, which also stomped llama-side badges
    // (ov-llama-badge, llamaCtrlBadge) every 6s and caused them to
    // flicker between the LMS-shaped text and the llama text written by
    // pollServerState.
    ['lms-dash-badge'].forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      el.className = `status ${online ? 'status--ok' : 'status--crit'}`;
      el.innerHTML = '<span class="status__dot"></span>' + (online ? 'online' : 'offline');
    });

    // ---- LM Studio Dashboard sub-tab ----
    // Active model
    if (activeData) {
      _setEl('lms-active-model',    activeData.identifier || activeData.model || '—');
      _setEl('lms-active-status',   activeData.status || '—');
      _setEl('lms-active-size',     activeData.size   || '—');
      _setEl('lms-active-ctx',      activeData.context ? activeData.context.toLocaleString() : '—');
      _setEl('lms-active-parallel', activeData.parallel ?? '—');
      _setEl('lms-active-device',   activeData.device || '—');
    }

    // CPU
    _setEl('lms-cpu-total', sys.cpu_total != null ? sys.cpu_total.toFixed(1) + '%' : '—');
    if (lmsCpuChart && sys.cpu_total != null) pushPoint(lmsCpuChart, ts, sys.cpu_total);
    if (cpu.length && document.getElementById('lmsCoreGrid')) {
      document.getElementById('lmsCoreGrid').innerHTML = cpu.map((pct, i) => {
        const glowClass = pct >= 90 ? ' crit' : pct >= 70 ? ' warn' : '';
        const col = pct >= 90 ? 'color:var(--crit)' : pct >= 70 ? 'color:var(--note)' : '';
        return `<div class="core${glowClass}"><div class="sub">C${i}</div><div class="pct" style="${col}">${pct.toFixed(0)}%</div></div>`;
      }).join('');
    }

    // RAM
    const ramPct = ram.percent != null ? ram.percent.toFixed(1) + '%' : '—';
    _setEl('lms-ram-pct',  ramPct);
    _setEl('lms-ram-sub',  ram.used_bytes ? _fmtBytes(ram.used_bytes) + ' used / ' + _fmtBytes(ram.total_bytes) + ' total' : '—');
    _setEl('lms-swap-used', sys.swap?.used_bytes ? _fmtBytes(sys.swap.used_bytes) : '—');
    _setEl('lms-ram-avail', ram.available_bytes  ? _fmtBytes(ram.available_bytes) : '—');
    if (lmsRamChart && ram.percent != null) pushPoint(lmsRamChart, ts, ram.percent);

    // Network
    const mbSent = net.bytes_sent_per_sec != null ? (net.bytes_sent_per_sec / 1048576).toFixed(2) : '—';
    const mbRecv = net.bytes_recv_per_sec != null ? (net.bytes_recv_per_sec / 1048576).toFixed(2) : '—';
    _setEl('lms-net-sent', mbSent);
    _setEl('lms-net-recv', mbRecv);
    if (lmsNetChart && net.bytes_sent_per_sec != null) {
      pushDual(lmsNetChart, ts, net.bytes_sent_per_sec / 1048576, net.bytes_recv_per_sec / 1048576);
    }

    // Disk
    const diskEl = document.getElementById('lmsDiskList');
    if (diskEl && sys.disk) {
      diskEl.innerHTML = sys.disk
        .filter(d => d.mountpoint && d.total_bytes > 1e8)
        .map(d => {
          const fillColor = d.percent > 90 ? 'var(--crit)' : d.percent > 75 ? 'var(--warn)' : 'var(--accent-2)';
          return `<div class="disk-row">
            <span style="min-width:100px;color:var(--fg-muted);font-size:0.8em">${_esc(d.mountpoint)}</span>
            <div class="disk-bar"><div class="disk-fill" style="width:${Number(d.percent)}%;background:${fillColor};"></div></div>
            <span>${d.percent.toFixed(1)}%</span>
          </div>`;
        }).join('');
    }

    // Model list — use ps for status, models for IDs
    const modelListEl = document.getElementById('lmsDashModelList');
    if (modelListEl) {
      const displayModels = models.filter(m => !m.id.toLowerCase().includes('embed'));
      const psOnly2 = ps.filter(p => {
        const pn = normId(p.identifier || p.model);
        return !displayModels.some(m => normId(m.id) === pn) &&
               !(p.identifier || p.model || '').toLowerCase().includes('embed');
      });
      const listRows = [
        ...displayModels.map(m => {
          const pr = ps.find(p => normId(p.identifier || p.model) === normId(m.id));
          return { name: m.id, status: pr?.status || 'IDLE', size: pr?.size || '' };
        }),
        ...psOnly2.map(p => ({ name: p.model || p.identifier, status: p.status || 'IDLE', size: p.size || '' })),
      ];
      modelListEl.innerHTML = listRows.map(r => {
        const st  = (r.status || 'IDLE').toUpperCase();
        const isOn = !['IDLE','STOPPED',''].includes(st);
        const col  = isOn ? 'var(--ok)' : 'var(--fg-dim)';
        const extra = r.size ? ` · ${r.size}` : '';
        return `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--bg-card);">
          <span style="font-size:0.82em;color:var(--fg);font-family:monospace;word-break:break-all;">${_esc(r.name)}</span>
          <span style="font-size:0.75em;color:${col};white-space:nowrap;margin-left:8px;">${_esc(st)}${_esc(extra)}</span>
        </div>`;
      }).join('') || '<div style="color:var(--fg-dim);font-size:0.85em;">No models</div>';
    }

    // LLM Control — LMS model cards
    renderLMSModelCards(ps, models);

    // LMS server control status
    // Use models presence as authoritative "server is up" indicator —
    // lms server status output format varies and can report false negatives.
    const srvOn = d.server?.on === true || (models && models.length > 0);
    _setLmsBtns(online && srvOn);
    _setEl('lmsCtrlStatus', srvOn ? `Server ON · port ${d.server?.port || 1235}` : 'Server OFF');
    document.querySelectorAll('#lmsCtrlBadge').forEach(el => {
      el.className = `status ${online ? 'status--ok' : 'status--crit'}`;
      el.innerHTML = '<span class="status__dot"></span>' + (online ? 'Agent online' : 'Agent offline');
    });

    // Update header LMS state pill
    const lmsBanner = document.getElementById('lmsStateBanner');
    const lmsText   = document.getElementById('lmsStateText');
    if (lmsBanner && lmsText) {
      if (!online) {
        lmsBanner.className = 'state-banner state-unknown';
        lmsText.textContent  = 'LMS · offline';
      } else {
        // Find any loaded model (in ps) regardless of active/idle
        const loadedRow = ps.find(p => p.status && p.status !== 'STOPPED') || d.active || null;
        const modelShort = loadedRow
          ? (loadedRow.model || loadedRow.identifier || '').split('@')[0].split('_').slice(-2).join('_') || 'model'
          : null;
        const lmsStatus = loadedRow
          ? (['IDLE',''].includes((loadedRow.status||'').toUpperCase()) ? 'Idle' : 'Active')
          : null;
        if (loadedRow) {
          lmsBanner.className = lmsStatus === 'Active' ? 'state-banner state-awake' : 'state-banner state-sleeping';
          lmsText.textContent  = `LMS · ${lmsStatus} · ${modelShort}`;
        } else {
          lmsBanner.className = 'state-banner state-sleeping';
          lmsText.textContent  = 'LMS · no model loaded';
        }
      }
    }

    // Overall tab is fleet-aggregated (PR4) — driven by fetchOverallMetrics
    // (kicked from fetchMetrics while the tab is visible), not this per-agent
    // LMS payload.

    // ---- LMS Dashboard card accent borders ----
    // lms-models: server running = ok, no models but online = warn, offline = off
    _dashSetStatus('lms-models', !online ? 'dash-off' : (models && models.length > 0 ? 'dash-ok' : 'dash-warn'));
    // lms-active: active model processing = ok, loaded/idle = warn, none = off
    _dashSetStatus('lms-active', !online ? 'dash-off' : (activeData
      ? (['IDLE',''].includes((activeData.status || '').toUpperCase()) ? 'dash-warn' : 'dash-ok')
      : 'dash-off'));
    // lms-cpu: cpu_total thresholds
    {
      const _lct = sys.cpu_total;
      _dashSetStatus('lms-cpu', !online ? 'dash-off' : (_lct != null ? (_lct >= 90 ? 'dash-crit' : _lct >= 75 ? 'dash-warn' : 'dash-ok') : 'dash-off'));
    }
    // lms-ram: ram.percent thresholds
    {
      const _lrp = ram.percent;
      _dashSetStatus('lms-ram', !online ? 'dash-off' : (_lrp != null ? (_lrp >= 90 ? 'dash-crit' : _lrp >= 75 ? 'dash-warn' : 'dash-ok') : 'dash-off'));
    }
    // lms-network / lms-disk: ok when data present
    _dashSetStatus('lms-network', !online ? 'dash-off' : (sys.net ? 'dash-ok' : 'dash-off'));
    _dashSetStatus('lms-disk',    !online ? 'dash-off' : (sys.disk ? 'dash-ok' : 'dash-off'));

  } catch(e) {
    console.warn('fetchLMStudioMetrics:', e);
  } finally {
    _release(_lk);
    syncBorrowedCards();
  }
}

function onLmsModelSortChange(v) {
  if (!VALID_MODEL_SORTS.includes(v)) return;
  if (typeof layout !== 'object' || !layout) layout = {};
  layout.lmsModelSort = v;
  try { saveLayout(); } catch (_) {}
  // Re-render with whatever ps/models the last fetch produced.
  if (_lmsLastPs || _lmsLastModels) renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
}
function _currentLmsModelSort() {
  const v = (layout && layout.lmsModelSort) || 'group_by_author';
  return VALID_MODEL_SORTS.includes(v) ? v : 'group_by_author';
}
let _lmsLastPs = null, _lmsLastModels = null, _lmsAliasesHydrated = false;

function renderLMSModelCards(ps, models) {
  const container = document.getElementById('lmsModelCards');
  if (!container) return;
  // Skip while an inline alias edit is in progress — the 6s metrics
  // poll would otherwise wipe the input mid-keystroke. We still update
  // the cached ps/models below so the next render after commit reflects
  // the latest state.
  if (container.querySelector('.model-card-name input')) {
    _lmsLastPs = ps; _lmsLastModels = models;
    return;
  }

  // One-shot alias hydration: refreshLLMTab loads /api/llm/aliases on the
  // llama tab, but the user may land on the LMS tab first. Without this,
  // aliases set previously wouldn't render until they pop over to llama.
  if (!_lmsAliasesHydrated) {
    _lmsAliasesHydrated = true;
    fetch('/api/llm/aliases').then(r => r.json()).then(ar => {
      _llmAliases = (ar && typeof ar === 'object') ? ar : (_llmAliases || {});
      renderLMSModelCards(_lmsLastPs || ps, _lmsLastModels || models);
    }).catch(() => {});
  }

  // Cache the inputs so the sort dropdown can re-render without an extra fetch.
  _lmsLastPs = ps;
  _lmsLastModels = models;
  const sortSel = document.getElementById('lmsModelSortSel');
  if (sortSel && sortSel.value !== _currentLmsModelSort()) sortSel.value = _currentLmsModelSort();

  // Filter out embedding models
  const displayModels = (models || []).filter(m => !m.id.toLowerCase().includes('embed'));

  // Any ps rows not matched by a /v1/models entry (edge case)
  const psOnly = (ps || []).filter(p => {
    const pn = normId(p.identifier || p.model);
    return !displayModels.some(m => normId(m.id) === pn);
  }).filter(p => !(p.identifier || p.model || '').toLowerCase().includes('embed'));

  const allEntries = [
    ...displayModels.map(m => ({ modelId: m.id, psRow: ps.find(p => normId(p.identifier || p.model) === normId(m.id)) || null })),
    ...psOnly.map(p => ({ modelId: p.identifier || p.model, psRow: p })),
  ];

  if (!allEntries.length) {
    container.innerHTML = '<div style="color:var(--fg-dim);font-size:0.85em;">Waiting for agent...</div>';
    return;
  }

  // Apply the same grouping/sorting the llama cards use. Build the status
  // lookup the helper expects, then map sorted IDs back to entries.
  const entryById   = new Map(allEntries.map(e => [e.modelId, e]));
  const statusLookup = {};
  for (const e of allEntries) {
    const raw = ((e.psRow?.status) || '').toUpperCase();
    const loaded     = e.psRow != null;
    const processing = loaded && !['IDLE', 'STOPPED', ''].includes(raw);
    statusLookup[e.modelId] = { value: processing ? 'loaded' : (loaded ? 'sleeping' : 'unloaded') };
  }
  const groups = _buildSortedGroups([...entryById.keys()], _currentLmsModelSort(), statusLookup, _authorOfLms);

  const renderCard = (modelId) => {
    const { psRow } = entryById.get(modelId) || {};
    const rawStatus    = ((psRow?.status) || '').toUpperCase();
    const isLoaded     = psRow != null;
    const isProcessing = isLoaded && !['IDLE', 'STOPPED', ''].includes(rawStatus);

    let pillMod, pillLabel, cardClass;
    if (isProcessing) {
      pillMod = 'warn';   pillLabel = rawStatus === 'PROCESSINGPROMPT' ? 'Processing' : rawStatus.charAt(0) + rawStatus.slice(1).toLowerCase();
      cardClass = 'loaded';       // green — actively inferring
    } else if (isLoaded) {
      pillMod = 'ok';     pillLabel = 'Loaded';
      cardClass = 'idle-loaded';  // yellow — loaded but idle
    } else {
      pillMod = 'muted';  pillLabel = 'Unloaded';
      cardClass = '';
    }

    const chips = psRow ? [
      psRow.size                  && { k: 'size',     v: psRow.size },
      psRow.context               && { k: 'ctx',      v: Number(psRow.context).toLocaleString() },
      psRow.parallel != null      && { k: 'parallel', v: psRow.parallel },
      psRow.device                && { k: 'dev',      v: psRow.device },
    ].filter(Boolean).map(c =>
      `<span class="param-chip"><span class="pk">${_esc(String(c.k))}</span><span class="pv">${_esc(String(c.v))}</span></span>`
    ).join('') : '';

    const mid = _esc(modelId);
    return `
    <div class="model-card ${cardClass}" data-id="${mid}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div class="model-card-name" title="Click to edit alias (blank = use Model ID)" data-lmsact="rename" data-id="${mid}">${_esc(aliasOrShort(modelId))}</div>
        <span class="status status--${pillMod}">${_esc(pillLabel)}</span>
      </div>
      <div class="model-card-params">${chips}</div>
      <div class="model-card-actions">
        ${!isLoaded ? `<button class="btn btn-stone-muted-gradient" data-lmsact="load"   data-id="${mid}">▶ Load</button>` : ''}
        ${isLoaded  ? `<button class="btn btn-slate-muted-gradient" data-lmsact="unload" data-id="${mid}">⏹ Unload</button>` : ''}
        ${isLoaded  ? `<button class="btn btn-amber-muted-gradient"  data-lmsact="reload" data-id="${mid}">↺ Reload</button>` : ''}
      </div>
    </div>`;
  };

  container.innerHTML = groups.map(g => {
    const cards = g.ids.map(renderCard).join('');
    if (!g.header) return cards;
    return `<div class="model-group-header" style="grid-column:1/-1;">
      <span>${_esc(g.header)}</span>
      <span class="rule"></span>
      <span class="count">${g.ids.length}</span>
    </div>${cards}`;
  }).join('');

  if (!container._actBound) {
    container._actBound = true;
    container.addEventListener('click', ev => {
      const nameDiv = ev.target.closest('[data-lmsact="rename"]');
      if (nameDiv) { ev.stopPropagation(); startLmsCardRename(ev, nameDiv.dataset.id); return; }
      const btn = ev.target.closest('button[data-lmsact]');
      if (!btn) return;
      const id  = btn.dataset.id;
      const act = btn.dataset.lmsact;
      if (act === 'load')   lmsLoad(id);
      else if (act === 'unload') lmsUnload(id);
      else if (act === 'reload') lmsReload(id);
    });
  }
}

// Mirror of startCardRename but scoped to the LMS card. Reuses the same
// /api/llm/aliases store + _llmAliases cache. Operations on LMS models
// (load/unload/reload) always key off the underlying model ID, never
// the alias.
function startLmsCardRename(evt, modelId) {
  const nameDiv = evt.target.closest('[data-lmsact="rename"]');
  if (!nameDiv || nameDiv.querySelector('input')) return;
  const input = document.createElement('input');
  input.type  = 'text';
  input.value = (_llmAliases && _llmAliases[modelId]) || '';
  input.placeholder = shortName(modelId);
  input.title = 'Editing alias (blank = use Model ID)';
  input.style.cssText = 'width:100%;background:var(--bg);border:1px solid var(--accent);border-radius:3px;color:var(--fg);font-size:0.85em;padding:2px 4px;box-sizing:border-box;';
  input.onclick = e => e.stopPropagation();

  let committed = false;
  // Pull the input out of the DOM before triggering a re-render so the
  // "skip while editing" guard in renderLMSModelCards (which checks for
  // .model-card-name input) doesn't bail on the commit pass.
  const _detach = () => { try { input.remove(); } catch (_) {} };
  const finish = async () => {
    if (committed) return;
    committed = true;
    const newAlias = _sanitizeAlias(input.value);
    const oldAlias = ((_llmAliases && _llmAliases[modelId]) || '').trim();
    _detach();
    if (newAlias === oldAlias) { renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []); return; }
    try {
      const r = await fetch('/api/llm/aliases', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({model_id: modelId, alias: newAlias}),
      }).then(r => r.json());
      if (r && r.aliases) _llmAliases = r.aliases;
      else if (newAlias) _llmAliases[modelId] = newAlias;
      else delete _llmAliases[modelId];
    } catch (_) {
      if (newAlias) _llmAliases[modelId] = newAlias;
      else delete _llmAliases[modelId];
    }
    renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
  };

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); finish(); }
    if (e.key === 'Escape') { committed = true; _detach(); renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []); }
  });
  input.addEventListener('blur', finish);

  nameDiv.textContent = '';
  nameDiv.appendChild(input);
  input.focus();
  input.select();
}

// ---------------------------------------------------------------------------
// LM Studio Server Control
// ---------------------------------------------------------------------------
let _lmsLogOpen  = false;
let _lmsLogTimer = null;

function startLmsLogRefresh() {
  fetchLmsLog();
  if (_lmsLogTimer) clearInterval(_lmsLogTimer);
  _lmsLogTimer = setInterval(fetchLmsLog, 8000);
}

function stopLmsLogRefresh() {
  if (_lmsLogTimer) { 
    clearInterval(_lmsLogTimer); 
    _lmsLogTimer = null; 
  }
  // Clean up any existing EventSource connection
  if (_dlEventSrc) { 
    try { 
      _dlEventSrc.close(); 
    } catch(_) {}
    _dlEventSrc = null; 
  }
}

let _lmsSectionsInited = false;

function _initLMSSections() {
  if (_lmsSectionsInited) return;
  _lmsSectionsInited = true;
  // Expand models, collapse download
  document.getElementById('lmsSecModels')?.classList.remove('collapsed');
  document.getElementById('lmsSecDownload')?.classList.add('collapsed');
  // Open log panel
  const panel = document.getElementById('lmsLogPanel');
  if (panel) {
    panel.style.display = '';
    _lmsLogOpen = true;
    startLmsLogRefresh();
  }
}

async function lmsServerAction(action) {
  const _lmsPrompts = {
    stop:    { title: 'Stop the LM Studio server?',    body: 'Any active inference will be interrupted.', label: 'Stop' },
    start:   { title: 'Start the LM Studio server?',   body: '',                                          label: 'Start' },
    restart: { title: 'Restart the LM Studio server?', body: 'Any active inference will be interrupted.', label: 'Restart' },
  };
  if (_lmsPrompts[action]) {
    const p = _lmsPrompts[action];
    const ok = await _themedConfirm({
      title:        p.title,
      bodyHtml:     p.body,
      confirmLabel: p.label,
      cancelLabel:  'Cancel',
    });
    if (!ok) return;
  }
  const statusEl = document.getElementById('lmsCtrlStatus');
  statusEl.style.color = 'var(--warn)';
  statusEl.textContent = action === 'status' ? 'Checking...' : `${action.charAt(0).toUpperCase() + action.slice(1)}ing...`;
  try {
    const r = await fetch(`/api/lmstudio/server/${action}`, {
      method: action === 'status' ? 'GET' : 'POST'
    }).then(r => r.json());
    statusEl.style.color = r.ok ? 'var(--ok)' : 'var(--crit)';
    statusEl.textContent = r.output?.trim().split('\n')[0] || (r.ok ? 'OK' : r.error || 'failed');
    if (_lmsLogOpen && r.output) {
      const box = document.getElementById('lmsLogBox');
      if (box) { box.textContent = r.output; box.scrollTop = box.scrollHeight; }
    }
    if (action !== 'status') setTimeout(fetchLMStudioMetrics, 2000);
  } catch(e) {
    statusEl.style.color = 'var(--crit)';
    statusEl.textContent = String(e);
  }
  setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = 'var(--fg-dim)'; }, 12000);
}

function toggleLmsLog() {
  const panel = document.getElementById('lmsLogPanel');
  _lmsLogOpen = !_lmsLogOpen;
  panel.style.display = _lmsLogOpen ? '' : 'none';
  if (_lmsLogOpen) startLmsLogRefresh();
  else stopLmsLogRefresh();
}

async function fetchLmsLog() {
  // Don't poll the LMS log from non-LMS views (e.g. the llama.cpp sub-tab) —
  // a leftover timer would otherwise hit /api/lmstudio/server/log there (#115).
  if (!_lmsLogViewActive()) return;
  const box = document.getElementById('lmsLogBox');
  if (!box) return;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  try {
    const r = await fetch('/api/lmstudio/server/log').then(r => r.json());
    const lines = r.lines || [];
    const text  = lines.length ? lines.join('\n') : (r.error || '(no log lines found)');
    if (box.textContent !== text) {
      box.textContent = text;
      // Always scroll on first load (empty box), otherwise only if already at bottom
      if (atBottom || box.textContent === '') box.scrollTop = box.scrollHeight;
    }
  } catch(e) {
    box.textContent = 'Error: ' + e;
  }
}

function popOutLmsLog() {
  const box = document.getElementById('lmsLogBox');
  const content = box ? box.textContent : '';
  const win = window.open('', 'lmslog', 'width=900,height=600,resizable=yes,scrollbars=yes,toolbar=no,menubar=no');
  if (!win) { alert('Pop-out blocked — allow pop-ups for this page.'); return; }
  win.document.write(`<!DOCTYPE html><html><head><title>LM Studio Server Log</title>
  <style>*{box-sizing:border-box;margin:0;padding:0;}body{background:#0a0a0a;color:#8a8;font-family:monospace;font-size:0.88em;display:flex;flex-direction:column;height:100vh;}
  #toolbar{background:var(--bg);border-bottom:1px solid var(--bg-card-alt);display:flex;align-items:center;gap:10px;padding:8px 12px;flex-shrink:0;}
  #toolbar span{color:var(--fg-dim);font-size:0.85em;}#log{flex:1;overflow-y:auto;padding:12px;white-space:pre-wrap;word-break:break-all;}</style>
  </head><body><div id="toolbar"><span>LM Studio Server Log</span></div>
  <div id="log">${content.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>
  <script>document.getElementById('log').scrollTop=document.getElementById('log').scrollHeight;<\/script>
  </body></html>`);
}

function fullscreenLmsLog() {
  const box = document.getElementById('lmsLogBox');
  if (!box) return;
  if (box.requestFullscreen) box.requestFullscreen();
  else if (box.webkitRequestFullscreen) box.webkitRequestFullscreen();
}

// LMS log resize handle
(function() {
  let dragging = false, startY = 0, startH = 0;
  document.addEventListener('DOMContentLoaded', () => {
    const handle = document.getElementById('lmsLogResizeHandle');
    const box    = document.getElementById('lmsLogBox');
    if (!handle || !box) return;
    handle.addEventListener('mousedown', e => {
      dragging = true; startY = e.clientY; startH = box.offsetHeight;
      document.body.style.userSelect = 'none'; e.preventDefault();
    });
    document.addEventListener('mousemove', e => {
      if (!dragging) return;
      box.style.height = Math.max(80, Math.min(window.innerHeight * 0.9, startH + (e.clientY - startY))) + 'px';
    });
    document.addEventListener('mouseup', () => { dragging = false; document.body.style.userSelect = ''; });
  });
})();

async function lmsLoad(modelId) {
  {
    const ok = await _themedConfirm({
      title:        `Load "${adminEsc(modelId)}" in LM Studio?`,
      bodyHtml:     '',
      confirmLabel: 'Load',
      cancelLabel:  'Cancel',
    });
    if (!ok) return;
  }
  if (!_actionClaim('lmsLoad:' + modelId)) return;
  try {
    const r = await _fetchT('/api/lmstudio/load', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 60000).then(r => r.json());
    if (!r.ok) alert('Load failed: ' + (r.error || JSON.stringify(r.response)));
    setTimeout(fetchLMStudioMetrics, 2000);
  } catch(e) {
    alert('Error: ' + e);
  } finally {
    _actionRelease('lmsLoad:' + modelId);
  }
}

async function lmsUnload(modelId) {
  {
    const ok = await _themedConfirm({
      title:        `Unload "${adminEsc(modelId)}"?`,
      bodyHtml:     '',
      confirmLabel: 'Unload',
      cancelLabel:  'Cancel',
    });
    if (!ok) return;
  }
  if (!_actionClaim('lmsUnload:' + modelId)) return;
  try {
    const r = await _fetchT('/api/lmstudio/unload', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 30000).then(r => r.json());
    if (!r.ok) alert('Unload failed: ' + (r.error || JSON.stringify(r.response)));
    setTimeout(fetchLMStudioMetrics, 2000);
  } catch(e) {
    alert('Error: ' + e);
  } finally {
    _actionRelease('lmsUnload:' + modelId);
  }
}

async function lmsReload(modelId) {
  {
    const ok = await _themedConfirm({
      title:        `Reload "${adminEsc(modelId)}"?`,
      bodyHtml:     'This will unload then reload the model.',
      confirmLabel: 'Reload',
      cancelLabel:  'Cancel',
    });
    if (!ok) return;
  }
  if (!_actionClaim('lmsReload:' + modelId)) return;
  try {
    // Unload
    const ur = await _fetchT('/api/lmstudio/unload', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 30000).then(r => r.json());
    if (!ur.ok) { alert('Reload failed on unload: ' + (ur.error || JSON.stringify(ur))); return; }

    // Poll until unloaded (up to 20s). Network errors don't count as "still loaded".
    let unloaded = false;
    let netErrors = 0;
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 1000));
      try {
        await fetchLMStudioMetrics();
        const d = await _fetchT('/api/lmstudio/metrics', {}, 8000).then(r => r.json());
        const ps = d.ps || [];
        if (!ps.find(p => (p.identifier === modelId || p.model === modelId))) {
          unloaded = true; break;
        }
      } catch(e) {
        netErrors++;
        if (netErrors > 5) { alert('Reload: lost connection to backend — aborting.'); return; }
      }
    }
    if (!unloaded) { alert('Reload: model did not unload within 20 seconds.'); return; }

    // Reload
    const lr = await _fetchT('/api/lmstudio/load', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 60000).then(r => r.json());
    if (!lr.ok) alert('Reload failed on load: ' + (lr.error || JSON.stringify(lr.response)));
    setTimeout(fetchLMStudioMetrics, 2000);
  } catch(e) {
    alert('Reload error: ' + e);
  } finally {
    _actionRelease('lmsReload:' + modelId);
  }
}

async function lmsDownloadModel() {
  const modelId = document.getElementById('lmsDlModel')?.value?.trim();
  if (!modelId) return;
  const logEl = document.getElementById('lmsDlLog');
  if (logEl) logEl.textContent = `Requesting download of ${modelId}...`;
  try {
    const r = await fetch('/api/lmstudio/download', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }).then(r => r.json());
    if (logEl) logEl.textContent = JSON.stringify(r, null, 2);
  } catch(e) {
    if (logEl) logEl.textContent = 'Error: ' + e;
  }
}
