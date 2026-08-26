// Shared JSDOM/eval harness helpers for the frontend classic-script tests.
// Not itself a *.test.js file, so vitest's test glob skips it.
import { expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

// Reads a frontend-relative file (js/…, css/…, index.html, ../backend/…) as text.
export function srcFile(relPath) {
  return readFileSync(join(FRONTEND_ROOT, relPath), 'utf8');
}

// Extracts a real `[async ]function <name>(...) { ... }` up to the first
// column-0 `}`. Returns null when absent — callers assert with their own message.
export function fnSrc(src, name) {
  const m = src.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  return m ? m[0] : null;
}

// Extracts a top-level `const NAME = {...};`, rewritten as `window.NAME = {...};`
// so repeated eval doesn't hit "already declared". Returns null when absent.
export function constAsWindowProp(src, name) {
  const m = src.match(new RegExp(`const ${name} = \\{[\\s\\S]*?\\};`));
  return m ? m[0].replace(new RegExp(`^const ${name}`), `window.${name}`) : null;
}

// Slices source from startMarker up to endMarker (inclusive by default).
export function blockSrc(source, startMarker, endMarker, { includeEnd = true } = {}) {
  const start = source.indexOf(startMarker);
  expect(start, `"${startMarker}" not found`).toBeGreaterThan(-1);
  const endAt = source.indexOf(endMarker, start);
  expect(endAt, `"${endMarker}" not found after "${startMarker}"`).toBeGreaterThan(includeEnd ? -1 : start);
  return source.slice(start, includeEnd ? endAt + endMarker.length : endAt);
}

// Runs code at global scope via indirect eval, matching classic-script
// semantics (bare identifiers resolve to window) instead of ESM scoping.
export function evalGlobal(code) {
  (0, eval)(code);
}

// Full-inject JSDOM harness: injects each source string as a script tag in
// order, then the bootstrap, and returns the resulting dom.window.
export function runHarness({ sources = [], bootstrap = '', bodyHtml = '' } = {}) {
  const dom = new JSDOM(`<!doctype html><html><head></head><body>${bodyHtml}</body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  sources.forEach(inject);
  inject(bootstrap);
  return dom.window;
}

// Evaluates boot.js's real _subTabState/_SUB_TAB_MAP/switchSubTab at global
// scope instead of grepping boot.js text for their behavior.
export function loadSwitchSubTab(bootSrc) {
  const stateSrc = constAsWindowProp(bootSrc, '_subTabState');
  const mapSrc = constAsWindowProp(bootSrc, '_SUB_TAB_MAP');
  const fnSource = fnSrc(bootSrc, 'switchSubTab');
  expect(stateSrc, '_subTabState not found').toBeTruthy();
  expect(mapSrc, '_SUB_TAB_MAP not found').toBeTruthy();
  expect(fnSource, 'switchSubTab not found').toBeTruthy();
  evalGlobal([
    // switchSubTab calls this unconditionally whenever sub !== 'lmstudio'.
    'window.stopLmsLogRefresh = function () {};',
    stateSrc,
    mapSrc,
    fnSource,
    'window.switchSubTab = switchSubTab;',
  ].join('\n'));
}

// setTimeout(0) tick, for waiting out a microtask/macrotask handoff.
export function flush() {
  return new Promise((r) => setTimeout(r, 0));
}
