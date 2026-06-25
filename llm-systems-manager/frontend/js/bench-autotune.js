// ===========================================================================
// Benchmark (llama-bench / llama-batched-bench)
// ===========================================================================
let _benchEventSrc      = null;
let _benchChart         = null;
let _benchData          = {};   // model_id → stored results (DB)
let _benchSwitches      = [];   // current editable switch list
let _benchModelDatasets = {};   // model_id → dataset index pair
let _benchRawRows       = [];   // all result rows for axis re-render: {model_id, ts, seq, gen_tps, ppt_tps, n_prompt, n_gen, n_depth, n_batch, n_ubatch, avg_ts}

const BENCH_COLOR_PAIRS = [
  {gen: '#5a8fc2', ppt: '#c28a3a'},   // steel blue / warm amber
  {gen: '#a05ac2', ppt: '#3aaa7a'},   // muted purple / teal
  {gen: '#c25a6a', ppt: '#5aaac2'},   // dusty rose / sky
  {gen: '#7a9a3a', ppt: '#c27a3a'},   // olive / terra
  {gen: '#3a7ac2', ppt: '#c25a9a'},   // cobalt / mauve
];

function _mkBenchChart(id, xAxisType) {
  const ctx = document.getElementById(id)?.getContext('2d');
  if (!ctx) return null;

  const TICK_COLOR  = cssVar('--fg-muted');
  const LABEL_COLOR = 'var(--fg)';
  const GRID_COLOR  = cssVar('--border-soft');
  const TICK_SZ     = 12;
  const TITLE_SZ    = 12;

  // Bar chart uses a category x-axis.
  const xScale = { type: 'category',
    ticks: { color: TICK_COLOR, font: { size: TICK_SZ }, maxRotation: 35 },
    grid: { color: GRID_COLOR },
    title: { display: !!xAxisType && xAxisType !== 'seq',
             text: (xAxisType || 'sequence'), color: LABEL_COLOR, font: { size: TITLE_SZ } } };

  const yAxisSel = document.getElementById('benchYAxis')?.value || 'avg_ts';
  const yLabel = yAxisSel === 'ms_tok' ? 'ms/tok' : yAxisSel === 'avg_ts' ? 't/s' : yAxisSel;

  return new Chart(ctx, {
    type: 'bar',
    data: { datasets: [] },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: true, position: 'top',
          labels: { font: { size: 12 }, color: cssVar('--fg-muted'), boxWidth: 12, padding: 10 } },
        tooltip: {
          backgroundColor: cssVar('--bg-card'),
          borderColor: cssVar('--border'), borderWidth: 1,
          titleColor: cssVar('--fg'), bodyColor: cssVar('--fg-muted'),
          padding: 10, cornerRadius: 6,
          titleFont: { size: 12 }, bodyFont: { size: 12 },
          callbacks: {
            title: function(items) {
              if (!items.length) return '';
              const xSel = document.getElementById('benchXAxis')?.value || 'seq';
              // raw.x is the value we pushed (numeric string for the category
              // axis); parsed.x is the category index, useless for real values.
              const xVal = items[0].raw?.x ?? items[0].parsed.x;
              if (xSel === 'seq')  return 'Test #' + xVal;
              const short = _BENCH_AXIS_SHORT[xSel] || xSel;
              return short + ': ' + xVal;
            },
            label: function(ctx) {
              const yVal = ctx.parsed.y;
              const yType = document.getElementById('benchYAxis')?.value || 'avg_ts';
              const suffix = yType === 'ms_tok' ? ' ms/tok' : yType === 'avg_ts' ? ' t/s' : '';
              return '  ' + ctx.dataset.label + ':  ' + yVal.toFixed(2) + suffix;
            }
          }
        }
      },
      scales: {
        x: xScale,
        y: { beginAtZero: true,
             ticks: { color: TICK_COLOR, font: { size: TICK_SZ } },
             grid: { color: GRID_COLOR },
             title: { display: true, text: yLabel, color: LABEL_COLOR, font: { size: TITLE_SZ } } }
      }
    }
  });
}

function _benchGetX(row) {
  const axis = document.getElementById('benchXAxis')?.value || 'seq';
  if (axis === 'seq')  return row.seq;
  return row[axis] ?? 0;
}

function _benchGetY(row) {
  const axis = document.getElementById('benchYAxis')?.value || 'avg_ts';
  if (axis === 'ms_tok') { const ts = row.avg_ts || 0; return ts > 0 ? 1000 / ts : 0; }
  return row[axis] ?? 0;
}

function _rechartBench() {
  const xAxis = document.getElementById('benchXAxis')?.value || 'seq';
  // Preserve dataset configs (labels + colors) but clear data
  const dsConfigs = (_benchChart?.data.datasets || []).map(d => ({...d, data: []}));
  if (_benchChart) { try { _benchChart.destroy(); } catch(_) {} _benchChart = null; }
  _benchChart = _mkBenchChart('benchChart', xAxis);
  if (!_benchChart) return;
  dsConfigs.forEach(d => _benchChart.data.datasets.push(d));
  // Re-plot all stored rows
  _benchRawRows.forEach(r => {
    const dsIdx = _benchModelDatasets[r.model_id];
    if (dsIdx === undefined) return;
    let x = _benchGetX(r);
    const y = _benchGetY(r);
    x = String(x);   // bar chart needs category (string) x values
    if (r.n_gen > 0) {
      _benchChart.data.datasets[dsIdx].data.push({x, y});
    } else if (r.n_prompt > 0) {
      _benchChart.data.datasets[dsIdx + 1].data.push({x, y});
    }
  });
  _benchChart.update('none');
}

// Human-readable label for an axis option. Keys are JSONL field names from
// llama-bench OR custom-switch names (with the leading dashes stripped).
// Falls back to the raw key when no translation is registered, so unknown
// custom switches still appear (just without a description).
const _BENCH_AXIS_LABELS = {
  // Synthetic axes
  time:        'Time (run order)',
  seq:         'Sequence # (run order)',
  // llama-bench JSONL fields
  n_prompt:    'Prompt tokens (n_prompt)',
  n_gen:       'Generated tokens (n_gen)',
  n_depth:     'Depth (n_depth)',
  n_batch:     'Batch size (n_batch)',
  n_ubatch:    'Micro-batch size (n_ubatch)',
  n_threads:   'CPU threads',
  n_gpu_layers:'GPU layers offloaded',
  flash_attn:  'Flash-attention enabled',
  no_mmap:     'No-mmap enabled',
  avg_ts:      'Avg tokens/sec',
  stddev_ts:   'Std-dev tokens/sec',
  // Custom-switch shortcuts the user types in the switches panel
  t:           'CPU threads (-t)',
  ngl:         'GPU layers (-ngl)',
  fa:          'Flash-attention (-fa)',
  ctk:         'KV cache type — K (-ctk)',
  ctv:         'KV cache type — V (-ctv)',
  ncmoe:       'Non-cache MoE experts (-ncmoe)',
  mmp:         'No-mmap (-mmp)',
  c:           'Context size (-c)',
  p:           'Prompt batch (p)',
  n:           'Gen tokens (n)',
  d:           'Depth (d)',
  b:           'Batch (b)',
  ub:          'Micro-batch (ub)',
};
function _benchAxisLabel(key) {
  return _BENCH_AXIS_LABELS[key] || key;
}

// Maps JSONL field names to the short flag used in llama-bench CLI / footer
// (e.g. n_gen → "n" so a tooltip on the gen-tokens axis shows "n: 512"
// matching the footer line "n: 512" rather than the JSONL field name).
const _BENCH_AXIS_SHORT = {
  n_prompt:     'p',
  n_gen:        'n',
  n_depth:      'd',
  n_batch:      'b',
  n_ubatch:     'ub',
  n_threads:    't',
  n_gpu_layers: 'ngl',
  flash_attn:   'fa',
  no_mmap:      'mmp',
  type_k:       'ctk',
  type_v:       'ctv',
};

// Dynamically populate axis selects from numeric keys found in raw rows
// AND from the user's custom switch flags so a sweep over e.g. --threads
// can be plotted even before any results have arrived for the current run.
function _updateBenchAxisOpts() {
  const xSel = document.getElementById('benchXAxis');
  const ySel = document.getElementById('benchYAxis');
  if (!xSel || !ySel) return;
  const curX = xSel.value;
  const curY = ySel.value;

  const { xOptions, yOptions, defaultX, defaultY } =
    computeBenchAxisOptions(_benchRawRows, _benchSwitches, _benchAxisLabel);

  const fill = (sel, opts, cur, dflt) => {
    sel.innerHTML = '';
    opts.forEach(({ v, t }) => {
      const opt = document.createElement('option');
      opt.value = v;
      opt.textContent = t;
      sel.appendChild(opt);
    });
    sel.value = opts.some(o => o.v === cur) ? cur : dflt;
  };
  fill(xSel, xOptions, curX, defaultX);
  fill(ySel, yOptions, curY, defaultY);
}

function _benchAddModelDatasets(modelId) {
  if (!_benchChart) return;
  if (_benchModelDatasets[modelId] !== undefined) return;
  const colorIdx = Object.keys(_benchModelDatasets).length % BENCH_COLOR_PAIRS.length;
  const colors = BENCH_COLOR_PAIRS[colorIdx];
  const shortName = modelId.split('/').pop() || modelId;
  _benchModelDatasets[modelId] = _benchChart.data.datasets.length;
  _benchChart.data.datasets.push(
    { label: shortName + ' ppt', data: [], borderColor: colors.ppt, backgroundColor: colors.ppt + '40',
      borderWidth: 1, pointRadius: 3, pointHoverRadius: 7, fill: false },
    { label: shortName + ' gen', data: [], borderColor: colors.gen, backgroundColor: colors.gen + '40',
      borderWidth: 1, pointRadius: 3, pointHoverRadius: 7, fill: false },
  );
  _benchChart.update('none');
}

// ---- Log helpers ----
function _benchLogClear() {
  const el = document.getElementById('benchLog');
  if (el) el.innerHTML = '';
}

function _benchLogAppend(html) {
  const el = document.getElementById('benchLog');
  if (!el) return;
  el.insertAdjacentHTML('beforeend', html);
  el.scrollTop = el.scrollHeight;
}

function _benchFormatLine(text) {
  if (!text) return '';
  const t = text.trim();
  if (!t.startsWith('{')) {
    // Plain text (stderr, command echo, etc.)
    return `<span class="bench-log-text">${_hEsc(t)}</span>`;
  }
  let obj;
  try { obj = JSON.parse(t); } catch(_) {
    return `<span class="bench-log-text">${_hEsc(t)}</span>`;
  }

  // Result row — has n_prompt / n_gen
  if (obj.n_prompt !== undefined || obj.n_gen !== undefined) {
    const nP = obj.n_prompt ?? 0, nG = obj.n_gen ?? 0, nD = obj.n_depth ?? 0;
    const nB = obj.n_batch ?? 0, nU = obj.n_ubatch ?? 0;
    const ts = Number(obj.avg_ts ?? 0);
    const sd = obj.stddev_ts != null ? ` <span style="color:var(--fg-faint)">±${Number(obj.stddev_ts).toFixed(1)}</span>` : '';
    let typeLabel, typeCls;
    if (nG > 0 && nP === 0)  { typeLabel = 'gen'; typeCls = 'gen'; }
    else if (nP > 0 && nG === 0) { typeLabel = 'ppt'; typeCls = 'ppt'; }
    else                     { typeLabel = 'pg';  typeCls = 'pg';  }
    // Always-visible llama-bench params
    const baseFields = [
      ['p', nP], ['n', nG], ['d', nD], ['b', nB], ['ub', nU],
    ];
    // Append every custom switch the user added so each line shows what
    // configuration produced this result. Reads JSONL first (canonical
    // value llama-bench observed) and falls back to the raw user-typed
    // value if the flag isn't echoed in the JSON output.
    const FLAG_TO_JSONL = {
      '-t': 'n_threads', '--threads': 'n_threads',
      '-ngl': 'n_gpu_layers', '--n-gpu-layers': 'n_gpu_layers',
      '-mmp': 'no_mmap', '--no-mmap': 'no_mmap',
      '-fa': 'flash_attn', '--flash-attn': 'flash_attn',
      '-ctk': 'type_k', '--cache-type-k': 'type_k',
      '-ctv': 'type_v', '--cache-type-v': 'type_v',
    };
    (typeof _benchSwitches !== 'undefined' ? _benchSwitches : []).forEach(sw => {
      if (!sw || !sw.flag) return;
      const flag = String(sw.flag).trim();
      const label = flag.replace(/^--?/, '');
      const jsonlKey = FLAG_TO_JSONL[flag] || label;
      let val = obj[jsonlKey];
      if (val === undefined || val === null || val === '') val = sw.value;
      if (val === undefined || val === null || val === '') return;
      // Avoid duplicating the always-on fields we already render
      if (['p', 'n', 'd', 'b', 'ub'].includes(label)) return;
      baseFields.push([label, val]);
    });
    const fields = baseFields.map(([k, v]) =>
      `<span class="bench-log-field"><span>${_hEsc(String(k))}:</span><b>${_hEsc(String(v))}</b></span>`
    ).join('');
    const yType = document.getElementById('benchYAxis')?.value;
    const dispVal = yType === 'ms_tok' ? (ts > 0 ? (1000/ts).toFixed(2) + ' ms/tok' : '—')
                                       : ts.toFixed(2) + ' t/s';
    return `<div class="bench-log-result">
      <span class="bench-log-type ${typeCls}">${typeLabel}</span>${fields}
      <span class="bench-log-tps">${dispVal}${sd}</span>
    </div>`;
  }

  // Build-info / header row — show selected key fields
  const show = ['model_type', 'model_size', 'n_gpu_layers', 'flash_attn', 'type_k', 'type_v', 'n_threads', 'build_commit'];
  const kvs = show
    .filter(k => obj[k] != null)
    .map(k => {
      let val = obj[k];
      if (k === 'model_size') val = (Number(val) / 1e9).toFixed(2) + ' GB';
      if (k === 'flash_attn') val = val ? 'on' : 'off';
      return `<span class="bench-log-info-kv"><b>${k}:</b> ${_hEsc(String(val))}</span>`;
    });
  if (!kvs.length) return '';   // skip empty / unknown JSON rows
  return `<div class="bench-log-info">${kvs.join('')}</div>`;
}

function _hEsc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _benchPushPoint(msg) {
  // Build raw row — capture all numeric fields from JSONL result for dynamic axis options
  const raw = { model_id: msg.model_id, ts: Date.now(), seq: _benchRawRows.length,
                gen_tps: msg.gen_tps, ppt_tps: msg.ppt_tps };
  Object.entries(msg).forEach(([k, v]) => {
    if (typeof v === 'number' && k !== 'gen_tps' && k !== 'ppt_tps') raw[k] = v;
  });
  _benchRawRows.push(raw);
  _updateBenchAxisOpts();

  if (!_benchChart) return;
  const dsIdx = _benchModelDatasets[msg.model_id];
  if (dsIdx === undefined) return;
  let x = _benchGetX(raw);
  const y = _benchGetY(raw);
  x = String(x);
  if ((msg.n_gen ?? 0) > 0) {
    _benchChart.data.datasets[dsIdx].data.push({x, y});
  } else if ((msg.n_prompt ?? 0) > 0) {
    _benchChart.data.datasets[dsIdx + 1].data.push({x, y});
  }
  _benchChart.update('none');
}

// default switch values for new benchmarks — used to populate UI on open
const BENCH_DEFAULTS = {
  'llama-bench': [
    {flag:'-ngl', value:'99'},
    {flag:'-fa',  value:'1'},
    {flag:'-pg',  value:'4096,256'},
    {flag:'-p',   value:'2048,8192'},
    {flag:'-n',   value:'512,1024'},
    {flag:'-d',   value:'0,8192,32768'},
    {flag:'-b',   value:'2048'},
    {flag:'-ub',  value:'512,1024,2048'},
    {flag:'-ctk', value:'f16'},
    {flag:'-ctv', value:'f16'},
    {flag:'-t',   value:'4,12'},
  ],
  'llama-batched-bench': [
    {flag:'-npp', value:'128,256,512'},
    {flag:'-ntg', value:'128,256'},
    {flag:'-npl', value:'1,2,3'},
    {flag:'-b',   value:'2048'},
    {flag:'-ub',  value:'2048'},
    {flag:'-ngl', value:'99'},
    {flag:'-fa',  value:'1'},
    {flag:'-t',   value:'4,12'},
  ],
};

// Load stored benchmark results for all models on startup, to show badges on model cards and have data ready on bench open
async function loadBenchmarkData() {
  try {
    const d = await fetch('/api/benchmark/results').then(r => r.json());
    _benchData = {};
    (d.results || []).forEach(r => { _benchData[r.model_id] = r; });
  } catch (e) {
    console.warn('loadBenchmarkData failed:', e);
    _benchData = {};
  }
}

async function openBench(modelId) {
  // Show overlay FIRST so the canvas has real pixel dimensions for Chart.js
  document.getElementById('benchOverlay').classList.add('open');

  // Lazy-init chart now that canvas is visible
  if (!_benchChart) {
    try { _benchChart = _mkBenchChart('benchChart'); }
    catch (e) { console.warn('benchChart init failed', e); }
  } else {
    try { _benchChart.resize(); } catch(_) {}
  }

  // Populate model checkboxes from /api/benchmark/models
  let models = [];
  try {
    const r = await fetch('/api/benchmark/models').then(r => r.json());
    models = r.models || [];
  } catch (e) { console.warn('benchmark/models failed', e); }

  const panel = document.getElementById('benchModelPanel');
  panel.innerHTML = '';
  models.forEach(m => {
    const item = document.createElement('div');
    item.className = 'bench-model-item';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = m;
    cb.id = 'benchcb_' + m.replace(/[^a-zA-Z0-9]/g, '_');
    if (m === modelId) cb.checked = true;
    cb.addEventListener('change', _updateBenchModelLabel);
    const lbl = document.createElement('label');
    lbl.htmlFor = cb.id;
    lbl.textContent = m;
    item.appendChild(cb);
    item.appendChild(lbl);
    panel.appendChild(item);
  });
  _updateBenchModelLabel();

  // Reset UI state
  switchBenchTab('llama-bench');
  _benchLogClear();
  _benchRenderPlaceholder();
  document.getElementById('benchStatus').textContent = 'idle';
  document.getElementById('benchStatus').classList.remove('running');
  document.getElementById('benchRunBtn').disabled = false;
  if (_benchChart) {
    _benchChart.data.datasets = [];
    _benchChart.update('none');
  }
  _benchModelDatasets = {};
  _benchRawRows = [];
  _benchSetChartIdle(true);
}

// Toggle dropdown panels in benchmark overlay (model select, switch edit, etc.)
function toggleBenchDrop(dropId) {
  const drop = document.getElementById(dropId);
  const dropPanel = drop.querySelector('.bench-drop-panel');
  const isOpen = dropPanel.classList.contains('open');
  // Close all bench dropdowns first
  document.querySelectorAll('.bench-drop-panel.open').forEach(p => p.classList.remove('open'));
  if (!isOpen) dropPanel.classList.add('open');
}

// Update the label of the model select dropdown based on how many models are checked
function _updateBenchModelLabel() {
  const checked = document.querySelectorAll('#benchModelPanel input[type=checkbox]:checked');
  const lbl = document.getElementById('benchModelLabel');
  if (!lbl) return;
  if (checked.length === 0) lbl.textContent = 'Select models…';
  else if (checked.length === 1) lbl.textContent = checked[0].value.split('/').pop() || checked[0].value;
  else lbl.textContent = checked.length + ' models selected';
}

// Update the label of the switch edit dropdown based on how many switches are defined
function _updateBenchSwitchLabel() {
  const lbl = document.getElementById('benchSwitchLabel');
  if (lbl) lbl.textContent = _benchSwitches.length + ' switch' + (_benchSwitches.length !== 1 ? 'es' : '');
}

// Close benchmark overlay and stop any running benchmark on the backend
function closeBench() {
  // If a benchmark is running, kill it on the backend before closing
  if (_benchEventSrc) {
    try { _benchEventSrc.close(); } catch(_) {}
    _benchEventSrc = null;
    fetch('/api/benchmark/cancel', {method: 'POST'}).catch(() => {});
  }
  document.getElementById('benchOverlay').classList.remove('open');
  // Restore powersave mode after benchmarking
  _benchSetPerfMode('powersave');
}

// Switch between different benchmark tools (llama-bench, llama-batched-bench, etc.) and load their default switches into the UI
function switchBenchTab(tool) {
  document.querySelectorAll('.bench-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tool);
  });
  _benchSwitches = (BENCH_DEFAULTS[tool] || []).map(s => ({...s}));
  _renderBenchSwitches();
  _updateBenchSwitchLabel();
}

// Render the list of benchmark switches in the UI, allowing editing and deletion
function _renderBenchSwitches() {
  const list = document.getElementById('benchSwitchList');
  if (!list) return;
  list.innerHTML = '';
  _benchSwitches.forEach((sw, i) => {
    const row = document.createElement('div');
    row.className = 'bench-switch-row';

    const flagInput = document.createElement('input');
    flagInput.className = 'bench-input';
    flagInput.value = sw.flag || '';
    flagInput.addEventListener('change', () => {
      _benchSwitches[i].flag = flagInput.value;
      _updateBenchAxisOpts();   // surface the new flag in the axis dropdown
    });

    const valInput = document.createElement('input');
    valInput.className = 'bench-input';
    valInput.value = sw.value || '';
    valInput.addEventListener('change', () => { _benchSwitches[i].value = valInput.value; });

    const delBtn = document.createElement('button');
    delBtn.className = 'bench-del-btn';
    delBtn.textContent = '✕';
    delBtn.addEventListener('click', () => {
      _benchSwitches.splice(i, 1);
      _renderBenchSwitches();
      _updateBenchSwitchLabel();
      _updateBenchAxisOpts();    // remove orphaned flag from axis dropdown
    });

    row.appendChild(flagInput);
    row.appendChild(valInput);
    row.appendChild(delBtn);
    list.appendChild(row);
  });
}

// Add a new empty switch to the list and open the switch edit dropdown
function addBenchSwitch() {
  _benchSwitches.push({flag:'', value:''});
  _renderBenchSwitches();
  _updateBenchSwitchLabel();
  _updateBenchAxisOpts();
  // Open switches dropdown and focus the new flag input
  document.getElementById('benchSwitchPanel').classList.add('open');
  const inputs = document.querySelectorAll('#benchSwitchList .bench-input');
  if (inputs.length) inputs[(_benchSwitches.length - 1) * 2].focus();
}

// Set the performance mode on the backend (performance, powersave, etc.) to optimize for benchmarking or normal use
async function _benchSetPerfMode(mode) {
  try {
    const r = await fetch('/api/benchmark/perf-mode', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({mode})
    }).then(r => r.json());
    if (!r.ok) console.warn(`perf-mode ${mode} failed:`, r.error);
    return r.ok;
  } catch (e) {
    console.warn(`perf-mode ${mode} error:`, e);
    return false;
  }
}

// Main function to start the benchmark: gathers selected models, tool, and switches; sets perf mode; starts the benchmark on the backend; and listens for streaming results to update the UI
async function runBenchmark() {
  const modelIds = [...document.querySelectorAll('#benchModelPanel input[type=checkbox]:checked')]
                     .map(cb => cb.value);
  const tool     = document.querySelector('.bench-tab.active')?.dataset.tab || 'llama-bench';
  const switches = _benchSwitches.filter(s => (s.flag || '').trim());
  if (!modelIds.length) { alert('Select at least one model.'); return; }

  // llama-bench spawns its own llama.cpp instance and will fail if the
  // configured port is already bound. If a model is loaded or the server
  // is running, offer to unload/stop first using the same endpoints as the
  // llama.cpp tab's Load/Unload/Start/Stop buttons.
  try {
    const [modelsRes, stateRes] = await Promise.all([
      fetch('/api/llm/models').then(r => r.json()).catch(() => ({data: []})),
      fetch('/api/llama-state').then(r => r.json()).catch(() => ({state: 'unknown'})),
    ]);
    const loadedModel = (modelsRes.data || []).find(m => m.status?.value === 'loaded');
    const serverUp    = stateRes.state === 'awake' || stateRes.state === 'sleeping';
    if (loadedModel || serverUp) {
      let title, body;
      if (loadedModel && serverUp) {
        title = `Unload "${adminEsc(shortName(loadedModel.id))}" and stop the server before benchmarking?`;
        body  = 'The model is loaded and the llama.cpp server is running. Both will be stopped before the benchmark starts.';
      } else if (loadedModel) {
        title = `Unload "${adminEsc(shortName(loadedModel.id))}" before benchmarking?`;
        body  = 'The model is currently loaded and will be unloaded before the benchmark starts.';
      } else {
        title = 'Stop the llama.cpp server before benchmarking?';
        body  = 'The server is currently running and will be stopped before the benchmark starts.';
      }
      const ok = await _themedConfirm({
        title, bodyHtml: body,
        confirmLabel: 'Continue',
        cancelLabel:  'Cancel',
      });
      if (!ok) return;
      if (loadedModel) {
        document.getElementById('benchStatus').textContent = 'unloading model…';
        document.getElementById('benchStatus').classList.add('running');
        try {
          await fetch('/api/llm/unload', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({model: loadedModel.id})
          });
        } catch(_) {}
      }
      if (serverUp) {
        document.getElementById('benchStatus').textContent = 'stopping server…';
        document.getElementById('benchStatus').classList.add('running');
        try { await fetch('/api/llm/server/stop', {method: 'POST'}); } catch(_) {}
        // Poll up to 15s for server to actually be down before launching bench
        for (let i = 0; i < 15; i++) {
          await new Promise(r => setTimeout(r, 1000));
          try {
            const s = await fetch('/api/llama-state').then(r => r.json());
            if (s.state !== 'awake' && s.state !== 'sleeping') break;
          } catch(_) {}
        }
      }
    }
  } catch(_) {}

  document.getElementById('benchRunBtn').disabled = true;
  document.getElementById('benchStatus').textContent = 'perf mode…';
  document.getElementById('benchStatus').classList.add('running');
  await _benchSetPerfMode('performance');
  document.getElementById('benchStatus').textContent = 'starting…';
  document.getElementById('benchStatus').classList.add('running');
  document.getElementById('benchResults').classList.remove('shown');
  document.getElementById('benchResultRows').innerHTML = '';
  document.getElementById('benchCancelBtn').style.display = '';
  _benchLogClear();
  if (_benchChart) {
    _benchChart.data.datasets = [];
    _benchChart.update('none');
  }
  _benchModelDatasets = {};
  _benchRawRows = [];
  _benchSetChartIdle(false);

  // Start benchmark on backend, which will respond with a stream of events for logs and results
  fetch('/api/benchmark/run', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({model_ids: modelIds, tool, switches})
  }).then(r => r.json()).then(d => {
    if (!d.ok) {
      alert(d.error || 'Failed to start benchmark');
      document.getElementById('benchRunBtn').disabled = false;
      document.getElementById('benchCancelBtn').style.display = 'none';
      document.getElementById('benchStatus').textContent = 'idle';
      document.getElementById('benchStatus').classList.remove('running');
      return;
    }
    if (_benchEventSrc) { try { _benchEventSrc.close(); } catch(_){} }
    _benchEventSrc = new EventSource('/api/benchmark/stream');
    document.getElementById('benchStatus').textContent = 'running…';
    _benchEventSrc.onmessage = e => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (msg.type === 'keepalive') return;

      if (msg.type === 'model_start') {
        _benchAddModelDatasets(msg.model_id);
        _benchLogAppend(`<div class="bench-log-sep">── ${_hEsc(msg.model_id)} ──</div>`);
        if (msg.cmd) _benchLogAppend(`<span class="bench-log-cmd">$ ${_hEsc(msg.cmd)}</span>`);
        document.getElementById('benchStatus').textContent = `running: ${msg.model_id.split('/').pop()}`;
      } else if (msg.type === 'line') {
        const html = _benchFormatLine(msg.text || '');
        if (html) _benchLogAppend(html);
      } else if (msg.type === 'result') {
        if (msg.model_id && (msg.gen_tps != null || msg.ppt_tps != null)) {
          _benchPushPoint(msg);
        }
      } else if (msg.type === 'model_done') {
        // last_gen_tps / last_ppt_tps are raw per-run averages, not re-averaged
        _benchAddModelResultRow(msg.model_id, msg.last_gen_tps, msg.last_ppt_tps, tool);
        document.getElementById('benchResults').classList.add('shown');
      } else if (msg.type === 'done') {
        if (_benchEventSrc) { try { _benchEventSrc.close(); } catch(_){} _benchEventSrc = null; }
        document.getElementById('benchRunBtn').disabled = false;
        document.getElementById('benchCancelBtn').style.display = 'none';
        document.getElementById('benchStatus').textContent = msg.ok ? 'done' : (msg.error ? 'error' : 'done');
        document.getElementById('benchStatus').classList.remove('running');
        if (msg.error) _benchLogAppend(`<span class="bench-log-text" style="color:var(--crit)">✗ Error: ${_hEsc(String(msg.error))}</span>`);
      }
    };
    _benchEventSrc.onerror = () => {
      // Transient drop: EventSource auto-reconnects and resumes from the last
      // event id (server replays the gap). Only a CLOSED state is terminal.
      if (_benchEventSrc && _benchEventSrc.readyState === EventSource.CONNECTING) {
        document.getElementById('benchStatus').textContent = 'reconnecting…';
        return;
      }
      if (_benchEventSrc) { try { _benchEventSrc.close(); } catch(_){} _benchEventSrc = null; }
      document.getElementById('benchRunBtn').disabled = false;
      document.getElementById('benchCancelBtn').style.display = 'none';
      document.getElementById('benchStatus').textContent = 'disconnected';
      document.getElementById('benchStatus').classList.remove('running');
    };
  }).catch(e => {
    alert('Benchmark request failed: ' + e);
    document.getElementById('benchRunBtn').disabled = false;
    document.getElementById('benchCancelBtn').style.display = 'none';
    document.getElementById('benchStatus').textContent = 'idle';
    document.getElementById('benchStatus').classList.remove('running');
  });
}

// Function to cancel a running benchmark: closes the event stream, sends a cancel request to the backend, and updates the UI state
function cancelBenchmark() {
  if (_benchEventSrc) { try { _benchEventSrc.close(); } catch(_){} _benchEventSrc = null; }
  fetch('/api/benchmark/cancel', {method: 'POST'}).catch(() => {});
  document.getElementById('benchRunBtn').disabled = false;
  document.getElementById('benchCancelBtn').style.display = 'none';
  document.getElementById('benchStatus').textContent = 'cancelled';
  document.getElementById('benchStatus').classList.remove('running');
}

// ===========================================================================
// Auto-Tune CTX
// ---------------------------------------------------------------------------
// Wizard that iteratively runs llama-server with -fitt and converges ctx-size
// on a user-specified free-VRAM headroom. Streams progress over SSE, then
// prompts to save the discovered values back into each model's config.ini
// section via the existing POST /api/llm/config round-trip.
// ===========================================================================
let _atEventSrc = null;
let _atPending = {};   // model_id -> {converged, final_fitt, ctx_size, free_mb, total_vram_mb, iters, applied_params}
let _atLastTarget = null, _atLastTol = null;   // captured at run start for the tolerance gauge

function _atSetStatus(text, running) {
  const el = document.getElementById('atStatus');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('running', !!running);
}

function _atLogClear() {
  const el = document.getElementById('atLog');
  if (el) el.innerHTML = '';
  const raw = document.getElementById('atRawLog');
  if (raw) raw.innerHTML = '';
  const cnt = document.getElementById('atRawCount');
  if (cnt) cnt.textContent = '0';
}

function _atRawAppend(text) {
  const raw = document.getElementById('atRawLog');
  if (!raw) return;
  // Cap to ~10k lines to keep the DOM responsive across long runs
  if (raw.childElementCount >= 10000) {
    raw.removeChild(raw.firstChild);
  }
  const div = document.createElement('div');
  div.textContent = text;
  raw.appendChild(div);
  // Only auto-scroll when the user has the panel open AND is at/near the bottom
  const det = document.getElementById('atRawDetails');
  if (det && det.open) {
    const nearBottom = raw.scrollTop + raw.clientHeight >= raw.scrollHeight - 40;
    if (nearBottom) raw.scrollTop = raw.scrollHeight;
  }
  const cnt = document.getElementById('atRawCount');
  if (cnt) cnt.textContent = String(parseInt(cnt.textContent || '0', 10) + 1);
}

function _atLogAppend(html) {
  const el = document.getElementById('atLog');
  if (!el) return;
  const div = document.createElement('div');
  div.innerHTML = html;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

function _atCollectOptionalParams() {
  const p = {};
  const en = id => document.getElementById('atEn_' + id)?.checked;
  const v  = id => (document.getElementById('atV_' + id)?.value || '').trim();
  if (en('mlock'))      p.mlock      = true;
  if (en('no_mmap'))    p.no_mmap    = true;
  if (en('kv_unified')) p.kv_unified = true;
  if (en('parallel')   && v('parallel')   !== '') p.parallel   = parseInt(v('parallel'), 10);
  if (en('cache_ram')  && v('cache_ram')  !== '') p.cache_ram  = parseInt(v('cache_ram'), 10);
  if (en('b')          && v('b')          !== '') p.b          = parseInt(v('b'), 10);
  if (en('ub')         && v('ub')         !== '') p.ub         = parseInt(v('ub'), 10);
  if (en('ngl')        && v('ngl')        !== '') p.ngl        = parseInt(v('ngl'), 10);
  if (en('ctk')        && v('ctk')        !== '') p.ctk        = v('ctk');
  if (en('ctv')        && v('ctv')        !== '') p.ctv        = v('ctv');
  // Custom-args textarea: one flag (with optional value) per line.
  // Split each line on the first run of whitespace so a flag with
  // an embedded space in its value still gets a single token-value.
  // Lines not starting with '-' are silently skipped — those are
  // either comments or accidental input.
  const customRaw = (document.getElementById('atCustomArgs')?.value || '').trim();
  if (customRaw) {
    const tokens = [];
    customRaw.split('\n').forEach(line => {
      const t = line.trim();
      if (!t || !t.startsWith('-')) return;
      const sp = t.search(/\s/);
      if (sp === -1) {
        tokens.push(t);
      } else {
        tokens.push(t.slice(0, sp));
        const val = t.slice(sp + 1).trim();
        if (val) tokens.push(val);
      }
    });
    if (tokens.length) p.custom_args = tokens;
  }
  return p;
}

// Translate the wizard's optional-params dict into the matching config.ini key/value pairs.
// Only keys actually enabled in the wizard appear here. Booleans serialize as 'true'.
function _atParamsToConfigKeys(p) {
  const out = {};
  if (!p) return out;
  if (p.mlock)                 out['mlock']      = 'true';
  if (p.no_mmap)               out['no-mmap']    = 'true';
  if (p.kv_unified)            out['kv-unified'] = 'true';
  if (p.parallel  != null)     out['parallel']   = String(p.parallel);
  if (p.cache_ram != null)     out['cache-ram']  = String(p.cache_ram);
  if (p.b         != null)     out['batch-size'] = String(p.b);
  if (p.ub        != null)     out['ubatch-size']= String(p.ub);
  if (p.ngl       != null)     out['n-gpu-layers'] = String(p.ngl);
  if (p.ctk)                   out['cache-type-k'] = p.ctk;
  if (p.ctv)                   out['cache-type-v'] = p.ctv;
  // Custom args: walk the token list, treating `--foo bar` as key/value
  // and `--foo` as a bare boolean flag. Keys are stored without the
  // leading dashes to match the config.ini convention used elsewhere.
  if (Array.isArray(p.custom_args)) {
    let i = 0;
    while (i < p.custom_args.length) {
      const tok = String(p.custom_args[i] || '').trim();
      if (!tok.startsWith('-')) { i++; continue; }
      const key = tok.replace(/^-+/, '');
      const next = p.custom_args[i + 1];
      if (next != null && !String(next).startsWith('-')) {
        out[key] = String(next);
        i += 2;
      } else {
        out[key] = 'true';
        i += 1;
      }
    }
  }
  return out;
}

async function _atCheckPreflight() {
  const banner = document.getElementById('atPreflight');
  const msg    = document.getElementById('atPreflightMsg');
  const btn    = document.getElementById('atStopBtn');
  const runBtn = document.getElementById('atRunBtn');
  banner.style.display = '';
  msg.textContent = 'Checking llama-server state…';
  btn.style.display = 'none';
  try {
    const s = await fetch('/api/llama-state').then(r => r.json());
    const up = (s.state === 'awake' || s.state === 'sleeping');
    if (up) {
      msg.textContent = '⚠ llama-server is running. Stop it before auto-tune (it would collide on port and VRAM).';
      btn.style.display = '';
      runBtn.disabled = true;
    } else {
      banner.style.display = 'none';
      runBtn.disabled = false;
    }
  } catch (_) {
    msg.textContent = 'Could not check server state — proceed with caution.';
    runBtn.disabled = false;
  }
}

async function atStopServer() {
  _atSetStatus('stopping llama-server…', true);
  try { await fetch('/api/llm/server/stop', {method:'POST'}); } catch(_) {}
  // Poll up to 15s for server to actually be down
  for (let i = 0; i < 15; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const s = await fetch('/api/llama-state').then(r => r.json());
      if (s.state !== 'awake' && s.state !== 'sleeping') break;
    } catch(_) {}
  }
  _atSetStatus('idle', false);
  await _atCheckPreflight();
}

// One-shot wiring: when any optional-param value field receives input or a
// dropdown selection, auto-tick its enable checkbox so the param is actually
// sent. Saves the user from the "I set a value but forgot to enable it" trap.
// Show how many optional params are enabled in the collapsed group header.
function _atUpdateOptCount() {
  const det  = document.getElementById('atOptionalParamsDetails');
  const span = document.getElementById('atOptCount');
  if (!det || !span) return;
  const n = det.querySelectorAll('input[type=checkbox]:checked').length;
  span.textContent = n ? ` (${n} set)` : '';
}

function _atWireOptionalAutoCheck() {
  if (window._atOptAutoCheckWired) return;
  const pairs = ['parallel','cache_ram','b','ub','ngl','ctk','ctv'];
  pairs.forEach(name => {
    const v  = document.getElementById('atV_'  + name);
    const en = document.getElementById('atEn_' + name);
    if (!v || !en) return;
    const tick = () => { if (!en.checked) en.checked = true; _atUpdateOptCount(); };
    v.addEventListener('input',  tick);
    v.addEventListener('change', tick);
  });
  const det = document.getElementById('atOptionalParamsDetails');
  if (det) det.addEventListener('change', _atUpdateOptCount);
  window._atOptAutoCheckWired = true;
}

async function openAutotune() {
  document.getElementById('autotuneOverlay').classList.add('open');
  _atWireOptionalAutoCheck();
  _atLogClear();
  _atRenderPlaceholder();
  _atPending = {};
  _atSetStatus('idle', false);
  document.getElementById('atRunBtn').disabled = false;
  document.getElementById('atCancelBtn').style.display = 'none';
  // Models open for selection; optional-params stays collapsed.
  document.getElementById('atModelsDetails')?.setAttribute('open', '');
  document.getElementById('atOptionalParamsDetails')?.removeAttribute('open');
  _atUpdateOptCount();

  // Populate model list (same source as benchmark)
  let models = [];
  try {
    const r = await fetch('/api/benchmark/models').then(r => r.json());
    models = r.models || [];
  } catch (e) { console.warn('benchmark/models failed', e); }
  const panel = document.getElementById('atModelPanel');
  panel.innerHTML = '';
  if (!models.length) {
    panel.innerHTML = '<div style="font-size:0.85em;color:var(--fg-dim);">No models configured.</div>';
  } else {
    models.forEach(m => {
      const item = document.createElement('div');
      item.className = 'bench-model-item';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = m;
      cb.id = 'atcb_' + m.replace(/[^a-zA-Z0-9]/g, '_');
      const lbl = document.createElement('label');
      lbl.htmlFor = cb.id;
      lbl.textContent = m;
      item.appendChild(cb);
      item.appendChild(lbl);
      panel.appendChild(item);
    });
  }
  await _atCheckPreflight();
}

function closeAutotune() {
  if (_atEventSrc) {
    try { _atEventSrc.close(); } catch(_) {}
    _atEventSrc = null;
    fetch('/api/llm/autotune/cancel', {method:'POST'}).catch(() => {});
    /* agent restores perf mode in its finally: block */
  }
  document.getElementById('autotuneOverlay').classList.remove('open');
}

async function runAutotune() {
  const modelIds = [...document.querySelectorAll('#atModelPanel input[type=checkbox]:checked')]
                     .map(cb => cb.value);
  if (!modelIds.length) { alert('Select at least one model.'); return; }
  const targetMb = parseInt(document.getElementById('atTargetMb').value, 10);
  if (!Number.isFinite(targetMb) || targetMb < 0) {
    alert('Target free VRAM (MB) must be a non-negative integer.');
    return;
  }
  const toleranceMb = parseInt(document.getElementById('atToleranceMb').value, 10);
  if (!Number.isFinite(toleranceMb) || toleranceMb < 1) {
    alert('Tolerance (MB) must be a positive integer.');
    return;
  }
  const optional_params = _atCollectOptionalParams();
  _atLastTarget = targetMb; _atLastTol = toleranceMb;

  // Collapse the setup sections now that the run is starting — they
  // take a lot of vertical space and the operator wants to see iter
  // results, not the inputs they just submitted. The X close button
  // and the result cards stay visible.
  document.getElementById('atModelsDetails')?.removeAttribute('open');
  document.getElementById('atOptionalParamsDetails')?.removeAttribute('open');

  // Render "pending" cards up front so the user sees the queue
  const results = document.getElementById('atResults');
  results.innerHTML = '';
  modelIds.forEach(mid => {
    const card = document.createElement('div');
    card.className = 'at-result-card pending';
    card.id = 'atcard_' + mid.replace(/[^a-zA-Z0-9]/g, '_');
    card.innerHTML = `<div><b>${_hEsc(mid)}</b> — <span class="at-card-status" style="color:var(--fg-dim);">queued</span></div>`;
    results.appendChild(card);
  });

  document.getElementById('atRunBtn').disabled = true;
  document.getElementById('atCancelBtn').style.display = '';
  // The agent flips the perf governor itself at run start/end (see
  // _autotune_set_perf_mode in the agent); no client-side call needed here.
  _atSetStatus('starting…', true);
  _atLogClear();

  let resp, bodyText;
  try {
    resp = await fetch('/api/llm/autotune/run', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model_ids: modelIds, target_mb: targetMb,
                            tolerance_mb: toleranceMb, optional_params}),
    });
    bodyText = await resp.text();
  } catch (e) {
    alert('Auto-tune request failed (network): ' + (e && e.message ? e.message : e));
    document.getElementById('atRunBtn').disabled = false;
    document.getElementById('atCancelBtn').style.display = 'none';
    _atSetStatus('idle', false);
    /* agent restores perf mode in its finally: block */
    return;
  }
  let r;
  try { r = JSON.parse(bodyText); }
  catch (_) {
    alert(`Auto-tune request failed: HTTP ${resp.status}\n\n${(bodyText || '').slice(0, 400)}`);
    document.getElementById('atRunBtn').disabled = false;
    document.getElementById('atCancelBtn').style.display = 'none';
    _atSetStatus('idle', false);
    /* agent restores perf mode in its finally: block */
    return;
  }
  if (!resp.ok || !r.ok) {
    alert(r.error || `Failed to start auto-tune (HTTP ${resp.status})`);
    document.getElementById('atRunBtn').disabled = false;
    document.getElementById('atCancelBtn').style.display = 'none';
    _atSetStatus('idle', false);
    /* agent restores perf mode in its finally: block */
    return;
  }

  if (_atEventSrc) { try { _atEventSrc.close(); } catch(_){} }
  // Use the manager-proxied SSE path (same as Benchmark) — avoids the
  // direct-to-agent token + CORS hop, which fails on some setups.
  _atEventSrc = new EventSource('/api/llm/autotune/stream');
  _atSetStatus('running…', true);

  _atEventSrc.onmessage = e => {
    let msg;
    try { msg = JSON.parse(e.data); } catch(_) { return; }
    if (msg.type === 'keepalive') return;

    if (msg.type === 'model_start') {
      _atLogAppend(`<div class="at-log-sep">── ${_hEsc(msg.model_id)} · target ${Number(msg.target_mb)||0} MB ──</div>`);
      _atSetCardStatus(msg.model_id, `tuning (target ${msg.target_mb} MB)`);
      _atSetStatus(`tuning ${_atShort(msg.model_id)}`, true);
    } else if (msg.type === 'iter_start') {
      // Single placeholder line per (model, iter); gets rewritten in place on
      // every loading_progress heartbeat, sentinel_retry, and finally
      // iter_result / iter_failed. Sentinel retries re-fire iter_start with
      // the same iter index but a new -fitt — reuse the existing row then.
      const text = `Iter ${msg.iter} · -fitt=${msg.fitt} MB · loading…`;
      const row  = _atFindIterRow(msg.model_id, msg.iter);
      if (row) {
        row.textContent = text;
        row.style.color = '';
        row.setAttribute('data-fitt', msg.fitt);
      } else {
        _atLogAppend(`<div class="at-iter-line" data-iter="${_hEsc(String(msg.iter))}" data-model="${_hEsc(String(msg.model_id))}" data-fitt="${_hEsc(String(msg.fitt))}">${_hEsc(text)}</div>`);
      }
      _atSetCardStatus(msg.model_id, `iter ${msg.iter} — fitt=${msg.fitt} MB · loading…`);
    } else if (msg.type === 'loading_progress') {
      const row = _atFindIterRow(msg.model_id, msg.iter);
      if (row) {
        const pct = Math.min(100, Math.round(100 * (msg.elapsed_s || 0) / Math.max(1, msg.timeout_s || 1)));
        row.textContent = `Iter ${msg.iter} · -fitt=${msg.fitt} MB · loading ${_atProgressBar(pct)} ${pct}% · ${_atMMSS(msg.elapsed_s)} / ${_atMMSS(msg.timeout_s)}`;
      }
      _atSetCardStatus(msg.model_id, `iter ${msg.iter} · loading ${_atMMSS(msg.elapsed_s)}/${_atMMSS(msg.timeout_s)}`);
    } else if (msg.type === 'iter_result') {
      const tgt  = parseInt(document.getElementById('atTargetMb').value, 10) || 0;
      const diff = msg.actual_free_mb - tgt;
      const sign = diff > 0 ? '+' : '';
      const text = `Iter ${msg.iter} · -fitt=${msg.fitt} MB → free ${msg.actual_free_mb} MB / ${msg.total_vram_mb} MB (${sign}${diff} MB vs target) · ctx ${msg.n_ctx_seq.toLocaleString()}`;
      const row  = _atFindIterRow(msg.model_id, msg.iter);
      if (row) row.textContent = text;
      else     _atLogAppend(`<div>${_hEsc(text)}</div>`);
      _atSetCardStatus(msg.model_id, `iter ${msg.iter}: free ${msg.actual_free_mb} MB (target ${tgt} MB)`);
    } else if (msg.type === 'iter_failed') {
      const txt = `Iter ${msg.iter} · failed: ${msg.error || msg.reason || 'unknown'}`;
      const row = _atFindIterRow(msg.model_id, msg.iter);
      if (row) { row.textContent = txt; row.style.color = 'var(--crit)'; }
      else     _atLogAppend(`<div class="at-log-crit">${_hEsc(txt)}</div>`);
    } else if (msg.type === 'sentinel_retry') {
      // Shutdown memory breakdown came back with sentinel values (typically
      // ~2^44 MiB free — SIZE_MAX divided through). Agent is doubling -fitt
      // and re-running this iteration. Replace the in-progress row in place
      // so the log keeps one line per iter.
      const row  = _atFindIterRow(msg.model_id, msg.iter);
      const note = `Iter ${msg.iter} · out-of-bounds free=${msg.raw_free_mb} MB · doubling -fitt ${msg.old_fitt}→${msg.new_fitt} MB (retry ${msg.attempt}/${msg.max_attempts})`;
      if (row) {
        row.textContent = note;
        row.style.color = 'var(--warn)';
        row.setAttribute('data-fitt', msg.new_fitt);
      } else {
        _atLogAppend(`<div class="at-log-warn">${_hEsc(note)}</div>`);
      }
      _atSetCardStatus(msg.model_id, `iter ${msg.iter} · sentinel retry ${msg.attempt}/${msg.max_attempts}`);
    } else if (msg.type === 'sentinel_seen_update') {
      // After a sentinel-retry succeeds, the agent records the failed
      // fitt buckets so the picker will avoid them on later iters.
      // Show a small note so the operator sees the picker now knows
      // about the sentinel zone.
      const chain = (msg.failed_chain || []).join(', ');
      _atLogAppend(`<div class="at-log-dim" style="font-size:0.85em;margin-left:18px;">↳ sentinel buckets recorded: [${_hEsc(chain)}] — picker will avoid these exact values</div>`);
    } else if (msg.type === 'perf_mode') {
      // Agent-reported result of the systemctl reload-or-restart for the
      // performance/powersave unit. Surface success AND failure so the user
      // knows whether the perf controller actually moved.
      if (msg.ok) {
        _atLogAppend(`<div class="at-log-ok">perf mode → <b>${_hEsc(msg.mode)}</b></div>`);
      } else {
        const detail = msg.error ? ` (${_hEsc(msg.error)})` : (msg.rc != null ? ` (rc=${Number(msg.rc)})` : '');
        _atLogAppend(`<div class="at-log-warn">perf mode <b>${_hEsc(msg.mode)}</b> not applied${detail}</div>`);
      }
    } else if (msg.type === 'line') {
      // Raw llama-server output goes into the collapsed "Raw output" panel
      // below the condensed log. Not shown in the main log to avoid drowning
      // the typed progress events the user actually cares about.
      if (msg.text) _atRawAppend(msg.text);
    } else if (msg.type === 'model_done') {
      _atPending[msg.model_id] = msg;
      if (msg.ok) {
        // Pick the tag from the structured stop_reason if present, so the
        // operator can tell sentinel_unreachable / non_monotonic_peak /
        // cycle apart from a genuine iter-limit overrun.
        const reason = (msg.stop_reason || '').toString();
        let tag, cls;
        if (msg.converged) {
          tag = '✓ converged'; cls = 'at-log-ok';
        } else if (reason.startsWith('sentinel_unreachable')) {
          tag = '⛔ target unreachable (sentinel-prone region)'; cls = 'at-log-warn';
        } else if (reason.startsWith('bracket_precision')) {
          tag = '✓ converged to precision limit'; cls = 'at-log-ok';
        } else if (reason.startsWith('non_monotonic_peak')) {
          tag = '⛔ target above peak (non-monotonic curve)'; cls = 'at-log-warn';
        } else if (reason.startsWith('cycle')) {
          tag = '⚠ search exhausted (cycle / no new gap)'; cls = 'at-log-warn';
        } else if (reason.startsWith('iter_limit')) {
          tag = '⚠ capped at iter limit'; cls = 'at-log-warn';
        } else {
          tag = '⚠ stopped early'; cls = 'at-log-warn';
        }
        _atLogAppend(`<div class="${cls}" style="font-weight:500;">${tag} · final -fitt=${Number(msg.final_fitt)||0} MB · ctx-size=${Number(msg.ctx_size ?? 0).toLocaleString()} · free=${Number(msg.free_mb)||0} MB (${Number(msg.iters)||0} iters)</div>`);
        if (!msg.converged && reason) {
          _atLogAppend(`<div class="at-log-dim" style="font-size:0.85em;margin-left:18px;">${_hEsc(reason)}</div>`);
        }
      } else {
        _atLogAppend(`<div class="at-log-crit" style="font-weight:500;">✗ no valid result for ${_hEsc(msg.model_id)}</div>`);
      }
      _atRenderResultCard(msg);
    } else if (msg.type === 'done') {
      if (_atEventSrc) { try { _atEventSrc.close(); } catch(_){} _atEventSrc = null; }
      document.getElementById('atRunBtn').disabled = false;
      document.getElementById('atCancelBtn').style.display = 'none';
      _atSetStatus(msg.cancelled ? 'cancelled' : (msg.ok ? 'done' : 'error'), false);
      if (msg.error) _atLogAppend(`<span class="at-log-crit">✗ ${_hEsc(msg.error)}</span>`);
      /* agent restores perf mode in its finally: block */
      // If user opted to restart llama-server after, do it now (only when at
      // least one model converged successfully).
      if (msg.ok && document.getElementById('atRestartAfter')?.checked) {
        fetch('/api/llm/server/start', {method: 'POST'}).catch(() => {});
      }
    }
  };
  _atEventSrc.onerror = (ev) => {
    const rs = _atEventSrc ? _atEventSrc.readyState : -1;
    console.error('[autotune] SSE error; readyState=' + rs, ev);
    _atLogAppend(`<span class="at-log-crit">SSE disconnected (readyState=${rs}). Check the manager + agent logs.</span>`);
    if (_atEventSrc) { try { _atEventSrc.close(); } catch(_){} _atEventSrc = null; }
    document.getElementById('atRunBtn').disabled = false;
    document.getElementById('atCancelBtn').style.display = 'none';
    _atSetStatus('disconnected', false);
    /* agent restores perf mode in its finally: block */
  };
}

function cancelAutotune() {
  if (_atEventSrc) { try { _atEventSrc.close(); } catch(_){} _atEventSrc = null; }
  fetch('/api/llm/autotune/cancel', {method:'POST'}).catch(() => {});
  document.getElementById('atRunBtn').disabled = false;
  document.getElementById('atCancelBtn').style.display = 'none';
  _atSetStatus('cancelled', false);
  try { _benchSetPerfMode('powersave'); } catch(_) {}
}

function _atShort(mid) {
  return (mid || '').split('/').pop() || mid;
}

// Locate the iter row that belongs to *this* model. We must scope by model id
// as well as iter index — when multiple models run sequentially, iter 1 of
// model 2 would otherwise stomp the displayed row for iter 1 of model 1
// (same data-iter, same DOM).
function _atFindIterRow(modelId, iter) {
  if (!window.CSS || typeof CSS.escape !== 'function') {
    // Pre-CSS.escape fallback — walk the log and match by attribute manually
    const rows = document.querySelectorAll(`#atLog .at-iter-line[data-iter="${iter}"]`);
    for (const r of rows) if (r.getAttribute('data-model') === modelId) return r;
    return null;
  }
  return document.querySelector(
    `#atLog .at-iter-line[data-iter="${iter}"][data-model="${CSS.escape(modelId)}"]`
  );
}

function _atProgressBar(pct) {
  const WIDTH = 24;
  const filled = Math.max(0, Math.min(WIDTH, Math.round(WIDTH * (pct / 100))));
  return '[' + '█'.repeat(filled) + '░'.repeat(WIDTH - filled) + ']';
}

function _atMMSS(totalSec) {
  const s = Math.max(0, Math.floor(totalSec || 0));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return String(m).padStart(2,'0') + ':' + String(r).padStart(2,'0');
}

function _atSetCardStatus(modelId, text) {
  const card = document.getElementById('atcard_' + modelId.replace(/[^a-zA-Z0-9]/g, '_'));
  if (!card) return;
  const sp = card.querySelector('.at-card-status');
  if (sp) sp.textContent = text;
}

// Show a dashed placeholder result card before any run so the report layout
// is visible on an empty modal. Cleared when a run starts (renders pending cards).
function _atRenderPlaceholder() {
  const results = document.getElementById('atResults');
  if (!results) return;
  const card = document.createElement('div');
  card.className = 'at-result-card at-result-placeholder';
  const head = document.createElement('div');
  head.style.color = 'var(--fg-dim)';
  head.textContent = 'Run auto tune to populate results';
  const grid = document.createElement('div');
  grid.className = 'at-result-grid';
  ['ctx-size', 'free VRAM', 'final -fitt', 'iterations'].forEach(label => {
    const stat = document.createElement('div');
    stat.className = 'at-stat';
    const k = document.createElement('div');
    k.className = 'at-stat-k';
    k.textContent = label;
    const v = document.createElement('div');
    v.className = 'at-stat-v';
    v.style.color = 'var(--fg-faint)';
    v.textContent = '—';
    stat.appendChild(k);
    stat.appendChild(v);
    grid.appendChild(stat);
  });
  card.appendChild(head);
  card.appendChild(grid);
  results.innerHTML = '';
  results.appendChild(card);
}

// Tolerance-band gauge HTML: where free VRAM landed vs target ± tolerance.
// Window is target ± 3·tol; marker is green inside the band, amber outside.
function _atGaugeHtml(free, target, tol) {
  if (![free, target, tol].every(Number.isFinite) || tol <= 0) return '';
  const lo = target - 3 * tol, hi = target + 3 * tol, span = hi - lo;
  if (span <= 0) return '';
  const clamp = p => Math.max(0, Math.min(100, p));
  const bandL = clamp((target - tol - lo) / span * 100);
  const bandW = clamp((2 * tol) / span * 100);
  const mark  = clamp((free - lo) / span * 100);
  const hit = Math.abs(free - target) <= tol;
  return `
    <div class="at-gauge">
      <div class="at-gauge-lbls"><span>${lo} MB</span><span>target ${target} ±${tol}</span><span>${hi} MB</span></div>
      <div class="at-gauge-track">
        <div class="at-gauge-band" style="left:${bandL}%;width:${bandW}%;"></div>
        <div class="at-gauge-mark${hit ? '' : ' miss'}" style="left:calc(${mark}% - 1px);"></div>
      </div>
    </div>`;
}

function _atRenderResultCard(payload) {
  const modelId = payload.model_id;
  const card = document.getElementById('atcard_' + modelId.replace(/[^a-zA-Z0-9]/g, '_'));
  if (!card) return;
  card.classList.remove('pending');
  // Pick the result card tag the same way the log line does, so they
  // can't disagree (e.g. log says "✓ converged to precision limit" while
  // the card says "capped at iter limit").
  let tag;
  if (!payload.ok) {
    tag = '<span class="at-log-crit">failed — nothing to save</span>';
  } else {
    const reason = (payload.stop_reason || '').toString();
    if (payload.converged) {
      tag = '<span class="at-log-ok">converged ✓</span>';
    } else if (reason.startsWith('sentinel_unreachable')) {
      tag = '<span class="at-log-warn">target unreachable (sentinel)</span>';
    } else if (reason.startsWith('bracket_precision')) {
      tag = '<span class="at-log-warn">stopped at precision limit (outside TOL)</span>';
    } else if (reason.startsWith('non_monotonic_peak')) {
      tag = '<span class="at-log-warn">target above achievable peak</span>';
    } else if (reason.startsWith('cycle')) {
      tag = '<span class="at-log-warn">search exhausted</span>';
    } else if (reason.startsWith('iter_limit')) {
      tag = '<span class="at-log-warn">capped at iter limit</span>';
    } else {
      tag = '<span class="at-log-warn">stopped early</span>';
    }
  }
  const ctx  = payload.ctx_size   ?? '—';
  const fitt = payload.final_fitt ?? '—';
  const free = payload.free_mb    ?? '—';
  const tot  = payload.total_vram_mb ?? '—';
  const iters = payload.iters     ?? '—';
  const params = payload.applied_params || {};
  const paramSummary = Object.keys(params).length
    ? Object.entries(params).map(([k,v]) => `${_hEsc(String(k))}=${_hEsc(String(v))}`).join(', ')
    : '<i style="color:var(--fg-dim)">defaults</i>';
  const gaugeHtml = payload.ok ? _atGaugeHtml(Number(payload.free_mb), _atLastTarget, _atLastTol) : '';
  card.innerHTML = `
    <div class="at-result-head"><span class="at-result-model">${_hEsc(modelId)}</span> ${tag}</div>
    <div class="at-result-grid">
      <div class="at-stat"><div class="at-stat-k">ctx-size</div><div class="at-stat-v">${_hEsc(String(ctx))}</div></div>
      <div class="at-stat"><div class="at-stat-k">free VRAM</div><div class="at-stat-v">${_hEsc(String(free))}<span class="at-stat-u"> / ${_hEsc(String(tot))} MB</span></div></div>
      <div class="at-stat"><div class="at-stat-k">final -fitt</div><div class="at-stat-v">${_hEsc(String(fitt))}</div></div>
      <div class="at-stat"><div class="at-stat-k">iterations</div><div class="at-stat-v">${_hEsc(String(iters))}</div></div>
    </div>
    ${gaugeHtml}
    <div style="font-size:0.82em;color:var(--fg-dim);">applied params: ${paramSummary}</div>
    <div class="at-result-actions">
      <button class="btn btn-stone-muted-gradient" data-at-save="ctx"        data-at-model="${_hEsc(modelId)}" ${payload.ok ? '' : 'disabled'}
              title="Write only ctx-size to this model's config.ini section; leave other keys untouched">💾 Save ctx-size only</button>
      <button class="btn btn-stone-muted-gradient" data-at-save="ctx-params" data-at-model="${_hEsc(modelId)}" ${payload.ok ? '' : 'disabled'}
              title="Write ctx-size plus every optional param that was enabled for this tune">💾 Save ctx-size + params</button>
      <button class="btn btn-gray-muted-gradient"  data-at-skip="1"          data-at-model="${_hEsc(modelId)}">Skip</button>
    </div>
    <!-- Error messages only — success state lives on the button. -->
    <div class="at-save-status" style="font-size:0.82em;margin-top:4px;"></div>
  `;
}

async function saveAutotuneResult(modelId, includeParams, btn) {
  const payload = _atPending[modelId];
  if (!payload || !payload.ok || payload.ctx_size == null) {
    alert('Nothing valid to save for this model.');
    return;
  }
  const card = document.getElementById('atcard_' + modelId.replace(/[^a-zA-Z0-9]/g, '_'));
  const statusEl = card?.querySelector('.at-save-status');
  if (statusEl) { statusEl.textContent = ''; statusEl.style.color = ''; }

  // Flip the clicked button to a "Saving…" intermediate state so the
  // operator gets immediate feedback before the POST roundtrip.
  const origLabel = btn ? btn.textContent : null;
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Saving…';
  }

  // Refresh _llmConfig to avoid clobbering concurrent edits, then mutate just this section
  try {
    const fresh = await fetch('/api/llm/config').then(r => r.json());
    if (fresh && typeof fresh === 'object') _llmConfig = fresh;
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'config refresh failed: ' + e; statusEl.style.color = 'var(--crit)'; }
    if (btn) { btn.disabled = false; btn.textContent = origLabel; }
    return;
  }

  const cfg = {..._llmConfig};
  delete cfg['__DEFAULTS__'];
  const section = {...(cfg[modelId] || {})};
  section['ctx-size'] = String(payload.ctx_size);
  if (includeParams) {
    Object.assign(section, _atParamsToConfigKeys(payload.applied_params || {}));
  }
  cfg[modelId] = section;

  try {
    const r = await fetch('/api/llm/config', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(cfg),
    }).then(r => r.json());
    if (r.ok) {
      // Lock both save buttons — one click per card; the sibling save
      // would just rewrite the same section. Skip stays clickable.
      if (btn) {
        btn.textContent = '✓ Saved';
        btn.classList.add('btn-saved');
        btn.disabled = true;
        card?.querySelectorAll('[data-at-save]').forEach(b => {
          if (b !== btn) b.disabled = true;
        });
      }
      try { await refreshLLMTab(); } catch(_) {}
    } else {
      if (statusEl) { statusEl.textContent = 'save failed: ' + (r.error || 'unknown'); statusEl.style.color = 'var(--crit)'; }
      if (btn) { btn.disabled = false; btn.textContent = origLabel; }
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'save error: ' + e; statusEl.style.color = 'var(--crit)'; }
    if (btn) { btn.disabled = false; btn.textContent = origLabel; }
  }
}

function skipAutotuneResult(modelId) {
  const card = document.getElementById('atcard_' + modelId.replace(/[^a-zA-Z0-9]/g, '_'));
  const statusEl = card?.querySelector('.at-save-status');
  if (statusEl) { statusEl.textContent = 'skipped'; statusEl.style.color = 'var(--fg-dim)'; }
}

// Delegated handler: result-card buttons carry data-at-model / data-at-save /
// data-at-skip rather than inline onclick, so attacker-influenced model_id
// strings cannot break out of an HTML attribute. Registered once.
if (!window.__atDelegatedBound) {
  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-at-save],[data-at-skip]');
    if (!btn) return;
    const modelId = btn.getAttribute('data-at-model') || '';
    if (!modelId) return;
    if (btn.hasAttribute('data-at-skip')) {
      skipAutotuneResult(modelId);
    } else {
      const mode = btn.getAttribute('data-at-save');
      saveAutotuneResult(modelId, mode === 'ctx-params', btn);
    }
  });
  window.__atDelegatedBound = true;
}

// Show a dashed placeholder stat-card row before any run so the report layout
// is visible on an empty modal. Cleared when a run starts (runBenchmark).
function _benchRenderPlaceholder() {
  const rows = document.getElementById('benchResultRows');
  if (!rows) return;
  rows.innerHTML = '';
  const row = document.createElement('div');
  row.className = 'bench-result-row bench-result-placeholder';
  const head = document.createElement('div');
  head.className = 'bench-result-head';
  const name = document.createElement('span');
  name.className = 'bench-result-model';
  name.style.color = 'var(--fg-dim)';
  name.textContent = 'Run a benchmark to populate results';
  head.appendChild(name);
  const grid = document.createElement('div');
  grid.className = 'bench-result-grid';
  ['Prompt', 'Generation', 'Combined'].forEach(label => {
    const card = document.createElement('div');
    card.className = 'bench-stat-card';
    const k = document.createElement('div');
    k.className = 'bench-stat-k';
    k.textContent = label;
    const v = document.createElement('div');
    v.className = 'bench-stat-v';
    v.style.color = 'var(--fg-faint)';
    v.textContent = '—';
    card.appendChild(k);
    card.appendChild(v);
    grid.appendChild(card);
  });
  row.appendChild(head);
  row.appendChild(grid);
  rows.appendChild(row);
  document.getElementById('benchResults').classList.add('shown');
}

// After a model finishes benchmarking, add a result row to the UI showing the best t/s for prompt, gen, and combined tests, and buttons to save or clear the benchmark data
function _benchAddModelResultRow(modelId, _unused1, _unused2, tool) {
  // Compute max t/s per test type from raw rows collected during the run
  const modelRows = _benchRawRows.filter(r => r.model_id === modelId);
  const maxOf = fn => {
    const vals = modelRows.filter(fn).map(r => r.avg_ts ?? 0);
    return vals.length ? Math.max(...vals) : null;
  };
  const maxPpt = maxOf(r => (r.n_prompt ?? 0) > 0 && (r.n_gen ?? 0) === 0);
  const maxGen = maxOf(r => (r.n_gen ?? 0) > 0 && (r.n_prompt ?? 0) === 0);
  const maxPg  = maxOf(r => (r.n_prompt ?? 0) > 0 && (r.n_gen ?? 0) > 0);

  const rows = document.getElementById('benchResultRows');

  const row = document.createElement('div');
  row.className = 'bench-result-row';

  const head = document.createElement('div');
  head.className = 'bench-result-head';

  const nameEl = document.createElement('span');
  nameEl.className = 'bench-result-model';
  // Strip embedded parameter strings llama-bench likes to inject into the
  // model_id (e.g. "Qwen2.5-7B-Instruct-Q4_K_M.gguf,b=2048,ub=512"); keep
  // only the file/identifier so the column doesn't blur into a wall of text.
  const cleanModelId = String(modelId)
    .split(/[,;]/)[0]
    .trim();
  nameEl.textContent = cleanModelId || modelId;
  nameEl.title = modelId;     // keep the full string discoverable on hover

  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-zinc-muted-gradient';
  saveBtn.textContent = '💾 Save';
  saveBtn.style.fontSize = '0.78em';
  saveBtn.addEventListener('click', () => {
    saveBenchmark(modelId, maxGen, maxPpt, maxPg, tool, saveBtn);
  });

  const clearBtn = document.createElement('button');
  clearBtn.className = 'btn btn-red-muted-gradient';
  clearBtn.textContent = '✕';
  clearBtn.style.fontSize = '0.78em';
  clearBtn.title = 'Clear stored benchmark for this model';
  clearBtn.addEventListener('click', async () => {
    const ok = await _themedConfirm({
      title:        `Clear stored benchmark for ${adminEsc(modelId)}?`,
      bodyHtml:     'The saved tps values will be removed for this model.',
      confirmLabel: 'Clear',
      cancelLabel:  'Cancel',
      danger:       true,
    });
    if (!ok) return;
    clearStoredBenchmark(modelId, clearBtn.closest('.bench-result-row'));
  });

  const actions = document.createElement('div');
  actions.className = 'bench-result-actions';
  actions.appendChild(saveBtn);
  actions.appendChild(clearBtn);

  head.appendChild(nameEl);
  head.appendChild(actions);

  const grid = document.createElement('div');
  grid.className = 'bench-result-grid';
  const mkStat = (label, val, unit, digits) => {
    const card = document.createElement('div');
    card.className = 'bench-stat-card';
    const k = document.createElement('div');
    k.className = 'bench-stat-k';
    k.textContent = label;
    const v = document.createElement('div');
    v.className = 'bench-stat-v';
    if (val != null) {
      v.textContent = val.toFixed(digits);
      if (unit) { const u = document.createElement('span'); u.className = 'bench-stat-u'; u.textContent = unit; v.appendChild(u); }
    } else {
      v.textContent = '—';
    }
    card.appendChild(k);
    card.appendChild(v);
    return card;
  };
  grid.appendChild(mkStat('Prompt', maxPpt, 't/s', 0));
  grid.appendChild(mkStat('Generation', maxGen, 't/s', 1));
  grid.appendChild(mkStat('Combined', maxPg, 't/s', 0));

  row.appendChild(head);
  row.appendChild(grid);

  // Comparison bar: generation t/s relative to the fastest model in this run.
  if (maxGen != null) {
    row.dataset.gen = String(maxGen);
    const rank = document.createElement('div');
    rank.className = 'bench-rank';
    rank.innerHTML = '<span class="bench-rank-lbl">gen vs fastest</span>'
      + '<div class="bench-rank-track"><div class="bench-rank-fill"></div></div>'
      + '<span class="bench-rank-pct"></span>';
    row.appendChild(rank);
  }
  rows.appendChild(row);
  _benchUpdateRankBars();
}

// Recompute the gen-vs-fastest comparison bars across all rendered result rows.
function _benchUpdateRankBars() {
  const rows = [...document.querySelectorAll('#benchResultRows .bench-result-row')]
    .filter(r => r.dataset.gen != null && r.dataset.gen !== '');
  const vals = rows.map(r => parseFloat(r.dataset.gen)).filter(v => Number.isFinite(v) && v > 0);
  const max = vals.length ? Math.max(...vals) : 0;
  rows.forEach(r => {
    const gen = parseFloat(r.dataset.gen);
    const fill = r.querySelector('.bench-rank-fill');
    const pct = r.querySelector('.bench-rank-pct');
    if (!fill || !max || !Number.isFinite(gen)) return;
    const ratio = Math.max(0, Math.min(100, Math.round((gen / max) * 100)));
    fill.style.width = ratio + '%';
    fill.classList.toggle('lead', gen >= max);
    if (pct) pct.textContent = ratio + '%';
  });
}

// Toggle the benchmark chart between idle (slim dashed placeholder) and active.
function _benchSetChartIdle(idle) {
  const wrap = document.getElementById('benchChartWrap');
  const empty = document.getElementById('benchChartEmpty');
  const canvas = document.getElementById('benchChart');
  if (!wrap) return;
  wrap.classList.toggle('idle', !!idle);
  if (empty) empty.style.display = idle ? '' : 'none';
  if (canvas) canvas.style.display = idle ? 'none' : '';
  if (!idle && _benchChart) { try { _benchChart.resize(); } catch(_) {} }
}

// Save benchmark results for a model to the backend, then update local state and UI. Called when user clicks "Save" on a model's benchmark result row.
function saveBenchmark(model_id, avg_gen_tps, avg_ppt_tps, avg_pg_tps, tool, saveBtn) {
  if (!model_id) return;
  fetch('/api/benchmark/store', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({model_id, avg_gen_tps, avg_ppt_tps, avg_pg_tps, bench_tool: tool, switches: _benchSwitches})
  }).then(r => r.json()).then(d => {
    if (!d.ok) { alert(d.error || 'Save failed'); return; }
    _benchData[model_id] = {model_id, avg_gen_tps, avg_ppt_tps, avg_pg_tps, bench_tool: tool};
    if (saveBtn) saveBtn.textContent = '✓ Saved';
    if (typeof renderModelCards === 'function') renderModelCards();
  }).catch(e => alert('Save failed: ' + e));
}

// Clear stored benchmark data for a model on the backend, then update local state and UI. Called when user clicks "✕" on a model's benchmark result row.
function clearStoredBenchmark(model_id, rowEl) {
  fetch('/api/benchmark/results/' + encodeURIComponent(model_id), {method:'DELETE'})
    .then(r => r.json()).then(() => {
      delete _benchData[model_id];
      if (rowEl) rowEl.remove();
      const rowsEl = document.getElementById('benchResultRows');
      // Hide the results panel once the last model card is removed.
      if (rowsEl && rowsEl.querySelectorAll('.bench-result-row').length === 0) {
        rowsEl.innerHTML = '';
        document.getElementById('benchResults').classList.remove('shown');
      }
      if (typeof renderModelCards === 'function') renderModelCards();
    });
}
