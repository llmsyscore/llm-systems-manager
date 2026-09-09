// #885: Benchmark's "Add to Report Card" deep-links into the Report Card
// module with the model pre-filled, and the context chip can clear it.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const BODY = `
  <span id="toolsRunDot"></span>
  <div id="toolsHome"><div id="toolsLauncher"></div></div>
  <div id="toolsLedgerBody"></div>
  <div id="toolsMod" style="display:none;">
    <span class="ctx-chip" id="toolsChipReportcard" style="display:none;"></span>
  </div>
`;

// tools.js reads these classic-script globals from its sibling modules;
// TC (esc/viewOf/launcher/…) comes from the real libs, as in tools-activity.test.js.
const STUBS = `
  window.layout = { toolsView: 'card' };
  window.saveLayout = function () {};
  window._claim = function () { return true; };
  window._release = function () {};
  window.__initReportCardCalls = [];
  window.initReportCard = function (modelId) { window.__initReportCardCalls.push(modelId); };
  window._fetchT = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
`;

function boot(bootstrap = '') {
  return runHarness({
    sources: [STUBS, srcFile('js/lib/modelcards.js'), srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
    bodyHtml: BODY,
    bootstrap: `initToolsTab();\n${bootstrap}`,
  });
}

describe('Report Card deep link + chip (#885)', () => {
  it('toolsOpenTool passes the model to initReportCard and shows the chip', async () => {
    const win = boot();
    await flush();
    win.toolsOpenTool('reportcard', 'org/m:Q4');
    expect(win.__initReportCardCalls).toEqual(['org/m:Q4']);
    const chip = win.document.getElementById('toolsChipReportcard');
    expect(chip.style.display).toBe('');
    expect(chip.textContent).toContain('m:Q4');
    expect(chip.dataset.model).toBe('org/m:Q4');
  });

  it('the chip ✕ clears the filter by calling initReportCard with no model', async () => {
    const win = boot();
    await flush();
    win.toolsOpenTool('reportcard', 'org/m:Q4');
    win.document.querySelector('#toolsChipReportcard .ctx-chip-x')
      .dispatchEvent(new win.Event('click', { bubbles: true }));
    expect(win.__initReportCardCalls[win.__initReportCardCalls.length - 1]).toBeUndefined();
    expect(win.document.getElementById('toolsChipReportcard').style.display).toBe('none');
  });
});
