import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from './load.mjs';
import { assertCompatible } from './shape.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'handlers/admin-settings-data.js', 'handlers/admin.js'],
  { DEMO_RELEASE: 'v9.9.9' });
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

const TEMPLATED = ['/api/admin/audit-log', '/api/admin/audit-log/stats', '/api/admin/auth', '/api/admin/backup-status',
  '/api/admin/dbstats/sqlite', '/api/admin/gateway/flow', '/api/admin/settings', '/api/admin/stream-stats',
  '/api/admin/system-health', '/api/admin/users', '/api/jobs'];

test('captured templates are shape-compatible with their routes', async () => {
  for (const p of TEMPLATED) {
    const file = join(ROOT, 'templates', `GET_${p.replace(/[^A-Za-z0-9]/g, '_')}.json`);
    assert.ok(existsSync(file), file);
    assertCompatible(await call(p), JSON.parse(readFileSync(file, 'utf8')).body);
  }
});

test('users are alex (admin) and sam (operator); the signed-in user is alex', async () => {
  const u = await call('/api/admin/users');
  assert.deepEqual(u.users.map((x) => [x.username, x.role]), [['alex', 'admin'], ['sam', 'operator']]);
  assert.equal((await call('/api/admin/auth')).current_user, 'alex');
});

test('settings carry the catalog and values', async () => {
  const s = await call('/api/admin/settings');
  assert.ok(s.entries.length > 100 && s.groups.length > 5);
  assert.equal(s.values['manager.tower.enabled'], true);
  assert.ok(!JSON.stringify(s).includes('alexcore'));
});

test('system health is ok with every sample machine and the current release', async () => {
  const h = await call('/api/admin/system-health');
  assert.equal(h.overall, 'ok');
  assert.deepEqual(h.agents.map((a) => a.hostname), ['atlas.example', 'forge.example', 'studio.example', 'quill.example']);
  assert.equal(h.manager.version, 'v9.9.9');
  assert.equal(h.agent_update.outdated, 0);
});

test('jobs: live list, filters, paging and detail', async () => {
  const live = await call('/api/jobs?status=live&facets=1');
  assert.deepEqual(live.jobs.map((j) => j.status).sort(), ['queued', 'running']);
  const all = await call('/api/jobs?status=all&limit=3&offset=0');
  assert.equal(all.jobs.length, 3);
  assert.ok(all.total > 3);
  assert.ok((await call('/api/jobs?status=all&kind=tool_run')).jobs.every((j) => j.kind === 'tool_run'));
  assert.ok((await call('/api/jobs?status=all&user=sam')).jobs.every((j) => j.user === 'sam'));
  const one = await call(`/api/jobs/${all.jobs[0].id}`);
  assert.equal(one.job.id, all.jobs[0].id);
  assert.ok('spec' in one.job && Array.isArray(one.job.audit));
  assert.equal((await raw('/api/jobs/nope')).status, 404);
});

test('audit log pages, filters and settings', async () => {
  const page = await call('/api/admin/audit-log?limit=5&offset=0');
  assert.equal(page.entries.length, 5);
  assert.ok(page.total > 5);
  assert.ok(page.entries.every((e, i) => i === 0 || e.ts <= page.entries[i - 1].ts));
  assert.ok((await call('/api/admin/audit-log?actor=sam')).entries.every((e) => e.actor === 'sam'));
  assert.ok((await call('/api/admin/audit-log?q=forge')).entries.length >= 1);
  const stats = await call('/api/admin/audit-log/stats');
  assert.equal(stats.total, (await call('/api/admin/audit-log?limit=200')).total);
  const ev = await call('/api/admin/audit-log/events');
  assert.ok(ev.groups.length > 3 && ev.config.page_size === 25);
});

test('live-looking counters differ a minute later but keep their keys', async () => {
  for (const p of ['/api/admin/system-health', '/api/admin/stream-stats', '/api/admin/dbstats/sqlite', '/api/admin/gateway/flow']) {
    let a, b;
    for (let i = 0; i < 96; i++) {
      a = await call(p, NOW + i * 300000); b = await call(p, NOW + i * 300000 + 60000);
      if (JSON.stringify(a) !== JSON.stringify(b)) break;
    }
    assert.notDeepEqual(a, b, p);
    assert.deepEqual(keysOf(a), keysOf(b), p);
  }
});

test('backups, leftovers, telemetry and archive downloads', async () => {
  const b = await call('/api/admin/backup-status');
  assert.equal(b.backups.length, 14);
  assert.ok(b.last.ok && b.next_due_ts > b.last.ts);
  assert.ok(!JSON.stringify(b).includes('lsmenc-'));
  assert.equal((await raw(`/api/admin/backup-archive/${b.backups[0].file}`)).status, 403);
  const l = await call('/api/admin/stores/leftovers');
  assert.equal(l.ok, true);
  assert.equal((await call('/api/admin/telemetry')).ok, true);
});

test('tool runs, ticket and OpenClaw analytics', async () => {
  const runs = await call('/api/tools/runs?limit=100');
  assert.ok(runs.runs.length >= 3 && runs.totals.benchmark === 2 && runs.latest.autotune);
  assert.ok((await call('/api/tools/runs?tool=autotune')).runs.every((r) => r.tool === 'autotune'));
  const t = await call('/api/tool-ticket?tool=openclaw');
  assert.equal(t.ok, true);
  assert.ok(t.url.endsWith('/proxy/openclaw/'));
  const oc = await call('/api/openclaw/analytics');
  assert.ok(oc.agents.length >= 2 && oc.totals.sessions > 0 && oc.tool_attribution.length > 0);
});

test('provider model lists match the templates and the models each machine serves', async () => {
  const want = {
    llama: { 'Gemma-2-9B-it-Q6_K': ['forge.example'], 'Llama-3.1-8B-Instruct-Q8_0': ['atlas.example'],
      'Phi-3.5-mini-instruct-Q4_K_M': ['quill.example'], 'Qwen2.5-14B-Instruct-Q5_K_M': ['forge.example'],
      'Qwen2.5-32B-Instruct-Q4_K_M': ['atlas.example'] },
    lms: { 'Llama-3.3-70B-Instruct-4bit': ['studio.example'], 'Qwen2.5-Coder-32B-Instruct-8bit': ['studio.example'] },
    vllm: { 'Mistral-7B-Instruct-v0.3': ['atlas.example'] },
  };
  for (const prov of Object.keys(want)) {
    const p = `/api/admin/${prov}-models`;
    const out = await call(p);
    const file = join(ROOT, 'templates', `GET_${p.replace(/[^A-Za-z0-9]/g, '_')}.json`);
    assertCompatible(out, JSON.parse(readFileSync(file, 'utf8')).body);
    assert.equal(out.ok, true);
    assert.deepEqual(out.errors, []);
    assert.deepEqual(Object.fromEntries(out.models.map((m) => [m.id, m.agents])), want[prov], prov);
    assert.deepEqual(out.models.map((m) => m.id), Object.keys(want[prov]).sort(), prov);
  }
});
