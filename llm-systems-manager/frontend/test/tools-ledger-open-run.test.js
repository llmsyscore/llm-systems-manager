// #1162: a past live-benchmark row in the ledger opens Benchmark on that stored run.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const LIVE = { tool: 'benchmark', model_id: 'org/m:Q4', agent_id: 'a1', provider: 'llama', ok: true, run_id: 'p1',
  ts: '2026-09-30T04:43:00Z', summary: { gen_tps: 69, bench_tool: 'speed-bench' } };
const OFFLINE = { ...LIVE, run_id: 'o1', ts: '2026-09-29T04:43:00Z', summary: { gen_tps: 40, bench_tool: 'llama-bench' } };
const LMS = { ...LIVE, run_id: 'p2', agent_id: 'm1', provider: 'lms', ts: '2026-09-28T04:43:00Z' };
const ELSEWHERE = { ...LIVE, run_id: 'p3', agent_id: 'a2', ts: '2026-09-27T04:43:00Z' };

const BODY = `
  <span id="toolsRunDot"></span>
  <div id="toolsHome"><div id="toolsLauncher"></div></div>
  <div id="toolsLedgerSec"><div id="toolsLedgerBody"></div><div id="toolsLedgerPager"></div><div id="toolsLedgerDiff"></div></div>
  <div id="toolsModBench" style="display:none;"></div>
`;

function boot(runs) {
  const stubs = `
    window.layout = { toolsView: 'card' };
    window.saveLayout = function () {};
    window._claim = function () { return true; };
    window._release = function () {};
    window.__opens = [];
    window.BL = { running: () => false, onOpen: (model, opts) => { window.__opens.push([model, opts, toolsTarget()]); } };
    window._fetchT = (u) => Promise.resolve({ ok: true, json: () => Promise.resolve(
      String(u).indexOf('/api/tools/runs') >= 0 ? { runs: ${JSON.stringify(runs)}, totals: {}, latest: {} }
      : String(u).indexOf('list-by-provider') >= 0 ? { llama: [{ agent_id: 'a1', hostname: 'loki', is_default: true }, { agent_id: 'a2', hostname: 'thor' }],
          lms: [{ agent_id: 'm1', hostname: 'mac', is_default: true }] } : {}) });
  `;
  return runHarness({
    sources: [stubs, srcFile('js/lib/modelcards.js'), srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
    bodyHtml: BODY,
    bootstrap: 'initToolsTab();',
  });
}

const click = (win, el) => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
const rowOf = (win, run) => win.document.querySelector(`tr[data-run="${run}"]`);

describe('ledger opens a past live run (#1162)', () => {
  it('a live-benchmark row carries its run and opens the Live view on it', async () => {
    const win = boot([LIVE, OFFLINE]);
    await flush(); await flush();
    const tr = rowOf(win, 'p1');
    expect(tr).not.toBeNull();
    expect(tr.getAttribute('title')).toBe("Open this run's results");
    click(win, tr.querySelector('td.tool'));
    expect(win.__opens).toHaveLength(1);
    const [model, opts] = win.__opens[0];
    expect(model).toBe('org/m:Q4');
    expect(opts).toMatchObject({ provider: 'llama', run: 'p1', mode: 'live' });
    expect(win.document.getElementById('toolsModBench').style.display).toBe('block');
  });

  it('an offline row opens Benchmark without a stored run', async () => {
    const win = boot([LIVE, OFFLINE]);
    await flush(); await flush();
    expect(rowOf(win, 'o1')).toBeNull();
    const tr = Array.from(win.document.querySelectorAll('tr.rowlink')).find(r => !r.dataset.run);
    expect(tr.getAttribute('title')).toBe('Open Benchmark');
    click(win, tr.querySelector('td.tool'));
    expect(win.__opens[0][1].run).toBeUndefined();
  });

  it('an LM Studio row targets its own host; a row from another llama host stays inert', async () => {
    const win = boot([LMS, ELSEWHERE]);
    await flush(); await flush();
    expect(rowOf(win, 'p3')).toBeNull();
    click(win, rowOf(win, 'p2').querySelector('td.tool'));
    const [, opts, target] = win.__opens[0];
    expect(opts).toMatchObject({ provider: 'lms', agent: 'm1', run: 'p2' });
    expect(target).toEqual({ provider: 'lms', agent: 'm1' });
    expect(win.toolsHostName('m1')).toBe('mac');
  });
});
