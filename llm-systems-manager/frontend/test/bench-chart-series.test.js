import { describe, it, expect } from 'vitest';
import { srcFile, runHarness } from './helpers/harness.js';

// Chart.js + cssVar stubs for running the real bench-autotune.js in jsdom.
const STUBS = `
  window.cssVar = () => '#888';
  HTMLCanvasElement.prototype.getContext = function () { return {}; };
  window.Chart = function (ctx, cfg) { this.data = cfg.data; this.options = cfg.options; };
  Chart.prototype.update = function () {};
  Chart.prototype.resize = function () {};
`;

const MODEL = 'org/QwenTest';
const ROWS = [
  { type: 'result', model_id: MODEL, gen_tps: null, ppt_tps: 769.87,
    n_prompt: 2048, n_gen: 0, n_depth: 0, n_batch: 2048, n_ubatch: 512, avg_ts: 769.87 },
  { type: 'result', model_id: MODEL, gen_tps: 40.02, ppt_tps: null,
    n_prompt: 0, n_gen: 512, n_depth: 0, n_batch: 2048, n_ubatch: 512, avg_ts: 40.02 },
  { type: 'result', model_id: MODEL, gen_tps: 370.08, ppt_tps: null,
    n_prompt: 4096, n_gen: 256, n_depth: 0, n_batch: 2048, n_ubatch: 512, avg_ts: 370.08 },
];

function runBench(extra = '') {
  const win = runHarness({
    sources: [STUBS, srcFile('js/bench-autotune.js')],
    bodyHtml: '<canvas id="benchChart"></canvas>',
    bootstrap: `
      _benchAddModelDatasets(${JSON.stringify(MODEL)});
      ${JSON.stringify(ROWS)}.forEach(r => _benchPushPoint(r));
      ${extra}
      window.__datasets = _benchChart.data.datasets;
      window.__labels = _benchChart.data.labels;
    `,
  });
  return { datasets: win.__datasets, labels: win.__labels };
}

const bySuffix = (datasets, suffix) => datasets.find(d => d.label.endsWith(' ' + suffix));
const ys = (ds) => (ds?.data || []).map(p => p.y);

describe('benchmark chart series routing', () => {
  it('plots ppt/gen/pg rows into their matching datasets', () => {
    const { datasets } = runBench();
    expect(ys(bySuffix(datasets, 'ppt'))).toEqual([769.87]);
    expect(ys(bySuffix(datasets, 'gen'))).toEqual([40.02]);
    expect(ys(bySuffix(datasets, 'pg'))).toEqual([370.08]);
  });

  it('keeps routing correct after an axis change re-render (_rechartBench)', () => {
    const { datasets } = runBench(`
      const sel = document.createElement('select');
      sel.id = 'benchXAxis';
      ['seq', 'n_ubatch'].forEach(v => {
        const o = document.createElement('option'); o.value = v; sel.appendChild(o);
      });
      document.body.appendChild(sel);
      sel.value = 'n_ubatch';
      _rechartBench();
    `);
    expect(ys(bySuffix(datasets, 'ppt'))).toEqual([769.87]);
    expect(ys(bySuffix(datasets, 'gen'))).toEqual([40.02]);
    expect(ys(bySuffix(datasets, 'pg'))).toEqual([370.08]);
    expect(bySuffix(datasets, 'pg').data[0].x).toBe('512');
  });

  it('drops rows with neither n_prompt nor n_gen without throwing', () => {
    const { datasets } = runBench(`_benchPushPoint({ type: 'result', model_id: ${JSON.stringify(MODEL)},
      gen_tps: 12.5, ppt_tps: null, n_prompt: 0, n_gen: 0, avg_ts: 12.5 });`);
    const total = datasets.reduce((n, d) => n + d.data.length, 0);
    expect(total).toBe(ROWS.length);
  });

  it('sorts category axis labels ascending numerically', () => {
    const { labels } = runBench(`
      const sel = document.createElement('select');
      sel.id = 'benchXAxis';
      ['seq', 'n_gen'].forEach(v => {
        const o = document.createElement('option'); o.value = v; sel.appendChild(o);
      });
      document.body.appendChild(sel);
      sel.value = 'n_gen';
      _benchPushPoint({ type: 'result', model_id: ${JSON.stringify(MODEL)},
        gen_tps: 39.9, ppt_tps: null, n_prompt: 0, n_gen: 1024, avg_ts: 39.9 });
      _rechartBench();
    `);
    expect(labels).toEqual(['0', '256', '512', '1024']);
  });

  it('gives adjacent models distinct ppt/gen/pg colors', () => {
    const { datasets } = runBench(`_benchAddModelDatasets('org/OtherModel');`);
    expect(datasets.length).toBe(6);
    const colors = datasets.map(d => d.borderColor);
    expect(new Set(colors).size).toBe(colors.length);
  });
});

describe('KV sweep re-plots earlier points when the default X axis flips (#886)', () => {
  it('first KV group lands under its kv label, not the stale default', () => {
    const win = runHarness({
      sources: [STUBS, srcFile('js/bench-autotune.js')],
      bodyHtml: '<canvas id="benchChart"></canvas><select id="benchXAxis"></select><select id="benchYAxis"></select>',
      bootstrap: `
        _benchAddModelDatasets('m1');
        _benchPushPoint({ type: 'result', model_id: 'm1', gen_tps: null, ppt_tps: 900, n_prompt: 512, n_gen: 0, n_depth: 0, n_batch: 2048, n_ubatch: 512, avg_ts: 900, type_k: 'f16', type_v: 'f16' });
        window.__xBefore = document.getElementById('benchXAxis').value;
        _benchPushPoint({ type: 'result', model_id: 'm1', gen_tps: null, ppt_tps: 880, n_prompt: 512, n_gen: 0, n_depth: 0, n_batch: 2048, n_ubatch: 512, avg_ts: 880, type_k: 'q8_0', type_v: 'q8_0' });
        window.__xAfter = document.getElementById('benchXAxis').value;
        window.__ppt = _benchChart.data.datasets.find(d => d.label.endsWith(' ppt')).data.map(p => p.x);
        window.__labels = _benchChart.data.labels;
      `,
    });
    expect(win.__xBefore).toBe('seq');
    expect(win.__xAfter).toBe('kv');
    expect(win.__ppt).toEqual(['f16/f16', 'q8_0/q8_0']);
    expect(win.__labels).toEqual(['f16/f16', 'q8_0/q8_0']);
  });
});
