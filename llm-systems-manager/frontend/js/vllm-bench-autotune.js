// vLLM wizards: Auto-Tune max-model-len + Benchmark (vllm bench serve) —
// overlay + EventSource clients for /api/vllm/{autotune,bench}/*.

let _vatEventSrc = null;
let _vatOrig = null;        // {binary, args} from svcconfig GET
let _vatResult = null;      // last model_done payload

function _vatEl(id) { return document.getElementById(id); }

function _wizStatus(id, txt, cls) {
  const el = _vatEl(id);
  if (el) { el.textContent = txt; el.className = 'sub ' + (cls || ''); }
}

function _wizRawAppend(boxId, cntId, text) {
  const box = _vatEl(boxId);
  if (!box) return;
  const cnt = _vatEl(cntId);
  if (cnt) cnt.textContent = String((+cnt.textContent || 0) + 1);
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const div = document.createElement('div');
  div.textContent = text;
  box.appendChild(div);
  while (box.childNodes.length > 2000) box.removeChild(box.firstChild);
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

function _wizRawReset(boxId, cntId) {
  const box = _vatEl(boxId);
  if (box) box.innerHTML = '';
  const cnt = _vatEl(cntId);
  if (cnt) cnt.textContent = '0';
}

function _wizStat(k, v, u) {
  return `<div class="at-stat"><div class="at-stat-k">${k}</div>
      <div class="at-stat-v">${v}<span class="at-stat-u">${u || ''}</span></div></div>`;
}

function _vatProgress(html) {
  const el = _vatEl('vllmAtProgress');
  if (el) el.insertAdjacentHTML('beforeend', html);
}

function _vatStripMaxLen(args) {
  return (args || []).filter(a => a.flag !== '--max-model-len'
                                  && !String(a.flag || '').startsWith('--max-model-len='));
}

function _vatArgsWithMaxLen(args, value) {
  const out = _vatStripMaxLen(args);
  out.push({ flag: '--max-model-len', value: String(value), bool: false });
  return out;
}

async function openVllmAutotune() {
  _vatEl('vllmAtOverlay').classList.add('open');
  _vatEl('vllmAtProgress').innerHTML = '';
  _vatEl('vllmAtResults').innerHTML = '';
  _wizRawReset('vllmAtRawLog', 'vllmAtRawCount');
  _wizStatus('vllmAtStatus', '');
  _vatOrig = null;
  _vatResult = null;
  _vatEl('vllmAtRunBtn').style.display = '';
  _vatEl('vllmAtCancelBtn').style.display = 'none';
  const cur = _vatEl('vllmAtCurrent');
  cur.textContent = 'Reading server config…';
  try {
    const r = await _vatFetchSvcconfig();
    _vatOrig = { binary: r.binary, args: r.args || [] };
    const ml = _vatGetMaxLen(r.args || []);
    const parts = (r.binary || '').split(/\s+/);
    const model = parts.length > 2 ? parts[2] : '(unknown)';
    cur.textContent = `Model: ${model} — current --max-model-len: ${ml != null ? ml : '(model default)'}`;
    _vatEl('vllmAtRunBtn').disabled = false;
  } catch (e) {
    cur.textContent = `⚠ ${e.message || e} — is the vLLM agent online?`;
    _vatEl('vllmAtRunBtn').disabled = true;
  }
}

async function _vatFetchSvcconfig() {
  const r = await fetch(window._withAgentParam('/api/vllm/server/svcconfig'))
    .then(_jsonOrThrow);
  if (!r.ok) throw new Error(r.error || 'svcconfig read failed');
  return r;
}

function _vatGetMaxLen(args) {
  for (const a of args) {
    const flag = String(a.flag || '');
    if (flag === '--max-model-len' && a.value) return a.value;
    if (flag.startsWith('--max-model-len=')) return flag.split('=', 2)[1];
  }
  return null;
}

function closeVllmAutotune() {
  _vatEl('vllmAtOverlay').classList.remove('open');
  if (_vatEventSrc) cancelVllmAutotune();
  _vatFinish();
}

function _vatNum(id, def) {
  const v = parseFloat(_vatEl(id).value);
  return Number.isFinite(v) ? v : def;
}

async function runVllmAutotune() {
  if (!_vatOrig) return;
  const body = {
    probe_len: Math.round(_vatNum('vllmAtProbeLen', 4096)),
    concurrency: _vatNum('vllmAtConc', 1.0),
    kv_fraction: _vatNum('vllmAtFrac', 100) / 100,
    load_timeout_s: Math.round(_vatNum('vllmAtTimeout', 600)),
    report_only: _vatEl('vllmAtReportOnly').checked,
  };
  const okGo = await _themedConfirm({
    title: 'Run vLLM Auto-Tune?',
    bodyHtml: 'Auto-tune restarts the vLLM server 2–3 times and drops any '
      + 'in-flight requests. The original config is restored if anything fails.',
    confirmLabel: 'Run Auto Tune',
    danger: true,
  });
  if (!okGo) return;
  _vatEl('vllmAtProgress').innerHTML = '';
  _vatEl('vllmAtResults').innerHTML = '';
  _vatEl('vllmAtRunBtn').style.display = 'none';
  _vatEl('vllmAtCancelBtn').style.display = '';
  _wizStatus('vllmAtStatus', 'Starting…');
  let r;
  try {
    r = await fetch(window._withAgentParam('/api/vllm/autotune/run'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(_jsonOrThrow);
  } catch (e) { r = { ok: false, error: e.message || String(e) }; }
  if (!r.ok) {
    _wizStatus('vllmAtStatus', `⚠ ${r.error || 'run failed'}`, 'err');
    _vatFinish();
    return;
  }
  _vatEventSrc = new EventSource(window._withAgentParam('/api/vllm/autotune/stream'));
  _vatEventSrc.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    _vatHandleEvent(msg);
  };
  _vatEventSrc.onerror = () => {
    if (_vatEventSrc && _vatEventSrc.readyState === EventSource.CLOSED) {
      _wizStatus('vllmAtStatus', '⚠ progress stream closed', 'err');
      _vatFinish();
    }
  };
}

function _vatHandleEvent(msg) {
  switch (msg.type) {
    case 'keepalive': return;
    case 'model_start':
      _wizStatus('vllmAtStatus', `Tuning ${msg.model || msg.unit}…`);
      _vatProgress(`<div class="sub">Unit <b>${_esc(msg.unit)}</b> — original --max-model-len: ${msg.original_max_len ?? '(model default)'}</div>`);
      return;
    case 'step_start':
      _vatProgress(`<div class="sub">▸ ${_esc(msg.step)}${msg.max_model_len != null ? ' @ --max-model-len ' + msg.max_model_len : ''}</div>`);
      return;
    case 'loading_progress':
      _wizStatus('vllmAtStatus', `${msg.step}: loading… ${Math.round(msg.elapsed_s)}s / ${msg.timeout_s}s`);
      return;
    case 'line':
      _wizRawAppend('vllmAtRawLog', 'vllmAtRawCount', msg.text);
      return;
    case 'kv_capacity':
      _vatProgress(`<div class="sub">KV cache capacity: <b>${Number(msg.tokens).toLocaleString()}</b> tokens</div>`);
      return;
    case 'recommendation':
      _vatProgress(`<div class="sub">Recommended --max-model-len: <b>${Number(msg.max_model_len).toLocaleString()}</b> (concurrency ${msg.concurrency}×, KV budget ${Math.round(msg.kv_fraction * 100)}%)</div>`);
      return;
    case 'rollback_failed':
      _vatProgress(`<div class="sub" style="color:var(--crit,#e05050);">⚠ Rollback failed: ${_esc(msg.error || '')} — check the unit manually.</div>`);
      return;
    case 'model_done':
      _vatResult = msg;
      _vatRenderResult(msg);
      return;
    case 'done':
      _wizStatus('vllmAtStatus', msg.cancelled ? 'Cancelled.' : (msg.ok ? 'Done.' : 'Failed.'),
                    msg.ok ? '' : 'err');
      _vatFinish();
      return;
  }
}

function _vatRenderResult(msg) {
  const el = _vatEl('vllmAtResults');
  if (!msg.ok) {
    el.innerHTML = `<div class="at-result-card" style="border-left-color:var(--crit);"><span style="color:var(--crit);">✕ ${_esc(msg.error || 'failed')}</span></div>`;
    return;
  }
  const conc = msg.max_concurrency_x != null ? String(msg.max_concurrency_x) : '—';
  const actions = msg.applied
    ? `<span style="color:var(--ok);font-size:0.85em;">✓ Applied to ExecStart</span>
       <button class="btn" onclick="vllmAtRevert(this)">↩ Revert to ${msg.original_max_len ?? 'model default'}</button>`
    : `<button class="btn" onclick="vllmAtApply(this)">💾 Apply ${Number(msg.max_model_len).toLocaleString()}</button>`;
  el.innerHTML = `
    <div class="at-result-card">
      <div class="at-result-head"><span class="at-result-model">--max-model-len</span></div>
      <div class="at-result-grid">
        ${_wizStat('Recommended', Number(msg.max_model_len).toLocaleString(), 'tok')}
        ${_wizStat('KV capacity', Number(msg.kv_tokens).toLocaleString(), 'tok')}
        ${_wizStat('Concurrency', conc, '×')}
        ${_wizStat('Previous', msg.original_max_len != null ? Number(msg.original_max_len).toLocaleString() : 'default', msg.original_max_len != null ? 'tok' : '')}
      </div>
      <div class="at-result-actions">${actions}</div>
    </div>`;
}

async function _vatPostSvcconfig(maxLen, btn, okLabel) {
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  let r;
  try {
    // Fresh read so a concurrent Server Config edit isn't clobbered.
    const cur = await _vatFetchSvcconfig();
    const args = maxLen != null ? _vatArgsWithMaxLen(cur.args, maxLen)
                                : _vatStripMaxLen(cur.args);
    r = await fetch(window._withAgentParam('/api/vllm/server/svcconfig'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ binary: cur.binary, args, restart: true }),
    }).then(_jsonOrThrow);
  } catch (e) { r = { ok: false, error: e.message || String(e) }; }
  if (btn) {
    btn.disabled = false;
    btn.textContent = r.ok ? okLabel : `⚠ ${r.error || 'failed'}`;
  }
  if (r.ok && typeof fetchVllmMetrics === 'function') fetchVllmMetrics();
}

function vllmAtApply(btn) {
  if (!_vatResult) return;
  _vatPostSvcconfig(_vatResult.max_model_len, btn, '✓ Applied');
}

function vllmAtRevert(btn) {
  if (!_vatResult) return;
  _vatPostSvcconfig(_vatResult.original_max_len ?? null, btn, '✓ Reverted');
}

async function cancelVllmAutotune() {
  _wizStatus('vllmAtStatus', 'Cancelling…');
  try {
    await fetch(window._withAgentParam('/api/vllm/autotune/cancel'), { method: 'POST' });
  } catch (e) {
    _wizStatus('vllmAtStatus', `⚠ cancel failed: ${e.message || e}`, 'err');
  }
}

function _vatFinish() {
  if (_vatEventSrc) { _vatEventSrc.close(); _vatEventSrc = null; }
  _vatEl('vllmAtRunBtn').style.display = '';
  _vatEl('vllmAtCancelBtn').style.display = 'none';
}

// ── Benchmark wizard (vllm bench serve) ─────────────────────────────────

const VBENCH_DEFAULTS = [
  { flag: '--dataset-name', value: 'random' },
  { flag: '--random-input-len', value: '1024' },
  { flag: '--random-output-len', value: '128' },
  { flag: '--num-prompts', value: '200' },
];

let _vbenchEventSrc = null;
let _vbenchSwitches = [];
let _vbenchResult = null;
let _vbenchModel = null;
window._vbenchData = window._vbenchData || {};

function _vbenchUrl(path) {
  // ?provider=vllm routes storage AND _withAgentParam's picker to vllm.
  const sep = path.includes('?') ? '&' : '?';
  return `${path}${sep}provider=vllm`;
}

async function loadVllmBenchData() {
  try {
    const r = await fetch(_vbenchUrl('/api/benchmark/results')).then(_jsonOrThrow);
    const map = {};
    for (const row of (r.results || [])) map[row.model_id] = row;
    window._vbenchData = map;
    if (typeof fetchVllmMetrics === 'function') fetchVllmMetrics();
  } catch (e) { /* offline manager: badges simply stay hidden */ }
}

function _vbenchRenderSwitches() {
  const host = _vatEl('vllmBenchSwitchList');
  if (!host) return;
  host.innerHTML = '';
  _vbenchSwitches.forEach((s, i) => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:4px;align-items:center;flex-wrap:nowrap;';
    const flag = document.createElement('input');
    flag.type = 'text'; flag.value = s.flag;
    flag.className = 'vbench-input';
    flag.style.cssText = 'width:150px;flex:0 0 auto;';
    flag.oninput = () => { _vbenchSwitches[i].flag = flag.value; };
    const val = document.createElement('input');
    val.type = 'text'; val.value = s.value;
    val.className = 'vbench-input';
    val.style.cssText = 'flex:1;min-width:0;';
    val.oninput = () => { _vbenchSwitches[i].value = val.value; };
    const rm = document.createElement('button');
    rm.className = 'btn btn-gray-muted-gradient btn-sm';
    rm.style.flex = '0 0 auto';
    rm.textContent = '✕';
    rm.onclick = () => { _vbenchSwitches.splice(i, 1); _vbenchRenderSwitches(); };
    row.append(flag, val, rm);
    host.appendChild(row);
  });
}

function vllmBenchAddSwitch() {
  const flag = (_vatEl('vllmBenchAddFlag').value || '').trim();
  if (!flag) return;
  _vbenchSwitches.push({ flag, value: (_vatEl('vllmBenchAddVal').value || '').trim() });
  _vatEl('vllmBenchAddFlag').value = '';
  _vatEl('vllmBenchAddVal').value = '';
  _vbenchRenderSwitches();
}

async function _vbenchPreflight() {
  const msg = _vatEl('vllmBenchPreflightMsg');
  const startBtn = _vatEl('vllmBenchStartBtn');
  const runBtn = _vatEl('vllmBenchRunBtn');
  msg.textContent = 'Checking vLLM server…';
  startBtn.style.display = 'none';
  try {
    const d = await fetch('/api/vllm/metrics').then(_jsonOrThrow);
    const v = d.vllm || {};
    if (d.agent_online && v.state === 'running') {
      _vbenchModel = v.model || null;
      msg.textContent = `Server running — model: ${_vbenchModel || '(unknown)'}`;
      runBtn.disabled = false;
      return;
    }
    msg.textContent = d.agent_online
      ? '⚠ vLLM server is not running — the benchmark needs a live server.'
      : '⚠ vLLM agent offline.';
    startBtn.style.display = d.agent_online ? '' : 'none';
    runBtn.disabled = true;
  } catch (e) {
    msg.textContent = `⚠ ${e.message || e}`;
    runBtn.disabled = true;
  }
}

async function vllmBenchStartServer() {
  const btn = _vatEl('vllmBenchStartBtn');
  btn.disabled = true; btn.textContent = '…';
  try { await fetch('/api/vllm/server/start', { method: 'POST' }); } catch (e) { /* poll below reports it */ }
  const until = Date.now() + 90000;
  while (Date.now() < until
         && _vatEl('vllmBenchOverlay').classList.contains('open')) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const d = await fetch('/api/vllm/metrics').then(_jsonOrThrow);
      if ((d.vllm || {}).state === 'running') break;
    } catch (e) { /* keep polling until deadline */ }
  }
  btn.disabled = false; btn.textContent = '▶ Start vLLM';
  _vbenchPreflight();
}

function openVllmBench() {
  _vatEl('vllmBenchOverlay').classList.add('open');
  _vatEl('vllmBenchResults').innerHTML = '';
  _wizRawReset('vllmBenchRawLog', 'vllmBenchRawCount');
  _vbenchResult = null;
  _wizStatus('vllmBenchStatus', '');
  _vatEl('vllmBenchRunBtn').style.display = '';
  _vatEl('vllmBenchCancelBtn').style.display = 'none';
  if (!_vbenchSwitches.length) _vbenchSwitches = VBENCH_DEFAULTS.map(s => ({ ...s }));
  _vbenchRenderSwitches();
  _vbenchPreflight();
  loadVllmBenchData();
}

function closeVllmBench() {
  _vatEl('vllmBenchOverlay').classList.remove('open');
  if (_vbenchEventSrc) cancelVllmBench();
  _vbenchFinish();
}

async function runVllmBench() {
  const switches = _vbenchSwitches
    .map(s => ({ flag: String(s.flag || '').trim(), value: String(s.value || '').trim() }))
    .filter(s => s.flag);
  _vatEl('vllmBenchResults').innerHTML = '';
  _vatEl('vllmBenchRunBtn').style.display = 'none';
  _vatEl('vllmBenchCancelBtn').style.display = '';
  _wizStatus('vllmBenchStatus', 'Starting…');
  let r;
  try {
    r = await fetch('/api/vllm/bench/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: _vbenchModel, switches }),
    }).then(_jsonOrThrow);
  } catch (e) { r = { ok: false, error: e.message || String(e) }; }
  if (!r.ok) {
    _wizStatus('vllmBenchStatus', `⚠ ${r.error || 'run failed'}`, 'err');
    _vbenchFinish();
    return;
  }
  _vbenchEventSrc = new EventSource(window._withAgentParam('/api/vllm/bench/stream'));
  _vbenchEventSrc.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    _vbenchHandleEvent(msg);
  };
  _vbenchEventSrc.onerror = () => {
    if (_vbenchEventSrc && _vbenchEventSrc.readyState === EventSource.CLOSED) {
      _wizStatus('vllmBenchStatus', '⚠ progress stream closed', 'err');
      _vbenchFinish();
    }
  };
}

function _vbenchHandleEvent(msg) {
  switch (msg.type) {
    case 'keepalive': return;
    case 'model_start':
      _wizStatus('vllmBenchStatus', `Benchmarking ${msg.model}…`);
      _wizRawAppend('vllmBenchRawLog', 'vllmBenchRawCount', `$ ${msg.cmd}`);
      return;
    case 'line':
      _wizRawAppend('vllmBenchRawLog', 'vllmBenchRawCount', msg.text);
      return;
    case 'result':
      _vbenchResult = msg;
      _vbenchRenderResult(msg);
      return;
    case 'model_done':
      if (!msg.ok && !msg.cancelled) {
        _vatEl('vllmBenchResults').innerHTML =
          `<div class="at-result-card" style="border-left-color:var(--crit);"><span style="color:var(--crit);">✕ ${_esc(msg.error || 'benchmark failed')}</span></div>`;
      }
      return;
    case 'done':
      _wizStatus('vllmBenchStatus', msg.cancelled ? 'Cancelled.' : (msg.ok ? 'Done.' : 'Failed.'),
                       msg.ok ? '' : 'err');
      _vbenchFinish();
      return;
  }
}


function _vbenchFmt(x, digits = 1) {
  return (typeof x === 'number' && isFinite(x)) ? x.toFixed(digits) : '—';
}

function _vbenchRenderResult(msg) {
  const m = msg.extra || {};
  const saved = window._vbenchData[msg.model_id];
  _vatEl('vllmBenchResults').innerHTML = `
    <div class="at-result-card">
      <div class="at-result-head"><span class="at-result-model">${_esc(msg.model_id)}</span></div>
      <div class="at-result-grid">
        ${_wizStat('Requests', _vbenchFmt(m.request_throughput, 2), 'req/s')}
        ${_wizStat('Output', _vbenchFmt(m.output_throughput), 'tok/s')}
        ${_wizStat('Total', _vbenchFmt(m.total_token_throughput), 'tok/s')}
        ${_wizStat('TTFT p50/p99', `${_vbenchFmt(m.median_ttft_ms, 0)}/${_vbenchFmt(m.p99_ttft_ms, 0)}`, 'ms')}
        ${_wizStat('TPOT p50/p99', `${_vbenchFmt(m.median_tpot_ms, 1)}/${_vbenchFmt(m.p99_tpot_ms, 1)}`, 'ms')}
        ${_wizStat('ITL p50/p99', `${_vbenchFmt(m.median_itl_ms, 1)}/${_vbenchFmt(m.p99_itl_ms, 1)}`, 'ms')}
      </div>
      <div class="at-result-actions">
        <button class="btn" onclick="saveVllmBench(this)">💾 Save</button>
        ${saved ? `<button class="btn" onclick="clearVllmBench(this)">✕ Clear saved</button>` : ''}
      </div>
    </div>`;
}

async function saveVllmBench(btn) {
  if (!_vbenchResult) return;
  const m = _vbenchResult.extra || {};
  btn.disabled = true; btn.textContent = '…';
  let r;
  try {
    r = await fetch(_vbenchUrl('/api/benchmark/store'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model_id: _vbenchResult.model_id,
        provider: 'vllm',
        avg_gen_tps: m.output_throughput ?? null,
        avg_pg_tps: m.total_token_throughput ?? null,
        avg_ppt_tps: null,
        bench_tool: 'vllm-bench-serve',
        switches: _vbenchResult.switches || [],
        extra_json: m,
      }),
    }).then(_jsonOrThrow);
  } catch (e) { r = { ok: false, error: e.message || String(e) }; }
  btn.disabled = false;
  btn.textContent = r.ok ? '✓ Saved' : `⚠ ${r.error || 'failed'}`;
  if (r.ok) loadVllmBenchData();
}

async function clearVllmBench(btn) {
  if (!_vbenchResult) return;
  btn.disabled = true;
  let r;
  try {
    r = await fetch(_vbenchUrl('/api/benchmark/results/' + encodeURIComponent(_vbenchResult.model_id)),
                    { method: 'DELETE' }).then(_jsonOrThrow);
  } catch (e) { r = { ok: false, error: e.message || String(e) }; }
  btn.disabled = false;
  btn.textContent = r.ok ? '✓ Cleared' : `⚠ ${r.error || 'failed'}`;
  if (r.ok) loadVllmBenchData();
}

async function cancelVllmBench() {
  _wizStatus('vllmBenchStatus', 'Cancelling…');
  try {
    await fetch(window._withAgentParam('/api/vllm/bench/cancel'), { method: 'POST' });
  } catch (e) {
    _wizStatus('vllmBenchStatus', `⚠ cancel failed: ${e.message || e}`, 'err');
  }
}

function _vbenchFinish() {
  if (_vbenchEventSrc) { _vbenchEventSrc.close(); _vbenchEventSrc = null; }
  _vatEl('vllmBenchRunBtn').style.display = '';
  _vatEl('vllmBenchCancelBtn').style.display = 'none';
}

loadVllmBenchData();
