// Unit tests for js/lib/toolcards.js — Tools launcher + ledger renderers (#769).
import { describe, it, expect } from 'vitest';
import TC from '../js/lib/toolcards.js';

const TOOL = {
  id: 'reportcard', icon: '▤', tone: 1, name: 'Report Card',
  desc: 'Standardized score', status: 'ready',
  stats: [{ v: '42.7', u: 't/s', l: 'Last score' }, { v: 'RTX 4090', l: 'GPU' }],
  last: 'last run <b>2d ago</b>', sub: '42.7 t/s', action: 'Open', primary: true,
};

describe('view helpers', () => {
  it('validates views with card fallback', () => {
    expect(TC.validView('list')).toBe('list');
    expect(TC.validView('compact')).toBe('compact');
    expect(TC.validView('bogus')).toBe('card');
    expect(TC.viewOf({ toolsView: 'compact' })).toBe('compact');
    expect(TC.viewOf(null)).toBe('card');
    expect(TC.viewOf({})).toBe('card');
  });

  it('shows relative age up to 15 days, then the calendar date', () => {
    const now = Date.UTC(2026, 7, 31, 12, 0, 0);
    expect(TC.when(now / 1000 - 4 * 86400, now)).toBe('4d ago');
    expect(TC.when(now / 1000 - 14 * 86400, now)).toBe('14d ago');
    expect(TC.when('2026-08-10T12:00:00Z', now)).toBe('Aug 10');
    expect(TC.when('2025-12-01T12:00:00Z', now)).toBe('Dec 1, 2025');
    expect(TC.when('garbage', now)).toBe(null);
  });

  it('ages epoch seconds, epoch millis, and ISO strings', () => {
    const now = Date.UTC(2026, 7, 31, 12, 0, 0);
    expect(TC.age(now / 1000 - 120, now)).toBe('2m ago');
    expect(TC.age(now - 7200e3, now)).toBe('2h ago');
    expect(TC.age('2026-08-29T12:00:00Z', now)).toBe('2d ago');
    expect(TC.age('garbage', now)).toBe(null);
    expect(TC.age(null, now)).toBe(null);
  });
});

describe('card view', () => {
  it('renders name, stats, pill, and action', () => {
    const html = TC.card(TOOL);
    expect(html).toContain('data-tool="reportcard"');
    expect(html).toContain('Report Card');
    expect(html).toContain('42.7');
    expect(html).toContain('mc-pill p-idle');
    expect(html).toContain('mcbtn-pri');
  });

  it('escapes hostile names and stat values', () => {
    const t = { ...TOOL, name: '<img src=x onerror=1>', stats: [{ v: '"><script>', l: 'X' }] };
    const html = TC.card(t);
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;img');
  });

  it('renders a planned tile as an inert div without action', () => {
    const html = TC.card({ id: 'more', icon: '⇄', name: 'More tools', desc: 'd', status: 'soon' });
    expect(html).toContain('tool-card soon');
    expect(html).not.toContain('data-tool');
    expect(html).not.toContain('mcbtn');
  });

  it('falls back to the empty hint when stats are missing', () => {
    const html = TC.card({ ...TOOL, stats: null, empty: '<b>Never run.</b> hint' });
    expect(html).toContain('tc-empty');
    expect(html).toContain('Never run.');
  });
});

describe('list view', () => {
  it('renders a keyboard-reachable row with stats and status dot', () => {
    const html = TC.row({ ...TOOL, status: 'running' });
    expect(html).toContain('role="button"');
    expect(html).toContain('tabindex="0"');
    expect(html).toContain('d-busy');
    expect(html).toContain('Last score');
  });

  it('shows never run for a tool without stats', () => {
    expect(TC.row({ ...TOOL, stats: null })).toContain('never run');
  });
});

describe('compact view', () => {
  it('renders a chip with running spinner state', () => {
    const html = TC.chip({ ...TOOL, status: 'running', sub: 'running · 3/7' });
    expect(html).toContain('deck-chip');
    expect(html).toContain('st run');
    expect(html).toContain('running · 3/7');
  });
});

describe('launcher', () => {
  const tools = [TOOL, { id: 'more', icon: '⇄', name: 'More tools', desc: 'd', status: 'soon' }];
  it('switches wrapper by view', () => {
    expect(TC.launcher(tools, 'card')).toContain('tool-grid');
    expect(TC.launcher(tools, 'list')).toContain('tool-listwrap');
    expect(TC.launcher(tools, 'compact')).toContain('class="deck"');
    expect(TC.launcher(tools, 'nope')).toContain('tool-grid');
  });
});

describe('ledger', () => {
  const RUN = { icon: '▤', tool: 'Report Card', toolId: 'reportcard',
    model: 'qwen3-30b', host: 'loki', result: '<b>42.7 t/s</b>', ts: '2026-08-29T12:00:00Z' };

  it('renders clickable rows with tool + model data attributes', () => {
    const html = TC.ledgerRow(RUN);
    expect(html).toContain('data-tool="reportcard"');
    expect(html).toContain('data-model="qwen3-30b"');
    expect(html).toContain('rowlink');
    expect(html).toContain('title="Open Report Card"');
  });

  it('renders a row without toolId as inert', () => {
    const html = TC.ledgerRow({ ...RUN, toolId: null });
    expect(html).not.toContain('rowlink');
    expect(html).not.toContain('data-tool');
    expect(html).not.toContain('title=');
    expect(html).toContain('qwen3-30b');
  });

  it('escapes model and host but passes pre-built result markup', () => {
    const html = TC.ledgerRow({ ...RUN, model: '<x>&m', host: '"h"' });
    expect(html).toContain('&lt;x&gt;&amp;m');
    expect(html).toContain('&quot;h&quot;');
    expect(html).toContain('<b>42.7 t/s</b>');
  });

  it('shows live label instead of age for a running row', () => {
    const html = TC.ledgerRow({ ...RUN, live: true, when: '04:12 elapsed' });
    expect(html).toContain('04:12 elapsed');
    expect(html).toContain('class="live"');
  });

  it('renders an empty state when there are no rows', () => {
    expect(TC.ledger([])).toContain('No results yet');
    expect(TC.ledger(null)).toContain('No results yet');
    expect(TC.ledger([RUN])).toContain('<table class="tools-ledger">');
  });

  it('marks the sorted column header with direction', () => {
    const html = TC.ledger([RUN], { key: 'ts', dir: 'desc' });
    expect(html).toContain('data-sort="ts"');
    expect(html).toContain('Last ▾');
    expect(html).toContain('sortable on');
    const asc = TC.ledger([RUN], { key: 'model', dir: 'asc' });
    expect(asc).toContain('Model ▴');
  });
});
