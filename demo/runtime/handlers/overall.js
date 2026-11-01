// Demo handlers for the fleet and machine views: identity, config, agents, metrics, history, energy, streams.
(function (self) {
  var R = self.DemoRouter;
  var F = self.DemoFleet;
  var S = self.DemoSeries;
  var DEFAULT = { llama: 'atlas', lms: 'studio', vllm: 'atlas' };
  var SPEC = {
    atlas:  { cores: 16, cpu: 'AMD Ryzen 9 7950X', gpuCap: 450, tokScale: 1 },
    forge:  { cores: 12, cpu: 'AMD Ryzen 7 5800X3D', gpuCap: 350, tokScale: 0.7 },
    studio: { cores: 24, cpu: 'Apple M2 Ultra', gpuCap: 0, tokScale: 0.45 },
    quill:  { cores: 8,  cpu: 'Intel Core i5-12400', gpuCap: 0, tokScale: 0.12 }
  };
  var GB = 1073741824;
  var VERSION = self.DEMO_RELEASE || 'dev';

  function sec(ctx) { return Math.floor(ctx.now / 1000); }
  function iso(t) { return new Date(t * 1000).toISOString(); }
  function round(v, d) { var k = Math.pow(10, d || 0); return Math.round(v * k) / k; }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function hasGpu(m) { return m.gpus.length > 0 && m.os !== 'macos'; }
  function vramTotal(m) { var s = 0; m.gpus.forEach(function (g) { s += g.vramGb; }); return s; }
  function has(m, prov) { return m.providers.indexOf(prov) >= 0; }

  function machineFor(q) {
    var m = q && q.get('agent') ? F.byId(q.get('agent')) : null;
    return m || F.byId(DEFAULT.llama);
  }
  function providerMachine(q, prov) {
    var m = q && q.get('agent') ? F.byId(q.get('agent')) : null;
    return m && has(m, prov) ? m : F.byId(DEFAULT[prov]);
  }
  function modelOf(m, prov) { return prov === 'vllm' ? m.models[2] : m.models[0]; }

  // 0..1 level of vLLM's short request run in every half hour: 8 minutes at a fixed offset, 30 s ramps.
  function vllmTrickle(t) {
    var slot = Math.floor(t / 1800), start = slot * 1800 + (Math.imul(slot, 2654435761) >>> 0) % 1320;
    var x = t - start;
    return x <= 0 || x >= 480 ? 0 : clamp(Math.min(x / 30, (480 - x) / 30), 0, 1);
  }

  // Per-machine throughput; vLLM on the shared host runs on a shifted clock plus its half-hourly run.
  function tps(m, prov, t) {
    var shift = prov === 'vllm' ? 137 : 0;
    var k = SPEC[m.id].tokScale * (prov === 'vllm' ? 0.6 : 1);
    var v = S.value(m.id, 'tok_s', t + shift);
    if (prov === 'vllm') v = Math.max(v, vllmTrickle(t) * (40 + 12 * Math.sin(t / 23)));
    return round(v * k, 2);
  }
  function pps(m, prov, t) { return round(tps(m, prov, t) * 8, 2); }
  function busy(m, prov, t) {
    return S.busy(m.id, t + (prov === 'vllm' ? 137 : 0)) || (prov === 'vllm' && vllmTrickle(t) > 0);
  }
  // Lifetime counters and context size per machine and provider; the counters grow slowly from a fixed start date.
  var LIFE0 = 1767225600;
  var LIFE = {
    'atlas|llama': { ctx: 32768, gen: 1100000, prompt: 9500000, gr: 0.09, pr: 0.8 },
    'atlas|vllm':  { ctx: 16384, gen: 400000, prompt: 3200000, gr: 0.03, pr: 0.28 },
    'forge|llama': { ctx: 16384, gen: 800000, prompt: 7000000, gr: 0.05, pr: 0.45 },
    'studio|lms':  { ctx: 65536, gen: 500000, prompt: 4600000, gr: 0.03, pr: 0.26 },
    'quill|llama': { ctx: 8192, gen: 200000, prompt: 1800000, gr: 0.015, pr: 0.12 }
  };
  var NO_LIFE = { ctx: 0, gen: 0, prompt: 0, gr: 0, pr: 0 };
  function life(m, prov) { return LIFE[(m && m.id) + '|' + prov] || NO_LIFE; }
  function lifeAge(t) { return Math.max(0, t - LIFE0); }
  function genTotal(m, prov, t) { var l = life(m, prov); return l.gen + Math.floor(lifeAge(t) * l.gr); }
  function promptTotal(m, prov, t) { var l = life(m, prov); return l.prompt + Math.floor(lifeAge(t) * l.pr); }
  function ctxOf(m, prov) { return life(m, prov).ctx; }

  function reading(v, unit) { return { value: round(v, 1), unit: unit }; }

  // UPS, power supply, liquid cooler and fan controller readings for the one machine that has them.
  function hardware(m, t, lvl) {
    var out = round(S.value(m.id, 'power_w', t) * 0.95, 1);
    var fanRpm = Math.round(780 + 640 * lvl);
    var aio = 28.5 + 7 * lvl + 0.4 * Math.sin(t / 61);
    return {
      ups: { percent: 100, state: 'Online', warning_level: 'None', on_battery: false, time_to_empty: '40.2 minutes', time_to_full: null },
      liquidctl: {
        aio: { _name: 'Corsair Hydro H150i Elite', 'Liquid temperature': reading(aio, '°C'),
          'Pump speed': reading(2350 + 90 * lvl, 'rpm'), 'Pump duty': reading(58 + 22 * lvl, '%'),
          'Fan speed': reading(fanRpm, 'rpm'), 'Fan duty': reading(32 + 38 * lvl, '%') },
        psu: { _name: 'Corsair HX1200i', 'VRM temperature': reading(41 + 9 * lvl, '°C'),
          'Case temperature': reading(33 + 6 * lvl, '°C'), 'Fan speed': reading(0 + 520 * lvl, 'rpm'),
          'Input voltage': reading(229.6, 'V'), 'Total power output': reading(out, 'W'),
          'Estimated input power': reading(out / 0.92, 'W'), 'Estimated efficiency': reading(92, '%') },
        smart: { _name: 'NZXT Smart Device V2', fans: [1, 2, 3].map(function (id) {
          return { id: id, control_mode: 'PWM', duty: Math.round(40 + 30 * lvl), speed: { value: Math.round(fanRpm * (0.9 + 0.05 * id)), unit: 'rpm' },
            voltage_v: round(8.4 + 2.2 * lvl, 2), current_ma: Math.round(150 + 90 * lvl) };
        }) }
      }
    };
  }

  function systemBlock(m, t) {
    var sp = SPEC[m.id];
    var cpu = round(S.value(m.id, 'cpu_pct', t), 1);
    var lvl = S.value(m.id, 'tok_s', t) / 140;
    var ramPct = round(clamp(38 + 0.3 * cpu + 12 * lvl, 0, 96), 1);
    var total = m.ramGb * GB;
    var used = Math.round(total * ramPct / 100);
    var cores = [];
    for (var i = 0; i < sp.cores; i++) {
      cores.push(round(clamp(cpu * (0.55 + 0.9 * Math.abs(Math.sin(i * 1.7 + t / 13))), 0, 100), 1));
    }
    var diskTotal = m.diskGb * GB;
    var sys = {
      ts: iso(t),
      cpu_name: sp.cpu,
      cpu_total: cpu,
      cpu_temp_c: round(S.value(m.id, 'temp_c', t) - 6, 1),
      cpu_governor: m.os === 'macos' ? 'auto' : 'performance',
      cpu_per_core: cores,
      ram: {
        percent: ramPct, total_bytes: total, used_bytes: used, available_bytes: total - used,
        cached_bytes: Math.round(total * 0.18), buffers_bytes: Math.round(total * 0.01)
      },
      swap: { used_bytes: Math.round(0.4 * GB), free_bytes: Math.round(7.6 * GB) },
      net: {
        bytes_sent_per_sec: Math.round(30000 + 900000 * lvl),
        bytes_recv_per_sec: Math.round(45000 + 1400000 * lvl)
      },
      disk: [
        { mountpoint: '/', percent: m.diskPct, total_bytes: diskTotal, used_bytes: Math.round(diskTotal * m.diskPct / 100) },
        { mountpoint: '/data', percent: 63.2, total_bytes: diskTotal * 2, used_bytes: Math.round(diskTotal * 2 * 0.632) }
      ],
      disk_io: {
        read_bytes_per_sec: Math.round(120000 + 6000000 * lvl),
        write_bytes_per_sec: Math.round(80000 + 1500000 * lvl)
      }
    };
    if (m.id === 'atlas') {
      var hw = hardware(m, t, lvl);
      sys.ups = hw.ups;
      sys.liquidctl = hw.liquidctl;
    }
    return sys;
  }

  function gpuBlock(m, t) {
    if (!hasGpu(m)) return null;
    var total = vramTotal(m);
    var used = S.value(m.id, 'vram_used_gb', t);
    var temp = round(S.value(m.id, 'temp_c', t), 1);
    return {
      name: m.gpus[0].name + (m.gpus.length > 1 ? ' x' + m.gpus.length : ''),
      temperature_c: temp, temperature_junction_c: round(temp + 9, 1), temperature_memory_c: round(temp + 6, 1),
      vram_usage_percent: round(100 * used / total, 1), vram_used_mb: Math.round(used * 1024),
      gpu_util_percent: round(S.value(m.id, 'gpu_util', t), 1),
      power_watts: round(S.value(m.id, 'power_w', t) * 0.8, 1), power_cap_watts: SPEC[m.id].gpuCap * m.gpus.length,
      fan1_rpm: Math.round(900 + 22 * temp), sclk_mhz: Math.round(1400 + 12 * S.value(m.id, 'gpu_util', t)),
      mclk_mhz: 10501, performance_level: 'auto', power_profile: 'default'
    };
  }

  function llamaBlock(m, t) {
    var b = busy(m, 'llama', t);
    var kv = round(clamp(0.12 + 0.5 * (tps(m, 'llama', t) / 100), 0, 0.9), 3);
    return {
      state: 'awake', sleeping: false, model: modelOf(m, 'llama'),
      tokens_per_second: tps(m, 'llama', t), prompt_tokens_per_second: pps(m, 'llama', t),
      total_tokens_generated: genTotal(m, 'llama', t), total_tokens_prompted: promptTotal(m, 'llama', t),
      n_decode_total: Math.floor(genTotal(m, 'llama', t) * 0.7), n_busy_slots_per_decode: b ? 1.4 : 0,
      n_tokens_max: ctxOf(m, 'llama'), kv_cache_usage_ratio: kv, kv_cache_tokens: Math.round(kv * ctxOf(m, 'llama')),
      cache_reuse: { state: 'active', ratio: 0.62, cached: 18420, processed: 29710 },
      spec_accept: { state: 'off' },
      n_remain: b ? 180 : 0, total_slots: 4, modalities: { vision: false, audio: false },
      chat_template_len: 7600, chat_template: '',
      active_slots: b ? 1 : 0, requests_processing: b ? 1 : 0, requests_deferred: 0
    };
  }

  function llamaMetrics(m, t) {
    var out = systemBlock(m, t);
    var g = gpuBlock(m, t);
    if (g) out.gpu = g;
    out.llama = llamaBlock(m, t);
    return out;
  }

  function llamaState(m, nowMs) {
    var t = Math.floor(nowMs / 1000);
    return {
      state: 'awake', model: modelOf(m, 'llama'), port: 8080, agent_online: true,
      agent_age_s: round(0.5 + (t % 4) * 0.6, 1), age_s: round(0.5 + (t % 4) * 0.6, 1), stale: false,
      aggregate: 'active',
      residency: { aggregate: 'active', models: [], servers: { llama: 'up' }, source: 'legacy', ts: null },
      power: null, pulled: true, build_method: 'source'
    };
  }

  function macPower(m, t) {
    var w = S.value(m.id, 'power_w', t);
    var gu = S.value(m.id, 'gpu_util', t);
    var cpu = S.value(m.id, 'cpu_pct', t);
    return {
      soc_total_w: round(w, 2), cpu_package_w: round(w * 0.3, 2), gpu_w: round(w * 0.5, 2), ane_w: round(0.2 + w * 0.01, 2),
      thermal_pressure_n: 0, thermal_pressure: 'Nominal', gpu_busy_pct: round(gu, 1),
      pcore_freq_mhz: Math.round(2400 + 10 * cpu), pcore_util_pct: round(cpu * 1.3, 1),
      ecore_freq_mhz: Math.round(1500 + 4 * cpu), ecore_util_pct: round(cpu * 0.7, 1),
      gpu_freq_mhz: Math.round(700 + 6 * gu), net_in_pkts_s: Math.round(300 + 40 * gu), net_out_pkts_s: Math.round(220 + 30 * gu)
    };
  }

  function lmsMetrics(m, t, light, ctx) {
    var out = systemBlock(m, t);
    var b = busy(m, 'lms', t);
    var ps = [{
      identifier: m.models[0], model: m.models[0], status: b ? 'GENERATING' : 'IDLE',
      size: '39.8 GB', context: ctxOf(m, 'lms'), parallel: 4, quant: '4bit'
    }];
    out.agent_online = true;
    out.agent_age_s = round(0.5 + (t % 4) * 0.6, 1);
    out.agent_id = m.id;
    out.ps = ps;
    out.models = m.models.map(function (id) { return { id: id }; });
    out.server = { on: true, port: 1235 };
    out.mac_power = macPower(m, t);
    if (!light) {
      out.gateway_tokens = { gen: genTotal(m, 'lms', t), prompt: promptTotal(m, 'lms', t) };
      out.gateway_rates = { gen_tps: tps(m, 'lms', t), prompt_tps: pps(m, 'lms', t), ts: iso(t) };
      out.gateway_inflight = b ? 1 : 0;
    }
    return out;
  }

  function vllmMetrics(m, t) {
    var out = systemBlock(m, t);
    var b = busy(m, 'vllm', t);
    var g = gpuBlock(m, t);
    if (g) out.gpu = g;
    out.agent_online = true;
    out.agent_age_s = round(0.5 + (t % 4) * 0.6, 1);
    out.agent_id = m.id;
    out.vllm = {
      state: 'running', model: modelOf(m, 'vllm'), models: [modelOf(m, 'vllm')], port: 8000,
      requests_running: b ? 2 : 0, requests_waiting: 0,
      kv_cache_usage_pct: round(clamp(8 + 0.5 * tps(m, 'vllm', t), 0, 90), 1),
      tokens_per_second: tps(m, 'vllm', t), prompt_tokens_per_second: pps(m, 'vllm', t),
      total_tokens_generated: genTotal(m, 'vllm', t), total_tokens_prompted: promptTotal(m, 'vllm', t),
      max_model_len: ctxOf(m, 'vllm')
    };
    return out;
  }

  // ---- fleet aggregates ----
  function providerMachines(prov) { return F.machines.filter(function (m) { return has(m, prov); }); }

  function gpuRollup(rows) {
    var acc = { max_temp_c: 0, max_vram_pct: 0, total_power_watts: 0, thermal_crit_count: 0 };
    rows.forEach(function (r) {
      var g = r.gpu;
      if (g) {
        acc.max_temp_c = Math.max(acc.max_temp_c, g.temperature_c);
        acc.max_vram_pct = Math.max(acc.max_vram_pct, g.vram_usage_percent);
      }
      acc.total_power_watts += r.power;
    });
    acc.max_temp_c = round(acc.max_temp_c, 1);
    acc.max_vram_pct = round(acc.max_vram_pct, 1);
    acc.total_power_watts = round(acc.total_power_watts, 1);
    return acc;
  }

  function aggregate(prov, t) {
    var ms = providerMachines(prov);
    var rows = ms.map(function (m) {
      return { m: m, gpu: gpuBlock(m, t), power: round(S.value(m.id, 'power_w', t), 1), b: busy(m, prov, t),
        tps: tps(m, prov, t), pps: pps(m, prov, t) };
    });
    var totalTps = round(rows.reduce(function (s, r) { return s + r.tps; }, 0), 2);
    var totalPps = round(rows.reduce(function (s, r) { return s + r.pps; }, 0), 2);
    var gpu = gpuRollup(rows);
    var age = round(0.5 + (t % 4) * 0.6, 1);
    var out = {
      provider: prov, throughput: { total_tps: totalTps, total_pps: totalPps }, gpu: gpu,
      agent_count_total: ms.length, agent_count_online: ms.length
    };
    if (prov === 'llama') {
      out.awake_agent_count = ms.length;
      out.active_models = ms.map(function (m) { return modelOf(m, 'llama'); }).sort();
      out.active_model_count = out.active_models.length;
      out.agents = rows.map(function (r) {
        return { agent_id: r.m.id, online: true, state: 'awake', model: modelOf(r.m, 'llama'),
          tokens_per_second: r.tps, prompt_tokens_per_second: r.pps, ctx: ctxOf(r.m, 'llama'),
          total_tokens_generated: genTotal(r.m, 'llama', t), total_tokens_prompted: promptTotal(r.m, 'llama', t),
          power_watts: r.power, thermal_crit: false, age_s: age };
      });
    } else if (prov === 'lms') {
      out.server_on_count = ms.length;
      out.busy_agent_count = rows.filter(function (r) { return r.b; }).length;
      out.loaded_model_count_total = ms.length;
      out.process_count_total = ms.length;
      out.busy_process_count_total = out.busy_agent_count;
      out.agents = rows.map(function (r) {
        return { agent_id: r.m.id, online: true, server_on: true, loaded_model_count: 1, ps_error: null,
          loaded_models: [r.m.models[0]], busy_process_count: r.b ? 1 : 0, ctx: ctxOf(r.m, 'lms'),
          total_tokens_generated: genTotal(r.m, 'lms', t), total_tokens_prompted: promptTotal(r.m, 'lms', t),
          power_watts: r.power, thermal_crit: false, age_s: age };
      });
    } else {
      out.server_on_count = ms.length;
      out.requests_running_total = rows.reduce(function (s, r) { return s + (r.b ? 2 : 0); }, 0);
      out.requests_waiting_total = 0;
      out.max_kv_cache_pct = round(Math.max.apply(null, rows.map(function (r) { return clamp(8 + 0.5 * r.tps, 0, 90); })), 1);
      out.total_gpu_power_watts = gpu.total_power_watts;
      out.active_models = ms.map(function (m) { return modelOf(m, 'vllm'); }).sort();
      out.active_model_count = out.active_models.length;
      out.agents = rows.map(function (r) {
        return { agent_id: r.m.id, online: true, server_on: true, model: modelOf(r.m, 'vllm'),
          requests_running: r.b ? 2 : 0, tokens_per_second: r.tps, ctx: ctxOf(r.m, 'vllm'),
          total_tokens_generated: genTotal(r.m, 'vllm', t), total_tokens_prompted: promptTotal(r.m, 'vllm', t),
          power_watts: r.power, thermal_crit: false, age_s: age };
      });
    }
    return out;
  }

  // ---- history ----
  function historyPoints(q, t) {
    var since = parseInt(q.get('since_minutes'), 10);
    if (!(since >= 1)) since = 60;
    since = Math.min(since, 43200);
    var maxRows = parseInt(q.get('max_rows'), 10);
    var limit = parseInt(q.get('limit'), 10);
    var n = 300;
    if (maxRows >= 2) n = Math.min(n, maxRows);
    if (limit >= 2) n = Math.min(n, limit);
    var span = since * 60;
    n = Math.max(2, Math.min(n, Math.ceil(span / 5) + 1));
    var step = span / (n - 1);
    var pts = [];
    for (var i = 0; i < n; i++) pts.push(Math.floor(t - span + i * step));
    return pts;
  }

  function hostRow(m, ts) {
    var s = systemBlock(m, ts);
    var row = {
      ts: iso(ts), cpu_total: s.cpu_total, ram_percent: s.ram.percent,
      net_sent: s.net.bytes_sent_per_sec, net_recv: s.net.bytes_recv_per_sec,
      io_read: s.disk_io.read_bytes_per_sec, io_write: s.disk_io.write_bytes_per_sec,
      disk_root_pct: s.disk[0].percent
    };
    if (hasGpu(m)) {
      var g = gpuBlock(m, ts);
      row.gpu_util = g.gpu_util_percent; row.gpu_vram = g.vram_usage_percent;
      row.gpu_vram_used = Math.round(g.vram_used_mb * 1048576); row.gpu_vram_total = vramTotal(m) * GB;
      row.gpu_temp = g.temperature_c; row.gpu_power = g.power_watts;
    }
    if (s.liquidctl) {
      row.aio_temp = s.liquidctl.aio['Liquid temperature'].value;
      row.psu_out = s.liquidctl.psu['Total power output'].value;
      row.psu_in = s.liquidctl.psu['Estimated input power'].value;
      row.ups_percent = s.ups.percent;
    }
    if (m.os === 'macos') row.mac_gpu_busy = round(S.value(m.id, 'gpu_util', ts), 1);
    if (has(m, 'llama')) {
      row.llama_tps = tps(m, 'llama', ts); row.llama_pps = pps(m, 'llama', ts);
      row.llama_ctx = ctxOf(m, 'llama'); row.llama_gen_tokens = genTotal(m, 'llama', ts);
    }
    if (has(m, 'lms')) { row.lms_tps = tps(m, 'lms', ts); row.lms_pps = pps(m, 'lms', ts); }
    if (has(m, 'vllm')) {
      row.vllm_tps = tps(m, 'vllm', ts); row.vllm_pps = pps(m, 'vllm', ts);
      row.vllm_kv = round(clamp(8 + 0.5 * row.vllm_tps, 0, 90), 1);
    }
    return row;
  }

  function fleetRow(ts) {
    var row = { ts: iso(ts), llama_tps: 0, llama_pps: 0, lms_tps: 0, lms_pps: 0, vllm_tps: 0, vllm_pps: 0, psu_in: 0 };
    F.machines.forEach(function (m) {
      ['llama', 'lms', 'vllm'].forEach(function (p) {
        if (!has(m, p)) return;
        row[p + '_tps'] = round(row[p + '_tps'] + tps(m, p, ts), 2);
        row[p + '_pps'] = round(row[p + '_pps'] + pps(m, p, ts), 2);
      });
      row.psu_in += S.value(m.id, 'power_w', ts);
    });
    row.psu_in = round(row.psu_in, 1);
    return row;
  }

  // ---- energy ----
  var PRICE = 0.15, CLOUD_IN = 0.15, CLOUD_OUT = 0.6;

  function numArg(q, k, d) { var v = parseFloat(q.get(k)); return isFinite(v) ? v : d; }

  function windowOf(q, t) {
    var days = parseInt(q.get('days'), 10);
    var end = (Math.floor(t / 3600) + 1) * 3600;
    if (days >= 1) {
      days = Math.min(days, 366);
      return { start: end - days * 86400, end: end, label: 'last ' + days + ' days', days: days };
    }
    if (/^(1|true|yes)$/i.test(q.get('ytd') || '')) {
      var y = new Date(t * 1000).getUTCFullYear();
      var s = Math.floor(Date.UTC(y, 0, 1) / 1000);
      return { start: s, end: end, label: 'YTD ' + y, days: Math.max(1, Math.ceil((end - s) / 86400)) };
    }
    var month = q.get('month') || new Date(t * 1000).toISOString().slice(0, 7);
    var mm = /^(\d{4})-(\d{2})$/.exec(month);
    var ms = mm ? Date.UTC(+mm[1], +mm[2] - 1, 1) : Date.UTC(1970, 0, 1);
    var me = mm ? Date.UTC(+mm[1], +mm[2], 1) : ms + 30 * 86400000;
    return { start: Math.floor(ms / 1000), end: Math.floor(me / 1000), label: month, days: Math.round((me - ms) / 86400000) };
  }

  function hostEnergy(m, windowS, price, cin, cout) {
    var idle = m.powerW.idle + 0.05 * (m.powerW.load - m.powerW.idle);
    var activeW = m.powerW.idle + 0.8 * (m.powerW.load - m.powerW.idle);
    var activeS = 0.38 * windowS;
    var energyWh = (activeW * activeS + idle * (windowS - activeS)) / 3600;
    var activeWh = activeW * activeS / 3600;
    var gen = Math.round(activeS * 40 * SPEC[m.id].tokScale);
    var prompt = gen * 6;
    var kwh = energyWh / 1000, cost = kwh * price, activeCost = activeWh / 1000 * price;
    return {
      m: m, energyWh: energyWh, activeWh: activeWh, activeS: activeS, gen: gen, prompt: prompt,
      row: {
        observed_s: round(windowS, 1), active_s: round(activeS, 1), active_pct: 38, coverage_pct: 100, power_coverage_pct: 100,
        kwh: round(kwh, 3), active_kwh: round(activeWh / 1000, 3), idle_kwh: round(kwh - activeWh / 1000, 3),
        avg_watts: round(energyWh * 3600 / windowS, 1), cost_usd: round(cost, 2),
        idle_cost_usd: round(cost - activeCost, 2), tokens_gen: gen, tokens_prompt: prompt,
        usd_per_mtok: round(cost / gen * 1e6, 4), usd_per_mtok_active: round(activeCost / gen * 1e6, 4),
        cloud_cost_usd: round(prompt / 1e6 * cin + gen / 1e6 * cout, 2),
        power_source: m.os === 'macos' ? 'mac' : hasGpu(m) ? 'gpu' : 'psu', has_power: true, has_tokens: true
      }
    };
  }

  function energySummary(ctx) {
    var q = ctx.query, t = sec(ctx);
    var price = numArg(q, 'price_kwh', PRICE), cin = numArg(q, 'cloud_in', CLOUD_IN), cout = numArg(q, 'cloud_out', CLOUD_OUT);
    var w = windowOf(q, t);
    var windowS = Math.max(3600, Math.min(w.end, t) - w.start);
    var hosts = F.machines.map(function (m) { return hostEnergy(m, windowS, price, cin, cout); });
    var sum = function (k) { return hosts.reduce(function (s, h) { return s + h[k]; }, 0); };
    var energyWh = sum('energyWh'), activeWh = sum('activeWh'), activeS = sum('activeS'), gen = sum('gen'), prompt = sum('prompt');
    var kwh = energyWh / 1000, cost = kwh * price, activeCost = activeWh / 1000 * price;
    var cloud = prompt / 1e6 * cin + gen / 1e6 * cout;
    var observed = windowS * hosts.length;
    var totals = {
      observed_s: round(observed, 1), active_s: round(activeS, 1), active_pct: 38, coverage_pct: 100, power_coverage_pct: 100,
      kwh: round(kwh, 3), active_kwh: round(activeWh / 1000, 3), idle_kwh: round(kwh - activeWh / 1000, 3),
      avg_watts: round(energyWh * 3600 / observed, 1), cost_usd: round(cost, 2), idle_cost_usd: round(cost - activeCost, 2),
      tokens_gen: gen, tokens_prompt: prompt, usd_per_mtok: round(cost / gen * 1e6, 4),
      usd_per_mtok_active: round(activeCost / gen * 1e6, 4), cloud_cost_usd: round(cloud, 2),
      power_source: 'gpu', has_power: true, has_tokens: true, mtok_energy_coverage_pct: 100
    };
    var hostRows = hosts.map(function (h) {
      return Object.assign({ agent_id: h.m.id, hostname: h.m.hostname }, h.row);
    }).sort(function (a, b) { return b.kwh - a.kwh; });
    var models = [];
    F.machines.forEach(function (m, i) {
      var h = hosts[i];
      var servedModels = m.models.slice(0, 2);
      servedModels.forEach(function (name, j) {
        var share = j === 0 ? 0.7 : 0.3;
        var g = Math.round(h.gen * share), p = g * 6;
        var wh = h.energyWh * share, c = wh / 1000 * price;
        models.push({
          observed_s: round(windowS, 1), active_s: round(h.activeS * share, 1), active_pct: 38,
          kwh: round(wh / 1000, 3), active_kwh: round(h.activeWh * share / 1000, 3),
          idle_kwh: round((wh - h.activeWh * share) / 1000, 3), avg_watts: round(h.row.avg_watts * share, 1),
          cost_usd: round(c, 2), idle_cost_usd: round(c - h.activeWh * share / 1000 * price, 2),
          tokens_gen: g, tokens_prompt: p, usd_per_mtok: round(c / g * 1e6, 4),
          usd_per_mtok_active: round(h.activeWh * share / 1000 * price / g * 1e6, 4),
          cloud_cost_usd: round(p / 1e6 * cin + g / 1e6 * cout, 2), has_power: true, has_tokens: true,
          resident_pct: j === 0 ? 96.5 : 41.2, model: name, hosts: [m.hostname],
          energy_share_pct: round(100 * wh / energyWh, 1)
        });
      });
    });
    models.sort(function (a, b) { return b.kwh - a.kwh; });
    return {
      ok: true,
      window: { label: w.label, start_ts: w.start, end_ts: w.end, elapsed_s: Math.round(windowS) },
      since_ts: t - 45 * 86400,
      config: { price_kwh: PRICE, cloud_price_in_per_mtok: CLOUD_IN, cloud_price_out_per_mtok: CLOUD_OUT,
        cloud_price_label: 'budget cloud API tier (GPT-4o-mini class, 2026-07 list)' },
      price_kwh: price, cloud_in: cin, cloud_out: cout,
      totals: totals, hosts: hostRows, models: models, savings_usd: round(cloud - cost, 2)
    };
  }

  function energyHourly(ctx) {
    var q = ctx.query, t = sec(ctx);
    var cap = 45 * 24;
    var end = (Math.floor(t / 3600) + 1) * 3600;
    var hours, label, truncated = false;
    if (q.get('days') || q.get('month') || q.get('ytd') || q.get('start')) {
      var w = windowOf(q, t);
      hours = Math.round((w.end - w.start) / 3600);
      label = w.label;
      end = Math.min(w.end, end);
      truncated = hours > cap;
      hours = Math.min(hours, cap);
    } else {
      hours = clamp(parseInt(q.get('hours'), 10) || 168, 1, cap);
      label = 'last ' + hours + ' hours';
    }
    var start = end - hours * 3600;
    var machines = q.get('agent') && F.byId(q.get('agent')) ? [F.byId(q.get('agent'))] : F.machines;
    var rows = [];
    for (var h = start; h < end && h <= t; h += 3600) {
      var r = { hour_ts: h, energy_wh: 0, active_energy_wh: 0, tokens_gen: 0, tokens_prompt: 0, observed_s: 0, active_s: 0 };
      machines.forEach(function (m) {
        var activeN = 0, wSum = 0, g = 0;
        for (var k = 0; k < 4; k++) {
          var ts = h + 450 + k * 900;
          wSum += S.value(m.id, 'power_w', ts);
          if (S.busy(m.id, ts)) { activeN++; g += tps(m, 'llama', ts); }
        }
        var avgW = wSum / 4, frac = activeN / 4;
        r.energy_wh += avgW;
        r.active_energy_wh += frac * (m.powerW.idle + 0.8 * (m.powerW.load - m.powerW.idle));
        r.tokens_gen += Math.round(g / 4 * 3600 * frac);
        r.observed_s += 3600;
        r.active_s += 3600 * frac;
      });
      r.tokens_prompt = r.tokens_gen * 6;
      r.energy_wh = round(r.energy_wh, 2);
      r.active_energy_wh = round(Math.min(r.active_energy_wh, r.energy_wh), 2);
      r.active_s = round(r.active_s, 1);
      rows.push(r);
    }
    return { ok: true, label: label, truncated: truncated, cap_days: 45, hours: hours, start_ts: start, end_ts: end, rows: rows };
  }

  // ---- agents ----
  function agentRow(m, t) {
    var caps = { image_gen: false, llama: has(m, 'llama'), lms: has(m, 'lms'), monitor_alarm_engine: m.id === 'atlas',
      monitor_manager: m.id === 'atlas', openclaw: false, perf_controller: false, sysperf: true, vllm: has(m, 'vllm') };
    var base = iso(t - 40 * 86400);
    var colocated = m.id === 'atlas' ? [
      { role: 'manager', version: VERSION }, { role: 'alarm_engine', version: VERSION }, { role: 'influxdb', version: 'v2.9.1' }
    ] : [];
    return {
      agent_id: m.id, agent_user: 'alex', approved_at: base, approved_by: '192.0.2.1',
      bind_url: 'https://' + m.ip + ':8082', capabilities: caps, cert_role_sent_at: base,
      colocated_infra: colocated, description: '',
      fingerprint: 'sha256:' + m.id + '0123456789abcdef0123456789abcdef0123456789abcdef01234567',
      first_seen: base, fp_version: VERSION, hostname: m.hostname, image_gen_port: 1234, is_host_agent: m.id === 'atlas',
      last_cert_issued_at: base, last_heartbeat: iso(t - 2),
      last_heartbeat_data: {
        clock_skew_s: 0.001, collection_enabled: true, control_channel_tls: true, llama_state: null,
        samples_posted: 120000 + (t % 100000), tls_cert_has_role: true, tls_expires_at: iso(t + 320 * 86400), tls_serves_role: true
      },
      last_register: iso(t - 86400), liveness: 'live', os: m.os, power: null, registered_from: m.ip,
      role: m.id === 'atlas' ? 'host' : 'system_only', status: 'approved', tls_restart_until: base,
      tls_role_checked_at: iso(t - 30), token: '<redacted>', update_available: false, version: VERSION
    };
  }

  // Registry globals: each provider's pool lists every machine that runs it, default host first; pins follow Autopilot.
  function registryGlobal(ctx) {
    var g = { auth_disabled: false, autopilot: autopilot(ctx).state };
    ['llama', 'lms', 'vllm'].forEach(function (p) {
      var ids = F.machines.filter(function (m) { return has(m, p); }).map(function (m) { return m.id; });
      ids.sort(function (a, b) { return (b === DEFAULT[p]) - (a === DEFAULT[p]); });
      g[p + '_pool'] = ids;
      g['default_' + p + '_id'] = DEFAULT[p];
      g['primary_' + p + '_id'] = DEFAULT[p];
      g[p + '_model_pins'] = {};
    });
    g.autopilot.entries.forEach(function (e) {
      if (e.placement !== 'auto') g[e.provider + '_model_pins'][e.model] = e.placement;
    });
    var mine = R.state().get('registry', {});
    Object.keys(mine.pools || {}).forEach(function (p) { g[p + '_pool'] = mine.pools[p]; });
    Object.keys(mine.pins || {}).forEach(function (p) { g[p + '_model_pins'] = mine.pins[p]; });
    return g;
  }

  // Pool order and model pins the visitor changes in Admin are kept in this browser.
  function saveRegistry(ctx, prov, fn) {
    var g = registryGlobal(ctx);
    var mine = R.state().get('registry', {});
    mine.pools = mine.pools || {}; mine.pins = mine.pins || {};
    var out = fn(g[prov + '_pool'].slice(), JSON.parse(JSON.stringify(g[prov + '_model_pins'])), mine);
    return R.state().set('registry', mine).then(function () { return out; });
  }
  function regError(status, error) {
    return new Response(JSON.stringify({ ok: false, error: error }), { status: status, headers: { 'Content-Type': 'application/json' } });
  }
  ['llama', 'lms', 'vllm'].forEach(function (prov) {
    R.route('POST /api/agents/:id/' + prov + '-pool', function (ctx) {
      var m = F.byId(ctx.params.id);
      if (!m) return regError(404, 'unknown agent');
      var b = ctx.body && typeof ctx.body === 'object' ? ctx.body : {};
      var inPool = b.in_pool === undefined ? true : !!b.in_pool;
      if (inPool && !has(m, prov)) return regError(400, 'agent does not advertise ' + prov + ' capability');
      return saveRegistry(ctx, prov, function (pool, pins, mine) {
        pool = pool.filter(function (id) { return id !== m.id; });
        if (inPool) {
          var pos = typeof b.position === 'number' ? Math.max(0, Math.min(pool.length, Math.floor(b.position))) : pool.length;
          pool.splice(pos, 0, m.id);
        }
        mine.pools[prov] = pool;
        var out = { ok: true };
        out[prov + '_pool'] = pool;
        return out;
      });
    }, { read: true });
    R.route('POST /api/admin/' + prov + '-pins', function (ctx) {
      var b = ctx.body && typeof ctx.body === 'object' ? ctx.body : {};
      var model = String(b.model_id || '').trim();
      var aid = String(b.agent_id || '');
      if (!model) return regError(400, 'model_id required');
      var m = aid ? F.byId(aid) : null;
      if (aid && !m) return regError(404, 'unknown agent');
      if (m && !has(m, prov)) return regError(400, 'agent does not advertise ' + prov + ' capability');
      return saveRegistry(ctx, prov, function (pool, pins, mine) {
        if (m) pins[model] = m.id; else delete pins[model];
        mine.pins[prov] = pins;
        var out = { ok: true };
        out[prov + '_model_pins'] = pins;
        return out;
      });
    }, { read: true });
  });

  function agentsList(ctx) {
    var t = sec(ctx);
    return {
      agents: F.machines.map(function (m) { return agentRow(m, t); }),
      ca_fingerprint: 'sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
      collect_interval_s: 60,
      enrollment: { mode: 'auto', open: false, source: 'startup', until: iso(t - 3600), window_min: 15 },
      enrollment_secret_required: true, global: registryGlobal(ctx), host_auto_detected: true,
      latest_agent_version: VERSION, manager_https_url: 'https://192.0.2.10:5443', manager_version: VERSION,
      pool_providers: [
        { label: 'llama.cpp', name: 'llama', pin_key: 'llama_model_pins' },
        { label: 'LM Studio', name: 'lms', pin_key: 'lms_model_pins' },
        { label: 'vLLM', name: 'vllm', pin_key: 'vllm_model_pins' }
      ],
      providers: [
        { capability_key: 'llama', label: 'llama.cpp', name: 'llama', sub_tab: 'llamacpp' },
        { capability_key: 'lms', label: 'LM Studio', name: 'lms', sub_tab: 'lmstudio' },
        { capability_key: 'vllm', label: 'vLLM', name: 'vllm', sub_tab: 'vllm' }
      ]
    };
  }

  function listByProvider(ctx) {
    var t = sec(ctx), out = {};
    ['llama', 'lms', 'vllm'].forEach(function (p) {
      out[p] = providerMachines(p).map(function (m) {
        return { agent_id: m.id, hostname: m.hostname, is_default: m.id === DEFAULT[p], online: true,
          age_s: round(0.5 + (t % 4) * 0.6, 1) };
      });
    });
    return out;
  }

  // ---- forecast ----
  var FORECAST_CHECKS = [
    ['disk_fill', 'Disk fill', 3], ['load_shift', 'Load shift', 3], ['memory_headroom', 'Memory headroom', 3],
    ['thermal_trend', 'Thermal trend', 7], ['power_cost', 'Power and cost', 7], ['throughput', 'Throughput', 3],
    ['model_errors', 'Model errors', 3], ['slot_pressure', 'Slot pressure', 3], ['model_churn', 'Model churn', 7],
    ['alarm_patterns', 'Alarm patterns', 7]
  ];

  var FORECAST_DIGEST = {
    'demo-fc-1,demo-fc-2': 'Two things to watch: a slow temperature rise on forge.example and a disk that is filling on atlas.example.',
    'demo-fc-1': 'One thing to watch: a slow temperature rise on forge.example.',
    'demo-fc-2': 'One thing to watch: a disk that is filling on atlas.example.'
  };
  function forecast(ctx) {
    var t = sec(ctx);
    var findings = [
      { id: 'demo-fc-1', host: 'forge.example', title: 'Thermal trend', severity: 'warning', verified: 'code',
        summary: 'GPU temperature on forge.example is creeping up about 1.5 degrees a week.',
        detail: 'The GPU has run hotter under the same load for the last two weeks. Check the case fans and dust filters.',
        suggested_action: 'Clean the intake filters and check the fan curve.', predicted_at: t + 6 * 86400, confidence: 'medium' },
      { id: 'demo-fc-2', host: 'atlas.example', title: 'Disk fill', severity: 'info', verified: 'code',
        summary: 'The model disk on atlas.example is on track to be full in about two months.',
        detail: 'Downloads are adding roughly 18 GB a day.',
        suggested_action: 'Remove models you no longer use.', predicted_at: t + 61 * 86400, confidence: 'high' }
    ];
    var gone = R.state().get('forecastDismissed', {});
    var cleared = findings.filter(function (f) { return gone[f.id]; }).map(function (f) {
      f.status = 'dismissed'; f.dismissed_by = 'alex'; f.resolved = gone[f.id];
      return f;
    });
    findings = findings.filter(function (f) { return !gone[f.id]; });
    return {
      checks: FORECAST_CHECKS.map(function (c) {
        var found = findings.filter(function (f) { return f.title === c[1]; }).length;
        return { id: c[0], title: c[1], min_days: c[2], state: 'ok', found: found, have_days: 14 };
      }),
      cleared: cleared, digest: FORECAST_DIGEST[findings.map(function (f) { return f.id; }).join(',')] || 'Nothing to watch right now.',
      enabled: true, findings: findings, last_result: findings.length + ' finding' + (findings.length === 1 ? '' : 's'),
      last_run: t - 5400, mode: 'auto',
      next_run: t + 81000, ok: true, run: null, running: false,
      tower: { reason: 'On', model: 'Qwen2.5-14B-Instruct-Q5_K_M' }, window_days: 14
    };
  }

  // ---- logs and server status ----
  function logLines(m, t, label) {
    var lines = [];
    for (var i = 11; i >= 0; i--) {
      var ts = new Date((t - i * 20) * 1000).toISOString().slice(11, 19);
      lines.push(ts + ' ' + label + ': request completed, ' + Math.round(40 + 90 * Math.abs(Math.sin(i + t / 60))) + ' tokens');
    }
    return lines;
  }

  function serverStatus(m, label) {
    return { ok: true, output: label + ' server: running on ' + m.hostname + '\nModel loaded and ready.' };
  }

  // ---- routes ----
  R.route('GET /api/me', function () {
    return { role: 'admin', is_admin: true, admin_access: true, username: 'alex', authenticated: true,
      ok: true, admin_ip: true, bypass: false };
  });

  R.route('GET /api/config', function () {
    var manual = R.state().get('interval', null);
    return {
      agents: { llama_present: true, lms_present: true, vllm_present: true },
      interval_mode: manual ? 'manual' : 'auto', interval_override: manual, interval_reason: 'active',
      poll_interval: manual || 5, poll_interval_active: 5, poll_interval_idle: 30,
      proxies: { image_gen: true, llm_chat: true, openclaw: true }
    };
  });

  // The visitor's card layout and refresh choice, kept in this browser (DemoState) so they survive reloads.
  R.route('GET /api/layout', function () { return R.state().get('layout', {}); });
  R.route('POST /api/layout', function (ctx) {
    var body = ctx.body && typeof ctx.body === 'object' ? ctx.body : {};
    return R.state().set('layout', body).then(function () { return { ok: true }; });
  }, { read: true });
  R.route('POST /api/config/interval', function (ctx) {
    var b = ctx.body && typeof ctx.body === 'object' ? ctx.body : {};
    var val = b.mode === 'manual' ? Math.max(5, Math.min(300, parseInt(b.value, 10) || 30)) : null;
    return R.state().set('interval', val).then(function () {
      return { ok: true, mode: val ? 'manual' : 'auto', value: val };
    });
  }, { read: true });

  R.route('GET /api/agents/list-by-provider', listByProvider);
  R.route('GET /api/agents/enrollment-secret', function () {
    return { ok: true, secret: 'demo-enrollment-secret', set: true };
  });
  R.route('GET /api/agents/:id/config-file', function (ctx) {
    var m = F.byId(ctx.params.id) || F.byId('atlas');
    var text = 'manager_url: https://192.0.2.10:5443\nagent_id: ' + m.id + '\nhostname: ' + m.hostname +
      '\ncollect_interval_s: 60\ncapabilities:\n  sysperf: true\n';
    return { ok: true, path: 'agent_config.yaml', size: text.length, mtime: sec(ctx) - 86400, text: text };
  });
  R.route('GET /api/agents', agentsList);

  R.route('GET /api/metrics', function (ctx) {
    return llamaMetrics(providerMachine(ctx.query, 'llama'), sec(ctx));
  });

  R.route('GET /api/history', function (ctx) {
    var t = sec(ctx), q = ctx.query;
    var pts = historyPoints(q, t);
    if (q.get('fleet')) return pts.map(fleetRow);
    var m = machineFor(q);
    return pts.map(function (ts) { return hostRow(m, ts); });
  });

  R.route('GET /api/llama-state/stream-info', function () { return { enabled: false }; });
  R.route('GET /api/llama-state/stream', function (ctx) {
    var m = providerMachine(ctx.query, 'llama');
    return self.DemoSse.stream(function (nowMs) { return { data: llamaState(m, nowMs) }; }, 2000);
  });
  R.route('GET /api/llama-state', function (ctx) {
    return llamaState(providerMachine(ctx.query, 'llama'), ctx.now);
  });

  R.route('GET /api/llm/download/stream-info', function () { return { ok: false }; });
  R.route('GET /api/llm/build/stream-info', function () { return { ok: false }; });
  R.route('GET /api/llm/server/log/stream-info', function () { return { ok: false }; });
  R.route('GET /api/llm/builds', function (ctx) {
    return { ok: true, scanned_at: sec(ctx), truncated: false, builds: [{
      path: '/opt/llama.cpp/build/bin/llama-server', dir: '/opt/llama.cpp/build/bin', name: 'llama.cpp', active: true,
      backend: 'Vulkan', note: '', version: '0.8.0-dev', build: '1', commit: 'f3586e66', ok: true, error: null }, {
      path: '/opt/llama-rocm/build/bin/llama-server', dir: '/opt/llama-rocm/build/bin', name: 'llama-rocm', active: false,
      backend: 'ROCm', note: 'source build', version: '0.8.0-dev', build: '1', commit: 'f3586e66', ok: true, error: null }] };
  });

  R.route('GET /api/fleet/:provider/aggregate', function (ctx) {
    var p = ctx.params.provider;
    if (p !== 'llama' && p !== 'lms' && p !== 'vllm') {
      return new Response(JSON.stringify({ ok: false, error: 'unknown provider: ' + p }),
        { status: 404, headers: { 'Content-Type': 'application/json' } });
    }
    return aggregate(p, sec(ctx));
  });

  R.route('GET /api/energy/summary', energySummary);
  R.route('GET /api/energy/hourly', energyHourly);
  R.route('GET /api/energy/host-peak', function (ctx) {
    var m = F.byId(ctx.query.get('agent_id')) || providerMachine(ctx.query, 'llama');
    var peakActive = m.powerW.idle + 0.85 * (m.powerW.load - m.powerW.idle);
    return { ok: true, agent_id: m.id, days: 30, peak_w: round(peakActive * 0.72, 1), hours: 640,
      since: sec(ctx) - 30 * 86400, peak_active_w: round(peakActive, 1), active_hours: 210 };
  });

  R.route('GET /api/forecast', forecast);
  // Dismissing a finding is kept in this browser and moves it to the Dismissed list.
  R.route('POST /api/forecast/:id/dismiss', function (ctx) {
    var gone = R.state().get('forecastDismissed', {});
    if (ctx.params.id !== 'demo-fc-1' && ctx.params.id !== 'demo-fc-2') {
      return new Response(JSON.stringify({ ok: false, error: 'not found' }), { status: 404, headers: { 'Content-Type': 'application/json' } });
    }
    gone[ctx.params.id] = gone[ctx.params.id] || sec(ctx);
    return R.state().set('forecastDismissed', gone).then(function () { return { ok: true }; });
  }, { read: true });
  // Autopilot keeps three models placed: two llama.cpp (one pinned to forge) and one LM Studio.
  function autopilot(ctx) {
    var t = sec(ctx);
    function entry(model, prov, placement, failover, priority, rankBy) {
      return { model: model, provider: prov, placement: placement, failover: failover, priority: priority,
        min_replicas: 1, max_replicas: 1, rank_by: rankBy };
    }
    function speed(id, model, tps, lat, wh, days) {
      var m = F.byId(id);
      return { agent_id: id, hostname: m.hostname, gen_tps: tps, latency_s: lat, wh_per_ktok: wh,
        run_id: 'demo-bench-' + id + '-' + model.slice(0, 5).toLowerCase(), llama_build: 'b6870', age_s: days * 86400, tier: 'measured' };
    }
    var entries = [
      entry('Llama-3.1-8B-Instruct-Q8_0', 'llama', 'auto', 'semi', 100, 'speed'),
      entry('Qwen2.5-14B-Instruct-Q5_K_M', 'llama', 'forge', 'auto', 90, 'speed'),
      entry(F.byId('studio').models[0], 'lms', 'auto', 'semi', 80, 'capacity')
    ];
    var status = {};
    status[entries[0].model + '/llama'] = { placed: 1, want: 1, blocked: null, basis: 'measured', pick: 'atlas',
      speed: [speed('atlas', entries[0].model, 112.4, 0.041, 1.02, 3)] };
    status[entries[1].model + '/llama'] = { placed: 1, want: 1, blocked: null, basis: null, pick: 'forge',
      speed: [speed('forge', entries[1].model, 54.2, 0.058, 1.38, 5)] };
    status[entries[2].model + '/lms'] = { placed: 1, want: 1, blocked: null, basis: 'capacity', pick: 'studio', speed: [] };
    return { entry_status: status, last_plan_ts: t - (t % 30) - 12, proposals: [], tick_period_s: 30,
      state: { enabled: true, entries: entries, hosts: {}, protect_unmanaged: false, speed_max_age_days: 30 } };
  }
  R.route('GET /api/autopilot', autopilot);
  R.route('GET /api/companion/release', function () {
    return { ahead: 0, build: self.DEMO_RELEASE || VERSION, describe: VERSION, enabled: false, install_kind: 'package',
      installed: VERSION, ok: true, repo: 'llmsyscore/llm-systems-manager', source: 'release-file' };
  });
  R.route('GET /api/tools/activity', function () {
    return { agents: {}, autotune: false, benchmark: false, quality: false, queue: {}, reportcard: false, unqueueable: [] };
  });

  R.route('GET /api/lmstudio/metrics', function (ctx) {
    var light = ctx.query.get('light') === '1' || ctx.query.get('light') === 'true';
    return lmsMetrics(providerMachine(ctx.query, 'lms'), sec(ctx), light, ctx);
  });
  R.route('GET /api/lmstudio/load-prefs', function (ctx) {
    var m = providerMachine(ctx.query, 'lms');
    var rec = { config: { context_length: ctxOf(m, 'lms'), parallel: 4 }, note: 'tuned 2026-10-01', ts: iso(sec(ctx) - 20 * 86400) };
    var model = ctx.query.get('model');
    if (model) return { ok: true, agent_id: m.id, model: model, prefs: m.models.indexOf(model) === 0 ? rec : null };
    var models = {};
    models[m.models[0]] = rec;
    return { ok: true, agent_id: m.id, models: models };
  });
  R.route('GET /api/lmstudio/server/log', function (ctx) {
    var m = providerMachine(ctx.query, 'lms');
    return { ok: true, lines: logLines(m, sec(ctx), 'lms') };
  });
  R.route('GET /api/lmstudio/server/:action', function (ctx) {
    return serverStatus(providerMachine(ctx.query, 'lms'), 'LM Studio');
  }, { fallback: true });

  R.route('GET /api/vllm/metrics', function (ctx) {
    return vllmMetrics(providerMachine(ctx.query, 'vllm'), sec(ctx));
  });
  R.route('GET /api/vllm/server/log', function (ctx) {
    var m = providerMachine(ctx.query, 'vllm');
    return { ok: true, lines: logLines(m, sec(ctx), 'vllm') };
  });
  R.route('GET /api/vllm/server/svcconfig', function (ctx) {
    var m = providerMachine(ctx.query, 'vllm');
    return { ok: true, binary: 'vllm', args: ['serve', modelOf(m, 'vllm'), '--port', '8000', '--max-model-len', String(ctxOf(m, 'vllm')),
      '--gpu-memory-utilization', '0.9'] };
  });
  R.route('GET /api/vllm/server/:action', function (ctx) {
    return serverStatus(providerMachine(ctx.query, 'vllm'), 'vLLM');
  }, { fallback: true });

  self.DemoOverall = { life: life, tps: tps };
})(self);
