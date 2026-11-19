// #1031: Forecast sub-tab registration, panel + Overall panel markup, asset
// tags and cache-busters, and the IIFE/no-innerHTML rules js/forecast.js keeps.
import { describe, it, expect, beforeAll } from 'vitest';
import { srcFile, loadSwitchSubTab } from './helpers/harness.js';

const indexSrc = srcFile('index.html');
const bootSrc = srcFile('js/boot.js');
const swSrc = srcFile('sw.js');
const pageSrc = srcFile('js/forecast.js');
const cssSrc = srcFile('css/forecast.css');

describe('Forecast sub-tab registration (#1031)', () => {
  beforeAll(() => { loadSwitchSubTab(bootSrc); });

  it('boot.js lists forecast among the dashboard sub-tabs', () => {
    expect(window._SUB_TAB_MAP.dashboard.subs).toContain('forecast');
  });

  it('switching to it activates the panel and calls forecastLoad', () => {
    document.documentElement.innerHTML = indexSrc;
    const calls = [];
    window.forecastLoad = () => calls.push('load');
    window.switchSubTab('dashboard', 'forecast');
    expect(document.getElementById('dash-forecast').classList.contains('active')).toBe(true);
    expect(document.getElementById('dash-energy').classList.contains('active')).toBe(false);
    expect(calls).toEqual(['load']);
  });
});

describe('Forecast markup (#1031)', () => {
  beforeAll(() => { document.documentElement.innerHTML = indexSrc; });

  it('the sub-tab button sits in the sidebar Dashboards group', () => {
    const btn = document.querySelector('#side [data-tab="dashboard"][data-sub="forecast"]');
    expect(btn).toBeTruthy();
    expect(btn.textContent.trim()).toBe('Forecast');
  });

  it('the panel follows the Energy panel and holds every render target', () => {
    expect(indexSrc.indexOf('id="dash-forecast"'))
      .toBeGreaterThan(indexSrc.indexOf('id="dash-energy"'));
    const panel = document.getElementById('dash-forecast');
    expect(panel).toBeTruthy();
    expect(panel.classList.contains('sub-tab-panel')).toBe(true);
    for (const id of ['fc-root', 'fcBrief', 'fcRunbar', 'fcChecks', 'fcTabs', 'fcHorizon', 'fcToolbar',
                      'fcRows', 'fcPager', 'fcDetail', 'fc-b-tiles', 'fc-b-findings', 'fc-b-checks', 'fc-b-tower']) {
      expect(panel.querySelector('#' + id), id).toBeTruthy();
    }
  });

  it('the Overall page carries a Forecast panel first in the Watch list row', () => {
    const row = document.querySelector('#overallTab section.row[data-row="watch"]');
    expect(row).toBeTruthy();
    const panel = row.querySelector('.panels > section.panel');
    expect(panel.dataset.card).toBe('forecast');
    expect(panel.dataset.title).toBe('Forecast');
    expect(panel.querySelector('.p-body#ov-b-forecast')).toBeTruthy();
  });

  it('the run bar sits in the Forecast row header, Tower\'s analysis folds away above the tabs, the checks follow the findings', () => {
    const panel = document.getElementById('dash-forecast');
    const order = [...panel.querySelectorAll('#fcBrief, #fcRunbar, #fcChecks, #fcTabs, #fcSplit')].map(n => n.id);
    expect(order).toEqual(['fcRunbar', 'fcBrief', 'fcTabs', 'fcSplit', 'fcChecks']);
    expect(document.getElementById('fcRunbar').parentNode.getAttribute('data-row')).toBe('fc-run');
    expect(document.getElementById('fcBrief').tagName).toBe('DETAILS');
    expect(document.getElementById('fcChecks').closest('details')).toBeNull();
  });
});

describe('Forecast assets (#1031)', () => {
  it('loads lib/forecast.js before js/forecast.js', () => {
    const lib = indexSrc.indexOf('src="/static/js/lib/forecast.js"');
    const page = indexSrc.indexOf('src="/static/js/forecast.js"');
    expect(lib).toBeGreaterThan(-1);
    expect(page).toBeGreaterThan(-1);
    expect(lib).toBeLessThan(page);
  });

  it('links css/forecast.css', () => {
    expect(indexSrc).toContain('href="/static/css/forecast.css"');
  });

  it('loads the scripts the forecast tab depends on', () => {
    expect(indexSrc).toContain('src="/static/js/boot.js"');
    expect(indexSrc).toContain('src="/static/js/overall.js"');
    expect(indexSrc).toContain('src="/static/js/foundation.js"');
  });

  it('wraps long unbroken tokens instead of overflowing the page', () => {
    expect(cssSrc).toContain('overflow-wrap: anywhere');
  });

  it('the service worker pre-caches none of them: the companion never loads Forecast', () => {
    for (const p of ['/static/js/lib/forecast.js', '/static/js/forecast.js',
                     '/static/css/forecast.css']) {
      expect(swSrc.includes(`'${p}'`), p).toBe(false);
    }
  });
});

describe('js/forecast.js scope and escaping rules (#1031)', () => {
  it('is one IIFE and declares nothing at top level', () => {
    expect(pageSrc.trimStart().startsWith('(function')
           || /^\(function/m.test(pageSrc.split('\n').find(l => l.startsWith('(function')) || '')).toBe(true);
    // Column-0 declarations = the shared global scope; fmt/cssVar/el are taken.
    expect(pageSrc).not.toMatch(/^(?:const|let|var|function)\s+(?:fmt|cssVar|el)\b/m);
    expect(pageSrc).not.toMatch(/^(?:const|let|var|function)\s/m);
  });

  it('exposes only the two page hooks, the kit refit hook and its scroll listener', () => {
    expect(pageSrc).toContain('window.forecastLoad');
    expect(pageSrc).toContain('window.forecastSnapshot');
    // Assignments only — `window.x === y` comparisons are not exports.
    const globals = [...pageSrc.matchAll(/window\.([A-Za-z_$][\w$]*)\s*=(?!=)/g)].map(m => m[1]);
    expect([...new Set(globals)].sort()).toEqual(['_fcOnScroll', 'fcRefit', 'forecastLoad', 'forecastSnapshot']);
  });

  it('never writes API strings as HTML', () => {
    for (const bad of ['innerHTML', 'insertAdjacentHTML', 'outerHTML']) {
      expect(pageSrc.includes(bad), bad).toBe(false);
    }
  });

  it('offers Open conversation only through the read-only drawer entry point, to admins with a kept thread', () => {
    expect(pageSrc).toContain("FC.canOpenThread(f, isAdmin())");
    expect(pageSrc).toContain('window.towerOpenThread(tid)');
    expect(pageSrc).not.toContain('/api/tower/threads');
  });
});

describe('forecastSnapshot (#1432)', () => {
  it('returns the cached payload and refetches it at most once a minute', async () => {
    document.body.innerHTML = '';
    const calls = [];
    window.fetch = async (url) => {
      calls.push(url);
      return { ok: true, json: async () => ({ ok: true, enabled: true, findings: [] }) };
    };
    (0, eval)(pageSrc);
    expect(window.forecastSnapshot()).toBeNull();
    for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0));
    expect(calls).toEqual(['/api/forecast']);
    expect(window.forecastSnapshot()).toMatchObject({ ok: true, enabled: true });
    expect(calls).toHaveLength(1);
  });
});
