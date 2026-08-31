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
  const _LEDGER_CAP = 100, _LEDGER_PAGE = 15;

  function _tEl(id) { return document.getElementById(id); }
  function _tLayout() { return (typeof layout === 'object' && layout) ? layout : null; }

  function _tProv(p) {
    return (((typeof RC !== 'undefined' && RC.PROVIDER_LABEL) || {})[p]) || p || '';
  }

  function _toolsRunning() {
    const rc = typeof _rcEventSrc !== 'undefined' && _rcEventSrc;
    const bench = typeof _benchEventSrc !== 'undefined' && _benchEventSrc;
    const at = typeof _atEventSrc !== 'undefined' && _atEventSrc;
    return { rc: !!rc, bench: !!bench, at: !!at, any: !!(rc || bench || at) };
  }

  function _tHost(agentId) { return _toolsAgents[agentId] || agentId || ''; }

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
  function _runToolDesc(cfg, row, running) {
    const subVal = row ? (cfg.sub ? cfg.sub(row) : (_tNum(cfg.tps(row)) || '—') + ' t/s') : null;
    const core = row
      ? '<b>' + TC.esc(TC.age(row.ts) || '') + '</b> · '
        + TC.esc(_tProv(row.provider)) + ' · ' + TC.esc(_tHost(row.agent_id))
      : null;
    return {
      id: cfg.id, icon: cfg.icon, tone: cfg.tone, name: cfg.name, desc: cfg.desc,
      status: running ? 'running' : 'ready',
      stats: row ? cfg.stats(row) : null,
      empty: cfg.empty,
      last: core ? 'last run ' + core : 'no runs yet',
      lastShort: row ? '<b>' + TC.esc(TC.when(row.ts) || '—') + '</b>' : '—',
      sub: row ? subVal + ' · ' + (TC.age(row.ts) || '') : null,
      action: running ? 'View run' : (row ? 'Open' : 'Set up'),
      primary: !running,
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
      }, _toolsRc[0] || null, run.rc),
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
      }, bench, run.bench),
      _runToolDesc({
        id: 'autotune', icon: '⌖', tone: 3, name: 'Autotune',
        desc: 'Automatically size each model’s context to the memory you have free.',
        empty: '<b>Set a memory target.</b> Pick the models to tune and Autotune finds each one’s best context size.',
        sub: a => {
          const s = a.summary || {};
          return a.ok && s.ctx_size != null ? 'ctx ' + Number(s.ctx_size).toLocaleString() : 'context tuner';
        },
        stats: a => {
          const s = a.summary || {};
          return [
            { v: a.ok && s.ctx_size != null ? Number(s.ctx_size).toLocaleString() : '—', l: 'Last ctx' },
            { v: a.ok ? (_tNum(s.free_mb, 0) || '—') : '—', u: 'MB', l: 'Free VRAM' },
            { v: histTool('autotune'), u: 'runs', l: 'History' },
          ];
        },
      }, at, run.at),
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
        if (!r.ok) bits.push('<span style="color:var(--crit)">failed</span>');
        else if (!s.converged) bits.push('not converged');
        rows.push({ icon: '⌖', tool: 'Autotune',
          toolId: clickable ? 'autotune' : null,
          title: clickable ? 'Open Autotune' : null, model: r.model_id || '',
          host: _tHost(r.agent_id),
          result: bits.join(' · ') || '—', tps: null, ts: r.ts });
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
    const dot = _tEl('toolsRunDot');
    if (dot) dot.classList.toggle('on', _toolsRunning().any);
    const home = _tEl('toolsHome');
    if (_toolsInited && home && home.style.display !== 'none') _toolsRenderLauncher();
  }

  const _TOOL_MODS = { reportcard: 'toolsMod', benchmark: 'toolsModBench', autotune: 'toolsModAt' };
  const _TOOL_CHIPS = { benchmark: 'toolsChipBenchmark', autotune: 'toolsChipAutotune' };

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

  function _toolsHideModules() {
    Object.values(_TOOL_MODS).forEach(m => {
      const el = _tEl(m);
      if (el) el.style.display = 'none';
    });
  }

  function toolsOpenTool(id, modelId) {
    const modId = _TOOL_MODS[id];
    if (!modId) return;
    const run = _toolsRunning();
    const home = _tEl('toolsHome');
    if (home) home.style.display = 'none';
    _toolsHideModules();
    const mod = _tEl(modId);
    if (mod) mod.style.display = 'block';
    // Chip only when the model actually pre-fills — a live run keeps its state.
    const willInit =
      (id === 'benchmark' && !run.bench && typeof openBench === 'function')
      || (id === 'autotune' && !run.at && typeof openAutotune === 'function');
    _toolsSetChip(id, willInit && modelId ? modelId : null);
    // A live run keeps its pickers and progress; re-init only when idle.
    if (id === 'reportcard') {
      if (!run.rc && typeof initReportCard === 'function') initReportCard();
    } else if (id === 'benchmark') {
      if (!run.bench && typeof openBench === 'function') openBench(modelId || undefined);
      else if (typeof _benchChart !== 'undefined' && _benchChart) {
        try { _benchChart.resize(); } catch (_) {}
      }
    } else if (id === 'autotune') {
      if (!run.at && typeof openAutotune === 'function') openAutotune(modelId || undefined);
    }
  }

  // Entry point for model-card ⋯ actions: land on the Tools tab with the
  // tool's module open and the model pre-filled (#770).
  let _toolsPendingOpen = false;
  function toolsDeepLink(id, modelId) {
    if (typeof switchTab === 'function' && typeof _activeTab !== 'undefined'
        && _activeTab !== 'llm') switchTab('llm');
    _toolsPendingOpen = true;
    try {
      if (typeof switchSubTab === 'function') switchSubTab('llm', 'tools');
    } finally { _toolsPendingOpen = false; }
    toolsOpenTool(id, modelId || null);
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
        // The model panel populates after an async fetch — retry the untick
        // briefly so a fast ✕ click still clears the pre-selection.
        const untick = tries => {
          const panel = _tEl(id === 'benchmark' ? 'benchModelPanel' : 'atModelPanel');
          const cb = panel && [...panel.querySelectorAll('input[type=checkbox]')]
            .find(c => c.value === model);
          if (cb) {
            cb.checked = false;
            cb.dispatchEvent(new Event('change'));
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
      if (agents.status === 'fulfilled') {
        _toolsAgents = {};
        Object.entries(agents.value || {}).forEach(([prov, list]) =>
          (list || []).forEach(a => {
            _toolsAgents[a.agent_id] = a.hostname;
            if (prov === 'llama' && a.is_default) _toolsDefaultLlama = a.agent_id;
          }));
      }
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
})();
