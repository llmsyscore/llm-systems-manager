// Alarm-rule threshold lines as chartjs-plugin-annotation objects. Mirrors the
// engine's threshold_evaluator value precedence; shared by both dashboards.

// Severity → line color (matches the alarm console's severity tokens).
const _SEV_COLOR = { critical: '#ef4444', warning: '#f59e0b', info: '#7aa2ff' };

// Line colour at reduced opacity so the threshold reads as a background
// reference rather than competing with the series.
const _LINE_ALPHA = 0.45;
function _fade(hex) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${_LINE_ALPHA})`;
}

// Annotation line objects from enabled rules matching source + metricName.
// hostWildcard: a null host matches any source_host (console "any host").
function thresholdAnnotations(rules, opts) {
  const o = opts || {};
  const source = o.source;
  const metricName = o.metricName;
  const host = o.host != null ? o.host : null;
  const hostWildcard = !!o.hostWildcard;
  const out = {};
  (rules || []).forEach(rule => {
    if (!rule || !rule.enabled) return;
    if (rule.metric_source !== source || rule.metric_name !== metricName) return;
    const hostScoped = !(hostWildcard && host === null);
    if (hostScoped && rule.source_host && rule.source_host !== host) return;
    const t = (rule.config && rule.config.threshold) || {};
    const color = _SEV_COLOR[rule.severity] || _SEV_COLOR.info;
    const lines = [];
    if (rule.rule_type === 'threshold_above') { const v = t.upper ?? t.value ?? t.critical ?? t.warning; if (v != null) lines.push(v); }
    else if (rule.rule_type === 'threshold_below') { const v = t.lower ?? t.value ?? t.warning ?? t.critical; if (v != null) lines.push(v); }
    else if (rule.rule_type === 'threshold_range') { if (t.lower != null && t.upper != null) { lines.push(t.lower); lines.push(t.upper); } }
    lines.forEach((v, i) => {
      out[`thr_${rule.rule_id}_${i}`] = {
        // Line only, no label: severity is carried by colour and nothing is
        // painted over the series.
        type: 'line', yMin: v, yMax: v, borderColor: _fade(color), borderWidth: 1, borderDash: [4, 4],
        adjustScaleRange: false,
      };
    });
  });
  return out;
}

if (typeof window !== 'undefined')
  window.Thresholds = { thresholdAnnotations, SEV_COLOR: _SEV_COLOR };
if (typeof module !== 'undefined' && module.exports)
  module.exports = { thresholdAnnotations, SEV_COLOR: _SEV_COLOR };
