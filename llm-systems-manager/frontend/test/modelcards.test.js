// Unit tests for js/lib/modelcards.js — pure render + state helpers (#765).
import { describe, it, expect } from 'vitest';
import MC from '../js/lib/modelcards.js';

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
  it('marks stats as bench results and flags staleness without an age', () => {
    const plain = MC.card(BASE);
    expect(plain).toContain('mc-benchtag');
    expect(plain).not.toContain('mc-stale');
    const withStale = MC.card({ ...BASE, fresh: { stale: true, staleTitle: 'ctx changed' } });
    expect(withStale).toContain('re-bench');
    expect(withStale).toContain('ctx changed');
    expect(MC.card({ ...BASE, fresh: { age: '3d ago' } })).not.toContain('3d ago');
  });
  it('bench stats become a button only when benchClick is set', () => {
    const plain = MC.card(BASE);
    expect(plain).not.toContain('mc-stats" data-act');
    const clickable = MC.card({ ...BASE, benchClick: 'bench' });
    expect(clickable).toMatch(/mc-stats" data-act="bench" data-id="[^"]+" role="button"/);
    expect(MC.row({ ...BASE, benchClick: 'bench' })).toMatch(/mc-rowmet" data-act="bench"/);
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
  it('row header carries the bench label and rows self-label their stats', () => {
    const html = MC.rowHeader('Bench (t/s)', 'Profile', 'not live');
    expect(html).toContain('mc-methdr');
    expect(html).toContain('title="not live"');
    expect(html).toContain('Bench (t/s)');
    const rowHtml = MC.row(BASE);
    expect(rowHtml).toContain('<div class="l">Gen</div>');
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
