// #897: the queue slot follows the server's tool_run jobs instead of holding a payload in the tab.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const BODY = `
  <span id="toolsRunDot"></span>
  <div id="toolsHome"><div id="toolsLauncher"></div></div>
  <div id="toolsLedgerBody"></div>
`;
const STUBS = `
  window.layout = { toolsView: 'card' };
  window.saveLayout = function () {};
  window._claim = function () { return true; };
  window._release = function () {};
  window._me = { username: 'alice' };
`;
const AGENTS = { llama: [{ agent_id: 'a1', hostname: 'gpu-01', is_default: true },
                         { agent_id: 'a2', hostname: 'gpu-02' }] };
const AT_ON_A1 = { reportcard: false, benchmark: false, autotune: true, quality: false,
                   agents: { a1: ['autotune'] }, queue: {} };
const row = (id, tool, user, status = 'queued') => ({ job_id: id, tool, provider: 'llama', model_id: 'org/m', user, mine: user === 'alice', status, created: 1 });

function boot(activity, bootstrap = '') {
  const win = runHarness({
    sources: [STUBS, srcFile('js/lib/modelcards.js'), srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
    bodyHtml: BODY,
    bootstrap: `
      window.__activity = ${JSON.stringify(activity)};
      window.__posts = [];
      window._fetchT = (url, opts) => {
        if (opts && opts.method === 'POST') { window.__posts.push(url); return Promise.resolve({ ok: true, json: () => Promise.resolve(window.__cancelAnswer || { ok: true }) }); }
        return Promise.resolve({ ok: true, json: () => Promise.resolve(
          url.indexOf('/api/tools/activity') === 0 ? window.__activity
          : url.indexOf('/api/agents/list-by-provider') === 0 ? ${JSON.stringify(AGENTS)} : {}) });
      };
      initToolsTab();
      window.__done = toolsPollActivity().then(() => {
        window.__rendered = []; window.__attached = []; window.__dropped = 0;
        window.__slot = toolsQueueSlot('autotune', {
          provider: () => 'llama',
          render: (st) => window.__rendered.push(st),
          attach: (r) => window.__attached.push(r),
          dropped: () => { window.__dropped++; },
        });
        ${bootstrap}
      });
    `,
  });
  return win.__done.then(() => flush()).then(() => flush()).then(() => win);
}
const last = (win) => win.__rendered[win.__rendered.length - 1];
async function poll(win, activity) { win.__activity = activity; await win.toolsPollActivity(); await flush(); await flush(); }

describe('toolsQueueSlot on the server queue (#897)', () => {
  it('started() defers attach until the running row qualifies, and stamps the host (#1136)', async () => {
    const win = await boot(AT_ON_A1, `
      window.__rcAt = [];
      window.__rc = toolsQueueSlot('reportcard', { provider: () => 'llama', started: r => !!r.run_id,
        attach: r => window.__rcAt.push(r), dropped: () => { window.__dropped++; } });
    `);
    win.__rc.hold('c1', { position: 1 });
    await poll(win, { ...AT_ON_A1, queue: { a1: [{ ...row('c1', 'reportcard', 'alice', 'running'), run_id: '' }] } });
    expect(win.__rcAt).toHaveLength(0);
    await poll(win, { ...AT_ON_A1, queue: { a1: [{ ...row('c1', 'reportcard', 'alice', 'running'), run_id: 'rc9' }] } });
    expect(win.__rcAt).toHaveLength(1);
    expect(win.__rcAt[0]).toMatchObject({ job_id: 'c1', run_id: 'rc9', agent_id: 'a1' });
    expect(win.__rc.queued()).toBe(false);
    expect(win.__dropped).toBe(0);
  });

  it('keeps the provisional hold until the row appears', async () => {
    const win = await boot(AT_ON_A1);
    win.__slot.hold('j1', { position: 2, wait_for: 'Autotune on gpu-01' });
    expect(win.__slot.queued()).toBe(true);
    expect(last(win).ahead).toBe(1);
    expect(last(win).waitFor).toBe('Autotune on gpu-01');
    await poll(win, AT_ON_A1);                      // row not there yet — still held
    expect(win.__slot.queued()).toBe(true);
    expect(win.__dropped).toBe(0);
  });

  it('reads position and ahead from the snapshot once the row is there', async () => {
    const win = await boot(AT_ON_A1);
    win.__slot.hold('j2', { position: 2, wait_for: 'Autotune on gpu-01' });
    await poll(win, { ...AT_ON_A1, queue: { a1: [row('j1', 'benchmark', 'bob'), row('j2', 'autotune', 'alice')] } });
    expect(last(win)).toMatchObject({ queued: true, mine: true, job_id: 'j2', ahead: 1, position: 2, others: 1 });
  });

  it('fires attach once when the held job goes running', async () => {
    const win = await boot(AT_ON_A1);
    win.__slot.hold('j2', { position: 1 });
    await poll(win, { ...AT_ON_A1, queue: { a1: [row('j2', 'autotune', 'alice')] } });
    await poll(win, { ...AT_ON_A1, queue: { a1: [row('j2', 'autotune', 'alice', 'running')] } });
    expect(win.__attached).toHaveLength(1);
    expect(win.__attached[0].job_id).toBe('j2');
    expect(win.__slot.queued()).toBe(false);
    await poll(win, { ...AT_ON_A1, queue: { a1: [row('j2', 'autotune', 'alice', 'running')] } });
    expect(win.__attached).toHaveLength(1);
  });

  it('fires dropped when the held row vanishes before running', async () => {
    const win = await boot(AT_ON_A1);
    win.__slot.hold('j2', { position: 1 });
    await poll(win, { ...AT_ON_A1, queue: { a1: [row('j2', 'autotune', 'alice')] } });
    await poll(win, AT_ON_A1);
    expect(win.__dropped).toBe(1);
    expect(win.__slot.queued()).toBe(false);
  });

  it('drop posts the job cancel and clears the hold', async () => {
    const win = await boot(AT_ON_A1);
    win.__slot.hold('j2', { position: 1 });
    await poll(win, { ...AT_ON_A1, queue: { a1: [row('j2', 'autotune', 'alice')] } });
    expect(win.__slot.drop()).toBe(true);
    expect(win.__posts).toEqual(['/api/jobs/j2/cancel']);
    expect(win.__slot.queued()).toBe(false);
    expect(win.__slot.drop()).toBe(false);
  });

  it('adopts the current user’s queued row for its tool on this host', async () => {
    const win = await boot({ ...AT_ON_A1, queue: { a1: [row('j7', 'autotune', 'alice')] } });
    expect(win.__slot.queued()).toBe(true);
    expect(win.__slot.jobId()).toBe('j7');
    await poll(win, { ...AT_ON_A1, queue: { a1: [row('j7', 'autotune', 'alice', 'running')] } });
    expect(win.__attached).toHaveLength(1);
  });

  it('does not adopt a row queued for another provider', async () => {
    const win = await boot({ ...AT_ON_A1, queue: { a1: [{ ...row('j9', 'autotune', 'alice'), provider: 'vllm' }] } });
    expect(win.__slot.queued()).toBe(false);
    expect(win.__slot.jobId()).toBe(null);
  });

  it('does not adopt another user’s row but counts it', async () => {
    const win = await boot({ ...AT_ON_A1, queue: { a1: [row('j8', 'autotune', 'bob')] } });
    expect(win.__slot.queued()).toBe(false);
    expect(last(win)).toMatchObject({ queued: false, others: 1 });
    expect(win.toolsQueueText(last(win), 'run')).toBe('Autotune is running on gpu-01. 1 run queued on this host.');
  });

  it('words the notice for the holder', async () => {
    const win = await boot(AT_ON_A1);
    win.__slot.hold('j2', { position: 3, wait_for: 'Autotune on gpu-01' });
    expect(win.toolsQueueText(last(win), 'run')).toBe('2 runs queued ahead of you — this run starts on its own when they finish.');
    win.__slot.drop();
    win.__slot.hold('j3', { position: 1, wait_for: 'Autotune on gpu-01' });
    expect(win.toolsQueueText(last(win), 'check')).toBe('Queued behind Autotune on gpu-01 — this check starts on its own when that finishes.');
  });

  it('counts queued rows per tool for the tiles', async () => {
    const win = await boot({ ...AT_ON_A1, queue: { a1: [row('j1', 'benchmark', 'bob'), row('j2', 'benchmark', 'alice')],
                                                   a2: [row('j3', 'quality', 'bob')] } });
    expect(win.toolsQueuedCount('benchmark')).toBe(2);
    expect(win.toolsQueuedCount('quality')).toBe(1);
    expect(win.toolsQueuedCount('autotune')).toBe(0);
    const html = win.document.getElementById('toolsLauncher').innerHTML;
    expect(html).toContain('2 queued');
  });
  it('adopts a row only in the slot whose match accepts it', async () => {
    const live = { ...row('j4', 'benchmark', 'alice'), path: '/llama/bench/live/run' };
    const win = await boot(AT_ON_A1, `
      window.__liveAt = []; window.__offAt = [];
      window.__off = toolsQueueSlot('benchmark:offline', { provider: () => 'llama',
        match: r => !/\\/bench\\/live\\//.test(r.path || ''), attach: r => window.__offAt.push(r) });
      window.__live = toolsQueueSlot('benchmark', { provider: () => 'llama',
        match: r => /\\/bench\\/live\\//.test(r.path || ''), attach: r => window.__liveAt.push(r) });
    `);
    await poll(win, { ...AT_ON_A1, queue: { a1: [live] } });
    expect(win.__live.jobId()).toBe('j4');
    expect(win.__off.queued()).toBe(false);
    await poll(win, { ...AT_ON_A1, queue: { a1: [{ ...live, status: 'running' }] } });
    expect(win.__liveAt).toHaveLength(1);
    expect(win.__offAt).toHaveLength(0);
  });

  it('does not adopt a row another slot on the same tool holds', async () => {
    const win = await boot(AT_ON_A1, `
      window.__a = toolsQueueSlot('benchmark', { provider: () => 'llama' });
      window.__b = toolsQueueSlot('benchmark:offline', { provider: () => 'llama' });
      window.__a.hold('j5', { position: 1 });
    `);
    await poll(win, { ...AT_ON_A1, queue: { a1: [row('j5', 'benchmark', 'alice')] } });
    expect(win.__a.jobId()).toBe('j5');
    expect(win.__b.queued()).toBe(false);
  });

  it('ends a provisional hold after five polls without its row', async () => {
    const win = await boot(AT_ON_A1);
    win.__slot.hold('j9', { position: 1 });
    await flush(); await flush();                    // hold's own poll is the first
    for (let i = 0; i < 3; i++) await poll(win, AT_ON_A1);
    expect(win.__dropped).toBe(0);
    expect(win.__slot.queued()).toBe(true);
    await poll(win, AT_ON_A1);
    expect(win.__dropped).toBe(1);
    expect(win.__slot.queued()).toBe(false);
    await poll(win, AT_ON_A1);
    expect(win.__dropped).toBe(1);
  });

  it('pins the hold to the host the 202 answer names', async () => {
    const win = await boot(AT_ON_A1);
    win.__slot.hold('j1', { position: 1, agent_id: 'a2' });
    await poll(win, { ...AT_ON_A1, queue: { a2: [row('j1', 'autotune', 'alice', 'running')] } });
    expect(win.__attached).toHaveLength(1);
    expect(win.__attached[0].job_id).toBe('j1');
  });

  it('flags a busy host whose tools/state cannot be read so modules keep the plain Run label (#1132)', async () => {
    const win = await boot({ ...AT_ON_A1, unqueueable: ['a1'] });
    expect(last(win).busy && last(win).busy.queueable).toBe(false);
    expect(last(win).canQueue).toBe(false);
    await poll(win, AT_ON_A1);
    expect(last(win).busy.queueable).toBe(true);
    expect(last(win).canQueue).toBe(true);
  });

  it('re-holds the job when the cancel is refused', async () => {
    const win = await boot(AT_ON_A1);
    win.__slot.hold('j2', { position: 1 });
    await poll(win, { ...AT_ON_A1, queue: { a1: [row('j2', 'autotune', 'alice')] } });
    win.__cancelAnswer = { ok: false };
    expect(win.__slot.drop()).toBe(true);
    await flush(); await flush();
    expect(win.__slot.queued()).toBe(true);
    expect(win.__slot.jobId()).toBe('j2');
    await poll(win, { ...AT_ON_A1, queue: { a1: [row('j2', 'autotune', 'alice', 'running')] } });
    expect(win.__attached).toHaveLength(1);
    expect(win.__slot.queued()).toBe(false);
  });
});
