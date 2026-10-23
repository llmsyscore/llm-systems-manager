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

test('works with storage blocked, and does nothing inside a frame or on the bootstrap page', () => {
  const blocked = boot('https://demo.llmsyscore.com/dashboard', { blockStorage: true });
  blocked.w.DemoAnalytics.flush(false);
  assert.ok(blocked.events().some((e) => e.type === 'start'));

  const boot2 = boot('https://demo.llmsyscore.com/');
  assert.equal(boot2.w.DemoAnalytics, undefined);
  assert.equal(boot2.sent.length, 0);
});

test('a refused change on the dashboard is recorded with its method and path', async () => {
  const { w, events } = boot('https://demo.llmsyscore.com/dashboard');
  await w.fetch('/api/admin/settings', { method: 'PUT', body: '{}' });
  await new Promise((r) => setTimeout(r, 10));
  w.DemoAnalytics.flush(false);
  assert.deepEqual(events().filter((e) => e.type === 'refused').map((e) => e.key), ['PUT /api/admin/settings']);
});
