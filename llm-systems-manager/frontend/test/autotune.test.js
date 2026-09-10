// #880: Autotune module — dims state, plan card, estimate, stream plumbing, stepper.
import { describe, it, expect, vi } from 'vitest';
import { srcFile, runHarness, flush, QUEUE_SLOT_STUB } from './helpers/harness.js';

const INDEX = srcFile('index.html');
// The real module markup, so ids and classes cannot drift from index.html.
const BODY = INDEX.slice(INDEX.indexOf('<div id="toolsModAt"'), INDEX.indexOf('<!-- /toolsModAt -->'));

const PRE = {
  ok: true, busy: false, unit_active: false, cores: { physical: 16, logical: 32 }, perplexity: true,
  runtime: { ok: true }, drafts: [{ repo: 'unsloth/Qwen3-0.6B-GGUF', file: 'Qwen3-0.6B-Q8_0.gguf', path: '/h/a.gguf', size: 7e8 }],
  sizes: { 'org/big:Q4': 90e9, 'org/m:Q4': 18e9 }, vram_total_mb: 32768, ram_total_mb: 32768,
  drafts_for: { 'org/m:Q4': null, 'org/big:Q4': { repo: 'unsloth/Qwen3-0.6B-GGUF', file: 'Qwen3-0.6B-Q8_0.gguf', size: 7e8 } },
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
  window.openAgentSse = async () => { const s = { close() { s.closed = true; } }; window.__dl = s; return s; };
  window.__alerts = [];
  window.alert = function (m) { window.__alerts.push(String(m)); };
  window.__fetches = [];
  window.__syncCalls = [];
  window._syncActiveProfile = function (mid, values) { window.__syncCalls.push([mid, values, window.__fetches.length]); return Promise.resolve(); };
  window.__pre = ${JSON.stringify(PRE)};
  window.toolsOpenTool = function (id, m, o) { window.__opened = [id, m, o]; };
  window._recordToolRun = function (tool, data) {
    fetch('/api/tools/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tool, ...data }) }).catch(() => {});
  };
  window.fetch = function (url, opts) {
    window.__fetches.push([String(url), opts]);
    const u = String(url);
    const body = u.startsWith('/api/llm/autotune/preflight') ? window.__pre
      : u.startsWith('/api/llm/autotune/status') ? (window.__status || { ok: true, items: [] })
      : u.startsWith('/api/benchmark/models') ? { models: ['org/m:Q4', 'org/big:Q4'] }
      : u.startsWith('/api/tools/runs') ? { runs: [{ tool: 'autotune', model_id: 'org/m:Q4', ok: true, ts: '2026-08-28T10:00:00Z', summary: { objective: 'fit', ctx_size: 32768, n_expert: 128 } }], latest: {} }
      : u.startsWith('/api/llama-state') ? { state: window.__llamaState || 'stopped' }
      : u.startsWith('/api/llm/config') && !(opts && opts.method) ? { __DEFAULTS__: {}, 'org/m:Q4': { 'ctx-size': '32768', threads: '32', temperature: '0.8' } }
      : u.startsWith('/api/llm/model-meta') ? { repo: 'org/m', base_model: 'org/base', suggestions: [{ key: 'temperature', value: 0.7, source: 'sidecar' }, { key: 'top-p', value: 0.8, source: 'model_card' }, { key: 'min-p', value: 0, source: 'base_model' }] }
      : u.startsWith('/api/llm/autotune/run') ? (window.__runReply || { ok: true, run_id: 'r1' })
      : (u === '/api/llm/config' && opts && opts.method === 'POST') ? (window.__failConfig ? { ok: false, error: 'boom' } : { ok: true })
      : u.startsWith('/api/energy/host-peak') ? (window.__peak || { ok: true, peak_w: 312.4, hours: 21, peak_active_w: 300.2, active_hours: 12 })
      : u.startsWith('/api/llm/draft-candidates') ? (window.__draft || { ok: true, candidate: { repo: 'unsloth/Qwen3-0.6B-GGUF', file: 'Qwen3-0.6B-Q4_K_M.gguf', size_bytes: 420e6, params_b: 0.6 }, reason: 'smallest instruct GGUF at ≤ 25 % of the target' })
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

// Same module with the shared run gate wired in (#888).
async function openedGated(bootstrap = '') {
  const win = runHarness({ sources: [LAYOUT, STUBS, QUEUE_SLOT_STUB, srcFile('js/autotune.js')],
                           bodyHtml: BODY, bootstrap });
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
    expect(list.querySelectorAll('.mc-toggle[data-model]').length).toBe(2);
    expect(list.textContent).toContain('MoE · 128 experts');
    expect(list.textContent).toContain('no fit');
    expect(win.document.getElementById('atDraftSel').options.length).toBe(2);   // the cached Qwen draft is another family
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
    win.document.querySelector('#atModelList .mc-toggle[data-model="org/big:Q4"]').click();
    win.document.querySelector('#atModelList .mc-toggle[data-model="org/m:Q4"]').click();
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

  it('locks the rail while a run is active and unlocks when it finishes', async () => {
    const win = await opened();
    const rail = win.document.querySelector('#toolsModAt .at-rail');
    const kvChip = win.document.querySelector('#atKvChips .bl-chip.on');
    const wasOn = kvChip.classList.contains('on');
    await win.AT.run();
    await flush();
    expect(rail.classList.contains('locked')).toBe(true);
    expect(win.document.getElementById('atBudgetMin').disabled).toBe(true);
    kvChip.click();
    expect(kvChip.classList.contains('on')).toBe(wasOn);        // click swallowed while locked
    expect(win.document.getElementById('atCancelBtn').disabled).toBe(false);
    win.document.getElementById('atCancelBtn').click();          // still clickable while locked
    expect(win.__fetches.some(f => f[0] === '/api/llm/autotune/cancel')).toBe(true);
    win.__sse.onEvent({ type: 'done', ok: true, model_id: 'org/m:Q4' }, {});
    expect(rail.classList.contains('locked')).toBe(false);
    expect(win.document.getElementById('atBudgetMin').disabled).toBe(false);
    kvChip.click();
    expect(kvChip.classList.contains('on')).toBe(!wasOn);
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

describe('AT export, regression warning, recRows numeric guard', () => {
  it('downloads the report as a JSON file instead of opening a blocked tab', async () => {
    const win = await opened();
    await finished(win);
    win.URL.createObjectURL = () => 'blob:mock';
    win.URL.revokeObjectURL = () => {};
    const clickSpy = vi.spyOn(win.HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    win.AT.exportReport();
    expect(clickSpy).toHaveBeenCalledTimes(1);
    const a = win.document.body.querySelector('a[download]');
    expect(a).toBeTruthy();
    expect(a.download).toMatch(/^autotune-.*\.json$/);
    clickSpy.mockRestore();
  });

  it('flags a regression when the recommended set is slower than the current config', async () => {
    const win = await opened();
    const SLOW_DONE = { ...DONE, before: { ...DONE.before, decode_tps: 100 }, after: { ...DONE.after, decode_tps: 55 } };
    await win.AT.run();
    for (let i = 0; i < 4; i++) await flush();
    win.__sse.onEvent({ type: 'model_start', model_id: 'org/m:Q4', objective: 'balanced', stages: ['context', 'kv', 'moe', 'verify'] }, {});
    win.__sse.onEvent(SLOW_DONE, {});
    win.__sse.onEvent({ type: 'done', ok: true }, {});
    await flush();
    const big = win.document.getElementById('atRecBig');
    expect(big.innerHTML).toContain('class="neg"');
    expect(big.textContent).toContain('-45 %');
    const warn = win.document.getElementById('atRecWarn');
    expect(warn.style.display).toBe('');
    expect(warn.textContent).toContain('Slower than your current config');
    expect(warn.textContent).toContain('45 % decode');
  });

  it('hides the regression warning for a normal (faster) run', async () => {
    const win = await opened();
    await finished(win);
    const warn = win.document.getElementById('atRecWarn');
    expect(warn.style.display).toBe('none');
  });

  it('recRows skips a sampling suggestion that already matches numerically', () => {
    const win = boot();
    const meta = { repo: 'org/m', base_model: 'org/base', suggestions: [{ key: 'temperature', value: 0, source: 'sidecar' }, { key: 'top-p', value: 1, source: 'model_card' }] };
    const rows = win.AT.recRows(DONE, { temperature: '0.00', 'top-p': '1.0' }, meta, false);
    expect(rows.map(r => r.key)).not.toContain('temperature');
    expect(rows.map(r => r.key)).not.toContain('top-p');
  });

  it('shows an amber verify warning without treating verify as failed', async () => {
    const win = await opened();
    const WARN_DONE = { ...DONE, verify: { ...DONE.verify, ok: true, warning: 'free VRAM 925 MB is below the 1024 ± 50 MB target' } };
    await win.AT.run();
    for (let i = 0; i < 4; i++) await flush();
    win.__sse.onEvent({ type: 'model_start', model_id: 'org/m:Q4', objective: 'balanced', stages: ['context', 'kv', 'moe', 'verify'] }, {});
    win.__sse.onEvent(WARN_DONE, {});
    win.__sse.onEvent({ type: 'done', ok: true }, {});
    await flush();
    const items = win.document.querySelectorAll('#atGuard .g');
    const verifyItem = items[1];
    expect(verifyItem.querySelector('.at-dot').classList.contains('warn')).toBe(true);
    expect(verifyItem.textContent).toContain('below the');
    const dv = win.document.getElementById('atDoneVerify');
    expect(dv.textContent).toContain('below the');
    expect(dv.innerHTML).toContain('class="warn"');
  });
});

describe('re-verify (#887)', () => {
  it('sends every recorded measurement as the baseline, mapped to the before-column keys', async () => {
    const win = await opened();
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b100', current_build: 'b120', stale: true,
      summary: { decode_tps: 41.5, prefill_tps: 400, agg_tps: 48, ctx_size: 125440, free_mb: 925, gain_pct: null } }] };
    await win.AT.onOpen('org/m:Q4', { verify: true }); await flush();
    const post = win.__fetches.find(([u, o]) => u === '/api/llm/autotune/run' && o && o.method === 'POST');
    expect(JSON.parse(post[1].body).baseline).toEqual({ decode_tps: 41.5, prefill_tps: 400, agg_tps: 48, ctx: 125440, free_mb: 925 });
  });
  it('hides the empty parameter table and explains a missing baseline on a verify', async () => {
    const win = await opened();
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b100', current_build: 'b120', stale: true, summary: { ctx_size: 125440 } }] };
    await win.AT.onOpen('org/m:Q4', { verify: true }); await flush();
    win.AT.onEvent({ type: 'model_start', model_id: 'org/m:Q4', objective: 'fit', mode: 'verify', stages: ['verify'] });
    win.AT.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'verify', llama_build: 'b120', regressed: null,
      before: {}, after: { decode_tps: 63.8, ctx: 125440 }, verify: { ok: true, seconds: 64 }, changes: [], stages: [{ stage: 'verify', status: 'done' }] });
    win.AT.onEvent({ type: 'done', ok: true }); await flush();
    const table = win.document.getElementById('atRecRows').closest('.at-card-b');
    expect(table.style.display).toBe('none');
    expect(win.document.getElementById('atRecSel').textContent).toMatch(/nothing to apply/);
    expect(win.document.getElementById('atCmp').textContent).toMatch(/becomes the baseline/);
  });
  it('only focuses the Re-verify button when llama-server still holds the host', async () => {
    const win = await opened();
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b100', current_build: 'b120', stale: true, summary: {} }] };
    win.__llamaState = 'awake';
    await win.AT.onOpen('org/m:Q4', { verify: true }); await flush();
    expect(win.__fetches.filter(([u, o]) => u === '/api/llm/autotune/run' && o && o.method === 'POST')).toHaveLength(0);
    expect(win.document.getElementById('atRunBtn').disabled).toBe(true);
  });
  it('keeps the newest row when two agents tuned the same model', async () => {
    const win = await opened();
    win.__status = { ok: true, items: [
      { agent_id: 'newer', model_id: 'org/m:Q4', llama_build: 'b120', current_build: 'b120', stale: false, ts: '2026-09-05T00:00:00Z', summary: {} },
      { agent_id: 'older', model_id: 'org/m:Q4', llama_build: 'b90', current_build: 'b130', stale: true, ts: '2026-08-01T00:00:00Z', summary: {} },
    ] };
    await win.AT.onOpen('org/m:Q4'); await flush();
    expect(win.document.getElementById('atVerifyBtn').style.display).toBe('none');
    expect(win.document.getElementById('atPrevTune').textContent).toContain('b120');
  });
  it('says so when the tune status could not be loaded', async () => {
    const win = await opened();
    const realFetch = win.fetch;
    win.fetch = (u, o) => (String(u).startsWith('/api/llm/autotune/status') ? Promise.reject(new Error('down')) : realFetch(u, o));
    await win.AT.onOpen('org/m:Q4'); await flush();
    expect(win.document.getElementById('atPrevTune').textContent).toMatch(/could not be loaded/);
  });
  it('opening with {verify:true} on a stale model reveals the Re-verify button and posts a verify-mode body', async () => {
    const win = await opened();     // the file's helper that boots + awaits AT.onOpen('org/m:Q4')
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b100', current_build: 'b120', stale: true, summary: { decode_tps: 41.5 } }] };
    await win.AT.onOpen('org/m:Q4', { verify: true }); await flush();
    const btn = win.document.getElementById('atVerifyBtn');
    expect(btn.style.display).not.toBe('none');
    expect(win.document.getElementById('atPrevTune').textContent).toContain('stale');
    // The deep link ran the check itself; no click needed.
    const posts = win.__fetches.filter(([u, o]) => u === '/api/llm/autotune/run' && o && o.method === 'POST');
    expect(posts).toHaveLength(1);
    const post = posts[0];
    const body = JSON.parse(post[1].body);
    expect(body.mode).toBe('verify');
    expect(body.model_ids).toEqual(['org/m:Q4']);
    expect(body.baseline_tps).toBe(41.5);
    expect(body.baseline).toEqual({ decode_tps: 41.5 });
    expect(body.budget_min).toBe(15);
  });
  it('model_done in verify mode records mode + build in the ledger and shows the regression headline', async () => {
    const win = await opened();
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b100', current_build: 'b120', stale: true, summary: { decode_tps: 41.5 } }] };
    await win.AT.onOpen('org/m:Q4', { verify: true }); await flush();
    win.AT.onEvent({ type: 'model_start', model_id: 'org/m:Q4', objective: 'balanced', mode: 'verify', stages: ['verify'] });
    win.AT.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'verify', llama_build: 'b120', regressed: true,
      before: { decode_tps: 41.5 }, after: { decode_tps: 30.1, ctx: 8192 }, verify: { ok: true, seconds: 60 }, changes: [], stages: [{ stage: 'verify', status: 'done' }] });
    win.AT.onEvent({ type: 'done', ok: true });
    await flush();
    const rec = win.__fetches.filter(([u, o]) => u === '/api/tools/runs' && o && o.method === 'POST').map(([, o]) => JSON.parse(o.body)).pop();
    expect(rec.mode).toBe('verify'); expect(rec.llama_build).toBe('b120');
    expect(win.document.getElementById('atRecWarn').textContent).toMatch(/slower.*re-tune/i);
    expect(win.document.getElementById('atRetuneBtn').style.display).not.toBe('none');
  });
  it('Check quality opens the Quality guard tool with the recommended overrides', async () => {
    const win = await opened();
    win.AT.onEvent({ type: 'model_start', model_id: 'org/m:Q4', objective: 'fit', stages: ['context', 'verify'] });
    win.AT.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'tune', before: { decode_tps: 40 }, after: { decode_tps: 44, ctx: 16384 }, verify: { ok: true },
      changes: [{ key: 'cache-type-k', current: 'f16', recommended: 'q8_0', source: 'measured', selected: true },
                { key: 'ctx-size', current: '8192', recommended: '16384', source: 'measured', selected: true }], stages: [] });
    win.AT.onEvent({ type: 'done', ok: true }); await flush();
    win.AT.checkQuality();
    expect(win.__opened).toEqual(['quality', 'org/m:Q4', { overrides: { 'cache-type-k': 'q8_0' } }]);
  });
  it('Check quality sends only the selected rows and skips a null recommendation', async () => {
    const win = await opened();
    win.AT.onEvent({ type: 'model_start', model_id: 'org/m:Q4', objective: 'fit', stages: ['context', 'verify'] });
    win.AT.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'tune', before: {}, after: { ctx: 16384 }, verify: { ok: true },
      changes: [{ key: 'cache-type-k', current: 'f16', recommended: 'q8_0', source: 'measured', selected: true },
                { key: 'cache-type-v', current: 'f16', recommended: 'q4_0', source: 'measured', selected: false },
                { key: 'threads', current: '32', recommended: null, source: 'measured', selected: true }], stages: [] });
    win.AT.onEvent({ type: 'done', ok: true }); await flush();
    win.AT.checkQuality();
    expect(win.__opened[2]).toEqual({ overrides: { 'cache-type-k': 'q8_0' } });
  });
  it('a quality-mode model_done on the shared stream is ignored by Autotune', async () => {
    const win = await opened();
    await win.AT.run(); for (let i = 0; i < 4; i++) await flush();
    win.__sse.onEvent({ type: 'model_start', model_id: 'org/m:Q4', mode: 'quality', objective: 'quality', stages: ['quality'] }, {});
    win.__sse.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'quality', run_id: 'q1',
      guard: { kl: 0.006, pass: true }, changes: [{ key: 'cache-type-k', current: 'f16', recommended: 'q8_0' }], stages: [] }, {});
    await flush();
    const tools = win.__fetches.filter(([u, o]) => u === '/api/tools/runs' && o && o.method === 'POST').map(([, o]) => JSON.parse(o.body));
    expect(tools.some(t => t.tool === 'autotune')).toBe(false);
    win.AT.checkQuality();
    expect(win.__opened).toBeUndefined();
  });
  it('a failed verify repaints the Done panel instead of keeping the previous tune', async () => {
    const win = await opened();
    await finished(win);                                  // a full tune leaves green complete + guard cards
    expect(win.document.getElementById('atDonePill').textContent).toBe('complete');
    expect(win.document.getElementById('atQualityBtn').style.display).toBe('');
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b100', current_build: 'b120', stale: true, summary: { decode_tps: 41.5 } }] };
    win.AT.onEvent({ type: 'model_start', model_id: 'org/m:Q4', mode: 'verify', objective: 'balanced', stages: ['verify'] });
    win.AT.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: false, mode: 'verify', llama_build: 'b120', regressed: null,
      before: { decode_tps: 41.5 }, after: { ctx: 8192, concurrency: 1 }, guard: null,
      verify: { ok: false, seconds: 0, reason: 'OOM under load', dropped: [] }, changes: [], stages: [] });
    win.AT.onEvent({ type: 'done', ok: false }); await flush();
    const pill = win.document.getElementById('atDonePill');
    expect(pill.textContent).toBe('stopped');
    expect(pill.classList.contains('ok')).toBe(false);
    expect(win.document.getElementById('atDoneVerify').textContent).toContain('OOM under load');
    expect(win.document.getElementById('atQualityBtn').style.display).toBe('none');
    expect(win.document.getElementById('atGuard').style.display).toBe('none');
    expect(win.document.getElementById('atGuard').innerHTML).toBe('');
    expect(win.document.getElementById('atCmp').textContent).not.toContain('142.6');
    expect(win.document.getElementById('atRecWarn').textContent).toContain('Verify did not complete');
  });
  it('a regressed verify reads as regressed, not complete', async () => {
    const win = await opened();
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b100', current_build: 'b120', stale: true, summary: { decode_tps: 41.5 } }] };
    win.AT.onEvent({ type: 'model_start', model_id: 'org/m:Q4', mode: 'verify', objective: 'balanced', stages: ['verify'] });
    win.AT.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'verify', llama_build: 'b120', regressed: true,
      before: { decode_tps: 41.5 }, after: { decode_tps: 30.1, ctx: 8192 }, verify: { ok: true, seconds: 60 }, changes: [], stages: [] });
    win.AT.onEvent({ type: 'done', ok: true }); await flush();
    const pill = win.document.getElementById('atDonePill');
    expect(pill.textContent).toBe('regressed');
    expect(pill.classList.contains('ok')).toBe(false);
    expect(win.document.getElementById('atDoneVerify').textContent).toContain('current config');
  });
  it('the previous-tune line keeps its objective text and gains the build sentence', async () => {
    const win = await opened();
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b100', current_build: 'b120', stale: true, ts: '2026-09-02T00:00:00Z', summary: { decode_tps: 41.5 } }] };
    await win.AT.onOpen('org/m:Q4'); await flush();
    const prev = win.document.getElementById('atPrevTune');
    expect(prev.textContent).toContain('2026-08-28');           // run-history line from syncModels
    expect(prev.textContent).toContain('ctx 32,768');
    expect(prev.textContent).toContain('Tune is stale.');
    expect(prev.querySelectorAll('[data-at-build]').length).toBe(1);
  });
  it('an unknown-build status row still offers Re-verify', async () => {
    const win = await opened();
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: null, current_build: null, stale: null, ts: '2026-09-02T00:00:00Z', summary: {} }] };
    await win.AT.onOpen('org/m:Q4'); await flush();
    expect(win.document.getElementById('atVerifyBtn').style.display).not.toBe('none');
  });
  it('an unknown build says why re-verifying helps instead of printing "?" or repeating the date', async () => {
    const win = await opened();
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: null, current_build: null, stale: null, ts: '2026-09-02T00:00:00Z', summary: {} }] };
    await win.AT.onOpen('org/m:Q4'); await flush();
    const note = win.document.querySelector('#atPrevTune [data-at-build]');
    expect(note.textContent).toMatch(/predates build recording/);
    expect(note.textContent).not.toMatch(/\?/);
    expect(note.textContent).not.toContain('2026-09-02');
    expect(note.textContent).not.toMatch(/Last tune/);
  });
  it('a current-build tune names the build once, without the date', async () => {
    const win = await opened();
    win.__status = { ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b120', current_build: 'b120', stale: false, ts: '2026-09-02T00:00:00Z', summary: {} }] };
    await win.AT.onOpen('org/m:Q4'); await flush();
    const note = win.document.querySelector('#atPrevTune [data-at-build]');
    expect(note.textContent).toContain('b120');
    expect(note.textContent).not.toContain('2026-09-02');
  });
  it('detach closes the stream without cancelling the run', async () => {
    const win = await opened();
    await win.AT.run(); for (let i = 0; i < 4; i++) await flush();
    expect(win.AT.running()).toBe(true);
    win.AT.detach();
    expect(win.__closed).toBe(true);
    expect(win.AT.running()).toBe(false);
    expect(win.__fetches.some(([u]) => u === '/api/llm/autotune/cancel')).toBe(false);
  });
});


// #888: the shared run gate — Run becomes Queue while another tool holds the host.
describe('AT queueing behind another tool (#888)', () => {
  const BUSY = "window.__gateBusy = { tool: 'reportcard', label: 'Report Card', host: 'gpu-01', agent_id: 'a1' };";
  const runPosts = (win) => win.__fetches.filter(
    f => f[0] === '/api/llm/autotune/run' && f[1] && f[1].method === 'POST');

  it('queues instead of starting while a Report Card holds the host', async () => {
    const win = await openedGated(BUSY);
    win.__fetches.length = 0;
    await win.AT.run();
    await flush();
    expect(runPosts(win)).toHaveLength(0);
    expect(win.__slots[0].queued()).toBe(true);
    expect(win.document.getElementById('atRunBtn').textContent).toContain('Queued');
    expect(win.document.getElementById('atQueueNote').textContent)
      .toContain('Queued behind Report Card on gpu-01');
    expect(win.__queued).toEqual(['autotune', 'Report Card on gpu-01']);
  });

  it('starts the queued run by itself once the gate clears', async () => {
    const win = await openedGated(BUSY);
    await win.AT.run();
    await flush();
    win.__fetches.length = 0;
    win.__gateBusy = null;
    await win.__slots[0].fire();
    for (let i = 0; i < 4; i++) await flush();
    expect(runPosts(win)).toHaveLength(1);
    expect(JSON.parse(runPosts(win)[0][1].body).model_ids).toEqual(['org/m:Q4']);
  });

  it('drops a queued run on Cancel without cancelling anything on the agent', async () => {
    const win = await openedGated(BUSY);
    await win.AT.run();
    await flush();
    win.__fetches.length = 0;
    win.AT.cancel();
    expect(win.__slots[0].queued()).toBe(false);
    expect(win.__fetches.map(f => f[0])).not.toContain('/api/llm/autotune/cancel');
    expect(win.__queued).toBe(null);
  });

  it('labels the button Queue while the host is busy and nothing is pending', async () => {
    const win = await openedGated(BUSY);
    expect(win.document.getElementById('atRunBtn').textContent).toContain('Queue autotune');
    expect(win.document.getElementById('atQueueNote').textContent)
      .toContain('Report Card is running on gpu-01');
  });

  it('queues rather than losing the run when the agent refuses it', async () => {
    const win = await openedGated();
    win.__runReply = { ok: false, error: 'an autotune run is already in progress' };
    await win.AT.run();
    for (let i = 0; i < 4; i++) await flush();
    expect(win.__slots[0].queued()).toBe(true);
    expect(win.__slots[0].waitFor()).toBe('the run in progress');
    expect(win.__alerts).toEqual([]);
  });
});

describe('Quiet objective (#890)', () => {
  it('reveals a cap prefilled at 90 % of the busiest hour under load and remembers it', async () => {
    const win = await opened();
    win.AT.setObjective('quiet'); await flush();
    const row = win.document.getElementById('atCapRow'), cap = win.document.getElementById('atPowerCap');
    expect(row.style.display).not.toBe('none');
    expect(cap.value).toBe('270');
    expect(win.document.getElementById('atCapHint').textContent).toMatch(/drew 300 W under load.*12 h.*270 W/);
    cap.value = '200'; cap.dispatchEvent(new win.Event('input', { bubbles: true }));
    expect(win.__layout().atPowerCap).toBe(200);
    win.AT.setObjective('balanced');
    expect(row.style.display).toBe('none');
  });

  it('falls back to the hourly peak itself when no load was ever metered', async () => {
    const win = boot(); win.__peak = { ok: true, peak_w: 312.4, hours: 21, peak_active_w: null, active_hours: 0 };
    win.AT.onOpen('org/m:Q4'); for (let i = 0; i < 6; i++) await flush();
    win.AT.setObjective('quiet'); await flush();
    expect(win.document.getElementById('atPowerCap').value).toBe('312');
    expect(win.document.getElementById('atCapHint').textContent).toMatch(/no load-draw history.*312 W/);
  });

  it('asks for a cap when the host has no power history', async () => {
    const win = boot(); win.__peak = { ok: true, peak_w: null, hours: 0, peak_active_w: null, active_hours: 0 };
    win.AT.onOpen('org/m:Q4'); for (let i = 0; i < 6; i++) await flush();
    win.AT.setObjective('quiet'); await flush();
    expect(win.document.getElementById('atPowerCap').value).toBe('');
    expect(win.document.getElementById('atCapHint').textContent).toMatch(/no power history/i);
    await win.AT.run();
    expect(win.__alerts.pop()).toMatch(/power cap/i);
    expect(win.__fetches.some(([u, o]) => u === '/api/llm/autotune/run' && o && o.method === 'POST')).toBe(false);
  });

  it('switches the dims to the quiet set and posts the cap', async () => {
    const win = await opened();
    win.AT.setObjective('quiet'); await flush();
    const on = d => win.document.querySelector(`.at-dim[data-dim="${d}"] [data-dim-on]`).classList.contains('on');
    expect(on('kv')).toBe(false); expect(on('spec')).toBe(false); expect(on('sampling')).toBe(false);
    expect(on('threads')).toBe(true); expect(on('slots')).toBe(true);
    expect([...win.document.querySelectorAll('#atSlotChips .bl-chip.on')].map(c => c.dataset.v)).toEqual(['1', '2', '4']);
    await win.AT.run(); for (let i = 0; i < 4; i++) await flush();
    const post = win.__fetches.find(([u, o]) => u === '/api/llm/autotune/run' && o && o.method === 'POST');
    const body = JSON.parse(post[1].body);
    expect(body.objective).toBe('quiet'); expect(body.power_cap_w).toBe(270);
    expect(body.dims.kv.on).toBe(false); expect(body.dims.slots.candidates).toEqual([1, 2, 4]);
    expect(win.document.getElementById('atObjHint').textContent).toMatch(/watt/i);
  });

  it('shows watts on the stage bars, marks over-cap candidates, and reads the draw on the done pane', async () => {
    const win = await opened();
    win.AT.setObjective('quiet'); await flush();
    await win.AT.run(); for (let i = 0; i < 4; i++) await flush();
    win.__sse.onEvent({ type: 'model_start', model_id: 'org/m:Q4', objective: 'quiet', stages: ['context', 'threads', 'verify'] }, {});
    win.__sse.onEvent({ type: 'stage_start', model_id: 'org/m:Q4', stage: 'threads', candidates: [8, 16], est_s: 60 }, {});
    win.__sse.onEvent({ type: 'candidate_result', model_id: 'org/m:Q4', stage: 'threads', value: 8, ok: true, decode_tps: 90, avg_w: 180 }, {});
    win.__sse.onEvent({ type: 'candidate_result', model_id: 'org/m:Q4', stage: 'threads', value: 16, ok: true, decode_tps: 101, avg_w: 310 }, {});
    const bars = win.document.querySelectorAll('#atStageBody .at-bar');
    expect(bars[0].textContent).toContain('180 W'); expect(bars[0].classList.contains('over')).toBe(false);
    expect(bars[1].classList.contains('over')).toBe(true);
    expect(win.document.getElementById('atStageMeta').textContent).toContain('cap 270 W');
    win.__sse.onEvent({ ...DONE, objective: 'quiet', power_cap_w: 250, over_cap: false, after: { ...DONE.after, avg_w: 212.5 } }, {});
    win.__sse.onEvent({ type: 'done', ok: true }, {});
    await flush();
    expect(win.document.getElementById('atRecBig').textContent).toMatch(/213 W under the 250 W cap/);
  });

  it('plans the MoE stage as four measured offload counts under Quiet', async () => {
    const win = await opened();
    const dims = win.AT.dimsState(), facts = { n_expert: 128, n_layer: 48 };
    const moe = o => win.AT.planRows(o, dims, win.AT.state().pre, facts).find(r => r.stage === 'moe');
    expect(moe('quiet').desc).toBe('measure 4 offload counts for draw, fastest under the cap');
    expect(moe('quiet').est_s).toBe(4 * moe('balanced').est_s);
    const noRt = win.AT.planRows('quiet', dims, { runtime: { ok: false } }, facts).find(r => r.stage === 'moe');
    expect(noRt.desc).toMatch(/install the bench runtime/);
    expect(noRt.est_s).toBe(0);
  });

  it('sends the cap on a verify run too, since the agent requires it', async () => {
    const win = await opened();
    win.AT.setObjective('quiet'); await flush();
    await win.AT.verify(); for (let i = 0; i < 4; i++) await flush();
    const post = win.__fetches.find(([u, o]) => u === '/api/llm/autotune/run' && o && o.method === 'POST');
    expect(JSON.parse(post[1].body).power_cap_w).toBe(270);
  });

  it('surfaces the agent refusal text for an agent that predates Quiet', async () => {
    const win = await opened();
    win.AT.setObjective('quiet'); await flush();
    win.__runReply = { ok: false, detail: 'objective must be one of fit, speed, balanced, serve' };
    await win.AT.run(); for (let i = 0; i < 4; i++) await flush();
    expect(win.__alerts.pop()).toMatch(/objective must be one of/);
  });
});

describe('draft discovery (#889)', () => {
  it('needs no draft for a model whose last tune recorded a NextN head', async () => {
    const win = boot();
    const realFetch = win.fetch;
    win.fetch = (u, o) => (String(u).startsWith('/api/tools/runs')
      ? Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ runs: [{ tool: 'autotune', model_id: 'org/m:Q4', ok: true, ts: '2026-09-10T10:00:00Z', summary: { objective: 'fit', n_expert: 0, mtp_layers: 1 } }], latest: {} }) })
      : realFetch(u, o));
    win.AT.onOpen('org/m:Q4'); for (let i = 0; i < 6; i++) await flush();
    expect(win.document.getElementById('atDraftRow').style.display).toBe('none');
    expect(win.__fetches.some(([u]) => String(u).startsWith('/api/llm/draft-candidates'))).toBe(false);
    expect(win.document.querySelector('.at-dim[data-dim="spec"] [data-dim-sum]').textContent).toContain('NextN head, no draft needed');
  });
  it('lists only same-family drafts small enough to auto-detect, and hides the row on a manual pick', async () => {
    const win = boot();
    win.__pre = { ...PRE, drafts: [...PRE.drafts,
      { repo: 'org/m-0.6B-GGUF', file: 'm-0.6B-Q8_0.gguf', path: '/h/s.gguf', size: 7e8 },
      { repo: 'org/m-9B-GGUF', file: 'm-9B-Q4_K_M.gguf', path: '/h/big.gguf', size: 6e9 },
      { repo: 'org/m-0.6B-GGUF', file: 'm-0.6B-dflash.gguf', path: '/h/df.gguf', size: 3e8 }] };
    win.AT.onOpen('org/m:Q4'); for (let i = 0; i < 6; i++) await flush();
    const sel = win.document.getElementById('atDraftSel');
    expect([...sel.options].map(o => o.value)).toEqual(['auto', 'none', '/h/s.gguf']);
    expect(win.document.getElementById('atDraftRow').style.display).not.toBe('none');
    sel.value = '/h/s.gguf'; sel.dispatchEvent(new win.Event('change', { bubbles: true })); await flush();
    expect(win.document.getElementById('atDraftRow').style.display).toBe('none');
    sel.value = 'auto'; sel.dispatchEvent(new win.Event('change', { bubbles: true })); await flush();
    expect(win.document.getElementById('atDraftRow').style.display).not.toBe('none');
  });
  it('offers the Hugging Face candidate when the model has no draft on disk', async () => {
    const win = await opened();
    const row = win.document.getElementById('atDraftRow');
    expect(row.style.display).not.toBe('none');
    expect(win.document.getElementById('atDraftNote').textContent).toMatch(/no draft on disk.*Qwen3-0\.6B-Q4_K_M\.gguf.*0\.4 GB/);
    expect(win.document.getElementById('atDraftDlBtn').style.display).not.toBe('none');
    const spec = [...win.document.querySelectorAll('#atPlanRows .at-plan-r')].find(r => r.textContent.includes('Speculative'));
    expect(spec.textContent).toMatch(/no draft yet/);
  });
  it('stays quiet when a draft is already cached or the model has a NextN head', async () => {
    const win = await opened();
    win.document.querySelector('#atModelList .mc-toggle[data-model="org/big:Q4"]').click(); win.document.querySelector('#atModelList .mc-toggle[data-model="org/m:Q4"]').click();
    await flush();
    expect(win.document.getElementById('atDraftRow').style.display).toBe('none');
    win.document.querySelector('#atModelList .mc-toggle[data-model="org/m:Q4"]').click(); win.document.querySelector('#atModelList .mc-toggle[data-model="org/big:Q4"]').click();
    win.AT.onEvent({ type: 'facts', model_id: 'org/m:Q4', n_expert: 0, n_layer: 32, mtp_layers: 1 }); await flush();
    expect(win.document.getElementById('atDraftRow').style.display).toBe('none');
  });
  it('asks only for drafts the agent would accept — an eighth of the target size', async () => {
    const win = await opened();
    const url = win.__fetches.map(([u]) => String(u)).find(u => u.startsWith('/api/llm/draft-candidates'));
    expect(url).toContain('max_bytes=' + Math.floor(PRE.sizes['org/m:Q4'] / 8));
  });
  it('explains when Hugging Face has nothing suitable', async () => {
    const win = boot(); win.__draft = { ok: true, candidate: null, reason: 'no smaller GGUF of the qwen3 family on Hugging Face' };
    win.AT.onOpen('org/m:Q4'); for (let i = 0; i < 6; i++) await flush();
    expect(win.document.getElementById('atDraftNote').textContent).toMatch(/no smaller GGUF/);
    expect(win.document.getElementById('atDraftDlBtn').style.display).toBe('none');
  });
  it('says it is looking while the lookup is in flight, never that it failed', async () => {
    const win = boot(); const real = win.fetch; let release;
    win.fetch = (u, o) => String(u).startsWith('/api/llm/draft-candidates')
      ? new Promise(r => { release = () => r({ ok: true, json: () => Promise.resolve({ ok: true, candidate: { repo: 'unsloth/Qwen3-0.6B-GGUF', file: 'Qwen3-0.6B-Q4_K_M.gguf', size_bytes: 420e6 } }) }); })
      : real(u, o);
    win.AT.onOpen('org/m:Q4'); for (let i = 0; i < 6; i++) await flush();
    expect(win.document.getElementById('atDraftNote').textContent).toMatch(/looking for one on Hugging Face/);
    expect(win.document.getElementById('atDraftDlBtn').style.display).toBe('none');
    release(); for (let i = 0; i < 4; i++) await flush();
    expect(win.document.getElementById('atDraftNote').textContent).toMatch(/Qwen3-0\.6B-Q4_K_M\.gguf/);
    expect(win.document.getElementById('atDraftDlBtn').style.display).not.toBe('none');
  });
  it('never renders or downloads one model\'s candidate under another', async () => {
    const win = boot(); const real = win.fetch; const pending = {};
    win.__pre = { ...PRE, drafts_for: { 'org/m:Q4': null, 'org/big:Q4': null } };
    win.fetch = (u, o) => {
      const s = String(u);
      if (!s.startsWith('/api/llm/draft-candidates')) return real(u, o);
      const mid = decodeURIComponent(s.split('model_id=')[1].split('&')[0]);
      return new Promise(r => { pending[mid] = () => r({ ok: true, json: () => Promise.resolve({ ok: true, candidate: { repo: `repo-for-${mid}`, file: `${mid}.gguf`, size_bytes: 1e9 } }) }); });
    };
    win.AT.onOpen('org/m:Q4'); for (let i = 0; i < 6; i++) await flush();
    win.document.querySelector('#atModelList .mc-toggle[data-model="org/big:Q4"]').click();
    win.document.querySelector('#atModelList .mc-toggle[data-model="org/m:Q4"]').click();
    await flush();
    pending['org/m:Q4'](); for (let i = 0; i < 4; i++) await flush();
    win.AT.onEvent({ type: 'facts', model_id: 'org/other', n_expert: 0 }); await flush();
    const note = win.document.getElementById('atDraftNote');
    expect(note.textContent).not.toContain('org/m:Q4.gguf');
    expect(note.textContent).toMatch(/looking for one on Hugging Face/);
    await win.AT.downloadDraft(); await flush();
    expect(win.__fetches.find(([u]) => u === '/api/llm/download')).toBeUndefined();
    pending['org/big:Q4'](); for (let i = 0; i < 4; i++) await flush();
    expect(note.textContent).toContain('org/big:Q4.gguf');
  });
  it('downloads with one click, streams progress into the row, and refreshes the draft list on completion', async () => {
    const win = await opened();
    await win.AT.downloadDraft(); await flush();
    const post = win.__fetches.find(([u, o]) => u === '/api/llm/download' && o && o.method === 'POST');
    expect(JSON.parse(post[1].body)).toEqual({ repo: 'unsloth/Qwen3-0.6B-GGUF', patterns: ['Qwen3-0.6B-Q4_K_M.gguf'] });
    expect(win.document.getElementById('atDraftDlBtn').disabled).toBe(true);
    win.__dl.onmessage({ data: JSON.stringify({ type: 'line', text: 'Qwen3-0.6B-Q4_K_M.gguf: 41%', progress: true }) });
    expect(win.document.getElementById('atDraftNote').textContent).toContain('41%');
    win.__pre = { ...PRE, drafts: [...PRE.drafts, { repo: 'org/m-0.6B-GGUF', file: 'm-0.6B-Q4_K_M.gguf', path: '/h/c.gguf', size: 420e6 }],
                  drafts_for: { ...PRE.drafts_for, 'org/m:Q4': { repo: 'unsloth/Qwen3-0.6B-GGUF', file: 'Qwen3-0.6B-Q4_K_M.gguf', size: 420e6 } } };
    win.__dl.onmessage({ data: JSON.stringify({ type: 'done', ok: true }) });
    for (let i = 0; i < 4; i++) await flush();
    expect(win.__dl.closed).toBe(true);
    expect(win.document.getElementById('atDraftRow').style.display).toBe('none');
    expect([...win.document.getElementById('atDraftSel').options].some(o => o.textContent.includes('m-0.6B-Q4_K_M.gguf'))).toBe(true);
  });
});
