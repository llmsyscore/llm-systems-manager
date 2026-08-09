// DOM contract for the companion shell: element-id parity + classic-script
// co-load. Guards the failure modes the transform unit tests can't see —
// a typo'd getElementById (silent blank / null crash in a poll interval) and
// a top-level name collision across the co-loaded classic scripts.
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');
const html = read('companion.html');
const js = read('js/companion.js');
const htmlIds = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));

describe('companion DOM contract', () => {
  it('every literal element id companion.js references exists in companion.html', () => {
    const refs = new Set();
    for (const m of js.matchAll(/\$\('([^']+)'\)/g)) refs.add(m[1]);
    for (const m of js.matchAll(/getElementById\('([^']+)'\)/g)) refs.add(m[1]);
    const missing = [...refs].filter((id) => !htmlIds.has(id));
    expect(missing).toEqual([]);
  });

  it('every tab has a matching screen section', () => {
    const tabs = [...html.matchAll(/data-tab="([^"]+)"/g)].map((m) => m[1]);
    expect(tabs).toEqual(['glance', 'alerts', 'energy', 'models', 'admin', 'settings']);
    tabs.forEach((t) => expect(htmlIds.has('scr-' + t)).toBe(true));
  });

  it('Home carries the cross-provider fleet tiles and the 24 h trend cards', () => {
    for (const id of ['glanceFleet', 'glanceMinis', 'glanceUpdated']) {
      expect(htmlIds.has(id), id).toBe(true);
    }
    // The mini sparklines reference the strip's gradient by id.
    expect(htmlIds.has('glanceGrad')).toBe(true);
    expect(js.includes("url(#glanceGrad)")).toBe(true);
  });

  it('the 24 h cards read fleet-aggregated history, not one arbitrary host', () => {
    // The unscoped endpoint lets the last host writing a timestamp win, which
    // interleaved 8 hosts' CPU into one series.
    expect(js.includes('fleet=all')).toBe(true);
  });

  it('the push opt-in button also takes the device back out', () => {
    expect(js.includes('togglePush')).toBe(true);
    expect(js.includes("'/api/companion/push/unsubscribe'")).toBe(true);
  });

  it('a push notification can deep-link into a tab', () => {
    const sw = read('sw.js');
    expect(sw.includes("'lsm-open'")).toBe(true);
    expect(js.includes("'lsm-open'")).toBe(true);
    expect(js.includes('tabFromUrl')).toBe(true);
  });

  it('models, admin and settings screens expose the controller contract ids', () => {
    for (const id of ['modelsServices', 'modelsLoaded', 'modelsAutopilot',
      'modelsPins', 'modelsPinsWrap', 'modelsGatedNote', 'modelsMsg',
      'adminManager', 'adminAgents', 'adminRows', 'adminAudit',
      'adminPending', 'adminPendingWrap', 'adminGatedNote', 'adminMsg',
      'settingsRelease', 'settingsUser', 'settingsMsg',
      'themeChips', 'pushStatus', 'pushCount', 'btnEnable', 'btnTest',
      'sheet', 'sheetTitle', 'sheetBody', 'sheetCancel']) {
      expect(htmlIds.has(id), id).toBe(true);
    }
  });

  it('the Actions screen is gone — its ids must not linger', () => {
    for (const id of ['scr-actions', 'actionsServices', 'actionsModel',
      'actionsAgents', 'actionsMsg']) {
      expect(htmlIds.has(id), id).toBe(false);
    }
    expect(js.includes("actions.start()")).toBe(false);
  });

  it('classic companion scripts co-load without a top-level name collision', () => {
    // A duplicate top-level const/function across these files is a page-break
    // SyntaxError that isolated-module vitest imports cannot see; parsing the
    // concatenation together surfaces it.
    const files = ['js/lib/pushutil.js', 'js/lib/energy.js', 'js/lib/companion-spark.js',
      'js/lib/companion-view.js', 'js/companion.js'];
    const concat = files.map(read).join('\n;\n');
    // Compile (parse) the concatenation without running it; a duplicate
    // top-level declaration throws SyntaxError here.
    expect(() => new vm.Script(concat)).not.toThrow();
  });

  it('the service-worker SHELL references only paths that exist on disk', () => {
    const sw = read('sw.js');
    const shell = [...sw.matchAll(/'(\/static\/[^']+)'/g)].map((m) => m[1]);
    expect(shell.length).toBeGreaterThanOrEqual(5);
    shell.forEach((p) => {
      const rel = p.replace('/static/', '');
      expect(fs.existsSync(path.join(root, rel)), rel).toBe(true);
    });
  });
});
