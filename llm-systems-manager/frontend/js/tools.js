// Tools tab controller (#769/#770): launcher (card/list/compact), run ledger,
// module shells. Rendering lives in js/lib/toolcards.js (window.TC).
(function () {
  let _toolsInited = false;
  let _toolsAgents = {};   // agent_id -> hostname
  let _toolsRc = [];       // recent report cards, newest first
  let _toolsRuns = [];     // cross-tool run ledger rows, newest first
  let _toolsRunTotals = {}; // tool -> stored-row count (beyond the fetched page)
  let _toolsRunLatest = {}; // tool -> newest stored run (beyond the fetched page)
  let _toolsDefaultLlama = null;
  let _toolsFetchedAt = 0;
  let _ledgerSort = { key: 'ts', dir: 'desc' };
  let _ledgerTool = 'all';
  let _ledgerPage = 0;
  let _toolsActivity = { reportcard: false, benchmark: false, autotune: false };
  let _toolsLocalWas = { rc: false, bench: false, at: false };
  const _LEDGER_CAP = 100, _LEDGER_PAGE = 15;
  // Shared run gate (#888): who is busy where, and one pending run per tool.
  const _TOOL_LABEL = { reportcard: 'Report Card', benchmark: 'Benchmark',
                        autotune: 'Autotune', quality: 'Quality guard' };
  let _toolsActivityAgents = {};   // agent_id -> tools running on it
  let _toolsDefaultAgent = {};     // provider -> primary agent id
  let _toolsPending = {};          // tool id -> what its queued run waits for
  let _toolsAgentsLoad = null;
  let _toolsAgentsReady = false;
  let _toolsGateKey = '';
  const _toolsGateSubs = new Set();

  function _tEl(id) { return document.getElementById(id); }
  function _tLayout() { return (typeof layout === 'object' && layout) ? layout : null; }

  function _tProv(p) {
    return (((typeof RC !== 'undefined' && RC.PROVIDER_LABEL) || {})[p]) || p || '';
  }

  // Streams this browser owns; instant, but private to this session.
  function _toolsRunningLocal() {
    const rc = typeof _rcEventSrc !== 'undefined' && _rcEventSrc;
    const bench = (typeof _benchEventSrc !== 'undefined' && _benchEventSrc)
      || (typeof _vbenchEventSrc !== 'undefined' && _vbenchEventSrc)
      || (window.BL && BL.running());
    const at = (window.AT && AT.running())
      || (window.QG && QG.running())
      || (typeof _vatEventSrc !== 'undefined' && _vatEventSrc);
    return { rc: !!rc, bench: !!bench, at: !!at };
  }

  // Local streams OR the fleet-wide snapshot, so every dashboard shows the
  // same run indicators (#775).
  function _toolsRunning() {
    const l = _toolsRunningLocal();
    const rc = l.rc || !!_toolsActivity.reportcard;
    const bench = l.bench || !!_toolsActivity.benchmark;
    const at = l.at || !!_toolsActivity.autotune;
    return { rc, bench, at, any: !!(rc || bench || at), local: l };
  }

  // Fleet-wide run state from GET /api/tools/activity; polled from boot.js.
  function toolsPollActivity() {
    const f = typeof _fetchT === 'function' ? _fetchT : (u => fetch(u));
    return f('/api/tools/activity')
      .then(r => (r.ok ? r.json() : Promise.reject(new Error('http ' + r.status))))
      .then(d => {
        // The quality guard shares the Autotune tile's run indicator (#887).
        _toolsActivity = {
          reportcard: !!d.reportcard, benchmark: !!d.benchmark,
          autotune: !!d.autotune || !!d.quality,
        };
        _toolsActivityAgents = (d.agents && typeof d.agents === 'object') ? d.agents : {};
        toolsSyncRunDot();
      })
      .catch(() => {});
  }

  function _tHost(agentId) { return _toolsAgents[agentId] || agentId || ''; }

  // ── shared run gate (#888) ──────────────────────────────────────────────
  function _toolsApplyAgents(byProvider) {
    _toolsAgents = {};
    _toolsDefaultAgent = {};
    _toolsAgentsReady = true;
    Object.entries(byProvider || {}).forEach(([prov, list]) =>
      (list || []).forEach(a => {
        _toolsAgents[a.agent_id] = a.hostname;
        if (a.is_default) _toolsDefaultAgent[prov] = a.agent_id;
      }));
    if (_toolsDefaultAgent.llama) _toolsDefaultLlama = _toolsDefaultAgent.llama;
  }

  // A deep link opens a module before the launcher's own fetch runs.
  // A failed fetch keeps the gate closed and retries; it never reads as idle.
  function _toolsEnsureAgents() {
    if (_toolsAgentsLoad || Object.keys(_toolsDefaultAgent).length) return;
    const f = typeof _fetchT === 'function' ? _fetchT : (u => fetch(u));
    _toolsAgentsLoad = f('/api/agents/list-by-provider')
      .then(r => (r.ok ? r.json() : null))
      .catch(() => null)
      .then(d => {
        if (d) { _toolsApplyAgents(d); toolsSyncRunDot(); return; }
        console.warn('tools gate: agent list failed to load; retrying in 5 s');
        _toolsAgentsLoad = null;
        setTimeout(_toolsEnsureAgents, 5000);
      });
  }

  // Local streams mapped to the agent they drive, so the gate answers instantly.
  function _toolsLocalAgents() {
    const out = {};
    const add = (id, tool) => { if (id) (out[id] = out[id] || []).push(tool); };
    const l = _toolsRunningLocal();
    const vb = typeof _vbenchEventSrc !== 'undefined' && _vbenchEventSrc;
    const va = typeof _vatEventSrc !== 'undefined' && _vatEventSrc;
    const rcTarget = typeof _rcRunTarget !== 'undefined' && _rcRunTarget;
    if (l.rc) add((rcTarget && rcTarget.agent) || _toolsDefaultAgent.llama, 'reportcard');
    if (l.bench) add(vb ? _toolsDefaultAgent.vllm : _toolsDefaultAgent.llama, 'benchmark');
    if (l.at) add(va ? _toolsDefaultAgent.vllm : _toolsDefaultAgent.llama,
                  (window.QG && QG.running()) ? 'quality' : 'autotune');
    return out;
  }

  // Which tool holds one provider/agent right now; null when it is free.
  // Fails closed until the agent list resolves — an unknown host is not idle.
  function toolsGateBusy(provider, agentId) {
    _toolsEnsureAgents();
    const id = agentId || _toolsDefaultAgent[provider || 'llama'] || null;
    if (!id) {
      return _toolsAgentsReady ? null
        : { tool: 'unknown', label: 'the agent list to load', agent_id: null,
            host: '', unresolved: true };
    }
    const tools = [...new Set([...(_toolsLocalAgents()[id] || []),
                               ...(_toolsActivityAgents[id] || [])])];
    if (!tools.length) return null;
    return { tool: tools[0], label: _TOOL_LABEL[tools[0]] || tools[0],
             agent_id: id, host: _tHost(id) };
  }

  function toolsGateOn(fn) { if (typeof fn === 'function') _toolsGateSubs.add(fn); }

  // An agent refusal that means "something else holds the tool lock".
  function toolsGateRefusal(text) {
    return /in progress|already running/i.test(String(text || ''));
  }

  // Tile id behind a slot key: 'benchmark:offline' marks the Benchmark tile.
  function _toolsPendingFor(toolId) {
    const hit = Object.keys(_toolsPending).find(
      k => k === toolId || k.indexOf(toolId + ':') === 0);
    return hit ? _toolsPending[hit] : null;
  }

  // A module's pending run, so the launcher can't show the tool as idle.
  function toolsSetQueued(toolId, waitFor) {
    if (!toolId) return;
    if (waitFor) _toolsPending[toolId] = waitFor; else delete _toolsPending[toolId];
    const home = _tEl('toolsHome');
    if (_toolsInited && home && home.style.display !== 'none') _toolsRenderLauncher();
  }

  function _toolsGateNotify() {
    const key = JSON.stringify([_toolsActivityAgents, _toolsLocalAgents(),
                                _toolsDefaultAgent, _toolsAgentsReady]);
    if (key === _toolsGateKey) return;
    _toolsGateKey = key;
    [..._toolsGateSubs].forEach(fn => { try { fn(); } catch (_) {} });
  }

  // One queue slot per tool: at most one pending run, started by the gate once
  // the provider/agent it waits on goes idle.
  function toolsQueueSlot(toolId, opts) {
    let pending = null;
    // A pending run keeps the host it was queued against, so changing a picker
    // can't repoint the gate at a host the frozen payload will not hit.
    const pick = () => ({ provider: opts.provider ? opts.provider() : 'llama',
                          agent: opts.agent ? opts.agent() : null });
    const target = () => {
      const t = pending ? pending.target : pick();
      return toolsGateBusy(t.provider, t.agent);
    };
    const paint = () => {
      toolsSetQueued(toolId, pending ? pending.waitFor : null);
      if (opts.render) {
        // An unresolved host gates the run but has no name to show for it.
        const b = target();
        try {
          opts.render({ queued: !!pending, waitFor: pending ? pending.waitFor : null,
                        busy: (b && b.unresolved) ? null : b });
        } catch (_) {}
      }
    };
    const slot = {
      busy: target,
      queued: () => !!pending,
      waitFor: () => (pending ? pending.waitFor : null),
      queue(payload, waitFor) {
        const t = pick();
        const b = toolsGateBusy(t.provider, t.agent);
        pending = { payload, target: t, waitFor: waitFor || _toolsGateText(b) };
        paint();
        return pending.waitFor;
      },
      drop() { if (!pending) return false; pending = null; paint(); return true; },
      fire() {
        if (!pending) return;
        const payload = pending.payload;
        pending = null;
        paint();
        try { opts.start(payload); } catch (_) {}
      },
      sync: paint,
    };
    toolsGateOn(() => { if (pending && !target()) slot.fire(); else paint(); });
    return slot;
  }

  // "Benchmark on gpu-01" — the phrase every module shows while it waits.
  function _toolsGateText(busy) {
    if (!busy) return 'the run in progress';
    return busy.label + (busy.host ? ' on ' + busy.host : '');
  }

  function _tNum(v, dp) {
    return (v == null || !isFinite(v)) ? null : Number(v).toFixed(dp == null ? 1 : dp);
  }

  // Trim "NVIDIA GeForce RTX 4090" and multi-GPU configs to a short tile value.
  function _tGpuShort(cfg) {
    if (!cfg) return null;
    let s = String(cfg).replace(/NVIDIA GeForce\s*/gi, '').replace(/NVIDIA\s*/gi, '')
      .replace(/AMD Radeon\s*/gi, '').split('+')[0].trim();
    return s.length > 14 ? s.slice(0, 13) + '…' : (s || null);
  }

  // Shared shape for a runnable tool tile: status/last/sub/action derived once.
  function _runToolDesc(cfg, row, running, local) {
    const pending = !running && _toolsPendingFor(cfg.id);
    const subVal = row ? (cfg.sub ? cfg.sub(row) : (_tNum(cfg.tps(row)) || '—') + ' t/s') : null;
    const core = row
      ? '<b>' + TC.esc(TC.age(row.ts) || '') + '</b> · '
        + TC.esc(_tProv(row.provider)) + ' · ' + TC.esc(_tHost(row.agent_id))
      : null;
    return {
      id: cfg.id, icon: cfg.icon, tone: cfg.tone, name: cfg.name, desc: cfg.desc,
      status: running ? 'running' : pending ? 'queued' : 'ready',
      stats: row ? cfg.stats(row) : null,
      empty: cfg.empty,
      last: pending ? 'queued behind ' + TC.esc(pending)
        : core ? 'last run ' + core : 'no runs yet',
      lastShort: row ? '<b>' + TC.esc(TC.when(row.ts) || '—') + '</b>' : '—',
      sub: row ? subVal + ' · ' + (TC.age(row.ts) || '') : null,
      action: local ? 'View run' : (row ? 'Open' : 'Set up'),
      primary: !local,
    };
  }

  function _toolsRunsFor(tool) { return _toolsRuns.filter(r => r.tool === tool); }

  function _toolDescriptors() {
    const run = _toolsRunning();
    const bench = _toolsRunLatest.benchmark || _toolsRunsFor('benchmark')[0] || null;
    const at = _toolsRunLatest.autotune || _toolsRunsFor('autotune')[0] || null;
    const hist = list =>
      String(list.length) + (list.length >= _LEDGER_CAP ? '+' : '');
    const histTool = tool =>
      _toolsRunTotals[tool] != null ? String(_toolsRunTotals[tool])
        : hist(_toolsRunsFor(tool));
    const tools = [
      _runToolDesc({
        id: 'reportcard', icon: '▤', tone: 1, name: 'Report Card',
        desc: 'Measure a GPU’s speed, power draw, and running cost with one standard test.',
        empty: '<b>Never run.</b> Pick a host and a reference model, then run the standard test.',
        tps: c => (c.result || {}).gen_tps,
        stats: c => [
          { v: _tNum((c.result || {}).gen_tps) || '—', u: 't/s', l: 'Last score' },
          { v: _tGpuShort((c.result || {}).gpu_config) || '—', l: 'GPU' },
          { v: hist(_toolsRc), u: 'runs', l: 'History' },
        ],
      }, _toolsRc[0] || null, run.rc, run.local.rc),
      _runToolDesc({
        id: 'benchmark', icon: '◷', tone: 2, name: 'Benchmark',
        desc: 'See how fast a model runs at different prompt sizes.',
        empty: '<b>Never run.</b> Pick one or more models and measure their speed.',
        tps: b => (b.summary || {}).gen_tps,
        stats: b => [
          { v: _tNum((b.summary || {}).gen_tps) || '—', u: 't/s', l: 'Last gen' },
          { v: String(b.model_id || '').slice(0, 14), l: 'Model' },
          { v: (b.summary || {}).bench_tool || '—', l: 'Tool' },
        ],
      }, bench, run.bench, run.local.bench),
      _runToolDesc({
        id: 'autotune', icon: '⌖', tone: 3, name: 'Autotune',
        desc: 'Pick an objective and let Autotune search context, KV cache, offload, threads, speculative decoding, slots, and sampling.',
        empty: '<b>Never run.</b> Pick a model and an objective; Autotune measures each dimension and recommends a config.',
        sub: a => {
          const s = a.summary || {};
          return a.ok && s.ctx_size != null ? 'ctx ' + Number(s.ctx_size).toLocaleString() : 'context tuner';
        },
        stats: a => {
          const s = a.summary || {};
          const gain = s.gain_pct != null ? (s.gain_pct >= 0 ? '+' : '') + Math.round(s.gain_pct) : null;
          return [
            { v: a.ok && s.ctx_size != null ? Number(s.ctx_size).toLocaleString() : '—', l: 'Last ctx' },
            { v: a.ok && gain != null ? gain : '—', u: '%', l: 'Gain' },
            { v: histTool('autotune'), u: 'runs', l: 'History' },
          ];
        },
      }, at, run.at, run.local.at),
      _runToolDesc({
        id: 'quality', icon: '⚖', tone: 4, name: 'Quality guard',
        desc: 'Check any config change against f16 with a KL-divergence pass before you apply it.',
        empty: '<b>Never run.</b> Pick a model, change a key, and measure the quality cost.',
        sub: q => { const s = q.summary || {}; return s.kl != null ? 'KL ' + Number(s.kl).toFixed(3) : 'quality check'; },
        stats: q => {
          const s = q.summary || {};
          return [
            { v: s.kl != null ? Number(s.kl).toFixed(3) : '—', l: 'Last KL' },
            { v: s.kl_pass == null ? '—' : s.kl_pass ? 'pass' : 'fail', l: 'Guard' },
            { v: histTool('quality'), u: 'runs', l: 'History' },
          ];
        },
      }, _toolsRunLatest.quality || _toolsRunsFor('quality')[0] || null, run.at, run.local.at),
    ];
    return tools;
  }

  function _toolsView() { return TC.viewOf(_tLayout()); }

  function _toolsSetView(v) {
    const l = _tLayout();
    if (l) {
      l.toolsView = TC.validView(v);
      try { saveLayout(); } catch (_) {}
    }
    _toolsRenderLauncher();
    _toolsSyncSeg();
  }

  function _toolsSyncSeg() {
    const seg = _tEl('toolsViewSeg');
    if (!seg) return;
    const v = _toolsView();
    seg.querySelectorAll('button[data-view]').forEach(b =>
      b.classList.toggle('on', b.dataset.view === v));
  }

  function _toolsRenderLauncher() {
    const host = _tEl('toolsLauncher');
    if (host) host.innerHTML = TC.launcher(_toolDescriptors(), _toolsView());
  }

  function _toolsLedgerRows() {
    const rows = [];
    _toolsRc.forEach(c => {
      const r = c.result || {};
      const bits = [];
      if (r.gen_tps != null) bits.push('<b>' + TC.esc(_tNum(r.gen_tps)) + ' t/s</b>');
      if (r.avg_watts != null) bits.push(TC.esc(_tNum(r.avg_watts, 0)) + ' W');
      if (r.usd_per_mtok != null) bits.push('$' + TC.esc(_tNum(r.usd_per_mtok, 2)) + '/M');
      rows.push({ icon: '▤', tool: 'Report Card', toolId: 'reportcard',
        title: 'Open Report Card', model: r.model || '', host: _tHost(c.agent_id),
        result: bits.join(' · ') || '—', tps: r.gen_tps, ts: c.ts });
    });
    _toolsRuns.forEach(r => {
      const s = r.summary || {};
      // These modules only drive the primary llama agent; rows recorded
      // from other providers or hosts stay inert (#769 semantics).
      const clickable = r.provider === 'llama' && r.agent_id === _toolsDefaultLlama;
      if (r.tool === 'benchmark') {
        const bits = [];
        if (s.gen_tps != null) bits.push('<b>' + TC.esc(_tNum(s.gen_tps)) + ' t/s</b> gen');
        if (s.ppt_tps != null) bits.push(TC.esc(_tNum(s.ppt_tps, 0)) + ' pp/s');
        if (s.bench_tool) bits.push(TC.esc(s.bench_tool));
        if (!r.ok) bits.push('<span style="color:var(--crit)">failed</span>');
        rows.push({ icon: '◷', tool: 'Benchmark',
          toolId: clickable ? 'benchmark' : null,
          title: clickable ? 'Open Benchmark' : null, model: r.model_id || '',
          host: _tHost(r.agent_id),
          result: bits.join(' · ') || '—', tps: s.gen_tps, ts: r.ts });
      } else if (r.tool === 'autotune') {
        const bits = [];
        if (r.ok && s.ctx_size != null) bits.push('<b>ctx ' + TC.esc(Number(s.ctx_size).toLocaleString()) + '</b>');
        if (r.ok && s.free_mb != null) bits.push(TC.esc(_tNum(s.free_mb, 0)) + ' MB free');
        if (r.ok && s.objective) bits.push(TC.esc(String(s.objective)));
        if (r.ok && s.gain_pct != null) bits.push('<b>' + (s.gain_pct >= 0 ? '+' : '') + TC.esc(String(Math.round(s.gain_pct))) + ' %</b>');
        if (!r.ok) bits.push('<span style="color:var(--crit)">failed</span>');
        else if (s.verify_ok === false) bits.push('verify failed');
        rows.push({ icon: '⌖', tool: 'Autotune',
          toolId: clickable ? 'autotune' : null,
          title: clickable ? 'Open Autotune' : null, model: r.model_id || '',
          host: _tHost(r.agent_id),
          result: bits.join(' · ') || '—', tps: s.decode_tps ?? null, ts: r.ts });
      } else if (r.tool === 'quality') {
        const bits = [];
        if (s.kl != null) bits.push('<b>KL ' + TC.esc(Number(s.kl).toFixed(4)) + '</b>');
        if (s.changed) bits.push(TC.esc(String(s.changed)));
        bits.push(!r.ok ? '<span style="color:var(--crit)">failed</span>' : s.kl_pass ? 'pass' : '<span style="color:var(--warn)">fail</span>');
        rows.push({ icon: '⚖', tool: 'Quality', toolId: clickable ? 'quality' : null, title: clickable ? 'Open Quality guard' : null,
          model: r.model_id || '', host: _tHost(r.agent_id), result: bits.join(' · '), tps: null, ts: r.ts });
      }
    });
    // Newest 100 overall, then the active tool filter and column sort.
    rows.sort((a, b) => (TC.toMs(b.ts) || 0) - (TC.toMs(a.ts) || 0));
    return rows.slice(0, _LEDGER_CAP);
  }

  function _ledgerSorted(rows) {
    const { key, dir } = _ledgerSort;
    const mul = dir === 'asc' ? 1 : -1;
    const val = r => key === 'ts' ? (TC.toMs(r.ts) || 0)
      : key === 'tps' ? (r.tps == null ? -Infinity : r.tps)
      : String(r[key] || '').toLowerCase();
    return [...rows].sort((a, b) => {
      const x = val(a), y = val(b);
      return (x < y ? -1 : x > y ? 1 : 0) * mul;
    });
  }

  function _toolsRenderLedger() {
    const body = _tEl('toolsLedgerBody');
    const pager = _tEl('toolsLedgerPager');
    if (body) {
      let rows = _toolsLedgerRows();
      if (_ledgerTool !== 'all') rows = rows.filter(r => r.tool === _ledgerTool);
      rows = _ledgerSorted(rows);
      const pages = Math.max(1, Math.ceil(rows.length / _LEDGER_PAGE));
      _ledgerPage = Math.min(Math.max(0, _ledgerPage), pages - 1);
      const page = rows.slice(_ledgerPage * _LEDGER_PAGE,
                              (_ledgerPage + 1) * _LEDGER_PAGE);
      body.innerHTML = TC.ledger(page, _ledgerSort);
      if (pager) {
        pager.innerHTML = pages > 1
          ? `<button class="mcbtn mcbtn-ghost mcbtn-sm" data-pg="prev"${_ledgerPage === 0 ? ' disabled' : ''}>‹</button>` +
            `<span class="pg">${_ledgerPage + 1} / ${pages}</span>` +
            `<button class="mcbtn mcbtn-ghost mcbtn-sm" data-pg="next"${_ledgerPage >= pages - 1 ? ' disabled' : ''}>›</button>`
          : '';
      }
    }
    const sec = _tEl('toolsLedgerSec');
    const l = _tLayout();
    if (sec) sec.classList.toggle('collapsed', !!(l && l.toolsLedgerCollapsed));
  }

  // Called by report-card.js/bench-autotune.js whenever a run stream opens or
  // closes; keeps the sub-tab dot and launcher pills in sync without polling.
  function toolsSyncRunDot() {
    const local = _toolsRunningLocal();
    // A local stream that just ended re-polls, so the pill can't stay busy
    // on a stale snapshot for a whole poll interval.
    ['rc', 'bench', 'at'].forEach(k => {
      if (_toolsLocalWas[k] && !local[k]) toolsPollActivity();
      _toolsLocalWas[k] = local[k];
    });
    const dot = _tEl('toolsRunDot');
    if (dot) dot.classList.toggle('on', _toolsRunning().any);
    const home = _tEl('toolsHome');
    if (_toolsInited && home && home.style.display !== 'none') _toolsRenderLauncher();
    _toolsGateNotify();
  }

  const _TOOL_MODS = { reportcard: 'toolsMod', benchmark: 'toolsModBench', autotune: 'toolsModAt', quality: 'toolsModQg' };
  const _TOOL_CHIPS = { reportcard: 'toolsChipReportcard', benchmark: 'toolsChipBenchmark', autotune: 'toolsChipAutotune', quality: 'toolsChipQuality' };

  // Context chip in a module head: the model a deep link pre-filled (#770).
  function _toolsSetChip(id, modelId) {
    const chip = _tEl(_TOOL_CHIPS[id] || '');
    if (!chip) return;
    if (!modelId) {
      chip.style.display = 'none';
      chip.innerHTML = '';
      delete chip.dataset.model;
      return;
    }
    const short = String(modelId).split('/').pop() || modelId;
    chip.innerHTML = TC.esc(short)
      + ' <button class="ctx-chip-x" type="button" title="Clear pre-selected model">✕</button>';
    chip.title = modelId;
    chip.dataset.model = modelId;
    chip.style.display = '';
  }

  // Modules sharing /api/llm/autotune/stream; the outgoing one closes its EventSource.
  const _TOOL_STREAMS = { autotune: () => window.AT, quality: () => window.QG };
  let _toolsOpenId = null;

  function _toolsDetach(id) {
    const mod = id && _TOOL_STREAMS[id] && _TOOL_STREAMS[id]();
    if (mod && typeof mod.detach === 'function') { try { mod.detach(); } catch (_) {} }
  }

  // keepId stays attached; every other open module's stream is closed, never cancelled.
  function _toolsHideModules(keepId) {
    if (_toolsOpenId && _toolsOpenId !== keepId) _toolsDetach(_toolsOpenId);
    _toolsOpenId = keepId || null;
    Object.values(_TOOL_MODS).forEach(m => {
      const el = _tEl(m);
      if (el) el.style.display = 'none';
    });
  }

  function toolsOpenTool(id, modelId, opts) {
    const modId = _TOOL_MODS[id];
    if (!modId) return;
    const home = _tEl('toolsHome');
    if (home) home.style.display = 'none';
    _toolsHideModules(id);
    const run = _toolsRunningLocal();
    const mod = _tEl(modId);
    if (mod) mod.style.display = 'block';
    // Chip only when the model actually pre-fills — a live run keeps its state.
    const willInit =
      (id === 'reportcard' && !run.rc && typeof initReportCard === 'function')
      || (id === 'benchmark' && !run.bench && typeof openBench === 'function')
      || (id === 'autotune' && !run.at && window.AT)
      || (id === 'quality' && !run.at && window.QG);
    _toolsSetChip(id, willInit && modelId ? modelId : null);
    // A live run keeps its pickers and progress; re-init only when idle.
    if (id === 'reportcard') {
      if (!run.rc && typeof initReportCard === 'function') initReportCard(modelId || undefined);
    } else if (id === 'benchmark') {
      if (window.BL) BL.onOpen(modelId || undefined);
      if (!run.bench && typeof openBench === 'function') openBench(modelId || undefined);
      else if (typeof _benchChart !== 'undefined' && _benchChart) {
        try { _benchChart.resize(); } catch (_) {}
      }
    } else if (id === 'autotune') {
      if (window.AT) AT.onOpen(modelId || undefined, opts);
    } else if (id === 'quality') {
      if (window.QG) QG.onOpen(modelId || undefined, opts);
    }
  }

  // Entry point for model-card ⋯ actions: land on the Tools tab with the
  // tool's module open and the model pre-filled (#770).
  let _toolsPendingOpen = false;
  function toolsDeepLink(id, modelId, opts) {
    if (typeof switchTab === 'function' && typeof _activeTab !== 'undefined'
        && _activeTab !== 'llm') switchTab('llm');
    _toolsPendingOpen = true;
    try {
      if (typeof switchSubTab === 'function') switchSubTab('llm', 'tools');
    } finally { _toolsPendingOpen = false; }
    toolsOpenTool(id, modelId || null, opts);
  }

  function toolsClearHistory() {
    if (!confirm('Clear the run history for all tools? Saved per-model benchmark badges are kept. This cannot be undone.')) return;
    const f = typeof _fetchT === 'function' ? _fetchT : (u, o) => fetch(u, o);
    Promise.allSettled([
      f('/api/reportcard/history', { method: 'DELETE' }),
      f('/api/tools/runs', { method: 'DELETE' }),
    ]).then(() => {
      _toolsFetchedAt = 0;
      _toolsRefresh();
    });
  }

  function toolsCloseModule() {
    _toolsHideModules();
    const home = _tEl('toolsHome');
    if (home) home.style.display = 'block';
    _toolsFetchedAt = 0;
    _toolsRefresh();
  }

  function _toolsWire() {
    const seg = _tEl('toolsViewSeg');
    if (seg) seg.addEventListener('click', ev => {
      const b = ev.target.closest('button[data-view]');
      if (b) _toolsSetView(b.dataset.view);
    });
    const launcher = _tEl('toolsLauncher');
    const activate = t => toolsOpenTool(t.dataset.tool, null);
    if (launcher) {
      launcher.addEventListener('click', ev => {
        const t = ev.target.closest('[data-tool]');
        if (t) activate(t);
      });
      launcher.addEventListener('keydown', ev => {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        const t = ev.target.closest('[data-tool]');
        if (t) { ev.preventDefault(); activate(t); }
      });
    }
    const head = _tEl('toolsLedgerHead');
    if (head) head.addEventListener('click', () => {
      const sec = _tEl('toolsLedgerSec');
      const collapsed = sec && sec.classList.toggle('collapsed');
      const l = _tLayout();
      if (l) { l.toolsLedgerCollapsed = !!collapsed; try { saveLayout(); } catch (_) {} }
    });
    const body = _tEl('toolsLedgerBody');
    if (body) body.addEventListener('click', ev => {
      const th = ev.target.closest('th[data-sort]');
      if (th) {
        const key = th.dataset.sort;
        _ledgerSort = _ledgerSort.key === key
          ? { key, dir: _ledgerSort.dir === 'desc' ? 'asc' : 'desc' }
          : { key, dir: key === 'ts' || key === 'tps' ? 'desc' : 'asc' };
        _ledgerPage = 0;
        _toolsRenderLedger();
        return;
      }
      const tr = ev.target.closest('tr.rowlink');
      if (tr && tr.dataset.tool) toolsOpenTool(tr.dataset.tool, tr.dataset.model || null);
    });
    const filter = _tEl('toolsLedgerFilter');
    if (filter) filter.addEventListener('change', () => {
      _ledgerTool = filter.value;
      _ledgerPage = 0;
      _toolsRenderLedger();
    });
    const pager = _tEl('toolsLedgerPager');
    if (pager) pager.addEventListener('click', ev => {
      const b = ev.target.closest('button[data-pg]');
      if (!b || b.disabled) return;
      _ledgerPage += b.dataset.pg === 'next' ? 1 : -1;
      _toolsRenderLedger();
    });
    // Chip dismiss: hide the chip and un-tick the pre-selected model (#770).
    Object.entries(_TOOL_CHIPS).forEach(([id, cid]) => {
      const chip = _tEl(cid);
      if (!chip) return;
      chip.addEventListener('click', ev => {
        if (!ev.target.closest('.ctx-chip-x')) return;
        const model = chip.dataset.model || '';
        _toolsSetChip(id, null);
        if (id === 'reportcard') {
          if (typeof initReportCard === 'function') initReportCard();
          return;
        }
        // The model panel populates after an async fetch — retry the untick
        // briefly so a fast ✕ click still clears the pre-selection.
        const untick = tries => {
          const panel = _tEl(id === 'benchmark' ? 'benchModelPanel' : 'atModelList');
          if (id === 'benchmark') {
            const cb = panel && [...panel.querySelectorAll('input[type=checkbox]')]
              .find(c => c.value === model);
            if (cb) {
              cb.checked = false;
              cb.dispatchEvent(new Event('change'));
            } else if (tries > 0) {
              setTimeout(() => untick(tries - 1), 250);
            }
            return;
          }
          // atModelList: models are mc-toggle buttons now, not checkboxes (#880).
          const btn = panel && [...panel.querySelectorAll('.mc-toggle[data-model]')]
            .find(b => b.dataset.model === model);
          if (btn) {
            if (btn.classList.contains('on')) btn.click();
          } else if (tries > 0) {
            setTimeout(() => untick(tries - 1), 250);
          }
        };
        untick(20);
      });
    });
  }

  function _toolsRefresh() {
    if (Date.now() - _toolsFetchedAt < 10000 || !_claim('toolsRefresh')) {
      _toolsRenderLauncher();
      _toolsRenderLedger();
      return Promise.resolve();
    }
    const f = typeof _fetchT === 'function' ? _fetchT : (u => fetch(u));
    const j = r => (r.ok ? r.json() : Promise.reject(new Error('http ' + r.status)));
    return Promise.allSettled([
      f('/api/agents/list-by-provider').then(j),
      f('/api/reportcard/recent?limit=100').then(j),
      f('/api/tools/runs?limit=100').then(j),
    ]).then(([agents, rc, runs]) => {
      if (agents.status === 'fulfilled') _toolsApplyAgents(agents.value);
      if (rc.status === 'fulfilled') _toolsRc = rc.value.cards || [];
      if (runs.status === 'fulfilled') {
        _toolsRuns = runs.value.runs || [];
        _toolsRunTotals = runs.value.totals || {};
        _toolsRunLatest = runs.value.latest || {};
      }
      if (rc.status === 'fulfilled' || runs.status === 'fulfilled') {
        _toolsFetchedAt = Date.now();
      }
      _toolsRenderLauncher();
      _toolsRenderLedger();
    }).finally(() => _release('toolsRefresh'));
  }

  function initToolsTab() {
    if (!_toolsInited) {
      _toolsInited = true;
      _toolsWire();
    }
    // A deep link opens its module right away — skip the launcher paint
    // and its fetches; toolsCloseModule refreshes on the way back.
    if (_toolsPendingOpen) return;
    // Entry always lands on the launcher; a hidden module keeps no state.
    _toolsHideModules();
    const home = _tEl('toolsHome');
    if (home) home.style.display = 'block';
    _toolsSyncSeg();
    _toolsRefresh();
    toolsSyncRunDot();
  }

  window.initToolsTab = initToolsTab;
  window.toolsCloseModule = toolsCloseModule;
  window.toolsOpenTool = toolsOpenTool;
  window.toolsDeepLink = toolsDeepLink;
  window.toolsClearHistory = toolsClearHistory;
  window.toolsSyncRunDot = toolsSyncRunDot;
  window.toolsPollActivity = toolsPollActivity;
  window.toolsGateBusy = toolsGateBusy;
  window.toolsGateOn = toolsGateOn;
  window.toolsGateRefusal = toolsGateRefusal;
  window.toolsSetQueued = toolsSetQueued;
  window.toolsQueueSlot = toolsQueueSlot;
})();
