// Companion Tower screen (#964): ask/answer over the Tower thread and run routes,
// the Insights list, approval and question cards. Classic script; window.CTower.
(() => {
  const OTHER = '__other__';
  const THREAD_KEY = 'companionTowerThread';
  const TIER_LABEL = { read: 'answer only', operate: 'answer and act', admin: 'answer and act, incl. admin actions' };
  const SEND_ERRORS = {
    no_model: 'No chat model is loaded right now.',
    run_active: 'Tower is still answering; wait for it to finish.',
    rate_limited: 'Too many questions in a minute; try again shortly.',
    'text required': 'That question was empty.',
  };
  const PAGE = { tab: 'companion' };

  function storedThread() { try { return localStorage.getItem(THREAD_KEY) || null; } catch (_) { return null; } }
  function storeThread(id) { try { if (id) localStorage.setItem(THREAD_KEY, id); else localStorage.removeItem(THREAD_KEY); } catch (_) { /* private mode */ } }

  // deps: $, sheet, openAlert(alertId), badge(n), visible() → the screen is on.
  function create(deps) {
    const TW = window.TW, SG = window.SG, $ = deps.$, esc = TW.esc;
    const c = {
      enabled: false, view: TW.stateView(null), thread: null, state: TW.initial(), runId: null, sse: null,
      insights: [], insNew: 0, unread: 0, tab: 'ask', notice: null, insNotice: null, pending: [],
      openTicks: new Set(), openIns: new Set(), qSel: {}, qOther: {}, qTab: {}, optSel: {}, wired: false,
    };

    async function jget(url) {
      const r = await fetch(url, { credentials: 'same-origin' });
      const d = await r.json().catch(() => ({}));
      return { res: r, d };
    }
    async function jpost(url, body) {
      const init = { method: 'POST', credentials: 'same-origin' };
      if (body) { init.headers = { 'Content-Type': 'application/json' }; init.body = JSON.stringify(body); }
      let r = null;
      try { r = await fetch(url, init); } catch (_) { /* offline */ }
      const d = r ? await r.json().catch(() => ({})) : {};
      return { res: r, d };
    }
    const visible = () => (deps.visible ? deps.visible() : true);
    const badge = () => { if (deps.badge) deps.badge(c.unread + c.insNew); };

    // ── state + thread ──────────────────────────────────────────────────────
    async function loadState() {
      try {
        const { res, d } = await jget('/api/tower/state');
        if (!res.ok || !d.ok) return c.enabled;
        c.view = TW.stateView(d); c.enabled = c.view.enabled;
        c.insNew = Number(d.insights_new) || 0; badge();
      } catch (_) { /* keep the last state */ }
      return c.enabled;
    }
    function setState(rows, activeRun) {
      c.state = { ...TW.initial(), turns: TW.threadView(rows) };
      c.openTicks = new Set();
      resumeRun(activeRun);
    }
    async function ensureThread() {
      if (c.thread) return c.thread;
      const id = storedThread();
      if (id) {
        const { res, d } = await jget(`/api/tower/threads/${encodeURIComponent(id)}`).catch(() => ({ res: null, d: {} }));
        if (res && res.ok && d.ok) { c.thread = d.thread; setState(d.messages, d.active_run); return c.thread; }
      }
      return newThread();
    }
    async function newThread() {
      await parkRun();
      c.notice = null; c.pending = []; c.openTicks = new Set();
      const { res, d } = await jpost('/api/tower/threads', { page: PAGE });
      if (!res || !res.ok || !d.ok) { c.thread = null; c.notice = 'Tower could not start a thread; try again.'; return null; }
      c.thread = d.thread; c.state = TW.initial(); storeThread(c.thread.id);
      return c.thread;
    }
    async function openThread(id) {
      if (c.thread && c.thread.id === id) return;
      await parkRun();
      const { res, d } = await jget(`/api/tower/threads/${encodeURIComponent(id)}`).catch(() => ({ res: null, d: {} }));
      if (!res || !res.ok || !d.ok) { c.notice = 'That conversation is gone.'; paint(); return; }
      c.thread = d.thread; c.notice = null; c.pending = []; storeThread(id);
      setState(d.messages, d.active_run);
      c.tab = 'ask'; paint({ toBottom: true });
    }
    // Re-reads the thread while idle so turns that landed elsewhere (a timer report, the dashboard) show up.
    async function reloadThread() {
      if (!c.thread || c.runId || c.state.status !== 'idle') return;
      const id = c.thread.id;
      const { res, d } = await jget(`/api/tower/threads/${encodeURIComponent(id)}`).catch(() => ({ res: null, d: {} }));
      if (!res || !c.thread || c.thread.id !== id) return;
      if (res.status === 404) { c.thread = null; storeThread(null); return; }
      if (!res.ok || !d.ok) return;
      const before = c.state.turns.length;
      c.thread = d.thread; c.state = { ...TW.initial(), turns: TW.threadView(d.messages) };
      resumeRun(d.active_run);
      if (!visible() && c.state.turns.length > before) { c.unread += 1; badge(); }
    }
    // A stored turn still being answered (a timer report) takes the streamed answer instead of a new turn.
    function reopenLastTurn() {
      const turns = c.state.turns.slice(), t = turns[turns.length - 1];
      if (t && t.role === 'tower' && t.done && !t.text && !t.error) { turns[turns.length - 1] = { ...t, done: false }; c.state = { ...c.state, turns }; }
    }
    function resumeRun(activeRun) {
      const live = TW.liveRun(c.state.turns);
      if (live) {
        const turns = c.state.turns.slice();
        turns[turns.length - 1] = { ...turns[turns.length - 1], done: false };
        c.state = { ...c.state, turns, status: live.status === 'running' ? 'thinking' : 'awaiting' };
        if (live.status === 'running') attach(live.runId); else c.runId = live.runId;
        return;
      }
      if (activeRun) { reopenLastTurn(); c.state = TW.reduce(c.state, { event: 'status', state: 'thinking' }); attach(activeRun); }
    }

    // ── run stream ──────────────────────────────────────────────────────────
    function closeStream() { if (c.sse) { c.sse.close(); c.sse = null; } }
    function endRun() { closeStream(); c.runId = null; }
    function attach(runId) {
      c.runId = runId;
      let dropped = false;
      closeStream();
      c.sse = SG.open({
        url: `/api/tower/runs/${encodeURIComponent(runId)}/stream`, bypassPause: true,
        onEvent: (ev) => {
          if (ev.event === 'reattach') { closeStream(); attach(runId); return; }
          c.state = TW.reduce(c.state, ev); paintConv();
          if (ev.event === 'truncated') dropped = true;
          if (ev.event === 'action' && ev.action_id) delete c.optSel[ev.action_id];
          if (ev.event === 'answer' && ev.action_id) forgetQuestion(ev.action_id);
          if (ev.event === 'confirm' || ev.event === 'question') { closeStream(); if (!visible()) { c.unread += 1; badge(); } return; }
          if (ev.event === 'done' || ev.event === 'error') {
            endRun();
            if (!visible()) { c.unread += 1; badge(); }
            if (dropped) { dropped = false; reloadThread().then(paintConv); }
            drainPending();
          }
        },
        onLost: () => {
          if (c.state.status === 'awaiting') { closeStream(); return; }
          c.state = TW.reduce(c.state, { event: 'error', message: 'Lost the connection to Tower.' }); endRun(); paintConv();
        },
      });
    }
    async function send(text) {
      const t = String(text || '').trim();
      if (!t) return;
      c.tab = 'ask';
      if (c.state.status !== 'idle') {
        if (c.pending.length >= 5) { c.notice = 'Five questions are already waiting.'; paint(); return; }
        c.pending.push(t); paint({ toBottom: true }); return;
      }
      if (!c.thread) await ensureThread();
      if (!c.thread) { c.notice = 'Tower could not start a thread; try again.'; paint(); return; }
      c.notice = null;
      if ((c.thread.title || 'New thread') === 'New thread' && !c.state.turns.length) c.thread.title = t.slice(0, 60);
      c.state = TW.reduce(c.state, { event: 'user', text: t });
      paint({ toBottom: true });
      const { res, d } = await jpost(`/api/tower/threads/${encodeURIComponent(c.thread.id)}/messages`, { text: t, page: PAGE });
      if (!res || !res.ok || !d.ok) {
        c.state = TW.reduce(c.state, { event: 'error', message: SEND_ERRORS[d.error] || 'Tower could not start; try again.' });
        paintConv(); return;
      }
      attach(d.run_id);
    }
    function drainPending() {
      if (c.state.status !== 'idle') return;
      if (c.pending.length) send(c.pending.shift());
    }
    async function stop() {
      if (!c.runId) return;
      const { res } = await jpost(`/api/tower/runs/${encodeURIComponent(c.runId)}/stop`);
      if (res && res.status === 404) { endRun(); c.state = TW.reduce(c.state, { event: 'error', message: 'Tower stopped.' }); paintConv(); return; }
      if (res && res.ok && c.state.status === 'awaiting') { closeStream(); attach(c.runId); return; }
      if (!res || !res.ok) { c.notice = 'Could not stop; the answer will finish on its own.'; paintConv(); }
    }
    // Leaves a run to finish on its own; the manager keeps it for a later re-attach.
    async function parkRun() {
      const rid = c.runId, awaiting = c.state.status === 'awaiting';
      closeStream(); c.runId = null;
      if (rid && !awaiting) await jpost(`/api/tower/runs/${encodeURIComponent(rid)}/park`);
      c.state = { ...c.state, status: 'idle' };
    }

    // ── approvals + questions ───────────────────────────────────────────────
    function actionById(aid) {
      for (const t of c.state.turns) for (const a of t.actions || []) if (a.id === aid) return a;
      return null;
    }
    function optionPicks(a) {
      const sel = c.optSel[a.id] || {}, out = {};
      for (const o of (a.card && a.card.options) || []) { if (o && o.name) out[o.name] = sel[o.name] != null ? sel[o.name] : String(o.value == null ? '' : o.value); }
      return out;
    }
    function questionList(a) {
      const qs = (a.card && a.card.questions) || [];
      return qs.length ? qs : [{ question: (a.card && a.card.question) || '', choices: (a.card && a.card.choices) || [], label: '' }];
    }
    function questionAnswers(a) {
      const sel = c.qSel[a.id] || {}, other = c.qOther[a.id] || {};
      return questionList(a).map((q, i) => sel[i] === OTHER ? String(other[i] || '').trim() : (sel[i] == null ? '' : String(sel[i])));
    }
    function forgetQuestion(aid) { delete c.qSel[aid]; delete c.qOther[aid]; delete c.qTab[aid]; }
    function advanceQuestion(aid) {
      const a = actionById(aid);
      if (!a) return;
      const answers = questionAnswers(a), cur = c.qTab[aid] || 0;
      const next = answers.map((_, i) => (cur + 1 + i) % answers.length).find(i => !answers[i]);
      if (next != null) c.qTab[aid] = next;
    }
    function decisionFailed(d, kind) {
      const what = kind === 'question' ? 'question' : 'approval';
      c.notice = d.error === 'not allowed' ? 'Your role or the current tier does not allow this action.'
        : d.error === 'not pending' ? `That ${what} was already decided.`
        : `Tower could not record the ${what === 'question' ? 'answer' : 'decision'}; try again.`;
      paintConv();
      if (d.error === 'not pending' && c.runId) { closeStream(); attach(c.runId); }
    }
    // Records a decision on a card, then re-attaches to the run it releases.
    async function decide(aid, verb, body) {
      c.notice = null;
      const { res, d } = await jpost(`/api/tower/actions/${encodeURIComponent(aid)}/${verb}`, body);
      const question = (actionById(aid) || {}).tool === 'ask_operator';
      if (!res || !res.ok || !d.ok) {
        if (d.error === 'expired') {
          c.notice = question ? 'That question expired; ask again.' : 'That approval expired; ask again.';
          c.state = TW.reduce(c.state, question ? { event: 'answer', action_id: aid, status: 'expired', message: 'no answer from the operator' }
            : { event: 'action', action_id: aid, tool: null, status: 'expired', message: 'approval expired', ms: 0 });
          c.state = TW.reduce(c.state, { event: 'done', ok: false });
          forgetQuestion(aid); endRun(); paintConv(); drainPending(); return;
        }
        decisionFailed(d, question ? 'question' : 'approval'); return;
      }
      closeStream(); forgetQuestion(aid);
      if (verb === 'approve') c.state = TW.reduce(c.state, { event: 'action', action_id: aid, status: 'running' });
      c.state = TW.reduce(c.state, { event: 'status', state: 'thinking' }); paintConv();
      attach(d.run_id);
    }
    function approve(aid) {
      const a = actionById(aid);
      if (!a) return;
      const body = (a.card && (a.card.options || []).length) ? { options: optionPicks(a) } : null;
      deps.sheet.confirm(a.card.title || a.tool, [a.card.target, a.card.does, a.card.not].filter(Boolean).join(' · '), 'Approve', false, () => decide(aid, 'approve', body));
    }
    function submitQuestion(aid) {
      const a = actionById(aid);
      if (!a) return;
      const answers = questionAnswers(a);
      if (!answers.every(Boolean)) return;
      decide(aid, 'answer', { answers });
    }

    // ── insights ────────────────────────────────────────────────────────────
    async function loadInsights() {
      try {
        const { res, d } = await jget('/api/tower/insights');
        if (res.ok && d.ok) { c.insights = TW.visibleInsights(d.insights); c.insNew = Number(d.new) || 0; badge(); }
      } catch (_) { /* keep the last list */ }
    }
    function markSeen() {
      if (!c.insNew || !visible() || c.tab !== 'ins') return;
      c.insNew = 0; badge(); paintIns();
      jpost('/api/tower/insights/seen');
    }
    function applyInsight(id) {
      const r = c.insights.find(x => x.id === id);
      if (!r) return;
      const v = TW.insightView(r, c.view, Date.now() / 1000);
      deps.sheet.confirm(v.title || 'Apply', v.summary, 'Apply', false, async () => {
        c.insNotice = null;
        c.insights = c.insights.map(x => x.id === id ? { ...x, status: 'applying' } : x); paintIns();
        const { res, d } = await jpost(`/api/tower/insights/${encodeURIComponent(id)}/apply`);
        if (!res || !res.ok || !d.ok) {
          c.insNotice = d.error === 'not allowed' ? 'Your role or the current tier does not allow this playbook.'
            : d.error === 'stale' ? 'The alert changed or closed; that playbook no longer fits.'
            : d.error === 'not open' ? 'That insight is already being handled.'
            : (d.result && d.result.message) ? `Playbook failed: ${d.result.message}`
            : 'Tower could not apply the playbook; try again.';
        }
        await loadInsights(); paintIns();
      });
    }
    async function dismissInsight(id) {
      await jpost(`/api/tower/insights/${encodeURIComponent(id)}/dismiss`);
      c.insights = c.insights.filter(r => r.id !== id); paintIns();
    }
    async function dismissAll() {
      await jpost('/api/tower/insights/dismiss_all');
      c.insNotice = null; c.insights = c.insights.filter(r => r.status === 'applying'); paintIns();
    }

    // ── render ──────────────────────────────────────────────────────────────
    function snapshotHtml(snapshot) {
      const sp = snapshot ? TW.sparkline(snapshot, 240, 36) : null;
      if (!sp) return '';
      const thr = sp.thrY !== null ? `<line class="thr" x1="0" x2="${sp.w}" y1="${sp.thrY}" y2="${sp.thrY}"/>` : '';
      return `<div class="snap"><svg viewBox="0 0 ${sp.w} ${sp.h}" preserveAspectRatio="none" aria-hidden="true">${thr}<path d="${sp.d}"/></svg><span class="snapc">${esc(sp.caption)}</span></div>`;
    }
    function rawHtml(t) {
      if (t.result == null) return '';
      let s = typeof t.result === 'string' ? t.result : JSON.stringify(t.result, null, 1);
      if (s.length > 1500) s = s.slice(0, 1500) + '…';
      return `<pre class="raw">${esc(s)}</pre>`;
    }
    function tickHtml(t, key) {
      const open = c.openTicks.has(key);
      const label = String(t.summary || t.name || '').replace(/\s*·\s*\d+\s*ms\s*$/, '');
      const snap = t.name === 'timer' ? snapshotHtml(TW.timerSnapshot(t.result)) : '';
      return `<button type="button" class="tick${t.ok ? '' : ' bad'}${open ? ' open' : ''}" data-tk="${esc(key)}" aria-expanded="${open}">`
        + `<span class="k">${t.ok ? '▸' : '✕'}</span>${esc(label)}<span class="ms">${t.ms == null ? '' : esc(t.ms + ' ms')}</span></button>${snap}${open ? rawHtml(t) : ''}`;
    }
    function answerHtml(t) {
      const html = TW.md(t.text);
      if (t.done) return html;
      return html.endsWith('</p>') ? `${html.slice(0, -4)}<span class="caret"></span></p>` : `${html}<span class="caret"></span>`;
    }
    function optionsHtml(a) {
      const list = (a.card && a.card.options) || [];
      if (!list.length) return '';
      const picks = optionPicks(a);
      return '<div class="opts">' + list.map(o => `<div class="opt"><span class="ol">${esc(o.label || o.name)}</span><div class="chips">`
        + (o.choices || []).map(ch => { const v = String(ch && ch.value != null ? ch.value : ch), l = ch && ch.label != null ? ch.label : v;
          return `<button type="button" class="chip${picks[o.name] === v ? ' on' : ''}" data-opt="${esc(a.id)}" data-name="${esc(o.name)}" data-val="${esc(v)}" aria-pressed="${picks[o.name] === v}">${esc(l)}</button>`; }).join('')
        + '</div></div>').join('') + '</div>';
    }
    function doneTick(ok, text, tail) {
      return `<div class="tick act${ok ? ' ok' : ' bad'}"><span class="k">${ok ? '✓' : '✕'}</span>${text}<span class="ms">${tail}</span></div>`;
    }
    function actionHtml(a) {
      if (a.tool === 'ask_operator') return questionHtml(a);
      const running = a.status === 'running', id = esc(a.id);
      const stale = a.status === 'pending' && a.expires != null && a.expires * 1000 < Date.now();
      if (a.status === 'pending' || running) {
        const mine = a.role !== 'admin' || c.view.admin;
        const eyebrow = running ? 'Running…' : stale ? 'Approval expired' : mine ? 'Needs your approval' : "Needs an admin's approval";
        const live = !stale && !running && mine;
        const btns = live ? `<div class="btns"><button type="button" class="btn sm primary" data-approve="${id}">Approve</button>`
          + `<button type="button" class="btn sm" data-deny="${id}">Deny</button></div>` : '';
        return `<div class="act${running ? ' running' : ''}" data-act="${id}"><div class="eyebrow">${esc(eyebrow)}</div>`
          + `<h4>${esc(a.card.title || a.tool)}</h4><div class="tgt">${esc(a.card.target || '')}</div>`
          + `<p>${esc(a.card.does || '')}${a.card.not ? ' ' + esc(a.card.not) : ''}</p>${live ? optionsHtml(a) : ''}`
          + `<div class="role">${esc(TIER_LABEL[a.tier] || a.tier)} · ${a.role === 'admin' ? 'admins only' : 'allowed for operators'}</div>${btns}</div>`;
      }
      const ok = a.status === 'done';
      const msg = a.message && a.status !== 'done' && a.message !== a.status ? ` · ${esc(a.message)}` : '';
      return doneTick(ok, esc(a.card.title || a.tool) + msg, esc(ok ? (a.ms != null ? `${a.ms} ms` : '') : a.status));
    }
    function questionHtml(a) {
      const qs = questionList(a), id = esc(a.id), first = (qs[0] && qs[0].question) || '';
      const stale = a.status === 'pending' && a.expires != null && a.expires * 1000 < Date.now();
      if (a.status === 'pending') {
        if (stale) return `<div class="act q" data-act="${id}"><div class="eyebrow">Question expired</div><h4>${esc(first)}</h4></div>`;
        const sel = c.qSel[a.id] || {}, other = c.qOther[a.id] || {}, answers = questionAnswers(a);
        const cur = Math.min(c.qTab[a.id] || 0, qs.length - 1), q = qs[cur];
        const tabs = qs.length > 1 ? '<div class="chips qtabs">' + qs.map((x, i) =>
          `<button type="button" class="chip${i === cur ? ' on' : ''}${answers[i] ? ' done' : ''}" data-qtab="${id}" data-i="${i}">${esc(x.label || `Question ${i + 1}`)}</button>`).join('') + '</div>' : '';
        const rows = (q.choices || []).map(ch =>
          `<button type="button" class="choice${sel[cur] === ch ? ' on' : ''}" data-pick="${id}" data-i="${cur}" data-val="${esc(ch)}" aria-pressed="${sel[cur] === ch}"><i></i>${esc(ch)}</button>`).join('')
          + (sel[cur] === OTHER
            ? `<div class="choice other on"><i></i><input type="text" data-other-input="${id}" data-i="${cur}" maxlength="500" placeholder="Type your answer" aria-label="Your answer" value="${esc(other[cur] || '')}"></div>`
            : `<button type="button" class="choice other" data-pick="${id}" data-i="${cur}" data-val="${OTHER}"><i></i>Other…</button>`);
        const ready = answers.every(Boolean);
        const btns = `<div class="btns"><button type="button" class="btn sm primary" data-submit="${id}"${ready ? '' : ' disabled'}>Submit</button>`
          + `<button type="button" class="btn sm" data-dismiss="${id}">Dismiss</button></div>`;
        return `<div class="act q" data-act="${id}"><div class="eyebrow">Tower asks</div>${tabs}<h4>${esc(q.question)}</h4><div class="choices">${rows}</div>${btns}</div>`;
      }
      const ok = a.status === 'done';
      const more = qs.length > 1 ? ` (+${qs.length - 1} more)` : '';
      return doneTick(ok, esc(first + more), ok ? '' : esc(a.message && a.message !== a.status ? `${a.status} · ${a.message}` : a.status));
    }
    function turnHtml(t, i) {
      if (t.role === 'user') return `<div class="u">${esc(t.text)}</div>`;
      const log = t.ticks.length ? `<div class="log">${t.ticks.map((k, j) => tickHtml(k, `${i}:${j}`)).join('')}</div>` : '';
      const acts = (t.actions || []).map(actionHtml).join('');
      const body = t.text ? `<div class="ans">${answerHtml(t)}</div>` : (t.done ? '' : '<div class="ans"><span class="caret"></span></div>');
      const wait = !t.done && c.state.wait && i === c.state.turns.length - 1 ? `<div class="wait">${esc(TW.waitText(c.state.wait))}</div>` : '';
      const drop = t.truncated ? '<div class="drop">some output was dropped</div>' : '';
      const err = t.error ? `<div class="notice">${esc(t.error)}</div>` : '';
      const note = t.note ? `<div class="tick"><span class="k">·</span>${esc(t.note)}</div>` : '';
      return `<div class="t">${log}${acts}${wait}${body}${drop}${note}${err}</div>`;
    }
    function pendingHtml() {
      return c.pending.map((t, i) => `<div class="u queued"><span class="ql">queued</span>${esc(t)}`
        + `<button type="button" class="qx" data-qx="${i}" aria-label="Remove queued question">✕</button></div>`).join('');
    }
    function convHtml() {
      if (c.view.off) return '<div class="muted-note"><span class="big">Tower is off</span>Turn it on under Settings › Tower in the dashboard.</div>';
      const notice = c.notice ? `<div class="notice">${esc(c.notice)}</div>` : '';
      if (!c.state.turns.length && !c.pending.length) {
        const sugs = TW.suggestions(PAGE, c.view.capabilities).slice(0, 6).map(s => `<button type="button" class="chip" data-sug="${esc(s)}">${esc(s)}</button>`).join('');
        const hint = c.view.noModel ? '<div class="muted-note"><span class="big">No chat model is loaded</span>Load one from the Models screen, then ask.</div>' : '';
        return `${notice}${hint}<div class="chips sugs">${sugs}</div>`;
      }
      return notice + c.state.turns.map(turnHtml).join('') + pendingHtml();
    }
    function insHtml(r) {
      const v = TW.insightView(r, c.view, Date.now() / 1000), id = esc(v.id), open = c.openIns.has(v.id);
      const top = `<div class="top"><span class="rule">${esc(v.rule)}</span>${v.host ? `<span class="host">${esc(v.host)}</span>` : ''}<span class="age">${esc(v.age)}</span></div>`;
      const checks = v.checks.map(k => `<div class="tick${k.ok ? '' : ' bad'}"><span class="k">${k.ok ? '▸' : '✕'}</span>${esc(k.summary || k.name || '')}</div>`).join('');
      const hasDet = !!(v.detail || v.action || v.checks.length || v.auditActor || (v.applied && v.summary));
      const det = hasDet ? `<button type="button" class="lnk" data-ins-det="${id}" aria-expanded="${open}">Details ${open ? '▾' : '▸'}</button>` : '';
      const detb = open ? `<div class="detb">${v.applied && v.summary ? `<p>${esc(v.summary)}</p>` : ''}${v.detail ? `<p>${esc(v.detail)}</p>` : ''}`
        + `${v.action ? `<p><b>Suggested:</b> ${esc(v.action)}</p>` : ''}${checks}${v.auditActor ? `<p class="aud">Audit: ${esc(v.auditActor)}</p>` : ''}</div>` : '';
      const dismiss = `<button type="button" class="lnk" data-ins-dismiss="${id}">Dismiss</button>`;
      const alert = v.alertId ? `<button type="button" class="lnk" data-ins-open="${esc(v.alertId)}">Alert</button>` : '';
      const cls = `ins${v.cls ? ' ' + v.cls : ''}`;
      if (v.applied) return `<div class="${cls}" data-ins="${id}">${top}<div class="acts"><span class="ok">✓ ${esc(v.title)}${v.appliedBy ? ` · ${esc(v.appliedBy)}` : ''}</span>${det}${dismiss}</div>${open ? snapshotHtml(v.snapshot) : ''}${detb}</div>`;
      if (v.running) return `<div class="${cls}" data-ins="${id}">${top}<p class="sum">${esc(v.summary)}</p><div class="acts"><span class="run">Running ${esc(v.title)}…</span></div></div>`;
      const apply = v.applyLabel ? `<button type="button" class="btn sm primary" data-ins-apply="${id}">${esc(v.applyLabel)}</button>` : '';
      const notes = (v.failed ? `<div class="fail">Failed: ${esc(v.failed)}</div>` : '') + (v.adminOnly ? '<div class="fail">Admin only</div>' : '');
      return `<div class="${cls}" data-ins="${id}">${top}<p class="sum">${esc(v.summary)}</p>${snapshotHtml(v.snapshot)}${notes}<div class="acts">${apply}${dismiss}${alert}${det}</div>${detb}</div>`;
    }
    function insListHtml() {
      if (c.view.off) return '';
      const notice = c.insNotice ? `<div class="notice">${esc(c.insNotice)}</div>` : '';
      if (!c.insights.length) return notice + '<div class="muted-note"><span class="big">No insights</span>Tower posts one here when an alert fires and it has something to say.</div>';
      const head = `<div class="inshead"><span>${esc(TW.insightsHeader(c.insights))}</span><button type="button" class="lnk" data-ins-dismiss-all>Dismiss all</button></div>`;
      return notice + head + c.insights.map(insHtml).join('');
    }
    const patch = (el, html) => { if (el && el._html !== html) { el._html = html; el.innerHTML = html; } };
    const nearBottom = (el) => el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    function paintConv(opts) {
      const body = $('towerBody'), scroller = $('towerConv');
      if (!body) return;
      const stick = (opts && opts.toBottom) || (scroller && nearBottom(scroller));
      patch(body, convHtml());
      if (stick && scroller) scroller.scrollTop = scroller.scrollHeight;
      const busy = c.state.status !== 'idle';
      const stop = $('towerStop'), send = $('towerSend'), input = $('towerInput');
      if (stop) stop.hidden = !busy || (c.state.status === 'awaiting' && !c.runId);
      if (send) send.disabled = c.view.off || c.view.noModel;
      if (input) input.disabled = c.view.off || c.view.noModel;
    }
    function paintIns() {
      patch($('towerInsList'), insListHtml());
      const n = $('towerInsCount');
      if (n) { n.textContent = c.insNew ? String(c.insNew) : ''; n.hidden = !c.insNew; }
    }
    function paint(opts) {
      const ins = c.tab === 'ins' && !c.view.off;
      const conv = $('towerConv'), insv = $('towerIns'), ask = $('towerAsk'), chips = $('towerChips');
      if (conv) conv.hidden = ins;
      if (insv) insv.hidden = !ins;
      if (ask) ask.hidden = ins || c.view.off;
      if (chips) chips.querySelectorAll('[data-tview]').forEach(b => b.classList.toggle('on', b.dataset.tview === (ins ? 'ins' : 'ask')));
      ['towerNew', 'towerHistory'].forEach(id => { const b = $(id); if (b) b.hidden = c.view.off; });
      paintConv(opts); paintIns();
    }

    // ── history sheet ───────────────────────────────────────────────────────
    async function history() {
      const { res, d } = await jget('/api/tower/threads').catch(() => ({ res: null, d: {} }));
      const rows = res && res.ok && d.ok ? (d.threads || []) : [];
      const groups = TW.historyGroups(rows, Date.now());
      const html = rows.length ? '<div class="sheetlist">' + groups.map(g => `<div class="sec">${esc(g.label)}</div>` + g.rows.map(t =>
        `<button type="button" class="arow" data-thread="${esc(t.id)}"${c.thread && c.thread.id === t.id ? ' disabled' : ''}><span class="an">${esc(t.title)}</span><span class="pr">${esc(t.time)}</span></button>`).join('')).join('') + '</div>'
        : '<div class="sd">No conversations yet.</div>';
      deps.sheet.open('Conversations', html, (e) => {
        const b = e.target.closest('[data-thread]');
        if (!b || b.disabled) return;
        deps.sheet.close(); openThread(b.dataset.thread);
      });
    }

    // ── events ──────────────────────────────────────────────────────────────
    function onBodyClick(e) {
      const q = (sel) => e.target.closest(sel);
      let b;
      if ((b = q('[data-tk]'))) { const k = b.dataset.tk; c.openTicks.has(k) ? c.openTicks.delete(k) : c.openTicks.add(k); paintConv(); return; }
      if ((b = q('[data-sug]'))) { send(b.dataset.sug); return; }
      if ((b = q('[data-qx]'))) { c.pending.splice(Number(b.dataset.qx), 1); paintConv(); return; }
      if ((b = q('[data-approve]'))) { approve(b.dataset.approve); return; }
      if ((b = q('[data-deny]'))) { decide(b.dataset.deny, 'deny'); return; }
      if ((b = q('[data-submit]'))) { submitQuestion(b.dataset.submit); return; }
      if ((b = q('[data-dismiss]'))) { decide(b.dataset.dismiss, 'deny'); return; }
      if ((b = q('[data-qtab]'))) { c.qTab[b.dataset.qtab] = Number(b.dataset.i); paintConv(); return; }
      if ((b = q('[data-pick]'))) {
        const aid = b.dataset.pick, i = Number(b.dataset.i);
        c.qSel[aid] = { ...(c.qSel[aid] || {}), [i]: b.dataset.val };
        if (b.dataset.val !== OTHER) advanceQuestion(aid);
        paintConv();
        const inp = $('towerBody').querySelector(`[data-other-input="${aid}"]`);
        if (inp && b.dataset.val === OTHER) inp.focus();
        return;
      }
      if ((b = q('[data-opt]'))) { const aid = b.dataset.opt; c.optSel[aid] = { ...(c.optSel[aid] || {}), [b.dataset.name]: b.dataset.val }; paintConv(); }
    }
    function onBodyInput(e) {
      const inp = e.target.closest('[data-other-input]');
      if (!inp) return;
      const aid = inp.dataset.otherInput, i = Number(inp.dataset.i);
      c.qOther[aid] = { ...(c.qOther[aid] || {}), [i]: inp.value };
      const a = actionById(aid), btn = $('towerBody').querySelector(`[data-submit="${aid}"]`);
      if (a && btn) btn.disabled = !questionAnswers(a).every(Boolean);
    }
    function onInsClick(e) {
      const q = (sel) => e.target.closest(sel);
      let b;
      if ((b = q('[data-ins-det]'))) { const k = b.dataset.insDet; c.openIns.has(k) ? c.openIns.delete(k) : c.openIns.add(k); paintIns(); return; }
      if ((b = q('[data-ins-apply]'))) { applyInsight(b.dataset.insApply); return; }
      if ((b = q('[data-ins-dismiss]'))) { dismissInsight(b.dataset.insDismiss); return; }
      if (q('[data-ins-dismiss-all]')) { dismissAll(); return; }
      if ((b = q('[data-ins-open]'))) { if (deps.openAlert) deps.openAlert(b.dataset.insOpen); }
    }
    function submitInput() {
      const input = $('towerInput');
      const t = input.value.trim();
      if (!t) return;
      input.value = ''; input.style.height = '';
      send(t);
    }
    function autosize() {
      const input = $('towerInput');
      input.style.height = '';
      input.style.height = Math.min(120, input.scrollHeight) + 'px';
    }
    function wire() {
      if (c.wired) return;
      c.wired = true;
      $('towerBody').addEventListener('click', onBodyClick);
      $('towerBody').addEventListener('input', onBodyInput);
      $('towerInsList').addEventListener('click', onInsClick);
      $('towerSend').addEventListener('click', submitInput);
      $('towerStop').addEventListener('click', stop);
      $('towerNew').addEventListener('click', async () => { await newThread(); paint(); });
      $('towerHistory').addEventListener('click', history);
      $('towerChips').addEventListener('click', (e) => {
        const b = e.target.closest('[data-tview]');
        if (!b) return;
        c.tab = b.dataset.tview; paint(); markSeen();
      });
      $('towerInput').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitInput(); } });
      $('towerInput').addEventListener('input', autosize);
    }

    // ── controller contract ─────────────────────────────────────────────────
    c.init = async function () { wire(); return loadState(); };
    // Shell interval (15 s) and pull-to-refresh: state, thread (while idle) and insights.
    c.refresh = async function (force) {
      await loadState();
      if (!c.enabled) { paint(); return; }
      if (visible()) { c.unread = 0; badge(); }
      const loads = [loadInsights()];
      if (!c.view.noModel) loads.push(c.thread ? reloadThread() : ensureThread());
      await Promise.all(loads);
      paint(); markSeen();
    };
    c.pollBadge = loadState;
    c.send = send;
    return c;
  }

  const api = { create };
  if (typeof window !== 'undefined') window.CTower = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
