import { describe, it, expect } from 'vitest';
import CSpark from '../js/lib/companion-spark.js';

describe('CSpark.path', () => {
  it('returns empty for fewer than two finite points', () => {
    expect(CSpark.path([], 100, 100)).toEqual({ line: '', fill: '' });
    expect(CSpark.path([5], 100, 100)).toEqual({ line: '', fill: '' });
    expect(CSpark.path([NaN, Infinity], 100, 100)).toEqual({ line: '', fill: '' });
  });

  it('spans the padded box: max value at padTop, min at h-padBottom', () => {
    const { line } = CSpark.path([0, 10], 100, 100, { padTop: 10, padBottom: 20 });
    // first point (min=0) -> y = 10 + 1*(100-30) = 80; last (max=10) -> y = 10
    expect(line).toBe('M0,80 L100,10');
  });

  it('distributes x evenly across width', () => {
    const { line } = CSpark.path([1, 1, 1], 100, 50);
    // three points at x = 0, 50, 100
    expect(line.startsWith('M0,')).toBe(true);
    expect(line).toContain('L50,');
    expect(line).toContain('L100,');
  });

  it('fill closes down to the baseline and back', () => {
    const { fill } = CSpark.path([0, 10], 40, 30, { padTop: 5, padBottom: 5 });
    expect(fill.endsWith('L40,30 L0,30 Z')).toBe(true);
  });

  it('flat series does not throw and stays within the box', () => {
    const { line } = CSpark.path([7, 7, 7, 7], 100, 100, { padTop: 10, padBottom: 10 });
    const ys = line.match(/,(-?\d+(\.\d+)?)/g).map((s) => parseFloat(s.slice(1)));
    ys.forEach((y) => { expect(y).toBeGreaterThanOrEqual(10); expect(y).toBeLessThanOrEqual(90); });
  });

  it('ignores non-finite entries mixed into the series', () => {
    const clean = CSpark.path([1, 2, 3], 100, 100);
    const dirty = CSpark.path([1, null, 2, NaN, 3], 100, 100);
    expect(dirty).toEqual(clean);
  });
});
