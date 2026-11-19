// Dashboards · Energy on the panel kit (#470, #1432): the window and price head, then Totals, Over time, Hosts and models
// and Snapshot. Panels fill from js/lib/energy.js (window.EN); Today's split and Power now use Overall's bodies.
(function () {
  const ROWS = [
    ['en-tot', 'Totals', [['tiles', 'Totals']]],
    ['en-time', 'Over time', [['hourly', 'Energy per step'], ['tokens', 'Tokens generated']]],
    ['en-who', 'Hosts and models', [['hosts', 'Energy by host'], ['models', 'Energy by model']]],
    ['en-now', 'Snapshot', [['split', 'Today\'s split'], ['power', 'Power now']]],
  ];
  // Price box id, summary query key, label and the summary.config key it is prefilled from.
  const PRICES = [['en-price', 'price_kwh', 'Electricity $/kWh', 'price_kwh'],
    ['en-cloud-in', 'cloud_in', 'Cloud $/Mtok in', 'cloud_price_in_per_mtok'],
    ['en-cloud-out', 'cloud_out', 'Cloud $/Mtok out', 'cloud_price_out_per_mtok']];
  const CONTROLS = ['en-window', 'en-from', 'en-to'].concat(PRICES.map(p => p[0]));
  const TZ_WINDOWS = ['today', 'ytd', 'custom'];
  const PROVIDERS = ['llama', 'lms', 'vllm'];
  const NOW_MS = 60000;
  const TONES = ['good', 'warn', 'off'];
  const CHART_H = 170;
  // Rows whose panels share the line in equal halves.
  const HALVES = ['en-time'];
  const PICK_DATES = 'Pick both dates for the custom range.';
  const FAILED = 'Could not load this window.';

  // Page state: the built root, the fetch generation, the payloads, which payloads have answered for this refresh
  // (window ones) or once (Snapshot), which window payloads failed, and panel writes.
  const S = {
    root: null, gen: 0, summary: null, hourly: null, summary1: null, aggs: { llama: null, lms: null, vllm: null },
    ended: {}, failed: {}, last: {}, fitH: {}, place: null,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (v) => kitEsc(v);
  const empty = (text) => '<div class="ov-empty">' + esc(text) + '</div>';
  const val = (id) => { const el = $(id); return el ? String(el.value || '') : ''; };
  const lay = () => ((typeof layout !== 'undefined' && layout) || window.layout || null);
  function visible() {
    return !!S.root && S.root.isConnected
      && (typeof _activeTab === 'undefined' || _activeTab === 'dashboard')
      && (typeof _subTabState === 'undefined' || !_subTabState || _subTabState.dashboard === 'energy');
  }
  async function getJson(url) {
    try {
      const r = await fetch(url);
      return r && r.ok ? await r.json() : null;
    } catch (_) { return null; }
  }

  // ---- Markup ----
  function headHtml() {
    const opts = EN.windowOptions().map(o => '<option value="' + esc(o.value) + '">' + esc(o.label) + '</option>').join('');
    const price = (p) => '<label class="en-f">' + esc(p[2]) + ' <input type="number" class="en-num" id="' + p[0] + '" min="0" step="0.01"></label>';
    return '<div class="en-head">'
      + '<label class="en-f">Window <select class="en-win" id="en-window">' + opts + '</select></label>'
      + '<label class="en-f" id="en-from-f" hidden>From <input type="date" class="en-date" id="en-from"></label>'
      + '<label class="en-f" id="en-to-f" hidden>To <input type="date" class="en-date" id="en-to"></label>'
      + PRICES.map(price).join('')
      + '<button type="button" class="en-btn" id="en-refresh">Refresh</button>'
      + '<div class="en-note" id="en-note" role="status"></div></div>';
  }
  // Builds the head, rows and panels into the root once, then dresses them and applies the saved kit layout.
  function ensure() {
    const root = $('en-root');
    if (!root || typeof EN === 'undefined') return false;
    if (S.root === root && root.querySelector('.en-head')) return true;
    let h = headHtml();
    ROWS.forEach(([row, title, panels]) => {
      h += '<section class="row" data-row="' + row + '"><button type="button" class="row-hd" data-row-fold>' + esc(title)
        + '</button><div class="panels' + (HALVES.includes(row) ? ' en-halves' : '') + '">' + panels.map(([name, t]) => '<section class="panel' + (['hosts', 'models'].includes(name) ? ' hp-wide' : '') + '" data-card="en-' + name + '" data-title="'
          + esc(t) + '" data-kit-table><div class="p-body" id="en-b-' + name + '"></div></section>').join('') + '</div></section>';
    });
    root.innerHTML = h + '<div class="en-foot" id="en-foot"></div>';
    S.root = root; S.last = {}; S.fitH = {}; S.place = null;
    kitDress(root);
    if (typeof mcSoloTitles === 'function') mcSoloTitles(root);
    kitLayout('energy', root, window.mcShellStore);
    if (root._enChange) root.removeEventListener('change', root._enChange);
    if (root._enClick) root.removeEventListener('click', root._enClick);
    root._enChange = onChange;
    root._enClick = onClick;
    root.addEventListener('change', onChange);
    root.addEventListener('click', onClick);
    return true;
  }
  function onChange(ev) {
    if (ev.target && CONTROLS.includes(ev.target.id)) enOnControlChange();
  }
  // Refresh refetches; Show as table or chart evens out the rows again.
  function onClick(ev) {
    const b = ev.target && ev.target.closest ? ev.target.closest('#en-refresh, [data-kit-do="table"]') : null;
    if (!b) return;
    if (b.id === 'en-refresh') { enRefresh(); return; }
    setTimeout(() => { const at = holdPlace(); fit(); restorePlace(at); }, 0);
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
  function note(id, text) {
    const el = $(id), n = el && el.closest('.panel') && el.closest('.panel').querySelector('.p-hd .p-note');
    if (n ? n.textContent === text : !text) return;
    kitNote(id, text);
  }
  function loading(id, cols) {
    fill(id, empty('Loading…'));
    setRows(id, { cols, rows: [] });
  }
  function failed(id, cols) {
    fill(id, empty(FAILED));
    setRows(id, { cols, rows: [] });
  }
  function setNote(msg, warn) {
    const n = $('en-note');
    if (!n) return;
    n.textContent = msg || '';
    n.classList.toggle('en-warn', !!warn);
  }
  function footnote(text) {
    const f = $('en-foot');
    if (f && f.textContent !== text) f.textContent = text;
  }
  // Rows hidden by the operator in layout.energyRowsHidden.
  function applyRows() {
    const l = lay(), user = (l && l.energyRowsHidden) || [];
    S.root.querySelectorAll('section.row[data-row]').forEach(r => {
      const hide = user.includes(r.dataset.row);
      if (r.hidden !== hide) r.hidden = hide;
    });
    kitRowMenus(S.root);
  }

  // ---- Panels ----
  function paintTotals() {
    const bid = 'en-b-tiles';
    if (!S.ended.summary) { loading(bid, ['Tile', 'Value']); note(bid, ''); footnote(''); return; }
    if (S.failed.summary) { failed(bid, ['Tile', 'Value']); note(bid, ''); footnote(''); return; }
    const d = S.summary || {}, tiles = EN.pageTiles(d), line = EN.savingsLine(d);
    fill(bid, '<div class="en-tiles">' + tiles.map(t => '<div class="en-tile' + (TONES.includes(t.tone) ? ' ' + t.tone : '') + '">'
      + '<div class="en-tl">' + esc(t.label) + '</div>' + kpiBody({ value: t.value, sub: t.sub || '' }) + '</div>').join('') + '</div>'
      + '<div class="k-sub en-save">' + esc(line) + '</div>');
    setRows(bid, { cols: ['Tile', 'Value'], rows: tiles.map(t => [t.label, t.sub ? t.value + ' · ' + t.sub : t.value])
      .concat(line ? [['Savings', line]] : []) });
    note(bid, (d.window || {}).label || '');
    footnote(EN.coverageNote(d));
  }
  function drawChart(bid, id, labels, series, joined) {
    const o = { id, labels, series, mode: 'stack', w: kitWidth(bid), height: chartHeight(bid) };
    if (joined) o.joined = true;
    fill(bid, tsHtml(o));
  }
  // Energy and tokens per step of 1 to 24 hours, so the chart keeps to 60 bars or fewer.
  function paintTime() {
    const hb = 'en-b-hourly', tb = 'en-b-tokens', cols = ['From', 'Idle Wh', 'Generating Wh', 'Tokens'];
    if (!S.ended.hourly) { loading(hb, cols); loading(tb, cols); note(hb, ''); return; }
    if (S.failed.hourly) { failed(hb, cols); failed(tb, cols); note(hb, ''); return; }
    const h = S.hourly || {}, rows = Array.isArray(h.rows) ? h.rows : [];
    const step = EN.stepHours(rows.length), e = EN.stepSeries(rows, step);
    drawChart(hb, 'en-ts-hourly', e.labels, [{ name: 'Idle', values: e.idle, slot: 5, dim: true }, { name: 'Generating', values: e.gen, slot: 2 }], true);
    drawChart(tb, 'en-ts-tokens', e.labels, [{ name: 'Tokens', values: e.tokens, slot: 1 }], false);
    setRows(hb, e.table);
    setRows(tb, e.table);
    note(hb, (step === 1 ? 'Wh per hour' : 'Wh per ' + step + ' hours')
      + (h.truncated ? ' (chart: last ' + (h.cap_days || 45) + ' days of range)' : ''));
  }
  function tablePanel(bid, t, none) {
    if (!S.ended.summary) { loading(bid, t.cols); return; }
    if (S.failed.summary) { failed(bid, t.cols); return; }
    fill(bid, t.rows.length ? '<div class="en-table">' + kitTable(t.cols, t.rows) + '</div>' : empty(none));
    setRows(bid, t);
  }
  function paintWho() {
    const d = S.summary || {};
    tablePanel('en-b-hosts', EN.hostTable(d.hosts), 'No per-host data in this window yet.');
    tablePanel('en-b-models', EN.modelTable(d.models), 'No per-model data in this window yet.');
  }
  // Today's split and Power now for all hosts, built as on Overall; Power now has no spark here.
  function paintNow() {
    const sb = 'en-b-split', pb = 'en-b-power';
    if (typeof window.ovSplitBody !== 'function' || typeof window.ovPowerBody !== 'function') return;
    if (!S.ended.split) loading(sb, ['Where', 'Wh']);
    else {
      const b = window.ovSplitBody(S.summary1, null);
      fill(sb, b.html);
      setRows(sb, { cols: b.cols, rows: b.rows });
    }
    if (!S.ended.power) { loading(pb, ['Host', 'Watts', 'Source']); note(pb, ''); return; }
    const b = window.ovPowerBody(S.aggs, window._agentsByProvider || {}, null, null);
    fill(pb, b.html);
    setRows(pb, { cols: b.cols, rows: b.rows });
    note(pb, b.note);
  }

  // ---- Equal rows ----
  // A chart's drawing height: its fitted height, else the base height.
  function chartHeight(holderId) {
    return S.fitH[holderId] > CHART_H ? S.fitH[holderId] : CHART_H;
  }
  // The last panel of a box spans the columns left empty after it; spans clear first so the column count read is the grid's own.
  function widen(box) {
    if (!box.clientWidth) return;
    const all = Array.from(box.children).filter(p => p.classList.contains('panel') && !p.hidden && !p.classList.contains('full'));
    all.forEach(p => { if (p.style.gridColumn) p.style.gridColumn = ''; });
    // Full-width panels take a line of their own and are not counted.
    const list = all.filter(p => !p.classList.contains('hp-wide'));
    const cols = getComputedStyle(box).gridTemplateColumns.split(' ').filter(Boolean).length || 1, pos = list.length % cols;
    if (list.length && pos > 0) list[list.length - 1].style.gridColumn = 'span ' + (cols - pos + 1);
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

  // ---- Paint ----
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
  if (window._enOnScroll) window.removeEventListener('scroll', window._enOnScroll);
  window._enOnScroll = onScroll;
  window.addEventListener('scroll', onScroll, { passive: true });
  // Paints every panel from the payloads, then evens out the rows, keeping the reader's place.
  function paint() {
    if (!S.root || !S.root.isConnected) return;
    const at = holdPlace();
    applyRows();
    paintTotals();
    paintTime();
    paintWho();
    paintNow();
    fit();
    restorePlace(at);
    S.place = holdPlace();
  }

  // ---- Fetching ----
  function windowParams() {
    const v = val('en-window');
    const params = new URLSearchParams(EN.windowQuery(v, { start: val('en-from'), end: val('en-to') }));
    // Only local-anchored windows resolve against the caller's timezone.
    if (TZ_WINDOWS.includes(v)) params.set('tz_offset_min', String(-new Date().getTimezoneOffset()));
    return params;
  }
  function summaryParams() {
    const params = windowParams();
    PRICES.forEach(([id, key]) => {
      const raw = val(id);
      if (raw !== '' && Number.isFinite(parseFloat(raw))) params.set(key, raw);
    });
    return params;
  }
  // Empty price boxes take the configured prices; filled ones are kept.
  function prefill(cfg) {
    const c = cfg || {};
    PRICES.forEach(([id, , , key]) => {
      const el = $(id);
      if (el && !el.value && c[key] != null) el.value = c[key];
    });
  }
  // True while Custom is picked without both dates.
  const pickPending = () => val('en-window') === 'custom' && !(val('en-from') && val('en-to'));
  // Fetches the three provider aggregates behind Power now and paints as each lands.
  function fetchNow() {
    return Promise.all(PROVIDERS.map(p => getJson('/api/fleet/' + p + '/aggregate').then(d => {
      if (!S.root) return;
      S.ended.power = true;
      S.aggs = Object.assign({}, S.aggs, { [p]: d });
      paint();
    })));
  }
  // Fetches the window summary (with prices) and its hourly energy, plus today's summary and the aggregates unless
  // windowOnly; each paints as it lands, and answers of an older refresh are dropped. An incomplete custom range fetches nothing.
  function enRefresh(windowOnly) {
    if (!ensure()) return Promise.resolve();
    if (pickPending()) {
      setNote(PICK_DATES);
      return Promise.resolve();
    }
    const gen = ++S.gen;
    const win = windowParams().toString();
    // The window panels show Loading… until this refresh's payloads land.
    S.ended.summary = false; S.ended.hourly = false;
    paint();
    const mine = (fn) => (d) => { if (gen !== S.gen) return; fn(d); paint(); };
    const summary = fetch('/api/energy/summary?' + summaryParams().toString()).then(r => r.json())
      .then(mine(d => {
        S.ended.summary = true;
        S.failed.summary = !d || !d.ok;
        S.summary = S.failed.summary ? null : d;
        if (S.failed.summary) { setNote((d && d.error) || 'Failed to load energy summary.', true); return; }
        // An answer landing while a custom range waits for its second date keeps that note.
        setNote(pickPending() ? PICK_DATES : '');
        prefill(d.config);
      }))
      .catch(e => {
        if (gen !== S.gen) return;
        S.ended.summary = true; S.failed.summary = true; S.summary = null;
        setNote('Failed to load energy summary: ' + e, true);
        paint();
      });
    const hourly = getJson('/api/energy/hourly?' + win).then(mine(d => {
      S.ended.hourly = true;
      S.failed.hourly = !(d && d.ok);
      S.hourly = S.failed.hourly ? null : d;
    }));
    if (windowOnly === true) return Promise.all([summary, hourly]);
    const today = getJson('/api/energy/summary?days=1').then(mine(d => {
      S.ended.split = true;
      S.summary1 = d;
    }));
    return Promise.all([summary, hourly, today, fetchNow()]);
  }
  // A window or price change: From and To show only for Custom, then the page refetches.
  function enOnControlChange() {
    if (!ensure()) return Promise.resolve();
    const custom = val('en-window') === 'custom';
    ['en-from-f', 'en-to-f'].forEach(id => { const el = $(id); if (el) el.hidden = !custom; });
    return enRefresh(true);
  }
  // Page entry: builds the page once, paints from the payloads it has, then refetches.
  function initEnergyTab() {
    if (!ensure()) return Promise.resolve();
    paint();
    // Power now refreshes every minute while the page shows.
    if (!S.nowTimer) S.nowTimer = setInterval(() => { if (visible()) fetchNow(); }, NOW_MS);
    return enRefresh();
  }
  // Kit refit hook: evens out the page's rows when root holds it or sits inside it.
  function enRefit(root) {
    const el = $('en-root');
    if (!(S.root === el && el && root && (root === document || root.contains(el) || el.contains(root)))) return;
    const at = S.place && S.place.el.isConnected ? S.place : holdPlace();
    fit();
    restorePlace(at);
    S.place = holdPlace();
  }

  window.initEnergyTab = initEnergyTab;
  window.enRefresh = enRefresh;
  window.enOnControlChange = enOnControlChange;
  window.enRefit = enRefit;
})();
