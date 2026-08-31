// Tools launcher rendering (#769): card/list/compact views + run ledger rows.
// Dual-mode lib: classic <script> global (window.TC) and vitest-importable.
(function () {
  const MC = (typeof window !== 'undefined' && window.MC)
    || (typeof require === 'function' ? require('./modelcards.js') : null);

  const VIEWS = ['card', 'list', 'compact'];
  const esc = MC.esc;

  function validView(v) { return VIEWS.includes(v) ? v : 'card'; }

  function viewOf(layoutObj) {
    return validView(layoutObj && layoutObj.toolsView);
  }

  // Epoch seconds, epoch millis, or ISO string → millis; null when unparsable.
  function toMs(ts) {
    if (ts == null || ts === '') return null;
    if (typeof ts === 'number') return ts < 1e12 ? ts * 1000 : ts;
    const t = Date.parse(ts);
    return isNaN(t) ? null : t;
  }

  // Short human age via the shared MC ladder; null when unparsable.
  function age(ts, nowMs) {
    const ms = toMs(ts);
    return ms == null ? null : MC.age(new Date(ms).toISOString(), nowMs);
  }

  // Relative age up to 15 days, then the calendar date; null when unparsable.
  function when(ts, nowMs) {
    const ms = toMs(ts);
    if (ms == null) return null;
    const now = nowMs != null ? nowMs : Date.now();
    if (now - ms <= 15 * 86400e3) return MC.age(new Date(ms).toISOString(), now);
    const d = new Date(ms), n = new Date(now);
    const opts = { month: 'short', day: 'numeric' };
    if (d.getFullYear() !== n.getFullYear()) opts.year = 'numeric';
    return d.toLocaleDateString('en-US', opts);
  }

  const _PILL = { ready: 'p-idle', running: 'p-busy', soon: 'p-unloaded' };

  function pill(t) {
    const cls = _PILL[t.status] || 'p-unloaded';
    const label = t.statusLabel || (t.status === 'running' ? 'Running'
      : t.status === 'soon' ? 'Planned' : 'Ready');
    return `<span class="mc-pill ${cls}">${esc(label)}</span>`;
  }

  function statsHtml(t) {
    if (!t.stats || !t.stats.length) {
      return `<div class="tc-empty">${t.empty || ''}</div>`;
    }
    return '<div class="tc-stats">' + t.stats.map(s =>
      `<div class="tc-stat"><div class="v">${esc(s.v)}` +
      (s.u ? `<em>${esc(s.u)}</em>` : '') +
      `</div><div class="l">${esc(s.l)}</div></div>`).join('') + '</div>';
  }

  function card(t) {
    const soon = t.status === 'soon';
    const tag = soon ? 'div' : 'button';
    const attrs = soon ? '' : ` data-tool="${esc(t.id)}" type="button"`;
    return `<${tag} class="tool-card${soon ? ' soon' : ''}"${attrs}>
      <div class="tc-top">
        <div class="tc-ico tone-${t.tone || 1}">${esc(t.icon)}</div>
        <div class="tc-title">
          <div class="tc-name">${esc(t.name)}</div>
          <div class="tc-desc">${esc(t.desc)}</div>
        </div>
        ${pill(t)}
      </div>
      <div class="tc-body">${statsHtml(t)}</div>
      <div class="tc-foot"${soon ? ' style="border-top:0;"' : ''}>
        <span class="tc-last">${t.last || ''}</span>
        <span class="tc-gap"></span>
        ${soon ? '' : `<span class="mcbtn ${t.primary ? 'mcbtn-pri' : 'mcbtn-ghost'} mcbtn-sm">${esc(t.action || 'Open')}</span>`}
      </div>
    </${tag}>`;
  }

  function listHeader() {
    return `<div class="tl-row hdr"><span></span><span>Tool</span><span>Stats</span>` +
      `<span>Last run</span><span>Status</span><span></span></div>`;
  }

  function row(t) {
    const soon = t.status === 'soon';
    const dot = t.status === 'running' ? 'd-busy' : soon ? '' : 'd-idle';
    const stats = (t.stats && t.stats.length)
      ? t.stats.map(s => `<span class="tl-stat"><span class="rl">${esc(s.l)}</span><b>${esc(s.v)}${s.u ? ' ' + esc(s.u) : ''}</b></span>`).join('')
      : `<span class="tl-empty">${soon ? '' : 'never run'}</span>`;
    return `<div class="tl-row${soon ? ' soon' : ''}"${soon ? '' : ` data-tool="${esc(t.id)}" role="button" tabindex="0"`}>
      <span class="mc-dot ${dot}"></span>
      <div class="tl-name"><div class="n">${esc(t.name)}</div></div>
      <div class="tl-stats">${stats}</div>
      <span class="tl-last">${t.lastShort || t.last || '—'}</span>
      ${pill(t)}
      <div class="tl-act">${soon ? '' : `<span class="mcbtn ${t.primary ? 'mcbtn-pri' : 'mcbtn-ghost'} mcbtn-sm">${esc(t.action || 'Open')}</span>`}</div>
    </div>`;
  }

  function chip(t) {
    const soon = t.status === 'soon';
    const st = t.status === 'running' ? '<span class="st run"></span>'
      : (t.stats && t.stats.length) ? '<span class="st done"></span>' : '';
    const tag = soon ? 'div' : 'button';
    const attrs = soon ? '' : ` data-tool="${esc(t.id)}" type="button"`;
    return `<${tag} class="deck-chip${soon ? ' soon' : ''}"${attrs}>
      <span class="ic">${esc(t.icon)}</span>
      <span class="tx"><span class="n">${esc(t.name)}</span><span class="s">${t.sub || (soon ? 'planned' : 'never run')}</span></span>
      ${st}
    </${tag}>`;
  }

  function launcher(tools, view) {
    view = validView(view);
    if (view === 'list') {
      return `<div class="tool-listwrap"><div class="tool-list">${listHeader()}${tools.map(row).join('')}</div></div>`;
    }
    if (view === 'compact') {
      return `<div class="deck">${tools.map(chip).join('')}</div>`;
    }
    return `<div class="tool-grid">${tools.map(card).join('')}</div>`;
  }

  // r: {icon, tool, toolId?, title?, model, host, result, live?, when?, ts};
  // a row without toolId renders inert (no rowlink, no data attributes).
  function ledgerRow(r) {
    const attrs = r.toolId
      ? ` class="rowlink" data-tool="${esc(r.toolId)}"` +
        (r.model ? ` data-model="${esc(r.model)}"` : '') +
        ` title="${esc(r.title || 'Open ' + r.tool)}"`
      : '';
    return `<tr${attrs}>` +
      `<td class="tool"><i>${esc(r.icon)}</i>${esc(r.tool)}</td>` +
      `<td>${esc(r.model || '—')}</td><td>${esc(r.host || '—')}</td>` +
      `<td class="${r.live ? 'live' : 'res'}">${r.result || '—'}</td>` +
      `<td>${r.live ? esc(r.when || 'running') : esc(when(r.ts) || '—')}</td></tr>`;
  }

  const LEDGER_COLS = [
    ['tool', 'Tool'], ['model', 'Model'], ['host', 'Host'],
    ['tps', 'Result'], ['ts', 'Last'],
  ];

  // sort: {key, dir:'asc'|'desc'} — marks the active column header.
  function ledgerHeader(sort) {
    return '<tr>' + LEDGER_COLS.map(([key, label]) => {
      const on = sort && sort.key === key;
      const arr = on ? (sort.dir === 'asc' ? ' ▴' : ' ▾') : '';
      return `<th class="sortable${on ? ' on' : ''}" data-sort="${key}"` +
        ` role="button" tabindex="0">${label}${arr}</th>`;
    }).join('') + '</tr>';
  }

  function ledger(rows, sort) {
    if (!rows || !rows.length) {
      return '<div class="ledger-empty">No results yet. Results from every tool land here.</div>';
    }
    return '<table class="tools-ledger">' + ledgerHeader(sort) +
      rows.map(ledgerRow).join('') + '</table>';
  }

  const _TC_API = { VIEWS, esc, validView, viewOf, age, when, toMs, pill, statsHtml, card, row, listHeader, chip, launcher, ledgerRow, ledgerHeader, ledger };
  if (typeof window !== 'undefined') window.TC = _TC_API;
  if (typeof module !== 'undefined' && module.exports) module.exports = _TC_API;
})();
