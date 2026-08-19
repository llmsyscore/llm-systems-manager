// #572: one --load-mode dropdown replaces mmap/direct-io/mlock profile keys.
// Real source in jsdom via extraction.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'js', 'llmcontrol-models.js'), 'utf8');

function fnSrc(name) {
  const m = src.match(new RegExp(
    `function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  expect(m, `${name} not found`).toBeTruthy();
  return m[0];
}

const defs = src.match(/const EF_SPECIAL_DEFAULTS = \{[^}]*\};/);
(0, eval)([defs[0], fnSrc('efLoadModeFromCfg'),
           'window.efLoadModeFromCfg = efLoadModeFromCfg;'].join('\n'));

describe('efLoadModeFromCfg (#572)', () => {
  it('explicit load-mode wins', () => {
    expect(window.efLoadModeFromCfg({ 'load-mode': 'dio' })).toBe('dio');
  });
  it('legacy keys map onto load modes', () => {
    expect(window.efLoadModeFromCfg({ 'direct-io': 'on' })).toBe('dio');
    expect(window.efLoadModeFromCfg({ 'no-mmap': 'on' })).toBe('none');
    expect(window.efLoadModeFromCfg({ mlock: 'on' })).toBe('mmap+mlock');
    expect(window.efLoadModeFromCfg({ 'no-mmap': 'on', mlock: 'on' })).toBe('mlock');
  });
  it('defaults to auto', () => {
    expect(window.efLoadModeFromCfg({})).toBe('auto');
    expect(window.efLoadModeFromCfg({ mmap: 'on' })).toBe('auto');
  });
});
