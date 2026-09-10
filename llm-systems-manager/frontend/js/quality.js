// Quality guard tool (#888): KL-divergence check of a config change against f16, over the autotune stream.
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => (window.TC ? TC.esc(String(s ?? '')) : String(s ?? ''));
  const today = () => new Date().toISOString().slice(0, 10);
  const KV_TYPES = ['f16', 'bf16', 'q8_0', 'q5_1', 'q5_0', 'q4_1', 'q4_0'];
  const LOAD_MODES = ['auto', 'none', 'mmap', 'mlock', 'mmap+mlock', 'dio'];
  const CHIP_SETS = { 'cache-type-k': KV_TYPES, 'cache-type-v': KV_TYPES, 'load-mode': LOAD_MODES };
  const BOOL_KEYS = ['flash-attn'];
  const KEYS = ['cache-type-k', 'cache-type-v', 'threads', 'threads-batch', 'n-gpu-layers', 'n-cpu-moe', 'batch-size', 'ubatch-size', 'flash-attn', 'load-mode'];
  let _models = [], _model = '', _cfg = {}, _rows = [], _es = null, _done = null, _pre = null, _wired = false, _neutral = false;
  let _elapsedIv = null, _startTs = 0;
  const PASS_NAME = { 'f16 base': 'pass 1 of 2 · f16 reference', candidate: 'pass 2 of 2 · candidate config' };
  // Parsed llama-perplexity statistics rendered beside the KL number.
  const STAT_DEFS = [
    ['same_top_p', 'Same top token', 1, '%', 'how often the change picks the same most-likely token as f16'],
    ['rms_dp', 'Typical Δ probability', 2, '%', 'RMS shift in a token’s probability'],
    ['p999_dp', '99.9% Δ probability', 1, '%', 'all but one token in a thousand shift less than this'],
    ['max_dp', 'Worst Δ probability', 1, '%', 'the single largest probability shift seen'],
  ];

  function model() { return _model; }
  function section() { return _cfg[_model] || {}; }
  function running() { return !!_es; }
  function row(key) { return _rows.find(r => r.key === key) || null; }
  function isBool(key) { return BOOL_KEYS.includes(key); }
  function boolish(v) { return ['true', '1', 'on', 'yes'].includes(String(v ?? '').trim().toLowerCase()); }
  function curOf(key) { const v = section()[key]; return v == null ? '' : String(v); }
  // A chosen value equal to the model's current one is not an override.
  function sameAsCur(key, val) {
    const cur = curOf(key);
    if (isBool(key)) return boolish(cur) === boolish(val);
    if (cur === '' || val === '') return cur === val;
    const a = Number(cur), b = Number(val);
    return Number.isFinite(a) && Number.isFinite(b) ? a === b : cur === val;
  }
  function normIn(key, v) { return isBool(key) ? (boolish(v) ? 'true' : 'false') : String(v ?? '').trim(); }
  function disp(key, v) { return isBool(key) ? (boolish(v) ? 'on' : 'off') : (v === '' ? '—' : v); }
  function overrides() {
    const out = {};
    _rows.forEach(r => { if (r.on && r.value !== '' && !sameAsCur(r.key, r.value)) out[r.key] = r.value; });
    return out;
  }
  function syncCount() { const c = $('qgCount'); if (c) c.textContent = String(Object.keys(overrides()).length); }
  function rowSummary(r) {
    const cur = `<span class="cur">${esc(disp(r.key, curOf(r.key)))}</span>`;
    if (!r.on) return `now ${cur}`;
    if (r.value === '') return `${cur} <span class="arrow">→</span> pick a value`;
    if (sameAsCur(r.key, r.value)) return `${cur} · unchanged`;
    return `${cur} <span class="arrow">→</span> ${esc(disp(r.key, r.value))}`;
  }
  function rowBody(r) {
    const set = CHIP_SETS[r.key];
    if (set) return `<div class="bl-chips">${set.map(v => `<span class="bl-chip${r.value === v ? ' on' : ''}" data-qg-v="${esc(v)}">${esc(v)}</span>`).join('')}</div>`;
    if (isBool(r.key)) return `<div class="at-frow"><label>Set to</label><button type="button" class="mc-toggle${r.value === 'true' ? ' on' : ''}" data-qg-bool><span class="track"></span><span class="tlbl">${r.value === 'true' ? 'on' : 'off'}</span></button></div>`;
    return `<div class="at-frow g2"><label>Value</label><input class="at-in xs" type="number" data-qg-num value="${esc(r.value)}" placeholder="${esc(curOf(r.key))}"></div>`;
  }
  function renderRows() {
    const box = $('qgRows'); if (!box) return;
    box.innerHTML = _rows.map(r => `<div class="at-dim qg-row${r.open ? '' : ' closed'}${r.on ? ' on' : ' off'}" data-qg-key="${esc(r.key)}">`
      + `<div class="at-dim-h"><button type="button" class="mc-toggle${r.on ? ' on' : ''}" data-qg-on><span class="track"></span></button>`
      + `<span class="n">${esc(r.key)}</span><span class="s" data-qg-sum>${rowSummary(r)}</span><span class="chev">▾</span></div>`
      + `<div class="at-dim-b">${rowBody(r)}</div></div>`).join('');
    syncCount();
  }
  function syncRow(r) {
    const box = $('qgRows'), host = box && [...box.children].find(c => c.dataset.qgKey === r.key);
    const el = host && host.querySelector('[data-qg-sum]');
    if (el) el.innerHTML = rowSummary(r);
    syncCount();
  }
  function renderModels() {
    const host = $('qgModelList'); if (!host) return;
    host.innerHTML = _models.length
      ? _models.map(m => `<label class="at-check"><button type="button" class="mc-toggle${m === _model ? ' on' : ''}" data-model="${esc(m)}"><span class="track"></span></button><b>${esc(m)}</b></label>`).join('')
      : '<div class="at-hint">No models configured.</div>';
    const c = $('qgModelCount'); if (c) c.textContent = _model || 'none selected';
  }
  function selectModel(m) { if (!m || m === _model) return; _model = m; renderModels(); renderRows(); }
  function toggleRow(key) {
    const r = row(key); if (!r) return;
    r.on = !r.on;
    // Switching a row on starts from the model's current value, so nothing is sent until it is changed.
    if (r.on) { r.open = true; if (r.value === '') r.value = normIn(key, curOf(key)); }
    renderRows();
  }
  function wire() {
    if (_wired) return; _wired = true;
    const mod = $('toolsModQg'); if (!mod) return;
    mod.addEventListener('click', ev => {
      const rail = ev.target.closest('.at-rail');
      if (rail && rail.classList.contains('locked') && !ev.target.closest('.at-runbar')) return;
      const mt = ev.target.closest('#qgModelList .mc-toggle');
      if (mt) { ev.preventDefault(); selectModel(mt.dataset.model); return; }
      const host = ev.target.closest('[data-qg-key]');
      if (host) {
        const key = host.dataset.qgKey, r = row(key);
        if (ev.target.closest('[data-qg-on]')) { toggleRow(key); return; }
        if (r && ev.target.closest('[data-qg-bool]')) { r.value = r.value === 'true' ? 'false' : 'true'; renderRows(); return; }
        const chip = ev.target.closest('.bl-chip');
        if (r && chip) { r.value = chip.dataset.qgV === r.value ? '' : chip.dataset.qgV; renderRows(); return; }
        if (ev.target.closest('.at-dim-h')) { r.open = !r.open; host.classList.toggle('closed', !r.open); return; }
      }
    });
    mod.addEventListener('input', ev => {
      const host = ev.target.closest('[data-qg-key]');
      if (!host || !ev.target.matches('[data-qg-num]')) return;
      const r = row(host.dataset.qgKey);
      if (r) { r.value = ev.target.value.trim(); syncRow(r); }
    });
  }
  async function onOpen(modelId, opts) {
    wire();
    if (running()) return;
    const [models, cfg, pre] = await Promise.all([
      fetch('/api/benchmark/models').then(r => r.json()).catch(() => ({})),
      fetch('/api/llm/config').then(r => r.json()).catch(() => ({})),
      fetch('/api/llm/autotune/preflight').then(r => r.json()).catch(() => null),
    ]);
    _models = (models && models.models) || []; _cfg = cfg || {}; _pre = pre && pre.ok ? pre : null;
    _model = modelId && _models.includes(modelId) ? modelId : (_models[0] || '');
    _rows = KEYS.map(k => ({ key: k, on: false, open: false, value: '' }));
    const ov = (opts && opts.overrides) || {};
    Object.keys(ov).forEach(k => { const r = row(k); if (r) { r.on = true; r.open = true; r.value = normIn(k, ov[k]); } });
    renderModels(); renderRows();
    _done = null; renderResult(null);
    await checkServer(pre && pre.ok ? pre : null);
    if (_pre && _pre.busy && !running()) attach(true);
  }
  async function serverUp() {
    try { const s = await fetch('/api/llama-state').then(r => r.json()); return s.state === 'awake' || s.state === 'sleeping'; } catch (_) { return false; }
  }
  // Re-reads preflight unless handed a fresh doc, so a stopped server clears the banner.
  async function checkServer(pre) {
    const banner = $('qgPreflight'), msg = $('qgPreflightMsg'), stop = $('qgStopBtn'), run = $('qgRunBtn');
    if (!banner) return;
    const up = await serverUp();
    let active = false, pxMissing = false, hint = '';
    try {
      const p = pre || await fetch('/api/llm/autotune/preflight').then(r => r.json());
      if (p && p.ok) {
        _pre = p;
        active = !!p.unit_active;
        pxMissing = !p.perplexity;
        const d = p.perplexity_detail;
        if (d && d.hint) hint = String(d.hint);
      }
    } catch (_) {}
    const show = (html, text, withStop, block) => {
      banner.style.display = '';
      if (msg) { if (html) msg.innerHTML = html; else msg.textContent = text; }
      if (stop) stop.style.display = withStop ? '' : 'none';
      if (run) { run.disabled = block; run.title = block ? (text || 'llama-server is running') : ''; }
    };
    if (up || active) show('<b>llama-server is running.</b> The quality check needs the port and the VRAM; stop it first.', 'llama-server is running', true, true);
    else if (hint) show('', hint, false, true);
    else if (pxMissing) show('', 'llama-perplexity or the KL text is missing on the agent — install the bench runtime.', false, false);
    else {
      banner.style.display = 'none';
      if (stop) stop.style.display = 'none';
      if (run && !running()) { run.disabled = false; run.title = ''; }
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
  function log(text) { const el = $('qgLog'); if (!el) return; const d = document.createElement('div'); d.textContent = text; el.appendChild(d); el.scrollTop = el.scrollHeight; }
  // Themed toast when the dashboard provides one; the run log always keeps the message.
  function notify(title, body, sev) {
    log(`${title} — ${body}`);
    if (typeof showToast === 'function') { try { showToast(title, body, sev || 'warning', false, '', 'alert', '', null, 9000); } catch (_) {} }
  }
  function pill(state, text) { const p = $('qgPill'); if (p) { p.className = 'bench-status-pill ' + state; p.textContent = text; } }
  function stage(text) { const s = $('qgStrip'); if (s) s.textContent = text || ''; }
  function mmss(s) { s = Math.max(0, Math.floor(s || 0)); return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; }
  function tick() {
    const el = $('qgStripTime'); if (!el || !_startTs) return;
    el.innerHTML = `elapsed <b>${esc(mmss((Date.now() - _startTs) / 1000))}</b>`;
  }
  function startElapsed() { stopElapsed(); _startTs = Date.now(); tick(); _elapsedIv = setInterval(tick, 1000); }
  function stopElapsed() { if (_elapsedIv) { clearInterval(_elapsedIv); _elapsedIv = null; } }
  // Settles the strip on the run's own total when the agent reports one.
  function settleElapsed(done) {
    stopElapsed();
    const el = $('qgStripTime'); if (!el) return;
    const s = done && done.elapsed_s != null ? Number(done.elapsed_s) : (_startTs ? (Date.now() - _startTs) / 1000 : null);
    el.innerHTML = s == null ? '' : `took <b>${esc(mmss(s))}</b>`;
    _startTs = 0;
  }
  function busy(on) {
    const rail = $('qgRail'); if (rail) rail.classList.toggle('locked', on);
    const r = $('qgRunBtn'), c = $('qgCancelBtn'); if (r) r.style.display = on ? 'none' : ''; if (c) c.style.display = on ? '' : 'none';
    if (typeof toolsSyncRunDot === 'function') toolsSyncRunDot();
  }
  function openStream() {
    if (_es) { try { _es.close(); } catch (_) {} }
    _es = SG.open({ url: '/api/llm/autotune/stream', onEvent: onEvent, onDrop: () => log('stream dropped — reconnecting'), onGiveUp: () => finish({ ok: false, error: 'stream lost' }) });
  }
  async function run() {
    const mid = model(); if (!mid) { notify('Quality guard', 'Select a model before running the check.'); return; }
    const ov = overrides();
    if (!Object.keys(ov).length) { notify('Quality guard', 'Switch on at least one key and give it a value that differs from the current one.'); return; }
    const klMax = parseFloat(($('qgKlMax') || {}).value); const body = { model_ids: [mid], objective: 'fit', mode: 'quality', overrides: ov, kl_max: Number.isFinite(klMax) ? klMax : 0.02 };
    let r;
    try { r = await fetch('/api/llm/autotune/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(x => x.json()); }
    catch (e) { notify('Quality check failed to start', (e && e.message ? e.message : String(e)), 'critical'); return; }
    if (!r || !r.ok) {
      if (r && /in progress/i.test(r.error || r.detail || '')) { attach(true); return; }
      notify('Quality check failed to start', (r && (r.error || r.detail)) || 'the agent refused the run', 'critical'); return;
    }
    _done = null; renderResult(null); const lg = $('qgLog'); if (lg) lg.innerHTML = '';
    pill('running', 'running'); stage('starting'); startElapsed(); busy(true); openStream();
  }
  // neutral=true: the shared stream is busy, but we don't yet know it's a quality run.
  function attach(neutral) {
    _neutral = !!neutral;
    if (_neutral) { pill('warn', 'another tool is running'); }
    else { pill('running', 'attached'); log('attached to a run already in progress'); }
    stage(_neutral ? '' : 'attached'); startElapsed();
    busy(true);
    openStream();
  }
  function cancel() { fetch('/api/llm/autotune/cancel', { method: 'POST' }).catch(() => {}); }
  // Tool switch: drop this module's stream, leaving the run itself alone.
  function detach() {
    if (!_es) return;
    try { _es.close(); } catch (_) {}
    _es = null; _neutral = false;
    stopElapsed(); _startTs = 0;
    busy(false);
  }
  function finish(msg) {
    if (_es) { try { _es.close(); } catch (_) {} _es = null; }
    busy(false);
    if (!_done) { pill('warn', msg && msg.error ? 'failed' : 'stopped'); stage(msg && msg.error ? 'the run did not finish' : 'stopped'); settleElapsed(null); }
    _neutral = false;
  }
  function onEvent(msg) {
    // Flip a neutral "another tool" attach to "running" once a quality-mode event proves it's ours.
    if (_neutral && (msg.mode === 'quality' || msg.stage === 'quality')) { _neutral = false; pill('running', 'running'); }
    const t = msg.type;
    if (t === 'line') log(msg.text || '');
    else if (t === 'candidate_start') { stage(PASS_NAME[msg.value] || ('measuring ' + msg.value)); log('▶ ' + msg.value); }
    else if (t === 'candidate_result') log((msg.ok ? '✓ ' : '✗ ') + msg.value + (msg.kl != null ? ' · KL ' + msg.kl : '') + (msg.error ? ' · ' + msg.error : ''));
    else if (t === 'model_done') {
      if (msg.mode !== 'quality') return;   // an Autotune run on the shared stream is not ours
      _done = msg;
      if (typeof _recordToolRun === 'function') { try { _recordToolRun('quality', { model_id: msg.model_id, ok: !!msg.ok, run_id: msg.run_id || '', mode: 'quality', llama_build: msg.llama_build || undefined, kl: (msg.guard || {}).kl, kl_max: (msg.guard || {}).kl_max, kl_pass: (msg.guard || {}).pass, changed: (msg.changes || []).map(c => c.key).join(',') }); } catch (_) {} }
      renderResult(msg);
    } else if (t === 'done') finish(msg);
  }
  // Plain-language reading of the KL number, scaled by how far it sits from the limit.
  function verdict(g) {
    const kl = Number(g.kl), max = Number(g.kl_max);
    if (!Number.isFinite(kl) || !Number.isFinite(max) || max <= 0) return '';
    const lim = String(g.kl_max), num = kl.toFixed(4), ratio = kl / max;
    const x = n => (n >= 10 ? String(Math.round(n)) : String(Math.round(n * 10) / 10));
    let head, tail;
    if (ratio > 1) {
      head = `Quality is not safe: the measured ${num} is ${x(ratio)}× over the ${lim} limit`;
      tail = 'Applying this would cost measurable output quality — keep the current setting, or test a milder value.';
    } else if (ratio > 0.5) {
      head = `Quality is inside the limit, but only just: the measured ${num} is ${Math.round(ratio * 100)}% of the ${lim} line`;
      tail = 'Occasional wording differences are likely. Tighten the limit or test a milder value if this model does exact-format work.';
    } else if (ratio > 0.1) {
      head = `Quality is safe: the measured ${num} is ${x(1 / ratio)}× below the ${lim} limit`;
      tail = 'Differences at this level are not visible in normal use.';
    } else {
      head = ratio > 0
        ? `Quality is effectively unchanged: the measured ${num} is ${x(1 / ratio)}× below the ${lim} limit`
        : `Quality is unchanged: no divergence from the f16 reference was measured at all`;
      tail = 'The changed config behaves like the f16 reference.';
    }
    const same = Number((g.stats || {}).same_top_p);
    const mid = Number.isFinite(same)
      ? (ratio > 1
        ? `, and it picks a different most-likely next token from the f16 reference on ${(100 - same).toFixed(1)}% of tokens`
        : `, and it picks the same most-likely next token as the f16 reference ${same.toFixed(1)}% of the time`)
      : '';
    return head + mid + '. ' + tail;
  }
  function statsHtml(g) {
    const st = g.stats || {};
    const cells = STAT_DEFS.filter(d => Number.isFinite(Number(st[d[0]]))).map(d =>
      `<div class="qg-stat"><span class="v">${esc(Number(st[d[0]]).toFixed(d[2]) + d[3])}</span>`
      + `<span class="k">${esc(d[1])}</span><span class="h">${esc(d[4])}</span></div>`);
    return cells.length ? `<div class="qg-stats">${cells.join('')}</div>` : '';
  }
  function renderResult(done) {
    const box = $('qgResult'), ab = $('qgApplyBtn'), bb = $('qgBenchBtn'), note = $('qgApplyNote'), meta = $('qgResultMeta');
    if (!box) return;
    if (!done) {
      box.innerHTML = '<div class="at-hint">Pick a model, change one or more keys, then run the check.</div>';
      if (ab) ab.style.display = 'none';
      if (bb) bb.style.display = 'none';
      if (note) note.textContent = '';
      if (meta) meta.textContent = '';
      stage(''); const t = $('qgStripTime'); if (t) t.textContent = '';
      pill('', 'idle'); return;
    }
    const g = done.guard || {};
    const ok = done.ok && g.pass === true;
    pill(ok ? 'ok' : 'warn', g.error ? 'failed' : ok ? 'pass' : 'fail');
    stage(g.error ? 'the check did not complete' : ok ? 'both passes complete · quality within the limit' : 'both passes complete · quality over the limit');
    settleElapsed(done);
    if (meta) meta.textContent = `${done.elapsed_s != null ? Math.round(done.elapsed_s / 60) + ' min' : ''}${done.llama_build ? ' · llama.cpp ' + done.llama_build : ''}`;
    const rows = (done.changes || []).map(c => `<tr><td>${esc(c.key)}</td><td>${esc(c.current ?? '—')}</td><td><b>${esc(c.recommended ?? '(removed)')}</b></td></tr>`).join('');
    const say = g.error ? '' : verdict(g);
    box.innerHTML = `<div class="qg-big ${ok ? 'ok' : 'bad'}"><span class="v">${g.kl != null ? esc(Number(g.kl).toFixed(4)) : '—'}</span><span class="l">mean KL vs f16 · pass when ≤ ${esc(g.kl_max)}</span></div>`
      + (say ? `<div class="qg-verdict${ok ? '' : ' bad'}" id="qgVerdict">${esc(say)}</div>` : '')
      + statsHtml(g)
      + (g.error ? `<div class="at-notice warn">${esc(g.error)}</div>` : '')
      + `<table class="at-rt"><thead><tr><th>Key</th><th>Current</th><th>Tested</th></tr></thead><tbody>${rows}</tbody></table>`;
    if (ab) ab.style.display = ok && rows ? '' : 'none';
    if (bb) bb.style.display = done.model_id ? '' : 'none';
    if (note) {
      note.textContent = ok && rows
        ? 'A pass means the change is safe for quality — it does not mean it is faster. This check never measures speed; benchmark the model to find that out.'
        : 'This check measures quality only, never speed. Applying is not recommended while the guard fails.';
    }
  }
  // Deep-links the Benchmark tool at the same model, so speed can be measured separately.
  function openBenchmark() {
    const mid = _done && _done.model_id; if (!mid) return;
    if (typeof toolsOpenTool === 'function') toolsOpenTool('benchmark', mid);
  }
  // Throws on a non-2xx status or an {ok:false} body, same contract as AT.js's postJson.
  async function postJson(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let j = null; try { j = await r.json(); } catch (_) {}
    if (!r.ok || !j || j.ok === false) throw new Error((j && j.error) || `HTTP ${r.status}`);
    return j;
  }
  // Mirrors AT.apply's write path: read config, save a before-profile, merge, write, then sync.
  async function apply() {
    if (!_done || !(_done.guard || {}).pass) return;
    const mid = _done.model_id, changes = _done.changes || [];
    if (!mid || !changes.length) return;
    const ab = $('qgApplyBtn'); if (ab) ab.disabled = true;
    let step = 'read config';
    try {
      const cfg = await fetch('/api/llm/config').then(r => r.json());
      if (!cfg || typeof cfg !== 'object' || !cfg[mid] || typeof cfg[mid] !== 'object') throw new Error(`config unavailable for ${mid}`);
      const sec = { ...cfg[mid] };
      step = 'save before-profile';
      await postJson(`/api/llm/profiles/${encodeURIComponent(mid)}/save`, { name: `before quality check ${today()}`, values: sec });
      changes.forEach(c => { if (c.recommended == null) delete sec[c.key]; else sec[c.key] = c.recommended; });
      delete cfg.__DEFAULTS__; cfg[mid] = sec;
      step = 'write config';
      await postJson('/api/llm/config', cfg);
      _cfg[mid] = sec;
      if (typeof _syncActiveProfile === 'function') { step = 'sync active profile'; await _syncActiveProfile(mid, sec); }
      if (ab) ab.textContent = '✓ Applied';
      renderRows();
      if (typeof refreshLLMTab === 'function') refreshLLMTab();
    } catch (e) {
      notify(`Apply failed at “${step}”`, (e && e.message ? e.message : String(e)), 'critical');
      if (ab) ab.disabled = false;
    }
  }
  window.QG = { onOpen, run, cancel, detach, apply, openBenchmark, onEvent, running, overrides, stopServer, rows: () => _rows };
})();
