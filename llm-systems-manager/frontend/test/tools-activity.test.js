// #775: the Tools run indicators follow fleet-wide state, not just this tab.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const BODY = `
  <span id="toolsRunDot"></span>
  <div id="toolsHome"><div id="toolsLauncher"></div></div>
  <div id="toolsLedgerBody"></div>
`;

// tools.js reads these classic-script globals from its sibling modules.
const STUBS = `
  window.layout = { toolsView: 'card' };
  window.saveLayout = function () {};
  window._claim = function () { return true; };
  window._release = function () {};
`;

// activity: the /api/tools/activity body. local: EventSource globals to fake.
function run(activity, { local = '', after = '' } = {}) {
  const win = runHarness({
    sources: [
      STUBS,
      srcFile('js/lib/modelcards.js'),
      srcFile('js/lib/toolcards.js'),
      srcFile('js/tools.js'),
    ],
    bodyHtml: BODY,
    bootstrap: `
      ${local}
      window._fetchT = (url) => Promise.resolve({
        ok: true, json: () => Promise.resolve(
          url.indexOf('/api/tools/activity') === 0 ? ${JSON.stringify(activity)} : {}),
      });
      window.__polls = 0;
      const _origFetchT = window._fetchT;
      window._fetchT = (url) => {
        if (url.indexOf('/api/tools/activity') === 0) window.__polls++;
        return _origFetchT(url);
      };
      initToolsTab();
      window.__done = toolsPollActivity().then(() => { ${after} });
    `,
  });
  return win.__done.then(() => flush()).then(() => win);
}

const launcher = (win) => win.document.getElementById('toolsLauncher').innerHTML;
const dotOn = (win) => win.document.getElementById('toolsRunDot').classList.contains('on');

describe('fleet-wide tool activity', () => {
  it('lights the run dot for a run this browser did not start', async () => {
    const win = await run({ reportcard: false, benchmark: true, autotune: false });
    expect(dotOn(win)).toBe(true);
  });

  it('leaves the dot dark when nothing is running anywhere', async () => {
    const win = await run({ reportcard: false, benchmark: false, autotune: false });
    expect(dotOn(win)).toBe(false);
  });

  it('lights the dot for a remote report card and a remote autotune', async () => {
    const rc = await run({ reportcard: true, benchmark: false, autotune: false });
    expect(dotOn(rc)).toBe(true);
    const at = await run({ reportcard: false, benchmark: false, autotune: true });
    expect(dotOn(at)).toBe(true);
  });
});

describe('launcher tiles under a remote run', () => {
  it('shows a Running pill for a run started elsewhere', async () => {
    const win = await run({ reportcard: false, benchmark: true, autotune: false },
      { after: 'window.__html = document.getElementById("toolsLauncher").innerHTML;' });
    expect(launcher(win)).toContain('Running');
  });

  it('keeps the action "Open", never "View run", without a local stream', async () => {
    const win = await run({ reportcard: false, benchmark: true, autotune: false });
    // "View run" would open a panel with no stream to show and a Run button
    // the agent will refuse — that affordance belongs to the owning tab only.
    expect(launcher(win)).not.toContain('View run');
  });

  it('offers "View run" to the browser that owns the stream', async () => {
    const win = await run({ reportcard: false, benchmark: true, autotune: false },
      { local: 'window._benchEventSrc = { readyState: 1 };' });
    expect(launcher(win)).toContain('View run');
  });

  it('shows Running from a local stream even before any poll lands', async () => {
    const win = await run({ reportcard: false, benchmark: false, autotune: false },
      { local: 'window._benchEventSrc = { readyState: 1 };' });
    expect(dotOn(win)).toBe(true);
    expect(launcher(win)).toContain('Running');
  });

  it('treats a live vLLM benchmark as a running Benchmark tool', async () => {
    const win = await run({ reportcard: false, benchmark: false, autotune: false },
      { local: 'window._vbenchEventSrc = { readyState: 1 };' });
    expect(dotOn(win)).toBe(true);
  });
});

describe('stale-state guards', () => {
  it('re-polls as soon as a local stream closes', async () => {
    const win = await run({ reportcard: false, benchmark: true, autotune: false },
      { local: 'window._benchEventSrc = { readyState: 1 };',
        after: 'window.__before = window.__polls; window._benchEventSrc = null; toolsSyncRunDot();' });
    expect(win.__polls).toBeGreaterThan(win.__before);
  });

  it('survives a failed poll without throwing', async () => {
    const win = runHarness({
      sources: [STUBS, srcFile('js/lib/modelcards.js'),
                srcFile('js/lib/toolcards.js'), srcFile('js/tools.js')],
      bodyHtml: BODY,
      bootstrap: `
        window._fetchT = () => Promise.reject(new Error('offline'));
        window.__done = toolsPollActivity();
      `,
    });
    await win.__done;
    expect(dotOn(win)).toBe(false);
  });
});
