// #1432: pinned cards on Overall all drag by a handle on the left, kit panels by their own grip.
import { test, expect, vi } from 'vitest';
import { srcFile, fnSrc, evalGlobal } from './helpers/harness.js';

test('the Overall grid drags by the card handle or a pinned panel\'s kit grip', () => {
  const fn = fnSrc(srcFile('js/charts.js'), 'initSortable');
  expect(fn).toBeTruthy();
  document.body.innerHTML = '<div id="overallGrid"></div>';
  const made = [];
  window.Sortable = { create: vi.fn((el, o) => made.push([el.id, o.handle])) };
  window.initCardResize = () => {};
  evalGlobal(fn + '\ninitSortable();');
  expect(made).toEqual([['overallGrid', '.card-handle, .grip']]);
  delete window.Sortable;
});

test('card handles sit on the left with the title moved clear of them, and the size button takes the right', () => {
  const css = srcFile('css/base.css');
  expect(css).toMatch(/\.card-handle \{ position: absolute; top: 10px; left: 10px;/);
  expect(css).toMatch(/\.card-handle \+ h3 \{ padding-left: 18px; \}/);
  expect(css).toMatch(/\.card-size-btn \{\s*position: absolute; top: 10px; right: 10px;/);
});

test('the sidebar names the alarm console Events and metrics', () => {
  expect(srcFile('index.html')).toMatch(/data-tab="events">[\s\S]*?<span class="side-lab">Events and metrics<\/span>/);
  expect(srcFile('js/shell.js')).toMatch(/events: 'Events and metrics'/);
});

test('on a pinned kit panel the shell\'s size button sits left of the panel\'s menu', () => {
  expect(srcFile('css/overall.css')).toMatch(/\.ov-shell:has\(> \.panel\) > \.card-size-btn \{ right: 46px; top: 14px; \}/);
});

test('Overall keeps the reader on the same panel when content above changes size', async () => {
  const fn = fnSrc(srcFile('js/foundation.js'), 'keepPlace');
  expect(fn).toBeTruthy();
  let cb = null;
  window.ResizeObserver = class { constructor(f) { cb = f; } observe() {} };
  evalGlobal(fn + '\nwindow.keepPlace = keepPlace;');
  document.body.innerHTML = '<div id="ov"><section class="panel" id="a"></section><section class="panel" id="b"></section></div>';
  const root = document.getElementById('ov'), tops = { a: -300, b: 50 };
  const P = window.HTMLElement.prototype, rect = P.getBoundingClientRect, cw = Object.getOwnPropertyDescriptor(P, 'clientWidth');
  const sy = Object.getOwnPropertyDescriptor(window, 'scrollY');
  Object.defineProperty(window, 'scrollY', { configurable: true, get: () => 900 });
  Object.defineProperty(P, 'clientWidth', { configurable: true, get: () => 1200 });
  P.getBoundingClientRect = function () { const t = tops[this.id]; return t === undefined ? rect.call(this) : { top: t, bottom: t + 200, height: 200 }; };
  window.scrollBy = vi.fn();
  try {
    window.keepPlace(root, '.panel');
    root._keepPlace.note();
    tops.b = 130;
    cb();
    expect(window.scrollBy).toHaveBeenCalledWith(0, 80);
    expect(srcFile('css/overall.css')).toMatch(/#overallTab \{ overflow-anchor: none; \}/);
    expect(srcFile('js/overall.js')).toMatch(/keepPlace\(root, '\.panel, #overallGrid > \.card'\)/);
  } finally {
    P.getBoundingClientRect = rect;
    if (cw) Object.defineProperty(P, 'clientWidth', cw); else delete P.clientWidth;
    if (sy) Object.defineProperty(window, 'scrollY', sy); else delete window.scrollY;
    delete window.ResizeObserver;
  }
});
