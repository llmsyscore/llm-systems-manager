// #847: OpenClaw / LLM Chat / Image Generation bundled under one Tools tab,
// Account moved into the settings drawer, tabs and Admin sub-tabs renamed.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { srcFile, fnSrc, evalGlobal, loadSwitchSubTab } from './helpers/harness.js';

const indexSrc = srcFile('index.html');
const bootSrc = srcFile('js/boot.js');
const foundationSrc = srcFile('js/foundation.js');
const chartsSrc = srcFile('js/charts.js');

const navHtml = indexSrc.slice(indexSrc.indexOf('<div class="tab-nav">'),
                               indexSrc.indexOf('<!-- Dashboard Tab'));
const toolsHtml = indexSrc.slice(indexSrc.indexOf('<div id="toolsTab"'),
                                 indexSrc.indexOf('<!-- Admin tab -->'));

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

describe('checkConfig proxy visibility (#847)', () => {
  it('hides the Tools tab only when every proxy is disabled', () => {
    const body = chartsSrc.slice(chartsSrc.indexOf('const ocOn = px.openclaw'),
                                 chartsSrc.indexOf("toggle('subTabBtnOpenclaw'"));
    expect(body).toContain("toggle('tabBtnTools',            ocOn || chatOn || imgOn)");
    expect(body).toContain("toggle('subTabBtnToolsOpenclaw', ocOn)");
    expect(body).toContain("toggle('subTabBtnToolsLlmchat',  chatOn)");
    expect(body).toContain("toggle('subTabBtnToolsImggen',   imgOn)");
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

describe('Admin sub-tab renames (#847)', () => {
  beforeEach(() => { document.documentElement.innerHTML = indexSrc; });

  it('renames Routing to Gateway and Backup & Restore to Backups', () => {
    expect(document.getElementById('subTabBtnAdminRouting').textContent.trim()).toBe('Gateway');
    expect(document.getElementById('subTabBtnAdminBackup').textContent.trim()).toBe('Backups');
    expect(document.querySelector('#admin-routing .hdr h2').textContent.trim()).toBe('Gateway');
    expect(document.querySelector('#admin-backup .hdr h2').textContent.trim()).toBe('Backups');
  });
});
