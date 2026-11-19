// Forecast (#1031, #1432) — Dashboard › Forecast on the panel kit and the payload snapshot for the Overall page.
// Pure helpers live in js/lib/forecast.js (window.FC); API strings go in with textContent, or kitEsc in kit panel HTML.
(function () {
  'use strict';

  const POLL_MS = 60000;          // idle cadence while the panel is on screen
  const RUN_POLL_MS = 3000;       // cadence while a run is in flight
  const DONE_FLASH_MS = 12000;    // how long the strip keeps the finished note
  const OV_STALE_MS = 60000;      // Overall snapshot refetch age
  const AE_SINCE_MAX_MIN = 43200; // alarm engine history ceiling (30 days)
  const SERIES_POINTS = 400;
  const PER_PAGE = 8;             // rows per page in every list
  const HORIZON_DAYS = 30;
  const GO_SCROLL_MS = 300;       // lets the target tab paint before scrolling to its card
  const SERIES_TTL_MS = 300000;   // a finding's cached history is refetched after this long

  const OFF_TEXT = 'Forecast looks at the last two weeks for trends — a disk filling, '
    + 'a load change, a model that keeps failing — and tells you before the alarm does.';
  const ERR_TEXT = 'Forecast is unavailable right now.';
  const VERIFIED_CLS = { code: '', 'tower+code': 'fc-vboth', model: 'fc-vunv' };
  const TILE_LABELS = ['Open findings', 'Last run', 'Next run', 'Checks ran', 'Writing model', 'Looking back'];
  // Tiles whose day-and-time value keeps the full tile size.
  const WHEN_TILES = [TILE_LABELS[1], TILE_LABELS[2]];

  let _view = null;      // last /api/forecast payload
  let _error = '';
  let _sig = null;       // payload signature, so an unchanged poll repaints nothing
  let _fetchedAt = 0;
  let _inflight = null;
  let _timer = null;
  let _runT0 = 0;        // ms the run in flight started, on this browser's clock
  let _clock = null;     // one-second ticker of the elapsed label
  let _doneUntil = 0;    // ms until which the finished note stays on the strip
  let _doneTimer = null;
  const _ui = { tab: 'open', sev: new Set(), host: '', check: '', q: '', sort: 'urgency', dir: 'asc', page: 1, sel: null,
                view: 'outlook', group: 'host', drawer: true, open: new Set() };
  const _series = {};        // finding id → [[ms, value], …]
  const _state = {};         // finding id → 'loading' | 'done' | 'error'
  const _seriesAt = {};      // finding id → ms the series was fetched
  const _charts = {};        // finding id → Chart
  const _canvas = {};        // finding id → canvas element
  const _holder = {};        // finding id → canvas wrapper
  const _note = {};          // finding id → chart note element
  // Kit page state: the dressed root, panel writes, the reader's place, and the Tower payloads with their fetch time.
  const S = { root: null, last: {}, place: null, tower: null, insights: null, towerEnded: false, towerAt: 0, towerRun: null };

  // ── DOM helpers ──
  function byId(id) { return document.getElementById(id); }
  function mk(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function wipe(node) { if (node) node.replaceChildren(); return node; }
  function toast(msg, sev) {
    if (typeof showToast === 'function') showToast('Forecast', msg, sev || 'info');
  }
  function paint(node, text) { if (node) node.textContent = text; }
  const esc = (v) => kitEsc(v);
  const empty = (text) => '<div class="ov-empty">' + esc(text) + '</div>';
  const lay = () => ((typeof layout !== 'undefined' && layout) || window.layout || null);
  async function getJson(url) {
    try {
      const r = await fetch(url);
      return r && r.ok ? await r.json() : null;
    } catch (_) { return null; }
  }

  // ── session ──
  function canOperate() {
    const r = (window._me || {}).role;
    return r === 'operator' || r === 'admin';
  }
  function isAdmin() { return !!(window._me || {}).admin_access; }

  // ── fetching and polling ──
  function panelVisible() {
    const p = byId('dash-forecast');
    return !!p && p.classList.contains('active') && p.offsetParent !== null && !document.hidden;
  }

  function schedule() {
    if (_timer) { clearTimeout(_timer); _timer = null; }
    if (!panelVisible()) return;
    const ms = _view && _view.running ? RUN_POLL_MS : POLL_MS;
    _timer = setTimeout(tick, ms);
  }

  function tick() {
    _timer = null;
    if (!panelVisible()) return;
    if (window.LivePause && LivePause.on) { schedule(); return; }
    Promise.all([refresh(), refreshTower()]).then(schedule, schedule);
  }

  // Tower state and insights for the Tower panel, fetched at most once a minute, failed attempts included;
  // a failed fetch keeps the last payload.
  function refreshTower() {
    if (S.towerRun) return S.towerRun;
    if (S.towerAt && Date.now() - S.towerAt < POLL_MS) return Promise.resolve();
    const at = Date.now();
    S.towerRun = Promise.all([getJson('/api/tower/state'), getJson('/api/tower/insights')])
      .then(got => {
        if (got[0] != null) S.tower = got[0];
        if (got[1] != null) S.insights = got[1];
        S.towerAt = at;
        S.towerEnded = true;
        if (ensure()) { const place = holdPlace(); paintTower(); fit(); restorePlace(place); }
      })
      .finally(() => { S.towerRun = null; });
    return S.towerRun;
  }

  function refresh() {
    if (_inflight) return _inflight;
    _inflight = (async () => {
      try {
        const r = await fetch('/api/forecast');
        const d = await r.json().catch(() => ({}));
        if (r.ok && d && d.ok) { noteRun(_view, d); _view = d; _error = ''; }
        else _error = (d && (d.error || d.message)) ? String(d.error || d.message) : ERR_TEXT;
      } catch (_) {
        _error = ERR_TEXT;
      }
      _fetchedAt = Date.now();
      paintPanel();
    })().finally(() => { _inflight = null; });
    return _inflight;
  }

  // ── page head ──
  // Tracks a run starting and finishing between two payloads.
  function noteRun(prev, next) {
    const run = next.run;
    if (run && run.state === 'running' && run.elapsed_s != null) _runT0 = Date.now() - run.elapsed_s * 1000;
    if (prev && prev.run && !run) {
      _doneUntil = Date.now() + DONE_FLASH_MS;
      clearTimeout(_doneTimer);
      _doneTimer = setTimeout(runBar, DONE_FLASH_MS + 50);
    }
  }

  function stopClock() {
    if (_clock) { clearInterval(_clock); _clock = null; }
  }

  // The strip while a run is queued or in flight: what it is doing, how far along, and for how long.
  function liveStrip(bar, run) {
    const st = FC.runStatus(run);
    const text = mk('div', 'fc-live-text');
    text.setAttribute('role', 'status');
    text.append(mk('b', null, st.title), mk('span', null, st.sub));
    const time = mk('span', 'fc-live-time', run.state === 'running' ? FC.elapsedLabel((Date.now() - _runT0) / 1000) : '');
    const btn = mk('button', 'mcbtn mcbtn-pri mcbtn-sm', run.state === 'queued' ? 'Starting…' : 'Running…');
    btn.type = 'button';
    btn.disabled = true;
    const track = mk('div', 'fc-bar' + (st.pct == null ? ' fc-bar-wait' : ''));
    track.setAttribute('role', 'progressbar');
    track.setAttribute('aria-label', 'Forecast run progress');
    const fill = mk('i');
    if (st.pct != null) { fill.style.width = st.pct + '%'; track.setAttribute('aria-valuenow', String(st.pct)); }
    track.append(fill);
    bar.append(mk('span', 'fc-pulse'), text, mk('span', 'fc-sp'), time, btn, track);
    stopClock();
    if (run.state === 'running') {
      _clock = setInterval(() => {
        if (!time.isConnected) { stopClock(); return; }
        time.textContent = FC.elapsedLabel((Date.now() - _runT0) / 1000);
      }, 1000);
    }
  }

  // The Forecast row's header: the error note, the run in flight, the finished flash, and Run now or Enable in Settings.
  function runBar() {
    const bar = wipe(byId('fcRunbar'));
    if (!bar) return;
    bar.classList.toggle('fc-running', !!(_view && _view.enabled && _view.run));
    // A failed refresh leaves the last findings on screen with this note.
    if (_error && _view) bar.append(mk('div', 'fc-note', _error));
    if (!_view) return;
    const v = _view;
    if (!v.enabled) {
      if (isAdmin()) bar.append(mk('span', 'fc-sp'), settingsButton());
      return;
    }
    if (v.run) { liveStrip(bar, v.run); return; }
    stopClock();
    bar.append(mk('span', 'fc-sp'));
    if (Date.now() < _doneUntil) {
      bar.append(mk('span', 'fc-done', '✓ Finished' + (v.last_result ? ' — ' + v.last_result : '')));
    }
    if (canOperate()) {
      const btn = mk('button', 'mcbtn mcbtn-pri mcbtn-sm', 'Run now');
      btn.type = 'button';
      btn.addEventListener('click', () => runNow(btn));
      bar.append(btn);
    }
  }

  function settingsButton() {
    const btn = mk('button', 'mcbtn mcbtn-ghost mcbtn-sm', 'Enable in Settings');
    btn.type = 'button';
    btn.addEventListener('click', openSettings);
    return btn;
  }

  function openSettings() {
    if (typeof switchTab === 'function') switchTab('admin');
    if (typeof switchSubTab === 'function') switchSubTab('admin', 'settings');
    if (typeof adminSettingsOpenGroup === 'function') adminSettingsOpenGroup('forecast');
  }

  async function runNow(btn) {
    btn.disabled = true;
    _doneUntil = 0;
    if (_view) { _view.run = { state: 'queued' }; _view.running = true; _sig = null; runBar(); }
    try {
      const r = await fetch('/api/forecast/run', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        toast('Forecast started — findings appear as the checks finish.');
      } else {
        toast((d && (d.error || d.message)) || 'Could not start a forecast run.', 'warning');
      }
    } catch (_) {
      toast('Could not start a forecast run.', 'warning');
    }
    await refresh();
    schedule();
  }

  async function dismiss(id) {
    let ok = false;
    try {
      const r = await fetch('/api/forecast/' + encodeURIComponent(id) + '/dismiss', { method: 'POST' });
      ok = r.ok;
    } catch (_) { ok = false; }
    if (!ok) { toast('Could not dismiss that finding.', 'warning'); return; }
    if (_ui.sel === id) _ui.sel = null;
    _sig = null;
    await refresh();
  }

  // ── findings ──
  function sevChip(sev) {
    const s = FC.sevLabel(sev);
    return mk('span', 'fc-sev fc-' + s.cls, (s.icon ? s.icon + ' ' : '') + s.text);
  }

  function factList(f) {
    const dl = mk('dl', 'fc-facts');
    FC.facts(f, Date.now()).forEach(([k, v]) => {
      dl.append(mk('dt', null, k), mk('dd', null, v));
    });
    return dl;
  }

  function button(cls, text, onClick) {
    const b = mk('button', cls, text);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }

  function towerReady() {
    const btn = byId('towerBtn');
    return typeof window.towerAsk === 'function' && !!btn && !btn.hidden;
  }
  function askTower(text) {
    window.towerAsk(text, { tab: 'dashboard', sub: 'forecast' });
  }

  // Opens the conversation Tower held for a finding's check, read-only.
  async function openThread(tid) {
    const ok = await window.towerOpenThread(tid);
    if (!ok) toast('That conversation is no longer kept.', 'warning');
  }

  function goTo(t) {
    if (typeof switchTab === 'function') switchTab(t.tab);
    if (t.sub && typeof switchSubTab === 'function') switchSubTab(t.tab, t.sub);
    if (!t.anchor) return;
    setTimeout(() => {
      const n = byId(t.anchor);
      if (n) n.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }, GO_SCROLL_MS);
  }

  function openRows() {
    const v = _view;
    if (!v || !v.enabled) return [];
    const kept = FC.filterFindings(v.findings || [], { sev: [..._ui.sev], host: _ui.host, check: _ui.check, q: _ui.q });
    return FC.sortBy(kept, _ui.sort, _ui.dir);
  }

  // The finding shown in the detail pane: the picked one while it is listed, else the page's first.
  function selected(rows, page) {
    return rows.find(f => f.id === _ui.sel) || page.rows[0] || null;
  }

  // ── Tower's analysis ──
  function renderBrief() {
    const box = byId('fcBrief');
    if (!box) return;
    const v = _view || {};
    const gone = v.enabled && !v.digest ? FC.discardedNote(v.tower) : '';
    const text = v.enabled ? (v.digest || gone) : '';
    box.hidden = !text;
    if (!text) return;
    const sum = wipe(byId('fcBriefSum'));
    sum.append(mk('span', 'fc-brief-k', "Tower's analysis"), mk('span', 'fc-brief-short', FC.shortDigest(text, 220)));
    const body = wipe(byId('fcBriefBody'));
    body.append(mk('p', gone ? 'fc-dim' : null, text));
    const foot = mk('div', 'fc-brief-foot');
    const note = FC.digestNote(v.tower);
    if (note) foot.append(mk('span', 'fc-dim', note));
    foot.append(mk('span', 'fc-sp'));
    if (towerReady()) {
      foot.append(button('mcbtn mcbtn-pri mcbtn-sm', 'Ask Tower about this run', () => askTower(FC.askRunText(_view))));
    }
    body.append(foot);
    if (!box._fcBound) {
      box._fcBound = true;
      box.open = pref('fcBriefOpen', '1') === '1';
      box.addEventListener('toggle', () => setPref('fcBriefOpen', box.open ? '1' : '0'));
    }
  }

  function pref(key, fallback) {
    try { return localStorage.getItem(key) ?? fallback; } catch (_) { return fallback; }
  }
  function setPref(key, value) {
    try { localStorage.setItem(key, value); } catch (_) { /* storage unavailable */ }
  }

  // ── kit panel writes that skip a body, table or note equal to the last one written ──
  function fill(id, html) {
    const el = byId(id), was = S.last[id];
    if (!el || (was && was.el === el && was.html === html)) return;
    S.last[id] = { el, html };
    kitFill(id, html);
  }
  function setRows(id, table) {
    const t = JSON.stringify(table), el = byId(id), was = S.last['t:' + id];
    if (!el || (was && was.el === el && was.t === t)) return;
    S.last['t:' + id] = { el, t };
    kitSetRows(id, table);
  }
  // The header note of a body's panel; warn colours it.
  function note(id, text, warn) {
    const el = byId(id), panel = el && el.closest('.panel'), n = panel && panel.querySelector('.p-hd .p-note');
    if (!(n ? n.textContent === text : !text)) kitNote(id, text);
    const now = panel && panel.querySelector('.p-hd .p-note');
    if (now && now.classList.contains('fc-warn') !== !!warn) now.classList.toggle('fc-warn', !!warn);
  }

  // ── tiles ──
  function countState(checks, state) { return checks.filter(c => c.state === state).length; }
  // Label, value, sub line and tone of each tile; while Forecast is off, every tile but Last run says Off.
  function tileList(v) {
    const now = Date.now(), f = v.findings || [], checks = v.checks || [];
    if (!v.enabled) {
      return TILE_LABELS.map(label => ({ label, value: label === TILE_LABELS[1] ? (FC.whenLabel(v.last_run, now) || 'Never') : 'Off', sub: '' }));
    }
    const has = (sev) => f.some(x => String(x.severity || '').toLowerCase() === sev);
    return [
      { label: TILE_LABELS[0], value: String(f.length), sub: '', tone: has('critical') ? 'bad' : (has('warning') ? 'warn' : '') },
      { label: TILE_LABELS[1], value: FC.whenLabel(v.last_run, now) || 'Never', sub: '' },
      { label: TILE_LABELS[2], value: FC.whenLabel(v.next_run, now) || 'Not scheduled', sub: '' },
      { label: TILE_LABELS[3], value: countState(checks, 'ok') + ' / ' + checks.length,
        sub: countState(checks, 'collecting') + ' collecting · ' + countState(checks, 'failed') + ' could not run' },
      { label: TILE_LABELS[4], value: (v.tower && v.tower.model) || 'Off', sub: FC.towerLine(v.tower) },
      { label: TILE_LABELS[5], value: v.window_days ? v.window_days + ' days' : '—', sub: '' },
    ];
  }
  function renderTiles() {
    const bid = 'fc-b-tiles', cols = ['Tile', 'Value'];
    if (!_view) { fill(bid, empty(_error || 'Loading…')); setRows(bid, { cols, rows: [] }); return; }
    const tiles = tileList(_view);
    fill(bid, '<div class="fc-tiles">' + tiles.map(t => '<div class="fc-tile' + (t.tone ? ' ' + t.tone : '') + (WHEN_TILES.includes(t.label) ? ' fc-tw' : '') + '">'
      + '<div class="fc-tl">' + esc(t.label) + '</div>' + kpiBody({ value: t.value, sub: t.sub }) + '</div>').join('') + '</div>');
    setRows(bid, { cols, rows: tiles.map(t => [t.label, t.sub ? t.value + ' · ' + t.sub : t.value]) });
  }

  // ── checks ──
  function renderChecks() {
    const bid = 'fc-b-checks', cols = ['Check', 'State', 'Detail'];
    const host = wipe(byId('fcChecks'));
    if (!host) return;
    const v = _view, checks = (v && v.checks) || [];
    const msg = !v ? (_error || 'Loading…') : (checks.length ? '' : 'No checks yet');
    if (msg) { host.append(mk('div', 'ov-empty', msg)); setRows(bid, { cols, rows: [] }); note(bid, ''); return; }
    const collecting = countState(checks, 'collecting'), failed = countState(checks, 'failed');
    const parts = [countState(checks, 'ok') + ' ran'];
    if (collecting) parts.push(collecting + ' still collecting data');
    if (failed) parts.push(failed + ' could not run');
    note(bid, checks.length + ' checks · ' + parts.join(', '), failed > 0);
    checks.forEach(c => {
      const st = FC.checkState(c);
      const chip = mk('div', 'fc-cstate fc-' + st.cls);
      chip.append(mk('span', 'fc-dot'), document.createTextNode(c.title || ''));
      if (st.note) chip.append(mk('small', null, st.note));
      host.append(chip);
    });
    setRows(bid, { cols, rows: checks.map(c => [c.title || '', c.state || '', FC.checkState(c).note]) });
  }

  // ── Tower ──
  function paintTower() {
    const bid = 'fc-b-tower';
    if (!byId(bid)) return;
    if (typeof window.ovTowerBody !== 'function') {
      fill(bid, empty('Tower unavailable'));
      setRows(bid, { cols: ['Field', 'Value'], rows: [] });
      note(bid, '');
      return;
    }
    if (!S.towerEnded) { fill(bid, empty('Loading…')); setRows(bid, { cols: ['Field', 'Value'], rows: [] }); note(bid, ''); return; }
    const b = window.ovTowerBody(S.tower, S.insights);
    fill(bid, b.html);
    setRows(bid, { cols: b.cols, rows: b.rows });
    note(bid, b.note);
  }

  // ── tabs ──
  function renderTabs() {
    const bar = wipe(byId('fcTabs'));
    if (!bar) return;
    const v = _view;
    bar.hidden = !(v && v.enabled);
    if (bar.hidden) return;
    const res = FC.resolvedTabs(v.cleared);
    [['open', 'Open', (v.findings || []).length], ['cleared', 'Cleared', res.cleared.length],
     ['dismissed', 'Dismissed', res.dismissed.length]].forEach(([key, label, n]) => {
      const b = button('fc-tab', label, () => { _ui.tab = key; _ui.page = 1; paintList(); });
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(_ui.tab === key));
      b.append(mk('span', 'fc-n', String(n)));
      bar.append(b);
    });
    bar.append(mk('span', 'fc-sp'));
    const seg = mk('div', 'fc-seg');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', 'View');
    [['outlook', 'Outlook'], ['briefing', 'Briefing']].forEach(([key, label]) => {
      const b = button('fc-seg-btn', label, () => {
        _ui.view = key; _ui.page = 1; setPref('fcView', key); paintList();
      });
      b.setAttribute('aria-pressed', String(_ui.view === key));
      seg.append(b);
    });
    bar.append(seg);
  }

  // ── 30-day horizon ──
  function renderHorizon(rows) {
    const box = wipe(byId('fcHorizon'));
    if (!box) return;
    const h = FC.horizon(rows, Date.now(), HORIZON_DAYS);
    box.hidden = _ui.tab !== 'open' || _ui.view !== 'outlook' || !rows.length;
    if (box.hidden) return;
    const cap = mk('div', 'fc-hz-cap');
    cap.append(mk('b', null, 'Next ' + HORIZON_DAYS + ' days'),
               mk('span', null, h.dated.length
                 ? h.dated.length + (h.dated.length === 1 ? ' finding has' : ' findings have') + ' a predicted date'
                 : 'No finding has a predicted date'));
    const line = mk('div', 'fc-hz-line');
    const track = mk('div', 'fc-hz-track');
    [['fc-hz-crit', 3], ['fc-hz-warn', 11], ['fc-hz-info', 16]].forEach(([cls, days]) => {
      const z = mk('span', cls);
      z.style.flex = String(days);
      track.append(z);
    });
    line.append(track);
    [[0, 'Today'], [3, '3 days'], [7, '1 week'], [14, '2 weeks'], [30, '30 days']].forEach(([d, label]) => {
      const t = mk('span', 'fc-hz-tick' + (d === 30 ? ' fc-hz-end' : ''), label);
      t.style.left = (d / HORIZON_DAYS * 100) + '%';
      line.append(t);
    });
    let last = -100;
    h.dated.forEach((d, i) => {
      const pct = d.days / h.span * 100;
      const dot = button('fc-hz-dot fc-' + FC.sevLabel(d.sev).cls + (d.id === _ui.sel ? ' on' : '')
        + (pct - last < 9 && i % 2 ? ' fc-hz-low' : '') + (pct < 8 ? ' fc-hz-l' : (pct > 92 ? ' fc-hz-r' : '')),
        '', () => pick(d.id));
      last = pct;
      dot.style.left = pct + '%';
      dot.dataset.id = String(d.id);
      dot.title = (d.host ? d.host + ' — ' : '') + d.summary + (d.beyond ? ' (beyond ' + h.span + ' days)' : '');
      dot.setAttribute('aria-label', dot.title);
      dot.append(mk('span', 'fc-hz-host', d.host || '—'));
      line.append(dot);
    });
    const side = mk('div', 'fc-hz-side');
    side.append(mk('b', null, String(h.ongoing)), mk('span', null, 'ongoing, no date'));
    const wrap = mk('div', 'fc-hz-wrap');
    wrap.append(line, side);
    box.append(cap, wrap);
  }

  // Selects a finding and turns to the page that lists it.
  function pick(id) {
    _ui.sel = id;
    const i = openRows().findIndex(f => f.id === id);
    if (i >= 0) _ui.page = Math.floor(i / PER_PAGE) + 1;
    paintList();
  }

  // ── toolbar ──
  function select(label, options, value, onChange) {
    const sel = mk('select', 'fc-sel');
    sel.setAttribute('aria-label', label);
    options.forEach(([val, text]) => {
      const o = mk('option', null, text);
      o.value = val;
      if (val === value) o.selected = true;
      sel.append(o);
    });
    sel.addEventListener('change', () => onChange(sel.value));
    return sel;
  }

  function renderToolbar() {
    const bar = wipe(byId('fcToolbar'));
    if (!bar) return;
    const v = _view;
    bar.hidden = !(v && v.enabled) || _ui.tab !== 'open' || !(v.findings || []).length;
    if (bar.hidden) return;
    const fa = FC.facets(v.findings);
    ['critical', 'warning', 'info'].forEach(sev => {
      const chip = button('fc-fchip', FC.sevLabel(sev).text, () => {
        if (_ui.sev.has(sev)) _ui.sev.delete(sev); else _ui.sev.add(sev);
        _ui.page = 1;
        paintList();
      });
      chip.setAttribute('aria-pressed', String(_ui.sev.has(sev)));
      chip.append(mk('span', 'fc-n', String(fa.sev[sev])));
      bar.append(chip);
    });
    const reset = fn => val => { fn(val); _ui.page = 1; paintList(); };
    bar.append(select('Host', [['', 'All hosts']].concat(fa.hosts.map(h => [h, h])), _ui.host, reset(x => { _ui.host = x; })));
    bar.append(select('Check', [['', 'All checks']].concat(fa.checks.map(c => [c, c])), _ui.check, reset(x => { _ui.check = x; })));
    if (_ui.view === 'outlook') {
      const d = button('fc-fchip', 'Details', () => { setDrawer(!_ui.drawer); paintList(); });
      d.setAttribute('aria-pressed', String(_ui.drawer));
      d.title = _ui.drawer ? 'Hide the details drawer' : 'Show the details drawer';
      bar.append(d);
    }
    if (_ui.view === 'briefing') {
      bar.append(select('Sort', [['urgency', 'Most urgent first'], ['when', 'Soonest date first'], ['host', 'Host A to Z'],
                                 ['conf', 'Highest confidence']], _ui.sort, reset(x => { _ui.sort = x; _ui.dir = 'asc'; })));
      bar.append(select('Group by', [['host', 'Group by host'], ['check', 'Group by check'], ['', 'No grouping']],
                        _ui.group, reset(x => { _ui.group = x; })));
    }
    bar.append(mk('span', 'fc-sp'));
    const q = mk('input', 'fc-q');
    q.type = 'search';
    q.placeholder = 'Search findings';
    q.setAttribute('aria-label', 'Search findings');
    q.value = _ui.q;
    q.addEventListener('input', () => { _ui.q = q.value; _ui.page = 1; paintRows(); });
    bar.append(q);
  }

  // ── list + detail ──
  function sortHead(key, label, cls) {
    const th = mk('th', cls || null);
    const b = button('fc-sort', label, () => {
      if (_ui.sort === key) _ui.dir = _ui.dir === 'asc' ? 'desc' : 'asc';
      else { _ui.sort = key; _ui.dir = 'asc'; }
      paintRows();
    });
    if (_ui.sort === key) {
      b.dataset.dir = _ui.dir;
      th.setAttribute('aria-sort', _ui.dir === 'asc' ? 'ascending' : 'descending');
    }
    th.append(b);
    return th;
  }

  function cell(cls, text) { return mk('td', cls, text); }

  function openTable(page, sel) {
    const table = mk('table', 'fc-table');
    const head = mk('tr');
    head.append(sortHead('urgency', 'Severity'), mk('th', null, 'Finding'), sortHead('host', 'Host'),
                sortHead('when', 'When'));
    const thead = mk('thead');
    thead.append(head);
    const body = mk('tbody');
    page.rows.forEach(f => {
      const tr = mk('tr');
      tr.tabIndex = 0;
      tr.setAttribute('aria-selected', String(!!sel && sel.id === f.id));
      const sev = mk('td');
      sev.append(sevChip(f.severity));
      const what = mk('td', 'fc-what');
      what.append(document.createTextNode(f.summary || ''), mk('small', null, f.title || ''));
      const host = cell('fc-host', f.host || '—');
      if (f.host) host.title = f.host;
      tr.append(sev, what, host, cell('fc-when', FC.whenText(f, Date.now())));
      const show = () => { _ui.sel = f.id; setDrawer(true); paintRows(); };
      tr.addEventListener('click', show);
      tr.addEventListener('keydown', ev => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); show(); }
      });
      body.append(tr);
    });
    table.append(thead, body);
    return table;
  }

  function resolvedTable(page) {
    const table = mk('table', 'fc-table fc-table-plain');
    const head = mk('tr');
    ['Host', 'Check', 'Finding', _ui.tab === 'dismissed' ? 'Dismissed' : 'Cleared'].forEach((t, i) => {
      head.append(mk('th', i === 1 ? 'fc-col-conf' : null, t));
    });
    const thead = mk('thead');
    thead.append(head);
    const body = mk('tbody');
    page.rows.forEach(r => {
      const tr = mk('tr');
      tr.append(cell('fc-host', r.host || '—'), cell('fc-col-conf fc-dim', r.title || ''),
                cell('fc-what', r.summary || ''), cell('fc-when', FC.clearedLabel(r)));
      body.append(tr);
    });
    table.append(thead, body);
    return table;
  }

  function renderPager(page) {
    const bar = wipe(byId('fcPager'));
    if (!bar) return;
    bar.hidden = page.total <= PER_PAGE;
    if (bar.hidden) return;
    const turn = step => () => { _ui.page = page.page + step; paintRows(); };
    const prev = button('mcbtn mcbtn-ghost mcbtn-sm', 'Previous', turn(-1));
    const next = button('mcbtn mcbtn-ghost mcbtn-sm', 'Next', turn(1));
    prev.disabled = page.page <= 1;
    next.disabled = page.page >= page.pages;
    bar.append(mk('span', 'fc-num', page.from + '–' + page.to + ' of ' + page.total), mk('span', 'fc-sp'),
               prev, mk('span', 'fc-num', 'Page ' + page.page + ' of ' + page.pages), next);
  }

  function setDrawer(open) {
    _ui.drawer = !!open;
    setPref('fcDrawer', open ? '1' : '0');
  }

  // Facts, text, cause, next step and the action buttons of one finding.
  function detailParts(f) {
    const out = [factList(f)];
    const split = FC.splitCause(f.detail);
    if (split.detail) out.push(mk('p', null, split.detail));
    if (split.cause) {
      const p = mk('p', 'fc-cause');
      p.append(mk('b', null, 'Likely cause: '), document.createTextNode(split.cause));
      out.push(p);
    }
    const gone = FC.discardedNote(f);
    if (gone) out.push(mk('p', 'fc-dim fc-gone', gone));
    if (f.suggested_action) {
      const box = mk('div', 'fc-action');
      box.append(mk('div', 'k', 'Suggested next step'), mk('div', null, f.suggested_action));
      out.push(box);
    }
    const btns = mk('div', 'fc-btns');
    if (towerReady()) btns.append(button('mcbtn mcbtn-pri mcbtn-sm', 'Ask Tower', () => askTower(FC.askText(f))));
    const go = FC.goTarget(f, isAdmin());
    if (go) btns.append(button('mcbtn mcbtn-ghost mcbtn-sm', go.label, () => goTo(go)));
    if (FC.canOpenThread(f, isAdmin()) && typeof window.towerOpenThread === 'function' && towerReady()) {
      btns.append(button('mcbtn mcbtn-ghost mcbtn-sm', 'Open conversation', () => openThread(f.thread_id)));
    }
    btns.append(mk('span', 'fc-sp'));
    if (canOperate()) btns.append(button('mcbtn mcbtn-ghost mcbtn-sm fc-quiet', 'Dismiss', () => dismiss(f.id)));
    out.push(btns);
    return out;
  }

  // The details drawer beside the list; closed, the list takes the full width.
  function renderDetail(f) {
    const pane = wipe(byId('fcDetail'));
    if (!pane) return;
    const on = _ui.tab === 'open' && _ui.view === 'outlook' && _ui.drawer && !!f;
    pane.hidden = !on;
    const split = byId('fcSplit');
    if (split) split.classList.toggle('fc-one', !on);
    if (!on) return;
    const meta = mk('div', 'fc-meta');
    const vcls = VERIFIED_CLS[String(f.verified || '').toLowerCase()];
    const close = button('fc-x', '×', () => { setDrawer(false); paintRows(); });
    close.setAttribute('aria-label', 'Close details');
    close.title = 'Close details';
    meta.append(sevChip(f.severity), mk('span', 'fc-host', f.host || ''), mk('span', 'fc-dim', f.title || ''),
                mk('span', 'fc-sp'), mk('span', 'fc-vchip' + (vcls ? ' ' + vcls : ''), FC.verifiedLabel(f.verified)), close);
    const title = mk('div', 'fc-title', f.summary || '');
    title.setAttribute('role', 'heading');
    title.setAttribute('aria-level', '3');
    pane.append(meta, title);
    const chart = chartBlock(f);
    if (chart) pane.append(chart);
    detailParts(f).forEach(n => pane.append(n));
    if (chart) loadSeries(f);
  }

  // ── briefing view: grouped rows that open in place ──
  function briefRow(f) {
    const row = mk('div', 'fc-brow' + (_ui.open.has(f.id) ? ' open' : ''));
    const head = button('fc-brow-head', '', () => {
      if (_ui.open.has(f.id)) _ui.open.delete(f.id); else _ui.open.add(f.id);
      paintRows();
    });
    head.setAttribute('aria-expanded', String(_ui.open.has(f.id)));
    const what = mk('span', 'fc-what');
    what.append(document.createTextNode(f.summary || ''),
                mk('small', null, _ui.group === 'host' ? (f.title || '') : (f.host || '')));
    const vcls = VERIFIED_CLS[String(f.verified || '').toLowerCase()];
    head.append(sevChip(f.severity), what, mk('span', 'fc-when', FC.whenText(f, Date.now())),
                mk('span', 'fc-vchip' + (vcls ? ' ' + vcls : ''), FC.verifiedLabel(f.verified)), mk('span', 'fc-caret', '›'));
    row.append(head);
    if (_ui.open.has(f.id)) {
      const body = mk('div', 'fc-brow-body');
      const left = mk('div', 'fc-bcol');
      const chart = chartBlock(f);
      if (chart) left.append(chart);
      else left.append(mk('p', 'fc-dim', 'This finding comes from a table of events, so there is no series to draw.'));
      const right = mk('div', 'fc-bcol');
      detailParts(f).forEach(n => right.append(n));
      body.append(left, right);
      row.append(body);
    }
    return row;
  }

  function briefing(page) {
    const box = mk('div', 'fc-brief-list');
    FC.groupRows(page.rows, _ui.group).forEach(g => {
      if (g.name) {
        const head = mk('div', 'fc-group');
        head.append(mk('b', null, g.name), mk('span', 'fc-dim', g.rows.length + (g.rows.length === 1 ? ' finding' : ' findings')),
                    mk('span', 'fc-sp'));
        ['critical', 'warning', 'info'].forEach(sev => {
          if (!g.sev[sev]) return;
          const s = FC.sevLabel(sev);
          head.append(mk('span', 'fc-sev fc-' + s.cls, g.sev[sev] + ' ' + s.short));
        });
        box.append(head);
      }
      g.rows.forEach(f => box.append(briefRow(f)));
    });
    return box;
  }

  // Rows, pager and detail for the current tab; the toolbar keeps its focus.
  function paintRows() {
    const host = wipe(byId('fcRows'));
    if (!host) return;
    Object.keys(_charts).forEach(destroyChart);
    const v = _view;
    if (_error && !v) { host.append(mk('div', 'fc-empty', _error)); renderPager(FC.paginate([], 1, PER_PAGE)); renderDetail(null); return; }
    if (!v) { host.append(mk('div', 'fc-empty', 'Loading…')); renderDetail(null); return; }
    if (!v.enabled) { host.append(mk('div', 'fc-empty', OFF_TEXT)); renderPager(FC.paginate([], 1, PER_PAGE)); renderDetail(null); return; }
    if (_ui.tab !== 'open') {
      const res = FC.resolvedTabs(v.cleared)[_ui.tab] || [];
      const page = FC.paginate(res, _ui.page, PER_PAGE);
      _ui.page = page.page;
      host.append(res.length ? resolvedTable(page)
        : mk('div', 'fc-empty', _ui.tab === 'dismissed' ? 'Nothing has been dismissed.' : 'Nothing has cleared yet.'));
      renderPager(page);
      renderDetail(null);
      return;
    }
    const rows = openRows();
    const page = FC.paginate(rows, _ui.page, PER_PAGE);
    _ui.page = page.page;
    const sel = selected(rows, page);
    if (!(v.findings || []).length) host.append(mk('div', 'fc-empty', allClearText()));
    else if (!rows.length) host.append(mk('div', 'fc-empty', 'No findings match these filters. Clear a filter to see more.'));
    else host.append(_ui.view === 'briefing' ? briefing(page) : openTable(page, _ui.drawer ? sel : null));
    // Loads the series of every open briefing row now that the rows are in the page.
    if (_ui.view === 'briefing') page.rows.filter(f => _ui.open.has(f.id)).forEach(loadSeries);
    renderPager(page);
    renderDetail(rows.length ? sel : null);
    document.querySelectorAll('#fcHorizon .fc-hz-dot').forEach(d => {
      d.classList.toggle('on', !!sel && d.dataset.id === String(sel.id));
    });
  }

  function paintList() {
    renderTabs();
    renderHorizon(openRows());
    renderToolbar();
    paintRows();
  }

  function allClearText() {
    const checks = (_view && _view.checks) || [];
    const ran = checks.filter(c => c.state === 'ok' || c.state === 'failed').length;
    const collecting = checks.filter(c => c.state === 'collecting').length;
    let text = 'Nothing ahead.';
    if (ran) text += ' ' + ran + (ran === 1 ? ' check ran' : ' checks ran');
    if (collecting) text += (ran ? '; ' : ' ') + collecting
      + (collecting === 1 ? ' is' : ' are') + ' still collecting data';
    return ran || collecting ? text + '.' : text;
  }

  // Chart holder, or null when the finding carries no graph spec.
  function chartBlock(f) {
    if (!f.graph || !f.graph.name) return null;
    const wrap = mk('div', 'fc-chart');
    const holder = mk('div', 'fc-canvas');
    const cv = document.createElement('canvas');
    cv.setAttribute('role', 'img');
    cv.setAttribute('aria-label', 'History and projection for this finding');
    holder.append(cv);
    wrap.append(holder);
    const note = mk('div', 'fc-chart-note', 'Loading history…');
    wrap.append(note);
    _canvas[f.id] = cv;
    _holder[f.id] = holder;
    _note[f.id] = note;
    return wrap;
  }

  // Nothing to draw: the canvas goes away and only the note stays.
  function hideCanvas(id, msg) {
    destroyChart(id);
    if (_holder[id]) _holder[id].hidden = true;
    paint(_note[id], msg);
  }

  // ── history series + chart ──
  function seriesUrl(g) {
    const nowS = Date.now() / 1000;
    const start = g.start == null ? nowS - 86400 : Number(g.start);
    const mins = Math.min(AE_SINCE_MAX_MIN, Math.max(1, Math.ceil((nowS - start) / 60)));
    const q = new URLSearchParams({ since_minutes: String(mins),
                                    max_points: String(SERIES_POINTS),
                                    agg: g.agg === 'max' ? 'max' : 'mean' });
    if (g.host) q.set('hostname', g.host);
    return '/api/alarm/metrics/' + encodeURIComponent(g.source) + '/'
      + encodeURIComponent(g.name) + '?' + q.toString();
  }

  function loadSeries(f) {
    const g = f.graph;
    if (!g || !g.name || !_canvas[f.id]) return;
    if (_state[f.id] === 'done' && FC.seriesFresh(_seriesAt[f.id], Date.now(), SERIES_TTL_MS)) { drawChart(f); return; }
    if (_state[f.id] === 'loading') return;
    if (_state[f.id] === 'done') drawChart(f);
    _state[f.id] = 'loading';
    fetch(seriesUrl(g))
      .then(r => (r.ok ? r.json() : []))
      .then(rows => {
        const lo = (g.start == null ? 0 : Number(g.start)) * 1000;
        const hi = Math.max(Date.now(), (g.end == null ? 0 : Number(g.end)) * 1000);
        _series[f.id] = (Array.isArray(rows) ? rows : [])
          .map(p => [Date.parse((p || {}).timestamp), Number((p || {}).value)])
          .filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]) && p[0] >= lo && p[0] <= hi)
          .sort((a, b) => a[0] - b[0]);
        _state[f.id] = 'done';
        _seriesAt[f.id] = Date.now();
        drawChart(f);
      })
      .catch(() => {
        _state[f.id] = 'error';
        hideCanvas(f.id, 'History for this metric could not be loaded.');
      });
  }

  function destroyChart(id) {
    if (_charts[id]) { try { _charts[id].destroy(); } catch (_) { /* gone */ } }
    delete _charts[id];
  }

  function drawChart(f) {
    const cv = _canvas[f.id];
    if (!cv || !cv.isConnected || typeof Chart === 'undefined') return;
    const g = f.graph || {};
    const pts = _series[f.id] || [];
    const fit = FC.fitPoints(g, Date.now());
    const scale = FC.chartScale(pts, fit, g.threshold);
    if (!scale) { hideCanvas(f.id, 'No history for this metric yet.'); return; }
    if (_holder[f.id]) _holder[f.id].hidden = false;
    paint(_note[f.id], '');
    destroyChart(f.id);
    const xy = p => ({ x: p[0], y: p[1] });
    const line = (label, data, colour, dash, width) => ({
      label: label, data: data.map(xy), borderColor: colour, backgroundColor: colour,
      borderWidth: width, borderDash: dash, pointRadius: 0, tension: 0.2, fill: false,
    });
    const sets = [line('Measured', pts, cssVar('--accent'), [], 2)];
    if (fit.length) {
      const proj = line('Projected', fit, cssVar('--accent'), [5, 4], 2);
      proj.pointRadius = c => (c.dataIndex === fit.length - 1 ? 4 : 0);
      proj.clip = false;
      sets.push(proj);
    }
    if (g.threshold != null) {
      const t = Number(g.threshold);
      sets.push(line(g.limit === false ? 'Baseline' : (t === 100 ? 'Full' : 'Limit'),
                     [[scale.xMin, t], [scale.xMax, t]], cssVar('--crit'), [], 1.5));
    }
    _charts[f.id] = new Chart(cv.getContext('2d'), {
      type: 'line',
      data: { datasets: sets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        layout: { padding: { right: 8 } },
        interaction: { mode: 'nearest', intersect: false },
        scales: {
          x: { type: 'time', min: scale.xMin, max: scale.xMax,
               time: { tooltipFormat: 'MMM d HH:mm' },
               ticks: { color: cssVar('--fg-dim'), maxTicksLimit: 6 },
               grid: { color: cssVar('--border-soft') } },
          y: { min: scale.yMin, max: scale.yMax,
               ticks: { color: cssVar('--fg-dim'), includeBounds: false },
               grid: { color: cssVar('--border-soft') } },
        },
        plugins: { legend: { position: 'bottom', align: 'start',
                             labels: { color: cssVar('--fg-muted'), boxWidth: 14, boxHeight: 2 } } },
      },
      plugins: fit.length ? [todayMarker(fit[0][0])] : [],
    });
  }

  // Inline Chart.js plugin: a dashed "Today" line where the projection starts.
  function todayMarker(ms) {
    return {
      id: 'fcToday',
      afterDatasetsDraw(chart) {
        const x = chart.scales.x.getPixelForValue(ms), a = chart.chartArea, c = chart.ctx;
        if (!(x >= a.left && x <= a.right)) return;
        c.save();
        c.strokeStyle = cssVar('--border-strong'); c.setLineDash([2, 3]); c.lineWidth = 1;
        c.beginPath(); c.moveTo(x, a.top); c.lineTo(x, a.bottom); c.stroke();
        c.fillStyle = cssVar('--fg-muted'); c.font = '10.5px sans-serif';
        c.textAlign = x > a.right - 40 ? 'right' : 'left';
        c.fillText('Today', x + (c.textAlign === 'right' ? -5 : 5), a.bottom - 5);
        c.restore();
      },
    };
  }

  // ── kit page: build, equal rows and place keeping ──
  function visible() {
    return !!S.root && S.root.isConnected
      && (typeof _activeTab === 'undefined' || _activeTab === 'dashboard')
      && (typeof _subTabState === 'undefined' || !_subTabState || _subTabState.dashboard === 'forecast');
  }
  // Dresses the page's panels and applies the saved kit layout once per root.
  function ensure() {
    const root = byId('fc-root');
    if (!root || typeof kitDress !== 'function') return false;
    if (S.root === root && root.hasAttribute('data-kit-page')) return true;
    S.root = root; S.last = {}; S.place = null; _sig = null;
    kitDress(root);
    if (typeof mcSoloTitles === 'function') mcSoloTitles(root);
    kitLayout('forecast', root, window.mcShellStore);
    // The run bar sits in the Forecast row's header line, left of the row's ⋯ menu.
    const bar = byId('fcRunbar'), menu = bar && bar.parentNode && Array.from(bar.parentNode.children).find(c => c.classList.contains('row-menu'));
    if (menu && bar.nextElementSibling !== menu) bar.parentNode.insertBefore(bar, menu);
    if (root._fcClick) root.removeEventListener('click', root._fcClick);
    root._fcClick = onClick;
    root.addEventListener('click', onClick);
    return true;
  }
  // Show as table or chart evens out the rows again.
  function onClick(ev) {
    const b = ev.target && ev.target.closest ? ev.target.closest('[data-kit-do="table"]') : null;
    if (!b) return;
    setTimeout(() => { const at = holdPlace(); fit(); restorePlace(at); }, 0);
  }
  // Rows hidden by the operator in layout.forecastRowsHidden.
  function applyRows() {
    const l = lay(), user = (l && l.forecastRowsHidden) || [];
    S.root.querySelectorAll('section.row[data-row]').forEach(r => {
      const hide = user.includes(r.dataset.row);
      if (r.hidden !== hide) r.hidden = hide;
    });
    kitRowMenus(S.root);
  }
  // The last panel of a box spans the columns left empty after it; spans clear first so the column count read is the grid's own.
  function widen(box) {
    if (!box.clientWidth) return;
    const list = Array.from(box.children).filter(p => p.classList.contains('panel') && !p.hidden && !p.classList.contains('full'));
    list.forEach(p => { if (p.style.gridColumn) p.style.gridColumn = ''; });
    const cols = getComputedStyle(box).gridTemplateColumns.split(' ').filter(Boolean).length || 1, pos = list.length % cols;
    if (list.length && pos > 0) list[list.length - 1].style.gridColumn = 'span ' + (cols - pos + 1);
  }
  function fit() {
    const root = S.root;
    if (!root || !root.isConnected || !root.clientWidth) return;
    root.querySelectorAll('.panels').forEach(widen);
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
  if (window._fcOnScroll) window.removeEventListener('scroll', window._fcOnScroll);
  window._fcOnScroll = onScroll;
  window.addEventListener('scroll', onScroll, { passive: true });

  // ── panel paint ──
  function paintPanel() {
    if (!byId('fcRows') || !ensure()) return;
    if (!_ui.ready) {
      _ui.ready = true;
      _ui.view = pref('fcView', 'outlook') === 'briefing' ? 'briefing' : 'outlook';
      _ui.drawer = pref('fcDrawer', '1') === '1';
    }
    const at = holdPlace();
    applyRows();
    // The run in flight repaints only the head and tiles, never the lists and charts under them.
    const sig = _error + '|' + JSON.stringify(_view, (k, val) => (k === 'run' || k === 'running' ? undefined : val));
    runBar();
    renderTiles();
    if (sig !== _sig) {
      _sig = sig;
      renderChecks();
      renderBrief();
      paintList();
    }
    paintTower();
    fit();
    restorePlace(at);
    S.place = holdPlace();
  }

  // ── entry points ──
  // Sub-tab entry: paint what we have, refresh the payload and Tower, and start the poll.
  function forecastLoad() {
    paintPanel();
    return Promise.all([refresh(), refreshTower()]).then(schedule, schedule);
  }

  // Kit refit hook: evens out the page's rows when root holds it or sits inside it.
  function fcRefit(root) {
    const el = byId('fc-root');
    if (!(S.root === el && el && root && (root === document || root.contains(el) || el.contains(root)))) return;
    const at = S.place && S.place.el.isConnected ? S.place : holdPlace();
    fit();
    restorePlace(at);
    S.place = holdPlace();
  }

  // Overall's Forecast panel: the cached /api/forecast payload, refetched at most once a minute.
  function forecastSnapshot() {
    if (!_inflight && Date.now() - _fetchedAt >= OV_STALE_MS && !(window.LivePause && LivePause.on && _view)) refresh();
    return _view;
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && panelVisible()) forecastLoad();
  });

  window.forecastLoad = forecastLoad;
  window.forecastSnapshot = forecastSnapshot;
  window.fcRefit = fcRefit;
})();
