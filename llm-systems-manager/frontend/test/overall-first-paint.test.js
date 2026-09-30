// #1140: history backfills redraw each chart once per batch instead of once
// per point, and the Overall hero's Power/Energy overlays start enabled.
import { describe, test, expect, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile, fnSrc, blockSrc, evalGlobal } from './helpers/harness.js';

const charts = srcFile('js/charts.js');
const html = srcFile('index.html');

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

describe('Overall hero live point waits for the first backfill (#1140)', () => {
  const overall = srcFile('js/overall.js');
  function loadFetchOverall(aggregate) {
    evalGlobal([fnSrc(overall, 'fetchOverallMetrics'), 'window.fetchOverallMetrics = fetchOverallMetrics;'].join('\n'));
    window._ovRefreshEnergy = vi.fn();
    window._ovPaintBand = vi.fn();
    window.pushDual = vi.fn();
    window.ovHeroBucketMs = () => 300000;
    window.ovHeroBucketSync = vi.fn();
    window.ovHeroChart = { data: { labels: [] } };
    window.fetch = vi.fn(async () => ({ ok: true, json: async () => aggregate }));
  }

  test('band paints but no hero point while the backfill is in flight on an empty chart', async () => {
    loadFetchOverall({ throughput: { total_tps: 5, total_pps: 1 } });
    window._ovHistoryInflight = 1; window._ovHeroRows = null;
    await window.fetchOverallMetrics();
    expect(window._ovPaintBand).toHaveBeenCalledTimes(1);
    expect(window.pushDual).not.toHaveBeenCalled();
  });

  test('hero point lands once the backfill settled, or when rows are already cached', async () => {
    loadFetchOverall({ throughput: { total_tps: 5, total_pps: 1 } });
    window._ovHistoryInflight = 0; window._ovHeroRows = null;
    await window.fetchOverallMetrics();
    expect(window.pushDual).toHaveBeenCalledTimes(1);
    window._ovHistoryInflight = 1; window._ovHeroRows = [{ ts: 1 }];
    await window.fetchOverallMetrics();
    expect(window.pushDual).toHaveBeenCalledTimes(2);
  });

  test('loadOverallHistory counts itself in flight only around its fetch', async () => {
    evalGlobal([fnSrc(charts, 'loadOverallHistory'), 'window.loadOverallHistory = loadOverallHistory;'].join('\n'));
    window._ovHistoryGen = 0; window._ovHistoryInflight = 0;
    window.ovHeroChart = {};
    let release;
    window._historyRows = () => new Promise(res => { release = res; });
    const run = window.loadOverallHistory();
    expect(window._ovHistoryInflight).toBe(1);
    release([]);
    await run;
    expect(window._ovHistoryInflight).toBe(0);
  });
});

describe('Overall hero overlays default on (#1140)', () => {
  test('index.html ships both toggles checked', () => {
    const doc = new JSDOM(html).window.document;
    expect(doc.getElementById('ovShowPower').checked).toBe(true);
    expect(doc.getElementById('ovShowEnergy').checked).toBe(true);
  });

  function heroWith(bodyHtml) {
    document.body.innerHTML = bodyHtml + '<canvas id="ovHeroChart"></canvas>';
    window.Chart = function (ctx, cfg) { return { data: cfg.data }; };
    window.cssVar = () => '#000';
    window._sparkInteraction = {}; window._sparkTooltip = {}; window._zoomOpts = {}; window.xAxis = {};
    window._hex6 = (c) => c;
    HTMLCanvasElement.prototype.getContext = () => ({});
    evalGlobal([fnSrc(charts, '_ovOverlayOn'), fnSrc(charts, '_mkHeroChart'), 'window._mkHeroChart = _mkHeroChart;'].join('\n'));
    return window._mkHeroChart().data.datasets;
  }

  test('the chart datasets follow the toggles: checked → visible', () => {
    const ds = heroWith('<input type="checkbox" id="ovShowPower" checked><input type="checkbox" id="ovShowEnergy" checked>');
    expect(ds[2].hidden).toBe(false);
    expect(ds[3].hidden).toBe(false);
  });

  test('the chart datasets follow the toggles: unchecked → hidden', () => {
    const ds = heroWith('<input type="checkbox" id="ovShowPower"><input type="checkbox" id="ovShowEnergy">');
    expect(ds[2].hidden).toBe(true);
    expect(ds[3].hidden).toBe(true);
  });
});
