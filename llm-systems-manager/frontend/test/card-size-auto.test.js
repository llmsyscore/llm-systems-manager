// #735: "auto" (content-height) card size joins the size cycle; per-card
// defaults; borrowed shells inherit the base card's default. Real source in jsdom.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { fnSrc, srcFile } from './helpers/harness.js';

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
    cycle, classes,
    src.match(/const _CARD_DEFAULT_SIZE = '[^']*';/)[0].replace(/^const/, 'window.'),
    fn('_defaultCardSize'), fn('_sizeCols'), fn('_sizeLabel'), fn('_allowedSizes'),
    fn('_nextCardSize'), fn('_gridColCount'), fn('_clampSize'),
    fn('_applyCardSize'), fn('_cycleCardSize'),
    'window._applyCardSize = _applyCardSize; window._cycleCardSize = _cycleCardSize;',
    'window._clampSize = _clampSize; window._defaultCardSize = _defaultCardSize;',
  ].join('\n'));
});

const card = (id) => document.querySelector(`[data-card="${id}"]`);

describe('auto card size (#735)', () => {
  it('cycles auto → 1x1 → 2x1 → 2x2 → 1x2 and back', () => {
    const c = card('cpu');
    window._applyCardSize(c, 'auto');
    const seen = [];
    for (let i = 0; i < 5; i++) { window._cycleCardSize(c); seen.push(c.dataset.size); }
    expect(seen).toEqual(['1x1', '2x1', '2x2', '1x2', 'auto']);
  });
  it('auto is the default: cycling to it clears the saved entry, anything else is saved', () => {
    const c = card('cpu');
    window._applyCardSize(c, 'auto');
    window._cycleCardSize(c);                    // auto → 1x1: explicit
    expect(c.classList.contains('size-auto')).toBe(false);
    expect(window._sizes).toEqual({ cpu: '1x1' });
    window._applyCardSize(c, '1x2');
    window._cycleCardSize(c);                    // 1x2 → auto: the default
    expect(c.classList.contains('size-auto')).toBe(true);
    expect(window._sizes).toEqual({});
  });
  it('the picker tooltip names the current size and the next one', () => {
    const c = card('cpu');
    c.appendChild(Object.assign(document.createElement('button'), { className: 'card-size-btn' }));
    window._applyCardSize(c, 'auto');
    expect(c.querySelector('.card-size-btn').title).toBe('Card size: auto (content height) · click for 1×1');
    window._applyCardSize(c, '2x2');
    expect(c.querySelector('.card-size-btn').title).toBe('Card size: 2×2 · click for 1×2');
  });
  it('auto survives a 1-column grid and clamps garbage to 1x1', () => {
    window.getComputedStyle = () => ({ gridTemplateColumns: '1fr' });
    const c = card('cpu');
    window._applyCardSize(c, 'auto');
    window._cycleCardSize(c);
    expect(c.dataset.size).toBe('1x1');
    window._cycleCardSize(c);
    expect(c.dataset.size).toBe('1x2');            // 2-column sizes skipped
    expect(window._clampSize('auto', 3)).toBe('auto');
    expect(window._clampSize('9x9', 3)).toBe('3x2');
    expect(window._clampSize('nope', 3)).toBe('1x1');
  });
  it('every card and borrowed shell defaults to auto', () => {
    expect(window._defaultCardSize('lms-active')).toBe('auto');
    expect(window._defaultCardSize('ov-borrow-cpu')).toBe('auto');
    expect(window._defaultCardSize(undefined)).toBe('auto');
  });
});
