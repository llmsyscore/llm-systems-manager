// js/lib/settingsdrawer.js — presets per column count, theme catalog +
// legacy migration, and the per-page drawer scope (#817).
import { describe, it, expect } from 'vitest';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const L = require('../js/lib/settingsdrawer.js');

describe('layout presets', () => {
  it('every preset only uses sizes that fit its column count', () => {
    for (const [id, p] of Object.entries(L.PRESETS)) {
      for (const size of Object.values(p.sizes)) {
        const [w, h] = size.split('x').map(Number);
        expect(w, `${id} ${size}`).toBeLessThanOrEqual(p.cols);
        expect(h, `${id} ${size}`).toBeLessThanOrEqual(2);
      }
    }
  });

  it('presetsFor filters by column count and offers one auto-fit preset', () => {
    expect(L.presetsFor(3).map(([id]) => id)).toEqual(
      ['uniform-3', 'hero-3', 'hero-right-3', 'featured-3', 'wide-pair-3', 'tall-pair-3', 'sidebar-3']);
    expect(L.presetsFor(6).length).toBe(2);
    expect(L.presetsFor('auto')).toEqual([['auto-uniform', { label: 'Uniform', cols: 'auto', sizes: {} }]]);
    for (const c of [2, 3, 4, 5, 6]) expect(L.presetsFor(c).length, `cols ${c}`).toBeGreaterThan(0);
  });

  it('matchPreset recognises the shape of the visible cards, treating auto/1x1 alike', () => {
    expect(L.matchPreset(3, { 0: '2x2', 1: 'auto', 2: '1x1' })).toBe('hero-3');
    expect(L.matchPreset(3, { 0: 'auto', 1: '2x2' })).toBe('hero-right-3');
    expect(L.matchPreset(3, {})).toBe('uniform-3');
    expect(L.matchPreset(4, { 0: '2x2', 1: '2x2', 5: 'auto' })).toBe('twin-4');
    expect(L.matchPreset(3, { 2: '2x1' })).toBeNull();
    expect(L.matchPreset('auto', { 0: 'auto' })).toBe('auto-uniform');
  });

  it('gridTemplate + normalizeCols cover 2–6 and auto, falling back to 3', () => {
    expect(L.gridTemplate(4)).toBe('repeat(4, 1fr)');
    expect(L.gridTemplate('auto')).toBe(`repeat(auto-fit, minmax(${L.AUTO_MIN_COL_PX}px, 1fr))`);
    expect(L.normalizeCols('5')).toBe(5);
    expect(L.normalizeCols(9)).toBe(3);
    expect(L.normalizeCols(undefined)).toBe(3);
    expect(L.normalizeCols('auto')).toBe('auto');
  });
});

describe('themes', () => {
  it('drops classic, adds oled/graphite/frost, and keeps every legacy theme still valid', () => {
    expect(L.THEME_IDS).not.toContain('classic');
    for (const t of ['dark', 'medium', 'light', 'modern', 'slate', 'enterprise', 'oled', 'graphite', 'frost']) {
      expect(L.THEME_IDS).toContain(t);
    }
    expect(L.THEMES.every(t => t.swatch.length === 5 && typeof t.dark === 'boolean')).toBe(true);
  });

  it('normalizeTheme migrates classic to oled and unknown names to the default', () => {
    expect(L.normalizeTheme('classic')).toBe('oled');
    expect(L.normalizeTheme('nope')).toBe(L.DEFAULT_THEME);
    expect(L.normalizeTheme(undefined)).toBe('modern');
    expect(L.normalizeTheme('slate')).toBe('slate');
  });

  it('effectiveTheme swaps to the light theme only while following an OS in light mode', () => {
    expect(L.effectiveTheme('modern', false, true)).toBe('modern');
    expect(L.effectiveTheme('modern', true, true)).toBe('frost');
    expect(L.effectiveTheme('modern', true, false)).toBe('modern');
    expect(L.effectiveTheme('light', true, true)).toBe('light');
    expect(L.effectiveTheme('light', true, false)).toBe('modern');
    expect(L.effectiveTheme('classic', true, false)).toBe('oled');
  });
});

describe('settingsScope', () => {
  it('card pages get the card sections with their layout keys', () => {
    expect(L.settingsScope('overall')).toMatchObject({ kind: 'cards', cols: 'overallCols', grid: 'overallGrid', borrowed: 'overallBorrowed' });
    expect(L.settingsScope('dashboard', 'llamacpp')).toMatchObject({ kind: 'cards', hidden: 'hidden', cols: 'cols', grid: 'cardGrid' });
    expect(L.settingsScope('dashboard', undefined).key).toBe('dashboard/llamacpp');
    expect(L.settingsScope('dashboard', 'vllm').label).toBe('Dashboard · vLLM');
    expect(L.settingsScope('dashboard', 'manager').cols).toBe('managerCols');
  });

  it('proxy tabs, control tabs and non-card dashboard sub-tabs get no card sections', () => {
    for (const tab of ['llm', 'events', 'openclaw', 'llmchat', 'imggen', 'admin']) {
      expect(L.settingsScope(tab).kind, tab).toBe('none');
    }
    expect(L.settingsScope('llmchat').label).toBe('LLM Chat');
    expect(L.settingsScope('dashboard', 'energy')).toMatchObject({ kind: 'none', label: 'Dashboard · Energy' });
    expect(L.settingsScope('dashboard', 'openclaw').kind).toBe('none');
  });
});

describe('interval helpers', () => {
  it('clampInterval keeps manual values within 5–300 seconds', () => {
    expect(L.clampInterval(0)).toBe(5);
    expect(L.clampInterval(301)).toBe(300);
    expect(L.clampInterval('12.4')).toBe(12);
    expect(L.clampInterval('x')).toBe(30);
    expect(L.INTERVAL_CHIPS).toEqual([30, 60, 90, 120, 300]);
    expect(L.INTERVAL_CHIPS.every(v => v >= L.INTERVAL_MIN && v <= L.INTERVAL_MAX)).toBe(true);
  });
});
