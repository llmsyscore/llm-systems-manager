// #1439: charts on hidden tabs skip their redraw and catch up when shown.
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { srcFile, fnSrc, blockSrc, evalGlobal } from './helpers/harness.js';

const foundation = srcFile('js/foundation.js');
const charts = srcFile('js/charts.js');

evalGlobal([
  blockSrc(foundation, 'const _dirtyCharts = new Set();', '// Apply chart defaults once at load', { includeEnd: false }),
  'window._chartUpdate = _chartUpdate; window._flushDirtyCharts = _flushDirtyCharts; window._dirtyChartsRef = _dirtyCharts;',
  blockSrc(charts, 'let _pushBatch = null;', 'function pushPoint', { includeEnd: false }),
  fnSrc(charts, '_clearChart'),
  'window._withPushBatch = _withPushBatch; window._pushUpdate = _pushUpdate; window._clearChart = _clearChart;',
].join('\n'));

// A chart stub whose canvas is shown or hidden by the test.
function mkChart(shown) {
  return { canvas: { offsetParent: shown ? {} : null, isConnected: true },
           data: { labels: [1], datasets: [{ data: [1] }] }, update: vi.fn() };
}
// Lets the per-task visibility memo reset, as it would between browser tasks.
const nextTask = () => new Promise(r => setTimeout(r, 0));

beforeEach(() => window._dirtyChartsRef.clear());

describe('chart visibility gate (#1439)', () => {
  test('a shown chart redraws at once; a hidden one is only marked', () => {
    const shown = mkChart(true), hidden = mkChart(false);
    window._chartUpdate(shown);
    window._chartUpdate(hidden);
    expect(shown.update).toHaveBeenCalledWith('none');
    expect(hidden.update).not.toHaveBeenCalled();
    expect(window._dirtyChartsRef.has(hidden)).toBe(true);
  });

  test('a flush resets the visibility read, so a chart shown in the same task redraws at once', async () => {
    await nextTask();
    const ch = mkChart(false);
    window._chartUpdate(ch);
    ch.canvas.offsetParent = {};
    window._flushDirtyCharts();
    window._chartUpdate(ch);
    expect(ch.update).toHaveBeenCalledTimes(2);
  });

  test('the flush redraws a marked chart once it is shown, and only then', () => {
    const ch = mkChart(false);
    window._chartUpdate(ch);
    window._flushDirtyCharts();
    expect(ch.update).not.toHaveBeenCalled();
    ch.canvas.offsetParent = {};
    window._flushDirtyCharts();
    expect(ch.update).toHaveBeenCalledTimes(1);
    window._flushDirtyCharts();
    expect(ch.update).toHaveBeenCalledTimes(1);
  });

  test('the flush drops charts whose canvas left the page', () => {
    const ch = mkChart(false);
    window._chartUpdate(ch);
    ch.canvas.isConnected = false;
    window._flushDirtyCharts();
    expect(window._dirtyChartsRef.size).toBe(0);
  });

  test('visibility is read once per task', async () => {
    await nextTask();
    let reads = 0;
    const cv = { isConnected: true, get offsetParent() { reads++; return {}; } };
    const ch = { canvas: cv, update: vi.fn() };
    window._chartUpdate(ch); window._chartUpdate(ch); window._chartUpdate(ch);
    expect(reads).toBe(1);
    await nextTask();
    window._chartUpdate(ch);
    expect(reads).toBe(2);
  });

  test('live pushes, batch flushes and chart clears go through the gate', async () => {
    await nextTask();
    const shown = mkChart(true), hidden = mkChart(false);
    window._pushUpdate(shown); window._pushUpdate(hidden);
    window._withPushBatch(() => { window._pushUpdate(shown); window._pushUpdate(hidden); });
    window._clearChart(hidden);
    expect(shown.update).toHaveBeenCalledTimes(2);
    expect(hidden.update).not.toHaveBeenCalled();
    expect(hidden.data.labels).toEqual([]);
  });

  test('every path that shows a tab, panel or card flushes', () => {
    const shows = [[foundation, 'switchTab'], [srcFile('js/boot.js'), 'switchSubTab'], [foundation, 'adoptPinnedCards'],
                   [foundation, '_applyHiddenForGrid'], [foundation, 'toggleCard'], [foundation, 'applyLayout']];
    for (const [src, name] of shows) {
      expect(fnSrc(src, name), name).toMatch(/_flushDirtyCharts\(\)/);
    }
  });
});

describe('clears and retints (#1439)', () => {
  test('a chart cleared inside a batch with no pushes redraws once when the batch closes', async () => {
    await nextTask();
    const ch = mkChart(true);
    window._withPushBatch(() => { window._clearChart(ch); expect(ch.update).not.toHaveBeenCalled(); });
    expect(ch.update).toHaveBeenCalledTimes(1);
  });

  test('_retintCharts redraws only when the chrome colours changed', async () => {
    await nextTask();
    const vars = { '--fg-muted': '#111', '--border-soft': '#222', '--bg-card': '#333', '--fg': '#444', '--border': '#555' };
    window.cssVar = (n) => vars[n] || '';
    window.Chart = { defaults: {}, instances: {} };
    evalGlobal([fnSrc(foundation, '_themeChartDefaults'), fnSrc(foundation, '_retintCharts'),
      "var _chartTint = ['--fg-muted', '--border-soft', '--bg-card', '--fg', '--fg-muted', '--border'].map(v => cssVar(v)).join('|');",
      'window._retintCharts = _retintCharts;'].join('\n'));
    const ch = mkChart(true);
    ch.options = { scales: { y: { ticks: { color: '#111' }, grid: { color: '#222' } } } };
    window.Chart.instances = { a: ch };
    window._retintCharts();
    expect(ch.update).not.toHaveBeenCalled();
    vars['--fg-muted'] = '#abc';
    window._retintCharts();
    expect(ch.update).toHaveBeenCalledTimes(1);
    expect(ch.options.scales.y.ticks.color).toBe('#abc');
    await nextTask();
    vars['--fg-muted'] = '#111';
    window._retintCharts();
    expect(ch.update).toHaveBeenCalledTimes(2);
  });
});
