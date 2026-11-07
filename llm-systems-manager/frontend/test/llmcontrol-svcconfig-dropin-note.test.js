// #1419: the server-flags editor says where drop-in edits go.
import { describe, test, expect } from 'vitest';
import { srcFile, runHarness } from './helpers/harness.js';

const src = srcFile('js/llmcontrol.js');
const FN = src.slice(src.indexOf('async function openServerConfig'), src.indexOf('function closeSvcConfig'));

function run(resp) {
  return runHarness({
    sources: [`
      var _svcBinary = '', _svcArgs = [];
      function _svcConfigRoute() { return '/x'; }
      async function _jsonOrThrow(r) { return r.json(); }
      function _renderSvcArgs() {}
      window.fetch = () => Promise.resolve({ json: () => Promise.resolve(${JSON.stringify(resp)}) });
      ` + FN],
    bodyHtml: '<div id="svcConfigTitle"></div><div id="svcConfigStatus"></div><div id="svcArgList"></div>'
      + '<div id="svcConfigBinary"></div><div id="svcConfigOverlay"></div>',
    bootstrap: 'openServerConfig("llama");',
  });
}
const flush = () => new Promise(r => setTimeout(r, 0));

describe('openServerConfig drop-in note', () => {
  test('shows the note for a drop-in source', async () => {
    const w = run({ ok: true, binary: '/b', args: [], source: 'drop-in' });
    await flush(); await flush();
    expect(w.document.getElementById('svcConfigStatus').textContent)
      .toBe("Edits go to a small extra settings file; the package's service file is left alone.");
  });
  test('shows nothing for a plain unit', async () => {
    const w = run({ ok: true, binary: '/b', args: [], source: 'unit' });
    await flush(); await flush();
    expect(w.document.getElementById('svcConfigStatus').textContent).toBe('');
  });
});
