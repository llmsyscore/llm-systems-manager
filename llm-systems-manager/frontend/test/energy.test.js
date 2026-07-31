// #470: pure display helpers for the Energy sub-tab.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import EN from '../js/lib/energy.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

const TOTALS = {
  observed_s: 3600, active_s: 1800, active_pct: 50.0, coverage_pct: 100.0,
  power_coverage_pct: 100.0, kwh: 4.2, active_kwh: 3.0, idle_kwh: 1.2,
  avg_watts: 208.0, cost_usd: 0.63, idle_cost_usd: 0.18,
  tokens_gen: 2_400_000, tokens_prompt: 900_000,
  usd_per_mtok: 0.26, usd_per_mtok_active: 0.19,
  cloud_cost_usd: 1.58, power_source: 'psu',
  has_power: true, has_tokens: true,
};

const SUMMARY = {
  totals: TOTALS, savings_usd: 0.95, since_ts: 1785110400,
  config: { cloud_price_label: 'budget tier' },
  window: { label: '2026-07' },
};

describe('formatters', () => {
  it('fmtUsd handles null, negatives, digits', () => {
    expect(EN.fmtUsd(null)).toBe('—');
    expect(EN.fmtUsd(1.234)).toBe('$1.23');
    expect(EN.fmtUsd(-0.5)).toBe('-$0.50');
  });
  it('fmtKwh scales Wh → kWh', () => {
    expect(EN.fmtKwh(0.42)).toBe('420 Wh');
    expect(EN.fmtKwh(4.24)).toBe('4.2 kWh');
    expect(EN.fmtKwh(123.4)).toBe('123 kWh');
    expect(EN.fmtKwh(null)).toBe('—');
  });
  it('fmtTokens compacts magnitudes', () => {
    expect(EN.fmtTokens(950)).toBe('950');
    expect(EN.fmtTokens(2400)).toBe('2.4k');
    expect(EN.fmtTokens(2_400_000)).toBe('2.40M');
    expect(EN.fmtTokens(1.2e9)).toBe('1.20B');
  });
  it('fmtMtokRate and fmtWatts degrade to em-dash', () => {
    expect(EN.fmtMtokRate(null)).toBe('—');
    expect(EN.fmtMtokRate(0.26)).toBe('$0.26/Mtok');
    expect(EN.fmtWatts(208.0)).toBe('208 W');
    expect(EN.fmtWatts(null)).toBe('—');
  });
});

describe('savingsView', () => {
  it('positive savings headline with honest subtitle', () => {
    const v = EN.savingsView(SUMMARY);
    expect(v.headline).toBe('You saved ~$0.95');
    expect(v.cls).toBe('good');
    expect(v.sub).toContain('2.40M tokens generated');
    expect(v.sub).toContain('$0.63 of electricity');
    expect(v.sub).toContain('budget tier');
  });
  it('negative savings flips the framing', () => {
    const v = EN.savingsView({ ...SUMMARY, savings_usd: -1.4 });
    expect(v.headline).toBe('Local ran over by ~$1.40');
    expect(v.cls).toBe('warn');
  });
  it('missing token telemetry refuses the comparison', () => {
    const v = EN.savingsView({
      ...SUMMARY, savings_usd: null,
      totals: { ...TOTALS, has_tokens: false },
    });
    expect(v.headline).toBe('Savings unavailable');
    expect(v.sub).toContain('token telemetry');
  });
  it('missing power telemetry refuses the comparison', () => {
    const v = EN.savingsView({
      ...SUMMARY, savings_usd: null,
      totals: { ...TOTALS, has_power: false },
    });
    expect(v.sub).toContain('power telemetry');
  });
  it('empty window shows onboarding text', () => {
    const v = EN.savingsView({
      totals: { has_power: false, has_tokens: false }, savings_usd: null,
    });
    expect(v.headline).toBe('No data in this window');
    expect(v.cls).toBe('muted');
  });
});

describe('totalTiles', () => {
  it('renders six tiles with split subtitle', () => {
    const tiles = EN.totalTiles(TOTALS);
    expect(tiles).toHaveLength(6);
    expect(tiles[0].value).toBe('4.2 kWh');
    expect(tiles[0].sub).toContain('active');
    expect(tiles[3].value).toBe('$0.26/Mtok');
    expect(tiles[4].value).toBe('$0.19/Mtok');
  });
  it('no-power totals degrade tiles without crashing', () => {
    const tiles = EN.totalTiles({ ...TOTALS, has_power: false, kwh: null,
                                  cost_usd: null, idle_cost_usd: null,
                                  avg_watts: null, usd_per_mtok: null,
                                  usd_per_mtok_active: null });
    expect(tiles[0].value).toBe('—');
    expect(tiles[0].sub).toBe('no power telemetry');
  });
});

describe('hostRows', () => {
  it('maps telemetry gaps to notes', () => {
    const rows = EN.hostRows([
      { agent_id: 'a'.repeat(32), hostname: 'box', has_power: true,
        has_tokens: true, kwh: 2.0, active_kwh: 1.0, power_source: 'psu',
        active_pct: 40, tokens_gen: 5e6, cost_usd: 0.3, usd_per_mtok: 0.06,
        coverage_pct: 98.7 },
      { agent_id: 'b'.repeat(32), hostname: 'mac', has_power: false,
        has_tokens: false, kwh: null, power_source: null, active_pct: 10,
        tokens_gen: 0, cost_usd: null, usd_per_mtok: null, coverage_pct: 50 },
    ]);
    expect(rows[0].source).toBe('wall');
    expect(rows[0].split).toBe(50);
    expect(rows[0].notes).toBe('');
    expect(rows[1].notes).toBe('no power telemetry · no token telemetry');
    expect(rows[1].split).toBeNull();
    expect(rows[1].kwh).toBe('—');
  });
  it('falls back to agent id prefix without hostname', () => {
    const rows = EN.hostRows([{ agent_id: 'c'.repeat(32) }]);
    expect(rows[0].hostname).toBe('cccccccc');
  });
});

describe('hourlySeries', () => {
  it('splits idle from total and carries tokens', () => {
    const s = EN.hourlySeries([
      { hour_ts: 3600, energy_wh: 100, active_energy_wh: 60, tokens_gen: 5 },
      { hour_ts: 7200, energy_wh: 40, active_energy_wh: 50, tokens_gen: 0 },
    ]);
    expect(s.labels).toHaveLength(2);
    expect(s.activeWh).toEqual([60, 50]);
    expect(s.idleWh).toEqual([40, 0]);
    expect(s.tokens).toEqual([5, 0]);
  });
});

describe('coverageNote', () => {
  it('mentions tracking start and partial coverage', () => {
    const note = EN.coverageNote({
      ...SUMMARY,
      totals: { ...TOTALS, coverage_pct: 42.0, power_coverage_pct: 80.0 },
    });
    expect(note).toContain('tracking since 2026-07-27');
    expect(note).toContain('observed 42%');
    expect(note).toContain('power known 80%');
  });
  it('quiet when coverage is complete', () => {
    expect(EN.coverageNote(SUMMARY)).toBe('tracking since 2026-07-27');
    expect(EN.coverageNote({})).toBe('');
  });
});

// Mirrors reportcard-wiring: a sub-tab id missing from the base.css
// id-scoped .btn restyle keeps the raw gradient and differs from siblings.
describe('base.css wiring', () => {
  const css = readFileSync(resolve(ROOT, 'css/base.css'), 'utf8');

  it('restyles #llm-energy buttons like the sibling sub-tabs', () => {
    const block = css.split('}').find(b => b.includes('#llm-llamacpp .btn'));
    expect(block).toContain('#llm-energy .btn');
  });

  it('hover selectors always carry :hover', () => {
    css.split('}').forEach(block => {
      block.split(',').forEach(sel => {
        if (!sel.includes('#llm-energy .btn')) return;
        const siblings = block.split(',').filter(s => s.includes('#llm-llamacpp'));
        if (!siblings.length) return;
        const sibHover = siblings.some(s => s.includes(':hover'));
        expect(sel.includes(':hover')).toBe(sibHover);
      });
    });
  });

  it('index.html loads both energy scripts and the stylesheet', () => {
    const html = readFileSync(resolve(ROOT, 'index.html'), 'utf8');
    expect(html).toContain('js/lib/energy.js');
    expect(html).toContain('js/energy.js');
    expect(html).toContain('css/energy.css');
    expect(html).toContain("switchSubTab('llm','energy')");
  });
});

describe('window options', () => {
  it('offers current + previous month and day windows', () => {
    const opts = EN.windowOptions(Date.UTC(2026, 6, 31));
    expect(opts[0].value).toBe('month:2026-07');
    expect(opts[1].value).toBe('month:2026-06');
    expect(opts.map(o => o.value)).toContain('days:30');
  });
  it('handles the January rollover', () => {
    const opts = EN.windowOptions(Date.UTC(2026, 0, 5));
    expect(opts[1].value).toBe('month:2025-12');
  });
  it('windowQuery round-trips values', () => {
    expect(EN.windowQuery('month:2026-07')).toEqual({ month: '2026-07' });
    expect(EN.windowQuery('days:30')).toEqual({ days: '30' });
    expect(EN.windowQuery('junk')).toEqual({});
  });
});
