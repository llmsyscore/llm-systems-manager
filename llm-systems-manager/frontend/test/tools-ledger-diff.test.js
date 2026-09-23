// #892: pick two ledger runs of one model and tool, then compare switches and results.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';
import TC from '../js/lib/toolcards.js';

const A = { tool: 'benchmark', model_id: 'org/m:Q4', agent_id: 'a1', provider: 'llama', ok: true,
  ts: '2026-09-20T10:00:00Z', summary: { gen_tps: 40, ppt_tps: 900, bench_tool: 'llama-bench',
    switches: { '-ngl': '99', '-fa': 'on' } } };
const B = { tool: 'benchmark', model_id: 'org/m:Q4', agent_id: 'a1', provider: 'llama', ok: true,
  ts: '2026-09-21T10:00:00Z', summary: { gen_tps: 50, ppt_tps: 850, bench_tool: 'llama-bench',
    switches: { '-ngl': '99', '-ub': '512' } } };
const OTHER = { ...B, model_id: 'org/other', ts: '2026-09-22T10:00:00Z' };
const TUNE = { ...A, tool: 'autotune', ts: '2026-09-19T10:00:00Z', summary: { decode_tps: 30 } };

describe('TC.diffRuns', () => {
  it('orders the older run as A and marks changed switches', () => {
    const d = TC.diffRuns(B, A);
    expect(d.a.ts).toBe(A.ts);
    const byKey = Object.fromEntries(d.switches.map(r => [r.label, r]));
    expect(byKey['-ngl'].changed).toBe(false);
    expect(byKey['-fa']).toMatchObject({ a: 'on', b: null, changed: true });
    expect(byKey['-ub']).toMatchObject({ a: null, b: '512', changed: true });
  });

  it('tones result deltas by direction', () => {
    const d = TC.diffRuns(A, B);
    const gen = d.results.find(r => r.label === 'Gen t/s');
    expect(gen).toMatchObject({ a: '40.0', b: '50.0', tone: 'good' });
    expect(Math.round(gen.pct)).toBe(25);
    expect(d.results.find(r => r.label === 'Prompt t/s').tone).toBe('bad');
  });

  it('notes runs that predate switch recording', () => {
    const old = { ...A, summary: { gen_tps: 40 } };
    const d = TC.diffRuns(old, B);
    expect(d.recorded).toEqual({ a: false, b: true });
    expect(TC.diffHtml(d)).toContain('Run A predates switch recording.');
    const none = TC.diffHtml(TC.diffRuns(old, { ...B, summary: { gen_tps: 50 } }));
    expect(none).toContain('Neither run recorded its switches.');
  });

  it('escapes switch keys and values', () => {
    const x = { ...B, summary: { switches: { '<k>': '"v"' } } };
    const html = TC.diffHtml(TC.diffRuns(A, x));
    expect(html).toContain('&lt;k&gt;');
    expect(html).toContain('&quot;v&quot;');
    expect(html).not.toContain('<k>');
  });
});

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
    window._fetchT = (u) => Promise.resolve({ ok: true, json: () => Promise.resolve(
      String(u).indexOf('/api/tools/runs') >= 0 ? { runs: ${JSON.stringify(runs)}, totals: {}, latest: {} }
      : String(u).indexOf('list-by-provider') >= 0 ? { llama: [{ agent_id: 'a1', hostname: 'loki', is_default: true }] } : {}) });
  `;
  return runHarness({
    sources: [stubs, srcFile('js/lib/modelcards.js'), srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
    bodyHtml: BODY,
    bootstrap: 'initToolsTab();',
  });
}

const click = (win, el) => el.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
const picks = win => Array.from(win.document.querySelectorAll('button[data-pick]'));

describe('ledger compare picking', () => {
  it('offers picks on Benchmark/Autotune rows only and gates to one model + tool', async () => {
    const win = boot([OTHER, B, A, TUNE]);
    await flush(); await flush();
    expect(picks(win)).toHaveLength(4);
    const byModelTool = el => el.dataset.pick.split('|');
    click(win, picks(win).find(el => byModelTool(el)[0] === 'benchmark' && byModelTool(el)[3] === A.ts));
    const diff = win.document.getElementById('toolsLedgerDiff');
    expect(diff.textContent).toContain('Pick one more Benchmark run');
    expect(win.document.querySelector('tr.rowlink')).not.toBeNull();
    expect(win.document.getElementById('toolsHome').style.display).not.toBe('none');
    const state = picks(win).map(el => [byModelTool(el)[2], byModelTool(el)[0], el.disabled]);
    expect(state).toContainEqual(['org/other', 'benchmark', true]);
    expect(state).toContainEqual(['org/m:Q4', 'autotune', true]);
    expect(state).toContainEqual(['org/m:Q4', 'benchmark', false]);
    click(win, picks(win).find(el => byModelTool(el)[3] === B.ts));
    expect(diff.textContent).toContain('Compare · Benchmark · org/m:Q4');
    expect(diff.textContent).toContain('2 of 3 differ');
    expect(diff.textContent).toContain('loki');
    expect(picks(win).filter(el => el.getAttribute('aria-pressed') === 'true')).toHaveLength(2);
    expect(picks(win).filter(el => !el.classList.contains('on')).every(el => el.disabled)).toBe(true);
    click(win, diff.querySelector('[data-diff="clear"]'));
    expect(diff.innerHTML).toBe('');
    expect(picks(win).some(el => el.disabled)).toBe(false);
  });
});
