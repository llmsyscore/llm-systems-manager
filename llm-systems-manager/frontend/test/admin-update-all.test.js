// #637/#1423: Admin → Agents "Update all" — the page starts the manager-side run, shows its output, and re-attaches.
// Co-loads the real admin.js in jsdom with fetch/confirm/toast stubbed; run order and retries are tested in the backend.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const adminSrc = readFileSync(join(here, '..', 'js', 'admin.js'), 'utf8');

const A1 = 'a'.repeat(32);
const A2 = 'b'.repeat(32);

function harness(bootstrap) {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(adminSrc);
  inject(bootstrap);
  return dom.window;
}

// polls: successive GET replies for the job; post: the POST reply ({status, body}); start: the call to run.
function boot({ agents = [], post, polls = [], getLatest = null, confirm = true, start = 'adminUpdateAll()' }) {
  return `
    _adminAgentsCache = ${JSON.stringify(agents)};
    _latestAgentVersion = 'v2';
    window.__calls = { posts: 0, gets: [], toasts: [], reloads: 0 };
    _themedConfirm = async () => ${JSON.stringify(confirm)};
    _themedToast = (m) => { window.__calls.toasts.push(m); };
    adminLoadAgents = async () => { window.__calls.reloads++; };
    _ADMIN_UPDATE_ALL_POLL_MS = 0;
    const __post = ${JSON.stringify(post || null)};
    const __polls = ${JSON.stringify(polls)};
    const __latest = ${JSON.stringify(getLatest)};
    const reply = (status, body) => ({ ok: status < 400, status, json: async () => body });
    fetch = async (url, opts) => {
      if (opts && opts.method === 'POST') { window.__calls.posts++; return reply(__post.status, __post.body); }
      if (url.startsWith('/api/agents/update-all')) window.__calls.gets.push(url);
      if (!url.includes('job=')) return reply(200, __latest || { ok: true, job: null, lines: [], next: 0 });
      return reply(200, __polls.length > 1 ? __polls.shift() : __polls[0]);
    };
    window.__P = ${start};
  `;
}

const agent = (aid, host, upd = true) =>
  ({ agent_id: aid, hostname: host, version: 'v1', update_available: upd, status: 'approved' });
const line = (text, level = '') => ({ t: '20:01:02', text, level });
const job = (status, extra = {}) => ({ id: 'j1', status, message: '', state: { total: 2 }, ...extra });

function panelText(win) {
  const el = win.document.getElementById('adminUpdateLog');
  return el ? el.textContent : '';
}

describe('#1423 adminUpdateAll', () => {
  test('starts the run on the manager and shows its output until it ends', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1'), agent(A2, 'h2')],
      post: { status: 200, body: { ok: true, job: job('queued'), lines: [], next: 0, todo: [{}, {}] } },
      polls: [
        { ok: true, job: job('running'), lines: [line('── [1/2] h1: v1 → v2', 'stage')], next: 1 },
        { ok: true, job: job('running'), lines: [line('✓ h1 is back on v2', 'ok')], next: 2 },
        { ok: true, job: job('done'), lines: [line('── Update all finished: 2 updated', 'ok')], next: 3 },
      ],
    }));
    await win.__P;
    expect(win.__calls.posts).toBe(1);
    expect(win.__calls.gets).toEqual([
      '/api/agents/update-all?job=j1&after=0', '/api/agents/update-all?job=j1&after=1',
      '/api/agents/update-all?job=j1&after=2']);
    const text = panelText(win);
    expect(text).toContain('[20:01:02] ── [1/2] h1: v1 → v2');
    expect(text).toContain('✓ h1 is back on v2');
    expect(text).toContain('Update all finished: 2 updated');
    expect(win.document.getElementById('adminUpdateTitle').textContent).toBe('all agents (2)');
    expect(win.__calls.reloads).toBe(1);
    expect(win.eval('_adminUpdateBusy')).toBe(false);
  });

  test('a refused start shows the reason and opens nothing', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1')],
      post: { status: 409, body: { ok: false, error: 'an Update all run is already going' } },
    }));
    await win.__P;
    expect(win.__calls.toasts.join(' ')).toContain('an Update all run is already going');
    expect(win.document.getElementById('adminUpdatePanel')).toBeNull();
  });

  test('a cancelled run says so', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1')],
      post: { status: 200, body: { ok: true, job: job('queued'), todo: [{}] } },
      polls: [{ ok: true, job: job('cancelled'), lines: [], next: 0 }],
    }));
    await win.__P;
    expect(panelText(win)).toContain('Update all was cancelled');
  });

  test('a lost session stops watching and says the run carries on', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1')],
      post: { status: 200, body: { ok: true, job: job('queued'), todo: [{}] } },
      polls: [],
      start: `(fetch = ((f) => async (url, opts) => (url.includes('job=') ? ({ ok: false, status: 401, json: async () => ({}) }) : f(url, opts)))(fetch), adminUpdateAll())`,
    }));
    await win.__P;
    expect(panelText(win)).toContain('lost contact with the manager');
    expect(win.eval('_adminUpdateBusy')).toBe(false);
  });

  test('nothing to do → toast, no request', async () => {
    const win = harness(boot({ agents: [agent(A1, 'h1', false)] }));
    await win.__P;
    expect(win.__calls.toasts.length).toBe(1);
    expect(win.__calls.posts).toBe(0);
  });

  test('declined confirm starts nothing', async () => {
    const win = harness(boot({ agents: [agent(A1, 'h1')], confirm: false }));
    await win.__P;
    expect(win.__calls.posts).toBe(0);
    expect(win.document.getElementById('adminUpdatePanel')).toBeNull();
  });
});

describe('#1423 adminResumeUpdateAll', () => {
  test('while a run is watched, Update all and re-entering Admin show the hidden window again', async () => {
    const win = harness(boot({
      agents: [agent(A1, 'h1')],
      post: { status: 200, body: { ok: true, job: job('queued'), todo: [{}] } },
      polls: [{ ok: true, job: job('running'), lines: [line('step one')], next: 1 }],
      start: 'adminUpdateAll()',
    }));
    await new Promise(r => setTimeout(r, 20));
    const panel = win.document.getElementById('adminUpdatePanel');
    expect(win.document.getElementById('adminUpdateLog').textContent).toContain('step one');
    win.eval('_adminUpdateClose()');
    expect(panel.style.display).toBe('none');
    await win.eval('adminResumeUpdateAll()');
    expect(panel.style.display).toBe('flex');
    win.eval('_adminUpdateClose()');
    await win.eval('adminUpdateAll()');
    expect(panel.style.display).toBe('flex');
    expect(win.__calls.posts).toBe(1);
    expect(win.document.getElementById('adminUpdateLog').textContent).toContain('step one');
  });

  test('re-opens the output window for a run that is still going', async () => {
    const win = harness(boot({
      getLatest: { ok: true, job: job('running'), lines: [], next: 0 },
      polls: [{ ok: true, job: job('done'), lines: [line('earlier'), line('── Update all finished: 2 updated')], next: 2 }],
      start: 'adminResumeUpdateAll()',
    }));
    await win.__P;
    expect(win.__calls.gets[0]).toBe('/api/agents/update-all');
    expect(win.__calls.gets[1]).toBe('/api/agents/update-all?job=j1&after=0');
    expect(panelText(win)).toContain('earlier');
    expect(win.document.getElementById('adminUpdateTitle').textContent).toBe('all agents (2)');
  });

  test('a finished run is not re-opened', async () => {
    const win = harness(boot({
      getLatest: { ok: true, job: job('done'), lines: [], next: 0 },
      start: 'adminResumeUpdateAll()',
    }));
    await win.__P;
    expect(win.__calls.gets).toEqual(['/api/agents/update-all']);
    expect(win.document.getElementById('adminUpdatePanel')).toBeNull();
  });
});
