// Demo handlers for models: llama.cpp model control, autotune, benchmarks, report card, gateway models.
// Model lists follow DemoFleet; benchmark and report card figures are fixed sample runs.
(function (self) {
  var R = self.DemoRouter;
  var F = self.DemoFleet;
  var S = self.DemoSeries;
  var DEFAULT = { llama: 'atlas', lms: 'studio', vllm: 'atlas' };
  var HOUR = 3600000;
  var DAY = 86400000;
  var BUILD = 'b6870';
  var BENCH_CATS = ['coding', 'math', 'reasoning', 'writing'];

  // Per machine: memory for the tuner and report card figures (7B 4-bit reference model).
  var HW = {
    atlas:  { cores: 16, vramMb: 49152, gpuLabel: '2x NVIDIA GeForce RTX 4090',
              card: { gen: 138.6, prefill: 4120, ttft: 0.041, vramMb: 7420, watts: 410, src: 'psu' } },
    forge:  { cores: 12, vramMb: 24576, gpuLabel: 'NVIDIA GeForce RTX 3090',
              card: { gen: 96.4, prefill: 2780, ttft: 0.058, vramMb: 7110, watts: 265, src: 'gpu' } },
    studio: { cores: 24, vramMb: null, gpuLabel: 'Apple M2 Ultra',
              card: { gen: 61.2, prefill: 1340, ttft: 0.092, vramMb: 7050, watts: 74, src: 'mac' } },
    quill:  { cores: 8, vramMb: null, gpuLabel: 'CPU only',
              card: { gen: 11.8, prefill: 96, ttft: 0.64, vramMb: null, watts: 42, src: 'psu' } }
  };

  // Per model: cache repo, file, size, context and offline bench speed (gen / prompt tokens per second).
  var INFO = {
    'Llama-3.1-8B-Instruct-Q8_0': { repo: 'bartowski/Meta-Llama-3.1-8B-Instruct-GGUF', file: 'Meta-Llama-3.1-8B-Instruct-Q8_0.gguf', bytes: 8540000000, ctx: 32768, gen: 112.4, ppt: 3480 },
    'Qwen2.5-32B-Instruct-Q4_K_M': { repo: 'bartowski/Qwen2.5-32B-Instruct-GGUF', file: 'Qwen2.5-32B-Instruct-Q4_K_M.gguf', bytes: 19900000000, ctx: 16384, gen: 36.8, ppt: 1120 },
    'Qwen2.5-14B-Instruct-Q5_K_M': { repo: 'bartowski/Qwen2.5-14B-Instruct-GGUF', file: 'Qwen2.5-14B-Instruct-Q5_K_M.gguf', bytes: 10500000000, ctx: 32768, gen: 54.2, ppt: 1460 },
    'Gemma-2-9B-it-Q6_K': { repo: 'bartowski/gemma-2-9b-it-GGUF', file: 'gemma-2-9b-it-Q6_K.gguf', bytes: 7590000000, ctx: 8192, gen: 66.3, ppt: 2010 },
    'Phi-3.5-mini-instruct-Q4_K_M': { repo: 'bartowski/Phi-3.5-mini-instruct-GGUF', file: 'Phi-3.5-mini-instruct-Q4_K_M.gguf', bytes: 2390000000, ctx: 16384, gen: 14.9, ppt: 118 }
  };
  var ALIASES = {
    'Llama-3.1-8B-Instruct-Q8_0': 'Everyday chat',
    'Qwen2.5-14B-Instruct-Q5_K_M': 'Writing helper',
    'Qwen2.5-Coder-32B-Instruct-8bit': 'Coding helper'
  };
  var EXTRA_CACHE = { repo: 'bartowski/Llama-3.2-3B-Instruct-GGUF', file: 'Llama-3.2-3B-Instruct-Q4_K_M.gguf', bytes: 2020000000 };
  var TRENDING = [
    ['Qwen/Qwen2.5-32B-Instruct', 'Qwen', 18400000, 412.6], ['mistralai/Mistral-Small-24B-Instruct-2501', 'mistralai', 9100000, 301.2],
    ['google/gemma-2-27b-it', 'google', 12700000, 268.9], ['Qwen/Qwen2.5-Coder-32B-Instruct', 'Qwen', 7300000, 244.1],
    ['bartowski/Qwen2.5-32B-Instruct-GGUF', 'bartowski', 3800000, 181.7], ['deepseek-ai/DeepSeek-R1-Distill-Qwen-32B', 'deepseek-ai', 6200000, 176.3],
    ['01-ai/Yi-1.5-34B-Chat', '01-ai', 2100000, 92.4], ['CohereForAI/aya-expanse-32b', 'CohereForAI', 1500000, 71.8]
  ];

  function round(v, d) { var k = Math.pow(10, d || 0); return Math.round(v * k) / k; }
  function iso(ms) { return new Date(ms).toISOString(); }
  function sec(ctx) { return Math.floor(ctx.now / 1000); }
  function hourAgo(ctx, hours) { return Math.floor(ctx.now / HOUR) * HOUR - hours * HOUR; }
  function has(m, prov) { return m.providers.indexOf(prov) >= 0; }
  function json(obj, status) {
    return new Response(JSON.stringify(obj), { status: status || 200, headers: { 'Content-Type': 'application/json' } });
  }
  function notFound() { return json({ ok: false, error: 'not found' }, 404); }

  function machineFor(q, prov) {
    var id = q && (q.get('agent') || q.get('agent_id'));
    var m = id ? F.byId(id) : null;
    return m && has(m, prov) ? m : F.byId(DEFAULT[prov]);
  }
  // llama.cpp models on a machine (the third model on the shared host runs under vLLM).
  function llamaModels(m) {
    if (!has(m, 'llama')) return [];
    return has(m, 'vllm') ? m.models.slice(0, 2) : m.models.slice();
  }
  function llamaMachines() { return F.machines.filter(function (m) { return has(m, 'llama'); }); }
  function infoOf(id) { return INFO[id] || INFO['Llama-3.1-8B-Instruct-Q8_0']; }
  function gb(bytes) { return round(bytes / 1e9, 1) + 'G'; }

  function configOf(m, id) {
    var cfg = {
      'ctx-size': String(infoOf(id).ctx), 'n-gpu-layers': m.gpus.length ? '99' : '0', 'flash-attn': 'on',
      'batch-size': '2048', 'ubatch-size': '512', 'temperature': '0.7', 'top-p': '0.95',
      'cache-type-k': 'q8_0', 'cache-type-v': 'q8_0', 'parallel': '4'
    };
    if (!m.gpus.length) { cfg['cache-type-k'] = 'f16'; cfg['cache-type-v'] = 'f16'; cfg['parallel'] = '2'; cfg['threads'] = '6'; }
    return cfg;
  }
  // The llama-server command line the router runs for one model: alias, model file, then its config as flags.
  function serverArgs(m, id) {
    var cfg = configOf(m, id);
    var args = ['llama-server', '--alias', id, '--model', 'models/' + infoOf(id).file, '--host', '127.0.0.1',
      '--port', String(39100 + llamaModels(m).indexOf(id))];
    Object.keys(cfg).forEach(function (k) { args.push('--' + k, cfg[k]); });
    return args;
  }
  // Settings and profiles the visitor saved are kept per machine in this browser (DemoState).
  function saved(key, m) { return R.state().get(key, {})[m.id] || {}; }
  function save(key, m, val) {
    var all = R.state().get(key, {});
    all[m.id] = val;
    return R.state().set(key, all);
  }
  function configAll(m) {
    var out = {};
    llamaModels(m).forEach(function (id) { out[id] = configOf(m, id); });
    var mine = saved('llmConfig', m);
    Object.keys(mine).forEach(function (id) { out[id] = mine[id]; });
    return out;
  }
  // Every configured model gets a profile so the page never tries to save one.
  function profilesAll(m) {
    var out = {};
    var ids = llamaModels(m);
    ids.forEach(function (id, n) {
      var base = configOf(m, id);
      var profiles = { 'default': base };
      if (n === ids.length - 1) {
        var long = {};
        Object.keys(base).forEach(function (k) { long[k] = base[k]; });
        long['ctx-size'] = String(infoOf(id).ctx * 2);
        profiles['long context'] = long;
      }
      out[id] = { active: 'default', profiles: profiles };
    });
    var mine = saved('profiles', m);
    Object.keys(mine).forEach(function (id) { out[id] = mine[id]; });
    return out;
  }

  R.route('GET /api/llm/models', function (ctx) {
    var m = machineFor(ctx.query, 'llama');
    var created = Math.floor(hourAgo(ctx, 24 * 30) / 1000);
    return {
      object: 'list',
      data: llamaModels(m).map(function (id, n) {
        return { id: id, object: 'model', owned_by: 'llamacpp', created: created, in_cache: true,
          status: { value: n === 0 ? 'loaded' : 'unloaded', args: serverArgs(m, id) } };
      })
    };
  });
  R.route('GET /api/llm/config', function (ctx) { return configAll(machineFor(ctx.query, 'llama')); });
  function aliasesAll() {
    var out = {};
    var mine = R.state().get('aliases', {});
    Object.keys(ALIASES).forEach(function (k) { out[k] = ALIASES[k]; });
    Object.keys(mine).forEach(function (k) { if (mine[k]) out[k] = mine[k]; else delete out[k]; });
    return out;
  }
  R.route('GET /api/llm/aliases', aliasesAll);
  R.route('GET /api/llm/profiles', function (ctx) { return profilesAll(machineFor(ctx.query, 'llama')); });

  // Model settings, nicknames and profiles the visitor changes are kept in this browser.
  R.route('POST /api/llm/config', function (ctx) {
    var m = machineFor(ctx.query, 'llama');
    var body = ctx.body && typeof ctx.body === 'object' ? ctx.body : {};
    var mine = saved('llmConfig', m);
    Object.keys(body).forEach(function (id) {
      if (id !== '*' && id !== '__DEFAULTS__' && body[id] && typeof body[id] === 'object') mine[id] = body[id];
    });
    return save('llmConfig', m, mine).then(function () { return { ok: true }; });
  }, { read: true });
  R.route('POST /api/llm/aliases', function (ctx) {
    var b = ctx.body && typeof ctx.body === 'object' ? ctx.body : {};
    var id = String(b.model_id || '').trim();
    if (!id) return json({ ok: false, error: 'model_id required' }, 400);
    var mine = R.state().get('aliases', {});
    mine[id] = String(b.alias || '').trim().slice(0, 80);
    return R.state().set('aliases', mine).then(function () { return { ok: true, aliases: aliasesAll() }; });
  }, { read: true });

  // Profile changes follow the manager's rules (names up to 64 characters, the last profile stays).
  function profileOp(ctx, op) {
    var m = machineFor(ctx.query, 'llama');
    var id = ctx.params.id;
    var b = ctx.body && typeof ctx.body === 'object' ? ctx.body : {};
    var all = profilesAll(m);
    var p = all[id];
    var name = String(b.name || '').trim();
    if (op === 'save') {
      if (!name) return json({ ok: false, error: 'agent and name required' }, 400);
      if (name.length > 64) return json({ ok: false, error: 'name too long' }, 400);
      if (b.values != null && typeof b.values !== 'object') return json({ ok: false, error: 'values must be an object' }, 400);
      p = p || { active: name, profiles: {} };
      p.profiles[name] = b.values || {};
      if (b.make_active || !p.profiles[p.active]) p.active = name;
    } else if (op === 'rename') {
      var from = String(b.from || '').trim(), to = String(b.to || '').trim();
      if (to.length > 64) return json({ ok: false, error: 'name too long' }, 400);
      if (!p || !p.profiles[from]) return json({ ok: false, error: 'no such profile' }, 404);
      if (!to || p.profiles[to]) return json({ ok: false, error: 'name exists or empty' }, 409);
      p.profiles[to] = p.profiles[from];
      delete p.profiles[from];
      if (p.active === from) p.active = to;
    } else {
      if (!p || !p.profiles[name]) return json({ ok: false, error: 'no such profile' }, 404);
      if (op === 'activate') {
        p.active = name;
      } else {
        if (Object.keys(p.profiles).length <= 1) return json({ ok: false, error: 'cannot delete the last profile' }, 409);
        delete p.profiles[name];
        if (p.active === name) p.active = p.profiles['default'] ? 'default' : Object.keys(p.profiles)[0];
      }
    }
    var mine = saved('profiles', m);
    mine[id] = p;
    return save('profiles', m, mine).then(function () {
      var out = { ok: true, model: p };
      if (op === 'activate') out.values = p.profiles[name];
      return out;
    });
  }
  ['save', 'activate', 'rename', 'delete'].forEach(function (op) {
    R.route('POST /api/llm/profiles/:id/' + op, function (ctx) { return profileOp(ctx, op); }, { read: true });
  });

  R.route('GET /api/llm/server/status', function (ctx) {
    var m = machineFor(ctx.query, 'llama');
    return { ok: true, output: [
      '* llama-server.service - llama.cpp server',
      '     Loaded: loaded (enabled)',
      '     Active: active (running) since ' + iso(hourAgo(ctx, 52)).replace('T', ' ').slice(0, 19) + ' UTC; 2 days ago',
      '   Main PID: 2218 (llama-server)',
      '      Tasks: 21',
      '     Memory: ' + round(infoOf(llamaModels(m)[0]).bytes / 1e9 + 1.4, 1) + 'G',
      '     CGroup: /system.slice/llama-server.service',
      '             `-2218 llama-server --host ' + m.ip + ' --port 8080 --models-preset config.ini'
    ].join('\n') };
  });

  // Log output: a short static tail; the live log and job streams stay idle.
  R.route('GET /api/llm/server/log/tail', function (ctx) {
    var m = machineFor(ctx.query, 'llama');
    var id = llamaModels(m)[0];
    return { ok: true, lines: [
      'srv  load_model: loading model ' + id,
      'llm_load_tensors: offloaded ' + (m.gpus.length ? 'all layers to GPU' : '0 layers to GPU'),
      'llama_new_context_with_model: n_ctx = ' + infoOf(id).ctx,
      'srv  init: initializing slots, n_slots = 4',
      'srv  init: server is listening on http://' + m.ip + ':8080',
      'srv  update_slots: all slots are idle',
      'slot launch_slot_: id  0 | task 4182 | processing task',
      'slot update_slots: id  0 | task 4182 | prompt processing done, n_tokens = 612',
      'slot      release: id  0 | task 4182 | stop processing: n_tokens = 884, truncated = 0',
      'srv  update_slots: all slots are idle'
    ] };
  });
  R.route('GET /api/llm/server/log/stream', function () {
    return self.DemoSse.stream(function () { return { data: { keepalive: true } }; }, 15000);
  });
  R.route('GET /api/llm/download/stream', function () {
    return self.DemoSse.stream(function () { return { data: { type: 'keepalive' } }; }, 15000);
  });
  R.route('GET /api/llm/build/stream', function () {
    return self.DemoSse.stream(function () { return { data: { type: 'keepalive' } }; }, 15000);
  });

  R.route('GET /api/llm/cache/gguf', function (ctx) {
    var m = machineFor(ctx.query, 'llama');
    var rows = llamaModels(m).map(function (id) {
      var i = infoOf(id);
      return { repo: i.repo, file: i.file, path: 'hf-cache/' + i.repo.replace('/', '--') + '/' + i.file, size: i.bytes };
    });
    rows.push({ repo: EXTRA_CACHE.repo, file: EXTRA_CACHE.file,
      path: 'hf-cache/' + EXTRA_CACHE.repo.replace('/', '--') + '/' + EXTRA_CACHE.file, size: EXTRA_CACHE.bytes });
    return { ok: true, data: rows };
  });
  R.route('GET /api/llm/cache', function (ctx) {
    var m = machineFor(ctx.query, 'llama');
    var seen = {};
    var data = [];
    llamaModels(m).forEach(function (id, n) {
      var i = infoOf(id);
      if (seen[i.repo]) return;
      seen[i.repo] = 1;
      data.push({ repo_id: i.repo, size: gb(i.bytes), last_accessed: (n + 1) + ' days ago', refs: ['main'] });
    });
    data.push({ repo_id: EXTRA_CACHE.repo, size: gb(EXTRA_CACHE.bytes), last_accessed: '3 weeks ago', refs: ['main'] });
    return { ok: true, data: data };
  });
  R.route('GET /api/llm/hf-trending', function (ctx) {
    var limit = parseInt(ctx.query.get('limit'), 10);
    if (!(limit > 0)) limit = 10;
    var day = Math.floor(ctx.now / DAY) * DAY;
    return { ok: true, data: TRENDING.slice(0, limit).map(function (t, n) {
      return { id: t[0], author: t[1], downloads_all_time: t[2], trending_score: t[3],
        created_at: iso(day - (120 + n * 17) * DAY), last_modified: iso(day - (2 + n * 3) * DAY) };
    }) };
  });

  // Autotune: tuned rows, preflight, batch hosts.
  R.route('GET /api/llm/autotune/status', function (ctx) {
    var m = machineFor(ctx.query, 'llama');
    var want = ctx.query.get('model_id');
    var items = [];
    llamaModels(m).forEach(function (id, n) {
      if (want && want !== id) return;
      var stale = m.id === 'forge' && n === 1;
      var tuned = stale ? 'b6641' : BUILD;
      items.push({ agent_id: m.id, model_id: id, ts: iso(hourAgo(ctx, 70 + n * 96)), ok: 1, mode: 'tune',
        llama_build: tuned, current_build: BUILD, stale: stale,
        summary: { mode: 'tune', llama_build: tuned, objective: 'fit', ctx_size: infoOf(id).ctx, gain_pct: 11 + n * 6 } });
    });
    return { ok: true, items: items };
  });
  R.route('GET /api/llm/autotune/preflight', function (ctx) {
    var prov = ctx.query.get('provider') || 'llama';
    if (prov === 'vllm') {
      return { ok: true, busy: false, server: { up: true }, runtime: { ok: true }, config: { gpu_memory_utilization: 0.9 } };
    }
    if (prov === 'lms') {
      var s = machineFor(ctx.query, 'lms');
      return { ok: true, busy: false, server: { up: true }, runtime: { ok: true }, drafts_for: {},
        models: s.models.map(function (id, n) {
          return { key: id, type: 'llm', loaded: n === 0, params: n === 0 ? '70B' : '32B', quant: n === 0 ? '4bit' : '8bit' };
        }) };
    }
    var m = machineFor(ctx.query, 'llama');
    var hw = HW[m.id];
    var sizes = {};
    var draftsFor = {};
    llamaModels(m).forEach(function (id) {
      sizes[id] = infoOf(id).bytes;
      draftsFor[id] = { repo: EXTRA_CACHE.repo, file: EXTRA_CACHE.file, size: EXTRA_CACHE.bytes };
    });
    return {
      ok: true, busy: false, unit_active: true, autotune_active: false, quality_active: false,
      help_valued: { ok: true, count: 64 }, llama_build: BUILD, cores: { physical: hw.cores, logical: hw.cores * 2 },
      perplexity: true, perplexity_detail: { present: true, kl_text: true, runnable: true, rc: 0, hint: '' },
      runtime: { ok: true, python: true, script: true }, drafts: [], drafts_for: draftsFor, sizes: sizes,
      vram_total_mb: hw.vramMb, ram_total_mb: m.ramGb * 1024
    };
  });
  R.route('GET /api/llm/draft-candidates', function (ctx) {
    var id = ctx.query.get('model_id') || '';
    return { ok: true, cached: true, repo: infoOf(id).repo, candidate: null, reason: 'a matching draft is already downloaded' };
  });
  R.route('GET /api/llm/model-meta', function (ctx) {
    var id = ctx.query.get('model_id') || '';
    var none = function () { return { found: false, values: {} }; };
    return { ok: true, cached: true, repo: infoOf(id).repo, base_model: null, fetched_ts: sec(ctx), error: null, gated: false,
      sources: { sidecar: none(), model_card: none(), base_model: none() }, suggestions: [] };
  });
  R.route('GET /api/llm/autotune/batch-hosts', function () {
    return { ok: true, hosts: llamaMachines().map(function (m) {
      return { agent_id: m.id, hostname: m.hostname, online: true, busy: false, models: llamaModels(m).slice().sort() };
    }) };
  });
  R.route('GET /api/llm/autotune/batches', function () { return { ok: true, batches: [] }; });
  R.route('GET /api/llm/autotune/batch/:id', notFound);

  // Benchmarks: offline results, live runs and baselines.
  function resultsFor(m, ctx) {
    return llamaModels(m).map(function (id, n) {
      var i = infoOf(id);
      return { model_id: id, avg_gen_tps: i.gen, avg_ppt_tps: i.ppt, avg_pg_tps: round(i.gen * 0.93, 1), bench_tool: 'llama-bench',
        switches: [{ flag: '-c', value: String(i.ctx) }, { flag: '-ngl', value: m.gpus.length ? '99' : '0' }, { flag: '-fa', value: '1' }],
        ts: iso(hourAgo(ctx, 40 + n * 72)), agent_id: m.id, extra_json: null };
    });
  }
  R.route('GET /api/benchmark/models', function (ctx) { return { models: llamaModels(machineFor(ctx.query, 'llama')) }; });
  R.route('GET /api/benchmark/results', function (ctx) {
    var prov = ctx.query.get('provider');
    if (prov && prov !== 'llama') return { results: [] };
    return { results: resultsFor(machineFor(ctx.query, 'llama'), ctx) };
  });

  function spread(v, n) {
    return { n: n, min: round(v * 0.86, 1), p25: round(v * 0.94, 1), p50: round(v, 1), p75: round(v * 1.05, 1), max: round(v * 1.13, 1) };
  }
  function whPerKtok(m, gen) { return round(m.powerW.load * 0.75 * 1000 / (gen * 3600), 2); }

  // One stored live run: k=0 newest, k=2 oldest; the oldest run of the first model is the pinned baseline.
  function liveRun(m, mi, k, ctx) {
    var id = llamaModels(m)[mi];
    var i = infoOf(id);
    var gen = round(i.gen * [0.97, 0.95, 0.99][k], 1);
    var ppt = round(i.ppt * [0.96, 0.98, 1.0][k], 0);
    return {
      id: 900 - (mi * 3 + k) - llamaMachines().indexOf(m) * 20, run_id: 'run-' + m.id + '-' + mi + '-' + k, model_id: id, agent_id: m.id,
      ts: iso(hourAgo(ctx, 6 + mi * 30 + k * 52)), ok: true, baseline: mi === 0 && k === 2,
      gen_tps: gen, ppt_tps: ppt, latency_s: round(4.2 / (gen / 60), 1), accept_rate: null, wh_per_ktok: whPerKtok(m, gen),
      config: { bench: 'qualitative', concurrency: [1, 4], limit: 8, osl: 1024 },
      llama_build: BUILD, provider: 'llama', gen_spread: spread(gen, 32)
    };
  }
  function allRuns(m, ctx) {
    var out = [];
    llamaModels(m).forEach(function (id, mi) { for (var k = 0; k < 3; k++) out.push(liveRun(m, mi, k, ctx)); });
    return out;
  }
  function runDoc(row) {
    var levels = [1, 4].map(function (conc) {
      var one = conc === 1;
      var pred = round(row.gen_tps * (one ? 1 : 0.78), 1);
      var prompt = round(row.ppt_tps * (one ? 1 : 0.9), 0);
      var lat = round(row.latency_s * (one ? 1 : 1.35), 1);
      return {
        concurrency: conc,
        all: { requests: 32, failed: 0, prompt_tps: prompt, pred_tps: pred, agg_pred_tps: round(pred * conc * (one ? 1 : 0.88), 1),
          latency_s: lat, accept_rate: null, spread: { pred_tps: spread(pred, 32), prompt_tps: spread(prompt, 32) } },
        rows: BENCH_CATS.map(function (cat, n) {
          var d = 1 + (n - 1.5) * 0.04;
          return { category: cat, requests: 8, avg_prompt_t_s: round(prompt * d, 0), avg_pred_t_s: round(pred * d, 1),
            avg_latency: round(lat * (2 - d), 1), accept_rate: null, spread: { pred_tps: spread(pred * d, 8) } };
        })
      };
    });
    return { bench: 'qualitative', config: row.config, ts: row.ts, elapsed_s: 188, wh_per_ktok: row.wh_per_ktok,
      llama_build: row.llama_build, levels: levels };
  }

  R.route('GET /api/benchmark/live/latest', function (ctx) {
    var m = machineFor(ctx.query, 'llama');
    var models = {};
    llamaModels(m).forEach(function (id, mi) {
      var r = liveRun(m, mi, 0, ctx);
      models[id] = { run_id: r.run_id, ts: r.ts, bench: 'qualitative', gen_tps: r.gen_tps, ppt_tps: r.ppt_tps, latency_s: r.latency_s,
        wh_per_ktok: r.wh_per_ktok, hostname: m.hostname, gen_spread: r.gen_spread };
    });
    return { ok: true, agent_id: m.id, models: models };
  });
  R.route('GET /api/benchmark/live/runs', function (ctx) {
    var m = machineFor(ctx.query, 'llama');
    var want = ctx.query.get('model_id');
    return { ok: true, runs: allRuns(m, ctx).filter(function (r) { return !want || r.model_id === want; }) };
  });
  R.route('GET /api/benchmark/live/runs/:id', function (ctx) {
    var machines = llamaMachines();
    for (var a = 0; a < machines.length; a++) {
      var runs = allRuns(machines[a], ctx);
      for (var b = 0; b < runs.length; b++) {
        if (runs[b].run_id === ctx.params.id) return { ok: true, meta: runs[b], run: runDoc(runs[b]) };
      }
    }
    return notFound();
  });
  R.route('GET /api/benchmark/live/fleet/:id', notFound);
  R.route('GET /api/benchmark/live/hosts', function (ctx) {
    var model = ctx.query.get('model_id');
    return { ok: true, provider: 'llama', hosts: llamaMachines().map(function (m) {
      return { agent_id: m.id, hostname: m.hostname, online: true, loaded: llamaModels(m).indexOf(model) === 0 };
    }) };
  });
  R.route('GET /api/benchmark/live/preflight', function (ctx) {
    var m = machineFor(ctx.query, 'llama');
    var cats = { categories: BENCH_CATS };
    var models = llamaModels(m).map(function (id, n) { return { id: id, status: n === 0 ? 'loaded' : 'unloaded' }; });
    return {
      ok: true, busy: false, benches: ['qualitative', 'throughput_1k', 'throughput_2k', 'throughput_8k', 'throughput_16k', 'throughput_32k'],
      server: { up: true, url: 'http://' + m.ip + ':8080', models: models, loaded_id: models[0].id,
        slots_total: 4, slots_idle: S.busy(m.id, sec(ctx)) ? 3 : 4, spec: null },
      runtime: { python: true, script: true, script_status: 'ok' },
      datasets: { qualitative: cats, throughput_1k: cats, throughput_2k: cats, throughput_8k: cats, throughput_16k: cats, throughput_32k: cats }
    };
  });
  R.route('GET /api/benchmark/live/baselines', function (ctx) {
    var rows = [];
    llamaMachines().forEach(function (m) {
      var r = liveRun(m, 0, 2, ctx);
      var check = round(r.gen_tps * 0.985, 1);
      rows.push({ run_id: r.run_id, model_id: r.model_id, agent_id: m.id, hostname: m.hostname, ts: r.ts, gen_tps: r.gen_tps, config: r.config,
        online: true, loaded: true, llama_build: BUILD, build_changed: false, running: false, active_run_id: null, pending: null,
        last_check: { id: 1, baseline_run_id: r.run_id, run_id: 'check-' + m.id, model_id: r.model_id, agent_id: m.id, ts: iso(hourAgo(ctx, 9)),
          trigger: 'nightly', status: 'ok', gen_tps: check, base_tps: r.gen_tps, delta_pct: round((check - r.gen_tps) / r.gen_tps * 100, 1),
          severity: null, error: null, llama_build: BUILD, build_from: null, promoted: false, spread_floor: null },
        gen_spread: r.gen_spread, promoted_from: null });
    });
    return { ok: true, baselines: rows, primary_agent_id: DEFAULT.llama,
      schedule: { enabled: true, nightly_at: '03:00', on_build_change: true, promote_on_build_change: false, regression_pct: 10,
        nightly_valid: true, next_nightly_ts: Math.floor((Math.floor(ctx.now / DAY) + 1) * DAY / 1000) + 3 * 3600 } };
  });

  // Report card: reference-model cards, one earlier card per week.
  var REF_MODEL = 'Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M';
  function cardFor(m, prov, model, week, ctx) {
    var c = HW[m.id].card;
    var f = 1 - week * 0.012;
    var gen = round(c.gen * f, 1);
    var watts = round(c.watts * (1 + week * 0.004), 0);
    return {
      ts: Math.floor(hourAgo(ctx, 5 + week * 168) / 1000), provider: prov, mode: model === REF_MODEL ? 'standard' : 'custom',
      preset_version: 'preset_v2', eligible: model === REF_MODEL,
      result: {
        model: model, gpu_config: HW[m.id].gpuLabel, provider: prov, gen_tps: gen, prefill_tps: round(c.prefill * f, 0), ttft_s: c.ttft,
        vram_used_mb: c.vramMb, vram_total_mb: HW[m.id].vramMb, avg_watts: watts, power_source: c.src, tokens_per_joule: round(gen / watts, 3),
        usd_per_mtok: round(watts * 1e6 / gen / 3.6e6 * 0.15, 3),
        spread: { gen_tps: spread(gen, 10), prefill_tps: spread(c.prefill * f, 10) }
      }
    };
  }
  function cardMachine(q) {
    var m = q.get('agent') ? F.byId(q.get('agent')) : null;
    return m || F.byId(DEFAULT.llama);
  }
  // Recent cards across the fleet, newest first, in the slim ledger shape.
  R.route('GET /api/reportcard/recent', function (ctx) {
    var limit = Math.max(1, Math.min(100, parseInt(ctx.query.get('limit'), 10) || 12));
    var cards = [];
    F.machines.forEach(function (m) {
      ['llama', 'vllm', 'lms'].forEach(function (prov) {
        if (!has(m, prov)) return;
        var ids = prov === 'llama' ? llamaModels(m) : prov === 'vllm' ? m.models.slice(2) : m.models;
        if (!ids.length) return;
        var c = cardFor(m, prov, ids[0], 0, ctx), r = c.result;
        cards.push({ ts: c.ts, agent_id: m.id, provider: prov, eligible: c.eligible,
          result: { model: r.model, gen_tps: r.gen_tps, avg_watts: r.avg_watts, usd_per_mtok: r.usd_per_mtok, gpu_config: r.gpu_config } });
      });
    });
    cards.sort(function (a, b) { return b.ts - a.ts; });
    return { ok: true, cards: cards.slice(0, limit) };
  });
  R.route('GET /api/reportcard/preset', function () {
    return { preset_version: 'preset_v2', gen_tokens: 128, reps: 10, providers: ['llama', 'vllm', 'lms'], price_kwh: 0.15,
      models: [{ key: 'small', label: 'Qwen2.5-1.5B-Instruct (4-bit)' }, { key: 'mid', label: 'Qwen2.5-7B-Instruct (4-bit)' }] };
  });
  R.route('GET /api/reportcard/models', function (ctx) {
    var m = cardMachine(ctx.query);
    var prov = ctx.query.get('provider') || 'llama';
    var ids = prov === 'llama' ? llamaModels(m) : prov === 'vllm' ? (has(m, 'vllm') ? m.models.slice(2) : []) : (has(m, 'lms') ? m.models : []);
    return { ok: true, models: ids };
  });
  // Card model: the asked one, else the reference; LM Studio cards always name a model from its own list.
  function cardModel(m, prov, asked) {
    if (prov === 'lms') return m.models.indexOf(asked) >= 0 ? asked : m.models[0];
    return asked || REF_MODEL;
  }
  R.route('GET /api/reportcard/latest', function (ctx) {
    var m = cardMachine(ctx.query);
    var prov = ctx.query.get('provider') || 'llama';
    if (!has(m, prov)) return { ok: true, card: null };
    return { ok: true, card: cardFor(m, prov, cardModel(m, prov, ctx.query.get('model')), 0, ctx) };
  });
  R.route('GET /api/reportcard/history', function (ctx) {
    var m = cardMachine(ctx.query);
    var prov = ctx.query.get('provider') || 'llama';
    var model = has(m, prov) ? cardModel(m, prov, ctx.query.get('model')) : null;
    if (!has(m, prov)) return { ok: true, cards: [] };
    return { ok: true, cards: [0, 1, 2, 3].map(function (w) { return cardFor(m, prov, model, w, ctx); }) };
  });

  // Gateway: every machine's models tagged by provider; the shared host's third model is vLLM.
  R.route('GET /api/gateway/v1/models', function (ctx) {
    var created = Math.floor(hourAgo(ctx, 24 * 30) / 1000);
    var data = [];
    var seen = {};
    function add(id, prov, loaded) {
      if (seen[id]) return;
      seen[id] = 1;
      data.push({ id: id, object: 'model', created: created, owned_by: prov, provider: prov, status: { value: loaded ? 'loaded' : 'unloaded' } });
    }
    F.machines.forEach(function (m) {
      llamaModels(m).forEach(function (id, n) { add(id, 'llama', n === 0); });
      if (has(m, 'lms')) m.models.forEach(function (id, n) { add(id, 'lms', n === 0); });
      if (has(m, 'vllm')) add(m.models[2], 'vllm', true);
    });
    return { object: 'list', data: data };
  });
})(self);
