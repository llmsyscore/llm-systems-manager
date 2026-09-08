// #880: Autotune module — dims state, plan card, estimate, stream plumbing, stepper.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const INDEX = srcFile('index.html');
// The real module markup, so ids and classes cannot drift from index.html.
const BODY = INDEX.slice(INDEX.indexOf('<div id="toolsModAt"'), INDEX.indexOf('<!-- /toolsModAt -->'));

const PRE = {
  ok: true, busy: false, unit_active: false, cores: { physical: 16, logical: 32 }, perplexity: true,
  runtime: { ok: true }, drafts: [{ repo: 'unsloth/Qwen3-0.6B-GGUF', file: 'Qwen3-0.6B-Q8_0.gguf', path: '/h/a.gguf', size: 7e8 }],
  sizes: { 'org/big:Q4': 90e9, 'org/m:Q4': 18e9 }, vram_total_mb: 32768, ram_total_mb: 32768,
};

// foundation.js declares `let layout` at top level, so window.layout is undefined;
// its own source string reproduces that classic-script scope.
const LAYOUT = `
  let layout = {}; window.__layout = () => layout; window.saveLayout = function () {};
`;

const STUBS = `
  window.TC = { esc: (s) => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])) };
  window.SG = { open: (opts) => { window.__sse = opts; return { close() { window.__closed = true; } }; } };
  window.toolsSyncRunDot = function () {};
  window.__alerts = [];
  window.alert = function (m) { window.__alerts.push(String(m)); };
  window.__fetches = [];
  window.__syncCalls = [];
  window._syncActiveProfile = function (mid, values) { window.__syncCalls.push([mid, values, window.__fetches.length]); return Promise.resolve(); };
  window.__pre = ${JSON.stringify(PRE)};
  window.fetch = function (url, opts) {
    window.__fetches.push([String(url), opts]);
    const u = String(url);
    const body = u.startsWith('/api/llm/autotune/preflight') ? window.__pre
      : u.startsWith('/api/benchmark/models') ? { models: ['org/m:Q4', 'org/big:Q4'] }
      : u.startsWith('/api/tools/runs') ? { runs: [{ tool: 'autotune', model_id: 'org/m:Q4', ok: true, ts: '2026-08-28T10:00:00Z', summary: { objective: 'fit', ctx_size: 32768, n_expert: 128 } }], latest: {} }
      : u.startsWith('/api/llama-state') ? { state: window.__llamaState || 'stopped' }
      : u.startsWith('/api/llm/config') && !(opts && opts.method) ? { __DEFAULTS__: {}, 'org/m:Q4': { 'ctx-size': '32768', threads: '32', temperature: '0.8' } }
      : u.startsWith('/api/llm/model-meta') ? { repo: 'org/m', base_model: 'org/base', suggestions: [{ key: 'temperature', value: 0.7, source: 'sidecar' }, { key: 'top-p', value: 0.8, source: 'model_card' }, { key: 'min-p', value: 0, source: 'base_model' }] }
      : u.startsWith('/api/llm/autotune/run') ? (window.__runReply || { ok: true, run_id: 'r1' })
      : (u === '/api/llm/config' && opts && opts.method === 'POST') ? (window.__failConfig ? { ok: false, error: 'boom' } : { ok: true })
      : { ok: true };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
  };
`;

function boot(bootstrap = '') {
  return runHarness({ sources: [LAYOUT, STUBS, srcFile('js/autotune.js')], bodyHtml: BODY, bootstrap });
}

async function opened() {
  const win = boot();
  win.AT.onOpen('org/m:Q4');
  for (let i = 0; i < 6; i++) await flush();
  return win;
}

describe('AT rail', () => {
  it('reads the dimension state from the DOM with defaults', () => {
    const win = boot();
    const d = win.AT.dimsState();
    expect(d.context).toEqual({ on: true, target_mb: 1024, tolerance_mb: 50, custom_args: [] });
    expect(d.kv.candidates).toEqual(['f16', 'q8_0', 'q4_0']);
    expect(d.slots).toEqual({ on: true, candidates: [1, 2, 4, 8], min_ctx_per_slot: 32768 });
    expect(d.spec.types).toEqual(['auto']);
    expect(d.sampling).toEqual({ on: true, overwrite: false });
  });

  it('seeds thread chips from the physical core count and tags models', async () => {
    const win = await opened();
    const chips = [...win.document.querySelectorAll('#atThreadChips .bl-chip')].map(c => c.dataset.v);
    expect(chips).toEqual(['8', '12', '16', '32']);
    expect(win.AT.dimsState().threads.candidates).toEqual([8, 12, 16]);
    const list = win.document.getElementById('atModelList');
    expect(list.querySelectorAll('input[type=checkbox]').length).toBe(2);
    expect(list.textContent).toContain('MoE · 128 experts');
    expect(list.textContent).toContain('no fit');
    expect(win.document.getElementById('atDraftSel').options.length).toBe(3);
    expect(win.document.getElementById('atPrevTune').textContent).toContain('2026-08-28');
  });

  it('objective persists and the MoE row follows the primary model', async () => {
    const win = await opened();
    win.document.querySelector('#atObjSeg [data-obj="serve"]').click();
    expect(win.AT.objective()).toBe('serve');
    expect(win.layout).toBeUndefined();                // real scope: `let layout`, not a window prop
    expect(win.__layout().atObjective).toBe('serve');
    const moe = win.document.querySelector('.at-dim[data-dim="moe"]');
    expect(moe.style.display).toBe('');            // MoE fact from the ledger → shown
    win.document.querySelector('#atModelList input[value="org/big:Q4"]').click();
    win.document.querySelector('#atModelList input[value="org/m:Q4"]').click();
    expect(moe.style.display).toBe('');            // unknown model (no fact yet) → still shown
    expect(win.AT.planRows('balanced', win.AT.dimsState(), win.__pre, { n_expert: 0 }).find(r => r.stage === 'moe').on).toBe(false);
  });

  it('opens with one preflight fetch and re-reads it after a stop', async () => {
    const win = await opened();
    const pre = () => win.__fetches.filter(([u]) => u.startsWith('/api/llm/autotune/preflight')).length;
    expect(pre()).toBe(1);
    await win.AT.stopServer();
    expect(pre()).toBe(2);                             // stopServer must not reuse the open-time doc
  });

  it('builds the plan rows and the estimate', () => {
    const win = boot();
    const rows = win.AT.planRows('balanced', win.AT.dimsState(), PRE, { n_expert: 128 });
    expect(rows.map(r => r.stage)).toEqual(['context', 'kv', 'moe', 'threads', 'spec', 'slots', 'sampling', 'verify']);
    expect(rows[1].flag).toBe('-ctk -ctv');
    expect(rows.every(r => r.est_s > 0)).toBe(true);
    const off = win.AT.planRows('fit', { ...win.AT.dimsState(), kv: { on: false } }, PRE, { n_expert: 0 });
    expect(off.find(r => r.stage === 'kv').on).toBe(false);
    expect(off.find(r => r.stage === 'moe').on).toBe(false);   // dense model
    expect(win.AT.estimateText(rows)).toMatch(/~\d+ min/);
    const noRt = win.AT.planRows('balanced', win.AT.dimsState(), { ...PRE, runtime: { ok: false } }, {});
    expect(noRt.find(r => r.stage === 'threads').desc).toContain('runtime');
  });
});

describe('AT run + stream', () => {
  it('posts the v2 body and switches to the running pane', async () => {
    const win = await opened();
    await win.AT.run();
    await flush();
    const post = win.__fetches.find(f => f[0] === '/api/llm/autotune/run');
    const body = JSON.parse(post[1].body);
    expect(body.objective).toBe('balanced');
    expect(body.model_ids).toEqual(['org/m:Q4']);
    expect(body.dims.kv.candidates).toEqual(['f16', 'q8_0', 'q4_0']);
    expect(body.budget_min).toBe(120);
    expect(win.__sse.url).toBe('/api/llm/autotune/stream');
    expect(win.AT.running()).toBe(true);
    expect(win.document.getElementById('atPaneRun').style.display).toBe('');
    expect(win.document.getElementById('atCancelBtn').style.display).toBe('');
  });

  it('drives the stepper from the event stream', async () => {
    const win = await opened();
    await win.AT.run();
    await flush();
    const ev = (m) => win.__sse.onEvent({ model_id: 'org/m:Q4', ...m }, {});
    ev({ type: 'model_start', objective: 'balanced', stages: ['context', 'kv', 'moe', 'threads', 'spec', 'slots', 'sampling', 'verify'] });
    const steps = () => [...win.document.querySelectorAll('#atStepper .at-step')];
    expect(steps().length).toBe(8);
    ev({ type: 'stage_start', stage: 'context', candidates: ['baseline', '-fitt 1024'], est_s: 300 });
    expect(steps()[0].classList.contains('live')).toBe(true);
    ev({ type: 'iter_result', iter: 1, fitt: 1024, n_ctx_seq: 65536, actual_free_mb: 1012, total_vram_mb: 32768 });
    expect(win.document.getElementById('atStageBody').innerHTML).toContain('at-gauge');
    ev({ type: 'stage_done', stage: 'context', choice: '65536', reason: 'converged', seconds: 351, loads: 5 });
    expect(steps()[0].classList.contains('done')).toBe(true);
    expect(steps()[0].textContent).toContain('65536');
    ev({ type: 'stage_start', stage: 'kv', candidates: ['q8_0', 'q4_0'], est_s: 200 });
    ev({ type: 'candidate_result', stage: 'kv', value: 'q8_0', ok: true, ctx: 131072, kl: 0.006, guard_pass: true });
    expect(win.document.getElementById('atStageBody').innerHTML).toContain('0.006');
    ev({ type: 'stage_skipped', stage: 'moe', reason: 'fits' });
    expect(steps()[2].classList.contains('skipped')).toBe(true);
    ev({ type: 'stage_start', stage: 'threads', candidates: [8, 12, 16], est_s: 90 });
    ev({ type: 'candidate_result', stage: 'threads', value: 8, ok: true, decode_tps: 100 });
    ev({ type: 'candidate_result', stage: 'threads', value: 12, ok: false, error: 'OOM' });
    const bars = win.document.querySelectorAll('#atStageBody .at-bar');
    expect(bars.length).toBe(3);
    expect(bars[1].classList.contains('fail')).toBe(true);
    expect(bars[2].classList.contains('pend')).toBe(true);
    expect(win.document.getElementById('atStripStage').textContent).toMatch(/stage 4 \/ 8/);
    ev({ type: 'done', ok: true });
    expect(win.__closed).toBe(true);
    expect(win.AT.running()).toBe(false);
  });

  it('shows time left once a stage is live, and re-derives the total from live progress', async () => {
    const win = await opened();
    await win.AT.run();
    await flush();
    const ev = (m) => win.__sse.onEvent({ model_id: 'org/m:Q4', ...m }, {});
    const order = ['context', 'kv', 'moe', 'threads', 'spec', 'slots', 'sampling', 'verify'];
    ev({ type: 'model_start', objective: 'balanced', stages: order });
    ev({ type: 'stage_start', stage: 'context', candidates: ['baseline'], est_s: 300 });
    expect(win.document.getElementById('atStripTime').textContent).toContain('left');

    const rows = win.AT.planRows('balanced', win.AT.dimsState(), win.__pre, { n_expert: 128 });
    const est = {}; rows.forEach(r => { est[r.stage] = r.est_s; });
    ev({ type: 'stage_start', stage: 'threads', candidates: [8, 12, 16], est_s: 600 });
    const expectedRest = order.slice(order.indexOf('threads') + 1).reduce((a, s) => a + (est[s] || 0), 0);
    expect(win.AT.state().run.estTotal).toBeCloseTo(600 + expectedRest, 0);
  });

  it('shows "past estimate" and pins the bar at 97% once elapsed outgrows the total', async () => {
    const win = await opened();
    await win.AT.run();
    await flush();
    const ev = (m) => win.__sse.onEvent({ model_id: 'org/m:Q4', ...m }, {});
    ev({ type: 'model_start', objective: 'balanced', stages: ['context', 'kv', 'moe', 'threads', 'spec', 'slots', 'sampling', 'verify'] });
    ev({ type: 'stage_start', stage: 'context', candidates: ['baseline'], est_s: 300 });
    const estTotal = win.AT.state().run.estTotal;
    win.AT._debugSetStart(Date.now() - (estTotal + 120) * 1000);
    expect(win.document.getElementById('atStripTime').textContent).toContain('past estimate');
    expect(win.document.getElementById('atProgBar').style.width).toBe('97%');
  });

  it('attaches to a run that is already busy on the agent', async () => {
    const win = boot();
    win.__pre = { ...PRE, busy: true };
    win.AT.onOpen('org/m:Q4');
    for (let i = 0; i < 6; i++) await flush();
    expect(win.__sse && win.__sse.url).toBe('/api/llm/autotune/stream');
    expect(win.AT.running()).toBe(true);
    expect(win.document.getElementById('atRunBtn').disabled).toBe(true);
  });
});

describe('AT preflight gating', () => {
  it('re-reads preflight so the banner clears once the server is really down', async () => {
    const win = boot();
    win.__pre = { ...PRE, unit_active: true };
    win.AT.onOpen('org/m:Q4');
    for (let i = 0; i < 6; i++) await flush();
    expect(win.document.getElementById('atRunBtn').disabled).toBe(true);
    expect(win.document.getElementById('atPreflight').style.display).toBe('');
    expect(win.document.getElementById('atStopBtn').style.display).toBe('');
    // The unit is gone; the cached preflight must not keep the banner up.
    win.__pre = { ...PRE, unit_active: false };
    await win.AT.stopServer();
    for (let i = 0; i < 4; i++) await flush();
    expect(win.__fetches.some(f => f[0] === '/api/llm/server/stop')).toBe(true);
    expect(win.document.getElementById('atRunBtn').disabled).toBe(false);
    expect(win.document.getElementById('atPreflight').style.display).toBe('none');
    expect(win.document.getElementById('atStopBtn').style.display).toBe('none');
  });

  it('keeps the banner up while llama-state still reports the server awake', async () => {
    const win = boot();
    win.__llamaState = 'awake';
    win.AT.onOpen('org/m:Q4');
    for (let i = 0; i < 6; i++) await flush();
    expect(win.document.getElementById('atRunBtn').disabled).toBe(true);
    expect(win.document.getElementById('atPreflight').style.display).toBe('');
  });

  it('warns without disabling Run when --help could not be parsed', async () => {
    const win = boot();
    win.__pre = { ...PRE, help_valued: { ok: false, count: 0 } };
    win.AT.onOpen('org/m:Q4');
    for (let i = 0; i < 6; i++) await flush();
    expect(win.document.getElementById('atPreflight').style.display).toBe('');
    expect(win.document.getElementById('atPreflightMsg').textContent).toContain('--help');
    expect(win.document.getElementById('atRunBtn').disabled).toBe(false);
  });

  it('hides the banner when --help parsed fine and the server is stopped', async () => {
    const win = boot();
    win.__pre = { ...PRE, help_valued: { ok: true, count: 12 } };
    win.AT.onOpen('org/m:Q4');
    for (let i = 0; i < 6; i++) await flush();
    expect(win.document.getElementById('atPreflight').style.display).toBe('none');
  });
});

describe('AT dimension validation', () => {
  it('refuses to post when an enabled dimension has no candidates', async () => {
    const win = await opened();
    win.document.querySelectorAll('#atKvChips .bl-chip.on').forEach(c => c.click());
    expect(win.AT.dimsState().kv.candidates).toEqual([]);
    await win.AT.run();
    await flush();
    expect(win.__alerts.join(' ')).toContain('KV cache type');
    expect(win.__fetches.some(f => f[0] === '/api/llm/autotune/run')).toBe(false);
    expect(win.AT.running()).toBe(false);
  });

  it('refuses an inverted draft window', async () => {
    const win = await opened();
    win.document.getElementById('atSpecNmin').value = '16';
    win.document.getElementById('atSpecNmax').value = '16';
    await win.AT.run();
    await flush();
    expect(win.__alerts.join(' ')).toContain('draft window');
    expect(win.__fetches.some(f => f[0] === '/api/llm/autotune/run')).toBe(false);
  });

  it('refuses an inverted MoE range', async () => {
    const win = await opened();
    win.document.getElementById('atMoeMin').value = '20';
    win.document.getElementById('atMoeMax').value = '4';
    await win.AT.run();
    await flush();
    expect(win.__alerts.join(' ')).toContain('MoE CPU offload');
    expect(win.__fetches.some(f => f[0] === '/api/llm/autotune/run')).toBe(false);
  });

  it('still posts non-empty lists for dimensions that are switched off', async () => {
    const win = await opened();
    win.document.querySelector('.at-dim[data-dim="kv"] [data-dim-on]').click();
    win.document.querySelector('.at-dim[data-dim="slots"] [data-dim-on]').click();
    win.document.querySelectorAll('#atKvChips .bl-chip.on').forEach(c => c.click());
    win.document.querySelectorAll('#atSlotChips .bl-chip.on').forEach(c => c.click());
    const d = win.AT.dimsState();
    expect(d.kv.on).toBe(false);
    expect(d.slots.on).toBe(false);
    await win.AT.run();
    await flush();
    const post = win.__fetches.find(f => f[0] === '/api/llm/autotune/run');
    expect(post).toBeTruthy();
    const body = JSON.parse(post[1].body);
    expect(body.dims.kv.candidates).toEqual(['f16', 'q8_0', 'q4_0']);
    expect(body.dims.slots.candidates).toEqual([1, 2, 4, 8]);
    expect(body.dims.spec.types).toEqual(['auto']);
  });

  it('still posts a valid threads dim when switched off with no chips selected', async () => {
    const win = await opened();
    win.document.querySelector('.at-dim[data-dim="threads"] [data-dim-on]').click();
    win.document.querySelectorAll('#atThreadChips .bl-chip.on').forEach(c => c.click());
    const d = win.AT.dimsState();
    expect(d.threads).toEqual({ on: false, candidates: [] });
    await win.AT.run();
    await flush();
    const post = win.__fetches.find(f => f[0] === '/api/llm/autotune/run');
    expect(post).toBeTruthy();
    const body = JSON.parse(post[1].body);
    // The agent runs _list() on candidates whenever the key is present, and
    // _list() rejects []. Omitted → the agent seeds from the host's cores.
    expect('candidates' in body.dims.threads).toBe(false);
    expect(body.dims.threads.on).toBe(false);
  });

  it('surfaces a FastAPI detail body in the failure alert', async () => {
    const win = await opened();
    win.__runReply = { detail: 'kv candidates must not be empty' };
    await win.AT.run();
    await flush();
    expect(win.__alerts.join(' ')).toContain('kv candidates must not be empty');
    expect(win.AT.running()).toBe(false);
  });
});

describe('AT multi-model + panes', () => {
  it('resets the per-model view on the second model_start but keeps the run clock', async () => {
    const win = await opened();
    await win.AT.run();
    await flush();
    const ev = (m) => win.__sse.onEvent({ model_id: 'org/m:Q4', ...m }, {});
    ev({ type: 'model_start', objective: 'balanced', stages: ['context', 'kv', 'sampling', 'verify'] });
    ev({ type: 'stage_start', stage: 'context', candidates: ['baseline'], est_s: 300 });
    ev({ type: 'iter_result', iter: 1, fitt: 1024, n_ctx_seq: 65536, actual_free_mb: 1012, total_vram_mb: 32768 });
    ev({ type: 'stage_done', stage: 'context', choice: '65536', reason: 'converged', seconds: 351, loads: 5 });
    const startTs = win.AT.state().run.startTs, estTotal = win.AT.state().run.estTotal;
    expect(win.AT.state().run.iters.length).toBe(1);

    win.__sse.onEvent({ type: 'model_start', model_id: 'org/big:Q4', objective: 'balanced', stages: ['context', 'kv', 'sampling', 'verify'] }, {});
    const st = win.AT.state().run;
    expect(st.iters).toEqual([]);
    expect(st.cands).toEqual({});
    expect(st.current).toBe(null);
    expect(st.startTs).toBe(startTs);
    expect(st.estTotal).toBe(estTotal);
    const steps = [...win.document.querySelectorAll('#atStepper .at-step')];
    expect(steps.every(s => s.classList.contains('pending'))).toBe(true);
    expect(steps[0].textContent).not.toContain('65536');
  });

  it('shows a hint rather than an empty chart for the sampling stage', async () => {
    const win = await opened();
    await win.AT.run();
    await flush();
    win.__sse.onEvent({ type: 'stage_start', model_id: 'org/m:Q4', stage: 'sampling', candidates: [], est_s: 5 }, {});
    const body = win.document.getElementById('atStageBody');
    expect(body.textContent).toContain('from metadata');
    expect(body.querySelectorAll('.at-bars').length).toBe(0);
  });

  it('again() hides the Run again button and returns to the plan pane', async () => {
    const win = await opened();
    await win.AT.run();
    await flush();
    const ev = (m) => win.__sse.onEvent({ model_id: 'org/m:Q4', ...m }, {});
    ev({ type: 'model_done', ok: true, objective: 'balanced', changes: [], verify: { ok: true }, after: { ctx: 65536 } });
    ev({ type: 'done', ok: true });
    expect(win.document.getElementById('atPaneDone').style.display).toBe('');
    expect(win.document.getElementById('atAgainBtn').style.display).toBe('');
    win.AT.again();
    expect(win.document.getElementById('atPanePlan').style.display).toBe('');
    expect(win.document.getElementById('atAgainBtn').style.display).toBe('none');
    expect(win.AT.state().doneModel).toBe(null);
  });
});

const DONE = {
  type: 'model_done', model_id: 'org/m:Q4', run_id: 'r1', ok: true, objective: 'balanced',
  facts: { n_expert: 128, n_layer: 48, mtp_layers: 1 },
  changes: [
    { key: 'ctx-size', current: '32768', recommended: '65536', source: 'measured', evidence: 'free 1012 MB after fit · 5 loads', selected: true },
    { key: 'cache-type-k', current: '', recommended: 'q8_0', source: 'measured', evidence: 'KL 0.006', selected: true },
    { key: 'threads', current: '32', recommended: '12', source: 'measured', evidence: '12 = 16 within noise', selected: false },
    { key: 'parallel', current: '', recommended: '4', source: 'measured', evidence: 'aggregate 3.1×', selected: true },
  ],
  before: { decode_tps: 101, prefill_tps: 2355, ctx: 32768, agg_tps: 101, free_mb: 2960 },
  after: { decode_tps: 142.6, prefill_tps: 2310, ctx: 65536, agg_tps: 402, free_mb: 1012, concurrency: 4, wh_per_ktok: 0.24, energy_source: 'psu' },
  guard: { kl: 0.006, max: 0.02, pass: true, text: 'q8_0 KV vs f16 · pass' },
  verify: { ok: true, seconds: 60, free_mb: 1012, dropped: [], reason: null },
  stages: [{ stage: 'context', status: 'done', seconds: 351, loads: 5, choice: '65536' }, { stage: 'kv', status: 'done', seconds: 500, loads: 6, choice: 'q8_0' },
           { stage: 'moe', status: 'skipped', reason: 'fits' }, { stage: 'verify', status: 'done', seconds: 90, loads: 1, choice: 'pass' }],
  stop_reason: null, elapsed_s: 2091, loads: 23,
};

async function finished(win) {
  await win.AT.run();
  for (let i = 0; i < 4; i++) await flush();
  win.__sse.onEvent({ type: 'model_start', model_id: 'org/m:Q4', objective: 'balanced', stages: ['context', 'kv', 'moe', 'verify'] }, {});
  win.__sse.onEvent(DONE, {});
  win.__sse.onEvent({ type: 'done', ok: true }, {});
  await flush();
}

describe('AT recommendation', () => {
  it('builds rows from changes and metadata with the overwrite rule', () => {
    const win = boot();
    const meta = { repo: 'org/m', base_model: 'org/base', suggestions: [{ key: 'temperature', value: 0.7, source: 'sidecar' }, { key: 'top-p', value: 0.8, source: 'model_card' }, { key: 'min-p', value: 0, source: 'base_model' }] };
    const rows = win.AT.recRows(DONE, { temperature: '0.8', 'min-p': '0' }, meta, false);
    expect(rows.map(r => r.key)).toEqual(['ctx-size', 'cache-type-k', 'threads', 'parallel', 'temperature', 'top-p']);
    const t = rows.find(r => r.key === 'temperature');
    expect(t.selected).toBe(false);
    expect(t.source).toBe('sidecar');
    expect(t.evidence).toContain('generation_config.json');
    expect(rows.find(r => r.key === 'top-p').selected).toBe(true);
    expect(win.AT.recRows(DONE, { temperature: '0.8' }, meta, true).find(r => r.key === 'temperature').selected).toBe(true);
  });

  it('renders the recommendation pane and toggles rows', async () => {
    const win = await opened();
    await finished(win);
    expect(win.document.getElementById('atPaneDone').style.display).toBe('');
    const rows = win.document.querySelectorAll('#atRecRows tr');
    expect(rows.length).toBe(7);                                  // 4 measured + temperature (by hand) + top-p + min-p (both blank)
    expect(win.document.getElementById('atRecBig').textContent).toContain('+41 %');
    expect(win.document.getElementById('atApplyBtn').textContent).toBe('Apply 5 changes + restart');
    expect(rows[2].classList.contains('skip')).toBe(true);
    win.AT.toggleRow(2);
    expect(win.document.getElementById('atApplyBtn').textContent).toBe('Apply 6 changes + restart');
    expect(win.document.getElementById('atGuard').textContent).toContain('KL 0.006');
    expect(win.document.getElementById('atCmp').textContent).toContain('142.6');
    expect(win.AT.argsText(win.AT.rows().filter(r => r.selected))).toContain('--ctx-size 65536 --cache-type-k q8_0 --threads 12 --parallel 4');
  });

  it('applies in order and stops at the failing step', async () => {
    const win = await opened();
    await finished(win);
    win.__fetches.length = 0;
    const r = await win.AT.apply();
    expect(r.ok).toBe(true);
    const calls = win.__fetches.map(f => [f[0], (f[1] || {}).method || 'GET']);
    expect(calls[0]).toEqual(['/api/llm/config', 'GET']);
    expect(calls[1][0]).toBe('/api/llm/profiles/org%2Fm%3AQ4/save');
    expect(JSON.parse(win.__fetches[1][1].body).name).toMatch(/^before tune \d{4}-\d{2}-\d{2}$/);
    expect(calls[2]).toEqual(['/api/llm/config', 'POST']);
    const cfg = JSON.parse(win.__fetches[2][1].body);
    expect(cfg['org/m:Q4']['ctx-size']).toBe('65536');
    expect(cfg['org/m:Q4'].threads).toBe('32');                   // deselected row untouched
    expect(cfg.__DEFAULTS__).toBeUndefined();
    expect(calls[3]).toEqual(['/api/llm/server/restart', 'POST']);
    // the active profile is re-synced after the config write and before the restart
    expect(win.__syncCalls.length).toBe(1);
    expect(win.__syncCalls[0][0]).toBe('org/m:Q4');
    expect(win.__syncCalls[0][1]['ctx-size']).toBe('65536');
    expect(win.__syncCalls[0][1].threads).toBe('32');
    expect(win.__syncCalls[0][2]).toBe(3);
    // failure path: the config write fails → no restart, no profile sync, message names the step
    win.__failConfig = true;
    win.__fetches.length = 0;
    win.__syncCalls.length = 0;
    const r2 = await win.AT.apply();
    expect(r2.ok).toBe(false);
    expect(r2.step).toBe('write config');
    expect(win.__fetches.some(f => f[0] === '/api/llm/server/restart')).toBe(false);
    expect(win.document.getElementById('atRecMsg').textContent).toContain('write config');
    expect(win.__syncCalls.length).toBe(0);
  });

  it('skips the restart when the toggle is off', async () => {
    const win = await opened();
    win.document.getElementById('atRestartAfter').classList.remove('on');
    await finished(win);
    expect(win.document.getElementById('atApplyBtn').textContent).toBe('Apply 5 changes');
    win.__fetches.length = 0;
    await win.AT.apply();
    expect(win.__fetches.some(f => f[0] === '/api/llm/server/restart')).toBe(false);
    // the rail toggles re-render the footer, so the Apply label cannot lie about the restart
    win.document.getElementById('atRestartAfter').click();
    expect(win.document.getElementById('atApplyBtn').textContent).toBe('Apply 5 changes + restart');
    win.document.getElementById('atRestartAfter').click();
    expect(win.document.getElementById('atApplyBtn').textContent).toBe('Apply 5 changes');
  });
});
