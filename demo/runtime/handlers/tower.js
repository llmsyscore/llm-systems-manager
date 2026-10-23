// Demo Tower answers: state, threads, insights, models and a canned streamed reply chosen by keyword.
(function (self) {
  var R = self.DemoRouter;
  var F = self.DemoFleet;
  var FALLBACK = 'In the demo I can only answer a few sample questions. Install it to ask about your own machines.';
  var PREV_THREAD = 'demo-earlier';
  var HOUR = 3600;

  function sec(ctx) { return Math.floor(ctx.now / 1000); }
  function round(v, d) { var k = Math.pow(10, d || 0); return Math.round(v * k) / k; }
  function model() { return F.byId('atlas').models[0]; }
  function names(id) { return F.byId(id).models.join(', '); }

  var ANSWERS = {
    slow: { tool: 'get_metrics', args: { host: 'forge.example' }, ms: 118,
      text: 'forge.example is the slow one. Its graphics card has been around 82 °C for the last 20 minutes, '
        + 'above the 80 °C warning level, so it slows itself down to cool off. It answers noticeably slower than '
        + 'it did earlier today. Sending new requests to atlas.example for a while would help, and so would cleaning '
        + 'the fans on forge.example.' },
    alarm: { tool: 'get_alerts', args: {}, ms: 84,
      text: 'There are two open alarms. The warning is on forge.example: its graphics card is a little over the 80 °C '
        + 'limit and has been for about 25 minutes. The other is a low-priority note on quill.example: it is using a '
        + 'little swap memory, around 6%. Neither one is stopping any work right now, but the forge.example one is '
        + 'worth watching.' },
    cost: { tool: 'get_energy', args: { window: '24h' }, ms: 133,
      text: 'atlas.example costs the most to run because it has two graphics cards, and quill.example costs the least. '
        + 'The machines draw far more power while they are answering requests than when they are idle, so the bill '
        + 'follows how busy they are. For the same number of words, running them yourself is much cheaper than a '
        + 'paid cloud service.' },
    model: { tool: 'get_models', args: {}, ms: 97, text: '' },
    about: { text: 'LLM Systems Manager is built by LLM SysCore. It is an open-source project under the AGPL-3.0 license, '
      + 'and the code is on GitHub at github.com/llmsyscore/llm-systems-manager. Everything runs on your own hardware: '
      + 'your machines, your models and your data stay with you.' },
    help: { text: 'The guides are at https://www.llmsyscore.com/docs.html. For questions, email support@llmsyscore.com. '
      + 'Report bugs and ask for new features as GitHub issues at github.com/llmsyscore/llm-systems-manager/issues. '
      + 'Please send security problems privately by email instead of opening a public issue.' }
  };
  ANSWERS.model.text = 'atlas.example is running ' + F.byId('atlas').models[0] + ' and also has '
    + F.byId('atlas').models[1] + ' and ' + F.byId('atlas').models[2] + ' available. forge.example has '
    + names('forge') + '. studio.example has ' + names('studio') + ', and quill.example runs the small '
    + names('quill') + '. Each machine answers on its own, so you can pick the one that fits the job.';

  // Picks the canned answer key for a question; null means the fallback.
  function keyFor(text) {
    var t = String(text || '').toLowerCase();
    if (/\bwho\b/.test(t) && /develop|make|made|build|built|maintain|behind|creat/.test(t)) return 'about';
    if (/\b(help|support|contact)\b|\bbugs?\b/.test(t)) return 'help';
    if (/slow/.test(t)) return 'slow';
    if (/alarm|alert/.test(t)) return 'alarm';
    if (/cost/.test(t)) return 'cost';
    if (/model/.test(t)) return 'model';
    return null;
  }

  function stateBody() {
    var m = model();
    return {
      ok: true, enabled: true, admin: true, model: m, provider: 'llama', hosts: ['atlas.example'],
      check: { model: m, grade: 'native', mode: 'native', size_b: 8 },
      thinking_options: null, fallback: null, fallback_enabled: false,
      capabilities: 'read', off_topic: 'refuse', diagnose_alarms: true,
      insights_new: 1, latest_insight: null, insights_rev: 1, timers: 0
    };
  }

  function alertList(nowMs) {
    return self.DemoAlarms ? self.DemoAlarms.alerts(nowMs) : [];
  }
  function createdS(a) { return Math.floor(Date.parse(a.created_at + 'Z') / 1000); }

  function snapshot(nowS) {
    var pts = [];
    for (var i = 0; i < 30; i++) {
      var ts = nowS - (29 - i) * 120;
      pts.push([ts, round(74 + 8.4 * Math.min(1, i / 18) + 0.8 * Math.sin(i * 1.3), 1)]);
    }
    return { points: pts, threshold: 80, unit: '°C', metric: 'gpu_temperature_c', minutes: 60 };
  }

  function insightRows(ctx) {
    var s = sec(ctx);
    var al = alertList(ctx.now);
    var notes = [
      { status: 'new', summary: 'The graphics card on forge.example is running hot and has slowed down.',
        detail: 'The card has been just above 80 °C for about 25 minutes. Speed drops when it gets this warm.',
        action: 'Check the case fans on forge.example, or send new requests to atlas.example for a while.',
        checks: [{ ok: true, name: 'get_metrics', summary: 'GPU temperature 82 °C, fan speed steady' },
                 { ok: true, name: 'get_alerts', summary: 'One warning open on forge.example' }], snap: true },
      { status: 'seen', summary: 'quill.example is using a little swap memory.',
        detail: 'Swap use is around 6%. The machine has little memory, so this is expected while it answers.',
        action: 'Nothing to do unless it keeps growing.', checks: [] },
      { status: 'seen', summary: 'The graphics card on atlas.example got too hot for a few minutes and then cooled down.',
        detail: 'It peaked at 86 °C and cleared on its own after about 9 minutes.',
        action: 'No action needed.', checks: [] }
    ];
    var mine = R.state().get('insights', {});
    return notes.map(function (n, i) {
      var a = al[i] || {};
      var at = a.created_at ? createdS(a) : s - 3600 * (i + 1);
      var id = 'demo-ins-' + (i + 1);
      var my = mine[id] || {};
      return {
        id: id, alert_id: a.alert_id || '', rule: a.rule_name || 'Alert', host: a.source_host || '',
        severity: a.severity || 'warning', summary: n.summary, detail: n.detail, suggested_action: n.action,
        playbook_id: null, playbook_title: null, playbook_safe: null, steps: [], checks: n.checks, thread_id: null,
        status: my.status || n.status, created: at, resolved: my.resolved || null, applied_by: my.applied_by || null,
        result: my.result || null, seen_at: n.status === 'seen' ? at + 120 : null, snapshot: n.snap ? snapshot(s) : null
      };
    });
  }
  function openInsights(ctx) {
    return insightRows(ctx).filter(function (r) { return r.status !== 'dismissed'; });
  }
  function newCount(rows) { return rows.filter(function (r) { return r.status === 'new'; }).length; }

  function thread(id, title, created, updated) {
    return { id: id, title: title, created: created, updated: updated, page: {} };
  }
  function prevThread(ctx) {
    var s = sec(ctx);
    return thread(PREV_THREAD, 'Which machine is busiest?', s - 3 * HOUR, s - 3 * HOUR + 40);
  }
  function prevMessages(ctx) {
    var s = sec(ctx) - 3 * HOUR;
    return [
      { role: 'user', content: 'Which machine is busiest right now?', tool_name: null, tool_args: null, tool_ok: null, tool_ms: null, ts: s },
      { role: 'tool', content: JSON.stringify({ ok: true, hosts: 4 }), tool_name: 'get_fleet_status', tool_args: '{}', tool_ok: 1, tool_ms: 92, ts: s + 5 },
      { role: 'assistant', tool_name: null, tool_args: null, tool_ok: null, tool_ms: null, ts: s + 12,
        content: 'atlas.example is the busiest. It is answering requests with both of its graphics cards and its speed '
          + 'is steady. forge.example is next, running a little warm. studio.example and quill.example are mostly idle.' }
    ];
  }

  R.route('GET /api/tower/state', function (ctx) {
    var st = stateBody();
    var rows = self.DemoAlarms ? openInsights(ctx) : [];
    var ins = rows[0] || null;
    st.insights_new = newCount(rows);
    st.latest_insight = ins ? { id: ins.id, rule: ins.rule, host: ins.host, severity: ins.severity, summary: ins.summary, created: ins.created } : null;
    return st;
  });
  R.route('GET /api/tower/timers', function () { return { ok: true, timers: [] }; });
  R.route('GET /api/tower/insights', function (ctx) {
    var rows = insightRows(ctx);
    return { ok: true, insights: rows, new: newCount(rows) };
  });

  R.route('GET /api/tower/eval', function (ctx) {
    var s = sec(ctx);
    var cases = [
      ['alerts', 'Open alarms', 1, 62], ['hosts', 'Fleet status', 1, 71], ['metrics', 'One machine’s numbers', 2, 148],
      ['models', 'Loaded models', 1, 80], ['timer', 'Watch a number for a while', 2, 190], ['off', 'A question about something else', 0, 40]
    ].map(function (c) {
      return { id: c[0], title: c[1], passed: true, calls: c[2], corrections: 0, retries: 0, ms: c[3] * 10, detail: '' };
    });
    var row = { id: 'demo-eval-1', model: model(), provider: 'llama', quant: 'Q8_0', server: 'llama.cpp', tool_mode: 'native',
      grade: 'native', at: s - 2 * 86400, ms: 41000, passed: 6, total: 6, calls: 7, corrections: 0, retries: 0, score_pct: 100,
      actor: 'alex', hosts: ['atlas.example'], cases: cases };
    return { ok: true, results: [row], live: null, model: model(), admin: true };
  });

  var CURATED = [
    ['qwen35-9b-q4', 'Qwen3.5 9B', 8, 'unsloth/Qwen3.5-9B-GGUF', 'Qwen3.5-9B-Q4_K_M.gguf', 'Q4_K_M', 5.7, 9, 'high', false],
    ['gemma4-12b-q6', 'Gemma 4 12B (Q6)', 16, 'unsloth/gemma-4-12b-it-GGUF', 'gemma-4-12b-it-Q6_K.gguf', 'Q6_K', 9.8, 12, 'high', false],
    ['qwen35-4b-q4', 'Qwen3.5 4B', 6, 'unsloth/Qwen3.5-4B-GGUF', 'Qwen3.5-4B-Q4_K_M.gguf', 'Q4_K_M', 2.7, 4, 'good', true]
  ];
  R.route('GET /api/tower/models', function () {
    var models = CURATED.map(function (c) {
      return { key: c[0], name: c[1], tier_gb: c[2], repo: c[3], file: c[4], quant: c[5], size_gb: c[6], params_b: c[7],
        expected: c[8], small: c[9], notes: 'Works well for Tower on a card of this size.', measured: '8 of 8, 2026-09-24',
        model_id: c[3] + ':' + c[5], present: false, loaded: false, eval: null, evals: { llama: null, lms: null } };
    });
    return { ok: true, models: models,
      hosts: [{ provider: 'llama', label: 'llama.cpp', host: 'atlas.example', agent_id: 'atlas', primary: true }],
      host: 'atlas.example', live: null, last: null,
      index: [{ id: model(), provider: 'llama', hosts: ['atlas.example'], loaded: true }], admin: true };
  });

  // Threads: literal path first, then the id route.
  // Renamed and deleted conversations are kept in this browser.
  function threadState() { return R.state().get('threads', { titles: {}, deleted: {} }); }
  function known(id) { return id === PREV_THREAD || id.indexOf('demo-t') === 0; }
  function notFound(msg) {
    return new Response(JSON.stringify({ ok: false, error: msg }), { status: 404, headers: { 'Content-Type': 'application/json' } });
  }
  R.route('GET /api/tower/threads', function (ctx) {
    var t = prevThread(ctx);
    var ts = threadState();
    var list = ts.deleted[t.id] ? [] : [{ id: t.id, title: ts.titles[t.id] || t.title, created: t.created, updated: t.updated }];
    return { ok: true, threads: list, discord: [] };
  });
  R.route('POST /api/tower/threads', function (ctx) {
    var s = sec(ctx);
    return { ok: true, thread: thread('demo-t' + s, (ctx.body && ctx.body.title) || 'New thread', s, s) };
  }, { read: true });
  R.route('GET /api/tower/threads/:tid', function (ctx) {
    var id = ctx.params.tid;
    var ts = threadState();
    if (!known(id) || ts.deleted[id]) return notFound('unknown thread');
    if (id === PREV_THREAD) {
      var p = prevThread(ctx);
      if (ts.titles[id]) p.title = ts.titles[id];
      return { ok: true, thread: p, messages: prevMessages(ctx), read_only: false, active_run: null };
    }
    var s = sec(ctx);
    return { ok: true, thread: thread(id, ts.titles[id] || 'New thread', s, s), messages: [], read_only: false, active_run: null };
  });
  R.route('PATCH /api/tower/threads/:tid', function (ctx) {
    var id = ctx.params.tid;
    var title = String((ctx.body && ctx.body.title) || '').trim().slice(0, 60);
    var ts = threadState();
    if (!known(id) || ts.deleted[id]) return notFound('unknown thread');
    if (!title) {
      return new Response(JSON.stringify({ ok: false, error: 'title required' }), { status: 400, headers: { 'Content-Type': 'application/json' } });
    }
    ts.titles[id] = title;
    var s = sec(ctx);
    return R.state().set('threads', ts).then(function () { return { ok: true, thread: thread(id, title, s, s) }; });
  }, { read: true });
  R.route('DELETE /api/tower/threads/:tid', function (ctx) {
    var id = ctx.params.tid;
    var ts = threadState();
    if (!known(id) || ts.deleted[id]) return notFound('unknown thread');
    ts.deleted[id] = true;
    return R.state().set('threads', ts).then(function () { return { ok: true }; });
  }, { read: true });
  R.route('POST /api/tower/threads/:tid/messages', function (ctx) {
    var text = ctx.body && ctx.body.text;
    if (!String(text || '').trim()) {
      return new Response(JSON.stringify({ ok: false, error: 'text required' }), { status: 400, headers: { 'Content-Type': 'application/json' } });
    }
    return { ok: true, run_id: 'demo-run-' + (keyFor(text) || 'other') + '-' + ctx.now };
  }, { read: true });

  // Splits a reply into small word groups so it types in like a live answer.
  function chunks(text) {
    var words = text.split(' ');
    var out = [];
    for (var i = 0; i < words.length; i += 3) out.push(words.slice(i, i + 3).join(' ') + (i + 3 < words.length ? ' ' : ''));
    return out;
  }

  // Event list for one run: thinking, an optional tool tick, the text in pieces, then done.
  function runEvents(key) {
    var a = ANSWERS[key];
    var ev = [{ event: 'status', state: 'thinking' }];
    if (a && a.tool) {
      ev.push({ event: 'tool', name: a.tool, ok: true, ms: a.ms,
        summary: 'read ' + a.tool.replace(/_/g, ' ') + (a.args.host ? ' · ' + a.args.host : '') + ' · ' + a.ms + ' ms',
        result: { ok: true } });
    }
    chunks(a ? a.text : FALLBACK).forEach(function (c) { ev.push({ event: 'delta', text: c }); });
    ev.push({ event: 'done', elapsed_ms: 1800 });
    return ev;
  }

  R.route('GET /api/tower/runs/:rid/stream', function (ctx) {
    var m = /^demo-run-([a-z]+)-/.exec(ctx.params.rid);
    var events = runEvents(m ? m[1] : 'other');
    return self.DemoSse.stream(function (nowMs, i) {
      return i < events.length ? { data: events[i] } : null;
    }, 140);
  });

  // Page-fired writes (insights opened, panel closed) are answered quietly.
  R.route('POST /api/tower/insights/seen', function () { return { ok: true, seen: 0 }; }, { read: true });
  R.route('POST /api/tower/runs/:rid/park', function () { return { ok: true }; }, { read: true });
  R.route('POST /api/tower/runs/:rid/stop', function () { return { ok: true }; }, { read: true });

  // Dismissed and applied insights are kept in this browser; literal path before the id routes.
  function setInsights(mine, out) {
    return R.state().set('insights', mine).then(function () { return out; });
  }
  R.route('POST /api/tower/insights/dismiss_all', function (ctx) {
    var mine = R.state().get('insights', {});
    var n = 0;
    insightRows(ctx).forEach(function (r) {
      if (r.status === 'dismissed' || r.status === 'applying') return;
      mine[r.id] = { status: 'dismissed', resolved: sec(ctx), applied_by: r.applied_by, result: r.result };
      n++;
    });
    return setInsights(mine, { ok: true, dismissed: n });
  }, { read: true });
  function insightOp(ctx, to, allow) {
    var row = insightRows(ctx).filter(function (r) { return r.id === ctx.params.iid; })[0];
    if (!row) return notFound('unknown insight');
    if (allow.indexOf(row.status) < 0) {
      return new Response(JSON.stringify({ ok: false, error: 'not open' }), { status: 409, headers: { 'Content-Type': 'application/json' } });
    }
    var mine = R.state().get('insights', {});
    var my = { status: to, resolved: sec(ctx), applied_by: row.applied_by, result: row.result };
    if (to === 'applied') { my.applied_by = 'tower via alex'; my.result = { ok: true, message: 'done', steps: [] }; }
    mine[row.id] = my;
    return setInsights(mine, to === 'applied' ? { ok: true, status: 'applied', result: my.result } : { ok: true });
  }
  R.route('POST /api/tower/insights/:iid/dismiss', function (ctx) {
    return insightOp(ctx, 'dismissed', ['new', 'seen', 'applied']);
  }, { read: true });
  R.route('POST /api/tower/insights/:iid/apply', function (ctx) {
    return insightOp(ctx, 'applied', ['new', 'seen']);
  }, { read: true });

  self.DemoTower = { keyFor: keyFor, FALLBACK: FALLBACK };
})(self);
