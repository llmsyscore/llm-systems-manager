// Overall-tab fleet-band view-model transforms (#565); band wiring is in
// js/overall.js. Pure functions only — no fetch, no DOM. Exposed as window.OV.
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.OV = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

const PROVIDER_LABEL = { llama: 'llama.cpp', lms: 'LM Studio', vllm: 'vLLM' };

// Hero bucket widths (ms) offered by #ovHeroBucket; 5 minutes by default.
const HERO_BUCKET_CHOICES = [60000, 300000, 900000, 3600000];
const HERO_BUCKET_DEFAULT_MS = 300000;

// Any stored/DOM value → a valid bucket width, falling back to the default.
function heroBucketMs(raw) {
  const v = parseInt(raw, 10);
  return HERO_BUCKET_CHOICES.includes(v) ? v : HERO_BUCKET_DEFAULT_MS;
}

// Shared time-bucket helper: window global in the browser, sibling in Node.
const _series = (typeof window !== 'undefined' && window.LMSeries)
  || (typeof require === 'function' ? require('./series.js') : null);

function _num(v) { return (typeof v === 'number' && !Number.isNaN(v)) ? v : null; }

// Null-safe sum: null when every input is null, else the numeric sum.
function _sumOrNull(...vs) {
  const nums = vs.map(_num).filter(v => v !== null);
  return nums.length ? nums.reduce((a, b) => a + b, 0) : null;
}

function _fmtKwh(v) {
  if (v == null || Number.isNaN(v)) return '—';
  if (v >= 100) return v.toFixed(0) + ' kWh';
  if (v >= 1) return v.toFixed(1) + ' kWh';
  return (v * 1000).toFixed(0) + ' Wh';
}

function _fmtUsd(v) {
  if (v == null || Number.isNaN(v)) return '—';
  const sign = v < 0 ? '-' : '';
  return sign + '$' + Math.abs(v).toFixed(2);
}

// Null-safe max: null only when both inputs are null.
function _maxOrNull(a, b) {
  if (a == null) return b;
  if (b == null) return a;
  return Math.max(a, b);
}

// Fleet watts for one history row: the larger of the wall-metered and GPU
// cross-host sums, so GPU-only hosts still count in mixed fleets.
function _powerOf(r) {
  return _maxOrNull(_num(r.psu_in), _num(r.gpu_power));
}

// Rows from /api/history?fleet=all → [{ts, gen, prompt, power}] with chart
// gaps. With bucketMs, each bucket keeps the peak values (one point each).
function heroSeries(rows, bucketMs) {
  if (!Array.isArray(rows)) return [];
  const pts = rows.map(r => ({
    ts: r.ts,
    gen: _sumOrNull(r.llama_tps, r.vllm_tps, r.lms_tps),
    prompt: _sumOrNull(r.llama_pps, r.vllm_pps, r.lms_pps),
    power: _powerOf(r),
  }));
  if (!bucketMs) return pts;
  const out = [];
  let lastKey = null;
  for (const p of pts) {
    const key = _series.bucketDate(p.ts, bucketMs).getTime();
    if (!Number.isFinite(key)) continue;
    if (key === lastKey) {
      const last = out[out.length - 1];
      last.gen = _maxOrNull(last.gen, p.gen);
      last.prompt = _maxOrNull(last.prompt, p.prompt);
      last.power = _maxOrNull(last.power, p.power);
    } else {
      lastKey = key;
      out.push({ ts: new Date(key).toISOString(), gen: p.gen,
                 prompt: p.prompt, power: p.power });
    }
  }
  return out;
}

// Hourly energy rows mapped onto chart label times (ms): each label gets
// the Wh of the hour bucket it falls in, null when unmetered.
function energySeries(hourlyRows, labelsMs) {
  const byHour = {};
  (Array.isArray(hourlyRows) ? hourlyRows : []).forEach(r => {
    if (r && r.hour_ts != null) byHour[r.hour_ts] = _num(r.energy_wh);
  });
  return (labelsMs || []).map(ms => {
    const v = byHour[Math.floor(ms / 3600000) * 3600];
    return v == null ? null : v;
  });
}

function _fmt1(v) { return v != null ? Number(v).toFixed(1) : '—'; }

// The three /api/fleet/<p>/aggregate payloads → provider tile view-models.
function tiles(llama, lms, vllm) {
  const out = [];
  {
    const a = llama || {};
    const online = a.agent_count_online || 0;
    const gpu = a.gpu || {};
    const tp = a.throughput || {};
    let accent = 'off';
    if (online > 0) accent = (a.awake_agent_count || 0) > 0 ? 'ok' : 'warn';
    if (online > 0 && _num(gpu.max_temp_c) != null && gpu.max_temp_c >= 85) accent = 'crit';
    out.push({
      key: 'llama', label: PROVIDER_LABEL.llama,
      online, total: a.agent_count_total || 0, accent,
      stats: [
        { v: _fmt1(tp.total_tps), l: 'gen t/s' },
        { v: _fmt1(tp.total_pps), l: 'prompt t/s' },
        { v: String(a.awake_agent_count || 0), l: 'awake' },
        { v: String(a.active_model_count || 0), l: 'models' },
      ],
    });
  }
  {
    const a = lms || {};
    const online = a.agent_count_online || 0;
    const accent = online > 0
      ? ((a.busy_process_count_total || 0) > 0 ? 'ok' : 'warn') : 'off';
    out.push({
      key: 'lms', label: PROVIDER_LABEL.lms,
      online, total: a.agent_count_total || 0, accent,
      stats: [
        { v: String(a.server_on_count || 0), l: 'servers on' },
        { v: String(a.loaded_model_count_total || 0), l: 'models loaded' },
        { v: String(a.busy_agent_count || 0), l: 'busy' },
        { v: String(a.process_count_total || 0), l: 'processes' },
      ],
    });
  }
  {
    const a = vllm || {};
    const online = a.agent_count_online || 0;
    const tp = a.throughput || {};
    const accent = online > 0
      ? ((a.requests_running_total || 0) > 0 ? 'ok' : 'warn') : 'off';
    out.push({
      key: 'vllm', label: PROVIDER_LABEL.vllm,
      online, total: a.agent_count_total || 0, accent,
      stats: [
        { v: String(a.server_on_count || 0), l: 'servers on' },
        { v: String(a.requests_running_total || 0), l: 'requests' },
        { v: _fmt1(tp.total_tps), l: 'gen t/s' },
        { v: (_num(a.max_kv_cache_pct) != null ? a.max_kv_cache_pct.toFixed(0) + '%' : '—'), l: 'kv cache' },
      ],
    });
  }
  return out;
}

function _agentDetail(prov, row) {
  if (prov === 'llama') {
    const state = row.state || 'unknown';
    return row.model ? `${state} · ${row.model}` : state;
  }
  if (prov === 'lms') {
    if (!row.server_on) return 'server off';
    const state = (row.busy_process_count || 0) > 0 ? 'busy' : 'idle';
    const count = row.loaded_model_count || 0;
    const names = (row.loaded_models || []).slice(0, 3);
    if (!names.length) return `${state} · ${count} model${count === 1 ? '' : 's'}`;
    // +N covers models beyond the display cap or without a reported name.
    const extra = Math.max(0, count - names.length);
    return `${state} · ${names.join(' · ')}` + (extra ? ` +${extra}` : '');
  }
  const req = _num(row.requests_running);
  if (!row.server_on) return 'server off';
  const base = `${req != null ? req : 0} req`;
  return row.model ? `${base} · ${row.model}` : base;
}

// Aggregates' per-agent rows joined with list-by-provider hostnames →
// one row per distinct agent, hostname asc, offline last.
function agentRows(aggs, byProvider) {
  const hostById = {};
  Object.values(byProvider || {}).forEach(list => (list || []).forEach(a => {
    if (a && a.agent_id) hostById[a.agent_id] = a.hostname || null;
  }));
  const provs = ['llama', 'lms', 'vllm'];
  const byAgent = {};
  (aggs || []).forEach((agg, i) => {
    const prov = provs[i];
    ((agg && agg.agents) || []).forEach(row => {
      const id = row.agent_id;
      if (!id) return;
      const rec = byAgent[id] = byAgent[id] || {
        agentId: id,
        hostname: hostById[id] || (id.slice(0, 8) + '…'),
        online: false, ageS: null, provs: [],
      };
      rec.online = rec.online || !!row.online;
      const age = _num(row.age_s);
      if (age != null && (rec.ageS == null || age < rec.ageS)) rec.ageS = age;
      rec.provs.push({ prov, detail: _agentDetail(prov, row) });
    });
  });
  return Object.values(byAgent).sort((a, b) =>
    (a.online === b.online ? 0 : a.online ? -1 : 1)
    || a.hostname.localeCompare(b.hostname));
}

// Active-alert rows → strip summary: counts, worst severity, newest 3.
function alertsSummary(alerts) {
  const counts = { critical: 0, warning: 0, info: 0 };
  const rows = Array.isArray(alerts) ? alerts : [];
  rows.forEach(a => {
    if (a && counts[a.severity] != null) counts[a.severity]++;
  });
  const worst = counts.critical ? 'critical'
    : counts.warning ? 'warning'
    : counts.info ? 'info' : null;
  const newest = rows.slice()
    .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
    .slice(0, 3)
    .map(a => ({ id: a.alert_id, rule: a.rule_name, severity: a.severity, ts: a.created_at }));
  return { total: rows.length, counts, worst, newest };
}

// /api/energy/summary payload → {kwh, cost} chip, or null when unmetered.
function energyChip(summary) {
  if (!summary || !summary.ok) return null;
  const t = summary.totals || {};
  if (!t.has_power) return null;
  return { kwh: _fmtKwh(_num(t.kwh)), cost: _fmtUsd(_num(t.cost_usd)) };
}

// Hero overlay stats: agents online, models in flight, fleet GPU W, energy.
function toplines(llama, lms, vllm, energy) {
  const online = ((llama && llama.agent_count_online) || 0)
    + ((lms && lms.agent_count_online) || 0)
    + ((vllm && vllm.agent_count_online) || 0);
  const models = ((llama && llama.active_model_count) || 0)
    + ((vllm && vllm.active_model_count) || 0);
  const watts = ((llama && llama.gpu && llama.gpu.total_power_watts) || 0)
    + ((vllm && vllm.total_gpu_power_watts) || 0);
  const chip = energyChip(energy);
  return [
    { v: String(online), l: 'agents online' },
    { v: String(models), l: 'models in flight' },
    { v: watts > 0 ? watts.toFixed(0) + ' W' : '—', l: 'fleet GPU power' },
    { v: chip ? `${chip.kwh} · ${chip.cost}` : '—', l: 'energy consumption' },
  ];
}

return { heroSeries, energySeries, tiles, agentRows, alertsSummary, energyChip,
         toplines, heroBucketMs, PROVIDER_LABEL, HERO_BUCKET_CHOICES,
         HERO_BUCKET_DEFAULT_MS };
});
