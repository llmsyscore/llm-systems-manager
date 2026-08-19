// Energy & cost intelligence display helpers (#470); sub-tab wiring is in
// js/energy.js. IIFE-scoped, exposed as window.EN.
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.EN = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

const SOURCE_LABEL = { psu: 'wall', mac: 'SoC', gpu: 'GPU' };

function fmtUsd(v, digits) {
  if (v == null || Number.isNaN(v)) return '—';
  const d = digits == null ? 2 : digits;
  const sign = v < 0 ? '-' : '';
  return sign + '$' + Math.abs(v).toFixed(d);
}

function fmtKwh(v) {
  if (v == null || Number.isNaN(v)) return '—';
  if (v >= 100) return v.toFixed(0) + ' kWh';
  if (v >= 1) return v.toFixed(1) + ' kWh';
  return (v * 1000).toFixed(0) + ' Wh';
}

function fmtTokens(v) {
  if (v == null || Number.isNaN(v)) return '—';
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'k';
  return String(Math.round(v));
}

function fmtWatts(v) {
  if (v == null || Number.isNaN(v)) return '—';
  return v.toFixed(v >= 100 ? 0 : 1) + ' W';
}

function fmtPct(v) {
  if (v == null || Number.isNaN(v)) return '—';
  return v.toFixed(v >= 10 ? 0 : 1) + '%';
}

function fmtMtokRate(v) {
  if (v == null || Number.isNaN(v)) return '—';
  return fmtUsd(v, v >= 10 ? 1 : (v >= 0.1 || v === 0 ? 2 : 4)) + '/Mtok';
}

function sourceLabel(src) {
  return SOURCE_LABEL[src] || null;
}

// Headline + honest subtitle for the savings hero.
function savingsView(summary) {
  const t = (summary || {}).totals || {};
  const s = (summary || {}).savings_usd;
  const label = ((summary || {}).config || {}).cloud_price_label || 'cloud API';
  if (!t.has_tokens && !t.has_power) {
    return { headline: 'No data in this window', cls: 'muted',
             sub: 'Savings appear for windows where hosts reported power and token telemetry.' };
  }
  if (s == null) {
    const cov = t.mtok_energy_coverage_pct;
    if (t.has_tokens && t.has_power && cov != null && cov < 95) {
      return { headline: 'Savings unavailable', cls: 'muted',
               sub: `Only ${fmtPct(cov)} of measured energy came from hosts that also ` +
                    'report tokens, so the comparison would not be honest.' };
    }
    const missing = !t.has_tokens ? 'token telemetry'
      : (!t.has_power ? 'power telemetry' : null);
    return { headline: 'Savings unavailable', cls: 'muted',
             sub: missing
               ? `No ${missing} in this window, so the comparison would not be honest.`
               : 'Not enough matched power + token telemetry in this window for an honest comparison.' };
  }
  const gained = s >= 0;
  return {
    headline: (gained ? 'You saved ~' : 'Local ran over by ~') + fmtUsd(Math.abs(s)),
    cls: gained ? 'good' : 'warn',
    sub: `${fmtTokens(t.tokens_gen)} tokens generated + ${fmtTokens(t.tokens_prompt)} prompted locally ` +
         `for ${fmtUsd(t.cost_usd)} of electricity vs ~${fmtUsd(t.cloud_cost_usd)} at ${label}.`,
  };
}

// $/Mtok tile subtitle: base label, coverage caveat, or withheld reason.
function mtokSub(t, base) {
  const cov = t.mtok_energy_coverage_pct;
  if (t.usd_per_mtok == null && cov != null && cov < 99.5) {
    return 'needs hosts reporting both power + tokens';
  }
  if (cov != null && cov < 99.5) {
    return `${base} · ${fmtPct(cov)} of energy token-matched`;
  }
  return base;
}

// Stat-tile list for the totals row; null values render as em-dashes.
function totalTiles(totals) {
  const t = totals || {};
  return [
    { label: 'energy used', value: fmtKwh(t.kwh),
      sub: t.has_power ? `${fmtKwh(t.active_kwh)} active · ${fmtKwh(t.idle_kwh)} idle`
                       : 'no power telemetry' },
    { label: 'electricity cost', value: fmtUsd(t.cost_usd),
      sub: t.idle_cost_usd != null ? `${fmtUsd(t.idle_cost_usd)} of it idle` : '' },
    { label: 'tokens generated', value: fmtTokens(t.tokens_gen),
      sub: t.tokens_prompt ? `+ ${fmtTokens(t.tokens_prompt)} prompt` : '' },
    { label: '$/Mtok all-in', value: fmtMtokRate(t.usd_per_mtok),
      sub: mtokSub(t, 'idle power included') },
    { label: '$/Mtok marginal', value: fmtMtokRate(t.usd_per_mtok_active),
      sub: mtokSub(t, 'active power only') },
    { label: 'avg draw', value: fmtWatts(t.avg_watts),
      sub: t.active_pct != null ? `active ${fmtPct(t.active_pct)} of uptime` : '' },
  ];
}

// Per-host display rows with degradation notes instead of blank cells.
function hostRows(hosts) {
  return (hosts || []).map(h => {
    const notes = [];
    if (!h.has_power) notes.push('no power telemetry');
    if (!h.has_tokens) notes.push('no token telemetry');
    return {
      hostname: h.hostname || (h.agent_id || '').slice(0, 8) || '?',
      source: sourceLabel(h.power_source),
      kwh: fmtKwh(h.kwh),
      split: h.has_power && h.kwh > 0 ? Math.round(100 * (h.active_kwh || 0) / h.kwh) : null,
      activePct: fmtPct(h.active_pct),
      tokens: fmtTokens(h.tokens_gen),
      cost: fmtUsd(h.cost_usd),
      mtok: fmtMtokRate(h.usd_per_mtok),
      coverage: fmtPct(h.coverage_pct),
      notes: notes.join(' · '),
    };
  });
}

// Chart series from /api/energy/hourly rows.
function hourlySeries(rows) {
  const r = rows || [];
  return {
    labels: r.map(x => new Date(x.hour_ts * 1000)),
    activeWh: r.map(x => x.active_energy_wh),
    idleWh: r.map(x => Math.max(0, (x.energy_wh || 0) - (x.active_energy_wh || 0))),
    tokens: r.map(x => x.tokens_gen),
  };
}

// "tracking since …" + coverage caveat for the footer.
function coverageNote(summary) {
  const t = (summary || {}).totals || {};
  const since = (summary || {}).since_ts;
  const parts = [];
  if (since) {
    parts.push('tracking since ' + new Date(since * 1000).toISOString().slice(0, 10));
  }
  if (t.coverage_pct != null && t.coverage_pct < 99) {
    parts.push(`observed ${fmtPct(t.coverage_pct)} of this window`);
  }
  if (t.power_coverage_pct != null && t.power_coverage_pct < 99 && t.has_power) {
    parts.push(`power known ${fmtPct(t.power_coverage_pct)} of observed time`);
  }
  return parts.join(' · ');
}

// Window-select options; month values are 'month:YYYY-MM', days 'days:N'.
function windowOptions(now) {
  const d = now ? new Date(now) : new Date();
  const cur = d.toISOString().slice(0, 7);
  const prev = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - 1, 1))
    .toISOString().slice(0, 7);
  return [
    { value: 'month:' + cur, label: 'This month (' + cur + ')' },
    { value: 'month:' + prev, label: 'Previous month (' + prev + ')' },
    { value: 'today', label: 'Today' },
    { value: 'days:7', label: 'Last 7 days' },
    { value: 'days:30', label: 'Last 30 days' },
    { value: 'ytd', label: 'Year to date' },
    { value: 'custom', label: 'Custom range…' },
  ];
}

// custom: {start, end} (YYYY-MM-DD) from the pickers, used only for 'custom'.
function windowQuery(value, custom) {
  const v = String(value || '');
  if (v === 'today') return { days: 1 };
  if (v === 'ytd') return { ytd: 1 };
  if (v === 'custom') {
    const c = custom || {};
    return (c.start && c.end) ? { start: c.start, end: c.end } : {};
  }
  const [kind, arg] = v.split(':');
  if (kind === 'month' && arg) return { month: arg };
  if (kind === 'days' && arg) return { days: arg };
  return {};
}

return { fmtUsd, fmtKwh, fmtTokens, fmtWatts, fmtPct, fmtMtokRate,
         sourceLabel, savingsView, totalTiles, hostRows, hourlySeries,
         coverageNote, windowOptions, windowQuery, SOURCE_LABEL };
});
