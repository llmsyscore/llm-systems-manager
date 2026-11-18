// #1432: the manager shell — sidebar items, toolbar title, Sidebar/Theme/Live switches — on the real index.html body.
// Loads kit.js, settingsdrawer.js, foundation.js, boot.js and shell.js with fetch stubbed.
import { describe, test, expect, afterEach } from 'vitest';
import { JSDOM } from 'jsdom';
import { Script, createContext } from 'node:vm';
import { srcFile, blockSrc, fnSrc } from './helpers/harness.js';

const indexSrc = srcFile('index.html');
// The body markup with its script elements removed through a parsed DOM.
const bodyDoc = new JSDOM(indexSrc).window.document;
bodyDoc.querySelectorAll('body script').forEach(s => s.remove());
const bodyHtml = bodyDoc.body.innerHTML;
const SCRIPTS = ['kit/kit.js', 'js/lib/settingsdrawer.js', 'js/foundation.js', 'js/boot.js', 'js/shell.js'].map(srcFile);
const chartsSrc = srcFile('js/charts.js');
const shellCss = srcFile('css/shell.css');

// Boot-time functions that live in scripts this harness does not load.
const STUBS = ['loadBenchmarkData', 'renderModelCards', 'initSortable', 'syncInterval', 'loadHistory',
  'loadManagerPerfHistory', 'checkConfig', 'pollServerState', 'fetchMetrics', 'startFetching',
  'fetchLMStudioMetrics', 'fetchVllmMetrics', 'fetchServicesAndInflux', 'fetchManagerAgentsCard', 'fetchManagerStreamsCard',
  'refreshTabIndicators', 'refreshAlarmRules', 'fetchOverallMetrics', 'refreshLLMTab', 'loadHFTrending', 'startPerfRefresh',
  '_initLLMSections', 'startLogStream', 'adminLoadAgents', 'adminLoadHealth', 'adminAuthLoad', 'adminStartAutoRefresh',
  'adminStopAutoRefresh', 'stopLogStream', 'stopPerfRefresh', 'stopLmsLogRefresh', 'fetchOpenclawAnalytics',
  'initEnergyTab', 'forecastLoad', '_initLMSSections'];
const stubSrc = STUBS.map(n => `window.${n} = function () { return Promise.resolve(); };`).join('\n')
  + '\nwindow.fetchInterval = 5000; window._logPanelOpen = false; window._lmsLogOpen = false;';

const _windows = [];
afterEach(() => { while (_windows.length) _windows.pop().close(); });

// Waits up to `max` macrotask ticks for `done()` to hold.
async function waitFor(w, done, max = 50) {
  for (let i = 0; i < max && !done(); i++) await new Promise(r => w.setTimeout(r, 0));
}

async function boot(saved = { theme: 'dark' }, before = null) {
  const dom = new JSDOM(`<!doctype html><html><head></head><body>${bodyHtml}</body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/', pretendToBeVisual: true });
  const w = dom.window;
  _windows.push(w);
  w.__posts = [];
  w.fetch = async (url, opts) => {
    if (opts && opts.method === 'POST') {
      if (url === '/api/layout') w.__posts.push(JSON.parse(opts.body));
      return { ok: true, json: async () => ({}) };
    }
    if (url === '/api/layout') return { ok: true, json: async () => JSON.parse(JSON.stringify(saved)) };
    if (url === '/api/me') return { ok: true, json: async () => ({ role: 'admin', admin_access: true, authenticated: false }) };
    return { ok: true, json: async () => ({}) };
  };
  const inject = (code) => { const s = w.document.createElement('script'); s.textContent = code; w.document.head.appendChild(s); };
  if (before) before(w);
  inject(stubSrc);
  SCRIPTS.forEach(inject);
  await w.mcLayoutReady;
  await waitFor(w, () => w.document.getElementById('overallTab').style.display === '');
  expect(w.document.getElementById('overallTab').style.display, 'boot reached switchTab').toBe('');
  w.__posts.length = 0;
  return w;
}

const MOVED = {
  side: ['tabBtnOverall', 'tabBtnLlmControl', 'tabBtnTools', 'tabBtnEvents', 'tabBtnAdmin',
    'subTabBtnDashAll', 'subTabBtnDashLlamacpp', 'subTabBtnDashLmstudio', 'subTabBtnDashVllm', 'subTabBtnDashEnergy', 'subTabBtnOpenclaw',
    'subTabBtnDashManager', 'subTabBtnDashForecast', 'towerNav', 'towerNavBadge', 'tabDotEvents', 'tabDotAdmin'],
  toolbar: ['serverStateBanner', 'lmsStateBanner', 'vllmStateBanner', 'towerBtn', 'towerBadge', 'intervalBadge', 'settingsCog', 'backToCompanion'],
};

describe('manager shell', () => {
  test('moved ids exist once', () => {
    const doc = new JSDOM(indexSrc, { url: 'http://localhost/' }).window.document;
    for (const id of [...MOVED.side, ...MOVED.toolbar]) {
      expect(doc.querySelectorAll(`[id="${id}"]`).length, id).toBe(1);
    }
    for (const id of MOVED.side) expect(doc.getElementById(id).closest('#side'), id).toBeTruthy();
    for (const id of MOVED.toolbar) expect(doc.getElementById(id).closest('#toolbar'), id).toBeTruthy();
  });

  test('no old header', () => {
    const doc = new JSDOM(indexSrc, { url: 'http://localhost/' }).window.document;
    expect(doc.querySelector('.tab-nav')).toBeNull();
    expect(doc.querySelector('.header-divider')).toBeNull();
    // Tools and Admin keep their in-page bars; they sit inside dashboardTab in the HTML until boot.js hoists them.
    const dashBars = [...doc.querySelectorAll('#dashboardTab .sub-tab-nav')].filter(n => !n.closest('#toolsTab,#adminTab'));
    expect(dashBars.length).toBe(0);
  });

  test('switchTab marks and titles', async () => {
    const w = await boot();
    const d = w.document;
    w.switchTab('llm');
    const llm = d.getElementById('tabBtnLlmControl');
    expect(llm.classList.contains('on')).toBe(true);
    expect(llm.getAttribute('aria-current')).toBe('page');
    expect(d.getElementById('pageTitle').textContent).toBe('LLM Control');
    expect(d.title).toBe('LLM Control — LLM Systems Manager');
    w.switchTab('dashboard');
    w.switchSubTab('dashboard', 'energy');
    const on = [...d.querySelectorAll('#side [data-tab].on')];
    expect(on.map(b => b.id)).toEqual(['subTabBtnDashEnergy']);
    expect(d.querySelectorAll('#side [aria-current="page"]').length).toBe(1);
    expect(d.getElementById('pageTitle').textContent).toBe('Dashboards · Energy');
  });

  test('the Host page marks All hosts in the sidebar', async () => {
    const w = await boot();
    const d = w.document;
    w.switchTab('dashboard');
    w.switchSubTab('dashboard', 'host');
    const on = [...d.querySelectorAll('#side [data-tab].on')].map(b => b.id);
    expect(on).toEqual(['subTabBtnDashAll']);
    expect(d.getElementById('subTabBtnDashAll').getAttribute('aria-current')).toBe('page');
    expect(d.getElementById('pageTitle').textContent).toBe('Dashboards · Host');
    w.switchSubTab('dashboard', 'lmstudio');
    expect([...d.querySelectorAll('#side [data-tab].on')].map(b => b.id)).toEqual(['subTabBtnDashLmstudio']);
  });

  test('full-size panels on every page redraw their charts to fit', async () => {
    const w = await boot();
    expect(w.KIT_CONF.fullRedraw).toBe(true);
    expect(w.KIT_CONF.prefix).toBe('mgr');
  });

  test('opening a Dashboards page from Overall or Admin enters only that page, never the one shown last', async () => {
    const w = await boot();
    const d = w.document;
    const entered = [];
    w.hpEnter = (page) => { entered.push(page); };
    w.hpLeave = () => {};
    w.switchTab('dashboard');
    w.switchSubTab('dashboard', 'llamacpp');
    w.switchTab('overall');
    entered.length = 0;
    d.getElementById('subTabBtnDashLmstudio').click();
    expect(entered).toEqual(['lms', 'lms']);
    w.switchTab('overall');
    entered.length = 0;
    const admin = srcFile('js/admin.js');
    const inject = (code) => { const s = d.createElement('script'); s.textContent = code; d.head.appendChild(s); };
    inject("var _adminProviders = [{ name: 'vllm', sub_tab: 'vllm' }];\n" + fnSrc(admin, '_jumpToDashboard'));
    w._jumpToDashboard('agent-1', 'vllm');
    expect(entered).toEqual(['vllm', 'vllm']);
    expect(d.getElementById('dash-vllm').classList.contains('active')).toBe(true);
  });

  test('sidebar click switches', async () => {
    const w = await boot();
    const d = w.document;
    d.getElementById('subTabBtnDashManager').click();
    expect(d.getElementById('dashboardTab').style.display).toBe('');
    expect(d.getElementById('dash-manager').classList.contains('active')).toBe(true);
    expect(d.getElementById('pageTitle').textContent).toBe('Dashboards · Manager');
  });

  test('hidden active sidebar item falls back', async () => {
    const w = await boot();
    const d = w.document;
    w.switchTab('llm');
    const calls = [];
    const real = w.switchTab;
    w.switchTab = (t) => { calls.push(t); return real(t); };
    d.getElementById('tabBtnLlmControl').style.display = 'none';
    w.eval(blockSrc(chartsSrc, '    const activeBtn = ', "switchTab('dashboard');"));
    expect(calls).toEqual(['dashboard']);
    expect(d.getElementById('dashboardTab').style.display).toBe('');
  });

  test('hidden active sub item falls back to Manager', async () => {
    const w = await boot();
    const d = w.document;
    d.getElementById('subTabBtnDashVllm').style.display = '';
    d.getElementById('subTabBtnDashVllm').click();
    expect(d.getElementById('subTabBtnDashVllm').classList.contains('on')).toBe(true);
    // checkConfig's real toggle and fallback block, with vLLM gone from the agents.
    const body = blockSrc(chartsSrc, '    const toggle = (id, on) => {', "switchSubTab('dashboard','manager');");
    w.__cfg = { agents: { llama_present: true, lms_present: true, vllm_present: false } };
    const calls = [];
    const real = w.switchTab;
    w.switchTab = (t) => { calls.push(t); return real(t); };
    w.eval(`(function(px, cfg) {\n${body}\n})({}, window.__cfg);`);
    expect(calls).toEqual([]);
    expect(d.getElementById('subTabBtnDashVllm').style.display).toBe('none');
    expect(d.getElementById('dashboardTab').style.display).toBe('');
    expect(d.getElementById('dash-manager').classList.contains('active')).toBe(true);
    expect([...d.querySelectorAll('#side [data-tab].on')].map(b => b.id)).toEqual(['subTabBtnDashManager']);
    expect(d.getElementById('pageTitle').textContent).toBe('Dashboards · Manager');
  });

  test('pre-paint script collapses the sidebar before shell.js runs', () => {
    const region = blockSrc(indexSrc, '<!-- kit shell -->', '<!-- /kit shell -->');
    const dom = new JSDOM(`<!doctype html><html><body>${region}</div></div></body></html>`, {
      runScripts: 'dangerously', url: 'http://localhost/',
      beforeParse(win) { win.localStorage.setItem('mgr.nav', 'collapsed'); },
    });
    _windows.push(dom.window);
    expect(dom.window.mcShellInit).toBeUndefined();
    expect(dom.window.document.getElementById('side').classList.contains('collapsed')).toBe(true);
  });

  test('Tower button sits in the toolbar right group before Live; the sidebar keeps a Tower item', () => {
    const doc = new JSDOM(indexSrc, { url: 'http://localhost/' }).window.document;
    const btn = doc.getElementById('towerBtn');
    expect(btn.parentElement.classList.contains('tb-right')).toBe(true);
    expect(btn.nextElementSibling.id).toBe('liveBtn');
    expect(btn.className).toBe('tb-tower');
    expect(btn.hidden).toBe(true);
    expect(btn.getAttribute('onclick')).toBe('towerToggle()');
    expect(btn.getAttribute('title')).toBe('Tower (Alt+T)');
    expect(btn.getAttribute('aria-haspopup')).toBe('dialog');
    expect(btn.querySelector('svg')).toBeTruthy();
    expect(btn.querySelector('.tb-tower-lab').textContent).toBe('Tower');
    expect(btn.querySelector('#towerBadge.n').hidden).toBe(true);
    expect(btn.querySelector('#towerBadge').hasAttribute('aria-hidden')).toBe(false);
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    const nav = doc.getElementById('towerNav');
    expect(nav.classList.contains('nav')).toBe(true);
    expect(nav.hidden).toBe(true);
    expect(nav.getAttribute('onclick')).toBe('towerToggle()');
    expect(nav.querySelector('svg')).toBeTruthy();
    expect(nav.querySelector('.side-lab').textContent).toBe('Tower');
    expect(nav.querySelector('#towerNavBadge.n').hidden).toBe(true);
    expect(nav.querySelector('#towerNavBadge').getAttribute('aria-hidden')).toBe('true');
    expect(nav.getAttribute('aria-expanded')).toBe('false');
    const flat = shellCss.replace(/\s+/g, '');
    expect(flat).toContain('@media(max-width:600px){.tb-tower{padding:08px;}.tb-tower.tb-tower-lab{display:none;}}');
  });

  test('phone-width Tower item click closes the overlay', async () => {
    const w = await boot(undefined, (win) => { win.towerToggle = () => { win.__toggled = (win.__toggled || 0) + 1; }; });
    const d = w.document;
    w.innerWidth = 420;
    d.getElementById('navOpen').click();
    d.getElementById('towerNav').hidden = false;
    d.getElementById('towerNav').click();
    expect(w.__toggled).toBe(1);
    expect(d.getElementById('side').classList.contains('open')).toBe(false);
    expect(d.activeElement.id).toBe('navOpen');
  });

  test('phone-width item click closes the overlay and focuses the menu button', async () => {
    const w = await boot();
    const d = w.document;
    w.innerWidth = 420;
    d.getElementById('navOpen').click();
    expect(d.getElementById('side').classList.contains('open')).toBe(true);
    d.getElementById('tabBtnEvents').click();
    expect(d.getElementById('side').classList.contains('open')).toBe(false);
    expect(d.getElementById('navOpen').getAttribute('aria-expanded')).toBe('false');
    expect(d.activeElement.id).toBe('navOpen');
  });

  test('widening to 900px closes the overlay', async () => {
    const mqs = [];
    const w = await boot(undefined, (win) => {
      win.matchMedia = (q) => {
        const mq = { media: q, matches: false, _l: [], addEventListener(t, fn) { this._l.push(fn); }, removeEventListener() {} };
        mqs.push(mq);
        return mq;
      };
    });
    const d = w.document;
    d.getElementById('navOpen').click();
    expect(d.getElementById('side').classList.contains('open')).toBe(true);
    const wide = mqs.find(m => m.media === '(min-width: 900px)');
    expect(wide && wide._l.length).toBe(1);
    wide._l[0]({ matches: true });
    expect(d.getElementById('side').classList.contains('open')).toBe(false);
    expect(d.getElementById('navOpen').getAttribute('aria-expanded')).toBe('false');
  });

  test('pin saves to layout', async () => {
    const w = await boot();
    const d = w.document;
    d.getElementById('navPin').click();
    await waitFor(w, () => w.__posts.length >= 1);
    expect(d.getElementById('side').classList.contains('collapsed')).toBe(true);
    expect(d.getElementById('navPinT').textContent).toBe('Sidebar: Icons only');
    expect(d.getElementById('navPin').getAttribute('aria-checked')).toBe('false');
    expect(w.eval('layout.kit')['mgr.nav']).toBe('collapsed');
    expect(w.localStorage.getItem('mgr.nav')).toBe('collapsed');
    await waitFor(w, () => false, 5);
    expect(w.__posts.length).toBe(1);
  });

  test('saved sidebar state wins over the pre-paint copy', async () => {
    const w = await boot({ theme: 'dark', kit: { 'mgr.nav': 'collapsed' } });
    expect(w.document.getElementById('side').classList.contains('collapsed')).toBe(true);
    expect(w.document.getElementById('navPinT').textContent).toBe('Sidebar: Icons only');
    expect(w.localStorage.getItem('mgr.nav')).toBe('collapsed');
  });

  test('theme switch', async () => {
    const w = await boot();
    const d = w.document;
    expect(d.getElementById('themeT').textContent).toBe('Theme: Dark');
    d.getElementById('themeBtn').click();
    await waitFor(w, () => w.__posts.length >= 1);
    expect(d.documentElement.getAttribute('data-theme')).toBe('light');
    expect(d.getElementById('themeT').textContent).toBe('Theme: Light');
    expect(d.getElementById('themeBtn').getAttribute('aria-checked')).toBe('true');
    expect(w.__posts[w.__posts.length - 1].theme).toBe('light');

    const f = await boot({ theme: 'dark', themeFollowSystem: true });
    f.document.getElementById('themeBtn').click();
    await waitFor(f, () => f.__posts.length >= 1);
    expect(f.eval('layout.themeFollowSystem')).toBe(false);
    expect(f.__posts[f.__posts.length - 1].themeFollowSystem).toBe(false);
  });

  test('live switch', async () => {
    const w = await boot();
    const d = w.document;
    expect(d.getElementById('liveT').textContent).toBe('Live: On');
    d.getElementById('liveBtn').click();
    expect(w.LivePause.on).toBe(true);
    expect(d.getElementById('liveT').textContent).toBe('Live: Off');
    expect(d.getElementById('liveBtn').getAttribute('aria-checked')).toBe('false');
    d.getElementById('liveBtn').click();
    expect(w.LivePause.on).toBe(false);
    expect(d.getElementById('liveT').textContent).toBe('Live: On');
  });

  test('fixed tabs follow the sidebar width', () => {
    const doc = new JSDOM(indexSrc, { url: 'http://localhost/' }).window.document;
    for (const id of ['eventsTab', 'toolsTab', 'adminTab']) {
      const st = doc.getElementById(id).getAttribute('style');
      expect(st, id).toContain('var(--mc-top');
      expect(st, id).toContain('var(--mc-side');
      expect(doc.getElementById(id).closest('.shell'), id).toBeTruthy();
    }
    const flat = shellCss.replace(/\s+/g, '');
    expect(flat).toContain('.shell{--mc-side:210px;}');
    expect(flat).toContain('@media(min-width:900px){.shell:has(.side.collapsed){--mc-side:56px;}}');
    expect(flat).toContain('@media(max-width:899px){.shell{--mc-side:0px;}}');
  });

  test('Tower lock reaches the root and in-page jumps clear the toolbar', () => {
    const flat = shellCss.replace(/\s+/g, '');
    expect(flat).toContain('html:has(body.tw-lock){overflow:hidden;}');
    expect(flat).toContain('html:has(body.tw-lock)body.tw-lock{overflow:visible;}');
    expect(flat).toContain('html{scroll-padding-top:var(--mc-top,61px);}');
  });

  test('toolbar group shows only on Overall', async () => {
    const w = await boot();
    const d = w.document;
    const bar = d.querySelector('#toolbar .tb-ov');
    expect(new JSDOM(indexSrc).window.document.querySelector('#toolbar .tb-ov').hidden, 'hidden in the markup').toBe(true);
    expect(bar.hidden).toBe(false);
    expect([...bar.querySelectorAll('#ovProv button')].map(b => [b.dataset.ovProv, b.textContent, b.getAttribute('aria-pressed')]))
      .toEqual([['all', 'All providers', 'true'], ['llama', 'llama.cpp', 'false'], ['lms', 'LM Studio', 'false'], ['vllm', 'vLLM', 'false']]);
    expect([...bar.querySelectorAll('#ovRange button')].map(b => [b.dataset.ovRange, b.getAttribute('aria-pressed')]))
      .toEqual([['6', 'false'], ['12', 'false'], ['24', 'true']]);
    expect(bar.querySelectorAll('input').length).toBe(0);
    w.mcShellMark('llm');
    expect(bar.hidden).toBe(true);
    w.mcShellMark('overall');
    expect(bar.hidden).toBe(false);
    w.switchTab('events');
    expect(bar.hidden).toBe(true);
    w.switchTab('overall');
    expect(bar.hidden).toBe(false);
  });

  test('All hosts range group shows on Dashboards · All hosts and the host pages', async () => {
    const w = await boot({ theme: 'dark', kit: { 'mgr.ahRange': '12' } });
    const d = w.document;
    const bar = d.querySelector('#toolbar .tb-ah');
    const pressed = () => [...bar.querySelectorAll('[data-ah-range]')].map(b => [b.dataset.ahRange, b.getAttribute('aria-pressed')]);
    expect(new JSDOM(indexSrc).window.document.querySelector('#toolbar .tb-ah').hidden, 'hidden in the markup').toBe(true);
    expect(bar.hidden).toBe(true);
    expect(w.ahState.range).toBe(12);
    expect(pressed()).toEqual([['6', 'false'], ['12', 'true'], ['24', 'false']]);
    d.getElementById('subTabBtnDashAll').click();
    expect(bar.hidden).toBe(false);
    expect(d.getElementById('dash-all').classList.contains('active')).toBe(true);
    expect(d.getElementById('pageTitle').textContent).toBe('Dashboards · All hosts');
    const got = [];
    w.ahSetRange = (n) => { got.push(n); };
    w.hpRender = () => { got.push('render'); };
    bar.querySelector('[data-ah-range="6"]').click();
    expect(got).toEqual([6, 'render']);
    expect(bar.querySelectorAll('input').length).toBe(0);
    for (const sub of ['llamacpp', 'lmstudio', 'vllm', 'host']) {
      w.switchSubTab('dashboard', sub);
      expect(bar.hidden, sub).toBe(false);
    }
    expect(d.getElementById('pageTitle').textContent).toBe('Dashboards · Host');
    d.getElementById('subTabBtnDashEnergy').click();
    expect(bar.hidden).toBe(true);
    w.switchTab('dashboard');
    w.switchSubTab('dashboard', 'all');
    expect(bar.hidden).toBe(false);
    w.switchTab('overall');
    expect(bar.hidden).toBe(true);
  });

  test('no user-visible "fleet" in the dashboard and companion markup', () => {
    for (const file of ['index.html', 'companion.html']) {
      const doc = new JSDOM(srcFile(file)).window.document;
      const walker = doc.createTreeWalker(doc.body, 4);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (n.parentElement && n.parentElement.closest('script,style')) continue;
        expect(n.textContent, file + ' text').not.toMatch(/fleet/i);
      }
      for (const el of doc.querySelectorAll('[title],[data-title],[aria-label],[placeholder],[data-note]')) {
        for (const a of ['title', 'data-title', 'aria-label', 'placeholder', 'data-note']) {
          expect(el.getAttribute(a) || '', `${file} ${a}`).not.toMatch(/fleet/i);
        }
      }
    }
  });

  test('toasts start below the sticky toolbar', () => {
    const flat = srcFile('css/toasts.css').replace(/\s+/g, '');
    expect(flat).toContain('#alarmToastContainer{position:fixed;top:calc(var(--mc-top,61px)+12px);');
  });

  test('toolbar group wraps and takes its colours from kit tokens', () => {
    const flat = shellCss.replace(/\s+/g, '');
    expect(flat).toContain('.tb-ov{display:flex;flex-wrap:wrap;');
    expect(flat).toContain('.tb-ah{display:flex;flex-wrap:wrap;');
    expect(flat).toContain('@media(max-width:600px){#toolbar.tb-right{gap:6px;}#toolbar#liveT{display:none;}#toolbar.tb-ov{flex-basis:100%;}');
    expect(new JSDOM(indexSrc).window.document.getElementById('liveBtn').getAttribute('aria-label')).toBe('Live updates');
    expect(flat).toContain('.tb-chips{display:flex;flex-wrap:wrap;');
    const rules = [...shellCss.matchAll(/^[^{\n]*\.tb-(?:ov|chips?|seg)[^{]*\{([^}]*)\}/gm)].map(m => m[1]);
    expect(rules.length).toBeGreaterThan(5);
    for (const body of rules) {
      expect(body, body).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i);
      for (const m of body.matchAll(/var\((--[\w-]+)/g)) {
        expect(['--card', '--text', '--text-2', '--muted', '--border', '--accent', '--hover'], m[1]).toContain(m[1]);
      }
    }
  });

  test('kit co-load', () => {
    const srcs = [...indexSrc.matchAll(/<script src="\/static\/((?:kit|js)\/[^"?]+)/g)].map(m => m[1]);
    expect(srcs.indexOf('kit/kit.js')).toBeGreaterThan(-1);
    expect(srcs.indexOf('kit/kit.js')).toBeLessThan(srcs.indexOf('js/foundation.js'));
    expect(srcs[srcs.length - 1]).toBe('js/shell.js');
    let script;
    expect(() => { script = new Script(srcs.map(srcFile).join('\n;\n'), { filename: 'mgr-coload.js' }); }).not.toThrow();
    // Global function declarations exist before the first statement runs, so a throw on a missing DOM is fine.
    const ctx = createContext({});
    try { script.runInContext(ctx, { timeout: 5000 }); } catch (_) { /* no DOM here */ }
    for (const name of ['switchTab', 'tsHtml', 'kitTsAppend']) expect(typeof ctx[name], name).toBe('function');
  });
});
