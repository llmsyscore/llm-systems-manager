// #847: OpenClaw / LLM Chat / Image Generation bundled under one Tools tab,
// Account moved into the settings drawer, tabs and Admin sub-tabs renamed.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { srcFile, blockSrc, fnSrc, evalGlobal, loadSwitchSubTab } from './helpers/harness.js';

const indexSrc = srcFile('index.html');
const bootSrc = srcFile('js/boot.js');
const foundationSrc = srcFile('js/foundation.js');
const chartsSrc = srcFile('js/charts.js');

const navHtml = blockSrc(indexSrc, '<div class="tab-nav">', '<!-- Dashboard Tab', { includeEnd: false });
const toolsHtml = blockSrc(indexSrc, '<div id="toolsTab"', '<!-- Admin tab -->', { includeEnd: false });

describe('top-level tab bar (#847)', () => {
  beforeEach(() => { document.body.innerHTML = navHtml; });

  it('has one Tools button and no per-proxy buttons', () => {
    expect(document.getElementById('tabBtnTools')).toBeTruthy();
    for (const id of ['tabBtnOpenclaw', 'tabBtnLlmchat', 'tabBtnImggen']) {
      expect(document.getElementById(id), id).toBeNull();
    }
  });

  it('drops the standalone Account button', () => {
    expect(document.getElementById('tabBtnAccount')).toBeNull();
  });

  it('renames Overall and Dashboards', () => {
    expect(document.getElementById('tabBtnOverall').textContent.trim()).toBe('Overall');
    expect(document.getElementById('tabBtnDashboard').textContent.trim()).toBe('Dashboards');
  });
});

describe('Tools sub-tabs (#847)', () => {
  beforeEach(() => {
    document.body.innerHTML = toolsHtml;
    loadSwitchSubTab(bootSrc);
  });

  it('registers tools in the sub-tab map with the three proxies', () => {
    expect(window._SUB_TAB_MAP.tools.subs).toEqual(['openclaw', 'llmchat', 'imggen']);
    expect(window._subTabState.tools).toBe('openclaw');
  });

  it('ships every proxy iframe unloaded', () => {
    const frames = [...document.querySelectorAll('#toolsTab iframe')];
    expect(frames).toHaveLength(3);
    expect(frames.every(f => f.hasAttribute('data-src') && !f.getAttribute('src'))).toBe(true);
  });

  it('promotes data-src to src only for the sub-tab being opened', () => {
    window.switchSubTab('tools', 'llmchat');
    const chat = document.querySelector('#tools-llmchat iframe');
    expect(chat.getAttribute('src')).toBe('/proxy/llmchat/');
    expect(chat.hasAttribute('data-src')).toBe(false);
    expect(document.querySelector('#tools-imggen iframe').getAttribute('src')).toBeNull();
    expect(document.querySelector('#tools-openclaw iframe').getAttribute('src')).toBeNull();
  });

  it('shows only the opened panel and marks its button active', () => {
    window.switchSubTab('tools', 'imggen');
    expect(document.querySelector('#tools-imggen').classList.contains('active')).toBe(true);
    expect(document.querySelector('#tools-openclaw').classList.contains('active')).toBe(false);
    expect(document.getElementById('subTabBtnToolsImggen').classList.contains('active')).toBe(true);
  });
});

describe('switchTab legacy proxy ids (#847)', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <button class="tab-btn" onclick="switchTab('tools')">Tools</button>
      <div id="overallTab"></div><div id="dashboardTab"></div><div id="llmTab"></div>
      <div id="eventsTab"></div><div id="toolsTab"></div><div id="adminTab"></div>`;
    window._me = { admin_access: true };
    window._activeTab = 'overall';
    window._subTabState = { tools: 'openclaw' };
    window.switchSubTab = vi.fn();
    for (const fn of ['adminStopAutoRefresh', '_adminLogsClose', '_adminUpdateClose',
                      'stopLogStream', 'stopPerfRefresh', 'stopLmsLogRefresh']) {
      window[fn] = vi.fn();
    }
    evalGlobal(fnSrc(foundationSrc, 'switchTab') + '\nwindow.switchTab = switchTab;');
  });

  it('maps a bookmarked openclaw/llmchat/imggen id onto the Tools sub-tab', () => {
    for (const legacy of ['openclaw', 'llmchat', 'imggen']) {
      window.switchTab(legacy);
      expect(window._activeTab).toBe('tools');
      expect(window.switchSubTab).toHaveBeenLastCalledWith('tools', legacy);
      expect(document.getElementById('toolsTab').style.display).toBe('');
    }
  });

  it('opens the remembered sub-tab when Tools itself is clicked', () => {
    window._subTabState.tools = 'imggen';
    window.switchTab('tools');
    expect(window.switchSubTab).toHaveBeenLastCalledWith('tools', 'imggen');
  });
});

describe('toolsTab is hoisted out of dashboardTab (#847)', () => {
  it('index.html nests toolsTab inside dashboardTab and boot.js reparents it', async () => {
    const { JSDOM } = await import('jsdom');
    const dom = new JSDOM(indexSrc, { url: 'http://localhost/' });
    const doc = dom.window.document;
    // Without the hoist the panel stays inside a display:none parent.
    expect(doc.getElementById('toolsTab').closest('#dashboardTab')).toBeTruthy();
    const hoist = blockSrc(bootSrc, "  ['toolsTab'", '  });', { includeEnd: true });
    const fn = new dom.window.Function('document', hoist);
    fn(doc);
    expect(doc.getElementById('toolsTab').parentElement).toBe(doc.body);
    for (const id of ['eventsTab', 'adminTab']) {
      expect(doc.getElementById(id).parentElement, id).toBe(doc.body);
    }
  });
});

describe('checkConfig tools fallback (#847)', () => {
  function runFallback({ ocOn, chatOn, imgOn, activeTab, remembered }) {
    const calls = [];
    window._activeTab = activeTab;
    window._subTabState = { tools: remembered };
    window.switchSubTab = (...a) => calls.push(a);
    const body = blockSrc(chartsSrc, '    // Tools sub-tabs: re-point', '    }', { includeEnd: true });
    window.__on = [ocOn, chatOn, imgOn];
    evalGlobal(`(function(ocOn, chatOn, imgOn) {\n${body}\n})(...window.__on);`);
    return { calls, remembered: window._subTabState.tools };
  }

  it('switches for real when Tools is the active tab', () => {
    const r = runFallback({ ocOn: false, chatOn: true, imgOn: true, activeTab: 'tools', remembered: 'openclaw' });
    expect(r.calls).toEqual([['tools', 'llmchat']]);
  });

  it('only re-points the pending sub-tab off-view, so no iframe loads', () => {
    const r = runFallback({ ocOn: false, chatOn: true, imgOn: true, activeTab: 'admin', remembered: 'openclaw' });
    expect(r.calls).toEqual([]);
    expect(r.remembered).toBe('llmchat');
  });

  it('leaves state alone when the remembered proxy is still enabled', () => {
    const r = runFallback({ ocOn: true, chatOn: true, imgOn: true, activeTab: 'tools', remembered: 'openclaw' });
    expect(r.calls).toEqual([]);
    expect(r.remembered).toBe('openclaw');
  });

  it('does nothing when every proxy is disabled', () => {
    const r = runFallback({ ocOn: false, chatOn: false, imgOn: false, activeTab: 'tools', remembered: 'openclaw' });
    expect(r.calls).toEqual([]);
    expect(r.remembered).toBe('openclaw');
  });
});

describe('checkConfig proxy visibility (#847)', () => {
  // Runs the real toggle block so realigning the source can't break the test
  // and a wrong id or predicate can't slip through.
  function runToggles({ openclaw, llm_chat, image_gen }) {
    document.body.innerHTML = ['tabBtnTools', 'subTabBtnToolsOpenclaw', 'subTabBtnToolsLlmchat',
                               'subTabBtnToolsImggen', 'subTabBtnOpenclaw']
      .map(id => `<button id="${id}"></button>`).join('');
    const body = blockSrc(chartsSrc, '    // Tools is one tab', "toggle('subTabBtnOpenclaw',      ocOn);");
    // Arguments go through window, never interpolated into the evaluated source.
    window.__px = { openclaw, llm_chat, image_gen };
    window.__toggle = (id, on) => { document.getElementById(id).style.display = on ? '' : 'none'; };
    evalGlobal(`(function(px, toggle) {\n${body}\n})(window.__px, window.__toggle);`);
    const shown = id => document.getElementById(id).style.display !== 'none';
    return {
      tools: shown('tabBtnTools'), oc: shown('subTabBtnToolsOpenclaw'),
      chat: shown('subTabBtnToolsLlmchat'), img: shown('subTabBtnToolsImggen'),
      dashOc: shown('subTabBtnOpenclaw'),
    };
  }

  it('shows each sub-tab for its own proxy', () => {
    expect(runToggles({ openclaw: false, llm_chat: 'auto', image_gen: false }))
      .toMatchObject({ oc: false, chat: true, img: false, dashOc: false });
  });

  it('keeps Tools visible while any one proxy is enabled', () => {
    for (const only of ['openclaw', 'llm_chat', 'image_gen']) {
      const px = { openclaw: false, llm_chat: false, image_gen: false, [only]: 'auto' };
      expect(runToggles(px).tools, only).toBe(true);
    }
  });

  it('hides Tools only when every proxy is disabled', () => {
    expect(runToggles({ openclaw: false, llm_chat: false, image_gen: false }).tools).toBe(false);
  });

  it('treats a manager with no proxies key as all-enabled', () => {
    expect(runToggles({})).toMatchObject({ tools: true, oc: true, chat: true, img: true });
  });
});

describe('Account moved into the settings drawer (#847)', () => {
  beforeEach(() => {
    document.body.innerHTML = '<section class="sd-sec" id="sdAccount" hidden></section>';
    window._esc = (s) => String(s);
    evalGlobal('window._esc = window._esc;'
      + fnSrc(foundationSrc, '_sdRenderAccount') + '\nwindow._sdRenderAccount = _sdRenderAccount;');
  });

  it('renders both entries for a logged-in session', () => {
    window._me = { authenticated: true, username: 'llmadmin', role: 'admin' };
    window._sdRenderAccount();
    const el = document.getElementById('sdAccount');
    expect(el.hidden).toBe(false);
    expect(el.querySelector('[data-sd="account-password"]')).toBeTruthy();
    expect(el.querySelector('[data-sd="account-logout"]')).toBeTruthy();
    expect(el.textContent).toContain('llmadmin');
  });

  it('stays hidden and empty for a bypass session', () => {
    window._me = { authenticated: false };
    window._sdRenderAccount();
    const el = document.getElementById('sdAccount');
    expect(el.hidden).toBe(true);
    expect(el.innerHTML).toBe('');
  });
});

describe('settings-drawer Account wiring (#847)', () => {
  beforeEach(() => {
    document.documentElement.innerHTML = indexSrc;
    window._me = { authenticated: true, username: 'llmadmin', role: 'admin', admin_access: true };
    window._activeTab = 'overall';
    window.closeSettings = vi.fn();
    window._accountChangePassword = vi.fn();
    window._esc = (v) => String(v);
    // _sdBind's once-only latch is a module-level `let`, not a window prop.
    evalGlobal('var _sdBound = false;');
    for (const name of ['_sdRenderAccount', '_sdBind']) {
      evalGlobal(fnSrc(foundationSrc, name) + `\nwindow.${name} = ${name};`);
    }
  });

  it('keeps the Account section inside the element _sdBind listens on', () => {
    expect(document.getElementById('sdAccount').closest('#settingsOverlay')).toBeTruthy();
  });

  it('routes the password button through the delegated handler', () => {
    window._sdBind();
    window._sdRenderAccount();
    document.querySelector('[data-sd="account-password"]').click();
    expect(window.closeSettings).toHaveBeenCalledTimes(1);
    expect(window._accountChangePassword).toHaveBeenCalledTimes(1);
  });

  it('applyRoleGating hides the section for a bypass session', () => {
    window._me = { authenticated: false, admin_access: true };
    evalGlobal(fnSrc(foundationSrc, 'applyRoleGating') + '\nwindow.applyRoleGating = applyRoleGating;');
    window.applyRoleGating();
    const el = document.getElementById('sdAccount');
    expect(el.hidden).toBe(true);
    expect(el.innerHTML).toBe('');
  });
});

describe('Admin sub-tab renames (#847)', () => {
  beforeEach(() => { document.documentElement.innerHTML = indexSrc; });

  it('renames Routing to Gateway and Backup & Restore to Backups', () => {
    expect(document.getElementById('subTabBtnAdminRouting').textContent.trim()).toBe('Gateway');
    expect(document.getElementById('subTabBtnAdminBackup').textContent.trim()).toBe('Backups');
    expect(document.querySelector('#admin-routing .hdr h2').textContent.trim()).toBe('Gateway');
    expect(document.querySelector('#admin-backup .hdr h2').textContent.trim()).toBe('Backups');
  });
});
