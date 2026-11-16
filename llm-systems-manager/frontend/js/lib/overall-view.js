// Overall-page view-model transforms (#565, #1432); page wiring is in
// js/overall.js. Pure functions only — no fetch, no DOM. Exposed as window.OV.
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.OV = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

const PROVIDER_LABEL = { llama: 'llama.cpp', lms: 'LM Studio', vllm: 'vLLM' };

function _num(v) { return (typeof v === 'number' && Number.isFinite(v)) ? v : null; }

function _gpuWatts(agg) {
  if (!agg) return 0;
  const w = _num(agg.gpu && agg.gpu.total_power_watts);
  return w != null ? w : (_num(agg.total_gpu_power_watts) || 0);
}

// Fleet watts summed once per agent_id across provider rollups (a host
// serving two providers appears in both); provider totals as fallback.
function fleetWatts(aggs) {
  const seen = new Set();
  let total = 0, rowsCarryPower = false;
  for (const a of aggs) {
    for (const r of (a && Array.isArray(a.agents)) ? a.agents : []) {
      if (!r || !r.online) continue;
      if (r.power_watts !== undefined) rowsCarryPower = true;
      const w = _num(r.power_watts);
      if (w == null || seen.has(r.agent_id)) continue;
      seen.add(r.agent_id);
      total += w;
    }
  }
  return rowsCarryPower ? total : aggs.reduce((s, a) => s + _gpuWatts(a), 0);
}

// 1234567 → "1.2M", 89012 → "89.0k"; integers below 1k stay plain.
function fmtShort(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return null;
  if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + 'k';
  return String(Math.round(v));
}

// Loaded-model extras (#593): " · 124.2k ctx · 89.0k prompt · 1.2M gen",
// decimal token counts, each part present only when its field is reported.
function _modelExtras(row) {
  const parts = [];
  const ctx = fmtShort(row.ctx);
  if (ctx != null) parts.push(`${ctx} ctx`);
  const prompt = fmtShort(row.total_tokens_prompted);
  if (prompt != null) parts.push(`${prompt} prompt`);
  const gen = fmtShort(row.total_tokens_generated);
  if (gen != null) parts.push(`${gen} gen`);
  return parts.length ? ` · ${parts.join(' · ')}` : '';
}

function _agentDetail(prov, row) {
  if (prov === 'llama') {
    const state = row.state || 'unknown';
    return row.model ? `${state} · ${row.model}${_modelExtras(row)}` : state;
  }
  if (prov === 'lms') {
    if (row.ps_error) return 'lms ps unreadable';
    if (!row.server_on) return 'server off';
    const state = (row.busy_process_count || 0) > 0 ? 'busy' : 'idle';
    const count = row.loaded_model_count || 0;
    const names = (row.loaded_models || []).slice(0, 3);
    if (!names.length) return `${state} · ${count} model${count === 1 ? '' : 's'}`;
    // +N covers models beyond the display cap or without a reported name.
    const extra = Math.max(0, count - names.length);
    return `${state} · ${names.join(' · ')}` + (extra ? ` +${extra}` : '')
      + _modelExtras(row);
  }
  const req = _num(row.requests_running);
  if (!row.server_on) return 'server off';
  const base = `${req != null ? req : 0} req`;
  return row.model ? `${base} · ${row.model}${_modelExtras(row)}` : base;
}

// Aggregates' per-agent rows joined with list-by-provider hostnames →
// one row per distinct agent, hostname asc, offline last.
function agentRows(aggs, byProvider) {
  const hostById = {};
  Object.values(byProvider || {}).forEach(list => (list || []).forEach(a => {
    if (a && a.agent_id) hostById[a.agent_id] = a.hostname || null;
  }));
  const provs = ['llama', 'lms', 'vllm'];
  const byAgent = {};
  (aggs || []).forEach((agg, i) => {
    const prov = provs[i];
    ((agg && agg.agents) || []).forEach(row => {
      const id = row.agent_id;
      if (!id) return;
      const rec = byAgent[id] = byAgent[id] || {
        agentId: id,
        hostname: hostById[id] || (id.slice(0, 8) + '…'),
        online: false, ageS: null, provs: [],
      };
      rec.online = rec.online || !!row.online;
      const age = _num(row.age_s);
      if (age != null && (rec.ageS == null || age < rec.ageS)) rec.ageS = age;
      rec.provs.push({ prov, detail: _agentDetail(prov, row) });
    });
  });
  return Object.values(byAgent).sort((a, b) =>
    (a.online === b.online ? 0 : a.online ? -1 : 1)
    || a.hostname.localeCompare(b.hostname));
}

// Active-alert rows → strip summary: counts, worst severity, newest 3.
function alertsSummary(alerts) {
  const counts = { critical: 0, warning: 0, info: 0 };
  const rows = Array.isArray(alerts) ? alerts : [];
  rows.forEach(a => {
    if (a && counts[a.severity] != null) counts[a.severity]++;
  });
  const worst = counts.critical ? 'critical'
    : counts.warning ? 'warning'
    : counts.info ? 'info' : null;
  const newest = rows.slice()
    .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
    .slice(0, 3)
    .map(a => ({ id: a.alert_id, rule: a.rule_name, severity: a.severity, ts: a.created_at }));
  return { total: rows.length, counts, worst, newest };
}

// ---- Overall on the kit (#1432 phase 4b) ----

const PROVS = ['llama', 'lms', 'vllm'];
// Range hours → bucket width: 6 h → 1 min, 12 h and 24 h → 5 min.
const RANGE_BUCKET_MS = { 6: 60000, 12: 300000, 24: 300000 };
const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function _obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }
function _arr(v) { return Array.isArray(v) ? v : []; }
function _rnd(v, dp) { const f = Math.pow(10, dp); return Math.round(v * f) / f; }
function _now(ms) { return (typeof ms === 'number' && Number.isFinite(ms)) ? ms : Date.now(); }
function _pad2(n) { return (n < 10 ? '0' : '') + n; }
function _cap(s) { s = String(s || '').toLowerCase(); return s ? s[0].toUpperCase() + s.slice(1) : ''; }
function _short(h) { return String(h || '').replace(/\.local$/, ''); }
function _tail(id) { return String(id || '').split('/').pop(); }
function _plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }
// The first sentence of a text on its first line, else that whole first line.
function _firstSentence(t) {
  const s = String(t || ''), m = /^(.*?[.!?])(\s|$)/.exec(s);
  return m ? m[1] : s.split(/\r?\n/)[0];
}
// Sort order for times, newest first; a missing time sorts last.
function _byNewest(a, b) {
  const x = a == null ? -Infinity : a, y = b == null ? -Infinity : b;
  return y > x ? 1 : y < x ? -1 : 0;
}
// Epoch seconds, epoch ms or an ISO string → ms; null when unreadable.
function _ms(v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v > 1e12 ? v : v * 1000;
  const t = typeof v === 'string' ? Date.parse(v) : NaN;
  return Number.isFinite(t) ? t : null;
}
function _fmtBytes(v) {
  if (_num(v) == null) return '–';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (v >= 1024 && i < 4) { v /= 1024; i++; }
  return v.toFixed(v < 10 && i ? 1 : 0) + ' ' + u[i];
}
// A Set or array of ids (empty included) narrows; anything else → null (no narrowing).
function _idSet(ids) {
  if (ids instanceof Set) return ids;
  return Array.isArray(ids) ? new Set(ids) : null;
}
// True when no narrowing is set or any of the keys (agent id, hostname) is in ids.
function _inIds(ids, ...keys) {
  return !ids || keys.some(k => k != null && k !== '' && ids.has(k));
}
// {llama, lms, vllm} or [llama, lms, vllm] → the three aggregates in that order.
function _aggList(aggs) {
  if (Array.isArray(aggs)) return [aggs[0] || null, aggs[1] || null, aggs[2] || null];
  const a = _obj(aggs);
  return [a.llama || null, a.lms || null, a.vllm || null];
}
function _agentsOf(agg) { return _arr(agg && agg.agents); }
function _hostMap(byProvider) {
  const out = {};
  Object.values(_obj(byProvider)).forEach(list => _arr(list).forEach(a => {
    if (a && a.agent_id && a.hostname) out[a.agent_id] = a.hostname;
  }));
  return out;
}
// Hourly payload ({rows, end_ts}) or a bare row list → rows sorted by hour_ts.
function _hourRows(hourly) {
  const rows = Array.isArray(hourly) ? hourly : _arr(_obj(hourly).rows);
  return rows.filter(r => r && typeof r.hour_ts === 'number' && Number.isFinite(r.hour_ts))
    .slice().sort((a, b) => a.hour_ts - b.hour_ts);
}
function _sumKey(rows, k) {
  const nums = rows.map(r => _num(r && r[k])).filter(v => v != null);
  return nums.length ? nums.reduce((a, b) => a + b, 0) : null;
}

function _rangeH(h) { const n = Number(h); return RANGE_BUCKET_MS[n] ? n : 24; }
function rangeBucketMs(rangeH) { return RANGE_BUCKET_MS[_rangeH(rangeH)]; }
function _keepFor(rangeH) { const h = _rangeH(rangeH); return h * 3600000 / RANGE_BUCKET_MS[h]; }

// Epoch ms → local 'HH:MM'; '' when unreadable.
function bucketLabel(ms) {
  const d = new Date(typeof ms === 'number' ? ms : NaN);
  if (!Number.isFinite(d.getTime())) return '';
  return _pad2(d.getHours()) + ':' + _pad2(d.getMinutes());
}

// Labels of n buckets from startMs: bucketLabel, plus a trailing ′ when that wall time already
// fell in an earlier bucket of the same window (clock changes); one pass, only when the offset moves.
function bucketLabels(startMs, n, bucketMs) {
  const out = [];
  if (typeof startMs !== 'number' || !Number.isFinite(startMs) || typeof n !== 'number'
      || !Number.isFinite(n) || n < 1 || typeof bucketMs !== 'number' || !(bucketMs > 0)) return out;
  n = Math.min(Math.floor(n), 100000);
  const shifted = new Date(startMs).getTimezoneOffset()
    !== new Date(startMs + (n - 1) * bucketMs).getTimezoneOffset();
  const seen = new Set();
  for (let i = 0; i < n; i++) {
    const base = bucketLabel(startMs + i * bucketMs);
    out.push(shifted && seen.has(base) ? base + '′' : base);
    if (shifted) seen.add(base);
  }
  return out;
}
// Label of the bucket at ms as the last bucket of a range window ending there.
function rangeLabel(ms, rangeH) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return '';
  const b = rangeBucketMs(rangeH), keep = _keepFor(rangeH), start = ms - (keep - 1) * b;
  if (new Date(start).getTimezoneOffset() === new Date(ms).getTimezoneOffset()) return bucketLabel(ms);
  return bucketLabels(start, keep, b)[keep - 1];
}

function _shownProvs(prov) { return PROVS.includes(prov) ? [prov] : PROVS; }

// Agent ids and hostnames of the hosts running prov; null for 'all' (no narrowing).
function hostIds(byProvider, prov) {
  if (!PROVS.includes(prov)) return null;
  const out = new Set();
  _arr(_obj(byProvider)[prov]).forEach(a => {
    if (a && a.agent_id) out.add(a.agent_id);
    if (a && a.hostname) out.add(a.hostname);
  });
  return out;
}

// Prompt series name: 'Prompt (all)' for All, else 'Prompt (<provider label>)'.
function promptName(prov) { return PROVS.includes(prov) ? 'Prompt (' + PROVIDER_LABEL[prov] + ')' : 'Prompt (all)'; }
function _isPrompt(name) { return typeof name === 'string' && name.indexOf('Prompt (') === 0; }
// Fleet watts of one history row: the larger of wall power (psu_in) and GPU power when psu_in is reported.
function _rowPower(r) {
  const psu = _num(r.psu_in), gpu = _num(r.gpu_power) || 0;
  return psu != null ? Math.max(psu, gpu) : gpu;
}

// History rows → keep buckets ending with the one holding nowMs: per-provider gen t/s
// peaks, promptName(prov) as the peak of the shown providers' prompt sum, power as fleet watt peaks.
function throughput(rows, rangeH, prov, nowMs) {
  const b = rangeBucketMs(rangeH), keep = _keepFor(rangeH), shown = _shownProvs(prov);
  const start = Math.floor(_now(nowMs) / b) * b - (keep - 1) * b;
  const labels = bucketLabels(start, keep, b);
  const gen = shown.map(() => new Array(keep).fill(0));
  const prompt = new Array(keep).fill(0), power = new Array(keep).fill(0);
  _arr(rows).forEach(r => {
    const t = r && typeof r.ts === 'string' ? Date.parse(r.ts) : NaN;
    const i = Math.floor((t - start) / b);
    if (!(i >= 0 && i < keep)) return;
    shown.forEach((p, k) => { gen[k][i] = Math.max(gen[k][i], _num(r[p + '_tps']) || 0); });
    prompt[i] = Math.max(prompt[i], shown.reduce((s, p) => s + (_num(r[p + '_pps']) || 0), 0));
    power[i] = Math.max(power[i], _rowPower(r));
  });
  const series = shown.map((p, k) => ({ name: PROVIDER_LABEL[p], values: gen[k] }))
    .concat([{ name: promptName(prov), values: prompt }]);
  return { labels, series, keep, power };
}

// One live tick's values in throughput's series order, from the aggregates.
function _tickValues(aggs, prov) {
  const shown = _shownProvs(prov), list = _aggList(aggs);
  const tp = p => _obj(_obj(list[PROVS.indexOf(p)]).throughput);
  return shown.map(p => _num(tp(p).total_tps) || 0)
    .concat([shown.reduce((s, p) => s + (_num(tp(p).total_pps) || 0), 0)]);
}

// Folds a tick into state {key: bucket start ms, values}, keeping the peak; update is the current bucket.
// On a new bucket append lists the finished one, then zero buckets for the skipped ones (gap counts them).
function liveBucket(state, nowMs, rangeH, aggs, prov) {
  const b = rangeBucketMs(rangeH), keep = _keepFor(rangeH), key = Math.floor(_now(nowMs) / b) * b;
  const vals = _tickValues(aggs, prov);
  const s = _obj(state);
  const prev = (typeof s.key === 'number' && Number.isFinite(s.key) && Array.isArray(s.values)
    && s.values.length === vals.length) ? s : null;
  let values = vals, append = null, gap = 0;
  if (prev && prev.key === key) values = vals.map((v, i) => Math.max(v, _num(prev.values[i]) || 0));
  else if (prev && prev.key < key) {
    append = [{ label: rangeLabel(prev.key, rangeH), values: prev.values.map(v => _num(v) || 0) }];
    gap = Math.round((key - prev.key) / b) - 1;
    const winStart = key - (keep - 1) * b;
    let win = null;
    for (let k = Math.max(prev.key + b, key - keep * b); k < key; k += b) {
      if (k >= winStart) win = win || bucketLabels(winStart, keep, b);
      append.push({ label: k >= winStart ? win[(k - winStart) / b] : rangeLabel(k, rangeH), values: vals.map(() => 0) });
    }
  }
  return { state: { key, values }, append, gap,
           update: { label: rangeLabel(key, rangeH), values: values.slice() } };
}

// kpiBody delta: percent change of cur over prev; null without a usable prev.
function _delta(cur, prev, upGood) {
  if (_num(cur) == null || !_num(prev)) return null;
  const pct = (cur - prev) / prev * 100;
  const dir = pct > 0.5 ? 'up' : pct < -0.5 ? 'down' : 'flat';
  const tone = dir === 'flat' ? undefined : ((dir === 'up') === upGood ? 'good' : 'bad');
  return { dir, text: Math.round(Math.abs(pct)) + '%', tone };
}

function _summaryHosts(summary, ids) {
  return _arr(_obj(summary).hosts).filter(h => h && _inIds(ids, h.agent_id, h.hostname));
}
// A summary total, or under a host narrowing the sum over those hosts; null when unreported.
function _summaryVal(summary, ids, k) {
  const s = _obj(summary);
  if (!ids) return _num(_obj(s.totals)[k]);
  return _sumKey(_summaryHosts(s, ids), k);
}
function _summaryKwh(summary, ids) {
  const t = _obj(_obj(summary).totals);
  if (!ids && t.has_power === false) return null;
  return _summaryVal(summary, ids, 'kwh');
}
function _savings(summary, ids) {
  const s = _obj(summary);
  if (!ids) return _num(s.savings_usd);
  const hosts = _summaryHosts(s, ids).filter(h => _num(h.cloud_cost_usd) != null);
  return hosts.length
    ? hosts.reduce((a, h) => a + h.cloud_cost_usd - (_num(h.cost_usd) || 0), 0) : null;
}
// Hourly rows split into the last 24 h and the 24 h before, by the payload's end_ts.
function _hourHalves(hourly) {
  const rows = _hourRows(hourly);
  if (!rows.length) return { cur: [], prev: [] };
  const end = _num(_obj(hourly).end_ts);
  const cut = (end != null ? end : rows[rows.length - 1].hour_ts + 3600) - 86400;
  return { cur: rows.filter(r => r.hour_ts >= cut),
           prev: rows.filter(r => r.hour_ts >= cut - 86400 && r.hour_ts < cut) };
}
// Fleet watts now: fleetWatts for the whole fleet, else per-row watts once per narrowed agent.
function _wattsNow(aggs, ids) {
  const seen = new Set();
  let total = 0, sensor = false;
  aggs.forEach(a => _agentsOf(a).forEach(r => {
    if (!r || !r.online || !r.agent_id || seen.has(r.agent_id) || !_inIds(ids, r.agent_id)) return;
    const w = _num(r.power_watts);
    if (w == null) return;
    seen.add(r.agent_id);
    total += w;
    sensor = true;
  }));
  if (ids) return sensor ? total : null;
  const w = fleetWatts(aggs);
  return (sensor || w > 0) ? w : null;
}

// Fleet tiles: hosts and power now, peak speed over tp's range, tokens, energy and
// savings over 24 h; hourly48 is taken as already narrowed to ids by the caller.
function fleetKpis(input) {
  const o = _obj(input), ids = _idSet(o.ids), aggs = _aggList(o.aggs), s1 = o.summary1;
  const halves = _hourHalves(o.hourly48);

  const agents = {};
  aggs.forEach(a => _agentsOf(a).forEach(r => {
    if (!r || !r.agent_id || !_inIds(ids, r.agent_id)) return;
    const rec = agents[r.agent_id] = agents[r.agent_id] || { online: false, age: null };
    rec.online = rec.online || !!r.online;
    const age = _num(r.age_s);
    if (r.online && age != null) rec.age = rec.age == null ? age : Math.max(rec.age, age);
  }));
  const recs = Object.values(agents), online = recs.filter(r => r.online);
  const maxAge = online.reduce((m, r) => (r.age != null && r.age > m ? r.age : m), -1);
  let agentsSub = 'No hosts yet';
  if (recs.length && online.length < recs.length) agentsSub = `${recs.length - online.length} offline`;
  else if (recs.length) agentsSub = maxAge >= 0
    ? `all reporting in under ${Math.max(1, Math.ceil(maxAge))} s` : 'all online';

  const tokens = _summaryVal(s1, ids, 'tokens_gen');
  const prevTok = _sumKey(halves.prev, 'tokens_gen');
  const tokSpark = halves.cur.map(r => _num(r.tokens_gen) || 0);

  const tp = _obj(o.tp), tpSeries = _arr(tp.series).filter(s => s && !_isPrompt(s.name));
  const nb = _arr(tp.labels).length;
  const genSums = [];
  for (let i = 0; i < nb; i++) genSums.push(tpSeries.reduce((s, x) => s + (_num(_arr(x.values)[i]) || 0), 0));
  const rangeH = [6, 12, 24].find(h => _keepFor(h) === tp.keep) || 24;
  const peak = genSums.length ? _rnd(Math.max(...genSums), 1) : null;

  const watts = _wattsNow(aggs, ids);
  const powerSpark = ids ? [] : _arr(tp.power).map(v => _num(v) || 0);

  const kwh = _summaryKwh(s1, ids);
  const prevWh = _sumKey(halves.prev, 'energy_wh');
  const cost = _summaryVal(s1, ids, 'cost_usd');
  const price = _num(_obj(s1).price_kwh) != null ? s1.price_kwh : _num(_obj(_obj(s1).config).price_kwh);
  let enSub = 'No power readings yet';
  if (kwh != null) enSub = cost != null
    ? '$' + cost.toFixed(2) + (price != null ? ` at $${price}/kWh` : '') : 'last 24 h';
  const enSpark = halves.cur.map(r => _num(r.energy_wh) || 0);

  const saved = _savings(s1, ids);
  const cloudLabel = _obj(_obj(s1).config).cloud_price_label;

  return [
    { id: 'k-agents', title: 'Hosts online', value: online.length, unit: '/ ' + recs.length,
      sub: agentsSub, delta: null, spark: null },
    { id: 'k-tok', title: 'Tokens', value: tokens != null ? fmtShort(tokens) : null, unit: '',
      sub: 'last 24 h' + (prevTok != null ? ` · ${fmtShort(prevTok)} the day before` : ''),
      delta: _delta(tokens, prevTok, true), spark: tokSpark.length ? tokSpark : null },
    { id: 'k-peak', title: 'Peak speed', value: peak != null ? peak.toFixed(1) : null, unit: 't/s',
      sub: `highest combined rate in a ${rangeBucketMs(rangeH) / 60000} min slot · last ${rangeH} h`, delta: null,
      spark: genSums.length ? genSums : null },
    { id: 'k-pow', title: 'Fleet power', value: watts != null ? watts.toFixed(1) : null, unit: 'W',
      sub: 'now · GPU and Mac power only', delta: null,
      spark: powerSpark.length ? powerSpark : null },
    { id: 'k-en', title: 'Energy', value: kwh != null ? kwh.toFixed(2) : null, unit: 'kWh',
      sub: enSub, delta: kwh != null ? _delta(kwh * 1000, prevWh, false) : null,
      spark: enSpark.length ? enSpark : null },
    { id: 'k-save', title: 'Saved vs cloud', value: saved != null ? '$' + saved.toFixed(2) : null,
      unit: '', sub: `same tokens at ${cloudLabel || 'cloud'} prices`, delta: null, spark: null },
  ];
}

// caps = cfg.agents: llama and lms on unless <p>_present is false, vllm only when true;
// a provider with no agents gets no server panel either.
function _provOn(p, agg, caps) {
  const c = _obj(caps), flag = c[p + '_present'];
  if (p === 'vllm' ? flag !== true : flag === false) return false;
  return !!agg && ((_num(agg.agent_count_total) || 0) > 0 || _agentsOf(agg).length > 0);
}
function _hostsTotal(a) { return _num(a.agent_count_total) != null ? a.agent_count_total : _agentsOf(a).length; }
function _hostsLine(agg) {
  const a = _obj(agg);
  return `${_num(a.agent_count_online) || 0} of ${_hostsTotal(a)} hosts online`;
}
// t/s with one decimal under 100, whole from 100.
function _tps(v) {
  v = _num(v) || 0;
  return (Math.abs(v) < 100 ? v.toFixed(1) : String(Math.round(v))) + ' t/s';
}
// Epoch ms → "12 min ago", "3 h ago" or "2 d ago" before now; '' when unreadable.
function _agoText(ms, now) {
  if (_num(ms) == null) return '';
  const s = Math.max(0, (now - ms) / 1000);
  return s < 3600 ? Math.max(1, Math.round(s / 60)) + ' min ago'
    : s < 86400 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
}
// Live generation and prompt rates with the rolling peaks: "Now 12.3 t/s · prompt 40.1 t/s · peak 197 t/s 12 min ago".
function _speed(agg, now) {
  const tp = _obj(_obj(agg).throughput), w = _obj(tp.window);
  if (_num(tp.total_tps) == null && _num(tp.total_pps) == null) return null;
  const parts = [`Now ${_tps(tp.total_tps)}`, `prompt ${_tps(tp.total_pps)}`];
  [['peak', _obj(w.gen).peak], ['prompt peak', _obj(w.prompt).peak]].forEach(([word, p]) => {
    const v = _num(_obj(p).v), ago = _agoText(_ms(_obj(p).ts), now);
    if (v != null) parts.push(`${word} ${_tps(v)}${ago ? ' ' + ago : ''}`);
  });
  return parts.join(' · ');
}
// Adds the live Speed line and the GPU hot flag (thermal_crit_count above 0) of a provider's aggregate to its panel.
function _live(p, agg, now) {
  const s = _speed(agg, now);
  if (s) { p.kv.unshift(['Speed', s]); p.table.rows.splice(1, 0, ['Speed', s]); }
  p.hot = (_num(_obj(_obj(agg).gpu).thermal_crit_count) || 0) > 0;
  if (p.hot) p.table.rows.splice(1, 0, ['GPU temperature', 'Hot']);
  return p;
}
// The preset args of a model in /api/llm/models: context, slots, KV, speculative decoding, reasoning.
function _modelArgs(llmModels, id) {
  const entry = _arr(_obj(llmModels).data).find(m => m && m.id === id);
  const args = _arr(entry && _obj(entry.status).args).map(String);
  const val = names => {
    for (let i = 0; i < args.length; i++) {
      for (const n of names) {
        if (args[i] === n) return i + 1 < args.length ? args[i + 1] : null;
        if (args[i].startsWith(n + '=')) return args[i].slice(n.length + 1);
      }
    }
    return null;
  };
  const draft = args.includes('--model-draft') || args.includes('-md');
  return {
    ctx: val(['--ctx-size', '-c']), parallel: val(['--parallel', '-np']),
    kv: val(['--cache-type-k', '-ctk']),
    spec: val(['--spec-type']) || (draft ? 'draft model' : null),
    reasoning: val(['--reasoning', '-rea']),
  };
}
const LLAMA_AGG_STATE = {
  active: ['Model loaded', 'ok', ''], idle: ['No model loaded', 'off', ''],
  sleeping: ['Sleeping', 'sl', 'wakes on the next request'], loading: ['Loading', 'warn', ''],
  off: ['Server off', 'off', ''],
};
const MODEL_STATUS = {
  loaded: ['Loaded', 'ok'], sleeping: ['Sleeping', 'sl'], loading: ['Loading', 'warn'],
  unloaded: ['Not loaded', 'off'],
};
const POWER_WORD = { powersave: 'Power saving', performance: 'Performance', balanced: 'Balanced' };
function _powerWord(v) { return POWER_WORD[v] || _cap(v); }

function _llamaPanel(st, llmModels, agg, host) {
  const s = _obj(st), res = _obj(s.residency);
  let state = { label: 'Nothing reported yet', tone: 'off', detail: '' };
  const agg0 = s.aggregate || res.aggregate;
  const m = Object.prototype.hasOwnProperty.call(LLAMA_AGG_STATE, agg0) ? LLAMA_AGG_STATE[agg0] : null;
  if (s.agent_online === false) state = { label: 'Host offline', tone: 'off', detail: '' };
  else if (m) state = { label: m[0], tone: m[1], detail: m[2] };
  else if (typeof agg0 === 'string' && agg0) state = { label: _cap(agg0), tone: 'off', detail: '' };
  const model = typeof s.model === 'string' && s.model ? s.model : null;
  const a = model ? _modelArgs(llmModels, model) : {};
  const kv = [['Model', model ? _tail(model) : 'none loaded']];
  if (a.ctx) {
    const slots = a.parallel ? ` · ${a.parallel} slot${a.parallel === '1' ? '' : 's'}` : '';
    kv.push(['Context', `${fmtShort(Number(a.ctx)) || a.ctx} tokens${slots}${a.kv ? ' · KV ' + a.kv : ''}`]);
  }
  if (a.spec || a.reasoning) {
    kv.push(['Extras', [a.spec ? `speculative decoding (${a.spec})` : 'no speculative decoding',
      a.reasoning ? `reasoning ${a.reasoning}` : ''].filter(Boolean).join(' · ')]);
  }
  kv.push(['Loaded models', String(_num(_obj(agg).active_model_count) || 0)]);
  const pw = _obj(s.power), pills = [];
  if (Object.keys(pw).length && pw.enabled !== false) {
    if (pw.applied) pills.push({ label: `${_powerWord(pw.applied)} now`, tone: pw.applied === pw.sleep ? 'sl' : 'ok' });
    if (pw.awake) pills.push({ label: `${_powerWord(pw.awake)} when awake`, tone: '' });
    _arr(pw.readback).forEach(r => {
      if (r && r.label) pills.push({ label: `${r.label} ${r.actual == null ? '' : r.actual} ${r.ok ? '✓' : '✗'}`.replace(/\s+/g, ' '), tone: r.ok ? 'ok' : 'off' });
    });
    const c = _obj(pw.counters), last = _obj(pw.last_switch);
    kv.push(['GPU power', `Power mode changed ${_num(c.switches) || 0} times, ${_num(c.failures) || 0} failed`
      + (last.outcome ? ` · last ${last.outcome}` : '')]);
  }
  let src = _arr(res.models).filter(x => x && x.model_id && (!x.provider || x.provider === 'llama'))
    .map(x => ({ id: x.model_id, status: x.status }));
  if (!src.length) {
    src = _arr(_obj(llmModels).data).filter(x => x && x.id)
      .map(x => ({ id: x.id, status: _obj(x.status).value }));
  }
  const models = src.map(x => {
    const st2 = MODEL_STATUS[x.status] || [_cap(x.status) || 'Unknown', 'off'];
    const ctx = _modelArgs(llmModels, x.id).ctx;
    return { label: String(x.id).replace('-GGUF', ''), title: String(x.id), status: st2[0], tone: st2[1],
             sub: ctx ? `${fmtShort(Number(ctx)) || ctx} ctx` : '' };
  });
  const rows = [['State', state.label]].concat(kv);
  if (pw.applied) rows.push(['Power now', String(pw.applied)]);
  if (pw.awake) rows.push(['Power when awake', String(pw.awake)]);
  models.forEach(x => rows.push(['Model on disk', `${x.title} (${x.status})`]));
  const ag = _obj(agg), awake = `${_num(ag.awake_agent_count) || 0} of ${_hostsTotal(ag)} awake`;
  rows.push(['Hosts awake', awake]);
  return { state, note: [_short(s.host) || _short(host), awake].filter(Boolean).join(' · '), kv, pills, models, hint: null,
           table: { cols: ['Field', 'Value'], rows } };
}

function _lmsPanel(metrics, agg) {
  const m = _obj(metrics), sys = _obj(m.system), server = _obj(m.server);
  let state = { label: 'Nothing reported yet', tone: 'off', detail: '' };
  const errText = [m.error, m.ps_error].find(e => typeof e === 'string' && e) || '';
  const failed = !!m.error || !!m.ps_error || m.ps_ok === false;
  const hint = !failed ? null : errText.includes('LMS_CMD not found')
    ? "LM Studio's command-line tool isn't on this host." : "LM Studio didn't answer.";
  if (m.agent_online === false) state = { label: 'Host offline', tone: 'off', detail: '' };
  else if (failed) state = { label: 'Not reachable', tone: 'bad', detail: '' };
  else if (server.on) state = { label: 'Server on', tone: 'ok', detail: server.port ? `port ${server.port}` : '' };
  else if (Object.keys(m).length) state = { label: 'Server off', tone: 'off', detail: '' };
  const ps = _arr(m.ps).filter(p => p && typeof p === 'object');
  const kv = [];
  if (ps.length) {
    const p = ps[0];
    kv.push(['Loaded', `${p.model || p.identifier || 'model'}${p.status ? ' · ' + _cap(p.status) : ''}`]);
    const parts = [p.params, p.quant, p.size, _num(p.context) != null ? fmtShort(p.context) + ' ctx' : null]
      .filter(Boolean);
    if (parts.length) kv.push(['Model', parts.join(' · ')]);
  } else {
    kv.push(['Loaded', 'No model loaded']);
  }
  if (Array.isArray(m.models)) kv.push(['Installed', _plural(m.models.length, 'model')]);
  const gt = _obj(m.gateway_tokens);
  if (_num(gt.gen) != null || _num(gt.prompt) != null) {
    kv.push(['Through manager', `${fmtShort(_num(gt.gen) || 0)} out · ${fmtShort(_num(gt.prompt) || 0)} in · `
      + `${_num(m.gateway_inflight) || 0} running`]);
  }
  const ram = _obj(sys.ram), swap = _obj(sys.swap), sw = _num(swap.percent);
  if (_num(ram.percent) != null) {
    kv.push(['Memory', `${ram.percent.toFixed(0)}% of ${_fmtBytes(ram.total_bytes)}`
      + (sw != null ? ` · swap ${sw.toFixed(0)}% of ${_fmtBytes(swap.total_bytes)}` : '')]);
  }
  kv.push(['Loaded models', String(_num(_obj(agg).loaded_model_count_total) || 0)]);
  const mp = _obj(m.mac_power);
  let power = null;
  if (Object.keys(mp).length) {
    const sub = [];
    if (_num(mp.pcore_util_pct) != null) {
      sub.push(`Performance cores ${mp.pcore_util_pct.toFixed(0)}%`
        + (_num(mp.pcore_freq_mhz) != null ? ` at ${(mp.pcore_freq_mhz / 1000).toFixed(2)} GHz` : ''));
    }
    if (_num(mp.ecore_util_pct) != null) sub.push(`efficiency cores ${mp.ecore_util_pct.toFixed(0)}%`);
    if (_num(mp.gpu_busy_pct) != null) sub.push(`GPU busy ${mp.gpu_busy_pct.toFixed(0)}%`);
    power = {
      title: `${sys.cpu_name || 'Mac'} power now` + (mp.thermal_pressure ? ` · thermal ${mp.thermal_pressure}` : ''),
      split: [{ label: 'CPU', v: _num(mp.cpu_package_w) || 0 }, { label: 'GPU', v: _num(mp.gpu_w) || 0 },
              { label: 'Neural Engine', v: _num(mp.ane_w) || 0 }],
      sub: sub.join(' · '),
    };
  }
  const models = ps.map(p => ({ label: String(p.identifier || p.model || ''), title: String(p.model || p.identifier || ''),
    status: _cap(p.status) || 'Loaded', tone: 'ok', sub: _num(p.context) != null ? fmtShort(p.context) + ' ctx' : '' }));
  const rows = [['State', state.label]].concat(kv);
  if (_num(mp.soc_total_w) != null) rows.push(['Mac power (W)', String(mp.soc_total_w)]);
  return { state, note: [sys.host ? _short(sys.host) : '', _hostsLine(agg)].filter(Boolean).join(' · '), kv, pills: [], models,
           power, swapHigh: sw != null && sw > 90, hint, table: { cols: ['Field', 'Value'], rows } };
}

function _vllmPanel(agg) {
  const a = _obj(agg), on = (_num(a.server_on_count) || 0) > 0;
  const state = on ? { label: 'Server on', tone: 'ok', detail: '' } : { label: 'Server off', tone: 'off', detail: '' };
  const names = _arr(a.active_models).map(x => (x && typeof x === 'object' ? (x.model || x.id) : x))
    .filter(x => typeof x === 'string' && x);
  const kv = [['Model', names.length ? names.map(_tail).join(', ') : 'none served'],
    ['Requests', `${_num(a.requests_running_total) || 0} running · ${_num(a.requests_waiting_total) || 0} waiting`]];
  if (_num(a.max_kv_cache_pct) != null) kv.push(['KV cache', `${a.max_kv_cache_pct.toFixed(0)}% full`]);
  kv.push(['GPU power', `${_gpuWatts(a).toFixed(0)} W`]);
  const ages = _agentsOf(a).map(r => _num(r && r.age_s)).filter(v => v != null);
  if (ages.length) kv.push(['Last report', `${Math.min(...ages).toFixed(1)} s ago`]);
  kv.push(['Loaded models', String(_num(a.active_model_count) || 0)]);
  return { state, note: _hostsLine(a), kv, pills: [], models: [],
           hint: on ? null : 'Start the server from LLM Control to see its requests and cache here.',
           table: { cols: ['Field', 'Value'], rows: [['State', state.label]].concat(kv) } };
}

// One server panel per provider, null when the provider is off or has no agents; o.llamaHost names the
// llama.cpp detail host when llama-state has none, o.now (ms) dates the rolling peaks.
function serverPanels(input) {
  const o = _obj(input), [llama, lms, vllm] = _aggList(o.aggs), now = _now(o.now);
  return {
    llama: _provOn('llama', llama, o.caps) ? _live(_llamaPanel(o.llamaState, o.llmModels, llama, o.llamaHost), llama, now) : null,
    lms: _provOn('lms', lms, o.caps) ? _live(_lmsPanel(o.lms, lms), lms, now) : null,
    vllm: _provOn('vllm', vllm, o.caps) ? _live(_vllmPanel(vllm), vllm, now) : null,
  };
}

// Legend text per throughput series: each shown provider's tokens generated in the summary window (a host
// under several providers counts under each), then the shown hosts' prompt tokens; '–' without a summary.
function tpLegend(summary1, byProvider, prov) {
  const s = _obj(summary1), has = Array.isArray(s.hosts), hosts = _arr(s.hosts).filter(h => h && h.agent_id);
  const sum = (ids, k) => hosts.reduce((t, h) => t + (ids.has(h.agent_id) ? (_num(h[k]) || 0) : 0), 0);
  const text = v => (has ? fmtShort(v) + ' tokens' : '–');
  const all = new Set();
  const out = _shownProvs(prov).map(p => {
    const ids = new Set(_arr(_obj(byProvider)[p]).map(a => a && a.agent_id).filter(Boolean));
    ids.forEach(id => all.add(id));
    return text(sum(ids, 'tokens_gen'));
  });
  return out.concat([text(sum(all, 'tokens_prompt'))]);
}

function _hourLabel(ts) {
  const d = new Date(ts * 1000);
  return DOW[d.getDay()] + ' ' + _pad2(d.getHours()) + ':00';
}
// Hourly energy rows → local 'Ddd HH:00' labels, Idle and Generating Wh, and kWh totals.
function energyPerHour(hourlyRows) {
  const rows = _hourRows(hourlyRows);
  const act = rows.map(r => _rnd(_num(r.active_energy_wh) || 0, 2));
  const idle = rows.map((r, i) => Math.max(0, _rnd((_num(r.energy_wh) || 0) - act[i], 2)));
  const wh = rows.reduce((s, r) => s + (_num(r.energy_wh) || 0), 0);
  const awh = act.reduce((s, v) => s + v, 0);
  const kwh = _rnd(wh / 1000, 3), active = _rnd(awh / 1000, 3);
  return {
    labels: rows.map(r => _hourLabel(r.hour_ts)),
    series: [{ name: 'Idle', values: idle }, { name: 'Generating', values: act }],
    totals: { kwh, active_kwh: active, idle_kwh: _rnd(Math.max(0, kwh - active), 3) },
  };
}

// Metered hosts ranked by Wh (v), filter value agent id; noPower counts hosts without power.
function energyByHost(summary7, ids) {
  ids = _idSet(ids);
  const s = _obj(summary7), hosts = _summaryHosts(s, ids);
  const metered = hosts.filter(h => _num(h.kwh) != null);
  const rows = metered.slice().sort((a, b) => b.kwh - a.kwh).map(h => ({
    label: _short(h.hostname) || String(h.agent_id || ''), v: _rnd(h.kwh * 1000, 1), val: h.agent_id || h.hostname,
  }));
  const noPower = hosts.length - metered.length, parts = [];
  if (noPower) parts.push(`${_plural(noPower, 'host')} ${noPower === 1 ? 'reports' : 'report'} no power.`);
  const saved = _savings(s, ids), label = _obj(s.config).cloud_price_label;
  if (saved != null) {
    let t = `Same tokens at ${label || 'cloud'} prices: $${saved.toFixed(2)} saved`;
    const per = _num(_obj(s.totals).usd_per_mtok);
    if (!ids && per != null) t += ` · $${per.toFixed(2)} per million tokens here`;
    parts.push(t + '.');
  }
  return { rows, noPower, unit: 'Wh', sub: parts.join(' ') };
}

// Generating vs idle Wh over the summary window; Wh per model only for the whole fleet.
function energySplit(summary1, ids) {
  ids = _idSet(ids);
  const s = _obj(summary1);
  const active = _summaryVal(s, ids, 'active_kwh'), idle = _summaryVal(s, ids, 'idle_kwh');
  const split = (active == null && idle == null) ? [] : [
    { label: 'Generating', v: _rnd((active || 0) * 1000, 1) }, { label: 'Idle', v: _rnd((idle || 0) * 1000, 1) }];
  const models = _arr(s.models)
    .filter(m => !ids && m && (_num(m.kwh) || 0) > 0)
    .map(m => ({ label: m.model === '(no model)' ? 'No model' : _tail(m.model), v: _rnd(m.kwh * 1000, 1) }))
    .sort((a, b) => b.v - a.v);
  return { split, models, unit: 'Wh' };
}

// Online hosts with a power reading, counted once per agent, Mac power or GPU.
function powerNow(aggs, byProvider, ids) {
  ids = _idSet(ids);
  const host = _hostMap(byProvider), byAgent = {};
  _aggList(aggs).forEach((a, i) => _agentsOf(a).forEach(r => {
    if (!r || !r.agent_id || !r.online || !_inIds(ids, r.agent_id, host[r.agent_id])) return;
    const rec = byAgent[r.agent_id] = byAgent[r.agent_id] || { w: null, prov: null };
    const w = _num(r.power_watts);
    if (rec.w == null && w != null) { rec.w = w; rec.prov = PROVS[i]; }
  }));
  const rows = [];
  let total = 0, noSensor = 0;
  Object.keys(byAgent).forEach(id => {
    const rec = byAgent[id];
    if (rec.w == null) { noSensor++; return; }
    total += rec.w;
    rows.push({ label: _short(host[id]) || id.slice(0, 8) + '…', v: _rnd(rec.w, 1),
                sub: rec.prov === 'lms' ? 'Mac power' : 'GPU', val: id });
  });
  rows.sort((a, b) => b.v - a.v);
  return { rows, total: _rnd(total, 1), noSensor };
}

// Hosts ranked by tokens generated in the summary window.
function hostsByTokens(summary1, ids) {
  return _summaryHosts(summary1, _idSet(ids))
    .map(h => ({ label: _short(h.hostname) || String(h.agent_id || ''), v: _num(h.tokens_gen) || 0,
                 val: h.agent_id || h.hostname }))
    .sort((a, b) => b.v - a.v);
}

// agentRows narrowed to the hosts in ids.
function hostRows(aggs, byProvider, ids) {
  ids = _idSet(ids);
  return agentRows(_aggList(aggs), byProvider).filter(r => _inIds(ids, r.agentId, r.hostname));
}

function _checkTitle(x) { return x.title || _cap(String(x.check || '').replace(/_/g, ' ')) || 'Check'; }
const SEV_RANK = { critical: 0, warning: 1, info: 2 };
function _sevRank(v) { return Object.prototype.hasOwnProperty.call(SEV_RANK, v) ? SEV_RANK[v] : 3; }
// Forecast findings → open (critical, warning, info, then newest first) and recently cleared feed rows;
// under a narrowing only matching hosts.
function forecastFeed(fc, ids) {
  ids = _idSet(ids);
  const f = _obj(fc);
  const pick = x => x && typeof x === 'object' && (!ids || (!!x.host && ids.has(x.host)));
  const seen = x => _ms(x.last_seen) || _ms(x.first_seen);
  const open = _arr(f.findings).filter(pick).slice()
    .sort((a, b) => _sevRank(a.severity) - _sevRank(b.severity) || _byNewest(seen(a), seen(b))).map(x => ({
    kind: _cap(x.severity) || 'Info',
    title: _firstSentence(x.detail) || String(x.summary || _checkTitle(x)),
    detail: [_short(x.host), _checkTitle(x), x.confidence ? `${x.confidence} confidence` : '']
      .filter(Boolean).join(' · '),
    at: seen(x),
  }));
  const cleared = _arr(f.cleared).filter(pick).slice()
    .sort((a, b) => _byNewest(_ms(a.resolved), _ms(b.resolved))).slice(0, 4)
    .map(x => ({
      kind: x.status === 'dismissed' ? 'Dismissed' : 'Cleared',
      title: _checkTitle(x) + (x.host ? ' on ' + _short(x.host) : ''),
      detail: _firstSentence(x.detail), at: _ms(x.resolved),
    }));
  let note = 'Nothing reported yet';
  if (f.enabled === false) note = 'Forecast is off';
  else if (f.last_result) note = String(f.last_result);
  const checks = _arr(f.checks), model = _obj(f.tower).model;
  const sub = checks.length
    ? `${_plural(checks.filter(c => c && c.state === 'ok').length, 'check')} on ${_num(f.window_days) || 0} days of data`
      + (model ? ` · written by ${model}` : '')
    : '';
  return { open, cleared, note, sub, lastAt: _ms(f.last_run), nextAt: _ms(f.next_run) };
}

// Tower state + insights → the newest insight as a feed row and the model, fallback, activity lines.
function towerFeed(state, insights) {
  const s = _obj(state);
  const list = (Array.isArray(insights) ? insights : _arr(_obj(insights).insights)).filter(i => i && typeof i === 'object');
  const i = list[0];
  const insight = i ? {
    kind: _cap(i.severity) || 'Info', title: String(i.summary || ''),
    detail: [_short(i.host), i.rule, i.status].filter(Boolean).join(' · '),
    at: _ms(i.created) || _ms(i.created_at), action: String(i.suggested_action || ''),
    checks: _arr(i.checks).filter(c => c && c.name)
      .map(c => ({ label: String(c.name).replace(/_/g, ' '), ok: !!c.ok, title: String(c.summary || '') })),
  } : null;
  const kv = [];
  if (s.enabled === false) kv.push(['Tower', 'Off']);
  else if (s.model) {
    const h = _arr(s.hosts)[0], grade = _obj(s.check).grade;
    kv.push(['Model', `${s.model}${h ? ' on ' + _short(h) : ''}${grade ? ' · tools ' + grade : ''}`]);
    const fb = _obj(s.fallback);
    if (s.fallback_enabled !== false && fb.model) {
      const fh = _arr(fb.hosts)[0];
      kv.push(['Fallback', `${_tail(fb.model)}${fh ? ' on ' + _short(fh) : ''}`]);
    }
    const act = [];
    if (_num(s.threads) != null) act.push(_plural(s.threads, 'conversation'));
    if (_num(s.timers) != null) act.push(_plural(s.timers, 'timer'));
    if (typeof s.diagnose_alarms === 'boolean') act.push(`alarm diagnosis ${s.diagnose_alarms ? 'on' : 'off'}`);
    if (act.length) kv.push(['Activity', act.join(' · ')]);
  }
  return { insight, kv, note: _plural(list.length, 'insight') };
}

// Active alerts narrowed by source host → newest-first feed rows and alertsSummary.
function alertsFeed(alerts, ids) {
  ids = _idSet(ids);
  const list = _arr(alerts).filter(a => a && typeof a === 'object'
    && (!ids || (!!a.source_host && ids.has(a.source_host))));
  const at = a => { const t = _ms(a.created_at); return t == null ? -Infinity : t; };
  const rows = list.slice()
    .sort((a, b) => (at(b) > at(a) ? 1 : at(b) < at(a) ? -1 : 0))
    .map(a => ({
      kind: _cap(a.severity) || 'Info', title: String(a.rule_name || 'Alert'),
      detail: [a.message, _short(a.source_host)].filter(Boolean).join(' · '),
      at: _ms(a.created_at),
    }));
  return { rows, summary: alertsSummary(list) };
}

// overallBandHidden hero → throughput, tiles → servers, and alerts, forecast and agents together → watch
// into overallRowsHidden, other ids dropped; overallBandHidden and overallBandOrder deleted.
const LEGACY_BAND_ROW = { hero: 'throughput', tiles: 'servers' };
function mapLegacyBand(layout) {
  if (!layout || typeof layout !== 'object' || Array.isArray(layout)) return { changed: false };
  const had = 'overallBandHidden' in layout || 'overallBandOrder' in layout;
  if (!had) return { changed: false };
  const old = _arr(layout.overallBandHidden), mapped = old.map(id => (Object.prototype.hasOwnProperty.call(LEGACY_BAND_ROW, id) ? LEGACY_BAND_ROW[id] : null)).filter(Boolean);
  if (['alerts', 'forecast', 'agents'].every(id => old.includes(id))) mapped.push('watch');
  if (mapped.length) {
    const cur = _arr(layout.overallRowsHidden);
    layout.overallRowsHidden = cur.concat(mapped.filter((id, i) => !cur.includes(id) && mapped.indexOf(id) === i));
  }
  delete layout.overallBandHidden;
  delete layout.overallBandOrder;
  return { changed: true };
}

return { agentRows, alertsSummary, fleetWatts, fmtShort, PROVIDER_LABEL,
         rangeBucketMs, bucketLabel, hostIds, throughput, liveBucket, fleetKpis, serverPanels,
         energyPerHour, energyByHost, energySplit, powerNow, hostsByTokens, hostRows,
         forecastFeed, towerFeed, alertsFeed, mapLegacyBand, rangeLabel, bucketLabels, promptName, tpLegend };
});
