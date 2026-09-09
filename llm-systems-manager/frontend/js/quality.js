// Quality guard tool (#888): KL-divergence check of a config change against f16, over the autotune stream.
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => (window.TC ? TC.esc(String(s ?? '')) : String(s ?? ''));
  const today = () => new Date().toISOString().slice(0, 10);
  const KEYS = ['cache-type-k', 'cache-type-v', 'threads', 'threads-batch', 'n-gpu-layers', 'n-cpu-moe', 'batch-size', 'ubatch-size', 'flash-attn', 'no-mmap', 'mlock'];
  let _models = [], _cfg = {}, _rows = [], _es = null, _done = null, _pre = null, _wired = false;

  function model() { const s = $('qgModel'); return s ? s.value : ''; }
  function section() { return _cfg[model()] || {}; }
  function running() { return !!_es; }
  function overrides() {
    const out = {};
    _rows.forEach(r => { if (r.value !== '' && r.value !== (section()[r.key] ?? '')) out[r.key] = r.value; });
    return out;
  }
  function renderRows() {
    const box = $('qgRows'); if (!box) return;
    const sec = section();
    box.innerHTML = _rows.map((r, i) => `<div class="qg-row at-frow g2" data-i="${i}"><label>${esc(r.key)}</label><span class="cur" title="current value">${esc(sec[r.key] ?? '—')}</span><input class="at-in xs" data-i="${i}" value="${esc(r.value)}" placeholder="new value"><button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-rm="${i}" title="Remove">✕</button></div>`).join('')
      || '<div class="at-hint">No changes yet — add a key below or open this tool from an Autotune recommendation.</div>';
    const c = $('qgCount'); if (c) c.textContent = String(Object.keys(overrides()).length);
    const add = $('qgAddKey');
    if (add) add.innerHTML = '<option value="">add key…</option>' + KEYS.filter(k => !_rows.some(r => r.key === k)).map(k => `<option value="${k}">${k}</option>`).join('');
  }
  function addRow(key, value) { if (!key || _rows.some(r => r.key === key)) return; _rows.push({ key, value: value ?? '' }); renderRows(); }
  function wire() {
    if (_wired) return; _wired = true;
    const mod = $('toolsModQg'); if (!mod) return;
    mod.addEventListener('change', ev => {
      if (ev.target.id === 'qgAddKey' && ev.target.value) { addRow(ev.target.value, ''); ev.target.value = ''; }
      else if (ev.target.id === 'qgModel') { renderRows(); }
    });
    mod.addEventListener('input', ev => { const i = ev.target.dataset.i; if (i != null && ev.target.tagName === 'INPUT') { _rows[i].value = ev.target.value.trim(); const c = $('qgCount'); if (c) c.textContent = String(Object.keys(overrides()).length); } });
    mod.addEventListener('click', ev => { const rm = ev.target.closest('[data-rm]'); if (rm) { _rows.splice(parseInt(rm.dataset.rm, 10), 1); renderRows(); } });
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
    const sel = $('qgModel');
    if (sel) { sel.innerHTML = _models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join(''); if (modelId && _models.includes(modelId)) sel.value = modelId; }
    const pf = $('qgPreflight'), pm = $('qgPreflightMsg');
    if (pf && pm) { const missing = _pre && !_pre.perplexity; pf.style.display = missing ? '' : 'none'; pm.textContent = missing ? 'llama-perplexity or the KL text is missing on the agent — install the bench runtime.' : ''; }
    _rows = [];
    const ov = opts && opts.overrides || {};
    Object.keys(ov).forEach(k => { if (KEYS.includes(k)) _rows.push({ key: k, value: String(ov[k]) }); });
    _done = null; renderResult(null); renderRows();
    if (_pre && _pre.busy) attach();
  }
  function log(text) { const el = $('qgLog'); if (!el) return; const d = document.createElement('div'); d.textContent = text; el.appendChild(d); el.scrollTop = el.scrollHeight; }
  function pill(state, text) { const p = $('qgPill'); if (p) { p.className = 'bench-status-pill ' + state; p.textContent = text; } }
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
    const mid = model(); if (!mid) { alert('Select a model.'); return; }
    const ov = overrides();
    if (!Object.keys(ov).length) { alert('Change at least one key before running the check.'); return; }
    const klMax = parseFloat(($('qgKlMax') || {}).value); const body = { model_ids: [mid], objective: 'fit', mode: 'quality', overrides: ov, kl_max: Number.isFinite(klMax) ? klMax : 0.02 };
    let r;
    try { r = await fetch('/api/llm/autotune/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(x => x.json()); }
    catch (e) { alert('Quality check request failed: ' + (e && e.message ? e.message : e)); return; }
    if (!r || !r.ok) { if (r && /in progress/i.test(r.error || r.detail || '')) { attach(); return; } alert((r && (r.error || r.detail)) || 'Failed to start the check'); return; }
    _done = null; renderResult(null); const lg = $('qgLog'); if (lg) lg.innerHTML = '';
    pill('running', 'running'); busy(true); openStream();
  }
  function attach() { pill('running', 'attached'); busy(true); log('attached to a run already in progress'); openStream(); }
  function cancel() { fetch('/api/llm/autotune/cancel', { method: 'POST' }).catch(() => {}); }
  function finish(msg) {
    if (_es) { try { _es.close(); } catch (_) {} _es = null; }
    busy(false);
    if (!_done) pill('warn', msg && msg.error ? 'failed' : 'stopped');
  }
  function onEvent(msg) {
    const t = msg.type;
    if (t === 'line') log(msg.text || '');
    else if (t === 'candidate_start') { const s = $('qgStrip'); if (s) s.textContent = 'measuring ' + msg.value; log('▶ ' + msg.value); }
    else if (t === 'candidate_result') log((msg.ok ? '✓ ' : '✗ ') + msg.value + (msg.kl != null ? ' · KL ' + msg.kl : '') + (msg.error ? ' · ' + msg.error : ''));
    else if (t === 'model_done') {
      if (msg.mode !== 'quality') return;   // an Autotune run on the shared stream is not ours
      _done = msg;
      if (typeof _recordToolRun === 'function') { try { _recordToolRun('quality', { model_id: msg.model_id, ok: !!msg.ok, run_id: msg.run_id || '', mode: 'quality', llama_build: msg.llama_build || undefined, kl: (msg.guard || {}).kl, kl_max: (msg.guard || {}).kl_max, kl_pass: (msg.guard || {}).pass, changed: (msg.changes || []).map(c => c.key).join(',') }); } catch (_) {} }
      renderResult(msg);
    } else if (t === 'done') finish(msg);
  }
  function renderResult(done) {
    const box = $('qgResult'), ab = $('qgApplyBtn'), meta = $('qgResultMeta'), strip = $('qgStrip');
    if (!box) return;
    if (!done) { box.innerHTML = '<div class="at-empty">Pick a model, change one or more keys, then run the check.</div>'; if (ab) ab.style.display = 'none'; if (meta) meta.textContent = ''; if (strip) strip.textContent = ''; pill('', 'idle'); return; }
    const g = done.guard || {};
    const ok = done.ok && g.pass === true;
    pill(ok ? 'ok' : 'warn', g.error ? 'failed' : ok ? 'pass' : 'fail');
    if (strip) strip.textContent = '';
    if (meta) meta.textContent = `${done.elapsed_s != null ? Math.round(done.elapsed_s / 60) + ' min' : ''}${done.llama_build ? ' · llama.cpp ' + done.llama_build : ''}`;
    const rows = (done.changes || []).map(c => `<tr><td>${esc(c.key)}</td><td>${esc(c.current ?? '—')}</td><td><b>${esc(c.recommended ?? '(removed)')}</b></td></tr>`).join('');
    box.innerHTML = `<div class="qg-big ${ok ? 'ok' : 'bad'}"><span class="v">${g.kl != null ? esc(Number(g.kl).toFixed(4)) : '—'}</span><span class="l">mean KL vs f16 · pass when ≤ ${esc(g.kl_max)}</span></div>`
      + (g.error ? `<div class="at-notice warn">${esc(g.error)}</div>` : '')
      + `<table class="at-rt"><thead><tr><th>Key</th><th>Current</th><th>Tested</th></tr></thead><tbody>${rows}</tbody></table>`;
    if (ab) ab.style.display = ok && rows ? '' : 'none';
  }
  // Mirrors AT.apply's write path: read config, save a before-profile, merge, write, then sync.
  async function apply() {
    if (!_done || !(_done.guard || {}).pass) return;
    const mid = _done.model_id, changes = _done.changes || [];
    if (!mid || !changes.length) return;
    const ab = $('qgApplyBtn'); if (ab) ab.disabled = true;
    try {
      const cfg = await fetch('/api/llm/config').then(r => r.json());
      if (!cfg || typeof cfg !== 'object' || !cfg[mid] || typeof cfg[mid] !== 'object') throw new Error(`config unavailable for ${mid}`);
      const sec = { ...cfg[mid] };
      await fetch(`/api/llm/profiles/${encodeURIComponent(mid)}/save`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: `before quality check ${today()}`, values: sec }) });
      changes.forEach(c => { if (c.recommended == null) delete sec[c.key]; else sec[c.key] = c.recommended; });
      delete cfg.__DEFAULTS__; cfg[mid] = sec;
      await fetch('/api/llm/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg) });
      _cfg[mid] = sec;
      if (typeof _syncActiveProfile === 'function') await _syncActiveProfile(mid, sec);
      if (ab) ab.textContent = '✓ Applied';
      if (typeof loadLlmConfig === 'function') loadLlmConfig();
    } catch (e) {
      alert('Apply failed: ' + (e && e.message ? e.message : e));
      if (ab) ab.disabled = false;
    }
  }
  window.QG = { onOpen, run, cancel, apply, onEvent, running, overrides, addRow };
})();
