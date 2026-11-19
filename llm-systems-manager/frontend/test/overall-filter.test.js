// #1432 phase 4b: the Overall toolbar provider filter and time range, the per-filter hourly energy (R4),
// the history seed states, the saved filter and range, and the one-time legacy band key mapping (R5).
import { describe, test, expect, afterEach, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import OV from '../js/lib/overall-view.js';
import { srcFile, evalGlobal, flush } from './helpers/harness.js';

const overall = srcFile('js/overall.js');
const indexSrc = srcFile('index.html');
const tabHtml = new JSDOM(indexSrc).window.document.getElementById('overallTab').outerHTML;
evalGlobal(srcFile('kit/kit.js'));
window.OV = OV;

const ID_LLAMA = '056ba13c-689e-411e-b865-74a83b9086cd';
const ID_LLAMA2 = '601964c9-eaeb-4c04-af3e-74e1b5808710';
const ID_LMS = '42cf47d9-2849-47c3-99b9-bca2a9955fd1';
const ID_LMS2 = 'cc8ae083-738f-40b7-9b5b-18fffab4df98';
const ID_VLLM = 'dd90b4e9-d426-41e1-8951-ca55198146a2';

// Dev payloads of 2026-11-16, trimmed to the fields read (as in overall-kit.test.js).
const LLAMA_AGG = {
  agent_count_online: 2, agent_count_total: 2,
  agents: [
    { age_s: 2.2, agent_id: ID_LLAMA2, model: null, online: true, power_watts: null, state: 'awake' },
    { age_s: 3.1, agent_id: ID_LLAMA, model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M', online: true, power_watts: 11.0, state: 'sleeping' },
  ],
  gpu: { max_temp_c: 47.0, thermal_crit_count: 0, total_power_watts: 11.0 },
  throughput: { total_pps: 0.0, total_tps: 0.0 },
};
const LMS_AGG = {
  agent_count_online: 2, agent_count_total: 2, loaded_model_count_total: 1, server_on_count: 1,
  agents: [
    { age_s: 0.8, agent_id: ID_LMS, loaded_model_count: 1, loaded_models: ['qwen3.5-9b@q6_k'], online: true, power_watts: 0.502, server_on: true },
    { age_s: 3.1, agent_id: ID_LMS2, loaded_model_count: 0, loaded_models: [], online: true, power_watts: null, server_on: false },
  ],
  gpu: { max_temp_c: 0.0, thermal_crit_count: 0, total_power_watts: 0.502 },
  throughput: { total_pps: 0.0, total_tps: 0.0 },
};
const VLLM_AGG = {
  agent_count_online: 1, agent_count_total: 1,
  agents: [{ age_s: 2.8, agent_id: ID_VLLM, model: null, online: true, power_watts: null, requests_running: null, server_on: false }],
  gpu: { max_temp_c: 0.0, thermal_crit_count: 0, total_power_watts: 0.0 },
  requests_running_total: 0, requests_waiting_total: 0, server_on_count: 0,
  throughput: { total_pps: 0.0, total_tps: 0.0 }, total_gpu_power_watts: 0.0,
};
const BY_PROVIDER = {
  llama: [{ agent_id: ID_LLAMA, hostname: 'llm-systems-agent-llama' }, { agent_id: ID_LLAMA2, hostname: 'llm-systems-agent-llama2' }],
  lms: [{ agent_id: ID_LMS, hostname: 'llm-systems-lmstudio.local' }, { agent_id: ID_LMS2, hostname: 'llm-systems-agent-lms' }],
  vllm: [{ agent_id: ID_VLLM, hostname: 'llm-systems-agent-vllm1' }],
};
const SUMMARY1 = {
  config: { cloud_price_label: 'Opus 5', price_kwh: 0.15 }, price_kwh: 0.15, savings_usd: 3.34, ok: true,
  hosts: [
    { active_kwh: 0.02, agent_id: ID_LLAMA, cloud_cost_usd: 0.71, cost_usd: 0.04, has_power: true,
      hostname: 'llm-systems-agent-llama', idle_kwh: 0.233, kwh: 0.253, power_source: 'gpu', tokens_gen: 10237 },
    { active_kwh: 0.021, agent_id: ID_LMS, cloud_cost_usd: 2.67, cost_usd: 0.01, has_power: true,
      hostname: 'llm-systems-lmstudio.local', idle_kwh: 0.015, kwh: 0.036, power_source: 'mac', tokens_gen: 55007 },
  ],
  models: [{ hosts: ['llm-systems-agent-llama'], kwh: 0.253, model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M' }],
  totals: { active_kwh: 0.041, cost_usd: 0.04, has_power: true, idle_kwh: 0.247, kwh: 0.288, tokens_gen: 65244 },
};
const END_TS = 1794812400;
const HOURLY = { end_ts: END_TS, rows: [
  { active_energy_wh: 2.35, energy_wh: 13.21, hour_ts: END_TS - 7200, tokens_gen: 633 },
  { active_energy_wh: 0.0, energy_wh: 4.7, hour_ts: END_TS - 3600, tokens_gen: 0 },
] };
// Per-host hourly payloads (shape of /api/energy/hourly?agent=); the 30 h old row falls in the day before.
const HOURLY_LMS = { end_ts: END_TS, rows: [
  { active_energy_wh: 0.5, energy_wh: 1.5, hour_ts: END_TS - 7200, tokens_gen: 100 },
  { active_energy_wh: 1.0, energy_wh: 2.0, hour_ts: END_TS - 3600, tokens_gen: 50 },
] };
const HOURLY_LMS2 = { end_ts: END_TS, rows: [
  { active_energy_wh: 0.0, energy_wh: 4.0, hour_ts: END_TS - 30 * 3600, tokens_gen: 300 },
  { active_energy_wh: 0.0, energy_wh: 3.0, hour_ts: END_TS - 3600, tokens_gen: 0 },
] };
const HIST = [
  { ts: '2026-11-15T13:02:00+00:00', gpu_power: 35.275, llama_pps: 616.522, llama_tps: 94.1566, lms_pps: 0.0, lms_tps: 0.0 },
];
const FORECAST = {
  ok: true, enabled: true, last_result: '10 found, 0 new', checks: [{ id: 'disk_fill', state: 'ok' }],
  findings: [{ check: 'disk_fill', detail: 'Disk use on / has grown steadily.', host: 'llm-systems-agent-llama',
    last_seen: 1794729608, severity: 'warning', status: 'open', title: 'Disk fill' }],
  cleared: [],
};
const ALERTS = [{ rule_name: 'GPU hot', severity: 'warning', message: 'GPU at 91 C', source_host: 'llm-systems-agent-llama',
  created_at: '2026-11-16T05:00:00Z' }];

const DEV = {
  '/api/fleet/llama/aggregate': LLAMA_AGG, '/api/fleet/lms/aggregate': LMS_AGG, '/api/fleet/vllm/aggregate': VLLM_AGG,
  '/api/llama-state': { agent_online: true, aggregate: 'sleeping', state: 'sleeping' }, '/api/lmstudio/metrics': {},
  '/api/llm/models': { data: [] },
  '/api/energy/summary?days=1': SUMMARY1, '/api/energy/summary?days=7': SUMMARY1,
  '/api/energy/hourly?hours=168': HOURLY, '/api/energy/hourly?hours=48': HOURLY,
  [`/api/energy/hourly?hours=168&agent=${ID_LMS}`]: HOURLY_LMS, [`/api/energy/hourly?hours=168&agent=${ID_LMS2}`]: HOURLY_LMS2,
  '/api/tower/state': { enabled: true }, '/api/tower/insights': { insights: [], ok: true }, '/api/history': HIST,
};

// Every URL fetched; each answers a copy of its exact routes entry, else of the first entry it starts with, else {}.
// A route set to null answers ok:false.
function stubFetch(routes) {
  const calls = [];
  window.fetch = vi.fn(async (url) => {
    calls.push(url);
    const key = url in routes ? url : Object.keys(routes).find(k => url.startsWith(k));
    if (key && routes[key] === null) return { ok: false, json: async () => ({}) };
    return { ok: true, json: async () => JSON.parse(JSON.stringify(key ? routes[key] : {})) };
  });
  return calls;
}

// A fresh #overallTab and a fresh overall.js with the dev globals and an in-memory shell store.
function load() {
  document.body.innerHTML = tabHtml;
  document.getElementById('overallTab').style.display = '';
  delete window._activeTab;
  delete window.ovState;
  const saved = {};
  window.mcShellStore = { saved, getItem: (k) => (k in saved ? saved[k] : null), setItem: (k, v) => { saved[k] = String(v); } };
  window.mcShellPaintOv = vi.fn();
  window._agentsByProvider = BY_PROVIDER;
  window._pollCfg = { agents: { llama_present: true, lms_present: true, vllm_present: true } };
  window._activeAlerts = ALERTS;
  window._latestMetric = undefined;
  window.forecastSnapshot = () => FORECAST;
  evalGlobal(overall);
}

const text = (id) => document.getElementById(id).textContent;
const chart = () => document.querySelector('#ov-b-tp .ts[data-kit-ts]');
const tpData = () => window.kitTsData(chart());
const en7 = () => window.kitTsData(document.querySelector('#ov-b-energy7 .ts[data-kit-ts]'));
const T0 = Date.parse('2026-11-16T06:00:30Z');

// Holds every fetch whose URL matches until the returned release() is called.
function holdFetch(match) {
  const ok = window.fetch;
  let release;
  const gate = new Promise(r => { release = r; });
  window.fetch = vi.fn(async (url) => (match(url) ? gate.then(() => ok(url)) : ok(url)));
  return () => release();
}
const kpi = (id) => document.getElementById('ov-b-' + id);

afterEach(() => { vi.restoreAllMocks(); });

describe('Overall provider filter (#1432)', () => {
  test('filter to a provider with no hosts', async () => {
    load();
    window._agentsByProvider = { ...BY_PROVIDER, vllm: [] };
    const calls = stubFetch({ ...DEV, '/api/fleet/vllm/aggregate': { ...VLLM_AGG, agents: [], agent_count_online: 0, agent_count_total: 0 } });
    await window.ovLoad();
    await window.fetchOverallMetrics();
    await flush();
    calls.length = 0;
    await window.ovSetFilter('vllm');
    await flush();
    expect(calls.filter(u => u.startsWith('/api/energy/hourly'))).toEqual([]);
    for (const id of ['k-agents', 'k-tok', 'k-peak', 'k-pow', 'k-en', 'k-save']) {
      expect(document.querySelector(`#ov-b-${id} .k-val`).textContent, id).toMatch(/^(0(\.0)?( |$)|–$)/);
    }
    expect(document.querySelector('#ov-b-k-agents .k-val').textContent).toBe('0 / 0');
    for (const id of ['energy7', 'energy-hosts', 'energy-split', 'power-now', 'hosts-tokens', 'forecast', 'alerts', 'hosts', 'srv-vllm']) {
      expect(document.querySelector(`#ov-b-${id} .empty, #ov-b-${id} .ov-empty`), id).toBeTruthy();
    }
    expect(text('ov-b-srv-vllm')).toBe('No vLLM hosts');
    expect(document.querySelector('[data-card="srv-vllm"]').hidden).toBe(false);
    expect(document.querySelector('[data-card="srv-llama"]').hidden).toBe(true);
    expect(document.querySelector('[data-card="srv-lms"]').hidden).toBe(true);
    const legend = [...document.querySelectorAll('#ov-b-tp [data-kit-series]')].map(b => b.getAttribute('data-kit-series'));
    expect(legend).toContain('ov-tp|vLLM');
    expect(legend.some(s => /llama\.cpp|LM Studio/.test(s))).toBe(false);
    expect(document.querySelector('[data-card="tp"] .p-note').textContent).toMatch(/^vLLM · last 24 h/);
    const tab = document.getElementById('overallTab').textContent;
    for (const bad of ['NaN', 'undefined', 'null', 'Infinity']) expect(tab, bad).not.toContain(bad);
  });

  test('filter narrows energy and hosts to that provider\'s agent ids', async () => {
    load();
    let now = T0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    const calls = stubFetch(DEV);
    await window.ovLoad();
    await window.fetchOverallMetrics();
    await flush();
    expect(text('ov-b-energy-hosts')).toContain('llm-systems-agent-llama');
    calls.length = 0;
    await window.ovSetFilter('lms');
    await flush();
    expect(window.mcShellStore.saved['mgr.ovProv']).toBe('lms');
    expect(window.mcShellPaintOv).toHaveBeenCalled();
    const hourly = calls.filter(u => u.startsWith('/api/energy/hourly'));
    expect(hourly.sort()).toEqual([`/api/energy/hourly?hours=168&agent=${ID_LMS}`, `/api/energy/hourly?hours=168&agent=${ID_LMS2}`].sort());
    // Energy use is the two LM Studio hosts summed per hour, then per 3-hour step: Idle then Generating Wh.
    const e = en7(), want = OV.energyPerHour([
      { hour_ts: END_TS - 30 * 3600, energy_wh: 4, active_energy_wh: 0 },
      { hour_ts: END_TS - 7200, energy_wh: 1.5, active_energy_wh: 0.5 },
      { hour_ts: END_TS - 3600, energy_wh: 5, active_energy_wh: 1 }], 3, { wrap: true });
    expect(e.labels).toEqual(want.labels);
    expect(e.series[0].values).toEqual(want.series[0].values);
    expect(e.series[1].values).toEqual(want.series[1].values);
    expect(want.series[0].values.reduce((x, y) => x + y, 0)).toBe(9);
    expect(text('ov-b-k-tok')).toContain('300 the day before');
    expect(text('ov-b-energy-hosts')).toContain('llm-systems-lmstudio');
    expect(text('ov-b-energy-hosts')).not.toContain('llm-systems-agent-llama');
    expect(text('ov-b-hosts')).toContain('llm-systems-agent-lms');
    expect(text('ov-b-hosts')).not.toContain('llm-systems-agent-llama');
    expect(text('ov-b-forecast')).toContain('No open findings');
    expect(text('ov-b-alerts')).toContain('No active alerts');
    expect(document.querySelector('[data-card="srv-lms"]').hidden).toBe(false);
    expect(document.querySelector('[data-card="srv-llama"]').hidden).toBe(true);

    // The filter's sums are cached for the page visit: back and forth within the minute fetches nothing.
    calls.length = 0;
    await window.ovSetFilter('all');
    await window.ovSetFilter('lms');
    expect(calls.filter(u => u.startsWith('/api/energy/hourly'))).toEqual([]);
    expect(en7().series[0].values.reduce((x, y) => x + y, 0)).toBe(9);
    // The once-a-minute refresh refetches the filter's hosts and drops the cache.
    now = T0 + 61000;
    await window.fetchOverallMetrics();
    await flush();
    expect(calls).toContain(`/api/energy/hourly?hours=168&agent=${ID_LMS}`);
    expect(calls).not.toContain('/api/energy/hourly?hours=168');
    calls.length = 0;
    await window.ovSetFilter('all');
    await flush();
    expect(calls).toContain('/api/energy/hourly?hours=168');
    expect(calls).toContain('/api/energy/hourly?hours=48');
    expect(text('ov-b-energy-hosts')).toContain('llm-systems-agent-llama');
  });
});

describe('Overall time range (#1432)', () => {
  test('range change reseeds and clears zoom', async () => {
    load();
    vi.spyOn(Date, 'now').mockImplementation(() => T0);
    const calls = stubFetch(DEV);
    await window.ovLoad();
    const d = tpData();
    expect(d.keep).toBe(96);
    d.z = { from: d.labels[20], to: d.labels[60] };
    window.kitRedrawTs(chart(), d, d.w);
    expect(tpData().z).toBeTruthy();
    calls.length = 0;
    await window.ovSetRange(6);
    expect(window.mcShellStore.saved['mgr.ovRange']).toBe('6');
    expect(calls).toContain('/api/history?fleet=all&since_minutes=360&max_rows=360');
    const after = tpData();
    expect(after.keep).toBe(72);
    expect(after.labels).toHaveLength(72);
    expect(after.z).toBeFalsy();
    expect(document.querySelector('#ov-b-tp [data-kit-zoom-reset]')).toBeNull();
    expect(document.querySelector('[data-card="tp"] .p-note').textContent).toMatch(/^All providers · last 6 h/);
    calls.length = 0;
    await window.ovSetRange(6);
    expect(calls).toEqual([]);
    await window.ovSetRange(7);
    expect(window.ovState.range).toBe(6);
  });
});

describe('Overall history seed (#1432)', () => {
  // A failed fetch shows Loading…; a live tick may then draw the one-point chart (live data exists), and a later
  // successful load clears the failure and draws the full chart.
  test('a failed history fetch stays on Loading until a live tick or a later load', async () => {
    load();
    let now = T0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    const routes = { ...DEV, '/api/history': null };
    stubFetch(routes);
    await window.ovLoad();
    expect(chart()).toBeNull();
    expect(text('ov-b-tp')).toBe('Loading…');
    await window.fetchOverallMetrics();
    expect(tpData().labels).toHaveLength(1);
    routes['/api/history'] = HIST;
    now = T0 + 5000;
    await window.ovLoad();
    expect(tpData().labels).toHaveLength(96);
  });

  test('no one-point Throughput before the first history lands', async () => {
    load();
    vi.spyOn(Date, 'now').mockImplementation(() => T0);
    const calls = stubFetch(DEV);
    const ok = window.fetch;
    let release;
    const held = new Promise(r => { release = r; });
    window.fetch = vi.fn(async (url) => (url.startsWith('/api/history') ? held.then(() => ok(url)) : ok(url)));
    // A boot tick before the tab entry, then a tick while the history fetch is in flight.
    await window.fetchOverallMetrics();
    expect(chart()).toBeNull();
    expect(text('ov-b-tp')).toBe('Loading…');
    const loading = window.ovLoad();
    await window.fetchOverallMetrics();
    expect(chart()).toBeNull();
    expect(text('ov-b-tp')).toBe('Loading…');
    release();
    await loading;
    expect(calls.some(u => u.startsWith('/api/history'))).toBe(true);
    expect(tpData().labels).toHaveLength(96);
  });
});

// The full page: kit, settingsdrawer, foundation, boot, overall-view, overall and shell on the real body, fetch stubbed.
const bodyDoc = new JSDOM(indexSrc).window.document;
bodyDoc.querySelectorAll('body script').forEach(s => s.remove());
const bodyHtml = bodyDoc.body.innerHTML;
const PAGE = ['kit/kit.js', 'js/lib/settingsdrawer.js', 'js/foundation.js', 'js/boot.js', 'js/lib/overall-view.js',
  'js/overall.js', 'js/shell.js'].map(srcFile);
const STUBS = ['loadBenchmarkData', 'renderModelCards', 'initSortable', 'syncInterval', 'loadHistory',
  'loadManagerPerfHistory', 'checkConfig', 'pollServerState', 'fetchMetrics', 'startFetching',
  'fetchLMStudioMetrics', 'fetchVllmMetrics', 'fetchServicesAndInflux', 'fetchManagerAgentsCard', 'fetchManagerStreamsCard',
  'refreshTabIndicators', 'refreshAlarmRules', 'refreshLLMTab', 'loadHFTrending', 'startPerfRefresh',
  '_initLLMSections', 'startLogStream', 'adminLoadAgents', 'adminLoadHealth', 'adminAuthLoad', 'adminStartAutoRefresh',
  'adminStopAutoRefresh', 'stopLogStream', 'stopPerfRefresh', 'stopLmsLogRefresh', 'fetchOpenclawAnalytics',
  'initEnergyTab', 'forecastLoad', '_initLMSSections'];
const stubSrc = STUBS.map(n => `window.${n} = function () { return Promise.resolve(); };`).join('\n')
  + '\nwindow.fetchInterval = 5000; window._logPanelOpen = false; window._lmsLogOpen = false;';

const _windows = [];
afterEach(() => { while (_windows.length) _windows.pop().close(); });

// Waits up to `max` macrotask ticks for `done()` to hold.
async function waitFor(w, done, max = 50) {
  for (let i = 0; i < max && !done(); i++) await new Promise(r => w.setTimeout(r, 0));
}

// Boots the page on a saved layout; w.__posts holds every layout POST from the start, w.__urls every GET.
async function boot(saved) {
  const dom = new JSDOM(`<!doctype html><html><head></head><body>${bodyHtml}</body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/', pretendToBeVisual: true });
  const w = dom.window;
  _windows.push(w);
  w.__posts = [];
  w.__urls = [];
  w.fetch = async (url, opts) => {
    if (opts && opts.method === 'POST') {
      if (url === '/api/layout') w.__posts.push(JSON.parse(opts.body));
      return { ok: true, json: async () => ({}) };
    }
    w.__urls.push(url);
    if (url === '/api/layout') return { ok: true, json: async () => JSON.parse(JSON.stringify(saved)) };
    if (url === '/api/me') return { ok: true, json: async () => ({ role: 'admin', admin_access: true, authenticated: false }) };
    return { ok: true, json: async () => ({}) };
  };
  const inject = (code) => { const s = w.document.createElement('script'); s.textContent = code; w.document.head.appendChild(s); };
  inject(stubSrc);
  PAGE.forEach(inject);
  await w.mcLayoutReady;
  await waitFor(w, () => w.document.getElementById('overallTab').style.display === '');
  expect(w.document.getElementById('overallTab').style.display, 'boot reached switchTab').toBe('');
  await waitFor(w, () => false, 5);
  return w;
}
const pressed = (w, sel) => [...w.document.querySelectorAll(sel)].filter(b => b.getAttribute('aria-pressed') === 'true')
  .map(b => b.textContent);

describe('Overall toolbar on the page (#1432)', () => {
  test('filter and range persist through the store', async () => {
    const w = await boot({ theme: 'dark' });
    const d = w.document;
    expect(d.querySelector('#toolbar .tb-ov').hidden).toBe(false);
    expect(pressed(w, '#ovProv button')).toEqual(['All providers']);
    expect(pressed(w, '#ovRange button')).toEqual(['24 h']);
    w.__posts.length = 0;
    d.querySelector('#ovProv [data-ov-prov="lms"]').click();
    d.querySelector('#ovRange [data-ov-range="6"]').click();
    await waitFor(w, () => w.__posts.length >= 2);
    await waitFor(w, () => false, 5);
    expect(w.eval('layout.kit')['mgr.ovProv']).toBe('lms');
    expect(w.eval('layout.kit')['mgr.ovRange']).toBe('6');
    const last = w.__posts[w.__posts.length - 1];
    expect(last.kit['mgr.ovProv']).toBe('lms');
    expect(last.kit['mgr.ovRange']).toBe('6');
    expect(pressed(w, '#ovProv button')).toEqual(['LM Studio']);
    expect(pressed(w, '#ovRange button')).toEqual(['6 h']);
    expect(w.__urls).toContain('/api/history?fleet=all&since_minutes=360&max_rows=360');

    const r = await boot({ theme: 'dark', kit: { 'mgr.ovProv': 'lms', 'mgr.ovRange': '6' } });
    expect({ ...r.ovState }).toEqual({ prov: 'lms', range: 6 });
    expect(pressed(r, '#ovProv button')).toEqual(['LM Studio']);
    expect(pressed(r, '#ovRange button')).toEqual(['6 h']);
    expect(r.__urls).toContain('/api/history?fleet=all&since_minutes=360&max_rows=360');
    expect(r.__urls.some(u => u.includes('since_minutes=1440'))).toBe(false);
    expect(r.document.querySelector('[data-card="tp"] .p-note').textContent).toMatch(/^LM Studio · last 6 h/);
    expect(r.__posts).toEqual([]);

    const junk = await boot({ theme: 'dark', kit: { 'mgr.ovProv': 'bogus', 'mgr.ovRange': '7' } });
    expect({ ...junk.ovState }).toEqual({ prov: 'all', range: 24 });
    expect(pressed(junk, '#ovRange button')).toEqual(['24 h']);
  });

  test('legacy band keys map once', async () => {
    const w = await boot({ theme: 'dark', overallBandHidden: ['hero', 'agents'], overallBandOrder: ['agents', 'hero', 'tiles'] });
    expect(w.__posts).toHaveLength(1);
    const posted = w.__posts[0];
    expect(posted.overallRowsHidden).toEqual(['throughput']);
    expect('overallBandHidden' in posted).toBe(false);
    expect('overallBandOrder' in posted).toBe(false);
    const lay = w.eval('layout');
    expect(lay.overallRowsHidden).toEqual(['throughput']);
    expect('overallBandHidden' in lay).toBe(false);
    expect(w.document.querySelector('#overallTab section.row[data-row="throughput"]').hidden).toBe(true);
    expect(w.document.querySelector('#overallTab section.row[data-row="servers"]').hidden).toBe(false);

    const again = await boot(posted);
    expect(again.__posts).toEqual([]);
    expect(again.eval('layout').overallRowsHidden).toEqual(['throughput']);
    expect(again.document.querySelector('#overallTab section.row[data-row="throughput"]').hidden).toBe(true);
  });
});

describe('Overall filter fix round 1 (#1432)', () => {
  test('the prompt series is named after the filter and shows in its own band on top', async () => {
    load();
    vi.spyOn(Date, 'now').mockImplementation(() => T0);
    stubFetch({ ...DEV, '/api/history': [{ ts: '2026-11-16T05:00:00+00:00', gpu_power: 20, llama_tps: 0, llama_pps: 0, lms_tps: 10, lms_pps: 500 }] });
    await window.ovLoad();
    const series = () => [...document.querySelectorAll('#ov-b-tp [data-kit-series]')]
      .map(b => [b.getAttribute('data-kit-series'), b.getAttribute('aria-pressed')]);
    expect(series()[0]).toEqual(['ov-tp|Prompt (all)', 'true']);
    expect(tpData()).toMatchObject({ bars: 'peak', split: true });
    expect(text('ov-b-k-peak')).toMatch(/^10\.0 t\/s/);
    await window.ovSetFilter('lms');
    expect(series()).toEqual([['ov-tp|Prompt (LM Studio)', 'true'], ['ov-tp|LM Studio', 'true']]);
    expect(tpData().series.map(x => [x.name, x.band])).toEqual([['Prompt (LM Studio)', undefined], ['LM Studio', 'Generation']]);
    expect(text('ov-b-k-peak')).toMatch(/^10\.0 t\/s/);
    await window.ovSetFilter('vllm');
    expect(series()[0]).toEqual(['ov-tp|Prompt (vLLM)', 'true']);
  });

  test('a slow run for an old filter does not delay the new filter\'s energy after a hidden filter change', async () => {
    load();
    let now = T0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    const calls = stubFetch(DEV);
    await window.ovLoad();
    now = T0 + 61000;
    const release = holdFetch(u => u.startsWith('/api/energy/hourly'));
    await window.fetchOverallMetrics();
    window._activeTab = 'dashboard';
    await window.ovSetFilter('lms');
    release();
    await flush();
    await flush();
    now = T0 + 66000;
    window._activeTab = 'overall';
    calls.length = 0;
    await window.ovLoad();
    await flush();
    expect(calls).toContain(`/api/energy/hourly?hours=168&agent=${ID_LMS}`);
    expect(calls).toContain(`/api/energy/hourly?hours=168&agent=${ID_LMS2}`);
    expect(en7().series[0].values.reduce((x, y) => x + y, 0)).toBe(9);
    delete window._activeTab;
  });

  test('energy shows Loading while the new filter\'s hourly energy is in flight, never the old numbers', async () => {
    load();
    const PREV = { ...HOURLY, rows: [{ active_energy_wh: 0, energy_wh: 9, hour_ts: END_TS - 30 * 3600, tokens_gen: 777 }, ...HOURLY.rows] };
    stubFetch({ ...DEV, '/api/energy/hourly?hours=48': PREV });
    await window.ovLoad();
    await window.fetchOverallMetrics();
    await flush();
    expect(text('ov-b-k-tok')).toContain('777 the day before');
    const release = holdFetch(u => u.includes('&agent='));
    const p = window.ovSetFilter('lms');
    expect(text('ov-b-energy7')).toBe('Loading…');
    for (const id of ['k-tok', 'k-en']) {
      expect(kpi(id).querySelector('.k-delta').textContent, id).toBe('Loading…');
      expect(kpi(id).querySelector('.k-spark'), id).toBeNull();
    }
    expect(text('ov-b-k-tok')).not.toContain('777');
    await window.fetchOverallMetrics();
    expect(text('ov-b-energy7')).toBe('Loading…');
    expect(kpi('k-tok').querySelector('.k-delta').textContent).toBe('Loading…');
    release();
    await p;
    expect(en7().series[0].values.reduce((x, y) => x + y, 0)).toBe(9);
    expect(text('ov-b-k-tok')).toContain('300 the day before');
    expect(document.getElementById('overallTab').textContent).not.toContain('Loading…');
  });

  test('the Loading… change slot shows on Tokens and Energy tiles even with an empty sub line', async () => {
    load();
    stubFetch(DEV);
    await window.ovLoad();
    await window.fetchOverallMetrics();
    await flush();
    const real = OV.fleetKpis;
    vi.spyOn(OV, 'fleetKpis').mockImplementation((input) => real(input).map(k => ({ ...k, sub: '' })));
    const release = holdFetch(u => u.includes('&agent='));
    const p = window.ovSetFilter('lms');
    for (const id of ['k-tok', 'k-en']) {
      expect(kpi(id).querySelector('.k-sub').textContent, id).toBe('');
      expect(kpi(id).querySelector('.k-row .k-delta.flat').textContent, id).toBe('Loading…');
    }
    expect(kpi('k-pow').querySelector('.k-delta')).toBeNull();
    release();
    await p;
    expect(text('ov-b-k-tok')).not.toContain('Loading…');
  });

  test('a stale hourly response landing after the filter moved on does not paint', async () => {
    load();
    const LLAMA_H = { end_ts: END_TS, rows: [{ active_energy_wh: 7, energy_wh: 70, hour_ts: END_TS - 3600, tokens_gen: 5 }] };
    stubFetch({ ...DEV, [`/api/energy/hourly?hours=168&agent=${ID_LLAMA}`]: LLAMA_H,
      [`/api/energy/hourly?hours=168&agent=${ID_LLAMA2}`]: LLAMA_H });
    await window.ovLoad();
    await flush();
    const before = en7();
    expect(before.series[1].values.reduce((x, y) => x + y, 0)).toBe(2.35);
    const release = holdFetch(u => u.includes('&agent='));
    const p = window.ovSetFilter('llama');
    expect(text('ov-b-energy7')).toBe('Loading…');
    await window.ovSetFilter('all');
    expect(en7().series[1].values).toEqual(before.series[1].values);
    release();
    await p;
    await flush();
    expect(en7().series[1].values).toEqual(before.series[1].values);
    expect(en7().labels).toEqual(before.labels);
    expect(document.getElementById('overallTab').textContent).not.toContain('Loading…');
  });

  test('summing hosts with disjoint hours keeps every hour and the latest end', async () => {
    load();
    const A = { end_ts: END_TS - 7200, rows: [{ active_energy_wh: 0, energy_wh: 2, hour_ts: END_TS - 25 * 3600, tokens_gen: 300 }] };
    const B = { end_ts: END_TS, rows: [{ active_energy_wh: 1, energy_wh: 1, hour_ts: END_TS - 3600, tokens_gen: 10 }] };
    stubFetch({ ...DEV, [`/api/energy/hourly?hours=168&agent=${ID_LMS}`]: A, [`/api/energy/hourly?hours=168&agent=${ID_LMS2}`]: B });
    await window.ovLoad();
    await window.ovSetFilter('lms');
    const e = en7();
    expect(e.labels).toHaveLength(2);
    expect(e.series[0].values).toEqual([2, 0]);
    expect(e.series[1].values).toEqual([0, 1]);
    // With end_ts = the later end, the 25 h old row falls in the day before.
    expect(text('ov-b-k-tok')).toContain('300 the day before');
  });
});
