// #468: compiles every classic script the dashboard co-loads into one scope
// to catch top-level redeclaration collisions isolated-module imports miss.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { Script } from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..');

// Script order as index.html loads them, vendor bundles excluded.
function coLoadedScripts() {
  const html = readFileSync(resolve(ROOT, 'index.html'), 'utf8');
  const srcs = [...html.matchAll(/<script src="\/static\/(js\/[^"?]+)/g)]
    .map(m => m[1]);
  return [...new Set(srcs)];
}

// Compiles without running: redeclaration collisions throw at compile time,
// so no browser-dependent top-level code executes.
function parseTogether(sources) {
  new Script(sources.join('\n;\n'));
}

describe('classic-script global scope', () => {
  it('index.html lists the report card scripts', () => {
    const scripts = coLoadedScripts();
    expect(scripts).toContain('js/lib/reportcard.js');
    expect(scripts).toContain('js/report-card.js');
  });

  it('every co-loaded dashboard script shares one scope without collisions', () => {
    const scripts = coLoadedScripts();
    const sources = scripts.map(s => readFileSync(resolve(ROOT, s), 'utf8'));
    expect(() => parseTogether(sources)).not.toThrow();
  });

  // Control: proves the harness actually detects the failure it guards against.
  it('detects a duplicate top-level declaration (control)', () => {
    const charts = readFileSync(resolve(ROOT, 'js/charts.js'), 'utf8');
    expect(() => parseTogether([charts, 'const fmt = 1;']))
      .toThrow(/already been declared/);
  });
});
