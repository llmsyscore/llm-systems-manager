// #888: Autotune and the Quality guard share one SSE endpoint, so switching
// tools must close the outgoing module's stream without cancelling the run.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const BODY = `
  <span id="toolsRunDot"></span>
  <div id="toolsHome"><div id="toolsLauncher"></div></div>
  <div id="toolsLedgerBody"></div>
  <div id="toolsModAt" style="display:none;"><span class="ctx-chip" id="toolsChipAutotune" style="display:none;"></span></div>
  <div id="toolsModQg" style="display:none;"><span class="ctx-chip" id="toolsChipQuality" style="display:none;"></span></div>
`;

const STUBS = `
  window.layout = { toolsView: 'card' };
  window.saveLayout = function () {};
  window._claim = function () { return true; };
  window._release = function () {};
  window._fetchT = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
  window.__cancels = 0;
  window.fetch = function (u) { if (String(u).indexOf('/cancel') >= 0) window.__cancels++; return Promise.resolve({ ok: true, json: () => Promise.resolve({}) }); };
  function fakeTool(name) {
    return {
      live: false, opens: 0, detaches: 0,
      running() { return this.live; },
      detach() { this.detaches++; this.live = false; },
      onOpen() { this.opens++; },
    };
  }
  window.AT = fakeTool('at');
  window.QG = fakeTool('qg');
`;

function boot() {
  return runHarness({
    sources: [STUBS, srcFile('js/lib/modelcards.js'), srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
    bodyHtml: BODY,
    bootstrap: 'initToolsTab();',
  });
}

describe('shared autotune stream handoff', () => {
  it('closes the Autotune stream when the Quality guard opens, and cancels nothing', async () => {
    const win = boot();
    await flush();
    win.toolsOpenTool('autotune', 'org/m:Q4');
    win.AT.live = true;
    win.toolsOpenTool('quality', 'org/m:Q4');
    expect(win.AT.detaches).toBe(1);
    expect(win.AT.running()).toBe(false);
    expect(win.QG.detaches).toBe(0);
    expect(win.QG.opens).toBe(1);
    expect(win.__cancels).toBe(0);
  });

  it('closes the Quality-guard stream on the way back to Autotune', async () => {
    const win = boot();
    await flush();
    win.toolsOpenTool('quality', 'org/m:Q4');
    win.QG.live = true;
    win.toolsOpenTool('autotune', 'org/m:Q4');
    expect(win.QG.detaches).toBe(1);
    expect(win.AT.detaches).toBe(0);
    expect(win.__cancels).toBe(0);
  });

  it('re-opening the same tool keeps its stream attached', async () => {
    const win = boot();
    await flush();
    win.toolsOpenTool('autotune', 'org/m:Q4');
    win.AT.live = true;
    win.toolsOpenTool('autotune', 'org/m:Q4');
    expect(win.AT.detaches).toBe(0);
    expect(win.AT.running()).toBe(true);
  });

  it('closing the module back to the launcher closes the stream but never cancels', async () => {
    const win = boot();
    await flush();
    win.toolsOpenTool('autotune', 'org/m:Q4');
    win.AT.live = true;
    win.toolsCloseModule();
    expect(win.AT.detaches).toBe(1);
    expect(win.__cancels).toBe(0);
    expect(win.document.getElementById('toolsHome').style.display).toBe('block');
  });
});
