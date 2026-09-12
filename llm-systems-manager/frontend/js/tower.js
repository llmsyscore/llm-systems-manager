// Tower drawer (#924): open/pin/resize, state poll, run + SSE, composer.
// DOM only; logic in js/lib/tower-view.js.
(() => {
  'use strict';
  const KEY = 'lsm.tower';
  const $ = id => document.getElementById(id);
  let _api = null, _view = null, _state = null, _thread = null, _sse = null, _runId = null, _notice = null;
  let _openTicks = new Set(), _lastKey = '', _unread = 0, _pending = [];
  let _prefs = { open: false, pinned: false, width: 400, thread: null, noCtx: false };

  function loadPrefs() { try { _prefs = { ..._prefs, ...(JSON.parse(localStorage.getItem(KEY) || '{}')) }; } catch (_) { /* fresh */ } }
  function savePrefs() { try { localStorage.setItem(KEY, JSON.stringify(_prefs)); } catch (_) { /* private mode */ } }

  // Identifies everything the drawer body is rendered from: state view + page.
  function bodyKey(v) {
    if (!v) return '';
    const p = pageContext();
    return [v.off, v.noModel, v.admin, v.chip ? `${v.chip.model}|${v.chip.provider}|${v.chip.host}` : '',
            p.tab || '', p.sub || ''].join('\u0001');
  }

  // Only a 200 payload replaces the known state; a failed poll changes nothing.
  async function towerRefreshState() {
    let next = null;
    try { const r = await fetch('/api/tower/state'); if (r.ok) next = await r.json(); } catch (_) { /* offline */ }
    if (!next) return;
    _api = next;
    _view = TW.stateView(_api);
    const btn = $('towerBtn');
    if (!btn) return;
    btn.hidden = _view.off && !_view.admin;
    btn.classList.toggle('off', _view.off);
    if (_view.off && !_view.admin && isOpen()) towerClose();
    paintHeader();
    if (isOpen() && bodyKey(_view) !== _lastKey) refreshBody();
  }

  function isOpen() { return !!($('towerOverlay') && $('towerOverlay').classList.contains('open')); }
  function towerToggle() { isOpen() ? towerClose() : towerOpen(); }

  // Repaints the body; the off and no-model states create no thread.
  function refreshBody() {
    if (!_view || _view.off || _view.noModel) { paintBody(); return; }
    ensureThread().then(paintBody);
  }

  // A reply that lands while the drawer is closed pulses the header button until it is opened.
  function markUnread() {
    _unread += 1;
    const btn = $('towerBtn'), badge = $('towerBadge');
    btn?.classList.add('unread');
    if (badge) { badge.textContent = String(_unread); badge.hidden = false; }
  }
  function clearUnread() {
    _unread = 0;
    $('towerBtn')?.classList.remove('unread');
    const badge = $('towerBadge'); if (badge) { badge.textContent = ''; badge.hidden = true; }
  }

  function towerOpen() {
    const ov = $('towerOverlay'); if (!ov) return;
    ov.classList.add('open');
    $('towerBtn')?.classList.add('open');
    clearUnread();
    document.addEventListener('keydown', onKey);
    _prefs.open = true; savePrefs();
    applyDock();
    refreshBody();
    setTimeout(() => $('towerAside')?.focus({ preventScroll: true }), 0);
  }
  function towerClose(opts) {
    $('towerOverlay')?.classList.remove('open');
    $('towerBtn')?.classList.remove('open');
    document.body.classList.remove('tw-docked');
    _prefs.open = false; savePrefs();
    const btn = $('towerBtn');
    if (btn && !btn.hidden && !(opts && opts.returnFocus === false)) btn.focus({ preventScroll: true });
  }
  function applyDock() {
    document.body.classList.toggle('tw-docked', !!_prefs.pinned && isOpen());
    document.documentElement.style.setProperty('--tw-w', Math.max(360, Math.min(560, _prefs.width || 400)) + 'px');
    $('twPin')?.classList.toggle('on', !!_prefs.pinned);
    $('twPin')?.setAttribute('aria-pressed', String(!!_prefs.pinned));
  }
  function onKey(ev) {
    if (ev.key === 'Escape' && isOpen() && !_prefs.pinned) towerClose();
  }
  // Tab and sub-tab switches repaint the context chip and, when the page changed, the body.
  function onTabChange() {
    if (!isOpen() || !_view) return;
    paintHeader();
    if (bodyKey(_view) !== _lastKey) refreshBody();
  }
  document.addEventListener('lsm:tab', onTabChange);
  document.addEventListener('keydown', ev => {
    if (ev.altKey && !ev.ctrlKey && !ev.metaKey && (ev.key === 't' || ev.key === 'T' || ev.key === '†')) {
      const tag = (ev.target && ev.target.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      const btn = $('towerBtn');
      if (btn && !btn.hidden) { ev.preventDefault(); towerToggle(); }
    }
  });

  function pageContext() {
    const cards = [...document.querySelectorAll('.tab-panel.active [data-card], .sub-tab-panel.active [data-card]')]
      .filter(el => el.offsetParent !== null).map(el => el.dataset.card);
    const alert = (window._activeAlerts || [])[0];
    return TW.pageContext({ tab: typeof _activeTab !== 'undefined' ? _activeTab : '',
                            sub: typeof _getDashSubTab === 'function' ? _getDashSubTab() : '',
                            cards, alertId: alert && alert.id });
  }

  async function ensureThread() {
    if (_thread) return _thread;
    if (_prefs.thread) {
      const r = await fetch(`/api/tower/threads/${encodeURIComponent(_prefs.thread)}`);
      if (r.ok) { const d = await r.json(); if (d.ok) { _thread = d.thread; _state = { ...TW.initial(), turns: TW.threadView(d.messages) }; return _thread; } }
    }
    return newThread();
  }
  async function newThread() {
    await abortRun();
    _notice = null; _openTicks = new Set(); _pending = [];
    const r = await fetch('/api/tower/threads', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ page: pageContext() }) });
    const d = r.ok ? await r.json() : { ok: false };
    _thread = d.ok ? d.thread : null;
    _state = TW.initial();
    _prefs.thread = _thread ? _thread.id : null; savePrefs();
    return _thread;
  }

  function paintHeader() {
    const chip = $('twModelChip'), ctx = $('twCtxChip');
    if (chip) {
      const c = _view && _view.chip;
      if (c) {
        const sfx = [c.provider, c.host].filter(Boolean).map(x => `· ${TW.esc(x)}`).join(' ');
        chip.innerHTML = `<i></i><b>${TW.esc(c.model)}</b>${sfx ? `<span class="sfx">${sfx}</span>` : ''}`;
        chip.title = [c.model, c.provider, c.host].filter(Boolean).join(' · ');
      } else if (_view && _view.off) {
        chip.innerHTML = '<i class="dim"></i><b>Off</b>'; chip.title = 'Tower is off';
      } else {
        chip.innerHTML = '<i class="warn"></i><b>No model loaded</b>'; chip.title = 'No chat model is loaded';
      }
    }
    if (ctx) {
      const p = pageContext();
      const label = [p.tab, p.sub].filter(Boolean).join(' · ') || 'no page context';
      ctx.innerHTML = `${TW.esc(label)}<span class="x">✕</span>`;
      ctx.title = _prefs.noCtx ? 'Page context is excluded · click to send it' : 'Page context sent with each question · click to exclude';
      ctx.setAttribute('aria-pressed', String(!_prefs.noCtx));
      ctx.classList.toggle('off', !!_prefs.noCtx);
    }
  }

  function rawHtml(t) {
    if (t.result == null) return '';
    const s = typeof t.result === 'string' ? t.result : JSON.stringify(t.result, null, 2);
    return `<pre class="raw">${TW.esc(s.slice(0, 4000))}</pre>`;
  }
  function tickHtml(t, key) {
    const open = _openTicks.has(key);
    const label = String(t.summary || t.name || '').replace(/\s*·\s*\d+\s*ms\s*$/, '');
    const ms = t.ms == null ? '' : `${t.ms} ms`;
    return `<button type="button" class="tick${t.ok ? '' : ' bad'}${open ? ' open' : ''}" data-tk="${TW.esc(key)}" aria-expanded="${open}">`
      + `<span class="k">${t.ok ? '▸' : '✕'}</span>${TW.esc(label)}<span class="ms">${TW.esc(ms)}</span></button>${rawHtml(t)}`;
  }
  // Keeps the streaming caret inline at the end of the last rendered paragraph.
  function answerHtml(t) {
    const html = TW.md(t.text);
    if (t.done) return html;
    return html.endsWith('</p>') ? `${html.slice(0, -4)}<span class="caret"></span></p>` : `${html}<span class="caret"></span>`;
  }
  function turnHtml(t, i) {
    if (t.role === 'user') return `<div class="u">${TW.esc(t.text)}</div>`;
    const log = t.ticks.length ? `<div class="log">${t.ticks.map((k, j) => tickHtml(k, `${i}:${j}`)).join('')}</div>` : '';
    const body = t.text ? `<div class="ans">${answerHtml(t)}</div>` : (t.done ? '' : '<div class="ans"><span class="caret"></span></div>');
    const drop = t.truncated ? '<div class="drop">some output was dropped</div>' : '';
    const err = t.error ? `<div class="notice"><h4><i></i>${TW.esc(t.error)}</h4></div>` : '';
    const note = t.note ? `<div class="tick"><span class="k">·</span>${TW.esc(t.note)}</div>` : '';
    return `<div class="t">${log}${body}${drop}${note}${err}</div>`;
  }
  function noticeHtml() {
    return _notice ? `<div class="notice"><h4><i></i>${TW.esc(_notice)}</h4></div>` : '';
  }
  // Questions typed while Tower is busy wait here and go out one at a time.
  function pendingHtml() {
    return _pending.map((t, i) => `<div class="u queued"><span class="ql">queued</span>${TW.esc(t)}`
      + `<button type="button" class="qx" data-qx="${i}" title="Remove from queue" aria-label="Remove queued question">✕</button></div>`).join('');
  }

  function paintBody(opts) {
    const body = $('twBody'), box = $('twBox'), input = $('twInput'), sugs = $('twSugs');
    if (!body || !_view) return;
    _lastKey = bodyKey(_view);
    const prevTop = body.scrollTop;
    const atBottom = (opts && opts.toBottom) || body.scrollHeight - prevTop - body.clientHeight < 24;
    body.onclick = null;
    paintHeader();
    if (_view.off) {
      body.innerHTML = `<div class="on-card"><h3>Turn on Tower</h3><p>Answers questions about your hosts, models, alerts and energy using a model you already run behind the gateway. Nothing leaves the lab. It starts <b>read-only</b> and declines anything outside this manager; actions and alarm diagnosis are separate switches in Settings.</p>`
          + `<div class="foot"><button type="button" class="mcbtn mcbtn-pri mcbtn-sm" id="twEnable">Turn on</button><a href="#" id="twSettingsLink">All settings →</a></div></div>`;
      box?.classList.add('dis'); if (input) input.disabled = true; if (sugs) sugs.innerHTML = '';
      return;
    }
    if (_view.noModel) {
      body.innerHTML = `<div class="empty"><h3>Nothing to think with</h3><p>No chat model is loaded on any host, and Tower never loads one on its own. Load a model in <b>LLM Control</b> and this drawer wakes up.</p>`
        + `<div class="fu"><button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" onclick="switchTab('llm')">Open LLM Control</button></div></div>`;
      box?.classList.add('dis'); if (input) input.disabled = true; if (sugs) sugs.innerHTML = '';
      return;
    }
    box?.classList.remove('dis'); if (input) input.disabled = false;
    const turns = (_state && _state.turns) || [];
    if (!turns.length) {
      const p = pageContext();
      body.innerHTML = `<div class="empty"><h3>Ask about your hosts</h3><p>Tower reads live telemetry, alerts, models, energy and recent runs through the gateway.${p.tab ? ` It knows you are on <b>${TW.esc(p.tab)}</b>.` : ''}</p>`
        + `<div class="fu">${TW.suggestions(p).map(s => `<button type="button" class="sug" data-sug="${TW.esc(s)}">${TW.esc(s)}</button>`).join('')}</div></div>` + noticeHtml();
    } else {
      body.innerHTML = turns.map(turnHtml).join('') + pendingHtml() + noticeHtml();
    }
    body.scrollTop = atBottom ? body.scrollHeight : prevTop;
    if (sugs) sugs.innerHTML = turns.length ? TW.suggestions(pageContext()).map(s => `<button type="button" class="sug" data-sug="${TW.esc(s)}">${TW.esc(s)}</button>`).join('') : '';
    const busy = !!(_state && _state.status !== 'idle');
    if (input) input.placeholder = busy ? 'Ask another — it goes next' : 'Ask Tower…';
    const sendBtn = $('twSend');
    if (sendBtn) {
      sendBtn.classList.toggle('stop', busy);
      sendBtn.textContent = busy ? '■' : '→';
      sendBtn.title = busy ? 'Stop' : 'Send (Enter)';
      sendBtn.setAttribute('aria-label', busy ? 'Stop' : 'Send');
    }
    $('towerAside')?.classList.toggle('streaming', busy);
    $('towerBtn')?.classList.toggle('streaming', busy);
  }

  function closeStream() {
    if (_sse) { _sse.close(); _sse = null; }
    _runId = null;
  }
  // Drops the client stream and asks the manager to cancel any active run.
  async function abortRun() {
    const rid = _runId;
    closeStream();
    if (rid) { try { await fetch(`/api/tower/runs/${encodeURIComponent(rid)}/stop`, { method: 'POST' }); } catch (_) { /* already gone */ } }
  }

  async function send(text, fromInput) {
    const input = $('twInput');
    const t = String((fromInput ? (input && input.value) : text) || '').trim();
    if (!t) return;
    if (_state && _state.status !== 'idle') {
      if (_pending.length >= 5) { _notice = 'Five questions are already waiting.'; paintBody(); return; }
      _pending.push(t);
      if (fromInput && input) { input.value = ''; input.style.height = 'auto'; }
      paintBody({ toBottom: true });
      return;
    }
    if (!_thread) await ensureThread();
    if (!_thread) { _notice = 'Tower could not start a thread; try again.'; paintBody(); return; }
    if (fromInput && input) { input.value = ''; input.style.height = 'auto'; }
    _notice = null;
    _state = TW.reduce(_state || TW.initial(), { event: 'user', text: t });
    paintBody({ toBottom: true });
    const page = _prefs.noCtx ? {} : pageContext();
    const r = await fetch(`/api/tower/threads/${encodeURIComponent(_thread.id)}/messages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: t, page }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) {
      const msg = d.error === 'no_model' ? 'No chat model is loaded right now.'
        : d.error === 'run_active' ? 'Tower is still answering; wait for it to finish.'
        : d.error === 'rate_limited' ? 'Too many questions in a minute; try again shortly.'
        : d.error === 'text required' ? 'That question was empty.'
        : 'Tower could not start; try again.';
      _state = TW.reduce(_state, { event: 'error', message: msg }); paintBody(); return;
    }
    _runId = d.run_id;
    _sse = SG.open({ url: `/api/tower/runs/${encodeURIComponent(_runId)}/stream`, bypassPause: true,
                     onEvent: ev => {
                       _state = TW.reduce(_state, ev); paintBody();
                       if (ev.event === 'done' || ev.event === 'error') { closeStream(); if (!isOpen()) markUnread(); drainPending(); }
                     },
                     onLost: () => { _state = TW.reduce(_state, { event: 'error', message: 'Lost the connection to Tower.' }); paintBody(); closeStream(); } });
  }

  function drainPending() {
    if (!_pending.length || (_state && _state.status !== 'idle')) return;
    send(_pending.shift());
  }

  // Stops the active run; only a 404 also tears the client stream down.
  async function stop() {
    if (!_runId) return;
    let res = null;
    try { res = await fetch(`/api/tower/runs/${encodeURIComponent(_runId)}/stop`, { method: 'POST' }); } catch (_) { /* offline */ }
    if (res && res.status === 404) {
      closeStream();
      _state = TW.reduce(_state, { event: 'error', message: 'Tower stopped.' });
      paintBody();
      return;
    }
    if (!res || !res.ok) { _notice = 'Could not stop; the answer will finish on its own.'; paintBody(); }
  }

  function bind() {
    const input = $('twInput'), sendBtn = $('twSend'), body = $('twBody'), sugs = $('twSugs');
    if (!input || input._twBound) return;
    input._twBound = true;
    input.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); send(null, true); }
    });
    input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(120, input.scrollHeight) + 'px'; });
    sendBtn?.addEventListener('click', () => { if (sendBtn.classList.contains('stop')) stop(); else send(null, true); });
    const onSug = ev => { const b = ev.target.closest('[data-sug]'); if (b) send(b.dataset.sug); };
    body?.addEventListener('click', ev => {
      onSug(ev);
      if (ev.target.closest('#twEnable')) { ev.preventDefault(); enable(); }
      if (ev.target.closest('#twSettingsLink')) { ev.preventDefault(); towerClose({ returnFocus: false }); switchTab('admin'); switchSubTab('admin', 'settings'); if (typeof adminSettingsOpenGroup === 'function') adminSettingsOpenGroup('tower'); }
      const qx = ev.target.closest('[data-qx]');
      if (qx) { _pending.splice(Number(qx.dataset.qx), 1); paintBody(); return; }
      const tick = ev.target.closest('.tick[data-tk]');
      if (tick) {
        const open = !_openTicks.has(tick.dataset.tk);
        if (open) _openTicks.add(tick.dataset.tk); else _openTicks.delete(tick.dataset.tk);
        tick.classList.toggle('open', open);
        tick.setAttribute('aria-expanded', String(open));
      }
    });
    sugs?.addEventListener('click', onSug);
    $('twNew')?.addEventListener('click', () => newThread().then(paintBody));
    $('twPin')?.addEventListener('click', () => { _prefs.pinned = !_prefs.pinned; savePrefs(); applyDock(); });
    $('twCtxChip')?.addEventListener('click', () => { _prefs.noCtx = !_prefs.noCtx; savePrefs(); paintHeader(); });
    $('twHist')?.addEventListener('click', showHistory);
    bindGrip($('twGrip'));
  }

  function bindGrip(grip) {
    grip?.addEventListener('pointerdown', ev => {
      ev.preventDefault();
      try { grip.setPointerCapture(ev.pointerId); } catch (_) { /* no capture */ }
      document.body.style.userSelect = 'none';
      const move = e => { _prefs.width = Math.max(360, Math.min(560, window.innerWidth - e.clientX)); applyDock(); };
      const up = () => {
        document.body.style.userSelect = '';
        try { grip.releasePointerCapture(ev.pointerId); } catch (_) { /* already released */ }
        savePrefs();
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
        document.removeEventListener('pointercancel', up);
      };
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
      document.addEventListener('pointercancel', up);
    });
  }

  function historyRow(t) {
    return `<div class="hrow"><button type="button" class="sug" data-thread="${TW.esc(t.id)}">${TW.esc(t.title)}</button>`
      + `<button type="button" class="ib del" data-del="${TW.esc(t.id)}" title="Delete conversation" aria-label="Delete ${TW.esc(t.title)}">✕</button></div>`;
  }
  // Deleting the current thread forgets it locally; the next question starts a new one.
  async function deleteThread(id) {
    const r = await fetch(`/api/tower/threads/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => null);
    if (!r || (!r.ok && r.status !== 404)) { _notice = 'Could not delete that conversation.'; return false; }
    if (_thread && _thread.id === id) { await abortRun(); _thread = null; _state = TW.initial(); _openTicks = new Set(); _pending = []; }
    if (_prefs.thread === id) { _prefs.thread = null; savePrefs(); }
    return true;
  }
  async function showHistory() {
    const r = await fetch('/api/tower/threads'); const d = r.ok ? await r.json() : { threads: [] };
    const body = $('twBody'); if (!body) return;
    const threads = d.threads || [];
    body.innerHTML = `<div class="ins-h"><h3>History</h3><span class="cnt">${threads.length}</span><button type="button" class="lnk" id="twHistBack">Back</button></div>`
      + (threads.length ? `<div class="hist">${threads.map(historyRow).join('')}</div>` : '<p class="hist-empty">No past conversations.</p>')
      + noticeHtml();
    body.onclick = async ev => {
      const b = ev.target.closest('[data-thread]');
      if (b) {
        await abortRun();
        _notice = null; _openTicks = new Set(); _pending = [];
        _prefs.thread = b.dataset.thread; _thread = null; savePrefs();
        await ensureThread(); paintBody();
      }
      const del = ev.target.closest('[data-del]');
      if (del) {
        if (!del.classList.contains('arm')) {
          body.querySelectorAll('.del.arm').forEach(x => { x.classList.remove('arm'); x.textContent = '✕'; });
          del.classList.add('arm'); del.textContent = 'Delete'; return;
        }
        const ok = await deleteThread(del.dataset.del);
        if (ok) { const row = del.closest('.hrow'); row?.remove(); const cnt = body.querySelector('.cnt'); if (cnt) cnt.textContent = String(body.querySelectorAll('.hrow').length); }
        else showHistory();
      }
      if (ev.target.closest('#twHistBack')) paintBody();
    };
  }

  async function enable() {
    const r = await fetch('/api/admin/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ changes: { 'manager.tower.enabled': true } }) });
    if (r.ok) { _thread = null; await towerRefreshState(); }
    else if (typeof showToast === 'function') showToast('Tower', 'Could not turn Tower on — check Admin › Settings.', 'warning');
  }

  function init() {
    loadPrefs(); bind(); applyDock();
    towerRefreshState().then(() => { if (_prefs.open && _api && _api.enabled) towerOpen(); });
    setInterval(towerRefreshState, 30000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();

  window.towerToggle = towerToggle;
  window.towerOpen = towerOpen;
  window.towerClose = towerClose;
  window.towerRefreshState = towerRefreshState;
})();
