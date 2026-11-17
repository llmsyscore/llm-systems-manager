// js/lib/settingsdrawer.js — presets per column count, theme catalog +
// legacy migration, and the per-page drawer scope (#817).
import { describe, it, expect, vi } from 'vitest';
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
  it('two themes', () => {
    expect(L.THEME_IDS).toEqual(['dark', 'light']);
    expect(L.THEMES.every(t => t.swatch.length === 5 && typeof t.dark === 'boolean')).toBe(true);
    expect(L.DEFAULT_THEME).toBe('dark');
    expect(L.SYSTEM_LIGHT_THEME).toBe('light');
  });

  it('legacy names map', () => {
    const want = {
      dark: 'dark', medium: 'dark', light: 'light', modern: 'dark', slate: 'dark',
      enterprise: 'dark', oled: 'dark', graphite: 'dark', frost: 'light', classic: 'dark', '': 'dark', nope: 'dark',
    };
    for (const [name, theme] of Object.entries(want)) expect(L.normalizeTheme(name), name).toBe(theme);
    expect(L.normalizeTheme(undefined)).toBe('dark');
  });

  it('follow system', () => {
    expect(L.effectiveTheme('dark', true, true)).toBe('light');
    expect(L.effectiveTheme('light', true, false)).toBe('dark');
    expect(L.effectiveTheme('graphite', false, true)).toBe('dark');
  });
});

describe('settingsScope', () => {
  it('card pages get the card sections with their layout keys', () => {
    expect(L.settingsScope('overall')).toMatchObject({ kind: 'cards', cols: 'overallCols', grid: 'overallGrid', borrowed: 'overallBorrowed' });
    expect(L.settingsScope('dashboard', 'llamacpp')).toMatchObject({ kind: 'cards', hidden: 'hidden', cols: 'cols', grid: 'cardGrid' });
    expect(L.settingsScope('dashboard', undefined).key).toBe('dashboard/all');
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

  it('All hosts gets its rows with their layout key', () => {
    expect(L.settingsScope('dashboard', 'all')).toMatchObject({ kind: 'rows', key: 'dashboard/all', label: 'Dashboards · All hosts',
      rowsHidden: 'allHostsRowsHidden', root: 'dash-all' });
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

describe('Overall drawer cards section (#1432)', () => {
  const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'js', 'foundation.js'), 'utf8');
  const fn = (name) => {
    const m = src.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
    expect(m, name).not.toBeNull();
    return m[0];
  };
  const overallTab = new JSDOM(html).window.document.getElementById('overallTab').outerHTML;

  // The real drawer chips, row list, row toggle and click binding on the real #overallTab rows.
  function setup(saved) {
    document.body.innerHTML = overallTab + '<div id="settingsOverlay"><section id="sdCards"></section></div>';
    (0, eval)([fn('_sdChip'), fn('_sdRenderCards'), fn('_kitRows'), fn('_applyRowsHidden'), fn('toggleOverallRow'), fn('_sdBind'),
      'window._sdChip = _sdChip; window._sdRenderCards = _sdRenderCards; window._kitRows = _kitRows;',
      'window._applyRowsHidden = _applyRowsHidden; window.toggleOverallRow = toggleOverallRow; window._sdBind = _sdBind;'].join('\n'));
    window._sdBound = false;
    window._activeTab = 'overall';
    window.layout = { overallBorrowed: ['gpu'], ...saved };
    window._esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
    window._cardLabel = (id) => id;
    window.CARD_LABELS = { gpu: 'GPU' };
    window.CARD_LABELS_LMS = {};
    window.CARD_LABELS_VLLM = {};
    window.CARD_LABELS_MANAGER = {};
    window.saveLayout = vi.fn();
    window._sdRerenderCards = () => window._sdRenderCards({ kind: 'cards' });
    window._sdBind();
    window._sdRenderCards({ kind: 'cards' });
    return document.getElementById('sdCards');
  }
  const chips = (el) => [...el.querySelectorAll('[data-sd="row"]')].map(b => [b.dataset.id, b.textContent, b.getAttribute('aria-pressed')]);

  it('lists the pinned cards and no longer offers the old band sections', () => {
    const el = setup({});
    expect(el.textContent).toContain('Pinned from other pages');
    expect(el.querySelector('[data-sd="pin"][data-id="gpu"]').getAttribute('aria-pressed')).toBe('true');
    expect(el.querySelector('[data-sd="strip"]')).toBeNull();
  });

  it('settings drawer lists the six rows and hides one', () => {
    document.body.innerHTML = '';
    const el0 = setup({});
    // A dressed fold button carries the kit's chevron; the chip label leaves it out.
    document.querySelector('#overallTab section.row[data-row="fleet"] button[data-row-fold]')
      .insertAdjacentHTML('afterbegin', '<span class="chev" aria-hidden="true">▾</span>');
    window._sdRenderCards({ kind: 'cards' });
    expect(el0.textContent).toContain('Sections on this page');
    expect(el0.textContent).toContain('6 of 6 shown');
    expect(chips(el0)).toEqual([
      ['fleet', 'At a glance', 'true'], ['throughput', 'Throughput', 'true'], ['servers', 'LLM servers', 'true'],
      ['power', 'Power and energy', 'true'], ['watch', 'Watch list', 'true'], ['pinned', 'Pinned', 'true'],
    ]);
    el0.querySelector('[data-sd="row"][data-id="power"]').click();
    expect(window.layout.overallRowsHidden).toEqual(['power']);
    expect(document.querySelector('#overallTab section.row[data-row="power"]').hidden).toBe(true);
    expect(document.querySelector('#overallTab section.row[data-row="watch"]').hidden).toBe(false);
    expect(window.saveLayout).toHaveBeenCalledTimes(1);
    expect(el0.textContent).toContain('5 of 6 shown');
    expect(el0.querySelector('[data-sd="row"][data-id="power"]').getAttribute('aria-pressed')).toBe('false');
    el0.querySelector('[data-sd="row"][data-id="power"]').click();
    expect(window.layout.overallRowsHidden).toEqual([]);
    expect(document.querySelector('#overallTab section.row[data-row="power"]').hidden).toBe(false);

    // A saved list applies on load and keeps only ids of rows on the page.
    const el = setup({ overallRowsHidden: ['pinned', 'gone'] });
    window._applyRowsHidden();
    expect(document.querySelector('#overallTab section.row[data-row="pinned"]').hidden).toBe(true);
    expect(chips(el).find(c => c[0] === 'pinned')[2]).toBe('false');
    el.querySelector('[data-sd="row"][data-id="fleet"]').click();
    expect(window.layout.overallRowsHidden).toEqual(['pinned', 'fleet']);
    expect(el.querySelectorAll('input[type="checkbox"]').length).toBe(0);
  });

  it('hiding or showing a row from the drawer refreshes the rows\' Move up and Move down', () => {
    document.body.innerHTML = '';
    (0, eval)(readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'kit', 'kit.js'), 'utf8'));
    const el = setup({});
    const tab = document.getElementById('overallTab');
    window.kitRowDress(tab);
    window.kitRowMenus(tab);
    const move = (row, dir) => tab.querySelector(`section.row[data-row="${row}"] button[data-row-move="${dir}"]`).disabled;
    expect(move('fleet', -1)).toBe(true);
    expect(move('throughput', -1)).toBe(false);
    el.querySelector('[data-sd="row"][data-id="fleet"]').click();
    expect(move('throughput', -1)).toBe(true);
    el.querySelector('[data-sd="row"][data-id="pinned"]').click();
    expect(move('watch', 1)).toBe(true);
    el.querySelector('[data-sd="row"][data-id="fleet"]').click();
    expect(move('fleet', -1)).toBe(true);
    expect(move('throughput', -1)).toBe(false);
  });
});

describe('All hosts drawer sections (#1432)', () => {
  const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'js', 'foundation.js'), 'utf8');
  const fn = (name) => {
    const m = src.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
    expect(m, name).not.toBeNull();
    return m[0];
  };
  const page = new JSDOM(html).window.document.getElementById('dash-all').outerHTML;

  it('lists the two All hosts rows and hides one', () => {
    document.body.innerHTML = page + '<div id="settingsOverlay"><section id="sdCards"></section></div>';
    (0, eval)([fn('_sdChip'), fn('_sdRenderCards'), fn('_kitRows'), fn('_applyRowsHidden'), fn('toggleAllHostsRow'), fn('_sdBind'),
      'window._sdRenderCards = _sdRenderCards; window._applyRowsHidden = _applyRowsHidden; window._sdBind = _sdBind;'].join('\n'));
    window._sdBound = false;
    window._activeTab = 'dashboard';
    window.layout = {};
    window._esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
    window.saveLayout = vi.fn();
    const scope = L.settingsScope('dashboard', 'all');
    window._sdRerenderCards = () => window._sdRenderCards(scope);
    window._sdBind();
    window._sdRenderCards(scope);
    const el = document.getElementById('sdCards');
    const chips = () => [...el.querySelectorAll('[data-sd="ahrow"]')].map(b => [b.dataset.id, b.textContent, b.getAttribute('aria-pressed')]);
    expect(el.textContent).toContain('Sections on this page');
    expect(el.textContent).toContain('2 of 2 shown');
    expect(chips()).toEqual([['ah-now', 'Right now', 'true'], ['ah-cmp', 'Compare over time', 'true']]);
    el.querySelector('[data-sd="ahrow"][data-id="ah-cmp"]').click();
    expect(window.layout.allHostsRowsHidden).toEqual(['ah-cmp']);
    expect(document.querySelector('#dash-all section.row[data-row="ah-cmp"]').hidden).toBe(true);
    expect(document.querySelector('#dash-all section.row[data-row="ah-now"]').hidden).toBe(false);
    expect(window.saveLayout).toHaveBeenCalledTimes(1);
    expect(el.textContent).toContain('1 of 2 shown');
    el.querySelector('[data-sd="ahrow"][data-id="ah-cmp"]').click();
    expect(window.layout.allHostsRowsHidden).toEqual([]);
    expect(document.querySelector('#dash-all section.row[data-row="ah-cmp"]').hidden).toBe(false);
    window.layout = { allHostsRowsHidden: ['ah-now', 'gone'] };
    window._applyRowsHidden();
    expect(document.querySelector('#dash-all section.row[data-row="ah-now"]').hidden).toBe(true);
    expect(el.querySelectorAll('input[type="checkbox"]').length).toBe(0);
  });
});

describe('Dashboards · Forecast drawer (#1432)', () => {
  const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'js', 'foundation.js'), 'utf8');
  const fn = (name) => {
    const m = src.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
    expect(m, name).not.toBeNull();
    return m[0];
  };
  const dash = new JSDOM(html).window.document.getElementById('dashboardTab').outerHTML;

  it('shows the no-cards notice, not All hosts sections or llama.cpp cards', () => {
    expect(L.settingsScope('dashboard', 'forecast')).toMatchObject({ kind: 'none', key: 'dashboard/forecast' });
    document.body.innerHTML = dash + '<section id="sdNoCards" hidden></section><section id="sdCards"></section><section id="sdLayout"></section>';
    document.querySelectorAll('#dashboardTab .sub-tab-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('dash-forecast').classList.add('active');
    (0, eval)([fn('_getDashSubTab'), fn('_sdScope'), fn('renderSettingsPanel'),
      'window._getDashSubTab = _getDashSubTab; window.renderSettingsPanel = renderSettingsPanel;'].join('\n'));
    window.SettingsLib = L;
    window._activeTab = 'dashboard';
    window._sdRenderCards = vi.fn();
    window._sdRenderLayout = vi.fn();
    for (const n of ['_sdRenderAccount', '_sdRenderHeader', '_sdRenderAppearance', '_sdRenderRefresh']) window[n] = vi.fn();
    expect(window._getDashSubTab()).toBe('forecast');
    window.renderSettingsPanel();
    expect(document.getElementById('sdNoCards').hidden).toBe(false);
    expect(document.getElementById('sdCards').hidden).toBe(true);
    expect(document.getElementById('sdLayout').hidden).toBe(true);
    expect(window._sdRenderCards).not.toHaveBeenCalled();
  });
});
