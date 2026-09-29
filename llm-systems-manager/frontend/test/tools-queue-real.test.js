// #887/#897: modules driving the REAL shared gate and server-queue slot from js/tools.js.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const TOOLS_BODY = `
  <span id="toolsRunDot"></span>
  <div id="toolsHome" style="display:none"><div id="toolsLauncher"></div></div>
  <div id="toolsLedgerBody"></div>
`;

const AGENTS = { llama: [{ agent_id: 'a1', hostname: 'gpu-01', is_default: true }] };
const IDLE = { reportcard: false, benchmark: false, autotune: false, agents: {}, queue: {} };
const AT_ON_A1 = { reportcard: false, benchmark: false, autotune: true,
                   agents: { a1: ['autotune'] }, queue: {} };

// The activity/agents feed tools.js polls, swappable at runtime via __activity.
const GATE_FEED = `
  window.saveLayout = function () {};
  window._claim = function () { return true; };
  window._release = function () {};
  window._fetchT = (url, opts) => {
    if (opts && opts.method === 'POST') {
      window.__fetches.push([url, opts]);
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
    }
    return Promise.resolve({
    ok: true, json: () => Promise.resolve(
      url.indexOf('/api/tools/activity') === 0 ? window.__activity
      : url.indexOf('/api/agents/list-by-provider') === 0 ? ${JSON.stringify(AGENTS)}
      : {}),
    });
  };
`;

const TOOLS_SRC = ['js/lib/modelcards.js', 'js/lib/toolcards.js', 'js/tools.js'];

// ── Benchmark · Live ────────────────────────────────────────────────────

const BL_BODY = `
  <div class="mc-seg" id="benchModeSeg"><button data-mode="live" class="on">Live</button><button data-mode="offline">Offline</button></div>
  <span id="benchModeNote"></span>
  <div id="benchOffline"></div>
  <div id="benchLive">
  <div id="blPresets"><span class="bl-chip on" data-preset="chat">Chat</span></div>
  <div id="blBenchRow"><select id="blBench"><option>qualitative</option></select></div>
  <div id="blCatsRow"><span id="blCatsHint"></span><div id="blCats"></div></div>
  <div id="blOslRow"><input id="blOsl" value="1024"></div><input id="blLimit" value="8">
  <div class="bl-chips" id="blMatrixTgl"><span class="bl-chip" data-matrix="1">m</span></div>
  <div id="blMatrixRows"><div class="bl-chips" id="blMatrixBench"></div><input id="blMatrixOsl" value="256"></div>
  <div class="bl-chips" id="blSweepChips"><span class="bl-chip on" data-conc="1">1</span></div>
  <div id="blSweepRow"><input id="blSweep" value="1"></div>
  <input id="blTimeout" value="600"><textarea id="blExtra">{}</textarea>
  <select id="blBaseline"><option value="">none</option></select>
  <button id="blRunBtn"></button><button id="blCancelBtn" style="display:none"></button>
  <span id="blEstimate"></span><button id="blAttachBtn" style="display:none"></button>
  <button id="blPinBtn"></button>
  <div class="bl-notice" id="blNotice" style="display:none"></div>
  <span id="blStatus"></span><div id="blStrip"></div><span id="blElapsed"></span>
  <div id="blProgress"><i></i></div><div id="blTiles"></div>
  <canvas id="blChart"></canvas><div id="blChartEmpty"></div><div id="blChartCaption"></div>
  <div class="mc-seg" id="blCellSeg"></div><div id="blLevelSeg"></div>
  <div id="blTable"></div><div id="blLog"></div>
  <div id="blHeatCard"><span id="blHeatMeta"></span><div id="blHeat"></div><div id="blHeatCaption"></div></div>
  <button id="blSetupBtn"></button>
  <div class="bl-chips" id="blFleetTgl"><span class="bl-chip" data-fleet="1">fleet</span></div>
  <div id="blFleetHosts"></div><div id="blFleetNote"></div>
  <div id="blFleetCard"><span id="blFleetMeta"></span><span id="blFleetProgress"></span><div id="blFleetTable"></div></div>
  <div id="blBaseCard"><input type="checkbox" id="blBaseEnabled"><span id="blBaseMeta"></span>
    <a id="blBaseSettings"></a><button id="blBaseAllBtn"></button><div id="blBaseTable"></div></div>
  <div id="blPreflight"></div>
  </div>
`;

const BL_STUBS = `
  let layout = { toolsView: 'card' };
  window.TC = { esc: (s) => String(s) };
  window.SG = { open: (opts) => { window.__sse = opts; return { close() {} }; } };
  HTMLCanvasElement.prototype.getContext = function () { return {}; };
  window.Chart = function (ctx, cfg) { this.data = cfg.data; this.options = cfg.options; };
  Chart.prototype.update = function () {}; Chart.prototype.resize = function () {};
  Chart.prototype.destroy = function () {};
  window.__fetches = [];
  window._me = { username: 'alice' };
  window.__fleetJob = { job_id: 'j1', hosts: [{ agent_id: 'a1', hostname: 'gpu-01', status: 'running' }], done: false };
  window.fetch = function (url, opts) {
    const u = String(url);
    window.__fetches.push([u, opts]);
    const body = u.indexOf('/api/benchmark/live/preflight') === 0
      ? { ok: true, server: { up: true, url: 'http://h:9931', models: [{ id: 'org/m', status: 'loaded' }],
          loaded_id: 'org/m', slots_idle: 2, slots_total: 2 },
          runtime: { python: '/p', source: 'venv', script: '/s', script_status: 'ok' },
          datasets: { qualitative: { categories: ['coding'] } }, benches: ['qualitative'], busy: false }
      : (u.indexOf('/api/benchmark/live/fleet/') === 0 && u.indexOf('/cancel') < 0)
      ? { ok: true, job: window.__fleetJob }
      : u.indexOf('/api/benchmark/live/runs') === 0 ? { ok: true, runs: [] }
      : u.indexOf('/api/benchmark/live/run') === 0 ? (window.__runAnswer || { ok: true, run_id: 'r1' })
      : { ok: true, run_id: 'r1' };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  };
`;

function bootLive(activity, bootstrap = '') {
  const win = runHarness({
    sources: [BL_STUBS, GATE_FEED, ...TOOLS_SRC.map(srcFile), srcFile('js/bench-live.js')],
    bodyHtml: TOOLS_BODY + BL_BODY,
    bootstrap: `
      window.__activity = ${JSON.stringify(activity)};
      initToolsTab();
      window.__done = toolsPollActivity();
      ${bootstrap}
    `,
  });
  return win.__done.then(() => flush()).then(() => flush()).then(() => win);
}

const posted = (win, path) => win.__fetches.filter(f => f[0] === path);

const QUEUED = { ok: true, queued: true, job_id: 'j1', position: 1, wait_for: 'Autotune on gpu-01' };
const heldRow = (status = 'queued', tool = 'benchmark') =>
  ({ ...AT_ON_A1, queue: { a1: [{ job_id: 'j1', tool, provider: 'llama', model_id: 'org/m', user: 'alice', status, created: 1 }] } });

describe('Benchmark · Live on the server queue (#897)', () => {
  it('posts the start even while the host is busy and holds the queued job', async () => {
    const win = await bootLive(AT_ON_A1);
    win.__runAnswer = QUEUED;
    await win.BL.onOpen('org/m');
    await flush(); await flush();
    await win.BL.run();
    for (let i = 0; i < 4; i++) await flush();
    expect(posted(win, '/api/benchmark/live/run')).toHaveLength(1);
    expect(win.BL.running()).toBe(false);
    expect(win.document.getElementById('blStatus').textContent).toContain('queued');
    expect(win.document.getElementById('blNotice').textContent).toContain('Queued behind Autotune on gpu-01');
  });

  it('attaches by itself when the held job starts running', async () => {
    const win = await bootLive(AT_ON_A1);
    win.__runAnswer = QUEUED;
    await win.BL.onOpen('org/m');
    await flush(); await flush();
    await win.BL.run();
    for (let i = 0; i < 4; i++) await flush();
    win.__activity = heldRow('queued');
    await win.toolsPollActivity(); await flush(); await flush();
    win.__activity = { ...heldRow('running'), autotune: false, benchmark: true, agents: { a1: ['benchmark'] } };
    await win.toolsPollActivity(); await flush(); await flush();
    expect(win.BL.running()).toBe(true);
    expect(win.document.getElementById('blStatus').textContent).toContain('running');
    expect(posted(win, '/api/benchmark/live/run')).toHaveLength(1);
  });

  it('runs the adopted job as this tab own run, cancellable from here', async () => {
    const win = await bootLive(AT_ON_A1);
    win.__runAnswer = QUEUED;
    await win.BL.onOpen('org/m');
    await flush(); await flush();
    await win.BL.run();
    for (let i = 0; i < 4; i++) await flush();
    win.__activity = heldRow('queued');
    await win.toolsPollActivity(); await flush(); await flush();
    win.__activity = { ...heldRow('running'), autotune: false, benchmark: true, agents: { a1: ['benchmark'] } };
    await win.toolsPollActivity(); await flush(); await flush();
    const d = win.document;
    expect(d.getElementById('blCancelBtn').style.display).toBe('');
    expect(d.getElementById('blStatus').textContent).toContain('running');
    expect(d.getElementById('blStatus').textContent).not.toContain('elsewhere');
    win.__fetches.length = 0;
    win.BL.cancel();
    expect(posted(win, '/api/benchmark/cancel')).toHaveLength(1);
  });

  it('drops the queued job through the jobs API', async () => {
    const win = await bootLive(AT_ON_A1);
    win.__runAnswer = QUEUED;
    await win.BL.onOpen('org/m');
    await flush(); await flush();
    await win.BL.run();
    for (let i = 0; i < 4; i++) await flush();
    win.__activity = heldRow('queued');
    await win.toolsPollActivity(); await flush(); await flush();
    win.BL.cancel();
    await flush();
    expect(win.__fetches.some(f => f[0] === '/api/jobs/j1/cancel')).toBe(true);
    expect(win.document.getElementById('blStatus').textContent).toBe('queued run dropped');
  });

  it('still cancels a running fleet job before anything else', async () => {
    const win = await bootLive(AT_ON_A1);
    win.sessionStorage.setItem('bl.fleetJob', 'j1');
    await win.BL.onOpen('org/m');
    await flush(); await flush();
    win.__fetches.length = 0;
    win.BL.cancel();
    expect(posted(win, '/api/benchmark/live/fleet/j1/cancel')).toHaveLength(1);
  });

  it('shows a cap refusal as the status error', async () => {
    const win = await bootLive(AT_ON_A1);
    win.__runAnswer = { ok: false, error: '5 runs already queued on gpu-01' };
    await win.BL.onOpen('org/m');
    await flush(); await flush();
    await win.BL.run();
    for (let i = 0; i < 4; i++) await flush();
    expect(win.document.getElementById('blStatus').textContent).toBe('5 runs already queued on gpu-01');
  });
});

// ── Benchmark · Offline ─────────────────────────────────────────────────

const BA_BODY = `
  <div id="benchModelPanel"><input type="checkbox" value="org/m" checked></div>
  <div class="bench-tab active" data-tab="llama-bench"></div>
  <button id="benchRunBtn">▶ Run Benchmark</button>
  <button id="benchCancelBtn" style="display:none;">✕ Cancel</button>
  <span id="benchStatus">idle</span>
  <div id="benchResults"><div id="benchResultRows"></div></div>
  <div id="benchLog"></div><canvas id="benchChart"></canvas>
`;

const BA_STUBS = `
  window.layout = { toolsView: 'card' };
  window.cssVar = () => '#888';
  window.adminEsc = (s) => String(s);
  window.shortName = (s) => String(s);
  window._me = { username: 'alice' };
  window.__confirmed = null;
  window._themedConfirm = () => new Promise((r) => { window.__confirmed = r; });
  HTMLCanvasElement.prototype.getContext = function () { return {}; };
  window.Chart = function (ctx, cfg) { this.data = cfg.data; this.options = cfg.options; };
  Chart.prototype.update = function () {}; Chart.prototype.resize = function () {};
  Chart.prototype.destroy = function () {};
  window.SG = { open: (opts) => { window.__sse = opts; return { close() {} }; } };
  window.__fetches = [];
  window.fetch = function (url, opts) {
    const u = String(url);
    window.__fetches.push([u, opts]);
    const body = u.indexOf('/api/llm/models') === 0
      ? { data: [{ id: 'org/m', status: { value: 'loaded' } }] }
      : u.indexOf('/api/llama-state') === 0 ? { state: 'unknown' }
      : u.indexOf('/api/benchmark/run') === 0 ? (window.__runAnswer || { ok: true })
      : { ok: true };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  };
`;

function bootOffline(activity) {
  const win = runHarness({
    sources: [BA_STUBS, GATE_FEED, ...TOOLS_SRC.map(srcFile), srcFile('js/bench-autotune.js')],
    bodyHtml: TOOLS_BODY + BA_BODY,
    bootstrap: `
      window.__activity = ${JSON.stringify(activity)};
      initToolsTab();
      window.__done = toolsPollActivity();
    `,
  });
  return win.__done.then(() => flush()).then(() => flush()).then(() => win);
}

describe('Offline benchmark on the server queue (#897)', () => {
  it('queues instead of stopping llama-server when the host was taken meanwhile', async () => {
    const win = await bootOffline(IDLE);
    win.__runAnswer = QUEUED;
    win.runBenchmark();
    for (let i = 0; i < 4; i++) await flush();
    expect(typeof win.__confirmed).toBe('function');
    win.__activity = AT_ON_A1;
    await win.toolsPollActivity();
    await flush();
    win.__confirmed(true);
    for (let i = 0; i < 6; i++) await flush();
    expect(posted(win, '/api/llm/unload')).toHaveLength(0);
    expect(posted(win, '/api/llm/server/stop')).toHaveLength(0);
    expect(posted(win, '/api/benchmark/run')).toHaveLength(1);
    expect(win.document.getElementById('benchStatus').textContent).toContain('queued');
  });

  it('goes ahead when the host is still free after the confirm', async () => {
    const win = await bootOffline(IDLE);
    win.runBenchmark();
    for (let i = 0; i < 4; i++) await flush();
    win.__confirmed(true);
    for (let i = 0; i < 8; i++) await flush();
    expect(posted(win, '/api/llm/unload')).toHaveLength(1);
    expect(posted(win, '/api/benchmark/run')).toHaveLength(1);
  });

  it('adopts the user queued row when the module opens, without a Run click', async () => {
    const win = await bootOffline({ ...AT_ON_A1, queue: { a1: [{ job_id: 'j1', tool: 'benchmark', provider: 'llama', model_id: 'org/m',
      user: 'alice', status: 'queued', created: 1, path: '/llama/bench/run' }] } });
    await win.openBench('org/m');
    for (let i = 0; i < 4; i++) await flush();
    expect(win.document.getElementById('benchStatus').textContent).toContain('queued');
    expect(posted(win, '/api/benchmark/run')).toHaveLength(0);
  });

  it('opens the stream when the held job starts running', async () => {
    const win = await bootOffline(AT_ON_A1);
    win.__runAnswer = QUEUED;
    win.runBenchmark();
    for (let i = 0; i < 4; i++) await flush();
    win.__confirmed(true);
    for (let i = 0; i < 6; i++) await flush();
    expect(win.document.getElementById('benchStatus').textContent).toContain('queued');
    win.__activity = heldRow('queued');
    await win.toolsPollActivity(); await flush(); await flush();
    win.__activity = { ...heldRow('running'), autotune: false, benchmark: true, agents: { a1: ['benchmark'] } };
    await win.toolsPollActivity(); await flush(); await flush();
    expect(win.__sse && win.__sse.url).toBe('/api/benchmark/stream');
    expect(win.document.getElementById('benchStatus').textContent).toContain('running');
  });
});
