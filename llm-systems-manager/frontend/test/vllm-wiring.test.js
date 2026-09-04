// #125/#567: vLLM frontend wiring — executes the real extracted/injected
// source (jsdom) and asserts on resulting DOM/data, not source-text matches.
import { describe, test, expect, beforeEach } from 'vitest';
import { JSDOM } from 'jsdom';
import OV from '../js/lib/overall-view.js';
import { srcFile, fnSrc as sharedFnSrc, blockSrc, evalGlobal, runHarness, flush } from './helpers/harness.js';

const src = srcFile;

// Same convention as test/overall-adopt.test.js: asserts its own message.
function fnSrc(name, source) {
  const m = sharedFnSrc(source, name);
  expect(m, `${name} not found`).toBeTruthy();
  return m;
}

// ---------------------------------------------------------------------------
describe('foundation.js', () => {
  const foundation = src('js/foundation.js');

  function loadRouting() {
    const code = [
      blockSrc(foundation, 'window._agentsByProvider = window._agentsByProvider', 'vllm: [] };'),
      blockSrc(foundation, 'window._selectedAgents   = window._selectedAgents', 'vllm: null };'),
      blockSrc(foundation, 'const _AGENT_PATH_PROVIDER', '\n];'),
      fnSrc('_providerForApiPath', foundation),
      fnSrc('_selectedAgent', foundation),
      blockSrc(foundation, 'window._withAgentParam = function (url) {', '\n};'),
    ].join('\n');
    evalGlobal(code);
  }

  test('routes /api/vllm/* to the vllm provider for ?agent= injection', () => {
    loadRouting();
    // Default state seeds a vllm bucket (picker/provider-state wiring).
    expect(window._agentsByProvider.vllm).toEqual([]);
    expect(window._selectedAgents.vllm).toBeNull();
    expect(_providerForApiPath('/api/vllm/models')).toBe('vllm');
    expect(_providerForApiPath('/api/lmstudio/models')).toBe('lms');

    window._selectedAgents.vllm = 'agent-9';
    expect(window._withAgentParam('/api/vllm/models')).toBe('/api/vllm/models?agent=agent-9');
    // A different provider's picked agent must not leak onto a vllm path.
    window._selectedAgents.llama = 'agent-llama';
    expect(window._withAgentParam('/api/vllm/models')).toBe('/api/vllm/models?agent=agent-9');
    // No selection → no-op (single-agent installs stay byte-identical).
    window._selectedAgents.vllm = null;
    expect(window._withAgentParam('/api/vllm/models')).toBe('/api/vllm/models');
  });

  test('picker containers and provider state include vllm', () => {
    const code = [
      fnSrc('_esc', foundation),
      fnSrc('_selectedAgent', foundation),
      blockSrc(foundation, 'const _AGENT_PICKER_CONTAINERS', '\n};'),
      fnSrc('_renderAgentPickers', foundation),
    ].join('\n');
    evalGlobal(code);

    document.body.innerHTML = `
      <div class="agent-picker" id="agentPickerDashVllm" style="display:none"></div>
      <div class="agent-picker" id="agentPickerCtrlVllm" style="display:none"></div>`;
    window._agentsByProvider = {
      vllm: [
        { agent_id: 'a1', hostname: 'vllm-host-1', online: true, is_default: true },
        { agent_id: 'a2', hostname: 'vllm-host-2', online: false },
      ],
    };
    window._selectedAgents = { vllm: 'a1' };

    _renderAgentPickers();

    for (const cid of ['agentPickerDashVllm', 'agentPickerCtrlVllm']) {
      const el = document.getElementById(cid);
      expect(el.style.display).not.toBe('none');
      const chips = [...el.querySelectorAll('.agent-chip')];
      expect(chips.length).toBe(2);
      expect(chips[0].classList.contains('active')).toBe(true);
      expect(chips[0].textContent).toContain('vllm-host-1');
      expect(chips[1].classList.contains('offline')).toBe(true);
    }
  });

  test('CARD_LABELS_VLLM drives per-agent provider resolution for vllm cards', () => {
    const code = [
      blockSrc(foundation, 'const CARD_LABELS_LMS', '\n};'),
      blockSrc(foundation, 'const CARD_LABELS_VLLM', '\n};'),
      blockSrc(foundation, 'const CARD_LABELS = {', '\n};'),
      fnSrc('_perAgentProviderForCard', foundation),
    ].join('\n');
    evalGlobal(code);

    expect(_perAgentProviderForCard('vllm-cpu')).toBe('vllm');
    expect(_perAgentProviderForCard('vllm-kv')).toBe('vllm');
    // Contrast: llama/global and LMS cards resolve to their own provider,
    // not vllm — proves the lookup is keyed off CARD_LABELS_VLLM specifically.
    expect(_perAgentProviderForCard('gpu')).toBe('llama');
    expect(_perAgentProviderForCard('lms-cpu')).toBe('lms');
    expect(_perAgentProviderForCard('nope')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
describe('boot.js', () => {
  const boot = src('js/boot.js');

  test('dashboard + llm sub-tab maps include vllm (switchSubTab activates the vllm panel)', async () => {
    document.body.innerHTML = `
      <div id="dash-vllm" class="sub-tab-panel"></div>
      <div id="llm-vllm" class="sub-tab-panel"></div>`;
    const code = [
      blockSrc(boot, "const _subTabState = {", '};'),
      blockSrc(boot, 'const _SUB_TAB_MAP', '\n};'),
      fnSrc('switchSubTab', boot),
      'window._subTabState = _subTabState;',
    ].join('\n');
    evalGlobal(code);

    const calls = [];
    window.stopLogStream = () => calls.push('stopLogStream');
    window.stopPerfRefresh = () => calls.push('stopPerfRefresh');
    window.stopLmsLogRefresh = () => calls.push('stopLmsLogRefresh');
    window.loadVllmHistory = () => { calls.push('loadVllmHistory'); return Promise.resolve(); };
    window.fetchVllmMetrics = () => calls.push('fetchVllmMetrics');

    switchSubTab('dashboard', 'vllm');
    await flush();
    expect(document.getElementById('dash-vllm').classList.contains('active')).toBe(true);
    expect(_subTabState.dashboard).toBe('vllm');
    // Dashboard entry re-backfills then resumes the live poll (#504).
    expect(calls).toContain('loadVllmHistory');
    expect(calls).toContain('fetchVllmMetrics');

    calls.length = 0;
    switchSubTab('llm', 'vllm');
    expect(document.getElementById('llm-vllm').classList.contains('active')).toBe(true);
    expect(_subTabState.llm).toBe('vllm');
    // llm-tab entry has no charts to backfill — plain fetch only.
    expect(calls).toEqual(['stopLogStream', 'stopPerfRefresh', 'stopLmsLogRefresh', 'fetchVllmMetrics']);
  });

  test('boot IIFE kicks off the vllm poller at startup', async () => {
    const stub = `
      window.__calls = [];
      window.setInterval = () => {};
      window.LivePause = { on: false, every: () => {} };
      window.fetchInterval = 5000;
      window.loadLayout = async () => {};
      window.loadMe = async () => {};
      window._loadAgentsByProvider = async () => {};
      window.initSortable = () => {};
      window.syncInterval = async () => {};
      window.loadHistory = async () => {};
      window.loadLmsHistory = () => Promise.resolve();
      window.loadVllmHistory = () => Promise.resolve();
      window.loadManagerPerfHistory = () => Promise.resolve();
      window.checkConfig = async () => {};
      window.pollServerState = () => {};
      window.fetchMetrics = () => {};
      window.startFetching = () => {};
      window.switchTab = () => {};
      window.fetchLMStudioMetrics = () => window.__calls.push('fetchLMStudioMetrics');
      window.fetchVllmMetrics = () => window.__calls.push('fetchVllmMetrics');
      window.fetchServicesAndInflux = () => {};
      window.fetchManagerAgentsCard = () => {};
      window.fetchManagerStreamsCard = () => {};
      window.refreshTabIndicators = () => {};
      window.refreshAlarmRules = () => {};
    `;
    const w = runHarness({ sources: [stub, boot] });
    await new Promise((r) => setTimeout(r, 20));
    expect(w.__calls).toContain('fetchVllmMetrics');
  });
});

// ---------------------------------------------------------------------------
describe('overall.js', () => {
  const overall = src('js/overall.js');

  test('fetches the vllm fleet aggregate and hands it to the paint step', async () => {
    evalGlobal(fnSrc('fetchOverallMetrics', overall) + '\nwindow.fetchOverallMetrics = fetchOverallMetrics;');
    const fetchCalls = [];
    window._ovRefreshEnergy = () => {};
    window._ovPaintBand = (llama, lms, vllm) => { window.__paintedVllm = vllm; };
    window.fetch = async (url) => {
      fetchCalls.push(url);
      const body = url.includes('/vllm/') ? { throughput: { total_tps: 7 } } : {};
      return { ok: true, json: async () => body };
    };
    await window.fetchOverallMetrics();
    expect(fetchCalls).toContain('/api/fleet/vllm/aggregate');
    expect(window.__paintedVllm).toEqual({ throughput: { total_tps: 7 } });
  });

  // #565: tiles render through the OV transforms in the fleet band.
  test('_ovPaintTiles renders the vllm tile built by OV.tiles', () => {
    document.body.innerHTML = '<div id="ovTiles"></div>';
    evalGlobal(fnSrc('_ovPaintTiles', overall) + '\nwindow._ovPaintTiles = _ovPaintTiles;');
    const vllmAgg = {
      agent_count_online: 2, agent_count_total: 3,
      requests_running_total: 4, max_kv_cache_pct: 42,
      throughput: { total_tps: 12.3, total_pps: 4.5 },
    };
    const tiles = OV.tiles(null, null, vllmAgg, {}, Date.now());
    window._ovPaintTiles(tiles);
    const tile = document.querySelector('#ovTiles [data-prov="vllm"]');
    expect(tile).toBeTruthy();
    expect(tile.textContent).toContain('2/3 online');
    expect(tile.textContent).toContain('42%');
  });
});

// ---------------------------------------------------------------------------
describe('charts.js', () => {
  const charts = src('js/charts.js');
  const foundation = src('js/foundation.js');

  test('checkConfig gates the vllm sub-tab buttons + state pill on vllm_present', async () => {
    document.body.innerHTML = `
      <button id="subTabBtnDashVllm"></button>
      <button id="subTabBtnLlmVllm"></button>
      <div id="vllmStateBanner"></div>`;
    evalGlobal(fnSrc('checkConfig', charts) + '\nwindow.checkConfig = checkConfig;');
    window._claim = () => true;
    window._release = () => {};
    window._subTabState = { dashboard: 'manager', llm: 'llamacpp' };
    window.switchTab = () => {};
    window.switchSubTab = () => {};
    window.startFetching = () => {};
    window.fetchInterval = 5000;

    const cfg = { agents: { llama_present: true, lms_present: true, vllm_present: true } };
    window._fetchT = async () => ({ json: async () => cfg });
    await window.checkConfig();
    expect(document.getElementById('subTabBtnDashVllm').style.display).toBe('');
    expect(document.getElementById('subTabBtnLlmVllm').style.display).toBe('');
    expect(document.getElementById('vllmStateBanner').style.display).toBe('');

    // Flip vllm_present off — the same buttons/pill must hide again.
    cfg.agents.vllm_present = false;
    await window.checkConfig();
    expect(document.getElementById('subTabBtnDashVllm').style.display).toBe('none');
    expect(document.getElementById('subTabBtnLlmVllm').style.display).toBe('none');
    expect(document.getElementById('vllmStateBanner').style.display).toBe('none');
  });

  // #366: _activeTabLayoutKeys must resolve the vllm sub-tab to the vllm grid.
  test('_activeTabLayoutKeys resolves the dashboard vllm sub-tab to the vllm grid', () => {
    document.body.innerHTML = `
      <div id="dash-vllm" class="active"></div>
      <div class="grid" id="vllmCardGrid"></div>`;
    const code = [
      blockSrc(foundation, 'const CARD_LABELS_LMS', '\n};'),
      blockSrc(foundation, 'const CARD_LABELS_MANAGER', '\n};'),
      blockSrc(foundation, 'const CARD_LABELS_VLLM', '\n};'),
      blockSrc(foundation, 'const CARD_LABELS = {', '\n};'),
      srcFile('js/lib/settingsdrawer.js'),
      fnSrc('_getDashSubTab', foundation),
      fnSrc('_sdScope', foundation),
      fnSrc('_sdCardMap', foundation),
      fnSrc('_activeTabLayoutKeys', charts),
      'window.CARD_LABELS_VLLM = CARD_LABELS_VLLM;',
    ].join('\n');
    evalGlobal(code);
    window._activeTab = 'dashboard';

    const ks = _activeTabLayoutKeys();
    expect(ks.hidden).toBe('vllmHidden');
    expect(ks.order).toBe('vllmOrder');
    expect(ks.cols).toBe('vllmCols');
    expect(ks.grid).toBe(document.getElementById('vllmCardGrid'));
    expect(ks.map).toBe(CARD_LABELS_VLLM);
  });
});

// ---------------------------------------------------------------------------
// #358: vLLM dashboard history backfill wiring.
describe('vllm history backfill (#358)', () => {
  const charts = src('js/charts.js');

  function loadVllmHistoryHarness() {
    const code = [
      'const MAX_POINTS = 3600;',
      fnSrc('_historyRows', charts),
      fnSrc('_makeHistoryBackfill', charts),
      blockSrc(charts, 'const loadVllmHistory', '\n  });'),
      'window.loadVllmHistory = loadVllmHistory;',
    ].join('\n');
    evalGlobal(code);
  }

  beforeEach(() => {
    window._resetVllmCharts = () => window.__resetCalls.push(Date.now());
    window.__resetCalls = [];
    window.__pushPointCalls = [];
    window.__pushDualCalls = [];
    window.pushPoint = (chart, ts, val) => window.__pushPointCalls.push({ chart, val });
    window.pushDual = (chart, ts, v1, v2) => window.__pushDualCalls.push({ chart, v1, v2 });
    window.vllmKvChart = { name: 'kv' };
    window.vllmTpsChart = { name: 'tps' };
    window._selectedAgent = undefined;
    window.__VLLM_AGENT = 'agent-1';
  });

  test('builds loadVllmHistory from the shared backfill factory and paints kv/tps', async () => {
    loadVllmHistoryHarness();
    const fetchCalls = [];
    window.fetch = async (url) => {
      fetchCalls.push(url);
      return { ok: true, json: async () => [{ ts: 1000, vllm_kv: 55, vllm_tps: 3, vllm_pps: 2 }] };
    };
    await window.loadVllmHistory();
    expect(fetchCalls).toEqual(['/api/history?agent=agent-1']);
    // Reset fires twice on this first-ever call: once because the agent
    // changed from unset (#121), again just before repainting the rows.
    expect(window.__resetCalls.length).toBe(2);
    expect(window.__pushPointCalls).toEqual([{ chart: window.vllmKvChart, val: 55 }]);
    expect(window.__pushDualCalls).toEqual([{ chart: window.vllmTpsChart, v1: 3, v2: 2 }]);
  });

  test('generation guard: only the newest in-flight call paints (++gen)', async () => {
    loadVllmHistoryHarness();
    let resolveFirst;
    let call = 0;
    window.fetch = async () => {
      call += 1;
      if (call === 1) {
        return new Promise((resolve) => { resolveFirst = () => resolve({ ok: true, json: async () => [{ ts: 1, vllm_kv: 1 }] }); });
      }
      return { ok: true, json: async () => [{ ts: 2, vllm_kv: 99 }] };
    };
    const first = window.loadVllmHistory();   // stale call — resolves last
    const second = window.loadVllmHistory();  // newest call — resolves first
    await second;
    resolveFirst();
    await first;
    // Only the second (newest) call's row was painted; the stale first
    // call's late resolution must not overwrite it.
    expect(window.__pushPointCalls).toEqual([{ chart: window.vllmKvChart, val: 99 }]);
  });

  test('boot.js backfills vllm history at startup, then re-backfills on dashboard sub-tab entry',
    () => {
      const boot = src('js/boot.js');
      expect(boot).toContain('loadVllmHistory().catch');
      const entry = boot.slice(boot.indexOf("if (sub === 'vllm')"));
      // wiring (unexecutable): narrow source check on the boot-time call site.
      expect(entry.slice(0, 400)).toContain('loadVllmHistory().finally');
    });

  test('foundation.js agent-switch backfills, resumes the poll, and clears the disk list', () => {
    const foundation = src('js/foundation.js');
    const branch = foundation.slice(foundation.indexOf("provider === 'vllm'"));
    // wiring (unexecutable): branch lives inside a large multi-provider switch.
    expect(branch.slice(0, 600)).toContain('loadVllmHistory().finally');
    // #121 parity with the LMS branch: agent switch clears the disk bar list.
    expect(branch.slice(0, 600)).toContain("_clearBars('vllmDiskList')");
  });

  test('backend maps the vllm history fields and injects __VLLM_AGENT', () => {
    // wiring (unexecutable): Python backend source — no JS/jsdom harness runs it.
    const be = src('../backend/llm-systems-manager.py');
    for (const f of ['vllm_kv', 'vllm_tps', 'vllm_pps', 'vllm_req_running', 'vllm_req_waiting']) {
      expect(be).toContain(`"${f}"`);
    }
    expect(be).toContain('window.__VLLM_AGENT');
  });
});

// ---------------------------------------------------------------------------
describe('admin.js', () => {
  const adminSrc = src('js/admin.js');
  const agentsSrc = src('js/admin-agents.js');

  function runAdminHarness(bootstrap, bodyHtml = '') {
    return runHarness({ sources: [adminSrc, agentsSrc], bootstrap, bodyHtml });
  }

  test('capability chip order renders llama, lms, vllm in that order', () => {
    const boot = `
      window.__T = { html: AgentsView.capsHtml({
        agent_id: 'a1', status: 'approved', is_host_agent: false,
        capabilities: { vllm: true, llama: true, lms: true, sysperf: true },
      }) };
    `;
    const w = runAdminHarness(boot);
    const html = w.__T.html;
    // Order-sensitive: llama's chip text must precede lms's, which precedes vllm's.
    const iLlama = html.indexOf('>llama<');
    const iLms = html.indexOf('>lms<');
    const iVllm = html.indexOf('>vllm<');
    expect(iLlama).toBeGreaterThan(-1);
    expect(iLms).toBeGreaterThan(iLlama);
    expect(iVllm).toBeGreaterThan(iLms);
  });

  // #370: the Agents node detail strip must report the vLLM push age
  // alongside llama/LMS.
  test('Agents detail strip reports the primary vLLM push age', () => {
    const healthSrc = srcFile('js/admin-health.js');
    const boot = `
      window.__T = {};
      const d = { data_flow: {
        primary_llama_push: { has_agent: false },
        primary_lms_push: { has_agent: false },
        primary_vllm_push: { has_agent: true, age_s: 12, ok: true },
      }, agents: [], warnings: [] };
      window.__T.rows = HealthView.detailRows(d, 'agents').map(r => r.join('|')).join('\\n');
      const d2 = JSON.parse(JSON.stringify(d));
      d2.data_flow.primary_vllm_push = { has_agent: false };
      window.__T.rows2 = HealthView.detailRows(d2, 'agents').map(r => r.join('|')).join('\\n');
    `;
    const w = runHarness({ sources: [adminSrc, healthSrc], bootstrap: boot });
    expect(w.__T.rows).toContain('vLLM 12 s');
    expect(w.__T.rows2).toContain('vLLM no agent');
  });
});

// ---------------------------------------------------------------------------
describe('index.html', () => {
  const dom = new JSDOM(src('index.html'));
  const doc = dom.window.document;

  test('has the two vllm sub-tab panels and nav buttons', () => {
    expect(doc.getElementById('dash-vllm')).toBeTruthy();
    expect(doc.getElementById('llm-vllm')).toBeTruthy();
    expect(doc.getElementById('subTabBtnDashVllm')).toBeTruthy();
    expect(doc.getElementById('subTabBtnLlmVllm')).toBeTruthy();
  });

  test('loads js/vllm.js before boot.js', () => {
    const srcs = [...doc.querySelectorAll('script[src]')].map((s) => s.getAttribute('src'));
    const vllmIdx = srcs.findIndex((s) => s.includes('/static/js/vllm.js'));
    const bootIdx = srcs.findIndex((s) => s.includes('/static/js/boot.js'));
    expect(vllmIdx).toBeGreaterThan(-1);
    expect(vllmIdx).toBeLessThan(bootIdx);
  });

  // #364: each vllm chart canvas must sit directly in a .chart-wrap.
  test.each(['vllmKvChart', 'vllmTpsChart'])('%s canvas is a direct child of .chart-wrap', (id) => {
    const canvas = doc.getElementById(id);
    expect(canvas).toBeTruthy();
    expect(canvas.parentElement.classList.contains('chart-wrap')).toBe(true);
  });

  // #366: the vllm control panel must match the llama/LMS toolbar pattern.
  describe('vllm control panel matches llama/LMS UX', () => {
    const panel = doc.getElementById('llm-vllm');

    test('server-control buttons sit in a .llm-toolbar', () => {
      const start = panel.querySelector('#vllmBtnStart');
      expect(start).toBeTruthy();
      expect(start.closest('.llm-toolbar')).toBeTruthy();
    });
    test('uses the muted button palette, not bright green/red/amber', () => {
      expect(panel.querySelectorAll('.btn-green-muted-gradient').length).toBe(0);
      expect(panel.querySelectorAll('.btn-red-muted-gradient').length).toBe(0);
      expect(panel.querySelectorAll('.btn-amber-muted-gradient').length).toBe(0);
    });
    test('section titles use the canonical llm-collapse-icon', () => {
      const titles = panel.querySelectorAll('.llm-section-title');
      expect(titles.length).toBeGreaterThan(0);
      titles.forEach((t) => expect(t.querySelector('.llm-collapse-icon')).toBeTruthy());
      expect(panel.querySelectorAll('.llm-section-arrow').length).toBe(0);
    });
    // #368: full parity with the llama/LMS Server Control.
    test('has Terminal, Server Log, and Server Config buttons', () => {
      expect(panel.querySelector('[onclick="toggleVllmTerminal()"]')).toBeTruthy();
      expect(panel.querySelector("[onclick=\"openServerConfig('vllm')\"]")).toBeTruthy();
      const buttons = [...panel.querySelectorAll('button')].map((b) => b.textContent.replace(/\s+/g, ' ').trim());
      expect(buttons).toContain('≡ Server Log');
    });
    test('terminal panel + mount with the vllm fit key', () => {
      expect(panel.querySelector('#vllmTerminalPanel')).toBeTruthy();
      expect(panel.querySelector('#vllmTerminalMount')).toBeTruthy();
      expect(panel.querySelector('[data-fit-xterm="vllm"]')).toBeTruthy();
      expect(panel.querySelector('[onclick="reconnectVllmTerminal()"]')).toBeTruthy();
    });
    test('log panel has pop-out / fullscreen / refresh toolbar', () => {
      expect(panel.querySelector('[onclick="popOutVllmLog()"]')).toBeTruthy();
      expect(panel.querySelector('[onclick="fullscreenVllmLog()"]')).toBeTruthy();
      expect(panel.querySelector('[onclick="fetchVllmLog()"]')).toBeTruthy();
    });
    test('control badge is seeded as a status pill', () => {
      const badge = panel.querySelector('#vllmCtrlBadge');
      expect(badge).toBeTruthy();
      expect(badge.classList.contains('status')).toBe(true);
      expect(badge.classList.contains('status--crit')).toBe(true);
      expect(badge.querySelector('.status__dot')).toBeTruthy();
    });
  });
});

// ---------------------------------------------------------------------------
describe('#368 vllm control parity wiring', () => {
  test('base.css button override selectors actually match the vllm panel buttons', () => {
    const css = src('css/base.css');
    // Selector text pulled from the stylesheet, executed against real DOM
    // nodes via jsdom's Element.matches(), not just substring-checked.
    const baseSel = [...css.matchAll(/(#llm-vllm \.btn:not\(\[data-act\]\):not\(\[data-lmsact\]\):not\(\.btn-log\)[^,{]*)/g)]
      .map((m) => m[0].trim());
    expect(baseSel.length).toBeGreaterThanOrEqual(2);
    const plain = baseSel[0];
    const hoverSel = baseSel.find((s) => s.includes(':hover'));
    expect(hoverSel).toContain(':not(:disabled)');
    // jsdom can't evaluate a real `:hover` pointer state — strip the
    // pseudo-class to test the meaningful :not(:disabled) part.
    const hoverStructural = hoverSel.replace(/:hover$/, '');

    document.body.innerHTML = `
      <div id="llm-vllm">
        <button class="btn" id="plain">x</button>
        <button class="btn" data-act="start" id="withDataAct">x</button>
        <button class="btn btn-log" id="isLog">x</button>
        <button class="btn" disabled id="disabled">x</button>
      </div>`;
    expect(document.getElementById('plain').matches(plain)).toBe(true);
    expect(document.getElementById('withDataAct').matches(plain)).toBe(false);
    expect(document.getElementById('isLog').matches(plain)).toBe(false);
    // The plain rule still matches a disabled button (only :hover excludes it).
    expect(document.getElementById('disabled').matches(plain)).toBe(true);
    expect(document.getElementById('plain').matches(hoverStructural)).toBe(true);
    expect(document.getElementById('disabled').matches(hoverStructural)).toBe(false);
  });

  test('toggleVllmTerminal opens the panel and starts a PTY session via /api/vllm/terminal/create', async () => {
    const terminal = src('js/terminal.js');
    document.body.innerHTML = `
      <div id="vllmTerminalPanel" style="display:none">
        <div id="vllmTerminalMount"></div>
      </div>`;
    window._vllmTerm = null;
    window._vllmTermSid = null;
    window._vllmTermEvt = null;
    window._vllmTermFit = null;
    const startCalls = [];
    window._vllmTermStart = (mount) => startCalls.push(mount);
    evalGlobal(fnSrc('toggleVllmTerminal', terminal) + '\nwindow.toggleVllmTerminal = toggleVllmTerminal;');

    window.toggleVllmTerminal();
    expect(document.getElementById('vllmTerminalPanel').style.display).toBe('');
    expect(startCalls).toEqual([document.getElementById('vllmTerminalMount')]);

    // Now exercise the real _vllmTermStart against a stubbed Terminal/fetch
    // stack to prove the POST actually targets /api/vllm/terminal/create.
    const fetchCalls = [];
    window.fetch = async (url, opts) => {
      fetchCalls.push({ url, method: opts && opts.method });
      return { ok: true, json: async () => ({ sid: 'sid-1' }) };
    };
    window._jsonOrThrow = async (r) => r.json();
    window._termPostSize = () => {};
    window.EventSource = function (url) { this.url = url; };
    window.EventSource.CLOSED = 2;
    function FakeTerm() { this.rows = 24; this.cols = 80; }
    FakeTerm.prototype.write = function () {};
    FakeTerm.prototype.dispose = function () {};
    FakeTerm.prototype.loadAddon = function () {};
    FakeTerm.prototype.open = function () {};
    FakeTerm.prototype.onData = function () {};
    FakeTerm.prototype.onResize = function () {};
    window.Terminal = FakeTerm;
    window.FitAddon = { FitAddon: function () { this.fit = () => {}; } };
    const code = [
      fnSrc('_vllmTermInit', terminal),
      fnSrc('_vllmTermCloseSession', terminal),
      fnSrc('_vllmTermStart', terminal),
    ].join('\n');
    evalGlobal(code);
    await _vllmTermStart(document.getElementById('vllmTerminalMount'));
    expect(fetchCalls).toEqual([{ url: '/api/vllm/terminal/create', method: 'POST' }]);
    expect(window._vllmTermSid).toBe('sid-1');
  });

  test('popOutVllmTerminal writes a popup that targets /api/vllm/terminal/create', () => {
    const terminal = src('js/terminal.js');
    window._selectedAgent = () => '';
    const written = [];
    window.open = () => ({ document: { write: (h) => written.push(h), close: () => {} } });
    evalGlobal(fnSrc('popOutVllmTerminal', terminal) + '\nwindow.popOutVllmTerminal = popOutVllmTerminal;');
    window.popOutVllmTerminal();
    expect(written.join('')).toContain('/api/vllm/terminal/create');
  });

  test('llmcontrol.js _fitXterm fits the vllm terminal on the vllm key, not others', () => {
    const llmcontrol = src('js/llmcontrol.js');
    // Nested inside a drag-resize IIFE — tailored indentation-aware match.
    const m = llmcontrol.match(/function _fitXterm\(key\) \{[\s\S]*?\n {2}\}/);
    expect(m, '_fitXterm not found').toBeTruthy();
    evalGlobal(m[0] + '\nwindow._fitXterm = _fitXterm;');

    const fits = [];
    window._vllmTermFit = { fit: () => fits.push('vllm') };
    window._termFit = { fit: () => fits.push('llama') };
    window._lmsTermFit = { fit: () => fits.push('lms') };

    window._fitXterm('vllm');
    expect(fits).toEqual(['vllm']);
    window._fitXterm('llama');
    expect(fits).toEqual(['vllm', 'llama']);
  });

  test('backend registers POST /api/vllm/terminal/create routed to the vllm agent', () => {
    // wiring (unexecutable): Flask route registration — no JS/jsdom harness runs it.
    const bt = src('../backend/terminal.py');
    expect(bt).toContain('/api/vllm/terminal/create');
    expect(bt).toContain('_proxy_create("vllm"');
  });
});

// ---------------------------------------------------------------------------
// #373 follow-up: vLLM header state pill (parity with LLCPP/LMS pills).
describe('vllm header state pill', () => {
  function runVllmHarness(fixture, bodyHtml) {
    const stub = `
      // jsdom documents default to hidden (visibilityState 'prerender'),
      // which fetchVllmMetrics treats as "tab backgrounded" and no-ops on.
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
      window._activeTab = 'dashboard';
      window._subTabState = { dashboard: 'vllm', llm: 'llamacpp' };
      window.Chart = function (ctx, cfg) { return { data: cfg.data, update: () => {} }; };
      window.cssVar = () => '#000';
      window._sparkInteraction = {}; window._pctTick = () => {};
      window._sparkTooltip = {}; window._zoomOpts = {};
      window._clearChart = (ch) => { if (ch) { ch.data.labels = []; ch.data.datasets.forEach((d) => d.data = []); } };
      window._agentClaimKey = (base) => base;
      window._claim = () => true; window._release = () => {};
      window._esc = (s) => String(s);
      window._fmtBytes = (b) => (b ? String(b) : '—');
      window._setEl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val ?? '—'; };
      window._dashSetStatus = (id, cls) => { window.__dashCalls.push([id, cls]); };
      window.pushPoint = (chart, ts, val) => window.__pushPointCalls.push({ chart, val });
      window.pushDual = (chart, ts, v1, v2) => window.__pushDualCalls.push({ chart, v1, v2 });
      window.__dashCalls = []; window.__pushPointCalls = []; window.__pushDualCalls = [];
      window._fetchT = () => fetch('/api/vllm/metrics');
      window.fetch = async () => ({ json: async () => (${JSON.stringify(fixture)}) });
    `;
    return runHarness({ sources: [stub, src('js/vllm.js')], bodyHtml });
  }

  test('index.html has the vllm state banner pill', () => {
    const dom = new JSDOM(src('index.html'));
    const doc = dom.window.document;
    expect(doc.getElementById('vllmStateBanner')).toBeTruthy();
    expect(doc.getElementById('vllmStateText')).toBeTruthy();
    expect(doc.getElementById('vllmStateIcon')).toBeTruthy();
  });

  test('fetchVllmMetrics updates the pill text/class from the metrics poll', async () => {
    const w = runVllmHarness(
      { agent_online: true, vllm: { state: 'running', model: 'org/Some-Model-7B' } },
      '<div id="vllmStateBanner"></div><span id="vllmStateText"></span>');
    await w.fetchVllmMetrics();
    const banner = w.document.getElementById('vllmStateBanner');
    const text = w.document.getElementById('vllmStateText');
    expect(banner.className).toBe('state-banner state-awake');
    expect(text.textContent).toBe('VLLM · Active · Some-Model-7B');
  });

  test('fetchVllmMetrics marks the pill offline when the agent is unreachable', async () => {
    const w = runVllmHarness(
      { agent_online: false, vllm: {} },
      '<div id="vllmStateBanner"></div><span id="vllmStateText"></span>');
    await w.fetchVllmMetrics();
    expect(w.document.getElementById('vllmStateBanner').className).toBe('state-banner state-unknown');
    expect(w.document.getElementById('vllmStateText').textContent).toBe('VLLM · offline');
  });

  test('charts.js checkConfig toggles vllmStateBanner visibility with vllm presence', () => {
    // Covered behaviorally in the charts.js "checkConfig gates..." test above.
    const charts = src('js/charts.js');
    expect(charts).toMatch(/toggle\('vllmStateBanner',\s*vllmOn\)/);
  });
});

// ---------------------------------------------------------------------------
// #411: vLLM tab surfaces the host system metrics, mirroring LM Studio.
describe('vllm host system metrics (#411)', () => {
  const dom = new JSDOM(src('index.html'));
  const doc = dom.window.document;

  test('index.html has the five host system cards', () => {
    for (const c of ['vllm-cpu', 'vllm-ram', 'vllm-network', 'vllm-disk', 'vllm-io']) {
      expect(doc.querySelector(`[data-card="${c}"]`)).toBeTruthy();
    }
  });
  test('index.html has the host metric element ids', () => {
    for (const id of ['vllm-cpu-total', 'vllm-cpu-temp', 'vllm-cpu-governor', 'vllmCoreGrid',
                      'vllm-ram-pct', 'vllm-ram-sub', 'vllm-ram-cached', 'vllm-ram-buffers',
                      'vllm-swap-used', 'vllm-swap-free', 'vllm-net-sent', 'vllm-net-recv',
                      'vllmDiskList', 'vllm-io-read', 'vllm-io-write']) {
      expect(doc.getElementById(id)).toBeTruthy();
    }
  });
  // #364: each chart canvas must sit directly in a height-constrained .chart-wrap.
  test.each(['vllmCpuChart', 'vllmRamChart', 'vllmNetChart', 'vllmIoChart', 'vllmDiskUsageChart'])(
    '%s canvas is a direct child of .chart-wrap', (id) => {
      const canvas = doc.getElementById(id);
      expect(canvas).toBeTruthy();
      expect(canvas.parentElement.classList.contains('chart-wrap')).toBe(true);
    });

  function runVllmHarness(fixture) {
    const bodyHtml = `
      <div id="vllm-cpu-total"></div><div id="vllm-cpu-temp"></div><div id="vllm-cpu-governor"></div>
      <div id="vllmCoreGrid"></div>
      <div id="vllm-ram-pct"></div><div id="vllm-ram-sub"></div>
      <div id="vllm-ram-cached"></div><div id="vllm-ram-buffers"></div>
      <div id="vllm-swap-used"></div><div id="vllm-swap-free"></div>
      <div id="vllm-net-sent"></div><div id="vllm-net-recv"></div>
      <div id="vllmDiskList"></div><div id="vllm-io-read"></div><div id="vllm-io-write"></div>
      <div id="vllm-active-model"></div><div id="vllm-active-state"></div>
      <div id="vllm-req-running"></div><div id="vllm-req-waiting"></div>
      <div id="vllm-kv-pct"></div><div id="vllm-tps"></div><div id="vllm-pps"></div>
      <div id="vllm-dash-badge"></div><div id="vllmCtrlBadge"></div>
      <canvas id="vllmKvChart"></canvas><canvas id="vllmTpsChart"></canvas>
      <canvas id="vllmCpuChart"></canvas><canvas id="vllmRamChart"></canvas>
      <canvas id="vllmNetChart"></canvas><canvas id="vllmIoChart"></canvas>
      <canvas id="vllmDiskUsageChart"></canvas>
    `;
    const stub = `
      // jsdom documents default to hidden (visibilityState 'prerender'),
      // which fetchVllmMetrics treats as "tab backgrounded" and no-ops on.
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
      // jsdom has no real canvas backend — stub a truthy getContext().
      window.HTMLCanvasElement.prototype.getContext = function () { return {}; };
      window._activeTab = 'dashboard';
      window._subTabState = { dashboard: 'vllm', llm: 'llamacpp' };
      window.Chart = function (ctx, cfg) { return { data: cfg.data, update: () => {} }; };
      window.cssVar = () => '#000';
      window._sparkInteraction = {}; window._pctTick = () => {};
      window._sparkTooltip = {}; window._zoomOpts = {};
      window._clearChart = (ch) => { if (ch) { ch.data.labels = []; ch.data.datasets.forEach((d) => d.data = []); } };
      window._agentClaimKey = (base) => base;
      window._claim = () => true; window._release = () => {};
      window._esc = (s) => String(s);
      window._fmtBytes = (b) => (b ? String(b) : '—');
      window._setEl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val ?? '—'; };
      window.__dashCalls = [];
      window._dashSetStatus = (id, cls) => { window.__dashCalls.push([id, cls]); };
      window.__pushPointCalls = []; window.__pushDualCalls = [];
      window.pushPoint = (chart, ts, val) => window.__pushPointCalls.push({ chart, val });
      window.pushDual = (chart, ts, v1, v2) => window.__pushDualCalls.push({ chart, v1, v2 });
      window._fetchT = () => fetch('/api/vllm/metrics');
      window.fetch = async () => ({ json: async () => (${JSON.stringify(fixture)}) });
    `;
    const exposeCharts = 'window.vllmCpuChart = vllmCpuChart; window.vllmRamChart = vllmRamChart; window.vllmNetChart = vllmNetChart;';
    return runHarness({ sources: [stub, src('js/vllm.js'), exposeCharts], bodyHtml });
  }

  test('vllm.js reads the host system block from the metrics payload', async () => {
    const w = runVllmHarness({
      agent_online: true,
      vllm: { state: 'running' },
      system: {
        cpu_total: 41.2, cpu_temp_c: 55.5, cpu_governor: 'performance', cpu_per_core: [10, 90],
        ram: { percent: 62.5, used_bytes: 2e9, total_bytes: 4e9 },
        net: { bytes_sent_per_sec: 1048576, bytes_recv_per_sec: 2097152 },
        disk: [{ mountpoint: '/', percent: 33.3, total_bytes: 2e11 }],
        disk_io: { read_bytes_per_sec: 1048576, write_bytes_per_sec: 2097152 },
      },
    });
    await w.fetchVllmMetrics();
    expect(w.document.getElementById('vllm-cpu-total').textContent).toBe('41.2%');
    expect(w.document.getElementById('vllm-ram-pct').textContent).toBe('62.5%');
    expect(w.document.getElementById('vllm-net-sent').textContent).toBe('1.00');
    expect(w.document.getElementById('vllmCoreGrid').textContent).toContain('C0');
    expect(w.document.getElementById('vllmDiskList').innerHTML).toContain('33.3%');
  });

  test('vllm.js defines and resets the three host charts', async () => {
    const w = runVllmHarness({ agent_online: true, vllm: {}, system: {} });
    expect(w.vllmCpuChart).toBeTruthy();
    expect(w.vllmRamChart).toBeTruthy();
    expect(w.vllmNetChart).toBeTruthy();
    w.vllmCpuChart.data.datasets[0].data = [1, 2, 3];
    w.vllmCpuChart.data.labels = ['a', 'b'];
    w._resetVllmCharts();
    // #504: reset clears labels along with datasets (via the real _clearChart).
    expect(w.vllmCpuChart.data.datasets[0].data).toEqual([]);
    expect(w.vllmCpuChart.data.labels).toEqual([]);
  });

  // #504 parity with fetchLMStudioMetrics: fetch failures are logged, not swallowed silently.
  test('fetchVllmMetrics logs fetch errors via console.warn', async () => {
    const w = runVllmHarness({});
    w.fetch = async () => { throw new Error('boom'); };
    const warnings = [];
    w.console.warn = (...args) => warnings.push(args);
    await w.fetchVllmMetrics();
    expect(warnings.length).toBe(1);
    expect(warnings[0][0]).toBe('fetchVllmMetrics:');
  });

  test('vllm.js sets accent status on the new host cards', async () => {
    const w = runVllmHarness({
      agent_online: true, vllm: { state: 'running' },
      system: { cpu_total: 95, ram: { percent: 50 }, net: {}, disk: [] },
    });
    await w.fetchVllmMetrics();
    const byId = Object.fromEntries(w.__dashCalls);
    expect(byId['vllm-cpu']).toBe('dash-crit');   // 95% >= 90 threshold
    expect(byId['vllm-ram']).toBe('dash-ok');
    expect(byId['vllm-network']).toBe('dash-ok');
    expect(byId['vllm-disk']).toBe('dash-ok');
    expect(byId['vllm-io']).toBe('dash-off');     // no disk_io in the payload
  });

  test('foundation.js registers labels for the new host cards', () => {
    const foundation = src('js/foundation.js');
    evalGlobal(blockSrc(foundation, 'const CARD_LABELS_VLLM', '\n};') + '\nwindow.CARD_LABELS_VLLM = CARD_LABELS_VLLM;');
    for (const c of ['vllm-cpu', 'vllm-ram', 'vllm-network', 'vllm-disk']) {
      expect(Object.prototype.hasOwnProperty.call(CARD_LABELS_VLLM, c)).toBe(true);
    }
  });
});

describe('agent forwards the system block for vLLM (#411)', () => {
  test('_push_vllm_payload includes the system block', () => {
    // wiring (unexecutable): Python agent source — no JS/jsdom harness runs it.
    const agent = src('../../agent/llm-systems-agent.py');
    const fn = agent.slice(agent.indexOf('def _push_vllm_payload'));
    const body = fn.slice(0, fn.indexOf('\n\n\n'));
    expect(body).toContain('"system": sample.get("system")');
  });
});
