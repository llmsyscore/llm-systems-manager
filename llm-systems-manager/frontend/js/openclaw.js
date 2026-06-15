// ---------------------------------------------------------------------------
// OpenClaw analytics (Dashboard → OpenClaw sub-tab)
// ---------------------------------------------------------------------------
function _opcaFmtN(n)    { return (n == null) ? '—' : Number(n).toLocaleString(); }
function _opcaFmtCost(n) { return (n == null) ? '—' : '$' + Number(n).toFixed(4); }
function _opcaFmtTs(s) {
  if (!s) return '—';
  try { return new Date(s).toLocaleString(); } catch(e) { return s; }
}

async function fetchOpenclawAnalytics() {
  try {
    const r = await fetch('/api/openclaw/analytics');
    if (!r.ok) return;
    const data = await r.json();
    renderOpenclawAnalytics(data);
  } catch (e) { /* silent */ }
}

function renderOpenclawAnalytics(data) {
  const t = data.totals || {};
  document.getElementById('opcaTotSessions').textContent = _opcaFmtN(t.sessions);
  document.getElementById('opcaTotMessages').textContent = _opcaFmtN(t.messages);
  document.getElementById('opcaTotTokens').textContent   =
    `${_opcaFmtN(t.input)} / ${_opcaFmtN(t.output)}`;
  document.getElementById('opcaTotCache').textContent    =
    `${_opcaFmtN(t.cacheRead)} / ${_opcaFmtN(t.cacheWrite)}`;
  document.getElementById('opcaTotTools').textContent    = _opcaFmtN(t.tool_uses);
  document.getElementById('opcaTotCost').textContent     = _opcaFmtCost(t.cost);
  document.getElementById('opcaLastUpdate').textContent  =
    'Updated ' + new Date((data.ts || Date.now()/1000)*1000).toLocaleTimeString();

  // Trend indicator
  const trendEl = document.getElementById('opcaTrend');
  const tr = data.trend || {};
  if (tr.trend === 'increasing') {
    trendEl.innerHTML = `<span style="color:var(--note);">↑ increasing</span> <span style="color:var(--fg-dim);">• ~${_opcaFmtN(tr.dailyAvg)}/day</span>`;
  } else if (tr.trend === 'decreasing') {
    trendEl.innerHTML = `<span style="color:var(--ok);">↓ decreasing</span> <span style="color:var(--fg-dim);">• ~${_opcaFmtN(tr.dailyAvg)}/day</span>`;
  } else if (tr.trend === 'stable') {
    trendEl.innerHTML = `<span style="color:var(--accent);">→ stable</span> <span style="color:var(--fg-dim);">• ~${_opcaFmtN(tr.dailyAvg)}/day</span>`;
  } else {
    trendEl.textContent = '—';
  }

  // Task Runs
  renderOpcaTasks(data.tasks || {});
  // Flows
  renderOpcaFlows(data.flows || {});
  // Delivery Queue
  renderOpcaDelivery(data.delivery || {});
  // Daily cost sparkline
  renderOpcaDailyCost(data.daily_cost || {});

  // --- new panels ---
  // Token distribution stacked bar + cache efficiency
  renderOpcaTokenDist(data.token_dist || {}, data.totals || {});
  // Thinking flows: top agents and sessions using extended thinking
  renderOpcaThinkingFlows(data.thinking || {});
  // Predictive analysis: velocity, monthly projection, per-agent forecasts
  renderOpcaPredictive(data);
  // Combined: cost anomaly table + tool attribution with trend arrows
  renderOpcaAnomaliesAndTools(
    data.anomalies      || [],
    data.tool_attribution || [],
    data.tool_trends    || {}
  );

  // Per-agent table
  const tbody = document.getElementById('opcaAgentTbody');
  const agents = data.agents || [];
  if (!agents.length) {
    tbody.innerHTML = '<tr><td colspan="10" style="color:var(--fg-dim);padding:10px;">No agent sessions found.</td></tr>';
  } else {
    tbody.innerHTML = agents.map(a => `
      <tr style="border-bottom:1px solid var(--bg-card-alt);">
        <td style="padding:6px 8px;color:var(--fg);font-weight:600;">${_esc(a.name)}</td>
        <td style="padding:6px 8px;text-align:right;">${_opcaFmtN(a.sessions)}</td>
        <td style="padding:6px 8px;text-align:right;">${_opcaFmtN(a.messages)}</td>
        <td style="padding:6px 8px;text-align:right;">${_opcaFmtN(a.input)}</td>
        <td style="padding:6px 8px;text-align:right;">${_opcaFmtN(a.output)}</td>
        <td style="padding:6px 8px;text-align:right;color:var(--fg-dim);">${_opcaFmtN(a.cacheRead)}</td>
        <td style="padding:6px 8px;text-align:right;color:var(--fg-dim);">${_opcaFmtN(a.cacheWrite)}</td>
        <td style="padding:6px 8px;text-align:right;">${_opcaFmtN(a.tool_uses)}</td>
        <td style="padding:6px 8px;text-align:right;color:var(--ok);">${_opcaFmtCost(a.cost)}</td>
        <td style="padding:6px 8px;color:var(--fg-muted);">${_opcaFmtTs(a.last_ts)}</td>
      </tr>
    `).join('');
  }

  // Recent sessions grouped by agent
  const wrap = document.getElementById('opcaRecentWrap');
  if (!agents.length) { wrap.innerHTML = '<div style="color:var(--fg-dim);">—</div>'; return; }
  wrap.innerHTML = agents.filter(a => a.recent && a.recent.length).map(a => `
    <div style="margin-bottom:14px;">
      <div style="color:var(--fg);font-weight:600;margin-bottom:4px;">${_esc(a.name)}</div>
      <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;">
          <thead>
            <tr style="color:var(--fg-muted);text-align:left;border-bottom:1px solid var(--border);">
              <th style="padding:4px 6px;">Session</th>
              <th style="padding:4px 6px;text-align:right;">Msgs</th>
              <th style="padding:4px 6px;text-align:right;">In</th>
              <th style="padding:4px 6px;text-align:right;">Out</th>
              <th style="padding:4px 6px;text-align:right;">Cache R/W</th>
              <th style="padding:4px 6px;text-align:right;">Tools</th>
              <th style="padding:4px 6px;text-align:right;">Cost</th>
              <th style="padding:4px 6px;">Last</th>
            </tr>
          </thead>
          <tbody>
            ${a.recent.map(s => `
              <tr style="border-bottom:1px solid var(--bg-card);">
                <td style="padding:4px 6px;color:var(--accent);font-family:monospace;font-size:0.85em;" title="${_esc(s.id)}">${_esc(s.id.slice(0,8))}…</td>
                <td style="padding:4px 6px;text-align:right;">${_opcaFmtN(s.messages)}</td>
                <td style="padding:4px 6px;text-align:right;">${_opcaFmtN(s.input)}</td>
                <td style="padding:4px 6px;text-align:right;">${_opcaFmtN(s.output)}</td>
                <td style="padding:4px 6px;text-align:right;color:var(--fg-dim);">${_opcaFmtN(s.cacheRead)} / ${_opcaFmtN(s.cacheWrite)}</td>
                <td style="padding:4px 6px;text-align:right;">${_opcaFmtN(s.tool_uses)}</td>
                <td style="padding:4px 6px;text-align:right;color:var(--ok);">${_opcaFmtCost(s.cost)}</td>
                <td style="padding:4px 6px;color:var(--fg-muted);">${_opcaFmtTs(s.last_ts)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `).join('');
}

// ---- Sub-renderers ---------------------------------------------------------
function _opcaStatusColor(status) {
  const s = (status || '').toLowerCase();
  if (s === 'succeeded')              return 'var(--ok)';
  if (s === 'failed')                 return 'var(--crit)';
  if (s === 'timed_out' || s === 'lost') return 'var(--warn)';
  if (s === 'running')                return 'var(--accent)';
  return 'var(--fg-dim)';
}

function _opcaStatusMod(status) {
  const s = (status || '').toLowerCase();
  if (s === 'succeeded')              return 'ok';
  if (s === 'failed')                 return 'crit';
  if (s === 'timed_out' || s === 'lost') return 'warn';
  if (s === 'running')                return 'info';
  return 'muted';
}

function _opcaStatusIcon(status) {
  const s = (status || '').toLowerCase();
  if (s === 'succeeded') return '✓';
  if (s === 'failed')    return '✗';
  if (s === 'timed_out') return '⊘';
  if (s === 'lost')      return '?';
  if (s === 'running')   return '▶';
  return '•';
}

function _opcaPillsFromStatus(byStatus) {
  const entries = Object.entries(byStatus || {});
  if (!entries.length) return '<span style="color:var(--fg-dim);font-size:0.82em;">no data</span>';
  return entries.map(([s, c]) => `
    <span class="status status--${_opcaStatusMod(s)}">
      ${_opcaStatusIcon(s)} ${_esc(s)}
      <span style="color:var(--fg);">${_opcaFmtN(c)}</span>
    </span>`).join('');
}

function _opcaAgeFromIso(iso) {
  if (!iso) return '—';
  const ms = new Date(iso).getTime();
  if (isNaN(ms)) return '—';
  const d = Math.floor((Date.now() - ms) / 1000);
  if (d < 60)      return d + 's ago';
  if (d < 3600)    return Math.floor(d/60) + 'm ago';
  if (d < 86400)   return Math.floor(d/3600) + 'h ago';
  return Math.floor(d/86400) + 'd ago';
}

function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function renderOpcaTasks(tasks) {
  document.getElementById('opcaTaskStatusPills').innerHTML = _opcaPillsFromStatus(tasks.by_status);
  const avg = tasks.avg_duration_s;
  const avgEl = document.getElementById('opcaTaskAvg');
  avgEl.textContent = (avg != null) ? `• avg ${avg.toFixed(2)}s (succeeded)` : '';
  const rt = tasks.by_runtime || {};
  const rtStr = Object.entries(rt).map(([k, v]) => `${k}: ${v}`).join(' • ');
  document.getElementById('opcaTaskRuntimes').textContent = rtStr ? 'Runtime — ' + rtStr : '';

  const fwrap = document.getElementById('opcaTaskFailsWrap');
  const fails = tasks.recent_failures || [];
  if (!fails.length) {
    fwrap.innerHTML = '<div style="color:var(--fg-dim);font-size:0.82em;padding:4px 0;">No recent failures.</div>';
  } else {
    fwrap.innerHTML = `
      <table style="width:100%;border-collapse:collapse;font-size:0.78em;">
        <thead>
          <tr style="color:var(--fg-muted);text-align:left;border-bottom:1px solid var(--border);">
            <th style="padding:4px 6px;">Label</th>
            <th style="padding:4px 6px;">Kind</th>
            <th style="padding:4px 6px;">Status</th>
            <th style="padding:4px 6px;">Error</th>
            <th style="padding:4px 6px;">Age</th>
          </tr>
        </thead>
        <tbody>
          ${fails.map(f => `
            <tr style="border-bottom:1px solid var(--bg-card);">
              <td style="padding:4px 6px;color:var(--fg);">${_esc((f.label || '').slice(0,40)) || '<span style="color:var(--fg-dim);">—</span>'}</td>
              <td style="padding:4px 6px;color:var(--fg);">${_esc(f.kind || '')}</td>
              <td style="padding:4px 6px;color:${_opcaStatusColor(f.status)};">${_esc(f.status)}</td>
              <td style="padding:4px 6px;color:var(--crit);" title="${_esc(f.error)}">${_esc((f.error || '').slice(0,80))}</td>
              <td style="padding:4px 6px;color:var(--fg-muted);">${_opcaAgeFromIso(f.created_iso)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>`;
  }
}

function renderOpcaFlows(flows) {
  document.getElementById('opcaFlowStatusPills').innerHTML = _opcaPillsFromStatus(flows.by_status);
  const wrap = document.getElementById('opcaFlowRecentWrap');
  const recent = (flows.recent || []).slice(0, 10);
  if (!recent.length) {
    wrap.innerHTML = `
      <div style="color:var(--fg-muted);font-size:0.85em;line-height:1.4;">
        No flow runs recorded yet. Flows are orchestrated multi-step agent
        runs tracked by OpenClaw — distinct from per-session activity. They
        appear here once the flows registry receives runs.
      </div>`;
    return;
  }
  wrap.innerHTML = `
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="color:var(--fg-muted);text-align:left;border-bottom:1px solid var(--border);">
          <th style="padding:4px 6px;">Goal</th>
          <th style="padding:4px 6px;">Status</th>
          <th style="padding:4px 6px;text-align:right;">Duration</th>
          <th style="padding:4px 6px;">Age</th>
        </tr>
      </thead>
      <tbody>
        ${recent.map(f => `
          <tr style="border-bottom:1px solid var(--bg-card);">
            <td style="padding:4px 6px;color:var(--fg);" title="${_esc(f.goal)}">${_esc((f.goal || '—').slice(0,60))}</td>
            <td style="padding:4px 6px;color:${_opcaStatusColor(f.status)};">${_opcaStatusIcon(f.status)} ${_esc(f.status)}</td>
            <td style="padding:4px 6px;text-align:right;color:var(--fg);">${f.duration_s != null ? Number(f.duration_s) + 's' : '—'}</td>
            <td style="padding:4px 6px;color:var(--fg-muted);">${_opcaAgeFromIso(f.created_iso)}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>`;
}

function renderOpcaDelivery(dq) {
  const badge = document.getElementById('opcaDeliveryBadge');
  const total = dq.total || 0;
  if (total > 0) {
    badge.innerHTML = `<span class="status status--crit">${total} failed</span>`;
  } else {
    badge.innerHTML = '<span class="status status--ok">all clear</span>';
  }
  const body = document.getElementById('opcaDeliveryBody');
  if (!total) {
    body.innerHTML = '<div style="color:var(--ok);">No messages stuck in failed queue.</div>';
    return;
  }
  const channels = Object.entries(dq.by_channel || {})
    .map(([k, v]) => `<span style="display:inline-block;margin-right:8px;"><span style="color:var(--accent);">${_esc(k)}</span>: ${_opcaFmtN(v)}</span>`)
    .join('');
  const errors = (dq.common_errors || [])
    .map(e => `<div style="color:var(--crit);font-size:0.82em;margin-top:2px;">• <span style="color:var(--fg);">${_opcaFmtN(e.count)}×</span> ${_esc(e.error)}</div>`)
    .join('');
  body.innerHTML = `
    <div style="margin-bottom:6px;">By channel — ${channels || '<span style="color:var(--fg-dim);">?</span>'}</div>
    <div style="margin-bottom:6px;">Total retries: <span style="color:var(--fg);">${_opcaFmtN(dq.total_retries)}</span>
      <span style="margin-left:12px;">Oldest: <span style="color:var(--fg-muted);">${_opcaAgeFromIso(dq.oldest_enqueue_iso)}</span></span></div>
    ${errors ? '<div style="margin-top:8px;"><div style="color:var(--fg-muted);font-size:0.78em;text-transform:uppercase;letter-spacing:0.05em;">Top errors</div>' + errors + '</div>' : ''}
  `;
}

function renderOpcaDailyCost(daily) {
  const wrap = document.getElementById('opcaDailyCostSpark');
  const days = Object.keys(daily).sort();
  if (!days.length) { wrap.innerHTML = '<div style="color:var(--fg-dim);">No daily cost data.</div>'; return; }
  const vals = days.map(d => daily[d] || 0);
  const max = Math.max(...vals, 0.001);
  const total = vals.reduce((a, b) => a + b, 0);
  // All-zero days mean local-model usage — show an honest empty state instead
  // of a flat chart of 2px stubs so the panel reads as informative, not broken.
  if (total === 0) {
    wrap.innerHTML = `
      <div style="color:var(--fg-muted);font-size:0.85em;line-height:1.4;">
        $0.00 across ${days.length} day${days.length === 1 ? '' : 's'} —
        local models incur no API cost.
      </div>`;
    return;
  }
  const barColor = (v) => v >= 0.50 ? 'var(--crit)' : v >= 0.10 ? 'var(--warn)' : 'var(--ok)';
  const bars = days.map((d, i) => {
    const v = vals[i];
    const h = Math.max(2, Math.round((v / max) * 70));
    const label = d.slice(5); // MM-DD
    return `
      <div style="display:flex;flex-direction:column;align-items:center;flex:1;min-width:0;">
        <div title="${_esc(d)}: $${v.toFixed(4)}" style="width:80%;height:${h}px;background:${barColor(v)};border-radius:2px 2px 0 0;"></div>
        <div style="font-size:0.65em;color:var(--fg-dim);margin-top:4px;white-space:nowrap;">${_esc(label)}</div>
      </div>`;
  }).join('');
  wrap.innerHTML = `
    <div style="color:var(--fg);font-size:0.82em;margin-bottom:6px;">Total: <span style="color:var(--ok);font-weight:600;">$${total.toFixed(4)}</span>
      <span style="color:var(--fg-dim);margin-left:8px;">max day: $${max.toFixed(4)}</span></div>
    <div style="display:flex;align-items:flex-end;gap:4px;height:90px;">${bars}</div>`;
}

// ---- Panel 1: Token Distribution -------------------------------------------
// Renders a horizontal stacked bar showing input/output/cache_r/cache_w as
// proportional segments, plus a cache efficiency callout below.
function renderOpcaTokenDist(td, totals) {
  const card = document.getElementById('opcaTokenDistCard');
  if (!card) return;
  if (!td || !td.total) {
    // Local models (llama.cpp / LM Studio) don't report usage tokens, so the
    // distribution is zero. Surface the activity that IS tracked instead so
    // the panel isn't a dead "no data" blank: messages, sessions, tool uses.
    const t = totals || {};
    const stats = `
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:8px;">
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:10px;text-align:center;">
          <div style="font-size:1.4em;font-weight:600;color:var(--accent);">${_opcaFmtN(t.messages || 0)}</div>
          <div style="font-size:0.72em;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.04em;">Messages</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:10px;text-align:center;">
          <div style="font-size:1.4em;font-weight:600;color:#7ec8a0;">${_opcaFmtN(t.sessions || 0)}</div>
          <div style="font-size:0.72em;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.04em;">Sessions</div>
        </div>
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:10px;text-align:center;">
          <div style="font-size:1.4em;font-weight:600;color:#c8a07e;">${_opcaFmtN(t.tool_uses || 0)}</div>
          <div style="font-size:0.72em;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.04em;">Tool uses</div>
        </div>
      </div>`;
    card.innerHTML = `
      <h3 style="margin-top:0;">Token Distribution</h3>
      <div style="color:var(--fg-muted);font-size:0.82em;line-height:1.4;">
        Local models (llama.cpp / LM Studio) don't report token usage —
        showing session activity instead.
      </div>${stats}`;
    return;
  }
  // Each segment is a flex child whose width equals its percentage of total tokens.
  // Colors: input=blue, output=green, cache_read=amber, cache_write=purple.
  const segments = [
    { label: 'Input',       pct: Number(td.input_pct)   || 0, color: '#4a90d9' },
    { label: 'Output',      pct: Number(td.output_pct)  || 0, color: '#7ec8a0' },
    { label: 'Cache Read',  pct: Number(td.cache_r_pct) || 0, color: '#c8a07e' },
    { label: 'Cache Write', pct: Number(td.cache_w_pct) || 0, color: '#9a7ec8' },
  ];
  const bar = segments.map(s =>
    `<div title="${s.label}: ${s.pct}%" style="flex:${s.pct};background:${s.color};height:18px;min-width:${s.pct > 0 ? 2 : 0}px;"></div>`
  ).join('');
  const legend = segments.map(s =>
    `<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px;">
       <span style="display:inline-block;width:10px;height:10px;background:${s.color};border-radius:2px;"></span>
       <span style="color:var(--fg);font-size:0.82em;">${s.pct}% ${s.label}</span>
     </span>`
  ).join('');
  // cache_hit_pct: how many input reads were served from cache (cost saver)
  const hitColor = td.cache_hit_pct >= 40 ? 'var(--ok)' : td.cache_hit_pct >= 15 ? 'var(--warn)' : 'var(--crit)';
  card.innerHTML = `
    <h3 style="margin-top:0;">Token Distribution</h3>
    <div style="display:flex;border-radius:4px;overflow:hidden;margin-bottom:8px;">${bar}</div>
    <div style="margin-bottom:10px;">${legend}</div>
    <div style="display:flex;gap:24px;font-size:0.85em;">
      <span>Cache efficiency: <strong style="color:${hitColor};">${(td.cache_hit_pct || 0).toFixed(1)}%</strong>
        <span style="color:var(--fg-dim);font-size:0.82em;margin-left:4px;">(cache_read / input+cache_read)</span></span>
      <span>Total: <strong style="color:var(--fg);">${_opcaFmtN(td.total)}</strong> tokens</span>
    </div>`;
}

// ---- Panel 2: Thinking Flows -----------------------------------------------
// Renders a horizontal bar chart of agents ranked by extended-thinking events,
// followed by a table of the top thinking-heavy sessions.
// Data comes from thinking_events / thinking_chars accumulated per session.
function renderOpcaThinkingFlows(thinking) {
  const card = document.getElementById('opcaThinkingCard');
  if (!card) return;
  const agents   = thinking.agents   || [];
  const sessions = thinking.sessions || [];
  if (!agents.length) {
    card.innerHTML = `
      <h3 style="margin-top:0;">Thinking Flows <span style="color:var(--fg-muted);font-size:0.75em;font-weight:400;">(extended reasoning)</span></h3>
      <div style="color:var(--fg-dim);font-size:0.85em;">No extended thinking detected in recent sessions.</div>`;
    return;
  }
  const maxEv = Math.max(...agents.map(a => a.thinking_events), 1);
  const agentBars = agents.map(a => {
    const bw = Math.round((a.thinking_events / maxEv) * 100);
    const kchars = (a.thinking_chars / 1000).toFixed(1);
    return `
      <div style="display:grid;grid-template-columns:140px 1fr auto;gap:8px;align-items:center;margin-bottom:6px;">
        <div style="color:var(--fg);font-size:0.85em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
             title="${_esc(a.name)}">${_esc(a.name)}</div>
        <div style="background:var(--bg-card);height:12px;border-radius:6px;overflow:hidden;">
          <div style="background:#9a7ec8;height:100%;width:${bw}%;border-radius:6px;"></div>
        </div>
        <div style="color:var(--fg);font-size:0.82em;white-space:nowrap;">${_opcaFmtN(a.thinking_events)} events · ${kchars}k chars</div>
      </div>`;
  }).join('');

  // Session table — only shown when there are sessions with thinking
  const sessRows = sessions.length ? `
    <table style="width:100%;border-collapse:collapse;font-size:0.82em;margin-top:12px;">
      <thead>
        <tr style="color:var(--fg-muted);text-align:left;border-bottom:1px solid var(--border);">
          <th style="padding:4px 6px;">Session</th>
          <th style="padding:4px 6px;">Agent</th>
          <th style="padding:4px 6px;text-align:right;">Events</th>
          <th style="padding:4px 6px;text-align:right;">Chars</th>
          <th style="padding:4px 6px;text-align:right;">Cost</th>
        </tr>
      </thead>
      <tbody>
        ${sessions.map(s => `
          <tr style="border-bottom:1px solid var(--bg-card);">
            <td style="padding:4px 6px;color:var(--accent);font-family:monospace;font-size:0.9em;"
                title="${_esc(s.id)}">${_esc(s.id.slice(0,8))}…</td>
            <td style="padding:4px 6px;color:var(--fg);">${_esc(s.agent || '')}</td>
            <td style="padding:4px 6px;text-align:right;">${_opcaFmtN(s.thinking_events)}</td>
            <td style="padding:4px 6px;text-align:right;color:var(--fg-muted);">${_opcaFmtN(s.thinking_chars)}</td>
            <td style="padding:4px 6px;text-align:right;color:var(--ok);">${_opcaFmtCost(s.cost)}</td>
          </tr>`).join('')}
      </tbody>
    </table>` : '';

  card.innerHTML = `
    <h3 style="margin-top:0;">Thinking Flows <span style="color:var(--fg-muted);font-size:0.75em;font-weight:400;">(extended reasoning)</span></h3>
    ${agentBars}${sessRows}`;
}

// ---- Panel 3: Predictive Analysis ------------------------------------------
// Shows current token velocity (burn rate over last 60 min), a monthly cost
// projection based on the global 7-day trend, active session count, and a
// per-agent table of individual monthly projections and trend directions.
function renderOpcaPredictive(data) {
  const card = document.getElementById('opcaPredictiveCard');
  if (!card) return;
  const vel = data.velocity || {};
  const tr  = data.trend    || {};
  const t   = data.totals   || {};

  // Compute dollar projection from token prediction + observed $/token rate.
  // Avoids hardcoding a price — uses the actual ratio from collected data.
  const observedRate = t.cost / Math.max(t.input + t.output, 1);
  const projTokens   = tr.monthlyPrediction || 0;
  const projUsd      = projTokens > 0 ? (projTokens * observedRate).toFixed(2) : null;
  // When projUsd is null (no cost data, e.g. all local models) we still want
  // the tile to convey something real. Substitute the projected message
  // throughput from velocity*60min*24h*30d so the projection panel surfaces
  // a useful number instead of '—'.
  const projMsgsPerMonth = (vel.tokens_per_min || 0) === 0 && t.messages
    ? Math.round((t.messages / Math.max(t.sessions || 1, 1)) * 30)  // very rough: per-session avg × 30 days
    : null;

  // Trend arrow and color for the global trend
  const trendArrow = { increasing: '↑', decreasing: '↓', stable: '→' };
  const trendColor = { increasing: 'var(--warn)', decreasing: 'var(--ok)', stable: 'var(--accent)' };
  const tDir = tr.trend || 'insufficient_data';

  // Velocity color: green < 500 tok/min, amber < 2000, red >= 2000
  const vpm = vel.tokens_per_min || 0;
  const velColor = vpm >= 2000 ? 'var(--crit)' : vpm >= 500 ? 'var(--warn)' : 'var(--ok)';

  const statCards = `
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px;">
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:12px;">
        <div style="color:var(--fg-muted);font-size:0.75em;text-transform:uppercase;letter-spacing:0.05em;">Monthly Projection</div>
        <div style="font-size:1.5em;font-weight:600;color:var(--ok);margin:4px 0;">
          ${projUsd != null ? '$' + projUsd : (projMsgsPerMonth != null ? _opcaFmtN(projMsgsPerMonth) : '—')}
        </div>
        <div style="font-size:0.8em;color:${trendColor[tDir] || 'var(--fg-dim)'};">
          ${projUsd != null
            ? `${trendArrow[tDir] || '?'} ${tDir !== 'insufficient_data' ? tDir : 'insufficient data'}`
            : (projMsgsPerMonth != null ? 'msgs/mo (local models, no cost)' : 'no projection data')}
        </div>
      </div>
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:12px;">
        <div style="color:var(--fg-muted);font-size:0.75em;text-transform:uppercase;letter-spacing:0.05em;">Token Velocity</div>
        <div style="font-size:1.5em;font-weight:600;color:${velColor};margin:4px 0;">
          ${_opcaFmtN(Math.round(vpm))} <span style="font-size:0.55em;color:var(--fg-dim);">tok/min</span>
        </div>
        <div style="font-size:0.8em;color:var(--fg-dim);">${_opcaFmtN(vel.tokens_1h || 0)} last 60 min</div>
      </div>
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:12px;">
        <div style="color:var(--fg-muted);font-size:0.75em;text-transform:uppercase;letter-spacing:0.05em;">Active Sessions</div>
        <div style="font-size:1.5em;font-weight:600;color:var(--accent);margin:4px 0;">
          ${_opcaFmtN(vel.active_sessions || 0)}
        </div>
        <div style="font-size:0.8em;color:var(--fg-dim);">files active in last hour</div>
      </div>
    </div>`;

  // Per-agent projection table — only agents with enough data to project
  const agents = (data.agents || []).filter(a => a.monthly_projection_usd != null);
  const agentTable = agents.length ? `
    <table style="width:100%;border-collapse:collapse;font-size:0.84em;">
      <thead>
        <tr style="color:var(--fg-muted);text-align:left;border-bottom:1px solid var(--border);">
          <th style="padding:5px 8px;">Agent</th>
          <th style="padding:5px 8px;text-align:right;">7d Avg Cost/day</th>
          <th style="padding:5px 8px;text-align:right;">Monthly Proj</th>
          <th style="padding:5px 8px;text-align:center;">Trend</th>
        </tr>
      </thead>
      <tbody>
        ${agents.map(a => {
          const aDir   = (a.trend || {}).trend || '';
          const aArrow = trendArrow[aDir] || '?';
          const aColor = trendColor[aDir] || 'var(--fg-dim)';
          // Compute 7d average from projection (projection = avg * 30)
          const avgDay = a.monthly_projection_usd != null
            ? '$' + (a.monthly_projection_usd / 30).toFixed(3) : '—';
          return `
            <tr style="border-bottom:1px solid var(--bg-card);">
              <td style="padding:5px 8px;color:var(--fg);font-weight:600;">${_esc(a.name)}</td>
              <td style="padding:5px 8px;text-align:right;color:var(--fg);">${avgDay}</td>
              <td style="padding:5px 8px;text-align:right;color:var(--ok);">$${a.monthly_projection_usd.toFixed(2)}</td>
              <td style="padding:5px 8px;text-align:center;color:${aColor};">${aArrow}</td>
            </tr>`;
        }).join('')}
      </tbody>
    </table>` : '<div style="color:var(--fg-dim);font-size:0.85em;">No agents with sufficient data for projection.</div>';

  card.innerHTML = `
    <h3 style="margin-top:0;">Predictive Analysis</h3>
    ${statCards}
    ${agentTable}`;
}

// ---- Panel 4: Cost Anomalies + Tool Attribution with Trends ----------------
// Left column: sessions whose cost exceeded 2x the rolling prior-session
// average. Hidden entirely when no anomalies exist.
// Right column: tool attribution table (same as before) with an added 7d
// trend arrow column. Replaces the old standalone renderOpcaTools function.
function renderOpcaAnomaliesAndTools(anomalies, tools, trends) {
  // --- left column: cost anomalies ---
  const aWrap = document.getElementById('opcaAnomalyWrap');
  if (aWrap) {
    if (!anomalies.length) {
      // Hide the left column entirely — the tool table expands to full width
      aWrap.style.display = 'none';
      const parent = aWrap.parentElement;
      if (parent) parent.style.gridTemplateColumns = '1fr';
    } else {
      aWrap.style.display = '';
      const parent = aWrap.parentElement;
      if (parent) parent.style.gridTemplateColumns = '1fr 1fr';
      aWrap.innerHTML = `
        <h3 style="margin-top:0;color:var(--crit);">Cost Anomalies
          <span style="font-size:0.75em;font-weight:400;color:var(--fg-muted);">(cost > 2× rolling avg)</span>
        </h3>
        <table style="width:100%;border-collapse:collapse;font-size:0.84em;">
          <thead>
            <tr style="color:var(--fg-muted);text-align:left;border-bottom:1px solid var(--border);">
              <th style="padding:5px 6px;">Session</th>
              <th style="padding:5px 6px;">Agent</th>
              <th style="padding:5px 6px;text-align:right;">Ratio</th>
              <th style="padding:5px 6px;text-align:right;">Cost</th>
            </tr>
          </thead>
          <tbody>
            ${anomalies.map(a => `
              <tr style="border-bottom:1px solid var(--bg-card);">
                <td style="padding:5px 6px;color:var(--crit);font-family:monospace;font-size:0.88em;"
                    title="${_esc(a.session_id)}">${_esc((a.session_id || '').slice(0,8))}…</td>
                <td style="padding:5px 6px;color:var(--fg);">${_esc(a.agent)}</td>
                <td style="padding:5px 6px;text-align:right;color:var(--note);">${Number(a.ratio)}×</td>
                <td style="padding:5px 6px;text-align:right;color:var(--crit);">${_opcaFmtCost(a.cost)}</td>
              </tr>`).join('')}
          </tbody>
        </table>`;
    }
  }

  // --- right column: tool attribution with trend arrows ---
  // Trend direction -> HTML arrow with color
  function _trendCell(tool) {
    const dir = trends[tool] || 'stable';
    const map = {
      up:     '<span style="color:var(--ok);" title="More calls vs prior 7d">↑</span>',
      down:   '<span style="color:var(--crit);" title="Fewer calls vs prior 7d">↓</span>',
      stable: '<span style="color:var(--fg-dim);">→</span>',
      new:    '<span style="color:var(--note);" title="New in last 7d">✦</span>',
      gone:   '<span style="color:var(--fg-faint);" title="No calls in last 7d">–</span>',
    };
    return map[dir] || map.stable;
  }

  const tbody = document.getElementById('opcaToolTbody');
  if (!tbody) return;
  if (!tools.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="color:var(--fg-dim);padding:10px;">No tool usage recorded.</td></tr>';
    return;
  }
  const maxPct = Math.max(...tools.map(t => t.pct || 0), 1);
  tbody.innerHTML = tools.map((t, i) => {
    const color = i < 3 ? 'var(--accent)' : 'var(--fg)';
    const bw    = Math.round(((t.pct || 0) / maxPct) * 100);
    return `
      <tr style="border-bottom:1px solid var(--bg-card-alt);">
        <td style="padding:6px 8px;color:${color};font-weight:${i < 3 ? 600 : 400};
                   font-family:monospace;">${_esc(t.tool)}</td>
        <td style="padding:6px 8px;text-align:right;">${_opcaFmtN(t.count)}</td>
        <td style="padding:6px 8px;text-align:right;color:var(--fg);">${(t.pct || 0).toFixed(1)}%</td>
        <td style="padding:6px 8px;min-width:80px;">
          <div style="background:var(--bg-card);height:8px;border-radius:4px;overflow:hidden;">
            <div style="background:#4a7;height:100%;width:${bw}%;"></div>
          </div>
        </td>
        <td style="padding:6px 8px;text-align:center;">${_trendCell(t.tool)}</td>
      </tr>`;
  }).join('');
}
