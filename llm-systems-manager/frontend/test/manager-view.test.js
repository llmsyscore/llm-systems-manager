// #1432 phase 4d: Manager page transforms in js/lib/manager-view.js.
// Catalog rows carry ISO latest_timestamp, as /api/alarm/metrics returns them.
import { describe, it, expect } from 'vitest';
import MV from '../js/lib/manager-view.js';
import LMSeries from '../js/lib/series.js';

const NOW_MS = Date.parse('2026-11-17T12:00:00Z');
const NOW_S = NOW_MS / 1000;
const iso = (sAgo) => new Date(NOW_MS - sAgo * 1000).toISOString();
// Catalog rows of one watched service on a host, all sampled sAgo seconds ago.
function svc(host, name, fields, sAgo) {
  return Object.entries(fields).map(([f, v]) => ({
    source: 'processes', hostname: host, metric_name: name + '_' + f, latest_value: v, latest_timestamp: iso(sAgo),
  }));
}

describe('services', () => {
  const rows = [].concat(
    svc('mgr', 'manager', { running: 1, pid: 101.2, rss_mb: 210.4, uptime_s: 7300 }, 10),
    svc('mgr', 'alarm-engine', { running: 1, pid: 102, rss_mb: 90, uptime_s: 30 }, 120),
    svc('beta', 'llama-server', { running: 1, pid: 7 }, 5),
    svc('beta', 'gone', { running: 1 }, 400),
    [{ source: 'influxdb', metric_name: 'up', latest_value: 1, latest_timestamp: iso(1) }],
  );
  it('labels running and stale rows, sorts by host then service, hides old rows in the note', () => {
    const s = MV.services(rows, NOW_S);
    expect(s.rows).toEqual([
      ['beta', 'llama-server', 'running', '7', '—', '—'],
      ['mgr', 'alarm-engine', 'stale', '102', '90 MB', '30s'],
      ['mgr', 'manager', 'running', '101', '210 MB', '2h'],
    ]);
    expect(s.up).toBe(3);
    expect(s.total).toBe(3);
    expect(s.note).toBe('3/3 up · 1 stale hidden');
    expect(s.tone).toBe('ok');
  });
  it('a stopped fresh service is bad; nothing reporting is off', () => {
    const down = rows.concat(svc('beta', 'vllm', { running: 0 }, 3));
    const s = MV.services(down, NOW_S);
    expect(s.rows.find(r => r[1] === 'vllm')[2]).toBe('stopped');
    expect(s.tone).toBe('bad');
    expect(s.note).toBe('3/4 up · 1 stale hidden');
    const none = MV.services([], NOW_S);
    expect(none).toMatchObject({ rows: [], up: 0, total: 0, tone: 'off', note: '0/0 up' });
    expect(MV.services(svc('h', 'x', { running: 1 }, 100), NOW_S).tone).toBe('warn');
  });
});

describe('processes', () => {
  it('lists the manager host services that reported in the last 300 s', () => {
    const rows = [].concat(
      svc('mgr', 'manager', { running: 1, pid: 1, rss_mb: 10, uptime_s: 90000 }, 5),
      svc('mgr', 'influxdb', { running: 0, pid: 3 }, 20),
      svc('mgr', 'llm-systems-agent', { running: 1 }, 200),
      svc('mgr', 'alarm-engine', { running: 1 }, 301),
      svc('other', 'manager-ish', { running: 1 }, 5),
    );
    expect(MV.processes(rows, NOW_S)).toEqual([
      ['manager', 'running', '1', '10 MB', '1d'],
      ['influxdb', 'stopped', '3', '—', '—'],
      ['llm-systems-agent', 'stale', '—', '—', '—'],
    ]);
    expect(MV.processes([], NOW_S)).toEqual([]);
  });
  it('marks a process stale after 90 s, as the services table does', () => {
    const rows = [].concat(
      svc('mgr', 'manager', { running: 1, pid: 1 }, 60),
      svc('mgr', 'alarm-engine', { running: 1, pid: 2 }, 89),
      svc('mgr', 'influxdb', { running: 1, pid: 3 }, 120),
    );
    expect(MV.processes(rows, NOW_S).map(r => r.slice(0, 2))).toEqual([['manager', 'running'], ['alarm-engine', 'running'], ['influxdb', 'stale']]);
    expect(MV.services(rows, NOW_S).rows.map(r => r[2])).toEqual(['running', 'stale', 'running']);
  });
});

describe('databases', () => {
  const influx = { ping_ms: { value: 1.234 }, query_ms: { value: 20 }, write_ms: { value: 3 }, write_ok_rate: { value: 0.9951 },
    bytes_on_disk: { value: 1048576 * 300 }, cardinality_metrics: { value: 1234.4 } };
  const mgrDb = { manager_db: { size_bytes: 4096, wal_size_bytes: 1024, query_ms: 0.5, benchmarks: 2, report_cards: 3, tool_runs: 4 },
    audit_db: { size_bytes: 8192, audit_rows: 12000 }, energy_db: { size_bytes: 2048, energy_rows: 5 } };
  const aeDb = { alarms_db: { alerts: 3, alert_history: 7, size_bytes: 10240, wal_size_bytes: 512, page_count: 10, query_ms: 1.5 },
    settings_db: { rules: 1, channels: 2, configs: 3, size_bytes: 2048, page_count: 5, deliveries: 9, query_ms: 0.25 } };
  it('gives the 22 values in three groups with the old labels', () => {
    const d = MV.databases(influx, mgrDb, aeDb);
    expect(d).toHaveLength(22);
    const groups = [...new Set(d.map(r => r[0]))];
    expect(groups).toEqual(['InfluxDB', 'Manager SQLite', 'Alarm engine SQLite']);
    expect(d.filter(r => r[0] === 'InfluxDB')).toHaveLength(6);
    expect(d.filter(r => r[0] === 'Manager SQLite')).toHaveLength(8);
    expect(d.filter(r => r[0] === 'Alarm engine SQLite')).toHaveLength(8);
    expect(d.slice(0, 6)).toEqual([
      ['InfluxDB', 'Ping ms', '1.2'], ['InfluxDB', 'Query ms', '20.0'], ['InfluxDB', 'Write ms', '3.0'],
      ['InfluxDB', 'Write OK %', '99.5'], ['InfluxDB', 'On disk', '300 MB'], ['InfluxDB', 'Metrics series', '1234'],
    ]);
    expect(d.slice(6, 14).map(r => r[1])).toEqual(['manager.db', 'audit.db', 'energy.db', 'WAL total', 'Audit rows',
      'Energy rows', 'Bench + tool runs', 'SELECT 1 ms']);
    expect(d.slice(6, 14).map(r => r[2])).toEqual(['4.0 KB', '8.0 KB', '2.0 KB', '1.0 KB', (12000).toLocaleString(), '5', '9', '0.50']);
    expect(d.slice(14).map(r => r[1])).toEqual(['Alerts rows', 'Settings rows', 'Alarms DB', 'Settings DB', 'WAL total',
      'Pages (a+s)', 'SELECT 1 ms', 'Deliveries']);
    expect(d.slice(14).map(r => r[2])).toEqual(['10', '6', '10 KB', '2.0 KB', '512 B', '15', '1.50', '9']);
  });
  it('missing values read a dash', () => {
    const d = MV.databases(null, null, null);
    expect(d).toHaveLength(22);
    expect(d.every(r => r[2] === '—')).toBe(true);
  });
  it('database size adds Influx on disk and the SQLite files', () => {
    expect(MV.dbTotalBytes(influx, mgrDb, aeDb)).toBe(1048576 * 300 + 4096 + 8192 + 2048 + 1024 + 10240 + 2048 + 512);
    expect(MV.dbTotalBytes({}, { audit_db: { size_bytes: 10 } }, {})).toBe(10);
    expect(MV.dbTotalBytes(null, null, null)).toBeNull();
  });
});

describe('influxState', () => {
  const at = (sAgo) => NOW_MS - sAgo * 1000;
  it('reads up, down and no data from the up metric, no data after 90 s', () => {
    expect(MV.influxState({ up: { value: 1, at: at(10) } }, NOW_MS)).toEqual({ text: 'up', tone: 'ok' });
    expect(MV.influxState({ up: { value: 0, at: at(10) } }, NOW_MS)).toEqual({ text: 'down', tone: 'bad' });
    expect(MV.influxState({ up: { value: 1, at: at(91) } }, NOW_MS)).toEqual({ text: 'no data', tone: 'off' });
    expect(MV.influxState({ up: { value: 1 } }, NOW_MS)).toEqual({ text: 'no data', tone: 'off' });
    expect(MV.influxState(null, NOW_MS)).toEqual({ text: 'no data', tone: 'off' });
  });
  it('warns when ping is over 100 ms or query over 500 ms', () => {
    const up = { value: 1, at: at(5) };
    expect(MV.influxState({ up, ping_ms: { value: 101 } }, NOW_MS).tone).toBe('warn');
    expect(MV.influxState({ up, query_ms: { value: 501 } }, NOW_MS).tone).toBe('warn');
    expect(MV.influxState({ up, ping_ms: { value: 100 }, query_ms: { value: 500 } }, NOW_MS).tone).toBe('ok');
  });
});

describe('streams', () => {
  const stats = {
    pool: { active: 4, limit: 4, peak: 4, refusals: 2 }, worker_threads_busy: 3, worker_threads: 16, worker_backlog: 0,
    browser_connections: 2, agent_connections: 5, sse_daemon_running: true, sse_daemon_streams: 1,
    anon_requests_per_min: 12, anon_budget_per_min: 60, anon_refused_per_min: 0,
    agents: [
      { agent_id: 'a1', hostname: 'alpha', reachable: true, active: 1, cap: 4, peak: 2, refusals: 0, terminal_sessions: 1 },
      { agent_id: 'b1', hostname: 'beta', reachable: false },
      { hostname: 'gamma', reachable: true, active: 0, cap: 2, peak: 0, refusals: 1, terminal_sessions: 0 },
    ],
  };
  const agents = { agents: [
    { agent_id: 'a1', hostname: 'alpha', status: 'approved', liveness: 'live', last_heartbeat: iso(5) },
    { agent_id: 'b1', hostname: 'beta', status: 'approved', liveness: 'down', last_heartbeat: iso(7200) },
    { agent_id: 'd1', hostname: 'delta', status: 'pending', last_heartbeat: null },
  ] };
  it('gives the ten summary cells with the old labels and warn rules', () => {
    const s = MV.streams(stats, agents, null, NOW_MS);
    expect(s.kv).toHaveLength(10);
    expect(s.kv.map(r => r[0])).toEqual(['Pool active', 'Peak (boot)', 'Refusals (boot)', 'Workers busy', 'Worker backlog',
      'Browser connections', 'Host connections', 'Off-pool SSE', 'Open routes', 'Refused (open routes)']);
    expect(s.kv[0]).toEqual(['Pool active', '4 / 4', true, 1]);
    expect(s.kv[3]).toEqual(['Workers busy', '3 / 16', false, 3 / 16]);
    expect(s.kv[7]).toEqual(['Off-pool SSE', '1 active', false, null]);
    expect(s.kv[8]).toEqual(['Open routes', '12 /min of 60', false, 0.2]);
    expect(s.badge).toEqual({ text: 'saturated', tone: 'bad' });
    const free = MV.streams(Object.assign({}, stats, { pool: { active: 1, limit: 4 } }), agents, null, NOW_MS);
    expect(free.kv[0][2]).toBe(false);
    expect(free.badge).toEqual({ text: 'ok', tone: 'ok' });
    const flood = MV.streams(Object.assign({}, stats, { pool: { active: 1, limit: 4 }, anon_refused_per_min: 3 }), agents, null, NOW_MS);
    expect(flood.badge).toEqual({ text: 'flooded', tone: 'warn' });
    expect(flood.kv[9]).toEqual(['Refused (open routes)', 3, true, 0.05]);
    const bare = MV.streams({ pool: { active: 2 } }, agents, null, NOW_MS);
    expect(bare.kv[0][3]).toBeNull();
    expect(bare.kv[8][3]).toBeNull();
  });
  it('merges hosts from both sources by id, then hostname', () => {
    const s = MV.streams(stats, agents, null, NOW_MS);
    expect(s.hosts.cols).toEqual(['Host', 'Status', 'Last sample', 'Streams', 'Peak', 'Refusals', 'Terminals']);
    expect(s.hosts.rows).toEqual([
      ['alpha', 'live', '5s ago', '1/4', 2, 0, 1],
      ['beta', 'down', '2h ago', 'unreachable', '—', '—', '—'],
      ['delta', 'pending', '—', '—', '—', '—', '—'],
      ['gamma', '—', '—', '0/2', 0, 1, 0],
    ]);
  });
  it('a host listed without an agent id merges into its stats row by hostname', () => {
    const s = MV.streams({ pool: {}, agents: [{ agent_id: 'e1', hostname: 'echo', reachable: true, active: 1, cap: 2, peak: 1, refusals: 0, terminal_sessions: 0 }] },
      [{ hostname: 'echo', status: 'approved', liveness: 'live', last_heartbeat: iso(3) }], null, NOW_MS);
    expect(s.hosts.rows).toEqual([['echo', 'live', '3s ago', '1/2', 1, 0, 0]]);
    expect(s.tracks.agents.e1.count).toBe(0);
  });
  it('a heartbeat ahead of the browser clock reads "just now"', () => {
    const s = MV.streams({ pool: {} }, [{ agent_id: 'f1', hostname: 'fox', status: 'approved', liveness: 'live', last_heartbeat: iso(-14) }], null, NOW_MS);
    expect(s.hosts.rows[0][2]).toBe('just now');
  });
  it('tracks refusals across polls and returns the new tracks', () => {
    const first = MV.streams(stats, agents, null, NOW_MS);
    expect(first.tracks.pool.count).toBe(2);
    expect(first.tracks.agents.a1.count).toBe(0);
    expect(first.tracks.agents.gamma.count).toBe(1);
    expect('b1' in first.tracks.agents).toBe(false);
    const more = Object.assign({}, stats, { pool: { active: 1, limit: 4, refusals: 5 } });
    const second = MV.streams(more, agents, first.tracks, NOW_MS + 1000);
    expect(second.tracks.pool).toEqual(LMSeries.trackRefusals(first.tracks.pool, 5, NOW_MS + 1000));
    expect(second.kv[2]).toEqual(['Refusals (boot)', 5, true, null]);
    expect(second.badge).toEqual({ text: 'refusing', tone: 'warn' });
  });
  it('a full host and recent host refusals read as text in their cells', () => {
    const at = (active, refusals) => Object.assign({}, stats, { agents: [
      { agent_id: 'a1', hostname: 'alpha', reachable: true, active, cap: 4, peak: 4, refusals, terminal_sessions: 0 }] });
    const first = MV.streams(at(4, 2), agents, null, NOW_MS);
    const alpha = r => r.hosts.rows.find(x => x[0] === 'alpha');
    expect(alpha(first)[3]).toBe('4/4 · full');
    expect(alpha(first)[5]).toBe(2);
    const second = MV.streams(at(3, 3), agents, first.tracks, NOW_MS + 1000);
    expect(alpha(second)[3]).toBe('3/4');
    expect(alpha(second)[5]).toBe('3 · recent');
    const later = MV.streams(at(3, 3), agents, second.tracks, NOW_MS + 120000);
    expect(alpha(later)[5]).toBe(3);
  });
  it('copes with no stats and no agents', () => {
    const s = MV.streams(null, null, null, NOW_MS);
    expect(s.kv).toHaveLength(10);
    expect(s.hosts.rows).toEqual([]);
    expect(MV.streams(null, agents, null, NOW_MS).hosts.rows.map(r => r[3])).toEqual(['—', '—', '—']);
  });
});

describe('probes', () => {
  const mk = (vals, ages) => [k => vals[k], k => (ages && k in ages ? ages[k] : 10)];
  it('picks the worst fresh probe and skips stale ones', () => {
    const [get, age] = mk({ manager_api_latency_ms: 40, ae_ingest_latency_ms: 900, influx_write_latency_ms: 120 },
      { ae_ingest_latency_ms: 181, manager_api_latency_ms: 4 });
    const p = MV.probes(get, age);
    expect(p.worst).toEqual({ name: 'InfluxDB write', ms: 120 });
    expect(p.freshAgeS).toBe(4);
    expect(p.api).toBe(40);
    expect(p.ae.ae_ingest).toBe(900);
    expect(Object.keys(p.ae)).toEqual(['ae_health', 'ae_ingest', 'ae_query_24h', 'rule_eval_cycle']);
    expect(p.influx).toEqual({ influx_write: 120, influx_query_5m: null, influx_query_24h: null });
    expect(p.tone.summary).toBe('warn');
  });
  it('tones: 99 ok, 100 warn, 500 bad, nothing off', () => {
    const tone = ms => MV.probes(...mk({ manager_api_latency_ms: ms, ae_health_latency_ms: ms, influx_write_latency_ms: ms })).tone;
    expect(tone(99)).toEqual({ summary: 'ok', mgr: 'ok', ae: 'ok', influx: 'ok' });
    expect(tone(100)).toEqual({ summary: 'warn', mgr: 'warn', ae: 'warn', influx: 'warn' });
    expect(tone(500)).toEqual({ summary: 'bad', mgr: 'bad', ae: 'bad', influx: 'bad' });
    const none = MV.probes(() => undefined, () => undefined);
    expect(none.worst).toBeNull();
    expect(none.freshAgeS).toBeNull();
    expect(none.tone).toEqual({ summary: 'off', mgr: 'off', ae: 'off', influx: 'off' });
  });
  it('open-route counts only while fresh', () => {
    const [get, age] = mk({ manager_anon_requests_per_min: 12.4, manager_anon_refused_per_min: 2 });
    expect(MV.probes(get, age).anon).toEqual({ requests: 12, refused: 2 });
    expect(MV.probes(get, () => 200).anon).toBeNull();
  });
});

describe('tiles', () => {
  it('gives the five tiles', () => {
    const stats = { browser_connections: 3, agent_connections: 7, pool: { active: 4, limit: 4, peak: 4 },
      worker_threads_busy: 2, worker_threads: 16, worker_backlog: 1 };
    const probes = { worst: { name: 'Alarm engine ingest', ms: 12.34 }, freshAgeS: 41.6, tone: { summary: 'ok' } };
    const t = MV.tiles({ stats, probes, dbBytes: 1048576 * 5 });
    expect(t.map(x => x.label)).toEqual(['Browsers connected', 'Streams', 'Workers busy', 'Database size', 'Worst probe']);
    expect(t[0]).toMatchObject({ value: '3', sub: '7 host links' });
    expect(t[1]).toMatchObject({ value: '4 / 4', sub: 'peak 4', tone: 'bad' });
    expect(t[2]).toMatchObject({ value: '2 / 16', sub: 'backlog 1', tone: 'warn' });
    expect(t[3]).toMatchObject({ value: '5.0 MB' });
    expect(t[4]).toMatchObject({ value: '12.3 ms', sub: 'Alarm engine ingest · 42s ago', tone: 'ok' });
  });
  it('degrades to dashes', () => {
    const t = MV.tiles({});
    expect(t.map(x => x.value)).toEqual(['—', '— / —', '— / —', '—', '—']);
    expect(t[4].sub).toBe('no recent samples');
    const ahead = MV.tiles({ probes: { worst: { name: 'Alarm engine health', ms: 3 }, freshAgeS: -9, tone: { summary: 'ok' } } });
    expect(ahead[4].sub).toBe('Alarm engine health · just now');
    expect(t[1].tone).toBe('');
    expect(t[2].tone).toBe('');
  });
});

describe('perfSeries', () => {
  const pts = (vals) => vals.map((v, i) => ({ timestamp: iso(60 * (vals.length - i)), value: v }));
  it('manager: API slot 1, History slot 2, aligned by timestamp', () => {
    const p = MV.perfSeries([pts([1, 2, 3]), pts([4, 5])], 'mgr');
    expect(p.labels).toHaveLength(3);
    expect(p.series.map(s => [s.name, s.slot])).toEqual([['API', 1], ['History', 2]]);
    expect(p.series[0].values).toEqual([1, 2, 3]);
    expect(p.series[1].values).toEqual([null, 4, 5]);
  });
  it('alarm engine and InfluxDB charts: their own series, each in its own colour', () => {
    const ae = MV.perfSeries(Array.from({ length: 4 }, (_, i) => pts([i])), 'ae');
    expect(ae.series.map(s => [s.name, s.slot])).toEqual([['Health', 1], ['Ingest', 2], ['Query, 24 h', 3], ['Rule check cycle', 4]]);
    expect(ae.labels).toHaveLength(1);
    const fx = MV.perfSeries(Array.from({ length: 3 }, (_, i) => pts([i])), 'influx');
    expect(fx.series.map(s => [s.name, s.slot])).toEqual([['Write', 1], ['Query, 5 min', 2], ['Query, 24 h', 3]]);
  });
  it('no points give no labels', () => {
    expect(MV.perfSeries([[], []], 'mgr')).toEqual({ labels: [], series: [
      { name: 'API', values: [], slot: 1 }, { name: 'History', values: [], slot: 2 }] });
  });
});

describe('mapLegacyLayout', () => {
  it('maps pinned manager cards once and dedupes', () => {
    const lay = {
      overallBorrowed: ['services', 'mgr-streams', 'mgr-agents', 'mgr-perf-summary', 'mgr-ram'],
      overallOrder: ['ov-tiles', 'ov-borrow-services', 'ov-borrow-mgr-agents', 'ov-borrow-mgr-streams', 'ov-borrow-llama-tps'],
      cardSizes: { 'ov-borrow-services': 'w2', 'mgr-network': 'w1', 'ae-perf': 'w3', 'llama-tps': 'w2' },
      managerOrder: ['services'], managerHidden: ['influxdb'], managerCols: 3,
    };
    expect(MV.mapLegacyLayout(lay)).toEqual({ changed: true });
    expect(lay.overallBorrowed).toEqual(['mgr-services', 'mgr-streams', 'mgr-hosts', 'mgr-tiles', 'mgr-cpu-mem']);
    expect(lay.overallOrder).toEqual(['ov-tiles', 'ov-borrow-mgr-services', 'ov-borrow-mgr-hosts', 'ov-borrow-mgr-streams', 'ov-borrow-llama-tps']);
    expect(lay.cardSizes).toEqual({ 'ov-borrow-mgr-services': 'w2', 'mgr-net': 'w1', 'mgr-ae-perf': 'w3', 'llama-tps': 'w2' });
    for (const k of ['managerOrder', 'managerHidden', 'managerCols']) expect(k in lay, k).toBe(false);
    expect(lay.managerPageMapped).toBe(1);
    const snap = JSON.stringify(lay);
    expect(MV.mapLegacyLayout(lay)).toEqual({ changed: false });
    expect(JSON.stringify(lay)).toBe(snap);
  });
  it('lists every old manager card id', () => {
    expect(MV.LEGACY_IDS).toEqual({
      services: 'mgr-services', influxdb: 'mgr-db', 'mgr-agents': 'mgr-hosts', 'mgr-streams': 'mgr-streams',
      'mgr-ram': 'mgr-cpu-mem', 'mgr-disk': 'mgr-disk', 'mgr-network': 'mgr-net', 'mgr-processes': 'mgr-procs',
      'mgr-perf-summary': 'mgr-tiles', 'mgr-perf': 'mgr-perf', 'ae-perf': 'mgr-ae-perf',
    });
  });
  it('ignores anything that is not a layout object', () => {
    expect(MV.mapLegacyLayout(null)).toEqual({ changed: false });
    expect(MV.mapLegacyLayout([])).toEqual({ changed: false });
  });
});
