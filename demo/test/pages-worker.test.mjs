import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { isVisit, report } from '../pages/_worker.js';

const html = (status = 200) => new Response('<html><head><title>x</title></head><body></body></html>',
  { status, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
const js = () => new Response('1', { headers: { 'Content-Type': 'application/javascript' } });

// Stand-in for the Workers HTMLRewriter: prepends to <head> by text replace.
class FakeRewriter {
  on(sel, h) { this.h = h; return this; }
  transform(res) {
    const h = this.h;
    const stream = new ReadableStream({ async start(c) {
      let add = '';
      h.element({ prepend: (s) => { add = s; } });
      c.enqueue(new TextEncoder().encode((await res.text()).replace('<head>', '<head>' + add)));
      c.close();
    } });
    return new Response(stream, { status: res.status, headers: res.headers });
  }
}

// Calls the worker with a stubbed ASSETS binding and global fetch, recording receiver posts.
async function run(req, asset, env = { LANDING_SECRET: 's3cret' }, { fetchFails = false } = {}) {
  const posts = [], waits = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (u, init) => {
    if (fetchFails) throw new Error('down');
    posts.push({ url: u, init, body: JSON.parse(init.body) });
    return new Response(null, { status: 204 });
  };
  globalThis.HTMLRewriter = FakeRewriter;
  try {
    const res = await worker.fetch(req, { ...env, ASSETS: { fetch: async () => asset() } }, { waitUntil: (p) => waits.push(p) });
    await Promise.all(waits);
    const body = await res.text();
    return { res, body, posts };
  } finally {
    globalThis.fetch = realFetch;
    delete globalThis.HTMLRewriter;
  }
}

const nav = (url, headers = {}) => new Request(url, { headers: { 'Sec-Fetch-Mode': 'navigate', 'User-Agent': 'Mozilla/5.0 Firefox/140', ...headers } });

test('a page navigation is reported and the page gets the same landing id', async () => {
  const { body, posts } = await run(nav('https://demo.llmsyscore.com/?ref=hn&utm_source=hn', { Referer: 'https://news.ycombinator.com/' }), html);
  assert.equal(posts.length, 1);
  const p = posts[0];
  assert.equal(p.url, 'https://telemetry-receiver.llmsyscore.workers.dev/v1/demo/landing');
  assert.equal(p.init.headers.Authorization, 'Bearer s3cret');
  assert.match(p.body.id, /^[0-9a-f]{16}$/);
  assert.equal(p.body.path, '/');
  assert.equal(p.body.ref, 'hn');
  assert.equal(p.body.utm_source, 'hn');
  assert.equal(p.body.referer, 'https://news.ycombinator.com/');
  assert.equal(p.body.status, 200);
  assert.ok(body.includes(`<head><meta name="lsm-landing" content="${p.body.id}">`));
});

test('a client without fetch-mode headers (crawlers, curl) is reported too', async () => {
  const req = new Request('https://demo.llmsyscore.com/robots.txt', { headers: { 'User-Agent': 'Googlebot/2.1' } });
  const { posts } = await run(req, html);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].body.path, '/robots.txt');
});

test('the service worker precache, scripts and HEAD-only pages are handled without injection', async () => {
  const pre = new Request('https://demo.llmsyscore.com/dashboard', { headers: { 'Sec-Fetch-Mode': 'same-origin' } });
  const a = await run(pre, html);
  assert.equal(a.posts.length, 0);
  assert.ok(!a.body.includes('lsm-landing'));
  const b = await run(nav('https://demo.llmsyscore.com/x.js'), js);
  assert.equal(b.posts.length, 0);
  const head = await run(new Request('https://demo.llmsyscore.com/', { method: 'HEAD', headers: { 'User-Agent': 'Slackbot' } }), html);
  assert.equal(head.posts.length, 1);
  assert.equal(head.posts[0].body.method, 'HEAD');
});

test('a page load from the demo itself (its own redirect to /) is not reported', async () => {
  const { body, posts } = await run(nav('https://demo.llmsyscore.com/', { Referer: 'https://demo.llmsyscore.com/dashboard' }), html);
  assert.equal(posts.length, 0);
  assert.ok(!body.includes('lsm-landing'));
});

test('without the secret nothing is sent and the page is unchanged', async () => {
  const { body, posts } = await run(nav('https://demo.llmsyscore.com/'), html, {});
  assert.equal(posts.length, 0);
  assert.ok(!body.includes('lsm-landing'));
});

test('a failing receiver still serves the page', async () => {
  const { res, body } = await run(nav('https://demo.llmsyscore.com/'), html, undefined, { fetchFails: true });
  assert.equal(res.status, 200);
  assert.ok(body.includes('<title>x</title>'));
});

test('isVisit and report read only what the receiver classifies by', () => {
  const req = nav('https://demo.llmsyscore.com/dashboard?utm_campaign=launch', { 'Accept-Language': 'en-US' });
  Object.defineProperty(req, 'cf', { value: { country: 'US', asn: 7922, asOrganization: 'Example', verifiedBotCategory: 'Search Engine Crawler' } });
  assert.equal(isVisit(req, html()), true);
  assert.equal(isVisit(req, js()), false);
  const r = report('0123456789abcdef', req, html());
  assert.deepEqual([r.site, r.path, r.utm_campaign, r.country, r.asn, r.as_org, r.bot_category, r.lang],
    ['demo.llmsyscore.com', '/dashboard', 'launch', 'US', 7922, 'Example', 'Search Engine Crawler', 'en-US']);
  assert.ok(!('ip' in r));
});

test('pages.dev responses are noindex and the demo domain is not', async () => {
  const dev = await run(new Request('https://llmsyscore-demo.pages.dev/'), html);
  assert.equal(dev.res.headers.get('X-Robots-Tag'), 'noindex');
  assert.ok(dev.body.includes('lsm-landing'));
  const own = await run(new Request('https://demo.llmsyscore.com/'), html);
  assert.equal(own.res.headers.get('X-Robots-Tag'), null);
});

test('dashboard links with no demo page redirect to the landing page', async () => {
  for (const path of ['/logout', '/login', '/llm/log']) {
    const { res, posts } = await run(new Request('https://demo.llmsyscore.com' + path), html);
    assert.equal(res.status, 302, path);
    assert.equal(res.headers.get('Location'), 'https://demo.llmsyscore.com/', path);
    assert.equal(posts.length, 0, path);
  }
});

test('a direct visit to the phone app is reported like the dashboard; the switch from the dashboard is not', async () => {
  for (const path of ['/phone', '/dashboard']) {
    const { body, posts } = await run(nav('https://demo.llmsyscore.com' + path + '?ref=hn'), html);
    assert.equal(posts.length, 1, path);
    assert.equal(posts[0].body.path, path);
    assert.equal(posts[0].body.ref, 'hn');
    assert.ok(body.includes(`<meta name="lsm-landing" content="${posts[0].body.id}">`), path);
  }
  const own = await run(nav('https://demo.llmsyscore.com/phone', { Referer: 'https://demo.llmsyscore.com/dashboard' }), html);
  assert.equal(own.posts.length, 0);
});

test('/internal with the key sets the device cookie, is not reported, and tagged visits carry own', async () => {
  const env = { LANDING_SECRET: 's3cret', INTERNAL_TAG_KEY: 'k1' };
  const tag = await run(nav('https://demo.llmsyscore.com/internal?key=k1'), html, env);
  assert.equal(tag.posts.length, 0);
  const c = tag.res.headers.get('Set-Cookie');
  assert.match(c, /^lsm_device=[0-9a-f]{64}; Max-Age=\d+; Path=\/; Secure; HttpOnly; SameSite=Lax$/);
  assert.match(tag.body, /marked as yours/);
  assert.equal(tag.res.headers.get('Referrer-Policy'), 'no-referrer');
  const value = c.split(';')[0];
  const own = await run(nav('https://demo.llmsyscore.com/', { Cookie: 'x=1; ' + value }), html, env);
  assert.equal(own.posts[0].body.own, 1);
  const other = await run(nav('https://demo.llmsyscore.com/', { Cookie: 'lsm_device=nope' }), html, env);
  assert.equal(other.posts[0].body.own, 0);
  const off = await run(nav('https://demo.llmsyscore.com/internal?key=k1&remove=1'), html, env);
  assert.match(off.res.headers.get('Set-Cookie'), /^lsm_device=; Max-Age=0;/);
});

test('/internal with a wrong or unset key is an ordinary reported request', async () => {
  const wrong = await run(nav('https://demo.llmsyscore.com/internal?key=bad'), html, { LANDING_SECRET: 's3cret', INTERNAL_TAG_KEY: 'k1' });
  assert.equal(wrong.res.headers.get('Set-Cookie'), null);
  assert.equal(wrong.posts.length, 1);
  assert.equal(wrong.posts[0].body.path, '/internal');
  const unset = await run(nav('https://demo.llmsyscore.com/internal?key='), html);
  assert.equal(unset.res.headers.get('Set-Cookie'), null);
  assert.equal(unset.posts[0].body.own, 0);
});

// A WRITES queue binding that throws on the first `fails` sends and records the rest.
function queue(fails = 0) {
  const sent = [];
  let calls = 0;
  return { sent, get calls() { return calls; }, async send(m) { calls += 1; if (calls <= fails) throw new Error('down'); sent.push(m); } };
}

test('queues the report when WRITES is bound', async () => {
  const q = queue();
  const { body, posts } = await run(nav('https://demo.llmsyscore.com/?ref=hn'), html, { LANDING_SECRET: 's3cret', WRITES: q });
  assert.equal(posts.length, 0);
  assert.equal(q.sent.length, 1);
  const m = q.sent[0];
  assert.equal(m.kind, 'landing_report');
  assert.match(m.at, /^\d{4}-\d{2}-\d{2}T/);
  assert.equal(m.report.ref, 'hn');
  assert.ok(body.includes(`<meta name="lsm-landing" content="${m.report.id}">`));
});

test('retries once then queues', async () => {
  const q = queue(1);
  const { posts } = await run(nav('https://demo.llmsyscore.com/'), html, { LANDING_SECRET: 's3cret', WRITES: q });
  assert.equal(q.calls, 2);
  assert.equal(q.sent.length, 1);
  assert.equal(posts.length, 0);
});

test('posts to the receiver after two failed sends', async () => {
  const q = queue(2);
  const { posts } = await run(nav('https://demo.llmsyscore.com/'), html, { LANDING_SECRET: 's3cret', WRITES: q });
  assert.equal(q.calls, 2);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].init.headers.Authorization, 'Bearer s3cret');
});

test('without the secret nothing is queued or posted even when WRITES is bound', async () => {
  const q = queue();
  const { body, posts } = await run(nav('https://demo.llmsyscore.com/'), html, { WRITES: q });
  assert.equal(q.calls, 0);
  assert.equal(posts.length, 0);
  assert.ok(!body.includes('lsm-landing'));
});

test('the queued report matches the posted report field by field', async () => {
  const req = () => nav('https://demo.llmsyscore.com/dashboard?ref=hn&utm_source=hn&utm_campaign=launch',
    { Referer: 'https://news.ycombinator.com/', 'Accept-Language': 'en-US' });
  const q = queue();
  const queued = await run(req(), html, { LANDING_SECRET: 's3cret', WRITES: q });
  const posted = await run(req(), html);
  const qr = q.sent[0].report, pr = posted.posts[0].body;
  assert.ok(queued.body.includes(`<meta name="lsm-landing" content="${qr.id}">`));
  assert.deepEqual(Object.keys(qr).sort(), Object.keys(pr).sort());
  for (const k of Object.keys(pr)) if (k !== 'id') assert.deepEqual(qr[k], pr[k], k);
});
