// #1432: the manager never uses the kit's bare class names top, ver, hint, empty or mono as its own classes.
// Scans class attributes, className strings, classList calls and CSS or querySelector selectors.
import { describe, it, expect } from 'vitest';
import { readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';
import { srcFile } from './helpers/harness.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const TOKENS = ['top', 'ver', 'hint', 'empty', 'mono'];

function walk(dir, ext) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p, ext));
    else if (p.endsWith(ext)) out.push(relative(root, p));
  }
  return out;
}

// Class tokens in an attribute value; `${…}` parts contribute only their quoted literals.
function valueTokens(val) {
  const toks = [];
  const plain = val.replace(/\$\{([^}]*)\}/g, (_, expr) => {
    for (const q of expr.matchAll(/(['"`])([^'"`]*)\1/g)) toks.push(...q[2].split(/\s+/));
    return ' ';
  });
  toks.push(...plain.split(/\s+/));
  return toks.filter(Boolean);
}
const selTokens = (sel) => [...sel.matchAll(/\.([\w-]+)/g)].map(m => m[1]);

// Each class attribute value; quotes inside `${…}` do not end it.
function attrValues(src) {
  const out = [];
  for (const m of src.matchAll(/\bclass=(\\?)(["'])/g)) {
    const end = m[1] + m[2];
    let val = '', depth = 0;
    for (let i = m.index + m[0].length; i < src.length && val.length < 4000; i++) {
      if (depth === 0 && src.startsWith(end, i)) break;
      if (src.startsWith('${', i)) { depth++; val += '${'; i++; continue; }
      if (depth && src[i] === '}') depth--;
      val += src[i];
    }
    out.push(val);
  }
  return out;
}

// Top-level argument texts of each `name(` call that is not a method call or part of a longer name.
function callArgs(src, name) {
  const out = [];
  for (const m of src.matchAll(new RegExp(`(?<![\\w.$])${name}\\(`, 'g'))) {
    const args = [];
    let depth = 0, cur = '', q = null;
    for (let i = m.index + m[0].length; i < src.length && i < m.index + 2000; i++) {
      const c = src[i];
      if (q) { cur += c; if (c === '\\') cur += src[++i] || ''; else if (c === q) q = null; continue; }
      if (c === '"' || c === "'" || c === '`') { q = c; cur += c; continue; }
      if ('([{'.includes(c)) depth++;
      if (')]}'.includes(c)) { if (depth === 0) { args.push(cur); break; } depth--; }
      if (c === ',' && depth === 0) { args.push(cur); cur = ''; continue; }
      cur += c;
    }
    out.push(args.map(a => a.trim()));
  }
  return out;
}
const literals = (s) => [...(s || '').matchAll(/(['"`])([^'"`]*)\1/g)].flatMap(q => q[2].split(/\s+/)).filter(Boolean);

function markupTokens(src) {
  const toks = [];
  for (const v of attrValues(src)) toks.push(...valueTokens(v));
  for (const m of src.matchAll(/\.className\s*\+?=\s*(['"`])((?:(?!\1).)*)\1/g)) toks.push(...valueTokens(m[2]));
  for (const m of src.matchAll(/\.classList\.\w+\(([^)]*)\)/g)) toks.push(...literals(m[1]));
  for (const m of src.matchAll(/\.(?:querySelector(?:All)?|closest|matches)\(\s*(['"`])((?:(?!\1).)*)\1/g)) toks.push(...selTokens(m[2]));
  for (const m of src.matchAll(/(?<![\w$-])class\s*:\s*(['"])((?:(?!\1).)*)\1/g)) toks.push(...valueTokens(m[2]));
  for (const m of src.matchAll(/setAttribute\(\s*(['"])class\1\s*,\s*(['"`])((?:(?!\2).)*)\2/g)) toks.push(...valueTokens(m[3]));
  // el(tag, className, text) takes the class second; cell(value, cls) takes it last.
  for (const a of callArgs(src, 'el')) toks.push(...literals(a[1]));
  for (const a of callArgs(src, 'cell')) toks.push(...literals(a[a.length - 1]));
  for (const m of src.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/gi)) toks.push(...cssTokens(m[1]));
  return toks;
}

function cssTokens(src) {
  const toks = [];
  const bare = src.replace(/\/\*[\s\S]*?\*\//g, '');
  for (const m of bare.matchAll(/([^{}]+)\{/g)) {
    const sel = m[1].split(';').pop().trim();
    if (!sel.startsWith('@')) toks.push(...selTokens(sel));
  }
  return toks;
}

const KIT_SHELL = /<!-- kit shell -->[\s\S]*?<!-- \/kit shell -->/g;
const FILES = ['index.html', 'companion.html', ...walk(join(root, 'js'), '.js'),
  ...readdirSync(join(root, 'css')).filter(n => n.endsWith('.css')).map(n => `css/${n}`)];

describe('kit host class collisions', () => {
  it('scans the manager sources', () => {
    expect(FILES).toContain('js/foundation.js');
    expect(FILES).toContain('css/base.css');
    expect(markupTokens('<div class="a top">')).toContain('top');
    expect(cssTokens('.sd-row.top .k { top: 0; }')).toContain('top');
  });
  it('flags every class form the manager writes', () => {
    const forms = {
      attribute: '<span class="x hint">',
      escapedAttribute: '"<span class=\\"hint\\">"',
      className: "d.className = 'hint';",
      classList: "d.classList.toggle('hint', on);",
      selector: "w.querySelector('#a .hint');",
      elHelper: "el('div', 'hint', 'Some text');",
      cellHelper: "cell(r.a, (r.changed ? 'old' : 'hint') + w);",
      optionSingle: "n('g', { class: 'hint' });",
      optionDouble: 'n("g", { class: "hint" });',
      setAttribute: "e.setAttribute('class', 'hint');",
      nestedDoubleQuoted: '`<span class="x ${on ? "hint" : ""}">`',
      styleBlock: '<style>\n  .card .hint { color: red; }\n</style>',
    };
    for (const [form, sample] of Object.entries(forms)) expect(markupTokens(sample), form).toContain('hint');
    expect(markupTokens("el('div', 'm-hint', 'back to top');")).not.toContain('top');
  });
  for (const f of FILES) {
    it(`${f} uses no kit bare class name`, () => {
      let src = srcFile(f);
      if (f === 'index.html') src = src.replace(KIT_SHELL, '');
      const toks = new Set(f.endsWith('.css') ? cssTokens(src) : markupTokens(src));
      for (const t of TOKENS) expect(toks.has(t), `${f} uses class "${t}"; rename it to "m-${t}"`).toBe(false);
    });
  }
});
