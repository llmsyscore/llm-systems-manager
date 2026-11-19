// #1432 phase 4d: Dashboards › Manager on the panel kit, built by js/dashboard-manager.js; its System row by js/hostpage.js.
// Runs kit.js, the view libs, overall.js, hostpage.js, dashboard-manager.js and charts.js's perf loader with fetch stubbed.
import { describe, test, expect, afterEach, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import OV from '../js/lib/overall-view.js';
import HV from '../js/lib/hosts-view.js';
import HP from '../js/lib/host-view.js';
import MV from '../js/lib/manager-view.js';
import LMSeries from '../js/lib/series.js';
import LMPeaks from '../js/lib/peaks.js';
import SettingsLib from '../js/lib/settingsdrawer.js';
import DashboardManagerDb from '../js/lib/dbstats.js';
import { srcFile, fnSrc, blockSrc, evalGlobal, flush, runHarness } from './helpers/harness.js';

const indexSrc = srcFile('index.html');
const indexDoc = new JSDOM(indexSrc).window.document;
// #dashboardTab holding only #dash-manager, and Overall's Pinned row with its grid.
const tabHtml = (() => {
  const tab = indexDoc.getElementById('dashboardTab').cloneNode(false);
  tab.appendChild(indexDoc.getElementById('dash-manager').cloneNode(true));
  return tab.outerHTML + '<div id="overallTab"><section class="row" data-row="pinned"><div class="grid" id="overallGrid"></div></section></div>';
})();
const foundation = srcFile('js/foundation.js');
const charts = srcFile('js/charts.js');
const overall = srcFile('js/overall.js');
const hostpage = srcFile('js/hostpage.js');
const dashMgr = srcFile('js/dashboard-manager.js');
evalGlobal(srcFile('kit/kit.js'));
Object.assign(window, { OV, HV, HP, MV, LMSeries, LMPeaks, SettingsLib, DashboardManagerDb });

const ID_M = 'mmmmmmmm-0000-4000-8000-000000000001';
const ID_B = 'bbbbbbbb-0000-4000-8000-000000000002';
const NOW = Date.now();
const iso = (sAgo) => new Date(NOW - sAgo * 1000).toISOString();
const row = (source, host, name, value, sAgo = 5) => ({ source, hostname: host, metric_name: name, latest_value: value, latest_timestamp: iso(sAgo) });
const svc = (host, name, f) => Object.entries(f).map(([k, v]) => row('processes', host, name + '_' + k, v));
const PROCESSES = [].concat(
  svc('mgr-host', 'manager', { running: 1, pid: 11, rss_mb: 200, uptime_s: 7200 }),
  svc('mgr-host', 'alarm-engine', { running: 1, pid: 12, rss_mb: 90, uptime_s: 7200 }),
  svc('mgr-host', 'influxdb', { running: 1, pid: 13, rss_mb: 300, uptime_s: 7200 }),
  svc('beta', 'llama-server', { running: 1, pid: 21, rss_mb: 900, uptime_s: 60 }),
);
// The default sample is as old as the newest seeded point, so it appends nothing.
const SELFMON = (api, at = 60) => [row('manager_self_monitor', 'mgr-host', 'manager_api_latency_ms', api, at),
  row('manager_self_monitor', 'mgr-host', 'manager_history_latency_ms', 40, at),
  row('manager_self_monitor', 'mgr-host', 'ae_health_latency_ms', 8, at)];
const INFLUX = [row('influxdb', 'mgr-host', 'up', 1), row('influxdb', 'mgr-host', 'ping_ms', 1.5), row('influxdb', 'mgr-host', 'bytes_on_disk', 1048576)];
const STATS = (extra) => Object.assign({
  pool: { active: 2, limit: 8, peak: 3, refusals: 0 }, worker_threads_busy: 1, worker_threads: 16, worker_backlog: 0,
  browser_connections: 2, agent_connections: 2, sse_daemon_running: false,
  agents: [{ agent_id: ID_B, hostname: 'beta', reachable: true, active: 1, cap: 4, peak: 2, refusals: 0, terminal_sessions: 0 },
    { hostname: 'gamma', reachable: true, active: 0, cap: 2, peak: 0, refusals: 0, terminal_sessions: 1 }],
}, extra);
const AGENTS = { agents: [{ agent_id: ID_M, hostname: 'mgr-host', status: 'approved', liveness: 'live', last_heartbeat: iso(3) },
  { agent_id: ID_B, hostname: 'beta', status: 'approved', liveness: 'stale', last_heartbeat: iso(120) }] };
// Seeded points of a self-monitor series: one a minute for the last 5 minutes.
const POINTS = (v) => [5, 4, 3, 2, 1].map(m => ({ timestamp: iso(m * 60), value: v }));
const SYS = { cpu_total: 30, ram: { percent: 40, used_bytes: 4e9, total_bytes: 1e10 },
  disk: [{ mountpoint: '/', percent: 50, used_bytes: 5e10, total_bytes: 1e11 }], net: { bytes_sent_per_sec: 100, bytes_recv_per_sec: 200 } };

// Live answers by route; tests replace entries to change what is reported.
function routes() {
  return {
    processes: PROCESSES, self: SELFMON(12), influx: INFLUX, stats: STATS(), agents: AGENTS, points: (name) => POINTS(name === 'manager_api_latency_ms' ? 10 : 20),
    aeDb: { alarms_db: { alerts: 1, size_bytes: 4096 }, settings_db: { rules: 2, size_bytes: 2048 } },
    mgrDb: { manager_db: { size_bytes: 8192 }, audit_db: { size_bytes: 1024, audit_rows: 3 }, energy_db: { size_bytes: 512 } },
    hosts: { hosts: [{ agent_id: ID_M, hostname: 'mgr-host', online: true, providers: [] }] },
    system: { agent_id: ID_M, hostname: 'mgr-host', online: true, age_s: 1, sample: SYS },
    tower: { enabled: true, model: 'm' }, insights: { insights: [] },
  };
}
const FAIL = Symbol('fail');
// fetch stub: every URL recorded; GET /api/layout answers saved, its POST bodies are kept in posts.
function stubFetch(r, saved = {}) {
  const calls = [], posts = [];
  // A route answer of FAIL comes back as HTTP 500.
  const ok = (j) => Promise.resolve(j === FAIL ? { ok: false, status: 500, json: async () => ({}) }
    : { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(j)) });
  window.fetch = vi.fn((url, opts) => {
    calls.push(url);
    if (url === '/api/layout') {
      if (opts && opts.method === 'POST') { posts.push(JSON.parse(opts.body)); return ok({ ok: true }); }
      return ok(saved);
    }
    const src = (/^\/api\/alarm\/metrics\?source=([^&]+)/.exec(url) || [])[1];
    if (src === 'processes') return ok(r.processes);
    if (src === 'manager_self_monitor') return ok(r.self);
    if (src === 'influxdb') return ok(r.influx);
    const pt = /^\/api\/alarm\/metrics\/manager_self_monitor\/([^?]+)/.exec(url);
    if (pt) return ok(r.points(decodeURIComponent(pt[1])));
    if (url === '/api/alarm/dbstats/sqlite') return ok(r.aeDb);
    if (url === '/api/admin/dbstats/sqlite') return ok(r.mgrDb);
    if (url === '/api/admin/stream-stats') return ok(r.stats);
    if (url === '/api/agents') return ok(r.agents);
    if (url === '/api/agents/hosts') return ok(r.hosts);
    if (/^\/api\/agents\/[^/]+\/system$/.test(url)) return ok(r.system);
    if (url.startsWith('/api/history?')) return ok([]);
    if (url === '/api/tower/state') return ok(r.tower);
    if (url === '/api/tower/insights') return ok(r.insights);
    return ok({});
  });
  return { calls, posts };
}

function makeStore() {
  const m = {};
  return { getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null), setItem: (k, v) => { m[k] = String(v); } };
}
// foundation.js's real layout load, save, pin adoption and pinned backfill at global scope; `layout` is window.layout.
function loadFoundation() {
  const labels = blockSrc(foundation, '// Host page panel ids and titles', '\n// ---------------------------------------------------------------------------\n// Layout persistence');
  const renames = blockSrc(foundation, 'const _LEGACY_CARD_RENAMES = {', '\n};');
  const saver = blockSrc(foundation, 'let _layoutInFlight = null;', 'let _layoutPending  = false;');
  evalGlobal([
    labels.replace(/^const /gm, 'var '), renames.replace(/^const /, 'var '), saver.replace(/^let /gm, 'var '),
    'var _ovHomeMarks = {};', 'window._ovAdopted = new Set();',
    ...['loadLayout', '_migrateLegacyCardIds', 'applyLayout', 'saveLayout', '_ovPinned', '_homeCardEl', 'adoptPinnedCards',
      '_returnOneAdopted', 'returnPinnedCards', '_ovBackfillPinnedProviders'].map(n => fnSrc(foundation, n)),
    'Object.assign(window, { loadLayout, saveLayout, _ovPinned, adoptPinnedCards, returnPinnedCards, _ovBackfillPinnedProviders });',
  ].join('\n'));
  Object.assign(window, { applyTheme() {}, applyAllGridCols() {}, _applyRowsHidden() {}, _ensureSizeBtn() {}, _resizeChartsIn() {} });
}
// A fresh Manager page: the markup, the scripts and their globals, on Dashboards › Manager unless tab says otherwise.
function load(opts = {}) {
  if (typeof window.hpLeave === 'function') window.hpLeave();
  document.body.innerHTML = tabHtml;
  window._activeTab = opts.tab || 'dashboard';
  window._subTabState = { dashboard: 'manager' };
  window.__MGR_AGENT = 'mgr' in opts ? opts.mgr : ID_M;
  window.ahState = { range: 24 };
  window.mcShellStore = makeStore();
  window.LivePause = { on: false, every: (fn, ms) => setInterval(fn, ms) };
  window._agentsByProvider = { llama: [], lms: [], vllm: [] };
  window._pollCfg = {};
  window._activeAlerts = [];
  window.forecastSnapshot = () => null;
  window.layout = opts.layout || {};
  Object.keys(window.KIT_SERIES_OFF).forEach(k => { delete window.KIT_SERIES_OFF[k]; });
  loadFoundation();
  evalGlobal(overall);
  evalGlobal(hostpage);
  evalGlobal(dashMgr);
  evalGlobal(fnSrc(charts, 'loadManagerPerfHistory') + '\nwindow.loadManagerPerfHistory = loadManagerPerfHistory;');
}
async function settle() { for (let i = 0; i < 10; i++) await flush(); }
// Manager page entry as boot.js does it: the page, its fetches and the perf seed, then the System row.
async function enter() {
  window.mgEnter();
  window.fetchServicesAndInflux();
  window.fetchManagerAgentsCard();
  window.fetchManagerStreamsCard();
  window.loadManagerPerfHistory();
  window.hpEnter('mgr');
  await settle();
}
const $ = (id) => document.getElementById(id);
const noteOf = (card) => (document.querySelector(`[data-card="${card}"] .p-hd .p-note`) || { textContent: '' }).textContent;
const bodyRows = (id) => [...$(id).querySelectorAll('tbody tr')].map(tr => [...tr.children].map(td => td.textContent));

afterEach(() => {
  if (typeof window.hpLeave === 'function') window.hpLeave();
  vi.restoreAllMocks();
});

describe('Manager page (#1432)', () => {
  test('index.html has the kit roots and none of the old cards or charts', () => {
    const page = indexDoc.getElementById('dash-manager');
    expect(indexDoc.getElementById('managerCardGrid')).toBeNull();
    expect(indexDoc.querySelector('canvas#mgrPerfChart')).toBeNull();
    expect(indexDoc.querySelector('canvas#aePerfChart')).toBeNull();
    expect([...page.children].map(c => c.id)).toEqual(['mg-root', 'hp-mgr']);
    expect(indexDoc.getElementById('hp-mgr').getAttribute('data-hp-page')).toBe('mgr');
  });

  test('entry builds the 5 rows and 10 panels, and the System row with its 6 panels', async () => {
    load();
    stubFetch(routes());
    await enter();
    const rows = [...document.querySelectorAll('#mg-root section.row[data-row]')].map(r => r.getAttribute('data-row'));
    expect(rows).toEqual(['mgr-now', 'mgr-perf-row', 'mgr-data', 'mgr-conn', 'mgr-tower-row']);
    const thirds = [...document.querySelectorAll('#mg-root .panels.mg-thirds')].map(b => b.closest('section.row').getAttribute('data-row'));
    expect(thirds).toEqual(['mgr-perf-row']);
    const titles = [...document.querySelectorAll('#mg-root section.row[data-row] > .row-hd')]
      .map(b => [...b.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim());
    expect(titles).toEqual(['Snapshot', 'Performance', 'Services and databases', 'Streams', 'Tower']);
    const panels = [...document.querySelectorAll('#mg-root section.panel[data-card]')].map(p => p.getAttribute('data-card'));
    expect(panels).toEqual(['mgr-tiles', 'mgr-perf', 'mgr-ae-perf', 'mgr-influx-perf', 'mgr-services', 'mgr-db', 'mgr-streams', 'mgr-hosts', 'mgr-procs', 'mgr-tower']);
    expect(window.mgPagePanelIds()).toEqual(panels);
    const solo = [...document.querySelectorAll('#mg-root section.panel.mc-solo')].map(p => p.getAttribute('data-card'));
    expect(solo).toEqual(['mgr-tiles', 'mgr-tower']);
    expect($('mg-root').getAttribute('data-kit-page')).toBe('manager');
    const sys = [...document.querySelectorAll('#hp-mgr section.panel[data-card]')].map(p => p.getAttribute('data-card'));
    expect(sys).toEqual(['mgr-cpu-mem', 'mgr-cpu-details', 'mgr-mem-details', 'mgr-net', 'mgr-io', 'mgr-disk']);
    expect(document.querySelector('#hp-mgr .hp-head').textContent).toContain('Manager host: mgr-host');
    expect(document.querySelector('#hp-mgr select')).toBeNull();
    const shown = $('dash-manager').textContent;
    for (const bad of ['NaN', 'undefined', 'Infinity', 'Agent']) expect(shown, bad).not.toContain(bad);
  });

  test('the tiles show browsers, streams, workers, database size and the worst probe', async () => {
    load();
    stubFetch(routes());
    await enter();
    const labels = [...document.querySelectorAll('#mg-b-tiles .hp-tl')].map(t => t.textContent);
    expect(labels).toEqual(['Browsers connected', 'Streams', 'Workers busy', 'Database size', 'Worst probe']);
    expect($('mg-b-tiles').textContent).toContain('2 / 8');
    expect($('mg-b-tiles').textContent).toContain('40.0 ms');
  });

  test('the services table groups by host, with a Host column', async () => {
    load();
    stubFetch(routes());
    await enter();
    const head = [...$('mg-b-services').querySelectorAll('thead th')].map(th => th.textContent);
    expect(head).toEqual(['Host', 'Service', 'Status', 'PID', 'RSS', 'Uptime']);
    const hosts = bodyRows('mg-b-services').map(r => r[0]);
    expect(hosts).toEqual(['beta · 1/1 up', 'mgr-host · 3/3 up', 'mgr-host · 3/3 up', 'mgr-host · 3/3 up']);
    expect(noteOf('mgr-services')).toBe('4/4 up');
  });

  test('a host\'s up count counts only its running services', async () => {
    load();
    const r = routes();
    r.processes = PROCESSES.concat(svc('mgr-host', 'llm-systems-agent', { running: 0, pid: 14 }));
    stubFetch(r);
    await enter();
    expect(bodyRows('mg-b-services').map(x => x[0])).toEqual(['beta · 1/1 up', 'mgr-host · 3/4 up', 'mgr-host · 3/4 up', 'mgr-host · 3/4 up', 'mgr-host · 3/4 up']);
    expect(noteOf('mgr-services')).toBe('4/5 up');
  });

  test('the Databases note gives the InfluxDB state from its up metric', async () => {
    const run = async (influx) => {
      load();
      const r = routes();
      r.influx = influx;
      stubFetch(r);
      await enter();
      const n = document.querySelector('[data-card="mgr-db"] .p-hd .p-note');
      return [n.textContent, n.classList.contains('mg-warn'), n.classList.contains('mg-bad')];
    };
    expect(await run(INFLUX)).toEqual(['InfluxDB up', false, false]);
    expect(await run([row('influxdb', 'mgr-host', 'up', 1), row('influxdb', 'mgr-host', 'ping_ms', 150)])).toEqual(['InfluxDB up', true, false]);
    expect(await run([row('influxdb', 'mgr-host', 'up', 1), row('influxdb', 'mgr-host', 'query_ms', 600)])).toEqual(['InfluxDB up', true, false]);
    expect(await run([row('influxdb', 'mgr-host', 'up', 0)])).toEqual(['InfluxDB down', false, true]);
    expect(await run([row('influxdb', 'mgr-host', 'up', 1, 120)])).toEqual(['InfluxDB no data', false, false]);
  });

  test('the Worst probe tile says how fresh the probes are, in the tile and its table row', async () => {
    load();
    const r = routes();
    stubFetch(r);
    await enter();
    const sub = [...document.querySelectorAll('#mg-b-tiles .hp-tile')].pop().querySelector('.k-sub').textContent;
    expect(sub).toMatch(/^Manager history · (6\d)s ago$/);
    const tableRow = [...document.querySelectorAll('[data-card="mgr-tiles"] .p-table tbody tr')].map(tr => tr.textContent).pop();
    expect(tableRow).toContain(sub);
    r.self = SELFMON(12, 400);
    load();
    stubFetch(r);
    await enter();
    expect([...document.querySelectorAll('#mg-b-tiles .hp-tile')].pop().querySelector('.k-sub').textContent).toBe('no recent samples');
  });

  test('the databases panel is full width with the 22 values in three Field/Value groups side by side', async () => {
    load();
    stubFetch(routes());
    await enter();
    expect(document.querySelector('[data-card="mgr-db"]').classList.contains('hp-wide')).toBe(true);
    const groups = [...$('mg-b-db').querySelectorAll('.mg-dbs > .mg-dbg')];
    expect(groups.map(g => g.querySelector('.ov-lab').textContent)).toEqual(['InfluxDB', 'Manager SQLite', 'Alarm engine SQLite']);
    const counts = groups.map(g => g.querySelectorAll('tbody tr').length);
    expect(counts).toEqual([6, 8, 8]);
    expect([...groups[0].querySelectorAll('tbody tr')][0].textContent).toBe('Ping ms1.5');
    expect(groups.every(g => g.querySelectorAll('tbody tr td').length === g.querySelectorAll('tbody tr').length * 2)).toBe(true);
    const table = document.querySelector('[data-card="mgr-db"] .p-table');
    expect([...table.querySelectorAll('thead th')].map(th => th.textContent)).toEqual(['Database', 'Field', 'Value']);
    const rows = [...table.querySelectorAll('tbody tr')].map(tr => [...tr.children].map(td => td.textContent));
    expect(rows).toHaveLength(22);
    expect(rows[0]).toEqual(['InfluxDB', 'Ping ms', '1.5']);
    expect(noteOf('mgr-db')).toBe('InfluxDB up');
  });

  test('full-width Services and Databases get no span; a short line\'s last panel spans the empty columns', async () => {
    const P = window.HTMLElement.prototype, gcs = window.getComputedStyle;
    const rootW = Object.getOwnPropertyDescriptor(P, 'clientWidth');
    Object.defineProperty(P, 'clientWidth', { configurable: true, get() { return 1500; } });
    window.getComputedStyle = (el) => (el.classList && el.classList.contains('panels') ? { gridTemplateColumns: '500px 500px 500px' } : gcs(el));
    try {
      load();
      stubFetch(routes());
      await enter();
      window.mgPaint();
      const span = (c) => document.querySelector('[data-card="' + c + '"]').style.gridColumn;
      expect([span('mgr-services'), span('mgr-db')]).toEqual(['', '']);
      expect([span('mgr-perf'), span('mgr-ae-perf'), span('mgr-influx-perf')]).toEqual(['', '', '']);
      expect(span('mgr-tower')).toBe('span 3');
      expect(srcFile('css/hostpage.css')).toContain('.mg-dbs { grid-template-columns: minmax(0, 1fr); }');
    } finally {
      window.getComputedStyle = gcs;
      if (rootW) Object.defineProperty(P, 'clientWidth', rootW); else delete P.clientWidth;
    }
  });

  test('the streams kv has 10 entries, warn values in bad and the badge as the note', async () => {
    load();
    const r = routes();
    r.stats = STATS({ pool: { active: 8, limit: 8, peak: 8, refusals: 0 } });
    stubFetch(r);
    await enter();
    const rows = [...$('mg-b-streams').querySelectorAll('.mg-meters li')];
    expect(rows).toHaveLength(10);
    expect(rows[0].querySelector('.r-lab').textContent).toBe('Pool active');
    expect(rows[0].querySelector('.r-bar i').style.width).toBe('100%');
    expect($('mg-b-streams').querySelector('li.mg-over .r-val').textContent).toBe('8 / 8');
    expect(noteOf('mgr-streams')).toBe('saturated');
  });

  test('the Host streams table has one row per host across /api/agents and the stream stats, and the footnote', async () => {
    load();
    stubFetch(routes());
    await enter();
    const rows = bodyRows('mg-b-hosts');
    expect(rows.map(x => x[0])).toEqual(['beta', 'gamma', 'mgr-host']);
    expect(rows[0].slice(1, 4)).toEqual(['stale', '2m ago', '1/4']);
    expect($('mg-b-hosts').textContent).toContain('Pushed to the alarm engine as manager_streams / agent_streams — add rules in Events and metrics to alert on these.');
  });

  test('the Processes panel lists the manager host services', async () => {
    load();
    stubFetch(routes());
    await enter();
    expect(bodyRows('mg-b-procs').map(r => r[0])).toEqual(['manager', 'alarm-engine', 'influxdb']);
  });

  test('seeded perf points draw, a live probe value appends one point, and the notes read the probes', async () => {
    load();
    const r = routes();
    stubFetch(r);
    await enter();
    const ts = () => kitTsData($('mg-b-perf').querySelector('.ts[data-kit-ts]'));
    expect(ts().labels).toHaveLength(5);
    expect(ts().series.map(s => s.name)).toEqual(['API', 'History']);
    expect(kitTsData($('mg-b-ae-perf').querySelector('.ts[data-kit-ts]')).series.map(s => s.slot)).toEqual([1, 2, 3, 4]);
    expect(kitTsData($('mg-b-influx-perf').querySelector('.ts[data-kit-ts]')).series.map(s => s.slot)).toEqual([1, 2, 3]);
    expect(noteOf('mgr-perf')).toBe('api 12.0 ms · history 40.0 ms · open routes —');
    expect(noteOf('mgr-ae-perf')).toBe('worst 8.0 ms');
    const redraw = vi.spyOn(window, 'kitRedrawTs');
    const appended = () => redraw.mock.calls.map(c => c[1].id).filter(id => /^mg-ts-/.test(id));
    r.self = SELFMON(30, 0);
    await window.fetchServicesAndInflux();
    await settle();
    expect(appended()).toEqual(['mg-ts-perf', 'mg-ts-ae-perf']);
    expect(ts().labels).toHaveLength(6);
    expect(ts().series[0].values[5]).toBe(30);
    // The same probe sample again appends nothing.
    await window.fetchServicesAndInflux();
    await settle();
    expect(ts().labels).toHaveLength(6);
    expect(window._mgPerf.mgr).toHaveLength(2);
  });

  test('probes on different cadences: a series with no new sample gets a gap, and the append needs no redraw', async () => {
    load();
    const r = routes();
    stubFetch(r);
    await enter();
    const ts = () => kitTsData($('mg-b-perf').querySelector('.ts[data-kit-ts]'));
    const fills = vi.spyOn(window, 'kitFill');
    const redraw = vi.spyOn(window, 'kitRedrawTs');
    // API has a new sample; History's is as old as its last seeded point.
    r.self = [row('manager_self_monitor', 'mgr-host', 'manager_api_latency_ms', 30, 0),
      row('manager_self_monitor', 'mgr-host', 'manager_history_latency_ms', 40, 60)];
    await window.fetchServicesAndInflux();
    await settle();
    expect(redraw.mock.calls.filter(c => c[1].id === 'mg-ts-perf')).toHaveLength(1);
    expect(ts().labels).toHaveLength(6);
    expect(ts().series[0].values[5]).toBe(30);
    expect(ts().series[1].values[5]).toBeNull();
    expect(window._mgPerf.mgr[1]).toHaveLength(5);
    // The appended point matches what a full draw gives, so the paint after it redraws nothing.
    expect(ts().labels).toEqual(MV.perfSeries(window._mgPerf.mgr, 'mgr').labels);
    expect(fills.mock.calls.filter(c => c[0] === 'mg-b-perf')).toHaveLength(0);
    window.mgPaint();
    expect(fills.mock.calls.filter(c => c[0] === 'mg-b-perf')).toHaveLength(0);
  });

  test('a live point appends without a full draw when the oldest seeded point passes an hour', async () => {
    load();
    const r = routes();
    r.points = (name) => [59, 50, 40, 30, 20].map(m => ({ timestamp: iso(m * 60), value: name === 'manager_api_latency_ms' ? 10 : 20 }));
    stubFetch(r);
    await enter();
    const ts = () => kitTsData($('mg-b-perf').querySelector('.ts[data-kit-ts]'));
    expect(ts().labels).toHaveLength(6);
    const fills = vi.spyOn(window, 'kitFill');
    vi.spyOn(Date, 'now').mockReturnValue(NOW + 120000);
    r.self = SELFMON(30, 0);
    await window.fetchServicesAndInflux();
    await settle();
    expect(window._mgPerf.mgr[0]).toHaveLength(6);
    expect(window._mgPerf.mgr[0][0].timestamp).toBe(iso(50 * 60));
    expect(ts().labels).toEqual(MV.perfSeries(window._mgPerf.mgr, 'mgr').labels);
    expect(ts().series[0].values[5]).toBe(30);
    expect(fills.mock.calls.filter(c => c[0] === 'mg-b-perf')).toHaveLength(0);
  });

  test('each perf chart menu lists View … in metrics for its stored series', async () => {
    load();
    stubFetch(routes());
    await enter();
    const items = (card) => [...document.querySelectorAll(`[data-card="${card}"] .p-pop [data-mg-metric]`)]
      .map(b => [b.textContent, b.getAttribute('data-mg-metric')]);
    expect(items('mgr-perf')).toEqual([['View Manager API in metrics', 'manager_self_monitor/manager_api_latency_ms'],
      ['View Manager history in metrics', 'manager_self_monitor/manager_history_latency_ms']]);
    expect(items('mgr-ae-perf').map(i => i[0])).toEqual(['View Alarm engine health in metrics', 'View Alarm engine ingest in metrics',
      'View Alarm engine query, 24 h in metrics', 'View Rule check cycle in metrics']);
    expect(items('mgr-influx-perf').map(i => i[0])).toEqual(['View InfluxDB write in metrics', 'View InfluxDB query, 5 min in metrics',
      'View InfluxDB query, 24 h in metrics']);
    window.focusAlarmMetric = vi.fn();
    document.querySelector('[data-card="mgr-perf"] .p-pop [data-mg-metric]').click();
    expect(window.focusAlarmMetric).toHaveBeenCalledWith('mgr-host', 'manager_self_monitor', 'manager_api_latency_ms');
    delete window.focusAlarmMetric;
  });

  test('Tower paints from one fetch and refetches at most once a minute', async () => {
    load();
    const { calls } = stubFetch(routes());
    await enter();
    expect($('mg-b-tower').textContent).not.toContain('Loading');
    window.mgEnter();
    await window.fetchServicesAndInflux();
    await settle();
    expect(calls.filter(u => u === '/api/tower/state')).toHaveLength(1);
  });

  test('the perf note keeps the open-routes hover text', async () => {
    load();
    stubFetch(routes());
    await enter();
    expect(document.querySelector('[data-card="mgr-perf"] .p-hd .p-note').getAttribute('title'))
      .toBe('Requests on the pages and routes that need no login or token, last full minute');
  });

  test('a failed stream poll after a good one keeps the sample and warns its age; with no sample it says it failed', async () => {
    load();
    const r = routes();
    stubFetch(r);
    await enter();
    r.stats = FAIL;
    await window.fetchManagerStreamsCard();
    const n = document.querySelector('[data-card="mgr-streams"] .p-hd .p-note');
    expect(n.textContent).toMatch(/^last sample \d+s ago$/);
    expect(n.classList.contains('mg-warn')).toBe(true);
    expect($('mg-b-streams').querySelectorAll('.mg-meters li')).toHaveLength(10);
    r.stats = STATS();
    await window.fetchManagerStreamsCard();
    expect(n.textContent).toBe('ok');
    load();
    const f = routes();
    f.stats = FAIL;
    stubFetch(f);
    await enter();
    expect($('mg-b-streams').textContent).toBe('Could not load stream stats.');
  });

  test('a failed or empty catalog after a good one keeps the last services', async () => {
    load();
    const r = routes();
    stubFetch(r);
    await enter();
    r.processes = FAIL; r.self = FAIL; r.influx = [];
    await window.fetchServicesAndInflux();
    await settle();
    expect(bodyRows('mg-b-services')).toHaveLength(4);
    expect(bodyRows('mg-b-procs')).toHaveLength(3);
  });

  test('on Overall with nothing of the Manager page pinned, nothing is fetched for it', async () => {
    load({ tab: 'overall' });
    const { calls } = stubFetch(routes());
    await window.fetchServicesAndInflux();
    await window.fetchManagerAgentsCard();
    await window.fetchManagerStreamsCard();
    await settle();
    expect(calls).toEqual([]);
  });

  test('a perf chart fits its row on the page, and pinned on Overall draws at the base height and its holder\'s width', async () => {
    load();
    const r = routes();
    stubFetch(r);
    await enter();
    const ts = () => kitTsData($('mg-b-ae-perf').querySelector('.ts[data-kit-ts]'));
    // A laid-out page: the Manager latency panel is the taller one on the Performance line.
    Object.defineProperty($('mg-root'), 'clientWidth', { configurable: true, get: () => 1000 });
    const box = (id, h) => { document.querySelector(`[data-card="${id}"]`).getBoundingClientRect = () => ({ top: 100, bottom: 100 + h, height: h }); };
    box('mgr-perf', 400);
    box('mgr-ae-perf', 200);
    window.mgRefit(document);
    expect(ts().height).toBe(370);
    // Pinned on Overall, a redraw uses the base height and the holder's width.
    window.layout = { overallBorrowed: ['mgr-ae-perf'] };
    $('overallGrid').innerHTML = '<div class="card ov-shell" data-card="ov-borrow-mgr-ae-perf"></div>';
    window._activeTab = 'overall';
    window.adoptPinnedCards();
    expect(document.querySelector('#overallGrid [data-card="mgr-ae-perf"]')).not.toBeNull();
    Object.defineProperty($('mg-b-ae-perf'), 'clientWidth', { configurable: true, get: () => 420 });
    window.mgPaint();
    expect(ts().w).toBe(420);
    expect(ts().height).toBe(170);
    Object.defineProperty($('mg-b-ae-perf'), 'clientWidth', { configurable: true, get: () => 380 });
    window.mgPaint();
    expect(ts().w).toBe(380);
  });

  test('a failed first Tower fetch ends Loading… and paints the empty Tower body', async () => {
    load();
    const r = routes();
    r.tower = FAIL;
    r.insights = FAIL;
    stubFetch(r);
    await enter();
    expect(() => window.mgPaint()).not.toThrow();
    const body = document.createElement('div');
    body.innerHTML = window.ovTowerBody(null, null).html;
    expect($('mg-b-tower').innerHTML).toBe(body.innerHTML);
    expect($('mg-b-tower').textContent).not.toContain('Loading');
  });

  test('with no manager host the System row says so and nothing is fetched for a host', async () => {
    load({ mgr: null });
    const { calls } = stubFetch(routes());
    await enter();
    const bodies = [...document.querySelectorAll('#hp-mgr .p-body')];
    expect(bodies).toHaveLength(6);
    for (const b of bodies) expect(b.textContent).toBe('No host reported for the manager yet.');
    expect(calls.some(u => u.startsWith('/api/history?agent='))).toBe(false);
    expect(calls.some(u => /^\/api\/agents\/[^/]+\/system$/.test(u))).toBe(false);
    expect(bodyRows('mg-b-services')).toHaveLength(4);
  });

  test('an old pinned layout maps once, saves once, and the pinned Connection health repaints on Overall', async () => {
    const saved = { overallBorrowed: ['services', 'mgr-streams', 'mgr-agents', 'mgr-perf-summary', 'mgr-ram'], hostPageMapped: 1, lmsMacPowerShown: 1 };
    load({ tab: 'overall' });
    const r = routes();
    const { posts, calls } = stubFetch(r, saved);
    await window.loadLayout();
    await settle();
    expect(window.layout.overallBorrowed).toEqual(['mgr-services', 'mgr-streams', 'mgr-hosts', 'mgr-tiles', 'mgr-cpu-mem']);
    expect(posts).toHaveLength(1);
    window.adoptPinnedCards();
    window._ovBackfillPinnedProviders();
    await settle();
    const pinned = () => document.querySelector('#overallGrid [data-card="ov-borrow-mgr-streams"] section.panel[data-card="mgr-streams"]');
    expect(pinned()).not.toBeNull();
    // Connection health pinned alone still fetches the host list for the stream view.
    expect(calls).toContain('/api/agents');
    expect(document.querySelector('#overallGrid [data-card="ov-borrow-mgr-cpu-mem"] section.panel[data-card="mgr-cpu-mem"]')).not.toBeNull();
    expect(pinned().querySelector('.p-body').textContent).toContain('2 / 8');
    r.stats = STATS({ pool: { active: 5, limit: 8, peak: 5, refusals: 0 } });
    await window.fetchManagerStreamsCard();
    await settle();
    expect(pinned().querySelector('.p-body').textContent).toContain('5 / 8');
    // A mapped layout loads again without another save.
    const again = stubFetch(r, window.layout);
    await window.loadLayout();
    await settle();
    expect(again.posts).toHaveLength(0);
  });

  test('the drawer lists both roots\' rows under the Manager page', () => {
    expect(SettingsLib.settingsScope('dashboard', 'manager')).toMatchObject({ kind: 'rows', rowsHidden: 'managerRowsHidden', root: 'dash-manager' });
    expect(Object.keys(SettingsLib.CARD_PAGES)).toEqual(['overall']);
    for (const [id, role] of [['mgr-services', 'table'], ['mgr-hosts', 'table'], ['mgr-procs', 'table'], ['mgr-perf', 'chart'],
      ['mgr-ae-perf', 'chart'], ['mgr-db', 'stats'], ['mgr-streams', 'stats'], ['mgr-tiles', 'stats']]) {
      expect(SettingsLib.roleOf(id), id).toBe(role);
      expect(SettingsLib.roleOf('ov-borrow-' + id), id).toBe(role);
    }
    for (const id of ['services', 'influxdb', 'mgr-agents', 'mgr-ram', 'mgr-perf-summary', 'ae-perf']) expect(SettingsLib.CARD_ROLES[id], id).toBeUndefined();
  });

  test('.tb-ah shows on the Manager page', () => {
    const doc = new JSDOM(indexSrc).window.document;
    doc.querySelectorAll('body script').forEach(s => s.remove());
    const w = runHarness({
      sources: ['window.mcLayoutReady = new Promise(() => {});', srcFile('kit/kit.js'), srcFile('js/shell.js')],
      bodyHtml: doc.body.innerHTML,
    });
    const bar = w.document.querySelector('#toolbar .tb-ah');
    w.mcShellMark('dashboard', 'manager');
    expect(bar.hidden).toBe(false);
    w.mcShellMark('dashboard', 'energy');
    expect(bar.hidden).toBe(true);
    w.close();
  });
});
