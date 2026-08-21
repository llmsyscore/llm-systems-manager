// Rolling-window peak trackers for live+peak stat displays (#590).
// Dual-mode: window.LMPeaks in the browser, module export under Node.
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.LMPeaks = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

// One metric's rolling window: push(ts, v) samples, peak(nowMs) → the
// window's max as {v, t} (latest occurrence) or null when empty.
function makeTracker(windowMs) {
  let samples = [];
  let newest = 0;

  function _ms(ts) {
    if (typeof ts === 'number') return Number.isFinite(ts) ? ts : null;
    const t = ts instanceof Date ? ts.getTime() : new Date(ts).getTime();
    return Number.isFinite(t) ? t : null;
  }

  function _prune(now) {
    const cut = now - windowMs;
    let i = 0;
    while (i < samples.length && samples[i].t < cut) i++;
    if (i) samples = samples.slice(i);
  }

  function push(ts, v) {
    const t = _ms(ts);
    if (t == null || typeof v !== 'number' || Number.isNaN(v)) return;
    if (t < newest - windowMs) return;
    samples.push({ t, v });
    if (t < newest) samples.sort((a, b) => a.t - b.t);
    else newest = t;
    _prune(newest);
  }

  function peak(nowMs) {
    const now = _ms(nowMs);
    if (now != null && now > newest) newest = now;
    _prune(newest);
    if (!samples.length) return null;
    let best = samples[0];
    for (const s of samples) if (s.v >= best.v) best = s;
    return { v: best.v, t: best.t };
  }

  function reset() { samples = []; newest = 0; }

  return { push, peak, reset };
}

return { makeTracker };
});
