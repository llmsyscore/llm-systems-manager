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
  <div id="llm-ctrl-wrap"><div class="llm-section" id="secModels"></div></div><div id="llm-llamacpp"></div><div id="llm-tools"><button id="blRunBtn">Run live benchmark</button><span id="blStatus">idle</span></div>
  <div id="terminalPanel" style="display:none;"><div id="terminalMount"></div></div>
  <div id="serverLogPanel" style="display:none;"><div id="serverLogBox"></div></div>
</div>
<div id="eventsTab"><iframe id="alarmEngineIframe"></iframe></div>
<div id="settingsOverlay"><aside class="sd"><section id="sdAppearance"></section></aside></div>
<div id="towerOverlay"><aside id="towerAside"><div id="twBox"><textarea id="twInput"></textarea></div></aside></div>
<div id="adminTab"><div id="adminHealthCard"></div>
  <button class="sub-tab-btn" id="subTabBtnAdminAgents">Agents</button><button class="sub-tab-btn" id="subTabBtnAdminRouting">Gateway</button>
  <button class="sub-tab-btn" id="subTabBtnAdminJobs">Jobs</button>
  <div id="admin-agents"></div><div id="admin-routing"></div><div id="admin-jobs"></div></div>
</body></html>`;

// Gives the page's first section a height so the welcome card sees a painted page.
function paint(w) {
  const e = w.document.querySelector('#overallTab .ov-hero, #glanceStrip');
  if (e) e.getBoundingClientRect = () => ({ top: 100, bottom: 400, left: 10, right: 1000, width: 990, height: 300, x: 10, y: 100 });
}

// Builds a jsdom dashboard, runs the given runtime scripts in it and waits wait ms; painted:false leaves the page unpainted.
async function dom(scripts, { before, after, url = 'https://demo.example/dashboard', html = DASH_HTML, wait = 1300, painted = true } = {}) {
  const d = new JSDOM(html, { url, runScripts: 'outside-only' });
  const w = d.window;
  if (painted) paint(w);
  if (before) before(w);
  for (const f of scripts.includes('demo-tour.js') ? ['demo-device.js', 'demo-install.js', ...scripts] : scripts) w.eval(src(f));
  if (after) after(w);
  await new Promise((r) => setTimeout(r, wait));
  return w;
}

test('tour does not start when the seen flag is set', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  assert.equal(w.document.querySelector('.demo-tour'), null);
  assert.ok(w.document.getElementById('demoBanner'), 'banner still shows');
});

test('a first visit on a painted page shows the welcome card at once; Ask starts the tour at Ask Tower, which walks the steps and Close sets the flag', async () => {
  const tabs = [], tracked = [];
  const w = await dom(['demo-tour.js'], { wait: 0, before: (w) => {
    w.switchTab = (t) => tabs.push(t);
    w.DemoTrack = (t, f) => tracked.push(t === 'tour' ? [t, f.action, f.step] : [t, f.target]);
  } });
  const steps = w.DemoTour.steps;
  assert.equal(w.document.querySelector('.demo-tour'), null, 'the tour waits for the welcome choice');
  const welcome = w.document.querySelector('.demo-welcome-card');
  assert.ok(welcome, 'shown at once on a painted page');
  assert.equal(welcome.getAttribute('role'), 'dialog');
  assert.equal(welcome.querySelector('.demo-welcome-title').textContent, 'Welcome to the LLM Systems Manager demo');
  assert.equal(welcome.querySelector('.demo-welcome-text').textContent, 'This is the real dashboard running on sample data.');
  assert.deepEqual([...welcome.querySelectorAll('.demo-welcome-nav > *')].map((e) => e.className.split(' ').pop()),
    ['demo-welcome-ask', 'demo-welcome-tour', 'demo-welcome-explore', 'demo-welcome-install']);
  assert.equal(w.document.activeElement, welcome.querySelector('.demo-welcome-ask'));
  welcome.querySelector('.demo-welcome-ask').click();
  assert.equal(w.document.querySelector('.demo-welcome'), null);
  assert.deepEqual(tracked, [['click', 'welcome-shown'], ['click', 'welcome-ask'], ['tour', 'start', 1]]);
  assert.equal(w.DemoTour.index, 1);
  assert.equal(steps.length, 7);
  assert.deepEqual([...steps.map((s) => s.title)], ['Your whole fleet', 'Ask Tower', 'Energy and cost', 'Forecast',
    'Models and Autotune', 'Try it: run a benchmark', 'Install it']);
  assert.ok(steps.every((s) => s.text.split(/(?<=\.|\?"?)\s+(?=[A-Z])/).length <= 2), 'one or two sentences per step');
  assert.ok(steps.every((s) => !/\bagents?\b|turned off/i.test(s.text)), 'plain words: no "agent", no reassurance');
  assert.match(steps.find((s) => s.sub === 'subTabBtnLlmLlamacpp').text, /Autotune tries .* keeps the fastest/);
  for (const s of steps.filter((s) => !s.install)) {
    assert.ok(w.document.querySelector(s.selector), s.selector);
    assert.ok(s.title && s.text, s.selector);
  }
  assert.ok(steps.some((s) => s.selector === '#towerAside' && s.tower && s.ask));
  const card = w.document.querySelector('.demo-tour-card');
  assert.equal(card.querySelector('.demo-tour-title').textContent, 'Ask Tower');
  card.querySelector('.demo-tour-back').click();
  assert.equal(w.DemoTour.index, 0);
  assert.equal(w.document.querySelector('.demo-tour-back').disabled, true);
  w.document.querySelector('.demo-tour-next').click();
  assert.equal(w.DemoTour.index, 1);
  assert.deepEqual(tabs, [steps[1].tab, steps[0].tab, steps[1].tab]);
  w.document.querySelector('.demo-tour-close').click();
  assert.equal(w.document.querySelector('.demo-tour'), null);
  assert.ok(w.localStorage.getItem('lsmDemoTourSeen'));
});

test('the welcome tour button starts the tour at the first step', async () => {
  const tracked = [];
  const w = await dom(['demo-tour.js'], { wait: 0, before: (w) => { w.DemoTrack = (t, f) => tracked.push(t === 'tour' ? [t, f.action, f.step] : [t, f.target]); } });
  w.document.querySelector('.demo-welcome-tour').click();
  assert.equal(w.DemoTour.index, 0);
  assert.deepEqual(tracked, [['click', 'welcome-shown'], ['click', 'welcome-tour'], ['tour', 'start', 0]]);
  w.DemoTour.close();
});

for (const [how, want, pick] of [
  ['its button', 'welcome-explore', (w) => w.document.querySelector('.demo-welcome-explore').click()],
  ['Esc', 'welcome-close', (w) => w.document.body.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))],
  ['a click outside the card', 'welcome-close', (w) => {
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
    assert.deepEqual(tracked, [['click', 'welcome-shown'], ['click', want]]);
  });
}

test('leaving the page with the welcome card open is recorded once and sent right away; a later choice is still recorded', async () => {
  const tracked = [], flushed = [];
  const w = await dom(['demo-tour.js'], { before: (w) => {
    w.DemoTrack = (t, f) => tracked.push([t, f.target]);
    w.DemoAnalytics = { flush: (leaving) => flushed.push(leaving) };
  } });
  Object.defineProperty(w.document, 'visibilityState', { value: 'hidden', configurable: true });
  w.document.dispatchEvent(new w.Event('visibilitychange'));
  w.dispatchEvent(new w.Event('pagehide'));
  assert.deepEqual(tracked, [['click', 'welcome-shown'], ['click', 'welcome-left']]);
  assert.deepEqual(flushed, [true]);
  w.document.querySelector('.demo-welcome-explore').click();
  w.dispatchEvent(new w.Event('pagehide'));
  assert.deepEqual(tracked.slice(2), [['click', 'welcome-explore']]);
});

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
  const strip = w.document.getElementById('dash-energy');
  let top = 1153;
  strip.getBoundingClientRect = () => ({ top, bottom: top + 170, left: 10, right: 380, width: 370, height: 170, x: 10, y: top });
  const scrolled = [];
  strip.scrollIntoView = (o) => { scrolled.push(o.block); top = 300; };
  w.DemoTour.start(w.DemoTour.steps.findIndex((s) => s.selector === '#dash-energy'));
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
  assert.equal(banner.textContent, 'Demo with sample dataInstallation directionsShareGitHubTourPhone version');
  assert.equal(banner.querySelector('a').href, 'https://get.llmsysmanager.com/?ref=demo');
  w.localStorage.removeItem('lsmDemoTourSeen');
  banner.querySelector('.demo-banner-tour').click();
  assert.equal(w.DemoTour.index, 0);
  assert.equal(clicked[0], 'tabBtnOverall');
  for (let i = 0; i < w.DemoTour.steps.length; i++) w.document.querySelector('.demo-tour-next').click();
  assert.equal(w.document.querySelector('.demo-tour'), null);
  assert.ok(w.localStorage.getItem('lsmDemoTourSeen'));
  const ids = { overall: 'tabBtnOverall', dashboard: 'tabBtnDashboard', llm: 'tabBtnLlmControl' };
  assert.deepEqual(clicked, [...w.DemoTour.steps.filter((s) => s.tab).map((s) => ids[s.tab])]);
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
  assert.equal(a.href, 'https://get.llmsysmanager.com/?ref=demo');
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
  for (const s of tour.DemoTour.steps.filter((s) => !s.install)) {
    assert.ok(w.document.querySelector(s.selector), s.selector);
    assert.ok(w.document.querySelector(`.tab-btn[onclick="switchTab('${s.tab}')"]`), s.tab);
  }
});

for (const [kind, fire] of [
  ['click', (w) => w.document.getElementById('tabBtnAdmin').dispatchEvent(new w.Event('pointerdown', { bubbles: true }))],
  ['key', (w) => w.document.body.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))],
]) {
  test(`the welcome card shows even after an early ${kind}`, async () => {
    const w = await dom(['demo-tour.js'], { after: fire });
    assert.ok(w.document.querySelector('.demo-welcome-card'));
    assert.equal(w.document.querySelector('.demo-tour'), null);
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
function runBootstrap(userAgent, { controller = null, window = {}, getRegistration, register, setTimeout: timer = () => 0, clearTimeout: clearer = () => {} } = {}) {
  const html = src('bootstrap.html');
  const code = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));
  const registered = [], replaced = [], reloaded = [], els = {};
  const serviceWorker = { controller, addEventListener() {}, register: (u) => { registered.push(u); return register ? register() : new Promise(() => {}); } };
  if (getRegistration) serviceWorker.getRegistration = getRegistration;
  const links = ['screenshots', 'install'].map((t) => ({ t, on: {}, getAttribute: () => t, addEventListener(k, fn) { this.on[k] = fn; } }));
  const el = (id) => els[id] || (els[id] = { style: {}, on: {}, addEventListener(k, fn) { this.on[k] = fn; } });
  const document = { getElementById: el, querySelectorAll: (sel) => (sel === '.demo-load-link' ? links : []) };
  const nav = { userAgent, serviceWorker };
  new Function('navigator', 'location', 'document', 'window', 'setTimeout', 'clearTimeout', code)(
    nav, { replace: (u) => replaced.push(u), reload: () => reloaded.push(1) }, document, window, timer, clearer);
  const click = (id) => els[id].on.click();
  const shown = () => ['demoLoading', 'demoSlow', 'demoNotice'].filter((n) => els[n] && els[n].style.display === 'block');
  return { registered, replaced, reloaded, links, click, shown, serviceWorker };
}

test('the loading-page links carry ref=demo-loading, open a new tab, and report their click', () => {
  const html = src('bootstrap.html');
  const hrefs = [...html.matchAll(/<a class="demo-load-link"[^>]*>/g)].map((m) => m[0]);
  assert.equal(hrefs.length, 4);
  for (const a of hrefs) {
    assert.match(a, /href="https:\/\/(www\.llmsyscore\.com\/\?ref=demo-loading#screenshots|get\.llmsysmanager\.com\/\?ref=demo-loading)"/);
    assert.match(a, /target="_blank" rel="noopener"/);
  }
  assert.ok(!/install it on your own machine/.test(html));
  const seen = [];
  const { links } = runBootstrap('Mozilla/5.0 (X11; Linux x86_64) Firefox/133.0', { window: { DemoLanding: (s) => seen.push(s) } });
  links.forEach((l) => l.on.click());
  assert.deepEqual(seen, ['screenshots', 'install']);
});

test('retry waits on an installing worker instead of reloading', async () => {
  const worker = { state: 'installing', on: {}, addEventListener(k, fn) { this.on[k] = fn; } };
  const r = runBootstrap('Mozilla/5.0 Firefox/133.0', { getRegistration: () => Promise.resolve({ installing: worker }) });
  r.click('demoRetry');
  await new Promise((x) => setTimeout(x, 0));
  assert.deepEqual(r.replaced, []);
  worker.state = 'activated';
  r.serviceWorker.controller = {};
  worker.on.statechange();
  assert.deepEqual(r.replaced, ['/dashboard']);
  assert.equal(r.registered.length, 1);
});

test('retry re-registers when no worker is installing', async () => {
  const r = runBootstrap('Mozilla/5.0 Firefox/133.0', { getRegistration: () => Promise.resolve(undefined) });
  r.click('demoRetry');
  await new Promise((x) => setTimeout(x, 0));
  assert.equal(r.registered.length, 2);
  assert.deepEqual(r.shown(), ['demoLoading']);
});

test('retry re-registers when the waited-on worker becomes redundant', async () => {
  const worker = { state: 'installing', on: {}, addEventListener(k, fn) { this.on[k] = fn; } };
  const r = runBootstrap('Mozilla/5.0 Firefox/133.0', { getRegistration: () => Promise.resolve({ installing: worker }) });
  r.click('demoRetry');
  await new Promise((x) => setTimeout(x, 0));
  worker.state = 'redundant';
  worker.on.statechange();
  assert.equal(r.registered.length, 2);
  assert.deepEqual(r.replaced, []);
});

test('retry re-registers when getRegistration rejects', async () => {
  const r = runBootstrap('Mozilla/5.0 Firefox/133.0', { getRegistration: () => Promise.reject(new Error('x')) });
  r.click('demoRetry');
  await new Promise((x) => setTimeout(x, 0));
  assert.equal(r.registered.length, 2);
});

test('a stalled retry shows the still-loading notice again after 10 s and can be retried', async () => {
  const worker = { state: 'installing', on: {}, addEventListener(k, fn) { this.on[k] = fn; } };
  const timers = [];
  const r = runBootstrap('Mozilla/5.0 Firefox/133.0', { setTimeout: (fn) => timers.push(fn), getRegistration: () => Promise.resolve({ installing: worker }) });
  r.click('demoRetry');
  await new Promise((x) => setTimeout(x, 0));
  assert.deepEqual(r.shown(), ['demoLoading']);
  timers[timers.length - 1]();
  assert.deepEqual(r.shown(), ['demoSlow']);
  r.click('demoRetry');
  await new Promise((x) => setTimeout(x, 0));
  assert.deepEqual(r.shown(), ['demoLoading']);
});

test('a failed registration shows Try again and still reports failed', async () => {
  const seen = [];
  const r = runBootstrap('Mozilla/5.0 Firefox/133.0', { window: { DemoLanding: (s) => seen.push(s) }, register: () => Promise.reject(new Error('x')) });
  await new Promise((x) => setTimeout(x, 0));
  assert.deepEqual(r.shown(), ['demoSlow']);
  assert.ok(seen.includes('failed'));
});

test('retries do not stack slow timers', async () => {
  let next = 1;
  const live = new Set();
  const r = runBootstrap('Mozilla/5.0 Firefox/133.0', {
    setTimeout: () => { const id = next++; live.add(id); return id; }, clearTimeout: (id) => live.delete(id),
    getRegistration: () => Promise.resolve(undefined) });
  assert.equal(live.size, 1);
  r.click('demoRetry');
  await new Promise((x) => setTimeout(x, 0));
  assert.equal(live.size, 1);
});

test('retry reloads when a worker is active but not controlling', async () => {
  const r = runBootstrap('Mozilla/5.0 Firefox/133.0', { getRegistration: () => Promise.resolve({ active: {} }) });
  r.click('demoRetry');
  await new Promise((x) => setTimeout(x, 0));
  assert.equal(r.reloaded.length, 1);
  assert.equal(r.registered.length, 1);
});

test('retry is ignored once the page has moved on', async () => {
  const r = runBootstrap('Mozilla/5.0 Firefox/133.0', { controller: undefined, getRegistration: () => Promise.resolve(undefined) });
  r.serviceWorker.controller = {};
  r.click('demoRetry');
  r.click('demoRetry');
  assert.deepEqual(r.replaced, ['/dashboard']);
});

test('the Try again button is in the still-loading notice', () => {
  assert.match(src('bootstrap.html'), /<div id="demoSlow">[\s\S]*<button id="demoRetry" type="button">Try again<\/button>[\s\S]*<\/div>/);
});

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
    const { registered, replaced } = runBootstrap(ua);
    assert.deepEqual({ registered, replaced }, { registered: [], replaced: [] });
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

test('the banner and the welcome card carry the install link; every tour card has an Install button that opens the install card', async () => {
  const tracked = [];
  const w = await dom(['demo-tour.js'], { before: (w) => { w.DemoTrack = (t, f) => tracked.push([t, f.target]); } });
  const hasInstall = (a) => a && /get\.llmsysmanager\.com/.test(a.href) && a.target === '_blank';
  assert.ok(hasInstall(w.document.querySelector('#demoBanner a.demo-banner-install')));
  const welcome = w.document.querySelector('.demo-welcome-install');
  assert.ok(hasInstall(welcome));
  welcome.addEventListener('click', (e) => e.preventDefault());
  welcome.click();
  assert.ok(w.document.querySelector('.demo-welcome'));
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(w.document.querySelector('.demo-welcome'), null);
  assert.deepEqual(tracked, [['click', 'welcome-shown'], ['click', 'welcome-install']]);
  const steps = w.DemoTour.steps;
  for (let i = 0; i < steps.length; i++) {
    w.DemoTour.start(i);
    const btn = w.document.querySelector('.demo-tour-card .demo-tour-install');
    if (steps[i].install) assert.equal(btn, null, `step ${i + 1}`);
    else assert.equal(btn && btn.textContent, 'Install', `step ${i + 1}`);
  }
  w.DemoTour.start(2);
  tracked.length = 0;
  w.document.querySelector('.demo-tour-install').click();
  assert.deepEqual(tracked, [['click', 'tour-install-3']]);
  assert.equal(w.DemoTour.index, steps.length - 1);
  const card = w.document.querySelector('.demo-tour-card');
  assert.equal(card.querySelector('.demo-tour-title').textContent, 'Install it');
  assert.equal(card.querySelector('.demo-install-cmd').textContent, 'bash <(curl -fsSL https://get.llmsysmanager.com/install.sh)');
  assert.match(card.className, /demo-tour-card-center/);
  assert.equal(w.document.querySelector('.demo-tour-ring').style.display, 'none');
  assert.match(w.document.querySelector('.demo-tour-next').className, /demo-btn-ghost/);
  assert.equal(w.document.querySelector('.demo-tour-next').textContent, 'Done');
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
  w.eval(src('demo-device.js'));
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
    assert.equal(w.DemoDevice.isPhone(), want, JSON.stringify(opts));
  }
});

test('on /phone the welcome card shows the same three choices', async () => {
  const tracked = [];
  const w = await dom(['demo-tour.js'], { url: 'https://demo.example/phone', html: COMPANION_HTML,
    before: (w) => { device(w); w.DemoTrack = (t, f) => tracked.push(t === 'tour' ? [t, f.action, f.step] : [t, f.target]); } });
  const card = w.document.querySelector('.demo-welcome-card');
  assert.equal(card.querySelector('.demo-welcome-text').textContent, 'This is the real phone app running on sample data.');
  assert.deepEqual([...card.querySelectorAll('.demo-welcome-nav > *')].map((e) => e.textContent),
    ['Ask your fleet a question', 'Take the 1-minute tour', 'Explore on my own', 'Installation directions']);
  assert.equal(card.querySelector('.demo-welcome-phone'), null);
  assert.equal(w.document.activeElement, card.querySelector('.demo-welcome-ask'));
  card.querySelector('.demo-welcome-ask').click();
  const i = w.DemoTour.phoneSteps.findIndex((s) => s.ask);
  assert.equal(w.DemoTour.phoneSteps[i].screen, 'tower');
  assert.equal(w.DemoTour.index, i);
  assert.deepEqual(tracked, [['click', 'welcome-shown'], ['click', 'welcome-ask'], ['tour', 'start', i]]);
  w.DemoTour.close();
});

test('the desktop welcome card offers Ask, Tour, Explore and the install link', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => device(w, { width: 1600, coarse: false, ua: 'Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0' }) });
  const card = w.document.querySelector('.demo-welcome-card');
  assert.match(card.textContent, /real dashboard running on sample data/);
  assert.deepEqual([...card.querySelectorAll('.demo-welcome-nav > *')].map((e) => e.textContent),
    ['Ask your fleet a question', 'Take the 1-minute tour', 'Explore on my own', 'Installation directions']);
  assert.equal(card.querySelector('.demo-welcome-phone'), null);
});

test('the dashboard banner has the install directions, Tour and a Phone version link that is recorded', async () => {
  const tracked = [];
  const w = await dom(['demo-tour.js'], { before: (w) => { w.localStorage.setItem('lsmDemoTourSeen', '1'); w.DemoTrack = (t, f) => tracked.push([t, f.target]); } });
  const b = w.document.getElementById('demoBanner');
  const inst = b.querySelector('a.demo-banner-install');
  assert.equal(inst.textContent, 'Installation directions');
  assert.equal(inst.href, 'https://get.llmsysmanager.com/?ref=demo');
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

test('on /phone the banner offers the desktop version and the Tour walks the phone screens to the install card', async () => {
  const tracked = [];
  const w = await dom(['demo-tour.js'], { url: 'https://demo.example/phone', html: COMPANION_HTML,
    before: (w) => { device(w); w.localStorage.setItem('lsmDemoTourSeen', '1'); w.DemoTrack = (t, f) => tracked.push([t, f.target]); } });
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
  assert.equal(steps.length, 6);
  const clicked = [];
  w.document.querySelectorAll('#tabbar .tab').forEach((t) => t.addEventListener('click', () => clicked.push(t.dataset.tab)));
  b.querySelector('.demo-banner-tour').click();
  for (let i = 1; i < steps.length; i++) w.document.querySelector('.demo-tour-next').click();
  assert.deepEqual(clicked, [...steps.filter((s) => s.screen).map((s) => s.screen)]);
  assert.ok(steps[steps.length - 1].install);
  assert.equal(w.document.querySelector('.demo-tour-title').textContent, 'Install it');
  assert.ok(w.document.querySelector('.demo-tour-card .demo-install-cmd'));
  assert.equal(w.document.querySelector('.demo-tour-card [data-demo-track="copy-phone-tour"]').textContent, 'Copy');
  w.document.querySelector('.demo-tour-next').click();
  assert.equal(w.document.querySelector('.demo-tour'), null);
});

test('phone tour steps match the real phone app page', () => {
  const w = new JSDOM(COMPANION_HTML, { url: 'https://demo.example/phone' }).window;
  const tour = new JSDOM('<body></body>', { url: 'https://demo.example/dashboard', runScripts: 'outside-only' }).window;
  tour.localStorage.setItem('lsmDemoTourSeen', '1');
  tour.eval(src('demo-tour.js'));
  for (const s of tour.DemoTour.phoneSteps.filter((s) => !s.install)) {
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

test('the Ask Tower step runs the tour questions once per entry', async () => {
  const calls = [], runs = [];
  const w = await dom(['demo-tour.js'], { before: (w) => {
    w.localStorage.setItem('lsmDemoTourSeen', '1');
    towerStubs(w, calls);
    w.DemoTourAsk = { run: (surface, isCurrent) => { runs.push({ surface, isCurrent }); return Promise.resolve(0); } };
  } });
  const i = w.DemoTour.steps.findIndex((s) => s.ask);
  w.DemoTour.start(i - 1);
  assert.equal(runs.length, 0);
  w.DemoTour.next();
  assert.equal(runs.length, 1);
  assert.equal(runs[0].surface, 'desk');
  assert.equal(runs[0].isCurrent(), true);
  w.DemoTour.next();
  assert.equal(runs[0].isCurrent(), false);
  assert.equal(runs.length, 1);
  w.DemoTour.back();
  assert.equal(runs.length, 2);
  assert.equal(runs[1].isCurrent(), true);
  w.DemoTour.close();
  assert.equal(runs[1].isCurrent(), false);
});

// Starts the tour on the benchmark step with toolsOpenTool and DemoTrack stubs.
async function benchStep() {
  const opened = [], tracked = [];
  const w = await dom(['demo-tour.js'], { before: (w) => {
    w.localStorage.setItem('lsmDemoTourSeen', '1');
    w.toolsOpenTool = (id) => opened.push(id);
    w.DemoTrack = (t, f) => tracked.push([t, f.target]);
  } });
  const i = w.DemoTour.steps.findIndex((s) => s.tool === 'benchmark');
  w.DemoTour.start(i);
  tracked.length = 0;
  const status = (t) => { w.document.getElementById('blStatus').textContent = t; return new Promise((r) => setTimeout(r, 0)); };
  return { w, opened, tracked, status };
}

test('the benchmark step opens the benchmark tool and rings Run', async () => {
  const { w, opened } = await benchStep();
  assert.deepEqual(opened, ['benchmark']);
  const run = w.document.getElementById('blRunBtn');
  run.getBoundingClientRect = () => ({ top: 200, bottom: 232, left: 50, right: 250, width: 200, height: 32 });
  w.document.getElementById('llm-tools').getBoundingClientRect = () => ({ top: 20, bottom: 700, left: 10, right: 420, width: 410, height: 680 });
  w.DemoTour.place();
  const ring = w.document.querySelector('.demo-tour-ring');
  assert.equal(ring.style.display, '');
  assert.equal(ring.style.top, '196px');
  assert.equal(ring.style.height, '40px');
  assert.equal(ring.style.width, '208px');
  assert.ok(w.DemoTour.steps.filter((s) => s.exact).length === 1, 'only the benchmark step skips the climb');
  w.DemoTour.close();
});

test('bench-done is recorded once when the run completes on the benchmark step', async () => {
  const { w, tracked, status } = await benchStep();
  await status('running');
  assert.deepEqual(tracked, []);
  await status('complete');
  await status('running');
  await status('complete');
  assert.deepEqual(tracked, [['click', 'bench-done']]);
  w.DemoTour.close();
});

test('bench-done still records when #blStatus appears after the benchmark step opens', async () => {
  const opened = [], tracked = [];
  const w = await dom(['demo-tour.js'], { before: (w) => {
    w.localStorage.setItem('lsmDemoTourSeen', '1');
    w.document.getElementById('blStatus').remove();
    w.DemoTrack = (t, f) => tracked.push([t, f.target]);
  } });
  w.DemoTour.start(w.DemoTour.steps.findIndex((s) => s.tool === 'benchmark'));
  await new Promise((r) => setTimeout(r, 600));
  const st = w.document.createElement('span');
  st.id = 'blStatus';
  st.textContent = 'idle';
  w.document.getElementById('llm-tools').appendChild(st);
  await new Promise((r) => setTimeout(r, 400));
  st.textContent = 'running';
  await new Promise((r) => setTimeout(r, 0));
  st.textContent = 'complete';
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(tracked.filter((e) => e[0] === 'click'), [['click', 'bench-done']]);
  w.DemoTour.close();
});

test('the welcome card waits for the dashboard to paint', async () => {
  const w = await dom(['demo-tour.js'], { painted: false, wait: 300 });
  assert.equal(w.document.querySelector('.demo-welcome'), null);
  paint(w);
  await new Promise((r) => setTimeout(r, 150));
  assert.ok(w.document.querySelector('.demo-welcome-card'));
});

test('the welcome card shows after 3 s even if the dashboard never paints', async () => {
  const w = await dom(['demo-tour.js'], { painted: false, wait: 2500 });
  assert.equal(w.document.querySelector('.demo-welcome'), null);
  await new Promise((r) => setTimeout(r, 800));
  assert.ok(w.document.querySelector('.demo-welcome-card'));
});

test('the banner Share says Link copied when there is no share sheet', async () => {
  const copied = [];
  const w = await dom(['demo-tour.js'], { before: (w) => {
    w.localStorage.setItem('lsmDemoTourSeen', '1');
    Object.defineProperty(w.navigator, 'clipboard', { configurable: true, value: { writeText: (t) => { copied.push(t); return Promise.resolve(); } } });
  } });
  assert.equal(typeof w.navigator.share, 'undefined');
  const b = w.document.getElementById('demoBanner');
  const note = b.querySelector('.demo-banner-note');
  assert.equal(note.getAttribute('aria-live'), 'polite');
  assert.equal(note.previousElementSibling, b.querySelector('.demo-install-share'));
  b.querySelector('.demo-install-share').click();
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(note.textContent, 'Link copied');
  assert.deepEqual(copied, ['https://get.llmsysmanager.com/?ref=demo-share']);
});

test('a repeat banner Share click restarts the clear timer', async () => {
  const w = await dom(['demo-tour.js'], { before: (w) => {
    w.localStorage.setItem('lsmDemoTourSeen', '1');
    Object.defineProperty(w.navigator, 'clipboard', { configurable: true, value: { writeText: () => Promise.resolve() } });
  } });
  const b = w.document.getElementById('demoBanner');
  const note = b.querySelector('.demo-banner-note');
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  b.querySelector('.demo-install-share').click();
  await sleep(2000);
  b.querySelector('.demo-install-share').click();
  await sleep(1000);
  assert.equal(note.textContent, 'Link copied');
});

test('leaving the benchmark step mid-run is quiet', async () => {
  const { w, tracked, status } = await benchStep();
  await status('running');
  w.DemoTour.next();
  tracked.length = 0;
  await status('complete');
  assert.deepEqual(tracked, []);
  w.DemoTour.close();
});

// Runs the built dashboard's head script against a stub window and returns the replace calls.
async function dashboardRedirect({ ua, width, search = '', desktop = false }) {
  const { mkdtemp, readFile, rm } = await import('node:fs/promises');
  const { tmpdir } = await import('node:os');
  const { build } = await import('../build.mjs');
  const out = await mkdtemp(join(tmpdir(), 'dist-'));
  try {
    await build({ outDir: out, release: 'v9.9.9' });
    const html = await readFile(join(out, 'dashboard.html'), 'utf8');
    const head = html.slice(0, html.indexOf('demo-analytics.js'));
    const inline = [...head.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]).find((c) => c.includes('wantsPhone'));
    const replaced = [];
    const store = desktop ? { lsmDemoDesktop: '1' } : {};
    const self = load(['demo-device.js'], {
      innerWidth: width, navigator: { userAgent: ua }, matchMedia: () => ({ matches: false }),
      sessionStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; } },
      location: { search, hash: '', replace: (u) => replaced.push(u) },
    });
    const vm = await import('node:vm');
    vm.runInContext(inline, self);
    return { replaced, inline: Boolean(inline) };
  } finally { await rm(out, { recursive: true, force: true }); }
}

test('phones go from /dashboard to /phone keeping the query', async () => {
  const r = await dashboardRedirect({ ua: IPHONE_UA, width: 390, search: '?ref=hn' });
  assert.ok(r.inline);
  assert.deepEqual(r.replaced, ['/phone?ref=hn']);
});

test('desktop choice survives the loading page', async () => {
  const r = await dashboardRedirect({ ua: IPHONE_UA, width: 390, desktop: true });
  assert.deepEqual(r.replaced, []);
  const store = { lsmDemoDesktop: '1' };
  const sessionStorage = { getItem: (k) => (k in store ? store[k] : null), removeItem: (k) => { delete store[k]; } };
  const dev = load(['demo-device.js'], { innerWidth: 390, navigator: { userAgent: IPHONE_UA }, matchMedia: () => ({ matches: true }), sessionStorage });
  assert.deepEqual(runBootstrap(IPHONE_UA, { controller: {}, window: { sessionStorage, DemoDevice: dev.DemoDevice } }).replaced, ['/dashboard']);
});

test('the loading page sends phones to /phone', () => {
  const DESK_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36';
  const mk = (ua, width) => load(['demo-device.js'], { innerWidth: width, navigator: { userAgent: ua }, matchMedia: () => ({ matches: false }), sessionStorage: { getItem: () => null } }).DemoDevice;
  assert.deepEqual(runBootstrap(IPHONE_UA, { controller: {}, window: { DemoDevice: mk(IPHONE_UA, 390) } }).replaced, ['/phone']);
  assert.deepEqual(runBootstrap(DESK_UA, { controller: {}, window: { DemoDevice: mk(DESK_UA, 1600) } }).replaced, ['/dashboard']);
  const store = { lsmDemoReturn: '/phone' };
  const sessionStorage = { getItem: (k) => (k in store ? store[k] : null), removeItem: (k) => { delete store[k]; } };
  assert.deepEqual(runBootstrap(DESK_UA, { controller: {}, window: { sessionStorage, DemoDevice: mk(DESK_UA, 1600) } }).replaced, ['/phone']);
});

test('Desktop version sets the desktop choice; Phone version clears it', async () => {
  const phone = await dom(['demo-tour.js'], { url: 'https://demo.example/phone', before: (w) => w.localStorage.setItem('lsmDemoTourSeen', '1') });
  phone.document.querySelector('.demo-banner-switch').dispatchEvent(new phone.MouseEvent('click', { bubbles: true, cancelable: true }));
  assert.equal(phone.sessionStorage.getItem('lsmDemoDesktop'), '1');
  const dash = await dom(['demo-tour.js'], { before: (w) => { w.localStorage.setItem('lsmDemoTourSeen', '1'); w.sessionStorage.setItem('lsmDemoDesktop', '1'); } });
  dash.document.querySelector('.demo-banner-switch').dispatchEvent(new dash.MouseEvent('click', { bubbles: true, cancelable: true }));
  assert.equal(dash.sessionStorage.getItem('lsmDemoDesktop'), null);
});
