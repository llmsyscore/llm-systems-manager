// vLLM Auto-Tune max-model-len wizard — overlay + EventSource client
// for the /api/vllm/autotune/* routes; mirrors bench-autotune.js.

let _vatEventSrc = null;
let _vatOrig = null;        // {binary, args} from svcconfig GET
let _vatResult = null;      // last model_done payload
let _vatRawCount = 0;

function _vatEl(id) { return document.getElementById(id); }

function _vatSetStatus(txt, cls) {
  const el = _vatEl('vllmAtStatus');
  if (el) { el.textContent = txt; el.className = 'sub ' + (cls || ''); }
}

function _vatRawAppend(text) {
  const box = _vatEl('vllmAtRawLog');
  if (!box) return;
  _vatRawCount++;
  const cnt = _vatEl('vllmAtRawCount');
  if (cnt) cnt.textContent = String(_vatRawCount);
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const div = document.createElement('div');
  div.textContent = text;
  box.appendChild(div);
  while (box.childNodes.length > 2000) box.removeChild(box.firstChild);
  if (nearBottom) box.scrollTop = box.scrollHeight;
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
  _vatEl('vllmAtRawLog').innerHTML = '';
  _vatRawCount = 0;
  _vatSetStatus('');
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
  _vatSetStatus('Starting…');
  let r;
  try {
    r = await fetch(window._withAgentParam('/api/vllm/autotune/run'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(_jsonOrThrow);
  } catch (e) { r = { ok: false, error: e.message || String(e) }; }
  if (!r.ok) {
    _vatSetStatus(`⚠ ${r.error || 'run failed'}`, 'err');
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
      _vatSetStatus('⚠ progress stream closed', 'err');
      _vatFinish();
    }
  };
}

function _vatHandleEvent(msg) {
  switch (msg.type) {
    case 'keepalive': return;
    case 'model_start':
      _vatSetStatus(`Tuning ${msg.model || msg.unit}…`);
      _vatProgress(`<div class="sub">Unit <b>${_esc(msg.unit)}</b> — original --max-model-len: ${msg.original_max_len ?? '(model default)'}</div>`);
      return;
    case 'step_start':
      _vatProgress(`<div class="sub">▸ ${_esc(msg.step)}${msg.max_model_len != null ? ' @ --max-model-len ' + msg.max_model_len : ''}</div>`);
      return;
    case 'loading_progress':
      _vatSetStatus(`${msg.step}: loading… ${Math.round(msg.elapsed_s)}s / ${msg.timeout_s}s`);
      return;
    case 'line':
      _vatRawAppend(msg.text);
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
      _vatSetStatus(msg.cancelled ? 'Cancelled.' : (msg.ok ? 'Done.' : 'Failed.'),
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
  const stat = (k, v, u) => `<div class="at-stat"><div class="at-stat-k">${k}</div>
      <div class="at-stat-v">${v}<span class="at-stat-u">${u || ''}</span></div></div>`;
  const conc = msg.max_concurrency_x != null ? String(msg.max_concurrency_x) : '—';
  const actions = msg.applied
    ? `<span style="color:var(--ok);font-size:0.85em;">✓ Applied to ExecStart</span>
       <button class="btn" onclick="vllmAtRevert(this)">↩ Revert to ${msg.original_max_len ?? 'model default'}</button>`
    : `<button class="btn" onclick="vllmAtApply(this)">💾 Apply ${Number(msg.max_model_len).toLocaleString()}</button>`;
  el.innerHTML = `
    <div class="at-result-card">
      <div class="at-result-head"><span class="at-result-model">--max-model-len</span></div>
      <div class="at-result-grid">
        ${stat('Recommended', Number(msg.max_model_len).toLocaleString(), 'tok')}
        ${stat('KV capacity', Number(msg.kv_tokens).toLocaleString(), 'tok')}
        ${stat('Concurrency', conc, '×')}
        ${stat('Previous', msg.original_max_len != null ? Number(msg.original_max_len).toLocaleString() : 'default', msg.original_max_len != null ? 'tok' : '')}
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
  _vatSetStatus('Cancelling…');
  try {
    await fetch(window._withAgentParam('/api/vllm/autotune/cancel'), { method: 'POST' });
  } catch (e) {
    _vatSetStatus(`⚠ cancel failed: ${e.message || e}`, 'err');
  }
}

function _vatFinish() {
  if (_vatEventSrc) { _vatEventSrc.close(); _vatEventSrc = null; }
  _vatEl('vllmAtRunBtn').style.display = '';
  _vatEl('vllmAtCancelBtn').style.display = 'none';
}
