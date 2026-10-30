import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const RUNTIME = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'runtime');
const SRC = readFileSync(join(RUNTIME, 'demo-analytics.js'), 'utf8');
const ENDPOINT = 'https://edge.llmsysmanager.com/v1/demo';

// Runs demo-analytics.js in a jsdom window at `url`, recording every batch it sends.
function boot(url, { blockStorage = false, before } = {}) {
  const dom = new JSDOM('<!doctype html><body><button id="subTabBtnDashLlamacpp">x</button><a id="inst" href="https://get.llmsysmanager.com/?ref=demo">i</a></body>',
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
  const { w, sent, events } = boot('https://demo.llmsysmanager.com/dashboard?ref=hn&utm_source=hn&utm_campaign=launch');
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
  boot('https://demo.llmsysmanager.com/?ref=reddit', { before: shared });
  const { w, events } = boot('https://demo.llmsysmanager.com/dashboard', { before: shared });
  w.DemoAnalytics.flush(false);
  const s = events().find((e) => e.type === 'start');
  assert.equal(s.landing, '/?ref=reddit');
  assert.equal(s.ref, 'reddit');
});

test('tab switches record the previous tab and time spent on it; sub-tabs and install clicks are recorded', () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/dashboard');
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
  const { w, events } = boot('https://demo.llmsysmanager.com/dashboard');
  await w.fetch('/api/tower/threads/demo-t1/messages', { method: 'POST', body: JSON.stringify({ text: 'Any alarm right now?' }) });
  await new Promise((r) => setTimeout(r, 10));
  w.DemoAnalytics.flush(false);
  const t = events().find((e) => e.type === 'tower');
  assert.deepEqual([t.text, t.answer], ['Any alarm right now?', 'alarm']);
});

test('leaving the page sends an end event with sendBeacon', async () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/dashboard');
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  const end = events().find((e) => e.type === 'end');
  assert.equal(end.reason, 'pagehide');
  assert.equal(end.tab, 'overall');
  assert.ok(end.total_ms >= 0);
});

test('hidden then pagehide sends one end event; a page shown again can end again', async () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/dashboard');
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
  const blocked = boot('https://demo.llmsysmanager.com/dashboard', { blockStorage: true });
  blocked.w.DemoAnalytics.flush(false);
  assert.ok(blocked.events().some((e) => e.type === 'start'));

  const boot2 = boot('https://demo.llmsysmanager.com/');
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
  boot('https://demo.llmsysmanager.com/', { before: withId });
  const { sent } = boot('https://demo.llmsysmanager.com/dashboard', { before: shared });
  assert.equal(sent.length, 1);
  const s = sent[0].events.find((e) => e.type === 'start');
  assert.equal(s.lid, '0123456789abcdef');
});

test('the loading page reports a visit that did not open: left, slow or unsupported', async () => {
  const left = boot('https://demo.llmsysmanager.com/?ref=hn');
  left.w.dispatchEvent(new left.w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(left.sent.length, 1);
  const e = left.events()[0];
  assert.deepEqual([e.type, e.reason, e.ref, e.landing], ['landing', 'left', 'hn', '/?ref=hn']);
  assert.ok(e.total_ms >= 0);
  left.w.dispatchEvent(new left.w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(left.sent.length, 1);

  const slow = boot('https://demo.llmsysmanager.com/');
  slow.w.DemoLanding('slow');
  slow.w.dispatchEvent(new slow.w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(slow.events()[0].reason, 'slow');

  const old = boot('https://demo.llmsysmanager.com/');
  old.w.DemoLanding('unsupported');
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(old.events()[0].reason, 'unsupported');
});

test('a later reason on the loading page replaces a provisional left; a failed start is its own reason', async () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/');
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  Object.defineProperty(w.document, 'visibilityState', { value: 'hidden', configurable: true });
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  w.DemoLanding('slow');
  w.dispatchEvent(new w.Event('pagehide'));
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events().map((e) => e.reason), ['left', 'slow']);

  const bad = boot('https://demo.llmsysmanager.com/');
  bad.w.DemoLanding('failed');
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(bad.events()[0].reason, 'failed');
});

test('the loading page sends nothing when it hops to the dashboard', async () => {
  const { w, sent } = boot('https://demo.llmsysmanager.com/');
  w.DemoLanding('open');
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(sent.length, 0);
});

test('a late open after "Still loading" is reported once with the wait time', async () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/');
  w.DemoLanding('slow');
  w.DemoLanding('open');
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events().map((e) => e.reason), ['slow_open']);
  assert.ok(events()[0].total_ms >= 0);
});

test('retry is reported and later outcomes still report', async () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/');
  w.DemoLanding('slow');
  w.DemoLanding('retry');
  w.DemoLanding('open');
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events().map((e) => e.reason), ['retry', 'slow_open']);
});

test('a loading-page link click is reported and kept over later reasons', async () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/');
  w.DemoLanding('slow');
  w.DemoLanding('screenshots');
  w.DemoLanding('install');
  w.DemoLanding('open');
  w.dispatchEvent(new w.Event('pagehide'));
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(events().map((e) => e.reason), ['screenshots']);
});

test('a refused change on the dashboard is recorded with its method and path', async () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/dashboard');
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
  const dash = boot('https://demo.llmsysmanager.com/dashboard', { before: shared });
  dash.w.DemoAnalytics.flush(false);
  const phone = boot('https://demo.llmsysmanager.com/phone', { before: shared });
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

test('controls with data-demo-track record their own target', () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/dashboard');
  const d = w.document;
  d.body.insertAdjacentHTML('beforeend', '<a id="s" data-demo-track="star-banner" href="https://github.com/llmsyscore/llm-systems-manager">s</a><a id="d" data-demo-track="directions-tour" href="https://get.llmsysmanager.com/?ref=demo">d</a>');
  for (const id of ['s', 'd']) { d.getElementById(id).addEventListener('click', (e) => e.preventDefault()); d.getElementById(id).click(); }
  w.DemoAnalytics.flush(false);
  assert.deepEqual(events().filter((e) => e.type === 'click').map((e) => ['click', e.target]), [['click', 'star-banner'], ['click', 'directions-tour']]);
});

test('a started benchmark is recorded once', async () => {
  let status = 200;
  const { w, events } = boot('https://demo.llmsysmanager.com/dashboard', { before: (w) => {
    const base = w.fetch;
    w.fetch = (u, init) => {
      if (typeof u === 'string' && u.split('?')[0] === '/api/benchmark/live/run') {
        const body = status === 200 ? { ok: true, run_id: 'demo-bench-1' } : { ok: false, error: 'Not available in the demo' };
        return Promise.resolve({ status, clone: () => ({ json: () => Promise.resolve(body) }) });
      }
      return base(u, init);
    };
  } });
  await w.fetch('/api/benchmark/live/run?provider=llama&agent=', { method: 'POST', body: '{}' });
  await new Promise((r) => setTimeout(r, 10));
  status = 403;
  await w.fetch('/api/benchmark/live/run', { method: 'POST', body: '{}' });
  await new Promise((r) => setTimeout(r, 10));
  w.DemoAnalytics.flush(false);
  const got = events().filter((e) => e.type === 'click' || e.type === 'refused').map((e) => [e.type, e.target || e.key]);
  assert.deepEqual(got, [['click', 'bench-start'], ['refused', 'POST /api/benchmark/live/run']]);
});

test('tour questions are not counted as visitor questions', async () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/dashboard');
  w.DemoTourAsking = true;
  await w.fetch('/api/tower/threads/demo-t1/messages', { method: 'POST', body: JSON.stringify({ text: 'What does each machine cost to run?' }) });
  await new Promise((r) => setTimeout(r, 10));
  w.DemoAnalytics.flush(false);
  assert.equal(events().filter((e) => e.type === 'tower').length, 0);
});

test('opening the Tower drawer is recorded', async () => {
  const { w, events } = boot('https://demo.llmsysmanager.com/dashboard', {
    before: (win) => { const o = win.document.createElement('div'); o.id = 'towerOverlay'; win.document.body.appendChild(o); } });
  const o = w.document.getElementById('towerOverlay');
  const tick = () => new Promise((r) => setTimeout(r, 10));
  await tick();
  o.classList.add('open'); await tick();
  o.classList.add('x'); await tick();
  o.classList.remove('open'); await tick();
  o.classList.add('open'); await tick();
  w.DemoAnalytics.flush(false);
  assert.equal(events().filter((e) => e.type === 'click' && e.target === 'tower-open').length, 2);
});

const VAL = 'a'.repeat(64);

test('payload carries pv only when a valid lsmPref is stored', () => {
  const none = boot('https://demo.llmsysmanager.com/dashboard');
  none.w.DemoAnalytics.flush(false);
  assert.equal('pv' in none.sent[0], false);
  const bad = boot('https://demo.llmsysmanager.com/dashboard', { before: (w) => w.localStorage.setItem('lsmPref', 'nope') });
  bad.w.DemoAnalytics.flush(false);
  assert.equal('pv' in bad.sent[0], false);
  const ok = boot('https://demo.llmsysmanager.com/dashboard', { before: (w) => w.localStorage.setItem('lsmPref', VAL) });
  ok.w.DemoAnalytics.flush(false);
  assert.equal(ok.sent[0].pv, VAL);
});

test('throwing localStorage leaves the payload without pv', () => {
  const p = boot('https://demo.llmsysmanager.com/dashboard', { before: (w) => Object.defineProperty(w, 'localStorage', { get() { throw new Error('blocked'); } }) });
  p.w.DemoAnalytics.flush(false);
  assert.equal(p.sent.length, 1);
  assert.equal('pv' in p.sent[0], false);
});
