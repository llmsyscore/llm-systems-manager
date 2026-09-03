// Alarm-rule threshold lines as chartjs-plugin-annotation objects. Mirrors the
// engine's threshold_evaluator value precedence; shared by both dashboards.

// Severity → line color. Read from the page's theme tokens when a DOM is
// present; the hex fallbacks serve the manager's tests.
const _SEV_COLOR = { critical: '#ef4444', warning: '#f59e0b', info: '#7aa2ff' };
function _sevColor(sev) {
  if (typeof document === 'undefined' || !document.documentElement) return _SEV_COLOR[sev] || _SEV_COLOR.info;
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue({ critical: '--crit', warning: '--warn', info: '--accent' }[sev] || '--accent').trim();
  return v || _SEV_COLOR[sev] || _SEV_COLOR.info;
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
    const unit = t.unit || '';
    const color = _sevColor(rule.severity);
    const lines = [];
    if (rule.rule_type === 'threshold_above') { const v = t.upper ?? t.value ?? t.critical ?? t.warning; if (v != null) lines.push(v); }
    else if (rule.rule_type === 'threshold_below') { const v = t.lower ?? t.value ?? t.warning ?? t.critical; if (v != null) lines.push(v); }
    else if (rule.rule_type === 'threshold_range') { if (t.lower != null && t.upper != null) { lines.push(t.lower); lines.push(t.upper); } }
    lines.forEach((v, i) => {
      out[`thr_${rule.rule_id}_${i}`] = {
        type: 'line', yMin: v, yMax: v, borderColor: color, borderWidth: 1.5, borderDash: [5, 5],
        adjustScaleRange: false,
        label: { display: true, content: `${rule.name} ${v}${unit ? ' ' + unit : ''}`, position: 'start',
          backgroundColor: 'transparent', color, font: { size: 10, family: 'IBM Plex Mono, ui-monospace, monospace' },
          padding: { top: 0, bottom: 2, left: 4, right: 4 }, yAdjust: -8 },
      };
    });
  });
  return out;
}

if (typeof window !== 'undefined')
  window.Thresholds = { thresholdAnnotations, SEV_COLOR: _SEV_COLOR };
if (typeof module !== 'undefined' && module.exports)
  module.exports = { thresholdAnnotations, SEV_COLOR: _SEV_COLOR };
