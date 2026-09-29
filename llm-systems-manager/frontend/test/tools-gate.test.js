// #888: the shared run gate — one answer to "is this provider/agent busy?",
// and one queue slot per tool that follows the server's tool_run jobs (#897).
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const BODY = `
  <span id="toolsRunDot"></span>
  <div id="toolsHome"><div id="toolsLauncher"></div></div>
  <div id="toolsLedgerBody"></div>
`;

const STUBS = `
  window.layout = { toolsView: 'card' };
  window.saveLayout = function () {};
  window._claim = function () { return true; };
  window._release = function () {};
`;

const AGENTS = {
  llama: [{ agent_id: 'a1', hostname: 'gpu-01', is_default: true },
          { agent_id: 'a2', hostname: 'gpu-02' }],
  vllm: [{ agent_id: 'a2', hostname: 'gpu-02', is_default: true }],
};

// activity: the /api/tools/activity body, swappable at runtime via __activity.
function boot(activity, bootstrap = '') {
  const win = runHarness({
    sources: [STUBS, srcFile('js/lib/modelcards.js'),
              srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
    bodyHtml: BODY,
    bootstrap: `
      window.__activity = ${JSON.stringify(activity)};
      window._fetchT = (url) => Promise.resolve({
        ok: true, json: () => Promise.resolve(
          url.indexOf('/api/tools/activity') === 0 ? window.__activity
          : url.indexOf('/api/agents/list-by-provider') === 0 ? ${JSON.stringify(AGENTS)}
          : {}),
      });
      initToolsTab();
      window.__done = toolsPollActivity().then(() => { ${bootstrap} });
    `,
  });
  return win.__done.then(() => flush()).then(() => flush()).then(() => win);
}

const BENCH_ON_A1 = { reportcard: false, benchmark: true, autotune: false,
                      agents: { a1: ['benchmark'] }, queue: {} };
const IDLE = { reportcard: false, benchmark: false, autotune: false, agents: {}, queue: {} };

describe('toolsGateBusy scope (#888)', () => {
  it('reports the running tool for the agent that is actually busy', async () => {
    const win = await boot(BENCH_ON_A1);
    const b = win.toolsGateBusy('llama', 'a1');
    expect(b).toBeTruthy();
    expect(b.tool).toBe('benchmark');
    expect(b.label).toBe('Benchmark');
    expect(b.host).toBe('gpu-01');
  });

  it('leaves a different host on the same provider free', async () => {
    const win = await boot(BENCH_ON_A1);
    expect(win.toolsGateBusy('llama', 'a2')).toBe(null);
  });

  it('falls back to the provider’s primary agent when none is named', async () => {
    const win = await boot(BENCH_ON_A1);
    expect(win.toolsGateBusy('llama')).toBeTruthy();
    // vllm's primary is a2, which is idle.
    expect(win.toolsGateBusy('vllm')).toBe(null);
  });

  it('is idle when nothing runs anywhere', async () => {
    const win = await boot(IDLE);
    expect(win.toolsGateBusy('llama', 'a1')).toBe(null);
  });

  it('names a report card as the busy tool', async () => {
    const win = await boot({ reportcard: true, benchmark: false, autotune: false,
                             agents: { a1: ['reportcard'] } });
    expect(win.toolsGateBusy('llama', 'a1').label).toBe('Report Card');
  });
});

describe('toolsQueueSlot reads the gate (#888 → #897)', () => {
  it('names the busy tool for the slot’s target', async () => {
    const win = await boot(BENCH_ON_A1, `
      window.__slot = toolsQueueSlot('autotune', { provider: () => 'llama' });
    `);
    expect(win.__slot.busy().label).toBe('Benchmark');
    expect(win.__slot.waitFor()).toBe('Benchmark on gpu-01');
    expect(win.__slot.queued()).toBe(false);
  });
});

describe('launcher tiles with queued runs (#897)', () => {
  const launcher = (win) => win.document.getElementById('toolsLauncher').innerHTML;

  it('shows the queued count while the server holds runs for a tool', async () => {
    const win = await boot({ ...BENCH_ON_A1, queue: { a1: [{ job_id: 'j1', tool: 'autotune', model_id: 'org/m', user: 'bob', status: 'queued', created: 1 }] } });
    expect(launcher(win)).toContain('1 queued');
  });

  it('goes back to Ready once the queue is empty', async () => {
    const win = await boot(BENCH_ON_A1);
    expect(launcher(win)).not.toContain('queued');
  });
});

// A held job keeps the host it was queued against, whatever the picker says later.
describe('a held job keeps its own target (#897)', () => {
  const PICKER_BODY = BODY + '<select id="tAgent"><option value="a1">gpu-01</option>'
    + '<option value="a2">gpu-02</option></select>';

  function pickerBoot(activity) {
    const win = runHarness({
      sources: [STUBS, srcFile('js/lib/modelcards.js'),
                srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
      bodyHtml: PICKER_BODY,
      bootstrap: `
        window._me = { username: 'alice' };
        window.__activity = ${JSON.stringify(activity)};
        window._fetchT = (url, opts) => Promise.resolve({
          ok: true, json: () => Promise.resolve(
            url.indexOf('/api/tools/activity') === 0 ? window.__activity
            : url.indexOf('/api/agents/list-by-provider') === 0 ? ${JSON.stringify(AGENTS)}
            : {}),
        });
        initToolsTab();
        window.__attached = []; window.__dropped = 0;
        window.__done = toolsPollActivity().then(() => {
          window.__slot = toolsQueueSlot('reportcard', {
            provider: () => 'llama',
            agent: () => document.getElementById('tAgent').value,
            attach: (r) => window.__attached.push(r),
            dropped: () => { window.__dropped++; },
          });
        });
      `,
    });
    return win.__done.then(() => flush()).then(() => flush()).then(() => win);
  }

  it('reads the picker for the busy answer', async () => {
    const win = await pickerBoot({ reportcard: false, benchmark: true, autotune: false,
                                   agents: { a2: ['benchmark'] }, queue: {} });
    expect(win.__slot.busy()).toBe(null);
    win.document.getElementById('tAgent').value = 'a2';
    expect(win.__slot.busy().host).toBe('gpu-02');
  });

  const IDLE_Q = (q) => ({ reportcard: false, benchmark: false, autotune: false, agents: {}, queue: q });
  const rcRow = (status) => ({ job_id: 'j1', tool: 'reportcard', provider: 'llama', model_id: 'org/m', user: 'alice', status, created: 1 });
  async function heldThenRepointed() {
    const win = await pickerBoot(IDLE_Q({}));
    win.__slot.hold('j1', { position: 1 });
    win.document.getElementById('tAgent').value = 'a2';
    return win;
  }
  async function pollWith(win, activity) {
    win.__activity = activity;
    await win.toolsPollActivity(); await flush(); await flush();
  }

  it('attaches off the host it held, after the picker moves', async () => {
    const win = await heldThenRepointed();
    await pollWith(win, IDLE_Q({ a1: [rcRow('running')] }));
    expect(win.__attached).toHaveLength(1);
    expect(win.__dropped).toBe(0);
  });

  it('stays queued on the host it held, after the picker moves', async () => {
    const win = await pickerBoot(IDLE_Q({}));
    win.__slot.hold('j1', { position: 1 });
    await pollWith(win, IDLE_Q({ a1: [rcRow('queued')] }));
    win.document.getElementById('tAgent').value = 'a2';
    await pollWith(win, IDLE_Q({ a1: [rcRow('queued')], a2: [] }));
    expect(win.__slot.queued()).toBe(true);
    expect(win.__dropped).toBe(0);
  });
});

// #887: an unresolved agent is not an idle agent.
describe('the gate before the agent list resolves (#887)', () => {
  function unresolvedBoot() {
    const win = runHarness({
      sources: [STUBS, srcFile('js/lib/modelcards.js'),
                srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
      bodyHtml: BODY,
      bootstrap: `
        window.__resolve = null;
        window._fetchT = (url) => (url.indexOf('/api/agents/list-by-provider') === 0
          ? new Promise((r) => { window.__resolve = () => r({
              ok: true, json: () => Promise.resolve(${JSON.stringify(AGENTS)}) }); })
          : Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));
      `,
    });
    return win;
  }

  it('is busy, not free, while the primary agent is unknown', () => {
    const win = unresolvedBoot();
    const b = win.toolsGateBusy('llama');
    expect(b).toBeTruthy();
    expect(b.unresolved).toBe(true);
  });
});

// A failed agent-list fetch must not open the gate.
describe('the gate when the agent list fails to load', () => {
  it('stays closed instead of reading every host as idle', async () => {
    const win = runHarness({
      sources: [STUBS, srcFile('js/lib/modelcards.js'),
                srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
      bodyHtml: BODY,
      bootstrap: `
        window._fetchT = (url) => (url.indexOf('/api/agents/list-by-provider') === 0
          ? Promise.reject(new Error('down'))
          : Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));
      `,
    });
    win.toolsGateBusy('llama');
    for (let i = 0; i < 4; i++) await flush();
    const b = win.toolsGateBusy('llama');
    expect(b).toBeTruthy();
    expect(b.unresolved).toBe(true);
  });
});

// #887: the quality guard has its own name in another dashboard's wait text.
describe('remote quality runs are named (#887)', () => {
  it('names the Quality guard rather than Autotune', async () => {
    const win = await boot({ reportcard: false, benchmark: false, autotune: false,
                             quality: true, agents: { a1: ['quality'] } });
    expect(win.toolsGateBusy('llama', 'a1').label).toBe('Quality guard');
  });
});
