// #774: a transient SSE drop must not leave the pill stuck on "reconnecting…".
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const BODY = `
  <div id="benchModelPanel"><input type="checkbox" value="org/m" checked></div>
  <div class="bench-tab active" data-tab="llama-bench"></div>
  <button id="benchRunBtn"></button>
  <button id="benchCancelBtn"></button>
  <span id="benchStatus">idle</span>
  <div id="benchResults"><div id="benchResultRows"></div></div>
  <div id="benchLog"></div>
  <canvas id="benchChart"></canvas>
`;

// Minimum surface bench-autotune.js touches on the way into a run.
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

  window.__streams = [];
  window.EventSource = function (url) {
    this.url = url;
    this.readyState = 0;
    window.__streams.push(this);
  };
  EventSource.CONNECTING = 0;
  EventSource.OPEN = 1;
  EventSource.CLOSED = 2;
  EventSource.prototype.close = function () { this.readyState = 2; };

  window.fetch = function (url) {
    const body =
      url.indexOf('/api/llm/models') === 0 ? { data: [] }
      : url.indexOf('/api/llama-state') === 0 ? { state: 'unknown' }
      : { ok: true };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  };
`;

const status = (win) => win.document.getElementById('benchStatus').textContent;

// Starts a run and returns {win, es, send, drop}.
async function startRun() {
  const win = runHarness({
    sources: [STUBS, srcFile('js/bench-autotune.js')],
    bodyHtml: BODY,
    bootstrap: 'window.__started = runBenchmark();',
  });
  await win.__started;
  await flush();
  const es = win.__streams[win.__streams.length - 1];
  expect(es, 'runBenchmark should have opened a stream').toBeTruthy();
  es.readyState = 1;
  return {
    win, es,
    send: (msg) => es.onmessage({ data: JSON.stringify(msg), lastEventId: 'run1:7' }),
    drop: () => { es.readyState = 0; es.onerror(); },
  };
}

describe('benchmark stream reconnect', () => {
  it('shows the running model while the stream is healthy', async () => {
    const r = await startRun();
    r.send({ type: 'model_start', model_id: 'org/m' });
    expect(status(r.win)).toBe('running: m');
  });

  it('says reconnecting on a transient drop', async () => {
    const r = await startRun();
    r.send({ type: 'model_start', model_id: 'org/m' });
    r.drop();
    expect(status(r.win)).toBe('reconnecting…');
  });

  it('restores the live label on the first message after the drop', async () => {
    const r = await startRun();
    r.send({ type: 'model_start', model_id: 'org/m' });
    r.drop();
    r.es.readyState = 1;
    r.send({ type: 'line', text: 'still benchmarking' });
    expect(status(r.win)).toBe('running: m');
  });

  it('stays on reconnecting when only keepalives come back', async () => {
    const r = await startRun();
    r.send({ type: 'model_start', model_id: 'org/m' });
    r.drop();
    r.es.readyState = 1;
    r.send({ type: 'keepalive' });
    // A keepalive-only stream is not proof the run resumed.
    expect(status(r.win)).toBe('reconnecting…');
  });

  it('restores the label after repeated drops in one run', async () => {
    const r = await startRun();
    r.send({ type: 'model_start', model_id: 'org/m' });
    for (let i = 0; i < 3; i++) {
      r.drop();
      expect(status(r.win)).toBe('reconnecting…');
      r.es.readyState = 1;
      r.send({ type: 'result', model_id: 'org/m', gen_tps: 40, n_gen: 128, avg_ts: 40 });
      expect(status(r.win)).toBe('running: m');
    }
  });

  it('gives up after too many drops with no message, freeing the buttons', async () => {
    const r = await startRun();
    r.send({ type: 'model_start', model_id: 'org/m' });
    for (let i = 0; i < 25; i++) r.drop();
    expect(status(r.win)).toBe('disconnected');
    expect(r.win.document.getElementById('benchRunBtn').disabled).toBe(false);
    expect(r.win.document.getElementById('benchCancelBtn').style.display).toBe('none');
  });

  it('a terminal close still reports disconnected', async () => {
    const r = await startRun();
    r.es.readyState = 2;
    r.es.onerror();
    expect(status(r.win)).toBe('disconnected');
  });

  it('done wins over a pending reconnect notice', async () => {
    const r = await startRun();
    r.send({ type: 'model_start', model_id: 'org/m' });
    r.drop();
    r.es.readyState = 1;
    r.send({ type: 'done', ok: true });
    expect(status(r.win)).toBe('done');
  });
});

describe('ledger rows carry a run id (#772)', () => {
  it('prefers the agent-sent maxes and run_id on model_done', async () => {
    const r = await startRun();
    const posts = [];
    r.win.fetch = (url, opts) => {
      if (url === '/api/tools/runs') posts.push(JSON.parse(opts.body));
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
    };
    r.send({ type: 'model_start', model_id: 'org/m' });
    r.send({ type: 'model_done', model_id: 'org/m', run_id: 'agentrun',
             max_gen_tps: 41.5, max_ppt_tps: 900, max_pg_tps: null });
    expect(posts[0].run_id).toBe('agentrun');
    expect(posts[0].gen_tps).toBe(41.5);
    expect(posts[0].ppt_tps).toBe(900);
  });

  it('falls back to the SSE event id when an older agent sends no run_id', async () => {
    const r = await startRun();
    const posts = [];
    r.win.fetch = (url, opts) => {
      if (url === '/api/tools/runs') posts.push(JSON.parse(opts.body));
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
    };
    r.send({ type: 'model_start', model_id: 'org/m' });
    r.send({ type: 'result', model_id: 'org/m', gen_tps: 40, n_gen: 128, avg_ts: 40 });
    r.send({ type: 'model_done', model_id: 'org/m' });
    expect(posts[0].run_id).toBe('run1');
    expect(posts[0].gen_tps).toBe(40);
  });
});
