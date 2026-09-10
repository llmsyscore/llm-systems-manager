// #888: Quality guard module — standalone KL check of any config change against f16.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

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
});
