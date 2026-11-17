// All-hosts page view-model transforms (#1432). Pure functions only — no fetch,
// no DOM. Exposed as window.HV; reads bucket widths and labels from OV.
(function (root, factory) {
  const ov = (root && root.OV) || (typeof require === 'function' ? require('./overall-view') : null);
  const api = factory(ov);
  if (typeof root !== 'undefined') root.HV = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function (OV) {

const METRICS = [
  { key: 'cpu_total', card: 'ah-cpu', title: 'CPU', unit: '%', ymax: 100 },
  { key: 'ram_percent', card: 'ah-ram', title: 'Memory', unit: '%', ymax: 100 },
  { key: 'disk_root_pct', card: 'ah-disk', title: 'Disk / used', unit: '%', ymax: 100 },
  { key: 'net_recv', card: 'ah-net', title: 'Network in', unit: 'B/s' },
  { key: 'io_write', card: 'ah-io', title: 'Disk writes', unit: 'B/s' },
];
const PAGE_FOR = [['llama', 'llamacpp'], ['lms', 'lmstudio'], ['vllm', 'vllm']];
const NOW_WINDOW_MS = 5 * 60000;
const FUTURE_SLACK_MS = 60000;
const BUSIEST_N = 5;
const SLOTS = 5;
const MAX_HOLD = 3;

function _num(v) { return (typeof v === 'number' && Number.isFinite(v)) ? v : null; }
function _arr(v) { return Array.isArray(v) ? v : []; }
function _now(ms) { return (typeof ms === 'number' && Number.isFinite(ms)) ? ms : Date.now(); }
// Epoch seconds, epoch ms or an ISO string → ms; null when unreadable.
function _ms(v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v > 1e12 ? v : v * 1000;
  const t = typeof v === 'string' ? Date.parse(v) : NaN;
  return Number.isFinite(t) ? t : null;
}
function _rowsOf(histById, id) {
  if (!histById) return [];
  return _arr(histById instanceof Map ? histById.get(id) : histById[id]);
}

function shortName(hostname) { return String(hostname || '').replace(/\.local$/, ''); }

// /api/agents/hosts payload (or its hosts list) → host entries with the provider page to link.
function hostList(payload) {
  const list = Array.isArray(payload) ? payload : _arr(payload && payload.hosts);
  return list.filter(h => h && h.agent_id).map(h => {
    const providers = _arr(h.providers);
    const hit = PAGE_FOR.find(([p]) => providers.includes(p));
    return { id: h.agent_id, name: shortName(h.hostname || h.agent_id), hostname: h.hostname || null, providers,
             online: !!h.online, ageS: _num(h.age_s), page: hit ? hit[1] : null };
  });
}

// Newest non-null value of key among rows from the last 5 minutes (at most 60 s ahead); null when none.
function _recent(rows, key, nowMs) {
  let best = null, bestMs = -Infinity;
  for (const r of rows) {
    const t = _ms(r && r.ts), v = _num(r && r[key]);
    if (t == null || v == null || nowMs - t > NOW_WINDOW_MS
        || t - nowMs > FUTURE_SLACK_MS || t < bestMs) continue;
    best = v; bestMs = t;
  }
  return best;
}

function nowRows(hosts, histById, nowMs) {
  const now = _now(nowMs);
  return _arr(hosts).map(h => {
    const rows = _rowsOf(histById, h.id);
    return { id: h.id, name: h.name, online: !!h.online, page: h.page == null ? null : h.page,
             cpu: _recent(rows, 'cpu_total', now), ram: _recent(rows, 'ram_percent', now),
             disk: _recent(rows, 'disk_root_pct', now) };
  });
}

// Runs of at most MAX_HOLD empty buckets take the value before them (the first value at the start); longer runs stay null.
function _holdShortGaps(values) {
  let last = -1;
  for (let i = 0; i < values.length; i++) {
    if (values[i] == null) continue;
    const gap = i - last - 1;
    if (gap > 0 && gap <= MAX_HOLD) for (let j = last + 1; j < i; j++) values[j] = values[last >= 0 ? last : i];
    last = i;
  }
  const tail = values.length - last - 1;
  if (last >= 0 && tail > 0 && tail <= MAX_HOLD) for (let j = last + 1; j < values.length; j++) values[j] = values[last];
  return values;
}

// Range hours Overall knows (6, 12, 24); anything else → 24.
function _rangeHours(rangeH) {
  const h = Number(rangeH);
  return (h === 6 || h === 12 || h === 24) ? h : 24;
}

// Per-host bucket means of key over the range ending at nowMs, with the 5 busiest ids by range mean.
// slotOf(id) gives each host's colour slot; without it slots cycle 1–5 in hosts-list order.
function compare(hosts, histById, key, rangeH, nowMs, slotOf) {
  const hours = _rangeHours(rangeH), b = OV.rangeBucketMs(hours);
  const n = Math.round(hours * 3600000 / b);
  const end = Math.floor(_now(nowMs) / b) * b, start = end - (n - 1) * b;
  const labels = OV.bucketLabels(start, n, b);
  const series = [], meanOf = {};
  for (const h of _arr(hosts)) {
    const sum = new Array(n).fill(0), cnt = new Array(n).fill(0);
    let total = 0, count = 0;
    for (const r of _rowsOf(histById, h.id)) {
      const t = _ms(r && r.ts), v = _num(r && r[key]);
      if (t == null || v == null) continue;
      const i = Math.floor((t - start) / b);
      if (i < 0 || i >= n) continue;
      sum[i] += v; cnt[i] += 1; total += v; count += 1;
    }
    if (!count) continue;
    meanOf[h.id] = total / count;
    series.push({ name: h.name, id: h.id, values: _holdShortGaps(sum.map((s, i) => cnt[i] ? s / cnt[i] : null)), slot: 0 });
  }
  const busiest = series.map(s => s.id).sort((x, y) => meanOf[y] - meanOf[x]).slice(0, BUSIEST_N);
  const order = _arr(hosts).map(h => h.id);
  for (const s of series) {
    const given = typeof slotOf === 'function' ? Number(slotOf(s.id)) : NaN;
    s.slot = given >= 1 && given <= SLOTS ? Math.floor(given) : (order.indexOf(s.id) % SLOTS) + 1;
  }
  return { labels, series, busiest };
}

// Rows of old and fresh keyed by ts (fresh wins), ascending, without rows before cutoffMs.
function mergeRows(old, fresh, cutoffMs) {
  const cut = _num(cutoffMs);
  const byMs = new Map();
  for (const r of _arr(old).concat(_arr(fresh))) {
    const t = _ms(r && r.ts);
    if (t == null || (cut != null && t < cut)) continue;
    byMs.set(t, r);
  }
  return Array.from(byMs.keys()).sort((a, b) => a - b).map(t => byMs.get(t));
}

function fmtPct(v) {
  if (_num(v) == null) return '—';
  const r = Math.round(v * 10) / 10;
  return (Math.abs(r) < 10 ? r.toFixed(1) : String(Math.round(v))) + '%';
}
function fmtRate(v) {
  if (_num(v) == null) return '—';
  const u = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
  let i = 0, r = Math.round(v);
  while (Math.abs(r) >= 1024 && i < u.length - 1) { v /= 1024; i++; r = Math.round(v * 10) / 10; }
  return (i ? r.toFixed(1) : String(r)) + ' ' + u[i];
}

return { METRICS, shortName, hostList, nowRows, compare, mergeRows, fmtPct, fmtRate };
});
