// Sparkline path builder for the companion's signature strip (#522).
// Pure: values -> SVG path 'd' strings + point coords. Dual-mode
// (window.CSpark + CJS).
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.CSpark = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

  const r1 = (n) => Math.round(n * 10) / 10;

  // Monotone cubic (Fritsch-Carlson) tangents: smooths the curve without
  // overshooting, so a rate series never dips below its own minimum.
  function tangents(pts) {
    const n = pts.length;
    const d = [], m = [];
    for (let i = 0; i < n - 1; i++) {
      const dx = pts[i + 1].x - pts[i].x;
      d.push(dx === 0 ? 0 : (pts[i + 1].y - pts[i].y) / dx);
    }
    m.push(d[0]);
    for (let i = 1; i < n - 1; i++) {
      m.push(d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2);
    }
    m.push(d[n - 2]);
    for (let i = 0; i < n - 1; i++) {
      if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
      const a = m[i] / d[i], b = m[i + 1] / d[i];
      const s = a * a + b * b;
      if (s > 9) {
        const t = 3 / Math.sqrt(s);
        m[i] = t * a * d[i];
        m[i + 1] = t * b * d[i];
      }
    }
    return m;
  }

  function smoothPath(pts) {
    const m = tangents(pts);
    let out = 'M' + r1(pts[0].x) + ',' + r1(pts[0].y);
    for (let i = 0; i < pts.length - 1; i++) {
      const dx = (pts[i + 1].x - pts[i].x) / 3;
      out += ' C' + r1(pts[i].x + dx) + ',' + r1(pts[i].y + m[i] * dx)
        + ' ' + r1(pts[i + 1].x - dx) + ',' + r1(pts[i + 1].y - m[i + 1] * dx)
        + ' ' + r1(pts[i + 1].x) + ',' + r1(pts[i + 1].y);
    }
    return out;
  }

  // Map values onto a w×h box (higher value = higher on screen = lower y),
  // with vertical padding so the line never touches the edges. Returns the
  // stroke path, a closed fill path and the point coords; empty for <2 points.
  function path(values, w, h, opts) {
    const o = opts || {};
    const padTop = o.padTop == null ? 12 : o.padTop;
    const padBottom = o.padBottom == null ? 14 : o.padBottom;
    const vals = (values || []).filter((v) => typeof v === 'number' && isFinite(v));
    if (vals.length < 2) return { line: '', fill: '', pts: [] };
    // min/max may be pinned so two series share one scale and stay comparable.
    const min = o.min == null ? Math.min(...vals) : o.min;
    const max = o.max == null ? Math.max(...vals) : o.max;
    const span = (max - min) || 1;
    const usable = h - padTop - padBottom;
    const n = vals.length;
    const pts = vals.map((v, i) => ({
      x: r1((i / (n - 1)) * w),
      y: r1(padTop + (1 - (v - min) / span) * usable),
      v,
    }));
    const line = o.smooth === false || n < 3
      ? 'M' + pts.map((p) => p.x + ',' + p.y).join(' L')
      : smoothPath(pts);
    const fill = line + ' L' + r1(w) + ',' + r1(h) + ' L0,' + r1(h) + ' Z';
    return { line, fill, pts };
  }

  return { path };
});
