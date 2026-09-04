// #851: `.mcbtn { display: inline-flex }` outranks the UA `[hidden]` rule, so
// a hidden .mcbtn still painted. The shared stylesheet must carry the guard.
import { describe, it, expect } from 'vitest';
import { srcFile } from './helpers/harness.js';

const css = srcFile('css/modelcards.css');

describe('.mcbtn honours the hidden attribute (#851)', () => {
  it('declares a [hidden] guard that wins over the display rule', () => {
    const base = css.indexOf('.mcbtn {');
    expect(base, '.mcbtn base rule').toBeGreaterThan(-1);
    const m = css.match(/\.mcbtn\[hidden\]\s*\{([^}]*)\}/);
    expect(m, '.mcbtn[hidden] rule').toBeTruthy();
    expect(m[1]).toMatch(/display\s*:\s*none\s*!important/);
    expect(css.indexOf(m[0])).toBeGreaterThan(base);
  });
});
