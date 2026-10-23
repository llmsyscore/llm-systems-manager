import test from 'node:test';
import assert from 'node:assert/strict';
import { load } from './load.mjs';

// A Cache Storage stand-in shared between loads, so a second load acts like a restarted worker.
function fakeCaches() {
  const stores = new Map();
  return {
    stores,
    keys: async () => [...stores.keys()],
    delete: async (n) => stores.delete(n),
    open: async (n) => {
      if (!stores.has(n)) stores.set(n, new Map());
      const m = stores.get(n);
      const key = (r) => (typeof r === 'string' ? r : r.url);
      return {
        match: async (r) => { const v = m.get(key(r)); return v ? v.clone() : undefined; },
        put: async (r, res) => { m.set(key(r), res); },
      };
    },
  };
}

test('saved changes survive a worker restart', async () => {
  const caches = fakeCaches();
  const a = load(['demo-state.js'], { caches });
  await a.DemoState.ready;
  await a.DemoState.set('layout', { overall: { order: ['agents', 'hero'] } });
  await a.DemoState.set('interval', 30);
  assert.deepEqual([...caches.stores.keys()], ['lsmdemo-state']);
  assert.ok(!'lsmdemo-state'.startsWith('lsm-demo-'), 'a new release must not delete the saved changes');
  const b = load(['demo-state.js'], { caches });
  await b.DemoState.ready;
  assert.equal(JSON.stringify(b.DemoState.get('layout')), '{"overall":{"order":["agents","hero"]}}');
  assert.equal(b.DemoState.get('interval'), 30);
  assert.equal(b.DemoState.get('missing', 'none'), 'none');
});

test('a change made before the saved copy is read keeps the newer value', async () => {
  const caches = fakeCaches();
  const a = load(['demo-state.js'], { caches });
  await a.DemoState.set('k', 'old');
  await a.DemoState.set('other', 1);
  const b = load(['demo-state.js'], { caches });
  const write = b.DemoState.set('k', 'new');
  await b.DemoState.ready;
  await write;
  assert.equal(b.DemoState.get('k'), 'new');
  assert.equal(b.DemoState.get('other'), 1);
  const c = load(['demo-state.js'], { caches });
  await c.DemoState.ready;
  assert.equal(c.DemoState.get('k'), 'new');
});

test('blocked storage keeps changes in memory and never rejects', async () => {
  const caches = { open: () => Promise.reject(new Error('SecurityError')) };
  const s = load(['demo-state.js'], { caches });
  await s.DemoState.ready;
  await s.DemoState.set('x', { a: 1 });
  assert.equal(s.DemoState.get('x').a, 1);
  const none = load(['demo-state.js']);
  await none.DemoState.set('y', 2);
  assert.equal(none.DemoState.get('y'), 2);
});

test('get hands out copies, so a handler cannot change saved state by accident', async () => {
  const s = load(['demo-state.js']);
  await s.DemoState.set('obj', { list: [1] });
  s.DemoState.get('obj').list.push(2);
  assert.equal(s.DemoState.get('obj').list.length, 1);
});

test('a read route can refuse one request; the page gets the refusal message and toast', async () => {
  const sent = [];
  const self = load(['demo-router.js', 'demo-state.js'],
    { clients: { get: async () => ({ postMessage: (m) => sent.push(m) }) } });
  self.DemoRouter.route('PUT /api/thing/:id', (ctx) => (ctx.body && ctx.body.enabled !== undefined ? { ok: true } : self.DemoRouter.REFUSE), { read: true });
  const put = (body) => self.DemoRouter.respond(new self.Request('http://demo.example/api/thing/1',
    { method: 'PUT', body: JSON.stringify(body) }), 0, 'client-1');
  assert.equal((await put({ enabled: true })).status, 200);
  const res = await put({ name: 'x' });
  assert.equal(res.status, 403);
  assert.deepEqual(await res.json(), { ok: false, error: 'Not available in the demo' });
  assert.equal(JSON.stringify(sent), '[{"type":"demo-refused","key":"PUT /api/thing/:id"}]');
});

test('the worker loads the saved-changes helper before the handlers', async () => {
  const imported = [];
  const ctx = { addEventListener() {}, location: new URL('https://demo.example/demo-sw.js'),
    DEMO_HANDLERS: ['/demo/handlers/a.js'], importScripts: (...u) => imported.push(...u) };
  ctx.self = ctx;
  const { readFileSync } = await import('node:fs');
  const vm = await import('node:vm');
  vm.runInNewContext(readFileSync(new URL('../runtime/demo-sw.js', import.meta.url), 'utf8'), ctx);
  const i = imported.findIndex((u) => u.startsWith('/demo/demo-state.js'));
  assert.ok(i >= 0 && i < imported.findIndex((u) => u.startsWith('/demo/handlers/')), imported.join(' '));
});

test('a new release starts with no saved changes; the same release keeps them', async () => {
  const caches = fakeCaches();
  const a = load(['demo-state.js'], { caches, DEMO_RELEASE: 'v1.0.0' });
  await a.DemoState.ready;
  await a.DemoState.set('layout', { x: 1 });
  const same = load(['demo-state.js'], { caches, DEMO_RELEASE: 'v1.0.0' });
  await same.DemoState.ready;
  assert.equal(same.DemoState.get('layout').x, 1);
  const next = load(['demo-state.js'], { caches, DEMO_RELEASE: 'v1.1.0' });
  await next.DemoState.ready;
  assert.equal(next.DemoState.get('layout'), undefined);
  const raw = await (await (await caches.open('lsmdemo-state')).match('/demo-state/all')).json();
  assert.equal(raw.release, 'v1.0.0');
  await next.DemoState.set('interval', 30);
  const after = await (await (await caches.open('lsmdemo-state')).match('/demo-state/all')).json();
  assert.equal(JSON.stringify(after), '{"release":"v1.1.0","data":{"interval":30}}');
  for (const junk of ['{"layout":{"x":1}}', '{"release":"v1.1.0","data":"bad"}', 'not json']) {
    const c = fakeCaches();
    await (await c.open('lsmdemo-state')).put('/demo-state/all', new Response(junk));
    const s = load(['demo-state.js'], { caches: c, DEMO_RELEASE: 'v1.1.0' });
    await s.DemoState.ready;
    assert.equal(s.DemoState.get('layout'), undefined, junk);
  }
});

test('storage that never answers delays requests by at most about 1.5 s', async () => {
  const caches = { open: () => new Promise(() => {}) };
  const self = load(['demo-router.js', 'demo-state.js'], { caches });
  self.DemoRouter.route('GET /api/x', () => ({ ok: true, v: self.DemoState.get('v', 'default') }));
  const t0 = Date.now();
  const res = await self.DemoRouter.respond(new self.Request('http://demo.example/api/x'), 0, null);
  const ms = Date.now() - t0;
  assert.equal(res.status, 200);
  assert.equal((await res.json()).v, 'default');
  assert.ok(ms >= 1400 && ms < 2500, `${ms} ms`);
  const t1 = Date.now();
  await self.DemoState.set('v', 'kept');
  assert.ok(Date.now() - t1 < 2500, 'a write that never lands gives up too');
  assert.equal(self.DemoState.get('v'), 'kept', 'changes still work from memory');
});

test('handlers fall back to defaults when the saved-changes file is missing', async () => {
  const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js',
    'handlers/alarms.js', 'handlers/overall.js', 'handlers/models.js', 'handlers/tower.js']);
  assert.equal(self.DemoState, undefined);
  for (const p of ['/api/layout', '/api/config', '/api/forecast', '/api/agents', '/api/llm/profiles', '/api/llm/config',
    '/api/llm/aliases', '/api/tower/insights', '/api/tower/threads', '/api/tower/state', '/api/alarm/rules', '/api/alarm/alerts']) {
    const res = await self.DemoRouter.respond(new self.Request('http://demo.example' + p), Date.UTC(2026, 0, 1), null);
    assert.equal(res.status, 200, p);
  }
  const post = await self.DemoRouter.respond(new self.Request('http://demo.example/api/layout',
    { method: 'POST', body: '{"a":1}' }), 0, null);
  assert.equal(post.status, 200);
});
