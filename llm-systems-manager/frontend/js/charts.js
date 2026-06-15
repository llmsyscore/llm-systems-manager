// ---------------------------------------------------------------------------
// Card-title hardware-name helper — set the <h3> text from a probed hardware
// name (CPU model, lspci GPU, liquidctl device header) with a generic
// fallback when the agent didn't report one. Also records the resolved
// name into window._hardwareNames keyed by data-card id so the settings
// picker can show the same hardware label.
// ---------------------------------------------------------------------------
function _setCardTitle(id, name, fallback, cardIds) {
  const el = document.getElementById(id);
  if (!el) return;
  const trimmed = name && String(name).trim();
  const next = trimmed || fallback;
  if (el.textContent !== next) el.textContent = next;
  if (cardIds) {
    window._hardwareNames = window._hardwareNames || {};
    const ids = Array.isArray(cardIds) ? cardIds : [cardIds];
    if (trimmed) ids.forEach(c => { window._hardwareNames[c] = trimmed; });
    else         ids.forEach(c => { delete window._hardwareNames[c]; });
  }
}

// ---------------------------------------------------------------------------
// Drag and drop
// ---------------------------------------------------------------------------
function initSortable() {
  Sortable.create(document.getElementById('cardGrid'), {
    handle: '.card-handle', animation: 150,
    onEnd: saveLayout,
  });
  Sortable.create(document.getElementById('overallGrid'), {
    handle: '.card-handle', animation: 150, ghostClass: 'sortable-ghost',
    onEnd: () => {
      layout.overallOrder = [...document.querySelectorAll('#overallGrid [data-card]')]
        .map(c => c.dataset.card);
      saveLayout();
    },
  });
  const lmsGrid = document.getElementById('lmsCardGrid');
  if (lmsGrid) {
    Sortable.create(lmsGrid, {
      handle: '.card-handle', animation: 150,
      onEnd: saveLmsLayout,
    });
  }
  const mgrGrid = document.getElementById('managerCardGrid');
  if (mgrGrid) {
    Sortable.create(mgrGrid, {
      handle: '.card-handle', animation: 150,
      onEnd: saveManagerLayout,
    });
  }
  initCardResize();
}

// Card sizes are discrete: '1x1' (default), '2x1', '3x1', '1x2', '2x2',
// '3x2'. They map to grid column/row span CSS classes, so cards always
// stay on the same grid rails as their neighbours regardless of how
// they've been resized. Each card has a small ⤢ button that cycles
// through the sensible sequence; users with a column-count constraint
// won't see oversize options.
const _CARD_SIZE_CYCLE = ['1x1', '2x1', '2x2', '1x2'];
const _CARD_SIZE_CLASSES = ['size-1x1','size-2x1','size-3x1','size-1x2','size-2x2','size-3x2'];
let _cardSizeSaveTimer = null;

function _gridColCount(grid) {
  const cs = getComputedStyle(grid).gridTemplateColumns || '';
  const toks = cs.trim().split(/\s+/).filter(t => t && t !== 'none');
  return Math.max(1, toks.length);
}
function _clampSize(size, maxCols) {
  // Old-format back-compat: {cs, rs} object → "<c>x<r>" string.
  if (size && typeof size === 'object' && size.cs) size = `${size.cs}x${size.rs || 1}`;
  if (!size || typeof size !== 'string' || !/^\dx\d$/.test(size)) return '1x1';
  let [cs, rs] = size.split('x').map(Number);
  cs = Math.max(1, Math.min(maxCols, cs));
  rs = Math.max(1, Math.min(2, rs));
  return `${cs}x${rs}`;
}
function _applyCardSize(card, size) {
  _CARD_SIZE_CLASSES.forEach(c => card.classList.remove(c));
  const grid = card.parentElement;
  const maxCols = grid ? _gridColCount(grid) : 3;
  const eff = _clampSize(size, maxCols);
  card.dataset.size = eff;
  if (eff !== '1x1') card.classList.add('size-' + eff);
  const btn = card.querySelector('.card-size-btn');
  if (btn) {
    btn.title = `Card size: ${eff} — click to cycle (1×1 → 2×1 → 2×2 → 1×2)`;
  }
  // Charts inside grow/shrink with the card; re-call resize() so they
  // re-paint at the monitor's actual DPR (no blur from CSS stretching).
  _resizeChartsIn(card);
}
function _scheduleCardSizesSave() {
  if (_cardSizeSaveTimer) clearTimeout(_cardSizeSaveTimer);
  _cardSizeSaveTimer = setTimeout(async () => {
    _cardSizeSaveTimer = null;
    try {
      const current = await fetch('/api/layout').then(r => r.json()).catch(() => ({}));
      current.cardSizes = layout.cardSizes;
      await fetch('/api/layout', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(current),
      });
    } catch (e) { /* best-effort */ }
  }, 250);
}
function _cycleCardSize(card) {
  const id = card.dataset.card; if (!id) return;
  const grid = card.parentElement;
  const maxCols = grid ? _gridColCount(grid) : 3;
  const cur = card.dataset.size || '1x1';
  // Filter the cycle to options that fit the current grid (e.g. on a
  // 1-column grid only 1x1 and 1x2 make sense).
  const allowed = _CARD_SIZE_CYCLE.filter(s => {
    const cs = Number(s.split('x')[0]);
    return cs <= maxCols;
  });
  const idx = allowed.indexOf(cur);
  const next = allowed[(idx + 1) % allowed.length];
  _applyCardSize(card, next);
  layout.cardSizes = layout.cardSizes || {};
  if (next === '1x1') delete layout.cardSizes[id];
  else layout.cardSizes[id] = next;
  _scheduleCardSizesSave();
}
function _resizeChartsIn(root) {
  if (!root || !window.Chart) return;
  // Defer to the next frame so CSS layout has resolved the new chart-
  // wrap dimensions before Chart.js reads them.
  requestAnimationFrame(() => {
    try {
      const inst = window.Chart.instances || {};
      Object.values(inst).forEach(ch => {
        if (ch && ch.canvas && root.contains(ch.canvas)) {
          try { ch.resize(); } catch(_) {}
        }
      });
    } catch(_) {}
  });
}
function _ensureSizeBtn(card) {
  if (card.querySelector('.card-size-btn')) return;
  const btn = document.createElement('button');
  btn.className = 'card-size-btn';
  btn.type = 'button';
  btn.textContent = '⤢';
  btn.addEventListener('click', e => {
    e.stopPropagation();
    _cycleCardSize(card);
  });
  card.appendChild(btn);
}
function initCardResize() {
  document.querySelectorAll('[data-card]').forEach(card => {
    const id = card.dataset.card; if (!id) return;
    _ensureSizeBtn(card);
    const saved = (layout.cardSizes || {})[id];
    if (saved) _applyCardSize(card, saved);
    else _applyCardSize(card, '1x1');
  });
}

// ----- Active-tab layout key resolver (single source of truth) -----
function _activeTabLayoutKeys() {
  if (_activeTab === 'overall') {
    return {
      label: 'LLM Overall', map: CARD_LABELS_OVERALL,
      hidden: 'hiddenOverall', order: 'overallOrder', cols: 'overallCols', borrowed: 'overallBorrowed',
      grid: document.getElementById('overallGrid'),
    };
  }
  if (_activeTab === 'dashboard') {
    const sub = _getDashSubTab();
    if (sub === 'lmstudio') return {
      label: 'Dashboard · LM Studio', map: CARD_LABELS_LMS,
      hidden: 'lmsHidden', order: 'lmsOrder', cols: 'lmsCols',
      grid: document.getElementById('lmsCardGrid'),
    };
    if (sub === 'manager') return {
      label: 'Dashboard · Manager', map: CARD_LABELS_MANAGER,
      hidden: 'managerHidden', order: 'managerOrder', cols: 'managerCols',
      grid: document.getElementById('managerCardGrid'),
    };
    return {
      label: 'Dashboard · llama.cpp', map: CARD_LABELS,
      hidden: 'hidden', order: 'order', cols: 'cols',
      grid: document.getElementById('cardGrid'),
    };
  }
  return null;
}

// ----- Layout presets ----------------------------------------------
// Each preset names a column count + an optional per-card-index sizing
// template. Cards beyond the indexed entries fall back to 1x1. Presets
// apply only to the active tab. Index 0 = first visible card in the
// current order.
const LAYOUT_PRESETS = {
  'uniform-2':    { label: '2 columns — uniform',          cols: 2, sizes: {} },
  'uniform-3':    { label: '3 columns — uniform',          cols: 3, sizes: {} },
  'hero-3':       { label: '3 columns — hero card (2×2)',  cols: 3, sizes: { 0: '2x2' } },
  'featured-3':   { label: '3 columns — featured row (3×1)', cols: 3, sizes: { 0: '3x1' } },
  'wide-pair-3':  { label: '3 columns — two wide leads (2×1, plus 1×1)', cols: 3, sizes: { 0: '2x1' } },
  'tall-pair-3':  { label: '3 columns — two tall leads (1×2)', cols: 3, sizes: { 0: '1x2', 1: '1x2' } },
  'uniform-4':    { label: '4 columns — uniform',          cols: 4, sizes: {} },
  'mixed-4':      { label: '4 columns — mixed (2×2 lead + tiles)', cols: 4, sizes: { 0: '2x2' } },
};
function applyLayoutPreset(presetId) {
  const ks = _activeTabLayoutKeys(); if (!ks || !ks.grid) return;
  const preset = LAYOUT_PRESETS[presetId]; if (!preset) return;
  // Column count for this tab's grid.
  layout[ks.cols] = preset.cols;
  // Clear sizes for every card in this tab's label map, then apply the
  // preset's index-keyed sizes against the current visible order.
  layout.cardSizes = layout.cardSizes || {};
  for (const id of Object.keys(ks.map)) delete layout.cardSizes[id];
  const visible = [...ks.grid.querySelectorAll('[data-card]')]
    .filter(c => c.style.display !== 'none' && !c.dataset.card.startsWith('ov-borrow-'));
  Object.entries(preset.sizes).forEach(([idx, size]) => {
    const card = visible[Number(idx)];
    if (card) layout.cardSizes[card.dataset.card] = size;
  });
  // Persist + reapply in place.
  fetch('/api/layout', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(layout),
  }).catch(() => {});
  applyAllGridCols();
  initCardResize();
  _resizeChartsIn(document.body);
  renderSettingsPanel();
}

// ---------------------------------------------------------------------------
// Chart factory
// ---------------------------------------------------------------------------
const xAxis = {
  type: 'time',
  time: { tooltipFormat: 'h:mm:ss a', displayFormats: { second: 'h:mm:ss a', minute: 'h:mm a', hour: 'h:mm a' } },
  ticks: { color: cssVar('--fg-muted'), font: { size: 9 }, maxTicksLimit: 6, maxRotation: 0 },
  grid: { color: cssVar('--border-soft') }
};

// Tooltip + interaction config shared by every chart factory below.
// pointRadius:0 hides dots in the steady-state line; without this, the
// default `nearest` interaction mode requires the cursor to land exactly
// on a (zero-pixel) point before showing a tooltip — operator complaint
// was "hovering rarely shows the time/value". `index + intersect:false`
// surfaces the tooltip whenever the cursor is over the matching x-axis
// position. hoverRadius makes the matching point visible on hover.
const _sparkInteraction = { mode: 'index', intersect: false };
const _sparkTooltip = {
  mode: 'index',
  intersect: false,
  // Default Chart.js label callback returns an empty value string for null
  // parsed.y — visually that turns into "label: " and some versions hide
  // the line entirely. Force "—" for missing values so every dataset in
  // the chart appears in the tooltip regardless of which probes happened
  // to land on that 10s tick.
  filter: () => true,
  callbacks: {
    label: (ctx) => {
      const v = ctx.parsed?.y;
      const lbl = ctx.dataset.label || '';
      if (v == null) return `${lbl}: —`;
      const formatted = Math.abs(v) >= 100 ? v.toFixed(0)
                      : Math.abs(v) >= 10  ? v.toFixed(1)
                      :                       v.toFixed(2);
      return `${lbl}: ${formatted}`;
    },
  },
};

function mkChart(id, label, color) {
  return new Chart(document.getElementById(id).getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [{ label, data: [], borderColor: color, borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.2 }] },
    options: { animation: false, responsive: true, maintainAspectRatio: false,
      interaction: _sparkInteraction,
      plugins: { legend: { display: false }, tooltip: _sparkTooltip },
      scales: { x: xAxis, y: { beginAtZero: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 } }, grid: { color: cssVar('--border-soft') } } }
    }
  });
}

// N-line chart factory — used by the self-monitor cards which graph more
// than two latency series on one canvas. `lines` is [{label, color}, ...].
function mkMultiChart(id, lines) {
  const canvas = document.getElementById(id);
  if (!canvas) return null;
  return new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: [],
      datasets: lines.map(l => ({
        label: l.label, data: [], borderColor: l.color,
        borderWidth: 1.2, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.2,
      })),
    },
    options: { animation: false, responsive: true, maintainAspectRatio: false,
      interaction: _sparkInteraction,
      plugins: { legend: { display: false }, tooltip: _sparkTooltip },
      scales: { x: xAxis, y: { beginAtZero: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 } }, grid: { color: cssVar('--border-soft') } } }
    }
  });
}

// Snap a timestamp down to the current poll-interval grid so live points and
// history backfill share one resolution (= the settings cadence). Same-grid
// appends collapse onto the prior point instead of densifying one side (#129).
function _bucketDate(ts) {
  const ms = new Date(ts).getTime();
  const w = (typeof fetchInterval === 'number' && fetchInterval > 0) ? fetchInterval : 0;
  return w ? new Date(Math.floor(ms / w) * w) : new Date(ms);
}

// Push the same timestamp to all datasets in a multi-line chart; missing
// values come in as `null` so Chart.js draws a gap rather than connecting
// across stale points.
function pushMulti(chart, ts, values) {
  if (!chart) return;
  const t = _bucketDate(ts);
  const l = chart.data.labels;
  if (l.length && t.getTime() <= l[l.length - 1].getTime()) {
    chart.data.datasets.forEach((ds, i) => {
      if (values[i] != null) ds.data[ds.data.length - 1] = values[i];
    });
  } else {
    l.push(t);
    chart.data.datasets.forEach((ds, i) => ds.data.push(values[i] != null ? values[i] : null));
    if (l.length > MAX_POINTS) {
      l.shift();
      chart.data.datasets.forEach(ds => ds.data.shift());
    }
  }
  chart.update('none');
}

function mkDualChart(id, l1, c1, l2, c2) {
  return new Chart(document.getElementById(id).getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: l1, data: [], borderColor: c1, borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.2 },
      { label: l2, data: [], borderColor: c2, borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.2 },
    ]},
    options: { animation: false, responsive: true, maintainAspectRatio: false,
      interaction: _sparkInteraction,
      plugins: { legend: { display: false }, tooltip: _sparkTooltip },
      scales: { x: xAxis, y: { beginAtZero: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 } }, grid: { color: cssVar('--border-soft') } } }
    }
  });
}

const cpuChart      = mkChart('cpuChart',      'CPU %',       '#e05');
const ramChart      = mkChart('ramChart',      'RAM %',       '#05e');
const gpuChart      = mkChart('gpuChart',      'GPU util %',  '#0e5');
const netChart      = mkChart('netChart',      'MB/s',        '#e50');
const ctxChart      = mkChart('ctxChart',      'Peak ctx',    '#88f');
const aioTempChart = mkChart('aioTempChart', 'Liquid °C', '#4dd');
const genTokensChart  = mkChart('genTokensChart',  'Tokens gen', '#7af');
const llamaChart    = mkDualChart('llamaChart',    'Gen t/s',  '#7af', 'Prompt t/s', '#fa7');
const ioChart       = mkDualChart('ioChart',       'Read',     '#a7f', 'Write',       '#f7a');
const psuPowerChart = mkDualChart('psuPowerChart', 'Output W', '#0e9', 'Input W',     '#fa7');

// Self-monitor cards — manager_self_monitor source. Order of datasets in
// each chart must match the order of values passed to pushMulti() below.
const mgrPerfChart = mkMultiChart('mgrPerfChart', [
  { label: 'manager_api',     color: '#8af' },
  { label: 'manager_history', color: '#fa8' },
]);
// 7-color palette chosen for dark-background contrast — the previous
// palette had three near-duplicate pairs (#8af/#88f, #fa8/#f88, #af8/#8f8)
// making it impossible to tell ae_health from influx_q_24h at a glance.
const aePerfChart = mkMultiChart('aePerfChart', [
  { label: 'ae_health',         color: '#4ea1ff' },  // blue
  { label: 'ae_ingest',         color: '#ff8a3d' },  // orange
  { label: 'ae_query_24h',      color: '#3ad17f' },  // emerald
  { label: 'rule_eval_cycle',   color: '#ff5775' },  // rose
  { label: 'influx_write',      color: '#36d7e6' },  // cyan
  { label: 'influx_query_5m',   color: '#ffd042' },  // gold
  { label: 'influx_query_24h',  color: '#b88aff' },  // lavender
]);

// Dual-line disk usage chart — root + iscsi target.
const diskUsageCtx = document.getElementById('diskUsageChart').getContext('2d');
const diskUsageChart = new Chart(diskUsageCtx, {
  type: 'line',
  data: { labels: [], datasets: [
    { label: '/', data: [], borderColor: '#4a9', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.2 },
    { label: '/mnt/iscsi', data: [], borderColor: '#7af', borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.2 },
  ]},
  options: { animation: false, responsive: true, maintainAspectRatio: false,
    interaction: _sparkInteraction,
    plugins: { legend: { display: false }, tooltip: _sparkTooltip },
    scales: { x: xAxis, y: { min: 0, max: 100, ticks: { color: cssVar('--fg-muted'), font: { size: 10 }, callback: v => v + '%' }, grid: { color: cssVar('--border-soft') } } }
  }
});


function pushPoint(chart, ts, val) {
  const d = chart.data.datasets[0].data, l = chart.data.labels;
  const t = _bucketDate(ts);
  if (l.length && t.getTime() <= l[l.length - 1].getTime()) {
    d[d.length - 1] = val;
  } else {
    d.push(val); l.push(t);
    if (d.length > MAX_POINTS) { d.shift(); l.shift(); }
  }
  chart.update('none');
}

function pushDual(chart, ts, v1, v2) {
  const l = chart.data.labels, d0 = chart.data.datasets[0].data, d1 = chart.data.datasets[1].data;
  const t = _bucketDate(ts);
  if (l.length && t.getTime() <= l[l.length - 1].getTime()) {
    d0[d0.length - 1] = v1 || 0; d1[d1.length - 1] = v2 || 0;
  } else {
    d0.push(v1 || 0); d1.push(v2 || 0); l.push(t);
    if (l.length > MAX_POINTS) { l.shift(); d0.shift(); d1.shift(); }
  }
  chart.update('none');
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function fmt(bytes) {
  if (bytes == null) return '—';
  const gb = bytes / 1073741824;
  return gb >= 1 ? gb.toFixed(1) + ' GB' : (bytes / 1048576).toFixed(0) + ' MB';
}

function lqVal(obj, key) {
  if (!obj || !obj[key]) return '—';
  const v = obj[key].value;
  return v != null ? (typeof v === 'number' ? v.toFixed(1) : v) : '—';
}

function timeSince(isoStr) {
  if (!isoStr) return '';
  const secs = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs/60)}m ago`;
  return `${Math.floor(secs/3600)}h ago`;
}

const lastNonZero = {
  active_slots: { val: null, ts: null },
  requests_processing: { val: null, ts: null },
  requests_deferred: { val: null, ts: null },
};

function updateNonZero(key, val) {
  if (val !== null && val !== 0) lastNonZero[key] = { val, ts: new Date().toISOString() };
}

function fmtWithPeak(current, key) {
  const p = lastNonZero[key];
  if (current !== null && current !== 0) return String(current);
  if (p && p.val !== null) return `0 <span style="font-size:0.7em;color:var(--fg-dim)">(peak ${p.val} ${timeSince(p.ts)})</span>`;
  return '0';
}

// ---------------------------------------------------------------------------
// Server state polling — every 2 seconds regardless of main interval
// ---------------------------------------------------------------------------
let _lastKnownState = 'unknown';
let _pillModelName  = '';

// Open an SSE stream against the agent directly, falling back to the
// manager's two-hop proxy URL on failure. Caller passes the *-info
// endpoint path and the legacy proxy path; the wrapper returns a
// Promise<EventSource>.
//
// Why fetch-then-stream: EventSource can't carry an Authorization
// header, so the manager mints a short-lived HMAC token bound to one
// agent + one path + one expiry, returns the agent URL with the token
// in ?token=. Browser connects directly to the agent (CORS allowed).
//
// Falls back transparently when:
//   1) the info endpoint is missing / returns 503 (flag off / no primary set);
//   2) the info endpoint returns a URL but the EventSource fails to open
//      (browser can't reach the agent's IP/port, untrusted TLS cert,
//      firewall blocking the agent port, etc.) — without (2), a browser that
//      can't reach the agent directly would just get `[stream disconnected]`
//      with no recovery, even though the manager-proxied path would work.
// Once the direct-to-agent attempt fails this session (the common case: the
// browser doesn't trust the agent's internal-CA cert, so it logs "certificate
// invalid" and we fall back), remember it and go straight to the manager-proxied
// path on every later open — no repeated cert errors, no 3s race per stream.
let _directAgentSseFailed = false;

async function openAgentSse(infoPath, fallbackPath) {
  if (_directAgentSseFailed) return new EventSource(fallbackPath);
  let directUrl = null;
  try {
    const r = await fetch(infoPath, { cache: 'no-store' });
    if (r.ok) {
      const d = await r.json();
      if (d && d.ok && d.url) directUrl = d.url;
    }
  } catch (e) {
    // network/JSON error → skip direct, use fallback
  }
  if (!directUrl) return new EventSource(fallbackPath);

  // Race: try the direct-to-agent URL first. If it opens within 3s, return
  // it. If it errors before opening (or just doesn't open in time), close it
  // and return the manager-proxied EventSource instead. The caller still gets
  // a single EventSource back; they don't have to know which path won.
  return new Promise((resolve) => {
    const direct = new EventSource(directUrl);
    let settled = false;
    const finish = (es) => {
      if (settled) return;
      settled = true;
      resolve(es);
    };
    const timer = setTimeout(() => {
      if (settled) return;
      _directAgentSseFailed = true;   // direct didn't open in time — stop trying it
      try { direct.close(); } catch(_) {}
      finish(new EventSource(fallbackPath));
    }, 3000);
    direct.addEventListener('open', () => {
      // If the race already settled (3s timer fired → proxied fallback chosen),
      // a late-opening direct stream would leak. Close it.
      if (settled) { try { direct.close(); } catch(_) {} return; }
      clearTimeout(timer);
      finish(direct);
    });
    direct.addEventListener('error', () => {
      if (settled) return;
      clearTimeout(timer);
      _directAgentSseFailed = true;   // cert-invalid / unreachable — stop trying direct
      try { direct.close(); } catch(_) {}
      finish(new EventSource(fallbackPath));
    });
  });
}

function _applyLlamaStatePayload(data) {
  if (!data || typeof data !== 'object') return;
  const state = data.state || 'unknown';
  // Update pill model name from state endpoint — never overwrite with "sleeping".
  // Explicitly clear when server reports no model so an old name doesn't linger.
  if (!data.model) {
    _pillModelName = '';
  } else {
    const clean = data.model.replace(/\s*\(sleeping\)$/i, '')
                            .replace(/\s*\(unloaded\)$/i, '')
                            .trim();
    if (!clean || clean.toLowerCase() === 'sleeping') {
      _pillModelName = '';
    } else {
      _pillModelName = clean.split('/').pop() || clean;
    }
  }
  const banner = document.getElementById('serverStateBanner');
  const icon   = document.getElementById('serverStateIcon');
  const text   = document.getElementById('serverStateText');
  if (!banner || !icon || !text) return;

    banner.className = `state-banner state-${state}`;

    const isLlamaUp = (state === 'awake' || state === 'sleeping');
    // The LLM Control badge says "Agent" — it must reflect whether the
    // llm-systems-agent process is reporting, not whether llama-server
    // is up. Conflating them showed "Agent offline" whenever
    // llama-server was stopped, which made it look like the host
    // dropped off when only the service had stopped. Use the manager's
    // explicit agent_online flag (true when a host-metrics push has
    // arrived within the last 30s); fall back to isLlamaUp only on
    // legacy responses without the new field.
    const agentLive = (typeof data.agent_online === 'boolean') ? data.agent_online : isLlamaUp;
    const llamaCtrlBadge = document.getElementById('llamaCtrlBadge');
    if (llamaCtrlBadge) {
      llamaCtrlBadge.className = `status ${agentLive ? 'status--ok' : 'status--crit'}`;
      llamaCtrlBadge.innerHTML = '<span class="status__dot"></span>' + (agentLive ? 'Agent online' : 'Agent offline');
    }

    // Update persistent server status label next to control buttons
    const srvStatusEl = document.getElementById('llamaServerStatus');
    if (srvStatusEl) {
      if (isLlamaUp) {
        const port = data.port || 8080;
        srvStatusEl.style.color = '#999';
        srvStatusEl.textContent = `Server ON · port ${port}`;
      } else {
        srvStatusEl.style.color = 'var(--crit)';
        srvStatusEl.textContent = 'Server OFF';
      }
    }

    if (state === 'awake') {
      icon.textContent = '●';
      if (_pillModelName) {
        text.textContent = 'LLCPP · Active · ' + _pillModelName;
      } else {
        text.textContent = 'LLCPP · no model loaded';   // server up, no model loaded
      }
    } else if (state === 'sleeping') {
      if (_pillModelName) {
        // Model was loaded when server entered idle/sleep
        banner.className = 'state-banner state-sleeping';
        icon.textContent = '◌';
        text.textContent = 'LLCPP · Sleeping · ' + _pillModelName;
      } else {
        // Server is sleeping but no model is loaded — show as standby/ready
        banner.className = 'state-banner state-ready';
        icon.textContent = '●';
        text.textContent = 'LLCPP · no model loaded';
      }
    } else {
      icon.textContent = '○';
      text.textContent = 'LLCPP · Off';
    }

    // Enable/disable llama.cpp server control buttons based on state
    const llamaUp = (state === 'awake' || state === 'sleeping');
    _setLlamaBtns(llamaUp);

    // On wake transition — refresh metrics and LLM tab model cards
    if (_lastKnownState === 'sleeping' && state === 'awake') {
      fetchMetrics();
      if (document.getElementById('llmTab').style.display !== 'none') {
        setTimeout(() => { refreshLLMTab().then(() => _updateModelPerf()); }, 1500);
      }
    }
    // Any state transition can change the backend poll interval (awake↔sleeping
    // flips between 2s and 30s). Re-read /api/config so the badge and the
    // fetchMetrics timer update immediately instead of waiting up to 10s for
    // the next checkConfig tick.
    if (_lastKnownState !== state) {
      checkConfig();
    }
    _lastKnownState = state;
}

async function pollServerState() {
  // One-shot fetch + apply. Kept for explicit refreshes (post-action,
  // visibility-change, restart confirm, etc.). Steady-state updates flow
  // through the SSE stream below, not this function.
  if (document.hidden) return;
  const _pk = _agentClaimKey('pollServerState', 'llama');
  if (!_claim(_pk)) return;
  try {
    const data = await _fetchT('/api/llama-state', {}, 8000).then(r => r.json());
    _applyLlamaStatePayload(data);
  } catch(e) {
  } finally {
    _release(_pk);
  }
}

// SSE-driven llama-state updates. Replaces the previous 2s polling loop —
// the manager broadcasts a payload whenever (state | model | agent_online)
// actually changes, plus a heartbeat every 25s. Auto-reconnect on drop.
let _llamaStateES = null;
function _stopLlamaStateStream() {
  if (_llamaStateES) { try { _llamaStateES.close(); } catch(_) {} _llamaStateES = null; }
}
// Daemon-path CLOSED-error count; >=2 stops -info probing for the page life.
let _llamaDaemonFails = 0;
// Single-flight guard: the async fetch window otherwise lets re-entry orphan an ES.
let _llamaStartInflight = false;
async function _startLlamaStateStream(isReconnect) {
  // Skip for a backgrounded tab (visibilitychange reopens) or while one is opening.
  if (document.hidden || _llamaStartInflight) return;
  _llamaStartInflight = true;
  // Fresh open (load / focus / agent switch) re-probes the daemon; only the
  // reconnect chain preserves the fail count so a dead daemon isn't hammered.
  if (!isReconnect) _llamaDaemonFails = 0;
  try {
    if (_llamaStateES) { try { _llamaStateES.close(); } catch(_) {} }
    _llamaStateES = null;

    // Ask the manager (session-gated) whether to use the off-pool daemon.
    // fetch() is wrapped in foundation.js to append ?agent= for /api/* paths.
    let info = null;
    if (_llamaDaemonFails < 2) {
      try {
        const r = await fetch('/api/llama-state/stream-info', { cache: 'no-store' });
        if (r.ok) info = await r.json();
      } catch (_) { /* network error → Cheroot fallback below */ }
    }

    let es;
    let viaDaemon = false;
    if (info && info.enabled && info.url) {
      // Absolute cross-origin daemon URL; foundation.js leaves non-/api/ URLs alone.
      es = new EventSource(info.url);
      viaDaemon = true;
    } else {
      es = new EventSource('/api/llama-state/stream');  // Cheroot path (wrapper adds ?agent=)
    }
    _llamaStateES = es;

    es.onmessage = (ev) => {
      if (viaDaemon) _llamaDaemonFails = 0;  // only a daemon message proves it recovered
      try { _applyLlamaStatePayload(JSON.parse(ev.data)); } catch(_) {}
    };
    es.onerror = () => {
      // CLOSED: tear down, count daemon failures, re-fetch -info + reconnect in 3s.
      if (es.readyState === EventSource.CLOSED) {
        if (viaDaemon) _llamaDaemonFails++;
        _llamaStateES = null;
        setTimeout(() => _startLlamaStateStream(true), 3000);
      }
    };
  } catch (e) {
    // EventSource unsupported (very old browser); fall back to slow poll.
    setInterval(pollServerState, 5000);
  } finally {
    _llamaStartInflight = false;
  }
}
_startLlamaStateStream();
let fetchInterval = 5000;
let fetchTimer = null;

function startFetching(ms) {
  if (fetchTimer) clearInterval(fetchTimer);
  fetchTimer = setInterval(fetchMetrics, ms);
}

// Apply the live poll interval WITHOUT starting the timer — called at boot
// before history backfill so backfill and live appends bucket to the same
// grid (#129). Starting the timer here would race chart resets in loadHistory.
async function syncInterval() {
  try {
    const cfg = await _fetchT('/api/config', {}, 8000).then(r => r.json());
    const ms = (cfg.poll_interval || 5) * 1000;
    if (ms > 0) fetchInterval = ms;
  } catch (_) {}
}

async function checkConfig() {
  if (!_claim('checkConfig')) return;
  try {
    const cfg = await _fetchT('/api/config', {}, 8000).then(r => r.json());
    const newMs = (cfg.poll_interval || 5) * 1000;
    const mode  = cfg.interval_mode || 'auto';
    _intervalMode = mode;
    const badge = document.getElementById('intervalBadge');
    if (badge) {
      badge.textContent = mode === 'manual'
        ? `${cfg.poll_interval}s · manual`
        : `${cfg.poll_interval}s · auto`;
      badge.style.color = mode === 'manual' ? 'var(--warn)' : 'var(--fg-dim)';
    }
    if (newMs !== fetchInterval) { fetchInterval = newMs; startFetching(fetchInterval); }

    // `!== false` (not `=== true`) so an older manager without `proxies` in
    // the payload keeps tabs visible — backwards-compatible default.
    const px = (cfg && cfg.proxies) || {};
    const toggle = (id, on) => {
      const el = document.getElementById(id);
      if (!el) return;
      const target = on ? '' : 'none';
      if (el.style.display !== target) el.style.display = target;
    };
    toggle('tabBtnLlmchat',     px.llm_chat  !== false);
    toggle('tabBtnOpenclaw',    px.openclaw  !== false);
    toggle('subTabBtnOpenclaw', px.openclaw  !== false);
    toggle('tabBtnImggen',      px.image_gen !== false);

    // Agent-driven visibility: hide LLM tabs/pills when no agent
    // advertises the matching capability yet. Defaults to visible so
    // older backends without `cfg.agents` don't lose the tabs.
    const ag = (cfg && cfg.agents) || {};
    const llamaOn = ag.llama_present !== false;
    const lmsOn   = ag.lms_present   !== false;
    const llmOn   = llamaOn || lmsOn;
    toggle('tabBtnOverall',          llmOn);
    toggle('tabBtnLlmControl',       llmOn);
    toggle('subTabBtnDashLlamacpp',  llamaOn);
    toggle('subTabBtnDashLmstudio',  lmsOn);
    toggle('subTabBtnLlmLlamacpp',   llamaOn);
    toggle('subTabBtnLlmLmstudio',   lmsOn);
    toggle('serverStateBanner',      llamaOn);
    toggle('lmsStateBanner',         lmsOn);

    // If the currently active tab just got hidden, fall back to Dashboard
    // — otherwise the operator stares at an empty panel with no nav.
    const activeBtn = document.querySelector('.tab-nav .tab-btn.active');
    if (activeBtn && activeBtn.style.display === 'none') switchTab('dashboard');

    // Same for Dashboard sub-tabs: if the active sub-tab was hidden, fall
    // back to a visible sibling (openclaw or manager always stay visible).
    if (_subTabState.dashboard === 'llamacpp' && !llamaOn) switchSubTab('dashboard','manager');
    if (_subTabState.dashboard === 'lmstudio' && !lmsOn)   switchSubTab('dashboard','manager');
    if (_subTabState.llm === 'llamacpp' && !llamaOn && lmsOn) switchSubTab('llm','lmstudio');
    if (_subTabState.llm === 'lmstudio' && !lmsOn && llamaOn) switchSubTab('llm','llamacpp');
  } catch(e) {
  } finally {
    _release('checkConfig');
  }
}

// ---------------------------------------------------------------------------
// History backfill
// ---------------------------------------------------------------------------
// Empty a chart's labels + every dataset so a backfill replaces rather than
// appends. update('none') redraws without animation.
function _clearChart(ch) {
  if (!ch || !ch.data) return;
  ch.data.labels = [];
  (ch.data.datasets || []).forEach(d => { d.data = []; });
  ch.update('none');
}

// Wipe every dashboard time-series. Called at the top of loadHistory so a
// per-agent backfill never blends onto the previously-selected agent's lines
// (#121); a no-op at boot when the charts are already empty.
function _resetMetricCharts() {
  [cpuChart, ramChart, gpuChart, netChart, ctxChart, aioTempChart,
   genTokensChart, llamaChart, ioChart, psuPowerChart, diskUsageChart]
    .forEach(_clearChart);
}

// LM Studio dashboard time-series. Cleared at the top of loadLmsHistory so an
// LMS agent switch doesn't blend onto the previous agent's lines (#121).
function _resetLmsCharts() {
  [typeof lmsCpuChart !== 'undefined' ? lmsCpuChart : null,
   typeof lmsRamChart !== 'undefined' ? lmsRamChart : null,
   typeof lmsNetChart !== 'undefined' ? lmsNetChart : null]
    .forEach(_clearChart);
}

async function loadHistory() {
  try {
    // Replace, don't append — charts are per-agent, so clear before backfill.
    _resetMetricCharts();
    // Backfill the (picker-)selected llama agent's host history. No selection
    // (single-agent install) → plain /api/history = the default-agent ring,
    // byte-identical to pre-multi-agent.
    const sel = (typeof _selectedAgent === 'function') ? _selectedAgent('llama') : null;
    const url = sel ? `/api/history?agent=${encodeURIComponent(sel)}` : '/api/history';
    const rows = await fetch(url).then(r => r.json());
    if (!rows || !rows.length) return;
    // Clear again after the await: a live fetchMetrics tick can append a
    // current-time point during the fetch, which the bucketed backfill would
    // otherwise collapse onto (charts start at "now" on agent switch) (#137).
    _resetMetricCharts();
    // Convert bytes-per-second → MiB-per-second so backfill points match the
    // live-fetch unit (see net/io conversion in fetchMetrics around line 3550).
    const B_PER_MIB = 1048576;
    for (const r of rows.slice(-MAX_POINTS)) {
      pushPoint(cpuChart,  r.ts, r.cpu_total   || 0);
      pushPoint(ramChart,  r.ts, r.ram_percent || 0);
      pushPoint(gpuChart,  r.ts, r.gpu_util    || 0);
      pushPoint(netChart,  r.ts, ((r.net_sent || 0) + (r.net_recv || 0)) / B_PER_MIB);
      pushDual(llamaChart, r.ts, r.llama_tps,  r.llama_pps);
      pushDual(ioChart,    r.ts, (r.io_read  || 0) / B_PER_MIB,
                                  (r.io_write || 0) / B_PER_MIB);
      // Hardware sensor charts (AIO liquid temp + PSU power draw).
      // Skip the push when the field is undefined so the line stays at
      // its previous value instead of dropping to 0.
      if (typeof aioTempChart !== 'undefined' && r.aio_temp != null)
        pushPoint(aioTempChart, r.ts, r.aio_temp);
      if (typeof psuPowerChart !== 'undefined' && (r.psu_out != null || r.psu_in != null))
        pushDual(psuPowerChart, r.ts, r.psu_out || 0, r.psu_in || 0);
      // Detailed llama charts — only present when llama was active during
      // the backfill window. Empty otherwise.
      if (typeof ctxChart !== 'undefined' && r.llama_ctx != null)
        pushPoint(ctxChart, r.ts, r.llama_ctx);
      if (typeof genTokensChart !== 'undefined' && r.llama_gen_tokens != null)
        pushPoint(genTokensChart, r.ts, r.llama_gen_tokens);
      // Disk usage — / and /mnt/iscsi percent over time.
      if (typeof diskUsageChart !== 'undefined'
          && (r.disk_root_pct != null || r.disk_iscsi_pct != null)) {
        pushDual(diskUsageChart, r.ts,
                 r.disk_root_pct  != null ? r.disk_root_pct  : 0,
                 r.disk_iscsi_pct != null ? r.disk_iscsi_pct : 0);
      }
    }
  } catch(e) { console.error('History error:', e); }
}

// Backfill manager + alarm-engine self-monitor charts from the alarm
// engine catalog so the operator sees the last 60 min instead of an
// empty pane until a probe lands. Fires once at startup. Each metric is
// fetched in parallel; failures are silent (chart just starts empty).
async function loadManagerPerfHistory() {
  // Scope to the manager's own host by agent id (resolved server-side via the
  // alarm proxy); no id → unfiltered, fine since these series are single-host
  // (#140). Never keyed by a browser-held hostname.
  const AGENT = window.__MGR_AGENT;
  const agentQ = AGENT ? `&agent=${encodeURIComponent(AGENT)}` : '';
  const url = (name) =>
    `/api/alarm/metrics/manager_self_monitor/${encodeURIComponent(name)}`
    + `?since_minutes=60${agentQ}`;
  const fetchPoints = async (name) => {
    try {
      const r = await fetch(url(name));
      if (!r.ok) return [];
      const pts = await r.json();
      return Array.isArray(pts) ? pts : [];
    } catch { return []; }
  };

  // Align timestamps across series by zipping into a {ts → values} map.
  // Different probe metrics fire on different cadences (META_PERF_INTERVAL_S=60s);
  // we don't try to interpolate — each x position has only the values
  // that actually arrived at that timestamp, the tooltip already handles
  // nulls gracefully (see _sparkTooltip).
  const zipByTs = (seriesPoints) => {
    const map = new Map();
    seriesPoints.forEach((pts, idx) => {
      for (const p of pts) {
        const ts = p.timestamp || p.ts;
        if (!ts) continue;
        if (!map.has(ts)) map.set(ts, new Array(seriesPoints.length).fill(null));
        map.get(ts)[idx] = p.value;
      }
    });
    return [...map.entries()].sort(([a], [b]) => new Date(a) - new Date(b));
  };

  // Manager Perf (2 series)
  if (typeof mgrPerfChart !== 'undefined' && mgrPerfChart) {
    const [api, hist] = await Promise.all([
      fetchPoints('manager_api_latency_ms'),
      fetchPoints('manager_history_latency_ms'),
    ]);
    _clearChart(mgrPerfChart);  // discard any racing live point (#137)
    for (const [ts, vals] of zipByTs([api, hist])) pushMulti(mgrPerfChart, ts, vals);
  }

  // AE + Influx Perf (7 series — keep order in sync with pushMulti call
  // in fetchServicesAndInflux + the aePerfChart factory).
  if (typeof aePerfChart !== 'undefined' && aePerfChart) {
    const names = [
      'ae_health_latency_ms', 'ae_ingest_latency_ms', 'ae_query_24h_latency_ms',
      'rule_eval_cycle_ms',
      'influx_write_latency_ms', 'influx_query_5m_latency_ms', 'influx_query_24h_latency_ms',
    ];
    const series = await Promise.all(names.map(fetchPoints));
    _clearChart(aePerfChart);  // discard any racing live point (#137)
    for (const [ts, vals] of zipByTs(series)) pushMulti(aePerfChart, ts, vals);
  }
}

// Backfill LM Studio host charts (CPU/RAM/Net) from the selected LMS agent's
// history at startup so the cards land already populated with the last 60 min
// instead of waiting for the next 6s poll. Scoped by agent id (resolved to a
// host server-side via /api/history?agent=), never by a browser-held hostname
// (#140). Makes no llama calls — the Overall llama chart backfills separately
// from the Overall tab (loadOverallHistory), not the LMS dashboard (#142).
async function loadLmsHistory() {
  // Replace, don't append — clear before backfill so an agent switch shows
  // only the selected agent's history (#121).
  _resetLmsCharts();
  const B_PER_MIB = 1048576;

  // LMS host series: picker selection, else the server-injected default LMS
  // agent id. No id (no approved LMS agent) → skip the host-card backfill.
  const sel = (typeof _selectedAgent === 'function') ? _selectedAgent('lms') : null;
  const lmsAgent = sel || window.__LMS_AGENT;
  if (lmsAgent) {
    try {
      const rows = await fetch(`/api/history?agent=${encodeURIComponent(lmsAgent)}`)
        .then(r => r.json());
      if (rows && rows.length) {
        // Clear again after the await: a racing live poll can append a
        // current-time point that the #129 bucketing would otherwise collapse
        // the whole backfill onto (#137).
        _resetLmsCharts();
        for (const r of rows.slice(-MAX_POINTS)) {
          if (typeof lmsCpuChart !== 'undefined' && lmsCpuChart && r.cpu_total != null)
            pushPoint(lmsCpuChart, r.ts, r.cpu_total);
          if (typeof lmsRamChart !== 'undefined' && lmsRamChart && r.ram_percent != null)
            pushPoint(lmsRamChart, r.ts, r.ram_percent);
          if (typeof lmsNetChart !== 'undefined' && lmsNetChart)
            pushDual(lmsNetChart, r.ts,
              r.net_sent != null ? r.net_sent / B_PER_MIB : null,
              r.net_recv != null ? r.net_recv / B_PER_MIB : null);
        }
      }
    } catch (e) { console.error('LMS history error:', e); }
  }
}

// Backfill the Overall-tab llama TPS chart (Gen / Prompt) from the fleet
// rollup so it matches the live fleet totals painted by fetchOverallMetrics.
// Called only from the Overall tab (one-time) so the LMS dashboard makes no
// llama calls (#142).
async function loadOverallHistory() {
  if (typeof ovLlamaChart === 'undefined' || !ovLlamaChart) return;
  try {
    const rows = await fetch('/api/history?fleet=llama').then(r => r.json());
    if (!rows || !rows.length) return;
    _clearChart(ovLlamaChart);  // discard any racing live point (#137)
    for (const r of rows.slice(-MAX_POINTS)) {
      if (r.llama_tps != null || r.llama_pps != null)
        pushDual(ovLlamaChart, r.ts, r.llama_tps, r.llama_pps);
    }
  } catch (e) { console.error('Overall llama history error:', e); }
}

// ---------------------------------------------------------------------------
// Main fetch
// ---------------------------------------------------------------------------
async function fetchMetrics() {
  // Keep the active dashboard updating at the settings cadence even when the
  // browser tab is backgrounded (#129). SSE streams still release on hide.
  const _mk = _agentClaimKey('fetchMetrics', 'llama');
  if (!_claim(_mk)) return;
  try {
    const m = await _fetchT('/api/metrics', {}, 10000).then(r => r.json());
    const ts = m.ts || new Date().toISOString();

    window._latestMetric = m;
    // Overall tab is fleet-aggregated (PR4) — refresh it from the fleet
    // endpoints, not this single-agent sample, while it's visible.
    if (document.getElementById('overallTab')?.style.display !== 'none') {
      fetchOverallMetrics();
    }

    // OpenClaw analytics — refresh only when that sub-tab is visible
    if (_activeTab === 'dashboard' && _subTabState.dashboard === 'openclaw') {
      fetchOpenclawAnalytics();
    }

    // CPU
    _setCardTitle('cpuCardTitle', m.cpu_name, 'CPU', 'cpu-overall');
    document.getElementById('cpuStat').textContent = (m.cpu_total || 0).toFixed(1) + '%';
    pushPoint(cpuChart, ts, m.cpu_total || 0);
    if (m.cpu_temp_c != null) document.getElementById('cpuTemp').textContent = m.cpu_temp_c.toFixed(1) + '°C';
    if (m.cpu_governor) document.getElementById('cpuGovernor').textContent = m.cpu_governor;
    if (m.cpu_per_core) {
      document.getElementById('coreGrid').innerHTML = m.cpu_per_core.map((pct, i) => {
        const glowClass = pct >= 90 ? ' crit' : pct >= 70 ? ' warn' : '';
        const color = pct >= 90 ? '#f55' : pct >= 70 ? '#fc0' : '';
        return `<div class="core${glowClass}"><div class="sub">C${i}</div><div class="pct" style="${color ? `color:${color}` : ''}">${pct.toFixed(0)}%</div></div>`;
      }).join('');
    }

    // RAM
    const rp = m.ram ? m.ram.percent : 0;
    document.getElementById('ramStat').textContent = rp.toFixed(1) + '%';
    document.getElementById('ramSub').textContent  = m.ram ? fmt(m.ram.used_bytes) + ' used / ' + fmt(m.ram.available_bytes) + ' avail' : '';
    if (m.ram) {
      document.getElementById('ramCached').textContent  = fmt(m.ram.cached_bytes);
      document.getElementById('ramBuffers').textContent = fmt(m.ram.buffers_bytes);
    }
    if (m.swap) {
      document.getElementById('swapUsed').textContent = fmt(m.swap.used_bytes);
      document.getElementById('swapFree').textContent = fmt(m.swap.free_bytes);
    }
    pushPoint(ramChart, ts, rp);

    // GPU
    const g = m.gpu || {};
    _setCardTitle('gpuCardTitle',   g.name, 'GPU', ['gpu', 'ov-llama-gpu']);
    document.getElementById('gpuTemp').textContent            = g.temperature_c           != null ? g.temperature_c.toFixed(1) : '—';
    document.getElementById('gpuTempJunction').textContent    = g.temperature_junction_c  != null ? g.temperature_junction_c.toFixed(1) : '—';
    document.getElementById('gpuTempMemory').textContent      = g.temperature_memory_c    != null ? g.temperature_memory_c.toFixed(1) : '—';
    document.getElementById('gpuVddgfx').textContent          = g.vddgfx_mv               != null ? g.vddgfx_mv : '—';
    document.getElementById('gpuFan1').textContent            = g.fan1_rpm                != null ? g.fan1_rpm : '—';
    document.getElementById('gpuVram').textContent            = g.vram_usage_percent      != null ? g.vram_usage_percent.toFixed(1) : '—';
    document.getElementById('gpuVramMb').textContent          = g.vram_used_mb            != null ? '(' + g.vram_used_mb.toLocaleString() + ' MB)' : '';
    document.getElementById('gpuUtil').textContent            = g.gpu_util_percent        != null ? g.gpu_util_percent.toFixed(1) : '—';
    document.getElementById('gpuPower').textContent           = g.power_watts             != null ? g.power_watts.toFixed(0) : '—';
    document.getElementById('gpuPowerCap').textContent        = g.power_cap_watts         != null ? g.power_cap_watts.toFixed(0) : '—';
    document.getElementById('gpuVoltage').textContent         = g.voltage_offset_mv       != null ? g.voltage_offset_mv : '—';
    document.getElementById('gpuSclk').textContent            = g.sclk_mhz               != null ? g.sclk_mhz : '—';
    document.getElementById('gpuMclk').textContent            = g.mclk_mhz               != null ? g.mclk_mhz : '—';
    document.getElementById('gpuPerfLevel').textContent       = g.performance_level       || '—';
    document.getElementById('gpuPowerProfile').textContent    = g.power_profile            || '—';
    pushPoint(gpuChart, ts, g.gpu_util_percent || 0);

    // Network
    const net = m.net || {};
    const sMiB = (net.bytes_sent_per_sec || 0) / 1048576;
    const rMiB = (net.bytes_recv_per_sec || 0) / 1048576;
    document.getElementById('netSent').textContent = sMiB.toFixed(2);
    document.getElementById('netRecv').textContent = rMiB.toFixed(2);
    pushPoint(netChart, ts, sMiB + rMiB);

    // Disk usage
    if (m.disk && m.disk.length) {
      document.getElementById('diskList').innerHTML = m.disk.map(d =>
        `<div class="disk-row">
          <span style="min-width:100px;color:var(--fg-muted);font-size:0.8em">${_esc(d.mountpoint)}</span>
          <div class="disk-bar"><div class="disk-fill" style="width:${Number(d.percent)}%"></div></div>
          <span>${d.percent.toFixed(1)}%</span>
        </div>`
      ).join('');
      const byMount = Object.fromEntries(m.disk.map(d => [d.mountpoint, d.percent]));
      pushDual(diskUsageChart, ts,
        byMount['/'] || 0,
        byMount['/mnt/iscsi'] || 0,
      );
    }

    // iSCSI
    const isc = m.iscsi || {};
    const iscsiStateEl = document.getElementById('iscsiState');
    iscsiStateEl.textContent = isc.state || '—';
    iscsiStateEl.style.color = isc.state === 'LOGGED_IN' ? '#4e9' : '#f55';
    if (isc.target) {
      const parts = isc.target.split(':');
      document.getElementById('iscsiTarget').textContent = parts[parts.length - 1] || isc.target;
    }

    // Disk IO
    const io = m.disk_io || {};
    const rMiB2 = (io.read_bytes_per_sec  || 0) / 1048576;
    const wMiB  = (io.write_bytes_per_sec || 0) / 1048576;
    document.getElementById('ioRead').textContent  = rMiB2.toFixed(2);
    document.getElementById('ioWrite').textContent = wMiB.toFixed(2);
    pushDual(ioChart, ts, rMiB2, wMiB);

    // Llama
    const ll = m.llama || {};
    const sleeping = ll.sleeping === true;
    const modelEl = document.getElementById('llamaModel');
    // Track model name for state pill — only update when awake with a real name
    if (ll.model && !sleeping) {
      const clean = ll.model.replace(/\s*\(sleeping\)$/i, '').trim();
      if (clean && clean.toLowerCase() !== 'sleeping') {
        _pillModelName = clean.split('/').pop() || clean;
      }
    }
    modelEl.textContent = ll.model || 'No model loaded';
    modelEl.style.color = sleeping ? '#444' : '#aaa';
    modelEl.title = sleeping ? 'Model is sleeping — metrics polling paused' : '';
    document.getElementById('llamaTps').textContent          = ll.tokens_per_second        != null ? ll.tokens_per_second.toFixed(1) : '—';
    document.getElementById('llamaPps').textContent          = ll.prompt_tokens_per_second != null ? ll.prompt_tokens_per_second.toFixed(1) : '—';
    document.getElementById('llamaGenTokens').textContent    = ll.total_tokens_generated   != null ? ll.total_tokens_generated.toLocaleString() : '—';
    document.getElementById('llamaPromptTokens').textContent = ll.total_tokens_prompted    != null ? ll.total_tokens_prompted.toLocaleString() : '—';
    document.getElementById('llamaDecodes').textContent      = ll.n_decode_total           != null ? ll.n_decode_total.toLocaleString() : '—';
    document.getElementById('llamaBusySlots').textContent    = ll.n_busy_slots_per_decode  != null ? ll.n_busy_slots_per_decode.toFixed(2) : '—';
    document.getElementById('llamaCtxHigh').textContent      = ll.n_tokens_max             != null ? ll.n_tokens_max.toLocaleString() : '—';
    document.getElementById('llamaKvRatio').textContent      = ll.kv_cache_usage_ratio     != null ? (ll.kv_cache_usage_ratio * 100).toFixed(1) + '%' : '—';
    document.getElementById('llamaKvTokens').textContent     = ll.kv_cache_tokens          != null ? ll.kv_cache_tokens.toLocaleString() : '—';
    document.getElementById('llamaNRemain').textContent      = ll.n_remain                 != null ? ll.n_remain.toLocaleString() : '—';
    const _prevLlamaActive = _llamaActiveSlots > 0;
    _llamaActiveSlots = ll.active_slots || 0;
    if ((_llamaActiveSlots > 0) !== _prevLlamaActive) renderModelCards();
    updateNonZero('active_slots',        ll.active_slots);
    updateNonZero('requests_processing', ll.requests_processing);
    updateNonZero('requests_deferred',   ll.requests_deferred);
    document.getElementById('llamaSlots').innerHTML      = fmtWithPeak(ll.active_slots,        'active_slots');
    document.getElementById('llamaProcessing').innerHTML = fmtWithPeak(ll.requests_processing, 'requests_processing');
    document.getElementById('llamaDeferred').innerHTML   = fmtWithPeak(ll.requests_deferred,   'requests_deferred');
    pushDual(llamaChart, ts, ll.tokens_per_second, ll.prompt_tokens_per_second);
    pushPoint(ctxChart, ts, ll.n_tokens_max || 0);
    pushPoint(genTokensChart, ts, ll.total_tokens_generated || 0);

    // UPS
    const ups = m.ups || {};
    const pct = ups.percent;
    const upsEl = document.getElementById('upsPercent');
    upsEl.textContent = pct != null ? pct.toFixed(0) + '%' : '—';
    upsEl.className   = 'val' + (pct != null && pct < 20 ? ' crit' : pct != null && pct < 50 ? ' warn' : '');
    document.getElementById('upsState').textContent   = ups.state         || '—';
    document.getElementById('upsWarning').textContent = ups.warning_level || '—';
    const onBat = ups.on_battery;
    const onBatEl = document.getElementById('upsOnBattery');
    onBatEl.textContent = onBat == null ? '—' : onBat ? 'Yes' : 'No';
    onBatEl.className   = 'val' + (onBat ? ' crit' : '');
    document.getElementById('upsTimeEmpty').textContent = ups.time_to_empty || '—';
    const ttf = document.getElementById('upsTimeFull');
    const ttfLbl = document.getElementById('upsTimeFullLbl');
    if (ups.time_to_full) {
      ttf.textContent = ups.time_to_full; ttfLbl.textContent = 'Time to full'; ttf.className = 'val';
    } else {
      ttf.textContent = 'Charged'; ttfLbl.textContent = 'Status'; ttf.className = 'val';
    }

    // Liquidctl — AIO / PSU / Smart Device — h3s renamed from the device
    // headers liquidctl prints on its first non-tree line.
    const lq = m.liquidctl || {};
    const k = lq.aio || {};
    _setCardTitle('aioCardTitle',         k._name,              'AIO',            'aio');
    _setCardTitle('psuCardTitle',         (lq.psu   || {})._name, 'PSU',            'psu');
    _setCardTitle('smartDeviceCardTitle', (lq.smart || {})._name, 'Fan controller', 'smart-device');
    document.getElementById('aioTemp').textContent     = lqVal(k, 'Liquid temperature');
    document.getElementById('aioPumpSpeed').textContent= lqVal(k, 'Pump speed');
    document.getElementById('aioPumpDuty').textContent = lqVal(k, 'Pump duty');
    document.getElementById('aioFanSpeed').textContent = lqVal(k, 'Fan speed');
    document.getElementById('aioFanDuty').textContent  = lqVal(k, 'Fan duty');
    pushPoint(aioTempChart, ts, k['Liquid temperature'] ? k['Liquid temperature'].value : 0);

    // Liquidctl — PSU
    const p = lq.psu || {};
    document.getElementById('psuVrmTemp').textContent   = lqVal(p, 'VRM temperature');
    document.getElementById('psuCaseTemp').textContent  = lqVal(p, 'Case temperature');
    document.getElementById('psuFanSpeed').textContent  = lqVal(p, 'Fan speed');
    document.getElementById('psuInputV').textContent    = lqVal(p, 'Input voltage');
    document.getElementById('psuTotalOut').textContent  = lqVal(p, 'Total power output');
    document.getElementById('psuInputPower').textContent= lqVal(p, 'Estimated input power');
    document.getElementById('psuEfficiency').textContent= lqVal(p, 'Estimated efficiency');
    const psuOut = p['Total power output']   ? p['Total power output'].value   : 0;
    const psuIn  = p['Estimated input power'] ? p['Estimated input power'].value : 0;
    pushDual(psuPowerChart, ts, psuOut, psuIn);

    // Liquidctl — Smart Device fans (with sensors voltage/current)
    const sd = lq.smart || {};
    const fans = sd.fans || [];
    document.getElementById('smartFanTable').innerHTML = fans.map(f =>
      `<tr>
        <td>Fan ${f.id}</td>
        <td>${_esc(f.control_mode || '—')}</td>
        <td>${f.duty != null ? _esc(f.duty) : '—'}</td>
        <td>${f.speed ? _esc(f.speed.value) + ' ' + _esc(f.speed.unit) : '—'}</td>
        <td>${f.voltage_v != null ? f.voltage_v.toFixed(2) + ' V' : '—'}</td>
        <td>${f.current_ma != null ? f.current_ma + ' mA' : '—'}</td>
      </tr>`
    ).join('');

// ---- Dashboard card accent borders (severity-based left border color) ----
    // llama-server / llama-throughput: model+active→ok, sleeping→warn, no model→off
    {
      const _ll = m.llama || {};
      const _llamaState = _ll.model
        ? (_ll.sleeping ? 'dash-warn' : 'dash-ok')
        : 'dash-off';
      _dashSetStatus('llama-server',     _llamaState);
      _dashSetStatus('llama-throughput', _llamaState);
    }
    // GPU: edge temp thresholds
    {
      const _gt = (m.gpu || {}).temperature_c;
      _dashSetStatus('gpu', _gt != null ? (_gt >= 85 ? 'dash-crit' : _gt >= 70 ? 'dash-warn' : 'dash-ok') : 'dash-off');
    }
    // CPU
    {
      const _ct = m.cpu_total;
      _dashSetStatus('cpu-overall', _ct != null ? (_ct >= 90 ? 'dash-crit' : _ct >= 75 ? 'dash-warn' : 'dash-ok') : 'dash-off');
    }
    // RAM
    {
      const _rp = (m.ram || {}).percent;
      _dashSetStatus('ram', _rp != null ? (_rp >= 90 ? 'dash-crit' : _rp >= 75 ? 'dash-warn' : 'dash-ok') : 'dash-off');
    }
    // UPS
    {
      const _u = m.ups || {};
      const _upsSt = _u.on_battery ? 'dash-crit'
        : (_u.percent != null && _u.percent < 40) ? 'dash-warn'
        : (_u.percent != null ? 'dash-ok' : 'dash-off');
      _dashSetStatus('ups', _upsSt);
    }
    // AIO liquid temp
    {
      const _kTemp = ((m.liquidctl || {}).aio || {})['Liquid temperature'];
      const _kt = _kTemp ? _kTemp.value : null;
      _dashSetStatus('aio', _kt != null ? (_kt >= 40 ? 'dash-crit' : _kt >= 38 ? 'dash-warn' : 'dash-ok') : 'dash-off');
    }
    // PSU / Network / Disk — show ok if data present, off otherwise
    _dashSetStatus('psu',        (m.liquidctl || {}).psu ? 'dash-ok' : 'dash-off');
    _dashSetStatus('network',    m.net ? 'dash-ok' : 'dash-off');
    _dashSetStatus('disk-usage', m.disk ? 'dash-ok' : 'dash-off');
    _dashSetStatus('disk-io',    m.disk_io ? 'dash-ok' : 'dash-off');
    _dashSetStatus('smart-device', (m.liquidctl || {}).smart ? 'dash-ok' : 'dash-off');

  } catch(e) {
    console.error('Fetch error:', e);
  } finally {
    _release(_mk);
    syncBorrowedCards();
  }
}

