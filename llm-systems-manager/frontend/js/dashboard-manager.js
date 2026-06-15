// ---------------------------------------------------------------------------
// Services + InfluxDB self-monitor cards
// ---------------------------------------------------------------------------
// Both data sources live in the alarm engine (the agent pushes process
// metrics under source=processes; the alarm engine writes its own
// InfluxDB probes under source=influxdb). /api/alarm/* is proxied to
// localhost:8081, so the frontend just hits the catalog endpoint.

function _fmtBytesShort(n) {
  if (n == null || isNaN(n)) return '—';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0; let v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
}
function _fmtUptime(s) {
  if (s == null || isNaN(s)) return '—';
  s = Math.max(0, Math.floor(Number(s)));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
}
function _fmtAge(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (!t) return null;
  return (Date.now() - t) / 1000;
}

async function fetchServicesAndInflux() {
  if (document.hidden) return;
  // The cards this populates (Services watchlist, InfluxDB, manager system,
  // manager_self_monitor, mac_power) are manager/fleet cards — they live on the
  // Overall tab and the Dashboard → Manager sub-tab ONLY, not the per-agent
  // llama/lms dashboards. Skip elsewhere so dbstats + the cross-host source
  // metrics don't fire from a per-agent dashboard (#123, #127).
  const _onMgrSub = _activeTab === 'dashboard'
    && _subTabState && _subTabState['dashboard'] === 'manager';
  if (_activeTab !== 'overall' && !_onMgrSub) return;
  try {
    // Fan out to four narrow source-filtered fetches instead of one
    // unconstrained "give me all 2000 rows" pull. compute_metric_list in
    // the alarm engine short-circuits per-source via its (source, limit)
    // cache key, so each call scans only the slice we need and the
    // results are cached independently. Net: ~10× less payload, faster
    // server scan, smaller browser-side JSON parse.
    const sources = ['processes', 'manager_self_monitor', 'influxdb', 'mac_power'];
    // Manager-host system metrics are scoped by agent id (resolved to a host
    // server-side), never by a browser-held hostname (#140). The other sources
    // are intentionally cross-host (services/influx/mac span the fleet).
    const MGR_AGENT = window.__MGR_AGENT;
    const mgrSysFetch = MGR_AGENT
      ? fetch(`/api/alarm/metrics?source=system&agent=${encodeURIComponent(MGR_AGENT)}&limit=500`)
          .then(r => r.json()).catch(() => [])
      : Promise.resolve([]);
    const [sqliteStatsRaw, mgrSysRaw, ...results] = await Promise.all([
      fetch('/api/alarm/dbstats/sqlite').then(r => r.json()).catch(() => ({})),
      mgrSysFetch,
      ...sources.map(s =>
        fetch(`/api/alarm/metrics?source=${encodeURIComponent(s)}&limit=500`)
          .then(r => r.json()).catch(() => [])
      ),
    ]);
    const sqliteStats = sqliteStatsRaw || {};
    const rows = [].concat(...results.map(r => Array.isArray(r) ? r : []));
    if (!rows.length && !(Array.isArray(mgrSysRaw) && mgrSysRaw.length)) return;
    // Key services by (host, svc) so multi-host watchlists don't clobber each
    // other (e.g. both llm-systems-manager and llm-systems-llama report on
    // their own copies of the watchlist).
    const services = {};       // "host::svc" -> {host, svc, running, pid, rss_mb, uptime_s, count, age}
    const influx = {};         // metric_name -> {value, host, age}
    const mac    = {};         // metric_name -> {value, host, age}
    const mgrSys = {};         // metric_name -> {value, age} — system source on the manager host
    const selfMon = {};        // metric_name -> {value, age} — manager_self_monitor source (agent self-probes)
    // Manager-host system metrics from the agent-scoped fetch above — already
    // filtered to the manager's host server-side, so no hostname check here.
    for (const r of (Array.isArray(mgrSysRaw) ? mgrSysRaw : [])) {
      if (r.source === 'system' && r.metric_name) {
        mgrSys[r.metric_name] = { value: r.latest_value, age: _fmtAge(r.latest_timestamp) };
      }
    }
    for (const r of rows) {
      if (r.source === 'manager_self_monitor' && r.metric_name) {
        // Multi-host safe: if there are eventually two monitor agents (e.g.
        // manager-host AND alarm-host), each row carries hostname; latest
        // value wins, which is fine for the dashboard's "current" view.
        selfMon[r.metric_name] = { value: r.latest_value, age: _fmtAge(r.latest_timestamp) };
      }
      if (r.source === 'processes' && r.metric_name) {
        // metric_name is like "llama-server_running" / "<name>_count" /
        // "<name>_rss_mb" / "<name>_uptime_s" / "<name>_pid" / "<name>_available".
        const m = r.metric_name.match(/^(.+?)_(running|count|pid|rss_mb|uptime_s|available)$/);
        if (!m) continue;
        const [, svc, field] = m;
        const host = r.hostname || '';
        const key = host + '::' + svc;
        const bucket = services[key] = services[key] || { host, svc };
        bucket[field] = r.latest_value;
        const age = _fmtAge(r.latest_timestamp);
        if (bucket.age == null || (age != null && age < bucket.age)) bucket.age = age;
      } else if (r.source === 'influxdb' && r.metric_name) {
        const bucket = influx[r.metric_name] = { value: r.latest_value, host: r.hostname };
        bucket.age = _fmtAge(r.latest_timestamp);
      } else if (r.source === 'mac_power' && r.metric_name) {
        const bucket = mac[r.metric_name] = { value: r.latest_value, host: r.hostname };
        bucket.age = _fmtAge(r.latest_timestamp);
      }
    }

    // ---- Services card (one row per host × svc) ----
    // Drop entries that haven't reported within HIDE_S — these are stale
    // series left in the alarm-engine cache after the operator removed an
    // entry from PROCESS_WATCHLIST. The cache TTL (~15 min) will eventually
    // age them out on its own, but hiding sooner gives the card immediate
    // feedback after a watchlist edit. STALE_S below it controls when an
    // entry still in the watchlist is rendered as "stale" vs running.
    // STALE_S must exceed the agent's METRIC_FLUSH_INTERVAL_S (default 30s)
    // by enough margin that a normal flush-window boundary doesn't read as
    // stale. 90s = ~3 flush windows; a real service stop still shows within
    // one flush cycle but transient timing drift no longer flaps the card.
    const HIDE_S = 300, STALE_S = 90;
    const tbody = document.getElementById('servicesTable');
    if (tbody) {
      const allKeys = Object.keys(services);
      const keys = allKeys.filter(k => services[k].age != null && services[k].age <= HIDE_S)
        .sort((a, b) => {
          const sa = services[a], sb = services[b];
          return (sa.host || '').localeCompare(sb.host || '') ||
                 (sa.svc || '').localeCompare(sb.svc || '');
        });
      const hiddenStale = allKeys.length - keys.length;
      if (!keys.length) {
        const msg = hiddenStale
          ? `No services reporting in the last ${HIDE_S}s (${hiddenStale} stale entries hidden).`
          : 'No services reporting yet.';
        tbody.innerHTML = `<tr><td colspan="5" style="color:var(--fg-dim);font-size:0.85em;">${msg}</td></tr>`;
      } else {
        // Group by host, alphabetical within each group. Emit a sticky
        // header row for each host so the visual division is clear even
        // when the table is dragged into a narrow column.
        const byHost = {};
        for (const k of keys) {
          const s = services[k];
          const h = s.host || '(unknown)';
          (byHost[h] = byHost[h] || []).push(s);
        }
        const hosts = Object.keys(byHost).sort();
        let rows = '';
        for (const h of hosts) {
          const group = byHost[h].sort((a, b) => (a.svc || '').localeCompare(b.svc || ''));
          const upHere = group.filter(s => s.running === 1 && (s.age == null || s.age <= STALE_S)).length;
          rows += `<tr class="svc-group-hdr">
            <td colspan="5" style="background:var(--bg-elev);color:var(--fg-muted);font-size:0.72em;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;padding:6px 8px;border-top:1px solid var(--border);">
              ${_esc(h)}
              <span style="float:right;color:var(--fg-dim);font-weight:normal;letter-spacing:0.04em;">${upHere}/${group.length} up</span>
            </td>
          </tr>`;
          rows += group.map(s => {
            const stale = s.age > STALE_S;
            const running = s.running === 1 && !stale;
            const dot = `<span class="dot dot--${running ? 'ok' : stale ? 'muted' : 'crit'}"></span>`;
            const label = stale ? 'stale' : (running ? 'running' : 'stopped');
            return `<tr>
              <td style="padding-left:18px;">${_esc(s.svc)}</td>
              <td>${dot} ${label}</td>
              <td>${s.pid != null ? Math.round(s.pid) : '—'}</td>
              <td>${s.rss_mb != null ? s.rss_mb.toFixed(0) + ' MB' : '—'}</td>
              <td>${_fmtUptime(s.uptime_s)}</td>
            </tr>`;
          }).join('');
        }
        if (hiddenStale) {
          rows += `<tr><td colspan="5" style="color:var(--fg-dim);font-size:0.72em;padding-top:8px;">${hiddenStale} stale entr${hiddenStale === 1 ? 'y' : 'ies'} hidden (will clear from cache automatically)</td></tr>`;
        }
        tbody.innerHTML = rows;
      }
      let cls = 'dash-off';
      if (keys.length) {
        const fresh = keys.filter(k => services[k].age != null && services[k].age <= STALE_S);
        const anyDown = fresh.some(k => services[k].running === 0);
        const anyOk = fresh.some(k => services[k].running === 1);
        cls = anyDown ? 'dash-crit' : (anyOk ? 'dash-ok' : 'dash-warn');
      }
      _dashSetStatus('services', cls);
      const badge = document.getElementById('services-badge');
      if (badge) {
        const totalUp = keys.filter(k => services[k].running === 1).length;
        badge.textContent = keys.length ? `${totalUp}/${keys.length} up` : '';
      }
    }

    // ---- InfluxDB card ----
    const up = influx['up'];
    const setText = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
    setText('influxPing',         influx['ping_ms']?.value != null ? influx['ping_ms'].value.toFixed(1) : '—');
    setText('influxQuery',        influx['query_ms']?.value != null ? influx['query_ms'].value.toFixed(1) : '—');
    setText('influxWrite',        influx['write_ms']?.value != null ? influx['write_ms'].value.toFixed(1) : '—');
    setText('influxWriteOk',      influx['write_ok_rate']?.value != null ? (influx['write_ok_rate'].value * 100).toFixed(1) : '—');
    setText('influxDisk',         influx['bytes_on_disk']?.value != null ? _fmtBytesShort(influx['bytes_on_disk'].value) : '—');
    setText('influxCardMetrics',  influx['cardinality_metrics']?.value != null ? Math.round(influx['cardinality_metrics'].value) : '—');

    const a = sqliteStats.alarms_db || {};
    const s = sqliteStats.settings_db || {};
    const sumIfAny = (...vs) => {
      const nums = vs.filter(v => typeof v === 'number');
      return nums.length ? nums.reduce((x, y) => x + y, 0) : null;
    };
    const settingsRows = sumIfAny(s.rules, s.channels, s.configs);
    const walTotal = sumIfAny(a.wal_size_bytes, s.wal_size_bytes);
    const pagesTotal = sumIfAny(a.page_count, s.page_count);
    const slowestQuery = sumIfAny(Math.max(a.query_ms ?? -Infinity, s.query_ms ?? -Infinity));
    const fmtCount = v => v != null ? v.toLocaleString() : '—';
    setText('sqliteAlertsRows',   fmtCount(a.alerts));
    setText('sqliteSettingsRows', fmtCount(settingsRows));
    setText('sqliteAlarmsSize',   a.size_bytes != null ? _fmtBytesShort(a.size_bytes) : '—');
    setText('sqliteSettingsSize', s.size_bytes != null ? _fmtBytesShort(s.size_bytes) : '—');
    setText('sqliteWalSize',      walTotal != null ? _fmtBytesShort(walTotal) : '—');
    setText('sqlitePages',        fmtCount(pagesTotal));
    setText('sqliteQueryMs',      slowestQuery != null ? slowestQuery.toFixed(2) : '—');
    setText('sqliteDeliveries',   fmtCount(s.deliveries));
    const inflBadge = document.getElementById('influxdb-badge');
    const upStale = (up?.age == null) || (up.age > 90);
    if (inflBadge) {
      if (upStale) { inflBadge.className = 'status status--muted'; inflBadge.textContent = 'no data'; }
      else if (up.value === 1) { inflBadge.className = 'status status--ok'; inflBadge.innerHTML = '<span class="status__dot"></span>up'; }
      else { inflBadge.className = 'status status--crit'; inflBadge.innerHTML = '<span class="status__dot"></span>down'; }
    }
    let influxCls = 'dash-off';
    if (!upStale) {
      if (up.value !== 1) influxCls = 'dash-crit';
      else if ((influx['ping_ms']?.value ?? 0) > 100 || (influx['query_ms']?.value ?? 0) > 500) influxCls = 'dash-warn';
      else influxCls = 'dash-ok';
    }
    _dashSetStatus('influxdb', influxCls);

    // ---- Mac powermetrics card ----
    const mpAge = mac['cpu_package_w']?.age ?? mac['soc_total_w']?.age ?? mac['thermal_pressure_n']?.age;
    const mpStale = (mpAge == null) || (mpAge > 30);
    const fmtW = k => mac[k]?.value != null ? mac[k].value.toFixed(2) + ' W' : '—';
    setText('mpSocW',   fmtW('soc_total_w'));
    setText('mpCpuPkg', fmtW('cpu_package_w'));
    setText('mpGpuW',   fmtW('gpu_w'));
    setText('mpAneW',   fmtW('ane_w'));
    const tpN = mac['thermal_pressure_n']?.value;
    setText('mpThermal', tpN == null ? '—' : ['Nominal','Fair','Serious','Critical'][Math.round(tpN)] || '—');
    const gbusy = mac['gpu_busy_pct']?.value;
    setText('mpGpuBusyVal', gbusy != null ? gbusy.toFixed(1) + '%' : '—');
    const pfreq = mac['pcore_freq_mhz']?.value, putil = mac['pcore_util_pct']?.value;
    const efreq = mac['ecore_freq_mhz']?.value, eutil = mac['ecore_util_pct']?.value;
    setText('mpPcore', pfreq == null && putil == null ? '—'
      : `${pfreq != null ? Math.round(pfreq) + ' MHz' : '—'}${putil != null ? ' · ' + putil.toFixed(0) + '%' : ''}`);
    setText('mpEcore', efreq == null && eutil == null ? '—'
      : `${efreq != null ? Math.round(efreq) + ' MHz' : '—'}${eutil != null ? ' · ' + eutil.toFixed(0) + '%' : ''}`);
    const gfreq = mac['gpu_freq_mhz']?.value;
    setText('mpGpuClock', gfreq != null ? Math.round(gfreq) + ' MHz' : '—');
    const nin = mac['net_in_pkts_s']?.value, nout = mac['net_out_pkts_s']?.value;
    setText('mpNet', nin == null && nout == null ? '—'
      : `${nin != null ? Math.round(nin) : '—'} in / ${nout != null ? Math.round(nout) : '—'} out`);
    const mpBadge = document.getElementById('lms-power-badge');
    if (mpBadge) {
      if (mpStale) { mpBadge.className = 'status status--muted'; mpBadge.textContent = 'no data'; }
      else { mpBadge.className = 'status status--ok'; mpBadge.innerHTML = '<span class="status__dot"></span>live'; }
    }
    let mpCls = 'dash-off';
    if (!mpStale) {
      if (tpN != null && tpN >= 2) mpCls = 'dash-crit';
      else if (tpN != null && tpN >= 1) mpCls = 'dash-warn';
      else mpCls = 'dash-ok';
    }
    _dashSetStatus('lms-power', mpCls);

    // ---- Manager host performance cards (llm-systems-manager agent) ----
    // (`setText` and `_fmtBytesShort` / `_fmtUptime` are already in scope.)
    const mGet = k => mgrSys[k]?.value;
    const mAge = k => mgrSys[k]?.age;
    // Same anti-flap reasoning as STALE_S in the Services card: must
    // comfortably exceed the agent's METRIC_FLUSH_INTERVAL_S (30s default).
    const MGR_STALE_S = 90;
    const fmtPct = v => v == null ? '—' : v.toFixed(1) + '%';
    const fmtBpair = (used, total) => {
      if (used == null || total == null) return '—';
      return `${_fmtBytesShort(used)} / ${_fmtBytesShort(total)}`;
    };
    const fmtMiBs = bps => bps == null ? '—' : (bps / (1024 * 1024)).toFixed(2);
    const dashClsPct = (v, age, warn, crit) => {
      if (age == null || age > MGR_STALE_S || v == null) return 'dash-off';
      if (v >= crit) return 'dash-crit';
      if (v >= warn) return 'dash-warn';
      return 'dash-ok';
    };

    const rank = c => ({'dash-crit':3,'dash-warn':2,'dash-ok':1,'dash-off':0})[c] || 0;
    const worst = (...cs) => cs.reduce((a,b) => rank(a) >= rank(b) ? a : b, 'dash-off');
    // CPU + RAM + Swap (combined card)
    {
      const cv = mGet('cpu_total'),    cAge = mAge('cpu_total');
      const rp = mGet('ram_percent'),  rAge = mAge('ram_percent');
      const sp = mGet('swap_percent'), sAge = mAge('swap_percent');
      setText('mgrCpuTotal',   fmtPct(cv));
      setText('mgrRamPct',     fmtPct(rp));
      setText('mgrRamUsed',    fmtBpair(mGet('ram_used_bytes'), mGet('ram_total_bytes')));
      setText('mgrRamCached',  mGet('ram_cached_bytes')  != null ? _fmtBytesShort(mGet('ram_cached_bytes'))  : '—');
      setText('mgrRamBuffers', mGet('ram_buffers_bytes') != null ? _fmtBytesShort(mGet('ram_buffers_bytes')) : '—');
      setText('mgrSwapPct',    fmtPct(sp));
      setText('mgrSwapUsed',   fmtBpair(mGet('swap_used_bytes'), mGet('swap_total_bytes')));
      _dashSetStatus('mgr-ram', worst(
        dashClsPct(cv, cAge, 80, 95),
        dashClsPct(rp, rAge, 85, 95),
        dashClsPct(sp, sAge, 50, 80),
      ));
    }
    // Disk usage (root + boot) + Disk IO (combined card)
    {
      const rootPct = mGet('disk_root_percent'), bootPct = mGet('disk_boot_percent');
      const dAge = mAge('disk_root_percent');
      const rd = mGet('disk_io_read_bytes_per_sec'), wr = mGet('disk_io_write_bytes_per_sec');
      setText('mgrDiskRootPct',  fmtPct(rootPct));
      setText('mgrDiskRootUsed', fmtBpair(mGet('disk_root_used_bytes'), mGet('disk_root_total_bytes')));
      setText('mgrDiskBootPct',  fmtPct(bootPct));
      setText('mgrDiskBootUsed', fmtBpair(mGet('disk_boot_used_bytes'), mGet('disk_boot_total_bytes')));
      setText('mgrIoRead',  fmtMiBs(rd));
      setText('mgrIoWrite', fmtMiBs(wr));
      _dashSetStatus('mgr-disk',
        dashClsPct(Math.max(rootPct ?? 0, bootPct ?? 0), dAge, 80, 92));
    }
    // Network
    {
      const rx = mGet('net_bytes_recv_per_sec') ?? mGet('net_bytes_recv_per_s');
      const tx = mGet('net_bytes_sent_per_sec') ?? mGet('net_bytes_sent_per_s');
      const age = mAge('net_bytes_recv_per_sec') ?? mAge('net_bytes_recv_per_s');
      setText('mgrNetRecv', fmtMiBs(rx));
      setText('mgrNetSent', fmtMiBs(tx));
      _dashSetStatus('mgr-network', (age == null || age > MGR_STALE_S) ? 'dash-off' : 'dash-ok');
    }
    // Self-monitor cards — manager_self_monitor source. The agent's
    // _meta_perf_loop refreshes these every META_PERF_INTERVAL_S (default
    // 60s); we re-paint on every 10s tick of fetchServicesAndInflux.
    {
      const sGet = k => selfMon[k]?.value;
      const sAge = k => selfMon[k]?.age;
      const SM_STALE_S = 180;
      const fmtMs = v => v == null ? '—' : v.toFixed(1) + ' ms';
      const latencyCls = (v, age) => {
        if (age == null || age > SM_STALE_S || v == null) return 'dash-off';
        if (v >= 500) return 'dash-crit';
        if (v >= 100) return 'dash-warn';
        return 'dash-ok';
      };

      // Card 1: Summary tile — worst current latency across all probes.
      const probeKeys = [
        'manager_api_latency_ms', 'manager_history_latency_ms',
        'ae_health_latency_ms', 'ae_ingest_latency_ms', 'ae_query_24h_latency_ms',
        'rule_eval_cycle_ms',
        'influx_write_latency_ms', 'influx_query_5m_latency_ms', 'influx_query_24h_latency_ms',
      ];
      let worstK = null, worstV = -1, freshestAge = null;
      for (const k of probeKeys) {
        const v = sGet(k), age = sAge(k);
        if (age == null || age > SM_STALE_S || v == null) continue;
        if (v > worstV) { worstV = v; worstK = k; }
        if (freshestAge == null || age < freshestAge) freshestAge = age;
      }
      setText('selfMonWorst', worstK ? fmtMs(worstV) : '—');
      setText('selfMonWorstLabel', worstK ? worstK.replace(/_ms$/, '') : 'no probes reporting');
      setText('selfMonAgeNote', freshestAge != null
        ? `freshest probe: ${Math.round(freshestAge)}s ago`
        : 'no recent samples');
      _dashSetStatus('mgr-perf-summary', latencyCls(worstV >= 0 ? worstV : null, freshestAge));

      // Card 2: Manager Perf — two-line sparkline.
      const apiV  = sGet('manager_api_latency_ms');
      const histV = sGet('manager_history_latency_ms');
      const apiAge = sAge('manager_api_latency_ms');
      setText('mgrPerfApi',     fmtMs(apiV));
      setText('mgrPerfHistory', 'history: ' + fmtMs(histV));
      if (mgrPerfChart && (apiV != null || histV != null)) {
        pushMulti(mgrPerfChart, Date.now(), [apiV, histV]);
      }
      _dashSetStatus('mgr-perf', latencyCls(apiV, apiAge));

      // Card 3: AE + Influx Perf — seven-line sparkline + disk-bytes stat.
      const aeVals = {
        ae_health:        sGet('ae_health_latency_ms'),
        ae_ingest:        sGet('ae_ingest_latency_ms'),
        ae_query_24h:     sGet('ae_query_24h_latency_ms'),
        rule_eval_cycle:  sGet('rule_eval_cycle_ms'),
        influx_write:     sGet('influx_write_latency_ms'),
        influx_query_5m:  sGet('influx_query_5m_latency_ms'),
        influx_query_24h: sGet('influx_query_24h_latency_ms'),
      };
      const aeArr = [
        aeVals.ae_health, aeVals.ae_ingest, aeVals.ae_query_24h, aeVals.rule_eval_cycle,
        aeVals.influx_write, aeVals.influx_query_5m, aeVals.influx_query_24h,
      ];
      let aeWorst = null;
      for (const v of aeArr) {
        if (v != null && (aeWorst == null || v > aeWorst)) aeWorst = v;
      }
      setText('aePerfWorst', fmtMs(aeWorst));
      if (aePerfChart && aeWorst != null) {
        pushMulti(aePerfChart, Date.now(), aeArr);
      }
      _dashSetStatus('ae-perf', latencyCls(aeWorst, sAge('ae_health_latency_ms')));
    }
    // Process detail card — pull the manager-host rows we already keyed in `services`.
    {
      const tbody = document.getElementById('mgrProcTable');
      if (tbody) {
        const want = ['manager', 'alarm-engine', 'influxdb', 'llm-systems-agent'];
        // Manager host = the box running the manager-only services (#140 dropped
        // the MGR_HOST global; services are keyed host::svc).
        const mgrHost = (Object.values(services).find(s =>
          s.svc === 'manager' || s.svc === 'alarm-engine' || s.svc === 'influxdb') || {}).host;
        const found = (mgrHost ? want.map(svc => services[mgrHost + '::' + svc]) : [])
          .filter(s => s && s.age != null && s.age <= 300);
        if (!found.length) {
          tbody.innerHTML = '<tr><td colspan="4" style="color:var(--fg-dim);font-size:0.85em;">No process data from the manager agent.</td></tr>';
          _dashSetStatus('mgr-processes', 'dash-off');
        } else {
          const anyDown = found.some(s => s.running === 0);
          const anyUp   = found.some(s => s.running === 1);
          _dashSetStatus('mgr-processes', anyDown ? 'dash-crit' : (anyUp ? 'dash-ok' : 'dash-warn'));
          tbody.innerHTML = found.map(s => {
            const stale = s.age > 30;
            const running = s.running === 1 && !stale;
            const dot = `<span class="dot dot--${running ? 'ok' : stale ? 'muted' : 'crit'}"></span>`;
            return `<tr>
              <td>${dot} ${_esc(s.svc)}</td>
              <td>${s.pid != null ? Math.round(s.pid) : '—'}</td>
              <td>${s.rss_mb != null ? s.rss_mb.toFixed(0) + ' MB' : '—'}</td>
              <td>${_fmtUptime(s.uptime_s)}</td>
            </tr>`;
          }).join('');
        }
      }
    }
  } catch (e) {
    console.error('fetchServicesAndInflux:', e);
  }
}

// Manager dashboard — Agents card. Polls /api/agents (the same source
// the Admin tab uses) and renders a compact hostname/status/last-sample
// table. Status reflects `liveness` (live/stale/down) for approved
// agents, or the registration status otherwise.
async function fetchManagerAgentsCard() {
  if (document.hidden) return;
  // mgrAgentsTable lives only on Dashboard → Manager sub-tab. Skip the
  // poll on every other tab/sub-tab so the 10s tick doesn't slam the
  // backend while the user is somewhere else.
  if (_activeTab !== 'dashboard' || (_subTabState && _subTabState['dashboard']) !== 'manager') {
    return;
  }
  const tbody = document.getElementById('mgrAgentsTable');
  if (!tbody) return;
  try {
    const r = await fetch('/api/agents');
    if (!r.ok) {
      tbody.innerHTML = `<tr><td colspan="3" style="color:var(--fg-dim);font-size:0.85em;">GET /api/agents → ${r.status}</td></tr>`;
      _dashSetStatus('mgr-agents', 'dash-off');
      return;
    }
    const d = await r.json();
    const agents = (d.agents || []).slice().sort((a,b) =>
      (a.hostname || a.agent_id || '').localeCompare(b.hostname || b.agent_id || ''));
    if (!agents.length) {
      tbody.innerHTML = '<tr><td colspan="3" style="color:var(--fg-dim);font-size:0.85em;">No agents registered.</td></tr>';
      _dashSetStatus('mgr-agents', 'dash-off');
      return;
    }
    let anyDown = false, anyStale = false, anyLive = false;
    tbody.innerHTML = agents.map(a => {
      const host = a.hostname || a.agent_id || '(unknown)';
      const status = a.status || 'unknown';
      const live = a.liveness || 'unknown';
      // Decide dot + label. For approved agents we use liveness; for
      // pending/disabled we surface the registration status instead.
      let dotMod = 'muted', label = live;
      if (status !== 'approved') {
        label = status;
        dotMod = status === 'pending' ? 'warn' : 'muted';
      } else if (live === 'live') { dotMod = 'ok'; anyLive = true; }
      else if (live === 'stale')  { dotMod = 'warn'; anyStale = true; }
      else if (live === 'down')   { dotMod = 'crit'; anyDown = true; }
      else                        { dotMod = 'muted'; }
      const dot = `<span class="dot dot--${dotMod}" style="margin-right:6px;"></span>`;
      return `<tr>
        <td>${host.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</td>
        <td>${dot}${label}</td>
        <td>${adminAgo(a.last_heartbeat)}</td>
      </tr>`;
    }).join('');
    const cls = anyDown ? 'dash-crit' : anyStale ? 'dash-warn' : anyLive ? 'dash-ok' : 'dash-off';
    _dashSetStatus('mgr-agents', cls);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="3" style="color:var(--fg-dim);font-size:0.85em;">error: ${e.message}</td></tr>`;
    _dashSetStatus('mgr-agents', 'dash-off');
  }
}

async function fetchManagerStreamsCard() {
  if (document.hidden) return;
  // mgrStreamsTable lives only on Dashboard → Manager sub-tab. Skip the poll
  // on every other tab/sub-tab so the 10s tick doesn't fan out to every agent
  // while the card isn't visible (and 403 for operator sessions).
  if (_activeTab !== 'dashboard' || (_subTabState && _subTabState['dashboard']) !== 'manager') {
    return;
  }
  const summary = document.getElementById('mgrStreamsSummary');
  const tbody = document.getElementById('mgrStreamsTable');
  const badge = document.getElementById('mgrStreamsBadge');
  if (!summary || !tbody) return;
  const crit = 'color:var(--crit,#e0556b)';
  const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  try {
    const r = await fetch('/api/admin/stream-stats');
    if (!r.ok) {
      tbody.innerHTML = `<tr><td colspan="5" style="color:var(--fg-dim);font-size:0.85em;">GET /api/admin/stream-stats → ${r.status}</td></tr>`;
      _dashSetStatus('mgr-streams', 'dash-off');
      return;
    }
    const d = await r.json();
    const pool = d.pool || {};
    const saturated = (pool.refusals > 0) || (pool.active >= pool.limit);
    if (badge) {
      badge.className = 'status ' + (saturated ? 'status--crit' : 'status--ok');
      badge.innerHTML = '<span class="status__dot"></span>' + (saturated ? 'saturated' : 'ok');
    }
    const cell = (label, val, warn) =>
      `<div><span style="color:var(--fg-dim);">${label}:</span> <b style="${warn ? crit : ''}">${val}</b></div>`;
    const v = (x) => (x === null || x === undefined) ? '–' : x;
    summary.innerHTML =
      cell('Pool active', `${v(pool.active)} / ${v(pool.limit)}`, pool.active >= pool.limit) +
      cell('Peak', v(pool.peak), pool.peak >= pool.limit) +
      cell('Refusals', v(pool.refusals), pool.refusals > 0) +
      cell('Workers busy', `${v(d.worker_threads_busy)} / ${v(d.worker_threads)}`, false) +
      cell('Worker backlog', v(d.worker_backlog), d.worker_backlog > 0) +
      cell('Browser conns', v(d.browser_connections), false) +
      cell('Agent conns', v(d.agent_connections), false) +
      cell('Off-pool SSE', d.sse_daemon_running ? `${v(d.sse_daemon_streams)} active` : 'off');
    const agents = d.agents || [];
    if (!agents.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--fg-dim);font-size:0.85em;">No agents.</td></tr>';
    } else {
      tbody.innerHTML = agents.map(a => {
        if (!a.reachable)
          return `<tr><td>${esc(a.hostname)}</td><td colspan="4" style="color:var(--fg-dim);font-size:0.85em;">unreachable</td></tr>`;
        const aw = (a.active >= a.cap) ? crit : '';
        const rw = (a.refusals > 0) ? crit : '';
        return `<tr><td>${esc(a.hostname)}</td><td style="${aw}">${v(a.active)}/${v(a.cap)}</td>`
             + `<td>${v(a.peak)}</td><td style="${rw}">${v(a.refusals)}</td><td>${v(a.terminal_sessions)}</td></tr>`;
      }).join('');
    }
    _dashSetStatus('mgr-streams', saturated ? 'dash-crit' : 'dash-ok');
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:var(--fg-dim);font-size:0.85em;">error: ${e.message}</td></tr>`;
    _dashSetStatus('mgr-streams', 'dash-off');
  }
}
