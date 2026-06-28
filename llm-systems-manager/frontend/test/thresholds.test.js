import { describe, test, expect } from 'vitest';
import thresholds from '../js/lib/thresholds.js';

const { thresholdAnnotations } = thresholds;

// Build a rule with sensible defaults; override any field per case.
const rule = (over = {}) => ({
  rule_id: over.rule_id || 'r1',
  name: over.name || 'Test rule',
  enabled: over.enabled !== undefined ? over.enabled : true,
  metric_source: over.metric_source || 'cpu',
  metric_name: over.metric_name || 'usage_percent',
  rule_type: over.rule_type || 'threshold_above',
  severity: over.severity || 'warning',
  source_host: over.source_host !== undefined ? over.source_host : null,
  config: { threshold: over.threshold || {} },
});

const CPU = { source: 'cpu', metricName: 'usage_percent', host: null, hostWildcard: false };
const yMins = (out) => Object.values(out).map(a => a.yMin);

describe('threshold_above value precedence (upper → value → critical → warning)', () => {
  test('upper wins over all', () => {
    const out = thresholdAnnotations([rule({ threshold: { upper: 90, value: 80, critical: 95, warning: 70 } })], CPU);
    expect(yMins(out)).toEqual([90]);
  });
  test('falls back to value when upper absent', () => {
    expect(yMins(thresholdAnnotations([rule({ threshold: { value: 80, critical: 95, warning: 70 } })], CPU))).toEqual([80]);
  });
  test('falls back to critical when upper+value absent', () => {
    expect(yMins(thresholdAnnotations([rule({ threshold: { critical: 95, warning: 70 } })], CPU))).toEqual([95]);
  });
  test('falls back to warning last', () => {
    expect(yMins(thresholdAnnotations([rule({ threshold: { warning: 70 } })], CPU))).toEqual([70]);
  });
  test('no line when no usable field set', () => {
    expect(thresholdAnnotations([rule({ threshold: {} })], CPU)).toEqual({});
  });
});

describe('threshold_below value precedence (lower → value → warning → critical)', () => {
  const below = (threshold) => rule({ rule_type: 'threshold_below', threshold });
  test('lower wins over all', () => {
    expect(yMins(thresholdAnnotations([below({ lower: 10, value: 20, warning: 30, critical: 5 })], CPU))).toEqual([10]);
  });
  test('falls back to value when lower absent', () => {
    expect(yMins(thresholdAnnotations([below({ value: 20, warning: 30, critical: 5 })], CPU))).toEqual([20]);
  });
  test('warning beats critical (opposite of above)', () => {
    expect(yMins(thresholdAnnotations([below({ warning: 30, critical: 5 })], CPU))).toEqual([30]);
  });
  test('falls back to critical last', () => {
    expect(yMins(thresholdAnnotations([below({ critical: 5 })], CPU))).toEqual([5]);
  });
});

describe('threshold_range emits two lines', () => {
  test('one line each for lower and upper', () => {
    const out = thresholdAnnotations([rule({ rule_type: 'threshold_range', threshold: { lower: 10, upper: 90 } })], CPU);
    expect(yMins(out).sort((a, b) => a - b)).toEqual([10, 90]);
    expect(Object.keys(out)).toEqual(['thr_r1_0', 'thr_r1_1']);
  });
  test('single-bound range draws nothing (engine requires both bounds to fire)', () => {
    expect(thresholdAnnotations([rule({ rule_type: 'threshold_range', threshold: { upper: 90 } })], CPU)).toEqual({});
  });
});

describe('anomaly rule types draw no line', () => {
  for (const rt of ['z_score', 'moving_average', 'percentile', 'rate_of_change']) {
    test(rt, () => {
      expect(thresholdAnnotations([rule({ rule_type: rt, threshold: { value: 3 } })], CPU)).toEqual({});
    });
  }
});

describe('filters', () => {
  test('disabled rule is skipped', () => {
    expect(thresholdAnnotations([rule({ enabled: false, threshold: { upper: 90 } })], CPU)).toEqual({});
  });
  test('wrong metric_source is skipped', () => {
    expect(thresholdAnnotations([rule({ metric_source: 'ram', threshold: { upper: 90 } })], CPU)).toEqual({});
  });
  test('wrong metric_name is skipped', () => {
    expect(thresholdAnnotations([rule({ metric_name: 'temp_c', threshold: { upper: 90 } })], CPU)).toEqual({});
  });
});

describe('host scoping — dashboard (hostWildcard=false, concrete host)', () => {
  const opts = { source: 'cpu', metricName: 'usage_percent', host: 'h1', hostWildcard: false };
  test('unscoped rule (source_host null) always matches', () => {
    expect(yMins(thresholdAnnotations([rule({ source_host: null, threshold: { upper: 90 } })], opts))).toEqual([90]);
  });
  test('rule scoped to this host matches', () => {
    expect(yMins(thresholdAnnotations([rule({ source_host: 'h1', threshold: { upper: 90 } })], opts))).toEqual([90]);
  });
  test('rule scoped to another host is excluded', () => {
    expect(thresholdAnnotations([rule({ source_host: 'h2', threshold: { upper: 90 } })], opts)).toEqual({});
  });
});

describe('host scoping — null host', () => {
  test('dashboard (hostWildcard=false): scoped rule excluded when host unknown', () => {
    const opts = { source: 'cpu', metricName: 'usage_percent', host: null, hostWildcard: false };
    expect(thresholdAnnotations([rule({ source_host: 'h1', threshold: { upper: 90 } })], opts)).toEqual({});
  });
  test('console (hostWildcard=true): scoped rule matches when "any host" selected', () => {
    const opts = { source: 'cpu', metricName: 'usage_percent', host: null, hostWildcard: true };
    expect(yMins(thresholdAnnotations([rule({ source_host: 'h1', threshold: { upper: 90 } })], opts))).toEqual([90]);
  });
});

describe('multiple rules accumulate distinct keys', () => {
  test('two rules → two entries keyed by rule_id', () => {
    const out = thresholdAnnotations([
      rule({ rule_id: 'a', severity: 'critical', threshold: { upper: 95 } }),
      rule({ rule_id: 'b', severity: 'warning', threshold: { upper: 80 } }),
    ], CPU);
    expect(Object.keys(out).sort()).toEqual(['thr_a_0', 'thr_b_0']);
    expect(yMins(out).sort((a, b) => a - b)).toEqual([80, 95]);
  });
});
