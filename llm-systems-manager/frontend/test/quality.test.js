// #888: Quality guard module — standalone KL check of any config change against f16.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush, QUEUE_SLOT_STUB } from './helpers/harness.js';

const INDEX = srcFile('index.html');
const BODY = INDEX.slice(INDEX.indexOf('<div id="toolsModQg"'), INDEX.indexOf('<!-- /toolsModQg -->'));

const STUBS = `
  window.TC = { esc: (s) => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])) };
  window.SG = { open: (opts) => { window.__sse = opts; return { close() { window.__closed = true; } }; } };
  window.toolsSyncRunDot = function () {};
  window.__alerts = [];
  window.alert = function (m) { window.__alerts.push(String(m)); };
  window.__toasts = [];
  window.showToast = function (title, body) { window.__toasts.push(String(title) + ' — ' + String(body)); };
  window.__fetches = [];
  window.__syncCalls = [];
  window._syncActiveProfile = function (mid, values) { window.__syncCalls.push([mid, values]); return Promise.resolve(); };
  window._recordToolRun = function (tool, data) {
    fetch('/api/tools/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tool, ...data }) }).catch(() => {});
  };
  window.__cfg = { 'org/m:Q4': { 'ctx-size': '8192', 'cache-type-k': 'q8_0', threads: '16' } };
  window.fetch = function (url, opts) {
    window.__fetches.push([String(url), opts]);
    const u = String(url);
    const body = u.startsWith('/api/benchmark/models') ? { models: ['org/m:Q4', 'org/big:Q4'] }
      : (u === '/api/llm/config' && (!opts || !opts.method)) ? window.__cfg
      : u.startsWith('/api/llm/autotune/preflight') ? (window.__pre || { ok: true, busy: false, perplexity: true })
      : u.startsWith('/api/llama-state') ? { state: window.__llamaState || 'stopped' }
      : (u === '/api/llm/autotune/run' && opts && opts.method === 'POST') ? (window.__runReply || { ok: true, run_id: 'q1' })
      : (u === '/api/llm/config' && opts && opts.method === 'POST') ? (window.__configWriteReply || { ok: true })
      : { ok: true };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
  };
`;

function boot(bootstrap = '') {
  return runHarness({ sources: [STUBS, srcFile('js/quality.js')], bodyHtml: BODY, bootstrap });
}

async function opened(model, opts) {
  const win = boot();
  await win.QG.onOpen(model, opts);
  await flush();
  return win;
}

// Same module with the shared run gate wired in (#888).
async function openedGated(bootstrap = '') {
  const win = runHarness({ sources: [STUBS, QUEUE_SLOT_STUB, srcFile('js/quality.js')],
                           bodyHtml: BODY, bootstrap });
  await win.QG.onOpen('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
  await flush();
  return win;
}

const qrow = (win, key) => win.document.querySelector(`#qgRows [data-qg-key="${key}"]`);
const clickOn = (el) => el.dispatchEvent(new el.ownerDocument.defaultView.MouseEvent('click', { bubbles: true }));

describe('Quality guard module (#888)', () => {
  it('pre-fills override rows from opts and posts a quality-mode run body', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
    expect(win.document.querySelector('#qgModelList .mc-toggle.on').dataset.model).toBe('org/m:Q4');
    // Every quality-guard key has a row; only the pre-filled one is switched on.
    expect(win.document.querySelectorAll('#qgRows .qg-row').length).toBe(10);
    const on = [...win.document.querySelectorAll('#qgRows .qg-row')].filter(r => r.classList.contains('on'));
    expect(on.map(r => r.dataset.qgKey)).toEqual(['cache-type-k']);
    expect(on[0].querySelector('.cur').textContent).toBe('q8_0');
    expect(on[0].querySelector('.bl-chip.on').dataset.qgV).toBe('q4_0');
    expect(win.QG.overrides()).toEqual({ 'cache-type-k': 'q4_0' });
    await win.QG.run(); await flush();
    const post = win.__fetches.find(([u, o]) => u === '/api/llm/autotune/run' && o && o.method === 'POST');
    expect(JSON.parse(post[1].body)).toEqual({ model_ids: ['org/m:Q4'], objective: 'fit', mode: 'quality', overrides: { 'cache-type-k': 'q4_0' }, kl_max: 0.02 });
    expect(win.__sse.url).toBe('/api/llm/autotune/stream');
    expect(win.QG.running()).toBe(true);
  });

  it('refuses to run with no changes, through a themed toast rather than alert()', async () => {
    const win = await opened('org/m:Q4');
    await win.QG.run(); await flush();
    expect(win.__alerts.length).toBe(0);
    expect(win.__toasts.pop()).toMatch(/at least one key/i);
    expect(win.document.getElementById('qgLog').textContent).toMatch(/at least one key/i);
    const keys = [...win.document.querySelectorAll('#qgRows .qg-row')].map(r => r.dataset.qgKey);
    expect(keys).toContain('cache-type-v'); expect(keys).not.toContain('model');
  });

  it('drives overrides from chips, an on/off toggle and a number box', async () => {
    const win = await opened('org/m:Q4');
    clickOn(qrow(win, 'cache-type-v').querySelector('[data-qg-on]'));
    clickOn(qrow(win, 'cache-type-v').querySelector('.bl-chip[data-qg-v="q5_1"]'));
    clickOn(qrow(win, 'flash-attn').querySelector('[data-qg-on]'));
    clickOn(qrow(win, 'flash-attn').querySelector('[data-qg-bool]'));
    clickOn(qrow(win, 'batch-size').querySelector('[data-qg-on]'));
    const num = qrow(win, 'batch-size').querySelector('[data-qg-num]');
    num.value = '512';
    num.dispatchEvent(new win.Event('input', { bubbles: true }));
    expect(win.QG.overrides()).toEqual({ 'cache-type-v': 'q5_1', 'flash-attn': 'true', 'batch-size': '512' });
    expect(win.document.getElementById('qgCount').textContent).toBe('3');
  });

  it('offers the six editor load-mode values as chips, not a boolean toggle', async () => {
    const win = await opened('org/m:Q4');
    clickOn(qrow(win, 'load-mode').querySelector('[data-qg-on]'));
    const chips = [...qrow(win, 'load-mode').querySelectorAll('.bl-chip')].map(c => c.dataset.qgV);
    expect(chips).toEqual(['auto', 'none', 'mmap', 'mlock', 'mmap+mlock', 'dio']);
    clickOn(qrow(win, 'load-mode').querySelector('.bl-chip[data-qg-v="mmap+mlock"]'));
    expect(win.QG.overrides()).toEqual({ 'load-mode': 'mmap+mlock' });
  });

  it('does not send a key whose chosen value equals the model\'s current one', async () => {
    const win = await opened('org/m:Q4');
    // threads is 16 in the config; switching the row on seeds it with 16.
    clickOn(qrow(win, 'threads').querySelector('[data-qg-on]'));
    expect(qrow(win, 'threads').querySelector('[data-qg-num]').value).toBe('16');
    expect(win.QG.overrides()).toEqual({});
    expect(qrow(win, 'threads').querySelector('[data-qg-sum]').textContent).toMatch(/unchanged/);
    // Same for a chip set: re-picking the current KV type clears it from the body.
    clickOn(qrow(win, 'cache-type-k').querySelector('[data-qg-on]'));
    clickOn(qrow(win, 'cache-type-k').querySelector('.bl-chip[data-qg-v="q8_0"]'));
    expect(win.QG.overrides()).toEqual({});
    clickOn(qrow(win, 'cache-type-k').querySelector('.bl-chip[data-qg-v="q4_1"]'));
    expect(win.QG.overrides()).toEqual({ 'cache-type-k': 'q4_1' });
  });

  it('renders pass/fail from model_done, records the ledger row, and applies through the config write path', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
    await win.QG.run(); await flush();
    win.QG.onEvent({ type: 'model_start', model_id: 'org/m:Q4', mode: 'quality', stages: ['quality'] });
    win.QG.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'quality', run_id: 'q1', llama_build: 'b1',
      guard: { kl: 0.011, kl_max: 0.02, pass: true, error: null }, changes: [{ key: 'cache-type-k', current: 'q8_0', recommended: 'q4_0' }] });
    win.QG.onEvent({ type: 'done', ok: true }); await flush();
    expect(win.document.getElementById('qgResult').textContent).toMatch(/0\.011/);
    expect(win.document.getElementById('qgPill').textContent).toBe('pass');
    expect(win.document.getElementById('qgApplyBtn').style.display).not.toBe('none');
    const rec = win.__fetches.filter(([u, o]) => u === '/api/tools/runs' && o && o.method === 'POST').map(([, o]) => JSON.parse(o.body)).pop();
    expect(rec).toMatchObject({ tool: 'quality', model_id: 'org/m:Q4', ok: true, kl: 0.011, kl_pass: true, run_id: 'q1', llama_build: 'b1' });
    await win.QG.apply(); await flush();
    // Mirrors AT.apply's write path: read config, merge the full section, POST it, then sync the full section.
    const configWrite = win.__fetches.filter(([u, o]) => u === '/api/llm/config' && o && o.method === 'POST').map(([, o]) => JSON.parse(o.body)).pop();
    expect(configWrite['org/m:Q4']).toEqual({ 'ctx-size': '8192', 'cache-type-k': 'q4_0', threads: '16' });
    expect(win.__syncCalls.pop()).toEqual(['org/m:Q4', { 'ctx-size': '8192', 'cache-type-k': 'q4_0', threads: '16' }]);
  });

  it('shows a failed guard as fail and hides Apply', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
    await win.QG.run(); await flush();
    win.QG.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'quality', guard: { kl: 0.09, kl_max: 0.02, pass: false, error: null }, changes: [] });
    win.QG.onEvent({ type: 'done', ok: true }); await flush();
    expect(win.document.getElementById('qgPill').textContent).toBe('fail');
    expect(win.document.getElementById('qgApplyBtn').style.display).toBe('none');
  });

  it('ignores model_done events from a shared Autotune run', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
    await win.QG.run(); await flush();
    win.QG.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'autotune', changes: [] });
    expect(win.document.getElementById('qgResult').textContent).not.toMatch(/mean KL/);
  });

  it('surfaces a failed config write via a toast and applies neither the sync nor the "Applied" label', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
    await win.QG.run(); await flush();
    win.QG.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'quality', run_id: 'q1',
      guard: { kl: 0.011, kl_max: 0.02, pass: true, error: null }, changes: [{ key: 'cache-type-k', current: 'q8_0', recommended: 'q4_0' }] });
    win.QG.onEvent({ type: 'done', ok: true }); await flush();
    win.__configWriteReply = { ok: false, error: 'config locked' };
    await win.QG.apply(); await flush();
    expect(win.__alerts.length).toBe(0);
    expect(win.__toasts.pop()).toMatch(/config locked/);
    expect(win.document.getElementById('qgApplyBtn').textContent).not.toContain('Applied');
    expect(win.__syncCalls.length).toBe(0);
  });

  it('warns and blocks Run while llama-server holds the GPU', async () => {
    const win = boot();
    win.__pre = { ok: true, busy: false, perplexity: true, unit_active: true };
    await win.QG.onOpen('org/m:Q4'); await flush();
    const pf = win.document.getElementById('qgPreflight');
    expect(pf.style.display).toBe('');
    expect(win.document.getElementById('qgPreflightMsg').textContent).toMatch(/llama-server is running.*quality check/i);
    expect(win.document.getElementById('qgRunBtn').disabled).toBe(true);
  });

  it('keeps Run enabled and the banner hidden on a clean preflight', async () => {
    const win = await opened('org/m:Q4');
    expect(win.document.getElementById('qgPreflight').style.display).toBe('none');
    expect(win.document.getElementById('qgRunBtn').disabled).toBe(false);
  });

  it('attaches neutrally when the shared stream is already busy', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
    win.__runReply = { ok: false, error: 'a run is already in progress' };
    await win.QG.run(); await flush();
    expect(win.document.getElementById('qgPill').textContent).toBe('another tool is running');
    expect(win.__alerts.length).toBe(0);
    expect(win.__toasts.length).toBe(0);
  });

  it('the post-Apply refresh calls the function the dashboard actually defines', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
    win.__refreshed = 0;
    win.refreshLLMTab = function () { win.__refreshed += 1; };
    await win.QG.run(); await flush();
    win.QG.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'quality', run_id: 'q1',
      guard: { kl: 0.011, kl_max: 0.02, pass: true, error: null }, changes: [{ key: 'cache-type-k', current: 'q8_0', recommended: 'q4_0' }] });
    win.QG.onEvent({ type: 'done', ok: true }); await flush();
    await win.QG.apply(); await flush();
    expect(win.__refreshed).toBe(1);
  });

  it('detach closes the stream without cancelling the run', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
    await win.QG.run(); await flush();
    expect(win.QG.running()).toBe(true);
    win.QG.detach();
    expect(win.__closed).toBe(true);
    expect(win.QG.running()).toBe(false);
    expect(win.__fetches.some(([u]) => u === '/api/llm/autotune/cancel')).toBe(false);
  });

  // A failed KL guard sets .warn on the pill; the module is width-capped and its panes spaced like Autotune's.
  it('styles the warn pill and shares Autotune\'s width cap and pane spacing', () => {
    expect(srcFile('css/base.css')).toMatch(/\.bench-status-pill\.warn\b[^}]*var\(--warn\)/);
    const tools = srcFile('css/tools.css');
    expect(tools).toMatch(/#toolsModAt, #toolsModQg \{ max-width: 1180px; margin: 0 auto; \}/);
    expect(tools).toMatch(/#toolsModQg \.bench-body \{ grid-template-columns: 1fr; \}/);
    expect(BODY).toContain('class="bench-results-pane at-panes"');
    expect(BODY).toContain('class="at-pane"');
  });

  it('shows the agent\'s perplexity hint and blocks Run', async () => {
    const win = boot();
    win.__pre = { ok: true, busy: false, perplexity: false,
      perplexity_detail: { present: true, kl_text: true, runnable: false, rc: -11, hint: 'llama-perplexity crashes on startup and looks stale next to the installed llama.cpp libraries.' } };
    await win.QG.onOpen('org/m:Q4'); await flush();
    expect(win.document.getElementById('qgPreflight').style.display).toBe('');
    expect(win.document.getElementById('qgPreflightMsg').textContent).toMatch(/crashes on startup/);
    expect(win.document.getElementById('qgRunBtn').disabled).toBe(true);
    expect(win.document.getElementById('qgStopBtn').style.display).toBe('none');
  });

  it('keeps the old missing-tooling banner and an enabled Run for an agent with no perplexity_detail', async () => {
    const win = boot();
    win.__pre = { ok: true, busy: false, perplexity: false };
    await win.QG.onOpen('org/m:Q4'); await flush();
    expect(win.document.getElementById('qgPreflightMsg').textContent).toMatch(/bench runtime/);
    expect(win.document.getElementById('qgRunBtn').disabled).toBe(false);
  });

  it('stops llama-server from the banner and clears it', async () => {
    const win = boot();
    win.__pre = { ok: true, busy: false, perplexity: true, unit_active: true };
    win.__llamaState = 'awake';
    await win.QG.onOpen('org/m:Q4'); await flush();
    const stop = win.document.getElementById('qgStopBtn');
    expect(stop.style.display).toBe('');
    win.__llamaState = 'stopped';
    win.__pre = { ok: true, busy: false, perplexity: true, unit_active: false };
    await win.QG.stopServer(); await flush();
    expect(win.__fetches.some(([u, o]) => u === '/api/llm/server/stop' && o && o.method === 'POST')).toBe(true);
    expect(win.document.getElementById('qgPreflight').style.display).toBe('none');
    expect(win.document.getElementById('qgRunBtn').disabled).toBe(false);
    expect(stop.style.display).toBe('none');
  });

  it('opens neutral when the agent is busy with another tool, then flips to running on a quality-mode event', async () => {
    const win = boot();
    win.__pre = { ok: true, busy: true, perplexity: true };
    await win.QG.onOpen('org/m:Q4'); await flush();
    expect(win.document.getElementById('qgPill').textContent).toBe('another tool is running');
    expect(win.document.getElementById('qgRunBtn').style.display).toBe('none');
    win.QG.onEvent({ type: 'stage_start', model_id: 'org/m:Q4', stage: 'context' });
    expect(win.document.getElementById('qgPill').textContent).toBe('another tool is running');
    win.QG.onEvent({ type: 'model_start', model_id: 'org/m:Q4', mode: 'quality', stages: ['quality'] });
    expect(win.document.getElementById('qgPill').textContent).toBe('running');
  });
  const STATS = { kl: 0.0054, same_top_p: 98.118, rms_dp: 3.236, p999_dp: 17.595, max_dp: 23.904 };

  // Drives a finished run into the module and returns the window.
  async function finished(win, guard, changes) {
    await win.QG.run(); await flush();
    win.QG.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'quality', run_id: 'q1',
      elapsed_s: 132, guard, changes: changes || [{ key: 'ubatch-size', current: '1024', recommended: '512' }] });
    win.QG.onEvent({ type: 'done', ok: true }); await flush();
    return win;
  }
  const verdictText = (win) => win.document.getElementById('qgVerdict').textContent;

  it('reads a comfortable pass, a marginal pass and a fail differently', async () => {
    let win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    await finished(win, { kl: 0.0054, kl_max: 0.02, pass: true, error: null, stats: STATS });
    const comfortable = verdictText(win);
    expect(comfortable).toBe('Quality is safe: the measured 0.0054 is 3.7× smaller than the 0.02 limit, and it picks the same most-likely next token as the f16 reference 98.1% of the time. Differences at this level are not visible in normal use.');

    win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    await finished(win, { kl: 0.019, kl_max: 0.02, pass: true, error: null, stats: { ...STATS, same_top_p: 91.02 } });
    const marginal = verdictText(win);
    expect(marginal).toBe('Quality is inside the limit, but only just: the measured 0.0190 is 95% of the 0.02 line, and it picks the same most-likely next token as the f16 reference 91.0% of the time. Occasional wording differences are likely. Tighten the limit or test a milder value if this model does exact-format work.');

    win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    await finished(win, { kl: 0.031, kl_max: 0.02, pass: false, error: null, stats: { ...STATS, same_top_p: 88.0 } });
    const fail = verdictText(win);
    expect(fail).toBe('Quality is not safe: the measured 0.0310 is 1.6× the 0.02 limit (55% over), and it picks a different most-likely next token from the f16 reference on 12.0% of tokens. Applying this would cost measurable output quality — keep the current setting, or test a milder value.');
    expect(new Set([comfortable, marginal, fail]).size).toBe(3);
    expect(win.document.querySelector('#qgResult .qg-verdict').classList.contains('bad')).toBe(true);
    expect(win.document.getElementById('qgApplyBtn').style.display).toBe('none');
  });

  // #887: a ratio is not a percentage over, and a rounded zero is not a proven zero.
  it('states the over-limit multiple and the percentage over separately', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    await finished(win, { kl: 0.03, kl_max: 0.02, pass: false, error: null, stats: null });
    const say = verdictText(win);
    expect(say).toContain('is 1.5× the 0.02 limit (50% over)');
    expect(say).not.toContain('1.5× over');
  });

  it('hedges a divergence that only rounded to zero', async () => {
    let win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    await finished(win, { kl: 0, kl_max: 0.02, pass: true, error: null, stats: null });
    const zero = verdictText(win);
    expect(zero).toContain('rounds to 0.0000');
    expect(zero).not.toContain('at all');

    win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    await finished(win, { kl: 0.00002, kl_max: 0.02, pass: true, error: null, stats: null });
    // Below the limit reads as a size comparison, never as "× below".
    expect(verdictText(win)).toContain('smaller than the 0.02 limit');
    expect(verdictText(win)).not.toContain('× below');
  });

  it('renders the stat strip the payload carries and drops the fields it does not', async () => {
    let win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    await finished(win, { kl: 0.0054, kl_max: 0.02, pass: true, error: null, stats: STATS });
    let cells = [...win.document.querySelectorAll('#qgResult .qg-stat')];
    expect(cells.map(c => c.querySelector('.v').textContent)).toEqual(['98.1%', '3.24%', '17.6%', '23.9%']);
    expect(cells[0].querySelector('.k').textContent).toBe('Same top token');

    win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    await finished(win, { kl: 0.0054, kl_max: 0.02, pass: true, error: null, stats: { kl: 0.0054, rms_dp: 3.236 } });
    cells = [...win.document.querySelectorAll('#qgResult .qg-stat')];
    expect(cells.map(c => c.querySelector('.k').textContent)).toEqual(['Typical Δ probability']);
    // No Same-top-p means the verdict drops that clause but still reads the KL number.
    expect(verdictText(win)).toBe('Quality is safe: the measured 0.0054 is 3.7× smaller than the 0.02 limit. Differences at this level are not visible in normal use.');

    win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    await finished(win, { kl: 0.0054, kl_max: 0.02, pass: true, error: null, stats: null });
    expect(win.document.querySelectorAll('#qgResult .qg-stat').length).toBe(0);
    expect(win.document.querySelectorAll('#qgResult .qg-big .v')[0].textContent).toBe('0.0054');
  });

  it('ticks the elapsed strip while a check runs and settles it on completion', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    let now = 1_000_000, ticker = null, cleared = 0;
    win.Date.now = () => now;
    win.setInterval = (fn) => { ticker = fn; return 7; };
    win.clearInterval = () => { cleared += 1; };
    await win.QG.run(); await flush();
    const time = win.document.getElementById('qgStripTime'), strip = win.document.getElementById('qgStrip');
    expect(time.textContent).toBe('elapsed 00:00');
    expect(win.document.getElementById('qgPill').textContent).toBe('running');
    win.QG.onEvent({ type: 'candidate_start', stage: 'quality', value: 'f16 base' });
    expect(strip.textContent).toBe('pass 1 of 2 · f16 reference');
    now += 65_000; ticker();
    expect(time.textContent).toBe('elapsed 01:05');
    win.QG.onEvent({ type: 'candidate_start', stage: 'quality', value: 'candidate' });
    expect(strip.textContent).toBe('pass 2 of 2 · candidate config');
    win.QG.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'quality', run_id: 'q1',
      elapsed_s: 132, guard: { kl: 0.0054, kl_max: 0.02, pass: true, error: null, stats: STATS },
      changes: [{ key: 'ubatch-size', current: '1024', recommended: '512' }] });
    win.QG.onEvent({ type: 'done', ok: true }); await flush();
    expect(cleared).toBeGreaterThan(0);
    expect(time.textContent).toBe('took 02:12');
    expect(strip.textContent).toBe('both passes complete · quality within the limit');
    expect(win.document.getElementById('qgPill').textContent).toBe('pass');
    // The counter stops: a stale interval callback can no longer overwrite the total.
    ticker(); now += 60_000; ticker();
    expect(time.textContent).toBe('took 02:12');
  });

  it('offers a speed measurement beside Apply and deep-links Benchmark at the same model', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    win.__opened = [];
    win.toolsOpenTool = function (id, mid) { win.__opened.push([id, mid]); };
    await finished(win, { kl: 0.0054, kl_max: 0.02, pass: true, error: null, stats: STATS });
    const note = win.document.getElementById('qgApplyNote');
    expect(win.document.getElementById('qgApplyBtn').style.display).toBe('');
    expect(note.textContent).toMatch(/does not mean it is faster/);
    expect(note.textContent).toMatch(/never measures speed/);
    const bb = win.document.getElementById('qgBenchBtn');
    expect(bb.style.display).toBe('');
    clickOn(bb); win.QG.openBenchmark();
    expect(win.__opened.pop()).toEqual(['benchmark', 'org/m:Q4']);
  });

  it('keeps the speed caveat and the benchmark link on a failed guard', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'ubatch-size': '512' } });
    await finished(win, { kl: 0.031, kl_max: 0.02, pass: false, error: null, stats: STATS });
    expect(win.document.getElementById('qgApplyBtn').style.display).toBe('none');
    expect(win.document.getElementById('qgBenchBtn').style.display).toBe('');
    expect(win.document.getElementById('qgApplyNote').textContent).toMatch(/measures quality only, never speed/);
    expect(win.document.getElementById('qgStrip').textContent).toBe('both passes complete · quality over the limit');
  });
});


// #888: the shared run gate — Run becomes Queue while another tool holds the host.
describe('QG queueing behind another tool (#888)', () => {
  const BUSY = "window.__gateBusy = { tool: 'benchmark', label: 'Benchmark', host: 'gpu-01', agent_id: 'a1' };";
  const runPosts = (win) => win.__fetches.filter(
    f => f[0] === '/api/llm/autotune/run' && f[1] && f[1].method === 'POST');

  it('queues instead of starting while a Benchmark holds the host', async () => {
    const win = await openedGated(BUSY);
    win.__fetches.length = 0;
    await win.QG.run();
    await flush();
    expect(runPosts(win)).toHaveLength(0);
    expect(win.__slots[0].queued()).toBe(true);
    expect(win.document.getElementById('qgRunBtn').textContent).toContain('Queued');
    expect(win.document.getElementById('qgQueueNote').textContent)
      .toContain('Queued behind Benchmark on gpu-01');
    expect(win.document.getElementById('qgPill').textContent).toBe('queued');
    expect(win.__queued).toEqual(['quality', 'Benchmark on gpu-01']);
  });

  it('starts the queued check by itself once the gate clears', async () => {
    const win = await openedGated(BUSY);
    await win.QG.run();
    await flush();
    win.__fetches.length = 0;
    win.__gateBusy = null;
    await win.__slots[0].fire();
    for (let i = 0; i < 4; i++) await flush();
    expect(runPosts(win)).toHaveLength(1);
    expect(JSON.parse(runPosts(win)[0][1].body).mode).toBe('quality');
  });

  it('drops a queued check on Cancel without cancelling anything on the agent', async () => {
    const win = await openedGated(BUSY);
    await win.QG.run();
    await flush();
    win.__fetches.length = 0;
    win.QG.cancel();
    expect(win.__slots[0].queued()).toBe(false);
    expect(win.__fetches.map(f => f[0])).not.toContain('/api/llm/autotune/cancel');
  });

  it('labels the button Queue while the host is busy and nothing is pending', async () => {
    const win = await openedGated(BUSY);
    expect(win.document.getElementById('qgRunBtn').textContent).toContain('Queue check');
    expect(win.document.getElementById('qgQueueNote').textContent)
      .toContain('Benchmark is running on gpu-01');
  });

  it('queues rather than losing the check when the agent refuses it', async () => {
    const win = await openedGated();
    win.__runReply = { ok: false, error: 'an autotune run is already in progress' };
    await win.QG.run();
    for (let i = 0; i < 4; i++) await flush();
    expect(win.__slots[0].queued()).toBe(true);
    expect(win.__slots[0].waitFor()).toBe('the run in progress');
    // Still attached to the run it lost the race to.
    expect(win.__sse.url).toBe('/api/llm/autotune/stream');
  });
});
