import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from './load.mjs';
import { assertCompatible } from './shape.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'demo-state.js', 'handlers/alarms.js', 'handlers/tower.js']);
const NOW = Date.UTC(2026, 0, 1, 12, 0, 0);

async function raw(method, path, body = null, now = NOW) {
  const url = new URL(path, 'http://demo.example');
  const found = self.DemoRouter.find(method, url);
  assert.ok(found, `no route for ${method} ${path}`);
  const ctx = { now, params: found.params, query: url.searchParams, body, fleet: self.DemoFleet };
  return found.route.handler(ctx);
}
async function call(path, now = NOW) { return JSON.parse(JSON.stringify(await raw('GET', path, null, now))); }

async function streamEvents(runId) {
  const res = await raw('GET', `/api/tower/runs/${runId}/stream`);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  const events = [];
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value);
    for (const part of buf.split('\n\n').slice(0, -1)) {
      assert.ok(part.startsWith('data: '), 'frames carry data only');
      events.push(JSON.parse(part.slice(6)));
    }
    buf = buf.split('\n\n').pop();
    if (events.length && events[events.length - 1].event === 'done') { await reader.cancel(); break; }
  }
  return events;
}
async function answerFor(text) {
  const out = await raw('POST', '/api/tower/threads/demo-t1/messages', { text });
  assert.equal(out.ok, true);
  const ev = await streamEvents(out.run_id);
  assert.equal(ev[0].event, 'status');
  assert.equal(ev[ev.length - 1].event, 'done');
  return ev.filter((e) => e.event === 'delta').map((e) => e.text).join('');
}

test('templated tower routes keep the captured shape', async () => {
  const file = join(ROOT, 'templates', 'GET__api_tower_state.json');
  assert.ok(existsSync(file));
  assertCompatible(await call('/api/tower/state'), JSON.parse(readFileSync(file, 'utf8')).body);
});

test('state has a model loaded, a finished capability check and a past latest insight', async () => {
  const st = await call('/api/tower/state');
  assert.equal(st.enabled, true);
  assert.ok(st.model && st.hosts.length === 1);
  assert.notEqual(st.check.grade, 'pending');
  assert.ok(st.latest_insight.created * 1000 < NOW);
});

test('insights: three canned rows tied to the sample alerts', async () => {
  const d = await call('/api/tower/insights');
  assert.equal(d.insights.length, 3);
  assert.equal(d.insights[0].host, 'forge.example');
  assert.equal(d.insights[0].severity, 'warning');
  assert.ok(d.insights[0].snapshot.points.length > 2);
  const ids = new Set(self.DemoAlarms.alerts(NOW).map((a) => a.alert_id));
  assert.ok(d.insights.every((i) => ids.has(i.alert_id)));
});

test('thread list and history hold one earlier conversation', async () => {
  const list = await call('/api/tower/threads');
  assert.equal(list.threads.length, 1);
  const t = await call(`/api/tower/threads/${list.threads[0].id}`);
  assert.deepEqual(t.messages.map((m) => m.role), ['user', 'tool', 'assistant']);
  assert.equal((await raw('GET', '/api/tower/threads/nope')).status, 404);
  const made = await raw('POST', '/api/tower/threads', { page: {} });
  assert.equal(made.ok, true);
  assert.equal((await call(`/api/tower/threads/${made.thread.id}`)).messages.length, 0);
});

test('keywords pick the canned answer; anything else gets the fallback', async () => {
  assert.match(await answerFor('Why is forge so slow?'), /forge\.example is the slow one/);
  assert.match(await answerFor('Any alarms open?'), /two open alarms/);
  assert.match(await answerFor('What does it cost?'), /costs the most/);
  assert.match(await answerFor('Which model is loaded?'), /Llama-3\.1-8B/);
  assert.equal(await answerFor('Tell me a joke'),
    'In the demo I can only answer a few sample questions. Install it to ask about your own machines.');
});

test('thread and message POSTs are answered as reads; other Tower writes are refused', async () => {
  const post = (path, body) => self.DemoRouter.respond(new self.Request('http://demo.example' + path,
    { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } }), NOW, null);
  assert.equal((await post('/api/tower/threads', {})).status, 200);
  assert.equal((await post('/api/tower/threads/demo-t1/messages', { text: 'hi' })).status, 200);
  assert.equal((await post('/api/tower/threads/demo-t1/messages', { text: ' ' })).status, 400);
  assert.equal((await post('/api/tower/actions/x/approve', {})).status, 403);
  const seen = await post('/api/tower/insights/seen', {});
  assert.equal(seen.status, 200);
  assert.equal((await seen.json()).ok, true);
  const park = await post('/api/tower/runs/demo-run-slow-1/park', {});
  assert.equal(park.status, 200);
  assert.deepEqual(await park.json(), { ok: true });
  assert.equal((await post('/api/tower/timers/x/cancel', {})).status, 403);
});

test('eval and models pages are populated', async () => {
  const e = await call('/api/tower/eval');
  assert.equal(e.results[0].passed, e.results[0].total);
  const m = await call('/api/tower/models');
  assert.ok(m.models.length >= 3 && m.hosts.length === 1);
  assert.deepEqual((await call('/api/tower/timers')).timers, []);
});

test('who makes it and how to get help have their own answers, with no tool call', async () => {
  const K = self.DemoTower.keyFor;
  for (const q of ['Who develops LLM Systems Manager?', 'Who makes this?', 'who builds it', 'Who maintains the project?', 'Who is behind LLM Systems Manager?']) {
    assert.equal(K(q), 'about', q);
  }
  for (const q of ['How do I get help?', 'Where is support?', 'How can I contact you?', 'How do I report a bug?']) {
    assert.equal(K(q), 'help', q);
  }
  assert.equal(K('Which machine is slow?'), 'slow');
  assert.equal(K('Is the alarm engine healthy?'), 'alarm');
  assert.equal(K('What models are loaded everywhere?'), 'model');
  const about = await answerFor('Who develops LLM Systems Manager?');
  assert.match(about, /LLM SysCore/);
  assert.match(about, /AGPL-3\.0/);
  assert.match(about, /github\.com\/llmsyscore\/llm-systems-manager/);
  assert.match(about, /own hardware/);
  const help = await answerFor('How do I get help?');
  for (const re of [/https:\/\/www\.llmsyscore\.com\/docs\.html/, /support@llmsyscore\.com/,
    /github\.com\/llmsyscore\/llm-systems-manager\/issues/, /security/i]) assert.match(help, re);
  for (const key of ['about', 'help']) {
    const out = await raw('POST', '/api/tower/threads/demo-t1/messages', { text: key === 'about' ? 'Who makes it?' : 'help' });
    assert.match(out.run_id, /^demo-run-([a-z]+)-/);
    assert.equal(/^demo-run-([a-z]+)-/.exec(out.run_id)[1], key, 'the analytics read the answer key from the run id');
    const ev = await streamEvents(out.run_id);
    assert.ok(!ev.some((e) => e.event === 'tool'), `${key} has no tool tick`);
  }
});

test('the dashboard\'s own help suggestions get the new answers', async () => {
  const src = readFileSync(join(ROOT, '..', 'llm-systems-manager', 'frontend', 'js', 'lib', 'tower-view.js'), 'utf8');
  const list = JSON.parse(/const HELP_SUGS = (\[[^\]]*\]);/.exec(src)[1].replace(/'/g, '"'));
  assert.deepEqual(list.map((q) => self.DemoTower.keyFor(q)), ['about', 'help']);
});
