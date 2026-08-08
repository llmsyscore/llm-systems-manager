// Sparkline path builder for the companion's signature strip (#522).
// Pure: values -> SVG path 'd' strings. Dual-mode (window.CSpark + CJS).
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.CSpark = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

  const r1 = (n) => Math.round(n * 10) / 10;

  // Map values onto a w×h box (higher value = higher on screen = lower y),
  // with vertical padding so the line never touches the edges. Returns the
  // stroke path and a closed fill path; empty strings for <2 finite points.
  function path(values, w, h, opts) {
    const o = opts || {};
    const padTop = o.padTop == null ? 12 : o.padTop;
    const padBottom = o.padBottom == null ? 14 : o.padBottom;
    const vals = (values || []).filter((v) => typeof v === 'number' && isFinite(v));
    if (vals.length < 2) return { line: '', fill: '' };
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const span = (max - min) || 1;
    const usable = h - padTop - padBottom;
    const n = vals.length;
    const pts = vals.map((v, i) => {
      const x = (i / (n - 1)) * w;
      const y = padTop + (1 - (v - min) / span) * usable;
      return r1(x) + ',' + r1(y);
    });
    const line = 'M' + pts.join(' L');
    const fill = line + ' L' + r1(w) + ',' + r1(h) + ' L0,' + r1(h) + ' Z';
    return { line, fill };
  }

  return { path };
});
