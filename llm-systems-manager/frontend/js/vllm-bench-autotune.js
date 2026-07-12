// vLLM wizards: Auto-Tune max-model-len (#356). Bench wizard lands in #357.
// Mirrors bench-autotune.js: overlay + EventSource over the manager proxy.

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
  const div = document.createElement('div');
  div.textContent = text;
  box.appendChild(div);
  while (box.childNodes.length > 2000) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

function _vatProgress(html) {
  const el = _vatEl('vllmAtProgress');
  if (el) el.insertAdjacentHTML('beforeend', html);
}

function _vatArgsWithMaxLen(args, value) {
  const out = (args || []).filter(a => a.flag !== '--max-model-len')
                          .map(a => Object.assign({}, a));
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
  const cur = _vatEl('vllmAtCurrent');
  cur.textContent = 'Reading server config…';
  try {
    const r = await fetch(window._withAgentParam('/api/vllm/server/svcconfig'))
      .then(x => x.json());
    if (!r.ok) throw new Error(r.error || 'svcconfig read failed');
    _vatOrig = { binary: r.binary, args: r.args || [] };
    const ml = (r.args || []).find(a => a.flag === '--max-model-len');
    const parts = (r.binary || '').split(/\s+/);
    const model = parts.length > 2 ? parts[2] : '(unknown)';
    cur.textContent = `Model: ${model} — current --max-model-len: ${ml && ml.value ? ml.value : '(model default)'}`;
    _vatEl('vllmAtRunBtn').disabled = false;
  } catch (e) {
    cur.textContent = `⚠ ${e.message || e} — is the vLLM agent online?`;
    _vatEl('vllmAtRunBtn').disabled = true;
  }
}

function closeVllmAutotune() {
  _vatEl('vllmAtOverlay').classList.remove('open');
  if (_vatEventSrc) { _vatEventSrc.close(); _vatEventSrc = null; }
}

async function runVllmAutotune() {
  if (!_vatOrig) return;
  const body = {
    probe_len: parseInt(_vatEl('vllmAtProbeLen').value, 10) || 4096,
    concurrency: parseFloat(_vatEl('vllmAtConc').value) || 1.0,
    kv_fraction: (parseFloat(_vatEl('vllmAtFrac').value) || 100) / 100,
    load_timeout_s: parseInt(_vatEl('vllmAtTimeout').value, 10) || 600,
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
    }).then(x => x.json());
  } catch (e) { r = { ok: false, error: String(e) }; }
  if (!r.ok) {
    _vatSetStatus(`⚠ ${r.error || 'run failed'}`, 'err');
    _vatFinish();
    return;
  }
  _vatEventSrc = new EventSource('/api/vllm/autotune/stream');
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

async function _vatPostSvcconfig(args, btn, okLabel) {
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  let r;
  try {
    r = await fetch(window._withAgentParam('/api/vllm/server/svcconfig'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ binary: _vatOrig.binary, args, restart: true }),
    }).then(x => x.json());
  } catch (e) { r = { ok: false, error: String(e) }; }
  if (btn) {
    btn.disabled = false;
    btn.textContent = r.ok ? okLabel : `⚠ ${r.error || 'failed'}`;
  }
  if (r.ok && typeof fetchVllmMetrics === 'function') fetchVllmMetrics();
}

function vllmAtApply(btn) {
  if (!_vatOrig || !_vatResult) return;
  _vatPostSvcconfig(_vatArgsWithMaxLen(_vatOrig.args, _vatResult.max_model_len),
                    btn, '✓ Applied');
}

function vllmAtRevert(btn) {
  if (!_vatOrig) return;
  _vatPostSvcconfig(_vatOrig.args.map(a => Object.assign({}, a)), btn, '✓ Reverted');
}

async function cancelVllmAutotune() {
  try {
    await fetch(window._withAgentParam('/api/vllm/autotune/cancel'), { method: 'POST' });
  } catch (e) { /* stream close surfaces the outcome */ }
  _vatSetStatus('Cancelling…');
}

function _vatFinish() {
  if (_vatEventSrc) { _vatEventSrc.close(); _vatEventSrc = null; }
  _vatEl('vllmAtRunBtn').style.display = '';
  _vatEl('vllmAtCancelBtn').style.display = 'none';
}
