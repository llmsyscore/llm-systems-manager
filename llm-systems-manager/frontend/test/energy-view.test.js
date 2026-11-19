// #1432 phase 4d: Energy page transforms added to js/lib/energy.js (tiles, step series, kit tables).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import EN from '../js/lib/energy.js';
import OV from '../js/lib/overall-view.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const SUMMARY = JSON.parse(readFileSync(resolve(HERE, 'fixtures/energy_summary_days_1.json'), 'utf8'));

// n hourly rows from local midnight, tokens_gen = i + 1.
function hourly(n) {
  const start = new Date(2026, 10, 2, 0, 0, 0).getTime() / 1000;
  return Array.from({ length: n }, (_, i) => ({
    hour_ts: start + i * 3600, energy_wh: 10 + (i % 5), active_energy_wh: i % 3, tokens_gen: i + 1,
  }));
}

describe('pageTiles', () => {
  it('gives seven tiles in order, saved-vs-cloud first', () => {
    const t = EN.pageTiles(SUMMARY);
    expect(t.map(x => x.label)).toEqual(['Saved vs cloud', 'Energy', 'Electricity cost', 'Tokens generated',
      'Cost per million, all-in', 'Cost per million, marginal', 'Average draw']);
    expect(t[0]).toMatchObject({ value: '$3.34', tone: 'good', sub: 'vs $3.38 at Opus 5' });
    const tt = EN.totalTiles(SUMMARY.totals);
    for (let i = 1; i < 7; i++) expect([t[i].value, t[i].sub]).toEqual([tt[i - 1].value, tt[i - 1].sub]);
    expect(t[1].sub).toContain('active');
  });
  it('a local over-run is a warn tile with the amount', () => {
    const t = EN.pageTiles(Object.assign({}, SUMMARY, { savings_usd: -1.4 }));
    expect(t[0]).toMatchObject({ value: '$1.40', tone: 'warn', sub: 'vs $3.38 at Opus 5' });
  });
  it('the saved tile sub names the cloud label alone when the cloud cost is missing', () => {
    const t = EN.pageTiles(Object.assign({}, SUMMARY, { config: {}, totals: Object.assign({}, SUMMARY.totals, { cloud_cost_usd: null }) }));
    expect(t[0].sub).toBe('vs cloud API');
  });
  it('without savings the first tile is a dash with the headline as sub', () => {
    const t = EN.pageTiles(Object.assign({}, SUMMARY, { savings_usd: null }));
    expect(t[0]).toMatchObject({ value: '—', tone: 'off', sub: 'Savings unavailable' });
    const none = EN.pageTiles({ totals: {}, savings_usd: null });
    expect(none[0]).toMatchObject({ value: '—', tone: 'off', sub: 'No data in this window' });
    expect(none[1].sub).toBe('no power telemetry');
  });
});

describe('savingsLine', () => {
  it('is the savings sentence', () => {
    expect(EN.savingsLine(SUMMARY)).toBe(EN.savingsView(SUMMARY).sub);
    expect(EN.savingsLine(SUMMARY)).toContain('Opus 5');
  });
});

describe('stepHours', () => {
  it('picks the smallest step that keeps 60 bars or fewer', () => {
    expect(EN.stepHours(24)).toBe(1);
    expect(EN.stepHours(60)).toBe(1);
    expect(EN.stepHours(61)).toBe(3);
    expect(EN.stepHours(168)).toBe(3);
    expect(EN.stepHours(720)).toBe(24);
    expect(EN.stepHours(180)).toBe(6);
    expect(EN.stepHours(1080)).toBe(24);
    expect(EN.stepHours(5000)).toBe(24);
    expect(EN.stepHours(0)).toBe(1);
    expect(EN.stepHours(undefined)).toBe(1);
  });
});

describe('stepSeries', () => {
  it('168 hourly rows at step 3 give 56 steps whose tokens sum to the input', () => {
    const rows = hourly(168);
    const s = EN.stepSeries(rows, 3);
    expect(s.labels).toHaveLength(56);
    expect(s.idle).toHaveLength(56);
    expect(s.gen).toHaveLength(56);
    expect(s.tokens).toHaveLength(56);
    const sum = a => a.reduce((x, y) => x + y, 0);
    expect(sum(s.tokens)).toBe(sum(rows.map(r => r.tokens_gen)));
    expect(s.tokens.slice(0, 2)).toEqual([1 + 2 + 3, 4 + 5 + 6]);
  });
  it('720 hourly rows at step 24 give 30 steps, 1080 give 45', () => {
    const rows = hourly(720), s = EN.stepSeries(rows, EN.stepHours(720));
    expect(s.labels).toHaveLength(30);
    expect(s.tokens.reduce((x, y) => x + y, 0)).toBe(rows.reduce((x, r) => x + r.tokens_gen, 0));
    expect(EN.stepSeries(hourly(1080), EN.stepHours(1080)).labels).toHaveLength(45);
  });
  it('windows of 7.5 to 15 days starting mid-day keep unique dated labels, 60 bars or fewer and every token', () => {
    for (const n of [170, 180, 300, 360]) {
      const rows = hourly(n + 7).slice(7), s = EN.stepSeries(rows, EN.stepHours(n));
      expect(new Set(s.labels).size, String(n)).toBe(s.labels.length);
      expect(s.labels.length, String(n)).toBeLessThanOrEqual(60);
      expect(s.labels[0], String(n)).toMatch(/^[A-Z][a-z]{2} \d{1,2} \d{2}:00$/);
      expect(s.tokens.reduce((x, y) => x + y, 0), String(n)).toBe(rows.reduce((x, r) => x + r.tokens_gen, 0));
    }
  });
  it('wraps OV.energyPerHour for labels, idle and generating Wh', () => {
    const rows = hourly(168);
    const e = OV.energyPerHour(rows, 3), s = EN.stepSeries(rows, 3);
    expect(s.labels).toEqual(e.labels);
    expect(s.idle).toEqual(e.series[0].values);
    expect(s.gen).toEqual(e.series[1].values);
  });
  it('builds the step table', () => {
    const s = EN.stepSeries(hourly(6), 3);
    expect(s.table.cols).toEqual(['From', 'Idle Wh', 'Generating Wh', 'Tokens']);
    expect(s.table.rows).toHaveLength(2);
    expect(s.table.rows[0]).toEqual([s.labels[0], s.idle[0], s.gen[0], 6]);
  });
  it('takes the hourly payload or nothing', () => {
    expect(EN.stepSeries({ rows: hourly(3) }, 1).tokens).toEqual([1, 2, 3]);
    const empty = EN.stepSeries(null, 1);
    expect(empty).toMatchObject({ labels: [], idle: [], gen: [], tokens: [] });
    expect(empty.table.rows).toEqual([]);
  });
});

describe('hostTable', () => {
  it('has ten columns, sortable number cells and notes for hosts without power', () => {
    const t = EN.hostTable(SUMMARY.hosts);
    expect(t.cols).toEqual(['Host', 'Source', 'Energy', 'Generating share', 'Active', 'Tokens', 'Cost',
      'Cost per million', 'Coverage', 'Notes']);
    expect(t.rows).toHaveLength(SUMMARY.hosts.length);
    const llama = t.rows[0];
    expect(llama[0]).toBe('host-a');
    expect(llama[1]).toBe('GPU');
    expect(llama[2]).toEqual({ text: '253 Wh', v: 0.253 });
    expect(llama[3]).toEqual({ text: '8%', v: 8 });
    expect(llama[5]).toEqual({ text: '10.2k', v: 10237 });
    expect(llama[7]).toEqual({ text: EN.fmtMtokRate(3.7009), v: 3.7009 });
    const off = t.rows[2];
    expect(off[3]).toEqual({ text: '—', v: null });
    expect(off[1]).toBe('—');
    expect(off[9]).toBe('no power telemetry · no token telemetry');
    expect(t.rows.every(r => r.length === 10)).toBe(true);
  });
  it('no hosts give no rows', () => {
    expect(EN.hostTable(null).rows).toEqual([]);
  });
});

describe('modelTable', () => {
  it('has eight columns and names the no-model row', () => {
    const models = SUMMARY.models.concat([{ model: '(no model)', hosts: ['a', 'b'], kwh: 0.01, energy_share_pct: 3.4 }]);
    const t = EN.modelTable(models);
    expect(t.cols).toEqual(['Model', 'Hosts', 'Energy', 'Share', 'Resident', 'Tokens', 'Cost', 'Cost per million']);
    const last = t.rows[t.rows.length - 1];
    expect(last[0]).toBe('No model loaded');
    expect(last[1]).toBe('a, b');
    expect(last[3]).toEqual({ text: '3%', v: 3 });
    expect(t.rows[0][2]).toEqual({ text: '253 Wh', v: 0.253 });
    expect(t.rows.every(r => r.length === 8)).toBe(true);
  });
});
