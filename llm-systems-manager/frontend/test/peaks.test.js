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
    expect(charts).toMatch(/_peakSkewMs = Number\.isFinite/);
    expect(charts).toMatch(/_llamaPeaks\.tps\.reset\(\);/);
  });

  it('index.html loads the peaks lib before charts.js', () => {
    const peaksAt = html.indexOf('js/lib/peaks.js');
    const chartsAt = html.indexOf('js/charts.js?v');
    expect(peaksAt).toBeGreaterThan(-1);
    expect(peaksAt).toBeLessThan(chartsAt);
  });
});
