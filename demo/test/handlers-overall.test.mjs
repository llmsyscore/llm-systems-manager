import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from './load.mjs';
import { assertCompatible } from './shape.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'handlers/overall.js']);
const NOW = Date.UTC(2026, 0, 1, 12, 0, 0);

async function call(path, now = NOW) {
  const url = new URL(path, 'http://demo.example');
  const found = self.DemoRouter.find('GET', url);
  assert.ok(found, `no route for GET ${path}`);
  const ctx = { now, params: found.params, query: url.searchParams, body: null, fleet: self.DemoFleet };
  const out = await found.route.handler(ctx);
  return out instanceof self.Response ? out : JSON.parse(JSON.stringify(out));
}

function keysOf(v, p = '') {
  if (Array.isArray(v)) return v.length ? keysOf(v[0], p + '[]') : [p + '[]'];
  if (v && typeof v === 'object') return Object.keys(v).sort().flatMap((k) => [p + '.' + k, ...keysOf(v[k], p + '.' + k)]);
  return [];
}

const ROUTES = [
  '/api/me', '/api/config', '/api/layout', '/api/agents', '/api/agents/list-by-provider', '/api/metrics',
  '/api/history', '/api/llama-state', '/api/llama-state/stream-info', '/api/energy/hourly', '/api/energy/summary',
  '/api/forecast', '/api/autopilot', '/api/companion/release', '/api/tools/activity', '/api/vllm/metrics',
];

test('each captured template is shape-compatible with its route', async () => {
  for (const p of ROUTES) {
    const file = join(ROOT, 'templates', `GET_${p.replace(/[^A-Za-z0-9]/g, '_')}.json`);
    assert.ok(existsSync(file), file);
    const tpl = JSON.parse(readFileSync(file, 'utf8'));
    assertCompatible(await call(p), tpl.body);
  }
});

test('/api/me is the demo admin', async () => {
  const me = await call('/api/me');
  assert.deepEqual({ role: me.role, is_admin: me.is_admin, admin_access: me.admin_access, username: me.username, authenticated: me.authenticated },
    { role: 'admin', is_admin: true, admin_access: true, username: 'alex', authenticated: true });
});

test('config shows every provider and proxy', async () => {
  const c = await call('/api/config');
  assert.deepEqual(c.agents, { llama_present: true, lms_present: true, vllm_present: true });
  assert.deepEqual(c.proxies, { image_gen: true, llm_chat: true, openclaw: true });
});

test('a second call 60 s later has the same keys but different values', async () => {
  for (const p of ['/api/metrics', '/api/lmstudio/metrics', '/api/vllm/metrics', '/api/fleet/llama/aggregate',
    '/api/fleet/lms/aggregate', '/api/fleet/vllm/aggregate', '/api/llama-state']) {
    const a = await call(p), b = await call(p, NOW + 60000);
    if (p !== '/api/llama-state') assert.notDeepEqual(a, b, p);
    assert.deepEqual(keysOf(a), keysOf(b), p);
  }
});

test('?agent= picks the machine, unknown ids fall back', async () => {
  const forge = await call('/api/metrics?agent=forge');
  assert.equal(forge.llama.model, 'Qwen2.5-14B-Instruct-Q5_K_M');
  assert.equal(forge.gpu.name, 'NVIDIA GeForce RTX 3090');
  assert.equal((await call('/api/metrics?agent=nope')).llama.model, 'Llama-3.1-8B-Instruct-Q8_0');
  const quill = await call('/api/metrics?agent=quill');
  assert.equal('gpu' in quill, false);
  assert.equal(quill.cpu_per_core.length, 8);
  const lms = await call('/api/lmstudio/metrics?agent=atlas');
  assert.equal(lms.agent_id, 'studio');
  assert.equal(lms.mac_power.thermal_pressure_n, 0);
  assert.equal((await call('/api/lmstudio/metrics?light=1')).gateway_tokens, undefined);
});

test('list-by-provider and aggregates describe the same fleet', async () => {
  const l = await call('/api/agents/list-by-provider');
  assert.deepEqual(l.llama.map((a) => a.agent_id), ['atlas', 'forge', 'quill']);
  assert.deepEqual(l.lms.map((a) => a.agent_id), ['studio']);
  assert.deepEqual(l.vllm.map((a) => a.agent_id), ['atlas']);
  assert.ok(l.llama.every((a) => a.hostname.endsWith('.example')));
  for (const p of ['llama', 'lms', 'vllm']) {
    const a = await call(`/api/fleet/${p}/aggregate`);
    assert.equal(a.agent_count_total, l[p].length);
    assert.equal(a.agents.length, l[p].length);
    assert.ok(a.agents.every((r) => r.online && r.power_watts > 0));
  }
  const ag = await call('/api/agents');
  assert.equal(ag.agents.length, 4);
  assert.ok(ag.agents.every((a) => a.hostname.endsWith('.example') && a.bind_url.includes('192.0.2.')));
});

test('history is an array bounded to 300 points for any range', async () => {
  for (const q of ['', '?agent=forge', '?agent=studio', '?since_minutes=1440&max_rows=1440&fleet=all',
    '?since_minutes=43200&max_rows=100000', '?since_minutes=10&limit=5', '?since_minutes=1']) {
    const rows = await call('/api/history' + q);
    assert.ok(Array.isArray(rows), q);
    assert.ok(rows.length >= 2 && rows.length <= 300, `${q}: ${rows.length}`);
    assert.ok(rows.every((r, i) => i === 0 || r.ts > rows[i - 1].ts), q);
  }
  const fleet = await call('/api/history?since_minutes=1440&max_rows=1440&fleet=all');
  assert.ok(fleet[10].llama_tps != null && fleet[10].lms_tps != null && fleet[10].vllm_tps != null && fleet[10].psu_in > 0);
  assert.equal((await call('/api/history?agent=quill'))[5].gpu_util, undefined);
  assert.equal((await call('/api/history?since_minutes=10&limit=5')).length, 5);
});

test('energy summary and hourly rows add up and stay bounded', async () => {
  const s = await call('/api/energy/summary?days=7');
  assert.equal(s.hosts.length, 4);
  assert.equal(s.window.label, 'last 7 days');
  assert.ok(s.totals.has_power && s.totals.kwh > 0 && s.savings_usd > 0);
  assert.ok(Math.abs(s.hosts.reduce((a, h) => a + h.kwh, 0) - s.totals.kwh) < 0.01);
  assert.ok(s.models.length >= 4 && s.models[0].hosts.length === 1);
  assert.equal((await call('/api/energy/summary?price_kwh=0.3')).price_kwh, 0.3);
  const h = await call('/api/energy/hourly?hours=24');
  assert.ok(h.rows.length >= 24 && h.rows.every((r) => r.hour_ts % 3600 === 0 && r.energy_wh > 0));
  assert.ok((await call('/api/energy/hourly?days=366')).rows.length <= 1080);
  assert.ok((await call('/api/energy/hourly?hours=100000')).rows.length <= 1080);
  const p = await call('/api/energy/host-peak?agent_id=forge');
  assert.equal(p.agent_id, 'forge');
  assert.ok(p.peak_active_w > p.peak_w);
});

test('forecast has findings and every check reports', async () => {
  const f = await call('/api/forecast');
  assert.ok(f.enabled && f.findings.length >= 2 && f.checks.length >= 10);
  assert.ok(f.findings.every((x) => x.host.endsWith('.example') && x.id));
});

test('server status, logs and settings routes answer', async () => {
  for (const p of ['/api/lmstudio/server/status', '/api/vllm/server/status']) assert.equal((await call(p)).ok, true);
  for (const p of ['/api/lmstudio/server/log', '/api/vllm/server/log']) assert.ok((await call(p)).lines.length > 5);
  assert.deepEqual((await call('/api/lmstudio/load-prefs')).agent_id, 'studio');
  assert.ok((await call('/api/vllm/server/svcconfig')).args.length > 2);
  assert.equal((await call('/api/agents/enrollment-secret')).secret, 'demo-enrollment-secret');
  assert.ok((await call('/api/agents/forge/config-file')).text.includes('forge.example'));
  assert.equal((await call('/api/fleet/bogus/aggregate')).status, 404);
});

test('openAgentSse info endpoints return ok:false so only same-origin streams are used', async () => {
  for (const p of ['/api/llm/download/stream-info', '/api/llm/build/stream-info', '/api/llm/server/log/stream-info']) {
    const d = await call(p);
    assert.ok(!d.ok, p);
    assert.equal(d.url, undefined, p);
  }
  assert.deepEqual(await call('/api/llama-state/stream-info'), { enabled: false });
});

test('llama-state stream sends a state frame on connect', async () => {
  const res = await call('/api/llama-state/stream?agent=forge');
  assert.equal(res.headers.get('Content-Type'), 'text/event-stream');
  const reader = res.body.getReader();
  const { value } = await reader.read();
  await reader.cancel();
  const text = new TextDecoder().decode(value);
  assert.match(text, /^data: /);
  const d = JSON.parse(text.slice(6));
  assert.equal(d.model, 'Qwen2.5-14B-Instruct-Q5_K_M');
  assert.equal(d.agent_online, true);
});

test('agent rows differ per machine and lifetime counters are plausible', async () => {
  const NOW_2 = Date.UTC(2026, 9, 23, 12, 0, 0);
  const rows = [
    ...(await call('/api/fleet/llama/aggregate', NOW_2)).agents,
    ...(await call('/api/fleet/lms/aggregate', NOW_2)).agents,
    ...(await call('/api/fleet/vllm/aggregate', NOW_2)).agents,
  ];
  assert.equal(new Set(rows.map((r) => `${r.ctx}|${r.total_tokens_prompted}|${r.total_tokens_generated}`)).size, rows.length);
  for (const r of rows) {
    assert.ok(r.total_tokens_prompted > 1e6 && r.total_tokens_prompted < 1e8, String(r.total_tokens_prompted));
    assert.ok(r.total_tokens_generated > 1e5 && r.total_tokens_generated < 1e7);
  }
  const a = (await call('/api/metrics?agent=atlas', NOW_2)).llama, b = (await call('/api/metrics?agent=atlas', NOW_2 + 86400000)).llama;
  assert.ok(b.total_tokens_generated > a.total_tokens_generated && b.total_tokens_generated - a.total_tokens_generated < 1e6);
  assert.ok(a.n_decode_total < a.total_tokens_generated);
});

test('only atlas reports UPS, PSU, liquid cooler and fan controller', async () => {
  const m = await call('/api/metrics?agent=atlas');
  assert.equal(m.ups.state, 'Online');
  assert.equal(m.ups.on_battery, false);
  assert.ok(m.liquidctl.aio['Liquid temperature'].value > 20);
  assert.ok(m.liquidctl.psu['Total power output'].value > 0 && m.liquidctl.psu['Estimated input power'].value > m.liquidctl.psu['Total power output'].value);
  assert.equal(m.liquidctl.smart.fans.length, 3);
  const h = (await call('/api/history?agent=atlas&since_minutes=60')).rows || (await call('/api/history?agent=atlas&since_minutes=60'));
  const row = (Array.isArray(h) ? h : h.rows || h.history || [])[0];
  assert.ok(row && row.aio_temp > 0 && row.psu_out > 0 && row.ups_percent === 100);
  assert.equal((await call('/api/metrics?agent=forge')).ups, undefined);
});

test('lifetime counters for an unknown machine or provider are zero', () => {
  const zero = { ctx: 0, gen: 0, prompt: 0, gr: 0, pr: 0 };
  assert.deepEqual({ ...self.DemoOverall.life(self.DemoFleet.byId('forge'), 'vllm') }, zero);
  assert.deepEqual({ ...self.DemoOverall.life({ id: 'nope' }, 'llama') }, zero);
  assert.deepEqual({ ...self.DemoOverall.life(null, 'llama') }, zero);
  assert.equal(self.DemoOverall.life(self.DemoFleet.byId('atlas'), 'llama').ctx, 32768);
});

test('vLLM serves some traffic in every hour, overnight included, as the fleet history sees it', async () => {
  for (const day of [0, 1, 2]) {
    const now = NOW + day * 86400000;
    const rows = await call('/api/history?since_minutes=1440&max_rows=1440&fleet=all', now);
    const ts = rows.map((r) => Date.parse(r.ts));
    for (let end = ts[ts.length - 1]; end - 3600000 >= ts[0]; end -= 600000) {
      const hour = rows.filter((r, i) => ts[i] > end - 3600000 && ts[i] <= end);
      assert.ok(hour.some((r) => r.vllm_tps > 0), `no vLLM traffic in the hour before ${new Date(end).toISOString()}`);
    }
  }
});

test('autopilot is on with three placed entries whose models the admin model lists offer', async () => {
  const ap = await call('/api/autopilot');
  assert.equal(ap.state.enabled, true);
  assert.equal(ap.state.entries.length, 3);
  const lists = { llama: ['Llama-3.1-8B-Instruct-Q8_0', 'Qwen2.5-32B-Instruct-Q4_K_M', 'Qwen2.5-14B-Instruct-Q5_K_M', 'Gemma-2-9B-it-Q6_K'],
    lms: self.DemoFleet.byId('studio').models };
  for (const e of ap.state.entries) {
    assert.ok(lists[e.provider].includes(e.model), e.model);
    const st = ap.entry_status[`${e.model}/${e.provider}`];
    assert.ok(st && st.placed === st.want && st.blocked === null);
    assert.ok(e.placement === 'auto' || self.DemoFleet.byId(e.placement));
    for (const r of st.speed) assert.ok(self.DemoFleet.byId(r.agent_id) && r.hostname.endsWith('.example'));
  }
});

test('each provider pool lists the machines that run it, default first, matching the gateway model list; pins follow Autopilot', async () => {
  const g = (await call('/api/agents')).global;
  const gw = (await call('/api/agents/list-by-provider'));
  for (const p of ['llama', 'lms', 'vllm']) {
    const runs = self.DemoFleet.machines.filter((m) => m.providers.includes(p)).map((m) => m.id);
    assert.deepEqual([...g[`${p}_pool`]].sort(), [...runs].sort(), p);
    assert.equal(g[`${p}_pool`][0], g[`default_${p}_id`]);
    assert.equal(g[`default_${p}_id`], g[`primary_${p}_id`]);
    const listed = gw[p].map((a) => a.agent_id);
    assert.deepEqual([...listed].sort(), [...g[`${p}_pool`]].sort(), `${p} list-by-provider`);
  }
  assert.deepEqual({ ...g.llama_model_pins }, { 'Qwen2.5-14B-Instruct-Q5_K_M': 'forge' });
  assert.ok(self.DemoFleet.byId('forge').models.includes('Qwen2.5-14B-Instruct-Q5_K_M'));
  assert.equal(g.autopilot.entries.length, 3);
});
