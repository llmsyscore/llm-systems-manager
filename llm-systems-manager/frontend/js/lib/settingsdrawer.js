// Settings drawer data: layout presets per column count, theme catalog (with
// legacy-name migration) and the per-page drawer scope. Dual-mode lib (window.SettingsLib).
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
    'overall':            { label: 'LLM Overall',           hidden: 'hiddenOverall', cols: 'overallCols', order: 'overallOrder', grid: 'overallGrid', borrowed: 'overallBorrowed' },
    'dashboard/llamacpp': { label: 'Dashboard · llama.cpp', hidden: 'hidden',        cols: 'cols',        order: 'order',        grid: 'cardGrid' },
    'dashboard/lmstudio': { label: 'Dashboard · LM Studio', hidden: 'lmsHidden',     cols: 'lmsCols',     order: 'lmsOrder',     grid: 'lmsCardGrid' },
    'dashboard/vllm':     { label: 'Dashboard · vLLM',      hidden: 'vllmHidden',    cols: 'vllmCols',    order: 'vllmOrder',    grid: 'vllmCardGrid' },
    'dashboard/manager':  { label: 'Dashboard · Manager',   hidden: 'managerHidden', cols: 'managerCols', order: 'managerOrder', grid: 'managerCardGrid' },
  };
  const PAGE_LABELS = {
    dashboard: 'Dashboard', llm: 'LLM Control', events: 'Events', openclaw: 'OpenClaw',
    llmchat: 'LLM Chat', imggen: 'Image Generation', admin: 'Admin',
  };
  const SUB_LABELS = { energy: 'Energy', openclaw: 'OpenClaw' };

  function settingsScope(activeTab, dashSub) {
    const key = activeTab === 'dashboard' ? `dashboard/${dashSub || 'llamacpp'}` : activeTab;
    const page = CARD_PAGES[key];
    if (page) return { kind: 'cards', key, ...page };
    let label = PAGE_LABELS[activeTab] || activeTab;
    if (activeTab === 'dashboard' && SUB_LABELS[dashSub]) label = `Dashboard · ${SUB_LABELS[dashSub]}`;
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
    THEMES, THEME_IDS, DEFAULT_THEME, LEGACY_THEMES, SYSTEM_LIGHT_THEME, normalizeTheme, effectiveTheme,
    CARD_PAGES, settingsScope, INTERVAL_MIN, INTERVAL_MAX, INTERVAL_CHIPS, clampInterval,
  };
  if (typeof window !== 'undefined') window.SettingsLib = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})();
