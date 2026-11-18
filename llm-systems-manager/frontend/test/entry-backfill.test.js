// #506: views whose live poll is view-gated must re-backfill their charts on
// entry. Executes the real switchSubTab/switchTab/backfill sources in jsdom
// with stubbed collaborators, rather than pattern-matching the source text.
import { describe, test, expect, vi } from 'vitest';
import { srcFile, fnSrc, evalGlobal, loadSwitchSubTab as loadSharedSwitchSubTab, flush as tick } from './helpers/harness.js';
import LMPeaks from '../js/lib/peaks.js';
import OV from '../js/lib/overall-view.js';

const boot = srcFile('js/boot.js');
const foundation = srcFile('js/foundation.js');
const overall = srcFile('js/overall.js');
const kit = srcFile('kit/kit.js');
const charts = srcFile('js/charts.js');

function loadSwitchSubTab() {
  loadSharedSwitchSubTab(boot);
}

function loadSwitchTab() {
  const code = [fnSrc(foundation, 'switchTab'), 'window.switchTab = switchTab;'].join('\n');
  evalGlobal(code);
}

// charts.js's real loadHistory with its generation and last-agent state as window vars.
function loadLoadHistory() {
  evalGlobal([fnSrc(charts, 'loadHistory'), 'window.loadHistory = loadHistory;'].join('\n'));
  window._histGen = 0;
  window._histLastAgent = undefined;
  window._LLAMA_PEAK_WINDOW_MS = 3600000;
  window.LMPeaks = LMPeaks;
}

function switchTabDom() {
  document.body.innerHTML = `
    <aside id="side"><button class="nav" id="tabBtnOverall" data-tab="overall"></button></aside>
    <div id="overallTab"></div>`;
}

describe('LM Studio sub-tab entry (#506)', () => {
  // #1432: the LM Studio host page loads its own history; the sub-tab only refreshes the live payload.
  test('dashboard entry fetches the live payload at once and backfills no chart history', async () => {
    loadSwitchSubTab();
    window._lmsLogOpen = false;
    window._initLMSSections = vi.fn();
    window.fetchLMStudioMetrics = vi.fn();

    switchSubTab('dashboard', 'lmstudio');
    await tick(); await tick();

    expect(window.fetchLMStudioMetrics).toHaveBeenCalledTimes(1);
  });

  test('the llm-tab panel fetches directly', () => {
    loadSwitchSubTab();
    window._lmsLogOpen = false;
    window._initLMSSections = vi.fn();
    window.stopLogStream = vi.fn();
    window.stopPerfRefresh = vi.fn();
    window.fetchLMStudioMetrics = vi.fn();

    switchSubTab('llm', 'lmstudio');

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
  test('switchTab overall paints the live band at once and backfills alongside it, even if the backfill fails (#1140)', async () => {
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
    window.ovLoad = vi.fn(() => { order.push('backfill'); return Promise.reject(new Error('ae down')); });
    window.fetchOverallMetrics = vi.fn(() => order.push('live'));

    switchTab('overall');

    expect(order).toEqual(['live', 'backfill']);   // both fire synchronously on entry
    await tick(); await tick();
    expect(window.fetchOverallMetrics).toHaveBeenCalledTimes(1);
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
    window.ovLoad = vi.fn(() => Promise.resolve());
    window.fetchOverallMetrics = vi.fn();

    switchTab('overall');
    await tick();
    switchTab('overall');
    await tick();

    expect(window.ovLoad).toHaveBeenCalledTimes(2);
  });

  test('fetchOverallMetrics never fetches history on the live path', async () => {
    document.body.innerHTML = '<div id="overallTab"></div>';
    window._activeTab = 'overall';
    window.OV = OV;
    evalGlobal(kit);
    evalGlobal(overall);
    window.fetch = vi.fn(async () => ({ ok: false }));

    await window.fetchOverallMetrics();

    const urls = window.fetch.mock.calls.map(c => c[0]);
    expect(urls).toContain('/api/fleet/llama/aggregate');
    expect(urls.some(u => u.startsWith('/api/history'))).toBe(false);
  });
});

describe('loadHistory peak seeding (#507, #1432)', () => {
  // A tracker stub that records its pushes and resets.
  const tracker = () => ({ pushes: [], resets: 0, push(t, v) { this.pushes.push(v); }, reset() { this.resets++; this.pushes = []; } });

  test('resets the llama peaks before the fetch only when the selected agent changed', async () => {
    loadLoadHistory();
    window._llamaPeaks = { tps: tracker(), pps: tracker() };
    let agent = 'agent-A', deferred;
    window._selectedAgent = () => agent;
    window._historyRows = () => new Promise(res => { deferred = res; });

    let run = window.loadHistory();
    expect(window._llamaPeaks.tps.resets).toBe(1);
    deferred([]);
    await run;

    run = window.loadHistory();
    expect(window._llamaPeaks.tps.resets).toBe(1);
    deferred([]);
    await run;

    agent = 'agent-B';
    run = window.loadHistory();
    expect(window._llamaPeaks.pps.resets).toBe(2);
    deferred([]);
    await run;
  });

  test('seeds both trackers from the rows; an older call that answers late seeds nothing', async () => {
    loadLoadHistory();
    window._llamaPeaks = { tps: tracker(), pps: tracker() };
    window._selectedAgent = () => 'agent-A';
    const answers = [];
    window._historyRows = () => new Promise(res => answers.push(res));
    const first = window.loadHistory();
    const second = window.loadHistory();
    const now = Date.now();
    answers[1]([{ ts: new Date(now - 60000).toISOString(), llama_tps: 5, llama_pps: 50 }, { ts: new Date(now).toISOString(), llama_tps: 7, llama_pps: 70 }]);
    await second;
    answers[0]([{ ts: new Date(now).toISOString(), llama_tps: 99, llama_pps: 99 }]);
    await first;

    expect(window._llamaPeaks.tps.pushes).toEqual([5, 7]);
    expect(window._llamaPeaks.pps.pushes).toEqual([50, 70]);
  });

  test('charts.js has no LM Studio or vLLM chart backfill left; their host pages load the history', () => {
    // wiring (unexecutable): absence check across the whole file.
    expect(charts).not.toMatch(/loadLmsHistory|loadVllmHistory/);
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
  test('loadHistory asks for the last hour, of the picked host when there is one', async () => {
    loadLoadHistory();
    let agent = null;
    window._selectedAgent = () => agent;
    window._llamaPeaks = { tps: { reset: vi.fn() }, pps: { reset: vi.fn() } };
    window._historyRows = vi.fn(async () => []);   // empty rows: returns before seeding
    window.fetch = vi.fn();

    await window.loadHistory();
    agent = 'agent-A';
    await window.loadHistory();

    expect(window._historyRows).toHaveBeenNthCalledWith(1, '/api/history?since_minutes=60', 'llama');
    expect(window._historyRows).toHaveBeenNthCalledWith(2, '/api/history?agent=agent-A&since_minutes=60', 'llama');
    expect(window.fetch).not.toHaveBeenCalled();
  });

  test.each(['loadHistory'])(
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
