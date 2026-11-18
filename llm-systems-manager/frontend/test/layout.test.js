import { describe, test, expect } from 'vitest';
import layoutLib from '../js/lib/layout.js';

const { resolveHiddenList } = layoutLib;

describe('resolveHiddenList', () => {
  test('returns the saved list under its key', () => {
    const layout = { managerHidden: ['mgr-ram'], hiddenOverall: ['ov-fleet'] };
    expect(resolveHiddenList(layout, 'managerHidden')).toBe(layout.managerHidden);
    expect(resolveHiddenList(layout, 'hiddenOverall')).toBe(layout.hiddenOverall);
  });

  test('seeds a missing list to an empty array', () => {
    const layout = {};
    const list = resolveHiddenList(layout, 'managerHidden');
    expect(list).toEqual([]);
    expect(layout.managerHidden).toBe(list);
  });

  test('keeps no per-host lists: an old grid key reads its own list and adds no hiddenByAgent', () => {
    const layout = { hidden: ['aio'] };
    expect(resolveHiddenList(layout, 'hidden', 'llama1')).toBe(layout.hidden);
    expect(layout.hiddenByAgent).toBeUndefined();
    expect(layoutLib.PER_AGENT_HIDDEN).toBeUndefined();
  });

  test('returns a stable reference across calls so in-place mutation persists', () => {
    const layout = {};
    const a = resolveHiddenList(layout, 'llamaPanelsHidden');
    a.push('llama-ups');
    expect(resolveHiddenList(layout, 'llamaPanelsHidden')).toBe(a);
    expect(layout.llamaPanelsHidden).toEqual(['llama-ups']);
  });

  test('tolerates a malformed layout without throwing', () => {
    expect(resolveHiddenList(null, 'hidden')).toEqual([]);
    const layout = { hidden: 'not-an-array' };
    expect(resolveHiddenList(layout, 'hidden')).toEqual([]);
  });
});
