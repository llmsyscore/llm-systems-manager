// #1432 phase 4c-2: host page names, threshold lines, hidden panels, shared llama sample, poll pace and Overall range.
// Runs kit.js, overall-view, hosts-view, peaks, host-view and hostpage.js on the real #dashboardTab markup with fetch stubbed.
import { describe, test, expect, afterEach, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import OV from '../js/lib/overall-view.js';
import HV from '../js/lib/hosts-view.js';
import HP from '../js/lib/host-view.js';
import LMPeaks from '../js/lib/peaks.js';
import { createRequire } from 'node:module';
import { srcFile, fnSrc, blockSrc, evalGlobal, flush } from './helpers/harness.js';

const indexDoc = new JSDOM(srcFile('index.html')).window.document;
// #dashboardTab with All hosts and each host page holding only its index.html host root, to keep the DOM small.
const tabHtml = (() => {
  const tab = indexDoc.getElementById('dashboardTab').cloneNode(false);
  tab.appendChild(indexDoc.getElementById('dash-all').cloneNode(true));
  ['dash-llamacpp', 'dash-lmstudio', 'dash-vllm', 'dash-host'].forEach(id => {
    const page = indexDoc.getElementById(id), shell = page.cloneNode(false);
    shell.appendChild(page.querySelector(':scope > .hp-root').cloneNode(true));
    tab.appendChild(shell);
  });
  tab.appendChild(indexDoc.getElementById('dash-energy').cloneNode(false));
  return tab.outerHTML;
})();
const hostpage = srcFile('js/hostpage.js');
const foundation = srcFile('js/foundation.js');
evalGlobal(srcFile('kit/kit.js'));
window.OV = OV;
window.HV = HV;
window.HP = HP;
window.LMPeaks = LMPeaks;

const ID_A = 'aaaaaaaa-0000-4000-8000-000000000001';
const ID_B = 'bbbbbbbb-0000-4000-8000-000000000002';
const ID_C = 'cccccccc-0000-4000-8000-000000000003';
const HOST = { [ID_A]: 'llm-a', [ID_B]: 'llm-b.local', [ID_C]: 'llm-bare' };
const LEVEL = { [ID_A]: 30, [ID_B]: 70, [ID_C]: 10 };
const agent = (id, extra) => Object.assign({ agent_id: id, hostname: HOST[id], online: true }, extra);

// One row a minute for the last hour, newest last; level sets the CPU of the host.
function rowsFor(level, now) {
  const out = [];
  for (let i = 59; i >= 0; i--) {
    out.push({ ts: new Date(now - i * 60000).toISOString(), cpu_total: level, ram_percent: 40, gpu_util: 20, gpu_vram: 30,
      net_sent: 1000, net_recv: 2000, io_read: 10, io_write: 20, disk_root_pct: 50, llama_tps: 5, llama_pps: 50 });
  }
  return out;
}
const SYS = (level) => ({ cpu_total: level, ram: { percent: 40, used_bytes: 4e9, total_bytes: 1e10 },
  disk: [{ mountpoint: '/', percent: 50, used_bytes: 5e10, total_bytes: 1e11 }], net: { bytes_sent_per_sec: 100, bytes_recv_per_sec: 200 } });
const GPU = { gpu_util_percent: 20, vram_usage_percent: 30, temperature_c: 55, power_watts: 120 };

// A llama host's flat sample, as /api/metrics and its llama bucket give it.
const llamaSample = (id) => Object.assign({ ts: new Date().toISOString(), gpu: GPU, llama: { tokens_per_second: 0, active_slots: 0 } }, SYS(LEVEL[id]));
// Live answers by route; tests replace entries to change what a host reports.
function defaults() {
  return {
    metrics: llamaSample,
    state: () => ({ state: 'idle', agent_online: true, aggregate: 'idle' }),
    models: () => ({ data: [] }),
    lms: (id) => ({ agent_online: true, ts: new Date().toISOString(), system: SYS(LEVEL[id]), ps: [], models: [] }),
    vllm: (id) => ({ agent_online: true, ts: new Date().toISOString(), system: SYS(LEVEL[id]), vllm: { state: 'running', model: 'm' } }),
    hosts: () => ({ hosts: [agent(ID_A, { providers: ['llama'] }), agent(ID_C, { providers: [] })] }),
    // The system route gives a host with no LLM server its system sample and an LLM host its llama sample.
    system: (id) => ({ agent_id: id, hostname: HOST[id], online: true, age_s: 1, sample: id === ID_C ? SYS(LEVEL[id]) : llamaSample(id) }),
    history: (id) => rowsFor(LEVEL[id], Date.now()),
  };
}

const FAIL = Symbol('fail');
// fetch stub: records every URL; a route value that is a function of the agent id answers it.
function stubFetch(routes) {
  const calls = [];
  // A route answer of FAIL comes back as HTTP 500.
  const ok = (v) => Promise.resolve(v).then(j => (j === FAIL ? { ok: false, status: 500, json: async () => ({}) }
    : { ok: true, json: async () => JSON.parse(JSON.stringify(j)) }));
  window.fetch = vi.fn((url) => {
    calls.push(url);
    const id = (/[?&]agent=([^&]+)/.exec(url) || [])[1];
    const aid = id ? decodeURIComponent(id) : null;
    if (url.startsWith('/api/history?')) return ok(routes.history(aid, url));
    if (url.startsWith('/api/metrics?')) return ok(routes.metrics(aid));
    if (url.startsWith('/api/llama-state?')) return ok(routes.state(aid));
    if (url.startsWith('/api/llm/models')) return ok(routes.models(aid));
    if (url.startsWith('/api/lmstudio/metrics?')) return ok(routes.lms(aid));
    if (url.startsWith('/api/vllm/metrics?')) return ok(routes.vllm(aid));
    if (url === '/api/agents/hosts') return ok(routes.hosts());
    if (url.startsWith('/api/energy/hourly?') && routes.energy) return ok(routes.energy(aid, url));
    const m = /^\/api\/agents\/([^/]+)\/system$/.exec(url);
    if (m) return ok(routes.system(decodeURIComponent(m[1])));
    return ok({});
  });
  return calls;
}

function makeStore() {
  const m = {};
  return { getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null), setItem: (k, v) => { m[k] = String(v); } };
}

// A fresh #dashboardTab and a fresh hostpage.js; providers lists each provider's hosts.
function load(providers = { llama: [agent(ID_A)], lms: [agent(ID_A)], vllm: [agent(ID_A)] }, saved = {}) {
  if (typeof window.hpLeave === 'function') window.hpLeave();
  document.body.innerHTML = tabHtml;
  delete window._activeTab;
  delete window._subTabState;
  delete window._alarmRules;
  ['_latestMetric', '_latestMetricAt', '_latestMetricAgent', '_pollCfg', 'ovState'].forEach(k => { delete window[k]; });
  window.ahState = { range: 24 };
  window.mcShellStore = makeStore();
  window.LivePause = { on: false, every: (fn, ms) => setInterval(fn, ms) };
  window._agentsByProvider = providers;
  window._selectedAgents = { llama: null, lms: null, vllm: null };
  window._selectedAgent = (p) => window._selectedAgents[p] || null;
  window._selectAgent = vi.fn((p, id) => { window._selectedAgents[p] = id; });
  window.layout = { _selectedAgents: Object.assign({}, saved) };
  window.saveLayout = vi.fn(() => Promise.resolve());
  Object.keys(window.KIT_SERIES_OFF).forEach(k => { delete window.KIT_SERIES_OFF[k]; });
  evalGlobal(hostpage);
}

async function settle() { for (let i = 0; i < 6; i++) await flush(); }

const historyCalls = (calls) => calls.filter(u => u.startsWith('/api/history'));
const cpuValues = () => {
  const ts = document.querySelector('#hp-b-llama-cpu-mem .ts[data-kit-ts]');
  return ts ? window.kitTsData(ts).series[0].values.filter(v => v != null) : [];
};

afterEach(() => {
  if (typeof window.hpLeave === 'function') window.hpLeave();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('Host pages fix wave (#1432)', () => {
  test('an LLM host on the host page reads online with its CPU', async () => {
    load({ llama: [agent(ID_A)], lms: [], vllm: [] }, { system: ID_A });
    stubFetch(defaults());
    await window.hpEnter('host');
    await settle();
    const tiles = document.getElementById('hp-b-host-tiles').textContent;
    expect(tiles).toContain('Online');
    expect(tiles).not.toContain('Host offline');
    expect(tiles).toContain('30%');
    expect(document.getElementById('hp-b-host-gpu-details').textContent).toContain('55.0');
  });

  test('memory and VRAM rules draw their lines on CPU and memory and on GPU busy and memory, without the busy line its strip shows', async () => {
    load();
    stubFetch(defaults());
    const rule = (name, metric, v) => ({ enabled: true, name, metric_source: 'system', metric_name: metric,
      rule_type: 'threshold_above', severity: 'warning', source_host: 'llm-a', config: { threshold: { upper: v } } });
    window._alarmRules = [rule('CPU hot', 'cpu_total', 90), rule('Memory full', 'ram_percent', 85),
      rule('GPU busy', 'gpu_gpu_util_percent', 95), rule('VRAM full', 'gpu_vram_usage_percent', 92)];
    await window.hpEnter('llama');
    await settle();
    const lines = (id) => [...document.querySelectorAll(`#hp-b-llama-${id} line.th`)].map(e => e.getAttribute('y1'));
    expect(lines('cpu-mem')).toHaveLength(2);
    expect(new Set(lines('cpu-mem')).size).toBe(2);
    expect(lines('gpu-busy')).toHaveLength(1);
    // Lines only: severity shows by colour, with no text over them.
    expect([...document.querySelectorAll('#hp-llama .th-l')].every(e => e.textContent === '')).toBe(true);
  });

  test('a panel in the page\'s hidden list hides on its page and still shows when pinned to Overall', async () => {
    load();
    stubFetch(defaults());
    window.layout.llamaPanelsHidden = ['llama-net', 'llama-cpu-mem'];
    await window.hpEnter('llama');
    await settle();
    const panel = (id) => document.querySelector(`[data-card="${id}"]`);
    expect(panel('llama-net').hidden).toBe(true);
    expect(panel('llama-cpu-mem').hidden).toBe(true);
    expect(panel('llama-io').hidden).toBe(false);
    window.layout.llamaPanelsHidden = ['llama-cpu-mem'];
    window.hpRender();
    expect(panel('llama-net').hidden).toBe(false);
    // Pinned on Overall: the panel leaves its page and shows there.
    document.body.insertAdjacentHTML('beforeend', '<div id="ovBox"></div>');
    document.getElementById('ovBox').appendChild(panel('llama-cpu-mem'));
    window.layout.overallBorrowed = ['llama-cpu-mem'];
    window._activeTab = 'overall';
    window.hpRender();
    expect(panel('llama-cpu-mem').closest('#ovBox')).not.toBeNull();
    expect(panel('llama-cpu-mem').hidden).toBe(false);
  });

  test('the chat template cell shows its size with a 600-character title; the table view has the full text', async () => {
    load();
    const tmpl = 'x'.repeat(700) + 'END';
    const r = defaults();
    r.metrics = (id) => Object.assign(llamaSample(id), { llama: { chat_template_len: 3072, chat_template: tmpl } });
    stubFetch(r);
    await window.hpEnter('llama');
    await settle();
    const body = document.getElementById('hp-b-llama-details');
    const cell = [...body.querySelectorAll('tr')].find(tr => tr.cells[0].textContent === 'Chat template').cells[1];
    expect(cell.textContent).toBe('3.0 KB');
    expect(cell.title).toBe('x'.repeat(600));
    expect(body.textContent).not.toContain('END');
    const table = body.closest('.panel').querySelector('.p-table');
    const tcell = [...table.querySelectorAll('tr')].find(tr => tr.cells[0].textContent === 'Chat template').cells[1];
    expect(tcell.textContent).toBe('3.0 KB · ' + tmpl);
  });

  test('the fan controller name is the fan panel note', async () => {
    load();
    const r = defaults();
    r.metrics = (id) => Object.assign(llamaSample(id), { liquidctl: { smart: { _name: 'Smart Device V2',
      fans: [{ id: 1, control_mode: 'PWM', duty: 40, speed: { value: 800, unit: 'rpm' } }] } } });
    stubFetch(r);
    await window.hpEnter('llama');
    await settle();
    expect(document.querySelector('[data-card="llama-fans"] .p-hd .p-note').textContent).toBe('Smart Device V2');
  });

  test('on llama.cpp the page reuses the dashboard\'s own /api/metrics poll: one request per poll', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const calls = stubFetch(defaults());
    // The dashboard's fetchMetrics poll for the selected llama host, every 5 s.
    const poll = async () => {
      calls.push('/api/metrics');
      window._latestMetric = llamaSample(ID_A);
      window._latestMetricAt = Date.now();
    };
    await poll();
    const timer = setInterval(poll, 5000);
    window.hpEnter('llama');
    await vi.advanceTimersByTimeAsync(60000);
    clearInterval(timer);
    const metrics = calls.filter(u => u.startsWith('/api/metrics'));
    expect(metrics.length).toBe(13);
    expect(metrics.every(u => u === '/api/metrics')).toBe(true);
    expect(cpuValues().length).toBeGreaterThan(0);
    // With the dashboard's sample stale, the page asks for its own.
    window._latestMetricAt = Date.now() - 60000;
    await vi.advanceTimersByTimeAsync(5000);
    expect(calls.filter(u => u === `/api/metrics?agent=${ID_A}`).length).toBe(1);
  });

  test('a dashboard sample of another llama host is not reused', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load({ llama: [agent(ID_A), agent(ID_B)], lms: [], vllm: [] });
    window._selectedAgents.llama = ID_B;
    window._latestMetric = llamaSample(ID_A);
    window._latestMetricAt = Date.now();
    window._latestMetricAgent = ID_A;
    const calls = stubFetch(defaults());
    window.hpEnter('llama');
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toContain(`/api/metrics?agent=${ID_B}`);
    window._latestMetric = llamaSample(ID_B);
    window._latestMetricAt = Date.now();
    window._latestMetricAgent = ID_B;
    await vi.advanceTimersByTimeAsync(5000);
    expect(calls.filter(u => u.startsWith('/api/metrics')).length).toBe(1);
  });

  test('a new poll interval restarts the page poll at the new pace', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const calls = stubFetch(defaults());
    window._pollCfg = { poll_interval: 5 };
    window.hpEnter('lms');
    await vi.advanceTimersByTimeAsync(0);
    const live = () => calls.filter(u => u.startsWith('/api/lmstudio/metrics?')).length;
    await vi.advanceTimersByTimeAsync(10000);
    expect(live()).toBe(3);
    window._pollCfg = { poll_interval: 30 };
    await vi.advanceTimersByTimeAsync(5000);
    const at = live();
    await vi.advanceTimersByTimeAsync(29000);
    expect(live()).toBe(at);
    await vi.advanceTimersByTimeAsync(1000);
    expect(live()).toBe(at + 1);
    expect(vi.getTimerCount()).toBe(1);
  });

  test('a panel pinned to Overall follows the Overall range there and the Dashboards range on its page', async () => {
    load();
    const calls = stubFetch(defaults());
    window.ahState = { range: 24 };
    window.ovState = { prov: 'all', range: 6 };
    window.layout.overallBorrowed = ['vllm-cpu-mem'];
    window._activeTab = 'overall';
    window.adoptPinnedCards = vi.fn();
    await window.hpPaintPinned();
    await settle();
    expect(historyCalls(calls)).toEqual([`/api/history?agent=${ID_A}&since_minutes=360&max_rows=1080`]);
    expect(window.kitTsData(document.querySelector('#hp-b-vllm-cpu-mem .ts[data-kit-ts]')).labels).toHaveLength(360);
    window.ovState.range = 12;
    window.hpRender();
    await settle();
    expect(historyCalls(calls)[1]).toBe(`/api/history?agent=${ID_A}&since_minutes=720&max_rows=432`);
    window._activeTab = 'dashboard';
    window._subTabState = { dashboard: 'vllm' };
    await window.hpEnter('vllm');
    await settle();
    expect(historyCalls(calls)[2]).toBe(`/api/history?agent=${ID_A}&since_minutes=1440&max_rows=864`);
    expect(window.kitTsData(document.querySelector('#hp-b-vllm-cpu-mem .ts[data-kit-ts]')).labels).toHaveLength(288);
    delete window.adoptPinnedCards;
  });
});

const SettingsLib = createRequire(import.meta.url)('../js/lib/settingsdrawer.js');
// foundation.js's real drawer chips, panel toggle, page reset and pin adoption at global scope.
function loadDrawer() {
  const labels = blockSrc(foundation, '// Host page panel ids and titles', '\n// ---------------------------------------------------------------------------\n// Layout persistence');
  evalGlobal([labels.replace(/^const /gm, 'var '), 'var _ovHomeMarks = {};', 'window._ovAdopted = new Set();',
    ...['_esc', '_hiddenList', '_hpPanelMap', 'toggleCard', '_getDashSubTab', '_sdScope', '_sdCardMap', '_sdChip', '_cardLabel',
      '_sdHostPanels', '_sdRenderCards', '_sdSetAllCards', '_kitRows', '_hpResetLayout', '_homeCardEl', 'adoptPinnedCards']
      .map(n => fnSrc(foundation, n)),
    'Object.assign(window, { toggleCard, _sdRenderCards, _sdSetAllCards, _hpResetLayout, adoptPinnedCards });'].join('\n'));
  window.SettingsLib = SettingsLib;
  window._themedConfirm = () => Promise.resolve(true);
  window.renderSettingsPanel = () => {};
  window._ensureSizeBtn = () => {};
  window._activeTab = 'dashboard';
  window._subTabState = { dashboard: 'llamacpp' };
  document.querySelectorAll('#dashboardTab > .active').forEach(el => el.classList.remove('active'));
  document.getElementById('dash-llamacpp').classList.add('active');
  document.body.insertAdjacentHTML('beforeend', '<div id="sdCards"></div><div id="overallGrid"></div>');
}
const DRAWER_KEYS = ['SettingsLib', '_themedConfirm', 'renderSettingsPanel', '_ensureSizeBtn', '_activeTab', '_subTabState', 'toggleCard',
  '_sdRenderCards', '_sdSetAllCards', '_hpResetLayout', 'adoptPinnedCards', '_ovAdopted', '_ovHomeMarks'];

describe('Host page live-check fixes (#1432)', () => {
  test('the network and disk charts read in one unit; their table view uses it too', async () => {
    load();
    const r = defaults();
    r.history = (id) => rowsFor(LEVEL[id], Date.now()).map(x => Object.assign(x, { net_sent: 100 * 1024, net_recv: 50 * 1024,
      io_read: 500 * 1048576, io_write: 20 * 1048576 }));
    stubFetch(r);
    await window.hpEnter('host');
    await settle();
    const names = (id) => window.kitTsData(document.querySelector(`#hp-b-host-${id} .ts[data-kit-ts]`)).series.map(s => s.name);
    expect(names('net')).toEqual(['Out (KB/s)', 'In (KB/s)']);
    expect(names('io')).toEqual(['Read (MB/s)', 'Write (MB/s)']);
    const table = document.getElementById('hp-b-host-io').closest('.panel').querySelector('.p-table table');
    expect([...table.tHead.rows[0].cells].map(c => c.textContent)).toEqual(['Time', 'Read (MB/s)', 'Write (MB/s)']);
    const filled = [...table.tBodies[0].rows].find(tr => tr.cells[1].textContent !== '');
    expect(filled.cells[1].textContent).toBe('500');
    expect(document.querySelector('#hp-host [data-card="host-tiles"] .p-title').textContent).toBe('Snapshot');
  });
});

describe('Host page panels in the settings drawer (#1432)', () => {
  afterEach(() => { DRAWER_KEYS.forEach(k => { delete window[k]; }); });

  test('the drawer lists each panel of the page as a chip; a chip hides and shows it on the page and saves', async () => {
    load();
    stubFetch(defaults());
    loadDrawer();
    await window.hpEnter('llama');
    await settle();
    window._sdRenderCards();
    const chips = [...document.querySelectorAll('#sdCards .sd-chip[data-sd="card"]')];
    expect(chips.map(c => c.dataset.id)).toEqual(HP.panelsFor('llama').map(p => 'llama-' + p.name));
    expect(chips.find(c => c.dataset.id === 'llama-net').textContent).toBe('Network');
    expect(chips.every(c => c.getAttribute('aria-pressed') === 'true')).toBe(true);
    expect(document.querySelector('#sdCards input[type="checkbox"]')).toBeNull();
    window.toggleCard('llama-net', false);
    expect(window.layout.llamaPanelsHidden).toEqual(['llama-net']);
    expect(document.querySelector('[data-card="llama-net"]').hidden).toBe(true);
    expect(window.saveLayout).toHaveBeenCalled();
    window._sdRenderCards();
    expect(document.querySelector('#sdCards [data-id="llama-net"]').getAttribute('aria-pressed')).toBe('false');
    window.toggleCard('llama-net', true);
    expect(window.layout.llamaPanelsHidden).toEqual([]);
    expect(document.querySelector('[data-card="llama-net"]').hidden).toBe(false);
    window.saveLayout.mockClear();
    const render = vi.spyOn(window, 'hpRender');
    window._sdSetAllCards(false);
    expect(window.layout.llamaPanelsHidden).toEqual(HP.panelsFor('llama').map(p => 'llama-' + p.name));
    expect(window.layout.lmsPanelsHidden).toBeUndefined();
    expect(window.saveLayout).toHaveBeenCalledTimes(1);
    expect(render).toHaveBeenCalledTimes(1);
    expect(document.querySelector('[data-card="llama-io"]').hidden).toBe(true);
    window._sdSetAllCards(true);
    expect(window.layout.llamaPanelsHidden).toEqual([]);
    expect(window.saveLayout).toHaveBeenCalledTimes(2);
    expect(render).toHaveBeenCalledTimes(2);
    expect(document.querySelector('[data-card="llama-io"]').hidden).toBe(false);
  });

  test('role presets do not run on a host page and leave its hidden panels as they are', async () => {
    load();
    stubFetch(defaults());
    loadDrawer();
    const charts = srcFile('js/charts.js');
    evalGlobal([fnSrc(charts, '_activeTabLayoutKeys'), fnSrc(charts, 'applyRolePreset'), 'window.applyRolePreset = applyRolePreset;'].join('\n'));
    window.layout.llamaPanelsHidden = ['llama-net'];
    window.layout.rolePreset = {};
    window.saveLayout.mockClear();
    window.applyRolePreset('charts');
    expect(window.layout.llamaPanelsHidden).toEqual(['llama-net']);
    expect(window.layout.rolePreset).toEqual({});
    expect(window.saveLayout).not.toHaveBeenCalled();
    delete window.applyRolePreset;
  });

  test('a panel hidden on its page shows when Overall adopts it', async () => {
    load();
    stubFetch(defaults());
    loadDrawer();
    await window.hpEnter('llama');
    await settle();
    window.toggleCard('llama-cpu-mem', false);
    expect(document.querySelector('#hp-llama [data-card="llama-cpu-mem"]').hidden).toBe(true);
    document.getElementById('overallGrid').innerHTML = '<div class="card ov-shell" data-card="ov-borrow-llama-cpu-mem"></div>';
    window.layout.overallBorrowed = ['llama-cpu-mem'];
    window.adoptPinnedCards();
    const panel = document.querySelector('#overallGrid [data-card="llama-cpu-mem"]');
    expect(panel).not.toBeNull();
    expect(panel.hidden).toBe(false);
  });

  test('"Reset this page" shows every panel again and folds the empty Power hardware row again', async () => {
    load();
    stubFetch(defaults());
    loadDrawer();
    window.kitConfigure({ store: window.mcShellStore });
    window.layout.llamaPanelsHidden = ['llama-net'];
    await window.hpEnter('llama');
    await settle();
    const row = () => document.querySelector('#hp-llama [data-row="llama-hw"]');
    expect(row().classList.contains('folded')).toBe(true);
    await window._hpResetLayout();
    await settle();
    window.kitConfigure({ store: null });
    expect(window.layout.llamaPanelsHidden).toEqual([]);
    expect(document.querySelector('[data-card="llama-net"]').hidden).toBe(false);
    expect(row().classList.contains('folded')).toBe(true);
    expect(row().querySelector('.hp-row-note').hidden).toBe(false);
  });
});

describe('Host page operator fixes (#1432)', () => {
  afterEach(() => { delete window.lastNonZero; delete window._llamaPeaks; });

  test('host page panels on a line share one height and long key-value lists flow into two columns when wide', () => {
    const css = srcFile('css/hostpage.css');
    expect(css).toMatch(/\.hp-root \.panels \{ align-items: stretch; \}/);
    expect(css).toMatch(/\.hp-root \.panels > \.panel \{ container-type: inline-size; \}/);
    expect(css).toMatch(/@container \(min-width: 460px\) \{[\s\S]*\.hp-kv\.flow tbody \{ display: grid; grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  });

  test('llama.cpp switched to a second host shows that host\'s context, numbers and slot peaks, never the first host\'s', async () => {
    load({ llama: [agent(ID_A), agent(ID_B)], lms: [], vllm: [] }, { llama: ID_A });
    window._selectedAgents.llama = ID_A;
    window._activeTab = 'dashboard';
    window._subTabState = { dashboard: 'llamacpp' };
    // The dashboard's slot peak is the first host's; both hosts serve one model, each with its own context.
    window.lastNonZero = { active_slots: { val: 4, ts: new Date().toISOString() },
      requests_processing: { val: null, ts: null }, requests_deferred: { val: null, ts: null } };
    window._llamaPeaks = { tps: LMPeaks.makeTracker(LMPeaks.RATE_WINDOW_MS), pps: LMPeaks.makeTracker(LMPeaks.RATE_WINDOW_MS) };
    const r = defaults(), CTX = { [ID_A]: '8192', [ID_B]: '4096' };
    r.state = () => ({ state: 'idle', agent_online: true, aggregate: 'active', model: 'qwen' });
    // Models answer per agent id; a request without one gets the first host's.
    r.models = (id) => ({ data: [{ id: 'qwen', status: { value: 'loaded', args: ['--ctx-size', CTX[id || ID_A]] } }] });
    const calls = stubFetch(r);
    await window.hpEnter('llama');
    await settle();
    const sel = document.querySelector('#hp-llama select.hp-pick');
    sel.value = ID_B;
    sel.dispatchEvent(new window.Event('change', { bubbles: true }));
    await settle();
    const dd = (label) => [...document.querySelectorAll('#hp-b-llama-server dt')].find(dt => dt.textContent === label).nextElementSibling.textContent;
    const row = (label) => [...document.querySelectorAll('#hp-b-llama-details tr')].find(tr => tr.cells[0].textContent === label).cells[1].textContent;
    expect(sel.value).toBe(ID_B);
    expect(calls).toContain(`/api/llm/models?agent=${ID_B}`);
    expect(dd('Context')).toMatch(/^4/);
    expect(document.getElementById('hp-b-llama-tiles').textContent).toContain('70%');
    expect(row('Active slots')).toBe('0');
  });

  test('host page line charts break at a long gap instead of dropping to 0', async () => {
    load();
    const r = defaults();
    // Data 45 to 59 and 0 to 14 minutes ago: a 30-minute gap, longer than the 3-bucket hold.
    r.history = (id) => rowsFor(LEVEL[id], Date.now()).filter((x, i) => i < 15 || i >= 45);
    stubFetch(r);
    await window.hpEnter('llama');
    await settle();
    const ts = document.querySelector('#hp-b-llama-cpu-mem .ts[data-kit-ts]');
    expect(window.kitTsData(ts).gaps).toBe('break');
    const d = ts.querySelector('path[fill="none"]').getAttribute('d');
    expect((d.match(/M/g) || []).length).toBe(2);
  });

  test('a Mac\'s Disk space bars and table list its data volume once as Macintosh HD (/), then other drives', async () => {
    load({ llama: [], lms: [agent(ID_A)], vllm: [] });
    const mounts = ['/', '/System/Volumes/VM', '/System/Volumes/Preboot', '/System/Volumes/Data',
      '/private/var/run/com.apple.security.cryptexd/mnt/a1', '/Library/Developer/CoreSimulator/Volumes/iOS', '/Volumes/Backup'];
    const r = defaults();
    r.lms = (id) => ({ agent_online: true, ts: new Date().toISOString(), ps: [], models: [], mac_power: { gpu_busy_pct: 5 },
      system: Object.assign(SYS(LEVEL[id]), { disk: mounts.map(m => ({ mountpoint: m, percent: 20, used_bytes: 2e10, total_bytes: 1e11 })) }) });
    stubFetch(r);
    await window.hpEnter('lms');
    await settle();
    const bars = [...document.querySelectorAll('#hp-b-lms-disk .hp-disks .hp-dm')].map(e => e.textContent);
    expect(bars).toEqual(['Macintosh HD (/)', '/Volumes/Backup']);
    const table = document.getElementById('hp-b-lms-disk').closest('.panel').querySelector('.p-table table');
    expect([...table.tBodies[0].rows].map(tr => tr.cells[0].textContent)).toEqual(['Macintosh HD (/)', '/Volumes/Backup']);
  });

  test('Tokens per hour stacks prompt on generated tokens from the host\'s energy record, LM Studio and vLLM pages too', async () => {
    load({ llama: [], lms: [agent(ID_A)], vllm: [] });
    const r = defaults(), hr = 3600000, cur = Math.floor(Date.now() / hr) * hr / 1000;
    r.energy = () => ({ ok: true, rows: [{ hour_ts: cur - 3600, tokens_gen: 1200, tokens_prompt: 4000 }, { hour_ts: cur, tokens_gen: 300 }] });
    const calls = stubFetch(r);
    await window.hpEnter('lms');
    await settle();
    expect(calls).toContain('/api/energy/hourly?hours=24&agent=' + encodeURIComponent(ID_A));
    const ts = document.querySelector('#hp-b-lms-gen .ts[data-kit-ts]');
    const o = JSON.parse(ts.getAttribute('data-kit-ts'));
    expect(o.mode).toBe('stack');
    expect(o.idleLine).toBe(true);
    expect(o.labels).toHaveLength(24);
    expect(o.series.map(x => x.name)).toEqual(['Generated', 'Prompt']);
    expect(o.series[0].values.slice(-2)).toEqual([1200, 300]);
    expect(o.series[1].values.slice(-2)).toEqual([4000, 0]);
    expect(document.querySelector('[data-card="lms-gen"] .p-hd .p-note').textContent).toBe('last 24 h');
  });

  test('Tokens per hour keeps the last 24 hourly bars on a 6 h range', async () => {
    load({ llama: [], lms: [agent(ID_A)], vllm: [] });
    window.ahState.range = 6;
    const r = defaults(), cur = Math.floor(Date.now() / 3600000) * 3600;
    r.energy = () => ({ ok: true, rows: [{ hour_ts: cur - 20 * 3600, tokens_gen: 50, tokens_prompt: 70 }] });
    const calls = stubFetch(r);
    await window.hpEnter('lms');
    await settle();
    expect(calls).toContain('/api/energy/hourly?hours=24&agent=' + encodeURIComponent(ID_A));
    const o = JSON.parse(document.querySelector('#hp-b-lms-gen .ts[data-kit-ts]').getAttribute('data-kit-ts'));
    expect(o.labels).toHaveLength(24);
    expect(o.series[0].values[3]).toBe(50);
  });
});

describe('Host page review minors (#1432)', () => {
  afterEach(() => { delete window._llamaPeaks; });

  test('llama.cpp feeds its own rate peaks while it reads the dashboard\'s, so dropping to its own keeps them', async () => {
    load();
    window._llamaPeaks = { tps: LMPeaks.makeTracker(LMPeaks.RATE_WINDOW_MS), pps: LMPeaks.makeTracker(LMPeaks.RATE_WINDOW_MS) };
    const r = defaults();
    r.metrics = (id) => Object.assign(llamaSample(id), { llama: { tokens_per_second: 30 } });
    stubFetch(r);
    await window.hpEnter('llama');
    await settle();
    delete window._llamaPeaks;
    window.hpRender();
    const gen = [...document.querySelectorAll('#hp-b-llama-details tr')].find(tr => tr.cells[0].textContent === 'Gen tokens/s').cells[1].textContent;
    expect(gen).toMatch(/^30\.0 · live · peak 30\.0/);
  });
});

describe('llama Server details while the server sleeps (#1432)', () => {
  test('a sleeping host keeps its last counters with a note; a host switch clears them; offline shows dashes', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load({ llama: [agent(ID_A), agent(ID_B)], lms: [], vllm: [] }, { llama: ID_A });
    window._selectedAgents.llama = ID_A;
    window._activeTab = 'dashboard';
    window._subTabState = { dashboard: 'llamacpp' };
    const r = defaults();
    let asleep = false, online = true;
    r.metrics = (id) => Object.assign(llamaSample(id), { llama: asleep ? { tokens_per_second: null, active_slots: 0 }
      : { tokens_per_second: 5, active_slots: 0, total_tokens_generated: 12000, total_slots: 4 } });
    r.state = () => ({ state: 'idle', agent_online: online, aggregate: asleep ? 'sleeping' : 'active' });
    stubFetch(r);
    window.hpEnter('llama');
    await vi.advanceTimersByTimeAsync(0);
    const row = (label) => [...document.querySelectorAll('#hp-b-llama-details tr')].find(tr => tr.cells[0].textContent === label).cells[1].textContent;
    const noteText = () => document.querySelector('[data-card="llama-details"] .p-hd .p-note')?.textContent || '';
    expect(row('Tokens generated')).toBe('12,000');
    expect(noteText()).toBe('');
    asleep = true;
    await vi.advanceTimersByTimeAsync(5000);
    expect(row('Tokens generated')).toBe('12,000');
    expect(row('Total slots')).toBe('4');
    // Rates and slots stay live: no live rate now, so the rate reads its 60-minute average.
    expect(row('Gen tokens/s')).not.toContain('live');
    expect(row('Active slots')).toBe('0');
    expect(noteText()).toMatch(/^Last seen \d\d:\d\d · server sleeping$/);
    // A range change on the same host keeps them.
    window.ahState.range = 6;
    window.hpRender();
    await vi.advanceTimersByTimeAsync(0);
    expect(row('Tokens generated')).toBe('12,000');
    expect(noteText()).toMatch(/^Last seen \d\d:\d\d · server sleeping$/);
    online = false;
    await vi.advanceTimersByTimeAsync(5000);
    expect(row('Tokens generated')).toBe('—');
    expect(noteText()).toBe('');
    online = true;
    // The second host opens asleep: nothing is known of it yet.
    await window.hpSetHost('llama', ID_B);
    await vi.advanceTimersByTimeAsync(0);
    expect(row('Tokens generated')).toBe('—');
    expect(noteText()).toBe('');
  });
});

describe('Host page equal rows and chart styles (#1432)', () => {
  const tsOf = (sel) => window.kitTsData(document.querySelector(sel + ' .ts[data-kit-ts]'));
  async function llamaPage() {
    load({ llama: [agent(ID_A)], lms: [], vllm: [] }, { llama: ID_A });
    window._activeTab = 'dashboard';
    window._subTabState = { dashboard: 'llamacpp' };
    stubFetch(defaults());
    await window.hpEnter('llama');
    await settle();
  }

  test('Throughput draws split peak bars with averages and an idle message; GPU busy draws a busy strip over memory steps', async () => {
    await llamaPage();
    const tp = tsOf('#hp-b-llama-tps');
    expect(tp.bars).toBe('peak');
    expect(tp.split).toBe(true);
    expect(tp.idle).toBe('Idle · no requests in this range');
    expect(tp.series.map(x => x.name)).toEqual(['Prompt t/s', 'Gen t/s']);
    expect(tp.series.map(x => x.slot)).toEqual([2, 1]);
    expect(tp.series[1].avg.filter(v => v != null).length).toBeGreaterThan(0);
    // 15-minute buckets over 24 hours.
    expect(tp.labels.length).toBe(96);
    const gb = tsOf('#hp-b-llama-gpu-busy');
    expect(gb.strip).toBe('GPU busy %');
    expect(gb.stripLabel).toBe('Busy');
    expect(gb.step).toBe(true);
    expect(gb.labels.length).toBe(288);
    const head = [...document.querySelectorAll('[data-card="llama-tps"] .p-table th')].map(th => th.textContent);
    expect(head).toEqual(['Time', 'Prompt t/s peak', 'Prompt t/s avg', 'Gen t/s peak', 'Gen t/s avg']);
  });

  test('a key-value list of six or more rows is marked to flow and a short one is not', async () => {
    await llamaPage();
    expect(document.querySelector('#hp-b-llama-details > .hp-kv:last-child').classList.contains('flow')).toBe(true);
    expect(document.querySelector('#hp-b-llama-cpu-details .hp-kv').classList.contains('flow')).toBe(false);
  });

  test('Server details gives the generation and prompt rates their own lines above the flowing list; the tiles add Prompt tokens/s', async () => {
    await llamaPage();
    const rates = document.querySelector('#hp-b-llama-details > .hp-kv:first-child');
    expect(rates.classList.contains('flow')).toBe(false);
    expect([...rates.querySelectorAll('tbody tr')].map(tr => tr.cells[0].textContent)).toEqual(['Prompt tokens/s', 'Gen tokens/s']);
    expect(rates.nextElementSibling.classList.contains('flow')).toBe(true);
    const rest = [...rates.nextElementSibling.querySelectorAll('tbody tr')].map(tr => tr.cells[0].textContent);
    expect(rest).not.toContain('Gen tokens/s');
    expect(rest).not.toContain('Prompt tokens/s');
    const table = [...document.querySelectorAll('[data-card="llama-details"] .p-table tbody tr')].map(tr => tr.cells[0].textContent);
    expect(table.slice(0, 2)).toEqual(['Prompt tokens/s', 'Gen tokens/s']);
    const labels = [...document.querySelectorAll('#hp-b-llama-tiles .hp-tl')].map(t => t.textContent), at = labels.indexOf('Prompt tokens/s');
    expect(labels.slice(at, at + 2)).toEqual(['Prompt tokens/s', 'Gen tokens/s']);
  });

  test('the last panel on a line spans the empty columns after it, and charts grow to the tallest panel on their line', async () => {
    const P = window.HTMLElement.prototype, gcs = window.getComputedStyle;
    const tops = { 'llama-tps': 100, 'llama-server': 100, 'llama-details': 100, 'llama-gen': 600 };
    const heights = { 'llama-tps': 300, 'llama-server': 250, 'llama-details': 400, 'llama-gen': 260 };
    const free = { 'llama-tps': 80 };
    const rootW = Object.getOwnPropertyDescriptor(P, 'clientWidth');
    Object.defineProperty(P, 'clientWidth', { configurable: true, get() { return this.classList.contains('hp-root') ? 1600 : 500; } });
    const rect = P.getBoundingClientRect;
    P.getBoundingClientRect = function () {
      const c = this.getAttribute('data-card');
      return c && c in tops ? { top: tops[c], height: heights[c], bottom: tops[c] + heights[c], left: 0, right: 0, width: 500 } : rect.call(this);
    };
    window.getComputedStyle = (el) => (el.classList && el.classList.contains('panels') ? { gridTemplateColumns: '500px 500px 500px' } : gcs(el));
    const kfh = window.kitFreeHeight;
    window.kitFreeHeight = (body) => free[body.closest('.panel').getAttribute('data-card')] || 0;
    try {
      await llamaPage();
      const span = (c) => document.querySelector('[data-card="' + c + '"]').style.gridColumn;
      expect(span('llama-gen')).toBe('span 3');
      expect(span('llama-tps')).toBe('');
      expect(document.querySelector('[data-card="llama-tiles"]').style.gridColumn).toBe('');
      // Throughput's natural height is 300 - 80 = 220 against Server details' 400: its chart grows by 180.
      expect(tsOf('#hp-b-llama-tps').height).toBe(350);
      // Once grown it fills its panel and keeps that height on the next paint.
      heights['llama-tps'] = 400; free['llama-tps'] = 0;
      window.hpFit(document);
      window.hpRender();
      await settle();
      expect(tsOf('#hp-b-llama-tps').height).toBe(350);
      // A shorter tallest panel shrinks it again.
      heights['llama-details'] = 300;
      window.hpFit(document);
      expect(tsOf('#hp-b-llama-tps').height).toBe(250);
    } finally {
      window.getComputedStyle = gcs;
      window.kitFreeHeight = kfh;
      P.getBoundingClientRect = rect;
      if (rootW) Object.defineProperty(P, 'clientWidth', rootW); else delete P.clientWidth;
    }
  });

  test('a narrower grid recounts its columns from cleared spans, and a chart drawn at another width is redrawn at its holder\'s', async () => {
    const P = window.HTMLElement.prototype, gcs = window.getComputedStyle;
    let cols = 3, w = 500;
    const rootW = Object.getOwnPropertyDescriptor(P, 'clientWidth');
    Object.defineProperty(P, 'clientWidth', { configurable: true, get() { return this.classList.contains('hp-root') ? 1600 : w; } });
    // A panel still spanning 3 adds implicit columns, as a real grid lists them.
    window.getComputedStyle = (el) => {
      if (!(el.classList && el.classList.contains('panels'))) return gcs(el);
      const span = Math.max(0, ...Array.from(el.children).map(c => +((/span (\d+)/.exec(c.style.gridColumn) || [])[1] || 0)));
      return { gridTemplateColumns: new Array(Math.max(cols, span)).fill('400px').join(' ') };
    };
    try {
      await llamaPage();
      const span = (c) => document.querySelector('[data-card="' + c + '"]').style.gridColumn;
      expect(span('llama-gen')).toBe('span 3');
      cols = 2; w = 700;
      window.hpFit(document);
      // tps, server | details, gen: four panels fill two lines of two.
      expect(span('llama-gen')).toBe('');
      expect(span('llama-details')).toBe('');
      expect(tsOf('#hp-b-llama-tps').w).toBe(700);
    } finally {
      window.getComputedStyle = gcs;
      if (rootW) Object.defineProperty(P, 'clientWidth', rootW); else delete P.clientWidth;
    }
  });

  test('GPU busy leaves out the busy threshold its strip shows and keeps the VRAM one', () => {
    expect(HP.chartMetrics('gpu-busy')).toEqual([['system', 'gpu_vram_usage_percent']]);
    expect(HP.chartMetrics('cpu-mem')).toEqual(HP.CHART_METRIC['cpu-mem']);
    expect(HP.chartMetrics('tps')).toEqual([]);
  });

  test('a chart panel\'s menu lists "View … in metrics" for its stored series and opens that metric of the picked host', async () => {
    await llamaPage();
    const items = (card) => [...document.querySelectorAll(`[data-card="${card}"] .p-pop [data-hp-metric]`)];
    expect(items('llama-cpu-mem').map(b => b.textContent)).toEqual(['View CPU % in metrics', 'View Memory % in metrics']);
    expect(items('llama-tps').map(b => b.getAttribute('data-hp-metric'))).toEqual(['llama/prompt_tokens_per_second', 'llama/tokens_per_second']);
    expect(items('llama-disk').map(b => b.textContent)).toEqual(['View disk / used % in metrics']);
    expect(items('llama-details')).toEqual([]);
    window.focusAlarmMetric = vi.fn();
    items('llama-cpu-mem')[1].click();
    expect(window.focusAlarmMetric).toHaveBeenCalledWith('llm-a', 'system', 'ram_percent');
    // A repaint keeps one set of items.
    window.hpRender();
    await settle();
    expect(items('llama-cpu-mem')).toHaveLength(2);
    delete window.focusAlarmMetric;
  });

  test('host pages turn the browser\'s own scroll anchoring off and keep the reader\'s place themselves', () => {
    expect(srcFile('css/hostpage.css')).toMatch(/\.hp-root \{ overflow-anchor: none; \}/);
  });

  test('a repaint scrolls back by however far the first panel in view moved', async () => {
    await llamaPage();
    const P = window.HTMLElement.prototype, rect = P.getBoundingClientRect;
    const sy = Object.getOwnPropertyDescriptor(window, 'scrollY'), rootW = Object.getOwnPropertyDescriptor(P, 'clientWidth');
    let shift = 0;
    Object.defineProperty(window, 'scrollY', { configurable: true, get: () => 800 });
    Object.defineProperty(P, 'clientWidth', { configurable: true, get() { return this.classList.contains('hp-root') ? 1600 : 0; } });
    P.getBoundingClientRect = function () {
      const c = this.getAttribute('data-card');
      if (c === 'llama-tiles') return { top: -500, bottom: -400, height: 100, left: 0, right: 0, width: 0 };
      if (c === 'llama-tps') return { top: 40 + shift, bottom: 300 + shift, height: 260, left: 0, right: 0, width: 0 };
      return rect.call(this);
    };
    const kfh = window.kitFreeHeight;
    window.kitFreeHeight = () => { shift = 35; return 0; };
    window.scrollBy = vi.fn();
    try {
      window.hpRender();
      await settle();
      expect(window.scrollBy).toHaveBeenCalledWith(0, 35);
    } finally {
      window.kitFreeHeight = kfh;
      P.getBoundingClientRect = rect;
      if (sy) Object.defineProperty(window, 'scrollY', sy); else delete window.scrollY;
      if (rootW) Object.defineProperty(P, 'clientWidth', rootW); else delete P.clientWidth;
    }
  });

  test('a "View … in metrics" item on a panel pinned to Overall opens its page\'s host, closes full size and returns focus to the menu button', async () => {
    await llamaPage();
    const panel = document.querySelector('[data-card="llama-cpu-mem"]');
    const shell = document.createElement('div');
    shell.className = 'card ov-shell';
    document.body.appendChild(shell);
    shell.appendChild(panel);
    window.kitSetFull(panel, true);
    window.focusAlarmMetric = vi.fn();
    panel.querySelector('.p-pop [data-hp-metric="system/cpu_total"]').click();
    expect(window.focusAlarmMetric).toHaveBeenCalledWith('llm-a', 'system', 'cpu_total');
    expect(panel.classList.contains('full')).toBe(false);
    expect(document.activeElement).toBe(panel.querySelector('[data-kit-menu]'));
    delete window.focusAlarmMetric;
    shell.remove();
  });

  async function placeHarness(rects) {
    await llamaPage();
    const P = window.HTMLElement.prototype, rect = P.getBoundingClientRect;
    const sy = Object.getOwnPropertyDescriptor(window, 'scrollY'), rootW = Object.getOwnPropertyDescriptor(P, 'clientWidth');
    Object.defineProperty(window, 'scrollY', { configurable: true, get: () => 800 });
    Object.defineProperty(P, 'clientWidth', { configurable: true, get() { return this.classList.contains('hp-root') ? 1600 : 0; } });
    P.getBoundingClientRect = function () {
      const c = this.getAttribute('data-card'), r = c && rects[c];
      return r ? Object.assign({ left: 0, right: 0, width: 0, bottom: r.top + r.height }, r) : rect.call(this);
    };
    window.scrollBy = vi.fn();
    return () => {
      P.getBoundingClientRect = rect;
      if (sy) Object.defineProperty(window, 'scrollY', sy); else delete window.scrollY;
      if (rootW) Object.defineProperty(P, 'clientWidth', rootW); else delete P.clientWidth;
    };
  }

  test('a panel hidden by the repaint is not scrolled back to, and a full-size panel is never the place', async () => {
    const rects = { 'llama-tiles': { top: -500, height: 100 }, 'llama-tps': { top: 40, height: 260 }, 'llama-server': { top: 40, height: 260 } };
    const done = await placeHarness(rects);
    const kfh = window.kitFreeHeight;
    try {
      window.kitFreeHeight = () => { rects['llama-tps'] = { top: 0, height: 0 }; return 0; };
      window.hpRender();
      await settle();
      expect(window.scrollBy).not.toHaveBeenCalled();
      rects['llama-tps'] = { top: 40, height: 260 };
      document.querySelector('[data-card="llama-tps"]').classList.add('full');
      window.kitFreeHeight = () => { rects['llama-server'] = { top: 75, height: 260 }; return 0; };
      window.hpRender();
      await settle();
      expect(window.scrollBy).toHaveBeenCalledWith(0, 35);
    } finally {
      document.querySelector('[data-card="llama-tps"]').classList.remove('full');
      window.kitFreeHeight = kfh;
      done();
    }
  });

  test('a refit after a width change returns to the place noted when the reader last scrolled', async () => {
    const rects = { 'llama-tiles': { top: -500, height: 100 }, 'llama-tps': { top: 40, height: 260 } };
    const done = await placeHarness(rects);
    try {
      window.dispatchEvent(new Event('scroll'));
      await new Promise(r => setTimeout(r, 40));
      // The window narrows: the panel moves and the browser scrolls before the refit runs.
      rects['llama-tps'] = { top: 160, height: 300 };
      Object.defineProperty(window.HTMLElement.prototype, 'clientWidth', { configurable: true, get() { return this.classList.contains('hp-root') ? 1200 : 0; } });
      window.dispatchEvent(new Event('scroll'));
      await new Promise(r => setTimeout(r, 40));
      window.hpFit(document);
      expect(window.scrollBy).toHaveBeenCalledWith(0, 120);
    } finally {
      done();
    }
  });
});
