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

  // Absolute run stamp: time today, +date this year, +year otherwise.
  function stamp(ts, nowMs) {
    const ms = toMs(ts);
    if (ms == null) return null;
    const d = new Date(ms), n = new Date(nowMs != null ? nowMs : Date.now());
    const opts = { hour: 'numeric', minute: '2-digit' };
    if (d.toDateString() !== n.toDateString()) { opts.month = 'short'; opts.day = 'numeric'; }
    if (d.getFullYear() !== n.getFullYear()) opts.year = 'numeric';
    return d.toLocaleString('en-US', opts);
  }

  const _PILL = { ready: 'p-idle', running: 'p-busy', queued: 'p-unloaded', soon: 'p-unloaded' };

  function pill(t) {
    const cls = _PILL[t.status] || 'p-unloaded';
    const label = t.statusLabel || (t.status === 'running' ? 'Running'
      : t.status === 'queued' ? 'Queued'
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
    const st = (t.status === 'running' || t.status === 'queued') ? '<span class="st run"></span>'
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
  function ledgerRow(r, nowMs) {
    const attrs = r.toolId
      ? ` class="rowlink" data-tool="${esc(r.toolId)}"` +
        (r.model ? ` data-model="${esc(r.model)}"` : '') +
        (r.target && r.target.provider ? ` data-provider="${esc(r.target.provider)}"` +
          (r.target.agent ? ` data-agent="${esc(r.target.agent)}"` : '') : '') +
        ` title="${esc(r.title || 'Open ' + r.tool)}"`
      : '';
    const pk = r.pick;
    const pick = pk
      ? `<button type="button" class="tl-pick${pk.on ? ' on' : ''}" data-pick="${esc(pk.key)}"` +
        ` aria-pressed="${pk.on ? 'true' : 'false'}" aria-label="Pick for compare"` +
        ` title="${esc(pk.why || 'Pick two runs of the same model to compare')}"${pk.disabled ? ' disabled' : ''}></button>`
      : '';
    return `<tr${attrs}><td class="pick">${pick}</td>` +
      `<td class="tool"><i>${esc(r.icon)}</i>${esc(r.tool)}</td>` +
      `<td>${esc(r.model || '—')}</td><td>${esc(r.host || '—')}</td>` +
      `<td class="${r.live ? 'live' : 'res'}">${r.result || '—'}</td>` +
      `<td>${r.live ? esc(r.when || 'running') : esc(stamp(r.ts, nowMs) || '—')}</td></tr>`;
  }

  const LEDGER_COLS = [
    ['tool', 'Tool'], ['model', 'Model'], ['host', 'Host'],
    ['tps', 'Result'], ['ts', 'Last'],
  ];

  // sort: {key, dir:'asc'|'desc'} — marks the active column header.
  function ledgerHeader(sort) {
    return '<tr><th class="pick" aria-label="Compare"></th>' + LEDGER_COLS.map(([key, label]) => {
      const on = sort && sort.key === key;
      const arr = on ? (sort.dir === 'asc' ? ' ▴' : ' ▾') : '';
      return `<th class="sortable${on ? ' on' : ''}" data-sort="${key}"` +
        ` role="button" tabindex="0">${label}${arr}</th>`;
    }).join('') + '</tr>';
  }

  function ledger(rows, sort, nowMs) {
    if (!rows || !rows.length) {
      return '<div class="ledger-empty">No results yet. Results from every tool land here.</div>';
    }
    return '<table class="tools-ledger">' + ledgerHeader(sort) +
      rows.map(r => ledgerRow(r, nowMs)).join('') + '</table>';
  }

  // Ledger diff (#892): [summary key, label, decimals, +1 higher is better / -1 lower is better].
  const DIFF_METRICS = {
    benchmark: [['gen_tps', 'Gen t/s', 1, 1], ['ppt_tps', 'Prompt t/s', 0, 1], ['pg_tps', 'Prompt + gen t/s', 1, 1],
      ['latency_s', 'Latency s', 2, -1], ['accept_rate', 'Draft accept', 2, 1], ['wh_per_ktok', 'Wh / 1k tok', 2, -1]],
    autotune: [['decode_tps', 'Decode t/s', 1, 1], ['prefill_tps', 'Prefill t/s', 0, 1], ['agg_tps', 'Aggregate t/s', 0, 1],
      ['ctx_size', 'Context', 0, 1], ['free_mb', 'Memory free MB', 0, 1], ['gain_pct', 'Gain vs before %', 0, 1],
      ['avg_w', 'Average W', 0, -1], ['wh_per_ktok', 'Wh / 1k tok', 2, -1], ['kl', 'KL', 4, -1]],
  };
  const DIFF_SETUP = {
    benchmark: [['bench_tool', 'Bench tool']],
    autotune: [['objective', 'Objective'], ['mode', 'Mode'], ['llama_build', 'Build']],
  };

  function _num(v) { return v == null || v === '' || !isFinite(v) ? null : Number(v); }

  // Two ledger runs {tool, model_id, ok, summary, ts, host} → rows for diffHtml; older run is A.
  function diffRuns(x, y) {
    const [a, b] = (toMs(x.ts) || 0) <= (toMs(y.ts) || 0) ? [x, y] : [y, x];
    const sa = a.summary || {}, sb = b.summary || {};
    const row = (label, va, vb) => ({ label, a: va == null || va === '' ? null : String(va),
      b: vb == null || vb === '' ? null : String(vb) });
    const setup = [row('Host', a.host, b.host), row('Result', a.ok === false ? 'failed' : 'ok', b.ok === false ? 'failed' : 'ok')]
      .concat((DIFF_SETUP[a.tool] || []).map(([k, l]) => row(l, sa[k], sb[k])))
      .filter(r => r.a != null || r.b != null);
    const swa = sa.switches && typeof sa.switches === 'object' ? sa.switches : null;
    const swb = sb.switches && typeof sb.switches === 'object' ? sb.switches : null;
    const keys = Array.from(new Set(Object.keys(swa || {}).concat(Object.keys(swb || {})))).sort();
    const switches = keys.map(k => row(k, swa && swa[k], swb && swb[k]));
    const results = (DIFF_METRICS[a.tool] || []).map(([k, label, dp, dir]) => {
      const va = _num(sa[k]), vb = _num(sb[k]);
      if (va == null && vb == null) return null;
      const pct = va != null && vb != null && va !== 0 ? (vb - va) / Math.abs(va) * 100 : null;
      const tone = pct == null || Math.abs(pct) < 0.5 ? '' : (pct * dir > 0 ? 'good' : 'bad');
      return { label, a: va == null ? null : va.toFixed(dp), b: vb == null ? null : vb.toFixed(dp), pct, tone };
    }).filter(Boolean);
    [...setup, ...switches].forEach(r => { r.changed = r.a !== r.b; });
    return { tool: a.tool, model: a.model_id || '', a, b, setup, switches, results,
             recorded: { a: !!swa, b: !!swb } };
  }

  function diffHtml(d, nowMs) {
    const cell = (v, cls) => `<td class="${cls}">${v == null ? '—' : esc(v)}</td>`;
    const tr = (r, mono) => `<tr class="${r.changed ? 'chg' : 'same'}"><td${mono ? ' class="mono"' : ''}>${esc(r.label)}</td>` +
      cell(r.a, r.changed ? 'old' : 'mono') + cell(r.b, r.changed ? 'new' : 'mono') + '</tr>';
    const sec = t => `<tr class="sec"><td colspan="3">${esc(t)}</td></tr>`;
    const toolName = d.tool === 'autotune' ? 'Autotune' : 'Benchmark';
    const miss = !d.recorded.a && !d.recorded.b ? 'Neither run recorded its switches.'
      : !d.recorded.a ? 'Run A predates switch recording.' : !d.recorded.b ? 'Run B recorded no switches.' : '';
    const changed = d.switches.filter(r => r.changed).length;
    let body = sec('Setup') + d.setup.map(r => tr(r, false)).join('');
    body += sec(d.switches.length ? `Switches · ${changed} of ${d.switches.length} differ` : 'Switches');
    body += d.switches.length ? d.switches.map(r => tr(r, true)).join('')
      : `<tr class="same"><td colspan="3" class="ev">${esc(miss || 'No switches set on either run.')}</td></tr>`;
    body += sec('Results') + (d.results.length ? d.results.map(r => {
      const dl = r.pct == null ? '' : ` <span class="tl-dl ${r.tone}">${r.pct >= 0 ? '+' : ''}${Math.round(r.pct)} %</span>`;
      return `<tr class="same"><td>${esc(r.label)}</td>${cell(r.a, 'mono')}<td class="mono">${r.b == null ? '—' : esc(r.b)}${dl}</td></tr>`;
    }).join('') : '<tr class="same"><td colspan="3" class="ev">No comparable results.</td></tr>');
    return `<div class="tl-diff"><div class="tl-diff-h"><span class="t">Compare · ${esc(toolName)} · ${esc(d.model)}</span>` +
      '<span class="lgap"></span><button class="mcbtn mcbtn-ghost mcbtn-sm" type="button" data-diff="clear">Clear</button></div>' +
      (miss && d.switches.length ? `<div class="tl-diff-note">${esc(miss)}</div>` : '') +
      '<div class="tl-diff-b"><table class="at-rt tl-diff-t"><thead><tr><th style="width:30%">Setting</th>' +
      `<th>Run A · ${esc(stamp(d.a.ts, nowMs) || '—')}</th><th>Run B · ${esc(stamp(d.b.ts, nowMs) || '—')}</th></tr></thead>` +
      `<tbody>${body}</tbody></table></div></div>`;
  }

  function diffHint(toolName, model) {
    return `<div class="tl-diff tl-diff-hint"><span>Pick one more ${esc(toolName)} run of <b>${esc(model)}</b> to compare.</span>` +
      '<span class="lgap"></span><button class="mcbtn mcbtn-ghost mcbtn-sm" type="button" data-diff="clear">Clear</button></div>';
  }

  const _TC_API = { VIEWS, esc, validView, viewOf, age, when, stamp, toMs, pill, statsHtml, card, row, listHeader, chip, launcher, ledgerRow, ledgerHeader, ledger, diffRuns, diffHtml, diffHint };
  if (typeof window !== 'undefined') window.TC = _TC_API;
  if (typeof module !== 'undefined' && module.exports) module.exports = _TC_API;
})();
