import test from 'node:test';
import assert from 'node:assert/strict';
import { load } from './load.mjs';

const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'demo-state.js',
  'handlers/admin-settings-data.js', 'handlers/admin.js'], { DEMO_RELEASE: 'v9.9.9' });
const NOW = Date.UTC(2026, 0, 1, 12, 0, 0);

async function call(path, now = NOW) {
  const url = new URL(path, 'http://demo.example');
  const f = self.DemoRouter.find('GET', url);
  assert.ok(f, path);
  return JSON.parse(JSON.stringify(await f.route.handler({ now, params: f.params, query: url.searchParams, body: null, fleet: self.DemoFleet })));
}

test('the System Health jobs card lists the same live jobs as the Jobs page', async () => {
  for (const now of [NOW, NOW + 7 * 3600e3]) {
    const live = await call('/api/jobs?status=live', now);
    const h = (await call('/api/admin/system-health', now)).jobs;
    assert.deepEqual(h.rows.map((r) => r.id), live.jobs.map((j) => j.id));
    assert.deepEqual(h.rows, live.jobs);
    assert.equal(h.queued, live.summary.queued);
    assert.equal(h.running, live.summary.running);
    assert.equal(h.queued, live.jobs.filter((j) => j.status === 'queued').length);
    assert.equal(h.running, live.jobs.filter((j) => j.status === 'running').length);
    assert.equal(h.more, 0);
    assert.ok(h.queued >= 1 && h.running >= 1);
    for (const r of h.rows) assert.ok(r.label && r.kind_title && r.user && r.can_cancel, r.id);
  }
});

test('OpenClaw fills every panel with sessions from the last day', async () => {
  const d = await call('/api/openclaw/analytics');
  const s = Math.floor(NOW / 1000);
  assert.equal(d.ts, s);
  assert.ok(d.agents.length >= 3);
  const hosts = self.DemoFleet.machines.map((m) => m.id);
  const models = [].concat(...self.DemoFleet.machines.map((m) => m.models));
  for (const a of d.agents) {
    assert.ok(hosts.some((h) => a.name.startsWith(h)), a.name);
    assert.ok(a.models.length && a.models.every((m) => models.includes(m)), a.name);
    assert.ok(a.recent.length >= 3 && a.recent.length <= 10);
    for (const r of a.recent) {
      const last = Date.parse(r.last_ts) / 1000;
      assert.ok(last <= s && last > s - 86400, `${r.id} ${r.last_ts}`);
      assert.match(r.id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-a[0-9a-f]{3}-[0-9a-f]{12}$/);
      assert.ok(r.messages > 0 && r.input > 0 && r.output > 0 && r.cost === 0);
      assert.ok(!Object.keys(r).some((k) => k.startsWith('_')));
    }
    for (const k of ['messages', 'input', 'output', 'cacheRead', 'tool_uses']) {
      assert.equal(a[k], a.recent.reduce((t, r) => t + r[k], 0), `${a.name} ${k}`);
    }
  }
  for (const k of ['sessions', 'messages', 'input', 'output', 'cacheRead', 'cacheWrite', 'tool_uses']) {
    assert.equal(d.totals[k], d.agents.reduce((t, a) => t + a[k], 0), k);
  }
  assert.equal(d.totals.cost, 0);
  const st = d.tasks.by_status;
  assert.ok(st.succeeded > 0 && st.failed > 0 && st.running === 1);
  assert.equal(d.tasks.total, Object.values(st).reduce((a, b) => a + b, 0));
  assert.ok(d.tasks.recent_failures.length >= 2);
  for (const f of d.tasks.recent_failures) assert.ok(f.label && f.kind && f.status && f.error && f.created_iso);
  assert.ok(d.flows.recent.length >= 5);
  assert.equal(d.flows.total, Object.values(d.flows.by_status).reduce((a, b) => a + b, 0));
  assert.ok(d.flows.recent.some((f) => f.status === 'running' && f.duration_s === null));
  assert.ok(d.flows.recent.some((f) => f.status === 'failed'));
  assert.ok(d.flows.recent.every((f) => f.goal && f.created_iso));
  assert.ok(d.token_dist.total > 0 && d.token_dist.input_pct > 0 && d.token_dist.cache_hit_pct > 0);
  assert.ok(d.thinking.agents.length >= 1 && d.thinking.sessions.length >= 1);
  assert.ok(d.thinking.sessions.every((x) => x.agent && x.thinking_events > 0));
  assert.ok(d.velocity.active_sessions >= 1 && d.velocity.tokens_per_min > 0);
  assert.ok(d.trend.monthlyPrediction > 0 && d.trend.trend === 'stable');
  const pct = d.tool_attribution.reduce((t, x) => t + x.pct, 0);
  assert.ok(Math.abs(pct - 100) < 0.5, `${pct}`);
  assert.equal(d.tool_attribution.reduce((t, x) => t + x.count, 0), d.totals.tool_uses);
  assert.ok(d.tool_attribution.every((x) => d.tool_trends[x.tool]));
  assert.equal(Object.keys(d.daily_cost).length, 7);
  assert.equal(d.budget.monthly_budget_usd, null);
});

test('OpenClaw sessions stay put between polls and move on over the day', async () => {
  const a = await call('/api/openclaw/analytics', NOW);
  const b = await call('/api/openclaw/analytics', NOW + 60e3);
  assert.deepEqual(a.agents.map((x) => x.recent.map((r) => r.id)), b.agents.map((x) => x.recent.map((r) => r.id)));
  assert.deepEqual(a.totals, b.totals);
  const later = await call('/api/openclaw/analytics', NOW + 5 * 3600e3);
  const s = Math.floor(NOW / 1000) + 5 * 3600;
  assert.ok(later.agents.every((x) => Date.parse(x.last_ts) / 1000 > s - 86400));
  assert.notEqual(later.agents[0].last_ts, a.agents[0].last_ts);
});
