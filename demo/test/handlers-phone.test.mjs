import test from 'node:test';
import assert from 'node:assert/strict';
import { load } from './load.mjs';

const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'demo-state.js', 'handlers/overall.js', 'handlers/phone.js'],
  { DEMO_RELEASE: 'v9.9.9' });
const NOW = Date.UTC(2026, 0, 1, 12, 0, 0);

async function get(path) {
  const url = new URL(path, 'http://demo.example');
  const found = self.DemoRouter.find('GET', url);
  assert.ok(found, `no route for GET ${path}`);
  return found.route.handler({ now: NOW, params: found.params, query: url.searchParams, body: null, fleet: self.DemoFleet });
}

test('the notification devices list has the admin roster the phone app shows', async () => {
  const s = JSON.parse(JSON.stringify(await get('/api/companion/push/subscriptions')));
  assert.equal(s.ok, true);
  assert.equal(s.count, s.devices.length);
  assert.equal(s.endpoints.length, s.count);
  for (const d of s.devices) {
    assert.match(d.endpoint, /^https:\/\/push\.example\//);
    assert.ok(d.ua && d.created < NOW / 1000);
  }
  assert.equal((await get('/api/companion/push/public-key')).ok, true);
});

test('agent logs: a tail of lines for the agent, and a stream of one line per tick', async () => {
  const t = await get('/api/agents/forge/log/tail');
  assert.equal(t.ok, true);
  assert.equal(t.lines.length, 20);
  assert.ok(t.lines.every((l) => l.includes('llm-systems-agent[forge]')));
  assert.ok((await get('/api/agents/nobody/log/tail')).lines[0].includes('[atlas]'));
  const res = await get('/api/agents/forge/log/stream');
  assert.equal(res.headers.get('Content-Type'), 'text/event-stream');
  const reader = res.body.getReader();
  const { value } = await reader.read();
  await reader.cancel();
  assert.match(new TextDecoder().decode(value), /^data: \d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO llm-systems-agent\[forge\]: .+\n\n$/);
});

test('changes from the phone app are refused like any other demo change', async () => {
  for (const [m, p] of [['POST', '/api/companion/push/subscribe'], ['POST', '/api/companion/push/unsubscribe'],
    ['POST', '/api/companion/push/test'], ['POST', '/api/agents/atlas/restart'], ['PUT', '/api/companion/release']]) {
    const res = await self.DemoRouter.answer(new Request('http://demo.example' + p, { method: m, body: '{}' }),
      new URL('http://demo.example' + p), m, NOW, null);
    assert.equal(res.status, 403, `${m} ${p}`);
  }
});
