// #891: Fleet batch group + Batch pane — host list, start body, resume, poll, lock, cancel.
import { describe, it, expect, vi } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const INDEX = srcFile('index.html');
const BODY = INDEX.slice(INDEX.indexOf('<div id="toolsModAt"'), INDEX.indexOf('<!-- /toolsModAt -->'));

const A1 = 'a'.repeat(32), A2 = 'b'.repeat(32);
const HOSTS = { ok: true, hosts: [
  { agent_id: A1, hostname: 'alpha', online: true, busy: false, models: ['org/m:Q4', 'org/n:Q8'] },
  { agent_id: A2, hostname: 'bravo', online: false, busy: false, models: [] },
] };
const PRE = { ok: true, busy: false, unit_active: false, cores: { physical: 16, logical: 32 }, perplexity: true,
  runtime: { ok: true }, drafts: [], sizes: { 'org/m:Q4': 18e9 }, drafts_for: {}, vram_total_mb: 32768, ram_total_mb: 32768 };

function batchDoc(status, extra = {}) {
  return { id: 'b1', status, start_at: 1, started: 1, finished: null, budget_min: 480, objective: 'balanced', current: 0,
    items: [{ agent_id: A1, hostname: 'alpha', model_id: 'org/m:Q4', status: status === 'queued' ? 'queued' : 'running', gain_pct: null, ctx: null, applied: false, note: null },
            { agent_id: A1, hostname: 'alpha', model_id: 'org/n:Q8', status: 'queued', gain_pct: null, ctx: null, applied: false, note: null }],
    summary: null, error: null, ...extra };
}

const LAYOUT = `let layout = {}; window.__layout = () => layout; window.saveLayout = function () {};`;
const STUBS = `
  window.TC = { esc: (s) => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])) };
  window.SG = { open: (opts) => { window.__sse = opts; return { close() { window.__closed = true; } }; } };
  window.toolsSyncRunDot = function () {};
  window.__alerts = []; window.alert = function (m) { window.__alerts.push(String(m)); };
  window.__fetches = [];
  window.__pre = ${JSON.stringify(PRE)};
  window.__hosts = ${JSON.stringify(HOSTS)};
  window.__batch = null; window.__batches = { ok: true, batches: [] };
  window.fetch = function (url, opts) {
    window.__fetches.push([String(url), opts]);
    const u = String(url);
    const body = u.startsWith('/api/llm/autotune/preflight') ? window.__pre
      : u.startsWith('/api/llm/autotune/status') ? { ok: true, items: [] }
      : u.startsWith('/api/benchmark/models') ? { models: ['org/m:Q4'] }
      : u.startsWith('/api/tools/runs') ? { runs: [], latest: {} }
      : u.startsWith('/api/llama-state') ? { state: 'stopped' }
      : u.startsWith('/api/energy/host-peak') ? { ok: true, peak_w: 300, peak_active_w: 280 }
      : u.startsWith('/api/llm/autotune/batch-hosts') ? window.__hosts
      : u.startsWith('/api/llm/autotune/batches') ? window.__batches
      : (u === '/api/llm/autotune/batch' && opts && opts.method === 'POST') ? (window.__startReply || { ok: true, batch: window.__batch })
      : u.match(/^\\/api\\/llm\\/autotune\\/batch\\/[^/]+\\/cancel$/) ? { ok: true, status: 'running' }
      : u.startsWith('/api/llm/autotune/batch/') ? (window.__batch ? { ok: true, batch: window.__batch } : { ok: false, error: 'not found' })
      : { ok: true };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
  };
`;

function boot(bootstrap = '') {
  return runHarness({ sources: [LAYOUT, STUBS, srcFile('js/autotune.js')], bodyHtml: BODY, bootstrap });
}
async function opened(pre = '') {
  const w = boot(pre + ' window.AT.onOpen();');
  await flush(w); await flush(w); await flush(w);
  return w;
}
const posts = w => w.__fetches.filter(([u, o]) => u === '/api/llm/autotune/batch' && o && o.method === 'POST');

describe('Fleet batch group', () => {
  it('lists each online host with its models as toggles and tags offline hosts', async () => {
    const w = await opened();
    const list = w.document.getElementById('atBatchHosts');
    expect(list.querySelectorAll('.at-bhost').length).toBe(2);
    expect(list.querySelectorAll(`[data-batch-agent="${A1}"]`).length).toBe(2);
    expect(list.querySelectorAll(`[data-batch-agent="${A2}"]`).length).toBe(0);
    expect(list.textContent).toContain('bravo');
    expect(list.textContent).toContain('offline');
    expect(w.document.getElementById('atBatchCount').textContent).toBe('0 queued');
  });

  it('parses Start at as the next occurrence of HH:MM', async () => {
    const w = await opened();
    const now = Date.parse('2026-09-10T20:00:00');
    const later = w.AT.batchStartAt('23:30', now);
    expect(later).toBeGreaterThan(now / 1000);
    expect(later - now / 1000).toBe(3.5 * 3600);
    const tomorrow = w.AT.batchStartAt('01:00', now);
    expect(tomorrow - now / 1000).toBe(5 * 3600);
    expect(w.AT.batchStartAt('', now)).toBe(null);
    expect(w.AT.batchStartAt('zz', now)).toBe(null);
  });

  it('refuses to start with nothing queued', async () => {
    const w = await opened();
    await w.AT.startBatch(); await flush(w);
    expect(posts(w).length).toBe(0);
    expect(w.__alerts[0]).toMatch(/queue at least one/i);
  });

  it('posts items, budget, objective, dims and restart, then locks the rail and shows the Batch pane', async () => {
    const w = await opened('window.__batch = ' + JSON.stringify(batchDoc('queued')) + ';');
    w.document.querySelectorAll(`[data-batch-agent="${A1}"]`).forEach(b => b.classList.add('on'));
    w.document.getElementById('atBatchBudget').value = '300';
    await w.AT.startBatch(); await flush(w); await flush(w);
    const [, opts] = posts(w)[0];
    const body = JSON.parse(opts.body);
    expect(body.items).toEqual([{ agent_id: A1, model_id: 'org/m:Q4' }, { agent_id: A1, model_id: 'org/n:Q8' }]);
    expect(body.budget_min).toBe(300);
    expect(body.objective).toBe('balanced');
    expect(body.restart).toBe(true);
    expect(body.start_at).toBe(null);
    expect(body.dims.context).toBeTruthy();
    expect(w.sessionStorage.getItem('at.batch')).toBe('b1');
    expect(w.document.getElementById('atPaneBatch').style.display).toBe('');
    expect(w.document.querySelector('#toolsModAt .at-rail').classList.contains('locked')).toBe(true);
    expect(w.document.getElementById('atRunBtn').disabled).toBe(true);
    expect(w.document.getElementById('atBatchStartBtn').disabled).toBe(true);
    expect(w.document.getElementById('atBatchCancelBtn').style.display).toBe('');
    expect(w.AT.batchActive()).toBe(true);
  });

  it('surfaces a refused start', async () => {
    const w = await opened('window.__startReply = { ok: false, error: "a batch is already queued or running" };');
    w.document.querySelector(`[data-batch-agent="${A1}"]`).classList.add('on');
    await w.AT.startBatch(); await flush(w);
    expect(w.__alerts.pop()).toMatch(/already queued/);
    expect(w.AT.batchActive()).toBe(false);
  });
});

describe('Batch pane', () => {
  it('resumes from sessionStorage, renders rows and the watch button for the running item', async () => {
    const w = await opened('window.sessionStorage.setItem("at.batch", "b1"); window.__batch = ' + JSON.stringify(batchDoc('running')) + ';');
    expect(w.__fetches.some(([u]) => u === '/api/llm/autotune/batch/b1')).toBe(true);
    const rows = w.document.querySelectorAll('#atBatchRows tr');
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('alpha');
    expect(rows[0].textContent).toContain('running');
    expect(w.document.getElementById('atBatchWatchBtn').style.display).toBe('');
    expect(w.document.getElementById('atBatchPill').textContent).toBe('running');
    expect(w.AT.batchActive()).toBe(true);
  });

  it('adopts an active batch another browser started', async () => {
    const w = await opened('window.__batch = ' + JSON.stringify(batchDoc('running')) + '; window.__batches = { ok: true, batches: [window.__batch] };');
    expect(w.AT.batchActive()).toBe(true);
    expect(w.sessionStorage.getItem('at.batch')).toBe('b1');
  });

  it('watching opens the current item agent stream', async () => {
    const w = await opened('window.sessionStorage.setItem("at.batch", "b1"); window.__batch = ' + JSON.stringify(batchDoc('running')) + ';');
    w.AT.batchWatch(); await flush(w);
    expect(w.__sse.url).toBe('/api/llm/autotune/stream?agent=' + A1);
    expect(w.document.getElementById('atPaneRun').style.display).toBe('');
    w.__sse.onEvent({ type: 'done', ok: true }); await flush(w);
    // Back to the Batch pane, still locked, because the batch itself is not done.
    expect(w.document.getElementById('atPaneBatch').style.display).toBe('');
    expect(w.document.querySelector('#toolsModAt .at-rail').classList.contains('locked')).toBe(true);
  });

  it('unlocks, clears sessionStorage and shows the summary when the batch finishes', async () => {
    const done = batchDoc('done', { finished: 2, summary: { title: 'Overnight autotune: 2 tuned, 1 applied, 0 skipped', body: 'alpha · org/m:Q4 · +12 % · ctx 32k · applied\nalpha · org/n:Q8 · −2 % · ctx 8k · not applied · slower than the live config' } });
    done.items[0] = { ...done.items[0], status: 'done', gain_pct: 12.4, ctx: 32768, applied: true, note: 'applied 1 change' };
    done.items[1] = { ...done.items[1], status: 'done', gain_pct: -2, ctx: 8192, applied: false, note: 'slower than the live config' };
    const w = await opened('window.sessionStorage.setItem("at.batch", "b1"); window.__batch = ' + JSON.stringify(done) + ';');
    expect(w.AT.batchActive()).toBe(false);
    expect(w.sessionStorage.getItem('at.batch')).toBe(null);
    expect(w.document.querySelector('#toolsModAt .at-rail').classList.contains('locked')).toBe(false);
    expect(w.document.getElementById('atBatchSummary').style.display).toBe('');
    expect(w.document.getElementById('atBatchSummary').textContent).toContain('2 tuned, 1 applied');
    const rows = w.document.querySelectorAll('#atBatchRows tr');
    expect(rows[0].textContent).toContain('+12 %');
    expect(rows[0].textContent).toContain('32k');
    expect(rows[0].textContent).toContain('applied');
    expect(rows[1].textContent).toContain('−2 %');
    expect(w.document.getElementById('atBatchPill').textContent).toBe('complete');
  });

  it('drops a batch the manager no longer knows', async () => {
    const w = await opened('window.sessionStorage.setItem("at.batch", "gone"); window.__batch = null;');
    expect(w.AT.batchActive()).toBe(false);
    expect(w.sessionStorage.getItem('at.batch')).toBe(null);
  });

  it('cancel posts to the batch cancel route', async () => {
    const w = await opened('window.sessionStorage.setItem("at.batch", "b1"); window.__batch = ' + JSON.stringify(batchDoc('running')) + ';');
    await w.AT.cancelBatch(); await flush(w);
    expect(w.__fetches.some(([u, o]) => u === '/api/llm/autotune/batch/b1/cancel' && o && o.method === 'POST')).toBe(true);
  });
});
