// Demo alarm engine answers: rules, alerts, metrics, notifications; DemoAlarms is shared with the page's WebSocket stand-in.
(function (self) {
  var TAU = 2 * Math.PI;
  var GB = 1073741824;
  var JSON_HEADERS = { 'Content-Type': 'application/json' };

  function hash(str) {
    var h = 2166136261;
    for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0) / 4294967296;
  }
  // 0..1 smooth deterministic wobble per key.
  function wob(key, t) {
    return 0.5 + 0.3 * Math.sin(TAU * t / 89 + hash(key + 'a') * TAU) + 0.2 * Math.sin(TAU * t / 37 + hash(key + 'b') * TAU);
  }
  function round(v, d) { var k = Math.pow(10, d || 0); return Math.round(v * k) / k; }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function naive(ms) { return new Date(ms).toISOString().replace('Z', ''); }
  function zulu(ms) { return new Date(ms).toISOString(); }
  function aware(ms) { return new Date(ms).toISOString().replace('Z', '+00:00'); }

  // ---- rules (the five defaults plus the two behind the open alerts) ----
  function thr(v, unit) { return { value: v, lower: null, upper: v, warning: null, critical: null, unit: unit }; }
  var RULES = [
    { id: '3f6c2a91-7d4e-4b8a-9c15-2e7f0a6b4d01', name: 'CPU temperature warning', desc: 'CPU package temperature exceeds 80.0°C',
      metric: 'cpu_temp_c', sev: 'warning', t: thr(80, '°C'), age: 86400 * 21 },
    { id: '8a1d5e47-2c9b-4f3a-b6e8-5d4c7f2a9e02', name: 'CPU usage warning', desc: 'CPU usage above 95.0%',
      metric: 'cpu_total', sev: 'warning', t: thr(95, '%'), age: 86400 * 21 },
    { id: 'c47b9e12-5a3f-4d6c-8e21-7b9a4c1f3d03', name: 'GPU temperature critical', desc: 'GPU temperature exceeds 85.0°C',
      metric: 'gpu_temperature_c', sev: 'critical', t: thr(85, '°C'), age: 86400 * 21 },
    { id: 'e2f8a3b6-9d1c-4e7f-a5b4-3c6d8e2f1a04', name: 'GPU VRAM warning', desc: 'GPU memory use above 95.0%',
      metric: 'gpu_vram_usage_percent', sev: 'warning', t: thr(95, '%'), age: 86400 * 21 },
    { id: '5b9c4d2e-1f7a-4a8b-9d36-8e1f5a7c2b05', name: 'RAM usage warning', desc: 'RAM usage above 90.0%',
      metric: 'ram_percent', sev: 'warning', t: thr(90, '%'), age: 86400 * 21 },
    { id: '9d3e7f1a-6b2c-4c5d-8a47-1f9b3e6d4c06', name: 'GPU temperature warning', desc: 'GPU temperature exceeds 80.0°C',
      metric: 'gpu_temperature_c', sev: 'warning', t: thr(80, '°C'), age: 86400 * 9 },
    { id: '1a7f3c5e-8d4b-4e9a-b2c6-6d3a1f8e7b07', name: 'Swap in use', desc: 'Swap use above 5.0%',
      metric: 'swap_percent', sev: 'info', t: thr(5, '%'), age: 86400 * 6 }
  ];

  function ruleById(id) {
    for (var i = 0; i < RULES.length; i++) if (RULES[i].id === id) return RULES[i];
    return null;
  }

  // GPU temperature at t, or at the latest moment in the last 4 h it was at least min (the alert's last breach).
  function hotTemp(id, min, t) {
    var S = self.DemoSeries;
    if (!S) return min;
    for (var x = t; x > t - 4 * 3600; x -= 15) {
      var v = S.value(id, 'temp_c', x);
      if (v >= min) return v;
    }
    return min;
  }

  // ---- alerts: two open (forge GPU temperature, quill swap) and four closed ones ----
  var ALERTS = [
    { id: '7c2e9a14-3b6d-4f8e-a1c5-9d4b2e7f3a11', rule: '9d3e7f1a-6b2c-4c5d-8a47-1f9b3e6d4c06', host: 'forge.example',
      open: true, age: 1380, value: function (t) { return round(hotTemp('forge', 80.2, t), 1); } },
    { id: '2f8b4d61-9a3c-4e7b-b5d2-6c1e8a4f9b12', rule: '1a7f3c5e-8d4b-4e9a-b2c6-6d3a1f8e7b07', host: 'quill.example',
      open: true, age: 4920, value: function (t) { return round(6.1 + 1.2 * clamp(wob('quill-swap', t), 0, 1), 1); } },
    { id: 'a4d1f7c3-6e2b-4b9d-8c53-1e7a9d2f4c13', rule: 'c47b9e12-5a3f-4d6c-8e21-7b9a4c1f3d03', host: 'atlas.example',
      age: 7400, dur: 540, peak: 86.2, cleared: 79.4, how: 'auto' },
    { id: '6e9c2b85-1d4f-4a7e-9b36-4f2d8c1a6e14', rule: '5b9c4d2e-1f7a-4a8b-9d36-8e1f5a7c2b05', host: 'studio.example',
      age: 18600, dur: 1260, peak: 91.8, cleared: 84.1, how: 'auto' },
    { id: 'd3b7a5f2-4c8e-4d1b-a6f9-2b5e7d3c8a15', rule: '3f6c2a91-7d4e-4b8a-9c15-2e7f0a6b4d01', host: 'forge.example',
      age: 33000, dur: 2700, peak: 81.5, cleared: 74.0, how: 'manual', ack: 600 },
    { id: 'b8f1e4a7-2d5c-4f3e-8b19-7a4c2f6d1e16', rule: 'e2f8a3b6-9d1c-4e7f-a5b4-3c6d8e2f1a04', host: 'atlas.example',
      age: 71000, dur: 900, peak: 96.4, cleared: 88.7, how: 'auto' }
  ];

  function message(r, host, v) {
    return r.name + ' on ' + host + ': ' + r.metric + ' = ' + v + ' (threshold ' + r.t.value + ')';
  }

  function alertAt(a, nowMs) {
    var t = Math.floor(nowMs / 1000);
    var r = ruleById(a.rule);
    var created = nowMs - a.age * 1000;
    var value = a.open ? a.value(t) : a.peak;
    var closed = a.open ? null : created + a.dur * 1000;
    return {
      alert_id: a.id, rule_id: r.id, rule_name: r.name, metric_source: 'system', metric_name: r.metric,
      current_value: value, threshold_value: r.t.value, severity: r.sev,
      status: a.open ? 'active' : 'closed', message: message(r, a.host, value),
      notification_channel_ids: [],
      created_at: naive(created),
      last_evaluated_at: naive(a.open ? nowMs - (t % 15) * 1000 : closed),
      trigger_count: a.open ? 1 + Math.floor(a.age / 15) : Math.floor(a.dur / 15),
      acknowledged_at: a.ack ? naive(created + a.ack * 1000) : null,
      closed_at: closed == null ? null : naive(closed),
      acknowledged_by: a.ack ? 'alex' : null,
      exception_details: null, source_host: a.host, incident_id: a.id,
      resolution_reason: a.open ? null : a.how,
      resolved_value: a.open ? null : a.cleared,
      ignored_until: null
    };
  }

  function alerts(nowMs) { return ALERTS.map(function (a) { return alertAt(a, nowMs); }); }
  function openAlerts(nowMs) { return alerts(nowMs).filter(function (a) { return a.status === 'active'; }); }

  // Toast title, body and severity as the engine's _alert_headline builds them.
  function headline(a) {
    var parts = [];
    if (a.source_host) parts.push(a.source_host);
    if (a.metric_source) parts.push(a.metric_source);
    parts.push(a.metric_name + ' = ' + round(a.current_value, 2));
    return { title: a.rule_name || 'Alert', body: parts.join(' · '), severity: a.severity };
  }

  // Frames for open alert i when it fires ({event, data, ts}): alert_created, plus the toast when asked.
  function firingFrames(nowMs, i, toast) {
    var open = openAlerts(nowMs);
    var a = open[(i || 0) % open.length];
    var h = headline(a);
    var ts = aware(nowMs);
    var created = {}, k;
    for (k in a) created[k] = a[k];
    created.incident_size = 1;
    var out = [{ event: 'alert_created', data: created, ts: ts }];
    if (!toast) return out;
    return out.concat([
      { event: 'notification', ts: ts, data: {
        action: 'toast', title: h.title, body: h.body, severity: h.severity, alert_id: a.alert_id,
        alert_status: a.status, category: 'alert', sticky: false, dismiss_seconds: 10,
        source_host: a.source_host, metric_source: a.metric_source, metric_name: a.metric_name,
        incident_id: a.incident_id, incident_size: 1 } }
    ]);
  }

  function rules(nowMs) {
    var t = Math.floor(nowMs / 1000);
    var open = openAlerts(nowMs);
    return RULES.map(function (r) {
      var last = null;
      ALERTS.forEach(function (a) {
        if (a.rule === r.id && (last === null || a.age < last)) last = a.age;
      });
      var firing = open.some(function (a) { return a.rule_id === r.id; });
      return {
        rule_id: r.id, name: r.name, description: r.desc, source_host: null,
        metric_source: 'system', metric_name: r.metric, rule_type: 'threshold_above',
        config: { threshold: r.t, moving_average: null, percentile: null, rate_of_change: null, z_score: null },
        severity: r.sev, enabled: true, notification_channel_ids: [],
        quiet_hours_start: null, quiet_hours_end: null, auto_resolve_cycles: 2, min_trigger_cycles: 1,
        correlation_group: null,
        created_at: naive(nowMs - r.age * 1000), updated_at: naive(nowMs - r.age * 1000 + 4000),
        last_evaluated_at: naive(nowMs - (t % 15) * 1000),
        last_alert_at: last === null ? null : naive(nowMs - last * 1000),
        breaching: firing ? 1 : 0
      };
    });
  }

  self.DemoAlarms = { rules: rules, alerts: alerts, openAlerts: openAlerts, headline: headline, firingFrames: firingFrames };

  var R = self.DemoRouter;
  if (!R) return;
  var F = self.DemoFleet;
  var S = self.DemoSeries;

  // ---- notification channels, policies and deliveries ----
  var CH_TOAST = '4e1a7c3f-9b2d-4f6e-a8c5-3d7b1e9f2a21';
  var CH_EMAIL = '8c5f2a9d-3e7b-4d1c-b6a4-9f2e5c8a1d22';
  var CH_PUSH = 'f2d6b8e4-7a1c-4e3f-9d58-2b4f6a8c3e23';
  function chConfig(type) {
    var c = { toast: null, sms: null, email: null, webhook: null, discord: null, webpush: null };
    if (type === 'toast') c.toast = { enabled: true };
    if (type === 'email') c.email = { enabled: true, to_email: 'alex@mail.example', subject_prefix: '[ALARM]' };
    if (type === 'webpush') c.webpush = { enabled: true, url: '', token: null, verify_tls: true };
    return c;
  }
  function channels(nowMs) {
    return [
      [CH_TOAST, 'Browser toasts', 'Pop-ups on every open dashboard', 'toast', 41, 0],
      [CH_EMAIL, 'On-call email', 'Mail to the on-call inbox', 'email', 12, 1],
      [CH_PUSH, 'Phone', 'Push to the companion app', 'webpush', 7, 0]
    ].map(function (c, i) {
      return { channel_id: c[0], name: c[1], description: c[2], channel_type: c[3], config: chConfig(c[3]),
        enabled: true, rule_ids: [], created_at: naive(nowMs - (86400 * (20 - i * 3)) * 1000),
        last_sent_at: naive(nowMs - (c[3] === 'toast' ? 1380 : 4920) * 1000), send_count: c[4], fail_count: c[5] };
    });
  }
  function configs(nowMs) {
    function cfg(id, name, desc, chs, minSev, count, repeat) {
      return { config_id: id, name: name, description: desc, channels: chs, enabled: true, auto_dismiss: true,
        toast_dismiss_seconds: 10, created_at: naive(nowMs - 86400 * 19 * 1000), last_triggered_at: naive(nowMs - 1380 * 1000),
        trigger_count: count, min_severity: minSev, metric_sources: [], metric_names: [], source_hosts: [], rule_ids: [],
        repeat_interval_minutes: repeat, notify_on_clear: true, min_alarm_count: 1 };
    }
    return [
      cfg('6a3d9f1c-2e8b-4c7a-b5d1-8f4e2a6c9b31', 'Everyday alerts', 'Toasts and email for warnings and above',
        [CH_TOAST, CH_EMAIL], 'warning', 34, 30),
      cfg('c9e5b2a7-4f1d-4b8e-9a63-5d2c8f1e4a32', 'Critical to phone', 'Push critical alerts to the phone',
        [CH_PUSH, CH_TOAST], 'critical', 6, 15)
    ];
  }
  function deliveriesFor(a, nowMs) {
    var h = headline(a);
    var created = Date.parse(a.created_at + 'Z');
    var rows = [[CH_TOAST, 'toast', 'webui', created + 2000]];
    if (a.severity !== 'info') rows.push([CH_EMAIL, 'email', 'alex@mail.example', created + 3000]);
    if (a.severity === 'critical') rows.push([CH_PUSH, 'webpush', 'companion', created + 3500]);
    return rows.filter(function (d) { return d[3] <= nowMs; }).map(function (d, i) {
      return { delivery_id: a.alert_id.slice(0, 30) + ('00000' + i).slice(-6), config_id: null, channel_id: d[0],
        channel_type: d[1], method: 'channel', recipient: d[2], title: h.title, body: h.body,
        severity: a.severity, success: true, error_message: null, delivered_at: aware(d[3]),
        metadata: { alert_id: a.alert_id } };
    });
  }
  function allDeliveries(nowMs) {
    var out = [];
    alerts(nowMs).forEach(function (a) { out = out.concat(deliveriesFor(a, nowMs)); });
    return out.sort(function (x, y) { return x.delivered_at < y.delivered_at ? 1 : -1; });
  }
  function eventsFor(a) {
    var created = Date.parse(a.created_at + 'Z');
    var ev = [{ event: 'created', at: naive(created), actor: null, detail: a.current_value + ' vs ' + a.threshold_value }];
    if (a.acknowledged_at) ev.push({ event: 'acknowledged', at: a.acknowledged_at, actor: 'alex', detail: null });
    if (a.closed_at) {
      ev.push({ event: 'closed', at: a.closed_at, actor: a.acknowledged_by,
        detail: a.resolution_reason + (a.resolved_value != null ? ' @ ' + a.resolved_value : '') });
    }
    return ev;
  }

  // ---- metric catalog: system per machine, services, InfluxDB, manager self-probes ----
  var MGR_HOST = 'atlas.example';
  var SELF = [
    ['ae_health_latency_ms', 3.7], ['rule_eval_cycle_ms', 0.45], ['influx_query_5m_latency_ms', 2.8],
    ['influx_query_24h_latency_ms', 2.5], ['influx_write_latency_ms', 6.4], ['manager_anon_requests_per_min', 2, null],
    ['ae_query_24h_latency_ms', 4.4], ['manager_anon_refused_per_min', 0, null], ['manager_api_latency_ms', 1.7],
    ['manager_history_latency_ms', 1.3], ['ae_ingest_latency_ms', 0.96]
  ];
  var BOOT = Date.UTC(2026, 0, 1) / 1000;

  function gpuHost(m) { return m.gpus.length > 0 && m.os !== 'macos'; }
  function hostId(host) {
    for (var i = 0; i < F.machines.length; i++) if (F.machines[i].hostname === host) return F.machines[i];
    return null;
  }

  function systemValues(m, t) {
    var cpu = S.value(m.id, 'cpu_pct', t);
    var lvl = S.value(m.id, 'tok_s', t) / 140;
    var ramPct = clamp(38 + 0.3 * cpu + 12 * lvl, 0, 96);
    var ramTotal = m.ramGb * GB, diskTotal = m.diskGb * GB;
    var swapUsed = m.id === 'quill' ? 0.52 * GB + 0.1 * GB * clamp(wob('quill-swap', t), 0, 1) : 0.4 * GB;
    var v = {
      cpu_total: round(cpu, 1), cpu_temp_c: round(S.value(m.id, 'temp_c', t) - 6, 1),
      ram_percent: round(ramPct, 1), ram_total_bytes: ramTotal, ram_used_bytes: Math.round(ramTotal * ramPct / 100),
      ram_cached_bytes: Math.round(ramTotal * 0.18), ram_buffers_bytes: Math.round(ramTotal * 0.01),
      swap_percent: round(100 * swapUsed / (8 * GB), 1), swap_used_bytes: Math.round(swapUsed), swap_total_bytes: 8 * GB,
      disk_root_percent: m.diskPct, disk_root_used_bytes: Math.round(diskTotal * m.diskPct / 100), disk_root_total_bytes: diskTotal,
      disk_io_read_bytes_per_sec: Math.round(120000 + 6000000 * lvl), disk_io_write_bytes_per_sec: Math.round(80000 + 1500000 * lvl),
      net_bytes_recv_per_sec: Math.round(45000 + 1400000 * lvl), net_bytes_sent_per_sec: Math.round(30000 + 900000 * lvl)
    };
    if (gpuHost(m)) {
      var total = 0;
      m.gpus.forEach(function (g) { total += g.vramGb; });
      v.gpu_temperature_c = round(S.value(m.id, 'temp_c', t), 1);
      v.gpu_gpu_util_percent = round(S.value(m.id, 'gpu_util', t), 1);
      v.gpu_vram_usage_percent = round(100 * S.value(m.id, 'vram_used_gb', t) / total, 1);
      v.gpu_power_watts = round(S.value(m.id, 'power_w', t) * 0.8, 1);
    }
    return v;
  }

  function services(m) {
    var out = ['llm-systems-agent'];
    if (m.providers.indexOf('llama') >= 0) out.push('llama-server');
    if (m.providers.indexOf('vllm') >= 0) out.push('vllm');
    if (m.providers.indexOf('lms') >= 0) out.push('lmstudio');
    if (m.hostname === MGR_HOST) out = ['manager', 'alarm-engine', 'influxdb'].concat(out);
    return out;
  }
  var RSS = { manager: 310, 'alarm-engine': 240, influxdb: 880, 'llm-systems-agent': 72, 'llama-server': 9600, vllm: 14200, lmstudio: 2100 };
  function processValues(m, t) {
    var v = {};
    services(m).forEach(function (svc, i) {
      v[svc + '_running'] = 1;
      v[svc + '_count'] = 1;
      v[svc + '_pid'] = 1000 + Math.floor(hash(m.id + svc) * 60000);
      v[svc + '_rss_mb'] = round(RSS[svc] * (0.95 + 0.1 * clamp(wob(m.id + svc, t), 0, 1)), 1);
      v[svc + '_uptime_s'] = Math.max(0, t - BOOT - i * 40);
    });
    return v;
  }
  function influxValues(t) {
    return {
      up: 1, ping_ms: round(0.9 + 0.8 * wob('ping', t), 2), query_ms: round(4 + 5 * wob('query', t), 2),
      write_ms: round(2.5 + 3 * wob('write', t), 2), write_ok_rate: 1,
      bytes_on_disk: Math.round(2.1 * GB + (t - BOOT) * 900), cardinality_metrics: 1840
    };
  }
  function selfValue(name, base, t) {
    if (name === 'manager_anon_refused_per_min') return 0;
    if (name === 'manager_anon_requests_per_min') return Math.round(3 * clamp(wob(name, t), 0, 1));
    return base * (0.75 + 0.5 * clamp(wob(name, t), 0, 1));
  }

  // Every (source, host, metric) series as {source, host, name, unit, at(t)}.
  function catalog() {
    var out = [];
    SELF.forEach(function (s) {
      out.push({ source: 'manager_self_monitor', host: MGR_HOST, name: s[0], unit: s.length > 2 ? null : 'ms',
        at: function (t) { return selfValue(s[0], s[1], t); } });
    });
    Object.keys(influxValues(0)).forEach(function (k) {
      out.push({ source: 'influxdb', host: MGR_HOST, name: k, unit: k === 'bytes_on_disk' ? 'bytes' : null,
        at: function (t) { return influxValues(t)[k]; } });
    });
    F.machines.forEach(function (m) {
      Object.keys(processValues(m, 0)).forEach(function (k) {
        out.push({ source: 'processes', host: m.hostname, name: k, unit: null, at: function (t) { return processValues(m, t)[k]; } });
      });
      Object.keys(systemValues(m, 0)).forEach(function (k) {
        out.push({ source: 'system', host: m.hostname, name: k, unit: null, at: function (t) { return systemValues(m, t)[k]; } });
      });
    });
    return out;
  }

  // Hostname filter from ?hostname= or ?agent= (the manager agent sits on the manager host).
  function hostFilter(q) {
    if (q.get('hostname')) return q.get('hostname');
    var a = q.get('agent');
    if (!a) return null;
    var m = F.byId(a);
    return m ? m.hostname : MGR_HOST;
  }
  function series(q, source, name) {
    var host = hostFilter(q);
    return catalog().filter(function (s) {
      return (!source || s.source === source) && (!name || s.name === name) && (!host || s.host === host);
    });
  }

  function metricList(ctx) {
    var t = Math.floor(ctx.now / 1000);
    var limit = Math.max(1, parseInt(ctx.query.get('limit'), 10) || 1000);
    return series(ctx.query, ctx.query.get('source'), null).slice(0, limit).map(function (s) {
      return { source: s.source, metric_name: s.name, hostname: s.host, unit: s.unit,
        latest_value: s.at(t - (t % 5)), latest_timestamp: zulu((t - (t % 5)) * 1000) };
    });
  }

  // History bounded to 300 points in total over the requested window.
  function metricHistory(ctx) {
    var q = ctx.query;
    var src = series(q, ctx.params.source, ctx.params.metric);
    if (!src.length) return [];
    var to = Math.floor(ctx.now / 1000);
    var mins = clamp(parseInt(q.get('since_minutes'), 10) || 60, 1, 43200);
    var cap = 300;
    [q.get('max_points'), q.get('limit')].forEach(function (v) {
      var n = parseInt(v, 10);
      if (n > 0 && n < cap) cap = n;
    });
    var per = Math.max(1, Math.floor(cap / src.length));
    var from = to - mins * 60;
    var step = Math.max(5, Math.ceil((to - from) / per));
    var out = [];
    src.forEach(function (s) {
      var first = Math.ceil((to - (per - 1) * step) / step) * step;
      if (first < from) first = Math.ceil(from / step) * step;
      for (var t = first; t <= to && out.length < cap; t += step) {
        out.push({ metric_id: s.host.split('.')[0] + '-' + s.name + '-' + t, source: s.source, metric_name: s.name,
          value: s.at(t), unit: s.unit, timestamp: aware(t * 1000), hostname: s.host });
      }
    });
    return out;
  }

  function metricSummary(ctx) {
    var src = series(ctx.query, ctx.params.source, ctx.params.metric)[0];
    if (!src) return notFound('No data for ' + ctx.params.source + '/' + ctx.params.metric);
    var to = Math.floor(ctx.now / 1000);
    var win = clamp(parseInt(ctx.query.get('window_minutes'), 10) || 60, 1, 10080) * 60;
    var vals = [];
    var step = Math.max(5, Math.ceil(win / 120));
    for (var t = to - win; t <= to; t += step) vals.push(src.at(t));
    var sorted = vals.slice().sort(function (a, b) { return a - b; });
    var avg = vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
    var sd = Math.sqrt(vals.reduce(function (a, b) { return a + (b - avg) * (b - avg); }, 0) / vals.length);
    function pct(p) { return sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))]; }
    return { source: src.source, metric_name: src.name, unit: src.unit, current_value: src.at(to),
      min_value: sorted[0], max_value: sorted[sorted.length - 1], avg_value: avg, std_dev: sd,
      p90: pct(0.9), p95: pct(0.95), p99: pct(0.99), data_points: Math.round(win / 5), last_updated: naive(to * 1000) };
  }

  function notFound(detail) {
    return new Response(JSON.stringify({ detail: detail }), { status: 404, headers: JSON_HEADERS });
  }
  function csv(name, rows) {
    var body = rows.map(function (r) {
      return r.map(function (c) { var s = c == null ? '' : String(c); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }).join(',');
    }).join('\n') + '\n';
    return new Response(body, { status: 200, headers: {
      'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename="' + name + '"' } });
  }

  function listAlerts(ctx) {
    var q = ctx.query;
    var list = alerts(ctx.now);
    var status = q.get('status');
    var includeClosed = q.get('include_closed') === 'true';
    list = list.filter(function (a) {
      if (status) return a.status === status;
      if (q.get('only_active') === 'true') return a.status === 'active';
      return includeClosed || a.status !== 'closed';
    });
    ['severity', 'rule_id', 'metric_name'].forEach(function (k) {
      var v = q.get(k);
      if (v) list = list.filter(function (a) { return a[k] === v; });
    });
    var limit = clamp(parseInt(q.get('limit'), 10) || 100, 1, 1000);
    return list.slice(0, limit);
  }
  function findAlert(ctx) {
    var list = alerts(ctx.now);
    for (var i = 0; i < list.length; i++) if (list[i].alert_id === ctx.params.id) return list[i];
    return null;
  }

  R.route('GET /alarm/health', function (ctx) {
    var t = Math.floor(ctx.now / 1000);
    return {
      status: 'ok', version: self.DEMO_RELEASE || 'dev', uptime_s: t - BOOT, auth: 'enforced',
      ingest_points_per_s: round(38 + 14 * wob('ingest', t), 3), influx_writes_per_s: round(0.9 + 0.4 * wob('iw', t), 3),
      active_alerts: openAlerts(ctx.now).length, evaluation_interval_s: 15, restart_pending: false,
      components: { cache: 'active', influxdb: 'connected', influxdb_ping_ms: round(0.9 + 0.8 * wob('ping', t), 2),
        influxdb_version: 'v2.7.11', rule_eval_last_cycle_ms: round(selfValue('rule_eval_cycle_ms', 0.45, t), 3) }
    };
  });

  R.route('GET /api/alarm-ws-ticket', function (ctx) {
    return { ticket: Math.floor(ctx.now / 1000) + 300 + '.demo.' + 'd'.repeat(64), ttl_s: 300 };
  });

  R.route('GET /api/alarm/rules', function (ctx) {
    var limit = parseInt(ctx.query.get('limit'), 10) || 100;
    return rules(ctx.now).slice(0, limit);
  });
  R.route('GET /api/alarm/rules/:id', function (ctx) {
    var list = rules(ctx.now);
    for (var i = 0; i < list.length; i++) {
      if (list[i].rule_id === ctx.params.id) {
        var r = {}, k;
        for (k in list[i]) if (k !== 'breaching') r[k] = list[i][k];
        return r;
      }
    }
    return notFound('Rule not found: ' + ctx.params.id);
  });

  R.route('GET /api/alarm/alerts/export', function (ctx) {
    var cols = ['alert_id', 'rule_name', 'severity', 'status', 'source_host', 'metric_source', 'metric_name',
      'current_value', 'threshold_value', 'created_at', 'closed_at', 'message'];
    return csv('alerts.csv', [cols].concat(alerts(ctx.now).map(function (a) {
      return cols.map(function (c) { return a[c]; });
    })));
  });
  R.route('GET /api/alarm/alerts', listAlerts);
  R.route('GET /api/alarm/alerts/:id/events', function (ctx) {
    var a = findAlert(ctx);
    return { alert_id: ctx.params.id, events: a ? eventsFor(a) : [] };
  });
  R.route('GET /api/alarm/alerts/:id/deliveries', function (ctx) {
    var a = findAlert(ctx);
    return { alert_id: ctx.params.id, deliveries: a ? deliveriesFor(a, ctx.now) : [] };
  });
  R.route('GET /api/alarm/alerts/:id', function (ctx) {
    return findAlert(ctx) || notFound('Alert ' + ctx.params.id + ' not found');
  });

  R.route('GET /api/alarm/metrics/export', function (ctx) {
    var c = { now: ctx.now, query: ctx.query, params: { source: ctx.query.get('source'), metric: ctx.query.get('metric_name') } };
    var rows = metricHistory(c);
    return csv((c.params.source || 'metric') + '_' + (c.params.metric || 'export') + '.csv',
      [['timestamp', 'hostname', 'source', 'metric_name', 'value', 'unit']].concat(rows.map(function (r) {
        return [r.timestamp, r.hostname, r.source, r.metric_name, r.value, r.unit];
      })));
  });
  R.route('GET /api/alarm/metrics', metricList);
  R.route('GET /api/alarm/metrics/:source/:metric/summary', metricSummary);
  R.route('GET /api/alarm/metrics/:source/:metric', metricHistory);

  R.route('GET /api/alarm/dbstats/sqlite', function (ctx) {
    var t = Math.floor(ctx.now / 1000);
    var events = 0;
    alerts(ctx.now).forEach(function (a) { events += eventsFor(a).length; });
    return {
      alarms_db: { size_bytes: 69632, wal_size_bytes: 115392 + (t % 600) * 64, shm_size_bytes: 32768, page_size: 4096,
        page_count: 17, journal_mode: 'wal', cache_size: -2000, query_ms: round(0.003 + 0.004 * wob('aq', t), 3),
        alerts: ALERTS.length, alert_history: events, db: 'ae_alarms.db' },
      settings_db: { size_bytes: 57344, wal_size_bytes: 189552, shm_size_bytes: 32768, page_size: 4096, page_count: 14,
        journal_mode: 'wal', cache_size: -2000, query_ms: round(0.002 + 0.003 * wob('sq', t), 3),
        rules: RULES.length, channels: 3, configs: 2, deliveries: allDeliveries(ctx.now).length, db: 'ae_notif_rules.db' }
    };
  });

  R.route('GET /api/alarm/notifications/channels', function (ctx) { return channels(ctx.now); });
  R.route('GET /api/alarm/notifications/channels/:id', function (ctx) {
    var list = channels(ctx.now);
    for (var i = 0; i < list.length; i++) if (list[i].channel_id === ctx.params.id) return list[i];
    return notFound('Channel not found');
  });
  R.route('GET /api/alarm/notifications/configs', function (ctx) { return configs(ctx.now); });
  R.route('GET /api/alarm/notifications/delivery-history', function (ctx) {
    var list = allDeliveries(ctx.now);
    var type = ctx.query.get('channel_type');
    if (type) list = list.filter(function (d) { return d.channel_type === type; });
    return list.slice(0, clamp(parseInt(ctx.query.get('limit'), 10) || 100, 1, 1000));
  });
})(self);
