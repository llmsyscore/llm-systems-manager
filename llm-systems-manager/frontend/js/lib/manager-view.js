// Manager page view-model transforms (#1432). Pure functions only — no fetch, no DOM.
// Exposed as window.MV; reads LMSeries, DashboardManagerDb and OV bucket labels.
(function (root, factory) {
  const ls = (root && root.LMSeries) || (typeof require === 'function' ? require('./series') : null);
  const db = (root && root.DashboardManagerDb) || (typeof require === 'function' ? require('./dbstats') : null);
  const ov = (root && root.OV) || (typeof require === 'function' ? require('./overall-view') : null);
  const api = factory(ls, db, ov);
  if (typeof root !== 'undefined') root.MV = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function (LMSeries, DashboardManagerDb, OV) {

const DASH = '—';
const HIDE_S = 300, STALE_S = 90, PROC_HIDE_S = 300, SM_STALE_S = 180;
const WARN_MS = 100, BAD_MS = 500;
const PROC_WANT = ['manager', 'alarm-engine', 'influxdb', 'llm-systems-agent'];
const MGR_ONLY = ['manager', 'alarm-engine', 'influxdb'];
const SVC_RE = /^(.+?)_(running|count|pid|rss_mb|uptime_s|available)$/;
const PROBE_KEYS = [
  'manager_api_latency_ms', 'manager_history_latency_ms',
  'ae_health_latency_ms', 'ae_ingest_latency_ms', 'ae_query_24h_latency_ms', 'rule_eval_cycle_ms',
  'influx_write_latency_ms', 'influx_query_5m_latency_ms', 'influx_query_24h_latency_ms',
];
const AE_KEYS = [
  ['ae_health', 'ae_health_latency_ms'], ['ae_ingest', 'ae_ingest_latency_ms'], ['ae_query_24h', 'ae_query_24h_latency_ms'],
  ['rule_eval_cycle', 'rule_eval_cycle_ms'],
];
const INFLUX_KEYS = [
  ['influx_write', 'influx_write_latency_ms'], ['influx_query_5m', 'influx_query_5m_latency_ms'],
  ['influx_query_24h', 'influx_query_24h_latency_ms'],
];
// Operator names of the self-monitor probes.
const PROBE_NAMES = {
  manager_api_latency_ms: 'Manager API', manager_history_latency_ms: 'Manager history',
  ae_health_latency_ms: 'Alarm engine health', ae_ingest_latency_ms: 'Alarm engine ingest',
  ae_query_24h_latency_ms: 'Alarm engine query, 24 h', rule_eval_cycle_ms: 'Rule check cycle',
  influx_write_latency_ms: 'InfluxDB write', influx_query_5m_latency_ms: 'InfluxDB query, 5 min',
  influx_query_24h_latency_ms: 'InfluxDB query, 24 h',
};
// Chart series [name, colour slot] of each perf chart, in its metric order.
const PERF_SERIES = {
  mgr: [['API', 1], ['History', 2]],
  ae: [['Health', 1], ['Ingest', 2], ['Query, 24 h', 3], ['Rule check cycle', 4]],
  influx: [['Write', 1], ['Query, 5 min', 2], ['Query, 24 h', 3]],
};
const LEGACY_IDS = {
  'services': 'mgr-services', 'influxdb': 'mgr-db', 'mgr-agents': 'mgr-hosts', 'mgr-streams': 'mgr-streams',
  'mgr-ram': 'mgr-cpu-mem', 'mgr-disk': 'mgr-disk', 'mgr-network': 'mgr-net', 'mgr-processes': 'mgr-procs',
  'mgr-perf-summary': 'mgr-tiles', 'mgr-perf': 'mgr-perf', 'ae-perf': 'mgr-ae-perf',
};
const OLD_KEYS = ['managerOrder', 'managerHidden', 'managerCols'];

function _num(v) { return (typeof v === 'number' && Number.isFinite(v)) ? v : null; }
function _obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }
function _arr(v) { return Array.isArray(v) ? v : []; }
function _has(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
function _now(ms) { return (typeof ms === 'number' && Number.isFinite(ms)) ? ms : Date.now(); }
// Bytes → "1.5 MB" (whole units from 10 up), a dash when unreadable.
function _bytes(n) {
  if (n == null || isNaN(n)) return DASH;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
}
function _uptime(s) {
  if (s == null || isNaN(s)) return DASH;
  s = Math.max(0, Math.floor(Number(s)));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
}
// ISO time → "5s ago" / "3m ago" / "2h ago" / "1d ago" at nowMs, "just now" when in the future; a dash when unreadable.
function _ago(iso, nowMs) {
  if (!iso) return DASH;
  const ms = nowMs - new Date(iso).getTime();
  if (isNaN(ms)) return DASH;
  const s = Math.round(ms / 1000);
  if (s < 0) return 'just now';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
function _fix(v, dp) { return _num(v) == null ? DASH : v.toFixed(dp); }
function _count(v) { return _num(v) == null ? DASH : v.toLocaleString(); }
function _ms(v) { return v == null ? DASH : v.toFixed(1) + ' ms'; }
function _v(x) { return x === null || x === undefined ? '–' : x; }
function _sum(...vs) {
  const n = vs.filter(v => typeof v === 'number');
  return n.length ? n.reduce((a, b) => a + b, 0) : null;
}
function _pid(s) { return s.pid != null ? String(Math.round(s.pid)) : DASH; }
function _rss(s) { return s.rss_mb != null ? s.rss_mb.toFixed(0) + ' MB' : DASH; }

// Catalog rows of source processes → {"host::svc": {host, svc, <fields>, age}}; age is the freshest field's.
function _serviceMap(rows, nowS) {
  const out = {}, now = _num(nowS) != null ? nowS : Date.now() / 1000;
  for (const r of _arr(rows)) {
    if (!r || r.source !== 'processes' || !r.metric_name) continue;
    const m = String(r.metric_name).match(SVC_RE);
    if (!m) continue;
    const host = r.hostname || '', key = host + '::' + m[1];
    const b = out[key] = out[key] || { host, svc: m[1] };
    b[m[2]] = r.latest_value;
    const t = r.latest_timestamp ? new Date(r.latest_timestamp).getTime() : NaN;
    const age = t ? now - t / 1000 : null;
    if (b.age == null || (age != null && age < b.age)) b.age = age;
  }
  return out;
}

// Services table rows per host × service; rows older than 300 s are hidden and counted in note.
function services(rows, nowS) {
  const map = _serviceMap(rows, nowS), all = Object.values(map);
  const shown = all.filter(s => s.age != null && s.age <= HIDE_S).sort((a, b) =>
    (a.host || '').localeCompare(b.host || '') || (a.svc || '').localeCompare(b.svc || ''));
  const hidden = all.length - shown.length;
  const out = shown.map(s => {
    const stale = s.age > STALE_S, running = s.running === 1 && !stale;
    return [s.host || '(unknown)', s.svc, stale ? 'stale' : (running ? 'running' : 'stopped'), _pid(s), _rss(s), _uptime(s.uptime_s)];
  });
  let tone = 'off';
  if (shown.length) {
    const fresh = shown.filter(s => s.age <= STALE_S);
    tone = fresh.some(s => s.running === 0) ? 'bad' : (fresh.some(s => s.running === 1) ? 'ok' : 'warn');
  }
  const up = shown.filter(s => s.running === 1).length;
  return { rows: out, up, total: shown.length, tone,
           note: `${up}/${shown.length} up` + (hidden ? ` · ${hidden} stale hidden` : '') };
}

// Manager host rows [name, status, pid, rss, uptime] of the manager services seen in the last 300 s; stale after 90 s.
function processes(rows, nowS) {
  const map = _serviceMap(rows, nowS);
  const host = (Object.values(map).find(s => MGR_ONLY.includes(s.svc)) || {}).host;
  if (host == null) return [];
  return PROC_WANT.map(svc => map[host + '::' + svc]).filter(s => s && s.age != null && s.age <= PROC_HIDE_S).map(s => {
    const stale = s.age > STALE_S, running = s.running === 1 && !stale;
    return [s.svc, stale ? 'stale' : (running ? 'running' : 'stopped'), _pid(s), _rss(s), _uptime(s.uptime_s)];
  });
}

function _iv(influx, k) { const e = _obj(influx)[k]; return e && e.value != null ? e.value : null; }

// Rows [group, label, value] of the InfluxDB, manager SQLite and alarm engine SQLite values.
function databases(influx, mgrDb, aeDb) {
  const I = 'InfluxDB', M = 'Manager SQLite', A = 'Alarm engine SQLite';
  const ping = _iv(influx, 'ping_ms'), q = _iv(influx, 'query_ms'), w = _iv(influx, 'write_ms');
  const ok = _iv(influx, 'write_ok_rate'), disk = _iv(influx, 'bytes_on_disk'), card = _iv(influx, 'cardinality_metrics');
  const md = DashboardManagerDb.summarize(mgrDb);
  const a = _obj(_obj(aeDb).alarms_db), s = _obj(_obj(aeDb).settings_db);
  const slowest = (a.query_ms != null || s.query_ms != null) ? Math.max(a.query_ms ?? -Infinity, s.query_ms ?? -Infinity) : null;
  const size = v => (v != null ? _bytes(v) : DASH);
  return [
    [I, 'Ping ms', ping != null ? ping.toFixed(1) : DASH],
    [I, 'Query ms', q != null ? q.toFixed(1) : DASH],
    [I, 'Write ms', w != null ? w.toFixed(1) : DASH],
    [I, 'Write OK %', ok != null ? (ok * 100).toFixed(1) : DASH],
    [I, 'On disk', size(disk)],
    [I, 'Metrics series', card != null ? String(Math.round(card)) : DASH],
    [M, 'manager.db', size(md.manager_size)],
    [M, 'audit.db', size(md.audit_size)],
    [M, 'energy.db', size(md.energy_size)],
    [M, 'WAL total', size(md.wal_total)],
    [M, 'Audit rows', _count(md.audit_rows)],
    [M, 'Energy rows', _count(md.energy_rows)],
    [M, 'Bench + tool runs', _count(md.run_rows)],
    [M, 'SELECT 1 ms', _fix(md.query_ms, 2)],
    [A, 'Alerts rows', _count(_sum(a.alerts, a.alert_history))],
    [A, 'Settings rows', _count(_sum(s.rules, s.channels, s.configs))],
    [A, 'Alarms DB', size(a.size_bytes)],
    [A, 'Settings DB', size(s.size_bytes)],
    [A, 'WAL total', size(_sum(a.wal_size_bytes, s.wal_size_bytes))],
    [A, 'Pages (a+s)', _count(_sum(a.page_count, s.page_count))],
    [A, 'SELECT 1 ms', slowest != null ? slowest.toFixed(2) : DASH],
    [A, 'Deliveries', _count(s.deliveries)],
  ];
}

// InfluxDB state from its up metric ({value, at}): no data when missing or over 90 s old, up (warn when ping is over
// 100 ms or query over 500 ms) or down.
function influxState(influx, nowMs) {
  const up = _obj(influx).up, at = up ? _num(up.at) : null;
  if (!up || at == null || (_now(nowMs) - at) / 1000 > STALE_S) return { text: 'no data', tone: 'off' };
  if (up.value !== 1) return { text: 'down', tone: 'bad' };
  const ping = _iv(influx, 'ping_ms'), q = _iv(influx, 'query_ms');
  return { text: 'up', tone: (ping != null && ping > 100) || (q != null && q > 500) ? 'warn' : 'ok' };
}

// Influx on disk + manager DB files + alarm engine DB files (main and WAL) in bytes; null when none is known.
function dbTotalBytes(influx, mgrDb, aeDb) {
  const md = DashboardManagerDb.summarize(mgrDb);
  const a = _obj(_obj(aeDb).alarms_db), s = _obj(_obj(aeDb).settings_db);
  return _sum(_num(_iv(influx, 'bytes_on_disk')), md.manager_size, md.audit_size, md.energy_size, md.wal_total,
    _num(a.size_bytes), _num(s.size_bytes), _num(a.wal_size_bytes), _num(s.wal_size_bytes));
}

function _agentStatus(a) {
  const status = a.status || 'unknown';
  return status !== 'approved' ? status : (a.liveness || 'unknown');
}

// Share of a limit from 0 to 1, or null when either number is missing or the limit is not above 0.
function _share(v, cap) {
  const a = Number(v), c = Number(cap);
  return v == null || cap == null || !Number.isFinite(a) || !(c > 0) ? null : Math.max(0, Math.min(1, a / c));
}

// Stream health: summary cells, hosts merged from /api/agents and stream stats, badge and the next refusal tracks.
function streams(stats, agents, tracks, nowMs) {
  const d = _obj(stats), pool = _obj(d.pool), now = _now(nowMs), t = _obj(tracks), prevAgents = _obj(t.agents);
  const poolTrack = LMSeries.trackRefusals(t.pool, pool.refusals, now);
  const saturated = pool.active >= pool.limit;
  const refusing = !saturated && poolTrack.recent;
  const anonRefused = Number(d.anon_refused_per_min) > 0;
  const kv = [
    ['Pool active', `${_v(pool.active)} / ${_v(pool.limit)}`, saturated, _share(pool.active, pool.limit)],
    ['Peak (boot)', _v(pool.peak), false, _share(pool.peak, pool.limit)],
    ['Refusals (boot)', _v(pool.refusals), poolTrack.recent, null],
    ['Workers busy', `${_v(d.worker_threads_busy)} / ${_v(d.worker_threads)}`, false, _share(d.worker_threads_busy, d.worker_threads)],
    ['Worker backlog', _v(d.worker_backlog), d.worker_backlog > 0, _share(d.worker_backlog, d.worker_threads)],
    ['Browser connections', _v(d.browser_connections), false, _share(d.browser_connections, pool.limit)],
    ['Host connections', _v(d.agent_connections), false, _share(d.agent_connections, pool.limit)],
    ['Off-pool SSE', d.sse_daemon_running ? `${_v(d.sse_daemon_streams)} active` : 'off', false, null],
    ['Open routes', d.anon_requests_per_min == null ? '–'
      : `${_v(d.anon_requests_per_min)} /min` + (d.anon_budget_per_min ? ` of ${_v(d.anon_budget_per_min)}` : ''), false,
      _share(d.anon_requests_per_min, d.anon_budget_per_min)],
    ['Refused (open routes)', _v(d.anon_refused_per_min), anonRefused, _share(d.anon_refused_per_min, d.anon_budget_per_min)],
  ];
  const byKey = new Map(), nextAgents = {};
  const entry = (id, host) => {
    const k = id || host;
    if (!byKey.has(k)) byKey.set(k, { host: host || id || '(unknown)', status: DASH, seen: DASH, streams: DASH, peak: DASH, refusals: DASH, terms: DASH });
    return byKey.get(k);
  };
  const list = Array.isArray(agents) ? agents : _arr(_obj(agents).agents);
  for (const a of list) {
    if (!a) continue;
    const e = entry(a.agent_id, a.hostname);
    e.status = _agentStatus(a);
    e.seen = _ago(a.last_heartbeat, now);
  }
  const byHost = new Map(list.filter(a => a && a.hostname).map(a => [a.hostname, a.agent_id || a.hostname]));
  for (const a of _arr(d.agents)) {
    if (!a) continue;
    // Same agent_id first, then a hostname row from /api/agents, then the agent_id of an agent with that hostname.
    const id = a.agent_id && byKey.has(a.agent_id) ? a.agent_id
      : (byKey.has(a.hostname) ? a.hostname : (byHost.get(a.hostname) || a.agent_id));
    const e = entry(id, a.hostname);
    if (!a.reachable) { e.streams = 'unreachable'; continue; }
    const key = a.agent_id || a.hostname;
    const track = nextAgents[key] = LMSeries.trackRefusals(prevAgents[key], a.refusals, now);
    e.streams = `${_v(a.active)}/${_v(a.cap)}` + (a.active >= a.cap ? ' · full' : '');
    e.refusals = track.recent ? `${_v(a.refusals)} · recent` : _v(a.refusals);
    e.peak = _v(a.peak); e.terms = _v(a.terminal_sessions);
  }
  const rows = Array.from(byKey.values()).sort((x, y) => String(x.host).localeCompare(String(y.host)))
    .map(e => [e.host, e.status, e.seen, e.streams, e.peak, e.refusals, e.terms]);
  const badge = saturated ? { text: 'saturated', tone: 'bad' }
    : refusing ? { text: 'refusing', tone: 'warn' } : anonRefused ? { text: 'flooded', tone: 'warn' } : { text: 'ok', tone: 'ok' };
  return { kv, hosts: { cols: ['Host', 'Status', 'Last sample', 'Streams', 'Peak', 'Refusals', 'Terminals'], rows },
           badge, tracks: { pool: poolTrack, agents: nextAgents } };
}

function _latencyTone(v, age) {
  if (age == null || age > SM_STALE_S || v == null) return 'off';
  return v >= BAD_MS ? 'bad' : v >= WARN_MS ? 'warn' : 'ok';
}

// Self-monitor probes read through get(metric) and age(metric): worst fresh probe, values and tones.
function probes(get, age) {
  let worst = null, freshAgeS = null;
  for (const k of PROBE_KEYS) {
    const v = get(k), a = age(k);
    if (a == null || a > SM_STALE_S || v == null) continue;
    if (!worst || v > worst.ms) worst = { name: PROBE_NAMES[k] || k, ms: v };
    if (freshAgeS == null || a < freshAgeS) freshAgeS = a;
  }
  const api = get('manager_api_latency_ms'), history = get('manager_history_latency_ms');
  const anonAge = age('manager_anon_requests_per_min');
  const req = (anonAge == null || anonAge > SM_STALE_S) ? null : get('manager_anon_requests_per_min');
  const ref = req == null ? null : get('manager_anon_refused_per_min');
  const anon = req == null ? null : { requests: Math.round(req), refused: ref == null ? null : Math.round(ref) };
  const group = (keys) => {
    const out = {};
    let top = null;
    for (const [name, k] of keys) {
      const v = get(k);
      out[name] = v == null ? null : v;
      if (v != null && (top == null || v > top)) top = v;
    }
    return { out, top };
  };
  const ae = group(AE_KEYS), influx = group(INFLUX_KEYS);
  return {
    worst, freshAgeS, api: api == null ? null : api, history: history == null ? null : history, anon, ae: ae.out, influx: influx.out,
    tone: { summary: _latencyTone(worst ? worst.ms : null, freshAgeS),
            mgr: _latencyTone(api, age('manager_api_latency_ms')),
            ae: _latencyTone(ae.top, age('ae_health_latency_ms')),
            influx: _latencyTone(influx.top, age('influx_write_latency_ms')) },
  };
}

function _d(x) { return x === null || x === undefined ? DASH : String(x); }

// Manager page tile row: browsers, streams, workers, database size and worst probe.
function tiles(opts) {
  const o = _obj(opts), d = _obj(o.stats), pool = _obj(d.pool), p = _obj(o.probes);
  const saturated = _num(pool.active) != null && _num(pool.limit) != null && pool.active >= pool.limit;
  const worst = p.worst || null;
  return [
    { label: 'Browsers connected', value: _d(d.browser_connections),
      sub: d.agent_connections == null ? '' : d.agent_connections + ' host links', tone: '' },
    { label: 'Streams', value: `${_d(pool.active)} / ${_d(pool.limit)}`, sub: pool.peak == null ? '' : 'peak ' + pool.peak,
      tone: saturated ? 'bad' : '' },
    { label: 'Workers busy', value: `${_d(d.worker_threads_busy)} / ${_d(d.worker_threads)}`,
      sub: d.worker_backlog == null ? '' : 'backlog ' + d.worker_backlog, tone: d.worker_backlog > 0 ? 'warn' : '' },
    { label: 'Database size', value: _num(o.dbBytes) == null ? DASH : _bytes(o.dbBytes), sub: '', tone: '' },
    { label: 'Worst probe', value: worst ? _ms(worst.ms) : DASH,
      sub: worst ? worst.name + (_num(p.freshAgeS) != null ? ' · ' + (Math.round(p.freshAgeS) < 0 ? 'just now' : Math.round(p.freshAgeS) + 's ago') : '')
        : 'no recent samples',
      tone: _obj(p.tone).summary || 'off' },
  ];
}

// Point lists aligned by timestamp → kit series of a perf chart ('mgr', 'ae' or 'influx').
function perfSeries(points, kind) {
  const lists = _arr(points).map(_arr);
  const names = PERF_SERIES[kind] || PERF_SERIES.mgr;
  const rows = LMSeries.zipByTs(lists);
  return {
    labels: rows.map(([ts]) => OV.bucketLabel(new Date(ts).getTime())),
    series: names.map(([name, slot], i) => ({ name, values: rows.map(([, vals]) => (vals[i] == null ? null : vals[i])), slot })),
  };
}

function _mapId(id) { return typeof id === 'string' && _has(LEGACY_IDS, id) ? LEGACY_IDS[id] : id; }
function _dedupe(list) { return list.filter((x, i) => x != null && list.indexOf(x) === i); }

// Old manager card ids in pins, Overall order and sizes → new panel ids, once; old manager grid keys deleted.
function mapLegacyLayout(layout) {
  if (!layout || typeof layout !== 'object' || Array.isArray(layout) || layout.managerPageMapped) return { changed: false };
  if (Array.isArray(layout.overallBorrowed)) layout.overallBorrowed = _dedupe(layout.overallBorrowed.map(_mapId));
  if (Array.isArray(layout.overallOrder)) {
    layout.overallOrder = _dedupe(layout.overallOrder.map(id =>
      (typeof id === 'string' && id.indexOf('ov-borrow-') === 0 ? 'ov-borrow-' + _mapId(id.slice(10)) : id)));
  }
  const cs = layout.cardSizes;
  if (cs && typeof cs === 'object' && !Array.isArray(cs)) {
    Object.keys(cs).forEach(k => {
      const pre = k.indexOf('ov-borrow-') === 0 ? 'ov-borrow-' : '', id = k.slice(pre.length);
      if (!_has(LEGACY_IDS, id) || LEGACY_IDS[id] === id) return;
      const v = cs[k];
      delete cs[k];
      if (!(pre + LEGACY_IDS[id] in cs)) cs[pre + LEGACY_IDS[id]] = v;
    });
  }
  OLD_KEYS.forEach(k => { delete layout[k]; });
  layout.managerPageMapped = 1;
  return { changed: true };
}

return { LEGACY_IDS, PROBE_NAMES, services, processes, databases, influxState, dbTotalBytes, streams, probes, tiles, perfSeries, mapLegacyLayout };
});
