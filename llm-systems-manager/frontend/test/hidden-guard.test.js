// #858: base.css carries one global `[hidden]` guard; no per-class guards remain.
import { describe, it, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { srcFile } from './helpers/harness.js';

const base = srcFile('css/base.css');
const cssFiles = readdirSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'css')).filter(f => f.endsWith('.css'));

describe('global [hidden] guard (#858)', () => {
  it('base.css declares a bare [hidden] { display: none !important } rule', () => {
    const m = base.match(/^\s*\[hidden\]\s*\{([^}]*)\}/m);
    expect(m, '[hidden] rule').toBeTruthy();
    expect(m[1]).toMatch(/display\s*:\s*none\s*!important/);
  });

  it('no stylesheet carries a per-class [hidden] guard any more', () => {
    for (const f of cssFiles) {
      const css = srcFile(`css/${f}`);
      const perClass = css.match(/^[^\n{]*\S[^\n{]*\[hidden\][^\n{]*\{/gm) || [];
      expect(perClass, `${f}: ${perClass.join(' | ')}`).toEqual([]);
    }
  });

  it('hidden wins over class display values in the dashboard and the companion', () => {
    const sheets = ['base.css', 'modelcards.css', 'admin-tabs.css', 'agents.css', 'settings.css', 'companion.css']
      .map(f => `<style>${srcFile(`css/${f}`)}</style>`).join('');
    const dom = new JSDOM(`<!doctype html><html><head>${sheets}</head><body>
      <button class="mcbtn" id="b" hidden>x</button>
      <div id="adminTab"><div class="notice" id="n" hidden>x</div></div>
      <div id="admin-routing"><div class="card" id="rtGatewayCard" hidden>x</div></div>
      <nav class="tabbar"><a class="tab"><span class="badge" id="bd" hidden>1</span></a></nav>
      <div class="sheetwrap" id="sh" hidden>x</div></body></html>`);
    const w = dom.window;
    for (const id of ['b', 'n', 'rtGatewayCard', 'bd', 'sh']) {
      const el = w.document.getElementById(id);
      expect(w.getComputedStyle(el).display, `#${id} hidden`).toBe('none');
      el.hidden = false;
      expect(w.getComputedStyle(el).display, `#${id} shown`).not.toBe('none');
    }
  });
});
