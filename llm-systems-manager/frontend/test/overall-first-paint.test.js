// #1432: the manager perf seed feeds the Manager page's kit charts; #1437: the pinned shells are sized when Overall opens.
import { describe, test, expect } from 'vitest';
import { srcFile, fnSrc } from './helpers/harness.js';

const charts = srcFile('js/charts.js');

describe('Manager perf seed (#1432)', () => {
  test('the manager perf seed hands its points to the Manager page and pushes into no Chart.js chart (#1432)', () => {
    const src = fnSrc(charts, 'loadManagerPerfHistory');
    expect(src).toMatch(/window\._mgPerf = \{ mgr, ae, influx \}/);
    expect(src).not.toMatch(/pushMulti|_clearChart|_withPushBatch/);
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
