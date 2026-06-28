// Alarm-rule threshold lines as chartjs-plugin-annotation objects. Mirrors the
// engine's threshold_evaluator value precedence; shared by both dashboards.

// Severity → line color (matches the alarm console's severity tokens).
const _SEV_COLOR = { critical: '#ef4444', warning: '#f59e0b', info: '#7aa2ff' };

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
    const unit = t.unit || '';
    const color = _SEV_COLOR[rule.severity] || _SEV_COLOR.info;
    const lines = [];
    if (rule.rule_type === 'threshold_above') { const v = t.upper ?? t.value ?? t.critical ?? t.warning; if (v != null) lines.push(v); }
    else if (rule.rule_type === 'threshold_below') { const v = t.lower ?? t.value ?? t.warning ?? t.critical; if (v != null) lines.push(v); }
    else if (rule.rule_type === 'threshold_range') { if (t.lower != null && t.upper != null) { lines.push(t.lower); lines.push(t.upper); } }
    lines.forEach((v, i) => {
      out[`thr_${rule.rule_id}_${i}`] = {
        type: 'line', yMin: v, yMax: v, borderColor: color, borderWidth: 1.5, borderDash: [5, 5],
        label: { display: true, content: `${rule.name}: ${v}${unit}`, position: 'end',
          backgroundColor: color, color: '#fff', font: { size: 9 }, padding: 2 },
      };
    });
  });
  return out;
}

if (typeof window !== 'undefined')
  window.Thresholds = { thresholdAnnotations, SEV_COLOR: _SEV_COLOR };
if (typeof module !== 'undefined' && module.exports)
  module.exports = { thresholdAnnotations, SEV_COLOR: _SEV_COLOR };
