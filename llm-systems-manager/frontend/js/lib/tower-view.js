// Tower drawer view-model (#924): SSE reducer, markdown-lite, thread/page transforms.
// Dual-mode lib: classic <script> global (window.TW) and vitest-importable.
(function (root, factory) {
  const api = factory();
  if (typeof window !== 'undefined') window.TW = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  const PROVIDER = { llama: 'llama.cpp', lms: 'LM Studio', vllm: 'vLLM' };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // Question-card picks: one value per question, a list for a multi-select question (#1045).
  const Q_OTHER = '__other__';
  function qToggle(q, cur, val) {
    if (!(q && q.multi)) return val;
    const list = Array.isArray(cur) ? cur : [];
    return list.includes(val) ? list.filter(x => x !== val) : list.concat([val]);
  }
  function qPicked(q, cur, val) { return q && q.multi ? Array.isArray(cur) && cur.includes(val) : cur === val; }
  function qAnswer(q, cur, other) {
    const typed = String(other || '').trim();
    if (!(q && q.multi)) return cur === Q_OTHER ? typed : (cur == null ? '' : String(cur));
    const list = Array.isArray(cur) ? cur : [];
    const picks = (q.choices || []).filter(c => list.includes(c));
    return list.includes(Q_OTHER) && typed ? picks.concat([typed]) : picks;
  }
  function qAnswered(a) { return Array.isArray(a) ? a.length > 0 : Boolean(a); }

  function initial() { return { model: null, status: 'idle', turns: [], error: null, wait: null }; }

  function last(turns) { return turns[turns.length - 1]; }
  function ensureTower(turns) {
    const t = last(turns);
    if (t && t.role === 'tower' && !t.done) return turns;
    return turns.concat([{ role: 'tower', ticks: [], text: '', done: false, error: null, actions: [] }]);
  }

  // Index of the turn whose actions hold this id, newest first; -1 when none does.
  function findAction(turns, id) {
    for (let i = turns.length - 1; i >= 0; i--) if ((turns[i].actions || []).some(a => a.id === id)) return i;
    return -1;
  }

  function reduce(state, ev) {
    const s = { ...state, turns: state.turns.slice() };
    if (ev.event !== 'status' || ev.state !== 'waiting') s.wait = null;
    switch (ev.event) {
      case 'user': s.turns.push({ role: 'user', text: ev.text }); s.status = 'thinking'; s.error = null; return s;
      case 'model': s.model = { model: ev.model, provider: ev.provider, hosts: ev.hosts || [] }; return s;
      case 'status':
        s.status = ev.state; s.turns = ensureTower(s.turns);
        if (ev.state === 'waiting') s.wait = { name: String(ev.name || ''), elapsed_s: Number(ev.elapsed_s) || 0, timeout_s: Number(ev.timeout_s) || 0 };
        return s;
      case 'tool': {
        s.turns = ensureTower(s.turns);
        const t = { ...last(s.turns), ticks: last(s.turns).ticks.concat([{ name: ev.name, ok: !!ev.ok, ms: ev.ms, summary: ev.summary, result: ev.result }]) };
        s.turns[s.turns.length - 1] = t; return s;
      }
      case 'delta': {
        s.turns = ensureTower(s.turns);
        const t = { ...last(s.turns), text: last(s.turns).text + (ev.text || '') };
        s.turns[s.turns.length - 1] = t; s.status = 'answering'; return s;
      }
      case 'truncated': {
        s.turns = ensureTower(s.turns);
        s.turns[s.turns.length - 1] = { ...last(s.turns), truncated: true }; return s;
      }
      case 'done': {
        const l = last(s.turns);
        // A second done (e.g. after an expired-approval close-out) must not append an empty turn.
        if (l && l.role === 'tower' && !l.done) {
          s.turns[s.turns.length - 1] = { ...l, done: true, note: ev.note || null, elapsed_ms: ev.elapsed_ms };
        }
        s.status = 'idle'; return s;
      }
      case 'error': {
        s.turns = ensureTower(s.turns);
        s.turns[s.turns.length - 1] = { ...last(s.turns), done: true, error: ev.message || 'Tower hit an error.' };
        s.status = 'idle'; s.error = ev.message || null; return s;
      }
      case 'confirm': {
        s.turns = ensureTower(s.turns);
        const a = { id: ev.action_id, tool: ev.tool, args: ev.args || {}, card: ev.card || {}, status: 'pending', tier: ev.tier || 'operate',
                    role: ev.role || 'operator', actor: ev.actor || '', message: null, ms: null,
                    expires: ev.expires_s != null ? Math.floor(Date.now() / 1000) + Number(ev.expires_s) : null };
        s.turns[s.turns.length - 1] = { ...last(s.turns), actions: (last(s.turns).actions || []).concat([a]) };
        s.status = 'awaiting'; return s;
      }
      case 'action': {
        const i = findAction(s.turns, ev.action_id);
        if (i < 0) { s.turns = ensureTower(s.turns); s.status = 'thinking'; return s; }
        const t = s.turns[i];
        const actions = t.actions.map(a => a.id === ev.action_id
          ? { ...a, status: ev.status || a.status, message: ev.message == null ? a.message : ev.message, ms: ev.ms == null ? a.ms : ev.ms, actor: ev.actor || a.actor }
          : a);
        s.turns[i] = { ...t, actions };
        s.status = 'thinking'; return s;
      }
      // A question card parks the turn like an approval; the answer becomes the next user turn (#1028).
      case 'question': {
        s.turns = ensureTower(s.turns);
        const qs = (ev.questions || []).map(q => ({ question: String(q.question || ''), choices: (q.choices || []).map(String), label: String(q.label || ''), ...(q.multi ? { multi: true } : {}) }));
        const a = { id: ev.action_id, tool: ev.tool || 'ask_operator', args: {}, card: { question: ev.question || '', choices: (ev.choices || []).map(String), questions: qs },
                    status: 'pending', tier: 'read', role: 'operator', actor: ev.actor || '', message: null, ms: null, answer: null,
                    expires: ev.expires_s != null ? Math.floor(Date.now() / 1000) + Number(ev.expires_s) : null };
        s.turns[s.turns.length - 1] = { ...last(s.turns), actions: (last(s.turns).actions || []).concat([a]) };
        s.status = 'awaiting'; return s;
      }
      case 'answer': {
        const i = findAction(s.turns, ev.action_id);
        const answered = ev.status === 'answered';
        const was = i >= 0 ? s.turns[i].actions.find(a => a.id === ev.action_id).status : 'pending';
        if (was !== 'pending') { s.turns = ensureTower(s.turns); s.status = 'thinking'; return s; }
        if (i >= 0) {
          const t = s.turns[i];
          const actions = t.actions.map(a => a.id === ev.action_id
            ? { ...a, status: answered ? 'done' : (ev.status || a.status), answer: answered ? String(ev.answer || '') : a.answer,
                message: ev.message == null ? a.message : ev.message, actor: ev.actor || a.actor }
            : a);
          s.turns[i] = { ...t, actions, done: answered ? true : t.done };
        }
        if (answered) s.turns.push({ role: 'user', text: String(ev.answer || '') });
        s.turns = ensureTower(s.turns);
        s.status = 'thinking'; return s;
      }
      default: return s;
    }
  }

  const URL_RE = /https?:\/\/[^\s<>"'`)\]]+[^\s<>"'`)\].,;:!?]/g;
  const MAIL_RE = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g;
  const LINK_RE = new RegExp(URL_RE.source + '|' + MAIL_RE.source, 'g');
  // Escaped text with `code`, **bold** and plain http(s) URLs / e-mail addresses as links (outside code spans).
  function inline(t) {
    return esc(t).split('`').map((seg, i) => i % 2
      ? `<code>${seg}</code>`
      : seg.replace(LINK_RE, m => m.startsWith('http') ? `<a href="${m}" target="_blank" rel="noopener">${m}</a>` : `<a href="mailto:${m}">${m}</a>`)
           .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')).join('');
  }
  function md(text) {
    const out = [];
    const lines = String(text || '').replace(/\r/g, '').split('\n');
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) { i++; continue; }
      if (/^\s*```/.test(l)) {
        const code = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i]), i++;
        i++;
        out.push('<pre>' + esc(code.join('\n')) + '</pre>'); continue;
      }
      if (/^\s*[-*]\s+/.test(l)) {
        const items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push('<li>' + inline(lines[i].replace(/^\s*[-*]\s+/, '')) + '</li>'), i++;
        out.push('<ul>' + items.join('') + '</ul>'); continue;
      }
      if (OL_LINE.test(l)) {
        const items = [];
        const start = Number((l.match(OL_LINE) || [])[1]) || 1;
        while (i < lines.length && OL_LINE.test(lines[i])) items.push('<li>' + inline(lines[i].replace(OL_LINE, '')) + '</li>'), i++;
        out.push(`<ol${start > 1 ? ` start="${start}"` : ''}>` + items.join('') + '</ol>'); continue;
      }
      if (/^\s*\|/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
        const head = cells(l); i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(cells(lines[i])), i++;
        out.push('<table><tr>' + head.map(h => '<th>' + inline(h) + '</th>').join('') + '</tr>'
          + rows.map(r => '<tr>' + r.map(c => '<td>' + inline(c) + '</td>').join('') + '</tr>').join('') + '</table>');
        continue;
      }
      const para = [lines[i]];
      i++;
      while (i < lines.length && lines[i].trim() && !/^\s*[-*]\s+/.test(lines[i]) && !OL_LINE.test(lines[i]) && !/^\s*\|/.test(lines[i]) && !/^\s*```/.test(lines[i])) para.push(lines[i]), i++;
      out.push(enumHtml(para.join(' ')));
    }
    return out.join('');
  }
  const OL_LINE = /^\s*(\d{1,3})[.)]\s+/;
  // A paragraph that carries an inline "1. … 2. … 3. …" enumeration becomes a lead sentence plus an ordered list (#995).
  function enumHtml(text) {
    const re = /(^|\s)(\d{1,2})[.)]\s+(?=\S)/g;
    const hits = [];
    let m;
    while ((m = re.exec(text))) hits.push({ n: Number(m[2]), at: m.index + m[1].length, end: m.index + m[0].length });
    const seq = [];
    for (const h of hits) { if (h.n === seq.length + 1) seq.push(h); else if (h.n === 1) seq.splice(0, seq.length, h); }
    if (seq.length < 2) return '<p>' + inline(text) + '</p>';
    const lead = text.slice(0, seq[0].at).trim();
    const items = seq.map((h, k) => text.slice(h.end, k + 1 < seq.length ? seq[k + 1].at : text.length).trim());
    return (lead ? '<p>' + inline(lead) + '</p>' : '') + '<ol>' + items.map(t => '<li>' + inline(t) + '</li>').join('') + '</ol>';
  }

  function tickSummary(r) {
    const label = String(r.tool_name || '').replace(/_/g, ' ');
    let a = {};
    try { a = JSON.parse(r.tool_args || '{}') || {}; } catch (_) { /* no args */ }
    if (r.tool_name === 'timer') {
      const b = safeJson(r.content) || {};
      const n = Number(b.ticks) || 0;
      return `timer · ${a.label || b.label || ''} · ${n} tick${n === 1 ? '' : 's'}` + (b.ok ? '' : ` · ${b.message || b.status || 'failed'}`);
    }
    const verb = r.tool_name === 'schedule' ? 'scheduled' : 'read';
    const tgt = r.tool_name === 'schedule' ? (a.label || '') : (a.host || a.model || a.alert_id || a.path || a.window || '');
    return verb + ' ' + label + (tgt ? ' · ' + tgt : '') + ' · ' + (r.tool_ms || 0) + ' ms';
  }
  // Folds a tool round's optional assistant preamble + tool rows + final
  // assistant answer into one tower turn; a new turn starts only at 'user'.
  function threadView(rows) {
    const turns = [];
    let cur = null;
    function flush() {
      if (cur) { turns.push(cur); cur = null; }
    }
    (rows || []).forEach(r => {
      if (r.role === 'user') { flush(); turns.push({ role: 'user', text: r.content || '' }); }
      else if (r.role === 'tool') {
        if (!cur) cur = { role: 'tower', ticks: [], text: '', done: true, error: null, actions: [] };
        cur.ticks.push({ name: r.tool_name, ok: !!r.tool_ok, ms: r.tool_ms, summary: tickSummary(r), result: safeJson(r.content) });
      } else if (r.role === 'assistant') {
        if (!cur) cur = { role: 'tower', ticks: [], text: '', done: true, error: null, actions: [] };
        cur.text = [cur.text, r.content || ''].filter(Boolean).join('\n');
      } else if (r.role === 'action') {
        if (!cur) cur = { role: 'tower', ticks: [], text: '', done: true, error: null, actions: [] };
        const b = safeJson(r.content) || {};
        cur.actions.push({ id: b.action_id, runId: b.run_id || null, tool: b.tool || r.tool_name, args: b.args || {}, card: b.card || {}, status: b.status || 'pending',
                           tier: b.tier || 'operate', role: b.role || 'operator', actor: b.actor || '', message: b.message == null ? null : b.message,
                           answer: b.answer == null ? null : String(b.answer),
                           ms: r.tool_ms == null ? null : r.tool_ms, expires: b.expires == null ? null : b.expires });
      }
    });
    flush();
    return turns;
  }
  function safeJson(s) { try { return JSON.parse(s); } catch (_) { return s; } }

  // The run a reloaded thread is still parked on: {runId, status} of the last turn's live
  // pending/running action (not past its expiry), else null (#956).
  function liveRun(turns, nowS) {
    const t = turns[turns.length - 1];
    if (!t || t.role !== 'tower') return null;
    const now = nowS != null ? nowS : Date.now() / 1000;
    const a = (t.actions || []).slice().reverse().find(x => x.runId && (x.status === 'running' || (x.status === 'pending' && (x.expires == null || x.expires > now))));
    return a ? { runId: a.runId, status: a.status } : null;
  }

  const SUGS = {
    events: ['Alarm summary for today', 'Summarise active alarms', 'Which alert needs me first?', 'Top 10 alarm rules over 30 days', 'Alarms per day this week as a chart',
             'Which host alarms the most?', 'What changed in the last hour?'],
    energy: ['What used the most power today?', 'What is my $/Mtok today?', 'Energy per host this week', 'Idle vs active energy today',
             'Which host is idle the most?'],
    llm: ['Which model is fastest here?', 'What models are loaded everywhere?', 'Is anything loaded that nobody uses?',
          'What CPU and GPU does each host have?', 'Explain slot pressure'],
    admin: ['Is the alarm engine healthy?', 'What version is each agent on?', 'Are any hosts offline?', 'Summarise active alarms'],
    tools: ['What was the last benchmark run?', 'Which host is fastest for a 27B model?', 'Any autotune runs this week?'],
  };
  const ACT_SUGS = { llm: 'Wake llama-server', events: 'Acknowledge the oldest active alert' };
  const ACTIVITY_SUG = 'Activity summary for today';
  const HELP_SUGS = ['Who develops LLM Systems Manager?', 'How do I get help?'];
  function suggestions(page, caps) {
    const p = page || {};
    let list;
    if (p.tab === 'events') list = SUGS.events;
    else if (p.tab === 'dashboard' && p.sub === 'energy') list = SUGS.energy;
    else if (p.tab === 'llm') list = SUGS.llm;
    else if (p.tab === 'admin') list = SUGS.admin;
    else if (p.tab === 'tools') list = SUGS.tools;
    else {
      const host = p.host ? `Why is ${p.host} red?` : 'Why is a host red?';
      const hw = p.host ? `What hardware does ${p.host} have?` : 'What hardware does each host have?';
      list = ['Alarm summary for today', host, 'What used the most power today?',
              'What models are loaded everywhere?', 'Top alarm offenders this month', hw].concat(HELP_SUGS);
    }
    list = p.tab === 'events' ? [list[0], ACTIVITY_SUG].concat(list.slice(1)) : [ACTIVITY_SUG].concat(list);
    if (caps === 'operate' || caps === 'admin') list = list.concat([ACT_SUGS[p.tab] || 'Unload a model nobody is using']);
    return list;
  }

  // One line for the drawer while a tool waits on a slow operation.
  function waitText(w) {
    if (!w) return '';
    return `waiting for ${w.name || 'the result'} · ${w.elapsed_s} s` + (w.timeout_s ? ` of ${w.timeout_s}` : '');
  }

  function pageContext(o) {
    const p = {};
    if (o && o.tab) p.tab = String(o.tab);
    if (o && o.sub) p.sub = String(o.sub);
    if (o && o.host) p.host = String(o.host);
    if (o && Array.isArray(o.cards) && o.cards.length) p.cards = o.cards.slice(0, 24).map(String);
    if (o && o.alertId) p.alert_id = String(o.alertId);
    return p;
  }

  function ageText(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    if (s < 60) return 'now';
    if (s < 3600) return Math.floor(s / 60) + ' min';
    if (s < 86400) return Math.floor(s / 3600) + ' h';
    return Math.floor(s / 86400) + ' d';
  }
  function visibleInsights(rows) { return (rows || []).filter(r => r && r.status !== 'dismissed'); }
  function insightsHeader(rows) {
    const n = (rows || []).filter(r => r.status === 'new').length, a = (rows || []).filter(r => r.status === 'applied').length;
    const parts = [];
    if (n) parts.push(n + ' new');
    if (a) parts.push(a + ' applied');
    return parts.join(' · ') || String((rows || []).length);
  }
  // One insight card's render model; `view` is stateView() (tier + admin flag).
  function insightView(row, view, nowS) {
    const r = row || {}, v = view || {};
    const applied = r.status === 'applied', running = r.status === 'applying', open = r.status === 'new' || r.status === 'seen';
    const safe = !!r.playbook_safe, caps = v.capabilities || 'read';
    const tierOk = safe ? (caps === 'operate' || caps === 'admin') : (caps === 'admin' && !!v.admin);
    const title = r.playbook_title || r.playbook_id || '';
    const res = r.result || null;
    const at = Number((applied && r.resolved) || r.created || 0);
    return {
      id: r.id, alertId: /^timer:/.test(String(r.alert_id || '')) ? '' : (r.alert_id || ''), status: r.status, open, applied, running,
      cls: applied ? 'done' : (String(r.severity || '').toLowerCase() === 'critical' ? 'crit' : ''),
      rule: r.rule || 'Alert', host: r.host || '',
      age: at ? ageText((nowS != null ? nowS : Date.now() / 1000) - at) : '',
      summary: r.summary || '', detail: r.detail || '', action: r.suggested_action || '',
      checks: Array.isArray(r.checks) ? r.checks : [],
      title, applyLabel: open && !!r.playbook_id && tierOk ? title : null,
      adminOnly: open && !!r.playbook_id && !safe && !tierOk,
      appliedLine: applied ? '✓ ' + title : null,
      appliedBy: applied ? (/^tower via alarm /.test(r.applied_by || '') ? 'auto' : String(r.applied_by || '').replace(/^tower via /, '')) : '',
      auditActor: r.applied_by || '',
      failed: !applied && res && res.ok === false ? String(res.message || 'failed') : null,
      snapshot: r.snapshot && Array.isArray(r.snapshot.points) ? r.snapshot : null,
    };
  }

  // SVG geometry for an insight's metric snapshot: the series path, the threshold line and a caption (#980).
  function sparkline(snap, w, h) {
    w = w || 240; h = h || 36;
    const pts = (snap && Array.isArray(snap.points) ? snap.points : []).filter(p => Array.isArray(p) && typeof p[0] === 'number' && typeof p[1] === 'number' && Number.isFinite(p[0]) && Number.isFinite(p[1]));
    if (pts.length < 2) return null;
    const xs = pts.map(p => Number(p[0])), ys = pts.map(p => Number(p[1]));
    const thr = snap.threshold != null && Number.isFinite(Number(snap.threshold)) ? Number(snap.threshold) : null;
    let lo = Math.min(...ys), hi = Math.max(...ys);
    if (thr !== null) { lo = Math.min(lo, thr); hi = Math.max(hi, thr); }
    if (hi === lo) hi = lo + 1;
    const pad = 2, x0 = Math.min(...xs), span = (Math.max(...xs) - x0) || 1;
    const X = t => pad + (t - x0) / span * (w - 2 * pad);
    const Y = v => h - pad - (v - lo) / (hi - lo) * (h - 2 * pad);
    const d = pts.map((p, i) => (i ? 'L' : 'M') + X(Number(p[0])).toFixed(1) + ' ' + Y(Number(p[1])).toFixed(1)).join(' ');
    const unit = String(snap.unit || '');
    const fmt = v => (Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + (unit === '%' || !unit ? unit : ' ' + unit);
    const caption = `${snap.metric || 'metric'} · last ${Number(snap.minutes) || 60} min · ${fmt(Math.min(...ys))}–${fmt(Math.max(...ys))}`
      + (thr !== null ? ` · threshold ${fmt(thr)}` : '');
    return { d, w, h, thrY: thr !== null ? Number(Y(thr).toFixed(1)) : null, caption };
  }

  // The first message and the thread title of a conversation started from an insight (#980).
  function troubleshootTitle(r) {
    return `Troubleshoot: ${(r && r.rule) || 'Alert'}${r && r.host ? ' · ' + r.host : ''}`;
  }
  function troubleshootPrompt(r) {
    const x = r || {};
    const prior = x.summary && !/^Not diagnosed/.test(x.summary) ? ` Tower's earlier read: ${x.summary}` : '';
    return `Troubleshoot the alert "${x.rule || 'Alert'}"${x.host ? ` on ${x.host}` : ''} (alert id ${x.alert_id || '?'}). `
      + 'Read the alert and the host\'s current state, explain the likely cause, and walk me through fixing it step by step.' + prior;
  }

  // History rows grouped by the day of their last message, newest first; rows without a stamp end up under "Undated" (#987).
  function historyGroups(threads, nowMs, discord) {
    const now = new Date(nowMs != null ? nowMs : Date.now());
    const dayKey = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const today = dayKey(now), yest = dayKey(new Date(now.getTime() - 86400000));
    const label = d => {
      const k = dayKey(d);
      if (k === today) return 'Today';
      if (k === yest) return 'Yesterday';
      const opts = { weekday: 'short', month: 'short', day: 'numeric' };
      if (d.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
      return d.toLocaleDateString(undefined, opts);
    };
    const rows = (threads || []).map(t => {
      const at = Number(t.updated || t.created || 0) * 1000;
      const d = at ? new Date(at) : null;
      return { id: t.id, title: t.title || 'New thread', at, key: d ? dayKey(d) : '', day: d ? label(d) : 'Undated',
               time: d ? d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : '' };
    }).sort((a, b) => b.at - a.at);
    const groups = [];
    for (const r of rows) {
      const g = groups[groups.length - 1];
      if (g && g.key === r.key) g.rows.push(r); else groups.push({ key: r.key, label: r.day, rows: [r] });
    }
    // Discord conversations (admins only) follow the dated groups, one group per Discord user (#996).
    const byUser = new Map();
    for (const t of discord || []) {
      const at = Number(t.updated || t.created || 0) * 1000, d = at ? new Date(at) : null;
      const uid = String(t.user || '').replace(/^discord:/, '') || '?';
      const row = { id: t.id, title: t.title || 'New thread', at, key: 'discord:' + uid, day: 'Discord · ' + uid,
                    time: d ? d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '' };
      if (!byUser.has(uid)) byUser.set(uid, []);
      byUser.get(uid).push(row);
    }
    for (const [uid, list] of byUser) groups.push({ key: 'discord:' + uid, label: 'Discord · ' + uid, rows: list.sort((a, b) => b.at - a.at) });
    return groups;
  }

  // Timers (#1029): the strip line for one live timer, given the seconds since its view was fetched.
  function timerLine(t, elapsedS) {
    if (!t) return '';
    const e = Math.max(0, Number(elapsedS) || 0);
    const parts = [`${Number(t.count) || 0}/${Number(t.times) || 0}`];
    if (t.status === 'reporting') parts.push('reporting…');
    else {
      if (t.next_in_s != null) parts.push(`next in ${Math.max(0, Math.round(Number(t.next_in_s) - e))} s`);
      if (t.left_s != null) { const left = Math.max(0, Math.round(Number(t.left_s) - e)); parts.push(left >= 90 ? `${Math.round(left / 60)} min left` : `${left} s left`); }
    }
    return parts.join(' · ');
  }
  function liveTimers(list) { return (list || []).filter(t => t && (t.status === 'queued' || t.status === 'running' || t.status === 'reporting')); }
  // Timers that were live in `prev` and now carry a report run: the drawer attaches to those runs.
  function finishedTimers(prev, next) {
    const was = new Set(liveTimers(prev).map(t => t.id));
    return (next || []).filter(t => t && t.status === 'done' && t.run_id && was.has(t.id));
  }
  // host_history charts (#1043): one per host carrying `_chart`; [] when the result has none.
  function historyCharts(result) {
    const r = result && typeof result === 'object' ? result : null;
    if (!r) return [];
    const rows = Array.isArray(r.hosts) ? r.hosts : [r];
    return rows.map(x => x && x._chart).filter(c => c && Array.isArray(c.points) && c.points.length >= 2);
  }
  // Geometry for a full-width history chart: the path, the peak (as % of the box) and axis labels.
  function historyChart(chart, w, h) {
    const sp = sparkline(chart, w || 240, h || 60);
    if (!sp) return null;
    const pts = chart.points.filter(p => Array.isArray(p) && Number.isFinite(Number(p[0])) && Number.isFinite(Number(p[1])));
    const ys = pts.map(p => Number(p[1])), xs = pts.map(p => Number(p[0]));
    const hi = Math.max(...ys), lo = Math.min(...ys), x0 = Math.min(...xs), span = (Math.max(...xs) - x0) || 1;
    const peakI = ys.indexOf(hi);
    const unit = String(chart.unit || '');
    const fmt = v => (Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + (unit === '%' || !unit ? unit : ' ' + unit);
    const long = span >= 36 * 3600;
    const when = t => {
      const d = new Date(t * 1000);
      return long ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
                  : d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    };
    const peakPct = { x: (xs[peakI] - x0) / span * 100, y: hi === lo ? 50 : 0 };
    const label = (chart.metric || 'metric') + (chart.host ? ' · ' + chart.host : '');
    return { d: sp.d, w: sp.w, h: sp.h, hi: fmt(hi), lo: fmt(lo), first: when(xs[0]), last: when(xs[xs.length - 1]),
             peakPct, peak: `peak ${fmt(hi)} at ${when(xs[peakI])}`, label,
             caption: `${label} · ${fmt(ys[0])} → ${fmt(ys[ys.length - 1])} · ${fmt(lo)}–${fmt(hi)}` };
  }

  // A timer tool row's numeric series as a sparkline snapshot; null when there is nothing to draw.
  function timerSnapshot(result) {
    const r = result && typeof result === 'object' ? result : null;
    if (!r || !Array.isArray(r.series) || r.series.length < 2) return null;
    const first = Number(r.series[0][0]), last = Number(r.series[r.series.length - 1][0]);
    return { points: r.series, unit: r.unit || '', metric: r.metric || r.pick || r.label || 'value',
             minutes: Math.max(1, Math.round((last - first) / 60)) };
  }

  // Model capability check (#1039): the drawer/settings chip for one check row.
  const SMALL_TIP = 'Small model: expect the occasional tool call written as text; Tower corrects it once per question';
  function checkChips(c) {
    if (!c || !c.grade) return [];
    const out = [];
    if (c.grade === 'pending') out.push({ text: 'Checking\u2026', short: 'Checking\u2026', cls: 'dim', title: 'Checking whether the model can call Tower\u2019s tools' });
    else if (c.grade === 'native') out.push({ text: 'Tools OK', short: 'Tools', cls: 'ok', title: 'Tool calls work: the model uses built-in function calling' });
    else if (c.grade === 'fenced') out.push({ text: 'Tools OK', short: 'Tools', cls: 'ok outline', title: 'Tool calls work: the model writes them in text prompt mode' });
    else if (c.grade === 'unknown') out.push({ text: 'Not checked', short: 'Not checked', cls: 'dim', title: `The model did not answer the check (${c.detail || 'unreachable'}); it runs again automatically` });
    else out.push({ text: 'No tool support', short: 'No tools', cls: 'crit', title: `No tool support: the model made no tool call in either mode (${c.detail || 'no call'})` });
    if (c.small) out.push({ text: 'small model', short: 'small', cls: 'warn', title: `${SMALL_TIP} (${c.size_b}B)` });
    return out;
  }

  // Conversation eval (#1047): one stored result as a chip + summary line; one live job as a progress line.
  function evalSummary(r, nowS) {
    if (!r || r.total == null) return null;
    const pct = Number(r.score_pct || 0);
    const cls = r.total === 0 ? 'dim' : pct >= 90 ? 'ok' : pct >= 60 ? 'warn' : 'crit';
    const bits = [];
    if (r.calls_per_case != null) bits.push(`${Number(r.calls_per_case).toFixed(1)} calls per question`);
    if (r.corrections) bits.push(`${r.corrections} corrected`);
    if (r.retries) bits.push(`${r.retries} ${r.retries === 1 ? 'retry' : 'retries'}`);
    bits.push(`${Math.max(1, Math.round(Number(r.ms || 0) / 1000))} s`);
    const meta = [r.quant, r.server].filter(Boolean).join(' · ');
    const when = r.at && nowS ? ageText(nowS - Number(r.at)) : '';
    const short = String(r.model || '').split('/').pop();
    return { text: `${r.passed}/${r.total}`, cls, line: bits.join(' · '), meta, when, short,
             title: [`${r.passed}/${r.total} passed`, r.model, meta, when ? (when === 'now' ? 'just now' : when + ' ago') : ''].filter(Boolean).join(' · ') };
  }
  function evalProgress(job) {
    if (!job) return '';
    const st = job.state || {};
    const p = st.phase;
    if (job.status === 'queued' && !p) return 'Queued…';
    if (p === 'download') return `Downloading${st.pct != null ? ' · ' + st.pct + ' %' : ''}…`;
    if (p === 'config') return 'Adding it to the host…';
    if (p === 'restart') return `Restarting llama.cpp${st.waited_s ? ' · ' + st.waited_s + ' s' : ''}…`;
    if (p === 'load') return `Loading${st.waited_s ? ' · ' + st.waited_s + ' s' : ''}…`;
    if (p === 'check') return 'Checking tool calls…';
    if (p === 'eval') return `Question ${st.case}/${st.total} · ${st.title || ''}` + (st.passed ? ` · ${st.passed} passed so far` : '');
    return 'Starting…';
  }

  function stateView(api) {
    const a = api || {};
    const off = !a.enabled;
    const noModel = !off && !a.model;
    return { enabled: !!a.enabled, admin: !!a.admin, off, noModel,
             capabilities: a.capabilities || 'read', offTopic: a.off_topic || 'refuse',
             check: a.check || null, fallback: a.fallback || null, fallbackEnabled: !!a.fallback_enabled,
             chip: a.model ? { model: a.model, provider: PROVIDER[a.provider] || a.provider || '', host: (a.hosts || [])[0] || '' } : null };
  }

  return { Q_OTHER, qToggle, qPicked, qAnswer, qAnswered, initial, reduce, md, threadView, liveRun, historyGroups, suggestions, pageContext, stateView, checkChips, evalSummary, evalProgress, esc, PROVIDER, waitText, HELP_SUGS,
           ageText, insightView, insightsHeader, visibleInsights, sparkline, troubleshootTitle, troubleshootPrompt,
           timerLine, liveTimers, finishedTimers, timerSnapshot, historyCharts, historyChart };
});
