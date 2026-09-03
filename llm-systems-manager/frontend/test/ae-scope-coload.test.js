// The alarm engine's classic scripts share one global scope, so a duplicate
// top-level `const`/`let` in any of them is a page-breaking SyntaxError that
// no per-file check can see. Compile them together the way the browser does.
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const AE_FRONTEND = path.resolve(here, '../../../llm-systems-alarm-engine/frontend');

function scriptSources() {
  const html = fs.readFileSync(path.join(AE_FRONTEND, 'index.html'), 'utf8');
  const srcs = [...html.matchAll(/<script src="(js\/[^"?]+)(?:\?[^"]*)?"/g)]
    .map(m => m[1])
    .filter(p => !p.startsWith('js/vendor/'));
  return srcs.map(p => ({ p, src: fs.readFileSync(path.join(AE_FRONTEND, p), 'utf8') }));
}

describe('alarm engine classic scripts co-load', () => {
  const files = scriptSources();

  it('index.html loads the module set in dependency order', () => {
    const names = files.map(f => f.p);
    expect(names.slice(0, 3)).toEqual(['js/api.js', 'js/websocket.js', 'js/thresholds.js']);
    expect(names[names.length - 1]).toBe('js/main.js');
    expect(names).toContain('js/ui.js');
  });

  it('compiles every script into one scope without a collision', () => {
    const joined = files.map(f => f.src).join('\n;\n');
    expect(() => new vm.Script(joined, { filename: 'ae-coload.js' })).not.toThrow();
  });

  it('control: a duplicate top-level const is detected', () => {
    const joined = files.map(f => f.src).join('\n;\n') + '\n;const escapeHtml = 1;';
    expect(() => new vm.Script(joined, { filename: 'ae-coload-control.js' })).toThrow(SyntaxError);
  });
});
