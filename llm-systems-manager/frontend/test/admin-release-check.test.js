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
    // source=null: no tag to compare.
    const out = info({
      enabled: true, update_available: null,
      note: 'install has no release tag to compare',
    });
    expect(out).toContain('install has no release tag to compare');
    expect(out).toContain('w info');
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

  test('reports the endpoint itself being unreachable', () => {
    expect(info({ unreachable: true })).toContain('endpoint unreachable');
  });

  test('silent before any fetch has completed', () => {
    expect(info(null)).toBe('');
  });

  test('escapes server-supplied text', () => {
    const out = info({
      enabled: true, update_available: null, error: '<img src=x onerror=1>',
    });
    expect(out).not.toContain('<img');
    expect(out).toContain('&lt;img');
  });
});

// Render-level: HealthView orders crit → warn → note → info, and the info row
// must not displace the health roll-up's own warnings.
const healthSrc = readFileSync(join(here, '..', 'js', 'admin-health.js'), 'utf8');
const indexSrc = readFileSync(join(here, '..', 'index.html'), 'utf8');
const CARD = indexSrc.slice(indexSrc.indexOf('<div id="adminHealthCard">'),
                            indexSrc.indexOf('<!-- Sub-tabs underneath System Health -->'));

function render(d, rel) {
  const dom = new JSDOM(`<!doctype html><html><head></head><body><div id="adminTab">${CARD}</div></body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(adminSrc);
  inject(healthSrc);
  inject(`HealthView.render(${JSON.stringify(d)}, ${JSON.stringify(rel)});`);
  return dom.window.document.getElementById('adminHealthWarnings').innerHTML;
}

describe('HealthView warnings panel', () => {
  const NO_VERDICT = {
    enabled: true, update_available: null,
    note: 'install has no release tag to compare',
  };

  test('an empty roll-up still explains the dead release check', () => {
    const out = render({ warnings: [] }, NO_VERDICT);
    expect(out).toContain('no release tag to compare');
    expect(out).toContain('w info');
    expect(out).not.toContain('w-none');
  });

  test('keeps real health warnings alongside the info row', () => {
    const out = render({ warnings: ['2 approved agent(s) down'] }, NO_VERDICT);
    expect(out).toContain('2 approved agent(s) down');
    expect(out).toContain('no release tag to compare');
  });

  test('an unreachable endpoint renders as the info row', () => {
    const out = render({ warnings: [] }, { unreachable: true });
    expect(out).toContain('endpoint unreachable');
  });

  test('no warnings and a quiet release check render the mono None', () => {
    const out = render({ warnings: [] }, { enabled: true, update_available: false });
    expect(out).toContain('w-none');
    expect(out).toContain('None');
  });

  test('an available update renders the note row, with no info row', () => {
    const out = render({ warnings: [] }, {
      enabled: true, update_available: true,
      latest: 'v1.4.0', installed: 'v1.3.0', repo: 'llmsyscore/llm-systems-manager',
    });
    expect(out).toContain('Manager <span class="m">v1.4.0</span> is available');
    expect(out).toContain('installed v1.3.0');
    expect(out).not.toContain('w info');
  });

  test('agent_update renders an Update all button', () => {
    const out = render({ warnings: [], agent_update: { latest: 'v2026.09.01-3', outdated: 3 } },
      { enabled: false });
    expect(out).toContain('3 agents can update to');
    expect(out).toContain('data-act="updateall"');
  });

  test('crit rows sort ahead of warn, note and info', () => {
    const out = render({
      warnings: ['agent mac-mini TLS cert expires in 9d', 'alarm engine unreachable: ConnectTimeout'],
      agent_update: { latest: 'v2', outdated: 1 },
    }, { enabled: true, update_available: null, note: 'no tag' });
    const kinds = [...out.matchAll(/class="w (crit|warn|note|info)"/g)].map(m => m[1]);
    expect(kinds).toEqual(['crit', 'warn', 'note', 'info']);
  });
});
