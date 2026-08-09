import { describe, it, expect } from 'vitest';
import CSpark from '../js/lib/companion-spark.js';

describe('CSpark.path', () => {
  it('returns empty for fewer than two finite points', () => {
    expect(CSpark.path([], 100, 100)).toEqual({ line: '', fill: '', pts: [] });
    expect(CSpark.path([5], 100, 100)).toEqual({ line: '', fill: '', pts: [] });
    expect(CSpark.path([NaN, Infinity], 100, 100)).toEqual({ line: '', fill: '', pts: [] });
  });

  it('spans the padded box: max value at padTop, min at h-padBottom', () => {
    const { line } = CSpark.path([0, 10], 100, 100, { padTop: 10, padBottom: 20 });
    // first point (min=0) -> y = 10 + 1*(100-30) = 80; last (max=10) -> y = 10
    expect(line).toBe('M0,80 L100,10');
  });

  it('distributes x evenly across width', () => {
    const { pts } = CSpark.path([1, 1, 1], 100, 50);
    expect(pts.map((p) => p.x)).toEqual([0, 50, 100]);
  });

  it('exposes each point so a marker can sit exactly on the curve', () => {
    const { pts } = CSpark.path([0, 5, 10], 100, 100, { padTop: 10, padBottom: 10 });
    expect(pts).toHaveLength(3);
    expect(pts[0]).toMatchObject({ x: 0, y: 90, v: 0 });
    expect(pts[1]).toMatchObject({ x: 50, y: 50, v: 5 });
    expect(pts[2]).toMatchObject({ x: 100, y: 10, v: 10 });
  });

  it('smooths with cubic segments but never overshoots the data range', () => {
    // A spike must not make the curve dip below the series minimum — that is
    // why the tangents are monotone (Fritsch-Carlson), not plain Catmull-Rom.
    const { line, pts } = CSpark.path([5, 5, 60, 5, 5], 100, 100, { padTop: 0, padBottom: 0 });
    expect(line).toContain('C');
    const ys = [...line.matchAll(/[-\d.]+,([-\d.]+)/g)].map((m) => parseFloat(m[1]));
    const lo = Math.min(...pts.map((p) => p.y)), hi = Math.max(...pts.map((p) => p.y));
    ys.forEach((y) => {
      expect(y).toBeGreaterThanOrEqual(lo - 0.05);
      expect(y).toBeLessThanOrEqual(hi + 0.05);
    });
  });

  it('two points stay a straight segment', () => {
    expect(CSpark.path([1, 9], 100, 100).line).not.toContain('C');
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
