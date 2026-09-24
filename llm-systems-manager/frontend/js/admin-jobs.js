// Admin → Jobs (#1038): job ledger with status/kind/user filters, paging and a detail panel.
// Talks to /api/jobs and /api/jobs/<id>{,/cancel,/ack}; reuses the Audit Log ledger styles.
(() => {
  'use strict';

  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const DEFAULTS = { status: 'all', kind: '', user: '' };
  const PILL = { queued: 'queued', running: 'ok', done: 'done', failed: 'error', cancelled: 'refused' };
  const esc = s => (typeof adminEsc === 'function' ? adminEsc(s) : String(s == null ? '' : s));

  // ── pure helpers (exported on window.JobsView for tests) ─────────────────
  function fmtTs(ts) {
    if (typeof ts !== 'number' || !ts) return '—';
    const d = new Date(ts * 1000);
    let h = d.getHours();
    const ap = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    return `${MON[d.getMonth()]} ${d.getDate()} · ${h}:${String(d.getMinutes()).padStart(2, '0')} ${ap}`;
  }
  function jobWhen(r) {
    if (r.status === 'queued') return r.next_run ? 'next ' + fmtTs(r.next_run) : 'waiting';
    if (r.status === 'running') return 'started ' + fmtTs(r.started);
    return r.resolved ? fmtTs(r.resolved) : '—';
  }
  function jobBy(r) { return r.user || r.source || '—'; }
  function pillClass(status) { return PILL[status] || 'refused'; }
  function queryParams(st, per, page) {
    const p = new URLSearchParams();
    p.set('status', st.status || 'all');
    if (st.kind) p.set('kind', st.kind);
    if (st.user) p.set('user', st.user);
    if (per != null) { p.set('limit', String(per)); p.set('offset', String((page - 1) * per)); p.set('facets', '1'); }
    return p;
  }
  function atDefaults(st) { return st.status === DEFAULTS.status && !st.kind && !st.user; }
  function pageList(page, pages) {
    const set = new Set([1, pages, page - 1, page, page + 1].filter(n => n >= 1 && n <= pages));
    const out = [];
    let prev = 0;
    [...set].sort((a, b) => a - b).forEach(n => { if (n - prev > 1) out.push('…'); out.push(n); prev = n; });
    return out;
  }

  // ── state ────────────────────────────────────────────────────────────────
  const state = { ...DEFAULTS, page: 1, per: 25, sel: null };
  let rows = [], total = 0, detail = null, seq = 0, bound = false, busy = false, actErr = '';
  const $ = id => document.getElementById(id);

  // offset 0 = fresh entry (page 1, detail closed); no arg = refresh in place.
  async function adminJobsLoad(offset) {
    if (!bind()) return;
    if (offset === 0) { state.page = 1; state.sel = null; detail = null; }
    else if (offset == null && state.sel != null) return;
    const mySeq = ++seq;
    try {
      const d = await fetch('/api/jobs?' + queryParams(state, state.per, state.page)).then(r => r.json());
      if (mySeq !== seq) return;
      if (!d || !d.ok) throw new Error((d && d.error) || 'request failed');
      total = d.total || 0;
      rows = d.jobs || [];
      const pages = Math.max(1, Math.ceil(total / state.per));
      if (state.page > pages) { state.page = pages; return adminJobsLoad(-1); }
      renderFilters(d.kinds || [], d.users || []);
      render();
    } catch (e) {
      if (mySeq !== seq) return;
      rows = []; total = 0;
      const tbody = $('jbTbody');
      if (tbody) tbody.innerHTML = `<tr><td colspan="7"><div class="au-empty">Failed to load jobs — ${esc(e.message)}</div></td></tr>`;
      renderPager();
    }
  }
  async function loadDetail(id) {
    try {
      const d = await fetch('/api/jobs/' + encodeURIComponent(id)).then(r => r.json());
      if (state.sel !== id) return;
      detail = d && d.ok ? d.job : null;
    } catch (_) { detail = null; }
    renderDetail();
  }

  // ── rendering ────────────────────────────────────────────────────────────
  function renderFilters(kinds, users) {
    const fill = (id, first, opts) => {
      const sel = $(id);
      if (!sel) return;
      const cur = state[id === 'jbKind' ? 'kind' : 'user'];
      if (cur && !opts.some(([v]) => v === cur)) opts = [...opts, [cur, cur]];
      const html = `<option value="">${first}</option>` + opts.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join('');
      if (sel._html === html) return;
      sel.innerHTML = html; sel._html = html;
      sel.value = cur;
    };
    fill('jbKind', 'All kinds', kinds.map(k => [k.name, k.title || k.name]));
    fill('jbUser', 'All users', users.map(u => [u, u]));
  }
  function render() {
    const tbody = $('jbTbody');
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7"><div class="au-empty">${atDefaults(state) ? 'No jobs yet.' : 'No jobs match. <b>Reset the filters</b>.'}</div></td></tr>`;
    } else {
      tbody.innerHTML = rows.map(r => `<tr data-id="${esc(r.id)}" class="${state.sel === r.id ? 'sel' : ''}">
        <td class="tg jb-l" title="${esc(r.label || r.id)}"><b>${esc(r.label || r.id)}</b></td>
        <td class="u">${esc(r.kind_title || r.kind)}</td>
        <td class="o"><span class="au-pill ${pillClass(r.status)}">${esc(r.status)}</span></td>
        <td class="u">${esc(jobBy(r))}${r.user && r.source ? `<span class="role">${esc(r.source)}</span>` : ''}</td>
        <td class="t"><b>${esc(fmtTs(r.created))}</b></td>
        <td class="t">${esc(jobWhen(r))}</td>
        <td class="tg c-from" title="${esc(r.message || '')}">${r.message ? esc(r.message) : '<em>—</em>'}</td></tr>`).join('');
    }
    const tot = $('jbTotal'); if (tot) tot.textContent = total.toLocaleString();
    const reset = $('jbReset'); if (reset) reset.classList.toggle('idle', atDefaults(state));
    renderPager();
    renderDetail();
  }
  function renderPager() {
    const pages = Math.max(1, Math.ceil(total / state.per));
    const start = (state.page - 1) * state.per;
    const info = $('jbPageInfo');
    if (info) info.textContent = total ? `${start + 1}–${Math.min(start + state.per, total)} of ${total.toLocaleString()}` : '0 jobs';
    const nums = $('jbPnums');
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
  function payload(title, v) {
    const empty = v == null || (typeof v === 'object' && !Array.isArray(v) && !Object.keys(v).length);
    return `<div class="sec"><span class="microlbl">${title}</span><div class="payload">${empty ? 'none' : esc(JSON.stringify(v, null, 2))}</div></div>`;
  }
  function renderDetail() {
    const det = $('jbDet'), split = $('jbSplit');
    if (!det || !split) return;
    const r = state.sel != null ? (detail && detail.id === state.sel ? detail : rows.find(x => x.id === state.sel)) : null;
    split.classList.toggle('detail', !!r);
    if (!r) { det.innerHTML = ''; return; }
    const full = detail && detail.id === r.id;
    const times = [['created', r.created], ['not before', r.not_before], ['next run', r.next_run], ['started', r.started],
                   ['last run', r.last_run], ['resolved', r.resolved]].filter(([, v]) => v);
    const audit = full && Array.isArray(r.audit)
      ? `<div class="sec"><span class="microlbl">Audit</span>${r.audit.length ? `<dl class="kv">${r.audit.map(a =>
          `<dt>${esc(fmtTs(Date.parse(a.ts) / 1000))}</dt><dd>${esc(a.action)} · ${esc(a.actor || 'system')} · ${esc(a.outcome || '')}</dd>`).join('')}</dl>`
          : '<div class="payload">no audit rows for this job</div>'}</div>` : '';
    det.innerHTML = `
      <div class="det-h"><span class="microlbl">Job ${esc(r.id)}</span>
        <div class="nav"><button type="button" class="au-ib" title="Close" data-close="1">×</button></div></div>
      <div class="det-b">
        <div><div class="act">${esc(r.label || r.id)}<span class="au-pill ${pillClass(r.status)}">${esc(r.status)}</span></div>
          <div class="said">${esc(r.kind_title || r.kind)}${r.message ? ' — <b>' + esc(r.message) + '</b>' : ''}</div></div>
        <div class="sec"><span class="microlbl">Who</span><dl class="kv">
          <dt>submitted by</dt><dd>${esc(r.user || '—')}</dd><dt>source</dt><dd>${esc(r.source || '—')}</dd>
          <dt>runs</dt><dd>${esc(r.run_count || 0)}</dd><dt>attempts</dt><dd>${esc(r.attempts || 0)}</dd></dl></div>
        <div class="sec"><span class="microlbl">Times</span><dl class="kv">${times.map(([k, v]) => `<dt>${k}</dt><dd>${esc(fmtTs(v))}</dd>`).join('')}</dl></div>
        ${full ? payload('Spec', r.spec) + payload('State', r.state) + payload('Result', r.result) : '<div class="au-empty">Loading…</div>'}
        ${audit}
      </div>
      <div class="det-f">
        ${actErr ? `<span class="jb-err">${esc(actErr)}</span>` : ''}
        ${r.can_cancel ? `<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-act="cancel" ${busy ? 'disabled' : ''}>Cancel job</button>` : ''}
        ${r.can_ack ? `<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-act="ack" ${busy ? 'disabled' : ''}>Dismiss failure</button>` : ''}
        <button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-copy="1" title="Copy job as JSON">⧉ JSON</button>
      </div>`;
  }

  // ── events ───────────────────────────────────────────────────────────────
  function go(n) {
    const pages = Math.max(1, Math.ceil(total / state.per));
    state.page = Math.min(Math.max(1, n), pages);
    state.sel = null; detail = null;
    adminJobsLoad(-1);
  }
  function refilter() { state.page = 1; state.sel = null; detail = null; adminJobsLoad(-1); }
  function syncControls() {
    const s = $('jbStatus'); if (s) s.value = state.status;
    const k = $('jbKind'); if (k) k.value = state.kind;
    const u = $('jbUser'); if (u) u.value = state.user;
  }
  async function act(kind) {
    if (state.sel == null || busy) return;
    const id = state.sel;
    busy = true; actErr = ''; renderDetail();
    try {
      const r = await fetch(`/api/jobs/${encodeURIComponent(id)}/${kind}`, { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) actErr = d.error || `request failed (${r.status})`;
    } catch (e) { actErr = e.message || 'request failed'; }
    busy = false;
    if (state.sel !== id) return;
    await adminJobsLoad(-1);
    if (state.sel === id) await loadDetail(id);
    if (typeof adminLoadHealth === 'function') adminLoadHealth();
  }
  function bind() {
    if (bound) return true;
    const tbody = $('jbTbody');
    if (!tbody) return false;
    bound = true;
    syncControls();
    const wire = (id, key) => { const el = $(id); if (el) el.addEventListener('change', () => { state[key] = el.value; refilter(); }); };
    wire('jbStatus', 'status'); wire('jbKind', 'kind'); wire('jbUser', 'user');
    const reset = $('jbReset'); if (reset) reset.addEventListener('click', () => { Object.assign(state, DEFAULTS); syncControls(); refilter(); });
    const per = $('jbPer'); if (per) per.addEventListener('change', () => { state.per = Number(per.value) || 25; refilter(); });
    const nums = $('jbPnums'); if (nums) nums.addEventListener('click', ev => { const b = ev.target.closest('button[data-go]'); if (b && !b.disabled) go(Number(b.dataset.go)); });
    const jump = $('jbJump'); if (jump) jump.addEventListener('keydown', ev => { if (ev.key === 'Enter') { go(Number(jump.value) || 1); jump.value = ''; } });
    tbody.addEventListener('click', ev => {
      const tr = ev.target.closest('tr[data-id]'); if (!tr) return;
      const id = tr.dataset.id;
      state.sel = state.sel === id ? null : id; detail = null; actErr = ''; render();
      if (state.sel) loadDetail(id);
    });
    const det = $('jbDet');
    if (det) det.addEventListener('click', ev => {
      const t = ev.target.closest('[data-close],[data-act],[data-copy]');
      if (!t) return;
      if (t.dataset.close) { state.sel = null; detail = null; actErr = ''; render(); }
      else if (t.dataset.act) act(t.dataset.act);
      else if (t.dataset.copy && detail && navigator.clipboard) navigator.clipboard.writeText(JSON.stringify(detail, null, 2)).catch(() => {});
    });
    return true;
  }
  // Opens the Jobs sub-tab, optionally on one status filter (the System Health strip links here).
  function open(status) {
    Object.assign(state, DEFAULTS, status ? { status } : {});
    syncControls();
    if (typeof switchSubTab === 'function') switchSubTab('admin', 'jobs');
  }

  window.adminJobsLoad = adminJobsLoad;
  window.JobsView = { fmtTs, jobWhen, jobBy, pillClass, queryParams, pageList, open, _state: state };
})();
