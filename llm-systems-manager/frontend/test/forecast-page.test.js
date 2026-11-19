// #1432 phase 4d: Dashboards › Forecast on the panel kit, painted by js/forecast.js.
// Runs kit.js, overall-view, forecast (FC), overall.js and forecast.js on the real #dash-forecast markup with fetch stubbed.
import { describe, test, expect, afterEach, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import OV from '../js/lib/overall-view.js';
import FC from '../js/lib/forecast.js';
import SettingsLib from '../js/lib/settingsdrawer.js';
import { srcFile, fnSrc, evalGlobal, flush } from './helpers/harness.js';

const indexDoc = new JSDOM(srcFile('index.html')).window.document;
// #dashboardTab holding only #dash-forecast, to keep the DOM small.
const tabHtml = (() => {
  const tab = indexDoc.getElementById('dashboardTab').cloneNode(false);
  tab.appendChild(indexDoc.getElementById('dash-forecast').cloneNode(true));
  return tab.outerHTML;
})();
const forecast = srcFile('js/forecast.js');
const overall = srcFile('js/overall.js');
const foundation = srcFile('js/foundation.js');
evalGlobal(srcFile('kit/kit.js'));
window.OV = OV;
window.FC = FC;

const NOW_S = Math.floor(Date.now() / 1000);
const CHECKS = [
  { id: 'disk', title: 'Disk filling', state: 'ok', found: 1 },
  { id: 'load', title: 'Load change', state: 'collecting', have_days: 3, min_days: 7 },
  { id: 'fail', title: 'Model failures', state: 'failed' },
  { id: 'mem', title: 'Memory', state: 'ok', found: 0 },
];
const FINDINGS = [
  { id: 1, host: 'node-1', severity: 'critical', summary: 'Disk / fills in 3 days', title: 'Disk filling', check: 'disk' },
  { id: 2, host: 'node-2', severity: 'warning', summary: 'Load up 40%', title: 'Load change', check: 'load' },
];
const PAYLOAD = (over) => Object.assign({
  ok: true, enabled: true, mode: 'scheduled', run: null, running: false, last_result: '2 findings', digest: null,
  tower: { tier: 'full', reason: 'Full effort', model: 'qwen3-8b', digest_discarded: false },
  last_run: NOW_S - 3600, next_run: NOW_S + 7200, window_days: 14,
  checks: CHECKS, findings: FINDINGS,
  cleared: [{ id: 9, host: 'node-4', severity: 'info', summary: 'Swap use settled', title: 'Memory', state: 'cleared', resolved_at: NOW_S - 600 }],
}, over || {});
const TOWER_STATE = { enabled: true, model: 'qwen3-8b', hosts: ['node-1'], threads: 2, timers: 1 };
const TOWER_INSIGHTS = { ok: true, insights: [{ severity: 'warning', summary: 'Memory pressure on node-3', host: 'node-3', suggested_action: 'Restart the service' }] };

// Every URL fetched with its options; each answers a copy of routes[url] (a function is called), else {}.
function stubFetch(routes) {
  const calls = [];
  window.fetch = vi.fn(async (url, opts) => {
    calls.push([url, opts]);
    const v = typeof routes[url] === 'function' ? routes[url]() : routes[url];
    return { ok: true, json: async () => JSON.parse(JSON.stringify(v === undefined ? {} : v)) };
  });
  return calls;
}
const ROUTES = (payload) => ({
  '/api/forecast': payload || PAYLOAD(),
  '/api/tower/state': TOWER_STATE,
  '/api/tower/insights': TOWER_INSIGHTS,
});
function makeStore() {
  const m = {};
  return { getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null), setItem: (k, v) => { m[k] = String(v); } };
}
// A fresh #dash-forecast and fresh overall.js and forecast.js.
function load(me) {
  document.body.innerHTML = tabHtml;
  window._activeTab = 'dashboard';
  window._subTabState = { dashboard: 'forecast' };
  window.mcShellStore = makeStore();
  window.layout = {};
  window._agentsByProvider = { llama: [], lms: [], vllm: [] };
  window._pollCfg = {};
  window._activeAlerts = [];
  window._me = me || { role: 'operator' };
  localStorage.clear();
  evalGlobal(overall);
  evalGlobal(forecast);
  window.forecastSnapshot = window.forecastSnapshot || (() => null);
}
const $ = (id) => document.getElementById(id);
const text = (id) => $(id).textContent;
const noteOf = (card) => (document.querySelector(`[data-card="${card}"] .p-hd .p-note`) || { textContent: '' }).textContent;
async function enter() { await window.forecastLoad(); await flush(); await flush(); }
// Tile label → [value, sub] as painted in the Forecast tiles.
function tiles() {
  const out = {};
  document.querySelectorAll('#fc-b-tiles .fc-tile').forEach(t => {
    out[t.querySelector('.fc-tl').textContent] = [t.querySelector('.k-val').textContent, t.querySelector('.k-sub').textContent];
  });
  return out;
}
function dom(html) { const d = document.createElement('div'); d.innerHTML = html; return d.innerHTML; }
const urls = (calls) => calls.map(c => c[0]);

afterEach(() => { vi.restoreAllMocks(); });

describe('Forecast page (#1432)', () => {
  test('the page starts with the Forecast row, whose header holds the run bar, then 3 rows and 4 panels, the findings browser in the Findings panel', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    const root = $('fc-root');
    expect(root.getAttribute('data-kit-page')).toBe('forecast');
    expect(root.firstElementChild.getAttribute('data-row')).toBe('fc-run');
    const head = [...root.firstElementChild.children].map(c => c.id || c.className);
    expect(head.slice(0, 4)).toEqual(['grip row-grip', 'row-hd', 'fcRunbar', 'row-menu']);
    expect($('fcRunbar').className).toContain('fc-head');
    const rows = [...root.querySelectorAll('section.row[data-row]')].map(r => r.getAttribute('data-row'));
    expect(rows).toEqual(['fc-run', 'fc-find', 'fc-more']);
    const titles = [...root.querySelectorAll('section.row[data-row] > button[data-row-fold]')]
      .map(b => [...b.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim());
    expect(titles).toEqual(['Forecast', 'Findings', 'Checks and Tower']);
    const panels = [...root.querySelectorAll('section.panel[data-card]')].map(p => [p.getAttribute('data-card'), p.getAttribute('data-title')]);
    expect(panels).toEqual([['fc-tiles', 'Forecast'], ['fc-findings', 'Findings'], ['fc-checks', 'Checks'], ['fc-tower', 'Tower']]);
    const find = root.querySelector('[data-card="fc-findings"] .p-body');
    for (const id of ['fcBrief', 'fcTabs', 'fcHorizon', 'fcToolbar', 'fcSplit', 'fcRows', 'fcPager', 'fcDetail']) {
      expect(find.querySelector('#' + id), id).toBeTruthy();
    }
    expect(find.hasAttribute('data-kit-nosort')).toBe(true);
    expect(root.querySelector('[data-card="fc-checks"] #fcChecks')).toBeTruthy();
    expect(root.querySelector('[data-card="fc-checks"] details')).toBeNull();
    expect($('fcChecksBox')).toBeNull();
    const shown = root.textContent;
    for (const bad of ['NaN', 'undefined', 'Infinity']) expect(shown, bad).not.toContain(bad);
  });

  test('the tiles read the snapshot: open count, last and next run, checks ran, writing model, looking back', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    const now = Date.now(), t = tiles();
    expect(Object.keys(t)).toEqual(['Open findings', 'Last run', 'Next run', 'Checks ran', 'Writing model', 'Looking back']);
    expect(t['Open findings'][0]).toBe('2');
    expect(t['Last run'][0]).toBe(FC.whenLabel(NOW_S - 3600, now));
    expect(t['Next run'][0]).toBe(FC.whenLabel(NOW_S + 7200, now));
    expect(t['Checks ran']).toEqual(['2 / 4', '1 collecting · 1 could not run']);
    expect(t['Writing model']).toEqual(['qwen3-8b', 'Full effort']);
    expect(t['Looking back'][0]).toBe('14 days');
    expect(document.querySelector('#fc-b-tiles .fc-tile').classList.contains('bad')).toBe(true);
    const tw = [...document.querySelectorAll('#fc-b-tiles .fc-tile.fc-tw .fc-tl')].map(e => e.textContent);
    expect(tw).toEqual(['Last run', 'Next run']);
    const table = [...document.querySelectorAll('[data-card="fc-tiles"] .p-table tbody tr')].map(r => r.children[0].textContent);
    expect(table).toEqual(Object.keys(t));
  });

  test('no runs yet, no schedule, no Tower and only warnings', async () => {
    load();
    stubFetch(ROUTES(PAYLOAD({ last_run: null, next_run: null, tower: null, findings: [FINDINGS[1]] })));
    await enter();
    const t = tiles();
    expect(t['Last run'][0]).toBe('Never');
    expect(t['Next run'][0]).toBe('Not scheduled');
    expect(t['Writing model']).toEqual(['Off', 'Off']);
    const first = document.querySelector('#fc-b-tiles .fc-tile');
    expect(first.classList.contains('warn')).toBe(true);
    expect(first.classList.contains('bad')).toBe(false);
  });

  test('Forecast off: the tiles say Off, Findings shows the intro, Checks and Tower still paint, admins get Enable in Settings', async () => {
    load({ role: 'admin', admin_access: true });
    stubFetch(ROUTES(PAYLOAD({ enabled: false, findings: [], tower: null })));
    await enter();
    expect(Object.values(tiles()).map(v => v[0]))
      .toEqual(['Off', FC.whenLabel(NOW_S - 3600, Date.now()), 'Off', 'Off', 'Off', 'Off']);
    expect(text('fcRows')).toContain('Forecast looks at the last two weeks for trends');
    expect(document.querySelectorAll('#fcChecks .fc-cstate')).toHaveLength(4);
    expect(text('fc-b-tower')).toContain('Memory pressure on node-3');
    const btn = [...$('fcRunbar').querySelectorAll('button')].map(b => b.textContent);
    expect(btn).toEqual(['Enable in Settings']);
    load({ role: 'operator' });
    stubFetch(ROUTES(PAYLOAD({ enabled: false, last_run: null })));
    await enter();
    expect($('fcRunbar').querySelectorAll('button')).toHaveLength(0);
    expect(tiles()['Last run'][0]).toBe('Never');
  });

  test('a queued run shows Starting… with a progress bar of unknown length in the head', async () => {
    load();
    stubFetch(ROUTES(PAYLOAD({ run: { state: 'queued' }, running: true })));
    await enter();
    const bar = $('fcRunbar');
    expect(bar.classList.contains('fc-running')).toBe(true);
    expect(bar.querySelector('[role="status"]').textContent).toContain('Starting…');
    const track = bar.querySelector('[role="progressbar"]');
    expect(track.getAttribute('aria-label')).toBe('Forecast run progress');
    expect(track.classList.contains('fc-bar-wait')).toBe(true);
    expect(track.hasAttribute('aria-valuenow')).toBe(false);
    expect([...bar.querySelectorAll('button')].map(b => b.textContent)).toEqual(['Starting…']);
  });

  test('a running run shows its step, progress and elapsed time in the head', async () => {
    load();
    stubFetch(ROUTES(PAYLOAD({ run: { state: 'running', stage: 'checks', check: 'Load change', done: 2, total: 4, elapsed_s: 65 }, running: true })));
    await enter();
    const bar = $('fcRunbar');
    expect(bar.querySelector('[role="status"]').textContent).toContain('Running checks');
    expect(bar.querySelector('[role="status"]').textContent).toContain('Check 3 of 4');
    expect(bar.querySelector('[role="progressbar"]').getAttribute('aria-valuenow')).toBe('45');
    expect(bar.querySelector('.fc-live-time').textContent).toBe(FC.elapsedLabel(65));
    expect(bar.querySelector('button').disabled).toBe(true);
  });

  test('a finished run flashes its result in the head; Tower is fetched at most once a minute', async () => {
    load();
    const routes = ROUTES(PAYLOAD({ run: { state: 'running', stage: 'checks', done: 1, total: 4, elapsed_s: 5 }, running: true }));
    const calls = stubFetch(routes);
    await enter();
    routes['/api/forecast'] = PAYLOAD();
    await enter();
    expect($('fcRunbar').querySelector('.fc-done').textContent).toBe('✓ Finished — 2 findings');
    expect($('fcRunbar').classList.contains('fc-running')).toBe(false);
    expect(urls(calls).filter(u => u === '/api/forecast')).toHaveLength(2);
    expect(urls(calls).filter(u => u === '/api/tower/state')).toHaveLength(1);
    expect(urls(calls).filter(u => u === '/api/tower/insights')).toHaveLength(1);
  });

  test('a failed refresh keeps the findings and shows the error note in the head', async () => {
    load();
    const routes = ROUTES();
    stubFetch(routes);
    await enter();
    routes['/api/forecast'] = { ok: false, error: 'Forecast store is busy.' };
    await enter();
    expect($('fcRunbar').querySelector('.fc-note').textContent).toBe('Forecast store is busy.');
    expect(text('fcRows')).toContain('Disk / fills in 3 days');
    expect(tiles()['Open findings'][0]).toBe('2');
  });

  test('Run now is hidden for a viewer and posts /api/forecast/run for an operator', async () => {
    load({ role: 'viewer' });
    stubFetch(ROUTES());
    await enter();
    expect([...$('fcRunbar').querySelectorAll('button')].map(b => b.textContent)).not.toContain('Run now');
    load({ role: 'operator' });
    const calls = stubFetch(ROUTES());
    await enter();
    const btn = [...$('fcRunbar').querySelectorAll('button')].find(b => b.textContent === 'Run now');
    expect(btn).toBeTruthy();
    btn.click();
    await flush(); await flush();
    const post = calls.filter(c => c[1] && c[1].method === 'POST');
    expect(post.map(c => c[0])).toEqual(['/api/forecast/run']);
  });

  test('the Checks panel lists the chips with the counts as its note', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    expect(noteOf('fc-checks')).toBe('4 checks · 2 ran, 1 still collecting data, 1 could not run');
    expect(document.querySelector('[data-card="fc-checks"] .p-hd .p-note').classList.contains('fc-warn')).toBe(true);
    const chips = [...document.querySelectorAll('#fcChecks .fc-cstate')];
    expect(chips.map(c => c.textContent)).toEqual(['Disk filling1', 'Load change3 of 7 days', 'Model failurescould not run', 'Memory']);
    expect(chips[2].classList.contains('fc-failed')).toBe(true);
    const rows = [...document.querySelectorAll('[data-card="fc-checks"] .p-table tbody tr')];
    expect(rows).toHaveLength(4);
  });

  test('the checks note leaves out counts that are zero', async () => {
    load();
    stubFetch(ROUTES(PAYLOAD({ checks: [CHECKS[0], CHECKS[3]] })));
    await enter();
    expect(noteOf('fc-checks')).toBe('2 checks · 2 ran');
    expect(document.querySelector('[data-card="fc-checks"] .p-hd .p-note').classList.contains('fc-warn')).toBe(false);
  });

  test('the Tower panel paints ovTowerBody of the Tower state and insights', async () => {
    load();
    const calls = stubFetch(ROUTES());
    await enter();
    expect(urls(calls)).toEqual(expect.arrayContaining(['/api/forecast', '/api/tower/state', '/api/tower/insights']));
    const b = window.ovTowerBody(TOWER_STATE, TOWER_INSIGHTS);
    expect($('fc-b-tower').innerHTML).toBe(dom(b.html));
    expect(noteOf('fc-tower')).toBe(b.note);
    const rows = [...document.querySelectorAll('[data-card="fc-tower"] .p-table tbody tr')].map(r => r.children[0].textContent);
    expect(rows).toEqual(b.rows.map(r => r[0]));
  });

  test('a failed Tower pair shows the empty Tower body, and forecastLoad within a minute does not fetch Tower again', async () => {
    load();
    const routes = ROUTES();
    routes['/api/tower/state'] = () => { throw new Error('down'); };
    routes['/api/tower/insights'] = () => { throw new Error('down'); };
    const calls = stubFetch(routes);
    await enter();
    expect($('fc-b-tower').innerHTML).toBe(dom(window.ovTowerBody(null, null).html));
    await enter();
    expect(urls(calls).filter(u => u === '/api/forecast')).toHaveLength(2);
    expect(urls(calls).filter(u => u === '/api/tower/state')).toHaveLength(1);
    expect(urls(calls).filter(u => u === '/api/tower/insights')).toHaveLength(1);
  });

  test('without ovTowerBody the Tower panel says Tower unavailable', async () => {
    load();
    delete window.ovTowerBody;
    stubFetch(ROUTES());
    await enter();
    expect(text('fc-b-tower')).toBe('Tower unavailable');
    expect(noteOf('fc-tower')).toBe('');
  });

  test('a rebuilt page root repaints the lists from an unchanged payload', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    document.body.innerHTML = tabHtml;
    await enter();
    expect(text('fcRows')).toContain('Disk / fills in 3 days');
    expect(document.querySelectorAll('#fcChecks .fc-cstate')).toHaveLength(4);
    expect(document.querySelectorAll('#fcTabs .fc-tab')).toHaveLength(3);
  });

  test('no checks: the Checks panel shows only No checks yet', async () => {
    load();
    stubFetch(ROUTES(PAYLOAD({ checks: [] })));
    await enter();
    const box = $('fcChecks');
    expect(box.children).toHaveLength(1);
    expect(box.textContent).toBe('No checks yet');
    expect(noteOf('fc-checks')).toBe('');
    expect(document.querySelectorAll('[data-card="fc-checks"] .p-table tbody tr')).toHaveLength(0);
    expect(tiles()['Checks ran'][0]).toBe('0 / 0');
  });

  test('before any payload lands the panels show Loading…', async () => {
    load();
    stubFetch(ROUTES());
    const p = window.forecastLoad();
    expect(text('fc-b-tiles')).toContain('Loading…');
    expect(text('fc-b-tower')).toContain('Loading…');
    expect(text('fcRows')).toContain('Loading…');
    await p; await flush();
  });

  test('tabs, horizon and the details drawer still work in the Findings panel', async () => {
    load();
    stubFetch(ROUTES(PAYLOAD({ findings: FINDINGS.map((f, i) => Object.assign({}, f, { predicted_at: NOW_S + (i + 2) * 86400 })) })));
    await enter();
    const tabs = [...document.querySelectorAll('#fcTabs .fc-tab')];
    expect(tabs.map(t => t.textContent)).toEqual(['Open2', 'Cleared1', 'Dismissed0']);
    expect($('fcHorizon').hidden).toBe(false);
    expect(document.querySelectorAll('#fcHorizon .fc-hz-dot')).toHaveLength(2);
    expect(document.querySelector('#fcDetail .fc-title').textContent).toBe('Disk / fills in 3 days');
    document.querySelectorAll('#fcRows tbody tr')[1].click();
    expect(document.querySelector('#fcDetail .fc-title').textContent).toBe('Load up 40%');
    tabs[1].click();
    expect(text('fcRows')).toContain('Swap use settled');
    expect($('fcHorizon').hidden).toBe(true);
  });

  test('rows hidden in the layout stay hidden, and the refit hook evens out the page', async () => {
    load();
    window.layout = { forecastRowsHidden: ['fc-more'] };
    stubFetch(ROUTES());
    await enter();
    expect(document.querySelector('section.row[data-row="fc-more"]').hidden).toBe(true);
    expect(document.querySelector('section.row[data-row="fc-find"]').hidden).toBe(false);
    expect(typeof window.fcRefit).toBe('function');
    expect(() => window.fcRefit(document)).not.toThrow();
  });
});

describe('Forecast page in the settings drawer (#1432)', () => {
  function drawer() {
    const names = ['_kitRows', '_getDashSubTab', '_sdScope', 'toggleHostPageRow', '_kitPageReset', 'resetCurrentTabLayout'];
    evalGlobal(names.map(n => {
      const f = fnSrc(foundation, n);
      expect(f, n).toBeTruthy();
      return f;
    }).concat(['Object.assign(window, { toggleHostPageRow, resetCurrentTabLayout });']).join('\n'));
    window.SettingsLib = SettingsLib;
    window.saveLayout = vi.fn();
    window.renderSettingsPanel = vi.fn();
    window.hpRender = vi.fn();
    $('dash-forecast').classList.add('active');
  }
  afterEach(() => { ['hpRender', 'renderSettingsPanel', '_themedConfirm'].forEach(k => { delete window[k]; }); });

  test('the drawer scope is the Forecast rows page', () => {
    expect(SettingsLib.settingsScope('dashboard', 'forecast')).toMatchObject({ kind: 'rows', key: 'dashboard/forecast',
      label: 'Dashboards · Forecast', rowsHidden: 'forecastRowsHidden', root: 'fc-root' });
  });

  test('a row toggle hides the row under forecastRowsHidden and evens out the Forecast page, not the host pages', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    drawer();
    const refit = vi.spyOn(window, 'fcRefit');
    window.toggleHostPageRow('fc-more', false);
    expect(window.layout.forecastRowsHidden).toEqual(['fc-more']);
    expect(document.querySelector('section.row[data-row="fc-more"]').hidden).toBe(true);
    expect(refit).toHaveBeenCalledWith(document);
    expect(window.hpRender).not.toHaveBeenCalled();
    window.toggleHostPageRow('fc-more', true);
    expect(window.layout.forecastRowsHidden).toEqual([]);
  });

  test('Reset this page clears the Forecast rows under the forecast kit key, then evens out the page', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    drawer();
    window.layout = { forecastRowsHidden: ['fc-run'], energyRowsHidden: ['en-who'] };
    document.querySelector('section.row[data-row="fc-run"]').hidden = true;
    const store = window.mcShellStore;
    store.setItem(window.kitKey('rowOrder'), JSON.stringify({ forecast: ['fc-more', 'fc-run', 'fc-find'] }));
    const reset = vi.spyOn(window, 'kitLayoutReset');
    const refit = vi.spyOn(window, 'fcRefit');
    window._themedConfirm = vi.fn(async () => true);
    await window.resetCurrentTabLayout();
    expect(reset).toHaveBeenCalledWith('forecast', $('fc-root'), store);
    expect(window.layout).toEqual({ forecastRowsHidden: [], energyRowsHidden: ['en-who'] });
    expect(document.querySelector('section.row[data-row="fc-run"]').hidden).toBe(false);
    expect(refit).toHaveBeenCalledWith(document);
    expect(window.hpRender).not.toHaveBeenCalled();
  });
});

// Runs last: kitWire adds document listeners that stay for the rest of the file.
describe('Forecast page double-click (#1432)', () => {
  test('a double-click in the findings browser leaves the panel normal size; one on the header opens it full size', async () => {
    const m = srcFile('js/shell.js').match(/dblSkip: '([^']+)'/);
    expect(m && m[1]).toBe('#fc-b-findings');
    load();
    stubFetch(ROUTES(PAYLOAD({ findings: FINDINGS.map((f, i) => Object.assign({}, f, { predicted_at: NOW_S + (i + 2) * 86400 })) })));
    await enter();
    window.kitConfigure({ dblSkip: m[1] });
    window.kitWire(document);
    const panel = document.querySelector('[data-card="fc-findings"]');
    const dbl = (el) => el.dispatchEvent(new window.MouseEvent('dblclick', { bubbles: true }));
    for (const sel of ['#fcRows tbody tr td', '#fcHorizon .fc-hz-cap', '#fcToolbar', '#fcDetail .fc-title', '#fcTabs']) {
      const el = document.querySelector(sel);
      expect(el, sel).toBeTruthy();
      dbl(el);
      expect(panel.classList.contains('full'), sel).toBe(false);
    }
    document.querySelector('#fcTabs .fc-seg-btn:nth-child(2)').click();
    dbl(document.querySelector('#fcRows .fc-brow-head .fc-what'));
    expect(panel.classList.contains('full')).toBe(false);
    dbl(panel.querySelector('.p-hd .p-title'));
    expect(panel.classList.contains('full')).toBe(true);
    window.kitSetFull(panel, false);
  });
});

describe('Forecast page wiring (#1432)', () => {
  test('index.html holds only the page root, without the old card', () => {
    const page = indexDoc.getElementById('dash-forecast');
    expect(page.children).toHaveLength(1);
    expect(page.firstElementChild.id).toBe('fc-root');
    expect(page.firstElementChild.className).toBe('fc-root');
    expect(page.getAttribute('style')).toBeNull();
    expect(page.querySelector('.fc-card')).toBeNull();
    const css = srcFile('css/forecast.css');
    for (const gone of ['.fc-card', '.fc-runbar', '.fc-checks-box']) expect(css, gone).not.toContain(gone);
    expect(css).toContain('.fc-head');
    expect(css).toContain('.row[data-row] > .fc-head.fc-running { order: 1; flex: 1 1 100%; }');
  });
  test('the shell refit hook calls fcRefit', () => {
    expect(srcFile('js/shell.js')).toMatch(/window\.fcRefit\(root\)/);
  });
});
