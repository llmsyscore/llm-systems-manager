// #985: System Health service rows get a View log button that opens the shared
// admin log dock over the manager / alarm-engine routes; level tinting.
// #946: `/` focuses the Settings filter while Admin › Settings is showing.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const adminSrc = srcFile('js/admin.js');
const healthSrc = srcFile('js/admin-health.js');
const indexSrc = srcFile('index.html');
const CARD = indexSrc.slice(indexSrc.indexOf('<div id="adminHealthCard">'),
                            indexSrc.indexOf('<!-- Sub-tabs underneath System Health -->'));

const HEALTHY = {
  overall: 'ok',
  manager: { ok: true, version: 'v1', uptime_s: 10, streams: {}, connections: {} },
  services: [
    { name: 'alarm_engine', ok: true, version: 'v1', uptime_s: 10, tls: { enabled: false } },
    { name: 'influxdb', ok: true, state: 'connected', version: '2.7' },
  ],
  agents: [], data_flow: {}, flow: {}, agent_update: {}, ae_restart: { available: true, via: 'systemctl' }, warnings: [],
};

describe('service log buttons (#985)', () => {
  test('manager and alarm engine rows carry a View log button; influx does not', () => {
    const win = runHarness({
      sources: [adminSrc, healthSrc],
      bodyHtml: `<div id="adminTab">${CARD}</div>`,
      bootstrap: `HealthView.render(${JSON.stringify(HEALTHY)}, null);`,
    });
    const svcs = [...win.document.querySelectorAll('[data-log-svc]')].map(b => b.getAttribute('data-log-svc'));
    expect(svcs).toEqual(['manager', 'alarm_engine']);
    expect(win.document.querySelector('[data-log-svc="manager"]').getAttribute('data-tip')).toBe('View Manager log');
  });

  test('clicking View log dispatches to adminServiceLogs with the service key', () => {
    const win = runHarness({
      sources: [adminSrc, healthSrc],
      bodyHtml: `<div id="adminTab">${CARD}</div>`,
      bootstrap: `window.__calls = [];
        adminServiceLogs = (s) => window.__calls.push(s);
        HealthView.render(${JSON.stringify(HEALTHY)}, null);
        document.querySelector('[data-log-svc="alarm_engine"]').click();`,
    });
    expect(win.__calls).toEqual(['alarm_engine']);
  });

  test('adminServiceLogs seeds the dock from the service tail route and opens its stream', async () => {
    const win = runHarness({
      sources: [adminSrc],
      bootstrap: `window.__fetched = []; window.__streams = [];
        window.fetch = (u) => { window.__fetched.push(u); return Promise.resolve({ ok: true, status: 200,
          json: () => Promise.resolve({ ok: true, lines: ['2026-09-17 [INFO] boot', '2026-09-17 [WARNING] slow', '2026-09-17 [ERROR] bad'] }) }); };
        window.EventSource = class { constructor(u) { window.__streams.push(u); } close() {} };
        window.LivePause = { on: false };
        adminServiceLogs('alarm_engine');`,
    });
    await flush(); await flush();
    expect(win.__fetched).toEqual(['/api/admin/alarm-engine/log/tail']);
    expect(win.__streams).toEqual(['/api/admin/alarm-engine/log/stream']);
    expect(win.document.getElementById('adminLogsKind').textContent).toBe('Service log');
    expect(win.document.getElementById('adminLogsTitle').textContent).toBe('Alarm Engine');
    const lines = [...win.document.querySelectorAll('#adminLogsBody div')];
    const byText = t => lines.find(l => l.textContent.includes(t));
    expect(byText('boot').className).toBe('');
    expect(byText('slow').className).toBe('l-warn');
    expect(byText('bad').className).toBe('l-err');
  });

  test('the manager service uses its own tail/stream routes', async () => {
    const win = runHarness({
      sources: [adminSrc],
      bootstrap: `window.__fetched = []; window.__streams = [];
        window.fetch = (u) => { window.__fetched.push(u); return Promise.resolve({ ok: false, status: 503,
          json: () => Promise.resolve({ ok: false, error: 'manager at stream capacity; retry shortly' }) }); };
        window.EventSource = class { constructor(u) { window.__streams.push(u); } close() {} };
        adminServiceLogs('manager');`,
    });
    await flush(); await flush(); await flush();
    expect(win.__fetched).toEqual(['/api/admin/log/tail']);
    expect(win.__streams).toEqual(['/api/admin/log/stream']);
    expect(win.document.getElementById('adminLogsBody').textContent).toContain('HTTP 503 — manager at stream capacity');
  });

  test('the agent viewer still labels the dock Agent log', () => {
    const win = runHarness({
      sources: [adminSrc],
      bootstrap: `_adminLogsOpen('h1');`,
    });
    expect(win.document.getElementById('adminLogsKind').textContent).toBe('Agent log');
  });
});

describe('Settings filter hotkey (#946)', () => {
  const settingsSrc = srcFile('js/admin-settings.js');
  const foundationSrc = srcFile('js/foundation.js');
  const _stAt = indexSrc.indexOf('<div id="admin-settings" class="sub-tab-panel">');
  const PANEL = indexSrc.slice(_stAt, indexSrc.indexOf('\n\n    </div>', _stAt));

  async function boot(sub) {
    const dom = new JSDOM(`<!doctype html><html><body><div id="adminTab">${PANEL}<input id="other"></div></body></html>`,
      { runScripts: 'dangerously', url: 'http://localhost/' });
    dom.window.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
      ok: true, groups: [], entries: [], values: {}, defaults: {}, secrets: {}, drift: {}, restart_pending: [],
      ae_sync_pending: [], topology: {}, enabled: false }) });
    const inject = (code) => { const s = dom.window.document.createElement('script'); s.textContent = code; dom.window.document.body.appendChild(s); };
    inject(`if (!window.CSS) window.CSS = { escape: s => String(s) }; var _subTabState = { admin: ${JSON.stringify(sub)} }; var _activeTab = 'admin';`);
    inject(foundationSrc);
    inject(srcFile('js/lib/tower-view.js'));
    inject(settingsSrc);
    await dom.window.adminSettingsLoad();
    return dom.window;
  }
  const slash = (win, target) => {
    const ev = new win.KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true });
    (target || win.document.body).dispatchEvent(ev);
    return ev;
  };

  test('/ focuses the filter box while Settings is active', async () => {
    const win = await boot('settings');
    const ev = slash(win);
    expect(win.document.activeElement.id).toBe('stFilter');
    expect(ev.defaultPrevented).toBe(true);
  });

  test('/ is ignored while typing in another input or on another sub-tab', async () => {
    const win = await boot('settings');
    const other = win.document.getElementById('other');
    other.focus();
    expect(slash(win, other).defaultPrevented).toBe(false);
    expect(win.document.activeElement.id).toBe('other');
    const win2 = await boot('audit');
    expect(slash(win2).defaultPrevented).toBe(false);
    expect(win2.document.activeElement.id).not.toBe('stFilter');
  });

  test('Escape in the filter clears it and drops focus', async () => {
    const win = await boot('settings');
    const box = win.document.getElementById('stFilter');
    box.value = 'tls'; box.focus();
    box.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(box.value).toBe('');
    expect(win.document.activeElement.id).not.toBe('stFilter');
  });
});
