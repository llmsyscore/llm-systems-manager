// ---------------------------------------------------------------------------
// LLM Overall tab — fleet band (#565)
//
// Renders the fixed fleet-status band (toplines, hero append, provider
// tiles, agents strip, alerts strip) from /api/fleet/<p>/aggregate via the
// pure transforms in js/lib/overall-view.js (window.OV). Live refresh
// piggybacks on fetchMetrics while the tab is visible; the hero's 24h
// backfill runs on tab entry via loadOverallHistory (#142, #506).
// ---------------------------------------------------------------------------

// Rolling gen/prompt rate windows per provider tile (#591); browser-clock
// timestamps throughout, like the llama card's trackers.
const _OV_TILE_PEAK_WINDOW_MS = LMPeaks.RATE_WINDOW_MS;
const _ovTilePeaks = {};
['llama', 'lms', 'vllm'].forEach(k => {
  _ovTilePeaks[k] = {
    gen: LMPeaks.makeTracker(_OV_TILE_PEAK_WINDOW_MS),
    prompt: LMPeaks.makeTracker(_OV_TILE_PEAK_WINDOW_MS),
  };
});

// Seeds the tile trackers from fleet history rows on the browser clock;
// only rows landing inside the peak window are pushed.
let _ovTileSeedRows = null;
function ovSeedTilePeaks(rows) {
  if (!Array.isArray(rows) || !rows.length) return;
  const now = Date.now();
  const clk = LMPeaks.rowClock(rows, now);
  const cutoff = now - _OV_TILE_PEAK_WINDOW_MS;
  let start = rows.length;
  while (start > 0 && clk(rows[start - 1].ts) >= cutoff) start--;
  for (const r of rows.slice(start)) {
    const t = clk(r.ts);
    _ovTilePeaks.llama.gen.push(t, r.llama_tps);
    _ovTilePeaks.llama.prompt.push(t, r.llama_pps);
    _ovTilePeaks.lms.gen.push(t, r.lms_tps);
    _ovTilePeaks.lms.prompt.push(t, r.lms_pps);
    _ovTilePeaks.vllm.gen.push(t, r.vllm_tps);
    _ovTilePeaks.vllm.prompt.push(t, r.vllm_pps);
  }
}

// Live aggregates → tracker pushes; returns OV.tiles' rates shape
// ({llama:{gen,prompt: rateStat},…), seeding once per fresh hero backfill.
function _ovTilePeaksPush(llama, lms, vllm) {
  if (typeof _ovHeroRows !== 'undefined' && _ovHeroRows && _ovHeroRows !== _ovTileSeedRows) {
    _ovTileSeedRows = _ovHeroRows;
    ovSeedTilePeaks(_ovHeroRows);
  }
  const now = Date.now();
  const aggs = { llama, lms, vllm };
  const out = {};
  for (const k of ['llama', 'lms', 'vllm']) {
    const tp = (aggs[k] && aggs[k].throughput) || {};
    _ovTilePeaks[k].gen.push(now, tp.total_tps);
    _ovTilePeaks[k].prompt.push(now, tp.total_pps);
    const win = tp.window || null;
    out[k] = { gen: LMPeaks.rateStat(_ovTilePeaks[k].gen, tp.total_tps, now, win && win.gen),
               prompt: LMPeaks.rateStat(_ovTilePeaks[k].prompt, tp.total_pps, now, win && win.prompt) };
  }
  return out;
}

// Energy summary cache — refreshed at most once a minute from the band paint.
let _ovEnergy = null;
let _ovEnergyTs = 0;
async function _ovRefreshEnergy() {
  if (Date.now() - _ovEnergyTs < 60000) return;
  _ovEnergyTs = Date.now();
  try {
    _ovEnergy = await fetch('/api/energy/summary?days=1').then(r => r.json());
  } catch (_) { _ovEnergy = null; }
}

async function fetchOverallMetrics() {
  try {
    _ovRefreshEnergy();
    let llama = null, lms = null, vllm = null;
    [llama, lms, vllm] = await Promise.all([
      fetch('/api/fleet/llama/aggregate').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/fleet/lms/aggregate').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/fleet/vllm/aggregate').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    _ovPaintBand(llama, lms, vllm);
    if (typeof ovHeroChart !== 'undefined' && ovHeroChart && (llama || lms || vllm)) {
      const tp = (llama && llama.throughput) || {}, vtp = (vllm && vllm.throughput) || {};
      const ltp = (lms && lms.throughput) || {};
      pushDual(ovHeroChart, new Date(),
        (tp.total_tps || 0) + (vtp.total_tps || 0) + (ltp.total_tps || 0),
        (tp.total_pps || 0) + (vtp.total_pps || 0) + (ltp.total_pps || 0),
        ovHeroBucketMs(), 'max');
      ovHeroBucketSync();
    }
    const el = document.getElementById('overallLastUpdate');
    if (el) el.textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch (_) {}
}

// Hero bucket width: session-only view control — every page load starts
// back at the 5m default. OV-validated.
let _ovHeroBucketMs = null;
function ovHeroBucketMs() {
  return OV.heroBucketMs(_ovHeroBucketMs);
}

// Keeps #ovHeroBucket showing the applied width across band repaints.
function ovHeroBucketSync() {
  const sel = document.getElementById('ovHeroBucket');
  if (sel && sel.value !== String(ovHeroBucketMs())) sel.value = String(ovHeroBucketMs());
}

// Selector change: re-bucket from cached rows.
function ovHeroBucketChange(sel) {
  _ovHeroBucketMs = OV.heroBucketMs(sel.value);
  ovHeroBucketSync();
  if (typeof _ovHeroRows !== 'undefined' && _ovHeroRows) _ovHeroRender();
  else if (typeof loadOverallHistory === 'function') loadOverallHistory().catch(() => {});
}

// Hero overlay toggles: power/energy datasets ride a second y-axis.
function ovToggleOverlay() {
  if (typeof ovHeroChart === 'undefined' || !ovHeroChart) return;
  const power = !!document.getElementById('ovShowPower')?.checked;
  const energy = !!document.getElementById('ovShowEnergy')?.checked;
  if (ovHeroChart.data.datasets[2]) ovHeroChart.data.datasets[2].hidden = !power;
  if (ovHeroChart.data.datasets[3]) ovHeroChart.data.datasets[3].hidden = !energy;
  ovHeroChart.update('none');
}

function _ovPaintBand(llama, lms, vllm) {
  if (typeof OV === 'undefined') return;
  _ovPaintToplines(OV.toplines(llama, lms, vllm, _ovEnergy));
  const rates = _ovTilePeaksPush(llama, lms, vllm);
  _ovPaintTiles(OV.tiles(llama, lms, vllm, rates, Date.now()));
  _ovPaintAgents(OV.agentRows([llama, lms, vllm], window._agentsByProvider || {}));
  _ovPaintAlerts();
}

function _ovPaintToplines(stats) {
  const el = document.getElementById('ovToplines');
  if (!el) return;
  el.innerHTML = stats.map(s => `
    <div class="ov-topline">
      <div class="stat">${_esc(s.v)}</div>
      <div class="sub">${_esc(s.l)}</div>
    </div>`).join('');
}

function _ovPaintTiles(tiles) {
  const el = document.getElementById('ovTiles');
  if (!el) return;
  el.innerHTML = tiles.map(t => `
    <div class="ov-tile ov-${t.accent}" data-prov="${_esc(t.key)}">
      <div class="ov-tile-head">
        <span class="ov-eyebrow">${_esc(t.label)}</span>
        <span class="ov-tile-online">${t.online}/${t.total} online</span>
      </div>
      <div class="ov-tile-stats">
        ${t.stats.map(s => `<div><div class="stat">${_esc(s.v)}</div><div class="sub">${_esc(s.l)}</div>${s.p !== undefined ? `<div class="ov-tile-peak">${_esc(s.p || '—')}</div>` : ''}</div>`).join('')}
      </div>
    </div>`).join('');
}

function _ovAge(s) {
  return s == null ? '' : LMPeaks.agoText(s * 1000);
}

function _ovPaintAgents(rows) {
  const el = document.getElementById('ovAgentsStrip');
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = '<div class="ov-agents-empty">No approved agents yet.</div>';
    return;
  }
  el.innerHTML = rows.map(r => `
    <div class="ov-agent-row${r.online ? '' : ' ov-agent-off'}">
      <span class="dot dot--${r.online ? 'ok' : 'muted'}"></span>
      <span class="ov-agent-host">${_esc(r.hostname)}</span>
      <span class="ov-agent-provs">${r.provs.map(p =>
        `<span class="ov-agent-prov"><b>${_esc(OV.PROVIDER_LABEL[p.prov] || p.prov)}</b>${_esc(p.detail)}</span>`).join('')}</span>
      <span class="ov-agent-age">${r.online ? _ovAge(r.ageS) : 'offline'}</span>
    </div>`).join('');
}

function _ovPaintAlerts() {
  const el = document.getElementById('ovAlertsStrip');
  if (!el) return;
  if (window._activeAlerts === undefined) {
    el.className = 'ov-alerts';
    el.innerHTML = '<span>Alerts unavailable.</span>';
    return;
  }
  const s = OV.alertsSummary(window._activeAlerts);
  el.className = 'ov-alerts' + (s.worst === 'critical' ? ' ov-alerts-critical'
    : s.worst === 'warning' ? ' ov-alerts-warning' : '');
  if (!s.total) {
    el.innerHTML = '<span>No active alerts.</span>';
    return;
  }
  const parts = [];
  if (s.counts.critical) parts.push(`<b>${s.counts.critical}</b> critical`);
  if (s.counts.warning) parts.push(`<b>${s.counts.warning}</b> warning`);
  if (s.counts.info) parts.push(`<b>${s.counts.info}</b> info`);
  el.innerHTML = `
    <span class="ov-alert-count">${parts.join(' · ')}</span>
    ${s.newest.map(a => `<span>${_esc(a.rule)} <span class="sub">(${_esc(a.severity)})</span></span>`).join('')}
    <a onclick="switchTab('events')">Open Events →</a>`;
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

// Applies severity accent border to Dashboard cards, including any card
// currently adopted into the Overall pinned grid (#565).
// cls: 'dash-ok' | 'dash-warn' | 'dash-crit' | 'dash-off'
function _dashSetStatus(cardId, cls) {
  const el = document.querySelector(`#cardGrid [data-card="${cardId}"], #lmsCardGrid [data-card="${cardId}"], #vllmCardGrid [data-card="${cardId}"], #managerCardGrid [data-card="${cardId}"], #overallGrid [data-card="${cardId}"]`);
  if (!el) return;
  el.classList.remove('dash-ok','dash-warn','dash-crit','dash-off');
  if (cls) el.classList.add(cls);
}
