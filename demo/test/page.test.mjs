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
<div id="towerOverlay"><div id="twBox"><textarea id="twInput"></textarea></div></div>
<div id="adminTab"><div id="adminHealthCard"></div>
  <button class="sub-tab-btn" id="subTabBtnAdminAgents">Agents</button><button class="sub-tab-btn" id="subTabBtnAdminRouting">Gateway</button>
  <button class="sub-tab-btn" id="subTabBtnAdminJobs">Jobs</button>
  <div id="admin-agents"></div><div id="admin-routing"></div><div id="admin-jobs"></div></div>
</body></html>`;

// Builds a jsdom dashboard, runs the given runtime scripts in it and waits for the tour's start delay.
async function dom(scripts, { before, after } = {}) {
  const d = new JSDOM(DASH_HTML, { url: 'https://demo.example/dashboard', runScripts: 'outside-only' });
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
  assert.ok(steps.some((s) => s.selector === '#twBox' && s.tower && /slow|alarm|cost|model/i.test(s.text)));
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
  assert.match(banner.textContent, /^Demo with sample data\. Install it on your own machines →Tour$/);
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
  assert.equal(a.textContent, 'Install');
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
function runBootstrap(userAgent) {
  const html = src('bootstrap.html');
  const code = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));
  const registered = [], replaced = [];
  const serviceWorker = { controller: null, addEventListener() {}, register: (u) => { registered.push(u); return new Promise(() => {}); } };
  const document = { getElementById: () => ({ style: {} }) };
  new Function('navigator', 'location', 'document', 'window', 'setTimeout', code)(
    { userAgent, serviceWorker }, { replace: (u) => replaced.push(u) }, document, {}, () => 0);
  return { registered, replaced };
}

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
