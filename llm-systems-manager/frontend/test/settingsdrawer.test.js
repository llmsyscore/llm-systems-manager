// js/lib/settingsdrawer.js — presets per column count, theme catalog +
// legacy migration, and the per-page drawer scope (#817).
import { describe, it, expect } from 'vitest';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

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
    expect(L.settingsScope('dashboard', 'vllm').label).toBe('Dashboards · vLLM');
    expect(L.settingsScope('dashboard', 'manager').cols).toBe('managerCols');
  });

  it('proxy tabs, control tabs and non-card dashboard sub-tabs get no card sections', () => {
    for (const tab of ['llm', 'events', 'tools', 'admin']) {
      expect(L.settingsScope(tab).kind, tab).toBe('none');
    }
    expect(L.settingsScope('tools').label).toBe('Tools');
    expect(L.settingsScope('dashboard', 'energy')).toMatchObject({ kind: 'none', label: 'Dashboards · Energy' });
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

const html = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'index.html'), 'utf8');

describe('flow engine (#823)', () => {
  const gridIds = ['cardGrid', 'lmsCardGrid', 'vllmCardGrid', 'managerCardGrid'];

  it('every card in the four dashboard grids has a declared role', () => {
    const doc = new JSDOM(html).window.document;
    for (const gid of gridIds) {
      const grid = doc.getElementById(gid);
      expect(grid, gid).not.toBeNull();
      const ids = [...grid.querySelectorAll(':scope > [data-card]')].map(c => c.dataset.card);
      expect(ids.length, gid).toBeGreaterThan(0);
      for (const id of ids) expect(L.CARD_ROLES[id], `${gid} ${id}`).toBeDefined();
    }
    for (const r of Object.values(L.CARD_ROLES)) expect(L.ROLES).toContain(r);
  });

  it('roleOf resolves pinned shells to their home card and unknown ids to stats', () => {
    expect(L.roleOf('gpu')).toBe('chart');
    expect(L.roleOf('ov-borrow-smart-device')).toBe('table');
    expect(L.roleOf('nope')).toBe('stats');
    expect(L.roleOf(undefined)).toBe('stats');
  });

  it('hero cards exist and belong to their page', () => {
    for (const [page, id] of Object.entries(L.HERO_CARDS)) {
      expect(L.CARD_PAGES[page], page).toBeDefined();
      expect(L.CARD_ROLES[id], `${page} ${id}`).toBeDefined();
    }
  });

  it('roleWidth follows the preset, widens the hero only under Hero, and clamps to the track count', () => {
    expect(L.roleWidth('uniform', 'chart', true, 3)).toBe(1);
    expect(L.roleWidth('charts', 'chart', false, 3)).toBe(2);
    expect(L.roleWidth('charts', 'stats', false, 3)).toBe(1);
    expect(L.roleWidth('hero', 'stats', true, 3)).toBe(2);
    expect(L.roleWidth('hero', 'chart', false, 3)).toBe(1);
    expect(L.roleWidth('tables', 'table', false, 4)).toBe(2);
    expect(L.roleWidth('tables', 'list', false, 4)).toBe(2);
    expect(L.roleWidth('charts', 'chart', false, 1)).toBe(1);
    expect(L.roleWidth('bogus', 'chart', false, 3)).toBe(1);
    for (const p of Object.values(L.ROLE_PRESETS)) {
      for (const w of Object.values(p.widths)) expect(w).toBeLessThanOrEqual(2);
      if (p.hero) expect(p.hero).toBeLessThanOrEqual(2);
    }
  });

  it('flowSpan covers the card height with unit rows plus gaps and never returns 0', () => {
    expect(L.flowSpan(8, 8, 8)).toBe(1);
    expect(L.flowSpan(9, 8, 8)).toBe(2);
    expect(L.flowSpan(200, 8, 8)).toBe(13);
    expect(L.flowSpan(200, 8, 16)).toBe(9);
    expect(L.flowSpan(0, 8, 8)).toBe(1);
    expect(L.flowSpan(100, 0, -1)).toBe(L.flowSpan(100, L.FLOW_UNIT_PX, L.FLOW_ROW_GAP_PX));
    for (const h of [37, 141, 333, 1000]) {
      const n = L.flowSpan(h, 8, 8);
      expect(n * 8 + (n - 1) * 8).toBeGreaterThanOrEqual(h);
      expect((n - 1) * 8 + (n - 2) * 8).toBeLessThan(h);
    }
  });

  it('engine, density and role-preset normalizers fall back to defaults', () => {
    expect(L.normalizeEngine('flow')).toBe('flow');
    expect(L.normalizeEngine(undefined)).toBe('grid');
    expect(L.normalizeEngine('dense')).toBe('grid');
    expect(L.normalizeDensity('compact')).toBe('compact');
    expect(L.normalizeDensity('tiny')).toBe('comfortable');
    expect(L.normalizeRolePreset('charts')).toBe('charts');
    expect(L.normalizeRolePreset('hero-3')).toBe('uniform');
  });
});

describe('fresh-install defaults (#823)', () => {
  it('a layout with no card state gets flow + compact + auto columns on every page', () => {
    const lay = { theme: 'oled', logHeights: { a: 1 }, llmSections: {} };
    expect(L.isFreshLayout(lay)).toBe(true);
    expect(L.applyFreshDefaults(lay)).toBe(true);
    expect(lay.layoutEngine).toBe('flow');
    expect(lay.density).toBe('compact');
    for (const p of Object.values(L.CARD_PAGES)) expect(lay[p.cols], p.cols).toBe('auto');
    expect(lay.theme).toBe('oled');
  });

  it('any saved card state, engine or density keeps the layout as it is', () => {
    for (const lay of [
      { order: ['gpu'] }, { hidden: ['ups'] }, { cols: 3 }, { overallBorrowed: ['gpu'] },
      { cardSizes: { gpu: '2x1' } }, { sizesByAgent: { llama: {} } }, { layoutEngine: 'grid' }, { density: 'comfortable' },
    ]) {
      const before = JSON.stringify(lay);
      expect(L.isFreshLayout(lay), before).toBe(false);
      expect(L.applyFreshDefaults(lay)).toBe(false);
      expect(JSON.stringify(lay)).toBe(before);
    }
    expect(L.isFreshLayout({ order: [], cardSizes: {} })).toBe(true);
  });
});

describe('card label maps cover their grids', () => {
  it('every card in each dashboard grid has a CARD_LABELS_* entry', () => {
    const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'js', 'foundation.js'), 'utf8');
    const keysOf = (name) => {
      const m = src.match(new RegExp(`const ${name} = \\{([\\s\\S]*?)\\n\\};`));
      expect(m, name).not.toBeNull();
      return [...m[1].matchAll(/^\s*'([^']+)':/gm)].map(x => x[1]);
    };
    const doc = new JSDOM(html).window.document;
    const pairs = { cardGrid: 'CARD_LABELS', lmsCardGrid: 'CARD_LABELS_LMS', vllmCardGrid: 'CARD_LABELS_VLLM', managerCardGrid: 'CARD_LABELS_MANAGER' };
    for (const [gid, name] of Object.entries(pairs)) {
      const labels = keysOf(name);
      const ids = [...doc.getElementById(gid).querySelectorAll(':scope > [data-card]')].map(c => c.dataset.card);
      for (const id of ids) expect(labels, `${gid} ${id} missing from ${name}`).toContain(id);
    }
  });
});
