// Admin → System Health card (#797): services column, live data-flow diagram
// with a per-node detail strip, and the ordered warnings column.
(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const SVG_LOG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 2.5h9v11h-9z"/><path d="M5.5 5.5h5M5.5 8h5M5.5 10.5h3"/></svg>';
  const SVG_RESTART = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13.2 8.6A5.3 5.3 0 1 1 11.8 4.2"/><path d="M12 1.8v3h-3"/></svg>';
  const NODE_NAME = { agents: 'Agents', manager: 'Manager', browsers: 'Browsers', ae: 'Alarm Engine', influx: 'InfluxDB' };
  const MARKER = { ok: 'url(#hcAhOk)', warn: 'url(#hcAhWarn)', crit: 'url(#hcAhCrit)', off: 'url(#hcAhOff)' };
  const EDGE_IDS = ['eAgMg', 'eAgAe', 'eMgAe', 'eMgBr', 'eAeIn'];
  const WORST = { ok: 0, off: 0, warn: 1, crit: 2 };
  // Which edges each node's dot/border follows (worst incident edge wins).
  const NODE_EDGES = {
    nAg: ['eAgMg', 'eAgAe'], nMg: ['eAgMg', 'eMgAe', 'eMgBr'],
    nBr: ['eMgBr'], nAe: ['eAgAe', 'eMgAe', 'eAeIn'], nIn: ['eAeIn'],
  };

  let _selNode = null;
  let _autoNode = null;      // node the card opened by itself (null = operator's choice)
  let _mutedNode = null;     // flagged node the operator closed; stays shut until it heals

  // Worst flag among a node's detail rows ('' | 'warn' | 'crit').
  function rowsWorst(d, node) {
    let worst = '';
    for (const r of detailRows(d, node)) {
      if (r[2] === 'crit') return 'crit';
      if (r[2] === 'warn') worst = 'warn';
    }
    return worst;
  }

  // Opens a node whose trouble the diagram cannot show on an edge: any crit
  // row, or a warn row on the manager (its own pressure has no edge). The
  // operator's own pick or close always wins.
  function autoSelect(flags) {
    const order = ['manager', 'agents', 'ae', 'influx', 'browsers'];
    const flagged = order.filter(n => flags[n] === 'crit' || (n === 'manager' && flags[n]));
    const worst = flagged.find(n => flags[n] === 'crit') || flagged[0] || null;
    if (_mutedNode && !flags[_mutedNode]) _mutedNode = null;
    if (_selNode && _selNode !== _autoNode) return;          // operator's own pick
    if (_autoNode && !flags[_autoNode]) { _selNode = null; _autoNode = null; }
    if (worst && worst !== _mutedNode && !_selNode) { _selNode = worst; _autoNode = worst; }
  }
  let _last = null;
  let _lastRel = null;
  let _view = 'recent';      // column view while there are no warnings; never saved
  const VIEW_LABEL = { recent: ['Recent', 'last 24 h'], capacity: ['Capacity', 'how close to each limit'], trends: ['Trends', 'last hour'] };
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  // {clock: '5:35 PM', day: 'Oct 13' | ''} — day only when the event is from an earlier calendar day.
  function fmtEventTime(iso, nowMs) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return { clock: '—', day: '' };
    let h = d.getHours();
    const ap = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    const clock = `${h}:${String(d.getMinutes()).padStart(2, '0')} ${ap}`;
    const now = new Date(nowMs == null ? Date.now() : nowMs);
    const same = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
    return { clock, day: same ? '' : `${MON[d.getMonth()]} ${d.getDate()}` };
  }
  function pctClass(p) { return p >= 90 ? 'crit' : p >= 60 ? 'warn' : ''; }

  // ── Recent view ──────────────────────────────────────────────────────
  function recentHtml(d) {
    const list = (d && Array.isArray(d.recent)) ? d.recent : [];
    if (!list.length) return '<div class="hc-ev-none">Nothing in the last 24 h</div>';
    const rows = list.map(e => {
      const t = fmtEventTime(e.ts);
      return `<div class="hc-evr"><time datetime="${esc(e.ts)}">${esc(t.clock)}${t.day ? `<small>${esc(t.day)}</small>` : ''}</time>`
        + `<span class="dot ${e.k === 'warn' ? 'warn' : 'ok'}"></span><div class="t"><b>${esc(e.title)}</b>`
        + (e.detail ? ` <span class="m">${esc(e.detail)}</span>` : '')
        + (e.actor ? ` <span class="by">· ${esc(e.actor)}</span>` : '') + '</div></div>';
    });
    return `<div class="hc-ev">${rows.join('')}<a class="hc-ev-all" href="#audit?since_hours=24" data-act="allevents">All events</a></div>`;
  }
  function budgetHtml(d) {
    const flow = (d && d.flow) || {};
    const limit = Number(flow.anon_budget_per_min) || 0;
    if (limit <= 0) return '';
    const used = Math.max(0, Number(flow.anon_req_per_min) || 0);
    const pct = Math.min(100, Math.round(used / limit * 100));
    const cls = pctClass(pct);
    return `<div class="hc-bud"><span class="l">Open-route budget<em>last minute</em></span><span class="n ${cls}">${used} / ${limit}</span>`
      + `<div class="tr"><div class="fi ${cls}${used === 0 ? ' idle' : ''}" style="width:${pct}%"></div></div></div>`;
  }
  // The column's HTML for the current view (no warnings).
  function viewHtml(d) {
    if (_view === 'capacity') return capacityHtml(d, _capCache.rows);
    if (_view === 'trends') return trendsHtml(_trendCache.series);
    return recentHtml(d) + budgetHtml(d);
  }
  function setView(name) {
    _view = VIEW_LABEL[name] ? name : 'recent';
    if (_view === 'capacity') loadCapacityExtra();
    if (_view === 'trends') loadTrends();
    const vs = $('adminHealthView');
    if (vs) vs.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === _view)));
    if (_last) render(_last, _lastRel);
  }
  // ── Capacity view ────────────────────────────────────────────────────
  let _capCache = { at: 0, rows: null };     // {influx_bytes, influx_rss_mb, influx_host, disk_pct, influx_ram_total_mb}
  const n = v => (v == null || v === '' ? NaN : Number(v));
  function gb(b) { return b == null ? null : (b / 1e9).toFixed(1).replace(/\.0$/, '') + ' GB'; }
  function capRow(group, label, num, pct, forceIdle) {
    const p = pct == null || !isFinite(pct) ? 0 : Math.max(0, Math.min(100, Math.round(pct)));
    const idle = forceIdle || pct == null || !isFinite(pct);
    return { group, label, num: num == null ? '—' : num, pct: p, cls: idle ? 'idle' : pctClass(p) };
  }
  function procRows(group, p) {
    if (!p || typeof p !== 'object') return [capRow(group, 'CPU', null, null, true), capRow(group, 'Memory', null, null, true)];
    const cpu = n(p.cpu_pct), ram = n(p.ram_pct), rss = n(p.rss_mb);
    return [
      capRow(group, 'CPU', isFinite(cpu) ? `${Math.round(cpu)} %` : null, isFinite(cpu) ? cpu : null),
      capRow(group, 'Memory', isFinite(rss) ? `${Math.round(rss)} MB${isFinite(ram) ? ` · ${Math.round(ram)} % of RAM` : ''}` : null, isFinite(ram) ? ram : null),
    ];
  }
  function capacityRows(d, extra) {
    const mgr = (d && d.manager) || {};
    const ae = svcOf(d, 'alarm_engine');
    const x = extra || {};
    const rows = [...procRows('Manager', mgr.process), ...procRows('Alarm Engine', ae && ae.process)];
    const disk = n(x.disk_pct), bytes = n(x.influx_bytes);
    rows.push(capRow('InfluxDB', 'On disk', isFinite(bytes)
      ? `${gb(bytes)}${isFinite(disk) ? ` · disk ${Math.round(disk)} % used` : ''}` : null, isFinite(disk) ? disk : null));
    const rss = n(x.influx_rss_mb), ownTotal = n(x.influx_ram_total_mb), mgrTotal = n(mgr.ram_total_mb);
    const sameHost = x.influx_host && mgr.hostname && x.influx_host === mgr.hostname;
    const total = ownTotal > 0 ? ownTotal : (sameHost ? mgrTotal : null);
    const ramPct = isFinite(rss) && total > 0 ? rss / total * 100 : null;
    rows.push(capRow('InfluxDB', 'Memory', isFinite(rss) ? `${Math.round(rss)} MB${ramPct != null ? ` · ${Math.round(ramPct)} % of RAM` : ''}` : null, ramPct, ramPct == null));
    const s = mgr.streams || {}, c = mgr.connections || {};
    const limRow = (label, used, lim) => (isFinite(used) && isFinite(lim)
      ? capRow('Limits', label, `${used} / ${lim}`, lim > 0 ? used / lim * 100 : null)
      : capRow('Limits', label, null, null, true));
    rows.push(limRow('Live streams', n(s.active), n(s.limit)));
    rows.push(limRow('Worker threads busy', n(c.worker_threads_busy), n(c.worker_threads)));
    return rows;
  }
  function capacityHtml(d, extra) {
    let group = null, out = '<div class="hc-cap">';
    for (const r of capacityRows(d, extra)) {
      if (r.group !== group) { group = r.group; out += `<div class="hc-cg">${esc(group)}</div>`; }
      out += `<div class="hc-cr"><span class="l">${esc(r.label)}</span><span class="n ${r.cls === 'idle' ? '' : r.cls}">${esc(r.num)}</span>`
        + `<div class="tr"><div class="fi ${r.cls}" style="width:${r.pct}%"></div></div></div>`;
    }
    return out + '<div class="hc-cap-foot">Amber at 60 % of a limit, red at 90 %; the matching warning appears at the same moment.</div></div>';
  }
  // InfluxDB on-disk bytes and RSS from the stored metrics, cached 60 s.
  async function loadCapacityExtra() {
    if (Date.now() - _capCache.at < 60000) return;
    _capCache.at = Date.now();
    const get = async u => { try { const r = await fetch(u); return r.ok ? await r.json() : []; } catch (_) { return []; } };
    const [inf, procs, sys] = await Promise.all([
      get('/api/alarm/metrics?source=influxdb&limit=50'), get('/api/alarm/metrics?source=processes&limit=300'),
      get('/api/alarm/metrics?source=system&limit=600'),
    ]);
    const bytes = (Array.isArray(inf) ? inf : []).find(r => r.metric_name === 'bytes_on_disk');
    const host = bytes && bytes.hostname;
    const rss = (Array.isArray(procs) ? procs : []).find(r => r.metric_name === 'influxdb_rss_mb' && (!host || r.hostname === host));
    const sysHost = host || (_last && _last.manager && _last.manager.hostname) || null;
    const sysRow = name => (Array.isArray(sys) ? sys : []).find(r => r.metric_name === name && (!sysHost || r.hostname === sysHost));
    const disk = sysRow('disk_root_percent'), ram = sysRow('ram_total_bytes');
    _capCache.rows = { influx_bytes: bytes ? bytes.latest_value : null, influx_rss_mb: rss ? rss.latest_value : null,
                       influx_host: host || null, disk_pct: disk ? disk.latest_value : null,
                       influx_ram_total_mb: ram && isFinite(Number(ram.latest_value)) ? Number(ram.latest_value) / 1048576 : null };
    if (_view === 'capacity' && _last) render(_last, _lastRel);
  }
  // ── Trends view ──────────────────────────────────────────────────────
  let _trendCache = { at: 0, series: null };   // {anon, refused, api, ing, influx, rule} → [{t, v}]
  const TREND_DEFS = [
    ['anon', 'Open-route requests /min', 'manager_self_monitor/manager_anon_requests_per_min', ''],
    ['api', 'Manager API latency', 'manager_self_monitor/manager_api_latency_ms', 'ms'],
    ['ing', 'Alarm Engine ingest latency', 'manager_self_monitor/ae_ingest_latency_ms', 'ms'],
    ['rule', 'Alarm Engine rule evaluation', 'manager_self_monitor/rule_eval_cycle_ms', 'ms'],
    ['influx', 'InfluxDB write latency', 'manager_self_monitor/influx_write_latency_ms', 'ms'],
  ];
  function trendSeries(raw) {
    const out = {};
    for (const k of Object.keys(raw || {})) {
      out[k] = (Array.isArray(raw[k]) ? raw[k] : [])
        .map(p => ({ t: new Date(p.timestamp).getTime(), v: Number(p.value) }))
        .filter(p => isFinite(p.t) && isFinite(p.v)).sort((a, b) => a.t - b.t);
    }
    return out;
  }
  // [[x, y]] for each value, scaled into a w × h box.
  function sparkXY(values, w, h) {
    const max = Math.max(...values, 0) * 1.08 || 1;
    return values.map((v, i) => [+(values.length === 1 ? w : i / (values.length - 1) * w).toFixed(1),
      +(h - 2 - (v / max) * (h - 6)).toFixed(1)]);
  }
  function sparkPath(values, w, h) {
    return sparkXY(values, w, h).map(([x, y], i) => `${i ? 'L' : 'M'}${x},${y}`).join(' ');
  }
  // One amber sub-path per marked point: previous point → it → next point.
  function sparkSvg(key, pts, segIdx) {
    const W = 240, H = 30, vals = pts.map(p => p.v);
    const xy = sparkXY(vals, W, H);
    const line = sparkPath(vals, W, H);
    let seg = '';
    if (segIdx && segIdx.length && xy.length > 1) {
      seg = segIdx.map(i => xy.slice(Math.max(0, i - 1), Math.min(xy.length, i + 2))
        .map(([x, y], k) => `${k ? 'L' : 'M'}${x},${y}`).join(' ')).join(' ');
    }
    const end = xy.length ? `<circle cx="${W}" cy="${xy[xy.length - 1][1]}" r="2"/>` : '';
    return `<svg data-sp="${esc(key)}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">`
      + `<line class="base" x1="0" y1="${H - 2}" x2="${W}" y2="${H - 2}"/>`
      + (line ? `<path class="area" d="${line} L${W},${H - 2} L0,${H - 2} Z"/><path class="line" d="${line}"/>` : '')
      + (seg ? `<path class="seg" d="${seg}"/>` : '') + end + '</svg>';
  }
  function trendsHtml(series) {
    if (!series) return '<div class="hc-ev-none">Loading…</div>';
    if (series.error) return '<div class="hc-ev-none">Not available</div>';
    const refused = new Set((series.refused || []).filter(p => p.v > 0).map(p => Math.round(p.t / 60000)));
    return '<div class="hc-tr">' + TREND_DEFS.map(([key, label, , unit]) => {
      const pts = series[key] || [];
      const last = pts.length ? pts[pts.length - 1].v : null;
      let num = '—', cls = '';
      if (key === 'anon') {
        const segIdx = pts.map((_, i) => i).filter(i => refused.has(Math.round(pts[i].t / 60000)));
        if (segIdx.length) {
          const peak = pts.reduce((a, p) => (p.v > a.v ? p : a), pts[0]);
          num = `peak ${Math.round(peak.v)} at ${fmtEventTime(new Date(peak.t).toISOString()).clock}`; cls = 'warn';
        } else if (last != null) num = `${Math.round(last)}/min now`;
        return `<div class="hc-sp"><div class="h"><span class="l">${esc(label)}</span><span class="n ${cls}">${esc(num)}</span></div>${sparkSvg(key, pts, segIdx)}</div>`;
      }
      if (last != null) num = unit === 'ms' ? `${last.toFixed(1)} ms now` : `${Math.round(last)} % now`;
      return `<div class="hc-sp"><div class="h"><span class="l">${esc(label)}</span><span class="n">${esc(num)}</span></div>${sparkSvg(key, pts, null)}</div>`;
    }).join('') + '</div>';
  }
  async function loadTrends() {
    if (Date.now() - _trendCache.at < 60000) return;
    _trendCache.at = Date.now();
    const url = p => `/api/alarm/metrics/${p}?since_minutes=60&limit=120`;
    const get = async u => { const r = await fetch(u); if (!r.ok) throw new Error(String(r.status)); return r.json(); };
    try {
      const [anon, refused, api, ing, influx, rule] = await Promise.all([
        get(url('manager_self_monitor/manager_anon_requests_per_min')), get(url('manager_self_monitor/manager_anon_refused_per_min')),
        get(url('manager_self_monitor/manager_api_latency_ms')), get(url('manager_self_monitor/ae_ingest_latency_ms')),
        get(url('manager_self_monitor/influx_write_latency_ms')), get(url('manager_self_monitor/rule_eval_cycle_ms')),
      ]);
      _trendCache.series = trendSeries({ anon, refused, api, ing, influx, rule });
    } catch (_) {
      _trendCache.series = { error: true };
    }
    if (_view === 'trends' && _last) render(_last, _lastRel);
  }

  // ── formatting ───────────────────────────────────────────────────────
  // Shrinks, then ellipsises, an SVG text run so it stays inside its box.
  function fitText(t, maxW) {
    if (!t || typeof t.getComputedTextLength !== 'function') return;
    const fits = () => { try { return t.getComputedTextLength() <= maxW; } catch (_) { return true; } };
    let size = 9.5;
    while (!fits() && size > 8) { size -= 0.5; t.style.fontSize = size + 'px'; }
    let s = t.textContent;
    while (!fits() && s.length > 4) { s = s.slice(0, -2); t.textContent = s + '…'; }
  }

  function num(n) {
    if (n == null || isNaN(n)) return null;
    const v = Number(n);
    return v >= 10 ? String(Math.round(v)) : String(parseFloat(v.toFixed(1)));
  }
  // Two-unit uptime: "3d 4h" / "6h 12m" / "45m" / "12s".
  function upStr(s) {
    if (s == null || isNaN(s)) return null;
    const t = Math.max(0, Math.floor(Number(s)));
    if (t >= 86400) return `${Math.floor(t / 86400)}d ${Math.floor((t % 86400) / 3600)}h`;
    if (t >= 3600) return `${Math.floor(t / 3600)}h ${Math.floor((t % 3600) / 60)}m`;
    if (t >= 60) return `${Math.floor(t / 60)}m`;
    return `${t}s`;
  }
  function svcOf(d, name) {
    return ((d && d.services) || []).find(s => s && s.name === name) || null;
  }
  function ms(v) { return v == null ? '—' : `${Math.round(v)} ms`; }
  function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }
  // OTLP receiver summary for the AE detail strip: "12 batches · 3m ago" / "none yet".
  function otlpStr(o) {
    const n = ['metric_batches', 'trace_batches', 'log_batches'].reduce((t, k) => t + (Number(o[k]) || 0), 0);
    if (!n) return 'none yet';
    const age = o.last_batch_age_s != null ? upStr(o.last_batch_age_s) : null;
    return plural(n, 'batch', 'batches') + (age ? ` · ${age} ago` : '');
  }

  // ── services column ──────────────────────────────────────────────────
  // One row per service: dot, name (+ link chip), version, status pill, restart,
  // plus a wrapping detail line under the version when the pill is not green.
  // #1235: InfluxDB state → short pill word; the full text goes to the detail line.
  function influxPill(state) {
    const st = String(state || 'unknown');
    if (st === 'connected') return { txt: 'connected', cls: 'ok', detail: null };
    const head = st.split(':')[0].replace(/_/g, ' ').trim() || 'unknown';
    return { txt: head, cls: 'crit', detail: st.indexOf(':') > 0 ? st : null };
  }

  function svcRows(d) {
    const mgr = (d && d.manager) || {};
    const ae = svcOf(d, 'alarm_engine');
    const influx = svcOf(d, 'influxdb');
    const pending = (d && d.restart_pending) || [];
    const rows = [{
      st: 'ok', n: 'Manager', lk: null, ver: mgr.version || '—',
      up: upStr(mgr.uptime_s), upTxt: 'unknown', upCls: upStr(mgr.uptime_s) ? '' : 'crit', detail: null,
      act: 'Restart Manager', svc: 'manager', rp: pending.includes('manager'),
    }];

    const aeOk = !!(ae && ae.ok);
    const tls = (ae && ae.tls && typeof ae.tls === 'object') ? ae.tls : null;
    let lk = null;
    if (tls) {
      if (tls.enabled && tls.active) lk = ['https', 'on'];
      else if (tls.enabled) lk = ['cert missing', 'crit'];
      else lk = ['http', 'off'];
    }
    // #764: the AE restart is always offered; the tip names how it restarts.
    const via = ((d && d.ae_restart) || {}).via;
    const auth = aeOk ? authState(ae) : null;
    // #1233: "degraded" = engine up, InfluxDB not connected.
    const aeDegraded = aeOk && ae.state === 'degraded';
    rows.push({
      st: aeOk ? (aeDegraded ? 'warn' : 'ok') : 'crit', n: 'Alarm Engine', lk, ver: (ae && ae.version) || '—',
      ak: auth && auth.k === 'warn' ? { chip: auth.chip, tip: auth.tip } : null,
      up: aeOk && !aeDegraded ? upStr(ae && ae.uptime_s) : null,
      upTxt: aeOk ? (aeDegraded ? 'degraded' : 'connected') : 'unreachable',
      upCls: aeOk ? (aeDegraded ? 'warn' : '') : 'crit',
      detail: aeDegraded ? 'InfluxDB is not connected — metric history is not being stored'
        : (!aeOk && ae && ae.error ? String(ae.error) : null),
      act: 'Restart Alarm Engine' + (via === 'self-restart' ? ' · via its self-restart API' : ''),
      svc: 'alarm_engine', rp: pending.includes('alarm_engine'),
    });

    const inOk = !!(influx && influx.ok);
    const ip = influxPill(influx && (influx.state || (influx.via && !inOk ? 'unreachable' : null)));
    rows.push({
      st: inOk ? 'ok' : 'crit', n: 'InfluxDB', lk: null, ver: (influx && influx.version) || '—',
      up: null, upTxt: ip.txt, upCls: inOk ? 'ok' : 'crit', detail: ip.detail,
      act: null, svc: null,
    });
    return rows;
  }

  // #828: the engine's auth posture — chip, detail text, severity, remedy.
  const AUTH_REMEDY = 'Set the same [alarm_engine].management_token on both hosts — Admin → Settings → Alarm engine';
  function authState(ae) {
    const a = ae && ae.auth;
    const det = (ae && ae.auth_detail && typeof ae.auth_detail === 'object') ? ae.auth_detail : {};
    if (a === 'open') {
      const lan = det.open_on_network !== false;
      return { k: lan ? 'warn' : '', chip: 'auth open', tip: AUTH_REMEDY,
        text: lan ? 'open — no token on the engine' : 'open (loopback bind only)' };
    }
    if (a !== 'enforced') return null;
    if (ae.bearer_configured === false) {
      return { k: 'warn', chip: 'no token', tip: AUTH_REMEDY, text: 'enforced — manager sends no token' };
    }
    if (det.bearer_ok === false) {
      return { k: 'warn', chip: 'token mismatch', tip: AUTH_REMEDY, text: 'enforced — engine rejects the manager token' };
    }
    return { k: 'ok', chip: null, tip: '', text: det.management === 'ingest_token' ? 'ingest_token (fallback)' : 'management_token' };
  }

  function svcRowHtml(s) {
    const lk = (s.lk
      ? `<span class="lk ${s.lk[1] === 'crit' ? 'crit' : (s.lk[1] === 'on' ? '' : 'off')}">${esc(s.lk[0])}</span>` : '')
      + (s.ak ? `<span class="lk warn" title="${esc(s.ak.tip)}">${esc(s.ak.chip)}</span>` : '')
      + (s.rp ? '<span class="lk warn" title="Saved settings apply after a restart">restart pending</span>' : '');
    const up = `<span class="pill ${s.upCls || ''}">`
      + (s.up ? `<span class="l">up</span>${esc(s.up)}` : esc(s.upTxt)) + '</span>';
    const detail = s.detail ? `<div class="d ${s.upCls || ''}">${esc(s.detail)}</div>` : '';
    const logBtn = s.svc
      ? `<button type="button" class="ib" data-log-svc="${esc(s.svc)}" data-tip="View ${esc(s.n)} log">${SVG_LOG}</button>`
      : '<span class="ib none"></span>';
    const btn = s.act
      ? `<button type="button" class="ib warnh" data-restart-svc="${esc(s.svc)}" data-tip="${esc(s.act)}">${SVG_RESTART}</button>`
      : '<span class="ib none"></span>';
    return `<div class="hc-svcr"><span class="dot ${s.st}"></span>`
      + `<div class="n"><span class="nt">${esc(s.n)}</span>${lk}</div><div class="v">${esc(s.ver)}</div>`
      + `<div class="up">${up}</div><span class="acts">${logBtn}${btn}</span>${detail}</div>`;
  }

  // ── data-flow edges ──────────────────────────────────────────────────
  // Each edge: {state, label, rate 0..1}. Missing counters degrade to a
  // label of "—" rather than inventing a number.
  function edgeStates(d) {
    const flow = (d && d.flow) || {};
    const df = (d && d.data_flow) || {};
    const mgr = (d && d.manager) || {};
    const streams = mgr.streams || {};
    const ae = svcOf(d, 'alarm_engine');
    const influx = svcOf(d, 'influxdb');
    const aeOk = !!(ae && ae.ok);
    const inOk = !!(influx && influx.ok);

    const pushes = ['primary_llama_push', 'primary_lms_push', 'primary_vllm_push'].map(k => df[k] || {});
    const expected = pushes.some(p => p.has_agent);
    const stale = pushes.some(p => p.has_agent && p.ok === false);

    const out = {};
    const mk = (state, label, rate) => ({ state, label, rate: rate == null ? 0 : Math.max(0, Math.min(1, rate)) });

    const push = flow.agent_pushes_per_s;
    out.eAgMg = !expected ? mk('off', '—', 0)
      : stale ? mk('warn', push != null ? `${num(push)} push/s` : 'stale', push != null ? push / 5 : 0.1)
      : push == null ? mk('ok', '—', 0.3)
      : push === 0 ? mk('crit', 'no pushes', 0)
      : mk('ok', `${num(push)} push/s`, push / 5);

    const ing = flow.ae_ingest_points_per_s;
    out.eAgAe = !aeOk ? mk('crit', 'no data', 0)
      : !expected ? mk('off', '—', 0)
      : ing == null ? mk('ok', '—', 0.3)
      : ing === 0 ? mk('crit', 'no data', 0)
      : mk('ok', `${num(ing)} metrics/s`, ing / 60);

    const hist = flow.history_req_per_s;
    out.eMgAe = !aeOk ? mk('crit', 'timeout', 0)
      : hist == null ? mk('ok', '—', 0.15)
      : mk('ok', `history ${num(hist)}/s`, hist / 2);

    const act = streams.active, lim = streams.limit;
    const near = lim ? act / lim : 0;
    out.eMgBr = act == null ? mk('off', '—', 0)
      : mk(lim && near >= 0.9 ? 'warn' : 'ok', `${act} ${act === 1 ? 'stream' : 'streams'}`, near);

    const wr = flow.influx_writes_per_s;
    out.eAeIn = !aeOk ? mk('off', '—', 0)
      : !inOk ? mk('crit', 'no writes', 0)
      : wr == null ? mk('ok', '—', 0.3)
      : mk('ok', `${num(wr)} writes/s`, wr / 60);

    return out;
  }

  // Node sub-labels; the dot/border comes from the worst incident edge.
  function nodeSubs(d) {
    const mgr = (d && d.manager) || {};
    const conns = mgr.connections || {};
    const agents = (d && d.agents) || [];
    const approved = agents.filter(a => a.status === 'approved');
    const live = approved.filter(a => a.liveness === 'live').length;
    const ae = svcOf(d, 'alarm_engine');
    const influx = svcOf(d, 'influxdb');
    const ing = ((d && d.flow) || {}).ae_ingest_points_per_s;
    const tabs = conns.browsers;
    const phones = mgr.push_subscriptions;
    let br = '—';
    if (tabs != null) {
      br = plural(tabs, 'tab', 'tabs');
      if (phones) br += ` · ${plural(phones, 'phone', 'phones')}`;
    }
    return {
      nAg: approved.length ? `${live} / ${approved.length} live` : 'none registered',
      nMg: 'this host',
      nBr: br,
      nAe: (ae && ae.ok) ? (ing != null ? `${num(ing)} points/s` : 'connected') : 'unreachable',
      nIn: influxPill(influx && (influx.state || (influx.via && !influx.ok ? 'unreachable' : null))).txt,
    };
  }

  // ── node detail strip ────────────────────────────────────────────────
  function detailRows(d, node) {
    const mgr = (d && d.manager) || {};
    const streams = mgr.streams || {};
    const conns = mgr.connections || {};
    const flow = (d && d.flow) || {};
    const df = (d && d.data_flow) || {};
    const ae = svcOf(d, 'alarm_engine');
    const influx = svcOf(d, 'influxdb');
    const agents = (d && d.agents) || [];
    const approved = agents.filter(a => a.status === 'approved');
    const dash = v => (v == null || v === '' ? '—' : String(v));

    if (node === 'agents') {
      const live = approved.filter(a => a.liveness === 'live').length;
      const stale = approved.filter(a => a.liveness === 'stale').length;
      const down = approved.filter(a => a.liveness === 'down');
      const bothTls = approved.filter(a => a.tls_direction === 'both').length;
      const roleOk = approved.filter(a => a.tls_role_checked).length;
      const age = k => {
        const p = df[k] || {};
        if (!p.has_agent) return 'no agent';
        return p.age_s == null ? 'no push' : `${Math.round(p.age_s)} s`;
      };
      const hb = typeof _adminCollectInterval !== 'undefined' && _adminCollectInterval
        ? `${_adminCollectInterval} s` : '—';
      return [
        ['live', `${live} / ${approved.length}`, live === approved.length && approved.length ? 'ok' : ''],
        ['stale', String(stale), stale ? 'warn' : ''],
        ['down', down.length ? `${down.length} · ${down.map(a => a.hostname || a.id).join(', ')}` : '0', down.length ? 'crit' : ''],
        ['heartbeat every', hb],
        ['pushes', `llama.cpp ${age('primary_llama_push')} · LM Studio ${age('primary_lms_push')} · vLLM ${age('primary_vllm_push')}`],
        ['metrics to engine', flow.ae_ingest_points_per_s != null ? `${num(flow.ae_ingest_points_per_s)} /s` : '—',
          flow.ae_ingest_points_per_s === 0 ? 'crit' : ''],
        ['TLS both ways', `${bothTls} of ${approved.length}`, bothTls === approved.length && approved.length ? 'ok' : ''],
        ['certificate role checked', `${roleOk} of ${approved.length}`, roleOk === approved.length && approved.length ? 'ok' : ''],
      ];
    }
    if (node === 'manager') {
      const near = streams.limit ? streams.active / streams.limit : 0;
      const busy = conns.worker_threads_busy, total = conns.worker_threads;
      return [
        ['version', dash(mgr.version)],
        ['up', dash(upStr(mgr.uptime_s))],
        ['streams', streams.limit != null
          ? `${streams.active} / ${streams.limit} · peak ${streams.peak} · refusals ${streams.refusals}` : '—',
          near >= 0.9 || streams.refusals ? 'warn' : ''],
        ['browsers', nodeSubs(d).nBr],
        ['agent links', dash(conns.agents)],
        ['worker threads', total != null ? `${busy} / ${total} busy` : '—',
          total && busy / total >= 0.9 ? 'warn' : ''],
        ['open routes', flow.anon_req_per_min != null
          ? `${flow.anon_req_per_min} /min · ${flow.anon_refused_per_min || 0} refused` : '—',
          flow.anon_refused_per_min ? 'warn' : ''],
        ['engine probe', ae && ae.ok ? ms(ae.latency_ms) : 'timeout', ae && ae.ok ? '' : 'crit'],
      ];
    }
    if (node === 'browsers') {
      return [
        ['dashboard tabs', dash(conns.browsers)],
        ['companion', mgr.push_subscriptions ? plural(mgr.push_subscriptions, 'phone', 'phones') : '0'],
        ['live streams', dash(streams.active)],
        ['push subscriptions', dash(mgr.push_subscriptions)],
        ['ws relay', dash(mgr.ws_relay)],
      ];
    }
    if (node === 'ae') {
      if (!ae || !ae.ok) {
        const t = (ae && ae.tls && typeof ae.tls === 'object') ? ae.tls : null;
        return [
          ['last good probe', dash(ae && ae.last_ok_at), 'crit'],
          ['failed probes', dash(ae && ae.consecutive_failures), 'crit'],
          ['serving', !t ? dash(ae && ae.url)
            : (t.enabled && !t.active ? 'cert missing → http' : (t.enabled ? 'https' : 'http')),
            t && t.enabled && !t.active ? 'crit' : ''],
          ['error', dash(ae && ae.error), 'crit'],
          ['ingest', 'unknown'],
          ['rules eval', '—'],
          ['active alerts', '—'],
        ];
      }
      const tls = (ae.tls && typeof ae.tls === 'object') ? ae.tls : null;
      const auth = authState(ae);
      const serving = !tls ? 'unknown'
        : (tls.enabled && tls.active) ? 'https' : (tls.enabled ? 'cert missing → http' : 'http');
      const rows = [
        ['version', dash(ae.version)],
        ['up', dash(upStr(ae.uptime_s))],
        ['serving', serving, serving === 'https' ? 'ok' : (serving.indexOf('cert') === 0 ? 'crit' : '')],
        ['certificate role', ae.tls_role_checked ? 'checked' : 'not yet', ae.tls_role_checked ? 'ok' : ''],
        ['auth', auth ? auth.text : 'unknown', auth ? auth.k : ''],
        ['ingest', ae.ingest_points_per_s != null ? `${num(ae.ingest_points_per_s)} points/s` : '—'],
        ['rules eval', ae.rule_eval_ms != null ? ms(ae.rule_eval_ms) : '—'],
        ['active alerts', dash(ae.active_alerts), ae.active_alerts ? 'warn' : ''],
        ['probe from manager', ms(ae.latency_ms)],
      ];
      if (ae.otlp && typeof ae.otlp === 'object') rows.splice(5, 0, ['OTLP received', otlpStr(ae.otlp)]);
      return rows;
    }
    const inOk = !!(influx && influx.ok);
    if (!inOk) {
      return [
        ['state', dash(influx && influx.state) === '—' ? 'unknown' : influx.state, 'crit'],
        ['reached via', dash(influx && influx.via) === '—' ? 'alarm engine' : influx.via, 'crit'],
      ];
    }
    return [
      ['version', dash(influx.version)],
      ['state', dash(influx.state), 'ok'],
      ['ping', ms(influx.ping_ms)],
      ['writes', influx.writes_per_s != null ? `${num(influx.writes_per_s)} /s` : '—'],
      ['reached via', dash(influx.via) === '—' ? 'alarm engine' : influx.via],
    ];
  }

  // ── warnings column ──────────────────────────────────────────────────
  const CRIT_RE = /down|unreachable|stale|EXPIRED/;

  // Ordered crit → warn → note → info; empty renders the mono "None".
  function warnRows(d, rel) {
    const rows = [];
    for (const w of ((d && d.warnings) || [])) {
      if (/^job failed: /.test(String(w))) continue;
      const crit = CRIT_RE.test(String(w));
      rows.push({ k: crit ? 'crit' : 'warn', g: crit ? '▲' : '!', t: esc(w) });
    }
    // Failed jobs come from the structured list so each row can be dismissed (#1044).
    for (const j of ((d && d.jobs && d.jobs.failed) || [])) {
      rows.push({ k: 'warn', g: '!', t: `job failed: ${esc(j.label)} — ${esc(j.message || 'failed')}`, ack: j.id });
    }
    if (rel && rel.enabled && rel.update_available === true) {
      const url = `https://github.com/${rel.repo || ''}/releases/latest`;
      rows.push({ k: 'note', g: '↑',
        t: `Manager <span class="m">${esc(rel.latest || '')}</span> is available`
          + (rel.installed ? ` <span class="m">(installed ${esc(rel.installed)})</span>` : '')
          + ` · <a href="${esc(url)}" target="_blank" rel="noopener">release notes</a>` });
    }
    const au = (d && d.agent_update) || {};
    if (au.outdated) {
      rows.push({ k: 'note', g: '↑',
        t: `${au.outdated} agent${au.outdated === 1 ? '' : 's'} can update to `
          + `<span class="m">${esc(au.latest || '')}</span> · `
          + (typeof _adminUpdateBusy !== 'undefined' && _adminUpdateBusy
            ? '<button type="button" class="lnk" data-act="updateall" disabled>Update all (running…)</button>'
            : '<button type="button" class="lnk" data-act="updateall">Update all</button>') });
    }
    for (const a of ((d && d.advisories) || [])) rows.push({ k: 'info', g: 'i', t: esc(a) });
    const info = releaseInfoText(rel);
    if (info) rows.push({ k: 'info', g: 'i', t: esc(info) });
    const rank = { crit: 0, warn: 1, note: 2, info: 3 };
    return rows.sort((a, b) => rank[a.k] - rank[b.k]);
  }

  // Same verdict logic as _adminReleaseInfoHtml; returns the message or ''.
  function releaseInfoText(rel) {
    if (typeof _adminReleaseInfoText === 'function') return _adminReleaseInfoText(rel);
    return '';
  }

  function pillOf(d, rows) {
    if ((d && d.overall) === 'down') return ['crit', 'Down'];
    return rows.some(r => r.k === 'crit') ? ['crit', 'Down']
      : rows.some(r => r.k === 'warn') ? ['warn', 'Attention'] : ['ok', 'Healthy'];
  }

  // ── jobs strip (#915) ────────────────────────────────────────────────
  function ago(s) { return upStr(s) || '0s'; }
  function jobsRows(d, nowS) {
    const j = (d && d.jobs) || {};
    const now = nowS != null ? nowS : Date.now() / 1000;
    return ((j.rows) || []).map(r => {
      const st = r.status || 'queued';
      let when = '';
      if (st === 'queued') when = r.next_run > now ? `next in ${ago(r.next_run - now)}` : 'due now';
      else if (st === 'running') when = `started ${ago(now - (r.started || now))} ago`;
      else when = `${st} ${ago(now - (r.resolved || now))} ago` + (st === 'failed' && r.message ? ` — ${r.message}` : '');
      if (st === 'queued' && r.message && /^waiting for /.test(r.message)) when = r.message;
      return { id: r.id, k: st, label: r.label || r.id, kind: r.kind_title || r.kind || '', when, by: r.user || r.source || '',
               cancel: !!r.can_cancel, ack: !!r.can_ack, acked: !!r.acked, live: st === 'queued' || st === 'running' };
    });
  }
  const DOT = { queued: 'ok', running: 'ok pulse', failed: 'crit', done: '', cancelled: '' };
  function jobsHtml(rows, more) {
    if (!rows.length) return '<div class="w-none">None</div>';
    const tail = more > 0 ? `<button type="button" class="hj-more" data-more-jobs="1">+${more} more</button>` : '';
    return rows.map(r => `<div class="hj ${r.k}${r.live ? ' live' : ''}${r.acked ? ' acked' : ''}" data-job="${esc(r.id)}"><span class="dot ${r.acked ? '' : (DOT[r.k] || '')}"></span>`
      + `<span class="hj-l" title="${esc(r.label)}">${esc(r.label)}</span><span class="hj-k">${esc(r.kind)}</span>`
      + `<span class="hj-w" title="${esc(r.when)}">${esc(r.when)}</span><span class="hj-by">${esc(r.by)}</span>`
      + (r.cancel ? `<button type="button" class="ib" data-cancel-job="${esc(r.id)}" aria-label="Cancel job">✕</button>`
        : r.ack ? `<button type="button" class="ib" data-ack-job="${esc(r.id)}" aria-label="Dismiss failed job" title="Dismiss">✕</button>`
        : '<span class="ib none"></span>')
      + '</div>').join('') + tail;
  }
  // Marks one failed job as seen; the next health poll drops it from the warnings and dims its row.
  async function ackJob(id) {
    try { await fetch(`/api/jobs/${encodeURIComponent(id)}/ack`, { method: 'POST' }); } catch (_) { /* next poll repaints */ }
    if (typeof adminLoadHealth === 'function') adminLoadHealth();
  }
  function jobsSummary(d) {
    const j = (d && d.jobs) || {};
    const parts = [];
    if (j.queued) parts.push(plural(j.queued, 'queued', 'queued'));
    if (j.running) parts.push(plural(j.running, 'running', 'running'));
    if (j.failed_24h) parts.push(`${j.failed_24h} failed today`);
    return parts.length ? parts.join(' · ') : 'none';
  }

  // ── render ───────────────────────────────────────────────────────────
  function renderDetail() {
    const host = $('adminHealthDetail');
    if (!host) return;
    const svg = $('adminHealthDataFlow');
    if (svg) svg.querySelectorAll('.hc-node').forEach(n => n.classList.toggle('sel', n.dataset.node === _selNode));
    if (!_selNode || !_last) { host.innerHTML = ''; return; }
    const rows = detailRows(_last, _selNode)
      .map(([k, v, c]) => `<dt>${esc(k)}</dt><dd class="${c || ''}">${esc(v)}</dd>`).join('');
    host.innerHTML = `<div class="hc-det-h"><span class="microlbl">${esc(NODE_NAME[_selNode])}</span>`
      + '<button type="button" class="ib" data-close aria-label="Close">✕</button></div>'
      + `<dl class="hc-kv">${rows}</dl>`;
  }

  function render(d, rel) {
    _last = d || {};
    _lastRel = rel;
    const rows = warnRows(_last, rel);

    const pill = $('adminHealthOverall');
    if (pill) {
      const [cls, txt] = pillOf(_last, rows);
      pill.className = 'pill ' + cls;
      pill.textContent = txt;
    }

    const svcEl = $('adminHealthServices');
    if (svcEl) {
      svcEl.innerHTML = svcRows(_last).map(svcRowHtml).join('');
      if (!svcEl._hcBound) {
        svcEl._hcBound = true;
        svcEl.addEventListener('click', e => {
          const lb = e.target.closest('[data-log-svc]');
          if (lb && typeof adminServiceLogs === 'function') { adminServiceLogs(lb.getAttribute('data-log-svc')); return; }
          const btn = e.target.closest('[data-restart-svc]');
          if (btn && typeof _restartService === 'function') _restartService(btn.getAttribute('data-restart-svc'));
        });
      }
    }

    const svg = $('adminHealthDataFlow');
    if (svg) {
      const edges = edgeStates(_last);
      const subs = nodeSubs(_last);
      for (const id of EDGE_IDS) {
        const e = svg.querySelector('#hc' + id);
        const l = svg.querySelector('#hcl' + id.slice(1));
        const st = edges[id];
        if (e) {
          e.setAttribute('class', 'hc-e ' + st.state);
          e.setAttribute('marker-end', MARKER[st.state]);
          e.style.setProperty('--dur', (2.2 - 1.7 * st.rate).toFixed(2) + 's');
          e.style.setProperty('--w', (1.2 + 1.3 * st.rate).toFixed(2));
          e.style.setProperty('--op', (0.55 + 0.45 * st.rate).toFixed(2));
        }
        if (l) {
          l.setAttribute('class', 'hc-el ' + (st.state === 'ok' ? '' : st.state));
          const t = l.querySelector('text');
          if (t) t.textContent = st.label;
        }
      }
      const flags = {};
      for (const [nid, eids] of Object.entries(NODE_EDGES)) {
        const n = svg.querySelector('#hc' + nid);
        if (!n) continue;
        let worst = 'ok';
        for (const eid of eids) if (WORST[edges[eid].state] > WORST[worst]) worst = edges[eid].state;
        const rw = rowsWorst(_last, n.dataset.node);
        if (rw && WORST[rw] > WORST[worst]) worst = rw;
        if (worst === 'warn' || worst === 'crit') flags[n.dataset.node] = worst;
        n.setAttribute('class', 'hc-node ' + worst + (n.dataset.node === _selNode ? ' sel' : ''));
        const sb = n.querySelector('.sb');
        if (sb) {
          sb.textContent = subs[nid]; sb.style.fontSize = '';
          sb.setAttribute('class', 'sb ' + (worst === 'ok' ? '' : worst));
          fitText(sb, Number(n.querySelector('rect').getAttribute('width')) - 30);
        }
      }
      autoSelect(flags);
      svg.querySelectorAll('.hc-node').forEach(n => n.classList.toggle('sel', n.dataset.node === _selNode));
      if (!svg._hcBound) {
        svg._hcBound = true;
        const toggleNode = n => {
          _selNode = (_selNode === n.dataset.node) ? null : n.dataset.node;
          _autoNode = null;
          if (!_selNode && n.dataset.node) _mutedNode = n.dataset.node;
          renderDetail();
        };
        svg.addEventListener('click', ev => {
          const n = ev.target.closest('.hc-node');
          if (n) toggleNode(n);
        });
        svg.addEventListener('keydown', ev => {
          if (ev.key !== 'Enter' && ev.key !== ' ') return;
          const n = ev.target.closest('.hc-node');
          if (!n) return;
          ev.preventDefault();
          toggleNode(n);
        });
      }
    }

    const warnEl = $('adminHealthWarnings');
    if (warnEl) {
      const lbl = $('adminHealthColLabel'), em = $('adminHealthColEm'), vs = $('adminHealthView');
      if (rows.length) {
        if (lbl) lbl.textContent = 'Warnings';
        if (em) em.textContent = '';
        if (vs) vs.hidden = true;
        warnEl.innerHTML = rows.map(r => `<div class="w ${r.k}${r.ack ? ' ack' : ''}"><span class="g">${r.g}</span><div>${r.t}</div>`
          + (r.ack ? `<button type="button" class="ib" data-ack-job="${esc(r.ack)}" aria-label="Dismiss failed job" title="Dismiss">✕</button>` : '')
          + '</div>').join('');
      } else {
        const [name, sub] = VIEW_LABEL[_view];
        if (lbl) lbl.textContent = name;
        if (em) em.textContent = sub;
        if (vs) { vs.hidden = false; vs.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === _view))); }
        if (_view === 'capacity') loadCapacityExtra();
        if (_view === 'trends') loadTrends();
        warnEl.innerHTML = viewHtml(_last);
      }
      if (!warnEl._hcBound) {
        warnEl._hcBound = true;
        warnEl.addEventListener('click', e => {
          if (e.target.closest('[data-act="updateall"]') && typeof adminUpdateAll === 'function') adminUpdateAll();
          const all = e.target.closest('[data-act="allevents"]');
          if (all) { e.preventDefault(); try { window.location.hash = '#audit?since_hours=24'; } catch (_) { /* harness */ } if (typeof switchSubTab === 'function') switchSubTab('admin', 'audit'); }
          const ack = e.target.closest('[data-ack-job]');
          if (ack) { ack.disabled = true; ackJob(ack.getAttribute('data-ack-job')); }
        });
      }
      const vsEl = $('adminHealthView');
      if (vsEl && !vsEl._hcBound) {
        vsEl._hcBound = true;
        vsEl.addEventListener('click', e => { const b = e.target.closest('[data-view]'); if (b) setView(b.dataset.view); });
      }
    }

    const jobsEl = $('adminHealthJobsList');
    if (jobsEl) {
      jobsEl.innerHTML = jobsHtml(jobsRows(_last), ((_last && _last.jobs) || {}).more || 0);
      const sum = $('adminHealthJobsSum');
      if (sum) sum.textContent = jobsSummary(_last);
      if (!jobsEl._hcBound) {
        jobsEl._hcBound = true;
        jobsEl.addEventListener('click', async e => {
          const ack = e.target.closest('[data-ack-job]');
          if (ack) { ack.disabled = true; ackJob(ack.getAttribute('data-ack-job')); return; }
          if (e.target.closest('[data-more-jobs]')) { if (window.JobsView) JobsView.open('live'); return; }
          const btn = e.target.closest('[data-cancel-job]');
          if (!btn) return;
          btn.disabled = true;
          try { await fetch(`/api/jobs/${encodeURIComponent(btn.getAttribute('data-cancel-job'))}/cancel`, { method: 'POST' }); } catch (_) { /* next poll repaints */ }
          if (typeof adminLoadHealth === 'function') adminLoadHealth();
        });
      }
    }

    const det = $('adminHealthDetail');
    if (det && !det._hcBound) {
      det._hcBound = true;
      det.addEventListener('click', ev => {
        if (ev.target.closest('[data-close]')) { _mutedNode = _selNode; _selNode = null; _autoNode = null; renderDetail(); }
      });
    }
    renderDetail();
  }

  window.HealthView = {
    render, svcRows, svcRowHtml, edgeStates, nodeSubs, detailRows, warnRows, pillOf, upStr, num, authState,
    jobsRows, jobsHtml, jobsSummary,
    recentHtml, budgetHtml, fmtEventTime, viewHtml, setView, view: () => _view,
    capacityHtml, capacityRows, trendsHtml, trendSeries, sparkPath, loadCapacityExtra, loadTrends,
    select: n => { _selNode = n; _autoNode = null; renderDetail(); },
    selected: () => _selNode,
  };
})();
