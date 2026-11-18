// #517: threshold lines never rendered on the LM Studio / vLLM charts —
// _applyThresholds needs a CHART_METRIC entry AND a plugins.annotation block.
import { describe, test, expect, beforeEach } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile, blockSrc } from './helpers/harness.js';

const charts = srcFile('js/charts.js');
const lmstudio = srcFile('js/lmstudio.js');
const vllm = srcFile('js/vllm.js');
const indexHtml = srcFile('index.html');

beforeEach(() => {
  document.body.innerHTML = '';
});

// Slice fileSrc from fromMarker up to (not including) toMarker.
function fnBlock(fromMarker, toMarker, fileSrc = charts) {
  return blockSrc(fileSrc, fromMarker, toMarker, { includeEnd: false });
}

// Runs code as a fresh IIFE at global scope: bare identifiers resolve to
// window stubs, but each call gets its own scope for const/let.
function evalAtGlobal(code) {
  (0, eval)('(function () {\n' + code + '\n})();');
}

// Evaluate the real CHART_METRIC object literal (actual JS parsing) instead
// of hand-regexing its fields out of the source text.
function loadChartMetric() {
  const m = charts.match(/const CHART_METRIC = (\{[\s\S]*?\});/);
  expect(m, 'CHART_METRIC not found').toBeTruthy();
  (0, eval)('window.__CHART_METRIC = ' + m[1] + ';');
  return window.__CHART_METRIC;
}
const map = loadChartMetric();

// A stand-in map for the _applyThresholds mechanics below: one llama and one LM Studio canvas.
const FIXTURE = {
  cpuChart:    { source: 'system', metric_name: 'cpu_total',   provider: 'llama' },
  ramChart:    { source: 'system', metric_name: 'ram_percent', provider: 'llama' },
  lmsCpuChart: { source: 'system', metric_name: 'cpu_total',   provider: 'lms' },
};

// #1432: the provider dashboards moved to the kit host pages, which draw their own alarm lines.
describe('CHART_METRIC after the host pages (#1432)', () => {
  test('maps no Chart.js canvas; every mapped id would have to be a real canvas', () => {
    expect(map).toEqual({});
    const dom = new JSDOM(indexHtml);
    for (const id of Object.keys(map)) {
      expect(dom.window.document.getElementById(id), `no <canvas id="${id}">`).toBeTruthy();
    }
  });

  test('lmstudio.js and vllm.js build no dashboard charts', () => {
    expect(lmstudio).not.toMatch(/new Chart\(/);
    expect(vllm).not.toMatch(/new Chart\(/);
  });
});

// --- Real chart-construction harness --------------------------------------
// Executes the real chart factory section with Chart/cssVar stubbed.

class FakeChart {
  constructor(ctx, config) { this.ctx = ctx; this.config = config; }
}

function stubChart() {
  window.Chart = FakeChart;
  window.cssVar = () => '#fff';
}

// The real "Chart factory" section of charts.js: xAxis, tooltip/zoom opts,
// _syncResetZoomBtn and _layoutResetZoomBtns.
function loadChartFactory() {
  stubChart();
  const body = fnBlock('const xAxis = {', '\n// N-line chart factory');
  evalAtGlobal(body
    + '\nwindow._syncResetZoomBtn = _syncResetZoomBtn;'
    + '\nwindow._layoutResetZoomBtns = _layoutResetZoomBtns;');
}

// Extracts and evaluates the real _thresholdHost/_applyThresholds functions.
function loadThresholdFns() {
  window.CHART_METRIC = FIXTURE;
  const hostSrc = fnBlock('function _thresholdHost(', '\nfunction _applyThresholds(');
  const applySrc = fnBlock('function _applyThresholds(', '\nwindow._applyThresholds');
  evalAtGlobal(hostSrc + '\n' + applySrc
    + '\nwindow._thresholdHost = _thresholdHost;'
    + '\nwindow._applyThresholds = _applyThresholds;');
}

describe('_applyThresholds redraws only mapped, already-annotated charts (#517)', () => {
  let calls;

  beforeEach(() => {
    calls = [];
    window._alarmRules = [{ id: 'r1' }];
    window.Thresholds = { thresholdAnnotations: (rules, opts) => { calls.push(opts); return { mock: true }; } };
    window._agentsByProvider = { llama: [{ agent_id: 'L1', hostname: 'llama-host' }] };
    window._selectedAgent = () => 'L1';
    loadThresholdFns();
  });

  test('updates a chart that is both mapped and already carries an annotation block', () => {
    const chartA = { canvas: { id: 'cpuChart' }, options: { plugins: { annotation: {} } }, update: () => {} };
    window.Chart = { instances: { a: chartA } };
    window._applyThresholds();
    expect(chartA.options.plugins.annotation.annotations).toEqual({ mock: true });
    expect(calls.length).toBe(1);
  });

  test('#517 regression: a mapped chart missing the annotation block is never even attempted', () => {
    const chartB = { canvas: { id: 'ramChart' }, options: { plugins: {} }, update: () => {} };
    window.Chart = { instances: { b: chartB } };
    window._applyThresholds();
    expect(chartB.options.plugins.annotation).toBeUndefined();
    expect(calls.length, 'thresholdAnnotations should not be called for an unannotated chart').toBe(0);
  });

  test('a chart id absent from CHART_METRIC is never even attempted, even with an annotation block', () => {
    const chartC = { canvas: { id: 'unknownChart' }, options: { plugins: { annotation: {} } }, update: () => {} };
    window.Chart = { instances: { c: chartC } };
    window._applyThresholds();
    expect(chartC.options.plugins.annotation.annotations).toBeUndefined();
    expect(calls.length, 'thresholdAnnotations should not be called for an unmapped chart').toBe(0);
  });
});

describe('threshold host is provider-aware (#517)', () => {
  beforeEach(() => {
    window._agentsByProvider = {
      llama: [{ agent_id: 'L1', hostname: 'llama-host' }],
      lms:   [{ agent_id: 'M1', hostname: 'lms-host' }],
    };
    window._selectedAgent = (p) => ({ llama: 'L1', lms: 'M1' }[p] || null);
    loadThresholdFns();
  });

  test('_thresholdHost resolves each provider\'s own selected agent, not a hardcoded llama', () => {
    expect(window._thresholdHost('llama')).toBe('llama-host');
    expect(window._thresholdHost('lms')).toBe('lms-host');
    expect(window._thresholdHost('lms')).not.toBe(window._thresholdHost('llama'));
  });

  test('_applyThresholds passes each chart its own resolved host, not llama\'s for every chart', () => {
    window._alarmRules = [];
    const calls = [];
    window.Thresholds = { thresholdAnnotations: (rules, opts) => { calls.push(opts); return {}; } };
    const chartLlama = { canvas: { id: 'cpuChart' }, options: { plugins: { annotation: {} } }, update: () => {} };
    const chartLms   = { canvas: { id: 'lmsCpuChart' }, options: { plugins: { annotation: {} } }, update: () => {} };
    window.Chart = { instances: { a: chartLlama, b: chartLms } };
    window._applyThresholds();
    expect(calls.length).toBe(2);
    expect(calls.map(c => c.host).sort()).toEqual(['llama-host', 'lms-host']);
  });
});

describe('zoom reset button clears the plot area (#517)', () => {
  beforeEach(() => {
    loadChartFactory();
  });

  // Builds a <div class="card"><div class="chart-wrap"><canvas></div></div>
  // and a matching fake Chart instance (only the bits _syncResetZoomBtn uses).
  function cardWithCanvas(canvasId) {
    const card = document.createElement('div');
    card.className = 'card';
    const wrap = document.createElement('div');
    wrap.className = 'chart-wrap';
    const canvas = document.createElement('canvas');
    canvas.id = canvasId;
    wrap.appendChild(canvas);
    card.appendChild(wrap);
    document.body.appendChild(card);
    const chart = { canvas, isZoomedOrPanned: () => true, resetZoom: () => {} };
    return { card, wrap, canvas, chart };
  }

  test('mounts on the card, not inside the chart wrap', () => {
    const { card, wrap, chart } = cardWithCanvas('fooChart');
    window._syncResetZoomBtn(chart);
    const directChild = [...card.children].find(c => c.classList.contains('chart-reset-zoom'));
    expect(directChild, 'button not a direct child of .card').toBeTruthy();
    expect(wrap.querySelector('.chart-reset-zoom')).toBeNull();
  });

  test('buttons are keyed per canvas so two-chart cards do not collide', () => {
    const { card, chart: chartA } = cardWithCanvas('aChart');
    const wrapB = document.createElement('div');
    wrapB.className = 'chart-wrap';
    const canvasB = document.createElement('canvas');
    canvasB.id = 'bChart';
    wrapB.appendChild(canvasB);
    card.appendChild(wrapB);
    const chartB = { canvas: canvasB, isZoomedOrPanned: () => true, resetZoom: () => {} };

    window._syncResetZoomBtn(chartA);
    window._syncResetZoomBtn(chartB);
    const btns = [...card.children].filter(c => c.classList.contains('chart-reset-zoom'));
    expect(btns.length).toBe(2);
    expect(btns.map(b => b.dataset.for).sort()).toEqual(['aChart', 'bChart']);
    // _layoutResetZoomBtns fans them out so the two buttons don't overlap.
    expect(btns[0].style.right).not.toBe(btns[1].style.right);
  });

  // wiring (unexecutable): jsdom has no layout engine, so the button's
  // on-screen position can only be checked against the stylesheet text.
  test('css positions it in the header row beside the drag grip', () => {
    const css = srcFile('css/base.css');
    const block = css.slice(css.indexOf('.chart-reset-zoom {'), css.indexOf('.chart-reset-zoom:hover'));
    expect(block).toMatch(/top:\s*6px/);
    expect(block).toMatch(/right:\s*30px/);
  });
});
