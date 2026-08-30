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
    expect(charts).toMatch(/fmtRateTile\(ll\.tokens_per_second, _llamaPeaks\.tps, 'Gen tokens\/s', undefined, llWin && llWin\.gen\)/);
    expect(charts).toMatch(/fmtRateTile\(ll\.prompt_tokens_per_second, _llamaPeaks\.pps, 'Prompt tokens\/s', undefined, llWin && llWin\.prompt\)/);
    expect(charts).toMatch(/const llWin = m\.throughput_window \|\| null;/);
    expect(charts).toMatch(/_setRateTile\('llamaTps', fmtRateTile\(/);
    expect(html).toMatch(/id="llamaTps-lbl"/);
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

describe('LMPeaks.rateStat (#736/#739)', () => {
  it('live while > 0, window average otherwise, peak only when > 0', () => {
    const tr = LMPeaks.makeTracker(LMPeaks.RATE_WINDOW_MS);
    tr.push(T0, 10); tr.push(T0 + MIN, 30); tr.push(T0 + 2 * MIN, 0);
    expect(LMPeaks.rateStat(tr, 42, T0 + 2 * MIN)).toEqual({ v: 42, mode: 'live', peak: { v: 30, t: T0 + MIN } });
    expect(LMPeaks.rateStat(tr, 0, T0 + 2 * MIN)).toEqual({ v: 20, mode: LMPeaks.AVG_LABEL, peak: { v: 30, t: T0 + MIN } });
    const empty = LMPeaks.makeTracker(LMPeaks.RATE_WINDOW_MS);
    empty.push(T0, 0);
    expect(LMPeaks.rateStat(empty, null, T0)).toEqual({ v: null, mode: LMPeaks.AVG_LABEL, peak: null });
  });
  it('the shared window is one hour and the label carries a non-breaking hyphen', () => {
    expect(LMPeaks.RATE_WINDOW_MS).toBe(60 * MIN);
    expect(LMPeaks.AVG_LABEL).toBe('60\u2011min avg');
  });
});

// #745: a server-computed window replaces the browser tracker's stats.
describe('LMPeaks.rateStat with a server window', () => {
  const win = { avg: 12.5, peak: { v: 40, ts: new Date(T0 - 5 * MIN).toISOString() } };

  it('serverWindow normalises {avg, peak:{v, ts}} to tracker shape', () => {
    expect(LMPeaks.serverWindow(win, T0)).toEqual({ avg: 12.5, peak: { v: 40, t: T0 - 5 * MIN } });
    expect(LMPeaks.serverWindow(null, T0)).toBeNull();
    expect(LMPeaks.serverWindow({ avg: null, peak: null }, T0)).toEqual({ avg: null, peak: null });
    expect(LMPeaks.serverWindow({ avg: 0, peak: { v: 0, ts: 'x' } }, T0)).toEqual({ avg: null, peak: null });
  });

  it('clamps a future peak ts and tolerates an unparsable one', () => {
    const fut = { avg: 1, peak: { v: 2, ts: new Date(T0 + MIN).toISOString() } };
    expect(LMPeaks.serverWindow(fut, T0).peak.t).toBe(T0);
    expect(LMPeaks.serverWindow({ avg: 1, peak: { v: 2, ts: 'nope' } }, T0).peak.t).toBe(T0);
  });

  it('idle: shows the server average and peak, ignoring the tracker', () => {
    const tr = LMPeaks.makeTracker(60 * MIN);
    tr.push(T0 - MIN, 99);
    expect(LMPeaks.rateStat(tr, 0, T0, win))
      .toEqual({ v: 12.5, mode: LMPeaks.AVG_LABEL, peak: { v: 40, t: T0 - 5 * MIN } });
  });

  it('live: live value wins and lifts the peak when it exceeds the server peak', () => {
    const tr = LMPeaks.makeTracker(60 * MIN);
    expect(LMPeaks.rateStat(tr, 30, T0, win)).toEqual({ v: 30, mode: 'live', peak: { v: 40, t: T0 - 5 * MIN } });
    expect(LMPeaks.rateStat(tr, 55, T0, win)).toEqual({ v: 55, mode: 'live', peak: { v: 55, t: T0 } });
  });

  it('falls back to the tracker when no window is supplied', () => {
    const tr = LMPeaks.makeTracker(60 * MIN);
    tr.push(T0 - MIN, 8);
    expect(LMPeaks.rateStat(tr, 0, T0)).toEqual({ v: 8, mode: LMPeaks.AVG_LABEL, peak: { v: 8, t: T0 - MIN } });
    expect(LMPeaks.rateStat(tr, 0, T0, undefined).v).toBe(8);
  });
});
