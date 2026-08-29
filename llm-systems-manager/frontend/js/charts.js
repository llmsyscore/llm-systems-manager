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
      layout.overallOrder = [...document.querySelectorAll('#overallGrid > [data-card]')]
        .map(c => c.dataset.card);
      saveLayout();
    },
  });
  // Fleet-band strips drag as whole units (#565).
  const band = document.querySelector('.ov-band');
  if (band) {
    Sortable.create(band, {
      handle: '.ov-strip-handle', animation: 150, ghostClass: 'sortable-ghost',
      onEnd: () => {
        layout.overallBandOrder = [...band.children]
          .map(s => s.dataset && s.dataset.strip).filter(Boolean);
        saveLayout();
      },
    });
  }
  const lmsGrid = document.getElementById('lmsCardGrid');
  if (lmsGrid) {
    Sortable.create(lmsGrid, {
      handle: '.card-handle', animation: 150,
      onEnd: saveLmsLayout,
    });
  }
  const vllmGrid = document.getElementById('vllmCardGrid');
  if (vllmGrid) {
    Sortable.create(vllmGrid, {
      handle: '.card-handle', animation: 150,
      onEnd: saveVllmLayout,
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

// Card sizes are discrete: 'auto' (default: one column, content height —
// never stretched to the grid row), '1x1' (one column, row height), '2x1',
// '3x1', '1x2', '2x2', '3x2'. They map to grid span CSS classes, so cards
// always stay on the same grid rails as their neighbours. Each card has a
// small ⤢ button that cycles through the sensible sequence; users with a
// column-count constraint won't see oversize options.
const _CARD_SIZE_CYCLE = ['auto', '1x1', '2x1', '2x2', '1x2'];
const _CARD_SIZE_CLASSES = ['size-1x1','size-auto','size-2x1','size-3x1','size-1x2','size-2x2','size-3x2'];
// Size a card (or borrowed shell) takes when the layout has none saved for it.
const _CARD_DEFAULT_SIZE = 'auto';
function _defaultCardSize(_id) {
  return _CARD_DEFAULT_SIZE;
}
function _sizeCols(size) {
  return size === 'auto' ? 1 : Number(String(size).split('x')[0]);
}
function _sizeLabel(size) {
  return size === 'auto' ? 'auto (content height)' : String(size).replace('x', '×');
}
// Sizes that fit the card's grid, in cycle order.
function _allowedSizes(card) {
  const grid = card.parentElement;
  const maxCols = grid ? _gridColCount(grid) : 3;
  return _CARD_SIZE_CYCLE.filter(s => _sizeCols(s) <= maxCols);
}
function _nextCardSize(card, cur) {
  const allowed = _allowedSizes(card);
  return allowed[(allowed.indexOf(cur) + 1) % allowed.length];
}
let _cardSizeSaveTimer = null;

function _gridColCount(grid) {
  const cs = getComputedStyle(grid).gridTemplateColumns || '';
  const toks = cs.trim().split(/\s+/).filter(t => t && t !== 'none');
  return Math.max(1, toks.length);
}
function _clampSize(size, maxCols) {
  // Old-format back-compat: {cs, rs} object → "<c>x<r>" string.
  if (size && typeof size === 'object' && size.cs) size = `${size.cs}x${size.rs || 1}`;
  if (size === 'auto') return size;
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
  const btn = card.querySelector(':scope > .card-size-btn');
  if (btn) {
    btn.title = `Card size: ${_sizeLabel(eff)} · click for ${_sizeLabel(_nextCardSize(card, eff))}`;
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
      current.sizesByAgent = layout.sizesByAgent;
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
  const next = _nextCardSize(card, card.dataset.size || _defaultCardSize(id));
  _applyCardSize(card, next);
  const sizes = _sizeMapFor(id);
  if (next === _defaultCardSize(id)) delete sizes[id];
  else sizes[id] = next;
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
  // Direct-child check only — an adopted card's own button inside a shell
  // must not satisfy the shell's guard (#565).
  if (card.querySelector(':scope > .card-size-btn')) return;
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
    _applyCardSize(card, _sizeMapFor(id)[id] || _defaultCardSize(id));
  });
}

// ----- Active-tab layout key resolver (single source of truth) -----
function _activeTabLayoutKeys() {
  if (_activeTab === 'overall') {
    return {
      label: 'LLM Overall', map: {},
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
    if (sub === 'vllm') return {
      label: 'Dashboard · vLLM', map: CARD_LABELS_VLLM,
      hidden: 'vllmHidden', order: 'vllmOrder', cols: 'vllmCols',
      grid: document.getElementById('vllmCardGrid'),
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
  // Clear this tab's card sizes (per-agent map for llama.cpp/LMS), then apply
  // the preset's index-keyed sizes against the current visible order.
  for (const id of Object.keys(ks.map)) delete _sizeMapFor(id)[id];
  const visible = [...ks.grid.querySelectorAll('[data-card]')]
    .filter(c => c.style.display !== 'none' && !c.dataset.card.startsWith('ov-borrow-'));
  Object.entries(preset.sizes).forEach(([idx, size]) => {
    const card = visible[Number(idx)];
    if (card) _sizeMapFor(card.dataset.card)[card.dataset.card] = size;
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
function _crEsc(s) {
  return String(s).replace(/[&<>"']/g,
    ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

// External tooltip: a readout pinned to the top center of the hosting
// card, above the plot, so it never covers any data point.
function _readoutTooltip(ctx) {
  const { chart, tooltip } = ctx;
  const host = chart.canvas.closest('.card, .ov-band > section') || chart.canvas.parentElement;
  if (!host) return;
  let el = host.querySelector(':scope > .chart-readout');
  if (!el) {
    el = document.createElement('div');
    el.className = 'chart-readout';
    host.appendChild(el);
  }
  if (!tooltip || tooltip.opacity === 0) { el.style.opacity = '0'; return; }
  const title = (tooltip.title || []).join(' ');
  const rows = (tooltip.body || []).map((b, i) => {
    const c = (tooltip.labelColors && tooltip.labelColors[i]) || {};
    const color = c.borderColor || c.backgroundColor || 'transparent';
    return `<span class="cr-item"><span class="cr-dot" style="background:${_crEsc(color)}"></span>${_crEsc(b.lines.join(' '))}</span>`;
  }).join('');
  el.innerHTML = `<span class="cr-title">${_crEsc(title)}</span>${rows}`;
  el.style.opacity = '1';
}

const _sparkTooltip = {
  mode: 'index',
  intersect: false,
  enabled: false,
  external: _readoutTooltip,
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

// Box-drag zoom (XY rectangle); double-click or the reset-zoom icon resets
// (wired globally at end of file). Wheel-zoom and pan are disabled.
const _zoomOpts = {
  zoom: {
    wheel: { enabled: false },
    drag: { enabled: true, borderColor: cssVar('--accent'), borderWidth: 1,
            backgroundColor: 'rgba(122,162,255,0.15)' },
    mode: 'xy',
    onZoomComplete: ({ chart }) => _syncResetZoomBtn(chart),
  },
  pan: { enabled: false },
  limits: { x: { min: 'original', max: 'original' } },
};

// Show/hide a per-chart reset-zoom icon in the chart's .chart-wrap based on
// whether the chart is currently zoomed. Clicking it resets and hides.
function _syncResetZoomBtn(chart) {
  if (!chart || !chart.canvas) return;
  const wrap = chart.canvas.parentElement;
  if (!wrap) return;
  // Mount in the card header beside the drag grip so it never overlays the
  // plot; fall back to the chart wrap for canvases outside a card.
  const host = chart.canvas.closest('.card') || wrap;
  const zoomed = typeof chart.isZoomedOrPanned === 'function' && chart.isZoomedOrPanned();
  const key = chart.canvas.id || '';
  let btn = host.querySelector(`.chart-reset-zoom[data-for="${key}"]`);
  if (!zoomed) { if (btn) { btn.remove(); _layoutResetZoomBtns(host); } return; }
  if (!btn) {
    btn = document.createElement('button');
    btn.className = 'chart-reset-zoom';
    btn.type = 'button';
    btn.dataset.for = key;
    btn.textContent = '⟲';
    btn.title = 'Reset zoom';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      try { chart.resetZoom(); } catch (_) {}
      _syncResetZoomBtn(chart);
    });
    host.appendChild(btn);
  }
  _layoutResetZoomBtns(host);
}

// Two-chart cards can have two buttons live at once — fan them out leftward
// from the drag grip instead of stacking them at the same coordinates.
function _layoutResetZoomBtns(host) {
  const btns = host.querySelectorAll('.chart-reset-zoom');
  btns.forEach((b, i) => { b.style.right = (30 + i * 28) + 'px'; });
}

// Percent tick label: Chart.js's own numeric formatter (what the unsuffixed
// llama axes use) plus the unit, so zoomed float bounds don't print 15 digits.
function _pctTick(v, i, ticks) {
  const f = window.Chart && Chart.Ticks && Chart.Ticks.formatters
         && Chart.Ticks.formatters.numeric;
  const n = f ? f.call(this, v, i, ticks)
       : Number.isInteger(v) ? String(v)
       : Math.abs(v) < 10 ? String(Number(Number(v).toFixed(1)))
       : String(Math.round(v));
  return n + '%';
}
window._pctTick = _pctTick;

function mkChart(id, label, color) {
  return new Chart(document.getElementById(id).getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [{ label, data: [], borderColor: color, borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.2 }] },
    options: { animation: false, responsive: true, maintainAspectRatio: false,
      interaction: _sparkInteraction,
      plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts, annotation: { annotations: {} } },
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
      plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts, annotation: { annotations: {} } },
      scales: { x: xAxis, y: { beginAtZero: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 } }, grid: { color: cssVar('--border-soft') } } }
    }
  });
}

// Snap a timestamp down to the current poll-interval grid so live points and
// history backfill share one resolution (= the settings cadence). Same-grid
// appends collapse onto the prior point instead of densifying one side (#129).
function _bucketDate(ts) {
  const w = (typeof fetchInterval === 'number' && fetchInterval > 0) ? fetchInterval : 0;
  return LMSeries.bucketDate(ts, w);
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
      plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts, annotation: { annotations: {} } },
      scales: { x: xAxis, y: { beginAtZero: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 } }, grid: { color: cssVar('--border-soft') } } }
    }
  });
}

// Expand a 3-digit hex token to 6 digits so an alpha byte can be appended.
function _hex6(c) {
  const m = String(c || '').trim().match(/^#([0-9a-fA-F]{3})$/);
  return m ? '#' + [...m[1]].map(ch => ch + ch).join('') : String(c || '').trim();
}

// Overall-tab hero: cross-provider 24h throughput. Gen keeps the fleet
// accent with a soft area fill; prompt rides as a plain line (#565).
function _mkHeroChart() {
  const canvas = document.getElementById('ovHeroChart');
  if (!canvas) return null;
  const genColor = cssVar('--accent'), promptColor = cssVar('--warn');
  return new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'Gen t/s', data: [], borderColor: genColor, borderWidth: 2,
        pointRadius: 0, pointHoverRadius: 4, tension: 0.25, fill: 'origin',
        backgroundColor: (ctx) => {
          const { chartArea, ctx: c } = ctx.chart;
          if (!chartArea) return 'transparent';
          try {
            const g = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
            g.addColorStop(0, _hex6(genColor) + '2e');
            g.addColorStop(1, _hex6(genColor) + '00');
            return g;
          } catch (_) { return 'transparent'; }
        } },
      { label: 'Prompt t/s', data: [], borderColor: promptColor, borderWidth: 1.5,
        pointRadius: 0, pointHoverRadius: 4, tension: 0.25, fill: false },
      { label: 'Power W', data: [], borderColor: cssVar('--accent-2'),
        borderWidth: 1.5, borderDash: [5, 3], pointRadius: 0, pointHoverRadius: 4,
        tension: 0.25, fill: false, hidden: true, yAxisID: 'y1', spanGaps: true },
      { label: 'Energy Wh/h', data: [], borderColor: cssVar('--note'),
        borderWidth: 1.5, borderDash: [2, 3], pointRadius: 0, pointHoverRadius: 4,
        stepped: true, fill: false, hidden: true, yAxisID: 'y1', spanGaps: true },
    ]},
    options: { animation: false, responsive: true, maintainAspectRatio: false,
      interaction: _sparkInteraction,
      plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts, annotation: { annotations: {} } },
      scales: { x: xAxis,
        y: { beginAtZero: true, ticks: { color: cssVar('--fg-muted'), font: { size: 10 } }, grid: { color: cssVar('--border-soft') } },
        y1: { display: 'auto', position: 'right', beginAtZero: true,
              ticks: { color: cssVar('--fg-muted'), font: { size: 10 } },
              grid: { display: false } } }
    }
  });
}
const ovHeroChart = _mkHeroChart();

const cpuChart      = mkChart('cpuChart',      'CPU %',       '#e05');
const ramChart      = mkChart('ramChart',      'RAM %',       '#05e');
const gpuChart      = mkChart('gpuChart',      'GPU util %',  '#0e5');
const netChart      = mkChart('netChart',      'MB/s',        '#e50');
const llamaSrvChart = mkDualChart('llamaSrvChart', 'Gen t/s',  '#7af', 'Prompt t/s', '#fa7');
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

// Toggle the /mnt/iscsi series + legend entry; the series only shows for
// agents whose collector actually discovered the mount (#565 feedback).
function _setIscsiSeriesVisible(on) {
  if (typeof diskUsageChart === 'undefined' || !diskUsageChart) return;
  if (diskUsageChart.data.datasets[1].hidden !== !on) {
    diskUsageChart.data.datasets[1].hidden = !on;
    diskUsageChart.update('none');
  }
  const leg = document.getElementById('diskIscsiLegend');
  if (leg) leg.style.display = on ? '' : 'none';
}

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
    plugins: { legend: { display: false }, tooltip: _sparkTooltip, zoom: _zoomOpts, annotation: { annotations: {} } },
    scales: { x: xAxis, y: { min: 0, max: 100, ticks: { color: cssVar('--fg-muted'), font: { size: 10 }, callback: _pctTick }, grid: { color: cssVar('--border-soft') } } }
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

// bucketMs overrides the poll-interval grid for series with their own fixed
// cadence (e.g. the 15s gateway pusher) so short bursts aren't collapsed away.
// agg 'max' keeps the in-bucket peak instead of the latest sample.
function pushDual(chart, ts, v1, v2, bucketMs, agg) {
  const l = chart.data.labels, d0 = chart.data.datasets[0].data, d1 = chart.data.datasets[1].data;
  const t = bucketMs ? LMSeries.bucketDate(ts, bucketMs) : _bucketDate(ts);
  if (l.length && t.getTime() <= l[l.length - 1].getTime()) {
    if (agg === 'max') {
      d0[d0.length - 1] = Math.max(d0[d0.length - 1] || 0, v1 || 0);
      d1[d1.length - 1] = Math.max(d1[d1.length - 1] || 0, v2 || 0);
    } else {
      d0[d0.length - 1] = v1 || 0; d1[d1.length - 1] = v2 || 0;
    }
  } else {
    d0.push(v1 || 0); d1.push(v2 || 0); l.push(t);
    if (l.length > MAX_POINTS) { l.shift(); d0.shift(); d1.shift(); }
  }
  chart.update('none');
}

// Bucket width for gateway-pushed token rates — matches gateway_usage
// PUSH_INTERVAL_S on the manager.
const GW_RATE_BUCKET_MS = 15000;

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

function timeSince(ts) {
  if (ts == null || ts === '') return '';
  const ms = typeof ts === 'number' ? ts : new Date(ts).getTime();
  if (!Number.isFinite(ms)) return '';
  return LMPeaks.agoText(Date.now() - ms);
}

// Muted "(peak N Xm ago)" suffix shared by fmtWithPeak and fmtLivePeak.
function _peakSpan(valText, ts) {
  return `<span style="font-size:0.7em;color:var(--fg-dim)">(peak ${valText} ${timeSince(ts)})</span>`;
}

const lastNonZero = {
  active_slots: { val: null, ts: null },
  requests_processing: { val: null, ts: null },
  requests_deferred: { val: null, ts: null },
};

// Rolling 15-min peaks for the Llama server card's Gen/Prompt tokens/s;
// all samples carry browser-clock timestamps.
const _LLAMA_PEAK_WINDOW_MS = 900000;
// Server-card spark bucket: peak-per-minute so short bursts stay visible.
const _LLAMA_SRV_BUCKET_MS = 60000;
const _llamaPeaks = {
  tps: LMPeaks.makeTracker(_LLAMA_PEAK_WINDOW_MS),
  pps: LMPeaks.makeTracker(_LLAMA_PEAK_WINDOW_MS),
};

// "12.3 (peak 45.6 3m ago)" — the window peak stays visible at all times.
function fmtLivePeak(cur, tracker) {
  const live = cur != null ? cur.toFixed(1) : '—';
  const p = tracker.peak(Date.now());
  if (!p) return live;
  return `${live} ${_peakSpan(p.v.toFixed(1), p.t)}`;
}

// Writes innerHTML only when the rendered string changed.
const _livePeakLast = {};
function _setLivePeak(id, html) {
  if (_livePeakLast[id] === html) return;
  _livePeakLast[id] = html;
  document.getElementById(id).innerHTML = html;
}

function updateNonZero(key, val) {
  if (val !== null && val !== 0) lastNonZero[key] = { val, ts: new Date().toISOString() };
}

function fmtWithPeak(current, key) {
  const p = lastNonZero[key];
  if (current !== null && current !== 0) return String(current);
  if (p && p.val !== null) return `0 ${_peakSpan(p.val, p.ts)}`;
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

// Mirrors backend clean_display_model(): '(unloaded)' → '' (no model);
// strips the '(sleeping)' suffix (model still resident).
function _cleanLlamaModelName(raw) {
  if (typeof raw !== 'string' || /\(unloaded\)\s*$/i.test(raw)) return '';
  const clean = raw.replace(/\s*\(sleeping\)$/i, '').trim();
  return (!clean || clean.toLowerCase() === 'sleeping') ? '' : clean;
}

function _applyLlamaStatePayload(data) {
  if (!data || typeof data !== 'object') return;
  const state = data.state || 'unknown';
  // Update pill model name from state endpoint — cleared when no model so
  // an old name doesn't linger.
  const _pillClean = _cleanLlamaModelName(data.model);
  _pillModelName = _pillClean ? (_pillClean.split('/').pop() || _pillClean) : '';
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
        // Server up, no model loaded — muted tone, same as the LMS/vLLM pills
        banner.className = 'state-banner state-sleeping';
        text.textContent = 'LLCPP · no model loaded';
      }
    } else if (state === 'sleeping') {
      if (_pillModelName) {
        // Model was loaded when server entered idle/sleep
        banner.className = 'state-banner state-sleeping';
        icon.textContent = '◌';
        text.textContent = 'LLCPP · Sleeping · ' + _pillModelName;
      } else {
        // Server up, no model loaded — muted tone, same as the LMS/vLLM pills
        banner.className = 'state-banner state-sleeping';
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

    _llamaBuildMethod = (data && data.build_method) || '';
    const _bbtn = document.getElementById('llamaBtnBuild');
    if (_bbtn) {
      _bbtn.textContent = _llamaBuildMethod
        ? `⬆ Update llama.cpp (${_llamaBuildMethod})`
        : '⬆ Update llama.cpp';
    }
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

// Cached build method from the last /api/llama-state payload — read cross-file.
var _llamaBuildMethod = '';

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
    const vllmOn  = ag.vllm_present  === true;   // new key: absent => hidden
    const llmOn   = llamaOn || lmsOn || vllmOn;
    toggle('tabBtnOverall',          llmOn);
    toggle('tabBtnLlmControl',       llmOn);
    toggle('subTabBtnDashLlamacpp',  llamaOn);
    toggle('subTabBtnDashLmstudio',  lmsOn);
    toggle('subTabBtnDashVllm',      vllmOn);
    toggle('subTabBtnLlmLlamacpp',   llamaOn);
    toggle('subTabBtnLlmLmstudio',   lmsOn);
    toggle('subTabBtnLlmVllm',       vllmOn);
    toggle('serverStateBanner',      llamaOn);
    toggle('lmsStateBanner',         lmsOn);
    toggle('vllmStateBanner',        vllmOn);

    // If the currently active tab just got hidden, fall back to Dashboard
    // — otherwise the operator stares at an empty panel with no nav.
    const activeBtn = document.querySelector('.tab-nav .tab-btn.active');
    if (activeBtn && activeBtn.style.display === 'none') switchTab('dashboard');

    // Same for Dashboard sub-tabs: if the active sub-tab was hidden, fall
    // back to a visible sibling (openclaw or manager always stay visible).
    if (_subTabState.dashboard === 'llamacpp' && !llamaOn) switchSubTab('dashboard','manager');
    if (_subTabState.dashboard === 'lmstudio' && !lmsOn)   switchSubTab('dashboard','manager');
    if (_subTabState.dashboard === 'vllm' && !vllmOn)      switchSubTab('dashboard','manager');
    // Only fall back to a sub-tab whose provider is actually present.
    if (_subTabState.llm === 'llamacpp' && !llamaOn && (lmsOn || vllmOn))
      switchSubTab('llm', lmsOn ? 'lmstudio' : 'vllm');
    if (_subTabState.llm === 'lmstudio' && !lmsOn && (llamaOn || vllmOn))
      switchSubTab('llm', llamaOn ? 'llamacpp' : 'vllm');
    if (_subTabState.llm === 'vllm' && !vllmOn && (llamaOn || lmsOn))
      switchSubTab('llm', llamaOn ? 'llamacpp' : 'lmstudio');
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
  [cpuChart, ramChart, gpuChart, netChart, llamaSrvChart, aioTempChart,
   genTokensChart, llamaChart, ioChart, psuPowerChart, diskUsageChart]
    .forEach(_clearChart);
}

// LM Studio dashboard time-series. Cleared at the top of loadLmsHistory so an
// LMS agent switch doesn't blend onto the previous agent's lines (#121).
function _resetLmsCharts() {
  [typeof lmsCpuChart !== 'undefined' ? lmsCpuChart : null,
   typeof lmsRamChart !== 'undefined' ? lmsRamChart : null,
   typeof lmsNetChart !== 'undefined' ? lmsNetChart : null,
   typeof lmsTpsChart !== 'undefined' ? lmsTpsChart : null,
   typeof lmsIoChart !== 'undefined' ? lmsIoChart : null,
   typeof lmsDiskUsageChart !== 'undefined' ? lmsDiskUsageChart : null,
   typeof lmsGpuChart !== 'undefined' ? lmsGpuChart : null]
    .forEach(_clearChart);
}

// Carry-forward state for the sparse gen-total llama chart, shared by
// backfill and live push.
let _genTokensCarry = 0;

// Fetch a /api/history* row array, returning null on any failure. An auth-gated
// 401 returns a JSON object, so a shape check is required, not just r.ok.
async function _historyRows(url, label) {
  try {
    const r = await fetch(url);
    if (!r.ok) {
      console.error(`${label} history: HTTP ${r.status}`);
      return null;
    }
    const rows = await r.json();
    if (!Array.isArray(rows)) {
      console.error(`${label} history: expected an array, got`, rows);
      return null;
    }
    return rows;
  } catch (e) {
    console.error(`${label} history error:`, e);
    return null;
  }
}

let _histGen = 0, _histLastAgent;
async function loadHistory() {
  // Generation counter: only the newest in-flight backfill may paint (#267).
  const gen = ++_histGen;
  try {
    // Backfill the (picker-)selected llama agent's host history. No selection
    // (single-agent install) → plain /api/history = the default-agent ring,
    // byte-identical to pre-multi-agent.
    const sel = (typeof _selectedAgent === 'function') ? _selectedAgent('llama') : null;
    // Clear before the fetch only on an agent change (#121); a same-agent
    // re-entry keeps its live data if the fetch fails (#507).
    if (sel !== _histLastAgent) {
      _resetMetricCharts();
      _llamaPeaks.tps.reset();
      _llamaPeaks.pps.reset();
    }
    _histLastAgent = sel;
    const url = sel ? `/api/history?agent=${encodeURIComponent(sel)}` : '/api/history';
    const rows = await _historyRows(url, 'llama');
    if (gen !== _histGen) return;
    if (!rows || !rows.length) return;
    // Clear again after the await: a live fetchMetrics tick can append a
    // current-time point during the fetch, which the bucketed backfill would
    // otherwise collapse onto (charts start at "now" on agent switch) (#137).
    _resetMetricCharts();
    // Convert bytes-per-second → MiB-per-second so backfill points match the
    // live-fetch unit (see net/io conversion in fetchMetrics around line 3550).
    const B_PER_MIB = 1048576;
    const _peakSeedTs = LMPeaks.rowClock(rows, Date.now());
    _genTokensCarry = 0;
    let _sawIscsiHistory = false;
    for (const r of rows.slice(-MAX_POINTS)) {
      pushPoint(cpuChart,  r.ts, r.cpu_total   || 0);
      pushPoint(ramChart,  r.ts, r.ram_percent || 0);
      pushPoint(gpuChart,  r.ts, r.gpu_util    || 0);
      pushPoint(netChart,  r.ts, ((r.net_sent || 0) + (r.net_recv || 0)) / B_PER_MIB);
      pushDual(llamaChart, r.ts, r.llama_tps,  r.llama_pps);
      _llamaPeaks.tps.push(_peakSeedTs(r.ts), r.llama_tps);
      _llamaPeaks.pps.push(_peakSeedTs(r.ts), r.llama_pps);
      pushDual(ioChart,    r.ts, (r.io_read  || 0) / B_PER_MIB,
                                  (r.io_write || 0) / B_PER_MIB);
      // Hardware sensor charts (AIO liquid temp + PSU power draw).
      // Skip the push when the field is undefined so the line stays at
      // its previous value instead of dropping to 0.
      if (typeof aioTempChart !== 'undefined' && r.aio_temp != null)
        pushPoint(aioTempChart, r.ts, r.aio_temp);
      if (typeof psuPowerChart !== 'undefined' && (r.psu_out != null || r.psu_in != null))
        pushDual(psuPowerChart, r.ts, r.psu_out || 0, r.psu_in || 0);
      // Detailed llama charts — the server-card throughput spark mirrors
      // llamaChart; gen total carries its last value across idle rows.
      if (typeof llamaSrvChart !== 'undefined') pushDual(llamaSrvChart, r.ts, r.llama_tps, r.llama_pps, _LLAMA_SRV_BUCKET_MS, 'max');
      if (r.llama_gen_tokens != null) _genTokensCarry = r.llama_gen_tokens;
      if (typeof genTokensChart !== 'undefined') pushPoint(genTokensChart, r.ts, _genTokensCarry);
      // Disk usage — / and /mnt/iscsi percent over time.
      if (typeof diskUsageChart !== 'undefined'
          && (r.disk_root_pct != null || r.disk_iscsi_pct != null)) {
        if (r.disk_iscsi_pct != null) _sawIscsiHistory = true;
        pushDual(diskUsageChart, r.ts,
                 r.disk_root_pct  != null ? r.disk_root_pct  : 0,
                 r.disk_iscsi_pct != null ? r.disk_iscsi_pct : 0);
      }
    }
    _setIscsiSeriesVisible(_sawIscsiHistory);
  } catch(e) { console.error('History error:', e); }
}

// Backfill manager + alarm-engine self-monitor charts from the alarm engine
// catalog. Fired at startup and on every manager-tab entry (#506).
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

  // Timestamp alignment lives in lib/series.js (shared with the unit tests).
  const zipByTs = LMSeries.zipByTs;

  // Manager Perf (2 series)
  if (typeof mgrPerfChart !== 'undefined' && mgrPerfChart) {
    const [api, hist] = await Promise.all([
      fetchPoints('manager_api_latency_ms'),
      fetchPoints('manager_history_latency_ms'),
    ]);
    const rows = zipByTs([api, hist]);
    if (rows.length) {
      _clearChart(mgrPerfChart);  // discard any racing live point (#137)
      for (const [ts, vals] of rows) pushMulti(mgrPerfChart, ts, vals);
    }
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
    const rows = zipByTs(series);
    if (rows.length) {
      _clearChart(aePerfChart);  // discard any racing live point (#137)
      for (const [ts, vals] of rows) pushMulti(aePerfChart, ts, vals);
    }
  }
}

// Per-provider /api/history?agent= chart backfill factory: generation guard so
// only the newest in-flight call paints, then repaint the last MAX_POINTS rows.
function _makeHistoryBackfill(provider, defaultAgentKey, resetCharts, paintRow) {
  let gen = 0, lastAgent;
  return async function () {
    const g = ++gen;
    const sel = (typeof _selectedAgent === 'function') ? _selectedAgent(provider) : null;
    const agent = sel || window[defaultAgentKey];
    // Clear before the fetch only when the agent changed (#121). On a
    // same-agent re-entry a failed fetch would blank good live data (#507).
    if (agent !== lastAgent) resetCharts();
    lastAgent = agent;
    if (!agent) return;
    const rows = await _historyRows(
      `/api/history?agent=${encodeURIComponent(agent)}`, provider);
    if (g !== gen) return;
    if (rows && rows.length) {
      resetCharts();
      for (const r of rows.slice(-MAX_POINTS)) paintRow(r);
    }
  };
}

// Backfill the LM Studio host + gateway-throughput charts from the selected
// LMS agent's history. Makes no llama calls — Overall backfills separately.
const loadLmsHistory = _makeHistoryBackfill('lms', '__LMS_AGENT',
  () => _resetLmsCharts(),
  (r) => {
    const B_PER_MIB = 1048576;
    if (typeof lmsCpuChart !== 'undefined' && lmsCpuChart && r.cpu_total != null)
      pushPoint(lmsCpuChart, r.ts, r.cpu_total);
    if (typeof lmsRamChart !== 'undefined' && lmsRamChart && r.ram_percent != null)
      pushPoint(lmsRamChart, r.ts, r.ram_percent);
    if (typeof lmsNetChart !== 'undefined' && lmsNetChart)
      pushDual(lmsNetChart, r.ts,
        r.net_sent != null ? r.net_sent / B_PER_MIB : null,
        r.net_recv != null ? r.net_recv / B_PER_MIB : null);
    if (typeof lmsIoChart !== 'undefined' && lmsIoChart
        && (r.io_read != null || r.io_write != null))
      pushDual(lmsIoChart, r.ts,
        (r.io_read || 0) / B_PER_MIB, (r.io_write || 0) / B_PER_MIB);
    if (typeof lmsDiskUsageChart !== 'undefined' && lmsDiskUsageChart
        && r.disk_root_pct != null)
      pushPoint(lmsDiskUsageChart, r.ts, r.disk_root_pct);
    if (typeof lmsGpuChart !== 'undefined' && lmsGpuChart
        && r.mac_gpu_busy != null)
      pushPoint(lmsGpuChart, r.ts, r.mac_gpu_busy);
    if (typeof lmsTpsChart !== 'undefined' && lmsTpsChart
        && (r.lms_tps != null || r.lms_pps != null))
      pushDual(lmsTpsChart, r.ts, r.lms_tps || 0, r.lms_pps || 0, GW_RATE_BUCKET_MS);
  });

// Backfill the vLLM KV-cache/throughput + host (CPU/RAM/Net/IO/disk) charts
// from the selected vLLM agent's history (#358, #502).
const loadVllmHistory = _makeHistoryBackfill('vllm', '__VLLM_AGENT',
  () => { if (typeof _resetVllmCharts === 'function') _resetVllmCharts(); },
  (r) => {
    const B_PER_MIB = 1048576;
    if (typeof vllmKvChart !== 'undefined' && vllmKvChart && r.vllm_kv != null)
      pushPoint(vllmKvChart, r.ts, r.vllm_kv);
    if (typeof vllmTpsChart !== 'undefined' && vllmTpsChart
        && (r.vllm_tps != null || r.vllm_pps != null))
      pushDual(vllmTpsChart, r.ts, r.vllm_tps || 0, r.vllm_pps || 0);
    if (typeof vllmCpuChart !== 'undefined' && vllmCpuChart && r.cpu_total != null)
      pushPoint(vllmCpuChart, r.ts, r.cpu_total);
    if (typeof vllmRamChart !== 'undefined' && vllmRamChart && r.ram_percent != null)
      pushPoint(vllmRamChart, r.ts, r.ram_percent);
    if (typeof vllmNetChart !== 'undefined' && vllmNetChart)
      pushDual(vllmNetChart, r.ts,
        r.net_sent != null ? r.net_sent / B_PER_MIB : null,
        r.net_recv != null ? r.net_recv / B_PER_MIB : null);
    if (typeof vllmIoChart !== 'undefined' && vllmIoChart
        && (r.io_read != null || r.io_write != null))
      pushDual(vllmIoChart, r.ts,
        (r.io_read || 0) / B_PER_MIB, (r.io_write || 0) / B_PER_MIB);
    if (typeof vllmDiskUsageChart !== 'undefined' && vllmDiskUsageChart
        && r.disk_root_pct != null)
      pushPoint(vllmDiskUsageChart, r.ts, r.disk_root_pct);
  });

// Last-fetched hero history + hourly-energy rows; _ovHeroRender re-buckets
// from these without refetching.
let _ovHeroRows = null;
let _ovEnergyRows = null;
let _ovHistoryGen = 0;

// Backfill the Overall-tab hero (cross-provider Gen / Prompt totals) from
// fleet=all history. Called only from Overall-tab entry and refocus (#506).
async function loadOverallHistory() {
  if (typeof ovHeroChart === 'undefined' || !ovHeroChart) return;
  const gen = ++_ovHistoryGen;
  const rows = await _historyRows('/api/history?since_minutes=1440&max_rows=1440&fleet=all', 'Overall fleet');
  if (gen !== _ovHistoryGen) return;  // only the newest in-flight call paints
  if (!rows || !rows.length) return;
  _ovHeroRows = rows;
  _ovHeroRender();
  _ovLoadEnergyOverlay();
}

// Repaint the hero from the cached rows at the current bucket width.
// Overlay datasets fill here only; live polls advance just gen/prompt.
function _ovHeroRender() {
  if (typeof ovHeroChart === 'undefined' || !ovHeroChart || !_ovHeroRows) return;
  _clearChart(ovHeroChart);  // discard any racing live point (#137)
  const bucketMs = ovHeroBucketMs();
  const l = ovHeroChart.data.labels;
  const d0 = ovHeroChart.data.datasets[0].data, d1 = ovHeroChart.data.datasets[1].data;
  const dsPower = ovHeroChart.data.datasets[2];
  for (const p of OV.heroSeries(_ovHeroRows, bucketMs).slice(-MAX_POINTS)) {
    if (p.gen == null && p.prompt == null && p.power == null) continue;
    l.push(new Date(p.ts));
    d0.push(p.gen || 0);
    d1.push(p.prompt || 0);
    if (dsPower) dsPower.data.push(p.power != null ? p.power : null);
  }
  _ovRenderEnergyOverlay();
  ovHeroChart.update('none');
}

// Fetches /api/energy/hourly into the cache, then paints the overlay.
async function _ovLoadEnergyOverlay() {
  try {
    const d = await fetch('/api/energy/hourly?hours=24').then(r => r.json());
    if (!d.ok) return;
    _ovEnergyRows = d.rows;
    _ovRenderEnergyOverlay();
    if (ovHeroChart) ovHeroChart.update('none');
  } catch (_) {}
}

// Maps the cached hourly-energy rows onto the hero's current labels.
function _ovRenderEnergyOverlay() {
  if (!ovHeroChart || !ovHeroChart.data.datasets[3] || !_ovEnergyRows) return;
  const labels = ovHeroChart.data.labels.map(t => t.getTime());
  ovHeroChart.data.datasets[3].data = OV.energySeries(_ovEnergyRows, labels);
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
    _setCardTitle('gpuCardTitle',   g.name, 'GPU', 'gpu');
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

    // Disk usage — the iSCSI series/row only render when the agent actually
    // discovered a /mnt/iscsi mount (or reports a live session).
    let _hasIscsiMount = false;
    if (m.disk && m.disk.length) {
      document.getElementById('diskList').innerHTML = m.disk.map(d =>
        `<div class="disk-row">
          <span style="min-width:100px;color:var(--fg-muted);font-size:0.8em">${_esc(d.mountpoint)}</span>
          <div class="disk-bar"><div class="disk-fill" style="width:${Number(d.percent)}%"></div></div>
          <span>${d.percent.toFixed(1)}%</span>
        </div>`
      ).join('');
      const byMount = Object.fromEntries(m.disk.map(d => [d.mountpoint, d.percent]));
      _hasIscsiMount = byMount['/mnt/iscsi'] != null;
      pushDual(diskUsageChart, ts,
        byMount['/'] || 0,
        byMount['/mnt/iscsi'] || 0,
      );
      _setIscsiSeriesVisible(_hasIscsiMount);
    }

    // iSCSI status line — hidden entirely for agents with no session/mount.
    const isc = m.iscsi || {};
    const iscsiRowEl = document.getElementById('iscsiRow');
    if (iscsiRowEl) iscsiRowEl.style.display = (isc.state || _hasIscsiMount) ? '' : 'none';
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
    const llModelClean = _cleanLlamaModelName(ll.model);
    // Track model name for state pill — only update when awake with a real name
    if (llModelClean && !sleeping) {
      _pillModelName = llModelClean.split('/').pop() || llModelClean;
    }
    modelEl.textContent = llModelClean || 'No model loaded';
    modelEl.style.color = sleeping ? '#444' : '#aaa';
    modelEl.title = sleeping ? 'Model is sleeping — metrics polling paused' : '';
    _llamaPeaks.tps.push(Date.now(), ll.tokens_per_second);
    _llamaPeaks.pps.push(Date.now(), ll.prompt_tokens_per_second);
    _setLivePeak('llamaTps', fmtLivePeak(ll.tokens_per_second, _llamaPeaks.tps));
    _setLivePeak('llamaPps', fmtLivePeak(ll.prompt_tokens_per_second, _llamaPeaks.pps));
    document.getElementById('llamaGenTokens').textContent    = ll.total_tokens_generated   != null ? ll.total_tokens_generated.toLocaleString() : '—';
    document.getElementById('llamaPromptTokens').textContent = ll.total_tokens_prompted    != null ? ll.total_tokens_prompted.toLocaleString() : '—';
    document.getElementById('llamaDecodes').textContent      = ll.n_decode_total           != null ? ll.n_decode_total.toLocaleString() : '—';
    document.getElementById('llamaBusySlots').textContent    = ll.n_busy_slots_per_decode  != null ? ll.n_busy_slots_per_decode.toFixed(2) : '—';
    document.getElementById('llamaCtxHigh').textContent      = ll.n_tokens_max             != null ? ll.n_tokens_max.toLocaleString() : '—';
    document.getElementById('llamaKvRatio').textContent      = ll.kv_cache_usage_ratio     != null ? (ll.kv_cache_usage_ratio * 100).toFixed(1) + '%' : '—';
    document.getElementById('llamaKvTokens').textContent     = ll.kv_cache_tokens          != null ? ll.kv_cache_tokens.toLocaleString() : '—';
    document.getElementById('llamaNRemain').textContent      = ll.n_remain                 != null ? ll.n_remain.toLocaleString() : '—';
    document.getElementById('llamaTotalSlots').textContent   = ll.total_slots              != null ? ll.total_slots.toLocaleString() : '—';
    const modsEl = document.getElementById('llamaModalities');
    if (ll.modalities && typeof ll.modalities === 'object') {
      const on = Object.keys(ll.modalities).filter(k => ll.modalities[k]);
      modsEl.textContent = on.length ? on.join(', ') : 'text';
    } else {
      modsEl.textContent = '—';
    }
    const tmplEl = document.getElementById('llamaChatTemplate');
    if (ll.chat_template_len != null) {
      tmplEl.textContent = (ll.chat_template_len / 1024).toFixed(1) + ' KB';
      tmplEl.title = (ll.chat_template || '').slice(0, 600);
    } else {
      tmplEl.textContent = '—';
      tmplEl.title = '';
    }
    const _prevLlamaActive = _llamaActiveSlots > 0;
    _llamaActiveSlots = ll.active_slots || 0;
    if ((_llamaActiveSlots > 0) !== _prevLlamaActive) renderModelCards();
    // Repopulate per-card avg gen/prompt t/s each frame so a slot-flip
    // re-render never leaves them blank and they track the live metric.
    if (typeof _updateModelPerf === 'function') _updateModelPerf();
    updateNonZero('active_slots',        ll.active_slots);
    updateNonZero('requests_processing', ll.requests_processing);
    updateNonZero('requests_deferred',   ll.requests_deferred);
    document.getElementById('llamaSlots').innerHTML      = fmtWithPeak(ll.active_slots,        'active_slots');
    document.getElementById('llamaProcessing').innerHTML = fmtWithPeak(ll.requests_processing, 'requests_processing');
    document.getElementById('llamaDeferred').innerHTML   = fmtWithPeak(ll.requests_deferred,   'requests_deferred');
    pushDual(llamaChart, ts, ll.tokens_per_second, ll.prompt_tokens_per_second);
    pushDual(llamaSrvChart, ts, ll.tokens_per_second, ll.prompt_tokens_per_second, _LLAMA_SRV_BUCKET_MS, 'max');
    if (ll.total_tokens_generated != null) _genTokensCarry = ll.total_tokens_generated;
    pushPoint(genTokensChart, ts, _genTokensCarry);

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
  }
}

// ---------------------------------------------------------------------------
// Alarm-rule threshold lines (chartjs-plugin-annotation)
// ---------------------------------------------------------------------------
// Maps each dashboard chart id to its alarm metric (source + metric_name) and
// the provider whose selected agent supplies the host for host-scoped rules.
// LM Studio GPU busy lives under mac_power — that host emits no system/gpu_*.
const CHART_METRIC = {
  cpuChart:           { source: 'system',    metric_name: 'cpu_total',            provider: 'llama' },
  ramChart:           { source: 'system',    metric_name: 'ram_percent',          provider: 'llama' },
  gpuChart:           { source: 'system',    metric_name: 'gpu_gpu_util_percent', provider: 'llama' },
  diskUsageChart:     { source: 'system',    metric_name: 'disk_root_percent',    provider: 'llama' },
  lmsCpuChart:        { source: 'system',    metric_name: 'cpu_total',            provider: 'lms' },
  lmsRamChart:        { source: 'system',    metric_name: 'ram_percent',          provider: 'lms' },
  lmsDiskUsageChart:  { source: 'system',    metric_name: 'disk_root_percent',    provider: 'lms' },
  lmsGpuChart:        { source: 'mac_power', metric_name: 'gpu_busy_pct',         provider: 'lms' },
  vllmCpuChart:       { source: 'system',    metric_name: 'cpu_total',            provider: 'vllm' },
  vllmRamChart:       { source: 'system',    metric_name: 'ram_percent',          provider: 'vllm' },
  vllmDiskUsageChart: { source: 'system',    metric_name: 'disk_root_percent',    provider: 'vllm' },
};
let _alarmRules = [];

// Fetch the alarm engine's rules through the manager proxy, then redraw lines.
async function refreshAlarmRules() {
  try {
    const r = await _fetchT('/api/alarm/rules?limit=1000', {}, 8000);
    if (!r.ok) { console.warn('alarm rules fetch returned', r.status); return; }
    const data = await r.json();
    _alarmRules = Array.isArray(data) ? data : [];
    _applyThresholds();
  } catch (_) {}
}

// Hostname of the selected agent for `provider` — each dashboard's system
// metrics follow its own picker.
function _thresholdHost(provider) {
  try {
    const p = provider || 'llama';
    const id = (typeof _selectedAgent === 'function') ? _selectedAgent(p) : null;
    const a = ((window._agentsByProvider || {})[p] || []).find(x => x.agent_id === id);
    return a ? (a.hostname || null) : null;
  } catch (_) { return null; }
}

// Redraw threshold lines on every mapped chart, host-scoped per provider.
function _applyThresholds() {
  if (!(window.Chart && Chart.instances && window.Thresholds)) return;
  const hostCache = {};
  Object.values(Chart.instances).forEach(c => {
    const meta = c && c.canvas && CHART_METRIC[c.canvas.id];
    if (!meta || !(c.options.plugins && c.options.plugins.annotation)) return;
    const p = meta.provider || 'llama';
    if (!(p in hostCache)) hostCache[p] = _thresholdHost(meta.provider);
    try {
      c.options.plugins.annotation.annotations = Thresholds.thresholdAnnotations(_alarmRules, { source: meta.source, metricName: meta.metric_name, host: hostCache[p], hostWildcard: false });
      c.update('none');
    } catch (_) {}
  });
}
window._applyThresholds = _applyThresholds;
window.refreshAlarmRules = refreshAlarmRules;

// Double-click any chart canvas to reset its zoom/pan to the full range.
document.addEventListener('dblclick', (e) => {
  if (!(window.Chart && Chart.instances)) return;
  const inst = Object.values(Chart.instances).find(c => c.canvas === e.target);
  if (inst && typeof inst.resetZoom === 'function') {
    try { inst.resetZoom(); } catch (_) {}
    _syncResetZoomBtn(inst);
  }
});

