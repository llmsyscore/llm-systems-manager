// #966: five-state llama pill + stale dimming, with legacy fallback.
import { describe, it, expect } from 'vitest';
import { srcFile, fnSrc, runHarness } from './helpers/harness.js';

const CHARTS = srcFile('js/charts.js');
const BODY = `
  <div id="serverStateBanner" class="state-banner"><span id="serverStateIcon"></span><span id="serverStateText"></span></div>
  <span id="llamaCtrlBadge"></span><span id="llamaServerStatus"></span><span id="llamaPowerPill" hidden></span>
  <button id="llamaBtnPerfPerformance"></button><button id="llamaBtnPerfPowersave"></button><button id="llamaBtnPerfAuto"></button>
  <button id="llamaBtnStart"></button><button id="llamaBtnBuild"></button><div id="llmTab" style="display:none"></div>`;

function boot() {
  const stubs = `
    var _pillModelName = ''; var _lastKnownState = 'unknown'; var _llamaBuildMethod = '';
    window.fetchMetrics = () => {}; window.checkConfig = () => {}; window.refreshLLMTab = () => Promise.resolve();
    window._updateModelPerf = () => {}; window._setLlamaBtns = (on) => { window.__btns = on; };`;
  const fns = ['_cleanLlamaModelName', '_llamaAggregateOf', '_renderLlamaPowerPill', '_applyLlamaStatePayload'].map((n) => {
    const s = fnSrc(CHARTS, n); expect(s, n + ' not found').toBeTruthy(); return s;
  });
  return runHarness({ sources: [stubs, ...fns, 'window._applyLlamaStatePayload = _applyLlamaStatePayload; window._llamaAggregateOf = _llamaAggregateOf;'], bodyHtml: BODY });
}

const text = (w) => w.document.getElementById('serverStateText').textContent;
const banner = (w) => w.document.getElementById('serverStateBanner').className;

describe('llama pill (#966)', () => {
  it('renders the five aggregates', () => {
    const w = boot();
    w._applyLlamaStatePayload({ state: 'awake', aggregate: 'active', model: 'org/m', agent_online: true, stale: false });
    expect(text(w)).toBe('LLCPP · Active · m');
    w._applyLlamaStatePayload({ state: 'awake', aggregate: 'loading', model: 'org/m', agent_online: true, stale: false });
    expect(text(w)).toBe('LLCPP · Loading · m'); expect(banner(w)).toContain('state-loading');
    w._applyLlamaStatePayload({ state: 'sleeping', aggregate: 'sleeping', model: 'org/m', agent_online: true, stale: false });
    expect(text(w)).toBe('LLCPP · Sleeping · m');
    w._applyLlamaStatePayload({ state: 'awake', aggregate: 'idle', model: null, agent_online: true, stale: false });
    expect(text(w)).toBe('LLCPP · Idle'); expect(w.__btns).toBe(true);
    w._applyLlamaStatePayload({ state: 'unknown', aggregate: 'off', model: null, agent_online: true, stale: false });
    expect(text(w)).toBe('LLCPP · Off'); expect(w.__btns).toBe(false);
  });
  it('dims when stale but keeps buttons enabled', () => {
    const w = boot();
    w._applyLlamaStatePayload({ state: 'awake', aggregate: 'active', model: 'm', agent_online: true, stale: true, age_s: 22 });
    expect(banner(w)).toContain('is-stale');
    expect(w.document.getElementById('serverStateBanner').title).toContain('22');
    expect(w.__btns).toBe(true);
  });
  it('shows the applied power profile, owner and outcome', () => {
    const w = boot();
    const pill = () => w.document.getElementById('llamaPowerPill');
    const cur = (id) => w.document.getElementById(id).classList.contains('is-current');
    const base = { state: 'awake', aggregate: 'active', model: 'm', agent_online: true, stale: false };
    w._applyLlamaStatePayload({ ...base, power: { enabled: true, mode: 'full', applied: 'performance', desired: 'performance',
      owner: 'policy', outcome: 'verified', readback: [{ label: 'gpu level', expected: 'auto', actual: 'auto', ok: true },
      { label: 'cpu governor', expected: 'performance', actual: null, ok: null }] } });
    expect(pill().hidden).toBe(false);
    expect(pill().textContent).toBe('⌁ Performance · auto · verified');
    expect(pill().className).toContain('p-active');
    expect(pill().title).toBe('gpu level: auto ✓');
    expect(cur('llamaBtnPerfAuto')).toBe(true); expect(cur('llamaBtnPerfPerformance')).toBe(false);
    w._applyLlamaStatePayload({ ...base, stale: true, power: { enabled: true, mode: 'full', applied: 'powersave', desired: 'powersave',
      owner: 'manual', outcome: 'applied_unverifiable', governor: null, readback: [] } });
    expect(pill().textContent).toBe('☾ Powersave · manual · unverifiable');
    expect(pill().className).toContain('p-sleeping'); expect(pill().className).toContain('is-stale');
    expect(pill().title).toBe('nothing to read back on this host');
    expect(cur('llamaBtnPerfPowersave')).toBe(true); expect(cur('llamaBtnPerfAuto')).toBe(false);
    w._applyLlamaStatePayload({ ...base, power: { enabled: true, mode: 'full', applied: 'powersave', desired: 'performance',
      owner: 'policy', outcome: 'failed', error: 'gpu level reads \'low\' (powersave) after performance',
      readback: [{ label: 'gpu level', expected: 'auto', actual: 'low', ok: false }] } });
    expect(pill().textContent).toBe('☾ Powersave · auto · failed · → performance…');
    expect(pill().className).toContain('p-failed');
    expect(pill().title).toContain('gpu level: low ✗ (want auto)');
    w._applyLlamaStatePayload({ ...base, power: { enabled: false, mode: 'disabled', applied: null, owner: null } });
    expect(pill().textContent).toBe('Power · off'); expect(pill().className).toContain('p-unloaded');
    expect(cur('llamaBtnPerfAuto')).toBe(false);
    w._applyLlamaStatePayload({ ...base, power: null });
    expect(pill().hidden).toBe(true);
  });
  it('falls back to the legacy binary state', () => {
    const w = boot();
    expect(w._llamaAggregateOf({ state: 'awake', model: 'm' })).toBe('active');
    expect(w._llamaAggregateOf({ state: 'awake', model: 'm (unloaded)' })).toBe('idle');
    expect(w._llamaAggregateOf({ state: 'sleeping', model: 'm' })).toBe('sleeping');
    expect(w._llamaAggregateOf({ state: 'unknown' })).toBe('off');
  });
});

// #966 fix round 1: the perf-mode hold must target one host, not the llama pool.
const LLMCTRL = srcFile('js/llmcontrol.js');
const PERF_BODY = '<span id="serverCtrlStatus"></span><button id="llamaBtnPerfAuto"></button>'
  + '<button id="llamaBtnPerfPerformance"></button><button id="llamaBtnPerfPowersave"></button>';

function bootPerf(selected, reply) {
  const src = fnSrc(LLMCTRL, '_perfReadbackWords') + '\n' + fnSrc(LLMCTRL, '_perfNotify') + '\n' + fnSrc(LLMCTRL, 'serverPerfMode');
  expect(fnSrc(LLMCTRL, 'serverPerfMode'), 'serverPerfMode not found').toBeTruthy();
  const body = reply || { ok: true, outcome: 'verified', owner: 'manual', applied: 'performance' };
  const stubs = `
    window.__urls = [];
    window.fetch = (u) => { window.__urls.push(String(u));
      return Promise.resolve({ json: () => Promise.resolve(window.__reply) }); };`;
  const w = runHarness({ sources: [stubs, src, 'window.serverPerfMode = serverPerfMode;'], bodyHtml: PERF_BODY });
  w.__reply = JSON.parse(JSON.stringify(body));
  if (selected !== undefined) w._selectedAgent = () => selected;
  return w;
}

describe('serverPerfMode toasts (#966)', () => {
  it('uses a toast and clears the inline status when the toaster is loaded', async () => {
    const w = bootPerf(null);
    w.__toasts = [];
    w.showToast = (title, body, sev) => w.__toasts.push([title, body, sev]);
    await w.serverPerfMode('auto');
    expect(w.__toasts).toEqual([['Power mode', '\u2713 policy control restored (applied performance)', 'success']]);
    expect(w.document.getElementById('serverCtrlStatus').textContent).toBe('');
  });
});

describe('serverPerfMode agent pinning (#966)', () => {
  it('pins the picker selection so pool round-robin never applies', async () => {
    const w = bootPerf('abc');
    await w.serverPerfMode('performance');
    expect(w.__urls).toEqual(['/api/benchmark/perf-mode?agent=abc']);
  });

  it('falls back to the bare path when nothing is selected', async () => {
    const w = bootPerf(null);
    await w.serverPerfMode('auto');
    expect(w.__urls).toEqual(['/api/benchmark/perf-mode']);
    expect(w.document.getElementById('serverCtrlStatus').textContent)
      .toBe('\u2713 policy control restored (applied performance)');
  });

  it('survives a page with no agent picker at all', async () => {
    const w = bootPerf(undefined);
    await w.serverPerfMode('powersave');
    expect(w.__urls).toEqual(['/api/benchmark/perf-mode']);
  });
});

describe('serverPerfMode unverifiable suffix (#966)', () => {
  const statusOf = (w) => w.document.getElementById('serverCtrlStatus').textContent;

  it('names the third-party governor instead of claiming no cpufreq', async () => {
    const w = bootPerf(null, { ok: true, outcome: 'applied_unverifiable', owner: 'manual',
                               applied: 'performance', governor: 'schedutil' });
    await w.serverPerfMode('performance');
    expect(statusOf(w)).toContain('· unverifiable (governor schedutil)');
    expect(statusOf(w)).not.toContain('no cpufreq');
  });

  it('says nothing to read back when the host has no readable targets', async () => {
    const w = bootPerf(null, { ok: true, outcome: 'applied_unverifiable', owner: 'manual',
                               applied: 'powersave', governor: null, readback: [] });
    await w.serverPerfMode('powersave');
    expect(statusOf(w)).toContain('· unverifiable (nothing to read back)');
  });

  it('lists the read-back values when some were readable', async () => {
    const w = bootPerf(null, { ok: true, outcome: 'applied_unverifiable', owner: 'manual', applied: 'powersave',
                               governor: null, readback: [{ label: 'gpu level', expected: 'low', actual: 'manual', ok: false },
                                                          { label: 'cpu governor', expected: 'powersave', actual: null, ok: null }] });
    await w.serverPerfMode('powersave');
    expect(statusOf(w)).toContain('· unverifiable (gpu level manual)');
  });

  it('adds no suffix when the switch verified', async () => {
    const w = bootPerf(null);
    await w.serverPerfMode('performance');
    expect(statusOf(w)).toBe('✓ performance held by manual · applied performance');
  });
});
