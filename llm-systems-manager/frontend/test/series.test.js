import { describe, test, expect } from 'vitest';
import series from '../js/lib/series.js';

const { zipByTs, bucketDate, isManagerSubActive, latchFilled } = series;

describe('zipByTs', () => {
  test('aligns two series sharing a timestamp into one row', () => {
    const out = zipByTs([[{ ts: 't1', value: 1 }], [{ ts: 't1', value: 2 }]]);
    expect(out).toEqual([['t1', [1, 2]]]);
  });

  test('null-fills slots a series did not report at a timestamp', () => {
    const t1 = '2026-01-01T00:01:00Z';
    const t2 = '2026-01-01T00:02:00Z';
    const a = [{ ts: t1, value: 1 }, { ts: t2, value: 3 }];
    const b = [{ ts: t2, value: 4 }];
    expect(zipByTs([a, b])).toEqual([
      [t1, [1, null]],
      [t2, [3, 4]],
    ]);
  });

  test('sorts rows chronologically regardless of input order', () => {
    const a = [{ ts: '2026-01-01T00:02:00Z', value: 2 }, { ts: '2026-01-01T00:01:00Z', value: 1 }];
    expect(zipByTs([a]).map(([ts]) => ts)).toEqual([
      '2026-01-01T00:01:00Z',
      '2026-01-01T00:02:00Z',
    ]);
  });

  test('accepts either timestamp or ts as the time key', () => {
    const out = zipByTs([[{ timestamp: 't1', value: 9 }]]);
    expect(out).toEqual([['t1', [9]]]);
  });

  test('skips points with no timestamp', () => {
    const out = zipByTs([[{ value: 5 }, { ts: 't1', value: 1 }]]);
    expect(out).toEqual([['t1', [1]]]);
  });

  test('returns an empty list for empty input', () => {
    expect(zipByTs([[], []])).toEqual([]);
  });
});

describe('bucketDate', () => {
  test('snaps a timestamp down to the interval grid', () => {
    const base = new Date('2026-01-01T00:00:00Z').getTime();
    const ts = new Date(base + 6500).toISOString();
    expect(bucketDate(ts, 6000).getTime()).toBe(base + 6000);
  });

  test('keeps full resolution when interval is zero or negative', () => {
    const ts = '2026-01-01T00:00:06.500Z';
    expect(bucketDate(ts, 0).toISOString()).toBe('2026-01-01T00:00:06.500Z');
    expect(bucketDate(ts, -1).toISOString()).toBe('2026-01-01T00:00:06.500Z');
  });

  test('keeps full resolution when interval is not a number', () => {
    const ts = '2026-01-01T00:00:06.500Z';
    expect(bucketDate(ts, undefined).toISOString()).toBe('2026-01-01T00:00:06.500Z');
  });
});

describe('isManagerSubActive', () => {
  test('true only on the dashboard tab with the manager sub-tab selected', () => {
    expect(isManagerSubActive('dashboard', { dashboard: 'manager' })).toBe(true);
  });

  test('false on the dashboard tab with a different sub-tab', () => {
    expect(isManagerSubActive('dashboard', { dashboard: 'llama' })).toBe(false);
  });

  test('false on a non-dashboard tab even if sub-tab says manager', () => {
    expect(isManagerSubActive('overall', { dashboard: 'manager' })).toBe(false);
  });

  test('false when sub-tab state is missing', () => {
    expect(isManagerSubActive('dashboard', null)).toBe(false);
    expect(isManagerSubActive('dashboard', undefined)).toBe(false);
  });
});

describe('latchFilled', () => {
  test('latches true once a non-empty result arrives', () => {
    expect(latchFilled(false, [['t1', [1]]])).toBe(true);
  });

  test('stays false on an empty result so the caller can retry (#131)', () => {
    expect(latchFilled(false, [])).toBe(false);
  });

  test('treats a non-array result as not-filled', () => {
    expect(latchFilled(false, null)).toBe(false);
    expect(latchFilled(false, undefined)).toBe(false);
  });

  test('stays latched once already filled, even on a later empty result', () => {
    expect(latchFilled(true, [])).toBe(true);
  });
});
