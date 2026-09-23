// Unit tests for js/lib/modelcards.js — pure render + state helpers (#765).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import MC from '../js/lib/modelcards.js';

const CSS = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../css/modelcards.css'), 'utf8');

const BASE = {
  id: 'unsloth/Qwen-GGUF:Q4_K_XL', actAttr: 'data-act',
  name: 'Qwen:Q4_K_XL', repo: 'unsloth/Qwen-GGUF:Q4_K_XL',
  pill: { state: 'sleeping', label: 'Sleeping' },
  specs: [{ k: 'Context', v: '124,160' }, { k: 'Reasoning', v: 'on', em: true }],
  stats: [{ l: 'Gen', v: '38.3', unit: 't/s' }],
  primary: { act: 'wake', label: 'Wake' },
  buttons: [{ act: 'edit', label: 'Edit' }],
  menu: [{ act: 'bench', label: 'Benchmark' }, '-', { act: 'delete', label: 'Delete', danger: true }],
};

describe('view helpers', () => {
  it('validates view names with card fallback', () => {
    expect(MC.validView('list')).toBe('list');
    expect(MC.validView('bogus')).toBe('compact');
    expect(MC.viewOf({ modelView: { llama: 'card' } }, 'llama')).toBe('card');
    expect(MC.viewOf(null, 'llama')).toBe('compact');
    expect(MC.viewOf({}, 'lms')).toBe('compact');
  });
});

describe('age', () => {
  const now = Date.parse('2026-08-30T12:00:00Z');
  it('formats ranges', () => {
    expect(MC.age('2026-08-30T11:59:30Z', now)).toBe('just now');
    expect(MC.age('2026-08-30T11:30:00Z', now)).toBe('30m ago');
    expect(MC.age('2026-08-30T02:00:00Z', now)).toBe('10h ago');
    expect(MC.age('2026-08-27T12:00:00Z', now)).toBe('3d ago');
    expect(MC.age('2026-05-30T12:00:00Z', now)).toBe('3mo ago');
  });
  it('rejects garbage', () => {
    expect(MC.age('not-a-date', now)).toBe(null);
    expect(MC.age(null, now)).toBe(null);
  });
});

describe('card rendering', () => {
  it('escapes model ids and labels', () => {
    const html = MC.card({ ...BASE, id: 'a"><img src=x onerror=1>', name: '<b>x</b>' });
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<b>x</b>');
    expect(html).toContain('&lt;b&gt;x&lt;/b&gt;');
  });
  it('renders pill state, specs, stats, menu and actions', () => {
    const html = MC.card(BASE);
    expect(html).toContain('mc-pill p-sleeping');
    expect(html).toContain('Sleeping');
    expect(html).toContain('<em>on</em>');
    expect(html).toContain('mcbtn-pri');
    expect(html).toContain('data-act="wake"');
    expect(html).toContain('data-act="bench"');
    expect(html).toContain('class="danger"');
    expect(html).toContain('mc-menubtn');
  });
  it('disables buttons during a transition', () => {
    const html = MC.card({ ...BASE, transition: true });
    expect(html).toContain('mc-transition');
    expect(html).toMatch(/data-act="wake" data-id="[^"]*" disabled/);
  });
  it('puts stats in an Offline bench section and flags staleness without an age', () => {
    const plain = MC.card(BASE);
    expect(plain).toContain('mc-bsect-off');
    expect(plain).toMatch(/mc-bhdr">Offline bench</);
    expect(plain).not.toContain('mc-stale');
    const withStale = MC.card({ ...BASE, fresh: { stale: true, staleTitle: 'ctx changed' } });
    expect(withStale).toContain('re-bench');
    expect(withStale).toContain('ctx changed');
    expect(MC.card({ ...BASE, fresh: { age: '3d ago' } })).not.toContain('3d ago');
  });
  it('appends the Wh/1k figure to the row tooltip when extra_json carries it, not otherwise', () => {
    const withWh = MC.row({ ...BASE, extraJson: { wh_per_ktok: 1.234 } });
    expect(withWh).toMatch(/mc-rowmet" title="[^"]*1\.23 Wh\/1k[^"]*"/);
    expect(MC.row(BASE)).not.toContain('Wh/1k');
    expect(MC.row({ ...BASE, extraJson: { wh_per_ktok: null } })).not.toContain('Wh/1k');
    expect(MC.row({ ...BASE, extraJson: '{"wh_per_ktok": 0.5}' })).toMatch(/0\.50 Wh\/1k/);
  });
  it('cards show the last run as a cell instead of a tooltip', () => {
    const html = MC.card({ ...BASE, benchAge: '3d ago' });
    expect(html).not.toContain('data-tip');
    expect(html).toMatch(/mc-bsect-off"[^>]*>.*Last run<\/div><div class="v"><b>3d ago<\/b>/s);
    expect(MC.card(BASE)).not.toContain('Last run');
  });
  it('the offline section becomes a button only when benchClick is set', () => {
    const plain = MC.card(BASE);
    expect(plain).not.toMatch(/mc-bsect-off" data-act/);
    const clickable = MC.card({ ...BASE, benchClick: 'bench', liveClick: 'benchlive', live: { stats: [{ l: 'Gen', v: '1' }] } });
    expect(clickable).toMatch(/mc-bsect-off" data-act="bench" data-id="[^"]+" role="button"/);
    expect(clickable).toMatch(/mc-bsect-live" data-act="benchlive" data-id="[^"]+" role="button"/);
    expect(MC.row({ ...BASE, liveClick: 'benchlive', live: { stats: [{ l: 'Gen', v: '1' }] } })).toMatch(/mc-rowlive" title="[^"]*" data-act="benchlive"/);
    expect(MC.row({ ...BASE, benchClick: 'bench' })).toMatch(/mc-rowmet" title="[^"]*" data-act="bench"/);
  });
  it('omits the action bar when there is nothing actionable', () => {
    const html = MC.card({ ...BASE, primary: null, buttons: [], menu: [] });
    expect(html).not.toContain('mc-actions');
  });
});

describe('compact + row rendering', () => {
  it('compact carries open state and drawer', () => {
    expect(MC.compact({ ...BASE, open: true })).toContain('mc-card open');
    expect(MC.compact(BASE)).toContain('aria-expanded="false"');
    expect(MC.compact(BASE)).toContain('mc-drawer');
  });
  it('row folds buttons into the menu and keeps a primary', () => {
    const html = MC.row(BASE);
    expect(html).toContain('mc-dot d-sleeping');
    expect(html).toContain('mcbtn-pri');
    const menuPart = html.slice(html.indexOf('mc-menu'));
    expect(menuPart).toContain('data-act="edit"');
    expect(menuPart).toContain('data-act="delete"');
  });
  it('row header carries both bench labels and rows self-label their stats', () => {
    const html = MC.rowHeader('Offline bench (t/s)', 'Profile', 'not live', 'Live bench (t/s)', 'server run');
    expect(html).toContain('mc-methdr');
    expect(html).toContain('title="not live"');
    expect(html).toContain('Offline bench (t/s)');
    expect(html).toMatch(/mc-rowlive mc-methdr" title="server run"><span>Live bench \(t\/s\)/);
    // header and rows carry the same seven cells so the subgrid columns line up
    const cells = (h) => (h.match(/<(span|div) class="mc-row(met|live|prof|name|cfg|act)|mc-profhdr|<span><\/span>|<span>Model<\/span>|<span>Configuration<\/span>|mc-dot/g) || []).length;
    expect(cells(html)).toBe(7);
    expect(cells(MC.row(BASE))).toBe(7);
    const rowHtml = MC.row(BASE);
    expect(rowHtml).toContain('<span class="rl">Gen</span>');
    expect(rowHtml).toMatch(/mc-rowlive"><span class="mc-bnone">—<\/span>/);
  });
  it('group rows encode collapse state', () => {
    expect(MC.groupRow('unsloth', 2, false)).toContain('▾');
    expect(MC.groupRow('unsloth', 2, true)).toContain('▸');
    expect(MC.groupHeader('a&b', 1, false)).toContain('a&amp;b');
  });
});

describe('per-surface state', () => {
  it('filter matching is case-insensitive substring over all haystacks', () => {
    expect(MC.filterMatch('t1', 'Qwen', 'other')).toBe(true); // no filter set
    // simulate a filter through the internal store via toolbar-less path:
    // filterOf returns '' until initToolbar wires an input, so match stays true.
    expect(MC.filterOf('t1')).toBe('');
  });
  it('collapse / open / busy toggles are per-surface', () => {
    expect(MC.isCollapsed('s1', 'g')).toBe(false);
    MC.toggleGroup('s1', 'g');
    expect(MC.isCollapsed('s1', 'g')).toBe(true);
    expect(MC.isCollapsed('s2', 'g')).toBe(false);
    MC.toggleGroup('s1', 'g');
    expect(MC.isCollapsed('s1', 'g')).toBe(false);

    MC.toggleOpen('s1', 'm');
    expect(MC.isOpen('s1', 'm')).toBe(true);
    expect(MC.isOpen('s2', 'm')).toBe(false);

    MC.setBusy('s1', 'm', 'Loading…');
    expect(MC.busyOf('s1', 'm')).toBe('Loading…');
    expect(MC.busyOf('s2', 'm')).toBe(null);
    MC.clearBusy('s1', 'm');
    expect(MC.busyOf('s1', 'm')).toBe(null);
  });
});

describe('tune chip (#887)', () => {
  it('renders a muted chip for a fresh tune and a warning chip that carries the re-verify action when stale', () => {
    const d = { id: 'org/m:Q4', actAttr: 'data-act', stats: [], fresh: null };
    expect(MC.tuneTag(null, d)).toBe('');
    const fresh = MC.tuneTag({ stale: false, label: 'tuned', title: 'Autotuned on b100', act: 'autotune' }, d);
    expect(fresh).toContain('class="mc-tune"');
    expect(fresh).toContain('data-act="autotune"');
    const stale = MC.tuneTag({ stale: true, label: 'tuned · stale', title: 'tuned on b100 · host now b120', act: 'reverify' }, d);
    expect(stale).toContain('class="mc-tune stale"');
    expect(stale).toContain('data-act="reverify"');
    expect(stale).toContain('data-id="org/m:Q4"');
    expect(stale).toContain('title="tuned on b100 · host now b120"');
  });
  it('badgesHtml and row include the tune chip when the descriptor has one', () => {
    const d = { id: 'x', actAttr: 'data-act', stats: [], fresh: null,
                tune: { stale: true, label: 'tuned · stale', title: 't', act: 'reverify' }, pill: { state: 'idle', label: 'Loaded' } };
    expect(MC.badgesHtml(d.fresh, d)).toContain('mc-tune stale');
    expect(MC.row({ ...d, name: 'x', repo: 'x', specs: [], buttons: [], menu: [] })).toContain('mc-tune stale');
  });

  it('gives the profile chip a bounded, truncating zone regardless of name length (#887)', () => {
    const withShort = { ...BASE, profileHtml: '<span class="mc-profchip mc-prof-edit">mtp</span>', profileText: 'mtp' };
    const withLong = { ...BASE, profileHtml: '<span class="mc-profchip mc-prof-edit">vscode-remote-workspace-profile</span>', profileText: 'vscode-remote-workspace-profile' };
    ['card', 'compact'].forEach(fn => {
      const shortHtml = MC[fn](withShort);
      const longHtml = MC[fn](withLong);
      expect(shortHtml).toContain('<div class="mc-tele-prof" title="mtp">');
      expect(longHtml).toContain('<div class="mc-tele-prof" title="vscode-remote-workspace-profile">');
      // same zone structure either way: one mc-tele-prof zone, one bench block
      expect((shortHtml.match(/mc-tele-prof/g) || []).length).toBe((longHtml.match(/mc-tele-prof/g) || []).length);
      expect(shortHtml).toContain('mc-bench');
      expect(longHtml).toContain('mc-bench');
    });
  });

  it('keeps badges on the profile row and stat cells in the bench block', () => {
    const d = { ...BASE, stats: [{ l: 'Prompt', v: '100', unit: 't/s' }, { l: 'Gen', v: '38.3', unit: 't/s' }],
                fresh: { stale: true, staleTitle: 'ctx changed' },
                tune: { stale: true, label: 'tuned · stale', title: 't', act: 'reverify' } };
    const html = MC.card(d);
    const badges = html.slice(html.indexOf('mc-badges'), html.indexOf('mc-bench'));
    const bench = html.slice(html.indexOf('mc-bench'));
    expect(badges).toContain('mc-stale');
    expect(badges).toContain('mc-tune stale');
    expect(badges).not.toContain('mc-stat"');
    expect(bench).toContain('mc-stat"');
    expect(bench).not.toContain('mc-tune');
  });

  it('still renders the profile and badge zones when a card has no bench data', () => {
    const d = { ...BASE, stats: [], fresh: null,
                profileHtml: '<span class="mc-profchip mc-prof-edit">mtp</span>', profileText: 'mtp',
                tune: { stale: false, label: 'tuned', title: 't', act: 'autotune' } };
    const html = MC.card(d);
    expect(html).toContain('mc-tele-prof');
    expect(html).toContain('mc-badges');
    expect(html).toContain('mc-tune');
    expect(html).not.toContain('mc-bench');
  });

  // A row can carry re-bench AND tuned · stale; a fixed profile track clipped the second chip.
  it('gives the row profile cell a growable track and lets its badges wrap', () => {
    const row = MC.row({ id: 'x', actAttr: 'data-act', name: 'x', repo: 'x', specs: [], stats: [],
                         buttons: [], menu: [], pill: { state: 'idle', label: 'Loaded' },
                         fresh: { stale: true, staleTitle: 'Config changed' },
                         tune: { stale: true, label: 'tuned · stale', title: 't', act: 'reverify' } });
    const prof = row.slice(row.indexOf('mc-rowprof'), row.indexOf('mc-rowact'));
    expect(prof).toContain('re-bench');
    expect(prof).toContain('tuned · stale');
    const grid = CSS.match(/^\.mc-list \{[^}]*grid-template-columns:([^;]+);/m);
    expect(grid).toBeTruthy();
    expect(grid[1]).toContain('minmax(118px, auto)');
    expect(grid[1].trim().split(/\s+(?![^(]*\))/).length).toBe(7);
    expect(CSS).toMatch(/^\.mc-row \{[^}]*grid-template-columns: subgrid/m);
    expect(CSS).toMatch(/\.mc-rowprof \{[^}]*flex-wrap: ?wrap/);
  });
});

describe('live bench section (#893)', () => {
  const live = { stats: [{ l: 'Prompt', v: '180', unit: 't/s' }, { l: 'Gen', v: '42.1', unit: 't/s' }], age: '3h ago',
                 title: 'Live benchmark against the running server · bench: throughput_1k · last run 3h ago' };
  it('renders a Live bench section after Offline bench with its own Last run cell', () => {
    const html = MC.card({ ...BASE, live });
    const off = html.indexOf('mc-bsect-off'), lv = html.indexOf('mc-bsect-live');
    expect(off).toBeGreaterThan(-1);
    expect(lv).toBeGreaterThan(off);
    expect(html.slice(lv)).toMatch(/<div class="mc-bhdr">Live bench<\/div><div class="mc-cells">.*<b>42\.1<\/b> t\/s.*Last run<\/div><div class="v"><b>3h ago<\/b>/s);
    expect(html.slice(off, lv)).not.toContain('Last run');
    expect(html).not.toContain('data-tip');
  });
  it('shows a placeholder for the missing side, and no block at all without either', () => {
    const noOff = MC.card({ ...BASE, stats: [], live });
    expect(noOff).toMatch(/mc-bsect-off"[^>]*><div class="mc-bhdr">Offline bench<\/div><div class="mc-bnone">no run yet/);
    expect(MC.card(BASE)).toMatch(/mc-bsect-live"[^>]*><div class="mc-bhdr">Live bench<\/div><div class="mc-bnone">no run yet/);
    expect(MC.card({ ...BASE, stats: [], live: null })).not.toContain('mc-bench');
  });
  it('escapes stat values and the row tooltip', () => {
    const html = MC.card({ ...BASE, live: { stats: [{ l: 'Gen', v: '<b>x</b>' }], age: '<i>' } });
    expect(html).not.toContain('<b>x</b>');
    expect(html).toContain('<b>&lt;b&gt;x&lt;/b&gt;</b>');
    expect(html).toContain('<b>&lt;i&gt;</b>');
    expect(MC.row({ ...BASE, live: { stats: [{ l: 'Gen', v: '1' }], title: 'host "q" <i>' } })).toContain('title="host &quot;q&quot; &lt;i&gt;"');
  });
  it('fills the live column in the row with its own tooltip', () => {
    const row = MC.row({ ...BASE, live });
    expect(row).toMatch(/mc-rowlive" title="[^"]*last run 3h ago[^"]*"><span class="mc-rstat"><span class="rl">Prompt<\/span><b>180<\/b><\/span><span class="mc-rstat"><span class="rl">Gen<\/span><b>42\.1<\/b>/);
    expect(MC.row(BASE)).toMatch(/mc-rowlive"><span class="mc-bnone">/);
  });
});
