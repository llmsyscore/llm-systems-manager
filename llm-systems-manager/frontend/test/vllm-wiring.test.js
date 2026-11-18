// #125/#567: vLLM frontend wiring — executes the real extracted/injected
// source (jsdom) and asserts on resulting DOM/data, not source-text matches.
import { describe, test, expect, beforeEach } from 'vitest';
import { JSDOM } from 'jsdom';
import OV from '../js/lib/overall-view.js';
import HP from '../js/lib/host-view.js';
// Sets window.LMPeaks, which charts.js's history backfill reads.
import '../js/lib/peaks.js';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { srcFile, fnSrc as sharedFnSrc, blockSrc, evalGlobal, runHarness, flush } from './helpers/harness.js';

const src = srcFile;
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

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
      'window._AGENT_PICKER_CONTAINERS = _AGENT_PICKER_CONTAINERS;',
    ].join('\n');
    evalGlobal(code);

    document.body.innerHTML = `
      <div class="agent-picker" id="agentPickerCtrlVllm" style="display:none"></div>`;
    window._agentsByProvider = {
      vllm: [
        { agent_id: 'a1', hostname: 'vllm-host-1', online: true, is_default: true },
        { agent_id: 'a2', hostname: 'vllm-host-2', online: false },
      ],
    };
    window._selectedAgents = { vllm: 'a1' };

    _renderAgentPickers();

    // The vLLM dashboard picks its host in the host page list (#1432); only LLM Control keeps chips.
    expect(window._AGENT_PICKER_CONTAINERS.vllm).toEqual(['agentPickerCtrlVllm']);
    for (const cid of ['agentPickerCtrlVllm']) {
      const el = document.getElementById(cid);
      expect(el.style.display).not.toBe('none');
      const chips = [...el.querySelectorAll('.agent-chip')];
      expect(chips.length).toBe(2);
      expect(chips[0].classList.contains('active')).toBe(true);
      expect(chips[0].textContent).toContain('vllm-host-1');
      expect(chips[1].classList.contains('offline')).toBe(true);
    }
  });

  test('CARD_LABELS_VLLM lists the vLLM host page panels with their titles (#1432)', () => {
    window.HP = HP;
    evalGlobal(blockSrc(foundation, '// Host page panel ids and titles', '\nconst CARD_LABELS_MANAGER', { includeEnd: false }).replace(/^const /gm, 'var ')
      + '\n_hostPanelLabels();');
    expect(Object.keys(window.CARD_LABELS_VLLM)).toEqual(HP.panelsFor('vllm').map(p => 'vllm-' + p.name));
    expect(window.CARD_LABELS_VLLM['vllm-kv']).toBe('KV cache');
    expect(window.CARD_LABELS_VLLM['vllm-cpu-mem']).toBe('CPU and memory');
    // The lookup is keyed per page: llama and LM Studio panels stay in their own maps.
    expect(window.CARD_LABELS_VLLM['llama-gpu-busy']).toBeUndefined();
    expect(window.CARD_LABELS['llama-gpu-busy']).toBe('GPU busy and memory');
    expect(window.CARD_LABELS_LMS['lms-models']).toBe('Models');
    expect(Object.keys(window.CARD_LABELS_HOST)).toEqual(HP.panelsFor('host').map(p => 'host-' + p.name));
    expect(window.CARD_LABELS_HOST['host-tps']).toBeUndefined();
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
    window.fetchVllmMetrics = () => calls.push('fetchVllmMetrics');

    switchSubTab('dashboard', 'vllm');
    await flush();
    expect(document.getElementById('dash-vllm').classList.contains('active')).toBe(true);
    expect(_subTabState.dashboard).toBe('vllm');
    // Dashboard entry refreshes the live payload; the vLLM host page loads its own history (#1432).
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
  const tabHtml = new JSDOM(src('index.html')).window.document.getElementById('overallTab').outerHTML;
  const VLLM_AGG = {
    agent_count_online: 2, agent_count_total: 3, server_on_count: 1, active_model_count: 1,
    active_models: ['org/served-model'], requests_running_total: 4, requests_waiting_total: 1, max_kv_cache_pct: 42,
    agents: [{ agent_id: 'v1', online: true, age_s: 1.5 }], throughput: { total_tps: 7, total_pps: 2 },
  };

  // The Overall page with the real overall.js; only the vllm aggregate answers.
  async function paintWith(caps) {
    document.body.innerHTML = tabHtml;
    document.getElementById('overallTab').style.display = '';
    window._activeTab = 'overall';
    window.OV = OV;
    window._pollCfg = { agents: caps };
    window._agentsByProvider = { llama: [], lms: [], vllm: [{ agent_id: 'v1', hostname: 'vllm-host' }] };
    window._activeAlerts = [];
    evalGlobal(src('kit/kit.js'));
    evalGlobal(overall);
    const fetchCalls = [];
    window.fetch = async (url) => {
      fetchCalls.push(url);
      return { ok: true, json: async () => (url.includes('/vllm/') ? JSON.parse(JSON.stringify(VLLM_AGG)) : {}) };
    };
    await window.fetchOverallMetrics();
    return fetchCalls;
  }

  test('fetches the vllm fleet aggregate and paints the vLLM server panel from it', async () => {
    const fetchCalls = await paintWith({ vllm_present: true });
    expect(fetchCalls).toContain('/api/fleet/vllm/aggregate');
    const body = document.getElementById('ov-b-srv-vllm');
    expect(body.closest('.panel').hidden).toBe(false);
    expect(body.textContent).toContain('Server on');
    expect(body.textContent).toContain('4 running · 1 waiting');
    expect(body.textContent).toContain('42% full');
    expect(body.textContent).toContain('served-model');
    expect(document.querySelector('[data-card="srv-vllm"] .p-note').textContent).toBe('2 of 3 hosts online');
  });

  test('leaves the vLLM panel out while no agent reports vLLM', async () => {
    await paintWith({});
    expect(document.querySelector('[data-card="srv-vllm"]').hidden).toBe(true);
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

  // #366, #1432: the vLLM dashboard is a host page, so it has no card grid keys.
  test('_activeTabLayoutKeys gives no card keys on the vllm host page', () => {
    document.body.innerHTML = `
      <div id="dash-vllm" class="active"><div class="hp-root" id="hp-vllm" data-hp-page="vllm"></div></div>`;
    const code = [
      blockSrc(foundation, 'const CARD_LABELS_MANAGER', '\n};'),
      srcFile('js/lib/settingsdrawer.js'),
      fnSrc('_getDashSubTab', foundation),
      fnSrc('_sdScope', foundation),
      fnSrc('_sdCardMap', foundation),
      fnSrc('_activeTabLayoutKeys', charts),
    ].join('\n');
    evalGlobal(code);
    window._activeTab = 'dashboard';

    expect(_sdScope()).toMatchObject({ kind: 'rows', root: 'hp-vllm', rowsHidden: 'vllmRowsHidden' });
    expect(_activeTabLayoutKeys()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// #358: vLLM dashboard history backfill wiring.
describe('vllm history backfill (#358, #1432)', () => {
  const charts = src('js/charts.js');

  test('no vllm chart backfill is left; the vLLM host page loads the history', () => {
    // wiring (unexecutable): absence check across the whole file.
    expect(charts).not.toContain('loadVllmHistory');
  });

  test('boot.js no longer backfills vllm history at startup or on dashboard sub-tab entry', () => {
    const boot = src('js/boot.js');
    // wiring (unexecutable): the boot IIFE needs the whole page to run.
    expect(boot).not.toContain('loadVllmHistory');
    const entry = boot.slice(boot.indexOf("if (sub === 'vllm')"));
    expect(entry.slice(0, 120)).toContain('fetchVllmMetrics();');
  });

  test('foundation.js agent-switch resumes the poll without a chart backfill', () => {
    const foundation = src('js/foundation.js');
    const branch = foundation.slice(foundation.indexOf("provider === 'vllm'"));
    // wiring (unexecutable): branch lives inside a large multi-provider switch.
    expect(branch.slice(0, 200)).toContain('fetchVllmMetrics();');
    expect(branch.slice(0, 600)).not.toContain('loadVllmHistory');
    expect(branch.slice(0, 600)).not.toContain('vllmDiskList');
  });

  test('backend maps the vllm history fields and no longer injects the provider default agent ids', () => {
    // wiring (unexecutable): Python backend source — no JS/jsdom harness runs it.
    const be = src('../backend/llm-systems-manager.py');
    for (const f of ['vllm_kv', 'vllm_tps', 'vllm_pps', 'vllm_req_running', 'vllm_req_waiting']) {
      expect(be).toContain(`"${f}"`);
    }
    expect(be).not.toContain('window.__VLLM_AGENT');
    expect(be).not.toContain('window.__LMS_AGENT');
    expect(be).toContain('window.__MGR_AGENT');
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

  // #364, #1432: the vLLM KV and throughput charts are host page panels, not canvases.
  test('has no old vllm chart canvases; the host page has the KV and throughput panels', () => {
    for (const id of ['vllmKvChart', 'vllmTpsChart']) expect(doc.getElementById(id), id).toBeNull();
    const names = HP.panelsFor('vllm').map(p => p.name);
    expect(names).toContain('kv');
    expect(names).toContain('tps');
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
      window._activeTab = 'llm';
      window._subTabState = { dashboard: 'manager', llm: 'vllm' };
      window._agentClaimKey = (base) => base;
      window._claim = () => true; window._release = () => {};
      window._esc = (s) => String(s);
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
// #411, #1432: the vLLM host system metrics show on the vLLM host page, not in old cards.
describe('vllm host system metrics (#411)', () => {
  const dom = new JSDOM(src('index.html'));
  const doc = dom.window.document;

  test('index.html has the vLLM host page and none of the old host system cards', () => {
    expect(doc.getElementById('hp-vllm')).toBeTruthy();
    for (const c of ['vllm-cpu', 'vllm-ram', 'vllm-network', 'vllm-disk', 'vllm-io']) {
      expect(doc.querySelector(`[data-card="${c}"]`), c).toBeNull();
    }
    for (const id of ['vllm-cpu-total', 'vllmCoreGrid', 'vllmDiskList', 'vllmCpuChart', 'vllm-dash-badge']) {
      expect(doc.getElementById(id), id).toBeNull();
    }
  });

  test('the vLLM host page has the host system panels', () => {
    const names = HP.panelsFor('vllm').map(p => p.name);
    for (const n of ['cpu-mem', 'cpu-details', 'mem-details', 'net', 'io', 'disk']) expect(names, n).toContain(n);
  });

  function runVllmHarness(fixture) {
    const bodyHtml = '<div id="vllmCtrlBadge"></div><span id="vllmSrvPill"></span>';
    const stub = `
      // jsdom documents default to hidden (visibilityState 'prerender'),
      // which fetchVllmMetrics treats as "tab backgrounded" and no-ops on.
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
      window._activeTab = 'llm';
      window._subTabState = { dashboard: 'manager', llm: 'vllm' };
      window._agentClaimKey = (base) => base;
      window._claim = () => true; window._release = () => {};
      window._esc = (s) => String(s);
      window._fetchT = () => fetch('/api/vllm/metrics');
      window.fetch = async () => ({ json: async () => (${JSON.stringify(fixture)}) });
    `;
    return runHarness({ sources: [stub, src('js/vllm.js')], bodyHtml });
  }

  test('fetchVllmMetrics keeps the LLM Control badge and server pill current', async () => {
    const w = runVllmHarness({ agent_online: true, vllm: { state: 'running', model: 'org/m', port: 8000 }, system: { cpu_total: 41.2 } });
    const warnings = [];
    w.console.warn = (...args) => warnings.push(args);
    await w.fetchVllmMetrics();
    expect(warnings).toEqual([]);
    expect(w.document.getElementById('vllmCtrlBadge').textContent).toBe('running — org/m');
    expect(w.document.getElementById('vllmSrvPill').textContent).toBe('Server ON · port 8000');
    expect(typeof w._resetVllmCharts).toBe('undefined');
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

describe('index.html vLLM sub-tab', () => {
  test('Benchmark menu entry deep-links to the vllm host', () => {
    const html = src('index.html');
    const m = html.match(/<button onclick="(toolsDeepLink\('benchmark'[^"]*)"/);
    expect(m, 'vLLM Benchmark button not found').toBeTruthy();
    expect(m[1]).toContain("provider: 'vllm'");
  });
  test('the vLLM sub-tab Autotune entry deep-links into Tools and no overlay code remains', () => {
    const html = readFileSync(resolve(ROOT, 'index.html'), 'utf8');
    expect(html).toContain("toolsDeepLink('autotune', null, { provider: 'vllm'");
    expect(html).not.toContain('openVllmAutotune');
    expect(html).not.toContain('vllmAtOverlay');
    expect(html).not.toContain('vllmBenchOverlay');
    expect(html).not.toContain('vllm-bench-autotune.js');
    expect(existsSync(resolve(ROOT, 'js/vllm-bench-autotune.js'))).toBe(false);
  });
});
