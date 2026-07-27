// ===========================================================================
// GPU Report Card sub-tab (#468) — run, stream progress, render, share.
// Pure helpers live in js/lib/reportcard.js (window.RC).
// ===========================================================================
let _rcEventSrc  = null;
let _rcLastCard  = null;
let _rcTrendChart = null;
let _rcPreset    = null;

function _rcEl(id) { return document.getElementById(id); }

function _rcLog(msg) {
  const box = _rcEl('rcProgress');
  if (!box) return;
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

function rcRun(confirmVllm) {
  const agent    = _rcEl('rcAgent')?.value || '';
  const provider = _rcEl('rcProvider')?.value || 'llama';
  const mode     = _rcEl('rcMode')?.value || 'standard';
  if (!agent) { _rcNote('Pick an agent first.', true); return; }
  const body = {agent, provider, mode,
                model_key: _rcEl('rcModelKey')?.value || 'small',
                price_kwh: parseFloat(_rcEl('rcPrice')?.value) || undefined};
  if (mode === 'custom') body.model = (_rcEl('rcCustomModel')?.value || '').trim();
  if (confirmVllm) body.confirm_vllm = true;

  _rcBusy(true);
  const box = _rcEl('rcProgress');
  if (box) box.textContent = '';
  _rcNote('');
  fetch('/api/reportcard/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json().then(d => ({ok: r.ok, d}))).then(({ok, d}) => {
    if (!ok) { _rcBusy(false); _rcNote(d.error || 'Run failed.', true); return; }
    if (d.status === 'needs_confirm') {
      _rcBusy(false);
      rcShowVllmConfirm(d);
      return;
    }
    _rcLog('run started');
    rcStream(d.job_id);
  }).catch(e => { _rcBusy(false); _rcNote('Run failed: ' + e, true); });
}

// vLLM is benched as-served; the manager never restarts it.
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
  rcRun(true);
}

function rcStream(jobId) {
  rcStopStream();
  _rcEventSrc = new EventSource('/api/reportcard/stream/' + encodeURIComponent(jobId));
  _rcEventSrc.onmessage = ev => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.event === 'phase')    _rcLog('ready: ' + (d.status || '') + ' ' + (d.model || ''));
    if (d.event === 'progress') {
      _rcLog(d.phase === 'warmup' ? 'warmup (discarded)'
                                  : `repetition ${d.n}/${d.of}`);
    }
    if (d.event === 'error') {
      _rcLog('error: ' + d.error);
      _rcNote(d.error, true);
      _rcBusy(false);
      rcStopStream();
    }
    if (d.event === 'done') {
      _rcLog('done');
      _rcBusy(false);
      rcStopStream();
      rcRenderCard(d.card);
    }
  };
  _rcEventSrc.onerror = () => { _rcBusy(false); rcStopStream(); };
}

function rcStopStream() {
  if (_rcEventSrc) { _rcEventSrc.close(); _rcEventSrc = null; }
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
    .then(r => r.json()).then(d => { if (d.card) rcRenderCard(d.card); })
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
