// Dashboards · llama.cpp, LM Studio, vLLM and Host pages on the panel kit (#1432): one renderer for every host page.
// Panels fill from js/lib/host-view.js (window.HP); history comes from /api/history?agent= and a live poll appends to it.
(function () {
  const PAGES = ['llama', 'lms', 'vllm', 'host'];
  const SUB = { llama: 'llamacpp', lms: 'lmstudio', vllm: 'vllm', host: 'host' };
  const ROWS_HIDDEN_KEY = { llama: 'llamaRowsHidden', lms: 'lmsRowsHidden', vllm: 'vllmRowsHidden', host: 'hostRowsHidden' };
  const PCT = ['kv', 'gpu-busy', 'mac-gpu', 'cpu-mem', 'disk'];
  const HW = ['ups', 'aio', 'psu', 'fans'];
  const GPU_PANELS = ['gpu-busy', 'gpu-temp', 'gpu-details'], MAC_PANELS = ['mac-power', 'mac-gpu'];
  const TONES = ['ok', 'sl', 'warn', 'bad', 'off'];
  const NONZERO = ['active_slots', 'requests_processing', 'requests_deferred'];
  const DASH = '—', MODELS_MS = 60000, HOSTS_MS = 30000, BAD_PCT = 85, GAP_MIN = 5;
  const CHART_H = 170, FLOW_ROWS = 6;

  const P = {};
  // One page's state: markup flags, the picked host, its rows and live payloads, poll and generation.
  function st(page) {
    return P[page] || (P[page] = {
      built: false, active: false, timer: null, gen: 0, run: null, busy: false,
      host: null, hostId: null, range: null, rows: [], seeded: false, live: null, state: null, models: null, modelsAt: 0,
      hosts: null, hostsFailed: false, hostsAt: 0, trackers: null, nonZero: {}, hwNone: null, pickHtml: null, last: {},
      refreshing: null, auto: null, autoPanels: null, pollMs: null, lastLlama: null, fitH: {}, place: null,
    });
  }

  const esc = (v) => kitEsc(v);
  const enc = (v) => encodeURIComponent(v);
  const empty = (text) => '<div class="ov-empty">' + esc(text) + '</div>';
  const num = (v) => (typeof v === 'number' && isFinite(v) ? Math.round(v * 10) / 10 : '');
  const lay = () => ((typeof layout !== 'undefined' && layout) || window.layout || null);
  const rootOf = (page) => document.getElementById('hp-' + page);
  const ids = (page) => HP.panelsFor(page).map(p => page + '-' + p.name);

  // The Dashboards range; while Overall shows (pinned panels) the Overall range.
  function range() {
    const ov = typeof _activeTab !== 'undefined' && _activeTab === 'overall';
    const r = +((ov ? window.ovState : window.ahState) || {}).range;
    return [6, 12, 24].includes(r) ? r : 24;
  }
  function pollMs() {
    const s = +(window._pollCfg || {}).poll_interval;
    return (s > 0 ? s : 5) * 1000;
  }
  function visible(page) {
    return !!rootOf(page)
      && (typeof _activeTab === 'undefined' || _activeTab === 'dashboard')
      && (typeof _subTabState === 'undefined' || !_subTabState || _subTabState.dashboard === SUB[page]);
  }
  // Panel ids of a page pinned to Overall, none while the Pinned row is hidden.
  function pinnedIds(page) {
    const l = lay();
    if (!l || (l.overallRowsHidden || []).includes('pinned')) return [];
    const mine = ids(page);
    return (l.overallBorrowed || []).filter(id => mine.includes(id));
  }
  function pinnedShown(page) {
    return typeof _activeTab !== 'undefined' && _activeTab === 'overall' && pinnedIds(page).length > 0;
  }
  async function getJson(url) {
    const r = await fetch(url);
    if (!r || !r.ok) throw new Error('HTTP ' + (r && r.status));
    return r.json();
  }

  // ---- Hosts and the picker ----
  // Shown names get a short id suffix when several hosts share one.
  function uniqueNames(list) {
    const count = {};
    list.forEach(h => { count[h.name] = (count[h.name] || 0) + 1; });
    return list.map(h => (count[h.name] > 1 ? Object.assign({}, h, { name: h.name + ' (' + String(h.id).slice(0, 6) + ')' }) : h));
  }
  function hostsOf(page) {
    if (page === 'host') return st('host').hosts || [];
    const list = ((window._agentsByProvider || {})[HP.PAGES[page].provider]) || [];
    return uniqueNames(list.filter(a => a && a.agent_id).map(a => ({ id: a.agent_id, hostname: a.hostname || null,
      name: HV.shortName(a.hostname || String(a.agent_id).slice(0, 8)), online: !!a.online })));
  }
  function savedId(page) {
    if (page === 'host') {
      const l = lay();
      return (l && l._selectedAgents && l._selectedAgents.system) || null;
    }
    return typeof _selectedAgent === 'function' ? _selectedAgent(HP.PAGES[page].provider) : null;
  }
  function saveSystem(id) {
    const l = lay();
    if (!l) return;
    l._selectedAgents = Object.assign({}, l._selectedAgents || {}, { system: id });
    if (typeof saveLayout === 'function') Promise.resolve(saveLayout()).catch(() => {});
  }
  // The picked host: the saved one, else the first; a saved host-page id that is gone is replaced by the first and saved.
  function pick(page) {
    const list = hostsOf(page), id = savedId(page), hit = list.find(h => h.id === id) || list[0] || null;
    if (page === 'host' && id && hit && hit.id !== id) saveSystem(hit.id);
    return hit;
  }
  async function loadHostList(force) {
    const s = st('host');
    if (!force && s.hosts && Date.now() - s.hostsAt < HOSTS_MS) return;
    s.hostsAt = Date.now();
    try {
      s.hosts = uniqueNames(HV.hostList(await getJson('/api/agents/hosts')));
      s.hostsFailed = false;
    } catch (_) {
      s.hostsFailed = true;
    }
  }
  function paintPicker(page) {
    const s = st(page), sel = rootOf(page) && rootOf(page).querySelector('select.hp-pick');
    if (!sel) return;
    const list = hostsOf(page);
    const html = list.length
      ? list.map(h => '<option value="' + esc(h.id) + '">' + esc(h.name + (h.online ? '' : ' (offline)')) + '</option>').join('')
      : '<option value="">No hosts</option>';
    if (s.pickHtml !== html) { s.pickHtml = html; sel.innerHTML = html; }
    sel.disabled = list.length <= 1;
    if (s.host && sel.value !== s.host.id) sel.value = s.host.id;
  }
  function onPick(ev) {
    const sel = ev.target, root = sel && sel.closest ? sel.closest('.hp-root') : null;
    if (!root || !sel.classList.contains('hp-pick') || !sel.value) return;
    hpSetHost(root.getAttribute('data-hp-page'), sel.value);
  }

  // ---- Markup ----
  // Builds a page's rows and panels into its root once, then dresses them and applies the saved kit layout.
  function ensure(page) {
    const s = st(page), root = rootOf(page);
    if (!root || typeof HP === 'undefined') return false;
    if (s.built) return true;
    const panels = HP.panelsFor(page);
    let h = '<div class="hp-head"><label class="hp-pick-l">Host <select class="hp-pick" disabled></select></label></div>';
    HP.ROWS.forEach(([row, title]) => {
      const list = panels.filter(p => p.row === row);
      if (!list.length) return;
      h += '<section class="row" data-row="' + page + '-' + row + '"><button type="button" class="row-hd" data-row-fold>' + esc(title)
        + '</button><div class="panels">' + list.map(p => {
          const id = page + '-' + p.name;
          return '<section class="panel' + (p.kind === 'tiles' ? ' hp-wide' : '') + '" data-card="' + id + '" data-title="'
            + esc(p.title) + '" data-kit-table><div class="p-body" id="hp-b-' + id + '"></div></section>';
        }).join('') + '</div></section>';
    });
    root.innerHTML = h;
    s.built = true;
    kitDress(root);
    kitLayout('host-' + page, root, window.mcShellStore);
    if (root._hpPick) root.removeEventListener('change', root._hpPick);
    root._hpPick = onPick;
    root.addEventListener('change', onPick);
    // Show as table or chart evens out the rows again; a reloaded script replaces the listener.
    if (root._hpTableClick) root.removeEventListener('click', root._hpTableClick);
    root._hpTableClick = (ev) => {
      if (ev.target && ev.target.closest && ev.target.closest('[data-kit-do="table"]')) setTimeout(() => { const at = holdPlace(page); fit(page); restorePlace(at); }, 0);
    };
    root.addEventListener('click', root._hpTableClick);
    return true;
  }

  // ---- Panel writes that skip a body, table or note equal to the last one written ----
  function fill(s, id, html) {
    const el = document.getElementById(id), was = s.last[id];
    if (!el || (was && was.el === el && was.html === html)) return;
    s.last[id] = { el, html };
    kitFill(id, html);
  }
  function setRows(s, id, table) {
    const t = JSON.stringify(table), el = document.getElementById(id), was = s.last['t:' + id];
    if (!el || (was && was.el === el && was.t === t)) return;
    s.last['t:' + id] = { el, t };
    kitSetRows(id, table);
  }
  function note(id, text) {
    const el = document.getElementById(id), n = el && el.closest('.panel') && el.closest('.panel').querySelector('.p-hd .p-note');
    if (n ? n.textContent === text : !text) return;
    kitNote(id, text);
  }
  // Hides a panel on its page; a panel pinned to Overall stays shown there.
  function panelHidden(page, name, on) {
    const body = document.getElementById('hp-b-' + page + '-' + name), panel = body && body.closest('.panel');
    if (!panel) return;
    const hide = !!on && !!panel.closest('.hp-root');
    if (panel.hidden !== hide) panel.hidden = hide;
  }
  // Panels hidden by the operator (layout key per page) or because the host lacks what they show.
  function applyPanels(page) {
    const l = lay(), user = (l && l[HP.HIDDEN_KEYS[page]]) || [], auto = st(page).autoPanels || {};
    HP.panelsFor(page).forEach(p => panelHidden(page, p.name, !!auto[p.name] || user.includes(page + '-' + p.name)));
  }
  function rowEl(page, row) { return rootOf(page).querySelector('section.row[data-row="' + page + '-' + row + '"]'); }
  // Rows hidden by the operator (layout key per page) or because the host has nothing for them.
  function applyRows(page, auto) {
    const root = rootOf(page), l = lay(), user = (l && l[ROWS_HIDDEN_KEY[page]]) || [];
    root.querySelectorAll('section.row[data-row]').forEach(r => {
      const hide = user.includes(r.dataset.row) || !!auto[r.dataset.row];
      if (r.hidden !== hide) r.hidden = hide;
    });
    kitRowMenus(root);
  }
  // Power hardware folds with "Nothing reported" while the host reports none of it; a later report restores the saved fold.
  function hwFold(page, none) {
    const s = st(page), row = rowEl(page, 'hw'), b = row && row.querySelector('button[data-row-fold]');
    if (!b) return;
    let n = b.querySelector('.hp-row-note');
    if (!n) { b.insertAdjacentHTML('beforeend', '<span class="hp-row-note" hidden>Nothing reported</span>'); n = b.querySelector('.hp-row-note'); }
    n.hidden = !none;
    if (s.hwNone === none) return;
    s.hwNone = none;
    kitSetRowFold(row, none || kitRowFolded('host-' + page, row.getAttribute('data-row'), window.mcShellStore) === true);
  }

  // ---- Panel bodies ----
  // Key-value rows; six or more flow into two columns in a wide panel.
  function kvHtml(rows) { return '<div class="hp-kv' + (rows.length >= FLOW_ROWS ? ' flow' : '') + '" data-kit-nosort>' + kitTable(['Field', 'Value'], rows) + '</div>'; }
  function pill(label, tone) { return '<span class="ov-pill' + (TONES.includes(tone) ? ' ' + tone : '') + '">' + esc(label) + '</span>'; }
  const lab = (text) => '<div class="ov-lab">' + esc(text) + '</div>';
  const watts = (v) => (+v || 0).toFixed(1) + ' W';
  function bytes(v) {
    if (typeof v !== 'number' || !isFinite(v)) return DASH;
    const gb = v / 1073741824;
    return gb >= 1 ? gb.toFixed(1) + ' GB' : (v / 1048576).toFixed(0) + ' MB';
  }
  function bar(v) {
    const w = Math.max(0, Math.min(100, +v || 0));
    return '<span class="hp-bar' + (v >= BAD_PCT ? ' bad' : '') + '"><i style="width:' + w.toFixed(1) + '%"></i></span>';
  }
  function tilesHtml(list) {
    return '<div class="hp-tiles">' + list.map(t => '<div class="hp-tile' + (TONES.includes(t.tone) ? ' ' + t.tone : '') + '">'
      + '<div class="hp-tl">' + esc(t.label) + '</div>' + kpiBody({ value: t.value, sub: t.sub || '' }) + '</div>').join('') + '</div>';
  }
  // The server panel in Overall's server style; LM Studio models show in their own panel.
  function serverHtml(page, p) {
    let h = '<dl class="ov-kv"><dt>State</dt><dd>' + pill(p.state.label, p.state.tone) + (p.hot ? ' ' + pill('GPU hot', 'bad') : '')
      + (p.state.detail ? ' ' + esc(p.state.detail) : '') + '</dd>'
      + p.kv.map(r => '<dt>' + esc(r[0]) + '</dt><dd' + (p.swapHigh && r[0] === 'Memory' ? ' class="ov-bad"' : '')
        + ' title="' + esc(r[1]) + '">' + esc(r[1]) + '</dd>').join('') + '</dl>';
    if (p.pills && p.pills.length) h += lab('GPU power policy') + '<div class="ov-pills">' + p.pills.map(x => pill(x.label, x.tone)).join('') + '</div>';
    if (p.power) h += lab(p.power.title) + splitHtml(p.power.split, { fmt: watts }) + (p.power.sub ? '<div class="k-sub">' + esc(p.power.sub) + '</div>' : '');
    if (page !== 'lms' && p.models && p.models.length) h += lab('Models on this server') + modelsHtml(p.models);
    if (p.hint) h += '<div class="ov-hint">' + esc(p.hint) + '</div>';
    return h;
  }
  function modelsHtml(list) {
    return '<ul class="ov-models">' + list.map(x => '<li><span class="ov-m" title="' + esc(x.title) + '">' + esc(x.label)
      + (x.sub ? '<small>' + esc(x.sub) + '</small>' : '') + '</span>' + pill(x.status, x.tone) + '</li>').join('') + '</ul>';
  }
  function coresHtml(cores) {
    const list = cores.filter(v => v != null);
    if (!list.length) return '';
    return '<div class="hp-cores">' + cores.map((v, i) => (v == null ? '' : '<div class="hp-core"><span class="hp-cn">' + i + '</span>'
      + bar(v) + '<span class="hp-cv">' + esc(HV.fmtPct(v)) + '</span></div>')).join('') + '</div>';
  }
  // Disk space bars: a Mac's data volumes only; the table view keeps every mount.
  function disksHtml(n, on) {
    const items = HP.diskBars(n).map(d => '<li><span class="hp-dm" title="' + esc(d.mount) + '">' + esc(d.mount) + '</span><span class="hp-dv">'
      + (on === false ? DASH : esc(HV.fmtPct(d.percent) + ' · ' + bytes(d.used) + ' of ' + bytes(d.total))) + '</span>'
      + (on === false ? '' : bar(d.percent)) + '</li>').join('');
    const isc = n.iscsi ? '<div class="hp-line">iSCSI: ' + esc(on === false ? DASH : [n.iscsi.state, n.iscsi.target].filter(Boolean).join(' · ') || DASH) + '</div>' : '';
    return (items ? '<ul class="hp-disks">' + items + '</ul>' : empty('No disks reported.')) + isc;
  }

  // ---- Charts ----
  // A missing value equals only a missing value.
  const same = (a, b) => (a == null ? b == null : b != null && +a === +b);
  // How many whole buckets the drawn chart is behind the new one when every drawn bucket is unchanged; 0 when not.
  function aheadBy(was, labels, series) {
    const n = labels.length, old = was.labels || [];
    if (old.length !== n || !was.series || was.series.length !== series.length
        || was.series.some((s, i) => s.name !== series[i].name)) return 0;
    for (let k = 1; k < n; k++) {
      if (old[k] !== labels[0]) continue;
      const ok = old.slice(k).every((l, i) => l === labels[i])
        && series.every((s, j) => was.series[j].values.slice(k).every((v, i) => same(v, s.values[i]))
          && (!s.avg || (was.series[j].avg || []).slice(k).every((v, i) => same(v, s.avg[i]))));
      return ok ? k : 0;
    }
    return 0;
  }
  // Time, then each series; a peak-bar series adds its average column.
  function chartTable(c) {
    const cols = ['Time'];
    c.series.forEach(x => { cols.push(x.avg ? x.name + ' peak' : x.name); if (x.avg) cols.push(x.name + ' avg'); });
    return { cols, rows: c.labels.map((l, i) => [l].concat(...c.series.map(x => (x.avg ? [num(x.values[i]), num(x.avg[i])] : [num(x.values[i])])))) };
  }
  // Draws a chart into a holder; a chart that only slid by whole buckets gets the new buckets appended. Returns its table.
  function drawChart(s, page, name, holderId) {
    const c = HP.series(page, name, s.rows, range(), Date.now());
    if (!c.series.length || c.series.every(x => x.values.every(v => v == null))) {
      fill(s, holderId, empty('Nothing in this period.'));
      return { cols: ['Time'], rows: [] };
    }
    const cid = 'hp-' + page + '-' + name, holder = document.getElementById(holderId);
    const ts = holder && holder.querySelector('.ts[data-kit-ts]'), was = ts ? kitTsData(ts) : null;
    const series = c.series.map(x => Object.assign({ name: x.name, values: x.values, slot: x.slot, legend: '' }, x.avg ? { avg: x.avg } : {}));
    const metrics = HP.chartMetrics(name), host = s.host ? s.host.hostname : null;
    const opts = Object.assign({ id: cid, labels: c.labels, series, zoom: true, keep: c.labels.length, w: kitWidth(holderId),
      height: chartHeight(s, holderId), gaps: 'break' }, HP.chartStyle(name));
    if (PCT.includes(name)) opts.ymax = 100;
    if (metrics.length) opts.thresholds = [].concat(...metrics.map(m => HP.thresholdsFor(window._alarmRules, m[0], m[1], host)));
    const k = was && JSON.stringify(was.thresholds || null) === JSON.stringify(opts.thresholds || null) ? aheadBy(was, c.labels, series) : 0;
    if (k) {
      const n = c.labels.length;
      for (let i = n - k; i < n; i++) kitTsAppend(holder, cid, c.labels[i], series.map(x => x.values[i]), series.map(x => (x.avg ? x.avg[i] : null)));
      s.last[holderId] = null;
    } else {
      const z = was && was.z, a = z ? c.labels.indexOf(z.from) : -1;
      if (a >= 0 && c.labels.indexOf(z.to) > a) opts.z = { from: z.from, to: z.to };
      fill(s, holderId, tsHtml(opts));
    }
    return chartTable(c);
  }
  // The panel's "View … in metrics" menu items, one per shown series with a stored metric of the picked host.
  function metricMenu(s, page, name, bid) {
    const body = document.getElementById(bid), panel = body && body.closest('.panel'), pop = panel && panel.querySelector('.p-pop');
    if (!pop) return;
    const links = s.host && s.host.hostname ? HP.metricLinks(page, name, s.rows) : [], key = JSON.stringify(links);
    if (pop.getAttribute('data-hp-links') === key) return;
    pop.querySelectorAll('[data-hp-metric]').forEach(b => b.remove());
    pop.insertAdjacentHTML('beforeend', links.map(l => '<button type="button" role="menuitem" data-hp-metric="' + esc(l.source + '/' + l.name) + '">'
      + esc(l.label) + '</button>').join(''));
    pop.setAttribute('data-hp-links', key);
  }
  // A "View … in metrics" item opens that metric of its page's picked host, also on a panel pinned to Overall.
  function onMetricItem(ev) {
    const b = ev.target && ev.target.closest ? ev.target.closest('.p-pop [data-hp-metric]') : null;
    const panel = b && b.closest('.panel'), page = panel && (/^(llama|lms|vllm|host)-/.exec(panel.getAttribute('data-card') || '') || [])[1];
    const host = page && P[page] && P[page].host;
    if (!host || !host.hostname || typeof focusAlarmMetric !== 'function') return;
    const v = b.getAttribute('data-hp-metric'), at = v.indexOf('/'), menu = panel.querySelector('[data-kit-menu]');
    kitCloseMenus();
    if (menu) menu.focus();
    if (panel.classList.contains('full')) kitSetFull(panel, false);
    focusAlarmMetric(host.hostname, v.slice(0, at), v.slice(at + 1));
  }
  if (window._hpOnMetricItem) document.removeEventListener('click', window._hpOnMetricItem);
  window._hpOnMetricItem = onMetricItem;
  document.addEventListener('click', onMetricItem);

  // A panel with key-value rows (or other parts) above a chart in its own holder.
  function withChart(s, page, name, topHtml) {
    const bid = 'hp-b-' + page + '-' + name, top = 'hp-t-' + page + '-' + name, ch = 'hp-c-' + page + '-' + name;
    const body = document.getElementById(bid);
    if (body && !document.getElementById(ch)) {
      s.last[bid] = null;
      kitFill(bid, '<div id="' + top + '"></div><div class="hp-chart" id="' + ch + '"></div>');
    }
    fill(s, top, topHtml);
    return drawChart(s, page, name, ch);
  }

  // ---- Equal rows ----
  // A chart's drawing height: its fitted height on the host page, else the base height.
  function chartHeight(s, holderId) {
    const el = document.getElementById(holderId);
    return el && el.closest('.hp-root') && s.fitH[holderId] > CHART_H ? s.fitH[holderId] : CHART_H;
  }
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
  function fit(page) {
    const s = st(page), root = rootOf(page);
    if (!root || !root.clientWidth) return;
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
          s.fitH[id] = h;
          if (!o || Math.abs((+o.height || CHART_H) - h) < 2) return;
          kitRedrawTs(t, Object.assign(o, { height: h }), o.w);
        });
      });
    });
  }

  // ---- Paint ----
  function infOf(page) {
    const s = st(page);
    return { live: s.live || {}, state: s.state, models: s.models, peaks: peaksOf(page), now: Date.now(), lastLlama: s.lastLlama };
  }
  function onlineOf(page) {
    const s = st(page), live = s.live || {}, state = s.state || {};
    if (page === 'host' && typeof live.online === 'boolean') return live.online;
    if (page === 'llama' && typeof state.agent_online === 'boolean') return state.agent_online;
    if (page !== 'host' && typeof live.agent_online === 'boolean') return live.agent_online;
    return s.host ? s.host.online : null;
  }
  // Every panel of a page shows one sentence, with no table and no note.
  function paintAll(page, text) {
    const s = st(page);
    HP.panelsFor(page).forEach(p => {
      const bid = 'hp-b-' + page + '-' + p.name;
      fill(s, bid, empty(text));
      setRows(s, bid, { cols: ['Field', 'Value'], rows: [] });
      note(bid, '');
    });
  }
  // Paints a page, then evens out its rows, keeping the reader's place.
  function paint(page) {
    const at = holdPlace(page);
    paintBody(page);
    if (st(page).built) fit(page);
    restorePlace(at);
    st(page).place = holdPlace(page);
  }
  // The first panel still in view is noted before a repaint and the window scrolls back by however far it moved;
  // css/hostpage.css turns the browser's own scroll anchoring off on host pages.
  function holdPlace(page) {
    const root = rootOf(page);
    if (!root || !root.clientWidth || !(window.scrollY > 0)) return null;
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
  // Notes the shown page's place as the reader scrolls; a refit returns to it. A scroll while the page width
  // differs from the noted one comes from a resize not yet refit and keeps the old place.
  function onScroll() {
    if (onScroll.due) return;
    onScroll.due = true;
    (typeof requestAnimationFrame === 'function' ? requestAnimationFrame : setTimeout)(() => {
      onScroll.due = false;
      Object.keys(P).forEach(page => {
        const s = P[page], root = rootOf(page);
        if (!s.built || !s.active || !visible(page) || !root) return;
        if (s.place && s.place.el.isConnected && s.place.w !== root.clientWidth) return;
        s.place = holdPlace(page);
      });
    });
  }
  if (window._hpOnScroll) window.removeEventListener('scroll', window._hpOnScroll);
  window._hpOnScroll = onScroll;
  window.addEventListener('scroll', onScroll, { passive: true });
  function paintBody(page) {
    const s = st(page);
    if (!s.built || !rootOf(page)) return;
    paintPicker(page);
    applyPanels(page);
    if (!s.host) {
      applyRows(page, {});
      if (page === 'host' && !s.hosts) paintAll(page, s.hostsFailed ? 'Could not load hosts.' : 'Loading…');
      else paintAll(page, 'No hosts reported yet.');
      return;
    }
    if (!s.seeded && !s.live) { paintAll(page, 'Loading…'); return; }
    const n = HP.normalize(s.live, { page }), inf = infOf(page), on = onlineOf(page), off = on === false;
    const mac = !!n.macPower, auto = {};
    // Rows and panels hide for what the host lacks only once a live sample has arrived.
    if (s.live) {
      auto[page + '-gpu'] = !HP.hasGpu(n) && !mac;
      s.auto = auto;
      const ap = {};
      GPU_PANELS.forEach(x => { ap[x] = mac; });
      MAC_PANELS.forEach(x => { ap[x] = !mac; });
      HW.forEach(x => { ap[x] = n[x] == null; });
      s.autoPanels = ap;
      applyPanels(page);
      hwFold(page, HW.every(x => n[x] == null));
    }
    applyRows(page, s.auto || {});
    const notes = HP.liveNotes(off ? null : n);
    let fanName = '';
    HP.panelsFor(page).forEach(p => {
      const bid = 'hp-b-' + page + '-' + p.name;
      let table;
      if (p.name === 'tiles') {
        const t = HP.tiles(page, n, inf, on);
        fill(s, bid, tilesHtml(t));
        table = { cols: ['Tile', 'Value'], rows: t.map(x => [x.label, x.sub ? x.value + ' · ' + x.sub : x.value]) };
      } else if (p.name === 'server') {
        const sp = HP.serverPanel(page, inf, n, on);
        fill(s, bid, sp ? serverHtml(page, sp) : empty('Nothing reported yet.'));
        table = sp ? sp.table : { cols: ['Field', 'Value'], rows: [] };
      } else if (p.name === 'models') {
        const sp = HP.serverPanel(page, inf, n, on), list = (sp && sp.models) || [];
        fill(s, bid, list.length ? modelsHtml(list) : empty('No models reported.'));
        table = { cols: ['Model', 'Status', 'Size'], rows: list.map(x => [x.label, x.status, x.sub || '']) };
      } else if (p.name === 'cpu-details') {
        const rows = HP.details(page, p.name, n, inf, on);
        fill(s, bid, kvHtml(rows) + (off ? '' : coresHtml(n.cores)));
        table = { cols: ['Field', 'Value'], rows: rows.concat(off ? [] : n.cores.map((v, i) => ['Core ' + i, HV.fmtPct(v)])) };
      } else if (p.name === 'mac-power') {
        const rows = HP.details(page, p.name, n, inf, on), m = n.macPower || {};
        const split = [['CPU', m.cpuW], ['GPU', m.gpuW], ['Neural Engine', m.aneW]].filter(r => r[1] != null).map(r => ({ label: r[0], v: r[1] }));
        fill(s, bid, (off || !split.length ? '' : splitHtml(split, { fmt: watts })) + kvHtml(rows));
        table = { cols: ['Field', 'Value'], rows };
      } else if (p.name === 'disk') {
        withChart(s, page, p.name, disksHtml(n, on));
        metricMenu(s, page, p.name, bid);
        table = { cols: ['Mount', 'Used %', 'Used', 'Total'], rows: n.disks.map(d => [d.mount, num(d.percent), bytes(d.used), bytes(d.total)]) };
      } else if (p.name === 'aio' || p.name === 'psu') {
        const rows = HP.details(page, p.name, n, inf, on);
        withChart(s, page, p.name, kvHtml(rows));
        table = { cols: ['Field', 'Value'], rows };
      } else if (p.name === 'fans') {
        const t = HP.fanTable(n), rows = off ? t.rows.map(r => [r[0]].concat(r.slice(1).map(() => DASH))) : t.rows;
        fill(s, bid, rows.length ? '<div class="hp-fans">' + kitTable(t.cols, rows) + '</div>' : empty('Nothing reported.'));
        table = { cols: t.cols, rows };
        fanName = off ? '' : t.name || '';
      } else if (p.kind === 'ts') {
        table = drawChart(s, page, p.name, bid);
        metricMenu(s, page, p.name, bid);
      } else {
        const rows = HP.details(page, p.name, n, inf, on);
        fill(s, bid, kvHtml(rows));
        table = { cols: ['Field', 'Value'], rows: rows.map(r => r.map(c => (c && typeof c === 'object' && 'full' in c ? c.full : c))) };
      }
      setRows(s, bid, table);
      note(bid, p.name === 'net' ? notes.net : p.name === 'io' ? notes.io : p.name === 'fans' ? fanName
        : p.name === 'details' ? HP.lastSeenNote(page, inf, on) : '');
    });
  }

  // ---- Rate peaks ----
  // True when the dashboard's llama selection is this host (or there is none and it is the only llama host).
  function dashHost(id) {
    const sel = typeof _selectedAgent === 'function' ? _selectedAgent('llama') : null;
    const list = ((window._agentsByProvider || {}).llama) || [];
    return sel ? sel === id : list.length === 1 && list[0].agent_id === id;
  }
  // The dashboard's llama rate peaks while they are of the picked host; slot peaks are always the page's own.
  const sharedLlama = () => typeof _llamaPeaks !== 'undefined' && _llamaPeaks && _llamaPeaks.tps && !!st('llama').host
    && dashHost(st('llama').host.id);
  // The page's own rate trackers, fed on every provider page even while the dashboard's are read.
  function ownTrackers(page) {
    const s = st(page);
    if (!s.trackers) s.trackers = { tps: LMPeaks.makeTracker(LMPeaks.RATE_WINDOW_MS), pps: LMPeaks.makeTracker(LMPeaks.RATE_WINDOW_MS) };
    return s.trackers;
  }
  function peaksOf(page) {
    const s = st(page);
    if (page === 'llama' && sharedLlama()) return Object.assign({ tps: _llamaPeaks.tps, pps: _llamaPeaks.pps }, s.nonZero);
    const t = ownTrackers(page);
    return Object.assign({ tps: t.tps, pps: t.pps }, s.nonZero);
  }
  function seedPeaks(page, rows) {
    if (page === 'host' || !rows.length) return;
    const t = ownTrackers(page), clock = LMPeaks.rowClock(rows, Date.now());
    rows.forEach(r => { t.tps.push(clock(r.ts), r[page + '_tps']); t.pps.push(clock(r.ts), r[page + '_pps']); });
  }
  function pushPeaks(page, row) {
    const s = st(page);
    if (page !== 'host') {
      const t = ownTrackers(page), now = Date.now();
      t.tps.push(now, row[page + '_tps']);
      t.pps.push(now, row[page + '_pps']);
    }
    if (page === 'llama') {
      const ll = (s.live && s.live.llama) || {};
      NONZERO.forEach(k => {
        if (typeof ll[k] === 'number' && ll[k] !== 0) s.nonZero[k] = { val: ll[k], ts: new Date().toISOString() };
      });
    }
  }

  // ---- Fetching ----
  // The dashboard's own llama sample (window._latestMetric) when it is of this host and at most two polls old.
  function sharedMetric(h) {
    const m = window._latestMetric, at = +window._latestMetricAt || 0, aid = window._latestMetricAgent || null;
    const list = ((window._agentsByProvider || {}).llama) || [];
    const mine = aid ? aid === h.id : list.length === 1 && list[0].agent_id === h.id;
    return mine && m && typeof m === 'object' && Date.now() - at <= 2 * pollMs() ? m : null;
  }
  // The page's live sample (llama also its server state, and its model list at most once a minute), each asked of the
  // picked host; true when the sample arrived.
  async function fetchLive(page, h, gen) {
    const s = st(page), id = enc(h.id), miss = () => undefined;
    if (page === 'llama') {
      const wantModels = !s.modelsAt || Date.now() - s.modelsAt >= MODELS_MS, shared = sharedMetric(h);
      const got = await Promise.all([shared || getJson('/api/metrics?agent=' + id).catch(miss), getJson('/api/llama-state?agent=' + id).catch(miss),
        wantModels ? getJson('/api/llm/models?agent=' + id).catch(miss) : undefined]);
      if (gen !== s.gen) return false;
      if (got[1] !== undefined) s.state = got[1];
      if (got[2] !== undefined) { s.models = got[2]; s.modelsAt = Date.now(); }
      if (got[0] === undefined || got[0] === s.live) return false;
      s.live = got[0];
      return true;
    }
    const url = page === 'lms' ? '/api/lmstudio/metrics?agent=' + id : page === 'vllm' ? '/api/vllm/metrics?agent=' + id
      : '/api/agents/' + id + '/system';
    const live = await getJson(url).catch(miss);
    if (gen !== s.gen || live === undefined) return false;
    s.live = live;
    return true;
  }
  // Appends the live sample as a history row, trimmed to the range, and feeds the rate peaks.
  function takeLive(page) {
    const s = st(page);
    if (!s.live) return;
    // The newest llama block with counters, for Server details while the server sleeps.
    if (page === 'llama' && HP.llamaHasCounters(s.live.llama)) s.lastLlama = { ll: s.live.llama, at: Date.now() };
    const row = HP.liveRow(page, HP.normalize(s.live, { page }), infOf(page));
    s.rows = HV.mergeRows(s.rows, [row], Date.now() - range() * 3600000);
    pushPeaks(page, row);
  }
  function resetHost(s) {
    s.rows = []; s.seeded = false; s.live = null; s.state = null; s.models = null; s.modelsAt = 0;
    s.nonZero = {}; s.hwNone = null; s.last = {}; s.auto = null; s.autoPanels = null; s.lastLlama = null;
    if (s.trackers) { s.trackers.tps.reset(); s.trackers.pps.reset(); }
  }
  // Fetches the picked host's history and live sample, then paints; answers for an older host or range are dropped.
  function load(page) {
    const s = st(page), gen = ++s.gen;
    const run = (async () => {
      if (page === 'host') await loadHostList(true);
      if (gen !== s.gen) return;
      const h = pick(page), hr = range();
      if (!h || h.id !== s.hostId || s.range !== hr) {
        // A range change on the same host keeps the last-seen llama counters; a host switch clears them.
        const seen = h && h.id === s.hostId ? s.lastLlama : null;
        resetHost(s);
        s.lastLlama = seen;
      }
      s.host = h; s.hostId = h ? h.id : null; s.range = hr;
      paint(page);
      if (!h) return;
      const want = 3 * Math.round(hr * 3600000 / OV.rangeBucketMs(hr));
      const got = await Promise.all([
        getJson('/api/history?agent=' + enc(h.id) + '&since_minutes=' + hr * 60 + '&max_rows=' + want).catch(() => null),
        fetchLive(page, h, gen),
      ]);
      if (gen !== s.gen) return;
      if (Array.isArray(got[0])) {
        const fresh = !s.seeded;
        s.rows = HV.mergeRows(got[0], s.rows, Date.now() - hr * 3600000);
        if (fresh) seedPeaks(page, got[0]);
      }
      s.seeded = true;
      if (got[1]) takeLive(page);
      paint(page);
    })().catch(e => console.warn('host page load failed', e)).finally(() => { if (s.run === run) s.run = null; });
    s.run = run;
    return run;
  }
  // The live poll: a new host pick or range reloads the page, else the live sample is appended and painted.
  function tick(page) {
    const s = st(page);
    // A page no longer shown, or whose last pinned panel left Overall, stops its poll.
    if (!(s.active && visible(page)) && !pinnedShown(page)) { if (!s.active) stopPoll(page); return; }
    if (s.timer != null && s.pollMs !== pollMs()) { stopPoll(page); startPoll(page); }
    if (document.hidden || s.run || s.busy) return;
    const gen = s.gen;
    s.busy = true;
    (async () => {
      if (page === 'host') await loadHostList(false);
      if (gen !== s.gen) return;
      const h = pick(page);
      if ((h ? h.id : null) !== s.hostId || s.range !== range()) { load(page); return; }
      if (!h) { paint(page); return; }
      const ok = await fetchLive(page, h, gen);
      if (gen !== s.gen) return;
      if (ok) takeLive(page);
      paint(page);
    })().catch(e => console.warn('host page poll failed', e)).finally(() => { s.busy = false; });
  }
  // Fetches the history since the newest cached row (at least 5 minutes) and repaints; a gap as long as the range reloads.
  function refreshOne(page) {
    const s = st(page);
    if (s.refreshing) return s.refreshing;
    if (s.run) return s.run;
    if (!s.host || !s.seeded || s.range !== range() || s.hostId !== (pick(page) || {}).id) return load(page);
    const now = Date.now(), last = s.rows.length ? Date.parse(s.rows[s.rows.length - 1].ts) : NaN;
    if (!isFinite(last) || now - last >= range() * 3600000) return load(page);
    const gen = s.gen, h = s.host, minutes = Math.max(GAP_MIN, Math.ceil((now - last) / 60000) + 1);
    const run = getJson('/api/history?agent=' + enc(h.id) + '&since_minutes=' + minutes).then(rows => {
      if (gen !== s.gen || !Array.isArray(rows)) return;
      s.rows = HV.mergeRows(s.rows, rows, Date.now() - range() * 3600000);
      paint(page);
    }).catch(e => console.warn('host page history refresh failed', e)).finally(() => { s.refreshing = null; });
    s.refreshing = run;
    return run;
  }
  function startPoll(page) {
    const s = st(page);
    if (s.timer != null) return;
    const fn = () => tick(page);
    s.pollMs = pollMs();
    s.timer = window.LivePause ? window.LivePause.every(fn, s.pollMs) : setInterval(fn, s.pollMs);
  }
  function stopPoll(page) {
    const s = st(page);
    if (s.timer != null) clearInterval(s.timer);
    s.timer = null;
  }

  // ---- Exports ----
  // Page entry: builds the page once, paints from the cache, then fetches the picked host and starts the live poll.
  function hpEnter(page) {
    if (!PAGES.includes(page) || typeof HV === 'undefined' || typeof HP === 'undefined' || !ensure(page)) return Promise.resolve();
    PAGES.forEach(p => { if (p !== page && P[p]) leaveOne(p); });
    const s = st(page);
    if (s.active) { paint(page); return s.run || Promise.resolve(); }
    s.active = true;
    paint(page);
    startPoll(page);
    return load(page);
  }
  function leaveOne(page) {
    const s = st(page);
    s.active = false;
    if (pinnedShown(page)) return;
    stopPoll(page);
    s.gen++;
    s.run = null;
  }
  // Page exit: stops each page's poll and drops answers on the way, unless a panel of it is pinned and Overall shows.
  function hpLeave() {
    Object.keys(P).forEach(leaveOne);
  }
  // Repaints the shown and pinned pages; a page whose range changed refetches its history.
  function hpRender() {
    if (typeof HP === 'undefined') return;
    Object.keys(P).forEach(page => {
      const s = P[page];
      if (!s.built || !((s.active && visible(page)) || pinnedShown(page))) return;
      if (s.range != null && s.range !== range()) { load(page); return; }
      paint(page);
    });
  }
  // Picks a host for a page: provider pages share LLM Control's pick, the host page saves its own; a shown page reloads.
  function hpSetHost(page, id) {
    if (!PAGES.includes(page) || !id) return Promise.resolve();
    if (page === 'host') saveSystem(id);
    else if (typeof _selectAgent === 'function') _selectAgent(HP.PAGES[page].provider, id);
    const s = st(page);
    return s.built && ((s.active && visible(page)) || pinnedShown(page)) ? load(page) : Promise.resolve();
  }
  // Builds and fills every page with a panel pinned to Overall, keeps its poll running there and adopts newly built panels.
  function hpPaintPinned() {
    if (typeof HP === 'undefined' || typeof HV === 'undefined') return Promise.resolve();
    let built = false;
    const runs = [];
    PAGES.forEach(page => {
      if (!pinnedIds(page).length || !rootOf(page)) return;
      if (!st(page).built) built = true;
      if (!ensure(page)) return;
      if (pinnedShown(page)) startPoll(page);
      runs.push(refreshOne(page));
    });
    if (built && typeof _activeTab !== 'undefined' && _activeTab === 'overall' && typeof adoptPinnedCards === 'function') adoptPinnedCards();
    return Promise.all(runs);
  }
  // Catch-up after a hidden or paused stretch: the shown page and pages pinned on Overall fetch their history gap.
  function hpRefresh() {
    if (typeof HP === 'undefined' || typeof HV === 'undefined') return Promise.resolve();
    return Promise.all(Object.keys(P).filter(page => P[page].built && ((P[page].active && visible(page)) || pinnedShown(page)))
      .map(refreshOne));
  }
  // Forgets a page's Power hardware fold state so the next paint folds or opens that row again.
  function hpResetFolds(page) {
    if (P[page]) P[page].hwNone = null;
  }
  // Kit refit hook: evens out the rows of each built host page under root.
  function hpFit(root) {
    Object.keys(P).forEach(page => {
      const el = rootOf(page);
      if (!(P[page].built && el && root && (root === document || root.contains(el) || el.contains(root)))) return;
      const s = P[page], at = s.place && s.place.el.isConnected ? s.place : holdPlace(page);
      fit(page);
      restorePlace(at);
      s.place = holdPlace(page);
    });
  }
  function hpPagePanelIds(page) {
    return PAGES.includes(page) && typeof HP !== 'undefined' ? ids(page) : [];
  }

  window.hpEnter = hpEnter;
  window.hpLeave = hpLeave;
  window.hpRender = hpRender;
  window.hpRefresh = hpRefresh;
  window.hpSetHost = hpSetHost;
  window.hpPaintPinned = hpPaintPinned;
  window.hpPagePanelIds = hpPagePanelIds;
  window.hpResetFolds = hpResetFolds;
  window.hpFit = hpFit;
})();
