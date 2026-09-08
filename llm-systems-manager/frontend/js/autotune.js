// Autotune module (#880): objective + seven dimensions, staged search, recommendation.
// Classic script; exposes window.AT. Streams /api/llm/autotune/stream via SG.
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => (window.TC && TC.esc ? TC.esc(String(s ?? '')) : String(s ?? ''));
  const OBJ_HINT = {
    fit: '<b>Fit</b> is today’s behaviour: the largest context that leaves the target VRAM free. Other dimensions only run if you turn them on.',
    speed: '<b>Speed</b> spends VRAM on single-request throughput: f16 KV where it fits, one slot, the fastest threads and speculative setup.',
    balanced: '<b>Balanced</b> keeps at least the minimum context per slot and takes any change that is speed-neutral or better.',
    serve: '<b>Serve</b> maximises aggregate tokens/s across parallel slots, accepting lower per-request speed.',
  };
  const STAGE_NAME = { context: 'Context size', kv: 'KV cache type', moe: 'MoE CPU offload', threads: 'CPU threads',
                       spec: 'Speculative decoding', slots: 'Parallel slots', sampling: 'Sampling defaults', verify: 'Verify' };
  const STAGE_FLAG = { context: '-c', kv: '-ctk -ctv', moe: '--n-cpu-moe', threads: '-t -tb', spec: '--spec-type',
                       slots: '-np', sampling: 'sidecar', verify: '' };
  const STAGE_SHORT = { context: 'Context', kv: 'KV cache', moe: 'MoE offload', threads: 'Threads', spec: 'Speculative',
                        slots: 'Slots', sampling: 'Sampling', verify: 'Verify' };
  const ORDER = ['context', 'kv', 'moe', 'threads', 'spec', 'slots', 'sampling', 'verify'];
  const MEASURED = ['threads', 'spec', 'slots'];
  const SKIP_TEXT = { off: 'off', runtime: 'no bench runtime', budget: 'out of budget', fits: 'already fits', not_moe: 'dense model',
                      manager: 'from metadata', kv_unified: 'kv-unified', no_candidates: 'nothing to try' };
  let _models = [], _pre = null, _sel = new Set(), _runs = [], _facts = {}, _es = null, _attached = false;
  let _run = null, _done = {}, _doneModel = null, _meta = {}, _section = {}, _elapsedIv = null;

  // ── rail state ──
  function dimOn(d) { const t = document.querySelector(`#toolsModAt .at-dim[data-dim="${d}"] [data-dim-on]`); return !!(t && t.classList.contains('on')); }
  function chips(id) { return [...document.querySelectorAll(`#${id} .bl-chip.on`)].map(c => c.dataset.v); }
  function num(id, def) { const el = $(id); const v = el ? parseFloat(el.value) : NaN; return Number.isFinite(v) ? v : def; }
  function moeVisible() { const el = document.querySelector('.at-dim[data-dim="moe"]'); return !el || el.style.display !== 'none'; }
  function dimsState() {
    return {
      context: { on: true, target_mb: num('atTargetMb', 1024), tolerance_mb: num('atToleranceMb', 50),
                 custom_args: (($('atCustomArgs') || {}).value || '').trim().split(/\s+/).filter(Boolean) },
      kv: { on: dimOn('kv'), candidates: chips('atKvChips'), guard_kl_max: num('atKvKl', 0.02) },
      moe: { on: dimOn('moe') && moeVisible(), min: num('atMoeMin', 0), max: num('atMoeMax', 16) },
      threads: { on: dimOn('threads'), candidates: chips('atThreadChips').map(Number) },
      slots: { on: dimOn('slots'), candidates: chips('atSlotChips').map(Number), min_ctx_per_slot: num('atMinCtxSlot', 32768) },
      spec: { on: dimOn('spec'), types: chips('atSpecChips'), draft_model: ($('atDraftSel') || {}).value || 'auto',
              n_min: num('atSpecNmin', 0), n_max: num('atSpecNmax', 16), p_min: num('atSpecPmin', 0.75) },
      sampling: { on: dimOn('sampling'), overwrite: !!($('atSamplingOverwrite') && $('atSamplingOverwrite').classList.contains('on')) },
    };
  }
  function objective() { const b = document.querySelector('#atObjSeg button.on'); return (b && b.dataset.obj) || 'balanced'; }
  function setObjective(o) {
    document.querySelectorAll('#atObjSeg button').forEach(b => b.classList.toggle('on', b.dataset.obj === o));
    const h = $('atObjHint'); if (h) h.innerHTML = OBJ_HINT[o] || '';
    const L = typeof layout !== 'undefined' ? layout : null;
    if (L) { L.atObjective = o; try { saveLayout(); } catch (_) {} }
    refreshPlan();
  }
  function selected() { return [...document.querySelectorAll('#atModelList .mc-toggle.on')].map(b => b.dataset.model); }
  function primaryModel() { return selected()[0] || null; }
  function factsFor(mid) {
    if (_facts[mid]) return _facts[mid];
    const r = _runs.find(x => x.tool === 'autotune' && x.model_id === mid && x.summary && x.summary.n_expert != null);
    return r ? { n_expert: r.summary.n_expert } : {};
  }
  function isMoe(mid) { const f = factsFor(mid); return f.n_expert == null ? null : f.n_expert > 1; }
  function noFit(mid) {
    if (!_pre || !_pre.sizes || _pre.sizes[mid] == null) return false;
    const total = (_pre.vram_total_mb || 0) + (_pre.ram_total_mb || 0);
    return total > 0 && _pre.sizes[mid] / 1048576 > total;
  }
  function renderModels(preselect) {
    const host = $('atModelList'); if (!host) return;
    const keep = new Set(_sel);
    host.innerHTML = '';
    if (!_models.length) { host.innerHTML = '<div class="at-hint">No models configured.</div>'; return; }
    _sel = new Set();
    _models.forEach(m => {
      const on = m === preselect || keep.has(m);
      const lab = document.createElement('label'); lab.className = 'at-check';
      lab.innerHTML = `<button type="button" class="mc-toggle${on ? ' on' : ''}" data-model="${esc(m)}"><span class="track"></span></button><b>${esc(m)}</b>`;
      const moe = isMoe(m);
      if (moe) { const t = document.createElement('span'); t.className = 'at-tag moe'; t.textContent = `MoE · ${factsFor(m).n_expert} experts`; lab.appendChild(t); }
      if (noFit(m)) { const t = document.createElement('span'); t.className = 'at-tag nofit'; t.textContent = 'no fit'; lab.appendChild(t); }
      host.appendChild(lab);
      if (on) _sel.add(m);
    });
    syncModels();
  }
  function syncModels() {
    const n = selected().length;
    const c = $('atModelsCount'); if (c) c.textContent = `${n} selected`;
    const moeRow = document.querySelector('.at-dim[data-dim="moe"]');
    if (moeRow) { const p = primaryModel(); moeRow.style.display = (p && isMoe(p) === false) ? 'none' : ''; }
    const prev = $('atPrevTune');
    if (prev) {
      const p = primaryModel();
      const r = p && _runs.find(x => x.tool === 'autotune' && x.model_id === p && x.ok);
      if (r) {
        const s = r.summary || {};
        prev.style.display = '';
        prev.innerHTML = `<b>Previous tune</b><span class="d">${esc((r.ts || '').slice(0, 10))} · ${esc(s.objective || 'fit')}${s.ctx_size != null ? ' · ctx ' + esc(Number(s.ctx_size).toLocaleString()) : ''}${s.gain_pct != null ? ' · ' + (s.gain_pct >= 0 ? '+' : '') + esc(Math.round(s.gain_pct)) + ' %' : ''}. The new run compares against it.</span>`;
      } else prev.style.display = 'none';
    }
    refreshPlan();
  }
  function seedThreads(cores) {
    const host = $('atThreadChips'); if (!host || host.childElementCount) return;
    const p = Math.max(1, (cores && cores.physical) || 4), l = Math.max(p, (cores && cores.logical) || p);
    const on = [...new Set([Math.round(p / 2), Math.round(p * 3 / 4), p])].filter(n => n >= 1);
    const all = [...new Set([...on, l])].sort((a, b) => a - b);
    host.innerHTML = all.map(n => `<span class="bl-chip${on.includes(n) ? ' on' : ''}" data-v="${n}">${n}</span>`).join('');
    const hint = $('atThreadHint'); if (hint) hint.textContent = `Physical cores on this host: ${p} (${l} logical). Batch threads are set separately when MoE offload is on.`;
  }
  function seedDrafts(drafts) {
    const sel = $('atDraftSel'); if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="auto">auto</option><option value="none">none</option>' +
      (drafts || []).map(d => `<option value="${esc(d.path)}">${esc(d.file)} · ${esc(d.repo)}</option>`).join('');
    sel.value = [...sel.options].some(o => o.value === cur) ? cur : 'auto';
  }
  function dimSummaries() {
    const d = dimsState();
    const tv = $('atTargetMbVal'); if (tv) tv.textContent = `${d.context.target_mb} MB`;
    const tol = $('atToleranceMbVal'); if (tol) tol.textContent = `${d.context.tolerance_mb} MB`;
    const s = {
      context: `free ${d.context.target_mb} ± ${d.context.tolerance_mb} MB`,
      kv: `${d.kv.candidates.join(' → ') || '—'} · KL ≤ ${d.kv.guard_kl_max}`,
      moe: `--n-cpu-moe ${d.moe.min} … ${d.moe.max}`,
      threads: `-t ${d.threads.candidates.join(' · ') || '—'}`,
      slots: `-np ${d.slots.candidates.join(' · ') || '—'} · ≥ ${d.slots.min_ctx_per_slot.toLocaleString()} ctx`,
      spec: `${d.spec.types.join(' · ') || '—'} · window ${d.spec.n_min}–${d.spec.n_max}`,
      sampling: d.sampling.overwrite ? 'overwrite' : 'fill blanks',
    };
    document.querySelectorAll('#toolsModAt .at-dim').forEach(row => {
      const k = row.dataset.dim, el = row.querySelector('[data-dim-sum]');
      if (el) el.textContent = s[k] || '';
      row.classList.toggle('off', !dimOn(k));
    });
  }

  // ── plan ──
  const EST = { context: 360, kv: 180, moe: 420, threads: 60, spec: 120, slots: 60, sampling: 5, verify: 120 };
  function planRows(obj, dims, pre, facts) {
    const rt = !!(pre && pre.runtime && pre.runtime.ok);
    const moe = facts && facts.n_expert != null ? facts.n_expert > 1 : null;
    const rows = [];
    ORDER.forEach(stage => {
      const d = dims[stage] || {};
      let on = stage === 'context' || stage === 'verify' || !!d.on;
      let desc = '', n = 1;
      if (stage === 'context') desc = `converge -fitt to ${dims.context.target_mb} ± ${dims.context.tolerance_mb} MB free · up to 10 loads`;
      else if (stage === 'kv') { n = Math.max(1, (d.candidates || []).length); desc = `${(d.candidates || []).join(' · ')} — re-fit each, KL guard ≤ ${d.guard_kl_max}`; }
      else if (stage === 'moe') { if (moe === false) on = false; desc = moe === null ? `bisect ${d.min} … ${d.max} if the model is MoE` : `bisect ${d.min} … ${d.max} expert layers on CPU, keep the fewest that fit`; }
      else if (stage === 'threads') { n = Math.max(1, (d.candidates || []).length); desc = `${(d.candidates || []).join(' · ')} — decode + batch threads`; }
      else if (stage === 'spec') { n = 3; desc = `${(d.types || []).join(' · ')} · draft ${d.draft_model === 'auto' ? 'auto-discovered' : d.draft_model === 'none' ? 'none' : 'from disk'} · window ${d.n_min}–${d.n_max}`; }
      else if (stage === 'slots') { n = Math.max(1, (d.candidates || []).length); desc = `${(d.candidates || []).join(' · ')} with ≥ ${Number(d.min_ctx_per_slot).toLocaleString()} ctx each — live concurrency sweep`; }
      else if (stage === 'sampling') desc = 'generation_config.json → model card → base model · no load needed';
      else desc = `load the recommended set once, confirm fit + ${rt ? '60 s of chat traffic' : 'free VRAM'}`;
      if (MEASURED.includes(stage) && on && !rt) desc = 'skipped: install the bench runtime (Benchmark · Live) to measure this';
      const est_s = on ? (MEASURED.includes(stage) && !rt ? 0 : EST[stage] * n) : 0;
      rows.push({ stage, name: STAGE_NAME[stage], flag: STAGE_FLAG[stage], desc, on, est_s });
    });
    if (obj === 'fit') rows.forEach(r => { if (r.stage === 'sampling') r.desc += ' · reported, applied only if selected'; });
    return rows;
  }
  function durText(s) { return s < 90 ? `~${Math.max(1, Math.round(s))} s` : `~${Math.round(s / 60)} min`; }
  function estimateText(rows) { return durText(rows.reduce((a, r) => a + (r.on ? r.est_s : 0), 0)); }
  function refreshPlan() {
    dimSummaries();
    const rows = planRows(objective(), dimsState(), _pre, factsFor(primaryModel() || ''));
    const host = $('atPlanRows'); if (!host) return;
    host.innerHTML = rows.map((r, i) => `<div class="at-plan-r${r.on ? '' : ' off'}"><span class="i">${i + 1}</span><div class="n"><b>${esc(r.name)}</b><span>${esc(r.desc)}</span></div><span class="c">${esc(r.flag)}</span><span class="t">${r.on ? esc(durText(r.est_s)) : 'off'}</span></div>`).join('');
    const onCount = rows.filter(r => r.on).length;
    const meta = $('atPlanMeta');
    if (meta) {
      meta.textContent = '';
      meta.append(document.createTextNode(`${primaryModel() || 'no model'} · ${objective()} · `));
      const b = document.createElement('b'); b.textContent = `${onCount} stages`; meta.append(b);
      meta.append(document.createTextNode(` · ${estimateText(rows)}`));
    }
    const dc = $('atDimsCount'); if (dc) dc.textContent = `${rows.filter(r => r.on && !['context', 'verify'].includes(r.stage)).length + 1} on · ${estimateText(rows)}`;
    const est = $('atEstimate'); if (est) est.textContent = estimateText(rows);
  }

  // ── preflight / open ──
  async function serverUp() {
    try { const s = await fetch('/api/llama-state').then(r => r.json()); return s.state === 'awake' || s.state === 'sleeping'; } catch (_) { return false; }
  }
  // Re-reads preflight unless the caller passes a fresh doc; a cached unit_active would
  // keep the banner up after a stop.
  async function checkServer(pre) {
    const banner = $('atPreflight'), msg = $('atPreflightMsg'), btn = $('atStopBtn'), run = $('atRunBtn');
    if (!banner) return;
    const up = await serverUp();
    let active = false, helpBad = false;
    try {
      const p = pre || await fetch('/api/llm/autotune/preflight').then(r => r.json());
      if (p && p.ok) { _pre = p; active = !!p.unit_active; helpBad = !!(p.help_valued && p.help_valued.ok === false); }
    } catch (_) {}
    if (up || active) {
      banner.style.display = '';
      if (msg) msg.innerHTML = '<b>llama-server is running.</b> Autotune needs the port and the VRAM; stop it first.';
      if (btn) btn.style.display = '';
      if (run) run.disabled = true;
    } else if (helpBad) {
      banner.style.display = '';
      if (msg) msg.textContent = 'Could not read llama-server --help — on/off flags will be guessed; loads may fail.';
      if (btn) btn.style.display = 'none';
      if (run && !running()) run.disabled = false;
    } else {
      banner.style.display = 'none';
      if (btn) btn.style.display = 'none';
      if (run && !running()) run.disabled = false;
    }
  }
  async function stopServer() {
    try { await fetch('/api/llm/server/stop', { method: 'POST' }); } catch (_) {}
    for (let i = 0; i < 15; i++) {
      if (!(await serverUp())) break;
      await new Promise(r => setTimeout(r, 1000));
    }
    await checkServer();
  }
  let _wired = false;
  function wire() {
    if (_wired) return; _wired = true;
    const mod = $('toolsModAt'); if (!mod) return;
    mod.addEventListener('click', ev => {
      const railEl = ev.target.closest('.at-rail');
      if (railEl && railEl.classList.contains('locked') && !ev.target.closest('.at-runbar')) return;
      const rowTog = ev.target.closest('.at-rt .mc-toggle');
      if (rowTog) { if (window.AT && AT.toggleRow) AT.toggleRow(parseInt(rowTog.dataset.row, 10)); return; }
      const tog = ev.target.closest('.mc-toggle');
      if (tog && mod.contains(tog)) {
        if (tog.disabled) return;
        tog.classList.toggle('on'); ev.stopPropagation();
        if (tog.dataset.model != null) {
          if (tog.classList.contains('on')) _sel.add(tog.dataset.model); else _sel.delete(tog.dataset.model);
          syncModels();
          return;
        }
        refreshPlan();
        if (_rows.length && tog.id === 'atRestartAfter') renderRows();
        return;
      }
      const chip = ev.target.closest('.bl-chip');
      if (chip && chip.closest('.at-dim-b')) { chip.classList.toggle('on'); refreshPlan(); return; }
      const head = ev.target.closest('.at-dim-h');
      if (head) { head.parentElement.classList.toggle('closed'); return; }
      const obj = ev.target.closest('#atObjSeg button');
      if (obj) setObjective(obj.dataset.obj);
    });
    mod.addEventListener('input', ev => { if (ev.target.closest('.at-dim-b, .at-grp-b')) refreshPlan(); });
    mod.addEventListener('change', ev => { if (ev.target.closest('.at-dim-b, .at-grp-b')) refreshPlan(); });
  }
  async function onOpen(preselect) {
    wire();
    const L = typeof layout !== 'undefined' ? layout : null;
    if (L && L.atObjective) setObjective(L.atObjective); else setObjective(objective());
    if (running()) return;
    const [models, pre, runs] = await Promise.all([
      fetch('/api/benchmark/models').then(r => r.json()).catch(() => ({})),
      fetch('/api/llm/autotune/preflight').then(r => r.json()).catch(() => null),
      fetch('/api/tools/runs?limit=100').then(r => r.json()).catch(() => ({})),
    ]);
    _models = (models && models.models) || [];
    _pre = pre && pre.ok ? pre : _pre;
    _runs = (runs && runs.runs) || [];
    if (_pre) { seedThreads(_pre.cores); seedDrafts(_pre.drafts); }
    renderModels(preselect);
    await checkServer(pre && pre.ok ? pre : null);
    if (_pre && _pre.busy && !running()) attach();
  }

  // ── run / stream ──
  function running() { return !!_es || _attached; }
  function setPane(name) {
    ['Plan', 'Run', 'Done'].forEach(p => { const el = $('atPane' + p); if (el) el.style.display = p === name ? '' : 'none'; });
    const note = $('atModeNote');
    if (note) note.textContent = name === 'Plan' ? 'Plan · nothing has run yet' : name === 'Run' ? 'Running' : 'Recommendation ready · nothing applied yet';
  }
  function setRailLocked(locked) {
    const rail = document.querySelector('#toolsModAt .at-rail'); if (!rail) return;
    rail.classList.toggle('locked', !!locked);
    rail.querySelectorAll('.at-grp input, .at-grp select, .at-grp button.mc-toggle, #atPreflight input, #atPreflight select, #atPreflight button.mc-toggle')
      .forEach(el => { el.disabled = !!locked; });
  }
  function busy(on) {
    const run = $('atRunBtn'), cancel = $('atCancelBtn'), again = $('atAgainBtn');
    if (run) run.disabled = on;
    if (cancel) cancel.style.display = on && !_attached ? '' : 'none';
    if (again) again.style.display = on ? 'none' : (_doneModel ? '' : 'none');
    setRailLocked(on);
    if (typeof toolsSyncRunDot === 'function') toolsSyncRunDot();
  }
  function mmss(s) { s = Math.max(0, Math.floor(s || 0)); return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; }
  function tick() {
    if (!_run) return;
    const el = $('atStripTime'); if (!el) return;
    const e = (Date.now() - _run.startTs) / 1000;
    const bar = $('atProgBar');
    if (_run.estTotal && e > _run.estTotal) {
      el.innerHTML = `elapsed <b>${esc(mmss(e))}</b> · past estimate`;
      if (bar) bar.style.width = '97%';
    } else {
      const left = Math.max(0, _run.estTotal - e);
      el.innerHTML = `elapsed <b>${esc(mmss(e))}</b>${_run.estTotal ? ` · ~${esc(durText(left).replace('~', ''))} left` : ''}`;
      if (bar && _run.estTotal) bar.style.width = Math.min(97, Math.round(100 * e / _run.estTotal)) + '%';
    }
  }
  function startElapsed() {
    stopElapsed();
    tick();
    _elapsedIv = setInterval(tick, 1000);
  }
  function stopElapsed() { if (_elapsedIv) { clearInterval(_elapsedIv); _elapsedIv = null; } }
  // Re-derives the remaining estimate from live progress: elapsed + this stage's
  // live est_s + the static plan estimate for stages still ahead in the order.
  function recomputeEstTotal(fromStage, curEstS) {
    if (!_run || !fromStage) return;
    const idx = _run.order.indexOf(fromStage);
    if (idx < 0) return;
    const elapsedNow = (Date.now() - _run.startTs) / 1000;
    let rest = 0;
    for (let i = idx + 1; i < _run.order.length; i++) {
      const s = _run.order[i];
      if (_run.stages[s] && _run.stages[s].status === 'skipped') continue;
      rest += (_run.staticEst && _run.staticEst[s]) || 0;
    }
    _run.estTotal = elapsedNow + (curEstS || 0) + rest;
  }
  function log(text, cls) {
    const el = $('atLog'); if (!el) return;
    if (el.childElementCount >= 5000) el.removeChild(el.firstChild);
    const t = new Date().toTimeString().slice(0, 8);
    const div = document.createElement('div');
    div.innerHTML = `<span class="dim">${t}</span> ${cls ? `<span class="${cls}">` : ''}${esc(text)}${cls ? '</span>' : ''}`;
    el.appendChild(div); el.scrollTop = el.scrollHeight;
  }
  function raw(text) {
    const el = $('atRawLog'); if (!el) return;
    if (el.childElementCount >= 10000) el.removeChild(el.firstChild);
    const d = document.createElement('div'); d.textContent = text; el.appendChild(d);
    const c = $('atRawCount'); if (c) c.textContent = String(el.childElementCount);
  }
  function newRun(stages) {
    const rows = planRows(objective(), dimsState(), _pre, factsFor(primaryModel() || ''));
    const staticEst = {}; rows.forEach(r => { staticEst[r.stage] = r.est_s; });
    _run = { order: stages || ORDER, stages: {}, cands: {}, current: null, startTs: Date.now(), staticEst,
             estTotal: rows.reduce((a, r) => a + (r.on ? r.est_s : 0), 0), iters: [], loads: 0 };
    _run.order.forEach(s => { _run.stages[s] = { status: 'pending', text: '—' }; });
    const lg = $('atLog'); if (lg) lg.innerHTML = '';
    const rw = $('atRawLog'); if (rw) rw.innerHTML = '';
    const rc = $('atRawCount'); if (rc) rc.textContent = '0';
    renderStepper();
    renderStage();
    startElapsed();
  }
  const DIM_FALLBACK = { kv: ['f16', 'q8_0', 'q4_0'], slots: [1, 2, 4, 8], spec: ['auto'] };
  function dimsProblem(d) {
    if (d.kv.on && !d.kv.candidates.length) return 'Pick at least one KV cache type, or switch the KV cache type dimension off.';
    if (d.threads.on && !d.threads.candidates.length) return 'Pick at least one thread count, or switch the CPU threads dimension off.';
    if (d.slots.on && !d.slots.candidates.length) return 'Pick at least one slot count, or switch the Parallel slots dimension off.';
    if (d.spec.on && !d.spec.types.length) return 'Pick at least one speculative decoding type, or switch that dimension off.';
    if (d.moe.on && d.moe.min > d.moe.max) return 'MoE CPU offload: the range minimum must not be above the maximum.';
    if (d.spec.on && d.spec.n_min >= d.spec.n_max) return 'Speculative decoding: the draft window minimum must be below the maximum.';
    return null;
  }
  // The agent validates every candidates key it is sent, empty or not, so an off
  // dimension must carry a usable list or omit the key entirely.
  function fillDimDefaults(d) {
    if (!d.kv.candidates.length) d.kv.candidates = DIM_FALLBACK.kv.slice();
    if (!d.slots.candidates.length) d.slots.candidates = DIM_FALLBACK.slots.slice();
    if (!d.spec.types.length) d.spec.types = DIM_FALLBACK.spec.slice();
    if (!d.threads.candidates.length) delete d.threads.candidates;
    if (d.moe.min > d.moe.max) d.moe.max = d.moe.min;
    if (d.spec.n_min >= d.spec.n_max) d.spec.n_max = d.spec.n_min + 1;
  }
  async function run() {
    const ids = selected();
    if (!ids.length) { alert('Select at least one model.'); return; }
    const dims = dimsState();
    if (!Number.isFinite(dims.context.target_mb) || dims.context.target_mb < 0) { alert('Target free VRAM must be a non-negative number.'); return; }
    const problem = dimsProblem(dims);
    if (problem) { alert(problem); return; }
    fillDimDefaults(dims);
    const body = { model_ids: ids, objective: objective(), budget_min: Math.round(num('atBudgetMin', 120)), dims };
    let r;
    try {
      const resp = await fetch('/api/llm/autotune/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      r = await resp.json();
    } catch (e) { alert('Autotune request failed: ' + (e && e.message ? e.message : e)); return; }
    if (!r || !r.ok) {
      if (r && /in progress/i.test(r.error || r.detail || '')) { attach(); return; }
      alert((r && (r.error || r.detail)) || 'Failed to start autotune'); return;
    }
    _done = {}; _doneModel = null; _meta = {}; _section = {};
    fetchMeta(ids);
    newRun(null);
    setPane('Run');
    busy(true);
    openStream();
  }
  function attach() {
    _attached = true;
    newRun(null);
    setPane('Run');
    busy(true);
    log('attached to a run already in progress on this host', 'dim');
    openStream();
  }
  function fetchMeta(ids) {
    ids.forEach(mid => {
      fetch('/api/llm/model-meta?model_id=' + encodeURIComponent(mid)).then(r => r.json()).then(d => { _meta[mid] = d; }).catch(() => {});
    });
    fetch('/api/llm/config').then(r => r.json()).then(cfg => { ids.forEach(mid => { _section[mid] = (cfg && cfg[mid]) || {}; }); }).catch(() => {});
  }
  function openStream() {
    if (_es) { try { _es.close(); } catch (_) {} }
    _es = SG.open({
      url: '/api/llm/autotune/stream', maxDrops: 20,
      onReconnecting: () => { const p = $('atRunPill'); if (p) p.textContent = 'reconnecting…'; },
      onRestored: () => { const p = $('atRunPill'); if (p) p.textContent = 'running'; },
      onLost: (rs) => { log(`stream lost (readyState=${rs})`, 'crit'); _es = null; _attached = false; stopElapsed(); busy(false); },
      onEvent: (msg) => onEvent(msg),
    });
    if (typeof toolsSyncRunDot === 'function') toolsSyncRunDot();
  }
  function cancel() {
    fetch('/api/llm/autotune/cancel', { method: 'POST' }).catch(() => {});
    log('cancel requested', 'warn');
  }
  function again() { _done = {}; _doneModel = null; _rows = []; setMsg(''); setPane('Plan'); busy(false); refreshPlan(); }
  function finish(msg) {
    if (_es) { try { _es.close(); } catch (_) {} _es = null; }
    _attached = false;
    stopElapsed();
    const p = $('atRunPill'); if (p) { p.textContent = msg.cancelled ? 'cancelled' : (msg.ok ? 'done' : 'error'); p.classList.remove('running'); }
    if (msg.error) log(msg.error, 'crit');
    busy(false);
    if (_doneModel && _done[_doneModel]) { renderDone(_done[_doneModel]); setPane('Done'); }
    else if (_run) { const ab = $('atAgainBtn'); if (ab) ab.style.display = ''; }
  }

  // ── stepper + stage cards ──
  function stageIndex(stage) { return _run ? _run.order.indexOf(stage) : -1; }
  function renderStepper() {
    const host = $('atStepper'); if (!host || !_run) return;
    host.innerHTML = _run.order.map(s => {
      const st = _run.stages[s] || { status: 'pending', text: '—' };
      return `<div class="at-step ${esc(st.status)}" data-stage="${esc(s)}"><div class="k"><span class="at-dot"></span>${esc(STAGE_SHORT[s] || s)}</div><div class="v">${esc(st.text)}</div></div>`;
    }).join('');
  }
  function setStrip() {
    if (!_run) return;
    const i = stageIndex(_run.current), n = _run.order.length;
    const c = (_run.cands[_run.current] || []);
    const k = c.filter(x => x.status !== 'pending').length;
    const el = $('atStripStage');
    if (el) el.innerHTML = _run.current ? `stage <b>${i + 1} / ${n}</b> · ${esc(STAGE_NAME[_run.current])}${c.length ? ` · candidate ${k} / ${c.length}` : ''}` : '';
    const pill = $('atStagePill'); if (pill) pill.textContent = c.length ? `candidate ${k} / ${c.length}` : '';
  }
  function gaugeHtml(free, target, tol) {
    if (![free, target, tol].every(Number.isFinite) || tol <= 0) return '';
    const lo = target - 3 * tol, hi = target + 3 * tol, span = hi - lo;
    if (span <= 0) return '';
    const clamp = p => Math.max(0, Math.min(100, p));
    const bandL = clamp((target - tol - lo) / span * 100), bandW = clamp((2 * tol) / span * 100), mark = clamp((free - lo) / span * 100);
    const hit = Math.abs(free - target) <= tol;
    return `<div class="at-gauge"><div class="at-gauge-lbls"><span>${lo} MB</span><span>target ${target} ±${tol}</span><span>${hi} MB</span></div><div class="at-gauge-track"><div class="at-gauge-band" style="left:${bandL}%;width:${bandW}%;"></div><div class="at-gauge-mark${hit ? '' : ' miss'}" style="left:calc(${mark}% - 1px);"></div></div></div>`;
  }
  function fmt(n, d = 1) { return n == null ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }); }
  function barsHtml(cands, metric) {
    const vals = cands.map(c => c[metric]).filter(v => typeof v === 'number' && v > 0);
    const max = Math.max(1, ...vals);
    const best = vals.length ? Math.max(...vals) : null;
    return `<div class="at-bars">${cands.map(c => {
      const v = c[metric];
      const cls = c.status === 'pending' ? 'pend' : (c.ok === false ? 'fail' : (v === best ? 'best' : ''));
      const h = c.status === 'pending' ? 40 : (typeof v === 'number' && v > 0 ? Math.max(4, Math.round(100 * v / max)) : 6);
      const top = c.status === 'pending' ? '…' : (c.ok === false ? (c.error && /oom/i.test(c.error) ? 'OOM' : 'failed') : fmt(v, 1));
      return `<div class="at-bar ${cls}"><span class="t">${esc(top)}</span><i style="height:${h}%"></i><span class="l">${esc(c.value)}${v === best && cls === 'best' ? ' ★' : ''}</span></div>`;
    }).join('')}</div>`;
  }
  function renderStage() {
    if (!_run) return;
    const s = _run.current;
    const title = $('atStageTitle'), meta = $('atStageMeta'), body = $('atStageBody');
    if (!s) { if (title) title.textContent = 'Starting'; if (meta) meta.textContent = ''; if (body) body.innerHTML = '<div class="at-hint">Waiting for the first stage…</div>'; return; }
    const cands = _run.cands[s] || [];
    if (title) title.textContent = STAGE_NAME[s];
    if (s === 'context') {
      if (meta) meta.textContent = 'free VRAM after each -fitt load · target band';
      const last = _run.iters[_run.iters.length - 1];
      const d = dimsState().context;
      if (body) body.innerHTML = (last ? gaugeHtml(Number(last.actual_free_mb), d.target_mb, d.tolerance_mb) : '') +
        `<div class="at-hint" style="margin-top:8px">${_run.iters.map(it => `iter ${it.iter} · -fitt ${it.fitt} → free ${it.actual_free_mb} MB · ctx ${Number(it.n_ctx_seq || 0).toLocaleString()}`).map(esc).join('<br>') || 'loading the current config first…'}</div>`;
    } else if (s === 'kv') {
      if (meta) meta.textContent = 'ctx after re-fit · KL vs f16 · guard';
      if (body) body.innerHTML = `<table class="at-kvt"><thead><tr><th>type</th><th>ctx</th><th>KL</th><th>guard</th></tr></thead><tbody>${cands.map(c => `<tr><td>${esc(c.value)}</td><td>${c.ctx != null ? esc(Number(c.ctx).toLocaleString()) : (c.status === 'pending' ? '…' : '—')}</td><td>${c.kl != null ? esc(c.kl) : '—'}</td><td>${c.guard_pass === true ? '<span class="ok">pass</span>' : c.guard_pass === false ? '<span class="warn">fail</span>' : (c.ok === false ? '<span class="crit">' + esc(c.error || 'failed') + '</span>' : '—')}</td></tr>`).join('')}</tbody></table>`;
    } else if (s === 'sampling') {
      if (meta) meta.textContent = 'author-recommended values';
      if (body) body.innerHTML = '<div class="at-hint">from metadata · no load</div>';
    } else if (s === 'verify') {
      if (meta) meta.textContent = 'recommended set · traffic at the chosen concurrency';
      const c = cands[cands.length - 1] || {};
      if (body) body.innerHTML = `<div class="bl-tiles"><div class="bl-tile"><div class="v">${esc(fmt(c.decode_tps))}<em>t/s</em></div><div class="l">decode</div></div><div class="bl-tile"><div class="v">${esc(fmt(c.agg_tps))}<em>t/s</em></div><div class="l">aggregate</div></div><div class="bl-tile"><div class="v">${esc(fmt(c.free_mb, 0))}<em>MB</em></div><div class="l">free VRAM</div></div><div class="bl-tile"><div class="v">${c.status === 'pending' ? '…' : (c.ok ? 'pass' : 'fail')}</div><div class="l">attempt ${esc(c.value)}</div></div></div>`;
    } else {
      const metric = s === 'slots' && objective() === 'serve' ? 'agg_tps' : 'decode_tps';
      if (meta) meta.innerHTML = `${metric === 'agg_tps' ? 'aggregate' : 'decode'} t/s per <b>${esc(STAGE_FLAG[s])}</b> · chat preset`;
      if (body) body.innerHTML = barsHtml(cands, metric) + `<div class="at-hint" style="margin-top:6px">${esc(cands.filter(c => c.ok === false).map(c => `${c.value}: ${c.error || 'failed'}`).join(' · '))}</div>`;
    }
    setStrip();
  }
  function candRow(stage, value) {
    const list = _run.cands[stage] || (_run.cands[stage] = []);
    let c = list.find(x => String(x.value) === String(value));
    if (!c) { c = { value, status: 'pending' }; list.push(c); }
    return c;
  }
  function onEvent(msg) {
    if (!_run) newRun(null);
    const t = msg.type;
    if (t === 'model_start') {
      if (Array.isArray(msg.stages) && msg.stages.length) _run.order = msg.stages;
      _run.stages = {}; _run.order.forEach(s => { _run.stages[s] = { status: 'pending', text: '—' }; });
      _run.cands = {}; _run.iters = []; _run.current = null; _run.curEstS = 0;
      renderStepper(); renderStage(); log(`── ${msg.model_id} · ${msg.objective} ──`, 'acc');
    } else if (t === 'facts') {
      _facts[msg.model_id] = msg;
      log(`model: ${msg.arch || '?'} · ${msg.n_layer || '?'} layers · ${msg.n_expert > 1 ? msg.n_expert + ' experts' : 'dense'}${msg.mtp_layers ? ' · MTP head' : ''}`, 'dim');
    } else if (t === 'stage_start') {
      _run.current = msg.stage;
      _run.curEstS = msg.est_s || 0;
      _run.cands[msg.stage] = (msg.candidates || []).map(v => ({ value: v, status: 'pending' }));
      _run.stages[msg.stage] = { status: 'live', text: 'running' };
      recomputeEstTotal(msg.stage, _run.curEstS);
      tick();
      renderStepper(); renderStage();
      log(`stage ${stageIndex(msg.stage) + 1} · ${STAGE_NAME[msg.stage]} · ${(msg.candidates || []).join(', ')} · ${durText(msg.est_s || 0)}`);
    } else if (t === 'candidate_start') {
      const c = candRow(msg.stage, msg.value); c.status = 'running';
      _run.stages[msg.stage] = { status: 'live', text: `trying ${msg.value}` };
      renderStepper(); setStrip();
    } else if (t === 'candidate_result') {
      const c = candRow(msg.stage, msg.value);
      Object.assign(c, msg, { status: 'done' });
      _run.loads += 1;
      renderStage();
      const bits = [msg.ok ? 'ok' : (msg.error || 'failed'), msg.ctx != null ? `ctx ${Number(msg.ctx).toLocaleString()}` : '', msg.free_mb != null ? `free ${msg.free_mb} MB` : '',
                    msg.decode_tps != null ? `decode ${fmt(msg.decode_tps)} t/s` : '', msg.agg_tps != null && msg.agg_tps !== msg.decode_tps ? `agg ${fmt(msg.agg_tps)} t/s` : '',
                    msg.accept != null ? `accept ${Math.round(msg.accept * 100)} %` : '', msg.kl != null ? `KL ${msg.kl}` : ''].filter(Boolean);
      log(`candidate ${msg.value} · ${bits.join(' · ')}`, msg.ok ? '' : 'warn');
    } else if (t === 'stage_done') {
      _run.stages[msg.stage] = { status: msg.choice == null ? 'failed' : 'done', text: msg.choice == null ? (msg.reason || 'failed') : `${msg.choice}` };
      renderStepper(); setStrip();
      log(`stage ${stageIndex(msg.stage) + 1} done · ${msg.choice != null ? msg.choice + ' · ' : ''}${msg.reason || ''} · ${mmss(msg.seconds)} · ${msg.loads || 0} loads`, 'ok');
    } else if (t === 'stage_skipped') {
      _run.stages[msg.stage] = { status: 'skipped', text: SKIP_TEXT[msg.reason] || msg.reason || 'skipped' };
      recomputeEstTotal(_run.current, _run.curEstS);
      tick();
      renderStepper();
      log(`${STAGE_NAME[msg.stage]} skipped · ${SKIP_TEXT[msg.reason] || msg.reason}`, 'dim');
    } else if (t === 'iter_start') {
      log(`iter ${msg.iter} · ${msg.ctx != null ? '-c ' + msg.ctx : '-fitt ' + msg.fitt + ' MB'} · loading…`, 'dim');
    } else if (t === 'iter_result') {
      _run.iters.push(msg);
      if (_run.current === 'context') renderStage();
      log(`iter ${msg.iter} · -fitt ${msg.fitt} → free ${msg.actual_free_mb} / ${msg.total_vram_mb} MB · ctx ${Number(msg.n_ctx_seq || 0).toLocaleString()}`);
    } else if (t === 'iter_failed') {
      log(`iter ${msg.iter} failed · ${msg.error || msg.reason || 'unknown'}`, 'crit');
    } else if (t === 'sentinel_retry') {
      log(`iter ${msg.iter} · bogus memory reading · doubling -fitt ${msg.old_fitt} → ${msg.new_fitt} (${msg.attempt}/${msg.max_attempts})`, 'warn');
    } else if (t === 'perf_mode') {
      log(`perf mode → ${msg.mode}${msg.ok ? '' : ' (not applied: ' + (msg.error || 'rc=' + msg.rc) + ')'}`, msg.ok ? 'dim' : 'warn');
    } else if (t === 'line') {
      if (msg.text) raw(msg.text);
    } else if (t === 'loading_progress' || t === 'sentinel_seen_update' || t === 'plateau_detected' || t === 'bracket_precision_reached'
               || t === 'non_monotonic_detected' || t === 'cycle_detected') {
      if (t !== 'loading_progress') log(`${t.replace(/_/g, ' ')}${msg.reason ? ' · ' + msg.reason : ''}`, 'dim');
    } else if (t === 'model_done') {
      _done[msg.model_id] = msg; _doneModel = _doneModel || msg.model_id;
      if (typeof _recordToolRun === 'function') { try { _recordToolRun('autotune', { model_id: msg.model_id, ok: !!msg.ok, run_id: msg.run_id || '', objective: msg.objective, ctx_size: (msg.after || {}).ctx, decode_tps: (msg.after || {}).decode_tps }); } catch (_) {} }
      log(msg.ok ? `complete · ${(msg.changes || []).length} changes · verify ${(msg.verify || {}).ok ? 'pass' : 'not passed'}` : `stopped · ${msg.stop_reason || 'no result'}`, msg.ok ? 'ok' : 'warn');
    } else if (t === 'done') {
      finish(msg);
    }
  }

  // ── recommendation ──
  const SRC_LABEL = { measured: 'measured', sidecar: 'sidecar', model_card: 'model card', base_model: 'base model' };
  let _rows = [];
  function pct(a, b) { a = Number(a); b = Number(b); return Number.isFinite(a) && Number.isFinite(b) && b ? (a - b) / b * 100 : null; }
  function today() { return new Date().toISOString().slice(0, 10); }
  function restartOn() { const t = $('atRestartAfter'); return !!(t && t.classList.contains('on')); }
  function metaEvidence(s, meta) {
    if (s.source === 'sidecar') return `generation_config.json · ${meta.repo || ''}`;
    if (s.source === 'model_card') return `${meta.repo || ''} model card`;
    return `${meta.base_model || 'base model'} card`;
  }
  function recRows(done, section, meta, overwrite) {
    const rows = (done.changes || []).map(c => ({ ...c, source: c.source || 'measured', note: '' }));
    ((meta && meta.suggestions) || []).forEach(s => {
      const cur = section && section[s.key] != null ? String(section[s.key]) : '';
      const val = String(s.value);
      const curN = Number(cur), valN = Number(val);
      if (cur !== '' && Number.isFinite(curN) && Number.isFinite(valN) ? curN === valN : cur === val) return;
      const byHand = !overwrite && cur !== '';
      rows.push({ key: s.key, current: cur, recommended: val, source: s.source, evidence: metaEvidence(s, meta || {}),
                  selected: !byHand, note: byHand ? 'you set this by hand, left off' : '' });
    });
    return rows;
  }
  function rows() { return _rows; }
  function setMsg(text, cls) { const el = $('atRecMsg'); if (!el) return; el.textContent = text; el.className = 'msg' + (cls ? ' ' + cls : ''); }
  function renderRows() {
    const tb = $('atRecRows'); if (!tb) return;
    tb.innerHTML = _rows.map((r, i) => `<tr class="${r.selected ? '' : 'skip'}"><td class="mono">${esc(r.key)}</td><td class="old">${esc(r.current || '—')}</td><td class="new">${esc(r.recommended)}</td><td class="ev"><span class="at-src ${esc(r.source)}">${esc(SRC_LABEL[r.source] || r.source)}</span>${esc(r.evidence || '')}${r.note ? ` <small>· ${esc(r.note)}</small>` : ''}</td><td><button type="button" class="mc-toggle${r.selected ? ' on' : ''}" data-row="${i}"><span class="track"></span></button></td></tr>`).join('');
    const n = _rows.filter(r => r.selected).length;
    const sel = $('atRecSel'); if (sel) sel.textContent = `${n} of ${_rows.length} changes selected`;
    const btn = $('atApplyBtn');
    if (btn) { btn.textContent = `Apply ${n} change${n === 1 ? '' : 's'}${restartOn() ? ' + restart' : ''}`; btn.disabled = !n; }
    const pb = $('atProfileBtn'); if (pb) { pb.textContent = `Save as profile “tuned ${today()}”`; pb.disabled = !n; }
  }
  function toggleRow(i) { const r = _rows[i]; if (!r) return; r.selected = !r.selected; renderRows(); }
  function renderGuard(done) {
    const g = done.guard || {}, v = done.verify || {}, a = done.after || {};
    const item = (dot, k, val, d) => `<div class="g"><div class="k"><span class="at-dot ${dot}"></span>${k}</div><div class="v">${val}</div><div class="d">${d}</div></div>`;
    const host = $('atGuard'); if (!host) return;
    host.innerHTML =
      item(g.kl == null ? '' : (g.pass ? 'ok' : 'crit'), 'Quality guard', g.kl != null ? `KL ${esc(g.kl)} · ${g.pass ? 'pass' : 'fail'}` : 'not needed',
           esc(g.text || 'No lossy KV type was tried.') + ' · Speculative decoding is lossless by construction.')
      + item(v.ok ? 'ok' : (v.reason ? 'crit' : ''), 'Verify load', v.ok ? `fit · ${fmt(v.free_mb, 0)} MB free` : (v.reason ? 'failed' : 'not run'),
             v.ok ? `Recommended set loaded once; ${esc(Math.round(v.seconds || 0))} s of traffic at ${esc(Number(a.concurrency || 1))} slot${Number(a.concurrency || 1) === 1 ? '' : 's'}${v.dropped && v.dropped.length ? '; dropped ' + esc(v.dropped.join(', ')) : ''}.` : esc(v.reason || 'Verify needs the bench runtime.'))
      + item(a.wh_per_ktok != null ? 'ok' : '', 'Energy', a.wh_per_ktok != null ? `${fmt(a.wh_per_ktok, 2)} Wh / 1k tok` : '—',
             a.wh_per_ktok != null ? `Read from the energy module during verify (${esc(a.energy_source || 'psu')}).` : 'No power reading during verify.');
  }
  function renderCmp(done) {
    const a = done.after || {}, b = done.before || {};
    const row = (k, bv, av, unit, f, neutral) => {
      const max = Math.max(Number(bv) || 0, Number(av) || 0, 1), g = pct(av, bv), good = g != null && g > 0 && !neutral;
      const tail = g == null ? '' : (good ? `<b>${g >= 0 ? '+' : ''}${Math.round(g)} %</b>` : `<span style="color:var(--fg-dim)">${g >= 0 ? '+' : ''}${Math.round(g)} %</span>`);
      return `<div class="at-cmp-r"><span class="k">${k}</span><div class="bars"><i style="width:${Math.round(100 * (Number(bv) || 0) / max)}%"></i><i class="new" style="width:${Math.round(100 * (Number(av) || 0) / max)}%"></i></div><span class="r">${f(bv)} → ${f(av)}${unit} ${tail}</span></div>`;
    };
    const host = $('atCmp'); if (!host) return;
    host.innerHTML = row('Decode t/s', b.decode_tps, a.decode_tps, '', v => fmt(v, 1)) + row('Context', b.ctx, a.ctx, '', v => fmt(v, 0))
      + row(`Aggregate · ${esc(Number(a.concurrency || 1))} req`, b.agg_tps, a.agg_tps, '', v => fmt(v, 0)) + row('Prefill t/s', b.prefill_tps, a.prefill_tps, '', v => fmt(v, 0))
      + row('VRAM free', b.free_mb, a.free_mb, ' MB', v => fmt(v, 0), true);
  }
  function renderDone(done) {
    const section = _section[done.model_id] || {}, meta = _meta[done.model_id] || null;
    const d = dimsState().sampling;
    _rows = recRows(done, section, d.on ? meta : null, d.overwrite);
    const b = done.before || {}, a = done.after || {};
    const g = pct(a.decode_tps, b.decode_tps), x = b.ctx && a.ctx ? a.ctx / b.ctx : null;
    const big = $('atRecBig');
    if (big) big.innerHTML = [g != null ? `decode <b${g < 0 ? ' class="neg"' : ''}>${g >= 0 ? '+' : ''}${Math.round(g)} %</b>` : '', x ? `context <b>${x >= 2 ? Math.round(x) : Math.round(x * 100) / 100}×</b>` : '',
                              a.free_mb != null ? `VRAM free ${fmt(a.free_mb, 0)} MB` : ''].filter(Boolean).join(' · ');
    const warn = $('atRecWarn');
    if (warn) {
      if (g != null && g < -3) {
        warn.style.display = '';
        warn.innerHTML = `<b>Slower than your current config</b> — the recommended set measured −${Math.abs(Math.round(g))} % decode. Review the selected rows before applying.`;
      } else {
        warn.style.display = 'none';
        warn.innerHTML = '';
      }
    }
    renderRows();
    const doneStages = (done.stages || []).filter(s => s.status === 'done').length;
    const st = $('atDoneStats'); if (st) st.innerHTML = `<b>${esc(doneStages)} stages</b> · ${esc(mmss(done.elapsed_s))} · ${esc(Number(done.loads || 0))} loads`;
    const v = done.verify || {};
    const dv = $('atDoneVerify');
    if (dv) dv.innerHTML = v.ok ? `verified <b>${esc(Math.round(v.seconds || 0))} s</b> on the recommended set${v.dropped && v.dropped.length ? ' · dropped ' + esc(v.dropped.join(', ')) : ''}` : `<span class="warn">verify ${v.reason ? 'failed: ' + esc(v.reason) : 'not run'}</span>`;
    const pill = $('atDonePill'); if (pill) { pill.textContent = done.ok ? 'complete' : 'stopped'; pill.classList.toggle('ok', !!done.ok); pill.classList.toggle('running', false); }
    renderGuard(done); renderCmp(done);
    const dl = $('atDoneLog'), src = $('atLog'); if (dl && src) dl.innerHTML = src.innerHTML;
    const dm = $('atDoneLogMeta'); if (dm) dm.textContent = `${doneStages} stages · ${Number(done.loads || 0)} loads`;
    setMsg(`Previous config will be kept as profile “before tune ${today()}” for one-click revert.`);
  }
  function selectedRows() { return _rows.filter(r => r.selected); }
  async function postJson(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let j = null; try { j = await r.json(); } catch (_) {}
    if (!r.ok || !j || j.ok === false) throw new Error((j && j.error) || `HTTP ${r.status}`);
    return j;
  }
  async function readConfig(mid) {
    const cfg = await fetch('/api/llm/config').then(r => r.json());
    if (!cfg || typeof cfg !== 'object' || !cfg[mid] || typeof cfg[mid] !== 'object') throw new Error(`config unavailable for ${mid}`);
    return cfg;
  }
  async function apply() {
    const mid = _doneModel, sel = selectedRows();
    if (!mid || !sel.length) { setMsg('Nothing selected.'); return { ok: false, step: 'nothing selected' }; }
    const restart = restartOn(), enc = encodeURIComponent(mid);
    const btn = $('atApplyBtn'); if (btn) btn.disabled = true;
    let step = 'read config';
    try {
      const cfg = await readConfig(mid);
      const section = { ...cfg[mid] };
      step = 'save before-profile';
      await postJson(`/api/llm/profiles/${enc}/save`, { name: `before tune ${today()}`, values: section });
      sel.forEach(r => { section[r.key] = r.recommended; });
      delete cfg.__DEFAULTS__; cfg[mid] = section;
      step = 'write config';
      await postJson('/api/llm/config', cfg);
      _section[mid] = section;
      if (typeof _syncActiveProfile === 'function') { step = 'sync active profile'; await _syncActiveProfile(mid, section); }
      if (restart) { step = 'restart server'; await postJson('/api/llm/server/restart', {}); }
      setMsg(`Applied ${sel.length} change${sel.length === 1 ? '' : 's'}${restart ? ' and restarted llama-server' : ''}. Revert = activate profile “before tune ${today()}”.`, 'ok');
      if (typeof refreshLLMTab === 'function') { try { refreshLLMTab(); } catch (_) {} }
      return { ok: true };
    } catch (e) {
      setMsg(`Stopped at “${step}”: ${e && e.message ? e.message : e}. Nothing after that step was attempted.`, 'crit');
      if (btn) btn.disabled = false;
      return { ok: false, step };
    }
  }
  async function saveProfile() {
    const mid = _doneModel, sel = selectedRows();
    if (!mid || !sel.length) return;
    try {
      const cfg = await readConfig(mid);
      const section = { ...cfg[mid] };
      sel.forEach(r => { section[r.key] = r.recommended; });
      await postJson(`/api/llm/profiles/${encodeURIComponent(mid)}/save`, { name: `tuned ${today()}`, values: section, make_active: false });
      setMsg(`Saved profile “tuned ${today()}” (not activated).`, 'ok');
    } catch (e) { setMsg('Profile save failed: ' + (e && e.message ? e.message : e), 'crit'); }
  }
  function argsText(list) { return (list || []).map(r => (r.recommended === true || r.recommended === 'true') ? `--${r.key}` : `--${r.key} ${r.recommended}`).join(' '); }
  async function copyArgs() {
    const text = argsText(selectedRows());
    try { await navigator.clipboard.writeText(text); setMsg('Copied: ' + text, 'ok'); }
    catch (_) { window.prompt('llama-server args', text); }
  }
  function exportReport() {
    const doc = _done[_doneModel]; if (!doc) return;
    const name = String(_doneModel || 'model').replace(/[^A-Za-z0-9_.-]/g, '_');
    const blob = new Blob([JSON.stringify({ ...doc, rows: _rows }, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `autotune-${name}-${today()}.json`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
  }

  window.AT = { onOpen, run, cancel, again, stopServer, running, dimsState, objective, planRows, estimateText, onEvent,
                state: () => ({ run: _run, done: _done, doneModel: _doneModel, meta: _meta, section: _section, pre: _pre }),
                renderDone, recRows, rows, toggleRow, apply, saveProfile, copyArgs, exportReport, argsText,
                _debugSetStart: (ts) => { if (_run) { _run.startTs = ts; tick(); } } };
})();
