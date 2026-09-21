// Forecast (#1031) — Dashboard › Forecast sub-tab and the Overall page strip.
// Pure helpers live in js/lib/forecast.js (window.FC); every API string is written with textContent only.
(function () {
  'use strict';

  const POLL_MS = 60000;          // idle cadence while the panel is on screen
  const RUN_POLL_MS = 5000;       // cadence while a run is in flight
  const OV_STALE_MS = 60000;      // Overall strip refetch age
  const OV_MAX = 6;               // findings listed on the Overall strip
  const AE_SINCE_MAX_MIN = 43200; // alarm engine history ceiling (30 days)
  const SERIES_POINTS = 400;
  const PER_PAGE = 8;             // rows per page in every list
  const HORIZON_DAYS = 30;
  const GO_SCROLL_MS = 300;       // lets the target tab paint before scrolling to its card

  const OFF_TEXT = 'Forecast looks at the last two weeks for trends — a disk filling, '
    + 'a load change, a model that keeps failing — and tells you before the alarm does.';
  const ERR_TEXT = 'Forecast is unavailable right now.';
  const VERIFIED_CLS = { code: '', 'tower+code': 'fc-vboth', model: 'fc-vunv' };

  let _view = null;      // last /api/forecast payload
  let _error = '';
  let _sig = null;       // payload signature, so an unchanged poll repaints nothing
  let _fetchedAt = 0;
  let _inflight = null;
  let _timer = null;
  const _ui = { tab: 'open', sev: new Set(), host: '', check: '', q: '', sort: 'urgency', dir: 'asc', page: 1, sel: null,
                view: 'outlook', group: 'host', drawer: true, open: new Set() };
  const _series = {};        // finding id → [[ms, value], …]
  const _state = {};         // finding id → 'loading' | 'done' | 'error'
  const _charts = {};        // finding id → Chart
  const _canvas = {};        // finding id → canvas element
  const _holder = {};        // finding id → canvas wrapper
  const _note = {};          // finding id → chart note element

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
    refresh().then(schedule, schedule);
  }

  function refresh() {
    if (_inflight) return _inflight;
    _inflight = (async () => {
      try {
        const r = await fetch('/api/forecast');
        const d = await r.json().catch(() => ({}));
        if (r.ok && d && d.ok) { _view = d; _error = ''; }
        else _error = (d && (d.error || d.message)) ? String(d.error || d.message) : ERR_TEXT;
      } catch (_) {
        _error = ERR_TEXT;
      }
      _fetchedAt = Date.now();
      paintPanel();
      paintOverall();
    })().finally(() => { _inflight = null; });
    return _inflight;
  }

  // ── run bar ──
  function kv(label, value) {
    const n = mk('div', 'fc-kv');
    n.append(mk('span', 'k', label), mk('span', 'v', value || '—'));
    return n;
  }

  function runBar() {
    const bar = wipe(byId('fcRunbar'));
    if (!bar) return;
    // A failed refresh leaves the last findings on screen with this note.
    if (_error && _view) bar.append(mk('div', 'fc-note', _error));
    if (!_view) return;
    const v = _view;
    const now = Date.now();
    if (!v.enabled) {
      bar.append(kv('Forecast', 'Off'), mk('span', 'fc-sp'));
      if (isAdmin()) bar.append(settingsButton());
      return;
    }
    bar.append(kv('Last run', FC.whenLabel(v.last_run, now) || 'Never'));
    bar.append(kv('Next run', v.enabled ? (FC.whenLabel(v.next_run, now) || 'Not scheduled') : 'Off'));
    bar.append(kv('Tower effort', FC.towerLine(v.tower)));
    bar.append(kv('Looking back', v.window_days ? v.window_days + ' days' : '—'));
    bar.append(mk('span', 'fc-sp'));
    if (canOperate()) {
      const btn = mk('button', 'mcbtn mcbtn-pri mcbtn-sm', v.running ? 'Running…' : 'Run now');
      btn.type = 'button';
      btn.disabled = !!v.running;
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
    try {
      const r = await fetch('/api/forecast/run', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        if (_view) { _view.running = true; _sig = null; }
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
    const text = v.enabled ? (v.digest || '') : '';
    box.hidden = !text;
    if (!text) return;
    const sum = wipe(byId('fcBriefSum'));
    sum.append(mk('span', 'fc-brief-k', "Tower's analysis"), mk('span', 'fc-brief-short', FC.shortDigest(text, 220)));
    const body = wipe(byId('fcBriefBody'));
    body.append(mk('p', null, text));
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

  // ── checks (collapsed by default) ──
  function renderChecks() {
    const box = byId('fcChecksBox');
    const host = wipe(byId('fcChecks'));
    const v = _view;
    const on = !!(v && v.enabled && (v.checks || []).length);
    if (box) box.hidden = !on;
    if (!on || !host) return;
    const ran = v.checks.filter(c => c.state === 'ok').length;
    const collecting = v.checks.filter(c => c.state === 'collecting').length;
    const failed = v.checks.filter(c => c.state === 'failed').length;
    const sum = wipe(byId('fcChecksSum'));
    sum.append(mk('b', null, v.checks.length + ' checks'));
    const parts = [ran + ' ran'];
    if (collecting) parts.push(collecting + ' still collecting data');
    if (failed) parts.push(failed + ' could not run');
    sum.append(mk('span', failed ? 'fc-warn-text' : null, parts.join(', ')));
    v.checks.forEach(c => {
      const st = FC.checkState(c);
      const chip = mk('div', 'fc-cstate fc-' + st.cls);
      chip.append(mk('span', 'fc-dot'), document.createTextNode(c.title || ''));
      if (st.note) chip.append(mk('small', null, st.note));
      host.append(chip);
    });
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
    if (f.suggested_action) {
      const box = mk('div', 'fc-action');
      box.append(mk('div', 'k', 'Suggested next step'), mk('div', null, f.suggested_action));
      out.push(box);
    }
    const btns = mk('div', 'fc-btns');
    if (towerReady()) btns.append(button('mcbtn mcbtn-pri mcbtn-sm', 'Ask Tower', () => askTower(FC.askText(f))));
    const go = FC.goTarget(f, isAdmin());
    if (go) btns.append(button('mcbtn mcbtn-ghost mcbtn-sm', go.label, () => goTo(go)));
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
    if (_state[f.id] === 'done') { drawChart(f); return; }
    if (_state[f.id] === 'loading') return;
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

  // ── panel paint ──
  function paintPanel() {
    if (!byId('fcRows')) return;
    if (!_ui.ready) {
      _ui.ready = true;
      _ui.view = pref('fcView', 'outlook') === 'briefing' ? 'briefing' : 'outlook';
      _ui.drawer = pref('fcDrawer', '1') === '1';
    }
    const sig = _error + '|' + JSON.stringify(_view);
    if (sig === _sig) { runBar(); return; }
    _sig = sig;
    runBar();
    renderChecks();
    renderBrief();
    paintList();
  }

  // ── Overall strip ──
  function ovList(rows) {
    const ul = mk('ul', 'fc-ov-list');
    rows.forEach(f => {
      const li = document.createElement('li');
      const s = FC.sevLabel(f.severity);
      li.append(mk('span', 'fc-pill fc-' + s.cls, s.short));
      li.title = (f.host ? f.host + ' — ' : '') + (f.summary || '');
      const txt = mk('span', 'fc-ov-text');
      txt.append(mk('span', 'fc-host', f.host || '—'),
                 document.createTextNode(' — ' + (f.summary || '')));
      li.append(txt);
      ul.append(li);
    });
    return ul;
  }

  function paintOverall() {
    const count = byId('fcOvCount');
    const body = byId('fcOvBody');
    const foot = byId('fcOvFoot');
    if (!count || !body || !foot) return;
    const v = _view;
    const viewBtn = byId('fcOvView');
    wipe(body);
    wipe(foot);
    count.textContent = _error && !v ? 'unavailable' : FC.countLabel(v);
    const open = v && v.enabled ? (v.findings || []) : [];
    const urgent = FC.urgent(open);
    const rows = urgent.slice(0, OV_MAX);
    const strip = body.closest('.fc-ov');
    if (strip) {
      const cls = rows.length ? FC.sevLabel(rows[0].severity).cls : 'info';
      strip.classList.toggle('fc-ov-crit', cls === 'crit');
      strip.classList.toggle('fc-ov-warn', cls === 'warn');
    }
    if (viewBtn) {
      viewBtn.hidden = !open.length;
      if (!viewBtn._fcBound) {
        viewBtn._fcBound = true;
        viewBtn.addEventListener('click', () => {
          if (typeof switchTab === 'function') switchTab('dashboard');
          if (typeof switchSubTab === 'function') switchSubTab('dashboard', 'forecast');
        });
      }
    }
    if (!v) { body.append(mk('div', 'fc-empty', _error || 'Loading…')); return; }
    if (!v.enabled) {
      body.append(mk('div', 'fc-empty', OFF_TEXT));
      if (isAdmin()) foot.append(settingsButton());
      return;
    }
    if (!rows.length) {
      const notes = open.length;
      body.append(mk('div', 'fc-empty', notes
        ? 'No warnings. ' + notes + (notes === 1 ? ' note' : ' notes') + ' on the Forecast page.'
        : allClearText()));
      foot.append(mk('span', null, 'Next run ' + (FC.whenLabel(v.next_run, Date.now()) || '—')));
      return;
    }
    const brief = FC.shortDigest(v.digest);
    if (brief) body.append(mk('div', 'fc-ov-digest', 'Tower: ' + brief));
    body.append(ovList(rows));
    if (urgent.length > rows.length) {
      body.append(mk('div', 'fc-ov-more', 'and ' + (urgent.length - rows.length) + ' more'));
    }
    const last = FC.whenLabel(v.last_run, Date.now());
    const next = FC.whenLabel(v.next_run, Date.now());
    foot.append(mk('span', null, 'Last run ' + (last || '—')
      + (next ? ' · next ' + next.toLowerCase() : '')));
  }

  // ── entry points ──
  // Sub-tab entry: paint what we have, refresh, and start the poll.
  function forecastLoad() {
    paintPanel();
    paintOverall();
    refresh().then(schedule, schedule);
  }

  // Overall band repaint: render from cache, refetching at most once a minute.
  function forecastOverallCard() {
    paintOverall();
    if (_inflight) return;
    if (Date.now() - _fetchedAt < OV_STALE_MS) return;
    if (window.LivePause && LivePause.on && _view) return;
    refresh();
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && panelVisible()) forecastLoad();
  });

  window.forecastLoad = forecastLoad;
  window.forecastOverallCard = forecastOverallCard;
})();
