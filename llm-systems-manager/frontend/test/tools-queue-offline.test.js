// #888: the Offline benchmark obeys the same run gate as the other tools.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush, QUEUE_SLOT_STUB } from './helpers/harness.js';

const BODY = `
  <div id="benchModelPanel"><input type="checkbox" value="org/m" checked></div>
  <div class="bench-tab active" data-tab="llama-bench"></div>
  <button id="benchRunBtn">▶ Run Benchmark</button>
  <button id="benchCancelBtn" style="display:none;">✕ Cancel</button>
  <span id="benchStatus">idle</span>
  <div id="benchResults"><div id="benchResultRows"></div></div>
  <div id="benchLog"></div>
  <canvas id="benchChart"></canvas>
`;

const STUBS = `
  window.cssVar = () => '#888';
  window.adminEsc = (s) => String(s);
  window.shortName = (s) => String(s);
  window._themedConfirm = () => Promise.resolve(true);
  window.toolsSyncRunDot = function () {};
  HTMLCanvasElement.prototype.getContext = function () { return {}; };
  window.Chart = function (ctx, cfg) { this.data = cfg.data; this.options = cfg.options; };
  Chart.prototype.update = function () {};
  Chart.prototype.resize = function () {};
  Chart.prototype.destroy = function () {};
  window.SG = { open: (opts) => { window.__sse = opts; return { close() {} }; } };
  window.__fetches = [];
  window.fetch = function (url, opts) {
    window.__fetches.push([String(url), opts]);
    const u = String(url);
    const body = u.indexOf('/api/llm/models') === 0 ? { data: [] }
      : u.indexOf('/api/llama-state') === 0 ? { state: 'unknown' }
      : u.indexOf('/api/benchmark/run') === 0 ? (window.__runReply || { ok: true })
      : { ok: true };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  };
`;

function boot(bootstrap = '') {
  return runHarness({ sources: [STUBS, QUEUE_SLOT_STUB, srcFile('js/bench-autotune.js')],
                      bodyHtml: BODY, bootstrap });
}

const el = (win, id) => win.document.getElementById(id);
const starts = (win) => win.__fetches.filter(f => f[0].indexOf('/api/benchmark/run') === 0);
const BUSY = "window.__gateBusy = { tool: 'autotune', label: 'Autotune', host: 'gpu-01', agent_id: 'a1' };";

describe('Offline benchmark queueing (#888)', () => {
  it('queues instead of starting while Autotune holds the host', async () => {
    const win = boot(BUSY);
    await win.runBenchmark();
    await flush();
    expect(starts(win)).toHaveLength(0);
    expect(win.__slots[0].queued()).toBe(true);
    expect(el(win, 'benchRunBtn').textContent).toContain('Queued');
    expect(el(win, 'benchStatus').textContent).toContain('waiting for Autotune on gpu-01');
    // The launcher tile is marked against the Benchmark tool, not a new one.
    expect(win.__queued[0]).toBe('benchmark:offline');
  });

  it('starts the queued run by itself once the gate clears', async () => {
    const win = boot(BUSY);
    await win.runBenchmark();
    await flush();
    win.__gateBusy = null;
    await win.__slots[0].fire();
    for (let i = 0; i < 6; i++) await flush();
    expect(starts(win)).toHaveLength(1);
    expect(JSON.parse(starts(win)[0][1].body).model_ids).toEqual(['org/m']);
  });

  it('drops a queued run on Cancel without cancelling anything on the agent', async () => {
    const win = boot(BUSY);
    await win.runBenchmark();
    await flush();
    win.__fetches.length = 0;
    win.cancelBenchmark();
    expect(win.__slots[0].queued()).toBe(false);
    expect(win.__fetches.map(f => f[0])).not.toContain('/api/benchmark/cancel');
    expect(el(win, 'benchStatus').textContent).toBe('queued run dropped');
  });

  it('labels the button Queue while the host is busy and nothing is pending', async () => {
    const win = boot(BUSY);
    win.__slots.length = 0;
    await win.runBenchmark();
    await flush();
    win.__slots[0].drop();
    expect(el(win, 'benchRunBtn').textContent).toContain('Queue Benchmark');
    expect(el(win, 'benchRunBtn').disabled).toBe(false);
  });

  it('queues rather than losing the run when the agent refuses it', async () => {
    const win = boot();
    win.__runReply = { ok: false, error: 'a benchmark is already in progress' };
    await win.runBenchmark();
    for (let i = 0; i < 6; i++) await flush();
    expect(win.__slots[0].queued()).toBe(true);
    expect(win.__slots[0].waitFor()).toBe('the run in progress');
  });

  it('runs straight away when the host is free', async () => {
    const win = boot();
    await win.runBenchmark();
    for (let i = 0; i < 6; i++) await flush();
    expect(starts(win)).toHaveLength(1);
    expect(win.__slots[0].queued()).toBe(false);
  });
});
