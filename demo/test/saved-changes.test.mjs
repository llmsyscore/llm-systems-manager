import test from 'node:test';
import assert from 'node:assert/strict';
import { load } from './load.mjs';

const NOW = Date.UTC(2026, 0, 1, 12, 0, 0);
const BASE = ['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'demo-state.js'];

// A fresh worker context per test, so saved changes never leak between tests.
function worker(handlers) {
  const sent = [];
  const self = load([...BASE, ...handlers.map((h) => `handlers/${h}`)],
    { clients: { get: async () => ({ postMessage: (m) => sent.push(m) }) } });
  const send = async (method, path, body, now = NOW) => {
    const init = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) init.body = JSON.stringify(body);
    const res = await self.DemoRouter.respond(new self.Request('http://demo.example' + path, init), now, 'c1');
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) { json = text; }
    return { status: res.status, body: json };
  };
  const get = async (path, now) => (await send('GET', path, undefined, now)).body;
  return { self, send, get, sent };
}
const refusals = (sent) => sent.filter((m) => m.type === 'demo-refused');

test('model profiles: save, activate, rename and delete are kept and listed', async () => {
  const w = worker(['overall.js', 'models.js']);
  const id = 'Llama-3.1-8B-Instruct-Q8_0';
  const enc = encodeURIComponent(id);
  const saved = await w.send('POST', `/api/llm/profiles/${enc}/save?agent=atlas`,
    { name: 'chat', values: { 'ctx-size': '8192' }, make_active: true });
  assert.equal(saved.status, 200);
  assert.equal(saved.body.model.active, 'chat');
  let p = (await w.get('/api/llm/profiles?agent=atlas'))[id];
  assert.deepEqual(Object.keys(p.profiles).sort(), ['chat', 'default']);
  assert.equal(p.active, 'chat');

  const act = await w.send('POST', `/api/llm/profiles/${enc}/activate?agent=atlas`, { name: 'default' });
  assert.equal(act.body.ok, true);
  assert.equal(act.body.values['n-gpu-layers'], '99');
  assert.equal((await w.get('/api/llm/profiles?agent=atlas'))[id].active, 'default');

  assert.equal((await w.send('POST', `/api/llm/profiles/${enc}/rename?agent=atlas`, { from: 'chat', to: 'short chat' })).status, 200);
  assert.equal((await w.send('POST', `/api/llm/profiles/${enc}/rename?agent=atlas`, { from: 'short chat', to: 'default' })).status, 409);
  assert.equal((await w.send('POST', `/api/llm/profiles/${enc}/rename?agent=atlas`, { from: 'nope', to: 'x' })).status, 404);
  p = (await w.get('/api/llm/profiles?agent=atlas'))[id];
  assert.deepEqual(Object.keys(p.profiles).sort(), ['default', 'short chat']);

  await w.send('POST', `/api/llm/profiles/${enc}/activate?agent=atlas`, { name: 'short chat' });
  const del = await w.send('POST', `/api/llm/profiles/${enc}/delete?agent=atlas`, { name: 'short chat' });
  assert.equal(del.body.model.active, 'default', 'deleting the active profile falls back to default');
  assert.equal((await w.send('POST', `/api/llm/profiles/${enc}/delete?agent=atlas`, { name: 'default' })).status, 409);
  assert.equal((await w.send('POST', `/api/llm/profiles/${enc}/activate?agent=atlas`, { name: 'gone' })).status, 404);
  assert.equal((await w.send('POST', `/api/llm/profiles/${enc}/save?agent=atlas`, { name: ' ' })).status, 400);
  assert.equal((await w.send('POST', `/api/llm/profiles/${enc}/save?agent=atlas`, { name: 'x'.repeat(65) })).status, 400);
  assert.deepEqual(refusals(w.sent), []);
  assert.deepEqual(Object.keys((await w.get('/api/llm/profiles?agent=forge'))['Qwen2.5-14B-Instruct-Q5_K_M'].profiles), ['default'],
    'other machines keep their own profiles');
});

test('activating a profile writes the model settings, which read back', async () => {
  const w = worker(['overall.js', 'models.js']);
  const id = 'Gemma-2-9B-it-Q6_K';
  const cfg = await w.get('/api/llm/config?agent=forge');
  cfg[id] = { ...cfg[id], 'ctx-size': '4096' };
  assert.equal((await w.send('POST', '/api/llm/config?agent=forge', cfg)).body.ok, true);
  assert.equal((await w.get('/api/llm/config?agent=forge'))[id]['ctx-size'], '4096');
  assert.equal((await w.get('/api/llm/config?agent=atlas'))[id], undefined);
  const al = await w.send('POST', '/api/llm/aliases', { model_id: id, alias: 'Quick answers' });
  assert.equal(al.body.aliases[id], 'Quick answers');
  await w.send('POST', '/api/llm/aliases', { model_id: 'Llama-3.1-8B-Instruct-Q8_0', alias: '' });
  const aliases = await w.get('/api/llm/aliases');
  assert.equal(aliases[id], 'Quick answers');
  assert.equal(aliases['Llama-3.1-8B-Instruct-Q8_0'], undefined, 'an empty nickname removes it');
  assert.deepEqual(refusals(w.sent), []);
  assert.equal((await w.send('POST', '/api/llm/load', { model: id })).status, 403, 'loading a model stays refused');
});

test('Tower insights: dismiss, dismiss all and apply are kept; the badge follows', async () => {
  const w = worker(['alarms.js', 'tower.js']);
  assert.equal((await w.get('/api/tower/state')).insights_new, 1);
  assert.equal((await w.send('POST', '/api/tower/insights/demo-ins-1/dismiss')).status, 200);
  let ins = (await w.get('/api/tower/insights')).insights;
  assert.equal(ins.find((r) => r.id === 'demo-ins-1').status, 'dismissed');
  const st = await w.get('/api/tower/state');
  assert.equal(st.insights_new, 0);
  assert.notEqual(st.latest_insight.id, 'demo-ins-1');
  assert.equal((await w.send('POST', '/api/tower/insights/demo-ins-1/dismiss')).status, 409);
  assert.equal((await w.send('POST', '/api/tower/insights/nope/dismiss')).status, 404);

  const ap = await w.send('POST', '/api/tower/insights/demo-ins-2/apply');
  assert.equal(ap.body.status, 'applied');
  ins = (await w.get('/api/tower/insights')).insights;
  assert.equal(ins.find((r) => r.id === 'demo-ins-2').applied_by, 'tower via alex');

  const all = await w.send('POST', '/api/tower/insights/dismiss_all');
  assert.equal(all.body.dismissed, 2);
  ins = (await w.get('/api/tower/insights')).insights;
  assert.ok(ins.every((r) => r.status === 'dismissed'));
  assert.equal((await w.get('/api/tower/state')).latest_insight, null);
  assert.deepEqual(refusals(w.sent), []);
});

test('Tower conversations: rename and delete are kept; stopping a run is quiet', async () => {
  const w = worker(['alarms.js', 'tower.js']);
  const ren = await w.send('PATCH', '/api/tower/threads/demo-earlier', { title: 'Busiest machine' });
  assert.equal(ren.body.thread.title, 'Busiest machine');
  assert.equal((await w.get('/api/tower/threads')).threads[0].title, 'Busiest machine');
  assert.equal((await w.get('/api/tower/threads/demo-earlier')).thread.title, 'Busiest machine');
  assert.equal((await w.send('PATCH', '/api/tower/threads/demo-earlier', { title: '  ' })).status, 400);
  assert.equal((await w.send('PATCH', '/api/tower/threads/demo-t5', { title: 'Mine' })).body.thread.title, 'Mine');
  assert.equal((await w.get('/api/tower/threads/demo-t5')).thread.title, 'Mine');
  assert.equal((await w.send('DELETE', '/api/tower/threads/demo-earlier')).status, 200);
  assert.deepEqual((await w.get('/api/tower/threads')).threads, []);
  assert.equal((await w.send('GET', '/api/tower/threads/demo-earlier')).status, 404);
  assert.equal((await w.send('POST', '/api/tower/runs/demo-run-slow-1/stop')).status, 200);
  assert.deepEqual(refusals(w.sent), []);
});

test('alarms: acknowledge, close and bulk are kept; ignore stays refused', async () => {
  const w = worker(['alarms.js']);
  const open = await w.get('/api/alarm/alerts?status=active');
  assert.equal(open.length, 2);
  const [a, b] = open.map((x) => x.alert_id);
  assert.equal((await w.send('POST', `/api/alarm/alerts/${a}/acknowledge`)).status, 200);
  let one = await w.get(`/api/alarm/alerts/${a}`);
  assert.equal(one.status, 'active');
  assert.equal(one.acknowledged_by, 'alex');
  assert.ok(one.acknowledged_at);
  assert.equal((await w.send('POST', `/api/alarm/alerts/${b}/close`)).status, 200);
  one = await w.get(`/api/alarm/alerts/${b}`);
  assert.equal(one.status, 'closed');
  assert.equal(one.resolution_reason, 'manual');
  assert.equal((await w.send('POST', `/api/alarm/alerts/${b}/acknowledge`)).status, 409);
  assert.equal(w.self.DemoAlarms.openAlerts(NOW).length, 1);
  assert.equal((await w.send('POST', '/api/alarm/alerts/nope/close')).status, 404);
  const bulk = await w.send('POST', '/api/alarm/alerts/bulk', { alert_ids: [a, 'nope'], action: 'close' });
  assert.equal(JSON.stringify(bulk.body), JSON.stringify({ status: 'ok', action: 'close', updated: 1, failed: ['nope'] }));
  assert.equal((await w.get('/api/alarm/alerts?status=active')).length, 0);
  assert.deepEqual(refusals(w.sent), []);
  assert.equal((await w.send('POST', '/api/alarm/alerts/bulk', { alert_ids: [a], action: 'ignore', duration_hours: 1 })).status, 403);
  assert.equal((await w.send('POST', `/api/alarm/alerts/${a}/ignore`, { duration_hours: 1 })).status, 403);
  assert.equal(refusals(w.sent).length, 2);
});

test('alarms: rule and notification switches are kept; full edits stay refused', async () => {
  const w = worker(['alarms.js']);
  const rule = (await w.get('/api/alarm/rules')).find((r) => r.name === 'GPU temperature warning');
  const t = await w.send('PATCH', `/api/alarm/rules/${rule.rule_id}/toggle`);
  assert.equal(JSON.stringify(t.body), JSON.stringify({ rule_id: rule.rule_id, enabled: false, message: 'Rule disabled' }));
  assert.equal((await w.get('/api/alarm/rules')).find((r) => r.rule_id === rule.rule_id).enabled, false);
  const hot = (await w.get('/api/alarm/alerts?include_closed=true')).find((x) => x.rule_id === rule.rule_id && x.source_host === 'forge.example');
  assert.equal(hot.status, 'closed', 'turning a rule off closes its open alert');
  assert.equal(hot.resolution_reason, 'rule disabled');
  await w.send('PATCH', `/api/alarm/rules/${rule.rule_id}/toggle`);
  assert.equal((await w.get('/api/alarm/rules')).find((r) => r.rule_id === rule.rule_id).enabled, true);

  const ch = (await w.get('/api/alarm/notifications/channels'))[1];
  const put = await w.send('PUT', `/api/alarm/notifications/channels/${ch.channel_id}`, { enabled: false });
  assert.equal(put.body.enabled, false);
  assert.equal((await w.get('/api/alarm/notifications/channels'))[1].enabled, false);
  const cfg = (await w.get('/api/alarm/notifications/configs'))[0];
  await w.send('PUT', `/api/alarm/notifications/configs/${cfg.config_id}`, { enabled: false });
  assert.equal((await w.get('/api/alarm/notifications/configs'))[0].enabled, false);
  assert.deepEqual(refusals(w.sent), []);
  assert.equal((await w.send('PUT', `/api/alarm/notifications/channels/${ch.channel_id}`, { name: 'x', enabled: true })).status, 403);
  assert.equal((await w.send('PUT', `/api/alarm/rules/${rule.rule_id}`, { name: 'x' })).status, 403);
  assert.equal(refusals(w.sent).length, 2);
});

test('forecast dismiss and the refresh choice are kept', async () => {
  const w = worker(['overall.js']);
  assert.equal((await w.send('POST', '/api/forecast/demo-fc-1/dismiss')).status, 200);
  const f = await w.get('/api/forecast');
  assert.deepEqual(f.findings.map((x) => x.id), ['demo-fc-2']);
  assert.equal(f.cleared[0].id, 'demo-fc-1');
  assert.equal(f.cleared[0].status, 'dismissed');
  assert.equal(f.last_result, '1 finding');
  assert.doesNotMatch(f.digest, /forge/);
  assert.equal((await w.send('POST', '/api/forecast/nope/dismiss')).status, 404);

  await w.send('POST', '/api/config/interval', { mode: 'manual', value: 2 });
  let c = await w.get('/api/config');
  assert.deepEqual([c.interval_mode, c.interval_override, c.poll_interval], ['manual', 5, 5]);
  await w.send('POST', '/api/config/interval', { mode: 'manual', value: 45 });
  assert.equal((await w.get('/api/config')).poll_interval, 45);
  await w.send('POST', '/api/config/interval', { mode: 'auto' });
  c = await w.get('/api/config');
  assert.deepEqual([c.interval_mode, c.interval_override], ['auto', null]);
  assert.deepEqual(refusals(w.sent), []);
});

test('Admin pool order and model pins are kept and read back from /api/agents', async () => {
  const w = worker(['overall.js']);
  const pool = async (p) => (await w.get('/api/agents')).global[p + '_pool'];
  const before = await pool('llama');
  assert.ok(before.length >= 3);
  const last = before[before.length - 1];
  const moved = await w.send('POST', `/api/agents/${last}/llama-pool`, { in_pool: true, position: 0 });
  assert.equal(moved.status, 200);
  assert.deepEqual(moved.body.llama_pool, [last, ...before.slice(0, -1)]);
  assert.deepEqual(await pool('llama'), [last, ...before.slice(0, -1)]);
  await w.send('POST', `/api/agents/${last}/llama-pool`, { in_pool: false });
  assert.deepEqual(await pool('llama'), before.slice(0, -1));
  assert.equal((await w.send('POST', '/api/agents/studio/llama-pool', { in_pool: true })).status, 400);
  assert.equal((await w.send('POST', '/api/agents/nope/llama-pool', { in_pool: true })).status, 404);
  assert.deepEqual(await pool('vllm'), (await w.get('/api/agents')).global.vllm_pool, 'other providers keep their pool');

  const pin = await w.send('POST', '/api/admin/llama-pins', { model_id: 'Gemma-2-9B-it-Q6_K', agent_id: 'forge' });
  assert.equal(pin.body.llama_model_pins['Gemma-2-9B-it-Q6_K'], 'forge');
  assert.equal((await w.get('/api/agents')).global.llama_model_pins['Gemma-2-9B-it-Q6_K'], 'forge');
  await w.send('POST', '/api/admin/llama-pins', { model_id: 'Gemma-2-9B-it-Q6_K', agent_id: '' });
  assert.equal((await w.get('/api/agents')).global.llama_model_pins['Gemma-2-9B-it-Q6_K'], undefined);
  assert.equal((await w.send('POST', '/api/admin/llama-pins', { model_id: 'x', agent_id: 'studio' })).status, 400);
  assert.deepEqual(refusals(w.sent), []);
  assert.equal((await w.send('POST', '/api/agents/atlas/disable', {})).status, 403, 'other Admin changes stay refused');
});
