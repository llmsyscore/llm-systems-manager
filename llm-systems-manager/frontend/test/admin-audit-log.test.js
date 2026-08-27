// Admin audit-log table (#654–#658): stale-fetch guard, sort reset on
// paging, typed comparators, pager reset on failure, auto-refresh tick.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness } from './helpers/harness.js';

const adminSrc = srcFile('js/admin.js');
const indexSrc = srcFile('index.html');
const auditTableHtml = (() => {
  const m = indexSrc.match(/<table id="adminAuditTable"[\s\S]*?<\/table>/);
  if (!m) throw new Error('adminAuditTable not found');
  return m[0];
})();
const pagerHtml = '<span id="adminAuditStatus"></span><button id="adminAuditNewer"></button>' +
  '<button id="adminAuditOlder"></button><span id="adminAuditPageInfo"></span>';

function run(bootstrap) {
  const stub = 'if (typeof Sortable === "undefined") { Sortable = { create: () => ({ destroy(){} }) }; }';
  return runHarness({ sources: [adminSrc], bootstrap: stub + '\n' + bootstrap, bodyHtml: auditTableHtml + pagerHtml });
}

describe('admin audit log (#654–#658)', () => {
  it('#654: a slower older response never overwrites the page loaded after it', async () => {
    const boot = `
      const pending = [];
      window.fetch = async (url) => {
        const offset = Number(new URL(url, 'http://x').searchParams.get('offset'));
        const total = 250;
        const body = { ok: true, total, entries: Array.from({ length: 100 }, (_, i) => ({ ts: 't', actor: 'a' + (offset + i), action: 'x', outcome: 'ok' })) };
        if (offset === 100) return new Promise(res => pending.push(() => res({ ok: true, status: 200, json: async () => body })));
        return { ok: true, status: 200, json: async () => body };
      };
      const p2 = adminAuditLoad(100);
      const p1 = adminAuditLoad(0);
      window.__done = p1.then(() => { pending.forEach(f => f()); return p2; }).then(() => {
        window.__firstActor = document.querySelector('#adminAuditTbody tr td:nth-child(2)').textContent;
        window.__info = document.getElementById('adminAuditPageInfo').textContent;
        window.__offset = _adminAuditOffset;
        window.__newerDisabled = document.getElementById('adminAuditNewer').disabled;
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__firstActor).toBe('a0');
    expect(win.__info).toBe('1–100 of 250');
    expect(win.__offset).toBe(0);
    expect(win.__newerDisabled).toBe(true);
  });

  it('#657: a failed load clears the pager info and re-derives the Newer/Older buttons', async () => {
    const boot = `
      _adminAuditOffset = 100; _adminAuditTotal = 250;
      document.getElementById('adminAuditPageInfo').textContent = '101–200 of 250';
      document.getElementById('adminAuditNewer').disabled = false;
      document.getElementById('adminAuditOlder').disabled = false;
      window.fetch = async () => { throw new Error('network down'); };
      window.__done = adminAuditLoad(0).then(() => {
        window.__info = document.getElementById('adminAuditPageInfo').textContent;
        window.__newer = document.getElementById('adminAuditNewer').disabled;
        window.__older = document.getElementById('adminAuditOlder').disabled;
        window.__row = document.getElementById('adminAuditTbody').textContent;
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__row).toContain('Failed to load audit log.');
    expect(win.__info).toBe('');
    expect(win.__newer).toBe(true);
    expect(win.__older).toBe(true);
  });

  it('#655: paging resets the column sort to server order', async () => {
    const boot = `
      _adminAuditOffset = 0; _adminAuditTotal = 250;
      _adminAuditSort = { key: 'actor', dir: -1 };
      window.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true, total: 250, entries: [{ actor: 'b' }, { actor: 'a' }] }) });
      adminAuditPage(1);
      window.__key = _adminAuditSort.key;
      window.__done = Promise.resolve().then(() => new Promise(r => setTimeout(r, 0))).then(() => {
        window.__order = [...document.querySelectorAll('#adminAuditTbody tr td:nth-child(2)')].map(td => td.textContent);
        window.__arrow = document.querySelector('#adminAuditTable th[data-key="actor"]').dataset.dir;
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__key).toBe(null);
    expect(win.__order).toEqual(['b', 'a']);
    expect(win.__arrow).toBeUndefined();
  });

  it('#656: ts sorts chronologically and IPv4 sorts numerically per octet', () => {
    const boot = `
      _adminAuditEntries = [
        { ts: '2026-08-27T10:00:00+00:00', ip: '192.168.1.10', actor: 'x' },
        { ts: '2026-08-27T09:00:00-05:00', ip: '192.168.1.9', actor: 'y' },
        { ts: '2026-08-27T08:00:00+00:00', ip: '10.0.0.1', actor: 'z' },
      ];
      const col = (n) => [...document.querySelectorAll('#adminAuditTbody tr td:nth-child(' + n + ')')].map(td => td.textContent);
      adminSortAudit(document.querySelector('#adminAuditTable th[data-key="ts"]'));
      window.__tsAsc = col(2);
      adminSortAudit(document.querySelector('#adminAuditTable th[data-key="ip"]'));
      window.__ipAsc = col(5);
    `;
    const win = run(boot);
    // 09:00-05:00 is 14:00Z, so chronological ascending is z, x, y (string order would be y, z, x).
    expect(win.__tsAsc).toEqual(['z', 'x', 'y']);
    expect(win.__ipAsc).toEqual(['10.0.0.1', '192.168.1.9', '192.168.1.10']);
  });

  it('#658: the admin auto-refresh tick reloads the audit page in place while the audit sub-tab is visible', () => {
    const boot = `
      window._activeTab = 'admin';
      window._subTabState = { admin: 'audit' };
      window.adminLoadAgents = () => {}; window.adminLoadHealth = () => {};
      window.__calls = [];
      window.adminAuditLoad = (o) => { window.__calls.push(o); };
      _adminAuditSort = { key: null, dir: 1 };
      _adminRefreshTick();
      _adminAuditSort = { key: 'actor', dir: 1 };
      _adminRefreshTick();
      window._subTabState.admin = 'agents';
      _adminAuditSort = { key: null, dir: 1 };
      _adminRefreshTick();
    `;
    const win = run(boot);
    expect(win.__calls).toEqual([undefined]);
  });
});
