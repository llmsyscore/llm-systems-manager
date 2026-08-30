// #506: views whose live poll is view-gated must re-backfill their charts on
// entry. Executes the real switchSubTab/switchTab/backfill sources in jsdom
// with stubbed collaborators, rather than pattern-matching the source text.
import { describe, test, expect, vi } from 'vitest';
import { srcFile, fnSrc, evalGlobal, loadSwitchSubTab as loadSharedSwitchSubTab, flush as tick } from './helpers/harness.js';
import LMPeaks from '../js/lib/peaks.js';

const boot = srcFile('js/boot.js');
const foundation = srcFile('js/foundation.js');
const overall = srcFile('js/overall.js');
const charts = srcFile('js/charts.js');

function loadSwitchSubTab() {
  loadSharedSwitchSubTab(boot);
}

function loadSwitchTab() {
  const code = [fnSrc(foundation, 'switchTab'), 'window.switchTab = switchTab;'].join('\n');
  evalGlobal(code);
}

function loadMakeHistoryBackfill() {
  const code = [fnSrc(charts, '_makeHistoryBackfill'), 'window._makeHistoryBackfill = _makeHistoryBackfill;'].join('\n');
  evalGlobal(code);
}

function switchTabDom() {
  document.body.innerHTML = `
    <button class="tab-btn" onclick="switchTab('overall')"></button>
    <div id="overallTab"></div>`;
}

describe('LM Studio sub-tab entry (#506)', () => {
  test('dashboard entry backfills history before resuming the live poll, and the live poll still resumes if the backfill fails', async () => {
    loadSwitchSubTab();
    window._lmsLogOpen = false;
    window._initLMSSections = vi.fn();
    const order = [];
    window.loadLmsHistory = vi.fn(() => { order.push('backfill'); return Promise.reject(new Error('ae down')); });
    window.fetchLMStudioMetrics = vi.fn(() => order.push('live'));

    switchSubTab('dashboard', 'lmstudio');
    await tick(); await tick();

    expect(order).toEqual(['backfill', 'live']);
  });

  test('backfill is gated to the dashboard parent — the llm-tab panel fetches directly', () => {
    loadSwitchSubTab();
    window._lmsLogOpen = false;
    window._initLMSSections = vi.fn();
    window.stopLogStream = vi.fn();
    window.stopPerfRefresh = vi.fn();
    window.loadLmsHistory = vi.fn();
    window.fetchLMStudioMetrics = vi.fn();

    switchSubTab('llm', 'lmstudio');

    expect(window.loadLmsHistory).not.toHaveBeenCalled();
    expect(window.fetchLMStudioMetrics).toHaveBeenCalledTimes(1);
  });
});

describe('Manager sub-tab entry (#506)', () => {
  test('re-backfills the perf sparklines on every entry — no one-shot latch gates it out', () => {
    loadSwitchSubTab();
    window.fetchServicesAndInflux = vi.fn();
    window.fetchManagerAgentsCard = vi.fn();
    window.fetchManagerStreamsCard = vi.fn();
    window.stopLmsLogRefresh = vi.fn();
    window.loadManagerPerfHistory = vi.fn(() => Promise.resolve());

    switchSubTab('dashboard', 'manager');
    switchSubTab('dashboard', 'manager');

    expect(window.loadManagerPerfHistory).toHaveBeenCalledTimes(2);
  });
});

describe('Overall tab entry (#506)', () => {
  test('switchTab overall backfills before the live fetch, and resumes it even if the backfill fails', async () => {
    switchTabDom();
    loadSwitchTab();
    window._activeTab = 'dashboard';
    window._me = { admin_access: true };
    window.adminStopAutoRefresh = vi.fn();
    window.stopLogStream = vi.fn();
    window.stopPerfRefresh = vi.fn();
    window.stopLmsLogRefresh = vi.fn();
    window._ovBackfillPinnedProviders = vi.fn();
    const order = [];
    window.loadOverallHistory = vi.fn(() => { order.push('backfill'); return Promise.reject(new Error('ae down')); });
    window.fetchOverallMetrics = vi.fn(() => order.push('live'));

    switchTab('overall');
    await tick(); await tick();

    expect(order).toEqual(['backfill', 'live']);
  });

  test('re-backfills on every entry — no one-shot latch gates it out', async () => {
    switchTabDom();
    loadSwitchTab();
    window._activeTab = 'dashboard';
    window._me = { admin_access: true };
    window.adminStopAutoRefresh = vi.fn();
    window.stopLogStream = vi.fn();
    window.stopPerfRefresh = vi.fn();
    window.stopLmsLogRefresh = vi.fn();
    window._ovBackfillPinnedProviders = vi.fn();
    window.loadOverallHistory = vi.fn(() => Promise.resolve());
    window.fetchOverallMetrics = vi.fn();

    switchTab('overall');
    await tick();
    switchTab('overall');
    await tick();

    expect(window.loadOverallHistory).toHaveBeenCalledTimes(2);
  });

  test('fetchOverallMetrics no longer backfills on the live path', async () => {
    const code = [fnSrc(overall, 'fetchOverallMetrics'), 'window.fetchOverallMetrics = fetchOverallMetrics;'].join('\n');
    evalGlobal(code);
    window._ovRefreshEnergy = vi.fn();
    window._ovPaintBand = vi.fn();
    window.fetch = vi.fn(async () => ({ ok: false }));
    window.loadOverallHistory = vi.fn();

    await window.fetchOverallMetrics();

    expect(window.loadOverallHistory).not.toHaveBeenCalled();
    expect(window._ovPaintBand).toHaveBeenCalledTimes(1);
  });
});

describe('_makeHistoryBackfill pre-fetch clear (#507)', () => {
  test('clears before the fetch only when the selected agent changed', async () => {
    loadMakeHistoryBackfill();
    window.MAX_POINTS = 3600;
    window._selectedAgent = () => 'agent-A';
    const resetCharts = vi.fn();
    let deferred;
    window._historyRows = () => new Promise(res => { deferred = res; });
    const backfill = window._makeHistoryBackfill('llama', '__DEF_AGENT', resetCharts, vi.fn());

    const run = backfill();                       // first call: agent undefined -> 'agent-A'
    expect(resetCharts).toHaveBeenCalledTimes(1);  // pre-fetch clear fires on agent change
    deferred([]);
    await run;
  });

  // An unconditional pre-fetch reset blanks the window when the fetch fails.
  test('has no unconditional pre-fetch resetCharts call', async () => {
    loadMakeHistoryBackfill();
    window.MAX_POINTS = 3600;
    window._selectedAgent = () => 'agent-A';
    const resetCharts = vi.fn();
    window._historyRows = () => Promise.resolve([]);
    const backfill = window._makeHistoryBackfill('llama', '__DEF_AGENT', resetCharts, vi.fn());
    await backfill();                              // prime lastAgent = 'agent-A'
    resetCharts.mockClear();

    let deferred;
    window._historyRows = () => new Promise(res => { deferred = res; });
    const run = backfill();                        // same agent as last time
    expect(resetCharts).not.toHaveBeenCalled();     // no clear before the fetch settles (#507 fix)
    deferred([]);
    await run;
  });

  test('still repaints from a clean slate once rows arrive', async () => {
    loadMakeHistoryBackfill();
    window.LMPeaks = LMPeaks;
    window.MAX_POINTS = 3600;
    window._selectedAgent = () => 'agent-A';
    const resetCharts = vi.fn();
    const paintRow = vi.fn();
    window._historyRows = () => Promise.resolve([]);
    const backfill = window._makeHistoryBackfill('llama', '__DEF_AGENT', resetCharts, paintRow);
    await backfill();                               // prime lastAgent, no rows yet
    resetCharts.mockClear();

    const rows = [{ ts: 1 }, { ts: 2 }];
    window._historyRows = () => Promise.resolve(rows);
    await backfill();

    expect(resetCharts).toHaveBeenCalledTimes(1);
    expect(paintRow).toHaveBeenNthCalledWith(1, rows[0], expect.any(Function));
    expect(paintRow).toHaveBeenNthCalledWith(2, rows[1], expect.any(Function));
  });
});

// An auth-gated 401 answers with a JSON object, so r.json() resolves and a
// bare rows.length check skips the repaint with no error anywhere (#507).
describe('history fetches detect non-array responses (#507)', () => {
  function loadHistoryRows() {
    const code = [fnSrc(charts, '_historyRows'), 'window._historyRows = _historyRows;'].join('\n');
    evalGlobal(code);
  }

  test('_historyRows rejects a non-ok response and logs it', async () => {
    loadHistoryRows();
    window.fetch = vi.fn(async () => ({ ok: false, status: 401 }));
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const rows = await window._historyRows('/api/history', 'llama');

    expect(rows).toBeNull();
    expect(errSpy).toHaveBeenCalledWith('llama history: HTTP 401');
    errSpy.mockRestore();
  });

  test('_historyRows rejects a non-array payload and logs it', async () => {
    loadHistoryRows();
    const payload = { auth_required: true };
    window.fetch = vi.fn(async () => ({ ok: true, json: async () => payload }));
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const rows = await window._historyRows('/api/history', 'llama');

    expect(rows).toBeNull();
    expect(errSpy).toHaveBeenCalledWith('llama history: expected an array, got', payload);
    errSpy.mockRestore();
  });
});

describe('history-backfill callers route through _historyRows, not raw fetch (#507)', () => {
  test('loadHistory', async () => {
    const code = [fnSrc(charts, 'loadHistory'), 'window.loadHistory = loadHistory;'].join('\n');
    evalGlobal(code);
    window._histGen = 0;
    window._histLastAgent = undefined;
    window._selectedAgent = () => null;
    window._resetMetricCharts = vi.fn();
    window._llamaPeaks = { tps: { reset: vi.fn() }, pps: { reset: vi.fn() } };
    window._historyRows = vi.fn(async () => []);   // empty rows: returns before touching any chart
    window.fetch = vi.fn();

    await window.loadHistory();

    expect(window._historyRows).toHaveBeenCalledWith('/api/history', 'llama');
    expect(window.fetch).not.toHaveBeenCalled();
  });

  test('_makeHistoryBackfill', async () => {
    loadMakeHistoryBackfill();
    window.MAX_POINTS = 3600;
    window._selectedAgent = () => 'agent-A';
    window._historyRows = vi.fn(async () => []);
    window.fetch = vi.fn();
    const backfill = window._makeHistoryBackfill('llama', '__DEF_AGENT', vi.fn(), vi.fn());

    await backfill();

    expect(window._historyRows).toHaveBeenCalledWith('/api/history?agent=agent-A', 'llama');
    expect(window.fetch).not.toHaveBeenCalled();
  });

  test('loadOverallHistory', async () => {
    const code = [fnSrc(charts, 'loadOverallHistory'), 'window.loadOverallHistory = loadOverallHistory;'].join('\n');
    evalGlobal(code);
    window._ovHistoryGen = 0;
    window.ovHeroChart = {};   // truthy: past the early-return guard
    window._historyRows = vi.fn(async () => []);
    window.fetch = vi.fn();

    await window.loadOverallHistory();

    expect(window._historyRows).toHaveBeenCalledWith(
      '/api/history?since_minutes=1440&max_rows=1440&fleet=all', 'Overall fleet');
    expect(window.fetch).not.toHaveBeenCalled();
  });

  test.each(['loadHistory', '_makeHistoryBackfill', 'loadOverallHistory'])(
    '%s body has no direct fetch call', (name) => {
      // wiring (unexecutable): absence must hold in branches no test drives.
      const body = fnSrc(charts, name);
      expect(body).toBeTruthy();
      expect(body).not.toMatch(/\bfetch\(/);
    });
});

// llama.cpp is excluded because fetchMetrics has no view gate (#129); adding
// one there would require an entry backfill too.
describe('llama.cpp needs no entry backfill (#506)', () => {
  test('fetchMetrics still polls even when the llama.cpp view is not the active one', async () => {
    const code = [fnSrc(charts, 'fetchMetrics'), 'window.fetchMetrics = fetchMetrics;'].join('\n');
    evalGlobal(code);
    window._agentClaimKey = () => 'fetchMetrics:';
    window._claim = () => true;
    window._release = vi.fn();
    window._fetchT = vi.fn(() => Promise.reject(new Error('network down')));
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    // Operator parked on a completely different tab/sub-tab — unlike
    // lmstudio/vllm/manager, fetchMetrics must still fire (#129).
    window._activeTab = 'admin';
    window._subTabState = { dashboard: 'manager' };

    await window.fetchMetrics();

    expect(window._fetchT).toHaveBeenCalledWith('/api/metrics', {}, 10000);
    errSpy.mockRestore();
  });
});
