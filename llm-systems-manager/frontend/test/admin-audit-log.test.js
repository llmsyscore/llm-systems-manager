// Admin → Audit Log module (#794): pure view helpers + the ledger harness
// (stale-fetch guard, filters → query, row click → detail, settings changes).
import { describe, it, expect, beforeAll } from 'vitest';
import { srcFile, runHarness } from './helpers/harness.js';

const auditSrc = srcFile('js/admin-audit.js');
const indexSrc = srcFile('index.html');
const panelHtml = (() => {
  const m = indexSrc.match(/<div id="admin-audit"[\s\S]*?<div id="admin-settings"/);
  if (!m) throw new Error('admin-audit panel not found');
  return m[0].replace(/<div id="admin-settings"$/, '');
})();
const escStub = 'window.adminEsc = s => String(s == null ? "" : s).replace(/[&<>"\']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"}[c]));';

function run(bootstrap) {
  return runHarness({ sources: [escStub, auditSrc], bootstrap, bodyHtml: panelHtml });
}
const entry = (i, over = {}) => ({ id: 1000 - i, ts: '2026-09-01T21:01:02-04:00', actor: 'llmadmin', role: 'admin',
  ip: '192.0.2.10', auth: 'session', method: 'POST', path: '/api/admin/users', action: 'user.create',
  target: 'adriel', status: 200, outcome: 'ok', group: 'user', label: 'Created a dashboard user', detail: null, ...over });

describe('AuditView pure helpers', () => {
  let AV;
  beforeAll(() => { process.env.TZ = 'America/New_York'; AV = run('').AuditView; });

  it('fmtShort renders short date · time with no locale "at"', () => {
    expect(AV.fmtShort('2026-09-01T21:01:02-04:00')).toBe('Sep 1 · 9:01 PM');
    expect(AV.fmtShort('2026-09-01T00:05:00-04:00')).toBe('Sep 1 · 12:05 AM');
    expect(AV.fmtShort('garbage')).toBe('—');
  });
  it('fmtFull carries the year and seconds', () => {
    expect(AV.fmtFull('2026-09-01T21:01:02-04:00')).toMatch(/^Sep 1, 2026 · 9:01:02 PM/);
  });
  it('age scales s → min → h → d', () => {
    const t = Date.parse('2026-09-01T21:00:00Z');
    expect(AV.age('2026-09-01T20:59:30Z', t)).toBe('30 s');
    expect(AV.age('2026-09-01T20:48:00Z', t)).toBe('12 min');
    expect(AV.age('2026-09-01T19:54:00Z', t)).toBe('1.1 h');
    expect(AV.age('2026-08-30T21:00:00Z', t)).toBe('2 d');
  });
  it('userCell names blank actors by how they got in', () => {
    expect(AV.userCell({ actor: '', auth: 'test' })).toContain('test harness');
    expect(AV.userCell({ actor: '', auth: 'internal', ip: '-' })).toContain('system');
    expect(AV.userCell({ actor: '', auth: 'bypass', ip: '127.0.0.1' })).toContain('local');
    expect(AV.userCell({ actor: 'autopilot' })).toContain('autopilot');
    expect(AV.userCell({ actor: 'adriel', role: 'operator' })).toContain('operator');
    expect(AV.userCell({ actor: '<b>', role: 'admin' })).toBe('&lt;b&gt;');
  });
  it('pageList collapses long ranges around the current page', () => {
    expect(AV.pageList(5, 20)).toEqual([1, '…', 4, 5, 6, '…', 20]);
    expect(AV.pageList(1, 3)).toEqual([1, 2, 3]);
    expect(AV.pageList(1, 1)).toEqual([1]);
    expect(AV.pageList(20, 20)).toEqual([1, '…', 19, 20]);
  });
  it('queryParams only carries non-default filters and the page window', () => {
    const p = AV.queryParams({ ...AV.DEFAULTS }, 25, 3);
    expect(p.get('since_hours')).toBe('168');
    expect(p.get('hide_automated')).toBe('1');
    expect(p.get('offset')).toBe('50');
    expect(p.has('sort')).toBe(false);
    const q = AV.queryParams({ ...AV.DEFAULTS, q: 'qwen', group: 'model', sort: 'actor', dir: 'asc', hours: 0, hideAuto: false }, null, 1);
    expect([...q.keys()].sort()).toEqual(['dir', 'group', 'q', 'sort']);
  });
  it('settingsChanges emits the five [manager.audit] keys with disabled events sorted', () => {
    const events = [{ key: 'auth.logout' }, { key: 'user.manage' }, { key: 'autopilot.executor' }];
    const ch = AV.settingsChanges({ retention: '45', pageSize: '50', saveAutomated: true, automatedActors: 'smoketestuser, bot smoketestuser',
      enabled: { 'auth.logout': false, 'user.manage': true, 'autopilot.executor': false } }, events);
    expect(ch).toEqual({
      'manager.audit.retention_days': 45, 'manager.audit.page_size': 50,
      'manager.audit.save_automated': true, 'manager.audit.automated_actors': ['smoketestuser', 'bot'],
      'manager.audit.disabled_events': ['auth.logout', 'autopilot.executor'],
    });
    expect(AV.parseActors('')).toEqual([]);
    expect(AV.parseActors(' a;b\n c ')).toEqual(['a', 'b', 'c']);
    expect(AV.settingsChanges({ retention: -3, pageSize: 2, enabled: {} }, [])['manager.audit.retention_days']).toBe(0);
    expect(AV.settingsChanges({ retention: 0, pageSize: 2, enabled: {} }, [])['manager.audit.page_size']).toBe(10);
  });
});

describe('audit ledger harness', () => {
  it('a slower older response never overwrites the page loaded after it', async () => {
    const boot = `
      let n = 0; const pending = [];
      window.fetch = async (url) => {
        const u = new URL(url, 'http://x');
        if (u.pathname.endsWith('/stats')) return { ok: true, json: async () => ({ ok: true, total: 2, actors: [] }) };
        const mine = ++n;
        const body = { ok: true, total: 2, page_size: 25, entries: [{ id: mine, ts: '2026-09-01T21:01:02Z', actor: 'resp' + mine, action: 'x', outcome: 'ok', group: 'config' }] };
        // First list request resolves only after the second has rendered.
        if (mine === 1) return new Promise(res => pending.push(() => res({ ok: true, json: async () => body })));
        return { ok: true, json: async () => body };
      };
      const p1 = adminAuditLoad(0);
      const p2 = adminAuditLoad(0);
      window.__done = p2.then(async () => { pending.forEach(f => f()); await p1; await new Promise(r => setTimeout(r, 0));
        window.__actor = document.querySelector('#adminAuditTbody tr td.u').textContent; });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__actor).toBe('resp2');
  });

  it('a failed load shows the error row and empty pager, then a good load recovers', async () => {
    const boot = `
      let fail = true;
      window.fetch = async (url) => {
        if (String(url).includes('/stats')) return { ok: true, json: async () => ({ ok: true, total: 0, actors: [] }) };
        if (fail) throw new Error('network down');
        return { ok: true, json: async () => ({ ok: true, total: 1, page_size: 25, entries: [${JSON.stringify(entry(0))}] }) };
      };
      window.__done = adminAuditLoad(0).then(() => {
        window.__err = document.getElementById('adminAuditTbody').textContent;
        window.__info = document.getElementById('auPageInfo').textContent;
        fail = false;
        return adminAuditLoad(0);
      }).then(() => { window.__row = document.getElementById('adminAuditTbody').textContent; });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__err).toContain('Failed to load audit log');
    expect(win.__info).toBe('0 entries');
    expect(win.__row).toContain('user.create');
    expect(win.__row).toContain('Sep 1 ·');
    expect(win.__row).not.toContain(' ago');
  });

  it('filters and the range segment land in the query string; Reset returns to defaults', async () => {
    const boot = `
      window.__urls = [];
      window.fetch = async (url) => {
        window.__urls.push(String(url));
        if (String(url).includes('/stats')) return { ok: true, json: async () => ({ ok: true, total: 0, actors: ['adriel'] }) };
        return { ok: true, json: async () => ({ ok: true, total: 0, page_size: 25, entries: [] }) };
      };
      window.__done = adminAuditLoad(0).then(async () => {
        document.getElementById('auGroup').value = 'model';
        document.getElementById('auGroup').dispatchEvent(new Event('change'));
        await new Promise(r => setTimeout(r, 0));
        document.querySelector('#auRange button[data-h="0"]').click();
        await new Promise(r => setTimeout(r, 0));
        window.__afterFilter = window.__urls.filter(u => u.includes('audit-log?')).pop();
        window.__resetIdle = document.getElementById('auReset').classList.contains('idle');
        document.getElementById('auReset').click();
        await new Promise(r => setTimeout(r, 0));
        window.__afterReset = window.__urls.filter(u => u.includes('audit-log?')).pop();
        window.__resetIdle2 = document.getElementById('auReset').classList.contains('idle');
        window.__export = document.getElementById('auExport').getAttribute('href');
        window.__actors = [...document.querySelectorAll('#auActor option')].map(o => o.value);
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__afterFilter).toContain('group=model');
    expect(win.__afterFilter).not.toContain('since_hours');
    expect(win.__resetIdle).toBe(false);
    expect(win.__afterReset).toContain('since_hours=168');
    expect(win.__afterReset).not.toContain('group=');
    expect(win.__resetIdle2).toBe(true);
    expect(win.__export).toMatch(/^\/api\/admin\/audit-log\.csv\?/);
    expect(win.__actors).toEqual(['', 'adriel', 'autopilot', 'system', 'local']);
  });

  it('clicking a row opens the detail panel with the changes diff; × closes it', async () => {
    const e = entry(0, { action: 'config.settings', group: 'config', label: 'Saved manager settings', target: null,
      detail: { changes: { 'manager.audit.retention_days': [30, 60] }, restart_required: [] } });
    const boot = `
      window.fetch = async (url) => {
        if (String(url).includes('/stats')) return { ok: true, json: async () => ({ ok: true, total: 1, actors: [] }) };
        return { ok: true, json: async () => ({ ok: true, total: 1, page_size: 25, entries: [${JSON.stringify(e)}] }) };
      };
      window.__done = adminAuditLoad(0).then(async () => {
        document.querySelector('#adminAuditTbody tr[data-id]').click();
        window.__open = document.getElementById('auSplit').classList.contains('detail');
        window.__det = document.getElementById('auDet').textContent;
        window.__selected = document.querySelector('#adminAuditTbody tr.sel') != null;
        document.querySelector('#auDet [data-close]').click();
        window.__closed = !document.getElementById('auSplit').classList.contains('detail');
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__open).toBe(true);
    expect(win.__selected).toBe(true);
    expect(win.__det).toContain('config.settings');
    expect(win.__det).toContain('manager.audit.retention_days');
    expect(win.__det).toContain('60');
    expect(win.__det).toContain('Sep 1, 2026');
    expect(win.__det).toMatch(/\d+ (s|min|h|d) ago/);
    expect(win.__closed).toBe(true);
  });

  it('an in-place refresh is skipped while a detail panel is open', async () => {
    const boot = `
      window.__n = 0;
      window.fetch = async (url) => {
        if (String(url).includes('/stats')) return { ok: true, json: async () => ({ ok: true, total: 1, actors: [] }) };
        window.__n++;
        return { ok: true, json: async () => ({ ok: true, total: 1, page_size: 25, entries: [${JSON.stringify(entry(0))}] }) };
      };
      window.__done = adminAuditLoad(0).then(async () => {
        await adminAuditLoad();            // tick refresh: allowed
        document.querySelector('#adminAuditTbody tr[data-id]').click();
        await adminAuditLoad();            // tick refresh with detail open: skipped
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__n).toBe(2);
  });

  it('the settings card is collapsed by default and renders the event toggles on expand', async () => {
    const boot = `
      window.fetch = async (url, opts) => {
        const u = String(url);
        if (u.includes('/stats')) return { ok: true, json: async () => ({ ok: true, total: 3, actors: [], purge: { ts: '2026-09-02T04:00:00Z', removed: 7338 } }) };
        if (u.includes('/events')) return { ok: true, json: async () => ({ ok: true, config: { retention_days: 60, page_size: 25, save_automated: false, automated_actors: ['smoketestuser'] },
          groups: [{ key: 'user', title: 'Users & access', events: [{ key: 'user.manage', label: 'Create / modify', default_on: true, enabled: true }, { key: 'auth.logout', label: 'Logout', default_on: false, enabled: false }] },
                   { key: 'auto', title: 'Autopilot', events: [{ key: 'autopilot.executor', label: 'Executor', default_on: true, enabled: true }] },
                   { key: 'config', title: 'Configuration', events: [{ key: 'admin.other', label: 'Other', default_on: true, enabled: true, hidden: true }] }] }) };
        if (u.includes('/api/admin/settings')) { window.__put = JSON.parse(opts.body); return { ok: true, json: async () => ({ ok: true, applied: [] }) }; }
        return { ok: true, json: async () => ({ ok: true, total: 3, page_size: 25, entries: [] }) };
      };
      window.__done = adminAuditLoad(0).then(async () => {
        window.__collapsed = document.getElementById('auCfg').classList.contains('collapsed');
        document.getElementById('auCfgHead').click();
        await new Promise(r => setTimeout(r, 0));
        const body = document.getElementById('auCfgBody');
        window.__labels = [...body.querySelectorAll('.mc-toggle .tlbl')].map(t => t.textContent);
        window.__logoutOn = body.querySelector('.mc-toggle[data-ev="auth.logout"]').classList.contains('on');
        window.__stat = body.querySelector('.au-stat').textContent;
        window.__foot = document.getElementById('auCfgFoot').textContent;
        body.querySelector('.mc-toggle[data-ev="auth.logout"]').click();
        body.querySelector('.mc-toggle[data-cfg]').click();
        document.getElementById('auCfgRet').value = '30';
        document.getElementById('auCfgRet').dispatchEvent(new Event('input'));
        window.__autoVal = document.getElementById('auCfgAuto').value;
        document.getElementById('auCfgAuto').value = 'smoketestuser, ci-bot';
        document.getElementById('auCfgAuto').dispatchEvent(new Event('input'));
        document.querySelector('#auCfgFoot [data-save]').click();
        await new Promise(r => setTimeout(r, 0));
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__collapsed).toBe(true);
    expect(win.__labels).toEqual(['Autopilot actions', 'Unit tests', 'Create / modify', 'Logout']);
    expect(win.__logoutOn).toBe(false);
    expect(win.__stat).toContain('7,338');
    expect(win.__foot).toContain('applies without restart');
    expect(win.__autoVal).toBe('smoketestuser');
    expect(win.__put).toEqual({ changes: {
      'manager.audit.retention_days': 30, 'manager.audit.page_size': 25,
      'manager.audit.save_automated': true, 'manager.audit.automated_actors': ['smoketestuser', 'ci-bot'],
      'manager.audit.disabled_events': [] } });
  });
});


describe('audit deep link', () => {
  it('the first load keeps the page and filters carried in the URL hash', async () => {
    const boot = `
      window.location.hash = '#audit?group=agent&page=3';
      window.__urls = [];
      window.fetch = async (url) => {
        window.__urls.push(String(url));
        if (String(url).includes('/stats')) return { ok: true, json: async () => ({ ok: true, total: 80, actors: [] }) };
        return { ok: true, json: async () => ({ ok: true, total: 80, page_size: 25, entries: [${JSON.stringify(entry(0))}] }) };
      };
      window.__done = adminAuditLoad(0).then(() => {
        window.__first = window.__urls.find(u => u.includes('audit-log?'));
        window.__group = document.getElementById('auGroup').value;
        return adminAuditLoad(0);
      }).then(() => { window.__second = window.__urls.filter(u => u.includes('audit-log?')).pop(); });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__first).toContain('group=agent');
    expect(win.__first).toContain('offset=50');
    expect(win.__group).toBe('agent');
    // A later sub-tab re-entry goes back to page 1 but keeps the filters.
    expect(win.__second).toContain('offset=0');
    expect(win.__second).toContain('group=agent');
  });
});
