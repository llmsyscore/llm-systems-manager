// Tower drawer view-model (#924): SSE reducer, markdown-lite, thread/page transforms.
// Dual-mode lib: classic <script> global (window.TW) and vitest-importable.
(function (root, factory) {
  const api = factory();
  if (typeof window !== 'undefined') window.TW = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  const PROVIDER = { llama: 'llama.cpp', lms: 'LM Studio', vllm: 'vLLM' };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function initial() { return { model: null, status: 'idle', turns: [], error: null }; }

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
    switch (ev.event) {
      case 'user': s.turns.push({ role: 'user', text: ev.text }); s.status = 'thinking'; s.error = null; return s;
      case 'model': s.model = { model: ev.model, provider: ev.provider, hosts: ev.hosts || [] }; return s;
      case 'status': s.status = ev.state; s.turns = ensureTower(s.turns); return s;
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
      default: return s;
    }
  }

  function inline(t) {
    return esc(t).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
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
      while (i < lines.length && lines[i].trim() && !/^\s*[-*]\s+/.test(lines[i]) && !/^\s*\|/.test(lines[i]) && !/^\s*```/.test(lines[i])) para.push(lines[i]), i++;
      out.push('<p>' + inline(para.join(' ')) + '</p>');
    }
    return out.join('');
  }

  function tickSummary(r) {
    const verb = 'read', label = String(r.tool_name || '').replace(/_/g, ' ');
    let tgt = '';
    try { const a = JSON.parse(r.tool_args || '{}'); tgt = a.host || a.model || a.alert_id || a.path || a.window || ''; } catch (_) { /* no args */ }
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
        cur.actions.push({ id: b.action_id, tool: b.tool || r.tool_name, args: b.args || {}, card: b.card || {}, status: b.status || 'pending',
                           tier: b.tier || 'operate', role: b.role || 'operator', actor: b.actor || '', message: b.message == null ? null : b.message,
                           ms: r.tool_ms == null ? null : r.tool_ms, expires: b.expires == null ? null : b.expires });
      }
    });
    flush();
    return turns;
  }
  function safeJson(s) { try { return JSON.parse(s); } catch (_) { return s; } }

  const SUGS = {
    events: ['Summarise active alarms', 'Which alert needs me first?', 'Top 10 alarm rules over 30 days', 'Alarms per day this week as a chart',
             'Which host alarms the most?', 'What changed in the last hour?'],
    energy: ['What used the most power today?', 'What is my $/Mtok today?', 'Energy per host this week', 'Idle vs active energy today',
             'Which host is idle the most?'],
    llm: ['Which model is fastest here?', 'What models are loaded everywhere?', 'Is anything loaded that nobody uses?',
          'What CPU and GPU does each host have?', 'Explain slot pressure'],
    admin: ['Is the alarm engine healthy?', 'What version is each agent on?', 'Are any hosts offline?', 'Summarise active alarms'],
    tools: ['What was the last benchmark run?', 'Which host is fastest for a 27B model?', 'Any autotune runs this week?'],
  };
  const ACT_SUGS = { llm: 'Wake llama-server', events: 'Acknowledge the oldest active alert' };
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
      list = [host, 'Summarise active alarms', 'What used the most power today?', 'What models are loaded everywhere?',
              'Top alarm offenders this month', hw];
    }
    if (caps === 'operate' || caps === 'admin') list = list.concat([ACT_SUGS[p.tab] || 'Unload a model nobody is using']);
    return list;
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

  function stateView(api) {
    const a = api || {};
    const off = !a.enabled;
    const noModel = !off && !a.model;
    return { enabled: !!a.enabled, admin: !!a.admin, off, noModel,
             capabilities: a.capabilities || 'read', offTopic: a.off_topic || 'refuse',
             chip: a.model ? { model: a.model, provider: PROVIDER[a.provider] || a.provider || '', host: (a.hosts || [])[0] || '' } : null };
  }

  return { initial, reduce, md, threadView, suggestions, pageContext, stateView, esc, PROVIDER };
});
