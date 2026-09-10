// #888: the shared run gate — one answer to "is this provider/agent busy?",
// and one queue slot per tool that starts itself when the host frees up.
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
                      agents: { a1: ['benchmark'] } };
const IDLE = { reportcard: false, benchmark: false, autotune: false, agents: {} };

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

describe('toolsQueueSlot (#888)', () => {
  const slotBoot = (activity) => boot(activity, `
    window.__started = [];
    window.__rendered = [];
    window.__slot = toolsQueueSlot('autotune', {
      provider: () => 'llama',
      start: (p) => window.__started.push(p),
      render: (st) => window.__rendered.push(st),
    });
  `);

  it('holds a run while the host is busy and starts it when the gate clears', async () => {
    const win = await slotBoot(BENCH_ON_A1);
    expect(win.__slot.busy().label).toBe('Benchmark');
    win.__slot.queue({ model: 'm1' });
    expect(win.__slot.queued()).toBe(true);
    expect(win.__slot.waitFor()).toBe('Benchmark on gpu-01');
    expect(win.__started).toEqual([]);
    win.__activity = IDLE;
    await win.toolsPollActivity();
    await flush();
    expect(win.__started).toEqual([{ model: 'm1' }]);
    expect(win.__slot.queued()).toBe(false);
  });

  it('drops a queued run, which then never starts', async () => {
    const win = await slotBoot(BENCH_ON_A1);
    win.__slot.queue({ model: 'm1' });
    expect(win.__slot.drop()).toBe(true);
    expect(win.__slot.queued()).toBe(false);
    win.__activity = IDLE;
    await win.toolsPollActivity();
    await flush();
    expect(win.__started).toEqual([]);
    expect(win.__slot.drop()).toBe(false);
  });

  it('keeps only one pending run per tool', async () => {
    const win = await slotBoot(BENCH_ON_A1);
    win.__slot.queue({ model: 'm1' });
    win.__slot.queue({ model: 'm2' });
    win.__activity = IDLE;
    await win.toolsPollActivity();
    await flush();
    expect(win.__started).toEqual([{ model: 'm2' }]);
  });

  it('tells the module what it is waiting for on every gate change', async () => {
    const win = await slotBoot(BENCH_ON_A1);
    win.__slot.queue({ model: 'm1' });
    const last = win.__rendered[win.__rendered.length - 1];
    expect(last.queued).toBe(true);
    expect(last.waitFor).toBe('Benchmark on gpu-01');
    expect(last.busy.tool).toBe('benchmark');
  });
});

describe('launcher tiles with a pending run (#888)', () => {
  const launcher = (win) => win.document.getElementById('toolsLauncher').innerHTML;

  it('shows Queued instead of Ready while a run is pending', async () => {
    const win = await boot(BENCH_ON_A1, `
      window.__slot = toolsQueueSlot('autotune', { provider: () => 'llama', start: () => {} });
      window.__slot.queue({});
    `);
    expect(launcher(win)).toContain('Queued');
    expect(launcher(win)).toContain('queued behind Benchmark on gpu-01');
  });

  it('goes back to Ready once the queued run is dropped', async () => {
    const win = await boot(BENCH_ON_A1, `
      window.__slot = toolsQueueSlot('autotune', { provider: () => 'llama', start: () => {} });
      window.__slot.queue({});
      window.__slot.drop();
    `);
    expect(launcher(win)).not.toContain('Queued');
  });

  it('marks the Benchmark tile from the offline mode’s own slot key', async () => {
    const win = await boot(IDLE, `
      window.toolsSetQueued('benchmark:offline', 'Autotune on gpu-01');
    `);
    expect(launcher(win)).toContain('Queued');
  });
});

describe('toolsGateRefusal (#888)', () => {
  it('recognises the agent’s busy-lock refusal text', async () => {
    const win = await boot(IDLE);
    expect(win.toolsGateRefusal('a benchmark is already in progress')).toBe(true);
    expect(win.toolsGateRefusal('autotune already running')).toBe(true);
    expect(win.toolsGateRefusal('model not found')).toBe(false);
    expect(win.toolsGateRefusal(null)).toBe(false);
  });
});


// #887: a queued run keeps the host it was queued against, whatever the
// picker says later — the payload the pending POST carries is frozen too.
describe('a pending run keeps its own target (#887)', () => {
  const PICKER_BODY = BODY + '<select id="tAgent"><option value="a1">gpu-01</option>'
    + '<option value="a2">gpu-02</option></select>';

  // A slot whose provider/agent read live DOM values, exactly as Report Card's do.
  function pickerBoot(activity) {
    const win = runHarness({
      sources: [STUBS, srcFile('js/lib/modelcards.js'),
                srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
      bodyHtml: PICKER_BODY,
      bootstrap: `
        window.__activity = ${JSON.stringify(activity)};
        window._fetchT = (url) => Promise.resolve({
          ok: true, json: () => Promise.resolve(
            url.indexOf('/api/tools/activity') === 0 ? window.__activity
            : url.indexOf('/api/agents/list-by-provider') === 0 ? ${JSON.stringify(AGENTS)}
            : {}),
        });
        initToolsTab();
        window.__started = [];
        window.__done = toolsPollActivity().then(() => {
          window.__slot = toolsQueueSlot('reportcard', {
            provider: () => 'llama',
            agent: () => document.getElementById('tAgent').value,
            start: (p) => window.__started.push(p),
          });
        });
      `,
    });
    return win.__done.then(() => flush()).then(() => flush()).then(() => win);
  }

  it('does not start when the picker moves to an idle host', async () => {
    const win = await pickerBoot(BENCH_ON_A1);
    win.__slot.queue({ agent: 'a1' });
    expect(win.__slot.waitFor()).toBe('Benchmark on gpu-01');
    // The operator repoints the picker at the idle host; the queued POST still
    // names a1, so the gate must keep watching a1.
    win.document.getElementById('tAgent').value = 'a2';
    win.__activity = { reportcard: false, benchmark: true, autotune: false,
                       agents: { a1: ['benchmark'] } };
    await win.toolsPollActivity();
    await flush();
    expect(win.__started).toEqual([]);
    expect(win.__slot.busy().agent_id).toBe('a1');
  });

  it('starts when the host it was queued against goes idle', async () => {
    const win = await pickerBoot(BENCH_ON_A1);
    win.__slot.queue({ agent: 'a1' });
    win.document.getElementById('tAgent').value = 'a2';
    win.__activity = IDLE;
    await win.toolsPollActivity();
    await flush();
    expect(win.__started).toEqual([{ agent: 'a1' }]);
  });

  it('reads the picker again for the next run once nothing is pending', async () => {
    const win = await pickerBoot({ reportcard: false, benchmark: true, autotune: false,
                                   agents: { a2: ['benchmark'] } });
    expect(win.__slot.busy()).toBe(null);        // picker is on the idle a1
    win.document.getElementById('tAgent').value = 'a2';
    expect(win.__slot.busy().host).toBe('gpu-02');
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
        window.__started = [];
        window.__slot = toolsQueueSlot('autotune', {
          provider: () => 'llama', start: (p) => window.__started.push(p) });
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

  it('starts the held run as soon as the list arrives idle', async () => {
    const win = unresolvedBoot();
    win.__slot.queue({ m: 1 });
    expect(win.__started).toEqual([]);
    win.__resolve();
    await flush(); await flush(); await flush();
    expect(win.__started).toEqual([{ m: 1 }]);
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
