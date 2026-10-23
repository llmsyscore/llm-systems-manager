// Demo Admin answers: users, settings, jobs, audit log, backups, system health, tool runs and OpenClaw analytics.
(function (self) {
  var R = self.DemoRouter;
  var F = self.DemoFleet;
  var S = self.DemoSeries;
  var HOUR = 3600;
  var DAY = 86400;
  var JSON_HEADERS = { 'Content-Type': 'application/json' };
  var VERSION = self.DEMO_RELEASE || 'dev';

  function sec(ctx) { return Math.floor(ctx.now / 1000); }
  function round(v, d) { var k = Math.pow(10, d || 0); return Math.round(v * k) / k; }
  function iso(t) { return new Date(t * 1000).toISOString(); }
  function isoTz(t) { return new Date(t * 1000).toISOString().replace('.000Z', '+00:00'); }
  function localStr(t) { return t ? new Date(t * 1000).toISOString().slice(0, 19).replace('T', ' ') : null; }
  function stamp(t) { return new Date(t * 1000).toISOString().replace(/[-:]/g, '').slice(0, 15).replace('T', '-'); }
  function num(q, name, dflt) { var v = Number(q.get(name)); return isFinite(v) && q.get(name) !== null && q.get(name) !== '' ? v : dflt; }
  function release() { return self.DEMO_RELEASE || VERSION; }
  function slug(m) { return m.hostname.replace(/\./g, '-'); }
  function noneAnswer(status, error) { return new Response(JSON.stringify({ ok: false, error: error }), { status: status, headers: JSON_HEADERS }); }

  // ---- identity ----
  R.route('GET /api/admin/auth', function () {
    return { admin_cidrs: ['192.0.2.0/24'], bypass_role: 'admin', current_user: 'alex', default_user: 'alex',
      instant: true, is_default: false, mode: 'required', modes: ['required', 'disabled', 'trusted_cidr', 'auto'],
      ok: true, policy: 'auto' };
  });
  R.route('GET /api/admin/users', function (ctx) {
    var s = sec(ctx);
    var base = { disabled: false, failed_count: 0, lock_minutes_left: null, locked: false };
    return { ok: true, users: [
      Object.assign({ created_at: iso(s - 60 * DAY), last_login: iso(s - 2 * HOUR), password_changed_at: iso(s - 40 * DAY),
        role: 'admin', updated_at: iso(s - 2 * HOUR), username: 'alex' }, base),
      Object.assign({ created_at: iso(s - 30 * DAY), last_login: iso(s - 26 * HOUR), password_changed_at: iso(s - 30 * DAY),
        role: 'operator', updated_at: iso(s - 26 * HOUR), username: 'sam' }, base)
    ] };
  });

  // ---- settings ----
  R.route('GET /api/admin/settings', function () {
    var d = self.DEMO_SETTINGS || {};
    return { ok: true, ae_sync_pending: [], ae_sync_retry_s: 30, restart_pending: [], restart_pending_paths: [],
      entries: d.entries || [], groups: d.groups || [], defaults: d.defaults || {}, values: d.values || {},
      secrets: d.secrets || {}, topology: d.topology || { ae_config_reachable: true, split: false } };
  });
  R.route('GET /api/admin/telemetry', function (ctx) {
    return { ok: true, install_report: false, crash_reports: false, heartbeat: false, pending: false,
      last: { event: 'install', sent_at: sec(ctx) - 20 * DAY, payload: { event: 'install', sent_at: sec(ctx) - 20 * DAY } } };
  });

  // ---- jobs ----
  var KINDS = [['tool_run', 'Tool run'], ['report_card', 'Report Card'], ['autotune_batch', 'Autotune batch'],
    ['tower_timer', 'Tower timer'], ['tower_eval', 'Tower eval'], ['tower_get_model', 'Download Tower model'],
    ['forecast_run', 'Forecast run']];
  // [id, kind, label, status, user, source, created s ago, duration s, message]
  var JOBS = [
    ['demo-job-1', 'autotune_batch', 'Autotune Llama-3.1-8B on atlas.example', 'running', 'alex', 'ui', 900, 0, 'Trying settings 4 of 9'],
    ['demo-job-2', 'tower_timer', 'Watch forge.example GPU temperature', 'queued', 'alex', 'tower', 300, 0, 'Next check in 2 minutes'],
    ['demo-job-3', 'report_card', 'Report card for forge.example', 'done', 'sam', 'ui', 5 * HOUR, 420, 'Finished'],
    ['demo-job-4', 'tool_run', 'Benchmark Llama-3.1-8B on atlas.example', 'done', 'alex', 'ui', 9 * HOUR, 240, 'Finished'],
    ['demo-job-5', 'forecast_run', 'Weekly forecast', 'done', 'alex', 'schedule', 20 * HOUR, 95, 'Finished'],
    ['demo-job-6', 'tool_run', 'Quality check on quill.example', 'cancelled', 'sam', 'ui', 30 * HOUR, 60, 'Cancelled']
  ];
  var LIVE = { queued: 1, running: 1 };

  function jobView(j, s, detail) {
    var created = s - j[6];
    var started = j[3] === 'queued' ? null : created + 10;
    var resolved = LIVE[j[3]] ? null : created + 10 + j[7];
    var nextRun = j[3] === 'queued' ? s + 120 : null;
    var v = { id: j[0], kind: j[1], label: j[2], status: j[3], user: j[4], source: j[5], created: created, not_before: null,
      next_run: nextRun, last_run: started, started: started, resolved: resolved, run_count: j[3] === 'queued' ? 0 : 1,
      attempts: j[3] === 'queued' ? 0 : 1, message: j[8], thread_id: null, exclusive: false,
      kind_title: (KINDS.filter(function (k) { return k[0] === j[1]; })[0] || [0, j[1]])[1],
      created_local: localStr(created), not_before_local: null, next_run_local: localStr(nextRun),
      last_run_local: localStr(started), started_local: localStr(started), resolved_local: localStr(resolved),
      can_cancel: !!LIVE[j[3]], acked: false, can_ack: false };
    if (detail) {
      v.spec = { label: j[2], source: j[5] };
      v.state = LIVE[j[3]] ? { progress: j[3] === 'running' ? 0.44 : 0 } : {};
      v.result = LIVE[j[3]] ? null : { ok: j[3] === 'done' };
      v.audit = [{ ts: isoTz(created), action: 'jobs.submit', actor: j[4], outcome: 'ok' }];
    }
    return v;
  }
  // Live jobs and their counts, shared by the Jobs page and the System Health card.
  function liveSummary(s) {
    var out = { failed_24h: 0, next_due: s + 120, queued: 0, running: 0 };
    JOBS.forEach(function (j) { if (LIVE[j[3]]) out[j[3]]++; });
    return out;
  }
  function healthJobs(s) {
    var summ = liveSummary(s);
    var rows = JOBS.filter(function (j) { return LIVE[j[3]]; }).slice(0, 8).map(function (j) { return jobView(j, s, false); });
    return { failed: [], failed_24h: summ.failed_24h, more: Math.max(0, summ.queued + summ.running - rows.length),
      next_due: summ.next_due, queued: summ.queued, rows: rows, running: summ.running };
  }
  R.route('GET /api/jobs', function (ctx) {
    var s = sec(ctx);
    var status = ctx.query.get('status') || 'live';
    var kind = ctx.query.get('kind');
    var user = ctx.query.get('user');
    var rows = JOBS.filter(function (j) {
      if (status === 'live' ? !LIVE[j[3]] : (status !== 'all' && j[3] !== status)) return false;
      return (!kind || j[1] === kind) && (!user || j[4] === user);
    });
    var limit = Math.max(1, Math.min(200, num(ctx.query, 'limit', 50)));
    var offset = Math.max(0, num(ctx.query, 'offset', 0));
    return { ok: true, jobs: rows.slice(offset, offset + limit).map(function (j) { return jobView(j, s, false); }),
      summary: liveSummary(s), total: rows.length,
      kinds: KINDS.map(function (k) { return { name: k[0], title: k[1] }; }), users: ['alex', 'sam'] };
  });
  R.route('GET /api/jobs/:id', function (ctx) {
    var j = JOBS.filter(function (x) { return x[0] === ctx.params.id; })[0];
    return j ? { ok: true, job: jobView(j, sec(ctx), true) } : noneAnswer(404, 'not found');
  });

  // ---- audit log ----
  // [minutes ago, actor, group, event, action, label, method, path, target, detail]
  var AUDIT = [
    [3, 'alex', 'user', 'auth.login', 'auth.login', 'Signed in', 'POST', '/login', 'alex', null],
    [22, 'alex', 'service', 'alarm.actions', 'alarm.acknowledge', 'Acknowledged an alarm', 'POST', '/api/alarm/alerts/acknowledge', 'forge.example', null],
    [55, 'sam', 'model', 'llama.model', 'llama.load', 'Loaded a model', 'POST', '/api/llama/load', 'forge.example', { model: 'Gemma-2-9B-it-Q6_K' }],
    [95, 'sam', 'user', 'auth.login', 'auth.login', 'Signed in', 'POST', '/login', 'sam', null],
    [150, 'alex', 'config', 'config.settings', 'config.settings', 'Changed manager settings', 'PUT', '/api/admin/settings', 'manager.polling', { changes: 1 }],
    [260, 'alex', 'tool', 'reportcard', 'reportcard.run', 'Started a report card', 'POST', '/api/reportcard/run', 'forge.example', null],
    [410, 'alex', 'agent', 'agent.lifecycle', 'agent.approve', 'Approved an agent', 'POST', '/api/agents/approve', 'quill.example', { agent: 'quill.example' }],
    [600, 'alex', 'service', 'service.restart', 'service.restart', 'Restarted a service', 'POST', '/api/admin/service/alarm-engine/restart', 'alarm-engine', null],
    [900, 'alex', 'jobs', 'jobs', 'jobs.submit', 'Submitted a job', 'POST', '/api/jobs', 'demo-job-5', { kind: 'forecast_run' }],
    [1300, 'sam', 'model', 'llama.model', 'llama.unload', 'Unloaded a model', 'POST', '/api/llama/unload', 'quill.example', null],
    [1800, 'alex', 'user', 'user.manage', 'user.create', 'Created a user', 'POST', '/api/admin/users', 'sam', { role: 'operator' }],
    [2600, 'alex', 'agent', 'agent.lifecycle', 'agent.approve', 'Approved an agent', 'POST', '/api/agents/approve', 'studio.example', { agent: 'studio.example' }],
    [2700, 'alex', 'agent', 'agent.lifecycle', 'agent.approve', 'Approved an agent', 'POST', '/api/agents/approve', 'forge.example', { agent: 'forge.example' }],
    [2800, 'alex', 'agent', 'agent.lifecycle', 'agent.approve', 'Approved an agent', 'POST', '/api/agents/approve', 'atlas.example', { agent: 'atlas.example' }]
  ];
  function auditRows(s) {
    return AUDIT.map(function (a, i) {
      return { action: a[4], actor: a[1], auth: 'session', detail: a[9], event: a[3], group: a[2], id: AUDIT.length - i,
        ip: '192.0.2.4', label: a[5], method: a[6], outcome: 'ok', path: a[7], role: a[1] === 'alex' ? 'admin' : 'operator',
        status: 200, target: a[8], ts: isoTz(s - a[0] * 60) };
    });
  }
  R.route('GET /api/admin/audit-log/stats', function (ctx) {
    var s = sec(ctx);
    return { actors: ['alex', 'sam'], ok: true, oldest: isoTz(s - AUDIT[AUDIT.length - 1][0] * 60), page_size: 25,
      purge: { removed: 0, ts: isoTz(s - 6 * HOUR) }, retention_days: 60, total: AUDIT.length };
  });
  R.route('GET /api/admin/audit-log/events', function () {
    var G = [
      ['agent', 'Agents', [['agent.lifecycle', 'Approve / disable / delete', 1], ['agent.control', 'Restart / self-update / redeploy', 1], ['agent.config', 'Config file edits', 1]]],
      ['user', 'Users & access', [['user.manage', 'Create / modify / delete / unlock', 1], ['account.password', 'Own password change', 1], ['auth.login', 'Login success / failure', 1], ['auth.logout', 'Logout', 0]]],
      ['config', 'Configuration', [['config.settings', 'Manager settings', 1], ['config.pins', 'Model pins', 1], ['backup', 'Backup export / import', 1]]],
      ['model', 'Models & routing', [['llama.model', 'llama load / unload', 1], ['lms.model', 'LM Studio load / unload', 1], ['model.config', 'Model config · profiles · aliases', 1]]],
      ['service', 'Services', [['service.restart', 'Service restart', 1], ['alarm.actions', 'Alarm acknowledge · rules', 1]]],
      ['tool', 'Tools & terminal', [['terminal.open', 'Terminal session opened', 1], ['reportcard', 'Report card run / delete', 1], ['tools.run', 'Benchmark / autotune run', 0]]],
      ['tower', 'Tower', [['tower.config', 'Model pin / thread delete', 1], ['tower.action', 'Action approved / denied / playbook applied', 1]]],
      ['jobs', 'Jobs', [['jobs', 'Job submitted / cancelled / failed', 1]]]
    ];
    return { ok: true, groups: G.map(function (g) {
      return { key: g[0], title: g[1], events: g[2].map(function (e) { return { key: e[0], label: e[1], default_on: !!e[2], enabled: !!e[2] }; }) };
    }), config: { retention_days: 60, page_size: 25, save_automated: false, automated_actors: [] } };
  });
  R.route('GET /api/admin/audit-log', function (ctx) {
    var s = sec(ctx);
    var q = (ctx.query.get('q') || '').toLowerCase();
    var group = ctx.query.get('group');
    var actor = ctx.query.get('actor');
    var outcome = ctx.query.get('outcome');
    var hours = num(ctx.query, 'since_hours', 0);
    var rows = auditRows(s).filter(function (e) {
      if (group && e.group !== group) return false;
      if (actor && e.actor !== actor) return false;
      if (outcome && e.outcome !== outcome) return false;
      if (hours && Date.parse(e.ts) < (s - hours * HOUR) * 1000) return false;
      return !q || (e.label + ' ' + e.target + ' ' + e.actor + ' ' + e.path).toLowerCase().indexOf(q) >= 0;
    });
    if (ctx.query.get('sort') === 'ts' && ctx.query.get('dir') === 'asc') rows.reverse();
    var limit = Math.max(1, Math.min(200, num(ctx.query, 'limit', 25)));
    var offset = Math.max(0, num(ctx.query, 'offset', 0));
    return { ok: true, entries: rows.slice(offset, offset + limit), page_size: 25, total: rows.length };
  });

  // ---- backups ----
  function lastBackupS(s) {
    var t = s - (s % DAY) + 3 * HOUR;
    return t > s ? t - DAY : t;
  }
  R.route('GET /api/admin/backup-status', function (ctx) {
    var last = lastBackupS(sec(ctx));
    var host = slug(F.byId('atlas'));
    var backups = [];
    for (var i = 0; i < 7; i++) {
      var t = last - i * DAY;
      backups.push({ bytes: 10682 + i * 31, component: 'manager', file: 'lsm-auto-manager-' + host + '-' + stamp(t) + '.lsmenc', mirrored: null, mtime: t, run: stamp(t) });
      backups.push({ bytes: 5266 + i * 12, component: 'alarm_engine', file: 'lsm-auto-ae-' + host + '-' + stamp(t) + '.lsmenc', mirrored: null, mtime: t, run: stamp(t) });
    }
    return { backups: backups, disabled_reason: null, enabled: true, encrypted: false, folder_bytes: 7 * 15948,
      interval_hours: 24, keep_last: 7,
      last: { bytes: 10682, components: {
        alarm_engine: { bytes: 5266, error: null, file: backups[1].file, ok: true, remedy: null, skipped: null },
        manager: { bytes: 10682, error: null, file: backups[0].file, files: 10, ok: true } },
        duration_s: 0.4, encrypted: false, error: null, file: backups[0].file, files: 10, ok: true, partial: false, ts: last },
      last_export: { alarm_engine: null, manager: null }, mirror_dir: '', next_due_ts: last + DAY, not_covered: {},
      ok: true, scheduler_running: true };
  });
  R.route('GET /api/admin/backup-archive/:name', function () { return noneAnswer(403, 'Not available in the demo'); });
  R.route('GET /api/admin/stores/leftovers', function (ctx) {
    return { ok: true, at: sec(ctx) - 5 * 60, removed_agents: [], absent_models: [], absent_aliases: [],
      unverified_models: [], unverified_aliases: [], unverified: [], aliases_checked: true };
  });

  // ---- system health and counters ----
  function agentRows(s) {
    return F.machines.map(function (m, i) {
      return { bind_url: 'https://' + m.ip + ':8082', cert_issued_at: iso(s - 20 * DAY), control_channel_tls: true,
        hostname: m.hostname, id: m.id, is_primary_llama: m.id === 'atlas', is_primary_lms: m.id === 'studio',
        last_heartbeat_age_s: round(4 + ((s + i * 3) % 12), 1), liveness: 'live', role: m.id === 'atlas' ? 'host' : 'system_only',
        status: 'approved', tls: 'tls', tls_direction: 'both', tls_role_checked: true, version: release() };
    });
  }
  R.route('GET /api/admin/system-health', function (ctx) {
    var s = sec(ctx);
    var pts = round(11.5 + 1.2 * Math.sin(s / 17), 2);
    return {
      advisories: [], ae_local: true, ae_restart: { available: true, via: 'systemctl' },
      agent_update: { hostnames: [], latest: release(), outdated: 0 }, agents: agentRows(s), containerized: false,
      data_flow: {
        manager_to_alarm_forwarding: { active: false, reason: 'agent forwards directly to alarm engine' },
        primary_llama_push: { age_s: 1.8, agent_id: 'atlas', has_agent: true, ok: true },
        primary_lms_push: { age_s: 2.1, has_agent: true, ok: true },
        primary_vllm_push: { age_s: 1.6, has_agent: true, ok: true } },
      flow: { ae_ingest_points_per_s: pts, agent_pushes_per_s: 1.6, anon_budget_per_min: 600, anon_refused_per_min: 0,
        anon_req_per_min: 0, history_req_per_s: round(1.4 + 0.3 * Math.sin(s / 29), 2), influx_writes_per_s: round(pts + 0.3, 3) },
      jobs: healthJobs(s),
      manager: { connections: { agents: 4, browsers: 2, worker_threads: 160, worker_threads_busy: 3 },
        hostname: 'atlas.example', ok: true, process: { cpu_pct: round(2 + Math.abs(Math.sin(s / 11)) * 3, 1), ram_pct: 6.4, rss_mb: 312 },
        push_subscriptions: 1, ram_total_mb: 128000, streams: { active: 6, limit: 40, peak: 11, refusals: 0, refusals_recent: 0 },
        uptime_s: 5 * DAY + (s % DAY), version: release(), ws_relay: 'connected' },
      overall: 'ok',
      recent: [
        { actor: 'alex', detail: 'forge.example', k: 'ok', title: 'Alarm acknowledged', ts: isoTz(s - 22 * 60) },
        { actor: 'sam', detail: 'forge.example', k: 'ok', title: 'Model loaded', ts: isoTz(s - 55 * 60) },
        { actor: 'alex', detail: 'quill.example', k: 'ok', title: 'Agent approved', ts: isoTz(s - 410 * 60) } ],
      restart_pending: [],
      services: [
        { active_alerts: 2, auth: 'enforced', auth_detail: { bearer_ok: true, ingest: 'enforced', loopback_only: false,
            management: 'management_token', open_on_network: false },
          bearer_configured: true, consecutive_failures: 0, ingest_points_per_s: pts, last_ok_at: isoTz(s - 3),
          latency_ms: round(8 + Math.abs(Math.sin(s / 7)) * 4, 1), name: 'alarm_engine', ok: true,
          otlp: { last_batch_age_s: null, last_batch_at: null, last_error_age_s: null, last_error_at: null, log_batches: 0,
            metric_batches: 0, parse_errors: 0, trace_batches: 0, write_errors: 0 },
          process: { cpu_pct: round(30 + Math.sin(s / 13) * 6, 1), ram_pct: 1.4, rss_mb: 142 },
          rule_eval_ms: round(0.9 + Math.abs(Math.sin(s / 9)) * 0.4, 3), state: 'ok', status_code: 200,
          tls: { active: true, enabled: true, error: null }, tls_role_checked: true, uptime_s: 5 * DAY + (s % DAY),
          url: 'https://localhost:8081', version: release() },
        { name: 'influxdb', ok: true, ping_ms: round(0.9 + Math.abs(Math.sin(s / 5)) * 0.4, 1), state: 'connected',
          version: 'v2.9.1', via: 'alarm_engine', writes_per_s: round(pts + 0.3, 3) } ],
      ts: iso(s), warnings: [] };
  });
  R.route('GET /api/admin/dbstats/sqlite', function (ctx) {
    var s = sec(ctx);
    function db(file, extra, pages) {
      return Object.assign({ file: file, journal_mode: 'wal', page_count: pages, page_size: 4096, query_ms: round(0.004 + Math.abs(Math.sin(s / 9)) * 0.01, 3),
        shm_size_bytes: 32768, size_bytes: pages * 4096, wal_size_bytes: 210000 + (s % 600) * 300 }, extra);
    }
    return { audit_db: db('audit.db', { audit_rows: AUDIT.length }, 24),
      energy_db: db('energy.db', { energy_rows: 2160 + Math.floor(s / 3600) % 24 }, 310),
      manager_db: db('manager.db', { benchmarks: 6, jobs: JOBS.length, report_cards: 3, tool_runs: 9, tower_messages: 3 }, 180), ok: true };
  });
  R.route('GET /api/admin/stream-stats', function (ctx) {
    var s = sec(ctx);
    var agents = F.machines.map(function (m, i) {
      return { active: 1 + ((s / 10 + i) | 0) % 2, agent_id: m.id, cap: 40, hostname: m.hostname, peak: 4, reachable: true,
        refusals: 0, terminal_sessions: 0, worker_threads_busy: 1 };
    });
    return { agent_connections: agents.length * 2, agents: agents, anon_budget_per_min: 600, anon_refused_per_min: 0,
      anon_requests_per_min: 0, browser_connections: 2, hostname: 'atlas.example',
      pool: { active: 6 + ((s / 7) | 0) % 3, limit: 40, peak: 11, refusals: 0, refusals_recent: 0 },
      sse_daemon_running: true, sse_daemon_streams: 8, ts: s + 0.5, worker_backlog: 0, worker_threads: 160,
      worker_threads_busy: 3, worker_threads_idle: 157 };
  });
  R.route('GET /api/admin/gateway/flow', function (ctx) {
    var s = sec(ctx);
    var hosts = F.machines.map(function (m) {
      var gen = m.id === 'quill' ? 0 : round(S.value(m.id, 'tok_s', s) * 0.5, 2);
      return { agent_id: m.id, hostname: m.hostname, provider: m.providers[0], model: m.models[0], gen_tps: gen,
        inflight: gen > 20 ? 1 : 0, last_served_s: gen > 0 ? 3 : 900, primary: m.id === 'atlas' || m.id === 'studio',
        state: gen > 20 ? 'active' : 'idle' };
    });
    var gen = hosts.reduce(function (a, h) { return a + h.gen_tps; }, 0);
    var clients = [
      { label: 'chat-app', ip: '192.0.2.31', model: F.byId('atlas').models[0], req_per_min: 14, inflight: 1, prompt_tokens: 188000, gen_tokens: 41000, last_seen_s: 2.4, state: 'active' },
      { label: 'notes-bot', ip: '192.0.2.32', model: F.byId('forge').models[0], req_per_min: 3, inflight: 0, prompt_tokens: 52000, gen_tokens: 9100, last_seen_s: 41, state: 'active' },
      { label: 'ci-runner', ip: '192.0.2.33', model: F.byId('atlas').models[1], req_per_min: 0, inflight: 0, prompt_tokens: 21000, gen_tokens: 4400, last_seen_s: 1260, state: 'idle' }
    ];
    return { clients: clients, enabled: true, endpoint: '/api/gateway/v1',
      energy: { cloud_usd_per_mtok: 0.6, cost_today: 0.42, kwh_today: 2.8, serving_w: 560, usd_per_mtok: 0.09 },
      hosts: hosts, keys: 3, ok: true,
      totals: { errors_15m: 0, gen_tps: round(gen, 2), inflight: 2, p50_ms: 640, prompt_tps: round(gen * 8, 2), req_per_min: 17 },
      usage_probe: true };
  });

  // ---- tools: ledger, tickets, OpenClaw ----
  function toolRows(s) {
    var a = F.byId('atlas'), f = F.byId('forge');
    return [
      { tool: 'benchmark', model_id: a.models[0], agent_id: 'atlas', provider: 'llama', ok: true, ts: s - 9 * HOUR, run_id: 'demo-run-b2', conditions: null,
        summary: { gen_tps: 68.4, ppt_tps: 1540, pg_tps: 61.2, latency_s: 1.9, bench_tool: 'llama-bench' } },
      { tool: 'autotune', model_id: a.models[0], agent_id: 'atlas', provider: 'llama', ok: true, ts: s - 2 * DAY, run_id: 'demo-run-a1', conditions: null,
        summary: { decode_tps: 71.9, prefill_tps: 1710, ctx_size: 16384, gain_pct: 12, objective: 'speed', mode: 'quick' } },
      { tool: 'reportcard', model_id: f.models[0], agent_id: 'forge', provider: 'llama', ok: true, ts: s - 5 * HOUR, run_id: 'demo-run-r1', conditions: null,
        summary: { gen_tps: 41.7, ppt_tps: 980, ttft_ms: 310, mode: 'standard' } },
      { tool: 'benchmark', model_id: a.models[0], agent_id: 'atlas', provider: 'llama', ok: true, ts: s - 3 * DAY, run_id: 'demo-run-b1', conditions: null,
        summary: { gen_tps: 66.1, ppt_tps: 1490, pg_tps: 59.8, latency_s: 2.0, bench_tool: 'llama-bench' } }
    ];
  }
  R.route('GET /api/tools/runs', function (ctx) {
    var all = toolRows(sec(ctx));
    var rows = all.filter(function (r) {
      return ['tool', 'model_id', 'agent_id'].every(function (k) { return !ctx.query.get(k) || r[k] === ctx.query.get(k); });
    });
    var limit = Math.max(1, Math.min(100, num(ctx.query, 'limit', 100)));
    var totals = {}, latest = {};
    all.forEach(function (r) { totals[r.tool] = (totals[r.tool] || 0) + 1; if (!latest[r.tool]) latest[r.tool] = r; });
    return { runs: rows.slice(0, limit), totals: totals, latest: latest };
  });
  // Pool model lists per provider, matching the models each machine serves in handlers/models.js.
  function providerModels(prov) {
    var seen = {};
    F.machines.forEach(function (m) {
      if (m.providers.indexOf(prov) < 0) return;
      var ids = prov === 'vllm' ? m.models.slice(2, 3)
        : prov === 'llama' && m.providers.indexOf('vllm') >= 0 ? m.models.slice(0, 2) : m.models;
      ids.forEach(function (id) { (seen[id] = seen[id] || []).push(m.hostname); });
    });
    var models = Object.keys(seen).sort().map(function (id) { return { id: id, agents: seen[id].sort() }; });
    return { ok: true, models: models, errors: [] };
  }
  ['llama', 'lms', 'vllm'].forEach(function (prov) {
    R.route('GET /api/admin/' + prov + '-models', function () { return providerModels(prov); });
  });

  R.route('GET /api/tool-ticket', function (ctx) {
    return { ok: true, url: self.location.origin + '/proxy/' + (ctx.query.get('tool') || 'tool') + '/', port: 0 };
  });
  R.route('GET /tools-ping', function () { return { ok: true }; });

  // ---- OpenClaw: three agents on the sample machines, sessions spread over the last day ----
  var OC_AGENTS = [
    { name: 'atlas-assistant', host: 'atlas', model: 'Llama-3.1-8B-Instruct-Q8_0', n: 9, think: false, fail: [5, 'failed'],
      tools: [['read_file', 4], ['search', 3], ['web_fetch', 2], ['run_command', 1]],
      goals: ['Summarize overnight alarms for the team', 'Draft the weekly status note', 'Answer a question from the support inbox',
        'Check which models are loaded on every machine', 'Collect GPU temperatures from forge.example'],
      error: 'web_fetch: the page took too long to answer' },
    { name: 'forge-coder', host: 'forge', model: 'Qwen2.5-14B-Instruct-Q5_K_M', n: 7, think: false, fail: [3, 'failed'],
      tools: [['read_file', 4], ['write_file', 3], ['run_command', 3], ['search', 1]],
      goals: ['Fix the failing unit test in the parser', 'Review a pull request for style issues',
        'Write a script that rotates log files', 'Add input checks to the upload form'],
      error: 'run_command: tests exited with code 1' },
    { name: 'studio-research', host: 'studio', model: 'Llama-3.3-70B-Instruct-4bit', n: 5, think: true, fail: [2, 'timed_out'],
      tools: [['web_fetch', 3], ['search', 3], ['read_file', 2]],
      goals: ['Compare three papers on speculative decoding', 'Write a short report on power use per model',
        'Plan a reading list on quantization'],
      error: 'subagent: no reply after 120 s' }
  ];
  var OC_TRENDS = { read_file: 'up', search: 'up', run_command: 'stable', write_file: 'stable', web_fetch: 'down' };

  function frac(str) {
    var h = 2166136261;
    for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0) / 4294967296;
  }
  function hex(str, n) {
    var out = '';
    for (var i = 0; out.length < n; i++) out += ('0000000' + Math.floor(frac(str + i) * 4294967296).toString(16)).slice(-8);
    return out.slice(0, n);
  }
  function uuid(str) {
    return hex(str + 'a', 8) + '-' + hex(str + 'b', 4) + '-4' + hex(str + 'c', 3) + '-a' + hex(str + 'd', 3) + '-' + hex(str + 'e', 12);
  }

  // One agent's sessions, newest first; times move in 10-minute steps; the first agent's newest one is still running.
  function ocSessions(a, s, first) {
    var anchor = Math.floor(s / 600) * 600;
    var out = [];
    for (var k = 0; k < a.n; k++) {
      var key = a.name + ':' + k;
      var h = [frac(key + '1'), frac(key + '2'), frac(key + '3'), frac(key + '4')];
      var running = first && k === 0;
      var last = running ? anchor : anchor - Math.round((k + 0.15 + 0.5 * h[0]) * (82800 / a.n));
      var messages = 6 + Math.floor(h[1] * 18);
      var input = Math.round(messages * (1800 + 2400 * h[2]));
      var dur = Math.round(40 + 560 * h[3]);
      var events = a.think ? 2 + Math.floor(h[2] * 6) : 0;
      out.push({ id: uuid(key), messages: messages, input: input, output: Math.round(messages * (220 + 380 * h[0])),
        cacheRead: Math.round(input * (0.35 + 0.3 * h[1])), cacheWrite: 0, cost: 0,
        tool_uses: Math.floor(messages * (0.4 + 0.4 * h[2])), first_ts: iso(last - dur), last_ts: iso(last), models: [a.model],
        thinking_events: events, thinking_chars: events * (900 + Math.round(1200 * h[3])),
        _last: last, _dur: dur, _goal: a.goals[k % a.goals.length],
        _status: running ? 'running' : k === a.fail[0] ? a.fail[1] : 'succeeded' });
    }
    return out;
  }
  function pub(x) {
    var o = {};
    Object.keys(x).forEach(function (k) { if (k.charAt(0) !== '_') o[k] = x[k]; });
    return o;
  }
  function sum(list, k) { return list.reduce(function (t, x) { return t + (x[k] || 0); }, 0); }

  R.route('GET /api/openclaw/analytics', function (ctx) {
    var s = sec(ctx);
    var KEYS = ['sessions', 'messages', 'input', 'output', 'cacheRead', 'cacheWrite', 'cost', 'tool_uses'];
    var totals = {};
    KEYS.forEach(function (k) { totals[k] = 0; });
    var tools = {}, all = [], agents = [];
    OC_AGENTS.forEach(function (a, ai) {
      var list = ocSessions(a, s, ai === 0);
      var row = { name: a.name, sessions: list.length, cost: 0, models: [a.model], last_ts: list[0].last_ts,
        recent: list.slice(0, 10).map(pub), monthly_projection_usd: null };
      ['messages', 'input', 'output', 'cacheRead', 'cacheWrite', 'tool_uses'].forEach(function (k) { row[k] = sum(list, k); });
      var day = row.input + row.output;
      row.trend = { trend: 'stable', dailyAvg: day, monthlyPrediction: day * 30 };
      var weight = a.tools.reduce(function (t, x) { return t + x[1]; }, 0), left = row.tool_uses;
      a.tools.forEach(function (t, i) {
        var c = i === a.tools.length - 1 ? left : Math.round(row.tool_uses * t[1] / weight);
        left -= c;
        tools[t[0]] = (tools[t[0]] || 0) + c;
      });
      KEYS.forEach(function (k) { totals[k] += row[k]; });
      list.forEach(function (x) { x._agent = a; all.push(x); });
      agents.push(row);
    });
    agents.sort(function (x, y) { return x.last_ts < y.last_ts ? 1 : -1; });
    var toolCalls = Object.keys(tools).reduce(function (t, k) { return t + tools[k]; }, 0) || 1;
    var attribution = Object.keys(tools).map(function (k) { return { tool: k, count: tools[k], pct: round(100 * tools[k] / toolCalls, 1) }; })
      .sort(function (x, y) { return y.count - x.count; });
    all.sort(function (x, y) { return y._last - x._last; });

    var flows = all.filter(function (x) { return x.tool_uses >= 6 || x._status === 'running'; });
    var flowStatus = {};
    flows.forEach(function (x) { flowStatus[x._status] = (flowStatus[x._status] || 0) + 1; });
    var failed = all.filter(function (x) { return x._status === 'failed' || x._status === 'timed_out'; });
    var taskStatus = { succeeded: 0, failed: 0, timed_out: 0, running: 0 };
    failed.forEach(function (x) { taskStatus[x._status]++; });
    var runtime = { subagent: 0, cli: 0 };
    all.forEach(function (x, i) {
      var n = Math.max(1, Math.floor(x.tool_uses / 5));
      if (x._status === 'running') { taskStatus.running++; n--; }
      taskStatus.succeeded += failed.indexOf(x) >= 0 ? n - 1 : n;
      runtime[i % 3 === 0 ? 'cli' : 'subagent'] += n;
    });
    var hour = all.filter(function (x) { return s - x._last < 3600; });
    var hourTokens = sum(hour, 'input') + sum(hour, 'output');
    var dayTokens = totals.input + totals.output;
    var daily = {};
    for (var d = 6; d >= 0; d--) daily[new Date((s - d * DAY) * 1000).toISOString().slice(0, 10)] = 0;
    var tokAll = totals.input + totals.output + totals.cacheRead + totals.cacheWrite;
    var pct = function (v) { return round(v / Math.max(tokAll, 1) * 100, 1); };
    var thinkers = all.filter(function (x) { return x.thinking_events > 0; });
    var thinkAgents = OC_AGENTS.filter(function (a) { return a.think; }).map(function (a) {
      var mine = thinkers.filter(function (x) { return x._agent === a; });
      return { name: a.name, thinking_events: sum(mine, 'thinking_events'), thinking_chars: sum(mine, 'thinking_chars'), cost: 0 };
    });
    return { ts: s, totals: totals,
      trend: { trend: 'stable', dailyAvg: dayTokens, monthlyPrediction: dayTokens * 30 },
      tasks: { total: Object.keys(taskStatus).reduce(function (t, k) { return t + taskStatus[k]; }, 0), by_status: taskStatus,
        by_runtime: runtime, avg_duration_s: 6.4,
        recent_failures: failed.slice(0, 10).map(function (x) {
          return { label: x._goal, kind: /^subagent/.test(x._agent.error) ? 'subagent' : 'cli', status: x._status,
            error: x._agent.error, created_iso: x.first_ts };
        }) },
      flows: { total: flows.length, by_status: flowStatus, recent: flows.slice(0, 20).map(function (x) {
        return { goal: x._goal, status: x._status, duration_s: x._status === 'running' ? null : x._dur, created_iso: x.first_ts };
      }) },
      delivery: { total: 0, by_channel: {}, total_retries: 0, common_errors: [], oldest_enqueue_iso: null },
      daily_cost: daily,
      token_dist: { input_pct: pct(totals.input), output_pct: pct(totals.output), cache_r_pct: pct(totals.cacheRead),
        cache_w_pct: pct(totals.cacheWrite), cache_hit_pct: round(totals.cacheRead / Math.max(totals.input + totals.cacheRead, 1) * 100, 1),
        total: tokAll },
      thinking: { agents: thinkAgents, sessions: thinkers.sort(function (x, y) { return y.thinking_events - x.thinking_events; })
        .slice(0, 10).map(function (x) { var o = pub(x); o.agent = x._agent.name; return o; }) },
      velocity: { tokens_1h: hourTokens, active_sessions: hour.length, tokens_per_min: round(hourTokens / 60, 1), window: 'hour' },
      budget: { monthly_budget_usd: null, warning_pct: 80, projected_monthly_usd: null, used_pct: null },
      anomalies: [], tool_attribution: attribution, tool_trends: OC_TRENDS, agents: agents };
  });
})(self);
