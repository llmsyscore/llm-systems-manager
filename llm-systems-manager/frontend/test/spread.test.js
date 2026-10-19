import { describe, it, expect } from 'vitest';
import SPREAD from '../js/lib/spread.js';
describe('SPREAD.rangeText', () => {
  it('uses the interquartile range at 8+ samples and min–max below', () => {
    expect(SPREAD.rangeText({ n: 10, min: 118.2, p25: 120.4, p50: 123, p75: 126.9, max: 131 })).toBe('120.4–126.9');
    expect(SPREAD.rangeText({ n: 3, min: 118.2, p25: 119, p50: 120, p75: 125, max: 131 })).toBe('118.2–131.0');
    expect(SPREAD.rangeText({ n: 2, min: 40, max: 42 }, 0)).toBe('40–42');
    expect(SPREAD.rangeText(null)).toBe('');
    expect(SPREAD.rangeText({ n: 1, min: 5, max: 5 })).toBe('');
  });
  it('labels with unit and count', () => {
    expect(SPREAD.rangeLabel({ n: 10, min: 118.2, p25: 120.4, p50: 123, p75: 126.9, max: 131 }, 'tok/s', 1, 'reps')).toBe('120.4–126.9 tok/s · 10 reps');
    expect(SPREAD.rangeLabel(null, 'tok/s', 1, 'reps')).toBe('');
  });
});
