// #806: the gateway poll stops on leaving Admin, restarts on re-entry with
// Routing remembered, and never stacks a second flow request.
import { describe, test, expect, beforeEach, afterEach, vi } from 'vitest';
import { srcFile, fnSrc, evalGlobal, runHarness } from './helpers/harness.js';

const adminSrc = srcFile('js/admin.js');
const gwSrc = srcFile('js/admin-gateway.js');
const indexSrc = srcFile('index.html');
const CARD = indexSrc.slice(indexSrc.indexOf('<div class="card" id="rtGatewayCard"'),
                            indexSrc.indexOf('<div class="card" id="apEntriesCard">'));

function loadAdminRefresh() {
  for (const name of ['adminStartAutoRefresh', '_adminRefreshTick', 'adminRefreshNow', 'adminStopAutoRefresh']) {
    const fn = fnSrc(adminSrc, name);
    expect(fn, `${name} not found in admin.js`).toBeTruthy();
    evalGlobal(fn + `\nwindow.${name} = ${name};`);
  }
}

describe('admin auto-refresh owns the gateway poll', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window._adminRefreshTimer = null;
    window._activeTab = 'admin';
    window.adminLoadAgents = vi.fn();
    window.adminLoadHealth = vi.fn();
    window.GatewayView = { start: vi.fn(), stop: vi.fn() };
    window.LivePause = { on: false, every: (fn, ms) => setInterval(fn, ms) };
    loadAdminRefresh();
  });
  afterEach(() => { window.adminStopAutoRefresh(); vi.useRealTimers(); });

  test('re-entering Admin with Routing remembered restarts the gateway poll', () => {
    window._subTabState = { admin: 'routing' };
    window.adminStartAutoRefresh();
    expect(window.GatewayView.start).toHaveBeenCalledTimes(1);
  });

  test('re-entering Admin on another sub-tab leaves the poll stopped', () => {
    window._subTabState = { admin: 'agents' };
    window.adminStartAutoRefresh();
    expect(window.GatewayView.start).not.toHaveBeenCalled();
  });

  test('leaving Admin still stops the poll', () => {
    window._subTabState = { admin: 'routing' };
    window.adminStartAutoRefresh();
    window.adminStopAutoRefresh();
    expect(window.GatewayView.stop).toHaveBeenCalledTimes(1);
  });
});

describe('gateway poll fetch is single-flight', () => {
  function harness(bootstrap) {
    return runHarness({
      sources: [gwSrc],
      bodyHtml: `<div id="adminTab"><div id="admin-routing">${CARD}</div></div>`,
      bootstrap,
    });
  }

  test('a pending flow request blocks a second one', async () => {
    const win = harness(`window.__calls = 0;
      window.fetch = () => { window.__calls++; return new Promise(() => {}); };
      GatewayView.refresh(); GatewayView.refresh();`);
    await Promise.resolve();
    expect(win.__calls).toBe(1);
  });

  test('a toggle waits out the pending request, then fetches again', async () => {
    const win = harness(`window.__resolvers = [];
      window.fetch = (url, opts) => new Promise(res => {
        if (opts && opts.method === 'PUT') { res({ ok: true, json: async () => ({ ok: true, enabled: false }) }); return; }
        window.__resolvers.push(() => res({ ok: true, json: async () => ({ ok: true, enabled: true, clients: [], hosts: [], totals: {}, energy: {} }) }));
      });
      GatewayView.refresh();
      window.__toggle = GatewayView.setEnabled(false);`);
    await Promise.resolve();
    expect(win.__resolvers.length).toBe(1);     // still just the first flow request
    win.__resolvers[0]();
    await win.__toggle;
    await new Promise(r => setTimeout(r, 0));
    expect(win.__resolvers.length).toBe(2);     // the post-toggle fetch went out after it
  });

  test('a hidden document skips the poll', async () => {
    const win = harness(`window.__calls = 0;
      window.fetch = () => { window.__calls++; return Promise.resolve({ ok: true, json: async () => ({ ok: true }) }); };
      Object.defineProperty(document, 'hidden', { value: true, configurable: true });
      window.__done = GatewayView.refresh();`);
    await win.__done;
    expect(win.__calls).toBe(0);
  });

  test('uses the timed fetch helper when the page provides one', async () => {
    const win = harness(`window.__t = [];
      window._fetchT = (url, opts, ms) => { window.__t.push([url, ms]);
        return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true, enabled: true, clients: [], hosts: [], totals: {}, energy: {} }) }); };
      window.__done = GatewayView.refresh();`);
    await win.__done;
    expect(win.__t).toEqual([['/api/admin/gateway/flow', 8000]]);
    expect(win.document.getElementById('rtGatewayCard').hidden).toBe(false);
  });
});
