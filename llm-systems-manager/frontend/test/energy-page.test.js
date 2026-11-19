// #1432 phase 4d: Dashboards › Energy on the panel kit, built by js/energy.js.
// Runs kit.js, overall-view, energy (EN), overall.js and energy.js on the real #dash-energy markup with fetch stubbed.
import { describe, test, expect, afterEach, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import OV from '../js/lib/overall-view.js';
import EN from '../js/lib/energy.js';
import SettingsLib from '../js/lib/settingsdrawer.js';
import { srcFile, fnSrc, evalGlobal, flush } from './helpers/harness.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const SUMMARY = JSON.parse(readFileSync(resolve(HERE, 'fixtures/energy_summary_days_1.json'), 'utf8'));
const indexDoc = new JSDOM(srcFile('index.html')).window.document;
// #dashboardTab holding only #dash-energy, to keep the DOM small.
const tabHtml = (() => {
  const tab = indexDoc.getElementById('dashboardTab').cloneNode(false);
  tab.appendChild(indexDoc.getElementById('dash-energy').cloneNode(true));
  return tab.outerHTML;
})();
const energy = srcFile('js/energy.js');
const overall = srcFile('js/overall.js');
const foundation = srcFile('js/foundation.js');
evalGlobal(srcFile('kit/kit.js'));
window.OV = OV;
window.EN = EN;

const ID_A = '00000000-0000-4000-8000-000000000001';
const LLAMA_AGG = { agents: [{ age_s: 2, agent_id: ID_A, online: true, power_watts: 11.0, state: 'sleeping' }],
  gpu: { total_power_watts: 11.0 }, throughput: { total_pps: 0, total_tps: 0 } };
const BY_PROVIDER = { llama: [{ agent_id: ID_A, hostname: 'host-a' }], lms: [], vllm: [] };

// Hourly rows from a local midnight, one an hour, each with idle and generating energy and tokens.
function hourly(n) {
  const t0 = new Date(2026, 9, 1).getTime() / 1000, rows = [];
  for (let i = 0; i < n; i++) rows.push({ hour_ts: t0 + i * 3600, energy_wh: 10, active_energy_wh: 4, tokens_gen: 100 });
  return { ok: true, label: 'x', rows, truncated: false };
}
const windowSummary = (label) => Object.assign(JSON.parse(JSON.stringify(SUMMARY)), { window: Object.assign({}, SUMMARY.window, { label }) });

// Every URL fetched; each answers a copy of the first route its URL starts with, else {}.
function stubFetch(routes) {
  const calls = [];
  window.fetch = vi.fn(async (url) => {
    calls.push(url);
    const key = Object.keys(routes).find(k => url.startsWith(k));
    const v = key ? (typeof routes[key] === 'function' ? await routes[key](url) : routes[key]) : {};
    return { ok: true, json: async () => JSON.parse(JSON.stringify(v)) };
  });
  return calls;
}
const ROUTES = () => ({
  '/api/energy/summary?days=1': SUMMARY,
  '/api/energy/summary?': windowSummary('November 2026'),
  '/api/energy/hourly?': hourly(48),
  '/api/fleet/llama/aggregate': LLAMA_AGG,
});

function makeStore() {
  const m = {};
  return { getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null), setItem: (k, v) => { m[k] = String(v); } };
}
// A fresh #dash-energy and fresh overall.js and energy.js.
function load() {
  document.body.innerHTML = tabHtml;
  window._activeTab = 'dashboard';
  window._subTabState = { dashboard: 'energy' };
  window.mcShellStore = makeStore();
  window.layout = {};
  window._agentsByProvider = BY_PROVIDER;
  window._pollCfg = {};
  window._activeAlerts = [];
  window.forecastSnapshot = () => null;
  Object.keys(window.KIT_SERIES_OFF).forEach(k => { delete window.KIT_SERIES_OFF[k]; });
  evalGlobal(overall);
  evalGlobal(energy);
}
const $ = (id) => document.getElementById(id);
const text = (id) => $(id).textContent;
const noteOf = (card) => (document.querySelector(`[data-card="${card}"] .p-hd .p-note`) || { textContent: '' }).textContent;
async function enter() { await window.initEnergyTab(); await flush(); }
function change(id, value) {
  const el = $(id);
  el.value = value;
  el.dispatchEvent(new window.Event('change', { bubbles: true }));
}
// HTML as the DOM serialises it, for comparing a built body with a panel's innerHTML.
function dom(html) { const d = document.createElement('div'); d.innerHTML = html; return d.innerHTML; }
const summaryCalls = (calls) => calls.filter(u => u.startsWith('/api/energy/summary?') && u !== '/api/energy/summary?days=1');

afterEach(() => { vi.restoreAllMocks(); });

describe('Energy page (#1432)', () => {
  test('entry builds 4 rows and the 7 panels and fetches every payload', async () => {
    load();
    const calls = stubFetch(ROUTES());
    await enter();
    const rows = [...document.querySelectorAll('#en-root section.row[data-row]')].map(r => r.getAttribute('data-row'));
    expect(rows).toEqual(['en-tot', 'en-time', 'en-who', 'en-now']);
    const titles = [...document.querySelectorAll('#en-root section.row[data-row] > .row-hd')]
      .map(b => [...b.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim());
    expect(titles).toEqual(['Totals', 'Over time', 'Hosts and models', 'Snapshot']);
    const panels = [...document.querySelectorAll('#en-root section.panel[data-card]')].map(p => p.getAttribute('data-card'));
    expect(panels).toEqual(['en-tiles', 'en-hourly', 'en-tokens', 'en-hosts', 'en-models', 'en-split', 'en-power']);
    expect($('en-root').getAttribute('data-kit-page')).toBe('energy');
    expect(summaryCalls(calls)).toHaveLength(1);
    expect(calls.filter(u => u.startsWith('/api/energy/hourly?'))).toHaveLength(1);
    expect(calls).toContain('/api/energy/summary?days=1');
    for (const p of ['llama', 'lms', 'vllm']) expect(calls).toContain(`/api/fleet/${p}/aggregate`);
    const shown = $('en-root').textContent;
    for (const bad of ['NaN', 'undefined', 'Infinity']) expect(shown, bad).not.toContain(bad);
  });

  test('full-width Energy by host and by model get no span; a short line\'s last panel spans the empty columns', async () => {
    const P = window.HTMLElement.prototype, gcs = window.getComputedStyle;
    const rootW = Object.getOwnPropertyDescriptor(P, 'clientWidth');
    Object.defineProperty(P, 'clientWidth', { configurable: true, get() { return 1500; } });
    window.getComputedStyle = (el) => (el.classList && el.classList.contains('panels') ? { gridTemplateColumns: '500px 500px 500px' } : gcs(el));
    try {
      load();
      stubFetch(ROUTES());
      await enter();
      window.enRefit(document);
      const span = (c) => document.querySelector('[data-card="' + c + '"]').style.gridColumn;
      expect([span('en-hosts'), span('en-models')]).toEqual(['', '']);
      expect([span('en-split'), span('en-power')]).toEqual(['', 'span 2']);
    } finally {
      window.getComputedStyle = gcs;
      if (rootW) Object.defineProperty(P, 'clientWidth', rootW); else delete P.clientWidth;
    }
  });

  test('the head has the window select, the price boxes, Refresh and the note line', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    const head = document.querySelector('#en-root .en-head');
    expect(head).not.toBeNull();
    const opts = [...$('en-window').options].map(o => o.textContent);
    expect(opts).toEqual(EN.windowOptions().map(o => o.label));
    expect($('en-window').value).toBe(EN.windowOptions()[0].value);
    expect($('en-from-f').hidden).toBe(true);
    expect($('en-to-f').hidden).toBe(true);
    expect(head.textContent).toContain('Electricity $/kWh');
    expect(head.textContent).toContain('Cloud $/Mtok in');
    expect(head.textContent).toContain('Cloud $/Mtok out');
    expect($('en-refresh').textContent).toBe('Refresh');
    expect($('en-note').getAttribute('role')).toBe('status');
    // Prefilled from summary.config.
    expect($('en-price').value).toBe('0.15');
    expect($('en-cloud-in').value).toBe('5');
    expect($('en-cloud-out').value).toBe('25');
  });

  test('the summary URL carries the typed prices; the hourly URL carries the window only', async () => {
    load();
    const calls = stubFetch(ROUTES());
    await enter();
    $('en-price').value = '0.3';
    $('en-cloud-in').value = '2';
    change('en-cloud-out', '8');
    await flush();
    const url = summaryCalls(calls).pop();
    const p = new URLSearchParams(url.split('?')[1]);
    expect(p.get('price_kwh')).toBe('0.3');
    expect(p.get('cloud_in')).toBe('2');
    expect(p.get('cloud_out')).toBe('8');
    expect(p.get('month')).toBe(EN.windowOptions()[0].value.split(':')[1]);
    const h = new URLSearchParams(calls.filter(u => u.startsWith('/api/energy/hourly?')).pop().split('?')[1]);
    expect(h.has('price_kwh')).toBe(false);
    expect(h.get('month')).toBe(p.get('month'));
  });

  test('Refresh fetches again', async () => {
    load();
    const calls = stubFetch(ROUTES());
    await enter();
    $('en-refresh').click();
    await flush();
    expect(summaryCalls(calls)).toHaveLength(2);
    expect(calls.filter(u => u === '/api/fleet/llama/aggregate')).toHaveLength(2);
  });

  test('Power now refetches the aggregates every minute while the page shows; a window change does not', async () => {
    load();
    const ticks = [];
    vi.spyOn(window, 'setInterval').mockImplementation((fn, ms) => { ticks.push([fn, ms]); return 1; });
    const calls = stubFetch(ROUTES());
    await enter();
    const aggCalls = () => calls.filter(u => u === '/api/fleet/llama/aggregate').length;
    expect(ticks.map(t => t[1])).toEqual([60000]);
    change('en-window', 'days:7');
    await flush();
    expect(aggCalls()).toBe(1);
    ticks[0][0]();
    await flush();
    expect(aggCalls()).toBe(2);
    expect(calls.filter(u => u === '/api/energy/summary?days=1')).toHaveLength(1);
  });

  test('Custom with one date shows the note and fetches nothing', async () => {
    load();
    const calls = stubFetch(ROUTES());
    await enter();
    const before = calls.length;
    change('en-window', 'custom');
    await flush();
    expect($('en-from-f').hidden).toBe(false);
    expect($('en-to-f').hidden).toBe(false);
    change('en-from', '2026-06-01');
    await flush();
    expect(calls.length).toBe(before);
    expect(text('en-note')).toBe('Pick both dates for the custom range.');
    change('en-to', '2026-06-10');
    await flush();
    const p = new URLSearchParams(summaryCalls(calls).pop().split('?')[1]);
    expect(p.get('start')).toBe('2026-06-01');
    expect(p.get('end')).toBe('2026-06-10');
    expect(p.has('tz_offset_min')).toBe(true);
    expect(text('en-note')).toBe('');
  });

  test('Custom with one date leaves a slower window\'s answer to paint', async () => {
    load();
    const routes = ROUTES();
    stubFetch(routes);
    await enter();
    let release;
    const gate = new Promise(r => { release = r; });
    routes['/api/energy/summary?'] = async () => { await gate; return windowSummary('Slow window'); };
    change('en-window', 'days:7');
    await flush();
    change('en-window', 'custom');
    change('en-from', '2026-06-01');
    await flush();
    expect(text('en-note')).toBe('Pick both dates for the custom range.');
    release();
    for (let i = 0; i < 5; i++) await flush();
    expect(noteOf('en-tiles')).toBe('Slow window');
    expect(text('en-note')).toBe('Pick both dates for the custom range.');
  });

  test('today adds tz_offset_min to both URLs, days:7 does not', async () => {
    load();
    const calls = stubFetch(ROUTES());
    await enter();
    change('en-window', 'today');
    await flush();
    const s = new URLSearchParams(summaryCalls(calls).pop().split('?')[1]);
    const h = new URLSearchParams(calls.filter(u => u.startsWith('/api/energy/hourly?')).pop().split('?')[1]);
    expect(s.get('days')).toBe('1');
    expect(s.has('tz_offset_min')).toBe(true);
    expect(h.has('tz_offset_min')).toBe(true);
    change('en-window', 'days:7');
    await flush();
    const s7 = new URLSearchParams(summaryCalls(calls).pop().split('?')[1]);
    expect(s7.get('days')).toBe('7');
    expect(s7.has('tz_offset_min')).toBe(false);
  });

  test('the tiles show Saved vs cloud first, then the totals, with the savings sentence and the window label', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    const labels = [...document.querySelectorAll('#en-b-tiles .en-tl')].map(e => e.textContent);
    expect(labels).toEqual(EN.pageTiles(SUMMARY).map(t => t.label));
    expect(labels[0]).toBe('Saved vs cloud');
    expect(labels).toHaveLength(7);
    const vals = [...document.querySelectorAll('#en-b-tiles .k-val')].map(e => e.textContent);
    expect(vals).toEqual(EN.pageTiles(SUMMARY).map(t => t.value));
    expect(document.querySelector('#en-b-tiles .en-tile .k-sub').textContent).toBe('vs $3.38 at Opus 5');
    expect(document.querySelector('#en-b-tiles > .k-sub').textContent).toBe(EN.savingsLine(SUMMARY));
    expect(noteOf('en-tiles')).toBe('November 2026');
    expect(text('en-foot')).toBe(EN.coverageNote(SUMMARY));
  });

  test('a 720-row hourly payload draws 30 bars and the note reads Wh per 24 hours', async () => {
    load();
    stubFetch(Object.assign(ROUTES(), { '/api/energy/hourly?': hourly(720) }));
    await enter();
    const ts = document.querySelector('#en-b-hourly .ts[data-kit-ts]');
    const o = window.kitTsData(ts);
    expect(o.labels).toHaveLength(30);
    expect(o.mode).toBe('stack');
    expect(o.joined).toBe(true);
    expect(o.series.map(s => [s.name, s.slot, !!s.dim])).toEqual([['Idle', 5, true], ['Generating', 2, false]]);
    expect(document.querySelectorAll('#en-b-hourly .ts-bar[fill="var(--s2)"]')).toHaveLength(30);
    expect(noteOf('en-hourly')).toBe('Wh per 24 hours');
    const tok = window.kitTsData(document.querySelector('#en-b-tokens .ts[data-kit-ts]'));
    expect(tok.labels).toHaveLength(30);
    expect(tok.series.map(s => [s.name, s.slot])).toEqual([['Tokens', 1]]);
    expect(tok.series[0].values[0]).toBe(2400);
    const head = [...document.querySelectorAll('[data-card="en-hourly"] .p-table th')].map(e => e.textContent);
    expect(head).toEqual(['From', 'Idle Wh', 'Generating Wh', 'Tokens']);
    expect(document.querySelectorAll('[data-card="en-tokens"] .p-table tbody tr')).toHaveLength(30);
  });

  test('a short payload is per hour, and a truncated one says so', async () => {
    load();
    stubFetch(Object.assign(ROUTES(), { '/api/energy/hourly?': Object.assign(hourly(48), { truncated: true }) }));
    await enter();
    expect(noteOf('en-hourly')).toBe('Wh per hour (chart: last 45 days of range)');
  });

  test('the host table has 10 columns and the model table 8', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    const hcols = [...document.querySelectorAll('#en-b-hosts th')].map(e => e.textContent);
    expect(hcols).toHaveLength(10);
    expect(hcols).toEqual(EN.hostTable(SUMMARY.hosts).cols);
    expect(document.querySelectorAll('#en-b-hosts tbody tr')).toHaveLength(SUMMARY.hosts.length);
    const mcols = [...document.querySelectorAll('#en-b-models th')].map(e => e.textContent);
    expect(mcols).toEqual(EN.modelTable(SUMMARY.models).cols);
    expect(mcols).toHaveLength(8);
  });

  test('no hosts or models in the window says so', async () => {
    load();
    stubFetch(Object.assign(ROUTES(), { '/api/energy/summary?': Object.assign(windowSummary('w'), { hosts: [], models: [] }) }));
    await enter();
    expect(text('en-b-hosts')).toBe('No per-host data in this window yet.');
    expect(text('en-b-models')).toBe('No per-model data in this window yet.');
  });

  test('Snapshot shows Today\'s split and Power now as on Overall', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    const split = window.ovSplitBody(SUMMARY, null);
    expect($('en-b-split').innerHTML).toBe(dom(split.html));
    const power = window.ovPowerBody({ llama: LLAMA_AGG, lms: {}, vllm: {} }, BY_PROVIDER, null, null);
    expect($('en-b-power').innerHTML).toBe(dom(power.html));
    expect(noteOf('en-power')).toBe('11.0 W');
    expect([...document.querySelectorAll('#en-b-power .r-val')].map(e => e.textContent)).toEqual(['11.0 W']);
    expect($('en-b-power').querySelector('.ov-spark')).toBeNull();
  });

  test('a failed summary shows its error in the note line', async () => {
    load();
    stubFetch(Object.assign(ROUTES(), { '/api/energy/summary?': { ok: false, error: 'bad window' } }));
    await enter();
    expect(text('en-note')).toBe('bad window');
  });

  test('two control changes whose fetches resolve in reverse order end showing the second window', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    const gates = {};
    const held = (label) => new Promise(r => { gates[label] = () => r(windowSummary(label)); });
    stubFetch(Object.assign(ROUTES(), {
      '/api/energy/summary?days=1': SUMMARY,
      '/api/energy/summary?': (url) => held(url.includes('days=7') ? 'seven' : 'thirty'),
    }));
    change('en-window', 'days:7');
    change('en-window', 'days:30');
    await flush();
    gates.thirty();
    await flush();
    gates.seven();
    await flush();
    await flush();
    expect(noteOf('en-tiles')).toBe('thirty');
  });

  test('a window change shows Loading… in the window panels until the new data lands; Snapshot keeps its data', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    const gates = [];
    const held = (v) => () => new Promise(r => { gates.push(() => r(v)); });
    stubFetch(Object.assign(ROUTES(), { '/api/energy/summary?days=1': SUMMARY,
      '/api/energy/summary?': held(windowSummary('seven')), '/api/energy/hourly?': held(hourly(48)) }));
    change('en-window', 'days:7');
    await flush();
    for (const id of ['en-b-tiles', 'en-b-hourly', 'en-b-tokens', 'en-b-hosts', 'en-b-models']) expect(text(id), id).toBe('Loading…');
    expect(noteOf('en-tiles')).toBe('');
    expect(text('en-b-split')).not.toBe('Loading…');
    expect(text('en-b-power')).toContain('11.0 W');
    gates.forEach(g => g());
    await flush();
    await flush();
    expect(noteOf('en-tiles')).toBe('seven');
    expect(document.querySelector('#en-b-hourly .ts[data-kit-ts]')).not.toBeNull();
  });

  test('a window whose hourly fetch fails drops the old window\'s bars', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    expect(document.querySelector('#en-b-hourly .ts-bar')).not.toBeNull();
    stubFetch(Object.assign(ROUTES(), { '/api/energy/hourly?': { ok: false, error: 'boom' } }));
    change('en-window', 'days:7');
    await flush();
    expect(document.querySelector('#en-b-hourly .ts-bar')).toBeNull();
    expect(document.querySelector('#en-b-tokens .ts-bar')).toBeNull();
    expect(text('en-b-hourly')).toBe('Could not load this window.');
    expect(text('en-b-tokens')).toBe('Could not load this window.');
    expect(noteOf('en-hourly')).toBe('');
    expect(document.querySelectorAll('[data-card="en-hourly"] .p-table tbody tr')).toHaveLength(0);
  });

  test('a window whose summary fails drops the old tiles and tables; failed Snapshot payloads are dropped too', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    window.fetch = vi.fn(async (url) => {
      if (url.startsWith('/api/energy/hourly?')) return { ok: true, json: async () => hourly(48) };
      if (url.startsWith('/api/energy/summary?') && url !== '/api/energy/summary?days=1') throw new Error('offline');
      return { ok: false, status: 500, json: async () => ({}) };
    });
    change('en-window', 'days:7');
    await flush();
    for (const id of ['en-b-tiles', 'en-b-hosts', 'en-b-models']) expect(text(id), id).toBe('Could not load this window.');
    expect(noteOf('en-tiles')).toBe('');
    expect(text('en-foot')).toBe('');
    expect(text('en-note')).toBe('Failed to load energy summary: Error: offline');
    // A window change leaves the Snapshot row alone; Refresh drops its failed payloads.
    expect(text('en-b-power')).toContain('11.0 W');
    $('en-refresh').click();
    await flush();
    expect(text('en-b-power')).not.toContain('11.0 W');
    expect(text('en-b-split')).toContain('Nothing in this period.');
  });

  test('rows hidden in the layout stay hidden, and the refit hook evens out the page', async () => {
    load();
    window.layout = { energyRowsHidden: ['en-who'] };
    stubFetch(ROUTES());
    await enter();
    expect(document.querySelector('section.row[data-row="en-who"]').hidden).toBe(true);
    expect(document.querySelector('section.row[data-row="en-now"]').hidden).toBe(false);
    expect(typeof window.enRefit).toBe('function');
    expect(() => window.enRefit(document)).not.toThrow();
  });
});

describe('Energy page in the settings drawer (#1432)', () => {
  // foundation.js's row toggle and page reset, run on the shown Energy page.
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
    $('dash-energy').classList.add('active');
  }
  afterEach(() => { ['hpRender', 'renderSettingsPanel', '_themedConfirm'].forEach(k => { delete window[k]; }); });

  test('a row toggle hides the row under energyRowsHidden and evens out the Energy page, not the host pages', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    drawer();
    const refit = vi.spyOn(window, 'enRefit');
    window.toggleHostPageRow('en-who', false);
    expect(window.layout.energyRowsHidden).toEqual(['en-who']);
    expect(document.querySelector('section.row[data-row="en-who"]').hidden).toBe(true);
    expect(refit).toHaveBeenCalledWith(document);
    expect(window.hpRender).not.toHaveBeenCalled();
    expect(window.saveLayout).toHaveBeenCalledTimes(1);
    window.toggleHostPageRow('en-who', true);
    expect(window.layout.energyRowsHidden).toEqual([]);
    expect(document.querySelector('section.row[data-row="en-who"]').hidden).toBe(false);
  });

  test('Reset this page clears the Energy rows, folds and order under the energy kit key, then evens out the page', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    drawer();
    window.layout = { energyRowsHidden: ['en-now'], hostRowsHidden: ['host-gpu'] };
    document.querySelector('section.row[data-row="en-now"]').hidden = true;
    const store = window.mcShellStore;
    store.setItem(window.kitKey('rows'), JSON.stringify({ energy: { 'en-tot': 1 }, 'host-host': { 'host-gpu': 1 } }));
    store.setItem(window.kitKey('rowOrder'), JSON.stringify({ energy: ['en-now', 'en-tot', 'en-time', 'en-who'] }));
    const reset = vi.spyOn(window, 'kitLayoutReset');
    const refit = vi.spyOn(window, 'enRefit');
    window._themedConfirm = vi.fn(async () => false);
    await window.resetCurrentTabLayout();
    expect(reset).not.toHaveBeenCalled();
    expect(window.layout.energyRowsHidden).toEqual(['en-now']);
    window._themedConfirm = vi.fn(async () => true);
    await window.resetCurrentTabLayout();
    expect(reset).toHaveBeenCalledWith('energy', $('en-root'), store);
    expect(window.layout).toEqual({ energyRowsHidden: [], hostRowsHidden: ['host-gpu'] });
    expect(document.querySelector('section.row[data-row="en-now"]').hidden).toBe(false);
    expect(JSON.parse(store.getItem(window.kitKey('rows')))).toEqual({ 'host-host': { 'host-gpu': 1 } });
    expect(JSON.parse(store.getItem(window.kitKey('rowOrder')))).toEqual({});
    expect([...document.querySelectorAll('#en-root section.row[data-row]')].map(r => r.getAttribute('data-row')))
      .toEqual(['en-tot', 'en-time', 'en-who', 'en-now']);
    expect(refit).toHaveBeenCalledWith(document);
    expect(window.hpRender).not.toHaveBeenCalled();
    expect(window.saveLayout).toHaveBeenCalledTimes(1);
  });
});

describe('Energy page wiring (#1432)', () => {
  test('index.html holds only the page root, and loads manager-view.js after its globals', () => {
    const page = indexDoc.getElementById('dash-energy');
    expect(page.children).toHaveLength(1);
    expect(page.firstElementChild.id).toBe('en-root');
    expect(page.firstElementChild.className).toBe('en-root');
    expect(page.getAttribute('style')).toBeNull();
    expect(srcFile('css/energy.css')).toContain('.en-root:not(:empty) { padding: 12px 16px 0; }');
    const html = srcFile('index.html'), at = (s) => html.indexOf('<script src="/static/js/lib/' + s + '"');
    expect(at('manager-view.js')).toBeGreaterThan(at('series.js'));
    expect(at('manager-view.js')).toBeGreaterThan(at('dbstats.js'));
    expect(at('manager-view.js')).toBeGreaterThan(at('overall-view.js'));
  });
  test('Over time gives Energy per step and Tokens generated equal halves', async () => {
    load();
    stubFetch(ROUTES());
    await enter();
    const box = document.querySelector('#en-root section.row[data-row="en-time"] > .panels');
    expect(box.classList.contains('en-halves')).toBe(true);
    expect([...box.children].map(p => p.getAttribute('data-card'))).toEqual(['en-hourly', 'en-tokens']);
    expect(document.querySelectorAll('#en-root .panels.en-halves')).toHaveLength(1);
    expect(srcFile('css/energy.css')).toContain('@media (min-width: 700px) { .en-root .panels.en-halves { grid-template-columns: repeat(2, minmax(0, 1fr)); } }');
  });
  test('the shell refit hook calls enRefit', () => {
    expect(srcFile('js/shell.js')).toMatch(/window\.enRefit\(root\)/);
  });
});
