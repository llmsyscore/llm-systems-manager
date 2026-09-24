// #1031: Forecast sub-tab registration, panel + Overall card markup, asset
// tags and cache-busters, and the IIFE/no-innerHTML rules js/forecast.js keeps.
import { describe, it, expect, beforeAll } from 'vitest';
import { srcFile, loadSwitchSubTab } from './helpers/harness.js';

const LIB_V = '2026.09.21-6';
const CSS_V = '2026.09.22-1';
const V = '2026.09.21-7';
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

  it('the sub-tab button sits in the dashboard nav', () => {
    const btn = document.querySelector(
      '#dashboardTab .sub-tab-nav [onclick="switchSubTab(\'dashboard\',\'forecast\')"]');
    expect(btn).toBeTruthy();
    expect(btn.textContent.trim()).toBe('Forecast');
  });

  it('the panel follows the Energy panel and holds every render target', () => {
    expect(indexSrc.indexOf('id="dash-forecast"'))
      .toBeGreaterThan(indexSrc.indexOf('id="dash-energy"'));
    const panel = document.getElementById('dash-forecast');
    expect(panel).toBeTruthy();
    expect(panel.classList.contains('sub-tab-panel')).toBe(true);
    for (const id of ['fcBrief', 'fcRunbar', 'fcChecksBox', 'fcChecks', 'fcTabs', 'fcHorizon', 'fcToolbar',
                      'fcRows', 'fcPager', 'fcDetail']) {
      expect(panel.querySelector('#' + id), id).toBeTruthy();
    }
  });

  it('the Overall page carries a Forecast strip placed after the alerts strip', () => {
    const strip = document.querySelector('.ov-band > [data-strip="forecast"]');
    expect(strip).toBeTruthy();
    expect(strip.dataset.stripAfter).toBe('alerts');
    expect(strip.querySelector('.ov-strip-handle')).toBeTruthy();
    for (const id of ['fcOvCount', 'fcOvBody', 'fcOvFoot', 'fcOvView']) {
      expect(strip.querySelector('#' + id), id).toBeTruthy();
    }
  });

  it('the run bar and checks lead, Tower\'s analysis folds away beneath them', () => {
    const panel = document.getElementById('dash-forecast');
    const order = [...panel.querySelectorAll('#fcBrief, #fcRunbar, #fcChecksBox, #fcTabs, #fcSplit')].map(n => n.id);
    expect(order).toEqual(['fcRunbar', 'fcChecksBox', 'fcBrief', 'fcTabs', 'fcSplit']);
    expect(document.getElementById('fcBrief').tagName).toBe('DETAILS');
    expect(document.getElementById('fcChecksBox').tagName).toBe('DETAILS');
  });
});

describe('Forecast assets (#1031)', () => {
  it('loads lib/forecast.js before js/forecast.js, both cache-busted', () => {
    const lib = indexSrc.indexOf(`/static/js/lib/forecast.js?v=${LIB_V}`);
    const page = indexSrc.indexOf(`/static/js/forecast.js?v=${V}`);
    expect(lib).toBeGreaterThan(-1);
    expect(page).toBeGreaterThan(-1);
    expect(lib).toBeLessThan(page);
  });

  it('links css/forecast.css with its own cache-buster', () => {
    expect(indexSrc).toContain(`/static/css/forecast.css?v=${CSS_V}`);
  });

  it('bumps the cache-buster of every edited script', () => {
    expect(indexSrc).toContain('/static/js/boot.js?v=2026.09.23-5');
    expect(indexSrc).toContain('/static/js/overall.js?v=2026.09.19-1');
    expect(indexSrc).toContain('/static/js/foundation.js?v=2026.09.21-1');
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

  it('exposes only the two page hooks', () => {
    expect(pageSrc).toContain('window.forecastLoad');
    expect(pageSrc).toContain('window.forecastOverallCard');
    // Assignments only — `window.x === y` comparisons are not exports.
    const globals = [...pageSrc.matchAll(/window\.([A-Za-z_$][\w$]*)\s*=(?!=)/g)].map(m => m[1]);
    expect([...new Set(globals)].sort()).toEqual(['forecastLoad', 'forecastOverallCard']);
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
