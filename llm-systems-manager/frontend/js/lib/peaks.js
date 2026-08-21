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

// Clamped "Ns/Nm/Nh ago" ladder shared by every peak display.
function agoText(deltaMs) {
  const secs = Math.max(0, Math.floor(deltaMs / 1000));
  if (secs < 60) return secs + 's ago';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
  return Math.floor(secs / 3600) + 'h ago';
}

// Positive offsets beyond this bound are data staleness, not clock skew,
// and must not shift old rows into the live window.
const MAX_ROW_SKEW_MS = 120000;

// History-row ts → caller-clock ms, shifted by the newest-row-to-now
// offset. Stale feeds (large positive offset) keep their real age;
// future-stamped rows (server clock ahead) are pulled back fully.
function rowClock(rows, nowMs) {
  const last = (rows && rows.length)
    ? new Date(rows[rows.length - 1].ts).getTime() : NaN;
  let skew = Number.isFinite(last) ? nowMs - last : 0;
  if (skew > MAX_ROW_SKEW_MS) skew = 0;
  return (ts) => new Date(ts).getTime() + skew;
}

return { makeTracker, agoText, rowClock, MAX_ROW_SKEW_MS };
});
