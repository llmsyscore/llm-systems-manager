// #517: threshold lines never rendered on the LM Studio / vLLM charts.
// _applyThresholds needs BOTH a CHART_METRIC entry and an existing
// plugins.annotation block, and those dashboards had neither.
import { describe, test, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = (f) => readFileSync(join(here, '..', f), 'utf8');
const charts = src('js/charts.js');
const lmstudio = src('js/lmstudio.js');
const vllm = src('js/vllm.js');
const indexHtml = src('index.html');

// Parse the CHART_METRIC literal into {id: {source, metric_name, provider}}.
function chartMetric() {
  const start = charts.indexOf('const CHART_METRIC = {');
  expect(start, 'CHART_METRIC not found').toBeGreaterThan(-1);
  const body = charts.slice(start, charts.indexOf('\n};', start));
  const out = {};
  for (const m of body.matchAll(/(\w+):\s*\{([^}]*)\}/g)) {
    const field = (k) => (m[2].match(new RegExp(`${k}:\\s*'([^']*)'`)) || [])[1];
    out[m[1]] = { source: field('source'), metric_name: field('metric_name'), provider: field('provider') };
  }
  return out;
}

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
  const map = chartMetric();

  for (const [id, want] of Object.entries(EXPECTED)) {
    test(`${id} maps to ${want.source}/${want.metric_name} on ${want.provider}`, () => {
      expect(map[id], `${id} missing from CHART_METRIC`).toBeDefined();
      expect(map[id]).toMatchObject(want);
    });
  }

  test('every mapped id is a real canvas in index.html', () => {
    for (const id of Object.keys(map)) {
      expect(indexHtml, `no <canvas id="${id}">`).toContain(`canvas id="${id}"`);
    }
  });

  test('LM Studio GPU is mac_power, not a system metric', () => {
    // The LM Studio host emits no system/gpu_* series; Apple GPU busy is
    // only under mac_power. Mapping it like the llama GPU chart draws nothing.
    expect(map.lmsGpuChart.source).toBe('mac_power');
    expect(map.lmsGpuChart.metric_name).not.toMatch(/^gpu_gpu_util/);
  });
});

describe('annotation plugin present on the newly-mapped charts (#517)', () => {
  // Second half of _applyThresholds' guard: without this block the chart is
  // skipped even with a CHART_METRIC entry.
  const idsIn = (file) => Object.keys(EXPECTED).filter(id => file.includes(`getElementById('${id}')`));

  test('lmstudio.js configures annotation on its mapped charts', () => {
    expect(idsIn(lmstudio).length).toBeGreaterThan(0);
    expect(lmstudio).toContain('annotation:');
  });

  test('vllm.js configures annotation on its mapped charts', () => {
    expect(idsIn(vllm).length).toBeGreaterThan(0);
    expect(vllm).toContain('annotation:');
  });

  test('every mapped LMS/vLLM chart config carries an annotation block', () => {
    for (const [file, name] of [[lmstudio, 'lmstudio.js'], [vllm, 'vllm.js']]) {
      for (const id of idsIn(file)) {
        // Slice from the canvas lookup to the end of that Chart config.
        const start = file.indexOf(`getElementById('${id}')`);
        const next = file.indexOf('getElementById(', start + 10);
        const block = file.slice(start, next === -1 ? file.length : next);
        expect(block, `${name}: ${id} has no annotation block`).toContain('annotation:');
      }
    }
  });
});

describe('y-axis parity with llama (#517)', () => {
  // Annotations are built with adjustScaleRange:false, so an auto-scaling axis
  // keeps a 90/95 line off-screen until the metric climbs toward it. A pinned
  // 0-100 axis shows it permanently and wastes vertical resolution.
  const cfgBlock = (file, id) => {
    const start = file.indexOf(`getElementById('${id}')`);
    expect(start, `${id} not found`).toBeGreaterThan(-1);
    const next = file.indexOf('getElementById(', start + 20);
    return file.slice(start, next === -1 ? file.length : next);
  };

  test('llama cpu/ram/gpu factory auto-scales rather than pinning 0-100', () => {
    const start = charts.indexOf('function mkChart(');
    const body = charts.slice(start, charts.indexOf('\n}', start));
    expect(body).toContain('beginAtZero: true');
    expect(body).not.toMatch(/y:\s*\{[^}]*max:\s*100/);
  });

  for (const [file, name, ids] of [
    [lmstudio, 'lmstudio.js', ['lmsCpuChart', 'lmsRamChart', 'lmsGpuChart']],
    [vllm, 'vllm.js', ['vllmCpuChart', 'vllmRamChart']],
  ]) {
    for (const id of ids) {
      test(`${name}: ${id} auto-scales like llama`, () => {
        const block = cfgBlock(file, id);
        expect(block, `${id} still pins the y-axis to 0-100`).not.toMatch(/y:\s*\{\s*min:\s*0,\s*max:\s*100/);
        expect(block).toContain('beginAtZero: true');
      });
    }
  }

  // llama's own diskUsageChart pins 0-100, so parity means leaving these pinned.
  for (const [file, name, id] of [
    [lmstudio, 'lmstudio.js', 'lmsDiskUsageChart'],
    [vllm, 'vllm.js', 'vllmDiskUsageChart'],
  ]) {
    test(`${name}: ${id} stays pinned 0-100, matching llama's disk chart`, () => {
      expect(cfgBlock(file, id)).toMatch(/y:\s*\{\s*min:\s*0,\s*max:\s*100/);
    });
  }
});

describe('threshold host is provider-aware (#517)', () => {
  test('_thresholdHost takes a provider instead of hardcoding llama', () => {
    const start = charts.indexOf('function _thresholdHost(');
    expect(start).toBeGreaterThan(-1);
    const body = charts.slice(start, charts.indexOf('\n}', start));
    expect(body).toMatch(/_thresholdHost\(\s*provider/);
    expect(body, 'still hardcodes the llama provider').not.toContain("_selectedAgent('llama')");
  });

  test('_applyThresholds resolves the host from each chart meta', () => {
    const start = charts.indexOf('function _applyThresholds(');
    const body = charts.slice(start, charts.indexOf('\n}\n', start));
    expect(body).toMatch(/_thresholdHost\(\s*meta\.provider/);
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
  const fn = charts.slice(charts.indexOf('function _syncResetZoomBtn('),
                          charts.indexOf('function _layoutResetZoomBtns('));

  test('mounts on the card, not inside the chart wrap', () => {
    expect(fn).toContain("closest('.card')");
    expect(fn).not.toMatch(/wrap\.appendChild\(btn\)/);
  });

  test('buttons are keyed per canvas so two-chart cards do not collide', () => {
    expect(fn).toContain('data-for');
  });

  test('css positions it in the header row beside the drag grip', () => {
    const css = src('css/base.css');
    const block = css.slice(css.indexOf('.chart-reset-zoom {'), css.indexOf('.chart-reset-zoom:hover'));
    expect(block).toMatch(/top:\s*6px/);
    expect(block).toMatch(/right:\s*30px/);
  });
});
