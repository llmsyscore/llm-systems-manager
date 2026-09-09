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

describe('Quality guard module (#888)', () => {
  it('pre-fills override rows from opts and posts a quality-mode run body', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
    expect(win.document.getElementById('qgModel').value).toBe('org/m:Q4');
    const rows = [...win.document.querySelectorAll('#qgRows .qg-row')];
    expect(rows.length).toBe(1);
    expect(rows[0].querySelector('.cur').textContent).toBe('q8_0');
    expect(rows[0].querySelector('input').value).toBe('q4_0');
    expect(win.QG.overrides()).toEqual({ 'cache-type-k': 'q4_0' });
    await win.QG.run(); await flush();
    const post = win.__fetches.find(([u, o]) => u === '/api/llm/autotune/run' && o && o.method === 'POST');
    expect(JSON.parse(post[1].body)).toEqual({ model_ids: ['org/m:Q4'], objective: 'fit', mode: 'quality', overrides: { 'cache-type-k': 'q4_0' }, kl_max: 0.02 });
    expect(win.__sse.url).toBe('/api/llm/autotune/stream');
    expect(win.QG.running()).toBe(true);
  });

  it('refuses to run with no changes and only offers quality-guard keys', async () => {
    const win = await opened('org/m:Q4');
    await win.QG.run(); await flush();
    expect(win.__alerts.pop()).toMatch(/change at least one/i);
    const keys = [...win.document.querySelectorAll('#qgAddKey option')].map(o => o.value).filter(Boolean);
    expect(keys).toContain('cache-type-v'); expect(keys).not.toContain('model');
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

  it('surfaces a failed config write via alert and applies neither the sync nor the "Applied" label', async () => {
    const win = await opened('org/m:Q4', { overrides: { 'cache-type-k': 'q4_0' } });
    await win.QG.run(); await flush();
    win.QG.onEvent({ type: 'model_done', model_id: 'org/m:Q4', ok: true, mode: 'quality', run_id: 'q1',
      guard: { kl: 0.011, kl_max: 0.02, pass: true, error: null }, changes: [{ key: 'cache-type-k', current: 'q8_0', recommended: 'q4_0' }] });
    win.QG.onEvent({ type: 'done', ok: true }); await flush();
    win.__configWriteReply = { ok: false, error: 'config locked' };
    await win.QG.apply(); await flush();
    expect(win.__alerts.pop()).toMatch(/config locked/);
    expect(win.document.getElementById('qgApplyBtn').textContent).not.toContain('Applied');
    expect(win.__syncCalls.length).toBe(0);
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
