// #888: host perf-mode note in the tools' run status strips, driven by the agent's perf_mode event.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';
import { perfModeNote } from '../js/lib/perfmode.js';

const INDEX = srcFile('index.html');
const AT_BODY = INDEX.slice(INDEX.indexOf('<div id="toolsModAt"'), INDEX.indexOf('<!-- /toolsModAt -->'));
const QG_BODY = INDEX.slice(INDEX.indexOf('<div id="toolsModQg"'), INDEX.indexOf('<!-- /toolsModQg -->'));

const AT_PRE = {
  ok: true, busy: false, unit_active: false, cores: { physical: 16, logical: 32 }, perplexity: true,
  runtime: { ok: true }, drafts: [], sizes: { 'org/m:Q4': 18e9 }, vram_total_mb: 32768, ram_total_mb: 32768,
};

const COMMON = `
  window.TC = { esc: (s) => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])) };
  window.SG = { open: (opts) => { window.__sse = opts; return { close() { window.__closed = true; } }; } };
  window.toolsSyncRunDot = function () {};
  window.alert = function () {};
  window.showToast = function () {};
  window._recordToolRun = function () {};
  window._syncActiveProfile = function () { return Promise.resolve(); };
  window.__fetches = [];
`;

const AT_STUBS = COMMON + `
  let layout = {}; window.saveLayout = function () {};
  window.__pre = ${JSON.stringify(AT_PRE)};
  window.fetch = function (url, opts) {
    window.__fetches.push([String(url), opts]);
    const u = String(url);
    const body = u.startsWith('/api/llm/autotune/preflight') ? window.__pre
      : u.startsWith('/api/llm/autotune/status') ? { ok: true, items: [] }
      : u.startsWith('/api/benchmark/models') ? { models: ['org/m:Q4'] }
      : u.startsWith('/api/tools/runs') ? { runs: [], latest: {} }
      : u.startsWith('/api/llama-state') ? { state: 'stopped' }
      : (u.startsWith('/api/llm/config') && !(opts && opts.method)) ? { __DEFAULTS__: {}, 'org/m:Q4': { 'ctx-size': '32768' } }
      : u.startsWith('/api/llm/model-meta') ? { repo: 'org/m', suggestions: [] }
      : u.startsWith('/api/llm/autotune/run') ? { ok: true, run_id: 'r1' }
      : { ok: true };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
  };
`;

const QG_STUBS = COMMON + `
  window.fetch = function (url, opts) {
    window.__fetches.push([String(url), opts]);
    const u = String(url);
    const body = u.startsWith('/api/benchmark/models') ? { models: ['org/m:Q4'] }
      : (u === '/api/llm/config' && (!opts || !opts.method)) ? { 'org/m:Q4': { 'cache-type-k': 'q8_0' } }
      : u.startsWith('/api/llm/autotune/preflight') ? { ok: true, busy: false, perplexity: true }
      : u.startsWith('/api/llama-state') ? { state: 'stopped' }
      : (u === '/api/llm/autotune/run' && opts && opts.method === 'POST') ? { ok: true, run_id: 'q1' }
      : { ok: true };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
  };
`;

const PERF = srcFile('js/lib/perfmode.js');

async function atRun() {
  const win = runHarness({ sources: [AT_STUBS, PERF, srcFile('js/autotune.js')], bodyHtml: AT_BODY });
  win.AT.onOpen('org/m:Q4');
  for (let i = 0; i < 6; i++) await flush();
  await win.AT.run();
  await flush();
  return win;
}

async function qgRun() {
  const win = runHarness({ sources: [QG_STUBS, PERF, srcFile('js/quality.js')], bodyHtml: QG_BODY });
  await win.QG.onOpen('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
  await flush();
  await win.QG.run();
  await flush();
  return win;
}

const BENCH_BODY = `
  <div id="benchModelPanel"><input type="checkbox" value="org/m" checked></div>
  <div class="bench-tab active" data-tab="llama-bench"></div>
  <button id="benchRunBtn"></button>
  <button id="benchCancelBtn"></button>
  <span id="benchPerf"></span>
  <span id="benchStatus">idle</span>
  <div id="benchResults"><div id="benchResultRows"></div></div>
  <div id="benchLog"></div>
  <canvas id="benchChart"></canvas>
`;

const BENCH_STUBS = `
  window.cssVar = () => '#888';
  window.adminEsc = (s) => String(s);
  window.shortName = (s) => String(s);
  window._themedConfirm = () => Promise.resolve(true);
  window.toolsSyncRunDot = function () {};
  window._recordToolRun = function () {};
  HTMLCanvasElement.prototype.getContext = function () { return {}; };
  window.Chart = function (ctx, cfg) { this.data = cfg.data; this.options = cfg.options; };
  Chart.prototype.update = function () {};
  Chart.prototype.resize = function () {};
  Chart.prototype.destroy = function () {};
  window.__streams = [];
  window.EventSource = function (url) { this.url = url; this.readyState = 0; window.__streams.push(this); };
  EventSource.CONNECTING = 0; EventSource.OPEN = 1; EventSource.CLOSED = 2;
  EventSource.prototype.close = function () { this.readyState = 2; };
  window.__perfPosts = [];
  window.fetch = function (url, opts) {
    if (String(url).indexOf('/api/benchmark/perf-mode') === 0) window.__perfPosts.push(opts && opts.body);
    const body = String(url).indexOf('/api/llm/models') === 0 ? { data: [] }
      : String(url).indexOf('/api/llama-state') === 0 ? { state: 'unknown' }
      : { ok: true };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  };
`;

async function benchRun() {
  const win = runHarness({
    sources: [BENCH_STUBS, srcFile('js/lib/sseguard.js'), PERF, srcFile('js/bench-autotune.js')],
    bodyHtml: BENCH_BODY,
    bootstrap: 'window.__started = runBenchmark();',
  });
  await win.__started;
  await flush();
  const es = win.__streams[win.__streams.length - 1];
  es.readyState = 1;
  return { win, send: (m) => es.onmessage({ data: JSON.stringify(m), lastEventId: 'run1:1' }) };
}

const ON = { type: 'perf_mode', phase: 'awake', mode: 'performance', enabled: true, ok: true, rc: 0, governor: 'performance' };

describe('perfModeNote', () => {
  it('names the governor actually in effect', () => {
    expect(perfModeNote(ON)).toBe('cpu performance');
  });

  it('says nothing when the agent has no perf controller enabled', () => {
    expect(perfModeNote({ ...ON, enabled: false, ok: false, skipped: true, governor: 'schedutil' })).toBe('');
  });

  it('says nothing when the governor could not be read, rather than echoing the request', () => {
    expect(perfModeNote({ ...ON, governor: null })).toBe('');
    expect(perfModeNote(null)).toBe('');
  });

  it('flags a switch the host refused instead of claiming the target mode', () => {
    expect(perfModeNote({ ...ON, ok: false, rc: 1, governor: 'powersave' })).toBe('cpu powersave · switch failed');
  });
});

describe('autotune run strip', () => {
  it('shows the mode and keeps logging the perf_mode line', async () => {
    const win = await atRun();
    win.__sse.onEvent({ model_id: 'org/m:Q4', ...ON }, {});
    expect(win.document.getElementById('atStripPerf').textContent).toBe('cpu performance');
    expect(win.document.getElementById('atLog').textContent).toMatch(/perf mode → performance/);
  });

  it('stays blank when the controller is disabled on that host', async () => {
    const win = await atRun();
    win.__sse.onEvent({ model_id: 'org/m:Q4', ...ON, enabled: false, ok: false, skipped: true,
                        error: 'perf controller disabled on this agent (PERF_CONTROLLER_ENABLED)' }, {});
    expect(win.document.getElementById('atStripPerf').textContent).toBe('');
    expect(win.document.getElementById('atLog').textContent).toMatch(/not applied/);
  });

  it('clears the note once the run finishes', async () => {
    const win = await atRun();
    win.__sse.onEvent({ model_id: 'org/m:Q4', ...ON }, {});
    win.__sse.onEvent({ type: 'done', ok: true, model_id: 'org/m:Q4' }, {});
    expect(win.document.getElementById('atStripPerf').textContent).toBe('');
  });
});

describe('quality guard run strip', () => {
  it('shows the mode reported by the agent', async () => {
    const win = await qgRun();
    win.__sse.onEvent({ ...ON }, {});
    expect(win.document.getElementById('qgStripPerf').textContent).toBe('cpu performance');
  });

  it('stays blank when the controller is disabled, and clears on done', async () => {
    const win = await qgRun();
    win.__sse.onEvent({ ...ON, enabled: false, ok: false, skipped: true }, {});
    expect(win.document.getElementById('qgStripPerf').textContent).toBe('');
    win.__sse.onEvent({ ...ON }, {});
    expect(win.document.getElementById('qgStripPerf').textContent).toBe('cpu performance');
    win.__sse.onEvent({ type: 'done', ok: true }, {});
    expect(win.document.getElementById('qgStripPerf').textContent).toBe('');
  });
});

describe('offline benchmark run header', () => {
  it('no longer drives the perf mode from the browser', async () => {
    const r = await benchRun();
    expect(r.win.__perfPosts).toEqual([]);
  });

  it('shows the mode from the agent stream and clears it on done', async () => {
    const r = await benchRun();
    r.send(ON);
    expect(r.win.document.getElementById('benchPerf').textContent).toBe('cpu performance');
    r.send({ type: 'done', ok: true });
    expect(r.win.document.getElementById('benchPerf').textContent).toBe('');
  });

  it('stays blank when the controller is disabled on that host', async () => {
    const r = await benchRun();
    r.send({ ...ON, enabled: false, ok: false, skipped: true, governor: 'schedutil' });
    expect(r.win.document.getElementById('benchPerf').textContent).toBe('');
  });
});
