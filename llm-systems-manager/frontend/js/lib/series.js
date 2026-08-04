// Pure data helpers shared by the chart/dashboard scripts and the frontend
// unit tests. Classic <script> in the browser (window.LMSeries), CommonJS under Node/Vitest.

// Align series points into a sorted [ts, values[]] list; absent slots stay null.
function zipByTs(seriesPoints) {
  const map = new Map();
  seriesPoints.forEach((pts, idx) => {
    for (const p of pts) {
      const ts = p.timestamp || p.ts;
      if (!ts) continue;
      if (!map.has(ts)) map.set(ts, new Array(seriesPoints.length).fill(null));
      map.get(ts)[idx] = p.value;
    }
  });
  return [...map.entries()].sort(([a], [b]) => new Date(a) - new Date(b));
}

// Snap a timestamp down to the poll-interval grid; interval <= 0 keeps full resolution.
function bucketDate(ts, interval) {
  const ms = new Date(ts).getTime();
  const w = (typeof interval === 'number' && interval > 0) ? interval : 0;
  return w ? new Date(Math.floor(ms / w) * w) : new Date(ms);
}

// True only when the Dashboard -> Manager sub-tab is the active view.
function isManagerSubActive(activeTab, subTabState) {
  return activeTab === 'dashboard'
    && !!subTabState && subTabState['dashboard'] === 'manager';
}

// Latch true once rows arrive; an empty/absent result leaves prev unchanged.
function latchFilled(prev, rows) {
  return prev || (Array.isArray(rows) && rows.length > 0);
}

// Follow a cumulative refusal counter across polls; `recent` is true only when
// it grew within windowMs. Counter resets and absent samples never flag recent.
function trackRefusals(prev, count, nowMs, windowMs = 60000) {
  const p = prev || { count: null, lastIncreaseMs: null };
  const n = (typeof count === 'number' && isFinite(count)) ? count : null;
  let last = p.lastIncreaseMs;
  if (n !== null && p.count !== null && n > p.count) last = nowMs;
  return {
    count: n !== null ? n : p.count,
    lastIncreaseMs: last,
    recent: last !== null && (nowMs - last) <= windowMs,
  };
}

if (typeof window !== 'undefined')
  window.LMSeries = { zipByTs, bucketDate, isManagerSubActive, latchFilled, trackRefusals };
if (typeof module !== 'undefined' && module.exports)
  module.exports = { zipByTs, bucketDate, isManagerSubActive, latchFilled, trackRefusals };
