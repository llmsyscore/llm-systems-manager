// Admin → Audit Log (#794): filtered ledger, paging, detail panel and the
// collapsed Audit settings card. Talks to /api/admin/audit-log{,.csv,/stats,/events}.
(() => {
  'use strict';

  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const DEFAULTS = { q: '', group: '', actor: '', outcome: '', hours: 168, hideAuto: true, sort: 'ts', dir: 'desc' };
  const esc = s => (typeof adminEsc === 'function' ? adminEsc(s) : String(s == null ? '' : s));

  // ── pure helpers (exported on window.AuditView for tests) ────────────────
  function clock(d, withSec) {
    let h = d.getHours();
    const ap = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    const mm = String(d.getMinutes()).padStart(2, '0');
    return h + ':' + mm + (withSec ? ':' + String(d.getSeconds()).padStart(2, '0') : '') + ' ' + ap;
  }
  function parseTs(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  function fmtShort(iso) {
    const d = parseTs(iso);
    return d ? `${MON[d.getMonth()]} ${d.getDate()} · ${clock(d)}` : '—';
  }
  function fmtFull(iso) {
    const d = parseTs(iso);
    if (!d) return '—';
    let tz = '';
    try { tz = (new Intl.DateTimeFormat('en-US', { timeZoneName: 'short' }).formatToParts(d).find(p => p.type === 'timeZoneName') || {}).value || ''; } catch (_) { /* no Intl */ }
    return `${MON[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} · ${clock(d, true)}${tz ? ' ' + tz : ''}`;
  }
  function age(iso, nowMs) {
    const d = parseTs(iso);
    if (!d) return '';
    const s = Math.max(0, ((nowMs == null ? Date.now() : nowMs) - d.getTime()) / 1000);
    if (s < 60) return Math.round(s) + ' s';
    if (s < 3600) return Math.round(s / 60) + ' min';
    if (s < 86400) return (s / 3600).toFixed(1).replace(/\.0$/, '') + ' h';
    return Math.round(s / 86400) + ' d';
  }
  // Who did it, for the User column: blank actors resolve by auth kind.
  function userCell(e) {
    if (e.actor === 'autopilot') return '<span class="sys">autopilot</span>';
    if (!e.actor) {
      if (e.auth === 'test') return '<span class="sys">test harness</span>';
      if (e.auth === 'internal' || e.ip === '-' || e.ip === '—') return '<span class="sys">system</span>';
      return '<span class="sys">local</span>';
    }
    return esc(e.actor) + (e.role && e.role !== 'admin' ? `<span class="role">${esc(e.role)}</span>` : '');
  }
  // 1 … p-1 p p+1 … N with gaps marked by '…'.
  function pageList(page, pages) {
    const set = new Set([1, pages, page - 1, page, page + 1].filter(n => n >= 1 && n <= pages));
    const list = [...set].sort((a, b) => a - b);
    const out = [];
    let prev = 0;
    list.forEach(n => { if (n - prev > 1) out.push('…'); out.push(n); prev = n; });
    return out;
  }
  function queryParams(st, per, page) {
    const p = new URLSearchParams();
    if (st.q) p.set('q', st.q);
    if (st.group) p.set('group', st.group);
    if (st.actor) p.set('actor', st.actor);
    if (st.outcome) p.set('outcome', st.outcome);
    if (st.hours) p.set('since_hours', String(st.hours));
    if (st.hideAuto) p.set('hide_automated', '1');
    if (st.sort && st.sort !== 'ts') p.set('sort', st.sort);
    if (st.dir && st.dir !== 'desc') p.set('dir', st.dir);
    if (per != null) { p.set('limit', String(per)); p.set('offset', String((page - 1) * per)); }
    return p;
  }
  function atDefaults(st) {
    return !st.q && !st.group && !st.actor && !st.outcome && st.hours === DEFAULTS.hours;
  }
  // Settings card → PUT /api/admin/settings changes object.
  function settingsChanges(form, events) {
    const disabled = events.filter(ev => !form.enabled[ev.key]).map(ev => ev.key).sort();
    return {
      'manager.audit.retention_days': Math.max(0, Math.floor(Number(form.retention) || 0)),
      'manager.audit.page_size': Math.max(10, Math.floor(Number(form.pageSize) || 25)),
      'manager.audit.save_automated': !!form.saveAutomated,
      'manager.audit.automated_actors': parseActors(form.automatedActors),
      'manager.audit.disabled_events': disabled,
    };
  }
  // "a, b\nc" → ['a', 'b', 'c'] (deduped, blanks dropped).
  function parseActors(text) {
    return [...new Set(String(text == null ? '' : text).split(/[\s,;]+/).map(s => s.trim()).filter(Boolean))];
  }
  window.AuditView = { fmtShort, fmtFull, age, userCell, pageList, queryParams, atDefaults, settingsChanges, parseActors, DEFAULTS };

  // ── state ────────────────────────────────────────────────────────────────
  const state = { ...DEFAULTS, per: 25, page: 1, sel: null };
  let entries = [];
  let total = 0;
  let stats = null;
  let seq = 0;
  let bound = false;
  let lastSig = '';
  let perFromServer = false;
  let cfg = null;        // /events payload for the settings card
  let cfgForm = null;    // editable copy

  const $ = id => document.getElementById(id);

  // ── loading ──────────────────────────────────────────────────────────────
  function readHash() {
    const h = window.location.hash || '';
    if (!h.startsWith('#audit?')) return;
    const p = new URLSearchParams(h.slice(7));
    state.q = p.get('q') || '';
    state.group = p.get('group') || '';
    state.actor = p.get('actor') || '';
    state.outcome = p.get('outcome') || '';
    if (p.has('since_hours')) state.hours = Number(p.get('since_hours')) || 0;
    if (p.has('hide_automated')) state.hideAuto = p.get('hide_automated') === '1';
    if (p.get('sort')) state.sort = p.get('sort');
    if (p.get('dir')) state.dir = p.get('dir');
    if (p.get('page')) state.page = Math.max(1, Number(p.get('page')) || 1);
  }
  function writeHash() {
    if (typeof _subTabState !== 'undefined' && _subTabState.admin !== 'audit') return;
    const p = queryParams(state, null, 1);
    if (!state.hideAuto) p.set('hide_automated', '0');
    if (state.page > 1) p.set('page', String(state.page));
    const next = p.toString() ? '#audit?' + p.toString() : '#audit';
    try { window.history.replaceState(null, '', next); } catch (_) { /* file:// harness */ }
  }

  // offset 0 = fresh entry (page 1, re-read stats); no arg = refresh in place.
  async function adminAuditLoad(offset) {
    const first = !bound;
    if (!bind()) return;
    // Sub-tab entry resets to page 1, except the very first load, which keeps the deep link.
    if (offset === 0 && !first) { state.page = 1; state.sel = null; }
    else if (offset == null && (state.sel != null || document.activeElement === $('auQ'))) return;
    const mySeq = ++seq;
    const tbody = $('adminAuditTbody');
    try {
      const listP = fetch('/api/admin/audit-log?' + queryParams(state, state.per, state.page)).then(r => r.json());
      // Stats (actors, oldest, purge) change rarely: skip them on the tick refresh.
      const statsP = (offset == null && stats) ? Promise.resolve(null)
        : fetch('/api/admin/audit-log/stats').then(r => r.json()).catch(() => null);
      const [d, s] = await Promise.all([listP, statsP]);
      if (mySeq !== seq) return;
      if (!d || !d.ok) throw new Error((d && d.error) || 'request failed');
      if (!perFromServer && d.page_size) { state.per = Number(d.page_size) || 25; perFromServer = true; syncPerSelect(); }
      total = d.total || 0;
      entries = d.entries || [];
      stats = s && s.ok ? s : stats;
      // Landed past the last page (rows purged/filtered): step back and retry.
      const pages = Math.max(1, Math.ceil(total / state.per));
      if (state.page > pages) { state.page = pages; return adminAuditLoad(-1); }
      render();
    } catch (e) {
      if (mySeq !== seq) return;
      entries = []; total = 0; lastSig = '';
      if (tbody) tbody.innerHTML = `<tr><td colspan="6"><div class="au-empty">Failed to load audit log — ${esc(e.message)}</div></td></tr>`;
      renderPager();
    }
  }

  // ── rendering ────────────────────────────────────────────────────────────
  function render() {
    const tbody = $('adminAuditTbody');
    if (!tbody) return;
    const now = Date.now();
    const sig = entries.map(e => e.id + ':' + e.outcome).join(',') + '|' + state.sel + '|' + state.per;
    if (sig === lastSig && entries.length) {
      // Same rows as last paint: leave the table alone, refresh the chrome only.
    } else if (!entries.length) {
      tbody.innerHTML = `<tr><td colspan="6"><div class="au-empty">${atDefaults(state) ? 'No audit entries yet.' : 'No entries match. <b>Reset the filters</b> or widen the time range.'}</div></td></tr>`;
    } else {
      tbody.innerHTML = entries.map(e => `<tr data-id="${e.id}" class="${state.sel === e.id ? 'sel' : ''}">
        <td class="t" title="${esc(fmtFull(e.ts))} · ${esc(age(e.ts, now))} ago"><b>${esc(fmtShort(e.ts))}</b></td>
        <td class="u">${userCell(e)}</td>
        <td class="a"><i class="g-${esc(e.group || 'config')}"></i>${esc(e.action)}<span class="desc">${esc(e.label || '')}</span></td>
        <td class="tg" title="${esc(e.target || '')}">${e.target ? esc(e.target) : '<em>—</em>'}</td>
        <td class="ip c-from">${esc(e.ip || '—')}${e.auth ? `<span class="how">· ${esc(e.auth)}</span>` : ''}</td>
        <td class="o"><span class="au-pill ${esc(e.outcome || 'ok')}">${esc(e.outcome || '?')}</span></td></tr>`).join('');
    }
    lastSig = sig;
    const tot = $('auTotal'); if (tot) tot.textContent = total.toLocaleString();
    const old = $('auOldest'); if (old && stats) old.textContent = stats.oldest ? fmtShort(stats.oldest).split(' · ')[0] : '—';
    renderActors();
    renderSortArrows();
    renderPager();
    renderDetail();
    const reset = $('auReset'); if (reset) reset.classList.toggle('idle', atDefaults(state));
    const exp = $('auExport'); if (exp) exp.href = '/api/admin/audit-log.csv?' + queryParams(state, null, 1);
    writeHash();
  }
  function renderActors() {
    const sel = $('auActor');
    if (!sel || !stats) return;
    const have = new Set([...sel.options].map(o => o.value));
    const people = (stats.actors || []).filter(a => a && a !== 'autopilot');
    const want = new Set(['', ...people, 'autopilot', 'system', 'local']);
    if ([...want].every(v => have.has(v)) && have.size === want.size) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">All users</option>' +
      people.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join('') +
      '<option value="autopilot">autopilot</option><option value="system">system</option><option value="local">local</option>';
    sel.value = cur;
  }
  function renderSortArrows() {
    document.querySelectorAll('#adminAuditTable th[data-key]').forEach(th => {
      const on = th.dataset.key === state.sort;
      th.classList.toggle('on', on);
      if (on) th.dataset.dir = state.dir; else delete th.dataset.dir;
    });
  }
  function renderPager() {
    const pages = Math.max(1, Math.ceil(total / state.per));
    const start = (state.page - 1) * state.per;
    const info = $('auPageInfo');
    if (info) info.textContent = total ? `${start + 1}–${Math.min(start + state.per, total)} of ${total.toLocaleString()}` : '0 entries';
    const nums = $('auPnums');
    if (!nums) return;
    const first = state.page === 1, last = state.page === pages;
    const btn = (label, go, opts = {}) => {
      const b = document.createElement('button');
      b.type = 'button'; b.textContent = label; b.dataset.go = String(go);
      if (opts.title) b.title = opts.title;
      if (opts.on) b.className = 'on';
      if (opts.disabled) b.disabled = true;
      return b;
    };
    nums.replaceChildren(
      btn('«', 1, { title: 'First', disabled: first }),
      btn('‹', state.page - 1, { title: 'Previous', disabled: first }),
      ...pageList(state.page, pages).map(n => {
        if (n === '…') { const d = document.createElement('span'); d.className = 'dots'; d.textContent = '…'; return d; }
        return btn(String(n), n, { on: n === state.page });
      }),
      btn('›', state.page + 1, { title: 'Next', disabled: last }),
      btn('»', pages, { title: 'Last', disabled: last }));
  }
  function syncPerSelect() {
    const sel = $('auPer');
    if (!sel) return;
    if (![...sel.options].some(o => Number(o.value) === state.per)) {
      const o = document.createElement('option'); o.value = String(state.per); o.textContent = String(state.per); sel.appendChild(o);
    }
    sel.value = String(state.per);
  }

  function methodFor(e) { return e.method || (e.action && e.action.startsWith('autopilot:') ? '' : '—'); }
  function renderDetail() {
    const det = $('auDet'), split = $('auSplit');
    if (!det || !split) return;
    const e = entries.find(x => x.id === state.sel);
    split.classList.toggle('detail', !!e);
    if (!e) { det.innerHTML = ''; return; }
    const i = entries.indexOf(e);
    const start = (state.page - 1) * state.per;
    const who = e.actor ? `${esc(e.actor)} · ${esc(e.role || '—')}`
      : (e.auth === 'test' ? 'test harness (no session)' : e.auth === 'internal' ? 'system' : 'unauthenticated loopback');
    const d = e.detail && typeof e.detail === 'object' ? e.detail : null;
    let details = '';
    if (d && d.changes && typeof d.changes === 'object') {
      details = `<div class="sec"><span class="microlbl">Changes</span><div class="diff">${Object.entries(d.changes).map(([k, v]) => {
        const [a, b] = Array.isArray(v) ? v : [undefined, v];
        return `<div class="r"><span class="k" title="${esc(k)}">${esc(k)}</span><span class="v">${a === undefined || a === null ? '' : `<s>${esc(JSON.stringify(a))}</s> → `}<b>${esc(JSON.stringify(b))}</b></span></div>`;
      }).join('')}</div></div>`;
      const rest = Object.entries(d).filter(([k]) => k !== 'changes');
      if (rest.length) details += `<div class="sec"><span class="microlbl">Details</span><dl class="kv">${rest.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === 'string' ? v : JSON.stringify(v))}</dd>`).join('')}</dl></div>`;
    } else if (d && Object.keys(d).length) {
      details = `<div class="sec"><span class="microlbl">Details</span><dl class="kv">${Object.entries(d).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === 'string' ? v : JSON.stringify(v))}</dd>`).join('')}</dl></div>`;
    } else {
      details = '<div class="sec"><span class="microlbl">Details</span><div class="payload">no extra detail recorded for this event</div></div>';
    }
    const tgt = (e.target || '').split(' · ')[0];
    det.innerHTML = `
      <div class="det-h"><span class="microlbl">Entry #${esc(e.id)} · ${start + i + 1} of ${total.toLocaleString()}</span>
        <div class="nav"><button type="button" class="au-ib" title="Newer" data-step="-1" ${start + i <= 0 ? 'disabled' : ''}>↑</button>
        <button type="button" class="au-ib" title="Older" data-step="1" ${start + i >= total - 1 ? 'disabled' : ''}>↓</button>
        <button type="button" class="au-ib" title="Close" data-close="1">×</button></div></div>
      <div class="det-b">
        <div><div class="act"><i class="g-${esc(e.group || 'config')}"></i>${esc(e.action)}<span class="au-pill ${esc(e.outcome || 'ok')}">${esc(e.outcome || '?')}</span></div>
          <div class="said">${esc(e.label || '')}${e.target ? ' — <b>' + esc(e.target) + '</b>' : ''}</div>
          <div class="when"><b>${esc(fmtFull(e.ts))}</b> · ${esc(age(e.ts))} ago</div></div>
        <div class="sec"><span class="microlbl">Who</span><dl class="kv">
          <dt>user</dt><dd class="link" data-actor="${esc(e.actor || (e.auth === 'test' ? 'test' : (e.auth === 'internal' || e.ip === '-') ? 'system' : 'local'))}">${who}</dd>
          <dt>from</dt><dd>${esc(e.ip || '—')}</dd><dt>auth</dt><dd>${esc(e.auth || '—')}</dd></dl></div>
        <div class="sec"><span class="microlbl">Request</span><dl class="kv">
          <dt>method</dt><dd>${esc(methodFor(e))}</dd><dt>path</dt><dd>${esc(e.path || '(internal)')}</dd>
          <dt>status</dt><dd>${e.status != null ? 'HTTP ' + esc(e.status) : '—'}</dd></dl></div>
        ${details}
      </div>
      <div class="det-f">
        <button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-group="${esc(e.group || '')}">Filter: ${esc(e.group || 'all')}</button>
        ${tgt ? `<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-q="${esc(tgt)}">Filter: this target</button>` : ''}
        <button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-copy="1" title="Copy entry as JSON">⧉ JSON</button>
      </div>`;
  }

  // ── events ───────────────────────────────────────────────────────────────
  function go(n) {
    const pages = Math.max(1, Math.ceil(total / state.per));
    state.page = Math.min(Math.max(1, n), pages);
    state.sel = null;
    adminAuditLoad(-1);
  }
  function refilter() { state.page = 1; state.sel = null; adminAuditLoad(-1); }
  async function step(dir) {
    const i = entries.findIndex(x => x.id === state.sel);
    if (i < 0) return;
    const j = i + dir;
    if (j >= 0 && j < entries.length) { state.sel = entries[j].id; render(); return; }
    const pages = Math.max(1, Math.ceil(total / state.per));
    const np = state.page + dir;
    if (np < 1 || np > pages) return;
    state.page = np;
    const keep = state.sel; state.sel = null;
    await adminAuditLoad(-1);
    if (entries.length) { state.sel = dir > 0 ? entries[0].id : entries[entries.length - 1].id; render(); }
    else state.sel = keep;
  }
  function resetFilters() {
    Object.assign(state, DEFAULTS, { hideAuto: state.hideAuto });
    const q = $('auQ'); if (q) q.value = '';
    ['auGroup', 'auActor', 'auOutcome'].forEach(id => { const el = $(id); if (el) el.value = ''; });
    document.querySelectorAll('#auRange button').forEach(b => b.classList.toggle('on', Number(b.dataset.h) === DEFAULTS.hours));
    refilter();
  }
  let qTimer = null;
  function bind() {
    if (bound) return true;
    const tbody = $('adminAuditTbody');
    if (!tbody) return false;
    bound = true;
    readHash();
    const q = $('auQ');
    if (q) {
      q.value = state.q;
      q.addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.q = q.value.trim(); refilter(); }, 220); });
      q.addEventListener('keydown', ev => { if (ev.key === 'Escape') { q.value = ''; state.q = ''; refilter(); q.blur(); } });
    }
    document.addEventListener('keydown', ev => {
      if (ev.key !== '/' || ev.ctrlKey || ev.metaKey || ev.altKey) return;
      const tag = (document.activeElement && document.activeElement.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (typeof _subTabState !== 'undefined' && _subTabState.admin !== 'audit') return;
      if (typeof _activeTab !== 'undefined' && _activeTab !== 'admin') return;
      ev.preventDefault(); if (q) q.focus();
    });
    const wire = (id, key) => { const el = $(id); if (!el) return; el.value = state[key]; el.addEventListener('change', () => { state[key] = el.value; refilter(); }); };
    wire('auGroup', 'group'); wire('auActor', 'actor'); wire('auOutcome', 'outcome');
    document.querySelectorAll('#auRange button').forEach(b => {
      b.classList.toggle('on', Number(b.dataset.h) === state.hours);
      b.addEventListener('click', () => {
        document.querySelectorAll('#auRange button').forEach(x => x.classList.remove('on'));
        b.classList.add('on'); state.hours = Number(b.dataset.h) || 0; refilter();
      });
    });
    const reset = $('auReset'); if (reset) reset.addEventListener('click', resetFilters);
    const hide = $('auHideAuto');
    if (hide) {
      hide.classList.toggle('on', state.hideAuto); hide.setAttribute('aria-pressed', String(state.hideAuto));
      hide.addEventListener('click', () => { state.hideAuto = !state.hideAuto; hide.classList.toggle('on', state.hideAuto); hide.setAttribute('aria-pressed', String(state.hideAuto)); refilter(); });
    }
    const per = $('auPer'); if (per) per.addEventListener('change', () => { state.per = Number(per.value) || 25; perFromServer = true; refilter(); });
    const nums = $('auPnums'); if (nums) nums.addEventListener('click', ev => { const b = ev.target.closest('button[data-go]'); if (b && !b.disabled) go(Number(b.dataset.go)); });
    const jump = $('auJump'); if (jump) jump.addEventListener('keydown', ev => { if (ev.key === 'Enter') { go(Number(jump.value) || 1); jump.value = ''; } });
    document.querySelectorAll('#adminAuditTable th[data-key]').forEach(th => th.addEventListener('click', () => {
      const key = th.dataset.key;
      if (state.sort === key) state.dir = state.dir === 'desc' ? 'asc' : 'desc';
      else { state.sort = key; state.dir = key === 'ts' ? 'desc' : 'asc'; }
      refilter();
    }));
    tbody.addEventListener('click', ev => {
      const tr = ev.target.closest('tr[data-id]'); if (!tr) return;
      const id = Number(tr.dataset.id);
      state.sel = state.sel === id ? null : id; render();
    });
    const det = $('auDet');
    if (det) det.addEventListener('click', ev => {
      const t = ev.target.closest('[data-step],[data-close],[data-group],[data-q],[data-copy],[data-actor]');
      if (!t) return;
      if (t.dataset.step) step(Number(t.dataset.step));
      else if (t.dataset.close) { state.sel = null; render(); }
      else if (t.dataset.group !== undefined) { state.group = t.dataset.group; const g = $('auGroup'); if (g) g.value = state.group; refilter(); }
      else if (t.dataset.q !== undefined) { state.q = t.dataset.q; if (q) q.value = state.q; refilter(); }
      else if (t.dataset.actor !== undefined) { state.actor = t.dataset.actor; const a = $('auActor'); if (a) a.value = state.actor; refilter(); }
      else if (t.dataset.copy) { const e = entries.find(x => x.id === state.sel); if (e && navigator.clipboard) navigator.clipboard.writeText(JSON.stringify(e, null, 2)).catch(() => {}); }
    });
    bindSettings();
    return true;
  }

  // ── settings card ────────────────────────────────────────────────────────
  function bindSettings() {
    const head = $('auCfgHead'), card = $('auCfg');
    if (!head || !card) return;
    head.addEventListener('click', () => {
      card.classList.toggle('collapsed');
      if (!card.classList.contains('collapsed') && !cfg) loadSettings();
    });
    const body = $('auCfgBody');
    if (body) body.addEventListener('click', ev => {
      const tg = ev.target.closest('.mc-toggle[data-ev],.mc-toggle[data-cfg]');
      if (tg) {
        if (tg.dataset.ev) cfgForm.enabled[tg.dataset.ev] = !cfgForm.enabled[tg.dataset.ev];
        else cfgForm.saveAutomated = !cfgForm.saveAutomated;
        renderSettings(); return;
      }
      const all = ev.target.closest('[data-all]');
      if (all && cfg) {
        const on = all.dataset.all === 'on';
        cfg.groups.filter(g => g.key === all.dataset.group).forEach(g => g.events.forEach(e => { if (!e.hidden) cfgForm.enabled[e.key] = on; }));
        renderSettings();
      }
    });
    const foot = $('auCfgFoot');
    if (foot) foot.addEventListener('click', ev => {
      if (ev.target.closest('[data-save]')) saveSettings();
      else if (ev.target.closest('[data-defaults]') && cfg) {
        cfgForm = { retention: 60, pageSize: 25, saveAutomated: false, automatedActors: '', enabled: {} };
        cfg.groups.forEach(g => g.events.forEach(e => { cfgForm.enabled[e.key] = !!e.default_on; }));
        renderSettings();
      }
    });
  }
  async function loadSettings() {
    const body = $('auCfgBody');
    try {
      const r = await fetch('/api/admin/audit-log/events');
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
      cfg = d;
      cfgForm = { retention: d.config.retention_days, pageSize: d.config.page_size, saveAutomated: !!d.config.save_automated,
                  automatedActors: (d.config.automated_actors || []).join(', '), enabled: {} };
      d.groups.forEach(g => g.events.forEach(e => { cfgForm.enabled[e.key] = !!e.enabled; }));
      renderSettings();
    } catch (e) {
      if (body) body.innerHTML = `<div class="au-empty">Failed to load audit settings — ${esc(e.message)}</div>`;
    }
  }
  function toggle(label, on, attrs) {
    return `<button type="button" class="mc-toggle ${on ? 'on' : ''}" ${attrs} aria-pressed="${on}"><span class="track"></span><span class="tlbl">${label}</span></button>`;
  }
  function renderSettings() {
    const body = $('auCfgBody'), foot = $('auCfgFoot');
    if (!body || !cfg || !cfgForm) return;
    const ret = Math.max(0, Math.floor(Number(cfgForm.retention) || 0));
    const per = Math.max(10, Math.floor(Number(cfgForm.pageSize) || 25));
    const p = stats && stats.purge ? stats.purge : {};
    const purge = p.ts ? `last purge <b>${esc(fmtShort(p.ts))}</b> removed <b>${Number(p.removed || 0).toLocaleString()}</b> rows` : 'no purge yet this session';
    const execOn = cfgForm.enabled['autopilot.executor'] !== false;
    body.innerHTML = `
      <div class="au-cfg-grid">
        <div>
          <div class="st-field"><label for="auCfgRet">Keep entries for</label>
            <div class="row"><input class="au-in" id="auCfgRet" type="number" min="0" max="3650" value="${ret}"><span class="unit">days</span></div>
            <div class="help">Older entries are purged once every 24 hours and at manager start. Set 0 to keep everything (row cap of 100,000 still applies).</div></div>
          <div class="st-field"><label for="auCfgPer">Rows per page</label>
            <div class="row"><select class="au-sel" id="auCfgPer">${[10, 25, 50, 100].map(n => `<option ${n === per ? 'selected' : ''}>${n}</option>`).join('')}</select></div>
            <div class="help">Default page size on the Audit Log tab. The page dropdown overrides it per session.</div></div>
          <div class="st-field"><label>Automated sources</label>
            ${toggle('Autopilot actions', execOn, 'data-ev="autopilot.executor"')}
            ${toggle('Unit tests', cfgForm.saveAutomated, 'data-cfg="save_automated"')}
            <div class="help">Requests tagged <code>X-LLMSys-Source: test</code> (unit tests) and the automated users below are excluded when disabled.</div></div>
          <div class="st-field"><label for="auCfgAuto">Automated users</label>
            <div class="row"><input class="au-in" id="auCfgAuto" type="text" placeholder="smoketestuser" spellcheck="false" style="width:100%"></div>
            <div class="help">Usernames whose actions count as automated: hidden by <b>Hide automated</b>, recorded only while Unit tests is on. Comma-separated.</div></div>
          <div class="au-stat"><b>${Number(stats ? stats.total : total).toLocaleString()}</b> rows · ${purge}</div>
        </div>
        <div>
          <div class="microlbl" style="display:block;margin-bottom:10px">Events to record</div>
          <div class="au-evgrid">${cfg.groups.map(g => `
            <div class="au-evg"><h5><i class="g-${esc(g.key)}"></i>${esc(g.title)}<span class="all"><span data-all="on" data-group="${esc(g.key)}">all</span> · <span data-all="off" data-group="${esc(g.key)}">none</span></span></h5>
              <div class="tg">${g.events.filter(e => !e.hidden && e.key !== 'autopilot.executor').map(e => toggle(esc(e.label), cfgForm.enabled[e.key] !== false, `data-ev="${esc(e.key)}"`)).join('')}</div></div>`).join('')}
          </div>
        </div>
      </div>`;
    const retEl = $('auCfgRet'); if (retEl) retEl.addEventListener('input', () => { cfgForm.retention = retEl.value; });
    const perEl = $('auCfgPer'); if (perEl) perEl.addEventListener('change', () => { cfgForm.pageSize = perEl.value; });
    const autoEl = $('auCfgAuto');
    if (autoEl) { autoEl.value = cfgForm.automatedActors || ''; autoEl.addEventListener('input', () => { cfgForm.automatedActors = autoEl.value; }); }
    if (foot) foot.innerHTML = `<button type="button" class="mcbtn mcbtn-pri mcbtn-sm" data-save="1">Save</button>
      <button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-defaults="1">Reset to defaults</button>
      <span class="microlbl">applies without restart</span><span class="msg" id="auCfgMsg"></span>`;
  }
  async function saveSettings() {
    if (!cfg || !cfgForm) return;
    const msg = $('auCfgMsg');
    const events = cfg.groups.flatMap(g => g.events);
    const changes = settingsChanges(cfgForm, events);
    try {
      const r = await fetch('/api/admin/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ changes }) });
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || (d.errors && Object.values(d.errors).join('; ')) || ('HTTP ' + r.status));
      if (msg) { msg.textContent = 'Saved'; msg.className = 'msg ok'; }
      state.per = changes['manager.audit.page_size']; syncPerSelect();
      cfg = null; stats = null; await loadSettings();
      adminAuditLoad(-1);
    } catch (e) {
      if (msg) { msg.textContent = 'Save failed — ' + e.message; msg.className = 'msg err'; }
    }
  }

  window.adminAuditLoad = adminAuditLoad;
})();
