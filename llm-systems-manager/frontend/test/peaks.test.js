// #590: rolling-window peak tracker behind the Llama server card's
// live+peak Gen/Prompt tokens/s display.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import LMPeaks from '../js/lib/peaks.js';

const T0 = Date.UTC(2026, 7, 21, 12, 0, 0);
const MIN = 60000;

describe('LMPeaks.makeTracker', () => {
  it('empty tracker has no peak', () => {
    const tr = LMPeaks.makeTracker(15 * MIN);
    expect(tr.peak(T0)).toBeNull();
  });

  it('returns the window max with its timestamp', () => {
    const tr = LMPeaks.makeTracker(15 * MIN);
    tr.push(T0, 10);
    tr.push(T0 + MIN, 45.6);
    tr.push(T0 + 2 * MIN, 3);
    expect(tr.peak(T0 + 2 * MIN)).toEqual({ v: 45.6, t: T0 + MIN });
  });

  it('accepts ISO strings and Dates', () => {
    const tr = LMPeaks.makeTracker(15 * MIN);
    tr.push(new Date(T0).toISOString(), 7);
    tr.push(new Date(T0 + MIN), 9);
    expect(tr.peak(new Date(T0 + MIN))).toEqual({ v: 9, t: T0 + MIN });
  });

  it('expires samples older than the window, even with no new pushes', () => {
    const tr = LMPeaks.makeTracker(15 * MIN);
    tr.push(T0, 99);
    tr.push(T0 + MIN, 5);
    expect(tr.peak(T0 + 14 * MIN).v).toBe(99);
    expect(tr.peak(T0 + 16 * MIN).v).toBe(5);
    expect(tr.peak(T0 + 40 * MIN)).toBeNull();
  });

  it('equal peaks report the most recent occurrence', () => {
    const tr = LMPeaks.makeTracker(15 * MIN);
    tr.push(T0, 12);
    tr.push(T0 + MIN, 12);
    expect(tr.peak(T0 + MIN).t).toBe(T0 + MIN);
  });

  it('ignores junk timestamps and values', () => {
    const tr = LMPeaks.makeTracker(15 * MIN);
    tr.push('bogus', 5);
    tr.push(T0, NaN);
    tr.push(T0, null);
    expect(tr.peak(T0)).toBeNull();
  });

  it('tolerates slightly out-of-order pushes and drops stale ones', () => {
    const tr = LMPeaks.makeTracker(15 * MIN);
    tr.push(T0 + 2 * MIN, 5);
    tr.push(T0 + MIN, 80);            // late but inside the window — kept
    tr.push(T0 - 20 * MIN, 999);      // outside the window — dropped
    expect(tr.peak(T0 + 2 * MIN).v).toBe(80);
  });

  it('reset clears the window', () => {
    const tr = LMPeaks.makeTracker(15 * MIN);
    tr.push(T0, 50);
    tr.reset();
    expect(tr.peak(T0)).toBeNull();
  });
});

describe('llama card wiring (#590)', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const charts = readFileSync(join(here, '..', 'js', 'charts.js'), 'utf8');
  const html = readFileSync(join(here, '..', 'index.html'), 'utf8');

  it('live poll pushes browser-clock samples and renders live+peak', () => {
    expect(charts).toMatch(/_llamaPeaks\.tps\.push\(Date\.now\(\), ll\.tokens_per_second\)/);
    expect(charts).toMatch(/fmtLivePeak\(ll\.tokens_per_second, _llamaPeaks\.tps\)/);
    expect(charts).toMatch(/fmtLivePeak\(ll\.prompt_tokens_per_second, _llamaPeaks\.pps\)/);
  });

  it('backfill seeds clock-normalized rows; agent switch resets the trackers', () => {
    expect(charts).toMatch(/_llamaPeaks\.tps\.push\(_peakSeedTs\(r\.ts\), r\.llama_tps\)/);
    expect(charts).toMatch(/_peakSeedTs = LMPeaks\.rowClock\(rows, Date\.now\(\)\)/);
    expect(charts).toMatch(/_llamaPeaks\.tps\.reset\(\);/);
  });

  it('index.html loads the peaks lib before charts.js', () => {
    const peaksAt = html.indexOf('js/lib/peaks.js');
    const chartsAt = html.indexOf('js/charts.js?v');
    expect(peaksAt).toBeGreaterThan(-1);
    expect(peaksAt).toBeLessThan(chartsAt);
  });
});

describe('LMPeaks.agoText / rowClock (#591)', () => {
  it('formats the s/m/h ladder and clamps negatives', () => {
    expect(LMPeaks.agoText(5000)).toBe('5s ago');
    expect(LMPeaks.agoText(180000)).toBe('3m ago');
    expect(LMPeaks.agoText(7200000)).toBe('2h ago');
    expect(LMPeaks.agoText(-4000)).toBe('0s ago');
  });

  it('rowClock shifts rows by small offsets onto the caller clock', () => {
    const now = T0 + 30000;
    const rows = [{ ts: new Date(T0 - MIN).toISOString() }, { ts: new Date(T0).toISOString() }];
    const clk = LMPeaks.rowClock(rows, now);
    expect(clk(rows[1].ts)).toBe(now);
    expect(clk(rows[0].ts)).toBe(now - MIN);
  });

  it('rowClock refuses to freshen a stale feed', () => {
    const now = T0 + 30 * MIN;
    const rows = [{ ts: new Date(T0).toISOString() }];
    const clk = LMPeaks.rowClock(rows, now);
    expect(clk(rows[0].ts)).toBe(T0);
  });

  it('rowClock pulls future-stamped rows fully back', () => {
    const now = T0;
    const rows = [{ ts: new Date(T0 + 30 * MIN).toISOString() }];
    const clk = LMPeaks.rowClock(rows, now);
    expect(clk(rows[0].ts)).toBe(now);
  });

  it('rowClock tolerates empty input', () => {
    const clk = LMPeaks.rowClock([], T0);
    expect(clk(new Date(T0).toISOString())).toBe(T0);
  });
});

describe('LMPeaks.makeTracker.avg (#736)', () => {
  it('is null with no active samples', () => {
    const tr = LMPeaks.makeTracker(60 * MIN);
    expect(tr.avg(T0)).toBeNull();
    tr.push(T0, 0);
    tr.push(T0 + MIN, 0);
    expect(tr.avg(T0 + MIN)).toBeNull();
  });
  it('averages only the non-zero samples in the window', () => {
    const tr = LMPeaks.makeTracker(60 * MIN);
    tr.push(T0, 0);
    tr.push(T0 + MIN, 10);
    tr.push(T0 + 2 * MIN, 0);
    tr.push(T0 + 3 * MIN, 30);
    expect(tr.avg(T0 + 3 * MIN)).toEqual({ v: 20 });
  });
  it('drops samples that age out of the window', () => {
    const tr = LMPeaks.makeTracker(60 * MIN);
    tr.push(T0, 100);
    tr.push(T0 + 30 * MIN, 10);
    expect(tr.avg(T0 + 59 * MIN)).toEqual({ v: 55 });
    expect(tr.avg(T0 + 61 * MIN)).toEqual({ v: 10 });
    expect(tr.avg(T0 + 91 * MIN)).toBeNull();
  });
});
