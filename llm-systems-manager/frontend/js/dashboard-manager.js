// Byte count → "1.5 MB" (whole units from 10 up), a dash when unreadable; Admin reads it too.
function _fmtBytesShort(n) {
  if (n == null || isNaN(n)) return '—';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0; let v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
}

// Dashboards · Manager on the panel kit (#1432): Snapshot, Performance, Services and databases, Streams and Tower.
// The fetches store their payloads and mgPaint() fills the panels from js/lib/manager-view.js (window.MV).
(function () {
  const ROWS = [
    ['mgr-now', 'Snapshot', [['tiles', 'Snapshot']]],
    ['mgr-perf-row', 'Performance', [['perf', 'Manager latency'], ['ae-perf', 'Alarm engine latency'], ['influx-perf', 'InfluxDB latency']]],
    ['mgr-data', 'Services and databases', [['services', 'Services'], ['db', 'Databases']]],
    ['mgr-conn', 'Streams', [['streams', 'Connection health'], ['hosts', 'Host streams'], ['procs', 'Processes']]],
    ['mgr-tower-row', 'Tower', [['tower', 'Tower']]],
  ];
  const WIDE = ['services', 'db'];
  // Rows whose panels share the line in equal thirds.
  const THIRDS = ['mgr-perf-row'];
  const IDS = [].concat(...ROWS.map(r => r[2].map(p => 'mgr-' + p[0])));
  const SOURCES = ['processes', 'manager_self_monitor', 'influxdb'];
  // Self-monitor metrics of each chart, in its series order, and the chart's body and id.
  const PERF = {
    mgr: { bid: 'mg-b-perf', id: 'mg-ts-perf', keys: ['manager_api_latency_ms', 'manager_history_latency_ms'] },
    ae: { bid: 'mg-b-ae-perf', id: 'mg-ts-ae-perf', keys: ['ae_health_latency_ms', 'ae_ingest_latency_ms', 'ae_query_24h_latency_ms',
      'rule_eval_cycle_ms'] },
    influx: { bid: 'mg-b-influx-perf', id: 'mg-ts-influx-perf', keys: ['influx_write_latency_ms', 'influx_query_5m_latency_ms',
      'influx_query_24h_latency_ms'] },
  };
  const PERF_MS = 3600000, TOWER_MS = 60000, CHART_H = 170, KEEP = 100000;
  const FOOT = 'Pushed to the alarm engine as manager_streams / agent_streams — add rules in Events and metrics to alert on these.';
  const STATS_FAILED = 'Could not load stream stats.';
  const OPEN_ROUTES_TIP = 'Requests on the pages and routes that need no login or token, last full minute';
  const TONES = ['ok', 'sl', 'warn', 'bad', 'off'];

  // Page state: the built root, panel writes and the reader's place; the catalog, stream and host payloads;
  // the refusal tracks before and after the last stream sample; Tower and its fetch time.
  const S = {
    root: null, last: {}, fitH: {}, place: null,
    svc: null, stats: null, statsFailed: false, statsLost: false, statsAt: 0, tracksIn: null, tracks: null, agents: null,
    tower: null, insights: null, towerEnded: false, towerAt: 0, towerRun: null,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (v) => kitEsc(v);
  const empty = (text) => '<div class="ov-empty">' + esc(text) + '</div>';
  const lay = () => ((typeof layout !== 'undefined' && layout) || window.layout || null);
  const ms = (v) => (v == null ? '—' : v.toFixed(1) + ' ms');
  function onPage() {
    return typeof _activeTab !== 'undefined' && _activeTab === 'dashboard'
      && typeof _subTabState !== 'undefined' && !!_subTabState && _subTabState.dashboard === 'manager';
  }
  const onOverall = (ids) => typeof _activeTab !== 'undefined' && _activeTab === 'overall'
    && typeof _ovPinned === 'function' && ids.some(id => _ovPinned(id));
  const visible = () => !!S.root && S.root.isConnected && onPage() && !document.hidden;
  async function getJson(url) {
    try {
      const r = await fetch(url);
      return r && r.ok ? await r.json() : null;
    } catch (_) { return null; }
  }

  // ---- Markup ----
  // Builds the rows and panels into the root once, then dresses them and applies the saved kit layout; 'new' on a build.
  function ensure() {
    const root = $('mg-root');
    if (!root || typeof MV === 'undefined') return false;
    if (S.root === root && root.querySelector('section.row')) return true;
    let h = '';
    ROWS.forEach(([row, title, panels]) => {
      h += '<section class="row" data-row="' + row + '"><button type="button" class="row-hd" data-row-fold>' + esc(title)
        + '</button><div class="panels' + (THIRDS.includes(row) ? ' mg-thirds' : '') + '">' + panels.map(([name, t]) => '<section class="panel' + (WIDE.includes(name) ? ' hp-wide' : '') + '" data-card="mgr-' + name + '" data-title="'
          + esc(t) + '" data-kit-table><div class="p-body" id="mg-b-' + name + '"></div></section>').join('') + '</div></section>';
    });
    root.innerHTML = h;
    S.root = root; S.last = {}; S.fitH = {}; S.place = null;
    kitDress(root);
    if (typeof mcSoloTitles === 'function') mcSoloTitles(root);
    kitLayout('manager', root, window.mcShellStore);
    if (root._mgClick) root.removeEventListener('click', root._mgClick);
    root._mgClick = (ev) => {
      if (ev.target && ev.target.closest && ev.target.closest('[data-kit-do="table"]')) setTimeout(() => { const at = holdPlace(); fit(); restorePlace(at); }, 0);
    };
    root.addEventListener('click', root._mgClick);
    return 'new';
  }

  // ---- Panel writes that skip a body, table or note equal to the last one written ----
  function fill(id, html) {
    const el = $(id), was = S.last[id];
    if (!el || (was && was.el === el && was.html === html)) return;
    S.last[id] = { el, html };
    kitFill(id, html);
  }
  function setRows(id, table) {
    const t = JSON.stringify(table), el = $(id), was = S.last['t:' + id];
    if (!el || (was && was.el === el && was.t === t)) return;
    S.last['t:' + id] = { el, t };
    kitSetRows(id, table);
  }
  // The header note of a body's panel with its hover title; a bad or warn tone colours it.
  function note(id, text, tone, title) {
    const el = $(id), panel = el && el.closest('.panel'), n = panel && panel.querySelector('.p-hd .p-note');
    if (!(n ? n.textContent === text : !text)) kitNote(id, text);
    const now = panel && panel.querySelector('.p-hd .p-note');
    if (!now) return;
    ['bad', 'warn'].forEach(t => { if (now.classList.contains('mg-' + t) !== (tone === t)) now.classList.toggle('mg-' + t, tone === t); });
    if ((now.getAttribute('title') || '') !== (title || '')) { if (title) now.setAttribute('title', title); else now.removeAttribute('title'); }
  }
  function sentence(id, text, cols) {
    fill(id, empty(text));
    setRows(id, { cols, rows: [] });
    note(id, '');
  }
  function tableHtml(t, foot) {
    return (t.rows.length ? '<div class="mg-table">' + kitTable(t.cols, t.rows) + '</div>' : '') + (foot ? '<div class="k-sub mg-foot">' + esc(foot) + '</div>' : '');
  }
  // Rows hidden by the operator in layout.managerRowsHidden.
  function applyRows() {
    const l = lay(), user = (l && l.managerRowsHidden) || [];
    S.root.querySelectorAll('section.row[data-row]').forEach(r => {
      const hide = user.includes(r.dataset.row);
      if (r.hidden !== hide) r.hidden = hide;
    });
    kitRowMenus(S.root);
  }

  // ---- Payload readers ----
  const nowS = () => Date.now() / 1000;
  const selfGet = (k) => { const e = S.svc && S.svc.self[k]; return e ? e.value : null; };
  const selfAge = (k) => { const e = S.svc && S.svc.self[k]; return e && e.at != null ? (Date.now() - e.at) / 1000 : null; };
  const probes = () => MV.probes(selfGet, selfAge);
  // Stream view of the last sample, read with the refusal tracks it was taken with.
  const streamView = () => MV.streams(S.stats, S.agents, S.tracksIn, S.statsAt);

  // ---- Panels ----
  function tilesHtml(list) {
    return '<div class="hp-tiles">' + list.map(t => '<div class="hp-tile' + (TONES.includes(t.tone) ? ' ' + t.tone : '') + '">'
      + '<div class="hp-tl">' + esc(t.label) + '</div>' + kpiBody({ value: t.value, sub: t.sub || '' }) + '</div>').join('') + '</div>';
  }
  function paintTiles() {
    const bid = 'mg-b-tiles', cols = ['Tile', 'Value'];
    if (!S.svc || !(S.stats || S.statsFailed)) { sentence(bid, 'Loading…', cols); return; }
    const sv = S.svc, t = MV.tiles({ stats: S.stats, probes: probes(), dbBytes: MV.dbTotalBytes(sv.influx, sv.mgrDb, sv.aeDb) });
    fill(bid, tilesHtml(t));
    setRows(bid, { cols, rows: t.map(x => [x.label, x.sub ? x.value + ' · ' + x.sub : x.value]) });
  }
  function paintServices() {
    const bid = 'mg-b-services', cols = ['Host', 'Service', 'Status', 'PID', 'RSS', 'Uptime'];
    if (!S.svc) { sentence(bid, 'Loading…', cols); return; }
    const s = MV.services(S.svc.rows, nowS()), up = {}, all = {};
    s.rows.forEach(r => { all[r[0]] = (all[r[0]] || 0) + 1; up[r[0]] = (up[r[0]] || 0) + (r[2] === 'running' ? 1 : 0); });
    // Every row names its host with that host's up count.
    const t = { cols, rows: s.rows.map(r => [r[0] + ' · ' + up[r[0]] + '/' + all[r[0]] + ' up'].concat(r.slice(1))) };
    fill(bid, t.rows.length ? tableHtml(t) : empty('No services reporting yet.'));
    setRows(bid, t);
    note(bid, s.note, s.tone);
  }
  function paintDb() {
    const bid = 'mg-b-db', cols = ['Database', 'Field', 'Value'];
    if (!S.svc) { sentence(bid, 'Loading…', cols); return; }
    const rows = MV.databases(S.svc.influx, S.svc.mgrDb, S.svc.aeDb), st = MV.influxState(S.svc.influx, Date.now());
    // One Field/Value list per database, side by side under its name.
    const groups = rows.map(r => r[0]).filter((g, i, a) => a.indexOf(g) === i);
    fill(bid, '<div class="mg-dbs">' + groups.map(g => '<div class="mg-dbg"><div class="ov-lab">' + esc(g) + '</div><div class="hp-kv" data-kit-nosort>'
      + kitTable(['Field', 'Value'], rows.filter(r => r[0] === g).map(r => r.slice(1))) + '</div></div>').join('') + '</div>');
    setRows(bid, { cols, rows });
    note(bid, 'InfluxDB ' + st.text, st.tone);
  }
  function paintStreams() {
    const sb = 'mg-b-streams', hb = 'mg-b-hosts';
    // A failed poll after a good one keeps the last sample and says how old it is.
    if (S.statsFailed || !S.stats) {
      const text = S.statsFailed ? STATS_FAILED : 'Loading…';
      sentence(sb, text, ['Field', 'Value']);
      sentence(hb, text, ['Host']);
      return;
    }
    const v = streamView();
    // One bar per field, filled to its share of its limit; a field with no limit keeps an empty track.
    fill(sb, '<ol class="rank mg-meters">' + v.kv.map(([k, val, warn, share]) => '<li' + (warn ? ' class="mg-over"' : '') + '><span class="r-lab">'
      + esc(k) + '</span><span class="r-bar"><i style="width:' + (share == null ? 0 : Math.round(share * 1000) / 10) + '%"></i></span><span class="r-val">'
      + esc(val) + '</span></li>').join('') + '</ol>');
    setRows(sb, { cols: ['Field', 'Value'], rows: v.kv.map(r => [r[0], String(r[1])]) });
    if (S.statsLost) note(sb, 'last sample ' + Math.round((Date.now() - S.statsAt) / 1000) + 's ago', 'warn');
    else note(sb, v.badge.text, v.badge.tone);
    fill(hb, v.hosts.rows.length ? tableHtml(v.hosts, FOOT) : empty('No hosts yet.') + '<div class="k-sub mg-foot">' + esc(FOOT) + '</div>');
    setRows(hb, { cols: v.hosts.cols, rows: v.hosts.rows.map(r => r.map(String)) });
  }
  function paintProcs() {
    const bid = 'mg-b-procs', cols = ['Process', 'Status', 'PID', 'RSS', 'Uptime'];
    if (!S.svc) { sentence(bid, 'Loading…', cols); return; }
    const t = { cols, rows: MV.processes(S.svc.rows, nowS()) };
    fill(bid, t.rows.length ? tableHtml(t) : empty('No process data from the manager host.'));
    setRows(bid, t);
  }

  // ---- Performance charts ----
  // A chart's drawing height: its fitted height on the Manager page, else the base height.
  function chartHeight(bid) {
    const el = $(bid);
    return el && el.closest('.mg-root') && S.fitH[bid] > CHART_H ? S.fitH[bid] : CHART_H;
  }
  // True when a drawn chart's kit data holds the first n labels and values of series p.
  function drawnAs(o, p, n) {
    if (!o || !Array.isArray(o.labels) || !Array.isArray(o.series) || o.labels.length !== n || o.series.length !== p.series.length) return false;
    const same = (a, b) => (a == null ? b == null : b != null && +a === +b);
    return o.labels.every((l, i) => l === p.labels[i])
      && o.series.every((s, j) => (s.values || []).length === n && s.values.every((v, i) => same(v, p.series[j].values[i])));
  }
  // Draws a chart from window._mgPerf unless its kit data already shows the same labels and values at the holder's width.
  function drawPerf(kind) {
    const c = PERF[kind], lists = (window._mgPerf || {})[kind], holder = $(c.bid);
    if (!holder) return null;
    const p = MV.perfSeries(lists || c.keys.map(() => []), kind);
    if (!lists) { fill(c.bid, empty('Loading…')); return p; }
    if (!p.labels.length) { fill(c.bid, empty('Nothing in the last hour.')); return p; }
    const ts = holder.querySelector('.ts[data-kit-ts]');
    const o = ts && kitTsData(ts);
    if (o && o.w === kitWidth(c.bid) && drawnAs(o, p, p.labels.length)) return p;
    fill(c.bid, tsHtml({ id: c.id, labels: p.labels, series: p.series.map(s => Object.assign({}, s, { legend: '' })),
      w: kitWidth(c.bid), height: chartHeight(c.bid), keep: KEEP, gaps: 'break' }));
    return p;
  }
  // The chart's "View … in metrics" items, one per series, for the stored self-monitor metric.
  function metricMenu(kind, p) {
    const c = PERF[kind], body = $(c.bid), pop = body && body.closest('.panel') && body.closest('.panel').querySelector('.p-pop');
    if (!pop) return;
    const items = p.series.map((s, i) => [MV.PROBE_NAMES[c.keys[i]] || s.name, c.keys[i]]), key = JSON.stringify(items);
    if (pop.getAttribute('data-mg-links') === key) return;
    pop.querySelectorAll('[data-mg-metric]').forEach(b => b.remove());
    pop.insertAdjacentHTML('beforeend', items.map(([name, k]) => '<button type="button" role="menuitem" data-mg-metric="'
      + esc('manager_self_monitor/' + k) + '">' + esc('View ' + name + ' in metrics') + '</button>').join(''));
    pop.setAttribute('data-mg-links', key);
  }
  function perfTable(p) {
    return { cols: ['Time'].concat(p.series.map(s => s.name)),
             rows: p.labels.map((l, i) => [l].concat(p.series.map(s => (s.values[i] == null ? '' : Math.round(s.values[i] * 10) / 10)))) };
  }
  function paintPerf() {
    const pr = S.svc ? probes() : null;
    ['mgr', 'ae', 'influx'].forEach(kind => {
      const c = PERF[kind], p = drawPerf(kind);
      if (!p) return;
      metricMenu(kind, p);
      setRows(c.bid, perfTable(p));
      if (!pr) { note(c.bid, ''); return; }
      if (kind === 'mgr') {
        const a = pr.anon;
        note(c.bid, 'api ' + ms(pr.api) + ' · history ' + ms(pr.history) + ' · open routes '
          + (a ? a.requests + '/min' + (a.refused ? ' (' + a.refused + ' refused)' : '') : '—'), pr.tone.mgr, OPEN_ROUTES_TIP);
      } else {
        const vals = Object.values(pr[kind]).filter(v => v != null);
        note(c.bid, 'worst ' + ms(vals.length ? Math.max(...vals) : null), pr.tone[kind]);
      }
    });
  }
  // Appends a self-monitor sample newer than a chart's last point to its lists, at the newest probe time: a series whose
  // own probe is not newer than its last point gets no point there (a gap). Points older than an hour drop.
  function perfAppend(kind) {
    const c = PERF[kind], self = S.svc ? S.svc.self : {}, perf = window._mgPerf;
    if (!perf || !Array.isArray(perf[kind])) return;
    const lists = perf[kind], lastOf = (l) => (l.length ? Date.parse(l[l.length - 1].timestamp || l[l.length - 1].ts) : -Infinity);
    const fresh = c.keys.map((k, i) => !!(self[k] && self[k].at != null && lists[i] && self[k].at > lastOf(lists[i])));
    const at = Math.max(...c.keys.map((k, i) => (fresh[i] ? self[k].at : -Infinity)));
    if (!isFinite(at) || !(at > Math.max(...lists.map(lastOf)))) return;
    const holder = $(c.bid), ts = holder && holder.querySelector('.ts[data-kit-ts]');
    const before = MV.perfSeries(lists, kind), shown = !!ts && drawnAs(kitTsData(ts), before, before.labels.length);
    const values = c.keys.map((k, i) => (fresh[i] ? self[k].value : null)), stamp = new Date(at).toISOString(), cut = Date.now() - PERF_MS;
    lists.forEach((l, i) => {
      if (fresh[i]) l.push({ timestamp: stamp, value: values[i] });
      while (l.length && Date.parse(l[0].timestamp || l[0].ts) < cut) l.shift();
    });
    // The point goes onto a chart showing the lists as they were, minus the points that dropped; else the next paint redraws.
    if (!shown) return;
    const p = MV.perfSeries(lists, kind), n = p.labels.length, d = kitTsData(ts);
    d.labels.push(p.labels[n - 1]);
    d.series.forEach((x, i) => x.values.push(p.series[i].values[n - 1]));
    const over = Math.max(0, d.labels.length - n);
    d.labels.splice(0, over);
    d.series.forEach(x => x.values.splice(0, over));
    if (d.z && !(d.labels.includes(d.z.from) && d.labels.includes(d.z.to))) delete d.z;
    if (!drawnAs(d, p, n)) return;
    kitRedrawTs(ts, d, d.w);
    S.last[c.bid] = null;
  }
  // A "View … in metrics" item opens that self-monitor metric of the probing host, also on a panel pinned to Overall.
  function onMetricItem(ev) {
    const b = ev.target && ev.target.closest ? ev.target.closest('.p-pop [data-mg-metric]') : null;
    const panel = b && b.closest('.panel');
    if (!panel || typeof focusAlarmMetric !== 'function') return;
    const v = b.getAttribute('data-mg-metric'), at = v.indexOf('/'), menu = panel.querySelector('[data-kit-menu]');
    kitCloseMenus();
    if (menu) menu.focus();
    if (panel.classList.contains('full')) kitSetFull(panel, false);
    focusAlarmMetric((S.svc && S.svc.selfHost) || null, v.slice(0, at), v.slice(at + 1));
  }
  if (window._mgOnMetricItem) document.removeEventListener('click', window._mgOnMetricItem);
  window._mgOnMetricItem = onMetricItem;
  document.addEventListener('click', onMetricItem);

  // ---- Tower ----
  function paintTower() {
    const bid = 'mg-b-tower', cols = ['Field', 'Value'];
    if (typeof window.ovTowerBody !== 'function') { sentence(bid, 'Tower unavailable', cols); return; }
    if (!S.towerEnded) { sentence(bid, 'Loading…', cols); return; }
    const b = window.ovTowerBody(S.tower, S.insights);
    fill(bid, b.html);
    setRows(bid, { cols: b.cols, rows: b.rows });
    note(bid, b.note);
  }
  // Tower state and insights while the page or its pinned Tower panel shows, at most once a minute, failed attempts
  // included; a failed fetch keeps the last payload.
  function refreshTower() {
    if (!(onPage() || onOverall(['mgr-tower']))) return Promise.resolve();
    if (S.towerRun) return S.towerRun;
    if (S.towerAt && Date.now() - S.towerAt < TOWER_MS) return Promise.resolve();
    const at = Date.now();
    S.towerRun = Promise.all([getJson('/api/tower/state'), getJson('/api/tower/insights')])
      .then(got => {
        if (got[0] != null) S.tower = got[0];
        if (got[1] != null) S.insights = got[1];
        S.towerAt = at;
        S.towerEnded = true;
        mgPaint();
      })
      .finally(() => { S.towerRun = null; });
    return S.towerRun;
  }

  // ---- Equal rows and the reader's place ----
  // The last panel before a full-width one, or at the end of a box, spans the columns left empty after it.
  // Spans clear first so the column count read is the grid's own; a box with no width is left alone.
  function widen(box) {
    if (!box.clientWidth) return;
    const list = Array.from(box.children).filter(p => p.classList.contains('panel') && !p.hidden && !p.classList.contains('full'));
    list.forEach(p => { if (p.style.gridColumn) p.style.gridColumn = ''; });
    const cols = getComputedStyle(box).gridTemplateColumns.split(' ').filter(Boolean).length || 1;
    let pos = 0, prev = null;
    list.forEach(p => {
      if (p.classList.contains('hp-wide')) {
        if (prev && pos > 0) prev.style.gridColumn = 'span ' + (cols - pos + 1);
        pos = 0; prev = null;
        return;
      }
      prev = p;
      pos = (pos + 1) % cols;
    });
    if (prev && pos > 0) prev.style.gridColumn = 'span ' + (cols - pos + 1);
  }
  // The shown part of a panel: its body, or its table while Show as table is on.
  function shownPart(p) {
    const body = p.querySelector('.p-body');
    return body && body.hidden ? p.querySelector('.p-table') : body;
  }
  // Grows each chart so its panel matches the tallest panel on its line; a full-size panel keeps its own sizing.
  // Charts drawn at another width than their holder's are redrawn at it first.
  function fit() {
    const root = S.root;
    if (!root || !root.isConnected || !root.clientWidth) return;
    root.querySelectorAll('.panels').forEach(widen);
    root.querySelectorAll('.panels > .panel:not(.full) .ts[data-kit-ts]').forEach(t => {
      const o = kitTsData(t), w = t.parentNode ? Math.round(+t.parentNode.clientWidth || 0) : 0;
      if (o && o.series && w > 0 && o.w !== w) kitRedrawTs(t, o, w);
    });
    const lines = new Map();
    root.querySelectorAll('.panels > .panel').forEach(p => {
      const r = p.getBoundingClientRect(), part = shownPart(p);
      if (p.hidden || p.classList.contains('full') || !r.height || !part) return;
      const charts = part.classList.contains('p-body')
        ? Array.from(part.querySelectorAll('.ts[data-kit-ts]')).filter(t => t.parentNode && t.parentNode.id) : [];
      const extra = charts.reduce((a, t) => a + Math.max(0, (+(kitTsData(t) || {}).height || CHART_H) - CHART_H), 0);
      const key = Math.round(r.top);
      if (!lines.has(key)) lines.set(key, []);
      lines.get(key).push({ charts, natural: r.height - kitFreeHeight(part) - extra });
    });
    lines.forEach(list => {
      const tall = Math.max(...list.map(x => x.natural));
      list.forEach(x => {
        if (!x.charts.length) return;
        const h = CHART_H + Math.max(0, Math.floor((tall - x.natural) / x.charts.length));
        x.charts.forEach(t => {
          const id = t.parentNode.id, o = kitTsData(t);
          S.fitH[id] = h;
          if (!o || Math.abs((+o.height || CHART_H) - h) < 2) return;
          kitRedrawTs(t, Object.assign(o, { height: h }), o.w);
        });
      });
    });
  }
  // The first panel still in view is noted before a repaint and the window scrolls back by however far it moved.
  function holdPlace() {
    const root = S.root;
    if (!root || !root.isConnected || !root.clientWidth || !(window.scrollY > 0)) return null;
    for (const p of root.querySelectorAll('.panels > .panel')) {
      const r = p.getBoundingClientRect();
      if (!p.classList.contains('full') && r.height && r.bottom > 0) return { el: p, top: r.top, w: root.clientWidth };
    }
    return null;
  }
  // A panel hidden or folded away by the repaint is no place to return to.
  function restorePlace(at) {
    if (!at || !at.el.isConnected) return;
    const r = at.el.getBoundingClientRect();
    if (!r.height) return;
    const d = r.top - at.top;
    if (Math.abs(d) >= 1) window.scrollBy(0, d);
  }
  // Notes the shown page's place as the reader scrolls; a scroll while the page width differs from the noted one
  // comes from a resize not yet refit and keeps the old place.
  function onScroll() {
    if (onScroll.due) return;
    onScroll.due = true;
    (typeof requestAnimationFrame === 'function' ? requestAnimationFrame : setTimeout)(() => {
      onScroll.due = false;
      if (!visible()) return;
      if (S.place && S.place.el.isConnected && S.place.w !== S.root.clientWidth) return;
      S.place = holdPlace();
    });
  }
  if (window._mgOnScroll) window.removeEventListener('scroll', window._mgOnScroll);
  window._mgOnScroll = onScroll;
  window.addEventListener('scroll', onScroll, { passive: true });

  // ---- Paint ----
  // Builds the page when needed and paints every panel from the payloads, here or pinned on Overall, then evens out the
  // rows and keeps the reader's place. A page built while Overall shows hands its pinned panels to Overall.
  function mgPaint() {
    const built = ensure();
    if (!built) return;
    const at = holdPlace();
    applyRows();
    paintTiles();
    paintServices();
    paintDb();
    paintStreams();
    paintProcs();
    paintPerf();
    paintTower();
    fit();
    restorePlace(at);
    S.place = holdPlace();
    if (built === 'new' && typeof _activeTab !== 'undefined' && _activeTab === 'overall' && typeof adoptPinnedCards === 'function') adoptPinnedCards();
  }
  // Page entry: builds the page, paints from the payloads it has and refreshes Tower.
  function mgEnter() {
    mgPaint();
    return refreshTower();
  }
  // Kit refit hook: evens out the page's rows when root holds it or sits inside it.
  function mgRefit(root) {
    const el = $('mg-root');
    if (!(S.root === el && el && root && (root === document || root.contains(el) || el.contains(root)))) return;
    const at = S.place && S.place.el.isConnected ? S.place : holdPlace();
    fit();
    restorePlace(at);
    S.place = holdPlace();
  }

  // ---- Fetching ----
  // The process, self-monitor and InfluxDB catalog rows and both SQLite stats, on Dashboards › Manager or while a Manager
  // panel is pinned on Overall; the self-monitor rows also append a live point to the perf charts.
  async function fetchServicesAndInflux() {
    if (document.hidden) return;
    if (!LMSeries.isManagerSubActive(_activeTab, _subTabState) && !onOverall(IDS)) return;
    try {
      const [aeDb, mgrDb, ...results] = await Promise.all([
        fetch('/api/alarm/dbstats/sqlite').then(r => r.json()).catch(() => ({})),
        fetch('/api/admin/dbstats/sqlite').then(r => r.json()).catch(() => ({})),
        ...SOURCES.map(s => fetch(`/api/alarm/metrics?source=${encodeURIComponent(s)}&limit=500`).then(r => r.json()).catch(() => [])),
      ]);
      const rows = [].concat(...results.map(r => (Array.isArray(r) ? r : [])));
      // A catalog that fails or comes back empty after a good sample keeps that sample.
      if (!rows.length && S.svc && S.svc.rows.length) { mgPaint(); refreshTower(); return; }
      const influx = {}, self = {};
      let selfHost = null;
      rows.forEach(r => {
        if (!r || !r.metric_name) return;
        const t = r.latest_timestamp ? Date.parse(r.latest_timestamp) : NaN;
        if (r.source === 'influxdb') influx[r.metric_name] = { value: r.latest_value, at: isFinite(t) ? t : null };
        if (r.source === 'manager_self_monitor') {
          self[r.metric_name] = { value: r.latest_value, at: isFinite(t) ? t : null };
          if (r.hostname) selfHost = r.hostname;
        }
      });
      S.svc = { rows, influx, self, selfHost, aeDb: aeDb || {}, mgrDb: mgrDb || {} };
      perfAppend('mgr');
      perfAppend('ae');
      perfAppend('influx');
      mgPaint();
      refreshTower();
    } catch (e) {
      console.error('fetchServicesAndInflux:', e);
    }
  }
  // /api/agents for the host rows of the stream view, on Dashboards › Manager or while one of its panels is pinned on Overall.
  async function fetchManagerAgentsCard() {
    if (document.hidden) return;
    if (!LMSeries.isManagerSubActive(_activeTab, _subTabState) && !onOverall(['mgr-streams', 'mgr-hosts', 'mgr-tiles'])) return;
    const d = await getJson('/api/agents');
    if (d) S.agents = d;
    mgPaint();
  }
  // Stream stats for Connection health, Host streams and the tiles, on Dashboards › Manager or while one is pinned on Overall.
  async function fetchManagerStreamsCard() {
    if (document.hidden) return;
    if (!LMSeries.isManagerSubActive(_activeTab, _subTabState) && !onOverall(['mgr-streams', 'mgr-hosts', 'mgr-tiles'])) return;
    const d = await getJson('/api/admin/stream-stats');
    if (d) {
      const at = Date.now();
      S.tracksIn = S.tracks;
      S.tracks = MV.streams(d, S.agents, S.tracksIn, at).tracks;
      S.stats = d; S.statsAt = at; S.statsFailed = false; S.statsLost = false;
    } else {
      S.statsFailed = !S.stats;
      S.statsLost = !!S.stats;
    }
    mgPaint();
  }
  // Manager page panel ids in page order.
  function mgPagePanelIds() { return IDS.slice(); }

  window.mgPaint = mgPaint;
  window.mgEnter = mgEnter;
  window.mgRefit = mgRefit;
  window.mgPagePanelIds = mgPagePanelIds;
  window.fetchServicesAndInflux = fetchServicesAndInflux;
  window.fetchManagerAgentsCard = fetchManagerAgentsCard;
  window.fetchManagerStreamsCard = fetchManagerStreamsCard;
})();
