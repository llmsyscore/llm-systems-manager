// #879: Live benchmark module — presets, sweep parsing, estimate, deltas, knee, mode switch.
import { describe, it, expect, vi } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const BODY = `
  <div class="mc-seg" id="benchModeSeg"><button data-mode="live" class="on">Live</button><button data-mode="offline">Offline</button></div>
  <span id="benchModeNote"></span>
  <div id="benchOffline"></div>
  <div id="benchLive" style="display:none">
    <div id="blPreflight"></div>
    <div id="blPresets"><span class="bl-chip on" data-preset="chat">Chat</span><span class="bl-chip" data-preset="coding">Coding</span><span class="bl-chip" data-preset="rag">RAG</span><span class="bl-chip" data-preset="agentic">Agentic</span><span class="bl-chip" data-preset="longctx">Long context</span><span class="bl-chip" data-preset="custom">Custom</span></div>
    <div id="blBenchRow"><select id="blBench"><option>qualitative</option><option>throughput_1k</option><option>throughput_2k</option><option>throughput_8k</option><option>throughput_16k</option><option>throughput_32k</option></select></div>
    <div id="blCatsRow"><span class="bl-hint" id="blCatsHint">all</span><div id="blCats"></div></div>
    <div id="blOslRow"><input id="blOsl" value="1024"></div><input id="blLimit" value="8">
    <div class="bl-chips" id="blMatrixTgl"><span class="bl-chip" data-matrix="1">Prompt × output matrix</span></div>
    <div id="blMatrixRows" style="display:none;">
      <div class="bl-chips" id="blMatrixBench"><span class="bl-chip on" data-bench="throughput_1k">1k</span><span class="bl-chip" data-bench="throughput_2k">2k</span><span class="bl-chip on" data-bench="throughput_8k">8k</span><span class="bl-chip" data-bench="throughput_16k">16k</span><span class="bl-chip on" data-bench="throughput_32k">32k</span></div>
      <input id="blMatrixOsl" value="256, 1024">
    </div>
    <div class="bl-chips" id="blSweepChips"><span class="bl-chip on" data-conc="1">1</span><span class="bl-chip on" data-conc="2">2</span><span class="bl-chip on" data-conc="4">4</span><span class="bl-chip on" data-conc="8">8</span><span class="bl-chip" data-conc="16">16</span><span class="bl-chip" data-conc="32">32</span><span class="bl-chip" data-conc="custom">Custom</span></div>
    <div id="blSweepRow"><input id="blSweep" value="1, 2, 4, 8"></div>
    <input id="blTimeout" value="600"><textarea id="blExtra">{"temperature": 0}</textarea>
    <select id="blBaseline"><option value="">none</option></select>
    <button id="blRunBtn"></button><button id="blCancelBtn" style="display:none"></button><span id="blEstimate"></span>
    <button id="blAttachBtn" style="display:none" onclick="BL.addToReportCard()">Add to Report Card</button>
    <div class="bl-notice" id="blNotice" style="display:none"></div>
    <span id="blStatus"></span><div id="blStrip"></div><span id="blElapsed"></span><div id="blProgress"><i></i></div><div id="blTiles"></div>
    <canvas id="blChart"></canvas><div class="bl-chart-empty" id="blChartEmpty"></div><div id="blChartCaption"></div>
    <div class="mc-seg" id="blCellSeg" style="display:none"></div><div id="blLevelSeg"></div><div id="blTable"></div><div id="blLog"></div>
    <div class="bl-card" id="blHeatCard" style="display:none"><span id="blHeatMeta"></span><div id="blHeat"></div><div id="blHeatCaption"></div></div>
    <button id="blSetupBtn" style="display:none"></button>
    <div class="bl-grp"><div class="bl-grp-h">Fleet <span class="bl-hint" id="blFleetHint"></span></div>
      <div class="bl-chips" id="blFleetTgl"><span class="bl-chip" data-fleet="1">Run on every host with this model loaded</span></div>
      <div id="blFleetHosts" class="bl-chips" style="display:none"></div>
      <div class="bl-hint" id="blFleetNote" style="display:none"></div>
    </div>
    <div class="bl-card" id="blFleetCard" style="display:none"><span id="blFleetMeta"></span><span id="blFleetProgress"></span><div id="blFleetTable"></div></div>
  </div>
`;

// foundation.js declares `let layout` at top level, so window.layout is undefined;
// its own source string reproduces that classic-script scope.
const LAYOUT = `
  let layout = {}; window.__layout = () => layout; window.saveLayout = function () {};
`;

const STUBS = `
  window.TC = { esc: (s) => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])) };
  window.SG = { open: (opts) => { window.__sse = opts; return { close() {} }; } };
  window.toolsSyncRunDot = function () {};
  HTMLCanvasElement.prototype.getContext = function () { return {}; };
  window.Chart = function (ctx, cfg) { this.data = cfg.data; this.options = cfg.options; window.__chart = this; };
  Chart.prototype.update = function () {}; Chart.prototype.resize = function () {}; Chart.prototype.destroy = function () {};
  window.__fetches = [];
  window.fetch = function (url, opts) {
    window.__fetches.push([url, opts]);
    const body = url.indexOf('/api/benchmark/live/preflight') === 0
      ? { ok: true, server: { up: true, url: 'http://h:9931', models: [{ id: 'org/m:Q4', status: 'loaded' }], loaded_id: 'org/m:Q4', slots_idle: 2, slots_total: 2 },
          runtime: window.__noRt ? { python: '', source: '', script: '', script_status: 'ok' } : { python: '/p', source: 'venv', script: '/s', script_status: 'ok' },
          datasets: { qualitative: { categories: ['coding', 'math', 'qa'] } }, benches: ['qualitative','throughput_1k','throughput_2k','throughput_8k','throughput_16k','throughput_32k'], busy: !!window.__busy }
      : url.indexOf('/api/benchmark/live/runs') === 0
      ? { ok: true, runs: [{ run_id: 'b1', ts: '2026-09-05T22:14:00Z', baseline: true, gen_tps: 103.2, config: { bench: 'qualitative' } }] }
      : { ok: true, run_id: window.__runId || 'r1' };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  };
`;

function boot(bootstrap = '') {
  return runHarness({ sources: [LAYOUT, STUBS, srcFile('js/bench-live.js')], bodyHtml: BODY, bootstrap });
}

describe('BL pure helpers', () => {
  const win = boot();
  it('parses a concurrency sweep', () => {
    expect(win.BL.parseSweep('1, 2, 4, 8')).toEqual([1, 2, 4, 8]);
    expect(win.BL.parseSweep('4,x,2,2,0,70')).toEqual([2, 4]);
    expect(win.BL.parseSweep('')).toEqual([1]);
    expect(win.BL.parseSweep('1,2,3,4,5,6,7,8,9,10').length).toBe(8);
  });
  it('estimates seconds from the workload and last decode t/s', () => {
    const s = win.BL.estimateSeconds({ levels: [1, 2], samples: 8, categories: 3, osl: 1024 }, 100);
    expect(s).toBe(Math.round(2 * 8 * 3 * 1024 / 100));
    expect(win.BL.estimateSeconds({ levels: [1], samples: 8, categories: 3, osl: 1024 }, null)).toBeNull();
  });
  it('formats deltas (higher-is-better and lower-is-better)', () => {
    expect(win.BL.deltaText(138, 100)).toEqual({ text: '+38 % vs baseline', cls: 'up' });
    expect(win.BL.deltaText(90, 100)).toEqual({ text: '−10 % vs baseline', cls: 'down' });
    expect(win.BL.deltaText(3.9, 5.3, true)).toEqual({ text: '−26 % vs baseline', cls: 'up' });
    expect(win.BL.deltaText(100, 101)).toEqual({ text: '−1 % vs baseline', cls: 'flat' });
    expect(win.BL.deltaText(100, null)).toEqual({ text: 'no baseline', cls: 'flat' });
  });
  it('finds the knee (largest level with per-request decode ≥ 70 % of level 1)', () => {
    const lv = (c, p) => ({ concurrency: c, all: { pred_tps: p } });
    expect(win.BL.knee([lv(1, 142), lv(2, 126), lv(4, 101), lv(8, 64)])).toBe(4);
    expect(win.BL.knee([lv(1, 100)])).toBe(1);
    expect(win.BL.knee([])).toBeNull();
  });
});

describe('BL presets and mode', () => {
  it('applies presets and flips to custom on edit', async () => {
    const win = boot('BL.onOpen("org/m:Q4");');
    await flush();
    const d = win.document;
    win.BL.applyPreset('rag');
    expect(d.getElementById('blBench').value).toBe('throughput_8k');
    expect(d.getElementById('blOsl').value).toBe('256');
    expect(d.getElementById('blLimit').value).toBe('6');
    expect(d.getElementById('blCatsRow').style.display).toBe('none');
    win.BL.applyPreset('coding');
    expect(d.getElementById('blBench').value).toBe('qualitative');
    expect([...d.querySelectorAll('#blCats .bl-chip.on')].map(c => c.dataset.cat)).toEqual(['coding']);
    expect(d.getElementById('blCatsRow').style.display).toBe('');
    d.getElementById('blOsl').value = '2048';
    d.getElementById('blOsl').dispatchEvent(new win.Event('input', { bubbles: true }));
    expect(d.querySelector('#blPresets .bl-chip.on').dataset.preset).toBe('custom');
  });
  it('persists the mode and toggles the bodies', async () => {
    const win = boot('BL.onOpen();');
    await flush();
    win.BL.setMode('offline');
    expect(win.layout).toBeUndefined();                // real scope: `let layout`, not a window prop
    expect(win.__layout().benchMode).toBe('offline');
    expect(win.document.getElementById('benchLive').style.display).toBe('none');
    expect(win.document.getElementById('benchOffline').style.display).toBe('');
    win.BL.setMode('live');
    expect(win.document.getElementById('benchLive').style.display).toBe('');
  });
  it('setup_done ends the stream and re-enables the run button', async () => {
    const win = boot('BL.onOpen("org/m:Q4");');
    await flush();
    const d = win.document;
    win.BL.setup();
    await flush();
    expect(d.getElementById('blRunBtn').disabled).toBe(true);
    win.__sse.onEvent({ type: 'setup_done', ok: true, runtime: { python: '/p', script: '/s', script_status: 'ok' } });
    expect(d.getElementById('blRunBtn').disabled).toBe(false);
    expect(d.getElementById('blCancelBtn').style.display).toBe('none');
    expect(d.getElementById('blStatus').textContent).toBe('runtime ready');
  });
  it('ignores stream events carrying another run id', async () => {
    const win = boot('BL.onOpen("org/m:Q4");');
    await flush();
    const d = win.document;
    win.BL.run();
    await flush();
    const lvl = runId => ({ type: 'level_result', run_id: runId, concurrency: 1, rows: [], all: { pred_tps: 1, agg_pred_tps: 1 } });
    win.__sse.onEvent(lvl('other'));
    expect(d.getElementById('blLevelSeg').innerHTML).toBe('');
    win.__sse.onEvent(lvl('r1'));
    expect(d.querySelectorAll('#blLevelSeg button').length).toBe(1);
  });
  it('run posts the validated body and lists the baseline', async () => {
    const win = boot('BL.onOpen("org/m:Q4");');
    await flush();
    const d = win.document;
    expect([...d.getElementById('blBaseline').options].map(o => o.value)).toContain('b1');
    win.BL.run();
    await flush();
    const call = win.__fetches.find(([u]) => u === '/api/benchmark/live/run');
    const body = JSON.parse(call[1].body);
    expect(body).toEqual({ model_id: 'org/m:Q4', bench: 'qualitative', categories: 'all', osl: 1024, limit: 8,
                           concurrency: [1, 2, 4, 8], timeout_s: 600, extra_inputs: { temperature: 0 }, baseline_run_id: 'b1' });
  });
  it('sweep chips select levels and Custom uses the input', async () => {
    const win = boot('BL.onOpen("org/m:Q4");');
    await flush();
    const d = win.document;
    const chip = c => d.querySelector(`#blSweepChips .bl-chip[data-conc="${c}"]`);
    const lastRun = () => JSON.parse(win.__fetches.filter(([u]) => u === '/api/benchmark/live/run').pop()[1].body);
    chip('16').dispatchEvent(new win.Event('click', { bubbles: true }));
    win.BL.run();
    await flush();
    expect(lastRun().concurrency).toEqual([1, 2, 4, 8, 16]);
    win.BL.cancel();
    chip('custom').dispatchEvent(new win.Event('click', { bubbles: true }));
    d.getElementById('blSweep').value = '2, 6';
    win.BL.run();
    await flush();
    expect(lastRun().concurrency).toEqual([2, 6]);
    win.BL.cancel();
  });
  it('runtime missing swaps the run button for the setup button', async () => {
    const win = boot('window.__noRt = true; BL.onOpen("org/m:Q4");');
    await flush();
    const d = win.document;
    expect(d.querySelector('#blPreflight .d').textContent).toBe('Bench runtime missing. Install it with the button below.');
    expect(d.getElementById('blRunBtn').style.display).toBe('none');
    const setup = d.getElementById('blSetupBtn');
    expect(setup.style.display).toBe('');
    expect(setup.className).toBe('mcbtn mcbtn-pri');
    expect(setup.textContent).toBe('Install bench runtime');
  });
  it('attaches when preflight says busy', async () => {
    const win = boot('window.__busy = true; BL.onOpen("org/m:Q4");');
    await flush();
    const d = win.document;
    expect(win.BL.running()).toBe(true);
    expect(d.getElementById('blNotice').style.display).toBe('');
    expect(d.getElementById('blRunBtn').textContent).toBe('Queue run');
    win.BL.cancel();
  });
  it('queued run starts after the attached run finishes', async () => {
    const win = boot('window.__busy = true; BL.onOpen("org/m:Q4");');
    await flush();
    const d = win.document;
    win.BL.run();
    await flush();
    expect(win.__fetches.some(([u]) => u === '/api/benchmark/live/run')).toBe(false);
    expect(d.getElementById('blStatus').textContent).toContain('queued');
    win.__sse.onEvent({ type: 'done', ok: true });
    await flush();
    expect(win.__fetches.some(([u]) => u === '/api/benchmark/live/run')).toBe(true);
    win.BL.cancel();
  });
  it('setup is refused while attached and the attached state clears on done', async () => {
    const win = boot('window.__busy = true; BL.onOpen("org/m:Q4");');
    await flush();
    const d = win.document;
    win.BL.setup();
    await flush();
    expect(win.__fetches.some(([u]) => u === '/api/benchmark/live/setup')).toBe(false);
    expect(d.getElementById('blStatus').textContent).toContain('in progress');
    win.__sse.onEvent({ type: 'done', ok: true });
    await flush();
    expect(win.BL.running()).toBe(false);
    expect(d.getElementById('blNotice').style.display).toBe('none');
    expect(d.getElementById('blRunBtn').textContent).not.toBe('Queue run');
  });
  it('cancel while attached drops only the queued run', async () => {
    const win = boot('window.__busy = true; BL.onOpen("org/m:Q4");');
    await flush();
    const d = win.document;
    win.BL.run();
    await flush();
    expect(d.getElementById('blCancelBtn').textContent).toBe('Drop queued run');
    win.BL.cancel();
    expect(win.__fetches.some(([u]) => u === '/api/benchmark/cancel')).toBe(false);
    expect(d.getElementById('blStatus').textContent).toContain('dropped');
    expect(win.BL.running()).toBe(true);
    expect(d.getElementById('blRunBtn').textContent).toBe('Queue run');
    win.__sse.onEvent({ type: 'done', ok: true });
    await flush();
    expect(win.__fetches.some(([u]) => u === '/api/benchmark/live/run')).toBe(false);
  });
});

describe('BL matrix + heatmap (#883)', () => {
  it('Long context preset turns the matrix on with 1k/8k/32k × 256,1024 at concurrency 1', async () => {
    const win = boot();
    await flush();
    win.BL.applyPreset('longctx');
    const c = win.BL._config();
    expect(c.matrix).toEqual({ benches: ['throughput_1k', 'throughput_8k', 'throughput_32k'], osls: [256, 1024] });
    expect(c.bench).toBe('throughput_1k'); expect(c.osl).toBe(256);
    expect(c.concurrency).toEqual([1]); expect(c.limit).toBe(4);
    expect(win.document.getElementById('blBenchRow').style.display).toBe('none');
  });
  it('other presets turn the matrix off', async () => {
    const win = boot();
    await flush();
    const before = win.BL._config().concurrency;
    win.BL.applyPreset('longctx');
    expect(win.BL._config().concurrency).toEqual([1]);
    win.BL.applyPreset('chat');
    expect(win.BL._config().matrix).toBeUndefined();
    expect(win.document.getElementById('blBenchRow').style.display).toBe('');
    expect(win.BL._config().concurrency).toEqual(before);
  });
  it('heatCells builds bench × osl grid from tagged levels at the lowest concurrency', () => {
    const win = boot();
    const levels = [
      { concurrency: 1, bench: 'throughput_1k', osl: 256, all: { pred_tps: 50 } },
      { concurrency: 1, bench: 'throughput_8k', osl: 256, all: { pred_tps: 40 } },
      { concurrency: 1, bench: 'throughput_1k', osl: 1024, all: { pred_tps: 48 } },
      { concurrency: 2, bench: 'throughput_1k', osl: 256, all: { pred_tps: 30 } },
    ];
    const h = win.BL.heatCells(levels);
    expect(h.benches).toEqual(['throughput_1k', 'throughput_8k']);
    expect(h.osls).toEqual([256, 1024]);
    expect(h.get('throughput_1k', 256)).toBe(50);
    expect(h.get('throughput_8k', 1024)).toBeNull();
    expect(h.max).toBe(50);
  });
  it('heatmap card renders cells and caption after matrix level results', async () => {
    const win = boot();
    await flush();
    win.BL._debugLevels([
      { concurrency: 1, bench: 'throughput_1k', osl: 256, all: { pred_tps: 50, agg_pred_tps: 50 }, rows: [] },
      { concurrency: 1, bench: 'throughput_32k', osl: 256, all: { pred_tps: 30, agg_pred_tps: 30 }, rows: [] },
    ]);
    expect(win.document.getElementById('blHeatCard').style.display).toBe('');
    expect(win.document.querySelectorAll('#blHeat .bl-heat-cell').length).toBe(2);
    expect(win.document.getElementById('blHeatCaption').textContent).toMatch(/32k.*40 %/);
    expect(win.document.getElementById('blCellSeg').querySelectorAll('button').length).toBe(2);
  });
  it('heatmap guards zero/null cells against NaN (final review)', async () => {
    const win = boot();
    await flush();
    win.BL._debugLevels([
      { concurrency: 1, bench: 'throughput_1k', osl: 256, all: { pred_tps: 0, agg_pred_tps: 0 }, rows: [] },
      { concurrency: 1, bench: 'throughput_32k', osl: 256, all: { pred_tps: null, agg_pred_tps: null }, rows: [] },
    ]);
    expect(win.document.getElementById('blHeat').innerHTML).not.toContain('NaN');
    expect(win.document.getElementById('blHeatCaption').textContent).not.toContain('NaN');
    expect(win.document.getElementById('blHeatCaption').textContent).toBe('');
  });
});

describe('BL final-review fixes', () => {
  it('warns when the agent ignored a submitted matrix', async () => {
    const win = boot('BL.onOpen("org/m:Q4");');
    await flush();
    win.BL.applyPreset('longctx');
    win.BL.run();
    await flush();
    win.__sse.onEvent({ type: 'model_done', run_id: 'r1', config: { bench: 'throughput_1k' }, levels: [], elapsed_s: 5 });
    expect(win.document.getElementById('blLog').textContent).toContain('agent ignored the matrix');
  });
  it('does not warn when the agent honored the matrix or none was requested', async () => {
    const win = boot('BL.onOpen("org/m:Q4");');
    await flush();
    win.BL.applyPreset('longctx');
    win.BL.run();
    await flush();
    win.__sse.onEvent({ type: 'model_done', run_id: 'r1', config: { matrix: { benches: ['throughput_1k'], osls: [256] } }, levels: [], elapsed_s: 5 });
    win.__sse.onEvent({ type: 'done', ok: true });
    expect(win.document.getElementById('blLog').textContent).not.toContain('agent ignored the matrix');
    win.BL.applyPreset('chat');
    win.BL.run();
    await flush();
    win.__sse.onEvent({ type: 'model_done', run_id: 'r1', config: { bench: 'qualitative' }, levels: [], elapsed_s: 5 });
    expect(win.document.getElementById('blLog').textContent).not.toContain('agent ignored the matrix');
  });
  it('parseOsls dedups, sorts, bounds to 16-8192, caps at 4, and falls back to [256]', () => {
    const win = boot();
    expect(win.BL.parseOsls('1024, 256, 256, 8192, 16')).toEqual([16, 256, 1024, 8192]);
    expect(win.BL.parseOsls('8, 100000, abc')).toEqual([256]);
    expect(win.BL.parseOsls('16,32,64,128,256')).toEqual([16, 32, 64, 128]);
    expect(win.BL.parseOsls('')).toEqual([256]);
  });
  it('the pending marker only lights up the active cell (#883 follow-up)', async () => {
    const win = boot('BL.onOpen("org/m:Q4");');
    await flush();
    win.BL.run();
    await flush();
    win.BL._debugLevels([
      { concurrency: 1, bench: 'throughput_1k', osl: 256, all: { pred_tps: 50, agg_pred_tps: 50 }, rows: [] },
      { concurrency: 1, bench: 'throughput_32k', osl: 256, all: { pred_tps: 30, agg_pred_tps: 30 }, rows: [] },
    ]);
    win.__sse.onEvent({ type: 'level_start', concurrency: 2, bench: 'throughput_32k', osl: 256 });
    expect(win.__chart.data.datasets[2].data.every(v => v === null)).toBe(true);
    win.__sse.onEvent({ type: 'level_start', concurrency: 2, bench: 'throughput_1k', osl: 256 });
    expect(win.__chart.data.datasets[2].data.some(v => v !== null)).toBe(true);
  });
});

describe('add to Report Card (#885)', () => {
  it('button hidden until a successful model_done, posts the run id, then offers to open the card', async () => {
    const win = boot('window.__runId = "r9"; BL.onOpen("org/m:Q4");');
    await flush();
    const btn = win.document.getElementById('blAttachBtn');
    expect(btn.style.display).toBe('none');
    win.BL.run();
    await flush();
    win.__sse.onEvent({ type: 'model_done', run_id: 'r9', ok: true, levels: [], elapsed_s: 5 });
    win.__sse.onEvent({ type: 'done', ok: true });
    await flush();
    expect(btn.style.display).toBe('');
    win.fetch = vi.fn(async () => ({ json: async () => ({ ok: true, attached: 'merged', card: {} }) }));
    win.toolsDeepLink = vi.fn();
    await win.BL.addToReportCard();
    await flush();
    expect(win.fetch.mock.calls[0][0]).toBe('/api/reportcard/attach-live');
    expect(JSON.parse(win.fetch.mock.calls[0][1].body)).toEqual({ run_id: 'r9' });
    expect(btn.textContent).toMatch(/Added.*open/);
    btn.click();
    expect(win.toolsDeepLink).toHaveBeenCalledWith('reportcard', expect.any(String));
  });

  it('stays hidden on a failed run and does not post', async () => {
    const win = boot('BL.onOpen("org/m:Q4");');
    await flush();
    win.BL.run();
    await flush();
    win.__sse.onEvent({ type: 'done', ok: false });
    await flush();
    expect(win.document.getElementById('blAttachBtn').style.display).toBe('none');
  });
});

describe('fleet ranking (#884)', () => {
  const job = { job_id: 'j1', model_id: 'org/m:Q4', done: true, cancelled: false, config: { bench: 'throughput_1k' },
    hosts: [
      { agent_id: 'a', hostname: 'alpha', status: 'done', run_id: 'ra', gen_tps: 60, agg_max_tps: 100, latency_s: 12, accept_rate: null, wh_per_ktok: 0.5 },
      { agent_id: 'b', hostname: 'bravo', status: 'done', run_id: 'rb', gen_tps: 70, agg_max_tps: 120, latency_s: 10, accept_rate: 0.5, wh_per_ktok: null },
      { agent_id: 'c', hostname: 'charlie', status: 'failed', run_id: null, error: 'Another benchmark or autotune is in progress' },
    ], ranking: ['b', 'a'] };
  it('rankHosts orders done hosts by decode t/s and computes pct of best', () => {
    const win = boot();
    const r = win.BL.rankHosts(job.hosts);
    expect(r.map(h => h.hostname)).toEqual(['bravo', 'alpha', 'charlie']);
    expect(r[0].rank).toBe(1); expect(r[1].pctOfBest).toBeCloseTo(60 / 70); expect(r[2].rank).toBeNull();
  });
  it('a finished fleet job is cleared when another model opens or a normal run starts', async () => {
    const win = boot(); await flush();
    win.fetch = vi.fn(async () => ({ json: async () => ({ ok: true, server: { up: true }, runtime: {}, runs: [], hosts: [] }) }));
    win.BL._debugFleet(job);
    const card = win.document.getElementById('blFleetCard');
    expect(card.style.display).toBe('');
    await win.BL.onOpen('org/other:Q8'); await flush();
    expect(card.style.display).toBe('none');
    win.BL._debugFleet(job);
    expect(card.style.display).toBe('');
    await win.BL.run({ model_id: 'org/other:Q8', concurrency: [1], extra_inputs: {} }); await flush();
    expect(card.style.display).toBe('none');
  });
  it('renders the ranking table with rank badges, bars, status and Δ vs best; clicking a done row loads its run', async () => {
    const win = boot(); await flush();
    win.fetch = vi.fn(async (url) => ({ json: async () => url.includes('/runs/ra') ? { ok: true, meta: {}, run: { levels: [{ concurrency: 1, all: { pred_tps: 60, agg_pred_tps: 60 }, rows: [] }] } } : { ok: true } }));
    win.BL._debugFleet(job);
    const card = win.document.getElementById('blFleetCard');
    expect(card.style.display).toBe('');
    const rows = card.querySelectorAll('tbody tr');
    expect(rows.length).toBe(3);
    expect(rows[0].textContent).toContain('bravo'); expect(rows[0].querySelector('.bl-rank').textContent).toBe('1');
    expect(rows[1].textContent).toMatch(/−14 %/);
    expect(rows[2].textContent).toContain('failed'); expect(rows[2].title || rows[2].querySelector('[title]').title).toContain('in progress');
    rows[1].click(); await flush();
    expect(win.fetch.mock.calls.some(c => String(c[0]).includes('/api/benchmark/live/runs/ra'))).toBe(true);
    expect(rows[1].classList.contains('on')).toBe(true);
  });
  it('fleet toggle loads hosts and the run posts a fleet job', async () => {
    const win = boot('BL.onOpen("org/m:Q4");'); await flush();
    win.fetch = vi.fn(async (url, opts) => ({ json: async () => url.includes('/hosts') ? { ok: true, hosts: [
      { agent_id: 'a', hostname: 'alpha', online: true, loaded: true }, { agent_id: 'c', hostname: 'charlie', online: true, loaded: false }] }
      : url.includes('/fleet') && opts ? { ok: true, job_id: 'j2', hosts: [] } : { ok: true, job: { ...job, done: false, hosts: [] } } }));
    win.BL.toggleFleet(); await flush();
    const chips = win.document.querySelectorAll('#blFleetHosts .bl-chip');
    expect(chips.length).toBe(2);
    expect(chips[0].classList.contains('on')).toBe(true); expect(chips[1].classList.contains('off')).toBe(true);
    await win.BL.run(); await flush();
    const post = win.fetch.mock.calls.find(c => String(c[0]) === '/api/benchmark/live/fleet' && c[1]);
    expect(JSON.parse(post[1].body)).toMatchObject({ model_id: expect.any(String), agents: ['a'] });
    expect(JSON.parse(post[1].body).config.model_id).toBeUndefined();
  });
});

describe('BL export', () => {
  it('downloads the report as a JSON file instead of opening a blocked tab', async () => {
    const win = boot('BL.onOpen("org/m:Q4");');
    await flush();
    win.BL.run();
    await flush();
    win.__sse.onEvent({ type: 'model_done', run_id: 'r1', levels: [], elapsed_s: 10 });
    win.URL.createObjectURL = () => 'blob:mock';
    win.URL.revokeObjectURL = () => {};
    const clickSpy = vi.spyOn(win.HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    win.BL.exportJson();
    expect(clickSpy).toHaveBeenCalledTimes(1);
    const a = win.document.body.querySelector('a[download]');
    expect(a).toBeTruthy();
    expect(a.download).toMatch(/^bench-live-.*\.json$/);
    clickSpy.mockRestore();
  });
});
