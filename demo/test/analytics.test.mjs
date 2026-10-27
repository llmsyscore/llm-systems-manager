import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const RUNTIME = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'runtime');
const SRC = readFileSync(join(RUNTIME, 'demo-analytics.js'), 'utf8');
const ENDPOINT = 'https://telemetry-receiver.llmsyscore.workers.dev/v1/demo';

// Runs demo-analytics.js in a jsdom window at `url`, recording every batch it sends.
function boot(url, { blockStorage = false, before } = {}) {
  const dom = new JSDOM('<!doctype html><body><button id="subTabBtnDashLlamacpp">x</button><a id="inst" href="https://get.llmsyscore.com/?ref=demo">i</a></body>',
    { url, runScripts: 'outside-only', referrer: 'https://news.ycombinator.com/item?id=1' });
  const w = dom.window;
  const sent = [];
  w.fetch = (u, init) => {
    if (u === ENDPOINT) sent.push(JSON.parse(init.body));
    if (u === '/api/admin/settings') {
      return Promise.resolve({ status: 403, clone: () => ({ json: () => Promise.resolve({ ok: false, error: 'Not available in the demo' }) }) });
    }
    if (typeof u === 'string' && /\/messages$/.test(u)) {
      return Promise.resolve({ clone: () => ({ json: () => Promise.resolve({ ok: true, run_id: 'demo-run-alarm-1' }) }) });
    }
    return Promise.resolve({ clone: () => ({ json: () => Promise.resolve({}) }) });
  };
  w.navigator.sendBeacon = (u, blob) => { blob.text().then((t) => sent.push(JSON.parse(t))); return true; };
  if (blockStorage) Object.defineProperty(w, 'sessionStorage', { get() { throw new Error('blocked'); } });
  if (before) before(w);
  w.eval(SRC);
  return { w, sent, events: () => sent.flatMap((b) => b.events) };
}

test('dashboard sends a start event with landing, referrer and campaign tags', () => {
  const { w, sent, events } = boot('https://demo.llmsyscore.com/dashboard?ref=hn&utm_source=hn&utm_campaign=launch');
  w.DemoAnalytics.flush(false);
  assert.equal(sent.length, 1);
  const b = sent[0];
  assert.equal(b.v, 1);
  assert.match(b.session, /^[0-9a-f]{16}$/);
  assert.equal(b.seq, 0);
  const s = events().find((e) => e.type === 'start');
  assert.equal(s.ref, 'hn');
  assert.equal(s.utm_source, 'hn');
  assert.equal(s.utm_campaign, 'launch');
  assert.equal(s.landing, '/dashboard?ref=hn&utm_source=hn&utm_campaign=launch');
  assert.equal(s.referrer, 'https://news.ycombinator.com/item?id=1');
  assert.ok(typeof s.t === 'number' && s.t >= 0);
});

test('landing and referrer saved on the bootstrap page carry over to the dashboard', () => {
  const store = {};
  const shared = (w) => {
    Object.defineProperty(w, 'sessionStorage', { value: {
      getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } } });
  };
  boot('https://demo.llmsyscore.com/?ref=reddit', { before: shared });
  const { w, events } = boot('https://demo.llmsyscore.com/dashboard', { before: shared });
  w.DemoAnalytics.flush(false);
  const s = events().find((e) => e.type === 'start');
  assert.equal(s.landing, '/?ref=reddit');
  assert.equal(s.ref, 'reddit');
});

test('tab switches record the previous tab and time spent on it; sub-tabs and install clicks are recorded', () => {
  const { w, events } = boot('https://demo.llmsyscore.com/dashboard');
  w.document.dispatchEvent(new w.CustomEvent('lsm:tab', { detail: { tab: 'dashboards' } }));
  w.document.getElementById('subTabBtnDashLlamacpp').click();
  w.document.getElementById('inst').addEventListener('click', (e) => e.preventDefault());
  w.document.getElementById('inst').click();
  w.DemoAnalytics.flush(false);
  const tab = events().find((e) => e.type === 'tab');
  assert.equal(tab.tab, 'dashboards');
  assert.equal(tab.prev, 'overall');
  assert.ok(tab.dwell_ms >= 0);
  assert.deepEqual(events().find((e) => e.type === 'subtab'), { ...events().find((e) => e.type === 'subtab'), tab: 'dashboards', sub: 'DashLlamacpp' });
  assert.equal(events().find((e) => e.type === 'click').target, 'install');
});

test('Tower questions record the text and the canned answer key', async () => {
  const { w, events } = boot('https://demo.llmsyscore.com/dashboard');
  await w.fetch('/api/tower/threads/demo-t1/messages', { method: 'POST', body: JSON.stringify({ text: 'Any alarm right now?' }) });
  await new Promise((r) => setTimeout(r, 10));
  w.DemoAnalytics.flush(false);
  const t = events().find((e) => e.type === 'tower');
  assert.deepEqual([t.text, t.answer], ['Any alarm right now?', 'alarm']);
});

test('leaving the page sends an end event with sendBeacon', async () => {
  const { w, events } = boot('https://demo.llmsyscore.com/dashboard');
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  const end = events().find((e) => e.type === 'end');
  assert.equal(end.reason, 'pagehide');
  assert.equal(end.tab, 'overall');
  assert.ok(end.total_ms >= 0);
});

test('hidden then pagehide sends one end event; a page shown again can end again', async () => {
  const { w, events } = boot('https://demo.llmsyscore.com/dashboard');
  const vis = (v) => { Object.defineProperty(w.document, 'visibilityState', { value: v, configurable: true }); w.document.dispatchEvent(new w.Event('visibilitychange')); };
  vis('hidden');
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events().filter((e) => e.type === 'end').map((e) => e.reason), ['hidden']);
  vis('visible');
  vis('hidden');
  w.dispatchEvent(new w.Event('pageshow'));
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events().filter((e) => e.type === 'end').map((e) => e.reason), ['hidden', 'hidden', 'pagehide']);
});

test('works with storage blocked, and sends nothing from the bootstrap page while it loads', () => {
  const blocked = boot('https://demo.llmsyscore.com/dashboard', { blockStorage: true });
  blocked.w.DemoAnalytics.flush(false);
  assert.ok(blocked.events().some((e) => e.type === 'start'));

  const boot2 = boot('https://demo.llmsyscore.com/');
  boot2.w.DemoLanding('slow');
  assert.equal(boot2.sent.length, 0);
});

test('the dashboard sends the start event right away, with the landing id from the loading page', () => {
  const store = {};
  const shared = (w) => {
    Object.defineProperty(w, 'sessionStorage', { value: {
      getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } } });
  };
  const withId = (w) => {
    shared(w);
    const m = w.document.createElement('meta');
    m.setAttribute('name', 'lsm-landing');
    m.setAttribute('content', '0123456789abcdef');
    w.document.head.appendChild(m);
  };
  boot('https://demo.llmsyscore.com/', { before: withId });
  const { sent } = boot('https://demo.llmsyscore.com/dashboard', { before: shared });
  assert.equal(sent.length, 1);
  const s = sent[0].events.find((e) => e.type === 'start');
  assert.equal(s.lid, '0123456789abcdef');
});

test('the loading page reports a visit that did not open: left, slow or unsupported', async () => {
  const left = boot('https://demo.llmsyscore.com/?ref=hn');
  left.w.dispatchEvent(new left.w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(left.sent.length, 1);
  const e = left.events()[0];
  assert.deepEqual([e.type, e.reason, e.ref, e.landing], ['landing', 'left', 'hn', '/?ref=hn']);
  assert.ok(e.total_ms >= 0);
  left.w.dispatchEvent(new left.w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(left.sent.length, 1);

  const slow = boot('https://demo.llmsyscore.com/');
  slow.w.DemoLanding('slow');
  slow.w.dispatchEvent(new slow.w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(slow.events()[0].reason, 'slow');

  const old = boot('https://demo.llmsyscore.com/');
  old.w.DemoLanding('unsupported');
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(old.events()[0].reason, 'unsupported');
});

test('a later reason on the loading page replaces a provisional left; a failed start is its own reason', async () => {
  const { w, events } = boot('https://demo.llmsyscore.com/');
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  Object.defineProperty(w.document, 'visibilityState', { value: 'hidden', configurable: true });
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  w.DemoLanding('slow');
  w.dispatchEvent(new w.Event('pagehide'));
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events().map((e) => e.reason), ['left', 'slow']);

  const bad = boot('https://demo.llmsyscore.com/');
  bad.w.DemoLanding('failed');
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(bad.events()[0].reason, 'failed');
});

test('the loading page sends nothing when it hops to the dashboard', async () => {
  const { w, sent } = boot('https://demo.llmsyscore.com/');
  w.DemoLanding('open');
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(sent.length, 0);
});

test('a late open after "Still loading" is reported once with the wait time', async () => {
  const { w, events } = boot('https://demo.llmsyscore.com/');
  w.DemoLanding('slow');
  w.DemoLanding('open');
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events().map((e) => e.reason), ['slow_open']);
  assert.ok(events()[0].total_ms >= 0);
});

test('a loading-page link click is reported and kept over later reasons', async () => {
  const { w, events } = boot('https://demo.llmsyscore.com/');
  w.DemoLanding('slow');
  w.DemoLanding('screenshots');
  w.DemoLanding('install');
  w.DemoLanding('open');
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events().map((e) => e.reason), ['screenshots']);
});

test('a refused change on the dashboard is recorded with its method and path', async () => {
  const { w, events } = boot('https://demo.llmsyscore.com/dashboard');
  await w.fetch('/api/admin/settings', { method: 'PUT', body: '{}' });
  await new Promise((r) => setTimeout(r, 10));
  w.DemoAnalytics.flush(false);
  assert.deepEqual(events().filter((e) => e.type === 'refused').map((e) => e.key), ['PUT /api/admin/settings']);
});

test('the phone app records the session, and switching between it and the dashboard keeps the same session', () => {
  const store = {};
  const shared = (w) => {
    Object.defineProperty(w, 'sessionStorage', { value: {
      getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } } });
  };
  const dash = boot('https://demo.llmsyscore.com/dashboard', { before: shared });
  dash.w.DemoAnalytics.flush(false);
  const phone = boot('https://demo.llmsyscore.com/phone', { before: shared });
  assert.equal(typeof phone.w.DemoAnalytics.onTab, 'function', 'the phone app records, not the loading page');
  phone.w.document.dispatchEvent(new phone.w.CustomEvent('lsm:tab', { detail: { tab: 'phone' } }));
  phone.w.document.dispatchEvent(new phone.w.CustomEvent('lsm:tab', { detail: { tab: 'phone-alerts' } }));
  phone.w.DemoAnalytics.flush(false);
  assert.equal(phone.sent[0].session, dash.sent[0].session);
  assert.equal(phone.sent[0].seq, dash.sent[0].seq + 1);
  const ev = phone.events();
  assert.ok(!ev.some((e) => e.type === 'start'), 'no second start');
  assert.deepEqual(ev.filter((e) => e.type === 'tab').map((e) => [e.tab, e.prev]), [['phone', 'overall'], ['phone-alerts', 'phone']]);
});
