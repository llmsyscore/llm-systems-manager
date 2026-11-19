// #1432 phase 4c-2: the old llama.cpp, LM Studio and vLLM card grids are gone; saved layouts move to the host page panels.
// Runs foundation's real layout load and pin adoption with kit.js, host-view and hostpage.js on a small DOM, fetch stubbed.
import { describe, test, expect, afterEach, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import { createRequire } from 'node:module';
import OV from '../js/lib/overall-view.js';
import HV from '../js/lib/hosts-view.js';
import HP from '../js/lib/host-view.js';
import LMPeaks from '../js/lib/peaks.js';
import { srcFile, fnSrc, blockSrc, evalGlobal, flush } from './helpers/harness.js';

const SettingsLib = createRequire(import.meta.url)('../js/lib/settingsdrawer.js');
const indexHtml = srcFile('index.html');
const indexDoc = new JSDOM(indexHtml).window.document;
const foundation = srcFile('js/foundation.js');
const charts = srcFile('js/charts.js');
evalGlobal(srcFile('kit/kit.js'));
window.OV = OV;
window.HV = HV;
window.HP = HP;
window.LMPeaks = LMPeaks;
window.SettingsLib = SettingsLib;

const ID_A = 'aaaaaaaa-0000-4000-8000-000000000001';
const agent = { agent_id: ID_A, hostname: 'llm-a', online: true };
const SYS = { cpu_total: 30, ram: { percent: 40, used_bytes: 4e9, total_bytes: 1e10 },
  disk: [{ mountpoint: '/', percent: 50, used_bytes: 5e10, total_bytes: 1e11 }], net: { bytes_sent_per_sec: 100, bytes_recv_per_sec: 200 },
  gpu: { gpu_util_percent: 20, vram_usage_percent: 30, temperature_c: 55, power_watts: 120 } };

// The host page roots and Overall's grid, copied from index.html.
function mountPages() {
  const ids = ['dash-llamacpp', 'dash-lmstudio', 'dash-vllm', 'dash-host'];
  document.body.innerHTML = '<div id="dashboardTab">' + ids.map(id => {
    const page = indexDoc.getElementById(id), shell = page.cloneNode(false);
    shell.appendChild(page.querySelector(':scope > .hp-root').cloneNode(true));
    return shell.outerHTML;
  }).join('') + '</div><div id="overallTab"><div class="grid" id="overallGrid"></div></div>';
}

// fetch stub: the saved layout on GET /api/layout, POST bodies recorded, host page routes answered.
function stubFetch(saved) {
  const posts = [];
  const ok = (j) => Promise.resolve({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(j)) });
  window.fetch = vi.fn((url, opts) => {
    if (url === '/api/layout') {
      if (opts && opts.method === 'POST') { posts.push(JSON.parse(opts.body)); return ok({ ok: true }); }
      return ok(saved);
    }
    if (url.startsWith('/api/history?')) return ok([]);
    if (url.startsWith('/api/metrics?')) return ok(Object.assign({ ts: new Date().toISOString(), llama: {} }, SYS));
    if (url.startsWith('/api/llama-state?')) return ok({ state: 'idle', agent_online: true, aggregate: 'idle' });
    if (url.startsWith('/api/llm/models')) return ok({ data: [] });
    if (url.startsWith('/api/lmstudio/metrics?')) return ok({ agent_online: true, ts: new Date().toISOString(), system: SYS, ps: [], models: [] });
    return ok({});
  });
  return posts;
}

// foundation.js's real layout load, layout save and pin adoption at global scope; `layout` is window.layout.
function loadFoundation() {
  const labels = blockSrc(foundation, '// Host page panel ids and titles', '\n// ---------------------------------------------------------------------------\n// Layout persistence');
  const renames = blockSrc(foundation, 'const _LEGACY_CARD_RENAMES = {', '\n};');
  const saver = blockSrc(foundation, 'let _layoutInFlight = null;', 'let _layoutPending  = false;');
  evalGlobal([
    labels.replace(/^const /gm, 'var '), renames.replace(/^const /, 'var '), saver.replace(/^let /gm, 'var '),
    'var layout = {};', 'var _ovHomeMarks = {};', 'window._ovAdopted = new Set();',
    fnSrc(foundation, 'loadLayout'), fnSrc(foundation, '_migrateLegacyCardIds'), fnSrc(foundation, 'applyLayout'),
    fnSrc(foundation, 'saveLayout'), fnSrc(foundation, '_homeCardEl'), fnSrc(foundation, 'adoptPinnedCards'),
    fnSrc(foundation, '_returnOneAdopted'), fnSrc(foundation, 'returnPinnedCards'), fnSrc(foundation, '_ovBackfillPinnedProviders'),
    'window.loadLayout = loadLayout; window.adoptPinnedCards = adoptPinnedCards; window._ovBackfillPinnedProviders = _ovBackfillPinnedProviders;',
  ].join('\n'));
  window.applyTheme = () => {};
  window.applyAllGridCols = () => {};
  window._applyRowsHidden = () => {};
  window._ensureSizeBtn = () => {};
  window._resizeChartsIn = () => {};
}

function makeStore() {
  const m = {};
  return { getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null), setItem: (k, v) => { m[k] = String(v); } };
}

async function settle() { for (let i = 0; i < 8; i++) await flush(); }

afterEach(() => {
  delete window._activeTab;
  if (typeof window.hpLeave === 'function') window.hpLeave();
  vi.restoreAllMocks();
});

describe('Old provider grids are gone (#1432)', () => {
  test('index.html has no old card grids, picker rows or their charts', () => {
    for (const id of ['cardGrid', 'lmsCardGrid', 'vllmCardGrid', 'agentPickerDashLlama', 'agentPickerDashLms', 'agentPickerDashVllm']) {
      expect(indexDoc.getElementById(id), id).toBeNull();
    }
    expect(indexDoc.querySelector('canvas#cpuChart')).toBeNull();
    expect(indexDoc.getElementById('lmsTpsChart')).toBeNull();
    expect(indexDoc.getElementById('vllmKvChart')).toBeNull();
    for (const id of ['agentPickerCtrlLlama', 'agentPickerCtrlLms', 'agentPickerCtrlVllm']) expect(indexDoc.getElementById(id), id).not.toBeNull();
    for (const page of ['llama', 'lms', 'vllm', 'host']) expect(indexDoc.getElementById('hp-' + page), page).not.toBeNull();
  });

  test('the drawer lists rows on every host page', () => {
    expect(SettingsLib.settingsScope('dashboard', 'lmstudio')).toMatchObject({ kind: 'rows', rowsHidden: 'lmsRowsHidden', root: 'hp-lms' });
    for (const [sub, page] of [['llamacpp', 'llama'], ['lmstudio', 'lms'], ['vllm', 'vllm'], ['host', 'host']])
      expect(SettingsLib.settingsScope('dashboard', sub).panelsHidden, sub).toBe(HP.HIDDEN_KEYS[page]);
    expect(SettingsLib.settingsScope('dashboard', 'llamacpp')).toMatchObject({ kind: 'rows', rowsHidden: 'llamaRowsHidden', root: 'hp-llama' });
    expect(SettingsLib.settingsScope('dashboard', 'vllm')).toMatchObject({ kind: 'rows', rowsHidden: 'vllmRowsHidden', root: 'hp-vllm' });
    expect(SettingsLib.settingsScope('dashboard', 'host')).toMatchObject({ kind: 'rows', rowsHidden: 'hostRowsHidden', root: 'hp-host' });
    expect(SettingsLib.settingsScope('dashboard', 'manager')).toMatchObject({ kind: 'rows', rowsHidden: 'managerRowsHidden', root: 'dash-manager' });
  });
});

describe('Saved layouts map to the host page panels (#1432)', () => {
  test('old pins become panel ids with one save, and the GPU panel lands on Overall', async () => {
    mountPages();
    const posts = stubFetch({ overallBorrowed: ['gpu', 'lms-power'], overallOrder: ['ov-borrow-gpu'], cardSizes: { 'ov-borrow-gpu': '2x1' },
      hidden: ['ups'], order: ['gpu'], hiddenByAgent: { llama: { [ID_A]: ['ups', 'kraken'] } }, lmsHidden: ['lms-network'] });
    window.ahState = { range: 24 };
    window.mcShellStore = makeStore();
    window.LivePause = { on: false, every: (fn, ms) => setInterval(fn, ms) };
    window._agentsByProvider = { llama: [agent], lms: [agent], vllm: [] };
    window._selectedAgents = { llama: null, lms: null, vllm: null };
    window._selectedAgent = (p) => window._selectedAgents[p] || null;
    window._activeTab = 'overall';
    evalGlobal(srcFile('js/hostpage.js'));
    loadFoundation();

    await window.loadLayout();
    await settle();
    expect(window.layout.overallBorrowed).toEqual(['llama-gpu-busy', 'lms-mac-power']);
    expect(window.layout.overallOrder).toEqual(['ov-borrow-llama-gpu-busy']);
    expect(window.layout.cardSizes).toEqual({ 'ov-borrow-llama-gpu-busy': '2x1' });
    expect(window.layout.hostPageMapped).toBe(1);
    for (const k of ['hidden', 'order', 'hiddenByAgent', 'lmsHidden']) expect(window.layout, k).not.toHaveProperty(k);
    expect(window.layout.llamaPanelsHidden).toEqual(['llama-ups', 'llama-aio']);
    expect(window.layout.lmsPanelsHidden).toEqual(['lms-net']);
    expect(posts.length).toBe(1);
    expect(posts[0].llamaPanelsHidden).toEqual(['llama-ups', 'llama-aio']);
    expect(posts[0].overallBorrowed).toEqual(['llama-gpu-busy', 'lms-mac-power']);
    expect(window.CARD_LABELS['llama-gpu-busy']).toBe('GPU busy and memory');
    expect(window.CARD_LABELS_LMS['lms-mac-power']).toBe('Mac power');

    window._ovBackfillPinnedProviders();
    await settle();
    const panel = document.querySelector('#overallGrid [data-card="ov-borrow-llama-gpu-busy"] section.panel[data-card="llama-gpu-busy"]');
    expect(panel).not.toBeNull();
    expect(document.querySelector('#hp-llama [data-card="llama-gpu-busy"]')).toBeNull();
    expect(document.querySelector('#overallGrid [data-card="ov-borrow-lms-mac-power"] section.panel[data-card="lms-mac-power"]')).not.toBeNull();

    // A mapped layout loads again without another save.
    stubFetch(window.layout);
    const again = window.fetch;
    await window.loadLayout();
    await settle();
    expect(again.mock.calls.filter(c => c[1] && c[1].method === 'POST').length).toBe(0);

    // A returned host page panel goes home without the old card size classes.
    window._applyCardSize = vi.fn();
    window._returnOneAdopted('llama-gpu-busy');
    const home = document.querySelector('#hp-llama [data-card="llama-gpu-busy"]');
    expect(home).not.toBeNull();
    expect(window._applyCardSize).not.toHaveBeenCalled();
    expect(home.dataset.size).toBeUndefined();
    ['_applyCardSize', '_activeTab', 'applyTheme', 'applyAllGridCols', '_applyRowsHidden',
      '_ensureSizeBtn', '_resizeChartsIn'].forEach(k => { delete window[k]; });
  });
});

describe('Layout load errors (#1432)', () => {
  test('a failed layout load or mapping is logged, not swallowed', async () => {
    mountPages();
    window.fetch = vi.fn(() => Promise.resolve({ ok: true, json: async () => ({ overallBorrowed: ['gpu'] }) }));
    loadFoundation();
    window._activeTab = 'dashboard';
    const boom = new Error('map failed');
    const mapped = vi.spyOn(HP, 'mapLegacyLayout').mockImplementation(() => { throw boom; });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    await window.loadLayout();
    expect(mapped).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalledWith('layout load failed', boom);
    ['applyTheme', 'applyAllGridCols', '_applyRowsHidden', '_ensureSizeBtn', '_resizeChartsIn'].forEach(k => { delete window[k]; });
  });
});

describe('fetchMetrics keeps only what the rest of the page reads (#1432)', () => {
  test('a sample sets _latestMetric, the llama peaks and the slot count, and logs no error', async () => {
    const peaks = blockSrc(charts, 'const lastNonZero = {', '\n// ---------------------------------------------------------------------------\n// Server state polling');
    evalGlobal([
      peaks.replace(/^const /gm, 'var '), 'var _pillModelName = "";', 'var _llamaActiveSlots = 0;',
      fnSrc(charts, '_cleanLlamaModelName'), fnSrc(charts, 'fetchMetrics'), 'window.fetchMetrics = fetchMetrics;',
    ].join('\n'));
    const sample = { ts: new Date().toISOString(), cpu_total: 12, llama: { model: 'org/m.gguf', tokens_per_second: 42, active_slots: 2 } };
    window._agentClaimKey = (k) => k;
    window._selectedAgent = () => 'agent-1';
    window._claim = () => true;
    window._release = () => {};
    window._fetchT = vi.fn(() => Promise.resolve({ json: async () => sample }));
    window._activeTab = 'dashboard';
    window._subTabState = { dashboard: 'llamacpp' };
    window.fetchOverallMetrics = vi.fn();
    window.renderModelCards = vi.fn();
    window._updateModelPerf = vi.fn();
    document.body.innerHTML = '<div id="overallTab" style="display:none"></div>';
    const err = vi.spyOn(console, 'error');
    await window.fetchMetrics();
    expect(err).not.toHaveBeenCalled();
    expect(window._latestMetric).toEqual(sample);
    expect(window._latestMetricAgent).toBe('agent-1');
    expect(Date.now() - window._latestMetricAt).toBeLessThan(5000);
    expect(window._llamaPeaks.tps.peak(Date.now()).v).toBe(42);
    expect(window.lastNonZero.active_slots.val).toBe(2);
    expect(window._llamaActiveSlots).toBe(2);
    expect(window._pillModelName).toBe('m.gguf');
    expect(window.renderModelCards).toHaveBeenCalledTimes(1);
    expect(window.fetchOverallMetrics).not.toHaveBeenCalled();
    ['_agentClaimKey', '_selectedAgent', '_latestMetricAgent', '_latestMetricAt', '_claim', '_release', '_fetchT', '_activeTab', '_subTabState', 'fetchOverallMetrics', 'renderModelCards',
      '_updateModelPerf'].forEach(k => { delete window[k]; });
  });
});

describe('Pinned host page panels on Overall (#1432)', () => {
  test('"Charts wide" widens a pinned chart panel and leaves the tiles panel at one column', () => {
    document.body.innerHTML = '<div class="grid flow" id="overallGrid">'
      + '<div class="card ov-shell" data-card="ov-borrow-llama-tiles"></div>'
      + '<div class="card ov-shell" data-card="ov-borrow-llama-cpu-mem"></div>'
      + '<div class="card ov-shell" data-card="ov-borrow-llama-details"></div></div>';
    evalGlobal(['_isFlowGrid', '_gridPageKey', '_rolePresetFor', '_flowHeroId', '_flowPresetWidth'].map(n => fnSrc(charts, n)).join('\n')
      + '\nwindow._flowPresetWidth = _flowPresetWidth;');
    window.layout = { rolePreset: { overall: 'charts' } };
    const shell = (id) => document.querySelector(`[data-card="ov-borrow-${id}"]`);
    expect(window._flowPresetWidth(shell('llama-cpu-mem'), 3)).toBe(2);
    expect(window._flowPresetWidth(shell('llama-tiles'), 3)).toBe(1);
    expect(window._flowPresetWidth(shell('llama-details'), 3)).toBe(1);
    window.layout.rolePreset.overall = 'tables';
    expect(window._flowPresetWidth(shell('llama-details'), 3)).toBe(2);
    expect(window._flowPresetWidth(shell('llama-cpu-mem'), 3)).toBe(1);
    window.layout.rolePreset.overall = 'uniform';
    expect(window._flowPresetWidth(shell('llama-cpu-mem'), 3)).toBe(1);
  });

  test('initCardResize sizes old-style cards and shells, never kit panels', () => {
    document.body.innerHTML = '<div class="grid" id="oldGrid"><div class="card" data-card="old-card"></div></div>'
      + '<div id="dash-all"><section class="panel" data-card="ah-cpu"></section></div>'
      + '<div class="hp-root" id="hp-llama"><section class="panel" data-card="llama-cpu-mem"></section></div>'
      + '<div id="overallTab"><section class="panel" data-card="ov-fleet"></section>'
      + '<div class="grid" id="overallGrid"><div class="card ov-shell" data-card="ov-borrow-llama-gpu-busy">'
      + '<section class="panel" data-card="llama-gpu-busy"></section></div></div></div>';
    evalGlobal([fnSrc(charts, '_ensureSizeBtn'), fnSrc(charts, 'initCardResize'), 'window.initCardResize = initCardResize;'].join('\n'));
    const sized = [];
    window._applyCardSize = (card) => sized.push(card.dataset.card);
    window._sizeMapFor = () => ({});
    window._defaultCardSize = () => 'auto';
    window._cycleCardSize = () => {};
    window.initCardResize();
    expect(sized).toEqual(['old-card', 'ov-borrow-llama-gpu-busy']);
    expect([...document.querySelectorAll('section.panel .card-size-btn')]).toEqual([]);
    expect(document.querySelectorAll('.card-size-btn').length).toBe(2);
    ['_applyCardSize', '_sizeMapFor', '_defaultCardSize', '_cycleCardSize'].forEach(k => { delete window[k]; });
  });
});
