// ---------------------------------------------------------------------------
// LLM Overall tab — fleet aggregates (PR4)
//
// Reads /api/fleet/<provider>/aggregate (server-side rollups) rather than a
// single primary host's sample. No picker here — this tab is the whole-fleet
// view. Live refresh piggybacks on fetchMetrics (when the tab is visible).
// ---------------------------------------------------------------------------
let _ovHistoryBackfilled = false;
async function fetchOverallMetrics() {
  try {
    // One-time history backfill for the fleet llama TPS chart — only when the
    // Overall tab is actually loaded, so the LMS dashboard makes no llama calls
    // (#142). Flag set synchronously to prevent a concurrent re-entry backfill.
    if (!_ovHistoryBackfilled && typeof loadOverallHistory === 'function') {
      _ovHistoryBackfilled = true;
      await loadOverallHistory();
    }
    let llama = null, lms = null;
    [llama, lms] = await Promise.all([
      fetch('/api/fleet/llama/aggregate').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/fleet/lms/aggregate').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    updateOverallLlamaFleet(llama);
    updateOverallLmsFleet(lms);
    updateOverallFleet(llama, lms);
    const el = document.getElementById('overallLastUpdate');
    if (el) el.textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch (_) {}
}

// Enable/disable llama.cpp server control buttons.
// When down: only Start is enabled; Stop/Restart/Status are dimmed.
function _setLlamaBtns(serverUp) {
  const ids = ['llamaBtnStop', 'llamaBtnRestart', 'llamaBtnStatus'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = !serverUp;
  });
  const start = document.getElementById('llamaBtnStart');
  if (start) start.disabled = serverUp; // Start only available when down
}

// Enable/disable LMS server control buttons.
// When agent offline or server down: only Start enabled.
function _setLmsBtns(serverUp) {
  const ids = ['lmsBtnStop', 'lmsBtnRestart', 'lmsBtnStatus'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = !serverUp;
  });
  const start = document.getElementById('lmsBtnStart');
  if (start) start.disabled = serverUp;
}

function _ovSetStatus(cardId, cls) {
  const el = document.querySelector(`#overallGrid [data-card="${cardId}"]`);
  if (!el) return;
  el.classList.remove('ov-ok','ov-warn','ov-crit','ov-off');
  if (cls) el.classList.add(cls);
}

// Applies severity accent border to Dashboard (cardGrid + lmsCardGrid) cards.
// cls: 'dash-ok' | 'dash-warn' | 'dash-crit' | 'dash-off'
function _dashSetStatus(cardId, cls) {
  const el = document.querySelector(`#cardGrid [data-card="${cardId}"], #lmsCardGrid [data-card="${cardId}"], #managerCardGrid [data-card="${cardId}"]`);
  if (!el) return;
  el.classList.remove('dash-ok','dash-warn','dash-crit','dash-off');
  if (cls) el.classList.add(cls);
}

function updateOverallLlamaFleet(agg) {
  if (!agg) {
    ['ov-llama-tps','ov-llama-pps','ov-gpu-temp','ov-gpu-vram','ov-gpu-power','ov-llama-active-n'].forEach(id => _setEl(id, '—'));
    _setEl('ov-llama-agents', '—');
    _setEl('ov-llama-awake', 'awake —');
    _setEl('ov-llama-models-n', 'models —');
    const listEl = document.getElementById('ov-llama-active-list');
    if (listEl) listEl.textContent = '—';
    ['ov-llama-fleet','ov-llama-gpu','ov-llama-active','ov-llama-chart'].forEach(c => _ovSetStatus(c, 'ov-off'));
    return;
  }
  const online = agg.agent_count_online || 0;
  const total  = agg.agent_count_total  || 0;
  const awake  = agg.awake_agent_count  || 0;
  const tp  = agg.throughput || {};
  const gpu = agg.gpu || {};

  // Fleet card
  _setEl('ov-llama-agents', `${online}/${total} online`);
  _setEl('ov-llama-tps', (tp.total_tps || 0).toFixed(1));
  _setEl('ov-llama-pps', (tp.total_pps || 0).toFixed(1));
  _setEl('ov-llama-awake', `awake ${awake}`);
  _setEl('ov-llama-models-n', `models ${agg.active_model_count || 0}`);
  _ovSetStatus('ov-llama-fleet', online > 0 ? (awake > 0 ? 'ov-ok' : 'ov-warn') : 'ov-off');

  // GPU card — max temp / max vram% / total power across the fleet
  const t = gpu.max_temp_c;
  _setEl('ov-gpu-temp',  t > 0 ? t.toFixed(1) + '°C' : '—');
  _setEl('ov-gpu-vram',  gpu.max_vram_pct > 0 ? gpu.max_vram_pct.toFixed(1) + '%' : '—');
  _setEl('ov-gpu-power', gpu.total_power_watts > 0 ? gpu.total_power_watts.toFixed(0) + ' W' : '—');
  _ovSetStatus('ov-llama-gpu', t > 0 ? (t >= 85 ? 'ov-crit' : t >= 70 ? 'ov-warn' : 'ov-ok') : 'ov-off');

  // Active models card
  const models = agg.active_models || [];
  _setEl('ov-llama-active-n', String(agg.active_model_count || 0));
  const listEl = document.getElementById('ov-llama-active-list');
  if (listEl) listEl.textContent = models.length ? models.join(', ') : '—';
  _ovSetStatus('ov-llama-active', models.length ? 'ov-ok' : 'ov-off');

  // Throughput chart — fleet totals
  if (ovLlamaChart) pushDual(ovLlamaChart, new Date(), tp.total_tps || 0, tp.total_pps || 0);
  _ovSetStatus('ov-llama-chart', online > 0 ? 'ov-ok' : 'ov-off');
}

function updateOverallLmsFleet(agg) {
  if (!agg) {
    ['ov-lms-servers','ov-lms-loaded'].forEach(id => _setEl(id, '—'));
    _setEl('ov-lms-agents', '—');
    _setEl('ov-lms-busy', 'busy —');
    _setEl('ov-lms-procs', 'processes —');
    _ovSetStatus('ov-lms-fleet', 'ov-off');
    return;
  }
  const online = agg.agent_count_online || 0;
  const total  = agg.agent_count_total  || 0;
  _setEl('ov-lms-agents', `${online}/${total} online`);
  _setEl('ov-lms-servers', String(agg.server_on_count || 0));
  _setEl('ov-lms-loaded',  String(agg.loaded_model_count_total || 0));
  _setEl('ov-lms-busy',    `busy ${agg.busy_agent_count || 0}`);
  _setEl('ov-lms-procs',   `processes ${agg.process_count_total || 0}`);
  _ovSetStatus('ov-lms-fleet', online > 0 ? ((agg.busy_process_count_total || 0) > 0 ? 'ov-ok' : 'ov-warn') : 'ov-off');
}

// Combined whole-fleet overview card (llama + lms).
function updateOverallFleet(llama, lms) {
  const lOn   = (llama && llama.agent_count_online) || 0;
  const mOn   = (lms   && lms.agent_count_online)   || 0;
  const models = (llama && llama.active_model_count) || 0;
  const power  = (llama && llama.gpu && llama.gpu.total_power_watts) || 0;
  _setEl('ov-fleet-agents', String(lOn + mOn));
  _setEl('ov-fleet-models', String(models));
  _setEl('ov-fleet-power',  power > 0 ? power.toFixed(0) + ' W' : '—');
  _ovSetStatus('ov-fleet', (lOn + mOn) > 0 ? 'ov-ok' : 'ov-off');
}
