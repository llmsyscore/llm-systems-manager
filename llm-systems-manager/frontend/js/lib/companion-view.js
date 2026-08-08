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
    let t = null;
    if (typeof ts === 'number' && isFinite(ts)) t = ts > 1e12 ? ts / 1000 : ts;
    else if (typeof ts === 'string' && ts) {
      const p = Date.parse(ts);
      if (!Number.isNaN(p)) t = p / 1000;
    }
    if (t == null) return null;
    const now = nowSec == null ? Date.now() / 1000 : nowSec;
    return Math.max(0, now - t);
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

  // ── Glance ──────────────────────────────────────────────────────────────
  function glance(d) {
    d = d || {};
    const m = d.metrics || {};
    const lm = m.llama || {};
    const ls = d.llama || {};
    const lms = d.lms || {};
    const vllm = (d.vllm || {}).vllm || {};
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

    const ctxK = round((lm.n_tokens_max || 0) / 1024);
    const gpuBusy = num((lms.mac_power || {}).gpu_busy_pct);
    const providers = [
      {
        status: (llamaAwake && llamaResident) ? 'ok' : 'idle',
        name: 'llama.cpp',
        detail: llamaResident
          ? llamaModel + (ctxK ? ' · ctx ' + ctxK + 'k' : '')
          : (ls.state === 'sleeping' ? 'sleeping' : 'no model'),
        rN: (num(lm.active_slots) != null && num(lm.total_slots) != null)
          ? lm.active_slots + '/' + lm.total_slots : '—',
        rUnit: 'slots',
      },
      {
        status: lmsModel ? 'ok' : 'idle',
        name: 'LM Studio',
        detail: lmsModel
          ? ((lms.system || {}).host || (lms.hardware || {}).name || lms.agent_id || 'lms')
            + ' · ' + lmsModel
          : 'no model',
        rN: lmsLoaded.length ? String(lmsLoaded.length)
          : (gpuBusy != null ? Math.round(gpuBusy) + '%' : '—'),
        rUnit: lmsLoaded.length
          ? (lmsLoaded.length === 1 ? 'model' : 'models') : 'gpu busy',
      },
      {
        status: vllmRunning ? 'ok' : 'idle',
        name: 'vLLM',
        detail: vllmRunning ? (vllm.model || 'running') : 'unit stopped',
        rN: vllmRunning && num(vllm.requests_running) != null
          ? String(vllm.requests_running) : '—',
        rUnit: vllmRunning ? 'running' : 'idle',
      },
    ];

    const temp = num(gpu.temperature_c);
    const vramPct = num(gpu.vram_usage_percent);
    const vramUsedGb = num(gpu.vram_used_mb) != null ? gpu.vram_used_mb / 1024 : null;
    const vramTotalGb = (vramUsedGb != null && vramPct) ? vramUsedGb / (vramPct / 100) : null;
    const psu = ((m.liquidctl || {}).psu || {})['Estimated input power'];
    const watts = psu ? num(psu.value) : num(gpu.power_watts);
    const enT = en.totals || {};

    const tiles = [
      { v: round(temp) != null ? String(round(temp)) : '—', unit: '°C',
        k: 'GPU temperature', meter: clampPct(temp), hot: num(temp) != null && temp >= 85 },
      { v: vramUsedGb != null ? vramUsedGb.toFixed(1) : '—',
        unit: vramTotalGb != null ? ' / ' + Math.round(vramTotalGb) + ' GB' : ' GB',
        k: 'VRAM', meter: clampPct(vramPct), hot: num(vramPct) != null && vramPct >= 85 },
      { v: round(watts) != null ? String(round(watts)) : '—', unit: 'W',
        k: 'Input power', sub: psu ? 'PSU · liquidctl' : 'GPU' },
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
      tiles,
    };
  }

  // ── Alerts ──────────────────────────────────────────────────────────────
  const SEV_GLYPH = { crit: '!', warn: '▲', ok: '✓' };

  function sevClass(severity) {
    const s = String(severity || '').toLowerCase();
    if (s === 'critical' || s === 'crit') return 'crit';
    if (s === 'warning' || s === 'warn') return 'warn';
    return 'warn';
  }

  function alertRow(a, nowSec) {
    const resolved = a.status === 'closed' || a.status === 'ignored'
      || a.status === 'exception' || !!a.closed_at;
    const info = a.category === 'info' || a.severity === 'info';
    const sev = (resolved || info) ? 'ok' : sevClass(a.severity);
    let word;
    if (resolved) word = 'resolved';
    else if (info) word = 'info';
    else word = String(a.severity || 'warning').toLowerCase()
      + (a.status === 'acknowledged' ? ' · acked' : ' · firing');
    const host = a.source_host || '—';
    const path = [a.metric_source, a.metric_name].filter(Boolean).join('/') || 'event';
    return {
      id: a.alert_id,
      sev, glyph: SEV_GLYPH[sev], word,
      msg: a.message || (a.rule_name || path),
      meta: path + ' · ' + host + ' · ' + age(a.created_at, nowSec),
      // Resolved/info rows never offer Ack, whatever their status field says.
      ackable: a.status === 'active' && !(resolved || info),
      acked: a.status === 'acknowledged',
      resolved: resolved || info,
    };
  }

  function alerts(list, nowSec) {
    const rows = (list || []).map((a) => alertRow(a, nowSec));
    const firing = rows.filter((r) => !r.resolved);
    const earlier = rows.filter((r) => r.resolved);
    return {
      firing, earlier,
      counts: {
        // Badge = unread: firing and not yet acknowledged.
        badge: firing.filter((r) => !r.acked).length,
        critical: firing.filter((r) => r.sev === 'crit').length,
        warning: firing.filter((r) => r.sev === 'warn').length,
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

    const agents = (d.agents || []).map((a) => {
      const live = a.liveness === 'live';
      const pending = a.liveness === 'pending' || a.status === 'pending';
      // /api/agents has no tls flag; agents serve TLS on 8082 so bind_url is https.
      const tls = /^https/i.test(String(a.bind_url || ''));
      const hb = live ? hbAge(a.last_heartbeat, d.now) : null;
      return {
        status: live ? 'ok' : 'idle',
        name: a.hostname || (a.agent_id || '').slice(0, 10) || '?',
        detail: a.is_host_agent ? 'local · manager host'
          : ('agent ' + (a.version || '?') + (tls ? ' · TLS' : '')),
        right: pending ? 'pending' : (live ? 'live' : (a.liveness || 'down')),
        rightSub: hb ? 'hb ' + hb : '',
        warn: pending,
      };
    });

    const backupLast = backup.last || {};
    const rows = [
      { name: 'Authentication',
        detail: (auth.mode || 'session') + ' mode'
          + (auth.current_user ? ' · ' + auth.current_user : '') },
      { name: 'InfluxDB', ok: influx.state === 'connected' || influx.ok === true,
        detail: (influx.state || (influx.ok ? 'connected' : 'unknown'))
          + (influx.via ? ' · ' + influx.via : '') },
      { name: 'Backups',
        detail: backup.enabled === false ? 'disabled'
          : (backupLast.ok === false ? 'last failed'
            : ('last ' + age(backupLast.ts || backupLast.mtime, d.now) + ' · '
               + (backup.keep_last != null ? backup.keep_last + ' kept' : 'ok'))) },
      // AE service.tls is a dict {enabled,active,error}; active === serving HTTPS.
      { name: 'Alarm engine', ok: ae.ok === true,
        detail: (ae.ok ? 'reachable' : 'unreachable')
          + ((ae.tls && ae.tls.active) ? ' · TLS' : '')
          + (num(ae.latency_ms) != null ? ' · ' + Math.round(ae.latency_ms) + 'ms' : '') },
    ];

    const mgr = health.manager || {};
    const uptime = num(mgr.uptime_s);
    const now = d.now == null ? Date.now() / 1000 : d.now;
    return {
      manager: {
        version: d.version || '—',
        uptime: uptime != null ? age(now - uptime, now) : null,
        updateNote: d.updateAvailable ? 'update available' : 'up to date',
      },
      agents, rows,
    };
  }

  // ── Actions (control surface) ───────────────────────────────────────────
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

    const services = [
      { key: 'llama', name: 'llama.cpp server',
        status: llamaUp ? 'ok' : 'idle',
        detail: llamaUp ? (ls.state || 'up') + (resident ? ' · ' + model : '')
          : (ls.agent_online === false ? 'agent offline' : ls.state || 'unknown'),
        canRestart: ls.agent_online === true },
    ];

    // LM Studio + vLLM rows appear only when a capable agent exists.
    const lm = d.lms;
    if (lm && lm.agent_id) {
      const loaded = ((lm.lms || lm).ps || (lm.ps || [])).filter(
        (p) => p && !/^(stopped|unloaded)$/i.test(String(p.status || '')));
      services.push({
        key: 'lms', name: 'LM Studio',
        status: (lm.agent_online === true && loaded.length) ? 'ok' : 'idle',
        detail: lm.agent_online === true
          ? (loaded.length
            ? (loaded[0].model || loaded[0].identifier || 'model loaded')
            : 'no model loaded')
          : 'agent offline',
        canRestart: lm.agent_online === true });
    }
    const vl = (d.vllm || {}).vllm;
    if (vl && Object.keys(vl).length) {
      const running = String(vl.state || '') === 'running';
      services.push({
        key: 'vllm', name: 'vLLM',
        status: running ? 'ok' : 'idle',
        detail: running ? (vl.model || 'running') : (vl.state || 'unit stopped'),
        canRestart: (d.vllm || {}).agent_online === true });
    }

    services.push(
      { key: 'manager', name: 'Manager', status: 'ok',
        detail: (d.version || '—')
          + (up != null ? ' · up ' + age(now - up, now).replace(' ago', '') : ''),
        canRestart: !!health },
      // AE restart gates on health.ae_local/containerized, not reachability.
      // Detail mirrors the Manager row: version · uptime (from AE /health).
      { key: 'alarm_engine', name: 'Alarm engine',
        status: aeSvc.ok === true ? 'ok' : 'idle',
        detail: !health ? 'status needs admin'
          : (aeSvc.ok
            ? (aeSvc.version || 'reachable')
              + (num(aeSvc.uptime_s) != null
                ? ' · up ' + age(now - aeSvc.uptime_s, now).replace(' ago', '') : '')
            : 'unreachable'),
        canRestart: !!health && (health.ae_local === true || health.containerized === true) });

    const st = (ap || {}).state || {};
    const entries = st.entries || [];
    const autopilot = ap ? {
      on: st.enabled === true,
      detail: entries.length
        ? entries.length + ' model' + (entries.length === 1 ? '' : 's') + ' managed'
        : 'no models declared',
    } : null;

    const pending = ((ag || {}).agents || [])
      .filter((a) => a.status === 'pending' || a.liveness === 'pending')
      .map((a) => ({
        id: a.agent_id,
        name: a.hostname || (a.agent_id || '').slice(0, 10) || '?',
        detail: 'registered ' + age(a.first_seen, d.now)
          + (a.version ? ' · agent ' + a.version : '') + ' · pending approval',
      }));

    return {
      gated, services,
      model: {
        name: model, resident, pinned,
        detail: resident
          ? 'llama.cpp · ' + (ls.state || '—') + (pinned ? ' · pinned' : '')
          : 'no model loaded',
      },
      autopilot, pending,
      agentsKnown: !!ag,
      primaryLlamaId: glob.primary_llama_id || glob.default_llama_id || null,
    };
  }

  return { glance, alerts, alertRow, admin, actions, age, hbAge, _sevClass: sevClass };
});
