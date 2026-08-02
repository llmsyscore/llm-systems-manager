// ===========================================================================
// GPU Report Card sub-tab (#468) — run, stream progress, render, share.
// Pure helpers live in js/lib/reportcard.js (window.RC).
// ===========================================================================
let _rcEventSrc  = null;
let _rcLastCard  = null;
let _rcTrendChart = null;
let _rcPreset    = null;
let _rcJobId     = null;
let _rcTick      = null;
let _rcCleanup   = null;
let _rcRunTarget = null;

function _rcEl(id) { return document.getElementById(id); }

function _rcLog(msg) {
  const box = _rcEl('rcProgress');
  if (!box) return;
  box.style.display = '';
  box.textContent += (box.textContent ? '\n' : '') + msg;
  box.scrollTop = box.scrollHeight;
}

function _rcNote(msg, warn) {
  const n = _rcEl('rcNote');
  if (!n) return;
  n.textContent = msg || '';
  n.classList.toggle('rc-warn', !!warn);
}

function _rcBusy(busy) {
  const btn = _rcEl('rcRunBtn');
  if (btn) { btn.disabled = busy; btn.textContent = busy ? 'Running…' : '▶ Run report card'; }
  const cancel = _rcEl('rcCancelBtn');
  if (cancel) cancel.style.display = busy ? '' : 'none';
  if (!busy) _rcJobId = null;
}

// Human-readable step names for the progress panel.
const RC_PHASE_TEXT = {
  resolving: 'Checking the model on this host',
  ready: 'Model ready',
  download: 'Starting download',
  downloading: 'Downloading model',
  download_progress: null,
  register: 'Registering the model with llama.cpp',
  restart: 'Restarting llama.cpp',
  waiting: 'Waiting for the model to come online',
  load: 'Loading the model',
  warmup: 'Warm-up pass (discarded)',
  unload: 'Unloading the model',
};

function _rcStatus(text, elapsed) {
  const el = _rcEl('rcStatus');
  if (!el) return;
  el.style.display = text ? '' : 'none';
  const secs = elapsed != null ? ` · ${Math.round(elapsed)}s` : '';
  el.textContent = text ? text + secs : '';
}

// Ticks the elapsed counter locally between SSE progress events (#491).
function _rcTickSet(text, elapsed) {
  const timer = _rcTick?.timer || setInterval(() => {
    if (!_rcTick) return;
    _rcStatus(_rcTick.text, _rcTick.base + (Date.now() - _rcTick.at) / 1000);
  }, 1000);
  _rcTick = {text, base: elapsed != null ? elapsed : 0, at: Date.now(), timer};
  _rcStatus(text, _rcTick.base);
}

function _rcTickStop() {
  if (_rcTick?.timer) clearInterval(_rcTick.timer);
  _rcTick = null;
}

// Populate the agent picker from the same enumeration the other tabs use.
function rcLoadAgents() {
  const provider = _rcEl('rcProvider')?.value || 'llama';
  const sel = _rcEl('rcAgent');
  if (!sel) return;
  fetch('/api/agents/list-by-provider').then(r => r.json()).then(d => {
    const list = d[provider] || [];
    sel.replaceChildren();
    list.forEach(a => {
      const o = document.createElement('option');
      o.value = a.agent_id;
      o.textContent = a.hostname + (a.is_default ? ' (default)' : '');
      sel.appendChild(o);
    });
    if (!list.length) {
      const o = document.createElement('option');
      o.value = '';
      o.textContent = 'no ' + (RC.PROVIDER_LABEL[provider] || provider) + ' agent';
      sel.appendChild(o);
    }
    rcLoadLatest();
  }).catch(() => {});
}

function rcLoadPreset() {
  fetch('/api/reportcard/preset').then(r => r.json()).then(d => {
    _rcPreset = d;
    const sel = _rcEl('rcModelKey');
    if (sel && !sel.options.length) {
      (d.models || []).forEach(m => {
        const o = document.createElement('option');
        o.value = m.key;
        o.textContent = m.label;
        sel.appendChild(o);
      });
    }
    const price = _rcEl('rcPrice');
    if (price && !price.value) price.value = d.price_kwh;
  }).catch(() => {});
}

function rcOnModeChange() {
  const custom = _rcEl('rcMode')?.value === 'custom';
  const kf = _rcEl('rcModelKeyField');
  const cf = _rcEl('rcCustomModelField');
  if (kf) kf.style.display = custom ? 'none' : '';
  if (cf) cf.style.display = custom ? '' : 'none';
  _rcNote(custom
    ? 'Custom runs are for your own tracking — they are not comparable to leaderboard cards.'
    : '');
}

function rcRun(confirm) {
  const agent    = _rcEl('rcAgent')?.value || '';
  const provider = _rcEl('rcProvider')?.value || 'llama';
  const mode     = _rcEl('rcMode')?.value || 'standard';
  if (!agent) { _rcNote('Pick an agent first.', true); return; }
  const body = {agent, provider, mode,
                model_key: _rcEl('rcModelKey')?.value || 'small'};
  // price_kwh is sent only when the field parses to a finite number.
  const price = parseFloat(_rcEl('rcPrice')?.value);
  if (Number.isFinite(price)) body.price_kwh = price;
  if (mode === 'custom') body.model = (_rcEl('rcCustomModel')?.value || '').trim();
  if (confirm === 'vllm') body.confirm_vllm = true;
  if (confirm === 'download') body.confirm_download = true;

  _rcBusy(true);
  const box = _rcEl('rcProgress');
  if (box) { box.textContent = ''; box.style.display = 'none'; }
  _rcNote('');
  rcCleanupKeep();
  fetch('/api/reportcard/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json().then(d => ({ok: r.ok, d}))).then(({ok, d}) => {
    if (!ok) { _rcBusy(false); _rcNote(d.error || 'Run failed.', true); return; }
    if (d.status === 'needs_confirm') {
      _rcBusy(false); rcShowVllmConfirm(d); return;
    }
    if (d.status === 'needs_download') {
      _rcBusy(false); rcShowDownloadConfirm(d); return;
    }
    _rcJobId = d.job_id;
    _rcRunTarget = {agent, provider};
    _rcTickSet('Starting…', 0);
    _rcLog('run started');
    rcStream(d.job_id);
  }).catch(e => { _rcBusy(false); _rcNote('Run failed: ' + e, true); });
}

function rcShowVllmConfirm(d) {
  const wrap = _rcEl('rcConfirm');
  if (!wrap) return;
  const served = _rcEl('rcConfirmServed');
  const ref = _rcEl('rcConfirmRef');
  if (served) served.textContent = d.model || 'unknown';
  if (ref) ref.textContent = d.reference || 'the reference model';
  wrap.style.display = '';
}

function rcConfirmCancel() {
  const wrap = _rcEl('rcConfirm');
  if (wrap) wrap.style.display = 'none';
}

function rcConfirmProceed() {
  rcConfirmCancel();
  rcRun('vllm');
}

// Renders the download prompt: target model, size, and restart cost.
function rcShowDownloadConfirm(d) {
  const wrap = _rcEl('rcDownload');
  if (!wrap) return;
  const size = d.approx_gb ? `~${d.approx_gb} GB` : 'the reference model';
  const tail = d.restarts
    ? ', register it with llama.cpp, and restart llama.cpp'
    : '';
  const msg = _rcEl('rcDownloadMsg');
  if (msg) {
    msg.textContent = `${d.model} is not installed on this host. `
      + `Download ${size}${tail}, then run the benchmark?`;
  }
  wrap.style.display = '';
}

function rcDownloadCancel() {
  const wrap = _rcEl('rcDownload');
  if (wrap) wrap.style.display = 'none';
}

function rcDownloadProceed() {
  rcDownloadCancel();
  rcRun('download');
}

function rcCancelRun() {
  if (!_rcJobId) return;
  if (_rcTick) _rcTick.text = 'Cancelling…';
  _rcStatus('Cancelling…',
            _rcTick ? _rcTick.base + (Date.now() - _rcTick.at) / 1000 : null);
  fetch('/api/reportcard/cancel/' + encodeURIComponent(_rcJobId),
        {method: 'POST'}).catch(() => {});
}

function _rcCloseStream() {
  if (_rcEventSrc) { _rcEventSrc.close(); _rcEventSrc = null; }
}

function rcStream(jobId) {
  _rcCloseStream();
  _rcEventSrc = new EventSource('/api/reportcard/stream/' + encodeURIComponent(jobId));
  _rcEventSrc.onmessage = ev => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.event === 'progress') {
      let text;
      if (d.phase === 'rep') {
        text = `Benchmarking — repetition ${d.n} of ${d.of}`;
      } else if (d.phase === 'download_progress') {
        text = 'Downloading — ' + (d.text || '');
      } else if (d.phase === 'ready') {
        text = d.status === 'ready' ? `Model ready — ${d.model || ''}`
                                    : `Model check: ${d.status}`;
      } else {
        text = RC_PHASE_TEXT[d.phase] || d.phase;
      }
      _rcTickSet(text, d.elapsed_s);
      _rcLog(text);
    }
    if (d.event === 'error') {
      _rcLog('error: ' + d.error);
      _rcStatus('');
      _rcNote(d.error, true);
      _rcBusy(false);
      rcStopStream();
    }
    if (d.event === 'cancelled') {
      _rcLog('cancelled');
      _rcStatus('');
      _rcNote('Run cancelled.');
      _rcBusy(false);
      rcStopStream();
    }
    if (d.event === 'done') {
      _rcLog('done');
      _rcStatus('');
      _rcBusy(false);
      rcStopStream();
      rcRenderCard(d.card);
      rcShowCleanup(d.cleanup);
    }
  };
  _rcEventSrc.onerror = () => {
    _rcLog('connection lost');
    _rcStatus('');
    _rcNote('Lost connection to the run — it may still be running. '
            + 'Reselect the host in a moment to see the result.', true);
    rcStopStream();
  };
}

// Abandons the run: closes the stream and re-enables the Run button.
// An explicit close fires no onerror, so the reset happens here.
function rcStopStream() {
  _rcTickStop();
  _rcCloseStream();
  _rcBusy(false);
}

// Post-run cleanup offer (#492): shown only when this run downloaded the
// reference model; the delete button appears only where the agent can purge.
function rcShowCleanup(c) {
  if (!c || !c.downloaded) return;
  const wrap = _rcEl('rcCleanup');
  if (!wrap) return;
  // Pin the run's own host/provider so a later picker change can't
  // redirect the delete to another agent.
  _rcCleanup = {...c, ...(_rcRunTarget || {})};
  const msg = _rcEl('rcCleanupMsg');
  if (msg) {
    msg.textContent = c.deletable
      ? 'The reference model downloaded for this run was unloaded after the '
        + 'bench. Delete it from the host to free the disk space?'
      : 'The reference model downloaded for this run was unloaded after the '
        + 'bench. It stays on disk — remove it in LM Studio if you don\'t '
        + 'want to keep it.';
  }
  const del = _rcEl('rcCleanupDeleteBtn');
  if (del) del.style.display = c.deletable ? '' : 'none';
  wrap.style.display = '';
}

function rcCleanupKeep() {
  const wrap = _rcEl('rcCleanup');
  if (wrap) wrap.style.display = 'none';
  _rcCleanup = null;
}

function rcCleanupDelete() {
  const c = _rcCleanup;
  rcCleanupKeep();
  if (!c) return;
  const agent = c.agent || _rcEl('rcAgent')?.value || '';
  const provider = c.provider || _rcEl('rcProvider')?.value || 'llama';
  _rcNote('Deleting the reference model…');
  fetch('/api/reportcard/delete-model', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({agent, provider, model_key: c.model_key}),
  }).then(r => r.json().then(d => ({ok: r.ok, d}))).then(({ok, d}) => {
    _rcNote(ok && d.ok ? 'Reference model deleted from the host.'
                       : (d.error || 'Delete failed.'), !(ok && d.ok));
  }).catch(e => _rcNote('Delete failed: ' + e, true));
}

function rcRenderCard(card) {
  _rcLastCard = card;
  const host = _rcEl('rcCardHost');
  if (!host || !card) return;
  host.replaceChildren();
  host.appendChild(RC.buildCard({...card.result, provider: card.provider,
                                 ts: card.ts,
                                 preset_version: card.preset_version}));
  const submit = _rcEl('rcSubmitBtn');
  const url = RC.submitUrl(card);
  if (submit) {
    submit.style.display = url ? '' : 'none';
    submit.onclick = () => window.open(url, '_blank', 'noopener');
  }
  const actions = _rcEl('rcActions');
  if (actions) actions.style.display = '';
  if (!url) {
    _rcNote(card.mode === 'custom'
      ? 'Custom run — kept locally, not eligible for the leaderboard.'
      : 'This run did not use the reference model, so it is not eligible for the leaderboard.');
  }
}

function rcLoadLatest() {
  const agent = _rcEl('rcAgent')?.value || '';
  const provider = _rcEl('rcProvider')?.value || 'llama';
  if (!agent) return;
  fetch(`/api/reportcard/latest?agent=${encodeURIComponent(agent)}`
        + `&provider=${encodeURIComponent(provider)}`)
    .then(r => r.json()).then(d => {
      if (d.card) { rcRenderCard(d.card); return; }
      // No history for this picker selection — clear any stale card.
      _rcLastCard = null;
      _rcEl('rcCardHost')?.replaceChildren();
      const actions = _rcEl('rcActions');
      if (actions) actions.style.display = 'none';
    })
    .catch(() => {});
}

function rcExportPng() {
  const el = _rcEl('rcCardHost')?.querySelector('.rc-card');
  if (!el) return;
  RC.exportPng(el).then(blob => {
    if (!blob) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'report-card.png';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }).catch(e => _rcNote('PNG export failed: ' + e, true));
}

function rcToggleTrends() {
  const wrap = _rcEl('rcTrends');
  if (!wrap) return;
  const showing = wrap.style.display !== 'none';
  wrap.style.display = showing ? 'none' : '';
  if (!showing) rcLoadTrends();
}

function rcLoadTrends() {
  const agent = _rcEl('rcAgent')?.value || '';
  const provider = _rcEl('rcProvider')?.value || 'llama';
  const model = _rcLastCard?.result?.model || '';
  if (!agent || !model) { _rcNote('Run a card first to see its trend.'); return; }
  fetch(`/api/reportcard/history?agent=${encodeURIComponent(agent)}`
        + `&provider=${encodeURIComponent(provider)}`
        + `&model=${encodeURIComponent(model)}`)
    .then(r => r.json()).then(d => rcDrawTrends(RC.trendSeries(d.cards || [])))
    .catch(() => {});
}

function rcDrawTrends(series) {
  const canvas = _rcEl('rcTrendChart');
  if (!canvas || typeof Chart === 'undefined') return;
  if (_rcTrendChart) _rcTrendChart.destroy();
  const line = (label, data, color, axis) => ({
    label, data, borderColor: color, backgroundColor: color,
    borderWidth: 2, pointRadius: 2, tension: 0.25, yAxisID: axis});
  _rcTrendChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {labels: series.labels, datasets: [
      line('generation tok/s', series.gen, cssVar('--accent'), 'y'),
      line('prefill tok/s', series.prefill, cssVar('--fg-muted'), 'y1'),
      line('tokens/joule', series.tpj, cssVar('--accent-2'), 'y2'),
    ]},
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: {mode: 'index', intersect: false},
      scales: {
        x: {type: 'time', ticks: {color: cssVar('--fg-muted')},
            grid: {color: cssVar('--border-soft')}},
        y:  {position: 'left', ticks: {color: cssVar('--fg-muted')},
             grid: {color: cssVar('--border-soft')}},
        y1: {position: 'right', ticks: {color: cssVar('--fg-dim')},
             grid: {display: false}},
        y2: {display: false},
      },
      plugins: {legend: {labels: {color: cssVar('--fg-muted')}}},
    },
  });
}

function rcOnProviderChange() {
  rcLoadAgents();
}

function initReportCard() {
  if (!_rcEl('rcRunBtn')) return;
  rcLoadPreset();
  rcLoadAgents();
  rcOnModeChange();
}
