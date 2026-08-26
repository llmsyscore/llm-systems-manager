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
  const start = charts.indexOf('const CHART_METRIC = {');
  expect(start, 'CHART_METRIC not found').toBeGreaterThan(-1);
  const end = charts.indexOf('\n};', start) + 3;
  (0, eval)(charts.slice(start, end) + '\nwindow.__CHART_METRIC = CHART_METRIC;');
  return window.__CHART_METRIC;
}
const map = loadChartMetric();

// Charts that plot a metric an alarm rule can target. Deliberately excludes
// throughput/IO/network canvases, which have no comparable system rule.
const EXPECTED = {
  cpuChart:           { source: 'system',    metric_name: 'cpu_total',            provider: 'llama' },
  ramChart:           { source: 'system',    metric_name: 'ram_percent',          provider: 'llama' },
  gpuChart:           { source: 'system',    metric_name: 'gpu_gpu_util_percent', provider: 'llama' },
  diskUsageChart:     { source: 'system',    metric_name: 'disk_root_percent',    provider: 'llama' },
  lmsCpuChart:        { source: 'system',    metric_name: 'cpu_total',            provider: 'lms' },
  lmsRamChart:        { source: 'system',    metric_name: 'ram_percent',          provider: 'lms' },
  lmsDiskUsageChart:  { source: 'system',    metric_name: 'disk_root_percent',    provider: 'lms' },
  lmsGpuChart:        { source: 'mac_power', metric_name: 'gpu_busy_pct',         provider: 'lms' },
  vllmCpuChart:       { source: 'system',    metric_name: 'cpu_total',            provider: 'vllm' },
  vllmRamChart:       { source: 'system',    metric_name: 'ram_percent',          provider: 'vllm' },
  vllmDiskUsageChart: { source: 'system',    metric_name: 'disk_root_percent',    provider: 'vllm' },
};

describe('CHART_METRIC covers all three dashboards (#517)', () => {
  for (const [id, want] of Object.entries(EXPECTED)) {
    test(`${id} maps to ${want.source}/${want.metric_name} on ${want.provider}`, () => {
      expect(map[id], `${id} missing from CHART_METRIC`).toBeDefined();
      expect(map[id]).toMatchObject(want);
    });
  }

  test('every mapped id is a real canvas in index.html', () => {
    const dom = new JSDOM(indexHtml);
    for (const id of Object.keys(map)) {
      expect(dom.window.document.getElementById(id), `no <canvas id="${id}">`).toBeTruthy();
    }
  });

  test('LM Studio GPU is mac_power, not a system metric', () => {
    // The LM Studio host emits no system/gpu_* series; Apple GPU busy is
    // only under mac_power. Mapping it like the llama GPU chart draws nothing.
    expect(map.lmsGpuChart.source).toBe('mac_power');
    expect(map.lmsGpuChart.metric_name).not.toMatch(/^gpu_gpu_util/);
  });
});

// --- Real chart-construction harness --------------------------------------
// Executes the real top-level chart consts/mkChart with Chart/cssVar stubbed.

class FakeChart {
  constructor(ctx, config) { this.ctx = ctx; this.config = config; }
}

function stubChart() {
  window.Chart = FakeChart;
  window.cssVar = () => '#fff';
}

// lmstudio.js/vllm.js only *reference* these (defined in charts.js); their
// shape doesn't matter for the plugin/scale assertions below.
function stubSparkPlaceholders() {
  window._sparkInteraction = {};
  window._sparkTooltip = {};
  window._zoomOpts = {};
  window._pctTick = (v) => `${v}%`;
}

function ensureCanvas(id) {
  const c = document.createElement('canvas');
  c.id = id;
  c.getContext = () => ({});
  document.body.appendChild(c);
}

// The real "Chart factory" section of charts.js: xAxis, tooltip/zoom opts,
// _syncResetZoomBtn, _layoutResetZoomBtns, _pctTick, and mkChart.
function loadChartFactory() {
  stubChart();
  const body = fnBlock('const xAxis = {', '\nfunction mkMultiChart(');
  evalAtGlobal(body
    + '\nwindow.mkChart = mkChart;'
    + '\nwindow._syncResetZoomBtn = _syncResetZoomBtn;'
    + '\nwindow._layoutResetZoomBtns = _layoutResetZoomBtns;');
}

// lmstudio.js's real top-level chart consts (lmsCpuChart, lmsRamChart, ...).
function loadLmsCharts() {
  stubChart();
  stubSparkPlaceholders();
  ['lmsRamChart', 'lmsCpuChart', 'lmsNetChart', 'lmsTpsChart', 'lmsIoChart', 'lmsGpuChart', 'lmsDiskUsageChart']
    .forEach(ensureCanvas);
  const body = fnBlock('let _lmsMetrics', 'function _fmtBytes(', lmstudio);
  evalAtGlobal(body
    + '\nwindow.lmsCpuChart = lmsCpuChart; window.lmsRamChart = lmsRamChart;'
    + '\nwindow.lmsGpuChart = lmsGpuChart; window.lmsDiskUsageChart = lmsDiskUsageChart;');
}

// vllm.js's real top-level chart consts (vllmCpuChart, vllmRamChart, ...).
function loadVllmCharts() {
  stubChart();
  stubSparkPlaceholders();
  ['vllmKvChart', 'vllmTpsChart', 'vllmCpuChart', 'vllmRamChart', 'vllmNetChart', 'vllmIoChart', 'vllmDiskUsageChart']
    .forEach(ensureCanvas);
  const body = fnBlock('let _vllmMetrics', 'function _resetVllmCharts(', vllm);
  evalAtGlobal(body
    + '\nwindow.vllmCpuChart = vllmCpuChart; window.vllmRamChart = vllmRamChart;'
    + '\nwindow.vllmDiskUsageChart = vllmDiskUsageChart;');
}

describe('annotation plugin present on the newly-mapped charts (#517)', () => {
  // Second half of _applyThresholds' guard: without this block the chart is
  // skipped even with a CHART_METRIC entry.
  test('lmstudio.js wires every mapped chart with a plugins.annotation block', () => {
    loadLmsCharts();
    for (const id of ['lmsCpuChart', 'lmsRamChart', 'lmsGpuChart', 'lmsDiskUsageChart']) {
      const chart = window[id];
      expect(chart, `${id} not constructed`).toBeTruthy();
      expect(chart.config.options.plugins.annotation, `${id}: no annotation block`).toBeDefined();
    }
  });

  test('vllm.js wires every mapped chart with a plugins.annotation block', () => {
    loadVllmCharts();
    for (const id of ['vllmCpuChart', 'vllmRamChart', 'vllmDiskUsageChart']) {
      const chart = window[id];
      expect(chart, `${id} not constructed`).toBeTruthy();
      expect(chart.config.options.plugins.annotation, `${id}: no annotation block`).toBeDefined();
    }
  });

  test('mapped LMS/vLLM charts use the shared _pctTick formatter, not an inline callback', () => {
    // Regression check: a chart that inlines its own `v => v + '%'` callback
    // instead of referencing the shared _pctTick would fail this identity check.
    loadLmsCharts();
    for (const id of ['lmsCpuChart', 'lmsRamChart', 'lmsGpuChart', 'lmsDiskUsageChart']) {
      expect(window[id].config.options.scales.y.ticks.callback, id).toBe(window._pctTick);
    }
    loadVllmCharts();
    for (const id of ['vllmCpuChart', 'vllmRamChart', 'vllmDiskUsageChart']) {
      expect(window[id].config.options.scales.y.ticks.callback, id).toBe(window._pctTick);
    }
  });
});

describe('y-axis parity with llama (#517)', () => {
  test('llama cpu/ram/gpu factory auto-scales rather than pinning 0-100', () => {
    loadChartFactory();
    ensureCanvas('probeChart');
    const chart = window.mkChart('probeChart', 'Test', '#fff');
    expect(chart.config.options.scales.y.beginAtZero).toBe(true);
    expect(chart.config.options.scales.y.max).toBeUndefined();
  });

  test('lmstudio.js: lmsCpuChart/lmsRamChart/lmsGpuChart auto-scale like llama', () => {
    loadLmsCharts();
    for (const id of ['lmsCpuChart', 'lmsRamChart', 'lmsGpuChart']) {
      const y = window[id].config.options.scales.y;
      expect(y.beginAtZero, id).toBe(true);
      expect(y.max, `${id} still pins the y-axis to 100`).toBeUndefined();
    }
  });

  test('vllm.js: vllmCpuChart/vllmRamChart auto-scale like llama', () => {
    loadVllmCharts();
    for (const id of ['vllmCpuChart', 'vllmRamChart']) {
      const y = window[id].config.options.scales.y;
      expect(y.beginAtZero, id).toBe(true);
      expect(y.max, `${id} still pins the y-axis to 100`).toBeUndefined();
    }
  });

  // llama's own diskUsageChart pins 0-100, so parity means leaving these pinned.
  test('lmstudio.js: lmsDiskUsageChart stays pinned 0-100, matching llama\'s disk chart', () => {
    loadLmsCharts();
    const y = window.lmsDiskUsageChart.config.options.scales.y;
    expect(y.min).toBe(0);
    expect(y.max).toBe(100);
  });

  test('vllm.js: vllmDiskUsageChart stays pinned 0-100, matching llama\'s disk chart', () => {
    loadVllmCharts();
    const y = window.vllmDiskUsageChart.config.options.scales.y;
    expect(y.min).toBe(0);
    expect(y.max).toBe(100);
  });
});

// Extracts and evaluates the real _thresholdHost/_applyThresholds functions.
function loadThresholdFns() {
  window.CHART_METRIC = map;
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

describe('percent tick labels stay short when zoomed (#517)', () => {
  // A zoomed axis produces float bounds; `v + '%'` printed all 15 digits,
  // which widened the y gutter and squeezed the plot.
  const pctTick = (() => {
    const start = charts.indexOf('function _pctTick(');
    expect(start, '_pctTick not found').toBeGreaterThan(-1);
    const src = charts.slice(start, charts.indexOf('\n}', start) + 2);
    // eslint-disable-next-line no-new-func
    return new Function(`${src}; return _pctTick;`)();
  })();

  // wiring (unexecutable): repo-wide absence check across all three chart files.
  test('no chart still uses the raw stringifying callback', () => {
    for (const [f, n] of [[charts, 'charts.js'], [lmstudio, 'lmstudio.js'], [vllm, 'vllm.js']]) {
      expect(f, `${n} still has a raw percent callback`).not.toContain("v => v + '%'");
    }
  });

  test.each([
    [33.278688524590016, 4],
    [23.524590163934443, 4],
    [55.90909090909091, 4],
    [0.12295081967213117, 5],
  ])('%p renders short', (v, maxLen) => {
    const out = pctTick.call({}, v, 0, []);
    expect(out.endsWith('%')).toBe(true);
    expect(out.length, `"${out}" is too long for the gutter`).toBeLessThanOrEqual(maxLen + 1);
  });

  test('whole numbers stay clean', () => {
    expect(pctTick.call({}, 90, 0, [])).toBe('90%');
    expect(pctTick.call({}, 0, 0, [])).toBe('0%');
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
