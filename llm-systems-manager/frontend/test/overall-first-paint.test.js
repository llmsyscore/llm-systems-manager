// #1140: history backfills redraw each chart once per batch instead of once
// per point; #1437: the pinned shells are sized when Overall opens.
import { describe, test, expect, vi } from 'vitest';
import { srcFile, fnSrc, blockSrc, evalGlobal } from './helpers/harness.js';

const charts = srcFile('js/charts.js');

// Helpers + pushPoint/pushDual/pushMulti share the `_pushBatch` state, so they
// are evaluated once as a block rather than per test.
evalGlobal([
  'window.MAX_POINTS = 3600;',
  'window._bucketDate = (ts) => new Date(ts);',
  blockSrc(charts, 'let _pushBatch = null;', 'function pushPoint', { includeEnd: false }),
  fnSrc(charts, 'pushPoint'), fnSrc(charts, 'pushDual'), fnSrc(charts, 'pushMulti'),
  'window._withPushBatch = _withPushBatch; window.pushPoint = pushPoint;',
  'window.pushDual = pushDual; window.pushMulti = pushMulti;',
].join('\n'));

function mkChart(n = 1) {
  return { data: { labels: [], datasets: Array.from({ length: n }, () => ({ data: [] })) }, update: vi.fn() };
}

describe('_withPushBatch (#1140)', () => {
  test('pushes outside a batch redraw immediately', () => {
    const ch = mkChart();
    window.pushPoint(ch, 1000, 1);
    window.pushPoint(ch, 2000, 2);
    expect(ch.update).toHaveBeenCalledTimes(2);
    expect(ch.update).toHaveBeenCalledWith('none');
  });

  test('a batch redraws each touched chart once, after all points landed', () => {
    const a = mkChart(), b = mkChart(2), c = mkChart(3);
    window._withPushBatch(() => {
      for (let i = 0; i < 500; i++) {
        window.pushPoint(a, i * 1000, i);
        window.pushDual(b, i * 1000, i, i);
        window.pushMulti(c, i * 1000, [i, null, i]);
        expect(a.update).not.toHaveBeenCalled();
      }
    });
    expect(a.data.labels).toHaveLength(500);
    expect(b.data.datasets[1].data).toHaveLength(500);
    expect(c.data.datasets[1].data[3]).toBeNull();
    for (const ch of [a, b, c]) expect(ch.update).toHaveBeenCalledTimes(1);
  });

  test('a throwing batch still closes, so later pushes redraw again', () => {
    const ch = mkChart();
    expect(() => window._withPushBatch(() => { window.pushPoint(ch, 1000, 1); throw new Error('boom'); })).toThrow('boom');
    expect(ch.update).toHaveBeenCalledTimes(1);
    window.pushPoint(ch, 2000, 2);
    expect(ch.update).toHaveBeenCalledTimes(2);
  });

  test('a nested batch flushes once, with the outermost', () => {
    const ch = mkChart();
    window._withPushBatch(() => {
      window._withPushBatch(() => window.pushPoint(ch, 1000, 1));
      expect(ch.update).not.toHaveBeenCalled();
      window.pushPoint(ch, 2000, 2);
    });
    expect(ch.update).toHaveBeenCalledTimes(1);
  });

  test('every history backfill loop runs inside a batch', () => {
    for (const name of ['loadHistory', '_makeHistoryBackfill', 'loadManagerPerfHistory']) {
      expect(fnSrc(charts, name), name).toMatch(/_withPushBatch\(\(\) => \{/);
    }
  });
});

describe('Pinned shells are sized when Overall opens (#1437)', () => {
  test('switchTab relays out #overallGrid right after adopting the pinned cards', () => {
    const src = fnSrc(srcFile('js/foundation.js'), 'switchTab');
    const adopt = src.indexOf('adoptPinnedCards();');
    const relayout = src.indexOf("_relayoutGridCards(document.getElementById('overallGrid'))");
    expect(adopt).toBeGreaterThan(-1);
    expect(relayout).toBeGreaterThan(adopt);
    expect(relayout).toBeLessThan(src.indexOf('fetchOverallMetrics();'));
  });
});
