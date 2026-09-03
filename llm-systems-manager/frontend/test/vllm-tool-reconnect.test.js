// #782/#780: the vLLM wizards ride the shared SSE guard and post ledger rows
// that carry the agent's run id.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const BODY = `
  <button id="vllmAtRunBtn"></button><button id="vllmAtCancelBtn"></button>
  <span id="vllmAtStatus" class="sub"></span>
  <div id="vllmAtProgress"></div><div id="vllmAtResults"></div>
  <div id="vllmAtRawLog"></div><span id="vllmAtRawCount"></span>
  <input id="vllmAtProbeLen" value="4096"><input id="vllmAtConc" value="1">
  <input id="vllmAtFrac" value="100"><input id="vllmAtTimeout" value="600">
  <input id="vllmAtReportOnly" type="checkbox">
  <button id="vllmBenchRunBtn"></button><button id="vllmBenchCancelBtn"></button>
  <span id="vllmBenchStatus" class="sub"></span>
  <div id="vllmBenchResults"></div>
  <div id="vllmBenchRawLog"></div><span id="vllmBenchRawCount"></span>
`;

const STUBS = `
  window._esc = (s) => String(s == null ? '' : s);
  window._jsonOrThrow = (r) => r.json();
  window._withAgentParam = (u) => u + '?agent=a1';
  window.toolsSyncRunDot = function () {};
  window._themedConfirm = () => Promise.resolve(true);
  window.__posts = [];
  window._recordToolRun = (tool, data) => { window.__posts.push({ tool, ...data }); };
  window.__streams = [];
  window.__timers = [];
  window.EventSource = function (url) { this.url = url; this.readyState = 0; window.__streams.push(this); };
  EventSource.CONNECTING = 0; EventSource.OPEN = 1; EventSource.CLOSED = 2;
  EventSource.prototype.close = function () { this.readyState = 2; };
  window.fetch = function () {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, results: [] }) });
  };
`;

const GUARD_TIMERS = `
  window.SG.timers.setTimeout = (fn, ms) => { window.__timers.push({ fn, ms }); return 1; };
  window.SG.timers.clearTimeout = () => {};
`;

async function start(fn) {
  const win = runHarness({
    sources: [STUBS, srcFile('js/lib/sseguard.js'), GUARD_TIMERS,
              srcFile('js/vllm-bench-autotune.js')],
    bodyHtml: BODY,
    bootstrap: `_vatOrig = { binary: '/opt/vllm/bin/vllm serve org/m', args: [] }; window.__started = ${fn}();`,
  });
  await win.__started;
  await flush();
  const es = win.__streams[win.__streams.length - 1];
  expect(es, `${fn} should have opened a stream`).toBeTruthy();
  es.readyState = 1;
  return { win, es,
           send: (m, id) => es.onmessage({ data: JSON.stringify(m), lastEventId: id || 'run9:3' }) };
}

const text = (win, id) => win.document.getElementById(id).textContent;

describe('vLLM autotune stream', () => {
  it('opens through the agent-aware URL and marks a transient drop', async () => {
    const r = await start('runVllmAutotune');
    expect(r.es.url).toBe('/api/vllm/autotune/stream?agent=a1');
    r.send({ type: 'model_start', unit: 'vllm.service', model: 'org/m' });
    expect(text(r.win, 'vllmAtStatus')).toBe('Tuning org/m…');
    r.es.readyState = 0; r.es.onerror();
    expect(text(r.win, 'vllmAtStatus')).toBe('reconnecting…');
    r.es.readyState = 1;
    r.send({ type: 'keepalive' });
    expect(text(r.win, 'vllmAtStatus')).toBe('Tuning org/m…');
  });

  it('re-opens a closed source with the resume id instead of giving up', async () => {
    const r = await start('runVllmAutotune');
    r.send({ type: 'step_start', step: 'probe' }, 'run9:1');
    r.es.readyState = 2; r.es.onerror();
    expect(text(r.win, 'vllmAtStatus')).toBe('reconnecting…');
    expect(r.win.document.getElementById('vllmAtCancelBtn').style.display).not.toBe('none');
    r.win.__timers.shift().fn();
    const es2 = r.win.__streams[r.win.__streams.length - 1];
    expect(es2.url).toBe('/api/vllm/autotune/stream?agent=a1&last_event_id=run9%3A1');
  });

  it('records a ledger row on model_done with the agent run id (#780)', async () => {
    const r = await start('runVllmAutotune');
    r.send({ type: 'model_done', ok: true, model_id: 'org/m', run_id: 'agentrun',
             applied: true, report_only: false, max_model_len: 230400, kv_tokens: 460800 });
    expect(r.win.__posts).toEqual([{ tool: 'autotune', model_id: 'org/m', provider: 'vllm',
      ok: true, run_id: 'agentrun', max_model_len: 230400, kv_tokens: 460800,
      applied: true, report_only: false }]);
  });

  it('falls back to the SSE id when an older agent sends no run id or model', async () => {
    const r = await start('runVllmAutotune');
    r.send({ type: 'model_done', ok: true, model_id: 'org/m' }, 'run9:7');
    expect(r.win.__posts[0].run_id).toBe('run9');
    r.win.__posts.length = 0;
    r.send({ type: 'model_done', ok: false, error: 'x' });
    expect(r.win.__posts).toEqual([]);
  });
});

describe('vLLM benchmark stream', () => {
  it('posts the ledger row with the run id and survives a transient drop', async () => {
    const r = await start('runVllmBench');
    r.send({ type: 'model_start', model: 'org/m', cmd: 'vllm bench serve' });
    expect(text(r.win, 'vllmBenchStatus')).toBe('Benchmarking org/m…');
    r.es.readyState = 0; r.es.onerror();
    expect(text(r.win, 'vllmBenchStatus')).toBe('reconnecting…');
    r.es.readyState = 1;
    r.send({ type: 'result', model_id: 'org/m', run_id: 'agentrun',
             extra: { output_throughput: 1063.9, total_token_throughput: 9800.2 } });
    expect(text(r.win, 'vllmBenchStatus')).toBe('Benchmarking org/m…');
    expect(r.win.__posts[0]).toMatchObject({ tool: 'benchmark', provider: 'vllm',
      model_id: 'org/m', gen_tps: 1063.9, pg_tps: 9800.2, run_id: 'agentrun' });
  });

  it('closes cleanly on done', async () => {
    const r = await start('runVllmBench');
    r.send({ type: 'done', ok: true, cancelled: false });
    expect(text(r.win, 'vllmBenchStatus')).toBe('Done.');
    expect(r.es.readyState).toBe(2);
  });
});
