import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from './load.mjs';

const BASE = 'http://demo.example';
const REFUSAL = { ok: false, error: 'Not available in the demo' };

function routerEnv() {
  const posted = [];
  const clients = { get: async (id) => ({ postMessage: (m) => posted.push({ id, m }) }) };
  const self = load(['demo-router.js'], { clients });
  return { self, posted, R: self.DemoRouter };
}

test('GET match returns key and :id params', () => {
  const { R } = routerEnv();
  R.route('GET /api/agents/:id/info', () => ({}));
  const m = R.match('GET', new URL(`${BASE}/api/agents/tower-1/info`));
  assert.equal(m.key, 'GET /api/agents/:id/info');
  assert.deepEqual({ ...m.params }, { id: 'tower-1' });
  assert.equal(R.match('POST', new URL(`${BASE}/api/agents/tower-1/info`)), null);
  assert.deepEqual([...R.keys()], ['GET /api/agents/:id/info']);
});

test('handler gets query, params and now; objects become JSON 200', async () => {
  const { R } = routerEnv();
  R.route('GET /api/agents/:id/history', (ctx) => ({
    id: ctx.params.id, range: ctx.query.get('range'), n: ctx.query.get('n'), now: ctx.now }));
  const res = await R.respond(new Request(`${BASE}/api/agents/a1/history?range=1h&n=5`), 1234);
  assert.equal(res.status, 200);
  assert.match(res.headers.get('content-type'), /application\/json/);
  assert.deepEqual(await res.json(), { id: 'a1', range: '1h', n: '5', now: 1234 });
});

test('returned Response passes through', async () => {
  const { R } = routerEnv();
  R.route('GET /api/raw', () => new Response('hi', { status: 201 }));
  const res = await R.respond(new Request(`${BASE}/api/raw`), 0);
  assert.equal(res.status, 201);
  assert.equal(await res.text(), 'hi');
});

test('POST without read is refused with 403 and a demo-refused message', async () => {
  const { R, posted } = routerEnv();
  let called = false;
  R.route('POST /api/x', () => { called = true; return {}; });
  const res = await R.respond(new Request(`${BASE}/api/x`, { method: 'POST', body: '{}' }), 0, 'c1');
  assert.equal(res.status, 403);
  assert.deepEqual(await res.json(), REFUSAL);
  assert.equal(called, false);
  assert.deepEqual(posted.map((p) => ({ id: p.id, m: { ...p.m } })),
    [{ id: 'c1', m: { type: 'demo-refused', key: 'POST /api/x' } }]);
});

test('unmatched write is refused with 403, not a miss', async () => {
  const { R, posted } = routerEnv();
  const res = await R.respond(new Request(`${BASE}/api/agents/a1/restart`, { method: 'DELETE' }), 0, 'c3');
  assert.equal(res.status, 403);
  assert.deepEqual(await res.json(), REFUSAL);
  assert.deepEqual([...R.misses], []);
  assert.deepEqual(posted.map((p) => ({ id: p.id, m: { ...p.m } })),
    [{ id: 'c3', m: { type: 'demo-refused', key: 'DELETE /api/agents/a1/restart' } }]);
});

test('read: true POST is answered by the handler with the parsed body', async () => {
  const { R } = routerEnv();
  R.route('POST /alarm/api/metrics/batch', (ctx) => ({ got: ctx.body.ids }), { read: true });
  const req = new Request(`${BASE}/alarm/api/metrics/batch`, {
    method: 'POST', body: JSON.stringify({ ids: [1, 2] }), headers: { 'Content-Type': 'application/json' } });
  const res = await R.respond(req, 0);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { got: [1, 2] });
});

test('unknown path is a 404 listed in misses with a demo-miss message', async () => {
  const { R, posted } = routerEnv();
  const res = await R.respond(new Request(`${BASE}/api/nope?x=1`), 0, 'c2');
  assert.equal(res.status, 404);
  assert.deepEqual(await res.json(), { ok: false, error: 'demo: no answer' });
  assert.deepEqual([...R.misses], ['GET /api/nope']);
  assert.deepEqual({ ...posted[0].m }, { type: 'demo-miss', method: 'GET', path: '/api/nope' });
});

test('DemoSse.stream emits data frames with i 0 and 1', async () => {
  const self = load(['demo-sse.js']);
  const res = self.DemoSse.stream((now, i) => ({ data: { i } }), 5);
  assert.equal(res.headers.get('content-type'), 'text/event-stream');
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  const a = dec.decode((await reader.read()).value);
  const b = dec.decode((await reader.read()).value);
  await reader.cancel();
  assert.equal(a, 'data: {"i":0}\n\n');
  assert.equal(b, 'data: {"i":1}\n\n');
});

test('DemoSse.stream skips a tick whose generator throws (logging once) and skips ticks while the reader is behind', async () => {
  const errors = [];
  const self = load(['demo-sse.js'], { console: { ...console, error: (...a) => errors.push(a) } });
  const calls = [];
  const res = self.DemoSse.stream((now, i) => {
    calls.push(i);
    if (i === 1 || i === 2) throw new Error('bad tick');
    return { data: { i } };
  }, 5);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  assert.equal(dec.decode((await reader.read()).value), 'data: {"i":0}\n\n');
  assert.equal(dec.decode((await reader.read()).value), 'data: {"i":3}\n\n');
  assert.equal(errors.length, 1);
  const before = calls.length;
  await new Promise((r) => setTimeout(r, 80));
  assert.ok(calls.length - before <= 1, `generator ran ${calls.length - before} times with no reader`);
  await reader.cancel();
});

function swEnv(cacheNames, { redirected = false } = {}) {
  const stores = new Map(cacheNames.map((n) => [n, new Map()]));
  const caches = {
    keys: async () => [...stores.keys()],
    delete: async (n) => stores.delete(n),
    open: async (n) => {
      if (!stores.has(n)) stores.set(n, new Map());
      const m = stores.get(n);
      const key = (r) => (typeof r === 'string' ? r : r.url);
      return {
        match: async (r) => {
          const v = m.get(key(r));
          if (!v) return undefined;
          const c = v.clone();
          if (v.redirected) Object.defineProperty(c, 'redirected', { value: true });
          return c;
        },
        put: async (r, res) => { m.set(key(r), res); },
      };
    },
  };
  let claimed = false;
  const clients = { claim: async () => { claimed = true; }, get: async () => null };
  const fetched = [];
  const answer = (url) => {
    if (url.endsWith('/dashboard')) {
      return new Response('<html><script src="/static/js/app.js?v=abc"></script></html>');
    }
    if (url.endsWith('/phone')) return new Response('<html><script src="/static/js/companion.js?v=def"></script></html>');
    if (url.endsWith('/alarm/')) return new Response('<html><script src="js/ae.js"></script></html>');
    return new Response(`asset ${url}`);
  };
  const fetch = async (r) => {
    const url = typeof r === 'string' ? new URL(r, 'http://demo.example/').href : r.url;
    fetched.push(url);
    const res = answer(url);
    if (redirected) Object.defineProperty(res, 'redirected', { value: true });
    return res;
  };
  const self = load(['demo-sw.js'], {
    DEMO_RELEASE: 'v9.9.9', DEMO_HANDLERS: [], caches, clients, fetch });
  return { self, stores, fetched, claimed: () => claimed };
}

async function fire(self, type, extra = {}) {
  const waits = [];
  let answer;
  const event = { waitUntil: (p) => waits.push(p), respondWith: (p) => { answer = p; }, ...extra };
  for (const fn of self.listeners[type]) fn(event);
  await Promise.all(waits);
  return answer && (await answer);
}

test('activate deletes old lsm-demo caches, keeps this release and claims clients', async () => {
  const { self, stores, claimed } = swEnv(['lsm-demo-old', 'lsm-demo-v9.9.9', 'other']);
  await fire(self, 'activate');
  assert.deepEqual([...stores.keys()].sort(), ['lsm-demo-v9.9.9', 'lsmdemo-state', 'other']);
  assert.equal(claimed(), true);
});

test('install skips waiting and pre-caches the pages and their assets', async () => {
  const { self, stores } = swEnv([]);
  await fire(self, 'install');
  assert.equal(self.skipped, true);
  assert.deepEqual([...stores.get('lsm-demo-v9.9.9').keys()].sort(), [
    'http://demo.example/alarm/', 'http://demo.example/alarm/js/ae.js',
    'http://demo.example/dashboard', 'http://demo.example/phone',
    'http://demo.example/static/js/app.js?v=abc', 'http://demo.example/static/js/companion.js?v=def']);
});

test('fetch serves the cached phone app for /phone and /phone/', async () => {
  const { self, fetched } = swEnv([]);
  await fire(self, 'install');
  const before = fetched.length;
  for (const u of ['http://demo.example/phone', 'http://demo.example/phone/']) {
    const nav = await fire(self, 'fetch', { request: { url: u, method: 'GET', mode: 'navigate' } });
    assert.match(await nav.text(), /companion\.js\?v=def/, u);
  }
  assert.equal(fetched.length, before);
});

test('fetch serves cached dashboard for /dashboard, routes /api, passes other origins', async () => {
  const { self, fetched } = swEnv([]);
  await fire(self, 'install');
  const nav = await fire(self, 'fetch', {
    request: { url: 'http://demo.example/dashboard', method: 'GET', mode: 'navigate' } });
  assert.match(await nav.text(), /app\.js\?v=abc/);
  const api = await fire(self, 'fetch', { request: new Request('http://demo.example/api/none') });
  assert.equal(api.status, 404);
  const before = fetched.length;
  const other = await fire(self, 'fetch', { request: new Request('http://elsewhere.example/x.js') });
  assert.equal(other, undefined);
  const st = await fire(self, 'fetch', { request: new Request('http://demo.example/static/js/app.js?v=abc') });
  assert.equal(await st.text(), 'asset http://demo.example/static/js/app.js?v=abc');
  assert.equal(fetched.length, before);
});

test('fetch answers tool frames under /proxy/ with the placeholder and routes /tools-ping', async () => {
  const { self, fetched } = swEnv([]);
  self.importScripts('/demo/handlers/admin.js');
  for (const u of ['http://demo.example/proxy/openclaw/', 'http://demo.example/proxy/llmchat/',
    'http://demo.example/proxy/imggen/assets/app.js']) {
    const res = await fire(self, 'fetch', { request: { url: u, method: 'GET', mode: 'navigate' } });
    assert.equal(await res.text(), 'asset http://demo.example/demo/placeholder', u);
  }
  assert.ok(!fetched.some((u) => u.includes('/proxy/')));
  const ping = await fire(self, 'fetch', { request: new Request('http://demo.example/tools-ping') });
  assert.equal(ping.status, 200);
  assert.deepEqual(await ping.json(), { ok: true });
});

test('fetch never answers a navigation with a redirected response', async () => {
  const { self, fetched } = swEnv([], { redirected: true });
  await fire(self, 'install');
  assert.ok(new Set(fetched).has('http://demo.example/dashboard'));
  assert.ok(!fetched.some((u) => new URL(u).pathname.endsWith('.html')));
  const nav = await fire(self, 'fetch', {
    request: { url: 'http://demo.example/dashboard', method: 'GET', mode: 'navigate' } });
  assert.equal(nav.redirected, false);
  assert.match(await nav.text(), /app\.js\?v=abc/);
  const frame = await fire(self, 'fetch', {
    request: { url: 'http://demo.example/proxy/openclaw/', method: 'GET', mode: 'navigate' } });
  assert.equal(frame.redirected, false);
  assert.equal(await frame.text(), 'asset http://demo.example/demo/placeholder');
  const cold = swEnv([], { redirected: true });
  const direct = await fire(cold.self, 'fetch', {
    request: { url: 'http://demo.example/dashboard', method: 'GET', mode: 'navigate' } });
  assert.equal(direct.redirected, false);
});

test('a throwing handler or a malformed path answers 500 JSON and posts demo-error', async () => {
  const { R, posted } = routerEnv();
  R.route('GET /api/boom', () => { throw new Error('x'); });
  R.route('GET /api/later', () => Promise.reject(new Error('y')));
  R.route('GET /api/agents/:id', () => ({}));
  const FAILED = { ok: false, error: 'demo: handler error' };
  for (const [path, client] of [['/api/boom', 'c7'], ['/api/later', 'c8'], ['/api/agents/%E0%A4%A', 'c9']]) {
    const res = await R.respond(new Request(`${BASE}${path}`), 0, client);
    assert.equal(res.status, 500, path);
    assert.deepEqual(await res.json(), FAILED);
  }
  assert.deepEqual(posted.map((p) => ({ id: p.id, m: { ...p.m } })), [
    { id: 'c7', m: { type: 'demo-error', method: 'GET', path: '/api/boom' } },
    { id: 'c8', m: { type: 'demo-error', method: 'GET', path: '/api/later' } },
    { id: 'c9', m: { type: 'demo-error', method: 'GET', path: '/api/agents/%E0%A4%A' } },
  ]);
});

test('asset list takes single- and double-quoted src/href but not data-src; cache writes go through waitUntil', async () => {
  const { self, stores } = swEnv([]);
  const html = `<script src='/static/js/a.js'></script><link href="/static/css/b.css"><img data-src="/static/img/c.png">`
    + `<div data-href='/alarm/x.js'></div><script src="https://cdn.example/x.js"></script>`;
  assert.deepEqual([...self.DemoSw.assetUrls(html, 'http://demo.example/dashboard')],
    ['http://demo.example/static/js/a.js', 'http://demo.example/static/css/b.css']);
  const waits = [];
  let answer;
  const event = { request: new Request('http://demo.example/static/js/new.js'),
    waitUntil: (p) => waits.push(p), respondWith: (p) => { answer = p; } };
  self.listeners.fetch.forEach((fn) => fn(event));
  assert.equal(await (await answer).text(), 'asset http://demo.example/static/js/new.js');
  assert.equal(waits.length, 1);
  await Promise.all(waits);
  assert.ok(stores.get('lsm-demo-v9.9.9').has('http://demo.example/static/js/new.js'));
});

test('with every handler file loaded in build order, each literal route is matched by its own entry', () => {
  const files = readdirSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'runtime', 'handlers'))
    .filter((f) => f.endsWith('.js')).sort();
  const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'demo-state.js', ...files.map((f) => `handlers/${f}`)]);
  const R = self.DemoRouter;
  const literal = [...R.keys()].filter((k) => !k.includes('/:'));
  assert.ok(literal.includes('GET /api/lmstudio/server/log') && literal.includes('GET /api/vllm/server/svcconfig'));
  for (const k of literal) {
    const [method, path] = k.split(' ');
    assert.equal(R.match(method, new URL(BASE + path)).key, k);
  }
  R.route('GET /api/vllm/server/late-literal', () => ({}));
  R.route('GET /api/lmstudio/server/:other', () => ({}));
  assert.equal(R.match('GET', new URL(`${BASE}/api/vllm/server/late-literal`)).key, 'GET /api/vllm/server/late-literal');
  assert.equal(R.match('GET', new URL(`${BASE}/api/vllm/server/status`)).key, 'GET /api/vllm/server/:action');
  assert.equal(R.match('GET', new URL(`${BASE}/api/lmstudio/server/status`)).key, 'GET /api/lmstudio/server/:other');
});
