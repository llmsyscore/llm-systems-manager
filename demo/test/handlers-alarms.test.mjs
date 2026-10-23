import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from './load.mjs';
import { assertCompatible } from './shape.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'demo-state.js', 'handlers/alarms.js']);
const NOW = Date.UTC(2026, 0, 1, 12, 0, 0);

async function raw(path, now = NOW) {
  const url = new URL(path, 'http://demo.example');
  const found = self.DemoRouter.find('GET', url);
  assert.ok(found, `no route for GET ${path}`);
  const ctx = { now, params: found.params, query: url.searchParams, body: null, fleet: self.DemoFleet };
  return found.route.handler(ctx);
}
async function call(path, now = NOW) {
  const out = await raw(path, now);
  return out instanceof self.Response ? out : JSON.parse(JSON.stringify(out));
}

function keysOf(v, p = '') {
  if (Array.isArray(v)) return v.length ? keysOf(v[0], p + '[]') : [p + '[]'];
  if (v && typeof v === 'object') return Object.keys(v).sort().flatMap((k) => [p + '.' + k, ...keysOf(v[k], p + '.' + k)]);
  return [];
}

const TEMPLATED = [
  '/api/alarm/rules', '/api/alarm/alerts', '/api/alarm/alerts/', '/api/alarm/dbstats/sqlite', '/api/alarm/metrics',
  '/api/alarm-ws-ticket', '/api/alarm/metrics/manager_self_monitor/ae_health_latency_ms',
  '/api/alarm/metrics/manager_self_monitor/rule_eval_cycle_ms',
  '/api/alarm/metrics/manager_self_monitor/manager_api_latency_ms',
];

test('captured templates are shape-compatible with their routes', async () => {
  for (const p of TEMPLATED) {
    const file = join(ROOT, 'templates', `GET_${p.replace(/[^A-Za-z0-9]/g, '_')}.json`);
    assert.ok(existsSync(file), file);
    assertCompatible(await call(p), JSON.parse(readFileSync(file, 'utf8')).body);
  }
});

test('two open alerts: a warning on forge GPU temperature and one info', async () => {
  const open = await call('/api/alarm/alerts/');
  assert.equal(open.length, 2);
  const [warn, info] = open;
  assert.deepEqual([warn.severity, warn.source_host, warn.metric_name, warn.status], ['warning', 'forge.example', 'gpu_temperature_c', 'active']);
  assert.ok(warn.current_value > warn.threshold_value);
  assert.equal(info.severity, 'info');
  assert.equal((await call('/api/alarm/alerts/?status=active&limit=50')).length, 2);
  assert.equal((await call('/api/alarm/alerts?only_active=true')).length, 2);
  const all = await call('/api/alarm/alerts?include_closed=true&limit=500');
  assert.ok(all.length > 2 && all.slice(2).every((a) => a.status === 'closed' && a.closed_at && a.resolution_reason));
  assert.ok(all.every((a) => a.source_host.endsWith('.example')));
  assert.equal((await call('/api/alarm/alerts?include_closed=true&limit=3')).length, 3);
  assert.equal((await call('/api/alarm/alerts?severity=info')).length, 1);
});

test('alerts, rules and the page generator agree', async () => {
  const rules = await call('/api/alarm/rules?limit=1000');
  const ids = new Set(rules.map((r) => r.rule_id));
  const all = await call('/api/alarm/alerts?include_closed=true');
  assert.ok(all.every((a) => ids.has(a.rule_id)));
  const firing = rules.filter((r) => r.breaching);
  assert.deepEqual(firing.map((r) => r.name).sort(), ['GPU temperature warning', 'Swap in use']);
  assert.deepEqual(JSON.parse(JSON.stringify(self.DemoAlarms.alerts(NOW))), all);
  const one = await call(`/api/alarm/rules/${rules[0].rule_id}`);
  assert.equal(one.name, rules[0].name);
  assert.equal('breaching' in one, false);
  assert.equal((await raw('/api/alarm/rules/nope')).status, 404);
});

test('alert detail, events and deliveries', async () => {
  const [warn] = await call('/api/alarm/alerts');
  assert.equal((await call(`/api/alarm/alerts/${warn.alert_id}`)).alert_id, warn.alert_id);
  const ev = await call(`/api/alarm/alerts/${warn.alert_id}/events`);
  assert.equal(ev.alert_id, warn.alert_id);
  assert.deepEqual(Object.keys(ev.events[0]).sort(), ['actor', 'at', 'detail', 'event']);
  const d = await call(`/api/alarm/alerts/${warn.alert_id}/deliveries`);
  assert.ok(d.deliveries.length >= 1 && d.deliveries.every((x) => x.metadata.alert_id === warn.alert_id && x.success));
  assert.equal((await raw('/api/alarm/alerts/nope')).status, 404);
  const csv = await raw('/api/alarm/alerts/export?format=csv');
  assert.equal(csv.headers.get('Content-Type'), 'text/csv');
  assert.ok((await csv.text()).split('\n')[0].startsWith('alert_id,'));
});

test('metric catalog feeds the manager cards', async () => {
  const row = (rows, name) => rows.find((r) => r.metric_name === name);
  const self_ = await call('/api/alarm/metrics?source=manager_self_monitor&limit=500');
  assert.equal(self_.length, 11);
  assert.ok(row(self_, 'ae_health_latency_ms').latest_value > 0);
  const age = (NOW - Date.parse(self_[0].latest_timestamp)) / 1000;
  assert.ok(age >= 0 && age < 10);
  const procs = await call('/api/alarm/metrics?source=processes&limit=500');
  for (const svc of ['manager', 'alarm-engine', 'influxdb', 'llm-systems-agent']) {
    assert.equal(procs.find((r) => r.metric_name === `${svc}_running` && r.hostname === 'atlas.example').latest_value, 1);
  }
  assert.ok(row(procs, 'influxdb_rss_mb').latest_value > 0);
  const inf = await call('/api/alarm/metrics?source=influxdb&limit=50');
  for (const k of ['up', 'ping_ms', 'query_ms', 'write_ms', 'write_ok_rate', 'bytes_on_disk', 'cardinality_metrics']) assert.ok(row(inf, k), k);
  const mgr = await call('/api/alarm/metrics?source=system&agent=demo-mgr&limit=500');
  assert.ok(mgr.length > 10 && mgr.every((r) => r.hostname === 'atlas.example'));
  for (const k of ['cpu_total', 'ram_percent', 'swap_percent', 'disk_root_percent', 'net_bytes_recv_per_sec']) assert.ok(row(mgr, k), k);
  const quill = await call('/api/alarm/metrics?source=system&hostname=quill.example');
  assert.equal(row(quill, 'gpu_temperature_c'), undefined);
  const forge = await call('/api/alarm/metrics?source=system&agent=forge');
  assert.ok(row(forge, 'gpu_temperature_c').latest_value > 0);
  const db = await call('/api/alarm/dbstats/sqlite');
  assert.ok(db.alarms_db.alerts > 0 && db.settings_db.rules === 7);
});

test('a second call 60 s later has the same keys but different values', async () => {
  for (const p of ['/api/alarm/metrics?source=manager_self_monitor', '/api/alarm/metrics?source=system&agent=atlas',
    '/api/alarm/metrics/manager_self_monitor/manager_api_latency_ms', '/api/alarm/alerts']) {
    const a = await call(p), b = await call(p, NOW + 60000);
    assert.notDeepEqual(a, b, p);
    assert.deepEqual(keysOf(a), keysOf(b), p);
  }
});

test('metric history honours the requested range and stays within 300 points', async () => {
  for (const q of ['', '?since_minutes=1', '?since_minutes=1440', '?since_minutes=43200', '?since_minutes=60&limit=120',
    '?since_minutes=1440&max_points=50']) {
    const rows = await call('/api/alarm/metrics/manager_self_monitor/manager_api_latency_ms' + q);
    const mins = Number(new URLSearchParams(q).get('since_minutes') || 60);
    assert.ok(rows.length >= 2 && rows.length <= 300, `${q}: ${rows.length}`);
    const first = Date.parse(rows[0].timestamp), last = Date.parse(rows[rows.length - 1].timestamp);
    assert.ok(first >= NOW - mins * 60000 && last <= NOW, q);
    assert.ok(rows.every((r, i) => i === 0 || Date.parse(r.timestamp) > Date.parse(rows[i - 1].timestamp)), q);
  }
  assert.ok((await call('/api/alarm/metrics/manager_self_monitor/manager_api_latency_ms?since_minutes=60&limit=120')).length <= 120);
  assert.ok((await call('/api/alarm/metrics/manager_self_monitor/manager_api_latency_ms?since_minutes=1440&max_points=50')).length <= 50);
  const multi = await call('/api/alarm/metrics/system/cpu_total?since_minutes=1440');
  assert.ok(multi.length <= 300 && new Set(multi.map((r) => r.hostname)).size === 4);
  const one = await call('/api/alarm/metrics/system/cpu_total?hostname=forge.example');
  assert.ok(one.every((r) => r.hostname === 'forge.example'));
  assert.deepEqual(await call('/api/alarm/metrics/system/nope'), []);
  const s = await call('/api/alarm/metrics/system/cpu_total/summary?window_minutes=60');
  assert.ok(s.min_value <= s.avg_value && s.avg_value <= s.max_value && s.data_points > 0);
});

test('notifications: channels, policies and delivery history', async () => {
  const ch = await call('/api/alarm/notifications/channels');
  assert.ok(ch.length >= 2 && ch.every((c) => c.channel_id && c.config && 'toast' in c.config));
  assert.equal((await call(`/api/alarm/notifications/channels/${ch[0].channel_id}`)).name, ch[0].name);
  const cfg = await call('/api/alarm/notifications/configs');
  assert.ok(cfg.length >= 1 && cfg.every((c) => c.channels.every((id) => ch.some((x) => x.channel_id === id))));
  const hist = await call('/api/alarm/notifications/delivery-history?limit=5');
  assert.ok(hist.length >= 1 && hist.length <= 5);
  assert.ok(hist.every((d, i) => i === 0 || d.delivered_at <= hist[i - 1].delivered_at));
});

test('alarm writes, metrics batch included, are refused', async () => {
  const post = (path, body) => self.DemoRouter.respond(new self.Request('http://demo.example' + path,
    { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } }), NOW, null);
  for (const path of ['/api/alarm/metrics/batch', '/api/alarm/alerts/x/ignore', '/api/alarm/rules']) {
    assert.equal((await post(path, {})).status, 403, path);
  }
});

test('the forge GPU temperature alert reads the same series as the forge card', async () => {
  const S = self.DemoSeries;
  let checked = 0;
  for (let k = 0; k < 400; k++) {
    const nowMs = NOW + k * 97000, t = Math.floor(nowMs / 1000);
    const a = self.DemoAlarms.openAlerts(nowMs).find((x) => x.source_host === 'forge.example');
    const live = S.value('forge', 'temp_c', t);
    assert.ok(a.current_value >= 80.2 && a.current_value <= 84, `${a.current_value}`);
    if (live >= 80.2) { assert.equal(a.current_value, Math.round(live * 10) / 10); checked++; }
  }
  assert.ok(checked > 20, `${checked}`);
});

test('the worker answers /alarm/health with the engine health the rules summary reads', async () => {
  const sw = load(['demo-sw.js'], { DEMO_RELEASE: 'v9.9.9', DEMO_HANDLERS: ['/demo/handlers/alarms.js'],
    caches: {}, clients: { get: async () => null } });
  let answer;
  for (const fn of sw.listeners.fetch) {
    fn({ request: new Request('http://demo.example/alarm/health'), clientId: null, respondWith: (p) => { answer = p; } });
  }
  assert.ok(answer, 'worker did not answer /alarm/health');
  const res = await answer;
  assert.equal(res.status, 200);
  const h = await res.json();
  assert.equal(h.status, 'ok');
  assert.equal(h.evaluation_interval_s, 15);
  assert.ok(h.components.rule_eval_last_cycle_ms > 0);
  assert.equal(h.active_alerts, 2);
});
