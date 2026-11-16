// #1432 phase 4b: the Overall page on the kit — rows, renderer, live updates and the pinned row.
// Runs kit.js, js/lib/overall-view.js and js/overall.js on the real #overallTab markup with fetch stubbed.
import { describe, test, expect, afterEach, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import OV from '../js/lib/overall-view.js';
import LMPeaks from '../js/lib/peaks.js';
import { srcFile, fnSrc, evalGlobal, flush } from './helpers/harness.js';

const overall = srcFile('js/overall.js');
const foundation = srcFile('js/foundation.js');
const tabHtml = new JSDOM(srcFile('index.html')).window.document.getElementById('overallTab').outerHTML;
evalGlobal(srcFile('kit/kit.js'));
window.OV = OV;
window.LMPeaks = LMPeaks;

const ID_LLAMA = '056ba13c-689e-411e-b865-74a83b9086cd';
const ID_LLAMA2 = '601964c9-eaeb-4c04-af3e-74e1b5808710';
const ID_LMS = '42cf47d9-2849-47c3-99b9-bca2a9955fd1';
const ID_LMS2 = 'cc8ae083-738f-40b7-9b5b-18fffab4df98';
const ID_VLLM = 'dd90b4e9-d426-41e1-8951-ca55198146a2';

// Dev payloads of 2026-11-16, trimmed to the fields read (as in overall-view-4b.test.js).
const LLAMA_AGG = {
  active_model_count: 0, agent_count_online: 2, agent_count_total: 2, awake_agent_count: 1,
  agents: [
    { age_s: 2.2, agent_id: ID_LLAMA2, model: null, online: true, power_watts: null, state: 'awake' },
    { age_s: 3.1, agent_id: ID_LLAMA, model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M', online: true, power_watts: 11.0, state: 'sleeping' },
  ],
  gpu: { max_temp_c: 47.0, thermal_crit_count: 0, total_power_watts: 11.0 },
  throughput: { total_pps: 0.0, total_tps: 0.0 },
};
const LMS_AGG = {
  agent_count_online: 2, agent_count_total: 2, loaded_model_count_total: 1, server_on_count: 1, busy_process_count_total: 0,
  agents: [
    { age_s: 0.8, agent_id: ID_LMS, busy_process_count: 0, loaded_model_count: 1, loaded_models: ['qwen3.5-9b@q6_k'],
      online: true, power_watts: 0.502, server_on: true },
    { age_s: 3.1, agent_id: ID_LMS2, busy_process_count: 0, loaded_model_count: 0, loaded_models: [], online: true,
      power_watts: null, ps_error: 'LMS_CMD not found', server_on: false },
  ],
  gpu: { max_temp_c: 0.0, thermal_crit_count: 0, total_power_watts: 0.502 },
  throughput: { total_pps: 0.0, total_tps: 0.0 },
};
const VLLM_AGG = {
  active_model_count: 0, active_models: [], agent_count_online: 1, agent_count_total: 1,
  agents: [{ age_s: 2.8, agent_id: ID_VLLM, model: null, online: true, power_watts: null, requests_running: null, server_on: false }],
  gpu: { max_temp_c: 0.0, thermal_crit_count: 0, total_power_watts: 0.0 },
  max_kv_cache_pct: 0.0, requests_running_total: 0, requests_waiting_total: 0, server_on_count: 0,
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
  totals: { active_kwh: 0.041, cost_usd: 0.04, has_power: true, idle_kwh: 0.247, kwh: 0.288, tokens_gen: 65244, usd_per_mtok: 0.6629 },
};
const END_TS = 1794812400;
const HOURLY = { end_ts: END_TS, rows: [
  { active_energy_wh: 2.35, energy_wh: 13.21, hour_ts: END_TS - 7200, tokens_gen: 633 },
  { active_energy_wh: 0.0, energy_wh: 4.7, hour_ts: END_TS - 3600, tokens_gen: 0 },
] };
const HIST = [
  { ts: '2026-11-15T13:02:00+00:00', gpu_power: 35.275, llama_pps: 616.522, llama_tps: 94.1566, lms_pps: 0.0, lms_tps: 0.0 },
];
const LLAMA_STATE = {
  agent_online: true, aggregate: 'sleeping', model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M', state: 'sleeping',
  power: { applied: 'powersave', awake: 'performance', sleep: 'powersave', enabled: true, counters: { failures: 0, switches: 21 },
    last_switch: { outcome: 'verified' }, readback: [{ actual: 'low', expected: 'low', label: 'gpu level', ok: true }] },
  residency: { aggregate: 'sleeping', models: [{ model_id: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M', provider: 'llama', status: 'sleeping' }] },
};
const LMS_METRICS = {
  agent_online: true, gateway_inflight: 0, gateway_tokens: { gen: 8639, prompt: 27654 },
  mac_power: { ane_w: 0.0, cpu_package_w: 0.502, gpu_w: 0.0, soc_total_w: 0.502, thermal_pressure: 'Nominal' },
  models: [{ id: 'qwen3.5-9b@q6_k' }, { id: 'qwen3-8b' }],
  ps: [{ context: 32768, identifier: 'qwen3.5-9b@q6_k', model: 'qwen3.5-9b@q6_k', params: '9B', quant: 'Q6_K', size: '9.28 GB', status: 'IDLE' }],
  server: { on: true, port: 1235 },
  system: { cpu_name: 'Apple M2 Pro', host: 'llm-systems-lmstudio.local',
            ram: { percent: 82.1, total_bytes: 17179869184 }, swap: { percent: 90.0, total_bytes: 6442450944 } },
};
const FORECAST = {
  ok: true, enabled: true, last_result: '10 found, 0 new', window_days: 14, checks: [{ id: 'disk_fill', state: 'ok' }],
  findings: [{ check: 'disk_fill', confidence: 'medium', detail: 'Disk use on / has grown steadily. More text.',
    host: 'llm-systems-agent-opc-herm', last_seen: 1794729608, severity: 'warning', status: 'open', title: 'Disk fill' }],
  cleared: [],
};
const TOWER_STATE = { enabled: true, hosts: ['llm-systems-lmstudio.local'], model: 'qwen3.5-9b@q6_k', timers: 0 };

const DEV = {
  '/api/fleet/llama/aggregate': LLAMA_AGG, '/api/fleet/lms/aggregate': LMS_AGG, '/api/fleet/vllm/aggregate': VLLM_AGG,
  '/api/llama-state': LLAMA_STATE, '/api/lmstudio/metrics': LMS_METRICS, '/api/llm/models': { data: [] },
  '/api/energy/summary?days=1': SUMMARY1, '/api/energy/summary?days=7': SUMMARY1,
  '/api/energy/hourly?hours=168': HOURLY, '/api/energy/hourly?hours=48': HOURLY,
  '/api/tower/state': TOWER_STATE, '/api/tower/insights': { insights: [], ok: true }, '/api/history': HIST,
};

// Every URL fetched; each answers a copy of the routes entry its URL starts with, else {}.
function stubFetch(routes) {
  const calls = [];
  window.fetch = vi.fn(async (url) => {
    calls.push(url);
    const key = Object.keys(routes).find(k => url.startsWith(k));
    return { ok: true, json: async () => JSON.parse(JSON.stringify(key ? routes[key] : {})) };
  });
  return calls;
}

// A fresh #overallTab and a fresh overall.js with the dev globals.
function load() {
  document.body.innerHTML = tabHtml;
  document.getElementById('overallTab').style.display = '';
  delete window._activeTab;
  delete window.ovState;
  window.mcShellStore = undefined;
  window._agentsByProvider = BY_PROVIDER;
  window._pollCfg = { agents: { llama_present: true, lms_present: true, vllm_present: true } };
  window._activeAlerts = [];
  window._latestMetric = undefined;
  window.forecastSnapshot = () => FORECAST;
  evalGlobal(overall);
}

const text = (id) => document.getElementById(id).textContent;
const chart = () => document.querySelector('#ov-b-tp .ts[data-kit-ts]');
const tpData = () => window.kitTsData(chart());

afterEach(() => { vi.restoreAllMocks(); });

describe('Overall rows (#1432)', () => {
  test('overall renders with empty payloads', async () => {
    load();
    window._agentsByProvider = { llama: [], lms: [], vllm: [] };
    window._pollCfg = {};
    window.forecastSnapshot = () => null;
    stubFetch({});
    await window.ovLoad();
    await window.fetchOverallMetrics();
    await flush();
    const bodies = [...document.querySelectorAll('#overallTab [id^="ov-b-"]')];
    expect(bodies).toHaveLength(19);
    for (const b of bodies) expect(b.innerHTML.trim(), b.id).not.toBe('');
    const tab = document.getElementById('overallTab');
    const shown = tab.textContent + [...tab.querySelectorAll('[title]')].map(e => e.getAttribute('title')).join(' ');
    for (const bad of ['NaN', 'undefined', 'null', 'Infinity']) expect(shown, bad).not.toContain(bad);
    expect(document.querySelector('#ov-b-k-agents .k-val').textContent).toBe('0 / 0');
    expect(text('ov-b-alerts')).toContain('No active alerts');
    expect(text('ov-b-hosts')).toContain('No hosts yet');
  });

  test('overall renders the dev samples', async () => {
    load();
    const calls = stubFetch(DEV);
    await window.ovLoad();
    await window.fetchOverallMetrics();
    await flush();
    expect(calls).toContain('/api/history?fleet=all&since_minutes=1440&max_rows=1440');
    expect(document.querySelector('#ov-b-k-agents .k-val').textContent).toBe('5 / 5');
    expect(text('ov-b-srv-llama')).toContain('Sleeping');
    expect(text('ov-b-srv-lms')).toContain('Server on');
    expect(text('ov-b-srv-lms')).toContain('port 1235');
    expect(text('ov-b-srv-vllm')).toContain('Server off');
    for (const k of ['llama', 'lms', 'vllm']) expect(document.querySelector(`[data-card="srv-${k}"]`).hidden, k).toBe(false);
    expect(document.querySelector('[data-card="tp"] .p-title').textContent).toBe('Token throughput');
    expect(document.querySelector('[data-card="tp"] .p-note').textContent).toMatch(/^All providers · last 24 h · Updated /);
    expect(document.querySelector('#ov-b-tp [data-kit-series="ov-tp|Prompt (all)"]').getAttribute('aria-pressed')).toBe('false');
    expect(text('ov-b-forecast')).toContain('Disk use on / has grown steadily.');
    expect(text('ov-b-hosts')).toContain('llm-systems-lmstudio.local');
    expect(document.getElementById('overallTab').getAttribute('data-kit-page')).toBe('overall');
    const vals = [...document.querySelectorAll('#ov-b-power-now .r-val')].map(e => e.textContent);
    expect(vals).toEqual(['11.0 W', '0.5 W']);
    expect([...document.querySelectorAll('#ov-b-srv-lms .c-v')].map(e => e.textContent)).toEqual(['0.5 W', '0.0 W', '0.0 W']);
  });

  test('Throughput says Nothing reported yet without history, then draws the live buckets', async () => {
    load();
    let now = Date.parse('2026-11-16T06:00:30Z');
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    stubFetch({ ...DEV, '/api/history': [] });
    await window.ovLoad();
    expect(chart()).toBeNull();
    expect(text('ov-b-tp')).toBe('Nothing reported yet');
    await window.fetchOverallMetrics();
    expect(tpData().labels).toHaveLength(1);
    now += 5 * 60000;
    await window.fetchOverallMetrics();
    expect(tpData().labels).toHaveLength(2);
    expect(tpData().keep).toBe(288);
  });

  test('a failing tick keeps the last good payloads on the page', async () => {
    load();
    stubFetch(DEV);
    await window.ovLoad();
    await window.fetchOverallMetrics();
    window.fetch = vi.fn(async () => { throw new Error('network down'); });
    await window.fetchOverallMetrics();
    expect(window.fetch).toHaveBeenCalled();
    expect(document.querySelector('#ov-b-k-agents .k-val').textContent).toBe('5 / 5');
    expect(text('ov-b-srv-lms')).toContain('Server on');
    expect(text('ov-b-srv-llama')).toContain('Sleeping');
    expect(document.querySelector('[data-card="srv-vllm"]').hidden).toBe(false);
  });

  test('a slow refresh that got nothing retries on the next tick', async () => {
    load();
    let now = Date.parse('2026-11-16T06:00:30Z');
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    const calls = stubFetch(DEV);
    const ok = window.fetch;
    window.fetch = vi.fn(async (url) => (/^\/api\/(energy|tower|llm)/.test(url) ? { ok: false } : ok(url)));
    await window.ovLoad();
    calls.length = 0;
    window.fetch = ok;
    now += 5000;
    await window.fetchOverallMetrics();
    await flush();
    expect(calls).toContain('/api/energy/summary?days=1');
    expect(text('ov-b-energy-hosts')).toContain('llm-systems-agent-llama');
    calls.length = 0;
    now += 5000;
    await window.fetchOverallMetrics();
    expect(calls.some(u => u.startsWith('/api/energy'))).toBe(false);
  });

  test('Open Events and Open Forecast go to their pages', async () => {
    load();
    stubFetch(DEV);
    window.switchTab = vi.fn();
    window.switchSubTab = vi.fn();
    await window.ovLoad();
    document.querySelector('#ov-b-alerts [data-ov-go="events"]').click();
    expect(window.switchTab).toHaveBeenLastCalledWith('events');
    document.querySelector('#ov-b-forecast [data-ov-go="forecast"]').click();
    expect(window.switchTab).toHaveBeenLastCalledWith('dashboard');
    expect(window.switchSubTab).toHaveBeenCalledWith('dashboard', 'forecast');
    delete window.switchTab;
    delete window.switchSubTab;
  });

  test('the llama.cpp request line shows only when the llama dashboard has a sample', async () => {
    load();
    stubFetch(DEV);
    await window.fetchOverallMetrics();
    expect(text('ov-b-srv-llama')).not.toContain('Requests');
    window._latestMetric = { llama: { requests_processing: 1, requests_deferred: 0, kv_cache_usage_ratio: 0.25 } };
    await window.fetchOverallMetrics();
    expect(text('ov-b-srv-llama')).toContain('Requests1 running · 0 waiting · KV 25% full');
    window._selectedAgents = { llama: ID_LLAMA };
    await window.fetchOverallMetrics();
    expect(text('ov-b-srv-llama')).toContain('Requests on llm-systems-agent-llama1 running');
    delete window._selectedAgents;
  });

  test('saved folds come from the shell store', async () => {
    load();
    const saved = { [window.kitKey('rows')]: JSON.stringify({ overall: { power: 1 } }) };
    window.mcShellStore = { getItem: (k) => saved[k] || null, setItem: (k, v) => { saved[k] = v; } };
    stubFetch(DEV);
    await window.ovLoad();
    expect(document.querySelector('section.row[data-row="power"]').classList.contains('folded')).toBe(true);
    expect(document.querySelector('section.row[data-row="watch"]').classList.contains('folded')).toBe(false);
  });
});

describe('Throughput live updates (#1432)', () => {
  const T0 = Date.parse('2026-11-16T06:00:30Z');
  const agg = (tps) => ({ ...LLAMA_AGG, throughput: { total_tps: tps, total_pps: 0 } });

  test('live append starts a new bucket only on a boundary', async () => {
    load();
    let now = T0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    const routes = { ...DEV };
    stubFetch(routes);
    await window.ovLoad();
    const d0 = tpData();
    expect(d0.labels).toHaveLength(288);
    expect(d0.keep).toBe(288);
    routes['/api/fleet/llama/aggregate'] = agg(5);
    await window.fetchOverallMetrics();
    now = T0 + 60000;
    routes['/api/fleet/llama/aggregate'] = agg(3);
    await window.fetchOverallMetrics();
    const d1 = tpData();
    expect(d1.labels).toHaveLength(288);
    expect(d1.labels[287]).toBe(d0.labels[287]);
    expect(d1.series[0].values[287]).toBe(5);
    now = T0 + 5 * 60000;
    routes['/api/fleet/llama/aggregate'] = agg(2);
    await window.fetchOverallMetrics();
    const d2 = tpData();
    expect(d2.labels).toHaveLength(288);
    expect(d2.keep).toBe(288);
    expect(d2.labels[286]).toBe(d0.labels[287]);
    expect(d2.labels[287]).toBe(OV.rangeLabel(Date.parse('2026-11-16T06:05:00Z'), 24));
    expect(d2.series[0].values[286]).toBe(5);
    expect(d2.series[0].values[287]).toBe(2);
  });

  test('zoomed chart holds on append', async () => {
    load();
    let now = T0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    stubFetch(DEV);
    await window.ovLoad();
    const d = tpData();
    const z = { from: d.labels[100], to: d.labels[200] };
    d.z = z;
    window.kitRedrawTs(chart(), d, d.w);
    now = T0 + 5 * 60000;
    await window.fetchOverallMetrics();
    const after = tpData();
    expect(after.z).toEqual(z);
    expect(after.labels).toHaveLength(288);
    expect(document.querySelector('#ov-b-tp [data-kit-zoom-reset]')).toBeTruthy();
  });

  test('a tick fetches no history and the slow payloads at most once a minute', async () => {
    load();
    let now = T0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    const calls = stubFetch(DEV);
    await window.ovLoad();
    calls.length = 0;
    now = T0 + 30000;
    await window.fetchOverallMetrics();
    expect(calls.some(u => u.startsWith('/api/history'))).toBe(false);
    expect(calls.some(u => u.startsWith('/api/energy'))).toBe(false);
    expect(calls).toContain('/api/llama-state');
    expect(calls).toContain('/api/lmstudio/metrics');
    now = T0 + 61000;
    await window.fetchOverallMetrics();
    await flush();
    expect(calls).toContain('/api/energy/hourly?hours=168');
    expect(calls).toContain('/api/tower/insights');
    expect(calls.some(u => u.startsWith('/api/history'))).toBe(false);
  });
});

describe('Pinned row and hidden tab (#1432)', () => {
  test('pinned row keeps adoption', async () => {
    load();
    document.body.insertAdjacentHTML('afterbegin',
      '<div class="grid" id="cardGrid"><div class="card" data-card="gpu"><h3>GPU</h3></div></div>');
    document.getElementById('overallGrid').innerHTML = '<div class="card ov-shell" data-card="ov-borrow-gpu"></div>';
    window.layout = { overallBorrowed: ['gpu'] };
    window._ovAdopted = new Set();
    window._ovHomeMarks = {};
    window._ensureSizeBtn = () => {};
    window._resizeChartsIn = () => {};
    evalGlobal([fnSrc(foundation, '_homeCardEl'), fnSrc(foundation, 'adoptPinnedCards'),
      'window._homeCardEl = _homeCardEl; window.adoptPinnedCards = adoptPinnedCards;'].join('\n'));
    stubFetch(DEV);
    await window.ovLoad();
    const grid = document.getElementById('overallGrid');
    expect(grid.parentElement.matches('section.row[data-row="pinned"]')).toBe(true);
    expect(grid.hasAttribute('data-cards')).toBe(false);
    expect([...document.querySelectorAll('#overallTab > section.row')].map(r => r.dataset.row))
      .toEqual(['fleet', 'throughput', 'servers', 'power', 'watch', 'pinned']);
    window.adoptPinnedCards();
    expect(document.querySelector('#overallGrid [data-card="ov-borrow-gpu"] [data-card="gpu"]')).toBeTruthy();
    expect(document.querySelector('#cardGrid [data-card="gpu"]')).toBeNull();
  });

  test('hidden tab does not fetch', async () => {
    load();
    const calls = stubFetch(DEV);
    window._activeTab = 'dashboard';
    await window.fetchOverallMetrics();
    await window.ovLoad();
    window.ovRender();
    expect(calls).toEqual([]);
    expect(document.getElementById('ov-b-tp').innerHTML).toBe('');
  });
});

// Holds every fetch whose URL matches until the returned release() is called.
function holdFetch(match) {
  const ok = window.fetch;
  let release;
  const gate = new Promise(r => { release = r; });
  window.fetch = vi.fn(async (url) => (match(url) ? gate.then(() => ok(url)) : ok(url)));
  return () => release();
}
const chips = () => [...document.querySelectorAll('#ov-b-tp .legend .chip')].map(c => c.textContent.replace(/\s+/g, ' ').trim());

describe('Overall final fixes (#1432)', () => {
  const T0 = Date.parse('2026-11-16T06:00:30Z');

  test('legend chips show each provider\'s tokens over 24 h and follow the minute refresh', async () => {
    load();
    let now = T0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    const s1 = JSON.parse(JSON.stringify(SUMMARY1));
    s1.hosts[0].tokens_prompt = 91227;
    s1.hosts[1].tokens_prompt = 250000;
    const routes = { ...DEV, '/api/energy/summary?days=1': s1 };
    stubFetch(routes);
    await window.ovLoad();
    await flush();
    expect(chips()).toEqual(['llama.cpp 10.2k tokens', 'LM Studio 55.0k tokens', 'vLLM 0 tokens', 'Prompt (all) 341.2k tokens hidden']);
    now = T0 + 5000;
    await window.fetchOverallMetrics();
    expect(chips()[0]).toBe('llama.cpp 10.2k tokens');
    routes['/api/energy/summary?days=1'] = { ...s1, hosts: [{ ...s1.hosts[0], tokens_gen: 20000 }, s1.hosts[1]] };
    now = T0 + 62000;
    await window.fetchOverallMetrics();
    await flush();
    expect(chips()[0]).toBe('llama.cpp 20.0k tokens');
    expect(tpData().series[0].legend).toBe('20.0k tokens');
  });

  test('server panels keep the live speed, awake count, detail host and GPU hot', async () => {
    load();
    vi.spyOn(Date, 'now').mockImplementation(() => T0);
    window._selectedAgents = { llama: ID_LLAMA };
    const lms = { ...LMS_AGG, gpu: { ...LMS_AGG.gpu, thermal_crit_count: 2 },
      throughput: { total_tps: 12.34, total_pps: 40.06, window: { gen: { peak: { ts: '2026-11-16T05:48:30Z', v: 197 } } } } };
    stubFetch({ ...DEV, '/api/fleet/lms/aggregate': lms });
    await window.ovLoad();
    await window.fetchOverallMetrics();
    expect(text('ov-b-srv-lms')).toContain('SpeedNow 12.3 t/s · prompt 40.1 t/s · peak 197 t/s 12 min ago');
    expect(document.querySelector('[data-card="srv-lms"] .ov-pill.bad').textContent).toBe('GPU hot');
    expect(document.querySelector('[data-card="srv-llama"] .ov-pill.bad')).toBeNull();
    expect(document.querySelector('[data-card="srv-llama"] .p-note').textContent).toBe('llm-systems-agent-llama · 1 of 2 awake');
    expect(document.querySelector('[data-card="srv-lms"] .p-note').textContent).toBe('llm-systems-lmstudio · 2 of 2 hosts online');
    expect(document.querySelector('[data-card="srv-lms"] .p-note').title).toBe('llm-systems-lmstudio · 2 of 2 hosts online');
    expect(document.querySelector('[data-card="energy7"] .p-note').title).toBe('7 days · Wh');
    expect(text('ov-b-srv-llama')).toContain('SpeedNow 0.0 t/s · prompt 0.0 t/s');
    delete window._selectedAgents;
  });

  test('forecast shows the top 6 by severity, N more and Tower\'s digest; alerts the newest 5; tables keep all', async () => {
    load();
    vi.spyOn(Date, 'now').mockImplementation(() => T0);
    const findings = Array.from({ length: 8 }, (_, i) => ({ check: 'disk_fill', detail: `Finding ${i}.`, host: 'h',
      last_seen: 1794729608 + i, severity: i === 0 ? 'info' : i === 1 ? 'critical' : 'warning' }));
    window.forecastSnapshot = () => ({ ...FORECAST, findings, digest: 'Memory is the common factor. Look at node-3 first.' });
    window.FC = { shortDigest: (t) => String(t).split('. ')[0] + '.' };
    window._activeAlerts = Array.from({ length: 7 }, (_, i) => ({ rule_name: `Alert ${i}`, severity: 'warning',
      created_at: `2026-11-16T0${i}:00:00Z` }));
    stubFetch(DEV);
    await window.ovLoad();
    const fc = [...document.querySelectorAll('#ov-b-forecast .fd')][0];
    expect([...fc.querySelectorAll('.fd-t')].map(e => e.firstChild.textContent))
      .toEqual(['Finding 1.', 'Finding 7.', 'Finding 6.', 'Finding 5.', 'Finding 4.', 'Finding 3.']);
    expect(document.querySelector('#ov-b-forecast .ov-more').textContent).toBe('2 more');
    expect(text('ov-b-forecast')).toContain('Tower: Memory is the common factor.');
    expect(document.querySelector('#ov-b-forecast [data-ov-go="forecast"]').textContent).toBe('Open Forecast');
    const fcTable = document.querySelector('[data-card="forecast"] .p-table');
    expect([...fcTable.querySelectorAll('tbody tr')].filter(r => r.textContent.startsWith('Open'))).toHaveLength(8);
    expect([...document.querySelectorAll('#ov-b-alerts .fd-t')].map(e => e.firstChild.textContent))
      .toEqual(['Alert 6', 'Alert 5', 'Alert 4', 'Alert 3', 'Alert 2']);
    expect(document.querySelector('#ov-b-alerts .ov-more').textContent).toBe('2 more');
    expect(document.querySelectorAll('[data-card="alerts"] .p-table tbody tr')).toHaveLength(7);
    delete window.FC;
  });

  test('tile and rank values: tokens in k, peak with one decimal, Wh with one decimal under 10', async () => {
    load();
    vi.spyOn(Date, 'now').mockImplementation(() => T0);
    const s7 = JSON.parse(JSON.stringify(SUMMARY1));
    s7.hosts[1].kwh = 0.0047;
    stubFetch({ ...DEV, '/api/energy/summary?days=7': s7 });
    await window.ovLoad();
    await flush();
    expect(document.querySelector('#ov-b-k-tok .k-val').textContent).toBe('65.2k');
    expect(document.querySelector('#ov-b-k-peak .k-val').textContent).toBe('94.2 t/s');
    expect([...document.querySelectorAll('#ov-b-hosts-tokens .r-val')].map(e => e.textContent)).toEqual(['55.0k', '10.2k']);
    expect([...document.querySelectorAll('#ov-b-energy-hosts .r-val')].map(e => e.textContent)).toEqual(['253 Wh', '4.7 Wh']);
    expect([...document.querySelectorAll('#ov-b-energy-split .c-v')].map(e => e.textContent)).toEqual(['41 Wh', '247 Wh', '253 Wh']);
    expect(document.querySelector('[data-card="k-agents"] .p-title').textContent).toBe('Hosts online');
    expect(document.querySelector('[data-card="k-tok"] .p-title').textContent).toBe('Tokens');
    expect(text('ov-b-k-tok')).toContain('last 24 h');
  });

  test('tab entry draws Throughput when history lands; energy and Tower say Loading until theirs land', async () => {
    load();
    vi.spyOn(Date, 'now').mockImplementation(() => T0);
    stubFetch(DEV);
    const release = holdFetch(u => /^\/api\/(energy|tower)/.test(u));
    await window.ovLoad();
    expect(tpData().labels).toHaveLength(288);
    for (const id of ['ov-b-energy7', 'ov-b-energy-hosts', 'ov-b-energy-split', 'ov-b-hosts-tokens', 'ov-b-tower']) {
      expect(text(id), id).toBe('Loading…');
    }
    release();
    await flush();
    expect(text('ov-b-energy-hosts')).toContain('llm-systems-agent-llama');
    expect(text('ov-b-tower')).toContain('qwen3.5-9b@q6_k');
    expect(document.querySelector('#ov-b-energy7 .ts[data-kit-ts]')).toBeTruthy();
  });

  test('llama.cpp presets are fetched only while the llama.cpp panel shows, at most once a minute', async () => {
    load();
    let now = T0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    window.ovState = { prov: 'lms', range: 24 };
    const calls = stubFetch(DEV);
    await window.ovLoad();
    await window.fetchOverallMetrics();
    await flush();
    expect(calls).not.toContain('/api/llm/models');
    await window.ovSetFilter('all');
    await window.fetchOverallMetrics();
    await flush();
    expect(calls.filter(u => u === '/api/llm/models')).toHaveLength(1);
    now = T0 + 30000;
    await window.fetchOverallMetrics();
    expect(calls.filter(u => u === '/api/llm/models')).toHaveLength(1);
    window._pollCfg = { agents: { llama_present: false, lms_present: true, vllm_present: true } };
    now = T0 + 120000;
    await window.fetchOverallMetrics();
    await flush();
    expect(calls.filter(u => u === '/api/llm/models')).toHaveLength(1);
  });

  test('a reseed with the same filter and range keeps the zoom while its labels exist', async () => {
    load();
    vi.spyOn(Date, 'now').mockImplementation(() => T0);
    stubFetch(DEV);
    await window.ovLoad();
    const d = tpData(), z = { from: d.labels[100], to: d.labels[200] };
    window.kitRedrawTs(chart(), { ...d, z }, d.w);
    await window.ovLoad();
    expect(tpData().z).toEqual(z);
    expect(document.querySelector('#ov-b-tp [data-kit-zoom-reset]')).toBeTruthy();
    window.kitRedrawTs(chart(), { ...tpData(), z: { from: 'gone', to: d.labels[200] } }, d.w);
    await window.ovLoad();
    expect(tpData().z).toBeUndefined();
    await window.ovSetFilter('lms');
    expect(tpData().z).toBeUndefined();
  });

  test('a tick with unchanged data writes no panel, so focus and nodes survive', async () => {
    load();
    vi.spyOn(Date, 'now').mockImplementation(() => T0);
    const routes = { ...DEV };
    stubFetch(routes);
    await window.ovLoad();
    await window.fetchOverallMetrics();
    const ul = document.querySelector('#ov-b-hosts ul'), btn = document.querySelector('#ov-b-alerts [data-ov-go]');
    const lmsBody = document.querySelector('#ov-b-srv-lms dl'), table = document.querySelector('[data-card="hosts"] .p-table table');
    btn.focus();
    await window.fetchOverallMetrics();
    expect(document.querySelector('#ov-b-hosts ul')).toBe(ul);
    expect(document.querySelector('#ov-b-srv-lms dl')).toBe(lmsBody);
    expect(document.querySelector('[data-card="hosts"] .p-table table')).toBe(table);
    expect(document.activeElement).toBe(btn);
    routes['/api/fleet/vllm/aggregate'] = { ...VLLM_AGG, agents: [{ ...VLLM_AGG.agents[0], online: false }] };
    await window.fetchOverallMetrics();
    expect(document.querySelector('#ov-b-hosts ul')).not.toBe(ul);
  });

  test('a live gap over 3 buckets reseeds from history; up to 3 buckets fill with zeros', async () => {
    load();
    let now = T0;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    const calls = stubFetch(DEV);
    await window.ovLoad();
    await window.fetchOverallMetrics();
    calls.length = 0;
    now = T0 + 20 * 60000;
    await window.fetchOverallMetrics();
    expect(calls.some(u => u.startsWith('/api/history'))).toBe(false);
    expect(tpData().labels[287]).toBe(OV.rangeLabel(Date.parse('2026-11-16T06:20:00Z'), 24));
    now = T0 + 45 * 60000;
    await window.fetchOverallMetrics();
    await flush();
    expect(calls.filter(u => u.startsWith('/api/history'))).toHaveLength(1);
    expect(tpData().labels[287]).toBe(OV.rangeLabel(Date.parse('2026-11-16T06:45:00Z'), 24));
  });

  test('an empty fleet says no LLM server has reported; a shown panel hides that sentence', async () => {
    load();
    window._agentsByProvider = { llama: [], lms: [], vllm: [] };
    window._pollCfg = {};
    stubFetch({});
    await window.ovLoad();
    await window.fetchOverallMetrics();
    const none = document.getElementById('ov-srv-none');
    expect(none.hidden).toBe(false);
    expect(none.textContent).toBe('No LLM servers reported yet.');
    expect(none.closest('section.row').dataset.row).toBe('servers');
    for (const k of ['llama', 'lms', 'vllm']) expect(document.querySelector(`[data-card="srv-${k}"]`).hidden).toBe(true);
    load();
    stubFetch(DEV);
    await window.ovLoad();
    await window.fetchOverallMetrics();
    expect(document.getElementById('ov-srv-none').hidden).toBe(true);
    await window.ovSetFilter('vllm');
    expect(document.getElementById('ov-srv-none').hidden).toBe(true);
  });

  test('markup and styles: Hosts spans the row, key-value lines wrap, the pinned sentence, no ruling tags', () => {
    load();
    expect(document.querySelector('[data-card="hosts"]').classList.contains('hero')).toBe(true);
    expect(document.getElementById('ovPinNone').textContent)
      .toBe('Nothing pinned yet. Pin a card from any dashboard in Settings › Pinned from other pages.');
    const css = srcFile('css/overall.css'), dd = css.match(/\.ov-kv dd \{([^}]*)\}/)[1];
    expect(dd).not.toMatch(/nowrap|ellipsis/);
    expect(dd).toMatch(/overflow-wrap: anywhere/);
    // Labels as narrow as their text (capped), values take the rest of the width.
    expect(css.match(/\.ov-kv \{([^}]*)\}/)[1]).toMatch(/grid-template-columns: auto minmax\(0, 1fr\)/);
    expect(css.match(/\.ov-kv dt \{([^}]*)\}/)[1]).toMatch(/max-width: 11em/);
    expect(dd).not.toMatch(/min-width: (?!0)/);
    // Overall panel headers stay on one line; a long note truncates with an ellipsis.
    expect(css).toMatch(/#overallTab \.panel > \.p-hd \{ flex-wrap: nowrap; \}/);
    const noteRule = css.match(/#overallTab \.panel > \.p-hd > \.p-note \{([^}]*)\}/)[1];
    for (const d of ['min-width: 0', 'overflow: hidden', 'text-overflow: ellipsis', 'white-space: nowrap']) expect(noteRule).toContain(d);
    for (const f of ['js/overall.js', 'js/lib/overall-view.js', 'js/foundation.js']) {
      const comments = srcFile(f).split('\n').filter(l => /^\s*\/\//.test(l)).join('\n');
      expect(comments, f).not.toMatch(/\((R|C|P)\d\)|\bR\d:|\bR\d,/);
    }
  });
});
