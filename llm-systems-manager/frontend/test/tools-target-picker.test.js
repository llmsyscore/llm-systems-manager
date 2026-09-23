// #916: the Benchmark / Autotune modules drive a picked host — llama.cpp by default,
// any approved LM Studio host on demand — and the picker only shows when one exists.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const BODY = `
  <span id="toolsRunDot"></span>
  <div id="toolsHome"><div id="toolsLauncher"></div></div>
  <div id="toolsLedgerBody"></div>
  <div id="toolsModBench" style="display:none;"><select id="toolsTargetBench" class="tools-target" style="display:none;"></select></div>
  <div id="toolsModAt" style="display:none;"><select id="toolsTargetAt" class="tools-target" style="display:none;"></select></div>
`;

function stubs(byProvider) {
  return `
  window.layout = { toolsView: 'card' };
  window.saveLayout = function () {};
  window._claim = function () { return true; };
  window._release = function () {};
  window.__agents = ${JSON.stringify(byProvider)};
  window._fetchT = (u) => Promise.resolve({ ok: true, json: () => Promise.resolve(String(u).indexOf('list-by-provider') >= 0 ? window.__agents : {}) });
  window.fetch = window._fetchT;
  window.__opens = [];
  window.BL = { live: false, running() { return this.live; }, detach() {}, onOpen(m, o) { window.__opens.push(['bench', m, o]); } };
  window.AT = { live: false, running() { return this.live; }, detach() {}, onOpen(m, o) { window.__opens.push(['at', m, o]); } };
  `;
}

function boot(byProvider) {
  return runHarness({
    sources: [stubs(byProvider), srcFile('js/lib/modelcards.js'), srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
    bodyHtml: BODY,
    bootstrap: 'initToolsTab();',
  });
}

const LLAMA_ONLY = { llama: [{ agent_id: 'L1', hostname: 'gpu-01', is_default: true, online: true }], lms: [], vllm: [] };
const MIXED = { llama: [{ agent_id: 'L1', hostname: 'gpu-01', is_default: true, online: true }],
                lms: [{ agent_id: 'M1', hostname: 'mac-01', is_default: true, online: true },
                      { agent_id: 'M2', hostname: 'mac-02', is_default: false, online: true }], vllm: [] };

describe('tool target picker (#916)', () => {
  it('defaults to llama with no query string and stays hidden without an LM Studio host', async () => {
    const win = boot(LLAMA_ONLY);
    await flush();
    expect(win.toolsTarget()).toEqual({ provider: 'llama', agent: null });
    expect(win.toolsTargetQs()).toBe('');
    expect(win.toolsUrl('/api/benchmark/live/preflight')).toBe('/api/benchmark/live/preflight');
    expect(win.document.getElementById('toolsTargetBench').style.display).toBe('none');
  });

  it('lists the default llama host plus every LM Studio host and carries the pick into the URLs', async () => {
    const win = boot(MIXED);
    await flush();
    const sel = win.document.getElementById('toolsTargetBench');
    expect(sel.style.display).toBe('');
    expect([...sel.options].map(o => o.value)).toEqual(['llama|', 'lms|M1', 'lms|M2']);
    expect([...sel.options].map(o => o.textContent)).toEqual(['llama.cpp · gpu-01', 'LM Studio · mac-01', 'LM Studio · mac-02']);
    win.toolsSetTarget('lms', 'M2');
    expect(win.toolsTarget()).toEqual({ provider: 'lms', agent: 'M2' });
    expect(win.toolsUrl('/api/benchmark/live/preflight')).toBe('/api/benchmark/live/preflight?provider=lms&agent=M2');
    expect(win.toolsUrl('/api/benchmark/live/runs?model_id=m')).toBe('/api/benchmark/live/runs?model_id=m&provider=lms&agent=M2');
    // both module heads mirror the pick
    expect(win.document.getElementById('toolsTargetAt').value).toBe('lms|M2');
    // an LM Studio pick without an agent falls back to the default LM Studio host
    win.toolsSetTarget('lms', null);
    expect(win.toolsTarget().agent).toBe('M1');
    // llama never carries an agent from the picker
    win.toolsSetTarget('llama', 'L1');
    expect(win.toolsTarget()).toEqual({ provider: 'llama', agent: null });
  });

  it('changing the picker re-opens the showing module against the new host', async () => {
    const win = boot(MIXED);
    await flush();
    win.toolsOpenTool('benchmark', null);
    win.__opens.length = 0;
    const sel = win.document.getElementById('toolsTargetBench');
    sel.value = 'lms|M1';
    sel.dispatchEvent(new win.Event('change'));
    expect(win.toolsTarget()).toEqual({ provider: 'lms', agent: 'M1' });
    expect(win.__opens).toEqual([['bench', undefined, { retarget: true }]]);
    // a live run keeps its host: no re-open while running
    win.BL.live = true;
    win.__opens.length = 0;
    sel.value = 'llama|';
    sel.dispatchEvent(new win.Event('change'));
    expect(win.__opens).toEqual([]);
  });

  it('a deep link with a provider sets the target before the module opens', async () => {
    const win = boot(MIXED);
    await flush();
    win.toolsOpenTool('autotune', 'qwen3.5-9b@q6_k', { provider: 'lms', agent: 'M2' });
    expect(win.toolsTarget()).toEqual({ provider: 'lms', agent: 'M2' });
    expect(win.__opens.at(-1)).toEqual(['at', 'qwen3.5-9b@q6_k', { provider: 'lms', agent: 'M2' }]);
    win.toolsOpenTool('benchmark', 'org/m:Q4', { provider: 'llama' });
    expect(win.toolsTarget()).toEqual({ provider: 'llama', agent: null });
  });

  it('the gate answers for the picked LM Studio host', async () => {
    const win = boot(MIXED);
    await flush();
    win.toolsSetTarget('lms', 'M1');
    expect(win.toolsGateBusy('lms', 'M1')).toBeNull();
  });
});
