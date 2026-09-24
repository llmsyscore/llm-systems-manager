// Admin → Jobs ledger (#1038): pure helpers, filters → query, paging, detail panel with audit rows and actions.
import { describe, it, expect, beforeAll } from 'vitest';
import { srcFile, runHarness } from './helpers/harness.js';

const jobsSrc = srcFile('js/admin-jobs.js');
const indexSrc = srcFile('index.html');
const panelHtml = (() => {
  const m = indexSrc.match(/<div id="admin-jobs"[\s\S]*?<!-- Settings sub-tab/);
  if (!m) throw new Error('admin-jobs panel not found');
  return m[0].replace(/<!-- Settings sub-tab$/, '');
})();
const escStub = 'window.adminEsc = s => String(s == null ? "" : s).replace(/[&<>"\']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"}[c]));';
const T = Date.parse('2026-09-01T21:01:00-04:00') / 1000;
const job = (i, over = {}) => ({ id: 'j' + i, kind: 'tower_timer', kind_title: 'Tower timer', label: 'Job ' + i, status: 'done',
  user: 'adriel', source: 'tower', created: T, resolved: T + 60, message: 'done', can_cancel: false, can_ack: false, ...over });

function run(bootstrap) {
  return runHarness({ sources: [escStub, jobsSrc], bootstrap, bodyHtml: panelHtml });
}
const tick = 'await new Promise(r => setTimeout(r, 0));';

describe('JobsView pure helpers', () => {
  let JV;
  beforeAll(() => { process.env.TZ = 'America/New_York'; JV = run('').JobsView; });

  it('fmtTs renders epoch seconds as short date · time', () => {
    expect(JV.fmtTs(T)).toBe('Sep 1 · 9:01 PM');
    expect(JV.fmtTs(null)).toBe('—');
  });
  it('jobWhen picks next run, start or end by status', () => {
    expect(JV.jobWhen({ status: 'queued', next_run: T })).toBe('next Sep 1 · 9:01 PM');
    expect(JV.jobWhen({ status: 'queued' })).toBe('waiting');
    expect(JV.jobWhen({ status: 'running', started: T })).toBe('started Sep 1 · 9:01 PM');
    expect(JV.jobWhen({ status: 'failed', resolved: T })).toBe('Sep 1 · 9:01 PM');
  });
  it('jobBy falls back to the source, pillClass maps each status', () => {
    expect(JV.jobBy({ user: '', source: 'system' })).toBe('system');
    expect(JV.jobBy({})).toBe('—');
    expect(['queued', 'running', 'done', 'failed', 'cancelled', 'x'].map(JV.pillClass))
      .toEqual(['queued', 'ok', 'done', 'error', 'refused', 'refused']);
  });
  it('queryParams carries status, filters and the page window', () => {
    const p = JV.queryParams({ status: 'all', kind: '', user: '' }, 25, 3);
    expect(Object.fromEntries(p)).toEqual({ status: 'all', limit: '25', offset: '50', facets: '1' });
    const q = JV.queryParams({ status: 'failed', kind: 'tower_timer', user: 'bob' }, null, 1);
    expect(Object.fromEntries(q)).toEqual({ status: 'failed', kind: 'tower_timer', user: 'bob' });
  });
});

describe('jobs ledger harness', () => {
  it('renders rows, fills kind/user filters and sends filter changes in the query', async () => {
    const boot = `
      window.__urls = [];
      window.fetch = async (url) => {
        window.__urls.push(String(url));
        return { ok: true, json: async () => ({ ok: true, total: 2, jobs: [${JSON.stringify(job(1))}, ${JSON.stringify(job(2, { status: 'failed', message: '<b>boom</b>' }))}],
          kinds: [{ name: 'tower_timer', title: 'Tower timer' }], users: ['adriel', 'bob'] }) };
      };
      window.__done = adminJobsLoad(0).then(async () => {
        window.__rows = document.querySelectorAll('#jbTbody tr[data-id]').length;
        window.__html = document.getElementById('jbTbody').innerHTML;
        window.__kinds = [...document.querySelectorAll('#jbKind option')].map(o => o.value);
        window.__users = [...document.querySelectorAll('#jbUser option')].map(o => o.value);
        const s = document.getElementById('jbStatus'); s.value = 'failed'; s.dispatchEvent(new Event('change')); ${tick}
        const u = document.getElementById('jbUser'); u.value = 'bob'; u.dispatchEvent(new Event('change')); ${tick}
        window.__reset = document.getElementById('jbReset').classList.contains('idle');
        document.getElementById('jbReset').click(); ${tick}
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__rows).toBe(2);
    expect(win.__html).toContain('&lt;b&gt;boom&lt;/b&gt;');
    expect(win.__kinds).toEqual(['', 'tower_timer']);
    expect(win.__users).toEqual(['', 'adriel', 'bob']);
    expect(win.__urls[0]).toBe('/api/jobs?status=all&limit=25&offset=0&facets=1');
    expect(win.__urls[1]).toBe('/api/jobs?status=failed&limit=25&offset=0&facets=1');
    expect(win.__urls[2]).toBe('/api/jobs?status=failed&user=bob&limit=25&offset=0&facets=1');
    expect(win.__reset).toBe(false);
    expect(win.__urls[3]).toBe('/api/jobs?status=all&limit=25&offset=0&facets=1');
  });

  it('paging requests the next offset', async () => {
    const boot = `
      window.__urls = [];
      window.fetch = async (url) => { window.__urls.push(String(url));
        return { ok: true, json: async () => ({ ok: true, total: 60, jobs: [${JSON.stringify(job(1))}], kinds: [], users: [] }) }; };
      window.__done = adminJobsLoad(0).then(async () => {
        window.__info = document.getElementById('jbPageInfo').textContent;
        document.querySelector('#jbPnums button[data-go="2"]').click(); ${tick}
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__info).toBe('1–25 of 60');
    expect(win.__urls[1]).toBe('/api/jobs?status=all&limit=25&offset=25&facets=1');
  });

  it('a row click loads the detail with spec, audit rows and a Cancel button that posts', async () => {
    const live = job(3, { status: 'queued', next_run: T + 600, resolved: null, can_cancel: true });
    const boot = `
      window.__posts = [];
      window.fetch = async (url, opts) => {
        const u = String(url);
        if (opts && opts.method === 'POST') { window.__posts.push(u); return { ok: true, json: async () => ({ ok: true }) }; }
        if (u.startsWith('/api/jobs/j3')) return { ok: true, json: async () => ({ ok: true, job: { ...${JSON.stringify(live)},
          spec: { hours: 2 }, state: {}, result: null,
          audit: [{ ts: '2026-09-01T21:01:00-04:00', action: 'jobs.submit', actor: 'adriel', outcome: 'ok' }] } }) };
        return { ok: true, json: async () => ({ ok: true, total: 1, jobs: [${JSON.stringify(live)}], kinds: [], users: [] }) };
      };
      window.__done = adminJobsLoad(0).then(async () => {
        document.querySelector('#jbTbody tr[data-id="j3"]').click(); ${tick} ${tick}
        window.__det = document.getElementById('jbDet').textContent;
        window.__split = document.getElementById('jbSplit').classList.contains('detail');
        document.querySelector('#jbDet [data-act="cancel"]').click(); ${tick} ${tick} ${tick}
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__split).toBe(true);
    expect(win.__det).toContain('"hours": 2');
    expect(win.__det).toContain('jobs.submit · adriel · ok');
    expect(win.__det).toContain('Cancel job');
    expect(win.__posts).toEqual(['/api/jobs/j3/cancel']);
  });

  it('a refused cancel shows the server error in the panel', async () => {
    const live = job(3, { status: 'queued', next_run: T + 600, resolved: null, can_cancel: true });
    const boot = `
      window.fetch = async (url, opts) => {
        const u = String(url);
        if (opts && opts.method === 'POST') return { ok: false, status: 409, json: async () => ({ ok: false, error: 'job is done' }) };
        if (u.startsWith('/api/jobs/j3')) return { ok: true, json: async () => ({ ok: true, job: { ...${JSON.stringify(live)}, spec: {}, state: {}, result: null, audit: null } }) };
        return { ok: true, json: async () => ({ ok: true, total: 1, jobs: [${JSON.stringify(live)}], kinds: [], users: [] }) };
      };
      window.__done = adminJobsLoad(0).then(async () => {
        document.querySelector('#jbTbody tr[data-id="j3"]').click(); ${tick} ${tick}
        document.querySelector('#jbDet [data-act="cancel"]').click(); ${tick} ${tick} ${tick} ${tick}
        window.__err = (document.querySelector('#jbDet .jb-err') || {}).textContent;
        window.__open = document.getElementById('jbSplit').classList.contains('detail');
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__err).toBe('job is done');
    expect(win.__open).toBe(true);
  });

  it('a filter value missing from the options is kept as its own option', async () => {
    const boot = `
      window.fetch = async () => ({ ok: true, json: async () => ({ ok: true, total: 0, jobs: [], kinds: [], users: ['adriel'] }) });
      JobsView._state.user = 'bob';
      window.__done = adminJobsLoad(-1).then(() => { window.__val = document.getElementById('jbUser').value; });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__val).toBe('bob');
  });

  it('non-admin detail (audit null) omits the Audit section', async () => {
    const boot = `
      window.fetch = async (url) => String(url).startsWith('/api/jobs/j1')
        ? { ok: true, json: async () => ({ ok: true, job: { ...${JSON.stringify(job(1))}, spec: {}, state: {}, result: null, audit: null } }) }
        : { ok: true, json: async () => ({ ok: true, total: 1, jobs: [${JSON.stringify(job(1))}], kinds: [], users: [] }) };
      window.__done = adminJobsLoad(0).then(async () => {
        document.querySelector('#jbTbody tr[data-id="j1"]').click(); ${tick} ${tick}
        window.__det = document.getElementById('jbDet').textContent;
      });
    `;
    const win = run(boot);
    await win.__done;
    expect(win.__det).toContain('Spec');
    expect(win.__det).not.toContain('Audit');
    expect(win.__det).not.toContain('Cancel job');
  });
});
