// #1432 phase 4c-2: the host pages (llama.cpp, LM Studio, vLLM, Host) built by js/hostpage.js.
// Runs kit.js, overall-view, hosts-view, peaks, host-view and hostpage.js on the real #dashboardTab markup with fetch stubbed.
import { describe, test, expect, afterEach, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import OV from '../js/lib/overall-view.js';
import HV from '../js/lib/hosts-view.js';
import HP from '../js/lib/host-view.js';
import LMPeaks from '../js/lib/peaks.js';
import { srcFile, fnSrc, evalGlobal, flush, loadSwitchSubTab } from './helpers/harness.js';

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
const allhosts = srcFile('js/allhosts.js');
const foundation = srcFile('js/foundation.js');
const boot = srcFile('js/boot.js');
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
    if (url.startsWith('/api/llm/models')) return ok(routes.models());
    if (url.startsWith('/api/lmstudio/metrics?')) return ok(routes.lms(aid));
    if (url.startsWith('/api/vllm/metrics?')) return ok(routes.vllm(aid));
    if (url === '/api/agents/hosts') return ok(routes.hosts());
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

describe('Host pages (#1432)', () => {
  test('llama.cpp builds five rows with its panels; the host page has no Inference row', async () => {
    load();
    stubFetch(defaults());
    await window.hpEnter('llama');
    await settle();
    const root = document.getElementById('hp-llama');
    expect([...root.querySelectorAll('section.row[data-row]')].map(r => r.dataset.row))
      .toEqual(['llama-tiles', 'llama-inf', 'llama-gpu', 'llama-sys', 'llama-hw']);
    const cards = [...root.querySelectorAll('section.panel[data-card]')].map(p => p.dataset.card);
    expect(cards).toEqual(window.hpPagePanelIds('llama'));
    for (const id of ['llama-details', 'llama-gen', 'llama-tps', 'llama-server', 'llama-cpu-mem', 'llama-fans']) expect(cards, id).toContain(id);
    expect(cards).not.toContain('llama-models');
    for (const p of root.querySelectorAll('section.panel[data-card]')) {
      expect(p.querySelector('.p-body').id).toBe('hp-b-' + p.dataset.card);
      expect(p.querySelector('.p-hd .p-title').textContent).toBe(p.dataset.title);
    }
    expect(root.querySelectorAll('button.row-hd[data-row-fold]').length).toBe(5);
    expect(root.getAttribute('data-kit-page')).toBe('host-llama');
    expect(document.getElementById('hp-b-llama-tiles').textContent).toContain('CPU');
    expect(document.getElementById('hp-b-llama-tiles').textContent).toContain('30%');

    await window.hpEnter('host');
    await settle();
    const host = document.getElementById('hp-host');
    expect(host.querySelectorAll('section.row[data-row]').length).toBe(4);
    expect(host.querySelector('[data-card="host-tps"]')).toBeNull();
    expect(host.querySelector('[data-row="host-inf"]')).toBeNull();
    for (const bad of ['NaN', 'undefined', 'null', 'Infinity']) {
      expect(root.textContent, bad).not.toContain(bad);
      expect(host.textContent, bad).not.toContain(bad);
    }
  });

  test('the picker lists the provider hosts; a change picks the host and refetches its history', async () => {
    load({ llama: [agent(ID_A), agent(ID_B, { online: false })], lms: [], vllm: [] });
    const calls = stubFetch(defaults());
    await window.hpEnter('llama');
    await settle();
    const sel = document.querySelector('#hp-llama select.hp-pick');
    expect(sel.closest('label').textContent).toContain('Host');
    expect([...sel.options].map(o => [o.value, o.textContent])).toEqual([[ID_A, 'llm-a'], [ID_B, 'llm-b (offline)']]);
    expect(sel.disabled).toBe(false);
    expect(sel.value).toBe(ID_A);
    expect(historyCalls(calls)).toEqual([`/api/history?agent=${ID_A}&since_minutes=1440&max_rows=864`]);
    sel.value = ID_B;
    sel.dispatchEvent(new window.Event('change', { bubbles: true }));
    await settle();
    expect(window._selectAgent).toHaveBeenCalledWith('llama', ID_B);
    expect(historyCalls(calls)[1]).toBe(`/api/history?agent=${ID_B}&since_minutes=1440&max_rows=864`);
    expect(calls).toContain(`/api/metrics?agent=${ID_B}`);
    expect(cpuValues().every(v => v === 70)).toBe(true);
  });

  test('one host: the picker is disabled and still names it', async () => {
    load();
    stubFetch(defaults());
    await window.hpEnter('vllm');
    await settle();
    const sel = document.querySelector('#hp-vllm select.hp-pick');
    expect(sel.disabled).toBe(true);
    expect(sel.options[0].textContent).toBe('llm-a');
  });

  test('a saved host-page id that is gone falls back to the first host and saves it', async () => {
    load(undefined, { system: 'gone-id' });
    const calls = stubFetch(defaults());
    await window.hpEnter('host');
    await settle();
    expect(window.layout._selectedAgents.system).toBe(ID_A);
    expect(window.saveLayout).toHaveBeenCalled();
    expect(document.querySelector('#hp-host select.hp-pick').value).toBe(ID_A);
    expect(calls).toContain(`/api/agents/${ID_A}/system`);
    expect(historyCalls(calls)).toEqual([`/api/history?agent=${ID_A}&since_minutes=1440&max_rows=864`]);
  });

  test('no hosts: every panel says so', async () => {
    load({ llama: [], lms: [], vllm: [] });
    const calls = stubFetch(defaults());
    await window.hpEnter('vllm');
    await settle();
    for (const id of window.hpPagePanelIds('vllm')) {
      expect(document.getElementById('hp-b-' + id).textContent, id).toBe('No hosts reported yet.');
    }
    expect(historyCalls(calls)).toHaveLength(0);
  });

  test('a host with no GPU hides the GPU row; a Mac shows Mac power instead of the GPU panels', async () => {
    load();
    const r = defaults();
    r.lms = (id) => ({ agent_online: true, ts: new Date().toISOString(), system: SYS(LEVEL[id]), ps: [], models: [],
      mac_power: { soc_total_w: 6, cpu_package_w: 3, gpu_w: 2, ane_w: 1, gpu_busy_pct: 25, thermal_pressure_n: 0 } });
    stubFetch(r);
    await window.hpEnter('vllm');
    await settle();
    expect(document.querySelector('#hp-vllm [data-row="vllm-gpu"]').hidden).toBe(true);
    expect(document.querySelector('#hp-vllm [data-row="vllm-sys"]').hidden).toBe(false);

    await window.hpEnter('lms');
    await settle();
    expect(document.querySelector('#hp-lms [data-row="lms-gpu"]').hidden).toBe(false);
    const panel = (id) => document.querySelector(`#hp-lms [data-card="${id}"]`);
    expect(panel('lms-mac-power').hidden).toBe(false);
    expect(panel('lms-mac-gpu').hidden).toBe(false);
    expect(panel('lms-gpu-busy').hidden).toBe(true);
    expect(panel('lms-gpu-details').hidden).toBe(true);
    expect(document.getElementById('hp-b-lms-mac-power').textContent).toContain('Neural Engine');
  });

  test('switching hosts while a history answer is slow paints only the newest host', async () => {
    load({ llama: [agent(ID_A), agent(ID_B)], lms: [], vllm: [] });
    const r = defaults(), held = {};
    r.history = (id) => new Promise(res => { held[id] = () => res(rowsFor(LEVEL[id], Date.now())); });
    r.metrics = (id) => new Promise(res => { held['m' + id] = () => res(defaults().metrics(id)); });
    stubFetch(r);
    window.hpEnter('llama');
    await settle();
    window.hpSetHost('llama', ID_B);
    await settle();
    held[ID_B](); held['m' + ID_B]();
    await settle();
    held[ID_A](); held['m' + ID_A]();
    await settle();
    expect(document.querySelector('#hp-llama select.hp-pick').value).toBe(ID_B);
    const vals = cpuValues();
    expect(vals.length).toBeGreaterThan(0);
    expect(vals.every(v => v === 70)).toBe(true);
    expect(document.getElementById('hp-b-llama-tiles').textContent).toContain('70%');
  });

  test('a host with no power hardware folds that row with "Nothing reported"', async () => {
    load();
    stubFetch(defaults());
    await window.hpEnter('llama');
    await settle();
    const row = document.querySelector('#hp-llama [data-row="llama-hw"]');
    expect(row.classList.contains('folded')).toBe(true);
    const n = row.querySelector('.hp-row-note');
    expect(n.hidden).toBe(false);
    expect(n.textContent).toBe('Nothing reported');
    for (const x of ['ups', 'aio', 'psu', 'fans']) expect(document.querySelector(`[data-card="llama-${x}"]`).hidden, x).toBe(true);
  });

  test('a reported UPS opens the power hardware row and shows only that panel', async () => {
    load();
    const r = defaults();
    r.metrics = (id) => Object.assign(defaults().metrics(id), { ups: { percent: 97, state: 'charging' } });
    stubFetch(r);
    await window.hpEnter('llama');
    await settle();
    const row = document.querySelector('#hp-llama [data-row="llama-hw"]');
    expect(row.classList.contains('folded')).toBe(false);
    expect(row.querySelector('.hp-row-note').hidden).toBe(true);
    expect(document.querySelector('[data-card="llama-ups"]').hidden).toBe(false);
    expect(document.querySelector('[data-card="llama-psu"]').hidden).toBe(true);
    expect(document.getElementById('hp-b-llama-ups').textContent).toContain('97%');
  });

  test('an offline host reads "Host offline" with dashes for its values', async () => {
    load();
    const r = defaults();
    r.system = (id) => ({ agent_id: id, hostname: HOST[id], online: false, age_s: 900, sample: SYS(LEVEL[id]) });
    stubFetch(r);
    await window.hpEnter('host');
    await settle();
    const tiles = document.getElementById('hp-b-host-tiles').textContent;
    expect(tiles).toContain('Host offline');
    expect(tiles).not.toContain('30%');
    expect(document.getElementById('hp-b-host-mem-details').textContent).toContain('—');
    // History still draws for an offline host.
    const ts = document.querySelector('#hp-b-host-cpu-mem .ts[data-kit-ts]');
    expect(window.kitTsData(ts).series[0].values.filter(v => v != null).length).toBeGreaterThan(0);
  });

  test('a critical CPU rule for the picked host draws its line on CPU and memory', async () => {
    load();
    stubFetch(defaults());
    window._alarmRules = [{ enabled: true, name: 'CPU hot', metric_source: 'system', metric_name: 'cpu_total',
      rule_type: 'threshold_above', severity: 'critical', source_host: 'llm-a', config: { threshold: { upper: 90 } } },
    { enabled: true, name: 'Other host', metric_source: 'system', metric_name: 'cpu_total',
      rule_type: 'threshold_above', severity: 'critical', source_host: 'llm-b.local', config: { threshold: { upper: 50 } } }];
    await window.hpEnter('llama');
    await settle();
    const lines = document.querySelectorAll('#hp-b-llama-cpu-mem line.th');
    expect(lines.length).toBe(1);
    expect(document.querySelector('#hp-b-llama-cpu-mem .th-l').textContent).toBe('');
    expect(document.querySelectorAll('#hp-b-llama-net line.th').length).toBe(0);
  });

  test('the live poll appends samples; leaving stops it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const calls = stubFetch(defaults());
    window.hpEnter('llama');
    await vi.advanceTimersByTimeAsync(0);
    const live = () => calls.filter(u => u.startsWith('/api/metrics?')).length;
    expect(live()).toBe(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(live()).toBe(2);
    await vi.advanceTimersByTimeAsync(5000);
    expect(live()).toBe(3);
    expect(historyCalls(calls)).toHaveLength(1);
    window.hpLeave();
    const left = calls.length;
    await vi.advanceTimersByTimeAsync(60000);
    expect(calls.length).toBe(left);
  });

  test('a new range refetches the history on render', async () => {
    load();
    const calls = stubFetch(defaults());
    await window.hpEnter('lms');
    await settle();
    window.ahState.range = 6;
    window.hpRender();
    await settle();
    expect(historyCalls(calls)).toEqual([`/api/history?agent=${ID_A}&since_minutes=1440&max_rows=864`,
      `/api/history?agent=${ID_A}&since_minutes=360&max_rows=1080`]);
    expect(window.kitTsData(document.querySelector('#hp-b-lms-cpu-mem .ts[data-kit-ts]')).labels).toHaveLength(360);
  });

  test('_homeCardEl finds a host page panel', async () => {
    load();
    stubFetch(defaults());
    await window.hpEnter('llama');
    evalGlobal(fnSrc(foundation, '_homeCardEl') + '\nwindow._homeCardEl = _homeCardEl;');
    const el = window._homeCardEl('llama-gpu-busy');
    expect(el).not.toBeNull();
    expect(el.closest('#hp-llama')).not.toBeNull();
    expect(el.matches('section.panel')).toBe(true);
  });

  test('a panel pinned to Overall is built and filled while Overall shows', async () => {
    load();
    stubFetch(defaults());
    window.layout.overallBorrowed = ['vllm-cpu-mem', 'services'];
    window._activeTab = 'overall';
    window.adoptPinnedCards = vi.fn();
    expect(window.hpPagePanelIds('vllm')).toContain('vllm-cpu-mem');
    await window.hpPaintPinned();
    await settle();
    expect(document.querySelector('#hp-vllm [data-card="vllm-cpu-mem"]')).not.toBeNull();
    expect(document.getElementById('hp-llama').children.length).toBe(0);
    expect(window.adoptPinnedCards).toHaveBeenCalledTimes(1);
    expect(document.querySelector('#hp-b-vllm-cpu-mem .ts[data-kit-ts]')).not.toBeNull();
    delete window.adoptPinnedCards;
  });

  test('All hosts: a host with no LLM server opens the host page with that host picked', async () => {
    load();
    const r = defaults();
    r.hosts = () => ({ hosts: [agent(ID_A, { providers: ['llama'] }), agent(ID_C, { providers: [] })] });
    const calls = stubFetch(r);
    window.mcShellStore = makeStore();
    evalGlobal(allhosts);
    loadSwitchSubTab(boot);
    window._activeTab = 'dashboard';
    window.switchSubTab('dashboard', 'all');
    await settle();
    const btn = document.querySelector('#ah-b-now button[data-ah-host]');
    expect(btn.textContent).toBe('No LLM server · open host');
    btn.click();
    await settle();
    expect(window.layout._selectedAgents.system).toBe(ID_C);
    expect(document.getElementById('dash-host').classList.contains('active')).toBe(true);
    expect(document.getElementById('dash-all').classList.contains('active')).toBe(false);
    expect(document.querySelector('#hp-host select.hp-pick').value).toBe(ID_C);
    expect(calls).toContain(`/api/agents/${ID_C}/system`);
    expect(calls).toContain(`/api/history?agent=${ID_C}&since_minutes=1440&max_rows=864`);
    window.switchSubTab('dashboard', 'energy');
    delete window._activeTab;
    delete window._subTabState;
    if (typeof window.ahLeave === 'function') window.ahLeave();
  });

  test('hpRefresh after a hidden stretch fetches the history gap and draws it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const r = defaults();
    // The gap answer: one row a minute over the last 20 minutes at CPU 99.
    r.history = (id, url) => (url.includes('max_rows=') ? rowsFor(LEVEL[id], Date.now())
      : rowsFor(99, Date.now()).slice(-20));
    const calls = stubFetch(r);
    window.hpEnter('llama');
    await vi.advanceTimersByTimeAsync(0);
    expect(cpuValues()).not.toContain(99);
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    const before = calls.length;
    await vi.advanceTimersByTimeAsync(20 * 60000);
    expect(calls.length).toBe(before);
    hidden.mockReturnValue(false);
    await window.hpRefresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(historyCalls(calls).slice(1)).toEqual([`/api/history?agent=${ID_A}&since_minutes=21`]);
    expect(cpuValues()).toContain(99);
    window.hpLeave();
    const left = calls.length;
    await window.hpRefresh();
    expect(calls.length).toBe(left);
  });

  test('resume runs hpRefresh on a host page and on Overall', () => {
    evalGlobal(fnSrc(boot, '_rebackfillActiveView') + '\nwindow._rebackfillActiveView = _rebackfillActiveView;');
    const was = window.hpRefresh;
    window.hpRefresh = vi.fn(() => Promise.resolve());
    window._activeTab = 'dashboard';
    window._subTabState = { dashboard: 'vllm' };
    window._rebackfillActiveView();
    expect(window.hpRefresh).toHaveBeenCalledTimes(1);
    window._activeTab = 'overall';
    window.ovLoad = () => Promise.resolve();
    window._ovBackfillPinnedProviders = () => {};
    window._rebackfillActiveView();
    expect(window.hpRefresh).toHaveBeenCalledTimes(2);
    window.hpRefresh = was;
    delete window._activeTab; delete window._subTabState; delete window.ovLoad; delete window._ovBackfillPinnedProviders;
  });

  test('a failed model list is asked again on the next poll; a good one waits a minute', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const r = defaults();
    let fails = 1;
    r.models = () => (fails-- > 0 ? FAIL : { data: [] });
    const calls = stubFetch(r);
    const models = () => calls.filter(u => u.startsWith('/api/llm/models')).length;
    window.hpEnter('llama');
    await vi.advanceTimersByTimeAsync(0);
    expect(models()).toBe(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(models()).toBe(2);
    await vi.advanceTimersByTimeAsync(5000 * 6);
    expect(models()).toBe(2);
    await vi.advanceTimersByTimeAsync(5000 * 6);
    expect(models()).toBe(3);
  });

  test('nothing hides for a missing GPU or power hardware before a live sample arrives', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const r = defaults();
    // The first live fetch fails, so the history paints with no live sample; the next poll brings one.
    let fails = 1;
    r.vllm = (id) => (fails-- > 0 ? FAIL : defaults().vllm(id));
    stubFetch(r);
    window.hpEnter('vllm');
    await vi.advanceTimersByTimeAsync(0);
    const gpuRow = document.querySelector('#hp-vllm [data-row="vllm-gpu"]');
    expect(document.querySelector('#hp-b-vllm-cpu-mem .ts[data-kit-ts]')).not.toBeNull();
    expect(gpuRow.hidden).toBe(false);
    expect(document.querySelector('[data-card="vllm-ups"]').hidden).toBe(false);
    expect(document.querySelector('[data-card="vllm-mac-power"]').hidden).toBe(false);
    expect(document.querySelector('[data-card="vllm-gpu-busy"]').hidden).toBe(false);
    await vi.advanceTimersByTimeAsync(5000);
    expect(gpuRow.hidden).toBe(true);
    expect(document.querySelector('[data-card="vllm-ups"]').hidden).toBe(true);
  });

  test('unpinning the last panel of a page while Overall stays open stops its poll', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const calls = stubFetch(defaults());
    window.layout.overallBorrowed = ['vllm-cpu-mem'];
    window._activeTab = 'overall';
    window.adoptPinnedCards = vi.fn();
    window.hpPaintPinned();
    await vi.advanceTimersByTimeAsync(0);
    const live = () => calls.filter(u => u.startsWith('/api/vllm/metrics?')).length;
    const seeded = live();
    await vi.advanceTimersByTimeAsync(5000);
    expect(live()).toBe(seeded + 1);
    window.layout.overallBorrowed = [];
    await vi.advanceTimersByTimeAsync(5000);
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(60000);
    expect(live()).toBe(seeded + 1);
    delete window.adoptPinnedCards;
    delete window._activeTab;
  });

  test('reported power hardware restores the fold saved by the kit for that row', async () => {
    load();
    const r = defaults();
    r.metrics = (id) => Object.assign(defaults().metrics(id), { ups: { percent: 97, state: 'charging' } });
    stubFetch(r);
    // A fold click saves the fold through the kit store, as the shell's store does.
    window.kitConfigure({ store: window.mcShellStore });
    await window.hpEnter('llama');
    await settle();
    const row = () => document.querySelector('#hp-llama [data-row="llama-hw"]');
    expect(row().classList.contains('folded')).toBe(false);
    window.kitFoldClick(document, row().querySelector('button[data-row-fold]'));
    expect(JSON.parse(window.mcShellStore.getItem(window.kitKey('rows')))).toEqual({ 'host-llama': { 'llama-hw': 1 } });
    // A fresh page with that store: the reported UPS keeps the saved fold, with no "Nothing reported".
    const store = window.mcShellStore;
    load();
    window.mcShellStore = store;
    stubFetch(r);
    await window.hpEnter('llama');
    await settle();
    window.kitConfigure({ store: null });
    expect(row().classList.contains('folded')).toBe(true);
    expect(row().querySelector('.hp-row-note').hidden).toBe(true);
  });

  test('pinning a panel while Overall is open starts its page poll at once; unpinning stops it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const calls = stubFetch(defaults());
    document.body.insertAdjacentHTML('beforeend', '<div id="overallTab"><div class="grid" id="overallGrid"></div></div>');
    evalGlobal(['window._ovAdopted = new Set();', fnSrc(foundation, '_homeCardEl'), fnSrc(foundation, 'adoptPinnedCards'),
      fnSrc(foundation, '_returnOneAdopted'), fnSrc(foundation, 'returnPinnedCards'),
      fnSrc(foundation, 'addBorrowedCard'), fnSrc(foundation, 'removeBorrowedCard'),
      'window._homeCardEl = _homeCardEl; window.adoptPinnedCards = adoptPinnedCards; window._returnOneAdopted = _returnOneAdopted;',
      'window.addBorrowedCard = addBorrowedCard; window.removeBorrowedCard = removeBorrowedCard;'].join('\n'));
    window._ovHomeMarks = {};
    window._ensureSizeBtn = () => {};
    window._applyCardSize = () => {};
    window._defaultCardSize = () => 'auto';
    window._sizeMapFor = () => ({});
    window._resizeChartsIn = () => {};
    window._activeTab = 'overall';
    window.layout.overallBorrowed = [];
    const live = () => calls.filter(u => u.startsWith('/api/metrics?')).length;
    expect(vi.getTimerCount()).toBe(0);
    window.addBorrowedCard('llama-cpu-mem');
    await vi.advanceTimersByTimeAsync(0);
    expect(live()).toBe(1);
    expect(vi.getTimerCount()).toBe(1);
    const panel = document.querySelector('#overallGrid [data-card="ov-borrow-llama-cpu-mem"] section.panel[data-card="llama-cpu-mem"]');
    expect(panel).not.toBeNull();
    expect(panel.querySelector('.ts[data-kit-ts]')).not.toBeNull();
    await vi.advanceTimersByTimeAsync(5000);
    expect(live()).toBe(2);
    window.removeBorrowedCard('llama-cpu-mem');
    expect(document.querySelector('#hp-llama [data-card="llama-cpu-mem"]')).not.toBeNull();
    await vi.advanceTimersByTimeAsync(5000);
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(60000);
    expect(live()).toBe(2);
    ['_ensureSizeBtn', '_applyCardSize', '_defaultCardSize', '_sizeMapFor', '_resizeChartsIn', '_activeTab', 'adoptPinnedCards',
      'addBorrowedCard', 'removeBorrowedCard', '_returnOneAdopted', '_homeCardEl'].forEach(k => { delete window[k]; });
  });
});
