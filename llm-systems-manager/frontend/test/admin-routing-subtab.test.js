// #476: Pools & Pins + Autopilot consolidated into one routing sub-tab,
// Admin sub-tabs alphabetized, Agents/Audit tables column-sortable.
import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { srcFile, runHarness as sharedRunHarness, loadSwitchSubTab } from './helpers/harness.js';

const bootSrc = srcFile('js/boot.js');
const adminSrc = srcFile('js/admin.js');
const apSrc = srcFile('js/autopilot.js');
const indexSrc = srcFile('index.html');

function extractTag(src, id) {
  const m = src.match(new RegExp(`<table id="${id}"[\\s\\S]*?<\\/table>`));
  if (!m) throw new Error(`${id} table not found in index.html`);
  return m[0];
}
const agentsTableHtml = extractTag(indexSrc, 'adminAgentsTable');
const auditTableHtml = extractTag(indexSrc, 'adminAuditTable');

function runAdminHarness(bootstrap, bodyHtml = '') {
  const defaultSortable =
    'if (typeof Sortable === "undefined") { Sortable = { create: () => ({ destroy(){} }) }; }';
  return sharedRunHarness({ sources: [adminSrc], bootstrap: defaultSortable + '\n' + bootstrap, bodyHtml });
}

function runApHarness(bootstrap, bodyHtml = '') {
  return sharedRunHarness({ sources: [apSrc], bootstrap, bodyHtml });
}

describe('routing sub-tab consolidation (#476)', () => {
  beforeEach(() => {
    document.documentElement.innerHTML = indexSrc;
    loadSwitchSubTab(bootSrc);
  });

  it('index.html has one admin-routing panel, no admin-pool/admin-autopilot panels', () => {
    expect(document.getElementById('admin-routing')).toBeTruthy();
    expect(document.getElementById('admin-pool')).toBeNull();
    expect(document.getElementById('admin-autopilot')).toBeNull();
  });

  it('merged panel contains pool, pins, and both autopilot cards', () => {
    expect(document.querySelector('#admin-routing #adminPoolCard')).toBeTruthy();
    expect(document.querySelector('#admin-routing #adminPinsCard')).toBeTruthy();
    expect(document.querySelector('#admin-routing #apEntriesBody')).toBeTruthy();
    expect(document.querySelector('#admin-routing #apProposalsBody')).toBeTruthy();
  });

  it('nav has a single Routing button wired to the routing sub-tab', () => {
    const btn = document.getElementById('subTabBtnAdminRouting');
    expect(btn).toBeTruthy();
    expect(btn.textContent).toBe('Routing');
    expect(btn.getAttribute('onclick')).toContain("switchSubTab('admin','routing')");
    expect(document.getElementById('subTabBtnAdminPool')).toBeNull();
    expect(document.getElementById('subTabBtnAdminAutopilot')).toBeNull();
  });

  it('admin sub-tab buttons are ordered alphabetically', () => {
    const nav = document.querySelector('#adminTab .sub-tab-nav');
    const labels = [...nav.querySelectorAll('.sub-tab-btn')].map(b => b.textContent.trim());
    expect(labels.length).toBeGreaterThanOrEqual(5);
    expect(labels).toEqual([...labels].sort((a, b) => a.localeCompare(b)));
  });

  it('Agents is the default admin sub-tab, driven by the real boot.js state', () => {
    // Executes the real _subTabState/_SUB_TAB_MAP object literals, not a
    // regex match on boot.js text.
    expect(window._subTabState.admin).toBe('agents');
    expect(window._SUB_TAB_MAP.admin.subs).toContain('routing');
    expect(window._SUB_TAB_MAP.admin.subs).not.toContain('pool');
    expect(window._SUB_TAB_MAP.admin.subs).not.toContain('autopilot');
    const agentsBtn = document.getElementById('subTabBtnAdminAgents');
    expect(agentsBtn.classList.contains('active')).toBe(true);
    expect(document.querySelector('#admin-agents.sub-tab-panel.active')).toBeTruthy();
  });

  it('switchSubTab(admin, routing) activates the routing panel and initializes the autopilot editor', () => {
    let initCalls = 0;
    window.AP = { init: () => { initCalls++; } };
    window.switchSubTab('admin', 'routing');
    expect(initCalls).toBe(1);
    expect(window._subTabState.admin).toBe('routing');
    expect(document.getElementById('admin-routing').classList.contains('active')).toBe(true);
    expect(document.getElementById('admin-agents').classList.contains('active')).toBe(false);
    expect(document.getElementById('subTabBtnAdminRouting').classList.contains('active')).toBe(true);
    expect(document.getElementById('subTabBtnAdminAgents').classList.contains('active')).toBe(false);
  });

  it('autopilot.js\'s AP.poll() only fetches while the routing sub-tab is actually visible', () => {
    // Drives the real _visible() gate through poll()/fetchState() instead
    // of grepping autopilot.js for the '_subTabState.admin === ...' string.
    const boot = `
      window.__calls = [];
      window.fetch = (url) => { window.__calls.push(String(url)); return Promise.resolve({ ok: true, json: async () => ({}) }); };
      window._activeTab = 'admin';
      window._subTabState = { admin: 'agents' };
      AP.poll();
      window.__callsOffRouting = window.__calls.length;
      window._subTabState.admin = 'routing';
      AP.poll();
      window.__callsOnRouting = window.__calls.length;
    `;
    const win = runApHarness(boot);
    expect(win.__callsOffRouting).toBe(0);
    expect(win.__callsOnRouting).toBeGreaterThan(win.__callsOffRouting);
  });
});

describe('autopilot-managed badges (#476)', () => {
  beforeAll(() => {
    document.documentElement.innerHTML = indexSrc;
  });

  function renderPins(entries) {
    const boot = `
      _adminPoolProviders = [{ name:'llama', label:'llama.cpp', pin_key:'llama_model_pins' }];
      _adminPinsSel = 'llama';
      _adminGlobal = { llama_model_pins: { 'model-a': 'agent-K' }, llama_pool: [],
                        autopilot: { entries: ${JSON.stringify(entries)} } };
      _adminAgentsCache = [{ agent_id: 'agent-K', hostname: 'k1', status: 'approved', capabilities: { llama: true } }];
      adminRenderPins();
      window.__T = { html: document.getElementById('adminPinsTbody').innerHTML };
    `;
    const body = '<div id="adminPinsProviderChips"></div>' +
      '<table><tbody id="adminPinsTbody"></tbody></table>' +
      '<select id="adminPinAgentSelect"></select>';
    return runAdminHarness(boot, body).__T.html;
  }

  function renderPoolOrder(entries) {
    const boot = `
      window.__sortableCalls = 0;
      Sortable = { create: () => { window.__sortableCalls++; return { destroy(){} }; } };
      _adminPoolProviders = [{ name:'llama', label:'llama.cpp', pin_key:'llama_model_pins' }];
      _adminPoolSel = 'llama';
      _adminGlobal = { llama_pool: ['agent-K'], autopilot: { entries: ${JSON.stringify(entries)} } };
      _adminAgentsCache = [{ agent_id: 'agent-K', hostname: 'k1', status: 'approved', capabilities: { llama: true } }];
      adminRenderPoolOrder();
      window.__T = {
        html: document.getElementById('adminPoolOrderList').innerHTML,
        badgeHidden: document.getElementById('adminPoolApBadge').style.display === 'none',
        dragHintHidden: document.getElementById('adminPoolDragHint').style.display === 'none',
        sortableCreated: window.__sortableCalls > 0,
      };
    `;
    const body = '<div id="adminPoolProviderChips"></div>' +
      '<ul id="adminPoolOrderList"></ul>' +
      '<span id="adminPoolApBadge"></span><span id="adminPoolDragHint"></span>';
    return runAdminHarness(boot, body).__T;
  }

  it('adminRenderPins tags a pin whose model+provider matches an autopilot entry', () => {
    const html = renderPins([{ provider: 'llama', model: 'model-a', max_replicas: 1 }]);
    expect(html).toContain('ap-managed-badge');
  });

  it('adminRenderPins leaves an unmanaged pin without the badge', () => {
    const html = renderPins([]);
    expect(html).not.toContain('ap-managed-badge');
  });

  it('pool card has the autopilot-managed membership badge element', () => {
    expect(document.getElementById('adminPoolApBadge')).toBeTruthy();
  });

  it('pool badge shows and reorder disables only once max_replicas > 1 (single-placed stays manual, #500)', () => {
    const single = renderPoolOrder([{ provider: 'llama', model: 'model-a', max_replicas: 1 }]);
    expect(single.badgeHidden).toBe(true);
    expect(single.dragHintHidden).toBe(false);
    expect(single.sortableCreated).toBe(true);
    expect(single.html).toContain('pool-handle');

    const replicated = renderPoolOrder([{ provider: 'llama', model: 'model-a', max_replicas: 2 }]);
    expect(replicated.badgeHidden).toBe(false);
    expect(replicated.dragHintHidden).toBe(true);
    expect(replicated.sortableCreated).toBe(false);
    expect(replicated.html).not.toContain('pool-handle');
  });
});

describe('sortable table columns (#476 scope)', () => {
  beforeAll(() => {
    document.documentElement.innerHTML = indexSrc;
  });

  it('agents table headers are sort-wired except Actions', () => {
    const ths = [...document.querySelectorAll('#adminAgentsTable thead th')];
    const sortable = ths.filter(th => th.classList.contains('adm-th-sort'));
    expect(sortable.length).toBe(4);
    sortable.forEach(th => {
      expect(th.dataset.key).toBeTruthy();
      expect(th.getAttribute('onclick')).toContain('adminSortAgents(this)');
    });
    expect(ths[ths.length - 1].classList.contains('adm-th-sort')).toBe(false);
  });

  it('audit table headers are all sort-wired', () => {
    const ths = [...document.querySelectorAll('#adminAuditTable thead th')];
    expect(ths.length).toBe(6);
    ths.forEach(th => {
      expect(th.classList.contains('adm-th-sort')).toBe(true);
      expect(th.dataset.key).toBeTruthy();
      expect(th.getAttribute('onclick')).toContain('adminSortAudit(this)');
    });
  });

  it('adminSortAgents re-sorts and re-renders the real agents table (click toggles direction)', () => {
    const boot = `
      _adminAgentsSort = { key: 'seen', dir: 1 };
      _adminAgentsCache = [
        { agent_id: 'a1', hostname: 'zzz-host', status: 'approved', capabilities: {} },
        { agent_id: 'a2', hostname: 'aaa-host', status: 'approved', capabilities: {} },
      ];
      _adminRenderAgentsTable();
      const th = document.querySelector('#adminAgentsTable th[data-key="agent"]');
      adminSortAgents(th);
      window.__order1 = [...document.querySelectorAll('#adminAgentsTbody .adm-host')].map(d => d.textContent);
      window.__dir1 = th.dataset.dir;
      adminSortAgents(th);
      window.__order2 = [...document.querySelectorAll('#adminAgentsTbody .adm-host')].map(d => d.textContent);
      window.__dir2 = th.dataset.dir;
    `;
    const win = runAdminHarness(boot, agentsTableHtml);
    expect(win.__order1).toEqual(['aaa-host', 'zzz-host']);
    expect(win.__dir1).toBe('1');
    expect(win.__order2).toEqual(['zzz-host', 'aaa-host']);
    expect(win.__dir2).toBe('-1');
  });

  it('adminSortAudit re-sorts and re-renders the real audit table', () => {
    const boot = `
      _adminAuditEntries = [
        { actor: 'zeta', action: 'x', ts: 1 },
        { actor: 'alpha', action: 'y', ts: 2 },
      ];
      _adminRenderAuditTable();
      const th = document.querySelector('#adminAuditTable th[data-key="actor"]');
      adminSortAudit(th);
      window.__order = [...document.querySelectorAll('#adminAuditTbody tr')].map(tr => tr.children[1].textContent);
      window.__dir = th.dataset.dir;
    `;
    const win = runAdminHarness(boot, auditTableHtml);
    expect(win.__order).toEqual(['alpha', 'zeta']);
    expect(win.__dir).toBe('1');
  });

  it('sort arrows render on the header even when the agents/audit tables are empty', () => {
    const boot = `
      _adminAgentsSort = { key: 'state', dir: -1 };
      _adminAgentsCache = [];
      _adminRenderAgentsTable();
      _adminAuditSort = { key: 'actor', dir: -1 };
      _adminAuditEntries = [];
      _adminRenderAuditTable();
      window.__agentsDir = document.querySelector('#adminAgentsTable th[data-key="state"]').dataset.dir;
      window.__agentsEmptyRow = document.getElementById('adminAgentsTbody').textContent;
      window.__auditDir = document.querySelector('#adminAuditTable th[data-key="actor"]').dataset.dir;
      window.__auditEmptyRow = document.getElementById('adminAuditTbody').textContent;
    `;
    const win = runAdminHarness(boot, agentsTableHtml + auditTableHtml);
    expect(win.__agentsDir).toBe('-1');
    expect(win.__agentsEmptyRow).toContain('No agents registered yet.');
    expect(win.__auditDir).toBe('-1');
    expect(win.__auditEmptyRow).toContain('No audit entries yet.');
  });

  it('adminLoadAgents renders pins + pool even with zero registered agents (no early return)', async () => {
    const boot = `
      window.fetch = async (url) => {
        if (String(url).includes('/api/agents')) {
          return { ok: true, json: async () => ({
            agents: [], global: {},
            pool_providers: [{ name: 'llama', label: 'llama.cpp', pin_key: 'llama_model_pins' }],
            providers: [],
          }) };
        }
        return { ok: true, json: async () => ({ models: [] }) };
      };
      window.__done = adminLoadAgents();
    `;
    const body = '<input type="checkbox" id="adminAuthDisabled">' +
      '<span id="adminLatestVersion"></span><span id="adminAgentsCount"></span>' +
      '<span id="adminLastRefresh"></span><div id="adminAgentsResult"></div>' +
      agentsTableHtml +
      '<div id="adminPinsProviderChips"></div>' +
      '<table><tbody id="adminPinsTbody">SENTINEL-PINS</tbody></table>' +
      '<select id="adminPinAgentSelect"></select>' +
      '<div id="adminPoolProviderChips"></div>' +
      '<ul id="adminPoolOrderList">SENTINEL-POOL</ul>' +
      '<span id="adminPoolApBadge"></span><span id="adminPoolDragHint"></span>' +
      '<datalist id="adminProviderModels"></datalist>';
    const win = runAdminHarness(boot, body);
    await win.__done;
    expect(win.document.getElementById('adminPinsTbody').innerHTML).not.toContain('SENTINEL-PINS');
    expect(win.document.getElementById('adminPinsTbody').innerHTML).toContain('no pins set');
    expect(win.document.getElementById('adminPoolOrderList').innerHTML).not.toContain('SENTINEL-POOL');
    expect(win.document.getElementById('adminPoolOrderList').innerHTML).toContain('pool is empty');
  });

  it('a failed audit load clears the cached entries so a re-render shows empty, not stale rows', async () => {
    const boot = `
      window.fetch = async () => { throw new Error('network down'); };
      _adminAuditEntries = [{ actor: 'stale', action: 'x', ts: 1 }];
      window.__done = adminAuditLoad(0).then(() => {
        window.__errorRow = document.getElementById('adminAuditTbody').textContent;
        _adminRenderAuditTable();
        window.__afterRerender = document.getElementById('adminAuditTbody').textContent;
      });
    `;
    const body = auditTableHtml +
      '<span id="adminAuditStatus"></span><button id="adminAuditNewer"></button>' +
      '<button id="adminAuditOlder"></button><span id="adminAuditPageInfo"></span>';
    const win = runAdminHarness(boot, body);
    await win.__done;
    expect(win.__errorRow).toContain('Failed to load audit log.');
    expect(win.__afterRerender).toContain('No audit entries yet.');
    expect(win.__afterRerender).not.toContain('stale');
  });

  it('admin.css styles the sortable headers and direction arrows', () => {
    // wiring (unexecutable): jsdom has no paint engine, so this stays a
    // source check of admin.css.
    const css = srcFile('css/admin.css');
    expect(css).toMatch(/\.adm-th-sort/);
    expect(css).toMatch(/data-dir/);
  });
});
