// #468: drive the real rcRun -> rcStream flow with stubbed network. Catches
// wiring bugs unit tests on the lib can't — e.g. rcStream's internal stream
// reset re-enabling the Run button and hiding Cancel for the whole run.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import SG from '../js/lib/sseguard.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const code = readFileSync(resolve(ROOT, 'js/report-card.js'), 'utf8');

const tick = () => new Promise(r => setTimeout(r, 0));

function mountDom() {
  document.body.innerHTML = `
    <select id="rcProvider"><option value="llama" selected>llama.cpp</option></select>
    <select id="rcAgent"><option value="${'a'.repeat(32)}" selected>host</option></select>
    <select id="rcMode"><option value="standard" selected>Standard</option>
      <option value="custom">Custom</option></select>
    <div id="rcModelKeyField"><select id="rcModelKey">
      <option value="small" selected>small</option></select></div>
    <div id="rcCustomModelField"><input id="rcCustomModel" list="rcModelOptions">
      <datalist id="rcModelOptions"></datalist></div>
    <input id="rcPrice" value="0.15">
    <button id="rcRunBtn">▶ Run report card</button>
    <button id="rcCancelBtn" style="display:none;">✕ Cancel</button>
    <div id="rcNote"></div><div id="rcStatus" style="display:none;"></div>
    <div id="rcProgress" style="display:none;"></div>
    <div id="rcCardHost"></div>
    <div id="rcActions" style="display:none;">
      <span id="rcSubmitWrap" style="display:none;" title="Leaderboard submission is coming soon in the next release."><button
        id="rcSubmitBtn" disabled aria-disabled="true"></button></span></div>
    <div id="rcDownload" style="display:none;"><div id="rcDownloadMsg"></div></div>
    <div id="rcCleanup" style="display:none;"><div id="rcCleanupMsg"></div>
      <button id="rcCleanupDeleteBtn"></button></div>
    <div id="rcConfirm" style="display:none;">
      <b id="rcConfirmServed"></b><span id="rcConfirmRef"></span></div>
    <div id="rcTrends" style="display:none;"><canvas id="rcTrendChart"></canvas></div>`;
}

function loadModule({ runResponse, httpOk = true }) {
  const sources = [];
  class FakeEventSource {
    constructor(url) { this.url = url; sources.push(this); }
    close() { this.closed = true; }
  }
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal('SG', SG);
  vi.stubGlobal('RC', {
    PROVIDER_LABEL: { llama: 'llama.cpp' },
    buildCard: () => document.createDocumentFragment(),
    submitUrl: () => '',
    trendSeries: () => ({ labels: [], gen: [], prefill: [], tpj: [] }),
  });
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: httpOk, json: async () => runResponse,
  })));
  // Execute the classic script with a return of the handles the test drives.
  const fn = new Function(code + `
    ;return { rcRun, rcStream, rcStopStream, rcCancelRun, rcCleanupDelete,
              rcOnModeChange, rcOnAgentChange };`);
  return { api: fn(), sources };
}

beforeEach(() => {
  vi.unstubAllGlobals();
  mountDom();
});

describe('run flow busy state', () => {
  it('keeps Run disabled and Cancel visible while the stream is open', async () => {
    const { api, sources } = loadModule({
      runResponse: { ok: true, job_id: 'j1' } });
    api.rcRun();
    await tick(); await tick();
    expect(sources).toHaveLength(1);
    expect(sources[0].url).toContain('/api/reportcard/stream/j1');
    const run = document.getElementById('rcRunBtn');
    const cancel = document.getElementById('rcCancelBtn');
    expect(run.disabled).toBe(true);
    expect(run.textContent).toBe('Running…');
    expect(cancel.style.display).not.toBe('none');
  });

  it('re-enables Run and hides Cancel when the run finishes', async () => {
    const { api, sources } = loadModule({
      runResponse: { ok: true, job_id: 'j1' } });
    api.rcRun();
    await tick(); await tick();
    sources[0].onmessage({ data: JSON.stringify({ event: 'done',
      card: { result: {}, provider: 'llama', ts: 1, mode: 'custom',
              preset_version: 'preset_v1', eligible: false } }) });
    expect(document.getElementById('rcRunBtn').disabled).toBe(false);
    expect(document.getElementById('rcCancelBtn').style.display).toBe('none');
    expect(sources[0].closed).toBe(true);
  });

  it('cancel POSTs against the live job id', async () => {
    const { api } = loadModule({ runResponse: { ok: true, job_id: 'j9' } });
    api.rcRun();
    await tick(); await tick();
    api.rcCancelRun();
    const urls = fetch.mock.calls.map(c => c[0]);
    expect(urls).toContain('/api/reportcard/cancel/j9');
  });

  it('abandoning the run via rcStopStream re-enables the button', async () => {
    const { api, sources } = loadModule({
      runResponse: { ok: true, job_id: 'j1' } });
    api.rcRun();
    await tick(); await tick();
    api.rcStopStream();          // tab-switch path
    expect(document.getElementById('rcRunBtn').disabled).toBe(false);
    expect(sources[0].closed).toBe(true);
  });

  it('a needs_download reply shows the prompt and leaves Run enabled', async () => {
    const { api, sources } = loadModule({
      runResponse: { ok: true, status: 'needs_download',
                     model: 'Qwen/x:Q4_K_M', approx_gb: 1.1, restarts: true } });
    api.rcRun();
    await tick(); await tick();
    expect(sources).toHaveLength(0);
    expect(document.getElementById('rcRunBtn').disabled).toBe(false);
    expect(document.getElementById('rcDownload').style.display).not.toBe('none');
    expect(document.getElementById('rcDownloadMsg').textContent)
      .toContain('restart llama.cpp');
  });
});

describe('elapsed ticker (#491)', () => {
  it('keeps the seconds counting between SSE events', async () => {
    vi.useFakeTimers();
    try {
      const { api, sources } = loadModule({
        runResponse: { ok: true, job_id: 'j1' } });
      api.rcRun();
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(0);
      sources[0].onmessage({ data: JSON.stringify({ event: 'progress',
        phase: 'waiting', elapsed_s: 12 }) });
      const el = document.getElementById('rcStatus');
      expect(el.textContent).toContain('· 12s');
      await vi.advanceTimersByTimeAsync(5000);
      expect(el.textContent).toContain('· 17s');
    } finally { vi.useRealTimers(); }
  });

  it('stops ticking once the run reaches a terminal event', async () => {
    vi.useFakeTimers();
    try {
      const { api, sources } = loadModule({
        runResponse: { ok: true, job_id: 'j1' } });
      api.rcRun();
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(0);
      sources[0].onmessage({ data: JSON.stringify({ event: 'progress',
        phase: 'waiting', elapsed_s: 3 }) });
      sources[0].onmessage({ data: JSON.stringify({ event: 'error',
        error: 'boom' }) });
      const el = document.getElementById('rcStatus');
      await vi.advanceTimersByTimeAsync(5000);
      expect(el.style.display).toBe('none');
    } finally { vi.useRealTimers(); }
  });
});

describe('post-run cleanup offer (#492)', () => {
  const done = (cleanup) => ({ data: JSON.stringify({ event: 'done',
    card: { result: {}, provider: 'llama', ts: 1, mode: 'standard',
            preset_version: 'preset_v1', eligible: true }, cleanup }) });

  it('offers deletion when the run downloaded a deletable model', async () => {
    const { api, sources } = loadModule({
      runResponse: { ok: true, job_id: 'j1' } });
    api.rcRun();
    await tick(); await tick();
    sources[0].onmessage(done({ downloaded: true, deletable: true,
                                model_key: 'small' }));
    expect(document.getElementById('rcCleanup').style.display).not.toBe('none');
    expect(document.getElementById('rcCleanupDeleteBtn').style.display)
      .not.toBe('none');
  });

  it('hides the delete button when the host cannot purge (lms)', async () => {
    const { api, sources } = loadModule({
      runResponse: { ok: true, job_id: 'j1' } });
    api.rcRun();
    await tick(); await tick();
    sources[0].onmessage(done({ downloaded: true, deletable: false,
                                model_key: 'small' }));
    expect(document.getElementById('rcCleanup').style.display).not.toBe('none');
    expect(document.getElementById('rcCleanupDeleteBtn').style.display)
      .toBe('none');
  });

  it('shows no offer when the model was already on the host', async () => {
    const { api, sources } = loadModule({
      runResponse: { ok: true, job_id: 'j1' } });
    api.rcRun();
    await tick(); await tick();
    sources[0].onmessage(done({ downloaded: false, deletable: false,
                                model_key: 'small' }));
    expect(document.getElementById('rcCleanup').style.display).toBe('none');
  });

  it('delete POSTs the run identity to the delete-model route', async () => {
    const { api, sources } = loadModule({
      runResponse: { ok: true, job_id: 'j1' } });
    api.rcRun();
    await tick(); await tick();
    sources[0].onmessage(done({ downloaded: true, deletable: true,
                                model_key: 'small' }));
    api.rcCleanupDelete();
    await tick();
    const call = fetch.mock.calls.find(c => c[0] === '/api/reportcard/delete-model');
    expect(call).toBeTruthy();
    expect(JSON.parse(call[1].body)).toEqual({
      agent: 'a'.repeat(32), provider: 'llama', model_key: 'small' });
    expect(document.getElementById('rcCleanup').style.display).toBe('none');
  });

  it('delete targets the run host even after the picker changes', async () => {
    const { api, sources } = loadModule({
      runResponse: { ok: true, job_id: 'j1' } });
    api.rcRun();
    await tick(); await tick();
    sources[0].onmessage(done({ downloaded: true, deletable: true,
                                model_key: 'small' }));
    const sel = document.getElementById('rcAgent');
    const other = document.createElement('option');
    other.value = 'b'.repeat(32);
    sel.appendChild(other);
    sel.value = 'b'.repeat(32);
    api.rcCleanupDelete();
    await tick();
    const call = fetch.mock.calls.find(c => c[0] === '/api/reportcard/delete-model');
    expect(JSON.parse(call[1].body).agent).toBe('a'.repeat(32));
  });
});

describe('custom-mode model datalist', () => {
  const withModels = (models) => vi.fn(async (url) => ({
    ok: true,
    json: async () => String(url).startsWith('/api/reportcard/models')
      ? { ok: true, models }
      : { ok: true },
  }));

  it('entering custom mode populates the datalist from the host', async () => {
    const { api } = loadModule({ runResponse: { ok: true } });
    vi.stubGlobal('fetch', withModels(['m1', 'owner/m2']));
    document.getElementById('rcMode').value = 'custom';
    api.rcOnModeChange();
    await tick(); await tick();
    const opts = [...document.querySelectorAll('#rcModelOptions option')]
      .map(o => o.value);
    expect(opts).toEqual(['m1', 'owner/m2']);
  });

  it('switching hosts in custom mode refreshes the options', async () => {
    const { api } = loadModule({ runResponse: { ok: true } });
    document.getElementById('rcMode').value = 'custom';
    vi.stubGlobal('fetch', withModels(['first']));
    api.rcOnModeChange();
    await tick(); await tick();
    vi.stubGlobal('fetch', withModels(['second']));
    api.rcOnAgentChange();
    await tick(); await tick();
    const opts = [...document.querySelectorAll('#rcModelOptions option')]
      .map(o => o.value);
    expect(opts).toEqual(['second']);
  });

  it('standard mode does not fetch model options on host change', async () => {
    const { api } = loadModule({ runResponse: { ok: true } });
    const f = withModels(['x']);
    vi.stubGlobal('fetch', f);
    api.rcOnAgentChange();
    await tick();
    const urls = f.mock.calls.map(c => c[0]);
    expect(urls.some(u => String(u).startsWith('/api/reportcard/models')))
      .toBe(false);
  });

  it('the input widens to fit the longest offered id', async () => {
    const { api } = loadModule({ runResponse: { ok: true } });
    const long = 'Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M';
    vi.stubGlobal('fetch', withModels(['m1', long]));
    document.getElementById('rcMode').value = 'custom';
    api.rcOnModeChange();
    await tick(); await tick();
    const w = document.getElementById('rcCustomModel').style.width;
    expect(w).toBe(Math.max(long.length + 2, 28) + 'ch');
  });
});


// #888: the shared run gate — Report Card drives a running server, so it
// contends for the same GPU as the agent-side tools.
function stubGate() {
  const slots = [];
  const state = { busy: null, queued: null };
  vi.stubGlobal('toolsGateRefusal',
    (t) => /in progress|already running/i.test(String(t || '')));
  vi.stubGlobal('toolsSetQueued',
    (id, w) => { state.queued = w ? [id, w] : null; });
  vi.stubGlobal('toolsQueueSlot', (id, opts) => {
    const s = {
      id, pending: null,
      busy: () => state.busy,
      queued: () => !!s.pending,
      waitFor: () => (s.pending ? s.pending.waitFor : null),
      queue(payload, waitFor) {
        const b = state.busy;
        s.pending = { payload,
          waitFor: waitFor || (b ? `${b.label} on ${b.host}` : 'the run in progress') };
        s.sync();
      },
      drop() { if (!s.pending) return false; s.pending = null; s.sync(); return true; },
      fire() { const p = s.pending.payload; s.pending = null; s.sync(); return opts.start(p); },
      sync() {
        globalThis.toolsSetQueued(id, s.pending ? s.pending.waitFor : null);
        if (opts.render) {
          opts.render({ queued: !!s.pending,
            waitFor: s.pending ? s.pending.waitFor : null, busy: s.busy() });
        }
      },
    };
    slots.push(s);
    return s;
  });
  return { slots, state };
}

describe('report card queueing behind another tool (#888)', () => {
  it('queues instead of starting while a Benchmark holds the same host', async () => {
    const { api, sources } = loadModule({ runResponse: { ok: true, job_id: 'j1' } });
    const gate = stubGate();
    gate.state.busy = { tool: 'benchmark', label: 'Benchmark', host: 'gpu-01' };
    api.rcRun();
    await tick(); await tick();
    expect(sources).toHaveLength(0);
    expect(fetch.mock.calls.map(c => c[0])).not.toContain('/api/reportcard/run');
    expect(gate.slots[0].queued()).toBe(true);
    expect(gate.state.queued).toEqual(['reportcard', 'Benchmark on gpu-01']);
    expect(document.getElementById('rcRunBtn').textContent).toContain('Queued');
    expect(document.getElementById('rcCancelBtn').textContent).toContain('Drop queued run');
    expect(document.getElementById('rcNote').textContent)
      .toContain('Queued behind Benchmark on gpu-01');
  });

  it('starts the queued card by itself once the gate clears', async () => {
    const { api, sources } = loadModule({ runResponse: { ok: true, job_id: 'j7' } });
    const gate = stubGate();
    gate.state.busy = { tool: 'benchmark', label: 'Benchmark', host: 'gpu-01' };
    api.rcRun();
    await tick(); await tick();
    gate.state.busy = null;
    gate.slots[0].fire();
    await tick(); await tick();
    expect(sources).toHaveLength(1);
    expect(sources[0].url).toContain('/api/reportcard/stream/j7');
  });

  it('drops a queued card on Cancel and never posts a cancel for it', async () => {
    const { api } = loadModule({ runResponse: { ok: true, job_id: 'j1' } });
    const gate = stubGate();
    gate.state.busy = { tool: 'autotune', label: 'Autotune', host: 'gpu-01' };
    api.rcRun();
    await tick(); await tick();
    api.rcCancelRun();
    expect(gate.slots[0].queued()).toBe(false);
    expect(fetch.mock.calls.map(c => c[0]).join(' ')).not.toContain('/api/reportcard/cancel');
    expect(document.getElementById('rcNote').textContent).toBe('Queued run dropped.');
  });

  it('labels the button Queue while the host is busy and nothing is pending', async () => {
    const { api } = loadModule({ runResponse: { ok: true, job_id: 'j1' } });
    const gate = stubGate();
    gate.state.busy = { tool: 'autotune', label: 'Autotune', host: 'gpu-01' };
    api.rcRun();
    await tick();
    gate.slots[0].drop();
    expect(document.getElementById('rcRunBtn').textContent).toContain('Queue report card');
    expect(document.getElementById('rcNote').textContent)
      .toContain('Autotune is running on gpu-01');
  });

  it('queues rather than losing the card when the run is refused', async () => {
    const { api, sources } = loadModule({
      runResponse: { error: 'a benchmark is already in progress' }, httpOk: false });
    const gate = stubGate();
    api.rcRun();
    await tick(); await tick();
    expect(sources).toHaveLength(0);
    expect(gate.slots[0].queued()).toBe(true);
    expect(gate.slots[0].waitFor()).toBe('the run in progress');
    expect(document.getElementById('rcRunBtn').disabled).toBe(false);
  });
});
