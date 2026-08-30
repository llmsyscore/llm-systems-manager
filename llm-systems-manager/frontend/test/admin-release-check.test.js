// #758: the Admin release check must distinguish "no verdict" / "check
// failed" from "up to date". Co-loads the real admin.js in jsdom and calls
// _adminReleaseInfoHtml() directly.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const adminSrc = readFileSync(join(here, '..', 'js', 'admin.js'), 'utf8');

function info(rel) {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(adminSrc);
  inject(`window.__out = _adminReleaseInfoHtml(${JSON.stringify(rel)});`);
  return dom.window.__out;
}

describe('_adminReleaseInfoHtml', () => {
  test('silent when the check is disabled', () => {
    expect(info({ enabled: false, update_available: null, note: 'n' })).toBe('');
  });

  test('silent when an update IS available (the warn row covers it)', () => {
    expect(info({ enabled: true, update_available: true, latest: 'v1.4.0' })).toBe('');
  });

  test('silent when up to date', () => {
    expect(info({ enabled: true, update_available: false, latest: 'v1.4.0' })).toBe('');
  });

  test('surfaces the note when the check reached no verdict', () => {
    // The #757 case: source=null, so there is no tag to compare.
    const out = info({
      enabled: true, update_available: null,
      note: 'install has no release tag to compare',
    });
    expect(out).toContain('install has no release tag to compare');
    expect(out).toContain('info-row');
  });

  test('falls back to a generic phrase when null carries no note', () => {
    expect(info({ enabled: true, update_available: null })).toContain('no verdict');
  });

  test('an error outranks the note, which would misattribute the cause', () => {
    const out = info({
      enabled: true, update_available: null,
      error: 'github returned HTTP 403',
      note: 'install has no release tag to compare',
    });
    expect(out).toContain('github returned HTTP 403');
    expect(out).not.toContain('no release tag');
  });

  test('reports a failed check even when a stale verdict says up to date', () => {
    const out = info({
      enabled: true, update_available: false, error: 'ConnectionError',
    });
    expect(out).toContain('ConnectionError');
  });

  test('escapes server-supplied text', () => {
    const out = info({
      enabled: true, update_available: null, error: '<img src=x onerror=1>',
    });
    expect(out).not.toContain('<img');
    expect(out).toContain('&lt;img');
  });
});

// Render-level: the info row must not displace the health roll-up's own
// warnings, nor the "all nominal" line when there are none.
function render(d, rel) {
  const dom = new JSDOM(
    '<!doctype html><html><head></head><body>'
    + '<div id="adminHealthWarnings"></div></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(adminSrc);
  inject(`_renderSystemHealth(${JSON.stringify(d)}, ${JSON.stringify(rel)});`);
  return dom.window.document.getElementById('adminHealthWarnings').innerHTML;
}

describe('_renderSystemHealth warnings panel', () => {
  const NO_VERDICT = {
    enabled: true, update_available: null,
    note: 'install has no release tag to compare',
  };

  test('keeps "all nominal" and still explains the dead release check', () => {
    const out = render({ warnings: [] }, NO_VERDICT);
    expect(out).toContain('All systems nominal');
    expect(out).toContain('no release tag to compare');
  });

  test('keeps real health warnings alongside the info row', () => {
    const out = render({ warnings: ['2 approved agent(s) down'] }, NO_VERDICT);
    expect(out).toContain('2 approved agent(s) down');
    expect(out).toContain('no release tag to compare');
    expect(out).not.toContain('All systems nominal');
  });

  test('an available update still renders the warn row, with no info row', () => {
    const out = render({ warnings: [] }, {
      enabled: true, update_available: true,
      latest: 'v1.4.0', installed: 'v1.3.0', repo: 'llmsyscore/llm-systems-manager',
    });
    expect(out).toContain('New release v1.4.0 available');
    expect(out).toContain('installed v1.3.0');
    expect(out).not.toContain('info-row');
  });
});
