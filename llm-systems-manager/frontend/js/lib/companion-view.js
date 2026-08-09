// View-model transforms for the companion screens (#522): raw API JSON ->
// render-ready objects. Pure + defensive (missing fields degrade to '—' /
// idle, never throw). Dual-mode (window.CView + CJS for vitest).
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.CView = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

  const num = (v) => (typeof v === 'number' && isFinite(v) ? v : null);
  const round = (v) => (num(v) == null ? null : Math.round(v));
  const clampPct = (v) => (num(v) == null ? 0 : Math.max(0, Math.min(100, v)));

  function usd(v) {
    if (num(v) == null) return '—';
    return '$' + Math.abs(v).toFixed(2);
  }
  function kwh(v) {
    if (num(v) == null) return '—';
    return v >= 1 ? v.toFixed(1) + ' kWh' : (v * 1000).toFixed(0) + ' Wh';
  }

  // Seconds since ts (epoch s/ms or ISO string), or null if unparseable.
  function _secs(ts, nowSec) {
    // The alarm engine serializes naive-UTC datetimes with no zone; bare
    // Date.parse reads those as local time. tsSeconds normalizes both.
    const t = tsSeconds(ts);
    if (t == null) return null;
    const now = nowSec == null ? Date.now() / 1000 : nowSec;
    return Math.max(0, now - t);
  }

  // Epoch seconds for a timestamp in any of the shapes the APIs emit.
  function tsSeconds(ts) {
    if (typeof ts === 'number' && isFinite(ts)) return ts > 1e12 ? ts / 1000 : ts;
    if (typeof ts === 'string' && ts) {
      const p = Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(ts) ? ts : ts + 'Z');
      if (!Number.isNaN(p)) return p / 1000;
    }
    return null;
  }

  // Relative age, e.g. "2m ago".
  function age(ts, nowSec) {
    const s = _secs(ts, nowSec);
    if (s == null) return '—';
    if (s < 45) return 'just now';
    if (s < 3600) return Math.round(s / 60) + 'm ago';
    if (s < 86400) return Math.round(s / 3600) + 'h ago';
    return Math.round(s / 86400) + 'd ago';
  }

  // Compact heartbeat freshness, e.g. "4s" / "2m" / "1h".
  function hbAge(ts, nowSec) {
    const s = _secs(ts, nowSec);
    if (s == null) return null;
    if (s < 90) return Math.round(s) + 's';
    if (s < 5400) return Math.round(s / 60) + 'm';
    return Math.round(s / 3600) + 'h';
  }

  // System block of a provider sample; llama's payload IS the system block.
  const sysBlock = (s) => ((s || {}).system && typeof s.system === 'object'
    ? s.system : (s || {}));

  // (watts, source) for one provider sample — mirrors energy.extract_power's
  // PSU wall > Apple SoC > GPU precedence.
  function sampleWatts(s) {
    const sysb = sysBlock(s);
    const psu = ((sysb.liquidctl || {}).psu || {})['Estimated input power'];
    if (psu && num(psu.value) != null) return { w: psu.value, src: 'PSU' };
    const mac = (s || {}).mac_power || sysb.mac_power || {};
    if (num(mac.soc_total_w) != null) return { w: mac.soc_total_w, src: 'SoC' };
    const g = sysb.gpu || (s || {}).gpu || {};
    if (num(g.power_watts) != null) return { w: g.power_watts, src: 'GPU' };
    return null;
  }

  const hostKey = (s) => sysBlock(s).host || (s || {}).host || (s || {}).agent_id || null;

  // 1234567 -> "1.2M". Token counts get large; the tile has ~6 characters.
  function compact(v) {
    if (num(v) == null) return '—';
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1) + 'B';
    if (a >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (a >= 1e3) return (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + 'k';
    return String(Math.round(v));
  }

  // Totals over the 24 h rate series: tokens are the integral of tok/s, so a
  // bursty workload reads as work done instead of a mostly-zero instant rate.
  // A gap longer than an hour is a reporting outage, not idle time.
  function window24(hist) {
    const h = Array.isArray(hist) ? hist : [];
    const out = { gen: 0, prompt: 0, seconds: 0, avgGen: 0, peak: 0, peakAt: null };
    for (let i = 1; i < h.length; i++) {
      const dt = (num(h[i].t) || 0) - (num(h[i - 1].t) || 0);
      if (!(dt > 0) || dt > 3600) continue;
      out.gen += (num(h[i].v) || 0) * dt;
      out.prompt += (num(h[i].p) || 0) * dt;
      out.seconds += dt;
    }
    h.forEach((p) => {
      const v = num(p.v) || 0;
      if (v > out.peak) { out.peak = v; out.peakAt = p.t; }
    });
    if (out.seconds > 0) out.avgGen = out.gen / out.seconds;
    return out;
  }

  // ── Glance ──────────────────────────────────────────────────────────────
  function glance(d) {
    d = d || {};
    const m = d.metrics || {};
    const lm = m.llama || {};
    const ls = d.llama || {};
    const lms = d.lms || {};
    const vs = d.vllm || {};
    const vllm = vs.vllm || {};
    const gpu = m.gpu || {};
    const en = d.energy || {};

    const llamaModel = ls.model || lm.model || null;
    const llamaResident = !!llamaModel && !/unloaded/i.test(llamaModel);
    const llamaAwake = (ls.state || lm.state) === 'awake';
    // LM Studio ps lists LOADED models; status IDLE means loaded-not-
    // generating, so only STOPPED/unloaded rows are excluded.
    const lmsLoaded = (lms.ps || []).filter(
      (p) => p && !/^(stopped|unloaded)$/i.test(String(p.status || '')));
    const lmsPs = lms.active || lmsLoaded[0] || null;
    const lmsModel = lmsPs ? (lmsPs.model || lmsPs.identifier) : null;
    const vllmRunning = String(vllm.state || '') === 'running';

    // hero = provider with the highest live gen rate
    const cands = [
      { prov: 'llama.cpp', tps: num(lm.tokens_per_second), model: llamaModel },
      { prov: 'LM Studio', tps: num((lms.gateway_rates || {}).gen_tps), model: lmsModel },
      { prov: 'vLLM', tps: num(vllm.tokens_per_second), model: vllm.model },
    ].filter((c) => c.model);
    let hero = cands.filter((c) => c.tps > 0).sort((a, b) => b.tps - a.tps)[0];
    if (!hero) hero = { prov: llamaResident ? 'llama.cpp' : (lmsModel ? 'LM Studio' : '—'),
                        tps: 0, model: llamaModel || lmsModel || null };

    // Every provider row reads the same two things: inference status + GPU use.
    const gpuPct = (v) => (num(v) == null ? null : Math.max(0, Math.min(100, v)));
    const llamaGpu = gpuPct(gpu.gpu_util_percent);
    const lmsGpu = gpuPct((lms.mac_power || sysBlock(lms).mac_power || {}).gpu_busy_pct);
    const vllmGpu = gpuPct((sysBlock(vs).gpu || vs.gpu || {}).gpu_util_percent);

    const llamaGen = num(lm.tokens_per_second) > 0 || num(lm.active_slots) > 0
      || num(lm.requests_processing) > 0;
    const lmsGen = num((lms.gateway_rates || {}).gen_tps) > 0
      || lmsLoaded.some((p) => !/^idle$/i.test(String(p.status || '')));
    const vllmGen = num(vllm.requests_running) > 0 || num(vllm.tokens_per_second) > 0;

    const provRow = (name, ok, word, model, g) => ({
      status: ok ? 'ok' : 'idle',
      name,
      detail: model ? word + ' · ' + model : word,
      rN: g == null ? '—' : Math.round(g) + '%',
      rUnit: 'gpu',
    });
    const providers = [
      provRow('llama.cpp', llamaAwake && llamaResident,
        llamaResident ? (llamaGen ? 'generating' : 'idle')
          : (ls.state === 'sleeping' ? 'sleeping' : 'no model'),
        llamaResident ? llamaModel : null, llamaGpu),
      provRow('LM Studio', !!lmsModel,
        lmsModel ? (lmsGen ? 'generating' : 'idle') : 'no model',
        lmsModel, lmsGpu),
      provRow('vLLM', vllmRunning,
        vllmRunning ? (vllmGen ? 'generating' : 'idle')
          : (vllm.state ? String(vllm.state) : 'stopped'),
        vllmRunning ? (vllm.model || null) : null, vllmGpu),
    ];

    // The GPU block comes from the PRIMARY llama agent, which on a split
    // fleet may not be the host with the GPU. Fall back to the newest fleet
    // history sample so the tiles aren't blank while the mini graphs plot.
    const trendLast = {};
    (Array.isArray(d.trends) ? d.trends : []).forEach((t) => {
      if (t && t.key) trendLast[t.key] = num(t.last);
    });
    const temp = num(gpu.temperature_c) != null ? gpu.temperature_c : trendLast.gpu_temp;
    const vramPct = num(gpu.vram_usage_percent) != null
      ? gpu.vram_usage_percent : trendLast.gpu_vram;
    const vramUsedGb = num(gpu.vram_used_mb) != null ? gpu.vram_used_mb / 1024 : null;
    const vramTotalGb = (vramUsedGb != null && num(gpu.vram_usage_percent))
      ? vramUsedGb / (gpu.vram_usage_percent / 100) : null;
    const enT = en.totals || {};
    // Input power sums every provider sample that reports power, deduped by
    // host, then falls back to the energy accumulator's fleet average.
    const perHost = new Map();
    [m, lms, vs].forEach((s, i) => {
      const w = sampleWatts(s);
      if (!w) return;
      const k = hostKey(s) || ('sample' + i);
      if (!perHost.has(k)) perHost.set(k, w);
    });
    const liveW = perHost.size
      ? [...perHost.values()].reduce((a, b) => a + b.w, 0) : null;
    const fleetW = num(enT.avg_watts);
    const watts = liveW != null ? liveW : fleetW;
    const srcs = [...new Set([...perHost.values()].map((x) => x.src))].join(' + ');
    const wattsSub = liveW != null
      ? srcs + ' · ' + perHost.size + (perHost.size === 1 ? ' host' : ' hosts')
      : (fleetW != null ? 'fleet avg · this window' : 'no telemetry');

    // Cross-provider totals — the aggregate the provider rows break down.
    const gr = lms.gateway_rates || {};
    const total = (vals) => {
      const f = vals.filter((v) => num(v) != null);
      return f.length ? f.reduce((a, b) => a + b, 0) : null;
    };
    const genTps = total([lm.tokens_per_second, gr.gen_tps, vllm.tokens_per_second]);
    // llama reports slots and in-flight requests separately; take the larger.
    // null (not 0) with neither present, so "no telemetry" reads as a dash.
    const llamaBusy = (num(lm.requests_processing) == null
      && num(lm.active_slots) == null)
      ? null
      : Math.max(num(lm.requests_processing) || 0, num(lm.active_slots) || 0);
    const inflight = total([llamaBusy, vllm.requests_running]);
    const queued = total([lm.requests_deferred, vllm.requests_waiting]);
    const loadedOn = [llamaResident ? 'llama.cpp' : null, lmsModel ? 'LM Studio' : null,
      (vllmRunning && vllm.model) ? 'vLLM' : null].filter(Boolean);
    // Rates are bursty — they read 0 between requests — so the headline is the
    // window TOTAL, integrated from the rate series. Counters would reset on a
    // provider restart; the rate series doesn't.
    const win = window24(d.hist);
    const fleet = [
      { v: win.gen > 0 ? compact(win.gen) : '—', unit: 'tokens',
        k: 'Generated · 24 h',
        sub: win.gen > 0
          ? 'avg ' + win.avgGen.toFixed(1) + ' tok/s · prompt ' + compact(win.prompt)
          : (genTps != null ? 'idle · ' + genTps.toFixed(1) + ' tok/s now'
            : 'no history yet') },
      { v: inflight != null ? String(Math.round(inflight)) : '—', unit: 'req',
        k: 'In flight',
        sub: num(queued) ? Math.round(queued) + ' queued' : 'nothing queued' },
      { v: String(loadedOn.length), unit: loadedOn.length === 1 ? 'model' : 'models',
        k: 'Loaded', sub: loadedOn.join(' + ') || 'none loaded' },
      { v: win.peak > 0 ? win.peak.toFixed(0) : '—', unit: 'tok/s', k: '24 h peak',
        sub: win.peak > 0 && win.peakAt ? 'at ' + clockAt(win.peakAt)
          : 'no history yet' },
    ];

    const tiles = [
      { v: round(temp) != null ? String(round(temp)) : '—', unit: '°C',
        k: 'GPU temperature', meter: clampPct(temp), hot: num(temp) != null && temp >= 85 },
      // GB when the live sample carries them, else the fleet history's percent.
      { v: vramUsedGb != null ? vramUsedGb.toFixed(1)
        : (num(vramPct) != null ? String(Math.round(vramPct)) : '—'),
        unit: vramUsedGb != null
          ? (vramTotalGb != null ? '/ ' + Math.round(vramTotalGb) + ' GB' : 'GB') : '%',
        k: 'VRAM', meter: clampPct(vramPct), hot: num(vramPct) != null && vramPct >= 85 },
      { v: round(watts) != null ? String(round(watts)) : '—', unit: 'W',
        k: 'Input power', sub: wattsSub },
      { v: usd(enT.cost_usd), k: 'Energy today',
        sub: num(enT.kwh) != null
          ? kwh(enT.kwh) + (num(en.price_kwh) != null ? ' · $' + en.price_kwh + '/kWh' : '')
          : 'no telemetry' },
    ];

    return {
      hero: {
        n: hero.tps > 0 ? hero.tps.toFixed(1) : '0.0',
        tps: hero.tps > 0 ? hero.tps : 0,
        unit: 'tok/s',
        label: hero.model ? hero.prov + ' · ' + hero.model : 'idle · no model loaded',
      },
      providers,
      fleet,
      tiles,
    };
  }

  // ── 24 h fleet trends (Home mini graphs) ────────────────────────────────
  // Keys are /api/history legacy field names; the manager already aggregates
  // each one across hosts (gpu_util mean, gpu_temp/gpu_vram max, cpu mean).
  const TREND_SPECS = [
    { key: 'gpu_util', name: 'GPU busy', unit: '%', dp: 0 },
    { key: 'gpu_temp', name: 'GPU temp', unit: '°C', dp: 0 },
    { key: 'gpu_vram', name: 'VRAM', unit: '%', dp: 0 },
    { key: 'cpu_total', name: 'CPU', unit: '%', dp: 0 },
  ];

  function trends(rows) {
    const list = Array.isArray(rows) ? rows : [];
    return TREND_SPECS.map((s) => {
      const pts = [];
      list.forEach((r) => {
        const t = tsSeconds((r || {}).ts);
        const v = num((r || {})[s.key]);
        if (t != null && v != null) pts.push({ t, v });
      });
      const vals = pts.map((p) => p.v);
      // The headline is the window MEAN, not the newest sample: history lags
      // the live tiles by a bucket, and two different "now" numbers on one
      // screen read as a contradiction.
      return {
        key: s.key, name: s.name, unit: s.unit, dp: s.dp, pts,
        avg: vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null,
        last: vals.length ? vals[vals.length - 1] : null,
        min: vals.length ? Math.min(...vals) : null,
        max: vals.length ? Math.max(...vals) : null,
      };
    }).filter((t) => t.pts.length > 1);
  }

  // Local wall-clock label for a trend/peak timestamp.
  function clockAt(t) {
    return new Date(t * 1000)
      .toLocaleTimeString('en', { hour: 'numeric', minute: '2-digit' });
  }

  // ── Alerts ──────────────────────────────────────────────────────────────
  const SEV_GLYPH = { crit: '!', warn: '▲', ok: '✓', info: 'i' };

  function sevClass(severity) {
    const s = String(severity || '').toLowerCase();
    if (s === 'critical' || s === 'crit') return 'crit';
    if (s === 'warning' || s === 'warn') return 'warn';
    return 'warn';
  }

  function alertRow(a, nowSec) {
    const resolved = a.status === 'closed' || a.status === 'ignored'
      || a.status === 'exception' || !!a.closed_at;
    const info = !resolved && (a.category === 'info' || a.severity === 'info');
    const sev = resolved ? 'ok' : (info ? 'info' : sevClass(a.severity));
    // `tone` colors the word by original severity; `sev` keeps driving the
    // glyph and the counts, so a resolved row still shows a green check.
    const sevWord = info ? 'info' : String(a.severity || 'warning').toLowerCase();
    const tone = info ? 'info' : (resolved ? sevClass(a.severity) : sev);
    let word;
    if (resolved) word = sevWord + ' · resolved';
    else if (info) word = 'info';
    else word = sevWord + (a.status === 'acknowledged' ? ' · acked' : ' · firing');
    const when = resolved ? (a.closed_at || a.acknowledged_at || a.created_at)
      : a.created_at;
    const host = a.source_host || '—';
    const path = [a.metric_source, a.metric_name].filter(Boolean).join('/') || 'event';
    const msg = a.message || (a.rule_name || path);
    return {
      id: a.alert_id,
      sev, tone, glyph: SEV_GLYPH[sev], word,
      msg,
      // Which rule fired. Suppressed when the message already IS the rule name.
      rule: (a.rule_name && a.rule_name !== msg) ? a.rule_name : '',
      meta: path + ' · ' + host + ' · ' + age(when, nowSec),
      // Resolved/info rows never offer Ack, whatever their status field says.
      ackable: a.status === 'active' && !(resolved || info),
      acked: a.status === 'acknowledged',
      info,
      resolved,
    };
  }

  function alerts(list, nowSec) {
    const rows = (list || []).map((a) => alertRow(a, nowSec));
    const firing = rows.filter((r) => !r.resolved);
    const earlier = rows.filter((r) => r.resolved);
    return {
      firing, earlier,
      counts: {
        // Badge = unread: firing, actionable (not info) and not acknowledged.
        badge: firing.filter((r) => !r.info && !r.acked).length,
        critical: firing.filter((r) => r.sev === 'crit').length,
        warning: firing.filter((r) => r.sev === 'warn').length,
        info: firing.filter((r) => r.info).length,
      },
    };
  }

  // ── Admin (read-only) ───────────────────────────────────────────────────
  function admin(d) {
    d = d || {};
    const health = d.health || {};
    const svcOf = (name) => (health.services || []).find((s) => s.name === name) || {};
    const ae = svcOf('alarm_engine');
    const influx = svcOf('influxdb');
    const backup = d.backup || {};
    const auth = d.auth || {};

    const now0 = d.now == null ? Date.now() / 1000 : d.now;
    const raw = d.agents || [];
    const agents = raw.filter((a) => !(a.liveness === 'pending' || a.status === 'pending'))
      .map((a) => {
        const live = a.liveness === 'live';
        // /api/agents has no tls flag; agents serve TLS on 8082 so bind_url is https.
        const tls = /^https/i.test(String(a.bind_url || ''));
        const hb = live ? hbAge(a.last_heartbeat, d.now) : null;
        return {
          id: a.agent_id,
          status: live ? 'ok' : 'down',
          name: a.hostname || (a.agent_id || '').slice(0, 10) || '?',
          detail: (a.is_host_agent ? 'local · manager host · ' : '')
            + 'agent ' + (a.version || '?')
            + (!a.is_host_agent && tls ? ' · TLS' : '')
            + (a.update_available ? ' · update available' : ''),
          right: live ? 'live' : (a.liveness || 'down'),
          rightSub: hb ? 'hb ' + hb : '',
          warn: !live,
        };
      });

    // Approve/deny cards belong here now, and only when something is waiting.
    const pending = raw.filter((a) => a.liveness === 'pending' || a.status === 'pending')
      .map((a) => ({
        id: a.agent_id,
        name: a.hostname || (a.agent_id || '').slice(0, 10) || '?',
        detail: 'registered ' + age(a.first_seen, d.now)
          + (a.version ? ' · agent ' + a.version : '') + ' · pending approval',
      }));

    // Manager, alarm engine and InfluxDB as one restartable service block.
    const mgr0 = health.manager || {};
    const up0 = num(mgr0.uptime_s);
    const services = [
      { key: 'manager', name: 'Manager',
        status: 'ok',
        detail: (d.version || '—')
          + (up0 != null ? ' · up ' + age(now0 - up0, now0).replace(' ago', '') : ''),
        right: d.releaseNote || '', canRestart: !!d.health },
      { key: 'alarm_engine', name: 'Alarm engine',
        status: ae.ok === true ? 'ok' : (d.health ? 'down' : 'idle'),
        detail: !d.health ? 'status needs admin'
          : (ae.ok
            ? (ae.version || 'reachable')
              + (num(ae.uptime_s) != null
                ? ' · up ' + age(now0 - ae.uptime_s, now0).replace(' ago', '') : '')
              + ((ae.tls && ae.tls.active) ? ' · TLS' : '')
            : 'unreachable'),
        // Gates on a local/containerized unit, not on reachability — an
        // unreachable engine is exactly when restart matters.
        canRestart: !!d.health
          && (health.ae_local === true || health.containerized === true) },
      { key: 'influxdb', name: 'InfluxDB',
        status: (influx.state === 'connected' || influx.ok === true)
          ? 'ok' : (d.health ? 'down' : 'idle'),
        detail: !d.health ? 'status needs admin'
          : (influx.version || influx.state || (influx.ok ? 'connected' : 'unknown'))
            + (influx.state && influx.version ? ' · ' + influx.state : '')
            + (num(influx.ping_ms) != null ? ' · ' + influx.ping_ms + 'ms' : ''),
        canRestart: false },
    ];

    // InfluxDB and the alarm engine moved into `services`; keeping them here
    // too would just say the same thing twice.
    const backupLast = backup.last || {};
    const rows = [
      { name: 'Authentication',
        detail: (auth.mode || 'session') + ' mode'
          + (auth.current_user ? ' · ' + auth.current_user : '') },
      { name: 'Backups',
        detail: backup.enabled === false ? 'disabled'
          : (backupLast.ok === false ? 'last failed'
            : ('last ' + age(backupLast.ts || backupLast.mtime, d.now) + ' · '
               + (backup.keep_last != null ? backup.keep_last + ' kept' : 'ok'))) },
    ];

    const mgr = health.manager || {};
    const uptime = num(mgr.uptime_s);
    const now = d.now == null ? Date.now() / 1000 : d.now;
    const stale = num(d.agentUpdates) || 0;
    return {
      manager: {
        version: d.version || '—',
        uptime: uptime != null ? age(now - uptime, now) : null,
        updateNote: stale
          ? stale + ' agent' + (stale === 1 ? '' : 's') + ' outdated' : '',
      },
      services, agents, pending, rows,
    };
  }

  // Audit-log rows (#217 endpoint) -> one line each.
  function audit(entries, nowSec) {
    return (entries || []).slice(0, 40).map((e) => ({
      name: (e.action || e.path || 'action'),
      detail: [e.actor || 'unknown', e.outcome || e.status, age(e.ts, nowSec)]
        .filter(Boolean).join(' · ')
        + (e.target ? ' → ' + e.target : ''),
      ok: e.outcome ? /^(ok|success|allow)/i.test(String(e.outcome)) : null,
    }));
  }

  // ── Actions (control surface) ───────────────────────────────────────────
  const PROV_PIN = [
    { key: 'llama', label: 'llama.cpp', pinKey: 'llama_model_pins' },
    { key: 'lms', label: 'LM Studio', pinKey: 'lms_model_pins' },
    { key: 'vllm', label: 'vLLM', pinKey: 'vllm_model_pins' },
  ];
  const PROV_LABEL = { llama: 'llama.cpp', lms: 'LM Studio', vllm: 'vLLM' };

  function actions(d) {
    d = d || {};
    const ls = d.llama || {};
    const health = d.health;
    const ap = d.autopilot;
    const ag = d.agents;
    const gated = !health && !ap && !ag;

    const model = ls.model || null;
    const resident = !!model && !/unloaded/i.test(model);
    const glob = (ag || {}).global || {};
    const pins = glob.llama_model_pins || {};
    const pinned = !!(model && pins[model]);
    const llamaUp = ls.agent_online === true && ls.state !== 'unknown';

    const mgr = (health || {}).manager || {};
    const up = num(mgr.uptime_s);
    const now = d.now == null ? Date.now() / 1000 : d.now;
    const aeSvc = ((health || {}).services || []).find((s) => s.name === 'alarm_engine') || {};

    // Provider units only — manager/AE/InfluxDB live on the Admin screen.
    const services = [
      { key: 'llama', name: 'llama.cpp server',
        status: llamaUp ? 'ok' : (ls.agent_online === false ? 'down' : 'idle'),
        detail: llamaUp ? (ls.state || 'up') + (resident ? ' · ' + model : '')
          : (ls.agent_online === false ? 'agent offline' : ls.state || 'unknown'),
        canRestart: ls.agent_online === true },
    ];

    // LM Studio + vLLM rows appear only when a capable agent exists.
    const lm = d.lms;
    const lmsLoaded = (lm && lm.agent_id)
      ? ((lm.lms || lm).ps || (lm.ps || [])).filter(
        (p) => p && !/^(stopped|unloaded)$/i.test(String(p.status || ''))) : [];
    if (lm && lm.agent_id) {
      services.push({
        key: 'lms', name: 'LM Studio',
        status: lm.agent_online !== true ? 'down' : (lmsLoaded.length ? 'ok' : 'idle'),
        detail: lm.agent_online === true
          ? (lmsLoaded.length
            ? (lmsLoaded[0].model || lmsLoaded[0].identifier || 'model loaded')
            : 'no model loaded')
          : 'agent offline',
        canRestart: lm.agent_online === true });
    }
    const vl = (d.vllm || {}).vllm;
    const vllmRunning = !!vl && String(vl.state || '') === 'running';
    if (vl && Object.keys(vl).length) {
      services.push({
        key: 'vllm', name: 'vLLM',
        status: (d.vllm || {}).agent_online === false ? 'down'
          : (vllmRunning ? 'ok' : 'idle'),
        detail: vllmRunning ? (vl.model || 'running') : (vl.state || 'unit stopped'),
        canRestart: (d.vllm || {}).agent_online === true });
    }

    // agent_id -> hostname, so pin targets read as hosts not UUID prefixes.
    const hostOf = {};
    ((ag || {}).agents || []).forEach((a) => {
      if (a && a.agent_id) hostOf[a.agent_id] = a.hostname || String(a.agent_id).slice(0, 10);
    });
    const hostName = (aid) => hostOf[aid] || (aid ? String(aid).slice(0, 10) : '—');

    // One row per provider that can serve a model, each carrying its own pin.
    const llamaAid = glob.primary_llama_id || glob.default_llama_id || null;
    const modelRow = (key, mid, state, aid, canSwap) => {
      const dict = glob[(PROV_PIN.find((p) => p.key === key) || {}).pinKey] || {};
      const isPinned = !!(mid && dict[mid]);
      return {
        key, label: PROV_LABEL[key], model: mid || null, resident: !!mid,
        agentId: aid || null, agentHost: hostName(aid), pinned: isPinned,
        pinHost: isPinned ? hostName(dict[mid]) : null, canSwap: !!canSwap,
        detail: mid
          ? PROV_LABEL[key] + ' · ' + (state || '—')
            + (isPinned ? ' · pinned → ' + hostName(dict[mid]) : '')
          : PROV_LABEL[key] + ' · no model loaded',
      };
    };
    const models = [modelRow('llama', resident ? model : null, ls.state, llamaAid, true)];
    if (lm && lm.agent_id) {
      const lmsModel = lmsLoaded.length
        ? (lmsLoaded[0].model || lmsLoaded[0].identifier || null) : null;
      models.push(modelRow('lms', lmsModel,
        lmsLoaded.length ? String(lmsLoaded[0].status || 'loaded').toLowerCase() : null,
        lm.agent_id, false));
    }
    if (vl && Object.keys(vl).length) {
      models.push(modelRow('vllm', vllmRunning ? (vl.model || null) : null,
        vl.state, (d.vllm || {}).agent_id, false));
    }

    // Every pin across every provider, so pins on models that aren't
    // currently resident stay visible (and clearable).
    const loadedByProv = {};
    models.forEach((r) => { loadedByProv[r.key] = r.model; });
    const allPins = [];
    PROV_PIN.forEach((p) => {
      const dict = glob[p.pinKey] || {};
      Object.keys(dict).forEach((mid) => allPins.push({
        provider: p.key, label: p.label, model: mid, agentId: dict[mid],
        host: hostName(dict[mid]), resident: loadedByProv[p.key] === mid,
      }));
    });

    const st = (ap || {}).state || {};
    const entries = st.entries || [];
    const estat = (ap || {}).entry_status || {};
    const proposals = ((ap || {}).proposals || []).length;
    let placedTotal = 0, wantTotal = 0;
    const apEntries = entries.map((e) => {
      const s = estat[e.model + '/' + e.provider] || {};
      const placed = num(s.placed) == null ? 0 : s.placed;
      const want = num(s.want) == null ? (num(e.min_replicas) || 1) : s.want;
      placedTotal += placed; wantTotal += want;
      const reps = e.max_replicas > e.min_replicas
        ? e.min_replicas + '–' + e.max_replicas + '×' : (e.min_replicas || 1) + '×';
      return {
        model: e.model,
        detail: [PROV_LABEL[e.provider] || e.provider,
          e.placement === 'auto' ? 'auto place' : 'host ' + hostName(e.placement),
          e.failover === 'auto' ? 'hands-off' : 'semi-auto', reps,
        ].join(' · ') + (s.blocked ? ' · ' + s.blocked : ''),
        right: placed + '/' + want,
        rightSub: 'placed',
        warn: !!s.blocked || placed < want,
      };
    });
    const autopilot = ap ? {
      on: st.enabled === true,
      detail: entries.length
        ? entries.length + ' model' + (entries.length === 1 ? '' : 's') + ' managed · '
          + placedTotal + '/' + wantTotal + ' placed'
        : 'no models declared',
      entries: apEntries,
      settings: [
        { name: 'Proposals waiting', detail: proposals
          ? proposals + ' pending — apply from the full dashboard' : 'none' },
        { name: 'Last reconcile', detail: num(ap.last_plan_ts)
          ? age(ap.last_plan_ts, now) : 'not run yet' },
      ],
    } : null;

    return {
      gated, services, models, pins: allPins,
      model: {
        name: model, resident, pinned,
        detail: resident
          ? 'llama.cpp · ' + (ls.state || '—') + (pinned ? ' · pinned' : '')
          : 'no model loaded',
      },
      autopilot,
      agentsKnown: !!ag,
      primaryLlamaId: llamaAid,
    };
  }

  return { glance, trends, alerts, alertRow, admin, actions, audit, age, hbAge,
    tsSeconds, clockAt, _sevClass: sevClass };
});
