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
    return turns.concat([{ role: 'tower', ticks: [], text: '', done: false, error: null }]);
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
        s.turns = ensureTower(s.turns);
        s.turns[s.turns.length - 1] = { ...last(s.turns), done: true, note: ev.note || null, elapsed_ms: ev.elapsed_ms };
        s.status = 'idle'; return s;
      }
      case 'error': {
        s.turns = ensureTower(s.turns);
        s.turns[s.turns.length - 1] = { ...last(s.turns), done: true, error: ev.message || 'Tower hit an error.' };
        s.status = 'idle'; s.error = ev.message || null; return s;
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
        if (!cur) cur = { role: 'tower', ticks: [], text: '', done: true, error: null };
        cur.ticks.push({ name: r.tool_name, ok: !!r.tool_ok, ms: r.tool_ms, summary: tickSummary(r), result: safeJson(r.content) });
      } else if (r.role === 'assistant') {
        if (!cur) cur = { role: 'tower', ticks: [], text: '', done: true, error: null };
        cur.text = [cur.text, r.content || ''].filter(Boolean).join('\n');
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
  function suggestions(page) {
    const p = page || {};
    if (p.tab === 'events') return SUGS.events;
    if (p.tab === 'dashboard' && p.sub === 'energy') return SUGS.energy;
    if (p.tab === 'llm') return SUGS.llm;
    if (p.tab === 'admin') return SUGS.admin;
    if (p.tab === 'tools') return SUGS.tools;
    const host = p.host ? `Why is ${p.host} red?` : 'Why is a host red?';
    const hw = p.host ? `What hardware does ${p.host} have?` : 'What hardware does each host have?';
    return [host, 'Summarise active alarms', 'What used the most power today?', 'What models are loaded everywhere?',
            'Top alarm offenders this month', hw];
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
