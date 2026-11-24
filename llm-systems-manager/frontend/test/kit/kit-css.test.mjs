import { describe, test } from 'vitest';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const KIT_SRC = readFileSync(resolve(HERE, '../../kit/kit.js'), 'utf8');
const CSS_PATH = resolve(HERE, '../../kit/kit.css');
const KIT_VERSION = readFileSync(resolve(HERE, '../../kit/KIT_VERSION'), 'utf8').trim();
// A small WORLD_DOTS of the cloud worlddots.js shape, so mapHtml draws its svg.
const WORLD_SRC = "var WORLD_DOTS = { w: 60, h: 30, step: 6, codes: ['DE', 'FR', 'JP', 'US'], dots: '1,1,0;2,1,0;3,1,1;7,2,2;8,2,2;5,3,3;6,3,3;6,4,3;' };";
// State modifiers styled only through compound selectors such as .chip.off or .k-val.long.
const KIT_CSS_EXEMPT = ['on', 'off', 'up', 'down', 'flat', 'long', 'static'];
// Script hooks on svg parts drawn with inline attributes; neither source styles them.
const KIT_CSS_HOOKS = ['ax-x', 'ts-bar', 'mk', 'xh', 'now-l'];
// The row fold chevron is an inline glyph inside the styled .row-hd button; neither source styles it.
const KIT_CSS_GLYPHS = ['chev'];
// State selectors the kit draws or toggles (kit.js and the shell markup), each with its own rule.
const KIT_STATE_SELECTORS = ['.chip.off', '.chip.static', '.k-val.long', '.k-delta.good', '.k-delta.bad', '.panel.full', '.row.folded',
  '[data-card].dragging', '.row[data-row].dragging', '.side .nav.on', '.side.collapsed', '.side.open', '.sw-btn[aria-checked="true"]',
  '.panel.full .p-close', '.panel.hero', '.side.collapsed + .main'];

const kitCss = () => readFileSync(CSS_PATH, 'utf8');
const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '');

// Loads the WORLD_DOTS fixture and kit.js into one context, as a classic script.
function kit() {
  const ctx = vm.createContext({});
  vm.runInContext(WORLD_SRC, ctx);
  vm.runInContext(KIT_SRC, ctx);
  return ctx;
}

// One sample of each renderer, with the options that add optional parts (notes, marks, hidden series, row actions).
function samples(k) {
  const labels = ['2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13'];
  k.KIT_SERIES_OFF['t2|B'] = true;
  return [
    k.panelHtml('p1', 'Panel', '<p>x</p>', { note: 'n', cls: 'span2', table: { cols: ['Name', 'Count'], rows: [['a', 1], ['b', 2]] } }),
    k.panelHead('p2', 'Grip', { grip: true, bars: true, table: true }),
    k.kpiHtml({ id: 'k1', title: 'Up', value: 1200, delta: { dir: 'up', tone: 'good', text: '5%' }, sub: 's', spark: [1, 3, 2, 5] }),
    k.kpiHtml({ id: 'k2', title: 'Down', value: 'a long text value here', delta: { dir: 'down', tone: 'bad', text: '2%' } }),
    k.kpiHtml({ id: 'k3', title: 'Flat', value: 3, delta: { dir: 'flat', text: '0' } }),
    k.tsHtml({ id: 't1', labels, series: [{ name: 'A', values: [1, 2, 3, 4] }, { name: 'B', values: [4, 3, 2, 1], color: 'bad' }], marks: [{ day: '2026-11-11', label: 'm' }] }),
    k.tsHtml({ id: 't2', labels, mode: 'stack', series: [{ name: 'A', values: [1, 2, 3, 4] }, { name: 'B', values: [1, 1, 1, 1] }, { name: 'C', values: [2, 0, 1, 3] }] }),
    k.tsHtml({ id: 't3', labels: [], series: [] }),
    k.lanesHtml({ id: 'l1', lanes: ['a', 'b'], now: '12:00', marks: [{ lane: 'a', at: '01:00', state: 'passed', key: 'x', tip: 't' }, { lane: 'b', at: '05:00', state: 'failed', key: 'y', tip: 't' }] }),
    k.rankHtml([{ label: 'a', v: 5 }, { label: 'b', v: 3 }, { label: 'c', v: 1 }], { key: 'model', n: 2, unit: 'req', dim: 'Model' }),
    k.splitHtml([{ label: 'a', v: 5 }, { label: 'b', v: 4 }, { label: 'c', v: 3 }, { label: 'd', v: 2 }, { label: 'e', v: 1 }], { unit: 'req' }),
    k.funnelHtml([{ label: 'a', v: 10 }, { label: 'b', v: 6 }]),
    k.mapHtml([{ code: 'DE', v: 4 }, { code: 'US', v: 9 }], { DE: 'Germany', US: 'United States' }, { key: 'country' }),
    k.feedHtml([{ kind: 'run', title: 't', detail: 'd', at: '2026-11-15T10:00:00Z' }], '2026-11-15T12:00:00Z'),
    k.kitTable(['Name', 'Count'], [['a', 1], ['b', 2]]),
    k.kitBarsHtml(['a', 'b'], [{ name: 'x', values: [1, 2] }, { name: 'y', values: [2, 1] }]),
    k.kitBarsHtml([], []),
    foldedRow(k),
  ].join('');
}

// A row dressed with its grip and menu, then drawn folded, as kitLayout does.
function foldedRow(k) {
  const root = document.createElement('div');
  root.innerHTML = '<section class="row" data-row="r1"><button type="button" class="row-hd" data-row-fold>Row</button><div class="panels"></div></section>';
  k.kitRowDress(root);
  k.kitRowDraw(root.firstChild, true);
  return root.innerHTML;
}

// Rules as {media, sels, body}: media is the enclosing at-rule heads joined by a space ('' at top level).
function rules(css) {
  const out = [], stack = [];
  let start = 0;
  for (let i = 0; i < css.length; i++) {
    if (css[i] === '{') {
      const head = css.slice(start, i).trim();
      if (head.startsWith('@')) { stack.push(head); start = i + 1; continue; }
      const end = css.indexOf('}', i);
      out.push({ media: stack.join(' '), sels: head.split(',').map((x) => x.trim()), body: css.slice(i + 1, end) });
      i = end;
      start = end + 1;
    } else if (css[i] === '}') {
      stack.pop();
      start = i + 1;
    }
  }
  return out;
}
// The joined bodies of the rules that list sel, under an at-rule head matching media (top level when media is '').
function bodyOf(css, sel, media = '') {
  return rules(css).filter((r) => (media ? media.test(r.media) : r.media === '') && r.sels.includes(sel)).map((r) => r.body).join(';');
}

// WCAG contrast ratio of two #rgb or #rrggbb colours.
function contrast(a, b) {
  const lum = (hex) => {
    const h = hex.slice(1), full = h.length === 3 ? h.replace(/./g, '$&$&') : h;
    const [r, g, bl] = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) / 255)
      .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// The custom properties declared in one block body, as {name: value}.
function decls(body) {
  const out = {};
  body.split(';').forEach((d) => {
    const m = /^\s*(--[\w-]+)\s*:\s*([\s\S]*?)\s*$/.exec(d);
    if (m) out[m[1]] = m[2];
  });
  return out;
}

describe('kit css', () => {
  test('line 1 names the kit version', () => {
    const first = kitCss().split('\n')[0];
    assert.ok(first.startsWith('/* Panel kit ' + KIT_VERSION + ': '), first);
    assert.ok(first.endsWith(' */'), first);
  });

  test('every class the renderers emit has a rule', () => {
    const css = stripComments(kitCss());
    const html = samples(kit());
    const tokens = new Set();
    for (const m of html.matchAll(/class="([^"]*)"/g)) m[1].split(/\s+/).filter(Boolean).forEach((t) => tokens.add(t));
    assert.ok(tokens.size > 40, 'samples emit the renderer classes');
    ['hint', 'chev', 'folded', 'row-grip', 'row-menu'].forEach((t) => assert.ok(tokens.has(t), 'samples emit ' + t));
    const missing = [...tokens].filter((t) => !KIT_CSS_EXEMPT.includes(t) && !KIT_CSS_HOOKS.includes(t) && !KIT_CSS_GLYPHS.includes(t))
      .filter((t) => !new RegExp('\\.' + t.replace(/[.*+?^${}()|[\]\\-]/g, '\\$&') + '(?![\\w-])').test(css));
    assert.deepEqual(missing, []);
    assert.deepEqual(KIT_STATE_SELECTORS.filter((s) => !css.includes(s)), [], 'every state selector has a rule');
  });

  test('both themes define the same tokens', () => {
    const css = stripComments(kitCss());
    const dark = {};
    for (const m of css.matchAll(/:root\s*\{([^}]*)\}/g)) Object.assign(dark, decls(m[1]));
    const lightBlocks = [...css.matchAll(/:root\[data-theme="light"\]\s*\{([^}]*)\}/g)];
    assert.equal(lightBlocks.length, 1);
    const light = decls(lightBlocks[0][1]);
    const literal = Object.keys(dark).filter((n) => /^(#[0-9a-f]{3,8}\b|rgba?\()/i.test(dark[n]));
    assert.ok(literal.length >= 20, 'dark block holds the colour tokens');
    assert.deepEqual(literal.filter((n) => !(n in light)), [], 'light block redefines every literal dark colour');
    assert.deepEqual(Object.keys(light).filter((n) => !(n in dark)), [], 'light block defines nothing absent from dark');
    assert.ok('--map-dot' in dark && '--map-dot' in light, '--map-dot in both themes');
    assert.match(css, /:root\s*\{\s*color-scheme:\s*dark/);
  });

  test('cloud tokens', () => {
    const css = stripComments(kitCss());
    const dark = {};
    for (const m of css.matchAll(/:root\s*\{([^}]*)\}/g)) Object.assign(dark, decls(m[1]));
    const light = decls(/:root\[data-theme="light"\]\s*\{([^}]*)\}/.exec(css)[1]);
    const names = ['--c1', '--c2', '--c3', '--c4', '--c5', '--c6', '--c7', '--c8', '--c-warn'];
    assert.deepEqual(names.filter((n) => !(n in dark)), [], 'dark declares the cloud colour set');
    assert.equal(dark['--c5'], '#eab308');
    assert.ok('--c5' in light, '--c5 in the light theme');
    assert.equal(light['--muted'], '#7a8291');
  });

  test('light status text tokens reach 4.5:1 on the light card', () => {
    const css = stripComments(kitCss());
    const dark = {};
    for (const m of css.matchAll(/:root\s*\{([^}]*)\}/g)) Object.assign(dark, decls(m[1]));
    const light = decls(/:root\[data-theme="light"\]\s*\{([^}]*)\}/.exec(css)[1]);
    assert.equal(contrast('#000', '#fff').toFixed(1), '21.0');
    for (const s of ['good', 'bad', 'warn']) {
      const n = '--' + s + '-text';
      assert.equal(dark[n], 'var(--' + s + ')', n + ' in dark is the status colour');
      assert.match(light[n] || '', /^#[0-9a-f]{3,6}$/i, n + ' in light is a literal colour');
      const ratio = contrast(light[n], light['--card']);
      assert.ok(ratio >= 4.5, n + ' ' + light[n] + ' on ' + light['--card'] + ' is ' + ratio.toFixed(2));
    }
    assert.match(bodyOf(css, '.k-delta.good'), /color:\s*var\(--good-text\)/);
    assert.match(bodyOf(css, '.k-delta.bad'), /color:\s*var\(--bad-text\)/);
    assert.doesNotMatch(css, /(^|[;{])\s*color:\s*var\(--(good|bad|warn)\)/, 'no text colour uses a status chart colour');
  });

  test('cloud grid', () => {
    const css = stripComments(kitCss());
    assert.doesNotMatch(css, /span-/, 'no span-N selector');
    assert.match(bodyOf(css, '.panels'), /grid-template-columns:\s*minmax\(0,\s*1fr\)/);
    assert.match(bodyOf(css, '.panels', /min-width:\s*700px/), /grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
    assert.match(bodyOf(css, '.panels', /min-width:\s*1200px/), /grid-template-columns:\s*repeat\(3,\s*minmax\(0,\s*1fr\)\)/);
    assert.match(bodyOf(css, '.panels .span2'), /grid-column:\s*1\s*\/\s*-1/);
    assert.match(bodyOf(css, '.panels .span2', /min-width:\s*1200px/), /grid-column:\s*span 2/);
    assert.match(bodyOf(css, '.panel.hero'), /grid-column:\s*1\s*\/\s*-1/);
    assert.match(bodyOf(css, '.kpis'), /grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(170px,\s*1fr\)\)/);
  });

  test('cloud shell', () => {
    const css = stripComments(kitCss());
    assert.match(bodyOf(css, '.side'), /position:\s*fixed/);
    assert.match(bodyOf(css, '.side'), /(^|;)\s*width:\s*210px/);
    assert.match(bodyOf(css, '.main'), /margin-left:\s*210px/);
    assert.match(bodyOf(css, '.side.collapsed', /min-width:\s*900px/), /(^|;)\s*width:\s*56px/);
    assert.match(bodyOf(css, '.side.collapsed + .main', /min-width:\s*900px/), /margin-left:\s*56px/);
    assert.match(bodyOf(css, '.side', /max-width:\s*899px/), /translateX\(-100%\)/);
    assert.match(bodyOf(css, '.side.open', /max-width:\s*899px/), /transform:\s*none/);
    assert.match(bodyOf(css, '.main', /max-width:\s*899px/), /margin-left:\s*0/);
    assert.match(bodyOf(css, '.nav-open', /max-width:\s*899px/), /display:\s*inline-block/);
    ['.wrap', '.side-in'].forEach((s) => assert.doesNotMatch(css, new RegExp(s.replace('.', '\\.') + '(?![\\w-])'), 'no ' + s));
  });

  test('no global reset, cloud title, toast, light on-accent and foot', () => {
    const css = stripComments(kitCss()), all = rules(css);
    assert.deepEqual(all.filter((r) => r.sels.includes('*')), [], 'no * rule');
    assert.doesNotMatch(css, /(^|[}\s])\*\s*\{/, 'no *{ rule');
    assert.doesNotMatch(bodyOf(css, 'body'), /line-height/);
    assert.match(bodyOf(css, 'body'), /font-family:\s*-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif/);
    assert.match(bodyOf(css, '.top h1'), /font-size:\s*22px/);
    assert.match(bodyOf(css, '.top h1'), /font-weight:\s*700/);
    for (const s of ['.top h1', '.side h4', '.panel', '.p-hd']) assert.match(bodyOf(css, s), /(^|;)\s*margin:\s*0(;|$)/, s + ' zeroes its margin');
    assert.match(bodyOf(css, '#toast'), /z-index:\s*50/);
    const light = decls(/:root\[data-theme="light"\]\s*\{([^}]*)\}/.exec(css)[1]);
    assert.equal(light['--on-accent'], '#ffffff');
    assert.equal(light['--good-text'], '#15803d');
    assert.doesNotMatch(bodyOf(css, '.side-foot'), /border-top|margin-top|padding-top/);
    assert.equal(all.filter((r) => r.sels.some((x) => x.startsWith('.side-foot:not('))).length, 0, 'no empty-foot phone rule');
    assert.doesNotMatch(css, /\.bar(?![\w-])/, 'no bare .bar selector');
  });

  test('the close button shows only on a full-size panel', () => {
    const css = stripComments(kitCss());
    assert.match(bodyOf(css, '.p-close'), /display:\s*none/);
    assert.match(bodyOf(css, '.panel.full .p-close'), /display:\s*inline-block/);
  });

  test('the table box keeps its height, full size lets it shrink, and its scrollbar is thin and hover-only with a mouse', () => {
    const css = stripComments(kitCss());
    assert.match(bodyOf(css, '.p-table'), /flex:\s*none/);
    assert.match(bodyOf(css, '.panel.full>.p-table'), /flex:\s*0 1 auto/);
    assert.match(bodyOf(css, '.panel.full'), /scrollbar-width:\s*thin/);
    assert.match(bodyOf(css, '.p-table'), /scrollbar-color:\s*color-mix/);
    assert.match(css, /@media \(hover:hover\)\{\.p-table:not\(:hover\):not\(:focus-within\),\.panel\.full:not\(:hover\):not\(:focus-within\)\{scrollbar-color:transparent transparent\}\}/);
  });

  test('no app selectors', () => {
    const css = stripComments(kitCss());
    ['#v-', '.ov-', '.run-', '.mcp-', '.lab-', '.st-', '#asst', '.hdr-asst', '#toolbarExtra'].forEach((s) => {
      assert.ok(!css.includes(s), 'kit.css has no ' + s + ' selector');
    });
  });
});
