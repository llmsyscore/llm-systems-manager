// Live benchmark module (#879): speed-bench against the running server.
// Classic script; exposes window.BL. Shares /api/benchmark/stream + cancel.
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => (window.TC && TC.esc ? TC.esc(String(s ?? '')) : String(s ?? ''));
  const PRESETS = {
    chat:    { bench: 'qualitative',   cats: 'all',    osl: 1024, limit: 8 },
    coding:  { bench: 'qualitative',   cats: 'coding', osl: 1024, limit: 12 },
    rag:     { bench: 'throughput_8k', cats: 'all',    osl: 256,  limit: 6 },
    agentic: { bench: 'throughput_1k', cats: 'all',    osl: 512,  limit: 12 },
    longctx: { matrix: { benches: ['throughput_1k', 'throughput_8k', 'throughput_32k'], osls: [256, 1024] }, cats: 'all', limit: 4, sweep: [1] },
  };
  const BENCH_LABEL = { qualitative: 'qual', throughput_1k: '1k', throughput_2k: '2k', throughput_8k: '8k', throughput_16k: '16k', throughput_32k: '32k' };
  const BENCH_ORDER = Object.keys(BENCH_LABEL);
  let _model = null, _pre = null, _runs = [], _es = null, _chart = null;
  let _levels = [], _baseline = null, _lastDoc = null, _runId = null, _activeLevel = null, _lastTps = null, _cell = null;
  let _attached = false, _queued = null, _elapsedIv = null, _runStart = 0, _sweepLevels = [], _curLevel = null, _curCell = null, _busyOn = false, _lastCfg = null, _attachedRun = null;
  let _fleetHosts = [], _fleetJob = null, _fleetPoll = null, _fleetSel = null;
  let _base = null, _baseTimer = null, _baseAutoAttached = null, _slot = null;
  const _baseOpenDet = new Set();  // run ids with an expanded config-detail row
  const FLEET_POLL_MS = 3000;

  // ISO server timestamp -> local "YYYY-MM-DD HH:MM"; '' for falsy/invalid.
  function fmtTs(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function parseSweep(text) {
    const seen = new Set();
    String(text || '').split(/[,\s]+/).forEach(t => {
      const n = parseInt(t, 10);
      if (Number.isInteger(n) && n >= 1 && n <= 64) seen.add(n);
    });
    const out = [...seen].sort((a, b) => a - b).slice(0, 8);
    return out.length ? out : [1];
  }
  // Sweep levels come from the chips unless the Custom chip is on, which uses #blSweep.
  function sweepCustom() {
    const c = document.querySelector('#blSweepChips .bl-chip[data-conc="custom"]');
    return !!(c && c.classList.contains('on'));
  }
  function sweepLevels() {
    if (!document.querySelector('#blSweepChips .bl-chip') || sweepCustom()) return parseSweep($('blSweep').value);
    const on = [...document.querySelectorAll('#blSweepChips .bl-chip.on')]
      .map(c => parseInt(c.dataset.conc, 10)).filter(n => Number.isInteger(n));
    const out = [...new Set(on)].sort((a, b) => a - b).slice(0, 8);
    return out.length ? out : [1];
  }
  function syncSweepUi() {
    const row = $('blSweepRow') || $('blSweep');
    if (row) row.style.display = sweepCustom() ? '' : 'none';
  }
  function parseOsls(text) {
    const seen = new Set();
    String(text || '').split(/[,\s]+/).forEach(t => {
      const n = parseInt(t, 10);
      if (Number.isInteger(n) && n >= 16 && n <= 8192) seen.add(n);
    });
    const out = [...seen].sort((a, b) => a - b).slice(0, 4);
    return out.length ? out : [256];
  }
  function matrixOn() { return !!document.querySelector('#blMatrixTgl .bl-chip.on'); }
  // Toggles the matrix chip and swaps which rail rows are visible.
  function setMatrix(on, benches, osls) {
    document.querySelectorAll('#blMatrixTgl .bl-chip').forEach(c => c.classList.toggle('on', !!on));
    const rows = $('blMatrixRows'); if (rows) rows.style.display = on ? '' : 'none';
    const br = $('blBenchRow'); if (br) br.style.display = on ? 'none' : '';
    const orow = $('blOslRow'); if (orow) orow.style.display = on ? 'none' : '';
    if (benches) document.querySelectorAll('#blMatrixBench .bl-chip').forEach(c => c.classList.toggle('on', benches.includes(c.dataset.bench)));
    if (osls) { const el = $('blMatrixOsl'); if (el) el.value = osls.join(', '); }
  }
  function toggleMatrix() { setMatrix(!matrixOn()); markCustom(); updateEstimate(); }
  function matrixConfig() {
    const benches = BENCH_ORDER.filter(b => document.querySelector(`#blMatrixBench .bl-chip[data-bench="${b}"].on`));
    return { benches, osls: parseOsls(($('blMatrixOsl') || {}).value) };
  }
  function estimateSeconds(cfg, lastTps) {
    if (!lastTps || lastTps <= 0) return null;
    return Math.round(cfg.levels.length * cfg.samples * cfg.categories * cfg.osl / lastTps);
  }
  function deltaText(cur, base, lowerIsBetter) {
    if (cur == null || base == null || !base) return { text: 'no baseline', cls: 'flat' };
    const pct = Math.round((cur - base) / base * 100);
    const sign = pct > 0 ? '+' : (pct < 0 ? '−' : '');
    const text = `${sign}${Math.abs(pct)} % vs baseline`;
    if (Math.abs(pct) < 3) return { text, cls: 'flat' };
    const good = lowerIsBetter ? pct < 0 : pct > 0;
    return { text, cls: good ? 'up' : 'down' };
  }
  function knee(levels) {
    if (!levels || !levels.length) return null;
    const first = levels[0].all && levels[0].all.pred_tps;
    if (!first) return levels[0].concurrency;
    let k = levels[0].concurrency;
    levels.forEach(l => { if (l.all && l.all.pred_tps >= 0.7 * first) k = Math.max(k, l.concurrency); });
    return k;
  }
  function fmt(n, d = 1) { return n == null ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }); }
  function catsSelected() {
    const on = [...document.querySelectorAll('#blCats .bl-chip.on')].map(c => c.dataset.cat);
    const all = document.querySelectorAll('#blCats .bl-chip').length;
    return (!on.length || on.length === all) ? 'all' : on;
  }
  function renderCats(names, selected) {
    const host = $('blCats'); if (!host) return;
    host.innerHTML = (names || []).map(c =>
      `<span class="bl-chip${selected === 'all' || (selected || []).includes(c) ? ' on' : ''}" data-cat="${esc(c)}">${esc(c)}</span>`).join('');
    host.querySelectorAll('.bl-chip').forEach(ch => ch.addEventListener('click', () => { ch.classList.toggle('on'); markCustom(); updateEstimate(); }));
    const known = !!(names && names.length);
    const row = $('blCatsRow'); if (row) row.style.display = known ? '' : 'none';
    if (host) host.style.display = known ? '' : 'none';
    const hint = $('blCatsHint');
    if (hint && known) hint.textContent = catsSelected() === 'all' ? `all · ${names.length} of ${names.length}` : `${catsSelected().length} of ${names.length}`;
  }
  function markCustom() {
    document.querySelectorAll('#blPresets .bl-chip').forEach(c => c.classList.toggle('on', c.dataset.preset === 'custom'));
  }
  // Sweep chips as they were before a matrix preset replaced them; restored by the next non-matrix preset.
  let _sweepBefore = null;
  function applyPreset(name) {
    const p = PRESETS[name];
    document.querySelectorAll('#blPresets .bl-chip').forEach(c => c.classList.toggle('on', c.dataset.preset === name));
    if (!p) return;
    if (p.matrix) {
      if (!_sweepBefore) _sweepBefore = [...document.querySelectorAll('#blSweepChips .bl-chip')].map(c => c.classList.contains('on'));
      setMatrix(true, p.matrix.benches, p.matrix.osls);
      $('blLimit').value = String(p.limit);
      const sweepSet = new Set((p.sweep || []).map(String));
      document.querySelectorAll('#blSweepChips .bl-chip').forEach(c => c.classList.toggle('on', sweepSet.has(c.dataset.conc)));
      syncSweepUi();
      const names = ((_pre && _pre.datasets && _pre.datasets[p.matrix.benches[0]]) || {}).categories || [];
      renderCats(names, 'all');
    } else {
      setMatrix(false);
      if (_sweepBefore) { document.querySelectorAll('#blSweepChips .bl-chip').forEach((c, i) => c.classList.toggle('on', !!_sweepBefore[i])); _sweepBefore = null; syncSweepUi(); }
      $('blBench').value = p.bench; $('blOsl').value = String(p.osl); $('blLimit').value = String(p.limit);
      const names = ((_pre && _pre.datasets && _pre.datasets[p.bench]) || {}).categories || [];
      renderCats(names, p.cats === 'all' ? 'all' : names.filter(n => n.includes(p.cats)));
    }
    updateEstimate();
  }
  function config() {
    let extra = {};
    try { extra = JSON.parse($('blExtra').value || '{}'); } catch (_) { extra = null; }
    const c = { model_id: _model, bench: $('blBench').value, categories: catsSelected(),
             osl: parseInt($('blOsl').value, 10) || 1024, limit: parseInt($('blLimit').value, 10) || 8,
             concurrency: sweepLevels(), timeout_s: parseInt($('blTimeout').value, 10) || 600,
             extra_inputs: extra, baseline_run_id: $('blBaseline').value || null };
    if (matrixOn()) { const m = matrixConfig(); return { ...c, bench: m.benches[0], osl: m.osls[0], matrix: m }; }
    return c;
  }
  function catCount(c) { return c.categories === 'all' ? Math.max(1, document.querySelectorAll('#blCats .bl-chip').length) : c.categories.length; }
  function durText(s) { return s < 90 ? `~${s} s` : `~${Math.round(s / 60)} min`; }
  function updateEstimate() {
    const c = config();
    let s;
    if (c.matrix) {
      let sum = 0;
      for (const osl of c.matrix.osls) {
        const v = estimateSeconds({ levels: c.concurrency, samples: c.limit, categories: catCount(c), osl }, _lastTps);
        if (v == null) { sum = null; break; }
        sum += v;
      }
      s = sum == null ? null : sum * c.matrix.benches.length;
    } else {
      s = estimateSeconds({ levels: c.concurrency, samples: c.limit, categories: catCount(c), osl: c.osl }, _lastTps);
    }
    $('blEstimate').textContent = s == null ? '' : durText(s);
  }
  function setMode(mode) {
    mode = mode === 'offline' ? 'offline' : 'live';
    const L = typeof layout !== 'undefined' ? layout : null;
    if (L) { L.benchMode = mode; try { saveLayout(); } catch (_) {} }
    document.querySelectorAll('#benchModeSeg button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
    $('benchLive').style.display = mode === 'live' ? '' : 'none';
    $('benchOffline').style.display = mode === 'offline' ? '' : 'none';
    const note = $('benchModeNote');
    if (note) note.textContent = mode === 'live' ? 'Live · speed-bench against the running server' : 'Offline · llama-bench · server must be stopped';
    if (mode === 'live' && _chart) { try { _chart.resize(); } catch (_) {} }
    if (mode !== 'live') { clearTimeout(_baseTimer); _baseTimer = null; }
  }
  function setRunBar(missing) {
    const run = $('blRunBtn'), setup = $('blSetupBtn');
    if (run) run.style.display = missing ? 'none' : '';
    if (!setup) return;
    setup.style.display = missing ? '' : 'none';
    setup.className = missing ? 'mcbtn mcbtn-pri' : 'mcbtn mcbtn-ghost';
    setup.textContent = 'Install bench runtime';
  }
  function renderPreflight() {
    const el = $('blPreflight'); if (!el || !_pre) return;
    const s = _pre.server || {}, rt = _pre.runtime || {};
    let dot = el.querySelector('.bl-dot'), d = el.querySelector('.d');
    if (!dot || !d) { el.innerHTML = '<span class="bl-dot"></span><span class="d"></span>';
      dot = el.querySelector('.bl-dot'); d = el.querySelector('.d'); }
    const start = $('blStartBtn');
    if (start) start.style.display = s.up ? 'none' : '';
    const missing = !rt.python || !rt.script;
    setRunBar(missing);
    if (!s.up) { dot.className = 'bl-dot warn'; d.innerHTML = '<b>llama-server is down.</b> Live runs need the running server.'; el.className = 'bl-preflight warn'; }
    else if (missing) { const st = rt.script_status && rt.script_status !== 'ok' ? ` (${esc(rt.script_status)})` : '';
      dot.className = 'bl-dot warn'; d.innerHTML = `<b>Bench runtime missing.</b> Install it with the button below.${st}`; el.className = 'bl-preflight warn'; }
    else { dot.className = 'bl-dot ok'; d.innerHTML = `<b>llama-server</b> · ${esc(s.loaded_id || 'no model loaded')} · ${esc(s.slots_idle)}/${esc(s.slots_total)} slots idle`; el.className = 'bl-preflight ok'; }
    if (!_model && s.loaded_id) _model = s.loaded_id;
    const names = ((_pre.datasets || {})[$('blBench').value] || {}).categories || [];
    if (!document.querySelectorAll('#blCats .bl-chip').length) renderCats(names, 'all');
  }
  async function loadRuns() {
    if (!_model) return;
    try {
      const r = await fetch('/api/benchmark/live/runs?model_id=' + encodeURIComponent(_model)).then(r => r.json());
      _runs = (r && r.runs) || [];
    } catch (_) { _runs = []; }
    const sel = $('blBaseline');
    const pinned = _runs.find(r => r.baseline);
    sel.innerHTML = `<option value=""${pinned ? '' : ' selected'}>none</option>`
      + _runs.map(r => `<option value="${esc(r.run_id)}"${pinned && pinned.run_id === r.run_id ? ' selected' : ''}>${esc(fmtTs(r.ts))} · ${esc((r.config || {}).bench || '')} · ${fmt(r.gen_tps)} t/s${r.baseline ? ' · baseline' : ''}</option>`).join('');
    if (_runs.length && _runs[0].gen_tps) _lastTps = _runs[0].gen_tps;
    updateEstimate();
  }
  async function onOpen(modelId) {
    if (modelId && modelId !== _model && !running()) { _fleetJob = null; _fleetSel = null; renderFleet(); syncPinBtn(); }
    if (modelId) _model = modelId;
    setMode((typeof layout !== 'undefined' && layout && layout.benchMode) || 'live');
    try { _pre = await fetch('/api/benchmark/live/preflight').then(r => r.json()); } catch (_) { _pre = { server: { up: false }, runtime: {} }; }
    renderPreflight();
    await loadRuns();
    await loadBaselines();
    if (!_chart && window.Chart && $('blChart')) mkChart();
    ['blOsl', 'blLimit', 'blSweep', 'blTimeout', 'blExtra'].forEach(id => { const el = $(id); if (el && !el._bl) { el._bl = 1; el.addEventListener('input', () => { markCustom(); updateEstimate(); }); } });
    const b = $('blBench'); if (b && !b._bl) { b._bl = 1; b.addEventListener('change', () => { markCustom(); renderCats((((_pre || {}).datasets || {})[b.value] || {}).categories || [], 'all'); updateEstimate(); }); }
    document.querySelectorAll('#blPresets .bl-chip').forEach(c => { if (!c._bl) { c._bl = 1; c.addEventListener('click', () => applyPreset(c.dataset.preset)); } });
    document.querySelectorAll('#blSweepChips .bl-chip').forEach(c => { if (!c._bl) { c._bl = 1; c.addEventListener('click', () => { c.classList.toggle('on'); syncSweepUi(); updateEstimate(); }); } });
    document.querySelectorAll('#blMatrixTgl .bl-chip').forEach(c => { if (!c._bl) { c._bl = 1; c.addEventListener('click', () => toggleMatrix()); } });
    document.querySelectorAll('#blMatrixBench .bl-chip').forEach(c => { if (!c._bl) { c._bl = 1; c.addEventListener('click', () => {
      if (c.classList.contains('on') && document.querySelectorAll('#blMatrixBench .bl-chip.on').length <= 1) return;
      c.classList.toggle('on'); markCustom(); updateEstimate();
    }); } });
    const mo = $('blMatrixOsl'); if (mo && !mo._bl) { mo._bl = 1; mo.addEventListener('input', () => { markCustom(); updateEstimate(); }); }
    const be = $('blBaseEnabled'); if (be && !be._bl) { be._bl = 1; be.addEventListener('change', toggleBaseSchedule); }
    const bs = $('blBaseSettings'); if (bs && !bs._bl) { bs._bl = 1; bs.addEventListener('click', (e) => { e.preventDefault(); openBaseSettings(); }); }
    document.querySelectorAll('#blFleetTgl .bl-chip').forEach(c => { if (!c._bl) { c._bl = 1; c.addEventListener('click', () => toggleFleet()); } });
    syncSweepUi();
    const rb = $('blRunBtn'); if (rb && rb._blLabel == null) rb._blLabel = rb.textContent;
    // Another tool holding the host (an autotune batch item, say) is gated, not attached to.
    const holder = typeof toolsGateBusy === 'function' ? toolsGateBusy('llama', null) : null;
    const foreign = !!(holder && !holder.unresolved && holder.tool && holder.tool !== 'benchmark');
    if (_pre && _pre.busy && !running() && !foreign) attach();
    const sl = slot(); if (sl) sl.sync();
    const savedJob = sessionStorage.getItem('bl.fleetJob');
    if (savedJob && !running()) { _fleetJob = { job_id: savedJob, hosts: [] }; busy(true); startFleetPoll(); }
  }
  function mkChart() {
    const css = v => (window.cssVar ? cssVar(v) : '#888');
    _chart = new Chart($('blChart').getContext('2d'), { type: 'line', data: { labels: [], datasets: [
      { label: 'this run', data: [], borderColor: css('--accent'), fill: true, tension: 0, pointRadius: 4, spanGaps: true,
        backgroundColor: 'color-mix(in srgb, ' + css('--accent') + ' 14%, transparent)' },
      { label: 'baseline', data: [], borderColor: css('--fg-dim'), borderDash: [5, 4], fill: false, tension: 0, pointRadius: 3, spanGaps: true },
      { label: 'pending', data: [], pointStyle: 'circle', pointRadius: 4, pointBorderColor: css('--accent'), pointBackgroundColor: 'transparent', showLine: false } ] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } },
                 scales: { x: { title: { display: true, text: 'concurrent requests' } }, y: { beginAtZero: true, title: { display: true, text: 'aggregate decode t/s' } } } } });
  }
  function baselineLevels() { return (_baseline && _baseline.levels) || []; }
  // Cell key ties a level to its matrix cell; untagged levels all share '|'.
  function cellKey(l) { return `${l.bench || (_lastDoc && _lastDoc.bench) || ''}|${l.osl != null ? l.osl : ''}`; }
  function cells() {
    const seen = [];
    _levels.forEach(l => { const k = cellKey(l); if (!seen.includes(k)) seen.push(k); });
    return seen;
  }
  function activeCellKey() {
    const all = cells();
    return (_cell != null && all.includes(_cell)) ? _cell : (all[0] != null ? all[0] : null);
  }
  function baselineAt(conc) {
    const active = activeCellKey();
    return baselineLevels().find(l => l.concurrency === conc && (l.bench == null || cellKey(l) === active)) || null;
  }
  function renderCellSeg(active) {
    const host = $('blCellSeg'); if (!host) return;
    const all = cells();
    if (all.length <= 1) { host.style.display = 'none'; host.innerHTML = ''; return; }
    host.style.display = '';
    host.innerHTML = all.map(k => {
      const l = _levels.find(x => cellKey(x) === k) || {};
      const label = `${BENCH_LABEL[l.bench] || l.bench || ''} · osl ${l.osl != null ? l.osl : ''}`;
      return `<button type="button" data-cell="${esc(k)}" class="${k === active ? 'on' : ''}">${esc(label)}</button>`;
    }).join('');
    host.querySelectorAll('button').forEach(b => b.addEventListener('click', () => { _cell = b.dataset.cell; redraw(); }));
  }
  // Lowest-concurrency pred_tps per (bench, osl), for the heatmap.
  function heatCells(levels) {
    const map = new Map();
    (levels || []).forEach(l => {
      if (l.bench == null || l.osl == null) return;
      const key = `${l.bench}|${l.osl}`;
      const cur = map.get(key);
      const v = l.all && l.all.pred_tps;
      if (!cur || l.concurrency < cur.conc) map.set(key, { conc: l.concurrency, v: v == null ? null : v });
    });
    const benchSet = new Set(), oslSet = new Set();
    map.forEach((_v, key) => { const i = key.lastIndexOf('|'); benchSet.add(key.slice(0, i)); oslSet.add(Number(key.slice(i + 1))); });
    const benches = BENCH_ORDER.filter(b => benchSet.has(b));
    const osls = [...oslSet].sort((a, b) => a - b);
    let max = null;
    map.forEach(e => { if (e.v != null && (max == null || e.v > max)) max = e.v; });
    return { benches, osls, get: (b, o) => { const e = map.get(`${b}|${o}`); return e && e.v != null ? e.v : null; }, max };
  }
  function renderHeat() {
    const card = $('blHeatCard'); const host = $('blHeat'), cap = $('blHeatCaption');
    if (!card || !host || !cap) return;
    const h = heatCells(_levels);
    const show = h.benches.length * h.osls.length >= 2;
    card.style.display = show ? '' : 'none';
    if (!show) { host.innerHTML = ''; cap.textContent = ''; return; }
    host.style.gridTemplateColumns = `auto repeat(${h.benches.length}, 1fr)`;
    let html = '<div class="bl-heat-h"></div>' + h.benches.map(b => `<div class="bl-heat-h">${esc(BENCH_LABEL[b] || b)}</div>`).join('');
    h.osls.forEach(osl => {
      html += `<div class="bl-heat-l">osl ${osl}</div>`;
      const first = h.get(h.benches[0], osl);
      h.benches.forEach((b, i) => {
        const v = h.get(b, osl);
        const pct = (h.max > 0 && v != null) ? Math.round(10 + 60 * v / h.max) : 0;
        const delta = (i > 0 && v != null && first > 0) ? `<small>${esc(deltaText(v, first).text.replace(' vs baseline', ''))}</small>` : '';
        html += `<div class="bl-heat-cell" style="--p:${pct}">${fmt(v)}${delta}</div>`;
      });
    });
    host.innerHTML = html;
    let caption = '';
    for (const osl of h.osls) {
      const present = h.benches.filter(b => h.get(b, osl) != null);
      if (present.length < 2) continue;
      const firstB = present[0], lastB = present[present.length - 1];
      const first = h.get(firstB, osl), last = h.get(lastB, osl);
      if (firstB !== lastB && first > 0) {
        caption = `Decode at ${BENCH_LABEL[lastB] || lastB} runs ${Math.round(100 - 100 * last / first)} % slower than at ${BENCH_LABEL[firstB] || firstB} (osl ${osl}).`;
        break;
      }
    }
    cap.textContent = caption;
  }
  function redraw() {
    const active = activeCellKey();
    const cur = _levels.filter(l => cellKey(l) === active);
    const pend = (running() && cur.length && _curLevel != null && (_curCell == null || _curCell === active) && !cur.some(l => l.concurrency === _curLevel)) ? _curLevel : null;
    const labels = [...new Set([...cur.map(l => l.concurrency), ...baselineLevels().map(l => l.concurrency), ...(pend == null ? [] : [pend])])].sort((a, b) => a - b);
    const empty = $('blChartEmpty'); if (empty) empty.style.display = cur.length ? 'none' : '';
    if (_chart) {
      const lastAgg = cur.length ? cur[cur.length - 1].all.agg_pred_tps : null;
      _chart.data.labels = labels.map(String);
      _chart.data.datasets[0].data = labels.map(c => { const l = cur.find(x => x.concurrency === c); return l ? l.all.agg_pred_tps : null; });
      _chart.data.datasets[1].data = labels.map(c => { const l = baselineAt(c); return l ? l.all.agg_pred_tps : null; });
      if (_chart.data.datasets[2]) _chart.data.datasets[2].data = labels.map(c => (pend != null && c === pend) ? lastAgg : null);
      _chart.update('none');
    }
    const k = knee(cur);
    $('blChartCaption').textContent = cur.length > 1 && k ? `Per-request decode stays within 70 % of single-request speed up to ${k} concurrent.` : '';
    const first = cur[0] && cur[0].all, b0 = baselineAt(1) && baselineAt(1).all;
    const tiles = first ? [
      ['decode · 1 request', fmt(first.pred_tps), 't/s', deltaText(first.pred_tps, b0 && b0.pred_tps), true],
      ['prefill', fmt(first.prompt_tps, 0), 't/s', deltaText(first.prompt_tps, b0 && b0.prompt_tps)],
      ['latency · mean', fmt(first.latency_s, 1), 's', deltaText(first.latency_s, b0 && b0.latency_s, true)],
      ['draft accept rate', first.accept_rate == null ? '—' : Math.round(first.accept_rate * 100), first.accept_rate == null ? '' : '%',
        first.accept_rate == null ? { text: 'no draft', cls: 'flat' } : (b0 && b0.accept_rate != null ? deltaText(first.accept_rate, b0.accept_rate) : { text: 'baseline had no draft', cls: 'flat' })],
    ] : [];
    $('blTiles').innerHTML = tiles.map(([l, v, u, d, hi]) => `<div class="bl-tile${hi ? ' hi' : ''}"><div class="v">${v}<em>${u}</em></div><div class="l">${esc(l)}</div><div class="d ${d.cls}">${esc(d.text)}</div></div>`).join('');
    const seg = $('blLevelSeg');
    seg.innerHTML = cur.map(l => `<button type="button" data-level="${l.concurrency}" class="${l.concurrency === (_activeLevel || (cur[0] && cur[0].concurrency)) ? 'on' : ''}">${l.concurrency}</button>`).join('');
    seg.querySelectorAll('button').forEach(b => b.addEventListener('click', () => { _activeLevel = parseInt(b.dataset.level, 10); redraw(); }));
    renderTable(cur);
    renderCellSeg(active);
    renderHeat();
    syncAttachBtn(); syncPinBtn();
  }
  function renderTable(cur) {
    const conc = _activeLevel || (cur[0] && cur[0].concurrency);
    const lv = (cur || []).find(l => l.concurrency === conc); const host = $('blTable');
    if (!lv) { host.innerHTML = '<div class="bl-hint" style="padding:12px">No results yet.</div>'; return; }
    const bl = baselineAt(conc); const brow = c => ((bl && bl.rows) || []).find(r => r.category === c);
    const row = (r, tot) => { const b = tot ? (bl && bl.all) : brow(r.category); const cur = tot ? r.pred_tps : r.avg_pred_t_s; const base = b && (tot ? b.pred_tps : b.avg_pred_t_s); const d = deltaText(cur, base);
      return `<tr${tot ? ' class="tot"' : ''}><td>${esc(tot ? 'all' : r.category)}</td><td class="num">${tot ? r.requests : r.requests}</td><td class="num">${fmt(tot ? r.prompt_tps : r.avg_prompt_t_s, 0)}</td><td class="num">${fmt(cur)}<span class="dlt ${d.cls}">${base ? esc(d.text.replace(' vs baseline', '')) : ''}</span></td><td class="num">${fmt(tot ? r.latency_s : r.avg_latency, 1)} s</td><td class="num">${(tot ? r.accept_rate : r.accept_rate) == null ? '—' : Math.round((tot ? r.accept_rate : r.accept_rate) * 100) + ' %'}</td></tr>`; };
    host.innerHTML = `<table class="bl-rt"><thead><tr><th>Category</th><th class="num">samples</th><th class="num">prompt t/s</th><th class="num">decode t/s</th><th class="num">latency</th><th class="num">accept</th></tr></thead><tbody>${lv.rows.map(r => row(r, false)).join('')}${row(lv.all, true)}</tbody></table>`;
  }
  // Ranks done hosts by decode t/s desc; the rest keep their original order untagged.
  function rankHosts(hosts) {
    const list = hosts || [];
    const done = list.filter(h => h.status === 'done' && typeof h.gen_tps === 'number');
    const sorted = done.slice().sort((a, b) => b.gen_tps - a.gen_tps);
    const best = sorted.length ? sorted[0].gen_tps : null;
    const ranked = sorted.map((h, i) => ({ ...h, rank: i + 1, pctOfBest: best ? h.gen_tps / best : null }));
    const rest = list.filter(h => !(h.status === 'done' && typeof h.gen_tps === 'number')).map(h => ({ ...h, rank: null, pctOfBest: null }));
    return [...ranked, ...rest];
  }
  function fleetOn() {
    const chip = document.querySelector('#blFleetTgl .bl-chip[data-fleet="1"]');
    return !!(chip && chip.classList.contains('on'));
  }
  function fleetAgents() {
    const host = $('blFleetHosts'); if (!host) return [];
    return [...host.querySelectorAll('.bl-chip.on')].map(c => c.dataset.agent).filter(Boolean);
  }
  async function loadFleetHosts() {
    if (!_model) { _fleetHosts = []; return; }
    let r;
    try { r = await fetch('/api/benchmark/live/hosts?model_id=' + encodeURIComponent(_model)).then(res => res.json()); }
    catch (_) { r = null; }
    _fleetHosts = (r && r.hosts) || [];
    const host = $('blFleetHosts');
    if (host) {
      host.innerHTML = _fleetHosts.map(h => {
        const title = h.loaded ? '' : (h.online ? 'not loaded' : 'offline');
        return `<span class="bl-chip ${h.loaded ? 'on' : 'off'}" data-agent="${esc(h.agent_id)}"${title ? ` title="${esc(title)}"` : ''}>${esc(h.hostname)}</span>`;
      }).join('');
      host.querySelectorAll('.bl-chip[data-agent]').forEach(c => { if (!c.classList.contains('off')) c.addEventListener('click', () => c.classList.toggle('on')); });
    }
    const hint = $('blFleetHint');
    if (hint) { const total = _fleetHosts.length, loaded = _fleetHosts.filter(h => h.loaded).length; hint.textContent = `${loaded} of ${total} hosts`; }
  }
  function toggleFleet() {
    const chip = document.querySelector('#blFleetTgl .bl-chip[data-fleet="1"]'); if (!chip) return;
    const on = !chip.classList.contains('on');
    chip.classList.toggle('on', on);
    const hostsEl = $('blFleetHosts'), note = $('blFleetNote');
    if (on) {
      if (hostsEl) hostsEl.style.display = ''; if (note) note.style.display = '';
      loadFleetHosts();
    } else {
      if (hostsEl) { hostsEl.style.display = 'none'; hostsEl.innerHTML = ''; }
      if (note) note.style.display = 'none';
      _fleetHosts = [];
      const hint = $('blFleetHint'); if (hint) hint.textContent = '';
    }
    updateEstimate();
  }
  function renderFleet() {
    const card = $('blFleetCard'); if (!card) return;
    card.style.display = _fleetJob ? '' : 'none';
    const table = $('blFleetTable');
    if (!_fleetJob) { if (table) table.innerHTML = ''; return; }
    const hosts = _fleetJob.hosts || [];
    const meta = $('blFleetMeta');
    if (meta) {
      const modelShort = String(_fleetJob.model_id || _model || '').split('/').pop() || '';
      const bench = (_fleetJob.config && _fleetJob.config.bench) || '';
      meta.textContent = [modelShort, bench].filter(Boolean).join(' · ');
    }
    const prog = $('blFleetProgress');
    if (prog) {
      const total = hosts.length;
      const finished = hosts.filter(h => h.status === 'done' || h.status === 'failed' || h.status === 'cancelled').length;
      prog.textContent = (!_fleetJob.done && total) ? `${finished}/${total} finished` : '';
    }
    if (!table) return;
    const ranked = rankHosts(hosts);
    const bestGen = ranked.length && ranked[0].rank === 1 ? ranked[0].gen_tps : null;
    const row = h => {
      const done = h.status === 'done';
      const bar = h.pctOfBest != null ? `<div class="bl-rank-bar" style="width:${Math.round(h.pctOfBest * 100)}%"></div>` : '';
      const delta = (h.rank === 1 || h.pctOfBest == null || bestGen == null) ? '' : esc(deltaText(h.gen_tps, bestGen).text.replace(' vs baseline', ''));
      const sel = done && _fleetSel === h.agent_id ? ' on' : '';
      const attr = done ? ` data-agent="${esc(h.agent_id)}"` : '';
      const titleParts = [h.error, h.matrix_ignored ? 'matrix ignored (old agent)' : null].filter(Boolean);
      const title = titleParts.length ? ` title="${esc(titleParts.join(' · '))}"` : '';
      return `<tr class="bl-frow${sel}"${attr}>` +
        `<td>${h.rank != null ? `<span class="bl-rank">${h.rank}</span>` : ''}</td>` +
        `<td>${esc(h.hostname)}</td>` +
        `<td><span${title}>${esc(h.status)}</span>${h.matrix_ignored ? ' ⚠' : ''}</td>` +
        `<td class="num">${fmt(h.gen_tps)}${bar}</td>` +
        `<td class="num">${fmt(h.agg_max_tps)}</td>` +
        `<td class="num">${fmt(h.latency_s, 1)}</td>` +
        `<td class="num">${h.accept_rate == null ? '—' : Math.round(h.accept_rate * 100) + ' %'}</td>` +
        `<td class="num">${fmt(h.wh_per_ktok, 2)}</td>` +
        `<td class="num">${delta}</td></tr>`;
    };
    table.innerHTML = `<table class="bl-rt"><thead><tr><th>#</th><th>Host</th><th>Status</th><th class="num">decode t/s</th>` +
      `<th class="num">aggregate max</th><th class="num">latency</th><th class="num">accept</th><th class="num">Wh/1k</th><th class="num">Δ vs best</th></tr></thead>` +
      `<tbody>${ranked.map(row).join('')}</tbody></table>`;
    table.querySelectorAll('tr.bl-frow[data-agent]').forEach(tr => {
      tr.addEventListener('click', () => {
        table.querySelectorAll('tr.bl-frow').forEach(r => r.classList.remove('on'));
        tr.classList.add('on');
        selectFleetHost(tr.dataset.agent);
      });
    });
  }
  async function selectFleetHost(agentId) {
    _fleetSel = agentId;
    const host = _fleetJob && (_fleetJob.hosts || []).find(h => h.agent_id === agentId);
    if (host && host.run_id) {
      let d;
      try { d = await fetch('/api/benchmark/live/runs/' + encodeURIComponent(host.run_id)).then(res => res.json()); }
      catch (_) { d = null; }
      if (d && d.ok && d.run) {
        _levels = d.run.levels || []; _lastDoc = { ...d.run, run_id: host.run_id, ok: true }; _cell = null;
        redraw();
        const meta = $('blChartMeta'); if (meta) meta.textContent = 'aggregate decode t/s · ' + host.hostname;
        log(`showing ${host.hostname} · run ${host.run_id}`, 'dim');
      }
    }
    renderFleet();
    syncAttachBtn(); syncPinBtn();
  }
  function setProgress(done, total) { const bar = $('blProgress') && $('blProgress').querySelector('i'); if (bar) bar.style.width = (total ? Math.round(done / total * 100) : 0) + '%'; }
  // Logs every host whose status changed between two polls of the autopilot job.
  function logFleetChanges(prev, next) {
    const before = {}; ((prev && prev.hosts) || []).forEach(h => { before[h.agent_id] = h.status; });
    ((next && next.hosts) || []).forEach(h => {
      if (before[h.agent_id] === h.status) return;
      if (h.status === 'running') log(`${h.hostname}: started`);
      else if (h.status === 'done') log(`${h.hostname}: done · decode ${fmt(h.gen_tps)} t/s · latency ${fmt(h.latency_s, 1)} s${h.wh_per_ktok != null ? ` · ${fmt(h.wh_per_ktok, 2)} Wh / 1k tokens` : ''}`, 'ok');
      else if (h.status === 'failed') log(`${h.hostname}: failed${h.error ? ' · ' + h.error : ''}`, 'warn');
      else if (h.status === 'cancelled') log(`${h.hostname}: cancelled`, 'dim');
    });
  }
  function stopFleetPoll() { if (_fleetPoll) { clearInterval(_fleetPoll); _fleetPoll = null; } }
  function startFleetPoll() {
    stopFleetPoll();
    fleetTick();
    _fleetPoll = setInterval(fleetTick, FLEET_POLL_MS);
  }
  async function fleetTick() {
      if (!_fleetJob) return;
      let d;
      try { d = await fetch('/api/benchmark/live/fleet/' + encodeURIComponent(_fleetJob.job_id)).then(res => res.json()); }
      catch (_) { return; }
      if (!d || !d.ok || !d.job) {
        stopFleetPoll(); sessionStorage.removeItem('bl.fleetJob'); _fleetJob = null; _fleetSel = null;
        busy(false); stopElapsed(); setStatus('autopilot job lost', 'err'); renderFleet(); syncPinBtn(); return;
      }
      logFleetChanges(_fleetJob, d.job); _fleetJob = d.job; renderFleet();
      const hosts = _fleetJob.hosts || [], total = hosts.length;
      const finished = hosts.filter(h => h.status === 'done' || h.status === 'failed' || h.status === 'cancelled').length;
      if (!_fleetJob.done) { setStatus('running · autopilot ' + finished + '/' + total, 'running'); setProgress(finished, total); return; }
      stopFleetPoll(); busy(false); stopElapsed(); sessionStorage.removeItem('bl.fleetJob');
      const ranking = _fleetJob.ranking || [];
      if (_fleetJob.cancelled) setStatus('cancelled', 'err');
      else if (ranking.length) setStatus('complete · autopilot', 'ok');
      else setStatus('failed', 'err');
      const best = ranking.length ? hosts.find(h => h.agent_id === ranking[0]) : null;
      $('blStrip').textContent = best ? `${total} hosts · best ${best.hostname} ${fmt(best.gen_tps)} t/s` : `${total} hosts`;
      log(best ? `autopilot ranking complete · best ${best.hostname} ${fmt(best.gen_tps)} t/s` : (_fleetJob.cancelled ? 'autopilot job cancelled' : 'autopilot job failed on every host'), best ? 'ok' : 'warn');
      setProgress(total, total);
      if (best) await selectFleetHost(best.agent_id);
      await loadRuns();
  }
  function log(text, cls) { const el = $('blLog'); if (!el) return; const t = new Date().toTimeString().slice(0, 8); el.innerHTML += `<div><span class="dim">${t}</span> ${cls ? `<span class="${cls}">` : ''}${esc(text)}${cls ? '</span>' : ''}</div>`; el.scrollTop = el.scrollHeight; }
  function setStatus(text, state) { const el = $('blStatus'); el.textContent = text; el.classList.remove('running', 'ok', 'err'); if (state) el.classList.add(state); }
  function running() { return !!_es || _attached || !!_fleetPoll; }
  // Cancel is for runs this tab started; while attached it only drops a queued config.
  function syncCancelBtn() {
    const b = $('blCancelBtn'); if (!b) return;
    if (b._blLabel == null) b._blLabel = b.textContent;
    const drop = (_attached && !!_queued) || !!(_slot && _slot.queued());
    b.style.display = (drop || (_busyOn && !_attached)) ? '' : 'none';
    b.textContent = drop ? 'Drop queued run' : (b._blLabel || '');
  }
  function busy(on) { _busyOn = on; $('blRunBtn').disabled = on; syncCancelBtn(); $('blProgress').style.display = on ? '' : 'none'; if (_slot) _slot.sync(); if (typeof toolsSyncRunDot === 'function') toolsSyncRunDot(); }
  // Shows "Add to Report Card" only after a successful, idle run.
  function syncAttachBtn() {
    const b = $('blAttachBtn'); if (!b) return;
    const ok = !!(_lastDoc && _lastDoc.ok && _lastDoc.run_id) && !running();
    b.style.display = ok ? '' : 'none';
    if (!ok || _lastDoc.run_id !== _attachedRun) b._blAdded = false;
    if (!ok || !b._blAdded) { b.textContent = 'Add to Report Card'; b.disabled = false; b._blAdded = false; }
  }
  // A fleet host's run belongs to another agent, so it can't be pinned as this host's baseline.
  function syncPinBtn() {
    const b = $('blPinBtn'); if (!b) return;
    b.style.display = _fleetSel ? 'none' : '';
    b.disabled = !!_fleetSel;
  }
  async function addToReportCard() {
    const b = $('blAttachBtn'); if (!b || !_lastDoc) return;
    if (_attachedRun === _lastDoc.run_id) { if (typeof toolsDeepLink === 'function') toolsDeepLink('reportcard', _model); return; }
    b.disabled = true;
    let d;
    try { d = await fetch('/api/reportcard/attach-live', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: _lastDoc.run_id }) }).then(r => r.json()); }
    catch (e) { d = { ok: false, error: String(e) }; }
    if (!d || !d.ok) { b.disabled = false; setStatus('add to Report Card failed', 'err'); return; }
    b.textContent = d.attached === 'merged' ? '✓ Added to card · open' : '✓ Card created · open';
    b._blAdded = true; b.disabled = false; _attachedRun = _lastDoc.run_id;
    log('report card: ' + d.attached, 'ok');
  }
  function setStrip(done, total) {
    const parts = [], n = _sweepLevels.length, i = _curLevel == null ? 0 : _sweepLevels.indexOf(_curLevel) + 1;
    if (i > 0 && n) parts.push(`sweep ${i} / ${n}`);
    if (_curLevel != null) parts.push(`concurrency ${_curLevel}`);
    if (total) parts.push(`sample ${done} / ${total}`);
    $('blStrip').textContent = parts.join(' · ');
  }
  function remainText() {
    const left = _sweepLevels.slice(_levels.length);
    if (!_levels.length || !left.length) return '';
    const c = config(), first = _levels[0].all && _levels[0].all.pred_tps;
    const s = estimateSeconds({ levels: left, samples: c.limit, categories: catCount(c), osl: c.osl }, first || _lastTps);
    return s == null ? '' : ` · ${durText(s)} left`;
  }
  function stopElapsed() { if (_elapsedIv) { clearInterval(_elapsedIv); _elapsedIv = null; } }
  function startElapsed() {
    stopElapsed(); _runStart = Date.now();
    const tick = () => { const el = $('blElapsed'); if (!el) return; const s = Math.round((Date.now() - _runStart) / 1000);
      el.textContent = `elapsed ${Math.floor(s / 60)} m ${s % 60} s${remainText()}`; };
    tick(); _elapsedIv = setInterval(tick, 1000);
  }
  function notice(on, text) {
    const n = $('blNotice'); if (!n) return;
    n.textContent = on ? (text || 'A benchmark is already running on this host. New runs queue behind it.') : '';
    n.style.display = on ? '' : 'none';
  }
  // Shared gate (#888): another tool on this host turns Run into Queue.
  function slot() {
    if (!_slot && typeof toolsQueueSlot === 'function') {
      _slot = toolsQueueSlot('benchmark', {
        provider: () => 'llama',
        start: (cfg) => run(cfg, { now: true }),
        render: (st) => syncQueue(st),
      });
    }
    return _slot;
  }
  function syncQueue(st) {
    if (_attached || running() || _busyOn) { syncCancelBtn(); return; }
    const b = st.busy;
    runLabel(st.queued || b ? 'Queue run' : null);
    notice(!!(st.queued || b), st.queued
      ? `Queued behind ${st.waitFor} — this run starts on its own when that finishes.`
      : b ? `${b.label} is running on ${b.host}. New runs queue behind it.` : '');
    const rb = $('blRunBtn'); if (rb) rb.disabled = !!st.queued;
    if (st.queued) setStatus(`queued · starts when ${st.waitFor} finishes`, 'running');
    syncCancelBtn();
  }
  function runLabel(text) { const b = $('blRunBtn'); if (b) b.textContent = text == null ? (b._blLabel || '') : text; }
  function leaveAttached() { _attached = false; notice(false); runLabel(null); syncCancelBtn(); }
  // Follows a run started by another tab (or the scheduler) — replays its stream and queues ours behind it.
  function attach(recheck) {
    _attached = true; _runId = null; _queued = null;
    stopElapsed(); const el = $('blElapsed'); if (el) el.textContent = '';
    openStream(); busy(true);
    $('blRunBtn').disabled = false; runLabel('Queue run');
    if (recheck) { startElapsed(); setStatus('running · re-check', 'running'); }
    else setStatus('running · started elsewhere', 'running');
    notice(true);
    if (typeof toolsSyncRunDot === 'function') toolsSyncRunDot();
  }
  async function run(cfg, opts) {
    const c = cfg || config();
    if (!c.model_id) { setStatus('pick a model', 'err'); return; }
    if (c.extra_inputs === null) { setStatus('request extras must be a JSON object', 'err'); return; }
    if (c.matrix && (!c.matrix.benches.length || c.matrix.benches.length * c.matrix.osls.length * c.concurrency.length > 24)) {
      // Mirrors MATRIX_MAX_CELLS in the agent.
      setStatus('matrix too large (max 24 cells × levels)', 'err'); return;
    }
    if (_attached) {
      if (fleetOn()) { setStatus('finish or cancel the attached run first', 'err'); return; }
      _queued = c; setStatus('queued · starts when the current run finishes', 'running'); $('blRunBtn').disabled = true; syncCancelBtn(); return;
    }
    const s = slot(), gateBusy = s && !fleetOn() && !(opts && opts.now) && s.busy();
    if (gateBusy) { s.queue(c); return; }
    if (fleetOn()) {
      const agents = fleetAgents();
      if (!agents.length) { setStatus('no host has this model loaded', 'err'); return; }
      if ($('blRunBtn').disabled) return;
      const { model_id, ...config } = c;
      let d;
      try { d = await fetch('/api/benchmark/live/fleet', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model_id, agents, config }) }).then(r => r.json()); }
      catch (e) { d = { ok: false, error: String(e) }; }
      if (!d || !d.ok) { setStatus(d && d.error ? d.error : 'failed to start', 'err'); return; }
      _fleetJob = { job_id: d.job_id, hosts: [] };
      sessionStorage.setItem('bl.fleetJob', d.job_id);
      busy(true); startElapsed(); _levels = []; _lastDoc = null; _activeLevel = null; _cell = null; _fleetSel = null; _baseline = null;
      $('blLog').innerHTML = ''; setProgress(0, 0); syncAttachBtn(); syncPinBtn();
      log(`autopilot job ${d.job_id} · ${agents.length} host${agents.length === 1 ? '' : 's'} · ${config.bench || ''}`);
      redraw(); renderFleet(); startFleetPoll();
      return;
    }
    if ($('blRunBtn').disabled) return;
    _lastCfg = c;
    busy(true); _levels = []; _lastDoc = null; _activeLevel = null; _cell = null; _fleetJob = null; _fleetSel = null; renderFleet(); $('blLog').innerHTML = ''; setProgress(0, 0);
    syncAttachBtn(); syncPinBtn();
    _baseline = null; _sweepLevels = (c.concurrency || []).slice(); _curLevel = null; startElapsed();
    if (c.baseline_run_id) { try { const r = await fetch('/api/benchmark/live/runs/' + encodeURIComponent(c.baseline_run_id)).then(r => r.json()); _baseline = r && r.run; } catch (_) {} }
    redraw(); setStatus('starting…', 'running');
    let d;
    try { d = await fetch('/api/benchmark/live/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(c) }).then(r => r.json()); }
    catch (e) { d = { ok: false, error: String(e) }; }
    if (!d || !d.ok) {
      busy(false); stopElapsed();
      if (d && d.runtime) { _pre = Object.assign(_pre || {}, { runtime: d.runtime }); renderPreflight(); }
      // Lost the race with another browser — the gate holds the run until the host frees up.
      if (s && typeof toolsGateRefusal === 'function' && toolsGateRefusal(d && (d.error || d.detail))) { s.queue(c, 'the run in progress'); return; }
      setStatus(d && d.error ? d.error : 'failed to start', 'err'); return;
    }
    _runId = d.run_id; openStream();
  }
  function openStream() {
    if (_es) { try { _es.close(); } catch (_) {} }
    _es = SG.open({ url: '/api/benchmark/stream', maxDrops: 6,
      onReconnecting: () => setStatus('reconnecting…', 'running'),
      onRestored: () => setStatus('running', 'running'),
      onLost: () => { _es = null; _attached = false; notice(false); runLabel(null); setStatus('disconnected', 'err'); busy(false); stopElapsed(); },
      onEvent: (msg) => {
        if (msg.run_id && _runId && msg.run_id !== _runId) return;
        if (msg.type === 'model_start') { if (_attached && msg.run_id) _runId = msg.run_id;
          if ((msg.levels || []).length) _sweepLevels = msg.levels.slice();
          log(`run ${msg.run_id} · ${msg.bench} · levels ${(msg.levels || []).join(', ')}`); if (msg.cmd) log('$ ' + msg.cmd, 'dim');
          if (msg.matrix) log(`matrix · benches ${(msg.matrix.benches || []).join(', ')} · osls ${(msg.matrix.osls || []).join(', ')}`, 'dim'); }
        else if (msg.type === 'level_start') { _curLevel = msg.concurrency; _curCell = msg.bench ? cellKey(msg) : null; if (!_attached) setStatus('running', 'running'); setStrip(0, 0);
          const tag = msg.bench ? ` · ${msg.bench}${msg.osl != null ? ' · osl ' + msg.osl : ''}` : '';
          log(`level ${msg.concurrency} started${tag}`); redraw(); }
        else if (msg.type === 'progress') { const pct = msg.total ? Math.round(msg.done / msg.total * 100) : 0; $('blProgress').querySelector('i').style.width = pct + '%'; _curLevel = msg.level; setStrip(msg.done, msg.total); }
        else if (msg.type === 'line') { log(msg.text || '', 'dim'); }
        else if (msg.type === 'level_result') { _levels.push(msg); _lastTps = _levels[0].all.pred_tps || _lastTps; log(`level ${msg.concurrency} done · decode ${fmt(msg.all.pred_tps)} t/s · aggregate ${fmt(msg.all.agg_pred_tps)} t/s`, 'ok'); redraw(); }
        else if (msg.type === 'setup_step') { log(`${msg.step}: ${msg.text || ''}`); }
        else if (msg.type === 'setup_done') { log(msg.ok ? 'runtime ready' : `setup failed: ${msg.error || ''}`, msg.ok ? 'ok' : 'warn');
          if (msg.runtime) { _pre = Object.assign(_pre || {}, { runtime: msg.runtime }); renderPreflight(); }
          if (_es) { try { _es.close(); } catch (_) {} _es = null; }
          _queued = null; leaveAttached(); stopElapsed();
          setStatus(msg.ok ? 'runtime ready' : 'setup failed', msg.ok ? 'ok' : 'err'); busy(false); }
        else if (msg.type === 'model_done') { _lastDoc = msg; const e = msg.wh_per_ktok != null ? ` · ${fmt(msg.wh_per_ktok, 2)} Wh / 1k tokens` : '';
          const sp = (msg.spec && msg.spec.n_max != null) ? ` · draft window ${msg.spec.n_min}–${msg.spec.n_max}` : '';
          const prefix = (msg.config && msg.config.matrix) ? `${cells().length} cells · ` : `${(msg.levels || []).length} levels · `;
          $('blStrip').textContent = `${prefix}${fmt(msg.elapsed_s, 0)} s${e}${sp}`;
          if (_lastCfg && _lastCfg.matrix && msg.config && !msg.config.matrix) log('agent ignored the matrix — upgrade the agent to v2026.09.08-8 or newer', 'warn'); }
        else if (msg.type === 'done') {
          if (_es) { try { _es.close(); } catch (_) {} _es = null; }
          stopElapsed(); _curLevel = null; redraw(); loadRuns(); loadBaselines(); syncAttachBtn(); syncPinBtn();
          if (_fleetPoll) return;
          busy(false);
          if (_attached) { const q = _queued; _queued = null; leaveAttached();
            if (q) { setStatus('starting…', 'running'); run(q, { now: true }); return; } }
          setStatus(msg.ok ? 'complete' : (msg.cancelled ? 'cancelled' : 'failed'), msg.ok ? 'ok' : 'err'); }
      } });
    if (typeof toolsSyncRunDot === 'function') toolsSyncRunDot();
  }
  function cancel() {
    // A live run — including a fleet job — outranks a pending queued one.
    if (!running() && !_busyOn && _slot && _slot.drop()) { setStatus('queued run dropped'); busy(false); return; }
    if (_fleetJob && !_fleetJob.done) {
      fetch('/api/benchmark/live/fleet/' + encodeURIComponent(_fleetJob.job_id) + '/cancel', { method: 'POST' }).catch(() => {});
      setStatus('cancelling…', 'running');
      return;
    }
    if (_attached) {
      if (!_queued) return;
      _queued = null; runLabel('Queue run'); $('blRunBtn').disabled = false; syncCancelBtn(); setStatus('queued run dropped');
      return;
    }
    if (_es) { try { _es.close(); } catch (_) {} _es = null; }
    _queued = null; leaveAttached(); stopElapsed(); _curLevel = null; redraw();
    fetch('/api/benchmark/cancel', { method: 'POST' }).catch(() => {}); setStatus('cancelled', 'err'); busy(false); }
  async function setup() {
    if (_attached) { setStatus('a run is in progress on this host', 'err'); return; }
    if ($('blRunBtn').disabled) return;
    busy(true); $('blLog').innerHTML = ''; setStatus('installing runtime…', 'running');
    let d; try { d = await fetch('/api/benchmark/live/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prefetch: [$('blBench').value] }) }).then(r => r.json()); } catch (e) { d = { ok: false, error: String(e) }; }
    if (!d || !d.ok) { setStatus(d && d.error ? d.error : 'setup failed', 'err'); busy(false); return; }
    _runId = d.run_id || null; openStream();
  }
  async function startServer() {
    const b = $('blStartBtn'); if (b) b.disabled = true;
    try { await fetch('/api/llm/server/start', { method: 'POST' }); } catch (_) {}
    await new Promise(r => setTimeout(r, 3000));
    try { _pre = await fetch('/api/benchmark/live/preflight').then(r => r.json()); } catch (_) {}
    if (b) b.disabled = false;
    renderPreflight();
  }
  async function pinBaseline() { if (_fleetSel) return; const id = (_lastDoc && _lastDoc.run_id) || _runId; if (!id) return; await fetch('/api/benchmark/live/runs/' + encodeURIComponent(id) + '/baseline', { method: 'POST' }).catch(() => {}); loadRuns(); loadBaselines(); }
  function baselineMeta(s) {
    s = s || {};
    const alert = `alert past −${Math.round(s.regression_pct || 0)} %`;
    if (!s.enabled) return alert;
    const parts = [];
    if (s.nightly_at) parts.push(s.nightly_valid ? `nightly at ${s.nightly_at}` : 'nightly time invalid');
    if (s.on_build_change) parts.push('after llama.cpp upgrades');
    parts.push(alert);
    return parts.join(' · ');
  }
  function baseStatus(b) {
    if (b.running) return 'running';
    if (b.pending) return 'queued';
    return (b.last_check && b.last_check.status) || 'never';
  }
  // Short workload label from a baseline's stored run config.
  function baseConfigSummary(cfg) {
    cfg = cfg || {};
    if (cfg.matrix) {
      const benches = (cfg.matrix.benches || []).map(x => BENCH_LABEL[x] || x).join('/');
      return `matrix ${benches} × osl ${(cfg.matrix.osls || []).join(',')}`;
    }
    const parts = [];
    if (cfg.bench) parts.push(cfg.bench);
    if (cfg.osl != null) parts.push(`osl ${cfg.osl}`);
    if ((cfg.concurrency || []).length) parts.push(`conc ${cfg.concurrency.join(',')}`);
    return parts.join(' · ');
  }
  // Full-config detail row (#882 followups): every key as a chip, baseline_run_id omitted (shown separately).
  function baseDetailsRow(b) {
    const cfg = b.config || {};
    const chips = Object.keys(cfg).filter(k => k !== 'baseline_run_id').map(k => {
      let v = cfg[k];
      if (v && typeof v === 'object') v = JSON.stringify(v);
      return `<span class="bl-bchip">${esc(k)}: ${esc(String(v))}</span>`;
    });
    chips.push(`<span class="bl-bchip">baseline run: ${esc(b.run_id)}</span>`);
    return `<tr class="bl-bdet" data-det="${esc(b.run_id)}"><td colspan="8">${chips.join('')}</td></tr>`;
  }
  function toggleBaseDetails(tr) {
    const rid = tr.dataset.run;
    if (_baseOpenDet.has(rid)) _baseOpenDet.delete(rid); else _baseOpenDet.add(rid);
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('bl-bdet')) next.remove();
    if (_baseOpenDet.has(rid)) {
      const b = ((_base && _base.baselines) || []).find(x => x.run_id === rid);
      if (b) tr.insertAdjacentHTML('afterend', baseDetailsRow(b));
    }
  }
  // A scheduled re-check on this tab's own host looks like a live run — attach to its stream.
  function maybeAttachRecheck(rows) {
    const primaryId = _base && _base.primary_agent_id;
    const active = primaryId ? rows.find(b => b.running && b.active_run_id && b.agent_id === primaryId) : null;
    if (!active) { _baseAutoAttached = null; return; }
    if (_baseAutoAttached === active.active_run_id || running() || _attached) return;
    _baseAutoAttached = active.active_run_id;
    attach(true);
    log(`re-check of baseline ${String(active.model_id || '').split('/').pop()} in progress`);
  }
  function renderBaselines() {
    const card = $('blBaseCard'); if (!card) return;
    const rows = (_base && _base.baselines) || [];
    card.style.display = rows.length ? '' : 'none';
    const sched = (_base && _base.schedule) || {};
    const meta = $('blBaseMeta'); if (meta) meta.textContent = baselineMeta(sched);
    const en = $('blBaseEnabled'); if (en) en.checked = !!sched.enabled;
    const allBtn = $('blBaseAllBtn'); if (allBtn) allBtn.disabled = rows.every(b => b.running || b.pending);
    maybeAttachRecheck(rows);
    const table = $('blBaseTable'); if (!table) return;
    const row = b => {
      const st = baseStatus(b), lc = b.last_check;
      const delta = lc && lc.delta_pct != null ? `${lc.delta_pct < 0 ? '−' : '+'}${Math.abs(Math.round(lc.delta_pct))} %` : '—';
      const dcls = lc && lc.status === 'regressed' ? 'down' : (lc && lc.delta_pct >= 3 ? 'up' : (lc && lc.delta_pct != null ? 'flat' : ''));
      const build = b.llama_build ? esc(b.llama_build) + (b.build_changed ? ' <span class="bl-bnote">changed</span>' : '') : '—';
      const last = lc ? `${esc(fmtTs(lc.ts))} · ${esc(lc.trigger)}${lc.error ? ` · ${esc(lc.error)}` : ''}` : 'never';
      const title = b.loaded ? '' : (b.online ? ' title="model not loaded on the host"' : ' title="host offline"');
      const summary = baseConfigSummary(b.config);
      return `<tr data-run="${esc(b.run_id)}"><td>${esc(String(b.model_id || '').split('/').pop())}${summary ? ` <small>${esc(summary)}</small>` : ''}</td><td${title}>${esc(b.hostname)}${b.loaded ? '' : ' ○'}</td>` +
        `<td class="num">${fmt(b.gen_tps)} t/s <small>${esc(fmtTs(b.ts))}</small></td><td>${build}</td><td>${last}</td>` +
        `<td class="num"><span class="dlt ${dcls}">${delta}</span></td><td><span class="bl-bstat ${esc(st)}">${esc(st)}</span></td>` +
        `<td><button class="mcbtn mcbtn-ghost mcbtn-sm" type="button"${b.running || b.pending ? ' disabled' : ''}>Re-check</button></td></tr>`;
    };
    table.innerHTML = `<table class="bl-rt"><thead><tr><th>Model</th><th>Host</th><th class="num">Baseline</th><th>llama.cpp</th><th>Last check</th><th class="num">Δ decode</th><th>Status</th><th></th></tr></thead><tbody>${rows.map(row).join('')}</tbody></table>`;
    table.querySelectorAll('tr[data-run]').forEach(tr => tr.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      toggleBaseDetails(tr);
    }));
    table.querySelectorAll('tr[data-run] button').forEach(btn => btn.addEventListener('click', () => recheckBaseline(btn.closest('tr').dataset.run)));
    [..._baseOpenDet].forEach(rid => {
      const b = rows.find(x => x.run_id === rid);
      if (!b) { _baseOpenDet.delete(rid); return; }
      const tr = table.querySelector(`tr[data-run="${esc(rid)}"]`);
      if (tr) tr.insertAdjacentHTML('afterend', baseDetailsRow(b));
    });
    const busy = rows.some(b => b.running || b.pending);
    clearTimeout(_baseTimer); _baseTimer = busy ? setTimeout(loadBaselines, 5000) : null;
  }
  async function loadBaselines() {
    try { const r = await fetch('/api/benchmark/live/baselines').then(r => r.json()); if (r && r.ok) _base = r; } catch (_) {}
    renderBaselines();
  }
  async function recheckBaseline(runId) {
    try { await fetch('/api/benchmark/live/baselines/recheck', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(runId ? { run_id: runId } : {}) }); } catch (_) {}
    await loadBaselines();
  }
  async function toggleBaseSchedule() {
    const cb = $('blBaseEnabled'); if (!cb) return;
    const val = cb.checked;
    let r, d;
    try {
      r = await fetch('/api/admin/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ changes: { 'manager.bench_baselines.enabled': val } }) });
      d = await r.json().catch(() => null);
    } catch (e) { r = null; d = null; }
    if (!r || !r.ok || !d || !d.ok) {
      cb.checked = !val;
      const m = $('blBaseMeta');
      if (m) m.textContent = (d && (d.error || Object.values(d.errors || {})[0])) || 'failed to update schedule';
      return;
    }
    await loadBaselines();
  }
  function openBaseSettings() {
    if (typeof switchTab === 'function') switchTab('admin');
    if (typeof switchSubTab === 'function') switchSubTab('admin', 'settings');
    if (typeof adminSettingsOpenGroup === 'function') adminSettingsOpenGroup('benchmark');
  }
  function exportJson() {
    if (_fleetJob && _fleetJob.done && !_fleetSel) {
      const id = String(_fleetJob.job_id || 'job').replace(/[^A-Za-z0-9_.-]/g, '_');
      const blob = new Blob([JSON.stringify(_fleetJob, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `bench-fleet-${id}.json`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
      return;
    }
    const doc = _lastDoc; if (!doc) return;
    const id = String(doc.run_id || _runId || 'run').replace(/[^A-Za-z0-9_.-]/g, '_');
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `bench-live-${id}.json`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
  }
  window.BL = { onOpen, setMode, run, cancel, setup, startServer, running, applyPreset, parseSweep, parseOsls, estimateSeconds, deltaText, knee, pinBaseline, exportJson,
    toggleMatrix, heatCells, cellKey, addToReportCard, toggleFleet, rankHosts, selectFleetHost, recheckBaseline, baselineMeta, loadBaselines,
    toggleBaseSchedule, openBaseSettings, fmtTs,
    _config: config, _debugLevels: (rows) => { _levels = rows; _cell = null; redraw(); },
    _debugFleet: (job) => { _fleetJob = job; renderFleet(); }, _debugPollOnce: fleetTick,
    _debugBaselines: (d) => { _base = d; renderBaselines(); },
    _debug: () => ({ baseTimer: _baseTimer, attached: _attached, baseAutoAttached: _baseAutoAttached }) };
})();
