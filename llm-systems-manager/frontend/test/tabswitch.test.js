// #268: leaving the Admin tab must close the floating log panel/SSE stream
// and hide the self-update panel. Runs the real switchTab with stubbed spies.
import { describe, test, expect, beforeEach, vi } from 'vitest';
import { srcFile, fnSrc, evalGlobal } from './helpers/harness.js';

const foundation = srcFile('js/foundation.js');

function switchTabSrc() {
  const fn = fnSrc(foundation, 'switchTab');
  expect(fn, 'switchTab not found in foundation.js').toBeTruthy();
  // Guard against a truncated match — the body runs through tab !== 'llm'.
  expect(fn, 'switchTab match looks truncated').toMatch(/tab !== 'llm'/);
  return fn;
}

function loadSwitchTab() {
  evalGlobal(switchTabSrc() + '\nwindow.switchTab = switchTab;');
}

beforeEach(() => {
  document.body.innerHTML = `
    <button class="tab-btn active" onclick="switchTab('admin')">Admin</button>
    <button class="tab-btn" onclick="switchTab('dashboard')">Dashboard</button>
    <div id="overallTab"></div>
    <div id="dashboardTab"></div>
    <div id="llmTab"></div>
    <div id="eventsTab"></div>
    <div id="toolsTab"></div>
    <div id="adminTab"></div>`;
  window._me = { admin_access: true };
  window.adminStopAutoRefresh = vi.fn();
  window._adminLogsClose = vi.fn();
  window._adminUpdateClose = vi.fn();
  window.stopLogStream = vi.fn();
  window.stopPerfRefresh = vi.fn();
  window.stopLmsLogRefresh = vi.fn();
  window.adminLoadAgents = vi.fn();
  window.adminLoadHealth = vi.fn();
  window.adminAuthLoad = vi.fn();
  window.adminStartAutoRefresh = vi.fn();
  loadSwitchTab();
});

describe('switchTab leaving the Admin tab', () => {
  beforeEach(() => { window._activeTab = 'admin'; });

  test('stops admin auto-refresh (pre-existing behavior)', () => {
    switchTab('dashboard');
    expect(window.adminStopAutoRefresh).toHaveBeenCalledTimes(1);
  });

  test('closes the admin log panel and its EventSource', () => {
    switchTab('dashboard');
    expect(window._adminLogsClose).toHaveBeenCalledTimes(1);
  });

  test('hides the admin self-update panel', () => {
    switchTab('dashboard');
    expect(window._adminUpdateClose).toHaveBeenCalledTimes(1);
  });
});

describe('switchTab entering the Admin tab', () => {
  test('does not immediately close the panels it just opened', () => {
    window._activeTab = 'dashboard';
    switchTab('admin');
    expect(window.adminStopAutoRefresh).not.toHaveBeenCalled();
    expect(window._adminLogsClose).not.toHaveBeenCalled();
    expect(window._adminUpdateClose).not.toHaveBeenCalled();
    expect(window.adminStartAutoRefresh).toHaveBeenCalledTimes(1);
  });
});
