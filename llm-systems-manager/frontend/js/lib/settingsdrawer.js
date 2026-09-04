// Settings drawer data: layout presets per column count, flow-engine roles and
// role presets, theme catalog and the per-page drawer scope. Dual-mode lib (window.SettingsLib).
(function () {
  // sizes: visible-card index → "<cols>x<rows>"; cards past the last index stay 1x1.
  const PRESETS = {
    'uniform-2':    { label: 'Uniform',       cols: 2, sizes: {} },
    'tall-lead-2':  { label: 'Tall lead',     cols: 2, sizes: { 0: '1x2' } },
    'wide-2':       { label: 'Wide lead',     cols: 2, sizes: { 0: '2x1' } },
    'uniform-3':    { label: 'Uniform',       cols: 3, sizes: {} },
    'hero-3':       { label: 'Hero',          cols: 3, sizes: { 0: '2x2' } },
    'hero-right-3': { label: 'Hero right',    cols: 3, sizes: { 1: '2x2' } },
    'featured-3':   { label: 'Featured row',  cols: 3, sizes: { 0: '3x1' } },
    'wide-pair-3':  { label: 'Wide lead',     cols: 3, sizes: { 0: '2x1' } },
    'tall-pair-3':  { label: 'Two tall',      cols: 3, sizes: { 0: '1x2', 1: '1x2' } },
    'sidebar-3':    { label: 'Sidebar',       cols: 3, sizes: { 0: '1x2' } },
    'uniform-4':    { label: 'Uniform',       cols: 4, sizes: {} },
    'mixed-4':      { label: 'Hero + tiles',  cols: 4, sizes: { 0: '2x2' } },
    'twin-4':       { label: 'Twin heroes',   cols: 4, sizes: { 0: '2x2', 1: '2x2' } },
    'banner-4':     { label: 'Banner',        cols: 4, sizes: { 0: '4x1' } },
    'uniform-5':    { label: 'Uniform',       cols: 5, sizes: {} },
    'dual-5':       { label: 'Twin wide',     cols: 5, sizes: { 0: '2x1', 1: '2x1' } },
    'uniform-6':    { label: 'Uniform',       cols: 6, sizes: {} },
    'hero-6':       { label: 'Hero + tiles',  cols: 6, sizes: { 0: '2x2' } },
  };
  const COLUMN_OPTIONS = [2, 3, 4, 5, 6, 'auto'];
  const AUTO_MIN_COL_PX = 300;

  function presetsFor(cols) {
    if (cols === 'auto') return [['auto-uniform', { label: 'Uniform', cols: 'auto', sizes: {} }]];
    return Object.entries(PRESETS).filter(([, p]) => p.cols === Number(cols));
  }

  // Preset id whose shape matches the visible cards' sizes, else null (custom).
  function matchPreset(cols, sizesByIndex) {
    const norm = (s) => (!s || s === 'auto' || s === '1x1') ? '1x1' : s;
    const actual = Object.entries(sizesByIndex || {})
      .filter(([, s]) => norm(s) !== '1x1').map(([i, s]) => `${i}:${s}`).sort().join(',');
    for (const [id, p] of presetsFor(cols)) {
      const want = Object.entries(p.sizes).map(([i, s]) => `${i}:${s}`).sort().join(',');
      if (want === actual) return id;
    }
    return null;
  }

  // Preset by id within a column count; covers the synthetic auto-fit entry.
  function getPreset(cols, id) {
    const hit = presetsFor(cols).find(([pid]) => pid === id);
    return hit ? hit[1] : null;
  }
  function presetLabel(cols, id) {
    const p = getPreset(cols, id);
    return p ? p.label : null;
  }

  function gridTemplate(cols) {
    return cols === 'auto'
      ? `repeat(auto-fit, minmax(${AUTO_MIN_COL_PX}px, 1fr))`
      : `repeat(${Number(cols) || 3}, 1fr)`;
  }
  function normalizeCols(v) {
    if (v === 'auto') return 'auto';
    const n = Number(v);
    return (n >= 2 && n <= 6) ? n : 3;
  }

  // Flow engine (#823): cards span content-height rows; presets size cards by role.
  const ENGINES = ['grid', 'flow'];
  const DEFAULT_ENGINE = 'grid';
  const DENSITIES = ['comfortable', 'compact'];
  const DEFAULT_DENSITY = 'comfortable';
  const FLOW_UNIT_PX = 8;
  const FLOW_ROW_GAP_PX = 8;
  function normalizeEngine(v) { return ENGINES.includes(v) ? v : DEFAULT_ENGINE; }
  function normalizeDensity(v) { return DENSITIES.includes(v) ? v : DEFAULT_DENSITY; }
  // New installs start on Flow + auto columns + compact; a layout that has ever
  // saved card state keeps its engine/columns/density as they are.
  const FRESH_DEFAULTS = { layoutEngine: 'flow', density: 'compact', cols: 'auto' };
  const _CARD_STATE_KEYS = ['cardSizes', 'sizesByAgent', 'hiddenByAgent', 'orderByAgent'];
  function isFreshLayout(lay) {
    if (!lay || typeof lay !== 'object') return true;
    if (lay.layoutEngine !== undefined || lay.density !== undefined) return false;
    const filled = v => Array.isArray(v) ? v.length > 0 : (v && typeof v === 'object') ? Object.keys(v).length > 0 : v != null;
    for (const p of Object.values(CARD_PAGES)) {
      for (const k of [p.hidden, p.cols, p.order, p.borrowed]) if (k && filled(lay[k])) return false;
    }
    return !_CARD_STATE_KEYS.some(k => filled(lay[k]));
  }
  function applyFreshDefaults(lay) {
    if (!isFreshLayout(lay)) return false;
    lay.layoutEngine = FRESH_DEFAULTS.layoutEngine;
    lay.density = FRESH_DEFAULTS.density;
    for (const p of Object.values(CARD_PAGES)) lay[p.cols] = FRESH_DEFAULTS.cols;
    return true;
  }

  // Card id → role. Unlisted ids (and ov-borrow-* shells) resolve through roleOf().
  const CARD_ROLES = {
    'llama-server': 'chart', 'llama-throughput': 'chart', 'gpu': 'chart', 'cpu-overall': 'chart',
    'ram': 'chart', 'network': 'chart', 'disk-usage': 'chart', 'disk-io': 'chart', 'ups': 'stats',
    'aio': 'chart', 'psu': 'chart', 'smart-device': 'table',
    'lms-models': 'list', 'lms-active': 'chart', 'lms-cpu': 'chart', 'lms-ram': 'chart',
    'lms-network': 'chart', 'lms-disk': 'chart', 'lms-io': 'chart', 'lms-power': 'chart',
    'vllm-server': 'stats', 'vllm-requests': 'stats', 'vllm-kv': 'chart', 'vllm-throughput': 'chart',
    'vllm-cpu': 'chart', 'vllm-ram': 'chart', 'vllm-network': 'chart', 'vllm-disk': 'chart', 'vllm-io': 'chart',
    'services': 'table', 'influxdb': 'stats', 'mgr-agents': 'table', 'mgr-streams': 'table',
    'mgr-ram': 'stats', 'mgr-disk': 'stats', 'mgr-network': 'stats', 'mgr-processes': 'table',
    'mgr-perf-summary': 'stats', 'mgr-perf': 'chart', 'ae-perf': 'chart',
  };
  const ROLES = ['chart', 'stats', 'table', 'list'];
  const OV_PREFIX = 'ov-borrow-';
  function roleOf(cardId) {
    const id = String(cardId || '');
    const home = id.startsWith(OV_PREFIX) ? id.slice(OV_PREFIX.length) : id;
    return CARD_ROLES[home] || 'stats';
  }
  // The card a page's Hero preset widens; pages without one use their first visible card.
  const HERO_CARDS = {
    'dashboard/llamacpp': 'gpu', 'dashboard/lmstudio': 'lms-active',
    'dashboard/vllm': 'vllm-throughput', 'dashboard/manager': 'services',
  };
  // widths: role → column span; hero: span for the page's hero card. Unlisted = 1.
  const ROLE_PRESETS = {
    'uniform': { label: 'Uniform',     widths: {} },
    'charts':  { label: 'Charts wide', widths: { chart: 2 } },
    'hero':    { label: 'Hero',        widths: {}, hero: 2 },
    'tables':  { label: 'Tables wide', widths: { table: 2, list: 2 } },
  };
  const DEFAULT_ROLE_PRESET = 'uniform';
  function normalizeRolePreset(id) { return ROLE_PRESETS[id] ? id : DEFAULT_ROLE_PRESET; }
  // Column span a role preset gives one card, clamped to the grid's track count.
  function roleWidth(presetId, role, isHero, maxCols) {
    const p = ROLE_PRESETS[normalizeRolePreset(presetId)];
    const w = (isHero && p.hero) || p.widths[role] || 1;
    const max = Math.max(1, Number(maxCols) || 1);
    return Math.min(w, max);
  }
  // Row-track span for a card of height h in a flow grid (unit rows, row gap).
  function flowSpan(h, unit, gap) {
    const u = Number(unit) > 0 ? Number(unit) : FLOW_UNIT_PX;
    const g = Number(gap) >= 0 ? Number(gap) : FLOW_ROW_GAP_PX;
    const px = Number(h) || 0;
    return Math.max(1, Math.ceil((px + g) / (u + g)));
  }

  // swatch: [bg, card, accent, accent-2, border] for the picker tile.
  const THEMES = [
    { id: 'modern',     label: 'Modern',     dark: true,  swatch: ['#0b0d16', '#131626', '#9a7cff', '#2dd4bf', '#242a45'] },
    { id: 'dark',       label: 'Dark',       dark: true,  swatch: ['#111111', '#1a1a1a', '#77aaff', '#44ee99', '#2a2a2a'] },
    { id: 'medium',     label: 'Medium',     dark: true,  swatch: ['#1c1c20', '#25252b', '#88ccff', '#66eebb', '#34343c'] },
    { id: 'oled',       label: 'OLED black', dark: true,  swatch: ['#000000', '#0b0b0c', '#5eb0ff', '#35d6a8', '#1f1f22'] },
    { id: 'graphite',   label: 'Graphite',   dark: true,  swatch: ['#1a1816', '#221f1c', '#e3a24a', '#7cc4a0', '#332e29'] },
    { id: 'slate',      label: 'Slate',      dark: true,  swatch: ['#303446', '#414559', '#8caaee', '#81c8be', '#51576d'] },
    { id: 'enterprise', label: 'Enterprise', dark: true,  swatch: ['#0f1218', '#161a23', '#5b8def', '#4a9fc8', '#232a3a'] },
    { id: 'light',      label: 'Light',      dark: false, swatch: ['#f4f5f7', '#ffffff', '#0a66c2', '#1a8a4e', '#d4d6dc'] },
    { id: 'frost',      label: 'Frost',      dark: false, swatch: ['#eef2f7', '#ffffff', '#2a6fd6', '#0f9d8a', '#cdd7e4'] },
  ];
  const THEME_IDS = THEMES.map(t => t.id);
  const DEFAULT_THEME = 'modern';
  const LEGACY_THEMES = { classic: 'oled' };
  const SYSTEM_LIGHT_THEME = 'frost';

  function normalizeTheme(name) {
    if (THEME_IDS.includes(name)) return name;
    return LEGACY_THEMES[name] || DEFAULT_THEME;
  }
  // Theme to render: the saved one, or the light theme while following an OS in light mode.
  function effectiveTheme(saved, followSystem, osLight) {
    const base = normalizeTheme(saved);
    if (!followSystem) return base;
    const meta = THEMES.find(t => t.id === base);
    if (osLight) return meta && !meta.dark ? base : SYSTEM_LIGHT_THEME;
    return meta && !meta.dark ? DEFAULT_THEME : base;
  }

  // Which drawer sections a page gets. Card pages: Overall + the four card
  // dashboards; every other tab (and the Energy/OpenClaw sub-tabs) has none.
  const CARD_PAGES = {
    'overall':            { label: 'Overall',               hidden: 'hiddenOverall', cols: 'overallCols', order: 'overallOrder', grid: 'overallGrid', borrowed: 'overallBorrowed' },
    'dashboard/llamacpp': { label: 'Dashboards · llama.cpp', hidden: 'hidden',        cols: 'cols',        order: 'order',        grid: 'cardGrid' },
    'dashboard/lmstudio': { label: 'Dashboards · LM Studio', hidden: 'lmsHidden',     cols: 'lmsCols',     order: 'lmsOrder',     grid: 'lmsCardGrid' },
    'dashboard/vllm':     { label: 'Dashboards · vLLM',      hidden: 'vllmHidden',    cols: 'vllmCols',    order: 'vllmOrder',    grid: 'vllmCardGrid' },
    'dashboard/manager':  { label: 'Dashboards · Manager',   hidden: 'managerHidden', cols: 'managerCols', order: 'managerOrder', grid: 'managerCardGrid' },
  };
  const PAGE_LABELS = {
    dashboard: 'Dashboards', llm: 'LLM Control', events: 'Events', tools: 'Tools', admin: 'Admin',
  };
  const SUB_LABELS = { energy: 'Energy', openclaw: 'OpenClaw' };

  function settingsScope(activeTab, dashSub) {
    const key = activeTab === 'dashboard' ? `dashboard/${dashSub || 'llamacpp'}` : activeTab;
    const page = CARD_PAGES[key];
    if (page) return { kind: 'cards', key, ...page };
    let label = PAGE_LABELS[activeTab] || activeTab;
    if (activeTab === 'dashboard' && SUB_LABELS[dashSub]) label = `Dashboards · ${SUB_LABELS[dashSub]}`;
    return { kind: 'none', key, label };
  }

  // Agents sample every 5 s, so nothing faster than that shows new data.
  const INTERVAL_MIN = 5, INTERVAL_MAX = 300;
  const INTERVAL_CHIPS = [30, 60, 90, 120, 300];
  function clampInterval(v) {
    const n = Math.round(Number(v));
    if (!Number.isFinite(n)) return INTERVAL_CHIPS[0];
    return Math.max(INTERVAL_MIN, Math.min(INTERVAL_MAX, n));
  }

  const API = {
    PRESETS, COLUMN_OPTIONS, AUTO_MIN_COL_PX, presetsFor, getPreset, presetLabel, matchPreset, gridTemplate, normalizeCols,
    ENGINES, DEFAULT_ENGINE, DENSITIES, DEFAULT_DENSITY, FLOW_UNIT_PX, FLOW_ROW_GAP_PX, normalizeEngine, normalizeDensity,
    FRESH_DEFAULTS, isFreshLayout, applyFreshDefaults,
    CARD_ROLES, ROLES, roleOf, HERO_CARDS, ROLE_PRESETS, DEFAULT_ROLE_PRESET, normalizeRolePreset, roleWidth, flowSpan,
    THEMES, THEME_IDS, DEFAULT_THEME, LEGACY_THEMES, SYSTEM_LIGHT_THEME, normalizeTheme, effectiveTheme,
    CARD_PAGES, settingsScope, INTERVAL_MIN, INTERVAL_MAX, INTERVAL_CHIPS, clampInterval,
  };
  if (typeof window !== 'undefined') window.SettingsLib = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})();
