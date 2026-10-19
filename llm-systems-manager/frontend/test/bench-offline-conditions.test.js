// #1302: Offline bench keeps the run conditions, shows them, and compares with earlier ledger runs.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const BODY = `
  <div id="benchModelPanel"><input type="checkbox" value="org/m" checked></div>
  <span id="benchModelLabel"></span>
  <div class="bench-tab active" data-tab="llama-bench"></div>
  <div id="benchSwitchList"></div><span id="benchSwitchLabel"></span><div id="benchSwitchPanel"></div>
  <button id="benchRunBtn"></button>
  <button id="benchCancelBtn"></button>
  <span id="benchStatus">idle</span>
  <div id="benchResults"><div id="benchResultRows"></div><div id="benchCond"></div><div id="benchCompare" class="bench-compare"></div></div>
  <div id="benchLog"></div>
  <div id="benchChartWrap"><div id="benchChartEmpty"></div><canvas id="benchChart"></canvas></div>
`;

const COND_A = { hardware: { gpu: '7900 XTX', driver: 'amdgpu 6.12.0' }, backend: { name: 'llama.cpp', build: 'b1' } };
const COND_B = { ...COND_A, backend: { name: 'llama.cpp', build: 'b2' } };
const ROWS = [
  { tool: 'benchmark', model_id: 'org/m', agent_id: 'a1', run_id: 'run1', ts: '2026-10-18T10:00:00Z', ok: true, summary: { gen_tps: 50 }, conditions: COND_B },
  { tool: 'benchmark', model_id: 'org/m', agent_id: 'a1', run_id: 'old', ts: '2026-10-17T10:00:00Z', ok: true, summary: { gen_tps: 48.25, switches: { '-ngl': '99' } }, conditions: COND_A },
  { tool: 'benchmark', model_id: 'org/m', agent_id: 'a2', run_id: 'other-host', ts: '2026-10-18T09:00:00Z', ok: true, summary: { gen_tps: 30 }, conditions: COND_A },
];

const STUBS = `
  window.cssVar = () => '#888';
  window.adminEsc = (s) => String(s);
  window.shortName = (s) => String(s);
  window._themedConfirm = () => Promise.resolve(true);
  window._toastErr = () => {};
  window.toolsSyncRunDot = function () {};
  HTMLCanvasElement.prototype.getContext = function () { return {}; };
  window.Chart = function (ctx, cfg) { this.data = cfg.data; this.options = cfg.options; };
  Chart.prototype.update = function () {};
  Chart.prototype.resize = function () {};
  Chart.prototype.destroy = function () {};
  window.__streams = [];
  window.EventSource = function (url) { this.url = url; this.readyState = 0; window.__streams.push(this); };
  EventSource.CONNECTING = 0; EventSource.OPEN = 1; EventSource.CLOSED = 2;
  EventSource.prototype.close = function () { this.readyState = 2; };
  window.__fetches = [];
  window.__runsReply = ${JSON.stringify(ROWS)};
  window.fetch = function (url, opts) {
    const u = String(url);
    window.__fetches.push([u, opts]);
    if (u.indexOf('/api/tools/runs?tool=') === 0 && window.__runsReply === null) return Promise.reject(new Error('down'));
    const body =
      u.indexOf('/api/tools/runs?tool=') === 0 ? { runs: window.__runsReply }
      : u.indexOf('/api/benchmark/models') === 0 ? { models: ['org/m'] }
      : u.indexOf('/api/llama-state') === 0 ? { state: 'unknown' }
      : { ok: true };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  };
`;

function boot() {
  return runHarness({
    sources: [STUBS, srcFile('js/lib/sseguard.js'), srcFile('js/lib/modelcards.js'), srcFile('js/lib/conditions.js'),
              srcFile('js/lib/toolcards.js'), srcFile('js/bench-autotune.js')],
    bodyHtml: BODY,
    bootstrap: `switchBenchTab('llama-bench');`,
  });
}

async function started() {
  const win = boot();
  await win.runBenchmark();
  await flush();
  const es = win.__streams[win.__streams.length - 1];
  es.readyState = 1;
  const send = (msg) => es.onmessage({ data: JSON.stringify(msg), lastEventId: 'run1:7' });
  return { win, send };
}

const done = (o) => ({ type: 'model_done', model_id: 'org/m', max_gen_tps: 50, max_ppt_tps: 900, max_pg_tps: 300,
  wh_per_ktok: 1.5, energy_wh: 2, energy_source: 'psu', conditions: COND_B, ...o });

describe('Offline bench conditions + comparison (#1302)', () => {
  it('shows the block, records conditions and compares with a picked ledger run', async () => {
    const { win, send } = await started();
    const d = win.document;
    send(done());
    for (let i = 0; i < 3; i++) await flush();
    expect(d.querySelector('#benchCond details.cond-block')).not.toBeNull();
    const post = win.__fetches.find(([u, o]) => u === '/api/tools/runs' && o && o.method === 'POST');
    expect(JSON.parse(post[1].body).conditions).toEqual(COND_B);
    const q = win.__fetches.filter(([u]) => u.indexOf('/api/tools/runs?tool=') === 0);
    expect(q.length).toBe(1);
    const p = new URL(q[0][0], 'http://x').searchParams;
    expect([p.get('tool'), p.get('model_id')]).toEqual(['benchmark', 'org/m']);
    const sel = d.querySelector('#benchCompare select.bl-sel');
    expect([...sel.options].map(o => o.value)).toEqual(['', 'old']);   // the current run and other agents are left out
    expect(sel.options[1].textContent).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2} · 48\.3 t\/s$/);
    sel.value = 'old'; sel.dispatchEvent(new win.Event('change'));
    const cmp = d.getElementById('benchCompare');
    expect(cmp.textContent).toContain('Compare · Benchmark · org/m');
    expect(cmp.textContent).toContain('Conditions');
    win._benchCompareChange('org/m', '');
    expect(cmp.textContent).not.toContain('Compare ·');
    expect(d.querySelector('#benchCompare select.bl-sel')).not.toBeNull();
  });

  it('save sends the conditions in extra_json next to the energy keys', async () => {
    const { win, send } = await started();
    send(done());
    await flush();
    win.document.querySelector('#benchResultRows .bench-result-row button').click();
    await flush();
    const store = win.__fetches.find(([u]) => u === '/api/benchmark/store');
    const body = JSON.parse(store[1].body);
    expect(body.extra_json.conditions).toEqual(COND_B);
    expect(body.extra_json.wh_per_ktok).toBe(1.5);
  });

  it('clearing a stored result drops its conditions, hosts and cached runs', async () => {
    const { win, send } = await started();
    const d = win.document;
    send(done());
    for (let i = 0; i < 3; i++) await flush();
    expect(d.getElementById('benchCond').innerHTML).not.toBe('');
    win.clearStoredBenchmark('org/m', null);
    for (let i = 0; i < 3; i++) await flush();
    expect(d.getElementById('benchCond').innerHTML).toBe('');
    expect(d.getElementById('benchCompare').innerHTML).toBe('');
    expect(win.eval('Object.keys(_benchCur).length + Object.keys(_benchCmpRows).length')).toBe(0);
    win.saveBenchmark('org/m', 50, 900, 300, 'llama-bench', null, { wh_per_ktok: 1.5 });
    await flush();
    const store = win.__fetches.filter(([u]) => u === '/api/benchmark/store').pop();
    const body = JSON.parse(store[1].body);
    expect(body.extra_json).toEqual({ wh_per_ktok: 1.5 });
  });

  it('a model_done without conditions leaves the block empty but keeps the picker', async () => {
    const { win, send } = await started();
    send(done({ conditions: undefined }));
    for (let i = 0; i < 3; i++) await flush();
    expect(win.document.getElementById('benchCond').innerHTML).toBe('');
    expect(win.document.querySelector('#benchCompare select.bl-sel')).not.toBeNull();
  });

  it('a failed ledger read or no earlier runs leaves the picker disabled', async () => {
    const { win, send } = await started();
    win.__runsReply = [ROWS[0]];
    send(done());
    for (let i = 0; i < 3; i++) await flush();
    let sel = win.document.querySelector('#benchCompare select.bl-sel');
    expect(sel.disabled).toBe(true);
    expect([...sel.options].map(o => o.textContent)).toEqual(['no earlier runs']);
    win.__runsReply = null;
    send(done({ model_id: 'org/n' }));
    for (let i = 0; i < 3; i++) await flush();
    sel = win.document.querySelector('#benchCompare select.bl-sel');
    expect(sel.disabled).toBe(true);
  });

  it('clears both hosts when a new run starts', async () => {
    const { win, send } = await started();
    send(done());
    for (let i = 0; i < 3; i++) await flush();
    expect(win.document.getElementById('benchCond').innerHTML).not.toBe('');
    send({ type: 'done', ok: true });
    await win.runBenchmark();
    expect(win.document.getElementById('benchCond').innerHTML).toBe('');
    expect(win.document.getElementById('benchCompare').innerHTML).toBe('');
  });

  it('reopening the tool shows the conditions of a stored result', async () => {
    const win = boot();
    win.eval(`_benchData = { 'org/m': { model_id: 'org/m', avg_gen_tps: 50, avg_ppt_tps: 900, avg_pg_tps: 300, bench_tool: 'llama-bench',
      switches: [{ flag: '-ngl', value: '99' }], ts: '2026-10-18T10:00:00Z', extra_json: { wh_per_ktok: 1.5, conditions: ${JSON.stringify(COND_B)} } } };`);
    await win.openBench('org/m');
    for (let i = 0; i < 3; i++) await flush();
    expect(win.document.querySelector('#benchCond details.cond-block')).not.toBeNull();
    const sel = win.document.querySelector('#benchCompare select.bl-sel');
    expect(sel.options.length).toBe(4);   // a stored result has no run id, so every ledger run is listed
  });

  it('hides the diff Clear button and places the hosts after the result rows', () => {
    expect(srcFile('css/base.css')).toMatch(/\.bench-compare \[data-diff="clear"\]/);
    expect(srcFile('index.html')).toMatch(/id="benchResultRows"[^>]*><\/div>\s*<div id="benchCond"><\/div><div id="benchCompare" class="bench-compare"><\/div>/);
  });
});
