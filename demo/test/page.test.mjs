import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import { load } from './load.mjs';

const RUNTIME = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'runtime');
const src = (f) => readFileSync(join(RUNTIME, f), 'utf8');

// Loads demo-page.js in a vm with a stub service worker container that records its listeners.
function vmPage({ path = '/dashboard', controller = {}, extra = {} } = {}) {
  const swListeners = {}, winListeners = {}, toasts = [], warns = [], errors = [], replaced = [], posted = [];
  const reloads = [];
  const serviceWorker = { controller, addEventListener: (t, fn) => { (swListeners[t] ||= []).push(fn); } };
  const location = Object.assign(new URL('https://demo.example' + path),
    { replace: (u) => replaced.push(u), reload: () => reloads.push(1) });
  const self = load(['demo-page.js'], {
    navigator: { serviceWorker }, location,
    console: { ...console, warn: (...a) => warns.push(a), error: (...a) => errors.push(a) },
    _toastErr: (m) => toasts.push(m),
    addEventListener: (t, fn) => { (winListeners[t] ||= []).push(fn); },
    ...extra,
  });
  const fromWorker = (data) => (swListeners.message || []).forEach((fn) => fn({ data }));
  const fromWindow = (ev) => (winListeners.message || []).forEach((fn) => fn(ev));
  return { self, toasts, warns, errors, replaced, reloads, posted, fromWorker, fromWindow, swListeners };
}

test('refusal message calls _toastErr with the exact text, at most once every 2 s', (t) => {
  t.mock.timers.enable({ apis: ['Date'], now: 1_000_000 });
  const p = vmPage({ extra: { Date } });
  p.fromWorker({ type: 'demo-refused', key: 'POST /api/terminal/create' });
  p.fromWorker({ type: 'demo-refused', key: 'POST /api/terminal/create' });
  assert.deepEqual(p.toasts, ['Not available in the demo']);
  t.mock.timers.tick(1999);
  p.fromWorker({ type: 'demo-refused', key: 'x' });
  assert.equal(p.toasts.length, 1);
  t.mock.timers.tick(1);
  p.fromWorker({ type: 'demo-refused', key: 'x' });
  assert.deepEqual(p.toasts, ['Not available in the demo', 'Not available in the demo']);
});

test('_toastErr is looked up when the message arrives', () => {
  const p = vmPage({ extra: { _toastErr: undefined } });
  p.fromWorker({ type: 'demo-refused', key: 'x' });
  const got = [];
  p.self._toastErr = (m) => got.push(m);
  p.fromWorker({ type: 'demo-refused', key: 'x' });
  assert.deepEqual(got, ['Not available in the demo']);
});

test('a refusal that arrives before _toastErr exists is shown once it is defined', async () => {
  const p = vmPage({ extra: { _toastErr: undefined } });
  p.fromWorker({ type: 'demo-refused', key: 'x' });
  p.fromWorker({ type: 'demo-refused', key: 'x' });
  const got = [];
  await new Promise((r) => setTimeout(r, 300));
  p.self._toastErr = (m) => got.push(m);
  await new Promise((r) => setTimeout(r, 600));
  assert.deepEqual(got, ['Not available in the demo']);
});

test('handler error message logs a demo error', () => {
  const p = vmPage();
  p.fromWorker({ type: 'demo-error', method: 'GET', path: '/api/boom' });
  assert.deepEqual(p.errors, [['demo error', 'GET', '/api/boom']]);
});

test('a new worker taking over an open dashboard reloads it once; the alarm frame does not reload', () => {
  const p = vmPage();
  p.swListeners.controllerchange.forEach((fn) => fn({}));
  p.swListeners.controllerchange.forEach((fn) => fn({}));
  assert.equal(p.reloads.length, 1);
  const frame = vmPage({ path: '/alarm/' });
  assert.equal((frame.swListeners.controllerchange || []).length, 0);
});

test('miss message warns with method and path', () => {
  const p = vmPage();
  p.fromWorker({ type: 'demo-miss', method: 'GET', path: '/api/nope' });
  assert.deepEqual(p.warns, [['demo miss', 'GET', '/api/nope']]);
  assert.equal(p.toasts.length, 0);
});

test('uncontrolled dashboard goes back to the bootstrap page; controlled one and the alarm frame stay', () => {
  assert.deepEqual(vmPage({ controller: null }).replaced, ['/']);
  const stored = {};
  const storage = { setItem: (k, v) => { stored[k] = v; } };
  assert.deepEqual(vmPage({ controller: null, extra: { sessionStorage: storage } }).replaced, ['/']);
  assert.deepEqual(stored, {});
  assert.deepEqual(vmPage({ path: '/phone', controller: null, extra: { sessionStorage: storage } }).replaced, ['/']);
  assert.deepEqual(stored, { lsmDemoReturn: '/phone' });
  assert.deepEqual(vmPage({ path: '/phone', controller: null }).replaced, ['/'], 'storage blocked still redirects');
  assert.deepEqual(vmPage().replaced, []);
  assert.deepEqual(vmPage({ path: '/alarm/', controller: null }).replaced, []);
  const noSw = load(['demo-page.js'], { location: Object.assign(new URL('https://demo.example/dashboard'),
    { replace: () => { throw new Error('should not redirect'); } }) });
  assert.ok(noSw.DemoPage);
});

test('alarm frame posts refusals to the dashboard, which toasts them', () => {
  const posted = [];
  const parent = { postMessage: (d, o) => posted.push([d, o]) };
  const frame = vmPage({ path: '/alarm/', extra: { _toastErr: undefined, parent } });
  frame.fromWorker({ type: 'demo-refused', key: 'POST /alarm/api/rules' });
  assert.deepEqual(JSON.parse(JSON.stringify(posted)),
    [[{ type: 'demo-refused', key: 'POST /alarm/api/rules' }, 'https://demo.example']]);
  const dash = vmPage();
  dash.fromWindow({ data: posted[0][0], origin: 'https://other.example' });
  assert.equal(dash.toasts.length, 0);
  dash.fromWindow({ data: posted[0][0], origin: 'https://demo.example' });
  assert.deepEqual(dash.toasts, ['Not available in the demo']);
});

const DASH_HTML = `<!DOCTYPE html><html><head></head><body>
<h1><a href="/">Home</a><div class="header-right"><button id="towerBtn">Tower</button></div></h1>
<div class="tab-nav">
  <button class="tab-btn" id="tabBtnOverall">Overall</button><button class="tab-btn" id="tabBtnDashboard">Dashboards</button>
  <button class="tab-btn" id="tabBtnLlmControl">LLM Control</button>
  <button class="tab-btn" id="tabBtnEvents">Events</button><button class="tab-btn" id="tabBtnAdmin">Admin</button>
</div>
<div id="overallTab"><section class="ov-hero"></section><div id="ovAgentsStrip"></div></div>
<div id="dashboardTab"><button class="sub-tab-btn" id="subTabBtnDashEnergy">Energy</button>
  <button class="sub-tab-btn" id="subTabBtnDashForecast">Forecast</button>
  <div id="dash-energy"></div><div id="dash-forecast"></div></div>
<div id="llmTab"><button class="sub-tab-btn" id="subTabBtnLlmLlamacpp">llama.cpp</button>
  <button class="sub-tab-btn" id="subTabBtnLlmTools">Tools</button>
  <div id="llm-ctrl-wrap"><div class="llm-section" id="secModels"></div></div><div id="llm-llamacpp"></div><div id="llm-tools"></div>
  <div id="terminalPanel" style="display:none;"><div id="terminalMount"></div></div>
  <div id="serverLogPanel" style="display:none;"><div id="serverLogBox"></div></div>
</div>
<div id="eventsTab"><iframe id="alarmEngineIframe"></iframe></div>
<div id="towerOverlay"><aside id="towerAside"><div id="twBox"><textarea id="twInput"></textarea></div></aside></div>
<div id="adminTab"><div id="adminHealthCard"></div>
  <button class="sub-tab-btn" id="subTabBtnAdminAgents">Agents</button><button class="sub-tab-btn" id="subTabBtnAdminRouting">Gateway</button>
  <button class="sub-tab-btn" id="subTabBtnAdminJobs">Jobs</button>
  <div id="admin-agents"></div><div id="admin-routing"></div><div id="admin-jobs"></div></div>
</body></html>`;

// Builds a jsdom dashboard, runs the given runtime scripts in it and waits for the tour's start delay.
async function dom(scripts, { before, after, url = 'https://demo.example/dashboard', html = DASH_HTML } = {}) {
  const d = new JSDOM(html, { url, runScripts: 'outside-only' });
  const w = d.window;
  if (before) before(w);
  for (const f of scripts) w.eval(src(f));
  if (after) after(w);
  await new Promise((r) => setTimeout(r, 1300));
  return w;
}

test('tour does not start when the seen flag is set', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  assert.equal(w.document.querySelector('.demo-tour'), null);
  assert.ok(w.document.getElementById('demoBanner'), 'banner still shows');
});

test('a first visit shows the welcome card; its tour button starts the tour, which walks the steps and Close sets the flag', async () => {
  const tabs = [], tracked = [];
  const w = await dom(['demo-tour.js'], { before: (w) => { w.switchTab = (t) => tabs.push(t); w.DemoTrack = (t, f) => tracked.push([t, f.target]); } });
  const steps = w.DemoTour.steps;
  assert.equal(w.document.querySelector('.demo-tour'), null, 'the tour waits for the welcome choice');
  const welcome = w.document.querySelector('.demo-welcome-card');
  assert.equal(welcome.getAttribute('role'), 'dialog');
  assert.equal(welcome.querySelector('.demo-welcome-title').textContent, 'Welcome to the LLM Systems Manager demo');
  assert.match(welcome.textContent, /real dashboard running on sample data\. Everything works/);
  assert.doesNotMatch(welcome.textContent, /leaves your browser/);
  assert.equal(w.document.activeElement, welcome.querySelector('.demo-welcome-tour'));
  welcome.querySelector('.demo-welcome-tour').click();
  assert.equal(w.document.querySelector('.demo-welcome'), null);
  assert.deepEqual(tracked, [['click', 'welcome-tour'], ['tour', undefined]]);
  assert.equal(steps.length, 12);
  assert.ok(steps.every((s) => s.text.split(/(?<=\.|\?"?)\s+(?=[A-Z])/).length <= 2), 'one or two sentences per step');
  for (const t of ['Energy', 'Forecast', 'Benchmarks', 'System health', 'Where requests go', 'Jobs', 'Add machines']) {
    assert.ok(steps.some((s) => s.title.includes(t)), t);
  }
  assert.ok(steps.every((s) => !/\bagents?\b|turned off/i.test(s.text)), 'plain words: no "agent", no reassurance');
  assert.match(steps.find((s) => s.sub === 'subTabBtnLlmTools').text, /Autotune tries .* keeps the fastest/);
  for (const s of steps) {
    assert.ok(w.document.querySelector(s.selector), s.selector);
    assert.ok(s.title && s.text, s.selector);
  }
  assert.ok(steps.some((s) => s.selector === '#towerAside' && s.tower && /slow|alarm|cost|model/i.test(s.text)));
  const card = w.document.querySelector('.demo-tour-card');
  assert.ok(card);
  assert.equal(card.querySelector('.demo-tour-title').textContent, steps[0].title);
  assert.equal(card.querySelector('.demo-tour-back').disabled, true);
  card.querySelector('.demo-tour-next').click();
  assert.equal(w.DemoTour.index, 1);
  w.document.querySelector('.demo-tour-back').click();
  assert.equal(w.DemoTour.index, 0);
  assert.deepEqual(tabs, [steps[0].tab, steps[1].tab, steps[0].tab]);
  w.document.querySelector('.demo-tour-close').click();
  assert.equal(w.document.querySelector('.demo-tour'), null);
  assert.ok(w.localStorage.getItem('lsmDemoTourSeen'));
});

for (const [how, pick] of [
  ['its button', (w) => w.document.querySelector('.demo-welcome-explore').click()],
  ['Esc', (w) => w.document.body.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))],
  ['a click outside the card', (w) => {
    const root = w.document.querySelector('.demo-welcome');
    root.dispatchEvent(new w.Event('pointerdown', { bubbles: true }));
    root.click();
  }],
]) {
  test(`explore on my own by ${how} closes the welcome card without a tour and sets the flag`, async () => {
    const tracked = [];
    const w = await dom(['demo-tour.js'], { before: (w) => { w.DemoTrack = (t, f) => tracked.push([t, f.target]); } });
    pick(w);
    assert.equal(w.document.querySelector('.demo-welcome'), null);
    assert.equal(w.document.querySelector('.demo-tour'), null);
    assert.ok(w.localStorage.getItem('lsmDemoTourSeen'));
    assert.deepEqual(tracked, [['click', 'welcome-explore']]);
  });
}

test('the welcome card stays closed on the next visit', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  assert.equal(w.document.querySelector('.demo-welcome'), null);
});

function towerStubs(w, calls) {
  const ov = () => w.document.getElementById('towerOverlay');
  w.towerOpen = () => { calls.push('open'); ov().classList.add('open'); };
  w.towerClose = (o) => { calls.push('close:' + (o && o.returnFocus)); ov().classList.remove('open'); };
}

test('a drag that ends outside the welcome card does not close it', async () => {
  const w = await dom(['demo-tour.js']);
  w.document.querySelector('.demo-welcome-card').dispatchEvent(new w.Event('pointerdown', { bubbles: true }));
  w.document.querySelector('.demo-welcome').click();
  assert.ok(w.document.querySelector('.demo-welcome'));
});

test('a step whose element is below the window scrolls it into view and keeps the card on screen', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  Object.defineProperty(w, 'innerHeight', { configurable: true, value: 844 });
  const strip = w.document.getElementById('ovAgentsStrip');
  let top = 1153;
  strip.getBoundingClientRect = () => ({ top, bottom: top + 170, left: 10, right: 380, width: 370, height: 170, x: 10, y: top });
  const scrolled = [];
  strip.scrollIntoView = (o) => { scrolled.push(o.block); top = 300; };
  w.DemoTour.start(w.DemoTour.steps.findIndex((s) => s.selector === '#ovAgentsStrip'));
  assert.deepEqual(scrolled, ['center']);
  const card = w.document.querySelector('.demo-tour-card');
  assert.ok(parseFloat(card.style.top) >= 12 && parseFloat(card.style.top) <= 844 - 12);
  w.DemoTour.close();
});

test('the tour leaves a Tower the visitor opened alone', async () => {
  const calls = [];
  const w = await dom(['demo-tour.js'], { before: (w) => { w.localStorage.setItem('lsmDemoTourSeen', '1'); towerStubs(w, calls); } });
  w.document.getElementById('towerOverlay').classList.add('open');
  const i = w.DemoTour.steps.findIndex((s) => s.tower);
  w.DemoTour.start(i);
  w.DemoTour.next();
  w.DemoTour.close();
  assert.deepEqual(calls, []);
  assert.ok(w.document.getElementById('towerOverlay').classList.contains('open'));
});

test('the Ask Tower step opens Tower and the tour closes it again when it moves on or closes', async () => {
  const calls = [];
  const w = await dom(['demo-tour.js'], { before: (w) => { w.localStorage.setItem('lsmDemoTourSeen', '1'); towerStubs(w, calls); } });
  const i = w.DemoTour.steps.findIndex((s) => s.tower);
  w.DemoTour.start(i - 1);
  assert.deepEqual(calls, []);
  w.DemoTour.next();
  assert.deepEqual(calls, ['open']);
  w.DemoTour.next();
  assert.deepEqual(calls, ['open', 'close:false']);
  w.DemoTour.back();
  w.DemoTour.close();
  assert.deepEqual(calls, ['open', 'close:false', 'open', 'close:false']);
});

test('finishing the tour sets the flag; the banner Tour button restarts it; tab buttons are clicked without switchTab', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  const clicked = [];
  w.document.querySelectorAll('.tab-btn').forEach((b) => b.addEventListener('click', () => clicked.push(b.id)));
  const banner = w.document.getElementById('demoBanner');
  assert.equal(w.document.body.firstElementChild, banner);
  assert.equal(banner.textContent, 'Demo with sample dataInstallation directionsTourPhone version');
  assert.equal(banner.querySelector('a').href, 'https://get.llmsyscore.com/?ref=demo');
  w.localStorage.removeItem('lsmDemoTourSeen');
  banner.querySelector('.demo-banner-tour').click();
  assert.equal(w.DemoTour.index, 0);
  assert.equal(clicked[0], 'tabBtnOverall');
  for (let i = 0; i < w.DemoTour.steps.length; i++) w.document.querySelector('.demo-tour-next').click();
  assert.equal(w.document.querySelector('.demo-tour'), null);
  assert.ok(w.localStorage.getItem('lsmDemoTourSeen'));
  assert.ok(clicked.includes('tabBtnAdmin') && clicked.includes('tabBtnEvents'));
});

test('steps with a sub-tab click its button; each step stays closable', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  const subs = [];
  w.document.querySelectorAll('.sub-tab-btn').forEach((b) => b.addEventListener('click', () => subs.push(b.id)));
  const steps = w.DemoTour.steps;
  for (let i = 0; i < steps.length; i++) {
    w.DemoTour.start(i);
    assert.equal(w.DemoTour.index, i);
    assert.ok(w.document.querySelector('.demo-tour-close'), `close on step ${i + 1}`);
    if (steps[i].sub) assert.equal(subs[subs.length - 1], steps[i].sub);
    w.document.querySelector('.demo-tour-close').click();
    assert.equal(w.document.querySelector('.demo-tour'), null);
  }
  assert.equal(JSON.stringify(subs), JSON.stringify(steps.filter((s) => s.sub).map((s) => s.sub)));
});

test('a thin target rings its nearest ancestor that is at least 40 px tall', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  const box = (top, h) => () => ({ top, bottom: top + h, left: 10, right: 410, width: 400, height: h, x: 10, y: top });
  w.document.getElementById('secModels').getBoundingClientRect = box(100, 2);
  w.document.getElementById('llm-ctrl-wrap').getBoundingClientRect = box(90, 300);
  w.DemoTour.start(w.DemoTour.steps.findIndex((s) => s.selector === '#secModels'));
  const ring = w.document.querySelector('.demo-tour-ring');
  assert.equal(ring.style.height, '308px');
  assert.equal(ring.style.top, '86px');
  w.DemoTour.close();
});

test('tour still starts and closes when storage throws', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => {
    Object.defineProperty(w, 'localStorage', { configurable: true, get() { throw new w.DOMException('blocked', 'SecurityError'); } });
  } });
  w.document.querySelector('.demo-welcome-tour').click();
  assert.ok(w.document.querySelector('.demo-tour-card'));
  w.document.querySelector('.demo-tour-close').click();
  assert.equal(w.document.querySelector('.demo-tour'), null);
});

test('the tour card is the only element the tour adds with pointer-events: auto', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  const before = new Set(w.document.querySelectorAll('*'));
  w.DemoTour.start(0);
  const added = [...w.document.querySelectorAll('*')].filter((e) => !before.has(e));
  assert.ok(added.length > 0);
  const auto = added.filter((e) => e.style.pointerEvents === 'auto');
  assert.deepEqual(auto, [w.document.querySelector('.demo-tour-card')]);
  assert.equal(w.document.querySelector('.demo-tour').style.pointerEvents, 'none');
  assert.equal(w.document.querySelector('.demo-tour-ring').style.pointerEvents, 'none');
  assert.equal(w.document.querySelector('.demo-backdrop, .demo-tour-backdrop'), null);
});

test('terminal panels get the install card when they become visible; log panels do not', async () => {
  const w = await dom(['demo-page.js'], { before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  const term = w.document.getElementById('terminalPanel');
  assert.equal(term.querySelector('.demo-cover'), null);
  term.style.display = '';
  await new Promise((r) => setTimeout(r, 0));
  const cover = term.querySelector('.demo-cover');
  assert.ok(cover);
  assert.match(cover.textContent, /This works on your own install\./);
  const a = cover.querySelector('a');
  assert.equal(a.textContent, 'Installation directions');
  assert.equal(a.href, 'https://get.llmsyscore.com/?ref=demo');
  term.style.display = 'none';
  term.style.display = '';
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(term.querySelectorAll('.demo-cover').length, 1);
  const log = w.document.getElementById('serverLogPanel');
  log.style.display = 'block';
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(log.querySelector('.demo-cover'), null, 'log panels show their sample lines');
  assert.deepEqual([...w.DemoPage.PANEL_IDS], ['terminalPanel', 'lmsTerminalPanel', 'vllmTerminalPanel']);
});

test('tour steps and tab buttons match the real dashboard page', () => {
  const html = readFileSync(join(RUNTIME, '..', '..', 'llm-systems-manager', 'frontend', 'index.html'), 'utf8');
  const w = new JSDOM(html, { url: 'https://demo.example/dashboard' }).window;
  const tour = new JSDOM('<body></body>', { url: 'https://demo.example/dashboard', runScripts: 'outside-only' }).window;
  tour.localStorage.setItem('lsmDemoTourSeen', '1');
  tour.eval(src('demo-tour.js'));
  for (const s of tour.DemoTour.steps) {
    assert.ok(w.document.querySelector(s.selector), s.selector);
    assert.ok(w.document.querySelector(`.tab-btn[onclick="switchTab('${s.tab}')"]`), s.tab);
  }
});

for (const [kind, fire] of [
  ['click', (w) => w.document.getElementById('tabBtnAdmin').dispatchEvent(new w.Event('pointerdown', { bubbles: true }))],
  ['key', (w) => w.document.body.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))],
]) {
  test(`tour does not auto-start after a ${kind} before its delay; the Tour button still starts it`, async () => {
    const tabs = [];
    const w = await dom(['demo-tour.js'], { before: (w) => { w.switchTab = (t) => tabs.push(t); }, after: fire });
    assert.equal(w.document.querySelector('.demo-tour'), null);
    assert.equal(w.document.querySelector('.demo-welcome'), null);
    assert.deepEqual(tabs, []);
    assert.equal(w.localStorage.getItem('lsmDemoTourSeen'), null);
    w.document.querySelector('.demo-banner-tour').click();
    assert.ok(w.document.querySelector('.demo-tour-card'));
    assert.deepEqual(tabs, [w.DemoTour.steps[0].tab]);
  });
}

test('a terminal panel inside a class-hidden section gets its card once the section is shown', async () => {
  const w = await dom(['demo-page.js'], { before: (w) => {
    const st = w.document.createElement('style');
    st.textContent = '.gone { display: none; }';
    w.document.head.appendChild(st);
    const term = w.document.getElementById('terminalPanel');
    term.parentElement.classList.add('gone');
    term.style.display = '';
  } });
  const term = w.document.getElementById('terminalPanel');
  assert.equal(term.querySelector('.demo-cover'), null);
  term.parentElement.classList.remove('gone');
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(term.querySelector('.demo-cover'));
});

test('the tab panels top follows the measured tab bar bottom', async () => {
  const w = await dom(['demo-page.js'], { before: (w) => {
    w.document.querySelector('.tab-nav').getBoundingClientRect = () => ({ top: 80, bottom: 123.4, left: 0, right: 0, width: 0, height: 43.4 });
  } });
  assert.equal(w.document.documentElement.style.getPropertyValue('--demo-panel-top'), '123px');
  const css = src('demo-tour.css');
  assert.match(css, /#eventsTab, #toolsTab, #adminTab \{ top: var\(--demo-panel-top/);
});

// Runs the bootstrap page's inline script with a given user agent; returns registered workers and redirects.
function runBootstrap(userAgent, { controller = null, window = {} } = {}) {
  const html = src('bootstrap.html');
  const code = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));
  const registered = [], replaced = [];
  const serviceWorker = { controller, addEventListener() {}, register: (u) => { registered.push(u); return new Promise(() => {}); } };
  const document = { getElementById: () => ({ style: {} }) };
  new Function('navigator', 'location', 'document', 'window', 'setTimeout', code)(
    { userAgent, serviceWorker }, { replace: (u) => replaced.push(u) }, document, window, () => 0);
  return { registered, replaced };
}

test('the bootstrap page opens /phone once when the phone app sent the visitor there, else the dashboard', () => {
  const ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1';
  const store = { lsmDemoReturn: '/phone' };
  const sessionStorage = { getItem: (k) => (k in store ? store[k] : null), removeItem: (k) => { delete store[k]; } };
  assert.deepEqual(runBootstrap(ua, { controller: {}, window: { sessionStorage } }).replaced, ['/phone']);
  assert.deepEqual(store, {});
  assert.deepEqual(runBootstrap(ua, { controller: {}, window: { sessionStorage } }).replaced, ['/dashboard']);
  assert.deepEqual(runBootstrap(ua, { controller: {} }).replaced, ['/dashboard']);
});

test('bootstrap page registers the demo worker for browsers but stays put for search crawlers', () => {
  for (const ua of ['Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15',
    'Mozilla/5.0 (Linux; Android 11; CUBOT KINGKONG 5 Pro) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Instagram 350.0.0']) {
    assert.deepEqual(runBootstrap(ua).registered, ['/demo-sw.js'], ua);
  }
  for (const ua of ['Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm) Chrome/116.0 Safari/537.36',
    'Mozilla/5.0 (Linux; Android 6.0.1) Chrome/130.0 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.1.1 Safari/605.1.15 (Applebot/0.1)',
    'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)']) {
    assert.deepEqual(runBootstrap(ua), { registered: [], replaced: [] });
  }
});

test('Ask Tower step with Tower closed hides the ring and offers Open Tower; with Tower open it rings the box', async () => {
  const calls = [];
  const w = await dom(['demo-tour.js'], { before: (w) => { w.localStorage.setItem('lsmDemoTourSeen', '1'); towerStubs(w, calls); } });
  const ov = w.document.getElementById('towerOverlay');
  const i = w.DemoTour.steps.findIndex((s) => s.tower);
  w.document.getElementById('towerAside').getBoundingClientRect = () => ({ top: 100, bottom: 300, left: 10, right: 400, width: 390, height: 200 });
  w.DemoTour.start(i);
  const ring = w.document.querySelector('.demo-tour-ring');
  assert.equal(ring.style.display, '');
  assert.equal(w.document.querySelector('.demo-tour-open-tower'), null);
  ov.classList.remove('open');
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(ring.style.display, 'none');
  w.DemoTour.next();
  w.DemoTour.back();
  assert.equal(w.document.querySelector('.demo-tour-open-tower'), null);
  ov.classList.remove('open');
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(w.document.querySelector('.demo-tour-open-tower'));
  const before = calls.length;
  w.document.querySelector('.demo-tour-open-tower').click();
  assert.equal(calls.length, before + 1);
  assert.equal(calls[calls.length - 1], 'open');
  assert.equal(w.document.querySelector('.demo-tour-open-tower'), null);
  assert.equal(ring.style.display, '');
  w.DemoTour.close();
});

test('the banner, the last tour step and the welcome card each carry an install link', async () => {
  const w = await dom(['demo-tour.js']);
  const hasInstall = (a) => a && /get\.llmsyscore\.com/.test(a.href) && a.target === '_blank';
  assert.ok(hasInstall(w.document.querySelector('#demoBanner a.demo-banner-install')));
  const welcome = w.document.querySelector('.demo-welcome-install');
  assert.ok(hasInstall(welcome));
  welcome.addEventListener('click', (e) => e.preventDefault());
  welcome.click();
  assert.ok(w.document.querySelector('.demo-welcome'));
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(w.document.querySelector('.demo-welcome'), null);
  w.DemoTour.start(w.DemoTour.steps.length - 1);
  assert.ok(hasInstall(w.document.querySelector('.demo-tour-card a.demo-tour-install')));
  assert.match(w.document.querySelector('.demo-tour-next').className, /demo-btn-ghost/);
  w.DemoTour.start(0);
  assert.equal(w.document.querySelector('.demo-tour-install'), null);
  w.DemoTour.close();
});

const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1';
const COMPANION_HTML = readFileSync(join(RUNTIME, '..', '..', 'llm-systems-manager', 'frontend', 'companion.html'), 'utf8');

// Makes the jsdom window look like a phone or desktop: width, pointer and user agent.
function device(w, { width = 390, coarse = true, ua = IPHONE_UA } = {}) {
  Object.defineProperty(w, 'innerWidth', { value: width, configurable: true });
  w.matchMedia = (q) => ({ matches: q === '(pointer: coarse)' ? coarse : false });
  Object.defineProperty(w.navigator, 'userAgent', { value: ua, configurable: true });
}

test('phones are narrow windows with touch or a mobile browser; tablets and desktops are not', async () => {
  const w = await dom([], { before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  w.eval(src('demo-tour.js'));
  const DESK_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36';
  const IPAD_UA = 'Mozilla/5.0 (iPad; CPU OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1';
  for (const [opts, want] of [
    [{}, true],
    [{ coarse: false }, true],
    [{ ua: DESK_UA }, true],
    [{ ua: DESK_UA, coarse: false }, false],
    [{ width: 820, ua: IPAD_UA }, false],
    [{ width: 1600, ua: DESK_UA, coarse: false }, false],
    [{ width: 641 }, false],
  ]) {
    device(w, opts);
    assert.equal(w.DemoTour.isPhone(), want, JSON.stringify(opts));
  }
});

test('on a phone the welcome card suggests a desktop and offers the desktop version, the phone app and the install directions', async () => {
  const tracked = [];
  const w = await dom(['demo-tour.js'], { before: (w) => { device(w); w.DemoTrack = (t, f) => tracked.push([t, f.target]); } });
  const card = w.document.querySelector('.demo-welcome-card');
  assert.equal(card.querySelector('.demo-welcome-title').textContent, 'Welcome to the LLM Systems Manager demo');
  assert.match(card.textContent, /best viewed on a desktop or tablet/);
  assert.equal(card.querySelector('.demo-welcome-tour'), null);
  const phone = card.querySelector('a.demo-welcome-phone');
  assert.equal(phone.textContent, 'Switch to the phone app');
  assert.equal(new URL(phone.href).pathname, '/phone');
  assert.equal(phone.target, '');
  assert.equal(card.querySelector('.demo-welcome-desktop').textContent, 'Open the desktop version');
  const inst = card.querySelector('a.demo-welcome-install');
  assert.equal(inst.textContent, 'Installation directions');
  assert.equal(inst.href, 'https://get.llmsyscore.com/?ref=demo');
  assert.equal(w.document.activeElement, phone);
  card.querySelector('.demo-welcome-desktop').click();
  assert.equal(w.document.querySelector('.demo-welcome'), null);
  assert.equal(w.document.querySelector('.demo-tour'), null);
  assert.ok(w.localStorage.getItem('lsmDemoTourSeen'));
  assert.deepEqual(tracked, [['click', 'welcome-desktop']]);
});

test('on a phone, Switch to the phone app is recorded and goes to /phone; Esc closes it', async () => {
  const tracked = [];
  const w = await dom(['demo-tour.js'], { before: (w) => { device(w); w.DemoTrack = (t, f) => tracked.push([t, f.target]); } });
  const phone = w.document.querySelector('a.demo-welcome-phone');
  let navigated = null;
  phone.addEventListener('click', (e) => { navigated = e.defaultPrevented ? null : new URL(phone.href).pathname; e.preventDefault(); });
  phone.click();
  assert.equal(navigated, '/phone');
  assert.equal(w.document.querySelector('.demo-welcome'), null);
  assert.ok(w.localStorage.getItem('lsmDemoTourSeen'));
  assert.deepEqual(tracked, [['click', 'welcome-phone']]);
  const w2 = await dom(['demo-tour.js'], { before: (w) => { device(w); w.DemoTrack = (t, f) => tracked.push([t, f.target]); } });
  w2.document.body.dispatchEvent(new w2.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(w2.document.querySelector('.demo-welcome'), null);
  assert.deepEqual(tracked.slice(1), [['click', 'welcome-dismiss']]);
});

test('the desktop welcome card keeps its tour and explore choices with the install directions', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => device(w, { width: 1600, coarse: false, ua: 'Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0' }) });
  const card = w.document.querySelector('.demo-welcome-card');
  assert.match(card.textContent, /real dashboard running on sample data/);
  assert.deepEqual([...card.querySelectorAll('.demo-welcome-nav > *')].map((e) => e.textContent),
    ['Take the 1-minute tour', 'Explore on my own', 'Installation directions']);
  assert.equal(card.querySelector('.demo-welcome-phone'), null);
});

test('the dashboard banner has the install directions, Tour and a Phone version link that is recorded', async () => {
  const tracked = [];
  const w = await dom(['demo-tour.js'], { before: (w) => { w.localStorage.setItem('lsmDemoTourSeen', '1'); w.DemoTrack = (t, f) => tracked.push([t, f.target]); } });
  const b = w.document.getElementById('demoBanner');
  const inst = b.querySelector('a.demo-banner-install');
  assert.equal(inst.textContent, 'Installation directions');
  assert.equal(inst.href, 'https://get.llmsyscore.com/?ref=demo');
  assert.equal(inst.target, '_blank');
  assert.equal(inst.rel, 'noopener');
  assert.match(inst.className, /demo-btn(?! demo-btn-ghost)/);
  assert.match(b.querySelector('.demo-banner-tour').className, /demo-btn-ghost/);
  const sw = b.querySelector('a.demo-banner-switch');
  assert.equal(sw.textContent, 'Phone version');
  assert.equal(new URL(sw.href).pathname, '/phone');
  sw.addEventListener('click', (e) => e.preventDefault());
  sw.click();
  assert.deepEqual(tracked, [['click', 'switch-phone']]);
});

test('on /phone the banner offers the desktop version, shows no welcome card and the Tour walks the phone screens', async () => {
  const tracked = [];
  const w = await dom(['demo-tour.js'], { url: 'https://demo.example/phone', html: COMPANION_HTML,
    before: (w) => { device(w); w.DemoTrack = (t, f) => tracked.push([t, f.target]); } });
  assert.equal(w.document.querySelector('.demo-welcome'), null);
  const b = w.document.getElementById('demoBanner');
  assert.equal(w.document.body.firstElementChild, b);
  assert.equal(b.textContent, 'Demo with sample dataInstallation directionsTourDesktop version');
  const sw = b.querySelector('a.demo-banner-switch');
  assert.equal(new URL(sw.href).pathname, '/dashboard');
  sw.addEventListener('click', (e) => e.preventDefault());
  sw.click();
  assert.deepEqual(tracked, [['click', 'switch-desktop']]);
  const steps = w.DemoTour.steps;
  assert.equal(steps, w.DemoTour.phoneSteps);
  assert.ok(steps.length >= 4 && steps.length <= 6);
  const clicked = [];
  w.document.querySelectorAll('#tabbar .tab').forEach((t) => t.addEventListener('click', () => clicked.push(t.dataset.tab)));
  b.querySelector('.demo-banner-tour').click();
  for (let i = 1; i < steps.length; i++) w.document.querySelector('.demo-tour-next').click();
  assert.deepEqual(clicked, [...steps].map((s) => s.screen));
  assert.ok(w.document.querySelector('.demo-tour-card a.demo-tour-install'));
  w.document.querySelector('.demo-tour-next').click();
  assert.equal(w.document.querySelector('.demo-tour'), null);
});

test('phone tour steps match the real phone app page', () => {
  const w = new JSDOM(COMPANION_HTML, { url: 'https://demo.example/phone' }).window;
  const tour = new JSDOM('<body></body>', { url: 'https://demo.example/dashboard', runScripts: 'outside-only' }).window;
  tour.localStorage.setItem('lsmDemoTourSeen', '1');
  tour.eval(src('demo-tour.js'));
  for (const s of tour.DemoTour.phoneSteps) {
    assert.ok(w.document.querySelector(s.selector), s.selector);
    assert.ok(w.document.querySelector(`#tabbar .tab[data-tab="${s.screen}"]`), s.screen);
    assert.ok(s.title && s.text && !/\bagents?\b/i.test(s.text), s.title);
  }
});

test('the phone layer reports the phone app and each screen change, and blocks the app worker and push', async () => {
  const tabs = [], registered = [];
  const d = new JSDOM(COMPANION_HTML, { url: 'https://demo.example/phone', runScripts: 'outside-only' });
  const w = d.window;
  const sw = { register: (u) => { registered.push(u); return Promise.resolve({ scope: '/' }); } };
  Object.defineProperty(w.navigator, 'serviceWorker', { value: sw, configurable: true });
  w.document.addEventListener('lsm:tab', (e) => tabs.push(e.detail.tab));
  w.eval(src('demo-phone.js'));
  assert.deepEqual(tabs, ['phone']);
  await assert.rejects(w.navigator.serviceWorker.register('/sw.js', { scope: '/companion' }), /Not available in the demo/);
  assert.deepEqual(registered, []);
  await w.navigator.serviceWorker.register('/demo-sw.js', { scope: '/' });
  assert.deepEqual(registered, ['/demo-sw.js']);
  const tab = (n) => w.document.querySelector(`#tabbar .tab[data-tab="${n}"]`);
  const show = (n) => w.document.querySelectorAll('#tabbar .tab').forEach((b) => b.classList.toggle('on', b === tab(n)));
  show('glance');
  await new Promise((r) => setTimeout(r, 0));
  show('alerts');
  await new Promise((r) => setTimeout(r, 0));
  show('alerts');
  show('energy');
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(tabs, ['phone', 'phone-alerts', 'phone-energy']);
  w.document.getElementById('pushStatus').textContent = 'service worker installing…';
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(w.document.getElementById('pushStatus').textContent, 'Not available in the demo');
});

test('the tour card sits outside the highlighted area: below, above, then beside it', async () => {
  const w = await dom(['demo-tour.js']);
  const spot = w.DemoTour.cardSpot;
  const W = 1280, H = 800, cw = 320, ch = 200;
  const overlaps = (p, a) => !(p.x + cw <= a.left || p.x >= a.right || p.y + ch <= a.top || p.y >= a.bottom);
  const below = { left: 100, right: 600, top: 100, bottom: 300 };
  assert.deepEqual(JSON.parse(JSON.stringify(spot(below, cw, ch, W, H))), { x: 100, y: 312 });
  const drawer = { left: 820, right: 1278, top: 2, bottom: 798 };
  const p = spot(drawer, cw, ch, W, H);
  assert.ok(!overlaps(p, drawer));
  assert.equal(p.x, 820 - 12 - cw);
  const full = { left: 2, right: 1278, top: 2, bottom: 798 };
  assert.equal(spot(full, cw, ch, W, H).y, H - ch - 24);
});
