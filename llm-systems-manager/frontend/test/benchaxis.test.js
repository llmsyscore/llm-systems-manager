import { describe, it, expect } from 'vitest';
import benchaxis from '../js/lib/benchaxis.js';

const { computeBenchAxisOptions } = benchaxis;

const xv = r => r.xOptions.map(o => o.v);
const yv = r => r.yOptions.map(o => o.v);

describe('computeBenchAxisOptions', () => {
  it('drops constant numeric dimensions (cardinality 1)', () => {
    const rows = [
      { n_depth: 0, n_batch: 2048, avg_ts: 100 },
      { n_depth: 512, n_batch: 2048, avg_ts: 90 },
    ];
    const r = computeBenchAxisOptions(rows, []);
    expect(xv(r)).toContain('n_depth');     // varied → kept
    expect(xv(r)).not.toContain('n_batch'); // constant → dropped
  });

  it('defaults X to n_depth when varied, Y to avg_ts', () => {
    const rows = [{ n_depth: 0, avg_ts: 1 }, { n_depth: 512, avg_ts: 2 }];
    const r = computeBenchAxisOptions(rows, []);
    expect(r.defaultX).toBe('n_depth');
    expect(r.defaultY).toBe('avg_ts');
  });

  it('falls back X to first varied key then seq', () => {
    const r1 = computeBenchAxisOptions([{ n_ubatch: 256, avg_ts: 1 }, { n_ubatch: 512, avg_ts: 2 }], []);
    expect(r1.defaultX).toBe('n_ubatch');
    const r2 = computeBenchAxisOptions([{ avg_ts: 1 }, { avg_ts: 1 }], []);
    expect(r2.defaultX).toBe('seq');
  });

  it('never offers seq/time on Y and keeps seq as X fallback', () => {
    const r = computeBenchAxisOptions([{ n_depth: 0, avg_ts: 1 }, { n_depth: 8, avg_ts: 2 }], []);
    expect(yv(r)).not.toContain('seq');
    expect(yv(r)).not.toContain('time');
    expect(xv(r)).toContain('seq');
    expect(xv(r)).not.toContain('time');
  });

  it('always includes avg_ts and ms_tok on Y first', () => {
    const r = computeBenchAxisOptions([{ n_depth: 0, avg_ts: 1 }, { n_depth: 8, avg_ts: 2 }], []);
    expect(yv(r).slice(0, 2)).toEqual(['avg_ts', 'ms_tok']);
  });

  it('includes varying switch flags as eligible axes', () => {
    const r = computeBenchAxisOptions([{ avg_ts: 1 }, { avg_ts: 2 }], [{ flag: '--threads', value: '4' }]);
    expect(xv(r)).toContain('threads');
  });

  it('prefers n_depth as default X even when other dims also vary', () => {
    const rows = [
      { n_depth: 0, n_batch: 256, avg_ts: 1 },
      { n_depth: 512, n_batch: 512, avg_ts: 2 },
    ];
    expect(computeBenchAxisOptions(rows, []).defaultX).toBe('n_depth');
  });

  it('ignores non-string switch flags', () => {
    const r = computeBenchAxisOptions([{ avg_ts: 1 }, { avg_ts: 2 }], [{ flag: {}, value: '4' }]);
    expect(xv(r)).not.toContain('[object Object]');
    expect(xv(r)).toEqual(['seq']);
  });

  it('handles empty rows without throwing', () => {
    const r = computeBenchAxisOptions([], []);
    expect(r.defaultX).toBe('seq');
    expect(r.defaultY).toBe('avg_ts');
    expect(yv(r)).toEqual(['avg_ts', 'ms_tok']);
  });

  it('passes axis keys through the label function', () => {
    const r = computeBenchAxisOptions(
      [{ n_depth: 0, avg_ts: 1 }, { n_depth: 8, avg_ts: 2 }], [],
      k => (k === 'n_depth' ? 'Depth (n_depth)' : k));
    expect(r.xOptions.find(o => o.v === 'n_depth').t).toBe('Depth (n_depth)');
  });
});
