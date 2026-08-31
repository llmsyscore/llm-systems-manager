// Tools tab controller (#769): launcher (card/list/compact), run ledger,
// module shell. Rendering lives in js/lib/toolcards.js (window.TC).
(function () {
  let _toolsInited = false;
  let _toolsAgents = {};   // agent_id -> hostname
  let _toolsDefaultLlama = null;
  let _toolsRc = [];       // recent report cards, newest first
  let _toolsBench = [];    // latest benchmark result per model, newest first
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
    return { rc: !!rc, bench: !!bench, any: !!(rc || bench) };
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
    const tps = row ? _tNum(cfg.tps(row)) : null;
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
      sub: row ? (tps || '—') + ' t/s · ' + (TC.age(row.ts) || '') : null,
      action: running ? 'View run' : (row ? 'Open' : 'Set up'),
      primary: !running,
    };
  }

  function _toolDescriptors() {
    const run = _toolsRunning();
    const tools = [
      _runToolDesc({
        id: 'reportcard', icon: '▤', tone: 1, name: 'Report Card',
        desc: 'Measure a GPU’s speed, power draw, and running cost with one standard test.',
        empty: '<b>Never run.</b> Pick a host and a reference model, then run the standard test.',
        tps: c => (c.result || {}).gen_tps,
        stats: c => [
          { v: _tNum((c.result || {}).gen_tps) || '—', u: 't/s', l: 'Last score' },
          { v: _tGpuShort((c.result || {}).gpu_config) || '—', l: 'GPU' },
          { v: String(_toolsRc.length) + (_toolsRc.length >= _LEDGER_CAP ? '+' : ''), u: 'runs', l: 'History' },
        ],
      }, _toolsRc[0] || null, run.rc),
      _runToolDesc({
        id: 'benchmark', icon: '◷', tone: 2, name: 'Benchmark',
        desc: 'See how fast a model runs at different prompt sizes.',
        empty: '<b>Never run.</b> Pick one or more models and measure their speed.',
        tps: b => b.avg_gen_tps,
        stats: b => [
          { v: _tNum(b.avg_gen_tps) || '—', u: 't/s', l: 'Last gen' },
          { v: String(b.model_id || '').slice(0, 14), l: 'Model' },
          { v: b.bench_tool || '—', l: 'Tool' },
        ],
      }, _toolsBench[0] || null, run.bench),
      {
        id: 'autotune', icon: '⌖', tone: 3, name: 'Autotune',
        desc: 'Automatically size each model’s context to the memory you have free.',
        status: 'ready', stats: null,
        empty: '<b>Set a memory target.</b> Pick the models to tune and Autotune finds each one’s best context size.',
        last: '', sub: 'context tuner', action: 'Open', primary: true,
      },
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
    _toolsBench.forEach(b => {
      const bits = [];
      if (b.avg_gen_tps != null) bits.push('<b>' + TC.esc(_tNum(b.avg_gen_tps)) + ' t/s</b> gen');
      if (b.avg_ppt_tps != null) bits.push(TC.esc(_tNum(b.avg_ppt_tps, 0)) + ' pp/s');
      if (b.bench_tool) bits.push(TC.esc(b.bench_tool));
      // The benchmark overlay only drives the primary llama agent; rows for
      // other providers or hosts stay inert.
      const clickable = b.provider === 'llama' && b.agent_id === _toolsDefaultLlama;
      rows.push({ icon: '◷', tool: 'Benchmark',
        toolId: clickable ? 'benchmark' : null,
        title: clickable ? 'Open Benchmark' : null,
        model: b.model_id || '', host: _tHost(b.agent_id),
        result: bits.join(' · ') || '—', tps: b.avg_gen_tps, ts: b.ts });
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

  function toolsOpenTool(id, modelId) {
    const run = _toolsRunning();
    if (id === 'reportcard') {
      const home = _tEl('toolsHome'), mod = _tEl('toolsMod');
      if (home) home.style.display = 'none';
      if (mod) mod.style.display = 'block';
      // A live run keeps its pickers and progress; re-init only when idle.
      if (!run.rc && typeof initReportCard === 'function') initReportCard();
      return;
    }
    if (id === 'benchmark' && !run.bench && typeof openBench === 'function') openBench(modelId);
    if (id === 'autotune' && typeof openAutotune === 'function') openAutotune();
  }

  function toolsClearHistory() {
    if (!confirm('Clear all report card and benchmark history? This cannot be undone.')) return;
    const f = typeof _fetchT === 'function' ? _fetchT : (u, o) => fetch(u, o);
    Promise.allSettled([
      f('/api/reportcard/history', { method: 'DELETE' }),
      f('/api/benchmark/results', { method: 'DELETE' }),
    ]).then(() => {
      _toolsFetchedAt = 0;
      _toolsRefresh();
    });
  }

  function toolsCloseModule() {
    const home = _tEl('toolsHome'), mod = _tEl('toolsMod');
    if (mod) mod.style.display = 'none';
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
      f('/api/benchmark/recent?limit=100').then(j),
    ]).then(([agents, rc, bench]) => {
      if (agents.status === 'fulfilled') {
        _toolsAgents = {};
        Object.entries(agents.value || {}).forEach(([prov, list]) =>
          (list || []).forEach(a => {
            _toolsAgents[a.agent_id] = a.hostname;
            if (prov === 'llama' && a.is_default) _toolsDefaultLlama = a.agent_id;
          }));
      }
      if (rc.status === 'fulfilled') _toolsRc = rc.value.cards || [];
      if (bench.status === 'fulfilled') _toolsBench = bench.value.results || [];
      if (rc.status === 'fulfilled' || bench.status === 'fulfilled') {
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
    // Entry always lands on the launcher; a hidden module keeps no state.
    const home = _tEl('toolsHome'), mod = _tEl('toolsMod');
    if (mod) mod.style.display = 'none';
    if (home) home.style.display = 'block';
    _toolsSyncSeg();
    _toolsRefresh();
    toolsSyncRunDot();
  }

  window.initToolsTab = initToolsTab;
  window.toolsCloseModule = toolsCloseModule;
  window.toolsOpenTool = toolsOpenTool;
  window.toolsClearHistory = toolsClearHistory;
  window.toolsSyncRunDot = toolsSyncRunDot;
})();
