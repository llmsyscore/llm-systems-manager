// ---------------------------------------------------------------------------
// Drag and drop
// ---------------------------------------------------------------------------
function initSortable() {
  // Pinned host page panels move by their own kit grip.
  Sortable.create(document.getElementById('overallGrid'), {
    handle: '.card-handle, .grip', animation: 150, ghostClass: 'sortable-ghost',
    onEnd: () => {
      layout.overallOrder = [...document.querySelectorAll('#overallGrid > [data-card]')]
        .map(c => c.dataset.card);
      saveLayout();
    },
  });
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
// Flow engine: rows follow content, so the cycle only sets width.
const _FLOW_SIZE_CYCLE = ['auto', '1x1', '2x1', '3x1'];
const _CARD_SIZE_CLASSES = ['size-1x1','size-auto','size-2x1','size-3x1','size-4x1','size-1x2','size-2x2','size-3x2'];
// Size a card (or borrowed shell) takes when the layout has none saved for it.
const _CARD_DEFAULT_SIZE = 'auto';
function _defaultCardSize(_id) {
  return _CARD_DEFAULT_SIZE;
}
function _sizeCols(size) {
  return size === 'auto' ? 1 : Number(String(size).split('x')[0]);
}
function _sizeLabel(size, flow) {
  if (size === 'auto') return flow ? 'auto (preset width)' : 'auto (content height)';
  return flow ? `${_sizeCols(size)} wide` : String(size).replace('x', '×');
}
// Sizes that fit the card's grid, in cycle order.
function _allowedSizes(card) {
  const grid = card.parentElement;
  const maxCols = grid ? _gridColCount(grid) : 3;
  const cycle = _isFlowGrid(grid) ? _FLOW_SIZE_CYCLE : _CARD_SIZE_CYCLE;
  return cycle.filter(s => _sizeCols(s) <= maxCols);
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
// Flow keeps only the width of a saved size ('2x2' → '2x1'); rows follow content.
function _flowSize(size) {
  return size === 'auto' ? 'auto' : `${_sizeCols(size)}x1`;
}
function _applyCardSize(card, size) {
  _CARD_SIZE_CLASSES.forEach(c => card.classList.remove(c));
  const grid = card.parentElement;
  const maxCols = grid ? _gridColCount(grid) : 3;
  const flow = _isFlowGrid(grid);
  const eff = flow ? _flowSize(_clampSize(size, maxCols)) : _clampSize(size, maxCols);
  card.dataset.size = eff;
  if (flow) {
    const w = eff === 'auto' ? _flowPresetWidth(card, maxCols) : Math.min(maxCols, _sizeCols(eff));
    card.style.gridColumn = w > 1 ? `span ${w}` : '';
    _flowObserve(card);
    _flowMeasure(card);
  } else {
    card.style.gridColumn = '';
    card.style.gridRow = '';
    if (eff !== '1x1') card.classList.add('size-' + eff);
  }
  const btn = card.querySelector(':scope > .card-size-btn');
  if (btn) {
    btn.dataset.tip = `Card size: ${_sizeLabel(eff, flow)} · click for ${_sizeLabel(_nextCardSize(card, eff), flow)}`;
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
// Kit panels (host pages, All hosts, Overall rows) keep the kit's own layout; only old-style cards get a size.
function initCardResize() {
  document.querySelectorAll('[data-card]').forEach(card => {
    const id = card.dataset.card; if (!id) return;
    if (card.matches('section.panel') || card.closest('.hp-root, #dash-all')) return;
    _ensureSizeBtn(card);
    _applyCardSize(card, _sizeMapFor(id)[id] || _defaultCardSize(id));
  });
}

// ----- Flow engine (#823) ------------------------------------------
// 8px row tracks; each card's row span is computed from its rendered height.
function _isFlowGrid(grid) {
  return !!(grid && grid.classList && grid.classList.contains('flow'));
}
function _cardGrids() {
  return Object.values(SettingsLib.CARD_PAGES).map(p => document.getElementById(p.grid)).filter(Boolean);
}
function _gridPageKey(grid) {
  const hit = Object.entries(SettingsLib.CARD_PAGES).find(([, p]) => grid && p.grid === grid.id);
  return hit ? hit[0] : null;
}
function _rolePresetFor(grid) {
  const key = _gridPageKey(grid);
  const map = (layout && layout.rolePreset) || {};
  return SettingsLib.normalizeRolePreset(key ? map[key] : null);
}
// The page's hero card while visible, else its first visible card.
function _flowHeroId(grid) {
  const key = _gridPageKey(grid);
  const fixed = key && SettingsLib.HERO_CARDS[key];
  const visible = [...grid.querySelectorAll(':scope > [data-card]')].filter(c => c.style.display !== 'none');
  const hero = fixed && visible.find(c => c.dataset.card === fixed);
  return hero ? fixed : (visible[0] ? visible[0].dataset.card : null);
}
function _flowPresetWidth(card, maxCols) {
  const grid = card.parentElement;
  const id = card.dataset.card;
  const preset = _rolePresetFor(grid);
  return SettingsLib.roleWidth(preset, SettingsLib.roleOf(id), id === _flowHeroId(grid), maxCols);
}
function _flowMetrics(grid) {
  const cs = getComputedStyle(grid);
  const gap = parseFloat(cs.rowGap);
  return { unit: parseFloat(cs.gridAutoRows) || SettingsLib.FLOW_UNIT_PX,
           gap: Number.isFinite(gap) ? gap : SettingsLib.FLOW_ROW_GAP_PX };
}
// Row span from the card's current content height; hidden cards get none.
function _flowMeasure(card, metrics) {
  const grid = card.parentElement;
  if (!_isFlowGrid(grid)) return;
  if (card.style.display === 'none') { card.style.gridRow = ''; return; }
  const h = card.offsetHeight;
  if (!h) return;
  const { unit, gap } = metrics || _flowMetrics(grid);
  const span = `span ${SettingsLib.flowSpan(h, unit, gap)}`;
  if (card.style.gridRow !== span) card.style.gridRow = span;
}
let _flowCardRO = null;
let _flowGridRO = null;
const _flowGridWidths = new WeakMap();
function _flowObserve(card) {
  if (typeof ResizeObserver === 'undefined') return;
  if (!_flowCardRO) {
    _flowCardRO = new ResizeObserver(entries => {
      const metrics = new Map();
      for (const e of entries) {
        const grid = e.target.parentElement;
        if (!_isFlowGrid(grid)) continue;
        if (!metrics.has(grid)) metrics.set(grid, _flowMetrics(grid));
        _flowMeasure(e.target, metrics.get(grid));
      }
    });
  }
  _flowCardRO.observe(card);
}
function _flowUnobserve(el) {
  if (_flowCardRO) _flowCardRO.unobserve(el);
}
// A grid whose width changed (window resize, auto-fit, tab shown) re-clamps
// every card's span against the new track count.
function _flowObserveGrid(grid) {
  if (typeof ResizeObserver === 'undefined') return;
  if (!_flowGridRO) {
    _flowGridRO = new ResizeObserver(entries => {
      for (const e of entries) {
        const g = e.target;
        const w = Math.round(e.contentRect.width);
        if (_flowGridWidths.get(g) === w) continue;
        _flowGridWidths.set(g, w);
        if (w && _isFlowGrid(g)) _relayoutGridCards(g);
      }
    });
  }
  _flowGridRO.observe(grid);
}
// Re-apply every card's saved (or default) size in one grid.
function _relayoutGridCards(grid) {
  if (!grid) return;
  grid.querySelectorAll(':scope > [data-card]').forEach(card => {
    const id = card.dataset.card;
    _applyCardSize(card, _sizeMapFor(id)[id] || _defaultCardSize(id));
  });
}
// Switch every card grid between the stretch grid and the flow engine.
function applyLayoutEngine(engine, save) {
  const eng = SettingsLib.normalizeEngine(engine);
  layout.layoutEngine = eng;
  _cardGrids().forEach(grid => {
    const flow = eng === 'flow';
    const changed = _isFlowGrid(grid) !== flow;
    grid.classList.toggle('flow', flow);
    if (flow) _flowObserveGrid(grid);
    else if (_flowGridRO) {
      _flowGridRO.unobserve(grid);
      grid.querySelectorAll('[data-card]').forEach(_flowUnobserve);
    }
    if (flow || changed) _relayoutGridCards(grid);
  });
  if (save) {
    saveLayout();
    if (typeof renderSettingsPanel === 'function') renderSettingsPanel();
  }
}
function applyDensity(density, save) {
  const d = SettingsLib.normalizeDensity(density);
  layout.density = d;
  document.documentElement.dataset.density = d;
  _cardGrids().forEach(grid => { if (_isFlowGrid(grid)) _relayoutGridCards(grid); });
  _resizeChartsIn(document.body);
  if (save) {
    saveLayout();
    if (typeof renderSettingsPanel === 'function') renderSettingsPanel();
  }
}
// Role preset for the active page (flow engine): clears per-card overrides
// so every card follows its role width.
function applyRolePreset(presetId) {
  const ks = _activeTabLayoutKeys(); if (!ks || !ks.grid) return;
  const key = _gridPageKey(ks.grid); if (!key) return;
  if (!layout.rolePreset || typeof layout.rolePreset !== 'object') layout.rolePreset = {};
  layout.rolePreset[key] = SettingsLib.normalizeRolePreset(presetId);
  _clearPageSizeOverrides(ks);
  saveLayout();
  _relayoutGridCards(ks.grid);
  if (typeof renderSettingsPanel === 'function') renderSettingsPanel();
}
// Drop every per-card size override on the active page (Overall keys its shells ov-*).
function _clearPageSizeOverrides(ks) {
  const ids = new Set(Object.keys(ks.map));
  ks.grid.querySelectorAll(':scope > [data-card]').forEach(c => ids.add(c.dataset.card));
  for (const id of ids) delete _sizeMapFor(id)[id];
  if (_activeTab === 'overall' && layout.cardSizes) {
    for (const id of Object.keys(layout.cardSizes)) if (id.startsWith('ov-')) delete layout.cardSizes[id];
  }
}

// ----- Active-tab layout key resolver (single source of truth) -----
// Page → layout keys come from SettingsLib.CARD_PAGES; null off card pages.
function _activeTabLayoutKeys() {
  const sc = _sdScope();
  if (sc.kind !== 'cards') return null;
  return { label: sc.label, map: _sdCardMap(sc), hidden: sc.hidden, order: sc.order,
           cols: sc.cols, borrowed: sc.borrowed, grid: document.getElementById(sc.grid) };
}

// ----- Layout presets ----------------------------------------------
// Presets live in js/lib/settingsdrawer.js: column count + per-visible-index
// sizes for the active page. 'auto-uniform' = auto-fit columns, all 1x1.
const LAYOUT_PRESETS = SettingsLib.PRESETS;
function applyLayoutPreset(presetId) {
  const ks = _activeTabLayoutKeys(); if (!ks || !ks.grid) return;
  const preset = SettingsLib.getPreset(presetId === 'auto-uniform' ? 'auto' : (LAYOUT_PRESETS[presetId] || {}).cols, presetId);
  if (!preset) return;
  layout[ks.cols] = preset.cols;
  ks.grid.style.gridTemplateColumns = SettingsLib.gridTemplate(preset.cols);
  _clearPageSizeOverrides(ks);
  const visible = _sdVisibleCards(ks);
  Object.entries(preset.sizes).forEach(([idx, size]) => {
    const card = visible[Number(idx)];
    if (card) _sizeMapFor(card.dataset.card)[card.dataset.card] = size;
  });
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
  const host = chart.canvas.closest('.card') || chart.canvas.parentElement;
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
  _pushUpdate(chart);
}

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


// Backfill batching: inside _withPushBatch the push helpers collect the
// charts they touched and redraw each once when the batch closes.
let _pushBatch = null;
function _withPushBatch(fn) {
  const outer = _pushBatch;
  _pushBatch = outer || new Set();
  try { fn(); } finally {
    if (!outer) { const b = _pushBatch; _pushBatch = null; b.forEach(_redrawChart); }
  }
}
// Redraws through the visibility gate in foundation.js when it is loaded.
function _redrawChart(ch) {
  if (typeof window._chartUpdate === 'function') window._chartUpdate(ch); else ch.update('none');
}
function _pushUpdate(chart) {
  if (_pushBatch) _pushBatch.add(chart); else _redrawChart(chart);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const lastNonZero = {
  active_slots: { val: null, ts: null },
  requests_processing: { val: null, ts: null },
  requests_deferred: { val: null, ts: null },
};

// llama Gen/Prompt tokens/s peak trackers, read by the llama.cpp host page;
// all samples carry browser-clock timestamps.
const _LLAMA_PEAK_WINDOW_MS = LMPeaks.RATE_WINDOW_MS;
const _llamaPeaks = {
  tps: LMPeaks.makeTracker(_LLAMA_PEAK_WINDOW_MS),
  pps: LMPeaks.makeTracker(_LLAMA_PEAK_WINDOW_MS),
};

function updateNonZero(key, val) {
  if (val !== null && val !== 0) lastNonZero[key] = { val, ts: new Date().toISOString() };
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

// Aggregate from the payload, else projected from the legacy binary state + model.
function _llamaAggregateOf(data) {
  const agg = data && data.aggregate;
  if (agg) return agg;
  const st = (data && data.state) || 'unknown';
  const hasModel = !!_cleanLlamaModelName(data && data.model);
  if (st === 'awake') return hasModel ? 'active' : 'idle';
  if (st === 'sleeping') return 'sleeping';
  return 'off';
}

// Power pill + ⋯ menu check from the agent's arbiter snapshot (null on old agents).
function _renderLlamaPowerPill(p, stale) {
  const pill = document.getElementById('llamaPowerPill');
  const btns = { performance: document.getElementById('llamaBtnPerfPerformance'),
                 powersave: document.getElementById('llamaBtnPerfPowersave'),
                 auto: document.getElementById('llamaBtnPerfAuto') };
  const current = (p && p.enabled !== false)
    ? ((p.owner === 'manual' && (p.desired || p.applied)) ? (p.desired || p.applied) : 'auto') : null;
  Object.keys(btns).forEach((k) => { if (btns[k]) btns[k].classList.toggle('is-current', k === current); });
  if (!pill) return;
  if (!p || typeof p !== 'object') { pill.hidden = true; return; }
  pill.hidden = false;
  const cap = (s) => s ? s.charAt(0).toUpperCase() + s.slice(1) : '—';
  let cls = 'p-unloaded', text, title = '';
  if (p.enabled === false) {
    text = 'Power · off';
    title = 'perf controller disabled on this agent (PERF_CONTROLLER_ENABLED)';
  } else {
    const applied = p.applied || null;
    const icon = applied === 'performance' ? '⌁ ' : (applied === 'powersave' ? '☾ ' : '');
    const owner = p.owner === 'policy' ? 'auto' : (p.owner || 'auto');
    const words = { verified: 'verified', applied_unverifiable: 'unverifiable', failed: 'failed', deferred: 'deferred' };
    const parts = [icon + cap(applied), owner];
    if (p.outcome && words[p.outcome]) parts.push(words[p.outcome]);
    if (p.desired && p.desired !== applied) parts.push('→ ' + p.desired + '…');
    text = parts.join(' · ');
    cls = p.outcome === 'failed' ? 'p-failed'
      : (applied === 'performance' ? 'p-active' : (applied === 'powersave' ? 'p-sleeping' : 'p-unloaded'));
    const checks = Array.isArray(p.readback) ? p.readback.filter((c) => c && c.actual != null) : [];
    title = checks.length
      ? checks.map((c) => `${c.label}: ${c.actual}${c.ok ? ' ✓' : ' ✗ (want ' + c.expected + ')'}`).join('\n')
      : (p.governor ? `cpu governor: ${p.governor}` : 'nothing to read back on this host');
    if (p.error) title += (title ? '\n' : '') + p.error;
    if (p.mode === 'observe') { title += (title ? '\n' : '') + 'observe mode: reported, never switched'; }
  }
  pill.className = `mc-pill ${cls} llm-sec-pill` + (stale ? ' is-stale' : '');
  pill.textContent = text;
  pill.title = title;
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

    const agg = _llamaAggregateOf(data);
    banner.className = `state-banner state-${agg}` + (data.stale ? ' is-stale' : '');
    banner.title = data.stale
      ? `stale · ${data.age_s != null ? Math.round(data.age_s) + 's' : 'no recent sample'}`
      : '';

    const isLlamaUp = (agg !== 'off' && agg !== 'unknown');
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
        srvStatusEl.className = 'mc-pill p-active llm-sec-pill';
        srvStatusEl.textContent = `Server ON · port ${port}`;
      } else {
        srvStatusEl.className = 'mc-pill p-unloaded llm-sec-pill';
        srvStatusEl.textContent = 'Server OFF';
      }
    }

    _renderLlamaPowerPill(data.power, !!data.stale);

    // Five-state pill: active/loading/sleeping carry the model name, idle and
    // off do not.
    const _aggLabels = { active: 'Active', loading: 'Loading', sleeping: 'Sleeping' };
    if (_aggLabels[agg]) {
      icon.textContent = agg === 'active' ? '●' : '◌';
      text.textContent = 'LLCPP · ' + _aggLabels[agg] + (_pillModelName ? ' · ' + _pillModelName : '');
    } else if (agg === 'idle') {
      icon.textContent = '●';
      text.textContent = 'LLCPP · Idle';
    } else {
      icon.textContent = '○';
      text.textContent = 'LLCPP · Off';
    }

    // Enable/disable llama.cpp server control buttons based on state
    _setLlamaBtns(isLlamaUp);

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
      _bbtn.innerHTML = '<span class="mi">↑</span>';
      _bbtn.appendChild(document.createTextNode(
        'Update llama.cpp' + (_llamaBuildMethod ? ` (${_llamaBuildMethod})` : '')));
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
      if (LivePause.on) return;
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
    LivePause.every(pollServerState, 5000);
  } finally {
    _llamaStartInflight = false;
  }
}
_startLlamaStateStream();
let fetchInterval = 5000;
let fetchTimer = null;

function startFetching(ms) {
  if (fetchTimer) clearInterval(fetchTimer);
  fetchTimer = LivePause.every(fetchMetrics, ms);
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
    window._pollCfg = cfg;
    document.dispatchEvent(new CustomEvent('lsm:pollcfg', { detail: cfg }));
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
    // Tools is one tab with the enabled proxies as sub-tabs; it hides itself
    // when none of them are enabled (#847).
    const ocOn = px.openclaw !== false, chatOn = px.llm_chat !== false, imgOn = px.image_gen !== false;
    toggle('subTabBtnToolsOpenclaw', ocOn);
    toggle('subTabBtnToolsLlmchat',  chatOn);
    toggle('subTabBtnToolsImggen',   imgOn);
    toggle('tabBtnTools',            ocOn || chatOn || imgOn);
    toggle('subTabBtnOpenclaw',      ocOn);

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
    // — a hidden Dashboards page is handled by the sub-page fallback below.
    const activeBtn = document.querySelector('#side [data-tab].on');
    if (activeBtn && activeBtn.style.display === 'none' && !activeBtn.dataset.sub) switchTab('dashboard');

    // Tools sub-tabs: re-point to an enabled proxy, switching only while
    // Tools is on screen.
    const toolsOn = { openclaw: ocOn, llmchat: chatOn, imggen: imgOn };
    if (!toolsOn[_subTabState.tools]) {
      const alt = ['openclaw', 'llmchat', 'imggen'].find(t => toolsOn[t]);
      if (alt && _activeTab === 'tools') switchSubTab('tools', alt);
      else if (alt) _subTabState.tools = alt;
    }

    // Same for Dashboard sub-tabs: if the active sub-tab was hidden, fall
    // back to Manager, which is never hidden.
    const dashOn = { llamacpp: llamaOn, lmstudio: lmsOn, vllm: vllmOn, openclaw: ocOn };
    if (dashOn[_subTabState.dashboard] === false) switchSubTab('dashboard','manager');
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
// appends; inside a push batch the redraw waits for the batch to close.
function _clearChart(ch) {
  if (!ch || !ch.data) return;
  ch.data.labels = [];
  (ch.data.datasets || []).forEach(d => { d.data = []; });
  _pushUpdate(ch);
}

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
// Seeds the llama rate peaks from the selected llama host's last hour of history;
// an agent change clears them first (#121). Only the newest call seeds (#267).
async function loadHistory() {
  const gen = ++_histGen;
  try {
    const sel = (typeof _selectedAgent === 'function') ? _selectedAgent('llama') : null;
    if (sel !== _histLastAgent) {
      _llamaPeaks.tps.reset();
      _llamaPeaks.pps.reset();
    }
    _histLastAgent = sel;
    const mins = Math.ceil(_LLAMA_PEAK_WINDOW_MS / 60000);
    const url = sel ? `/api/history?agent=${encodeURIComponent(sel)}&since_minutes=${mins}` : `/api/history?since_minutes=${mins}`;
    const rows = await _historyRows(url, 'llama');
    if (gen !== _histGen || !rows || !rows.length) return;
    const clock = LMPeaks.rowClock(rows, Date.now());
    for (const r of rows) {
      _llamaPeaks.tps.push(clock(r.ts), r.llama_tps);
      _llamaPeaks.pps.push(clock(r.ts), r.llama_pps);
    }
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
      _withPushBatch(() => {
        _clearChart(mgrPerfChart);  // discard any racing live point (#137)
        for (const [ts, vals] of rows) pushMulti(mgrPerfChart, ts, vals);
      });
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
      _withPushBatch(() => {
        _clearChart(aePerfChart);  // discard any racing live point (#137)
        for (const [ts, vals] of rows) pushMulti(aePerfChart, ts, vals);
      });
    }
  }
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
    const aid = _selectedAgent('llama');
    const m = await _fetchT('/api/metrics', {}, 10000).then(r => r.json());

    window._latestMetric = m;
    window._latestMetricAt = Date.now();
    window._latestMetricAgent = aid;
    // Overall tab is fleet-aggregated (PR4) — refresh it from the fleet
    // endpoints, not this single-agent sample, while it's visible.
    if (document.getElementById('overallTab')?.style.display !== 'none') {
      fetchOverallMetrics();
    }

    // OpenClaw analytics — refresh only when that sub-tab is visible
    if (_activeTab === 'dashboard' && _subTabState.dashboard === 'openclaw') {
      fetchOpenclawAnalytics();
    }

    // Llama: header pill model name, rate peaks, LLM Control model cards
    const ll = m.llama || {};
    const llModelClean = _cleanLlamaModelName(ll.model);
    // Track model name for state pill — only update when awake with a real name
    if (llModelClean && ll.sleeping !== true) {
      _pillModelName = llModelClean.split('/').pop() || llModelClean;
    }
    _llamaPeaks.tps.push(Date.now(), ll.tokens_per_second);
    _llamaPeaks.pps.push(Date.now(), ll.prompt_tokens_per_second);
    const _prevLlamaActive = _llamaActiveSlots > 0;
    _llamaActiveSlots = ll.active_slots || 0;
    if ((_llamaActiveSlots > 0) !== _prevLlamaActive) renderModelCards();
    // Repopulate per-card avg gen/prompt t/s each frame so a slot-flip
    // re-render never leaves them blank and they track the live metric.
    if (typeof _updateModelPerf === 'function') _updateModelPerf();
    updateNonZero('active_slots',        ll.active_slots);
    updateNonZero('requests_processing', ll.requests_processing);
    updateNonZero('requests_deferred',   ll.requests_deferred);
  } catch(e) {
    console.error('Fetch error:', e);
  } finally {
    _release(_mk);
  }
}

// ---------------------------------------------------------------------------
// Alarm-rule threshold lines (chartjs-plugin-annotation)
// ---------------------------------------------------------------------------
// Chart.js canvas id → {source, metric_name, provider} for alarm-rule lines; empty
// while no Chart.js chart draws one (the host pages draw their own, js/hostpage.js).
const CHART_METRIC = {};
let _alarmRules = [];

// Fetch the alarm engine's rules through the manager proxy, then redraw lines.
async function refreshAlarmRules() {
  try {
    const r = await _fetchT('/api/alarm/rules?limit=1000', {}, 8000);
    if (!r.ok) { console.warn('alarm rules fetch returned', r.status); return; }
    const data = await r.json();
    _alarmRules = Array.isArray(data) ? data : [];
    window._alarmRules = _alarmRules;
    _applyThresholds();
    if (typeof hpRender === 'function') hpRender();
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
      _redrawChart(c);
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

