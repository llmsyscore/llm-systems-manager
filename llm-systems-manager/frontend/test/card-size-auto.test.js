// #735: "auto" (content-height) card size joins the size cycle; per-card
// defaults; borrowed shells inherit the base card's default. Real source in jsdom.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { fnSrc, srcFile, constAsWindowProp } from './helpers/harness.js';

const src = srcFile('js/charts.js');
const fn = (name) => {
  const m = fnSrc(src, name);
  expect(m, `${name} not found`).toBeTruthy();
  return m;
};

beforeEach(() => {
  document.body.innerHTML = '<div class="grid" id="g"><div class="card" data-card="lms-active"></div>'
    + '<div class="card" data-card="cpu"></div><div class="card ov-shell" data-card="ov-borrow-lms-active"></div></div>';
  window._sizes = {};
  window._sizeMapFor = () => window._sizes;
  window._scheduleCardSizesSave = vi.fn();
  window._resizeChartsIn = vi.fn();
  window.getComputedStyle = () => ({ gridTemplateColumns: '1fr 1fr 1fr' });
  const cycle = src.match(/const _CARD_SIZE_CYCLE = \[[^\]]*\];/)[0].replace(/^const/, 'window.');
  const classes = src.match(/const _CARD_SIZE_CLASSES = \[[^\]]*\];/)[0].replace(/^const/, 'window.');
  (0, eval)([
    cycle, classes, constAsWindowProp(src, '_CARD_DEFAULT_SIZE'),
    fn('_defaultCardSize'), fn('_sizeCols'), fn('_gridColCount'), fn('_clampSize'),
    fn('_applyCardSize'), fn('_cycleCardSize'),
    'window._applyCardSize = _applyCardSize; window._cycleCardSize = _cycleCardSize;',
    'window._clampSize = _clampSize; window._defaultCardSize = _defaultCardSize;',
  ].join('\n'));
});

const card = (id) => document.querySelector(`[data-card="${id}"]`);

describe('auto card size (#735)', () => {
  it('cycles 1x1 → auto → 2x1 → 2x2 → 1x2 and back', () => {
    const c = card('cpu');
    window._applyCardSize(c, '1x1');
    const seen = [];
    for (let i = 0; i < 5; i++) { window._cycleCardSize(c); seen.push(c.dataset.size); }
    expect(seen).toEqual(['auto', '2x1', '2x2', '1x2', '1x1']);
  });
  it('auto adds size-auto and persists unless it is the card default', () => {
    const c = card('cpu');
    window._applyCardSize(c, '1x1');
    window._cycleCardSize(c);
    expect(c.classList.contains('size-auto')).toBe(true);
    expect(window._sizes).toEqual({ cpu: 'auto' });
    const l = card('lms-active');
    window._applyCardSize(l, '1x2');
    window._cycleCardSize(l);            // 1x2 → 1x1: explicit, not the default
    expect(window._sizes['lms-active']).toBe('1x1');
    window._cycleCardSize(l);            // 1x1 → auto: the default, entry cleared
    expect(window._sizes['lms-active']).toBeUndefined();
  });
  it('auto survives a 1-column grid and clamps garbage to 1x1', () => {
    window.getComputedStyle = () => ({ gridTemplateColumns: '1fr' });
    const c = card('cpu');
    window._applyCardSize(c, '1x1');
    window._cycleCardSize(c);
    expect(c.dataset.size).toBe('auto');
    expect(window._clampSize('auto', 3)).toBe('auto');
    expect(window._clampSize('9x9', 3)).toBe('3x2');
    expect(window._clampSize('nope', 3)).toBe('1x1');
  });
  it('defaults: lms-active and its borrowed shell are auto, others 1x1', () => {
    expect(window._defaultCardSize('lms-active')).toBe('auto');
    expect(window._defaultCardSize('ov-borrow-lms-active')).toBe('auto');
    expect(window._defaultCardSize('cpu')).toBe('1x1');
    expect(window._defaultCardSize(undefined)).toBe('1x1');
  });
});
