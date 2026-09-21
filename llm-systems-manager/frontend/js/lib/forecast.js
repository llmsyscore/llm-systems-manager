// Forecast display helpers (#1031); page wiring is in js/forecast.js.
// Pure and DOM-free, IIFE-scoped, exposed as window.FC.
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.FC = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

const SEV = {
  critical: { text: 'Critical', short: 'crit', cls: 'crit', icon: '▲' },
  warning: { text: 'Warning', short: 'warn', cls: 'warn', icon: '●' },
  info: { text: 'Info', short: 'info', cls: 'info', icon: '' },
};
const SEV_RANK = { critical: 0, warning: 1, info: 2 };
const VERIFIED = { code: 'Code', 'tower+code': 'Tower + code', model: 'Not verified' };
const CHECK_STATE = {
  failed: { cls: 'failed', note: 'could not run' },
  off: { cls: 'off', note: '' },
  pending: { cls: 'pending', note: 'not run yet' },
};
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const CAUSE_MARK = 'Likely cause: ';
const HOST_LAST = '￿';

// API lists and rows arrive unvalidated; anything else degrades to empty.
const isObj = v => !!v && typeof v === 'object' && !Array.isArray(v);
const list = v => (Array.isArray(v) ? v : []);
// NaN for null, '', booleans and objects, which Number() would coerce to 0 or 1.
const toNum = v => (typeof v === 'number' ? v
  : (typeof v === 'string' && v.trim() ? Number(v) : NaN));

function sevLabel(sev) {
  return { ...(SEV[String(sev || '').toLowerCase()] || SEV.info) };
}

function verifiedLabel(v) {
  return VERIFIED[String(v || '').toLowerCase()] || 'Not verified';
}

// Severity, then soonest prediction (nulls last), then host (nulls last).
function sortFindings(rows) {
  const key = r => [
    SEV_RANK[String(r.severity || '').toLowerCase()] ?? 3,
    r.predicted_at == null ? Infinity : Number(r.predicted_at),
    r.host || HOST_LAST,
  ];
  return list(rows).filter(isObj).sort((a, b) => {
    const ka = key(a), kb = key(b);
    if (ka[0] !== kb[0]) return ka[0] - kb[0];
    if (ka[1] !== kb[1]) return ka[1] - kb[1];
    return ka[2] < kb[2] ? -1 : (ka[2] > kb[2] ? 1 : 0);
  });
}

function topThree(rows) {
  return sortFindings(rows).slice(0, 3);
}

// Overall strip rows: critical and warning findings only, most urgent first.
function urgent(rows) {
  return sortFindings(rows).filter(r => ['critical', 'warning'].includes(String(r.severity || '').toLowerCase()));
}

// First sentence of Tower's digest, cut at a word boundary to `max` characters.
function shortDigest(text, max) {
  const cap = Number(max) > 0 ? Number(max) : 160;
  const t = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
  if (!t) return '';
  const stop = t.search(/[.!?](\s|$)/);
  const first = stop >= 0 ? t.slice(0, stop + 1) : t;
  if (first.length <= cap) return first;
  const cut = first.slice(0, cap - 1);
  return (cut.includes(' ') ? cut.slice(0, cut.lastIndexOf(' ')) : cut) + '…';
}

// Header count for the Overall card and the sub-tab.
function countLabel(view) {
  const v = isObj(view) ? view : {};
  if (!v.enabled) return 'off';
  const n = list(v.findings).filter(isObj).length;
  return n ? n + ' ahead' : 'all clear';
}

// ── list view: facets, filter, sort, pages ──
const CONF_RANK = { high: 0, medium: 1, low: 2 };
const sevOf = r => (String(r.severity || '').toLowerCase() in SEV ? String(r.severity).toLowerCase() : 'info');

// Hosts, check titles and per-severity counts present in a set of findings.
function facets(rows) {
  const hosts = new Set(), checks = new Set(), sev = { critical: 0, warning: 0, info: 0 };
  list(rows).filter(isObj).forEach(r => {
    if (r.host) hosts.add(String(r.host));
    if (r.title) checks.add(String(r.title));
    sev[sevOf(r)] += 1;
  });
  return { hosts: [...hosts].sort(), checks: [...checks].sort(), sev };
}

// Keeps findings matching every set filter: severities (any of), host, check title, free text.
function filterFindings(rows, f) {
  const o = isObj(f) ? f : {};
  const sevs = list(o.sev).map(x => String(x).toLowerCase());
  const q = String(o.q || '').trim().toLowerCase();
  return list(rows).filter(isObj).filter(r =>
    (!sevs.length || sevs.includes(sevOf(r)))
    && (!o.host || r.host === o.host)
    && (!o.check || r.title === o.check)
    && (!q || [r.summary, r.host, r.title, r.detail].some(t => String(t || '').toLowerCase().includes(q))));
}

// Sort keys: urgency (the default order), when, host, conf; dir 'desc' reverses.
function sortBy(rows, key, dir) {
  const base = sortFindings(rows);
  const when = r => (r.predicted_at == null ? Infinity : Number(r.predicted_at));
  const by = { when: (a, b) => when(a) - when(b),
               host: (a, b) => String(a.host || HOST_LAST).localeCompare(String(b.host || HOST_LAST)),
               conf: (a, b) => (CONF_RANK[String(a.confidence || '').toLowerCase()] ?? 3)
                 - (CONF_RANK[String(b.confidence || '').toLowerCase()] ?? 3) }[key];
  const out = by ? base.slice().sort(by) : base;
  return dir === 'desc' ? out.reverse() : out;
}

// One page of rows plus the numbers a pager shows; the page is clamped into range.
function paginate(rows, page, per) {
  const all = list(rows), size = Number(per) > 0 ? Math.floor(Number(per)) : 10;
  const pages = Math.max(1, Math.ceil(all.length / size));
  const at = Math.min(pages, Math.max(1, Math.floor(Number(page)) || 1));
  const from = all.length ? (at - 1) * size + 1 : 0;
  return { rows: all.slice((at - 1) * size, at * size), page: at, pages, from,
           to: Math.min(all.length, at * size), total: all.length };
}

// "In 2.2 days" for a dated prediction, "Overdue" once passed, "Ongoing" without a date.
function whenText(f, nowMs) {
  const at = toNum((f || {}).predicted_at);
  if (!Number.isFinite(at)) return 'Ongoing';
  const days = (at * 1000 - nowMs) / 86400000;
  if (days <= 0) return 'Overdue';
  if (days < 1) return 'In ' + Math.max(1, Math.round(days * 24)) + ' hours';
  return 'In ' + (days < 3 ? num1(days) : String(Math.round(days))) + ' days';
}

// Horizon line: dated findings placed in days from now (capped at `span`), and the undated count.
function horizon(rows, nowMs, span) {
  const cap = Number(span) > 0 ? Number(span) : 30, dated = [];
  let ongoing = 0;
  sortFindings(rows).forEach(r => {
    const at = toNum(r.predicted_at);
    if (!Number.isFinite(at)) { ongoing += 1; return; }
    const days = Math.max(0, (at * 1000 - nowMs) / 86400000);
    dated.push({ id: r.id, host: r.host || '', summary: r.summary || '', sev: sevOf(r),
                 days: Math.min(days, cap), beyond: days > cap });
  });
  return { dated, ongoing, span: cap };
}

// Findings bucketed for the briefing view: by 'host', by 'check' (its title), or one unnamed group.
function groupRows(rows, by) {
  const out = new Map();
  list(rows).filter(isObj).forEach(r => {
    const key = by === 'host' ? (r.host || 'All hosts') : (by === 'check' ? (r.title || 'Other') : '');
    if (!out.has(key)) out.set(key, []);
    out.get(key).push(r);
  });
  return [...out].map(([name, items]) => ({ name, rows: items, sev: facets(items).sev }));
}

// Cleared and dismissed rows apart, newest first as the API sends them.
function resolvedTabs(rows) {
  const all = list(rows).filter(isObj);
  return { cleared: all.filter(r => r.status !== 'dismissed'), dismissed: all.filter(r => r.status === 'dismissed') };
}

// Where a finding's next step happens: button label, tab, sub-tab, and whether it needs admin access.
const GO = {
  disk_fill: ['Open model maintenance', 'admin', 'routing', true, 'adminStoresCard'],
  load_shift: ['Open LLM Control', 'llm', '', false],
  memory_headroom: ['Open LLM Control', 'llm', '', false],
  thermal_trend: ['Open LLM Control', 'llm', '', false],
  capacity: ['Open Autopilot', 'admin', 'routing', true, 'apEntriesCard'],
  power_cost: ['Open Energy', 'dashboard', 'energy', false],
  idle_waste: ['Open Energy', 'dashboard', 'energy', false],
  throughput: ['Open Benchmark', 'llm', 'tools', false],
  bench_outcomes: ['Open Benchmark', 'llm', 'tools', false],
  model_errors: ['Open Gateway', 'admin', 'routing', true],
  slot_pressure: ['Open Autopilot', 'admin', 'routing', true, 'apEntriesCard'],
  model_churn: ['Open Autopilot', 'admin', 'routing', true, 'apEntriesCard'],
  alarm_patterns: ['Open Events', 'events', '', false],
  agent_health: ['Open Agents', 'admin', 'agents', true],
  service_health: ['Open Manager dashboard', 'dashboard', 'manager', false],
};
function goTarget(f, admin) {
  const row = isObj(f) ? f : {};
  let t = GO[String(row.check || '')];
  if (row.check === 'slot_pressure' && row.subject === 'streams') t = GO.service_health;
  if (!t || (t[3] && !admin)) return null;
  return { label: t[0], tab: t[1], sub: t[2], anchor: t[4] || '', host: row.host || '' };
}

// The question Ask Tower sends for one finding, and for a whole run.
function askText(f) {
  const r = isObj(f) ? f : {};
  const split = splitCause(r.detail);
  return ['Help me with this Forecast finding' + (r.host ? ' on ' + r.host : '') + ': '
          + String(r.summary || '').replace(/[.\s]+$/, '') + '.',
          split.detail, r.suggested_action ? 'Forecast suggests: ' + r.suggested_action : '',
          'What should I check first, and how do I confirm the cause?'].filter(Boolean).join(' ');
}
function askRunText(view) {
  const n = list((isObj(view) ? view : {}).findings).filter(isObj).length;
  return 'Walk me through the latest Forecast run: ' + n + (n === 1 ? ' finding is' : ' findings are')
    + ' open. Which should I deal with first, and why?';
}

// Summary tiles: open findings per severity and the soonest dated prediction.
function summary(view) {
  const v = isObj(view) ? view : {};
  const out = { critical: 0, warning: 0, info: 0, next: null };
  if (!v.enabled) return out;
  list(v.findings).filter(isObj).forEach(r => {
    const sev = String(r.severity || '').toLowerCase();
    out[sev in SEV ? sev : 'info'] += 1;
    const at = toNum(r.predicted_at);
    if (Number.isFinite(at) && (!out.next || at < toNum(out.next.predicted_at))) out.next = r;
  });
  return out;
}

// Trims a trailing .0 so whole days read as "3", not "3.0".
function num1(v) {
  const r = Math.round(Number(v || 0) * 10) / 10;
  return Number.isInteger(r) ? String(r) : r.toFixed(1);
}

function checkState(c) {
  const st = String((c || {}).state || '').toLowerCase();
  if (st === 'ok') {
    const found = Number((c || {}).found || 0);
    return { cls: 'ok', note: found > 0 ? String(found) : '' };
  }
  if (st === 'collecting') {
    return { cls: 'collect',
             note: num1((c || {}).have_days) + ' of ' + num1((c || {}).min_days) + ' days' };
  }
  return { ...(CHECK_STATE[st] || { cls: 'pending', note: '' }) };
}

function hhmm(d) {
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

// Whole-day distance in the browser's local zone.
function dayGap(d, now) {
  const a = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const b = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return Math.round((a - b) / 86400000);
}

// Epoch-seconds timestamp to a local Date; null for anything non-finite.
function tsDate(ts) {
  const n = toNum(ts);
  return Number.isFinite(n) ? new Date(n * 1000) : null;
}

// ts is epoch seconds, nowMs epoch milliseconds.
function whenLabel(ts, nowMs) {
  const d = tsDate(ts);
  if (!d) return '';
  const gap = dayGap(d, new Date(nowMs));
  if (gap === 0) return 'Today ' + hhmm(d);
  if (gap === 1) return 'Tomorrow ' + hhmm(d);
  if (gap === -1) return 'Yesterday ' + hhmm(d);
  return MONTHS[d.getMonth()] + ' ' + d.getDate() + ' ' + hhmm(d);
}

function dateLabel(ts) {
  const d = tsDate(ts);
  return d ? MONTHS[d.getMonth()] + ' ' + d.getDate() : '';
}

// Signed one-decimal rate with its unit, e.g. "+18.6 GB/day".
function rateLabel(rate, unit) {
  const v = Number(rate);
  if (rate == null || !Number.isFinite(v) || Math.abs(v) < 0.05) return '';
  const txt = (v < 0 ? '-' : '+') + Math.abs(v).toFixed(1);
  return unit ? txt + ' ' + unit : txt;
}

// Splits a model-written cause sentence off the end of the detail text.
function splitCause(detail) {
  const text = String(detail == null ? '' : detail);
  const at = text.lastIndexOf(CAUSE_MARK);
  if (at < 0) return { detail: text.trim(), cause: null };
  const cause = text.slice(at + CAUSE_MARK.length).trim();
  return { detail: text.slice(0, at).trim(), cause: cause || null };
}

function towerLine(tower) {
  if (!tower) return 'Off';
  return tower.reason || 'On';
}

function digestNote(tower) {
  const model = (tower || {}).model;
  return model ? 'written by ' + model + ' · figures always come from code' : '';
}

// Fact pairs for an expanded finding; a null or unformattable value drops its row.
function facts(f, nowMs) {
  const r = isObj(f) ? f : {};
  const pairs = [
    ['Since', dateLabel(r.since)],
    ['Rate', rateLabel(r.rate, r.unit)],
    ['Predicted', whenLabel(r.predicted_at, nowMs) || (r.summary ? 'No date, ongoing' : '')],
    ['Confidence', r.confidence
      ? String(r.confidence).charAt(0).toUpperCase() + String(r.confidence).slice(1) : ''],
    ['Checked by', r.verified ? verifiedLabel(r.verified) : ''],
  ];
  return pairs.filter(p => p[1]);
}

function clearedLabel(row) {
  const r = isObj(row) ? row : {};
  if (!r.status) return '';
  if (r.status === 'dismissed') {
    return r.dismissed_by ? 'dismissed by ' + r.dismissed_by : 'dismissed';
  }
  const when = dateLabel(r.resolved);
  return when ? 'cleared ' + when : 'cleared';
}

// Two-point projection line from now to graph.end, clipped at the threshold.
function fitPoints(graph, nowMs) {
  const g = isObj(graph) ? graph : {};
  const fit = isObj(g.fit) ? g.fit : null;
  if (!fit || g.end == null) return [];
  const now = (nowMs == null ? Date.now() : nowMs) / 1000;
  const end = toNum(g.end), slope = toNum(fit.slope_per_s), icpt = toNum(fit.intercept);
  if (!Number.isFinite(end) || !Number.isFinite(slope) || !Number.isFinite(icpt)) return [];
  if (end <= now) return [];
  const start = toNum(g.start);
  const t0 = Number.isFinite(start) ? Math.max(now, start) : now;
  if (end <= t0) return [];
  const at = t => icpt + slope * t;
  let t1 = end, v1 = at(end);
  if (g.threshold != null && slope !== 0 && g.limit !== false) {
    const cross = (Number(g.threshold) - icpt) / slope;
    if (cross <= t0) return [];
    if (cross < end) { t1 = cross; v1 = Number(g.threshold); }
  }
  return [[Math.round(t0 * 1000), at(t0)], [Math.round(t1 * 1000), v1]];
}

// Axis bounds over the measured points, the projection and the threshold.
function chartScale(points, fit, threshold) {
  const all = list(points).concat(list(fit))
    .filter(p => Array.isArray(p) && Number.isFinite(toNum(p[0])) && Number.isFinite(toNum(p[1])));
  if (!all.length) return null;
  const xs = all.map(p => toNum(p[0])), ys = all.map(p => toNum(p[1]));
  if (Number.isFinite(toNum(threshold))) ys.push(toNum(threshold));
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const pad = (hi - lo) * 0.05 || Math.abs(hi) * 0.05 || 1;
  let yMin = lo - pad;
  if (lo >= 0 && yMin < 0) yMin = 0;
  return { xMin: Math.min(...xs), xMax: Math.max(...xs), yMin, yMax: hi + pad };
}

return { sevLabel, verifiedLabel, sortFindings, topThree, urgent, shortDigest, countLabel, summary, checkState,
         facets, filterFindings, sortBy, paginate, whenText, horizon, groupRows, resolvedTabs, goTarget, askText, askRunText,
         whenLabel, dateLabel, rateLabel, splitCause, towerLine, digestNote,
         facts, clearedLabel, fitPoints, chartScale, SEV, VERIFIED, MONTHS };
});
