// #1432: a host page "View … in metrics" link lands on its metric, with one note when the console has no history for it.
import { describe, test, expect, beforeAll, beforeEach } from 'vitest';
import { srcFile, evalGlobal } from './helpers/harness.js';

const SRC = srcFile('../../llm-systems-alarm-engine/frontend/js/metrics.js');
const CATALOG = [
  { source: 'system', metric_name: 'cpu_total', hostname: 'llm-a' },
  { source: 'system', metric_name: 'ram_percent', hostname: 'llm-a' },
  { source: 'system', metric_name: 'ram_percent', hostname: 'llm-b' },
];
let toasts;

beforeAll(() => {
  window.escapeHtml = (v) => String(v);
  window.fmtAgo = () => 'now';
  window.parseTs = (v) => (v ? new Date(v) : null);
  window.ToastManager = { show: (m) => toasts.push(m) };
  window.AppState = { metrics: [], filters: { metrics: { host: '', key: '', minutes: 60, offset: 0, mark: null, want: null } } };
  evalGlobal(SRC + '\nwindow.__MV = MetricsView; window.__MM = MetricsManager;');
});
beforeEach(() => {
  toasts = [];
  document.body.innerHTML = '<select id="metricHost"></select><select id="metricSelect"></select><div id="metricsSum"></div>';
  window.__MM._knownAgentHosts = null;
  Object.assign(window.AppState.filters.metrics, { host: '', key: '', offset: 0, mark: null, want: null });
});
const want = (host, source, name) => {
  const f = window.AppState.filters.metrics;
  Object.assign(f, { host, key: `${host}|${source}/${name}`, want: `${host}|${source}/${name}` });
  return f;
};

describe('alarm console metric links', () => {
  test('a known metric is picked with no note and the link is used once', () => {
    window.AppState.metrics = CATALOG;
    const f = want('llm-a', 'system', 'ram_percent');
    window.__MV.populate();
    expect(f.key).toBe('llm-a|system/ram_percent');
    expect(toasts).toEqual([]);
    expect(f.want).toBeNull();
  });
  test('a metric the host has no history for gives one note and falls back', () => {
    window.AppState.metrics = CATALOG;
    const f = want('llm-a', 'llama', 'tokens_per_second');
    window.__MV.populate();
    expect(toasts).toHaveLength(1);
    expect(toasts[0]).toMatch(/^No history for .+ on llm-a$/);
    expect(f.key).toBe('llm-a|system/cpu_total');
    window.__MV.populate();
    expect(toasts).toHaveLength(1);
  });
  test('an unknown host says so and shows the metric for all hosts', () => {
    window.AppState.metrics = CATALOG;
    const f = want('llm-z', 'system', 'ram_percent');
    window.__MV.populate();
    expect(toasts).toHaveLength(1);
    expect(toasts[0]).toMatch(/^No history from llm-z; showing .+ for all hosts$/);
    expect(f.host).toBe('');
    expect(f.key).toBe('*|system/ram_percent');
  });
  test('an empty catalog keeps the link for the next fill and says nothing', () => {
    window.AppState.metrics = [];
    const f = want('llm-a', 'system', 'ram_percent');
    window.__MV.populate();
    expect(toasts).toEqual([]);
    expect(f.want).toBe('llm-a|system/ram_percent');
  });
});
