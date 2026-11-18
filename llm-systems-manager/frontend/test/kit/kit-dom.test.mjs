import { test } from 'vitest';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';
import { JSDOM } from 'jsdom';

const HERE = dirname(fileURLToPath(import.meta.url));
const KIT_SRC = readFileSync(resolve(HERE, '../../kit/kit.js'), 'utf8');

// A real DOM (jsdom) with kit.js loaded under a store prefix, wired, and one laid-out page holding the given rows.
function page(rows, opts = {}) {
  const dom = new JSDOM(`<!doctype html><body><div id="pg">${rows}</div><div id="toast"></div></body>`, { runScripts: 'outside-only', url: 'http://kit.test/' });
  const w = dom.window, d = w.document, pg = d.getElementById('pg');
  w.eval(KIT_SRC);
  w.kitConfigure({ prefix: opts.prefix || 'dt', ...(opts.store ? { store: opts.store } : {}) });
  for (const [k, v] of Object.entries(opts.seed || {})) w.localStorage.setItem(k, v);
  w.kitWire(d);
  w.kitDress(pg);
  w.kitLayout('p', pg, w.kitStore(d));
  const click = (el) => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  return { w, d, pg, click, $: (id) => d.getElementById(id) };
}
const row = (id, inner) => `<div class="row" data-row="${id}"><button type="button" class="row-hd" data-row-fold>${id}</button><div class="panels">${inner}</div></div>`;
const panel = (card, inner) => `<section class="panel" data-card="${card}" data-title="${card}"><div class="p-body" id="${card}Body">${inner}</div></section>`;
const tick = () => new Promise((r) => setTimeout(r, 0));
const firstCol = (t) => [...t.tBodies[0].rows].map((r) => r.cells[0].textContent);
const TABLE = (rows, attrs = '') => `<table ${attrs}><thead><tr><th>Tool</th><th>Calls</th></tr></thead>${rows.map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`).join('')}</table>`;

test('a table added later gets its default sort through the real mutation observer', async () => {
  const p = page(row('a', panel('late', '')));
  p.$('lateBody').innerHTML = TABLE([['a', '3'], ['b', '10']], 'data-sort="Calls desc"');
  await tick();
  const t = p.$('lateBody').querySelector('table');
  assert.equal(t.tBodies.length, 1);
  assert.ok(t.hasAttribute('data-kit-sortable'));
  assert.deepEqual(firstCol(t), ['b', 'a']);
  assert.equal(t.tHead.rows[0].cells[1].getAttribute('aria-sort'), 'descending');
});

test('a header click sorts, is saved, and holds when the table is rebuilt', async () => {
  const p = page(row('a', panel('late', '')));
  p.$('lateBody').innerHTML = TABLE([['b', '1'], ['a', '2']]);
  await tick();
  p.click(p.$('lateBody').querySelector('th'));
  assert.deepEqual(firstCol(p.$('lateBody').querySelector('table')), ['a', 'b']);
  assert.deepEqual(JSON.parse(p.w.localStorage.getItem('dt.sort')), { p: { 'late#Tool|Calls': { col: 'Tool', dir: 'asc' } } });
  p.$('lateBody').innerHTML = TABLE([['d', '1'], ['c', '2']]);
  await tick();
  assert.deepEqual(firstCol(p.$('lateBody').querySelector('table')), ['c', 'd']);
});

test('a difference row with a nested table stays under its section, and the nested table sorts on its own', async () => {
  const p = page(row('a', panel('runSide', '')));
  p.$('runSideBody').innerHTML = '<table><thead><tr><th>section</th><th>pass</th></tr></thead><tbody>' +
    '<tr><td>A</td><td>1</td></tr><tr><td>B</td><td>2</td></tr>' +
    '<tr data-kit-child><td colspan="2"><table><thead><tr><th>key</th></tr></thead><tbody><tr><td>z</td></tr><tr><td>y</td></tr></tbody></table></td></tr>' +
    '<tr><td>C</td><td>3</td></tr></tbody></table>';
  await tick();
  const outer = p.$('runSideBody').querySelector('table'), inner = outer.querySelector('table');
  p.click(outer.tHead.rows[0].cells[1]);
  assert.deepEqual([...outer.tBodies[0].rows].map((r) => r.cells[0].textContent.slice(0, 1)), ['C', 'B', 'k', 'A']);
  assert.ok(inner.hasAttribute('data-kit-sortable'));
  p.click(inner.tHead.rows[0].cells[0]);
  assert.deepEqual(firstCol(inner), ['y', 'z']);
  assert.deepEqual([...outer.tBodies[0].rows].map((r) => r.cells[0].textContent.slice(0, 1)), ['C', 'B', 'k', 'A']);
});

test('Move up from a real section menu moves the section and saves the order', () => {
  const p = page(row('a', panel('c1', '')) + row('b', panel('c2', '')));
  const second = p.d.querySelector('.row[data-row="b"]');
  p.click(second.querySelector('.row-menu [data-kit-menu]'));
  assert.equal(second.querySelector('.row-menu .p-pop').hidden, false);
  p.click(second.querySelector('button[data-row-move="-1"]'));
  assert.deepEqual([...p.pg.children].map((r) => r.dataset.row), ['b', 'a']);
  assert.deepEqual(JSON.parse(p.w.localStorage.getItem('dt.rowOrder')), { p: ['b', 'a'] });
  assert.equal(p.d.activeElement, second.querySelector('.row-menu [data-kit-menu]'));
});

test('a bars table built later gets Show as chart, and the table comes back when it goes', async () => {
  const p = page(row('a', panel('sto', '<div class="tbl-wrap" id="w"></div>')));
  p.$('w').innerHTML = TABLE([['x', '3'], ['y', '9']], 'data-kit-bars="Calls"');
  await tick();
  const item = p.d.querySelector('[data-card="sto"] button[data-kit-do="bars"]');
  assert.ok(item);
  p.click(item);
  const bars = p.d.querySelector('[data-card="sto"] .p-bars');
  assert.equal(bars.hidden, false);
  assert.equal(p.$('w').hidden, true);
  assert.match(bars.innerHTML, /width:100%/);
  p.$('w').innerHTML = '<div class="empty">No datastores reported.</div>';
  await tick();
  assert.equal(p.d.querySelector('[data-card="sto"] button[data-kit-do="bars"]'), null);
  assert.equal(p.$('w').hidden, false);
  assert.equal(bars.hidden, true);
});

test('prefix: dt keys read today\'s saved folds', () => {
  const p = page(row('a', panel('c1', '')) + row('b', panel('c2', '')), { seed: { 'dt.rows': JSON.stringify({ p: { a: 1 } }) } });
  assert.ok(p.d.querySelector('.row[data-row="a"]').classList.contains('folded'));
  assert.ok(!p.d.querySelector('.row[data-row="b"]').classList.contains('folded'));
});

test('kitRowFolded reads a saved fold and kitSetRowFold draws one without saving', () => {
  const p = page(row('a', panel('c1', '')) + row('b', panel('c2', '')) + row('c', panel('c3', '')),
    { seed: { 'dt.rows': JSON.stringify({ p: { a: 1, b: 0 }, q: 'junk' }) } });
  const st = p.w.kitStore(p.d);
  assert.equal(p.w.kitRowFolded('p', 'a', st), true);
  assert.equal(p.w.kitRowFolded('p', 'b', st), false);
  assert.equal(p.w.kitRowFolded('p', 'c', st), null);
  assert.equal(p.w.kitRowFolded('q', 'a', st), null);
  assert.equal(p.w.kitRowFolded('p', 'a', { getItem() { throw 1; } }), null);
  const c = p.d.querySelector('.row[data-row="c"]');
  p.w.kitSetRowFold(c, true);
  assert.ok(c.classList.contains('folded'));
  assert.equal(c.querySelector('button[data-row-fold]').getAttribute('aria-expanded'), 'false');
  assert.equal(c.querySelector('.chev').textContent, '▸');
  p.w.kitSetRowFold(c, false);
  assert.ok(!c.classList.contains('folded'));
  assert.doesNotThrow(() => p.w.kitSetRowFold(null, true));
  assert.deepEqual(JSON.parse(p.w.localStorage.getItem('dt.rows')), { p: { a: 1, b: 0 }, q: 'junk' });
});

test('prefix: other apps get their own keys', () => {
  const p = page(row('a', panel('c1', '')), { prefix: 'lsm' });
  p.click(p.d.querySelector('.row[data-row="a"] button[data-row-fold]'));
  assert.ok(p.d.querySelector('.row[data-row="a"]').classList.contains('folded'));
  assert.deepEqual(JSON.parse(p.w.localStorage.getItem('lsm.rows')), { p: { a: 1 } });
  assert.equal(p.w.localStorage.getItem('dt.rows'), null);
});

test('throwing store', () => {
  const store = { getItem() { throw 1; }, setItem() { throw 1; } };
  let p;
  assert.doesNotThrow(() => { p = page(row('a', panel('c1', '')), { store }); });
  assert.equal(p.w.kitStore(p.d), store);
  const fold = p.d.querySelector('.row[data-row="a"] button[data-row-fold]');
  assert.doesNotThrow(() => p.w.kitFoldClick(p.d, fold));
  assert.ok(p.d.querySelector('.row[data-row="a"]').classList.contains('folded'));
  assert.doesNotThrow(() => p.click(fold));
  assert.ok(!p.d.querySelector('.row[data-row="a"]').classList.contains('folded'));
});

test('copy without clipboard falls back', () => {
  const p = page('');
  Object.defineProperty(p.w.navigator, 'clipboard', { value: undefined, configurable: true });
  p.d.execCommand = () => true;
  p.w.kitCopy('a\tb', 'Copied');
  assert.equal(p.$('toast').textContent, 'Copied');
  p.d.execCommand = () => false;
  p.w.kitCopy('a\tb', 'Copied');
  assert.equal(p.$('toast').textContent, 'Could not copy');
});

// A wired jsdom page holding one ranked list with a filter key, so each row has Only show, Leave out and Copy.
function rankPage() {
  const p = page(row('a', panel('rk', '')));
  p.$('rkBody').innerHTML = p.w.rankHtml([{ label: 'llama 3', v: 5, val: 'llama3' }, { label: 'qwen', v: 3 }], { key: 'model' });
  return { ...p, btn: (i, act) => p.$('rkBody').querySelectorAll('li')[i].querySelector(`button[data-kit-act="${act}"]`) };
}

test('a rank Copy button copies that row label and toasts', () => {
  const p = rankPage(), copied = [];
  Object.defineProperty(p.w.navigator, 'clipboard', { value: undefined, configurable: true });
  p.d.execCommand = () => { copied.push(p.d.querySelector('textarea').value); return true; };
  p.click(p.btn(1, 'copy'));
  assert.deepEqual(copied, ['qwen']);
  assert.equal(p.$('toast').textContent, 'Copied');
});

test('rank Only show and Leave out fire a bubbling kit:act from the row', () => {
  const p = rankPage(), seen = [];
  p.d.addEventListener('kit:act', (ev) => seen.push({ tag: ev.target.tagName, ...ev.detail }));
  p.click(p.btn(0, 'only'));
  p.click(p.btn(1, 'out'));
  assert.deepEqual(seen, [
    { tag: 'LI', act: 'only', key: 'model', value: 'llama3' },
    { tag: 'LI', act: 'out', key: 'model', value: 'qwen' },
  ]);
  assert.equal(p.$('toast').textContent, '');
});

test('no dt literal left', () => {
  assert.doesNotMatch(KIT_SRC, /'dt\./);
  assert.doesNotMatch(KIT_SRC, /"dt\./);
});

// Loads kit.js into a bare context with no document, as a classic script, under the dt prefix.
function kit() {
  const ctx = vm.createContext({});
  vm.runInContext(KIT_SRC, ctx);
  ctx.kitConfigure({ prefix: 'dt' });
  return ctx;
}

// Matches one compound selector such as button[data-kit-do="full"] or .panel.full.
function matchOne(el, sel) {
  const tag = sel.match(/^[a-z0-9]+/);
  if (tag && el.tag !== tag[0]) return false;
  for (const m of sel.matchAll(/\.([\w-]+)/g)) if (!el.classList.contains(m[1])) return false;
  for (const m of sel.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
    if (!el.hasAttribute(m[1]) || (m[2] !== undefined && el.getAttribute(m[1]) !== m[2])) return false;
  }
  return true;
}
// A small finite DOM element: attributes, classes, tree walks and recorded HTML inserts.
class El {
  constructor(tag, attrs = {}, kids = []) {
    this.tag = tag; this.attrs = { ...attrs }; this.children = []; this.parentNode = null;
    this.hidden = false; this.text = ''; this.style = {}; this.inserted = []; this.focused = false; this.offsetWidth = 0;
    const self = this;
    this.classList = {
      contains: (c) => (self.attrs.class || '').split(' ').includes(c),
      toggle: (c, on) => { if (on) self.classList.add(c); else self.classList.remove(c); },
      add: (c) => { if (!self.classList.contains(c)) self.attrs.class = ((self.attrs.class || '') + ' ' + c).trim(); },
      remove: (c) => { self.attrs.class = (self.attrs.class || '').split(' ').filter((x) => x !== c).join(' '); },
    };
    kids.forEach((k) => { k.parentNode = this; this.children.push(k); });
  }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = String(v); }
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join('') : this.text; }
  set textContent(v) { this.text = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  hasAttribute(k) { return k in this.attrs; }
  matches(sel) {
    const parts = sel.split(' ');
    if (!matchOne(this, parts.pop())) return false;
    let up = this.parentNode;
    while (parts.length && up) { if (matchOne(up, parts[parts.length - 1])) parts.pop(); up = up.parentNode; }
    return parts.length === 0;
  }
  closest(sel) { for (let e = this; e; e = e.parentNode) if (e.matches(sel)) return e; return null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (e) => e.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  get nextElementSibling() { const s = this.parentNode ? this.parentNode.children : []; return s[s.indexOf(this) + 1] || null; }
  get previousElementSibling() { const s = this.parentNode ? this.parentNode.children : []; return s[s.indexOf(this) - 1] || null; }
  removeChild(k) { this.children.splice(this.children.indexOf(k), 1); k.parentNode = null; }
  appendChild(k) { return this.insertBefore(k, null); }
  insertBefore(k, ref) {
    if (k.parentNode) k.parentNode.removeChild(k);
    const at = ref ? this.children.indexOf(ref) : this.children.length;
    this.children.splice(at, 0, k);
    k.parentNode = this;
    return k;
  }
  get nextSibling() { return this.nextElementSibling; }
  removeAttribute(k) { delete this.attrs[k]; }
  setPointerCapture(id) { this.captured = id; }
  insertAdjacentHTML(pos, html) { this.inserted.push(html); }
  focus() { this.focused = true; }
  getBoundingClientRect() { return { left: 0, width: 600 }; }
}
const el = (tag, attrs, kids) => new El(tag, attrs, kids);
function text(tag, attrs, value) { const e = el(tag, attrs); e.textContent = value; return e; }

// One kit panel: menu button, hidden menu, body and an optional hidden table of a header and one row.
function fakePanel(withTable) {
  const items = ['table', 'copy', 'full'].map((d) => text('button', { 'data-kit-do': d }, d));
  const menu = el('button', { class: 'p-menu', 'data-kit-menu': 'a', 'aria-expanded': 'false' });
  const pop = el('div', { class: 'p-pop' }, items);
  pop.hidden = true;
  const body = el('div', { class: 'p-body' });
  const kids = [el('header', { class: 'p-hd' }, [menu, pop]), body];
  let table = null;
  if (withTable) {
    table = el('div', { class: 'p-table' }, [el('table', {}, [
      el('tr', {}, [text('th', {}, 'name'), text('th', {}, 'n')]),
      el('tr', {}, [text('td', {}, 'row'), text('td', { 'data-v': '1234' }, '1.23k')]),
    ])]);
    table.hidden = true;
    kids.push(table);
  }
  const panel = el('section', { class: 'panel' }, kids);
  return { panel, menu, pop, body, table, item: (d) => items.find((i) => i.getAttribute('data-kit-do') === d) };
}
// A wired page in a vm context under the dt prefix: fires events at the document's recorded listeners.
function wired(kids, opts = {}) {
  const root = el('html', {}, kids);
  const toast = el('div', { id: 'toast' });
  const on = {}, winOn = {}, copied = [];
  const add = (store) => (type, fn) => { (store[type] = store[type] || []).push(fn); };
  const body = el('body');
  const doc = {
    body,
    activeElement: opts.active || null,
    createElement: (tag) => Object.assign(el(tag), { style: {}, select() { this.selected = true; } }),
    execCommand: (cmd) => {
      const ta = body.children.find((c) => c.selected);
      if (cmd !== 'copy' || !ta || opts.execCopy === false) return false;
      copied.push(ta.value);
      return true;
    },
    addEventListener: add(on),
    defaultView: { addEventListener: add(winOn), localStorage: opts.store, innerHeight: 800, scrollBy: () => {} },
    querySelectorAll: (s) => root.querySelectorAll(s),
    getElementById: (id) => (id === 'toast' ? toast : root.querySelector('[id="' + id + '"]')),
    elementFromPoint: (x, y) => (opts.at ? opts.at(x, y) : null),
    documentElement: root,
  };
  const ctx = vm.createContext({
    document: doc, setTimeout: opts.setTimeout || (() => 0), clearTimeout: () => {},
    navigator: opts.navigator || { clipboard: { writeText: (t) => { copied.push(t); return Promise.resolve(); } } },
    ...(opts.MutationObserver ? { MutationObserver: opts.MutationObserver } : {}),
  });
  vm.runInContext(KIT_SRC, ctx);
  ctx.kitConfigure({ prefix: 'dt' });
  ctx.kitWire(doc);
  const fire = (type, ev) => (on[type] || []).forEach((fn) => fn(ev));
  return { ctx, on, winOn, copied, toast, body, fire, click: (target) => fire('click', { target }) };
}

test('kitWire attaches its listeners only when called', () => {
  const p = wired([]);
  for (const type of ['click', 'keydown', 'mousemove', 'mouseout', 'mouseleave']) assert.equal(p.on[type].length, 1, type);
  assert.equal(p.winOn.blur.length, 1);
});

test('menu opens, closes, and one open menu closes another', () => {
  const a = fakePanel(), b = fakePanel(), outside = el('div');
  const p = wired([a.panel, b.panel, outside]);
  p.click(a.menu);
  assert.equal(a.pop.hidden, false);
  assert.equal(a.menu.getAttribute('aria-expanded'), 'true');
  p.click(b.menu);
  assert.equal(a.pop.hidden, true);
  assert.equal(a.menu.getAttribute('aria-expanded'), 'false');
  assert.equal(b.pop.hidden, false);
  p.click(outside);
  assert.equal(b.pop.hidden, true);
  p.click(a.menu);
  p.click(a.menu);
  assert.equal(a.pop.hidden, true);
});

test('Show as table swaps the body and table and closes the menu', () => {
  const a = fakePanel(true);
  const p = wired([a.panel]);
  p.click(a.menu);
  p.click(a.item('table'));
  assert.equal(a.pop.hidden, true);
  assert.equal(a.table.hidden, false);
  assert.equal(a.body.hidden, true);
  assert.equal(a.item('table').textContent, 'Show as chart');
  p.click(a.item('table'));
  assert.equal(a.table.hidden, true);
  assert.equal(a.body.hidden, false);
  assert.equal(a.item('table').textContent, 'Show as table');
});

test('Copy data copies the table as CSV with raw numbers', async () => {
  const a = fakePanel(true);
  const p = wired([a.panel]);
  p.click(a.item('copy'));
  assert.equal(p.copied[0], 'name,n\nrow,1234');
  await Promise.resolve();
  assert.equal(p.toast.textContent, 'Copied as CSV');
});

test('Copy data works on a plain http page, which has no clipboard API', () => {
  const a = fakePanel(true), active = el('button');
  const p = wired([a.panel], { navigator: {}, active });
  p.click(a.item('copy'));
  assert.equal(p.copied[0], 'name,n\nrow,1234');
  assert.equal(p.toast.textContent, 'Copied as CSV');
  assert.equal(p.body.children.length, 0);
  assert.equal(active.focused, true);
});

test('Copy data still works when the browser refuses the clipboard API', async () => {
  const a = fakePanel(true);
  const p = wired([a.panel], { navigator: { clipboard: { writeText: () => Promise.reject(new Error('denied')) } } });
  p.click(a.item('copy'));
  await new Promise(setImmediate);
  assert.equal(p.copied[0], 'name,n\nrow,1234');
  assert.equal(p.toast.textContent, 'Copied as CSV');
  assert.equal(p.body.children.length, 0);
});

test('Copy data says so when no way of copying works', () => {
  const a = fakePanel(true);
  const p = wired([a.panel], { navigator: {}, execCopy: false });
  p.click(a.item('copy'));
  assert.equal(p.copied.length, 0);
  assert.equal(p.toast.textContent, 'Could not copy');
  assert.equal(p.body.children.length, 0);
});

test('Copy data says so when the panel has no table', () => {
  const a = fakePanel(false);
  const p = wired([a.panel]);
  p.click(a.item('copy'));
  assert.equal(p.copied.length, 0);
  assert.equal(p.toast.textContent, 'This panel has no data to copy');
});

test('Copy data falls back to the first table in the panel body', () => {
  const a = fakePanel(false);
  a.body.appendChild(el('table', {}, [el('tr', {}, [text('td', {}, 'one'), text('td', { 'data-v': '5' }, '5')])]));
  const p = wired([a.panel]);
  p.click(a.item('copy'));
  assert.equal(p.copied[0], 'one,5');
});

test('Copy data prefers the hidden table to a table in the body', () => {
  const a = fakePanel(true);
  a.body.appendChild(el('table', {}, [el('tr', {}, [text('td', {}, 'body')])]));
  const p = wired([a.panel]);
  p.click(a.item('copy'));
  assert.equal(p.copied[0], 'name,n\nrow,1234');
});

test('Copy data skips a table under data-kit-nocopy', async () => {
  const bodyTable = () => el('table', {}, [el('tr', {}, [text('td', {}, 'one'), text('td', { 'data-v': '5' }, '5')])]);
  const off = fakePanel(false);
  off.body.appendChild(el('div', { 'data-kit-nocopy': '' }, [bodyTable()]));
  const p = wired([off.panel]);
  p.click(off.item('copy'));
  await Promise.resolve();
  assert.equal(p.copied.length, 0);
  assert.equal(p.toast.textContent, 'This panel has no data to copy');
  const on = fakePanel(false);
  on.body.appendChild(el('div', {}, [bodyTable()]));
  const q = wired([on.panel]);
  q.click(on.item('copy'));
  await Promise.resolve();
  assert.equal(q.copied[0], 'one,5');
  assert.equal(q.toast.textContent, 'Copied as CSV');
  assert.doesNotMatch(KIT_SRC, /group-presets/);
});

test('kitSetTable adds one hidden table after the body, and a second call replaces its rows', () => {
  const a = fakePanel(false);
  a.body.setAttribute('id', 'b1');
  const p = wired([a.panel]);
  p.ctx.kitSetTable('b1', { cols: ['Endpoint', 'This run'], rows: [['one', 1.5]] });
  const holder = a.panel.querySelector('.p-table');
  assert.equal(holder.hidden, true);
  assert.equal(a.body.nextElementSibling, holder);
  assert.match(holder.innerHTML, /Endpoint.*one/);
  p.ctx.kitSetTable('b1', { cols: ['Endpoint'], rows: [['two']] });
  assert.equal(a.panel.querySelectorAll('.p-table').length, 1);
  assert.match(holder.innerHTML, /two/);
  assert.doesNotMatch(holder.innerHTML, /one/);
});

test('kitSetTable ignores a missing body or table, and kitFill still fills the table', () => {
  const a = fakePanel(false);
  a.body.setAttribute('id', 'b1');
  const p = wired([a.panel]);
  p.ctx.kitSetTable('nope', { cols: ['x'], rows: [] });
  p.ctx.kitSetTable('b1', null);
  assert.equal(a.panel.querySelector('.p-table'), null);
  p.ctx.kitFill('b1', '<i>x</i>');
  assert.equal(a.body.innerHTML, '<i>x</i>');
  assert.equal(a.panel.querySelector('.p-table'), null);
  p.ctx.kitFill('b1', '<i>y</i>', { cols: ['Col'], rows: [['cell']] });
  assert.equal(a.body.innerHTML, '<i>y</i>');
  assert.match(a.panel.querySelector('.p-table').innerHTML, /Col.*cell/);
});

test('Open full size toggles, and Escape closes the menu before full size', () => {
  const a = fakePanel();
  const p = wired([a.panel]);
  const esc = () => p.fire('keydown', { key: 'Escape' });
  p.click(a.item('full'));
  assert.ok(a.panel.classList.contains('full'));
  assert.equal(a.item('full').textContent, 'Close full size');
  p.click(a.menu);
  esc();
  assert.equal(a.pop.hidden, true);
  assert.equal(a.menu.focused, true);
  assert.ok(a.panel.classList.contains('full'));
  p.fire('keydown', { key: 'a' });
  assert.ok(a.panel.classList.contains('full'));
  esc();
  assert.ok(!a.panel.classList.contains('full'));
  assert.equal(a.item('full').textContent, 'Open full size');
});

const TS_OPTS = { id: 'a', labels: ['d1', 'd2'], mode: 'line', height: 220, w: 600, l: 34, r: 8, series: [{ name: 'S', values: [5, 7], slot: 1 }, { name: 'U', values: [1, 2], slot: 2 }], marks: [] };
function fakeTs() {
  const xh = el('line', { class: 'xh', visibility: 'hidden' }), tip = el('div', { class: 'tip' });
  tip.hidden = true;
  const ts = el('div', { class: 'ts', 'data-kit-ts': JSON.stringify(TS_OPTS) }, [el('svg', {}, [xh]), tip]);
  return { ts, xh, tip };
}

test('legend chip hides a series and redraws its chart at the same width', () => {
  const { ts } = fakeTs();
  const chip = el('button', { 'data-kit-series': 'a|S' });
  const legend = el('div', { class: 'legend' }, [chip]);
  const body = el('div', { class: 'p-body' }, [legend, ts]);
  const p = wired([body]);
  p.click(chip);
  assert.equal(p.ctx.KIT_SERIES_OFF['a|S'], true);
  assert.deepEqual(body.children, []);
  const html = ts.inserted[0];
  assert.match(html, /data-kit-series="a\|S" aria-pressed="false"/);
  assert.match(html, /data-kit-series="a\|U" aria-pressed="true"/);
  assert.match(html, /viewBox="0 0 600 220"/);
});

test('chart hover shows the nearest label and leaving hides it', () => {
  const { ts, xh, tip } = fakeTs();
  const p = wired([ts]);
  p.fire('mousemove', { target: ts, clientX: 500 });
  assert.equal(xh.getAttribute('visibility'), 'visible');
  assert.equal(tip.hidden, false);
  assert.match(tip.innerHTML, /d2/);
  assert.match(tip.innerHTML, /S<b>7<\/b>/);
  p.fire('mouseout', { relatedTarget: ts });
  assert.equal(tip.hidden, false);
  p.fire('mouseout', { relatedTarget: null });
  assert.equal(xh.getAttribute('visibility'), 'hidden');
  assert.equal(tip.hidden, true);
  p.fire('mousemove', { target: ts, clientX: 500 });
  p.winOn.blur[0]();
  assert.equal(tip.hidden, true);
});

test('chart hover leaves out a hidden series', () => {
  const { ts, tip } = fakeTs();
  const p = wired([ts]);
  p.ctx.KIT_SERIES_OFF['a|S'] = true;
  p.fire('mousemove', { target: ts, clientX: 500 });
  assert.doesNotMatch(tip.innerHTML, /S<b>/);
  assert.match(tip.innerHTML, /U<b>2<\/b>/);
});

// A store over a plain object, recording nothing beyond its keys.
function memStore(init = {}) {
  const data = { ...init };
  return { data, getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, json: (k) => JSON.parse(data[k]) };
}
const throwingStore = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
const rowIds = (root) => root.children.filter((c) => c.hasAttribute('data-row')).map((c) => c.getAttribute('data-row'));
const fakeRow = (id, attrs = {}, kids = []) => el('div', { class: 'row', 'data-row': id, ...attrs }, [el('button', { 'data-row-fold': id, 'aria-expanded': 'true' }, [el('span', { class: 'chev' })]), ...kids]);
const fakeCard = (id) => el('section', { class: 'panel', 'data-card': id }, [el('header', { class: 'p-hd' }, [el('span', { class: 'grip' })])]);
const fakeBox = (key, ids) => el('div', { class: 'panels', 'data-cards': key }, ids.map(fakeCard));
const cardIds = (box) => box.children.map((c) => c.getAttribute('data-card'));

test('kitReconcile keeps saved order, drops unknown ids and places new ones', () => {
  const k = kit(), plain = (v) => JSON.parse(JSON.stringify(v));
  assert.deepEqual(plain(k.kitReconcile(['a', 'b', 'c'], ['c', 'x', 'a'])), ['c', 'a', 'b']);
  assert.deepEqual(plain(k.kitReconcile(['a', 'b', 'c'], ['b', 'a'])), ['b', 'a', 'c']);
  assert.deepEqual(plain(k.kitReconcile(['a', 'b'], 'junk')), ['a', 'b']);
  assert.deepEqual(plain(k.kitReconcile(['a', 'b'], [1, null, 'b', 'b', 'a'])), ['b', 'a']);
});

test('junk saved JSON, a throwing store and no store leave the default layout', () => {
  const junk = [
    memStore({ 'dt.rows': '{nope', 'dt.rowOrder': '[1,2', 'dt.order': 'x' }),
    memStore({ 'dt.rows': '[1]', 'dt.rowOrder': '"a"', 'dt.order': '7' }),
    memStore({ 'dt.rows': '{"p":[1]}', 'dt.rowOrder': '{"p":"b"}', 'dt.order': '{"p":{"box":"c2"}}' }),
    memStore({ 'dt.rows': '{"p":{"a":"yes"}}', 'dt.rowOrder': '{"p":{"x":1}}', 'dt.order': '{"p":[1]}' }),
    throwingStore, null, undefined,
  ];
  for (const store of junk) {
    const box = fakeBox('box', ['c1', 'c2']);
    const root = el('div', {}, [fakeRow('a', {}, [box]), fakeRow('b', { 'data-folded': '' })]);
    const k = kit();
    k.kitLayout('p', root, store);
    assert.deepEqual(rowIds(root), ['a', 'b']);
    assert.deepEqual(cardIds(box), ['c1', 'c2']);
    assert.ok(!root.children[0].classList.contains('folded'));
    assert.ok(root.children[1].classList.contains('folded'));
    k.kitLayoutReset('p', root, store);
    assert.deepEqual(rowIds(root), ['a', 'b']);
  }
});

test('a saved fold survives kitLayout, also over the default state', () => {
  const store = memStore({ 'dt.rows': JSON.stringify({ p: { a: 1, b: 0 } }) });
  const root = el('div', {}, [fakeRow('a'), fakeRow('b', { 'data-folded': '' }), fakeRow('c')]);
  kit().kitLayout('p', root, store);
  assert.ok(root.children[0].classList.contains('folded'));
  assert.equal(root.children[0].querySelector('button[data-row-fold]').getAttribute('aria-expanded'), 'false');
  assert.equal(root.children[0].querySelector('.chev').textContent, '▸');
  assert.ok(!root.children[1].classList.contains('folded'));
  assert.equal(root.children[1].querySelector('.chev').textContent, '▾');
  assert.ok(!root.children[2].classList.contains('folded'));
});

test('kitLayout orders rows in their own places and leaves other content where it is', () => {
  const tiles = el('div', { class: 'kpis' });
  const root = el('div', {}, [fakeRow('a'), fakeRow('b'), tiles, fakeRow('c')]);
  kit().kitLayout('p', root, memStore({ 'dt.rowOrder': JSON.stringify({ p: ['c', 'gone', 'a'] }) }));
  assert.deepEqual(rowIds(root), ['c', 'a', 'b']);
  assert.equal(root.children[2], tiles);
});

test('kitLayout orders panels per box, with new panels at their default place', () => {
  const one = fakeBox('one', ['c1', 'c2', 'c3']), two = fakeBox('two', ['c4', 'c5']);
  const root = el('div', {}, [fakeRow('a', {}, [one]), fakeRow('b', {}, [two])]);
  kit().kitLayout('p', root, memStore({ 'dt.order': JSON.stringify({ p: { one: ['c3', 'c1'] }, q: { two: ['c5', 'c4'] } }) }));
  assert.deepEqual(cardIds(one), ['c3', 'c1', 'c2']);
  assert.deepEqual(cardIds(two), ['c4', 'c5']);
});

test('kitLayoutReset clears only that page, even with a row id both pages use', () => {
  const store = memStore({
    'dt.rows': JSON.stringify({ p: { a: 1, b: 1 }, q: { a: 1 } }),
    'dt.rowOrder': JSON.stringify({ p: ['b', 'a'], q: ['y', 'a'] }),
    'dt.order': JSON.stringify({ p: { one: ['c2', 'c1'] }, q: { one: ['c9'] } }),
  });
  const k = kit(), one = fakeBox('one', ['c1', 'c2']);
  const page = el('div', {}, [fakeRow('a', {}, [one]), fakeRow('b', { 'data-folded': '' })]);
  const other = el('div', {}, [fakeRow('a'), fakeRow('y')]);
  k.kitLayout('p', page, store);
  k.kitLayout('q', other, store);
  assert.deepEqual(rowIds(page), ['b', 'a']);
  assert.deepEqual(cardIds(one), ['c2', 'c1']);
  k.kitLayoutReset('p', page, store);
  assert.deepEqual(rowIds(page), ['a', 'b']);
  assert.deepEqual(cardIds(one), ['c1', 'c2']);
  assert.ok(!page.children[0].classList.contains('folded'));
  assert.ok(page.children[1].classList.contains('folded'));
  assert.deepEqual(store.json('dt.rows'), { q: { a: 1 } });
  assert.deepEqual(store.json('dt.rowOrder'), { q: ['y', 'a'] });
  assert.deepEqual(store.json('dt.order'), { q: { one: ['c9'] } });
  assert.deepEqual(rowIds(other), ['y', 'a']);
  assert.ok(other.children[1].classList.contains('folded'));
});

const COLOR_SERIES = [{ name: 'Passed', values: [1, 2], slot: 1 }, { name: 'Failed', values: [3, 4], color: 'bad' }, { name: 'Cancelled', values: [1, 1], slot: 5 }];
test('a series colour survives a chart redraw and shows in the hover', () => {
  const k0 = kit();
  const html = k0.tsHtml({ id: 'c', labels: ['d1', 'd2'], mode: 'line', series: COLOR_SERIES });
  const stored = html.match(/data-kit-ts="([^"]*)"/)[1].replace(/&quot;/g, '"');
  const opts = JSON.parse(stored);
  assert.equal(opts.series[1].color, 'bad');
  const tip = el('div', { class: 'tip' });
  const ts = el('div', { class: 'ts', 'data-kit-ts': stored }, [el('svg'), tip]);
  const p = wired([el('div', {}, [ts])]);
  p.fire('mousemove', { target: ts, clientX: 500 });
  assert.match(tip.innerHTML, /<i style="background:var\(--bad\)"><\/i>Failed/);
  ts.parentNode.clientWidth = 900;
  p.ctx.kitRefit(ts.parentNode);
  assert.match(ts.inserted[0], /viewBox="0 0 900 220"/);
  assert.match(ts.inserted[0], /stroke="var\(--bad\)"/);
});

const LANE_OPTS = {
  id: 'day', now: '13:30', lanes: ['smoketest', 'group: a really long group name'],
  marks: [
    { lane: 'smoketest', at: '00:00', state: 'passed', key: 0, tip: 'first <one>' },
    { lane: 'smoketest', at: '12:15', state: 'failed', key: 1, tip: 'second' },
    { lane: 'group: a really long group name', at: '23:59', state: 'come', key: 2, tip: 'third' },
    { lane: 'group: a really long group name', at: '24:00', state: 'skipped', key: 3, tip: 'fourth' },
  ],
};
// A lanes chart in a body, from real lanesHtml options, with one hoverable mark.
function fakeLanes(w = 600, extra = {}) {
  const html = kit().lanesHtml({ ...LANE_OPTS, w, ...extra });
  const stored = html.match(/data-kit-lanes="([^"]*)"/)[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
  const mark = el('g', { 'data-mark': '1', 'data-tip': 'second' }), tip = el('div', { class: 'tip' }), other = el('text');
  tip.hidden = true;
  const lanes = el('div', { class: 'ts lanes', 'data-kit-lanes': stored }, [el('svg', {}, [mark, other]), tip]);
  const body = el('div', { class: 'p-body' }, [lanes]);
  return { body, lanes, mark, other, tip };
}

test('hovering a mark shows the tooltip with its tip, and moving off hides it', () => {
  const { body, lanes, mark, other, tip } = fakeLanes();
  const p = wired([body]);
  p.fire('mousemove', { target: mark, clientX: 100 });
  assert.equal(tip.hidden, false);
  assert.equal(tip.textContent, 'second');
  assert.equal(tip.style.left, '112px');
  p.fire('mousemove', { target: other, clientX: 100 });
  assert.equal(tip.hidden, true);
  p.fire('mousemove', { target: mark, clientX: 100 });
  p.fire('mousemove', { target: body, clientX: 100 });
  assert.equal(tip.hidden, true);
  assert.equal(lanes.parentNode, body);
});

test('kitRefit redraws lanes at the body width, and only when it changed', () => {
  const same = fakeLanes(600);
  same.body.clientWidth = 600;
  const p = wired([same.body]);
  p.ctx.kitRefit(same.body);
  assert.equal(same.lanes.inserted.length, 0);
  same.body.clientWidth = 0;
  p.ctx.kitRefit(same.body);
  assert.equal(same.lanes.inserted.length, 0);
  same.body.clientWidth = 300;
  p.ctx.kitRefit(same.body);
  assert.match(same.lanes.inserted[0], /viewBox="0 0 300 /);
  assert.match(same.lanes.inserted[0], /first &lt;one&gt;/);
  assert.deepEqual(same.body.children, []);
});

test('kitRefit keeps a custom empty text and aria label on the redrawn lanes', () => {
  const f = fakeLanes(600, { empty: 'No scheduled runs today.', label: 'Scheduled runs by hour' });
  f.body.clientWidth = 300;
  const p = wired([f.body]);
  p.ctx.kitRefit(f.body);
  const html = f.lanes.inserted[0];
  assert.match(html, /viewBox="0 0 300 /);
  assert.match(html, /role="group" aria-label="Scheduled runs by hour"/);
  const stored = JSON.parse(html.match(/data-kit-lanes="([^"]*)"/)[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>'));
  assert.equal(stored.empty, 'No scheduled runs today.');
  assert.equal(stored.label, 'Scheduled runs by hour');
  assert.equal(p.ctx.lanesHtml({ ...stored, marks: [] }), '<div class="empty">No scheduled runs today.</div>');
});

test('a fold click folds a row, a second reopens it, and both save per page', () => {
  const store = memStore();
  const row = fakeRow('a'), root = el('div', {}, [row]);
  const fold = row.querySelector('button[data-row-fold]');
  const p = wired([root], { store });
  p.ctx.kitLayout('p', root, store);
  p.click(fold.children[0]);
  assert.ok(row.classList.contains('folded'));
  assert.equal(fold.getAttribute('aria-expanded'), 'false');
  assert.deepEqual(store.json('dt.rows'), { p: { a: 1 } });
  p.click(fold);
  assert.ok(!row.classList.contains('folded'));
  assert.equal(fold.getAttribute('aria-expanded'), 'true');
  assert.deepEqual(store.json('dt.rows'), { p: { a: 0 } });
});

test('a fold click works with a throwing store and with no layout, and leaves other pages alone', () => {
  for (const store of [throwingStore, undefined]) {
    const row = fakeRow('a'), root = el('div', {}, [row]);
    const p = wired([root], { store });
    p.click(row.querySelector('button[data-row-fold]'));
    assert.ok(row.classList.contains('folded'));
  }
  const store = memStore({ 'dt.rows': JSON.stringify({ q: { a: 1 } }) });
  const row = fakeRow('a'), root = el('div', {}, [row]);
  const p = wired([root], { store });
  p.ctx.kitLayout('p', root, store);
  p.click(row.querySelector('button[data-row-fold]'));
  assert.deepEqual(store.json('dt.rows'), { q: { a: 1 }, p: { a: 1 } });
});

test('reopening a row redraws its charts at their real width', () => {
  const { ts } = fakeTs();
  const body = el('div', { class: 'p-body' }, [ts]);
  body.clientWidth = 900;
  const row = fakeRow('a', { class: 'row folded' }, [body]);
  const p = wired([row]);
  p.click(row.querySelector('button[data-row-fold]'));
  assert.match(ts.inserted[0], /viewBox="0 0 900 220"/);
});

test('full size keeps the chart svg node and calls onRefit once with the panel, opening and closing', () => {
  const a = fakePanel();
  const { ts } = fakeTs();
  a.body.children.push(ts); ts.parentNode = a.body;
  a.body.clientWidth = 1200;
  const p = wired([a.panel]), seen = [], svg = ts.querySelector('svg');
  p.ctx.kitConfigure({ onRefit: (root) => seen.push(root) });
  p.ctx.kitSetFull(a.panel, true);
  assert.ok(a.panel.classList.contains('full'));
  assert.equal(ts.inserted.length, 0);
  assert.equal(a.body.querySelector('svg'), svg);
  assert.equal(seen.length, 1);
  assert.equal(seen[0], a.panel);
  p.click(a.item('full'));
  p.click(a.item('full'));
  p.fire('keydown', { key: 'Escape' });
  assert.ok(!a.panel.classList.contains('full'));
  assert.equal(ts.inserted.length, 0);
  assert.equal(ts.parentNode, a.body);
  assert.deepEqual(seen, [a.panel, a.panel, a.panel, a.panel]);
});

test('resize refits the document once per 150 ms at most', () => {
  const timers = [];
  const { ts } = fakeTs();
  const body = el('div', {}, [ts]);
  body.clientWidth = 900;
  const p = wired([body], { setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; } });
  assert.equal(p.winOn.resize.length, 1);
  p.winOn.resize[0]();
  p.winOn.resize[0]();
  p.winOn.resize[0]();
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 150);
  assert.equal(ts.inserted.length, 0);
  timers[0].fn();
  assert.equal(ts.inserted.length, 1);
  p.winOn.resize[0]();
  assert.equal(timers.length, 2);
});

const grip = (card) => card.querySelector('.grip');
test('touch drag moves a panel within its box and saves the order per page', () => {
  const store = memStore();
  const box = fakeBox('one', ['c1', 'c2', 'c3']), root = el('div', {}, [box]);
  const [c1, , c3] = box.children;
  let over = c3;
  const p = wired([root], { store, at: () => over });
  p.ctx.kitLayout('p', root, store);
  let prevented = false;
  p.fire('pointerdown', { target: grip(c1), pointerType: 'touch', pointerId: 7, preventDefault: () => { prevented = true; } });
  assert.ok(prevented);
  assert.equal(grip(c1).captured, 7);
  assert.ok(c1.classList.contains('dragging'));
  p.fire('pointermove', { pointerId: 8, clientX: 1, clientY: 100 });
  assert.deepEqual(cardIds(box), ['c1', 'c2', 'c3']);
  p.fire('pointermove', { pointerId: 7, clientX: 1, clientY: 100 });
  assert.deepEqual(cardIds(box), ['c2', 'c3', 'c1']);
  p.fire('pointerup', { pointerId: 7, pointerType: 'touch' });
  assert.ok(!c1.classList.contains('dragging'));
  assert.deepEqual(store.json('dt.order'), { p: { one: ['c2', 'c3', 'c1'] } });
  over = box.children[0];
  p.fire('pointerdown', { target: grip(c1), pointerType: 'touch', pointerId: 7, preventDefault: () => {} });
  p.fire('pointermove', { pointerId: 7, clientX: 1, clientY: 100 });
  p.fire('pointerup', { pointerId: 7, pointerType: 'touch' });
  assert.deepEqual(cardIds(box), ['c1', 'c2', 'c3']);
  assert.deepEqual(store.json('dt.order'), {});
});

test('mouse drag uses native dragging from the grip, and a plain press leaves nothing draggable', () => {
  const store = memStore();
  const box = fakeBox('one', ['c1', 'c2']), root = el('div', {}, [box]);
  const [c1, c2] = box.children;
  const p = wired([root], { store, at: () => c2 });
  p.ctx.kitLayout('p', root, store);
  p.fire('pointerdown', { target: grip(c1), pointerType: 'mouse', pointerId: 1 });
  assert.equal(c1.getAttribute('draggable'), 'true');
  p.fire('pointerup', { pointerType: 'mouse', pointerId: 1 });
  assert.equal(c1.hasAttribute('draggable'), false);
  p.fire('pointerdown', { target: grip(c1), pointerType: 'mouse', pointerId: 1 });
  const moved = [];
  p.fire('dragstart', { target: c1, dataTransfer: { setData: (k, v) => moved.push(v) } });
  assert.deepEqual(moved, ['c1']);
  let stopped = 0;
  p.fire('dragover', { clientX: 1, clientY: 1, preventDefault: () => { stopped++; } });
  assert.equal(stopped, 1);
  assert.deepEqual(cardIds(box), ['c2', 'c1']);
  p.fire('dragend', {});
  assert.equal(c1.hasAttribute('draggable'), false);
  assert.deepEqual(store.json('dt.order'), { p: { one: ['c2', 'c1'] } });
});

test('a panel outside a card box, a grip outside a panel and a panel in another box do not move', () => {
  const lone = fakeCard('c9');
  const free = el('div', {}, [lone]);
  const box = fakeBox('one', ['c1', 'c2']), otherBox = fakeBox('two', ['c3']);
  const stray = el('span', { class: 'grip' });
  const p = wired([free, box, otherBox, stray], { at: () => otherBox.children[0] });
  p.fire('pointerdown', { target: grip(lone), pointerType: 'mouse', pointerId: 1 });
  assert.equal(lone.hasAttribute('draggable'), false);
  p.fire('pointerdown', { target: stray, pointerType: 'mouse', pointerId: 1 });
  p.fire('pointerdown', { target: grip(box.children[0]), pointerType: 'touch', pointerId: 2, preventDefault: () => {} });
  p.fire('pointermove', { pointerId: 2, clientX: 1, clientY: 100 });
  p.fire('pointerup', { pointerId: 2, pointerType: 'touch' });
  assert.deepEqual(cardIds(box), ['c1', 'c2']);
  assert.deepEqual(cardIds(otherBox), ['c3']);
});

test('a mouse pointerup leaves draggable on a card outside a card box', () => {
  const lone = fakeCard('c9');
  lone.setAttribute('draggable', 'true');
  const box = fakeBox('one', ['c1']);
  box.children[0].setAttribute('draggable', 'true');
  const p = wired([el('div', {}, [lone]), box]);
  p.fire('pointerup', { pointerType: 'mouse', pointerId: 1 });
  assert.equal(lone.getAttribute('draggable'), 'true');
  assert.equal(box.children[0].hasAttribute('draggable'), false);
});

test('a native dragstart on a draggable card outside a card box is left to the page', () => {
  const lone = fakeCard('c9');
  lone.setAttribute('draggable', 'true');
  const free = el('div', {}, [lone]);
  const p = wired([free], { at: () => lone });
  const sent = [];
  p.fire('dragstart', { target: lone, dataTransfer: { setData: (k, v) => sent.push(v) } });
  assert.equal(p.ctx.kitDrag, null);
  assert.ok(!lone.classList.contains('dragging'));
  assert.deepEqual(sent, []);
  let stopped = 0;
  p.fire('dragover', { clientX: 1, clientY: 1, preventDefault: () => { stopped++; } });
  assert.equal(stopped, 0);
});

// A row as kitRowDress leaves it: grip, fold button, and a ⋯ menu with Move up / Move down.
function movableRow(id, attrs = {}, kids = []) {
  const up = text('button', { 'data-row-move': '-1' }, 'Move up'), dn = text('button', { 'data-row-move': '1' }, 'Move down');
  const pop = el('div', { class: 'p-pop' }, [up, dn]);
  pop.hidden = true;
  const menu = el('div', { class: 'row-menu' }, [el('button', { class: 'p-menu', 'data-kit-menu': '', 'aria-expanded': 'false' }), pop]);
  const row = el('div', { class: 'row', 'data-row': id, 'data-kit-dressed': '', ...attrs },
    [el('span', { class: 'grip row-grip' }), el('button', { 'data-row-fold': id }), menu, ...kids]);
  return { row, up, dn, menu: menu.children[0] };
}

test('kitRowDress adds a grip and a Move up / Move down menu once', () => {
  const k = kit(), a = fakeRow('a'), b = fakeRow('b'), root = el('div', {}, [a, b]);
  k.kitRowDress(root);
  k.kitRowDress(root);
  for (const row of [a, b]) {
    const fold = row.querySelector('button[data-row-fold]');
    assert.equal(fold.inserted.length, 2);
    assert.match(fold.inserted.join(''), /class="grip row-grip"/);
    assert.match(fold.inserted.join(''), /data-row-move="-1">Move up<\/button><button type="button" role="menuitem" data-row-move="1">Move down/);
  }
});

test('kitMoveRow moves a row and saves dt.rowOrder, dropping it at the default', () => {
  const store = memStore(), rows = ['a', 'b', 'c'].map((id) => movableRow(id)), root = el('div', {}, rows.map((r) => r.row));
  const p = wired([root], { store });
  p.ctx.kitLayout('mcp', root, store);
  assert.equal(p.ctx.kitMoveRow(p.ctx.document, rows[1].row, -1), true);
  assert.deepEqual(rowIds(root), ['b', 'a', 'c']);
  assert.deepEqual(store.json('dt.rowOrder'), { mcp: ['b', 'a', 'c'] });
  p.ctx.kitMoveRow(p.ctx.document, rows[1].row, 1);
  assert.deepEqual(rowIds(root), ['a', 'b', 'c']);
  assert.deepEqual(store.json('dt.rowOrder'), {});
});

test('kitMoveRow skips hidden rows and stops at the ends', () => {
  const store = memStore(), rows = ['a', 'b', 'c'].map((id) => movableRow(id)), root = el('div', {}, rows.map((r) => r.row));
  rows[1].row.hidden = true;
  const p = wired([root], { store });
  p.ctx.kitLayout('p', root, store);
  p.ctx.kitMoveRow(p.ctx.document, rows[2].row, -1);
  assert.deepEqual(rowIds(root), ['c', 'a', 'b']);
  assert.equal(p.ctx.kitMoveRow(p.ctx.document, rows[2].row, -1), false);
  assert.equal(p.ctx.kitMoveRow(p.ctx.document, rows[0].row, 1), false);
});

test('kitRowMenus disables Move up on the first shown row and Move down on the last', () => {
  const rows = ['a', 'b', 'c'].map((id) => movableRow(id)), root = el('div', {}, rows.map((r) => r.row));
  rows[2].row.hidden = true;
  kit().kitRowMenus(root);
  assert.equal(rows[0].up.disabled, true);
  assert.equal(rows[0].dn.disabled, false);
  assert.equal(rows[1].up.disabled, false);
  assert.equal(rows[1].dn.disabled, true);
});

test('Move up from the row menu moves the row, closes the menu and focuses the menu button', () => {
  const store = memStore(), rows = ['a', 'b'].map((id) => movableRow(id)), root = el('div', {}, rows.map((r) => r.row));
  const p = wired([root], { store });
  p.ctx.kitLayout('p', root, store);
  p.click(rows[1].menu);
  p.click(rows[1].up);
  assert.deepEqual(rowIds(root), ['b', 'a']);
  assert.equal(rows[1].up.parentNode.hidden, true);
  assert.equal(rows[1].menu.focused, true);
  assert.equal(rows[1].up.disabled, true);
});

test('touch drag on a row grip moves the row within its page and saves dt.rowOrder', () => {
  const store = memStore(), rows = ['a', 'b', 'c'].map((id) => movableRow(id)), root = el('div', {}, rows.map((r) => r.row));
  const p = wired([root], { store, at: () => rows[2].row });
  p.ctx.kitLayout('p', root, store);
  p.fire('pointerdown', { target: rows[0].row.querySelector('.row-grip'), pointerType: 'touch', pointerId: 3, preventDefault: () => {} });
  p.fire('pointermove', { pointerId: 3, clientX: 1, clientY: 100 });
  p.fire('pointerup', { pointerId: 3, pointerType: 'touch' });
  assert.deepEqual(rowIds(root), ['b', 'c', 'a']);
  assert.deepEqual(store.json('dt.rowOrder'), { p: ['b', 'c', 'a'] });
  assert.ok(!rows[0].row.classList.contains('dragging'));
});

test('kitDress and kitLayout give every row panels box a data-cards key', () => {
  const box = el('div', { class: 'panels' }, [fakeCard('c1'), fakeCard('c2')]);
  const keep = fakeBox('mine', ['c3']);
  const root = el('div', {}, [fakeRow('a', {}, [box]), fakeRow('b', {}, [keep])]);
  kit().kitLayout('p', root, memStore({ 'dt.order': JSON.stringify({ p: { a: ['c2', 'c1'] } }) }));
  assert.equal(box.getAttribute('data-cards'), 'a');
  assert.equal(keep.getAttribute('data-cards'), 'mine');
  assert.deepEqual(cardIds(box), ['c2', 'c1']);
  const lone = el('div', { class: 'panels' }, [fakeCard('c9')]);
  const root2 = el('div', {}, [fakeRow('z', {}, [lone])]);
  kit().kitBoxKeys(root2);
  assert.equal(lone.getAttribute('data-cards'), 'z');
});

// A sortable panel table: header row in thead, body rows of [label, value] with optional extra cells.
function sortPanel(rows, attrs = {}, card = 'tbl') {
  const head = el('tr', {}, [text('th', {}, 'Tool'), text('th', {}, 'Calls'), text('th', { class: 'no-copy' }, '')]);
  const body = el('tbody', {}, rows.map(([a, b, extra = {}]) => el('tr', extra, [text('td', {}, a), text('td', {}, b), el('td')])));
  const table = el('table', attrs, [el('thead', {}, [head]), body]);
  const panel = el('section', { class: 'panel', 'data-card': card }, [el('div', { class: 'p-body' }, [table])]);
  return { panel, table, body, th: head.children };
}
const col = (body, i = 0) => body.children.map((r) => r.children[i].textContent);

test('kitSortKey reads data-v, then numbers, then text', () => {
  const k = kit(), plain = (v) => JSON.parse(JSON.stringify(v));
  assert.deepEqual(plain(k.kitSortKey(text('td', { 'data-v': '5' }, 'five'))), { n: 5 });
  assert.deepEqual(plain(k.kitSortKey(text('td', {}, '1,234 ms'))), { n: 1234 });
  assert.deepEqual(plain(k.kitSortKey(text('td', {}, '12%'))), { n: 12 });
  assert.deepEqual(plain(k.kitSortKey(text('td', {}, 'Alpha'))), { s: 'alpha' });
  assert.deepEqual(plain(k.kitSortKey(text('td', {}, '2026-11-14'))), { s: '2026-11-14' });
  assert.equal(k.kitSortKey(text('td', {}, ' ')), null);
  assert.equal(k.kitSortKey(text('td', {}, '–')), null);
});

test('kitSortTable sorts by a column, marks its header and puts empty cells last', () => {
  const t = sortPanel([['a', '3'], ['b', ''], ['c', '10'], ['d', '1']]);
  const k = kit();
  k.kitSortTable(t.table, 'Calls', 'desc');
  assert.deepEqual(col(t.body), ['c', 'a', 'd', 'b']);
  assert.equal(t.th[1].getAttribute('aria-sort'), 'descending');
  assert.equal(t.th[0].hasAttribute('aria-sort'), false);
  k.kitSortTable(t.table, 'Calls', 'asc');
  assert.deepEqual(col(t.body), ['d', 'a', 'c', 'b']);
  k.kitSortTable(t.table, 'Tool', 'desc');
  assert.equal(t.th[1].hasAttribute('aria-sort'), false);
  assert.equal(t.th[0].getAttribute('aria-sort'), 'descending');
  k.kitSortTable(t.table, 'Nope', 'asc');
  assert.deepEqual(col(t.body), ['d', 'c', 'b', 'a']);
});

test('kitSortTable keeps child rows under their parent', () => {
  const t = sortPanel([['a', '1'], ['a-detail', '', { 'data-kit-child': '' }], ['b', '2'], ['b-detail', '', { 'data-kit-child': '' }]]);
  kit().kitSortTable(t.table, 'Calls', 'desc');
  assert.deepEqual(col(t.body), ['b', 'b-detail', 'a', 'a-detail']);
});

test('a header click sorts, a second click reverses, and the choice is saved per page', () => {
  const store = memStore(), t = sortPanel([['a', '3'], ['b', '10']]), root = el('div', {}, [t.panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('mcp', root, store);
  p.click(t.th[1]);
  assert.deepEqual(col(t.body), ['b', 'a']);
  assert.deepEqual(store.json('dt.sort'), { mcp: { 'tbl#Tool|Calls|': { col: 'Calls', dir: 'desc' } } });
  p.click(t.th[1]);
  assert.deepEqual(col(t.body), ['a', 'b']);
  assert.deepEqual(store.json('dt.sort'), { mcp: { 'tbl#Tool|Calls|': { col: 'Calls', dir: 'asc' } } });
  p.click(t.th[0]);
  assert.deepEqual(store.json('dt.sort'), { mcp: { 'tbl#Tool|Calls|': { col: 'Tool', dir: 'asc' } } });
  p.click(t.th[2]);
  assert.deepEqual(store.json('dt.sort'), { mcp: { 'tbl#Tool|Calls|': { col: 'Tool', dir: 'asc' } } });
});

test('kitRows applies the saved sort, else the data-sort default, after each fill', () => {
  const store = memStore(), t = sortPanel([['a', '3'], ['b', '10']], { 'data-sort': 'Calls desc' }), root = el('div', {}, [t.panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('mcp', root, store);
  p.ctx.kitRows(t.body, '<tr>…</tr>');
  assert.equal(t.body.innerHTML, '<tr>…</tr>');
  assert.deepEqual(col(t.body), ['b', 'a']);
  p.click(t.th[0]);
  t.body.children.reverse();
  p.ctx.kitRows(t.body, '<tr>…</tr>');
  assert.deepEqual(col(t.body), ['a', 'b']);
});

test('kitSortOf ignores junk in dt.sort and falls back to data-sort', () => {
  const junk = ['[1]', '"x"', '{"p":[1]}', '{"p":{"tbl":"Calls"}}', '{"p":{"tbl":{"col":"Nope","dir":"asc"}}}', '{"p":{"tbl":{"col":"Tool","dir":"sideways"}}}', '{"p":{"__proto__":{"col":"Tool"}}}'];
  for (const raw of junk) {
    const store = memStore({ 'dt.sort': raw }), t = sortPanel([['a', '1']], { 'data-sort': 'Calls desc' }), root = el('div', {}, [t.panel]);
    const k = kit();
    k.kitLayout('p', root, store);
    assert.deepEqual(JSON.parse(JSON.stringify(k.kitSortOf(t.table, store))), { col: 'Calls', dir: 'desc' }, raw);
  }
  const t = sortPanel([['a', '1']]), root = el('div', {}, [t.panel]), k = kit();
  k.kitLayout('p', root, throwingStore);
  assert.equal(k.kitSortOf(t.table, throwingStore), null);
});

test('kitLayoutReset forgets the page\'s sorts and puts back the default sort', () => {
  const store = memStore({ 'dt.sort': JSON.stringify({ p: { 'tbl#Tool|Calls|': { col: 'Tool', dir: 'desc' } }, q: { x: { col: 'A', dir: 'asc' } } }) });
  const t = sortPanel([['a', '3'], ['b', '10']], { 'data-sort': 'Calls asc' }), root = el('div', {}, [t.panel]);
  const k = kit();
  k.kitLayout('p', root, store);
  k.kitRows(t.body, '');
  assert.deepEqual(col(t.body), ['b', 'a']);
  k.kitLayoutReset('p', root, store);
  assert.deepEqual(col(t.body), ['a', 'b']);
  assert.deepEqual(store.json('dt.sort'), { q: { x: { col: 'A', dir: 'asc' } } });
});

// A count panel: menu with Show as chart, and a bars table inside a .tbl-wrap.
function barsPanel(rows, card = 'mcpPerTool') {
  const item = text('button', { 'data-kit-do': 'bars' }, 'Show as chart');
  const pop = el('div', { class: 'p-pop' }, [item]);
  pop.hidden = true;
  const head = el('tr', {}, [text('th', {}, 'Tool'), text('th', {}, 'Calls'), text('th', {}, 'Failures')]);
  const body = el('tbody', {}, rows.map(([a, b, c]) => el('tr', {}, [text('td', {}, a), text('td', {}, b), text('td', {}, c)])));
  const table = el('table', { 'data-kit-bars': 'Calls,Failures', 'data-sort': 'Calls desc' }, [el('thead', {}, [head]), body]);
  const wrap = el('div', { class: 'tbl-wrap' }, [table]);
  const panel = el('section', { class: 'panel', 'data-card': card }, [el('header', { class: 'p-hd' }, [el('button', { 'data-kit-menu': '' }), pop]), el('div', { class: 'p-body' }, [wrap])]);
  return { panel, item, wrap, table, body, bars: () => panel.querySelector('.p-bars') };
}

test('kitBarsHtml scales every series to the largest value', () => {
  const html = kit().kitBarsHtml(['a', 'b'], [{ name: 'Calls', values: [5, 10] }, { name: 'Failures', values: [1, 0] }]);
  assert.match(html, /width:50%;background:var\(--s1\)/);
  assert.match(html, /width:100%;background:var\(--s1\)/);
  assert.match(html, /width:10%;background:var\(--s2\)/);
  assert.match(html, />a</);
  assert.doesNotMatch(html, /NaN/);
});

test('kitBarsHtml says No data for no rows or only zeros', () => {
  const k = kit();
  for (const html of [k.kitBarsHtml([], []), k.kitBarsHtml(['a', 'b'], [{ name: 'Calls', values: [0, 0] }])]) {
    assert.match(html, /No data/);
    assert.doesNotMatch(html, /NaN/);
  }
});

test('Show as chart swaps the table for bars in table order, and is remembered per page', () => {
  const store = memStore(), a = barsPanel([['x', '3', '0'], ['y', '9', '1']]), root = el('div', {}, [a.panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('mcp', root, store);
  p.click(a.item);
  assert.equal(a.wrap.hidden, true);
  assert.equal(a.bars().hidden, false);
  assert.ok(a.bars().innerHTML.indexOf('>y<') < a.bars().innerHTML.indexOf('>x<'));
  assert.equal(a.item.textContent, 'Show as table');
  assert.deepEqual(store.json('dt.view'), { mcp: { mcpPerTool: 'chart' } });
  const b = barsPanel([['x', '1', '0']]), root2 = el('div', {}, [b.panel]);
  const q = wired([root2], { store });
  q.ctx.kitLayout('mcp', root2, store);
  assert.equal(b.wrap.hidden, true);
  assert.equal(b.bars().hidden, false);
  assert.equal(b.item.textContent, 'Show as table');
  q.click(b.item);
  assert.equal(b.wrap.hidden, false);
  assert.equal(b.bars().hidden, true);
  assert.deepEqual(store.json('dt.view'), {});
});

test('the chart follows refills and sorts while it is shown', () => {
  const store = memStore(), a = barsPanel([['x', '3', '0'], ['y', '9', '1']]), root = el('div', {}, [a.panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('mcp', root, store);
  p.click(a.item);
  a.body.children[0].children[1].textContent = '1';
  a.body.children.push(el('tr', {}, [text('td', {}, 'z'), text('td', {}, '20'), text('td', {}, '0')]));
  a.body.children[2].parentNode = a.body;
  p.ctx.kitRows(a.body, '');
  const html = a.bars().innerHTML;
  assert.ok(html.indexOf('>z<') < html.indexOf('>y<'));
  assert.match(html, /width:100%/);
});

test('kitLayoutReset puts the table back and forgets the view', () => {
  const store = memStore({ 'dt.view': JSON.stringify({ mcp: { mcpPerTool: 'chart' }, q: { k: 'chart' } }) });
  const a = barsPanel([['x', '3', '0']]), root = el('div', {}, [a.panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('mcp', root, store);
  assert.equal(a.wrap.hidden, true);
  p.ctx.kitLayoutReset('mcp', root, store);
  assert.equal(a.wrap.hidden, false);
  assert.equal(a.bars().hidden, true);
  assert.equal(a.item.textContent, 'Show as chart');
  assert.deepEqual(store.json('dt.view'), { q: { k: 'chart' } });
});

test('a bars label comes from data-label when the cell has one', () => {
  const a = barsPanel([['x', '3', '0']]);
  a.body.children[0].children[0].setAttribute('data-label', 'Claude Code');
  const d = JSON.parse(JSON.stringify(kit().kitBarsData(a.table)));
  assert.deepEqual(d.labels, ['Claude Code']);
  assert.deepEqual(d.series, [{ name: 'Calls', values: [3] }, { name: 'Failures', values: [0] }]);
});

test('kitSortKey reads signed numbers, durations and an em dash', () => {
  const k = kit(), plain = (v) => JSON.parse(JSON.stringify(v));
  assert.deepEqual(plain(k.kitSortKey(text('td', {}, '+12%'))), { n: 12 });
  assert.deepEqual(plain(k.kitSortKey(text('td', {}, '+3.2 ↑'))), { n: 3.2 });
  assert.deepEqual(plain(k.kitSortKey(text('td', {}, '45s'))), { n: 45 });
  assert.deepEqual(plain(k.kitSortKey(text('td', {}, '12m 03s'))), { n: 723 });
  assert.deepEqual(plain(k.kitSortKey(text('td', {}, '1h 05m'))), { n: 3900 });
  assert.equal(k.kitSortKey(text('td', {}, '—')), null);
});

test('only tables in the page when kitLayout ran sort by header click', () => {
  const store = memStore(), t = sortPanel([['a', '3'], ['b', '10']]), root = el('div', {}, [t.panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('p', root, store);
  assert.equal(t.table.hasAttribute('data-kit-sortable'), true);
  const late = sortPanel([['a', '3'], ['b', '10']], {}, 'late');
  root.appendChild(late.panel);
  p.click(late.th[1]);
  assert.deepEqual(col(late.body), ['a', 'b']);
  assert.equal(store.data['dt.sort'], undefined);
  const off = sortPanel([['a', '3'], ['b', '10']], { 'data-kit-nosort': '' }, 'off'), root2 = el('div', {}, [off.panel]);
  p.ctx.kitLayout('q', root2, store);
  assert.equal(off.table.hasAttribute('data-kit-sortable'), false);
});

test('a grouped table sorts within each group, keeping the group headings in place', () => {
  const store = memStore(), t = sortPanel([['b', '1'], ['a', '2']]), root = el('div', {}, [t.panel]);
  t.body.insertBefore(el('tr', {}, [text('td', { colspan: '3' }, 'G1')]), t.body.children[0]);
  t.body.appendChild(el('tr', {}, [text('td', { colspan: '3' }, 'G2')]));
  t.body.appendChild(el('tr', {}, [text('td', {}, 'd'), text('td', {}, '5'), el('td')]));
  t.body.appendChild(el('tr', {}, [text('td', {}, 'c'), text('td', {}, '9'), el('td')]));
  const p = wired([root], { store });
  p.ctx.kitLayout('p', root, store);
  p.click(t.th[0]);
  assert.deepEqual(col(t.body), ['G1', 'a', 'b', 'G2', 'c', 'd']);
  const one = sortPanel([]), root2 = el('div', {}, [one.panel]);
  one.body.appendChild(el('tr', {}, [text('td', { colspan: '3' }, 'None')]));
  p.ctx.kitLayout('q', root2, store);
  p.click(one.th[1]);
  assert.equal(one.th[1].getAttribute('aria-sort'), 'ascending');
});

test('a table with rows straight under it, with no tbody, sorts', () => {
  const head = el('thead', {}, [el('tr', {}, [text('th', {}, 'Name'), text('th', {}, 'N')])]);
  const table = el('table', {}, [head, el('tr', {}, [text('td', {}, 'a'), text('td', {}, '1')]), el('tr', {}, [text('td', {}, 'b'), text('td', {}, '7')])]);
  kit().kitSortTable(table, 'N', 'desc');
  assert.deepEqual(table.children.slice(1).map((r) => r.children[0].textContent), ['b', 'a']);
  assert.equal(table.children[0], head);
});

test('kitTablesIn makes tables built at run time sortable and applies their saved sort', () => {
  const store = memStore({ 'dt.sort': JSON.stringify({ p: { 'late#Tool|Calls|': { col: 'Calls', dir: 'desc' } } }) });
  const holder = el('div', { class: 'p-body' }), panel = el('section', { class: 'panel', 'data-card': 'late' }, [holder]), root = el('div', {}, [panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('p', root, store);
  const t = sortPanel([['a', '3'], ['b', '10']]);
  holder.appendChild(t.table);
  p.ctx.kitTablesIn(holder);
  assert.equal(t.table.hasAttribute('data-kit-sortable'), true);
  assert.deepEqual(col(t.body), ['b', 'a']);
  p.click(t.th[0]);
  assert.deepEqual(store.json('dt.sort'), { p: { 'late#Tool|Calls|': { col: 'Tool', dir: 'asc' } } });
  const again = sortPanel([['b', '1'], ['a', '2']]);
  holder.removeChild(t.table);
  holder.appendChild(again.table);
  p.ctx.kitTablesIn(holder);
  assert.deepEqual(col(again.body), ['a', 'b']);
  assert.equal(again.th[0].getAttribute('aria-sort'), 'ascending');
});

test('kitTablesIn adds Show as chart for a bars table built at run time, keeps the saved chart view, and drops the item when the table goes', () => {
  const store = memStore({ 'dt.view': JSON.stringify({ p: { runSide: 'chart' } }) });
  const pop = el('div', { class: 'p-pop' }, [text('button', { 'data-kit-do': 'copy' }, 'Copy data')]);
  const body = el('div', { class: 'p-body' });
  const panel = el('section', { class: 'panel', 'data-card': 'runSide' }, [el('header', { class: 'p-hd' }, [pop]), body]), root = el('div', {}, [panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('p', root, store);
  const a = barsPanel([['x', '3', '0']]);
  body.appendChild(a.wrap);
  p.ctx.kitTablesIn(body);
  assert.match(pop.inserted.join(''), /data-kit-do="bars">Show as chart/);
  const item = text('button', { 'data-kit-do': 'bars' }, 'Show as chart');
  pop.children.unshift(item); item.parentNode = pop;
  p.ctx.kitTablesIn(body);
  assert.equal(pop.inserted.length, 1);
  assert.equal(a.wrap.hidden, true);
  assert.equal(panel.querySelector('.p-bars').hidden, false);
  assert.equal(item.textContent, 'Show as table');
  body.removeChild(a.wrap);
  p.ctx.kitTablesIn(body);
  assert.equal(pop.children.includes(item), false);
  assert.equal(panel.querySelector('.p-bars').hidden, true);
});

test('a bars table can name its label column', () => {
  const a = barsPanel([['x', '3', '0']]);
  a.table.setAttribute('data-kit-bars', 'Calls');
  a.table.setAttribute('data-kit-bars-label', 'Failures');
  assert.deepEqual(JSON.parse(JSON.stringify(kit().kitBarsData(a.table))).labels, ['0']);
});

test('a panel without data-card gets no grip, and a box with no panels yet gets no key', () => {
  const tile = el('section', { class: 'panel', 'data-title': 'Tile' }), card = el('section', { class: 'panel', 'data-card': 'c1', 'data-title': 'Card' });
  const box = el('div', { class: 'panels' }, [tile, card]), empty = el('div', { class: 'panels' });
  const root = el('div', {}, [fakeRow('a', {}, [box]), fakeRow('b', {}, [empty])]);
  kit().kitDress(root);
  assert.equal(box.getAttribute('data-cards'), 'a');
  assert.equal(empty.hasAttribute('data-cards'), false);
  assert.doesNotMatch(tile.inserted[0], /class="grip"/);
  assert.match(card.inserted[0], /class="grip"/);
});

test('kitLayoutReset puts a table with no default sort back in the order it was filled', () => {
  const store = memStore(), t = sortPanel([['b', '1'], ['c', '3'], ['a', '2']]), root = el('div', {}, [t.panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('p', root, store);
  p.ctx.kitResort(t.body);
  p.click(t.th[0]);
  assert.deepEqual(col(t.body), ['a', 'b', 'c']);
  p.ctx.kitLayoutReset('p', root, store);
  assert.deepEqual(col(t.body), ['b', 'c', 'a']);
  assert.equal(t.th[0].hasAttribute('aria-sort'), false);
});

test('two tables in one panel keep their own saved sorts', () => {
  const store = memStore(), one = sortPanel([['a', '3'], ['b', '10']]), two = sortPanel([['x', '1'], ['y', '5']]);
  const panel = el('section', { class: 'panel', 'data-card': 'side' }, [one.table, two.table]), root = el('div', {}, [panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('p', root, store);
  p.click(one.th[1]);
  p.click(two.th[0]);
  assert.deepEqual(store.json('dt.sort'), { p: { 'side#Tool|Calls|': { col: 'Calls', dir: 'desc' }, 'side#Tool|Calls|#2': { col: 'Tool', dir: 'asc' } } });
  assert.deepEqual(JSON.parse(JSON.stringify(p.ctx.kitSortOf(one.table, store))), { col: 'Calls', dir: 'desc' });
  assert.deepEqual(JSON.parse(JSON.stringify(p.ctx.kitSortOf(two.table, store))), { col: 'Tool', dir: 'asc' });
});

test('kitSortTable moves no rows when they are already in order', () => {
  const t = sortPanel([['c', '10'], ['b', '3'], ['a', '1']]);
  let moves = 0;
  const add = t.body.appendChild.bind(t.body);
  t.body.appendChild = (r) => { moves++; return add(r); };
  kit().kitSortTable(t.table, 'Calls', 'desc');
  assert.equal(moves, 0);
  assert.equal(t.th[1].getAttribute('aria-sort'), 'descending');
  kit().kitSortTable(t.table, 'Calls', 'asc');
  assert.ok(moves > 0);
  assert.deepEqual(col(t.body), ['a', 'b', 'c']);
});

test('mouse drag on a row grip uses native dragging and saves the order', () => {
  const store = memStore(), rows = ['a', 'b'].map((id) => movableRow(id)), root = el('div', {}, rows.map((r) => r.row));
  const p = wired([root], { store, at: () => rows[1].row });
  p.ctx.kitLayout('p', root, store);
  p.fire('pointerdown', { target: rows[0].row.querySelector('.row-grip'), pointerType: 'mouse', pointerId: 1 });
  assert.equal(rows[0].row.getAttribute('draggable'), 'true');
  const sent = [];
  p.fire('dragstart', { target: rows[0].row, dataTransfer: { setData: (k, v) => sent.push(v) } });
  assert.deepEqual(sent, ['a']);
  let stopped = 0;
  p.fire('dragover', { clientX: 1, clientY: 100, preventDefault: () => { stopped++; } });
  assert.equal(stopped, 1);
  p.fire('dragend', {});
  assert.deepEqual(rowIds(root), ['b', 'a']);
  assert.equal(rows[0].row.hasAttribute('draggable'), false);
  assert.deepEqual(store.json('dt.rowOrder'), { p: ['b', 'a'] });
});

test('tables added to the page later are made sortable through a mutation observer', () => {
  const watched = [];
  class MO { constructor(fn) { this.fn = fn; } observe(node, opts) { watched.push({ mo: this, node, opts }); } }
  const store = memStore(), holder = el('div', { class: 'p-body' }), panel = el('section', { class: 'panel', 'data-card': 'late' }, [holder]), root = el('div', {}, [panel]);
  const p = wired([root], { store, MutationObserver: MO });
  p.ctx.kitLayout('p', root, store);
  assert.equal(watched.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(watched[0].opts)), { childList: true, subtree: true });
  const t = sortPanel([['a', '3'], ['b', '10']], { 'data-sort': 'Calls desc' });
  Object.assign(t.table, { nodeType: 1, tagName: 'TABLE' });
  holder.appendChild(t.table);
  const row = Object.assign(el('tr'), { nodeType: 1, tagName: 'TR' });
  watched[0].mo.fn([{ target: t.body, addedNodes: [row], removedNodes: [] }]);
  assert.equal(t.table.hasAttribute('data-kit-sortable'), false);
  watched[0].mo.fn([{ target: holder, addedNodes: [t.table], removedNodes: [] }, { target: holder, addedNodes: [{ nodeType: 3 }], removedNodes: [] }]);
  assert.equal(t.table.hasAttribute('data-kit-sortable'), true);
  assert.deepEqual(col(t.body), ['b', 'a']);
});

test('a saved sort follows its table when other tables come and go in the panel', () => {
  const store = memStore(), t = sortPanel([['a', '3'], ['b', '10']]);
  const panel = el('section', { class: 'panel', 'data-card': 'runSide' }, [t.table]), root = el('div', {}, [panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('runs', root, store);
  p.click(t.th[1]);
  const steps = el('table', {}, [el('thead', {}, [el('tr', {}, [text('th', {}, 'step')])]), el('tbody')]);
  panel.children.unshift(steps); steps.parentNode = panel;
  assert.deepEqual(JSON.parse(JSON.stringify(p.ctx.kitSortOf(t.table, store))), { col: 'Calls', dir: 'desc' });
  assert.equal(p.ctx.kitSortOf(steps, store), null);
});

test('a header row kept in the body is not sorted as data', () => {
  const table = el('table', {}, [el('tbody', {}, [
    el('tr', {}, [text('th', {}, 'Name'), text('th', {}, 'N')]),
    el('tr', {}, [text('td', {}, 'a'), text('td', {}, '1')]),
    el('tr', {}, [text('td', {}, 'b'), text('td', {}, '7')]),
  ])]);
  kit().kitSortTable(table, 'N', 'desc');
  assert.deepEqual(table.children[0].children.map((r) => r.children[0].textContent), ['Name', 'b', 'a']);
  kit().kitSortTable(table, 'N', 'asc');
  assert.deepEqual(table.children[0].children.map((r) => r.children[0].textContent), ['Name', 'a', 'b']);
});

test('the chart wrap shows again when its bars table goes away', () => {
  const store = memStore(), body = el('div', { class: 'p-body' });
  const panel = el('section', { class: 'panel', 'data-card': 'sto' }, [el('header', { class: 'p-hd' }, [el('div', { class: 'p-pop' })]), body]), root = el('div', {}, [panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('storage', root, store);
  const a = barsPanel([['x', '3', '0']]);
  body.appendChild(a.wrap);
  p.ctx.kitShowBars(panel, true);
  assert.equal(a.wrap.hidden, true);
  a.wrap.removeChild(a.table);
  p.ctx.kitTablesIn(body);
  assert.equal(a.wrap.hidden, false);
});

test('the chart is not redrawn when its numbers did not change', () => {
  const store = memStore(), a = barsPanel([['x', '3', '0']]), root = el('div', {}, [a.panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('mcp', root, store);
  p.click(a.item);
  const holder = a.bars();
  let writes = 0, html = holder.innerHTML;
  Object.defineProperty(holder, 'innerHTML', { get: () => html, set: (v) => { writes++; html = v; } });
  p.ctx.kitShowBars(a.panel, true);
  assert.equal(writes, 0);
  a.body.children[0].children[1].textContent = '4';
  p.ctx.kitShowBars(a.panel, true);
  assert.equal(writes, 1);
});

test('a table in a panel with only a title sorts and saves under the title', () => {
  const store = memStore(), t = sortPanel([['a', '3'], ['b', '10']]);
  const panel = el('section', { class: 'panel', 'data-title': 'Latest runs' }, [el('div', { class: 'p-body' }, [t.table])]), root = el('div', {}, [panel]);
  const p = wired([root], { store });
  p.ctx.kitLayout('fleet', root, store);
  p.click(t.th[1]);
  assert.deepEqual(col(t.body), ['b', 'a']);
  assert.deepEqual(store.json('dt.sort'), { fleet: { 'title:Latest runs#Tool|Calls|': { col: 'Calls', dir: 'desc' } } });
});

// Cloud-wins behaviour (Task 5): close button, double-click, legend and refit hooks.
const dbl = (p, el) => el.dispatchEvent(new p.w.MouseEvent('dblclick', { bubbles: true }));
const kpiPanel = (card) => `<section class="panel kpi" data-card="${card}" data-title="${card}"><div class="p-body" id="${card}Body"><b id="${card}Val">1</b></div></section>`;

test('a dressed panel has the close button, and it and Escape leave full size', () => {
  const p = page(row('a', panel('c1', '<p id="txt">x</p>')));
  const sec = p.d.querySelector('[data-card="c1"]'), shut = sec.querySelector('.p-hd button.p-close[data-kit-close]');
  const item = sec.querySelector('button[data-kit-do="full"]'), menu = sec.querySelector('.p-hd [data-kit-menu]');
  assert.ok(shut);
  assert.equal(shut.textContent, '✕');
  assert.equal(shut.getAttribute('aria-label'), 'Close full size: c1');
  p.click(menu);
  p.click(item);
  assert.ok(sec.classList.contains('full'));
  assert.equal(item.textContent, 'Close full size');
  p.click(menu);
  p.click(shut);
  assert.ok(!sec.classList.contains('full'));
  assert.equal(item.textContent, 'Open full size');
  assert.equal(sec.querySelector('.p-pop').hidden, true);
  assert.equal(p.d.activeElement, menu);
  p.click(item);
  assert.ok(sec.classList.contains('full'));
  p.d.dispatchEvent(new p.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.ok(!sec.classList.contains('full'));
  assert.equal(item.textContent, 'Open full size');
});

test('kitSetFull toggles the full class and the menu item text', () => {
  const p = page(row('a', panel('c1', '')));
  const sec = p.d.querySelector('[data-card="c1"]'), item = sec.querySelector('button[data-kit-do="full"]');
  p.w.kitSetFull(sec, true);
  assert.ok(sec.classList.contains('full'));
  assert.equal(item.textContent, 'Close full size');
  p.w.kitSetFull(sec, true);
  assert.ok(sec.classList.contains('full'));
  p.w.kitSetFull(sec, false);
  assert.ok(!sec.classList.contains('full'));
  assert.equal(item.textContent, 'Open full size');
});

test('a double-click on a panel body opens it full size, but not on a control or a grip', () => {
  const inner = '<p id="txt">x</p><button type="button" id="btn">b</button><a href="#" id="lnk">l</a><input id="inp">';
  const p = page(row('a', panel('c1', inner)) + row('b', panel('c2', '')));
  const sec = p.d.querySelector('[data-card="c1"]');
  assert.ok(p.d.querySelector('[data-card="c2"] .grip'));
  for (const id of ['btn', 'lnk', 'inp']) { dbl(p, p.$(id)); assert.ok(!sec.classList.contains('full'), id); }
  dbl(p, p.d.querySelector('[data-card="c2"] .grip'));
  assert.ok(!p.d.querySelector('[data-card="c2"]').classList.contains('full'));
  dbl(p, p.$('txt'));
  assert.ok(sec.classList.contains('full'));
  assert.equal(sec.querySelector('button[data-kit-do="full"]').textContent, 'Close full size');
});

test('a double-click opens no second panel while one is full, and no KPI panel', () => {
  const p = page(row('a', panel('c1', '<p id="t1">x</p>') + panel('c2', '<p id="t2">y</p>') + kpiPanel('k1')));
  const one = p.d.querySelector('[data-card="c1"]'), two = p.d.querySelector('[data-card="c2"]');
  dbl(p, p.$('k1Val'));
  assert.ok(!p.d.querySelector('[data-card="k1"]').classList.contains('full'));
  dbl(p, p.$('t1'));
  assert.ok(one.classList.contains('full'));
  dbl(p, p.$('t2'));
  assert.ok(!two.classList.contains('full'));
});

test('kitConfigure dblSkip adds selectors a double-click skips', () => {
  const p = page(row('a', panel('c1', '<p id="txt">x</p><div class="nofull"><span id="deep">d</span></div>')));
  const sec = p.d.querySelector('[data-card="c1"]');
  p.w.kitConfigure({ dblSkip: '.nofull' });
  dbl(p, p.$('deep'));
  assert.ok(!sec.classList.contains('full'));
  dbl(p, p.$('txt'));
  assert.ok(sec.classList.contains('full'));
  p.w.kitSetFull(sec, false);
  p.w.kitConfigure({ dblSkip: '' });
  dbl(p, p.$('deep'));
  assert.ok(sec.classList.contains('full'));
});

test('kitWire attaches one dblclick listener', () => {
  const p = wired([]);
  assert.equal(p.on.dblclick.length, 1);
});

test('the close button in a fake panel leaves full size and focuses the menu button', () => {
  const a = fakePanel();
  const shut = el('button', { class: 'p-close', 'data-kit-close': '' });
  a.panel.children[0].appendChild(shut);
  const p = wired([a.panel]);
  p.click(a.item('full'));
  assert.ok(a.panel.classList.contains('full'));
  p.click(shut);
  assert.ok(!a.panel.classList.contains('full'));
  assert.equal(a.item('full').textContent, 'Open full size');
  assert.equal(a.menu.focused, true);
});

test('with onLegend set, a legend chip calls it with the panel and chart id instead of redrawing', () => {
  const { ts } = fakeTs();
  const chip = el('button', { 'data-kit-series': 'a|S' });
  const body = el('div', { class: 'p-body' }, [el('div', { class: 'legend' }, [chip]), ts]);
  const sec = el('section', { class: 'panel' }, [body]);
  const p = wired([sec]), calls = [];
  p.ctx.kitConfigure({ onLegend: (panel, id) => calls.push([panel, id]) });
  p.click(chip);
  assert.equal(p.ctx.KIT_SERIES_OFF['a|S'], true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], sec);
  assert.equal(calls[0][1], 'a');
  assert.equal(body.children.length, 2);
  assert.equal(ts.inserted.length, 0);
  p.ctx.kitConfigure({ onLegend: null });
  p.click(chip);
  assert.equal(calls.length, 1);
  assert.equal(ts.inserted.length, 1);
  assert.equal(p.ctx.KIT_SERIES_OFF['a|S'], undefined);
});

test('onLegend gets the chart id from the chip key when the legend has no chart', () => {
  const chip = el('button', { 'data-kit-series': 'empty|S' });
  const sec = el('section', { class: 'panel' }, [el('div', { class: 'p-body' }, [el('div', { class: 'legend' }, [chip]), el('div', { class: 'empty' })])]);
  const p = wired([sec]), calls = [];
  p.ctx.kitConfigure({ onLegend: (panel, id) => calls.push([panel, id]) });
  p.click(chip);
  assert.deepEqual(calls.map((c) => c[1]), ['empty']);
  assert.equal(calls[0][0], sec);
});

test('onRefit is called with the root after kitRefit, full size and resize', () => {
  const timers = [];
  const a = fakePanel();
  const p = wired([a.panel], { setTimeout: (fn) => { timers.push(fn); return timers.length; } }), seen = [];
  p.ctx.kitConfigure({ onRefit: (root) => seen.push(root) });
  p.ctx.kitRefit(a.panel);
  assert.deepEqual(seen, [a.panel]);
  p.click(a.item('full'));
  assert.equal(seen.length, 2);
  assert.equal(seen[1], a.panel);
  p.winOn.resize[0]();
  timers[0]();
  assert.equal(seen.length, 3);
  assert.equal(seen[2].documentElement !== undefined, true);
  p.ctx.kitRefit(null);
  assert.equal(seen.length, 3);
  p.ctx.kitConfigure({ onRefit: null });
  p.ctx.kitRefit(a.panel);
  assert.equal(seen.length, 3);
});

// Fix round 1: holderRows hook and the kit's own sortable headers in the double-click skip list.
test('with holderRows set, Copy data copies the rows it returns verbatim, given the table holder', async () => {
  const a = fakePanel(false), holder = el('div', { class: 'tw' }, [el('table', {}, [el('tr', {}, [text('td', {}, 'shown')])])]);
  a.body.appendChild(holder);
  const p = wired([a.panel]), boxes = [];
  p.ctx.kitConfigure({ holderRows: (box) => { boxes.push(box); return [['Name', 'Calls'], ['b', '10'], ['a', '3'], ['pin', '1']]; } });
  p.click(a.item('copy'));
  await Promise.resolve();
  assert.deepEqual(boxes, [holder]);
  assert.equal(p.copied[0], 'Name,Calls\nb,10\na,3\npin,1');
  assert.equal(p.toast.textContent, 'Copied as CSV');
});

test('a holderRows hook that returns null or undefined leaves the rendered table to copy', async () => {
  for (const ret of [null, undefined]) {
    const a = fakePanel(true);
    const p = wired([a.panel]);
    let calls = 0;
    p.ctx.kitConfigure({ holderRows: () => { calls++; return ret; } });
    p.click(a.item('copy'));
    await Promise.resolve();
    assert.equal(calls, 1, String(ret));
    assert.equal(p.copied[0], 'name,n\nrow,1234', String(ret));
  }
});

test('data-kit-nocopy wins over the holderRows hook', async () => {
  const a = fakePanel(false);
  a.body.appendChild(el('div', { 'data-kit-nocopy': '' }, [el('table', {}, [el('tr', {}, [text('td', {}, 'one')])])]));
  const p = wired([a.panel]);
  let calls = 0;
  p.ctx.kitConfigure({ holderRows: () => { calls++; return [['x']]; } });
  p.click(a.item('copy'));
  await Promise.resolve();
  assert.equal(calls, 0);
  assert.equal(p.copied.length, 0);
  assert.equal(p.toast.textContent, 'This panel has no data to copy');
});

test('a double-click on a kit sortable header neither opens full size nor sorts', () => {
  const p = page(row('a', panel('c1', TABLE([['b', '1'], ['a', '2']]))));
  const sec = p.d.querySelector('[data-card="c1"]'), t = sec.querySelector('table'), th = t.tHead.rows[0].cells[0];
  assert.ok(t.hasAttribute('data-kit-sortable'));
  dbl(p, th);
  assert.ok(!sec.classList.contains('full'));
  assert.deepEqual(firstCol(t), ['b', 'a']);
  assert.equal(th.getAttribute('aria-sort'), null);
  assert.doesNotMatch(p.w.KIT_DBL_SKIP, /data-col/);
  p.w.kitConfigure({ dblSkip: 'th[data-col]' });
  th.setAttribute('data-col', '0');
  t.removeAttribute('data-kit-sortable');
  dbl(p, th);
  assert.ok(!sec.classList.contains('full'));
});

// Fix round 2: chart ids with a bar, throwing app hooks, and a rank row in a window-less document.
test('onLegend gets a chart id holding a bar intact on the chip-key fallback', () => {
  const chip = el('button', { 'data-kit-series': 'a|b|S' });
  const sec = el('section', { class: 'panel' }, [el('div', { class: 'p-body' }, [el('div', { class: 'legend' }, [chip]), el('div', { class: 'empty' })])]);
  const p = wired([sec]), ids = [];
  p.ctx.kitConfigure({ onLegend: (panel, id) => ids.push(id) });
  p.click(chip);
  assert.deepEqual(ids, ['a|b']);
});

test('a throwing onLegend falls back to redrawing that chart', () => {
  const { ts } = fakeTs();
  const chip = el('button', { 'data-kit-series': 'a|S' });
  const body = el('div', { class: 'p-body' }, [el('div', { class: 'legend' }, [chip]), ts]);
  const p = wired([el('section', { class: 'panel' }, [body])]);
  p.ctx.kitConfigure({ onLegend: () => { throw new Error('app'); } });
  assert.doesNotThrow(() => p.click(chip));
  assert.equal(ts.inserted.length, 1);
  assert.match(ts.inserted[0], /data-kit-series="a\|S" aria-pressed="false"/);
});

test('a throwing onRefit does not stop kitRefit or full size', () => {
  const a = fakePanel();
  const { ts } = fakeTs();
  a.body.children.push(ts); ts.parentNode = a.body;
  a.body.clientWidth = 900;
  const p = wired([a.panel]);
  p.ctx.kitConfigure({ onRefit: () => { throw new Error('app'); } });
  assert.doesNotThrow(() => p.click(a.item('full')));
  assert.ok(a.panel.classList.contains('full'));
  assert.equal(a.item('full').textContent, 'Close full size');
  assert.equal(ts.inserted.length, 0);
  assert.doesNotThrow(() => p.ctx.kitRefit(a.panel));
  assert.match(ts.inserted[0], /viewBox="0 0 900 220"/);
});

test('a throwing holderRows falls back to copying the rendered table', async () => {
  const a = fakePanel(true);
  const p = wired([a.panel]);
  p.ctx.kitConfigure({ holderRows: () => { throw new Error('app'); } });
  assert.doesNotThrow(() => p.click(a.item('copy')));
  await Promise.resolve();
  assert.equal(p.copied[0], 'name,n\nrow,1234');
  assert.equal(p.toast.textContent, 'Copied as CSV');
});

// Task 6 (R19): a throwing app hook is logged with its name through console.error.
test('a throwing app hook goes to console.error by name, and the kit fallback still runs', async () => {
  const a = fakePanel(true), chip = el('button', { 'data-kit-series': 'a|S' });
  a.body.appendChild(el('div', { class: 'legend' }, [chip]));
  const p = wired([a.panel]), logged = [], boom = new Error('app');
  p.ctx.console = { error: (...args) => logged.push(args) };
  p.ctx.kitConfigure({ onLegend: () => { throw boom; }, onRefit: () => { throw boom; }, holderRows: () => { throw boom; } });
  p.click(chip);
  p.ctx.kitRefit(a.panel);
  p.click(a.item('copy'));
  await Promise.resolve();
  assert.deepEqual(logged, [['kit hook onLegend failed', boom], ['kit hook onRefit failed', boom], ['kit hook holderRows failed', boom]]);
  assert.equal(p.copied[0], 'name,n\nrow,1234');
});

test('an onLegend hook that throws after a partial redraw still leaves the chart drawn and logs the error', () => {
  const { ts } = fakeTs();
  const chip = el('button', { 'data-kit-series': 'a|S' });
  const body = el('div', { class: 'p-body' }, [el('div', { class: 'legend' }, [chip]), ts]);
  const p = wired([el('section', { class: 'panel' }, [body])]), logged = [], boom = new Error('half done');
  p.ctx.console = { error: (...args) => logged.push(args) };
  p.ctx.kitConfigure({ onLegend: () => { ts.insertAdjacentHTML('beforebegin', '<p>partial</p>'); throw boom; } });
  p.click(chip);
  assert.deepEqual(logged, [['kit hook onLegend failed', boom]]);
  assert.equal(ts.inserted[0], '<p>partial</p>');
  assert.ok(ts.inserted.length >= 2, 'the kit redrew after the hook threw');
  assert.match(ts.inserted[ts.inserted.length - 1], /data-kit-series="a\|S" aria-pressed="false"/);
  assert.equal(p.ctx.KIT_SERIES_OFF['a|S'], true);
});

test('a rank Only show in a document with no window does nothing', () => {
  const p = page('');
  const doc = p.d.implementation.createHTMLDocument('');
  assert.equal(doc.defaultView, null);
  doc.body.innerHTML = p.w.rankHtml([{ label: 'qwen', v: 3 }], { key: 'model' });
  const seen = [];
  doc.addEventListener('kit:act', () => seen.push(1));
  assert.doesNotThrow(() => p.w.kitRankAct(doc.querySelector('button[data-kit-act="only"]')));
  assert.deepEqual(seen, []);
});

// Final fix wave: ancestor nosort, invalid dblSkip and a detaching onLegend hook.
test('data-kit-nosort on an ancestor stops kit sorting of static, kitRows and kitSetTable tables; on the table it still works', async () => {
  const SORTED = 'data-sort="Calls desc"';
  const p = page(`<div id="tab" data-kit-nosort>${row('a', panel('off', TABLE([['a', '3'], ['b', '10']], SORTED)))}</div>` +
    row('b', panel('on', '') + panel('own', '')));
  const T = { cols: ['Tool', 'Calls'], rows: [['a', 3], ['b', 10]] };
  const stat = p.$('offBody').querySelector('table');
  assert.ok(!stat.hasAttribute('data-kit-sortable'));
  assert.deepEqual(firstCol(stat), ['a', 'b']);
  p.w.kitRows(stat.tBodies[0], '<tr><td>c</td><td>1</td></tr><tr><td>d</td><td>9</td></tr>');
  assert.deepEqual(firstCol(stat), ['c', 'd']);
  for (const id of ['off', 'on', 'own']) p.w.kitSetTable(id + 'Body', T);
  p.d.querySelector('[data-card="own"] .p-table table').setAttribute('data-kit-nosort', '');
  await tick();
  const tableOf = (id) => p.d.querySelector('[data-card="' + id + '"] .p-table table');
  for (const t of [stat, tableOf('off'), tableOf('own')]) {
    const th = t.tHead.rows[0].cells[0], before = firstCol(t);
    assert.ok(!t.hasAttribute('data-kit-sortable'));
    p.click(th);
    assert.deepEqual(firstCol(t), before);
    assert.equal(th.getAttribute('aria-sort'), null);
  }
  const on = tableOf('on');
  assert.ok(on.hasAttribute('data-kit-sortable'));
  p.click(on.tHead.rows[0].cells[1]);
  assert.equal(on.tHead.rows[0].cells[1].getAttribute('aria-sort'), 'descending');
  assert.deepEqual(firstCol(on), ['b', 'a']);
});

test('an invalid dblSkip is logged once and ignored, and a double-click still opens full size', () => {
  const p = page(row('a', panel('c1', '<p id="txt">x</p><div class="nofull"><span id="deep">d</span></div>')));
  const sec = p.d.querySelector('[data-card="c1"]'), logged = [];
  p.w.console.error = (...args) => logged.push(args);
  p.w.kitConfigure({ dblSkip: '.nofull' });
  p.w.kitConfigure({ dblSkip: 'td[[' });
  assert.deepEqual(logged, [['kit dblSkip invalid', 'td[[']]);
  assert.equal(p.w.KIT_CONF.dblSkip, '.nofull');
  dbl(p, p.$('deep'));
  assert.ok(!sec.classList.contains('full'));
  dbl(p, p.$('txt'));
  assert.ok(sec.classList.contains('full'));
  p.w.kitSetFull(sec, false);
  p.w.kitConfigure({ dblSkip: '' });
  p.w.kitConfigure({ dblSkip: ':::' });
  dbl(p, p.$('txt'));
  assert.ok(sec.classList.contains('full'));
  assert.equal(logged.length, 2);
  assert.equal(p.w.KIT_CONF.dblSkip, '');
});

test('an onLegend hook that detaches the chart and then throws raises no second error', () => {
  const { ts } = fakeTs();
  const chip = el('button', { 'data-kit-series': 'a|S' });
  const body = el('div', { class: 'p-body' }, [el('div', { class: 'legend' }, [chip]), ts]);
  const p = wired([el('section', { class: 'panel' }, [body])]), logged = [], boom = new Error('gone');
  p.ctx.console = { error: (...args) => logged.push(args) };
  p.ctx.kitConfigure({ onLegend: () => { body.removeChild(ts); throw boom; } });
  assert.doesNotThrow(() => p.click(chip));
  assert.deepEqual(logged, [['kit hook onLegend failed', boom]]);
  assert.equal(ts.parentNode, null);
  assert.equal(ts.inserted.length, 0);
});

// A wired jsdom page with one 4-label chart (zoomable unless zoom is false), its svg stubbed to {left: 0, width: 600}.
function zoomPage(extra = {}) {
  const p = page(row('a', panel('zc', '')));
  p.$('zcBody').innerHTML = p.w.tsHtml({ id: 'zc', labels: ['a', 'b', 'c', 'd'], series: [{ name: 'A', values: [1, 2, 3, 4] }], zoom: true, ...extra });
  const ts = () => p.$('zcBody').querySelector('.ts[data-kit-ts]');
  const svg = () => { const s = ts().querySelector('svg'); s.getBoundingClientRect = () => ({ left: 0, width: 600 }); return s; };
  const json = () => JSON.parse(ts().getAttribute('data-kit-ts'));
  // Fires a pointer event at the chart svg (buttons 1 until pointerup); a missing PointerEvent falls back to a MouseEvent with pointerType set.
  const ptr = (type, x, kind = 'mouse', button = 0, buttons = type === 'pointerup' ? 0 : 1) => {
    const init = { bubbles: true, clientX: x, button, buttons, pointerId: 1, pointerType: kind };
    const ev = p.w.PointerEvent ? new p.w.PointerEvent(type, init) : new p.w.MouseEvent(type, init);
    if (!p.w.PointerEvent) { Object.defineProperty(ev, 'pointerType', { value: kind }); Object.defineProperty(ev, 'pointerId', { value: 1 }); }
    svg().dispatchEvent(ev);
  };
  const drag = (a, b, kind) => { ptr('pointerdown', a, kind); ptr('pointermove', b, kind); ptr('pointerup', b, kind); };
  const labels = () => [...p.$('zcBody').querySelectorAll('.ax-x')].map((t) => t.textContent);
  return { ...p, ts, svg, json, ptr, drag, labels };
}

test('drag zooms', () => {
  const p = zoomPage();
  p.ptr('pointerdown', 180);
  p.ptr('pointermove', 460);
  const box = p.svg().querySelector('rect.zb');
  assert.ok(box, 'the box is drawn');
  assert.equal(box.getAttribute('y'), '10');
  assert.equal(box.getAttribute('height'), '188');
  p.ptr('pointerup', 460);
  assert.deepEqual(p.json().z, { from: 'b', to: 'd' });
  assert.deepEqual(p.labels(), ['b', 'c', 'd']);
  assert.equal(p.d.querySelectorAll('rect.zb').length, 0);
});

test('short drag does nothing', () => {
  const p = zoomPage();
  p.drag(100, 102);
  assert.equal(p.json().z, undefined);
});

test('touch does not zoom', () => {
  const p = zoomPage();
  p.drag(180, 460, 'touch');
  assert.equal(p.json().z, undefined);
  assert.equal(p.d.querySelectorAll('rect.zb').length, 0);
});

test('non-zoomable chart ignores drags', () => {
  const p = zoomPage({ zoom: undefined });
  const before = p.ts();
  assert.ok(!before.hasAttribute('data-kit-zoom'));
  p.drag(180, 460);
  assert.equal(p.ts(), before);
  assert.equal(p.json().z, undefined);
  assert.equal(p.d.querySelectorAll('rect.zb').length, 0);
});

test('escape cancels a drag', () => {
  const p = zoomPage();
  p.ptr('pointerdown', 180);
  p.ptr('pointermove', 460);
  p.d.dispatchEvent(new p.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(p.d.querySelectorAll('rect.zb').length, 0);
  p.ptr('pointerup', 460);
  assert.equal(p.json().z, undefined);
});

test('reset clears zoom', () => {
  const p = zoomPage({ z: { from: 'b', to: 'c' } });
  assert.deepEqual(p.labels(), ['b', 'c']);
  p.click(p.$('zcBody').querySelector('[data-kit-zoom-reset]'));
  assert.equal(p.json().z, undefined);
  assert.deepEqual(p.labels(), ['a', 'b', 'c', 'd']);
  assert.equal(p.$('zcBody').querySelector('[data-kit-zoom-reset]'), null);
});

test('hover uses the slice', () => {
  const p = zoomPage({ z: { from: 'b', to: 'c' } });
  p.svg().dispatchEvent(new p.w.MouseEvent('mousemove', { bubbles: true, clientX: 10 }));
  const tip = p.ts().querySelector('.tip');
  assert.equal(tip.hidden, false);
  assert.equal(tip.querySelector('.tip-d').textContent, 'b');
  assert.match(tip.innerHTML, /A<b>2<\/b>/);
});

test('a move with no button pressed ends the drag', () => {
  const p = zoomPage();
  p.ptr('pointerdown', 180);
  p.ptr('pointermove', 300);
  assert.ok(p.svg().querySelector('rect.zb'));
  p.ptr('pointermove', 460, 'mouse', 0, 0);
  assert.equal(p.d.querySelectorAll('rect.zb').length, 0);
  p.ptr('pointerup', 460);
  assert.equal(p.json().z, undefined);
});

test('an append keeps the hover on the new chart', () => {
  const p = zoomPage();
  p.svg().dispatchEvent(new p.w.MouseEvent('mousemove', { bubbles: true, clientX: 300 }));
  assert.equal(p.ts().querySelector('.tip-d').textContent, 'b');
  const old = p.ts();
  assert.equal(p.w.kitTsAppend(p.d, 'zc', 'e', [5]), true);
  assert.notEqual(p.ts(), old);
  const tip = p.ts().querySelector('.tip');
  assert.equal(tip.hidden, false);
  assert.equal(tip.querySelector('.tip-d').textContent, 'c');
  assert.equal(p.ts().querySelector('.xh').getAttribute('visibility'), 'visible');
});

test('reset zoom moves focus to the panel menu', () => {
  const p = zoomPage({ z: { from: 'b', to: 'c' } });
  const menu = p.d.querySelector('[data-card="zc"] [data-kit-menu]');
  assert.ok(menu);
  p.click(p.$('zcBody').querySelector('[data-kit-zoom-reset]'));
  assert.equal(p.d.activeElement, menu);
});

test('reset zoom outside a panel focuses the new chart', () => {
  const p = page('<div id="bare"></div>');
  p.$('bare').innerHTML = p.w.tsHtml({ id: 'bz', labels: ['a', 'b', 'c'], series: [{ name: 'A', values: [1, 2, 3] }], zoom: true, z: { from: 'a', to: 'b' } });
  p.click(p.$('bare').querySelector('[data-kit-zoom-reset]'));
  const ts = p.$('bare').querySelector('.ts[data-kit-ts]');
  assert.equal(ts.getAttribute('tabindex'), '-1');
  assert.equal(p.d.activeElement, ts);
});

test('with gaps "break" kitTsAppend keeps a missing value missing and the hover shows a dash for it', () => {
  const p = page('<div id="gb"></div>');
  const o = { id: 'gc', labels: ['a', 'b'], series: [{ name: 'S', values: [3, 4] }], keep: 3, w: 600, height: 220 };
  p.$('gb').innerHTML = p.w.tsHtml({ ...o, gaps: 'break' }) + '<div id="plain">' + p.w.tsHtml({ ...o, id: 'pc' }) + '</div>';
  assert.equal(p.w.kitTsAppend(p.$('gb'), 'gc', 'c', [null]), true);
  assert.equal(p.w.kitTsAppend(p.$('plain'), 'pc', 'c', [null]), true);
  const data = (id) => p.w.kitTsData([...p.$('gb').querySelectorAll('.ts[data-kit-ts]')].find(t => p.w.kitTsData(t).id === id));
  assert.deepEqual([...data('gc').series[0].values], [3, 4, null]);
  assert.deepEqual([...data('pc').series[0].values], [3, 4, 0]);
  const ts = [...p.$('gb').querySelectorAll('.ts[data-kit-ts]')].find(t => p.w.kitTsData(t).id === 'gc');
  // 600 wide, 34 left and 8 right: the third of three steps starts at 406.
  p.w.kitTsHover(ts, 500);
  assert.match(ts.querySelector('.tip').innerHTML, /S<b>—<\/b>/);
  p.w.kitTsHover(ts, 100);
  assert.match(ts.querySelector('.tip').innerHTML, /S<b>3<\/b>/);
});

test('with fullRedraw, full size redraws a chart at the full width with the free height, and closing restores it', () => {
  const p = page(row('r', panel('fp', '')));
  p.w.kitConfigure({ fullRedraw: true });
  const P = p.w.HTMLElement.prototype, full = (el) => !!el.closest('.panel.full');
  // jsdom lays nothing out: the panel body is 1500 x 800 when full size and 600 wide otherwise; a legend is 20 high above its chart.
  Object.defineProperty(P, 'clientWidth', { configurable: true, get() { return this.classList.contains('p-body') ? (full(this) ? 1500 : 600) : 0; } });
  Object.defineProperty(P, 'clientHeight', { configurable: true, get() { return this.classList.contains('p-body') && full(this) ? 800 : 0; } });
  P.getBoundingClientRect = function () {
    const h = this.classList.contains('ts') ? p.w.kitTsData(this).height : this.classList.contains('legend') ? 20 : 0;
    const top = this.classList.contains('ts') ? 120 : 100;
    return { top, bottom: top + h, height: h, left: 0, right: 0, width: 0 };
  };
  try {
    p.$('fpBody').innerHTML = p.w.tsHtml({ id: 'fc', labels: ['a', 'b'], series: [{ name: 'S', values: [1, 2] }], w: 600, height: 170 });
    const box = () => p.$('fpBody').querySelector('svg').getAttribute('viewBox');
    const pnl = p.d.querySelector('[data-card="fp"]');
    p.w.kitSetFull(pnl, true);
    assert.equal(box(), '0 0 1500 780');
    p.w.kitSetFull(pnl, false);
    assert.equal(box(), '0 0 600 170');
    p.w.kitConfigure({ fullRedraw: false });
    const svg = p.$('fpBody').querySelector('svg');
    p.w.kitSetFull(pnl, true);
    assert.equal(p.$('fpBody').querySelector('svg'), svg);
  } finally {
    ['clientWidth', 'clientHeight', 'getBoundingClientRect'].forEach(k => { delete P[k]; });
  }
});

test('with fullRedraw, a chart filled again while its panel is full size (a range change) draws at the full-size box', () => {
  const p = page(row('r', panel('fr', '')));
  p.w.kitConfigure({ fullRedraw: true });
  const P = p.w.HTMLElement.prototype, full = (el) => !!el.closest('.panel.full');
  // jsdom lays nothing out: the panel body is 1500 x 800 when full size and 600 wide otherwise; a legend is 20 high above its chart.
  Object.defineProperty(P, 'clientWidth', { configurable: true, get() { return this.classList.contains('p-body') ? (full(this) ? 1500 : 600) : 0; } });
  Object.defineProperty(P, 'clientHeight', { configurable: true, get() { return this.classList.contains('p-body') && full(this) ? 800 : 0; } });
  P.getBoundingClientRect = function () {
    const h = this.classList.contains('ts') ? p.w.kitTsData(this).height : this.classList.contains('legend') ? 20 : 0;
    const top = this.classList.contains('ts') ? 120 : 100;
    return { top, bottom: top + h, height: h, left: 0, right: 0, width: 0 };
  };
  try {
    const draw = (labels) => p.w.kitFill('frBody', p.w.tsHtml({ id: 'rc', labels, series: [{ name: 'S', values: labels.map(() => 1) }], w: 600, height: 170 }));
    const box = () => p.$('frBody').querySelector('svg').getAttribute('viewBox');
    draw(['a', 'b']);
    const pnl = p.d.querySelector('[data-card="fr"]');
    p.w.kitSetFull(pnl, true);
    assert.equal(box(), '0 0 1500 780');
    draw(['a', 'b', 'c']);
    assert.equal(box(), '0 0 1500 780');
    p.w.kitSetFull(pnl, false);
    assert.equal(box(), '0 0 600 170');
    draw(['a']);
    assert.equal(box(), '0 0 600 170');
  } finally {
    ['clientWidth', 'clientHeight'].forEach(k => { delete P[k]; });
    delete P.getBoundingClientRect;
  }
});

test('kitTsAppend adds to avg lists and the hover shows peak and average for peak bars', () => {
  const p = page('<div id="pk"></div>');
  p.$('pk').innerHTML = p.w.tsHtml({ id: 'pkc', labels: ['a', 'b'], series: [{ name: 'Gen', values: [8, 0], avg: [4, 0] }], keep: 3, w: 600, height: 220, bars: 'peak', gaps: 'break' });
  assert.equal(p.w.kitTsAppend(p.$('pk'), 'pkc', 'c', [6], [3]), true);
  assert.equal(p.w.kitTsAppend(p.$('pk'), 'pkc', 'd', [null]), true);
  const ts = p.$('pk').querySelector('.ts[data-kit-ts]'), o = p.w.kitTsData(ts);
  assert.deepEqual([...o.series[0].values], [0, 6, null]);
  assert.deepEqual([...o.series[0].avg], [0, 3, null]);
  assert.equal(o.bars, 'peak');
  // 600 wide, 34 left and 8 right: three steps of about 186 from 34.
  p.w.kitTsHover(ts, 300);
  assert.match(ts.querySelector('.tip').innerHTML, /Gen<b>peak 6 · avg 3<\/b>/);
  p.w.kitTsHover(ts, 100);
  assert.match(ts.querySelector('.tip').innerHTML, /Gen<b>0<\/b>/);
  p.w.kitTsHover(ts, 500);
  assert.match(ts.querySelector('.tip').innerHTML, /Gen<b>—<\/b>/);
});

test('the hover of a split peak chart lists every series, with a dash for a missing average', () => {
  const p = page('<div id="sp"></div>');
  p.$('sp').innerHTML = p.w.tsHtml({ id: 'spc', labels: ['a', 'b'], w: 600, height: 220, bars: 'peak', split: true, gaps: 'break',
    series: [{ name: 'Gen', values: [8, 5], avg: [4, null] }, { name: 'Prompt', values: [700, 0], avg: [350, 0] }] });
  const ts = p.$('sp').querySelector('.ts[data-kit-ts]');
  p.w.kitTsHover(ts, 100);
  assert.match(ts.querySelector('.tip').innerHTML, /Gen<b>peak 8 · avg 4<\/b>.*Prompt<b>peak 700 · avg 350<\/b>/);
  p.w.kitTsHover(ts, 500);
  assert.match(ts.querySelector('.tip').innerHTML, /Gen<b>peak 5 · avg —<\/b>.*Prompt<b>0<\/b>/);
});
